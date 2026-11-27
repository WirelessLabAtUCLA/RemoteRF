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

# Last edit: Jun 26 16:37

from remoterf_device_core.freq_ranges import (
    FreqLimits,
    parse_freq_limits,
    strip_freq_limit_keys,
)
from remoterf_device_core.idl import DeviceSchema, idl_client, idl_expose, idl_register
from remoterf_device_core.drivers.usrp.uhd_manifest import build_usrp_schema_v2
from remoterf_device_core.drivers.usrp.profiles import (
    infer_inventory_profile,
    resolve_live_profile,
    validate_profile_inventory,
)
from remoterf_device_core.v2_errors import RemoteRFPolicyError

c = idl_client

import itertools
import logging
import math
import os
import re
import subprocess
import sys
import threading
from collections.abc import Mapping
from numbers import Real

_log = logging.getLogger(__name__)


def _configure_uhd_console_logging():
    """Keep native UHD logs out of the interactive server console by default."""
    os.environ["UHD_LOG_CONSOLE_LEVEL"] = os.getenv(
        "REMOTERF_UHD_CONSOLE_LEVEL",
        "off",
    )


_configure_uhd_console_logging()

try:
    import uhd
except Exception as exc:  # UHD is optional until a configured USRP session opens.
    uhd = None
    _UHD_IMPORT_ERROR = exc
else:
    _UHD_IMPORT_ERROR = None


_STREAM_USAGE = (
    "Use remoterf.uhd.get_rx_stream(usrp, stream_args) or "
    "remoterf.uhd.get_tx_stream(usrp, stream_args) on the client. "
    "If using the generated client directly, first call get_rx_stream/get_tx_stream "
    "and then pass {'handle': streamer_payload['handle'], ...} to stream_* calls."
)

# UHD 4.10's public API documents these OTW tokens, but does not expose a
# per-device enumeration call.  They are candidates only: support is decided
# by asking the connected device to construct an inactive native RX streamer.
_UHD_OTW_FORMAT_CANDIDATES = ("sc16", "sc12", "sc8")
_OTW_PROBE_CPU_FORMAT = "fc32"


def _require_uhd():
    if uhd is None:
        message = (
            "USRP support requires the UHD 4.10 Python module on the machine "
            "that owns the hardware"
        )
        if _UHD_IMPORT_ERROR is not None:
            message += f": {_UHD_IMPORT_ERROR}"
        raise RuntimeError(message) from _UHD_IMPORT_ERROR
    return uhd


def _looks_like_device_args(value):
    return isinstance(value, str) and ("=" in value or "," in value)


def _device_args(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        return ",".join(
            f"{key}={item}" for key, item in value.items() if item is not None
        )
    return str(value)


def _remote_transport_args(value):
    """Add buffering for UI/processing gaps between remote RX batches."""
    args = _device_args(value).strip().strip(",")
    keys = {
        item.split("=", 1)[0].strip().lower()
        for item in args.split(",")
        if "=" in item
    }
    if "num_recv_frames" in keys:
        return args

    raw_frames = os.getenv("REMOTERF_USRP_NUM_RECV_FRAMES", "256")
    try:
        frames = int(raw_frames)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "REMOTERF_USRP_NUM_RECV_FRAMES must be a positive integer"
        ) from exc
    if frames <= 0:
        raise ValueError(
            "REMOTERF_USRP_NUM_RECV_FRAMES must be a positive integer"
        )
    setting = f"num_recv_frames={frames}"
    return f"{args},{setting}" if args else setting


def _usrp_args(args=None, serial=None, addr=None, **kwargs):
    kwargs = strip_freq_limit_keys(kwargs)
    args = args or kwargs.pop("device_args", None) or kwargs.pop("init_args", None)
    type_hint = kwargs.pop("type", None) or kwargs.pop("device_type", None)
    profile_name = kwargs.pop("profile", None)

    if args is None and _looks_like_device_args(serial):
        args = serial
        serial = None

    if args is None and _looks_like_device_args(addr):
        args = addr
        addr = None

    if args is None:
        inventory = {
            "profile": profile_name,
            "type": type_hint,
            "serial": serial,
            "addr": addr,
        }
        profile = infer_inventory_profile(inventory)
        if profile_name:
            validate_profile_inventory(profile, inventory)
        resolved_type = type_hint or profile.uhd_device_type
        parts = [f"type={resolved_type}"] if resolved_type else []
        if serial:
            parts.append(f"serial={serial}")
        if addr:
            parts.append(f"addr={addr}")
        for key, value in kwargs.items():
            if value is not None:
                parts.append(f"{key}={value}")
        args = ",".join(parts)
    elif profile_name:
        inventory = {
            "profile": profile_name,
            "type": type_hint,
            "args": args,
        }
        profile = infer_inventory_profile(inventory)
        validate_profile_inventory(profile, inventory)
        arg_text = _device_args(args).strip().strip(",")
        arg_keys = {
            item.split("=", 1)[0].strip().lower()
            for item in arg_text.split(",")
            if "=" in item
        }
        if profile.uhd_device_type and "type" not in arg_keys:
            args = f"type={profile.uhd_device_type},{arg_text}"

    return _remote_transport_args(args)


# A cold B2xx open (firmware + FPGA image) took ~68 s inside MultiUSRP(),
# which holds the GIL, so the whole lab stopped answering. A child process
# pays that instead while this one waits without the GIL; the open after it
# is warm (~2.5 s).
# ponytail: "warm" is per process; a USB replug while the Server runs makes
# the next open cold in-process again. Clear the mark on hotplug if that bites.
_PRELOAD_TIMEOUT = 180.0
_preloaded = set()
_preload_locks = {}
_preload_guard = threading.Lock()


