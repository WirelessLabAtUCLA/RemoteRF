"""Host policy is supplied by the trusted server and also gates direct clients."""

import sys
import types

import numpy as np
import pytest

sys.modules.setdefault("adi", types.ModuleType("adi"))
from remoterf_device_core.drivers.hackrf.hackrf_schema import HackRFSchema
from remoterf_device_core.tx_limits import (
    POLICY_KEY,
    install_host_rule,
    parse_tx_freq,
    rule_wire,
)
from remoteRF_host.common.grpc import grpc_pb2
from remoteRF_host.common.grpc.v2_codec import encode_value
from remoteRF_host.common.utils import map_arg, unmap_arg
from remoteRF_host.server import dynamic_v2 as dv
from remoteRF_host.server import rpc_manager as rm
from remoteRF_host.server.dynamic_v2_bridge import close_all_bridge_sessions
from remoteRF_host.server.tunnel_agent import HostTunnelAgent

RULE = parse_tx_freq({"whitelist": [[2.4e9, 2.48e9]]})
GID = 706


@pytest.fixture(autouse=True)
def clean_policy():
    install_host_rule(GID, "null")
    yield
    close_all_bridge_sessions()
    install_host_rule(GID, "null")


def configure():
    result = rm.RpcManager().run_rpc(
        function_name="TXLimits:configure",
        args={"g": map_arg(GID), POLICY_KEY: map_arg(rule_wire(RULE))},
    )
    assert unmap_arg(result["tx_limits_version"]) == 1


def test_direct_client_cannot_erase_policy_or_configure_it(monkeypatch):
    schema = HackRFSchema()
    calls = []
    native = types.SimpleNamespace(
        center_freq=1e9, buffer=bytearray(b"a"), start_tx=lambda: calls.append("tx")
    )
    schema.bind(native)
    monkeypatch.setattr(rm, "get_device", lambda **kw: schema)
    configure()
    request = grpc_pb2.GenericRPCRequest(
        function_name="hackrf:start_tx:CALLN", args={POLICY_KEY: map_arg("null")}
    )
    result = HostTunnelAgent._run_direct_call(object(), GID, request)
    assert "not allowed" in unmap_arg(result["Error"])
    assert not calls
    request.function_name = "TXLimits:configure"
    with pytest.raises(ValueError, match="server tunnel"):
        HostTunnelAgent._run_direct_call(object(), GID, request)


def test_hackrf_policy_survives_missing_metadata_on_next_call(monkeypatch):
    schema = HackRFSchema()
    native = types.SimpleNamespace(
        center_freq=1e9, buffer=bytearray(b"a"), start_tx=lambda: None
    )
    schema.bind(native)
    monkeypatch.setattr(rm, "get_device", lambda **kw: schema)
    configure()
    for verb in ["CALL0", "CALLN"]:
        result = rm.RpcManager().run_rpc(
            function_name=f"hackrf:start_tx:{verb}", args={"g": map_arg(GID)}
        )
        assert "not allowed" in unmap_arg(result["Error"])


@pytest.fixture
def live_usrp(monkeypatch):
    from test_dynamic_v2_bridge import FakeInventorySchema

    inventory = FakeInventorySchema()
    monkeypatch.setattr(dv, "resolve_gid_for_token", lambda _: GID)
    monkeypatch.setattr(
        dv,
        "get_local_device_context",
        lambda _: {"schema": inventory, "dtype": "usrp", "ident": "S", "name": "test"},
    )
    monkeypatch.setattr(dv, "token_matches_device", lambda *a: True)
    monkeypatch.setattr(
        "remoteRF_host.server.dynamic_v2_bridge.make_host_bridge_token",
        lambda _: "token",
    )
    configure()
    result = rm.RpcManager().run_rpc(
        function_name="DynamicV2:OpenSession",
        args={
            "g": map_arg(GID),
            "wire": map_arg(
                grpc_pb2.OpenSessionRequest(
                    token="token",
                    control_protocol_version="2.0",
                    streaming_protocol_version="1.0",
                ).SerializeToString()
            ),
        },
    )
    opened = grpc_pb2.OpenSessionResponse.FromString(unmap_arg(result["wire"]))
    assert not opened.error.code
    calls = []
    inventory.live.invoke_native = lambda method, args, positional_args=None: (
        calls.append((method, args))
    )
    freqs = [2.42e9, 1e9]
    inventory.live.device = types.SimpleNamespace(
        get_tx_num_channels=lambda: 2, get_tx_freq=lambda chan: freqs[chan]
    )
    return opened, inventory.live, calls, freqs


@pytest.mark.parametrize(
    "method,args",
    [
        ("set_tx_freq", {"tune_request": 1e9}),
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
            "send_waveform",
            {"waveform_proto": np.array([1 + 0j]), "duration": 0.01, "freq": 1e9},
        ),
        ("set_tx_lo_freq", {"freq": 1e9, "name": "all"}),
    ],
)
def test_tunneled_usrp_v2_refuses_disallowed_requests(live_usrp, method, args):
    opened, live, calls, freqs = live_usrp
    request = grpc_pb2.InvokeRequest(
        session_id=opened.session_id,
        handle=opened.device_handle,
        method=method,
        overload_id="default",
    )
    for key, value in args.items():
        request.args.add(name=key).value.CopyFrom(encode_value(value))
    result = rm.RpcManager().run_rpc(
        function_name="DynamicV2:Invoke",
        args={"g": map_arg(GID), "wire": map_arg(request.SerializeToString())},
    )
    response = grpc_pb2.InvokeResponse.FromString(unmap_arg(result["wire"]))
    assert response.error.code == "RemoteRFPolicyError"
    assert not calls


@pytest.mark.parametrize("allowed", [False, True])
def test_tx_sample_frames_recheck_the_actual_stream_channel(
    live_usrp, monkeypatch, allowed
):
    opened, live, calls, freqs = live_usrp
    freqs[1] = 2.42e9 if allowed else 1e9
    session = dv.session_manager.get(opened.session_id)
    streamer = types.SimpleNamespace(send=lambda *a: calls.append("send") or 1)
    monkeypatch.setattr(dv.session_manager, "_start_async_worker", lambda *a: None)
    entry = dv.session_manager.register_handle(
        session, streamer, "uhd.usrp.TXStreamer", parent=session.root_handle
    )
    entry.stream_channels = [1]
    monkeypatch.setattr(
        dv.SampleDataV1Servicer, "_metadata_object", staticmethod(lambda *a: object())
    )
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
