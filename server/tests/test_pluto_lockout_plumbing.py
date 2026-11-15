import sqlite3
import sys
import threading
import types
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

sys.modules.setdefault("adi", types.ModuleType("adi"))
from remoterf_device_core.drivers.adalm_pluto import pluto_schema as core
from remoterf_device_core.v2_errors import RemoteRFPolicyError
from remoteRF_server.common.grpc import grpc_pb2
from remoteRF_server.common.utils import map_arg, unmap_arg
from remoteRF_server.server import device_manager as dm, rpc_manager as rpc, reservation as res
from remoteRF_server.server import host_dynamic_v2 as hd, startup_check as sc
from remoteRF_server.drivers.adalm_pluto import pluto_diag as diag

MESSAGE = "The reservation on this device changed; call again."


@pytest.fixture
def parser(monkeypatch):
    parse = Mock(return_value={"tx_enabled": False})
    monkeypatch.setattr(core, "parse_lockouts", parse, raising=False)
    monkeypatch.setattr(core, "LOCKOUT_KEYS", frozenset({"tx_enabled"}), raising=False)
    return parse


class PlutoSchema:
    lockout_bypass = False
    make_device = Mock(return_value=object())

    def bind(self, raw):
        self.device = raw

    def configure_lockouts(self, cfg):
        self.cfg = cfg

    def dispatch(self, name, args):
        return None


@pytest.mark.parametrize("problem", ["bad_config", "stale", "override", "non_pluto", "missing_configure"])
def test_load_refuses_before_make(monkeypatch, parser, caplog, problem):
    cls = type("Served", (PlutoSchema,), {})
    if problem == "bad_config":
        parser.side_effect = ValueError("invalid range")
    elif problem == "stale":
        cls.dispatch = lambda *args: None
    elif problem == "override":
        class Override(PlutoSchema):
            def dispatch(self, *args):
                return None
        cls = Override
    elif problem == "non_pluto":
        cls = type("Unrelated", (), {})
    else:
        cls.configure_lockouts = None
    make = Mock()
    cls.make_device = make
    monkeypatch.setattr(dm, "get_driver_class", lambda _: cls)
    assert dm._connect_from_record({"TYPE": "fake", "NAME": "Roof", "INIT": {"tx_enabled": False}})[0] is None
    make.assert_not_called()
    assert "device left offline" in caplog.text and "test_pluto_lockout_plumbing.py" in caplog.text


def test_load_configures_after_bind_and_unlocked_stale_still_loads(monkeypatch, parser):
    monkeypatch.setattr(dm, "get_driver_class", lambda _: PlutoSchema)
    schema = dm._connect_from_record({"TYPE": "fake", "INIT": {"tx_enabled": False}})[0]
    assert schema.device is not None and schema.cfg == {"tx_enabled": False}
    parser.return_value = None
    class Stale(PlutoSchema):
        configure_lockouts = None
        dispatch = None
    monkeypatch.setattr(dm, "get_driver_class", lambda _: Stale)
    assert dm._connect_from_record({"TYPE": "fake", "INIT": {}})[0] is not None


@pytest.fixture
def state(monkeypatch):
    dev = types.SimpleNamespace(lockout_bypass=False, dispatch=Mock())
    st = dm._DeviceState(dev, "local", True, "s", "h", "Roof", "", "fake")
    monkeypatch.setattr(dm, "_devices", {7: st})
    monkeypatch.setattr(dm, "_sync_host_devices", lambda **kw: None)
    monkeypatch.setattr(dm, "_native_cleanup_pending", lambda _: False)
    monkeypatch.setattr(dm, "_refuse_if_diag", lambda _: None)
    return st


