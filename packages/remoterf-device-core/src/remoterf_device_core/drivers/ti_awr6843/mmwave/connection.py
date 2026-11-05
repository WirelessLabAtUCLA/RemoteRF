import glob
import os
import time
from typing import List, Optional, Tuple
import serial


def find_radar_ports() -> Tuple[Optional[str], Optional[str]]:
    """
    Auto-detects the TI mmWave radar serial ports (CLI and Data).
    Returns (cli_port, data_port).
    """
    by_id_dir = "/dev/serial/by-id"
    cli_port = None
    data_port = None

    if os.path.exists(by_id_dir):
        entries = sorted(os.listdir(by_id_dir))
        for entry in entries:
            full_path = os.path.realpath(os.path.join(by_id_dir, entry))
            entry_lower = entry.lower()
            if "cp2105" in entry_lower or "texas_instruments" in entry_lower:
                if "if00" in entry_lower or "port0" in entry_lower:
                    cli_port = full_path
                elif "if01" in entry_lower or "port1" in entry_lower:
                    data_port = full_path

    # Fallback to standard /dev/ttyUSB* or /dev/ttyACM*
    if not cli_port or not data_port:
        usb_ports = sorted(glob.glob("/dev/ttyUSB*"))
        acm_ports = sorted(glob.glob("/dev/ttyACM*"))

        if len(usb_ports) >= 2:
            cli_port = usb_ports[0]
            data_port = usb_ports[1]
        elif len(acm_ports) >= 2:
            cli_port = acm_ports[0]
            data_port = acm_ports[1]

    return cli_port, data_port


