# Copyright (C) 2026 RemoteRF
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# SAMPLE schema for ADI Pluto SDR.
# Place into -> ~/.config/remoterf/drivers and restart server to utilize!


from remoterf_device_core.idl import DeviceSchema, idl_register, idl_expose
from remoterf_device_core.drivers.adalm_pluto.poe import PoePluto, is_poe, poe_pluto_from_init

import subprocess
import re
import logging
import math
import time
from collections.abc import Mapping
from contextlib import nullcontext
from numbers import Real, Integral

import adi
import numpy as np

from remoterf_device_core.v2_errors import RemoteRFPolicyError


from remoterf_device_core.drivers.adalm_pluto.lockout_keys import LOCKOUT_KEYS
_BOOL_KEYS = {"admin_group_override", "tx_enabled", "allow_rf_loopback", "allow_dds", "allow_cyclic_tx"}
_FREQ_KEYS = {"tx_min_freq", "tx_max_freq", "rx_min_freq", "rx_max_freq", "park_tx_lo"}
_STOP_METHODS = {"call_tx_destroy_buffer", "call_disable_dds"}
_DDS_CALLS = {"call_dds_single_tone", "call_dds_dual_tone"}
_DDS_VECTORS = {"set_dds_enabled", "set_dds_scales", "set_dds_frequencies", "set_dds_phases"}
_NUMBER_SETTERS = {
    "set_tx_lo", "set_rx_lo", "set_sample_rate", "set_rx_rf_bandwidth", "set_tx_rf_bandwidth",
    "set_rx_hardwaregain_chan0", "set_rx_hardwaregain_chan1",
    "set_tx_hardwaregain_chan0", "set_tx_hardwaregain_chan1",
}
_BOOL_SETTERS = {"set_tx_cyclic_buffer", "set_rx_annotated", "set_rx_dec8_filter_en", "set_tx_int8_filter_en"}
_CHANNEL_SETTERS = {"set_tx_enabled_channels", "set_rx_enabled_channels"}
_OTHER_METHODS = {
    "set_filter", "set_loopback", "set_rx_buffer_size", "set_rx_output_type",
    "set_gain_control_mode_chan0", "set_gain_control_mode_chan1", "call_tx", "call_rx",
    "call_rx_destroy_buffer", "call_power_up", "call_power_down", "call_power_cycle", "call_ip",
}
_LOCKOUT_METHODS = frozenset(
    _NUMBER_SETTERS | _BOOL_SETTERS | _CHANNEL_SETTERS | _OTHER_METHODS
    | _DDS_VECTORS | _DDS_CALLS | _STOP_METHODS
)
_BROKEN_MESSAGE = "This device is locked until it can be reset to a safe state; try again shortly."
_log = logging.getLogger(__name__)


def _config_number(key, value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{key} must be a finite number")
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{key} must be a finite number") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{key} must be between {low:g} and {high:g}")
    return value


def parse_lockouts(init) -> dict | None:
    if init is None:
        return None
    if not isinstance(init, Mapping):
        raise ValueError("init must be a mapping")
    known = {
        "tx_lo", "rx_lo", "sample_rate", "tx_rf_bandwidth", "rx_rf_bandwidth",
        "rx_buffer_size", "tx_enabled_channels", "rx_enabled_channels", "serial", "ip",
        "usb", "usb_port", "ykush", "ykush_cmd", "multi_tap",
        "poe", "POE", "poe_port", "poe_switch", "poe_user",
    }
    for key in init:
        if not isinstance(key, str):
            raise ValueError("init keys must be strings")
        lower = key.lower()
        if lower in LOCKOUT_KEYS and key != lower:
            raise ValueError(f"lockout key {key!r} must be lowercase ({lower})")
        if (key not in LOCKOUT_KEYS and key not in known
                and (lower.startswith(("tx_", "rx_")) or any(word in lower for word in (
                    "freq", "gain", "lockout", "admin", "allow", "override", "enable", "disable",
                    "only", "rate", "sample", "max", "min", "nabled", "scale", "dds", "cyclic",
                    "buffer", "park", "tx_lo", "loopback", "channel")))):
            raise ValueError(f"unknown lockout-like init key: {key!r}")
    cfg = {key: init[key] for key in LOCKOUT_KEYS if key in init}
    if not cfg:
        return None
    for key, value in list(cfg.items()):
        if key in _BOOL_KEYS:
            if type(value) is not bool:
                raise ValueError(f"{key} must be a bool")
        elif key in _FREQ_KEYS:
            cfg[key] = _config_number(key, value, 70e6, 6e9)
        elif key == "max_sample_rate":
            cfg[key] = _config_number(key, value, 521e3, 61.44e6)
        elif key == "tx_max_gain_db":
            cfg[key] = _config_number(key, value, -89.75, 0)
        elif key == "tx_max_dds_scale":
            cfg[key] = _config_number(key, value, 0, 1)
        elif key == "tx_max_buffer_samples":
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} must be a positive integer")
        elif key == "tx_allowed_channels":
            if (not isinstance(value, list) or not value
                    or any(type(c) is not int or c not in (0, 1) for c in value)
                    or len(set(value)) != len(value)):
                raise ValueError(f"{key} must be a nonempty list of distinct channels (0 or 1)")
            cfg[key] = list(value)
        elif key == "lockout_allow_methods":
            if (not isinstance(value, list) or any(
                    not isinstance(name, str) or not re.fullmatch(r"(?:set|call)_[A-Za-z0-9_]+", name)
                    for name in value)):
                raise ValueError(f"{key} must be a list of set_ or call_ method names")
            cfg[key] = list(value)
    for side in ("tx", "rx"):
        lo, hi = f"{side}_min_freq", f"{side}_max_freq"
        if (lo in cfg) != (hi in cfg):
            raise ValueError(f"{lo} and {hi} must be provided together")
        if lo in cfg:
            if cfg[lo] >= cfg[hi]:
                raise ValueError(f"{lo} must be less than {hi}")
            initial = f"{side}_lo"
            if initial in init:
                value = _config_number(initial, init[initial], 70e6, 6e9)
                if not cfg[lo] <= value <= cfg[hi]:
                    raise ValueError(f"init {initial} is outside the configured {side.upper()} range")
    tx_keys = LOCKOUT_KEYS - {"rx_min_freq", "rx_max_freq", "admin_group_override", "lockout_allow_methods"}
    cfg.setdefault("allow_rf_loopback", not bool(tx_keys.intersection(init)))
    cfg.setdefault("admin_group_override", False)
    cfg.setdefault("tx_enabled", True)
    cfg.setdefault("allow_dds", True)
    cfg.setdefault("allow_cyclic_tx", True)
    cfg.setdefault("lockout_allow_methods", [])
    cfg.setdefault("park_tx_lo", (cfg["tx_min_freq"] + cfg["tx_max_freq"]) / 2 if "tx_min_freq" in cfg else 915e6)
    if "tx_min_freq" in cfg and not cfg["tx_min_freq"] <= cfg["park_tx_lo"] <= cfg["tx_max_freq"]:
        raise ValueError("park_tx_lo is outside the configured TX range")
    return cfg