@pytest.mark.parametrize("by_gid", [False, True])
def test_queued_call_refused_and_lock_released(monkeypatch, state, by_gid):
    acquired = threading.Event()
    lock = threading.Lock()
    lock.acquire()
    class QueueLock:
        def acquire(self):
            acquired.set()
            return lock.acquire()
        def release(self):
            lock.release()
    state.io_lock = QueueLock()
    monkeypatch.setattr(dm, "_get_master_token", lambda: "master")
    monkeypatch.setattr(dm, "parse_mastertoken", lambda *a, **kw: None)
    monkeypatch.setattr(dm, "validate_token", lambda *a: True)
    errors = []
    def run():
        try:
            manager = dm.acquire_device_by_gid(7, expected_reservation=("s", "h")) if by_gid else dm.acquire_device("token")
            with manager:
                errors.append("ran")
        except Exception as exc:
            errors.append(str(exc))
    worker = threading.Thread(target=run)
    worker.start()
    assert acquired.wait(2)
    with dm._state_lock:
        state.salt, state.hsh = "next", "holder"
        state.dev.lockout_bypass = True
    lock.release()
    worker.join(2)
    assert not worker.is_alive() and errors == [MESSAGE]
    assert lock.acquire(blocking=False)
    lock.release()


@pytest.mark.parametrize("error", [RemoteRFPolicyError("TX is disabled on this device (receive-only)."), dm.ReservationChangedError()])
def test_federated_error_is_plain_text(monkeypatch, state, error):
    state.dev.dispatch.side_effect = error
    out = rpc.RpcManager().run_device_rpc_for_gid(gid=7, function_name="fake:tx:CALL0", args={})
    assert {key: unmap_arg(value) for key, value in out.items()} == {"a": str(error)}


def test_policy_failure_keeps_earlier_batch_ops(state):
    calls = []
    def dispatch(name, args):
        if name == "call_tx":
            raise RemoteRFPolicyError("TX is disabled on this device (receive-only).")
        calls.append(name)
    state.dev.dispatch.side_effect = dispatch
    args = {}
    for i, name in enumerate(["fake:rx_lo:SET", "fake:tx:CALL0", "fake:rx:CALL0"]):
        request = grpc_pb2.GenericRPCRequest(function_name=name)
        if i == 0:
            request.args["rx_lo"].CopyFrom(map_arg(915e6))
        args[f"op{i}"] = map_arg(request.SerializeToString())
    out = rpc.RpcManager().run_device_rpc_for_gid(gid=7, function_name="fake:batch:CALLN", args=args)
    assert calls == ["set_rx_lo"]
    assert unmap_arg(out["batch_done"]) == unmap_arg(out["batch_failed"]) == 1
    assert unmap_arg(out["batch_error"]).startswith("TX is disabled")


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("kind", ["reset", "prepare"])
def test_after_hooks_always_run(monkeypatch, state, kind, failed):
    after = state.dev.lockout_after_hook = Mock()
    hook = Mock(side_effect=RuntimeError("hook failed") if failed else None)
    if kind == "reset":
        state.dev.reset_for_next_holder = hook
        state.reset_pending = True
        dm._reset_locked(state)
    else:
        state.dev.prepare_for_holder = hook
        dm._prepare_for_holder(7, ("s", "h"))
    after.assert_called_once_with(kind, failed=failed)


def test_bypass_written_with_holder_and_cleared_when_free(monkeypatch, state):
    monkeypatch.setattr(dm, "_reservation_end_callbacks", [])
    monkeypatch.setattr(dm, "_reset_if_pending", lambda _: None)
    dm.set_device(7, "next", "holder", bypass=True)
    assert state.lockout_bypass is True
    assert state.dev.lockout_bypass is False
    with dm.acquire_device_by_gid(7):
        assert state.dev.lockout_bypass is True
    dm.set_device(7, "", "", bypass=True)
    assert state.dev.lockout_bypass is False


@pytest.mark.parametrize("start", [False, True])
def test_dropped_control_frame_drops_tunnel(monkeypatch, start):
    dev = dm.VirtualDevice(7, "host", "1")
    registry = types.SimpleNamespace(send_control=Mock(return_value=False), drop_host=Mock())
    monkeypatch.setattr(dm.hts, "get_tunnel_registry", lambda **kw: registry)
    if start:
        dm._start_host_reservation(dev, bypass=True)
        assert registry.send_control.call_args.kwargs["payload"]["lockout_bypass"] is True
    else:
        monkeypatch.setattr(hd.HostDynamicV2Proxy, "_virtual_for_gid", lambda _: dev)
        monkeypatch.setattr(hd.host_session_proxy, "close_gid", Mock())
        hd._end_host_reservation(7)
    assert registry.send_control.call_args.kwargs["drop_on_failure"] is True
    registry.drop_host.assert_not_called()


