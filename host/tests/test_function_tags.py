# Copyright (C) 2026 RemoteRF
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

import asyncio
import json
import queue
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import jwt
import numpy as np
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import NameOID
from remoterf_device_core.drivers.usrp.uhd_manifest import TARGET_UHD_VERSION, build_usrp_schema_v2
from remoterf_device_core.idl import (
    BLOCK_ALL_TAG,
    DeviceSchema,
    HOST_FUNCTION_TAGS_CAPABILITY,
    idl_expose,
)
from remoterf_device_core.v2_errors import RemoteRFPolicyError

from remoteRF_host.common.grpc import grpc_host_pb2 as host_pb2
from remoteRF_host.common.grpc import grpc_pb2
from remoteRF_host.common.grpc.v2_codec import encode_value
from remoteRF_host.common.utils.process_arg import map_arg, unmap_arg
from remoteRF_host.server import direct_host, dynamic_v2, dynamic_v2_bridge, rpc_manager, tunnel_agent


class _TaggedHostRadio(DeviceSchema):
    device_type = "tagged_host_radio"
    driver_version = "0.1.0"

    def __init__(self):
        super().__init__()
        self.tx_calls = []
        self.rx_calls = 0

    @idl_expose(kind="set", tag="TX")
    def set_tx_lo(self, value: int) -> None:
        self.tx_calls.append(("set_tx_lo", int(value)))

    @idl_expose(kind="call", tag="TX")
    def call_tx(self, value) -> None:
        self.tx_calls.append(("call_tx", value))

    @idl_expose(kind="call")
    def call_rx(self) -> int:
        self.rx_calls += 1
        return self.rx_calls


class _FakeRXStreamer:
    def get_max_num_samps(self):
        return 256

    def recv(self, buffer, metadata, timeout):
        buffer[..., :2] = np.array([1 + 2j, 3 + 4j], dtype=buffer.dtype)
        metadata.error_code = "none"
        metadata.has_time_spec = False
        return 2


class _FakeTXStreamer:
    def __init__(self):
        self.sent = []

    def get_max_num_samps(self):
        return 256

    def send(self, samples, metadata, timeout):
        self.sent.append(samples.copy())
        return int(samples.shape[-1])


class _FakeMetadata:
    def __init__(self):
        self.error_code = "none"
        self.has_time_spec = False
        self.time_spec = None
        self.more_fragments = False
        self.fragment_offset = 0
        self.out_of_sequence = False


def _fake_uhd_module():
    return types.SimpleNamespace(
        types=types.SimpleNamespace(
            RXMetadata=_FakeMetadata,
            TXMetadata=_FakeMetadata,
            TXAsyncMetadata=None,
            StreamCMD=lambda mode: mode,
            StreamMode=types.SimpleNamespace(stop_cont="stop_cont"),
        )
    )


class _FakeLiveUsrp:
    def __init__(self):
        self.device = object()
        self.tx_streamer = _FakeTXStreamer()
        self.rx_streamer = _FakeRXStreamer()
        self.calls = []
        self.closed = False

    def build_session_metadata_v2(self, policy_restrictions=None):
        return {
            "hardware_profile": "usrp2901",
            "uhd_version": TARGET_UHD_VERSION,
            "uhd_abi": "test",
            "policy_restrictions": dict(policy_restrictions or {}),
        }

    def invoke_native(self, method, args, positional_args=None):
        self.calls.append((method, args, list(positional_args or ())))
        if method == "get_rx_stream":
            return self.rx_streamer
        if method == "get_tx_stream":
            return self.tx_streamer
        if method == "get_time_now":
            return 2.5
        return None

    def close_native_device(self):
        self.closed = True


class _FakeInventoryUsrp:
    def __init__(self):
        self.live = None

    def get_idl_v2(self):
        return build_usrp_schema_v2()

    def create_session_schema(self):
        self.live = _FakeLiveUsrp()
        return self.live


def _certificate(ca_key, ca_cert, public_key, name: str) -> str:
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_cert.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


