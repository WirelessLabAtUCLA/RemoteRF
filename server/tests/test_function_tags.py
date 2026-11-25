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

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID
from datetime import timezone
from remoterf_device_core.idl import DeviceSchema, idl_expose
from remoterf_device_core.v2_errors import RemoteRFPolicyError
from remoterf_federation_core import verify_eddsa_jwt

from remoteRF_server.common.grpc import grpc_pb2
from remoteRF_server.common.grpc.v2_codec import encode_value
from remoteRF_server.common.utils import map_arg, unmap_arg
from remoteRF_server.drivers.usrp.uhd_manifest import TARGET_UHD_VERSION, build_usrp_schema_v2
from remoteRF_server.host import host_tunnel_server as hts
from remoteRF_server.server import device_manager, host_direct, reservation
from remoteRF_server.server.dynamic_v2 import (
    DynamicControlV2Servicer,
    SampleDataV1Servicer,
    SessionManager,
)
from remoteRF_server.server.federation.identity import (
    DeploymentState,
    init_identity,
    load_private_key,
    save_state,
)
from remoteRF_server.server.rpc_manager import RpcManager


class _TaggedRadioSchema(DeviceSchema):
    device_type = "tagged_radio"
    driver_version = "0.1.0"

    def __init__(self):
        super().__init__()
        self.tx_lo_val = 915_000_000
        self.tx_calls = []
        self.rx_calls = 0

    @idl_expose(kind="get", tag="TX")
    def get_tx_power(self) -> float:
        return -10.0

    @idl_expose(kind="set", tag="TX")
    def set_tx_lo(self, value: int) -> None:
        self.tx_lo_val = int(value)

    @idl_expose(kind="get")
    def get_rx_lo(self) -> int:
        return 915_000_000

    @idl_expose(kind="call", tag=("TX", "RF"))
    def call_tx(self, value) -> None:
        self.tx_calls.append(value)

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
        self.calls = []
        self.tx_streamer = _FakeTXStreamer()
        self.rx_streamer = _FakeRXStreamer()
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
            return 1.5
        if method == "send_waveform":
            return 4
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