@pytest.mark.parametrize("group,lan_only,bypass", [("admin", False, True), ("user", False, False), ("admin", True, False)])
def test_admin_membership_only_on_holder_change(monkeypatch, tmp_path, group, lan_only, bypass):
    from remoteRF_server.server.user_group_handler import UserGroupHandler
    path = tmp_path / "groups.db"
    with sqlite3.connect(path) as con:
        con.executescript("CREATE TABLE UserGroups (group_id INTEGER, group_name TEXT, lan_only INTEGER); CREATE TABLE UsersLookup (user_id INTEGER, group_id INTEGER);")
        con.execute("INSERT INTO UserGroups VALUES (1, ?, ?)", (group, int(lan_only)))
        con.execute("INSERT INTO UsersLookup VALUES (42, 1)")
    groups = UserGroupHandler.__new__(UserGroupHandler)
    groups.filepath, groups.lock = path, threading.RLock()
    lookup = Mock(wraps=groups.get_groups_user_in)
    groups.get_groups_user_in = lookup
    monkeypatch.setattr(res, "user_group_handler", groups)
    now = datetime.now()
    row = (1, "alice", 7, now - timedelta(minutes=1), now + timedelta(minutes=1), "s", "h")
    handler = types.SimpleNamespace(cursor=Mock(), _clean_expired_reservations=lambda _: None,
        _account_security_state_locked=lambda _: "native", get_uid=Mock(return_value=42))
    handler.cursor.fetchall.return_value = [row]
    inventory = {7: (object(), "", "")}
    monkeypatch.setattr(res, "get_all_devices", lambda: dict(inventory))
    monkeypatch.setattr(res, "get_registered_devices", lambda: dict(inventory))
    set_device = Mock()
    monkeypatch.setattr(res, "set_device", set_device)
    token = res.RELAYED.set(False)
    try:
        res.ReservationHandler.update_devices.__wrapped__(handler)
        assert res.RELAYED.get() is False
        set_device.assert_called_once_with("7", "s", "h", bypass=bypass)
        inventory[7] = (inventory[7][0], "s", "h")
        res.ReservationHandler.update_devices.__wrapped__(handler)
        assert lookup.call_count == handler.get_uid.call_count == set_device.call_count == 1
    finally:
        res.RELAYED.reset(token)


def test_startup_settles_reserved_board(state):
    state.dev.lockout_settle = Mock()
    state.dev.call_power_down = Mock()
    sc._restore_power(state)
    state.dev.lockout_settle.assert_called_once()
    state.dev.call_power_down.assert_not_called()


def test_diag_clamps_tx_lo_but_keeps_rx_sweep(parser):
    parser.return_value = {"tx_min_freq": 902e6, "tx_max_freq": 928e6}
    schema = Mock()
    schema.get_rx_lo.side_effect = [400e6, 915e6, 2.4e9, 3.5e9]
    schema.get_tx_lo.return_value = 915e6
    diag.lo_tuning({"schema": schema, "init": {"tx_enabled": True}})
    assert [call.args[0] for call in schema.set_rx_lo.call_args_list] == [400e6, 915e6, 2.4e9, 3.5e9]
    schema.set_tx_lo.assert_called_once_with(915_000_000)


@pytest.mark.parametrize("cfg", [{"tx_enabled": False}, {"tx_min_freq": 914e6, "tx_max_freq": 916e6}, {"tx_max_gain_db": -90}, "bad"])
@pytest.mark.parametrize("test", [diag.loopback_tone, diag.coax_loopback])
def test_diag_refuses_tones_before_setup(parser, cfg, test):
    if cfg == "bad":
        parser.side_effect = ValueError("bad config")
    else:
        parser.return_value = cfg
    schema = Mock()
    with pytest.raises(diag.Skip):
        test({"schema": schema, "init": {"multi_tap": True}})
    assert not schema.mock_calls


