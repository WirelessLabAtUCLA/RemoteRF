import math
import os
import time
from typing import Any, Dict, Generator, Optional

from .connection import RadarConnection
from .parser import RadarFrame, TLVParser
from .config_builder import build_config, preset_settings, PRESETS


class Radar:
    """
    High-level controller for TI mmWave Radar (AWR6843 / IWR6843).

    Supports direct property manipulation (e.g. radar.clutter_removal = True, radar.fps = 20.0),
    preset profiles, raw frame capture, and live streaming.

    Usage:
        radar = Radar()
        radar.start(preset="indoor-people")
        radar.clutter_removal = True  # Dynamic runtime toggle
        radar.fps = 15.0              # Reconfigures target frame rate
        for frame in radar.stream():
            print(f"Frame #{frame.frame_number}: {len(frame.points)} points")
        radar.stop()
    """

    def __init__(
        self,
        cli_port: Optional[str] = None,
        data_port: Optional[str] = None,
        cli_baud: int = 115200,
        data_baud: int = 921600,
    ):
        self.connection = RadarConnection(
            cli_port=cli_port,
            data_port=data_port,
            cli_baud=cli_baud,
            data_baud=data_baud,
        )
        self.parser = TLVParser()
        self._is_running = False

        # Active configuration state
        self._settings: Dict[str, Any] = {
            "preset": "default",
            "mode": "3d",
            "clutter_removal": False,
            "fps": 10.0,
            "sensitivity": "medium",
            "min_range": 0.25,
            "max_range": 8.0,
        }

    # -------------------------------------------------------------------------
    # Direct Setting Properties
    # -------------------------------------------------------------------------

    @property
    def clutter_removal(self) -> bool:
        """Static clutter removal filter state (True = walls/furniture filtered out)."""
        return bool(self._settings.get("clutter_removal", False))

    @clutter_removal.setter
    def clutter_removal(self, value: bool):
        val = bool(value)
        if self.connection.cli_ser and self.connection.cli_ser.is_open:
            # clutterRemoval is a TI runtime command: -1=all subframes, 1=enable, 0=disable
            resp = self.connection.send_command(f"clutterRemoval -1 {1 if val else 0}")
            if "Error" in resp or not ("Done" in resp or "mmwDemo:/>" in resp):
                raise RuntimeError(f"Radar did not accept clutterRemoval command: {resp!r}")
        self._settings["clutter_removal"] = val

    @property
    def fps(self) -> float:
        """Target frame capture rate in Hz (FPS)."""
        return float(self._settings.get("fps", 10.0))

    @fps.setter
    def fps(self, value: float):
        self.reconfigure(fps=value)

    @property
    def preset(self) -> str:
        """Active sensing preset ('default', 'indoor-people', 'gesture', 'fast-motion', 'heatmap')."""
        return str(self._settings.get("preset", "default"))

    @preset.setter
    def preset(self, value: str):
        self.reconfigure(preset=value)

    @property
    def mode(self) -> str:
        """Chirp dimension mode ('3d' with elevation or '2d' azimuth-only)."""
        return str(self._settings.get("mode", "3d"))

    @mode.setter
    def mode(self, value: str):
        self.reconfigure(mode=value)

    @property
    def sensitivity(self) -> str:
        """CFAR detection threshold sensitivity ('low', 'medium', 'high')."""
        return str(self._settings.get("sensitivity", "medium"))

    @sensitivity.setter
    def sensitivity(self, value: str):
        self.reconfigure(sensitivity=value)

    @property
    def min_range(self) -> float:
        """Minimum detection range in meters."""
        return float(self._settings.get("min_range", 0.25))

    @min_range.setter
    def min_range(self, value: float):
        self.reconfigure(min_range=value)

    @property
    def max_range(self) -> float:
        """Maximum detection range in meters."""
        return float(self._settings.get("max_range", 8.0))

    @max_range.setter
    def max_range(self, value: float):
        self.reconfigure(max_range=value)

    # -------------------------------------------------------------------------
    # Lifecycle & Control
    # -------------------------------------------------------------------------

    def start(
        self,
        config_filepath: Optional[str] = None,
        config_text: Optional[str] = None,
        preset: Optional[str] = None,
        mode: Optional[str] = None,
        clutter_removal: Optional[bool] = None,
        fps: Optional[float] = None,
        verbose: bool = False,
    ):
        """Connects to ports, uploads configuration, and starts radar chirping."""
        self.connection.connect()

        if config_text:
            self.connection.send_config_text(config_text, verbose=verbose)
        elif config_filepath:
            self.connection.send_config_file(config_filepath, verbose=verbose)
        else:
            # Build and send configuration from settings/preset
            if preset is not None:
                self._settings["preset"] = preset
            if mode is not None:
                self._settings["mode"] = mode
            if clutter_removal is not None:
                self._settings["clutter_removal"] = clutter_removal
            if fps is not None:
                self._settings["fps"] = float(fps)

            cfg = self._build_active_config()
            self.connection.send_config_text(cfg, verbose=verbose)

        self._is_running = True

    def _validate_settings(
        self,
        *,
        preset: Optional[str] = None,
        mode: Optional[str] = None,
        clutter_removal: Optional[bool] = None,
        fps: Optional[float] = None,
        sensitivity: Optional[str] = None,
        min_range: Optional[float] = None,
        max_range: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Validate and compute candidate settings without mutating self._settings."""
        candidate = dict(self._settings)
        if preset is not None:
            preset_val = str(preset).lower()
            if preset_val not in PRESETS:
                raise ValueError(f"Unknown preset {preset!r}; expected one of {sorted(PRESETS)}")
            candidate.update(preset_settings(preset_val))
            candidate["preset"] = preset_val
        if mode is not None:
            mode_val = str(mode).lower()
            if mode_val not in ("3d", "2d"):
                raise ValueError("Mode must be '3d' or '2d'")
            candidate["mode"] = mode_val
        if clutter_removal is not None:
            candidate["clutter_removal"] = bool(clutter_removal)
        if fps is not None:
            fps_val = float(fps)
            if not (math.isfinite(fps_val) and fps_val > 0):
                raise ValueError(f"FPS must be a finite positive number, got {fps!r}")
            candidate["fps"] = fps_val
        if sensitivity is not None:
            sens_val = str(sensitivity).lower()
            if sens_val not in ("low", "medium", "high"):
                raise ValueError(f"Sensitivity must be 'low', 'medium', or 'high', got {sensitivity!r}")
            candidate["sensitivity"] = sens_val
        if min_range is not None:
            min_r = float(min_range)
            if not (math.isfinite(min_r) and min_r >= 0):
                raise ValueError(f"min_range must be a finite non-negative number, got {min_range!r}")
            candidate["min_range"] = min_r
        if max_range is not None:
            max_r = float(max_range)
            if not (math.isfinite(max_r) and max_r > 0):
                raise ValueError(f"max_range must be a finite positive number, got {max_range!r}")
            candidate["max_range"] = max_r

        if candidate["min_range"] >= candidate["max_range"]:
            raise ValueError(
                f"min_range ({candidate['min_range']}) must be less than max_range ({candidate['max_range']})"
            )
        if candidate.get("preset") == "heatmap" and candidate.get("mode") != "3d":
            raise ValueError("Preset 'heatmap' requires mode='3d'")
        return candidate

    def reconfigure(
        self,
        preset: Optional[str] = None,
        mode: Optional[str] = None,
        clutter_removal: Optional[bool] = None,
        fps: Optional[float] = None,
        sensitivity: Optional[str] = None,
        min_range: Optional[float] = None,
        max_range: Optional[float] = None,
        start: Optional[bool] = None,
    ) -> str:
        """
        Updates radar settings and re-uploads full configuration to radar.
        """
        candidate = self._validate_settings(
            preset=preset,
            mode=mode,
            clutter_removal=clutter_removal,
            fps=fps,
            sensitivity=sensitivity,
            min_range=min_range,
            max_range=max_range,
        )
        previous = self._settings
        self._settings = candidate
        try:
            cfg = self._build_active_config()
            # Only send to hardware if connection is already open
            if self.connection.cli_ser and self.connection.cli_ser.is_open:
                was_running = self._is_running if start is None else bool(start)
                self.connection.send_config_text(
                    cfg, verbose=False, start=was_running, total_timeout=20.0,
                )
                self._is_running = was_running
                self.flush()
            return cfg
        except Exception:
            self._settings = previous
            raise

    def _build_active_config(self) -> str:
        """Helper to generate full configuration script from current settings."""
        preset = self._settings.get("preset", "default")
        mode = self._settings.get("mode", "3d")
        base_config_path = None
        if preset == "heatmap":
            if mode != "3d":
                raise ValueError("Preset 'heatmap' requires mode='3d'")
            root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            base_config_path = os.path.join(root_dir, "configs", "awr6843_heatmap_default.cfg")

        return build_config(
            base_config_path=base_config_path,
            preset=preset,
            mode=mode,
            clutter_removal=self._settings.get("clutter_removal", False),
            fps=self._settings.get("fps", 10.0),
            sensitivity=self._settings.get("sensitivity", "medium"),
            min_range=self._settings.get("min_range", 0.25),
            max_range=self._settings.get("max_range", 8.0),
        )

    def stream(self, poll_interval: float = 0.005) -> Generator[RadarFrame, None, None]:
        """
        Continuously yields RadarFrame objects as data arrives from the radar.
        Terminates if stop() is called.
        """
        while self._is_running:
            chunk = self.connection.read_data(max_bytes=4096)
            if chunk:
                frames = self.parser.feed(chunk, timestamp=time.time())
                for frame in frames:
                    yield frame
            else:
                time.sleep(poll_interval)

    def read_frame(self, timeout: float = 1.0) -> Optional[RadarFrame]:
        """Reads and returns a single RadarFrame within the given timeout."""
        start_t = time.time()
        while time.time() - start_t < timeout:
            chunk = self.connection.read_data(max_bytes=4096)
            if chunk:
                frames = self.parser.feed(chunk, timestamp=time.time())
                if frames:
                    return frames[0]
            time.sleep(0.005)
        return None

    def read_raw_frame(self, timeout: float = 1.0) -> bytes:
        """Reads and returns one complete raw TI UART packet as bytes."""
        frame = self.read_frame(timeout=timeout)
        if frame and frame.raw_packet:
            return frame.raw_packet
        return b""

    def flush(self):
        """Discards buffered data in serial queue and parser buffer."""
        if self.connection.data_ser and self.connection.data_ser.is_open:
            self.connection.data_ser.reset_input_buffer()
        self.parser._buffer.clear()

    def stop(self):
        """Stops radar chirping and closes serial connections."""
        self._is_running = False
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