class ReservationBlockedTagsTests(unittest.TestCase):
    def test_blocked_tags_for_holder_respects_roles_and_groups(self):
        handler = reservation.ReservationHandler.__new__(reservation.ReservationHandler)
        handler.get_uid = mock.Mock(return_value=7)

        fake_perms = mock.Mock()
        fake_groups = mock.Mock()
        fake_groups.get_users_blocked_tags.return_value = (True, ["TX"])

        with mock.patch.object(reservation, "perms", fake_perms), \
                mock.patch.object(reservation, "user_group_handler", fake_groups):
            # Normal user gets the group intersection.
            fake_perms.get_perms.return_value = [["Normal User"]]
            self.assertEqual(handler._blocked_tags_for_holder_locked("alice", 3), ("TX",))

            # Admin and Power User bypass group tag blacklists.
            fake_perms.get_perms.return_value = [["Admin"]]
            self.assertEqual(handler._blocked_tags_for_holder_locked("admin_u", 3), ())

            fake_perms.get_perms.return_value = [["Power User", 10, 3600, "[3]"]]
            self.assertEqual(handler._blocked_tags_for_holder_locked("power_u", 3), ())

            # Missing uid returns no blocks.
            handler.get_uid.return_value = -1
            fake_perms.get_perms.return_value = [["Normal User"]]
            self.assertEqual(handler._blocked_tags_for_holder_locked("ghost", 3), ())

    def test_update_devices_pushes_blocked_tags_and_admin_bypass(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "reservations.db"
        now = datetime.now()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE Reservations (res_id INTEGER PRIMARY KEY, username TEXT, "
                "device_id INTEGER, start_time TEXT, end_time TEXT, salt TEXT, hash TEXT)"
            )
            conn.execute(
                "INSERT INTO Reservations VALUES (1, 'alice', 3, ?, ?, 's1', 'h1')",
                ((now - timedelta(minutes=1)).isoformat(), (now + timedelta(minutes=10)).isoformat()),
            )
            conn.execute(
                "INSERT INTO Reservations VALUES (2, 'boss', 4, ?, ?, 's2', 'h2')",
                ((now - timedelta(minutes=1)).isoformat(), (now + timedelta(minutes=10)).isoformat()),
            )

        handler = reservation.ReservationHandler.__new__(reservation.ReservationHandler)
        handler.filepath = db_path
        handler.lock = threading.RLock()
        handler._clean_expired_reservations = mock.Mock()
        handler._account_security_state_locked = mock.Mock(return_value="native")
        handler.get_uid = mock.Mock(side_effect=lambda u: 10 if u == "alice" else 20)

        fake_groups = mock.Mock()
        fake_groups.get_groups_user_in.side_effect = lambda uid: ["admin"] if uid == 20 else ["rx-only"]
        fake_groups.get_users_blocked_tags.side_effect = lambda uid, device_id: (True, ["TX"]) if uid == 10 else (True, [])
        fake_perms = mock.Mock()
        fake_perms.get_perms.return_value = [["Normal User"]]
        set_calls = []

        with mock.patch.object(reservation, "get_all_devices", return_value={3: (object(), "", ""), 4: (object(), "", "")}), \
                mock.patch.object(reservation, "get_registered_devices", return_value={3: (object(), "", ""), 4: (object(), "", "")}), \
                mock.patch.object(reservation, "user_group_handler", fake_groups), \
                mock.patch.object(reservation, "perms", fake_perms), \
                mock.patch.object(reservation, "set_device", side_effect=lambda *a, **kw: set_calls.append((a, kw))):
            handler.update_devices()

        by_dev = {args[0]: kwargs for args, kwargs in set_calls}
        self.assertEqual(by_dev["3"], {"bypass": False, "blocked_tags": ("TX",)})
        self.assertEqual(by_dev["4"], {"bypass": True, "blocked_tags": ()})


class DeviceManagerHostPropagationTests(unittest.TestCase):
    def test_host_reservation_start_and_resync_carry_blocked_tags(self):
        sent = []

        class FakeRegistry:
            def send_control(self, host_id, *, kind, device_id, payload, **kwargs):
                sent.append((host_id, kind, device_id, payload))
                return True

        st = device_manager._DeviceState(
            dev=device_manager.VirtualDevice(gid=7, host_id="host-a", device_id="7", kind="adalm_pluto"),
            origin="host",
            online=True,
            salt="",
            hsh="",
            name="host-pluto",
            ident="serial-7",
            dtype="adalm_pluto",
        )
        with device_manager._state_lock:
            saved = device_manager._devices.get(7)
            device_manager._devices[7] = st
        try:
            with mock.patch.object(device_manager, "_sync_host_devices"), \
                    mock.patch.object(device_manager.hts, "get_tunnel_registry", return_value=FakeRegistry()), \
                    mock.patch.object(device_manager, "_reservation_end_callbacks", []), \
                    mock.patch.object(device_manager, "_reset_if_pending"):
                device_manager.set_device(7, "salt1", "hash1", bypass=False, blocked_tags=("TX", "Cal"))
                self.assertEqual(device_manager.get_device_blocked_tags(7), ("TX", "Cal"))
                with device_manager.acquire_device_by_gid(7) as (_gid, acquired_dev):
                    self.assertEqual(acquired_dev.blocked_tags, ("TX", "Cal"))
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0][1], "reservation_start")
                self.assertEqual(sent[0][3]["blocked_tags"], ["TX", "Cal"])

                sent.clear()
                device_manager.resync_host_reservations("host-a")
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0][1], "reservation_start")
                self.assertEqual(sent[0][3]["blocked_tags"], ["TX", "Cal"])
                self.assertTrue(sent[0][3]["resync"])

                # Clearing reservation resets blocked_tags.
                device_manager.set_device(7, "", "")
                self.assertEqual(device_manager.get_device_blocked_tags(7), ())
                self.assertEqual(st.blocked_tags, ())
        finally:
            with device_manager._state_lock:
                if saved is None:
                    device_manager._devices.pop(7, None)
                else:
                    device_manager._devices[7] = saved