def test_cli_preserves_all_lockout_keys(monkeypatch, tmp_path):
    from remoteRF_server import serverrf_cli as cli
    init = {"tx_min_freq": "902e6", "tx_max_freq": 928_000_000, "admin_group_override": True,
            "tx_max_gain_db": -10, "tx_enabled": True, "park_tx_lo": 915e6, "allow_rf_loopback": False,
            "allow_dds": False, "tx_max_dds_scale": 0.2, "allow_cyclic_tx": False,
            "tx_max_buffer_samples": 100, "max_sample_rate": 3e6, "tx_allowed_channels": [0],
            "rx_min_freq": 70e6, "rx_max_freq": 6e9, "lockout_allow_methods": ["call_camera_capture"]}
    monkeypatch.setattr(cli, "_cfg_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_devices_yaml_path", lambda: tmp_path / "devices.yml")
    monkeypatch.setattr(cli, "_devices_env_path", lambda: tmp_path / "devices.env")
    cli._write_device_records({7: {"TYPE": "adalm_pluto", "INIT": init}})
    assert cli._read_device_records()[7]["INIT"] == init


def test_pluto_has_no_dynamic_v2_path(monkeypatch):
    from remoteRF_server.server import dynamic_v2
    from remoterf_device_core.v2_errors import RemoteRFProtocolError
    schema = PlutoSchema()
    monkeypatch.setattr(dynamic_v2, "_refuse_partner_token", lambda _: None)
    monkeypatch.setattr(dynamic_v2, "resolve_gid_for_token", lambda _: 7)
    monkeypatch.setattr(dynamic_v2, "get_local_device_context", lambda _: {"schema": schema, "dtype": "adalm_pluto"})
    with pytest.raises(RemoteRFProtocolError, match="does not publish schema v2"):
        dynamic_v2.session_manager.schema_for_token("token")


def test_federated_reservation_changed_during_acquire(monkeypatch):
    monkeypatch.setattr(rpc, "acquire_device_by_gid", Mock(side_effect=dm.ReservationChangedError()))
    out = rpc.RpcManager().run_device_rpc_for_gid(gid=7, function_name="fake:tx:CALL0", args={})
    assert unmap_arg(out["a"]) == MESSAGE


def test_offline_same_holder_does_not_lookup_membership(monkeypatch):
    now = datetime.now()
    handler = types.SimpleNamespace(cursor=Mock(), _clean_expired_reservations=lambda _: None,
        _account_security_state_locked=lambda _: "native", get_uid=Mock())
    handler.cursor.fetchall.return_value = [(1, "alice", 7, now - timedelta(minutes=1), now + timedelta(minutes=1), "s", "h")]
    monkeypatch.setattr(res, "get_all_devices", lambda: {})
    monkeypatch.setattr(res, "get_registered_devices", lambda: {7: (None, "s", "h")})
    monkeypatch.setattr(res, "set_device", Mock())
    res.ReservationHandler.update_devices.__wrapped__(handler)
    handler.get_uid.assert_not_called()
    res.set_device.assert_not_called()


@pytest.mark.parametrize("pluto,init", [(False, {}), (True, {}), (False, {"tx_enabled": False})])
def test_native_import_failure_only_blocks_pluto(monkeypatch, pluto, init):
    import builtins
    original = builtins.__import__
    def no_native(name, *args, **kwargs):
        if name == "remoterf_device_core.drivers.adalm_pluto.pluto_schema":
            raise AttributeError("libiio is unavailable")
        return original(name, *args, **kwargs)
    class Other:
        make_device = Mock(return_value=object())
        def bind(self, raw):
            self.device = raw
    cls = type("Served", (PlutoSchema if pluto else Other,), {})
    cls.make_device = Mock(return_value=object())
    monkeypatch.setattr(dm, "get_driver_class", lambda _: cls)
    monkeypatch.setattr(builtins, "__import__", no_native)
    schema = dm._connect_from_record({"TYPE": "fake", "INIT": init})[0]
    # Without libiio a non-Pluto still loads, but lockout keys on it are refused, not ignored.
    offline = pluto or bool(init)
    assert (schema is None) is offline
    assert cls.make_device.call_count == (0 if offline else 1)


