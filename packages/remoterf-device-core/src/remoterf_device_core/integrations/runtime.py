"""Bounded application lifecycle, deliberately not exposed by device RPC yet.

Bindings are trusted runtime callbacks, not adapter-supplied functions. They
must fence every physical alias/access route and verify native release. Hooks
may hang: daemon worker capacity and hardware ownership then remain occupied.
No Python timeout is treated as termination of native work.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from queue import Queue
from threading import Event, RLock, Thread
import time
from uuid import uuid4

from .contract import Context
from .ownership import Gate
from .payload import IntegrationError, object_data


class _Pool:
    def __init__(self, workers, capacity, name):
        self.queue = Queue(maxsize=capacity)
        self.threads = []
        for index in range(workers):
            thread = Thread(target=self._run, name=f"{name}-{index}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def _run(self):
        while True:
            task = self.queue.get()
            try:
                if task is None:
                    return
                task()
            finally:
                self.queue.task_done()


@dataclass(frozen=True)
class Binding:
    """Trusted session binding. All hooks run outside runtime/manager locks.

    release_native must reject active/closing children, drop the native root,
    and return only after release; native_released must independently verify it.
    restore_native returns after eager reacquisition/metadata refresh. The lease
    stays held across that call. resources_released verifies adapter resources
    after stop (e.g. a supervised process group exited); stop returning is not
    sufficient evidence. authority checks the exact reservation generation.
    """
    gate: Gate
    device_id: str
    device_type: str
    session_id: str
    generation: int
    work_dir: Path
    authority: object
    release_native: object
    native_released: object
    restore_native: object
    resources_released: object
    claim: object = None


class Runtime:
    def __init__(self, registry, *, capacity=16, workers=4, timeout=30.0, clock=time.monotonic):
        if not 1 <= workers <= capacity <= 64 or not 0 < timeout < 3600:
            raise ValueError("invalid runtime bounds")
        self.registry = registry
        self.capacity = capacity
        self.timeout = timeout
        self.clock = clock
        self._lock = RLock()
        self._operations = {}
        self._keys = {}
        self._draining = False
        self._closed = False
        self._work = _Pool(workers, capacity, "remoterf-integration")
        # Cleanup has reserved execution capacity independent of starts/actions.
        self._cleanup = _Pool(workers, capacity, "remoterf-cleanup")
        self._exit = Event()
        self._watcher = Thread(target=self._watch, name="remoterf-integration-deadlines", daemon=True)
        self._watcher.start()

    def _watch(self):
        while not self._exit.wait(0.05):
            self.poll()

    @staticmethod
    def _scope(binding):
        return (binding.session_id, binding.generation, binding.gate.physical_id)

    def start(self, binding: Binding, integration_id: str, profile: str, configuration: dict, *, request_key: str):
        if type(request_key) is not str or not 1 <= len(request_key) <= 128:
            raise IntegrationError("invalid_request_key", "a bounded request key is required")
        configuration = self.registry.validate_start(integration_id, device_id=binding.device_id,
            device_type=binding.device_type, profile=profile, configuration=configuration)
        fingerprint = sha256(json.dumps([integration_id, profile, configuration], sort_keys=True).encode()).hexdigest()
        key = (*self._scope(binding), request_key)
        # Authority is never evaluated while holding the runtime lock.
        if not binding.authority():
            raise IntegrationError("expired", "reservation authority ended")
        with self._lock:
            previous = self._keys.get(key)
            if previous:
                op = self._operations[previous]
                if op["fingerprint"] != fingerprint:
                    raise IntegrationError("request_conflict", "request key was reused with different inputs")
                return self._snapshot(op)
            if self._draining or len(self._operations) >= self.capacity:
                raise IntegrationError("overloaded", "integration runtime is draining or full")
            # A maximum of capacity operations means queue submission cannot
            # overflow: each operation has at most one work and one cleanup task.
            claim = lambda: binding.gate.reserve(session=binding.session_id, generation=binding.generation,
                                                 integration=integration_id)
            # A binding may add a short, nonblocking native-resource admission
            # check around claim. It must not call native/adapter code here.
            lease = binding.claim(claim) if binding.claim is not None else claim()
            cancelled = Event()
            op = dict(id=uuid4().hex, state="STARTING", phase="queued", error=None, binding=binding, lease=lease,
                      integration=integration_id, profile=profile, configuration=configuration,
                      fingerprint=fingerprint, key=key, cancelled=cancelled, adapter=None,
                      active=True, cleanup=False, cleanup_attempted=False, released=False,
                      release_attempted=False, native_released=False, deadline=self.clock() + self.timeout,
                      context=Context(binding.device_id, binding.device_type, binding.generation,
                                      binding.work_dir, cancelled), details={})
            self._operations[op["id"]] = op
            self._keys[key] = op["id"]
            self._work.queue.put_nowait(lambda: self._start(op))
            return self._snapshot(op)

    def _check(self, op):
        if op["cancelled"].is_set() or not op["binding"].authority():
            raise IntegrationError("cancelled", "application operation was cancelled")

    def _start(self, op):
        try:
            # Durability may block on storage; it must not hold the runtime
            # lock or the caller/RPC thread. No native side effect precedes it.
            self._phase(op, "persisting_owner")
            op["binding"].gate.persist(op["lease"])
            self._check(op)
            binding = op["binding"]
            self._phase(op, "releasing_native")
            op["release_attempted"] = True
            binding.release_native()
            if binding.native_released() is not True:
                raise IntegrationError("recovery_required", "native release could not be verified")
            op["native_released"] = True
            self._check(op)
            self._phase(op, "loading_adapter")
            adapter = self.registry.create(op["integration"], op["context"])
            with self._lock:
                op["adapter"] = adapter
            self._check(op)
            self._phase(op, "validating")
            normalized = object_data(adapter.validate(op["profile"], op["configuration"]))
            self._check(op)
            self._phase(op, "starting")
            adapter.start(op["profile"], normalized)
            self._check(op)
            self._phase(op, "checking_readiness")
            details = object_data(adapter.status(), max_bytes=48 * 1024)
            self._check(op)
            with self._lock:
                if op["cancelled"].is_set():
                    raise IntegrationError("cancelled", "application operation was cancelled")
                op.update(state="RUNNING", phase="running", details=details)
        except BaseException as exc:
            self._fail(op, exc)
        finally:
            with self._lock:
                op["active"] = False
                if op["cancelled"].is_set():
                    self._schedule_cleanup(op)

    def _fail(self, op, exc):
        with self._lock:
            code = exc.code if isinstance(exc, IntegrationError) else f"{op['phase']}_failed"
            op.update(state="ERROR", error=code)
            op["cancelled"].set()

    def _phase(self, op, phase):
        with self._lock:
            op["phase"] = phase

    def _schedule_cleanup(self, op):
        if not op["active"] and not op["cleanup_attempted"] and not op["released"]:
            op.update(cleanup=True, cleanup_attempted=True, deadline=self.clock() + self.timeout)
            self._cleanup.queue.put_nowait(lambda: self._stop(op))

    def _stop(self, op):
        try:
            binding = op["binding"]
            self._phase(op, "stopping")
            if op["release_attempted"] and not op["native_released"]:
                raise IntegrationError("recovery_required", "native release failed; explicit recovery is required")
            if op["adapter"] is not None:
                op["adapter"].stop()
            self._phase(op, "verifying_release")
            if binding.resources_released() is not True:
                raise IntegrationError("recovery_required", "application resources remain owned")
            # Reacquisition is runtime work, never hidden in a normal UHD call.
            # Expired sessions must not reacquire; the next holder opens normally.
            if op["native_released"] and binding.authority():
                self._phase(op, "restoring_native")
                binding.restore_native()
                # Authority can end during a cold native reacquisition.
                if not binding.authority():
                    binding.release_native()
                    if binding.native_released() is not True:
                        raise IntegrationError("recovery_required", "expired native reacquisition still owns resources")
            elif op["native_released"] and binding.native_released() is not True:
                raise IntegrationError("recovery_required", "native resources remain owned")
            self._phase(op, "releasing_lease")
            binding.gate.release(op["lease"], verify=binding.resources_released)
            with self._lock:
                op.update(released=True, phase="complete", state="IDLE" if op["error"] is None else "ERROR")
        except BaseException as exc:
            self._fail(op, exc)
        finally:
            with self._lock:
                op["cleanup"] = False

    def _owned(self, binding, operation_id):
        op = self._operations.get(operation_id)
        if op is None or self._scope(op["binding"]) != self._scope(binding):
            raise IntegrationError("unknown_operation", "operation does not belong to this session generation")
        return op

    @staticmethod
    def _snapshot(op):
        return {"operation_id": op["id"], "state": op["state"], "phase": op["phase"], "error": op["error"],
                "generation": op["binding"].generation, "released": op["released"],
                "details": object_data(op["details"])}

    def status(self, binding, operation_id):
        # Cached snapshot: status never calls potentially hung adapter code.
        with self._lock:
            return self._snapshot(self._owned(binding, operation_id))

    def stop(self, binding, operation_id):
        with self._lock:
            op = self._owned(binding, operation_id)
            if op["released"]:
                return self._snapshot(op)
            op["cancelled"].set()
            if op["state"] != "ERROR":
                op["state"] = "STOPPING"
            self._schedule_cleanup(op)
            return self._snapshot(op)

    def poll(self):
        with self._lock:
            operations = list(self._operations.values())
        for op in operations:
            try:
                valid = op["binding"].authority()
            except BaseException:
                valid = False
            with self._lock:
                if op["released"]:
                    continue
                timed_out = (op["active"] or op["cleanup"]) and self.clock() >= op["deadline"]
                if not valid or timed_out or self._draining:
                    op["cancelled"].set()
                    op.update(state="ERROR" if timed_out or op["error"] else "STOPPING",
                              error="timeout" if timed_out else op["error"])
                    self._schedule_cleanup(op)

    def retire(self, binding):
        """Forget retry outcomes only when the entire session generation ends.

        Unreleased operations are never evicted to make room for new starts.
        """
        with self._lock:
            owned = [op for op in self._operations.values() if self._scope(op["binding"]) == self._scope(binding)]
            if any(not op["released"] or op["active"] or op["cleanup"] for op in owned):
                raise IntegrationError("recovery_required", "session still owns resources")
            for op in owned:
                self._keys.pop(op["key"])
                self._operations.pop(op["id"])

    def recover(self, binding, operation_id, *, verify):
        """Trusted operator reconciliation after failure; never a caller flag.

        verify must establish that *all* native/application owners are gone,
        including surviving processes. A running Python hook cannot be waived.
        """
        with self._lock:
            op = self._owned(binding, operation_id)
            if op["active"] or op["cleanup"] or not op["cleanup_attempted"]:
                raise IntegrationError("device_busy", "cleanup or native work is still active")
            if op["released"]:
                return self._snapshot(op)
            op["cleanup"] = True
        try:
            binding.gate.release(op["lease"], verify=verify)
            with self._lock:
                op.update(released=True, state="ERROR")
                return self._snapshot(op)
        finally:
            with self._lock:
                op["cleanup"] = False

    def shutdown(self):
        """Stop admission/cancel immediately; no unbounded worker join."""
        with self._lock:
            self._draining = True
        self.poll()
        # The watcher remains alive until cleanup drains, or process exit. It
        # is a daemon, as are workers: hung trusted code cannot block exit.

    def close(self):
        """Dispose an already drained runtime (mainly tests/explicit shutdown)."""
        with self._lock:
            if self._closed:
                return
            if any(not op["released"] or op["active"] or op["cleanup"] for op in self._operations.values()):
                raise IntegrationError("recovery_required", "runtime has unresolved work")
            self._draining = True
            self._closed = True
            self._exit.set()
            for pool in (self._work, self._cleanup):
                for _ in pool.threads:
                    pool.queue.put_nowait(None)
