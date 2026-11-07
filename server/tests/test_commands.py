"""One command table, two doors: the shell and the console prompt."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from remoteRF_server.server import commands, control
from remoteRF_server.server.commands import RestartRequested, dispatch, resolve


class FakeDaemon:
    """A control socket that records what a shell sends and answers like the server."""

    def __init__(self, path: Path):
        self.seen = []
        self.server = control.ControlServer(self._handle, path=path)

    def _handle(self, argv):
        self.seen.append(argv)
        print(f"daemon ran: {' '.join(argv)}")
        return 0

    def __enter__(self):
        self.server.start()
        return self

    def __exit__(self, *exc):
        self.server.stop()


class CommandTableTests(unittest.TestCase):
    def test_every_command_resolves_by_every_name(self):
        for command in commands.commands():
            for name in command.names:
                found, rest = resolve(name.split() + ["x"])
                self.assertIs(found, command, name)
                self.assertEqual(rest, ["x"])

    def test_singular_and_old_verbs_are_accepted(self):
        self.assertEqual(resolve(["device", "add"])[0].names[0], "devices add")
        self.assertEqual(resolve(["group", "delete"])[0].names[0], "groups remove")
        self.assertEqual(resolve(["certs", "show"])[0].names[0], "certs list")
        self.assertIsNone(resolve(["frobnicate"])[0])

    def test_help_is_two_levels_and_covers_every_visible_command(self):
        top = commands.help_text(in_daemon=True)
        self.assertLess(len(top), 20)
        for command in commands.commands():
            lines = commands.help_text(in_daemon=True, section=command.section)
            hits = [l for l in lines if l.startswith(f"  {command.names[0]} ")]
            self.assertEqual(len(hits), 0 if command.hidden else 1, command.names[0])
        self.assertNotIn("detach", "\n".join(commands.help_text(in_daemon=False, section="Server")))
        self.assertEqual(commands.section_for("device"), "Devices")
        self.assertEqual(commands.section_for("codes"), "Enrollment codes")
        self.assertIsNone(commands.section_for("nope"))


class ControlSocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "s.sock"

    def test_round_trip_and_absence(self):
        self.assertIsNone(control.call(["status"], path=self.path))
        self.assertFalse(control.running(self.path))
        with FakeDaemon(self.path) as daemon:
            self.assertTrue(control.running(self.path))
            self.assertEqual(control.call(["users", "list"], path=self.path), (0, "daemon ran: users list\n"))
            self.assertEqual(daemon.seen, [["users", "list"]])
            self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")
        self.assertFalse(self.path.exists())


class TwoDoorsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "remoterf"
        patch = mock.patch.dict(os.environ, {"REMOTERF_CONFIG_DIR": str(self.cfg), "XDG_CONFIG_HOME": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.sock = Path(self.tmp.name) / "serverrf.sock"
        patch = mock.patch.object(control, "socket_path", return_value=self.sock)
        patch.start()
        self.addCleanup(patch.stop)

    def run_shell(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = dispatch(argv, in_daemon=False)
        return rc, out.getvalue()

    def test_daemon_command_from_a_shell_goes_over_the_socket(self):
        with FakeDaemon(self.sock) as daemon:
            rc, out = self.run_shell(["users", "list"])
        self.assertEqual((rc, out), (0, "daemon ran: users list\n"))
        self.assertEqual(daemon.seen, [["users", "list"]])

    def test_reload_devices_uses_extended_control_timeout(self):
        with mock.patch.object(control, "running", return_value=True), \
                mock.patch.object(control, "call", return_value=(0, "Devices reloaded.\n")) as call:
            rc, out = self.run_shell(["reload", "devices"])
        self.assertEqual((rc, out), (0, "Devices reloaded.\n"))
        call.assert_called_once_with(["reload", "devices"], timeout=120.0)

    def test_daemon_command_without_a_server_says_so(self):
        rc, out = self.run_shell(["reservations", "list"])
        self.assertEqual(rc, 1)
        self.assertIn("not running", out)

    def test_devices_add_writes_the_inventory_and_reloads_the_running_server(self):
        with FakeDaemon(self.sock) as daemon:
            rc, out = self.run_shell(["devices", "add", "pluto", "SDR-1", "--serial", "1044abc"])
            self.assertEqual(rc, 0, out)
            rc, _ = self.run_shell(["devices", "add", "hackrf", "HackRF", "--serial", "75b0"])
            self.assertEqual(rc, 0)
        devices = yaml.safe_load((self.cfg / "devices.yml").read_text())["devices"]
        self.assertEqual(
            [(d["device_id"], d["device_type"], d["name"], d["init"]["serial"]) for d in devices],
            [(0, "adalm_pluto", "SDR-1", "1044abc"), (1, "hackrf", "HackRF", "75b0")],
        )
        self.assertEqual(daemon.seen, [["reload", "devices"], ["reload", "devices"]])
        self.assertIn("reloaded on the running server", out)

    def test_devices_add_without_a_server_just_edits_the_file(self):
        rc, out = self.run_shell(["devices", "remove", "5"])
        self.assertEqual(rc, 0)
        self.assertIn("takes effect when it starts", out)

    def test_ports_change_from_a_shell_restarts_the_running_server(self):
        with FakeDaemon(self.sock) as daemon:
            rc, out = self.run_shell(["config", "ports", "61010", "61011"])
        self.assertEqual(rc, 0, out)
        self.assertIn("GRPC_PORT=61010", (self.cfg / "server.env").read_text())
        self.assertEqual(daemon.seen, [["restart"]])

    def test_ports_change_inside_the_server_asks_for_a_restart(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RestartRequested):
                dispatch(["config", "ports", "61020", "61021"], in_daemon=True)
        self.assertIn("CERT_PORT=61021", (self.cfg / "server.env").read_text())

    def test_shell_commands_run_inside_the_console_too(self):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.output.plain_text import PlainTextOutput

        out = io.StringIO()
        with create_app_session(output=PlainTextOutput(out)), contextlib.redirect_stdout(out):
            rc = dispatch(["global", "status"], in_daemon=True)
            self.assertEqual(rc, 0)
            self.assertEqual(dispatch(["global", "clusters"], in_daemon=True), 1)
        self.assertIn("Not configured", out.getvalue())
        self.assertIn("not enabled", out.getvalue())


class NonInteractiveGroupCommandsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "remoterf"
        db = self.cfg / "db"
        db.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {
            "REMOTERF_CONFIG_DIR": str(self.cfg),
            "XDG_CONFIG_HOME": self.tmp.name,
            "XDG_DATA_HOME": str(Path(self.tmp.name) / "data"),
        })
        env.start()
        self.addCleanup(env.stop)

        from remoteRF_server.server import user_group_cli
        from remoteRF_server.server import user_group_handler as handler_module

        with mock.patch.object(handler_module, "get_db_dir", return_value=db):
            self.handler = handler_module.UserGroupHandler()
        handler = mock.patch.object(user_group_cli, "user_group_handler", self.handler)
        handler.start()
        self.addCleanup(handler.stop)

    def run_shell(self, argv):
        out = io.StringIO()
        from remoteRF_server import serverrf_cli

        stdin = SimpleNamespace(isatty=lambda: False)
        with mock.patch.object(serverrf_cli.sys, "stdin", stdin), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = dispatch(argv, in_daemon=False)
        return rc, out.getvalue()

    def create_group(self):
        return self.run_shell([
            "groups", "create", "ee123", "--devices", "7,3,7",
            "--max-reservations", "4", "--max-time", "900",
            "--horizon", "172800", "--lifetime", "86400",
            "--lan-only", "--yes", "--json",
        ])

    def test_group_create_from_flags_and_duplicate_is_refused(self):
        rc, output = self.create_group()
        self.assertEqual(rc, 0, output)
        value = json.loads(output)
        self.assertEqual(value["group_name"], "ee123")
        self.assertEqual(value["devices_whitelist"], [3, 7])
        group = self.handler.get_all_user_groups(include_users=False)[0]
        self.assertEqual(group["devices_whitelist"], [3, 7])
        self.assertEqual((group["max_reservations"], group["max_reservation_time_sec"]), (4, 900))
        self.assertEqual((group["max_horizon_sec"], group["lifetime_sec"], group["lan_only"]),
                         (172800, 86400, True))

        rc, output = self.run_shell([
            "groups", "create", "ee123", "--devices", "3", "--yes",
        ])
        self.assertEqual(rc, 1)
        self.assertEqual(output, "ERROR: group 'ee123' already exists\n")

    def test_codes_create_json_honours_group_uses_and_duration(self):
        rc, output = self.create_group()
        self.assertEqual(rc, 0, output)
        group_id = json.loads(output)["group_id"]

        rc, output = self.run_shell([
            "codes", "create", "ee123", "--count", "3", "--uses", "5",
            "--duration", "7200", "--yes", "--json",
        ])
        self.assertEqual(rc, 0, output)
        value = json.loads(output)
        self.assertEqual(value["group_id"], group_id)
        self.assertEqual((value["uses"], value["duration_sec"]), (5, 7200))
        self.assertEqual(len(value["invites"]), 3)
        rows = self.handler.get_all_enrollment_codes()
        self.assertEqual({row["group_id"] for row in rows}, {group_id})
        self.assertEqual({row["uses"] for row in rows}, {5})
        self.assertEqual({row["duration_sec"] for row in rows}, {7200})
        self.assertEqual(set(value["invites"]), {row["code_name"] for row in rows})

    def test_unknown_group_and_missing_yes_are_refused(self):
        rc, output = self.run_shell([
            "codes", "create", "missing", "--count", "3", "--yes",
        ])
        self.assertEqual(rc, 1)
        self.assertEqual(output, "ERROR: unknown group 'missing'\n")

        rc, output = self.run_shell([
            "groups", "create", "ee123", "--devices", "3",
        ])
        self.assertEqual(rc, 1)
        self.assertEqual(output, "ERROR: non-interactive group creation requires --yes\n")

        rc, output = self.run_shell([
            "codes", "create", "none", "--count", "1",
        ])
        self.assertEqual(rc, 1)
        self.assertEqual(output, "ERROR: non-interactive enrollment-code creation requires --yes\n")

    def test_help_lists_flag_forms(self):
        groups_help = "\n".join(commands.help_text(in_daemon=False, section="Groups"))
        codes_help = "\n".join(commands.help_text(in_daemon=False, section="Enrollment codes"))
        users_help = "\n".join(commands.help_text(in_daemon=False, section="Users"))
        self.assertIn("groups create <name> --devices <ids>", groups_help)
        self.assertIn("groups edit <id|name>", groups_help)
        self.assertIn("groups remove <id|name> --yes", groups_help)
        self.assertIn("codes create <group name|id|none> --count N", codes_help)
        self.assertIn("codes remove <code-or-invite> --yes", codes_help)
        self.assertIn("users manage <user> --role", users_help)

    def test_group_edit_by_name_and_json(self):
        rc, output = self.create_group()
        self.assertEqual(rc, 0, output)
        group_id = json.loads(output)["group_id"]
        with mock.patch("remoteRF_server.server.federation.cluster.share_group",
                        return_value=(["lab-b"], [])) as share:
            rc, output = self.run_shell([
                "groups", "edit", "ee123", "--name", "ee124", "--devices", "2,1,2",
                "--max-reservations", "6", "--max-time", "1200", "--lifetime", "0",
                "--horizon", "86400", "--no-lan-only", "--share", "lab-b", "--yes", "--json",
            ])
        self.assertEqual(rc, 0, output)
        value = json.loads(output)
        self.assertEqual(value["group_id"], group_id)
        self.assertEqual(value["group_name"], "ee124")
        self.assertEqual(value["devices_whitelist"], [1, 2])
        self.assertEqual(value["shared_with"], ["lab-b"])
        share.assert_called_once_with(str(group_id), ["lab-b"])
        row = self.handler.get_all_user_groups(include_users=False)[0]
        self.assertEqual((row["max_reservations"], row["max_reservation_time_sec"]), (6, 1200))
        self.assertEqual((row["max_horizon_sec"], row["lifetime_sec"], row["lan_only"]),
                         (86400, 0, False))

    def test_group_edit_errors_and_no_tty_fail_fast(self):
        rc, output = self.run_shell(["groups", "edit"])
        self.assertEqual(rc, 1)
        self.assertIn("requires a group and change flags", output)
        rc, output = self.run_shell(["groups", "edit", "missing", "--name", "new", "--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("unknown group", output)
        rc, output = self.run_shell(["groups", "edit", "missing", "--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("at least one change flag", output)
        rc, output = self.run_shell(["groups", "edit", "missing", "--name", "new"])
        self.assertEqual(rc, 1)
        self.assertEqual(output, "ERROR: non-interactive groups edit requires --yes\n")

    def test_group_remove_by_id_requires_yes(self):
        rc, output = self.create_group()
        group_id = json.loads(output)["group_id"]
        rc, output = self.run_shell(["groups", "remove", str(group_id)])
        self.assertEqual(rc, 1)
        self.assertEqual(output, "ERROR: non-interactive groups remove requires --yes\n")
        rc, output = self.run_shell(["groups", "remove", str(group_id), "--yes", "--json"])
        self.assertEqual(rc, 0, output)
        self.assertEqual(json.loads(output)["group_id"], group_id)
        self.assertEqual(self.handler.get_all_user_groups(include_users=False), [])
        rc, output = self.run_shell(["groups", "remove"])
        self.assertEqual(rc, 1)
        self.assertIn("requires a group and --yes", output)

    def test_codes_remove_one_invite_and_all_for_group(self):
        rc, output = self.create_group()
        group_id = json.loads(output)["group_id"]
        for count in (1, 2):
            rc, output = self.run_shell([
                "codes", "create", str(group_id), "--count", str(count), "--yes", "--json",
            ])
            self.assertEqual(rc, 0, output)
            invites = json.loads(output)["invites"]
            if count == 1:
                first = invites[0]
        rc, output = self.run_shell(["codes", "remove", first])
        self.assertEqual(rc, 1)
        self.assertEqual(output, "ERROR: non-interactive codes remove requires --yes\n")
        rc, output = self.run_shell(["codes", "remove", first, "--yes", "--json"])
        self.assertEqual(rc, 0, output)
        self.assertEqual(json.loads(output)["code_name"], first.rpartition("/")[2])
        rc, output = self.run_shell(["codes", "remove", "--group", str(group_id), "--yes", "--json"])
        self.assertEqual(rc, 0, output)
        self.assertEqual(len(json.loads(output)["removed"]), 2)
        self.assertEqual(self.handler.get_all_enrollment_codes(), [])

    def test_codes_remove_errors_and_no_tty_fail_fast(self):
        rc, output = self.run_shell(["codes", "remove"])
        self.assertEqual(rc, 1)
        self.assertIn("requires a code or --group", output)
        rc, output = self.run_shell(["codes", "remove", "missing", "--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", output)
        rc, output = self.run_shell(["codes", "remove", "--group", "-1", "--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("must be >= 0", output)


class NonInteractiveUsersManageTests(unittest.TestCase):
    def run_daemon(self, argv, perms):
        out = io.StringIO()
        from remoteRF_server.serverrf_cli import _cmd_users_manage_noninteractive

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = _cmd_users_manage_noninteractive(argv[2:], perms)
        return rc, out.getvalue()

    def test_users_manage_admin_json_and_user_role(self):
        perms = mock.Mock()
        perms.get_perms.return_value = [["Normal User"]]
        rc, output = self.run_daemon(
            ["users", "manage", "alice", "--role", "admin", "--yes", "--json"], perms)
        self.assertEqual(rc, 0, output)
        self.assertEqual(json.loads(output.splitlines()[-1]), {"user": "alice", "role": "Admin"})
        perms.set_admin.assert_called_once_with("alice", confirm=False, quiet=True)

        rc, output = self.run_daemon(
            ["users", "manage", "alice", "--role", "user", "--yes"], perms)
        self.assertEqual(rc, 0, output)
        perms.set_user.assert_called_once_with("alice")

    def test_users_manage_requires_yes_and_valid_existing_user_and_role(self):
        perms = mock.Mock()
        perms.get_perms.return_value = [["Normal User"]]
        rc, output = self.run_daemon(["users", "manage", "alice", "--role", "admin"], perms)
        self.assertEqual(rc, 1)
        self.assertEqual(output, "ERROR: non-interactive users manage requires --yes\n")
        rc, output = self.run_daemon(
            ["users", "manage", "alice", "--role", "owner", "--yes"], perms)
        self.assertEqual(rc, 1)
        self.assertIn("must be user, power-user, or admin", output)
        perms.get_perms.return_value = []
        rc, output = self.run_daemon(
            ["users", "manage", "missing", "--role", "user", "--yes"], perms)
        self.assertEqual(rc, 1)
        self.assertIn("does not exist", output)

    def test_users_manage_power_user_is_valid_but_needs_existing_quota_details(self):
        perms = mock.Mock()
        perms.get_perms.return_value = [["Power User"]]
        rc, output = self.run_daemon(
            ["users", "manage", "alice", "--role", "power-user", "--yes", "--json"], perms)
        self.assertEqual(rc, 0, output)
        self.assertEqual(json.loads(output), {"user": "alice", "role": "Power User"})

        perms.get_perms.return_value = [["Normal User"]]
        rc, output = self.run_daemon(
            ["users", "manage", "alice", "--role", "power-user", "--yes"], perms)
        self.assertEqual(rc, 1)
        self.assertIn("requires its device and quota details", output)

    def test_users_manage_no_tty_without_args_fails_before_console(self):
        stdin = SimpleNamespace(isatty=lambda: False)
        with mock.patch.object(commands.control, "running", return_value=True), \
                mock.patch.object(commands.sys, "stdin", stdin), \
                mock.patch("remoteRF_server.server.console.run_in_console") as console, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = dispatch(["users", "manage"], in_daemon=False)
        self.assertEqual(rc, 1)
        console.assert_not_called()
