"""Pluto policy checks use fake radios only; importing adi must never open libiio."""

import importlib.util
import logging
from pathlib import Path
import string
import sys
import threading
import types

import numpy as np
import pytest

sys.modules.setdefault("adi", types.ModuleType("adi"))

from remoterf_device_core.drivers.adalm_pluto.pluto_schema import (
    LOCKOUT_KEYS, PlutoSchema, _LOCKOUT_METHODS, parse_lockouts,
)
from remoterf_device_core.drivers.adalm_pluto.pluto_mimo_schema import PlutoMIMOSchema
from remoterf_device_core.drivers.adalm_pluto.poe import PoePluto
from remoterf_device_core.idl import idl_expose
from remoterf_device_core.v2_errors import RemoteRFPolicyError


BAND = {"tx_min_freq": 902e6, "tx_max_freq": 928e6}
LO_ERROR = "TX LO 901 MHz is outside this device's TX range 902–928 MHz."
BAND_ERROR = ("TX refused: the signal would occupy 901–929 MHz (tx_lo ± sample_rate/2), "
              "outside this device's TX range 902–928 MHz.")
GAIN_ERROR = "TX gain -9 dB is above this device's limit of -10 dB."
DISABLED = "TX is disabled on this device (receive-only)."
LOOPBACK_ERROR = "Loopback 2 (RF) is not allowed on this device; use 0 (off) or 1 (digital)."
DDS_ERROR = "DDS tones are disabled on this device; transmit samples with tx() instead."
BROKEN = "This device is locked until it can be reset to a safe state; try again shortly."


class FakeRadio:
    def __setattr__(self, name, value):
        if name in {"tx_lo", "rx_lo", "sample_rate", "tx_rf_bandwidth", "rx_rf_bandwidth"}:
            if not isinstance(value, int):
                raise Exception("Value must be an int")
        super().__setattr__(name, value)

    def __init__(self):
        self.tx_lo = 915_000_000
        self.rx_lo = 915_000_000
        self.sample_rate = 1_000_000
        self.tx_rate = 1_000_000
        self.tx_hardwaregain_chan0 = -20
        self.tx_hardwaregain_chan1 = -20
        self.tx_enabled_channels = [0, 1]
        self.rx_enabled_channels = [0, 1]
        self.dds_scales = [.9] * 8
        self.tx_cyclic_buffer = True
        self.loopback = 2
        self.events = []
        self.fail_dds = False
        self.fail_tx = False
        self.fail_stop = False
        self.uri = "fake:radio"

    def _get_iio_attr(self, channel, name, output):
        assert (channel, name) == ("voltage0", "sampling_frequency")
        return self.tx_rate if output else self.sample_rate

    def disable_dds(self):
        self.events.append("disable_dds")
        if self.fail_dds:
            raise RuntimeError("DDS stop failed")
        self.dds_enabled = [0] * 8

    def tx_destroy_buffer(self):
        self.events.append("tx_destroy_buffer")
        if self.fail_stop:
            raise RuntimeError("buffer stop failed")

    def rx_destroy_buffer(self):
        self.events.append("rx_destroy_buffer")

    def tx(self, samples):
        self.events.append("tx")
        self.samples = samples
        if self.fail_tx:
            raise RuntimeError("TX failed after starting")

    def dds_single_tone(self, frequency, scale, channel=0):
        self.tone = (frequency, scale, channel)

    def dds_dual_tone(self, frequency1, scale1, frequency2, scale2, channel=0):
        self.tone = (frequency1, scale1, frequency2, scale2, channel)


class FakePoe(PoePluto):
    def __init__(self, radio=None):
        vars(self).update(_pluto=radio, fault=None, _lock=threading.RLock(),
                          ups=0, downs=0, fail_down=False, uri="fake:poe", _port="fake")

    def power_up(self):
        vars(self)["ups"] += 1
        if vars(self)["_pluto"] is None:
            radio = FakeRadio()
            radio.tx_lo = 2_450_000_000
            radio.tx_hardwaregain_chan0 = 0
            radio.tx_hardwaregain_chan1 = 0
            vars(self)["_pluto"] = radio

    def power_down(self):
        vars(self)["downs"] += 1
        if vars(self)["fail_down"]:
            raise RuntimeError("power-off failed")
        vars(self)["_pluto"] = None

    def power_cycle(self):
        self.power_down()
        self.power_up()

    def __getattr__(self, name):
        # Hygiene/handle checks must never get here, even while the radio is on.
        raise AssertionError(f"proxy attribute read: {name}")


class CustomMIMO(PlutoMIMOSchema):
    @idl_expose(kind="call")
    def call_camera_capture(self):
        return "frame"

    @idl_expose(kind="set")
    def set_future_tx_mode(self, value):
        self.device.future_tx_mode = value

    @idl_expose(kind="call")
    def call_reconnect(self):
        vars(self.device)["_pluto"] = FakeRadio()
        vars(self.device)["_pluto"].tx_lo = 2_450_000_000

    @idl_expose(kind="call")
    def call_reconnect_then_fail(self):
        self.call_reconnect()
        raise RuntimeError("reconnect failure")


def schema(config=None, cls=PlutoSchema, radio=None):
    obj = cls()
    obj.bind(radio if radio is not None else FakeRadio())
    obj.configure_lockouts(parse_lockouts(BAND if config is None else config))
    return obj


def rpc(obj, name, value=None, **kwargs):
    return obj.dispatch(name, kwargs if kwargs else ({"value": value} if value is not None else {}))


def refused(obj, name, expected, value=None, **kwargs):
    with pytest.raises(RemoteRFPolicyError) as exc:
        rpc(obj, name, value, **kwargs)
    assert str(exc.value) == expected
    return str(exc.value)


@pytest.mark.parametrize("init", [None, {}, {"serial": "abc", "poe": True}, {"usb_port": "1-2"},
                                {"label": "custom", "metadata": {}}])
def test_no_lockouts(init):
    assert parse_lockouts(init) is None


def test_keys_and_defaults():
    assert LOCKOUT_KEYS == frozenset({
        "tx_min_freq", "tx_max_freq", "admin_group_override", "tx_max_gain_db", "tx_enabled",
        "park_tx_lo", "allow_rf_loopback", "allow_dds", "tx_max_dds_scale", "allow_cyclic_tx",
        "tx_max_buffer_samples", "max_sample_rate", "tx_allowed_channels", "rx_min_freq",
        "rx_max_freq", "lockout_allow_methods",
    })
    cfg = parse_lockouts({"tx_min_freq": "902e6", "tx_max_freq": 928_000_000})
    assert cfg["tx_min_freq"] == 902e6 and cfg["park_tx_lo"] == 915e6
    assert not cfg["allow_rf_loopback"]
    assert cfg["allow_dds"] and cfg["allow_cyclic_tx"] and cfg["tx_enabled"]
    assert not cfg["admin_group_override"]
    assert parse_lockouts({"rx_min_freq": 70e6, "rx_max_freq": 6e9})["allow_rf_loopback"]


@pytest.mark.parametrize("key", sorted(LOCKOUT_KEYS))
def test_case_variants_rejected(key):
    with pytest.raises(ValueError, match="must be lowercase"):
        parse_lockouts({key.upper(): True})


