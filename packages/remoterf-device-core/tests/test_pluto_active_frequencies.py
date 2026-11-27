"""Active frequency recording and clearing on PlutoSchema and its subclasses."""

import sys
import time
import types
from pathlib import Path

import numpy as np
import pytest

sys.modules.setdefault("adi", types.ModuleType("adi"))
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from remoterf_device_core import manifest
from remoterf_device_core.drivers.adalm_pluto.pluto_mimo_schema import PlutoMIMOSchema
from remoterf_device_core.drivers.adalm_pluto.pluto_schema import (
    PlutoSchema,
    _UNKNOWN,
    _any_nonzero,
    _dds_pair_active,
)
from remoterf_device_core.drivers.adalm_pluto.poe import PoePluto
from remoterf_device_core.v2_errors import RemoteRFPolicyError

from test_pluto_lockouts import BAND, CustomMIMO, FakePoe, FakeRadio, rpc, schema


class _ForwardingPoe(FakePoe):
    def __getattr__(self, name):
        return PoePluto.__getattr__(self, name)


class _FirFakeRadio(FakeRadio):
    def __setattr__(self, name, value):
        if name in {"tx_lo", "rx_lo", "sample_rate", "tx_rf_bandwidth", "rx_rf_bandwidth"}:
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
                raise Exception("Value must be an int")
            object.__setattr__(self, name, int(value))
            return
        super().__setattr__(name, value)

    def _get_iio_attr(self, channel, name, output):
        if (channel, name, output) == ("out", "voltage_filter_fir_en", False):
            return 1
        return super()._get_iio_attr(channel, name, output)


class _PyadiFakeRadio(FakeRadio):
    """FakeRadio modeling pyadi-iio's adi.dds string readback, prefix updates, and TX defaults."""

    def __init__(self, num_dds=4):
        self._dds_enabled = ["0"] * num_dds
        self._dds_scales = ["0.000000"] * num_dds
        super().__init__()
        self._dds_enabled = ["0"] * num_dds
        self._dds_scales = ["0.000000"] * num_dds
        self.tx_cyclic_buffer = False
        self.loopback = 0
        self.tx_enabled_channels = [0] if num_dds == 4 else [0, 1]
        self.rx_enabled_channels = [0] if num_dds == 4 else [0, 1]

    @property
    def dds_enabled(self):
        return list(self._dds_enabled)

    @dds_enabled.setter
    def dds_enabled(self, value):
        for i, v in enumerate(value):
            if i >= len(self._dds_enabled):
                break
            self._dds_enabled[i] = str(int(v))

    @property
    def dds_scales(self):
        return list(self._dds_scales)

    @dds_scales.setter
    def dds_scales(self, value):
        for i, v in enumerate(value):
            if i >= len(self._dds_scales):
                break
            self._dds_scales[i] = f"{float(v):.6f}"

    def disable_dds(self):
        self.events.append("disable_dds")
        if self.fail_dds:
            raise RuntimeError("DDS stop failed")
        self.dds_enabled = np.zeros(len(self._dds_enabled), dtype=bool)

    def tx(self, samples=None):
        if not self.tx_enabled_channels:
            if samples is not None and len(samples) > 0:
                raise Exception(
                    "When tx_enabled_channels is None or empty,"
                    " the input to tx() must be None or empty or not provided"
                )
            self.events.append("tx")
            self.samples = samples
            if self._dds_enabled:
                self._dds_enabled[0] = "0"
            return
        super().tx(samples)


class _CloseTrackingRadio(_PyadiFakeRadio):
    def __init__(self):
        super().__init__()
        self.closed = 0

    def close(self):
        self.closed += 1


def _plain_schema(cls=PlutoSchema, radio=None):
    obj = cls()
    if radio is None:
        num_dds = 8 if issubclass(cls, PlutoMIMOSchema) else 4
        radio = _PyadiFakeRadio(num_dds=num_dds)
    obj.bind(radio)
    return obj


def test_initial_state_is_unset_and_does_not_touch_off_poe():
    poe = FakePoe(radio=None)
    obj = _plain_schema(radio=poe)
    assert obj.active_frequencies() == {
        "rx_hz": None,
        "tx_hz": None,
        "tx_active": False,
        "sample_rate": None,
        "rx_rf_bandwidth": None,
        "tx_rf_bandwidth": None,
        "updated_at": None,
    }
    assert poe.ups == 0


def test_records_rx_tx_sample_rate_and_bandwidths_separately():
    radio = _FirFakeRadio()
    obj = _plain_schema(radio=radio)

    t0 = time.time()
    rpc(obj, "set_rx_lo", np.int64(915_000_000))
    rpc(obj, "set_tx_lo", 2_450_000_000)
    rpc(obj, "set_sample_rate", 2_000_000)
    rpc(obj, "set_rx_rf_bandwidth", 1_500_000)
    rpc(obj, "set_tx_rf_bandwidth", 1_800_000)
    t1 = time.time()

    info = obj.active_frequencies()
    assert info["rx_hz"] == 915_000_000 and type(info["rx_hz"]) is int
    assert info["tx_hz"] == 2_450_000_000 and type(info["tx_hz"]) is int
    assert info["sample_rate"] == 2_000_000 and type(info["sample_rate"]) is int
    assert info["rx_rf_bandwidth"] == 1_500_000
    assert info["tx_rf_bandwidth"] == 1_800_000
    assert info["tx_active"] is False
    assert t0 <= info["updated_at"] <= t1

    # Repeated sample_rate hits the FIR skip early-return path and still records.
    obj.clear_active_frequencies()
    rpc(obj, "set_sample_rate", 2_000_000)
    assert obj.active_frequencies()["sample_rate"] == 2_000_000


