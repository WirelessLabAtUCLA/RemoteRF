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

"""Per-device TX frequency limits from the ``limits`` block of devices.yaml."""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from typing import Any, Optional

# device_type -> the legacy SET property that tunes TX.
_TX_FREQ_PROPS = {"adalm_pluto": "tx_lo", "pluto_plus": "tx_lo"}


def tx_freq_prop(device_type: str) -> Optional[str]:
    dtype = str(device_type or "").strip().lower()
    if dtype.startswith("adalm_pluto_mimo"):
        return "tx_lo"
    return _TX_FREQ_PROPS.get(dtype)


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{value!r} is not a frequency")
    out = float(value)
    if not math.isfinite(out) or out < 0:
        raise ValueError(f"{value!r} is not a frequency")
    return out


def _fmt(hz: float) -> tuple[str, str]:
    if abs(hz) >= 1e9:
        text = f"{hz / 1e9:.6f}".rstrip("0")
        whole, _, frac = text.partition(".")
        return f"{whole}.{frac.ljust(2, '0')}", "GHz"
    return f"{hz / 1e6:.6f}".rstrip("0").rstrip("."), "MHz"


def format_freq(hz: float) -> str:
    return " ".join(_fmt(hz))


def _format_range(low: float, high: float) -> str:
    (lo, lo_unit), (hi, hi_unit) = _fmt(low), _fmt(high)
    if lo_unit == hi_unit:
        return f"{lo}–{hi} {hi_unit}"
    return f"{lo} {lo_unit}–{hi} {hi_unit}"


@dataclass(frozen=True)
class TxFreqRule:
    whitelist: tuple[tuple[float, float], ...] = ()
    blacklist: tuple[tuple[float, float], ...] = ()
    error: str = ""

    def allows(self, freq: float) -> bool:
        if any(lo <= freq <= hi for lo, hi in self.blacklist):
            return False
        return not self.whitelist or any(lo <= freq <= hi for lo, hi in self.whitelist)

    def message(self, freq: float) -> str:
        text = f"Transmitting at {format_freq(freq)} is not allowed by this device's limits."
        for label, ranges in (
            ("Whitelist", self.whitelist),
            ("Blacklist", self.blacklist),
        ):
            if ranges:
                text += (
                    f" {label}: "
                    + ", ".join(_format_range(lo, hi) for lo, hi in ranges)
                    + "."
                )
        return text

    def check(self, value: Any) -> float:
        """Validate a frequency without changing it; raise ValueError when refused."""
        try:
            freq = _number(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"TX frequency {value!r} is not a number; this device's limits refuse it"
            ) from None
        if self.error:
            raise ValueError(self.error)
        if self.allows(freq):
            return freq
        message = self.message(freq)
        raise ValueError(message)


def _parse_ranges(raw: Any, name: str) -> tuple[tuple[float, float], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{name} must be a list of [low, high] ranges")
    out = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"{name} range {item!r} must be [low, high]")
        low, high = _number(item[0]), _number(item[1])
        if low > high:
            raise ValueError(f"{name} range {item!r} has low above high")
        out.append((low, high))
    return tuple(out)


def parse_tx_freq(raw: Any) -> TxFreqRule:
    if not isinstance(raw, dict):
        raise ValueError("tx_freq must be a mapping")
    whitelist = _parse_ranges(raw.get("whitelist"), "whitelist")
    blacklist = _parse_ranges(raw.get("blacklist"), "blacklist")
    unknown = set(raw) - {"whitelist", "blacklist", "on_violation"}
    if unknown:
        raise ValueError(f"unknown tx_freq fields: {sorted(unknown)}")
    if raw.get("on_violation", "reject") != "reject":
        raise ValueError("only on_violation: reject is supported")
    if "whitelist" in raw and not whitelist:
        raise ValueError("an explicit whitelist must contain at least one range")
    return TxFreqRule(whitelist, blacklist)


def parse_limits(raw: Any, *, gid: int) -> dict[str, TxFreqRule]:
    """Absent limits are unrestricted; invalid configured limits disable TX."""
    if raw is None:
        return {}
    try:
        if not isinstance(raw, dict) or set(raw) != {"tx_freq"}:
            raise ValueError("limits must contain exactly tx_freq")
        return {"tx_freq": parse_tx_freq(raw["tx_freq"])}
    except (TypeError, ValueError, OverflowError) as exc:
        error = f"TX disabled for device gid={gid}: invalid limits: {exc}"
        print(error)
        return {"tx_freq": TxFreqRule(error=error)}