@pytest.mark.parametrize("init", [
    [], {1: True}, {"tx_min_freq": 902, "tx_max_freq": 928},
    {"tx_min_freq": 902e6}, {"tx_max_freq": 928e6},
    {"rx_min_freq": 70e6}, {"rx_max_freq": 6e9},
    {"tx_min_freq": 928e6, "tx_max_freq": 902e6},
    {"rx_min_freq": 915e6, "rx_max_freq": 915e6},
    {"tx_min_freq": 69e6, "tx_max_freq": 928e6},
    {"tx_min_freq": 902e6, "tx_max_freq": 6.1e9},
    {"max_sample_rate": 520999}, {"max_sample_rate": 61.45e6},
    {"tx_enabled": "false"}, {"allow_dds": 1}, {"allow_rf_loopback": None},
    {"allow_cyclic_tx": []}, {"admin_group_override": 0},
    {"tx_max_gain_db": float("nan")}, {"tx_max_gain_db": -90}, {"tx_max_gain_db": 1},
    {"tx_max_dds_scale": float("inf")}, {"tx_max_dds_scale": -0.1}, {"tx_max_dds_scale": 1.1},
    {"tx_max_buffer_samples": True}, {"tx_max_buffer_samples": 0}, {"tx_max_buffer_samples": 1.5},
    {"tx_allowed_channels": []}, {"tx_allowed_channels": [0, 0]},
    {"tx_allowed_channels": [True]}, {"tx_allowed_channels": [2]}, {"tx_allowed_channels": "0"},
    {"lockout_allow_methods": "call_camera_capture"}, {"lockout_allow_methods": [1]},
    {"lockout_allow_methods": ["get_something"]},
    {**BAND, "park_tx_lo": 929e6}, {**BAND, "tx_lo": 901e6},
    {"rx_min_freq": 902e6, "rx_max_freq": 928e6, "rx_lo": 929e6},
    {"tx_min_freq": True, "tx_max_freq": 928e6},
    {"tx_min_freq": [902e6], "tx_max_freq": 928e6},
    {"tx_min_freq": "nan", "tx_max_freq": 928e6},
    {"tx_min_freq": "bad\nvalue", "tx_max_freq": 928e6},
    {"park_tx_lo": 6.1e9},
])
def test_bad_configs(init):
    with pytest.raises(ValueError) as exc:
        parse_lockouts(init)
    assert "\n" not in str(exc.value)


@pytest.mark.parametrize("key", [
    "tx_max_frequ", "tx_gian_gain", "my_lockout", "Admin", "allow_magic", "override_tx",
    "tx_enable", "rx_only", "tx_enabld", "rx_custom", "enable_tx", "disable_tx", "receive_only",
    "sample_rates", "max_scale", "dds_limit", "cyclic_tx", "buffer_limit", "park_lo",
    "loopback_mode", "allowed_channel", "poe_allow_tx", "TX_ENABLE", "RX_ONLY",
])
def test_suspicious_keys_rejected_even_without_a_valid_key(key):
    with pytest.raises(ValueError, match="unknown lockout-like"):
        parse_lockouts({key: 1})


@pytest.mark.parametrize("key", sorted(LOCKOUT_KEYS))
def test_all_single_edit_lockout_typos_rejected(key):
    known = {
        "tx_lo", "rx_lo", "sample_rate", "tx_rf_bandwidth", "rx_rf_bandwidth",
        "rx_buffer_size", "tx_enabled_channels", "rx_enabled_channels", "serial", "ip",
        "usb", "usb_port", "ykush", "ykush_cmd", "multi_tap", "poe", "POE",
        "poe_port", "poe_switch", "poe_user", "poe_password",
    }
    characters = string.printable + "é"
    typos = {key[:i] + key[i + 1:] for i in range(len(key))}
    typos.update(key[:i] + key[i + 1] + key[i] + key[i + 2:] for i in range(len(key) - 1))
    typos.update(key[:i] + char + key[i + 1:] for i in range(len(key)) for char in characters)
    typos.update(key[:i] + char + key[i:] for i in range(len(key) + 1) for char in characters)
    for typo in sorted(typos - LOCKOUT_KEYS - known):
        with pytest.raises(ValueError) as exc:
            parse_lockouts({typo: 1})
        assert "\n" not in str(exc.value)


@pytest.mark.parametrize("key", ["tx_lo", "rx_lo", "sample_rate", "tx_rf_bandwidth", "rx_rf_bandwidth",
                                 "rx_buffer_size", "tx_enabled_channels", "rx_enabled_channels", "serial",
                                 "ip", "usb", "usb_port", "poe", "POE", "poe_port", "poe_switch",
                                 "poe_user", "poe_password", "ykush", "ykush_cmd", "multi_tap"])
def test_known_driver_keys(key):
    assert parse_lockouts({key: "driver value"}) is None


@pytest.mark.parametrize("init", [
    {"serial": "fake", "poe": True, "ip": "192.168.4.101", "poe_port": "ether2"},
    {"serial": "fake", "ykush": "1"},
    {"serial": "fake", "ykush": "1", "ykush_cmd": "~/bin/ykush"},
    {"ip": "192.168.4.101", "poe": True, "poe_port": "ether2", "serial": "fake",
     "poe_switch": "192.168.88.1", "poe_user": "admin"},
    {"ip": "192.168.4.101"},
    {"rx_enabled_channels": [0, 1], "tx_enabled_channels": [0, 1]},
    {"multi_tap": True, "POE": True},
    {"serial": "fake", "POE": True, "ip": "192.168.4.101", "poe_port": "ether2",
     "poe_switch": "192.168.88.1", "poe_user": "admin", "rx_enabled_channels": [0, 1],
     "tx_enabled_channels": [0, 1], "sample_rate": 1_000_000, "rx_lo": 915_000_000,
     "tx_lo": 915_000_000, "rx_rf_bandwidth": 1_000_000, "tx_rf_bandwidth": 1_000_000,
     "rx_buffer_size": 1024},
    {"serial": "fake", "ykush": "1", "ykush_cmd": "~/bin/ykush", "usb_port": "1-2",
     "multi_tap": True, "tx_enabled_channels": [0], "rx_enabled_channels": [0]},
])
def test_production_driver_init_shapes_with_and_without_lockouts(init):
    assert parse_lockouts(init) is None
    assert parse_lockouts({**init, **BAND})["tx_enabled"]


@pytest.mark.parametrize("key,low,high", [
    ("tx_min_freq", 70e6, 6e9), ("rx_min_freq", 70e6, 6e9),
    ("max_sample_rate", 521e3, 61.44e6), ("tx_max_gain_db", -89.75, 0),
    ("tx_max_dds_scale", 0, 1), ("park_tx_lo", 70e6, 6e9),
])
def test_config_bounds_inclusive(key, low, high):
    for value in (low, high):
        init = {key: value}
        if key.endswith("min_freq"):
            init[key] = low
            init[key.replace("min", "max")] = high
        assert parse_lockouts(init)[key] == init[key]


@pytest.mark.parametrize("cls", [PlutoSchema, PlutoMIMOSchema, CustomMIMO])
def test_inherited_guard_and_lo_boundaries(cls):
    obj = schema(cls=cls)
    for value in (902_000_000, 928_000_000):
        rpc(obj, "set_tx_lo", value)
        assert obj.device.tx_lo == value
        assert type(obj.device.tx_lo) is int
    refused(obj, "set_tx_lo", LO_ERROR, 901e6)
    assert cls.dispatch.__qualname__ == "PlutoSchema.dispatch"


@pytest.mark.parametrize("name,value", [
    ("tx_lo", 915_000_000), ("rx_lo", 915_000_000), ("sample_rate", 1_000_000),
    ("tx_rf_bandwidth", 1_000_000), ("rx_rf_bandwidth", 1_000_000),
])
@pytest.mark.parametrize("number_type", [int, np.int64])
def test_integer_rpc_values_reach_pyadi_as_python_int(name, value, number_type):
    obj = schema()
    rpc(obj, f"set_{name}", number_type(value))
    assert getattr(obj.device, name) == value
    assert type(getattr(obj.device, name)) is int


@pytest.mark.parametrize("name,value", [
    ("tx_lo", 915_000_000), ("rx_lo", 915_000_000), ("sample_rate", 1_000_000),
    ("tx_rf_bandwidth", 1_000_000), ("rx_rf_bandwidth", 1_000_000),
])
@pytest.mark.parametrize("number_type", [float, np.float64])
@pytest.mark.parametrize("fraction", [0, .25])
@pytest.mark.parametrize("locked", [False, True])
def test_float_rpc_values_keep_pyadi_integer_setter_rejection(name, value, number_type, fraction, locked):
    obj = schema(BAND if locked else {})
    with pytest.raises(Exception, match="^Value must be an int$") as exc:
        rpc(obj, f"set_{name}", number_type(value + fraction))
    assert type(exc.value) is Exception


