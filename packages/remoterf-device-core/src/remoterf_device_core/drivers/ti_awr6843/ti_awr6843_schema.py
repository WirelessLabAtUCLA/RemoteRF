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
RemoteRF Custom Device Schema for Texas Instruments mmWave Radars (AWR6843 / IWR6843).

Follows the RemoteRF Custom Device Specification:
https://remoterf.net/docs/server/device-setup/custom/

This schema bridges physical TI mmWave radar hardware to RemoteRF clients.
It exposes:
  - Readable device properties: status, port info, firmware profile, stream stats, config
  - Runtime setters: static clutter removal
  - Actions/Calls:
      * read_frame(): Raw UART packet bytes for RemoteRF's built-in decoder
      * capture_point_cloud(): Parsed 3D point cloud with positions, velocity, SNR as NumPy arrays
      * capture_points(): (N, 4) NumPy array [x, y, z, velocity]
      * capture_heatmap(): 2D Range-Azimuth heatmap power in dB with physical axis vectors
      * apply_config(): Upload multiline CLI config text
      * configure(): Configure radar via high-level presets (indoor-people, gesture, etc.)
      * send_command(): Send arbitrary CLI string to radar command port
      * query_version(): Query radar firmware version
      * flush_frames(): Discard buffered/stale serial data
      * start() / stop(): Control RF sensor transmission
