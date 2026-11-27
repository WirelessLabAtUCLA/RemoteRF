#!/usr/bin/env python3
"""Comprehensive protocol and schema test suite for upgraded RFbeam K-MD7 driver."""

from __future__ import annotations

import json
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import serial

from remoterf_device_core.drivers.rfbeam_kmd7.kmd7 import (
    BAUD_RATES,
    BAUD_RATES_REV,
    FLAG_ALL,
    FLAG_DONE,
    FLAG_PDAT,
    FLAG_RADC,
    FLAG_RFFT,
    FLAG_TDAT,
    KMD7Error,
    KMD7Radar,
    RADC_NUM_SAMPLES,
    RFFT_NUM_BINS,
    RadarParameters,
)
from remoterf_device_core.drivers.rfbeam_kmd7.rfbeam_kmd7_schema import (
    RFBeamKMD7Device,
    RFBeamKMD7Schema,
)


def _pack_packet(header: str, payload: bytes = b"") -> bytes:
    return header.encode("ascii") + struct.pack("<I", len(payload)) + payload


def _build_test_radc_payload() -> bytes:
    """Build deterministic 6144-byte RADC payload with distinct channel ranges."""
    blocks = [
        [1000 + i for i in range(RADC_NUM_SAMPLES)],   # IF1 Freq A I
        [2000 + i for i in range(RADC_NUM_SAMPLES)],   # IF1 Freq A Q
        [3000 + i for i in range(RADC_NUM_SAMPLES)],   # IF2 Freq A I
        [4000 + i for i in range(RADC_NUM_SAMPLES)],   # IF2 Freq A Q
        [5000 + i for i in range(RADC_NUM_SAMPLES)],   # IF1 Freq B I
        [6000 + i for i in range(RADC_NUM_SAMPLES)],   # IF1 Freq B Q
    ]
    all_vals = []
    for blk in blocks:
        all_vals.extend(blk)
    return struct.pack("<3072H", *all_vals)


def _build_test_rfft_payload() -> bytes:
    """Build deterministic 2048-byte RFFT payload (spectrum & threshold in dB * 100)."""
    spec = [4000 + i for i in range(RFFT_NUM_BINS)]   # 40.00 .. 45.11 dB
    thresh = [2500 + i for i in range(RFFT_NUM_BINS)]  # 25.00 .. 30.11 dB
    return struct.pack("<1024H", *(spec + thresh))


def _build_test_pdat_payload() -> bytes:
    # 2 raw targets: (dist_cm, speed_x100, angle_x100, mag_x100)
    t1 = struct.pack("<HhhH", 250, -360, 1500, 8250)   # 2.50m, -3.60 km/h, +15.0 deg, 82.5 dB
    t2 = struct.pack("<HhhH", 1200, 500, -1000, 6400)  # 12.00m, +5.00 km/h, -10.0 deg, 64.0 dB
    return t1 + t2


def _build_test_tdat_payload() -> bytes:
    # 1 tracked target with tracking_id=7
    return struct.pack("<HhhHB", 254, -360, 1000, 8600, 7)


