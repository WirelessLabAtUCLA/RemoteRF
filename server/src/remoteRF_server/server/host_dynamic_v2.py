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

"""Proxy Dynamic v2 protobuf operations to devices owned by a HostRF agent."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field

from remoterf_device_core.idl.tags import (
    OLD_HOST_TAGS_ERROR,
    blocked_tag_message,
    find_blocked_tag,
)
from remoterf_device_core.manifest import builtin_hashes
from remoterf_device_core.integrations import IntegrationError

from ..common.grpc import grpc_pb2
from ..common.idl.v2 import SCHEMA_VERSION, get_method_tags, get_schema_tags
from ..common.utils import map_arg, unmap_arg
from ..host import host_tunnel_server as hts
from .device_manager import (
    ReservationChangedError,
    VirtualDevice,
    acquire_device,
    get_device_by_id,
    get_device_schema,
    parse_mastertoken,
    register_reservation_end_callback,
    resolve_gid_for_token,
)
from .v2_errors import (
    RemoteRFPolicyError,
    RemoteRFReservationError,
    RemoteRFSessionExpiredError,
    RemoteRFTransportError,
)


def lab_copy(gid: int, device: VirtualDevice, *, version: str = "v1"):
    """The lab's own schema for a host device whose driver is a built-in (its
    host reported, this session, the manifest hash this lab's class also has
    for that schema version), else None: ask the host, which may run a custom driver."""
    try:
        registry = hts.get_tunnel_registry(create=False)
        if registry is None:
            return None
        reported = registry.get_host_device_schema_hash(device.host_id, device.device_id, version=version)
        if not reported or reported not in builtin_hashes():
            return None
        schema = get_device_schema(int(gid))
        if schema is None:
            return None
        ours = schema.get_idl_hash() if version == "v1" else schema.get_idl_v2()["schema_hash"]
        return schema if ours == reported else None
    except Exception:  # noqa: BLE001 - when in doubt, the host describes its own device
        return None


@dataclass
class HostSessionRoute:
    session_id: str
    gid: int
    host_id: str
    device_id: str
    reservation_tag: str = ""
    blocked_tags: tuple[str, ...] = ()
    is_master: bool = False
    schema_v2: dict | None = None
    root_handle: str = ""
    handles: dict[str, str] = field(default_factory=dict)


class HostDynamicV2Proxy:
    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: dict[str, HostSessionRoute] = {}

    @staticmethod
    def _virtual_for_gid(gid: int) -> VirtualDevice | None:
        resolved = get_device_by_id(int(gid))
        if resolved is None:
            return None
        device = resolved[0]
        return device if isinstance(device, VirtualDevice) else None

    def route_for_token(self, token: str) -> tuple[int, VirtualDevice] | None:
        try:
            gid = resolve_gid_for_token(token)
            if gid is None or self._virtual_for_gid(gid) is None:
                return None
            # Schema/session routing has always allowed master tokens on held
            # devices; admission must bind them to the current holder's tag.
            parsed = parse_mastertoken(token)
            if parsed and not parsed[1]:
                token = f"{token}_force"
            with acquire_device(token) as (gid, device):
                return (int(gid), device) if isinstance(device, VirtualDevice) else None
        except ReservationChangedError as exc:
            raise RemoteRFReservationError(str(exc)) from exc
        except (RuntimeError, IntegrationError):
            return None

    def is_remote_session(self, session_id: str) -> bool:
        with self._lock:
            return str(session_id) in self._sessions

    def _route(self, session_id: str) -> HostSessionRoute:
        with self._lock:
            route = self._sessions.get(str(session_id))
        if route is None:
            raise RemoteRFSessionExpiredError("unknown or closed Dynamic v2 host session")
        return route

    @staticmethod
    def _require_host_tag_support(host_id: str, blocked_tags: tuple[str, ...]) -> None:
        if blocked_tags and not hts.host_supports_function_tags(host_id):
            raise RemoteRFPolicyError(OLD_HOST_TAGS_ERROR)

    def _ensure_lab_schema_v2(self, route: HostSessionRoute) -> dict | None:
        if route.schema_v2 is not None or not route.blocked_tags:
            return route.schema_v2
        device = self._virtual_for_gid(route.gid)
        if device is None:
            return None
        local = lab_copy(route.gid, device, version="v2")
        if local is None:
            return None
        schema_v2 = local.get_idl_v2()
        with self._lock:
            route.schema_v2 = schema_v2
        return schema_v2

    def _check_lab_v2_blocked_tags(
        self,
        route: HostSessionRoute,
        owner: str | None,
        method_name: str,
    ) -> None:
        if route.is_master or not route.blocked_tags:
            return
        schema_v2 = self._ensure_lab_schema_v2(route)
        if schema_v2 is None:
            return
        matched = None
        if owner:
            matched = find_blocked_tag(
                get_method_tags(schema_v2, owner, method_name),
                route.blocked_tags,
            )
        else:
            for (_owner_type, name), method_tags in get_schema_tags(schema_v2).items():
                if name == method_name:
                    matched = find_blocked_tag(method_tags, route.blocked_tags)
                    if matched is not None:
                        break
        if matched is not None:
            raise RemoteRFPolicyError(
                blocked_tag_message(method_name, matched),
                method=method_name,
            )

    @staticmethod
    def _forward(
        route: HostSessionRoute | VirtualDevice,
        operation: str,
        request,
        response_type,
        *,
        timeout_sec: float = 30.0,
    ):
        registry = hts.get_tunnel_registry()
        if registry is None:
            raise RemoteRFTransportError("host tunnel registry is unavailable")

        host_id = route.host_id
        device_id = route.device_id
        if "token" in request.DESCRIPTOR.fields_by_name:
            # The lab already checked the reservation; the host swaps in its own
            # bridge token, so the client's never leaves this process.
            stripped = type(request)()
            stripped.CopyFrom(request)
            stripped.ClearField("token")
            request = stripped
        fwd_args = {
            "wire": map_arg(request.SerializeToString()),
            "__rrf_reservation_tag": map_arg(route.reservation_tag),
        }
        if getattr(route, "is_master", False):
            fwd_args["__rrf_master_bypass"] = map_arg(True)
        results = hts.handle_host_device(
            registry,
            host_id=host_id,
            device_id=device_id,
            function_name=f"DynamicV2:{operation}",
            args=fwd_args,
            timeout_sec=float(timeout_sec),
        )

        for key in ("Error", "error"):
            if key in results:
                try:
                    message = str(unmap_arg(results[key]))
                except Exception:
                    message = "host Dynamic v2 request failed"
                raise RemoteRFTransportError(
                    message,
                    details={
                        "host_id": host_id,
                        "device_id": device_id,
                        "operation": operation,
                    },
                )

        argument = results.get("wire")
        if argument is None:
            raise RemoteRFTransportError(
                "host Dynamic v2 response is missing wire data",
                details={"operation": operation},
            )
        wire = unmap_arg(argument)
        if not isinstance(wire, bytes):
            raise RemoteRFTransportError(
                "host Dynamic v2 wire response is not bytes",
                details={"operation": operation},
            )
        try:
            return response_type.FromString(wire)
        except Exception as exc:
            raise RemoteRFTransportError(
                "host Dynamic v2 response could not be decoded",
                details={"operation": operation},
            ) from exc

    def get_schema(self, request):
        target = self.route_for_token(request.token)
        if target is None:
            raise RemoteRFReservationError("invalid token or inactive host reservation")
        gid, device = target
        blocked_tags = () if getattr(device, "is_master", False) else tuple(getattr(device, "blocked_tags", ()) or ())
        self._require_host_tag_support(device.host_id, blocked_tags)
        local = lab_copy(gid, device, version="v2")
        if local is not None:
            schema = local.get_idl_v2()
            return grpc_pb2.GetSchemaResponse(
                schema_version=SCHEMA_VERSION,
                schema_json=json.dumps(schema, sort_keys=True, separators=(",", ":")),
                schema_hash=schema["schema_hash"],
            )
        return self._forward(
            device,
            "GetSchema",
            request,
            grpc_pb2.GetSchemaResponse,
        )

    def open(self, request):
        target = self.route_for_token(request.token)
        if target is None:
            raise RemoteRFReservationError("invalid token or inactive host reservation")
        gid, device = target
        is_master = bool(getattr(device, "is_master", False))
        blocked_tags = () if is_master else tuple(getattr(device, "blocked_tags", ()) or ())
        self._require_host_tag_support(device.host_id, blocked_tags)
        local = lab_copy(gid, device, version="v2") if blocked_tags else None
        schema_v2 = local.get_idl_v2() if local is not None else None
        response = self._forward(
            device,
            "OpenSession",
            request,
            grpc_pb2.OpenSessionResponse,
        )
        if response.session_id and not response.error.code:
            handles = {response.device_handle: "uhd.usrp.MultiUSRP"} if response.device_handle else {}
            route = HostSessionRoute(
                session_id=response.session_id,
                gid=gid,
                host_id=device.host_id,
                device_id=device.device_id,
                reservation_tag=device.reservation_tag,
                blocked_tags=blocked_tags,
                is_master=is_master,
                schema_v2=schema_v2,
                root_handle=response.device_handle,
                handles=handles,
            )
            with self._lock:
                if response.session_id in self._sessions:
                    raise RemoteRFTransportError("host returned a duplicate session id")
                self._sessions[response.session_id] = route
        return response

    def session_gid(self, session_id: str) -> int:
        return self._route(session_id).gid

    def invoke(self, request):
        route = self._route(request.session_id)
        self._require_host_tag_support(route.host_id, route.blocked_tags)
        if route.blocked_tags:
            with self._lock:
                owner = route.handles.get(request.handle)
            if owner is None and request.handle == route.root_handle:
                owner = "uhd.usrp.MultiUSRP"
            self._check_lab_v2_blocked_tags(route, owner, request.method)
        response = self._forward(
            route,
            "Invoke",
            request,
            grpc_pb2.InvokeResponse,
        )
        if (
            not response.error.code
            and response.HasField("result")
            and response.result.WhichOneof("value") == "json_value"
            and "__remoterf_handle__" in response.result.json_value
        ):
            try:
                parsed = json.loads(response.result.json_value)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                child_handle = parsed.get("__remoterf_handle__")
                child_type = parsed.get("__remoterf_type__")
                if isinstance(child_handle, str) and child_handle and isinstance(child_type, str) and child_type:
                    with self._lock:
                        route.handles[child_handle] = child_type
        return response

    def close_handle(self, request):
        route = self._route(request.session_id)
        try:
            return self._forward(
                route,
                "CloseHandle",
                request,
                grpc_pb2.CloseResponse,
            )
        finally:
            with self._lock:
                route.handles.pop(request.handle, None)

    def close_session(self, request):
        route = self._route(request.session_id)
        try:
            return self._forward(
                route,
                "CloseSession",
                request,
                grpc_pb2.CloseResponse,
            )
        finally:
            with self._lock:
                self._sessions.pop(request.session_id, None)

    def sample_frame(self, request):
        route = self._route(request.session_id)
        self._require_host_tag_support(route.host_id, route.blocked_tags)
        if route.blocked_tags:
            if request.direction == grpc_pb2.SAMPLE_DIRECTION_TX and request.kind in {
                grpc_pb2.SAMPLE_FRAME_OPEN,
                grpc_pb2.SAMPLE_FRAME_DATA,
            }:
                with self._lock:
                    owner = route.handles.get(request.handle) or "uhd.usrp.TXStreamer"
                self._check_lab_v2_blocked_tags(route, owner, "send")
            elif request.direction == grpc_pb2.SAMPLE_DIRECTION_RX and request.kind in {
                grpc_pb2.SAMPLE_FRAME_OPEN,
                grpc_pb2.SAMPLE_FRAME_REQUEST,
            }:
                with self._lock:
                    owner = route.handles.get(request.handle) or "uhd.usrp.RXStreamer"
                self._check_lab_v2_blocked_tags(route, owner, "recv")
        timeout = max(30.0, float(request.timeout_sec or 0.0) + 5.0)
        return self._forward(
            route,
            "SampleFrame",
            request,
            grpc_pb2.SampleFrame,
            timeout_sec=timeout,
        )

    def gid_sessions(self, gid: int) -> list[str]:
        with self._lock:
            return [
                session_id
                for session_id, route in self._sessions.items()
                if route.gid == int(gid)
            ]

    def close_gid(self, gid: int, session_ids: list[str] | None = None) -> None:
        if session_ids is None:
            session_ids = self.gid_sessions(gid)
        for session_id in session_ids:
            try:
                self.close_session(
                    grpc_pb2.CloseSessionRequest(session_id=session_id)
                )
            except Exception:
                with self._lock:
                    self._sessions.pop(session_id, None)

    def close_all(self) -> int:
        with self._lock:
            session_ids = list(self._sessions)
        for session_id in session_ids:
            try:
                self.close_session(
                    grpc_pb2.CloseSessionRequest(session_id=session_id)
                )
            except Exception:
                with self._lock:
                    self._sessions.pop(session_id, None)
        return len(session_ids)

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return {"host_sessions": len(self._sessions)}


host_session_proxy = HostDynamicV2Proxy()


def _end_host_reservation(gid: int) -> None:
    """Tell the device's host to close its sessions and reset it, then drop the routes here.

    The frame goes first: the next holder is already set, and the host handles
    it before any of that holder's frames queued behind it.
    """
    device = HostDynamicV2Proxy._virtual_for_gid(gid)
    registry = hts.get_tunnel_registry(create=False)
    if device is not None and registry is not None:
        registry.send_control(
            device.host_id, kind="reservation_end", device_id=device.device_id,
            payload={"at": time.time()}, drop_on_failure=True,
        )
    # CloseSession waits for a tunnel reply; Connect's inbound thread also
    # needs the reservation lock to resync, so never wait here under that lock.
    # Pick the sessions now: the next holder may open its own before the thread runs.
    sessions = host_session_proxy.gid_sessions(gid)
    threading.Thread(target=host_session_proxy.close_gid, args=(gid, sessions), daemon=True).start()


register_reservation_end_callback(_end_host_reservation)
