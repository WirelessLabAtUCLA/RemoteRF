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

"""The stable Python reservation API, re-exported as `remoterf.<name>`.
It only adapts arguments; `ops` (internal) does the work. Plain dicts out,
RemoteRFOpsError on failure; nothing prints or prompts."""

import os
import threading
from datetime import datetime, timezone

from . import ops
from .ops import RemoteRFOpsError

_session = None
_lock = threading.Lock()
_default = None


def _call(fn, *args, **kwargs):
    """fn(session, ...) with the saved login, loaded once; reloaded and
    retried once when the deployment says it expired. Safe across threads."""
    global _session
    with _lock:
        if _session is None:
            _session = ops.saved_session()
        s = _session
    try:
        return fn(s, *args, **kwargs)
    except RemoteRFOpsError as exc:
        if exc.code != "session_expired":
            raise
    with _lock:
        if _session is s:  # another thread may have reloaded already
            _session = ops.saved_session()
        s = _session
    return fn(s, *args, **kwargs)


def use(deployment):
    """Set the deployment a handle with no `@lab` means, for this process; None clears it."""
    global _default
    _default = str(deployment or "").strip().lower() or None


def _lab(s):
    """use(), then REMOTERF_DEPLOYMENT, then the lab this login is at."""
    return (_default or os.environ.get("REMOTERF_DEPLOYMENT", "").strip().lower()
            or (s.home if s.home_kind == "deployment" else None))


def _handle(s, handle):
    handle = str(handle or "").strip()
    if "@" in handle or not handle:
        return handle
    lab = _lab(s)
    if not lab:
        raise RemoteRFOpsError("invalid_argument", "A handle is Name@deployment, for example Pluto_00@ucla. "
                                                   'Or set a default deployment: remoterf.use("ucla").')
    return f"{handle}@{lab}"


def whoami() -> dict:
    return _call(ops.whoami)


def list_devices(deployment=None, online_only=True, type=None) -> dict:
    """Like remoterf.devices(), as a dict and without printing."""
    return _call(ops.list_devices, deployment=deployment, online_only=online_only, type=type)


def slots(handle, start_day=None, days=1, tz=None) -> dict:
    return _call(lambda s: ops.find_slots(s, _handle(s, handle), start_day=start_day, days=days, tz=tz))


def reservations(mine_only=True) -> dict:
    return _call(ops.list_reservations, mine_only=mine_only)


def _check_window(s, handle, start_ts, end_ts):
    """Refuse a window that isn't consecutive free blocks of the device's grid, before asking the lab."""
    first, last = datetime.fromtimestamp(start_ts), datetime.fromtimestamp(end_ts - 1)
    out = ops.find_slots(s, handle, start_day=first.date(), days=(last.date() - first.date()).days + 1)
    block = out["slot_minutes"] * 60
    free = {slot["start_ts"]: slot["end_ts"] for slot in out["slots"]}
    after = [slot["start"] for slot in out["slots"] if slot["start_ts"] >= start_ts]
    nxt = f"the next free one starts {after[0]}" if after else "none is free later on those days"
    minute = first.hour * 60 + first.minute
    if first.second or minute % (block // 60) or (end_ts - start_ts) % block:
        raise RemoteRFOpsError("invalid_argument", f"{out['handle']} is reserved in {block // 60}-minute slots "
                                                   f"from midnight; {nxt}.")
    ts = start_ts
    while ts < end_ts:
        if ts not in free:
            raise RemoteRFOpsError("slot_unavailable", f"The {block // 60}-minute slot at "
                                   f"{ops._instant('t', ts)['t']} on {out['handle']} isn't free; {nxt}.")
        ts = free[ts]


def reserve(handle, start, end=None, *, minutes=None, reveal_token=False) -> dict:
    """Reserve `handle` from `start` (datetime or RFC 3339; naive is local time)
    to `end`, or for `minutes`: one or more consecutive free slots."""
    if (end is None) == (minutes is None):
        raise RemoteRFOpsError("invalid_argument", "Give exactly one of end or minutes.")
    start_ts = ops._epoch(start)
    if minutes is not None:
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            raise RemoteRFOpsError("invalid_argument", "minutes must be a positive whole number.")
        end_ts = start_ts + minutes * 60
    else:
        end_ts = ops._epoch(end)
    if end_ts <= start_ts:
        raise RemoteRFOpsError("invalid_argument", "end must be after start.")

    def book(s):
        h = _handle(s, handle)
        _check_window(s, h, start_ts, end_ts)
        return ops.reserve(s, h, datetime.fromtimestamp(start_ts, timezone.utc),
                           datetime.fromtimestamp(end_ts, timezone.utc), reveal_token=reveal_token)

    return _call(book)


def reserve_now(handle) -> dict:
    """Reserve the free slot running now."""
    return _call(lambda s: ops.auto_reserve(s, _handle(s, handle)))


def cancel(reservation) -> dict:
    """Cancel a reservation: a dict from reserve() or reservations(), or its id ('10@ucla')."""
    rid = reservation.get("reservation_id") if isinstance(reservation, dict) else reservation
    if not rid:
        raise RemoteRFOpsError("invalid_argument", "No reservation_id: find it with remoterf.reservations().")

    def drop(s):
        num, at, lab = str(rid).rpartition("@")
        # A lab login knows its own ids without the '@lab' Global adds.
        return ops.cancel(s, num if at and s.home_kind == "deployment" and lab.lower() == s.home else rid)

    return _call(drop)
