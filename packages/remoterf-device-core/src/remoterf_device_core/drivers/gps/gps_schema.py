# Copyright (C) 2026 RemoteRF
#
# RemoteRF Device Schema for USB GPS Receivers (any NMEA-0183 serial receiver; auto-detects u-blox).
# Generated for RemoteRF custom device architecture.
#
# Server schema file: place or symlink into ~/.config/remoterf/drivers/
# Add device to ~/.config/remoterf/devices.yml with device_type: gps

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import pynmea2
import serial
import serial.tools.list_ports
from remoterf_device_core.idl import DeviceSchema, idl_expose, idl_register

logger = logging.getLogger("remoterf.driver.gps")


def find_gps_port() -> Optional[str]:
    """Auto-detect the GPS receiver serial port on the host system."""
    ports = list(serial.tools.list_ports.comports())

    # Any u-blox (1546:*), or a port that names itself a GPS. No guessing among
    # other serial ports: they are radars, mmWave boards and the like, and opening
    # one resets its baud under its own driver. A GPS behind a generic USB-serial
    # chip (PL2303, CP210x, CH340) needs `port:` in devices.yml.
    for p in ports:
        text = " ".join(filter(None, (p.description, p.manufacturer, p.product))).lower()
        if p.vid == 0x1546 or any(k in text for k in ("u-blox", "gps", "gnss")):
            return p.device

    return None