class ServerV1DispatchTagEnforcementTests(unittest.TestCase):
    def test_v1_blocks_tagged_get_set_call_and_batch_allows_rx_and_unblocked(self):
        rpc = RpcManager()
        dev = _TaggedRadioSchema()
        dev.blocked_tags = ("tx",)  # case-insensitive match against TX

        with self.assertRaises(RemoteRFPolicyError) as ctx:
            rpc._dispatch_native_schema(function_name="tagged_radio:tx:CALL1", args={"arg1": map_arg(1)}, dev=dev)
        self.assertEqual(str(ctx.exception), "tx is blocked for your user groups on this device (tag TX)")
        self.assertEqual(dev.tx_calls, [])

        with self.assertRaises(RemoteRFPolicyError) as ctx:
            rpc._dispatch_native_schema(
                function_name="tagged_radio:tx_lo:SET",
                args={"tx_lo": map_arg(2_400_000_000)},
                dev=dev,
            )
        self.assertEqual(str(ctx.exception), "set_tx_lo is blocked for your user groups on this device (tag TX)")
        self.assertEqual(dev.tx_lo_val, 915_000_000)

        with self.assertRaises(RemoteRFPolicyError) as ctx:
            rpc._dispatch_native_schema(function_name="tagged_radio:tx_power:GET", args={}, dev=dev)
        self.assertEqual(str(ctx.exception), "get_tx_power is blocked for your user groups on this device (tag TX)")

        # Untagged RX functions succeed.
        res_rx = rpc._dispatch_native_schema(function_name="tagged_radio:rx:CALL0", args={}, dev=dev)
        self.assertEqual(unmap_arg(res_rx["rx"]), 1)
        res_lo = rpc._dispatch_native_schema(function_name="tagged_radio:rx_lo:GET", args={}, dev=dev)
        self.assertEqual(unmap_arg(res_lo["rx_lo"]), 915_000_000)

        # Batch containing an allowed op followed by a blocked op fails at op1 with clear message.
        op0 = grpc_pb2.GenericRPCRequest(function_name="tagged_radio:rx:CALL0").SerializeToString()
        op1 = grpc_pb2.GenericRPCRequest(
            function_name="tagged_radio:tx_lo:SET",
            args={"tx_lo": map_arg(2_400_000_000)},
        ).SerializeToString()
        batch_res = rpc._dispatch_acquired(
            function_name="tagged_radio:batch:CALLN",
            args={"op0": map_arg(op0), "op1": map_arg(op1)},
            gid=1,
            dev=dev,
        )
        self.assertEqual(unmap_arg(batch_res["batch_done"]), 1)
        self.assertEqual(unmap_arg(batch_res["batch_failed"]), 1)
        self.assertEqual(
            unmap_arg(batch_res["batch_error"]),
            "set_tx_lo is blocked for your user groups on this device (tag TX)",
        )

        # Unblocked holder calls everything normally.
        dev.blocked_tags = ()
        rpc._dispatch_native_schema(
            function_name="tagged_radio:tx_lo:SET",
            args={"tx_lo": map_arg(2_400_000_000)},
            dev=dev,
        )
        self.assertEqual(dev.tx_lo_val, 2_400_000_000)
        rpc._dispatch_native_schema(function_name="tagged_radio:tx:CALL1", args={"arg1": map_arg([1, 2])}, dev=dev)
        self.assertEqual(dev.tx_calls, [[1, 2]])

    def test_v1_virtual_device_blocks_locally_when_lab_copy_matches(self):
        rpc = RpcManager()
        vdev = device_manager.VirtualDevice(
            gid=5, host_id="host-a", device_id="5", kind="tagged_radio", blocked_tags=("TX",)
        )
        with mock.patch("remoteRF_server.server.host_dynamic_v2.lab_copy", return_value=_TaggedRadioSchema()), \
                mock.patch("remoteRF_server.server.rpc_manager.hts.handle_host_device") as forward:
            with self.assertRaises(RemoteRFPolicyError) as ctx:
                rpc._dispatch_acquired(
                    function_name="tagged_radio:tx:CALL1",
                    args={"arg1": map_arg(1)},
                    gid=5,
                    dev=vdev,
                )
        self.assertEqual(str(ctx.exception), "tx is blocked for your user groups on this device (tag TX)")
        forward.assert_not_called()


