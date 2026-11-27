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

import json
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

import numpy as np

from remoterf_device_core.drivers.hackrf.hackrf_schema import HackRFSchema
from remoterf_device_core.drivers.usrp.usrp_schema import UsrpSchema
from remoterf_device_core.drivers.usrp_phaser.usrp_phaser_schema import UsrpPhaserSchema
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


class _FakeTimeSpec:
    def __init__(self, secs=0.0):
        self.secs = float(secs)

    def get_real_secs(self):
        return self.secs


class _FakeStreamMode:
    start_cont = "start_cont"
    stop_cont = "stop_cont"
    num_done = "num_done"
    num_more = "num_more"


class _FakeStreamCMD:
    def __init__(self, mode):
        self.mode = mode
        self.stream_now = True
        self.time_spec = None
        self.num_samps = 0


class _FakeDoubleProperty:
    def __init__(self, value=915e6):
        self.value = float(value)
        self.writes = []

    def get(self):
        return self.value

    def get_desired(self):
        return self.value

    def set(self, value):
        self.value = float(value)
        self.writes.append(("set", float(value)))

    def set_coerced(self, value):
        self.value = float(value)
        self.writes.append(("set_coerced", float(value)))


class _FakePropertyTree:
    def __init__(self):
        self.props = {}

    def access_double(self, path):
        return self.props.setdefault(path, _FakeDoubleProperty())


class _FakeRXStreamer:
    def __init__(self):
        self.commands = []

    def get_max_num_samps(self):
        return 256

    def issue_stream_cmd(self, cmd):
        self.commands.append(cmd)

    def recv(self, buffer, metadata, timeout):
        metadata.error_code = "none"
        return int(buffer.shape[-1])


class _FakeNativeUSRP:
    def __init__(self):
        self.tx_freqs = {0: 915e6, 1: 915e6}
        self.rx_freqs = {0: 915e6, 1: 915e6}
        self.tx_lo_freqs = {0: 915e6, 1: 915e6}
        self.rx_lo_freqs = {0: 915e6, 1: 915e6}
        self.tx_rates = {0: 4e6, 1: 4e6}
        self.rx_rates = {0: 4e6, 1: 4e6}
        self.tx_streamer = _FakeTXStreamer()
        self.rx_streamer = _FakeRXStreamer()
        self.tree = _FakePropertyTree()
        self.root_stream_cmds = []
        self.closed = False

    def close(self):
        self.closed = True

    def get_tree(self):
        return self.tree

    def issue_stream_cmd(self, rate, chan=(1 << 64) - 1):
        self.root_stream_cmds.append((rate, chan))

    def get_tx_lo_freq(self, name="all", chan=0):
        return self.tx_lo_freqs[int(chan)]

    def set_tx_lo_freq(self, freq, name, chan=0):
        self.tx_lo_freqs[int(chan)] = float(freq)
        self.tx_freqs[int(chan)] = float(freq)
        return float(freq)

    def get_rx_lo_freq(self, name="all", chan=0):
        return self.rx_lo_freqs[int(chan)]

    def set_rx_lo_freq(self, freq, name, chan=0):
        self.rx_lo_freqs[int(chan)] = float(freq)
        self.rx_freqs[int(chan)] = float(freq)
        return float(freq)

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

    def send_waveform(
        self,
        waveform_proto,
        duration,
        freq,
        rate=1e6,
        channels=(0,),
        gain=10,
        start_time=None,
        streamer=None,
    ):
        return len(waveform_proto)

    def recv_num_samps(
        self,
        num_samps,
        freq,
        rate=1e6,
        channels=(0,),
        gain=10,
        start_time=None,
        streamer=None,
    ):
        return np.zeros((max(len(channels), 1), num_samps), dtype=np.complex64)