class GpsDevice:
    """
    Hardware interface for USB GPS receivers.
    Spawns a background thread to continuously drain and parse NMEA sentences,
    preventing serial buffer stale-data lag.

    An unplugged receiver is reopened on the same port path every
    RECONNECT_INTERVAL seconds; until a new fix arrives the last position is
    kept, marked stale. Use a /dev/serial/by-id/... port in devices.yml so a
    replug that renumbers ttyACMn still finds it.
    """

    RECONNECT_INTERVAL = 1.0
    MAX_WAIT_FOR_FIX = 20.0

    def __init__(self, port: Optional[str] = None, baud: int = 9600, timeout: float = 1.0):
        resolved_port = port or find_gps_port()
        if not resolved_port:
            raise RuntimeError("No USB GPS receiver found. Please check connection or specify port.")

        self.port: str = resolved_port
        self.baud: int = int(baud)
        self.timeout: float = float(timeout)

        # Open serial port
        self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        self._lock = threading.Lock()
        self._running = True

        # State storage
        self.connected: bool = True
        self.stale: bool = False
        self.latitude: Optional[float] = None
        self.longitude: Optional[float] = None
        self.altitude: Optional[float] = None
        self.altitude_units: str = "M"
        self.satellites: int = 0
        self.speed_kmh: float = 0.0
        self.fix_quality: int = 0
        self.has_fix: bool = False
        self.hdop: Optional[float] = None
        self.pdop: Optional[float] = None
        self.vdop: Optional[float] = None
        self.timestamp: Optional[str] = None
        self.datestamp: Optional[str] = None
        self.last_update_ts: float = 0.0
        self._satellites_in_view: Dict[int, Dict[str, Any]] = {}
        self._raw_lines: List[str] = []

        # Start background polling thread
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()

    def _reader_loop(self):
        """Continuously reads NMEA sentences from serial buffer."""
        while self._running:
            try:
                line_bytes = self._ser.readline()
                if not line_bytes:
                    continue
                line = line_bytes.decode("ascii", errors="replace").strip()
                if not line.startswith("$"):
                    continue

                with self._lock:
                    self._raw_lines.append(line)
                    if len(self._raw_lines) > 50:
                        self._raw_lines.pop(0)

                try:
                    msg = pynmea2.parse(line)
                except Exception:
                    continue

                with self._lock:
                    # Update satellite count from GSV or GGA
                    if hasattr(msg, "num_sv_in_view") and msg.num_sv_in_view:
                        try:
                            self.satellites = int(msg.num_sv_in_view)
                        except (ValueError, TypeError):
                            pass
                    elif hasattr(msg, "num_sats") and msg.num_sats:
                        try:
                            self.satellites = int(msg.num_sats)
                        except (ValueError, TypeError):
                            pass

                    # RMC - Recommended Minimum Data
                    if isinstance(msg, pynmea2.types.talker.RMC):
                        if msg.status == "A" and msg.latitude != 0.0 and msg.longitude != 0.0:
                            self.latitude = float(msg.latitude)
                            self.longitude = float(msg.longitude)
                            self.has_fix = True
                            speed_knots = msg.spd_over_grnd or 0.0
                            self.speed_kmh = float(speed_knots) * 1.852 if speed_knots else 0.0
                            if msg.timestamp:
                                self.timestamp = str(msg.timestamp)
                            if msg.datestamp:
                                self.datestamp = str(msg.datestamp)
                            self.last_update_ts = time.time()
                            self.stale = False
                        else:
                            self.has_fix = False

                    # GGA - Fix Data & Altitude
                    elif isinstance(msg, pynmea2.types.talker.GGA):
                        qual = getattr(msg, "gps_qual", 0)
                        try:
                            self.fix_quality = int(qual)
                        except (ValueError, TypeError):
                            self.fix_quality = 0

                        if self.fix_quality > 0 and msg.latitude != 0.0 and msg.longitude != 0.0:
                            self.latitude = float(msg.latitude)
                            self.longitude = float(msg.longitude)
                            self.has_fix = True
                            if msg.altitude is not None:
                                self.altitude = float(msg.altitude)
                                self.altitude_units = msg.altitude_units or "M"
                            if msg.num_sats:
                                try:
                                    self.satellites = int(msg.num_sats)
                                except (ValueError, TypeError):
                                    pass
                            if msg.timestamp:
                                self.timestamp = str(msg.timestamp)
                            self.last_update_ts = time.time()
                            self.stale = False

                    # GLL - Geographic Position
                    elif isinstance(msg, pynmea2.types.talker.GLL):
                        if getattr(msg, "status", None) == "A" and msg.latitude != 0.0 and msg.longitude != 0.0:
                            self.latitude = float(msg.latitude)
                            self.longitude = float(msg.longitude)
                            self.has_fix = True
                            if msg.timestamp:
                                self.timestamp = str(msg.timestamp)
                            self.last_update_ts = time.time()
                            self.stale = False

                    # GSA - Dilution of Precision (PDOP, HDOP, VDOP)
                    elif isinstance(msg, pynmea2.types.talker.GSA):
                        for attr in ("pdop", "hdop", "vdop"):
                            val = getattr(msg, attr, None)
                            if val:
                                try:
                                    setattr(self, attr, float(val))
                                except (ValueError, TypeError):
                                    pass

                    # GSV - Satellites in View (PRN, Elevation, Azimuth, SNR/C_N0)
                    elif isinstance(msg, pynmea2.types.talker.GSV):
                        if getattr(msg, "msg_num", None) == "1":
                            self._satellites_in_view.clear()
                        for idx in range(1, 5):
                            prn_raw = getattr(msg, f"sv_prn_num_{idx}", None)
                            if not prn_raw:
                                continue
                            try:
                                prn = int(prn_raw)
                                elev = getattr(msg, f"elevation_deg_{idx}", None)
                                azim = getattr(msg, f"azimuth_{idx}", None)
                                snr = getattr(msg, f"snr_{idx}", None)
                                self._satellites_in_view[prn] = {
                                    "prn": prn,
                                    "elevation_deg": float(elev) if elev not in (None, "") else None,
                                    "azimuth_deg": float(azim) if azim not in (None, "") else None,
                                    "snr_db": float(snr) if snr not in (None, "") else None,
                                }
                            except (ValueError, TypeError):
                                pass

            except (serial.SerialException, OSError):
                self._reconnect()
            except Exception:
                time.sleep(0.1)
        try:
            self._ser.close()
        except Exception:
            pass

    def _reconnect(self):
        """Close the vanished port, then reopen the same path until it is back."""
        with self._lock:
            self.connected = False
            self.has_fix = False
            self.stale = True
        try:
            self._ser.close()
        except Exception:
            pass
        while self._running:
            time.sleep(self.RECONNECT_INTERVAL)
            if not self._running:
                return
            try:
                self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
            except (serial.SerialException, OSError):
                continue
            logger.info("GPS receiver back on %s", self.port)
            with self._lock:
                self.connected = True
            return

    def get_lat_long(self) -> Optional[Tuple[float, float]]:
        """Return (latitude, longitude) tuple or None if no valid fix."""
        with self._lock:
            if self.has_fix and self.latitude is not None and self.longitude is not None:
                return (self.latitude, self.longitude)
            return None

    def get_position(self) -> Dict[str, Any]:
        """Return comprehensive GPS telemetry dictionary."""
        with self._lock:
            dt_str = f"{self.datestamp or ''} {self.timestamp or ''}".strip() or None
            return {
                "latitude": self.latitude,
                "longitude": self.longitude,
                "altitude": self.altitude,
                "altitude_units": self.altitude_units,
                "satellites": self.satellites,
                "speed_kmh": round(self.speed_kmh, 2),
                "fix_quality": self.fix_quality,
                "has_fix": self.has_fix,
                "hdop": self.hdop,
                "pdop": self.pdop,
                "vdop": self.vdop,
                "datetime_utc": dt_str,
                "last_update_ts": self.last_update_ts,
                "connected": self.connected,
                "stale": self.stale,
            }

    def get_satellites_in_view(self) -> List[Dict[str, Any]]:
        """Return parsed list of satellites in view with PRN, elevation, azimuth, and SNR (dB-Hz)."""
        with self._lock:
            return [dict(v) for _, v in sorted(self._satellites_in_view.items())]

    def wait_for_fix(self, timeout: float = 30.0) -> bool:
        """Block until receiver acquires satellite fix, or timeout (capped at MAX_WAIT_FOR_FIX s) expires."""
        val = float(timeout)
        timeout = min(val, self.MAX_WAIT_FOR_FIX) if val > 0.0 else 0.0
        deadline = time.time() + timeout
        while True:
            with self._lock:
                if self.has_fix:
                    return True
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            time.sleep(min(0.5, remaining))

    def read_raw(self, count: int = 5) -> List[str]:
        """Return the most recent raw NMEA sentences."""
        if count <= 0:
            return []
        with self._lock:
            return list(self._raw_lines[-count:])

    def reset(self):
        """Reset internal telemetry cache."""
        with self._lock:
            self.latitude = None
            self.longitude = None
            self.altitude = None
            self.satellites = 0
            self.speed_kmh = 0.0
            self.has_fix = False
            self.fix_quality = 0
            self.hdop = None
            self.pdop = None
            self.vdop = None
            self.timestamp = None
            self.datestamp = None
            self.last_update_ts = 0.0
            self.stale = False
            self._satellites_in_view.clear()
            self._raw_lines.clear()

    def close(self):
        """Shutdown background thread and close serial connection."""
        self._running = False
        try:
            if self._thread.is_alive():
                self._thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            self._ser.close()
        except Exception:
            pass


