import asyncio
import json
import sys
import threading
import types
from unittest.mock import Mock

import pytest

sys.modules.setdefault("adi", types.ModuleType("adi"))
from remoterf_device_core.drivers.adalm_pluto import pluto_schema as core
from remoterf_device_core.v2_errors import RemoteRFPolicyError
from remoteRF_host.common.grpc import grpc_host_pb2 as pb, grpc_pb2
from remoteRF_host.common.utils import map_arg, unmap_arg
from remoteRF_host.server import device_manager as dm, rpc_manager as rpc, tunnel_agent as ta

MESSAGE = "The reservation on this device changed; call again."


class PlutoSchema:
    lockout_bypass = False
    make_device = Mock(return_value=object())

    def bind(self, raw):
        self.device = raw

    def configure_lockouts(self, cfg):
        self.cfg = cfg

    def dispatch(self, name, args):
        return None


@pytest.mark.parametrize("problem", ["bad_config", "stale", "override", "non_pluto", "missing_configure", None])
def test_load_checks_before_open_and_configures_after_bind(monkeypatch, problem):
    parser = Mock(return_value={"tx_enabled": False})
    monkeypatch.setattr(core, "parse_lockouts", parser, raising=False)
    monkeypatch.setattr(core, "LOCKOUT_KEYS", frozenset({"tx_enabled"}), raising=False)
    cls = type("Served", (PlutoSchema,), {})
    if problem == "bad_config":
        parser.side_effect = ValueError("bad range")
    elif problem in ("stale", "override"):
        cls.dispatch = lambda *args: None
    elif problem == "non_pluto":
        cls = type("Unrelated", (), {})
    elif problem == "missing_configure":
        cls.configure_lockouts = None
    cls.make_device = Mock(return_value=object())
    warn = Mock()
    monkeypatch.setattr(dm, "warn", warn)
    monkeypatch.setattr(dm, "get_driver_class", lambda _: cls)
    schema = dm._connect_from_record({"TYPE": "fake", "NAME": "Roof", "INIT": {"tx_enabled": False}})[0]
    if problem:
        assert schema is None
        cls.make_device.assert_not_called()
        assert "device left offline" in warn.call_args.args[0]
        assert "test_pluto_lockout_plumbing.py" in warn.call_args.args[0]
    else:
        assert schema.device is not None and schema.cfg == {"tx_enabled": False}


@pytest.fixture
def state(monkeypatch):
    schema = types.SimpleNamespace(lockout_bypass=False, device_type="fake", dispatch=Mock())
    st = dm._DeviceState(schema, "Roof", "", "fake", {})
    monkeypatch.setattr(dm, "_devices", {7: st})
    monkeypatch.setattr(dm, "_reservation_end_callbacks", [])
    return st


@pytest.mark.parametrize("payload,expected", [({}, False), ({"lockout_bypass": True}, True),
    ({"lockout_bypass": False}, False), ({"lockout_bypass": "true"}, False), ([], False)])
def test_payload_sets_bit_and_epoch_synchronously(monkeypatch, state, payload, expected):
    monkeypatch.setattr(dm, "_run_hook", Mock())
    agent = ta.HostTunnelAgent.__new__(ta.HostTunnelAgent)
    agent._devices = {7: {}}
    agent._handle_control(pb.Control(kind="reservation_start", device_id="7", payload=json.dumps(payload)))
    assert state.lockout_bypass is expected and state.epoch == 1
    assert not state.schema.lockout_bypass
    dm._run_hook.assert_called_once_with(7, "prepare_for_holder", "preparing")


def test_end_clears_before_callbacks_and_queued_reset(monkeypatch, state):
    state.schema.lockout_bypass = True
    observed = []
    monkeypatch.setattr(dm, "_reservation_end_callbacks", [lambda _: observed.append((state.schema.lockout_bypass, state.reset_pending, state.epoch))])
    monkeypatch.setattr(dm, "_run_hook", lambda *a: observed.append((state.schema.lockout_bypass, state.reset_pending, state.epoch)))
    dm.end_reservation(7)
    assert observed == [(False, True, 1), (False, True, 1)]


