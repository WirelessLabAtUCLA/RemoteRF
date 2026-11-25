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

"""Client/server Dynamic v2 regression test over a real localhost gRPC socket."""
from __future__ import annotations

import os
import sys
import types
import unittest
from concurrent import futures
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import grpc
import numpy as np

from remoteRF_server.common.grpc import grpc_pb2_grpc as server_grpc
from remoteRF_server.drivers.usrp.policy import USRPPolicy
from remoteRF_server.drivers.usrp.uhd_manifest import (
    TARGET_UHD_VERSION,
    build_usrp_schema_v2,
)
from remoteRF_server.server import dynamic_v2 as server_v2


def _add_client_source() -> None:
    configured = os.getenv("REMOTERF_CLIENT_SRC", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(
        Path(__file__).resolve().parents[2] / "client" / "src"
    )
    for candidate in candidates:
        if (candidate / "remoterf").is_dir():
            sys.path.insert(0, str(candidate.resolve()))
            return


_add_client_source()
# This is a required regression test. Import failures must fail collection,
# not turn a broken client/server boundary into a green skipped test.
from remoterf.common.grpc import grpc_pb2_grpc as client_grpc
from remoterf.core.dynamic_v2_transport import DynamicV2Transport, SC16_DTYPE
from remoterf.drivers.dynamic_v2 import build_uhd_bindings


class _FakeRXStreamer:
    def get_max_num_samps(self):
        return 256

    def recv(self, buffer, metadata, timeout):
        self.last_timeout = timeout
        buffer[..., :2] = np.array(
            [1 + 2j, 3 + 4j],
            dtype=buffer.dtype,
        )
        metadata.error_code = "none"
        metadata.has_time_spec = False
        return 2


class _FakeRXMetadata:
    def __init__(self):
        self.error_code = "none"
        self.has_time_spec = False
        self.time_spec = None
        self.more_fragments = False
        self.fragment_offset = 0
        self.out_of_sequence = False


class _FakeLiveSchema:
    def __init__(self):
        self.device = object()
        self.closed = False

    def build_session_metadata_v2(self, policy_restrictions=None):
        return {
            "hardware_profile": "usrp2901",
            "uhd_version": TARGET_UHD_VERSION,
            "uhd_abi": "localhost-test",
            "policy_restrictions": dict(policy_restrictions or {}),
        }

    def invoke_native(self, method, args, positional_args=None):
        if method == "get_rx_stream":
            return _FakeRXStreamer()
        if method == "get_time_now":
            return 1.5
        return None

    def close_native_device(self):
        self.closed = True


class _FakeInventorySchema:
    device = None

    def __init__(self):
        self.live = None

    def get_idl_v2(self):
        return build_usrp_schema_v2()

    def create_session_schema(self):
        self.live = _FakeLiveSchema()
        return self.live


class USRPClientServerLocalhostTests(unittest.TestCase):
    def test_control_handle_binary_rx_and_cleanup_cross_the_grpc_boundary(self):
        inventory = _FakeInventorySchema()
        device_context = {
            "schema": inventory,
            "dtype": "usrp",
            "ident": "LOCALHOST-SERIAL",
            "name": "usrp2901_localhost_test",
        }
        manager = server_v2.SessionManager()
        grpc_server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=8)
        )
        server_grpc.add_DynamicControlV2Servicer_to_server(
            server_v2.DynamicControlV2Servicer(),
            grpc_server,
        )
        server_grpc.add_SampleDataV1Servicer_to_server(
            server_v2.SampleDataV1Servicer(),
            grpc_server,
        )
        port = grpc_server.add_insecure_port("127.0.0.1:0")
        self.assertNotEqual(port, 0)

        fake_usrp_schema = types.ModuleType(
            "remoteRF_server.drivers.usrp.usrp_schema"
        )
        fake_usrp_schema._construct = lambda value: value

        channel = None
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    server_v2,
                    "resolve_gid_for_token",
                    return_value=7,
                )
            )
            stack.enter_context(
                patch.object(
                    server_v2,
                    "get_local_device_context",
                    return_value=device_context,
                )
            )
            stack.enter_context(
                patch.object(
                    server_v2,
                    "token_matches_device",
                    return_value=True,
                )
            )
            stack.enter_context(
                patch.object(
                    server_v2.USRPPolicy,
                    "load",
                    return_value=USRPPolicy({}),
                )
            )
            stack.enter_context(
                patch.object(server_v2, "session_manager", manager)
            )
            stack.enter_context(
                patch.object(
                    server_v2.SampleDataV1Servicer,
                    "_metadata_object",
                    return_value=_FakeRXMetadata(),
                )
            )
            stack.enter_context(
                patch.dict(
                    sys.modules,
                    {
                        "remoteRF_server.drivers.usrp.usrp_schema":
                            fake_usrp_schema,
                    },
                )
            )
            grpc_server.start()
            try:
                channel = grpc.insecure_channel(f"127.0.0.1:{port}")
                grpc.channel_ready_future(channel).result(timeout=5)
                transport = DynamicV2Transport(
                    control_stub=client_grpc.DynamicControlV2Stub(channel),
                    sample_stub=client_grpc.SampleDataV1Stub(channel),
                )
                schema = transport.get_schema("localhost-token")
                uhd, MultiUSRP = build_uhd_bindings(
                    schema,
                    transport_factory=lambda: transport,
                )

                with MultiUSRP("localhost-token") as device:
                    self.assertEqual(
                        device.remoterf_capabilities["hardware_profile"],
                        "usrp2901",
                    )
                    device.set_rx_rate(1e6)
                    stream_args = uhd.usrp.StreamArgs("fc32", "sc16")
                    stream_args.channels = [0]
                    with device.get_rx_stream(stream_args) as streamer:
                        self.assertEqual(
                            streamer.get_max_num_samps(),
                            64 * 1024,
                        )
                        samples = np.full(
                            4,
                            99 + 0j,
                            dtype=np.complex64,
                        )
                        metadata = uhd.types.RXMetadata()
                        count = streamer.recv(
                            samples,
                            metadata,
                            0.25,
                        )

                self.assertEqual(count, 4)
                np.testing.assert_array_equal(
                    samples[:count],
                    np.array(
                        [1 + 2j, 3 + 4j, 1 + 2j, 3 + 4j],
                        dtype=np.complex64,
                    ),
                )
                self.assertEqual(metadata.error_code.value, "none")
                self.assertEqual(manager.metrics()["sessions"], 0)
                self.assertTrue(inventory.live.closed)
            finally:
                if channel is not None:
                    channel.close()
                grpc_server.stop(0).wait()

    def test_sc16_rx_and_tx_round_trip_both_client_layouts_over_localhost_grpc(self):
        class _FakeTXMetadata:
            def __init__(self):
                self.has_time_spec = False
                self.time_spec = None
                self.start_of_burst = False
                self.end_of_burst = False

        class _FakeSC16RXStreamer:
            def __init__(self):
                self.seen = []

            def get_max_num_samps(self):
                return 256

            def issue_stream_cmd(self, command):
                pass

            def recv(self, buffer, metadata, timeout):
                self.seen.append((buffer.dtype, buffer.shape, buffer.itemsize))
                view = buffer.view(SC16_DTYPE)
                if buffer.ndim == 1:
                    view[:2] = np.array([(120, -340), (560, -780)], dtype=SC16_DTYPE)
                else:
                    for ch in range(buffer.shape[0]):
                        view[ch, :2] = np.array(
                            [(120 + ch, -340 - ch), (560 + ch, -780 - ch)],
                            dtype=SC16_DTYPE,
                        )
                metadata.error_code = "none"
                metadata.has_time_spec = False
                return 2

        class _FakeSC16TXStreamer:
            def __init__(self):
                self.sent = []

            def send(self, buffer, metadata, timeout):
                self.sent.append(
                    (
                        buffer.dtype,
                        buffer.shape,
                        buffer.itemsize,
                        buffer.view(SC16_DTYPE).copy(),
                        metadata.start_of_burst,
                        metadata.end_of_burst,
                    )
                )
                return int(buffer.shape[-1])

        class _FakeSC16LiveSchema(_FakeLiveSchema):
            def __init__(self):
                super().__init__()
                self.rx_streamers = []
                self.tx_streamers = []

            def invoke_native(self, method, args, positional_args=None):
                if method == "get_rx_stream":
                    streamer = _FakeSC16RXStreamer()
                    self.rx_streamers.append(streamer)
                    return streamer
                if method == "get_tx_stream":
                    streamer = _FakeSC16TXStreamer()
                    self.tx_streamers.append(streamer)
                    return streamer
                return super().invoke_native(method, args, positional_args=positional_args)

        class _FakeSC16InventorySchema(_FakeInventorySchema):
            def create_session_schema(self):
                self.live = _FakeSC16LiveSchema()
                return self.live

        inventory = _FakeSC16InventorySchema()
        device_context = {
            "schema": inventory,
            "dtype": "usrp",
            "ident": "LOCALHOST-SC16",
            "name": "usrp2901_localhost_sc16",
        }
        manager = server_v2.SessionManager()
        grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
        server_grpc.add_DynamicControlV2Servicer_to_server(
            server_v2.DynamicControlV2Servicer(),
            grpc_server,
        )
        server_grpc.add_SampleDataV1Servicer_to_server(
            server_v2.SampleDataV1Servicer(),
            grpc_server,
        )
        port = grpc_server.add_insecure_port("127.0.0.1:0")
        self.assertNotEqual(port, 0)

        fake_usrp_schema = types.ModuleType(
            "remoteRF_server.drivers.usrp.usrp_schema"
        )
        fake_usrp_schema._construct = lambda value: value
        fake_uhd = types.SimpleNamespace(
            types=types.SimpleNamespace(
                RXMetadata=_FakeRXMetadata,
                TXMetadata=_FakeTXMetadata,
                TXAsyncMetadata=None,
                StreamCMD=lambda mode: types.SimpleNamespace(mode=mode),
                StreamMode=types.SimpleNamespace(stop_cont="stop_cont"),
            )
        )

        channel = None
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(server_v2, "resolve_gid_for_token", return_value=7)
            )
            stack.enter_context(
                patch.object(
                    server_v2,
                    "get_local_device_context",
                    return_value=device_context,
                )
            )
            stack.enter_context(
                patch.object(server_v2, "token_matches_device", return_value=True)
            )
            stack.enter_context(
                patch.object(
                    server_v2.USRPPolicy,
                    "load",
                    return_value=USRPPolicy({}),
                )
            )
            stack.enter_context(
                patch.object(server_v2, "session_manager", manager)
            )
            stack.enter_context(
                patch.object(
                    server_v2.SampleDataV1Servicer,
                    "_metadata_object",
                    side_effect=lambda schema, tag, payload: (
                        _FakeTXMetadata() if tag == "TXMetadata" else _FakeRXMetadata()
                    ),
                )
            )
            stack.enter_context(
                patch.dict(
                    sys.modules,
                    {
                        "remoteRF_server.drivers.usrp.usrp_schema": fake_usrp_schema,
                        "uhd": fake_uhd,
                    },
                )
            )
            grpc_server.start()
            try:
                channel = grpc.insecure_channel(f"127.0.0.1:{port}")
                grpc.channel_ready_future(channel).result(timeout=5)
                transport = DynamicV2Transport(
                    control_stub=client_grpc.DynamicControlV2Stub(channel),
                    sample_stub=client_grpc.SampleDataV1Stub(channel),
                )
                schema = transport.get_schema("localhost-token")
                uhd, MultiUSRP = build_uhd_bindings(
                    schema,
                    transport_factory=lambda: transport,
                )

                with MultiUSRP("localhost-token") as device:
                    args1 = uhd.usrp.StreamArgs("sc16", "sc16")
                    args1.channels = [0]
                    with device.get_rx_stream(args1) as rx1:
                        self.assertEqual(rx1.get_max_num_samps(), 64 * 1024)
                        struct_rx = np.empty(4, dtype=SC16_DTYPE)
                        md = uhd.types.RXMetadata()
                        self.assertEqual(rx1.recv(struct_rx, md, 0.25), 4)
                        np.testing.assert_array_equal(
                            struct_rx,
                            np.array(
                                [(120, -340), (560, -780), (120, -340), (560, -780)],
                                dtype=SC16_DTYPE,
                            ),
                        )

                        iq_rx = np.zeros((4, 2), dtype=np.int16)
                        self.assertEqual(rx1.recv(iq_rx, md, 0.25), 4)
                        np.testing.assert_array_equal(
                            iq_rx,
                            np.array(
                                [[120, -340], [560, -780], [120, -340], [560, -780]],
                                dtype=np.int16,
                            ),
                        )

                    tx1_struct = np.array([(11, -22), (33, -44)], dtype=SC16_DTYPE)
                    tx1_iq = np.array([[55, -66], [77, -88], [99, -111]], dtype=np.int16)
                    with device.get_tx_stream(args1) as tx1:
                        self.assertEqual(tx1.get_max_num_samps(), 64 * 1024)
                        tx_md = uhd.types.TXMetadata()
                        self.assertEqual(tx1.send(tx1_struct, tx_md, 0.25), 2)
                        self.assertEqual(tx1.send(tx1_iq, tx_md, 0.25), 3)

                    args2 = uhd.usrp.StreamArgs("sc16", "sc16")
                    args2.channels = [0, 1]
                    with device.get_rx_stream(args2) as rx2:
                        iq2_rx = np.zeros((2, 2, 2), dtype=np.int16)
                        self.assertEqual(rx2.recv(iq2_rx, md, 0.25), 2)
                        np.testing.assert_array_equal(
                            iq2_rx,
                            np.array(
                                [
                                    [[120, -340], [560, -780]],
                                    [[121, -341], [561, -781]],
                                ],
                                dtype=np.int16,
                            ),
                        )

                    tx2_iq = np.array(
                        [[[1, -2], [3, -4]], [[5, -6], [7, -8]]],
                        dtype=np.int16,
                    )
                    with device.get_tx_stream(args2) as tx2:
                        self.assertEqual(tx2.send(tx2_iq, tx_md, 0.25), 2)

                sent_1ch = inventory.live.tx_streamers[0].sent
                self.assertEqual(sent_1ch[0][:3], (np.dtype(np.int32), (2,), 4))
                np.testing.assert_array_equal(sent_1ch[0][3], tx1_struct)
                self.assertEqual(sent_1ch[1][:3], (np.dtype(np.int32), (3,), 4))
                np.testing.assert_array_equal(
                    sent_1ch[1][3],
                    np.array([(55, -66), (77, -88), (99, -111)], dtype=SC16_DTYPE),
                )
                # Closing the TX streamer issues a 0-sample EOB flush with the int32 view.
                self.assertEqual(
                    (sent_1ch[2][0], sent_1ch[2][1], sent_1ch[2][4], sent_1ch[2][5]),
                    (np.dtype(np.int32), (0,), False, True),
                )

                sent_2ch = inventory.live.tx_streamers[1].sent
                self.assertEqual(sent_2ch[0][:3], (np.dtype(np.int32), (2, 2), 4))
                np.testing.assert_array_equal(
                    sent_2ch[0][3],
                    np.array([[(1, -2), (3, -4)], [(5, -6), (7, -8)]], dtype=SC16_DTYPE),
                )
                self.assertEqual(
                    (sent_2ch[1][0], sent_2ch[1][1], sent_2ch[1][4], sent_2ch[1][5]),
                    (np.dtype(np.int32), (2, 0), False, True),
                )

                self.assertEqual(manager.metrics()["sessions"], 0)
                self.assertTrue(inventory.live.closed)
            finally:
                if channel is not None:
                    channel.close()
                grpc_server.stop(0).wait()


if __name__ == "__main__":
    unittest.main()
