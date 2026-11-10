"""Bounded process ownership using a delegated Linux cgroup v2 subtree.

This is trusted-code supervision, not a security sandbox. The operator must
prevent applications from migrating themselves out of the delegated subtree.
The proof here covers processes and their descendants, not arbitrary network
interfaces, shared memory, hardware references or files created elsewhere.
Those resource classes require independent qualified cleanup before a radio's
lease may be released. There is deliberately no process-group/PID fallback.

Kernel semantics: https://docs.kernel.org/admin-guide/cgroup-v2.html
"""
from __future__ import annotations

from contextlib import ExitStack

import ctypes
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import tempfile
from threading import Event, Lock, RLock, Thread
import time
from uuid import uuid4

from .ownership import _unique_object
from .payload import IntegrationError, object_data


_PREFIX = "rrf-"
_OWNER = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SCOPE = re.compile(r"rrf-[0-9a-f]{32}")
_MAX_STATE = 65536


def _unsupported():
    return IntegrationError("supervision_unavailable", "delegated Linux cgroup v2 supervision is unavailable")


def _recovery():
    return IntegrationError("recovery_required", "process ownership could not be verified safely")


def _require_linux():
    if sys.platform != "linux":
        raise _unsupported()


def _assert_cgroup2(fd):
    # Check the actual filesystem of the pinned descriptor, not a pathname or
    # a caller-created directory containing files named cgroup.*.
    value = ctypes.create_string_buffer(256)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.fstatfs(ctypes.c_int(fd), ctypes.byref(value)) != 0:
        raise _unsupported()
    if ctypes.c_long.from_buffer(value).value != 0x63677270:
        raise _unsupported()


def _boot_id():
    value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not re.fullmatch(r"[0-9a-f-]{36}", value):
        raise _recovery()
    return value


def _identity(fd):
    value = os.fstat(fd)
    return [value.st_dev, value.st_ino]


def _open_dir(path, *, dir_fd=None):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=dir_fd)


def _read_control(fd, name):
    control = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
    try:
        data = os.read(control, 4097)
        if len(data) > 4096:
            raise _recovery()
        return data.decode("ascii").strip()
    finally:
        os.close(control)