def test_pending_reset_runs_once_before_rpc_and_after_hook(monkeypatch, state):
    queued = []
    from remoterf_device_core import native_lifetime
    monkeypatch.setattr(native_lifetime, "submit_cleanup", lambda ready, run: queued.append(run))
    events = []
    state.schema.reset_for_next_holder = lambda: events.append("reset")
    state.schema.lockout_after_hook = lambda kind, failed: events.append((kind, failed))
    state.schema.dispatch.side_effect = lambda *a: events.append("dispatch")
    state.schema.lockout_bypass = True
    future = dm.end_reservation(7)
    assert not future.done() and not state.schema.lockout_bypass and state.reset_pending
    out = rpc.RpcManager().run_rpc(function_name="fake:tx:CALL0", args={"g": map_arg(7)})
    assert "Error" not in out
    assert events == ["reset", ("reset", False), "dispatch"]
    queued[0]()
    assert future.done() and events == ["reset", ("reset", False), "dispatch"]


def test_epoch_mismatch_refuses_queued_rpc(monkeypatch, state):
    waiting = threading.Event()
    lock = threading.Lock()
    lock.acquire()
    class QueueLock:
        def __enter__(self):
            waiting.set()
            lock.acquire()
        def __exit__(self, *args):
            lock.release()
    state.io_lock = QueueLock()
    result = []
    worker = threading.Thread(target=lambda: result.append(rpc.RpcManager().run_rpc(
        function_name="fake:tx:CALL0", args={"g": map_arg(7)})))
    worker.start()
    assert waiting.wait(2)
    dm.set_lockout_bypass(7, True)
    lock.release()
    worker.join(2)
    assert not worker.is_alive()
    assert unmap_arg(result[0]["Error"]) == MESSAGE
    state.schema.dispatch.assert_not_called()


@pytest.mark.parametrize("kind", ["reset", "prepare"])
@pytest.mark.parametrize("failed", [False, True])
def test_after_hook_even_if_driver_hook_raises(monkeypatch, state, kind, failed):
    from remoterf_device_core import native_lifetime
    monkeypatch.setattr(native_lifetime, "submit_cleanup", lambda ready, run: run())
    hook = Mock(side_effect=RuntimeError("hook failed") if failed else None)
    after = state.schema.lockout_after_hook = Mock()
    if kind == "reset":
        state.schema.reset_for_next_holder = hook
        future = dm.end_reservation(7)
    else:
        state.schema.prepare_for_holder = hook
        future = dm.prepare_reservation(7)
    future.result(1)
    after.assert_called_once_with(kind, failed=failed)


def test_tunnel_loss_clears_even_if_session_cleanup_fails(monkeypatch, state):
    from remoteRF_host.server import dynamic_v2_bridge
    agent = ta.HostTunnelAgent.__new__(ta.HostTunnelAgent)
    agent._devices = {7: {}}
    closed = []
    agent._direct_host = types.SimpleNamespace(close_all=lambda: closed.append(state.epoch))
    agent._relay_supervisor = None
    agent._stop = threading.Event()
    agent._host_id = "host"
    agent._host_token = "token"
    agent.cfg = types.SimpleNamespace(version="test", server_host="lab", server_port=1,
        ca_pem_path="ca", reconnect_min_delay_sec=0.01, reconnect_max_delay_sec=0.01, heartbeat_interval_sec=60)
    for name in ("_reload_from_env", "_drain_outbound_queue", "_make_channel", "_outbound_generator", "_enqueue"):
        monkeypatch.setattr(agent, name, Mock())
    monkeypatch.setattr(agent, "_device_announce_frame", Mock(return_value=pb.HostFrame()))
    monkeypatch.setattr(ta.host_pb2_grpc, "HostTunnelStub", lambda *a: types.SimpleNamespace(Connect=lambda *a: iter([pb.HostFrame(heartbeat=pb.Heartbeat())])))
    monkeypatch.setattr(dynamic_v2_bridge, "close_all_bridge_sessions", Mock(side_effect=RuntimeError("cleanup failed")))
    monkeypatch.setattr(dm, "_run_hook", Mock())
    monkeypatch.setattr(ta.time, "sleep", lambda _: agent._stop.set() if threading.current_thread() is threading.main_thread() else None)
    state.schema.lockout_bypass = True
    agent.run_forever()
    assert state.schema.lockout_bypass is False and state.reset_pending and state.epoch == 1
    assert closed == [0]