@pytest.mark.parametrize("park_lo", [915_000_000.4, 915_000_000.6])
@pytest.mark.parametrize("kind", ["reset", "prepare"])
def test_park_and_settle_round_guard_lo_writes_to_python_int(park_lo, kind):
    obj = schema({**BAND, "park_tx_lo": park_lo})
    obj.device.tx_lo = 2_450_000_000
    obj.lockout_after_hook(kind)
    assert obj.device.tx_lo == int(round(park_lo))
    assert type(obj.device.tx_lo) is int
    assert not obj._broken


@pytest.mark.parametrize("side", ["rx", "tx"])
def test_occupied_band_uses_larger_rate(side):
    obj = schema()
    obj.device.sample_rate = 28_000_000 if side == "rx" else 1_000_000
    obj.device.tx_rate = 28e6 if side == "tx" else 1e6
    refused(obj, "call_tx", BAND_ERROR, [1j])
    assert not obj._tx_flag and "tx" not in obj.device.events
    obj.device.sample_rate = obj.device.tx_rate = 26_000_000
    rpc(obj, "call_tx", [1j])
    assert obj._tx_flag
    refused(obj, "set_tx_lo", (
        "TX refused: the signal would occupy 901–927 MHz (tx_lo ± sample_rate/2), "
        "outside this device's TX range 902–928 MHz."), 914e6)


@pytest.mark.parametrize("rate", [float("nan"), float("inf"), 0, -1])
def test_bad_live_rate_never_starts_tx(rate):
    obj = schema()
    obj.device.tx_rate = rate
    refused(obj, "call_tx", "TX refused: live LO and sample rates must be finite numbers.", [1])


def test_gain_cap_and_larger_mimo_gain():
    obj = schema({**BAND, "tx_max_gain_db": -10}, cls=PlutoMIMOSchema)
    for channel in (0, 1):
        name = f"set_tx_hardwaregain_chan{channel}"
        rpc(obj, name, -10)
        refused(obj, name, GAIN_ERROR, -9)
        refused(obj, name, "TX gain -90 dB is above this device's limit of -10 dB.", -90)
    obj.device.tx_hardwaregain_chan1 = -9
    refused(obj, "call_tx", GAIN_ERROR, [1])
    obj.device.tx_hardwaregain_chan1 = -10
    rpc(obj, "call_tx", [1])


@pytest.mark.parametrize("name,args", [
    ("call_tx", {"value": [1]}),
    ("call_dds_single_tone", {"frequency": 1000, "scale": .1}),
    ("call_dds_dual_tone", {"frequency1": 1000, "scale1": .1, "frequency2": 2000, "scale2": .2}),
    ("set_dds_enabled", {"value": [1]}), ("set_dds_scales", {"value": [.1]}),
    ("set_loopback", {"value": 2}),
])
def test_receive_only_refuses_all_start_operations(name, args):
    obj = schema({**BAND, "tx_enabled": False, "allow_rf_loopback": True})
    assert obj.device.tx_lo == 915e6
    assert obj.device.tx_hardwaregain_chan0 == obj.device.tx_hardwaregain_chan1 == -89.75
    refused(obj, name, DISABLED, **args)
    rpc(obj, "set_dds_scales", [0])
    rpc(obj, "set_dds_enabled", [0])
    rpc(obj, "set_loopback", 1)


def test_loopback_liveness_survives_both_stops_and_settle_clears_it():
    refused(schema(), "set_loopback", LOOPBACK_ERROR, 2)
    obj = schema({**BAND, "allow_rf_loopback": True})
    rpc(obj, "set_loopback", 2)
    for stop in ("call_tx_destroy_buffer", "call_disable_dds"):
        obj.device.events.clear()
        rpc(obj, stop)
        first = "tx_destroy_buffer" if stop == "call_tx_destroy_buffer" else "disable_dds"
        assert obj.device.events == [first, "tx_destroy_buffer", "disable_dds"]
        assert not obj._tx_flag and obj._rf_loopback
        refused(obj, "set_sample_rate", "Sample rate cannot change while this device is transmitting; call tx_destroy_buffer() first.", 1e6)
    obj.lockout_settle()
    assert obj.device.loopback == 0
    assert not obj._tx_flag and not obj._rf_loopback
    rpc(obj, "set_sample_rate", 2_000_000)


@pytest.mark.parametrize("value", [0, 1])
def test_loopback_off_or_digital_clears_rf_flag(value):
    obj = schema({**BAND, "allow_rf_loopback": True})
    rpc(obj, "set_loopback", 2)
    rpc(obj, "set_loopback", value)
    assert not obj._rf_loopback
    rpc(obj, "call_disable_dds")
    rpc(obj, "set_sample_rate", 1_000_000)


@pytest.mark.parametrize("name,label", [("set_sample_rate", "Sample rate"), ("set_filter", "FIR filter"),
                                        ("set_tx_int8_filter_en", "TX interpolation filter")])
def test_live_rate_and_filter_changes_refused(name, label):
    obj = schema()
    rpc(obj, "call_tx", [1])
    value = True if name == "set_tx_int8_filter_en" else "path" if name == "set_filter" else 2e6
    refused(obj, name, f"{label} cannot change while this device is transmitting; call tx_destroy_buffer() first.", value)


def test_fir_blocked_with_any_lockouts_and_unchanged_without_them():
    for config in (BAND, {"rx_min_freq": 70e6, "rx_max_freq": 6e9}, {"admin_group_override": False}):
        refused(schema(config), "set_filter", "Loading FIR filter files is disabled on this device.", "arbitrary/path")
    obj = schema({})
    rpc(obj, "set_filter", "old behavior")
    assert obj.device.filter == "old behavior"
    rpc(obj, "set_loopback", "2")
    assert obj.device.loopback == "2"
    assert obj.device.events == []


@pytest.mark.parametrize("name,args", [
    ("call_dds_single_tone", {"frequency": 1000, "scale": .1}),
    ("call_dds_dual_tone", {"frequency1": 1000, "scale1": .1, "frequency2": 2000, "scale2": .2}),
    ("set_dds_enabled", {"value": [1]}), ("set_dds_scales", {"value": [.1]}),
])
def test_dds_switch(name, args):
    refused(schema({**BAND, "allow_dds": False}), name, DDS_ERROR, **args)
    obj = schema({**BAND, "allow_dds": True})
    rpc(obj, name, **args)
    assert obj._tx_flag


@pytest.mark.parametrize("name,args", [
    ("set_dds_scales", {"value": [.6]}),
    ("call_dds_single_tone", {"frequency": 1000, "scale": .6}),
    ("call_dds_dual_tone", {"frequency1": 1000, "scale1": .6, "frequency2": 2000, "scale2": .5}),
    ("call_dds_dual_tone", {"frequency1": 1000, "scale1": .5, "frequency2": 2000, "scale2": .6}),
])
def test_dds_scale_cap(name, args):
    obj = schema({**BAND, "tx_max_dds_scale": .5})
    refused(obj, name, "DDS scale 0.6 is above this device's limit of 0.5.", **args)
    good = {key: [.5] if isinstance(value, list) else .5 if key.startswith("scale") else value for key, value in args.items()}
    rpc(obj, name, **good)


@pytest.mark.parametrize("name,args", [
    ("set_dds_enabled", {"value": [True]}),
    ("call_dds_single_tone", {"frequency": 1000, "scale": .1}),
    ("call_dds_dual_tone", {"frequency1": 1000, "scale1": .1, "frequency2": 2000, "scale2": .1}),
])
@pytest.mark.parametrize("scale", [.6, -.1, float("nan"), float("inf"), "0.6", "-0.1", "nan", "inf"])
def test_dds_start_checks_live_scales(name, args, scale):
    obj = schema({"tx_max_dds_scale": .5})
    obj.device.dds_scales[-1] = scale
    refused(obj, name, f"DDS scale {float(scale):g} is above this device's limit of 0.5.", **args)
    assert not obj._tx_flag
    rpc(obj, "set_dds_enabled", [False])
    obj.device.dds_scales[-1] = "0.5"
    rpc(obj, name, **args)
    assert obj._tx_flag


