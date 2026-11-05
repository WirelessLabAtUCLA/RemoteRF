"""Phase 0-3 RemoteRF Global participation: consent, request, relay, report."""

import builtins
import unittest
from unittest import mock

from remoteRF_server.server import global_onboarding as g


class ConsentTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(
            "os.environ",
            {
                "REMOTERF_CONFIG_DIR": self.tmp.name + "/config",
                "REMOTERF_FEDERATION_IDENTITY_DIR": self.tmp.name + "/identity",
                "REMOTERF_GLOBAL_DOMAIN": "global.remoterf.net",
                # Never the real broker: an unmocked call must fail fast here
                # rather than submit a deployment request to production.
                "REMOTERF_GLOBAL_ORIGIN": "https://127.0.0.1:9",
                # Hermetic: no LAN-address probe, and the loopback forward
                # the tunnel tests expect.
                "REMOTERF_BIND_HOST": "0.0.0.0",
            },
        )
        patch.start()
        self.addCleanup(patch.stop)
        for field in ("CONSENT", "NAME", "DISPLAY_NAME", "EMAIL"):
            g.os.environ.pop(f"REMOTERF_GLOBAL_{field}", None)  # restored by patch.stop
        # The prompts are only asked at a terminal; these tests answer them.
        tty = mock.patch.object(g.sys, "stdin", mock.Mock(isatty=mock.Mock(return_value=True)))
        tty.start()
        self.addCleanup(tty.stop)
        self.setUpSubmitStub()

    def answer(self, *replies):
        return mock.patch.object(builtins, "input", mock.Mock(side_effect=replies))

    def setUpSubmitStub(self):
        """Consent tests exercise the prompts; submission has its own tests."""
        patch = mock.patch.object(g, "submit_and_report", return_value=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_declining_is_a_durable_answer(self):
        with self.answer("n"):
            self.assertTrue(g.ensure_consent())
        self.assertEqual(g.load()["GLOBAL_CONSENT"], "no")
        self.assertFalse(g.participating())
        # Asked once: a second start must not prompt at all.
        with mock.patch.object(builtins, "input", mock.Mock(side_effect=AssertionError)):
            self.assertTrue(g.ensure_consent())

    def test_a_start_without_a_terminal_is_not_asked(self):
        # A service start must not refuse to run on an unanswered question.
        g.sys.stdin.isatty.return_value = False
        with mock.patch.object(builtins, "input", mock.Mock(side_effect=AssertionError)), \
                mock.patch("builtins.print") as printed:
            self.assertTrue(g.ensure_consent())
        self.assertNotIn("GLOBAL_CONSENT", g.load())
        self.assertFalse(g.participating())
        # One line, and it says how to answer without a terminal.
        printed.assert_called_once()
        line = printed.call_args[0][0]
        for needed in ("REMOTERF_GLOBAL_CONSENT=yes", "REMOTERF_GLOBAL_NAME", "REMOTERF_GLOBAL_EMAIL",
                       "server.env", "serverrf global join --yes --name <name> --email <email>"):
            self.assertIn(needed, line)

    def test_consent_text_allows_joining_after_opting_out(self):
        text = " ".join(g.CONSENT_TEXT.split())
        self.assertNotIn("cannot opt back in", text)
        self.assertIn("If you opt out, you can still join later", text)
        self.assertIn("serverrf global join", text)

    def test_joining_writes_configuration_and_identity(self):
        with mock.patch.object(g, "name_available", return_value=True):
            with self.answer("y", "UCLA", "UCLA Wireless Lab", "lab@ucla.edu"):
                self.assertTrue(g.ensure_consent())
        kv = g.load()
        self.assertEqual(kv["GLOBAL_CONSENT"], "yes")
        self.assertEqual(kv["GLOBAL_NAME"], "ucla")
        self.assertEqual(kv["GLOBAL_DISPLAY_NAME"], "UCLA Wireless Lab")
        self.assertEqual(kv["GLOBAL_EMAIL"], "lab@ucla.edu")
        self.assertEqual(kv["GLOBAL_STATE"], "unregistered")
        self.assertEqual(g.hostname(), "ucla.global.remoterf.net")
        self.assertTrue(g.relay_key_path().exists())
        self.assertTrue((g.identity_dir() / "deployment-private.pem").exists())
        self.assertTrue(g.relay_public_key().startswith("ssh-ed25519 "))

    def test_taken_name_is_reprompted(self):
        with mock.patch.object(g, "name_available", side_effect=[False, True]):
            with self.answer("y", "ucla", "ucla2", "", "lab@ucla.edu"):
                self.assertTrue(g.ensure_consent())
        self.assertEqual(g.load()["GLOBAL_NAME"], "ucla2")

    def test_unreachable_global_accepts_the_name_provisionally(self):
        with mock.patch.object(g, "name_available", return_value=None):
            with self.answer("y", "ucla", "", "lab@ucla.edu"):
                self.assertTrue(g.ensure_consent())
        self.assertEqual(g.load()["GLOBAL_NAME"], "ucla")

    def test_invalid_slug_and_email_are_rejected(self):
        with mock.patch.object(g, "name_available", return_value=True):
            with self.answer("y", "-bad-", "ok", "ucla", "", "nope", "lab@ucla.edu"):
                self.assertTrue(g.ensure_consent())
        self.assertEqual(g.load()["GLOBAL_NAME"], "ucla")
        self.assertEqual(g.load()["GLOBAL_EMAIL"], "lab@ucla.edu")

    def test_leave_is_local_and_immediate(self):
        with mock.patch.object(g, "name_available", return_value=True):
            with self.answer("y", "ucla", "", "lab@ucla.edu"):
                g.ensure_consent()
        self.assertEqual(g.main(["leave"]), 0)
        self.assertEqual(g.state(), "left")

    def test_status_and_usage(self):
        self.assertEqual(g.main(["status"]), 0)
        self.assertEqual(g.main(["bogus"]), 2)
        self.assertEqual(g.main([]), 2)




class RequestTests(ConsentTests):
    """Phase 1: the signed request, and what the Server records from /me."""

    def join(self, **overrides):
        with mock.patch.object(g, "name_available", return_value=True):
            with self.answer("y", "ucla", "UCLA Wireless Lab", "lab@ucla.edu"):
                g.ensure_consent()
        g.update(**overrides)

    def capture(self, reply):
        """Run one signed call against a stub broker; return what it received."""
        seen = {}

        class Response:
            status_code = 200
            content = b"{}"

            @staticmethod
            def json():
                return reply

        def request(method, url, data=None, headers=None, **kwargs):
            seen.update(method=method, url=url, data=data, headers=headers)
            return Response()

        return seen, mock.patch("requests.request", side_effect=request)

    def test_request_is_signed_and_bound_to_method_path_and_body(self):
        import hashlib
        import json

        import jwt

        self.join()
        seen, patched = self.capture(
            {"status": "requested", "deployment_id": "8a2e3f10-0000-4000-8000-000000000001",
             "name": None, "relay": None}
        )
        with patched:
            g.submit_request()

        self.assertEqual(seen["method"], "POST")
        self.assertTrue(seen["url"].endswith("/v2/broker/deployments/request"))
        body = json.loads(seen["data"])
        self.assertEqual(body["name"], "ucla")
        # An admin reviews the request by its human name, not its slug.
        self.assertEqual(body["display_name"], "UCLA Wireless Lab")
        self.assertEqual(body["email"], "lab@ucla.edu")
        self.assertEqual(body["public_key"], g._public_key_b64())
        self.assertTrue(body["ssh_public_key"].startswith("ssh-ed25519 "))

        token = seen["headers"]["Authorization"].removeprefix("Bearer ")
        header = jwt.get_unverified_header(token)
        self.assertEqual(header["typ"], "remoterf-broker-request+jwt")
        claims = jwt.decode(token, options={"verify_signature": False})
        self.assertEqual(claims["method"], "POST")
        self.assertEqual(claims["path"], "/v2/broker/deployments/request")
        self.assertEqual(
            claims["body_sha256"], hashlib.sha256(seen["data"]).hexdigest()
        )
        # Verifiable against the very key the body carries.
        import base64

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        jwt.decode(
            token,
            Ed25519PublicKey.from_public_bytes(base64.b64decode(body["public_key"])),
            algorithms=["EdDSA"],
            options={"verify_aud": False},
        )
        self.assertEqual(g.state(), "requested")

    def test_the_final_name_comes_from_global_not_the_request(self):
        self.join()
        _, patched = self.capture(
            {
                "status": "active",
                "name": "ucla-two",
                "deployment_id": "8a2e3f10-0000-4000-8000-000000000001",
                "relay": {
                    "hostname": "ucla-two.global.remoterf.net",
                    "tunnel_host": "global.remoterf.net",
                    "tunnel_port": 2222,
                    "tunnel_user": "remoterf-relay",
                    "tunnel_host_key": "ssh-ed25519 AAAAhost",
                    "grpc_port": 62005,
                    "cert_port": 62006,
                },
            }
        )
        with patched:
            g.fetch_me()
        self.assertEqual(g.state(), "active")
        self.assertEqual(g.hostname(), "ucla-two.global.remoterf.net")
        self.assertEqual(g.load()["GLOBAL_RELAY_GRPC_PORT"], "62005")
        self.assertEqual(g.load()["GLOBAL_RELAY_HOST_KEY"], "ssh-ed25519 AAAAhost")

    def test_join_writes_deployment_state(self):
        from remoteRF_server.server.federation.identity import load_state

        self.join()
        _, patched = self.capture(
            {"status": "requested", "deployment_id": "8a2e3f10-0000-4000-8000-000000000001",
             "name": None, "relay": None}
        )
        with patched:
            g.submit_request()
        state = load_state(g.identity_dir())
        self.assertTrue(state.enrolled)
        self.assertEqual(state.deployment_id, "8a2e3f10-0000-4000-8000-000000000001")
        self.assertEqual(state.display_name, "UCLA Wireless Lab")
        self.assertEqual(state.protocol_version, "2")
        enrolled_at = state.enrolled_at
        self.assertRegex(enrolled_at, r"^\d{4}-\d{2}-\d{2}T")
        # Later /me polls keep the first enrollment time.
        _, patched = self.capture(
            {"status": "active", "deployment_id": "8a2e3f10-0000-4000-8000-000000000001",
             "name": "ucla", "relay": None}
        )
        with patched:
            g.fetch_me()
        self.assertEqual(load_state(g.identity_dir()).enrolled_at, enrolled_at)
        self.assertEqual(g.state(), "active")

    def test_apply_me_refuses_a_changed_deployment_id(self):
        from remoteRF_server.server.federation.identity import load_state

        self.join()
        first = "8a2e3f10-0000-4000-8000-000000000001"
        g._apply_me({"status": "requested", "deployment_id": first, "name": None, "relay": None})
        before = (load_state(g.identity_dir()).to_dict(), dict(g.load()))
        with self.assertRaises(g.GlobalError) as caught:
            g._apply_me(
                {"status": "active", "deployment_id": "8a2e3f10-0000-4000-8000-000000000002",
                 "name": "ucla", "relay": None}
            )
        self.assertEqual(caught.exception.code, "deployment_id_changed")
        self.assertEqual((load_state(g.identity_dir()).to_dict(), dict(g.load())), before)

    def test_a_refusal_surfaces_its_code(self):
        self.join()

        class Response:
            status_code = 409
            content = b"x"

            @staticmethod
            def json():
                return {"error": {"code": "name_taken", "message": "taken"}}

        with mock.patch("requests.request", return_value=Response()):
            with self.assertRaises(g.GlobalError) as caught:
                g.submit_request()
        self.assertEqual(caught.exception.code, "name_taken")

    def test_the_agent_does_not_run_for_a_server_that_declined(self):
        with self.answer("n"):
            g.ensure_consent()
        self.assertIsNone(g.start_agent())

    def test_the_agent_stops_on_a_terminal_state(self):
        self.join(GLOBAL_STATE="rejected")
        self.assertIsNone(g.start_agent())
        # ...and an already-running loop returns rather than polling forever.
        with mock.patch.object(g, "_tick", side_effect=AssertionError("polled")):
            g.run_agent(stop=None)

    def test_the_agent_submits_once_then_polls(self):
        self.join()
        calls = []
        with mock.patch.object(g, "submit_request", side_effect=lambda: calls.append("submit")):
            g._tick({})
        self.assertEqual(calls, ["submit"])

        g.update(GLOBAL_STATE="requested")
        last = {}
        with mock.patch.object(g, "fetch_me", side_effect=lambda: calls.append("poll")):
            g._tick(last)
            g._tick(last)  # inside the interval: no second call
        self.assertEqual(calls, ["submit", "poll"])

class TunnelTests(RequestTests):
    """Phase 2: the reverse tunnel as a supervised child, not a boot unit."""

    ASSIGNMENT = {
        "status": "active",
        "name": "ucla",
        "deployment_id": "8a2e3f10-0000-4000-8000-000000000001",
        "relay": {
            "hostname": "ucla.global.remoterf.net",
            "tunnel_host": "global.remoterf.net",
            "tunnel_port": 2222,
            "tunnel_user": "remoterf-relay",
            "tunnel_host_key": "ssh-ed25519 AAAAhostkey",
            "grpc_port": 62005,
            "cert_port": 62006,
        },
    }

    def activate(self):
        self.join()
        g.write_env_kv(g.cfg_dir() / "server.env", {"GRPC_PORT": "61005", "CERT_PORT": "61006"})
        _, patched = self.capture(self.ASSIGNMENT)
        with patched:
            g.fetch_me()

    def test_no_tunnel_before_approval(self):
        self.join()
        self.assertIsNone(g.tunnel_command())
        Tunnel = g.Tunnel()
        with mock.patch.object(g.subprocess, "Popen", side_effect=AssertionError("started")):
            Tunnel.supervise(0.0)

    def test_tunnel_command_pins_the_host_key_and_maps_both_planes(self):
        self.activate()
        command = g.tunnel_command()
        self.assertIn("-NT", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("ExitOnForwardFailure=yes", command)
        self.assertIn("127.0.0.1:62005:127.0.0.1:61005", command)
        self.assertIn("127.0.0.1:62006:127.0.0.1:61006", command)
        self.assertIn("remoterf-relay@global.remoterf.net", command)
        self.assertIn(str(g.relay_key_path()), command)
        self.assertEqual(
            g.known_hosts_path().read_text().strip(),
            "[global.remoterf.net]:2222 ssh-ed25519 AAAAhostkey",
        )

    def test_the_tunnel_forwards_to_whatever_the_server_bound(self):
        """A Server bound to its LAN address is not reachable on loopback, so
        the reverse forward has to name the address it actually bound."""
        self.activate()
        with mock.patch.dict("os.environ", {"REMOTERF_BIND_HOST": "192.168.1.20"}):
            self.assertIn("127.0.0.1:62005:192.168.1.20:61005", g.tunnel_command())
        for wildcard in ("0.0.0.0", "::"):
            with mock.patch.dict("os.environ", {"REMOTERF_BIND_HOST": wildcard}):
                self.assertIn("127.0.0.1:62005:127.0.0.1:61005", g.tunnel_command())
        # Unset is not loopback: the Server then binds its detected LAN address.
        with mock.patch.dict("os.environ", {"REMOTERF_BIND_HOST": ""}), \
                mock.patch.object(g, "detected_lan_ip", return_value="10.0.0.7"):
            self.assertIn("127.0.0.1:62005:10.0.0.7:61005", g.tunnel_command())

    def test_server_exit_stops_the_tunnel_child(self):
        """The agent thread is a daemon; its ssh child must not outlive the
        server and squat on the relay ports."""
        self.activate()
        with mock.patch.object(g.atexit, "register") as register, \
                mock.patch.object(g, "run_agent"):
            stop = g.start_agent()
        (hook, stop_arg, tunnel), _ = register.call_args
        self.assertIs(stop_arg, stop)
        tunnel.process = mock.Mock(poll=mock.Mock(return_value=None))
        child = tunnel.process
        hook(stop, tunnel)
        self.assertTrue(stop.is_set())
        child.terminate.assert_called_once()
        self.assertIsNone(tunnel.process)

    def test_a_missing_host_key_refuses_to_start_a_tunnel(self):
        self.activate()
        g.update(GLOBAL_RELAY_HOST_KEY="")
        self.assertIsNone(g.tunnel_command())

    def test_supervisor_backs_off_then_recovers(self):
        self.activate()
        started = []

        class Child:
            def __init__(self):
                self.code = 1

            def poll(self):
                return self.code

            def terminate(self):
                self.code = 0

            def wait(self, timeout=None):
                return 0

        def spawn(command, **kwargs):
            started.append(command)
            return Child()

        tunnel = g.Tunnel()
        with mock.patch.object(g.subprocess, "Popen", side_effect=spawn):
            tunnel.supervise(0.0)
            self.assertEqual(len(started), 1)
            # A dead child is not respawned until the backoff elapses.
            tunnel.supervise(1.0)
            self.assertEqual(len(started), 1)
            tunnel.supervise(100.0)
            self.assertEqual(len(started), 2)
            self.assertGreater(tunnel.backoff, tunnel.MIN_BACKOFF)

    def test_leaving_tears_the_tunnel_down(self):
        self.activate()
        stopped = []

        class Child:
            def poll(self):
                return None

            def terminate(self):
                stopped.append(True)

            def wait(self, timeout=None):
                return 0

        tunnel = g.Tunnel()
        tunnel.process = Child()
        g.update(GLOBAL_STATE="left")
        tunnel.supervise(0.0)
        self.assertEqual(stopped, [True])
        self.assertIsNone(tunnel.process)

    def test_certificate_covers_the_global_name_from_the_moment_it_is_chosen(self):
        """Not only once approved: adding the name to the certificate later
        reissues the CA, which makes every enrolled client re-enrol."""
        from remoteRF_server.tools import gen_certs

        self.join()
        self.assertEqual(gen_certs._global_hostname(), "ucla.global.remoterf.net")
        self.activate()
        self.assertEqual(gen_certs._global_hostname(), "ucla.global.remoterf.net")

    def test_a_lab_that_declined_global_gets_no_extra_name(self):
        from remoteRF_server.tools import gen_certs

        self.join()
        g.update(GLOBAL_CONSENT="no")
        self.assertIsNone(gen_certs._global_hostname())


class StatusReportTests(TunnelTests):
    """Phase 3: exactly what the consent text promised, and nothing else."""

    def seed(self):
        import sqlite3

        db = g.cfg_dir() / "db" / "reservations.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE Accounts (username TEXT, auth_type TEXT)")
            conn.executemany(
                "INSERT INTO Accounts VALUES (?,?)",
                [("a", "local_password"), ("b", "local_password"), ("c", "local_password"),
                 ("fed-x", "federated")],
            )
            conn.execute(
                "CREATE TABLE Reservations "
                "(username TEXT, device_id INT, start_time TIMESTAMP, end_time TIMESTAMP)"
            )
            conn.executemany(
                "INSERT INTO Reservations VALUES (?,?,?,?)",
                [
                    ("a", 0, "2000-01-01 00:00:00", "2999-01-01 00:00:00"),
                    ("b", 1, "2000-01-01 00:00:00", "2000-01-02 00:00:00"),
                ],
            )
            conn.execute("CREATE TABLE ReservationCount (id INTEGER PRIMARY KEY, n INTEGER)")
            conn.execute("INSERT INTO ReservationCount VALUES (1, 5)")
        (g.cfg_dir() / "devices.yml").write_text(
            "devices:\n"
            "  - device_id: 0\n    device_type: adalm_pluto\n"
            "  - device_type: usrp_2901\n    device_id: 1\n",
            encoding="utf-8",
        )
        certs = g.cfg_dir() / "certs"
        certs.mkdir(parents=True, exist_ok=True)
        (certs / "ca.crt").write_bytes(b"-----BEGIN CERTIFICATE-----\nx\n")

    def test_report_payload_matches_the_consent_text(self):
        """The consent text is the contract; the payload may not exceed it."""
        self.activate()
        self.seed()
        payload = g.status_payload()

        self.assertEqual(set(payload), g.STATUS_FIELDS)
        self.assertEqual(payload["users"], 3)  # a partner's visitor is not one of ours
        self.assertEqual(payload["proxies"], 1)
        self.assertEqual(payload["active_reservations"], 1)
        self.assertEqual(payload["reservations"], 5)
        self.assertEqual(payload["transport"], {"days": []})
        self.assertEqual(payload["usage"],  # not on global
                         {"by_hour": [0] * 24, "by_weekday": [0] * 7, "by_type": {}, "by_device": []})
        self.assertEqual(
            sorted(entry["type"] for entry in payload["devices"]),
            ["adalm_pluto", "usrp_2901"],
        )
        self.assertEqual(payload["lan_address"], g.lan_endpoint())
        self.assertEqual(len(payload["cert_sha256"]), 64)

        # Every promised item is represented, and nothing forbidden leaks.
        serialized = repr(payload)
        for forbidden in ("username", "password", "token", "email", "salt", "hash"):
            self.assertNotIn(forbidden, serialized.lower())
        for name in ("a", "b", "c"):
            self.assertNotIn(f"'{name}'", serialized)

        # Each deployment field Global's stats.json publishes (vpsrf stats.public) is named.
        text = " ".join(g.CONSENT_TEXT.split())
        for item in ("name, version", "devices", "joined", "last online", "how many reservations",
                     "time reserved, at https://{domain}/stats.json", "devices you share on Global",
                     "each hour of the day", "each day of the week", "per device type", "these breakdowns",
                     "for each device shared on Global its name and its daily reservation counts and time reserved",
                     "daily per-path, per-device-type, per-function device call and byte counts",
                     "counts only, no users"):
            self.assertIn(item, text)

    def test_status_payload_survives_missing_state(self):
        self.activate()
        payload = g.status_payload()
        self.assertEqual(set(payload), g.STATUS_FIELDS)
        self.assertEqual(payload["users"], 0)
        self.assertEqual(payload["proxies"], 0)
        self.assertEqual(payload["reservations"], 0)
        self.assertEqual(payload["devices"], [])
        self.assertEqual(payload["usage"], {"by_hour": [0] * 24, "by_weekday": [0] * 7, "by_type": {}, "by_device": []})
        self.assertEqual(payload["transport"], {"days": []})
        self.assertIsNone(payload["cert_sha256"])

    def test_transport_is_sparse_newest_first_and_bounded(self):
        from datetime import datetime, timedelta, timezone
        import sqlite3

        self.seed()
        today = datetime.now(timezone.utc).date()
        rows = []
        for age in (0, 1):
            day = str(today - timedelta(days=age))
            rows.extend(
                (day, "lan", "adalm_pluto", f"Adalm_pluto:fn{i:04d}:CALL0", 1, i + 1, i + 2)
                for i in range(301)
            )
        rows.extend([
            (str(today - timedelta(days=2)), "direct", "usrp", "Usrp:rx:CALL0", 1, 2, 3),
            (str(today - timedelta(days=31)), "relay", "usrp", "Usrp:old:CALL0", 1, 2, 3),
            (str(today + timedelta(days=1)), "relay", "usrp", "Usrp:future:CALL0", 1, 2, 3),
            (str(today), "relay", "usrp", "Usrp:zero:CALL0", 0, 0, 0),
        ])
        with sqlite3.connect(g.cfg_dir() / "db" / "reservations.db") as conn:
            conn.execute(
                "CREATE TABLE TransportDays (day TEXT, path TEXT, type TEXT, fn TEXT, calls INTEGER, "
                "bytes_in INTEGER, bytes_out INTEGER, PRIMARY KEY(day,path,type,fn))"
            )
            conn.executemany("INSERT INTO TransportDays VALUES (?,?,?,?,?,?,?)", rows)

        days = g.transport_summary()["days"]
        self.assertEqual(len(days), 500)  # today and yesterday only; Global keeps older days
        self.assertEqual([row["day"] for row in days[:301]], [str(today)] * 301)
        self.assertEqual([row["day"] for row in days[301:]], [str(today - timedelta(days=1))] * 199)
        self.assertTrue(all(set(row) == {"day", "path", "type", "fn", "calls", "bytes_in", "bytes_out"}
                            for row in days))

    def test_usage_counts_only_the_devices_shared_on_global(self):
        import sqlite3
        from remoteRF_server.server.federation import cluster

        self.activate()
        self.seed()  # devices.yml: 0 adalm_pluto, 1 usrp_2901
        with sqlite3.connect(g.cfg_dir() / "db" / "reservations.db") as conn:
            conn.execute("CREATE TABLE ReservationBuckets (device_id INTEGER, bucket TEXT, reservations INTEGER, "
                         "seconds REAL, PRIMARY KEY (device_id, bucket))")
            conn.executemany("INSERT INTO ReservationBuckets VALUES (?,?,?,?)", [
                (0, "all", 3, 5400.4), (0, "h20", 2, 3600.0), (0, "h06", 1, 1800.4), (0, "d6", 3, 5400.4),
                (1, "all", 9, 99.0), (1, "h20", 9, 99.0), (1, "d0", 9, 99.0),  # not shared on Global
                (5, "all", 1, 60.0), (5, "h23", 1, 60.0), (5, "d4", 1, 60.0),  # gone from devices.yml
                (7, "all", 0, 0.0), (7, "h01", 0, 0.0), (7, "d1", 0, 0.0),  # every reservation cancelled
            ])
        shared = [{"devices": [0, 5]}, {"devices": [7]}]
        with mock.patch.object(cluster, "groups", return_value=shared) as groups:
            usage = g.usage_summary()
        groups.assert_called_once_with(cluster.GLOBAL)

        self.assertEqual(usage["by_hour"], [0] * 6 + [1] + [0] * 13 + [2, 0, 0, 1])
        self.assertEqual(usage["by_weekday"], [0, 0, 0, 0, 1, 0, 3])
        self.assertEqual(usage["by_type"], {"adalm_pluto": {"reservations": 3, "seconds": 5400},
                                            "unknown": {"reservations": 1, "seconds": 60}})

        with mock.patch.object(cluster, "groups", side_effect=cluster.ClusterError("no cluster named global")):
            self.assertEqual(g.usage_summary(),
                             {"by_hour": [0] * 24, "by_weekday": [0] * 7, "by_type": {}, "by_device": []})

    def days_table(self, rows):
        import sqlite3

        with sqlite3.connect(g.cfg_dir() / "db" / "reservations.db") as conn:
            conn.execute("CREATE TABLE ReservationDays (device_id INTEGER, day TEXT, reservations INTEGER, "
                         "seconds REAL, PRIMARY KEY (device_id, day))")
            conn.executemany("INSERT INTO ReservationDays VALUES (?,?,?,?)", rows)

    def test_by_device_reports_the_last_31_days_of_shared_devices_by_name(self):
        from datetime import datetime, timedelta, timezone
        from remoteRF_server.server.federation import cluster

        self.seed()
        (g.cfg_dir() / "devices.yml").write_text(
            "devices:\n"
            "  - {device_id: 0, device_type: adalm_pluto, name: Bench Pluto}\n"
            "  - {device_id: 1, device_type: usrp_2901, name: Not shared}\n"
            "  - {device_id: 2, device_type: adalm_pluto, name: \"\\e[31mRed\\nPluto \"}\n"
            "  - {device_id: 3, device_type: adalm_pluto, name: \"\\t\"}\n"
            f"  - {{device_id: 4, device_type: adalm_pluto, name: {'x' * 80}}}\n",
            encoding="utf-8",
        )
        self.use_host_registry([("host-a", 200, "adalm_pluto_mimo_ykush")])
        today = datetime.now(timezone.utc).date()
        day = lambda n: str(today - timedelta(days=n))  # noqa: E731
        self.days_table([
            (0, day(0), 2, 5400.4), (0, day(30), 1, 60.0), (0, day(31), 9, 999.0),  # day 31 is out of the window
            (0, str(today + timedelta(days=1)), 1, 60.0),  # not started yet
            (0, day(3), 0, 0.2),  # every reservation cancelled
            (1, day(0), 4, 400.0),  # not shared on Global
            (2, day(1), 1, 30.0), (3, day(1), 0, 30.0), (4, day(1), 1, 0.0),
            (5, day(2), 1, 60.0),  # gone from devices.yml
            (6, day(31), 1, 60.0),  # nothing left in the window
            (200, day(5), 3, 180.0),
        ])
        shared = [{"devices": [0, 2, 3, 4, 5, 6]}, {"devices": [200]}]
        with mock.patch.object(cluster, "groups", return_value=shared):
            by_device = g.usage_summary()["by_device"]
        self.assertEqual(by_device, [
            {"id": 0, "name": "Bench Pluto", "type": "adalm_pluto", "days": {day(0): [2, 5400], day(30): [1, 60]}},
            {"id": 2, "name": "[31mRedPluto", "type": "adalm_pluto", "days": {day(1): [1, 30]}},
            {"id": 3, "name": "device 3", "type": "adalm_pluto", "days": {day(1): [0, 30]}},
            {"id": 4, "name": "x" * 64, "type": "adalm_pluto", "days": {day(1): [1, 0]}},
            {"id": 5, "name": "device 5", "type": "unknown", "days": {day(2): [1, 60]}},
            {"id": 200, "name": "Pluto 200", "type": "adalm_pluto_mimo_ykush", "days": {day(5): [3, 180]}},
        ])

        # A rename shows on the next note.
        path = g.cfg_dir() / "devices.yml"
        path.write_text(path.read_text(encoding="utf-8").replace("Bench Pluto", "Bench A"), encoding="utf-8")
        with mock.patch.object(cluster, "groups", return_value=shared):
            self.assertEqual(g.usage_summary()["by_device"][0]["name"], "Bench A")

    def test_by_device_drops_the_oldest_days_first_to_fit_globals_caps(self):
        from datetime import datetime, timedelta, timezone
        from remoteRF_server.server.federation import cluster

        self.seed()
        today = datetime.now(timezone.utc).date()
        self.days_table([(gid, str(today - timedelta(days=n)), 1, 60.0) for gid in range(130) for n in range(10)])
        with mock.patch.object(cluster, "groups", return_value=[{"devices": list(range(130))}]):
            by_device = g.usage_summary()["by_device"]
        self.assertEqual([entry["id"] for entry in by_device], list(range(128)))
        days = [d for entry in by_device for d in entry["days"]]
        self.assertEqual(len(days), 1000)
        self.assertEqual(min(days), str(today - timedelta(days=7)))  # days 8 and 9 dropped, day 7 cut short
        self.assertEqual(sum(d == min(days) for d in days), 1000 - 7 * 128)

    def use_host_registry(self, persisted=(), online_hosts=()):
        """The Host directory as the server loads it at boot, with online_hosts connected."""
        from types import SimpleNamespace
        from remoteRF_server.common.grpc import grpc_host_pb2 as pb2
        from remoteRF_server.host import host_tunnel_server as hts
        from remoteRF_server.server import device_manager as dm

        writer = hts.HostTunnelRegistry()
        for host_id, gid, kind in persisted:
            writer._persist_device(host_id, str(gid), 0, pb2.DeviceInfo(label=f"Pluto {gid}", serial=f"sn{gid}", kind=kind))
        reg = hts.HostTunnelRegistry()
        for host_id in online_hosts:
            reg._hosts[host_id] = SimpleNamespace(alive=True)
        for patch in (mock.patch.object(hts, "get_tunnel_registry", return_value=reg),
                      mock.patch.object(dm, "_devices", {}), mock.patch.object(dm, "_host_sync_last_ms", 0),
                      mock.patch.dict(dm.devices_info), mock.patch.dict(dm.device_serialization)):
            patch.start()
            self.addCleanup(patch.stop)
        return reg

    def test_host_devices_are_reported_with_their_type_and_state(self):
        self.seed()  # devices.yml: 0 adalm_pluto, 1 usrp_2901
        self.use_host_registry([("host-a", 200, "adalm_pluto_mimo_ykush"), ("host-b", 201, "ADALM_Pluto_MIMO_YKUSH")],
                               online_hosts=["host-a"])
        devices = g.device_summary()
        self.assertEqual(sorted((d["type"], d["online"]) for d in devices), [
            ("adalm_pluto", False), ("adalm_pluto_mimo_ykush", False), ("adalm_pluto_mimo_ykush", True),
            ("usrp_2901", False)])
        for leak in ("host-", "Pluto", "sn20", "200", "201"):
            self.assertNotIn(leak, repr(devices))

    def test_a_device_its_host_drops_is_no_longer_reported(self):
        from remoteRF_server.common.grpc import grpc_host_pb2 as pb2

        kind = "adalm_pluto_mimo_ykush"
        reg = self.use_host_registry([("host-a", 200, kind), ("host-a", 201, kind)], online_hosts=["host-a"])
        self.assertEqual(len(g.device_summary()), 2)
        reg.announce_devices("host-a", [pb2.DeviceInfo(device_id="200", kind=kind)], full_snapshot=True)
        self.assertEqual(g.device_summary(), [{"type": kind, "online": True}])

    def test_a_host_kind_is_bounded_the_way_global_accepts_it(self):
        self.use_host_registry([("host-a", 200, "Custom-" + "x" * 80)])
        self.assertEqual(g.device_summary(), [{"type": "custom_" + "x" * 57, "online": False}])

    def test_usage_maps_host_devices_to_their_type(self):
        import sqlite3
        from remoteRF_server.server.federation import cluster

        self.seed()
        self.use_host_registry([("host-a", 200, "adalm_pluto_mimo_ykush")])
        with sqlite3.connect(g.cfg_dir() / "db" / "reservations.db") as conn:
            conn.execute("CREATE TABLE ReservationBuckets (device_id INTEGER, bucket TEXT, reservations INTEGER, "
                         "seconds REAL, PRIMARY KEY (device_id, bucket))")
            conn.executemany("INSERT INTO ReservationBuckets VALUES (?,?,?,?)",
                             [(200, "all", 2, 120.0), (200, "h05", 2, 120.0), (200, "d1", 2, 120.0)])
        with mock.patch.object(cluster, "groups", return_value=[{"devices": [200]}]):
            usage = g.usage_summary()
        self.assertEqual(usage["by_type"], {"adalm_pluto_mimo_ykush": {"reservations": 2, "seconds": 120}})

    def test_no_host_ever_connected_reports_devices_yml_only(self):
        self.seed()
        self.use_host_registry()
        self.assertEqual(g.device_summary(), [{"type": "adalm_pluto", "online": False},
                                              {"type": "usrp_2901", "online": False}])

    def test_status_falls_back_for_a_global_that_predates_proxies(self):
        self.activate()
        sent = []

        def old_global(method, path, body):
            sent.append(set(body["status"]))
            if "proxies" in body["status"]:
                raise g.GlobalError("invalid_status")

        with mock.patch.object(g, "call", side_effect=old_global):
            g.send_status()
        self.assertEqual(sent, [
            set(g.STATUS_FIELDS),
            set(g.STATUS_FIELDS) - {"transport"},
            set(g.STATUS_FIELDS) - {"transport"},
            set(g.STATUS_FIELDS) - {"transport", "usage"},
            set(g.STATUS_FIELDS) - {"transport", "usage", "reservations"},
            set(g.STATUS_FIELDS) - {"transport", "usage", "reservations", "proxies", "reserved_s"},
        ])

    def test_status_drops_reservations_too_for_a_global_that_predates_it(self):
        self.activate()
        sent = []

        def global_with_reserved_s(method, path, body):
            sent.append(set(body["status"]))
            if "reservations" in body["status"]:
                raise g.GlobalError("invalid_status")

        with mock.patch.object(g, "call", side_effect=global_with_reserved_s):
            g.send_status()
        self.assertEqual(sent, [
            set(g.STATUS_FIELDS),
            set(g.STATUS_FIELDS) - {"transport"},
            set(g.STATUS_FIELDS) - {"transport"},
            set(g.STATUS_FIELDS) - {"transport", "usage"},
            set(g.STATUS_FIELDS) - {"transport", "usage", "reservations"},
        ])

    def test_status_drops_only_usage_for_a_global_that_predates_it(self):
        self.activate()
        sent = []

        def global_with_reservations(method, path, body):
            sent.append(set(body["status"]))
            if "usage" in body["status"]:
                raise g.GlobalError("invalid_status")

        with mock.patch.object(g, "call", side_effect=global_with_reservations):
            g.send_status()
        self.assertEqual(sent, [
            set(g.STATUS_FIELDS),
            set(g.STATUS_FIELDS) - {"transport"},
            set(g.STATUS_FIELDS) - {"transport"},
            set(g.STATUS_FIELDS) - {"transport", "usage"},
        ])

    def test_status_drops_only_by_device_for_a_global_that_predates_it(self):
        self.activate()
        sent = []

        def global_with_usage(method, path, body):
            sent.append(body["status"])
            if "by_device" in body["status"]["usage"]:
                raise g.GlobalError("invalid_status")

        with mock.patch.object(g, "call", side_effect=global_with_usage):
            g.send_status()
        self.assertEqual([set(status) for status in sent], [
            set(g.STATUS_FIELDS),
            set(g.STATUS_FIELDS) - {"transport"},
            set(g.STATUS_FIELDS) - {"transport"},
        ])
        self.assertEqual(sent[2]["usage"], {"by_hour": [0] * 24, "by_weekday": [0] * 7, "by_type": {}})

    def test_status_drops_transport_first_for_an_older_global(self):
        self.activate()
        sent = []

        def old_global(method, path, body):
            sent.append(body["status"])
            if "transport" in body["status"]:
                raise g.GlobalError("invalid_status")

        with mock.patch.object(g, "call", side_effect=old_global):
            g.send_status()
        self.assertEqual([set(status) for status in sent], [
            set(g.STATUS_FIELDS), set(g.STATUS_FIELDS) - {"transport"}
        ])
        self.assertIn("by_device", sent[1]["usage"])

    def test_status_gives_up_when_even_the_oldest_note_is_refused(self):
        self.activate()

        with mock.patch.object(g, "call", side_effect=g.GlobalError("invalid_status")) as call:
            with self.assertRaises(g.GlobalError):
                g.send_status()
        self.assertEqual(call.call_count, 6)

    def test_reports_only_flow_once_active(self):
        self.join()
        sent = []
        with mock.patch.object(g, "send_liveness", side_effect=lambda: sent.append("live")), \
             mock.patch.object(g, "send_status", side_effect=lambda: sent.append("status")), \
             mock.patch.object(g, "submit_request"):
            g._tick({})
        self.assertEqual(sent, [])

        self.activate()
        last = {}
        with mock.patch.object(g, "send_liveness", side_effect=lambda: sent.append("live")), \
             mock.patch.object(g, "send_status", side_effect=lambda: sent.append("status")), \
             mock.patch.object(g, "fetch_me"):
            g._tick(last)
            g._tick(last)  # inside both intervals
        self.assertEqual(sent, ["live", "status"])

    def test_report_is_signed_to_its_own_path(self):
        import jwt

        self.activate()
        self.seed()
        seen, patched = self.capture({})
        with patched:
            g.send_status()
        claims = jwt.decode(
            seen["headers"]["Authorization"].removeprefix("Bearer "),
            options={"verify_signature": False},
        )
        self.assertEqual(claims["path"], "/v2/broker/deployments/report")
        body = __import__("json").loads(seen["data"])
        self.assertEqual(body["kind"], "status")
        self.assertEqual(set(body["status"]), g.STATUS_FIELDS)
        self.assertIn("GLOBAL_LAST_REPORT_AT", g.load())


class UnknownDeploymentTests(TunnelTests):
    """Global forgot the deployment (a wiped DB): stop, say so once, allow a re-join."""

    def refuse(self, code="unknown_deployment"):
        return mock.patch.object(
            g, "call", side_effect=g.GlobalError(code, "Broker request authentication failed")
        )

    def test_unknown_deployment_is_terminal_and_rejoinable(self):
        import threading

        self.activate()
        tunnel = mock.Mock()
        warnings = []
        with self.refuse(), mock.patch.object(g, "_warn", side_effect=warnings.append), \
             mock.patch.object(g, "TICK_SECONDS", 0):
            g.run_agent(threading.Event(), tunnel)  # returns once the state is terminal
            self.assertEqual(g.state(), "unknown")
            self.assertTrue(tunnel.stop.called)
            with self.assertRaises(g.GlobalError):
                g.fetch_me()  # a repeat is not a second warning
        self.assertEqual(len(warnings), 1)
        self.assertIn("unknown", g._STATE_SUMMARY)
        with mock.patch.object(g, "configure", return_value=True) as configure, self.answer("y"):
            self.assertEqual(g.cmd_join(), 0)
        configure.assert_called_once()

    def test_status_names_the_refusal_code(self):
        self.activate()
        with self.refuse("deployment_suspended"), \
             mock.patch("remoteRF_server.common.utils.panel.print_panel") as panel:
            g.cmd_status()
        self.assertIn("(refused by RemoteRF Global (deployment_suspended))", panel.call_args[0][1])

    def test_a_refused_report_forces_a_poll_next_tick(self):
        self.activate()
        last = {"poll": 0.0}
        with mock.patch("time.monotonic", return_value=1.0), \
             mock.patch.object(g, "fetch_me", side_effect=AssertionError("polled")), \
             mock.patch.object(g, "send_liveness", side_effect=g.GlobalError("unknown_deployment")):
            with self.assertRaises(g.GlobalError):
                g._tick(last)
        self.assertNotIn("poll", last)
        with mock.patch("time.monotonic", return_value=2.0), \
             mock.patch.object(g, "fetch_me") as me, \
             mock.patch.object(g, "send_liveness"), mock.patch.object(g, "send_status"):
            g._tick(last)
        me.assert_called_once()

    def test_failed_polls_back_off_and_reset_on_success(self):
        self.activate()
        last = {}
        now = 0.0
        gaps = []
        me = mock.patch.object(g, "fetch_me", side_effect=ConnectionError("down"))
        with me as fetch, mock.patch.object(g, "send_liveness"), mock.patch.object(g, "send_status"):
            for _ in range(6):
                with mock.patch("time.monotonic", return_value=now), self.assertRaises(ConnectionError):
                    g._tick(last)
                due = last["poll"] + g.POLL_ACTIVE_SECONDS
                gaps.append(due - now)
                calls = fetch.call_count
                with mock.patch("time.monotonic", return_value=due - 1):
                    g._tick(last)  # still inside the backoff: no poll
                self.assertEqual(fetch.call_count, calls)
                now = due
        self.assertEqual(gaps, [120, 240, 480, 900, 900, 900])
        with mock.patch("time.monotonic", return_value=now), mock.patch.object(g, "fetch_me"), \
             mock.patch.object(g, "send_liveness"), mock.patch.object(g, "send_status"):
            g._tick(last)
        self.assertEqual(last["poll"], now)
        self.assertNotIn("backoff", last)
        with me, mock.patch("time.monotonic", return_value=now + g.POLL_ACTIVE_SECONDS), \
             self.assertRaises(ConnectionError):
            g._tick(last)
        self.assertEqual(last["poll"] + g.POLL_ACTIVE_SECONDS - (now + g.POLL_ACTIVE_SECONDS), 120)

    def test_liveness_stamps_last_seen(self):
        self.activate()
        _, patched = self.capture({})
        with patched:
            g.send_liveness()
        self.assertIn("GLOBAL_LAST_SEEN_AT", g.load())
        self.assertNotIn("GLOBAL_LAST_REPORT_AT", g.load())


class DisplayNameTests(ConsentTests):
    def test_display_name_defaults_to_the_slug(self):
        with mock.patch.object(g, "name_available", return_value=True):
            with self.answer("y", "ucla", "", "lab@ucla.edu"):
                g.ensure_consent()
        self.assertEqual(g.load()["GLOBAL_DISPLAY_NAME"], "ucla")

    def test_control_characters_fall_back_to_the_slug(self):
        with mock.patch.object(g, "name_available", return_value=True):
            with self.answer("y", "ucla", "UCLA\x1b[2J", "lab@ucla.edu"):
                g.ensure_consent()
        self.assertEqual(g.load()["GLOBAL_DISPLAY_NAME"], "ucla")


class SubmitReportTests(RequestTests):
    """What the operator is told after answering the consent questions."""

    def setUpSubmitStub(self):
        # This class is specifically about the real submit path.
        pass

    def test_a_successful_request_is_acknowledged(self):
        printed = []
        with mock.patch.object(g, "name_available", return_value=True):
            with self.answer("y", "ucla", "UCLA Wireless Lab", "lab@ucla.edu"):
                _, patched = self.capture(
                    {"status": "requested", "name": None, "relay": None,
                     "deployment_id": "8a2e3f10-0000-4000-8000-000000000001"}
                )
                with patched, mock.patch("builtins.print", lambda *a, **k: printed.append(a)):
                    self.assertTrue(g.ensure_consent())
        self.assertEqual(g.state(), "requested")

    def test_an_unreachable_global_says_so_and_keeps_the_server_usable(self):
        """A lab with no route to Global still gets a working local server."""
        with mock.patch.object(g, "name_available", return_value=None):
            with self.answer("y", "ucla", "UCLA Wireless Lab", "lab@ucla.edu"):
                # No mock: the suite's origin is the discard port, so this is a
                # real failed connection, not a stubbed one.
                self.assertTrue(g.ensure_consent())
        # Consent is recorded, the request simply has not landed yet.
        self.assertTrue(g.participating())
        self.assertEqual(g.state(), "unregistered")

    def test_a_name_taken_race_reprompts_instead_of_failing(self):
        calls = []

        def submit():
            calls.append(load := g.load().get("GLOBAL_NAME"))
            if len(calls) == 1:
                raise g.GlobalError("name_taken", "taken")

        with mock.patch.object(g, "name_available", return_value=True):
            with self.answer("y", "ucla", "UCLA Wireless Lab", "lab@ucla.edu", "ucla2"):
                with mock.patch.object(g, "submit_request", side_effect=submit):
                    self.assertTrue(g.ensure_consent())
        self.assertEqual(calls, ["ucla", "ucla2"])
        self.assertEqual(g.load()["GLOBAL_NAME"], "ucla2")


class UnattendedJoinTests(unittest.TestCase):
    """Consent without a terminal: REMOTERF_GLOBAL_* or `global join --yes`."""

    # ConsentTests' hermetic config, without re-running its tests here.
    setUp = ConsentTests.setUp
    answer = ConsentTests.answer

    def setUpSubmitStub(self):
        # A service start, and the real submit path down to its one network call.
        g.sys.stdin.isatty.return_value = False
        for name, patch in (
            ("call", mock.patch.object(g, "call", return_value={"status": "unverified", "name": None, "relay": None})),
            ("name_available", mock.patch.object(g, "name_available", return_value=True)),
            ("input", mock.patch.object(builtins, "input", mock.Mock(side_effect=AssertionError("prompted")))),
        ):
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)

    def env(self, **fields):
        return mock.patch.dict("os.environ", {f"REMOTERF_GLOBAL_{k}": v for k, v in fields.items()})

    def output(self, fn, *args):
        import contextlib
        import io

        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.output.plain_text import PlainTextOutput

        out = io.StringIO()
        with create_app_session(output=PlainTextOutput(out)), contextlib.redirect_stdout(out):
            result = fn(*args)
        return result, out.getvalue()

    def test_env_consent_submits_once_and_records_it(self):
        # A service keeps them in server.env; the process environment wins.
        g.write_env_kv(g.cfg_dir() / "server.env", {
            "REMOTERF_GLOBAL_CONSENT": "yes", "REMOTERF_GLOBAL_NAME": "ucla",
            "REMOTERF_GLOBAL_EMAIL": "admin@example.org",
            "REMOTERF_GLOBAL_DISPLAY_NAME": "Westwood Radio Lab",
        })
        with self.env(NAME="westwood"):
            ok, text = self.output(g.ensure_consent)
        self.assertTrue(ok)
        self.call.assert_called_once()
        method, path, payload = self.call.call_args[0]
        self.assertEqual((method, path), ("POST", g.REQUEST_PATH))
        self.assertEqual(
            (payload["name"], payload["display_name"], payload["email"]),
            ("westwood", "Westwood Radio Lab", "admin@example.org"),
        )
        kv = g.load()
        self.assertEqual(kv["GLOBAL_CONSENT"], "yes")
        self.assertEqual(kv["GLOBAL_STATE"], "unverified")
        # The journal names the next step but never quotes the address.
        self.assertIn("serverrf global verify <code>", text)
        self.assertIn("a***@example.org", text)
        self.assertNotIn("admin@example.org", text)

    def test_a_missing_or_invalid_field_sends_nothing(self):
        cases = [
            ({"EMAIL": "admin@example.org"}, "REMOTERF_GLOBAL_NAME is not set"),
            ({"NAME": "-Bad-", "EMAIL": "admin@example.org"}, "REMOTERF_GLOBAL_NAME '-bad-' is not a valid name"),
            ({"NAME": "westwood"}, "REMOTERF_GLOBAL_EMAIL is not set"),
            ({"NAME": "westwood", "EMAIL": "admin@example"}, "REMOTERF_GLOBAL_EMAIL is not a valid email address"),
        ]
        for fields, message in cases:
            with self.subTest(fields=fields), self.env(CONSENT="yes", **fields), \
                    mock.patch.object(g, "_warn") as warn:
                self.assertTrue(g.ensure_consent())
                warn.assert_called_once()
                self.assertIn(message, warn.call_args[0][0])
                self.assertNotIn("admin@example", warn.call_args[0][0])
        self.call.assert_not_called()
        self.assertNotIn("GLOBAL_CONSENT", g.load())

    def test_a_taken_name_is_not_requested(self):
        self.name_available.return_value = False
        with self.env(CONSENT="yes", NAME="westwood", EMAIL="admin@example.org"), \
                mock.patch.object(g, "_warn") as warn:
            self.assertTrue(g.ensure_consent())
        self.assertIn("westwood is already registered", warn.call_args[0][0])
        self.call.assert_not_called()

    def test_env_opt_out_records_no(self):
        with self.env(CONSENT="no"):
            self.assertTrue(g.ensure_consent())
        self.assertEqual(g.load()["GLOBAL_CONSENT"], "no")
        self.call.assert_not_called()

    def test_an_answered_question_is_never_overridden(self):
        for earlier, env in (("no", "yes"), ("yes", "no")):
            with self.subTest(earlier=earlier):
                g.update(GLOBAL_CONSENT=earlier)
                with self.env(CONSENT=env, NAME="westwood", EMAIL="admin@example.org"):
                    self.assertTrue(g.ensure_consent())
                self.assertEqual(g.load()["GLOBAL_CONSENT"], earlier)
        self.call.assert_not_called()

    def test_join_yes_shows_the_text_once_and_submits(self):
        with mock.patch("remoteRF_server.common.utils.panel.print_panel") as panel:
            rc = g.main(["join", "--yes", "--name", "westwood", "--email", "admin@example.org"])
        self.assertEqual(rc, 0)
        labels = [c[0][0] for c in panel.call_args_list]
        self.assertEqual(labels.count("RemoteRF Global participation"), 1)
        self.call.assert_called_once()
        self.assertEqual(g.load()["GLOBAL_CONSENT"], "yes")

    def test_join_yes_without_a_name_sends_nothing(self):
        with mock.patch.object(g, "_warn") as warn:
            self.assertEqual(g.main(["join", "--yes", "--email", "admin@example.org"]), 1)
        self.assertIn("--name is not set", warn.call_args[0][0])
        self.call.assert_not_called()

    def test_join_after_opting_out_is_allowed_and_recorded(self):
        g.update(GLOBAL_CONSENT="no")
        g.sys.stdin.isatty.return_value = True
        with self.answer("n"):
            self.assertEqual(g.cmd_join(), 1)
        self.assertEqual(g.load()["GLOBAL_CONSENT"], "no")
        with self.answer("y", "westwood", "", "admin@example.org"):
            self.assertEqual(g.cmd_join(), 0)
        self.assertEqual(g.load()["GLOBAL_CONSENT"], "yes")
        self.call.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class InviteTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict("os.environ", {"REMOTERF_CONFIG_DIR": self.tmp.name, "REMOTERF_GLOBAL_DOMAIN": "global.remoterf.net"})
        patch.start()
        self.addCleanup(patch.stop)

    def test_live_global_deployment_hands_out_the_full_form(self):
        g.update(GLOBAL_CONSENT="yes", GLOBAL_STATE="active", GLOBAL_FINAL_NAME="ucla-dev0")
        self.assertEqual(g.invite("RERGSOQ5"), "ucla-dev0/RERGSOQ5")
        # LAN-only groups never get a Global invite: it would not work there.
        self.assertEqual(g.invite("RERGSOQ5", lan_only=True), "RERGSOQ5")

    def test_bare_code_until_the_deployment_is_live(self):
        g.update(GLOBAL_CONSENT="yes", GLOBAL_STATE="requested", GLOBAL_NAME="ucla-dev0")
        self.assertEqual(g.invite("RERGSOQ5"), "RERGSOQ5")
        self.assertEqual(g.tunnel_state(), "not assigned")
        g.update(GLOBAL_CONSENT="no")
        self.assertEqual(g.tunnel_state(), "off")