def test_cli_round_trip(monkeypatch, tmp_path):
    from remoteRF_host import hostrf_cli as cli
    init = {key: value for key, value in zip(
        ("tx_min_freq", "tx_max_freq", "admin_group_override", "tx_max_gain_db", "tx_enabled", "park_tx_lo", "allow_rf_loopback", "allow_dds", "tx_max_dds_scale", "allow_cyclic_tx", "tx_max_buffer_samples", "max_sample_rate", "tx_allowed_channels", "rx_min_freq", "rx_max_freq", "lockout_allow_methods"),
        ("902e6", 928e6, True, -10, True, 915e6, False, False, 0.2, False, 100, 3e6, [0], 70e6, 6e9, ["call_camera_capture"]))}
    monkeypatch.setattr(cli, "_devices_yaml_path", lambda: tmp_path / "devices.yml")
    cli._write_device_records({7: {"device_type": "adalm_pluto", "init": init}})
    assert cli._read_device_records()[7]["init"] == init


def test_direct_host_preserves_policy_error(state, tmp_path):
    from remoteRF_host.server.direct_host import DirectHost
    state.schema.dispatch.side_effect = RemoteRFPolicyError("TX is disabled on this device (receive-only).")
    manager = rpc.RpcManager()
    direct = DirectHost(host_id="host", key_path=tmp_path / "key", run_call=lambda gid, request, expected_epoch=None:
        manager.run_rpc(function_name=request.function_name, args={"g": map_arg(gid)}, expected_epoch=expected_epoch))
    direct._refusal = lambda *args: None
    request = grpc_pb2.GenericRPCRequest(function_name="fake:tx:CALL0")
    results, _ = asyncio.run(direct._serve_call(7, "reservation", request))
    assert unmap_arg(results["Error"]) == "TX is disabled on this device (receive-only)."


def test_stale_queued_prepare_cannot_boot_after_end(monkeypatch, state):
    from remoterf_device_core import native_lifetime
    queued = []
    monkeypatch.setattr(native_lifetime, "submit_cleanup", lambda ready, run: queued.append(run))
    state.schema.prepare_for_holder = Mock()
    state.schema.reset_for_next_holder = Mock()
    state.schema.lockout_after_hook = Mock()
    first = dm.prepare_reservation(7, bypass=True)
    second = dm.end_reservation(7)
    queued[0]()
    queued[1]()
    assert first.done() and second.done()
    state.schema.prepare_for_holder.assert_not_called()
    state.schema.reset_for_next_holder.assert_called_once()
    state.schema.lockout_after_hook.assert_called_once_with("reset", failed=False)


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


def test_reset_only_schema_resets_across_end_then_start(monkeypatch, state):
    from remoterf_device_core import native_lifetime
    queued = []
    monkeypatch.setattr(native_lifetime, "submit_cleanup", lambda ready, run: queued.append(run))
    state.schema.reset_for_next_holder = Mock()
    future = dm.end_reservation(7)
    assert dm.prepare_reservation(7) is None
    queued[0]()
    future.result(1)
    state.schema.reset_for_next_holder.assert_called_once()
    assert not state.reset_pending


