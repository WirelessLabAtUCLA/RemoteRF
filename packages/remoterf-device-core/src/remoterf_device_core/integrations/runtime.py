"""Bounded application lifecycle and actions for the generic integration API.

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
    selector: dict | None = None
    processes: object = None
    prepare: object = None
    resource_owner: str | None = None
    finalize: object = None


class Runtime:
    def __init__(self, registry, *, capacity=16, workers=4, action_capacity=64,
                 timeout=30.0, clock=time.monotonic):
        if (not 1 <= workers <= capacity <= 64 or not 1 <= action_capacity <= 256
                or not 0 < timeout < 3600):
            raise ValueError("invalid runtime bounds")
        self.registry = registry
        self.capacity = capacity
        self.action_capacity = action_capacity
        self.timeout = timeout
        self.clock = clock
        self._lock = RLock()
        self._operations = {}
        self._keys = {}
        self._actions = {}
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
        self._validate_request_key(request_key)
        configuration = self.registry.validate_start(integration_id, device_id=binding.device_id,
            device_type=binding.device_type, profile=profile, configuration=configuration)
        selector = object_data(binding.selector) if binding.selector is not None else None
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
                                                 integration=integration_id, resources=binding.resource_owner)
            # A binding may add a short, nonblocking native-resource admission
            # check around claim. It must not call native/adapter code here.
            lease = binding.claim(claim) if binding.claim is not None else claim()
            cancelled = Event()
            op = dict(id=uuid4().hex, state="STARTING", phase="queued", error=None, binding=binding, lease=lease,
                      integration=integration_id, profile=profile, configuration=configuration,
                      fingerprint=fingerprint, key=key, cancelled=cancelled, adapter=None,
                      active=True, cleanup=False, cleanup_attempted=False, released=False,
                      active_action=None, actions={}, action_keys={},
                      release_attempted=False, native_released=False, deadline=self.clock() + self.timeout,
                      context=Context(binding.device_id, binding.device_type, binding.generation,
                                      binding.work_dir, cancelled, selector=selector,
                                      processes=binding.processes), details={})
            self._operations[op["id"]] = op
            self._keys[key] = op["id"]
            self._work.queue.put_nowait(lambda: self._start(op))
            return self._snapshot(op)

    @staticmethod
    def _validate_request_key(request_key):
        if type(request_key) is not str or not 1 <= len(request_key) <= 128:
            raise IntegrationError("invalid_request_key", "a bounded request key is required")

    def call(self, binding, operation_id, action, parameters, *, request_key):
        """Admit one asynchronous action; retries return the same bounded record.

        Adapter calls are serialized per owner. There is no unbounded per-owner
        queue: another action must wait for the active one to finish. Completed
        results remain until retire(), so capacity exhaustion cannot erase the
        retry history of an operation which might have changed hardware.
        """
        self._validate_request_key(request_key)
        if not binding.authority():
            raise IntegrationError("expired", "reservation authority ended")
        with self._lock:
            op = self._owned(binding, operation_id)
            integration = op["integration"]
        parameters = self.registry.validate_call(integration, device_id=binding.device_id,
            device_type=binding.device_type, action=action, parameters=parameters)
        fingerprint = sha256(json.dumps([action, parameters], sort_keys=True).encode()).hexdigest()
        with self._lock:
            # Recheck after validation: retire/shutdown can run concurrently.
            op = self._owned(binding, operation_id)
            previous = op["action_keys"].get(request_key)
            if previous:
                record = op["actions"][previous]
                if record["fingerprint"] != fingerprint:
                    raise IntegrationError("request_conflict", "request key was reused with different inputs")
                return self._action_snapshot(op, record)
            if self._draining or len(self._actions) >= self.action_capacity:
                raise IntegrationError("overloaded", "integration action capacity is full or draining")
            if op["released"] or op["state"] != "RUNNING" or op["cancelled"].is_set():
                raise IntegrationError("invalid_state", "application is not running")
            if op["active"] or op["cleanup"]:
                raise IntegrationError("operation_busy", "application already has work in progress")
            record = dict(id=uuid4().hex, action=action, fingerprint=fingerprint, state="QUEUED",
                          error=None, result={}, parameters=parameters)
            op["actions"][record["id"]] = record
            op["action_keys"][request_key] = record["id"]
            self._actions[record["id"]] = record
            op.update(active=True, active_action=record["id"], deadline=self.clock() + self.timeout)
            # At most one start/action per retained operation uses this queue.
            self._work.queue.put_nowait(lambda: self._call(op, record))
            return self._action_snapshot(op, record)

    def _call(self, op, record):
        try:
            self._check(op)
            with self._lock:
                if op["cancelled"].is_set():
                    raise IntegrationError("cancelled", "application operation was cancelled")
                record["state"] = "RUNNING"
            result = object_data(op["adapter"].call(record["action"], record["parameters"]),
                                 max_bytes=48 * 1024)
            self._check(op)
            # Refresh cached status while still serializing adapter hooks. A
            # stuck status hook is an active action and cannot race stop().
            details = object_data(op["adapter"].status(), max_bytes=48 * 1024)
            self._check(op)
            with self._lock:
                if op["cancelled"].is_set():
                    raise IntegrationError("cancelled", "application operation was cancelled")
                record.update(state="SUCCEEDED", result=result)
                op["details"] = details
        except BaseException as exc:
            code = exc.code if isinstance(exc, IntegrationError) else "action_failed"
            with self._lock:
                # A late return cannot erase a timeout or publish success after
                # cancellation. Results stay empty when completion is uncertain.
                if record["state"] not in {"ERROR", "CANCELLED"}:
                    record.update(state="CANCELLED" if code == "cancelled" else "ERROR", error=code)
            self._fail(op, IntegrationError(code, "application action failed"))
        finally:
            with self._lock:
                record.pop("parameters", None)
                op.update(active=False, active_action=None)
                if op["cancelled"].is_set():
                    self._schedule_cleanup(op)

    @staticmethod
    def _action_snapshot(op, record):
        return {"operation_id": op["id"], "action_id": record["id"], "state": record["state"],
                "error": record["error"], "generation": op["binding"].generation,
                "result": object_data(record["result"])}

    def action_status(self, binding, operation_id, action_id):
        with self._lock:
            op = self._owned(binding, operation_id)
            record = op["actions"].get(action_id)
            if record is None:
                raise IntegrationError("unknown_action", "action does not belong to this operation")
            return self._action_snapshot(op, record)

    @staticmethod
    def _cancel_action(op, *, timed_out=False):
        if op["active_action"] is not None:
            record = op["actions"][op["active_action"]]
            if record["state"] in {"QUEUED", "RUNNING"} or (timed_out and record["state"] == "CANCELLED"):
                record.update(state="ERROR" if timed_out else "CANCELLED",
                              error="timeout" if timed_out else "cancelled")

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
            if binding.prepare is not None:
                self._phase(op, "preparing_resources")
                binding.prepare()
                self._check(op)
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
            if code == "cancelled" and op["error"] is not None:
                code = op["error"]
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
            try:
                if op["release_attempted"] and not op["native_released"]:
                    raise IntegrationError("recovery_required", "native release failed; explicit recovery is required")
                if op["adapter"] is not None:
                    op["adapter"].stop()
            finally:
                # A raised adapter error must not skip the trusted supervisor's
                # cleanup attempt. A still-running adapter hook is never raced.
                if binding.finalize is not None:
                    binding.finalize()
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
                "active_action_id": op["active_action"],
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
            self._cancel_action(op)
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
                    self._cancel_action(op, timed_out=timed_out)
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
                for action_id in op["actions"]:
                    self._actions.pop(action_id)
                self._keys.pop(op["key"])
                self._operations.pop(op["id"])

    def operations(self, binding=None):
        """Trusted controller inspection; snapshots never invoke adapter code.

        Without a binding this can span owners and must not be exposed directly
        to a remote caller. Controllers retain the original Binding for the
        retry horizon even if native reacquisition advances its root generation.
        """
        with self._lock:
            return [self._snapshot(op) for op in self._operations.values()
                    if binding is None or self._scope(op["binding"]) == self._scope(binding)]

    def drained(self, binding=None):
        """Whether a scope may be retired without losing work or ownership."""
        with self._lock:
            return all(op["released"] and not op["active"] and not op["cleanup"]
                       for op in self._operations.values()
                       if binding is None or self._scope(op["binding"]) == self._scope(binding))

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