def test_holder_change_never_elevates_in_flight_batch(monkeypatch, state):
    entered, resume = threading.Event(), threading.Event()
    observed = []
    def dispatch(name, args):
        if name == "call_first":
            entered.set()
            assert resume.wait(2)
        observed.append(state.dev.lockout_bypass)
    state.dev.dispatch.side_effect = dispatch
    monkeypatch.setattr(dm, "_reservation_end_callbacks", [])
    monkeypatch.setattr(dm, "_reset_if_pending", lambda _: None)
    args = {f"op{i}": map_arg(grpc_pb2.GenericRPCRequest(
        function_name=f"fake:{name}:CALL0").SerializeToString())
        for i, name in enumerate(("first", "second"))}
    result = []
    worker = threading.Thread(target=lambda: result.append(rpc.RpcManager().run_device_rpc_for_gid(
        gid=7, function_name="fake:batch:CALLN", args=args, expected_reservation=("s", "h"))))
    worker.start()
    assert entered.wait(2)
    try:
        dm.set_device(7, "admin", "next", bypass=True)
        assert state.lockout_bypass is True and state.dev.lockout_bypass is False
    finally:
        resume.set()
        worker.join(2)
    assert not worker.is_alive() and observed == [False, False]
    assert unmap_arg(result[0]["batch_done"]) == 2
    with dm.acquire_device_by_gid(7, expected_reservation=("admin", "next")):
        assert state.dev.lockout_bypass is True


SYNC_HOST_DEVICES = dm._sync_host_devices


@pytest.mark.parametrize("by_gid", [False, True])
def test_host_refresh_during_io_wait_uses_current_state(monkeypatch, state, by_gid):
    acquired = threading.Event()
    lock = threading.Lock()
    lock.acquire()
    class QueueLock:
        def acquire(self):
            acquired.set()
            return lock.acquire()
        def release(self):
            lock.release()
    state.io_lock = QueueLock()
    state.origin = "host"
    state.dev = dm.VirtualDevice(7, "host", "7", label="old")
    monkeypatch.setattr(dm, "_get_master_token", lambda: "master")
    monkeypatch.setattr(dm, "parse_mastertoken", lambda *a, **kw: None)
    monkeypatch.setattr(dm, "validate_token", lambda *a: True)
    registry = types.SimpleNamespace(device_directory_cached=lambda **kw: {
        "7": ("host", types.SimpleNamespace(label="new", serial="", kind="fake"), True)})
    monkeypatch.setattr(dm, "_get_host_registry", lambda: registry)
    result = []
    def run():
        try:
            manager = dm.acquire_device_by_gid(7, expected_reservation=("s", "h")) if by_gid else dm.acquire_device("token")
            with manager as (_, dev):
                result.append(dev.label)
        except Exception as exc:
            result.append(str(exc))
    worker = threading.Thread(target=run)
    worker.start()
    assert acquired.wait(2)
    try:
        SYNC_HOST_DEVICES(force=True)
        assert dm._devices[7] is not state
    finally:
        lock.release()
        worker.join(2)
    assert not worker.is_alive() and result == ["new"]


def test_expected_federated_reservation_refuses_cached_admin(state):
    state.lockout_bypass = state.dev.lockout_bypass = True
    out = rpc.RpcManager().run_device_rpc_for_gid(
        gid=7, function_name="fake:tx:CALL0", args={}, expected_reservation=("new", "holder"))
    assert unmap_arg(out["a"]) == MESSAGE
    state.dev.dispatch.assert_not_called()


def test_reset_after_hook_failure_is_logged_and_contained(state, caplog):
    state.reset_pending = True
    state.dev.lockout_after_hook = Mock(side_effect=RuntimeError("Park failed"))
    dm._reset_locked(state)
    assert not state.reset_pending
    assert "Lockout cleanup failed for Roof" in caplog.text


def test_failed_control_send_does_not_drop_replacement_session(monkeypatch):
    from remoteRF_server.host import host_tunnel_server as hts
    registry = hts.HostTunnelRegistry()
    old, new = Mock(alive=True), Mock(alive=True)
    def reconnect(frame):
        registry._hosts["host"] = new
        return False
    old.send.side_effect = reconnect
    registry._hosts["host"] = old
    assert not registry.send_control("host", kind="reservation_start", device_id="7", drop_on_failure=True)
    assert registry.session_for_host("host") is new
    old.close.assert_not_called()
    new.close.assert_not_called()
    new.send.return_value = False
    assert not registry.send_control("host", kind="reservation_end", device_id="7", drop_on_failure=True)
    assert registry.session_for_host("host") is None
    new.close.assert_called_once()


