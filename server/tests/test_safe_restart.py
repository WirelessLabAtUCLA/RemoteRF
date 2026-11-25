"""Tests for `serverrf restart --safe` and `serverrf reload devices --safe`."""

from __future__ import annotations

import builtins
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from remoteRF_server.server import commands, control, safe_restart
from remoteRF_server.server.commands import RestartRequested, dispatch
from remoteRF_server.server.safe_restart import ReservationWindow


class FakeClock:
    def __init__(self, start: datetime):
        self.current = start
        self.sleeps: list[float] = []
        self.on_sleep: list = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)
        for hook in list(self.on_sleep):
            hook(self.current)


def _seed_reservations_db(path: Path, rows: list[tuple], *, federated_ids: set[int] = frozenset()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS Reservations (
                res_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                device_id INTEGER NOT NULL,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP NOT NULL,
                salt TEXT NOT NULL,
                hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS FederatedReservations (
                res_id INTEGER PRIMARY KEY,
                issuer_deployment_id TEXT NOT NULL
            )
            """
        )
        for res_id, username, device_id, start_time, end_time in rows:
            conn.execute(
                "INSERT INTO Reservations (res_id, username, device_id, start_time, end_time, salt, hash) "
                "VALUES (?, ?, ?, ?, ?, 's', 'h')",
                (res_id, username, device_id, start_time, end_time),
            )
            if res_id in federated_ids:
                conn.execute(
                    "INSERT INTO FederatedReservations (res_id, issuer_deployment_id) VALUES (?, ?)",
                    (res_id, "11111111-1111-4111-8111-111111111111"),
                )
        conn.commit()


class SafeRestartWaitTests(unittest.TestCase):
    def test_waits_while_busy_and_restarts_once_free(self):
        t0 = datetime(2026, 11, 24, 23, 28, 0)
        clock = FakeClock(t0)
        rows = [
            ReservationWindow(0, datetime(2026, 11, 24, 23, 0, 0), datetime(2026, 11, 24, 23, 30, 0)),
            ReservationWindow(1, datetime(2026, 11, 24, 23, 30, 0), datetime(2026, 11, 24, 23, 31, 0)),
        ]
        names = {0: "Pluto 00", 1: "USRP 00"}
        out, err = io.StringIO(), io.StringIO()

        rc = safe_restart.gate_command(
            ["--safe"],
            action="restart",
            reservations=lambda: rows,
            device_names=names,
            now=clock.now,
            sleep=clock.sleep,
            out=out,
            err=err,
        )

        self.assertIsNone(rc)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(clock.current, datetime(2026, 11, 24, 23, 31, 0))
        self.assertEqual(clock.sleeps, [30.0] * 6)
        lines = out.getvalue().splitlines()
        waiting_lines = [line for line in lines if line.startswith("waiting:")]
        self.assertEqual(len(waiting_lines), 3)
        self.assertEqual(
            waiting_lines[0],
            "waiting: 2 reservations (Pluto 00 until 23:30, USRP 00 23:30-23:31); next idle window 23:31",
        )
        self.assertEqual(
            waiting_lines[2],
            "waiting: 1 reservation (USRP 00 until 23:31); next idle window 23:31",
        )
        self.assertIn("proceeding with restart", lines[-1])

    def test_waiting_line_matches_midnight_rollover_format(self):
        now = datetime(2026, 11, 24, 23, 20, 0)
        rows = [
            ReservationWindow(0, datetime(2026, 11, 24, 23, 0, 0), datetime(2026, 11, 24, 23, 30, 0)),
            ReservationWindow(1, datetime(2026, 11, 24, 23, 30, 0), datetime(2026, 11, 25, 0, 0, 0)),
        ]
        within = timedelta(minutes=15)
        blocking = safe_restart.blocking_reservations(now, rows, within)
        line = safe_restart.format_waiting_line(now, blocking, rows, within, {0: "Pluto 00", 1: "USRP 00"})
        self.assertEqual(
            line,
            "waiting: 2 reservations (Pluto 00 until 23:30, USRP 00 23:30-00:00); next idle window 00:00",
        )

    def test_honours_within_window(self):
        now = datetime(2026, 11, 24, 23, 0, 0)
        in_10 = [ReservationWindow(0, now + timedelta(minutes=10), now + timedelta(minutes=40))]
        in_20 = [ReservationWindow(0, now + timedelta(minutes=20), now + timedelta(minutes=50))]
        names = {0: "Pluto 00"}

        # Default --within is 15 min: 10 min blocks, 20 min does not.
        self.assertEqual(
            safe_restart.check_safe(reservations=lambda: in_10, device_names=names, now=lambda: now, out=io.StringIO()),
            1,
        )
        self.assertEqual(
            safe_restart.check_safe(reservations=lambda: in_20, device_names=names, now=lambda: now, out=io.StringIO()),
            0,
        )

        # Custom --within overrides the lookahead.
        self.assertEqual(
            safe_restart.check_safe(
                within_minutes=5, reservations=lambda: in_10, device_names=names, now=lambda: now, out=io.StringIO(),
            ),
            0,
        )
        self.assertEqual(
            safe_restart.check_safe(
                within_minutes=25, reservations=lambda: in_20, device_names=names, now=lambda: now, out=io.StringIO(),
            ),
            1,
        )

        # --within 0 only blocks reservations live now.
        live = [ReservationWindow(0, now - timedelta(minutes=5), now + timedelta(minutes=5))]
        self.assertEqual(
            safe_restart.check_safe(
                within_minutes=0, reservations=lambda: in_10, device_names=names, now=lambda: now, out=io.StringIO(),
            ),
            0,
        )
        self.assertEqual(
            safe_restart.check_safe(
                within_minutes=0, reservations=lambda: live, device_names=names, now=lambda: now, out=io.StringIO(),
            ),
            1,
        )

        # --safe waiting path also honours default 15 min --within: 10 min waits, 20 min proceeds immediately.
        clock_10 = FakeClock(now)
        rc_10 = safe_restart.gate_command(
            ["--safe"],
            action="restart",
            reservations=lambda: in_10,
            device_names=names,
            now=clock_10.now,
            sleep=clock_10.sleep,
            out=io.StringIO(),
            err=io.StringIO(),
        )
        self.assertIsNone(rc_10)
        self.assertEqual(clock_10.current, now + timedelta(minutes=40))
        self.assertGreater(len(clock_10.sleeps), 0)

        clock_20 = FakeClock(now)
        rc_20 = safe_restart.gate_command(
            ["--safe"],
            action="restart",
            reservations=lambda: in_20,
            device_names=names,
            now=clock_20.now,
            sleep=clock_20.sleep,
            out=io.StringIO(),
            err=io.StringIO(),
        )
        self.assertIsNone(rc_20)
        self.assertEqual(clock_20.sleeps, [])

    def test_max_wait_times_out_without_restarting(self):
        clock = FakeClock(datetime(2026, 11, 24, 23, 0, 0))
        rows = [
            ReservationWindow(0, datetime(2026, 11, 24, 22, 45, 0), datetime(2026, 11, 24, 23, 30, 0)),
        ]
        out, err = io.StringIO(), io.StringIO()

        rc = safe_restart.gate_command(
            ["--safe", "--max-wait", "2"],
            action="restart",
            reservations=lambda: rows,
            device_names={0: "Pluto 00"},
            now=clock.now,
            sleep=clock.sleep,
            out=out,
            err=err,
        )

        self.assertEqual(rc, 1)
        self.assertEqual(clock.sleeps, [30.0] * 4)
        self.assertEqual(len([l for l in out.getvalue().splitlines() if l.startswith("waiting:")]), 2)
        self.assertIn("Timed out after 2 min", err.getvalue())
        self.assertIn("Pluto 00 until 23:30", err.getvalue())
        self.assertIn("next idle window 23:30", err.getvalue())
        self.assertIn("server not restarted", err.getvalue())

        # --max-wait 0 fails immediately without sleeping when busy.
        zero_clock = FakeClock(datetime(2026, 11, 24, 23, 0, 0))
        zero_err = io.StringIO()
        rc0 = safe_restart.gate_command(
            ["--safe", "--max-wait=0"],
            action="restart",
            reservations=lambda: rows,
            device_names={0: "Pluto 00"},
            now=zero_clock.now,
            sleep=zero_clock.sleep,
            out=io.StringIO(),
            err=zero_err,
        )
        self.assertEqual(rc0, 1)
        self.assertEqual(zero_clock.sleeps, [])
        self.assertIn("Timed out after 0 min", zero_err.getvalue())

    def test_cancellation_mid_wait_is_noticed_on_next_30s_poll(self):
        clock = FakeClock(datetime(2026, 11, 24, 23, 0, 0))
        rows = [
            ReservationWindow(0, datetime(2026, 11, 24, 23, 0, 0), datetime(2026, 11, 24, 23, 30, 0)),
        ]

        def cancel_after_first_poll(now: datetime):
            if now >= datetime(2026, 11, 24, 23, 0, 30):
                rows.clear()

        clock.on_sleep.append(cancel_after_first_poll)
        out, err = io.StringIO(), io.StringIO()

        rc = safe_restart.gate_command(
            ["--safe"],
            action="restart",
            reservations=lambda: list(rows),
            device_names={0: "Pluto 00"},
            now=clock.now,
            sleep=clock.sleep,
            out=out,
            err=err,
        )

        self.assertIsNone(rc)
        self.assertEqual(clock.sleeps, [30.0])
        self.assertEqual(clock.current, datetime(2026, 11, 24, 23, 0, 30))
        self.assertIn("waiting: 1 reservation (Pluto 00 until 23:30); next idle window 23:30", out.getvalue())

    def test_ctrl_c_aborts_cleanly_without_restarting(self):
        now = datetime(2026, 11, 24, 23, 0, 0)
        rows = [ReservationWindow(0, now, now + timedelta(minutes=30))]
        out, err = io.StringIO(), io.StringIO()

        def interrupt(_seconds: float):
            raise KeyboardInterrupt

        rc = safe_restart.gate_command(
            ["--safe"],
            action="restart",
            reservations=lambda: rows,
            device_names={0: "Pluto 00"},
            now=lambda: now,
            sleep=interrupt,
            out=out,
            err=err,
        )
        self.assertEqual(rc, 130)
        self.assertIn("Aborted; server not restarted.", err.getvalue())

    def test_check_output_exit_codes_and_chained_idle_window(self):
        now = datetime(2026, 11, 24, 23, 20, 0)
        rows = [
            ReservationWindow(0, datetime(2026, 11, 24, 23, 0, 0), datetime(2026, 11, 24, 23, 30, 0)),
            ReservationWindow(1, datetime(2026, 11, 24, 23, 30, 0), datetime(2026, 11, 25, 0, 0, 0)),
            # Starts 10 min after 00:00 (< 15 min), so 00:00 is not an idle window; 00:40 is.
            ReservationWindow(2, datetime(2026, 11, 25, 0, 10, 0), datetime(2026, 11, 25, 0, 40, 0)),
            # Ended earlier; must be ignored.
            ReservationWindow(0, datetime(2026, 11, 24, 21, 0, 0), datetime(2026, 11, 24, 22, 0, 0)),
        ]
        names = {0: "Pluto 00", 1: "USRP 00", 2: "HackRF 00"}

        busy_out = io.StringIO()
        rc_busy = safe_restart.check_safe(
            action="restart",
            within_minutes=15,
            reservations=lambda: rows,
            device_names=names,
            now=lambda: now,
            out=busy_out,
        )
        self.assertEqual(rc_busy, 1)
        text = busy_out.getvalue()
        self.assertIn("Not safe to restart now: 2 blocking reservations", text)
        self.assertIn("Pluto 00: 2026-11-24 23:00 - 2026-11-24 23:30 (live)", text)
        self.assertIn("USRP 00: 2026-11-24 23:30 - 2026-11-25 00:00", text)
        self.assertNotIn("HackRF 00", text)
        self.assertIn("Next idle window: 2026-11-25 00:40", text)

        free_out = io.StringIO()
        rc_free = safe_restart.check_safe(
            action="restart",
            within_minutes=15,
            reservations=lambda: rows,
            device_names=names,
            now=lambda: datetime(2026, 11, 25, 0, 40, 0),
            out=free_out,
        )
        self.assertEqual(rc_free, 0)
        self.assertIn("Safe to restart now", free_out.getvalue())

    def test_arg_parsing_rejects_bad_flags_and_missing_safe_or_check(self):
        for bad in (
            ["--within", "10"],
            ["--max-wait", "60"],
            ["--safe", "--within", "-1"],
            ["--safe", "--within", "abc"],
            ["--safe", "--within"],
            ["--safe", "--max-wait", "-5"],
            ["--safe", "--within", "--max-wait", "10"],
            ["--safe=1"],
            ["--check=yes"],
            ["--bogus"],
            ["-x"],
            ["extra"],
        ):
            opts, err = safe_restart.parse_args(bad, action="restart")
            self.assertIsNone(opts, bad)
            self.assertIsNotNone(err, bad)

        defaults, err = safe_restart.parse_args(["--safe"])
        self.assertIsNone(err)
        self.assertTrue(defaults.safe and not defaults.check)
        self.assertEqual((defaults.within_minutes, defaults.max_wait_minutes), (15, 360))

        opts, err = safe_restart.parse_args(["--safe", "--check", "--within=20", "--max-wait=90"])
        self.assertIsNone(err)
        self.assertTrue(opts.safe and opts.check)
        self.assertEqual((opts.within_minutes, opts.max_wait_minutes), (20, 90))


class SafeRestartCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "remoterf"
        self.db_path = self.cfg / "db" / "reservations.db"
        self.sock = Path(self.tmp.name) / "serverrf.sock"
        for patch in (
            mock.patch.dict(
                os.environ,
                {"REMOTERF_CONFIG_DIR": str(self.cfg), "XDG_CONFIG_HOME": self.tmp.name},
            ),
            mock.patch.object(control, "socket_path", return_value=self.sock),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        self.cfg.mkdir(parents=True, exist_ok=True)
        (self.cfg / "devices.yml").write_text(
            "devices:\n"
            "  - device_id: 0\n    device_type: adalm_pluto\n    name: Pluto 00\n"
            "  - device_id: 2\n    device_type: hackrf\n    name: HackRF 00\n",
            encoding="utf-8",
        )
        (self.cfg / "host_directory_meta.env").write_text(
            "DEVICE_10_LABEL=USRP 00\n",
            encoding="utf-8",
        )

    def run_shell(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = dispatch(argv, in_daemon=False)
        return rc, out.getvalue(), err.getvalue()

    def test_reads_local_host_and_federated_rows_with_naive_local_times_and_no_usernames(self):
        # Naive server-local timestamps, matching what ReservationHandler writes to Reservations.
        _seed_reservations_db(
            self.db_path,
            [
                (1, "alice_private", 0, "2026-11-24 23:00:00", "2026-11-24 23:30:00"),
                (2, "bob_private", 10, "2026-11-24 23:30:00.250000", "2026-11-25 00:00:00.500000"),
                (3, "fed-999-private", 2, "2026-11-24 23:25:00", "2026-11-24 23:45:00"),
                (4, "expired_private", 0, "2026-11-24 21:00:00", "2026-11-24 22:00:00"),
            ],
            federated_ids={3},
        )
        # Parsed windows must remain naive local datetimes (same clock/tz as reservations.db).
        windows = safe_restart.read_reservations(self.db_path)
        self.assertEqual(len(windows), 4)
        self.assertTrue(all(w.start.tzinfo is None and w.end.tzinfo is None for w in windows))
        self.assertEqual(windows[0].start, datetime(2026, 11, 24, 23, 0, 0))
        self.assertEqual(windows[1].start, datetime(2026, 11, 24, 23, 30, 0, 250000))
        # Default clock is naive server-local datetime.now().
        self.assertIsNone(safe_restart._system_now().tzinfo)
        # Aware timestamps convert to naive local time, matching reservation._parse_reservation_datetime.
        aware = datetime(2026, 11, 24, 23, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            safe_restart.parse_reservation_datetime(aware.isoformat()),
            aware.astimezone().replace(tzinfo=None),
        )

        # --check lists local, host, and federated blocking reservations without any usernames.
        now = datetime(2026, 11, 24, 23, 20, 0)
        with mock.patch.object(safe_restart, "_now", lambda: now), \
                mock.patch.object(control, "call") as call:
            rc, out, err = self.run_shell(["restart", "--check"])

        self.assertEqual(rc, 1)
        self.assertEqual(err, "")
        call.assert_not_called()
        self.assertIn("Not safe to restart now: 3 blocking reservations", out)
        self.assertIn("Pluto 00: 2026-11-24 23:00 - 2026-11-24 23:30 (live)", out)
        self.assertIn("HackRF 00: 2026-11-24 23:25 - 2026-11-24 23:45", out)
        self.assertIn("USRP 00: 2026-11-24 23:30 - 2026-11-25 00:00", out)
        self.assertIn("Next idle window: 2026-11-25 00:00", out)
        for secret in ("alice_private", "bob_private", "fed-999-private", "expired_private"):
            self.assertNotIn(secret, out + err)

        # --check returns 0 without calling the daemon once the idle window arrives.
        with mock.patch.object(safe_restart, "_now", lambda: datetime(2026, 11, 25, 0, 0, 1)), \
                mock.patch.object(control, "call") as call:
            rc_ok, out_ok, err_ok = self.run_shell(["restart", "--check"])
        self.assertEqual((rc_ok, err_ok), (0, ""))
        self.assertIn("Safe to restart now", out_ok)
        call.assert_not_called()

    def test_cli_backs_off_after_a_busy_daemon_even_when_the_clock_moves(self):
        _seed_reservations_db(self.db_path, [])
        clock = FakeClock(datetime(2026, 11, 24, 23, 0, 0))
        start, ticks = clock.current, [0.0]

        def mono():
            ticks[0] += 0.001  # a real monotonic clock always moves a little between reads
            return (clock.current - start).total_seconds() + ticks[0]

        replies = [(safe_restart.BUSY_EXIT_CODE, "busy\n"), (safe_restart.BUSY_EXIT_CODE, "busy\n"), (0, "")]
        with mock.patch.object(control, "running", return_value=True), \
                mock.patch.object(control, "call", side_effect=lambda *a, **k: replies.pop(0)) as call, \
                mock.patch.object(safe_restart, "_make_monotonic", lambda *_a, **_k: mono), \
                mock.patch.object(safe_restart, "_now", clock.now), \
                mock.patch.object(safe_restart, "_sleep", clock.sleep):
            rc, out, err = self.run_shell(["restart", "--safe", "--within", "10"])

        self.assertEqual(rc, 0, err)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(clock.sleeps, [30.0, 30.0])

    def test_cli_restart_safe_non_interactive_waits_then_restarts_like_plain_restart(self):
        _seed_reservations_db(
            self.db_path,
            [(1, "alice", 0, "2026-11-24 23:00:00", "2026-11-24 23:01:00")],
        )
        clock = FakeClock(datetime(2026, 11, 24, 23, 0, 0))
        stdin = SimpleNamespace(isatty=lambda: False)

        with mock.patch.object(control, "running", return_value=True), \
                mock.patch.object(control, "call", return_value=(0, "")) as call, \
                mock.patch.object(safe_restart, "_now", clock.now), \
                mock.patch.object(safe_restart, "_sleep", clock.sleep), \
                mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(builtins, "input", side_effect=AssertionError("must not prompt")):
            rc, out, err = self.run_shell(["restart", "--safe"])

        self.assertEqual(rc, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(clock.sleeps, [30.0, 30.0])
        call.assert_called_once_with(
            ["restart", "--safe", "--within", "15", "--max-wait", "0"],
            timeout=30.0,
        )
        self.assertIn("waiting: 1 reservation (Pluto 00 until 23:01); next idle window 23:01", out)

        # Plain restart (no --safe) still dispatches immediately even while a reservation is active.
        clock.current = datetime(2026, 11, 24, 23, 0, 0)
        clock.sleeps.clear()
        with mock.patch.object(control, "running", return_value=True), \
                mock.patch.object(control, "call", return_value=(0, "")) as call, \
                mock.patch.object(safe_restart, "_now", clock.now), \
                mock.patch.object(safe_restart, "_sleep", clock.sleep):
            rc_plain, _, err_plain = self.run_shell(["restart"])
        self.assertEqual((rc_plain, err_plain), (0, ""))
        self.assertEqual(clock.sleeps, [])
        call.assert_called_once_with(["restart"], timeout=30.0)

    def test_cli_restart_safe_notices_sqlite_wal_cancellation_mid_wait(self):
        _seed_reservations_db(
            self.db_path,
            [(1, "alice", 0, "2026-11-24 23:00:00", "2026-11-24 23:30:00")],
        )
        writer = sqlite3.connect(self.db_path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.commit()

        clock = FakeClock(datetime(2026, 11, 24, 23, 0, 0))

        def cancel_in_db(now: datetime):
            if now >= datetime(2026, 11, 24, 23, 0, 30):
                writer.execute("DELETE FROM Reservations WHERE res_id = 1")
                writer.commit()

        clock.on_sleep.append(cancel_in_db)
        with mock.patch.object(control, "running", return_value=True), \
                mock.patch.object(control, "call", return_value=(0, "")) as call, \
                mock.patch.object(safe_restart, "_now", clock.now), \
                mock.patch.object(safe_restart, "_sleep", clock.sleep):
            rc, out, err = self.run_shell(["restart", "--safe"])

        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(clock.sleeps, [30.0])
        call.assert_called_once_with(
            ["restart", "--safe", "--within", "15", "--max-wait", "0"],
            timeout=30.0,
        )
        self.assertIn("Pluto 00 until 23:30", out)

    def test_cli_restart_safe_timeout_does_not_restart(self):
        _seed_reservations_db(
            self.db_path,
            [(1, "alice", 0, "2026-11-24 23:00:00", "2026-11-24 23:30:00")],
        )
        clock = FakeClock(datetime(2026, 11, 24, 23, 0, 0))

        with mock.patch.object(control, "running", return_value=True), \
                mock.patch.object(control, "call") as call, \
                mock.patch.object(safe_restart, "_now", clock.now), \
                mock.patch.object(safe_restart, "_sleep", clock.sleep):
            rc, out, err = self.run_shell(["restart", "--safe", "--max-wait", "1"])

        self.assertEqual(rc, 1)
        call.assert_not_called()
        self.assertIn("Timed out after 1 min", err)
        self.assertIn("server not restarted", err)

    def test_cli_reload_devices_safe_and_check(self):
        _seed_reservations_db(
            self.db_path,
            [(1, "alice", 10, "2026-11-24 23:00:00", "2026-11-24 23:01:00")],
        )
        clock = FakeClock(datetime(2026, 11, 24, 23, 0, 0))

        # --check when busy does not call the daemon.
        with mock.patch.object(safe_restart, "_now", clock.now), \
                mock.patch.object(control, "call") as call:
            rc, out, _ = self.run_shell(["reload", "devices", "--check"])
        self.assertEqual(rc, 1)
        self.assertIn("Not safe to reload devices now", out)
        self.assertIn("USRP 00", out)
        call.assert_not_called()

        # --safe when server is not running fails immediately without waiting.
        with mock.patch.object(safe_restart, "_now", clock.now), \
                mock.patch.object(safe_restart, "_sleep", clock.sleep), \
                mock.patch.object(control, "running", return_value=False):
            rc, _, err = self.run_shell(["reload", "devices", "--safe"])
        self.assertEqual(rc, 1)
        self.assertEqual(clock.sleeps, [])
        self.assertIn("not running", err)

        # --safe timeout does not reload and reports why.
        timeout_clock = FakeClock(datetime(2026, 11, 24, 23, 0, 0))
        with mock.patch.object(safe_restart, "_now", timeout_clock.now), \
                mock.patch.object(safe_restart, "_sleep", timeout_clock.sleep), \
                mock.patch.object(control, "running", return_value=True), \
                mock.patch.object(control, "call") as call:
            rc_to, _, err_to = self.run_shell(["reload", "devices", "--safe", "--max-wait", "0"])
        self.assertEqual(rc_to, 1)
        self.assertIn("devices not reloaded", err_to)
        call.assert_not_called()

        # --safe waits then reloads with the extended 120s timeout.
        with mock.patch.object(safe_restart, "_now", clock.now), \
                mock.patch.object(safe_restart, "_sleep", clock.sleep), \
                mock.patch.object(control, "running", return_value=True), \
                mock.patch.object(control, "call", return_value=(0, "")) as call:
            rc, out, err = self.run_shell(["reload", "devices", "--safe"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(clock.sleeps, [30.0, 30.0])
        call.assert_called_once_with(
            ["reload", "devices", "--safe", "--within", "15", "--max-wait", "0"],
            timeout=120.0,
        )
        self.assertIn("USRP 00 until 23:01", out)

    def test_unreadable_database_fails_closed(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_bytes(b"not a sqlite database")
        with mock.patch.object(control, "running", return_value=True), \
                mock.patch.object(control, "call") as call:
            rc, _, err = self.run_shell(["restart", "--safe"])
        self.assertEqual(rc, 1)
        call.assert_not_called()
        self.assertIn("ERROR:", err)

    def test_missing_database_while_server_running_fails_closed(self):
        self.assertFalse(self.db_path.exists())
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(control, "running", return_value=True):
            rc = safe_restart.wait_until_safe(
                action="restart",
                db_path=self.db_path,
                out=out,
                err=err,
            )
        self.assertNotEqual(rc, 0)
        self.assertIn("ERROR:", err.getvalue())
        self.assertIn("reservations.db", err.getvalue())
        self.assertIn("server not restarted", err.getvalue())

        with mock.patch.object(control, "running", return_value=True), \
                mock.patch.object(control, "call") as call:
            rc_cli, _, err_cli = self.run_shell(["restart", "--safe"])
        self.assertEqual(rc_cli, 1)
        call.assert_not_called()
        self.assertIn("ERROR:", err_cli)
        self.assertIn("server not restarted", err_cli)

        with mock.patch.object(control, "running", return_value=True), \
                mock.patch.object(control, "call") as call:
            rc_rel, _, err_rel = self.run_shell(["reload", "devices", "--safe"])
        self.assertEqual(rc_rel, 1)
        call.assert_not_called()
        self.assertIn("ERROR:", err_rel)
        self.assertIn("devices not reloaded", err_rel)

        # When the server is not running, direct wait_until_safe still treats a missing db as empty.
        out_off, err_off = io.StringIO(), io.StringIO()
        with mock.patch.object(control, "running", return_value=False):
            rc_off = safe_restart.wait_until_safe(
                action="restart",
                db_path=self.db_path,
                out=out_off,
                err=err_off,
            )
        self.assertEqual(rc_off, 0)
        self.assertEqual(err_off.getvalue(), "")

    def test_cli_restart_safe_when_server_stopped_does_not_wait(self):
        from remoteRF_server.server import console

        _seed_reservations_db(
            self.db_path,
            [(1, "alice", 0, "2026-11-24 23:00:00", "2026-11-25 00:00:00")],
        )
        clock = FakeClock(datetime(2026, 11, 24, 23, 0, 0))

        # With tmux available: starts detached immediately without waiting.
        with mock.patch.object(control, "running", return_value=False), \
                mock.patch.object(console, "available", return_value=True), \
                mock.patch.object(console, "start_detached", return_value=True) as start_detached, \
                mock.patch.object(safe_restart, "_now", clock.now), \
                mock.patch.object(safe_restart, "_sleep", clock.sleep):
            rc, out, err = self.run_shell(["restart", "--safe"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(clock.sleeps, [])
        self.assertIn("Server started.", out)
        start_detached.assert_called_once()

        # Without tmux: fails immediately with not-running error and no wait.
        clock.sleeps.clear()
        with mock.patch.object(control, "running", return_value=False), \
                mock.patch.object(console, "available", return_value=False), \
                mock.patch.object(safe_restart, "_now", clock.now), \
                mock.patch.object(safe_restart, "_sleep", clock.sleep):
            rc2, out2, err2 = self.run_shell(["restart", "--safe"])
        self.assertEqual(rc2, 1)
        self.assertEqual(clock.sleeps, [])
        self.assertIn("not running", out2 + err2)
        self.assertNotIn("Idle window reached", out2)

    def test_cli_safe_rechecks_in_daemon_and_loops_back_when_booked_in_gap(self):
        env = mock.patch.dict(
            os.environ,
            {
                "GRPC_PORT": os.environ.get("GRPC_PORT", "61005"),
                "CERT_PORT": os.environ.get("CERT_PORT", "61006"),
                "REMOTERF_BIND_HOST": os.environ.get("REMOTERF_BIND_HOST", "127.0.0.1"),
            },
        )
        with env:
            from remoteRF_server.server import grpc_server

            _seed_reservations_db(self.db_path, [])
            clock = FakeClock(datetime(2026, 11, 24, 23, 0, 0))
            seen_calls: list[tuple[list[str], float]] = []

            def fake_call(argv, *, timeout=30.0, **kwargs):
                seen_calls.append((list(argv), timeout))
                if len(seen_calls) == 1:
                    # Student books an immediate reservation after CLI check, before daemon acts.
                    _seed_reservations_db(
                        self.db_path,
                        [(1, "alice", 0, "2026-11-24 23:00:00", "2026-11-24 23:01:00")],
                    )
                buf = io.StringIO()
                prev = getattr(control._bridge, "current", None)
                control._bridge.current = object()
                try:
                    with contextlib.redirect_stdout(buf):
                        rc = grpc_server.control_handler(argv)
                finally:
                    control._bridge.current = prev
                return rc, buf.getvalue()

            with mock.patch.object(control, "running", return_value=True), \
                    mock.patch.object(control, "call", side_effect=fake_call), \
                    mock.patch.object(grpc_server.reservation_handler, "filepath", self.db_path), \
                    mock.patch.object(grpc_server, "_request_exit") as req_exit, \
                    mock.patch.object(safe_restart, "live_device_names", return_value={0: "Pluto 00"}), \
                    mock.patch.object(safe_restart, "_now", clock.now), \
                    mock.patch.object(safe_restart, "_sleep", clock.sleep):
                rc, out, err = self.run_shell(["restart", "--safe", "--within", "10"])

            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(
                seen_calls,
                [
                    (["restart", "--safe", "--within", "10", "--max-wait", "0"], 30.0),
                    (["restart", "--safe", "--within", "10", "--max-wait", "0"], 30.0),
                ],
            )
            req_exit.assert_called_once_with("restart")
            self.assertEqual(clock.sleeps, [30.0, 30.0])
            self.assertIn("waiting: 1 reservation (Pluto 00 until 23:01)", out)

            # Same protection for reload devices --safe with --max-wait 0: refuses without reloading.
            _seed_reservations_db(self.db_path, [])
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM Reservations")
                conn.commit()
            zero_clock = FakeClock(datetime(2026, 11, 24, 23, 0, 0))
            reload_calls: list[tuple[list[str], float]] = []

            def fake_reload_call(argv, *, timeout=30.0, **kwargs):
                reload_calls.append((list(argv), timeout))
                _seed_reservations_db(
                    self.db_path,
                    [(2, "bob", 0, "2026-11-24 23:04:00", "2026-11-24 23:20:00")],
                )
                buf = io.StringIO()
                prev = getattr(control._bridge, "current", None)
                control._bridge.current = object()
                try:
                    with contextlib.redirect_stdout(buf):
                        rc = grpc_server.control_handler(argv)
                finally:
                    control._bridge.current = prev
                return rc, buf.getvalue()

            with mock.patch.object(control, "running", return_value=True), \
                    mock.patch.object(control, "call", side_effect=fake_reload_call), \
                    mock.patch.object(grpc_server.reservation_handler, "filepath", self.db_path), \
                    mock.patch("remoteRF_server.server.device_manager.reload_devices") as reload_dev, \
                    mock.patch.object(safe_restart, "live_device_names", return_value={0: "Pluto 00"}), \
                    mock.patch.object(safe_restart, "_now", zero_clock.now), \
                    mock.patch.object(safe_restart, "_sleep", zero_clock.sleep):
                rc_rel, _, err_rel = self.run_shell(
                    ["reload", "devices", "--safe", "--within", "5", "--max-wait", "0"]
                )

            self.assertEqual(rc_rel, 1)
            reload_dev.assert_not_called()
            self.assertEqual(
                reload_calls,
                [(["reload", "devices", "--safe", "--within", "5", "--max-wait", "0"], 120.0)],
            )
            self.assertIn("Timed out after 0 min", err_rel)
            self.assertIn("devices not reloaded", err_rel)

    def test_help_text_explains_safe_restart_for_upgrades(self):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.output.plain_text import PlainTextOutput

        out = io.StringIO()
        with create_app_session(output=PlainTextOutput(out)), contextlib.redirect_stdout(out):
            rc = commands.cmd_help(["server"])
        self.assertEqual(rc, 0)
        text = " ".join(line.strip("│|╭╮╰╯─+- ") for line in out.getvalue().splitlines())
        text = " ".join(text.split())
        self.assertIn("--safe", text)
        self.assertIn("upgrades", text)
        self.assertIn("disrupts", text)
        self.assertIn("reservations survive", text)

        rc_h, out_h, err_h = self.run_shell(["restart", "--help"])
        self.assertEqual((rc_h, err_h), (0, ""))
        self.assertIn("--within", out_h)
        self.assertIn("--max-wait", out_h)
        self.assertIn("--check", out_h)


class SafeRestartConsoleTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(
            os.environ,
            {
                "GRPC_PORT": os.environ.get("GRPC_PORT", "61005"),
                "CERT_PORT": os.environ.get("CERT_PORT", "61006"),
                "REMOTERF_BIND_HOST": os.environ.get("REMOTERF_BIND_HOST", "127.0.0.1"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        from remoteRF_server.server import grpc_server

        self.grpc_server = grpc_server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "reservations.db"

    def test_console_restart_and_reload_devices_accept_safe_flags(self):
        _seed_reservations_db(
            self.db_path,
            [(1, "alice", 0, "2026-11-24 23:00:00", "2026-11-24 23:01:00")],
        )
        clock = FakeClock(datetime(2026, 11, 24, 23, 0, 0))

        with mock.patch.object(self.grpc_server.reservation_handler, "filepath", self.db_path), \
                mock.patch.object(safe_restart, "live_device_names", return_value={0: "Pluto 00"}), \
                mock.patch.object(safe_restart, "_now", clock.now), \
                mock.patch.object(safe_restart, "_sleep", clock.sleep):
            # --check at console returns 1 without raising RestartRequested.
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = dispatch(["restart", "--check"], in_daemon=True)
            self.assertEqual(rc, 1)
            self.assertIn("Pluto 00", out.getvalue())

            # --safe waits and then raises RestartRequested once idle.
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                with self.assertRaises(RestartRequested):
                    dispatch(["restart", "--safe"], in_daemon=True)
            self.assertEqual(clock.sleeps, [30.0, 30.0])
            self.assertIn("waiting: 1 reservation (Pluto 00 until 23:01); next idle window 23:01", out.getvalue())

            # Ctrl-C during console wait returns 130 without restarting or escaping to main().
            clock.current = datetime(2026, 11, 24, 23, 0, 0)
            with mock.patch.object(safe_restart, "_sleep", side_effect=KeyboardInterrupt):
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    rc_int = dispatch(["restart", "--safe"], in_daemon=True)
            self.assertEqual(rc_int, 130)
            self.assertIn("Aborted; server not restarted.", err.getvalue())

            # reload devices --check and --safe also work at the console.
            clock.current = datetime(2026, 11, 24, 23, 0, 0)
            clock.sleeps.clear()
            with mock.patch("remoteRF_server.server.device_manager.reload_devices") as reload_dev:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(dispatch(["reload", "devices", "--check"], in_daemon=True), 1)
                reload_dev.assert_not_called()

                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(dispatch(["reload", "devices", "--safe"], in_daemon=True), 0)
                reload_dev.assert_called_once_with()
                self.assertEqual(clock.sleeps, [30.0, 30.0])

            # server_input() integrates console parsing and _exit_action cleanly.
            self.grpc_server._exit_action = None
            clock.current = datetime(2026, 11, 24, 23, 0, 0)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(self.grpc_server.server_input("restart --check"))
            self.assertIsNone(self.grpc_server._exit_action)

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(self.grpc_server.server_input("restart --safe --max-wait 0"))
            self.assertIsNone(self.grpc_server._exit_action)

            clock.sleeps.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(self.grpc_server.server_input("restart --safe"))
            self.assertEqual(self.grpc_server._exit_action, "restart")
            self.assertEqual(clock.sleeps, [30.0, 30.0])

    def test_remote_admin_shell_restart_notices(self):
        from remoteRF_server import remote_admin_shell

        sh = remote_admin_shell.Shell.__new__(remote_admin_shell.Shell)
        sh.pin = lambda: {"read_only": 0}
        sh.audit = mock.Mock()

        def run_cmd(argv: list[str]) -> str:
            out = io.StringIO()
            with mock.patch.object(commands, "dispatch", return_value=0), \
                    contextlib.redirect_stdout(out):
                sh.command(argv)
            return out.getvalue()

        self.assertIn("this session ends; reconnect", run_cmd(["restart"]))
        self.assertIn("Waiting for an idle window", run_cmd(["restart", "--safe"]))
        self.assertEqual(run_cmd(["restart", "--check"]), "")
        self.assertEqual(run_cmd(["restart", "--safe", "--check"]), "")
        self.assertEqual(run_cmd(["restart", "--help"]), "")
        self.assertEqual(run_cmd(["restart", "--within", "10"]), "")

    def test_console_safe_wait_aborts_immediately_on_external_stop_without_overwriting_exit_action(self):
        _seed_reservations_db(
            self.db_path,
            [(1, "alice", 0, "2026-11-24 23:00:00", "2026-11-25 00:00:00")],
        )
        prev_runtime = self.grpc_server._runtime
        prev_exit = self.grpc_server._exit_action
        prev_session = self.grpc_server.session
        self.addCleanup(setattr, self.grpc_server, "_runtime", prev_runtime)
        self.addCleanup(setattr, self.grpc_server, "_exit_action", prev_exit)
        self.addCleanup(setattr, self.grpc_server, "session", prev_session)

        # Part 1: real stop_event.wait wakeup (no _sleep patch) aborts immediately on _request_exit("quit").
        stop_event = threading.Event()
        self.grpc_server._runtime = SimpleNamespace(stop_event=stop_event)
        self.grpc_server._exit_action = None
        self.grpc_server.session = SimpleNamespace(app=SimpleNamespace(is_running=False, loop=None))

        result: dict[str, object] = {}
        entered = threading.Event()

        def run_console_wait():
            entered.set()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result["keep_running"] = self.grpc_server.server_input("restart --safe")

        with mock.patch.object(self.grpc_server.reservation_handler, "filepath", self.db_path), \
                mock.patch.object(safe_restart, "live_device_names", return_value={0: "Pluto 00"}), \
                mock.patch.object(safe_restart, "_now", lambda: datetime(2026, 11, 24, 23, 0, 0)):
            worker = threading.Thread(target=run_console_wait, daemon=True)
            started = time.monotonic()
            worker.start()
            entered.wait(timeout=2.0)
            time.sleep(0.05)
            self.grpc_server._request_exit("quit")
            worker.join(timeout=2.0)
            elapsed = time.monotonic() - started

        self.assertFalse(worker.is_alive(), "console restart --safe did not wake up when stop_event was set")
        self.assertLess(elapsed, 2.0)
        self.assertIs(result.get("keep_running"), False)
        self.assertEqual(self.grpc_server._exit_action, "quit")

        # Part 2: reload devices --safe also aborts on external stop without reloading devices.
        stop_event2 = threading.Event()
        self.grpc_server._runtime = SimpleNamespace(stop_event=stop_event2)
        self.grpc_server._exit_action = None
        clock = FakeClock(datetime(2026, 11, 24, 23, 0, 0))
        clock.on_sleep.append(lambda _now: self.grpc_server._request_exit("quit"))

        with mock.patch.object(self.grpc_server.reservation_handler, "filepath", self.db_path), \
                mock.patch.object(safe_restart, "live_device_names", return_value={0: "Pluto 00"}), \
                mock.patch.object(safe_restart, "_now", clock.now), \
                mock.patch.object(safe_restart, "_sleep", clock.sleep), \
                mock.patch("remoteRF_server.server.device_manager.reload_devices") as reload_dev, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            keep_running = self.grpc_server.server_input("reload devices --safe")

        self.assertFalse(keep_running)
        self.assertEqual(self.grpc_server._exit_action, "quit")
        reload_dev.assert_not_called()

    def test_remote_admin_watchdog_suspends_idle_timeout_during_safe_wait(self):
        from remoteRF_server.server import remote_admin as ra
        from remoteRF_server.server import remote_admin_ssh as ssh

        cfg = Path(self.tmp.name) / "ra_cfg"
        env = {
            "XDG_CONFIG_HOME": str(cfg),
            "XDG_DATA_HOME": str(cfg / "data"),
            "REMOTERF_CONFIG_DIR": str(cfg / "remoterf"),
        }

        class TickEvent:
            def __init__(self, hooks):
                self._hooks = list(hooks)
                self._idx = 0

            def wait(self, _timeout=None):
                if self._idx >= len(self._hooks):
                    return True
                hook = self._hooks[self._idx]
                self._idx += 1
                if hook is not None:
                    hook()
                return False

        with mock.patch.dict(os.environ, env):
            base = 1000.0
            now = [base]

            def make_session():
                with mock.patch.object(ssh.time, "monotonic", lambda: base):
                    return ssh._Session(1, "jdoe", "SHA256:dev", "lan", "laptop")

            # 1. Open `restart --safe` suspends 15-minute idle kill, then refreshes idle clock on command_end.
            sid = "sess-safe-1"
            sess = make_session()
            ra.audit(
                "command",
                admin="jdoe",
                session_id=sid,
                detail={"command": "restart", "argv": ["restart", "--safe"], "tier": "write"},
            )
            ended_ticks: list[str | None] = []

            def tick1():
                now[0] = base + ssh.IDLE_SECONDS + 300.0  # 20 min idle while waiting

            def tick2():
                ended_ticks.append(sess.ended)
                ra.audit(
                    "command_end",
                    admin="jdoe",
                    session_id=sid,
                    detail={"command": "restart", "argv": ["restart", "--safe"], "tier": "write", "ms": 1200000},
                    rc=1,
                )
                now[0] = base + ssh.IDLE_SECONDS + 301.0

            def tick3():
                ended_ticks.append(sess.ended)
                now[0] = base + ssh.IDLE_SECONDS + 301.0 + ssh.IDLE_SECONDS + 5.0  # >15 min AFTER command_end

            with mock.patch.object(ssh.time, "monotonic", lambda: now[0]), \
                    mock.patch.object(ssh, "_still_allowed", return_value=None), \
                    mock.patch.object(ssh, "_notice"), \
                    mock.patch.object(ssh, "_stop") as stop_proc:
                ssh._watchdog(sid, sess, mock.Mock(), mock.Mock(), mock.Mock(), TickEvent([tick1, tick2, tick3]))

            self.assertEqual(ended_ticks, [None, None])
            self.assertEqual(sess.ended, "idle for 15 minutes")
            stop_proc.assert_called_once()

            # 2. Open `reload devices --safe` also suspends 15-minute idle kill.
            sid_rel = "sess-safe-rel"
            sess_rel = make_session()
            ra.audit(
                "command",
                admin="jdoe",
                session_id=sid_rel,
                detail={"command": "reload devices", "argv": ["reload", "devices", "--safe", "--within", "5"], "tier": "write"},
            )
            now[0] = base + ssh.IDLE_SECONDS + 600.0
            with mock.patch.object(ssh.time, "monotonic", lambda: now[0]), \
                    mock.patch.object(ssh, "_still_allowed", return_value=None), \
                    mock.patch.object(ssh, "_notice"), \
                    mock.patch.object(ssh, "_stop") as stop_proc:
                ssh._watchdog(sid_rel, sess_rel, mock.Mock(), mock.Mock(), mock.Mock(), TickEvent([None]))
            self.assertIsNone(sess_rel.ended)
            stop_proc.assert_not_called()

            # 3. Non-waiting commands (e.g. `restart --check`) still enforce the 15-minute idle kill.
            sid_chk = "sess-check"
            sess_chk = make_session()
            ra.audit(
                "command",
                admin="jdoe",
                session_id=sid_chk,
                detail={"command": "restart", "argv": ["restart", "--check"], "tier": "write"},
            )
            now[0] = base + ssh.IDLE_SECONDS + 60.0
            with mock.patch.object(ssh.time, "monotonic", lambda: now[0]), \
                    mock.patch.object(ssh, "_still_allowed", return_value=None), \
                    mock.patch.object(ssh, "_notice"), \
                    mock.patch.object(ssh, "_stop") as stop_proc:
                ssh._watchdog(sid_chk, sess_chk, mock.Mock(), mock.Mock(), mock.Mock(), TickEvent([None]))
            self.assertEqual(sess_chk.ended, "idle for 15 minutes")
            stop_proc.assert_called_once()

            # 4. MAX_SECONDS and _still_allowed() remain active during an open safe wait.
            sess_max = make_session()
            now[0] = base + ssh.MAX_SECONDS + 1.0
            with mock.patch.object(ssh.time, "monotonic", lambda: now[0]), \
                    mock.patch.object(ssh, "_still_allowed", return_value=None), \
                    mock.patch.object(ssh, "_notice"), \
                    mock.patch.object(ssh, "_stop") as stop_proc:
                ssh._watchdog(sid_rel, sess_max, mock.Mock(), mock.Mock(), mock.Mock(), TickEvent([None]))
            self.assertEqual(sess_max.ended, "8-hour limit")
            stop_proc.assert_called_once()

            sess_kick = make_session()
            now[0] = base + ssh.IDLE_SECONDS + 60.0
            with mock.patch.object(ssh.time, "monotonic", lambda: now[0]), \
                    mock.patch.object(ssh, "_still_allowed", return_value="ended from the console"), \
                    mock.patch.object(ssh, "_notice"), \
                    mock.patch.object(ssh, "_stop") as stop_proc:
                ssh._watchdog(sid_rel, sess_kick, mock.Mock(), mock.Mock(), mock.Mock(), TickEvent([None]))
            self.assertEqual(sess_kick.ended, "ended from the console")
            stop_proc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