"""

from __future__ import annotations

import hashlib
import importlib
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
from remoterf_device_core.drivers.ti_awr6843.mmwave.radar import Radar
from remoterf_device_core.drivers.ti_awr6843.mmwave.connection import RadarConnection, find_radar_ports
from remoterf_device_core.drivers.ti_awr6843.mmwave.parser import RadarFrame, TLVParser
from remoterf_device_core.drivers.ti_awr6843.mmwave.config_builder import build_config, preset_settings, PRESETS

# configs/ ships inside this driver package (also when an override copy loads from the drivers dir).
_PARENT_DIR = os.path.dirname(os.path.abspath(importlib.import_module("remoterf_device_core.drivers.ti_awr6843").__file__))

# Bounds on what a client may ask for in one call (under the 25 s Host tunnel timeout).
MAX_TIMEOUT_S = 20.0
MIN_ANGLE_BINS = 8
MAX_ANGLE_BINS = 1024
MAX_CONFIG_BYTES = 64 * 1024
MAX_CONFIG_LINES = 512


def _check_timeout(value: float, name: str = "timeout") -> float:
    """A finite timeout in (0, MAX_TIMEOUT_S] seconds, or ValueError."""
    val = float(value)
    if not math.isfinite(val) or val <= 0 or val > MAX_TIMEOUT_S:
        raise ValueError(f"{name} must be greater than 0 and at most {MAX_TIMEOUT_S:g} s, got {value!r}")
    return val


def _cli_ok(resp: str) -> bool:
    """True when a CLI response acknowledged the command without an Error."""
    return "Error" not in resp and ("Done" in resp or "mmwDemo:/>" in resp)


class TiMmWaveDevice:
    """
    Hardware controller managing the TI mmWave radar for RemoteRF.

    Wraps serial communication, configuration uploads, state management,
    frame parsing, and feature extraction (point cloud & range-azimuth heatmap).
    """

    def __init__(
        self,
        cli_port: Optional[str] = None,
        data_port: Optional[str] = None,
        cli_baud: int = 115200,
        data_baud: int = 921600,
    ):
        self.cli_port = cli_port
        self.data_port = data_port
        self.cli_baud = cli_baud
        self.data_baud = data_baud

        self.radar = Radar(
            cli_port=cli_port,
            data_port=data_port,
            cli_baud=cli_baud,
            data_baud=data_baud,
        )

        # radar._settings is the one record of preset/mode/clutter/fps/...;
        # this only notes where the last uploaded config came from.
        self._config_file: Optional[str] = None
        self._last_config_text: Optional[str] = None
        # What reset_for_next_holder restores (set by save_defaults()).
        self._defaults: Optional[Tuple[Dict[str, Any], Optional[str], Optional[str]]] = None

        # Frame and streaming statistics
        self._total_frames: int = 0
        self._total_bytes_read: int = 0
        self._last_frame_time: float = 0.0
        self._fps_ema: float = 0.0
        self._dropped_frames: int = 0

    @property
    def is_running(self) -> bool:
        return self.radar._is_running

    # -------------------------------------------------------------------------
    # Direct Setting Properties
    # -------------------------------------------------------------------------

    @property
    def clutter_removal(self) -> bool:
        """Static clutter removal filter state."""
        return self.radar.clutter_removal

    @clutter_removal.setter
    def clutter_removal(self, value: bool) -> None:
        self._ensure_connected()
        self.radar.clutter_removal = bool(value)

    def _apply_setting(self, **kwargs) -> None:
        """Validate a setting change, open serial ports if closed, upload, and clear _config_file."""
        self.radar._validate_settings(**kwargs)
        self._ensure_connected()
        cfg = self.radar.reconfigure(**kwargs)
        self._config_file = None
        self._last_config_text = cfg

    @property
    def fps(self) -> float:
        """Target frame capture rate in Hz (FPS)."""
        return self.radar.fps

    @fps.setter
    def fps(self, value: float) -> None:
        self._apply_setting(fps=float(value))

    @property
    def preset(self) -> str:
        """Active sensing preset ('default', 'indoor-people', 'gesture', 'fast-motion', 'heatmap')."""
        return self.radar.preset

    @preset.setter
    def preset(self, value: str) -> None:
        self._apply_setting(preset=str(value))

    @property
    def mode(self) -> str:
        """Chirp dimension mode ('3d' with elevation or '2d' azimuth-only)."""
        return self.radar.mode

    @mode.setter
    def mode(self, value: str) -> None:
        self._apply_setting(mode=str(value))

    @property
    def sensitivity(self) -> str:
        """CFAR detection threshold sensitivity ('low', 'medium', 'high')."""
        return self.radar.sensitivity

    @sensitivity.setter
    def sensitivity(self, value: str) -> None:
        self._apply_setting(sensitivity=str(value))

    @property
    def min_range(self) -> float:
        """Minimum detection range in meters."""
        return self.radar.min_range

    @min_range.setter
    def min_range(self, value: float) -> None:
        self._apply_setting(min_range=float(value))

    @property
    def max_range(self) -> float:
        """Maximum detection range in meters."""
        return self.radar.max_range

    @max_range.setter
    def max_range(self, value: float) -> None:
        self._apply_setting(max_range=float(value))

    def get_device_info(self) -> Dict[str, Any]:
        """Returns static and runtime connection information."""
        return {
            "device_type": "ti_awr6843",
            "model": "AWR6843 / IWR6843 (xWR68xx)",
            "firmware_profile": "xwr68xx_oob_sdk3",
            "cli_port": self.radar.connection.cli_port_name,
            "data_port": self.radar.connection.data_port_name,
            "cli_baud": self.cli_baud,
            "data_baud": self.data_baud,
            "is_running": self.is_running,
            "active_config": self.get_config(),
        }

    def get_stream_stats(self) -> Dict[str, Any]:
        """Returns streaming health, throughput, and frame counter metrics."""
        return {
            "total_frames": self._total_frames,
            "total_bytes_read": self._total_bytes_read,
            "current_fps": round(self._fps_ema, 2),
            "dropped_frames": self._dropped_frames,
            "buffer_bytes": len(self.radar.parser._buffer),
            "is_running": self.is_running,
        }

    def get_config(self) -> Dict[str, Any]:
        """Active configuration, derived from the radar's settings."""
        return {**self.radar._settings, "config_file": self._config_file}

    def _ensure_connected(self) -> None:
        """Opens the serial ports if they are not open (e.g. after close)."""
        if (
            not self.radar.connection.cli_ser
            or not self.radar.connection.cli_ser.is_open
            or not self.radar.connection.data_ser
            or not self.radar.connection.data_ser.is_open
        ):
            self.radar.connection.connect()

    def apply_config_text(
        self,
        config_text: str,
        start: bool = True,
        command_timeout: float = 5.0,
        config_file: Optional[str] = "custom_text",
    ) -> bool:
        """Applies raw newline-delimited TI mmWave CLI configuration commands.

        With start=False any sensorStart line is skipped, so the radar stays stopped.
        """
        command_timeout = _check_timeout(command_timeout, "command_timeout")
        if len(config_text.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ValueError(f"config_text is larger than {MAX_CONFIG_BYTES} bytes")
        if len(config_text.splitlines()) > MAX_CONFIG_LINES:
            raise ValueError(f"config_text has more than {MAX_CONFIG_LINES} lines")

        self._ensure_connected()
        self.radar.connection.send_config_text(
            config_text,
            verbose=False,
            start=start,
            command_timeout=command_timeout,
            total_timeout=MAX_TIMEOUT_S,
            on_command=self.radar._record_command,
        )
        self._config_file = config_file
        self._last_config_text = config_text
        return True

    def configure_preset(
        self,
        preset: str = "default",
        mode: Optional[str] = None,
        clutter_removal: Optional[bool] = None,
        fps: Optional[float] = None,
        start: bool = True,
    ) -> bool:
        """Builds configuration from high-level parameters and applies it to radar.

        mode, clutter_removal and fps left as None keep the preset's own values.
        """
        settings = self.radar._validate_settings(
            preset=preset, mode=mode, clutter_removal=clutter_removal, fps=fps,
        )

        previous = self.radar._settings
        self.radar._settings = settings
        try:
            cfg_text = self.radar._build_active_config()
            self.apply_config_text(cfg_text, start=start, config_file=None)
        except Exception:
            self.radar._settings = previous
            raise
        return True

    def save_defaults(self) -> None:
        """Remembers the current settings and config as what reset() restores."""
        self._defaults = (dict(self.radar._settings), self._config_file, self._last_config_text)

    def reset(self, timeout: float = 5.0) -> None:
        """Stops chirping, re-uploads the saved default config (stopped), and drops buffered frames.

        The serial ports stay open for the next caller.
        """
        timeout = _check_timeout(timeout)
        first_err: Optional[Exception] = None
        try:
            if not self.stop(timeout=timeout):
                first_err = RuntimeError("Radar did not acknowledge sensorStop")
        except Exception as exc:
            first_err = exc
        if self._defaults is not None:
            settings, config_file, config_text = self._defaults
            try:
                if config_text:
                    self.apply_config_text(
                        config_text,
                        start=False,
                        command_timeout=timeout,
                        config_file=config_file,
                    )
                self.radar._settings = dict(settings)
                self._config_file = config_file
            except Exception as exc:
                if first_err is None:
                    first_err = exc
        try:
            self.flush_frames()
        except Exception as exc:
            if first_err is None:
                first_err = exc
        if first_err is not None:
            raise first_err

    def send_command(self, command: str, timeout: float = 2.0) -> str:
        """Sends a single CLI command to the radar CLI port and returns response."""
        timeout = _check_timeout(timeout)
        self._ensure_connected()
        return self.radar.connection.send_command(command, timeout=timeout)

    def query_version(self, timeout: float = 2.0) -> str:
        """Queries firmware version string from radar CLI."""
        return self.send_command("version", timeout=timeout)

    def set_clutter_removal(self, enabled: bool) -> bool:
        """Enables or disables static clutter removal filter at runtime."""
        val = 1 if enabled else 0
        resp = self.send_command(f"clutterRemoval -1 {val}")
        ok = _cli_ok(resp)
        if ok:
            self.radar._settings["clutter_removal"] = bool(enabled)
        return ok

    def flush_frames(self) -> int:
        """Discards queued serial bytes and resets the internal parser buffer."""
        discarded = len(self.radar.parser._buffer)
        self.radar.flush()
        return discarded

    def read_frame_raw(self, timeout: float = 1.0) -> bytes:
        """
        Reads one complete raw TI UART packet as bytes.
        Matches the RemoteRF built-in client decoder contract.
        """
        frame = self._read_next_frame(timeout=timeout)
        if frame and frame.raw_packet:
            return frame.raw_packet
        return b""

    def capture_point_cloud(self, timeout: float = 1.0) -> Dict[str, Any]:
        """
        Captures one frame and returns detected 3D points as NumPy arrays.

        Returns:
            Dict containing:
                frame_number: int
                timestamp: float
                num_points: int
                x: 1D np.ndarray (float32) in meters (+Right, -Left)
                y: 1D np.ndarray (float32) in meters (+Forward)
                z: 1D np.ndarray (float32) in meters (+Up, -Down)
                velocity: 1D np.ndarray (float32) in m/s (+Away, -Toward)
                range: 1D np.ndarray (float32) in meters
                azimuth_deg: 1D np.ndarray (float32) in degrees
                elevation_deg: 1D np.ndarray (float32) in degrees
                snr_db: 1D np.ndarray (float32) in dB
        """
        frame = self._read_next_frame(timeout=timeout)
        if not frame:
            return {
                "frame_number": -1,
                "timestamp": time.time(),
                "num_points": 0,
                "x": np.empty(0, dtype=np.float32),
                "y": np.empty(0, dtype=np.float32),
                "z": np.empty(0, dtype=np.float32),
                "velocity": np.empty(0, dtype=np.float32),
                "range": np.empty(0, dtype=np.float32),
                "azimuth_deg": np.empty(0, dtype=np.float32),
                "elevation_deg": np.empty(0, dtype=np.float32),
                "snr_db": np.empty(0, dtype=np.float32),
            }

        pts = frame.points
        n = len(pts)
        if n == 0:
            return {
                "frame_number": frame.frame_number,
                "timestamp": frame.timestamp,
                "num_points": 0,
                "x": np.empty(0, dtype=np.float32),
                "y": np.empty(0, dtype=np.float32),
                "z": np.empty(0, dtype=np.float32),
                "velocity": np.empty(0, dtype=np.float32),
                "range": np.empty(0, dtype=np.float32),
                "azimuth_deg": np.empty(0, dtype=np.float32),
                "elevation_deg": np.empty(0, dtype=np.float32),
                "snr_db": np.empty(0, dtype=np.float32),
            }

        return {
            "frame_number": frame.frame_number,
            "timestamp": frame.timestamp,
            "num_points": n,
            "x": np.array([p.x for p in pts], dtype=np.float32),
            "y": np.array([p.y for p in pts], dtype=np.float32),
            "z": np.array([p.z for p in pts], dtype=np.float32),
            "velocity": np.array([p.velocity for p in pts], dtype=np.float32),
            "range": np.array([p.range for p in pts], dtype=np.float32),
            "azimuth_deg": np.array([p.azimuth_deg for p in pts], dtype=np.float32),
            "elevation_deg": np.array([p.elevation_deg for p in pts], dtype=np.float32),
            "snr_db": np.array([p.snr_db for p in pts], dtype=np.float32),
        }

    def capture_points(self, timeout: float = 1.0) -> np.ndarray:
        """
        Captures one frame and returns an (N, 4) float32 array: [x, y, z, velocity].
        Returns an empty (0, 4) array if no points were detected.
        """
        cloud = self.capture_point_cloud(timeout=timeout)
        n = cloud["num_points"]
        if n == 0:
            return np.empty((0, 4), dtype=np.float32)
        return np.column_stack((cloud["x"], cloud["y"], cloud["z"], cloud["velocity"])).astype(np.float32)

    def capture_heatmap(
        self,
        num_angle_bins: int = 64,
        max_range: float = 6.0,
        fov_deg: float = 60.0,
        range_resolution_m: float = 0.044,
        timeout: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Captures one frame containing azimuth static heatmap (TLV 4) and
        performs 2D Angle-FFT beamforming.

        Returns:
            Dict containing:
                frame_number: int
                timestamp: float
                range_grid: 1D np.ndarray of range coordinates in meters
                azimuth_grid_deg: 1D np.ndarray of azimuth angle coordinates in degrees
                heatmap_db: 2D np.ndarray (float32) of shape [num_ranges, num_angles] in dB
        """
        if not MIN_ANGLE_BINS <= int(num_angle_bins) <= MAX_ANGLE_BINS:
            raise ValueError(f"num_angle_bins must be between {MIN_ANGLE_BINS} and {MAX_ANGLE_BINS}, got {num_angle_bins}")
        num_angle_bins = int(num_angle_bins)
        frame = self._read_next_frame(timeout=timeout)
        if not frame or frame.azimuth_static_heatmap is None or frame.azimuth_static_heatmap.ndim != 2:
            return {
                "frame_number": frame.frame_number if frame else -1,
                "timestamp": frame.timestamp if frame else time.time(),
                "range_grid": np.empty(0, dtype=np.float32),
                "azimuth_grid_deg": np.empty(0, dtype=np.float32),
                "heatmap_db": np.empty((0, 0), dtype=np.float32),
            }

        c_data = frame.azimuth_static_heatmap
        num_range_bins = c_data.shape[0]

        # Calculate physical axes
        ranges = np.arange(num_range_bins, dtype=np.float32) * float(range_resolution_m)
        range_mask = ranges <= float(max_range)
        filtered_ranges = ranges[range_mask]

        sin_max = math.sin(math.radians(fov_deg))
        sin_angles = np.linspace(-sin_max, sin_max, num_angle_bins)
        angles_deg = np.degrees(np.arcsin(sin_angles)).astype(np.float32)

        # Beamforming Angle-FFT across virtual antennas
        c_subset = c_data[range_mask, :]
        n_ant = c_subset.shape[1]
        window = np.hanning(n_ant)
        windowed = c_subset * window[None, :]

        angle_fft = np.fft.fftshift(
            np.fft.fft(windowed, n=num_angle_bins, axis=1),
            axes=1,
        )

        power_db = (20.0 * np.log10(np.abs(angle_fft) + 1e-4)).astype(np.float32)

        return {
            "frame_number": frame.frame_number,
            "timestamp": frame.timestamp,
            "range_grid": filtered_ranges,
            "azimuth_grid_deg": angles_deg,
            "heatmap_db": power_db,
        }

    def start(self, timeout: float = 5.0) -> bool:
        """Starts radar chirping via sensorStart."""
        resp = self.send_command("sensorStart", timeout=timeout)
        ok = _cli_ok(resp)
        if ok:
            self.radar._is_running = True
        return ok

    def stop(self, timeout: float = 5.0) -> bool:
        """Stops radar chirping via sensorStop; the serial ports stay open."""
        resp = self.send_command("sensorStop", timeout=timeout)
        ok = _cli_ok(resp)
        if ok:
            self.radar._is_running = False
        self.radar.flush()
        return ok

    def close(self) -> None:
        """Stops radar chirping and closes both serial ports."""
        self.radar.stop()

    def _read_next_frame(self, timeout: float = 1.0) -> Optional[RadarFrame]:
        """Internal helper to read and track frame statistics."""
        timeout = _check_timeout(timeout)
        deadline = time.time() + timeout
        while True:
            chunk = self.radar.connection.read_data(max_bytes=4096)
            if chunk:
                self._total_bytes_read += len(chunk)
                now = time.time()
                frames = self.radar.parser.feed(chunk, timestamp=now)
                if frames:
                    frame = frames[0]
                    self._total_frames += 1

                    # Update FPS EMA
                    if self._last_frame_time > 0.0:
                        dt = now - self._last_frame_time
                        if dt > 0.0:
                            inst_fps = 1.0 / dt
                            self._fps_ema = 0.9 * self._fps_ema + 0.1 * inst_fps if self._fps_ema > 0 else inst_fps
                    self._last_frame_time = now

                    return frame
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(0.005, remaining))

        self._dropped_frames += 1
        return None


# -----------------------------------------------------------------------------
# RemoteRF Device Schema Class
# -----------------------------------------------------------------------------
@idl_register("ti_awr6843")
class TiMmWaveSchema(DeviceSchema):
    """
    RemoteRF Device Schema for TI mmWave FMCW Radars (AWR6843 / IWR6843).

    Exposes configuration, control, raw UART byte frames, structured 3D point
    clouds, and 2D range-azimuth heatmaps to remote Python clients.
    """
    device_type = "ti_awr6843"
    client_class = "TiMmWave"
    driver_version = "0.1.1"

    # Enables client to import remoteRF.drivers.support.ti_mmwave for local frame decoding
    client_modules = {"ti_mmwave": "remoteRF.drivers.support.ti_mmwave"}

    @staticmethod
    def make_device(**kwargs) -> Optional[TiMmWaveDevice]:
        """
        Factory method called by RemoteRF server/host when initializing device inventory.

        Parameters from devices.yml 'init' dictionary:
            cli_port (str, optional): CLI serial port path (default: auto-detected)
            data_port (str, optional): Data serial port path (default: auto-detected)
            cli_baud (int, optional): Baud rate for CLI (default: 115200)
            data_baud (int, optional): Baud rate for Data (default: 921600)
            config_file (str, optional): Path to .cfg profile file
            config_text (str, optional): Multiline string of radar CLI commands
            preset (str, optional): 'default', 'indoor-people', 'gesture', 'fast-motion', 'heatmap'
            mode (str, optional): '3d' or '2d' (default: the preset's)
            clutter_removal (bool, optional): Enable static clutter filter (default: the preset's)
            fps (float, optional): Frame rate in Hz (default: the preset's)
            auto_start (bool, optional): Start chirping immediately (default: True)

        Returns:
            TiMmWaveDevice instance if connection succeeds, or None if hardware is unavailable.
        """
        cli_port = kwargs.get("cli_port")
        data_port = kwargs.get("data_port")
        cli_baud = int(kwargs.get("cli_baud", 115200))
        data_baud = int(kwargs.get("data_baud", 921600))
        preset = kwargs.get("preset", "default")
        mode = kwargs.get("mode")
        clutter_removal = kwargs.get("clutter_removal")
        clutter_removal = None if clutter_removal is None else bool(clutter_removal)
        fps = kwargs.get("fps")
        fps = None if fps is None else float(fps)
        config_file = kwargs.get("config_file")
        config_text = kwargs.get("config_text")
        auto_start = bool(kwargs.get("auto_start", True))

        # Auto-detect ports if not explicitly specified. Guessing the first two
        # ttyUSB/ttyACM devices is only done when devices.yml names neither port.
        if not cli_port or not data_port:
            detected_cli, detected_data = find_radar_ports(fallback=not (cli_port or data_port))
            cli_port = cli_port or detected_cli
            data_port = data_port or detected_data

        if not cli_port or not data_port:
            print("[ti_mmwave] Could not locate TI mmWave serial ports (/dev/ttyUSB* or /dev/serial/by-id).")
            print("[ti_mmwave] Ensure the radar is connected over USB and CP2105 drivers are installed.")
            return None

        device: Optional[TiMmWaveDevice] = None
        try:
            print(f"[ti_mmwave] Initializing TI mmWave radar (CLI: {cli_port}, Data: {data_port})...")
            device = TiMmWaveDevice(
                cli_port=cli_port,
                data_port=data_port,
                cli_baud=cli_baud,
                data_baud=data_baud,
            )

            # Apply initial configuration
            if config_text:
                device.apply_config_text(config_text, start=auto_start)
            elif config_file:
                cfg_path = config_file if os.path.isabs(config_file) else os.path.join(_PARENT_DIR, config_file)
                with open(cfg_path, "r") as f:
                    device.apply_config_text(f.read(), start=auto_start, config_file=config_file)
            else:
                device.configure_preset(
                    preset=preset,
                    mode=mode,
                    clutter_removal=clutter_removal,
                    fps=fps,
                    start=auto_start,
                )
            device.save_defaults()

            print(f"[ti_mmwave] Radar successfully initialized (Preset: '{device.preset}', Mode: '{device.mode}').")
            return device

        except Exception as exc:
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass
            print(f"[ti_mmwave] Failed to initialize radar device: {exc}")
            print("[ti_mmwave] Hint: In TI mmWave SDK 3.x, if the sensor was previously stopped,")
            print("[ti_mmwave] press the physical NRST/RESET switch (SW2) on the board or replug USB.")
            return None

    # -------------------------------------------------------------------------
    # Exposed Getters
    # -------------------------------------------------------------------------

    @idl_expose(kind="get", doc="Whether the radar is active and transmitting.")
    def get_is_running(self) -> bool:
        return self.device.is_running

    @idl_expose(kind="get", doc="Serial ports, identity, and firmware profile.")
    def get_device_info(self) -> Dict[str, Any]:
        return self.device.get_device_info()

    @idl_expose(kind="get", doc="Firmware profile identifier (e.g. xwr68xx_oob_sdk3).")
    def get_firmware_profile(self) -> str:
        return "xwr68xx_oob_sdk3"

    @idl_expose(kind="get", doc="Frame rate, packet counts, and reader buffer health counters.")
    def get_stream_stats(self) -> Dict[str, Any]:
        return self.device.get_stream_stats()

    @idl_expose(kind="get", doc="Active configuration parameters (preset, mode, clutter, fps).")
    def get_config(self) -> Dict[str, Any]:
        return self.device.get_config()

    # -------------------------------------------------------------------------
    # Exposed Getters & Setters (Properties writable directly on radar object)
    # -------------------------------------------------------------------------

    @idl_expose(kind="get", doc="Static clutter removal filter state (True = filter walls/furniture).")
    def get_clutter_removal(self) -> bool:
        return self.device.clutter_removal

    @idl_expose(kind="set", doc="Enable or disable static clutter removal filter at runtime.")
    def set_clutter_removal(self, value: bool) -> None:
        self.device.clutter_removal = bool(value)

    @idl_expose(kind="get", doc="Target frame capture rate in Hz (FPS).")
    def get_fps(self) -> float:
        return self.device.fps

    @idl_expose(kind="set", doc="Set radar target frame rate in Hz (e.g. 10.0, 15.0, 30.0).")
    def set_fps(self, value: float) -> None:
        self.device.fps = float(value)

    @idl_expose(kind="get", doc="Active sensing profile preset ('default', 'indoor-people', 'gesture', 'fast-motion', 'heatmap').")
    def get_preset(self) -> str:
        return self.device.preset

    @idl_expose(kind="set", doc="Switch radar configuration to a preset profile.")
    def set_preset(self, value: str) -> None:
        self.device.preset = str(value)

    @idl_expose(kind="get", doc="Chirp dimension mode ('3d' with elevation or '2d' azimuth-only).")
    def get_mode(self) -> str:
        return self.device.mode

    @idl_expose(kind="set", doc="Set chirp dimension mode ('3d' or '2d').")
    def set_mode(self, value: str) -> None:
        self.device.mode = str(value)

    @idl_expose(kind="get", doc="CFAR detection threshold sensitivity ('low', 'medium', 'high').")
    def get_sensitivity(self) -> str:
        return self.device.sensitivity

    @idl_expose(kind="set", doc="Set CFAR detection threshold sensitivity ('low', 'medium', 'high').")
    def set_sensitivity(self, value: str) -> None:
        self.device.sensitivity = str(value)

    @idl_expose(kind="get", doc="Minimum detection range in meters.")
    def get_min_range(self) -> float:
        return self.device.min_range

    @idl_expose(kind="set", doc="Set minimum detection range in meters.")
    def set_min_range(self, value: float) -> None:
        self.device.min_range = float(value)

    @idl_expose(kind="get", doc="Maximum detection range in meters.")
    def get_max_range(self) -> float:
        return self.device.max_range

    @idl_expose(kind="set", doc="Set maximum detection range in meters.")
    def set_max_range(self, value: float) -> None:
        self.device.max_range = float(value)

    # -------------------------------------------------------------------------
    # Exposed Calls
    # -------------------------------------------------------------------------

    @idl_expose(kind="call", doc="Return one complete raw TI UART packet as bytes.")
    def call_read_frame(self, timeout: float = 1.0) -> bytes:
        """
        Reads one raw UART packet.
        Can be decoded on the client using remoteRF.drivers.support.ti_mmwave.decode_frame().
        """
        return self.device.read_frame_raw(timeout=float(timeout))

    @idl_expose(kind="call", doc="Capture detected 3D point cloud with position, velocity, and SNR.")
    def call_capture_point_cloud(self, timeout: float = 1.0) -> Dict[str, Any]:
        """
        Captures one frame and returns detected points as structured NumPy arrays.
        """
        return self.device.capture_point_cloud(timeout=float(timeout))

    @idl_expose(kind="call", doc="Capture detected points as an (N, 4) NumPy array [x, y, z, velocity].")
    def call_capture_points(self, timeout: float = 1.0) -> np.ndarray:
        """Returns detected points as an (N, 4) float32 array."""
        return self.device.capture_points(timeout=float(timeout))

    @idl_expose(kind="call", doc="Capture 2D Range-Azimuth heatmap with coordinate vectors in dB.")
    def call_capture_heatmap(
        self,
        num_angle_bins: int = 64,
        max_range: float = 6.0,
        timeout: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Captures azimuth static heatmap and computes 2D Angle-FFT power in dB.
        num_angle_bins is 8..1024; timeout at most 30 s.
        """
        return self.device.capture_heatmap(
            num_angle_bins=int(num_angle_bins),
            max_range=float(max_range),
            timeout=float(timeout),
        )

    @idl_expose(kind="call", doc="Apply newline-delimited TI CLI configuration commands in order.")
    def call_apply_config(
        self,
        config_text: str,
        start: bool = True,
        command_timeout: float = 5.0,
    ) -> bool:
        """Uploads custom multiline CLI configuration commands."""
        return self.device.apply_config_text(
            config_text=str(config_text),
            start=bool(start),
            command_timeout=float(command_timeout),
        )

    @idl_expose(kind="call", doc="Configure radar parameters via presets (indoor-people, gesture, fast-motion).")
    def call_configure(
        self,
        preset: str = "default",
        mode: Optional[str] = None,
        clutter_removal: Optional[bool] = None,
        fps: Optional[float] = None,
        start: bool = True,
    ) -> bool:
        """Reconfigures the radar with preset settings; mode/clutter_removal/fps
        left as None keep the preset's values. start=False leaves it stopped."""
        return self.device.configure_preset(
            preset=str(preset),
            mode=None if mode is None else str(mode),
            clutter_removal=None if clutter_removal is None else bool(clutter_removal),
            fps=None if fps is None else float(fps),
            start=bool(start),
        )

    @idl_expose(kind="call", doc="Send one runtime CLI command and return the textual response.")
    def call_send_command(self, command: str, timeout: float = 2.0) -> str:
        """Sends single CLI command string (e.g. 'version', 'sensorStop')."""
        return self.device.send_command(str(command), timeout=float(timeout))

    @idl_expose(kind="call", doc="Query radar firmware version string.")
    def call_query_version(self, timeout: float = 2.0) -> str:
        """Queries radar firmware version."""
        return self.device.query_version(timeout=float(timeout))

    @idl_expose(kind="call", doc="Discard queued and partially assembled data frames.")
    def call_flush_frames(self) -> int:
        """Flushes input buffers."""
        return self.device.flush_frames()

    @idl_expose(kind="call", doc="Start radar RF transmission and data streaming.")
    def call_start(self, timeout: float = 5.0) -> bool:
        """Sends sensorStart."""
        return self.device.start(timeout=float(timeout))

    @idl_expose(kind="call", doc="Stop radar RF transmission.")
    def call_stop(self, timeout: float = 5.0) -> bool:
        """Sends sensorStop."""
        return self.device.stop(timeout=float(timeout))

    @idl_expose(kind="call", doc="Reset radar state and flush buffers.")
    def call_reset(self) -> bool:
        """Flushes parser and resets driver state."""
        self.device.flush_frames()
        return True

    def reset_for_next_holder(self):
        """A reservation ended: stop chirping, restore the default config and drop unread frames."""
        if self.device is None:
            return
        self.device.reset()


# -----------------------------------------------------------------------------
# Module Self-Test and Verification
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 65)
    print("RemoteRF Custom Device Schema: TI mmWave Radar (AWR6843)")
    print("=" * 65)

    schema = TiMmWaveSchema()
    exposed = schema.list_exposed()

    print(f"Device Type     : {schema.device_type}")
    print(f"Client Class    : {schema.client_class}")
    print(f"Driver Version  : {schema.driver_version}")
    print(f"Client Modules  : {schema.client_modules}")
    print("-" * 65)
    print("Exposed Getters :", list(exposed["getters"].keys()))
    print("Exposed Setters :", list(exposed["setters"].keys()))
    print("Exposed Calls   :", list(exposed["calls"].keys()))
    print("-" * 65)

    idl_str = schema.get_idl_json(pretty=True)
    idl_dict = json.loads(idl_str)
    print(f"Generated Schema Hash : {idl_dict['schema_hash']}")
    print("IDL Schema Validation Passed.")
    print("=" * 65)
