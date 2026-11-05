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
RemoteRF Custom Device Schema for RFbeam K-MD7 (and K-MD7-EVAL) Radar Transceiver.

Follows the RemoteRF Custom Device Specification:
https://remoterf.net/docs/server/device-setup/custom/

This schema bridges physical RFbeam K-MD7 24 GHz digital radar hardware to RemoteRF clients.
It exposes:
  - Readable device properties: status, port info, firmware version, stream stats, radar parameters
  - Runtime setters: operating mode (indoor vs traffic), max range, max speed, threshold offset,
    tracking filter, minimum speed filter, direction filter
  - Actions/Calls:
      * capture_point_cloud(): Parsed point cloud (raw points + tracked targets) as NumPy arrays
      * capture_points(): (N, 6) NumPy array [x, y, distance, speed, angle, magnitude]
      * capture_targets(): List of active tracked objects with IDs, velocity vectors, and power
      * read_frame(): Synchronized raw frame dictionary
      * configure(): High-level configuration switching (indoor/pedestrian vs. traffic)
      * restore_factory_settings(): Restore factory defaults
      * flush_frames(): Discard buffered/stale serial packets
      * start() / stop(): Control radar streaming acquisition
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


from remoterf_device_core.idl import DeviceSchema, idl_expose, idl_register


# -----------------------------------------------------------------------------
# Device Controller Implementation
# -----------------------------------------------------------------------------
from remoterf_device_core.drivers.rfbeam_kmd7.kmd7 import KMD7Radar, RadarFrame, RadarTarget, RadarParameters


