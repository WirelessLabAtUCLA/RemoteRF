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

"""Safe restart and device-reload gating against the reservation database.

Waits until no reservation is live now and none starts within the lookahead
window (default 15 minutes), re-checking every 30 s and printing one status
line per minute. Reads the same Reservations table in reservations.db that
`serverrf reservations list` uses, without ever reading or printing usernames.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence, TextIO

DEFAULT_WITHIN_MINUTES = 15
DEFAULT_MAX_WAIT_MINUTES = 360
POLL_INTERVAL_SECONDS = 30.0
STATUS_INTERVAL_SECONDS = 60.0
BUSY_EXIT_CODE = 75

SAFE_GUIDANCE = (
    "For upgrades or maintenance, use `restart --safe` (or `reload devices --safe`) "
    "to wait until no reservation is live or starting soon. A plain `restart` disrupts "
    "users mid-session (in-flight calls, Dynamic v2 sessions, and direct paths drop, "
    "and local devices reopen or power-cycle), while reservations survive in the database."
)


@dataclass(frozen=True)
class ReservationWindow:
    device_id: int
    start: datetime
    end: datetime


@dataclass(frozen=True)
class SafeOptions:
    safe: bool = False
    check: bool = False
    within_minutes: int = DEFAULT_WITHIN_MINUTES
    max_wait_minutes: int = DEFAULT_MAX_WAIT_MINUTES
    help: bool = False


def parse_args(
    argv: Iterable[str],
    *,
    action: str = "restart",
) -> tuple[SafeOptions | None, str | None]:
    tokens = list(argv)
    if any(tok.lower() in ("-h", "--help") for tok in tokens):
        return SafeOptions(help=True), None

    safe = False
    check = False
    within_minutes = DEFAULT_WITHIN_MINUTES
    max_wait_minutes = DEFAULT_MAX_WAIT_MINUTES
    within_set = False
    max_wait_set = False

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-"):
            return None, f"unexpected argument {tok!r} for `{action}`"
        if not tok.startswith("--"):
            return None, f"unknown option {tok!r} for `{action}`"
        name, has_eq, inline = tok[2:].partition("=")
        name_l = name.lower()
        if name_l in ("safe", "check"):
            if has_eq:
                return None, f"--{name_l} does not take a value"
            if name_l == "safe":
                safe = True
            else:
                check = True
            i += 1
            continue
        if name_l in ("within", "max-wait"):
            if has_eq:
                raw = inline
                i += 1
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                raw = tokens[i + 1]
                i += 2
            else:
                return None, f"--{name_l} requires a value in minutes"
            try:
                value = int(raw.strip())
            except ValueError:
                return None, f"--{name_l} must be a whole number of minutes"
            if value < 0:
                return None, f"--{name_l} must be >= 0"
            if name_l == "within":
                within_minutes = value
                within_set = True
            else:
                max_wait_minutes = value
                max_wait_set = True
            continue
        return None, f"unknown option --{name} for `{action}`"

    if (within_set or max_wait_set) and not (safe or check):
        return None, "--within and --max-wait require --safe or --check"

    return SafeOptions(
        safe=safe,
        check=check,
        within_minutes=within_minutes,
        max_wait_minutes=max_wait_minutes,
    ), None


def usage_text(action: str = "restart") -> str:
    return (
        f"Usage: {action} [--safe [--within MINUTES] [--max-wait MINUTES] | --check [--within MINUTES]]\n"
        f"\n"
        f"  --safe              wait until no reservation is live or starting within --within minutes\n"
        f"  --within MINUTES    quiet lookahead window in minutes (default: {DEFAULT_WITHIN_MINUTES})\n"
        f"  --max-wait MINUTES  stop waiting without acting after this many minutes (default: {DEFAULT_MAX_WAIT_MINUTES})\n"
        f"  --check             print whether it is safe now, blocking reservations, and next idle window (exit 0/1)\n"
        f"\n"
        f"{SAFE_GUIDANCE}"
    )


def parse_reservation_datetime(value: object) -> datetime:
    """Parse stored SQLite timestamps to naive server-local datetime, matching reservation.py."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid reservation timestamp: {value!r}") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def default_db_path() -> Path:
    from ..common.utils.db_location import get_db_dir

    return get_db_dir() / "reservations.db"