def test_failed_and_refused_sets_are_not_recorded():
    radio = FakeRadio()
    obj = _plain_schema(radio=radio)

    with pytest.raises(Exception):
        rpc(obj, "set_rx_lo", "not-an-int")
    with pytest.raises(Exception):
        rpc(obj, "set_tx_lo", 2_450_000_000.5)
    assert obj.active_frequencies()["rx_hz"] is None
    assert obj.active_frequencies()["tx_hz"] is None
    assert obj.active_frequencies()["updated_at"] is None

    rpc(obj, "set_rx_lo", 915_000_000)
    with pytest.raises(Exception):
        rpc(obj, "set_rx_lo", 920_000_000.25)
    assert obj.active_frequencies()["rx_hz"] == 915_000_000

    radio.fail_tx = True
    with pytest.raises(RuntimeError):
        rpc(obj, "call_tx", np.zeros(16, dtype=np.complex64))
    assert obj.active_frequencies()["tx_active"] is False

    # Lockout refusals must not record frequencies or TX activity.
    locked = schema({**BAND, "rx_min_freq": 902e6, "rx_max_freq": 928e6, "max_sample_rate": 2e6, "tx_enabled": False})
    with pytest.raises(RemoteRFPolicyError):
        rpc(locked, "set_tx_lo", 900_000_000)
    with pytest.raises(RemoteRFPolicyError):
        rpc(locked, "set_rx_lo", 950_000_000)
    with pytest.raises(RemoteRFPolicyError):
        rpc(locked, "set_sample_rate", 4_000_000)
    with pytest.raises(RemoteRFPolicyError):
        rpc(locked, "call_tx", np.zeros(16, dtype=np.complex64))
    with pytest.raises(RemoteRFPolicyError):
        rpc(locked, "call_dds_single_tone", frequency=100_000, scale=0.5)
    assert locked.active_frequencies() == {
        "rx_hz": None,
        "tx_hz": None,
        "tx_active": False,
        "sample_rate": None,
        "rx_rf_bandwidth": None,
        "tx_rf_bandwidth": None,
        "updated_at": None,
    }


def test_tx_active_tracks_buffer_and_dds():
    obj = _plain_schema()

    # Stopping before anything started leaves updated_at unset.
    rpc(obj, "call_tx_destroy_buffer")
    rpc(obj, "call_disable_dds")
    assert obj.active_frequencies()["tx_active"] is False
    assert obj.active_frequencies()["updated_at"] is None

    # TX buffer lifecycle.
    rpc(obj, "call_tx", np.zeros(16, dtype=np.complex64))
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "call_tx_destroy_buffer")
    assert obj.active_frequencies()["tx_active"] is False

    # DDS followed by initial tx() disables DDS in pyadi-iio before creating _txbuf.
    rpc(obj, "call_dds_single_tone", frequency=100_000, scale=0.5)
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "call_tx", np.zeros(16, dtype=np.complex64))
    assert obj.device.dds_enabled == ["0", "0", "0", "0"]
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "call_tx_destroy_buffer")
    assert obj.device._txbuf is None
    assert obj.active_frequencies()["tx_active"] is False

    # Coexistence requires creating _txbuf first and enabling DDS afterward.
    rpc(obj, "call_tx", np.zeros(16, dtype=np.complex64))
    rpc(obj, "call_dds_single_tone", frequency=100_000, scale=0.5)
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "call_tx", np.zeros(16, dtype=np.complex64))  # _txbuf exists, so DDS stays enabled
    assert obj.device.dds_enabled == ["1", "1", "1", "1"]
    rpc(obj, "call_tx_destroy_buffer")
    assert obj.active_frequencies()["tx_active"] is True  # DDS still active
    rpc(obj, "call_disable_dds")
    assert obj.active_frequencies()["tx_active"] is False

    # Dual-tone and DDS vector setters.
    rpc(obj, "call_dds_dual_tone", frequency1=100_000, scale1=0.2, frequency2=200_000, scale2=0.2)
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "set_dds_enabled", [0, 0, 0, 0])
    assert obj.active_frequencies()["tx_active"] is False
    rpc(obj, "set_dds_enabled", [0, 1, 0, 0])
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "set_dds_scales", [0.0, 0.0, 0.0, 0.0])
    assert obj.active_frequencies()["tx_active"] is False
    rpc(obj, "set_dds_scales", [0.0, 0.25, 0.0, 0.0])
    assert obj.active_frequencies()["tx_active"] is True

    # Under lockouts, either stop method disables both buffer and DDS.
    locked = schema(BAND, radio=_PyadiFakeRadio())
    rpc(locked, "call_tx", np.zeros(16, dtype=np.complex64))
    rpc(locked, "call_dds_single_tone", frequency=100_000, scale=0.5)
    assert locked.active_frequencies()["tx_active"] is True
    rpc(locked, "call_tx_destroy_buffer")
    assert locked.active_frequencies()["tx_active"] is False


def test_reset_close_bind_and_poe_power_down_clear_state():
    # reset_for_next_holder clears even if hardware reset fails.
    radio = FakeRadio()
    obj = _plain_schema(radio=radio)
    rpc(obj, "set_rx_lo", 915_000_000)
    rpc(obj, "set_tx_lo", 920_000_000)
    rpc(obj, "call_tx", np.zeros(16, dtype=np.complex64))
    radio.fail_dds = True
    with pytest.raises(RuntimeError):
        obj.reset_for_next_holder()
    assert obj.active_frequencies() == {
        "rx_hz": None,
        "tx_hz": None,
        "tx_active": False,
        "sample_rate": None,
        "rx_rf_bandwidth": None,
        "tx_rf_bandwidth": None,
        "updated_at": None,
    }

    # close() clears recorded frequencies and invokes underlying close().
    closable = _CloseTrackingRadio()
    obj = _plain_schema(radio=closable)
    rpc(obj, "set_rx_lo", 2_400_000_000)
    rpc(obj, "call_tx", np.zeros(16, dtype=np.complex64))
    obj.close()
    assert closable.closed == 1
    assert obj.device is None
    assert obj.active_frequencies()["rx_hz"] is None
    assert obj.active_frequencies()["tx_active"] is False

    # bind(None) clears recorded frequencies.
    obj = _plain_schema()
    rpc(obj, "set_tx_lo", 915_000_000)
    obj.bind(None)
    assert obj.active_frequencies()["tx_hz"] is None

    # PoE power_down clears; non-PoE power_down (a no-op) keeps frequencies.
    poe = FakePoe(radio=FakeRadio())
    poe_obj = _plain_schema(radio=poe)
    rpc(poe_obj, "set_rx_lo", 915_000_000)
    rpc(poe_obj, "call_power_down")
    assert poe.downs == 1
    ups_after_power_down = poe.ups
    assert poe_obj.active_frequencies()["rx_hz"] is None
    assert poe.ups == ups_after_power_down  # active_frequencies never boots the powered-off Pluto

    usb_obj = _plain_schema()
    rpc(usb_obj, "set_rx_lo", 915_000_000)
    rpc(usb_obj, "call_power_down")
    assert usb_obj.active_frequencies()["rx_hz"] == 915_000_000