@pytest.mark.parametrize("value", [[False, np.bool_(False)], [True, np.bool_(True)],
                                 np.array([True, False], dtype=np.bool_)])
def test_dds_enabled_accepts_and_normalizes_boolean_elements(value):
    obj = schema()
    rpc(obj, "set_dds_enabled", value)
    assert obj.device.dds_enabled == [int(v) for v in value]
    assert all(type(v) is int for v in obj.device.dds_enabled)
    assert obj._tx_flag == any(value)


def test_boolean_dds_enabled_still_enforces_policy_and_allows_turning_off():
    obj = schema({"allow_dds": False})
    rpc(obj, "set_dds_enabled", [False, np.bool_(False)])
    refused(obj, "set_dds_enabled", DDS_ERROR, [np.bool_(True)])
    refused(schema({"tx_enabled": False}), "set_dds_enabled", DISABLED, [True])
    refused(schema({"tx_allowed_channels": [0]}), "set_dds_enabled",
            "TX channel 1 is not allowed on this device (allowed: [0]).", [False] * 4 + [True])
    refused(schema(), "set_dds_scales", "DDS value must be a finite number.", [True])


@pytest.mark.parametrize("kind", ["reset", "prepare"])
def test_hygiene_clears_dds_scales_and_cyclic_leftovers(kind):
    obj = schema()
    assert obj.device.dds_scales == [0] * 8
    assert obj.device.tx_cyclic_buffer is False
    obj.device.dds_scales = [.8] * 4
    obj.device.tx_cyclic_buffer = True
    obj.lockout_after_hook(kind)
    assert obj.device.dds_scales == [0] * 4
    assert obj.device.tx_cyclic_buffer is False


def test_hygiene_skips_absent_optional_dds_scales_and_cyclic_attributes():
    radio = FakeRadio()
    del radio.dds_scales
    del radio.tx_cyclic_buffer
    obj = schema(radio=radio)
    obj.lockout_after_hook("reset")
    assert not hasattr(radio, "dds_scales") and not hasattr(radio, "tx_cyclic_buffer")
    assert not obj._broken


def test_cyclic_buffer():
    obj = schema({"allow_cyclic_tx": False})
    refused(obj, "set_tx_cyclic_buffer", "Cyclic (continuous) TX is disabled on this device; send finite buffers with tx().", True)
    rpc(obj, "set_tx_cyclic_buffer", False)
    rpc(schema({"allow_cyclic_tx": True}), "set_tx_cyclic_buffer", True)


@pytest.mark.parametrize("samples", [[1, 2, 3], [[1, 2, 3], [4, 5, 6]], np.array([1j, 2j, 3j])])
def test_buffer_sample_cap_is_per_channel(samples):
    obj = schema({"tx_max_buffer_samples": 2})
    refused(obj, "call_tx", "TX buffer of 3 samples is larger than this device's limit of 2.", samples)
    rpc(obj, "call_tx", [1j, 2j])
    assert isinstance(obj.device.samples, np.ndarray)


@pytest.mark.parametrize("samples", [[float("nan")], [complex(1, float("inf"))], ["1"], [True], [[1], [2, 3]], 3, [[[1]]]])
def test_nonfinite_or_nonnumeric_samples(samples):
    refused(schema(), "call_tx", "TX samples must be finite numbers.", samples)


def test_rate_cap():
    obj = schema({"max_sample_rate": "2e6"})
    rpc(obj, "set_sample_rate", 2_000_000)
    assert type(obj.device.sample_rate) is int
    refused(obj, "set_sample_rate", "Sample rate 3 MS/s is above this device's limit of 2 MS/s.", 3e6)


@pytest.mark.parametrize("side", ["rx", "tx"])
@pytest.mark.parametrize("with_band", [False, True])
@pytest.mark.parametrize("name,args", [
    ("call_tx", {"value": [1]}),
    ("call_dds_single_tone", {"frequency": 1000, "scale": .1}),
    ("call_dds_dual_tone", {"frequency1": 1000, "scale1": .1, "frequency2": 2000, "scale2": .1}),
    ("set_dds_enabled", {"value": [True]}), ("set_dds_scales", {"value": [.1]}),
    ("set_loopback", {"value": 2}),
])
def test_tx_start_checks_live_sample_rate_cap(side, with_band, name, args):
    obj = schema({**(BAND if with_band else {}), "max_sample_rate": 2_000_000, "allow_rf_loopback": True})
    setattr(obj.device, "sample_rate" if side == "rx" else "tx_rate", 3_000_000)
    refused(obj, name, "Sample rate 3 MS/s is above this device's limit of 2 MS/s.", **args)
    assert not obj._tx_flag and not obj._rf_loopback
    obj.device.sample_rate = obj.device.tx_rate = 2_000_000
    rpc(obj, name, **args)
    assert obj._tx_flag


@pytest.mark.parametrize("name", ["set_dds_enabled", "set_dds_scales", "set_dds_frequencies", "set_dds_phases"])
def test_every_dds_vector_index_is_channel_checked(name):
    obj = schema({"tx_allowed_channels": [0]}, cls=PlutoMIMOSchema)
    assert obj.device.tx_enabled_channels == [0]
    rpc(obj, name, [0, 0, 0, 0])
    refused(obj, name, "TX channel 1 is not allowed on this device (allowed: [0]).", [0] * 5)
    obj = schema({"tx_allowed_channels": [1]}, cls=PlutoMIMOSchema)
    assert obj.device.tx_enabled_channels == [1]
    refused(obj, name, "TX channel 0 is not allowed on this device (allowed: [1]).", [0])


def test_channel_setters_dds_calls_and_live_channel_readback():
    obj = schema({"tx_allowed_channels": [0]}, cls=PlutoMIMOSchema)
    rpc(obj, "set_tx_enabled_channels", np.array([0], dtype=np.int64))
    assert type(obj.device.tx_enabled_channels[0]) is int
    refused(obj, "set_tx_enabled_channels", "TX channel 1 is not allowed on this device (allowed: [0]).", [1])
    for name, args in [("call_dds_single_tone", {"frequency": 1000, "scale": .1}),
                       ("call_dds_dual_tone", {"frequency1": 1000, "scale1": .1, "frequency2": 2000, "scale2": .2})]:
        rpc(obj, name, channel=0, **args)
        refused(obj, name, "TX channel 1 is not allowed on this device (allowed: [0]).", channel=1, **args)
    obj.device.tx_enabled_channels = [1]
    refused(obj, "call_tx", "TX channel 1 is not allowed on this device (allowed: [0]).", [1])


def test_rx_lo_range():
    obj = schema({"rx_min_freq": 902e6, "rx_max_freq": 928e6})
    for value in (902_000_000, 928_000_000):
        rpc(obj, "set_rx_lo", value)
    refused(obj, "set_rx_lo", "RX LO 929 MHz is outside this device's RX range 902–928 MHz.", 929e6)


@pytest.mark.parametrize("value", ["2", 2.0, True, np.bool_(True)])
def test_loopback_strict_integer(value):
    refused(schema(), "set_loopback", "Value must be an integer.", value)


@pytest.mark.parametrize("name", ["set_tx_lo", "set_rx_lo", "set_sample_rate", "set_tx_hardwaregain_chan0", "set_rx_rf_bandwidth"])
@pytest.mark.parametrize("value", ["915000000", True, float("nan"), float("inf"), 1j])
def test_numeric_setter_strict_types(name, value):
    refused(schema(), name, "Value must be a finite number.", value)


@pytest.mark.parametrize("name,value,message", [
    ("set_dds_scales", [".1"], "DDS value must be a finite number."),
    ("set_dds_frequencies", [float("nan")], "DDS value must be a finite number."),
    ("set_dds_phases", [[1]], "DDS value must be a finite number."),
    ("set_dds_enabled", "1", "Value must be a list or array."),
    ("set_dds_scales", np.array([[1]]), "Value must be a one-dimensional list or array."),
    ("set_tx_enabled_channels", [0.0], "Channel must be an integer."),
    ("set_rx_enabled_channels", [True], "Channel must be an integer."),
    ("set_tx_cyclic_buffer", 1, "Value must be a bool."),
])
def test_other_strict_types(name, value, message):
    refused(schema(), name, message, value)