def _rpc_number(value, label):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise RemoteRFPolicyError(f"{label} must be a finite number.")
    try:
        number = float(value)
    except OverflowError:
        raise RemoteRFPolicyError(f"{label} must be a finite number.") from None
    if not math.isfinite(number):
        raise RemoteRFPolicyError(f"{label} must be a finite number.")
    return int(value) if isinstance(value, Integral) else number


def _rpc_integer(value, label):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Integral):
        raise RemoteRFPolicyError(f"{label} must be an integer.")
    return int(value)


def _mhz(value):
    return f"{value / 1e6:g}"


def _freq_number(value):
    if isinstance(value, (bool, np.bool_)):
        return None
    if isinstance(value, Integral):
        return int(value)
    if not isinstance(value, (Real, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


_FALSE_STRINGS = frozenset({"false", "off"})
_UNKNOWN = object()


def _scalar_nonzero(value) -> bool:
    """True when a scalar DDS value is nonzero/enabled, or conservatively when unparseable."""
    if isinstance(value, np.generic):
        value = value.item()
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, Integral):
        return int(value) != 0
    if isinstance(value, Real):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return True
        return math.isnan(number) or number != 0.0
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return True
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return True
        if text in _FALSE_STRINGS:
            return False
        try:
            number = float(text)
        except (TypeError, ValueError, OverflowError):
            return True
        return math.isnan(number) or number != 0.0
    return True


def _any_nonzero(value) -> bool:
    if isinstance(value, np.ndarray):
        value = value.tolist()
    elif hasattr(value, "tolist") and not isinstance(value, (str, bytes, bytearray)):
        try:
            value = value.tolist()
        except Exception:
            return True
    if isinstance(value, (list, tuple)):
        return any(_any_nonzero(v) for v in value)
    return _scalar_nonzero(value)


def _dds_channel_flags(value):
    if value is _UNKNOWN:
        return None
    if value is None:
        return []
    if isinstance(value, np.ndarray):
        value = value.tolist()
    elif hasattr(value, "tolist") and not isinstance(value, (str, bytes, bytearray)):
        try:
            value = value.tolist()
        except Exception:
            return None
    if isinstance(value, (list, tuple)):
        return [_any_nonzero(v) for v in value]
    return None if _scalar_nonzero(value) else []


def _dds_pair_active(enabled, scales) -> bool:
    en_flags = _dds_channel_flags(enabled)
    sc_flags = _dds_channel_flags(scales)
    if en_flags is None and sc_flags is None:
        return True
    if en_flags is None:
        return any(sc_flags)
    if sc_flags is None:
        return any(en_flags)
    return any(en and sc for en, sc in zip(en_flags, sc_flags))


def _is_rf_loopback(value) -> bool:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bool):
        return False
    if isinstance(value, Integral):
        return int(value) == 2
    if isinstance(value, Real):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return False
        return math.isfinite(number) and number == 2.0
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        try:
            number = float(text)
        except (TypeError, ValueError, OverflowError):
            return False
        return math.isfinite(number) and number == 2.0
    return False


def _normalize_channels(value):
    """Restore channel lists after numeric lists cross the legacy RPC codec."""
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list):
        return value

    channels = []
    for channel in value:
        if not isinstance(channel, str):
            integer = int(channel)
            if channel == integer:
                channel = integer
        channels.append(channel)
    return channels


# == PLUTO Connection Logic ==

def connect_pluto(*, serial: str, ip: str = ""):
    serial = (serial or "").strip()
    ip = (ip or "").strip()
    if ip:
        # An always-powered Pluto on the network (its static ipaddr_eth).
        try:
            dev = adi.Pluto(f"ip:{ip}")
            found = str(dev._ctx.attrs.get("hw_serial", "")).strip()
            if serial and found.lower() != serial.lower():
                print(f"ip:{ip} is Pluto {found}, expected {serial}")
                return None
            from remoterf_device_core.utils.ansi_codes import Sty, printf

            printf(f"Connected to Pluto via ip:{ip}", Sty.GRAY)
            return dev
        except Exception as e:
            print(f"Failed to connect to Pluto ip:{ip}: {e}")
            return None
    if not serial:
        print("A Pluto serial must be provided")
        return None

    try:
        out = subprocess.check_output(
            ["iio_info", "-s"],
            text=True,
            stderr=subprocess.STDOUT
        )

        usb = None
        for line in out.splitlines():
            if (f"serial={serial}" in line) or (f"hw_serial={serial}" in line):
                m = re.search(r"\[usb:([^\]]+)\]", line)
                if m:
                    usb = m.group(1).strip()
                    break

        if not usb:
            print(f"No device found with serial {serial}")
            return None

        dev = adi.Pluto(f"usb:{usb}")
        from remoterf_device_core.utils.ansi_codes import Sty, printf

        printf(f"Connected to Pluto serial={serial} via usb:{usb}", Sty.GRAY)
        return dev

    except Exception as e:
        print(f"Failed to connect to Pluto serial={serial}: {e}")
        return None

# ========================= PLUTO Schema =========================