def _write_control(fd, name, value):
    control = os.open(name, os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
    try:
        data = value.encode("ascii")
        if os.write(control, data) != len(data):
            raise OSError("short cgroup control write")
    finally:
        os.close(control)


def _populated(fd):
    rows = {}
    for line in _read_control(fd, "cgroup.events").splitlines():
        key, value = line.split()
        if key in rows:
            raise _recovery()
        rows[key] = value
    if rows.get("populated") not in {"0", "1"}:
        raise _recovery()
    return rows["populated"] == "1"


def _wait_readable(fd, timeout):
    # select.select has a low FD ceiling even when the server has a much larger
    # descriptor limit; epoll/kqueue remain valid on busy deployments.
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        return bool(selector.select(timeout))


class _LogDrainer:
    """One daemon for all pipes; always drain after the bounded tail fills."""
    def __init__(self, limit):
        self.limit = limit
        self.lock = Lock()
        self.streams = {}
        self.selector = selectors.DefaultSelector()
        self.exit = Event()
        self.thread = Thread(target=self._run, name="remoterf-process-logs", daemon=True)
        self.thread.start()

    def add(self, process):
        with self.lock:
            for name in ("stdout", "stderr"):
                pipe = getattr(process["popen"], name)
                os.set_blocking(pipe.fileno(), False)
                self.selector.register(pipe.fileno(), selectors.EVENT_READ)
                self.streams[pipe.fileno()] = (pipe, process, name)

    def _run(self):
        while not self.exit.wait(0.01):
            with self.lock:
                if self.exit.is_set():
                    return
                ready = self.selector.select(timeout=0)
            for key, _events in ready:
                fd = key.fd
                with self.lock:
                    row = self.streams.get(fd)
                    if row is None:
                        continue
                    pipe, process, name = row
                    try:
                        data = os.read(fd, 8192)
                    except BlockingIOError:
                        continue
                    except OSError:
                        process["log_error"] = True
                        data = b""
                    if not data:
                        self.streams.pop(fd, None)
                        self.selector.unregister(fd)
                        pipe.close()
                        process["open_logs"] -= 1
                        continue
                    process[name + "_bytes"] += len(data)
                    tail = process[name]
                    tail.extend(data)
                    if len(tail) > self.limit:
                        del tail[:-self.limit]

    def close(self):
        self.exit.set()
        with self.lock:
            for pipe, process, _name in self.streams.values():
                pipe.close()
                process["open_logs"] -= 1
                process["log_error"] = True
            self.streams.clear()
            self.selector.close()

    def closed(self, process):
        with self.lock:
            return process["open_logs"] == 0 and not process["log_error"]

    def describe(self, process):
        with self.lock:
            return {"id": process["id"], "returncode": process["popen"].poll(),
                    "stdout_bytes": process["stdout_bytes"], "stderr_bytes": process["stderr_bytes"],
                    "stdout_truncated": process["stdout_bytes"] > self.limit,
                    "stderr_truncated": process["stderr_bytes"] > self.limit,
                    "logs_drained": process["open_logs"] == 0, "log_error": process["log_error"]}

    def tail(self, process, stream):
        with self.lock:
            return bytes(process[stream])


class CgroupSupervisor:
    """Single-writer owner registry. Construction launches no application.

    delegated_root must be an exclusive operator-created cgroup v2 domain with
    pids and memory controllers delegated. state_dir is durable local storage.
    Scope identity includes this boot, pinned root and scope inode identities.
    Unknown cgroups or incomplete records require operator reconciliation.
    """
    def __init__(self, delegated_root, state_dir, *, max_scopes=64, max_processes=4,
                 max_log_bytes=65536, max_pids=128, memory_bytes=512 * 1024 * 1024):
        if (type(max_scopes) is not int or not 1 <= max_scopes <= 64
                or type(max_processes) is not int or not 1 <= max_processes <= 16
                or type(max_log_bytes) is not int or not 1024 <= max_log_bytes <= 65536
                or type(max_pids) is not int or not 1 <= max_pids <= 4096
                or type(memory_bytes) is not int or not 16 * 1024 * 1024 <= memory_bytes <= 64 * 1024**3):
            raise ValueError("invalid supervision bounds")
        _require_linux()
        self.root = Path(delegated_root).absolute()
        self.state_dir = Path(state_dir).absolute()
        self.max_scopes, self.max_processes = max_scopes, max_processes
        self.max_pids, self.memory_bytes = max_pids, memory_bytes
        self._lock = RLock()
        self._scopes = {}
        self._closed = False
        self._broken = False
        self._draining = False
        try:
            self._root_fd = _open_dir(self.root)
        except OSError:
            raise _unsupported() from None
        self._file = None
        self._logs = None
        try:
            _assert_cgroup2(self._root_fd)
            if _read_control(self._root_fd, "cgroup.type") != "domain":
                raise _unsupported()
            self._boot = _boot_id()
            self._root_identity = _identity(self._root_fd)
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            import fcntl
            self._file = (self.state_dir / "supervisor.lock").open("a+b")
            try:
                fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise IntegrationError("journal_busy", "another runtime owns this process registry") from None
            self._path = self.state_dir / "scopes.json"
            self._records = self._load()
            known = {row["name"] for row in self._records.values()}
            if any(name.startswith(_PREFIX) and name not in known for name in os.listdir(self._root_fd)):
                raise _recovery()
            self._logs = _LogDrainer(max_log_bytes)
        except BaseException:
            if self._file is not None:
                self._file.close()
            os.close(self._root_fd)
            raise

    def _load(self):
        if not self._path.exists():
            return {}
        try:
            with self._path.open("rb") as stream:
                raw = stream.read(_MAX_STATE + 1)
            if len(raw) > _MAX_STATE:
                raise ValueError()
            state = object_data(json.loads(raw, object_pairs_hook=_unique_object))
            if set(state) != {"boot", "root", "scopes"}:
                raise ValueError()
            records = state["scopes"]
            if type(records) is not dict or len(records) > self.max_scopes:
                raise ValueError()
            if (type(state["boot"]) is not str or not re.fullmatch(r"[0-9a-f-]{36}", state["boot"])
                    or type(state["root"]) is not list or len(state["root"]) != 2
                    or any(type(value) is not int or value < 0 for value in state["root"])):
                raise ValueError()
            if records and (state["boot"] != self._boot or state["root"] != self._root_identity):
                raise ValueError()
            names = set()
            for owner, row in records.items():
                if not _OWNER.fullmatch(owner) or set(row) != {"name", "identity", "state"}:
                    raise ValueError()
                if not _SCOPE.fullmatch(row["name"]) or row["name"] in names:
                    raise ValueError()
                names.add(row["name"])
                identity = row["identity"]
                if (row["state"] not in {"creating", "ready", "released"}
                        or (row["state"] == "creating" and identity is not None)
                        or (row["state"] != "creating" and
                            (type(identity) is not list or len(identity) != 2 or
                             any(type(value) is not int or value < 0 for value in identity)))):
                    raise ValueError()
            return records
        except BaseException:
            raise _recovery() from None

    def _write(self):
        if self._closed or self._file.closed or self._broken:
            raise _recovery()
        data = json.dumps(object_data({"boot": self._boot, "root": self._root_identity,
                                       "scopes": self._records}), sort_keys=True).encode()
        name = None
        try:
            fd, name = tempfile.mkstemp(prefix="scopes-", dir=self.state_dir)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self._path)
            directory = _open_dir(self.state_dir)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            self._broken = True
            raise
        finally:
            if name is not None and os.path.exists(name):
                os.unlink(name)

    def describe(self):
        """Cached local-operator records; no process status or filesystem waits."""
        with self._lock:
            return object_data({"owners": self._records, "closed": self._closed, "recovery_required": self._broken})

    def _create_cgroup(self, name):
        os.mkdir(name, mode=0o700, dir_fd=self._root_fd)

    def create(self, owner_id):
        if type(owner_id) is not str or not _OWNER.fullmatch(owner_id):
            raise IntegrationError("invalid_owner", "invalid process owner identity")
        with self._lock:
            if self._closed or self._broken or self._draining:
                raise _recovery()
            if owner_id in self._records:
                return self.reopen(owner_id)
            if len(self._records) >= self.max_scopes:
                raise IntegrationError("overloaded", "process ownership registry is full")
            name = _PREFIX + uuid4().hex
            row = self._records[owner_id] = {"name": name, "identity": None, "state": "creating"}
            self._write()  # before mkdir, and necessarily before any application process
            try:
                self._create_cgroup(name)
                fd = _open_dir(name, dir_fd=self._root_fd)
                try:
                    _assert_cgroup2(fd)
                    if _read_control(fd, "cgroup.type") != "domain" or _populated(fd):
                        raise _recovery()
                    # Fail rather than silently omit fork/memory containment.
                    _write_control(fd, "pids.max", str(self.max_pids))
                    _write_control(fd, "memory.max", str(self.memory_bytes))
                    # All launched descendants inherit this single domain.
                    # Unbounded empty sub-cgroups are not part of this API.
                    _write_control(fd, "cgroup.max.descendants", "0")
                    control = os.open("cgroup.kill", os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(control)
                    row.update(identity=_identity(fd), state="ready")
                    self._write()
                    scope = Scope(self, owner_id, fd)
                    self._scopes[owner_id] = scope
                    return scope
                except BaseException:
                    os.close(fd)
                    raise
            except (OSError, ValueError):
                raise _unsupported() from None

    def reopen(self, owner_id):
        with self._lock:
            if self._closed or self._broken or self._draining or owner_id not in self._records:
                raise _recovery()
            if owner_id in self._scopes:
                return self._scopes[owner_id]
            row = self._records[owner_id]
            if row["state"] not in {"ready", "released"}:
                raise _recovery()
            try:
                fd = _open_dir(row["name"], dir_fd=self._root_fd)
            except OSError:
                raise _recovery() from None
            if _identity(fd) != row["identity"]:
                os.close(fd)
                raise _recovery()
            scope = Scope(self, owner_id, fd, recovered=True)
            self._scopes[owner_id] = scope
            return scope

    def reconcile_released(self, owner_id):
        """Offline operator repair of an already-released, removed scope.

        This never kills anything and never handles an unknown/creating owner.
        It repairs only the crash window after verified release/rmdir and before
        the final metadata removal. Matching boot/root metadata is mandatory.
        """
        with self._lock:
            if self._closed or self._broken or self._draining or owner_id in self._scopes:
                raise _recovery()
            row = self._records.get(owner_id)
            if row is None or row["state"] != "released":
                raise _recovery()
            try:
                os.stat(row["name"], dir_fd=self._root_fd, follow_symlinks=False)
            except FileNotFoundError:
                del self._records[owner_id]
                self._write()
                return True
            return False

    def disconnect(self):
        """Offline inspectors release handles/locks without claiming release.

        Recovered owners may still run in their cgroups. Their records remain
        intact. A supervisor with locally launched children cannot disconnect;
        its regular lifecycle must drain/close those scopes first.
        """
        with self._lock:
            if self._closed:
                return
            self._draining = True
            scopes = sorted(self._scopes.values(), key=lambda scope: scope.owner_id)
        # Scope -> supervisor lock order matches Scope.close(). No native work
        # or persistent write occurs here, and admission is already closed.
        with ExitStack() as stack:
            for scope in scopes:
                stack.enter_context(scope._lock)
            with self._lock:
                if any(scope._launching or scope._processes for scope in scopes):
                    self._draining = False
                    raise _recovery()
                self._closed = True
                for scope in scopes:
                    scope._accepting = False
                    if not scope._closed:
                        try:
                            os.close(scope._fd)
                        except OSError:
                            pass
                        scope._detached = True
                try:
                    self._logs.close()
                finally:
                    try:
                        self._file.close()
                    finally:
                        os.close(self._root_fd)

    def close(self):
        """Close only after every owned scope has been explicitly released."""
        with self._lock:
            if self._closed:
                return
            if self._records:
                raise _recovery()
            self._closed = True
            self._logs.close()
            self._file.close()
            os.close(self._root_fd)


class Scope:
    """Bound process service exposed to one trusted adapter's Context.

    launch accepts an absolute executable and explicit environment only. Cwd
    must be an existing absolute directory provisioned by the trusted runtime.
    Launch returns an opaque process ID; useful readiness is adapter-specific.
    Recovered scopes cannot launch again: inspect/stop/reconcile them first.
    """
    def __init__(self, supervisor, owner_id, fd, *, recovered=False):
        self._supervisor, self.owner_id, self._fd = supervisor, owner_id, fd
        self._lock = RLock()
        self._processes = {}
        self._launching = 0
        self._accepting = not recovered
        self._closed = False
        self._failure = None
        self._detached = False

    def _verify_identity(self):
        row = self._supervisor._records.get(self.owner_id)
        if (self._closed or self._supervisor._closed or self._supervisor._broken or self._supervisor._draining
                or row is None or row["identity"] != _identity(self._fd)):
            raise _recovery()
        # Never follow a renamed/replaced scope to a new owner's processes.
        current = os.stat(row["name"], dir_fd=self._supervisor._root_fd, follow_symlinks=False)
        if [current.st_dev, current.st_ino] != row["identity"]:
            raise _recovery()

    def launch(self, argv, *, env, cwd, timeout=3.0):
        if (type(argv) not in (list, tuple) or not 1 <= len(argv) <= 128
                or any(type(arg) is not str or "\0" in arg or len(arg) > 4096 for arg in argv)
                or not Path(argv[0]).is_absolute() or sum(len(arg) for arg in argv) > 32768
                or type(env) is not dict or len(env) > 64
                or any(type(key) is not str or type(value) is not str or not key or "=" in key
                       or "\0" in key + value or len(key) + len(value) > 4096 for key, value in env.items())
                or sum(len(key) + len(value) for key, value in env.items()) > 32768
                or type(cwd) not in (str, Path) and not isinstance(cwd, Path)
                or not Path(cwd).is_absolute() or type(timeout) not in (int, float) or not 0 < timeout <= 30):
            raise IntegrationError("invalid_process", "invalid bounded process launch specification")
        with self._lock:
            self._verify_identity()
            if not self._accepting:
                raise IntegrationError("cancelled", "process owner is draining or recovered")
            if len(self._processes) + self._launching >= self._supervisor.max_processes:
                raise IntegrationError("overloaded", "process owner has reached its launch limit")
            self._launching += 1
        ready_read = ready_write = grant_read = grant_write = None
        try:
            ready_read, ready_write = os.pipe()
            grant_read, grant_write = os.pipe()
            command = [sys.executable, "-I", "-S", str(Path(__file__).with_name("_process_worker.py")),
                       str(self._fd), str(ready_write), str(grant_read)]
            environment = json.dumps(env, ensure_ascii=False).encode("utf-8")
            if len(environment) > 65536:
                raise IntegrationError("invalid_process", "process environment exceeds its encoded budget")
            # Adapter environment must not affect the bootstrap loader (e.g.
            # LD_PRELOAD before cgroup admission). An unlinked descriptor carries
            # it privately; only the admitted worker uses it for the target exec.
            with tempfile.TemporaryFile() as env_file:
                env_file.write(environment)
                env_file.flush()
                env_file.seek(0)
                process = subprocess.Popen([*command, str(env_file.fileno()), *argv],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    close_fds=True, pass_fds=(self._fd, ready_write, grant_read, env_file.fileno()),
                    env={}, cwd=cwd, start_new_session=True)
            record = {"id": uuid4().hex, "popen": process, "stdout": bytearray(), "stderr": bytearray(),
                      "stdout_bytes": 0, "stderr_bytes": 0, "open_logs": 2, "log_error": False}
            with self._lock:
                self._processes[record["id"]] = record
            self._supervisor._logs.add(record)
            os.close(ready_write)
            ready_write = None
            os.close(grant_read)
            grant_read = None
            if not _wait_readable(ready_read, timeout) or os.read(ready_read, 1) != b"R":
                raise IntegrationError("process_launch_failed", "process failed to enter its owned scope")
            with self._lock:
                self._verify_identity()
                if not self._accepting:
                    raise IntegrationError("cancelled", "process owner is draining")
                os.write(grant_write, b"G")
            return record["id"]
        except BaseException:
            self._failure = "process_launch_failed"
            raise
        finally:
            for fd in (ready_read, ready_write, grant_read, grant_write):
                if fd is not None:
                    os.close(fd)
            with self._lock:
                self._launching -= 1

    def snapshot(self):
        with self._lock:
            return {"owner_id": self.owner_id, "accepting": self._accepting, "launching": self._launching,
                    "closed": self._closed, "failure": self._failure,
                    "processes": [self._supervisor._logs.describe(row) for row in self._processes.values()]}

    def log_tail(self, process_id, stream):
        """Local trusted reader only; raw logs must not be exposed by remote status."""
        with self._lock:
            if process_id not in self._processes or stream not in {"stdout", "stderr"}:
                raise IntegrationError("unknown_process", "process does not belong to this owner")
            return self._supervisor._logs.tail(self._processes[process_id], stream)

    def resources_released(self):
        # Verification permanently seals launch admission. A late adapter
        # thread cannot recreate processes after a successful release proof.
        with self._lock:
            self._accepting = False
            if self._closed:
                return True
            if self._launching:
                return False
            try:
                self._verify_identity()
                if _populated(self._fd):
                    return False
                return all(row["popen"].poll() is not None and self._supervisor._logs.closed(row)
                           for row in self._processes.values())
            except (OSError, ValueError, IntegrationError):
                self._failure = "recovery_required"
                return False

    def stop(self, timeout=5.0):
        if type(timeout) not in (int, float) or not 0 <= timeout <= 30:
            raise ValueError("invalid process cleanup timeout")
        with self._lock:
            self._accepting = False
            if self._closed:
                return True
            try:
                self._verify_identity()
                _write_control(self._fd, "cgroup.kill", "1")
            except (OSError, ValueError, IntegrationError):
                self._failure = "recovery_required"
                return False
        deadline = time.monotonic() + timeout
        while True:
            if self.resources_released():
                return True
            if time.monotonic() >= deadline:
                self._failure = "cleanup_timeout"
                return False
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))

    def close(self):
        with self._lock, self._supervisor._lock:
            if self._closed:
                return
            if not self.resources_released():
                raise _recovery()
            row = self._supervisor._records[self.owner_id]
            row["state"] = "released"
            self._supervisor._write()
            os.rmdir(row["name"], dir_fd=self._supervisor._root_fd)
            del self._supervisor._records[self.owner_id]
            self._supervisor._write()
            os.close(self._fd)
            self._supervisor._scopes.pop(self.owner_id, None)
            self._closed = True
