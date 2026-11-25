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

"""Server- and host-side function policy tag helpers."""
from __future__ import annotations

from typing import Any, Iterable


def normalize_tags(tag: Any) -> tuple[str, ...]:
    """Normalize a single tag string or a tuple/list of tag strings.

    Tags are compared case-insensitively (casefold), while preserving the
    original spelling of the first occurrence for display.
    """
    if tag is None:
        return ()
    if isinstance(tag, str):
        items: Iterable[Any] = (tag,)
    elif isinstance(tag, (list, tuple)):
        items = tag
    else:
        raise TypeError("tag must be a string or a tuple/list of strings")

    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"tag must be a string, got {type(item).__name__}")
        cleaned = item.strip()
        if not cleaned:
            raise ValueError("tag must not be empty")
        folded = cleaned.casefold()
        if folded not in seen:
            seen.add(folded)
            out.append(cleaned)
    return tuple(out)


def find_blocked_tag(
    method_tags: Iterable[str],
    blocked_tags: Iterable[str],
) -> str | None:
    """Return the first matching method tag (display text) blocked by policy."""
    if not method_tags or not blocked_tags:
        return None
    blocked = {
        cleaned.casefold()
        for item in blocked_tags
        if isinstance(item, str) and (cleaned := item.strip())
    }
    if not blocked:
        return None
    for item in method_tags:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if cleaned and cleaned.casefold() in blocked:
            return cleaned
    return None


def blocked_tag_message(function_name: str, tag: str) -> str:
    """Format the standard refusal message for a tag-blocked RPC."""
    return f"{function_name} is blocked for your user groups on this device (tag {tag})"


def v1_method_name(prop: str, verb: str) -> str | None:
    """Map a v1 RPC (prop, verb) pair to its DeviceSchema method name."""
    if verb == "GET":
        return f"get_{prop}"
    if verb == "SET":
        return f"set_{prop}"
    if verb in ("CALL0", "CALL1", "CALLN"):
        return f"call_{prop}"
    return None


def v1_function_label(prop: str, verb: str) -> str:
    """Human-readable function label for a v1 RPC error message."""
    if verb == "GET":
        return f"get_{prop}"
    if verb == "SET":
        return f"set_{prop}"
    return prop