def tune_request_freq(value):
    """One unambiguous automatic target; manual RF/DSP policies need separate support."""
    if not isinstance(value, dict):
        return _number(value)
    allowed = {"__uhd_type__", "target_freq", "freq", "args", "kwargs"}
    if (
        set(value) - allowed
        or value.get("__uhd_type__", "TuneRequest") != "TuneRequest"
    ):
        raise ValueError("TX limits support automatic TuneRequest targets only")
    candidates = []
    for key in ("target_freq", "freq"):
        if key in value:
            candidates.append(value[key])
    args = value.get("args", [])
    kwargs = value.get("kwargs", {})
    if not isinstance(args, (list, tuple)) or len(args) > 1:
        raise ValueError("TX limits refuse manual/ambiguous TuneRequest arguments")
    if not isinstance(kwargs, dict) or set(kwargs) - {"target_freq"}:
        raise ValueError("TX limits refuse manual/ambiguous TuneRequest keywords")
    candidates.extend(args)
    if "target_freq" in kwargs:
        candidates.append(kwargs["target_freq"])
    if len(candidates) != 1:
        raise ValueError("TX limits require exactly one TuneRequest target")
    return _number(candidates[0])


# The server tunnel owns host policy. Direct clients never install policy.
POLICY_KEY = "__server_tx_limits"
_host_rules = {}
_locks = {}
_registry_lock = threading.RLock()


def device_lock(gid):
    with _registry_lock:
        return _locks.setdefault(int(gid), threading.RLock())


def rule_wire(rule):
    from dataclasses import asdict

    return json.dumps(asdict(rule) if rule is not None else None)


def install_host_rule(gid, wire):
    data = json.loads(wire)
    rule = None if data is None else TxFreqRule(**data)
    with device_lock(gid):
        _host_rules[int(gid)] = rule


def host_rule(gid):
    return _host_rules.get(int(gid))


def check_current_usrp(rule, native, channels=None):
    if rule is None:
        return
    if not channels:
        channels = range(int(native.get_tx_num_channels()))
    for channel in channels:
        rule.check(native.get_tx_freq(int(channel)))


_UNSAFE_USRP = {
    "set_tx_lo_freq",
    "set_tx_lo_source",
    "set_tx_subdev_spec",
    "get_tree",
    "get_tx_dboard_iface",
    "get_rx_dboard_iface",
    "get_user_settings_iface",
    "set_user_register",
    "set_gpio_attr",
    "set_gpio_src",
    "set_command_time",
    "set_master_clock_rate",
    "set_clock_source",
    "set_sync_source",
}


def check_usrp_control(rule, native, method, args, positional=()):
    """Return normalized arguments, checking exactly the values UHD will receive."""
    if rule is None:
        return args, list(positional)
    args, positional = dict(args), list(positional)
    if method in _UNSAFE_USRP:
        raise ValueError(
            f"{method} is unavailable while TX frequency limits are configured"
        )
    if method in {"set_tx_freq", "send_waveform"}:
        index, key = (0, "tune_request") if method == "set_tx_freq" else (2, "freq")
        value = positional[index] if len(positional) > index else args.get(key)
        freq = tune_request_freq(value) if method == "set_tx_freq" else _number(value)
        rule.check(freq)
        if method == "send_waveform":
            streamer = positional[7] if len(positional) > 7 else args.get("streamer")
            if streamer is not None:
                check_current_usrp(rule, native)
        # Normalize before dispatch, so check and native construction cannot disagree.
        if len(positional) > index:
            positional[index] = freq
        else:
            args[key] = freq
    return args, positional


def check_legacy(rule, schema, prop, verb, args):
    """Check decoded RPC arguments regardless of CALL0/CALL1/CALLN spelling."""
    if rule is None:
        return args
    dtype = str(schema.device_type).lower()
    args = dict(args)
    if tx_freq_prop(dtype):
        if verb == "SET" and prop == "tx_lo":
            rule.check(args["value"])
        elif (
            prop in {"dds_enabled", "dds_scales"}
            and verb == "SET"
            and not any(
                args["value"]
                if isinstance(args["value"], (list, tuple))
                else [args["value"]]
            )
        ):
            pass  # Disabling output must remain possible even with an invalid policy.
        elif verb != "GET" and prop in {
            "tx",
            "dds_single_tone",
            "dds_dual_tone",
            "dds_enabled",
            "dds_scales",
        }:
            rule.check(schema.dispatch("get_tx_lo", {}))
    elif dtype == "hackrf":
        if prop == "start_tx":
            rule.check(schema.dispatch("get_center_freq", {}))
        elif prop in {"center_freq", "set_freq"}:
            if getattr(schema.device, "_remoterf_tx_active", False):
                rule.check(args["value"])
    elif dtype == "usrp":
        # Opening through dispatch preserves the legacy schema's lifecycle.
        if prop in {"stream_send", "get_tx_stream"}:
            schema.dispatch("call_get_tx_num_channels", {})
            check_current_usrp(rule, schema.device)
        args, _ = check_usrp_control(rule, schema.device, prop, args)
    elif verb != "GET":
        raise ValueError(f"TX frequency limits do not support device type {dtype!r}")
    return args
