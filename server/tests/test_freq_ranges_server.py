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

    def issue_stream_cmd(self, cmd, chan=(1 << 64) - 1):
        self.root_stream_cmds.append((cmd, chan))

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


if __name__ == "__main__":
    unittest.main()
