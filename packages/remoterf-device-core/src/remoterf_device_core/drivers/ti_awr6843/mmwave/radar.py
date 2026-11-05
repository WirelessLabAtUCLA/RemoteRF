import os
import time
from typing import Any, Dict, Generator, Optional

from .connection import RadarConnection
from .parser import RadarFrame, TLVParser
from .config_builder import build_config, PRESETS


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
        self._settings["clutter_removal"] = val
        if self.connection.cli_ser and self.connection.cli_ser.is_open:
            # clutterRemoval is a TI runtime command: -1=all subframes, 1=enable, 0=disable
            self.connection.send_command(f"clutterRemoval -1 {1 if val else 0}")

    @property
    def fps(self) -> float:
        """Target frame capture rate in Hz (FPS)."""
        return float(self._settings.get("fps", 10.0))

    @fps.setter
    def fps(self, value: float):
        new_fps = float(value)
        if new_fps <= 0:
            raise ValueError(f"FPS must be positive, got {new_fps}")
        self._settings["fps"] = new_fps
        self.reconfigure(fps=new_fps)

    @property
    def preset(self) -> str:
        """Active sensing preset ('default', 'indoor-people', 'gesture', 'fast-motion', 'heatmap')."""
        return str(self._settings.get("preset", "default"))

    @preset.setter
    def preset(self, value: str):
        val = str(value).lower()
        self._settings["preset"] = val
        self.reconfigure(preset=val)

    @property
    def mode(self) -> str:
        """Chirp dimension mode ('3d' with elevation or '2d' azimuth-only)."""
        return str(self._settings.get("mode", "3d"))

    @mode.setter
    def mode(self, value: str):
        val = str(value).lower()
        if val not in ("3d", "2d"):
            raise ValueError("Mode must be '3d' or '2d'")
        self._settings["mode"] = val
        self.reconfigure(mode=val)

    @property
    def sensitivity(self) -> str:
        """CFAR detection threshold sensitivity ('low', 'medium', 'high')."""
        return str(self._settings.get("sensitivity", "medium"))

    @sensitivity.setter
    def sensitivity(self, value: str):
        val = str(value).lower()
        self._settings["sensitivity"] = val
        self.reconfigure(sensitivity=val)

    @property
    def min_range(self) -> float:
        """Minimum detection range in meters."""
        return float(self._settings.get("min_range", 0.25))

    @min_range.setter
    def min_range(self, value: float):
        val = float(value)
        self._settings["min_range"] = val
        self.reconfigure(min_range=val)

    @property
    def max_range(self) -> float:
        """Maximum detection range in meters."""
        return float(self._settings.get("max_range", 8.0))

    @max_range.setter
    def max_range(self, value: float):
        val = float(value)
        self._settings["max_range"] = val
        self.reconfigure(max_range=val)

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
    ):
        """
        Updates radar settings and re-uploads full configuration to radar.
        """
        if preset is not None:
            self._settings["preset"] = preset
            if preset in PRESETS:
                self._settings.update(PRESETS[preset])
        if mode is not None:
            self._settings["mode"] = mode
        if clutter_removal is not None:
            self._settings["clutter_removal"] = clutter_removal
        if fps is not None:
            self._settings["fps"] = float(fps)
        if sensitivity is not None:
            self._settings["sensitivity"] = sensitivity
        if min_range is not None:
            self._settings["min_range"] = float(min_range)
        if max_range is not None:
            self._settings["max_range"] = float(max_range)

        # Only send to hardware if connection is already open
        if self.connection.cli_ser and self.connection.cli_ser.is_open:
            cfg = self._build_active_config()
            was_running = self._is_running if start is None else start
            self.connection.send_config_text(cfg, verbose=False)
            self._is_running = was_running
            self.flush()

    def _build_active_config(self) -> str:
        """Helper to generate full configuration script from current settings."""
        preset = self._settings.get("preset", "default")
        if preset == "heatmap":
            root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cfg_path = os.path.join(root_dir, "configs", "awr6843_heatmap_default.cfg")
            with open(cfg_path, "r") as f:
                return f.read()

        return build_config(
            preset=self._settings.get("preset"),
            mode=self._settings.get("mode", "3d"),
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
