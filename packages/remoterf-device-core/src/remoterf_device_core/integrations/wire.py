"""The generic integration Invoke envelope, before native/UHD decoding.

The seven methods take one required ``request: dict`` and return ``dict``. The
normal generated client sends a named ``request`` argument. The positional
transport form ``__args__: [request]`` is also accepted, with no other arguments.
Never pass these protobuf values through the native DynamicValue decoder: JSON
protocol tags must be rejected as data, not interpreted as helpers or handles.

Semantic request fields and discovery pagination belong to the authenticated
controller. These helpers enforce transport shape, exact plain-data types and
encoded limits independently of which adapter is installed or enabled.
"""
from __future__ import annotations

import json

from .payload import IntegrationError, MAX_ENVELOPE_BYTES, object_data


INTEGRATION_METHODS = (
    "integration_list", "integration_describe", "integration_start",
    "integration_status", "integration_stop", "integration_call", "integration_fetch",
)
INTEGRATION_OVERLOAD = "request"
MAX_INVOKE_BYTES = 128 * 1024


def _overload(overload_id):
    if type(overload_id) is not str or overload_id not in ("", INTEGRATION_OVERLOAD):
        raise IntegrationError("invalid_request", "unknown integration overload")


def validate_arguments(args, positional_args=None, *, overload_id="") -> dict:
    """Validate an internal Invoke call without any native value conversion."""
    _overload(overload_id)
    if type(args) is not dict:
        raise IntegrationError("invalid_request", "integration arguments must be named")
    if positional_args is None:
        positional_args = []
    if type(positional_args) is not list:
        raise IntegrationError("invalid_request", "integration positional arguments must be a list")
    if set(args) == {"request"} and not positional_args:
        return object_data(args["request"])
    if not args and len(positional_args) == 1:
        return object_data(positional_args[0])
    raise IntegrationError("invalid_request", "integration methods require exactly one request object")


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IntegrationError("invalid_payload", "duplicate integration JSON field")
        result[key] = value
    return result


def _reject_constant(value):
    raise IntegrationError("invalid_payload", "integration numbers must be finite")


def _json(value, *, positional):
    if value.WhichOneof("value") != "json_value":
        raise IntegrationError("invalid_payload", "integration request must use plain JSON")
    raw = value.json_value
    # Permit the two outer array brackets for the positional wire form. Count
    # actual wire text as well as the normalized envelope; whitespace is not free.
    limit = MAX_ENVELOPE_BYTES + (2 if positional else 0)
    if len(raw) > limit or len(raw.encode("utf-8")) > limit:
        raise IntegrationError("payload_limit", "encoded integration request exceeds its budget")
    try:
        return json.loads(raw, object_pairs_hook=_unique_fields, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise IntegrationError("invalid_payload", "invalid integration JSON") from exc


def decode_request(request) -> dict:
    """Read a protobuf InvokeRequest before decode_value or UHD construction.

    Also rejects duplicate argument names, native/binary variants, extra kwargs,
    unknown overloads and oversized *complete* protobuf requests. Authentication,
    root-handle validation and per-method field checks must follow at dispatch.
    """
    if request.method not in INTEGRATION_METHODS:
        raise IntegrationError("invalid_request", "unknown integration method")
    _overload(request.overload_id)
    if request.ByteSize() > MAX_INVOKE_BYTES:
        raise IntegrationError("payload_limit", "integration Invoke frame exceeds its budget")
    if len(request.args) != 1:
        raise IntegrationError("invalid_request", "integration methods require exactly one request object")
    argument = request.args[0]
    if argument.name == "request":
        return validate_arguments({"request": _json(argument.value, positional=False)},
                                  overload_id=request.overload_id)
    if argument.name == "__args__":
        return validate_arguments({}, _json(argument.value, positional=True),
                                  overload_id=request.overload_id)
    raise IntegrationError("invalid_request", "unexpected integration argument")


def validate_result(value) -> dict:
    """Bound a reply before native snapshot conversion or DynamicValue encoding."""
    return object_data(value)