def connect_gps(*, port: Optional[str] = None, baud: int = 9600, timeout: float = 1.0) -> Optional[GpsDevice]:
    """Factory helper to open GPS hardware and return GpsDevice instance."""
    try:
        dev = GpsDevice(port=port, baud=baud, timeout=timeout)
        print(f"[RemoteRF GPS] Successfully opened GPS receiver on {dev.port} at {dev.baud} baud")
        return dev
    except Exception as exc:
        print(f"[RemoteRF GPS] Failed to open GPS receiver: {exc}")
        return None


# =====================================================================
# RemoteRF Schema Definition
# =====================================================================

@idl_register("gps")
class GpsSchema(DeviceSchema):
    """
    RemoteRF Device Schema for USB GPS Receivers (u-blox 7 / VFAN UG-353).
    Exposes latitude, longitude, satellite tracking, fix status, and telemetry.
    """

    device_type = "gps"
    client_class = "GpsModule"
    driver_version = "0.2.0"

    @staticmethod
    def make_device(**kwargs) -> Optional[GpsDevice]:
        """Called by RemoteRF to instantiate the physical device from devices.yml."""
        port = kwargs.get("port")
        baud = kwargs.get("baud", 9600)
        timeout = kwargs.get("timeout", 1.0)
        return connect_gps(port=port, baud=baud, timeout=timeout)

    # -----------------------------------------------------------------
    # Properties (Getters)
    # -----------------------------------------------------------------

    @idl_expose(kind="get", doc="Current latitude in decimal degrees, or None if no fix.")
    def get_latitude(self) -> Optional[float]:
        return self.device.get_position()["latitude"]

    @idl_expose(kind="get", doc="Current longitude in decimal degrees, or None if no fix.")
    def get_longitude(self) -> Optional[float]:
        return self.device.get_position()["longitude"]

    @idl_expose(kind="get", doc="Current (latitude, longitude) tuple, or None if no fix.")
    def get_lat_long(self) -> Optional[Tuple[float, float]]:
        return self.device.get_lat_long()

    @idl_expose(kind="get", doc="Altitude in meters above sea level.")
    def get_altitude(self) -> Optional[float]:
        return self.device.get_position()["altitude"]

    @idl_expose(kind="get", doc="Number of satellites currently tracked / in view.")
    def get_satellites(self) -> int:
        return self.device.get_position()["satellites"]

    @idl_expose(kind="get", doc="True if receiver has valid 2D/3D satellite fix.")
    def get_has_fix(self) -> bool:
        return self.device.get_position()["has_fix"]

    @idl_expose(kind="get", doc="GPS fix quality indicator (0=No fix, 1=GPS fix, 2=DGPS fix).")
    def get_fix_quality(self) -> int:
        return self.device.get_position()["fix_quality"]

    @idl_expose(kind="get", doc="Ground speed in km/h.")
    def get_speed(self) -> float:
        return self.device.get_position()["speed_kmh"]

    @idl_expose(kind="get", doc="Latest UTC datetime string (YYYY-MM-DD HH:MM:SS).")
    def get_utc_time(self) -> Optional[str]:
        return self.device.get_position()["datetime_utc"]

    @idl_expose(kind="get", doc="True if the serial stream to the GPS receiver is open and active.")
    def get_connected(self) -> bool:
        return self.device.get_position()["connected"]

    @idl_expose(kind="get", doc="Serial port connected to the GPS receiver.")
    def get_port(self) -> str:
        return self.device.port

    @idl_expose(kind="get", doc="Horizontal Dilution of Precision (HDOP), or None if unavailable.")
    def get_hdop(self) -> Optional[float]:
        return self.device.get_position()["hdop"]

    @idl_expose(kind="get", doc="Position (3D) Dilution of Precision (PDOP), or None if unavailable.")
    def get_pdop(self) -> Optional[float]:
        return self.device.get_position()["pdop"]

    @idl_expose(kind="get", doc="Vertical Dilution of Precision (VDOP), or None if unavailable.")
    def get_vdop(self) -> Optional[float]:
        return self.device.get_position()["vdop"]

    # -----------------------------------------------------------------
    # Methods (Calls)
    # -----------------------------------------------------------------

    @idl_expose(kind="call", doc="Get current (latitude, longitude) tuple, or None if no fix.")
    def call_get_lat_long(self) -> Optional[Tuple[float, float]]:
        """Returns (latitude, longitude) as a tuple of floats."""
        return self.device.get_lat_long()

    @idl_expose(kind="call", doc="Get full position dictionary (lat, lon, altitude, satellites, speed, DOP, fix status).")
    def call_get_position(self) -> Dict[str, Any]:
        """Returns structured dictionary containing all GPS status and coordinate fields."""
        return self.device.get_position()

    @idl_expose(kind="call", doc="Get parsed list of satellites in view with PRN, elevation, azimuth, and SNR (dB-Hz).")
    def call_get_satellites_in_view(self) -> List[Dict[str, Any]]:
        """Returns list of dicts for each visible satellite parsed from GSV sentences."""
        return self.device.get_satellites_in_view()

    @idl_expose(kind="call", doc="Block until satellite fix is achieved or timeout expires.")
    def call_wait_for_fix(self, timeout: float = 30.0) -> bool:
        """Wait for receiver to achieve 2D/3D lock. Runs under the device lock, so GpsDevice caps it."""
        return self.device.wait_for_fix(timeout=float(timeout))

    @idl_expose(kind="call", doc="Read last N raw NMEA sentences from receiver buffer.")
    def call_read_raw(self, count: int = 5) -> List[str]:
        """Returns raw NMEA sentence strings."""
        return self.device.read_raw(count=int(count))

    @idl_expose(kind="call", doc="Reset internal GPS coordinate cache.")
    def call_reset(self) -> bool:
        """Clears cached coordinates and fix state."""
        self.device.reset()
        return True

    def reset_for_next_holder(self):
        """Called by the server (not exposed) when a reservation ends: drop the
        last holder's cached fix, DOP, satellites and NMEA lines. The serial reader keeps running."""
        if self.device is not None:
            self.device.reset()
