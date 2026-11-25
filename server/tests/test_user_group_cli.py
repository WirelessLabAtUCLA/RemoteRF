# Copyright (C) 2026 RemoteRF
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import io
import unittest
from contextlib import redirect_stdout

from remoteRF_server.server.user_group_cli import edit_user_group_local


class _Session:
    def __init__(self, responses: list[str]):
        self.responses = iter(responses)
        self.prompts: list[str] = []

    def prompt(self, message: str) -> str:
        self.prompts.append(message)
        return next(self.responses)


class EditUserGroupCliTests(unittest.TestCase):
    def test_all_is_refused_and_an_explicit_list_is_required(self) -> None:
        session = _Session([
            "1",       # group ID
            "",        # group name unchanged
            "all",     # no longer an option
            "0,1",     # an explicit list
            "",        # max reservation time unchanged
            "",        # max reservations unchanged
            "",        # lifetime unchanged
            "",        # days ahead unchanged
            "",        # LAN only unchanged
            "",        # blacklisted tags unchanged
            "n",       # do not persist the edit
        ])
        output = io.StringIO()
        with redirect_stdout(output):
            edit_user_group_local(session)
        self.assertIn("List the device ids this group may use", output.getvalue())
        self.assertIn("devices_whitelist       : 0,1", output.getvalue())

    def test_enter_leaves_the_whitelist_unchanged(self) -> None:
        session = _Session(["1", "renamed", "", "", "", "", "", "", "", "n"])
        output = io.StringIO()
        with redirect_stdout(output):
            edit_user_group_local(session)
        self.assertIn("devices_whitelist       : (unchanged)", output.getvalue())


