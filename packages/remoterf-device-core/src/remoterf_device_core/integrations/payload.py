"""Bounded plain-data envelopes for the experimental integration contract.

Kept separate from UHD value conversion: changing this must never change the
meaning of an existing UHD helper or sample payload.
"""
from __future__ import annotations

import json
import math


class IntegrationError(Exception):
    """A stable code and safe message; native exceptions stay in the cause."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# These tags are interpreted by existing value/handle codecs, including nested
# values. Application data must not accidentally construct a native object.
RESERVED_KEYS = frozenset({
    "__remoterf_handle_arg__", "__remoterf_handle__", "__remoterf_type__",
    "__uhd_type__", "__remoterf_uhd_helper__", "__remoterf_json_type__",
    "__args__", "__kwargs__",
})
MAX_ENVELOPE_BYTES = 64 * 1024
MAX_DEPTH = 16
MAX_ITEMS = 4096


def plain_data(value, *, max_bytes: int = MAX_ENVELOPE_BYTES):
    """Validate and copy a JSON value, without invoking user conversion hooks.

This budget covers the JSON envelope, not an entire relayed RPC. Bulk data
must use a separately bounded top-level binary transfer. Integers are int64;
bytes/arrays, custom objects, non-finite floats and protocol tags are rejected.
"""
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_ENVELOPE_BYTES:
        raise ValueError("max_bytes must be within the integration envelope limit")
    remaining = MAX_ITEMS
    text_bytes = 0

    def text_size(text):
        nonlocal text_bytes
        # Reject before encoding a potentially enormous string.
        if len(text) > max_bytes:
            raise IntegrationError("payload_limit", "integration text exceeds its budget")
        try:
            text_bytes += len(text.encode("utf-8"))
        except UnicodeError as exc:
            raise IntegrationError("invalid_payload", "integration text must be valid Unicode") from exc
        if text_bytes > max_bytes:
            raise IntegrationError("payload_limit", "integration text exceeds its budget")

    def copy(item, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_DEPTH:
            raise IntegrationError("payload_limit", "integration payload is too complex")
        kind = type(item)
        if item is None or kind is bool:
            return item
        if kind is int:
            if -(1 << 63) <= item < (1 << 63):
                return item
        elif kind is float:
            if math.isfinite(item):
                return item
        elif kind is str:
            text_size(item)
            return item
        elif kind is list:
            if len(item) > remaining:
                raise IntegrationError("payload_limit", "too many integration payload items")
            return [copy(child, depth + 1) for child in item]
        elif kind is dict:
            if len(item) > remaining:
                raise IntegrationError("payload_limit", "too many integration payload fields")
            result = {}
            for key, child in item.items():
                if type(key) is not str or key in RESERVED_KEYS:
                    raise IntegrationError("invalid_payload", "invalid or reserved integration field")
                text_size(key)
                result[key] = copy(child, depth + 1)
            return result
        raise IntegrationError("invalid_payload", "unsupported integration value")

    result = copy(value, 0)
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise IntegrationError("payload_limit", "encoded integration envelope exceeds its budget")
    return result


def object_data(value, *, max_bytes: int = MAX_ENVELOPE_BYTES) -> dict:
    if type(value) is not dict:
        raise IntegrationError("invalid_payload", "integration parameters must be an object")
    return plain_data(value, max_bytes=max_bytes)
