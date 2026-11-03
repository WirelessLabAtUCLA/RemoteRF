"""Stay-logged-in tokens for the deployment protocol."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ClientSessionTests(unittest.TestCase):
    def setUp(self):
        from remoteRF_server.server import acc_perms, reservation, user_group_handler as ugh

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for target in (reservation, acc_perms, ugh):
            patch = mock.patch.object(target, "get_db_dir", return_value=root)
            patch.start()
            self.addCleanup(patch.stop)
        self.perms = acc_perms.perms_db() if hasattr(acc_perms, "perms_db") else acc_perms.Perms()
        self.groups = ugh.UserGroupHandler()
        self.h = reservation.ReservationHandler()
        for name, value in (("perms", self.perms), ("user_group_handler", self.groups)):
            patch = mock.patch.object(reservation, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        gid = self.groups.create_user_group(
            group_name="g", devices_whitelist=[], max_reservation_time_sec=60,
            max_reservations=1, lifetime_sec=0,
        )
        self.groups.create_enrollment_codes(code_names=["C1"], group_id=gid, uses=1, duration_sec=0)
        from remoteRF_server.common.utils import map_arg
        created = self.h.create_user(un=map_arg("alice"), pw=map_arg("password1"), em=map_arg("a@x.org"), ec=map_arg("C1"))
        self.assertIn("UC", created, created)
        self.map = map_arg

    def login(self, secret, remember=False):
        from remoteRF_server.common.utils import unmap_arg
        args = {"un": self.map("alice"), "pw": self.map(secret)}
        if remember:
            args["remember"] = self.map(True)
        result = self.h.handle_call(function_name="login", args=args)
        return {k: unmap_arg(v) for k, v in result.items()}

    def test_token_replaces_the_password_and_logout_revokes_it(self):
        plain = self.login("password1")
        self.assertEqual(plain, {"UC": "alice"})           # no token unless asked
        remembered = self.login("password1", remember=True)
        token = remembered["token"]
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(self.login(token), {"UC": "alice"})   # token works as the secret
        self.assertNotIn("token", self.login(token))           # and never mints another
        # Only a hash is stored.
        with mock.patch.object(self.h, "filepath", self.h.filepath):
            import sqlite3
            rows = sqlite3.connect(self.h.filepath).execute("SELECT token_hash FROM ClientSessions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn(token, rows[0][0])
        # Wrong user, garbage, and a revoked token all fail.
        self.assertIn("UE", self.h.login_user("bob", token))
        self.assertIn("UE", self.login("x" * 43))
        self.h.handle_call(function_name="logout", args={"un": self.map("alice"), "pw": self.map(token)})
        self.assertIn("UE", self.login(token))
        self.assertEqual(self.login("password1"), {"UC": "alice"})

    def test_removing_the_user_revokes_sessions(self):
        token = self.login("password1", remember=True)["token"]
        self.h.remove_user(username="alice")
        self.assertIn("UE", self.h.login_user("alice", token))


    def test_direct_offer_needs_the_session_secret(self):
        """ACC:direct_offer is authenticated like every other account call."""
        from remoteRF_server.common.utils import unmap_arg

        token = self.login("password1", remember=True)["token"]
        with mock.patch("remoteRF_server.server.direct_path.offer", return_value={"ice": self.map("{}")}) as offer:
            for secret in ("wrong", "x" * 43, ""):
                result = self.h.handle_call(
                    function_name="direct_offer",
                    args={"un": self.map("alice"), "pw": self.map(secret), "ice": self.map("{}")},
                )
                self.assertEqual(unmap_arg(result["ace"]), "Invalid login credentials.", secret)
            offer.assert_not_called()
            result = self.h.handle_call(
                function_name="direct_offer",
                args={"un": self.map("alice"), "pw": self.map(token), "ice": self.map('{"id": "p1"}')},
            )
            self.assertIn("ice", result)
            offer.assert_called_once_with(self.h._session_hash(token), "alice", '{"id": "p1"}')
        # A revoked session cannot offer either.
        self.h.handle_call(function_name="logout", args={"un": self.map("alice"), "pw": self.map(token)})
        result = self.h.handle_call(
            function_name="direct_offer",
            args={"un": self.map("alice"), "pw": self.map(token), "ice": self.map("{}")},
        )
        self.assertEqual(unmap_arg(result["ace"]), "Invalid login credentials.")

if __name__ == "__main__":
    unittest.main()


class ReservedTotalTests(unittest.TestCase):
    def test_total_survives_purge_and_refunds_unused_time_on_cancel(self):
        import sqlite3
        from datetime import datetime, timedelta
        from remoteRF_server.server import reservation

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(reservation, "get_db_dir", return_value=Path(tmp)):
            reservation.ReservationHandler()
            db = sqlite3.connect(Path(tmp) / "reservations.db")
            now = datetime.now()
            add = "INSERT INTO Reservations (username, device_id, start_time, end_time, salt, hash) VALUES ('u', 1, ?, ?, '', '')"
            total = lambda: db.execute("SELECT seconds FROM ReservedTotal").fetchone()[0]
            db.execute(add, (now - timedelta(hours=3), now - timedelta(hours=2)))  # 1 h, past
            db.execute(add, (now + timedelta(hours=1), now + timedelta(hours=3)))  # 2 h, future
            self.assertAlmostEqual(total(), 3 * 3600, delta=1)
            db.execute("DELETE FROM Reservations WHERE end_time < ?", (now,))  # purge: no change
            self.assertAlmostEqual(total(), 3 * 3600, delta=1)
            db.execute("DELETE FROM Reservations")  # cancel the future one: unused 2 h refunded
            self.assertAlmostEqual(total(), 3600, delta=2)

    def test_a_partner_device_is_left_to_its_owner_even_with_the_old_triggers(self):
        import sqlite3
        from datetime import datetime, timedelta
        from remoteRF_server.server import reservation
        from remoteRF_server.server.federation.home_devices import ALIAS_BASE

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(reservation, "get_db_dir", return_value=Path(tmp)):
            reservation.ReservationHandler()
            db = sqlite3.connect(Path(tmp) / "reservations.db", isolation_level=None)
            # A lab from before the filter: its insert trigger counts every row.
            db.executescript("DROP TRIGGER reserved_total_ins; CREATE TRIGGER reserved_total_ins AFTER INSERT "
                             "ON Reservations BEGIN UPDATE ReservedTotal SET seconds = seconds + 1; END;")
            reservation.ReservationHandler()  # the restart replaces it
            now = datetime.now()
            add = "INSERT INTO Reservations (username, device_id, start_time, end_time, salt, hash) VALUES ('u', ?, ?, ?, '', '')"
            total = lambda: db.execute("SELECT seconds FROM ReservedTotal").fetchone()[0]
            db.execute(add, (ALIAS_BASE, now + timedelta(hours=1), now + timedelta(hours=2)))
            self.assertEqual(total(), 0)
            db.execute("DELETE FROM Reservations")
            self.assertEqual(total(), 0)
            db.execute(add, (1, now + timedelta(hours=1), now + timedelta(hours=2)))
            self.assertAlmostEqual(total(), 3600, delta=1)


class ReservationCountTests(unittest.TestCase):
    def test_count_seeds_from_old_rows_and_gives_back_only_a_cancel_before_start(self):
        import sqlite3
        from datetime import datetime, timedelta
        from remoteRF_server.server import reservation

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(reservation, "get_db_dir", return_value=Path(tmp)):
            db = sqlite3.connect(Path(tmp) / "reservations.db", isolation_level=None)
            now = datetime.now()
            add = "INSERT INTO Reservations (username, device_id, start_time, end_time, salt, hash) VALUES ('u', 1, ?, ?, '', '')"
            count = lambda: db.execute("SELECT n FROM ReservationCount").fetchone()[0]
            # A lab from before the counter, with one past and one future reservation.
            db.execute("CREATE TABLE Reservations (res_id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, "
                       "device_id INTEGER NOT NULL, start_time TIMESTAMP NOT NULL, end_time TIMESTAMP NOT NULL, "
                       "salt TEXT NOT NULL, hash TEXT NOT NULL)")
            db.execute(add, (now - timedelta(hours=3), now - timedelta(hours=2)))
            db.execute(add, (now + timedelta(hours=1), now + timedelta(hours=2)))
            reservation.ReservationHandler()
            self.assertEqual(count(), 2)
            reservation.ReservationHandler()  # a restart does not seed again
            self.assertEqual(count(), 2)
            db.execute(add, (now - timedelta(minutes=5), now + timedelta(minutes=5)))  # +1, active
            self.assertEqual(count(), 3)
            db.execute("DELETE FROM Reservations WHERE end_time < ?", (now,))  # purge: no change
            self.assertEqual(count(), 3)
            db.execute("DELETE FROM Reservations WHERE start_time < ?", (now,))  # cancel after start: kept
            self.assertEqual(count(), 3)
            db.execute("DELETE FROM Reservations")  # cancel before start: given back
            self.assertEqual(count(), 2)
            db.execute(add, (now + timedelta(hours=1), now + timedelta(hours=2)))
            db.execute("UPDATE ReservationCount SET n = 0")
            db.execute("DELETE FROM Reservations")
            self.assertEqual(count(), 0)  # never negative

    def test_a_partner_device_is_left_to_its_owner_to_count(self):
        import sqlite3
        from datetime import datetime, timedelta
        from remoteRF_server.server import reservation
        from remoteRF_server.server.federation.home_devices import ALIAS_BASE

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(reservation, "get_db_dir", return_value=Path(tmp)):
            db = sqlite3.connect(Path(tmp) / "reservations.db", isolation_level=None)
            now = datetime.now()
            add = "INSERT INTO Reservations (username, device_id, start_time, end_time, salt, hash) VALUES ('u', ?, ?, ?, '', '')"
            count = lambda: db.execute("SELECT n FROM ReservationCount").fetchone()[0]
            db.execute("CREATE TABLE Reservations (res_id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, "
                       "device_id INTEGER NOT NULL, start_time TIMESTAMP NOT NULL, end_time TIMESTAMP NOT NULL, "
                       "salt TEXT NOT NULL, hash TEXT NOT NULL)")
            db.execute(add, (ALIAS_BASE, now - timedelta(hours=3), now - timedelta(hours=2)))
            db.execute(add, (ALIAS_BASE - 1, now - timedelta(hours=3), now - timedelta(hours=2)))
            reservation.ReservationHandler()
            self.assertEqual(count(), 1)  # the seed skips the alias row
            db.execute(add, (ALIAS_BASE + 7, now + timedelta(hours=1), now + timedelta(hours=2)))
            self.assertEqual(count(), 1)
            db.execute("DELETE FROM Reservations WHERE device_id > ?", (ALIAS_BASE,))  # cancel before start
            self.assertEqual(count(), 1)
