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

"""Server integration tests for multi-range frequency limits (device_manager + Dynamic v2)."""
from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

import numpy as np

from remoterf_device_core.drivers.hackrf.hackrf_schema import HackRFSchema
from remoterf_device_core.drivers.usrp.usrp_schema import UsrpSchema
from remoterf_device_core.idl import DeviceSchema
from remoteRF_server.common.grpc import grpc_pb2
from remoteRF_server.common.grpc.v2_codec import decode_value, encode_value
from remoteRF_server.server import device_manager
from remoteRF_server.server.dynamic_v2 import (
    DynamicControlV2Servicer,
    SampleDataV1Servicer,
    session_manager,
)


class _UnsupportedSchema(DeviceSchema):
    device_type = "custom_sensor"
    driver_version = "0.0.1"


class _FakeTuneRequest:
    def __init__(self, target_freq, lo_offset=0.0):
        self.target_freq = float(target_freq)
        self.rf_freq = float(target_freq) + float(lo_offset)
        self.dsp_freq = 0.0
        self.rf_freq_policy = "MANUAL" if lo_offset else "AUTO"
        self.dsp_freq_policy = "AUTO"


class _FakeStreamArgs:
    def __init__(self, cpu_format, otw_format):
        self.cpu_format = cpu_format
        self.otw_format = otw_format
        self.channels = []
        self.args = ""


class _FakeTXMetadata:
    def __init__(self):
        self.has_time_spec = False
        self.start_of_burst = False
        self.end_of_burst = False


class _FakeRXMetadata:
    def __init__(self):
        self.error_code = "none"
        self.has_time_spec = False
        self.time_spec = None

    def reset(self):
        pass


class _FakeTXStreamer:
    def __init__(self):
        self.sent = []

    def send(self, buffer, metadata, timeout):
        self.sent.append(int(buffer.shape[-1]))
        return int(buffer.shape[-1])


class _FakeRXStreamer:
    def get_max_num_samps(self):
        return 256

    def recv(self, buffer, metadata, timeout):
        metadata.error_code = "none"
        return int(buffer.shape[-1])


class _FakeNativeUSRP:
    def __init__(self):
        self.tx_freq = 915e6
        self.rx_freq = 915e6
        self.tx_rate = 4e6
        self.rx_rate = 4e6
        self.tx_streamer = _FakeTXStreamer()
        self.rx_streamer = _FakeRXStreamer()

    def get_rx_num_channels(self):
        return 1

    def get_tx_num_channels(self):
        return 1

    def get_usrp_rx_info(self, chan=0):
        return {"mboard_id": "B210", "mboard_name": "B210", "mboard_serial": "SRV01"}

    def get_usrp_tx_info(self, chan=0):
        return {"mboard_id": "B210", "mboard_name": "B210", "mboard_serial": "SRV01"}

    def get_mboard_name(self, mboard=0):
        return "B210"

    def get_tx_freq(self, chan=0):
        return self.tx_freq

    def set_tx_freq(self, req, chan=0):
        self.tx_freq = float(getattr(req, "target_freq", req))
        return self.tx_freq

    def get_rx_freq(self, chan=0):
        return self.rx_freq

    def set_rx_freq(self, req, chan=0):
        self.rx_freq = float(getattr(req, "target_freq", req))
        return self.rx_freq

    def get_tx_rate(self, chan=0):
        return self.tx_rate

    def set_tx_rate(self, rate, chan=0):
        self.tx_rate = float(rate)

    def get_tx_stream(self, stream_args):
        return self.tx_streamer

    def get_rx_stream(self, stream_args):
        return self.rx_streamer


def _fake_uhd():
    return types.SimpleNamespace(
        get_version_string=lambda: "4.10.0.0",
        get_abi_string=lambda: "4.10.0",
        types=types.SimpleNamespace(
            TuneRequest=_FakeTuneRequest,
            TXMetadata=_FakeTXMetadata,
            RXMetadata=_FakeRXMetadata,
            DeviceAddr=lambda raw="": raw,
        ),
        usrp=types.SimpleNamespace(StreamArgs=_FakeStreamArgs),
        libpyuhd=types.SimpleNamespace(
            types=types.SimpleNamespace(
                tune_request=_FakeTuneRequest,
                device_addr=lambda raw="": raw,
            )
        ),
    )


class _FakeHackRF:
    def __init__(self):
        self.center_freq = 915_000_000
        self.sample_rate = 4_000_000
        self.buffer = bytearray(b"\x01\x02")