class MockKMD7Serial:
    """Byte-accurate in-memory K-MD7 UART protocol simulator."""

    def __init__(
        self,
        port: str = "/dev/mockFT230X",
        baudrate: int = 115200,
        *,
        initial_sensor_baudrate: int = 115200,
        initial_stream_mask: int = 0,
        **kwargs,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.sensor_baudrate = initial_sensor_baudrate
        self.is_open = True
        self.stream_mask = initial_stream_mask
        self.connected = initial_sensor_baudrate != 115200
        self.frame_counter = 10

        self.firmware = "K-MD7_APP-RFB-0205"
        self.params = self._factory_params()
        self.commands_log: list[tuple[str, bytes]] = []
        self.forced_error: int | None = None

        self._tx_to_host = bytearray()
        self._rx_from_host = bytearray()

        if self.stream_mask:
            self._queue_frame(self.stream_mask)

    def _factory_params(self) -> RadarParameters:
        return RadarParameters(
            firmware=self.firmware,
            frequency_channel=1,
            speed_setting=1,
            range_setting=1,
            threshold_offset_db=12,
            tracking_filter=0,
            min_zone_dist_pct=40,
            max_zone_dist_pct=60,
            min_zone_angle_deg=-30,
            max_zone_angle_deg=30,
            min_zone_speed_pct=5,
            max_zone_speed_pct=100,
            direction_filter=2,
        )

    @property
    def in_waiting(self) -> int:
        if not self._tx_to_host and self.stream_mask and self.baudrate == self.sensor_baudrate:
            self._queue_frame(self.stream_mask)
        return len(self._tx_to_host)

    def read(self, size: int = 1) -> bytes:
        if not self.is_open:
            raise RuntimeError("Serial port closed")
        if not self._tx_to_host and self.stream_mask and self.baudrate == self.sensor_baudrate:
            self._queue_frame(self.stream_mask)
        if not self._tx_to_host:
            return b""
        n = min(size, len(self._tx_to_host))
        out = bytes(self._tx_to_host[:n])
        del self._tx_to_host[:n]
        return out

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise RuntimeError("Serial port closed")
        # Only decode commands when host & sensor baud rates match
        if self.baudrate != self.sensor_baudrate:
            return len(data)
        self._rx_from_host.extend(data)
        self._process_commands()
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self._tx_to_host.clear()

    def reset_output_buffer(self) -> None:
        self._rx_from_host.clear()

    def close(self) -> None:
        self.is_open = False

    def _enqueue_resp(self, code: int) -> None:
        self._tx_to_host.extend(_pack_packet("RESP", bytes([code])))

    def _queue_frame(self, mask: int) -> None:
        self.frame_counter += 1
        if mask & FLAG_RADC:
            self._tx_to_host.extend(_pack_packet("RADC", _build_test_radc_payload()))
        if mask & FLAG_RFFT:
            self._tx_to_host.extend(_pack_packet("RFFT", _build_test_rfft_payload()))
        if mask & FLAG_PDAT:
            self._tx_to_host.extend(_pack_packet("PDAT", _build_test_pdat_payload()))
        if mask & FLAG_TDAT:
            self._tx_to_host.extend(_pack_packet("TDAT", _build_test_tdat_payload()))
        if mask & FLAG_DONE:
            self._tx_to_host.extend(_pack_packet("DONE", struct.pack("<I", self.frame_counter)))

    def _process_commands(self) -> None:
        while len(self._rx_from_host) >= 8:
            hdr = bytes(self._rx_from_host[:4]).decode("ascii", errors="replace")
            plen = struct.unpack("<I", self._rx_from_host[4:8])[0]
            if len(self._rx_from_host) < 8 + plen:
                break
            payload = bytes(self._rx_from_host[8 : 8 + plen])
            del self._rx_from_host[: 8 + plen]
            self.commands_log.append((hdr, payload))
            self._handle_command(hdr, payload)

    def _handle_command(self, hdr: str, payload: bytes) -> None:
        if self.forced_error is not None and hdr != "GBYE":
            err = self.forced_error
            self.forced_error = None
            self._enqueue_resp(err)
            return

        if hdr == "INIT":
            code = payload[0]
            if code not in BAUD_RATES_REV:
                self._enqueue_resp(2)
                return
            self._enqueue_resp(0)
            self._tx_to_host.extend(_pack_packet("VERS", self.firmware.encode("ascii") + b"\x00"))
            self.sensor_baudrate = BAUD_RATES_REV[code]
            self.connected = True
            return

        if hdr == "GBYE":
            self._enqueue_resp(0)
            self.stream_mask = 0
            self.sensor_baudrate = 115200
            self.connected = False
            return

        if hdr == "GRPS":
            self._enqueue_resp(0)
            self._tx_to_host.extend(_pack_packet("RPST", self.params.to_bytes()))
            return

        if hdr == "SRPS":
            try:
                parsed = RadarParameters.from_bytes(payload)
                if parsed.firmware != self.firmware:
                    self._enqueue_resp(3)
                    return
                parsed.validate()
            except Exception:
                self._enqueue_resp(2)
                return
            self.params = parsed
            self._enqueue_resp(0)
            return

        if hdr == "RFSE":
            self.params = self._factory_params()
            self._enqueue_resp(0)
            return

        if hdr == "RDOT":
            mask = payload[0]
            self.stream_mask = mask
            self._enqueue_resp(0)
            if mask != 0:
                # Queue one immediate in-flight frame to verify stop_streaming drains cleanly
                self._queue_frame(mask)
            return

        if hdr == "GNFD":
            mask = payload[0]
            self._enqueue_resp(0)
            self._queue_frame(mask)
            return

        single_byte_handlers = {
            "RBFR": ("frequency_channel", lambda b: b[0], (0, 2)),
            "RSPI": ("speed_setting", lambda b: b[0], (0, 2)),
            "RRAI": ("range_setting", lambda b: b[0], (0, 2)),
            "THOF": ("threshold_offset_db", lambda b: b[0], (0, 60)),
            "TRFT": ("tracking_filter", lambda b: b[0], (0, 2)),
            "MIRA": ("min_zone_dist_pct", lambda b: b[0], (0, 100)),
            "MARA": ("max_zone_dist_pct", lambda b: b[0], (0, 100)),
            "MIAN": ("min_zone_angle_deg", lambda b: struct.unpack("b", b[:1])[0], (-30, 30)),
            "MAAN": ("max_zone_angle_deg", lambda b: struct.unpack("b", b[:1])[0], (-30, 30)),
            "MISP": ("min_zone_speed_pct", lambda b: b[0], (0, 100)),
            "MASP": ("max_zone_speed_pct", lambda b: b[0], (0, 100)),
            "DEDI": ("direction_filter", lambda b: b[0], (0, 2)),
        }
        if hdr in single_byte_handlers:
            attr, decoder, (lo, hi) = single_byte_handlers[hdr]
            val = decoder(payload)
            if not (lo <= val <= hi):
                self._enqueue_resp(2)
                return
            setattr(self.params, attr, val)
            self._enqueue_resp(0)
            return

        self._enqueue_resp(1)


class TestKMD7DriverProtocol(unittest.TestCase):
    """Low-level binary protocol tests for KMD7Radar."""

    def _make_connected_radar(self, baudrate: int = 115200, mock_ser: MockKMD7Serial | None = None):
        ser = mock_ser or MockKMD7Serial()
        radar = KMD7Radar(port="/dev/mockFT230X", baudrate=baudrate, timeout=0.5)
        with patch.object(radar, "_open_serial", return_value=ser):
            radar.connect()
        return radar, ser

    def test_find_serial_port_strict_vs_fallback(self):
        generic_port = SimpleNamespace(device="/dev/ttyUSB9", vid=0x10C4, pid=0xEA60, description="CP2102")
        ftdi_port = SimpleNamespace(device="/dev/ttyUSB1", vid=0x0403, pid=0x6015, description="FT230X Basic UART")

        with patch(
            "remoterf_device_core.drivers.rfbeam_kmd7.kmd7.serial.tools.list_ports.comports",
            return_value=[generic_port],
        ):
            self.assertIsNone(KMD7Radar.find_serial_port(strict=True))
            self.assertEqual(KMD7Radar.find_serial_port(strict=False), "/dev/ttyUSB9")

        with patch(
            "remoterf_device_core.drivers.rfbeam_kmd7.kmd7.serial.tools.list_ports.comports",
            return_value=[generic_port, ftdi_port],
        ):
            self.assertEqual(KMD7Radar.find_serial_port(strict=True), "/dev/ttyUSB1")

    def test_find_serial_port_defaults_to_strict_and_make_device_uses_strict(self):
        usb_port = SimpleNamespace(device="/dev/ttyUSB0", vid=0x10C4, pid=0xEA60, description="CP2102")
        acm_port = SimpleNamespace(device="/dev/ttyACM0", vid=0x2341, pid=0x0043, description="Arduino")
        desc_ftdi = SimpleNamespace(device="/dev/ttyUSB2", vid=None, pid=None, description="FT230X Basic UART")

        with patch(
            "remoterf_device_core.drivers.rfbeam_kmd7.kmd7.serial.tools.list_ports.comports",
            return_value=[usb_port, acm_port],
        ):
            self.assertIsNone(KMD7Radar.find_serial_port())
            self.assertIsNone(RFBeamKMD7Schema.make_device())

        with patch(
            "remoterf_device_core.drivers.rfbeam_kmd7.kmd7.serial.tools.list_ports.comports",
            return_value=[usb_port, desc_ftdi],
        ):
            self.assertEqual(KMD7Radar.find_serial_port(), "/dev/ttyUSB2")

    def test_connect_custom_baud_and_crash_recovery(self):
        # Sensor left streaming at 3 Mbps from crashed previous run
        crashed_ser = MockKMD7Serial(initial_sensor_baudrate=3000000, initial_stream_mask=FLAG_PDAT | FLAG_DONE)
        radar = KMD7Radar(port="/dev/mockFT230X", baudrate=2000000, timeout=0.5)
        with patch.object(radar, "_open_serial", return_value=crashed_ser):
            radar.connect()

        self.assertEqual(radar.baudrate, 2000000)
        self.assertEqual(crashed_ser.sensor_baudrate, 2000000)
        self.assertEqual(radar.firmware_version, "K-MD7_APP-RFB-0205")
        self.assertGreater(radar.total_bytes_read, 0)

    def test_all_19_safe_commands_and_signed_angles(self):
        radar, ser = self._make_connected_radar(baudrate=921600)

        radar.set_frequency_channel(2)
        radar.set_speed_range(0)
        radar.set_max_range(2)
        radar.set_threshold_offset(24)
        radar.set_tracking_filter(2)
        radar.set_min_zone_distance(15)
        radar.set_max_zone_distance(85)
        radar.set_min_zone_angle(-30)
        radar.set_max_zone_angle(22)
        radar.set_min_speed_filter(10)
        radar.set_max_speed_filter(90)
        radar.set_direction_filter(1)

        p = radar.get_parameters()
        self.assertEqual(p.frequency_channel, 2)
        self.assertEqual(p.speed_setting, 0)
        self.assertEqual(p.range_setting, 2)
        self.assertEqual(p.threshold_offset_db, 24)
        self.assertEqual(p.tracking_filter, 2)
        self.assertEqual(p.min_zone_dist_pct, 15)
        self.assertEqual(p.max_zone_dist_pct, 85)
        self.assertEqual(p.min_zone_angle_deg, -30)
        self.assertEqual(p.max_zone_angle_deg, 22)
        self.assertEqual(p.min_zone_speed_pct, 10)
        self.assertEqual(p.max_zone_speed_pct, 90)
        self.assertEqual(p.direction_filter, 1)

        # SRPS atomic update
        new_p = RadarParameters(
            firmware="",  # verify driver fills from self.firmware_version
            frequency_channel=0,
            speed_setting=2,
            range_setting=0,
            threshold_offset_db=8,
            tracking_filter=1,
            min_zone_dist_pct=5,
            max_zone_dist_pct=95,
            min_zone_angle_deg=-15,
            max_zone_angle_deg=15,
            min_zone_speed_pct=2,
            max_zone_speed_pct=80,
            direction_filter=0,
        )
        radar.set_parameters(new_p)
        p2 = radar.get_parameters()
        self.assertEqual(p2.frequency_channel, 0)
        self.assertEqual(p2.min_zone_angle_deg, -15)
        self.assertEqual(p2.max_speed_kmh, 200.0)
        self.assertEqual(p2.max_range_meters, 100.0)

        # RFSE factory reset
        radar.restore_factory_settings()
        p3 = radar.get_parameters()
        self.assertEqual(p3.frequency_channel, 1)
        self.assertEqual(p3.threshold_offset_db, 12)
        self.assertEqual(p3.min_zone_angle_deg, -30)
        self.assertEqual(p3.max_zone_angle_deg, 30)

        # Disconnect (GBYE)
        radar.disconnect()
        cmds = [c[0] for c in ser.commands_log]
        for expected in [
            "INIT", "RBFR", "RSPI", "RRAI", "THOF", "TRFT",
            "MIRA", "MARA", "MIAN", "MAAN", "MISP", "MASP",
            "DEDI", "GRPS", "SRPS", "RFSE", "GBYE",
        ]:
            self.assertIn(expected, cmds)

    def test_gnfd_decodes_radc_rfft_pdat_tdat_and_resyncs(self):
        radar, ser = self._make_connected_radar(baudrate=3000000)
        # Inject noise bytes before next packet to test resynchronization
        ser._tx_to_host.extend(b"\xff\x00\xab\xcdNOISE")

        frame = radar.get_next_frame(raw_adc=True, raw_fft=True, raw_targets=True, tracked_targets=True)
        self.assertGreater(frame.frame_number, 10)
        self.assertGreater(frame.bytes_read, 6144 + 2048)

        # Verify RADC exact samples
        self.assertIsNotNone(frame.adc_data)
        self.assertEqual(len(frame.adc_data.if1_freq_a_i), 512)
        self.assertEqual(frame.adc_data.if1_freq_a_i[0], 1000)
        self.assertEqual(frame.adc_data.if1_freq_a_q[511], 2511)
        self.assertEqual(frame.adc_data.if2_freq_a_i[0], 3000)
        self.assertEqual(frame.adc_data.if2_freq_a_q[10], 4010)
        self.assertEqual(frame.adc_data.if1_freq_b_i[0], 5000)
        self.assertEqual(frame.adc_data.if1_freq_b_q[511], 6511)

        # Verify RFFT exact spectrum/threshold
        self.assertIsNotNone(frame.fft_data)
        self.assertEqual(len(frame.fft_data.spectrum_db), 512)
        self.assertAlmostEqual(frame.fft_data.spectrum_db[0], 40.00, places=2)
        self.assertAlmostEqual(frame.fft_data.spectrum_db[511], 45.11, places=2)
        self.assertAlmostEqual(frame.fft_data.threshold_db[0], 25.00, places=2)
        self.assertAlmostEqual(frame.fft_data.threshold_db[511], 30.11, places=2)

        # Verify PDAT & TDAT
        self.assertEqual(len(frame.raw_targets), 2)
        self.assertAlmostEqual(frame.raw_targets[0].distance, 2.50, places=2)
        self.assertAlmostEqual(frame.raw_targets[1].angle, -10.0, places=2)
        self.assertEqual(len(frame.tracked_targets), 1)
        self.assertEqual(frame.tracked_targets[0].tracking_id, 7)

    def test_stop_streaming_keeps_serial_open_and_drains_inflight(self):
        radar, ser = self._make_connected_radar()
        radar.start_streaming(raw_targets=True, tracked_targets=True)
        self.assertTrue(radar.is_streaming)
        self.assertEqual(radar.active_stream_mask, FLAG_PDAT | FLAG_TDAT | FLAG_DONE)

        # An unread frame was queued; stop_streaming sends RDOT(0) and drains until RESP
        radar.stop_streaming()
        self.assertFalse(radar.is_streaming)
        self.assertEqual(radar.active_stream_mask, 0)
        self.assertTrue(ser.is_open)

        # Config must still work while stopped!
        radar.set_threshold_offset(33)
        self.assertEqual(radar.get_parameters().threshold_offset_db, 33)

    def test_resp_errors_raise_kmd7_error(self):
        radar, ser = self._make_connected_radar()
        ser.forced_error = 3
        with self.assertRaises(KMD7Error) as ctx:
            radar.set_threshold_offset(10)
        self.assertIn("Invalid RPST version", str(ctx.exception))

    def test_connect_closes_serial_on_non_kmd7_exception(self):
        class FailingBaudSerial(MockKMD7Serial):
            def __init__(self, *args, **kwargs):
                self._fail_baud = 3000000
                super().__init__(*args, **kwargs)

            @property
            def baudrate(self) -> int:
                return self._baudrate

            @baudrate.setter
            def baudrate(self, value: int) -> None:
                if value == self._fail_baud:
                    raise serial.SerialException("Unsupported baud rate 3000000")
                self._baudrate = value

        ser = FailingBaudSerial()
        radar = KMD7Radar(port="/dev/mockFT230X", baudrate=115200, timeout=0.5)

        def _reopen(baudrate: int = 115200):
            ser.is_open = True
            ser.baudrate = baudrate
            return ser

        with patch.object(radar, "_open_serial", side_effect=_reopen):
            radar.connect()
            with self.assertRaises(serial.SerialException):
                radar.set_baudrate(3000000)

        self.assertFalse(ser.is_open)
        self.assertIsNone(radar.serial)
        self.assertEqual(ser.sensor_baudrate, 3000000)

        # Next connect should recover from sensor stuck at 3 Mbps now that serial was closed
        ser._fail_baud = None
        with patch.object(radar, "_open_serial", side_effect=_reopen):
            radar.connect(target_baudrate=115200)
        self.assertTrue(ser.is_open)
        self.assertEqual(ser.sensor_baudrate, 115200)


class TestRFBeamKMD7Schema(unittest.TestCase):
    """Schema & IDL dispatch tests against live remoterf_device_core.idl."""

    def _reopen_mock(self, baudrate: int = 115200) -> MockKMD7Serial:
        """Simulate opening a fresh serial handle against the same mock radar."""
        self.mock_ser.is_open = True
        self.mock_ser.baudrate = baudrate
        return self.mock_ser

    def setUp(self) -> None:
        self.mock_ser = MockKMD7Serial()
        self.dev = RFBeamKMD7Device(port="/dev/mockFT230X", baudrate=115200, mode="indoor")
        with patch.object(self.dev.radar, "_open_serial", side_effect=self._reopen_mock):
            self.dev.connect()
        self.schema = RFBeamKMD7Schema()
        self.schema.bind(self.dev)

    def tearDown(self) -> None:
        if self.mock_ser.is_open:
            self.dev.close()

    def test_idl_schema_and_capabilities(self):
        idl = self.schema.get_idl()
        self.assertEqual(idl["device_type"], "rfbeam_kmd7")
        self.assertEqual(idl["driver_version"], "0.2.0")
        self.assertTrue(idl["schema_hash"].startswith("sha256:"))
        json.loads(self.schema.get_idl_json(pretty=True))

        exposed = self.schema.list_exposed()
        for getter in (
            "get_is_running", "get_device_info", "get_firmware_version",
            "get_stream_stats", "get_stream_mask", "get_radar_parameters",
            "get_baudrate", "get_mode", "get_frequency_channel",
            "get_max_range", "get_max_speed", "get_threshold_offset",
            "get_tracking_filter", "get_min_zone_distance", "get_max_zone_distance",
            "get_min_zone_angle", "get_max_zone_angle", "get_min_speed_filter",
            "get_max_speed_filter", "get_direction_filter",
        ):
            self.assertIn(getter, exposed["getters"])

        for setter in (
            "set_baudrate", "set_mode", "set_frequency_channel",
            "set_max_range", "set_max_speed", "set_threshold_offset",
            "set_tracking_filter", "set_min_zone_distance", "set_max_zone_distance",
            "set_min_zone_angle", "set_max_zone_angle", "set_min_speed_filter",
            "set_max_speed_filter", "set_direction_filter",
        ):
            self.assertIn(setter, exposed["setters"])

        for call in (
            "call_capture_point_cloud", "call_capture_points", "call_capture_targets",
            "call_read_frame", "call_capture_next_frame", "call_capture_raw_adc",
            "call_capture_raw_fft", "call_capture_full_frame", "call_configure",
            "call_set_radar_parameters", "call_set_detection_zone",
            "call_restore_factory_settings", "call_flush_frames", "call_start", "call_stop",
        ):
            self.assertIn(call, exposed["calls"])

    def test_strict_validation_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            self.schema.dispatch("set_max_range", {"value": 150})
        with self.assertRaises(ValueError):
            self.schema.dispatch("set_max_speed", {"value": 75})
        with self.assertRaises(ValueError):
            self.schema.dispatch("call_capture_point_cloud", {"timeout": 0.01})
        with self.assertRaises(ValueError):
            self.schema.dispatch("call_capture_point_cloud", {"timeout": 120.0})
        with self.assertRaises(ValueError):
            self.schema.dispatch("call_set_detection_zone", {"min_dist_pct": 80, "max_dist_pct": 20})
        with self.assertRaises(ValueError):
            self.schema.dispatch("call_set_detection_zone", {"min_angle_deg": 10, "max_angle_deg": -5})
        with self.assertRaises(ValueError):
            self.schema.dispatch("call_set_radar_parameters", {"params": {"bogus_key": 1}})

    def test_getters_setters_zone_srps_and_baudrate_dispatch(self):
        # Change baudrate via schema (mock reopen reopens physical mock serial)
        with patch.object(self.dev.radar, "_open_serial", side_effect=self._reopen_mock):
            self.schema.dispatch("set_baudrate", {"value": 3000000})
        self.assertEqual(self.schema.dispatch("get_baudrate", {}), 3000000)

        self.schema.dispatch("set_frequency_channel", {"value": 2})
        self.assertEqual(self.schema.dispatch("get_frequency_channel", {}), 2)

        self.schema.dispatch("call_set_detection_zone", {
            "min_dist_pct": 10,
            "max_dist_pct": 90,
            "min_angle_deg": -20,
            "max_angle_deg": 25,
        })
        self.assertEqual(self.schema.dispatch("get_min_zone_distance", {}), 10)
        self.assertEqual(self.schema.dispatch("get_max_zone_distance", {}), 90)
        self.assertEqual(self.schema.dispatch("get_min_zone_angle", {}), -20)
        self.assertEqual(self.schema.dispatch("get_max_zone_angle", {}), 25)

        self.schema.dispatch("set_max_speed_filter", {"value": 77})
        self.assertEqual(self.schema.dispatch("get_max_speed_filter", {}), 77)

        updated = self.schema.dispatch("call_set_radar_parameters", {
            "params": {"frequency_channel": 0, "max_range": 300, "max_speed": 200, "threshold_offset": 18}
        })
        self.assertEqual(updated["frequency_channel"], 0)
        self.assertEqual(updated["max_range_meters"], 300.0)
        self.assertEqual(updated["max_speed_kmh"], 200.0)
        self.assertEqual(updated["threshold_offset_db"], 18)

    def test_capture_radc_rfft_full_and_handshake_while_streaming(self):
        self.schema.dispatch("call_start", {"raw_targets": True, "tracked_targets": True})
        self.assertTrue(self.schema.dispatch("get_is_running", {}))
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}), FLAG_PDAT | FLAG_TDAT | FLAG_DONE)

        # Calling capture_raw_adc uses GNFD handshake by default, pausing & restoring active stream
        adc = self.schema.dispatch("call_capture_raw_adc", {"timeout": 2.0})
        self.assertTrue(adc["valid"])
        self.assertEqual(adc["num_samples"], 512)
        self.assertEqual(adc["if1_freq_a_i"].dtype, np.uint16)
        self.assertEqual(adc["if1_freq_a_i"].shape, (512,))
        self.assertEqual(int(adc["if1_freq_a_i"][0]), 1000)
        self.assertEqual(int(adc["if1_freq_b_q"][511]), 6511)
        self.assertTrue(self.schema.dispatch("get_is_running", {}))
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}), FLAG_PDAT | FLAG_TDAT | FLAG_DONE)

        fft = self.schema.dispatch("call_capture_raw_fft", {"timeout": 2.0})
        self.assertTrue(fft["valid"])
        self.assertEqual(fft["spectrum_db"].dtype, np.float32)
        self.assertAlmostEqual(float(fft["spectrum_db"][0]), 40.0, places=2)
        self.assertAlmostEqual(float(fft["threshold_db"][511]), 30.11, places=2)

        full = self.schema.dispatch("call_capture_full_frame", {"timeout": 2.0})
        self.assertEqual(full["num_raw_points"], 2)
        self.assertEqual(full["num_tracked_targets"], 1)
        self.assertTrue(full["adc"]["valid"])
        self.assertTrue(full["fft"]["valid"])

        stats = self.schema.dispatch("get_stream_stats", {})
        self.assertGreater(stats["total_bytes_read"], 0)
        self.assertEqual(stats["total_frames"], 3)

        # call_stop keeps connection alive for config queries
        self.assertTrue(self.schema.dispatch("call_stop", {}))
        self.assertFalse(self.schema.dispatch("get_is_running", {}))
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}), 0)
        self.schema.dispatch("set_threshold_offset", {"value": 9})
        self.assertEqual(self.schema.dispatch("get_threshold_offset", {}), 9)

    def test_restart_and_capture_after_stop_and_after_disconnect(self):
        self.assertTrue(self.schema.dispatch("call_start", {}))
        self.assertTrue(self.schema.dispatch("call_stop", {}))
        self.assertTrue(self.mock_ser.is_open)
        self.assertFalse(self.schema.dispatch("get_is_running", {}))

        # Restarting and capturing after stop() reuses the live connection
        self.assertTrue(self.schema.dispatch("call_start", {}))
        cloud = self.schema.dispatch("call_capture_point_cloud", {"timeout": 1.0})
        self.assertEqual(cloud["num_raw_points"], 2)
        self.assertEqual(cloud["num_tracked_targets"], 1)

        # If serial was closed, call_start() reconnects cleanly
        self.dev.radar.disconnect()
        self.assertFalse(self.mock_ser.is_open)
        with patch.object(self.dev.radar, "_open_serial", side_effect=self._reopen_mock):
            self.assertTrue(self.schema.dispatch("call_start", {}))
        self.assertTrue(self.mock_ser.is_open)
        self.assertTrue(self.schema.dispatch("get_is_running", {}))

        # If serial was closed again, capture_point_cloud() auto-reconnects
        self.dev.radar.disconnect()
        self.assertFalse(self.mock_ser.is_open)
        with patch.object(self.dev.radar, "_open_serial", side_effect=self._reopen_mock):
            cloud2 = self.schema.dispatch("call_capture_point_cloud", {"timeout": 1.0})
        self.assertTrue(self.mock_ser.is_open)
        self.assertEqual(cloud2["num_raw_points"], 2)

    def test_reset_for_next_holder_restores_defaults_and_stops_stream(self):
        with patch.object(self.dev.radar, "_open_serial", side_effect=self._reopen_mock):
            self.schema.dispatch("set_baudrate", {"value": 2000000})
        self.schema.dispatch("call_start", {"bitmask": FLAG_ALL})
        self.schema.dispatch("call_configure", {
            "mode": "traffic",
            "frequency_channel": 2,
            "threshold_offset": 45,
            "min_zone_angle": -5,
            "max_zone_angle": 5,
        })
        self.schema.dispatch("call_capture_point_cloud", {"timeout": 1.0})

        with patch.object(self.dev.radar, "_open_serial", side_effect=self._reopen_mock):
            self.schema.reset_for_next_holder()

        self.assertFalse(self.schema.dispatch("get_is_running", {}))
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}), 0)
        self.assertEqual(self.schema.dispatch("get_baudrate", {}), 115200)
        self.assertEqual(self.schema.dispatch("get_mode", {}), "indoor")
        self.assertEqual(self.schema.dispatch("get_frequency_channel", {}), 1)
        self.assertEqual(self.schema.dispatch("get_threshold_offset", {}), 12)
        self.assertEqual(self.schema.dispatch("get_min_zone_angle", {}), -30)
        self.assertEqual(self.schema.dispatch("get_max_zone_angle", {}), 30)
        self.assertEqual(self.schema.dispatch("get_stream_stats", {})["total_frames"], 0)

    def test_reset_for_next_holder_reconnects_when_closed_and_noop_when_unbound(self):
        unbound = RFBeamKMD7Schema()
        unbound.reset_for_next_holder()

        self.schema.dispatch("set_threshold_offset", {"value": 42})
        self.dev.radar.disconnect()
        self.assertFalse(self.mock_ser.is_open)

        with patch.object(self.dev.radar, "_open_serial", side_effect=self._reopen_mock):
            self.schema.reset_for_next_holder()

        self.assertTrue(self.mock_ser.is_open)
        self.assertFalse(self.schema.dispatch("get_is_running", {}))
        self.assertEqual(self.schema.dispatch("get_threshold_offset", {}), 12)
        self.assertEqual(self.schema.dispatch("get_mode", {}), "indoor")

    def test_streaming_capture_enables_missing_flags_with_use_handshake_false(self):
        # Default start() sets PDAT|TDAT|DONE (0x2C); full frame with use_handshake=False must add RADC|RFFT
        self.schema.dispatch("call_start", {})
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}), FLAG_PDAT | FLAG_TDAT | FLAG_DONE)

        full = self.schema.dispatch("call_capture_full_frame", {"timeout": 2.0, "use_handshake": False})
        self.assertIsNotNone(full["adc"])
        self.assertTrue(full["adc"]["valid"])
        self.assertIsNotNone(full["fft"])
        self.assertTrue(full["fft"]["valid"])
        self.assertEqual(full["num_raw_points"], 2)
        self.assertEqual(full["num_tracked_targets"], 1)
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}), FLAG_ALL)

        # Start from stopped with RADC-only stream (0x21), then request targets/points/point_cloud
        self.schema.dispatch("call_stop", {})
        adc = self.schema.dispatch("call_capture_raw_adc", {"timeout": 2.0, "use_handshake": False})
        self.assertTrue(adc["valid"])
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}), FLAG_RADC | FLAG_DONE)

        targets = self.schema.dispatch("call_capture_targets", {"timeout": 1.0})
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["id"], 7)
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}) & FLAG_TDAT, FLAG_TDAT)

        # Stream has TDAT|DONE (missing PDAT); capture_points(raw+tracked) must also enable PDAT
        pts = self.schema.dispatch("call_capture_points", {"timeout": 1.0, "include_raw": True, "include_tracked": True})
        self.assertEqual(pts.shape, (3, 6))
        self.assertEqual(
            self.schema.dispatch("get_stream_mask", {}) & (FLAG_PDAT | FLAG_TDAT),
            FLAG_PDAT | FLAG_TDAT,
        )

        # Start with RFFT-only stream (0x22), then request point_cloud
        self.schema.dispatch("call_stop", {})
        fft = self.schema.dispatch("call_capture_raw_fft", {"timeout": 2.0, "use_handshake": False})
        self.assertTrue(fft["valid"])
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}), FLAG_RFFT | FLAG_DONE)

        cloud = self.schema.dispatch("call_capture_point_cloud", {"timeout": 1.0})
        self.assertEqual(cloud["num_raw_points"], 2)
        self.assertEqual(cloud["num_tracked_targets"], 1)
        self.assertEqual(
            self.schema.dispatch("get_stream_mask", {}) & (FLAG_PDAT | FLAG_TDAT),
            FLAG_PDAT | FLAG_TDAT,
        )

    def test_reset_for_next_holder_preserves_make_device_overrides(self):
        ser = MockKMD7Serial()

        def _reopen(baudrate: int = 115200):
            ser.is_open = True
            ser.baudrate = baudrate
            return ser

        with patch.object(KMD7Radar, "_open_serial", side_effect=_reopen):
            dev = RFBeamKMD7Schema.make_device(
                port="/dev/mockFT230X",
                baudrate=921600,
                mode="indoor",
                frequency_channel=2,
                max_range=300,
                max_speed=200,
                threshold_offset=35,
                auto_start=False,
            )
            self.assertIsNotNone(dev)
            schema = RFBeamKMD7Schema()
            schema.bind(dev)

            self.assertEqual(schema.dispatch("get_frequency_channel", {}), 2)
            self.assertEqual(schema.dispatch("get_max_range", {}), 300.0)
            self.assertEqual(schema.dispatch("get_max_speed", {}), 200.0)
            self.assertEqual(schema.dispatch("get_threshold_offset", {}), 35)

            # Holder modifies settings and baudrate
            schema.dispatch("set_baudrate", {"value": 3000000})
            schema.dispatch("call_configure", {
                "mode": "traffic",
                "frequency_channel": 0,
                "max_range": 100,
                "max_speed": 50,
                "threshold_offset": 5,
                "min_zone_angle": -10,
                "max_zone_angle": 10,
            })
            schema.dispatch("call_start", {})

            schema.reset_for_next_holder()

            self.assertFalse(schema.dispatch("get_is_running", {}))
            self.assertEqual(schema.dispatch("get_baudrate", {}), 921600)
            self.assertEqual(schema.dispatch("get_mode", {}), "indoor")
            self.assertEqual(schema.dispatch("get_frequency_channel", {}), 2)
            self.assertEqual(schema.dispatch("get_max_range", {}), 300.0)
            self.assertEqual(schema.dispatch("get_max_speed", {}), 200.0)
            self.assertEqual(schema.dispatch("get_threshold_offset", {}), 35)
            self.assertEqual(schema.dispatch("get_tracking_filter", {}), 1)
            self.assertEqual(schema.dispatch("get_min_speed_filter", {}), 0)
            self.assertEqual(schema.dispatch("get_min_zone_angle", {}), -30)
            self.assertEqual(schema.dispatch("get_max_zone_angle", {}), 30)
            dev.close()

    def test_reset_for_next_holder_propagates_errors_closes_serial_and_zeros_bytes(self):
        # Clean reset must zero total_bytes_read as well as frame counters
        self.schema.dispatch("call_start", {})
        self.schema.dispatch("call_capture_point_cloud", {"timeout": 1.0})
        self.assertGreater(self.schema.dispatch("get_stream_stats", {})["total_bytes_read"], 0)

        self.schema.reset_for_next_holder()
        stats = self.schema.dispatch("get_stream_stats", {})
        self.assertEqual(stats["total_bytes_read"], 0)
        self.assertEqual(stats["total_frames"], 0)

        # Failed reset must raise, close serial, clear cached params, and zero counters
        self.schema.dispatch("set_threshold_offset", {"value": 50})
        self.schema.dispatch("set_frequency_channel", {"value": 2})
        self.mock_ser.forced_error = 4

        with self.assertRaises(KMD7Error):
            self.schema.reset_for_next_holder()

        self.assertFalse(self.mock_ser.is_open)
        self.assertIsNone(self.dev.radar.serial)
        self.assertIsNone(self.dev._params)
        self.assertEqual(self.schema.dispatch("get_stream_stats", {})["total_bytes_read"], 0)

        # Subsequent reset reconnects cleanly and restores defaults
        with patch.object(self.dev.radar, "_open_serial", side_effect=self._reopen_mock):
            self.schema.reset_for_next_holder()
        self.assertTrue(self.mock_ser.is_open)
        self.assertEqual(self.schema.dispatch("get_threshold_offset", {}), 12)
        self.assertEqual(self.schema.dispatch("get_frequency_channel", {}), 1)

    def test_failed_reset_recovers_baseline_and_next_holder_commands_succeed(self):
        ser = MockKMD7Serial()

        def _reopen(baudrate: int = 115200):
            ser.is_open = True
            ser.baudrate = baudrate
            return ser

        with patch.object(KMD7Radar, "_open_serial", side_effect=_reopen):
            dev = RFBeamKMD7Schema.make_device(
                port="/dev/mockFT230X",
                baudrate=921600,
                mode="indoor",
                frequency_channel=2,
                max_range=300,
                max_speed=200,
                threshold_offset=35,
                auto_start=False,
            )
            self.assertIsNotNone(dev)
            schema = RFBeamKMD7Schema()
            schema.bind(dev)

            # Holder mutates baudrate, mode, parameters, and starts streaming
            schema.dispatch("set_baudrate", {"value": 3000000})
            schema.dispatch("call_configure", {
                "mode": "traffic",
                "frequency_channel": 0,
                "max_range": 100,
                "max_speed": 50,
                "threshold_offset": 5,
            })
            schema.dispatch("call_start", {})

            # Transient error during reset: recovery reconnects, restores baseline,
            # logs the recovered error, and returns without failing the next holder.
            ser.forced_error = 4
            with self.assertLogs("remoterf_device_core.drivers.rfbeam_kmd7.rfbeam_kmd7_schema", level="WARNING") as logs:
                schema.reset_for_next_holder()
            self.assertTrue(any("recovered" in msg.lower() for msg in logs.output))

            self.assertTrue(ser.is_open)
            self.assertFalse(schema.dispatch("get_is_running", {}))
            self.assertEqual(schema.dispatch("get_baudrate", {}), 921600)
            self.assertEqual(schema.dispatch("get_mode", {}), "indoor")
            self.assertEqual(schema.dispatch("get_frequency_channel", {}), 2)
            self.assertEqual(schema.dispatch("get_max_range", {}), 300.0)
            self.assertEqual(schema.dispatch("get_max_speed", {}), 200.0)
            self.assertEqual(schema.dispatch("get_threshold_offset", {}), 35)
            self.assertEqual(schema.dispatch("get_stream_stats", {})["total_bytes_read"], 0)

            # Next holder's setter and getter succeed without needing start() or capture
            schema.dispatch("set_threshold_offset", {"value": 22})
            self.assertEqual(schema.dispatch("get_threshold_offset", {}), 22)
            params = schema.dispatch("get_radar_parameters", {})
            self.assertEqual(params["threshold_offset_db"], 22)
            dev.close()

    def test_failed_reset_with_failed_reconnect_then_lazy_reconnect_on_commands(self):
        with patch.object(self.dev.radar, "_open_serial", side_effect=self._reopen_mock):
            self.schema.dispatch("set_baudrate", {"value": 2000000})
        self.schema.dispatch("set_threshold_offset", {"value": 50})
        self.schema.dispatch("set_frequency_channel", {"value": 2})

        # Both the initial reset and the failure-path reconnect fail; original error re-raises
        self.mock_ser.forced_error = 4
        with patch.object(
            self.dev.radar,
            "_open_serial",
            side_effect=serial.SerialException("Port temporarily unavailable"),
        ):
            with self.assertRaises(KMD7Error):
                self.schema.reset_for_next_holder()

        self.assertFalse(self.mock_ser.is_open)
        self.assertIsNone(self.dev.radar.serial)
        self.assertIsNone(self.dev._params)
        self.assertEqual(self.dev.baudrate, 115200)

        # Once port is available again, a getter reconnects lazily and succeeds
        with patch.object(self.dev.radar, "_open_serial", side_effect=self._reopen_mock):
            self.assertEqual(self.schema.dispatch("get_threshold_offset", {}), 50)
            self.assertTrue(self.mock_ser.is_open)
            self.assertEqual(self.schema.dispatch("get_baudrate", {}), 115200)

            # Verify lazy reconnect on setters, get/set_radar_parameters, detection zone, RFSE
            self.dev.radar.disconnect()
            self.schema.dispatch("set_frequency_channel", {"value": 0})
            self.assertTrue(self.mock_ser.is_open)
            self.assertEqual(self.schema.dispatch("get_frequency_channel", {}), 0)

            self.dev.radar.disconnect()
            params = self.schema.dispatch("get_radar_parameters", {})
            self.assertTrue(self.mock_ser.is_open)
            self.assertEqual(params["frequency_channel"], 0)

            self.dev.radar.disconnect()
            updated = self.schema.dispatch(
                "call_set_radar_parameters",
                {"params": {"threshold_offset_db": 18}},
            )
            self.assertTrue(self.mock_ser.is_open)
            self.assertEqual(updated["threshold_offset_db"], 18)

            self.dev.radar.disconnect()
            self.schema.dispatch("set_baudrate", {"value": 921600})
            self.assertTrue(self.mock_ser.is_open)
            self.assertEqual(self.schema.dispatch("get_baudrate", {}), 921600)

            self.dev.radar.disconnect()
            zone = self.schema.dispatch(
                "call_set_detection_zone",
                {"min_dist_pct": 10, "max_dist_pct": 80},
            )
            self.assertTrue(self.mock_ser.is_open)
            self.assertEqual(zone["min_zone_dist_pct"], 10)
            self.assertEqual(zone["max_zone_dist_pct"], 80)

            self.dev.radar.disconnect()
            self.assertTrue(self.schema.dispatch("call_restore_factory_settings", {}))
            self.assertTrue(self.mock_ser.is_open)
            self.assertEqual(self.schema.dispatch("get_threshold_offset", {}), 12)

    def test_set_radar_parameters_round_trip_and_alias_conflict_rejection(self):
        params = self.schema.dispatch("get_radar_parameters", {})
        self.assertIn("mode", params)
        params["threshold_offset_db"] = 20
        updated = self.schema.dispatch("call_set_radar_parameters", {"params": params})
        self.assertEqual(updated["threshold_offset_db"], 20)

        # Device is currently at indoor defaults: range_setting=0 (100 m), speed_setting=0 (50 km/h).
        # Conflicting aliases where both differ from the current value AND from each other raise ValueError.
        params_conflict = self.schema.dispatch("get_radar_parameters", {})
        params_conflict["range_setting_code"] = 1
        params_conflict["max_range_meters"] = 300
        with self.assertRaises(ValueError):
            self.schema.dispatch("call_set_radar_parameters", {"params": params_conflict})

        with self.assertRaises(ValueError):
            self.schema.dispatch("call_set_radar_parameters", {"params": {"range_setting": 2, "max_range": 200}})

        with self.assertRaises(ValueError):
            self.schema.dispatch(
                "call_set_radar_parameters",
                {"params": {"speed_setting_code": 2, "max_speed_kmh": 100.0}},
            )

        # Matching aliases for the same canonical field are accepted
        consistent = self.schema.dispatch(
            "call_set_radar_parameters",
            {"params": {"range_setting": 2, "max_range": 300, "speed_setting_code": 2, "max_speed_kmh": 200}},
        )
        self.assertEqual(consistent["range_setting_code"], 2)
        self.assertEqual(consistent["max_range_meters"], 300.0)
        self.assertEqual(consistent["speed_setting_code"], 2)
        self.assertEqual(consistent["max_speed_kmh"], 200.0)

    def test_set_radar_parameters_round_trip_range_and_speed(self):
        # Round-trip editing only max_range_meters while range_setting_code stays at current value
        params = self.schema.dispatch("get_radar_parameters", {})
        self.assertEqual(params["range_setting_code"], 0)
        self.assertEqual(params["max_range_meters"], 100.0)
        params["max_range_meters"] = 300
        updated = self.schema.dispatch("call_set_radar_parameters", {"params": params})
        self.assertEqual(updated["range_setting_code"], 2)
        self.assertEqual(updated["max_range_meters"], 300.0)

        # Round-trip editing only range_setting_code while max_range_meters stays at current value
        params = self.schema.dispatch("get_radar_parameters", {})
        params["range_setting_code"] = 1
        updated = self.schema.dispatch("call_set_radar_parameters", {"params": params})
        self.assertEqual(updated["range_setting_code"], 1)
        self.assertEqual(updated["max_range_meters"], 200.0)

        # Round-trip editing only max_speed_kmh while speed_setting_code stays at current value
        params = self.schema.dispatch("get_radar_parameters", {})
        self.assertEqual(params["speed_setting_code"], 0)
        self.assertEqual(params["max_speed_kmh"], 50.0)
        params["max_speed_kmh"] = 200
        updated = self.schema.dispatch("call_set_radar_parameters", {"params": params})
        self.assertEqual(updated["speed_setting_code"], 2)
        self.assertEqual(updated["max_speed_kmh"], 200.0)

        # Round-trip editing only speed_setting_code while max_speed_kmh stays at current value
        params = self.schema.dispatch("get_radar_parameters", {})
        params["speed_setting_code"] = 1
        updated = self.schema.dispatch("call_set_radar_parameters", {"params": params})
        self.assertEqual(updated["speed_setting_code"], 1)
        self.assertEqual(updated["max_speed_kmh"], 100.0)

    def test_make_device_closes_serial_on_post_connect_error(self):
        ser = MockKMD7Serial()

        def _reopen(baudrate: int = 115200):
            ser.is_open = True
            ser.baudrate = baudrate
            return ser

        with patch.object(KMD7Radar, "_open_serial", side_effect=_reopen):
            dev = RFBeamKMD7Schema.make_device(
                port="/dev/mockFT230X",
                baudrate=2000000,
                mode="indoor",
                max_range=150,
            )

        self.assertIsNone(dev)
        self.assertFalse(ser.is_open)
        self.assertEqual(ser.sensor_baudrate, 115200)
        self.assertIn("GBYE", [c[0] for c in ser.commands_log])

    def test_reset_for_next_holder_orders_baudrate_and_rfse_before_config(self):
        class StrictHardwareSerial(MockKMD7Serial):
            """Simulates hardware where RFSE drops baud to 115200 and GBYE clears volatile params."""

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.mismatched_writes: list[tuple[int, int, bytes]] = []
                self.command_bauds: list[tuple[str, int]] = []

            def write(self, data: bytes) -> int:
                if self.baudrate != self.sensor_baudrate:
                    self.mismatched_writes.append((self.baudrate, self.sensor_baudrate, data))
                return super().write(data)

            def _handle_command(self, hdr: str, payload: bytes) -> None:
                self.command_bauds.append((hdr, self.baudrate))
                super()._handle_command(hdr, payload)
                if hdr == "RFSE":
                    self.sensor_baudrate = 115200
                elif hdr == "GBYE":
                    self.params = self._factory_params()

        for init_baud in (115200, 921600):
            with self.subTest(init_baud=init_baud):
                ser = StrictHardwareSerial()

                def _reopen(baudrate: int = 115200):
                    ser.is_open = True
                    ser.baudrate = baudrate
                    return ser

                with patch.object(KMD7Radar, "_open_serial", side_effect=_reopen):
                    dev = RFBeamKMD7Schema.make_device(
                        port="/dev/mockFT230X",
                        baudrate=init_baud,
                        mode="indoor",
                        frequency_channel=2,
                        max_range=300,
                        max_speed=200,
                        threshold_offset=35,
                        auto_start=False,
                    )
                    self.assertIsNotNone(dev)
                    schema = RFBeamKMD7Schema()
                    schema.bind(dev)

                    # Holder switches to 3 Mbps, mutates settings, and starts streaming
                    schema.dispatch("set_baudrate", {"value": 3000000})
                    schema.dispatch("call_configure", {
                        "mode": "traffic",
                        "frequency_channel": 0,
                        "max_range": 100,
                        "max_speed": 50,
                        "threshold_offset": 5,
                    })
                    schema.dispatch("call_start", {})

                    ser.mismatched_writes.clear()
                    ser.command_bauds.clear()

                    schema.reset_for_next_holder()

                    self.assertEqual(ser.mismatched_writes, [])
                    rfse_bauds = [b for cmd, b in ser.command_bauds if cmd == "RFSE"]
                    self.assertEqual(rfse_bauds, [115200])
                    cmd_names = [cmd for cmd, _ in ser.command_bauds]
                    last_init_idx = max(i for i, cmd in enumerate(cmd_names) if cmd == "INIT")
                    first_override_idx = min(
                        i for i, cmd in enumerate(cmd_names) if cmd in ("RBFR", "THOF", "TRFT")
                    )
                    self.assertLess(last_init_idx, first_override_idx)

                    self.assertFalse(schema.dispatch("get_is_running", {}))
                    self.assertEqual(schema.dispatch("get_baudrate", {}), init_baud)
                    self.assertEqual(schema.dispatch("get_mode", {}), "indoor")
                    self.assertEqual(schema.dispatch("get_frequency_channel", {}), 2)
                    self.assertEqual(schema.dispatch("get_max_range", {}), 300.0)
                    self.assertEqual(schema.dispatch("get_max_speed", {}), 200.0)
                    self.assertEqual(schema.dispatch("get_threshold_offset", {}), 35)
                    self.assertEqual(schema.dispatch("get_tracking_filter", {}), 1)
                    self.assertEqual(schema.dispatch("get_min_speed_filter", {}), 0)
                    dev.close()

    def test_streaming_switch_flushes_stale_packets_and_drops_unused_flags(self):
        class InFlightStreamSerial(MockKMD7Serial):
            """Models in-flight stale frames arriving after RDOT(0) and RDOT(new_mask) RESP."""

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.flush_count = 0
                self._last_nonzero_mask = 0
                self.stop_stale_frames_queued = 0
                self.switch_stale_frames_queued = 0
                self.last_stale_frame_number = -1
                self.last_fresh_frame_number = -1

            def reset_input_buffer(self) -> None:
                self.flush_count += 1
                super().reset_input_buffer()

            def _handle_command(self, hdr: str, payload: bytes) -> None:
                if hdr == "RDOT":
                    old_mask = self.stream_mask
                    new_mask = payload[0]
                    self.stream_mask = new_mask
                    self._enqueue_resp(0)
                    if old_mask and new_mask == 0:
                        # An in-flight frame from old_mask finishes right after RDOT(0)'s RESP
                        self._queue_frame(old_mask)
                        self.stop_stale_frames_queued += 1
                    elif new_mask != 0:
                        if self._last_nonzero_mask and self._last_nonzero_mask != new_mask:
                            # A late frame from the previous active mask arrives after RDOT(new_mask)'s RESP
                            self._queue_frame(self._last_nonzero_mask)
                            self.switch_stale_frames_queued += 1
                            self.last_stale_frame_number = self.frame_counter
                        self._last_nonzero_mask = new_mask
                        self._queue_frame(new_mask)
                        self.last_fresh_frame_number = self.frame_counter
                    return
                super()._handle_command(hdr, payload)

        ser = InFlightStreamSerial()
        dev = RFBeamKMD7Device(port="/dev/mockFT230X", baudrate=115200, mode="indoor")

        def _reopen(baudrate: int = 115200):
            ser.is_open = True
            ser.baudrate = baudrate
            return ser

        with patch.object(dev.radar, "_open_serial", side_effect=_reopen):
            dev.connect()
        schema = RFBeamKMD7Schema()
        schema.bind(dev)

        try:
            # Start with PDAT|TDAT|DONE, then stop: post-RESP flush must drain the in-flight frame
            schema.dispatch("call_start", {})
            self.assertEqual(schema.dispatch("get_stream_mask", {}), FLAG_PDAT | FLAG_TDAT | FLAG_DONE)
            schema.dispatch("call_stop", {})
            self.assertEqual(ser.stop_stale_frames_queued, 1)
            self.assertEqual(bytes(dev.radar._rx_buf), b"")
            self.assertEqual(bytes(ser._tx_to_host), b"")
            self.assertIsNone(dev.radar.read_frame(timeout=0.05))

            # Restart streaming and verify flush() clears stale frames in both _rx_buf and serial buffer
            schema.dispatch("call_start", {})
            self.assertEqual(schema.dispatch("get_stream_mask", {}), FLAG_PDAT | FLAG_TDAT | FLAG_DONE)
            stale_rx_frame = (
                _pack_packet("PDAT", _build_test_pdat_payload())
                + _pack_packet("TDAT", _build_test_tdat_payload())
                + _pack_packet("DONE", struct.pack("<I", 9999))
            )
            stale_tx_frame = (
                _pack_packet("PDAT", _build_test_pdat_payload())
                + _pack_packet("TDAT", _build_test_tdat_payload())
                + _pack_packet("DONE", struct.pack("<I", 9998))
            )
            dev.radar._rx_buf.extend(stale_rx_frame)
            ser._tx_to_host.extend(stale_tx_frame)
            schema.dispatch("call_flush_frames", {})
            self.assertEqual(bytes(dev.radar._rx_buf), b"")
            self.assertEqual(bytes(ser._tx_to_host), b"")
            flushed_cloud = schema.dispatch("call_capture_point_cloud", {"timeout": 1.0})
            self.assertNotIn(flushed_cloud["frame_number"], (9998, 9999))
            flushes_before = ser.flush_count

            # Switching to RADC with use_handshake=False must flush, drop stale PDAT|TDAT frame,
            # and return the fresh RADC|DONE frame
            adc = schema.dispatch("call_capture_raw_adc", {"timeout": 2.0, "use_handshake": False})
            self.assertTrue(adc["valid"])
            self.assertEqual(adc["num_samples"], RADC_NUM_SAMPLES)
            self.assertEqual(adc["frame_number"], ser.last_fresh_frame_number)
            self.assertNotEqual(adc["frame_number"], ser.last_stale_frame_number)
            self.assertGreater(ser.flush_count, flushes_before)
            self.assertEqual(schema.dispatch("get_stream_mask", {}), FLAG_RADC | FLAG_DONE)

            # Switching back to point cloud must disable RADC and skip stale RADC|DONE frame
            cloud = schema.dispatch("call_capture_point_cloud", {"timeout": 1.0})
            self.assertEqual(cloud["frame_number"], ser.last_fresh_frame_number)
            self.assertNotEqual(cloud["frame_number"], ser.last_stale_frame_number)
            self.assertEqual(cloud["num_raw_points"], 2)
            self.assertEqual(cloud["num_tracked_targets"], 1)
            self.assertEqual(schema.dispatch("get_stream_mask", {}), FLAG_PDAT | FLAG_TDAT | FLAG_DONE)
            self.assertEqual(schema.dispatch("get_stream_mask", {}) & FLAG_RADC, 0)

            # Full frame (FLAG_ALL) followed by capture_targets must shrink mask to TDAT|DONE
            # and reject stale frames on both switches
            full = schema.dispatch("call_capture_full_frame", {"timeout": 2.0, "use_handshake": False})
            self.assertEqual(full["frame_number"], ser.last_fresh_frame_number)
            self.assertTrue(full["adc"]["valid"])
            self.assertTrue(full["fft"]["valid"])
            self.assertEqual(schema.dispatch("get_stream_mask", {}), FLAG_ALL)

            targets = schema.dispatch("call_capture_targets", {"timeout": 1.0})
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0]["id"], 7)
            self.assertEqual(schema.dispatch("get_stream_mask", {}), FLAG_TDAT | FLAG_DONE)
            self.assertEqual(schema.dispatch("get_stream_mask", {}) & (FLAG_RADC | FLAG_RFFT), 0)

            self.assertGreaterEqual(ser.stop_stale_frames_queued, 5)
            self.assertGreaterEqual(ser.switch_stale_frames_queued, 4)
        finally:
            dev.close()

    def test_zero_required_flag_switches_stream_off_radc_and_returns_empty(self):
        # Start with FLAG_ALL active
        self.schema.dispatch("call_start", {"bitmask": FLAG_ALL})
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}), FLAG_ALL)

        # call_read_frame (required_flag=None) preserves the active stream mask
        summary = self.schema.dispatch("call_read_frame", {"timeout": 1.0})
        self.assertTrue(summary["has_adc"])
        self.assertTrue(summary["has_fft"])
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}), FLAG_ALL)

        # call_capture_full_frame with all flags False must switch mask to FLAG_DONE and return no data
        empty_full = self.schema.dispatch(
            "call_capture_full_frame",
            {
                "timeout": 1.0,
                "raw_adc": False,
                "raw_fft": False,
                "raw_targets": False,
                "tracked_targets": False,
                "use_handshake": False,
            },
        )
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}), FLAG_DONE)
        self.assertIsNone(empty_full["adc"])
        self.assertIsNone(empty_full["fft"])
        self.assertEqual(empty_full["num_raw_points"], 0)
        self.assertEqual(empty_full["num_tracked_targets"], 0)
        self.assertGreater(empty_full["frame_number"], 0)

        # call_capture_points with include_raw=False, include_tracked=False must also disable RADC
        self.schema.dispatch("call_start", {"bitmask": FLAG_RADC | FLAG_DONE})
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}), FLAG_RADC | FLAG_DONE)
        pts = self.schema.dispatch(
            "call_capture_points",
            {"timeout": 1.0, "include_raw": False, "include_tracked": False},
        )
        self.assertEqual(pts.shape, (0, 6))
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}), FLAG_DONE)
        self.assertEqual(self.schema.dispatch("get_stream_mask", {}) & FLAG_RADC, 0)

    def test_stale_frame_filter_rejects_mismatched_pdat_tdat_packets(self):
        class StaleTargetMaskSerial(MockKMD7Serial):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.pending_stale_mask: int | None = None
                self.last_stale_frame_number = -1
                self.last_fresh_frame_number = -1

            def _handle_command(self, hdr: str, payload: bytes) -> None:
                if hdr == "RDOT":
                    new_mask = payload[0]
                    self.stream_mask = new_mask
                    self._enqueue_resp(0)
                    if new_mask != 0:
                        if self.pending_stale_mask is not None:
                            self._queue_frame(self.pending_stale_mask)
                            self.last_stale_frame_number = self.frame_counter
                            self.pending_stale_mask = None
                        self._queue_frame(new_mask)
                        self.last_fresh_frame_number = self.frame_counter
                    return
                super()._handle_command(hdr, payload)

        ser = StaleTargetMaskSerial()
        dev = RFBeamKMD7Device(port="/dev/mockFT230X", baudrate=115200, mode="indoor")

        def _reopen(baudrate: int = 115200):
            ser.is_open = True
            ser.baudrate = baudrate
            return ser

        with patch.object(dev.radar, "_open_serial", side_effect=_reopen):
            dev.connect()
        schema = RFBeamKMD7Schema()
        schema.bind(dev)

        try:
            # Start with PDAT|DONE, then switch to capture_targets (TDAT|DONE) with a stale PDAT|DONE frame
            pts = schema.dispatch(
                "call_capture_points",
                {"timeout": 1.0, "include_raw": True, "include_tracked": False},
            )
            self.assertEqual(pts.shape, (2, 6))
            self.assertEqual(schema.dispatch("get_stream_mask", {}), FLAG_PDAT | FLAG_DONE)

            ser.pending_stale_mask = FLAG_PDAT | FLAG_DONE
            targets = schema.dispatch("call_capture_targets", {"timeout": 1.0})
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0]["id"], 7)
            self.assertEqual(schema.dispatch("get_stream_mask", {}), FLAG_TDAT | FLAG_DONE)

            # Start with FLAG_ALL, then switch to RADC|RFFT|DONE with a stale FLAG_ALL frame
            schema.dispatch("call_start", {"bitmask": FLAG_ALL})
            ser.pending_stale_mask = FLAG_ALL
            full = schema.dispatch(
                "call_capture_full_frame",
                {
                    "timeout": 2.0,
                    "raw_adc": True,
                    "raw_fft": True,
                    "raw_targets": False,
                    "tracked_targets": False,
                    "use_handshake": False,
                },
            )
            self.assertEqual(full["frame_number"], ser.last_fresh_frame_number)
            self.assertNotEqual(full["frame_number"], ser.last_stale_frame_number)
            self.assertTrue(full["adc"]["valid"])
            self.assertTrue(full["fft"]["valid"])
            self.assertEqual(full["num_raw_points"], 0)
            self.assertEqual(full["num_tracked_targets"], 0)

            # call_read_frame on FLAG_ALL also rejects a stale subset frame (PDAT|DONE)
            ser.pending_stale_mask = FLAG_PDAT | FLAG_DONE
            schema.dispatch("call_start", {"bitmask": FLAG_ALL})
            summary = schema.dispatch("call_read_frame", {"timeout": 1.0})
            self.assertEqual(summary["frame_number"], ser.last_fresh_frame_number)
            self.assertTrue(summary["has_adc"])
            self.assertTrue(summary["has_fft"])

            # Empty 0-byte PDAT and TDAT payloads still carry their headers and are accepted
            schema.dispatch("call_start", {"raw_targets": True, "tracked_targets": True})
            dev.radar.flush()
            ser._tx_to_host.extend(
                _pack_packet("PDAT", b"")
                + _pack_packet("TDAT", b"")
                + _pack_packet("DONE", struct.pack("<I", 555))
            )
            empty_scene = schema.dispatch("call_capture_point_cloud", {"timeout": 1.0})
            self.assertEqual(empty_scene["frame_number"], 555)
            self.assertEqual(empty_scene["num_raw_points"], 0)
            self.assertEqual(empty_scene["num_tracked_targets"], 0)

            # Firmware that omits empty PDAT/TDAT packets entirely: a DONE-only frame is still accepted
            dev.radar.flush()
            ser._tx_to_host.extend(_pack_packet("DONE", struct.pack("<I", 556)))
            no_headers = schema.dispatch("call_capture_point_cloud", {"timeout": 1.0})
            self.assertEqual(no_headers["frame_number"], 556)
            self.assertEqual(no_headers["num_raw_points"], 0)
        finally:
            dev.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
