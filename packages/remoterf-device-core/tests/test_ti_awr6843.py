"""TI AWR6843 driver against a fake serial port: no radar or pyserial needed."""

import importlib
import struct
import sys
import time
import types

import numpy as np
import pytest

from remoterf_device_core.idl import schema as idl_schema

MAGIC_WORD = b"\x02\x01\x04\x03\x06\x05\x08\x07"
PROMPT = b"Done\nmmwDemo:/>"


@pytest.fixture(scope="module")
def ti():
    prefix = "remoterf_device_core.drivers.ti_awr6843"
    saved_serial = sys.modules.get("serial")
    had_driver = {
        k: v for k, v in sys.modules.items()
        if k == prefix or k.startswith(prefix + ".")
    }
    saved_registry = dict(idl_schema._DRIVER_REGISTRY)
    inserted_fake = saved_serial is None
    if inserted_fake:
        sys.modules["serial"] = types.ModuleType("serial")
    def _restore_driver_modules():
        for k in list(sys.modules):
            if (k == prefix or k.startswith(prefix + ".")) and k not in had_driver:
                sys.modules.pop(k, None)
        sys.modules.update(had_driver)
        drivers_pkg = sys.modules.get("remoterf_device_core.drivers")
        if drivers_pkg is not None and prefix not in had_driver and hasattr(drivers_pkg, "ti_awr6843"):
            delattr(drivers_pkg, "ti_awr6843")
        idl_schema._DRIVER_REGISTRY.clear()
        idl_schema._DRIVER_REGISTRY.update(saved_registry)

    try:
        connection = importlib.import_module(f"{prefix}.mmwave.connection")
        parser = importlib.import_module(f"{prefix}.mmwave.parser")
        radar = importlib.import_module(f"{prefix}.mmwave.radar")
        schema_mod = importlib.import_module(f"{prefix}.ti_awr6843_schema")
    except Exception:
        if inserted_fake:
            sys.modules.pop("serial", None)
            _restore_driver_modules()
        raise
    else:
        if inserted_fake:
            sys.modules.pop("serial", None)

    try:
        yield types.SimpleNamespace(
            connection=connection,
            parser=parser,
            radar=radar,
            schema=schema_mod,
        )
    finally:
        if inserted_fake:
            _restore_driver_modules()


class FakeSerial:
    ports = {}
    opened = {}
    fail_open = set()
    response_fn = None

    def __init__(self, port, baud, **_):
        if port in FakeSerial.fail_open:
            raise OSError(f"cannot open {port}")
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
        cmd = data.decode().strip()
        self.written.append(cmd)
        if self.port == "cli":
            if FakeSerial.response_fn is not None:
                reply = FakeSerial.response_fn(cmd)
                if reply:
                    self._in.extend(reply)
            else:
                self._in.extend(PROMPT)

    def reset_input_buffer(self):
        if self.port == "cli":
            self._in.clear()

    def reset_output_buffer(self):
        pass

    def close(self):
        self.is_open = False


@pytest.fixture
def schema(ti, monkeypatch):
    FakeSerial.ports, FakeSerial.opened = {}, {}
    FakeSerial.fail_open, FakeSerial.response_fn = set(), None
    monkeypatch.setattr(ti.connection, "serial", types.SimpleNamespace(
        Serial=FakeSerial, EIGHTBITS=8, PARITY_NONE="N", STOPBITS_ONE=1))
    monkeypatch.setattr(ti.connection, "time", types.SimpleNamespace(time=time.time, sleep=lambda s: None))
    s = ti.schema.TiMmWaveSchema()
    s.device = ti.schema.TiMmWaveDevice(cli_port="cli", data_port="data")
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


