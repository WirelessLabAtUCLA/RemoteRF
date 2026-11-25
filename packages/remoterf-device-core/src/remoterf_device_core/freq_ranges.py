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

"""Shared multi-range frequency limit parsing and matching for SDR drivers."""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from numbers import Real
from typing import Any

FREQ_RANGE_KEYS = frozenset({"tx_allow", "tx_deny", "rx_allow", "rx_deny"})
LEGACY_FREQ_KEYS = frozenset({"tx_min_freq", "tx_max_freq", "rx_min_freq", "rx_max_freq"})
FREQ_LIMIT_KEYS = FREQ_RANGE_KEYS | LEGACY_FREQ_KEYS
NON_PLUTO_LOCKOUT_KEYS = FREQ_LIMIT_KEYS | frozenset({"admin_group_override"})


def format_mhz(value: float) -> str:
    """Format a frequency in Hz as compact MHz text."""
    return f"{float(value) / 1e6:g}"


def _parse_hz(
    key: str,
    value: Any,
    *,
    min_hz: float | None = 0.0,
    max_hz: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (Real, str)):
        raise ValueError(f"{key} must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{key} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{key} must be a finite number")
    if min_hz is not None and max_hz is not None:
        if not min_hz <= number <= max_hz:
            raise ValueError(f"{key} must be between {min_hz:g} and {max_hz:g}")
    elif min_hz is not None:
        if number < min_hz:
            raise ValueError(f"{key} must be >= {min_hz:g}")
    elif max_hz is not None:
        if number > max_hz:
            raise ValueError(f"{key} must be <= {max_hz:g}")
    return number


