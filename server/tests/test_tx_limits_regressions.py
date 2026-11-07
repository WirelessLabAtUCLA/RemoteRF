"""Regression tests execute the real schemas with simulated radio hardware."""

import contextlib
import sys
import types
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import yaml

sys.modules.setdefault("adi", types.ModuleType("adi"))
from remoterf_device_core.drivers.adalm_pluto.pluto_schema import PlutoSchema
from remoterf_device_core.drivers.hackrf.hackrf_schema import HackRFSchema
from remoterf_device_core.drivers.usrp.usrp_schema import UsrpSchema
from remoterf_device_core.tx_limits import (
    POLICY_KEY,
    host_rule,
    install_host_rule,
    parse_limits,
    parse_tx_freq,
)
from remoteRF_server.common.utils import map_arg, unmap_arg
from remoteRF_server.server import device_manager as dm
from remoteRF_server.server import rpc_manager as rm
from remoteRF_server.server.dynamic_v2 import RemoteRFPolicyError, SessionManager

RULE = parse_tx_freq({"whitelist": [[2.4e9, 2.48e9]], "blacklist": [[2.45e9, 2.46e9]]})


def radio(dtype, freq):
    calls = []
    if dtype == "hackrf":
        native = types.SimpleNamespace(
            center_freq=freq,
            buffer=bytearray(b"ab"),
            start_tx=lambda: calls.append("start"),
            _remoterf_tx_active=False,
        )
        schema = HackRFSchema()
    elif dtype == "usrp":
        native = types.SimpleNamespace(
            set_tx_freq=lambda f, chan=0: calls.append(f),
            get_tx_num_channels=lambda: 2,
            get_tx_freq=lambda chan: freq,
        )
        schema = UsrpSchema()
    else:
        native = types.SimpleNamespace(
            tx_lo=freq,
            tx=lambda v: calls.append("tx"),
            dds_single_tone=lambda *a: calls.append("dds"),
        )
        schema = PlutoSchema()
    schema.bind(native)
    return schema, calls


def local(schema, function, args):
    with patch.object(rm, "get_tx_freq_limit", return_value=RULE):
        return rm.RpcManager()._dispatch_acquired(
            gid=701,
            dev=schema,
            function_name=function,
            args={k: map_arg(v) for k, v in args.items()},
        )


@pytest.mark.parametrize(
    "prop,verb,args",
    [
        ("tx", "CALL1", {"arg1": [1, 2]}),
        ("tx", "CALLN", {"value": [1, 2]}),
        ("dds_single_tone", "CALLN", {"frequency": 1000, "scale": 0.1}),
    ],
)
def test_pluto_existing_disallowed_lo_cannot_start_tx(prop, verb, args):
    schema, calls = radio("adalm_pluto", 1e9)
    with pytest.raises(ValueError, match="not allowed"):
        local(schema, f"adalm_pluto:{prop}:{verb}", args)
    assert not calls


def test_pluto_allowed_tx_and_blocked_retune():
    schema, calls = radio("adalm_pluto", 2.42e9)
    local(schema, "adalm_pluto:tx:CALL1", {"arg1": [1, 2]})
    assert calls == ["tx"]
    with pytest.raises(ValueError):
        local(schema, "adalm_pluto:tx_lo:SET", {"tx_lo": 1e9})
    assert schema.device.tx_lo == 2.42e9


@pytest.mark.parametrize("verb", ["CALL0", "CALLN"])
def test_hackrf_all_start_spellings_check_frequency(verb):
    schema, calls = radio("hackrf", 1e9)
    with pytest.raises(ValueError):
        local(schema, f"hackrf:start_tx:{verb}", {})
    assert not calls


@pytest.mark.parametrize(
    "verb,args", [("CALL1", {"arg1": 1e9}), ("CALLN", {"value": 1e9})]
)
def test_hackrf_all_retune_spellings_check_active_tx(verb, args):
    schema, calls = radio("hackrf", 2.42e9)
    local(schema, "hackrf:start_tx:CALLN", {})
    with pytest.raises(ValueError):
        local(schema, f"hackrf:set_freq:{verb}", args)
    assert schema.device.center_freq == 2.42e9
    assert calls == ["start"]