class HostTunnelAndV1TagEnforcementTests(unittest.TestCase):
    def _agent(self, direct=None):
        agent = tunnel_agent.HostTunnelAgent.__new__(tunnel_agent.HostTunnelAgent)
        agent._direct_host = direct
        agent._devices = {7: {}}
        agent._out_q = queue.Queue()
        agent._host_id = "lab-host"
        agent._host_token = "token"
        agent.cfg = mock.Mock(version="test")
        return agent

    def test_hello_advertises_function_tags_capability(self):
        hello = self._agent(None)._hello_frame().hello
        self.assertIn(HOST_FUNCTION_TAGS_CAPABILITY, list(hello.capabilities))

    def test_reservation_start_forwards_blocked_tags(self):
        agent = self._agent(None)
        with mock.patch("remoteRF_host.server.tunnel_agent.prepare_reservation") as prepare:
            agent._handle_control(host_pb2.Control(
                kind="reservation_start",
                device_id="7",
                payload=json.dumps({"tag": "res-1", "lockout_bypass": False, "blocked_tags": ["TX", "Cal"]}),
            ))
        prepare.assert_called_once_with(
            7, bypass=False, tag="res-1", resync=False, blocked_tags=("TX", "Cal")
        )

    def test_reservation_start_malformed_payload_fails_closed_to_block_all(self):
        agent = self._agent(None)
        malformed_outer_payloads = (
            "{bad-json",
            "[]",
            json.dumps({"tag": 123, "lockout_bypass": True, "resync": True, "blocked_tags": ["TX"]}),
        )
        for bad_payload in malformed_outer_payloads:
            with mock.patch("remoteRF_host.server.tunnel_agent.prepare_reservation") as prepare:
                agent._handle_control(host_pb2.Control(
                    kind="reservation_start",
                    device_id="7",
                    payload=bad_payload,
                ))
            prepare.assert_called_once_with(
                7, bypass=False, tag="", resync=False, blocked_tags=(BLOCK_ALL_TAG,)
            )

        for bad_tags in ("TX", "", {}, None, [""], [123], ["TX", None]):
            with mock.patch("remoteRF_host.server.tunnel_agent.prepare_reservation") as prepare:
                agent._handle_control(host_pb2.Control(
                    kind="reservation_start",
                    device_id="7",
                    payload=json.dumps({"tag": "res-1", "blocked_tags": bad_tags}),
                ))
            prepare.assert_called_once_with(
                7, bypass=False, tag="res-1", resync=False, blocked_tags=(BLOCK_ALL_TAG,)
            )

        with mock.patch("remoteRF_host.server.tunnel_agent.prepare_reservation") as prepare:
            agent._handle_control(host_pb2.Control(
                kind="reservation_start",
                device_id="7",
                payload=json.dumps({
                    "tag": "res-resync",
                    "lockout_bypass": True,
                    "resync": True,
                    "blocked_tags": "TX",
                }),
            ))
        prepare.assert_called_once_with(
            7, bypass=True, tag="res-resync", resync=True, blocked_tags=(BLOCK_ALL_TAG,)
        )

        # End-to-end: malformed blocked_tags keeps tag/resync, blocks tagged TX calls, and allows untagged RX.
        from remoteRF_host.server import device_manager

        radio = _TaggedHostRadio()
        radio.bind(object())
        st = device_manager._DeviceState(radio, "radio", "", "tagged_host_radio", {})
        with mock.patch.dict(device_manager._devices, {7: st}, clear=False), \
                mock.patch.object(device_manager, "_run_hook") as run_hook:
            agent._handle_control(host_pb2.Control(
                kind="reservation_start",
                device_id="7",
                payload=json.dumps({"tag": "res-bad", "blocked_tags": "TX"}),
            ))
            self.assertEqual(st.reservation_tag, "res-bad")
            self.assertEqual(st.blocked_tags, (BLOCK_ALL_TAG,))
            run_hook.assert_called_once_with(7, "prepare_for_holder", "preparing")
            res_tx = rpc_manager.RpcManager().run_rpc(
                function_name="tagged_host_radio:tx:CALL1",
                args={
                    "g": map_arg(7),
                    "arg1": map_arg([1, 2]),
                    "__rrf_reservation_tag": map_arg("res-bad"),
                },
            )
            self.assertEqual(
                unmap_arg(res_tx["Error"]),
                "tx is blocked for your user groups on this device (tag TX)",
            )
            self.assertEqual(radio.tx_calls, [])
            res_rx = rpc_manager.RpcManager().run_rpc(
                function_name="tagged_host_radio:rx:CALL0",
                args={
                    "g": map_arg(7),
                    "__rrf_reservation_tag": map_arg("res-bad"),
                },
            )
            self.assertNotIn("Error", res_rx)
            self.assertEqual(unmap_arg(res_rx["rx"]), 1)

            # Resync with malformed blocked_tags keeps tag and skips hardware prepare.
            run_hook.reset_mock()
            agent._handle_control(host_pb2.Control(
                kind="reservation_start",
                device_id="7",
                payload=json.dumps({
                    "tag": "res-resync",
                    "lockout_bypass": True,
                    "resync": True,
                    "blocked_tags": "TX",
                }),
            ))
            run_hook.assert_not_called()
            self.assertEqual(st.reservation_tag, "res-resync")
            self.assertTrue(st.lockout_bypass)
            self.assertEqual(st.blocked_tags, (BLOCK_ALL_TAG,))
            res_rx_resync = rpc_manager.RpcManager().run_rpc(
                function_name="tagged_host_radio:rx:CALL0",
                args={
                    "g": map_arg(7),
                    "__rrf_reservation_tag": map_arg("res-resync"),
                },
            )
            self.assertNotIn("Error", res_rx_resync)
            self.assertEqual(unmap_arg(res_rx_resync["rx"]), 2)

        # _clean_blocked_tags also fails closed on malformed inputs.
        for bad in ("TX", "", [""], [None], [123], ["TX", None], 123, {"TX": True}):
            self.assertEqual(device_manager._clean_blocked_tags(bad), (BLOCK_ALL_TAG,), repr(bad))
        self.assertEqual(device_manager._clean_blocked_tags(()), ())
        self.assertEqual(device_manager._clean_blocked_tags(None), ())
        self.assertEqual(device_manager._clean_blocked_tags(["TX", "Cal"]), ("TX", "Cal"))

    def test_host_v1_rpc_enforces_context_and_grant_blocked_tags(self):
        radio = _TaggedHostRadio()
        radio.bind(object())
        mgr = rpc_manager.RpcManager()
        context = {"schema": radio, "blocked_tags": ("TX",)}

        with mock.patch("remoteRF_host.server.rpc_manager.get_device", return_value=radio), \
                mock.patch("remoteRF_host.server.device_manager.get_local_device_context", return_value=context):
            res = mgr.run_rpc(
                function_name="tagged_host_radio:tx:CALL1",
                args={"g": map_arg(7), "arg1": map_arg([1, 2])},
            )
            self.assertEqual(
                unmap_arg(res["Error"]),
                "tx is blocked for your user groups on this device (tag TX)",
            )
            self.assertEqual(radio.tx_calls, [])

            res_rx = mgr.run_rpc(
                function_name="tagged_host_radio:rx:CALL0",
                args={"g": map_arg(7)},
            )
            self.assertNotIn("Error", res_rx)
            self.assertEqual(unmap_arg(res_rx["rx"]), 1)

            # Empty context, blocked via signed grant metadata (__rrf_blocked_tags).
            context["blocked_tags"] = ()
            res_grant = mgr.run_rpc(
                function_name="tagged_host_radio:tx_lo:SET",
                args={
                    "g": map_arg(7),
                    "tx_lo": map_arg(915_000_000),
                    "__rrf_blocked_tags": map_arg(json.dumps(["tx"])),
                },
            )
            self.assertEqual(
                unmap_arg(res_grant["Error"]),
                "set_tx_lo is blocked for your user groups on this device (tag TX)",
            )
            self.assertEqual(radio.tx_calls, [])

            # Malformed __rrf_blocked_tags is rejected.
            res_bad = mgr.run_rpc(
                function_name="tagged_host_radio:rx:CALL0",
                args={"g": map_arg(7), "__rrf_blocked_tags": map_arg("not-json")},
            )
            self.assertIn("Invalid blocked tags", unmap_arg(res_bad["Error"]))

            # Fail-closed wildcard blocks tagged calls and allows untagged RX.
            context["blocked_tags"] = (BLOCK_ALL_TAG,)
            res_wild = mgr.run_rpc(
                function_name="tagged_host_radio:tx:CALL1",
                args={"g": map_arg(7), "arg1": map_arg([9])},
            )
            self.assertEqual(
                unmap_arg(res_wild["Error"]),
                "tx is blocked for your user groups on this device (tag TX)",
            )
            res_wild_rx = mgr.run_rpc(
                function_name="tagged_host_radio:rx:CALL0",
                args={"g": map_arg(7)},
            )
            self.assertNotIn("Error", res_wild_rx)

            # Master token bypasses holder blocked_tags on host v1.
            context["blocked_tags"] = ("TX",)
            res_master = mgr.run_rpc(
                function_name="tagged_host_radio:tx_lo:SET",
                args={
                    "g": map_arg(7),
                    "tx_lo": map_arg(2_400_000_000),
                    "__rrf_master_bypass": map_arg(True),
                },
            )
            self.assertNotIn("Error", res_master)
            self.assertEqual(radio.tx_calls, [("set_tx_lo", 2_400_000_000)])

            # Unblocked holder can call TX normally.
            context["blocked_tags"] = ()
            res_ok = mgr.run_rpc(
                function_name="tagged_host_radio:tx_lo:SET",
                args={"g": map_arg(7), "tx_lo": map_arg(915_000_000)},
            )
            self.assertNotIn("Error", res_ok)
            self.assertEqual(
                radio.tx_calls,
                [("set_tx_lo", 2_400_000_000), ("set_tx_lo", 915_000_000)],
            )

    def test_host_v1_slow_reset_cannot_bypass_blocked_tags_on_handover_or_expiry(self):
        import threading
        from remoteRF_host.server import device_manager

        class _SlowResetHostRadio(_TaggedHostRadio):
            def __init__(self):
                super().__init__()
                self.reset_hook = None

            def reset_for_next_holder(self):
                if self.reset_hook is not None:
                    hook, self.reset_hook = self.reset_hook, None
                    hook()

        gid = 7171
        expected_changed = "The reservation on this device changed; call again."
        for action in ("expiry", "handover"):
            radio = _SlowResetHostRadio()
            radio.bind(object())
            st = device_manager._DeviceState(radio, "radio", "", "tagged_host_radio", {})
            with mock.patch.dict(device_manager._devices, {gid: st}, clear=False), \
                    mock.patch.object(device_manager, "_reservation_end_callbacks", []), \
                    mock.patch.object(device_manager, "_run_hook"):
                device_manager.prepare_reservation(gid, tag="tagA", blocked_tags=("TX",))
                st.reset_pending = True
                in_reset, transition_done = threading.Event(), threading.Event()
                radio.reset_hook = lambda: (in_reset.set(), self.assertTrue(transition_done.wait(1.0)))

                result_box = []
                worker = threading.Thread(
                    target=lambda: result_box.append(
                        rpc_manager.RpcManager().run_rpc(
                            function_name="tagged_host_radio:tx:CALL1",
                            args={
                                "g": map_arg(gid),
                                "arg1": map_arg([7, 7]),
                                "__rrf_reservation_tag": map_arg("tagA"),
                            },
                        )
                    )
                )
                worker.start()
                self.assertTrue(in_reset.wait(1.0))
                if action == "expiry":
                    device_manager.end_reservation(gid)
                else:
                    device_manager.prepare_reservation(gid, tag="tagB", blocked_tags=())
                transition_done.set()
                worker.join(2.0)
                self.assertFalse(worker.is_alive())
                self.assertEqual(radio.tx_calls, [], action)
                self.assertEqual(unmap_arg(result_box[0]["Error"]), expected_changed, action)