class ServerV2DispatchTagEnforcementTests(unittest.TestCase):
    def test_v2_invoke_and_sample_stream_enforce_blocked_tags(self):
        inventory = _FakeInventoryUsrp()
        context = {
            "schema": inventory,
            "dtype": "usrp",
            "ident": "SERIAL",
            "name": "test-usrp",
            "blocked_tags": ("TX",),
        }
        manager = SessionManager()
        with mock.patch("remoteRF_server.server.dynamic_v2.resolve_gid_for_token", return_value=7), \
                mock.patch("remoteRF_server.server.dynamic_v2.get_local_device_context", return_value=context), \
                mock.patch("remoteRF_server.server.dynamic_v2.token_matches_device", return_value=True), \
                mock.patch("remoteRF_server.server.dynamic_v2.is_master_token", return_value=False), \
                mock.patch("remoteRF_server.server.dynamic_v2.session_manager", manager):
            session = manager.open("user-token")

            # TX methods blocked on MultiUSRP.
            for fn, args in (
                ("set_tx_freq", {"tune_request": 2.4e9}),
                ("get_tx_stream", {"stream_args": {}}),
                ("send_waveform", {"samples": [0.0], "duration": 0.1, "freq": 2.4e9}),
            ):
                with self.assertRaises(RemoteRFPolicyError) as ctx:
                    manager.invoke(session.session_id, session.root_handle, fn, args, overload_id="default")
                self.assertEqual(str(ctx.exception), f"{fn} is blocked for your user groups on this device (tag TX)")

            # Servicer wraps the refusal as a normal Dynamic v2 policy error envelope.
            inv_req = grpc_pb2.InvokeRequest(
                session_id=session.session_id,
                handle=session.root_handle,
                method="set_tx_gain",
                overload_id="default",
            )
            inv_req.args.add(name="gain", value=encode_value(10.0))
            inv_resp = DynamicControlV2Servicer().Invoke(inv_req, None)
            self.assertEqual(inv_resp.error.code, "RemoteRFPolicyError")
            self.assertEqual(
                inv_resp.error.message,
                "set_tx_gain is blocked for your user groups on this device (tag TX)",
            )

            # Untagged / RX methods still succeed.
            rx_res, _ = manager.invoke(
                session.session_id, session.root_handle, "get_rx_stream", {}, positional_args=[{}], overload_id="default"
            )
            self.assertTrue(rx_res["__remoterf_handle__"].startswith("rx_"))
            t_val, _ = manager.invoke(session.session_id, session.root_handle, "get_time_now", {}, overload_id="default")
            self.assertEqual(t_val, 1.5)

            # Even if a TXStreamer handle exists, SampleStream TX OPEN refuses it.
            context["blocked_tags"] = ()
            tx_res, _ = manager.invoke(
                session.session_id, session.root_handle, "get_tx_stream", {}, positional_args=[{}], overload_id="default"
            )
            tx_handle = tx_res["__remoterf_handle__"]
            tx_gen = tx_res["generation"]

            context["blocked_tags"] = ("tx",)
            open_frame = grpc_pb2.SampleFrame(
                session_id=session.session_id,
                handle=tx_handle,
                generation=tx_gen,
                operation_id="tx-op-1",
                sequence=0,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                credits=1,
            )
            with mock.patch.object(SampleDataV1Servicer, "_metadata_object", return_value=_FakeMetadata()):
                responses = list(SampleDataV1Servicer().SampleStream(iter([open_frame]), None))
            self.assertEqual(len(responses), 1)
            self.assertEqual(responses[0].kind, grpc_pb2.SAMPLE_FRAME_ERROR)
            self.assertEqual(responses[0].error.code, "RemoteRFPolicyError")
            self.assertEqual(
                responses[0].error.message,
                "send is blocked for your user groups on this device (tag TX)",
            )
            self.assertEqual(inventory.live.tx_streamer.sent, [])

            # Master token bypasses blocked_tags.
            manager.close(session.session_id)
        with mock.patch("remoteRF_server.server.dynamic_v2.resolve_gid_for_token", return_value=7), \
                mock.patch("remoteRF_server.server.dynamic_v2.get_local_device_context", return_value=context), \
                mock.patch("remoteRF_server.server.dynamic_v2.token_matches_device", return_value=True), \
                mock.patch("remoteRF_server.server.dynamic_v2.is_master_token", return_value=True):
            master_session = manager.open("master_7")
            manager.invoke(
                master_session.session_id,
                master_session.root_handle,
                "set_tx_freq",
                {"tune_request": 2.4e9},
                overload_id="default",
            )
            manager.close(master_session.session_id)