def test_tunnel_epoch_is_captured_before_worker_starts(monkeypatch, state):
    from remoteRF_host.server import dynamic_v2_bridge
    agent = ta.HostTunnelAgent.__new__(ta.HostTunnelAgent)
    agent._devices = {7: {}}
    agent._direct_host = None
    agent._relay_supervisor = None
    agent._stop = threading.Event()
    agent._cancel_lock = threading.Lock()
    agent._cancel_flags = {}
    agent._host_id, agent._host_token = "host", "token"
    agent.cfg = types.SimpleNamespace(version="test", server_host="lab", server_port=1,
        ca_pem_path="ca", reconnect_min_delay_sec=0.01, reconnect_max_delay_sec=0.01, heartbeat_interval_sec=60)
    for name in ("_reload_from_env", "_drain_outbound_queue", "_make_channel", "_outbound_generator", "_enqueue"):
        monkeypatch.setattr(agent, name, Mock())
    monkeypatch.setattr(agent, "_device_announce_frame", Mock(return_value=pb.HostFrame()))
    monkeypatch.setattr(dm, "_run_hook", Mock())
    monkeypatch.setattr(dynamic_v2_bridge, "close_all_bridge_sessions", lambda: 0)
    workers = []
    def thread(*, target, args=(), **kwargs):
        if kwargs.get("name", "").startswith("rpc_"):
            return types.SimpleNamespace(start=lambda: workers.append(lambda: target(*args)))
        return types.SimpleNamespace(start=lambda: None)
    monkeypatch.setattr(ta.threading, "Thread", thread)
    def stream():
        yield pb.HostFrame(rpc_request=pb.RpcRequest(req_id="old", device_id="7",
            request=grpc_pb2.GenericRPCRequest(function_name="fake:tx:CALL0")))
        yield pb.HostFrame(control=pb.Control(kind="reservation_end", device_id="7"))
        yield pb.HostFrame(control=pb.Control(kind="reservation_start", device_id="7",
            payload=json.dumps({"lockout_bypass": True})))
        assert state.epoch == 2
        workers[0]()
    monkeypatch.setattr(ta.host_pb2_grpc, "HostTunnelStub", lambda *a: types.SimpleNamespace(Connect=lambda *a: stream()))
    monkeypatch.setattr(ta.time, "sleep", lambda _: agent._stop.set())
    agent.run_forever()
    replies = [call.args[0].rpc_response for call in agent._enqueue.call_args_list
        if call.args[0].WhichOneof("msg") == "rpc_response"]
    assert len(replies) == 1
    assert unmap_arg(replies[0].response.results["Error"]) == MESSAGE
    state.schema.dispatch.assert_not_called()


def test_direct_epoch_is_captured_at_grant_check(monkeypatch, state, tmp_path):
    from remoteRF_host.server.direct_host import DirectHost
    agent = ta.HostTunnelAgent.__new__(ta.HostTunnelAgent)
    direct = DirectHost(host_id="host", key_path=tmp_path / "key", run_call=agent._run_direct_call)
    checks = []
    def refusal(*args):
        checks.append(True)
        if len(checks) == 1:
            dm.set_lockout_bypass(7, True)
        return None
    direct._refusal = refusal
    results, drop = asyncio.run(direct._serve_call(7, "old", grpc_pb2.GenericRPCRequest(function_name="fake:tx:CALL0")))
    assert not drop and unmap_arg(results["Error"]) == MESSAGE
    state.schema.dispatch.assert_not_called()


@pytest.mark.parametrize("timing", ["early_next", "late_previous", "batch_tail"])
@pytest.mark.parametrize("lockouts", [False, True])
def test_reservation_tag_refuses_reordered_frames(monkeypatch, state, timing, lockouts):
    monkeypatch.setattr(dm, "_run_hook", Mock())
    events = []
    state.schema.reset_for_next_holder = lambda: events.append("reset")
    if lockouts:
        state.schema.lockout_after_hook = lambda *a, **kw: None
    state.schema.dispatch.side_effect = lambda *a: events.append(("call", state.schema.lockout_bypass))
    dm.prepare_reservation(7, bypass=timing == "early_next", tag="holder-a")
    manager = rpc.RpcManager()
    def call(tag):
        return manager.run_rpc(function_name="fake:tx:CALL0", args={
            "g": map_arg(7), "__rrf_reservation_tag": map_arg(tag)})
    if timing == "batch_tail":
        assert "Error" not in call("holder-a")
        assert events == [("call", False)]
        events.clear()
    if timing == "early_next":
        tag = "holder-b"
    else:
        dm.end_reservation(7)
        dm.prepare_reservation(7, bypass=True, tag="holder-b")
        tag = "holder-a"
    before = state.reset_pending
    assert unmap_arg(call(tag)["Error"]) == MESSAGE
    assert state.reset_pending == before
    assert events == []