class FederationRuntimeTests(unittest.TestCase):
    """Slice 0: `--serve` derives the federation runtime from global.env + deployment.json."""

    DEPLOYMENT = "8a2e3f10-0000-4000-8000-000000000001"

    def setUp(self):
        import os
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("REMOTERF_", "XDG_"))
        }
        env.update(
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_DATA_HOME=str(root / "data"),
            REMOTERF_GLOBAL_ORIGIN="https://127.0.0.1:9",
            REMOTERF_BIND_HOST="0.0.0.0",
        )
        patch = mock.patch.dict("os.environ", env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.db_dir = root / "config" / "remoterf" / "db"
        self.handlers(self.db_dir)

    def handlers(self, db_dir):
        """Build the three real handlers rooted at db_dir and install them as the
        module singletons the runtime binds to."""
        from remoteRF_server.server.acc_perms import perms_db
        from remoteRF_server.server.reservation import ReservationHandler
        from remoteRF_server.server.user_group_handler import UserGroupHandler

        db_dir.mkdir(parents=True, exist_ok=True)
        patches = [
            mock.patch(target, return_value=db_dir)
            for target in (
                "remoteRF_server.server.reservation.get_db_dir",
                "remoteRF_server.server.acc_perms.get_db_dir",
                "remoteRF_server.server.user_group_handler.get_db_dir",
            )
        ]
        for item in patches:
            item.start()
        try:
            self.perms = perms_db()
            self.groups = UserGroupHandler()
            self.reservations = ReservationHandler()
        finally:
            for item in patches:
                item.stop()
        for target, value in (
            ("remoteRF_server.server.acc_perms.perms", self.perms),
            ("remoteRF_server.server.user_group_handler.user_group_handler", self.groups),
            ("remoteRF_server.server.reservation.perms", self.perms),
            ("remoteRF_server.server.reservation.user_group_handler", self.groups),
        ):
            item = mock.patch(target, value)
            item.start()
            self.addCleanup(item.stop)

    def enroll(self, status="active"):
        g.update(
            GLOBAL_CONSENT="yes",
            GLOBAL_NAME="ucla",
            GLOBAL_DISPLAY_NAME="UCLA Wireless Lab",
            GLOBAL_EMAIL="lab@ucla.edu",
            GLOBAL_STATE="unregistered",
        )
        g.ensure_identity()
        g._apply_me(
            {"status": status, "deployment_id": self.DEPLOYMENT, "name": "ucla", "relay": None}
        )

    def snapshot(self):
        """Everything Slice 0 may touch: deployment.json, global.env, and the
        migration ledger + JTI/audit tables of every DB under both roots."""
        import sqlite3
        from pathlib import Path

        from remoteRF_server.server.federation.identity import load_state

        rows = {}
        for db in sorted(Path(self.tmp.name).rglob("*.db")):
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                for table in ("SchemaMigrations", "FederationGrantJTIs", "FederationAuditEvents",
                              "LocalAccountSubjects"):
                    try:
                        rows[(db.name, table)] = conn.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0]
                    except sqlite3.OperationalError:
                        rows[(db.name, table)] = None
        return (
            load_state(g.identity_dir()).to_dict(),
            dict(g.load()),
            sorted(str(p) for p in Path(self.tmp.name).rglob("*.db")),
            rows,
        )

    def test_runtime_refuses_when_paths_mismatch(self):
        from pathlib import Path

        from remoteRF_server.server.federation.runtime import (
            build_configured_server_federation,
            federation_enabled,
        )

        self.enroll("active")
        # The running Server's handlers live somewhere other than the
        # deployment's own db dir: the runtime must not bind to either.
        self.handlers(Path(self.tmp.name) / "elsewhere")
        self.assertTrue(federation_enabled())
        before = self.snapshot()
        with self.assertRaisesRegex(
            RuntimeError, "reservations path does not match the running Server"
        ):
            build_configured_server_federation(self.reservations)
        self.assertEqual(self.snapshot(), before)

    def test_runtime_refuses_when_global_is_not_active(self):
        from remoteRF_server.server.federation.runtime import (
            build_configured_server_federation,
            federation_enabled,
        )

        self.enroll("requested")
        self.assertFalse(federation_enabled())
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "GLOBAL_STATE=active"):
            build_configured_server_federation(self.reservations)
        self.assertEqual(self.snapshot(), before)

    def test_runtime_refuses_when_deployment_json_is_not_enrolled(self):
        from remoteRF_server.server.federation.runtime import (
            build_configured_server_federation,
            federation_enabled,
        )

        g.update(GLOBAL_CONSENT="yes", GLOBAL_NAME="ucla", GLOBAL_STATE="active")
        g.ensure_identity()  # keypair + empty deployment.json, as before Slice 0
        self.assertFalse(federation_enabled())
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "deployment.json to be enrolled"):
            build_configured_server_federation(self.reservations)
        self.assertEqual(self.snapshot(), before)

    def test_env_zero_keeps_federation_off(self):
        from remoteRF_server.server.federation.runtime import federation_enabled

        self.enroll("active")
        with mock.patch.dict("os.environ", {"REMOTERF_FEDERATION_ENABLED": "0"}):
            self.assertFalse(federation_enabled())
        self.assertTrue(federation_enabled())

    def test_runtime_starts_from_global_env_alone(self):
        import os

        from remoteRF_server.server.federation.runtime import (
            build_configured_server_federation,
            federation_enabled,
        )

        self.enroll("active")
        self.assertNotIn("REMOTERF_FEDERATION_CONFIG_PATH", os.environ)
        self.assertNotIn("REMOTERF_FEDERATION_ENABLED", os.environ)
        self.assertTrue(federation_enabled())
        runtime = build_configured_server_federation(self.reservations)
        try:
            self.assertIsNotNone(runtime.executor)
            self.assertIs(runtime.enroller.outbound, runtime.permissions.outbound)
            self.assertEqual(runtime.permissions.local_deployment_id, self.DEPLOYMENT)
            self.assertEqual(runtime.permissions.display_name, "UCLA Wireless Lab")
            self.assertEqual(
                runtime.permissions.path, (self.db_dir / "usergroups.db").resolve()
            )
        finally:
            runtime.permissions.close()

    def test_join_refuses_while_already_live(self):
        import contextlib
        import io

        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.output.plain_text import PlainTextOutput

        self.enroll("active")
        out = io.StringIO()
        with create_app_session(output=PlainTextOutput(out)), contextlib.redirect_stdout(out), \
                mock.patch.object(g, "configure") as configure:
            self.assertEqual(g.cmd_join(), 1)
        configure.assert_not_called()
        self.assertIn("Already live", out.getvalue())

    def test_global_clusters_is_empty_on_a_fresh_deployment(self):
        import contextlib
        import io

        from remoteRF_server.server.federation.cli import main as federation_main

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(federation_main(["clusters"]), 1)
        self.assertIn("not enabled", out.getvalue())

        self.enroll("active")
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.output.plain_text import PlainTextOutput

        out = io.StringIO()
        with create_app_session(output=PlainTextOutput(out)), contextlib.redirect_stdout(out):
            self.assertEqual(federation_main(["clusters"]), 0)
        text = out.getvalue()
        self.assertIn("UCLA Wireless Lab", text)
        self.assertNotIn(self.DEPLOYMENT, text)
        self.assertIn("No clusters", text)
        self.assertEqual(federation_main([]), 2)
