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

from __future__ import annotations

import json

import numpy as np

from remoterf_device_core.idl.tags import (
    blocked_tag_message,
    find_blocked_tag,
    v1_function_label,
    v1_method_name,
)

from ..common.utils import map_arg, unmap_arg
from .device_manager import find_device_id_by_name, get_device, get_device_schema


def _normalize_device_rpc_type(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _wire(result, args: dict):
    """map_arg, in the sample encoding the caller asked for (Gate I slice 3).

    ``wire`` is opt-in per call: ``c64`` halves complex128 samples, ``i16``
    quarters them when they are integer-valued IQ (a 12-bit ADC's are) and
    otherwise sends them unchanged (lossless or nothing). A multi-channel
    result (list of per-channel arrays) is stacked first, as map_arg would.
    Anything else, or no flag, is as before.
    """
    from ..common.utils.process_arg import fits_iq16, map_iq16

    wire = unmap_arg(args["wire"]) if "wire" in args else None
    if wire not in ("c64", "i16"):
        return map_arg(result)
    if isinstance(result, (list, tuple)):
        try:
            result = np.asarray(result)
        except Exception:
            pass
    if isinstance(result, np.ndarray) and np.iscomplexobj(result):
        if wire == "c64":
            return map_arg(result.astype(np.complex64))
        if fits_iq16(result):
            return map_iq16(result)
    return map_arg(result)


class RpcManager:
    def __init__(self):
        self.debug = False

    @staticmethod
    def _resolve_gid(args):
        for key in ("g", "gid"):
            if key in args:
                try:
                    return int(unmap_arg(args[key]))
                except Exception:
                    return None
        return None

    @staticmethod
    def _safe_map(x):
        a = map_arg(x if x is not None else "None")
        try:
            if not a.ListFields():
                a = map_arg(str(x))
        except Exception:
            pass
        return a

    def run_rpc(self, *, function_name, args, expected_epoch=None) -> dict:
        from contextlib import nullcontext
        from .device_manager import hardware_access, get_local_device_context, reset_before_rpc
        args = dict(args)
        tag_arg = args.pop("__rrf_reservation_tag", None)
        blocked_arg = args.pop("__rrf_blocked_tags", None)
        master_arg = args.pop("__rrf_master_bypass", None)
        try:
            tag = None if tag_arg is None else unmap_arg(tag_arg)
        except Exception:
            tag = None
        if tag_arg is not None and not isinstance(tag, str):
            return {"Error": map_arg("The reservation on this device changed; call again.")}
        try:
            master_bypass = (master_arg is not None) and (unmap_arg(master_arg) is True)
        except Exception:
            master_bypass = False
        grant_policy_tags: tuple[str, ...] = ()
        if blocked_arg is not None:
            try:
                raw_blocked = unmap_arg(blocked_arg)
                parsed_blocked = json.loads(raw_blocked) if isinstance(raw_blocked, str) else raw_blocked
                if not isinstance(parsed_blocked, list) or any(
                    not isinstance(item, str) or not item.strip() for item in parsed_blocked
                ):
                    raise ValueError("invalid blocked_tags")
                grant_policy_tags = tuple(item.strip() for item in parsed_blocked)
            except Exception:
                return {"Error": map_arg("Invalid blocked tags metadata.")}

        def _call_admitted() -> dict:
            if grant_policy_tags or master_bypass:
                return self._run_admitted(
                    function_name=function_name,
                    args=args,
                    policy_tags=grant_policy_tags,
                    master_bypass=master_bypass,
                )
            return self._run_admitted(function_name=function_name, args=args)

        gid = self._resolve_gid(args)
        if (gid is None or function_name.startswith("IDL:") or function_name == "echo"
                or (function_name.startswith("DynamicV2:") and tag is None)):
            return _call_admitted()
        try:
            context = get_local_device_context(gid) or {}
            dynamic_v2 = function_name.startswith("DynamicV2:")
            # v2 ops run after admission, as at HEAD; OpenSession stays in one hold so a stale open can't land after the next holder's.
            run_after = dynamic_v2 and function_name != "DynamicV2:OpenSession"
            access = nullcontext() if dynamic_v2 else hardware_access.native(gid)
            with access, context.get("io_lock", nullcontext()):
                if "epoch" in context:
                    reset_before_rpc(gid, context["epoch"] if expected_epoch is None else expected_epoch, context["schema"], tag, reset=not dynamic_v2)
                if not run_after:
                    return _call_admitted()
            return _call_admitted()
        except Exception as exc:
            return {"Error": map_arg(str(exc))}

    def _run_admitted(
        self,
        *,
        function_name,
        args,
        policy_tags: tuple[str, ...] = (),
        master_bypass: bool = False,
    ) -> dict:
        if function_name == "echo":
            return args

        if function_name.startswith("DynamicV2:"):
            gid = self._resolve_gid(args)
            if gid is None:
                return {"Error": map_arg("Missing device id for tunneled Dynamic v2 request.")}
            try:
                from .dynamic_v2_bridge import run_bridge

                if policy_tags or master_bypass:
                    return run_bridge(
                        operation=function_name.split(":", 1)[1],
                        args=args,
                        gid=gid,
                        blocked_tags=policy_tags,
                        master_bypass=master_bypass,
                    )
                return run_bridge(
                    operation=function_name.split(":", 1)[1],
                    args=args,
                    gid=gid,
                )
            except Exception as exc:
                return {"Error": map_arg(str(exc))}

        if function_name.startswith("IDL:"):
            return self._handle_idl(function_name.split(":", 1)[1], args)

        parts = (function_name or "").split(":", 2)
        if len(parts) != 3 or not all(parts):
            return {"Error": map_arg(f"Malformed function_name: {function_name!r}")}

        requested_type, prop, verb = parts
        gid = self._resolve_gid(args)
        if gid is None:
            return {"Error": map_arg("Missing device id (expected args['g'] or args['gid']).")}

        schema = get_device(id=gid)
        if schema is None:
            return {"Error": map_arg(f"Unknown/disconnected device gid={gid} on this host.")}

        actual_type = str(getattr(schema, "device_type", "") or "")
        if actual_type and _normalize_device_rpc_type(requested_type) != _normalize_device_rpc_type(actual_type):
            return {
                "Error": map_arg(
                    f"RPC device type {requested_type!r} does not match configured device type {actual_type!r}"
                )
            }

        if self._should_use_builtin_ip_fallback(gid=gid, prop=prop, verb=verb):
            return {"ip": self._safe_map(f"{actual_type or 'device'}:{gid}")}

        if not master_bypass:
            from .device_manager import get_local_device_context

            context = get_local_device_context(gid) or {}
            effective_blocked = (
                tuple(getattr(schema, "blocked_tags", ()) or ())
                + tuple(context.get("blocked_tags", ()) or ())
                + tuple(policy_tags or ())
            )
            if effective_blocked:
                method_name = v1_method_name(prop, verb)
                get_tags = getattr(schema, "get_method_tags", None)
                if method_name is not None and callable(get_tags):
                    matched = find_blocked_tag(get_tags(method_name), effective_blocked)
                    if matched is not None:
                        return {
                            prop: map_arg("None"),
                            "Error": map_arg(blocked_tag_message(v1_function_label(prop, verb), matched)),
                        }

        if getattr(schema, "session_scoped", False) and getattr(schema, "device", None) is None:
            # The legacy path is about to open the hardware lazily; a Dynamic
            # v2 session left behind by an earlier run must not block it.
            from .dynamic_v2 import session_manager

            session_manager.close_gid(gid)
            if session_manager.cleanup_pending(gid):
                raise RuntimeError("previous native cleanup is still in progress")

        try:
            if verb == "GET":
                result = schema.dispatch(f"get_{prop}", {})
                return {prop: self._safe_map(result)}

            if verb == "SET":
                if prop not in args:
                    raise ValueError(f"SET requires args['{prop}']")
                value = unmap_arg(args[prop])
                schema.dispatch(f"set_{prop}", {"value": value})
                return {prop: map_arg("None")}

            if verb == "CALL0":
                result = schema.dispatch(f"call_{prop}", {})
                return {prop: _wire(result, args) if result is not None else self._safe_map(result)}

            if verb == "CALL1":
                if "arg1" not in args:
                    raise ValueError("CALL1 requires 'arg1' in args")
                value = unmap_arg(args["arg1"])
                result = schema.dispatch(f"call_{prop}", {"value": value})
                return {prop: self._safe_map(result)}

            if verb == "CALLN":
                reserved = {
                    "a", "g", "token", "device_id", "device_name", "wire",
                    "__rrf_reservation_tag", "__rrf_blocked_tags", "__rrf_master_bypass",
                }
                call_args = {
                    str(key): unmap_arg(value)
                    for key, value in dict(args).items()
                    if str(key) not in reserved
                }
                result = schema.dispatch(f"call_{prop}", call_args)
                return {prop: self._safe_map(result)}

            return {"Error": map_arg(f"Unknown verb {verb!r} in {function_name!r}")}
        except Exception as e:
            return {prop: map_arg("None"), "Error": map_arg(str(e))}

    @staticmethod
    def _should_use_builtin_ip_fallback(*, gid: int, prop: str, verb: str) -> bool:
        if prop != "ip" or verb != "CALL0":
            return False

        schema = get_device_schema(gid)
        if schema is None or not hasattr(schema, "list_exposed"):
            return True

        calls = schema.list_exposed().get("calls", [])
        return "call_ip" not in calls

    def _handle_idl(self, sub: str, args: dict) -> dict:
        if sub not in ("get_drivers", "get_schema"):
            return {"error": map_arg(f"Unknown IDL call: {sub!r}")}

        schema = self._resolve_idl_schema(args)
        if schema is None:
            return {"error": map_arg("No schema available for this device on this host.")}

        requested_versions = []
        if "schema_versions" in args:
            try:
                requested_versions = list(unmap_arg(args["schema_versions"]) or [])
            except Exception:
                requested_versions = []
        get_v2 = getattr(schema, "get_idl_v2", None)
        if "2.0" in requested_versions and callable(get_v2):
            schema_v2 = get_v2()
            return {
                "schema": map_arg(json.dumps(
                    schema_v2, sort_keys=True, separators=(",", ":")
                )),
                "schema_version": map_arg("2.0"),
                "driver_version": map_arg(str(getattr(schema, "driver_version", "") or "")),
                "schema_hash": map_arg(schema_v2["schema_hash"]),
                "device_type": map_arg(str(getattr(schema, "device_type", "") or "")),
                "control_protocol_version": map_arg("2.0"),
                "streaming_protocol_version": map_arg("1.0"),
            }

        return {
            "schema": map_arg(schema.get_idl_json()),
            "driver_version": map_arg(str(getattr(schema, "driver_version", "") or "")),
            "schema_hash": map_arg(schema.get_idl_hash()),
            "device_type": map_arg(str(getattr(schema, "device_type", "") or "")),
        }

    @staticmethod
    def _resolve_idl_schema(args: dict):
        gid = RpcManager._resolve_gid(args)
        if gid is not None:
            return get_device_schema(gid)

        if "device_id" in args:
            try:
                gid = int(unmap_arg(args["device_id"]))
            except Exception:
                return None
            return get_device_schema(gid)

        if "device_name" in args:
            try:
                gid = find_device_id_by_name(str(unmap_arg(args["device_name"])))
            except Exception:
                return None
            if gid is None:
                return None
            return get_device_schema(gid)

        return None

    def toggle_debug(self):
        self.debug = not self.debug


rpc_manager = RpcManager()
