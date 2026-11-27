"""RFbeam K-MD7 Digital Radar Transceiver Python Driver.

Supports communication with K-MD7 and K-MD7-EVAL modules over UART / USB serial
per the K-MD7 Datasheet (Rev. D) Instruction Set.

Features:
  - Connection & handshake lifecycle (INIT, VERS, GBYE) with baud rates up to 3 Mbps
    and automatic baud-rate recovery after unclean disconnects
  - Resilient buffered packet framing with automatic header synchronization
  - Full radar parameter readout & configuration:
      * GRPS (read 31B RPST), SRPS (atomic write 31B RPST), RFSE (factory reset)
      * Individual setters: RBFR, RSPI, RRAI, THOF, TRFT, MIRA, MARA, MIAN, MAAN,
        MISP, MASP, DEDI
  - Frame acquisition in both continuous streaming (RDOT, incl. 0x00 stop) and
    single-frame handshake (GNFD) modes
  - Full message decoding for PDAT, TDAT, RADC (raw I/Q ADC), RFFT (raw FFT +
    threshold), RPST, VERS, RESP, and DONE
  - Strict RESP error code verification on every command

Safety note:
  The K-MD7 protocol defines no UART command or hardware pin to disable the
  24 GHz RF transmitter while powered. Bootloader / flash programming commands
  (JBTL, WMEM) are intentionally omitted so remote callers cannot trap the
  sensor in bootloader mode.
"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass, field
from typing import Generator, List, Optional, Tuple

import serial
import serial.tools.list_ports


# Baud rate encoding used by INIT command (Datasheet Table 11)
BAUD_RATES = {
    115200: 0,
    460800: 1,
    921600: 2,
    2000000: 3,
    3000000: 4,
}

BAUD_RATES_REV = {v: k for k, v in BAUD_RATES.items()}

# Recovery baud-rate probe order when INIT at 115200 fails
_RECOVERY_BAUD_ORDER = (3000000, 2000000, 921600, 460800, 115200)

# Bitmask flags for GNFD / RDOT (Datasheet Table 11)
FLAG_RADC = 1 << 0  # 1: Raw ADC values (6144 bytes)
FLAG_RFFT = 1 << 1  # 2: Raw FFT spectrum + threshold (2048 bytes)
FLAG_PDAT = 1 << 2  # 4: Raw target point cloud (up to 24 targets)
FLAG_TDAT = 1 << 3  # 8: Tracked targets (up to 8 targets with tracking IDs)
FLAG_DONE = 1 << 5  # 32: Frame done marker with frame counter
FLAG_ALL = FLAG_RADC | FLAG_RFFT | FLAG_PDAT | FLAG_TDAT | FLAG_DONE

# Human-readable parameter value mappings (Datasheet Table 10 & 11)
FREQUENCY_CHANNELS = {0: "low", 1: "middle", 2: "high"}
SPEED_SETTINGS = {0: 50.0, 1: 100.0, 2: 200.0}
RANGE_SETTINGS = {0: 100.0, 1: 200.0, 2: 300.0}
TRACKING_FILTERS = {0: "standard", 1: "fast_detection", 2: "long_visibility"}
DIRECTION_FILTERS = {0: "receding", 1: "approaching", 2: "both"}

# Error descriptions for RESP packets (Datasheet Table 12)
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
MAX_PAYLOAD_LEN = 10000

RADC_NUM_SAMPLES = 512
RADC_PAYLOAD_LEN = 6144  # 3 * (512 I + 512 Q) * 2 bytes
RFFT_NUM_BINS = 512
RFFT_PAYLOAD_LEN = 2048  # (512 spectrum + 512 threshold) * 2 bytes
RPST_PAYLOAD_LEN = 31


class KMD7Error(Exception):
    """Exception raised for K-MD7 radar protocol errors."""


@dataclass
class RadarTarget:
    """Represents a single detected or tracked radar target."""

    distance: float  # Radial distance in meters
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
class RadarADCData:
    """Raw 12-bit/16-bit ADC I/Q samples from an RADC message (6144 bytes).

    Contains 512 UINT16 samples per channel for three IF blocks:
      - IF1 Frequency A (I and Q)
      - IF2 Frequency A (I and Q)
      - IF1 Frequency B (I and Q)
    """

    if1_freq_a_i: Tuple[int, ...]
    if1_freq_a_q: Tuple[int, ...]
    if2_freq_a_i: Tuple[int, ...]
    if2_freq_a_q: Tuple[int, ...]
    if1_freq_b_i: Tuple[int, ...]
    if1_freq_b_q: Tuple[int, ...]


@dataclass
class RadarFFTData:
    """Raw 512-point FFT spectrum and detection threshold from an RFFT message (2048 bytes)."""

    spectrum_db: Tuple[float, ...]
    threshold_db: Tuple[float, ...]


@dataclass
class RadarFrame:
    """Represents a complete frame of radar data."""

    frame_number: int
    timestamp: float
    raw_targets: List[RadarTarget] = field(default_factory=list)
    tracked_targets: List[RadarTarget] = field(default_factory=list)
    adc_data: Optional[RadarADCData] = None
    fft_data: Optional[RadarFFTData] = None
    bytes_read: int = 0
    packet_mask: int = 0

    @property
    def all_targets(self) -> List[RadarTarget]:
        """Returns tracked targets if present, otherwise raw targets."""
        return self.tracked_targets if self.tracked_targets else self.raw_targets


@dataclass
class RadarParameters:
    """Radar configuration parameters corresponding to the 31-byte RPST structure."""

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

    _STRUCT_FMT = "<19sBBBBBBBbbBBB"

    @property
    def max_range_meters(self) -> float:
        return RANGE_SETTINGS.get(self.range_setting, 200.0)

    @property
    def max_speed_kmh(self) -> float:
        return SPEED_SETTINGS.get(self.speed_setting, 100.0)

    def validate(self) -> None:
        """Validate all RPST fields against Datasheet Table 10 limits."""
        if not self.firmware or len(self.firmware.encode("ascii", errors="strict")) > 18:
            raise ValueError("firmware string must be 1..18 ASCII characters (plus null terminator)")
        if self.frequency_channel not in FREQUENCY_CHANNELS:
            raise ValueError("frequency_channel must be 0 (Low), 1 (Middle), or 2 (High)")
        if self.speed_setting not in SPEED_SETTINGS:
            raise ValueError("speed_setting must be 0 (50 km/h), 1 (100 km/h), or 2 (200 km/h)")
        if self.range_setting not in RANGE_SETTINGS:
            raise ValueError("range_setting must be 0 (100 m), 1 (200 m), or 2 (300 m)")
        if not (0 <= self.threshold_offset_db <= 60):
            raise ValueError("threshold_offset_db must be between 0 and 60 dB")
        if self.tracking_filter not in TRACKING_FILTERS:
            raise ValueError("tracking_filter must be 0, 1, or 2")
        if not (0 <= self.min_zone_dist_pct <= 100 and 0 <= self.max_zone_dist_pct <= 100):
            raise ValueError("zone distance percentages must be between 0 and 100")
        if self.min_zone_dist_pct > self.max_zone_dist_pct:
            raise ValueError("min_zone_dist_pct cannot exceed max_zone_dist_pct")
        if not (-30 <= self.min_zone_angle_deg <= 30 and -30 <= self.max_zone_angle_deg <= 30):
            raise ValueError("zone angles must be between -30 and +30 degrees")
        if self.min_zone_angle_deg > self.max_zone_angle_deg:
            raise ValueError("min_zone_angle_deg cannot exceed max_zone_angle_deg")
        if not (0 <= self.min_zone_speed_pct <= 100 and 0 <= self.max_zone_speed_pct <= 100):
            raise ValueError("zone speed percentages must be between 0 and 100")
        if self.min_zone_speed_pct > self.max_zone_speed_pct:
            raise ValueError("min_zone_speed_pct cannot exceed max_zone_speed_pct")
        if self.direction_filter not in DIRECTION_FILTERS:
            raise ValueError("direction_filter must be 0 (Receding), 1 (Approaching), or 2 (Both)")

    def to_bytes(self) -> bytes:
        """Pack parameters into the 31-byte RPST binary structure."""
        self.validate()
        fw_bytes = self.firmware.encode("ascii")[:18].ljust(19, b"\x00")
        return struct.pack(
            self._STRUCT_FMT,
            fw_bytes,
            int(self.frequency_channel),
            int(self.speed_setting),
            int(self.range_setting),
            int(self.threshold_offset_db),
            int(self.tracking_filter),
            int(self.min_zone_dist_pct),
            int(self.max_zone_dist_pct),
            int(self.min_zone_angle_deg),
            int(self.max_zone_angle_deg),
            int(self.min_zone_speed_pct),
            int(self.max_zone_speed_pct),
            int(self.direction_filter),
        )

    @classmethod
    def from_bytes(cls, rpst_data: bytes) -> RadarParameters:
        """Unpack a 31-byte RPST payload into RadarParameters."""
        if len(rpst_data) < RPST_PAYLOAD_LEN:
            raise KMD7Error(f"RPST payload too short: {len(rpst_data)} bytes")

        (
            fw_raw,
            freq_ch,
            speed_set,
            range_set,
            thresh_off,
            filter_type,
            min_dist_pct,
            max_dist_pct,
            min_ang,
            max_ang,
            min_spd_pct,
            max_spd_pct,
            dir_filter,
        ) = struct.unpack(cls._STRUCT_FMT, rpst_data[:RPST_PAYLOAD_LEN])

        fw = fw_raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
        return cls(
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


def _build_output_bitmask(
    *,
    raw_adc: bool = False,
    raw_fft: bool = False,
    raw_targets: bool = True,
    tracked_targets: bool = True,
    done: bool = True,
    bitmask: Optional[int] = None,
) -> int:
    """Build or validate an 8-bit RDOT / GNFD output selection bitmask."""
    if bitmask is not None:
        mask = int(bitmask)
        if not (0 <= mask <= 0xFF):
            raise ValueError("bitmask must be between 0 and 255")
        if done and mask != 0:
            mask |= FLAG_DONE
        return mask

    mask = FLAG_DONE if done else 0
    if raw_adc:
        mask |= FLAG_RADC
    if raw_fft:
        mask |= FLAG_RFFT
    if raw_targets:
        mask |= FLAG_PDAT
    if tracked_targets:
        mask |= FLAG_TDAT
    return mask


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
            baudrate: Desired active UART baud rate (115200, 460800, 921600,
              2000000, 3000000). Initial handshake still starts at 115200.
            timeout: Default read timeout in seconds.
        """
        if baudrate not in BAUD_RATES:
            raise ValueError(
                f"Unsupported baudrate: {baudrate}. Must be one of {list(BAUD_RATES.keys())}"
            )
        self.port_name = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[serial.Serial] = None
        self.firmware_version: str = ""
        self.is_streaming: bool = False
        self.active_stream_mask: int = 0
        self.total_bytes_read: int = 0
        self._rx_buf = bytearray()

    @staticmethod
    def find_serial_port(*, strict: bool = True) -> Optional[str]:
        """Find the serial port associated with the RFbeam K-MD7 eval board.

        Args:
            strict: If True (default), match only the FTDI FT230X USB-UART
              bridge used on K-MD7-EVAL (VID 0x0403, PID 0x6015). If False,
              fall back to any ttyUSB or ttyACM device.
        """
        ports = list(serial.tools.list_ports.comports())
        for p in ports:
            if p.vid == 0x0403 and p.pid == 0x6015:
                return p.device
            if "FT230X" in (p.description or ""):
                return p.device

        if not strict:
            for p in ports:
                if "ttyUSB" in p.device or "ttyACM" in p.device:
                    return p.device

        return None

    def _open_serial(self, baudrate: int = 115200) -> serial.Serial:
        """Open the serial port with K-MD7 framing (8E1, no flow control)."""
        return serial.Serial(
            port=self.port_name,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
            rtscts=False,
            dsrdtr=False,
        )

    def _attempt_baud_recovery(self) -> None:
        """Try GBYE at each supported baud rate to reset a sensor left in high-baud state."""
        if self.serial is None or not self.serial.is_open:
            return
        for probe_baud in _RECOVERY_BAUD_ORDER:
            try:
                self.serial.baudrate = probe_baud
                self._rx_buf.clear()
                self.serial.reset_input_buffer()
                self.serial.reset_output_buffer()
                # Stop streaming first if high-rate packets are saturating the RX buffer
                self._send_command("RDOT", b"\x00")
                time.sleep(0.02)
                self._send_command("GBYE")
                time.sleep(0.05)
                self._rx_buf.clear()
                self.serial.reset_input_buffer()
            except Exception:
                continue

    def _init_handshake(self, target_baudrate: int) -> None:
        """Execute INIT -> RESP -> VERS at 115200 and switch to target_baudrate."""
        baud_code = BAUD_RATES[target_baudrate]
        assert self.serial is not None
        self.serial.baudrate = 115200
        self._rx_buf.clear()
        self.serial.reset_input_buffer()
        self.serial.reset_output_buffer()

        self._send_command("INIT", bytes([baud_code]))
        self._check_resp("INIT", timeout=1.5)

        _, vers_data = self._read_packet(expected_header="VERS", timeout=1.5)
        self.firmware_version = (
            vers_data.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
        )

        if target_baudrate != 115200:
            time.sleep(0.05)
            self.serial.baudrate = target_baudrate
        self.baudrate = target_baudrate

    def connect(self, target_baudrate: Optional[int] = None) -> None:
        """Connect to the K-MD7 sensor and perform the INIT handshake.

        Args:
            target_baudrate: Desired working baud rate (115200, 460800, 921600,
              2000000, 3000000). Defaults to `self.baudrate`. Sensor always starts
              at 115200 on boot or after GBYE.
        """
        if target_baudrate is None:
            target_baudrate = self.baudrate

        if target_baudrate not in BAUD_RATES:
            raise ValueError(
                f"Unsupported baudrate: {target_baudrate}. "
                f"Must be one of {list(BAUD_RATES.keys())}"
            )

        if self.serial is not None and self.serial.is_open:
            self.disconnect()

        if self.port_name is None:
            detected = self.find_serial_port(strict=True)
            if not detected:
                raise KMD7Error(
                    "No RFbeam K-MD7 radar found on any serial port."
                )
            self.port_name = detected

        self.serial = self._open_serial(baudrate=115200)
        try:
            self._init_handshake(target_baudrate)
        except KMD7Error as first_err:
            # Recover if a previous session exited without sending GBYE (e.g. stuck at 3 Mbps)
            try:
                self._attempt_baud_recovery()
                self._init_handshake(target_baudrate)
            except Exception as recovery_err:
                self._close_serial()
                raise KMD7Error(
                    f"Failed to connect on {self.port_name}: {first_err} "
                    f"(recovery failed: {recovery_err})"
                ) from first_err
        except Exception:
            self._close_serial()
            raise

        self.is_streaming = False
        self.active_stream_mask = 0

    def set_baudrate(self, target_baudrate: int) -> None:
        """Switch the active UART baud rate via GBYE + INIT, preserving stream state."""
        if target_baudrate not in BAUD_RATES:
            raise ValueError(
                f"Unsupported baudrate: {target_baudrate}. "
                f"Must be one of {list(BAUD_RATES.keys())}"
            )
        if self.serial is not None and self.serial.is_open:
            if target_baudrate == self.baudrate:
                return
        else:
            self.baudrate = target_baudrate
            return

        prev_streaming = self.is_streaming
        prev_mask = self.active_stream_mask

        self.disconnect()
        self.connect(target_baudrate=target_baudrate)

        if prev_streaming and prev_mask:
            self.start_streaming(bitmask=prev_mask)

    def _close_serial(self) -> None:
        """Close the underlying serial object without sending GBYE."""
        if self.serial is not None:
            try:
                if self.serial.is_open:
                    self.serial.close()
            except Exception:
                pass
        self.serial = None
        self._rx_buf.clear()
        self.is_streaming = False
        self.active_stream_mask = 0

    def disconnect(self) -> None:
        """Gracefully disconnect from the radar with GBYE command and close serial port."""
        if self.serial is not None and self.serial.is_open:
            try:
                self._send_command("GBYE")
                time.sleep(0.02)
                try:
                    self._read_packet(expected_header="RESP", timeout=0.5)
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                self._close_serial()
        else:
            self._close_serial()

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

        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            found_idx = -1
            found_hdr = b""
            for hdr in KNOWN_HEADERS:
                idx = self._rx_buf.find(hdr)
                if idx != -1 and (found_idx == -1 or idx < found_idx):
                    found_idx = idx
                    found_hdr = hdr

            if found_idx > 0:
                del self._rx_buf[:found_idx]
                found_idx = 0

            if found_idx == 0 and len(self._rx_buf) >= 8:
                payload_len = struct.unpack("<I", self._rx_buf[4:8])[0]
                if payload_len > MAX_PAYLOAD_LEN:
                    del self._rx_buf[:4]
                    continue

                total_len = 8 + payload_len
                if len(self._rx_buf) >= total_len:
                    header = found_hdr.decode("ascii")
                    payload = bytes(self._rx_buf[8:total_len])
                    del self._rx_buf[:total_len]

                    if expected_header and header != expected_header:
                        continue

                    return header, payload

            if time.monotonic() >= deadline:
                break

            waiting = max(getattr(self.serial, "in_waiting", 0), 1)
            chunk = self.serial.read(waiting)
            if chunk:
                self.total_bytes_read += len(chunk)
                self._rx_buf.extend(chunk)
            else:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.002)

        raise KMD7Error(
            "Timeout waiting for packet"
            + (f" '{expected_header}'" if expected_header else "")
        )

    def _check_resp(self, cmd_name: str, timeout: float = 2.0) -> None:
        """Read RESP packet and raise KMD7Error if status code is non-zero."""
        _, resp_data = self._read_packet(expected_header="RESP", timeout=timeout)
        err = resp_data[0] if resp_data else -1
        if err != 0:
            err_msg = ERROR_CODES.get(err, f"Unknown error {err}")
            raise KMD7Error(f"{cmd_name} failed with error {err}: {err_msg}")

    # -------------------------------------------------------------------------
    # Configuration Commands (GRPS, SRPS, RFSE, RBFR, RSPI, RRAI, THOF, TRFT,
    #                         MIRA, MARA, MIAN, MAAN, MISP, MASP, DEDI)
    # -------------------------------------------------------------------------

    def get_parameters(self) -> RadarParameters:
        """Query complete radar parameter structure (GRPS command -> RPST)."""
        self._send_command("GRPS")
        self._check_resp("GRPS", timeout=1.5)
        _, rpst_data = self._read_packet(expected_header="RPST", timeout=1.5)
        return RadarParameters.from_bytes(rpst_data)

    def set_parameters(self, params: RadarParameters) -> None:
        """Write complete 31-byte radar parameter structure atomically (SRPS command)."""
        if not params.firmware and self.firmware_version:
            params = RadarParameters(
                firmware=self.firmware_version,
                frequency_channel=params.frequency_channel,
                speed_setting=params.speed_setting,
                range_setting=params.range_setting,
                threshold_offset_db=params.threshold_offset_db,
                tracking_filter=params.tracking_filter,
                min_zone_dist_pct=params.min_zone_dist_pct,
                max_zone_dist_pct=params.max_zone_dist_pct,
                min_zone_angle_deg=params.min_zone_angle_deg,
                max_zone_angle_deg=params.max_zone_angle_deg,
                min_zone_speed_pct=params.min_zone_speed_pct,
                max_zone_speed_pct=params.max_zone_speed_pct,
                direction_filter=params.direction_filter,
            )
        payload = params.to_bytes()
        self._send_command("SRPS", payload)
        self._check_resp("SRPS")

    def set_frequency_channel(self, channel: int) -> None:
        """Set base RF frequency channel (RBFR): 0=Low, 1=Middle, 2=High."""
        if channel not in FREQUENCY_CHANNELS:
            raise ValueError("channel must be 0 (Low), 1 (Middle), or 2 (High)")
        self._send_command("RBFR", bytes([channel]))
        self._check_resp("RBFR")

    def set_speed_range(self, speed_code: int) -> None:
        """Set speed range (RSPI): 0=50 km/h, 1=100 km/h, 2=200 km/h."""
        if speed_code not in SPEED_SETTINGS:
            raise ValueError("speed_code must be 0 (50km/h), 1 (100km/h), or 2 (200km/h)")
        self._send_command("RSPI", bytes([speed_code]))
        self._check_resp("RSPI")

    def set_max_range(self, range_code: int) -> None:
        """Set distance range (RRAI): 0=100 m, 1=200 m, 2=300 m."""
        if range_code not in RANGE_SETTINGS:
            raise ValueError("range_code must be 0 (100m), 1 (200m), or 2 (300m)")
        self._send_command("RRAI", bytes([range_code]))
        self._check_resp("RRAI")

    def set_threshold_offset(self, offset_db: int) -> None:
        """Set detection threshold offset (THOF) in dB (0 - 60 dB). Lower is more sensitive."""
        if not (0 <= offset_db <= 60):
            raise ValueError("offset_db must be between 0 and 60")
        self._send_command("THOF", bytes([offset_db]))
        self._check_resp("THOF")

    def set_tracking_filter(self, filter_type: int) -> None:
        """Set tracking filter type (TRFT): 0=Standard, 1=Fast detection, 2=Long visibility."""
        if filter_type not in TRACKING_FILTERS:
            raise ValueError("filter_type must be 0, 1, or 2")
        self._send_command("TRFT", bytes([filter_type]))
        self._check_resp("TRFT")

    def set_min_zone_distance(self, pct: int) -> None:
        """Set minimum detection zone distance (MIRA) as % of range setting (0 - 100%)."""
        if not (0 <= pct <= 100):
            raise ValueError("pct must be between 0 and 100")
        self._send_command("MIRA", bytes([pct]))
        self._check_resp("MIRA")

    def set_max_zone_distance(self, pct: int) -> None:
        """Set maximum detection zone distance (MARA) as % of range setting (0 - 100%)."""
        if not (0 <= pct <= 100):
            raise ValueError("pct must be between 0 and 100")
        self._send_command("MARA", bytes([pct]))
        self._check_resp("MARA")

    def set_min_zone_angle(self, deg: int) -> None:
        """Set minimum detection zone angle (MIAN) in degrees (-30 to +30)."""
        if not (-30 <= deg <= 30):
            raise ValueError("deg must be between -30 and +30")
        self._send_command("MIAN", struct.pack("b", deg))
        self._check_resp("MIAN")

    def set_max_zone_angle(self, deg: int) -> None:
        """Set maximum detection zone angle (MAAN) in degrees (-30 to +30)."""
        if not (-30 <= deg <= 30):
            raise ValueError("deg must be between -30 and +30")
        self._send_command("MAAN", struct.pack("b", deg))
        self._check_resp("MAAN")

    def set_min_speed_filter(self, pct: int) -> None:
        """Set minimum detection speed filter (MISP) as % of speed setting (0 - 100%)."""
        if not (0 <= pct <= 100):
            raise ValueError("pct must be between 0 and 100")
        self._send_command("MISP", bytes([pct]))
        self._check_resp("MISP")

    def set_max_speed_filter(self, pct: int) -> None:
        """Set maximum detection speed filter (MASP) as % of speed setting (0 - 100%)."""
        if not (0 <= pct <= 100):
            raise ValueError("pct must be between 0 and 100")
        self._send_command("MASP", bytes([pct]))
        self._check_resp("MASP")

    def set_direction_filter(self, direction_code: int) -> None:
        """Set direction filter (DEDI): 0=Receding, 1=Approaching, 2=Both."""
        if direction_code not in DIRECTION_FILTERS:
            raise ValueError("direction_code must be 0 (Receding), 1 (Approaching), or 2 (Both)")
        self._send_command("DEDI", bytes([direction_code]))
        self._check_resp("DEDI")

    def configure_mode(self, mode: str = "indoor") -> None:
        """Configure radar for specific operating scenario.

        Args:
            mode: 'indoor' (pedestrian / slow motions: 50 km/h, 100 m, fast tracking, 0% min speed)
                  or 'traffic' (vehicles / highway: 100 km/h, 200 m, standard tracking, 5% min speed)
        """
        if mode == "indoor":
            self.set_speed_range(0)
            self.set_max_range(0)
            self.set_tracking_filter(1)
            self.set_min_speed_filter(0)
        elif mode == "traffic":
            self.set_speed_range(1)
            self.set_max_range(1)
            self.set_tracking_filter(0)
            self.set_min_speed_filter(5)
        else:
            raise ValueError(f"Unknown mode: {mode}. Choose 'indoor' or 'traffic'.")

    def restore_factory_settings(self) -> None:
        """Restore radar factory settings (RFSE command)."""
        self._send_command("RFSE")
        self._check_resp("RFSE")

    # -------------------------------------------------------------------------
    # Acquisition Commands (RDOT, GNFD) & Frame Decoding (RADC, RFFT, PDAT, TDAT, DONE)
    # -------------------------------------------------------------------------

    def flush(self) -> None:
        """Flush host serial input buffer and internal receive buffer."""
        if self.serial is not None and self.serial.is_open:
            self.serial.reset_input_buffer()
        self._rx_buf.clear()

    def start_streaming(
        self,
        raw_targets: bool = True,
        tracked_targets: bool = True,
        *,
        raw_adc: bool = False,
        raw_fft: bool = False,
        bitmask: Optional[int] = None,
    ) -> None:
        """Start real-time streaming mode (RDOT command).

        Passing `bitmask=0` stops streaming via `stop_streaming()`.
        """
        mask = _build_output_bitmask(
            raw_adc=raw_adc,
            raw_fft=raw_fft,
            raw_targets=raw_targets,
            tracked_targets=tracked_targets,
            done=True,
            bitmask=bitmask,
        )
        if mask == 0:
            self.stop_streaming()
            return

        if self.is_streaming and self.active_stream_mask != mask:
            self.stop_streaming()

        self.flush()
        self._send_command("RDOT", bytes([mask]))
        self._check_resp("RDOT", timeout=1.5)
        self.is_streaming = True
        self.active_stream_mask = mask

    def stop_streaming(self) -> None:
        """Disable continuous streaming via RDOT(0x00) while keeping serial connected."""
        if self.serial is None or not self.serial.is_open:
            self.is_streaming = False
            self.active_stream_mask = 0
            return

        self.flush()
        self._send_command("RDOT", b"\x00")
        self._check_resp("RDOT", timeout=1.5)
        self.flush()
        self.is_streaming = False
        self.active_stream_mask = 0

    def get_next_frame(
        self,
        *,
        raw_adc: bool = False,
        raw_fft: bool = False,
        raw_targets: bool = True,
        tracked_targets: bool = True,
        bitmask: Optional[int] = None,
        timeout: float = 2.0,
    ) -> RadarFrame:
        """Request a single synchronized frame on demand (GNFD handshake mode)."""
        mask = _build_output_bitmask(
            raw_adc=raw_adc,
            raw_fft=raw_fft,
            raw_targets=raw_targets,
            tracked_targets=tracked_targets,
            done=True,
            bitmask=bitmask,
        )
        if mask == 0:
            raise ValueError("GNFD bitmask must enable at least one message type")

        self._send_command("GNFD", bytes([mask]))
        self._check_resp("GNFD", timeout=min(max(timeout, 0.5), 2.0))

        frame = self.read_frame(timeout=timeout)
        if frame is None:
            raise KMD7Error("Timeout waiting for GNFD frame completion (DONE).")
        return frame

    def read_frame(self, timeout: float = 1.0) -> Optional[RadarFrame]:
        """Read a single complete radar frame (collecting enabled packets until DONE)."""
        if self.serial is None or not self.serial.is_open:
            return None

        raw_targets: List[RadarTarget] = []
        tracked_targets: List[RadarTarget] = []
        adc_data: Optional[RadarADCData] = None
        fft_data: Optional[RadarFFTData] = None
        packet_mask = 0
        frame_bytes = 0
        frame_num: int = -1
        deadline = time.monotonic() + max(timeout, 0.0)

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                header, payload = self._read_packet(timeout=min(0.2, max(remaining, 0.01)))
            except KMD7Error:
                continue

            frame_bytes += 8 + len(payload)

            if header == "RADC":
                adc_data = self._decode_radc(payload)
                packet_mask |= FLAG_RADC
            elif header == "RFFT":
                fft_data = self._decode_rfft(payload)
                packet_mask |= FLAG_RFFT
            elif header == "PDAT":
                raw_targets = self._decode_pdat(payload)
                packet_mask |= FLAG_PDAT
            elif header == "TDAT":
                tracked_targets = self._decode_tdat(payload)
                packet_mask |= FLAG_TDAT
            elif header == "DONE":
                packet_mask |= FLAG_DONE
                if len(payload) >= 4:
                    frame_num = struct.unpack("<I", payload[0:4])[0]
                return RadarFrame(
                    frame_number=frame_num,
                    timestamp=time.time(),
                    raw_targets=raw_targets,
                    tracked_targets=tracked_targets,
                    adc_data=adc_data,
                    fft_data=fft_data,
                    bytes_read=frame_bytes,
                    packet_mask=packet_mask,
                )

        return None

    def stream_frames(
        self,
        raw_targets: bool = True,
        tracked_targets: bool = True,
        *,
        raw_adc: bool = False,
        raw_fft: bool = False,
        bitmask: Optional[int] = None,
    ) -> Generator[RadarFrame, None, None]:
        """Generator that continuously yields RadarFrame objects in real time."""
        if not self.is_streaming:
            self.start_streaming(
                raw_targets=raw_targets,
                tracked_targets=tracked_targets,
                raw_adc=raw_adc,
                raw_fft=raw_fft,
                bitmask=bitmask,
            )

        try:
            while True:
                frame = self.read_frame(timeout=self.timeout)
                if frame is not None:
                    yield frame
        finally:
            if self.is_streaming:
                try:
                    self.stop_streaming()
                except Exception:
                    pass

    @staticmethod
    def _decode_radc(payload: bytes) -> RadarADCData:
        """Decode RADC payload (6144 bytes: 3 blocks x (512 I + 512 Q) UINT16)."""
        if len(payload) != RADC_PAYLOAD_LEN:
            raise KMD7Error(
                f"Invalid RADC payload length: expected {RADC_PAYLOAD_LEN} bytes, got {len(payload)}"
            )
        vals = struct.unpack("<3072H", payload)
        n = RADC_NUM_SAMPLES
        return RadarADCData(
            if1_freq_a_i=vals[0:n],
            if1_freq_a_q=vals[n : 2 * n],
            if2_freq_a_i=vals[2 * n : 3 * n],
            if2_freq_a_q=vals[3 * n : 4 * n],
            if1_freq_b_i=vals[4 * n : 5 * n],
            if1_freq_b_q=vals[5 * n : 6 * n],
        )

    @staticmethod
    def _decode_rfft(payload: bytes) -> RadarFFTData:
        """Decode RFFT payload (2048 bytes: 512 spectrum + 512 threshold UINT16 in dB x 100)."""
        if len(payload) != RFFT_PAYLOAD_LEN:
            raise KMD7Error(
                f"Invalid RFFT payload length: expected {RFFT_PAYLOAD_LEN} bytes, got {len(payload)}"
            )
        vals = struct.unpack("<1024H", payload)
        n = RFFT_NUM_BINS
        spectrum = tuple(v / 100.0 for v in vals[0:n])
        threshold = tuple(v / 100.0 for v in vals[n : 2 * n])
        return RadarFFTData(spectrum_db=spectrum, threshold_db=threshold)

    @staticmethod
    def _decode_pdat(payload: bytes) -> List[RadarTarget]:
        """Decode PDAT payload (raw target array: 8 bytes per target)."""
        num_targets = len(payload) // 8
        targets = []
        for i in range(num_targets):
            chunk = payload[i * 8 : (i + 1) * 8]
            dist_raw, spd_raw, ang_raw, mag_raw = struct.unpack("<HhhH", chunk)

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