class RadarConnection:
    """Manages serial connections to both CLI and Data ports of the radar."""

    def __init__(
        self,
        cli_port: Optional[str] = None,
        data_port: Optional[str] = None,
        cli_baud: int = 115200,
        data_baud: int = 921600,
    ):
        if not cli_port or not data_port:
            detected_cli, detected_data = find_radar_ports()
            cli_port = cli_port or detected_cli
            data_port = data_port or detected_data

        if not cli_port or not data_port:
            raise RuntimeError(
                "Could not find radar serial ports. Make sure the radar is connected over USB."
            )

        self.cli_port_name = cli_port
        self.data_port_name = data_port
        self.cli_baud = cli_baud
        self.data_baud = data_baud

        self.cli_ser: Optional[serial.Serial] = None
        self.data_ser: Optional[serial.Serial] = None

    def connect(self):
        """Opens serial connections to CLI and Data ports."""
        # Open data port first with appropriate buffer settings
        self.data_ser = serial.Serial(
            self.data_port_name,
            self.data_baud,
            timeout=0.1,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )
        self.data_ser.reset_input_buffer()
        self.data_ser.reset_output_buffer()

        # Open CLI control port
        self.cli_ser = serial.Serial(
            self.cli_port_name,
            self.cli_baud,
            timeout=1.0,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )
        self.cli_ser.reset_input_buffer()
        self.cli_ser.reset_output_buffer()

    def check_connection(self, timeout: float = 0.5) -> bool:
        """Checks if the radar CLI port is awake and responding."""
        if not self.cli_ser or not self.cli_ser.is_open:
            return False
        self.cli_ser.reset_input_buffer()
        self.cli_ser.write(b"\n")
        time.sleep(0.05)
        resp = self.cli_ser.read(self.cli_ser.in_waiting or 100).decode("utf-8", errors="ignore")
        return "mmwDemo:/>" in resp or "Done" in resp

    def send_command(self, cmd: str, timeout: float = 0.8) -> str:
        """Sends a single CLI command string and returns response."""
        if not self.cli_ser or not self.cli_ser.is_open:
            raise RuntimeError("CLI serial port is not open.")

        cmd = cmd.strip()
        self.cli_ser.reset_input_buffer()
        self.cli_ser.write((cmd + "\n").encode("utf-8"))
        time.sleep(0.02)

        start = time.time()
        response_lines: List[str] = []
        while time.time() - start < timeout:
            if self.cli_ser.in_waiting > 0:
                chunk = self.cli_ser.read(self.cli_ser.in_waiting).decode("utf-8", errors="ignore")
                response_lines.append(chunk)
                full = "".join(response_lines)
                if "Done" in full or "Error" in full or "mmwDemo:/>" in full:
                    break
            time.sleep(0.01)

        return "".join(response_lines).strip()

    def send_config_file(self, config_filepath: str, verbose: bool = True) -> bool:
        """
        Loads a .cfg file and sends each command line to the radar.
        Validates that every command is accepted by the radar CLI.
        """
        if not os.path.isfile(config_filepath):
            raise FileNotFoundError(f"Config file not found: {config_filepath}")

        # Check if CLI is awake
        if not self.check_connection():
            raise RuntimeError(
                "Radar CLI port (/dev/ttyUSB0) is not responding.\n"
                "In TI mmWave SDK 3.x, when a radar sensor is stopped the RF firmware halts.\n"
                "--> Please press the physical NRST/RESET button (SW2) on the AWR6843 board "
                "(or unplug and replug the USB cable) to reset the sensor, then rerun."
            )

        with open(config_filepath, "r") as f:
            lines = f.readlines()

        if verbose:
            print(f"Applying configuration: {config_filepath}")

        for raw_line in lines:
            line = raw_line.strip()
            # Skip empty lines and comment lines starting with '%'
            if not line or line.startswith("%"):
                continue

            resp = self.send_command(line)
            if not resp:
                raise RuntimeError(
                    f"Radar timed out without acknowledging command: '{line}'.\n"
                    "The radar may be halted. Please press the NRST/RESET button (SW2) on the board or replug USB."
                )
            if "Error" in resp:
                raise RuntimeError(
                    f"Radar returned error on command '{line}': {resp}"
                )
            if verbose:
                print(f"  [OK] {line}")

        return True

    def send_config_text(self, config_text: str, verbose: bool = True) -> bool:
        """
        Sends configuration commands from a multiline string directly to the radar.
        """
        # Check if CLI is awake
        if not self.check_connection():
            raise RuntimeError(
                "Radar CLI port (/dev/ttyUSB0) is not responding.\n"
                "In TI mmWave SDK 3.x, when a radar sensor is stopped the RF firmware halts.\n"
                "--> Please press the physical NRST/RESET button (SW2) on the AWR6843 board "
                "(or unplug and replug the USB cable) to reset the sensor, then rerun."
            )

        lines = config_text.strip().splitlines()

        for raw_line in lines:
            line = raw_line.strip()
            # Skip empty lines and comment lines starting with '%'
            if not line or line.startswith("%"):
                continue

            resp = self.send_command(line)
            if not resp:
                raise RuntimeError(
                    f"Radar timed out without acknowledging command: '{line}'.\n"
                    "The radar may be halted. Please press the NRST/RESET button (SW2) on the board or replug USB."
                )
            if "Error" in resp:
                raise RuntimeError(
                    f"Radar returned error on command '{line}': {resp}"
                )
            if verbose:
                print(f"  [OK] {line}")

        return True

    def read_data(self, max_bytes: int = 4096) -> bytes:
        """Reads raw bytes available on the Data port."""
        if not self.data_ser or not self.data_ser.is_open:
            return b""
        n = self.data_ser.in_waiting
        if n > 0:
            return self.data_ser.read(min(n, max_bytes))
        return b""

    def stop_sensor(self):
        """Sends sensorStop command to CLI port."""
        if self.cli_ser and self.cli_ser.is_open:
            try:
                self.cli_ser.write(b"sensorStop\n")
                time.sleep(0.05)
                self.cli_ser.reset_input_buffer()
            except Exception:
                pass

    def close(self):
        """Stops the sensor and closes both serial ports."""
        self.stop_sensor()
        if self.cli_ser and self.cli_ser.is_open:
            self.cli_ser.close()
        if self.data_ser and self.data_ser.is_open:
            self.data_ser.close()
