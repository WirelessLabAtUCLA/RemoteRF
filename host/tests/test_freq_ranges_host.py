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

"""Host integration tests for multi-range frequency limits (v1 RPC + Dynamic v2 bridge)."""
from __future__ import annotations

import sys
import threading
import types
import unittest
from unittest.mock import patch

import numpy as np

from remoterf_device_core.drivers.hackrf.hackrf_schema import HackRFSchema
from remoterf_device_core.drivers.usrp.usrp_schema import UsrpSchema
from remoterf_device_core.idl import DeviceSchema
from remoteRF_host.common.grpc import grpc_pb2
from remoteRF_host.common.grpc.v2_codec import decode_value, encode_value
from remoteRF_host.common.utils import map_arg, unmap_arg
from remoteRF_host.server import device_manager
from remoteRF_host.server.dynamic_v2 import session_manager
from remoteRF_host.server.dynamic_v2_bridge import run_bridge
from remoteRF_host.server.rpc_manager import RpcManager


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
        self.tx_freqs = {0: 915e6, 1: 915e6}
        self.rx_freqs = {0: 915e6, 1: 915e6}
        self.tx_rates = {0: 4e6, 1: 4e6}
        self.rx_rates = {0: 4e6, 1: 4e6}
        self.tx_streamer = _FakeTXStreamer()
        self.rx_streamer = _FakeRXStreamer()

    @property
    def tx_freq(self):
        return self.tx_freqs[0]

    @tx_freq.setter
    def tx_freq(self, value):
        self.tx_freqs[0] = float(value)

    @property
    def rx_freq(self):
        return self.rx_freqs[0]

    @rx_freq.setter
    def rx_freq(self, value):
        self.rx_freqs[0] = float(value)

    @property
    def tx_rate(self):
        return self.tx_rates[0]

    @tx_rate.setter
    def tx_rate(self, value):
        self.tx_rates[0] = float(value)

    def get_rx_num_channels(self):
        return 2

    def get_tx_num_channels(self):
        return 2

    def get_usrp_rx_info(self, chan=0):
        return {"mboard_id": "B210", "mboard_name": "B210", "mboard_serial": "HOST01"}

    def get_usrp_tx_info(self, chan=0):
        return {"mboard_id": "B210", "mboard_name": "B210", "mboard_serial": "HOST01"}

    def get_mboard_name(self, mboard=0):
        return "B210"

    def get_tx_freq(self, chan=0):
        return self.tx_freqs[int(chan)]

    def set_tx_freq(self, tune_request, chan=0):
        self.tx_freqs[int(chan)] = float(getattr(tune_request, "target_freq", tune_request))
        return self.tx_freqs[int(chan)]

    def get_rx_freq(self, chan=0):
        return self.rx_freqs[int(chan)]

    def set_rx_freq(self, tune_request, chan=0):
        self.rx_freqs[int(chan)] = float(getattr(tune_request, "target_freq", tune_request))
        return self.rx_freqs[int(chan)]

    def get_tx_rate(self, chan=0):
        return self.tx_rates[int(chan)]

    def set_tx_rate(self, rate, chan=0):
        if int(chan) >= (1 << 32):
            for ch in self.tx_rates:
                self.tx_rates[ch] = float(rate)
        else:
            self.tx_rates[int(chan)] = float(rate)

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
        self.center_freq = 104_000_000
        self.sample_rate = 4_000_000
        self.buffer = bytearray(b"\x01\x02\x03\x04")
        self.tx_starts = 0

    def start_tx(self):
        self.tx_starts += 1

    def stop_tx(self):
        pass

    def read_samples(self, count):
        return np.zeros(count, dtype=np.complex64)