def test_poe_power_cycle_and_failed_power_calls_clear_state():
    # Successful PoE power_cycle clears frequencies; non-PoE power_cycle keeps them.
    poe = _ForwardingPoe(radio=FakeRadio())
    poe_obj = _plain_schema(radio=poe)
    rpc(poe_obj, "set_rx_lo", 915_000_000)
    rpc(poe_obj, "set_tx_lo", 920_000_000)
    rpc(poe_obj, "call_tx", np.zeros(16, dtype=np.complex64))
    ups_before = poe.ups
    rpc(poe_obj, "call_power_cycle")
    assert poe.downs == 1 and poe.ups == ups_before + 1
    assert poe_obj.active_frequencies()["rx_hz"] is None
    assert poe_obj.active_frequencies()["tx_hz"] is None
    assert poe_obj.active_frequencies()["tx_active"] is False
    assert poe_obj.active_frequencies()["updated_at"] is None

    usb_obj = _plain_schema()
    rpc(usb_obj, "set_rx_lo", 915_000_000)
    rpc(usb_obj, "call_power_cycle")
    assert usb_obj.active_frequencies()["rx_hz"] == 915_000_000

    # Failed reboot during power_cycle still clears state after power_down cuts power.
    class _FailBootPoe(_ForwardingPoe):
        def power_up(self):
            vars(self)["ups"] += 1
            if vars(self)["_pluto"] is None:
                raise RuntimeError("boot timeout")

    failing = _FailBootPoe(radio=FakeRadio())
    fail_obj = _plain_schema(radio=failing)
    rpc(fail_obj, "set_rx_lo", 915_000_000)
    rpc(fail_obj, "set_tx_lo", 920_000_000)
    rpc(fail_obj, "call_tx", np.zeros(16, dtype=np.complex64))
    with pytest.raises(RuntimeError, match="boot timeout"):
        rpc(fail_obj, "call_power_cycle")
    assert vars(failing)["_pluto"] is None
    assert fail_obj.active_frequencies()["rx_hz"] is None
    assert fail_obj.active_frequencies()["tx_hz"] is None
    assert fail_obj.active_frequencies()["tx_active"] is False
    assert fail_obj.active_frequencies()["updated_at"] is None

    # Failed power_down leaves the Pluto powered and transmitting, so state is preserved.
    fail_down = _ForwardingPoe(radio=_PyadiFakeRadio())
    vars(fail_down)["fail_down"] = True
    down_obj = _plain_schema(radio=fail_down)
    rpc(down_obj, "set_rx_lo", 915_000_000)
    rpc(down_obj, "set_tx_lo", 920_000_000)
    rpc(down_obj, "call_tx", np.zeros(16, dtype=np.complex64))
    with pytest.raises(RuntimeError, match="power-off failed"):
        rpc(down_obj, "call_power_down")
    assert down_obj.active_frequencies()["rx_hz"] == 915_000_000
    assert down_obj.active_frequencies()["tx_hz"] == 920_000_000
    assert down_obj.active_frequencies()["tx_active"] is True
    assert down_obj.active_frequencies()["updated_at"] is not None

    with pytest.raises(RuntimeError, match="power-off failed"):
        rpc(down_obj, "call_power_cycle")
    assert down_obj.active_frequencies()["rx_hz"] == 915_000_000
    assert down_obj.active_frequencies()["tx_hz"] == 920_000_000
    assert down_obj.active_frequencies()["tx_active"] is True
    assert down_obj.active_frequencies()["updated_at"] is not None


def test_tx_active_tracks_rf_loopback():
    obj = _plain_schema()

    # Disabling or setting digital loopback before any activity leaves updated_at unset.
    rpc(obj, "set_loopback", 0)
    rpc(obj, "set_loopback", 1)
    assert obj.active_frequencies()["tx_active"] is False
    assert obj.active_frequencies()["updated_at"] is None

    # RF loopback (mode 2) counts as active TX and survives tx_destroy_buffer().
    rpc(obj, "set_tx_lo", 915_000_000)
    rpc(obj, "set_loopback", 2)
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "call_tx", np.zeros(16, dtype=np.complex64))
    rpc(obj, "call_tx_destroy_buffer")
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "set_loopback", 0)
    assert obj.active_frequencies()["tx_active"] is False

    # Refused RF loopback under lockouts must not mark tx_active.
    refused_obj = schema(BAND)
    with pytest.raises(RemoteRFPolicyError):
        rpc(refused_obj, "set_loopback", 2)
    assert refused_obj.active_frequencies()["tx_active"] is False
    assert refused_obj.active_frequencies()["updated_at"] is None

    # Allowed RF loopback under lockouts stays tx_active across stop methods until loopback is cleared.
    allowed_obj = schema({**BAND, "allow_rf_loopback": True})
    rpc(allowed_obj, "set_tx_lo", 915_000_000)
    rpc(allowed_obj, "set_loopback", 2)
    assert allowed_obj.active_frequencies()["tx_active"] is True
    rpc(allowed_obj, "call_tx", np.zeros(16, dtype=np.complex64))
    rpc(allowed_obj, "call_tx_destroy_buffer")
    assert allowed_obj.active_frequencies()["tx_active"] is True
    rpc(allowed_obj, "set_loopback", 1)
    assert allowed_obj.active_frequencies()["tx_active"] is False