def test_untagged_old_lab_keeps_epoch_behavior_and_tag_is_stripped(monkeypatch, state):
    monkeypatch.setattr(dm, "_run_hook", Mock())
    state.schema.dispatch.return_value = None
    dm.prepare_reservation(7, bypass=True, tag="holder")
    manager = rpc.RpcManager()
    assert "Error" not in manager.run_rpc(function_name="fake:custom:CALLN", args={"g": map_arg(7)})
    assert "Error" not in manager.run_rpc(function_name="fake:custom:CALLN", args={
        "g": map_arg(7), "__rrf_reservation_tag": map_arg("holder"), "x": map_arg(1)})
    state.schema.dispatch.assert_called_with("call_custom", {"x": 1})


def test_resync_restores_tag_and_bypass_without_prepare(monkeypatch, state):
    monkeypatch.setattr(dm, "_run_hook", Mock())
    agent = ta.HostTunnelAgent.__new__(ta.HostTunnelAgent)
    agent._devices = {7: {}}
    agent._handle_control(pb.Control(kind="reservation_start", device_id="7", payload=json.dumps({
        "tag": "current", "lockout_bypass": True, "resync": True})))
    assert state.reservation_tag == "current" and state.lockout_bypass
    dm._run_hook.assert_not_called()
    state.schema.dispatch.return_value = None
    out = rpc.RpcManager().run_rpc(function_name="fake:tx:CALL0", args={
        "g": map_arg(7), "__rrf_reservation_tag": map_arg("current")})
    assert "Error" not in out
    dm.end_reservation(7)
    assert state.reservation_tag == "" and not state.schema.lockout_bypass


@pytest.mark.parametrize("signed_tag", [None, "holder-a"])
def test_direct_grant_is_bound_to_reservation_tag(monkeypatch, state, tmp_path, signed_tag):
    import time
    from remoteRF_host.server.direct_host import DirectHost, GRANT_ENDED
    monkeypatch.setattr(dm, "_run_hook", Mock())
    state.schema.dispatch.return_value = None
    direct = DirectHost(host_id="host", key_path=tmp_path / "key", run_call=ta.HostTunnelAgent.__new__(ta.HostTunnelAgent)._run_direct_call)
    claims = {"rid": "reservation", "jti": "grant", "iat": time.time(), "exp": time.time() + 60}
    if signed_tag is not None:
        claims["reservation_tag"] = signed_tag
    monkeypatch.setattr(direct, "verify", lambda *a: dict(claims))
    dm.prepare_reservation(7, bypass=True, tag="holder-a")
    direct.refresh(7, "grant")
    request = grpc_pb2.GenericRPCRequest(function_name="fake:tx:CALL0", args={
        "__rrf_reservation_tag": map_arg("forged")})
    out, drop = asyncio.run(direct._serve_call(7, "reservation", request))
    assert not drop and "Error" not in out
    state.schema.dispatch.assert_called_once()
    state.schema.dispatch.reset_mock()
    dm.prepare_reservation(7, bypass=True, tag="holder-b")
    # Refreshing an old grant cannot rebind it to the new holder.
    direct.refresh(7, "grant")
    out, drop = asyncio.run(direct._serve_call(7, "reservation", request))
    assert drop and unmap_arg(out["a"]) == GRANT_ENDED
    state.schema.dispatch.assert_not_called()


def test_next_holder_direct_grant_cannot_use_previous_admin_bypass(monkeypatch, state, tmp_path):
    import time
    from remoteRF_host.server.direct_host import DirectHost, GRANT_ENDED
    monkeypatch.setattr(dm, "_run_hook", Mock())
    dm.prepare_reservation(7, bypass=True, tag="holder-a")
    direct = DirectHost(host_id="host", key_path=tmp_path / "key", run_call=Mock())
    monkeypatch.setattr(direct, "verify", lambda *a: {"rid": "next", "jti": "grant", "iat": time.time(),
        "exp": time.time() + 60, "reservation_tag": "holder-b"})
    direct.refresh(7, "grant")
    out, drop = asyncio.run(direct._serve_call(7, "next", grpc_pb2.GenericRPCRequest(function_name="fake:tx:CALL0")))
    assert drop and unmap_arg(out["a"]) == GRANT_ENDED
    direct.run_call.assert_not_called()