@pytest.mark.parametrize("operation", ["batch", "rx_n", "single"])
def test_host_forwarding_keeps_authorized_tag_across_holder_change(monkeypatch, state, operation):
    import numpy as np
    state.origin = "host"
    state.dev = dm.VirtualDevice(7, "host", "7", kind="fake")
    monkeypatch.setattr(dm, "_reservation_end_callbacks", [])
    monkeypatch.setattr(dm, "_reset_if_pending", lambda _: None)
    monkeypatch.setattr(dm, "_start_host_reservation", Mock())
    tags = []
    def forward(registry, **kwargs):
        tags.append(unmap_arg(kwargs["args"]["__rrf_reservation_tag"]))
        if len(tags) == 1:
            dm.set_device(7, "admin", "next", bypass=True)
            return {"rx": map_arg(np.array([1.0]))}
        return {"Error": map_arg(MESSAGE)}
    monkeypatch.setattr(rpc.hts, "handle_host_device", forward)
    monkeypatch.setattr(rpc.hts, "get_tunnel_registry", lambda **kw: object())
    if operation == "batch":
        args = {f"op{i}": map_arg(grpc_pb2.GenericRPCRequest(
            function_name="fake:rx:CALL0").SerializeToString()) for i in range(2)}
        name = "fake:batch:CALLN"
    elif operation == "rx_n":
        args, name = {"arg1": map_arg(2)}, "fake:rx_n:CALL1"
    else:
        args, name = {"__rrf_reservation_tag": map_arg("forged")}, "fake:rx:CALL0"
    result = rpc.RpcManager().run_device_rpc_for_gid(gid=7, function_name=name, args=args,
        expected_reservation=("s", "h"))
    assert tags == [dm.reservation_tag("s", "h")] * (1 if operation == "single" else 2)
    if operation == "batch":
        assert unmap_arg(result["batch_failed"]) == 1
        assert unmap_arg(result["batch_error"]) == MESSAGE


@pytest.mark.parametrize("capable", [False, True])
def test_host_rpc_tag_is_capability_gated(monkeypatch, capable):
    from remoteRF_server.host import host_tunnel_server as hts
    session = hts.HostSession("host", capabilities={"reservation_tag"} if capable else set())
    registry = hts.HostTunnelRegistry()
    monkeypatch.setattr(registry, "_get_session_and_route", lambda _: (session, types.SimpleNamespace(host_local_id=7)))
    frames = []
    def send(frame, **kw):
        frames.append(frame)
        req = frame.rpc_request
        session.inflight[req.req_id].set_result(hts.host_tunnel_pb2.RpcResponse(req_id=req.req_id, ok=True))
        return True
    monkeypatch.setattr(session, "send", send)
    request = grpc_pb2.GenericRPCRequest(function_name="fake:custom:CALLN",
        args={"__rrf_reservation_tag": map_arg("authorized")})
    registry.forward_request(device_id="7", request=request, timeout_sec=1)
    args = frames[0].rpc_request.request.args
    assert ("__rrf_reservation_tag" in args) is capable
    assert "__rrf_reservation_tag" in request.args


def test_reconnect_resends_only_held_host_state(monkeypatch, state):
    state.origin = "host"
    state.dev = dm.VirtualDevice(7, "host", "7")
    state.lockout_bypass = True
    send = Mock(return_value=True)
    monkeypatch.setattr(dm.hts, "get_tunnel_registry", lambda **kw: types.SimpleNamespace(send_control=send))
    dm.resync_host_reservations("other")
    send.assert_not_called()
    dm.resync_host_reservations("host")
    assert send.call_args.kwargs["payload"] == {
        "at": send.call_args.kwargs["payload"]["at"], "lockout_bypass": True,
        "tag": dm.reservation_tag("s", "h"), "resync": True}
    assert send.call_args.kwargs["drop_on_failure"] is True
    state.salt = state.hsh = ""
    send.reset_mock()
    dm.resync_host_reservations("host")
    send.assert_not_called()