class HostDeviceManagerAndRpcFreqRangeTests(unittest.TestCase):
    def test_connect_from_record_validates_and_rejects_bad_configs(self):
        drivers = {
            "hackrf": HackRFSchema,
            "usrp": UsrpSchema,
            "custom_sensor": _UnsupportedSchema,
        }
        with (
            patch.object(device_manager, "get_driver_class", side_effect=lambda t: drivers[t]),
            patch.object(HackRFSchema, "make_device", return_value=_FakeHackRF()),
        ):
            hackrf_schema, _, dtype = device_manager._connect_from_record(
                {
                    "TYPE": "hackrf",
                    "NAME": "host-hackrf",
                    "INIT": {
                        "serial": "H1",
                        "tx_allow": [[902e6, 928e6]],
                        "tx_deny": [[910e6, 912e6]],
                        "rx_allow": [[88e6, 108e6]],
                        "rx_deny": [[98e6, 100e6]],
                        "admin_group_override": True,
                    },
                }
            )
            self.assertEqual(dtype, "hackrf")
            self.assertIsNotNone(hackrf_schema)
            self.assertIsNotNone(hackrf_schema._freq_limits)

            usrp_schema, _, dtype = device_manager._connect_from_record(
                {
                    "TYPE": "usrp",
                    "NAME": "host-usrp",
                    "INIT": {
                        "serial": "HOST01",
                        "tx_allow": [[902e6, 928e6]],
                        "rx_deny": [[1.57e9, 1.58e9]],
                    },
                }
            )
            self.assertEqual(dtype, "usrp")
            self.assertIsNotNone(usrp_schema)
            self.assertIsNotNone(usrp_schema._freq_limits)
            self.assertEqual(usrp_schema._inventory, {"serial": "HOST01"})

            # Pluto-only key rejected on non-Pluto driver
            bad_pluto_key, _, _ = device_manager._connect_from_record(
                {"TYPE": "hackrf", "NAME": "bad", "INIT": {"tx_enabled": False}}
            )
            self.assertIsNone(bad_pluto_key)

            # Invalid range fails closed
            bad_range, _, _ = device_manager._connect_from_record(
                {"TYPE": "usrp", "NAME": "bad", "INIT": {"tx_allow": [[928e6, 902e6]]}}
            )
            self.assertIsNone(bad_range)

            # Unsupported driver rejected
            bad_other, _, _ = device_manager._connect_from_record(
                {"TYPE": "custom_sensor", "NAME": "bad", "INIT": {"rx_allow": [[88e6, 108e6]]}}
            )
            self.assertIsNone(bad_other)

            # Misspelled or unknown limit keys fail closed on HackRF and USRP
            for drv in ("hackrf", "usrp"):
                for bad_init in (
                    {"serial": "X", "tx_alow": [[902e6, 928e6]]},
                    {"serial": "X", "tx_deny_ranges": [[2.45e9, 2.46e9]]},
                ):
                    bad_typo, _, _ = device_manager._connect_from_record(
                        {"TYPE": drv, "NAME": f"{drv}-typo", "INIT": bad_init}
                    )
                    self.assertIsNone(bad_typo)

    def test_stale_pluto_driver_without_freq_limits_support_rejected_for_new_keys(self):
        sys.modules.setdefault("adi", types.ModuleType("adi"))
        made = []
        warnings = []

        class PlutoSchema:
            lockout_bypass = False

            @staticmethod
            def make_device(**kwargs):
                made.append(kwargs)
                return object()

            def bind(self, raw):
                self.device = raw

            def configure_lockouts(self, cfg):
                self.cfg = cfg

            def dispatch(self, name, args):
                return None

        PlutoSchema.dispatch.__qualname__ = "PlutoSchema.dispatch"

        with (
            patch.object(device_manager, "get_driver_class", return_value=PlutoSchema),
            patch.object(device_manager, "warn", side_effect=lambda msg, **kw: warnings.append(msg)),
        ):
            for init in (
                {"tx_allow": [[2.40e9, 2.48e9]]},
                {"tx_min_freq": 902e6, "tx_max_freq": 928e6, "tx_deny": [[914e6, 916e6]]},
                {"rx_deny": [[1.57e9, 1.58e9]]},
            ):
                schema, _, _ = device_manager._connect_from_record(
                    {"TYPE": "adalm_pluto", "NAME": "stale-pluto", "INIT": init}
                )
                self.assertIsNone(schema)
        self.assertEqual(made, [])
        self.assertTrue(any("allow/deny" in msg and "device left offline" in msg for msg in warnings))

        PlutoSchema._freq_limits_for = lambda self, side: None
        with patch.object(device_manager, "get_driver_class", return_value=PlutoSchema):
            modern, _, _ = device_manager._connect_from_record(
                {"TYPE": "adalm_pluto", "NAME": "modern-pluto", "INIT": {"tx_allow": [[2.40e9, 2.48e9]]}}
            )
            self.assertIsNotNone(modern)

    def test_host_rpc_manager_enforces_hackrf_and_propagates_bypass(self):
        fake = _FakeHackRF()
        drivers = {"hackrf": HackRFSchema}
        with (
            patch.object(device_manager, "get_driver_class", side_effect=lambda t: drivers[t]),
            patch.object(HackRFSchema, "make_device", return_value=fake),
        ):
            schema, ident, dtype = device_manager._connect_from_record(
                {
                    "TYPE": "hackrf",
                    "NAME": "host-hackrf",
                    "INIT": {
                        "serial": "H1",
                        "tx_allow": [[902e6, 928e6]],
                        "rx_allow": [[88e6, 108e6]],
                        "rx_deny": [[98e6, 100e6]],
                        "admin_group_override": True,
                    },
                }
            )
        self.assertIsNotNone(schema)

        gid = 42
        state = device_manager._DeviceState(
            schema=schema,
            name="host-hackrf",
            ident=ident,
            dtype=dtype,
            init={},
            io_lock=threading.RLock(),
        )
        rpc = RpcManager()
        with patch.dict(device_manager._devices, {gid: state}, clear=False):
            device_manager.prepare_reservation(gid, bypass=False, tag="res-1", resync=True)

            # TX at 104 MHz refused via host RPC path (used by tunnel & DirectHost).
            res = rpc.run_rpc(
                function_name="hackrf:start_tx:CALL0",
                args={"g": map_arg(gid), "__rrf_reservation_tag": map_arg("res-1")},
            )
            self.assertIn("Error", res)
            self.assertIn("902–928 MHz", str(unmap_arg(res["Error"])))
            self.assertEqual(fake.tx_starts, 0)

            # RX inside deny refused.
            fake.center_freq = 99_000_000
            rx_res = rpc.run_rpc(
                function_name="hackrf:read_samples:CALLN",
                args={"g": map_arg(gid), "num_samples": map_arg(4), "__rrf_reservation_tag": map_arg("res-1")},
            )
            self.assertIn("Error", rx_res)
            self.assertIn("98–100 MHz", str(unmap_arg(rx_res["Error"])))

            # Admin bypass propagated through prepare_reservation -> reset_before_rpc.
            device_manager.prepare_reservation(gid, bypass=True, tag="res-2", resync=True)
            ok = rpc.run_rpc(
                function_name="hackrf:start_tx:CALL0",
                args={"g": map_arg(gid), "__rrf_reservation_tag": map_arg("res-2")},
            )
            self.assertNotIn("Error", ok)
            self.assertEqual(fake.tx_starts, 1)
            schema.call_stop_tx()