def test_handover_after_admission_cannot_elevate_rpc_bypass(monkeypatch, state):
    monkeypatch.setattr(dm, "_run_hook", Mock())
    dm.prepare_reservation(7, tag="holder-a")
    def handover(st):
        dm.end_reservation(7)
        dm.prepare_reservation(7, bypass=True, tag="holder-b")
    monkeypatch.setattr(dm, "_reset_locked", handover)
    observed = []
    state.schema.dispatch.side_effect = lambda *a: observed.append(state.schema.lockout_bypass)
    out = rpc.RpcManager().run_rpc(function_name="fake:tx:CALL0", args={
        "g": map_arg(7), "__rrf_reservation_tag": map_arg("holder-a")})
    assert "Error" not in out and observed == [False]
    assert state.lockout_bypass


@pytest.mark.parametrize("tag", [None, 1, True, b"holder"])
def test_malformed_tag_cannot_select_legacy_behavior(state, tag):
    state.reservation_tag = "holder"
    state.reset_pending = True
    argument = grpc_pb2.Argument() if tag is None else map_arg(tag)
    out = rpc.RpcManager().run_rpc(function_name="fake:tx:CALL0", args={
        "g": map_arg(7), "__rrf_reservation_tag": argument})
    assert unmap_arg(out["Error"]) == MESSAGE and state.reset_pending
    state.schema.dispatch.assert_not_called()


def test_tagged_v2_operations_can_run_full_duplex(monkeypatch, state):
    state.reservation_tag = "holder"
    entered, release, second = (threading.Event() for _ in range(3))
    manager = rpc.RpcManager()
    results = []

    def dispatch(*, function_name, args):
        assert not state.io_lock._is_owned()
        assert "__rrf_reservation_tag" not in args
        if function_name == "DynamicV2:SampleFrame":
            entered.set()
            assert release.wait(5)
        else:
            second.set()
        return {}

    monkeypatch.setattr(manager, "_run_admitted", dispatch)
    args = {"g": map_arg(7), "__rrf_reservation_tag": map_arg("holder")}
    rx = threading.Thread(target=lambda: results.append(manager.run_rpc(function_name="DynamicV2:SampleFrame", args=args)))
    tx = threading.Thread(target=lambda: results.append(manager.run_rpc(function_name="DynamicV2:Invoke", args=args)))
    try:
        rx.start()
        assert entered.wait(2)
        tx.start()
        assert second.wait(2)
    finally:
        release.set()
        rx.join(2)
        if tx.ident is not None:
            tx.join(2)
    assert not rx.is_alive() and not tx.is_alive()
    assert results == [{}, {}]


@pytest.mark.parametrize("stale", ["tag", "epoch"])
def test_refused_tagged_v2_frame_preserves_pending_reset(monkeypatch, state, stale):
    state.reservation_tag = "holder"
    state.epoch = 2
    state.reset_pending = True
    reset = Mock()
    state.schema.reset_for_next_holder = reset
    manager = rpc.RpcManager()
    dispatch = Mock()
    monkeypatch.setattr(manager, "_run_admitted", dispatch)
    out = manager.run_rpc(function_name="DynamicV2:Invoke", args={
        "g": map_arg(7), "__rrf_reservation_tag": map_arg("old" if stale == "tag" else "holder")},
        expected_epoch=1 if stale == "epoch" else 2)
    assert unmap_arg(out["Error"]) == MESSAGE
    assert state.reset_pending
    reset.assert_not_called()
    dispatch.assert_not_called()


def test_v1_operation_keeps_io_lock_for_dispatch(monkeypatch, state):
    manager = rpc.RpcManager()
    observed = []
    monkeypatch.setattr(manager, "_run_admitted", lambda **kw: observed.append(state.io_lock._is_owned()) or {})
    assert manager.run_rpc(function_name="fake:tx:CALL0", args={"g": map_arg(7)}) == {}
    assert observed == [True]