def test_reload_preserves_holder_bypass(monkeypatch, state):
    state.lockout_bypass = True
    replacement = types.SimpleNamespace(lockout_bypass=False)
    monkeypatch.setattr(dm, "_load_device_records", lambda: {7: {"NAME": "Roof"}})
    monkeypatch.setattr(dm, "_close_schema", Mock())
    monkeypatch.setattr(dm, "_connect_from_record", lambda *a, **kw: (replacement, "", "fake"))
    dm._reload_devices_unfenced()
    assert (dm._devices[7].salt, dm._devices[7].hsh) == ("s", "h")
    assert dm._devices[7].lockout_bypass
    assert not replacement.lockout_bypass
    with dm.acquire_device_by_gid(7, expected_reservation=("s", "h")):
        assert replacement.lockout_bypass


def test_full_queue_close_wakes_pump_and_heartbeat_cannot_revive(monkeypatch):
    from remoteRF_server.host import host_tunnel_server as hts
    session = hts.HostSession("host", outbound_max=1)
    session.out_q.put_nowait(hts.host_tunnel_pb2.HostFrame())
    registry = hts.HostTunnelRegistry()
    registry._hosts["host"] = session
    registry.drop_host_if_match("host", session)
    assert not session.alive and session.out_q.get_nowait() is None
    registry.heartbeat("host", session=session)
    assert not registry.list_hosts()["host"].online
    replacement = hts.HostSession("host")
    registry._hosts["host"] = replacement
    registry.heartbeat("host", session=session)
    assert not registry.list_hosts()["host"].online


@pytest.mark.parametrize("capable", [False, True])
def test_dropped_session_ends_outbound_pump_without_sentinel(monkeypatch, capable):
    import queue
    from remoteRF_server.host import host_tunnel_server as hts
    monkeypatch.setattr(hts, "is_host_token_valid", lambda *a, **kw: True)
    monkeypatch.setattr(hts, "_send_lab_info", lambda *a: None)
    registry = hts.HostTunnelRegistry()
    monkeypatch.setattr(registry, "persist_host_meta_from_hello", lambda *a: None)
    resync = Mock()
    monkeypatch.setattr(dm, "resync_host_reservations", resync)
    blocked = threading.Event()
    release = threading.Event()
    def frames():
        yield hts.host_tunnel_pb2.HostFrame(hello=hts.host_tunnel_pb2.HostHello(host_id="host", host_token="token",
            capabilities=["reservation_tag"] if capable else []))
        blocked.set()
        assert release.wait(3)
        yield hts.host_tunnel_pb2.HostFrame(heartbeat=hts.host_tunnel_pb2.Heartbeat())
    context = types.SimpleNamespace(peer=lambda: "test", is_active=lambda: True, add_callback=lambda cb: None)
    stream = hts.HostTunnelServicer(registry).Connect(frames(), context)
    next(stream)
    assert blocked.wait(2)
    assert resync.call_args_list == ([(("host",), {})] if capable else [])
    session = registry.session_for_host("host")
    registry.drop_host_if_match("host", session)
    while True:
        try:
            session.out_q.get_nowait()
        except queue.Empty:
            break
    done = []
    worker = threading.Thread(target=lambda: done.append(list(stream)))
    worker.start()
    try:
        worker.join(2)
        assert not worker.is_alive() and done == [[]]
        registry.heartbeat("host", session=session)
        assert not registry.list_hosts()["host"].online
    finally:
        release.set()