def test_lockout_hygiene_and_after_hook_clear_tx_active_and_update_parked_tx_lo():
    # Recovery from _broken via _lockout_hygiene clears tx_active (buffer + RF loopback).
    locked = schema({**BAND, "allow_rf_loopback": True})
    rpc(locked, "set_tx_lo", 915_000_000)
    rpc(locked, "call_tx", np.zeros(16, dtype=np.complex64))
    rpc(locked, "set_loopback", 2)
    assert locked.active_frequencies()["tx_active"] is True
    locked._broken = True
    rpc(locked, "get_rx_lo")
    assert locked._tx_flag is False
    assert locked._rf_loopback is False
    assert locked.active_frequencies()["tx_active"] is False

    # Handle-change hygiene clears tx_active and updates parked tx_hz when tx_hz was previously set.
    poe = _ForwardingPoe(radio=FakeRadio())
    reconnecting = schema(
        {**BAND, "lockout_allow_methods": ["call_reconnect"]},
        cls=CustomMIMO,
        radio=poe,
    )
    rpc(reconnecting, "set_tx_lo", 920_000_000)
    rpc(reconnecting, "call_tx", np.zeros(16, dtype=np.complex64))
    assert reconnecting.active_frequencies()["tx_hz"] == 920_000_000
    assert reconnecting.active_frequencies()["tx_active"] is True
    rpc(reconnecting, "call_reconnect")
    assert reconnecting.active_frequencies()["tx_active"] is False
    assert reconnecting.active_frequencies()["tx_hz"] == 915_000_000

    # Parking during hygiene must not invent tx_hz when the holder never configured tx_lo.
    rx_only_radio = FakeRadio()
    rx_only = schema(BAND, radio=rx_only_radio)
    assert rx_only.active_frequencies()["tx_hz"] is None
    rpc(rx_only, "set_rx_lo", 915_000_000)
    rx_only_radio.tx_lo = 2_450_000_000
    rx_only._broken = True
    rpc(rx_only, "get_rx_lo")
    assert rx_only_radio.tx_lo == 915_000_000
    assert rx_only.active_frequencies()["tx_hz"] is None

    # lockout_after_hook on failed non-PoE reset clears tx_active flags.
    hook_obj = schema({**BAND, "allow_rf_loopback": True})
    rpc(hook_obj, "set_tx_lo", 915_000_000)
    rpc(hook_obj, "set_loopback", 2)
    assert hook_obj.active_frequencies()["tx_active"] is True
    hook_obj.lockout_after_hook("reset", failed=True)
    assert hook_obj.active_frequencies()["tx_active"] is False


def test_mimo_and_custom_pluto_subclasses_inherit_recording():
    for cls in (PlutoMIMOSchema, CustomMIMO):
        obj = _plain_schema(cls=cls)
        rpc(obj, "set_rx_lo", 2_412_000_000)
        rpc(obj, "set_tx_lo", 2_437_000_000)
        rpc(obj, "set_sample_rate", 3_000_000)
        rpc(obj, "call_dds_single_tone", frequency=50_000, scale=0.3, channel=1)
        info = obj.active_frequencies()
        assert (info["rx_hz"], info["tx_hz"], info["sample_rate"], info["tx_active"]) == (
            2_412_000_000,
            2_437_000_000,
            3_000_000,
            True,
        )
        obj.reset_for_next_holder()
        assert obj.active_frequencies()["rx_hz"] is None
        assert obj.active_frequencies()["tx_active"] is False


def test_schemas_json_unchanged_and_no_new_exposed_methods():
    rows = {row["device_type"]: row for row in manifest.load()}
    pluto = PlutoSchema()
    mimo = PlutoMIMOSchema()

    assert pluto.get_idl_hash() == rows["adalm_pluto"]["v1"]
    assert mimo.get_idl_hash() == rows["adalm_pluto_mimo"]["v1"]
    assert pluto.get_idl_hash() == "sha256:93097fbb33d6680473ccb681d1854d031702b216ebd9f35dfd03d58eea797ad5"
    assert mimo.get_idl_hash() == "sha256:2a492432fecdab19672f50edfb054a69927091d1c78024a1a7dcf20cc0867784"

    for name in ("active_frequencies", "clear_active_frequencies", "close", "close_native_device", "bind"):
        assert name not in pluto._exposed
        assert name not in mimo._exposed


def test_any_nonzero_and_string_dds_enabled_do_not_latch_tx_active():
    for zero in (
        "0",
        "0.0",
        " 0 ",
        "-0.0",
        " 0.000000 ",
        "false",
        "FALSE",
        "off",
        "OFF",
        0,
        0.0,
        False,
        None,
        np.int64(0),
        np.float32(0.0),
        np.bool_(False),
        np.str_("0"),
        np.str_("false"),
        ["0", "0", "0", "0"],
        np.array(["0", "0.0", "false", "off"]),
        [["0", " 0 "], ["false", [0, 0.0, False, np.int64(0)]]],
    ):
        assert _any_nonzero(zero) is False, f"expected False for {zero!r}"

    for nonzero in (
        "1",
        "0.25",
        " 1 ",
        1,
        0.1,
        True,
        np.int64(1),
        np.float64(0.5),
        np.bool_(True),
        np.str_("1"),
        [["0", "0"], ["off", "0.1"]],
        "garbage",
        "",
        float("nan"),
        "nan",
        object(),
        ["0", "unknown"],
    ):
        assert _any_nonzero(nonzero) is True, f"expected True for {nonzero!r}"

    # Real pyadi-iio reads back dds_enabled as ['0', '0', '0', '0'] after writing ints/bools.
    radio = _PyadiFakeRadio()
    radio.dds_enabled = [1, 1, 1, 1]
    radio.dds_scales = [0.25, 0.25, 0.25, 0.25]
    assert radio.dds_enabled == ["1", "1", "1", "1"]
    assert radio.dds_scales == ["0.250000", "0.250000", "0.250000", "0.250000"]
    obj = _plain_schema(radio=radio)

    rpc(obj, "set_dds_enabled", [0, 0, 0, 0])
    assert radio.dds_enabled == ["0", "0", "0", "0"]
    assert obj.active_frequencies()["tx_active"] is False
    rpc(obj, "call_tx", np.zeros(16, dtype=np.complex64))
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "call_tx_destroy_buffer")
    assert obj.active_frequencies()["tx_active"] is False

    # Same with lockouts enabled (which normalize bool/int vectors to ints).
    locked_radio = _PyadiFakeRadio()
    locked = schema(BAND, radio=locked_radio)
    rpc(locked, "set_dds_scales", [0.25, 0.25, 0.25, 0.25])
    rpc(locked, "set_dds_enabled", [True, True, True, True])
    assert locked_radio.dds_enabled == ["1", "1", "1", "1"]
    assert locked.active_frequencies()["tx_active"] is True
    rpc(locked, "set_dds_enabled", [False, False, False, False])
    assert locked_radio.dds_enabled == ["0", "0", "0", "0"]
    assert locked.active_frequencies()["tx_active"] is False