class HostDynamicV2BridgeFreqRangeTests(unittest.TestCase):
    def setUp(self):
        session_manager.close_all()
        self.native = _FakeNativeUSRP()
        self.bypass = False
        self.inventory = UsrpSchema()
        self.inventory.bind_inventory(
            {
                "serial": "HOST01",
                "tx_allow": [[902e6, 928e6], [2.40e9, 2.48e9]],
                "tx_deny": [[2.45e9, 2.46e9]],
                "rx_allow": [[902e6, 928e6]],
                "rx_deny": [[920e6, 922e6]],
                "admin_group_override": True,
            }
        )
        self.fake_uhd = _fake_uhd()
        self.patches = [
            patch.dict(sys.modules, {"uhd": self.fake_uhd}),
            patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", self.fake_uhd),
            patch("remoterf_device_core.drivers.usrp.usrp_schema._connect_usrp", return_value=self.native),
            patch("remoteRF_host.server.dynamic_v2.resolve_gid_for_token", return_value=7),
            patch("remoteRF_host.server.dynamic_v2.token_matches_device", return_value=True),
            patch(
                "remoteRF_host.server.dynamic_v2.get_local_device_context",
                side_effect=lambda gid: {
                    "gid": 7,
                    "schema": self.inventory,
                    "dtype": "usrp",
                    "ident": "HOST01",
                    "name": "usrp-host",
                    "reserved": True,
                    "lockout_bypass": self.bypass,
                },
            ),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(session_manager.close_all)

    @staticmethod
    def _call(operation, request, response_type):
        result = run_bridge(
            operation=operation,
            args={"wire": map_arg(request.SerializeToString())},
            gid=7,
        )
        return response_type.FromString(unmap_arg(result["wire"]))

    def test_host_v2_bridge_enforces_tx_rx_and_bypass(self):
        schema_hash = self.inventory.get_idl_v2()["schema_hash"]
        opened = self._call(
            "OpenSession",
            grpc_pb2.OpenSessionRequest(
                requested_schema_hash=schema_hash,
                control_protocol_version="2.0",
                streaming_protocol_version="1.0",
            ),
            grpc_pb2.OpenSessionResponse,
        )
        self.assertEqual(opened.error.code, "")
        sid, root = opened.session_id, opened.device_handle

        # TX touching deny range refused with typed RemoteRFPolicyError.
        req = grpc_pb2.InvokeRequest(session_id=sid, handle=root, method="set_tx_freq")
        req.args.add(name="tune_request", value=encode_value(2.448e9))
        req.args.add(name="chan", value=encode_value(0))
        resp = self._call("Invoke", req, grpc_pb2.InvokeResponse)
        self.assertEqual(resp.error.code, "RemoteRFPolicyError")
        self.assertIn("2450–2460 MHz", resp.error.message)

        # RX denied refused.
        rx_req = grpc_pb2.InvokeRequest(session_id=sid, handle=root, method="set_rx_freq")
        rx_req.args.add(name="tune_request", value=encode_value(921e6))
        rx_req.args.add(name="chan", value=encode_value(0))
        rx_resp = self._call("Invoke", rx_req, grpc_pb2.InvokeResponse)
        self.assertEqual(rx_resp.error.code, "RemoteRFPolicyError")

        # Open TX streamer at valid freq, then push hardware freq into deny and attempt TX SampleFrame.
        stream_req = grpc_pb2.InvokeRequest(session_id=sid, handle=root, method="get_tx_stream")
        stream_req.args.add(
            name="__args__",
            value=encode_value(
                [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]
            ),
        )
        stream_resp = self._call("Invoke", stream_req, grpc_pb2.InvokeResponse)
        self.assertEqual(stream_resp.error.code, "")
        handle_info = decode_value(stream_resp.result)
        tx_handle = handle_info["__remoterf_handle__"]
        tx_gen = int(handle_info["generation"])

        self.native.tx_freq = 2.455e9
        self._call(
            "SampleFrame",
            grpc_pb2.SampleFrame(
                session_id=sid,
                handle=tx_handle,
                generation=tx_gen,
                operation_id="host-op-1",
                sequence=0,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                credits=1,
            ),
            grpc_pb2.SampleFrame,
        )
        err_frame = self._call(
            "SampleFrame",
            grpc_pb2.SampleFrame(
                session_id=sid,
                handle=tx_handle,
                generation=tx_gen,
                operation_id="host-op-1",
                sequence=1,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_DATA,
                dtype="<c8",
                shape=[4],
                channels=[0],
                sample_count=4,
                payload=np.zeros(4, dtype="<c8").tobytes(),
                timeout_sec=0.1,
                metadata_json="{}",
            ),
            grpc_pb2.SampleFrame,
        )
        self.assertEqual(err_frame.kind, grpc_pb2.SAMPLE_FRAME_ERROR)
        self.assertEqual(err_frame.error.code, "RemoteRFPolicyError")
        self.assertEqual(self.native.tx_streamer.sent, [])

        # Admin bypass succeeds on host too.
        self.bypass = True
        self._call(
            "SampleFrame",
            grpc_pb2.SampleFrame(
                session_id=sid,
                handle=tx_handle,
                generation=tx_gen,
                operation_id="host-op-2",
                sequence=2,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                credits=1,
            ),
            grpc_pb2.SampleFrame,
        )
        ok_frame = self._call(
            "SampleFrame",
            grpc_pb2.SampleFrame(
                session_id=sid,
                handle=tx_handle,
                generation=tx_gen,
                operation_id="host-op-2",
                sequence=3,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_DATA,
                dtype="<c8",
                shape=[4],
                channels=[0],
                sample_count=4,
                payload=np.zeros(4, dtype="<c8").tobytes(),
                timeout_sec=0.1,
                metadata_json="{}",
            ),
            grpc_pb2.SampleFrame,
        )
        self.assertEqual(ok_frame.kind, grpc_pb2.SAMPLE_FRAME_RESULT)
        self.assertEqual(self.native.tx_streamer.sent, [4])

    def test_host_v2_helper_wrapped_stream_args_records_channels_and_never_falls_back(self):
        schema_hash = self.inventory.get_idl_v2()["schema_hash"]
        opened = self._call(
            "OpenSession",
            grpc_pb2.OpenSessionRequest(
                requested_schema_hash=schema_hash,
                control_protocol_version="2.0",
                streaming_protocol_version="1.0",
            ),
            grpc_pb2.OpenSessionResponse,
        )
        self.assertEqual(opened.error.code, "")
        sid, root = opened.session_id, opened.device_handle

        self.native.tx_rates[0] = 1e6
        self.native.tx_freqs[0] = 915e6
        self.native.tx_rates[1] = 1e6
        self.native.tx_freqs[1] = 927e6

        wrapped = {
            "__remoterf_uhd_helper__": "stream",
            "stream_args": {
                "__uhd_type__": "StreamArgs",
                "cpu_format": "fc32",
                "otw_format": "sc16",
                "channels": [1],
            },
        }
        stream_req = grpc_pb2.InvokeRequest(session_id=sid, handle=root, method="get_tx_stream")
        stream_req.args.add(name="__args__", value=encode_value([wrapped]))
        stream_resp = self._call("Invoke", stream_req, grpc_pb2.InvokeResponse)
        self.assertEqual(stream_resp.error.code, "")
        tx_info = decode_value(stream_resp.result)
        tx_handle = tx_info["__remoterf_handle__"]
        tx_gen = int(tx_info["generation"])

        session = session_manager.get(sid)
        tx_entry = session.handles[tx_handle]
        self.assertEqual(tx_entry.stream_channels, [1])
        self.assertEqual(tx_entry.cpu_format, "fc32")

        # set_tx_rate on channel 1 must be checked against channel 1's active streamer.
        rate_req = grpc_pb2.InvokeRequest(session_id=sid, handle=root, method="set_tx_rate")
        rate_req.args.add(name="rate", value=encode_value(20e6))
        rate_req.args.add(name="chan", value=encode_value(1))
        rate_resp = self._call("Invoke", rate_req, grpc_pb2.InvokeResponse)
        self.assertEqual(rate_resp.error.code, "RemoteRFPolicyError")
        self.assertEqual(self.native.tx_rates[1], 1e6)

        # SampleFrame with channels=[] fails channel mismatch; channels=[1] fails policy when out of band.
        self.native.tx_rates[1] = 20e6
        payload = np.zeros(4, dtype="<c8").tobytes()
        self._call(
            "SampleFrame",
            grpc_pb2.SampleFrame(
                session_id=sid,
                handle=tx_handle,
                generation=tx_gen,
                operation_id="host-op-empty",
                sequence=0,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                credits=1,
            ),
            grpc_pb2.SampleFrame,
        )
        mismatch = self._call(
            "SampleFrame",
            grpc_pb2.SampleFrame(
                session_id=sid,
                handle=tx_handle,
                generation=tx_gen,
                operation_id="host-op-empty",
                sequence=1,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_DATA,
                dtype="<c8",
                shape=[4],
                channels=[],
                sample_count=4,
                payload=payload,
                timeout_sec=0.1,
                metadata_json="{}",
            ),
            grpc_pb2.SampleFrame,
        )
        self.assertEqual(mismatch.kind, grpc_pb2.SAMPLE_FRAME_ERROR)
        self.assertEqual(mismatch.error.code, "RemoteRFProtocolError")
        self.assertEqual(self.native.tx_streamer.sent, [])

        # Helper-wrapped RX streamer on channel 1 refuses RX when channel 1 is in rx_deny.
        self.native.rx_freqs[0] = 915e6
        self.native.rx_freqs[1] = 921e6
        rx_stream_req = grpc_pb2.InvokeRequest(session_id=sid, handle=root, method="get_rx_stream")
        rx_stream_req.args.add(name="__args__", value=encode_value([wrapped]))
        rx_stream_resp = self._call("Invoke", rx_stream_req, grpc_pb2.InvokeResponse)
        self.assertEqual(rx_stream_resp.error.code, "")
        rx_info = decode_value(rx_stream_resp.result)
        rx_handle = rx_info["__remoterf_handle__"]
        rx_gen = int(rx_info["generation"])
        rx_entry = session.handles[rx_handle]
        self.assertEqual(rx_entry.stream_channels, [1])

        self._call(
            "SampleFrame",
            grpc_pb2.SampleFrame(
                session_id=sid,
                handle=rx_handle,
                generation=rx_gen,
                operation_id="host-rx-ch1",
                sequence=0,
                direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                credits=1,
            ),
            grpc_pb2.SampleFrame,
        )
        rx_err = self._call(
            "SampleFrame",
            grpc_pb2.SampleFrame(
                session_id=sid,
                handle=rx_handle,
                generation=rx_gen,
                operation_id="host-rx-ch1",
                sequence=1,
                direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                kind=grpc_pb2.SAMPLE_FRAME_REQUEST,
                dtype="<c8",
                shape=[4],
                channels=[1],
                sample_count=4,
                timeout_sec=0.1,
                metadata_json="{}",
            ),
            grpc_pb2.SampleFrame,
        )
        self.assertEqual(rx_err.kind, grpc_pb2.SAMPLE_FRAME_ERROR)
        self.assertEqual(rx_err.error.code, "RemoteRFPolicyError")

        # Empty stream_channels never falls back to client-chosen channels or [0].
        tx_entry.stream_channels = []
        self.native.tx_rates[1] = 1e6
        rate_req2 = grpc_pb2.InvokeRequest(session_id=sid, handle=root, method="set_tx_rate")
        rate_req2.args.add(name="rate", value=encode_value(1e6))
        rate_req2.args.add(name="chan", value=encode_value(0))
        self.assertEqual(self._call("Invoke", rate_req2, grpc_pb2.InvokeResponse).error.code, "RemoteRFPolicyError")
        self._call(
            "SampleFrame",
            grpc_pb2.SampleFrame(
                session_id=sid,
                handle=tx_handle,
                generation=tx_gen,
                operation_id="host-op-unknown",
                sequence=2,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                credits=1,
            ),
            grpc_pb2.SampleFrame,
        )
        tx_unknown = self._call(
            "SampleFrame",
            grpc_pb2.SampleFrame(
                session_id=sid,
                handle=tx_handle,
                generation=tx_gen,
                operation_id="host-op-unknown",
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
            grpc_pb2.SampleFrame,
        )
        self.assertEqual(tx_unknown.kind, grpc_pb2.SAMPLE_FRAME_ERROR)
        self.assertEqual(tx_unknown.error.code, "RemoteRFPolicyError")


if __name__ == "__main__":
    unittest.main()