class RFBeamKMD7Device:
    """
    Hardware controller managing the RFbeam K-MD7 radar for RemoteRF.

    Wraps serial communication, configuration, frame acquisition, and
    structured NumPy array conversions.
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 115200,
        mode: str = "indoor",
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.radar = KMD7Radar(port=port, baudrate=baudrate)

        self._mode = mode
        self._is_running = False

        # Performance & telemetry statistics
        self._total_frames = 0
        self._total_bytes_read = 0
        self._dropped_frames = 0
        self._last_frame_time = 0.0
        self._fps_ema = 0.0

        # Cached active parameters
        self._params: Optional[RadarParameters] = None

    def connect(self) -> None:
        """Connect to the physical radar and fetch initial configuration."""
        self.radar.connect()
        self.radar.configure_mode(self._mode)
        self._params = self.radar.get_parameters()
        self._is_running = False

    def close(self) -> None:
        """Disconnect cleanly from radar."""
        self.stop()
        self.radar.disconnect()

    @property
    def is_running(self) -> bool:
        return self._is_running and self.radar.is_streaming

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, val: str) -> None:
        mode_str = str(val).lower()
        self.radar.configure_mode(mode_str)
        self._mode = mode_str
        self._params = self.radar.get_parameters()

    @property
    def max_range(self) -> float:
        if self._params is None:
            self._params = self.radar.get_parameters()
        return self._params.max_range_meters

    @max_range.setter
    def max_range(self, val: float) -> None:
        val = float(val)
        code = 0 if val <= 100 else (1 if val <= 200 else 2)
        self.radar.set_max_range(code)
        self._params = self.radar.get_parameters()

    @property
    def max_speed(self) -> float:
        if self._params is None:
            self._params = self.radar.get_parameters()
        return self._params.max_speed_kmh

    @max_speed.setter
    def max_speed(self, val: float) -> None:
        val = float(val)
        code = 0 if val <= 50 else (1 if val <= 100 else 2)
        self.radar.set_speed_range(code)
        self._params = self.radar.get_parameters()

    @property
    def threshold_offset(self) -> int:
        if self._params is None:
            self._params = self.radar.get_parameters()
        return self._params.threshold_offset_db

    @threshold_offset.setter
    def threshold_offset(self, val: int) -> None:
        self.radar.set_threshold_offset(int(val))
        self._params = self.radar.get_parameters()

    @property
    def tracking_filter(self) -> int:
        if self._params is None:
            self._params = self.radar.get_parameters()
        return self._params.tracking_filter

    @tracking_filter.setter
    def tracking_filter(self, val: int) -> None:
        self.radar.set_tracking_filter(int(val))
        self._params = self.radar.get_parameters()

    @property
    def min_speed_filter(self) -> int:
        if self._params is None:
            self._params = self.radar.get_parameters()
        return self._params.min_zone_speed_pct

    @min_speed_filter.setter
    def min_speed_filter(self, val: int) -> None:
        self.radar.set_min_speed_filter(int(val))
        self._params = self.radar.get_parameters()

    @property
    def direction_filter(self) -> int:
        if self._params is None:
            self._params = self.radar.get_parameters()
        return self._params.direction_filter

    @direction_filter.setter
    def direction_filter(self, val: int) -> None:
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
        }

    def get_stream_stats(self) -> Dict[str, Any]:
        """Returns frame rate and streaming statistics."""
        return {
            "total_frames": self._total_frames,
            "dropped_frames": self._dropped_frames,
            "total_bytes_read": self._total_bytes_read,
            "current_fps": round(self._fps_ema, 1),
            "is_streaming": self.is_running,
        }

    def get_radar_parameters(self) -> Dict[str, Any]:
        """Returns complete parsed radar parameters (RPST)."""
        self._params = self.radar.get_parameters()
        p = self._params
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
            "mode": self._mode,
        }

    def start(self) -> bool:
        """Start streaming radar frames."""
        if not self.radar.serial or not self.radar.serial.is_open:
            self.radar.connect()
        if not self.radar.is_streaming:
            self.radar.start_streaming(raw_targets=True, tracked_targets=True)
        self._is_running = True
        return True

    def stop(self) -> bool:
        """Stop radar streaming."""
        if self.radar.is_streaming:
            try:
                self.radar.disconnect()
            except Exception:
                pass
        self._is_running = False
        return True

    def flush(self) -> None:
        """Flush internal buffers."""
        if self.radar.serial and self.radar.serial.is_open:
            self.radar.serial.reset_input_buffer()
            self.radar._rx_buf.clear()

    def _read_next_frame(self, timeout: float = 1.0) -> Optional[RadarFrame]:
        """Internal helper to read and track frame statistics."""
        if not self.is_running:
            self.start()

        frame = self.radar.read_frame(timeout=timeout)
        now = time.time()

        if frame is not None:
            self._total_frames += 1
            if self._last_frame_time > 0.0:
                dt = now - self._last_frame_time
                if dt > 0.0:
                    inst_fps = 1.0 / dt
                    self._fps_ema = 0.9 * self._fps_ema + 0.1 * inst_fps if self._fps_ema > 0 else inst_fps
            self._last_frame_time = now
            return frame
        else:
            self._dropped_frames += 1
            return None

    def capture_point_cloud(self, timeout: float = 1.0) -> Dict[str, Any]:
        """
        Capture one frame and return structured point cloud dictionary with NumPy arrays.

        Returns:
            Dictionary containing:
              - frame_number (int)
              - timestamp (float)
              - num_raw_points (int)
              - num_tracked_targets (int)
              - raw (dict): 1D NumPy arrays (float32) for x, y, distance, speed, angle, magnitude
              - tracked (dict): 1D NumPy arrays for id (int32), x, y, distance, speed, angle, magnitude
        """
        frame = self._read_next_frame(timeout=timeout)
        if not frame:
            empty_f32 = np.empty(0, dtype=np.float32)
            empty_i32 = np.empty(0, dtype=np.int32)
            return {
                "frame_number": -1,
                "timestamp": time.time(),
                "num_raw_points": 0,
                "num_tracked_targets": 0,
                "raw": {
                    "x": empty_f32, "y": empty_f32, "distance": empty_f32,
                    "speed": empty_f32, "angle": empty_f32, "magnitude": empty_f32,
                },
                "tracked": {
                    "id": empty_i32, "x": empty_f32, "y": empty_f32, "distance": empty_f32,
                    "speed": empty_f32, "angle": empty_f32, "magnitude": empty_f32,
                },
            }

        # Build raw targets arrays
        n_raw = len(frame.raw_targets)
        raw_x = np.array([t.x for t in frame.raw_targets], dtype=np.float32)
        raw_y = np.array([t.y for t in frame.raw_targets], dtype=np.float32)
        raw_d = np.array([t.distance for t in frame.raw_targets], dtype=np.float32)
        raw_s = np.array([t.speed for t in frame.raw_targets], dtype=np.float32)
        raw_a = np.array([t.angle for t in frame.raw_targets], dtype=np.float32)
        raw_m = np.array([t.magnitude for t in frame.raw_targets], dtype=np.float32)

        # Build tracked targets arrays
        n_trk = len(frame.tracked_targets)
        trk_id = np.array([t.tracking_id or 0 for t in frame.tracked_targets], dtype=np.int32)
        trk_x = np.array([t.x for t in frame.tracked_targets], dtype=np.float32)
        trk_y = np.array([t.y for t in frame.tracked_targets], dtype=np.float32)
        trk_d = np.array([t.distance for t in frame.tracked_targets], dtype=np.float32)
        trk_s = np.array([t.speed for t in frame.tracked_targets], dtype=np.float32)
        trk_a = np.array([t.angle for t in frame.tracked_targets], dtype=np.float32)
        trk_m = np.array([t.magnitude for t in frame.tracked_targets], dtype=np.float32)

        return {
            "frame_number": frame.frame_number,
            "timestamp": frame.timestamp,
            "num_raw_points": n_raw,
            "num_tracked_targets": n_trk,
            "raw": {
                "x": raw_x, "y": raw_y, "distance": raw_d,
                "speed": raw_s, "angle": raw_a, "magnitude": raw_m,
            },
            "tracked": {
                "id": trk_id, "x": trk_x, "y": trk_y, "distance": trk_d,
                "speed": trk_s, "angle": trk_a, "magnitude": trk_m,
            },
        }

    def capture_points(
        self,
        timeout: float = 1.0,
        include_raw: bool = True,
        include_tracked: bool = True,
    ) -> np.ndarray:
        """
        Capture points as a 2D float32 NumPy array of shape (N, 6):
        Columns: [x, y, distance, speed, angle, magnitude].
        """
        frame = self._read_next_frame(timeout=timeout)
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
        frame = self._read_next_frame(timeout=timeout)
        if not frame or not frame.tracked_targets:
            return []

        targets = []
        for t in frame.tracked_targets:
            targets.append({
                "id": t.tracking_id,
                "distance_m": float(t.distance),
                "speed_kmh": float(t.speed),
                "speed_mps": float(t.speed_mps),
                "angle_deg": float(t.angle),
                "x_m": float(t.x),
                "y_m": float(t.y),
                "power_db": float(t.magnitude),
                "motion": "receding" if t.speed > 0.5 else ("approaching" if t.speed < -0.5 else "stationary"),
            })
        return targets

    def read_frame_raw(self, timeout: float = 1.0) -> Dict[str, Any]:
        """Return raw decoded frame representation."""
        frame = self._read_next_frame(timeout=timeout)
        if not frame:
            return {"frame_number": -1, "timestamp": time.time(), "raw_count": 0, "tracked_count": 0}
        return {
            "frame_number": frame.frame_number,
            "timestamp": frame.timestamp,
            "raw_count": len(frame.raw_targets),
            "tracked_count": len(frame.tracked_targets),
        }


# -----------------------------------------------------------------------------
# RemoteRF Device Schema Class
# -----------------------------------------------------------------------------
@idl_register("rfbeam_kmd7")
class RFBeamKMD7Schema(DeviceSchema):
    """
    RemoteRF Device Schema for RFbeam K-MD7 24 GHz Digital Radar Transceivers.

    Exposes configuration, real-time telemetry, 2D Cartesian point clouds,
    and tracked target data to remote Python clients.
    """
    device_type = "rfbeam_kmd7"
    client_class = "RFBeamKMD7"
    driver_version = "0.1.0"

    client_modules = {"rfbeam_kmd7": "remoteRF.drivers.support.rfbeam_kmd7"}

    @staticmethod
    def make_device(**kwargs) -> Optional[RFBeamKMD7Device]:
        """
        Factory method called by RemoteRF server/host when initializing device inventory.

        Parameters from devices.yml 'init' dictionary:
            port (str, optional): Serial port path (default: auto-detected FTDI USB)
            baudrate (int, optional): Baud rate (default: 115200)
            mode (str, optional): 'indoor' (default) or 'traffic'
            max_range (float, optional): 100, 200, 300 meters
            max_speed (float, optional): 50, 100, 200 km/h
            threshold_offset (int, optional): 0-60 dB
            auto_start (bool, optional): Start streaming automatically (default: True)

        Returns:
            RFBeamKMD7Device instance if connection succeeds, or None if hardware is unavailable.
        """
        port = kwargs.get("port")
        baudrate = int(kwargs.get("baudrate", 115200))
        mode = kwargs.get("mode", "indoor")
        max_range = kwargs.get("max_range")
        max_speed = kwargs.get("max_speed")
        threshold_offset = kwargs.get("threshold_offset")
        auto_start = bool(kwargs.get("auto_start", True))

        # Auto-detect FTDI serial port if not explicitly provided
        if not port:
            port = KMD7Radar.find_serial_port()

        if not port:
            print("[rfbeam_kmd7] Could not locate RFbeam K-MD7 radar serial port.")
            print("[rfbeam_kmd7] Ensure the sensor is connected via USB and FTDI drivers are loaded.")
            return None

        try:
            print(f"[rfbeam_kmd7] Initializing RFbeam K-MD7 radar on {port} (Mode: '{mode}')...")
            dev = RFBeamKMD7Device(port=port, baudrate=baudrate, mode=mode)
            dev.connect()

            if max_range is not None:
                dev.max_range = float(max_range)
            if max_speed is not None:
                dev.max_speed = float(max_speed)
            if threshold_offset is not None:
                dev.threshold_offset = int(threshold_offset)

            if auto_start:
                dev.start()

            print(f"[rfbeam_kmd7] Successfully initialized on {port} (Firmware: {dev.radar.firmware_version}).")
            return dev

        except Exception as exc:
            print(f"[rfbeam_kmd7] Failed to initialize radar device: {exc}")
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

    @idl_expose(kind="get", doc="Complete 31-byte radar parameter structure (RPST).")
    def get_radar_parameters(self) -> Dict[str, Any]:
        return self.device.get_radar_parameters()

    # -------------------------------------------------------------------------
    # Exposed Getters & Setters (Properties writable directly on radar object)
    # -------------------------------------------------------------------------

    @idl_expose(kind="get", doc="Active operating mode ('indoor' for pedestrian/gestures, 'traffic' for vehicles).")
    def get_mode(self) -> str:
        return self.device.mode

    @idl_expose(kind="set", doc="Set active operating mode ('indoor' or 'traffic').")
    def set_mode(self, value: str) -> None:
        self.device.mode = str(value)

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

    @idl_expose(kind="get", doc="Minimum detection speed filter as percentage of speed setting (0 - 100%).")
    def get_min_speed_filter(self) -> int:
        return self.device.min_speed_filter

    @idl_expose(kind="set", doc="Set minimum detection speed filter percentage (0 - 100%).")
    def set_min_speed_filter(self, value: int) -> None:
        self.device.min_speed_filter = int(value)

    @idl_expose(kind="get", doc="Detection direction filter: 0=Receding, 1=Approaching, 2=Both.")
    def get_direction_filter(self) -> int:
        return self.device.direction_filter

    @idl_expose(kind="set", doc="Set detection direction filter: 0=Receding, 1=Approaching, 2=Both.")
    def set_direction_filter(self, value: int) -> None:
        self.device.direction_filter = int(value)

    # -------------------------------------------------------------------------
    # Exposed Calls
    # -------------------------------------------------------------------------

    @idl_expose(kind="call", doc="Capture structured 2D point cloud with positions, velocities, and power.")
    def call_capture_point_cloud(self, timeout: float = 1.0) -> Dict[str, Any]:
        """
        Captures one frame and returns detected points and tracked objects as structured NumPy arrays.
        """
        return self.device.capture_point_cloud(timeout=float(timeout))

    @idl_expose(kind="call", doc="Capture points as an (N, 6) NumPy array [x, y, dist, speed, angle, mag].")
    def call_capture_points(
        self,
        timeout: float = 1.0,
        *,
        include_raw: bool = True,
        include_tracked: bool = True,
    ) -> np.ndarray:
        """Returns detected points as an (N, 6) float32 array."""
        return self.device.capture_points(
            timeout=float(timeout),
            include_raw=bool(include_raw),
            include_tracked=bool(include_tracked),
        )

    @idl_expose(kind="call", doc="Capture active tracked targets as a list of structured dictionaries.")
    def call_capture_targets(self, timeout: float = 1.0) -> List[Dict[str, Any]]:
        """Returns list of active tracked targets with ID, Cartesian pos, velocity, and power."""
        return self.device.capture_targets(timeout=float(timeout))

    @idl_expose(kind="call", doc="Read one synchronized raw radar frame.")
    def call_read_frame(self, timeout: float = 1.0) -> Dict[str, Any]:
        return self.device.read_frame_raw(timeout=float(timeout))

    @idl_expose(kind="call", doc="Configure radar parameters via high-level presets.")
    def call_configure(
        self,
        mode: str = "indoor",
        *,
        max_range: Optional[float] = None,
        max_speed: Optional[float] = None,
        threshold_offset: Optional[int] = None,
        tracking_filter: Optional[int] = None,
        min_speed_filter: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Configure radar mode and optional parameters."""
        self.device.mode = mode
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
        return self.device.get_radar_parameters()

    @idl_expose(kind="call", doc="Restore factory default settings.")
    def call_restore_factory_settings(self) -> bool:
        self.device.radar.restore_factory_settings()
        self.device._params = self.device.radar.get_parameters()
        return True

    @idl_expose(kind="call", doc="Flush serial input buffer.")
    def call_flush_frames(self) -> bool:
        self.device.flush()
        return True

    @idl_expose(kind="call", doc="Start radar streaming.")
    def call_start(self) -> bool:
        return self.device.start()

    @idl_expose(kind="call", doc="Stop radar streaming.")
    def call_stop(self) -> bool:
        return self.device.stop()