class HostDirectGrantBlockedTagsTests(unittest.TestCase):
    def test_issue_grant_and_offer_carry_blocked_tags_claim(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)
        identity_dir = tmp_path / "identity"
        init_identity(identity_dir)
        save_state(identity_dir, DeploymentState(deployment_id="deployment-1", slug="lab"))
        cert_dir = tmp_path / "certs"
        cert_dir.mkdir()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=2)).add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256())
        )
        (cert_dir / "ca.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (cert_dir / "ca.key").write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        config = tmp_path / "config"
        config.mkdir()
        (config / "global.env").write_text("GLOBAL_STATE=active\n")

        env = {
            "REMOTERF_FEDERATION_IDENTITY_DIR": str(identity_dir),
            "REMOTERF_CERT_DIR": str(cert_dir),
            "REMOTERF_CONFIG_DIR": str(config),
        }
        with mock.patch.dict("os.environ", env), mock.patch.object(host_direct.time, "time", return_value=1000.0):
            pub = load_private_key(identity_dir).public_key()
            claims_blocked = verify_eddsa_jwt(
                host_direct.issue_grant("lab-host", 7, "res-1", 2000, "tag-1", ("TX", "Cal")),
                pub,
            )
            self.assertEqual(claims_blocked["blocked_tags"], ["TX", "Cal"])

            claims_plain = verify_eddsa_jwt(
                host_direct.issue_grant("lab-host", 7, "res-1", 2000, "tag-1"),
                pub,
            )
            self.assertNotIn("blocked_tags", claims_plain)

        # offer() forwards blocked_tags to issue_grant and _start_refresher.
        host_direct._offer_cooldowns.clear()
        session = hts.HostSession("host-a", capabilities={"direct"}, tls_public_key="key")
        session.direct_ready = True
        session.direct_lab_info = {"server_name": "host-a.global.remoterf.net", "ca_pem": "lab-ca"}

        class _Registry:
            def session_for_host(self, _host_id):
                return session

            def request_control(self, *args, **kwargs):
                return {"answer": {"ufrag": "u", "pwd": "p", "candidates": []}}

        grant_calls = []
        refresher_calls = []
        with mock.patch.object(hts, "get_tunnel_registry", return_value=_Registry()), \
                mock.patch.object(host_direct, "issue_grant", side_effect=lambda *a: grant_calls.append(a) or "g"), \
                mock.patch.object(host_direct, "_start_refresher", side_effect=lambda *a: refresher_calls.append(a)):
            ans = host_direct.offer(
                device_manager.VirtualDevice(gid=7, host_id="host-a", device_id="7"),
                "{}",
                rid="res-9",
                reservation_end=2000,
                still_live=lambda: True,
                tag="tag-1",
                blocked_tags=("TX",),
            )
        self.assertIsNotNone(ans)
        self.assertEqual(grant_calls[0][-1], ("TX",))
        self.assertEqual(refresher_calls[0][-1], ("TX",))


if __name__ == "__main__":
    unittest.main()
