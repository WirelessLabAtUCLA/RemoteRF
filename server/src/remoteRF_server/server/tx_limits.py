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

import math
from dataclasses import dataclass
from typing import Any, Optional

# device_type -> the legacy SET property that tunes TX.
_TX_FREQ_PROPS = {"adalm_pluto": "tx_lo", "pluto_plus": "tx_lo"}
# device_type -> the Dynamic v2 MultiUSRP method that tunes TX.
_TX_FREQ_METHODS = {"usrp": "set_tx_freq"}


def tx_freq_prop(device_type: str) -> Optional[str]:
    dtype = str(device_type or "").strip().lower()
    if dtype.startswith("adalm_pluto_mimo"):
        return "tx_lo"
    return _TX_FREQ_PROPS.get(dtype)


def tx_freq_method(device_type: str) -> Optional[str]:
    return _TX_FREQ_METHODS.get(str(device_type or "").strip().lower())


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{value!r} is not a frequency")
    out = float(value)
    if not math.isfinite(out):
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
    on_violation: str = "reject"
    default: Optional[float] = None

    def allows(self, freq: float) -> bool:
        if any(lo <= freq <= hi for lo, hi in self.blacklist):
            return False
        return not self.whitelist or any(lo <= freq <= hi for lo, hi in self.whitelist)

    def message(self, freq: float) -> str:
        text = f"Transmitting at {format_freq(freq)} is not allowed by this device's limits."
        for label, ranges in (("Whitelist", self.whitelist), ("Blacklist", self.blacklist)):
            if ranges:
                text += f" {label}: " + ", ".join(_format_range(lo, hi) for lo, hi in ranges) + "."
        return text

    def check(self, value: Any, *, allow_default: bool = True) -> tuple[float, Optional[str]]:
        """(frequency to tune, warning); raises ValueError when refused."""
        try:
            freq = _number(value)
        except (TypeError, ValueError):
            raise ValueError(f"TX frequency {value!r} is not a number; this device's limits refuse it") from None
        if self.allows(freq):
            return freq, None
        message = self.message(freq)
        if allow_default and self.on_violation == "default":
            return self.default, f"{message} Tuned to {format_freq(self.default)} instead."
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
    mode = str(raw.get("on_violation") or "reject").strip().lower()
    if mode not in ("reject", "default"):
        raise ValueError(f"on_violation must be reject or default, not {mode!r}")
    default = None
    if mode == "default":
        if raw.get("default") is None:
            raise ValueError("on_violation: default needs a default frequency")
        default = _number(raw["default"])
    rule = TxFreqRule(whitelist, blacklist, mode, default)
    if default is not None and not rule.allows(default):
        raise ValueError(f"default {format_freq(default)} is not allowed by the limits")
    return rule


def parse_limits(raw: Any, *, gid: int) -> dict[str, TxFreqRule]:
    """The device's valid rules; a bad rule is logged and skipped."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        print(f"Skipping limits for device gid={gid}: limits must be a mapping")
        return {}
    out: dict[str, TxFreqRule] = {}
    if "tx_freq" in raw:
        try:
            out["tx_freq"] = parse_tx_freq(raw["tx_freq"])
        except (TypeError, ValueError) as e:
            print(f"Skipping limits.tx_freq for device gid={gid}: {e}")
    return out


def tune_request_freq(value: Any) -> Any:
    """The target frequency of a v2 set_tx_freq tune_request (float or TuneRequest payload)."""
    if isinstance(value, dict):
        kwargs = value.get("kwargs") if isinstance(value.get("kwargs"), dict) else {}
        args = value.get("args")
        for candidate in (
            value.get("target_freq"),
            value.get("freq"),
            kwargs.get("target_freq"),
            args[0] if isinstance(args, (list, tuple)) and args else None,
        ):
            if candidate is not None:
                return candidate
    return value