class MakeEnrollmentCodesCliTests(unittest.TestCase):
    def test_declining_a_group_makes_account_only_codes(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        from remoteRF_server.server import user_group_handler as m
        from remoteRF_server.server.user_group_cli import make_enrollment_codes_local

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(m, "get_db_dir", return_value=Path(tmp.name)):
            h = m.UserGroupHandler()
            h.create_user_group(group_name="dev", devices_whitelist=[1], max_reservation_time_sec=600,
                                max_reservations=1, lifetime_sec=0)
            session = _Session(["1", "n", "", "y", "", "", "y"])  # count, no group, chars, use them, uses, duration, go
            output = io.StringIO()
            with mock.patch("remoteRF_server.server.user_group_cli.user_group_handler", h), redirect_stdout(output):
                make_enrollment_codes_local(session)
            (code,) = h.get_all_enrollment_codes()
        self.assertIn("none (account only)", output.getvalue())
        self.assertEqual((code["group_id"], code["cluster_name"]), (-1, None))


class WhitelistSemanticsTests(unittest.TestCase):
    """An empty whitelist grants every device, including ones added later."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        from remoteRF_server.server import user_group_handler as m

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = mock.patch.object(m, "get_db_dir", return_value=Path(tmp.name))
        root.start()
        self.addCleanup(root.stop)
        self.m = m
        self.h = m.UserGroupHandler()
        self.gid = int(self.h.create_user_group(
            group_name="dev", devices_whitelist=[], max_reservation_time_sec=600,
            max_reservations=1, lifetime_sec=0,
        ))
        self.h.create_enrollment_codes(
            code_names=["T1"], group_id=self.gid, uses=1, duration_sec=0
        )
        self.assertTrue(self.h.enroll_user_with_code(uid=7, code_name="T1"))

    def test_no_horizon_limit_in_one_group_lifts_it(self) -> None:
        self.assertEqual(self.h.get_users_max_horizon(uid=7, device_id=3), (True, 7 * 86400))
        self.assertTrue(self.h.edit_user_group(group_id=self.gid, max_horizon_sec=2 * 86400))
        self.assertEqual(self.h.get_users_max_horizon(uid=7, device_id=3), (True, 2 * 86400))
        open_ended = int(self.h.create_user_group(
            group_name="open", devices_whitelist=[3], max_reservation_time_sec=600,
            max_reservations=1, lifetime_sec=0, max_horizon_sec=0,
        ))
        self.h.create_enrollment_codes(code_names=["T2"], group_id=open_ended, uses=1, duration_sec=0)
        self.assertTrue(self.h.enroll_user_with_code(uid=7, code_name="T2"))
        self.assertEqual(self.h.get_users_max_horizon(uid=7, device_id=3), (True, 0))
        self.assertEqual(self.h.get_users_max_horizon(uid=7, device_id=4), (True, 2 * 86400))

    def test_account_only_code_creates_an_account_in_no_group(self) -> None:
        import sqlite3
        self.assertEqual(self.h.create_enrollment_codes(code_names=["ACC1"], group_id=-1, uses=1, duration_sec=0),
                         "Created 1/1 enrollment codes.")
        self.assertTrue(self.h.validate_enrollment_code(code_name="ACC1"))
        self.assertTrue(self.h.code_is_account_only(code_name="ACC1"))
        self.assertFalse(self.h.code_is_account_only(code_name="T1"))
        self.assertTrue(self.h.enroll_user_with_code(uid=9, code_name="ACC1"))
        self.assertEqual(self.h.get_groups_user_in(uid=9), [])
        self.assertFalse(self.h.validate_enrollment_code(code_name="ACC1"))  # single use, consumed
        # A cluster code is group -1 too, but it is redeemed by the cluster module, never here.
        with sqlite3.connect(self.h.filepath) as conn:
            conn.execute("INSERT INTO EnrollmentCodes(code_name,group_id,uses,duration_sec,created_at,cluster_name) "
                         "VALUES ('cluster.X',-1,1,0,'2026-01-01 00:00:00','lab')")
        self.assertFalse(self.h.enroll_user_with_code(uid=9, code_name="cluster.X"))
        self.assertFalse(self.h.code_is_account_only(code_name="cluster.X"))

    def test_empty_whitelist_is_unrestricted(self) -> None:
        from unittest import mock

        self.assertTrue(self.m.whitelist_allows([], 16))
        self.assertFalse(self.m.whitelist_allows([3], 16))
        with mock.patch(
            "remoteRF_server.server.device_manager.get_all_devices_str",
            return_value={16: "pluto", 4: "rtl"},
        ):
            self.assertEqual(self.h.get_users_devices(uid=7), [4, 16])
        self.assertEqual(self.h.get_users_max_reservation_time(uid=7, device_id=16), (True, 600))
        self.assertEqual(self.h.get_users_max_reservations(uid=7, device_id=999), (True, 1))
        self.assertEqual(
            self.h.get_group_quota_for_device(uid=7, device_id=16, reserved_device_ids=[4])[:3],
            (True, 1, 1),
        )

    def test_a_listed_whitelist_still_restricts(self) -> None:
        self.h.edit_user_group(group_id=self.gid, devices_whitelist=[4])
        self.assertEqual(self.h.get_users_devices(uid=7), [4])
        self.assertEqual(self.h.get_users_max_reservation_time(uid=7, device_id=16), (False, 0))


class LanOnlyGroupTests(unittest.TestCase):
    """A LAN-only group does not exist to a call that came through the relay."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        from remoteRF_server.server import user_group_handler as m

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = mock.patch.object(m, "get_db_dir", return_value=Path(tmp.name))
        root.start()
        self.addCleanup(root.stop)
        self.m = m
        self.h = m.UserGroupHandler()
        self.gid = int(self.h.create_user_group(
            group_name="lab-only", devices_whitelist=[16], max_reservation_time_sec=600,
            max_reservations=1, lifetime_sec=0, lan_only=True,
        ))
        self.h.create_enrollment_codes(code_names=["LAN1"], group_id=self.gid, uses=5, duration_sec=0)

    def relayed(self):
        from remoteRF_server.server.global_onboarding import RELAYED

        class _Ctx:
            def __enter__(_self):
                _self.token = RELAYED.set(True)
            def __exit__(_self, *exc):
                RELAYED.reset(_self.token)
        return _Ctx()

    def test_codes_and_membership_only_work_on_the_lan(self) -> None:
        h = self.h
        self.assertTrue(h.code_is_lan_only(code_name="LAN1"))
        with self.relayed():
            self.assertFalse(h.validate_enrollment_code(code_name="LAN1"))
            self.assertFalse(h.enroll_user_with_code(uid=7, code_name="LAN1"))
        self.assertTrue(h.validate_enrollment_code(code_name="LAN1"))
        self.assertTrue(h.enroll_user_with_code(uid=7, code_name="LAN1"))
        self.assertEqual(h.get_users_devices(uid=7), [16])
        self.assertTrue(h.user_is_lan_only(uid=7))
        with self.relayed():
            self.assertEqual(h.get_users_devices(uid=7), [])
            self.assertEqual(h.get_users_max_reservation_time(uid=7, device_id=16), (False, 0))
        self.assertTrue(h.edit_user_group(group_id=self.gid, lan_only=False))
        self.assertFalse(h.user_is_lan_only(uid=7))
        with self.relayed():
            self.assertEqual(h.get_users_devices(uid=7), [16])
        (row,) = [g for g in h.get_all_user_groups(include_users=False) if g["group_id"] == self.gid]
        self.assertEqual(row["lan_only"], False)

    def test_relay_peers_are_the_tunnel_target(self) -> None:
        from unittest import mock

        from remoteRF_server.server import global_onboarding as g

        with mock.patch.object(g, "local_bind_host", return_value="164.67.195.210"):
            self.assertTrue(g.is_relayed("ipv4:164.67.195.210:41234"))
            self.assertTrue(g.is_relayed("ipv6:[::ffff:164.67.195.210]:41234"))
            self.assertTrue(g.is_relayed("ipv4:127.0.0.1:5"))
            self.assertFalse(g.is_relayed("ipv4:164.67.195.77:41234"))
            self.assertFalse(g.is_relayed("ipv6:[2607:f010::1]:41234"))


class AdminGroupTests(unittest.TestCase):
    """The built-in admin group always exists and covers every device."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        from remoteRF_server.server import user_group_handler as m

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = mock.patch.object(m, "get_db_dir", return_value=Path(tmp.name))
        root.start()
        self.addCleanup(root.stop)
        self.m = m
        self.db = Path(tmp.name) / "usergroups.db"
        self.h = m.UserGroupHandler()

    def admin(self) -> dict:
        (row,) = [g for g in self.h.get_all_user_groups(include_users=False) if g["group_name"] == "admin"]
        return row

    def sql(self, statement: str) -> None:
        import sqlite3

        with sqlite3.connect(self.db) as conn:
            conn.execute(statement)

    def test_seeded_once_with_defaults_and_restored(self) -> None:
        self.m.UserGroupHandler()
        row = self.admin()
        self.assertEqual(
            (row["devices_whitelist"], row["max_reservation_time_sec"], row["max_reservations"],
             row["lifetime_sec"], row["lan_only"]),
            ([], 3600, 999, 0, False),
        )
        self.assertEqual(self.h.get_all_enrollment_codes(), [])
        self.sql("DELETE FROM UserGroups WHERE group_name='admin'")
        self.m.UserGroupHandler()
        self.assertEqual(self.admin()["max_reservations"], 999)

    def test_a_labs_own_narrow_admin_group_is_renamed_not_widened(self) -> None:
        old = self.admin()["group_id"]
        self.sql(f"UPDATE UserGroups SET devices_whitelist='[3]', lifetime_sec=60 WHERE group_id={old}")
        self.sql(f"INSERT INTO UsersLookup(user_id, group_id) VALUES (7, {old})")
        self.m.UserGroupHandler()
        groups = {g["group_name"]: g for g in self.h.get_all_user_groups(include_users=True)}
        self.assertEqual((groups[f"admin-old-{old}"]["devices_whitelist"], groups[f"admin-old-{old}"]["users"]),
                         ([3], [7]))
        self.assertEqual((groups["admin"]["devices_whitelist"], groups["admin"]["users"]), ([], []))
        self.assertNotEqual(groups["admin"]["group_id"], old)

    def test_code_enrolls_and_covers_devices_added_later(self) -> None:
        from unittest import mock

        gid = self.admin()["group_id"]
        self.h.create_enrollment_codes(code_names=["A1"], group_id=gid, uses=1, duration_sec=0)
        self.assertTrue(self.h.enroll_user_with_code(uid=7, code_name="A1"))
        with mock.patch("remoteRF_server.server.device_manager.get_all_devices_str",
                        return_value={0: "a", 42: "new"}):
            self.assertEqual(self.h.get_users_devices(uid=7), [0, 42])
        self.assertEqual(self.h.get_users_max_reservation_time(uid=7, device_id=42), (True, 3600))
        self.assertEqual(
            self.h.get_group_quota_for_device(uid=7, device_id=42, reserved_device_ids=[42])[:3],
            (True, 1, 999),
        )

    def test_cannot_be_deleted_renamed_narrowed_or_expired(self) -> None:
        gid = self.admin()["group_id"]
        self.assertFalse(self.h.delete_user_group(group_id=gid))
        self.assertFalse(self.h.edit_user_group(group_id=gid, group_name="staff"))
        self.assertFalse(self.h.edit_user_group(group_id=gid, devices_whitelist=[4]))
        self.assertFalse(self.h.edit_user_group(group_id=gid, lifetime_sec=60))
        self.assertEqual((self.admin()["group_name"], self.admin()["devices_whitelist"]), ("admin", []))
        self.assertTrue(self.h.edit_user_group(group_id=gid, max_reservation_time_sec=7200, max_reservations=5))
        self.assertEqual((self.admin()["max_reservation_time_sec"], self.admin()["max_reservations"]), (7200, 5))
        self.m.UserGroupHandler()  # a restart keeps the edited limits
        self.assertEqual(self.admin()["max_reservations"], 5)


class FunctionTagBlacklistGroupTests(unittest.TestCase):
    """Per-group blacklisted_tags storage, migration, CLI, and multi-group intersection."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        from remoteRF_server.server import user_group_handler as m

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_dir = Path(tmp.name)
        root = mock.patch.object(m, "get_db_dir", return_value=self.db_dir)
        root.start()
        self.addCleanup(root.stop)
        self.m = m
        self.h = m.UserGroupHandler()

    def _enroll(self, uid: int, group_id: int, code: str) -> None:
        self.h.create_enrollment_codes(code_names=[code], group_id=group_id, uses=5, duration_sec=0)
        self.assertTrue(self.h.enroll_user_with_code(uid=uid, code_name=code))

    def test_storage_edit_admin_refusal_list_export_and_interactive_cli(self) -> None:
        import csv
        from unittest import mock

        from remoteRF_server.server import user_group_cli

        gid = int(self.h.create_user_group(
            group_name="rx-only",
            devices_whitelist=[1, 2],
            max_reservation_time_sec=600,
            max_reservations=1,
            lifetime_sec=0,
            blacklisted_tags=["TX", "tx", "Cal"],
        ))
        (row,) = [g for g in self.h.get_all_user_groups(include_users=False) if g["group_id"] == gid]
        self.assertEqual(row["blacklisted_tags"], ["TX", "Cal"])

        # Built-in admin group refuses non-empty tag blacklists.
        (admin_row,) = [g for g in self.h.get_all_user_groups(include_users=False) if g["group_name"] == "admin"]
        self.assertFalse(self.h.edit_user_group(group_id=admin_row["group_id"], blacklisted_tags=["TX"]))

        # Edit and clear tags.
        self.assertTrue(self.h.edit_user_group(group_id=gid, blacklisted_tags=["RF"]))
        (row,) = [g for g in self.h.get_all_user_groups(include_users=False) if g["group_id"] == gid]
        self.assertEqual(row["blacklisted_tags"], ["RF"])
        self.assertTrue(self.h.edit_user_group(group_id=gid, blacklisted_tags=[]))
        (row,) = [g for g in self.h.get_all_user_groups(include_users=False) if g["group_id"] == gid]
        self.assertEqual(row["blacklisted_tags"], [])

        # Interactive create, edit, list, and CSV export.
        out = io.StringIO()
        with mock.patch.object(user_group_cli, "user_group_handler", self.h), \
                mock.patch.object(user_group_cli, "_clusters_here", return_value=[]), \
                mock.patch.object(user_group_cli, "_print_device_choices"), \
                mock.patch.object(user_group_cli, "_resolve_download_dir", return_value=self.db_dir), \
                mock.patch("remoteRF_server.server.federation.cluster.shared_with", return_value=[]), \
                redirect_stdout(out):
            create_session = _Session([
                "class-a", "y", "0,1", "900", "2", "0", "7", "TX, FOO", "y", "n", "y",
            ])
            user_group_cli.make_user_group_local(create_session)
            (created,) = [g for g in self.h.get_all_user_groups(include_users=False) if g["group_name"] == "class-a"]
            self.assertEqual(created["blacklisted_tags"], ["TX", "FOO"])

            user_group_cli.list_user_groups_local(_Session([]))
            self.assertIn("blacklisted_tags", out.getvalue())
            self.assertIn("TX, FOO", out.getvalue())

            csv_path = user_group_cli.export_user_groups_csv_local()
            self.assertIsNotNone(csv_path)

            edit_session = _Session([
                str(created["group_id"]), "", "", "", "", "", "", "", "none", "y",
            ])
            user_group_cli.edit_user_group_local(edit_session)

        (after_clear,) = [g for g in self.h.get_all_user_groups(include_users=False) if g["group_name"] == "class-a"]
        self.assertEqual(after_clear["blacklisted_tags"], [])

        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        by_name = {r["group_name"]: r for r in rows}
        self.assertEqual(by_name["class-a"]["blacklisted_tags"], '["TX","FOO"]')

    def test_migration_adds_blacklisted_tags_to_old_database(self) -> None:
        import sqlite3
        import tempfile
        from pathlib import Path
        from unittest import mock

        from remoteRF_server.server.federation.migrations import migrate_gate_f_usergroups_schema

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_dir = Path(tmp.name)
        db_path = db_dir / "usergroups.db"
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE UserGroups (
                    group_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL UNIQUE,
                    devices_whitelist TEXT NOT NULL DEFAULT '[]',
                    max_reservation_time_sec INTEGER NOT NULL,
                    max_reservations INTEGER NOT NULL,
                    lifetime_sec INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE UsersLookup (
                    user_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    PRIMARY KEY(user_id, group_id)
                );
                CREATE TABLE EnrollmentCodes (
                    code_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code_name TEXT NOT NULL UNIQUE,
                    group_id INTEGER NOT NULL,
                    uses INTEGER NOT NULL,
                    duration_sec INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO UserGroups (group_name, devices_whitelist, max_reservation_time_sec, max_reservations, lifetime_sec)
                VALUES ('legacy', '[0]', 600, 1, 0);
                """
            )
            migrate_gate_f_usergroups_schema(conn)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(UserGroups)")}
            self.assertIn("blacklisted_tags", cols)

        with mock.patch.object(self.m, "get_db_dir", return_value=db_dir):
            migrated = self.m.UserGroupHandler()
            (legacy,) = [g for g in migrated.get_all_user_groups(include_users=False) if g["group_name"] == "legacy"]
            self.assertEqual(legacy["blacklisted_tags"], [])
            self.assertTrue(migrated.edit_user_group(group_id=legacy["group_id"], blacklisted_tags=["TX"]))
            (updated,) = [g for g in migrated.get_all_user_groups(include_users=False) if g["group_name"] == "legacy"]
            self.assertEqual(updated["blacklisted_tags"], ["TX"])

    def test_effective_blocked_tags_multi_group_rules_and_admin_bypass(self) -> None:
        g_rx = int(self.h.create_user_group(
            group_name="rx", devices_whitelist=[1], max_reservation_time_sec=600,
            max_reservations=1, lifetime_sec=0, blacklisted_tags=["TX", "Cal"],
        ))
        g_tx_other = int(self.h.create_user_group(
            group_name="tx-other-dev", devices_whitelist=[2], max_reservation_time_sec=600,
            max_reservations=1, lifetime_sec=0, blacklisted_tags=[],
        ))
        g_rx2 = int(self.h.create_user_group(
            group_name="rx2", devices_whitelist=[1, 2], max_reservation_time_sec=600,
            max_reservations=1, lifetime_sec=0, blacklisted_tags=["tx", "Diag"],
        ))
        g_open = int(self.h.create_user_group(
            group_name="open", devices_whitelist=[1], max_reservation_time_sec=600,
            max_reservations=1, lifetime_sec=0, blacklisted_tags=[],
        ))

        # No access-granting group -> no access.
        self.assertEqual(self.h.get_users_blocked_tags(uid=50, device_id=1), (False, []))

        # Single granting group blacklists TX and Cal.
        self._enroll(50, g_rx, "C_RX")
        self.assertEqual(self.h.get_users_blocked_tags(uid=50, device_id=1), (True, ["TX", "Cal"]))
        self.assertEqual(self.h.get_users_blocked_tags(uid=50, device_id=2), (False, []))

        # Adding a group for a DIFFERENT device does not lift TX on device 1.
        self._enroll(50, g_tx_other, "C_OTHER")
        self.assertEqual(self.h.get_users_blocked_tags(uid=50, device_id=1), (True, ["TX", "Cal"]))
        self.assertEqual(self.h.get_users_blocked_tags(uid=50, device_id=2), (True, []))

        # Adding a second granting group that also blacklists tx (different case) -> intersection is ["TX"].
        self._enroll(50, g_rx2, "C_RX2")
        self.assertEqual(self.h.get_users_blocked_tags(uid=50, device_id=1), (True, ["TX"]))
        self.assertEqual(self.h.get_users_blocked_tags(uid=50, device_id=2), (True, []))

        # Adding a granting group with no TX blacklist -> non-blacklist wins.
        self._enroll(50, g_open, "C_OPEN")
        self.assertEqual(self.h.get_users_blocked_tags(uid=50, device_id=1), (True, []))

        # Built-in admin group never blocks tags.
        (admin_row,) = [g for g in self.h.get_all_user_groups(include_users=False) if g["group_name"] == "admin"]
        self._enroll(60, g_rx, "C_RX_ADMIN")
        self.assertEqual(self.h.get_users_blocked_tags(uid=60, device_id=1), (True, ["TX", "Cal"]))
        self._enroll(60, admin_row["group_id"], "C_ADMIN")
        self.assertEqual(self.h.get_users_blocked_tags(uid=60, device_id=1), (True, []))


if __name__ == "__main__":
    unittest.main()
