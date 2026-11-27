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

"""
RemoteRF Device Schema for RFbeam K-MD7 (and K-MD7-EVAL) 24 GHz Digital Radar Transceiver.

Follows the RemoteRF Device Specification and exposes the full application
command/message set from the K-MD7 Datasheet (Rev. D, Tables 10-12), excluding
bootloader/flash commands (JBTL, WMEM):
  - Getters / Setters:
      * is_running, device_info, firmware_version, stream_stats, stream_mask,
        radar_parameters
      * baudrate (115200..3000000), mode ('indoor' | 'traffic'), frequency_channel (RBFR)
      * max_range (RRAI), max_speed (RSPI), threshold_offset (THOF), tracking_filter (TRFT)
      * min_zone_distance (MIRA), max_zone_distance (MARA)
      * min_zone_angle (MIAN), max_zone_angle (MAAN)
      * min_speed_filter (MISP), max_speed_filter (MASP), direction_filter (DEDI)
  - Calls:
      * capture_point_cloud(), capture_points(), capture_targets(), read_frame()
      * capture_next_frame() [GNFD handshake], capture_raw_adc() [RADC],
        capture_raw_fft() [RFFT], capture_full_frame() [RADC+RFFT+PDAT+TDAT]
      * configure(), set_radar_parameters() [SRPS], set_detection_zone()
      * restore_factory_settings() [RFSE], flush_frames(), start(), stop() [RDOT(0x00)]

Hardware note:
  The K-MD7 / K-MD7-EVAL hardware and UART protocol provide NO command or GPIO pin to
  disable the 24 GHz RF transmitter while powered. Only external VCC/USB power gating
  stops RF emission.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional

import numpy as np

from remoterf_device_core.idl import DeviceSchema, idl_expose, idl_register

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Device Controller Implementation
# -----------------------------------------------------------------------------
from remoterf_device_core.drivers.rfbeam_kmd7.kmd7 import (
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
    RadarADCData,
    RadarFFTData,
    RadarFrame,
    RadarParameters,
    _build_output_bitmask,
)


_RANGE_METERS_TO_CODE = {100.0: 0, 200.0: 1, 300.0: 2}
_SPEED_KMH_TO_CODE = {50.0: 0, 100.0: 1, 200.0: 2}


def _validate_timeout(timeout: float) -> float:
    """Validate RPC capture timeout in seconds (0.05..30.0)."""
    val = float(timeout)
    if not math.isfinite(val) or not (0.05 <= val <= 30.0):
        raise ValueError(f"timeout must be between 0.05 and 30.0 seconds (got {timeout!r})")
    return val


def _range_meters_to_code(val: float) -> int:
    meters = float(val)
    if meters not in _RANGE_METERS_TO_CODE:
        raise ValueError(f"max_range must be 100, 200, or 300 meters (got {val!r})")
    return _RANGE_METERS_TO_CODE[meters]


def _speed_kmh_to_code(val: float) -> int:
    kmh = float(val)
    if kmh not in _SPEED_KMH_TO_CODE:
        raise ValueError(f"max_speed must be 50, 100, or 200 km/h (got {val!r})")
    return _SPEED_KMH_TO_CODE[kmh]


class RFBeamKMD7Device:
    """
    Hardware controller managing the RFbeam K-MD7 radar for RemoteRF.

    Wraps serial communication, full RPST/individual configuration, streaming and
    handshake frame acquisition, and NumPy array formatting.
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 115200,
        mode: str = "indoor",
    ) -> None:
        self.port = port
        self.baudrate = int(baudrate)
        self.radar = KMD7Radar(port=port, baudrate=self.baudrate)

        self._mode = str(mode).lower()
        self._init_baudrate = self.baudrate
        self._init_mode = self._mode
        self._init_overrides: Dict[str, Any] = {}
        self._is_running = False

        # Performance & telemetry statistics
        self._total_frames = 0
        self._dropped_frames = 0
        self._last_frame_time = 0.0
        self._fps_ema = 0.0

        # Cached active parameters
        self._params: Optional[RadarParameters] = None

    def connect(self) -> None:
        """Connect to the physical radar at configured baudrate and apply initial mode."""
        self.radar.connect(target_baudrate=self.baudrate)
        self.radar.configure_mode(self._mode)
        self._params = self.radar.get_parameters()
        self._is_running = False

    def close(self) -> None:
        """Stop streaming and disconnect cleanly from radar."""
        try:
            self.stop()
        finally:
            self.radar.disconnect()

    def _ensure_connected(self, target_baudrate: Optional[int] = None) -> bool:
        """Reconnect lazily when the serial port is closed; return True if reconnected."""
        if not self.radar.serial or not getattr(self.radar.serial, "is_open", False):
            baud = self.baudrate if target_baudrate is None else int(target_baudrate)
            self.radar.connect(target_baudrate=baud)
            self.baudrate = self.radar.baudrate
            return True
        return False

    def _restore_baseline(self) -> None:
        """Ensure connection and restore radar to lab baseline (mode, baudrate, overrides)."""
        self._ensure_connected(target_baudrate=115200)

        self.stop()
        if self.radar.baudrate != 115200:
            self.radar.set_baudrate(115200)
            self.baudrate = self.radar.baudrate

        self.radar.restore_factory_settings()
        self._mode = "traffic"

        if self.radar.baudrate != self._init_baudrate:
            self.radar.set_baudrate(self._init_baudrate)
        self.baudrate = self.radar.baudrate

        if self._init_mode in ("indoor", "traffic"):
            self.radar.configure_mode(self._init_mode)
            self._mode = self._init_mode

        overrides = getattr(self, "_init_overrides", None) or {}
        if "frequency_channel" in overrides:
            self.radar.set_frequency_channel(overrides["frequency_channel"])
        if "max_range" in overrides:
            self.radar.set_max_range(_range_meters_to_code(overrides["max_range"]))
        if "max_speed" in overrides:
            self.radar.set_speed_range(_speed_kmh_to_code(overrides["max_speed"]))
        if "threshold_offset" in overrides:
            self.radar.set_threshold_offset(overrides["threshold_offset"])

        self.flush()
        self._params = self.radar.get_parameters()

    def _get_cached_params(self) -> RadarParameters:
        reconnected = self._ensure_connected()
        if reconnected or self._params is None:
            self._params = self.radar.get_parameters()
        return self._params

    @property
    def is_running(self) -> bool:
        return self._is_running and self.radar.is_streaming

    @property
    def baudrate_active(self) -> int:
        return self.radar.baudrate

    @baudrate_active.setter
    def baudrate_active(self, val: int) -> None:
        new_baud = int(val)
        if not self._ensure_connected(target_baudrate=new_baud):
            self.radar.set_baudrate(new_baud)
        self.baudrate = self.radar.baudrate

    @property
    def stream_mask(self) -> int:
        return self.radar.active_stream_mask

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, val: str) -> None:
        mode_str = str(val).lower()
        if mode_str not in ("indoor", "traffic"):
            raise ValueError(f"Unknown mode: {mode_str}. Choose 'indoor' or 'traffic'.")
        self._ensure_connected()
        self.radar.configure_mode(mode_str)
        self._mode = mode_str
        self._params = self.radar.get_parameters()

    @property
    def frequency_channel(self) -> int:
        return self._get_cached_params().frequency_channel

    @frequency_channel.setter
    def frequency_channel(self, val: int) -> None:
        self._ensure_connected()
        self.radar.set_frequency_channel(int(val))
        self._params = self.radar.get_parameters()

    @property
    def max_range(self) -> float:
        return self._get_cached_params().max_range_meters

    @max_range.setter
    def max_range(self, val: float) -> None:
        code = _range_meters_to_code(val)
        self._ensure_connected()
        self.radar.set_max_range(code)
        self._params = self.radar.get_parameters()

    @property
    def max_speed(self) -> float:
        return self._get_cached_params().max_speed_kmh

    @max_speed.setter
    def max_speed(self, val: float) -> None:
        code = _speed_kmh_to_code(val)
        self._ensure_connected()
        self.radar.set_speed_range(code)
        self._params = self.radar.get_parameters()

    @property
    def threshold_offset(self) -> int:
        return self._get_cached_params().threshold_offset_db

    @threshold_offset.setter
    def threshold_offset(self, val: int) -> None:
        self._ensure_connected()
        self.radar.set_threshold_offset(int(val))
        self._params = self.radar.get_parameters()

    @property
    def tracking_filter(self) -> int:
        return self._get_cached_params().tracking_filter

    @tracking_filter.setter
    def tracking_filter(self, val: int) -> None:
        self._ensure_connected()
        self.radar.set_tracking_filter(int(val))
        self._params = self.radar.get_parameters()

    @property
    def min_zone_distance(self) -> int:
        return self._get_cached_params().min_zone_dist_pct

    @min_zone_distance.setter
    def min_zone_distance(self, val: int) -> None:
        self._ensure_connected()
        self.radar.set_min_zone_distance(int(val))
        self._params = self.radar.get_parameters()

    @property
    def max_zone_distance(self) -> int:
        return self._get_cached_params().max_zone_dist_pct

    @max_zone_distance.setter
    def max_zone_distance(self, val: int) -> None:
        self._ensure_connected()
        self.radar.set_max_zone_distance(int(val))
        self._params = self.radar.get_parameters()

    @property
    def min_zone_angle(self) -> int:
        return self._get_cached_params().min_zone_angle_deg

    @min_zone_angle.setter
    def min_zone_angle(self, val: int) -> None:
        self._ensure_connected()
        self.radar.set_min_zone_angle(int(val))
        self._params = self.radar.get_parameters()

    @property
    def max_zone_angle(self) -> int:
        return self._get_cached_params().max_zone_angle_deg

    @max_zone_angle.setter
    def max_zone_angle(self, val: int) -> None:
        self._ensure_connected()
        self.radar.set_max_zone_angle(int(val))
        self._params = self.radar.get_parameters()

    @property
    def min_speed_filter(self) -> int:
        return self._get_cached_params().min_zone_speed_pct

    @min_speed_filter.setter
    def min_speed_filter(self, val: int) -> None:
        self._ensure_connected()
        self.radar.set_min_speed_filter(int(val))
        self._params = self.radar.get_parameters()

    @property
    def max_speed_filter(self) -> int:
        return self._get_cached_params().max_zone_speed_pct

    @max_speed_filter.setter
    def max_speed_filter(self, val: int) -> None:
        self._ensure_connected()
        self.radar.set_max_speed_filter(int(val))
        self._params = self.radar.get_parameters()

    @property
    def direction_filter(self) -> int:
        return self._get_cached_params().direction_filter

    @direction_filter.setter
    def direction_filter(self, val: int) -> None:
        self._ensure_connected()
        self.radar.set_direction_filter(int(val))
        self._params = self.radar.get_parameters()

    def get_device_info(self) -> Dict[str, Any]:
        """Returns identity and connection metadata."""
        return {
            "device_type": "rfbeam_kmd7",
            "model": "K-MD7 Digital Radar Transceiver",
            "carrier_kit": "K-MD7-EVAL",
            "port": self.radar.port_name,
            "baudrate": self.radar.baudrate,
            "firmware_version": self.radar.firmware_version,
            "operating_frequency": "24.075 - 24.175 GHz (ISM band)",
            "antenna_beamwidth": "34° x 30°",
            "is_connected": (
                self.radar.serial is not None
                and getattr(self.radar.serial, "is_open", bool(self.radar.serial))
            ),
            "is_streaming": self.is_running,
            "stream_mask": self.radar.active_stream_mask,
        }

    def get_stream_stats(self) -> Dict[str, Any]:
        """Returns frame rate and streaming statistics."""
        return {
            "total_frames": self._total_frames,
            "dropped_frames": self._dropped_frames,
            "total_bytes_read": self.radar.total_bytes_read,
            "current_fps": round(self._fps_ema, 1),
            "is_streaming": self.is_running,
            "stream_mask": self.radar.active_stream_mask,
        }

    @staticmethod
    def _params_to_dict(p: RadarParameters, mode: str) -> Dict[str, Any]:
        return {
            "firmware": p.firmware,
            "frequency_channel": p.frequency_channel,
            "speed_setting_code": p.speed_setting,
            "max_speed_kmh": p.max_speed_kmh,
            "range_setting_code": p.range_setting,
            "max_range_meters": p.max_range_meters,
            "threshold_offset_db": p.threshold_offset_db,
            "tracking_filter_type": p.tracking_filter,
            "min_zone_dist_pct": p.min_zone_dist_pct,
            "max_zone_dist_pct": p.max_zone_dist_pct,
            "min_zone_angle_deg": p.min_zone_angle_deg,
            "max_zone_angle_deg": p.max_zone_angle_deg,
            "min_zone_speed_pct": p.min_zone_speed_pct,
            "max_zone_speed_pct": p.max_zone_speed_pct,
            "direction_filter_code": p.direction_filter,
            "mode": mode,
        }

    def get_radar_parameters(self) -> Dict[str, Any]:
        """Returns complete parsed radar parameters (GRPS -> RPST)."""
        self._ensure_connected()
        self._params = self.radar.get_parameters()
        return self._params_to_dict(self._params, self._mode)

    def set_radar_parameters(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Merge updates onto current RPST structure and write atomically via SRPS."""
        if not isinstance(params, dict):
            raise TypeError("params must be a dictionary")

        self._ensure_connected()
        cur = self.radar.get_parameters()
        data: Dict[str, Any] = {
            "firmware": cur.firmware,
            "frequency_channel": cur.frequency_channel,
            "speed_setting": cur.speed_setting,
            "range_setting": cur.range_setting,
            "threshold_offset_db": cur.threshold_offset_db,
            "tracking_filter": cur.tracking_filter,
            "min_zone_dist_pct": cur.min_zone_dist_pct,
            "max_zone_dist_pct": cur.max_zone_dist_pct,
            "min_zone_angle_deg": cur.min_zone_angle_deg,
            "max_zone_angle_deg": cur.max_zone_angle_deg,
            "min_zone_speed_pct": cur.min_zone_speed_pct,
            "max_zone_speed_pct": cur.max_zone_speed_pct,
            "direction_filter": cur.direction_filter,
        }

        alias_map = {
            "firmware": ("firmware", str),
            "frequency_channel": ("frequency_channel", int),
            "speed_setting": ("speed_setting", int),
            "speed_setting_code": ("speed_setting", int),
            "max_speed": ("speed_setting", _speed_kmh_to_code),
            "max_speed_kmh": ("speed_setting", _speed_kmh_to_code),
            "range_setting": ("range_setting", int),
            "range_setting_code": ("range_setting", int),
            "max_range": ("range_setting", _range_meters_to_code),
            "max_range_meters": ("range_setting", _range_meters_to_code),
            "threshold_offset_db": ("threshold_offset_db", int),
            "threshold_offset": ("threshold_offset_db", int),
            "tracking_filter": ("tracking_filter", int),
            "tracking_filter_type": ("tracking_filter", int),
            "min_zone_dist_pct": ("min_zone_dist_pct", int),
            "min_zone_distance": ("min_zone_dist_pct", int),
            "max_zone_dist_pct": ("max_zone_dist_pct", int),
            "max_zone_distance": ("max_zone_dist_pct", int),
            "min_zone_angle_deg": ("min_zone_angle_deg", int),
            "min_zone_angle": ("min_zone_angle_deg", int),
            "max_zone_angle_deg": ("max_zone_angle_deg", int),
            "max_zone_angle": ("max_zone_angle_deg", int),
            "min_zone_speed_pct": ("min_zone_speed_pct", int),
            "min_speed_filter": ("min_zone_speed_pct", int),
            "max_zone_speed_pct": ("max_zone_speed_pct", int),
            "max_speed_filter": ("max_zone_speed_pct", int),
            "direction_filter": ("direction_filter", int),
            "direction_filter_code": ("direction_filter", int),
        }

        grouped: Dict[str, List[tuple[str, Any]]] = {}
        for key, value in params.items():
            if key == "mode":
                continue
            if key not in alias_map:
                raise ValueError(f"Unknown radar parameter key: {key!r}")
            target_field, converter = alias_map[key]
            converted = converter(value)
            grouped.setdefault(target_field, []).append((key, converted))

        for target_field, entries in grouped.items():
            cur_val = data[target_field]
            changed = [(k, v) for k, v in entries if v != cur_val]
            if not changed:
                continue
            first_key, first_val = changed[0]
            for other_key, other_val in changed[1:]:
                if other_val != first_val:
                    raise ValueError(
                        f"Conflicting values for {target_field!r}: "
                        f"{first_key!r}={first_val!r} vs {other_key!r}={other_val!r}"
                    )
            data[target_field] = first_val

        new_params = RadarParameters(**data)
        self.radar.set_parameters(new_params)
        self._params = self.radar.get_parameters()
        return self._params_to_dict(self._params, self._mode)

    def set_detection_zone(
        self,
        min_dist_pct: Optional[int] = None,
        max_dist_pct: Optional[int] = None,
        min_angle_deg: Optional[int] = None,
        max_angle_deg: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Configure detection zone distance (0..100%) and azimuth angle (-30..+30°)."""
        self._ensure_connected()
        cur = self.radar.get_parameters()
        new_min_d = cur.min_zone_dist_pct if min_dist_pct is None else int(min_dist_pct)
        new_max_d = cur.max_zone_dist_pct if max_dist_pct is None else int(max_dist_pct)
        new_min_a = cur.min_zone_angle_deg if min_angle_deg is None else int(min_angle_deg)
        new_max_a = cur.max_zone_angle_deg if max_angle_deg is None else int(max_angle_deg)

        if not (0 <= new_min_d <= 100 and 0 <= new_max_d <= 100):
            raise ValueError("Detection zone distance percentages must be between 0 and 100")
        if new_min_d > new_max_d:
            raise ValueError("min_dist_pct cannot exceed max_dist_pct")
        if not (-30 <= new_min_a <= 30 and -30 <= new_max_a <= 30):
            raise ValueError("Detection zone angles must be between -30 and +30 degrees")
        if new_min_a > new_max_a:
            raise ValueError("min_angle_deg cannot exceed max_angle_deg")

        # Update max before min when raising window so intermediate firmware state stays ordered
        if new_min_d > cur.max_zone_dist_pct:
            if max_dist_pct is not None:
                self.radar.set_max_zone_distance(new_max_d)
            if min_dist_pct is not None:
                self.radar.set_min_zone_distance(new_min_d)
        else:
            if min_dist_pct is not None:
                self.radar.set_min_zone_distance(new_min_d)
            if max_dist_pct is not None:
                self.radar.set_max_zone_distance(new_max_d)

        if new_min_a > cur.max_zone_angle_deg:
            if max_angle_deg is not None:
                self.radar.set_max_zone_angle(new_max_a)
            if min_angle_deg is not None:
                self.radar.set_min_zone_angle(new_min_a)
        else:
            if min_angle_deg is not None:
                self.radar.set_min_zone_angle(new_min_a)
            if max_angle_deg is not None:
                self.radar.set_max_zone_angle(new_max_a)

        self._params = self.radar.get_parameters()
        return self._params_to_dict(self._params, self._mode)

    def start(
        self,
        raw_targets: bool = True,
        tracked_targets: bool = True,
        *,
        raw_adc: bool = False,
        raw_fft: bool = False,
        bitmask: Optional[int] = None,
    ) -> bool:
        """Start streaming radar frames with selected message types (RDOT)."""
        mask = _build_output_bitmask(
            raw_adc=raw_adc,
            raw_fft=raw_fft,
            raw_targets=raw_targets,
            tracked_targets=tracked_targets,
            done=True,
            bitmask=bitmask,
        )
        if mask == 0:
            return self.stop()

        self._ensure_connected()

        if not self.radar.is_streaming or self.radar.active_stream_mask != mask:
            self.radar.start_streaming(
                raw_targets=raw_targets,
                tracked_targets=tracked_targets,
                raw_adc=raw_adc,
                raw_fft=raw_fft,
                bitmask=mask,
            )
        self._is_running = True
        return True

    def stop(self) -> bool:
        """Stop radar streaming via RDOT(0x00) while keeping serial connection open."""
        self.radar.stop_streaming()
        self._is_running = False
        return True

    def flush(self) -> None:
        """Flush internal receive buffers."""
        self.radar.flush()

    def _record_frame_stats(self, frame: Optional[RadarFrame]) -> None:
        now = time.time()
        if frame is not None:
            self._total_frames += 1
            if self._last_frame_time > 0.0:
                dt = now - self._last_frame_time
                if dt > 0.0:
                    inst_fps = 1.0 / dt
                    self._fps_ema = (
                        0.9 * self._fps_ema + 0.1 * inst_fps if self._fps_ema > 0 else inst_fps
                    )
            self._last_frame_time = now
        else:
            self._dropped_frames += 1

    def _acquire_frame(
        self,
        *,
        timeout: float,
        required_flag: Optional[int] = None,
        use_handshake: bool = False,
        raw_adc: bool = False,
        raw_fft: bool = False,
        raw_targets: bool = True,
        tracked_targets: bool = True,
    ) -> Optional[RadarFrame]:
        """Unified frame acquisition for both streaming (RDOT) and handshake (GNFD) modes."""
        t = _validate_timeout(timeout)

        self._ensure_connected()

        if use_handshake:
            was_streaming = self.radar.is_streaming
            prev_mask = self.radar.active_stream_mask
            if was_streaming:
                self.radar.stop_streaming()
            try:
                frame = self.radar.get_next_frame(
                    raw_adc=raw_adc,
                    raw_fft=raw_fft,
                    raw_targets=raw_targets,
                    tracked_targets=tracked_targets,
                    timeout=t,
                )
            finally:
                if was_streaming and prev_mask:
                    try:
                        self.radar.start_streaming(bitmask=prev_mask)
                        self._is_running = True
                    except Exception:
                        self._is_running = False
            self._record_frame_stats(frame)
            return frame

        # Streaming mode
        req = required_flag or 0
        if not self.is_running:
            self.start(
                raw_targets=raw_targets,
                tracked_targets=tracked_targets,
                raw_adc=raw_adc or bool(req & FLAG_RADC),
                raw_fft=raw_fft or bool(req & FLAG_RFFT),
            )
        elif required_flag is not None:
            desired_mask = required_flag | FLAG_DONE
            if self.radar.active_stream_mask != desired_mask:
                self.start(bitmask=desired_mask)

        active_mask = self.radar.active_stream_mask
        allowed_mask = (active_mask & FLAG_ALL) | req | FLAG_DONE
        # PDAT/TDAT may be absent in an empty scene; RADC/RFFT/DONE are always sent when enabled.
        required_headers = (allowed_mask & (FLAG_RADC | FLAG_RFFT)) | FLAG_DONE
        deadline = time.monotonic() + t
        frame: Optional[RadarFrame] = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            candidate = self.radar.read_frame(timeout=remaining)
            if candidate is None:
                break
            if candidate.packet_mask & ~allowed_mask or (candidate.packet_mask & required_headers) != required_headers:
                continue  # a stale frame from the previous stream mask
            frame = candidate
            break
        self._record_frame_stats(frame)
        return frame

    @staticmethod
    def _format_targets_arrays(targets) -> Dict[str, np.ndarray]:
        return {
            "x": np.array([t.x for t in targets], dtype=np.float32),
            "y": np.array([t.y for t in targets], dtype=np.float32),
            "distance": np.array([t.distance for t in targets], dtype=np.float32),
            "speed": np.array([t.speed for t in targets], dtype=np.float32),
            "angle": np.array([t.angle for t in targets], dtype=np.float32),
            "magnitude": np.array([t.magnitude for t in targets], dtype=np.float32),
        }

    @classmethod
    def _format_point_cloud(cls, frame: Optional[RadarFrame]) -> Dict[str, Any]:
        if not frame:
            empty_f32 = np.empty(0, dtype=np.float32)
            empty_i32 = np.empty(0, dtype=np.int32)
            return {
                "frame_number": -1,
                "timestamp": time.time(),
                "num_raw_points": 0,
                "num_tracked_targets": 0,
                "raw": {
                    "x": empty_f32,
                    "y": empty_f32,
                    "distance": empty_f32,
                    "speed": empty_f32,
                    "angle": empty_f32,
                    "magnitude": empty_f32,
                },
                "tracked": {
                    "id": empty_i32,
                    "x": empty_f32,
                    "y": empty_f32,
                    "distance": empty_f32,
                    "speed": empty_f32,
                    "angle": empty_f32,
                    "magnitude": empty_f32,
                },
            }

        raw_dict = cls._format_targets_arrays(frame.raw_targets)
        trk_dict = cls._format_targets_arrays(frame.tracked_targets)
        trk_dict["id"] = np.array(
            [t.tracking_id or 0 for t in frame.tracked_targets], dtype=np.int32
        )

        return {
            "frame_number": frame.frame_number,
            "timestamp": frame.timestamp,
            "num_raw_points": len(frame.raw_targets),
            "num_tracked_targets": len(frame.tracked_targets),
            "raw": raw_dict,
            "tracked": trk_dict,
        }

    @staticmethod
    def _format_adc(
        frame: Optional[RadarFrame],
    ) -> Dict[str, Any]:
        adc: Optional[RadarADCData] = frame.adc_data if frame else None
        if adc is None:
            empty = np.empty(0, dtype=np.uint16)
            return {
                "frame_number": frame.frame_number if frame else -1,
                "timestamp": frame.timestamp if frame else time.time(),
                "valid": False,
                "num_samples": 0,
                "if1_freq_a_i": empty,
                "if1_freq_a_q": empty,
                "if2_freq_a_i": empty,
                "if2_freq_a_q": empty,
                "if1_freq_b_i": empty,
                "if1_freq_b_q": empty,
            }
        return {
            "frame_number": frame.frame_number,
            "timestamp": frame.timestamp,
            "valid": True,
            "num_samples": RADC_NUM_SAMPLES,
            "if1_freq_a_i": np.array(adc.if1_freq_a_i, dtype=np.uint16),
            "if1_freq_a_q": np.array(adc.if1_freq_a_q, dtype=np.uint16),
            "if2_freq_a_i": np.array(adc.if2_freq_a_i, dtype=np.uint16),
            "if2_freq_a_q": np.array(adc.if2_freq_a_q, dtype=np.uint16),
            "if1_freq_b_i": np.array(adc.if1_freq_b_i, dtype=np.uint16),
            "if1_freq_b_q": np.array(adc.if1_freq_b_q, dtype=np.uint16),
        }

    @staticmethod
    def _format_fft(
        frame: Optional[RadarFrame],
    ) -> Dict[str, Any]:
        fft: Optional[RadarFFTData] = frame.fft_data if frame else None
        if fft is None:
            empty = np.empty(0, dtype=np.float32)
            return {
                "frame_number": frame.frame_number if frame else -1,
                "timestamp": frame.timestamp if frame else time.time(),
                "valid": False,
                "num_bins": 0,
                "spectrum_db": empty,
                "threshold_db": empty,
            }
        return {
            "frame_number": frame.frame_number,
            "timestamp": frame.timestamp,
            "valid": True,
            "num_bins": RFFT_NUM_BINS,
            "spectrum_db": np.array(fft.spectrum_db, dtype=np.float32),
            "threshold_db": np.array(fft.threshold_db, dtype=np.float32),
        }

    @classmethod
    def _format_full_frame(cls, frame: Optional[RadarFrame]) -> Dict[str, Any]:
        result = cls._format_point_cloud(frame)
        result["adc"] = cls._format_adc(frame) if (frame and frame.adc_data is not None) else None
        result["fft"] = cls._format_fft(frame) if (frame and frame.fft_data is not None) else None
        return result

    def capture_point_cloud(self, timeout: float = 1.0) -> Dict[str, Any]:
        """Capture one frame and return structured point cloud with NumPy arrays."""
        frame = self._acquire_frame(
            timeout=timeout,
            required_flag=FLAG_PDAT | FLAG_TDAT,
            use_handshake=False,
        )
        return self._format_point_cloud(frame)

    def capture_points(
        self,
        timeout: float = 1.0,
        include_raw: bool = True,
        include_tracked: bool = True,
    ) -> np.ndarray:
        """Capture points as a 2D float32 NumPy array of shape (N, 6)."""
        required_flag = (FLAG_PDAT if include_raw else 0) | (FLAG_TDAT if include_tracked else 0)
        frame = self._acquire_frame(
            timeout=timeout,
            required_flag=required_flag,
            use_handshake=False,
            raw_targets=include_raw,
            tracked_targets=include_tracked,
        )
        if not frame:
            return np.empty((0, 6), dtype=np.float32)

        rows = []
        if include_tracked and frame.tracked_targets:
            for t in frame.tracked_targets:
                rows.append([t.x, t.y, t.distance, t.speed, t.angle, t.magnitude])

        if include_raw and frame.raw_targets:
            for t in frame.raw_targets:
                rows.append([t.x, t.y, t.distance, t.speed, t.angle, t.magnitude])

        if not rows:
            return np.empty((0, 6), dtype=np.float32)

        return np.array(rows, dtype=np.float32)

    def capture_targets(self, timeout: float = 1.0) -> List[Dict[str, Any]]:
        """Capture active tracked targets as a list of dictionaries."""
        frame = self._acquire_frame(
            timeout=timeout,
            required_flag=FLAG_TDAT,
            use_handshake=False,
            raw_targets=False,
            tracked_targets=True,
        )
        if not frame or not frame.tracked_targets:
            return []

        targets = []
        for t in frame.tracked_targets:
            targets.append(
                {
                    "id": t.tracking_id,
                    "distance_m": float(t.distance),
                    "speed_kmh": float(t.speed),
                    "speed_mps": float(t.speed_mps),
                    "angle_deg": float(t.angle),
                    "x_m": float(t.x),
                    "y_m": float(t.y),
                    "power_db": float(t.magnitude),
                    "motion": (
                        "receding"
                        if t.speed > 0.5
                        else ("approaching" if t.speed < -0.5 else "stationary")
                    ),
                }
            )
        return targets

    def read_frame_raw(self, timeout: float = 1.0) -> Dict[str, Any]:
        """Return frame header summary dictionary."""
        frame = self._acquire_frame(timeout=timeout, use_handshake=False)
        if not frame:
            return {
                "frame_number": -1,
                "timestamp": time.time(),
                "raw_count": 0,
                "tracked_count": 0,
                "has_adc": False,
                "has_fft": False,
            }
        return {
            "frame_number": frame.frame_number,
            "timestamp": frame.timestamp,
            "raw_count": len(frame.raw_targets),
            "tracked_count": len(frame.tracked_targets),
            "has_adc": frame.adc_data is not None,
            "has_fft": frame.fft_data is not None,
        }

    def capture_next_frame(
        self,
        *,
        raw_adc: bool = False,
        raw_fft: bool = False,
        raw_targets: bool = True,
        tracked_targets: bool = True,
        timeout: float = 2.0,
    ) -> Dict[str, Any]:
        """Capture a single synchronized frame on demand using GNFD handshake."""
        frame = self._acquire_frame(
            timeout=timeout,
            use_handshake=True,
            raw_adc=raw_adc,
            raw_fft=raw_fft,
            raw_targets=raw_targets,
            tracked_targets=tracked_targets,
        )
        return self._format_full_frame(frame)

    def capture_raw_adc(
        self,
        timeout: float = 2.0,
        *,
        use_handshake: bool = True,
    ) -> Dict[str, Any]:
        """Capture RADC raw I/Q ADC samples (6 x 512 uint16 NumPy arrays)."""
        frame = self._acquire_frame(
            timeout=timeout,
            required_flag=FLAG_RADC,
            use_handshake=use_handshake,
            raw_adc=True,
            raw_fft=False,
            raw_targets=False,
            tracked_targets=False,
        )
        return self._format_adc(frame)

    def capture_raw_fft(
        self,
        timeout: float = 2.0,
        *,
        use_handshake: bool = True,
    ) -> Dict[str, Any]:
        """Capture RFFT 512-bin spectrum and detection threshold (float32 dB NumPy arrays)."""
        frame = self._acquire_frame(
            timeout=timeout,
            required_flag=FLAG_RFFT,
            use_handshake=use_handshake,
            raw_adc=False,
            raw_fft=True,
            raw_targets=False,
            tracked_targets=False,
        )
        return self._format_fft(frame)

    def capture_full_frame(
        self,
        timeout: float = 2.0,
        *,
        raw_adc: bool = True,
        raw_fft: bool = True,
        raw_targets: bool = True,
        tracked_targets: bool = True,
        use_handshake: bool = True,
    ) -> Dict[str, Any]:
        """Capture all requested outputs (RADC, RFFT, PDAT, TDAT) from one radar frame."""
        required_flag = _build_output_bitmask(
            raw_adc=raw_adc,
            raw_fft=raw_fft,
            raw_targets=raw_targets,
            tracked_targets=tracked_targets,
            done=False,
        )
        frame = self._acquire_frame(
            timeout=timeout,
            required_flag=required_flag,
            use_handshake=use_handshake,
            raw_adc=raw_adc,
            raw_fft=raw_fft,
            raw_targets=raw_targets,
            tracked_targets=tracked_targets,
        )
        return self._format_full_frame(frame)


# -----------------------------------------------------------------------------
# RemoteRF Device Schema Class
# -----------------------------------------------------------------------------
@idl_register("rfbeam_kmd7")
class RFBeamKMD7Schema(DeviceSchema):
    """
    RemoteRF Device Schema for RFbeam K-MD7 24 GHz Digital Radar Transceivers.

    Exposes full parameter configuration, streaming and handshake acquisition,
    raw ADC I/Q, FFT spectrum/threshold, 2D Cartesian point clouds, and tracked targets.
    """

    device_type = "rfbeam_kmd7"
    client_class = "RFBeamKMD7"
    driver_version = "0.2.0"

    client_modules = {"rfbeam_kmd7": "remoteRF.drivers.support.rfbeam_kmd7"}

    @staticmethod
    def make_device(**kwargs) -> Optional[RFBeamKMD7Device]:
        """Factory called by RemoteRF server/host when initializing device inventory."""
        port = kwargs.get("port")
        baudrate = int(kwargs.get("baudrate", 115200))
        mode = kwargs.get("mode", "indoor")
        max_range = kwargs.get("max_range")
        max_speed = kwargs.get("max_speed")
        threshold_offset = kwargs.get("threshold_offset")
        frequency_channel = kwargs.get("frequency_channel")
        auto_start = bool(kwargs.get("auto_start", True))

        if not port:
            port = KMD7Radar.find_serial_port(strict=True)

        if not port:
            print("[rfbeam_kmd7] Could not locate RFbeam K-MD7 radar serial port.")
            print("[rfbeam_kmd7] Ensure the sensor is connected via USB and FTDI drivers are loaded.")
            return None

        dev: Optional[RFBeamKMD7Device] = None
        try:
            print(f"[rfbeam_kmd7] Initializing RFbeam K-MD7 radar on {port} (Mode: '{mode}')...")
            dev = RFBeamKMD7Device(port=port, baudrate=baudrate, mode=mode)
            dev.connect()

            if frequency_channel is not None:
                val = int(frequency_channel)
                dev.frequency_channel = val
                dev._init_overrides["frequency_channel"] = val
            if max_range is not None:
                val = float(max_range)
                dev.max_range = val
                dev._init_overrides["max_range"] = val
            if max_speed is not None:
                val = float(max_speed)
                dev.max_speed = val
                dev._init_overrides["max_speed"] = val
            if threshold_offset is not None:
                val = int(threshold_offset)
                dev.threshold_offset = val
                dev._init_overrides["threshold_offset"] = val

            if auto_start:
                dev.start()

            print(f"[rfbeam_kmd7] Successfully initialized on {port} (Firmware: {dev.radar.firmware_version}).")
            return dev

        except Exception as exc:
            print(f"[rfbeam_kmd7] Failed to initialize radar device: {exc}")
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass
            return None

    # -------------------------------------------------------------------------
    # Exposed Getters
    # -------------------------------------------------------------------------

    @idl_expose(kind="get", doc="Whether the radar is active and streaming.")
    def get_is_running(self) -> bool:
        return self.device.is_running

    @idl_expose(kind="get", doc="Serial port, connection identity, and firmware version.")
    def get_device_info(self) -> Dict[str, Any]:
        return self.device.get_device_info()

    @idl_expose(kind="get", doc="Firmware version string (e.g. K-MD7_APP-RFB-0205).")
    def get_firmware_version(self) -> str:
        return self.device.radar.firmware_version

    @idl_expose(kind="get", doc="Frame rate, packet counts, and reader health statistics.")
    def get_stream_stats(self) -> Dict[str, Any]:
        return self.device.get_stream_stats()

    @idl_expose(kind="get", doc="Active RDOT streaming output bitmask (0 when stopped).")
    def get_stream_mask(self) -> int:
        return self.device.stream_mask

    @idl_expose(kind="get", doc="Complete 31-byte radar parameter structure (RPST).")
    def get_radar_parameters(self) -> Dict[str, Any]:
        return self.device.get_radar_parameters()

    # -------------------------------------------------------------------------
    # Exposed Getters & Setters
    # -------------------------------------------------------------------------

    @idl_expose(kind="get", doc="Active UART baud rate in bps (115200..3000000).")
    def get_baudrate(self) -> int:
        return self.device.baudrate_active

    @idl_expose(kind="set", doc="Set active UART baud rate (115200, 460800, 921600, 2000000, or 3000000).")
    def set_baudrate(self, value: int) -> None:
        self.device.baudrate_active = int(value)

    @idl_expose(kind="get", doc="Active operating mode ('indoor' for pedestrian/gestures, 'traffic' for vehicles).")
    def get_mode(self) -> str:
        return self.device.mode

    @idl_expose(kind="set", doc="Set active operating mode ('indoor' or 'traffic').")
    def set_mode(self, value: str) -> None:
        self.device.mode = str(value)

    @idl_expose(kind="get", doc="RF base frequency channel (0=Low, 1=Middle, 2=High).")
    def get_frequency_channel(self) -> int:
        return self.device.frequency_channel

    @idl_expose(kind="set", doc="Set RF base frequency channel (RBFR: 0=Low, 1=Middle, 2=High).")
    def set_frequency_channel(self, value: int) -> None:
        self.device.frequency_channel = int(value)

    @idl_expose(kind="get", doc="Maximum unambiguous detection range in meters (100, 200, 300).")
    def get_max_range(self) -> float:
        return self.device.max_range

    @idl_expose(kind="set", doc="Set maximum detection range in meters (100, 200, or 300).")
    def set_max_range(self, value: float) -> None:
        self.device.max_range = float(value)

    @idl_expose(kind="get", doc="Maximum measurable speed in km/h (50, 100, 200).")
    def get_max_speed(self) -> float:
        return self.device.max_speed

    @idl_expose(kind="set", doc="Set maximum measurable speed in km/h (50, 100, or 200).")
    def set_max_speed(self, value: float) -> None:
        self.device.max_speed = float(value)

    @idl_expose(kind="get", doc="Detection threshold offset in dB (0 - 60 dB). Lower is more sensitive.")
    def get_threshold_offset(self) -> int:
        return self.device.threshold_offset

    @idl_expose(kind="set", doc="Set detection threshold offset in dB (0 - 60 dB).")
    def set_threshold_offset(self, value: int) -> None:
        self.device.threshold_offset = int(value)

    @idl_expose(kind="get", doc="Tracking filter type: 0=Standard, 1=Fast detection, 2=Long visibility.")
    def get_tracking_filter(self) -> int:
        return self.device.tracking_filter

    @idl_expose(kind="set", doc="Set tracking filter type: 0=Standard, 1=Fast detection, 2=Long visibility.")
    def set_tracking_filter(self, value: int) -> None:
        self.device.tracking_filter = int(value)

    @idl_expose(kind="get", doc="Minimum detection zone distance as percentage of range setting (0 - 100%).")
    def get_min_zone_distance(self) -> int:
        return self.device.min_zone_distance

    @idl_expose(kind="set", doc="Set minimum detection zone distance percentage (MIRA: 0 - 100%).")
    def set_min_zone_distance(self, value: int) -> None:
        self.device.min_zone_distance = int(value)

    @idl_expose(kind="get", doc="Maximum detection zone distance as percentage of range setting (0 - 100%).")
    def get_max_zone_distance(self) -> int:
        return self.device.max_zone_distance

    @idl_expose(kind="set", doc="Set maximum detection zone distance percentage (MARA: 0 - 100%).")
    def set_max_zone_distance(self, value: int) -> None:
        self.device.max_zone_distance = int(value)

    @idl_expose(kind="get", doc="Minimum detection zone azimuth angle in degrees (-30 to +30).")
    def get_min_zone_angle(self) -> int:
        return self.device.min_zone_angle

    @idl_expose(kind="set", doc="Set minimum detection zone azimuth angle in degrees (MIAN: -30 to +30).")
    def set_min_zone_angle(self, value: int) -> None:
        self.device.min_zone_angle = int(value)

    @idl_expose(kind="get", doc="Maximum detection zone azimuth angle in degrees (-30 to +30).")
    def get_max_zone_angle(self) -> int:
        return self.device.max_zone_angle

    @idl_expose(kind="set", doc="Set maximum detection zone azimuth angle in degrees (MAAN: -30 to +30).")
    def set_max_zone_angle(self, value: int) -> None:
        self.device.max_zone_angle = int(value)

    @idl_expose(kind="get", doc="Minimum detection speed filter as percentage of speed setting (0 - 100%).")
    def get_min_speed_filter(self) -> int:
        return self.device.min_speed_filter

    @idl_expose(kind="set", doc="Set minimum detection speed filter percentage (MISP: 0 - 100%).")
    def set_min_speed_filter(self, value: int) -> None:
        self.device.min_speed_filter = int(value)

    @idl_expose(kind="get", doc="Maximum detection speed filter as percentage of speed setting (0 - 100%).")
    def get_max_speed_filter(self) -> int:
        return self.device.max_speed_filter

    @idl_expose(kind="set", doc="Set maximum detection speed filter percentage (MASP: 0 - 100%).")
    def set_max_speed_filter(self, value: int) -> None:
        self.device.max_speed_filter = int(value)

    @idl_expose(kind="get", doc="Detection direction filter: 0=Receding, 1=Approaching, 2=Both.")
    def get_direction_filter(self) -> int:
        return self.device.direction_filter

    @idl_expose(kind="set", doc="Set detection direction filter: 0=Receding, 1=Approaching, 2=Both.")
    def set_direction_filter(self, value: int) -> None:
        self.device.direction_filter = int(value)

    # -------------------------------------------------------------------------
    # Exposed Calls
    # -------------------------------------------------------------------------

    @idl_expose(kind="call", doc="Capture structured 2D point cloud with positions, velocities, and power.", tag="TX")
    def call_capture_point_cloud(self, timeout: float = 1.0) -> Dict[str, Any]:
        return self.device.capture_point_cloud(timeout=float(timeout))

    @idl_expose(kind="call", doc="Capture points as an (N, 6) NumPy array [x, y, dist, speed, angle, mag].", tag="TX")
    def call_capture_points(
        self,
        timeout: float = 1.0,
        *,
        include_raw: bool = True,
        include_tracked: bool = True,
    ) -> np.ndarray:
        return self.device.capture_points(
            timeout=float(timeout),
            include_raw=bool(include_raw),
            include_tracked=bool(include_tracked),
        )

    @idl_expose(kind="call", doc="Capture active tracked targets as a list of structured dictionaries.", tag="TX")
    def call_capture_targets(self, timeout: float = 1.0) -> List[Dict[str, Any]]:
        return self.device.capture_targets(timeout=float(timeout))

    @idl_expose(kind="call", doc="Read one synchronized radar frame summary.", tag="TX")
    def call_read_frame(self, timeout: float = 1.0) -> Dict[str, Any]:
        return self.device.read_frame_raw(timeout=float(timeout))

    @idl_expose(kind="call", doc="Request one frame on demand via GNFD handshake mode.", tag="TX")
    def call_capture_next_frame(
        self,
        *,
        raw_adc: bool = False,
        raw_fft: bool = False,
        raw_targets: bool = True,
        tracked_targets: bool = True,
        timeout: float = 2.0,
    ) -> Dict[str, Any]:
        return self.device.capture_next_frame(
            raw_adc=bool(raw_adc),
            raw_fft=bool(raw_fft),
            raw_targets=bool(raw_targets),
            tracked_targets=bool(tracked_targets),
            timeout=float(timeout),
        )

    @idl_expose(kind="call", doc="Capture raw ADC I/Q samples (RADC: 3 IF blocks x 512 I/Q uint16).", tag="TX")
    def call_capture_raw_adc(
        self,
        timeout: float = 2.0,
        *,
        use_handshake: bool = True,
    ) -> Dict[str, Any]:
        return self.device.capture_raw_adc(
            timeout=float(timeout),
            use_handshake=bool(use_handshake),
        )

    @idl_expose(kind="call", doc="Capture raw 512-point FFT spectrum and threshold in dB (RFFT).", tag="TX")
    def call_capture_raw_fft(
        self,
        timeout: float = 2.0,
        *,
        use_handshake: bool = True,
    ) -> Dict[str, Any]:
        return self.device.capture_raw_fft(
            timeout=float(timeout),
            use_handshake=bool(use_handshake),
        )

    @idl_expose(kind="call", doc="Capture RADC, RFFT, PDAT, and TDAT together from one radar frame.", tag="TX")
    def call_capture_full_frame(
        self,
        timeout: float = 2.0,
        *,
        raw_adc: bool = True,
        raw_fft: bool = True,
        raw_targets: bool = True,
        tracked_targets: bool = True,
        use_handshake: bool = True,
    ) -> Dict[str, Any]:
        return self.device.capture_full_frame(
            timeout=float(timeout),
            raw_adc=bool(raw_adc),
            raw_fft=bool(raw_fft),
            raw_targets=bool(raw_targets),
            tracked_targets=bool(tracked_targets),
            use_handshake=bool(use_handshake),
        )

    @idl_expose(kind="call", doc="Configure radar parameters via high-level presets and optional overrides.")
    def call_configure(
        self,
        mode: str = "indoor",
        *,
        max_range: Optional[float] = None,
        max_speed: Optional[float] = None,
        threshold_offset: Optional[int] = None,
        tracking_filter: Optional[int] = None,
        min_speed_filter: Optional[int] = None,
        max_speed_filter: Optional[int] = None,
        direction_filter: Optional[int] = None,
        frequency_channel: Optional[int] = None,
        min_zone_distance: Optional[int] = None,
        max_zone_distance: Optional[int] = None,
        min_zone_angle: Optional[int] = None,
        max_zone_angle: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.device.mode = mode
        if frequency_channel is not None:
            self.device.frequency_channel = int(frequency_channel)
        if max_range is not None:
            self.device.max_range = float(max_range)
        if max_speed is not None:
            self.device.max_speed = float(max_speed)
        if threshold_offset is not None:
            self.device.threshold_offset = int(threshold_offset)
        if tracking_filter is not None:
            self.device.tracking_filter = int(tracking_filter)
        if min_speed_filter is not None:
            self.device.min_speed_filter = int(min_speed_filter)
        if max_speed_filter is not None:
            self.device.max_speed_filter = int(max_speed_filter)
        if direction_filter is not None:
            self.device.direction_filter = int(direction_filter)
        if any(
            v is not None
            for v in (min_zone_distance, max_zone_distance, min_zone_angle, max_zone_angle)
        ):
            self.device.set_detection_zone(
                min_dist_pct=min_zone_distance,
                max_dist_pct=max_zone_distance,
                min_angle_deg=min_zone_angle,
                max_angle_deg=max_zone_angle,
            )
        return self.device.get_radar_parameters()

    @idl_expose(kind="call", doc="Update complete radar parameter structure atomically (SRPS).")
    def call_set_radar_parameters(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.device.set_radar_parameters(params)

    @idl_expose(kind="call", doc="Set detection zone distance (%) and azimuth angle (-30..+30 deg).")
    def call_set_detection_zone(
        self,
        min_dist_pct: Optional[int] = None,
        max_dist_pct: Optional[int] = None,
        min_angle_deg: Optional[int] = None,
        max_angle_deg: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self.device.set_detection_zone(
            min_dist_pct=min_dist_pct,
            max_dist_pct=max_dist_pct,
            min_angle_deg=min_angle_deg,
            max_angle_deg=max_angle_deg,
        )

    @idl_expose(kind="call", doc="Restore factory default settings (RFSE).")
    def call_restore_factory_settings(self) -> bool:
        self.device._ensure_connected()
        self.device.radar.restore_factory_settings()
        self.device._mode = "traffic"
        self.device._params = self.device.radar.get_parameters()
        return True

    @idl_expose(kind="call", doc="Flush serial input buffer.")
    def call_flush_frames(self) -> bool:
        self.device.flush()
        return True

    @idl_expose(kind="call", doc="Start radar streaming with selectable output messages (RDOT).", tag="TX")
    def call_start(
        self,
        raw_targets: bool = True,
        tracked_targets: bool = True,
        *,
        raw_adc: bool = False,
        raw_fft: bool = False,
        bitmask: Optional[int] = None,
    ) -> bool:
        return self.device.start(
            raw_targets=bool(raw_targets),
            tracked_targets=bool(tracked_targets),
            raw_adc=bool(raw_adc),
            raw_fft=bool(raw_fft),
            bitmask=bitmask,
        )

    @idl_expose(kind="call", doc="Stop radar streaming cleanly via RDOT(0x00).")
    def call_stop(self) -> bool:
        return self.device.stop()

    def reset_for_next_holder(self) -> None:
        """Reset K-MD7 radar state between reservations.

        Note: K-MD7 hardware has no UART command or pin to mute the 24 GHz RF
        transmitter; stopping streaming halts UART emission and restores safe
        baseline configuration for the next holder.
        """
        if self.device is None:
            return

        dev = self.device
        try:
            dev._restore_baseline()
        except Exception as first_exc:
            dev._params = None
            dev._is_running = False
            try:
                dev.radar.disconnect()
            except Exception:
                pass
            try:
                dev._restore_baseline()
            except Exception:
                dev._params = None
                dev._is_running = False
                dev.baudrate = dev._init_baudrate
                dev.radar.baudrate = dev._init_baudrate
                try:
                    dev.radar.disconnect()
                except Exception:
                    pass
                raise first_exc
            logger.warning(
                "[rfbeam_kmd7] Reset failed (%s); recovered baseline after reconnect.",
                first_exc,
            )
        finally:
            dev._total_frames = 0
            dev._dropped_frames = 0
            dev._last_frame_time = 0.0
            dev._fps_ema = 0.0
            dev.radar.total_bytes_read = 0