@idl_register("adalm_pluto")
class PlutoSchema(DeviceSchema):

    device_type = "adalm_pluto"
    client_class = "Pluto"
    driver_version = "0.0.5"
    _fir_rate = None  # the rate whose pyadi FIR this schema last loaded
    _freq_state = None
    _freq_power_epoch = None
    _freq_power_off_failed = False
    _custom_filter_loaded = False
    _initial_rx_channels = None
    _initial_tx_channels = None
    lockout_bypass: bool = False
    _lockouts = None
    _broken = False
    _tx_flag = False
    _rf_loopback = False

    def _device_power_epoch(self):
        if isinstance(self.device, PoePluto):
            return vars(self.device).get("_power_epoch")
        return None

    def _power_off_failed(self):
        return self.device is not None and bool(vars(self.device).get("_power_off_failed"))

    def _sync_freq_power_state(self):
        if not isinstance(self.device, PoePluto):
            return
        state = vars(self.device)
        epoch = state.get("_power_epoch")
        power_off_failed = bool(state.get("_power_off_failed"))
        power_cut = (
            (epoch is not None and self._freq_power_epoch is not None and epoch != self._freq_power_epoch)
            or (self._freq_power_off_failed and not power_off_failed)
        )
        if power_cut:
            self._freq_state = None
            self._tx_flag = False
            self._rf_loopback = False
            self._freq_power_off_failed = False
        self._freq_power_epoch = epoch
        if power_off_failed:
            self._freq_power_off_failed = True

    def _freq_snapshot(self):
        self._sync_freq_power_state()
        state = self._freq_state
        if state is None:
            return {
                "rx_hz": None,
                "tx_hz": None,
                "sample_rate": None,
                "rx_rf_bandwidth": None,
                "tx_rf_bandwidth": None,
                "tx_buffer_active": False,
                "dds_active": False,
                "rf_loopback": False,
                "updated_at": None,
            }
        return dict(state)

    def _record_freq(self, field, value):
        number = _freq_number(value)
        if number is None:
            return
        state = self._freq_snapshot()
        state[field] = number
        state["updated_at"] = time.time()
        self._freq_state = state

    def _record_tx(self, *, buffer=None, dds=None, loopback=None):
        self._sync_freq_power_state()
        current = self._freq_state
        if current is None and not buffer and not dds and not loopback:
            return
        state = self._freq_snapshot()
        changed = False
        if buffer is not None:
            flag = bool(buffer)
            if flag or state["tx_buffer_active"] != flag:
                state["tx_buffer_active"] = flag
                changed = True
        if dds is not None:
            flag = bool(dds)
            if flag or state["dds_active"] != flag:
                state["dds_active"] = flag
                changed = True
        if loopback is not None:
            flag = bool(loopback)
            if flag or state["rf_loopback"] != flag:
                state["rf_loopback"] = flag
                changed = True
        if changed:
            state["updated_at"] = time.time()
            self._freq_state = state

    def clear_active_frequencies(self):
        self._freq_state = None
        self._freq_power_epoch = self._device_power_epoch()
        self._freq_power_off_failed = self._power_off_failed()

    def active_frequencies(self) -> dict:
        """Last holder-configured frequencies and TX state without touching hardware."""
        self._sync_freq_power_state()
        state = self._freq_state
        if state is None:
            return {
                "rx_hz": None,
                "tx_hz": None,
                "tx_active": False,
                "sample_rate": None,
                "rx_rf_bandwidth": None,
                "tx_rf_bandwidth": None,
                "updated_at": None,
            }
        return {
            "rx_hz": state["rx_hz"],
            "tx_hz": state["tx_hz"],
            "tx_active": bool(state["tx_buffer_active"] or state["dds_active"] or state["rf_loopback"]),
            "sample_rate": state["sample_rate"],
            "rx_rf_bandwidth": state["rx_rf_bandwidth"],
            "tx_rf_bandwidth": state["tx_rf_bandwidth"],
            "updated_at": state["updated_at"],
        }

    def bind(self, device) -> None:
        super().bind(device)
        self.clear_active_frequencies()
        self._fir_rate = None
        self._custom_filter_loaded = False
        self._initial_rx_channels = None
        self._initial_tx_channels = None
        if device is not None and not isinstance(device, PoePluto):
            rx_channels = getattr(device, "rx_enabled_channels", None)
            if rx_channels is not None:
                self._initial_rx_channels = list(rx_channels)
            tx_channels = getattr(device, "tx_enabled_channels", None)
            if tx_channels is not None:
                self._initial_tx_channels = list(tx_channels)

    def close(self) -> None:
        self._fir_rate = None
        self._custom_filter_loaded = False
        self._initial_rx_channels = None
        self._initial_tx_channels = None
        dev = self.device
        self.device = None
        self.clear_active_frequencies()
        if dev is not None:
            close = getattr(dev, "close", None)
            if callable(close):
                close()

    close_native_device = close

    def _lockout_lock(self):
        # Device managers also hold io_lock across the entire dispatch/hook.
        return (vars(self.device).get("_lock") if self.device is not None else None) or nullcontext()

    def _lockout_handle(self):
        return vars(self.device).get("_pluto", self.device)

    def _lockout_on(self):
        if self.device is None:
            return False
        state = vars(self.device)
        if "_pluto" in state:
            return state["_pluto"] is not None and not state.get("fault")
        return self.device is not None

    def _lockout_cut_power(self):
        power_down = getattr(type(self.device), "power_down", None)
        if power_down is None:
            return False
        try:
            power_down(self.device)
            self.clear_active_frequencies()
            self._tx_flag = self._rf_loopback = False
            return True
        except Exception:
            self._sync_freq_power_state()
            _log.exception("Pluto lockout power-off failed")
            return False

    def configure_lockouts(self, cfg: dict | None) -> None:
        self._lockouts = dict(cfg) if cfg is not None else None
        self._tx_flag = self._rf_loopback = False
        self._broken = False
        self.lockout_settle()

    def _lockout_hygiene(self, park=False):
        self._sync_freq_power_state()
        if self._lockouts is None or not self._lockout_on():
            return
        cfg = self._lockouts
        try:
            radio = self._lockout_handle()
            try:
                try:
                    radio.disable_dds()
                finally:
                    if hasattr(radio, "dds_scales"):
                        radio.dds_scales = [0] * len(radio.dds_scales)
            finally:
                try:
                    radio.tx_destroy_buffer()
                finally:
                    try:
                        if hasattr(radio, "tx_cyclic_buffer"):
                            radio.tx_cyclic_buffer = False
                    finally:
                        radio.loopback = 0
            self._tx_flag = self._rf_loopback = False
            self._record_tx(buffer=False, dds=False, loopback=False)
            lo = float(radio.tx_lo)
            if (park or not cfg["tx_enabled"] or ("tx_min_freq" in cfg
                    and not cfg["tx_min_freq"] <= lo <= cfg["tx_max_freq"])):
                parked = int(round(cfg["park_tx_lo"]))
                radio.tx_lo = parked
                if self._freq_state is not None and self._freq_state["tx_hz"] is not None:
                    self._record_freq("tx_hz", parked)
            cap = cfg.get("tx_max_gain_db", 0) if cfg["tx_enabled"] else -89.75
            for name in ("tx_hardwaregain_chan0", "tx_hardwaregain_chan1"):
                if name == "tx_hardwaregain_chan1" and not hasattr(radio, name):
                    continue
                gain = float(getattr(radio, name))
                if park or not -89.75 <= gain <= cap:
                    setattr(radio, name, -89.75)
            if "tx_allowed_channels" in cfg:
                allowed = cfg["tx_allowed_channels"]
                channels = [c for c in radio.tx_enabled_channels if c in allowed]
                radio.tx_enabled_channels = channels or list(allowed)
            self._broken = False
        except Exception as exc:
            self._broken = not self._lockout_cut_power()
            raise RemoteRFPolicyError(_BROKEN_MESSAGE) from exc

    def lockout_settle(self) -> None:
        if self._lockouts is not None:
            with self._lockout_lock():
                self._lockout_hygiene()

    def lockout_after_hook(self, kind: str, failed: bool = False) -> None:
        if self._lockouts is None:
            return
        with self._lockout_lock():
            self._tx_flag = self._rf_loopback = False
            self._record_tx(buffer=False, dds=False, loopback=False)
            if kind == "reset" and failed:
                self._broken = not self._lockout_cut_power()
                return
            self._lockout_hygiene(park=kind == "reset")

    def _lockout_normalize(self, name, args):
        args = dict(args)
        if name in _NUMBER_SETTERS:
            args["value"] = _rpc_number(args["value"], "Value")
        elif name in _BOOL_SETTERS:
            if type(args["value"]) is not bool:
                raise RemoteRFPolicyError("Value must be a bool.")
        elif name in {"set_loopback", "set_rx_buffer_size"}:
            args["value"] = _rpc_integer(args["value"], "Value")
        elif name in _CHANNEL_SETTERS or name in _DDS_VECTORS:
            value = args["value"]
            if not isinstance(value, (list, np.ndarray)):
                raise RemoteRFPolicyError("Value must be a list or array.")
            if isinstance(value, np.ndarray) and value.ndim != 1:
                raise RemoteRFPolicyError("Value must be a one-dimensional list or array.")
            normalize = _rpc_integer if name in _CHANNEL_SETTERS else _rpc_number
            args["value"] = [
                int(v) if name == "set_dds_enabled" and isinstance(v, (bool, np.bool_))
                else normalize(v, "Channel" if name in _CHANNEL_SETTERS else "DDS value")
                for v in value
            ]
        elif name in _DDS_CALLS:
            for key, value in args.items():
                args[key] = _rpc_integer(value, "Channel") if key == "channel" else _rpc_number(value, "DDS value")
        elif name == "call_tx":
            try:
                value = np.asarray(args["value"])
                if (value.dtype.kind not in "iufc" or value.ndim not in (1, 2)
                        or not np.isfinite(value).all()):
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                raise RemoteRFPolicyError("TX samples must be finite numbers.") from None
            args["value"] = list(value) if value.ndim == 2 else value
        return args

    def _lockout_refuse(self, message, bypass):
        if bypass:
            _log.warning("lockout bypassed: %s", message)
        else:
            raise RemoteRFPolicyError(message)

    def _lockout_channel(self, channel, bypass):
        allowed = self._lockouts.get("tx_allowed_channels")
        if allowed is not None and channel not in allowed:
            self._lockout_refuse(
                f"TX channel {channel} is not allowed on this device (allowed: {allowed}).", bypass)

    def _lockout_gain(self, gain, bypass):
        cfg = self._lockouts
        cap = cfg.get("tx_max_gain_db", 0) if cfg["tx_enabled"] else -89.75
        if not -89.75 <= gain <= cap:
            self._lockout_refuse(f"TX gain {gain:g} dB is above this device's limit of {cap:g} dB.", bypass)

    def _lockout_band(self, bypass, lo=None):
        cfg = self._lockouts
        if "tx_min_freq" not in cfg and "max_sample_rate" not in cfg:
            return
        radio = self._lockout_handle()
        lo = float(radio.tx_lo) if lo is None else lo
        # sample_rate reads only RX; the TX FPGA interpolation rate can differ.
        rates = [float(radio._get_iio_attr("voltage0", "sampling_frequency", output)) for output in (False, True)]
        if not all(math.isfinite(rate) and rate > 0 for rate in rates) or not math.isfinite(lo):
            self._lockout_refuse("TX refused: live LO and sample rates must be finite numbers.", bypass)
            return
        rate = max(rates)
        if "max_sample_rate" in cfg and rate > cfg["max_sample_rate"]:
            self._lockout_refuse(
                f"Sample rate {_mhz(rate)} MS/s is above this device's limit of {_mhz(cfg['max_sample_rate'])} MS/s.", bypass)
        if "tx_min_freq" not in cfg:
            return
        a, b = lo - rate / 2, lo + rate / 2
        if not cfg["tx_min_freq"] <= a <= b <= cfg["tx_max_freq"]:
            self._lockout_refuse(
                f"TX refused: the signal would occupy {_mhz(a)}–{_mhz(b)} MHz (tx_lo ± sample_rate/2), "
                f"outside this device's TX range {_mhz(cfg['tx_min_freq'])}–{_mhz(cfg['tx_max_freq'])} MHz.", bypass)

    def _lockout_checks(self, name, args, bypass):
        cfg = self._lockouts
        value = args.get("value")
        live = self._tx_flag or self._rf_loopback
        start = (name == "call_tx" or name in _DDS_CALLS
                 or (name in {"set_dds_enabled", "set_dds_scales"} and any(value))
                 or (name == "set_loopback" and value == 2))
        refuse = lambda message: self._lockout_refuse(message, bypass)
        if name == "set_filter":
            if live and "tx_min_freq" in cfg:
                refuse("FIR filter cannot change while this device is transmitting; call tx_destroy_buffer() first.")
            refuse("Loading FIR filter files is disabled on this device.")
        if live and "tx_min_freq" in cfg and name in {"set_sample_rate", "set_tx_int8_filter_en"}:
            label = "Sample rate" if name == "set_sample_rate" else "TX interpolation filter"
            refuse(f"{label} cannot change while this device is transmitting; call tx_destroy_buffer() first.")
        if name in {"set_tx_lo", "set_rx_lo"}:
            side = "tx" if name == "set_tx_lo" else "rx"
            low, high = cfg.get(f"{side}_min_freq"), cfg.get(f"{side}_max_freq")
            if low is not None and not low <= value <= high:
                refuse(f"{side.upper()} LO {_mhz(value)} MHz is outside this device's {side.upper()} range {_mhz(low)}–{_mhz(high)} MHz.")
            if side == "tx" and live:
                self._lockout_band(bypass, value)
        if name in {"set_tx_hardwaregain_chan0", "set_tx_hardwaregain_chan1"}:
            self._lockout_gain(value, bypass)
        if name == "set_loopback":
            if value not in (0, 1, 2):
                refuse("Loopback must be 0 (off), 1 (digital), or 2 (RF).")
            if value == 2 and not cfg["allow_rf_loopback"]:
                refuse("Loopback 2 (RF) is not allowed on this device; use 0 (off) or 1 (digital).")
        if name == "set_tx_cyclic_buffer" and value and not cfg["allow_cyclic_tx"]:
            refuse("Cyclic (continuous) TX is disabled on this device; send finite buffers with tx().")
        if name == "set_sample_rate" and "max_sample_rate" in cfg and value > cfg["max_sample_rate"]:
            refuse(f"Sample rate {_mhz(value)} MS/s is above this device's limit of {_mhz(cfg['max_sample_rate'])} MS/s.")
        if name == "set_tx_enabled_channels":
            for channel in value:
                self._lockout_channel(channel, bypass)
        if name in _DDS_VECTORS:
            for index in range(len(value)):
                self._lockout_channel(index // 4, bypass)
        if name in _DDS_CALLS:
            self._lockout_channel(args.get("channel", 0), bypass)
        scales = ([v for key, v in args.items() if key.startswith("scale")] if name in _DDS_CALLS
                  else value if name == "set_dds_scales" else [])
        if "tx_max_dds_scale" in cfg:
            if start and name in _DDS_CALLS | {"set_dds_enabled"}:
                scales = list(scales) + [float(s) for s in self._lockout_handle().dds_scales]
            for scale in scales:
                if not 0 <= scale <= cfg["tx_max_dds_scale"]:
                    refuse(f"DDS scale {scale:g} is above this device's limit of {cfg['tx_max_dds_scale']:g}.")
        if name == "call_tx" and "tx_max_buffer_samples" in cfg:
            samples = np.asarray(value)
            n = samples.shape[-1]
            if n > cfg["tx_max_buffer_samples"]:
                refuse(f"TX buffer of {n} samples is larger than this device's limit of {cfg['tx_max_buffer_samples']}.")
        if start:
            if not cfg["tx_enabled"]:
                refuse("TX is disabled on this device (receive-only).")
            if name != "call_tx" and name != "set_loopback" and not cfg["allow_dds"]:
                refuse("DDS tones are disabled on this device; transmit samples with tx() instead.")
            radio = self._lockout_handle()
            self._lockout_band(bypass)
            for channel in radio.tx_enabled_channels:
                self._lockout_channel(channel, bypass)
            gains = []
            for attr in ("tx_hardwaregain_chan0", "tx_hardwaregain_chan1"):
                if attr == "tx_hardwaregain_chan1" and not hasattr(radio, attr):
                    continue
                gains.append(float(getattr(radio, attr)))
            gain = next((g for g in gains if not math.isfinite(g)), max(gains))
            self._lockout_gain(gain, bypass)
        return start

    def dispatch(self, method_name, args):
        if self._lockouts is None:
            return super().dispatch(method_name, args)
        with self._lockout_lock():
            bypass = bool(self._lockouts["admin_group_override"] and self.lockout_bypass)
            ex = self._exposed.get(method_name)
            if self._broken:
                try:
                    if (self.device is not None and not self._lockout_on()
                            and method_name not in _STOP_METHODS | {"call_power_down", "call_ip"}):
                        try:
                            self.device.power_up()
                        finally:
                            self._lockout_hygiene()
                    else:
                        self._lockout_hygiene()
                except RemoteRFPolicyError:
                    if method_name not in _STOP_METHODS | {"call_power_down"} and not (ex and ex.kind == "get"):
                        raise
                if (self._broken and method_name not in _STOP_METHODS | {"call_power_down"}
                        and not (ex and ex.kind == "get")):
                    raise RemoteRFPolicyError(_BROKEN_MESSAGE)
            if (not (ex and ex.kind == "get") and method_name not in _LOCKOUT_METHODS
                    and method_name not in self._lockouts["lockout_allow_methods"]):
                self._lockout_refuse(
                    f"{method_name[:1].upper() + method_name[1:]} is not allowed on this device while lockouts are set.", bypass)
            if ex is None or self.device is None:
                return super().dispatch(method_name, args)
            bound = {} if ex.kind == "get" else self._bind_exposed_args(
                ex, args, allow_value_alias=True, allow_first_arg=ex.kind == "set")
            normalized = self._lockout_normalize(method_name, bound)
            if not self._lockout_on() and method_name not in _STOP_METHODS | {"call_power_down", "call_ip"}:
                try:
                    self.device.power_up()
                finally:
                    self._lockout_hygiene()
            if method_name in _STOP_METHODS and not self._lockout_on():
                if self._power_off_failed():
                    power_down = getattr(type(self.device), "power_down", None)
                    if power_down is None:
                        raise RuntimeError("PoE power-off previously failed; cannot stop TX")
                    self._fir_rate = None
                    self._custom_filter_loaded = False
                    try:
                        power_down(self.device)
                    except Exception:
                        self._sync_freq_power_state()
                        raise
                    self.clear_active_frequencies()
                    self._tx_flag = self._rf_loopback = False
                    return
                self._tx_flag = False
                self._record_tx(buffer=False, dds=False)
                return
            start = self._lockout_checks(method_name, normalized, bypass)
            if start:
                self._tx_flag = True
                if method_name == "set_loopback":
                    self._rf_loopback = True
            handle = self._lockout_handle()
            try:
                if method_name in _STOP_METHODS:
                    try:
                        result = ex.fn(self, **normalized)
                    finally:
                        try:
                            self.device.tx_destroy_buffer()
                        finally:
                            self.device.disable_dds()
                    self._tx_flag = False
                    self._record_tx(buffer=False, dds=False)
                    return result
                result = ex.fn(self, **normalized)
                if method_name == "set_loopback":
                    self._rf_loopback = normalized["value"] == 2
                return result
            finally:
                if self._lockout_handle() is not handle:
                    self._lockout_hygiene()

    @staticmethod
    def make_device(**kwargs):
        # init.poe: true -> the Pluto is powered from a PoE switch port and starts off.
        if is_poe(kwargs):
            return poe_pluto_from_init(kwargs)
        return connect_pluto(serial=kwargs.get("serial"), ip=kwargs.get("ip"))

    # region ad9364

    @idl_expose(kind="get")
    def get_filter(self):
        return self.device.filter

    @idl_expose(kind="set")
    def set_filter(self, value):
        self._fir_rate = None
        self._custom_filter_loaded = True
        self.device.filter = value

    @idl_expose(kind="get")
    def get_loopback(self):
        return self.device.loopback

    @idl_expose(kind="set", tag="TX")
    def set_loopback(self, value):
        self.device.loopback = value
        self._record_tx(loopback=_is_rf_loopback(value))

    @idl_expose(kind="get")
    def get_gain_control_mode_chan0(self):
        return self.device.gain_control_mode_chan0

    @idl_expose(kind="set")
    def set_gain_control_mode_chan0(self, value):
        self.device.gain_control_mode_chan0 = value

    @idl_expose(kind="get")
    def get_rx_hardwaregain_chan0(self):
        """RX hardware gain (dB). pyadi-iio attribute: rx_hardwaregain_chan0"""
        return self.device.rx_hardwaregain_chan0

    @idl_expose(kind="set")
    def set_rx_hardwaregain_chan0(self, value):
        """RX hardware gain (dB). pyadi-iio attribute: rx_hardwaregain_chan0"""
        self.device.rx_hardwaregain_chan0 = value

    @idl_expose(kind="get")
    def get_tx_hardwaregain_chan0(self):
        return self.device.tx_hardwaregain_chan0

    @idl_expose(kind="set", tag="TX")
    def set_tx_hardwaregain_chan0(self, value):
        self.device.tx_hardwaregain_chan0 = value

    @idl_expose(kind="get")
    def get_rx_rf_bandwidth(self):
        return self.device.rx_rf_bandwidth

    @idl_expose(kind="set")
    def set_rx_rf_bandwidth(self, value):
        self.device.rx_rf_bandwidth = value
        self._record_freq("rx_rf_bandwidth", value)

    @idl_expose(kind="get")
    def get_tx_rf_bandwidth(self):
        return self.device.tx_rf_bandwidth

    @idl_expose(kind="set", tag="TX")
    def set_tx_rf_bandwidth(self, value):
        self.device.tx_rf_bandwidth = value
        self._record_freq("tx_rf_bandwidth", value)

    @idl_expose(kind="get")
    def get_sample_rate(self):
        return self.device.sample_rate

    @idl_expose(kind="set")
    def set_sample_rate(self, value):
        # pyadi reloads its FIR and re-runs the AD9361 rate chain (~0.8 s) even
        # for an unchanged rate: skip only what our own last set left in place.
        if value == self._fir_rate:
            try:
                if (
                    self.device.sample_rate == value
                    and self.device._get_iio_attr("out", "voltage_filter_fir_en", False) == 1
                ):
                    self._record_freq("sample_rate", value)
                    return
            except Exception:
                pass
        self._fir_rate = None
        self.device.sample_rate = value
        self._fir_rate = value
        self._custom_filter_loaded = False
        self._record_freq("sample_rate", value)

    @idl_expose(kind="get")
    def get_rx_lo(self):
        return self.device.rx_lo

    @idl_expose(kind="set")
    def set_rx_lo(self, value):
        self.device.rx_lo = value
        self._record_freq("rx_hz", value)

    @idl_expose(kind="get")
    def get_tx_lo(self):
        return self.device.tx_lo

    @idl_expose(kind="set", tag="TX")
    def set_tx_lo(self, value):
        self.device.tx_lo = value
        self._record_freq("tx_hz", value)

    @idl_expose(kind="get")
    def get_tx_cyclic_buffer(self):
        return self.device.tx_cyclic_buffer

    @idl_expose(kind="set", tag="TX")
    def set_tx_cyclic_buffer(self, value):
        self.device.tx_cyclic_buffer = value

    # region RX/TX

    @idl_expose(kind="call")
    def call_rx(self):
        return self.device.rx()

    @idl_expose(kind="get")
    def get_rx_buffer_size(self):
        return self.device.rx_buffer_size

    @idl_expose(kind="set")
    def set_rx_buffer_size(self, value):
        # pyadi reads rx_buffer_size only when it (re)creates the RX buffer.
        if value != self.device.rx_buffer_size:
            self.device.rx_destroy_buffer()
        self.device.rx_buffer_size = value

    @idl_expose(kind="call")
    def call_rx_destroy_buffer(self):
        self.device.rx_destroy_buffer()

    @idl_expose(kind="call", tag="TX")
    def call_tx(self, value):
        self._sync_freq_power_state()
        radio = self._lockout_handle() if self.device is not None else None
        if radio is None:
            had_txbuf = False
        elif hasattr(radio, "_txbuf"):
            had_txbuf = bool(radio._txbuf)
        else:
            had_txbuf = bool(self._freq_state and self._freq_state["tx_buffer_active"])
        try:
            self.device.tx(value)
        except Exception:
            self._sync_freq_power_state()
            radio_after = self._lockout_handle() if self.device is not None else None
            if radio_after is not None:
                buf = bool(radio_after._txbuf) if hasattr(radio_after, "_txbuf") else None
                had_dds = bool(self._freq_state and self._freq_state["dds_active"])
                dds = self._dds_active_from_device() if had_dds else None
                self._record_tx(buffer=buf, dds=dds)
            raise
        self._sync_freq_power_state()
        radio_after = self._lockout_handle() if self.device is not None else None
        has_txbuf = (
            radio_after is None
            or not hasattr(radio_after, "_txbuf")
            or bool(radio_after._txbuf)
        )
        txbuf_created = not had_txbuf and has_txbuf
        if txbuf_created:
            dds = False
        elif (
            radio_after is not None
            and bool(self._freq_state and self._freq_state["dds_active"])
            and (not has_txbuf or not getattr(radio_after, "tx_enabled_channels", (0,)))
        ):
            dds = self._dds_active_from_device()
        else:
            dds = None
        self._record_tx(buffer=has_txbuf, dds=dds)

    @idl_expose(kind="call", tag="TX")
    def call_tx_destroy_buffer(self):
        self.device.tx_destroy_buffer()
        self._record_tx(buffer=False)

    def _stop_pluto_tx(self, radio, attempt):
        attempt(radio.disable_dds)

        def zero_dds_scales():
            if hasattr(radio, "dds_scales"):
                radio.dds_scales = [0] * len(radio.dds_scales)

        attempt(zero_dds_scales)
        attempt(radio.tx_destroy_buffer)

        def clear_tx_cyclic():
            if hasattr(radio, "tx_cyclic_buffer"):
                radio.tx_cyclic_buffer = False

        attempt(clear_tx_cyclic)
        attempt(lambda: setattr(radio, "loopback", 0))

    def _reset_fpga_filters(self, radio, attempt):
        def reset_rx_dec8():
            if hasattr(radio, "rx_dec8_filter_en"):
                radio.rx_dec8_filter_en = False

        def reset_tx_int8():
            if hasattr(radio, "tx_int8_filter_en"):
                radio.tx_int8_filter_en = False

        attempt(reset_rx_dec8)
        attempt(reset_tx_int8)

    def reset_for_next_holder(self):
        """A reservation ended: a PoE Pluto powers off. Otherwise stop its cyclic
        transmission, clear tx_cyclic_buffer, and drop its RX buffer so the next holder's rx_buffer_size takes effect."""
        self._fir_rate = None
        self.clear_active_frequencies()
        pending = None
        if self._lockouts is not None:
            with self._lockout_lock():
                try:
                    self._lockout_hygiene(park=True)
                except RemoteRFPolicyError as exc:
                    pending = exc
        if isinstance(self.device, PoePluto):
            self._custom_filter_loaded = False
            if pending is not None:
                raise pending
            errors = []

            def attempt(step):
                try:
                    step()
                except Exception as exc:
                    # Drop traceback frames so no reference keeps adi.Pluto alive into power_down().
                    stack = [exc]
                    seen = set()
                    while stack:
                        item = stack.pop()
                        if item is None or id(item) in seen:
                            continue
                        seen.add(id(item))
                        item.with_traceback(None)
                        stack.append(item.__cause__)
                        stack.append(item.__context__)
                    errors.append(exc)

            if self._lockout_on():
                self._stop_pluto_tx(self._lockout_handle(), attempt)
            attempt(self.device.power_down)
            if errors:
                raise errors[0]
        elif self.device is not None and self._lockout_on():
            errors = []

            def attempt(step):
                try:
                    step()
                except Exception as exc:
                    errors.append(exc)

            radio = self._lockout_handle()
            self._stop_pluto_tx(radio, attempt)
            attempt(radio.rx_destroy_buffer)
            self._reset_fpga_filters(radio, attempt)
            if self._custom_filter_loaded:
                def reset_fir():
                    radio.sample_rate = int(radio.sample_rate)
                    self._custom_filter_loaded = False

                attempt(reset_fir)

            def reset_output_type():
                if hasattr(radio, "rx_output_type"):
                    radio.rx_output_type = "raw"

            def reset_annotated():
                if hasattr(radio, "rx_annotated"):
                    radio.rx_annotated = False

            attempt(reset_output_type)
            attempt(reset_annotated)
            if self._initial_rx_channels is not None:
                rx_channels = list(self._initial_rx_channels)
                attempt(lambda: setattr(radio, "rx_enabled_channels", rx_channels))
            if self._initial_tx_channels is not None:
                tx_channels = list(self._initial_tx_channels)
                if self._lockouts is not None and "tx_allowed_channels" in self._lockouts:
                    allowed = self._lockouts["tx_allowed_channels"]
                    tx_channels = [c for c in tx_channels if c in allowed] or list(allowed)
                attempt(lambda: setattr(radio, "tx_enabled_channels", tx_channels))
            if pending is not None:
                raise pending
            if errors:
                raise errors[0]
        elif pending is not None:
            raise pending

    def prepare_for_holder(self):
        """A reservation started: a PoE Pluto boots now, not at the holder's first command."""
        if isinstance(self.device, PoePluto):
            try:
                self.device.power_up()
            finally:
                self._sync_freq_power_state()

    @idl_expose(kind="call", tag="TX")
    def call_disable_dds(self):
        """Disable the internal DDS tone generator."""
        self.device.disable_dds()
        self._record_tx(dds=False)

    # region power (PoE Plutos; a no-op on any other Pluto)

    @idl_expose(kind="call")
    def call_power_up(self):
        """PoE Plutos: power on and wait for boot (any command does this when needed). No-op on other Plutos."""
        if isinstance(self.device, PoePluto):
            try:
                self.device.power_up()
            finally:
                self._sync_freq_power_state()

    @idl_expose(kind="call")
    def call_power_down(self):
        """PoE Plutos: cut power; the next command powers it back up. No-op on other Plutos."""
        self._fir_rate = None
        if isinstance(self.device, PoePluto):
            self._custom_filter_loaded = False
            try:
                self.device.power_down()
            except Exception:
                self._sync_freq_power_state()
                raise
            self.clear_active_frequencies()
            self._tx_flag = self._rf_loopback = False

    @idl_expose(kind="call")
    def call_power_cycle(self):
        """PoE Plutos: power off, then on, and wait for boot. No-op on other Plutos."""
        self._fir_rate = None
        if isinstance(self.device, PoePluto):
            self._custom_filter_loaded = False
            try:
                self.device.power_cycle()
            except Exception:
                self._sync_freq_power_state()
                state = vars(self.device)
                if not state.get("_power_off_failed") and state.get("_pluto") is None:
                    self.clear_active_frequencies()
                    self._tx_flag = self._rf_loopback = False
                raise
            self.clear_active_frequencies()
            self._tx_flag = self._rf_loopback = False

    # endregion

    # region DDS

    def _dds_active_from_device(self) -> bool:
        radio = self._lockout_handle()

        def read(name):
            try:
                return getattr(radio, name)
            except Exception:
                return _UNKNOWN

        return _dds_pair_active(read("dds_enabled"), read("dds_scales"))

    def _reconcile_dds_from_device(self):
        self._sync_freq_power_state()
        if self.device is None or self._lockout_handle() is None:
            return
        self._record_tx(dds=self._dds_active_from_device())

    @idl_expose(kind="call", tag="TX")
    def call_dds_single_tone(self, frequency, scale, channel=0):
        """Generate one complex DDS tone on the selected TX channel."""
        try:
            self.device.dds_single_tone(frequency, scale, channel)
        except Exception:
            self._reconcile_dds_from_device()
            raise
        self._record_tx(dds=_any_nonzero(scale))

    @idl_expose(kind="call", tag="TX")
    def call_dds_dual_tone(
        self,
        frequency1,
        scale1,
        frequency2,
        scale2,
        channel=0,
    ):
        """Generate two complex DDS tones on the selected TX channel."""
        try:
            self.device.dds_dual_tone(
                frequency1,
                scale1,
                frequency2,
                scale2,
                channel,
            )
        except Exception:
            self._reconcile_dds_from_device()
            raise
        self._record_tx(dds=_any_nonzero((scale1, scale2)))

    @idl_expose(kind="get")
    def get_dds_frequencies(self):
        return self.device.dds_frequencies

    @idl_expose(kind="set", tag="TX")
    def set_dds_frequencies(self, value):
        self.device.dds_frequencies = value

    @idl_expose(kind="get")
    def get_dds_scales(self):
        return self.device.dds_scales

    @idl_expose(kind="set", tag="TX")
    def set_dds_scales(self, value):
        try:
            self.device.dds_scales = value
        finally:
            self._reconcile_dds_from_device()

    @idl_expose(kind="get")
    def get_dds_phases(self):
        return self.device.dds_phases

    @idl_expose(kind="set", tag="TX")
    def set_dds_phases(self, value):
        self.device.dds_phases = value

    @idl_expose(kind="get")
    def get_dds_enabled(self):
        return self.device.dds_enabled

    @idl_expose(kind="set", tag="TX")
    def set_dds_enabled(self, value):
        try:
            self.device.dds_enabled = value
        finally:
            self._reconcile_dds_from_device()

    # endregion

    # region channel configuration

    @idl_expose(kind="get")
    def get_rx_enabled_channels(self):
        return self.device.rx_enabled_channels

    @idl_expose(kind="set")
    def set_rx_enabled_channels(self, value):
        self.device.rx_enabled_channels = _normalize_channels(value)

    @idl_expose(kind="get")
    def get_tx_enabled_channels(self):
        return self.device.tx_enabled_channels

    @idl_expose(kind="set", tag="TX")
    def set_tx_enabled_channels(self, value):
        self.device.tx_enabled_channels = _normalize_channels(value)

    @idl_expose(kind="get")
    def get_rx_annotated(self):
        return self.device.rx_annotated

    @idl_expose(kind="set")
    def set_rx_annotated(self, value):
        self.device.rx_annotated = value

    @idl_expose(kind="get")
    def get_rx_output_type(self):
        return self.device.rx_output_type

    @idl_expose(kind="set")
    def set_rx_output_type(self, value):
        self.device.rx_output_type = value

    @idl_expose(kind="get")
    def get_rx_channel_names(self):
        return self.device.rx_channel_names

    @idl_expose(kind="get")
    def get_tx_channel_names(self):
        return self.device.tx_channel_names

    # endregion

    # endregion

    # region ip / repr

    @idl_expose(kind="call")
    def call_ip(self):
        """Return a connection identifier for this device (ping / verify)."""
        try:
            return str(self.device.uri)
        except AttributeError:
            return str(self.device._uri_auto)

    @idl_expose(kind="get")
    def get_repr(self):
        """Return repr() of the underlying device object."""
        return repr(self.device)

    # endregion

    # region _dec_int_fpga_filter

    @idl_expose(kind="get")
    def get_rx_dec8_filter_en(self):
        """rx_dec8_filter_en: Enable decimate-by-8 filter in FPGA"""
        return bool(self.device.rx_dec8_filter_en)

    @idl_expose(kind="set")
    def set_rx_dec8_filter_en(self, value):
        """rx_dec8_filter_en: Enable decimate-by-8 filter in FPGA"""
        self.device.rx_dec8_filter_en = value

    @idl_expose(kind="get")
    def get_tx_int8_filter_en(self):
        """tx_int8_filter_en: Enable interpolate-by-8 filter in FPGA"""
        return bool(self.device.tx_int8_filter_en)

    @idl_expose(kind="set", tag="TX")
    def set_tx_int8_filter_en(self, value):
        """tx_int8_filter_en: Enable interpolate-by-8 filter in FPGA"""
        self.device.tx_int8_filter_en = value

    @idl_expose(kind="get")
    def get_rates(self):
        """Get decimation/interpolation rates from the FPGA filter block."""
        return self.device._get_rates(self.device._rxadc, False)

    @idl_expose(kind="get")
    def get_temperature(self):
        """AD9361 die temperature in degrees C."""
        return float(self.device._get_iio_attr("temp0", "input", False)) / 1000

    @idl_expose(kind="get")
    def get_temp(self):
        """Alias for temperature: AD9361 die temperature in degrees C."""
        return self.get_temperature()

    # endregion
