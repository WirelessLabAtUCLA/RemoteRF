"""TI AWR6843 driver against a fake serial port: no radar or pyserial needed."""

import struct
import sys
import time
import types

import numpy as np
import pytest

sys.modules.setdefault("serial", types.ModuleType("serial"))

from remoterf_device_core.drivers.ti_awr6843.mmwave import connection
from remoterf_device_core.drivers.ti_awr6843.mmwave.parser import MAGIC_WORD
from remoterf_device_core.drivers.ti_awr6843.ti_awr6843_schema import TiMmWaveDevice, TiMmWaveSchema

PROMPT = b"Done\nmmwDemo:/>"


class FakeSerial:
    ports = {}

    def __init__(self, port, baud, **_):
        self.port = port
        self.is_open = True
        self.written = []
        self._in = bytearray(FakeSerial.ports.pop(port, b""))
        FakeSerial.opened[port] = self

    @property
    def in_waiting(self):
        return len(self._in)

    def read(self, n):
        out, self._in = bytes(self._in[:n]), self._in[n:]
        return out

    def write(self, data):
        self.written.append(data.decode().strip())
        if self.port == "cli":
            self._in.extend(PROMPT)

    def reset_input_buffer(self):
        if self.port == "cli":
            self._in.clear()

    def reset_output_buffer(self):
        pass

    def close(self):
        self.is_open = False


@pytest.fixture
def schema(monkeypatch):
    FakeSerial.ports, FakeSerial.opened = {}, {}
    monkeypatch.setattr(connection, "serial", types.SimpleNamespace(
        Serial=FakeSerial, EIGHTBITS=8, PARITY_NONE="N", STOPBITS_ONE=1))
    monkeypatch.setattr(connection, "time", types.SimpleNamespace(time=time.time, sleep=lambda s: None))
    s = TiMmWaveSchema()
    s.device = TiMmWaveDevice(cli_port="cli", data_port="data")
    return s


def commands():
    return [c for c in FakeSerial.opened["cli"].written if c]


def test_configure_keeps_the_presets_own_values(schema):
    assert schema.call_configure(preset="fast-motion")
    assert (schema.get_mode(), schema.get_clutter_removal(), schema.get_fps()) == ("2d", True, 30.0)
    assert "clutterRemoval -1 1" in commands()
    assert any(c.startswith("frameCfg") and " 33.33 " in c for c in commands())
    config = schema.get_config()
    assert (config["preset"], config["mode"], config["clutter_removal"], config["fps"]) == \
        ("fast-motion", "2d", True, 30.0)
    assert schema.get_fps() == config["fps"] and schema.get_preset() == "fast-motion"


def test_explicit_values_override_the_preset_and_survive_a_later_setter(schema):
    schema.call_configure(preset="gesture", fps=20.0)
    assert schema.get_fps() == 20.0 and schema.get_mode() == "3d"
    schema.set_mode("2d")
    assert (schema.get_preset(), schema.get_fps(), schema.get_mode()) == ("gesture", 20.0, "2d")
    assert schema.get_config()["mode"] == "2d"


def test_apply_config_start_false_skips_sensor_start_and_passes_the_timeout(schema, monkeypatch):
    timeouts = []
    send = connection.RadarConnection.send_command
    monkeypatch.setattr(connection.RadarConnection, "send_command",
                        lambda self, cmd, timeout=0.8: timeouts.append(timeout) or send(self, cmd, timeout))
    assert schema.call_apply_config("sensorStop\nframeCfg 0 2 16 0 100 1 0\nsensorStart\n",
                                    start=False, command_timeout=3.0)
    assert commands() == ["sensorStop", "frameCfg 0 2 16 0 100 1 0"]
    assert timeouts == [3.0, 3.0]
    assert schema.get_is_running() is False


def test_configure_start_false_does_not_chirp(schema):
    schema.call_configure(preset="default", start=False)
    assert "sensorStart" not in commands()
    assert schema.get_is_running() is False


def test_calls_after_stop_still_work(schema):
    schema.call_configure()
    assert schema.call_stop()
    assert commands()[-1] == "sensorStop" and schema.get_is_running() is False
    assert FakeSerial.opened["cli"].is_open and FakeSerial.opened["data"].is_open
    assert "Done" in schema.call_query_version()
    schema.set_clutter_removal(True)
    assert commands()[-1] == "clutterRemoval -1 1"
    assert schema.call_start()
    assert commands()[-1] == "sensorStart"


def test_reset_for_next_holder_stops_and_restores_defaults(schema):
    schema.device.configure_preset(start=True)
    schema.device.save_defaults()
    schema.call_configure(preset="fast-motion")
    FakeSerial.opened["cli"].written.clear()

    schema.reset_for_next_holder()
    assert commands()[0] == "sensorStop"
    assert "sensorStart" not in commands()
    assert schema.get_is_running() is False
    assert (schema.get_preset(), schema.get_mode(), schema.get_fps()) == ("default", "3d", 10.0)


def _packet(payload, tlv_type=4):
    tlv = struct.pack("<II", tlv_type, len(payload)) + payload
    total = 40 + len(tlv)
    return struct.pack("<8sIIIIIIII", MAGIC_WORD, 0x03050004, total, 0x6843, 7, 0, 0, 1, 0) + tlv


def test_heatmap_with_odd_antenna_count_is_empty(schema):
    FakeSerial.ports["data"] = _packet(np.arange(12, dtype=np.int16).tobytes())  # 6 complex samples
    schema.device.radar.connection.connect()
    out = schema.call_capture_heatmap(timeout=0.5)
    assert out["frame_number"] == 7
    assert out["heatmap_db"].shape == (0, 0)


def test_heatmap_with_eight_antennas_is_two_dimensional(schema):
    FakeSerial.ports["data"] = _packet(np.ones(2 * 8 * 4, dtype=np.int16).tobytes())  # 4 range bins
    schema.device.radar.connection.connect()
    out = schema.call_capture_heatmap(num_angle_bins=16, timeout=0.5)
    assert out["heatmap_db"].shape == (4, 16)


@pytest.mark.parametrize("call", [
    lambda s: s.call_capture_heatmap(num_angle_bins=4),
    lambda s: s.call_capture_heatmap(num_angle_bins=4096),
    lambda s: s.call_capture_heatmap(timeout=31),
    lambda s: s.call_read_frame(timeout=0),
    lambda s: s.call_send_command("version", timeout=1e9),
    lambda s: s.call_stop(timeout=float("nan")),
    lambda s: s.call_apply_config("sensorStop\n", command_timeout=60),
    lambda s: s.call_apply_config("sensorStop\n" * 1000),
    lambda s: s.call_apply_config("%" * 100_000),
    lambda s: s.call_configure(mode="4d"),
])
def test_out_of_range_arguments_are_rejected(schema, call):
    with pytest.raises(ValueError):
        call(schema)
    assert "cli" not in FakeSerial.opened or not commands()


def test_port_guessing_only_without_configured_ports(monkeypatch, caplog):
    monkeypatch.setattr(connection.os.path, "exists", lambda p: False)
    monkeypatch.setattr(connection.glob, "glob",
                        lambda pat: ["/dev/ttyUSB0", "/dev/ttyUSB1"] if "USB" in pat else [])
    assert connection.find_radar_ports(fallback=False) == (None, None)
    with caplog.at_level("WARNING"):
        assert connection.find_radar_ports() == ("/dev/ttyUSB0", "/dev/ttyUSB1")
    assert "devices.yml" in caplog.text