def merge_ranges(
    ranges: Iterable[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    """Sort and merge overlapping or touching [low, high] frequency ranges."""
    items = sorted((float(lo), float(hi)) for lo, hi in ranges)
    if not items:
        return ()
    merged: list[tuple[float, float]] = [items[0]]
    for lo, hi in items[1:]:
        prev_lo, prev_hi = merged[-1]
        if lo <= prev_hi:
            if hi > prev_hi:
                merged[-1] = (prev_lo, hi)
        else:
            merged.append((lo, hi))
    return tuple(merged)


def parse_range_list(
    key: str,
    raw: Any,
    *,
    min_hz: float | None = 0.0,
    max_hz: float | None = None,
) -> tuple[tuple[float, float], ...]:
    """Validate and merge a sequence of [low, high] frequency pairs in Hz."""
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{key} must be a list of [low, high] frequency ranges")
    parsed: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"{key} entries must be 2-item [low, high] ranges")
        lo = _parse_hz(key, item[0], min_hz=min_hz, max_hz=max_hz)
        hi = _parse_hz(key, item[1], min_hz=min_hz, max_hz=max_hz)
        if lo > hi:
            raise ValueError(f"{key} range low must be <= high")
        parsed.append((lo, hi))
    return merge_ranges(parsed)


@dataclass(frozen=True)
class DirectionFreqLimits:
    """Merged allow and deny ranges for one direction (TX or RX)."""

    allow: tuple[tuple[float, float], ...] | None = None
    deny: tuple[tuple[float, float], ...] = ()

    @property
    def is_active(self) -> bool:
        return self.allow is not None or bool(self.deny)

    def allows_freq(self, freq: Any) -> bool:
        if isinstance(freq, bool) or not isinstance(freq, Real):
            return False
        try:
            value = float(freq)
        except OverflowError:
            return False
        if not math.isfinite(value):
            return False
        if self.allow is not None and not any(lo <= value <= hi for lo, hi in self.allow):
            return False
        if any(lo <= value <= hi for lo, hi in self.deny):
            return False
        return True

    def allows_band(self, low: Any, high: Any) -> bool:
        """True iff [low, high] fits in ONE merged allow range and touches NO deny range."""
        if isinstance(low, bool) or isinstance(high, bool):
            return False
        if not isinstance(low, Real) or not isinstance(high, Real):
            return False
        try:
            a, b = float(low), float(high)
        except OverflowError:
            return False
        if not (math.isfinite(a) and math.isfinite(b)) or a > b:
            return False
        if self.allow is not None and not any(lo <= a and b <= hi for lo, hi in self.allow):
            return False
        if any(a <= dhi and dlo <= b for dlo, dhi in self.deny):
            return False
        return True

    def effective_allow(
        self,
        bounds: tuple[float, float] | None = None,
    ) -> tuple[tuple[float, float], ...]:
        """Return allowed intervals after subtracting deny ranges."""
        if self.allow is None:
            if bounds is None:
                return ()
            base: list[tuple[float, float]] = [(float(bounds[0]), float(bounds[1]))]
        else:
            base = list(self.allow)
        for dlo, dhi in self.deny:
            remaining: list[tuple[float, float]] = []
            for alo, ahi in base:
                if dhi < alo or dlo > ahi:
                    remaining.append((alo, ahi))
                else:
                    if dlo > alo:
                        remaining.append((alo, dlo))
                    if dhi < ahi:
                        remaining.append((dhi, ahi))
            base = remaining
        return tuple(base)

    def default_park_freq(
        self,
        *,
        fallback: float = 915e6,
        bounds: tuple[float, float] | None = None,
    ) -> float | None:
        """Midpoint of the first allowed range not covered by a deny range."""
        if not self.is_active:
            return float(fallback)
        if self.allow is None:
            if self.allows_freq(fallback):
                return float(fallback)
            intervals = self.effective_allow(bounds)
        else:
            intervals = self.effective_allow()
        for lo, hi in intervals:
            mid = (lo + hi) / 2.0
            if self.allows_freq(mid):
                return mid
        return None

    def describe_limits(self, side: str) -> str:
        tag = side.upper()
        if self.allow is not None and len(self.allow) == 1 and not self.deny:
            lo, hi = self.allow[0]
            return f"this device's {tag} range {format_mhz(lo)}–{format_mhz(hi)} MHz"
        if self.allow is not None:
            allowed = (
                ", ".join(f"{format_mhz(lo)}–{format_mhz(hi)} MHz" for lo, hi in self.allow)
                if self.allow
                else "none"
            )
            if self.deny:
                denied = ", ".join(f"{format_mhz(lo)}–{format_mhz(hi)} MHz" for lo, hi in self.deny)
                return f"this device's allowed {tag} ranges ({allowed}; denied: {denied})"
            return f"this device's allowed {tag} ranges ({allowed})"
        if self.deny:
            denied = ", ".join(f"{format_mhz(lo)}–{format_mhz(hi)} MHz" for lo, hi in self.deny)
            return f"this device's allowed {tag} ranges (denied: {denied})"
        return f"this device's {tag} range"

    def format_freq_error(
        self,
        side: str,
        freq: float,
        *,
        label: str | None = None,
    ) -> str:
        prefix = label or f"{side.upper()} LO"
        return f"{prefix} {format_mhz(freq)} MHz is outside {self.describe_limits(side)}."

    def format_band_error(
        self,
        side: str,
        low: float,
        high: float,
        *,
        detail: str = "tx_lo ± sample_rate/2",
    ) -> str:
        tag = side.upper()
        return (
            f"{tag} refused: the signal would occupy {format_mhz(low)}–{format_mhz(high)} MHz "
            f"({detail}), outside {self.describe_limits(side)}."
        )


@dataclass(frozen=True)
class FreqLimits:
    """Parsed TX and RX frequency limits plus admin bypass opt-in."""

    tx: DirectionFreqLimits = DirectionFreqLimits()
    rx: DirectionFreqLimits = DirectionFreqLimits()
    admin_group_override: bool = False

    @property
    def is_active(self) -> bool:
        return self.tx.is_active or self.rx.is_active


def parse_freq_limits(
    init: Mapping[str, Any] | None,
    *,
    min_hz: float | None = 0.0,
    max_hz: float | None = None,
    strict_min_max: bool = False,
) -> FreqLimits | None:
    """Parse frequency allow/deny ranges and legacy min/max keys from device init."""
    if init is None:
        return None
    if not isinstance(init, Mapping):
        raise ValueError("init must be a mapping")
    for key in init:
        if not isinstance(key, str):
            raise ValueError("init keys must be strings")
        lower = key.lower()
        if lower in NON_PLUTO_LOCKOUT_KEYS and key != lower:
            raise ValueError(f"frequency limit key {key!r} must be lowercase ({lower})")
    if not any(key in NON_PLUTO_LOCKOUT_KEYS for key in init):
        return None

    directions: dict[str, DirectionFreqLimits] = {}
    for side in ("tx", "rx"):
        lo_key, hi_key = f"{side}_min_freq", f"{side}_max_freq"
        allow_key, deny_key = f"{side}_allow", f"{side}_deny"
        has_legacy = lo_key in init or hi_key in init
        has_allow = allow_key in init
        if has_legacy and has_allow:
            raise ValueError(f"cannot specify both {lo_key}/{hi_key} and {allow_key}")
        if (lo_key in init) != (hi_key in init):
            raise ValueError(f"{lo_key} and {hi_key} must be provided together")
        if has_legacy:
            lo = _parse_hz(lo_key, init[lo_key], min_hz=min_hz, max_hz=max_hz)
            hi = _parse_hz(hi_key, init[hi_key], min_hz=min_hz, max_hz=max_hz)
            if strict_min_max:
                if lo >= hi:
                    raise ValueError(f"{lo_key} must be less than {hi_key}")
            elif lo > hi:
                raise ValueError(f"{lo_key} must be <= {hi_key}")
            allow: tuple[tuple[float, float], ...] | None = ((lo, hi),)
        elif has_allow:
            allow = parse_range_list(allow_key, init[allow_key], min_hz=min_hz, max_hz=max_hz)
        else:
            allow = None
        deny = (
            parse_range_list(deny_key, init[deny_key], min_hz=min_hz, max_hz=max_hz)
            if deny_key in init
            else ()
        )
        directions[side] = DirectionFreqLimits(allow=allow, deny=deny)

    admin_override = init.get("admin_group_override", False)
    if type(admin_override) is not bool:
        raise ValueError("admin_group_override must be a bool")

    return FreqLimits(
        tx=directions["tx"],
        rx=directions["rx"],
        admin_group_override=admin_override,
    )


def strip_freq_limit_keys(init: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy of init without frequency-limit / non-Pluto lockout keys."""
    if not init:
        return {}
    return {
        key: value
        for key, value in init.items()
        if not (isinstance(key, str) and key.lower() in NON_PLUTO_LOCKOUT_KEYS)
    }