def _fake_uhd():
    return types.SimpleNamespace(
        get_version_string=lambda: "4.10.0.0",
        get_abi_string=lambda: "4.10.0",
        types=types.SimpleNamespace(
            TuneRequest=_FakeTuneRequest,
            TXMetadata=_FakeTXMetadata,
            RXMetadata=_FakeRXMetadata,
            TimeSpec=_FakeTimeSpec,
            StreamMode=_FakeStreamMode,
            StreamCMD=_FakeStreamCMD,
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
        self.filter_bandwidth = 2_500_000
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
            "usrp_phaser": UsrpPhaserSchema,
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

            # usrp_phaser rejects frequency-limit configs at connect time (R1/F1),
            # while plain usrp_phaser configs without limit keys connect normally.
            for bad_phaser_init in (
                {"serial": "P1", "phaser_uri": "ip:localhost", "tx_allow": [[902e6, 928e6]]},
                {"serial": "P1", "rx_deny": [[10.6e9, 10.7e9]]},
                {"serial": "P1", "admin_group_override": True},
            ):
                bad_phaser, _, _ = device_manager._connect_from_record(
                    {"TYPE": "usrp_phaser", "NAME": "phaser-bad", "INIT": bad_phaser_init}
                )
                self.assertIsNone(bad_phaser)
            ok_phaser, _, dtype = device_manager._connect_from_record(
                {"TYPE": "usrp_phaser", "NAME": "phaser-ok", "INIT": {"serial": "P1", "phaser_uri": "ip:localhost"}}
            )
            self.assertEqual(dtype, "usrp_phaser")
            self.assertIsInstance(ok_phaser, UsrpPhaserSchema)
            self.assertIsNone(ok_phaser._freq_limits)

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

    def _invoke(self, sid, handle, method, args):
        req = grpc_pb2.InvokeRequest(session_id=sid, handle=handle, method=method)
        for name, val in args.items():
            req.args.add(name=name, value=encode_value(val))
        return self._call("Invoke", req, grpc_pb2.InvokeResponse)

    def _open(self):
        schema_hash = self.inventory.get_idl_v2()["schema_hash"]
        return self._call(
            "OpenSession",
            grpc_pb2.OpenSessionRequest(
                requested_schema_hash=schema_hash,
                control_protocol_version="2.0",
                streaming_protocol_version="1.0",
            ),
            grpc_pb2.OpenSessionResponse,
        )

    def test_host_v2_property_tree_writes_refused_while_limits_active(self):
        """ranges-1 (host): uhd.property.*.set and set_coerced refused while limits are active."""
        opened = self._open()
        self.assertEqual(opened.error.code, "")
        sid, root = opened.session_id, opened.device_handle

        tree_resp = self._invoke(sid, root, "get_tree", {})
        self.assertEqual(tree_resp.error.code, "")
        tree_handle = decode_value(tree_resp.result)["__remoterf_handle__"]

        path = "/mboards/0/dboards/A/tx_frontends/A/freq/value"
        prop_resp = self._invoke(sid, tree_handle, "access_double", {"__args__": [path]})
        self.assertEqual(prop_resp.error.code, "")
        prop_handle = decode_value(prop_resp.result)["__remoterf_handle__"]

        get_resp = self._invoke(sid, prop_handle, "get", {})
        self.assertEqual(get_resp.error.code, "")
        self.assertEqual(decode_value(get_resp.result), 915e6)

        for method in ("set", "set_coerced"):
            bad = self._invoke(sid, prop_handle, method, {"__args__": [2.45e9]})
            self.assertEqual(bad.error.code, "RemoteRFPolicyError")
            self.assertIn("every", bad.error.message.lower())
            self.assertIn("property-tree write", bad.error.message.lower())
            self.assertIn("MultiUSRP", bad.error.message)
        self.assertEqual(self.native.tree.props[path].writes, [])

        gain_path = "/mboards/0/dboards/A/tx_frontends/A/gains/PGA0/value"
        gain_prop = decode_value(
            self._invoke(sid, tree_handle, "access_double", {"__args__": [gain_path]}).result
        )["__remoterf_handle__"]
        bad_gain = self._invoke(sid, gain_prop, "set", {"__args__": [10.0]})
        self.assertEqual(bad_gain.error.code, "RemoteRFPolicyError")
        self.assertIn("every", bad_gain.error.message.lower())
        self.assertIn("MultiUSRP", bad_gain.error.message)
        ok_setter = self._invoke(sid, root, "set_tx_freq", {"tune_request": 916e6, "chan": 0})
        self.assertEqual(ok_setter.error.code, "")

        self.bypass = True
        ok = self._invoke(sid, prop_handle, "set", {"__args__": [2.45e9]})
        self.assertEqual(ok.error.code, "")
        self.assertEqual(self.native.tree.props[path].writes, [("set", 2.45e9)])

    def test_host_v2_rx_retune_preserves_prefetch_without_freq_limits(self):
        """Item 3 (host): labs without RX frequency limits skip _reset_rx_prefetch on retune."""
        for init in (
            {"serial": "HOST01"},
            {"serial": "HOST01", "tx_allow": [[902e6, 928e6]]},
        ):
            session_manager.close_all()
            inv = UsrpSchema()
            inv.bind_inventory(init)
            self.inventory = inv
            opened = self._open()
            self.assertEqual(opened.error.code, "")
            sid, root = opened.session_id, opened.device_handle

            rx_resp = self._invoke(
                sid,
                root,
                "get_rx_stream",
                {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
            )
            self.assertEqual(rx_resp.error.code, "")
            rx_handle = decode_value(rx_resp.result)["__remoterf_handle__"]
            rx_entry = session_manager.get(sid).handles[rx_handle]

            queued = np.full((1, 4), 42 + 0j, dtype=np.complex64)
            rx_entry.rx_prefetch.put((queued, {"error_code": "none"}))
            rx_entry.rx_pending = (queued, {"error_code": "none"}, 0)

            with patch.object(session_manager, "_reset_rx_prefetch", wraps=session_manager._reset_rx_prefetch) as reset_spy:
                ok_tune = self._invoke(sid, root, "set_rx_freq", {"tune_request": 916e6, "chan": 0})
                self.assertEqual(ok_tune.error.code, "")
                ok_lo = self._invoke(sid, root, "set_rx_lo_freq", {"freq": 917e6, "name": "all", "chan": 0})
                self.assertEqual(ok_lo.error.code, "")
                reset_spy.assert_not_called()
            self.assertFalse(rx_entry.rx_prefetch.empty())
            self.assertIsNotNone(rx_entry.rx_pending)

    def test_host_v2_rx_issue_stream_cmd_checked_and_prefetch_cleared_on_retune(self):
        """ranges-6 (host): RX issue_stream_cmd enforces rx_allow/rx_deny and retune flushes rx_prefetch."""
        opened = self._open()
        self.assertEqual(opened.error.code, "")
        sid, root = opened.session_id, opened.device_handle

        self.native.rx_freqs[0] = 2.45e9
        self.native.rx_lo_freqs[0] = 2.45e9
        rx_resp = self._invoke(
            sid,
            root,
            "get_rx_stream",
            {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
        )
        self.assertEqual(rx_resp.error.code, "")
        rx_handle = decode_value(rx_resp.result)["__remoterf_handle__"]

        start_cmd = {"__uhd_type__": "StreamCMD", "mode": "start_cont", "stream_now": True}
        bad_start = self._invoke(sid, rx_handle, "issue_stream_cmd", {"__args__": [start_cmd]})
        self.assertEqual(bad_start.error.code, "RemoteRFPolicyError")
        self.assertEqual(self.native.rx_streamer.commands, [])
        session = session_manager.get(sid)
        rx_entry = session.handles[rx_handle]
        self.assertIsNone(rx_entry.rx_worker)

        bad_root = self._invoke(sid, root, "issue_stream_cmd", {"rate": start_cmd, "chan": 0})
        self.assertEqual(bad_root.error.code, "RemoteRFPolicyError")
        self.assertEqual(self.native.root_stream_cmds, [])

        stop_cmd = {"__uhd_type__": "StreamCMD", "mode": "stop_cont", "stream_now": True}
        ok_stop = self._invoke(sid, rx_handle, "issue_stream_cmd", {"__args__": [stop_cmd]})
        self.assertEqual(ok_stop.error.code, "")

        stale = np.full((1, 4), 99 + 0j, dtype=np.complex64)
        rx_entry.rx_prefetch.put((stale, {"error_code": "none"}))
        rx_entry.rx_pending = (stale, {"error_code": "none"}, 0)
        ok_tune = self._invoke(sid, root, "set_rx_freq", {"tune_request": 915e6, "chan": 0})
        self.assertEqual(ok_tune.error.code, "")
        self.assertTrue(rx_entry.rx_prefetch.empty())
        self.assertIsNone(rx_entry.rx_pending)

        self.native.rx_lo_freqs[0] = 915e6
        self.native.rx_freqs[0] = 915e6
        rx_entry.rx_prefetch.put((stale, {"error_code": "none"}))
        rx_entry.rx_pending = (stale, {"error_code": "none"}, 0)
        ok_lo = self._invoke(sid, root, "set_rx_lo_freq", {"freq": 916e6, "name": "all", "chan": 0})
        self.assertEqual(ok_lo.error.code, "")
        self.assertTrue(rx_entry.rx_prefetch.empty())
        self.assertIsNone(rx_entry.rx_pending)

    def test_host_v2_live_schema_inherits_inventory_limits_or_fails_closed(self):
        """ranges-7 (host): live Dynamic v2 schema inherits inventory _freq_limits or fails closed."""

        class _StaleCustomUsrpSchema(UsrpSchema):
            def create_session_schema(self):
                clone = type(self)()
                clone.bind_inventory(self._inventory)
                clone.bind(clone.open_native_device())
                return clone

        stale_inv = _StaleCustomUsrpSchema()
        stale_inv.bind_inventory({"serial": "HOST01", "tx_allow": [[902e6, 928e6]]})
        self.inventory = stale_inv
        opened = self._open()
        self.assertEqual(opened.error.code, "")
        sid, root = opened.session_id, opened.device_handle
        session = session_manager.get(sid)
        self.assertEqual(session.schema._freq_limits, stale_inv._freq_limits)
        bad_tx = self._invoke(sid, root, "set_tx_freq", {"tune_request": 2.45e9, "chan": 0})
        self.assertEqual(bad_tx.error.code, "RemoteRFPolicyError")
        session_manager.close_all()

        class _BrokenCustomUsrpSchema(UsrpSchema):
            def configure_lockouts(self, cfg):
                self._freq_limits = None

            def create_session_schema(self):
                clone = type(self)()
                clone.bind_inventory(self._inventory)
                clone.bind(clone.open_native_device())
                return clone

        broken_inv = _BrokenCustomUsrpSchema()
        broken_inv.bind_inventory({"serial": "HOST01", "tx_allow": [[902e6, 928e6]]})
        self.inventory = broken_inv
        resp = self._open()
        self.assertEqual(resp.error.code, "RemoteRFPolicyError")

    def test_host_v2_empty_context_clears_bypass_and_stream_checks_hold_control_lock(self):
        """L5 (host): empty device context drops stale bypass; stream checks serialize with retunes."""
        opened = self._open()
        self.assertEqual(opened.error.code, "")
        sid, root = opened.session_id, opened.device_handle
        session = session_manager.get(sid)

        tx_resp = self._invoke(
            sid,
            root,
            "get_tx_stream",
            {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
        )
        tx_info = decode_value(tx_resp.result)
        tx_handle, tx_gen = tx_info["__remoterf_handle__"], int(tx_info["generation"])

        rx_resp = self._invoke(
            sid,
            root,
            "get_rx_stream",
            {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
        )
        rx_info = decode_value(rx_resp.result)
        rx_handle, rx_gen = rx_info["__remoterf_handle__"], int(rx_info["generation"])

        self.bypass = True
        self.assertEqual(
            self._invoke(sid, root, "set_tx_freq", {"tune_request": 2.455e9, "chan": 0}).error.code,
            "",
        )
        self.assertTrue(session.schema.lockout_bypass)
        self.native.rx_freqs[0] = 2.455e9
        self.native.rx_lo_freqs[0] = 2.455e9
        payload = np.zeros(4, dtype="<c8").tobytes()

        with patch("remoteRF_host.server.dynamic_v2.get_local_device_context", return_value={}):
            inv_err = self._invoke(sid, root, "set_tx_freq", {"tune_request": 2.455e9, "chan": 0})
            self.assertEqual(inv_err.error.code, "RemoteRFPolicyError")
            self.assertFalse(session.schema.lockout_bypass)

            session.schema.lockout_bypass = True
            self._call(
                "SampleFrame",
                grpc_pb2.SampleFrame(
                    session_id=sid, handle=tx_handle, generation=tx_gen,
                    operation_id="host-l5-tx", sequence=0,
                    direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                    kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                ),
                grpc_pb2.SampleFrame,
            )
            tx_err = self._call(
                "SampleFrame",
                grpc_pb2.SampleFrame(
                    session_id=sid, handle=tx_handle, generation=tx_gen,
                    operation_id="host-l5-tx", sequence=1,
                    direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                    kind=grpc_pb2.SAMPLE_FRAME_DATA,
                    dtype="<c8", shape=[4], channels=[0], sample_count=4,
                    payload=payload, timeout_sec=0.1, metadata_json="{}",
                ),
                grpc_pb2.SampleFrame,
            )
            self.assertEqual(tx_err.error.code, "RemoteRFPolicyError")
            self.assertFalse(session.schema.lockout_bypass)

            session.schema.lockout_bypass = True
            self._call(
                "SampleFrame",
                grpc_pb2.SampleFrame(
                    session_id=sid, handle=rx_handle, generation=rx_gen,
                    operation_id="host-l5-rx", sequence=0,
                    direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                    kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                ),
                grpc_pb2.SampleFrame,
            )
            rx_err = self._call(
                "SampleFrame",
                grpc_pb2.SampleFrame(
                    session_id=sid, handle=rx_handle, generation=rx_gen,
                    operation_id="host-l5-rx", sequence=1,
                    direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                    kind=grpc_pb2.SAMPLE_FRAME_REQUEST,
                    dtype="<c8", shape=[4], channels=[0], sample_count=4,
                    timeout_sec=0.1, metadata_json="{}",
                ),
                grpc_pb2.SampleFrame,
            )
            self.assertEqual(rx_err.error.code, "RemoteRFPolicyError")
            self.assertFalse(session.schema.lockout_bypass)

        self.bypass = False
        self.native.tx_freqs[0] = self.native.tx_lo_freqs[0] = 915e6
        self.native.rx_freqs[0] = self.native.rx_lo_freqs[0] = 915e6

        def probe_control_lock_held() -> bool:
            acquired = []

            def worker():
                got = session.control_lock.acquire(blocking=False)
                if got:
                    session.control_lock.release()
                acquired.append(got)

            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=2.0)
            return acquired == [False]

        observed = {}
        orig_send = self.native.tx_streamer.send
        orig_recv = self.native.rx_streamer.recv
        orig_cmd = self.native.rx_streamer.issue_stream_cmd

        def tracked_send(buf, meta, timeout):
            observed["tx_send"] = probe_control_lock_held()
            return orig_send(buf, meta, timeout)

        def tracked_recv(buf, meta, timeout):
            observed["rx_recv"] = probe_control_lock_held()
            return orig_recv(buf, meta, timeout)

        def tracked_cmd(cmd):
            observed["rx_cmd"] = probe_control_lock_held()
            return orig_cmd(cmd)

        with (
            patch.object(self.native.tx_streamer, "send", side_effect=tracked_send),
            patch.object(self.native.rx_streamer, "recv", side_effect=tracked_recv),
            patch.object(self.native.rx_streamer, "issue_stream_cmd", side_effect=tracked_cmd),
        ):
            self._call(
                "SampleFrame",
                grpc_pb2.SampleFrame(
                    session_id=sid, handle=tx_handle, generation=tx_gen,
                    operation_id="host-l5-lock-tx", sequence=2,
                    direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                    kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                ),
                grpc_pb2.SampleFrame,
            )
            tx_ok = self._call(
                "SampleFrame",
                grpc_pb2.SampleFrame(
                    session_id=sid, handle=tx_handle, generation=tx_gen,
                    operation_id="host-l5-lock-tx", sequence=3,
                    direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                    kind=grpc_pb2.SAMPLE_FRAME_DATA,
                    dtype="<c8", shape=[4], channels=[0], sample_count=4,
                    payload=payload, timeout_sec=0.1, metadata_json="{}",
                ),
                grpc_pb2.SampleFrame,
            )
            self.assertEqual(tx_ok.kind, grpc_pb2.SAMPLE_FRAME_RESULT)

            self._call(
                "SampleFrame",
                grpc_pb2.SampleFrame(
                    session_id=sid, handle=rx_handle, generation=rx_gen,
                    operation_id="host-l5-lock-rx", sequence=2,
                    direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                    kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                ),
                grpc_pb2.SampleFrame,
            )
            rx_ok = self._call(
                "SampleFrame",
                grpc_pb2.SampleFrame(
                    session_id=sid, handle=rx_handle, generation=rx_gen,
                    operation_id="host-l5-lock-rx", sequence=3,
                    direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                    kind=grpc_pb2.SAMPLE_FRAME_REQUEST,
                    dtype="<c8", shape=[4], channels=[0], sample_count=4,
                    timeout_sec=0.1, metadata_json="{}",
                ),
                grpc_pb2.SampleFrame,
            )
            self.assertEqual(rx_ok.kind, grpc_pb2.SAMPLE_FRAME_DATA)

            cmd_resp = self._invoke(
                sid,
                rx_handle,
                "issue_stream_cmd",
                {"__args__": [{"__uhd_type__": "StreamCMD", "mode": "num_done", "stream_now": True}]},
            )
            self.assertEqual(cmd_resp.error.code, "")

        self.assertEqual(observed, {"tx_send": True, "rx_recv": True, "rx_cmd": True})

    def test_host_v2_waveform_prefetch_reset_timed_tx_and_prompt_close(self):
        """R2, R3 & R5 (host): recv_num_samps prefetch reset, timed TX refusal, stop_cont & CloseSession under long RX wait."""
        opened = self._open()
        self.assertEqual(opened.error.code, "")
        sid, root = opened.session_id, opened.device_handle
        session = session_manager.get(sid)

        tx_info = decode_value(
            self._invoke(
                sid,
                root,
                "get_tx_stream",
                {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
            ).result
        )
        tx_handle, tx_gen = tx_info["__remoterf_handle__"], int(tx_info["generation"])
        rx_info = decode_value(
            self._invoke(
                sid,
                root,
                "get_rx_stream",
                {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
            ).result
        )
        rx_handle, rx_gen = rx_info["__remoterf_handle__"], int(rx_info["generation"])
        rx_entry = session.handles[rx_handle]

        # R2: recv_num_samps flushes RX prefetch when RX limits are active.
        stale = np.full((1, 4), 55 + 0j, dtype=np.complex64)
        rx_entry.rx_prefetch.put((stale, {"error_code": "none"}))
        rx_entry.rx_pending = (stale, {"error_code": "none"}, 0)
        ok_rx = self._invoke(sid, root, "recv_num_samps", {"num_samps": 8, "freq": 915e6, "channels": [0]})
        self.assertEqual(ok_rx.error.code, "")
        self.assertTrue(rx_entry.rx_prefetch.empty())
        self.assertIsNone(rx_entry.rx_pending)

        # R3: timed TXMetadata refused on host SampleFrame TX; default untimed TimeSpec(0.0) allowed.
        payload = np.zeros(4, dtype="<c8").tobytes()
        self._call(
            "SampleFrame",
            grpc_pb2.SampleFrame(
                session_id=sid, handle=tx_handle, generation=tx_gen,
                operation_id="host-timed-tx", sequence=0,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
            ),
            grpc_pb2.SampleFrame,
        )
        timed_err = self._call(
            "SampleFrame",
            grpc_pb2.SampleFrame(
                session_id=sid, handle=tx_handle, generation=tx_gen,
                operation_id="host-timed-tx", sequence=1,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_DATA,
                dtype="<c8", shape=[4], channels=[0], sample_count=4,
                payload=payload, timeout_sec=0.1,
                metadata_json=json.dumps({"has_time_spec": True, "time_spec": {"__uhd_type__": "TimeSpec", "secs": 5.0}}),
            ),
            grpc_pb2.SampleFrame,
        )
        self.assertEqual(timed_err.kind, grpc_pb2.SAMPLE_FRAME_ERROR)
        self.assertEqual(timed_err.error.code, "RemoteRFPolicyError")
        self.assertEqual(self.native.tx_streamer.sent, [])

        # R5: CloseSession during a 30s prefetched RX wait (rx_worker alive) completes promptly on host.
        def stalled_recv(buf, meta, timeout):
            time.sleep(min(float(timeout), 30.0))
            meta.error_code = "timeout"
            return 0

        with patch.object(self.native.rx_streamer, "recv", side_effect=stalled_recv):
            start_resp = self._invoke(
                sid,
                rx_handle,
                "issue_stream_cmd",
                {"__args__": [{"__uhd_type__": "StreamCMD", "mode": "start_cont", "stream_now": True}]},
            )
            self.assertEqual(start_resp.error.code, "")
            self.assertIsNotNone(rx_entry.rx_worker)
            self.assertTrue(rx_entry.rx_worker.is_alive())

            self._call(
                "SampleFrame",
                grpc_pb2.SampleFrame(
                    session_id=sid, handle=rx_handle, generation=rx_gen,
                    operation_id="host-r5-prefetch", sequence=0,
                    direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                    kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                ),
                grpc_pb2.SampleFrame,
            )

            def run_rx():
                self._call(
                    "SampleFrame",
                    grpc_pb2.SampleFrame(
                        session_id=sid, handle=rx_handle, generation=rx_gen,
                        operation_id="host-r5-prefetch", sequence=1,
                        direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                        kind=grpc_pb2.SAMPLE_FRAME_REQUEST,
                        dtype="<c8", shape=[4], channels=[0], sample_count=4,
                        timeout_sec=30.0, metadata_json="{}",
                    ),
                    grpc_pb2.SampleFrame,
                )

            t = threading.Thread(target=run_rx)
            t.start()
            wait_deadline = time.monotonic() + 2.0
            while session.activity.idle() and time.monotonic() < wait_deadline:
                time.sleep(0.005)
            self.assertFalse(session.activity.idle())

            t0 = time.monotonic()
            close_resp = self._call(
                "CloseSession",
                grpc_pb2.CloseSessionRequest(session_id=sid),
                grpc_pb2.CloseResponse,
            )
            elapsed = time.monotonic() - t0
            t.join(timeout=2.0)
            self.assertEqual(close_resp.error.code, "")
            self.assertTrue(close_resp.closed)
            self.assertLess(elapsed, 1.5)
            self.assertFalse(t.is_alive())
            deadline = time.monotonic() + 2.0
            while session_manager.cleanup_pending(7) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(session_manager.cleanup_pending(7))
            self.assertTrue(self.native.closed)

    def test_host_v2_stream_mode_only_stop_cont_partial_rx_metadata_and_closed_tx_rate(self):
        """R5, N2 & Hunt N1 (host): .stream_mode-only StreamCMD, root stop_cont wakes prefetch, partial RX metadata, closed TX set_tx_rate & MCR."""

        class _StreamModeOnlyCMD:
            def __init__(self, mode):
                self.stream_mode = mode
                self.stream_now = True
                self.time_spec = None
                self.num_samps = 0

        class _Fake_rx_metadata(_FakeRXMetadata):
            pass

        class _Fake_time_spec(_FakeTimeSpec):
            pass

        mcr_calls = []
        self.native.set_master_clock_rate = lambda rate, mboard=0: mcr_calls.append((rate, mboard))
        self.fake_uhd.types.StreamCMD = _StreamModeOnlyCMD
        self.fake_uhd.types.RXMetadata = _Fake_rx_metadata
        self.fake_uhd.types.TimeSpec = _Fake_time_spec

        opened = self._open()
        self.assertEqual(opened.error.code, "")
        sid, root = opened.session_id, opened.device_handle
        session = session_manager.get(sid)

        # Hunt N1 (host): set_tx_rate after closing TX handle and set_master_clock_rate refused.
        self.assertEqual(self._invoke(sid, root, "set_tx_freq", {"tune_request": 925e6, "chan": 0}).error.code, "")
        tx_info = decode_value(
            self._invoke(
                sid,
                root,
                "get_tx_stream",
                {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
            ).result
        )
        self.assertTrue(session_manager.close_handle(sid, tx_info["__remoterf_handle__"]))
        self.assertEqual(
            self._invoke(sid, root, "set_tx_rate", {"rate": 40e6, "chan": 0}).error.code,
            "RemoteRFPolicyError",
        )
        self.assertEqual(self.native.tx_rate, 4e6)
        self.assertEqual(
            self._invoke(sid, root, "set_master_clock_rate", {"rate": 192e6}).error.code,
            "RemoteRFPolicyError",
        )
        self.assertEqual(mcr_calls, [])

        rx_info = decode_value(
            self._invoke(
                sid,
                root,
                "get_rx_stream",
                {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
            ).result
        )
        rx_handle, rx_gen = rx_info["__remoterf_handle__"], int(rx_info["generation"])
        rx_entry = session.handles[rx_handle]

        # N2 (host): sliced direct RX partial read preserves first packet metadata.
        calls = {"n": 0}

        def partial_then_timeout(buf, meta, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                buf[:2] = 5 + 0j
                meta.error_code = "none"
                meta.has_time_spec = True
                meta.time_spec = _Fake_time_spec(19.25)
                return 2
            time.sleep(min(float(timeout), 0.1))
            meta.error_code = "timeout"
            meta.has_time_spec = False
            meta.time_spec = _Fake_time_spec(0.0)
            return 0

        with patch.object(self.native.rx_streamer, "recv", side_effect=partial_then_timeout):
            self._call(
                "SampleFrame",
                grpc_pb2.SampleFrame(
                    session_id=sid, handle=rx_handle, generation=rx_gen,
                    operation_id="host-n2-partial", sequence=0,
                    direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                    kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                ),
                grpc_pb2.SampleFrame,
            )
            data_frame = self._call(
                "SampleFrame",
                grpc_pb2.SampleFrame(
                    session_id=sid, handle=rx_handle, generation=rx_gen,
                    operation_id="host-n2-partial", sequence=1,
                    direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                    kind=grpc_pb2.SAMPLE_FRAME_REQUEST,
                    dtype="<c8", shape=[4], channels=[0], sample_count=4,
                    timeout_sec=0.25, metadata_json="{}",
                ),
                grpc_pb2.SampleFrame,
            )
        self.assertEqual(data_frame.kind, grpc_pb2.SAMPLE_FRAME_DATA)
        self.assertEqual(data_frame.sample_count, 2)
        self.assertEqual(data_frame.device_time_secs, 19.25)
        meta = json.loads(data_frame.metadata_json)
        self.assertEqual(meta["error_code"], "none")
        self.assertEqual(meta["time_spec"]["secs"], 19.25)

        # R5 (host): .stream_mode-only StreamCMD starts prefetch and root stop_cont wakes _recv_prefetched.
        def stalled_recv(buf, meta, timeout):
            time.sleep(min(float(timeout), 30.0))
            meta.error_code = "timeout"
            return 0

        with patch.object(self.native.rx_streamer, "recv", side_effect=stalled_recv):
            start_resp = self._invoke(
                sid,
                rx_handle,
                "issue_stream_cmd",
                {"__args__": [{"__uhd_type__": "StreamCMD", "mode": "start_cont", "stream_now": True}]},
            )
            self.assertEqual(start_resp.error.code, "")
            self.assertIsNotNone(rx_entry.rx_worker)
            self.assertTrue(rx_entry.rx_worker.is_alive())
            self.assertFalse(hasattr(self.native.rx_streamer.commands[-1], "mode"))

            self._call(
                "SampleFrame",
                grpc_pb2.SampleFrame(
                    session_id=sid, handle=rx_handle, generation=rx_gen,
                    operation_id="host-r5-root-stop", sequence=2,
                    direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                    kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                ),
                grpc_pb2.SampleFrame,
            )

            def run_rx():
                self._call(
                    "SampleFrame",
                    grpc_pb2.SampleFrame(
                        session_id=sid, handle=rx_handle, generation=rx_gen,
                        operation_id="host-r5-root-stop", sequence=3,
                        direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                        kind=grpc_pb2.SAMPLE_FRAME_REQUEST,
                        dtype="<c8", shape=[4], channels=[0], sample_count=4,
                        timeout_sec=30.0, metadata_json="{}",
                    ),
                    grpc_pb2.SampleFrame,
                )

            t = threading.Thread(target=run_rx)
            t.start()
            wait_deadline = time.monotonic() + 2.0
            while session.activity.idle() and time.monotonic() < wait_deadline:
                time.sleep(0.005)
            self.assertFalse(session.activity.idle())

            self.native.rx_freqs[0] = 2.45e9
            self.native.rx_lo_freqs[0] = 2.45e9
            t0 = time.monotonic()
            stop_resp = self._invoke(
                sid,
                root,
                "issue_stream_cmd",
                {"rate": {"__uhd_type__": "StreamCMD", "mode": "stop_cont", "stream_now": True}, "chan": 0},
            )
            elapsed = time.monotonic() - t0
            t.join(timeout=2.0)
            self.assertEqual(stop_resp.error.code, "")
            self.assertLess(elapsed, 1.0)
            self.assertFalse(t.is_alive())
            self.assertFalse(hasattr(self.native.root_stream_cmds[-1][0], "mode"))

    def test_host_v2_fresh_session_allows_rate_and_mcr_before_tx_then_enforces_after_close(self):
        """Fresh host v2 session with OOB default LO and wide rate can configure TX, then enforces rate/MCR once TX starts."""
        mcr_calls = []
        self.native.set_master_clock_rate = lambda rate, mboard=0: mcr_calls.append((rate, mboard))
        self.native.tx_freqs = {0: 0.0, 1: 0.0}
        self.native.tx_lo_freqs = {0: 0.0, 1: 0.0}
        self.native.tx_rates = {0: 200e6, 1: 200e6}

        opened = self._open()
        self.assertEqual(opened.error.code, "")
        sid, root = opened.session_id, opened.device_handle

        # Tuning first fails at 200 MS/s; premature get_tx_stream fails without locking out rate/MCR.
        self.assertEqual(
            self._invoke(sid, root, "set_tx_freq", {"tune_request": 915e6, "chan": 0}).error.code,
            "RemoteRFPolicyError",
        )
        self.assertEqual(
            self._invoke(
                sid,
                root,
                "get_tx_stream",
                {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
            ).error.code,
            "RemoteRFPolicyError",
        )

        # Fresh session before TX: set_master_clock_rate and set_tx_rate succeed, then set_tx_freq and streaming work.
        self.assertEqual(self._invoke(sid, root, "set_master_clock_rate", {"rate": 32e6}).error.code, "")
        self.assertEqual(mcr_calls, [(32e6, 0)])
        self.assertEqual(self._invoke(sid, root, "set_tx_rate", {"rate": 1e6, "chan": (1 << 64) - 1}).error.code, "")
        self.assertEqual(self.native.tx_rates, {0: 1e6, 1: 1e6})
        self.assertEqual(self._invoke(sid, root, "set_tx_freq", {"tune_request": 915e6, "chan": 0}).error.code, "")
        self.native.tx_freqs[1] = 915e6
        self.native.tx_lo_freqs = {0: 915e6, 1: 915e6}

        tx_resp = self._invoke(
            sid,
            root,
            "get_tx_stream",
            {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
        )
        self.assertEqual(tx_resp.error.code, "")
        tx_info = decode_value(tx_resp.result)
        tx_handle, tx_gen = tx_info["__remoterf_handle__"], int(tx_info["generation"])
        self._call(
            "SampleFrame",
            grpc_pb2.SampleFrame(
                session_id=sid, handle=tx_handle, generation=tx_gen,
                operation_id="host-fresh-tx", sequence=0,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
            ),
            grpc_pb2.SampleFrame,
        )
        sent = self._call(
            "SampleFrame",
            grpc_pb2.SampleFrame(
                session_id=sid, handle=tx_handle, generation=tx_gen,
                operation_id="host-fresh-tx", sequence=1,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_DATA,
                dtype="<c8", shape=[4], channels=[0], sample_count=4,
                payload=np.zeros(4, dtype="<c8").tobytes(),
                timeout_sec=0.1, metadata_json="{}",
            ),
            grpc_pb2.SampleFrame,
        )
        self.assertEqual(sent.kind, grpc_pb2.SAMPLE_FRAME_RESULT)
        self.assertEqual(self.native.tx_streamer.sent, [4])

        # After closing TX handle, out-of-band set_tx_rate and set_master_clock_rate are refused.
        self.assertTrue(session_manager.close_handle(sid, tx_handle))
        self.assertEqual(
            self._invoke(sid, root, "set_tx_rate", {"rate": 40e6, "chan": 0}).error.code,
            "RemoteRFPolicyError",
        )
        self.assertEqual(
            self._invoke(sid, root, "set_master_clock_rate", {"rate": 192e6}).error.code,
            "RemoteRFPolicyError",
        )
        self.assertEqual(len(mcr_calls), 1)

        # Closing session and opening a fresh session restores pre-TX rate/MCR setup.
        close_resp = self._call(
            "CloseSession",
            grpc_pb2.CloseSessionRequest(session_id=sid),
            grpc_pb2.CloseResponse,
        )
        self.assertEqual(close_resp.error.code, "")
        deadline = time.monotonic() + 2.0
        while session_manager.cleanup_pending(7) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.native.closed = False
        self.native.tx_freqs = {0: 0.0, 1: 0.0}
        self.native.tx_lo_freqs = {0: 0.0, 1: 0.0}
        self.native.tx_rates = {0: 200e6, 1: 200e6}
        opened2 = self._open()
        self.assertEqual(opened2.error.code, "")
        sid2, root2 = opened2.session_id, opened2.device_handle
        self.assertEqual(self._invoke(sid2, root2, "set_master_clock_rate", {"rate": 32e6}).error.code, "")
        self.assertEqual(self._invoke(sid2, root2, "set_tx_rate", {"rate": 1e6, "chan": 0}).error.code, "")
        self.assertEqual(len(mcr_calls), 2)


if __name__ == "__main__":
    unittest.main()
