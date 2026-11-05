"""Exact machine-readable contracts for the serverrf reads used by lab ops."""

import contextlib
import hashlib
import io
import json
import socket
import sqlite3
import sys
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("REMOTERF_CONFIG_DIR", str(tmp_path / "config" / "remoterf"))


def _run(function, *args, **kwargs):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        rc = function(*args, **kwargs)
    text = output.getvalue()
    assert text.count("\n") == 1
    return rc, json.loads(text)


def _assert_keys(value, keys):
    assert set(value) == set(keys)


def _control_round_trip(handler, argv):
    server_socket, client_socket = socket.socketpair()
    daemon = __import__("remoteRF_server.server.control", fromlist=["ControlServer"]).ControlServer(handler)
    thread = threading.Thread(target=daemon._handle, args=(server_socket,))
    thread.start()
    try:
        client_socket.sendall((json.dumps({"argv": argv}) + "\n").encode())
        return json.loads(client_socket.makefile("rb").readline())
    finally:
        client_socket.close()
        server_socket.close()
        thread.join()


def test_status_json_exact_keys_and_types(monkeypatch):
    from remoteRF_server.server import global_onboarding as global_state
    from remoteRF_server.server import grpc_server

    monkeypatch.setattr(grpc_server, "device_inventory_snapshot", lambda: {
        1: {"online": True}, 2: {"online": False},
    })
    monkeypatch.setattr(global_state, "server_version", lambda: "1.7.11")
    monkeypatch.setattr(global_state, "_count", lambda *args: 3)
    rc, value = _run(grpc_server.cmd_status, ["--json"])

    assert rc is None
    _assert_keys(value, ("server_version", "online", "uptime_s", "devices_total",
                         "devices_online", "reservations_active", "driver_overrides"))
    assert value["driver_overrides"] == []
    assert type(value["server_version"]) is str and value["online"] is True
    assert value["uptime_s"] is None or type(value["uptime_s"]) is int
    assert all(type(value[key]) is int for key in
               ("devices_total", "devices_online", "reservations_active"))
    assert type(value["server_version"]) is str
    assert value["online"] is True
    assert type(value["uptime_s"]) is int or value["uptime_s"] is None
    assert all(type(value[key]) is int for key in
               ("devices_total", "devices_online", "reservations_active"))


def test_devices_and_hosts_json_exact_keys_and_types(monkeypatch):
    from remoteRF_server.server import grpc_server

    seen = int(datetime.now().timestamp() * 1000)
    devices = {"7": SimpleNamespace(host_id="lab-host", host_local_id=2, last_seen_ms=seen)}
    hosts = {"lab-host": SimpleNamespace(online=True, last_seen_ms=seen)}
    registry = SimpleNamespace(list_devices=lambda: devices, list_hosts=lambda: hosts)
    monkeypatch.setattr(grpc_server.hts, "get_tunnel_registry", lambda create=True: registry)
    monkeypatch.setattr(grpc_server, "device_inventory_snapshot", lambda: {
        7: {"name": "SDR", "handle": "SDR", "device_type": "pluto", "online": True},
    })

    _, device_rows = _run(grpc_server.cmd_devices_list, ["--json"])
    assert len(device_rows) == 1
    device = device_rows[0]
    _assert_keys(device, ("device_id", "name", "handle", "type", "host", "online", "state", "last_seen"))
    assert type(device["device_id"]) is int
    assert all(type(device[key]) is str for key in ("name", "handle", "type", "host"))
    assert type(device["online"]) is bool
    assert device["state"] is None or type(device["state"]) is str
    assert datetime.fromisoformat(device["last_seen"]).utcoffset() is not None

    _, host_rows = _run(grpc_server.cmd_hosts_status, ["--json"])
    assert len(host_rows) == 1
    host = host_rows[0]
    _assert_keys(host, ("host_id", "online", "last_seen", "devices"))
    assert type(host["host_id"]) is str and type(host["online"]) is bool
    assert datetime.fromisoformat(host["last_seen"]).utcoffset() is not None
    assert host["devices"] == [7] and all(type(item) is int for item in host["devices"])


def test_global_status_json_exact_keys_and_types(monkeypatch):
    from remoteRF_server.server import global_onboarding

    monkeypatch.setattr(global_onboarding, "load", lambda: {
        "GLOBAL_STATE": "active",
        "GLOBAL_FINAL_NAME": "lab-a",
        "GLOBAL_DEPLOYMENT_ID": "11111111-1111-4111-8111-111111111111",
        "GLOBAL_RELAY_HOSTNAME": "relay.example.org",
    })
    rc, value = _run(global_onboarding.main, ["status", "--json"])
    assert rc == 0
    _assert_keys(value, ("state", "slug", "deployment_id", "relay"))
    assert type(value["state"]) is str
    assert all(item is None or type(item) is str for item in
               (value["slug"], value["deployment_id"], value["relay"]))