def test_tx_active_dds_scale_enable_combinations_and_string_loopback():
    radio = _PyadiFakeRadio()
    radio.dds_enabled = [0, 0, 0, 0]
    radio.dds_scales = [0.0, 0.0, 0.0, 0.0]
    obj = _plain_schema(radio=radio)

    # Zero-scale single/dual tone calls must not report active TX, and must clear prior DDS activity.
    rpc(obj, "call_dds_single_tone", frequency=100_000, scale=0)
    assert obj.active_frequencies()["tx_active"] is False
    rpc(obj, "call_dds_single_tone", frequency=100_000, scale=0.5)
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "call_dds_single_tone", frequency=100_000, scale=0.0)
    assert obj.active_frequencies()["tx_active"] is False

    rpc(obj, "call_dds_dual_tone", frequency1=100_000, scale1=0, frequency2=200_000, scale2=0.0)
    assert obj.active_frequencies()["tx_active"] is False
    rpc(obj, "call_dds_dual_tone", frequency1=100_000, scale1=0, frequency2=200_000, scale2=0.2)
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "call_dds_dual_tone", frequency1=100_000, scale1=0, frequency2=200_000, scale2="0")
    assert obj.active_frequencies()["tx_active"] is False

    # Setting non-zero scales while all channels are disabled stays inactive.
    radio.dds_enabled = [0, 0, 0, 0]
    rpc(obj, "set_dds_scales", [0.5, 0.0, 0.0, 0.0])
    assert radio.dds_scales == ["0.500000", "0.000000", "0.000000", "0.000000"]
    assert obj.active_frequencies()["tx_active"] is False

    # Enabling a channel whose scale is zero stays inactive (including mismatched channels).
    rpc(obj, "set_dds_enabled", [0, 1, 0, 0])
    assert radio.dds_enabled == ["0", "1", "0", "0"]
    assert obj.active_frequencies()["tx_active"] is False

    # Enabling all channels while scales are all zero stays inactive.
    rpc(obj, "set_dds_scales", [0.0, 0.0, 0.0, 0.0])
    rpc(obj, "set_dds_enabled", [1, 1, 1, 1])
    assert obj.active_frequencies()["tx_active"] is False

    # Active only when the same channel is both enabled and at non-zero scale.
    rpc(obj, "set_dds_enabled", [0, 1, 0, 0])
    rpc(obj, "set_dds_scales", [0.0, 0.25, 0.0, 0.0])
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "set_dds_enabled", [0, 0, 0, 0])
    assert obj.active_frequencies()["tx_active"] is False

    # String loopback values ("2" / " 2 ") must track RF loopback state.
    rpc(obj, "set_loopback", "2")
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "call_tx_destroy_buffer")
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "set_loopback", "0")
    assert obj.active_frequencies()["tx_active"] is False
    rpc(obj, "set_loopback", " 2 ")
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "set_loopback", "1")
    assert obj.active_frequencies()["tx_active"] is False


def test_tx_disables_dds_on_initial_buffer_creation_and_clears_tx_active():
    radio = _PyadiFakeRadio()
    obj = _plain_schema(radio=radio)

    rpc(obj, "call_dds_single_tone", frequency=100_000, scale=0.5)
    assert radio.dds_enabled == ["1", "1", "1", "1"]
    assert radio._txbuf is None
    assert obj.active_frequencies()["tx_active"] is True

    # First tx() creates _txbuf and disables DDS in pyadi-iio.
    rpc(obj, "call_tx", np.zeros(16, dtype=np.complex64))
    assert radio._txbuf is True
    assert radio.dds_enabled == ["0", "0", "0", "0"]
    assert obj.active_frequencies()["tx_active"] is True

    rpc(obj, "call_tx_destroy_buffer")
    assert radio._txbuf is None
    assert obj.active_frequencies()["tx_active"] is False

    # When _txbuf already exists, a subsequent tx() does not call disable_dds().
    rpc(obj, "call_tx", np.zeros(16, dtype=np.complex64))
    rpc(obj, "call_dds_single_tone", frequency=100_000, scale=0.5)
    rpc(obj, "call_tx", np.zeros(16, dtype=np.complex64))
    assert radio.dds_enabled == ["1", "1", "1", "1"]
    rpc(obj, "call_tx_destroy_buffer")
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "call_disable_dds")
    assert obj.active_frequencies()["tx_active"] is False


def test_partial_dds_writes_and_readback_errors_stay_active():
    # Partial writes leave higher channels live in pyadi-iio (both unlocked and locked).
    for locked in (False, True):
        radio = _PyadiFakeRadio()
        obj = schema(BAND, radio=radio) if locked else _plain_schema(radio=radio)
        rpc(obj, "set_dds_scales", [0.5, 0.5, 0.5, 0.5])
        rpc(obj, "set_dds_enabled", [1, 1, 1, 1])
        assert obj.active_frequencies()["tx_active"] is True

        rpc(obj, "set_dds_enabled", [0])
        assert radio.dds_enabled == ["0", "1", "1", "1"]
        assert obj.active_frequencies()["tx_active"] is True

        rpc(obj, "set_dds_scales", [0.0])
        assert radio.dds_scales == ["0.000000", "0.500000", "0.500000", "0.500000"]
        assert obj.active_frequencies()["tx_active"] is True

    # IIO readback exceptions must treat the unreadable attribute as unknown (active),
    # never falling back to a partial written vector like [0] or [0.0].
    class _UnreadableDdsRadio(_PyadiFakeRadio):
        fail_read_enabled = False
        fail_read_scales = False

        @property
        def dds_enabled(self):
            if self.fail_read_enabled:
                raise OSError("IIO read failed")
            return super().dds_enabled

        @dds_enabled.setter
        def dds_enabled(self, value):
            _PyadiFakeRadio.dds_enabled.fset(self, value)

        @property
        def dds_scales(self):
            if self.fail_read_scales:
                raise OSError("IIO read failed")
            return super().dds_scales

        @dds_scales.setter
        def dds_scales(self, value):
            _PyadiFakeRadio.dds_scales.fset(self, value)

    err_radio = _UnreadableDdsRadio()
    err_obj = _plain_schema(radio=err_radio)
    rpc(err_obj, "set_dds_scales", [0.5, 0.5, 0.5, 0.5])
    rpc(err_obj, "set_dds_enabled", [1, 1, 1, 1])

    err_radio.fail_read_enabled = True
    rpc(err_obj, "set_dds_enabled", [0])
    assert err_obj.active_frequencies()["tx_active"] is True

    err_radio.fail_read_scales = True
    rpc(err_obj, "set_dds_enabled", [0])
    assert err_obj.active_frequencies()["tx_active"] is True

    err_radio.fail_read_enabled = False
    rpc(err_obj, "set_dds_scales", [0.0])
    assert err_obj.active_frequencies()["tx_active"] is True

    # When dds_enabled readback fails but hardware confirms all scales are zero, inactive is safe.
    err_radio.fail_read_scales = False
    rpc(err_obj, "set_dds_scales", [0.0, 0.0, 0.0, 0.0])
    err_radio.fail_read_enabled = True
    rpc(err_obj, "set_dds_enabled", [0])
    assert err_obj.active_frequencies()["tx_active"] is False