class ServerDeviceManagerFreqRangeTests(unittest.TestCase):
    def test_accepts_freq_ranges_on_hackrf_and_usrp_and_rejects_invalid_or_unsupported(self):
        drivers = {
            "hackrf": HackRFSchema,
            "usrp": UsrpSchema,
            "custom_sensor": _UnsupportedSchema,
        }
        with (
            patch.object(device_manager, "get_driver_class", side_effect=lambda t: drivers[t]),
            patch.object(HackRFSchema, "make_device", return_value=_FakeHackRF()),
        ):
            # Valid HackRF config
            schema, _, dtype = device_manager._connect_from_record(
                {
                    "TYPE": "hackrf",
                    "NAME": "hackrf-0",
                    "INIT": {
                        "serial": "H0",
                        "tx_allow": [[902e6, 928e6]],
                        "tx_deny": [[910e6, 912e6]],
                        "rx_allow": [[70e6, 6e9]],
                        "rx_deny": [],
                    },
                },
                connect=True,
            )
            self.assertEqual(dtype, "hackrf")
            self.assertIsNotNone(schema)
            self.assertIsNotNone(schema._freq_limits)

            # Valid USRP session-scoped config
            usrp_schema, _, dtype = device_manager._connect_from_record(
                {
                    "TYPE": "usrp",
                    "NAME": "usrp-0",
                    "INIT": {
                        "serial": "U0",
                        "tx_allow": [[902e6, 928e6], [2.40e9, 2.48e9]],
                        "tx_deny": [[2.45e9, 2.46e9]],
                    },
                },
                connect=True,
            )
            self.assertEqual(dtype, "usrp")
            self.assertIsNotNone(usrp_schema)
            self.assertIsNotNone(usrp_schema._freq_limits)
            self.assertEqual(usrp_schema._inventory, {"serial": "U0"})

            # Pluto-only key rejected on HackRF and USRP -> offline (None)
            for drv in ("hackrf", "usrp"):
                bad, _, _ = device_manager._connect_from_record(
                    {"TYPE": drv, "NAME": f"{drv}-bad", "INIT": {"tx_max_gain_db": -10}},
                    connect=True,
                )
                self.assertIsNone(bad)

            # Invalid freq range fails closed at startup -> offline (None)
            bad_range, _, _ = device_manager._connect_from_record(
                {"TYPE": "usrp", "NAME": "usrp-bad", "INIT": {"tx_allow": [[928e6, 902e6]]}},
                connect=True,
            )
            self.assertIsNone(bad_range)

            # Unsupported driver rejects freq limit keys -> offline (None)
            bad_other, _, _ = device_manager._connect_from_record(
                {"TYPE": "custom_sensor", "NAME": "other-bad", "INIT": {"rx_allow": [[88e6, 108e6]]}},
                connect=True,
            )
            self.assertIsNone(bad_other)