def test_global_status_json_does_not_ensure_certificate_names(monkeypatch):
    from remoteRF_server import serverrf_cli
    from remoteRF_server.server import global_onboarding

    monkeypatch.setattr(global_onboarding, "main", lambda argv: print('{"state":"active"}') or 0)
    ensure = mock.Mock()
    monkeypatch.setattr(serverrf_cli, "_ensure_certificate_names", ensure)

    rc, value = _run(serverrf_cli.cmd_global, ["status", "--json"])

    assert rc == 0
    assert value == {"state": "active"}
    ensure.assert_not_called()


def test_cluster_list_and_info_json_exact_keys_and_types(monkeypatch):
    from remoteRF_server.server.federation import cli, cluster

    item = {
        "name": "shared", "role": "owner", "lab_cap": 2, "members": [{
            "deployment_id": "22222222-2222-4222-8222-222222222222",
            "display_name": "Partner Lab", "state": "active",
        }],
    }
    monkeypatch.setattr(cli, "load_config", lambda: {"display_name": "Lab A", "identity_dir": "/unused"})
    monkeypatch.setattr(cli, "federation_enabled", lambda: True)
    monkeypatch.setattr(cli, "load_state", lambda path: SimpleNamespace(deployment_id="local"))
    monkeypatch.setattr(cluster, "list_clusters", lambda: [item])

    rc, values = _run(cli.cmd_cluster, ["list", "--json"])
    assert rc == 0 and len(values) == 1
    rc, value = _run(cli.cmd_cluster, ["info", "shared", "--json"])
    assert rc == 0 and value == values[0]
    _assert_keys(value, ("name", "lab_cap", "members"))
    assert type(value["name"]) is str
    assert type(value["lab_cap"]) is int or value["lab_cap"] is None
    member = value["members"][0]
    _assert_keys(member, ("deployment_id", "display_name", "state"))
    assert all(type(member[key]) is str for key in member)


def test_reservations_json_exact_keys_types_and_local_offsets(tmp_path, monkeypatch):
    from remoteRF_server.server import reservation

    monkeypatch.setattr(reservation, "get_db_dir", lambda: tmp_path)
    handler = reservation.ReservationHandler()
    start = datetime.now().replace(microsecond=0)
    end = start + timedelta(hours=1)
    with sqlite3.connect(handler.filepath) as conn:
        res_id = conn.execute(
            "INSERT INTO Reservations (username,device_id,start_time,end_time,salt,hash) "
            "VALUES ('jdoe',7,?,?,'salt','hash')", (start.isoformat(" "), end.isoformat(" "))
        ).lastrowid
        conn.execute(
            "INSERT INTO FederatedReservations "
            "(res_id,issuer_deployment_id,remote_subject_id,local_account_id,contract_id,"
            "contract_version,request_id,created_at) VALUES (?,?,?,?,?,1,?,1)",
            (res_id, "11111111-1111-4111-8111-111111111111",
             "22222222-2222-4222-8222-222222222222", 1,
             "33333333-3333-4333-8333-333333333333",
             "44444444-4444-4444-8444-444444444444"),
        )

    _, values = _run(handler.print_all_reservations, json_output=True)
    assert len(values) == 1
    value = values[0]
    _assert_keys(value, ("res_id", "username", "device_id", "start_time", "end_time",
                         "federated", "issuer_deployment_id"))
    assert type(value["res_id"]) is int and type(value["device_id"]) is int
    assert type(value["username"]) is str and type(value["federated"]) is bool
    assert type(value["issuer_deployment_id"]) is str
    assert datetime.fromisoformat(value["start_time"]).utcoffset() is not None
    assert datetime.fromisoformat(value["end_time"]).utcoffset() is not None


def test_json_survives_control_protocol_and_read_tiers_stay_read(monkeypatch):
    from remoteRF_server import remote_admin_shell
    from remoteRF_server.server import grpc_server

    monkeypatch.setattr(grpc_server, "device_inventory_snapshot", lambda: {})
    reply = _control_round_trip(grpc_server.control_handler, ["status", "--json"])
    assert reply["rc"] == 0
    value = json.loads(reply["out"])
    _assert_keys(value, ("server_version", "online", "uptime_s", "devices_total",
                         "devices_online", "reservations_active", "driver_overrides"))
    assert set(remote_admin_shell.TIERS[name] for name in (
        "status", "devices list", "hosts status", "global status", "cluster list",
        "cluster info", "reservations list", "schema served",
    )) == {remote_admin_shell.READ}