def _preload_image(args):
    if str(os.getenv("REMOTERF_USRP_PRELOAD", "1")).strip().lower() in {"0", "false", "no", "off"}:
        return
    with _preload_guard:
        lock = _preload_locks.setdefault(args, threading.Lock())
    # One device, one driver at a time: an open waits here for a pre-load.
    with lock:
        if args in _preloaded:
            return
        try:
            done = subprocess.run(
                [sys.executable, "-c", "import sys, uhd; uhd.usrp.MultiUSRP(sys.argv[1])", args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_PRELOAD_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"Warning: USRP image pre-load for {args!r} failed: {exc}")
            return
        if done.returncode == 0:
            _preloaded.add(args)
        else:
            print(f"Warning: USRP image pre-load for {args!r} exited {done.returncode}")


def _connect_usrp(args=None, serial=None, addr=None, **kwargs):
    uhd_module = _require_uhd()
    args = _usrp_args(args, serial, addr, **kwargs)
    _preload_image(args)
    dev = uhd_module.usrp.MultiUSRP(args)
    _preloaded.add(args)
    return dev


def _payload_type(value):
    if isinstance(value, dict):
        return value.get("__uhd_type__")
    return None


def _tune_request_policy(value, current):
    """Translate a portable policy name back to UHD's native enum value."""
    if not isinstance(value, str):
        return value
    name = value.rsplit(".", 1)[-1].upper()
    policy_types = (
        type(current),
        getattr(getattr(uhd, "types", None), "TuneRequestPolicy", None),
        getattr(getattr(uhd, "types", None), "tune_request_policy", None),
        getattr(
            getattr(getattr(uhd, "libpyuhd", None), "types", None),
            "tune_request_policy",
            None,
        ),
    )
    for policy_type in policy_types:
        if policy_type is None:
            continue
        for candidate in (name, name.lower(), f"POLICY_{name}"):
            native = getattr(policy_type, candidate, None)
            if native is not None:
                return native
    return value


def _require_dict(value, method):
    if not isinstance(value, dict):
        raise TypeError(
            f"{method} expects a dict payload. Got {type(value).__name__}. "
            f"{_STREAM_USAGE}"
        )


def _require_key(value, key, method):
    if key not in value:
        raise KeyError(
            f"{method} payload is missing {key!r}. {_STREAM_USAGE}"
        )


def _handle(value, method):
    if isinstance(value, dict):
        _require_key(value, "handle", method)
        value = value["handle"]
    if not isinstance(value, str):
        raise TypeError(
            f"{method} expects a streamer handle string. "
            f"Got {type(value).__name__}. {_STREAM_USAGE}"
        )
    return value


def _time_spec_payload(value):
    return {
        "__uhd_type__": "TimeSpec",
        "secs": value.get_real_secs(),
    }


def _construct(value):
    if isinstance(value, dict) and value.get("__remoterf_uhd_helper__") == "stream":
        return _construct(value["stream_args"])

    kind = _payload_type(value)
    if kind is None and isinstance(value, dict):
        if "cpu_format" in value and "otw_format" in value:
            kind = "StreamArgs"
        elif "secs" in value:
            kind = "TimeSpec"
        elif "spec" in value:
            kind = "SubdevSpec"
        elif "target_freq" in value or "freq" in value:
            uhd_module = _require_uhd()
            return uhd_module.libpyuhd.types.tune_request(
                value.get("target_freq", value.get("freq"))
            )

    if kind is None:
        return value

    _require_uhd()

    if kind == "SubdevSpec":
        return uhd.usrp.SubdevSpec(value["spec"])

    if kind == "TuneRequest":
        # Older generated clients used ``args`` for constructor arguments.
        # Current TuneRequest snapshots use that same key for UHD's DeviceAddr
        # field, so only sequences may be interpreted as constructor arguments.
        raw_args = value.get("args", ())
        legacy_constructor_args = isinstance(raw_args, (list, tuple))
        args = (
            [_construct(item) for item in raw_args]
            if legacy_constructor_args
            else []
        )
        kwargs = {
            key: _construct(item)
            for key, item in value.get("kwargs", {}).items()
        }
        constructor = getattr(getattr(uhd, "types", None), "TuneRequest", None)
        if constructor is None:
            constructor = uhd.libpyuhd.types.tune_request
        if not args and "target_freq" in value:
            args = [value["target_freq"]]
            if value.get("lo_offset") is not None:
                args.append(value["lo_offset"])
        obj = constructor(*args, **kwargs)
        # TuneRequest(f, lo) places the RF LO manually at f + lo. Clients before
        # this fix modelled only the offset and send AUTO / 0.0 for the RF
        # fields next to it; applied, those defaults would undo the offset.
        keep_rf = (
            value.get("lo_offset") is not None
            and str(value.get("rf_freq_policy", "")).rsplit(".", 1)[-1].upper() == "AUTO"
            and not value.get("rf_freq")
        )
        for field in (
            "rf_freq", "dsp_freq", "rf_freq_policy", "dsp_freq_policy",
            "args",
        ):
            if field == "args" and legacy_constructor_args:
                continue
            if keep_rf and field in {"rf_freq", "rf_freq_policy"}:
                continue
            if field in value and hasattr(obj, field):
                converted = _construct(value[field])
                if field in {"rf_freq_policy", "dsp_freq_policy"}:
                    converted = _tune_request_policy(
                        converted,
                        getattr(obj, field),
                    )
                setattr(obj, field, converted)
        return obj

    if kind == "DeviceAddr":
        constructor = getattr(getattr(uhd, "types", None), "DeviceAddr", None)
        if constructor is None:
            constructor = uhd.libpyuhd.types.device_addr
        raw = value.get("value", value.get("items", value.get("args", "")))
        if isinstance(raw, dict):
            raw = _device_args(raw)
        return constructor(raw)

    if kind == "TimeSpec":
        return uhd.types.TimeSpec(value["secs"])

    if kind == "StreamArgs":
        obj = uhd.usrp.StreamArgs(value["cpu_format"], value["otw_format"])
        obj.channels = value.get("channels", [])
        raw_args = value.get("args")
        if raw_args:
            # Dynamic v2 represents StreamArgs.args as a tagged DeviceAddr.
            # Preserve that type instead of flattening the envelope itself
            # into invalid keys such as ``__uhd_type__`` and ``items``.
            obj.args = (
                _construct(raw_args)
                if _payload_type(raw_args) == "DeviceAddr"
                else _device_args(raw_args)
            )
        return obj

    if kind == "TXMetadata":
        return _tx_metadata(value)

    if kind == "RXMetadata":
        metadata = uhd.types.RXMetadata()
        reset = getattr(metadata, "reset", None)
        if callable(reset):
            reset()
        return metadata

    if kind in {"TXAsyncMetadata", "AsyncMetadata"}:
        constructor = getattr(uhd.types, "TXAsyncMetadata", None)
        if constructor is None:
            constructor = getattr(uhd.types, "AsyncMetadata")
        return constructor()

    if kind == "FilterInfoBase":
        filter_type = getattr(
            uhd.filters.FilterType,
            str(value["filter_type"]).split(".")[-1],
        )
        native_class = str(value.get("native_class") or "filter_info_base")
        common = [
            filter_type,
            bool(value.get("bypassed", False)),
            int(value.get("position", 0)),
        ]
        if native_class == "analog_filter_lp":
            return uhd.filters.AnalogFilterLP(
                *common,
                str(value.get("analog_type", "")),
                float(value["cutoff"]),
                float(value["rolloff"]),
            )
        if native_class == "analog_filter_base":
            return uhd.filters.AnalogFilterBase(
                *common,
                str(value.get("analog_type", "")),
            )
        if native_class in {
            "digital_filter_base_i16",
            "digital_filter_fir_i16",
        }:
            constructor = (
                uhd.filters.DigitalFilterFIRI16
                if native_class == "digital_filter_fir_i16"
                else uhd.filters.DigitalFilterBaseI16
            )
            return constructor(
                *common,
                float(value["input_rate"]),
                int(value["interpolation"]),
                int(value["decimation"]),
                int(value["tap_full_scale"]),
                int(value["max_num_taps"]),
                [int(item) for item in value.get("taps", ())],
            )
        return uhd.filters.FilterInfoBase(*common)

    if kind == "StreamCMD":
        mode_name = value["mode"]
        try:
            mode = getattr(uhd.types.StreamMode, mode_name)
        except AttributeError:
            raise ValueError(
                f"Unsupported StreamCMD mode {mode_name!r}. "
                "Use one of: start_cont, stop_cont, num_done, num_more."
            )
        obj = uhd.types.StreamCMD(mode)
        obj.stream_now = value.get("stream_now", True)
        if value.get("time_spec") is not None:
            obj.time_spec = _construct(value["time_spec"])
        if value.get("num_samps") is not None:
            obj.num_samps = value["num_samps"]
        return obj

    raise ValueError(f"Unknown UHD payload type: {kind}")


def _rx_metadata_payload(metadata):
    time_spec = None
    if hasattr(metadata, "time_spec"):
        time_spec = _time_spec_payload(metadata.time_spec)

    return {
        "error_code": str(metadata.error_code).split(".")[-1],
        "error_code_repr": str(metadata.error_code),
        "has_time_spec": getattr(metadata, "has_time_spec", None),
        "time_spec": time_spec,
        "out_of_sequence": getattr(metadata, "out_of_sequence", None),
        "fragment_offset": getattr(metadata, "fragment_offset", None),
        "more_fragments": getattr(metadata, "more_fragments", None),
    }


def _tx_metadata(value):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        return value

    _require_uhd()
    metadata = uhd.types.TXMetadata()
    metadata.has_time_spec = bool(value.get("has_time_spec", False))
    if value.get("time_spec") is not None:
        metadata.time_spec = _construct(value["time_spec"])
    metadata.start_of_burst = bool(value.get("start_of_burst", False))
    metadata.end_of_burst = bool(value.get("end_of_burst", False))
    return metadata


def _uhd_client_objects():
    time_spec = c.payload_class(
        "TimeSpec",
        {"secs": {"default": 0.0, "coerce": "float"}},
        init="timespec",
        methods=[
            c.method("timespec_get_real_secs"),
            c.method("timespec_get_full_secs"),
            c.method("timespec_get_frac_secs"),
            c.method("timespec_to_ticks"),
            c.method("timespec_get_tick_count"),
            c.method("timespec_float"),
            c.method("payload_as_payload"),
        ],
        operators=["timespec_numeric"],
    )

    stream_args = c.payload_class(
        "StreamArgs",
        {
            "cpu_format": {"coerce": "str"},
            "otw_format": {"coerce": "str"},
            "args": {"default": {}, "payload": True},
            "channels": {"default": [], "coerce": "list"},
        },
        init="stream_args",
        methods=[c.method("payload_as_payload")],
    )

    stream_cmd = c.payload_class(
        "StreamCMD",
        {
            "mode": {"coerce": "enum_value"},
            "stream_now": {"default": True, "coerce": "bool"},
            "time_spec": {"default": None, "payload": True},
            "num_samps": {"default": None},
        },
        init="stream_cmd",
        methods=[c.method("payload_as_payload")],
    )

    rx_metadata = c.strict_class(
        [
            "error_code",
            "error_code_repr",
            "time_spec",
            "out_of_sequence",
            "fragment_offset",
            "more_fragments",
        ],
        init="rx_metadata",
        methods=[
            c.method("metadata_update"),
            c.method("metadata_strerror"),
        ],
    )

    tx_metadata = c.strict_class(
        ["has_time_spec", "time_spec", "end_of_burst"],
        init="tx_metadata",
        methods=[c.method("payload_as_payload")],
    )

    streamer_methods = [
        c.method("streamer_get_max_num_samps"),
        c.method("streamer_issue_stream_cmd"),
        c.method("streamer_close"),
    ]

    rx_streamer = c.proxy_class(
        ["usrp", "handle"],
        init="streamer_handle",
        methods=[*streamer_methods, c.method("rx_streamer_recv")],
    )

    tx_streamer = c.proxy_class(
        ["usrp", "handle"],
        init="streamer_handle",
        methods=[*streamer_methods, c.method("tx_streamer_send")],
    )

    return {
        "uhd": c.module(
            {
                "payload": c.function("uhd_payload"),
                "get_rx_stream": c.function("uhd_get_rx_stream"),
                "get_tx_stream": c.function("uhd_get_tx_stream"),
                "close_all_streams": c.function("uhd_close_all_streams"),
                "streamer": c.function("uhd_streamer"),
                "RXStreamer": rx_streamer,
                "TXStreamer": tx_streamer,
                "usrp": c.namespace(
                    {
                        "SubdevSpec": c.function("uhd_subdev_spec"),
                        "StreamArgs": stream_args,
                    }
                ),
                "types": c.namespace(
                    {
                        "TimeSpec": time_spec,
                        "StreamCMD": stream_cmd,
                        "StreamMode": c.enum(
                            ["num_done", "num_more", "stop_cont", "start_cont"]
                        ),
                        "RXMetadata": rx_metadata,
                        "TXMetadata": tx_metadata,
                        "RXMetadataErrorCode": c.enum(
                            [
                                "none",
                                "timeout",
                                "overflow",
                                "late_command",
                                "broken_chain",
                                "alignment",
                                "bad_packet",
                            ]
                        ),
                    }
                ),
                "libpyuhd": c.namespace(
                    {
                        "types": c.namespace(
                            {
                                "tune_request": c.function("uhd_tune_request"),
                            }
                        ),
                    }
                ),
            },
            exports=[
                "usrp",
                "types",
                "libpyuhd",
                "payload",
                "get_rx_stream",
                "get_tx_stream",
                "close_all_streams",
                "streamer",
                "RXStreamer",
                "TXStreamer",
            ],
            bind_client_class_to="usrp",
        )
    }


def _to_finite_float(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _policy_name(value) -> str | None:
    if value is None:
        return "AUTO"
    if isinstance(value, bool):
        return None
    raw = getattr(value, "name", None)
    text = raw if isinstance(raw, str) else str(value)
    name = text.rsplit(".", 1)[-1].upper()
    if name.startswith("POLICY_"):
        name = name[len("POLICY_"):]
    return name if name in {"AUTO", "MANUAL", "NONE"} else None


def _finite_bounds(low: float, high: float) -> tuple[float, float] | None:
    if not (math.isfinite(low) and math.isfinite(high)):
        return None
    return (low, high) if low <= high else (high, low)


def _has_nonempty_tune_args(args) -> bool:
    if args is None:
        return False
    if isinstance(args, str):
        return bool(args.strip())
    if isinstance(args, Mapping):
        if args.get("__uhd_type__") == "DeviceAddr":
            if "value" in args:
                raw = args["value"]
            elif "items" in args:
                raw = args["items"]
            elif "args" in args:
                raw = args["args"]
            else:
                return bool(set(args) - {"__uhd_type__"})
            return _has_nonempty_tune_args(raw)
        return len(args) > 0
    to_string = getattr(args, "to_string", None)
    keys = getattr(args, "keys", None)
    if callable(to_string) or callable(keys):
        try:
            if callable(keys) and len(list(keys())) > 0:
                return True
            if callable(to_string) and str(to_string()).strip():
                return True
            return False
        except Exception:
            return True
    return True


def _time_spec_secs(value) -> float | None:
    if isinstance(value, Mapping):
        return _to_finite_float(value.get("secs"))
    getter = getattr(value, "get_real_secs", None)
    if callable(getter):
        try:
            return _to_finite_float(getter())
        except Exception:
            return None
    if hasattr(value, "secs"):
        return _to_finite_float(getattr(value, "secs"))
    return None


def _extract_explicit_channels(raw_channels) -> list[int] | None:
    if raw_channels is None or isinstance(raw_channels, (str, bytes)):
        return None
    try:
        items = list(raw_channels)
    except TypeError:
        return None
    if not items:
        return None
    channels: list[int] = []
    for item in items:
        if isinstance(item, bool):
            return None
        try:
            channels.append(int(item))
        except (TypeError, ValueError):
            return None
    return channels


def _extract_tune_bounds(
    tune_request,
    *,
    side: str,
    current_freq: float | None = None,
    current_rf: float | None = None,
) -> tuple[tuple[float, float], tuple[float, float] | None] | None:
    seen: set[int] = set()
    while (
        isinstance(tune_request, Mapping)
        and tune_request.get("__remoterf_uhd_helper__") == "stream"
    ):
        obj_id = id(tune_request)
        if obj_id in seen or "stream_args" not in tune_request:
            return None
        seen.add(obj_id)
        tune_request = tune_request["stream_args"]

    if isinstance(tune_request, bool):
        return None
    if isinstance(tune_request, Real):
        val = _to_finite_float(tune_request)
        return ((val, val), (val, val)) if val is not None else None

    if isinstance(tune_request, Mapping):
        kind = tune_request.get("__uhd_type__")
        if kind is None:
            if (
                ("cpu_format" in tune_request and "otw_format" in tune_request)
                or "secs" in tune_request
                or "spec" in tune_request
            ):
                return None
            if "target_freq" in tune_request or "freq" in tune_request:
                raw = (
                    tune_request["target_freq"]
                    if "target_freq" in tune_request
                    else tune_request["freq"]
                )
                val = _to_finite_float(raw)
                return ((val, val), (val, val)) if val is not None else None
            return None
        if kind != "TuneRequest":
            return None

        raw_args = tune_request.get("args", ())
        legacy_constructor_args = isinstance(raw_args, (list, tuple))
        if not legacy_constructor_args and _has_nonempty_tune_args(raw_args):
            return None
        raw_kwargs = tune_request.get("kwargs")
        if raw_kwargs is not None and not isinstance(raw_kwargs, Mapping):
            return None
        kw = dict(raw_kwargs or {})
        if not set(kw).issubset({"target_freq", "lo_offset"}):
            return None
        pos = list(raw_args) if legacy_constructor_args else []
        if len(pos) > 2:
            return None

        has_target = False
        raw_target = None
        raw_lo_offset = None
        if pos:
            if "target_freq" in kw or (len(pos) >= 2 and "lo_offset" in kw):
                return None
            has_target = True
            raw_target = pos[0]
            raw_lo_offset = pos[1] if len(pos) >= 2 else kw.get("lo_offset")
        elif "target_freq" in tune_request:
            if "target_freq" in kw or (
                tune_request.get("lo_offset") is not None and "lo_offset" in kw
            ):
                return None
            has_target = True
            raw_target = tune_request["target_freq"]
            raw_lo_offset = (
                tune_request.get("lo_offset")
                if tune_request.get("lo_offset") is not None
                else kw.get("lo_offset")
            )
        else:
            if "target_freq" in kw:
                has_target = True
                raw_target = kw["target_freq"]
            raw_lo_offset = kw.get("lo_offset")

        target_freq = _to_finite_float(raw_target) if has_target else None
        if has_target and target_freq is None:
            return None

        if raw_lo_offset is not None:
            lo_offset = _to_finite_float(raw_lo_offset)
            if lo_offset is None or target_freq is None:
                return None
            rf_policy: str | None = "MANUAL"
            rf_freq: float | None = target_freq + lo_offset
            rf_freq_set = True
        else:
            rf_policy = "AUTO"
            rf_freq = None
            rf_freq_set = False
        dsp_policy: str | None = "AUTO"
        dsp_freq: float | None = 0.0

        keep_rf = (
            tune_request.get("lo_offset") is not None
            and str(tune_request.get("rf_freq_policy", "")).rsplit(".", 1)[-1].upper() == "AUTO"
            and not tune_request.get("rf_freq")
        )
        if "rf_freq_policy" in tune_request and not keep_rf:
            if tune_request["rf_freq_policy"] is None:
                return None
            rf_policy = _policy_name(tune_request["rf_freq_policy"])
        if "rf_freq" in tune_request and not keep_rf:
            rf_freq = _to_finite_float(tune_request["rf_freq"])
            rf_freq_set = True
        if "dsp_freq_policy" in tune_request:
            if tune_request["dsp_freq_policy"] is None:
                return None
            dsp_policy = _policy_name(tune_request["dsp_freq_policy"])
        if "dsp_freq" in tune_request:
            dsp_freq = _to_finite_float(tune_request["dsp_freq"])
    else:
        if not any(
            hasattr(tune_request, attr)
            for attr in ("target_freq", "rf_freq_policy", "dsp_freq_policy")
        ):
            return None
        if hasattr(tune_request, "args") and _has_nonempty_tune_args(
            getattr(tune_request, "args", None)
        ):
            return None
        target_freq = _to_finite_float(getattr(tune_request, "target_freq", None))
        if hasattr(tune_request, "rf_freq_policy"):
            raw_rf_policy = getattr(tune_request, "rf_freq_policy")
            rf_policy = None if raw_rf_policy is None else _policy_name(raw_rf_policy)
        else:
            rf_policy = "AUTO"
        if hasattr(tune_request, "dsp_freq_policy"):
            raw_dsp_policy = getattr(tune_request, "dsp_freq_policy")
            dsp_policy = None if raw_dsp_policy is None else _policy_name(raw_dsp_policy)
        else:
            dsp_policy = "AUTO"
        rf_freq = _to_finite_float(getattr(tune_request, "rf_freq", None))
        rf_freq_set = hasattr(tune_request, "rf_freq")
        dsp_freq = _to_finite_float(getattr(tune_request, "dsp_freq", None))

    if rf_policy is None or dsp_policy is None:
        return None

    if rf_policy == "AUTO" and dsp_policy == "AUTO":
        if target_freq is None:
            return None
        bounds = _finite_bounds(target_freq, target_freq)
        return (bounds, bounds) if bounds is not None else None
    if rf_policy == "AUTO" and dsp_policy == "MANUAL":
        if target_freq is None or dsp_freq is None:
            return None
        dsp = abs(dsp_freq)
        bounds = _finite_bounds(target_freq - dsp, target_freq + dsp)
        rf_bounds = _finite_bounds(target_freq, target_freq)
        return (bounds, rf_bounds) if bounds is not None and rf_bounds is not None else None
    if rf_policy == "AUTO" and dsp_policy == "NONE":
        if target_freq is None or current_freq is None or current_rf is None:
            return None
        freq = target_freq + (current_freq - current_rf)
        bounds = _finite_bounds(freq, freq)
        rf_bounds = _finite_bounds(target_freq, target_freq)
        return (bounds, rf_bounds) if bounds is not None and rf_bounds is not None else None
    if rf_policy == "MANUAL" and dsp_policy == "AUTO":
        if not rf_freq_set or rf_freq is None or target_freq is None:
            return None
        bounds = _finite_bounds(rf_freq, target_freq)
        rf_bounds = _finite_bounds(rf_freq, rf_freq)
        return (bounds, rf_bounds) if bounds is not None and rf_bounds is not None else None
    if rf_policy == "MANUAL" and dsp_policy == "MANUAL":
        if not rf_freq_set or rf_freq is None or dsp_freq is None:
            return None
        dsp = abs(dsp_freq)
        bounds = _finite_bounds(rf_freq - dsp, rf_freq + dsp)
        rf_bounds = _finite_bounds(rf_freq, rf_freq)
        return (bounds, rf_bounds) if bounds is not None and rf_bounds is not None else None
    if rf_policy == "MANUAL" and dsp_policy == "NONE":
        if not rf_freq_set or rf_freq is None or current_freq is None or current_rf is None:
            return None
        freq = rf_freq + (current_freq - current_rf)
        bounds = _finite_bounds(freq, freq)
        rf_bounds = _finite_bounds(rf_freq, rf_freq)
        return (bounds, rf_bounds) if bounds is not None and rf_bounds is not None else None
    if rf_policy == "NONE" and dsp_policy == "AUTO":
        if current_rf is None or target_freq is None:
            return None
        bounds = _finite_bounds(current_rf, target_freq)
        return (bounds, None) if bounds is not None else None
    if rf_policy == "NONE" and dsp_policy == "MANUAL":
        if current_rf is None or dsp_freq is None:
            return None
        dsp = abs(dsp_freq)
        bounds = _finite_bounds(current_rf - dsp, current_rf + dsp)
        return (bounds, None) if bounds is not None else None
    if rf_policy == "NONE" and dsp_policy == "NONE":
        if current_freq is None:
            return None
        bounds = _finite_bounds(current_freq, current_freq)
        return (bounds, None) if bounds is not None else None
    return None


def _extract_tune_freq_bounds(
    tune_request,
    *,
    side: str,
    current_freq: float | None = None,
    current_rf: float | None = None,
) -> tuple[float, float] | None:
    extracted = _extract_tune_bounds(
        tune_request,
        side=side,
        current_freq=current_freq,
        current_rf=current_rf,
    )
    return extracted[0] if extracted is not None else None


def _extract_tune_freq(
    tune_request,
    *,
    side: str,
    current_freq: float | None = None,
    current_rf: float | None = None,
) -> float | None:
    bounds = _extract_tune_freq_bounds(
        tune_request,
        side=side,
        current_freq=current_freq,
        current_rf=current_rf,
    )
    if bounds is None or bounds[0] != bounds[1]:
        return None
    return bounds[0]


_UNSET = object()

_AD936X_MBOARD_RE = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(?:b200|b205|b206|b210|e310|e312|e313|e31x|e320|(?:(?:ni[\s_-]*)?(?:usrp[\s_-]*)?)?290[01])"
    r"(?:[^0-9]|$)"
)
_N310_MBOARD_RE = re.compile(r"(?:^|[^a-z0-9])n3[01]0(?:[^0-9]|$)")
_INDEPENDENT_MBOARD_RE = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(?:x3[01]0|x4[14]0|n2[01]0|n32[01]|usrp1|usrp2|b100|e1[01]0|"
    r"(?:(?:ni[\s_-]*)?(?:usrp[\s_-]*)?)?(?:292[012]|293[02]|294[02345]|295[02345]|2974))"
    r"(?:[^0-9]|$)"
)


def _classify_mboard_coupling(name: str | None) -> tuple[str, bool]:
    """Return (lo_coupling, rate_shared) from a UHD motherboard name.

    lo_coupling is 'all' (AD9361/AD9364 or unknown/unreadable, fail closed),
    'pair' (N300/N310 AD9371: channels 0/1 and 2/3 share an LO), or
    'none' (X300/X310, N200/N210, and other independent daughterboard devices).
    """
    if not isinstance(name, str):
        return ("all", True)
    text = name.strip().lower()
    if not text:
        return ("all", True)
    if _AD936X_MBOARD_RE.search(text):
        return ("all", True)
    if _N310_MBOARD_RE.search(text):
        return ("pair", False)
    if _INDEPENDENT_MBOARD_RE.search(text):
        return ("none", False)
    return ("all", True)


def _extract_stream_channels(stream_args) -> list[int]:
    if isinstance(stream_args, Mapping):
        if stream_args.get("__remoterf_uhd_helper__") == "stream":
            return _extract_stream_channels(stream_args.get("stream_args"))
        raw = stream_args.get("channels")
    else:
        raw = getattr(stream_args, "channels", None)
    if raw is None:
        return [0]
    channels = [int(ch) for ch in raw]
    return channels or [0]


def _stream_cmd_mode(cmd) -> str:
    if isinstance(cmd, Mapping):
        raw = cmd.get("stream_mode", cmd.get("mode", ""))
    else:
        raw = getattr(cmd, "stream_mode", getattr(cmd, "mode", ""))
    return str("" if raw is None else raw).rsplit(".", 1)[-1].lower()


@idl_register("usrp")
class UsrpSchema(DeviceSchema):
    device_type = "usrp"
    client_class = "MultiUSRP"
    client_objects = _uhd_client_objects()
    driver_version = "0.0.9"
    session_scoped = True
    lockout_bypass: bool = False
    _freq_limits: FreqLimits | None = None
    _tx_in_flight: bool = False

    def __init__(self):
        super().__init__()
        self._inventory = {}
        self._legacy_device = False
        self.lockout_bypass = False
        self._freq_limits = None
        self._tx_in_flight = False

    @staticmethod
    def make_device(**kwargs):
        return _connect_usrp(**kwargs)

    def configure_lockouts(self, cfg: FreqLimits | None) -> None:
        self._freq_limits = cfg

    def bind_inventory(self, init_kwargs):
        """Bind stable inventory data without opening USB/UHD at server startup."""
        raw = dict(init_kwargs or {})
        limits = parse_freq_limits(raw)
        self._inventory = strip_freq_limit_keys(raw)
        if limits is not None:
            self._freq_limits = limits

    def open_native_device(self):
        return _connect_usrp(**self._inventory)

    def prepare_for_holder(self):
        """A reservation started: load a cold image now, not in the holder's open."""
        if uhd is not None:
            _preload_image(_usrp_args(**self._inventory))

    def create_session_schema(self):
        """Create an isolated, live adapter for one authenticated reservation."""
        clone = type(self)()
        clone.bind_inventory(self._inventory)
        clone.configure_lockouts(self._freq_limits)
        clone.lockout_bypass = bool(self.lockout_bypass)
        clone.bind(clone.open_native_device())
        return clone

    def _freq_refuse(self, message: str) -> None:
        if (
            self._freq_limits is not None
            and self._freq_limits.admin_group_override
            and self.lockout_bypass
        ):
            _log.warning("frequency limit bypassed on usrp: %s", message)
            return
        raise RemoteRFPolicyError(message)

    def _read_device_channel_value(self, method_name: str, chan: int):
        fn = getattr(self.device, method_name, None)
        if not callable(fn):
            return False, None
        try:
            value = fn(chan)
        except TypeError:
            try:
                value = fn()
            except Exception:
                return True, None
        except Exception:
            return True, None
        return True, _to_finite_float(value)

    def _read_tree_frontend_freq(self, side: str, chan: int) -> float | None:
        get_tree = getattr(self.device, "get_tree", None)
        get_subdev = getattr(self.device, f"get_{side}_subdev_spec", None)
        if not callable(get_tree) or not callable(get_subdev):
            return None
        try:
            tree = get_tree()
            if tree is None:
                return None
            get_mboards = getattr(self.device, "get_num_mboards", None)
            num_mboards = int(get_mboards()) if callable(get_mboards) else 1
            idx = 0
            for mb in range(num_mboards):
                spec_text = str(get_subdev(mb) or "").strip()
                for token in spec_text.split():
                    if ":" not in token:
                        continue
                    db_name, fe_name = token.split(":", 1)
                    if idx == chan:
                        path = f"/mboards/{mb}/dboards/{db_name}/{side}_frontends/{fe_name}/freq/value"
                        return _to_finite_float(tree.access_double(path).get())
                    idx += 1
        except Exception:
            return None
        return None

    def _read_device_lo_names(self, side: str, chan: int) -> list[str] | None:
        fn = getattr(self.device, f"get_{side}_lo_names", None)
        if not callable(fn):
            return None
        try:
            raw = fn(chan)
        except TypeError:
            try:
                raw = fn()
            except Exception:
                return None
        except Exception:
            return None
        if raw is None or isinstance(raw, (str, bytes)):
            return None
        try:
            names = list(raw)
        except TypeError:
            return None
        if not all(isinstance(item, str) for item in names):
            return None
        return names

    def _read_named_lo_freq(self, side: str, name: str, chan: int) -> float | None:
        fn = getattr(self.device, f"get_{side}_lo_freq", None)
        if not callable(fn):
            return None
        try:
            value = fn(name, chan)
        except TypeError:
            if name != "all":
                return None
            try:
                value = fn(chan)
            except Exception:
                return None
        except Exception:
            return None
        return _to_finite_float(value)

    def _read_device_lo_freq(self, side: str, chan: int) -> float | None:
        parsed = self._read_named_lo_freq(side, "all", chan)
        if parsed is not None:
            return parsed
        names = self._read_device_lo_names(side, chan)
        if names is not None:
            non_all = list(dict.fromkeys(item for item in names if item != "all"))
            if len(non_all) == 1:
                parsed = self._read_named_lo_freq(side, non_all[0], chan)
                if parsed is not None:
                    return parsed
        return self._read_tree_frontend_freq(side, chan)

    def _mboard_coupling(self) -> tuple[str, bool]:
        fn = getattr(self.device, "get_mboard_name", None)
        if not callable(fn):
            return ("all", True)
        get_num = getattr(self.device, "get_num_mboards", None)
        if callable(get_num):
            try:
                num_mboards = int(get_num())
            except Exception:
                return ("all", True)
            if num_mboards <= 0:
                return ("all", True)
        else:
            num_mboards = 1
        lo_mode = "none"
        rate_shared = False
        for mb in range(num_mboards):
            try:
                name = fn(mb)
            except TypeError:
                if mb != 0:
                    return ("all", True)
                try:
                    name = fn()
                except Exception:
                    return ("all", True)
            except Exception:
                return ("all", True)
            mb_lo, mb_rate = _classify_mboard_coupling(name)
            if mb_lo == "all":
                lo_mode = "all"
            elif mb_lo == "pair" and lo_mode == "none":
                lo_mode = "pair"
            if mb_rate:
                rate_shared = True
        return (lo_mode, rate_shared)

    def _lo_routing_shared(self, side: str) -> bool:
        """True when a live LO source or export setting (made before limits applied)
        couples channels. Unreadable settings count as default so independent-LO
        radios that lack these getters keep working."""
        count_fn = getattr(self.device, f"get_{side}_num_channels", None)
        names_fn = getattr(self.device, f"get_{side}_lo_names", None)
        if not (callable(count_fn) and callable(names_fn)):
            return False
        src_fn = getattr(self.device, f"get_{side}_lo_source", None)
        exp_fn = getattr(self.device, f"get_{side}_lo_export_enabled", None)
        try:
            count = int(count_fn())
        except Exception:
            return False
        for chan in range(max(count, 0)):
            try:
                names = list(names_fn(chan))
            except Exception:
                continue
            for name in names:
                try:
                    if callable(src_fn) and str(src_fn(name, chan)) not in ("internal", ""):
                        return True
                    if callable(exp_fn) and bool(exp_fn(name, chan)):
                        return True
                except Exception:
                    continue
        return False

    def _coupled_channels(self, side: str, ch: int) -> list[int] | None:
        lo_mode, _ = self._mboard_coupling()
        if lo_mode != "all" and self._lo_routing_shared(side):
            lo_mode = "all"
        if lo_mode == "none":
            return []
        if ch < 0:
            return None
        fn = getattr(self.device, f"get_{side}_num_channels", None)
        if callable(fn):
            try:
                count = int(fn())
            except Exception:
                return None
            if count < 0:
                return None
            channels = set(range(count))
        else:
            channels = set()
        prefix = f"{side}:"
        for handle in self._streamers():
            if handle.startswith(prefix):
                channels.update(
                    int(item) for item in self._streamer_channels().get(handle, ())
                )
        channels.discard(ch)
        if lo_mode == "pair":
            sibling = ch ^ 1
            return [sibling] if sibling in channels else []
        return sorted(channels)

    def _all_rx_channels(self) -> list[int]:
        fn = getattr(self.device, "get_rx_num_channels", None)
        if callable(fn):
            try:
                rx_count = int(fn())
            except Exception:
                return []
            return list(range(rx_count)) if rx_count > 0 else []
        channels = {
            int(item)
            for handle in self._streamers()
            if handle.startswith("rx:")
            for item in self._streamer_channels().get(handle, ())
        }
        return sorted(channels) if channels else [0]

    def _all_tx_channels(self) -> list[int]:
        fn = getattr(self.device, "get_tx_num_channels", None)
        if callable(fn):
            try:
                tx_count = int(fn())
            except Exception:
                return []
            return list(range(tx_count)) if tx_count > 0 else []
        channels = {
            int(item)
            for handle in self._streamers()
            if handle.startswith("tx:")
            for item in self._streamer_channels().get(handle, ())
        }
        return sorted(channels) if channels else [0]

    def _check_tx_band(
        self,
        *,
        chan: int = 0,
        freq=None,
        freq_bounds: tuple[float, float] | None = None,
        rate=None,
        require_positive_rate: bool = False,
    ) -> None:
        limits = self._freq_limits
        if limits is None or not limits.tx.is_active:
            return
        if freq_bounds is not None:
            lo_f = _to_finite_float(freq_bounds[0])
            hi_f = _to_finite_float(freq_bounds[1])
            if lo_f is None or hi_f is None or lo_f > hi_f:
                self._freq_refuse("TX refused: TX frequency must be a finite number.")
                return
        elif freq is None:
            _, lo_f = self._read_device_channel_value("get_tx_freq", chan)
            hi_f = lo_f
        else:
            lo_f = _to_finite_float(freq)
            hi_f = lo_f
        if lo_f is None or hi_f is None:
            self._freq_refuse("TX refused: TX frequency must be a finite number.")
            return
        if rate is None:
            has_rate, sr = self._read_device_channel_value("get_tx_rate", chan)
            if has_rate and sr is None:
                self._freq_refuse(
                    "TX refused: live TX sample rate must be a finite positive number."
                )
                return
            if not has_rate:
                if require_positive_rate:
                    self._freq_refuse(
                        "TX refused: live TX sample rate must be a finite positive number."
                    )
                    return
                sr = 0.0
        else:
            sr = _to_finite_float(rate)
            if sr is None:
                self._freq_refuse(
                    "TX refused: live TX sample rate must be a finite positive number."
                )
                return
        if sr < 0 or (require_positive_rate and sr <= 0):
            self._freq_refuse(
                "TX refused: live TX sample rate must be a finite positive number."
            )
            return
        low, high = lo_f - sr / 2.0, hi_f + sr / 2.0
        if not limits.tx.allows_band(low, high):
            self._freq_refuse(
                limits.tx.format_band_error("tx", low, high, detail="tx_freq ± tx_rate/2")
            )
        if freq_bounds is None and freq is None:
            rf_lo = self._read_device_lo_freq("tx", chan)
            if rf_lo is not None:
                self._check_lo_freq("tx", rf_lo)

    def _check_lo_freq(self, side: str, freq) -> None:
        limits = self._freq_limits
        if limits is None:
            return
        side_limits = limits.tx if side == "tx" else limits.rx
        if not side_limits.is_active:
            return
        value = _to_finite_float(freq)
        if value is None:
            tag = side.upper()
            self._freq_refuse(f"{tag} refused: {tag} LO frequency must be a finite number.")
            return
        if not side_limits.allows_freq(value):
            self._freq_refuse(side_limits.format_freq_error(side, value))

    def _check_rx_freq(self, freq) -> None:
        limits = self._freq_limits
        if limits is None or not limits.rx.is_active:
            return
        value = _to_finite_float(freq)
        if value is None:
            self._freq_refuse("RX refused: RX frequency must be a finite number.")
            return
        if not limits.rx.allows_freq(value):
            self._freq_refuse(
                limits.rx.format_freq_error("rx", value, label="RX center frequency")
            )

    def _check_rx_bounds(self, bounds: tuple[float, float]) -> None:
        limits = self._freq_limits
        if limits is None or not limits.rx.is_active:
            return
        if bounds[0] == bounds[1]:
            self._check_rx_freq(bounds[0])
            return
        if not limits.rx.allows_band(bounds[0], bounds[1]):
            self._freq_refuse(
                limits.rx.format_band_error(
                    "rx", bounds[0], bounds[1], detail="RF/DSP tune range"
                )
            )

    def _check_coupled_lo(
        self,
        side: str,
        ch: int,
        new_rf_bounds: tuple[float, float],
        *,
        rate=None,
        require_positive_rate: bool = False,
    ) -> None:
        limits = self._freq_limits
        if limits is None:
            return
        side_limits = limits.tx if side == "tx" else limits.rx
        if not side_limits.is_active:
            return
        tag = side.upper()
        other_channels = self._coupled_channels(side, ch)
        if other_channels is None:
            self._freq_refuse(
                f"{tag} refused: cannot determine {tag} channel count for shared-LO check "
                f"against {side_limits.describe_limits(side)}."
            )
            return
        for other_ch in other_channels:
            _, other_freq = self._read_device_channel_value(f"get_{side}_freq", other_ch)
            other_rf = self._read_device_lo_freq(side, other_ch)
            if other_freq is None or other_rf is None:
                self._freq_refuse(
                    f"{tag} refused: cannot determine live DSP offset on {tag} channel {other_ch} "
                    f"for shared-LO check against {side_limits.describe_limits(side)}."
                )
                return
            other_dsp = other_freq - other_rf
            other_bounds = _finite_bounds(
                new_rf_bounds[0] + other_dsp,
                new_rf_bounds[1] + other_dsp,
            )
            if other_bounds is None:
                self._freq_refuse(
                    f"{tag} refused: cannot determine resulting {tag} frequency on {tag} channel {other_ch} "
                    f"against {side_limits.describe_limits(side)}."
                )
                return
            if side == "tx":
                self._check_tx_band(
                    chan=other_ch,
                    freq_bounds=other_bounds,
                    rate=rate,
                    require_positive_rate=require_positive_rate,
                )
            else:
                self._check_rx_bounds(other_bounds)

    def _check_tx_tune(self, tune_request, chan=0, *, current_rf=_UNSET) -> None:
        limits = self._freq_limits
        if limits is None or not limits.tx.is_active:
            return
        ch = int(chan)
        _, current = self._read_device_channel_value("get_tx_freq", ch)
        rf = self._read_device_lo_freq("tx", ch) if current_rf is _UNSET else current_rf
        extracted = _extract_tune_bounds(
            tune_request,
            side="tx",
            current_freq=current,
            current_rf=rf,
        )
        if extracted is None:
            self._freq_refuse(
                "TX refused: cannot determine resulting TX frequency for tune request "
                f"against {limits.tx.describe_limits('tx')}."
            )
            return
        bounds, new_rf_bounds = extracted
        self._check_tx_band(chan=ch, freq_bounds=bounds, require_positive_rate=False)
        lo_bounds = (
            new_rf_bounds
            if new_rf_bounds is not None
            else (_finite_bounds(rf, rf) if rf is not None else None)
        )
        if lo_bounds is not None:
            self._check_lo_freq("tx", lo_bounds[0])
            if lo_bounds[1] != lo_bounds[0]:
                self._check_lo_freq("tx", lo_bounds[1])
        if new_rf_bounds is not None:
            self._check_coupled_lo("tx", ch, new_rf_bounds)

    def _check_rx_tune(self, tune_request, chan=0, *, current_rf=_UNSET) -> None:
        limits = self._freq_limits
        if limits is None or not limits.rx.is_active:
            return
        ch = int(chan)
        _, current = self._read_device_channel_value("get_rx_freq", ch)
        rf = self._read_device_lo_freq("rx", ch) if current_rf is _UNSET else current_rf
        extracted = _extract_tune_bounds(
            tune_request,
            side="rx",
            current_freq=current,
            current_rf=rf,
        )
        if extracted is None:
            self._freq_refuse(
                "RX refused: cannot determine resulting RX frequency for tune request "
                f"against {limits.rx.describe_limits('rx')}."
            )
            return
        bounds, new_rf_bounds = extracted
        self._check_rx_bounds(bounds)
        lo_bounds = (
            new_rf_bounds
            if new_rf_bounds is not None
            else (_finite_bounds(rf, rf) if rf is not None else None)
        )
        if lo_bounds is not None:
            self._check_lo_freq("rx", lo_bounds[0])
            if lo_bounds[1] != lo_bounds[0]:
                self._check_lo_freq("rx", lo_bounds[1])
        if new_rf_bounds is not None:
            self._check_coupled_lo("rx", ch, new_rf_bounds)

    def _check_lo_freq_call(self, side: str, freq, name, chan=0) -> None:
        limits = self._freq_limits
        if limits is None:
            return
        side_limits = limits.tx if side == "tx" else limits.rx
        if not side_limits.is_active:
            return
        tag = side.upper()
        ch = 0 if chan is None else int(chan)
        if name == "all":
            current_rf = self._read_device_lo_freq(side, ch)
        else:
            reported = self._read_device_lo_names(side, ch)
            if not isinstance(name, str) or reported is None or name not in reported:
                allowed = ["all", *(item for item in (reported or ()) if item != "all")]
                allowed_text = ", ".join(repr(item) for item in allowed)
                self._freq_refuse(
                    f"{tag} refused: set_{side}_lo_freq LO name {name!r} is not reported "
                    f"by this device (allowed: {allowed_text}) against "
                    f"{side_limits.describe_limits(side)}."
                )
                return
            stage_freq = self._read_named_lo_freq(side, name, ch)
            if stage_freq is None:
                self._freq_refuse(
                    f"{tag} refused: cannot read current frequency for {tag} LO stage {name!r} "
                    f"against {side_limits.describe_limits(side)}."
                )
                return
            overall_rf = self._read_device_lo_freq(side, ch)
            non_all = list(dict.fromkeys(item for item in reported if item != "all"))
            if overall_rf is not None:
                if not math.isclose(stage_freq, overall_rf, rel_tol=1e-9, abs_tol=1.0):
                    self._freq_refuse(
                        f"{tag} refused: cannot check resulting {tag} frequency for "
                        f"intermediate LO stage {name!r} against "
                        f"{side_limits.describe_limits(side)}."
                    )
                    return
                current_rf = stage_freq
            elif non_all == [name]:
                current_rf = stage_freq
            else:
                self._freq_refuse(
                    f"{tag} refused: cannot determine RF LO frequency for {tag} LO stage {name!r} "
                    f"against {side_limits.describe_limits(side)}."
                )
                return
        tune_request = {
            "__uhd_type__": "TuneRequest",
            "rf_freq_policy": "MANUAL",
            "dsp_freq_policy": "NONE",
            "rf_freq": freq,
        }
        if side == "tx":
            self._check_tx_tune(tune_request, ch, current_rf=current_rf)
        else:
            self._check_rx_tune(tune_request, ch, current_rf=current_rf)

    def check_tx_metadata(self, metadata) -> None:
        limits = self._freq_limits
        if limits is None or not limits.tx.is_active:
            return
        if metadata is None:
            return
        if isinstance(metadata, Mapping):
            has_time_spec = bool(metadata.get("has_time_spec", False))
            time_spec = metadata.get("time_spec")
        else:
            has_time_spec = bool(getattr(metadata, "has_time_spec", False))
            time_spec = getattr(metadata, "time_spec", None)
        if has_time_spec:
            self._freq_refuse(
                "TX refused: timed TX metadata (time_spec) is not allowed while TX frequency limits are active."
            )
            return
        if time_spec is not None:
            secs = _time_spec_secs(time_spec)
            if secs is None or secs != 0.0:
                self._freq_refuse(
                    "TX refused: timed TX metadata (time_spec) is not allowed while TX frequency limits are active."
                )

    def check_tx_channels(self, channels) -> None:
        limits = self._freq_limits
        if limits is None or not limits.tx.is_active:
            return
        if not channels:
            self._freq_refuse("TX refused: stream channels are unknown.")
            return
        for chan in channels:
            self._check_tx_band(
                chan=int(chan),
                require_positive_rate=True,
            )

    def check_rx_channels(self, channels) -> None:
        limits = self._freq_limits
        if limits is None or not limits.rx.is_active:
            return
        if not channels:
            self._freq_refuse("RX refused: stream channels are unknown.")
            return
        for chan in channels:
            ch = int(chan)
            _, freq = self._read_device_channel_value("get_rx_freq", ch)
            self._check_rx_freq(freq)
            rf_lo = self._read_device_lo_freq("rx", ch)
            if rf_lo is not None:
                self._check_lo_freq("rx", rf_lo)

    def _has_open_v1_tx_streamer(self) -> bool:
        streamers = getattr(self, "_usrp_streamers", None)
        return bool(streamers and any(handle.startswith("tx:") for handle in streamers))

    def mark_tx_in_flight(self) -> None:
        self._tx_in_flight = True

    def check_master_clock_rate_change(self) -> None:
        limits = self._freq_limits
        if limits is None or not limits.tx.is_active:
            return
        if not (self._tx_in_flight or self._has_open_v1_tx_streamer()):
            return
        self._freq_refuse(
            "Master clock rate changes are refused after TX has started while TX frequency limits are active."
        )

    def check_tx_rate_change(self, rate, chan, active_channels=None) -> None:
        limits = self._freq_limits
        if limits is None or not limits.tx.is_active:
            return
        if active_channels is None and not (
            self._tx_in_flight or self._has_open_v1_tx_streamer()
        ):
            return
        if active_channels is not None and not active_channels:
            self._freq_refuse("TX refused: active TX stream channels are unknown.")
            return
        if isinstance(chan, bool):
            self._freq_refuse("TX refused: TX channel must be an integer.")
            return
        try:
            target_ch = None if chan is None else int(chan)
        except (TypeError, ValueError):
            self._freq_refuse("TX refused: TX channel must be an integer.")
            return
        all_tx = self._all_tx_channels()
        if active_channels is not None:
            known_channels = sorted(
                {int(ch) for ch in all_tx} | {int(ch) for ch in active_channels}
            )
        else:
            known_channels = all_tx
        _, rate_shared = self._mboard_coupling()
        all_chans = target_ch is None or target_ch < 0 or target_ch >= (1 << 32)
        if rate_shared or all_chans:
            if not known_channels:
                self._freq_refuse("TX refused: TX channels are unknown.")
                return
            channels_to_check = list(known_channels)
            if not all_chans and target_ch not in channels_to_check:
                channels_to_check.append(target_ch)
        else:
            channels_to_check = [target_ch]
        for ch in channels_to_check:
            self._check_tx_band(chan=ch, rate=rate, require_positive_rate=True)

    def _check_native_call(self, method_name, args, positional_args) -> None:
        limits = self._freq_limits
        if limits is None or not limits.is_active:
            return
        kw = dict(args or {})
        pos = list(positional_args or ())

        def _arg(name, index, default=None):
            if name in kw:
                return kw[name]
            if index < len(pos):
                return pos[index]
            return default

        if method_name == "set_tx_freq":
            self._check_tx_tune(_arg("tune_request", 0), _arg("chan", 1, 0))
        elif method_name == "set_rx_freq":
            self._check_rx_tune(_arg("tune_request", 0), _arg("chan", 1, 0))
        elif method_name == "set_tx_rate":
            self.check_tx_rate_change(_arg("rate", 0), _arg("chan", 1, None))
        elif method_name == "set_master_clock_rate":
            self.check_master_clock_rate_change()
        elif method_name == "set_tx_lo_freq":
            self._check_lo_freq_call("tx", _arg("freq", 0), _arg("name", 1), _arg("chan", 2, 0))
        elif method_name == "set_rx_lo_freq":
            self._check_lo_freq_call("rx", _arg("freq", 0), _arg("name", 1), _arg("chan", 2, 0))
        elif method_name in ("set_command_time", "clear_command_time"):
            self._freq_refuse(
                "Timed commands are refused while frequency limits are active."
            )
        elif method_name in (
            "set_tx_lo_source",
            "set_rx_lo_source",
            "set_tx_lo_export_enabled",
            "set_rx_lo_export_enabled",
        ):
            # Re-sourcing or exporting an LO couples channels the checks treat as independent.
            self._freq_refuse(
                "LO source and export changes are refused while frequency limits are active."
            )
        elif method_name == "set_tx_subdev_spec":
            if limits.tx.is_active:
                self._freq_refuse(
                    "TX subdev spec changes are refused while TX frequency limits are active."
                )
        elif method_name == "set_rx_subdev_spec":
            if limits.rx.is_active:
                self._freq_refuse(
                    "RX subdev spec changes are refused while RX frequency limits are active."
                )
        elif method_name == "set_tx_iq_balance":
            if limits.tx.is_active:
                self._freq_refuse(
                    "TX IQ balance changes are refused while TX frequency limits are active."
                )
        elif method_name == "set_tx_filter":
            if limits.tx.is_active:
                self._freq_refuse(
                    "TX filter changes are refused while TX frequency limits are active."
                )
        elif method_name == "issue_stream_cmd":
            if limits.rx.is_active:
                cmd = _arg("stream_cmd", 0)
                if cmd is None:
                    cmd = _arg("rate", 0)
                stream_mode = _stream_cmd_mode(cmd)
                if stream_mode not in {"stop_cont", "stop_continuous"}:
                    chan = _arg("chan", 1, None)
                    if chan is None or int(chan) >= (1 << 32):
                        channels = self._all_rx_channels()
                    else:
                        channels = [int(chan)]
                    self.check_rx_channels(channels)
        elif method_name == "get_tx_stream":
            self.check_tx_channels(_extract_stream_channels(_arg("stream_args", 0)))
        elif method_name == "send_waveform":
            if limits.tx.is_active:
                self._freq_refuse(
                    "TX refused: send_waveform is not allowed while TX frequency limits "
                    f"are active ({limits.tx.describe_limits('tx')}) because UHD coerces "
                    "TX rate and LO inside the call without readback; use set_tx_rate, "
                    "set_tx_freq, get_tx_stream, and send instead."
                )
        elif method_name == "recv_num_samps":
            if limits.rx.is_active:
                if _arg("streamer", 6, None) is not None:
                    self._freq_refuse(
                        "RX refused: caller-supplied streamer is not allowed on "
                        "recv_num_samps while RX frequency limits are active."
                    )
                    return
                raw_channels = _arg("channels", 3, _UNSET)
                if raw_channels is _UNSET:
                    channels = [0]
                else:
                    channels = _extract_explicit_channels(raw_channels)
                    if not channels:
                        self._freq_refuse(
                            "RX refused: recv_num_samps channels must be a non-empty list "
                            "while RX frequency limits are active."
                        )
                        return
                freq = _arg("freq", 1)
                self._check_rx_freq(freq)
                freq_val = _to_finite_float(freq)
                if freq_val is not None:
                    for ch_int in channels:
                        self._check_coupled_lo("rx", ch_int, (freq_val, freq_val))

    def close_native_device(self):
        self._legacy_device = False
        self._tx_in_flight = False
        self.call_close_all_streams()
        device = self.device
        self.device = None
        close = getattr(device, "close", None)
        if callable(close):
            close()

    def dispatch(self, method_name, args):
        # V1 compatibility: old clients still work, but share one lazily-opened
        # adapter exactly as they did before session-scoped inventory support.
        if self.device is None:
            self.bind(self.open_native_device())
            self._legacy_device = True
        return super().dispatch(method_name, args)

    def get_idl_v2(self):
        return build_usrp_schema_v2(self.driver_version)

    def _probe_rx_stream_formats(self, rx_count):
        """Ask native UHD which RX OTW formats this live device accepts.

        Streamers are constructed and immediately released without issuing a
        stream command or receiving samples.  The result is cached because
        streamer construction configures native transport/DSP resources and
        should happen only while the newly opened session is otherwise idle.
        """
        cached = getattr(self, "_rx_stream_format_probe_cache", None)
        if cached is not None:
            return cached

        channels = [0] if rx_count else []
        result = {
            "status": "runtime_probed" if channels else "unavailable",
            "method": "native_rx_streamer_creation",
            "cpu_format": _OTW_PROBE_CPU_FORMAT,
            "channels": channels,
            "default": None,
            "formats": [],
        }
        if not channels:
            result["reason"] = "device_has_no_rx_channels"
            self._rx_stream_format_probe_cache = result
            return result

        probe_enabled = str(
            os.getenv("REMOTERF_USRP_PROBE_RX_OTW_FORMATS", "1")
        ).strip().lower()
        if probe_enabled in {
            "0",
            "false",
            "no",
            "off",
        }:
            result["status"] = "disabled"
            result["reason"] = "REMOTERF_USRP_PROBE_RX_OTW_FORMATS"
            self._rx_stream_format_probe_cache = result
            return result

        if getattr(self, "_usrp_streamers", None):
            result["status"] = "skipped"
            result["reason"] = "active_remote_streamers"
            self._rx_stream_format_probe_cache = result
            return result

        stream_args_type = getattr(
            getattr(_require_uhd(), "usrp", None),
            "StreamArgs",
            None,
        )
        native_get_rx_stream = getattr(self.device, "get_rx_stream", None)
        if stream_args_type is None or not callable(native_get_rx_stream):
            result["status"] = "unavailable"
            result["reason"] = "native_rx_streamer_api_missing"
            self._rx_stream_format_probe_cache = result
            return result

        def attempt(otw_format):
            entry = {
                "otw_format": otw_format,
                "supported": False,
                "max_num_samps": None,
            }
            streamer = None
            try:
                stream_args = stream_args_type(
                    _OTW_PROBE_CPU_FORMAT,
                    otw_format,
                )
                stream_args.channels = list(channels)
                streamer = native_get_rx_stream(stream_args)
                entry["supported"] = True
                get_max_num_samps = getattr(streamer, "get_max_num_samps", None)
                if callable(get_max_num_samps):
                    try:
                        entry["max_num_samps"] = int(get_max_num_samps())
                    except Exception:
                        pass
            except Exception as exc:
                entry["native_error_type"] = type(exc).__name__
                entry["native_error"] = str(exc)[:1024]
            finally:
                # Native UHD streamers have no close() method. Dropping the
                # final Python reference releases their transport resources.
                streamer = None
            return entry

        result["default"] = attempt("")
        result["formats"] = [
            attempt(otw_format)
            for otw_format in _UHD_OTW_FORMAT_CANDIDATES
        ]
        self._rx_stream_format_probe_cache = result
        return result

    def invoke_native(self, method_name, args, positional_args=None):
        """Invoke a pinned UHD member while reconstructing tagged value inputs."""
        if self.device is None:
            raise RuntimeError("USRP session is not open")
        target = getattr(self.device, method_name)
        if method_name in (
            "set_command_time",
            "clear_command_time",
            "set_master_clock_rate",
            "set_tx_subdev_spec",
            "set_rx_subdev_spec",
            "set_tx_iq_balance",
            "set_tx_filter",
            "send_waveform",
        ):
            self._check_native_call(method_name, args, positional_args)
            positional = [_construct(item) for item in (positional_args or ())]
            keywords = {str(key): _construct(item) for key, item in dict(args or {}).items()}
            if method_name == "send_waveform":
                self.mark_tx_in_flight()
            return target(*positional, **keywords)
        positional = [_construct(item) for item in (positional_args or ())]
        keywords = {str(key): _construct(item) for key, item in dict(args or {}).items()}
        self._check_native_call(method_name, keywords, positional)
        result = target(*positional, **keywords)
        if method_name == "get_tx_stream" and result is not None:
            self.mark_tx_in_flight()
        return result

    @staticmethod
    def _version_value(*names):
        uhd_module = _require_uhd()
        for name in names:
            value = getattr(uhd_module, name, None)
            if callable(value):
                try:
                    return str(value())
                except Exception:
                    continue
            if value:
                return str(value)
        return ""

    def build_session_metadata_v2(self):
        device = self.device
        if device is None:
            raise RuntimeError("USRP session is not open")

        def call(name, *args, default=None):
            fn = getattr(device, name, None)
            if not callable(fn):
                return default
            try:
                return fn(*args)
            except Exception:
                return default

        rx_count = int(call("get_rx_num_channels", default=0) or 0)
        tx_count = int(call("get_tx_num_channels", default=0) or 0)
        rx_info = call("get_usrp_rx_info", 0, default={}) if rx_count else {}
        tx_info = call("get_usrp_tx_info", 0, default={}) if tx_count else {}
        info = dict(rx_info or {})
        info.update(dict(tx_info or {}))
        native_name = str(info.get("mboard_name") or "")
        product = (
            info.get("mboard_id")
            or call("get_mboard_name", 0, default="")
        )
        product_text = str(product or "")
        profile, profile_resolution = resolve_live_profile(
            self._inventory,
            native_name,
            product_text,
            info.get("rx_subdev_name"),
            info.get("tx_subdev_name"),
        )
        hardware_profile = profile.profile_id
        serial = (
            self._inventory.get("serial")
            or info.get("mboard_serial")
            or info.get("serial")
            or ""
        )
        tree = call("get_tree", default=None)

        def tree_scalar(path):
            if tree is None:
                return ""
            for accessor in ("access_str", "access_int", "access_double"):
                try:
                    return getattr(tree, accessor)(path).get()
                except Exception:
                    continue
            return ""

        firmware_version = str(tree_scalar("/mboards/0/fw_version"))
        fpga_version = str(tree_scalar("/mboards/0/fpga_version"))
        usb_version = str(tree_scalar("/mboards/0/usb_version"))
        link_rate = tree_scalar("/mboards/0/link_max_rate")
        transport_is_network = (
            profile.transport == "ethernet"
            or bool(self._inventory.get("addr"))
        )
        transport_address = str(self._inventory.get("addr") or "")
        if transport_is_network and not link_rate and profile.default_link_rate_bps:
            link_rate = profile.default_link_rate_bps
        if transport_is_network:
            transport_type = "ethernet"
        elif profile.transport != "runtime_detected":
            transport_type = profile.transport
        else:
            transport_type = "usb" if usb_version else "unknown"

        def ranges(method_name, count):
            return [call(method_name, chan, default=None) for chan in range(count)]

        rx_format_probe = self._probe_rx_stream_formats(rx_count)
        supported_otw_formats = [
            str(entry["otw_format"])
            for entry in rx_format_probe.get("formats", [])
            if entry.get("supported") and entry.get("otw_format")
        ]
        stream_format_capabilities = {
            "rx": rx_format_probe,
            "tx": {
                "status": "not_probed",
                "reason": "tx_capability_probe_requires_explicit_admin_qualification",
            },
        }

        return {
            "remote_device_type": "usrp",
            "hardware_profile": hardware_profile,
            "device_profile": profile.public_definition(),
            "profile_resolution": profile_resolution,
            "native_product": product_text,
            "native_name": native_name,
            "native_serial": str(serial),
            "uhd_version": self._version_value("get_version_string", "get_version"),
            "uhd_abi": self._version_value("get_abi_string", "get_abi"),
            "firmware_version": firmware_version,
            "fpga_version": fpga_version,
            "fpga_image": fpga_version,
            "transport_type": transport_type,
            "transport_address": transport_address,
            "usb_speed": "" if transport_is_network else usb_version,
            "transport_link_rate_bps": link_rate,
            "rx_channel_count": rx_count,
            "tx_channel_count": tx_count,
            "rx_frequency_ranges_per_channel": ranges("get_rx_freq_range", rx_count),
            "tx_frequency_ranges_per_channel": ranges("get_tx_freq_range", tx_count),
            "rx_gain_ranges_per_channel": ranges("get_rx_gain_range", rx_count),
            "tx_gain_ranges_per_channel": ranges("get_tx_gain_range", tx_count),
            "rx_bandwidth_ranges_per_channel": ranges("get_rx_bandwidth_range", rx_count),
            "tx_bandwidth_ranges_per_channel": ranges("get_tx_bandwidth_range", tx_count),
            "master_clock_rate_range": call("get_master_clock_rate_range", default=None),
            "time_sources": call("get_time_sources", 0, default=[]),
            "clock_sources": call("get_clock_sources", 0, default=[]),
            "gpio_banks": call("get_gpio_banks", 0, default=[]),
            "sensors": call("get_mboard_sensor_names", 0, default=[]),
            "supported_cpu_formats": ["fc32", "fc64", "sc16"],
            "supported_otw_formats": supported_otw_formats,
            "stream_format_capabilities": stream_format_capabilities,
            "advanced_interfaces": {
                "property_tree": tree is not None,
                "filters": True,
                "radio_control": False,
                "mboard_controller": False,
                "dboard_iface": False,
                "user_settings_iface": False,
            },
            "maximum_remote_payload_bytes": 4 * 1024 * 1024,
            "supported_streaming_protocol_versions": ["1.0"],
        }

    def _streamers(self):
        if not hasattr(self, "_usrp_streamers"):
            self._usrp_streamers = {}
            self._usrp_stream_ids = itertools.count(1)
        return self._usrp_streamers

    def _streamer_channels(self):
        if not hasattr(self, "_usrp_streamer_channels"):
            self._usrp_streamer_channels = {}
        return self._usrp_streamer_channels

    def _put_streamer(self, direction, streamer, channels=None):
        streamers = self._streamers()
        handle = f"{direction}:{next(self._usrp_stream_ids)}"
        streamers[handle] = streamer
        self._streamer_channels()[handle] = list(channels) if channels else [0]
        return {"__uhd_type__": f"{direction.upper()}Streamer", "handle": handle}

    def _get_streamer(self, handle):
        handle = _handle(handle, "streamer lookup")
        streamers = self._streamers()
        if handle not in streamers:
            existing = ", ".join(streamers.keys()) or "none"
            raise KeyError(
                f"Unknown USRP streamer handle {handle!r}. "
                "Create a streamer first with get_rx_stream/get_tx_stream. "
                f"Existing handles: {existing}. {_STREAM_USAGE}"
            )
        return streamers[handle]

    # region init / repr

    @idl_expose(kind="get")
    def get_repr(self):
        return repr(self.device)

    # endregion

    # region explicit MultiUSRP calls used by simple_send.py

    @idl_expose(kind="call", tag="TX")
    def call_set_tx_subdev_spec(self, spec, mboard=0):
        if self._freq_limits is not None and self._freq_limits.tx.is_active:
            self._freq_refuse(
                "TX subdev spec changes are refused while TX frequency limits are active."
            )
        self.device.set_tx_subdev_spec(_construct(spec), mboard)

    @idl_expose(kind="call", tag="TX")
    def call_set_tx_antenna(self, antenna, chan=0):
        self.device.set_tx_antenna(antenna, chan)

    @idl_expose(kind="call")
    def call_set_rx_subdev_spec(self, spec, mboard=0):
        if self._freq_limits is not None and self._freq_limits.rx.is_active:
            self._freq_refuse(
                "RX subdev spec changes are refused while RX frequency limits are active."
            )
        self.device.set_rx_subdev_spec(_construct(spec), mboard)

    @idl_expose(kind="call")
    def call_set_rx_antenna(self, antenna, chan=0):
        self.device.set_rx_antenna(antenna, chan)

    # endregion

    # region explicit MultiUSRP calls used by usrp_utils.py

    @idl_expose(
        kind="call",
        client_return=c.ctor("uhd.types.TimeSpec", "$result"),
    )
    def call_get_time_now(self):
        return _time_spec_payload(self.device.get_time_now())

    @idl_expose(kind="call")
    def call_get_rx_num_channels(self):
        return self.device.get_rx_num_channels()

    @idl_expose(kind="call")
    def call_get_tx_num_channels(self):
        return self.device.get_tx_num_channels()

    @idl_expose(kind="call")
    def call_set_clock_source(self, source, mboard=0):
        self.device.set_clock_source(source, mboard)

    @idl_expose(kind="call")
    def call_set_time_source(self, source, mboard=0):
        self.device.set_time_source(source, mboard)

    @idl_expose(kind="call")
    def call_set_time_unknown_pps(self, value):
        self.device.set_time_unknown_pps(_construct(value))

    @idl_expose(kind="call")
    def call_set_time_now(self, value):
        self.device.set_time_now(_construct(value))

    @idl_expose(kind="call")
    def call_set_time_next_pps(self, value):
        self.device.set_time_next_pps(_construct(value))

    @idl_expose(
        kind="call",
        client_return=c.ctor("uhd.types.TimeSpec", "$result"),
    )
    def call_get_time_last_pps(self):
        return _time_spec_payload(self.device.get_time_last_pps())

    @idl_expose(kind="call")
    def call_get_tx_subdev_spec(self, mboard=0):
        return str(self.device.get_tx_subdev_spec(mboard))

    @idl_expose(kind="call")
    def call_get_rx_subdev_spec(self, mboard=0):
        return str(self.device.get_rx_subdev_spec(mboard))

    @idl_expose(kind="call")
    def call_get_rx_rate(self, chan=0):
        return self.device.get_rx_rate(chan)

    @idl_expose(kind="call")
    def call_get_tx_rate(self, chan=0):
        return self.device.get_tx_rate(chan)

    @idl_expose(kind="call")
    def call_set_master_clock_rate(self, rate, mboard=None):
        self.check_master_clock_rate_change()
        if mboard is not None:
            self.device.set_master_clock_rate(rate, mboard)
        else:
            self.device.set_master_clock_rate(rate)

    @idl_expose(kind="call")
    def call_get_mboard_sensor(self, name, mboard=0):
        return str(self.device.get_mboard_sensor(name, mboard))

    @idl_expose(kind="call")
    def call_set_rx_rate(self, rate, chan=0):
        self.device.set_rx_rate(rate, chan)

    @idl_expose(kind="call")
    def call_set_rx_freq(self, tune_request, chan=0):
        native_request = _construct(tune_request)
        self._check_rx_tune(native_request, chan)
        self.device.set_rx_freq(native_request, chan)

    @idl_expose(kind="call")
    def call_set_rx_gain(self, gain, chan=0):
        self.device.set_rx_gain(gain, chan)

    @idl_expose(kind="call")
    def call_set_rx_dc_offset(self, enable, chan=0):
        self.device.set_rx_dc_offset(enable, chan)

    @idl_expose(kind="call", tag="TX")
    def call_set_tx_rate(self, rate, chan=0):
        active_tx = sorted(
            {
                ch
                for handle in self._streamers()
                if handle.startswith("tx:")
                for ch in self._streamer_channels().get(handle, [0])
            }
        )
        self.check_tx_rate_change(rate, chan, active_tx or None)
        self.device.set_tx_rate(rate, chan)

    @idl_expose(kind="call", tag="TX")
    def call_set_tx_freq(self, tune_request, chan=0):
        native_request = _construct(tune_request)
        self._check_tx_tune(native_request, chan)
        self.device.set_tx_freq(native_request, chan)

    @idl_expose(kind="call", tag="TX")
    def call_set_tx_gain(self, gain, chan=0):
        self.device.set_tx_gain(gain, chan)

    @idl_expose(
        kind="call",
        client_return=c.ctor("uhd.RXStreamer", "$self", "$result"),
    )
    def call_get_rx_stream(self, stream_args):
        native_args = _construct(stream_args)
        channels = _extract_stream_channels(native_args)
        return self._put_streamer(
            "rx",
            self.device.get_rx_stream(native_args),
            channels=channels,
        )

    @idl_expose(
        kind="call",
        client_return=c.ctor("uhd.TXStreamer", "$self", "$result"),
        tag="TX",
    )
    def call_get_tx_stream(self, stream_args):
        native_args = _construct(stream_args)
        channels = _extract_stream_channels(native_args)
        self.check_tx_channels(channels)
        streamer = self.device.get_tx_stream(native_args)
        self.mark_tx_in_flight()
        return self._put_streamer(
            "tx",
            streamer,
            channels=channels,
        )

    # endregion

    # region stream handle calls used by remoterf.uhd helpers

    @idl_expose(kind="call")
    def call_stream_get_max_num_samps(self, handle):
        return self._get_streamer(_handle(handle, "stream_get_max_num_samps")).get_max_num_samps()

    @idl_expose(kind="call")
    def call_stream_issue_stream_cmd(self, value):
        _require_dict(value, "stream_issue_stream_cmd")
        _require_key(value, "handle", "stream_issue_stream_cmd")
        _require_key(value, "stream_cmd", "stream_issue_stream_cmd")
        handle = _handle(value["handle"], "stream_issue_stream_cmd")
        native_cmd = _construct(value["stream_cmd"])
        if handle.startswith("rx:"):
            stream_mode = _stream_cmd_mode(native_cmd)
            if stream_mode not in {"stop_cont", "stop_continuous"}:
                self.check_rx_channels(self._streamer_channels().get(handle, [0]))
        self._get_streamer(handle).issue_stream_cmd(native_cmd)

    @idl_expose(kind="call")
    def call_stream_recv(self, value):
        _require_dict(value, "stream_recv")
        _require_key(value, "handle", "stream_recv")
        _require_key(value, "recv_buffer", "stream_recv")
        handle = _handle(value["handle"], "stream_recv")
        self.check_rx_channels(self._streamer_channels().get(handle, [0]))
        recv_buffer = value["recv_buffer"]
        timeout = value.get("timeout", 0.1)
        _require_uhd()
        metadata = uhd.types.RXMetadata()
        count = self._get_streamer(handle).recv(
            recv_buffer,
            metadata,
            timeout,
        )
        return count, recv_buffer, _rx_metadata_payload(metadata)

    @idl_expose(kind="call", tag="TX")
    def call_stream_send(self, value):
        _require_dict(value, "stream_send")
        _require_key(value, "handle", "stream_send")
        _require_key(value, "samples", "stream_send")
        handle = _handle(value["handle"], "stream_send")
        self.check_tx_channels(self._streamer_channels().get(handle, [0]))
        samples = value["samples"]
        metadata = _tx_metadata(value.get("metadata"))
        self.check_tx_metadata(metadata)
        timeout = value.get("timeout", 0.1)
        streamer = self._get_streamer(handle)
        self.mark_tx_in_flight()
        return streamer.send(samples, metadata, timeout)

    @idl_expose(kind="call")
    def call_stream_close(self, handle):
        key = _handle(handle, "stream_close")
        self._streamers().pop(key, None)
        self._streamer_channels().pop(key, None)

    @idl_expose(kind="call")
    def call_close_all_streams(self):
        count = len(self._streamers())
        self._streamers().clear()
        self._streamer_channels().clear()
        return count

    def reset_for_next_holder(self):
        """A reservation ended: drop v1 streamers and close any lazy v1 device."""
        self._tx_in_flight = False
        if self._legacy_device:
            self.close_native_device()
            return
        self._streamers().clear()
        self._streamer_channels().clear()

    # endregion