def test_hackrf_rx_tuning_is_unrestricted_but_start_is_checked():
    schema, calls = radio("hackrf", 2.42e9)
    local(schema, "hackrf:set_freq:CALLN", {"value": 1e9})
    assert schema.device.center_freq == 1e9
    with pytest.raises(ValueError):
        local(schema, "hackrf:start_tx:CALL0", {})


def test_legacy_usrp_setter_cannot_bypass_policy():
    schema, calls = radio("usrp", 2.42e9)
    with pytest.raises(ValueError):
        local(schema, "usrp:set_tx_freq:CALLN", {"tune_request": 1e9})
    local(schema, "usrp:set_tx_freq:CALLN", {"tune_request": 2.42e9})
    assert calls == [2.42e9]


@contextlib.contextmanager
def usrp_session():
    from test_dynamic_v2 import FakeInventorySchema

    inv = FakeInventorySchema()
    context = {"schema": inv, "dtype": "usrp", "ident": "SERIAL", "name": "test"}
    manager = SessionManager()
    p = "remoteRF_server.server.dynamic_v2."
    with (
        patch(p + "resolve_gid_for_token", return_value=702),
        patch(p + "get_local_device_context", return_value=context),
        patch(p + "token_matches_device", return_value=True),
        patch(p + "get_tx_freq_limit", return_value=RULE),
    ):
        session = manager.open("token")
        inv.live.device.get_tx_freq = lambda chan: 2.42e9
        inv.live.device.get_tx_num_channels = lambda: 2
        try:
            yield manager, session, inv.live
        finally:
            manager.close(session.session_id)


@pytest.mark.parametrize(
    "method,args",
    [
        (
            "send_waveform",
            {"waveform_proto": np.array([1 + 0j]), "duration": 0.01, "freq": 1e9},
        ),
        ("set_tx_lo_freq", {"freq": 1e9, "name": "all"}),
        ("get_tree", {}),
        (
            "set_tx_freq",
            {
                "tune_request": {
                    "__uhd_type__": "TuneRequest",
                    "target_freq": 2.42e9,
                    "args": [1e9],
                }
            },
        ),
        (
            "set_tx_freq",
            {
                "tune_request": {
                    "__uhd_type__": "TuneRequest",
                    "target_freq": 2.42e9,
                    "rf_freq": 1e9,
                }
            },
        ),
    ],
)
def test_usrp_v2_rejects_alternate_and_ambiguous_tuning(method, args):
    with usrp_session() as (manager, session, live):
        with pytest.raises(RemoteRFPolicyError):
            manager.invoke(
                session.session_id,
                session.root_handle,
                method,
                args,
                overload_id="default",
            )
        assert not live.calls


def test_usrp_normalizes_scalar_and_structured_targets():
    with usrp_session() as (manager, session, live):
        manager.invoke(
            session.session_id,
            session.root_handle,
            "set_tx_freq",
            {"tune_request": 2.42e9},
            overload_id="default",
        )
        manager.invoke(
            session.session_id,
            session.root_handle,
            "set_tx_freq",
            {"tune_request": {"__uhd_type__": "TuneRequest", "args": [2.42e9]}},
            overload_id="default",
        )
        assert live.calls[0][1]["tune_request"] == 2.42e9
        assert live.calls[1][1]["tune_request"] == 2.42e9


@pytest.mark.parametrize(
    "raw",
    [
        {"tx_freq": {"white_list": [[2.4e9, 2.48e9]]}},
        {"tx_freq": {"whitelist": []}},
        {"tx_freq": {"on_violation": "default", "default": 2.42e9}},
    ],
)
def test_bad_config_disables_tx(raw):
    rule = parse_limits(raw, gid=1)["tx_freq"]
    with pytest.raises(ValueError, match="invalid limits"):
        rule.check(2.42e9)


