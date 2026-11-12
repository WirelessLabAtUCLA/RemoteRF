"""Operation artifacts: flat regular files in ``work_dir/artifacts``.

Adapters publish each file under a final name (write a temporary name, then
os.replace). Callers read them in bounded chunks only while the operation is
running; the runtime discards the directory before the hardware lease is
released. This confines adapter-supplied names, not a same-uid process.
Errors never carry filesystem paths back to a remote caller.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys

from .payload import IntegrationError
from .supervision import _open_dir


ARTIFACTS = "artifacts"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MAX_FETCH_BYTES = 384 * 1024
MAX_ENTRIES = 256
# ponytail: detection not enforcement; per-op tmpfs/project quota if disk exhaustion is seen
QUOTA_BYTES = 256 * 1024 * 1024


def validate(name, offset, limit):
    if type(name) is not str or not NAME.fullmatch(name):
        raise IntegrationError("invalid_request", "artifact must be a plain artifact name")
    if type(offset) is not int or not 0 <= offset < 2**63 or type(limit) is not int or not 1 <= limit <= MAX_FETCH_BYTES:
        raise IntegrationError("invalid_request", "invalid artifact offset or limit")


def _regular(st):
    # A hard link could expose another same-uid file under an artifact name.
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def read_chunk(work_dir, name, offset, limit):
    """Return (data, size) for one chunk of a published artifact."""
    validate(name, offset, limit)
    try:
        directory = _open_dir(Path(work_dir) / ARTIFACTS)
        try:
            # O_NONBLOCK: a planted FIFO must not hang the RPC thread.
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        finally:
            os.close(directory)
        try:
            st = os.fstat(fd)
            if not _regular(st):
                raise IntegrationError("unknown_artifact", "artifact is not available")
            return os.pread(fd, limit, offset), st.st_size
        finally:
            os.close(fd)
    except (OSError, OverflowError):
        raise IntegrationError("unknown_artifact", "artifact is not available") from None


def usage(work_dir):
    """Fail with artifact_limit when the directory exceeds its bounds."""
    try:
        try:
            directory = _open_dir(Path(work_dir) / ARTIFACTS)
        except FileNotFoundError:
            return
        try:
            total = 0
            with os.scandir(directory) as entries:
                for count, entry in enumerate(entries, 1):
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue  # renamed away by an adapter publishing via os.replace
                    total += st.st_size
                    if count > MAX_ENTRIES or not _regular(st) or total > QUOTA_BYTES:
                        raise IntegrationError("artifact_limit", "application artifacts exceed their bounds")
        finally:
            os.close(directory)
    except OSError:
        raise IntegrationError("artifact_limit", "application artifacts could not be measured") from None


def _retry(func, path, _exc):
    # Best effort: an adapter may leave read-only or unreadable directories.
    try:
        os.chmod(os.path.dirname(path), 0o700)
        if stat.S_ISDIR(os.lstat(path).st_mode):
            os.chmod(path, 0o700)
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.unlink(path)
    except OSError:
        pass


def discard(work_dir):
    """Take artifacts off the fetch path atomically, then delete them.

    Only a failed rename raises: removing the quarantined copy is best effort
    and leaves any remainder in work_dir for operator review.
    """
    work_dir = Path(work_dir)
    quarantine = work_dir / (".discarded-" + secrets.token_hex(8))
    try:
        os.rename(work_dir / ARTIFACTS, quarantine)
    except FileNotFoundError:
        return
    except OSError:
        raise IntegrationError("recovery_required", "application artifacts could not be discarded") from None
    handler = "onexc" if sys.version_info >= (3, 12) else "onerror"
    try:
        shutil.rmtree(quarantine, **{handler: _retry})
    except OSError:
        pass
