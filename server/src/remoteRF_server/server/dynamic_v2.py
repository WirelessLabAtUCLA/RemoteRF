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

"""Dynamic v2 local-device control, sessions, handles, and sample streaming."""
from __future__ import annotations

import json
import math
import numbers
import queue
import re
import secrets
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
from remoterf_device_core.native_lifetime import Activity, submit_cleanup
from remoterf_device_core.integrations import IntegrationError
from remoterf_device_core.integrations.wire import INTEGRATION_METHODS, decode_request, validate_arguments, validate_result
from dataclasses import dataclass, field
from itertools import chain
from typing import Any

import numpy as np

from ..common.grpc import grpc_pb2, grpc_pb2_grpc
from ..common.grpc.v2_codec import decode_value, encode_value
from ..common.idl.v2 import (
    CONTROL_PROTOCOL_VERSION,
    SCHEMA_VERSION,
    STREAMING_PROTOCOL_VERSION,
)
# Retained as a module attribute for integrations that patched the former
# policy loader. Runtime access is no longer gated by it.
from ..drivers.usrp.policy import USRPPolicy
from ..drivers.usrp.uhd_manifest import (
    HANDLE_METHOD_RETURN_TYPES,
    HANDLE_RETURN_METHODS,
)
from .device_manager import (
    hardware_access,
    diag_blocks,
    get_local_device_context,
    register_reservation_end_callback,
    resolve_gid_for_token,
    token_matches_device,
)
from .v2_errors import (
    RemoteRFNativeUHDError,
    RemoteRFOverloadError,
    RemoteRFPolicyError,
    RemoteRFProtocolError,
    RemoteRFReservationError,
    RemoteRFServerOverloadedError,
    RemoteRFSessionExpiredError,
    RemoteRFStaleHandleError,
    error_envelope,
)
from .host_dynamic_v2 import host_session_proxy

_HANDLE_PREFIX_BYTES = 32
_MAX_ASYNC_EVENTS = 256
_MAX_SAMPLE_PAYLOAD_BYTES = 4 * 1024 * 1024
_MAX_SESSIONS = 64
_MAX_HANDLES_PER_SESSION = 64
_MAX_CREDITS = 64
_RX_PREFETCH_BATCH_SAMPS = 64 * 1024
_MAX_RX_PREFETCH_BATCHES = 16
_SAMPLE_DTYPES = {
    "<c8": np.dtype("<c8"),
    "<c16": np.dtype("<c16"),
    "<i2": np.dtype("<i2"),
    "<f4": np.dtype("<f4"),
    "<f8": np.dtype("<f8"),
}


def _opaque_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(_HANDLE_PREFIX_BYTES)}"


def _uhd_type_name(value: Any) -> str:
    cls = type(value)
    return f"{cls.__module__}.{cls.__name__}"


def _call_value(value: Any, name: str, default=None):
    target = getattr(value, name, None)
    if callable(target):
        try:
            return target()
        except Exception:
            return default
    return target if target is not None else default


def _sample_dtype(value: str) -> np.dtype:
    """Accept only the canonical scalar little-endian wire dtypes."""
    dtype = _SAMPLE_DTYPES.get(str(value))
    if dtype is None:
        raise RemoteRFProtocolError(
            f"unsupported or non-canonical sample dtype: {value!r}"
        )
    return dtype


def _sample_timeout(value: float) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout < 0:
        raise RemoteRFProtocolError(
            "sample timeout must be finite and non-negative"
        )
    return timeout


def _matches_wire_type(value: Any, wire_type: str) -> bool:
    if value is None:
        return "None" in str(wire_type).split("|") or wire_type == "any"
    for choice in (part.strip() for part in str(wire_type or "any").split("|")):
        if choice == "any":
            return True
        if choice == "bool" and isinstance(value, (bool, np.bool_)):
            return True
        if (
            choice == "int"
            and isinstance(value, (numbers.Integral, np.integer))
            and not isinstance(value, bool)
        ):
            return True
        if (
            choice == "float"
            and isinstance(value, (numbers.Real, np.integer, np.floating))
            and not isinstance(value, bool)
        ):
            return True
        if choice == "str" and isinstance(value, str):
            return True
        if choice == "bytes" and isinstance(value, bytes):
            return True
        if (
            choice == "complex"
            and isinstance(value, (numbers.Complex, np.number))
            and not isinstance(value, bool)
        ):
            return True
        if choice.startswith("list") and isinstance(value, (list, tuple, np.ndarray)):
            if "[" not in choice:
                return True
            item_type = choice.partition("[")[2].rpartition("]")[0]
            return all(_matches_wire_type(item, item_type) for item in value)
        if choice.startswith("dict") and isinstance(value, dict):
            return True
        if choice == "ndarray" and isinstance(value, np.ndarray):
            return True
        if choice.startswith("uhd.") and isinstance(value, dict):
            return True
    return False


def _accepts(parameter: dict, value: Any) -> bool:
    if value is None and not parameter.get("required", True):
        return True
    return _matches_wire_type(value, str(parameter.get("type") or "any"))


def _validate_overload(
    descriptor: dict,
    overload_id: str,
    args: dict[str, Any],
    positional_args: list[Any],
) -> str:
    candidates = list(descriptor.get("overloads") or ())
    if not overload_id:
        if len(candidates) != 1:
            raise RemoteRFOverloadError(
                f"{descriptor['name']} requires an explicit overload id"
            )
        selected = candidates[0]
    else:
        selected = next(
            (candidate for candidate in candidates if candidate.get("id") == overload_id),
            None,
        )
        if selected is None:
            raise RemoteRFOverloadError(
                f"unknown overload {overload_id!r} for {descriptor['name']}"
            )

    allowed: set[str] = set()
    accepts_positional = False
    accepts_keywords = False
    positional_index = 0
    for parameter in selected.get("parameters", ()):
        kind = parameter.get("kind", "positional_or_keyword")
        name = str(parameter["name"])
        if kind == "positional_only":
            accepts_positional = True
            if positional_index < len(positional_args):
                value = positional_args[positional_index]
                positional_index += 1
                if not _accepts(parameter, value):
                    raise RemoteRFOverloadError(
                        f"argument {name!r} does not match "
                        f"{parameter.get('type', 'any')}"
                    )
            elif parameter.get("required", True):
                raise RemoteRFOverloadError(
                    f"missing required argument {name!r} for "
                    f"{descriptor['name']}"
                )
            continue
        if kind == "var_positional":
            accepts_positional = True
            positional_index = len(positional_args)
            continue
        if kind == "var_keyword":
            accepts_keywords = True
            continue
        allowed.add(name)
        if parameter.get("required", True) and name not in args:
            raise RemoteRFOverloadError(
                f"missing required argument {name!r} for {descriptor['name']}"
            )
        if name in args and not _accepts(parameter, args[name]):
            raise RemoteRFOverloadError(
                f"argument {name!r} does not match {parameter.get('type', 'any')}"
            )

    unexpected = set(args) - allowed
    if unexpected and not accepts_keywords:
        raise RemoteRFOverloadError(
            f"unexpected arguments for {descriptor['name']}: {sorted(unexpected)}"
        )
    if len(positional_args) > positional_index:
        raise RemoteRFOverloadError(
            f"unexpected positional arguments for {descriptor['name']}"
        )
    if positional_args and not accepts_positional:
        raise RemoteRFOverloadError(
            f"unexpected positional arguments for {descriptor['name']}"
        )
    return str(selected.get("id") or "")