def test_failed_poe_power_off_preserves_active_frequencies_and_tx_active(monkeypatch):
    from remoterf_device_core.drivers.adalm_pluto import poe

    fail_off = [True]

    def fake_set_poe(*, on, **kw):
        if not on and fail_off[0]:
            raise OSError("switch timeout")

    monkeypatch.setattr(poe, "set_poe", fake_set_poe)
    monkeypatch.setattr(poe.time, "sleep", lambda s: None)

    poe_dev = PoePluto(ip="192.0.2.1", port="ether5")
    vars(poe_dev)["_pluto"] = _PyadiFakeRadio()
    obj = _plain_schema(radio=poe_dev)

    rpc(obj, "set_rx_lo", 915_000_000)
    rpc(obj, "set_tx_lo", 920_000_000)
    rpc(obj, "call_dds_single_tone", frequency=100_000, scale=0.5)

    # Failed power_down drops _pluto and sets _power_off_failed, but Pluto stays powered and live.
    with pytest.raises(OSError, match="switch timeout"):
        rpc(obj, "call_power_down")
    assert vars(poe_dev)["_pluto"] is None
    assert vars(poe_dev)["_power_off_failed"] is True
    info = obj.active_frequencies()
    assert info["rx_hz"] == 915_000_000
    assert info["tx_hz"] == 920_000_000
    assert info["tx_active"] is True
    assert info["updated_at"] is not None

    # Failed power_cycle during power_down also preserves recorded frequencies and tx_active.
    with pytest.raises(OSError, match="switch timeout"):
        rpc(obj, "call_power_cycle")
    assert vars(poe_dev)["_pluto"] is None
    assert vars(poe_dev)["_power_off_failed"] is True
    info = obj.active_frequencies()
    assert info["rx_hz"] == 915_000_000
    assert info["tx_hz"] == 920_000_000
    assert info["tx_active"] is True
    assert info["updated_at"] is not None

    # A later successful power_down cuts power and clears recorded frequencies.
    fail_off[0] = False
    rpc(obj, "call_power_down")
    assert vars(poe_dev)["_power_off_failed"] is False
    assert obj.active_frequencies()["rx_hz"] is None
    assert obj.active_frequencies()["tx_hz"] is None
    assert obj.active_frequencies()["tx_active"] is False

    # When power_down succeeds during power_cycle but power_up fails, state IS cleared.
    vars(poe_dev)["_pluto"] = _PyadiFakeRadio()
    rpc(obj, "set_rx_lo", 915_000_000)
    rpc(obj, "set_tx_lo", 920_000_000)
    rpc(obj, "call_tx", np.zeros(16, dtype=np.complex64))
    monkeypatch.setattr(poe, "_iiod_up", lambda ip: False)
    monkeypatch.setattr(poe, "port_running", lambda **kw: False)
    times = iter([0.0, poe.LINK_CHECK_S + 1.0])
    monkeypatch.setattr(poe.time, "monotonic", lambda: next(times))
    with pytest.raises(RuntimeError, match="no Ethernet link"):
        rpc(obj, "call_power_cycle")
    assert vars(poe_dev)["_pluto"] is None
    assert vars(poe_dev)["_power_off_failed"] is False
    assert obj.active_frequencies()["rx_hz"] is None
    assert obj.active_frequencies()["tx_hz"] is None
    assert obj.active_frequencies()["tx_active"] is False


def test_deferred_poe_power_cut_in_power_up_clears_stale_active_frequencies(monkeypatch):
    from remoterf_device_core.drivers.adalm_pluto import poe

    fail_off = [False]
    link_up = [True]

    def fake_set_poe(*, on, **kw):
        if not on and fail_off[0]:
            raise OSError("switch timeout")

    monkeypatch.setattr(poe, "set_poe", fake_set_poe)
    monkeypatch.setattr(poe.time, "sleep", lambda s: None)
    monkeypatch.setattr(poe, "_iiod_up", lambda ip: link_up[0])
    monkeypatch.setattr(poe, "port_running", lambda **kw: link_up[0])

    def make_board(uri):
        radio = _PyadiFakeRadio()
        radio.tx_lo = 2_450_000_000
        radio._ctx = types.SimpleNamespace(attrs={})
        radio.uri = uri
        return radio

    monkeypatch.setattr(sys.modules["adi"], "Pluto", make_board, raising=False)

    def setup_failed_power_down(lockouts=None):
        fail_off[0] = False
        link_up[0] = True
        poe_dev = PoePluto(ip="192.0.2.1", port="ether5")
        vars(poe_dev)["_pluto"] = make_board("ip:192.0.2.1")
        obj = schema(lockouts, radio=poe_dev) if lockouts is not None else _plain_schema(radio=poe_dev)
        rpc(obj, "set_rx_lo", 915_000_000)
        rpc(obj, "set_tx_lo", 920_000_000)
        rpc(obj, "call_dds_single_tone", frequency=100_000, scale=0.5)
        fail_off[0] = True
        with pytest.raises(OSError, match="switch timeout"):
            rpc(obj, "call_power_down")
        assert vars(poe_dev)["_power_off_failed"] is True
        assert obj.active_frequencies()["tx_active"] is True
        fail_off[0] = False
        return poe_dev, obj

    # 1. Explicit call_power_up() completes deferred power cut + cold boot -> clears all stale state.
    poe_dev, obj = setup_failed_power_down()
    rpc(obj, "call_power_up")
    assert vars(poe_dev)["_power_off_failed"] is False
    assert obj.active_frequencies() == {
        "rx_hz": None,
        "tx_hz": None,
        "tx_active": False,
        "sample_rate": None,
        "rx_rf_bandwidth": None,
        "tx_rf_bandwidth": None,
        "updated_at": None,
    }

    # 2. Lazy PoePluto.__setattr__ (set_rx_lo) clears pre-reboot tx_hz/tx_active before recording new rx_hz.
    _, obj = setup_failed_power_down()
    rpc(obj, "set_rx_lo", 925_000_000)
    info = obj.active_frequencies()
    assert info["rx_hz"] == 925_000_000
    assert info["tx_hz"] is None
    assert info["tx_active"] is False

    # 3. Lazy PoePluto.__getattr__ (get_rx_lo) clears stale pre-reboot state.
    _, obj = setup_failed_power_down()
    rpc(obj, "get_rx_lo")
    info = obj.active_frequencies()
    assert info["rx_hz"] is None
    assert info["tx_hz"] is None
    assert info["tx_active"] is False

    # 4. Deferred _poe(False) succeeds in prepare_for_holder() and _boot() fails -> Pluto is OFF, state cleared.
    poe_dev, obj = setup_failed_power_down()
    link_up[0] = False
    times = iter([0.0, poe.LINK_CHECK_S + 1.0])
    with monkeypatch.context() as mp:
        mp.setattr(poe.time, "monotonic", lambda: next(times))
        with pytest.raises(RuntimeError, match="no Ethernet link"):
            obj.prepare_for_holder()
    assert vars(poe_dev)["_pluto"] is None
    assert vars(poe_dev)["_power_off_failed"] is False
    info = obj.active_frequencies()
    assert info["rx_hz"] is None
    assert info["tx_hz"] is None
    assert info["tx_active"] is False

    # 5. Locked-out dispatch boots after failed power_down and clears stale rx_hz/tx_hz without inventing parked tx_hz.
    locked_poe, locked = setup_failed_power_down(lockouts=BAND)
    rpc(locked, "get_rx_lo")
    assert vars(locked_poe)["_pluto"].tx_lo == 915_000_000
    info = locked.active_frequencies()
    assert info["rx_hz"] is None
    assert info["tx_hz"] is None
    assert info["tx_active"] is False


