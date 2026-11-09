"""Fail-closed ownership primitives. No driver, token, or RPC dependencies.

Native admission is short-held bookkeeping, not a lock around RX/TX. An
application lease excludes new native work until verified release. A durable
claim survives process death; restarting never implies that hardware is free.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from threading import RLock
from uuid import uuid4

from .payload import IntegrationError, object_data


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate ownership key")
        result[key] = value
    return result


class Journal:
    """Single-writer, bounded ownership journal on a local durable filesystem.

    Hold the OS lock for the runtime's lifetime. Recovery is explicit and never
    kills a PID from a file. Corrupt/unreadable state prevents startup.
    """

    def __init__(self, directory: Path, *, limit: int = 64):
        import fcntl  # the experimental runtime currently targets Linux/macOS
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._file = (self.directory / "ownership.lock").open("a+b")
        try:
            fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self._file.close()
            raise IntegrationError("journal_busy", "another runtime owns this journal") from None
        self._lock = RLock()
        self._gates = {}
        self.limit = limit
        self.path = self.directory / "ownership.json"
        self._records = {}
        try:
            if self.path.exists():
                with self.path.open("rb") as stream:
                    raw = stream.read(65537)
                if len(raw) > 65536:
                    raise ValueError("journal too large")
                self._records = object_data(json.loads(raw, object_pairs_hook=_unique_object))
                if len(self._records) > limit:
                    raise ValueError("too many owners")
                for key, row in self._records.items():
                    if not key or set(row) != {"lease", "session", "generation", "integration"}:
                        raise ValueError("invalid ownership record")
                    if (any(type(row[name]) is not str or not row[name] for name in ("lease", "session", "integration"))
                            or type(row["generation"]) is not int or row["generation"] < 1):
                        raise ValueError("invalid ownership identity")
        except BaseException:
            self._file.close()
            raise IntegrationError("recovery_required", "ownership journal could not be read safely") from None

    def records(self):
        with self._lock:
            return object_data(self._records)

    def _write(self, records):
        if self._file.closed:
            raise IntegrationError("journal_closed", "ownership journal is closed")
        encoded = json.dumps(object_data(records), sort_keys=True, separators=(",", ":")).encode()
        fd, name = tempfile.mkstemp(prefix="ownership-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def claim(self, physical_id, record):
        with self._lock:
            if physical_id in self._records:
                raise IntegrationError("recovery_required", "hardware has an unresolved owner")
            if len(self._records) >= self.limit:
                raise IntegrationError("overloaded", "ownership journal is full")
            # Retain the in-memory fence even if persistence fails partway.
            self._records[physical_id] = object_data(record)
            self._write(self._records)

    def release(self, physical_id, lease):
        with self._lock:
            if self._records.get(physical_id, {}).get("lease") != lease:
                raise IntegrationError("stale_owner", "ownership generation has changed")
            updated = {key: value for key, value in self._records.items() if key != physical_id}
            self._write(updated)
            self._records = updated

    def close(self):
        with self._lock:
            self._file.close()


class Gate:
    """One *physical* device, shared by all aliases and hardware access routes.

    A native ticket spans dispatch and resource publication. Long-lived native
    resources must also hold tickets or be checked by the native-release hook.
    A journal is mandatory to claim application ownership. With no journal this
    is admission accounting only, and cannot enable application handoffs.
    """

    def __new__(cls, physical_id: str, journal: Journal | None = None):
        if type(physical_id) is not str or not 1 <= len(physical_id) <= 256:
            raise ValueError("physical identity must be a bounded nonempty string")
        if journal is None:
            return super().__new__(cls)
        with journal._lock:
            if physical_id not in journal._gates:
                journal._gates[physical_id] = super().__new__(cls)
            return journal._gates[physical_id]

    def __init__(self, physical_id: str, journal: Journal | None = None):
        with journal._lock if journal else RLock():
            if hasattr(self, "_lock"):
                return
            self.physical_id = physical_id
            self.journal = journal
            self._lock = RLock()
            self._active = 0
            self._maintenance = False
            self._releasing = False
            self._owner = journal.records().get(physical_id) if journal else None

    @contextmanager
    def native(self):
        with self._lock:
            if self._owner is not None or self._maintenance:
                raise IntegrationError("device_owned", "hardware is owned or awaiting recovery")
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1

    @contextmanager
    def maintenance(self):
        with self._lock:
            if self._owner is not None or self._maintenance or self._active:
                raise IntegrationError("device_busy", "hardware is busy; maintenance must wait")
            self._maintenance = True
        try:
            yield
        finally:
            with self._lock:
                self._maintenance = False

    def snapshot(self):
        with self._lock:
            return {"active_native": self._active, "blocked": self._owner is not None or self._maintenance,
                    "owner": dict(self._owner) if self._owner else None}

    def reserve(self, *, session: str, generation: int, integration: str):
        """Fence admission immediately; the worker must persist before side effects."""
        if (type(session) is not str or not session or type(generation) is not int or generation < 1
                or type(integration) is not str or not integration):
            raise IntegrationError("invalid_owner", "invalid application owner identity")
        with self._lock:
            if self.journal is None:
                raise IntegrationError("unsupported", "durable ownership has not been configured")
            if self._owner is not None or self._maintenance:
                raise IntegrationError("device_owned", "hardware is owned or awaiting recovery")
            if self._active:
                raise IntegrationError("device_busy", "native work is still in progress")
            record = object_data({"lease": uuid4().hex, "session": session, "generation": generation,
                                  "integration": integration})
            self._owner = record
            return record["lease"]

    def persist(self, lease):
        with self._lock:
            if not self._owner or self._owner["lease"] != lease:
                raise IntegrationError("stale_owner", "ownership generation has changed")
            record = dict(self._owner)
        self.journal.claim(self.physical_id, record)

    def claim(self, *, session: str, generation: int, integration: str):
        lease = self.reserve(session=session, generation=generation, integration=integration)
        self.persist(lease)
        return lease

    def release(self, lease: str, *, verify):
        # Never run driver/adapter code under the admission lock.
        with self._lock:
            if not self._owner or self._owner["lease"] != lease:
                raise IntegrationError("stale_owner", "ownership generation has changed")
        if verify() is not True:
            raise IntegrationError("recovery_required", "resource release could not be verified")
        with self._lock:
            if not self._owner or self._owner["lease"] != lease:
                raise IntegrationError("stale_owner", "ownership generation has changed")
            if self._releasing:
                raise IntegrationError("device_busy", "ownership release is already in progress")
            self._releasing = True
        try:
            # Disk latency must not block gate snapshots or the shared native
            # cleanup readiness monitor. The owner remains set throughout.
            self.journal.release(self.physical_id, lease)
            with self._lock:
                self._owner = None
        finally:
            with self._lock:
                self._releasing = False