def test_normalized_values_reach_exposed_function():
    obj = schema()
    rpc(obj, "set_loopback", np.int64(1))
    assert type(obj.device.loopback) is int
    rpc(obj, "set_tx_lo", np.int64(915_000_000))
    assert type(obj.device.tx_lo) is int
    rpc(obj, "set_dds_frequencies", np.array([1000.0, 2000.0]))
    assert obj.device.dds_frequencies == [1000, 2000]
    assert all(type(v) is float for v in obj.device.dds_frequencies)
    rpc(obj, "call_dds_single_tone", frequency=np.float64(1000), scale=np.float32(.5), channel=np.int64(0))
    assert tuple(type(v) for v in obj.device.tone) == (float, float, int)


@pytest.mark.parametrize("cls", [PlutoSchema, PlutoMIMOSchema])
def test_every_exposed_method_is_classified(cls):
    for name, ex in cls()._exposed.items():
        assert ex.kind == "get" or name in _LOCKOUT_METHODS, name


def test_unknown_methods_and_custom_allowlist():
    obj = schema(cls=CustomMIMO)
    refused(obj, "call_camera_capture", "Call_camera_capture is not allowed on this device while lockouts are set.")
    refused(obj, "set_future_tx_mode", "Set_future_tx_mode is not allowed on this device while lockouts are set.", True)
    obj = schema({"lockout_allow_methods": ["call_camera_capture"]}, cls=CustomMIMO)
    assert rpc(obj, "call_camera_capture") == "frame"
    assert rpc(obj, "get_mode") == "mimo"


def test_dispatch_override_detection_contract():
    class BadMIMO(PlutoMIMOSchema):
        def dispatch(self, method_name, args):
            return super().dispatch(method_name, args)
    assert BadMIMO.dispatch.__qualname__ != "PlutoSchema.dispatch"
    assert CustomMIMO.dispatch.__qualname__ == "PlutoSchema.dispatch"


def test_schema_hashes_and_no_dynamic_v2():
    from remoterf_device_core import manifest
    pinned = {row["device_type"]: row for row in manifest.load()}
    for cls in (PlutoSchema, PlutoMIMOSchema):
        obj = cls()
        assert obj.get_idl_hash() == pinned[cls.device_type]["v1"]
        assert not hasattr(obj, "get_idl_v2")


def test_configure_does_not_boot_off_poe_and_getter_boot_settles():
    proxy = FakePoe()
    obj = schema({**BAND, "tx_max_gain_db": -10}, radio=proxy)
    assert proxy.ups == proxy.downs == 0
    obj.lockout_settle()
    obj.lockout_after_hook("reset")
    obj.lockout_after_hook("prepare")
    assert proxy.ups == 0
    # get_repr is safe to execute without consulting proxy attributes.
    assert rpc(obj, "get_repr")
    radio = vars(proxy)["_pluto"]
    assert proxy.ups == 1 and radio.tx_lo == 915e6
    assert radio.loopback == 0
    assert radio.tx_hardwaregain_chan0 == radio.tx_hardwaregain_chan1 == -89.75
    assert radio.events == ["disable_dds", "tx_destroy_buffer"]


@pytest.mark.parametrize("name", ["call_power_down", "call_ip", "call_tx_destroy_buffer", "call_disable_dds"])
def test_off_poe_exempt_operations_do_not_boot(name):
    proxy = FakePoe()
    obj = schema(radio=proxy)
    rpc(obj, name)
    assert proxy.ups == 0


def test_power_cycle_changed_handle_settles_even_with_bypass():
    proxy = FakePoe(FakeRadio())
    obj = schema({**BAND, "tx_max_gain_db": -10, "admin_group_override": True}, radio=proxy)
    obj.lockout_bypass = True
    old = vars(proxy)["_pluto"]
    rpc(obj, "call_power_cycle")
    new = vars(proxy)["_pluto"]
    assert new is not old
    assert new.loopback == 0 and new.tx_lo == 915e6
    assert new.tx_hardwaregain_chan0 == new.tx_hardwaregain_chan1 == -89.75
    assert new.events == ["disable_dds", "tx_destroy_buffer"]


@pytest.mark.parametrize("name", ["call_reconnect", "call_reconnect_then_fail"])
def test_custom_reconnect_settles_also_on_exception(name):
    proxy = FakePoe(FakeRadio())
    obj = schema({**BAND, "lockout_allow_methods": [name]}, cls=CustomMIMO, radio=proxy)
    if name.endswith("fail"):
        with pytest.raises(RuntimeError, match="reconnect failure"):
            rpc(obj, name)
    else:
        rpc(obj, name)
    radio = vars(proxy)["_pluto"]
    assert radio.tx_lo == 915e6 and radio.loopback == 0
    assert radio.events == ["disable_dds", "tx_destroy_buffer"]


def test_bypass_needs_both_config_and_holder_bit_and_logs(caplog):
    obj = schema({**BAND, "admin_group_override": True, "tx_max_gain_db": -10})
    refused(obj, "set_tx_lo", LO_ERROR, 901e6)
    obj.lockout_bypass = True
    with caplog.at_level(logging.WARNING):
        rpc(obj, "set_tx_lo", 901_000_000)
    assert obj.device.tx_lo == 901e6
    assert f"lockout bypassed: {LO_ERROR}" in caplog.text
    obj.lockout_settle()
    assert obj.device.tx_lo == 915e6
    assert not obj._tx_flag
    other = schema()
    other.lockout_bypass = True
    refused(other, "set_tx_lo", LO_ERROR, 901e6)


def test_bypass_logs_every_skipped_check_and_still_parks(caplog):
    obj = schema({**BAND, "admin_group_override": True, "tx_enabled": False,
                  "allow_dds": False, "tx_max_dds_scale": .1, "tx_allowed_channels": [0]})
    obj.lockout_bypass = True
    with caplog.at_level(logging.WARNING):
        rpc(obj, "call_dds_single_tone", frequency=1000, scale=.2, channel=1)
    assert "DDS scale 0.2" in caplog.text and DDS_ERROR in caplog.text and DISABLED in caplog.text
    assert "TX channel 1" in caplog.text
    assert obj._tx_flag
    obj.lockout_after_hook("reset")
    assert obj.device.loopback == 0 and not obj._tx_flag
    assert obj.device.tx_lo == 915e6
    assert obj.device.tx_hardwaregain_chan0 == obj.device.tx_hardwaregain_chan1 == -89.75


def test_hygiene_settle_vs_park_and_prepare_after_hook():
    obj = schema({**BAND, "park_tx_lo": 916e6, "tx_max_gain_db": -10})
    obj.device.tx_lo = 914_000_000
    obj.device.tx_hardwaregain_chan0 = -9
    obj._tx_flag = obj._rf_loopback = True
    obj.lockout_after_hook("prepare")
    assert obj.device.tx_lo == 914e6
    assert obj.device.tx_hardwaregain_chan0 == -89.75
    assert obj.device.tx_hardwaregain_chan1 == -20
    assert not obj._tx_flag and not obj._rf_loopback
    obj.lockout_after_hook("reset")
    assert obj.device.tx_lo == 916e6
    assert obj.device.tx_hardwaregain_chan0 == obj.device.tx_hardwaregain_chan1 == -89.75


def test_base_reset_without_lockouts_hygiene():
    obj = schema({})
    obj.reset_for_next_holder()
    assert obj.device.events == ["disable_dds", "tx_destroy_buffer", "rx_destroy_buffer"]
    assert obj.device.loopback == 0
    assert obj.device.tx_lo == 915e6
    assert obj.device.tx_hardwaregain_chan0 == -20
    assert obj.device.dds_scales == [0] * 8
    assert obj.device.tx_cyclic_buffer is False


