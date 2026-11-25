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
from remoterf_device_core.drivers.adalm_pluto.pluto_schema import PlutoSchema
from remoterf_device_core.v2_errors import RemoteRFPolicyError

from test_pluto_lockouts import BAND, CustomMIMO, FakePoe, FakeRadio, rpc, schema


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


class _CloseTrackingRadio(FakeRadio):
    def __init__(self):
        super().__init__()
        self.closed = 0

    def close(self):
        self.closed += 1


def _plain_schema(cls=PlutoSchema, radio=None):
    obj = cls()
    obj.bind(radio if radio is not None else FakeRadio())
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

    # DDS lifecycle and coexistence with TX buffer.
    rpc(obj, "call_dds_single_tone", frequency=100_000, scale=0.5)
    assert obj.active_frequencies()["tx_active"] is True
    rpc(obj, "call_tx", np.zeros(16, dtype=np.complex64))
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
    locked = schema(BAND)
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
