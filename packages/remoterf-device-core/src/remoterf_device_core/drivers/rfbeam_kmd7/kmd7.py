"""RFbeam K-MD7 Digital Radar Transceiver Python Driver.

Supports communication with K-MD7 and K-MD7-EVAL modules over UART / USB serial.
Features:
  - Connection & handshake lifecycle (INIT, VERS, GBYE)
  - Resilient buffered packet framing with automatic header synchronization
  - Radar parameter readout & configuration (GRPS, RSPI, RRAI, THOF, TRFT, DEDI, etc.)
  - Real-time frame acquisition in on-demand (GNFD) and streaming (RDOT) modes
  - Target decoding for PDAT (raw target point cloud) and TDAT (tracked targets with ID)
  - Automatic Cartesian coordinate conversion (X lateral, Y forward)
"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass, field
from typing import Generator, List, Optional, Tuple

import serial
import serial.tools.list_ports


# Baud rate encoding used by INIT command
BAUD_RATES = {
    115200: 0,
    460800: 1,
    921600: 2,
    2000000: 3,
    3000000: 4,
}

BAUD_RATES_REV = {v: k for k, v in BAUD_RATES.items()}

# Bitmask flags for GNFD / RDOT
FLAG_RADC = 1 << 0  # 1: Raw ADC values
FLAG_RFFT = 1 << 1  # 2: Raw FFT spectrum
FLAG_PDAT = 1 << 2  # 4: Raw target point cloud (up to 24 targets)
FLAG_TDAT = 1 << 3  # 8: Tracked targets (up to 8 targets with tracking IDs)
FLAG_DONE = 1 << 5  # 32: Frame done marker with frame counter

# Error descriptions for RESP packets
ERROR_CODES = {
    0: "OK / Success",
    1: "Unknown command",
    2: "Invalid parameter value",
    3: "Invalid RPST version",
    4: "UART error (parity, framing, noise)",
    5: "No calibration values",
    6: "Timeout",
    7: "Application corrupt or not programmed",
}

KNOWN_HEADERS = [b"RESP", b"VERS", b"PDAT", b"TDAT", b"DONE", b"RPST", b"RADC", b"RFFT"]


@dataclass
class RadarTarget:
    """Represents a single detected or tracked radar target."""

    distance: float  # Distance in meters (radial distance)
    speed: float  # Radial velocity in km/h (positive = receding, negative = approaching)
    speed_mps: float  # Radial velocity in m/s
    angle: float  # Azimuth angle in degrees (-30° to +30°, 0° = boresight)
    magnitude: float  # Signal strength / reflection magnitude in dB
    x: float  # Lateral distance in meters (X > 0: right, X < 0: left)
    y: float  # Forward distance in meters (along boresight axis)
    tracking_id: Optional[int] = None  # Tracking ID (only for TDAT targets)
    is_tracked: bool = False  # True for TDAT, False for raw PDAT detection

    def __repr__(self) -> str:
        tid_str = f" id={self.tracking_id}" if self.tracking_id is not None else ""
        return (
            f"<Target{tid_str} dist={self.distance:.2f}m "
            f"spd={self.speed:+.1f}km/h ang={self.angle:+.1f}° mag={self.magnitude:.1f}dB "
            f"pos=({self.x:.2f}m, {self.y:.2f}m)>"
        )


@dataclass
class RadarFrame:
    """Represents a complete frame of radar data."""

    frame_number: int
    timestamp: float
    raw_targets: List[RadarTarget] = field(default_factory=list)
    tracked_targets: List[RadarTarget] = field(default_factory=list)

    @property
    def all_targets(self) -> List[RadarTarget]:
        """Returns tracked targets if present, otherwise raw targets."""
        return self.tracked_targets if self.tracked_targets else self.raw_targets


@dataclass
class RadarParameters:
    """Radar configuration parameters read from RPST."""

    firmware: str
    frequency_channel: int  # 0=Low, 1=Middle, 2=High
    speed_setting: int  # 0=50 km/h, 1=100 km/h, 2=200 km/h
    range_setting: int  # 0=100 m, 1=200 m, 2=300 m
    threshold_offset_db: int  # 0-60 dB
    tracking_filter: int  # 0=Standard, 1=Fast detection, 2=Long visibility
    min_zone_dist_pct: int  # 0-100% of max range
    max_zone_dist_pct: int  # 0-100% of max range
    min_zone_angle_deg: int  # -30 to +30 deg
    max_zone_angle_deg: int  # -30 to +30 deg
    min_zone_speed_pct: int  # 0-100% of max speed
    max_zone_speed_pct: int  # 0-100% of max speed
    direction_filter: int  # 0=Receding, 1=Approaching, 2=Both

    @property
    def max_range_meters(self) -> float:
        mapping = {0: 100.0, 1: 200.0, 2: 300.0}
        return mapping.get(self.range_setting, 200.0)

    @property
    def max_speed_kmh(self) -> float:
        mapping = {0: 50.0, 1: 100.0, 2: 200.0}
        return mapping.get(self.speed_setting, 100.0)


class KMD7Error(Exception):
    """Exception raised for K-MD7 radar protocol errors."""


class KMD7Radar:
    """Driver for RFbeam K-MD7 radar sensor over serial port."""

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 115200,
        timeout: float = 1.0,
    ) -> None:
        """Initialize driver.

        Args:
            port: Serial port device path (e.g. '/dev/ttyUSB0'). If None,
              auto-detection is used.
            baudrate: Initial UART communication baudrate (default: 115200).
            timeout: Serial read timeout in seconds.
        """
        self.port_name = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[serial.Serial] = None
        self.firmware_version: str = ""
        self.is_streaming: bool = False
        self._rx_buf = bytearray()

    @staticmethod
    def find_serial_port() -> Optional[str]:
        """Automatically find the serial port associated with the RFbeam K-MD7 eval board."""
        ports = list(serial.tools.list_ports.comports())
        # First priority: FTDI FT230X chip used on K-MD7-EVAL (VID 0x0403, PID 0x6015)
        for p in ports:
            if p.vid == 0x0403 and p.pid == 0x6015:
                return p.device
            if "FT230X" in (p.description or ""):
                return p.device

        # Second priority: any ttyUSB or ttyACM
        for p in ports:
            if "ttyUSB" in p.device or "ttyACM" in p.device:
                return p.device

        return None

    def connect(self, target_baudrate: int = 115200) -> None:
        """Connect to the K-MD7 sensor and perform the INIT handshake.

        Args:
            target_baudrate: Desired working baud rate (115200, 460800, 921600,
              2000000, 3000000). Sensor defaults to 115200 on boot.
        """
        if self.serial is not None and self.serial.is_open:
            self.disconnect()

        if self.port_name is None:
            detected = self.find_serial_port()
            if not detected:
                raise KMD7Error(
                    "No RFbeam K-MD7 radar found on any serial port."
                )
            self.port_name = detected

        if target_baudrate not in BAUD_RATES:
            raise ValueError(
                f"Unsupported baudrate: {target_baudrate}. "
                f"Must be one of {list(BAUD_RATES.keys())}"
            )

        baud_code = BAUD_RATES[target_baudrate]

        # Sensor starts at 115200, 8 data bits, Even parity, 1 stop bit
        self.serial = serial.Serial(
            port=self.port_name,
            baudrate=115200,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
        )

        self._rx_buf.clear()
        self.serial.reset_input_buffer()
        self.serial.reset_output_buffer()

        # Send INIT command with chosen baud rate code
        init_payload = bytes([baud_code])
        self._send_command("INIT", init_payload)

        # 1. Read RESP to verify INIT was accepted
        resp_hdr, resp_data = self._read_packet(expected_header="RESP", timeout=2.0)
        err_code = resp_data[0] if resp_data else -1
        if err_code != 0:
            err_msg = ERROR_CODES.get(err_code, f"Unknown error {err_code}")
            raise KMD7Error(f"INIT command rejected by radar: {err_msg}")

        # 2. Read VERS containing firmware string
        vers_hdr, vers_data = self._read_packet(expected_header="VERS", timeout=2.0)
        self.firmware_version = (
            vers_data.decode("ascii", errors="ignore").strip("\x00").strip()
        )

        # 3. If target baudrate differs from 115200, switch local baudrate now
        if target_baudrate != 115200:
            time.sleep(0.05)
            self.serial.baudrate = target_baudrate
            self.baudrate = target_baudrate

        self.is_streaming = False

    def disconnect(self) -> None:
        """Gracefully disconnect from the radar with GBYE command and close serial port."""
        if self.serial is not None and self.serial.is_open:
            try:
                self._send_command("GBYE")
                time.sleep(0.05)
                # Read potential RESP
                try:
                    self._read_packet(expected_header="RESP", timeout=0.5)
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                self.serial.close()
                self.serial = None
                self._rx_buf.clear()
                self.is_streaming = False

    def __enter__(self) -> KMD7Radar:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()

    def _send_command(self, header: str, payload: bytes = b"") -> None:
        """Pack and send a protocol packet: 4-byte ASCII header + 4-byte uint32 length + payload."""
        if self.serial is None or not self.serial.is_open:
            raise KMD7Error("Serial port is not connected.")

        hdr_bytes = header.encode("ascii")
        if len(hdr_bytes) != 4:
            raise ValueError(f"Header must be exactly 4 bytes, got: {header}")

        packet = hdr_bytes + struct.pack("<I", len(payload)) + payload
        self.serial.write(packet)
        self.serial.flush()

    def _read_packet(
        self, expected_header: Optional[str] = None, timeout: float = 1.0
    ) -> Tuple[str, bytes]:
        """Read a single protocol packet from the radar using buffered synchronization."""
        if self.serial is None or not self.serial.is_open:
            raise KMD7Error("Serial port is not open.")

        t_start = time.time()
        while time.time() - t_start < timeout:
            # 1. Search buffer for any known 4-byte header
            found_idx = -1
            found_hdr = b""
            for hdr in KNOWN_HEADERS:
                idx = self._rx_buf.find(hdr)
                if idx != -1 and (found_idx == -1 or idx < found_idx):
                    found_idx = idx
                    found_hdr = hdr

            # If found header, trim any preceding bytes
            if found_idx > 0:
                del self._rx_buf[:found_idx]
                found_idx = 0

            if found_idx == 0:
                # Check if we have header (4) + payload length (4) = 8 bytes
                if len(self._rx_buf) >= 8:
                    payload_len = struct.unpack("<I", self._rx_buf[4:8])[0]
                    # Sanity check: no packet is > 10,000 bytes
                    if payload_len > 10000:
                        # Corrupt length, drop the header and search again
                        del self._rx_buf[:4]
                        continue

                    total_len = 8 + payload_len
                    # Check if entire payload is received
                    if len(self._rx_buf) >= total_len:
                        header = found_hdr.decode("ascii")
                        payload = bytes(self._rx_buf[8:total_len])
                        del self._rx_buf[:total_len]

                        if expected_header and header != expected_header:
                            # Not the expected packet, but valid packet received
                            # If caller expects a specific packet (e.g. RESP), continue scanning
                            continue

                        return header, payload

            # Need more bytes from serial port
            waiting = max(self.serial.in_waiting, 1)
            chunk = self.serial.read(waiting)
            if chunk:
                self._rx_buf.extend(chunk)
            else:
                time.sleep(0.002)

        raise KMD7Error(
            f"Timeout waiting for packet"
            + (f" '{expected_header}'" if expected_header else "")
        )

    def get_parameters(self) -> RadarParameters:
        """Query radar parameter structure (GRPS command)."""
        self._send_command("GRPS")

        # Read RESP
        _, resp_data = self._read_packet(expected_header="RESP", timeout=1.5)
        err = resp_data[0] if resp_data else -1
        if err != 0:
            raise KMD7Error(
                f"GRPS failed with error {err}: {ERROR_CODES.get(err, '')}"
            )

        # Read RPST
        _, rpst_data = self._read_packet(expected_header="RPST", timeout=1.5)
        if len(rpst_data) < 31:
            raise KMD7Error(f"RPST payload too short: {len(rpst_data)} bytes")

        fw = rpst_data[0:19].decode("ascii", errors="ignore").strip("\x00")
        freq_ch = rpst_data[19]
        speed_set = rpst_data[20]
        range_set = rpst_data[21]
        thresh_off = rpst_data[22]
        filter_type = rpst_data[23]
        min_dist_pct = rpst_data[24]
        max_dist_pct = rpst_data[25]
        min_ang = struct.unpack("b", rpst_data[26:27])[0]
        max_ang = struct.unpack("b", rpst_data[27:28])[0]
        min_spd_pct = rpst_data[28]
        max_spd_pct = rpst_data[29]
        dir_filter = rpst_data[30]

        return RadarParameters(
            firmware=fw,
            frequency_channel=freq_ch,
            speed_setting=speed_set,
            range_setting=range_set,
            threshold_offset_db=thresh_off,
            tracking_filter=filter_type,
            min_zone_dist_pct=min_dist_pct,
            max_zone_dist_pct=max_dist_pct,
            min_zone_angle_deg=min_ang,
            max_zone_angle_deg=max_ang,
            min_zone_speed_pct=min_spd_pct,
            max_zone_speed_pct=max_spd_pct,
            direction_filter=dir_filter,
        )

    def set_speed_range(self, speed_code: int) -> None:
        """Set speed range: 0 = 50 km/h, 1 = 100 km/h, 2 = 200 km/h."""
        if speed_code not in (0, 1, 2):
            raise ValueError("speed_code must be 0 (50km/h), 1 (100km/h), or 2 (200km/h)")
        self._send_command("RSPI", bytes([speed_code]))
        self._read_packet(expected_header="RESP")

    def set_max_range(self, range_code: int) -> None:
        """Set distance range: 0 = 100 m, 1 = 200 m, 2 = 300 m."""
        if range_code not in (0, 1, 2):
            raise ValueError("range_code must be 0 (100m), 1 (200m), or 2 (300m)")
        self._send_command("RRAI", bytes([range_code]))
        self._read_packet(expected_header="RESP")

    def set_threshold_offset(self, offset_db: int) -> None:
        """Set detection threshold offset in dB (0 - 60 dB). Lower is more sensitive."""
        if not (0 <= offset_db <= 60):
            raise ValueError("offset_db must be between 0 and 60")
        self._send_command("THOF", bytes([offset_db]))
        self._read_packet(expected_header="RESP")

    def set_tracking_filter(self, filter_type: int) -> None:
        """Set tracking filter type: 0 = Standard, 1 = Fast detection, 2 = Long visibility."""
        if filter_type not in (0, 1, 2):
            raise ValueError("filter_type must be 0, 1, or 2")
        self._send_command("TRFT", bytes([filter_type]))
        self._read_packet(expected_header="RESP")

    def set_direction_filter(self, direction_code: int) -> None:
        """Set direction filter: 0 = Receding, 1 = Approaching, 2 = Both."""
        if direction_code not in (0, 1, 2):
            raise ValueError("direction_code must be 0 (Receding), 1 (Approaching), or 2 (Both)")
        self._send_command("DEDI", bytes([direction_code]))
        self._read_packet(expected_header="RESP")

    def set_min_speed_filter(self, pct: int) -> None:
        """Set minimum detection speed filter as percentage of speed setting (0 - 100%)."""
        if not (0 <= pct <= 100):
            raise ValueError("pct must be between 0 and 100")
        self._send_command("MISP", bytes([pct]))
        self._read_packet(expected_header="RESP")

    def configure_mode(self, mode: str = "indoor") -> None:
        """Configure radar for specific operating scenario.

        Args:
            mode: 'indoor' (pedestrian / slow motions, 50 km/h range, fast tracking, 0% speed filter)
                  or 'traffic' (highway / vehicles, 100 km/h range, standard tracking, 5% speed filter)
        """
        if mode == "indoor":
            self.set_speed_range(0)  # 50 km/h max speed, 0.2 km/h fine resolution
            self.set_max_range(0)  # 100 m
            self.set_tracking_filter(1)  # Fast detection filter
            self.set_min_speed_filter(0)  # 0% speed filter (tracks slow hand/body movements)
        elif mode == "traffic":
            self.set_speed_range(1)  # 100 km/h max speed
            self.set_max_range(1)  # 200 m
            self.set_tracking_filter(0)  # Standard vehicle tracking
            self.set_min_speed_filter(5)  # 5% speed filter (ignores < 5 km/h)
        else:
            raise ValueError(f"Unknown mode: {mode}. Choose 'indoor' or 'traffic'.")

    def restore_factory_settings(self) -> None:
        """Restore radar factory settings (RFSE command)."""
        self._send_command("RFSE")
        self._read_packet(expected_header="RESP")

    def start_streaming(
        self,
        raw_targets: bool = True,
        tracked_targets: bool = True,
    ) -> None:
        """Start real-time streaming mode (RDOT command).

        Args:
            raw_targets: Enable PDAT (raw target point cloud, up to 24 targets)
            tracked_targets: Enable TDAT (tracked targets with ID, up to 8
              targets)
        """
        bitmask = FLAG_DONE
        if raw_targets:
            bitmask |= FLAG_PDAT
        if tracked_targets:
            bitmask |= FLAG_TDAT

        self._send_command("RDOT", bytes([bitmask]))
        _, resp_data = self._read_packet(expected_header="RESP", timeout=1.5)
        err = resp_data[0] if resp_data else -1
        if err != 0:
            raise KMD7Error(f"RDOT streaming start failed: error {err}")

        self.is_streaming = True

    def read_frame(self, timeout: float = 1.0) -> Optional[RadarFrame]:
        """Read a single complete radar frame from the stream.

        Collects PDAT and TDAT packets until the DONE packet signals the end of the frame.
        """
        if self.serial is None or not self.serial.is_open:
            return None

        raw_targets: List[RadarTarget] = []
        tracked_targets: List[RadarTarget] = []
        frame_num: int = -1
        t_start = time.time()

        while time.time() - t_start < timeout:
            try:
                header, payload = self._read_packet(timeout=0.2)
            except KMD7Error:
                continue

            if header == "PDAT":
                raw_targets = self._decode_pdat(payload)
            elif header == "TDAT":
                tracked_targets = self._decode_tdat(payload)
            elif header == "DONE":
                if len(payload) >= 4:
                    frame_num = struct.unpack("<I", payload[0:4])[0]
                return RadarFrame(
                    frame_number=frame_num,
                    timestamp=time.time(),
                    raw_targets=raw_targets,
                    tracked_targets=tracked_targets,
                )

        return None

    def stream_frames(
        self,
        raw_targets: bool = True,
        tracked_targets: bool = True,
    ) -> Generator[RadarFrame, None, None]:
        """Generator that continuously yields RadarFrame objects in real time."""
        if not self.is_streaming:
            self.start_streaming(raw_targets=raw_targets, tracked_targets=tracked_targets)

        try:
            while True:
                frame = self.read_frame()
                if frame is not None:
                    yield frame
        finally:
            if self.is_streaming:
                self.disconnect()

    @staticmethod
    def _decode_pdat(payload: bytes) -> List[RadarTarget]:
        """Decode PDAT payload (raw target array: 8 bytes per target)."""
        num_targets = len(payload) // 8
        targets = []
        for i in range(num_targets):
            chunk = payload[i * 8 : (i + 1) * 8]
            dist_raw, spd_raw, ang_raw, mag_raw = struct.unpack("<HhhH", chunk)

            distance = dist_raw / 100.0  # cm -> m
            speed = spd_raw / 100.0  # km/h * 100 -> km/h
            speed_mps = speed / 3.6  # km/h -> m/s
            angle = ang_raw / 100.0  # deg * 100 -> deg
            magnitude = mag_raw / 100.0  # dB * 100 -> dB

            # Cartesian coordinates (Y forward along boresight, X lateral)
            rad = math.radians(angle)
            x = distance * math.sin(rad)
            y = distance * math.cos(rad)

            targets.append(
                RadarTarget(
                    distance=distance,
                    speed=speed,
                    speed_mps=speed_mps,
                    angle=angle,
                    magnitude=magnitude,
                    x=x,
                    y=y,
                    tracking_id=None,
                    is_tracked=False,
                )
            )
        return targets

    @staticmethod
    def _decode_tdat(payload: bytes) -> List[RadarTarget]:
        """Decode TDAT payload (tracked target array: 9 bytes per target)."""
        num_targets = len(payload) // 9
        targets = []
        for i in range(num_targets):
            chunk = payload[i * 9 : (i + 1) * 9]
            dist_raw, spd_raw, ang_raw, mag_raw, track_id = struct.unpack(
                "<HhhHB", chunk
            )

            distance = dist_raw / 100.0
            speed = spd_raw / 100.0
            speed_mps = speed / 3.6
            angle = ang_raw / 100.0
            magnitude = mag_raw / 100.0

            rad = math.radians(angle)
            x = distance * math.sin(rad)
            y = distance * math.cos(rad)

            targets.append(
                RadarTarget(
                    distance=distance,
                    speed=speed,
                    speed_mps=speed_mps,
                    angle=angle,
                    magnitude=magnitude,
                    x=x,
                    y=y,
                    tracking_id=track_id,
                    is_tracked=True,
                )
            )
        return targets