def read_reservations(db_path: Path | None = None) -> list[ReservationWindow]:
    """Read all reservation windows from the lab's reservations.db (read-only).

    Uses the same Reservations table as `serverrf reservations list` (covering
    local devices, host devices, and federated users' reservations). Usernames
    are not selected.
    """
    path = Path(db_path) if db_path is not None else default_db_path()
    if not path.exists():
        return []
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
            rows = conn.execute(
                "SELECT device_id, start_time, end_time FROM Reservations"
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise RuntimeError(f"could not read {path}: {exc}") from exc
    except sqlite3.Error as exc:
        raise RuntimeError(f"could not read {path}: {exc}") from exc

    windows: list[ReservationWindow] = []
    for device_id, start_raw, end_raw in rows:
        try:
            did = int(device_id)
            start = parse_reservation_datetime(start_raw)
            end = parse_reservation_datetime(end_raw)
        except (TypeError, ValueError):
            continue
        if end > start:
            windows.append(ReservationWindow(device_id=did, start=start, end=end))
    return windows


def blocking_reservations(
    now: datetime,
    reservations: Iterable[ReservationWindow],
    within: timedelta,
) -> list[ReservationWindow]:
    """Reservations live at `now` or starting within `within` after `now`."""
    moment = parse_reservation_datetime(now)
    horizon = moment + within
    blocking = [
        r for r in reservations
        if r.end > r.start and r.end > moment and r.start <= horizon
    ]
    blocking.sort(key=lambda r: (r.start, r.end, r.device_id))
    return blocking


def next_idle_window(
    now: datetime,
    reservations: Iterable[ReservationWindow],
    within: timedelta,
) -> datetime:
    """Earliest time >= now when no reservation is live and none starts within `within`."""
    moment = parse_reservation_datetime(now)
    candidate = moment
    active_or_future = sorted(
        (r for r in reservations if r.end > r.start and r.end > moment),
        key=lambda r: (r.start, r.end),
    )
    for r in active_or_future:
        if r.start <= candidate + within:
            if r.end > candidate:
                candidate = r.end
        else:
            break
    return candidate


def _cfg_dir() -> Path:
    from .global_onboarding import cfg_dir

    return cfg_dir()


def _device_names_from_yaml(cfg_dir: Path) -> dict[int, str]:
    import yaml

    names: dict[int, str] = {}
    for filename in ("devices.yml", "devices.yaml"):
        path = cfg_dir / filename
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        devices = data.get("devices") if isinstance(data, dict) else None
        if not isinstance(devices, list):
            continue
        for item in devices:
            if not isinstance(item, dict) or "device_id" not in item:
                continue
            try:
                gid = int(item["device_id"])
            except (TypeError, ValueError):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                names.setdefault(gid, name)
        break
    return names


def _device_names_from_host_meta(cfg_dir: Path) -> dict[int, str]:
    names: dict[int, str] = {}
    candidates = [cfg_dir / "host_directory_meta.env"]
    try:
        from ..host import host_tunnel_server as hts
        from ..host.host_directory_store import cfg_dir_from_file

        alt = cfg_dir_from_file(hts.__file__) / "host_directory_meta.env"
        if alt not in candidates:
            candidates.append(alt)
    except Exception:  # noqa: BLE001
        pass
    for path in candidates:
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            if key.startswith("DEVICE_") and key.endswith("_LABEL"):
                mid = key[len("DEVICE_"):-len("_LABEL")]
                if mid.isdigit():
                    label = val.strip().strip('"').strip("'")
                    if label:
                        names.setdefault(int(mid), label)
    return names


def _device_names_from_aliases(db_path: Path) -> dict[int, str]:
    if not db_path.exists():
        return {}
    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as conn:
            rows = conn.execute(
                "SELECT alias_id, display_name FROM FederatedDeviceAliases"
            ).fetchall()
    except sqlite3.Error:
        return {}
    names: dict[int, str] = {}
    for alias_id, display_name in rows:
        try:
            gid = int(alias_id)
        except (TypeError, ValueError):
            continue
        label = str(display_name or "").strip()
        if label:
            names.setdefault(gid, label)
    return names


def load_device_names(
    *,
    db_path: Path | None = None,
    cfg_dir: Path | None = None,
    needed_ids: Iterable[int] | None = None,
    use_control: bool = True,
) -> dict[int, str]:
    """Resolve device_id -> display name from local inventory, host directory, aliases, and live server."""
    root = Path(cfg_dir) if cfg_dir is not None else _cfg_dir()
    names = _device_names_from_yaml(root)
    for gid, label in _device_names_from_host_meta(root).items():
        names.setdefault(gid, label)
    path = Path(db_path) if db_path is not None else default_db_path()
    for gid, label in _device_names_from_aliases(path).items():
        names.setdefault(gid, label)

    missing = {int(i) for i in needed_ids} - set(names) if needed_ids is not None else set()
    if use_control and missing:
        try:
            from . import control

            reply = control.call(["devices", "list", "--json"], timeout=5.0)
            if reply and reply[0] == 0:
                for entry in json.loads(reply[1]):
                    if isinstance(entry, dict) and "device_id" in entry and entry.get("name"):
                        names.setdefault(int(entry["device_id"]), str(entry["name"]).strip())
        except Exception:  # noqa: BLE001 - best effort; fallback is "device <id>"
            pass
    return names


def live_device_names(*, db_path: Path | None = None) -> dict[int, str]:
    """In-daemon device name lookup using device_manager's live snapshot plus alias fallback."""
    names: dict[int, str] = {}
    try:
        from . import device_manager

        for gid, info in device_manager.device_inventory_snapshot().items():
            label = str(info.get("name") or "").strip()
            if label:
                names[int(gid)] = label
    except Exception:  # noqa: BLE001
        pass
    path = Path(db_path) if db_path is not None else default_db_path()
    for gid, label in _device_names_from_aliases(path).items():
        names.setdefault(gid, label)
    return names


def _resolve_names(
    device_names: Mapping[int, str] | Callable[[], Mapping[int, str]] | None,
    *,
    db_path: Path | None,
    needed_ids: Iterable[int],
) -> Mapping[int, str]:
    if device_names is None:
        return load_device_names(db_path=db_path, needed_ids=needed_ids)
    if callable(device_names):
        return device_names()
    return device_names


def _device_label(device_id: int, names: Mapping[int, str]) -> str:
    label = (names.get(device_id) or "").strip()
    return label or f"device {device_id}"


def _fmt_short_time(dt: datetime, now: datetime) -> str:
    if abs((dt - now).total_seconds()) < 24 * 3600:
        return dt.strftime("%H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


def _fmt_full_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S" if dt.second else "%Y-%m-%d %H:%M")


def _fmt_wait_entry(window: ReservationWindow, now: datetime, names: Mapping[int, str]) -> str:
    label = _device_label(window.device_id, names)
    if window.start <= now:
        return f"{label} until {_fmt_short_time(window.end, now)}"
    return f"{label} {_fmt_short_time(window.start, now)}-{_fmt_short_time(window.end, now)}"


def format_waiting_line(
    now: datetime,
    blocking: Sequence[ReservationWindow],
    all_reservations: Sequence[ReservationWindow],
    within: timedelta,
    names: Mapping[int, str],
) -> str:
    noun = "reservation" if len(blocking) == 1 else "reservations"
    items = ", ".join(_fmt_wait_entry(w, now, names) for w in blocking)
    idle_at = next_idle_window(now, all_reservations, within)
    return f"waiting: {len(blocking)} {noun} ({items}); next idle window {_fmt_short_time(idle_at, now)}"


def format_check_report(
    now: datetime,
    blocking: Sequence[ReservationWindow],
    all_reservations: Sequence[ReservationWindow],
    within: timedelta,
    names: Mapping[int, str],
    *,
    action: str = "restart",
) -> str:
    within_min = int(within.total_seconds() // 60)
    if not blocking:
        return f"Safe to {action} now (no reservations live or starting within {within_min} min)."
    noun = "reservation" if len(blocking) == 1 else "reservations"
    idle_at = next_idle_window(now, all_reservations, within)
    lines = [
        f"Not safe to {action} now: {len(blocking)} blocking {noun} (live or starting within {within_min} min):",
    ]
    for w in blocking:
        label = _device_label(w.device_id, names)
        status = " (live)" if w.start <= now else ""
        lines.append(f"  {label}: {_fmt_full_time(w.start)} - {_fmt_full_time(w.end)}{status}")
    lines.append(f"Next idle window: {_fmt_full_time(idle_at)}")
    return "\n".join(lines)


def _system_now() -> datetime:
    return datetime.now()


def _system_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _system_monotonic() -> float:
    return time.monotonic()


_now: Callable[[], datetime] = _system_now
_sleep: Callable[[float], None] = _system_sleep
_monotonic: Callable[[], float] = _system_monotonic


def _aborted_action_noun(action: str) -> str:
    return "devices not reloaded" if action == "reload devices" else "server not restarted"


def check_safe(
    *,
    action: str = "restart",
    within_minutes: int = DEFAULT_WITHIN_MINUTES,
    db_path: Path | None = None,
    reservations: Callable[[], Sequence[ReservationWindow]] | None = None,
    device_names: Mapping[int, str] | Callable[[], Mapping[int, str]] | None = None,
    now: Callable[[], datetime] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Print safe/busy status, blocking reservations, and next idle window; exit 0 if safe else 1."""
    stdout = sys.stdout if out is None else out
    stderr = sys.stderr if err is None else err
    now_fn = _now if now is None else now
    within = timedelta(minutes=within_minutes)
    current = parse_reservation_datetime(now_fn())
    try:
        rows = list(reservations()) if reservations is not None else read_reservations(db_path)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=stderr, flush=True)
        return 1
    blocking = blocking_reservations(current, rows, within)
    names = (
        _resolve_names(device_names, db_path=db_path, needed_ids=(w.device_id for w in blocking))
        if blocking else {}
    )
    print(
        format_check_report(current, blocking, rows, within, names, action=action),
        file=stdout,
        flush=True,
    )
    return 0 if not blocking else 1


def _make_monotonic(
    now_fn: Callable[[], datetime],
    monotonic: Callable[[], float] | None = None,
) -> Callable[[], float]:
    if monotonic is not None:
        return monotonic
    if _monotonic is not _system_monotonic:
        return _monotonic
    if now_fn is _system_now:
        return _monotonic
    start_wall = parse_reservation_datetime(now_fn())
    return lambda: (parse_reservation_datetime(now_fn()) - start_wall).total_seconds()


def wait_until_safe(
    *,
    action: str = "restart",
    within_minutes: int = DEFAULT_WITHIN_MINUTES,
    max_wait_minutes: int = DEFAULT_MAX_WAIT_MINUTES,
    db_path: Path | None = None,
    reservations: Callable[[], Sequence[ReservationWindow]] | None = None,
    device_names: Mapping[int, str] | Callable[[], Mapping[int, str]] | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    deadline_mono: float | None = None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    status_interval: float = STATUS_INTERVAL_SECONDS,
    out: TextIO | None = None,
    err: TextIO | None = None,
    busy_rc: int = 1,
    stop_event: threading.Event | None = None,
    require_db: bool = False,
) -> int:
    """Wait until no reservation is live or starting within `within_minutes`.

    Returns 0 when safe to proceed, `busy_rc` (default 1) on timeout, 1 on
    database error or server shutdown, or 130 on Ctrl-C.
    """
    stdout = sys.stdout if out is None else out
    stderr = sys.stderr if err is None else err
    now_fn = _now if now is None else now
    if sleep is not None:
        sleep_fn = sleep
    elif _sleep is not _system_sleep:
        sleep_fn = _sleep
    elif stop_event is not None:
        sleep_fn = stop_event.wait
    else:
        sleep_fn = _sleep
    within = timedelta(minutes=within_minutes)
    mono_fn = _make_monotonic(now_fn, monotonic)
    if deadline_mono is None:
        deadline_mono = mono_fn() + max_wait_minutes * 60.0
    last_status_mono: float | None = None
    tolerance = min(0.5, poll_interval / 4.0) if poll_interval > 0 else 0.0
    waited = False

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                print(f"Aborted; {_aborted_action_noun(action)}.", file=stderr, flush=True)
                return 1
            current = parse_reservation_datetime(now_fn())
            current_mono = mono_fn()
            try:
                if reservations is not None:
                    rows = list(reservations())
                else:
                    path = Path(db_path) if db_path is not None else default_db_path()
                    if not path.exists():
                        from . import control

                        if require_db or control.running():
                            raise RuntimeError(
                                f"reservation database not found at {path} while server is running"
                            )
                    rows = read_reservations(path)
            except RuntimeError as exc:
                print(
                    f"ERROR: {exc}; {_aborted_action_noun(action)}.",
                    file=stderr,
                    flush=True,
                )
                return 1
            if stop_event is not None and stop_event.is_set():
                print(f"Aborted; {_aborted_action_noun(action)}.", file=stderr, flush=True)
                return 1
            blocking = blocking_reservations(current, rows, within)
            if not blocking:
                if waited:
                    print(f"Idle window reached; proceeding with {action}.", file=stdout, flush=True)
                return 0
            names = _resolve_names(
                device_names,
                db_path=db_path,
                needed_ids=(w.device_id for w in blocking),
            )
            if current_mono >= deadline_mono:
                noun = "reservation" if len(blocking) == 1 else "reservations"
                items = ", ".join(_fmt_wait_entry(w, current, names) for w in blocking)
                idle_at = next_idle_window(current, rows, within)
                print(
                    f"Timed out after {max_wait_minutes} min: {len(blocking)} {noun} still blocking "
                    f"({items}); next idle window {_fmt_short_time(idle_at, current)}; "
                    f"{_aborted_action_noun(action)}.",
                    file=stderr,
                    flush=True,
                )
                return busy_rc
            if last_status_mono is None or (current_mono - last_status_mono) >= (status_interval - tolerance):
                print(
                    format_waiting_line(current, blocking, rows, within, names),
                    file=stdout,
                    flush=True,
                )
                last_status_mono = current_mono
            waited = True
            step = min(poll_interval, max(0.0, deadline_mono - current_mono))
            sleep_fn(step)
    except KeyboardInterrupt:
        print(f"\nAborted; {_aborted_action_noun(action)}.", file=stderr, flush=True)
        return 130


def gate_command(
    argv: Sequence[str],
    *,
    action: str,
    db_path: Path | None = None,
    reservations: Callable[[], Sequence[ReservationWindow]] | None = None,
    device_names: Mapping[int, str] | Callable[[], Mapping[int, str]] | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    deadline_mono: float | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    before_wait: Callable[[], int | None] | None = None,
    busy_rc: int = 1,
    stop_event: threading.Event | None = None,
    require_db: bool = False,
) -> int | None:
    """Handle --safe / --within / --max-wait / --check / -h for restart or reload devices.

    Returns None when the caller should proceed with the restart/reload, or an
    integer exit code when the command is already finished without acting.
    """
    stdout = sys.stdout if out is None else out
    stderr = sys.stderr if err is None else err
    if not argv:
        return None
    opts, error = parse_args(argv, action=action)
    if error is not None or opts is None:
        print(f"ERROR: {error}", file=stderr, flush=True)
        return 2
    if opts.help:
        print(usage_text(action), file=stdout, flush=True)
        return 0
    if opts.check:
        return check_safe(
            action=action,
            within_minutes=opts.within_minutes,
            db_path=db_path,
            reservations=reservations,
            device_names=device_names,
            now=now,
            out=stdout,
            err=stderr,
        )
    if opts.safe:
        if before_wait is not None:
            early = before_wait()
            if early is not None:
                return early
        rc = wait_until_safe(
            action=action,
            within_minutes=opts.within_minutes,
            max_wait_minutes=opts.max_wait_minutes,
            db_path=db_path,
            reservations=reservations,
            device_names=device_names,
            now=now,
            sleep=sleep,
            monotonic=monotonic,
            deadline_mono=deadline_mono,
            out=stdout,
            err=stderr,
            busy_rc=busy_rc,
            stop_event=stop_event,
            require_db=require_db,
        )
        return None if rc == 0 else rc
    return None