def test_host_policy_is_an_overlay_and_survives_directory_refresh(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("REMOTERF_CONFIG_DIR", str(tmp_path))
    (tmp_path / "devices.yml").write_text(
        yaml.safe_dump(
            {
                "host_devices": [
                    {
                        "host_id": "host-a",
                        "device_id": 703,
                        "limits": {"tx_freq": {"whitelist": [[2.4e9, 2.48e9]]}},
                    }
                ]
            }
        )
    )
    info = types.SimpleNamespace(kind="hackrf", serial="serial", label="test")
    registry = types.SimpleNamespace(
        device_directory_cached=lambda **kw: {"703": ("host-a", info, True)}
    )
    monkeypatch.setattr(dm, "_get_host_registry", lambda: registry)
    monkeypatch.setattr(dm, "_devices", {})
    assert dm._load_device_records() == {}
    for _ in range(2):
        dm._sync_host_devices(force=True)
        assert dm._devices[703].origin == "host"
        with pytest.raises(ValueError):
            dm.get_tx_freq_limit(703).check(1e9)


@pytest.mark.parametrize(
    "dtype,fn,args",
    [
        ("adalm_pluto", "adalm_pluto:tx:CALL1", {"arg1": [1, 2]}),
        ("hackrf", "hackrf:start_tx:CALLN", {}),
        ("usrp", "usrp:set_tx_freq:CALLN", {"tune_request": 1e9}),
    ],
)
@pytest.mark.parametrize("allowed", [False, True])
def test_server_host_roundtrip_enforces_actual_schema(
    dtype, fn, args, monkeypatch, allowed
):
    # The host source is in this monorepo; no host hardware or running service is used.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "host/src"))
    from remoteRF_host.server import rpc_manager as host_rm
    from remoteRF_server.server import host_tx_limits

    schema, calls = radio(dtype, 2.42e9 if allowed else 1e9)
    if allowed and dtype == "usrp":
        args = {"tune_request": 2.42e9}
    gid = 704
    monkeypatch.setattr(host_rm, "get_device", lambda **kw: schema)
    monkeypatch.setattr(host_tx_limits, "get_tx_freq_limit", lambda _: RULE)

    def tunnel(registry, **kw):
        payload = dict(kw["args"])
        payload["g"] = map_arg(gid)
        return host_rm.RpcManager().run_rpc(
            function_name=kw["function_name"], args=payload
        )

    monkeypatch.setattr(host_tx_limits.hts, "handle_host_device", tunnel)
    try:
        out = host_tx_limits.forward(
            None,
            gid=gid,
            host_id="host-a",
            device_id=str(gid),
            function_name=fn,
            args={
                **{k: map_arg(v) for k, v in args.items()},
                POLICY_KEY: map_arg("null"),
            },
        )
        if allowed:
            assert "Error" not in out
            assert calls
        else:
            assert "not allowed" in unmap_arg(out["Error"])
            assert not calls
        assert host_rule(gid) is not None
    finally:
        install_host_rule(gid, "null")


def test_old_host_is_refused_before_transmit(monkeypatch):
    from remoteRF_server.server import host_tx_limits

    calls = []
    monkeypatch.setattr(host_tx_limits, "get_tx_freq_limit", lambda _: RULE)
    monkeypatch.setattr(
        host_tx_limits.hts,
        "handle_host_device",
        lambda *a, **kw: calls.append(kw["function_name"]) or {},
    )
    with pytest.raises(ValueError, match="upgrade"):
        host_tx_limits.forward(
            None,
            gid=705,
            host_id="h",
            device_id="705",
            function_name="hackrf:start_tx:CALL0",
            args={},
        )
    assert calls == ["TXLimits:capabilities"]


@pytest.mark.parametrize("allowed", [False, True])
def test_local_usrp_stream_rechecks_actual_channel(monkeypatch, allowed):
    from remoteRF_server.common.grpc import grpc_pb2
    from remoteRF_server.server import dynamic_v2 as dv

    with usrp_session() as (manager, session, live):
        calls = []
        live.device.get_tx_freq = lambda chan: 2.42e9 if allowed or chan == 0 else 1e9
        monkeypatch.setattr(dv, "session_manager", manager)
        monkeypatch.setattr(manager, "_start_async_worker", lambda *a: None)
        monkeypatch.setattr(
            dv.SampleDataV1Servicer,
            "_metadata_object",
            staticmethod(lambda *a: object()),
        )
        streamer = types.SimpleNamespace(send=lambda *a: calls.append("send") or 1)
        entry = manager.register_handle(
            session, streamer, "uhd.usrp.TXStreamer", parent=session.root_handle
        )
        entry.stream_channels = [1]
        common = dict(
            session_id=session.session_id,
            handle=entry.handle,
            generation=entry.generation,
            operation_id="tx",
            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
        )
        frames = [
            grpc_pb2.SampleFrame(**common, kind=grpc_pb2.SAMPLE_FRAME_OPEN, sequence=1),
            grpc_pb2.SampleFrame(
                **common,
                kind=grpc_pb2.SAMPLE_FRAME_DATA,
                sequence=2,
                dtype="<c8",
                shape=[1],
                channels=[1],
                sample_count=1,
                payload=np.array([1 + 0j], dtype="<c8").tobytes(),
            ),
        ]
        responses = list(dv.SampleDataV1Servicer().SampleStream(iter(frames), None))
        assert bool(calls) == allowed
        assert bool(responses[-1].error.code) != allowed


def test_host_usrp_v2_roundtrip_checks_policy_before_native_dispatch(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "host/src"))
    from remoteRF_host.server import dynamic_v2 as host_dv
    from remoteRF_host.server import rpc_manager as host_rm
    from remoteRF_server.common.grpc import grpc_pb2
    from remoteRF_server.common.grpc.v2_codec import encode_value
    from remoteRF_server.server import host_dynamic_v2 as proxy_module
    from remoteRF_server.server import host_tx_limits
    from test_dynamic_v2 import FakeInventorySchema

    gid = 708
    inventory = FakeInventorySchema()
    monkeypatch.setattr(host_dv, "resolve_gid_for_token", lambda _: gid)
    monkeypatch.setattr(
        host_dv,
        "get_local_device_context",
        lambda _: {
            "schema": inventory,
            "dtype": "usrp",
            "ident": "SERIAL",
            "name": "test",
        },
    )
    monkeypatch.setattr(host_dv, "token_matches_device", lambda *a: True)
    monkeypatch.setattr(
        "remoteRF_host.server.dynamic_v2_bridge.make_host_bridge_token",
        lambda _: "token",
    )
    monkeypatch.setattr(host_tx_limits, "get_tx_freq_limit", lambda _: RULE)
    monkeypatch.setattr(proxy_module, "resolve_gid_for_token", lambda _: gid)
    device = dm.VirtualDevice(
        gid=gid, host_id="host-a", device_id=str(gid), kind="usrp"
    )
    monkeypatch.setattr(proxy_module, "get_device_by_id", lambda _: (device, "", ""))
    monkeypatch.setattr(
        proxy_module.hts, "get_tunnel_registry", lambda *a, **kw: object()
    )

    def tunnel(registry, **kw):
        args = dict(kw["args"])
        args["g"] = map_arg(gid)
        return host_rm.RpcManager().run_rpc(
            function_name=kw["function_name"], args=args
        )

    monkeypatch.setattr(host_tx_limits.hts, "handle_host_device", tunnel)
    proxy = proxy_module.HostDynamicV2Proxy()
    try:
        opened = proxy.open(
            grpc_pb2.OpenSessionRequest(
                token="token",
                control_protocol_version="2.0",
                streaming_protocol_version="1.0",
            )
        )
        assert not opened.error.code
        for freq in [1e9, 2.42e9]:
            request = grpc_pb2.InvokeRequest(
                session_id=opened.session_id,
                handle=opened.device_handle,
                method="set_tx_freq",
                overload_id="default",
            )
            request.args.add(name="tune_request").value.CopyFrom(encode_value(freq))
            response = proxy.invoke(request)
            if freq == 1e9:
                assert response.error.code == "RemoteRFPolicyError"
                assert not inventory.live.calls
            else:
                assert not response.error.code
                assert inventory.live.calls[0][1]["tune_request"] == freq
    finally:
        host_dv.session_manager.close_gid(gid)
        install_host_rule(gid, "null")
