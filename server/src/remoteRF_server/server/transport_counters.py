# Copyright (C) 2026 RemoteRF
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ..common.utils.db_location import get_db_dir

_MAX_INTEGER = 2**53
_PATHS = {"direct", "relay", "lan"}
_TYPE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_FN_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")

logger = logging.getLogger("remoteRF_server.transport")


def create_table(db) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS TransportDays ("
        "day TEXT NOT NULL, path TEXT NOT NULL, type TEXT NOT NULL, fn TEXT NOT NULL, "
        "calls INTEGER NOT NULL, bytes_in INTEGER NOT NULL, bytes_out INTEGER NOT NULL, "
        "PRIMARY KEY (day, path, type, fn))"
    )


def rpc_identity(function_name: str) -> tuple[str, str] | None:
    parts = str(function_name or "").split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    device_type = re.sub(r"[^a-z0-9_]", "_", parts[0].lower())[:64]
    fn = str(function_name)
    if not _TYPE_RE.fullmatch(device_type) or not _FN_RE.fullmatch(fn):
        return None
    return device_type, fn


class TransportCounters:
    def __init__(
        self,
        database: Path | Callable[[], Path] | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._database = database or (lambda: get_db_dir() / "reservations.db")
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str, str, str], list[int]] = {}
        self._last_flush = monotonic()

    def record(
        self,
        path: str,
        function_name: str,
        bytes_in: int,
        bytes_out: int,
    ) -> None:
        try:
            identity = rpc_identity(function_name)
            if path not in _PATHS or identity is None:
                return
            day = self._utcnow().astimezone(timezone.utc).date().isoformat()
            key = (day, path, *identity)
            with self._lock:
                totals = self._pending.setdefault(key, [0, 0, 0])
                totals[0] = min(totals[0] + 1, _MAX_INTEGER)
                totals[1] = min(totals[1] + max(0, int(bytes_in)), _MAX_INTEGER)
                totals[2] = min(totals[2] + max(0, int(bytes_out)), _MAX_INTEGER)
            self.flush()
        except Exception as exc:  # noqa: BLE001 - accounting cannot affect a device call
            logger.warning("transport count failed: %s", type(exc).__name__)

    def record_protobuf(self, path: str, function_name: str, request, response) -> None:
        try:
            self.record(
                path,
                function_name,
                request.ByteSize(),
                response.ByteSize() if response is not None else 0,
            )
        except Exception as exc:  # noqa: BLE001 - accounting cannot affect a device call
            logger.warning("transport count failed: %s", type(exc).__name__)

    def flush(self, *, force: bool = False) -> None:
        try:
            now = self._monotonic()
            with self._lock:
                if not self._pending or (not force and now - self._last_flush < 60.0):
                    return
                pending = self._pending
                self._pending = {}
                self._last_flush = now
            rows = [(*key, *totals) for key, totals in pending.items()]
            try:
                database = self._database() if callable(self._database) else self._database
                with sqlite3.connect(database, timeout=0.1) as db:
                    db.executemany(
                        "INSERT INTO TransportDays "
                        "(day, path, type, fn, calls, bytes_in, bytes_out) VALUES (?,?,?,?,?,?,?) "
                        "ON CONFLICT (day, path, type, fn) DO UPDATE SET "
                        "calls=MIN(calls + excluded.calls, ?), "
                        "bytes_in=MIN(bytes_in + excluded.bytes_in, ?), "
                        "bytes_out=MIN(bytes_out + excluded.bytes_out, ?)",
                        [(*row, _MAX_INTEGER, _MAX_INTEGER, _MAX_INTEGER) for row in rows],
                    )
            except Exception:
                with self._lock:
                    for key, totals in pending.items():
                        current = self._pending.setdefault(key, [0, 0, 0])
                        for index, value in enumerate(totals):
                            current[index] = min(current[index] + value, _MAX_INTEGER)
                raise
        except Exception as exc:  # noqa: BLE001 - accounting cannot affect a device call
            logger.warning("transport flush failed: %s", type(exc).__name__)


transport_counters = TransportCounters()
