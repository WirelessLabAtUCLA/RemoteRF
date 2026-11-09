"""Native-call accounting and bounded deferred cleanup shared by Server/Host.

Bookkeeping locks are never held across native calls. Cleanup keeps strong
references until admitted calls and worker threads finish. A fixed daemon pool
contains Python waits, not native crashes; stuck cleanup retains its record.
"""
from contextlib import contextmanager
from queue import Queue, Full
from threading import Event, Lock, Thread
import time


class Activity:
    def __init__(self):
        self._lock = Lock()
        self._count = 0

    @contextmanager
    def use(self):
        with self._lock:
            self._count += 1
        try:
            yield
        finally:
            with self._lock:
                self._count -= 1

    def idle(self):
        with self._lock:
            return self._count == 0


class Cleanup:
    def __init__(self, ready, run):
        self.ready = ready
        self.run = run
        self.done = Event()
        self.error = None
        self.running = False
        self.blocked = False


class _Reaper:
    def __init__(self):
        self.lock = Lock()
        self.pending = set()
        self.queue = Queue(maxsize=64)
        for index in range(4):
            Thread(target=self._worker, name=f"remoterf-native-cleanup-{index}", daemon=True).start()
        Thread(target=self._poll, name="remoterf-native-reaper", daemon=True).start()

    def add(self, job):
        with self.lock:
            # Device/session/handle limits bound live records. An explicit
            # global cap also prevents creating unlimited test/runtime managers.
            if len(self.pending) >= 8192:
                raise RuntimeError("native cleanup capacity exhausted; device remains busy")
            self.pending.add(job)

    def _poll(self):
        while True:
            with self.lock:
                pending = tuple(self.pending)
            for job in pending:
                if job.running or job.blocked:
                    continue
                try:
                    ready = job.ready()
                except BaseException as exc:
                    # A bad readiness check must not kill cleanup for every
                    # radio. Keep this record fenced for operator inspection.
                    job.error = exc
                    job.blocked = True
                    continue
                if not ready:
                    continue
                job.running = True
                try:
                    self.queue.put_nowait(job)
                except Full:
                    job.running = False
                    break
            time.sleep(0.01)

    def _worker(self):
        while True:
            job = self.queue.get()
            try:
                job.run()
            except BaseException as exc:
                # Retain the failure for the owner; never kill a cleanup worker
                # or lose attempts for unrelated devices.
                job.error = exc
            finally:
                job.done.set()
                with self.lock:
                    self.pending.discard(job)
                self.queue.task_done()


_reaper = None
_init_lock = Lock()


def submit_cleanup(ready, run):
    global _reaper
    with _init_lock:
        if _reaper is None:
            _reaper = _Reaper()
        job = Cleanup(ready, run)
        _reaper.add(job)
    return job
