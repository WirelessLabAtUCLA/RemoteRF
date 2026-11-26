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
        return {"mboard_id": "B210", "mboard_name": "B210", "mboard_serial": "SRV01"}

    def get_usrp_tx_info(self, chan=0):
        return {"mboard_id": "B210", "mboard_name": "B210", "mboard_serial": "SRV01"}

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
        self.center_freq = 915_000_000
        self.sample_rate = 4_000_000
        self.filter_bandwidth = 2_500_000
        self.buffer = bytearray(b"\x01\x02")


class ServerDeviceManagerFreqRangeTests(unittest.TestCase):
    def test_accepts_freq_ranges_on_hackrf_and_usrp_and_rejects_invalid_or_unsupported(self):
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

            # usrp_phaser refuses frequency limit configs at connect time (R1/F1),
            # while plain usrp_phaser configs without limit keys still connect.
            for bad_phaser_init in (
                {"serial": "P0", "phaser_uri": "ip:localhost", "tx_allow": [[902e6, 928e6]]},
                {"serial": "P0", "rx_deny": [[10.6e9, 10.7e9]]},
                {"serial": "P0", "admin_group_override": True},
            ):
                bad_phaser, _, _ = device_manager._connect_from_record(
                    {"TYPE": "usrp_phaser", "NAME": "phaser-bad", "INIT": bad_phaser_init},
                    connect=True,
                )
                self.assertIsNone(bad_phaser)
            ok_phaser, _, dtype = device_manager._connect_from_record(
                {"TYPE": "usrp_phaser", "NAME": "phaser-ok", "INIT": {"serial": "P0", "phaser_uri": "ip:localhost"}},
                connect=True,
            )
            self.assertEqual(dtype, "usrp_phaser")
            self.assertIsInstance(ok_phaser, UsrpPhaserSchema)
            self.assertIsNone(ok_phaser._freq_limits)

            # Misspelled or unknown limit keys fail closed on HackRF and USRP -> offline (None)
            for drv in ("hackrf", "usrp"):
                for bad_init in (
                    {"serial": "X", "tx_alow": [[902e6, 928e6]]},
                    {"serial": "X", "tx_deny_ranges": [[2.45e9, 2.46e9]]},
                ):
                    bad_typo, _, _ = device_manager._connect_from_record(
                        {"TYPE": drv, "NAME": f"{drv}-typo", "INIT": bad_init},
                        connect=True,
                    )
                    self.assertIsNone(bad_typo)

    def test_stale_pluto_driver_without_freq_limits_support_rejected_for_new_keys(self):
        sys.modules.setdefault("adi", types.ModuleType("adi"))
        made = []

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

        # Ensure __qualname__ mirrors an old managed pluto_schema.py copy (no _freq_limits_for).
        PlutoSchema.dispatch.__qualname__ = "PlutoSchema.dispatch"

        with (
            patch.object(device_manager, "get_driver_class", return_value=PlutoSchema),
            self.assertLogs(device_manager.logger, level="WARNING") as logs,
        ):
            for init in (
                {"tx_allow": [[2.40e9, 2.48e9]]},
                {"tx_min_freq": 902e6, "tx_max_freq": 928e6, "tx_deny": [[914e6, 916e6]]},
                {"rx_deny": [[1.57e9, 1.58e9]]},
            ):
                schema, _, _ = device_manager._connect_from_record(
                    {"TYPE": "adalm_pluto", "NAME": "stale-pluto", "INIT": init},
                    connect=True,
                )
                self.assertIsNone(schema)
        self.assertEqual(made, [])
        self.assertTrue(any("allow/deny" in line and "device left offline" in line for line in logs.output))

        # Legacy-only config still loads on the older copy; updated driver with _freq_limits_for loads new keys.
        with patch.object(device_manager, "get_driver_class", return_value=PlutoSchema):
            legacy, _, _ = device_manager._connect_from_record(
                {"TYPE": "adalm_pluto", "NAME": "stale-pluto", "INIT": {"tx_min_freq": 902e6, "tx_max_freq": 928e6}},
                connect=True,
            )
            self.assertIsNotNone(legacy)

        PlutoSchema._freq_limits_for = lambda self, side: None
        with patch.object(device_manager, "get_driver_class", return_value=PlutoSchema):
            modern, _, _ = device_manager._connect_from_record(
                {"TYPE": "adalm_pluto", "NAME": "modern-pluto", "INIT": {"tx_allow": [[2.40e9, 2.48e9]]}},
                connect=True,
            )
            self.assertIsNotNone(modern)


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

    def test_v2_helper_wrapped_stream_args_records_channels_and_never_falls_back(self):
        opened = self._open()
        sid, root = opened.session_id, opened.device_handle

        # Channel 0 stays legally parked at 915 MHz / 1 MS/s; channel 1 is at 927 MHz / 1 MS/s (legal: 926.5..927.5).
        self.native.tx_rates[0] = 1e6
        self.native.tx_freqs[0] = 915e6
        self.native.tx_rates[1] = 1e6
        self.native.tx_freqs[1] = 927e6

        wrapped_tx_args = {
            "__remoterf_uhd_helper__": "stream",
            "stream_args": {
                "__uhd_type__": "StreamArgs",
                "cpu_format": "fc32",
                "otw_format": "sc16",
                "channels": [1],
            },
        }
        tx_resp = self._invoke(sid, root, "get_tx_stream", {"__args__": [wrapped_tx_args]})
        self.assertEqual(tx_resp.error.code, "")
        tx_info = decode_value(tx_resp.result)
        tx_handle = tx_info["__remoterf_handle__"]
        tx_gen = int(tx_info["generation"])

        session = session_manager.get(sid)
        tx_entry = session.handles[tx_handle]
        self.assertEqual(tx_entry.stream_channels, [1])
        self.assertEqual(tx_entry.cpu_format, "fc32")

        # Widening chan 1 TX rate to 20 MS/s (917..937 MHz) must be refused (not skipped by [0] fallback),
        # including when chan=0 is requested (shared TX rate / active channel re-check, ranges-4).
        bad_rate = self._invoke(sid, root, "set_tx_rate", {"rate": 20e6, "chan": 1})
        self.assertEqual(bad_rate.error.code, "RemoteRFPolicyError")
        self.assertEqual(self.native.tx_rates[1], 1e6)
        bad_rate_other = self._invoke(sid, root, "set_tx_rate", {"rate": 20e6, "chan": 0})
        self.assertEqual(bad_rate_other.error.code, "RemoteRFPolicyError")
        self.assertEqual(self.native.tx_rates[0], 1e6)

        # Even if hardware rate widens out of band, TX SampleStream refuses empty channels (mismatch)
        # and refuses [1] with RemoteRFPolicyError.
        self.native.tx_rates[1] = 20e6
        payload = np.zeros(4, dtype="<c8").tobytes()
        mismatch_frames = list(
            self.samples.SampleStream(
                iter(
                    [
                        grpc_pb2.SampleFrame(
                            session_id=sid,
                            handle=tx_handle,
                            generation=tx_gen,
                            operation_id="op-empty-ch",
                            sequence=0,
                            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                            kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                            credits=1,
                        ),
                        grpc_pb2.SampleFrame(
                            session_id=sid,
                            handle=tx_handle,
                            generation=tx_gen,
                            operation_id="op-empty-ch",
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
                    ]
                ),
                None,
            )
        )
        self.assertEqual(mismatch_frames[-1].kind, grpc_pb2.SAMPLE_FRAME_ERROR)
        self.assertEqual(mismatch_frames[-1].error.code, "RemoteRFProtocolError")
        self.assertEqual(self.native.tx_streamer.sent, [])

        denied_ch1 = list(
            self.samples.SampleStream(
                iter(
                    [
                        grpc_pb2.SampleFrame(
                            session_id=sid,
                            handle=tx_handle,
                            generation=tx_gen,
                            operation_id="op-ch1",
                            sequence=2,
                            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                            kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                            credits=1,
                        ),
                        grpc_pb2.SampleFrame(
                            session_id=sid,
                            handle=tx_handle,
                            generation=tx_gen,
                            operation_id="op-ch1",
                            sequence=3,
                            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                            kind=grpc_pb2.SAMPLE_FRAME_DATA,
                            dtype="<c8",
                            shape=[4],
                            channels=[1],
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
        self.assertEqual(denied_ch1[-1].kind, grpc_pb2.SAMPLE_FRAME_ERROR)
        self.assertEqual(denied_ch1[-1].error.code, "RemoteRFPolicyError")
        self.assertEqual(self.native.tx_streamer.sent, [])

        # Helper-wrapped RX streamer on channel 1 checks channel 1 (denied at 921 MHz), not channel 0 (915 MHz).
        self.native.rx_freqs[0] = 915e6
        self.native.rx_freqs[1] = 921e6
        rx_resp = self._invoke(sid, root, "get_rx_stream", {"__args__": [wrapped_tx_args]})
        self.assertEqual(rx_resp.error.code, "")
        rx_info = decode_value(rx_resp.result)
        rx_handle = rx_info["__remoterf_handle__"]
        rx_gen = int(rx_info["generation"])
        rx_entry = session.handles[rx_handle]
        self.assertEqual(rx_entry.stream_channels, [1])

        rx_denied = list(
            self.samples.SampleStream(
                iter(
                    [
                        grpc_pb2.SampleFrame(
                            session_id=sid,
                            handle=rx_handle,
                            generation=rx_gen,
                            operation_id="op-rx-ch1",
                            sequence=0,
                            direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                            kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                            credits=1,
                        ),
                        grpc_pb2.SampleFrame(
                            session_id=sid,
                            handle=rx_handle,
                            generation=rx_gen,
                            operation_id="op-rx-ch1",
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
                    ]
                ),
                None,
            )
        )
        self.assertEqual(rx_denied[-1].kind, grpc_pb2.SAMPLE_FRAME_ERROR)
        self.assertEqual(rx_denied[-1].error.code, "RemoteRFPolicyError")

        # Defensive fail-closed: empty handle stream_channels never falls back to request.channels or [0].
        tx_entry.stream_channels = []
        rx_entry.stream_channels = []
        self.native.tx_rates[1] = 1e6
        self.assertEqual(
            self._invoke(sid, root, "set_tx_rate", {"rate": 1e6, "chan": 0}).error.code,
            "RemoteRFPolicyError",
        )
        tx_unknown = list(
            self.samples.SampleStream(
                iter(
                    [
                        grpc_pb2.SampleFrame(
                            session_id=sid,
                            handle=tx_handle,
                            generation=tx_gen,
                            operation_id="op-unknown-tx",
                            sequence=4,
                            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                            kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                            credits=1,
                        ),
                        grpc_pb2.SampleFrame(
                            session_id=sid,
                            handle=tx_handle,
                            generation=tx_gen,
                            operation_id="op-unknown-tx",
                            sequence=5,
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
        self.assertEqual(tx_unknown[-1].error.code, "RemoteRFPolicyError")

        rx_unknown = list(
            self.samples.SampleStream(
                iter(
                    [
                        grpc_pb2.SampleFrame(
                            session_id=sid,
                            handle=rx_handle,
                            generation=rx_gen,
                            operation_id="op-unknown-rx",
                            sequence=2,
                            direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                            kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                            credits=1,
                        ),
                        grpc_pb2.SampleFrame(
                            session_id=sid,
                            handle=rx_handle,
                            generation=rx_gen,
                            operation_id="op-unknown-rx",
                            sequence=3,
                            direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                            kind=grpc_pb2.SAMPLE_FRAME_REQUEST,
                            dtype="<c8",
                            shape=[4],
                            channels=[0],
                            sample_count=4,
                            timeout_sec=0.1,
                            metadata_json="{}",
                        ),
                    ]
                ),
                None,
            )
        )
        self.assertEqual(rx_unknown[-1].error.code, "RemoteRFPolicyError")

    def test_v2_property_tree_writes_refused_while_limits_active(self):
        """ranges-1: uhd.property.*.set and set_coerced refused while limits are active."""
        opened = self._open()
        sid, root = opened.session_id, opened.device_handle

        tree_resp = self._invoke(sid, root, "get_tree", {})
        self.assertEqual(tree_resp.error.code, "")
        tree_handle = decode_value(tree_resp.result)["__remoterf_handle__"]

        path = "/mboards/0/dboards/A/tx_frontends/A/freq/value"
        prop_resp = self._invoke(sid, tree_handle, "access_double", {"__args__": [path]})
        self.assertEqual(prop_resp.error.code, "")
        prop_handle = decode_value(prop_resp.result)["__remoterf_handle__"]

        # Read-only get still succeeds.
        get_resp = self._invoke(sid, prop_handle, "get", {})
        self.assertEqual(get_resp.error.code, "")
        self.assertEqual(decode_value(get_resp.result), 915e6)

        # set and set_coerced refused with typed RemoteRFPolicyError; property untouched.
        for method in ("set", "set_coerced"):
            bad = self._invoke(sid, prop_handle, method, {"__args__": [2.45e9]})
            self.assertEqual(bad.error.code, "RemoteRFPolicyError")
            self.assertIn("every", bad.error.message.lower())
            self.assertIn("property-tree write", bad.error.message.lower())
            self.assertIn("MultiUSRP", bad.error.message)
        self.assertEqual(self.native.tree.props[path].writes, [])

        # Non-frequency property paths are also refused with the same message, while normal MultiUSRP setters work.
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

        # Admin bypass allows property write when admin_group_override is enabled.
        self.bypass = True
        ok = self._invoke(sid, prop_handle, "set", {"__args__": [2.45e9]})
        self.assertEqual(ok.error.code, "")
        self.assertEqual(self.native.tree.props[path].writes, [("set", 2.45e9)])

    def test_v2_rx_retune_preserves_prefetch_without_freq_limits(self):
        """Item 3: labs without RX frequency limits skip _reset_rx_prefetch on retune."""
        for init in (
            {"serial": "SRV01"},
            {"serial": "SRV01", "tx_allow": [[902e6, 928e6]]},
        ):
            session_manager.close_all()
            inv = UsrpSchema()
            inv.bind_inventory(init)
            self.inventory = inv
            opened = self._open()
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

    def test_v2_rx_issue_stream_cmd_checked_and_prefetch_cleared_on_retune(self):
        """ranges-6: RX issue_stream_cmd enforces rx_allow/rx_deny and retune flushes rx_prefetch."""
        opened = self._open()
        sid, root = opened.session_id, opened.device_handle

        # RX LO starts outside rx_allow [902, 928].
        self.native.rx_freqs[0] = 2.45e9
        self.native.rx_lo_freqs[0] = 2.45e9
        rx_resp = self._invoke(
            sid,
            root,
            "get_rx_stream",
            {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
        )
        self.assertEqual(rx_resp.error.code, "")
        rx_info = decode_value(rx_resp.result)
        rx_handle = rx_info["__remoterf_handle__"]

        # Starting RX stream while out of band is refused on both RXStreamer and MultiUSRP.
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

        # stop_cont remains allowed even while RX LO is out of band.
        stop_cmd = {"__uhd_type__": "StreamCMD", "mode": "stop_cont", "stream_now": True}
        ok_stop = self._invoke(sid, rx_handle, "issue_stream_cmd", {"__args__": [stop_cmd]})
        self.assertEqual(ok_stop.error.code, "")

        # Stale pre-retune samples in rx_prefetch / rx_pending are flushed by set_rx_freq and set_rx_lo_freq.
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

    def test_v2_live_schema_inherits_inventory_limits_or_fails_closed(self):
        """ranges-7: Dynamic v2 live schema inherits inventory _freq_limits (open & restore_native) or fails closed."""
        from remoterf_device_core.v2_errors import RemoteRFPolicyError

        class _StaleCustomUsrpSchema(UsrpSchema):
            """Mirrors a locally edited pre-PR UsrpPhaserSchema whose create_session_schema skips configure_lockouts."""

            def create_session_schema(self):
                clone = type(self)()
                clone.bind_inventory(self._inventory)
                clone.bind(clone.open_native_device())
                return clone

        stale_inv = _StaleCustomUsrpSchema()
        stale_inv.bind_inventory({"serial": "SRV01", "tx_allow": [[902e6, 928e6]]})
        self.inventory = stale_inv
        opened = self._open()
        sid, root = opened.session_id, opened.device_handle
        session = session_manager.get(sid)
        self.assertEqual(session.schema._freq_limits, stale_inv._freq_limits)
        bad_tx = self._invoke(sid, root, "set_tx_freq", {"tune_request": 2.45e9, "chan": 0})
        self.assertEqual(bad_tx.error.code, "RemoteRFPolicyError")

        # restore_native also propagates limits onto the newly created live schema.
        fake_gate = types.SimpleNamespace(journal=object())
        with patch("remoteRF_server.server.dynamic_v2.hardware_access.gate", return_value=fake_gate):
            binding = session_manager.integration_binding(
                sid, work_dir="/tmp", resources_released=lambda: True
            )
            binding.release_native()
            binding.restore_native()
        self.assertEqual(session.schema._freq_limits, stale_inv._freq_limits)
        session_manager.close_all()

        class _BrokenCustomUsrpSchema(UsrpSchema):
            """Cannot retain configured frequency limits -> must fail closed."""

            def configure_lockouts(self, cfg):
                self._freq_limits = None

            def create_session_schema(self):
                clone = type(self)()
                clone.bind_inventory(self._inventory)
                clone.bind(clone.open_native_device())
                return clone

        broken_inv = _BrokenCustomUsrpSchema()
        broken_inv.bind_inventory({"serial": "SRV01", "tx_allow": [[902e6, 928e6]]})
        # Simulate inventory holding active limits (as set by bind_inventory) while live clone cannot retain them.
        self.assertIsNotNone(broken_inv._freq_limits)
        self.inventory = broken_inv
        schema_hash = broken_inv.get_idl_v2()["schema_hash"]
        resp = self.control.OpenSession(
            grpc_pb2.OpenSessionRequest(
                token="tok-srv",
                requested_schema_hash=schema_hash,
                control_protocol_version="2.0",
                streaming_protocol_version="1.0",
            ),
            None,
        )
        self.assertEqual(resp.error.code, "RemoteRFPolicyError")
        # Drain async cleanup of the failed session before opening a new session on the same gid.
        session_manager.close_all()
        self.assertTrue(self.native.closed)

        # restore_native also fails closed when restored live schema drops limits.
        self.native.closed = False
        self.inventory = stale_inv
        opened2 = self._open()
        session2 = session_manager.get(opened2.session_id)
        with patch("remoteRF_server.server.dynamic_v2.hardware_access.gate", return_value=fake_gate):
            binding2 = session_manager.integration_binding(
                opened2.session_id, work_dir="/tmp", resources_released=lambda: True
            )
            binding2.release_native()
            self.native.closed = False
            self.inventory = broken_inv
            with self.assertRaises(RemoteRFPolicyError):
                binding2.restore_native()
            self.assertTrue(self.native.closed)

    def test_v2_empty_context_clears_bypass_and_stream_checks_hold_control_lock(self):
        """L5 (server): empty device context drops stale bypass; stream checks serialize with retunes."""
        import threading

        opened = self._open()
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

        # Enable admin bypass, then simulate get_local_device_context returning {}.
        self.bypass = True
        self.assertEqual(
            self._invoke(sid, root, "set_tx_freq", {"tune_request": 2.455e9, "chan": 0}).error.code,
            "",
        )
        self.assertTrue(session.schema.lockout_bypass)
        self.native.rx_freqs[0] = 2.455e9
        self.native.rx_lo_freqs[0] = 2.455e9
        payload = np.zeros(4, dtype="<c8").tobytes()

        with patch("remoteRF_server.server.dynamic_v2.get_local_device_context", return_value={}):
            # Invoke must drop stale bypass and refuse.
            inv_err = self._invoke(sid, root, "set_tx_freq", {"tune_request": 2.455e9, "chan": 0})
            self.assertEqual(inv_err.error.code, "RemoteRFPolicyError")
            self.assertFalse(session.schema.lockout_bypass)

            # SampleStream TX must drop stale bypass and refuse.
            session.schema.lockout_bypass = True
            tx_err = list(
                self.samples.SampleStream(
                    iter(
                        [
                            grpc_pb2.SampleFrame(
                                session_id=sid, handle=tx_handle, generation=tx_gen,
                                operation_id="op-l5-tx", sequence=0,
                                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                                kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                            ),
                            grpc_pb2.SampleFrame(
                                session_id=sid, handle=tx_handle, generation=tx_gen,
                                operation_id="op-l5-tx", sequence=1,
                                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                                kind=grpc_pb2.SAMPLE_FRAME_DATA,
                                dtype="<c8", shape=[4], channels=[0], sample_count=4,
                                payload=payload, timeout_sec=0.1, metadata_json="{}",
                            ),
                        ]
                    ),
                    None,
                )
            )[-1]
            self.assertEqual(tx_err.error.code, "RemoteRFPolicyError")
            self.assertFalse(session.schema.lockout_bypass)

            # SampleStream RX must drop stale bypass and refuse.
            session.schema.lockout_bypass = True
            rx_err = list(
                self.samples.SampleStream(
                    iter(
                        [
                            grpc_pb2.SampleFrame(
                                session_id=sid, handle=rx_handle, generation=rx_gen,
                                operation_id="op-l5-rx", sequence=0,
                                direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                                kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                            ),
                            grpc_pb2.SampleFrame(
                                session_id=sid, handle=rx_handle, generation=rx_gen,
                                operation_id="op-l5-rx", sequence=1,
                                direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                                kind=grpc_pb2.SAMPLE_FRAME_REQUEST,
                                dtype="<c8", shape=[4], channels=[0], sample_count=4,
                                timeout_sec=0.1, metadata_json="{}",
                            ),
                        ]
                    ),
                    None,
                )
            )[-1]
            self.assertEqual(rx_err.error.code, "RemoteRFPolicyError")
            self.assertFalse(session.schema.lockout_bypass)

        # Verify session.control_lock is held across SampleStream TX/RX and RXStreamer.issue_stream_cmd.
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
            tx_ok = list(
                self.samples.SampleStream(
                    iter(
                        [
                            grpc_pb2.SampleFrame(
                                session_id=sid, handle=tx_handle, generation=tx_gen,
                                operation_id="op-l5-lock-tx", sequence=2,
                                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                                kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                            ),
                            grpc_pb2.SampleFrame(
                                session_id=sid, handle=tx_handle, generation=tx_gen,
                                operation_id="op-l5-lock-tx", sequence=3,
                                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                                kind=grpc_pb2.SAMPLE_FRAME_DATA,
                                dtype="<c8", shape=[4], channels=[0], sample_count=4,
                                payload=payload, timeout_sec=0.1, metadata_json="{}",
                            ),
                        ]
                    ),
                    None,
                )
            )[-1]
            self.assertEqual(tx_ok.kind, grpc_pb2.SAMPLE_FRAME_RESULT)

            rx_ok = list(
                self.samples.SampleStream(
                    iter(
                        [
                            grpc_pb2.SampleFrame(
                                session_id=sid, handle=rx_handle, generation=rx_gen,
                                operation_id="op-l5-lock-rx", sequence=2,
                                direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                                kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                            ),
                            grpc_pb2.SampleFrame(
                                session_id=sid, handle=rx_handle, generation=rx_gen,
                                operation_id="op-l5-lock-rx", sequence=3,
                                direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                                kind=grpc_pb2.SAMPLE_FRAME_REQUEST,
                                dtype="<c8", shape=[4], channels=[0], sample_count=4,
                                timeout_sec=0.1, metadata_json="{}",
                            ),
                        ]
                    ),
                    None,
                )
            )[-1]
            self.assertEqual(rx_ok.kind, grpc_pb2.SAMPLE_FRAME_DATA)

            cmd_resp = self._invoke(
                sid,
                rx_handle,
                "issue_stream_cmd",
                {"__args__": [{"__uhd_type__": "StreamCMD", "mode": "num_done", "stream_now": True}]},
            )
            self.assertEqual(cmd_resp.error.code, "")

        self.assertEqual(observed, {"tx_send": True, "rx_recv": True, "rx_cmd": True})

    def test_v2_waveform_streamer_empty_channels_prefetch_reset_and_timed_tx_refused(self):
        """R2 & R3 (server): recv_num_samps/send_waveform checks, prefetch flush, and timed TX refusal."""
        opened = self._open()
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

        tx_handle_arg = {
            "__remoterf_handle_arg__": tx_handle,
            "type_id": "uhd.usrp.TXStreamer",
            "generation": tx_gen,
        }
        rx_handle_arg = {
            "__remoterf_handle_arg__": rx_handle,
            "type_id": "uhd.usrp.RXStreamer",
            "generation": rx_gen,
        }
        wf = np.zeros(8, dtype=np.complex64)

        # R2: empty channels and caller-supplied streamers refused on recv_num_samps and send_waveform.
        self.assertEqual(
            self._invoke(sid, root, "recv_num_samps", {"num_samps": 8, "freq": 915e6, "channels": []}).error.code,
            "RemoteRFPolicyError",
        )
        self.assertEqual(
            self._invoke(
                sid,
                root,
                "recv_num_samps",
                {"num_samps": 8, "freq": 915e6, "channels": [0], "streamer": rx_handle_arg},
            ).error.code,
            "RemoteRFPolicyError",
        )
        self.assertEqual(
            self._invoke(
                sid,
                root,
                "send_waveform",
                {"waveform_proto": wf, "duration": 0.1, "freq": 915e6, "rate": 1e6, "channels": []},
            ).error.code,
            "RemoteRFPolicyError",
        )
        self.assertEqual(
            self._invoke(
                sid,
                root,
                "send_waveform",
                {"waveform_proto": wf, "duration": 0.1, "freq": 915e6, "rate": 1e6, "channels": [0], "streamer": tx_handle_arg},
            ).error.code,
            "RemoteRFPolicyError",
        )

        # R2: valid recv_num_samps resets RX prefetch when RX limits are active.
        stale = np.full((1, 4), 77 + 0j, dtype=np.complex64)
        rx_entry.rx_prefetch.put((stale, {"error_code": "none"}))
        rx_entry.rx_pending = (stale, {"error_code": "none"}, 0)
        ok_rx = self._invoke(sid, root, "recv_num_samps", {"num_samps": 8, "freq": 915e6, "channels": [0]})
        self.assertEqual(ok_rx.error.code, "")
        self.assertTrue(rx_entry.rx_prefetch.empty())
        self.assertIsNone(rx_entry.rx_pending)

        # R3: timed send_waveform(start_time=...) refused.
        bad_wf_time = self._invoke(
            sid,
            root,
            "send_waveform",
            {
                "waveform_proto": wf,
                "duration": 0.1,
                "freq": 915e6,
                "rate": 1e6,
                "channels": [0],
                "start_time": {"__uhd_type__": "TimeSpec", "secs": 5.0},
            },
        )
        self.assertEqual(bad_wf_time.error.code, "RemoteRFPolicyError")

        # R3: SampleStream TX refuses timed TXMetadata and accepts default untimed TimeSpec(0.0).
        payload = np.zeros(4, dtype="<c8").tobytes()
        timed_md = json.dumps({"has_time_spec": True, "time_spec": {"__uhd_type__": "TimeSpec", "secs": 5.0}})
        timed_err = list(
            self.samples.SampleStream(
                iter(
                    [
                        grpc_pb2.SampleFrame(
                            session_id=sid, handle=tx_handle, generation=tx_gen,
                            operation_id="op-timed-tx", sequence=0,
                            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                            kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                        ),
                        grpc_pb2.SampleFrame(
                            session_id=sid, handle=tx_handle, generation=tx_gen,
                            operation_id="op-timed-tx", sequence=1,
                            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                            kind=grpc_pb2.SAMPLE_FRAME_DATA,
                            dtype="<c8", shape=[4], channels=[0], sample_count=4,
                            payload=payload, timeout_sec=0.1, metadata_json=timed_md,
                        ),
                    ]
                ),
                None,
            )
        )[-1]
        self.assertEqual(timed_err.kind, grpc_pb2.SAMPLE_FRAME_ERROR)
        self.assertEqual(timed_err.error.code, "RemoteRFPolicyError")
        self.assertEqual(self.native.tx_streamer.sent, [])

        untimed_md = json.dumps({"has_time_spec": False, "time_spec": {"__uhd_type__": "TimeSpec", "secs": 0.0}})
        untimed_ok = list(
            self.samples.SampleStream(
                iter(
                    [
                        grpc_pb2.SampleFrame(
                            session_id=sid, handle=tx_handle, generation=tx_gen,
                            operation_id="op-untimed-tx", sequence=2,
                            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                            kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                        ),
                        grpc_pb2.SampleFrame(
                            session_id=sid, handle=tx_handle, generation=tx_gen,
                            operation_id="op-untimed-tx", sequence=3,
                            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                            kind=grpc_pb2.SAMPLE_FRAME_DATA,
                            dtype="<c8", shape=[4], channels=[0], sample_count=4,
                            payload=payload, timeout_sec=0.1, metadata_json=untimed_md,
                        ),
                    ]
                ),
                None,
            )
        )[-1]
        self.assertEqual(untimed_ok.kind, grpc_pb2.SAMPLE_FRAME_RESULT)
        self.assertEqual(self.native.tx_streamer.sent, [4])

    def test_v2_long_rx_wait_allows_stop_cont_and_prompt_close_session(self):
        """R5 (server): stop_cont and CloseSession complete promptly during long RX waits (direct & prefetched)."""
        opened = self._open()
        sid, root = opened.session_id, opened.device_handle
        rx_info = decode_value(
            self._invoke(
                sid,
                root,
                "get_rx_stream",
                {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
            ).result
        )
        rx_handle, rx_gen = rx_info["__remoterf_handle__"], int(rx_info["generation"])

        recv_entered = threading.Event()

        def stalled_recv(buf, meta, timeout):
            recv_entered.set()
            time.sleep(min(float(timeout), 30.0))
            meta.error_code = "timeout"
            return 0

        # Part A1: RXStreamer.issue_stream_cmd(stop_cont) does not block on control_lock during a 30s direct RX wait.
        frames_out = []

        def run_rx(op_id, seq_base):
            frames_out.append(
                list(
                    self.samples.SampleStream(
                        iter(
                            [
                                grpc_pb2.SampleFrame(
                                    session_id=sid, handle=rx_handle, generation=rx_gen,
                                    operation_id=op_id, sequence=seq_base,
                                    direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                                    kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                                ),
                                grpc_pb2.SampleFrame(
                                    session_id=sid, handle=rx_handle, generation=rx_gen,
                                    operation_id=op_id, sequence=seq_base + 1,
                                    direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                                    kind=grpc_pb2.SAMPLE_FRAME_REQUEST,
                                    dtype="<c8", shape=[4], channels=[0], sample_count=4,
                                    timeout_sec=30.0, metadata_json="{}",
                                ),
                            ]
                        ),
                        None,
                    )
                )
            )

        with patch.object(self.native.rx_streamer, "recv", side_effect=stalled_recv):
            t = threading.Thread(target=run_rx, args=("op-r5-stop", 0))
            t.start()
            self.assertTrue(recv_entered.wait(2.0))
            t0 = time.monotonic()
            stop_resp = self._invoke(
                sid,
                rx_handle,
                "issue_stream_cmd",
                {"__args__": [{"__uhd_type__": "StreamCMD", "mode": "stop_cont", "stream_now": True}]},
            )
            stop_elapsed = time.monotonic() - t0
            t.join(timeout=2.0)
            self.assertEqual(stop_resp.error.code, "")
            self.assertLess(stop_elapsed, 1.0)
            self.assertFalse(t.is_alive())

            # Part A2: CloseSession during a 30s direct RX wait completes promptly.
            recv_entered.clear()
            t2 = threading.Thread(target=run_rx, args=("op-r5-close-direct", 2))
            t2.start()
            self.assertTrue(recv_entered.wait(2.0))
            t0 = time.monotonic()
            close_resp = self.control.CloseSession(
                grpc_pb2.CloseSessionRequest(session_id=sid),
                None,
            )
            close_elapsed = time.monotonic() - t0
            t2.join(timeout=2.0)
            self.assertEqual(close_resp.error.code, "")
            self.assertTrue(close_resp.closed)
            self.assertLess(close_elapsed, 1.5)
            self.assertFalse(t2.is_alive())
            deadline = time.monotonic() + 2.0
            while session_manager.cleanup_pending(7) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(session_manager.cleanup_pending(7))
            self.assertTrue(self.native.closed)

        # Part B: CloseSession during a 30s prefetched RX wait (rx_worker alive, stalled queue)
        # enters admitted(), wakes promptly, and never blocks _Reaper on control_lock.
        self.native.closed = False
        opened2 = self._open()
        sid2, root2 = opened2.session_id, opened2.device_handle
        rx_info2 = decode_value(
            self._invoke(
                sid2,
                root2,
                "get_rx_stream",
                {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
            ).result
        )
        rx_handle2, rx_gen2 = rx_info2["__remoterf_handle__"], int(rx_info2["generation"])
        session2 = session_manager.get(sid2)
        rx_entry2 = session2.handles[rx_handle2]

        with patch.object(self.native.rx_streamer, "recv", side_effect=stalled_recv):
            start_resp = self._invoke(
                sid2,
                rx_handle2,
                "issue_stream_cmd",
                {"__args__": [{"__uhd_type__": "StreamCMD", "mode": "start_cont", "stream_now": True}]},
            )
            self.assertEqual(start_resp.error.code, "")
            self.assertIsNotNone(rx_entry2.rx_worker)
            self.assertTrue(rx_entry2.rx_worker.is_alive())

            prefetched_done = []

            def run_prefetched_rx():
                prefetched_done.append(
                    list(
                        self.samples.SampleStream(
                            iter(
                                [
                                    grpc_pb2.SampleFrame(
                                        session_id=sid2, handle=rx_handle2, generation=rx_gen2,
                                        operation_id="op-r5-prefetch", sequence=0,
                                        direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                                        kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                                    ),
                                    grpc_pb2.SampleFrame(
                                        session_id=sid2, handle=rx_handle2, generation=rx_gen2,
                                        operation_id="op-r5-prefetch", sequence=1,
                                        direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                                        kind=grpc_pb2.SAMPLE_FRAME_REQUEST,
                                        dtype="<c8", shape=[4], channels=[0], sample_count=4,
                                        timeout_sec=30.0, metadata_json="{}",
                                    ),
                                ]
                            ),
                            None,
                        )
                    )
                )

            t3 = threading.Thread(target=run_prefetched_rx)
            t3.start()
            wait_deadline = time.monotonic() + 2.0
            while session2.activity.idle() and time.monotonic() < wait_deadline:
                time.sleep(0.005)
            self.assertFalse(session2.activity.idle())

            t0 = time.monotonic()
            close_resp2 = self.control.CloseSession(
                grpc_pb2.CloseSessionRequest(session_id=sid2),
                None,
            )
            close_elapsed2 = time.monotonic() - t0
            t3.join(timeout=2.0)
            self.assertEqual(close_resp2.error.code, "")
            self.assertTrue(close_resp2.closed)
            self.assertLess(close_elapsed2, 1.5)
            self.assertFalse(t3.is_alive())
            deadline = time.monotonic() + 2.0
            while session_manager.cleanup_pending(7) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(session_manager.cleanup_pending(7))
            self.assertTrue(self.native.closed)

    def test_v2_stream_mode_only_stop_cont_and_root_stop_wakes_prefetched_rx(self):
        """R5 (server): StreamCMD with only .stream_mode works and MultiUSRP stop_cont wakes _recv_prefetched."""

        class _StreamModeOnlyCMD:
            def __init__(self, mode):
                self.stream_mode = mode
                self.stream_now = True
                self.time_spec = None
                self.num_samps = 0

        self.fake_uhd.types.StreamCMD = _StreamModeOnlyCMD
        opened = self._open()
        sid, root = opened.session_id, opened.device_handle
        rx_info = decode_value(
            self._invoke(
                sid,
                root,
                "get_rx_stream",
                {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
            ).result
        )
        rx_handle, rx_gen = rx_info["__remoterf_handle__"], int(rx_info["generation"])
        session = session_manager.get(sid)
        rx_entry = session.handles[rx_handle]

        def stalled_recv(buf, meta, timeout):
            time.sleep(min(float(timeout), 30.0))
            meta.error_code = "timeout"
            return 0

        with patch.object(self.native.rx_streamer, "recv", side_effect=stalled_recv):
            # start_cont with .stream_mode-only StreamCMD starts rx_worker.
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

            # Start a 30s prefetched RX wait, then stop via MultiUSRP.issue_stream_cmd(stop_cont).
            rx_frames = []

            def run_prefetched_rx():
                rx_frames.append(
                    list(
                        self.samples.SampleStream(
                            iter(
                                [
                                    grpc_pb2.SampleFrame(
                                        session_id=sid, handle=rx_handle, generation=rx_gen,
                                        operation_id="op-r5-root-stop", sequence=0,
                                        direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                                        kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                                    ),
                                    grpc_pb2.SampleFrame(
                                        session_id=sid, handle=rx_handle, generation=rx_gen,
                                        operation_id="op-r5-root-stop", sequence=1,
                                        direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                                        kind=grpc_pb2.SAMPLE_FRAME_REQUEST,
                                        dtype="<c8", shape=[4], channels=[0], sample_count=4,
                                        timeout_sec=30.0, metadata_json="{}",
                                    ),
                                ]
                            ),
                            None,
                        )
                    )
                )

            t = threading.Thread(target=run_prefetched_rx)
            t.start()
            wait_deadline = time.monotonic() + 2.0
            while session.activity.idle() and time.monotonic() < wait_deadline:
                time.sleep(0.005)
            self.assertFalse(session.activity.idle())

            # Even if RX freq moved out of band, MultiUSRP.issue_stream_cmd(stop_cont) succeeds
            # without blocking on control_lock and promptly wakes _recv_prefetched.
            self.native.rx_freqs[0] = 2.45e9
            self.native.rx_lo_freqs[0] = 2.45e9
            t0 = time.monotonic()
            stop_resp = self._invoke(
                sid,
                root,
                "issue_stream_cmd",
                {"rate": {"__uhd_type__": "StreamCMD", "mode": "stop_cont", "stream_now": True}, "chan": 0},
            )
            stop_elapsed = time.monotonic() - t0
            t.join(timeout=2.0)
            self.assertEqual(stop_resp.error.code, "")
            self.assertLess(stop_elapsed, 1.0)
            self.assertFalse(t.is_alive())
            self.assertEqual(len(self.native.root_stream_cmds), 1)
            self.assertFalse(hasattr(self.native.root_stream_cmds[0][0], "mode"))

            # RXStreamer.issue_stream_cmd(stop_cont) also works out of band with .stream_mode-only StreamCMD.
            rx_stop = self._invoke(
                sid,
                rx_handle,
                "issue_stream_cmd",
                {"__args__": [{"__uhd_type__": "StreamCMD", "mode": "stop_cont", "stream_now": True}]},
            )
            self.assertEqual(rx_stop.error.code, "")

    def test_v2_direct_rx_sliced_partial_read_preserves_first_packet_metadata(self):
        """N2 (server): sliced direct RX partial read keeps first packet metadata instead of final slice timeout."""

        class _Fake_rx_metadata(_FakeRXMetadata):
            pass

        class _Fake_time_spec(_FakeTimeSpec):
            pass

        self.fake_uhd.types.RXMetadata = _Fake_rx_metadata
        self.fake_uhd.types.TimeSpec = _Fake_time_spec

        opened = self._open()
        sid, root = opened.session_id, opened.device_handle
        rx_info = decode_value(
            self._invoke(
                sid,
                root,
                "get_rx_stream",
                {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
            ).result
        )
        rx_handle, rx_gen = rx_info["__remoterf_handle__"], int(rx_info["generation"])

        calls = {"n": 0}

        def partial_then_timeout(buf, meta, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                buf[:2] = 7 + 0j
                meta.error_code = "none"
                meta.has_time_spec = True
                meta.time_spec = _Fake_time_spec(42.5)
                return 2
            time.sleep(min(float(timeout), 0.1))
            meta.error_code = "timeout"
            meta.has_time_spec = False
            meta.time_spec = _Fake_time_spec(0.0)
            return 0

        with patch.object(self.native.rx_streamer, "recv", side_effect=partial_then_timeout):
            frames = list(
                self.samples.SampleStream(
                    iter(
                        [
                            grpc_pb2.SampleFrame(
                                session_id=sid, handle=rx_handle, generation=rx_gen,
                                operation_id="op-n2-partial", sequence=0,
                                direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                                kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                            ),
                            grpc_pb2.SampleFrame(
                                session_id=sid, handle=rx_handle, generation=rx_gen,
                                operation_id="op-n2-partial", sequence=1,
                                direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                                kind=grpc_pb2.SAMPLE_FRAME_REQUEST,
                                dtype="<c8", shape=[4], channels=[0], sample_count=4,
                                timeout_sec=0.25, metadata_json="{}",
                            ),
                        ]
                    ),
                    None,
                )
            )
        data_frame = frames[-1]
        self.assertEqual(data_frame.kind, grpc_pb2.SAMPLE_FRAME_DATA)
        self.assertEqual(data_frame.sample_count, 2)
        self.assertEqual(data_frame.device_time_secs, 42.5)
        meta = json.loads(data_frame.metadata_json)
        self.assertEqual(meta["error_code"], "none")
        self.assertTrue(meta["has_time_spec"])
        self.assertEqual(meta["time_spec"]["secs"], 42.5)

        # Full timeout with 0 samples still reports timeout metadata.
        def always_timeout(buf, meta, timeout):
            time.sleep(min(float(timeout), 0.1))
            meta.error_code = "timeout"
            meta.has_time_spec = False
            meta.time_spec = _Fake_time_spec(0.0)
            return 0

        with patch.object(self.native.rx_streamer, "recv", side_effect=always_timeout):
            timeout_frames = list(
                self.samples.SampleStream(
                    iter(
                        [
                            grpc_pb2.SampleFrame(
                                session_id=sid, handle=rx_handle, generation=rx_gen,
                                operation_id="op-n2-empty", sequence=2,
                                direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                                kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                            ),
                            grpc_pb2.SampleFrame(
                                session_id=sid, handle=rx_handle, generation=rx_gen,
                                operation_id="op-n2-empty", sequence=3,
                                direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                                kind=grpc_pb2.SAMPLE_FRAME_REQUEST,
                                dtype="<c8", shape=[4], channels=[0], sample_count=4,
                                timeout_sec=0.15, metadata_json="{}",
                            ),
                        ]
                    ),
                    None,
                )
            )
        empty_frame = timeout_frames[-1]
        self.assertEqual(empty_frame.sample_count, 0)
        self.assertEqual(json.loads(empty_frame.metadata_json)["error_code"], "timeout")

    def test_v2_set_tx_rate_after_close_handle_and_master_clock_rate_refused(self):
        """Hunt N1 (server): set_tx_rate checked after closing TX handle; set_master_clock_rate refused."""
        mcr_calls = []
        self.native.set_master_clock_rate = lambda rate, mboard=0: mcr_calls.append((rate, mboard))

        opened = self._open()
        sid, root = opened.session_id, opened.device_handle
        self.assertEqual(self._invoke(sid, root, "set_tx_freq", {"tune_request": 925e6, "chan": 0}).error.code, "")
        tx_info = decode_value(
            self._invoke(
                sid,
                root,
                "get_tx_stream",
                {"__args__": [{"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}]},
            ).result
        )
        tx_handle = tx_info["__remoterf_handle__"]

        # Close TX handle; subsequent out-of-band set_tx_rate must still be refused.
        self.assertTrue(session_manager.close_handle(sid, tx_handle))
        bad_rate = self._invoke(sid, root, "set_tx_rate", {"rate": 40e6, "chan": 0})
        self.assertEqual(bad_rate.error.code, "RemoteRFPolicyError")
        self.assertEqual(self.native.tx_rate, 4e6)

        # In-band rate change still succeeds.
        ok_rate = self._invoke(sid, root, "set_tx_rate", {"rate": 2e6, "chan": 0})
        self.assertEqual(ok_rate.error.code, "")
        self.assertEqual(self.native.tx_rate, 2e6)

        # set_master_clock_rate refused while TX limits are active.
        bad_mcr = self._invoke(sid, root, "set_master_clock_rate", {"rate": 192e6})
        self.assertEqual(bad_mcr.error.code, "RemoteRFPolicyError")
        self.assertEqual(mcr_calls, [])

    def test_v2_fresh_session_allows_rate_and_mcr_before_tx_then_enforces_after_close(self):
        """Fresh v2 session with OOB default LO and wide rate can configure TX, then enforces rate/MCR once TX starts."""
        mcr_calls = []
        self.native.set_master_clock_rate = lambda rate, mboard=0: mcr_calls.append((rate, mboard))
        self.native.tx_freqs = {0: 0.0, 1: 0.0}
        self.native.tx_lo_freqs = {0: 0.0, 1: 0.0}
        self.native.tx_rates = {0: 200e6, 1: 200e6}

        opened = self._open()
        sid, root = opened.session_id, opened.device_handle

        # Tuning first fails while default rate is 200 MS/s; premature get_tx_stream fails and does not trigger TX-in-flight.
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

        # Fresh session before any TX: set_master_clock_rate and set_tx_rate succeed, then set_tx_freq and streaming work.
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
        payload = np.zeros(4, dtype="<c8").tobytes()
        sent = list(
            self.samples.SampleStream(
                iter(
                    [
                        grpc_pb2.SampleFrame(
                            session_id=sid, handle=tx_handle, generation=tx_gen,
                            operation_id="op-fresh-tx", sequence=0,
                            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                            kind=grpc_pb2.SAMPLE_FRAME_OPEN, credits=1,
                        ),
                        grpc_pb2.SampleFrame(
                            session_id=sid, handle=tx_handle, generation=tx_gen,
                            operation_id="op-fresh-tx", sequence=1,
                            direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                            kind=grpc_pb2.SAMPLE_FRAME_DATA,
                            dtype="<c8", shape=[4], channels=[0], sample_count=4,
                            payload=payload, timeout_sec=0.1, metadata_json="{}",
                        ),
                    ]
                ),
                None,
            )
        )[-1]
        self.assertEqual(sent.kind, grpc_pb2.SAMPLE_FRAME_RESULT)
        self.assertEqual(self.native.tx_streamer.sent, [4])

        # After closing the TX handle, out-of-band set_tx_rate and set_master_clock_rate are refused.
        self.assertTrue(session_manager.close_handle(sid, tx_handle))
        self.assertEqual(
            self._invoke(sid, root, "set_tx_rate", {"rate": 40e6, "chan": 0}).error.code,
            "RemoteRFPolicyError",
        )
        self.assertEqual(self.native.tx_rates[0], 1e6)
        self.assertEqual(
            self._invoke(sid, root, "set_master_clock_rate", {"rate": 192e6}).error.code,
            "RemoteRFPolicyError",
        )
        self.assertEqual(len(mcr_calls), 1)
        # In-band rate change still succeeds.
        self.assertEqual(self._invoke(sid, root, "set_tx_rate", {"rate": 2e6, "chan": 0}).error.code, "")

        # Closing the session and opening a fresh session allows pre-TX rate/MCR setup again.
        self.assertEqual(
            self.control.CloseSession(grpc_pb2.CloseSessionRequest(session_id=sid), None).error.code,
            "",
        )
        deadline = time.monotonic() + 2.0
        while session_manager.cleanup_pending(7) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.native.closed = False
        self.native.tx_freqs = {0: 0.0, 1: 0.0}
        self.native.tx_lo_freqs = {0: 0.0, 1: 0.0}
        self.native.tx_rates = {0: 200e6, 1: 200e6}
        opened2 = self._open()
        sid2, root2 = opened2.session_id, opened2.device_handle
        self.assertEqual(self._invoke(sid2, root2, "set_master_clock_rate", {"rate": 32e6}).error.code, "")
        self.assertEqual(self._invoke(sid2, root2, "set_tx_rate", {"rate": 1e6, "chan": 0}).error.code, "")
        self.assertEqual(len(mcr_calls), 2)


if __name__ == "__main__":
    unittest.main()