def snapshot_native(value: Any):
    """Convert UHD/pybind values into portable tagged value snapshots."""
    if value is None or isinstance(value, (str, bool, int, float, bytes, np.ndarray)):
        return value
    class_name = type(value).__name__.lower()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, complex):
        return value
    if isinstance(value, Mapping) and "device_addr" not in class_name:
        return {str(key): snapshot_native(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [snapshot_native(item) for item in value]

    if "device_addr" in class_name:
        items = {}
        try:
            for key in value.keys():
                items[str(key)] = str(value[key])
        except Exception:
            raw = _call_value(value, "to_string", str(value))
            for item in str(raw or "").split(","):
                if "=" in item:
                    key, raw_value = item.split("=", 1)
                    items[key.strip()] = raw_value.strip()
        return {"__uhd_type__": "DeviceAddr", "items": items}

    if "time_spec" in class_name or (
        hasattr(value, "get_real_secs") and hasattr(value, "get_full_secs")
    ):
        return {
            "__uhd_type__": "TimeSpec",
            "secs": float(value.get_real_secs()),
        }

    if "meta_range" in class_name:
        ranges = []
        try:
            ranges = [snapshot_native(item) for item in value]
        except TypeError:
            pass
        start = snapshot_native(_call_value(value, "start"))
        stop = snapshot_native(_call_value(value, "stop"))
        step = snapshot_native(_call_value(value, "step"))
        if not ranges and start is not None and stop is not None:
            # UHD 4.10's Python MetaRange exposes aggregate methods but is not
            # iterable on every build. Preserve that interval as one Range so
            # clients do not decode a valid hardware range as an empty list.
            ranges = [
                {
                    "__uhd_type__": "Range",
                    "start": start,
                    "stop": stop,
                    "step": step,
                }
            ]
        return {
            "__uhd_type__": "MetaRange",
            "ranges": ranges,
            "start": start,
            "stop": stop,
            "step": step,
        }

    if class_name == "range" or class_name.endswith("_range"):
        return {
            "__uhd_type__": "Range",
            "start": snapshot_native(_call_value(value, "start")),
            "stop": snapshot_native(_call_value(value, "stop")),
            "step": snapshot_native(_call_value(value, "step")),
        }

    if "tune_result" in class_name:
        fields = (
            "clipped_rf_freq", "target_rf_freq", "actual_rf_freq",
            "target_dsp_freq", "actual_dsp_freq",
        )
        return {
            "__uhd_type__": "TuneResult",
            **{name: snapshot_native(getattr(value, name, None)) for name in fields},
        }

    if "tune_request" in class_name:
        fields = (
            "target_freq", "lo_offset", "rf_freq", "dsp_freq",
            "rf_freq_policy", "dsp_freq_policy", "args",
        )
        return {
            "__uhd_type__": "TuneRequest",
            **{name: snapshot_native(getattr(value, name, None)) for name in fields},
        }

    if "sensor_value" in class_name:
        fields = ("name", "value", "unit", "type")
        return {
            "__uhd_type__": "SensorValue",
            **{name: snapshot_native(getattr(value, name, None)) for name in fields},
            "pretty": str(value),
        }

    if "rx_metadata" in class_name:
        error = getattr(value, "error_code", None)
        return {
            "__uhd_type__": "RXMetadata",
            "error_code": str(error).split(".")[-1] if error is not None else "none",
            "error_code_repr": str(error) if error is not None else "none",
            "has_time_spec": getattr(value, "has_time_spec", None),
            "time_spec": snapshot_native(getattr(value, "time_spec", None)),
            "more_fragments": getattr(value, "more_fragments", None),
            "fragment_offset": getattr(value, "fragment_offset", None),
            "out_of_sequence": getattr(value, "out_of_sequence", None),
        }

    if "async_metadata" in class_name:
        event = getattr(value, "event_code", None)
        return {
            "__uhd_type__": "TXAsyncMetadata",
            "event_code": str(event).split(".")[-1] if event is not None else "",
            "event_code_repr": str(event) if event is not None else "",
            "has_time_spec": getattr(value, "has_time_spec", None),
            "time_spec": snapshot_native(getattr(value, "time_spec", None)),
            "channel": getattr(value, "channel", None),
            "user_payload": snapshot_native(getattr(value, "user_payload", None)),
        }

    if "subdev_spec_pair" in class_name:
        return {
            "__uhd_type__": "SubdevSpecPair",
            "db_name": str(getattr(value, "db_name", "")),
            "sd_name": str(getattr(value, "sd_name", "")),
        }

    if "subdev_spec" in class_name:
        return {"__uhd_type__": "SubdevSpec", "spec": str(value)}

    if "stream_args" in class_name:
        return {
            "__uhd_type__": "StreamArgs",
            "cpu_format": str(getattr(value, "cpu_format", "")),
            "otw_format": str(getattr(value, "otw_format", "")),
            "args": snapshot_native(getattr(value, "args", None)),
            "channels": snapshot_native(getattr(value, "channels", [])),
        }

    if "stream_cmd" in class_name:
        mode = getattr(value, "stream_mode", getattr(value, "mode", ""))
        return {
            "__uhd_type__": "StreamCMD",
            "mode": str(mode).split(".")[-1],
            "num_samps": getattr(value, "num_samps", 0),
            "stream_now": getattr(value, "stream_now", True),
            "time_spec": snapshot_native(getattr(value, "time_spec", None)),
        }

    if "tx_metadata" in class_name:
        return {
            "__uhd_type__": "TXMetadata",
            "has_time_spec": getattr(value, "has_time_spec", False),
            "time_spec": snapshot_native(getattr(value, "time_spec", None)),
            "start_of_burst": getattr(value, "start_of_burst", False),
            "end_of_burst": getattr(value, "end_of_burst", False),
        }

    if hasattr(value, "get_type") and hasattr(value, "is_bypassed"):
        pretty = str(value)

        def pretty_int(label, default=0):
            match = re.search(
                rf"^\s*{re.escape(label)}:\s*(\d+)\s*$",
                pretty,
                flags=re.MULTILINE,
            )
            return int(match.group(1)) if match else default

        fields = {
            "__uhd_type__": "FilterInfoBase",
            "native_class": class_name,
            "filter_type": str(value.get_type()).split(".")[-1],
            "bypassed": bool(value.is_bypassed()),
            "position": pretty_int("position index"),
            "pretty": pretty,
        }
        method_fields = {
            "get_analog_type": "analog_type",
            "get_cutoff": "cutoff",
            "get_rolloff": "rolloff",
            "get_output_rate": "output_rate",
            "get_input_rate": "input_rate",
            "get_interpolation": "interpolation",
            "get_decimation": "decimation",
            "get_tap_full_scale": "tap_full_scale",
            "get_taps": "taps",
        }
        for getter, field_name in method_fields.items():
            target = getattr(value, getter, None)
            if callable(target):
                fields[field_name] = snapshot_native(target())
        if "taps" in fields:
            fields["max_num_taps"] = pretty_int(
                "max num taps",
                len(fields["taps"]),
            )
        return fields

    enum_name = getattr(value, "name", None)
    if isinstance(enum_name, str):
        return enum_name

    public_fields = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            item = getattr(value, name)
        except Exception:
            continue
        if callable(item):
            continue
        if isinstance(item, (str, bool, int, float, complex, type(None))):
            public_fields[name] = snapshot_native(item)
    if public_fields:
        return {
            "__uhd_type__": type(value).__name__,
            "__native_path__": _uhd_type_name(value),
            **public_fields,
        }
    return str(value)


@dataclass
class HandleEntry:
    handle: str
    type_id: str
    value: Any
    parent: str | None
    generation: int
    lock: threading.RLock = field(default_factory=threading.RLock)
    closed: bool = False
    sequence: int = -1
    active_operation: str = ""
    async_events: queue.Queue = field(
        default_factory=lambda: queue.Queue(maxsize=_MAX_ASYNC_EVENTS)
    )
    stop_event: threading.Event = field(default_factory=threading.Event)
    worker: threading.Thread | None = None
    async_overflowed: bool = False
    stream_channels: list[int] = field(default_factory=list)
    cpu_format: str = ""
    rx_prefetch: queue.Queue = field(
        default_factory=lambda: queue.Queue(maxsize=_MAX_RX_PREFETCH_BATCHES)
    )
    rx_prefetch_stop: threading.Event = field(default_factory=threading.Event)
    rx_worker: threading.Thread | None = None
    # Only native recv calls: a prefetch worker outliving its 0.5 s stop join
    # must never recv on the streamer alongside its successor or a direct recv.
    rx_recv_lock: threading.Lock = field(default_factory=threading.Lock)
    rx_prefetch_overflowed: bool = False
    rx_pending: tuple[np.ndarray, dict, int] | None = None


@dataclass
class DeviceSession:
    session_id: str
    token: str
    gid: int
    schema: Any
    schema_v2: dict
    root_handle: str
    capabilities: dict
    handles: dict[str, HandleEntry] = field(default_factory=dict)
    control_lock: threading.RLock = field(default_factory=threading.RLock)
    lifecycle_lock: threading.RLock = field(default_factory=threading.RLock)
    closed: bool = False
    activity: Activity = field(default_factory=Activity)
    closing_handles: dict = field(default_factory=dict)
    cleanup: Any = None
    cleanup_error: bool = False
    retired_handles: list = field(default_factory=list)
    created_monotonic: float = field(default_factory=time.monotonic)


def _refuse_partner_token(token: str) -> None:
    """A HOME-minted token for a partner lab's device has no v2 session here:
    the Client carries Dynamic v2 to the owner over the device plane
    (``DynamicV2:<Operation>:WIRE``), which the HOME does federate. Say so as a
    protocol error naming the owner, not as a dead reservation."""
    from .reservation import reservation_handler

    plane = getattr(reservation_handler, "federation_devices", None)
    route = plane.resolve_token(token) if plane is not None else None
    if route is not None:
        raise RemoteRFProtocolError(
            "partner devices take Dynamic v2 over the device plane",
            details={"federated": str(route["owner_deployment_id"])},
        )


class SessionManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: dict[str, DeviceSession] = {}
        self._session_by_gid: dict[int, str] = {}
        self._opening_gids: set[int] = set()
        self._generation = 0
        self._closing_sessions = {}
        self._epochs = {}
        self._stopping = False
        self._cleanup_failures = 0
        from .integration_controller import Controller
        self.integrations = Controller()

    def _next_generation(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def busy(self, gid: int) -> bool:
        """A live or opening session holds this device."""
        with self._lock:
            return (hardware_access.blocked(gid) or gid in self._opening_gids or self._session_by_gid.get(gid) in self._sessions
                    or any(s.gid == gid for s in self._closing_sessions.values()))

    def cleanup_pending(self, gid):
        with self._lock:
            return int(gid) in self._opening_gids or any(s.gid == int(gid) for s in self._closing_sessions.values())

    @staticmethod
    def integration_gate(gid):
        return hardware_access.gate(gid)

    def integration_binding(self, session_id, *, work_dir, resources_released):
        """Internal binding for configured, durably fenced lab-local USRPs.

        No RPC/configuration exposes this yet. The deployment must install a
        canonical physical gate shared by every alias before this can succeed.
        resource verification comes from trusted supervision, never the client.
        """
        from remoterf_device_core.integrations import IntegrationError
        from remoterf_device_core.integrations.runtime import Binding

        session = self.get(session_id)
        inventory = get_local_device_context(session.gid)
        gate = hardware_access.gate(session.gid)
        if not inventory or inventory["dtype"] != "usrp" or gate.journal is None:
            raise IntegrationError("unsupported", "physical ownership has not been configured")
        root = self.get_handle(session, session.root_handle)
        generation = root.generation

        def authority():
            return not session.closed and token_matches_device(session.token, session.gid)

        def claim(acquire):
            aliases = set(hardware_access.aliases(gate))
            schemas = [context['schema'] for gid in aliases
                       if (context := get_local_device_context(gid)) is not None]
            # Never wait behind a long root call on the startup/RPC thread.
            if not session.control_lock.acquire(blocking=False):
                raise IntegrationError("device_busy", "native control is active")
            try:
                with session.lifecycle_lock:
                    if not authority():
                        raise IntegrationError("expired", "reservation authority ended")
                    if root.generation != generation:
                        raise IntegrationError("stale_owner", "native ownership generation has changed")
                    if session.cleanup_error:
                        raise IntegrationError("recovery_required", "a native resource failed cleanup")
                    if (session.closing_handles or len(session.handles) != 1
                            or not session.activity.idle() or root.value is None):
                        raise IntegrationError("device_busy", "close all native child handles before handoff")
                    with self._lock, gate.transition():
                        others = list(self._sessions.values()) + list(self._closing_sessions.values())
                        if (any(other is not session and other.gid in aliases for other in others)
                                or self._opening_gids.intersection(aliases)
                                or any(getattr(schema, 'device', None) is not None for schema in schemas)):
                            raise IntegrationError("device_busy", "another physical alias still owns native resources")
                        return acquire()
            finally:
                session.control_lock.release()

        def release_native():
            with session.control_lock:
                root.value = None  # a root handle must not pin the old native radio
                session.schema.close_native_device()

        def native_released():
            return root.value is None and session.schema.device is None

        def restore_native():
            with session.control_lock:
                live = inventory["schema"].create_session_schema()
                try:
                    metadata = live.build_session_metadata_v2()
                    with session.lifecycle_lock:
                        if not authority():
                            raise IntegrationError("expired", "reservation ended during native reacquisition")
                        metadata["remote_resource_limits"] = session.capabilities.get("remote_resource_limits", {})
                        metadata["application_integrations"] = self.integrations.capabilities()
                        session.schema = live
                        session.capabilities = metadata
                        root.value = live.device
                        root.generation = self._next_generation()
                except BaseException:
                    live.close_native_device()
                    raise

        return Binding(gate, str(session.gid), "usrp", session.session_id, generation,
                       work_dir, authority, release_native, native_released, restore_native,
                       resources_released, claim)

    def metrics(self) -> dict[str, int]:
        """Return bounded-resource counters for server status/monitoring hooks."""
        with self._lock:
            sessions = list(self._sessions.values())
            opening = len(self._opening_gids)
            closing = len(self._closing_sessions)
        handles = []
        for session in sessions:
            with session.lifecycle_lock:
                handles.extend(session.handles.values())
        return {
            "sessions": len(sessions),
            "opening_sessions": opening,
            "closing_sessions": closing,
            "failed_native_cleanups": self._cleanup_failures,
            "closing_handles": sum(len(s.closing_handles) for s in sessions),
            "handles": len(handles),
            "async_queue_depth": sum(entry.async_events.qsize() for entry in handles),
            "async_queue_overflows": sum(
                1 for entry in handles if entry.async_overflowed
            ),
            "rx_prefetch_queue_depth": sum(
                entry.rx_prefetch.qsize() for entry in handles
            ),
            "rx_prefetch_queue_overflows": sum(
                1 for entry in handles if entry.rx_prefetch_overflowed
            ),
        }

    def schema_for_token(self, token: str) -> dict:
        _refuse_partner_token(token)
        gid = resolve_gid_for_token(token)
        if gid is None:
            raise RemoteRFReservationError("invalid token or inactive reservation")
        context = get_local_device_context(gid)
        if context is None:
            raise RemoteRFReservationError(
                "Dynamic v2 currently supports local devices only"
            )
        schema = context["schema"]
        factory = getattr(schema, "get_idl_v2", None)
        if not callable(factory):
            raise RemoteRFProtocolError(
                f"device type {context['dtype']!r} does not publish schema v2"
            )
        return factory()

    def open(self, token: str, requested_schema_hash: str = "") -> DeviceSession:
        _refuse_partner_token(token)
        gid = resolve_gid_for_token(token)
        if gid is None:
            raise RemoteRFReservationError("invalid token or inactive reservation")
        context = get_local_device_context(gid) or {}
        with hardware_access.native(gid), context.get("io_lock", nullcontext()):
            prepare = context.get("prepare")
            if prepare is not None:
                prepare()
            return self._open_admitted(token, requested_schema_hash, admitted_gid=gid,
                                       admitted_schema=context.get("schema"))

    def _open_admitted(self, token: str, requested_schema_hash: str = "", *,
                       admitted_gid: int, admitted_schema) -> DeviceSession:
        _refuse_partner_token(token)
        gid = resolve_gid_for_token(token)
        if gid is None:
            raise RemoteRFReservationError("invalid token or inactive reservation")
        if gid != admitted_gid:
            raise RemoteRFReservationError("device authority changed; retry open")
        if diag_blocks(gid):
            raise RemoteRFReservationError("the device is under diagnostics; try again in a few minutes")
        context = get_local_device_context(gid)
        if context is None:
            raise RemoteRFReservationError(
                "Dynamic v2 currently supports local devices only"
            )
        inventory_schema = context["schema"]
        if inventory_schema is not admitted_schema:
            raise RemoteRFReservationError("device inventory changed; retry open")
        schema_v2 = self.schema_for_token(token)
        if requested_schema_hash and requested_schema_hash != schema_v2["schema_hash"]:
            raise RemoteRFProtocolError(
                "client schema hash is stale",
                details={
                    "requested": requested_schema_hash,
                    "current": schema_v2["schema_hash"],
                },
            )

        with self._lock:
            # Under the lock diag's busy() takes after its hold: one of the two sees the other.
            if diag_blocks(gid):
                raise RemoteRFReservationError("the device is under diagnostics; try again in a few minutes")
            if self._stopping:
                raise RemoteRFReservationError("device runtime is stopping")
            if any(s.gid == gid for s in self._closing_sessions.values()):
                raise RemoteRFReservationError("previous native cleanup is still in progress")
            epoch = self._epochs.get(gid, 0)
            if gid in self._opening_gids:
                raise RemoteRFReservationError(
                    "the reserved USRP is already being opened by another Dynamic v2 session"
                )
            existing = self._session_by_gid.get(gid)
            if existing not in self._sessions:
                existing = None
            live = len(self._sessions) - (1 if existing else 0)
            if live + len(self._opening_gids) + len(self._closing_sessions) >= _MAX_SESSIONS:
                raise RemoteRFServerOverloadedError(
                    "the Dynamic v2 session limit has been reached",
                    details={"maximum_sessions": _MAX_SESSIONS},
                )
            self._opening_gids.add(gid)

        create = getattr(inventory_schema, "create_session_schema", None)
        if not callable(create):
            with self._lock:
                self._opening_gids.discard(gid)
            raise RemoteRFProtocolError("device driver is not session-scoped")
        try:
            # Only the reservation holder can resolve this token, so a leftover
            # v2 session or lazily opened v1 device is theirs: a crashed or
            # unclosed previous run. Release it instead of locking them out;
            # a handle that fails to close is already unusable, and the fresh
            # open below decides whether the hardware is actually free.
            if existing:
                try:
                    self.close(existing)
                except Exception:
                    pass
            with self._lock:
                if any(s.gid == gid for s in self._closing_sessions.values()):
                    raise RemoteRFReservationError("previous native cleanup is still in progress")
            if getattr(inventory_schema, "device", None) is not None:
                try:
                    inventory_schema.close_native_device()
                except Exception:
                    pass
            live_schema = create()
            capabilities = live_schema.build_session_metadata_v2()
            capabilities["remote_resource_limits"] = {
                "maximum_sessions": _MAX_SESSIONS,
                "maximum_handles_per_session": _MAX_HANDLES_PER_SESSION,
                "maximum_async_events_per_streamer": _MAX_ASYNC_EVENTS,
                "maximum_sample_payload_bytes": _MAX_SAMPLE_PAYLOAD_BYTES,
                "maximum_stream_credits": _MAX_CREDITS,
            }
        except Exception:
            try:
                if "live_schema" in locals():
                    failed_id = _opaque_id("failed")
                    failed = DeviceSession(failed_id, token, gid, live_schema, schema_v2, "", {})
                    with self._lock:
                        self._sessions[failed_id] = failed
                        self._session_by_gid[gid] = failed_id
                    self.close(failed_id, wait=False)
            except Exception:
                pass  # preserve the open error and historical raw reopen policy
            finally:
                with self._lock:
                    self._opening_gids.discard(gid)
            raise

        capabilities["application_integrations"] = self.integrations.capabilities()
        session_id = _opaque_id("sess")
        root_handle = _opaque_id("dev")
        session = DeviceSession(
            session_id=session_id,
            token=token,
            gid=gid,
            schema=live_schema,
            schema_v2=schema_v2,
            root_handle=root_handle,
            capabilities=capabilities,
        )
        session.handles[root_handle] = HandleEntry(
            handle=root_handle,
            type_id="uhd.usrp.MultiUSRP",
            value=live_schema.device,
            parent=None,
            generation=self._next_generation(),
        )
        # Recheck after the potentially slow open. Host reservation-end
        # callbacks advance the epoch even though bridge tokens are long-lived.
        try:
            valid = resolve_gid_for_token(token) == gid
        except Exception:
            valid = False
        with self._lock:
            stale = not valid or self._epochs.get(gid, 0) != epoch or self._stopping
            self._sessions[session_id] = session
            self._session_by_gid[gid] = session_id
            if not stale:
                self._opening_gids.discard(gid)
        if stale:
            try:
                self.close(session_id, wait=False)
            finally:
                with self._lock:
                    self._opening_gids.discard(gid)
            raise RemoteRFSessionExpiredError("reservation changed while opening the device")
        return session

    def get(self, session_id: str, *, validate_reservation: bool = True) -> DeviceSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None or session.closed:
            raise RemoteRFSessionExpiredError("unknown or closed device session")
        if validate_reservation and not token_matches_device(session.token, session.gid):
            self.close(session_id)
            raise RemoteRFSessionExpiredError(
                "reservation ended; the device session and all handles are stale"
            )
        if validate_reservation and diag_blocks(session.gid):
            raise RemoteRFReservationError("the device is under diagnostics; try again in a few minutes")
        return session

    def get_handle(
        self,
        session: DeviceSession,
        handle: str,
        *,
        expected_type: str | None = None,
        generation: int | None = None,
    ) -> HandleEntry:
        with session.lifecycle_lock:
            entry = session.handles.get(handle)
            if entry is None or entry.closed:
                raise RemoteRFStaleHandleError("unknown or closed remote handle")
            if expected_type and entry.type_id != expected_type:
                raise RemoteRFStaleHandleError(
                    f"handle type mismatch: expected {expected_type}, got {entry.type_id}"
                )
            if generation is not None and int(generation) != entry.generation:
                raise RemoteRFStaleHandleError("stream generation does not match the handle")
            return entry

    def _resolve_handle_argument(self, session: DeviceSession, value: Any):
        if isinstance(value, Mapping):
            handle = value.get("__remoterf_handle_arg__")
            if handle is not None:
                entry = self.get_handle(
                    session,
                    str(handle),
                    expected_type=str(value.get("type_id") or "") or None,
                    generation=int(value.get("generation", 0)),
                )
                return entry.value
            if value.get("__uhd_type__") or value.get(
                "__remoterf_uhd_helper__"
            ):
                from ..drivers.usrp.usrp_schema import _construct
                return _construct(dict(value))
            return {
                str(key): self._resolve_handle_argument(session, item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                self._resolve_handle_argument(session, item)
                for item in value
            ]
        return value

    def register_handle(
        self,
        session: DeviceSession,
        value: Any,
        type_id: str,
        *,
        parent: str,
    ) -> HandleEntry:
        with session.lifecycle_lock:
            if session.closed:
                raise RemoteRFSessionExpiredError(
                    "cannot create a handle for a closed session"
                )
            if len(session.handles) + len(session.closing_handles) >= _MAX_HANDLES_PER_SESSION:
                raise RemoteRFServerOverloadedError(
                    "the remote handle limit for this session has been reached",
                    details={"maximum_handles": _MAX_HANDLES_PER_SESSION},
                )
            prefix = "rx" if type_id.endswith("RXStreamer") else "tx" if type_id.endswith("TXStreamer") else "obj"
            handle = _opaque_id(prefix)
            entry = HandleEntry(
                handle=handle,
                type_id=type_id,
                value=value,
                parent=parent,
                generation=self._next_generation(),
            )
            session.handles[handle] = entry
        if type_id.endswith("TXStreamer"):
            self._start_async_worker(session, entry)
        return entry

    def _publish_handle(self, session, value, type_id, *, parent):
        try:
            self.get(session.session_id)
            return self.register_handle(session, value, type_id, parent=parent)
        except BaseException:
            # A native return can arrive after CloseSession or hit the handle
            # limit. Retain it until admitted calls finish; don't lose the only
            # reference or leave it pinned in an exception traceback.
            with session.lifecycle_lock:
                existing = next((entry for entry in session.handles.values()
                                 if entry.value is value and entry.handle != session.root_handle), None)
                if existing is not None:
                    session.handles.pop(existing.handle)
                entry = existing or HandleEntry(_opaque_id("unpublished"), type_id, value, parent, 0)
                entry.closed = True
                entry.stop_event.set()
                entry.rx_prefetch_stop.set()
                session.closing_handles[entry.handle] = entry
            value = None

            def dispose():
                try:
                    self._dispose_entry(entry)
                except BaseException:
                    session.cleanup_error = True
                    with self._lock:
                        self._cleanup_failures += 1
                    raise
                finally:
                    with session.lifecycle_lock:
                        session.closing_handles.pop(entry.handle, None)
            submit_cleanup(lambda: session.activity.idle() and self._workers_done(entry), dispose)
            raise

    def _start_async_worker(self, session: DeviceSession, entry: HandleEntry) -> None:
        native_method = getattr(entry.value, "recv_async_msg", None)
        metadata_type = getattr(getattr(__import__("uhd"), "types", None), "TXAsyncMetadata", None)
        if not callable(native_method) or metadata_type is None:
            return

        def run():
            while not entry.stop_event.is_set() and not session.closed:
                metadata = metadata_type()
                try:
                    with self.admitted(session, entry, worker=True):
                        found = bool(native_method(metadata, 0.1))
                except Exception:
                    break
                if entry.stop_event.is_set() or session.closed:
                    break
                if not found:
                    continue
                event = snapshot_native(metadata)
                try:
                    entry.async_events.put_nowait(event)
                except queue.Full:
                    entry.async_overflowed = True

        entry.worker = threading.Thread(
            target=run,
            name=f"remoterf-{entry.handle[:16]}-async",
            daemon=True,
        )
        entry.worker.start()

    @staticmethod
    def _clear_rx_prefetch(entry: HandleEntry) -> None:
        while True:
            try:
                entry.rx_prefetch.get_nowait()
            except queue.Empty:
                break
        entry.rx_pending = None
        entry.rx_prefetch_overflowed = False

    def _start_rx_prefetch(self, session: DeviceSession, entry: HandleEntry) -> None:
        if not entry.type_id.endswith("RXStreamer"):
            return
        self._stop_rx_prefetch(entry)
        if entry.rx_worker is not None and entry.rx_worker.is_alive():
            return
        self._clear_rx_prefetch(entry)
        stop = threading.Event()
        entry.rx_prefetch_stop = stop

        try:
            import uhd
            metadata_type = uhd.types.RXMetadata
        except Exception:
            return

        dtype = {
            "fc32": np.dtype(np.complex64),
            "fc64": np.dtype(np.complex128),
            "f32": np.dtype(np.float32),
            "f64": np.dtype(np.float64),
            "s16": np.dtype(np.int16),
        }.get(entry.cpu_format.lower(), np.dtype(np.complex64))
        channels = max(1, len(entry.stream_channels))
        batch_samps = max(
            1,
            min(
                _RX_PREFETCH_BATCH_SAMPS,
                _MAX_SAMPLE_PAYLOAD_BYTES // (dtype.itemsize * channels),
            ),
        )

        def run():
            buffer = np.empty((channels, batch_samps), dtype=dtype)
            while (
                not stop.is_set()
                and not entry.stop_event.is_set()
                and not session.closed
            ):
                metadata = metadata_type()
                try:
                    # Not under entry.lock: a recv blocks for a whole batch, and
                    # every sample frame's bookkeeping takes that lock. UHD
                    # streamers accept control calls while another thread recvs,
                    # and issue_stream_cmd stops this worker before it runs.
                    with entry.rx_recv_lock, self.admitted(session, entry, worker=True):
                        received = int(entry.value.recv(buffer, metadata, 0.1))
                except Exception:
                    break
                if stop.is_set() or entry.stop_event.is_set() or session.closed:
                    break
                snapshot = snapshot_native(metadata)
                error_code = str(snapshot.get("error_code", "none")).rsplit(
                    ".", 1
                )[-1].lower()
                has_error = error_code not in {"none", "error_code_none", "timeout"}
                if received <= 0 and not has_error:
                    continue
                received = max(0, min(received, batch_samps))
                chunk = (buffer[:, :received].copy(), snapshot)
                try:
                    entry.rx_prefetch.put_nowait(chunk)
                except queue.Full:
                    try:
                        entry.rx_prefetch.get_nowait()
                    except queue.Empty:
                        pass
                    entry.rx_prefetch_overflowed = True
                    try:
                        entry.rx_prefetch.put_nowait(chunk)
                    except queue.Full:
                        pass

        entry.rx_worker = threading.Thread(
            target=run,
            name=f"remoterf-{entry.handle[:16]}-rx-prefetch",
            daemon=True,
        )
        entry.rx_worker.start()

    def _stop_rx_prefetch(self, entry: HandleEntry) -> None:
        entry.rx_prefetch_stop.set()
        worker = entry.rx_worker
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=0.5)
        if worker is None or not worker.is_alive():
            entry.rx_worker = None
        self._clear_rx_prefetch(entry)

    @staticmethod
    def _stop_streamer(entry: HandleEntry) -> None:
        if not entry.type_id.endswith(("RXStreamer", "TXStreamer")):
            return
        acquired = entry.lock.acquire(timeout=0.01)
        if not acquired:
            return
        try:
            import uhd

            if entry.type_id.endswith("RXStreamer"):
                command = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
                entry.value.issue_stream_cmd(command)
                return
            metadata = uhd.types.TXMetadata()
            metadata.start_of_burst = False
            metadata.end_of_burst = True
            dtype = (
                np.complex128
                if entry.cpu_format.lower() == "fc64"
                else np.complex64
            )
            entry.value.send(np.empty(0, dtype=dtype), metadata, 0.1)
        except Exception:
            pass
        finally:
            entry.lock.release()

    @contextmanager
    def admitted(self, session, entry, lock=None, *, worker=False):
        # Lock order: per-entry/control lock -> short lifecycle bookkeeping.
        # No manager/lifecycle lock is held across a native call or cleanup wait.
        with lock if lock is not None else nullcontext(), hardware_access.native(session.gid):
            if not worker:
                self.get(session.session_id)
            with session.lifecycle_lock:
                if session.closed or entry.closed:
                    raise RemoteRFStaleHandleError("native admission is closed")
                ticket = nullcontext() if worker else session.activity.use()
                ticket.__enter__()
            try:
                yield
            finally:
                ticket.__exit__(None, None, None)

    @staticmethod
    def _workers_done(entry):
        return all(worker is None or not worker.is_alive()
                   for worker in (entry.worker, entry.rx_worker))

    def _dispose_entry(self, entry):
        self._stop_streamer(entry)
        close = getattr(entry.value, "close", None)
        try:
            if callable(close):
                close()
        finally:
            entry.value = None
            self._clear_rx_prefetch(entry)

    def close_handle(self, session_id: str, handle: str) -> bool:
        session = self.get(session_id, validate_reservation=False)
        if handle == session.root_handle:
            return self.close(session_id)
        with session.lifecycle_lock:
            pending = session.closing_handles.get(handle)
            if pending:
                return True  # attach to existing cleanup, never claim release
            entry = session.handles.get(handle)
            if entry is None or entry.closed:
                return False
            entry.closed = True
            entry.stop_event.set()
            entry.rx_prefetch_stop.set()
            session.handles.pop(handle)
            session.closing_handles[handle] = entry

        def dispose():
            try:
                self._dispose_entry(entry)
            except BaseException:
                session.cleanup_error = True
                with self._lock:
                    self._cleanup_failures += 1
                raise
            finally:
                with session.lifecycle_lock:
                    session.closing_handles.pop(handle, None)

        job = submit_cleanup(lambda: session.activity.idle() and self._workers_done(entry), dispose)
        job.done.wait(0.5)
        return True

    def close(self, session_id: str, *, wait=True) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                pending = self._closing_sessions.get(session_id)
                return pending is not None
            self._closing_sessions[session_id] = session
        with session.lifecycle_lock:
            session.closed = True
            entries = list(session.handles.values())
            session.retired_handles = entries
            session.handles.clear()
            for entry in entries:
                entry.closed = True
                entry.stop_event.set()
                entry.rx_prefetch_stop.set()

        try:
            self.integrations.session_closed(session_id)
        except Exception:
            # The independent runtime authority watcher still cancels closed
            # sessions; a notification failure cannot skip native cleanup.
            session.cleanup_error = True

        def ready():
            with session.lifecycle_lock:
                return (session.activity.idle() and not session.closing_handles
                        and all(self._workers_done(entry) for entry in entries))

        def dispose():
            errors = []
            try:
                for entry in entries:
                    if entry.handle != session.root_handle:
                        try:
                            self._dispose_entry(entry)
                        except BaseException as exc:
                            errors.append(exc)
                try:
                    session.schema.close_native_device()
                except BaseException as exc:
                    errors.append(exc)
                for entry in entries:
                    entry.value = None
                session.retired_handles.clear()
            finally:
                # Raw-only close errors retain historical reopen behavior.
                # Application leases use the stricter journal/verification gate.
                with self._lock:
                    self._closing_sessions.pop(session_id, None)
                    if self._session_by_gid.get(session.gid) == session_id:
                        self._session_by_gid.pop(session.gid, None)
            if errors:
                with self._lock:
                    self._cleanup_failures += len(errors)
                raise errors[0]

        def dispose_locked():
            with session.control_lock:
                return dispose()

        session.cleanup = submit_cleanup(ready, dispose_locked)
        if wait and session.cleanup.done.wait(0.5) and session.cleanup.error:
            raise session.cleanup.error
        return True

    def close_gid(self, gid: int, *, wait=True) -> None:
        with self._lock:
            gid = int(gid)
            self._epochs[gid] = self._epochs.get(gid, 0) + 1
            session_id = self._session_by_gid.get(gid)
        if session_id:
            self.close(session_id, wait=wait)

    def resume_admission(self):
        """Allow an in-process service restart only after old native work drains."""
        with self._lock:
            if not self._stopping:
                return
            if self._sessions or self._closing_sessions or self._opening_gids:
                raise RemoteRFReservationError("previous runtime still owns native resources")
            self._stopping = False

    def close_all(self, *, stop_admission=False) -> int:
        """Stop admission first; one failed cleanup cannot skip other radios."""
        with self._lock:
            self._stopping = self._stopping or stop_admission
            session_ids = list(self._sessions)
        if stop_admission:
            self.integrations.shutdown()
        for session_id in session_ids:
            try:
                self.close(session_id, wait=False)
            except Exception:
                # Scheduler overload or a bookkeeping failure must not skip
                # other devices. The closing record retains native references.
                with self._lock:
                    self._cleanup_failures += 1
        deadline = time.monotonic() + 0.5
        with self._lock:
            jobs = [s.cleanup for s in self._closing_sessions.values() if s.cleanup is not None]
        for job in jobs:
            job.done.wait(max(0.0, deadline - time.monotonic()))
        return len(session_ids)

    def invoke(self, session_id, handle, method_name, args, *, positional_args=None, overload_id=""):
        if method_name in INTEGRATION_METHODS:
            try:
                request = validate_arguments(args, positional_args, overload_id=overload_id)
                session = self.get(session_id)
                entry = self.get_handle(session, handle)
                if handle != session.root_handle or entry.type_id != "uhd.usrp.MultiUSRP":
                    raise IntegrationError("unsupported_device", "integration methods require the owning USRP root")
                if not any(item['owner'] == entry.type_id and item['name'] == method_name
                           for item in session.schema_v2['methods']):
                    raise IntegrationError("unsupported", "generic integration API is not declared by this driver")
                context = get_local_device_context(session.gid)
                if not context or context['dtype'] != 'usrp':
                    raise IntegrationError("unsupported_device", "integrations require a configured plain USRP")
                # Application control must remain reachable while the native
                # gate is owned. Never interpret these arguments/results as UHD.
                return validate_result(self.integrations.dispatch(self, session, method_name, request)), []
            except IntegrationError as exc:
                raise RemoteRFProtocolError(str(exc), details={"integration_code": exc.code}, method=method_name) from None
        session = self.get(session_id)
        entry = self.get_handle(session, handle)
        lock = session.control_lock if entry.type_id == "uhd.usrp.MultiUSRP" else entry.lock
        with self.admitted(session, entry, lock):
            return self._invoke_admitted(session_id, handle, method_name, args,
                                         positional_args=positional_args, overload_id=overload_id)

    def _invoke_admitted(
        self,
        session_id: str,
        handle: str,
        method_name: str,
        args: dict[str, Any],
        *,
        positional_args: list[Any] | None = None,
        overload_id: str = "",
    ) -> tuple[Any, list[tuple[str, Any]]]:
        session = self.get(session_id)
        entry = self.get_handle(session, handle)
        mutations: list[tuple[str, Any]] = []
        positional_args = list(positional_args or ())

        if entry.type_id == "uhd.usrp.MultiUSRP":
            descriptor = next(
                (
                    item for item in session.schema_v2["methods"]
                    if item["owner"] == entry.type_id and item["name"] == method_name
                ),
                None,
            )
            if descriptor is None:
                raise RemoteRFProtocolError(f"method is not declared by schema v2: {method_name}")
            _validate_overload(
                descriptor,
                overload_id,
                args,
                positional_args,
            )
            native_args = {
                key: self._resolve_handle_argument(session, value)
                for key, value in args.items()
            }
            native_positional = [
                self._resolve_handle_argument(session, value)
                for value in positional_args
            ]
            with session.control_lock:
                try:
                    result = session.schema.invoke_native(
                        method_name,
                        native_args,
                        positional_args=native_positional,
                    )
                except RemoteRFProtocolError:
                    raise
                except Exception as exc:
                    raise RemoteRFNativeUHDError(
                        str(exc),
                        native_exception=_uhd_type_name(exc),
                        uhd_version=session.capabilities.get("uhd_version", ""),
                        method=method_name,
                    ) from exc
            type_id = HANDLE_RETURN_METHODS.get(method_name)
            if type_id and result is not None:
                try:
                    child = self._publish_handle(session, result, type_id, parent=session.root_handle)
                finally:
                    result = None
                stream_args = args.get("stream_args")
                if stream_args is None and positional_args:
                    stream_args = positional_args[0]
                if isinstance(stream_args, dict):
                    child.stream_channels = [
                        int(item) for item in stream_args.get("channels", ())
                    ]
                    child.cpu_format = str(stream_args.get("cpu_format") or "")
                return {
                    "__remoterf_handle__": child.handle,
                    "__remoterf_type__": child.type_id,
                    "generation": child.generation,
                }, mutations
            self.get(session_id)
            return snapshot_native(result), mutations

        descriptor = next(
            (
                item
                for item in session.schema_v2["methods"]
                if item["owner"] == entry.type_id and item["name"] == method_name
            ),
            None,
        )
        if descriptor is None:
            raise RemoteRFProtocolError(
                f"method is not declared by schema v2: "
                f"{entry.type_id}.{method_name}"
            )
        _validate_overload(
            descriptor,
            overload_id,
            args,
            positional_args,
        )

        if method_name in {"recv", "send"}:
            raise RemoteRFProtocolError(
                f"{method_name} must use the SampleDataV1.SampleStream RPC"
            )
        if entry.type_id.endswith("TXStreamer") and method_name == "recv_async_msg":
            timeout = float(args.get("timeout", 0.1))
            mutate_metadata = "async_metadata" in args
            try:
                event = entry.async_events.get(timeout=max(0.0, timeout))
                if entry.async_overflowed:
                    event["remoterf_queue_overflow"] = True
                    entry.async_overflowed = False
                if mutate_metadata:
                    mutations.append(("async_metadata", event))
                    return True, mutations
                return event, mutations
            except queue.Empty:
                return (False if mutate_metadata else None), mutations

        target = getattr(entry.value, method_name, None)
        if not callable(target):
            raise RemoteRFProtocolError(
                f"{entry.type_id} does not expose method {method_name!r}"
            )
        native_args = {
            key: self._resolve_handle_argument(session, value)
            for key, value in args.items()
        }
        native_positional = [
            self._resolve_handle_argument(session, value)
            for value in positional_args
        ]
        rx_stream_command = (
            entry.type_id.endswith("RXStreamer")
            and method_name == "issue_stream_cmd"
        )
        stream_mode = ""
        if rx_stream_command:
            command = (
                native_positional[0]
                if native_positional
                else native_args.get("stream_cmd")
            )
            stream_mode = str(getattr(command, "mode", "")).rsplit(".", 1)[-1].lower()
            self._stop_rx_prefetch(entry)
        with entry.lock:
            try:
                result = target(*native_positional, **native_args)
            except Exception as exc:
                raise RemoteRFNativeUHDError(
                    str(exc),
                    native_exception=_uhd_type_name(exc),
                    uhd_version=session.capabilities.get("uhd_version", ""),
                    method=method_name,
                ) from exc
        if rx_stream_command and stream_mode in {"start_cont", "start_continuous"}:
            self._start_rx_prefetch(session, entry)
        snapshot = snapshot_native(result)
        return_type = HANDLE_METHOD_RETURN_TYPES.get(
            (entry.type_id, method_name)
        )
        if return_type and result is not None:
            try:
                child = self._publish_handle(session, result, return_type, parent=entry.handle)
            finally:
                result = None
            return {
                "__remoterf_handle__": child.handle,
                "__remoterf_type__": child.type_id,
                "generation": child.generation,
            }, mutations
        if (
            result is not None
            and not isinstance(result, (str, bool, int, float, complex, bytes, dict, list, tuple, np.ndarray, np.generic))
            and isinstance(snapshot, str)
        ):
            try:
                child = self._publish_handle(session, result, f"{entry.type_id}.{method_name}", parent=entry.handle)
            finally:
                result = None
            return {
                "__remoterf_handle__": child.handle,
                "__remoterf_type__": child.type_id,
                "generation": child.generation,
            }, mutations
        self.get(session_id)
        return snapshot, mutations


session_manager = SessionManager()
register_reservation_end_callback(lambda gid: session_manager.close_gid(gid, wait=False))


class DynamicControlV2Servicer(grpc_pb2_grpc.DynamicControlV2Servicer):
    def Negotiate(self, request, context):
        schema = SCHEMA_VERSION if SCHEMA_VERSION in request.schema_versions else ""
        control = (
            CONTROL_PROTOCOL_VERSION
            if CONTROL_PROTOCOL_VERSION in request.control_protocol_versions
            else ""
        )
        streaming = (
            STREAMING_PROTOCOL_VERSION
            if STREAMING_PROTOCOL_VERSION in request.streaming_protocol_versions
            else ""
        )
        if not schema or not control or not streaming:
            exc = RemoteRFProtocolError("no mutually supported Dynamic protocol version")
            return grpc_pb2.NegotiateResponse(error=error_envelope(exc))
        return grpc_pb2.NegotiateResponse(
            schema_version=schema,
            control_protocol_version=control,
            streaming_protocol_version=streaming,
        )

    def GetSchema(self, request, context):
        try:
            if SCHEMA_VERSION not in request.schema_versions:
                raise RemoteRFProtocolError("client did not advertise schema v2")
            if host_session_proxy.route_for_token(request.token) is not None:
                return host_session_proxy.get_schema(request)
            schema = session_manager.schema_for_token(request.token)
            return grpc_pb2.GetSchemaResponse(
                schema_version=SCHEMA_VERSION,
                schema_json=json.dumps(schema, sort_keys=True, separators=(",", ":")),
                schema_hash=schema["schema_hash"],
            )
        except Exception as exc:
            return grpc_pb2.GetSchemaResponse(error=error_envelope(exc))

    def OpenSession(self, request, context):
        try:
            if request.control_protocol_version != CONTROL_PROTOCOL_VERSION:
                raise RemoteRFProtocolError("unsupported control protocol version")
            if request.streaming_protocol_version != STREAMING_PROTOCOL_VERSION:
                raise RemoteRFProtocolError("unsupported streaming protocol version")
            if host_session_proxy.route_for_token(request.token) is not None:
                return host_session_proxy.open(request)
            session = session_manager.open(
                request.token,
                requested_schema_hash=request.requested_schema_hash,
            )
            return grpc_pb2.OpenSessionResponse(
                session_id=session.session_id,
                device_handle=session.root_handle,
                schema_json=json.dumps(
                    session.schema_v2, sort_keys=True, separators=(",", ":")
                ),
                schema_hash=session.schema_v2["schema_hash"],
                capabilities_json=json.dumps(
                    snapshot_native(session.capabilities),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                uhd_version=str(session.capabilities.get("uhd_version") or ""),
                uhd_abi=str(session.capabilities.get("uhd_abi") or ""),
                hardware_profile=str(
                    session.capabilities.get("hardware_profile") or "generic_usrp"
                ),
            )
        except Exception as exc:
            return grpc_pb2.OpenSessionResponse(error=error_envelope(exc))

    def Invoke(self, request, context):
        try:
            if request.method in INTEGRATION_METHODS:
                try:
                    application_request = decode_request(request)
                except IntegrationError as exc:
                    raise RemoteRFProtocolError(str(exc), details={"integration_code": exc.code}, method=request.method) from None
                if host_session_proxy.is_remote_session(request.session_id):
                    return host_session_proxy.invoke(request)
                result, _ = session_manager.invoke(request.session_id, request.handle, request.method,
                                                   {"request": application_request}, overload_id=request.overload_id)
                return grpc_pb2.InvokeResponse(result=encode_value(result))
            if host_session_proxy.is_remote_session(request.session_id):
                return host_session_proxy.invoke(request)
            args = {}
            for item in request.args:
                if not item.name or item.name in args:
                    raise RemoteRFProtocolError(
                        "invoke arguments must have unique, non-empty names"
                    )
                args[item.name] = decode_value(item.value)
            positional = args.pop("__args__", [])
            extra_kwargs = args.pop("__kwargs__", {})
            if not isinstance(positional, (list, tuple)):
                raise RemoteRFProtocolError("__args__ must be a list")
            if not isinstance(extra_kwargs, dict):
                raise RemoteRFProtocolError("__kwargs__ must be a mapping")
            if extra_kwargs:
                overlap = set(args) & set(extra_kwargs)
                if overlap:
                    raise RemoteRFProtocolError(
                        f"duplicate invoke keywords: {sorted(overlap)}"
                    )
                args.update(extra_kwargs)

            result, mutations = session_manager.invoke(
                request.session_id,
                request.handle,
                request.method,
                args,
                positional_args=positional,
                overload_id=request.overload_id,
            )
            response = grpc_pb2.InvokeResponse(result=encode_value(result))
            for target, value in mutations:
                response.mutations.add(target=target, value=encode_value(value))
            return response
        except Exception as exc:
            return grpc_pb2.InvokeResponse(
                error=error_envelope(exc, method=request.method)
            )

    def CloseHandle(self, request, context):
        try:
            if host_session_proxy.is_remote_session(request.session_id):
                return host_session_proxy.close_handle(request)
            return grpc_pb2.CloseResponse(
                closed=session_manager.close_handle(request.session_id, request.handle)
            )
        except Exception as exc:
            return grpc_pb2.CloseResponse(error=error_envelope(exc))

    def CloseSession(self, request, context):
        try:
            if host_session_proxy.is_remote_session(request.session_id):
                return host_session_proxy.close_session(request)
            return grpc_pb2.CloseResponse(
                closed=session_manager.close(request.session_id)
            )
        except Exception as exc:
            return grpc_pb2.CloseResponse(error=error_envelope(exc))


class SampleDataV1Servicer(grpc_pb2_grpc.SampleDataV1Servicer):
    def _error_frame(self, request, exc):
        return grpc_pb2.SampleFrame(
            session_id=request.session_id,
            handle=request.handle,
            generation=request.generation,
            operation_id=request.operation_id,
            sequence=request.sequence,
            direction=request.direction,
            kind=grpc_pb2.SAMPLE_FRAME_ERROR,
            error=error_envelope(exc),
            end_of_stream=True,
        )

    @staticmethod
    def _metadata_object(schema, tag: str, payload: str):
        try:
            value = json.loads(payload or "{}")
        except json.JSONDecodeError as exc:
            raise RemoteRFProtocolError("invalid sample metadata JSON") from exc
        value.setdefault("__uhd_type__", tag)
        # UsrpSchema's input codec owns construction against the installed UHD.
        from ..drivers.usrp.usrp_schema import _construct
        return _construct(value)

    @staticmethod
    def _finish_operation(entry: HandleEntry, operation_id: str) -> None:
        with entry.lock:
            if entry.active_operation == operation_id:
                entry.active_operation = ""

    @staticmethod
    def _recv_prefetched(entry: HandleEntry, buffer: np.ndarray, timeout: float):
        deadline = time.monotonic() + timeout
        count = 0
        metadata_snapshot = {"error_code": "none"}
        while count < buffer.shape[-1]:
            pending = entry.rx_pending
            if pending is None:
                remaining = max(0.0, deadline - time.monotonic())
                if count and remaining <= 0.0:
                    break
                try:
                    samples, snapshot = entry.rx_prefetch.get(
                        timeout=remaining if remaining > 0 else 0
                    )
                except queue.Empty:
                    if count == 0:
                        metadata_snapshot = {"error_code": "timeout"}
                    break
                pending = (samples, snapshot, 0)

            samples, snapshot, offset = pending
            available = samples.shape[-1] - offset
            take = min(buffer.shape[-1] - count, available)
            if take:
                if buffer.ndim == 1:
                    buffer[count : count + take] = samples[0, offset : offset + take]
                else:
                    buffer[:, count : count + take] = samples[:, offset : offset + take]
                count += take
                offset += take

            error_code = str(snapshot.get("error_code", "none")).rsplit(
                ".", 1
            )[-1].lower()
            has_error = error_code not in {"none", "error_code_none", "timeout"}
            if count == take or has_error:
                metadata_snapshot = dict(snapshot)
            entry.rx_pending = (
                (samples, snapshot, offset) if offset < samples.shape[-1] else None
            )
            if available <= 0:
                entry.rx_pending = None
            if has_error:
                break

        if entry.rx_prefetch_overflowed:
            metadata_snapshot = dict(metadata_snapshot)
            metadata_snapshot["error_code"] = "overflow"
            metadata_snapshot["error_code_repr"] = (
                "RemoteRF RX prefetch queue overflow"
            )
            entry.rx_prefetch_overflowed = False
        return count, metadata_snapshot

    def SampleStream(self, request_iterator, context):
        opened: tuple[DeviceSession, HandleEntry, str, int] | None = None
        iterator = iter(request_iterator)
        try:
            first_request = next(iterator)
        except StopIteration:
            return

        if host_session_proxy.is_remote_session(first_request.session_id):
            for request in chain((first_request,), iterator):
                try:
                    yield host_session_proxy.sample_frame(request)
                except Exception as exc:
                    yield self._error_frame(request, exc)
                    return
            return

        request_iterator = chain((first_request,), iterator)
        try:
            for request in request_iterator:
                try:
                    session = session_manager.get(request.session_id)
                    entry = session_manager.get_handle(
                        session,
                        request.handle,
                        generation=request.generation,
                    )
                    if request.kind == grpc_pb2.SAMPLE_FRAME_OPEN:
                        if not request.operation_id:
                            raise RemoteRFProtocolError(
                                "sample OPEN requires an operation id"
                            )
                        if not (
                            entry.type_id.endswith("RXStreamer")
                            or entry.type_id.endswith("TXStreamer")
                        ):
                            raise RemoteRFProtocolError("sample handle is not a streamer")
                        if request.direction not in {
                            grpc_pb2.SAMPLE_DIRECTION_RX,
                            grpc_pb2.SAMPLE_DIRECTION_TX,
                        }:
                            raise RemoteRFProtocolError(
                                "sample direction must be RX or TX"
                            )
                        if (
                            request.direction == grpc_pb2.SAMPLE_DIRECTION_RX
                            and not entry.type_id.endswith("RXStreamer")
                        ) or (
                            request.direction == grpc_pb2.SAMPLE_DIRECTION_TX
                            and not entry.type_id.endswith("TXStreamer")
                        ):
                            raise RemoteRFProtocolError(
                                "sample direction does not match the streamer type"
                            )
                        with entry.lock:
                            if entry.active_operation:
                                raise RemoteRFServerOverloadedError(
                                    "the streamer already has an active operation"
                                )
                            if int(request.sequence) <= entry.sequence:
                                raise RemoteRFProtocolError(
                                    "sample frame sequence is not monotonic"
                                )
                            entry.sequence = int(request.sequence)
                            entry.active_operation = request.operation_id
                        opened = (
                            session,
                            entry,
                            request.operation_id,
                            int(request.direction),
                        )
                        yield grpc_pb2.SampleFrame(
                            session_id=session.session_id,
                            handle=entry.handle,
                            generation=entry.generation,
                            operation_id=request.operation_id,
                            sequence=request.sequence,
                            direction=request.direction,
                            kind=grpc_pb2.SAMPLE_FRAME_RESULT,
                            credits=min(
                                _MAX_CREDITS,
                                max(1, int(request.credits or 1)),
                            ),
                        )
                        continue

                    if opened is None or opened[1].handle != entry.handle:
                        raise RemoteRFProtocolError("sample stream must begin with OPEN")
                    if opened[0].session_id != session.session_id:
                        raise RemoteRFProtocolError(
                            "sample session changed after OPEN"
                        )
                    if (
                        request.operation_id != opened[2]
                        or int(request.direction) != opened[3]
                    ):
                        raise RemoteRFProtocolError(
                            "sample operation id or direction changed after OPEN"
                        )
                    if int(request.sequence) <= entry.sequence:
                        raise RemoteRFProtocolError("sample frame sequence is not monotonic")
                    entry.sequence = int(request.sequence)

                    if request.kind == grpc_pb2.SAMPLE_FRAME_CANCEL:
                        self._finish_operation(entry, request.operation_id)
                        yield grpc_pb2.SampleFrame(
                            session_id=session.session_id,
                            handle=entry.handle,
                            generation=entry.generation,
                            operation_id=request.operation_id,
                            sequence=request.sequence,
                            direction=request.direction,
                            kind=grpc_pb2.SAMPLE_FRAME_RESULT,
                            cancelled=True,
                            end_of_stream=True,
                        )
                        continue

                    if request.kind == grpc_pb2.SAMPLE_FRAME_CLOSE:
                        self._finish_operation(entry, request.operation_id)
                        session_manager.close_handle(session.session_id, entry.handle)
                        yield grpc_pb2.SampleFrame(
                            session_id=session.session_id,
                            handle=entry.handle,
                            generation=request.generation,
                            operation_id=request.operation_id,
                            sequence=request.sequence,
                            direction=request.direction,
                            kind=grpc_pb2.SAMPLE_FRAME_RESULT,
                            end_of_stream=True,
                        )
                        return

                    dtype = _sample_dtype(request.dtype)
                    try:
                        shape = tuple(int(item) for item in request.shape)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise RemoteRFProtocolError(
                            "sample shape contains a non-integer dimension"
                        ) from exc
                    if len(shape) not in {1, 2} or any(item < 0 for item in shape):
                        raise RemoteRFProtocolError(
                            "sample shape must be one- or two-dimensional"
                        )
                    byte_count = math.prod(shape) * dtype.itemsize
                    if byte_count > _MAX_SAMPLE_PAYLOAD_BYTES:
                        raise RemoteRFProtocolError(
                            f"sample operation exceeds {_MAX_SAMPLE_PAYLOAD_BYTES} bytes"
                        )
                    if int(request.sample_count) != int(shape[-1]):
                        raise RemoteRFProtocolError(
                            "sample_count must match the final shape dimension"
                        )
                    if entry.stream_channels and list(request.channels) != entry.stream_channels:
                        raise RemoteRFProtocolError(
                            "sample channels do not match the stream handle"
                        )
                    if (
                        len(shape) == 2
                        and request.channels
                        and shape[0] != len(request.channels)
                    ):
                        raise RemoteRFProtocolError(
                            "sample channel dimension does not match channel metadata"
                        )
                    if (
                        len(shape) == 1
                        and len(request.channels) > 1
                    ):
                        raise RemoteRFProtocolError(
                            "multi-channel samples require a channel-major array"
                        )
                    expected_dtype = {
                        "fc32": np.dtype(np.complex64),
                        "fc64": np.dtype(np.complex128),
                        "f32": np.dtype(np.float32),
                        "f64": np.dtype(np.float64),
                        "s16": np.dtype(np.int16),
                    }.get(entry.cpu_format.lower())
                    if expected_dtype is not None and dtype != expected_dtype:
                        raise RemoteRFProtocolError(
                            f"sample dtype {dtype} does not match CPU format "
                            f"{entry.cpu_format}"
                        )

                    if (
                        request.direction == grpc_pb2.SAMPLE_DIRECTION_RX
                        and request.kind == grpc_pb2.SAMPLE_FRAME_REQUEST
                    ):
                        if not entry.type_id.endswith("RXStreamer"):
                            raise RemoteRFStaleHandleError("RX request used a non-RX handle")
                        buffer = np.empty(shape, dtype=dtype)
                        metadata = self._metadata_object(
                            session.schema, "RXMetadata", request.metadata_json
                        )
                        if request.one_packet:
                            raise RemoteRFProtocolError(
                                "one_packet is not exposed by the pinned "
                                "UHD 4.10 Python RXStreamer.recv binding"
                            )
                        timeout = _sample_timeout(request.timeout_sec)
                        deadline = time.monotonic() + timeout
                        count = 0
                        metadata_snapshot = snapshot_native(metadata)
                        if entry.rx_worker and entry.rx_worker.is_alive():
                            count, metadata_snapshot = self._recv_prefetched(
                                entry,
                                buffer,
                                timeout,
                            )
                        else:
                            with entry.lock, entry.rx_recv_lock, session_manager.admitted(session, entry):
                                while count < int(request.sample_count):
                                    remaining = max(0.0, deadline - time.monotonic())
                                    if count and remaining <= 0.0:
                                        break
                                    current_metadata = (
                                        metadata
                                        if count == 0
                                        else self._metadata_object(
                                            session.schema,
                                            "RXMetadata",
                                            request.metadata_json,
                                        )
                                    )
                                    target = buffer[..., count:]
                                    received = int(
                                        entry.value.recv(
                                            target,
                                            current_metadata,
                                            timeout if count == 0 else remaining,
                                        )
                                    )
                                    if received < 0 or received > target.shape[-1]:
                                        raise RemoteRFProtocolError(
                                            "native RX returned an invalid sample count"
                                        )
                                    current_snapshot = snapshot_native(current_metadata)
                                    error_code = str(
                                        current_snapshot.get("error_code", "none")
                                    ).rsplit(".", 1)[-1].lower()
                                    has_error = error_code not in {
                                        "none",
                                        "error_code_none",
                                    }
                                    if count == 0 or has_error:
                                        metadata = current_metadata
                                        metadata_snapshot = current_snapshot
                                    count += received
                                    if received == 0 or has_error:
                                        break
                        session_manager.get(session.session_id)
                        if count < 0 or count > int(request.sample_count):
                            raise RemoteRFProtocolError(
                                "native RX returned an invalid sample count"
                            )
                        sliced = buffer[..., :count] if buffer.ndim > 1 else buffer[:count]
                        payload = np.ascontiguousarray(sliced).tobytes(order="C")
                        time_spec = metadata_snapshot.get("time_spec")
                        if isinstance(time_spec, Mapping):
                            device_time = float(time_spec.get("secs", 0.0) or 0.0)
                        else:
                            device_time = _call_value(
                                getattr(metadata, "time_spec", None),
                                "get_real_secs",
                                0.0,
                            )
                        self._finish_operation(entry, request.operation_id)
                        yield grpc_pb2.SampleFrame(
                            session_id=session.session_id,
                            handle=entry.handle,
                            generation=entry.generation,
                            operation_id=request.operation_id,
                            sequence=request.sequence,
                            direction=request.direction,
                            kind=grpc_pb2.SAMPLE_FRAME_DATA,
                            dtype=dtype.str,
                            shape=list(sliced.shape),
                            channels=list(request.channels),
                            sample_count=count,
                            payload=payload,
                            metadata_json=json.dumps(
                                metadata_snapshot,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            device_time_secs=float(device_time or 0.0),
                            credits=1,
                        )
                        continue

                    if (
                        request.direction == grpc_pb2.SAMPLE_DIRECTION_TX
                        and request.kind == grpc_pb2.SAMPLE_FRAME_DATA
                    ):
                        if not entry.type_id.endswith("TXStreamer"):
                            raise RemoteRFStaleHandleError("TX data used a non-TX handle")
                        if len(request.payload) != byte_count:
                            raise RemoteRFProtocolError(
                                "TX payload byte length does not match dtype/shape"
                            )
                        samples = np.frombuffer(request.payload, dtype=dtype).reshape(shape)
                        metadata = self._metadata_object(
                            session.schema, "TXMetadata", request.metadata_json
                        )
                        timeout = _sample_timeout(request.timeout_sec)
                        with entry.lock, session_manager.admitted(session, entry):
                            count = int(
                                entry.value.send(
                                    samples,
                                    metadata,
                                    timeout,
                                )
                            )
                        session_manager.get(session.session_id)
                        if count < 0 or count > int(request.sample_count):
                            raise RemoteRFProtocolError(
                                "native TX returned an invalid sample count"
                            )
                        self._finish_operation(entry, request.operation_id)
                        yield grpc_pb2.SampleFrame(
                            session_id=session.session_id,
                            handle=entry.handle,
                            generation=entry.generation,
                            operation_id=request.operation_id,
                            sequence=request.sequence,
                            direction=request.direction,
                            kind=grpc_pb2.SAMPLE_FRAME_RESULT,
                            sample_count=count,
                            credits=1,
                        )
                        continue

                    raise RemoteRFProtocolError("unsupported sample frame transition")
                except Exception as exc:
                    if opened is not None:
                        self._finish_operation(opened[1], opened[2])
                    yield self._error_frame(request, exc)
                    return
        finally:
            if opened is not None:
                self._finish_operation(opened[1], opened[2])
