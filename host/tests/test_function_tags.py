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
import tempfile
import time
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
from remoterf_device_core.idl import DeviceSchema, idl_expose
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

    def test_host_v1_rpc_enforces_context_and_grant_blocked_tags(self):
        radio = _TaggedHostRadio()
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

            # Unblocked holder can call TX normally.
            res_ok = mgr.run_rpc(
                function_name="tagged_host_radio:tx_lo:SET",
                args={"g": map_arg(7), "tx_lo": map_arg(915_000_000)},
            )
            self.assertNotIn("Error", res_ok)
            self.assertEqual(radio.tx_calls, [("set_tx_lo", 915_000_000)])


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
        with mock.patch("remoteRF_host.server.dynamic_v2.resolve_gid_for_token", return_value=7), \
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

            # Open a TX streamer while unblocked, then enforce via bridge blocked_tags (direct path).
            context["blocked_tags"] = ()
            tx_res, _ = manager.invoke(
                session.session_id, session.root_handle, "get_tx_stream", {}, positional_args=[{}], overload_id="default"
            )
            tx_handle = tx_res["__remoterf_handle__"]
            tx_gen = tx_res["generation"]

            inv_req = grpc_pb2.InvokeRequest(
                session_id=session.session_id,
                handle=session.root_handle,
                method="set_tx_gain",
                overload_id="default",
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
        for bad in ("TX", [123], [""]):
            with self.assertRaises(PermissionError):
                self.host.verify(self._token(blocked_tags=bad), 7)

        token = self._token(blocked_tags=["TX"], jti="grant-tx")
        self.host.refresh(7, token)
        self.assertEqual(self.host._grants[7]["_blocked_tags"], ("TX",))

        radio = _TaggedHostRadio()
        context = {"schema": radio, "blocked_tags": ()}

        # Client attempts a tagged TX call and even tries to spoof __rrf_blocked_tags=[].
        tx_req = grpc_pb2.GenericRPCRequest(function_name="tagged_host_radio:tx:CALL1")
        tx_req.args["arg1"].CopyFrom(map_arg([1, 2, 3]))
        tx_req.args["__rrf_blocked_tags"].CopyFrom(map_arg("[]"))

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


if __name__ == "__main__":
    unittest.main()