def test_tx_zero_source_and_partial_failures_reconcile_tx_active():
    # 1. Zero-source tx() with empty tx_enabled_channels does not create _txbuf or latch tx_active.
    radio = _PyadiFakeRadio()
    obj = _plain_schema(radio=radio)
    rpc(obj, "set_tx_enabled_channels", [])
    rpc(obj, "call_tx", [])
    assert radio._txbuf is None
    assert obj.active_frequencies()["tx_active"] is False

    # When only altvoltage0 DDS was enabled, zero-source tx() clears it and reconciles tx_active=False.
    rpc(obj, "set_dds_scales", [0.5, 0.0, 0.0, 0.0])
    rpc(obj, "set_dds_enabled", [1, 0, 0, 0])
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "call_tx", [])
    assert radio._txbuf is None
    assert obj.active_frequencies()["tx_active"] is False

    # When another DDS channel remains active, zero-source tx() keeps tx_active=True.
    rpc(obj, "set_dds_scales", [0.5, 0.0, 0.5, 0.0])
    rpc(obj, "set_dds_enabled", [1, 0, 1, 0])
    rpc(obj, "call_tx", [])
    assert radio._txbuf is None
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "call_disable_dds")
    assert obj.active_frequencies()["tx_active"] is False

    # 2. call_tx raising after disable_dds() reconciles dds_active and buffer state from the radio.
    class _PartialTxRadio(_PyadiFakeRadio):
        fail_after_disable = False
        fail_after_alloc = False

        def tx(self, samples=None):
            if not self._txbuf:
                self.disable_dds()
                if self.fail_after_disable:
                    raise RuntimeError("tx_init_channels failed")
                self._txbuf = True
                if self.fail_after_alloc:
                    raise RuntimeError("tx_buffer_push failed")

    ptx_radio = _PartialTxRadio()
    ptx_obj = _plain_schema(radio=ptx_radio)
    rpc(ptx_obj, "call_dds_single_tone", frequency=100_000, scale=0.5)
    assert ptx_obj.active_frequencies()["tx_active"] is True
    ptx_radio.fail_after_disable = True
    with pytest.raises(RuntimeError, match="tx_init_channels failed"):
        rpc(ptx_obj, "call_tx", np.zeros(16, dtype=np.complex64))
    assert ptx_radio._txbuf is None
    assert ptx_obj.active_frequencies()["tx_active"] is False

    ptx_radio.fail_after_disable = False
    ptx_radio.fail_after_alloc = True
    rpc(ptx_obj, "call_dds_single_tone", frequency=100_000, scale=0.5)
    with pytest.raises(RuntimeError, match="tx_buffer_push failed"):
        rpc(ptx_obj, "call_tx", np.zeros(16, dtype=np.complex64))
    assert ptx_radio._txbuf is True
    assert ptx_obj.active_frequencies()["tx_active"] is True
    rpc(ptx_obj, "call_tx_destroy_buffer")
    assert ptx_obj.active_frequencies()["tx_active"] is False

    # 3. Partial-progress exceptions in DDS setters and tone helpers reconcile from hardware.
    class _PartialDdsRadio(_PyadiFakeRadio):
        fail_dual_tone2 = False
        fail_single_channel1 = True

        def dds_single_tone(self, frequency, scale, channel=0):
            n = len(self._dds_scales)
            self.dds_scales = [0] * n
            self.dds_enabled = [1] * n
            base = channel * 4
            if self.fail_single_channel1 and base + 2 >= n:
                raise AttributeError("'NoneType' object has no attribute 'attrs'")
            for idx in (base, base + 2):
                self._dds_scales[idx] = f"{float(scale):.6f}"

        def dds_dual_tone(self, frequency1, scale1, frequency2, scale2, channel=0):
            n = len(self._dds_scales)
            self.dds_scales = [0] * n
            self.dds_enabled = [1] * n
            base = channel * 4
            for idx in (base, base + 2):
                self._dds_scales[idx] = f"{float(scale1):.6f}"
            if self.fail_dual_tone2:
                raise OSError("tone 2 write failed")
            for idx in (base + 1, base + 3):
                self._dds_scales[idx] = f"{float(scale2):.6f}"

    pdds = _PartialDdsRadio()
    pdds_obj = _plain_schema(radio=pdds)
    rpc(pdds_obj, "set_dds_scales", [0.0, 0.0, 0.0, 0.0])
    rpc(pdds_obj, "set_dds_enabled", [1, 0, 0, 0])
    assert pdds_obj.active_frequencies()["tx_active"] is False

    # set_dds_scales writes channel 0 (0.5) before raising on channel 1 -> channel 0 is live.
    with pytest.raises(ValueError):
        rpc(pdds_obj, "set_dds_scales", [0.5, "bad"])
    assert pdds_obj.active_frequencies()["tx_active"] is True

    # set_dds_enabled disables channel 0 before raising on channel 1 -> hardware is silent.
    with pytest.raises(ValueError):
        rpc(pdds_obj, "set_dds_enabled", [0, "bad"])
    assert pdds_obj.active_frequencies()["tx_active"] is False

    # dds_dual_tone writes Tone 1 before failing on Tone 2 -> Tone 1 is live.
    pdds.fail_dual_tone2 = True
    with pytest.raises(OSError, match="tone 2 write failed"):
        rpc(pdds_obj, "call_dds_dual_tone", frequency1=100_000, scale1=0.5, frequency2=200_000, scale2=0.2)
    assert pdds_obj.active_frequencies()["tx_active"] is True

    # dds_single_tone zeroes all scales before failing on invalid channel=1 -> hardware is silent.
    with pytest.raises(AttributeError):
        rpc(pdds_obj, "call_dds_single_tone", frequency=100_000, scale=0.5, channel=1)
    assert pdds_obj.active_frequencies()["tx_active"] is False

    # Failed power_up during set_dds_scales on an off PoE Pluto must not latch tx_active=True.
    class _FailBootOnAttrPoe(_ForwardingPoe):
        def power_up(self):
            vars(self)["ups"] += 1
            if vars(self)["_pluto"] is None:
                raise RuntimeError("boot failed")

        def __setattr__(self, name, value):
            return PoePluto.__setattr__(self, name, value)

    off_poe = _FailBootOnAttrPoe(radio=None)
    off_obj = _plain_schema(radio=off_poe)
    with pytest.raises(RuntimeError, match="boot failed"):
        rpc(off_obj, "set_dds_scales", [0.5, 0.0, 0.0, 0.0])
    assert off_obj.active_frequencies()["tx_active"] is False