def test_apply_config_start_false_skips_sensor_start_and_passes_the_timeout(schema, ti, monkeypatch):
    timeouts = []
    send = ti.connection.RadarConnection.send_command
    monkeypatch.setattr(ti.connection.RadarConnection, "send_command",
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


def test_port_guessing_only_without_configured_ports(ti, monkeypatch, caplog):
    monkeypatch.setattr(ti.connection.os.path, "exists", lambda p: False)
    monkeypatch.setattr(ti.connection.glob, "glob",
                        lambda pat: ["/dev/ttyUSB0", "/dev/ttyUSB1"] if "USB" in pat else [])
    assert ti.connection.find_radar_ports(fallback=False) == (None, None)
    with caplog.at_level("WARNING"):
        assert ti.connection.find_radar_ports() == ("/dev/ttyUSB0", "/dev/ttyUSB1")
    assert "devices.yml" in caplog.text


def test_driver_test_does_not_shadow_pyserial(ti):
    serial_mod = sys.modules.get("serial")
    assert serial_mod is None or hasattr(serial_mod, "Serial")


def test_reset_keeps_settings_consistent_flushes_and_isolates_failures(schema, ti, monkeypatch):
    schema.device.configure_preset(start=True)
    schema.device.save_defaults()
    schema.call_configure(preset="fast-motion", start=True)
    schema.device.radar.parser._buffer.extend(b"stale")
    FakeSerial.opened["cli"].written.clear()

    timeouts = []
    orig_send = ti.connection.RadarConnection.send_command
    monkeypatch.setattr(
        ti.connection.RadarConnection,
        "send_command",
        lambda self, cmd, timeout=0.8: timeouts.append(timeout) or orig_send(self, cmd, timeout),
    )
    FakeSerial.response_fn = lambda cmd: b"Error -1\nmmwDemo:/>" if cmd.startswith("frameCfg") else PROMPT

    with pytest.raises(RuntimeError):
        schema.device.reset(timeout=3.0)

    # Transmitter stopped, settings did not lie about restoring defaults, buffer still flushed.
    assert schema.get_is_running() is False
    assert schema.get_preset() == "fast-motion"
    assert len(schema.device.radar.parser._buffer) == 0
    assert timeouts and all(t == 3.0 for t in timeouts)
    assert "sensorStart" not in commands()

    # If stop() fails first, reset still runs later steps and re-raises the first error.
    schema.device.radar.parser._buffer.extend(b"stale2")
    FakeSerial.response_fn = None
    FakeSerial.opened["cli"].written.clear()
    monkeypatch.setattr(schema.device, "stop", lambda timeout=5.0: (_ for _ in ()).throw(RuntimeError("stop boom")))
    with pytest.raises(RuntimeError, match="stop boom"):
        schema.device.reset(timeout=3.0)
    assert len(schema.device.radar.parser._buffer) == 0
    assert schema.get_is_running() is False
    assert "sensorStart" not in commands()


def test_close_and_init_errors_do_not_leak_serial_ports(schema, ti):
    schema.call_configure(preset="default", start=True)
    assert FakeSerial.opened["cli"].is_open and FakeSerial.opened["data"].is_open

    schema.device.close()
    assert not FakeSerial.opened["cli"].is_open and not FakeSerial.opened["data"].is_open
    assert schema.get_is_running() is False
    assert commands()[-1] == "sensorStop"

    # make_device closes opened ports when initial config upload fails.
    FakeSerial.opened.clear()
    FakeSerial.response_fn = lambda cmd: b"Error -1\nmmwDemo:/>" if cmd.startswith("frameCfg") else PROMPT
    assert ti.schema.TiMmWaveSchema.make_device(cli_port="cli", data_port="data") is None
    assert not FakeSerial.opened["cli"].is_open and not FakeSerial.opened["data"].is_open

    # RadarConnection.connect closes data_ser if opening cli_ser fails.
    FakeSerial.opened.clear()
    FakeSerial.response_fn = None
    FakeSerial.fail_open = {"cli"}
    conn = ti.connection.RadarConnection(cli_port="cli", data_port="data")
    with pytest.raises(OSError):
        conn.connect()
    assert not FakeSerial.opened["data"].is_open
    assert conn.data_ser is None and conn.cli_ser is None

    # RadarConnection.close still closes data_ser if closing cli_ser raises.
    FakeSerial.opened.clear()
    FakeSerial.fail_open = set()
    conn.connect()
    conn.cli_ser.close = lambda: (_ for _ in ()).throw(OSError("cli close boom"))
    with pytest.raises(OSError, match="cli close boom"):
        conn.close()
    assert not FakeSerial.opened["data"].is_open


def test_heatmap_preset_applies_overrides_and_rejects_2d_mode(schema):
    assert schema.call_configure(preset="heatmap", fps=20.0, clutter_removal=True)
    assert (schema.get_preset(), schema.get_mode(), schema.get_fps(), schema.get_clutter_removal()) == (
        "heatmap", "3d", 20.0, True,
    )
    assert "guiMonitor -1 1 0 0 1 0 0" in commands()
    assert "clutterRemoval -1 1" in commands()
    assert any(c.startswith("frameCfg") and " 50.00 " in c for c in commands())

    FakeSerial.opened["cli"].written.clear()
    schema.set_fps(10.0)
    assert schema.get_fps() == 10.0 and schema.get_config()["fps"] == 10.0
    assert "guiMonitor -1 1 0 0 1 0 0" in commands()
    assert any(c.startswith("frameCfg") and " 100.00 " in c for c in commands())

    with pytest.raises(ValueError):
        schema.call_configure(preset="heatmap", mode="2d")
    with pytest.raises(ValueError):
        schema.set_mode("2d")
    assert schema.get_preset() == "heatmap" and schema.get_mode() == "3d"


def test_property_setters_validate_connect_rollback_and_clear_config_file(schema):
    for bad in (
        lambda s: s.set_fps(float("nan")),
        lambda s: s.set_fps(float("inf")),
        lambda s: s.set_fps(0.0),
        lambda s: s.set_min_range(float("nan")),
        lambda s: s.set_min_range(-1.0),
        lambda s: s.set_min_range(10.0),
        lambda s: s.set_max_range(float("inf")),
        lambda s: s.set_max_range(0.0),
        lambda s: s.set_max_range(0.1),
        lambda s: s.set_preset("bogus"),
        lambda s: s.set_sensitivity("ultra"),
        lambda s: s.set_mode("4d"),
    ):
        with pytest.raises(ValueError):
            bad(schema)
    assert "cli" not in FakeSerial.opened
    assert schema.get_fps() == 10.0 and schema.get_preset() == "default"

    # Setter opens serial ports if closed and uploads to hardware.
    schema.set_fps(15.0)
    assert FakeSerial.opened["cli"].is_open
    assert any(c.startswith("frameCfg") and " 66.67 " in c for c in commands())
    assert schema.get_fps() == 15.0

    # Setter clears stale _config_file left by apply_config.
    schema.call_apply_config("sensorStop\nframeCfg 0 2 16 0 100 1 0\nsensorStart\n")
    assert schema.get_config()["config_file"] == "custom_text"
    schema.set_fps(20.0)
    assert schema.get_config()["config_file"] is None
    assert schema.get_fps() == 20.0

    # Upload failure rolls back _settings.
    FakeSerial.response_fn = lambda cmd: b"Error -1\nmmwDemo:/>" if cmd.startswith("frameCfg") else PROMPT
    with pytest.raises(RuntimeError):
        schema.set_fps(25.0)
    assert schema.get_fps() == 20.0

    # clutter_removal does not record new state when the CLI rejects it.
    FakeSerial.response_fn = lambda cmd: b"Error -1\nmmwDemo:/>"
    assert schema.get_clutter_removal() is False
    with pytest.raises(RuntimeError):
        schema.set_clutter_removal(True)
    assert schema.get_clutter_removal() is False
    assert schema.device.set_clutter_removal(True) is False
    assert schema.get_clutter_removal() is False


def test_timeout_cap_and_apply_config_total_deadline(schema, ti, monkeypatch):
    assert ti.schema.MAX_TIMEOUT_S == 20.0
    for call in (
        lambda s: s.call_read_frame(timeout=25.0),
        lambda s: s.call_send_command("version", timeout=25.0),
        lambda s: s.call_apply_config("sensorStop\n", command_timeout=25.0),
    ):
        with pytest.raises(ValueError):
            call(schema)

    clock = [1000.0]
    seen_timeouts = []
    monkeypatch.setattr(ti.connection, "time", types.SimpleNamespace(time=lambda: clock[0], sleep=lambda s: None))
    orig_send = ti.connection.RadarConnection.send_command

    def timed_send(self, cmd, timeout=0.8):
        seen_timeouts.append(timeout)
        clock[0] += 6.0
        return orig_send(self, cmd, timeout)

    monkeypatch.setattr(ti.connection.RadarConnection, "send_command", timed_send)
    cfg = "sensorStop\nflushCfg\ndfeDataOutputMode 1\nchannelCfg 15 7 0\nsensorStart\n"
    with pytest.raises(RuntimeError, match="total timeout"):
        schema.call_apply_config(cfg, command_timeout=10.0)
    assert seen_timeouts == [10.0, 10.0, 8.0, 2.0]


def test_heatmap_tlv_malformed_byte_lengths_do_not_crash(schema, ti):
    for n in (0, 1, 2, 3, 5, 6, 7, 24, 31, 33, 34):
        frames = ti.parser.TLVParser().feed(_packet(bytes(range(n))))
        assert len(frames) == 1
        assert frames[0].azimuth_static_heatmap is None

    FakeSerial.ports["data"] = _packet(b"\x01\x02\x03\x04\x05")
    schema.device.radar.connection.connect()
    out = schema.call_capture_heatmap(timeout=0.5)
    assert out["frame_number"] == 7
    assert out["heatmap_db"].shape == (0, 0)


def test_is_running_tracks_actual_start_and_config_commands(schema, ti, monkeypatch):
    schema.call_configure(preset="default", start=False)
    assert schema.get_is_running() is False

    # Timeout on sensorStart leaves is_running False.
    clock = [100.0]
    monkeypatch.setattr(
        ti.connection,
        "time",
        types.SimpleNamespace(time=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s)),
    )
    FakeSerial.response_fn = lambda cmd: b""
    assert schema.call_start(timeout=0.5) is False
    assert schema.get_is_running() is False

    # Error response on sensorStart also leaves is_running False.
    FakeSerial.response_fn = lambda cmd: b"Error -1\nmmwDemo:/>"
    assert schema.call_start() is False
    assert schema.get_is_running() is False

    # apply_config reflects whether sensorStart (not followed by sensorStop) was sent.
    FakeSerial.response_fn = None
    assert schema.call_apply_config("sensorStop\nframeCfg 0 2 16 0 100 1 0\n", start=True)
    assert schema.get_is_running() is False
    assert schema.call_apply_config("sensorStart\nsensorStop\n", start=True)
    assert schema.get_is_running() is False
    assert schema.call_apply_config("sensorStop\nsensorStart\n", start=True)
    assert schema.get_is_running() is True

    # Failed sensorStop does not falsely clear is_running.
    FakeSerial.response_fn = lambda cmd: b"Error -1\nmmwDemo:/>"
    assert schema.call_stop() is False
    assert schema.get_is_running() is True
