from __future__ import annotations

import threading
from unittest import mock

from remoteRF_host.common.grpc import grpc_host_pb2 as pb
from remoteRF_host.server import device_manager
from remoteRF_host.server.tunnel_agent import HostTunnelAgent


def _agent(*gids: int) -> HostTunnelAgent:
    agent = HostTunnelAgent.__new__(HostTunnelAgent)
    agent._devices = {gid: {} for gid in gids}
    return agent


def test_reservation_start_queues_the_prepare() -> None:
    with mock.patch("remoteRF_host.server.tunnel_agent.prepare_reservation") as prepare:
        _agent(7)._handle_control(pb.Control(kind="reservation_start", device_id="7"))
    prepare.assert_called_once_with(7, bypass=False, tag="", resync=False)


def test_reservation_start_for_unknown_gid_only_warns() -> None:
    with mock.patch("remoteRF_host.server.tunnel_agent.prepare_reservation") as prepare, mock.patch(
        "remoteRF_host.server.tunnel_agent.warn"
    ) as warn:
        _agent(7)._handle_control(pb.Control(kind="reservation_start", device_id="8"))
    prepare.assert_not_called()
    warn.assert_called_once_with(
        "reservation_start for unknown gid=8", component="hostrf.tunnel_agent"
    )


def test_hooks_run_in_order_off_the_caller_and_never_block_it() -> None:
    gid = 7001
    order = []
    release = threading.Event()
    booting = threading.Event()

    class Schema:
        def prepare_for_holder(self) -> None:
            booting.set()
            assert release.wait(2)  # a slow boot
            order.append(("prepare", threading.current_thread() is threading.main_thread()))

        def reset_for_next_holder(self) -> None:
            order.append(("reset", threading.current_thread() is threading.main_thread()))

    device_manager._devices[gid] = device_manager._DeviceState(
        schema=Schema(), name="test-device", ident="", dtype="test", init={}
    )
    try:
        first = device_manager.prepare_reservation(gid)
        assert booting.wait(2)
        second = device_manager.end_reservation(gid)  # returns at once, though the boot still runs
        assert not first.done() and not second.done()
        release.set()
        second.result(2)
        assert order == [("prepare", False), ("reset", False)]
        assert device_manager.prepare_reservation(99999) is None  # no such device
    finally:
        release.set()
        device_manager._devices.pop(gid, None)
        device_manager._reservation_hooks.pop(gid, None)


def test_reset_before_rpc_logs_reset_failure_and_allows_next_holder_rpc() -> None:
    from remoteRF_host.common.utils import map_arg, unmap_arg
    from remoteRF_host.server.rpc_manager import RpcManager

    gid = 7002
    after_calls = []

    class FlakyResetSchema:
        device_type = "fake"
        lockout_bypass = False

        def reset_for_next_holder(self) -> None:
            raise OSError("usb reset glitch")

        def lockout_after_hook(self, kind: str, failed: bool = False) -> None:
            after_calls.append((kind, failed))

        def dispatch(self, method_name: str, args: dict):
            assert method_name == "get_center_freq"
            return 915_000_000.0

    schema = FlakyResetSchema()
    st = device_manager._DeviceState(
        schema=schema,
        name="flaky-dev",
        ident="0",
        dtype="fake",
        init={},
        reset_pending=True,
        epoch=3,
        reservation_tag="holder-b",
    )
    device_manager._devices[gid] = st
    try:
        with mock.patch("remoteRF_host.server.device_manager.warn") as warn_mock:
            reply = RpcManager().run_rpc(
                function_name="fake:center_freq:GET",
                args={
                    "g": map_arg(gid),
                    "__rrf_reservation_tag": map_arg("holder-b"),
                },
                expected_epoch=3,
            )
        assert "Error" not in reply, reply
        assert unmap_arg(reply["center_freq"]) == 915_000_000.0
        assert st.reset_pending is False
        assert after_calls == [("reset", True)]
        warn_mock.assert_called_once()
        assert "usb reset glitch" in warn_mock.call_args.args[0]
    finally:
        device_manager._devices.pop(gid, None)