def test_reload_preserves_latest_holder_state_and_pending_reset(monkeypatch, state):
    state.epoch = 8
    state.reservation_tag = "old"
    state.reset_pending = True
    fresh = types.SimpleNamespace(lockout_bypass=False, device_type="fake", dispatch=Mock(return_value=None))
    events = []
    fresh.reset_for_next_holder = lambda: events.append("reset")
    monkeypatch.setattr(dm, "_reload_signature", lambda: ("new",))
    monkeypatch.setattr(dm, "_load_device_records", lambda: {7: {"NAME": "Roof", "TYPE": "fake", "INIT": {}}})
    monkeypatch.setattr(dm, "_load_all_drivers", lambda _: None)

    def connect(record):
        # A control frame can update the holder while the new schema opens.
        dm.set_lockout_bypass(7, True, "current")
        return fresh, "", "fake"

    monkeypatch.setattr(dm, "_connect_from_record", connect)
    assert dm._reload_devices_unfenced(force=True)
    current = dm._devices[7]
    assert current is not state and current.schema is fresh
    assert current.reservation_tag == "current" and current.lockout_bypass
    assert current.epoch == 9 and current.reset_pending
    assert current.io_lock is state.io_lock
    manager = rpc.RpcManager()
    args = {"g": map_arg(7), "__rrf_reservation_tag": map_arg("current")}
    stale = manager.run_rpc(function_name="fake:tx:CALL0", args=args, expected_epoch=8)
    assert unmap_arg(stale["Error"]) == MESSAGE
    assert current.reset_pending and events == []
    out = manager.run_rpc(function_name="fake:tx:CALL0", args=args, expected_epoch=9)
    assert "Error" not in out and fresh.lockout_bypass
    assert not current.reset_pending and events == ["reset"]


def test_tagged_v2_open_stays_in_one_hold_and_leaves_reset_to_hook(monkeypatch, state):
    state.reservation_tag = "holder"
    state.reset_pending = True
    reset = Mock()
    state.schema.reset_for_next_holder = reset
    manager = rpc.RpcManager()
    observed = []
    monkeypatch.setattr(manager, "_run_admitted", lambda **kw: observed.append((kw["function_name"], state.io_lock._is_owned())) or {})
    args = {"g": map_arg(7), "__rrf_reservation_tag": map_arg("holder")}
    assert manager.run_rpc(function_name="DynamicV2:OpenSession", args=args) == {}
    assert manager.run_rpc(function_name="DynamicV2:Invoke", args=args) == {}
    assert observed == [("DynamicV2:OpenSession", True), ("DynamicV2:Invoke", False)]
    reset.assert_not_called()
    assert state.reset_pending


def test_reset_failure_is_logged_and_does_not_fail_next_holder_rpc(monkeypatch, state):
    warn = Mock()
    monkeypatch.setattr(dm, "warn", warn)
    state.reset_pending = True
    state.schema.reset_for_next_holder = Mock(side_effect=RuntimeError("serial glitch"))
    after = state.schema.lockout_after_hook = Mock()
    state.schema.dispatch.return_value = "ok"

    out = rpc.RpcManager().run_rpc(function_name="fake:tx:CALL0", args={"g": map_arg(7)})

    assert "Error" not in out
    assert unmap_arg(out["tx"]) == "ok"
    assert not state.reset_pending
    after.assert_called_once_with("reset", failed=True)
    assert any("serial glitch" in call.args[0] for call in warn.call_args_list)

    # Parity with server: a failing reset still allows prepare_for_holder to run,
    # and a failing lockout_after_hook during reset is also logged and contained.
    from remoterf_device_core import native_lifetime
    monkeypatch.setattr(native_lifetime, "submit_cleanup", lambda ready, run: run())
    warn.reset_mock()
    state.reset_pending = True

    def fail_reset_lockout(kind, failed=False):
        if kind == "reset":
            raise RuntimeError("park failed")

    after_prepare = state.schema.lockout_after_hook = Mock(side_effect=fail_reset_lockout)
    prepare = state.schema.prepare_for_holder = Mock()
    future = dm.prepare_reservation(7)
    future.result(1)
    assert not state.reset_pending
    prepare.assert_called_once()
    assert [c.args for c in after_prepare.call_args_list] == [("reset",), ("prepare",)]
    assert [c.kwargs for c in after_prepare.call_args_list] == [{"failed": True}, {"failed": False}]
    assert any(
        "lockout cleanup failed" in call.args[0] and "park failed" in call.args[0]
        for call in warn.call_args_list
    )
    assert not any("preparing gid=7 failed" in call.args[0] for call in warn.call_args_list)