class HostV2TagEnforcementTests(unittest.TestCase):
    def test_host_v2_invoke_sample_stream_and_bridge_enforce_blocked_tags(self):
        inventory = _FakeInventoryUsrp()
        context = {
            "schema": inventory,
            "dtype": "usrp",
            "ident": "SERIAL",
            "name": "host-usrp",
            "blocked_tags": ("TX",),
        }
        manager = dynamic_v2.SessionManager()
        with mock.patch.dict(sys.modules, {"uhd": _fake_uhd_module()}), \
                mock.patch("remoteRF_host.server.dynamic_v2.resolve_gid_for_token", return_value=7), \
                mock.patch("remoteRF_host.server.dynamic_v2.get_local_device_context", return_value=context), \
                mock.patch("remoteRF_host.server.dynamic_v2.token_matches_device", return_value=True), \
                mock.patch("remoteRF_host.server.dynamic_v2.session_manager", manager), \
                mock.patch("remoteRF_host.server.dynamic_v2_bridge.session_manager", manager):
            session = manager.open("bridge-token")

            with self.assertRaises(RemoteRFPolicyError) as ctx:
                manager.invoke(
                    session.session_id,
                    session.root_handle,
                    "set_tx_freq",
                    {"tune_request": 2.4e9},
                    overload_id="default",
                )
            self.assertEqual(
                str(ctx.exception),
                "set_tx_freq is blocked for your user groups on this device (tag TX)",
            )

            # RX works while TX is blocked.
            rx_res, _ = manager.invoke(
                session.session_id, session.root_handle, "get_rx_stream", {}, positional_args=[{}], overload_id="default"
            )
            self.assertTrue(rx_res["__remoterf_handle__"].startswith("rx_"))

            # Open a fresh session while unblocked, then enforce via bridge blocked_tags (direct path).
            manager.close(session.session_id)
            context["blocked_tags"] = ()
            session = manager.open("bridge-token")
            tx_res, _ = manager.invoke(
                session.session_id, session.root_handle, "get_tx_stream", {}, positional_args=[{}], overload_id="default"
            )
            tx_handle = tx_res["__remoterf_handle__"]
            tx_gen = tx_res["generation"]

            inv_req = grpc_pb2.InvokeRequest(
                session_id=session.session_id,
                handle=session.root_handle,
                method="set_tx_gain",
                overload_id="gain_chan",
            )
            inv_req.args.add(name="gain", value=encode_value(5.0))
            bridged_inv = dynamic_v2_bridge.run_bridge(
                operation="Invoke",
                args={"wire": map_arg(inv_req.SerializeToString())},
                gid=7,
                blocked_tags=("tx",),
            )
            inv_resp = grpc_pb2.InvokeResponse.FromString(unmap_arg(bridged_inv["wire"]))
            self.assertEqual(inv_resp.error.code, "RemoteRFPolicyError")
            self.assertEqual(
                inv_resp.error.message,
                "set_tx_gain is blocked for your user groups on this device (tag TX)",
            )
            self.assertEqual(dynamic_v2.GRANT_BLOCKED_TAGS.get(), ())

            open_frame = grpc_pb2.SampleFrame(
                session_id=session.session_id,
                handle=tx_handle,
                generation=tx_gen,
                operation_id="direct-tx-1",
                sequence=0,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                credits=1,
            )
            with mock.patch.object(dynamic_v2.SampleDataV1Servicer, "_metadata_object", return_value=_FakeMetadata()):
                bridged_sample = dynamic_v2_bridge.run_bridge(
                    operation="SampleFrame",
                    args={"wire": map_arg(open_frame.SerializeToString())},
                    gid=7,
                    blocked_tags=("TX",),
                )
            sample_resp = grpc_pb2.SampleFrame.FromString(unmap_arg(bridged_sample["wire"]))
            self.assertEqual(sample_resp.kind, grpc_pb2.SAMPLE_FRAME_ERROR)
            self.assertEqual(sample_resp.error.code, "RemoteRFPolicyError")
            self.assertEqual(
                sample_resp.error.message,
                "send is blocked for your user groups on this device (tag TX)",
            )
            self.assertEqual(dynamic_v2.GRANT_BLOCKED_TAGS.get(), ())

            # Master token bypasses holder blocked_tags on host v2 Invoke and SampleFrame.
            manager.close(session.session_id)
            context["blocked_tags"] = ("TX",)
            session = manager.open("bridge-token")
            tx_entry = manager.register_handle(
                session,
                inventory.live.tx_streamer,
                "uhd.usrp.TXStreamer",
                parent=session.root_handle,
            )
            tx_handle = tx_entry.handle
            tx_gen = tx_entry.generation
            inv_req = grpc_pb2.InvokeRequest(
                session_id=session.session_id,
                handle=session.root_handle,
                method="set_tx_gain",
                overload_id="gain_chan",
            )
            inv_req.args.add(name="gain", value=encode_value(5.0))
            bridged_master = dynamic_v2_bridge.run_bridge(
                operation="Invoke",
                args={"wire": map_arg(inv_req.SerializeToString())},
                gid=7,
                master_bypass=True,
            )
            master_inv_resp = grpc_pb2.InvokeResponse.FromString(unmap_arg(bridged_master["wire"]))
            self.assertFalse(master_inv_resp.error.code)
            self.assertFalse(dynamic_v2.GRANT_MASTER_BYPASS.get())

            master_open_frame = grpc_pb2.SampleFrame(
                session_id=session.session_id,
                handle=tx_handle,
                generation=tx_gen,
                operation_id="direct-tx-master",
                sequence=1,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                credits=1,
            )
            with mock.patch.object(dynamic_v2.SampleDataV1Servicer, "_metadata_object", return_value=_FakeMetadata()):
                bridged_master_sample = dynamic_v2_bridge.run_bridge(
                    operation="SampleFrame",
                    args={"wire": map_arg(master_open_frame.SerializeToString())},
                    gid=7,
                    master_bypass=True,
                )
            master_sample_resp = grpc_pb2.SampleFrame.FromString(unmap_arg(bridged_master_sample["wire"]))
            self.assertEqual(master_sample_resp.kind, grpc_pb2.SAMPLE_FRAME_RESULT)
            self.assertFalse(master_sample_resp.error.code)
            self.assertFalse(dynamic_v2.GRANT_MASTER_BYPASS.get())

            # Also verify RpcManager forwards __rrf_master_bypass to DynamicV2 bridge.
            rpc_master = rpc_manager.RpcManager().run_rpc(
                function_name="DynamicV2:Invoke",
                args={
                    "g": map_arg(7),
                    "wire": map_arg(inv_req.SerializeToString()),
                    "__rrf_master_bypass": map_arg(True),
                },
            )
            self.assertNotIn("Error", rpc_master)
            rpc_master_resp = grpc_pb2.InvokeResponse.FromString(unmap_arg(rpc_master["wire"]))
            self.assertFalse(rpc_master_resp.error.code)
            self.assertFalse(dynamic_v2.GRANT_MASTER_BYPASS.get())

            manager.close(session.session_id)

    def test_host_v2_session_snapshots_blocked_tags_and_refuses_midflight_expiry(self):
        import threading
        from remoteRF_host.server import device_manager
        from remoteRF_host.server.v2_errors import RemoteRFSessionExpiredError

        gid = 7172
        inventory = _FakeInventoryUsrp()
        st = device_manager._DeviceState(inventory, "host-usrp", "SERIAL", "usrp", {})
        manager = dynamic_v2.SessionManager()
        state_updated, allow_close = threading.Event(), threading.Event()

        def _delayed_close(_gid):
            state_updated.set()
            allow_close.wait(1.0)
            manager.close_gid(_gid, wait=False)

        with mock.patch.dict(sys.modules, {"uhd": _fake_uhd_module()}), \
                mock.patch.dict(device_manager._devices, {gid: st}, clear=False), \
                mock.patch.object(device_manager, "_reservation_end_callbacks", [_delayed_close]), \
                mock.patch.object(device_manager, "_run_hook"), \
                mock.patch("remoteRF_host.server.dynamic_v2.session_manager", manager):
            device_manager.prepare_reservation(gid, tag="tagA", blocked_tags=("TX",))
            session = manager.open(device_manager.make_host_bridge_token(gid))

            # Mutating st.blocked_tags without advancing epoch cannot weaken a TX-blocked session.
            with device_manager._state_lock:
                st.blocked_tags = ()
            with self.assertRaises(RemoteRFPolicyError):
                manager.invoke(
                    session.session_id,
                    session.root_handle,
                    "set_tx_freq",
                    {"tune_request": 2.4e9},
                    overload_id="default",
                )
            self.assertEqual(inventory.live.calls, [])

            # Reservation end before close_gid runs refuses immediately with session expiry.
            with device_manager._state_lock:
                st.blocked_tags = ("TX",)
            ender = threading.Thread(target=lambda: device_manager.end_reservation(gid))
            ender.start()
            self.assertTrue(state_updated.wait(1.0))
            try:
                with self.assertRaises(RemoteRFSessionExpiredError):
                    manager.invoke(
                        session.session_id,
                        session.root_handle,
                        "set_tx_freq",
                        {"tune_request": 2.4e9},
                        overload_id="default",
                    )
                self.assertEqual(inventory.live.calls, [])
            finally:
                allow_close.set()
                ender.join(2.0)
                self.assertFalse(ender.is_alive())
                manager.close(session.session_id)


class HostDirectPathTagEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.grant_key = ed25519.Ed25519PrivateKey.generate()
        self.ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
        now = datetime.now(timezone.utc)
        self.ca_cert = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(self.ca_key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(self.ca_key, hashes.SHA256())
        )
        self.agent = tunnel_agent.HostTunnelAgent.__new__(tunnel_agent.HostTunnelAgent)
        self.agent._devices = {7: {}}
        self.agent._out_q = queue.Queue()
        self.agent._host_id = "lab-host"
        self.agent._host_token = "token"
        self.agent.cfg = mock.Mock(version="test")
        self.host = direct_host.DirectHost(
            host_id="lab-host",
            key_path=Path(self.tmp.name) / "direct.key",
            run_call=self.agent._run_direct_call,
        )
        public_key = serialization.load_pem_public_key(self.host.public_key_pem().encode())
        self.host.configure({
            "deployment_id": "deployment-1",
            "grant_key": self.grant_key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode(),
            "server_name": "lab-host--lab.global.remoterf.net",
            "cert_pem": _certificate(self.ca_key, self.ca_cert, public_key, "lab-host--lab.global.remoterf.net"),
            "ca_pem": self.ca_cert.public_bytes(serialization.Encoding.PEM).decode(),
        })

    def _token(self, **changes):
        claims = {
            "iss": "deployment-1",
            "aud": "lab-host",
            "gid": 7,
            "jti": "grant-1",
            "rid": "reservation-1",
            "iat": time.time(),
            "exp": int(time.time() + 60),
        }
        claims.update(changes)
        return jwt.encode(
            claims,
            self.grant_key,
            algorithm="EdDSA",
            headers={"typ": "remoterf-host-grant"},
        )

    def test_direct_grant_validates_blocked_tags_and_enforces_on_call(self):
        # Malformed blocked_tags claims are refused.
        for bad in (None, "TX", [123], [""]):
            with self.assertRaises(PermissionError):
                self.host.verify(self._token(blocked_tags=bad), 7)

        token = self._token(blocked_tags=["TX"], jti="grant-tx")
        self.host.refresh(7, token)
        self.assertEqual(self.host._grants[7]["_blocked_tags"], ("TX",))

        radio = _TaggedHostRadio()
        radio.bind(object())
        context = {"schema": radio, "blocked_tags": ()}

        # Client attempts a tagged TX call and tries to spoof __rrf_blocked_tags=[] and __rrf_master_bypass=True.
        tx_req = grpc_pb2.GenericRPCRequest(function_name="tagged_host_radio:tx:CALL1")
        tx_req.args["arg1"].CopyFrom(map_arg([1, 2, 3]))
        tx_req.args["__rrf_blocked_tags"].CopyFrom(map_arg("[]"))
        tx_req.args["__rrf_master_bypass"].CopyFrom(map_arg(True))

        rx_req = grpc_pb2.GenericRPCRequest(function_name="tagged_host_radio:rx:CALL0")

        with mock.patch("remoteRF_host.server.rpc_manager.get_device", return_value=radio), \
                mock.patch("remoteRF_host.server.device_manager.get_local_device_context", return_value=context):
            tx_res, drop_tx = asyncio.run(self.host._serve_call(7, "reservation-1", tx_req))
            rx_res, drop_rx = asyncio.run(self.host._serve_call(7, "reservation-1", rx_req))

        self.assertFalse(drop_tx)
        self.assertEqual(
            unmap_arg(tx_res["Error"]),
            "tx is blocked for your user groups on this device (tag TX)",
        )
        self.assertEqual(radio.tx_calls, [])

        self.assertFalse(drop_rx)
        self.assertNotIn("Error", rx_res)
        self.assertEqual(unmap_arg(rx_res["rx"]), 1)

        # If the grant ends right after the lock-held _refusal check, _serve_call refuses
        # instead of defaulting blocked_tags=() and executing the call.
        run_call_spy = mock.Mock()
        self.host.run_call = run_call_spy
        original_refusal = self.host._refusal
        refusal_calls = 0

        def end_grant_after_second_check(gid, rid, fn):
            nonlocal refusal_calls
            refusal_calls += 1
            res = original_refusal(gid, rid, fn)
            if res is None and refusal_calls == 2:
                self.host.end(gid)
            return res

        with mock.patch.object(self.host, "_refusal", side_effect=end_grant_after_second_check):
            race_res, drop_race = asyncio.run(self.host._serve_call(7, "reservation-1", tx_req))
        self.assertTrue(drop_race)
        self.assertEqual(unmap_arg(race_res["a"]), direct_host.GRANT_ENDED)
        run_call_spy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