class ServerDynamicV2FreqRangeTests(unittest.TestCase):
    def setUp(self):
        session_manager.close_all()
        self.native = _FakeNativeUSRP()
        self.bypass = False
        self.inventory = UsrpSchema()
        self.inventory.bind_inventory(
            {
                "serial": "SRV01",
                "tx_allow": [[902e6, 928e6], [2.40e9, 2.48e9]],
                "tx_deny": [[2.45e9, 2.46e9]],
                "rx_allow": [[902e6, 928e6]],
                "rx_deny": [[920e6, 922e6]],
                "admin_group_override": True,
            }
        )
        self.fake_uhd = _fake_uhd()
        self.control = DynamicControlV2Servicer()
        self.samples = SampleDataV1Servicer()

        self.patches = [
            patch.dict(sys.modules, {"uhd": self.fake_uhd}),
            patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", self.fake_uhd),
            patch("remoterf_device_core.drivers.usrp.usrp_schema._connect_usrp", return_value=self.native),
            patch("remoteRF_server.server.dynamic_v2.resolve_gid_for_token", return_value=7),
            patch("remoteRF_server.server.dynamic_v2.token_matches_device", return_value=True),
            patch("remoteRF_server.server.dynamic_v2.diag_blocks", return_value=False),
            patch("remoteRF_server.server.dynamic_v2.host_session_proxy.route_for_token", return_value=None),
            patch("remoteRF_server.server.dynamic_v2.host_session_proxy.is_remote_session", return_value=False),
            patch(
                "remoteRF_server.server.dynamic_v2.get_local_device_context",
                side_effect=lambda gid: {
                    "gid": 7,
                    "schema": self.inventory,
                    "dtype": "usrp",
                    "ident": "SRV01",
                    "name": "usrp-srv",
                    "reserved": True,
                    "lockout_bypass": self.bypass,
                },
            ),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(session_manager.close_all)

    def _open(self):
        schema_hash = self.inventory.get_idl_v2()["schema_hash"]
        resp = self.control.OpenSession(
            grpc_pb2.OpenSessionRequest(
                token="tok-srv",
                requested_schema_hash=schema_hash,
                control_protocol_version="2.0",
                streaming_protocol_version="1.0",
            ),
            None,
        )
        self.assertEqual(resp.error.code, "")
        return resp

    def _invoke(self, session_id, handle, method, args):
        req = grpc_pb2.InvokeRequest(session_id=session_id, handle=handle, method=method)
        for name, val in args.items():
            req.args.add(name=name, value=encode_value(val))
        return self.control.Invoke(req, None)

    def test_v2_invoke_sample_stream_and_admin_bypass(self):
        opened = self._open()
        sid, root = opened.session_id, opened.device_handle

        # Out-of-band TX tune returns typed RemoteRFPolicyError (not NativeUHDError).
        bad_tx = self._invoke(sid, root, "set_tx_freq", {"tune_request": 2.448e9, "chan": 0})
        self.assertEqual(bad_tx.error.code, "RemoteRFPolicyError")
        self.assertIn("2450–2460 MHz", bad_tx.error.message)
        self.assertEqual(self.native.tx_freq, 915e6)

        # Denied RX tune returns RemoteRFPolicyError.
        bad_rx = self._invoke(sid, root, "set_rx_freq", {"tune_request": 921e6, "chan": 0})
        self.assertEqual(bad_rx.error.code, "RemoteRFPolicyError")
        self.assertIn("920–922 MHz", bad_rx.error.message)

        # Open TX streamer at valid freq/rate (get_tx_stream takes positional_only stream_args).
        tx_resp = self._invoke(
            sid,
            root,
            "get_tx_stream",
            {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
        )
        self.assertEqual(tx_resp.error.code, "")
        handle_info = decode_value(tx_resp.result)
        tx_handle = handle_info["__remoterf_handle__"]
        tx_gen = int(handle_info["generation"])

        # Widening TX rate while TX streamer handle is open -> refused.
        bad_rate = self._invoke(sid, root, "set_tx_rate", {"rate": 30e6, "chan": 0})
        self.assertEqual(bad_rate.error.code, "RemoteRFPolicyError")
        self.assertEqual(self.native.tx_rate, 4e6)

        # If hardware TX freq drifts into deny, SampleStream TX refuses before native send().
        self.native.tx_freq = 2.455e9
        payload = np.zeros(4, dtype="<c8").tobytes()
        frames = list(
            self.samples.SampleStream(
                iter(
                    [
                        grpc_pb2.SampleFrame(
                            session_id=sid,
                            handle=tx_handle,
                            generation=tx_gen,
                            operation_id="op-denied",
                            sequence=0,
                            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                            kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                            credits=1,
                        ),
                        grpc_pb2.SampleFrame(
                            session_id=sid,
                            handle=tx_handle,
                            generation=tx_gen,
                            operation_id="op-denied",
                            sequence=1,
                            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                            kind=grpc_pb2.SAMPLE_FRAME_DATA,
                            dtype="<c8",
                            shape=[4],
                            channels=[0],
                            sample_count=4,
                            payload=payload,
                            timeout_sec=0.1,
                            metadata_json="{}",
                        ),
                    ]
                ),
                None,
            )
        )
        self.assertEqual(frames[-1].kind, grpc_pb2.SAMPLE_FRAME_ERROR)
        self.assertEqual(frames[-1].error.code, "RemoteRFPolicyError")
        self.assertEqual(self.native.tx_streamer.sent, [])

        # Enabling reservation lockout_bypass allows both Invoke and SampleStream TX.
        self.bypass = True
        ok_tx = self._invoke(sid, root, "set_tx_freq", {"tune_request": 2.455e9, "chan": 0})
        self.assertEqual(ok_tx.error.code, "")
        frames_ok = list(
            self.samples.SampleStream(
                iter(
                    [
                        grpc_pb2.SampleFrame(
                            session_id=sid,
                            handle=tx_handle,
                            generation=tx_gen,
                            operation_id="op-bypass",
                            sequence=2,
                            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                            kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                            credits=1,
                        ),
                        grpc_pb2.SampleFrame(
                            session_id=sid,
                            handle=tx_handle,
                            generation=tx_gen,
                            operation_id="op-bypass",
                            sequence=3,
                            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                            kind=grpc_pb2.SAMPLE_FRAME_DATA,
                            dtype="<c8",
                            shape=[4],
                            channels=[0],
                            sample_count=4,
                            payload=payload,
                            timeout_sec=0.1,
                            metadata_json="{}",
                        ),
                    ]
                ),
                None,
            )
        )
        self.assertEqual(frames_ok[-1].kind, grpc_pb2.SAMPLE_FRAME_RESULT)
        self.assertEqual(self.native.tx_streamer.sent, [4])


if __name__ == "__main__":
    unittest.main()