def test_slow_host_session_close_does_not_block_handover_or_resync(monkeypatch, state):
    state.origin = "host"
    state.dev = dm.VirtualDevice(7, "host", "7")
    closing, release, changed, resynced = (threading.Event() for _ in range(4))
    frames, daemons = [], []

    def send_control(host_id, **kwargs):
        assert dm._host_reservation_lock._is_owned()
        frames.append(kwargs["kind"])
        return True

    def close(gid, session_ids=None):
        daemons.append(threading.current_thread().daemon)
        closing.set()
        release.wait(5)

    monkeypatch.setattr(hd.hts, "get_tunnel_registry", lambda **kw: types.SimpleNamespace(send_control=send_control))
    monkeypatch.setattr(hd.host_session_proxy, "close_gid", close)
    monkeypatch.setattr(dm, "_reservation_end_callbacks", [hd._end_host_reservation])
    monkeypatch.setattr(dm, "_reset_if_pending", lambda _: None)
    handover = threading.Thread(target=lambda: (dm.set_device(7, "next", "holder"), changed.set()), daemon=True)
    inbound = threading.Thread(target=lambda: (dm.resync_host_reservations("host"), resynced.set()), daemon=True)
    try:
        handover.start()
        assert closing.wait(2)
        inbound.start()
        assert changed.wait(2)
        assert resynced.wait(2)
        assert frames == ["reservation_end", "reservation_start", "reservation_start"]
        assert daemons == [True]
    finally:
        release.set()
        handover.join(2)
        if inbound.ident is not None:
            inbound.join(2)


@pytest.mark.parametrize("owned", [False, True])
def test_local_v2_route_does_not_acquire_device_io(monkeypatch, state, owned):
    from remoterf_device_core.integrations import IntegrationError
    monkeypatch.setattr(dm, "_get_master_token", lambda: "master")
    acquire = Mock(side_effect=IntegrationError("device_owned", "owned") if owned else AssertionError("local I/O acquired"))
    monkeypatch.setattr(hd, "acquire_device", acquire)
    state.io_lock.acquire()
    try:
        assert hd.HostDynamicV2Proxy().route_for_token("master_7") is None
        acquire.assert_not_called()
    finally:
        state.io_lock.release()


@pytest.mark.parametrize("error", [RuntimeError("gone"), "owned"])
def test_host_v2_route_treats_admission_errors_as_no_route(monkeypatch, state, error):
    from remoterf_device_core.integrations import IntegrationError
    state.origin = "host"
    state.dev = dm.VirtualDevice(7, "host", "7")
    monkeypatch.setattr(dm, "_get_master_token", lambda: "master")
    monkeypatch.setattr(hd, "acquire_device", Mock(side_effect=IntegrationError("device_owned", "owned") if error == "owned" else error))
    assert hd.HostDynamicV2Proxy().route_for_token("master_7") is None


def test_host_v2_route_reports_a_changed_reservation(monkeypatch, state):
    from remoteRF_server.server.v2_errors import RemoteRFReservationError
    state.origin = "host"
    state.dev = dm.VirtualDevice(7, "host", "7")
    monkeypatch.setattr(dm, "_get_master_token", lambda: "master")
    monkeypatch.setattr(hd, "acquire_device", Mock(side_effect=dm.ReservationChangedError()))
    with pytest.raises(RemoteRFReservationError, match="The reservation on this device changed; call again."):
        hd.HostDynamicV2Proxy().route_for_token("master_7")


@pytest.mark.parametrize("token", ["master_7", "master-7", "master_7_force", "holder-token"])
def test_host_v2_route_binds_master_and_holder_tokens_to_current_tag(monkeypatch, state, token):
    state.origin = "host"
    state.dev = dm.VirtualDevice(7, "host", "7")
    monkeypatch.setattr(dm, "_get_master_token", lambda: "master")
    monkeypatch.setattr(dm, "validate_token", lambda salt, hsh, value: value == "holder-token")
    route = hd.HostDynamicV2Proxy().route_for_token(token)
    assert route[0] == 7
    assert route[1].reservation_tag == dm.reservation_tag("s", "h")
    assert route[1] is not state.dev


def test_host_v2_route_does_not_follow_holder_token_across_handover(monkeypatch, state):
    state.origin = "host"
    state.dev = dm.VirtualDevice(7, "host", "7")
    monkeypatch.setattr(dm, "_get_master_token", lambda: "master")
    monkeypatch.setattr(dm, "validate_token", lambda salt, hsh, value: (salt, hsh, value) == ("s", "h", "holder-token"))
    acquire = dm.acquire_device

    def handover(token):
        state.salt, state.hsh = "next", "holder"
        return acquire(token)

    monkeypatch.setattr(hd, "acquire_device", handover)
    assert hd.HostDynamicV2Proxy().route_for_token("holder-token") is None