def _override(device_type: str, base_sha256: str) -> str:
    return (f"# rrfops-override base-sha256={base_sha256}\n"
            "from remoterf_device_core.idl.schema import DeviceSchema, idl_register\n\n"
            f"@idl_register({device_type!r})\n"
            "class Probe(DeviceSchema):\n"
            f"    device_type = {device_type!r}\n"
            "    driver_version = 'override'\n")


def _builtin_sha(device_type):
    from remoterf_device_core.idl import schema

    return hashlib.sha256(schema.builtin_driver_file(device_type).read_bytes()).hexdigest()


@pytest.fixture
def drivers_dir(monkeypatch):
    from remoteRF_server.common.utils import get_drivers_dir
    from remoteRF_server.server import grpc_server  # noqa: F401  (its first import seeds the drivers folder)
    from remoterf_device_core.idl import schema

    monkeypatch.setattr(schema, "_DRIVER_REGISTRY", dict(schema._DRIVER_REGISTRY))
    monkeypatch.setattr(schema, "_LOAD_RESULTS", [])
    monkeypatch.setitem(sys.modules, "zz_rrfops_rtl_sdr", None)
    path = get_drivers_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_schema_served_json_names_the_override_that_serves(drivers_dir):
    from remoteRF_server.server import grpc_server
    from remoterf_device_core.idl import schema

    override = drivers_dir / "zz_rrfops_rtl_sdr.py"
    override.write_text(_override("rtl_sdr", _builtin_sha("rtl_sdr")))
    (drivers_dir / "broken.py").write_text("raise ValueError('bad\\ndriver')\n")
    schema.load_drivers(drivers_dir)
    rc, value = _run(grpc_server.cmd_schema_served, ["rtl_sdr", "--json"])

    assert rc == 0
    _assert_keys(value, ("type", "hash", "module", "override", "source_sha256", "loads"))
    assert value["type"] == "rtl_sdr" and value["override"] is True
    assert value["module"] == str(override)
    assert value["hash"] == schema.get_driver_class("rtl_sdr")().get_idl_hash()
    assert value["hash"].startswith("sha256:")
    assert value["source_sha256"] == hashlib.sha256(override.read_bytes()).hexdigest()
    assert value["loads"][-1] == {"file": "zz_rrfops_rtl_sdr.py", "ok": True, "error": None}
    assert {"file": "broken.py", "ok": False, "error": "bad driver"} in value["loads"]


def test_schema_served_unknown_and_bad_types(drivers_dir, capsys):
    from remoteRF_server.server import grpc_server

    rc, value = _run(grpc_server.cmd_schema_served, ["no_such_type", "--json"])
    assert rc == 0
    loads = value.pop("loads")
    assert value == {"type": "no_such_type", "hash": None, "module": None, "override": False,
                     "source_sha256": None}
    assert not [row for row in loads if row["file"].endswith(".py")]  # only the built-in modules' rows
    capsys.readouterr()
    for argv in (["RTL", "--json"], [], ["a", "b"], ["x" * 65]):
        assert grpc_server.cmd_schema_served(argv) == 2
        assert capsys.readouterr().out == "Usage: schema served <type> [--json]\n"


def test_status_json_lists_driver_overrides_and_staleness(monkeypatch, drivers_dir):
    from remoteRF_server.server import grpc_server

    monkeypatch.setattr(grpc_server, "device_inventory_snapshot", lambda: {})
    (drivers_dir / "zz_rrfops_rtl_sdr.py").write_text(_override("rtl_sdr", _builtin_sha("rtl_sdr")))
    (drivers_dir / "zz_rrfops_hackrf.py").write_text(_override("hackrf", "0" * 64))
    _, value = _run(grpc_server.cmd_status, ["--json"])

    assert value["driver_overrides"] == [
        {"type": "hackrf", "file": "zz_rrfops_hackrf.py", "stale": True},
        {"type": "rtl_sdr", "file": "zz_rrfops_rtl_sdr.py", "stale": False},
    ]


@pytest.mark.parametrize("argv", (["status", "--json"], ["devices", "list", "--json"]))
def test_shell_dispatch_forwards_json_to_the_control_socket(monkeypatch, capsys, argv):
    from remoteRF_server.server import commands, control

    payload = {"server_version": "1.7.11", "online": True, "uptime_s": 1,
               "devices_total": 0, "devices_online": 0, "reservations_active": 0}
    seen = []

    def call(argv, **kwargs):
        seen.append(argv)
        return 0, json.dumps(payload) + "\n"

    monkeypatch.setattr(control, "running", lambda: True)
    monkeypatch.setattr(control, "call", call)
    assert commands.dispatch(argv, in_daemon=False) == 0
    assert seen == [argv]
    assert json.loads(capsys.readouterr().out) == payload
