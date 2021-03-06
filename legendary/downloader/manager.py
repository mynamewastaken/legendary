#!/usr/bin/env python
# coding: utf-8

# please don't look at this code too hard, it's a mess.

import logging
import os
import time

from collections import Counter, defaultdict, deque
from logging.handlers import QueueHandler
from multiprocessing import cpu_count, Process, Queue as MPQueue
from multiprocessing.shared_memory import SharedMemory
from queue import Empty
from sys import exit
from threading import Condition, Thread

from legendary.downloader.workers import DLWorker, FileWorker
from legendary.models.downloading import *
from legendary.models.manifest import ManifestComparison, Manifest


class DLManager(Process):
    def __init__(self, download_dir, base_url, cache_dir=None, status_q=None,
                 max_jobs=100, max_failures=5, max_workers=0, update_interval=1.0,
                 max_shared_memory=1024 * 1024 * 1024, resume_file=None):
        super().__init__(name='DLManager')
        self.log = logging.getLogger('DLM')
        self.proc_debug = False

        self.base_url = base_url
        self.dl_dir = download_dir
        self.cache_dir = cache_dir if cache_dir else os.path.join(download_dir, '.cache')

        # All the queues!
        self.logging_queue = None
        self.dl_worker_queue = None
        self.writer_queue = None
        self.dl_result_q = None
        self.writer_result_q = None
        self.max_jobs = max_jobs
        self.max_workers = max_workers if max_workers else min(cpu_count() * 2, 16)

        # Analysis stuff
        self.analysis = None
        self.tasks = deque()
        self.dl_cache_guids = set()  # guids that should be cached
        self.chunks_to_dl = deque()
        self.chunk_data_list = None

        # shared memory stuff
        self.max_shared_memory = max_shared_memory  # 1 GiB by default
        self.sms = deque()
        self.shared_memory = None

        # Interval for log updates and pushing updates to the queue
        self.update_interval = update_interval
        self.status_queue = status_q  # queue used to relay status info back to GUI/CLI

        # behaviour settings
        self.max_failures = max_failures
        self.resume_file = resume_file

        # cross-thread runtime information
        self.running = True
        self.active_tasks = 0
        self.children = []
        self.threads = []
        self.conditions = []
        # bytes downloaded and decompressed since last report
        self.bytes_downloaded_since_last = 0
        self.bytes_decompressed_since_last = 0
        # bytes written since last report
        self.bytes_written_since_last = 0
        # bytes read since last report
        self.bytes_read_since_last = 0
        # chunks written since last report
        self.num_processed_since_last = 0
        self.num_tasks_processed_since_last = 0

    def download_job_manager(self, task_cond: Condition, shm_cond: Condition):
        while self.chunks_to_dl and self.running:
            while self.active_tasks < self.max_workers * 2 and self.chunks_to_dl:
                try:
                    sms = self.sms.popleft()
                    no_shm = False
                except IndexError:  # no free cache
                    no_shm = True
                    break

                c_guid = self.chunks_to_dl.popleft()
                chunk = self.chunk_data_list.get_chunk_by_guid(c_guid)
                self.log.debug(f'Adding {chunk.guid_num} (active: {self.active_tasks})')
                try:
                    self.dl_worker_queue.put(DownloaderTask(url=self.base_url + '/' + chunk.path,
                                                            chunk_guid=c_guid, shm=sms),
                                             timeout=1.0)
                except Exception as e:
                    self.log.warning(f'Failed to add to download queue: {e!r}')
                    self.chunks_to_dl.appendleft(c_guid)
                    break

                self.active_tasks += 1
            else:
                # active tasks limit hit, wait for tasks to finish
                with task_cond:
                    self.log.debug('Waiting for download tasks to complete..')
                    task_cond.wait(timeout=1.0)
                    continue

            if no_shm:
                # if we break we ran out of shared memory, so wait for that.
                with shm_cond:
                    self.log.debug('Waiting for more shared memory...')
                    shm_cond.wait(timeout=1.0)

        self.log.info('Download Job Manager quitting...')

    def dl_results_handler(self, task_cond: Condition):
        in_buffer = dict()

        task = self.tasks.popleft()
        current_file = ''

        while task and self.running:
            if isinstance(task, FileTask):  # this wasn't necessarily a good idea...
                try:
                    if task.empty:
                        self.writer_queue.put(WriterTask(task.filename, empty=True), timeout=1.0)
                    elif task.rename:
                        self.writer_queue.put(WriterTask(task.filename, rename=True,
                                                         delete=task.delete,
                                                         old_filename=task.temporary_filename),
                                              timeout=1.0)
                    elif task.delete:
                        self.writer_queue.put(WriterTask(task.filename, delete=True), timeout=1.0)
                    elif task.open:
                        self.writer_queue.put(WriterTask(task.filename, fopen=True), timeout=1.0)
                        current_file = task.filename
                    elif task.close:
                        self.writer_queue.put(WriterTask(task.filename, close=True), timeout=1.0)
                except Exception as e:
                    self.tasks.appendleft(task)
                    self.log.warning(f'Adding to queue failed: {e!r}')
                    continue

                try:
                    task = self.tasks.popleft()
                except IndexError:  # finished
                    break
                continue

            while (task.chunk_guid in in_buffer) or task.chunk_file:
                res_shm = None
                if not task.chunk_file:  # not re-using from an old file
                    res_shm = in_buffer[task.chunk_guid].shm

                try:
                    self.writer_queue.put(WriterTask(
                        filename=current_file, shared_memory=res_shm,
                        chunk_offset=task.chunk_offset, chunk_size=task.chunk_size,
                        chunk_guid=task.chunk_guid, release_memory=task.cleanup,
                        old_file=task.chunk_file  # todo on-disk cache
                    ), timeout=1.0)
                except Exception as e:
                    self.log.warning(f'Adding to queue failed: {e!r}')
                    break

                if task.cleanup and not task.chunk_file:
                    del in_buffer[task.chunk_guid]

                try:
                    task = self.tasks.popleft()
                    if isinstance(task, FileTask):
                        break
                except IndexError:  # finished
                    task = None
                    break
            else:  # only enter blocking code if the loop did not break
                try:
                    res = self.dl_result_q.get(timeout=1)
                    self.active_tasks -= 1
                    with task_cond:
                        task_cond.notify()

                    if res.success:
                        in_buffer[res.guid] = res
                        self.bytes_downloaded_since_last += res.compressed_size
                        self.bytes_decompressed_since_last += res.size
                    else:
                        self.log.error(f'Download for {res.guid} failed, retrying...')
                        try:
                            self.dl_worker_queue.put(DownloaderTask(
                                url=res.url, chunk_guid=res.guid, shm=res.shm
                            ), timeout=1.0)
                            self.active_tasks += 1
                        except Exception as e:
                            self.log.warning(f'Failed adding retry task to queue! {e!r}')
                            # If this failed for whatever reason, put the chunk at the front of the DL list
                            self.chunks_to_dl.appendleft(res.chunk_guid)
                except Empty:
                    pass
                except Exception as e:
                    self.log.warning(f'Unhandled exception when trying to read download result queue: {e!r}')

        self.log.info('Download result handler quitting...')

    def fw_results_handler(self, shm_cond: Condition):
        while self.running:
            try:
                res = self.writer_result_q.get(timeout=1.0)
                self.num_tasks_processed_since_last += 1

                if res.closed and self.resume_file:
                    # write last completed file to super simple resume file
                    with open(self.resume_file, 'ab') as rf:
                        rf.write(f'{res.filename}\n'.encode('utf-8'))

                if res.kill:
                    self.log.info('Got termination command in FW result handler')
                    break

                if not res.success:
                    # todo make this kill the installation process or at least skip the file and mark it as failed
                    self.log.fatal(f'Writing for {res.filename} failed!')
                if res.release_memory:
                    self.sms.appendleft(res.shm)
                    with shm_cond:
                        shm_cond.notify()

                if res.chunk_guid:
                    self.bytes_written_since_last += res.size
                    # if there's no shared memory we must have read from disk.
                    if not res.shm:
                        self.bytes_read_since_last += res.size
                    self.num_processed_since_last += 1

            except Empty:
                continue
            except Exception as e:
                self.log.warning(f'Exception when trying to read writer result queue: {e!r}')
        self.log.info('Writer result handler quitting...')

    def run_analysis(self, manifest: Manifest, old_manifest: Manifest = None,
                     patch=True, resume=True) -> AnalysisResult:
        """
        Run analysis on manifest and old manifest (if not None) and return a result
        with a summary resources required in order to install the provided manifest.

        :param manifest: Manifest to install
        :param old_manifest: Old manifest to patch from (if applicable)
        :param patch: Patch instead of redownloading the entire file
        :param resume: Continue based on resume file if it exists
        :return: AnalysisResult
        """

        analysis_res = AnalysisResult()
        analysis_res.install_size = sum(fm.file_size for fm in manifest.file_manifest_list.elements)
        analysis_res.biggest_chunk = max(c.window_size for c in manifest.chunk_data_list.elements)
        analysis_res.biggest_file_size = max(f.file_size for f in manifest.file_manifest_list.elements)
        is_1mib = analysis_res.biggest_chunk == 1024 * 1024
        self.log.debug(f'Biggest chunk size: {analysis_res.biggest_chunk} bytes (== 1 MiB? {is_1mib})')

        self.log.debug(f'Creating manifest comparison...')
        mc = ManifestComparison.create(manifest, old_manifest)
        analysis_res.manifest_comparison = mc

        if resume and self.resume_file and os.path.exists(self.resume_file):
            try:
                completed_files = set(i.strip() for i in open(self.resume_file).readlines())
                # remove completed files from changed/added and move them to unchanged for the analysis.
                mc.added -= completed_files
                mc.changed -= completed_files
                mc.unchanged |= completed_files
                self.log.debug(f'Skipped {len(completed_files)} files based on resume data!')
            except Exception as e:
                self.log.warning(f'Reading resume file failed: {e!r}, continuing as normal...')

        if mc.removed:
            analysis_res.removed = len(mc.removed)
            self.log.debug(f'{analysis_res.removed} removed files')
        if mc.added:
            analysis_res.added = len(mc.added)
            self.log.debug(f'{analysis_res.added} added files')
        if mc.changed:
            analysis_res.changed = len(mc.changed)
            self.log.debug(f'{analysis_res.changed} changed files')
        if mc.unchanged:
            analysis_res.unchanged = len(mc.unchanged)
            self.log.debug(f'{analysis_res.unchanged} unchanged files')

        references = Counter()
        chunkstream_starts = list()
        # Chunks can have multiple entire files in them, the deque for a guid contains all files that start
        # in that chunk (sorted by offset) so we can quickly and easily find the next link in the chunkstream.
        # A nice side effect is that we can use this to check whether or not we missed something in the process.
        chunk_to_file_map = defaultdict(deque)

        # Find chunkstream starts and also count references to each chunk
        # Note that this has to be sorted to ensure the file map will be in the correct order
        self.log.debug('Looking for chunkstreams and counting references...')
        for fm in sorted(manifest.file_manifest_list.elements,
                         key=lambda x: x.chunk_parts[0].offset if x.chunk_parts else 0):
            if not fm.chunk_parts:
                self.tasks.append(FileTask(fm.filename, empty=True))
                continue

            if fm.filename in mc.unchanged:
                analysis_res.unchanged += fm.file_size

            for index, cp in enumerate(fm.chunk_parts):
                if index == 0:
                    chunk_to_file_map[cp.guid_num].append(fm)
                    if cp.offset == 0:
                        self.log.debug(f'Found chunk stream start: {fm.filename}, {fm.chunk_parts[0]}')
                        chunkstream_starts.append(fm.chunk_parts[0])

                # do not add references in case the file is unchanged and we do not need to download it anyway
                if fm.filename not in mc.unchanged:
                    references[cp.guid_num] += 1

        # determine reusable chunks and prepare lookup table for reusable ones
        re_usable = defaultdict(dict)
        if old_manifest and mc.changed and patch:
            self.log.debug('Analyzing manifests for re-usable chunks...')
            for changed in mc.changed:
                old_file = old_manifest.file_manifest_list.get_file_by_path(changed)
                new_file = manifest.file_manifest_list.get_file_by_path(changed)

                existing_chunks = dict()
                off = 0
                for cp in old_file.chunk_parts:
                    existing_chunks[(cp.guid_num, cp.offset, cp.size)] = off
                    off += cp.size

                for cp in new_file.chunk_parts:
                    key = (cp.guid_num, cp.offset, cp.size)
                    if key in existing_chunks:
                        references[cp.guid_num] -= 1
                        re_usable[changed][key] = existing_chunks[key]
                        analysis_res.reuse_size += cp.size

        last_cache_size = current_cache_size = 0
        cached = set()
        # Using this secondary set is orders of magnitude faster than checking the deque.
        chunks_in_dl_list = set()

        # run through the chunkstreams and create the download jobs,
        # also determine minimum runtime cache requirement.
        # Yeah this is a bit of a mess but still runs extremely
        # quickly even with tens of thousands of files/chunks
        self.log.debug('Creating filetasks and chunktasks...')
        for next_chunk in chunkstream_starts:
            self.log.debug(f'- Chunkstream start: {next_chunk!r}')

            while file_deque := chunk_to_file_map.get(next_chunk.guid_num):
                current_file = file_deque.popleft()

                if len(file_deque) == 0:
                    del chunk_to_file_map[next_chunk.guid_num]

                # skip unchanged files
                if current_file.filename in mc.unchanged:
                    # self.log.debug(f' + Skipping unchanged file: {current_file.filename}')
                    next_chunk = current_file.chunk_parts[-1]
                    continue

                existing_chunks = re_usable.get(current_file.filename, None)
                chunk_tasks = []
                reused = 0

                for cp in current_file.chunk_parts:
                    ct = ChunkTask(cp.guid_num, cp.offset, cp.size)

                    # re-use the chunk from the existing file if we can
                    if existing_chunks and (cp.guid_num, cp.offset, cp.size) in existing_chunks:
                        reused += 1
                        ct.chunk_file = current_file.filename
                        ct.chunk_offset = existing_chunks[(cp.guid_num, cp.offset, cp.size)]
                    else:
                        # add to DL list if not already in it
                        if cp.guid_num not in chunks_in_dl_list:
                            self.chunks_to_dl.append(cp.guid_num)
                            chunks_in_dl_list.add(cp.guid_num)

                        # if chunk has more than one use or is already in cache,
                        # check if we need to add or remove it again.
                        if references[cp.guid_num] > 1 or cp.guid_num in cached:
                            references[cp.guid_num] -= 1

                            if references[cp.guid_num] < 1:  # delete from cache again
                                current_cache_size -= analysis_res.biggest_chunk
                                cached.remove(cp.guid_num)
                                ct.cleanup = True
                            elif cp.guid_num not in cached:  # add to cache
                                self.dl_cache_guids.add(cp.guid_num)
                                cached.add(cp.guid_num)
                                current_cache_size += analysis_res.biggest_chunk
                        else:
                            ct.cleanup = True

                    chunk_tasks.append(ct)

                if reused:
                    self.log.debug(f' + Reusing {reused} chunks from: {current_file.filename}')
                    self.tasks.append(FileTask(current_file.filename + u'.tmp', fopen=True))
                else:
                    self.tasks.append(FileTask(current_file.filename, fopen=True))

                self.tasks.extend(chunk_tasks)

                if reused:
                    self.tasks.append(FileTask(current_file.filename + u'.tmp', close=True))
                    self.tasks.append(FileTask(current_file.filename, delete=True, rename=True,
                                               temporary_filename=current_file.filename + u'.tmp'))
                else:
                    self.tasks.append(FileTask(current_file.filename, close=True))

                if current_cache_size > last_cache_size:
                    self.log.debug(f' * New maximum cache size: {current_cache_size / 1024 / 1024:.02f} MiB')
                    last_cache_size = current_cache_size

                next_chunk = current_file.chunk_parts[-1]

        # If this is not empty something went horribly wrong.
        if chunk_to_file_map:
            raise ValueError('Some files were not processed:', chunk_to_file_map)

        self.log.debug(f'Final cache size requirement: {last_cache_size / 1024 / 1024} MiB.')
        analysis_res.min_memory = last_cache_size + (1024 * 1024 * 32)  # add some padding just to be safe

        # Todo implement on-disk caching to avoid this issue.
        if analysis_res.min_memory > self.max_shared_memory:
            shared_mib = f'{self.max_shared_memory / 1024 / 1024:.01f} MiB'
            required_mib = f'{analysis_res.min_memory / 1024 / 1024:.01} MiB'
            raise MemoryError(f'Current shared memory cache is smaller than required! {shared_mib} < {required_mib}')

        # calculate actual dl and patch write size.
        analysis_res.dl_size = \
            sum(c.file_size for c in manifest.chunk_data_list.elements if c.guid_num in chunks_in_dl_list)
        analysis_res.uncompressed_dl_size = \
            sum(c.window_size for c in manifest.chunk_data_list.elements if c.guid_num in chunks_in_dl_list)

        # add jobs to remove files
        for fname in mc.removed:
            self.tasks.append(FileTask(fname, delete=True))

        analysis_res.num_chunks_cache = len(self.dl_cache_guids)
        self.chunk_data_list = manifest.chunk_data_list
        self.analysis = analysis_res

        return analysis_res

    def run(self):
        if not self.analysis:
            raise ValueError('Did not run analysis before trying to run download!')

        # Subprocess will use its own root logger that logs to a Queue instead
        _root = logging.getLogger()
        _root.setLevel(logging.DEBUG if self.proc_debug else logging.INFO)
        if self.logging_queue:
            _root.handlers = []
            _root.addHandler(QueueHandler(self.logging_queue))

        self.log = logging.getLogger('DLMProc')
        self.log.info(f'Download Manager running with process-id: {os.getpid()}')

        try:
            self.run_real()
        except KeyboardInterrupt:
            self.log.warning('Immediate exit requested!')
            self.running = False

            # send conditions to unlock threads if they aren't already
            for cond in self.conditions:
                with cond:
                    cond.notify()

            # make sure threads are dead.
            for t in self.threads:
                t.join(timeout=5.0)
                if t.is_alive():
                    self.log.warning(f'Thread did not terminate! {repr(t)}')

            # clean up all the queues, otherwise this process won't terminate properly
            for name, q in zip(('Download jobs', 'Writer jobs', 'Download results', 'Writer results'),
                               (self.dl_worker_queue, self.writer_queue, self.dl_result_q, self.writer_result_q)):
                self.log.debug(f'Cleaning up queue "{name}"')
                try:
                    while True:
                        _ = q.get_nowait()
                except Empty:
                    q.close()
                    q.join_thread()

    def run_real(self):
        self.shared_memory = SharedMemory(create=True, size=self.max_shared_memory)
        self.log.debug(f'Created shared memory of size: {self.shared_memory.size / 1024 / 1024:.02f} MiB')

        # create the shared memory segments and add them to their respective pools
        for i in range(int(self.shared_memory.size / self.analysis.biggest_chunk)):
            _sms = SharedMemorySegment(offset=i * self.analysis.biggest_chunk,
                                       end=i * self.analysis.biggest_chunk + self.analysis.biggest_chunk)
            self.sms.append(_sms)

        self.log.debug(f'Created {len(self.sms)} shared memory segments.')

        # Create queues
        self.dl_worker_queue = MPQueue(-1)
        self.writer_queue = MPQueue(-1)
        self.dl_result_q = MPQueue(-1)
        self.writer_result_q = MPQueue(-1)

        self.log.info(f'Starting download workers...')
        for i in range(self.max_workers):
            w = DLWorker(f'DLWorker {i + 1}', self.dl_worker_queue, self.dl_result_q,
                         self.shared_memory.name, logging_queue=self.logging_queue)
            self.children.append(w)
            w.start()

        self.log.info('Starting file writing worker...')
        writer_p = FileWorker(self.writer_queue, self.writer_result_q, self.dl_dir,
                              self.shared_memory.name, self.cache_dir, self.logging_queue)
        self.children.append(writer_p)
        writer_p.start()

        num_chunk_tasks = sum(isinstance(t, ChunkTask) for t in self.tasks)
        num_dl_tasks = len(self.chunks_to_dl)
        num_tasks = len(self.tasks)
        num_shared_memory_segments = len(self.sms)
        self.log.debug(f'Chunks to download: {num_dl_tasks}, File tasks: {num_tasks}, Chunk tasks: {num_chunk_tasks}')

        # active downloader tasks
        self.active_tasks = 0
        processed_chunks = 0
        processed_tasks = 0
        total_dl = 0
        total_write = 0

        # synchronization conditions
        shm_cond = Condition()
        task_cond = Condition()
        self.conditions = [shm_cond, task_cond]

        # start threads
        s_time = time.time()
        self.threads.append(Thread(target=self.download_job_manager, args=(task_cond, shm_cond)))
        self.threads.append(Thread(target=self.dl_results_handler, args=(task_cond,)))
        self.threads.append(Thread(target=self.fw_results_handler, args=(shm_cond,)))

        for t in self.threads:
            t.start()

        last_update = time.time()

        while processed_tasks < num_tasks:
            delta = time.time() - last_update
            if not delta:
                time.sleep(self.update_interval)
                continue

            # update all the things
            processed_chunks += self.num_processed_since_last
            processed_tasks += self.num_tasks_processed_since_last

            total_dl += self.bytes_downloaded_since_last
            total_write += self.bytes_written_since_last

            dl_speed = self.bytes_downloaded_since_last / delta
            dl_unc_speed = self.bytes_decompressed_since_last / delta
            w_speed = self.bytes_written_since_last / delta
            r_speed = self.bytes_read_since_last / delta
            c_speed = self.num_processed_since_last / delta

            # set temporary counters to 0
            self.bytes_read_since_last = self.bytes_written_since_last = 0
            self.bytes_downloaded_since_last = self.num_processed_since_last = 0
            self.bytes_decompressed_since_last = self.num_tasks_processed_since_last = 0
            last_update = time.time()

            perc = (processed_chunks / num_chunk_tasks) * 100
            self.log.info(f'\n============== {time.time() - s_time:.01f} seconds since start')
            self.log.info(f'Progress: {processed_chunks}/{num_chunk_tasks} ({perc:.02f}%) chunk tasks processed.')
            self.log.info(f'Downloaded: {total_dl / 1024 / 1024:.02f} MiB, '
                          f'Written: {total_write / 1024 / 1024:.02f} MiB')

            # speed meters
            self.log.info('Speeds:')
            self.log.info(f' + Download     - {dl_speed / 1024 / 1024:.02f} MiB/s (raw) '
                          f'/ {dl_unc_speed / 1024 / 1024:.02f} MiB/s (decompressed)')
            self.log.info(f' + Write (disk) - {w_speed / 1024 / 1024:.02f} MiB/s')
            self.log.info(f' + Read (disk)  - {r_speed / 1024 / 1024:.02f} MiB/s')
            self.log.info(f' + Tasks        - {c_speed:.02f} Chunks/s')
            self.log.info(f'Active download tasks: {self.active_tasks}')

            # shared memory debugging
            total_avail = len(self.sms)
            total_used = (num_shared_memory_segments - total_avail) * (self.analysis.biggest_chunk / 1024 / 1024)
            self.log.info(f'Shared memory usage: {total_used} MiB, available: {total_avail}')

            # send status update to back to instantiator (if queue exists)
            if self.status_queue:
                try:
                    self.status_queue.put(UIUpdate(
                        progress=perc, download_speed=dl_unc_speed, write_speed=w_speed, read_speed=r_speed,
                        memory_usage=total_used * 1024 * 1024
                    ), timeout=1.0)
                except Exception as e:
                    self.log.warning(f'Failed to send status update to queue: {e!r}')

            time.sleep(self.update_interval)

        for i in range(self.max_workers):
            self.dl_worker_queue.put_nowait(DownloaderTask(kill=True))

        self.writer_queue.put_nowait(WriterTask('', kill=True))
        self.log.info('Waiting for writer process to finish...')

        writer_p.join(timeout=10.0)
        if writer_p.exitcode is None:
            self.log.warning(f'Terminating writer process {e!r}')
            writer_p.terminate()

        # forcibly kill DL workers that are not actually dead yet
        for child in self.children:
            if child.exitcode is None:
                child.terminate()

        # make sure all the threads are dead.
        for t in self.threads:
            t.join(timeout=5.0)
            if t.is_alive():
                self.log.warning(f'Thread did not terminate! {repr(t)}')

        # clean up resume file
        if self.resume_file:
            try:
                os.remove(self.resume_file)
            except OSError as e:
                self.log.warning(f'Failed to remove resume file: {e!r}')

        # close up shared memory
        self.shared_memory.close()
        self.shared_memory.unlink()
        self.shared_memory = None

        # finally, exit the process.
        exit(0)