def test_base_reset_without_lockouts_clears_cyclic_even_if_tx_teardown_fails():
    obj = schema({})
    obj.device.fail_stop = True
    with pytest.raises(RuntimeError, match="buffer stop failed"):
        obj.reset_for_next_holder()
    assert obj.device.events == ["disable_dds", "tx_destroy_buffer", "rx_destroy_buffer"]
    assert obj.device.tx_cyclic_buffer is False


def test_non_poe_reset_zeros_dds_scales_on_failure_and_restores_filters_output_and_channels():
    radio = FakeRadio()
    radio.rx_enabled_channels = [0]
    radio.tx_enabled_channels = [0]
    radio.rx_dec8_filter_en = False
    radio.tx_int8_filter_en = False
    radio.rx_output_type = "raw"
    radio.rx_annotated = False
    obj = schema({}, radio=radio)

    radio.fail_dds = True
    radio.dds_scales = [0.8] * 8
    radio.rx_dec8_filter_en = True
    radio.tx_int8_filter_en = True
    radio.rx_output_type = "SI"
    radio.rx_annotated = True
    radio.rx_enabled_channels = []
    radio.tx_enabled_channels = []

    with pytest.raises(RuntimeError, match="DDS stop failed"):
        obj.reset_for_next_holder()
    assert radio.dds_scales == [0] * 8
    assert radio.rx_dec8_filter_en is False
    assert radio.tx_int8_filter_en is False
    assert radio.rx_output_type == "raw"
    assert radio.rx_annotated is False
    assert radio.rx_enabled_channels == [0]
    assert radio.tx_enabled_channels == [0]

    rate_writes = []

    class FakeMimoRadio(FakeRadio):
        def __init__(self):
            super().__init__()
            self._rxadc = "rxadc"
            self._txdac = "txdac"
            self.rx_output_type = "raw"
            self.rx_annotated = False
            self.fpga_rates = {False: 30_720_000, True: 30_720_000}

        def __setattr__(self, name, value):
            if name == "sample_rate" and vars(self).get("_track_rate"):
                rate_writes.append(value)
            super().__setattr__(name, value)

        def _get_iio_attr_str(self, channel, attr, output, dev=None):
            return "[30720000 3840000]"

        def _get_iio_attr(self, channel, attr, output, dev=None):
            if dev in ("rxadc", "txdac"):
                return self.fpga_rates[output]
            return super()._get_iio_attr(channel, attr, output)

        def _set_iio_attr(self, channel, attr, output, value, dev=None):
            self.fpga_rates[output] = value

    mimo_radio = FakeMimoRadio()
    mimo = schema({}, cls=PlutoMIMOSchema, radio=mimo_radio)
    mimo.set_rx_dec8_filter_en(True)
    mimo.set_tx_int8_filter_en(True)
    mimo.set_rx_output_type("SI")
    mimo.set_rx_annotated(True)
    mimo.set_rx_enabled_channels([0])
    mimo.set_tx_enabled_channels([0])
    mimo.set_filter("custom.ftr")
    mimo_radio._track_rate = True

    mimo.reset_for_next_holder()
    assert mimo.get_rx_dec8_filter_en() is False
    assert mimo.get_tx_int8_filter_en() is False
    assert mimo_radio.rx_output_type == "raw"
    assert mimo_radio.rx_annotated is False
    assert mimo_radio.rx_enabled_channels == [0, 1]
    assert mimo_radio.tx_enabled_channels == [0, 1]
    assert rate_writes == [1_000_000]
    assert mimo._custom_filter_loaded is False

    # A sample_rate write after set_filter already reloads pyadi's FIR, so
    # reset_for_next_holder must not perform a redundant sample_rate write.
    rate_writes.clear()
    mimo.set_filter("custom2.ftr")
    mimo.set_sample_rate(2_000_000)
    assert rate_writes == [2_000_000]
    assert mimo._custom_filter_loaded is False
    mimo.reset_for_next_holder()
    assert rate_writes == [2_000_000]


def test_poe_power_off_failure_reports_fault_until_next_boot(monkeypatch):
    from remoterf_device_core.drivers.adalm_pluto import poe

    def refuse(**kw):
        raise OSError("switch timeout")

    monkeypatch.setattr(poe, "set_poe", refuse)
    monkeypatch.setattr(poe.time, "sleep", lambda s: None)
    pluto = PoePluto(ip="192.0.2.1", port="ether5")
    vars(pluto)["_pluto"] = FakeRadio()
    with pytest.raises(OSError):
        pluto.power_down()
    assert pluto.power_state == "fault"
    assert "ether5" in pluto.fault and "switch timeout" in pluto.fault
    assert vars(pluto)["_pluto"] is None

    monkeypatch.setattr(poe, "set_poe", lambda **kw: None)
    monkeypatch.setattr(poe, "_iiod_up", lambda ip: True)
    board = types.SimpleNamespace(_ctx=types.SimpleNamespace(attrs={}))
    monkeypatch.setattr(sys.modules["adi"], "Pluto", lambda uri: board, raising=False)
    pluto.power_up()
    assert pluto.power_state == "on" and pluto.fault is None


def test_poe_power_up_after_failed_power_down_power_cycles_first(monkeypatch):
    from remoterf_device_core.drivers.adalm_pluto import poe

    events = []

    def fail_off(**kw):
        events.append(("poe", kw["on"]))
        raise OSError("switch timeout")

    monkeypatch.setattr(poe, "set_poe", fail_off)
    monkeypatch.setattr(poe.time, "sleep", lambda s: events.append(("sleep", s)))
    pluto = PoePluto(ip="192.0.2.1", port="ether5")
    vars(pluto)["_pluto"] = FakeRadio()
    with pytest.raises(OSError):
        pluto.power_down()
    assert vars(pluto)["_power_off_failed"] is True

    events.clear()
    monkeypatch.setattr(poe, "set_poe", lambda **kw: events.append(("poe", kw["on"])))
    monkeypatch.setattr(poe, "_iiod_up", lambda ip: True)
    board = types.SimpleNamespace(_ctx=types.SimpleNamespace(attrs={}))
    monkeypatch.setattr(sys.modules["adi"], "Pluto", lambda uri: board, raising=False)

    pluto.power_up()
    assert events == [("poe", False), ("sleep", poe.OFF_S), ("poe", True)]
    assert vars(pluto)["_power_off_failed"] is False
    assert pluto.power_state == "on" and pluto.fault is None


def test_poe_later_successful_power_down_clears_power_off_fault_but_keeps_boot_fault(monkeypatch):
    from remoterf_device_core.drivers.adalm_pluto import poe

    monkeypatch.setattr(poe.time, "sleep", lambda s: None)
    pluto = PoePluto(ip="192.0.2.1", port="ether5")
    vars(pluto)["_pluto"] = FakeRadio()

    def fail_off(**kw):
        raise OSError("switch timeout")

    monkeypatch.setattr(poe, "set_poe", fail_off)
    with pytest.raises(OSError):
        pluto.power_down()
    assert pluto.power_state == "fault"

    monkeypatch.setattr(poe, "set_poe", lambda **kw: None)
    pluto.power_down()
    assert pluto.fault is None
    assert vars(pluto)["_power_off_failed"] is False
    assert pluto.power_state == "standby"

    monkeypatch.setattr(poe, "_iiod_up", lambda ip: False)
    monkeypatch.setattr(poe, "port_running", lambda **kw: False)
    times = iter([0.0, poe.LINK_CHECK_S + 1.0])
    monkeypatch.setattr(poe.time, "monotonic", lambda: next(times))
    with pytest.raises(RuntimeError, match="no Ethernet link"):
        pluto.power_up()
    assert pluto.power_state == "fault" and "no Ethernet link" in pluto.fault
    assert vars(pluto)["_power_off_failed"] is False

    pluto.power_down()
    assert pluto.power_state == "fault" and "no Ethernet link" in pluto.fault


def test_base_reset_parks_before_poe_power_down():
    radio = FakeRadio()
    proxy = FakePoe(radio)
    obj = schema({**BAND, "park_tx_lo": 916e6}, radio=proxy)
    # The base hook must use the raw handle for hygiene, avoiding __getattr__.
    obj.reset_for_next_holder()
    assert radio.tx_lo == 916e6 and radio.loopback == 0
    assert radio.tx_hardwaregain_chan0 == radio.tx_hardwaregain_chan1 == -89.75
    assert proxy.downs == 1 and vars(proxy)["_pluto"] is None