def test_dds_pair_active_does_not_flag_disabled_shorter_vector_as_active():
    # Writing all-zero 4-channel dds_enabled on a radio with 8-element dds_scales must not latch tx_active.
    radio = FakeRadio()
    obj = _plain_schema(radio=radio)
    rpc(obj, "set_dds_enabled", [0, 0, 0, 0])
    assert obj.active_frequencies()["tx_active"] is False

    rpc(obj, "call_disable_dds")
    assert obj.active_frequencies()["tx_active"] is False
    rpc(obj, "set_dds_enabled", [0, 0, 0, 0])
    assert obj.active_frequencies()["tx_active"] is False

    # Conversely, all-zero 4-channel scales with longer enabled vector must not report active.
    radio.dds_enabled = [1] * 8
    rpc(obj, "set_dds_scales", [0.0, 0.0, 0.0, 0.0])
    assert obj.active_frequencies()["tx_active"] is False

    # Unknown attributes remain conservatively active when the other side is nonzero.
    assert _dds_pair_active([0, 0, 0, 0], [0.9] * 8) is False
    assert _dds_pair_active([1] * 8, [0.0, 0.0, 0.0, 0.0]) is False
    assert _dds_pair_active(_UNKNOWN, [0.5, 0.5, 0.5, 0.5]) is True
    assert _dds_pair_active(_UNKNOWN, [0.0, 0.0, 0.0, 0.0]) is False
    assert _dds_pair_active([1, 1, 1, 1], _UNKNOWN) is True
    assert _dds_pair_active([0, 0, 0, 0], _UNKNOWN) is False


def test_fake_radios_match_pyadi_dds_and_cyclic_tx_semantics():
    # _PyadiFakeRadio must not have its _dds_scales clobbered by FakeRadio.__init__,
    # and defaults to tx_cyclic_buffer=False, loopback=0 like real pyadi-iio.
    pyadi_radio = _PyadiFakeRadio()
    assert pyadi_radio.dds_enabled == ["0", "0", "0", "0"]
    assert pyadi_radio.dds_scales == ["0.000000", "0.000000", "0.000000", "0.000000"]
    assert pyadi_radio.tx_cyclic_buffer is False
    assert pyadi_radio.loopback == 0

    obj = _plain_schema(radio=pyadi_radio)
    rpc(obj, "set_dds_enabled", [1, 0, 0, 0])
    assert obj.active_frequencies()["tx_active"] is False

    # Cyclic mode raises on a second tx() before tx_destroy_buffer(), matching adi.rx_tx.tx_core.tx().
    for radio in (_PyadiFakeRadio(), FakeRadio()):
        radio.tx_cyclic_buffer = True
        radio.tx(np.zeros(16, dtype=np.complex64))
        with pytest.raises(Exception, match="cyclic mode"):
            radio.tx(np.zeros(16, dtype=np.complex64))
        radio.tx_destroy_buffer()
        radio.tx(np.zeros(16, dtype=np.complex64))

    # FakeRadio initializes dds_enabled and maintains dds_enabled/dds_scales in tone helpers.
    raw = FakeRadio()
    assert raw.dds_enabled == [0] * 8
    raw_obj = _plain_schema(radio=raw)
    rpc(raw_obj, "set_dds_scales", [0.25, 0.0, 0.0, 0.0])
    assert raw_obj.active_frequencies()["tx_active"] is False

    locked = schema(BAND)
    rpc(locked, "call_dds_single_tone", frequency=100_000, scale=0.5)
    assert any(locked.device.dds_enabled)
    assert any(locked.device.dds_scales)
    rpc(locked, "set_dds_scales", [0.25, 0.0, 0.25, 0.0])
    assert locked.active_frequencies()["tx_active"] is True
    rpc(locked, "call_disable_dds")
    assert locked.active_frequencies()["tx_active"] is False
    rpc(locked, "call_dds_dual_tone", frequency1=100_000, scale1=0.2, frequency2=200_000, scale2=0.3)
    assert any(locked.device.dds_enabled)
    assert any(locked.device.dds_scales)
    rpc(locked, "set_dds_scales", [0.0, 0.25, 0.0, 0.25])
    assert locked.active_frequencies()["tx_active"] is True