def test_non_lockout_poe_reset_destroys_tx_buffer_and_drops_radio_before_power_down():
    import weakref

    radio = FakeRadio()
    radio.tx_cyclic_buffer = True
    radio_ref = weakref.ref(radio)
    alive_at_power_down = []

    class ObservingPoe(FakePoe):
        def power_down(self):
            vars(self)["_pluto"] = None
            alive_at_power_down.append(radio_ref() is not None)
            if vars(self)["fail_down"]:
                raise RuntimeError("power-off failed")

    proxy = ObservingPoe(radio)
    obj = schema({}, radio=proxy)
    vars(proxy)["fail_down"] = True
    del radio

    with pytest.raises(RuntimeError, match="power-off failed"):
        obj.reset_for_next_holder()
    assert alive_at_power_down == [False]

    radio2 = FakeRadio()
    radio2.tx_cyclic_buffer = True
    proxy2 = FakePoe(radio2)
    obj2 = schema({}, radio=proxy2)
    vars(proxy2)["fail_down"] = True
    with pytest.raises(RuntimeError, match="power-off failed"):
        obj2.reset_for_next_holder()
    assert radio2.events == ["disable_dds", "tx_destroy_buffer"]
    assert radio2.tx_cyclic_buffer is False
    assert radio2.dds_scales == [0] * 8
    assert radio2.loopback == 0


def test_base_reset_off_poe_never_boots():
    for config in ({}, BAND):
        proxy = FakePoe()
        obj = schema(config, radio=proxy)
        obj.reset_for_next_holder()
        assert proxy.ups == 0 and proxy.downs == 1


def test_reset_failure_retries_power_off_once_and_marks_broken():
    proxy = FakePoe(FakeRadio())
    obj = schema(radio=proxy)
    vars(proxy)["fail_down"] = True
    with pytest.raises(RuntimeError, match="power-off failed"):
        obj.reset_for_next_holder()
    obj.lockout_after_hook("reset", failed=True)
    assert proxy.downs == 2 and obj._broken
    vars(proxy)["_pluto"].fail_dds = True
    refused(obj, "set_tx_lo", BROKEN, 915e6)
    assert obj._broken
    vars(proxy)["_pluto"].fail_dds = False
    obj.lockout_settle()
    assert not obj._broken


def test_successful_power_off_retry_and_non_switchable_reset_failure():
    proxy = FakePoe(FakeRadio())
    obj = schema(radio=proxy)
    obj.lockout_after_hook("reset", failed=True)
    assert proxy.downs == 1 and not obj._broken
    obj = schema()
    obj.lockout_after_hook("reset", failed=True)
    assert obj._broken
    obj.lockout_settle()
    assert not obj._broken


def test_failed_settle_cuts_power_and_next_call_recovers():
    radio = FakeRadio()
    radio.fail_dds = True
    proxy = FakePoe(radio)
    obj = PlutoSchema()
    obj.bind(proxy)
    with pytest.raises(RemoteRFPolicyError, match="locked until"):
        obj.configure_lockouts(parse_lockouts({**BAND, "admin_group_override": True}))
    assert proxy.downs == 1 and not obj._broken
    assert vars(proxy)["_pluto"] is None
    obj.lockout_bypass = True
    rpc(obj, "call_power_up")
    assert not obj._broken
    assert vars(proxy)["_pluto"].tx_lo == 915_000_000
    assert vars(proxy)["_pluto"].dds_scales == [0] * 8


@pytest.mark.parametrize("name,args", [
    ("call_power_up", {}), ("set_tx_lo", {"value": 915_000_000}),
    ("call_tx", {"value": [1]}), ("get_repr", {}),
])
def test_broken_off_board_powers_up_and_settles_before_dispatch(name, args):
    class DroppingPowerOff(FakePoe):
        def power_down(self):
            vars(self)["_pluto"] = None
            super().power_down()

        def __getattr__(self, name):
            if name == "tx":
                return vars(self)["_pluto"].tx
            return super().__getattr__(name)

    proxy = DroppingPowerOff(FakeRadio())
    obj = schema({**BAND, "admin_group_override": True}, radio=proxy)
    vars(proxy)["fail_down"] = True
    vars(proxy)["_pluto"].fail_dds = True
    with pytest.raises(RemoteRFPolicyError, match="locked until"):
        obj.lockout_settle()
    assert obj._broken and vars(proxy)["_pluto"] is None
    obj.lockout_bypass = True
    rpc(obj, name, **args)
    radio = vars(proxy)["_pluto"]
    assert not obj._broken
    assert radio.tx_lo == 915_000_000 and radio.loopback == 0
    assert radio.dds_scales == [0] * 8 and radio.tx_cyclic_buffer is False
    assert radio.events[:2] == ["disable_dds", "tx_destroy_buffer"]


def test_bypass_cannot_skip_failed_recovery_from_broken_off_board():
    class UnsafeBoot(FakePoe):
        def power_up(self):
            super().power_up()
            vars(self)["_pluto"].fail_dds = True

    proxy = UnsafeBoot()
    obj = schema({**BAND, "admin_group_override": True}, radio=proxy)
    vars(proxy)["fail_down"] = True
    obj._broken = True
    obj.lockout_bypass = True
    refused(obj, "set_tx_lo", BROKEN, 914_000_000)
    assert obj._broken
    assert vars(proxy)["_pluto"].tx_lo == 2_450_000_000


@pytest.mark.parametrize("name,args", [
    ("set_tx_lo", {"value": 914_000_000}), ("call_tx", {"value": [1]}),
    ("call_power_up", {}),
])
@pytest.mark.parametrize("bypass", [False, True])
def test_failed_broken_recovery_with_successful_power_off_only_boots_once(name, args, bypass):
    class UnsafeBoot(FakePoe):
        def power_up(self):
            super().power_up()
            vars(self)["_pluto"].fail_dds = True

    proxy = UnsafeBoot()
    obj = schema({**BAND, "admin_group_override": True}, radio=proxy)
    obj._broken = True
    obj.lockout_bypass = bypass
    refused(obj, name, BROKEN, **args)
    assert proxy.ups == proxy.downs == 1
    assert vars(proxy)["_pluto"] is None


def test_broken_power_down_can_cut_power_after_failed_recovery():
    class RetryPowerOff(FakePoe):
        def power_down(self):
            vars(self)["fail_down"] = vars(self)["downs"] == 0
            super().power_down()

    proxy = RetryPowerOff(FakeRadio())
    obj = schema(radio=proxy)
    obj._broken = True
    vars(proxy)["_pluto"].fail_dds = True
    rpc(obj, "call_power_down")
    assert proxy.ups == 0 and proxy.downs == 2 and vars(proxy)["_pluto"] is None


@pytest.mark.parametrize("name", ["call_power_down", "call_ip", "call_tx_destroy_buffer", "call_disable_dds"])
def test_broken_off_board_exempt_operations_never_boot(name):
    proxy = FakePoe()
    obj = schema(radio=proxy)
    obj._broken = True
    if name == "call_ip":
        refused(obj, name, BROKEN)
    else:
        rpc(obj, name)
    assert obj._broken and proxy.ups == 0


def test_bypass_logs_live_scale_and_sample_rate_caps(caplog):
    obj = schema({"admin_group_override": True, "tx_max_dds_scale": .5, "max_sample_rate": 2_000_000})
    obj.lockout_bypass = True
    obj.device.dds_scales[-1] = .6
    obj.device.tx_rate = 3_000_000
    with caplog.at_level(logging.WARNING):
        rpc(obj, "set_dds_enabled", [True])
    assert "DDS scale 0.6 is above this device's limit of 0.5." in caplog.text
    assert "Sample rate 3 MS/s is above this device's limit of 2 MS/s." in caplog.text
    assert obj._tx_flag
    obj.lockout_after_hook("reset")
    assert obj.device.dds_scales == [0] * 8 and not obj._tx_flag


def test_failed_tx_start_stays_live_and_stop_attempts_both_operations():
    obj = schema()
    obj.device.fail_tx = True
    with pytest.raises(RuntimeError, match="TX failed"):
        rpc(obj, "call_tx", [1])
    assert obj._tx_flag
    refused(obj, "set_sample_rate", "Sample rate cannot change while this device is transmitting; call tx_destroy_buffer() first.", 1e6)
    obj.device.fail_stop = True
    obj.device.events.clear()
    with pytest.raises(RuntimeError, match="buffer stop failed"):
        rpc(obj, "call_disable_dds")
    assert obj.device.events == ["disable_dds", "tx_destroy_buffer", "disable_dds"]
    assert obj._tx_flag


def test_device_lock_covers_check_and_write():
    obj = schema()
    events = []

    class Lock:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, *exc):
            events.append("exit")

    obj.device._lock = Lock()
    obj.device.tx = lambda value: events.append("write")
    rpc(obj, "call_tx", [1])
    assert events == ["enter", "write", "exit"]


def test_messages_pass_through_client_explain_unchanged():
    try:
        from remoterf.core.messages import explain
    except ImportError:
        # Load the standalone source so the exact client rule is exercised.
        path = Path(__file__).resolve().parents[3] / "client/src/remoterf/core/messages.py"
        spec = importlib.util.spec_from_file_location("pluto_test_messages", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        explain = module.explain
    messages = [LO_ERROR, BAND_ERROR, GAIN_ERROR, DISABLED, LOOPBACK_ERROR, DDS_ERROR, BROKEN,
                "Sample rate cannot change while this device is transmitting; call tx_destroy_buffer() first.",
                "FIR filter cannot change while this device is transmitting; call tx_destroy_buffer() first.",
                "TX interpolation filter cannot change while this device is transmitting; call tx_destroy_buffer() first.",
                "Loading FIR filter files is disabled on this device.",
                "Call_camera_capture is not allowed on this device while lockouts are set.",
                "DDS scale 0.6 is above this device's limit of 0.5.",
                "Cyclic (continuous) TX is disabled on this device; send finite buffers with tx().",
                "TX buffer of 3 samples is larger than this device's limit of 2.",
                "TX samples must be finite numbers.",
                "Sample rate 3 MS/s is above this device's limit of 2 MS/s.",
                "TX channel 1 is not allowed on this device (allowed: [0]).",
                "RX LO 929 MHz is outside this device's RX range 902–928 MHz.",
                "Value must be an integer.", "Value must be a finite number."]
    for message in messages:
        assert explain(message) == message


@pytest.mark.parametrize("gains,message", [
    ((-9, -1), "TX gain -1 dB is above this device's limit of -10 dB."),
    ((-1, -9), "TX gain -1 dB is above this device's limit of -10 dB."),
    ((-20, float("nan")), "TX gain nan dB is above this device's limit of -10 dB."),
    ((float("inf"), -20), "TX gain inf dB is above this device's limit of -10 dB."),
])
def test_tx_start_checks_larger_gain_without_hiding_nan(gains, message):
    obj = schema({**BAND, "tx_max_gain_db": -10})
    obj.device.tx_hardwaregain_chan0, obj.device.tx_hardwaregain_chan1 = gains
    refused(obj, "call_tx", message, [1])


def test_hygiene_attempts_all_stops_even_when_dds_fails():
    obj = schema()
    obj.device.fail_dds = True
    obj.device.loopback = 2
    obj.device.dds_scales = [.9] * 8
    obj.device.tx_cyclic_buffer = True
    obj.device.events.clear()
    with pytest.raises(RemoteRFPolicyError, match="locked until"):
        obj.lockout_settle()
    assert obj.device.events == ["disable_dds", "tx_destroy_buffer"]
    assert obj.device.loopback == 0 and obj._broken
    assert obj.device.dds_scales == [0] * 8 and obj.device.tx_cyclic_buffer is False
    # Stop calls remain available while broken, including an attempted DDS stop.
    with pytest.raises(RuntimeError, match="DDS stop failed"):
        rpc(obj, "call_tx_destroy_buffer")


def test_boot_that_raises_after_connecting_still_settles():
    class PartialBoot(FakePoe):
        def power_up(self):
            super().power_up()
            raise RuntimeError("boot failed after connection")

    proxy = PartialBoot()
    obj = schema(radio=proxy)
    with pytest.raises(RuntimeError, match="boot failed after connection"):
        rpc(obj, "get_repr")
    radio = vars(proxy)["_pluto"]
    assert radio.tx_lo == 915e6 and radio.loopback == 0
    assert radio.events == ["disable_dds", "tx_destroy_buffer"]


def test_failed_loopback_write_is_conservatively_live():
    class FailingLoopback(FakeRadio):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            if name == "loopback" and vars(self).get("fail_loopback"):
                raise RuntimeError("loopback failed after writing")

    obj = schema({**BAND, "allow_rf_loopback": True}, radio=FailingLoopback())
    obj.device.fail_loopback = True
    with pytest.raises(RuntimeError, match="loopback failed"):
        rpc(obj, "set_loopback", 2)
    rpc(obj, "call_tx_destroy_buffer")
    assert obj._rf_loopback
    refused(obj, "set_sample_rate", "Sample rate cannot change while this device is transmitting; call tx_destroy_buffer() first.", 1e6)


def test_bad_key_message_is_one_line():
    with pytest.raises(ValueError) as exc:
        parse_lockouts({"tx_freq\n_typo": 123})
    assert "\n" not in str(exc.value)


def test_configure_and_after_hooks_do_nothing_when_unlocked():
    obj = schema({})
    obj.lockout_after_hook("reset", failed=True)
    obj.lockout_after_hook("prepare")
    obj.lockout_settle()
    assert not obj._broken and not obj.device.events


def test_settle_on_single_channel_pluto_does_not_require_gain_one():
    radio = FakeRadio()
    del radio.tx_hardwaregain_chan1
    radio.tx_enabled_channels = [0]
    obj = schema({**BAND, "tx_max_gain_db": -10}, radio=radio)
    rpc(obj, "call_tx", [1])
    obj.lockout_after_hook("reset")
    assert radio.tx_hardwaregain_chan0 == -89.75


def test_seeded_schema_copy_carries_the_guard(tmp_path):
    from remoterf_device_core.drivers.adalm_pluto import pluto_schema
    from remoterf_device_core.idl import schema as idl

    # Loading a drivers-dir copy registers it last; restore the test registry.
    registered = dict(idl._DRIVER_REGISTRY)
    copied = tmp_path / "pluto_schema.py"
    copied.write_text(Path(pluto_schema.__file__).read_text())
    try:
        spec = importlib.util.spec_from_file_location("seeded_pluto_lockout_test", copied)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        obj = module.PlutoSchema()
        obj.bind(FakeRadio())
        obj.configure_lockouts(module.parse_lockouts(BAND))
        refused(obj, "set_tx_lo", LO_ERROR, 901e6)
        assert obj.get_idl_hash() == PlutoSchema().get_idl_hash()
    finally:
        idl._DRIVER_REGISTRY.clear()
        idl._DRIVER_REGISTRY.update(registered)


class TempRadio(FakeRadio):
    def _get_iio_attr(self, channel, attr, output, ctrl=None):
        self.events.append(("iio", channel, attr, output, ctrl))
        return 41500


@pytest.mark.parametrize("cls", [PlutoSchema, PlutoMIMOSchema])
def test_get_temperature_reads_die_sensor_in_celsius(cls):
    radio = TempRadio()
    radio._ctrl = "phy"
    obj = schema(cls=cls, radio=radio)
    assert obj.get_temperature() == 41.5
    assert rpc(obj, "get_temperature") == 41.5
    assert rpc(obj, "get_temp") == 41.5
    iio = [event for event in radio.events if event[0] == "iio"]
    assert iio[0][1:4] == ("temp0", "input", False)
    if cls is PlutoMIMOSchema:
        assert iio[0][4] == "phy"
