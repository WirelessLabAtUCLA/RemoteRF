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

# Auto-generated from IDL schema — do not edit by hand.
# device_type: wifi  driver_version: 0.1.0  schema_hash: sha256:64e8e3ddbdb214abbade83769a87dd9a3ec730d6d924e339388dcc245cff5d09

_PREFIX = "Wifi"
_SCHEMA_HASH = "sha256:64e8e3ddbdb214abbade83769a87dd9a3ec730d6d924e339388dcc245cff5d09"
_CLIENT_MODULES = {'wifi': 'remoterf.drivers.support.wifi'}
_CLIENT_OBJECTS = {}

import os

from importlib import import_module as _import_module

from ...common.utils import map_arg, unmap_arg
from .._virtual import (
    is_virtual_token,
    make_virtual_token,
    virtual_call,
    virtual_get,
    virtual_set,
)

_NO_ARG = object()


def _rpc_client(*, function_name, args):
    from ...core.grpc_client import rpc_client

    return rpc_client(function_name=function_name, args=args)


for _alias, _module_path in _CLIENT_MODULES.items():
    globals()[_alias] = _import_module(_module_path)


def _resolve_client_target(target):
    parts = str(target or "").split(".")
    if not parts or not parts[0]:
        raise ValueError(f"Invalid client constructor target: {target!r}")
    if parts[0] not in globals():
        raise ValueError(
            f"Client helper module {parts[0]!r} is not declared in _CLIENT_MODULES"
        )
    obj = globals()[parts[0]]
    for part in parts[1:]:
        obj = getattr(obj, part)
    return obj


def _client_ctor_arg(spec, self_obj, result):
    if spec == "$self":
        return self_obj
    if spec == "$result":
        return result
    if isinstance(spec, dict) and "const" in spec:
        return spec["const"]
    return spec


def _wrap_client_return(spec, self_obj, result):
    if not spec:
        return result
    if spec.get("kind") != "constructor":
        raise ValueError(f"Unsupported client_return metadata: {spec!r}")
    ctor = _resolve_client_target(spec.get("target"))
    args = [
        _client_ctor_arg(item, self_obj, result)
        for item in spec.get("args", [])
    ]
    return ctor(*args)


def _try_get(prop, token):
    if is_virtual_token(token):
        return virtual_get(token, prop)
    return unmap_arg(_rpc_client(
        function_name=f"{_PREFIX}:{prop}:GET",
        args={'a': map_arg(token)},
    ).results[prop])


def _try_set(prop, value, token):
    if is_virtual_token(token):
        virtual_set(token, prop, value)
        return None
    _rpc_client(
        function_name=f"{_PREFIX}:{prop}:SET",
        args={prop: map_arg(value), 'a': map_arg(token)},
    )


def _try_call(prop, token, arg=_NO_ARG):
    if is_virtual_token(token):
        return virtual_call(token, prop, arg, has_arg=arg is not _NO_ARG)
    if arg is _NO_ARG:
        resp = _rpc_client(
            function_name=f"{_PREFIX}:{prop}:CALL0",
            args={'a': map_arg(token)},
        )
    else:
        resp = _rpc_client(
            function_name=f"{_PREFIX}:{prop}:CALL1",
            args={'a': map_arg(token), 'arg1': map_arg(arg)},
        )
    result = resp.results.get(prop)
    return unmap_arg(result) if result is not None else None


def _try_calln(prop, token, kwargs):
    if is_virtual_token(token):
        filtered = {key: value for key, value in dict(kwargs).items() if value is not _NO_ARG}
        return virtual_call(token, prop, filtered, has_arg=True)
    payload = {'a': map_arg(token)}
    for key, value in dict(kwargs).items():
        if value is _NO_ARG:
            continue
        payload[str(key)] = map_arg(value)
    resp = _rpc_client(
        function_name=f"{_PREFIX}:{prop}:CALLN",
        args=payload,
    )
    result = resp.results.get(prop)
    return unmap_arg(result) if result is not None else None

class WifiDongle:

    def __init__(self, token: str = None, *, virtual: bool = False):
        self.virtual = bool(virtual)
        if self.virtual:
            self.token = make_virtual_token(token, device_type="wifi")
            return
        if token is None:
            token = os.getenv("REMOTERF_TOKEN")
        if not token:
            raise ValueError("A reservation token is required; pass token= or set REMOTERF_TOKEN")
        self.token = token
        from ..dynamic_device import install_driver_if_stale
        if install_driver_if_stale(token=token, current_hash=_SCHEMA_HASH, instance=self):
            return  # now the lab's own class, set up by its __init__

    def batch(self):
        from ...core.grpc_client import batch
        return batch(self.token)

    @property
    def auto_rescan(self):
        'Whether active rescan is automatically triggered on scan calls.'
        return _try_get("auto_rescan", self.token)

    @auto_rescan.setter
    def auto_rescan(self, value):
        _try_set("auto_rescan", value, self.token)

    @property
    def default_route_safe(self):
        'Verification that default Ethernet gateway routing is intact.'
        return _try_get("default_route_safe", self.token)

    @property
    def device_info(self):
        'Hardware vendor ID, product ID, driver, and model.'
        return _try_get("device_info", self.token)

    @property
    def interface(self):
        'Wireless interface name used by this dongle.'
        return _try_get("interface", self.token)

    @property
    def is_available(self):
        'Whether the USB dongle interface is present and active.'
        return _try_get("is_available", self.token)

    @property
    def last_scan_time(self):
        'UNIX timestamp of the most recent scan.'
        return _try_get("last_scan_time", self.token)

    @property
    def scan_count(self):
        'Total number of scans executed since device initialization.'
        return _try_get("scan_count", self.token)

    def get_ap(self, target, *, rescan=_NO_ARG):
        'Get full AccessPoint metadata dictionary for a target.'
        return _try_calln("get_ap", self.token, {
            "target": target,
            "rescan": rescan,
        })

    def get_ap_details(self, target):
        'Extract deep hardware & WPS telemetry (manufacturer, model, TSF clock).'
        return _try_call("get_ap_details", self.token, target)

    def get_channel_distribution(self):
        'Channel distribution map showing AP density and signal by channel.'
        return _try_call("get_channel_distribution", self.token)

    def get_history(self, target=_NO_ARG):
        'Get buffered history of RSSI measurements over time.'
        return _try_calln("get_history", self.token, {
            "target": target,
        })

    def get_rssi(self, target, *, rescan=_NO_ARG):
        'Get the RSSI value (in dBm) for a target SSID or BSSID.'
        return _try_calln("get_rssi", self.token, {
            "target": target,
            "rescan": rescan,
        })

    def get_rssi_burst(self, target, *, count=_NO_ARG, max_time=_NO_ARG, rescan=_NO_ARG):
        'Collect burst of RSSI measurements with linear-power averaging.'
        return _try_calln("get_rssi_burst", self.token, {
            "target": target,
            "count": count,
            "max_time": max_time,
            "rescan": rescan,
        })

    def scan(self, rescan=_NO_ARG, *, band=_NO_ARG, min_rssi=_NO_ARG):
        'Scan nearby Wi-Fi APs. Returns list of access point dictionaries.'
        return _try_calln("scan", self.token, {
            "rescan": rescan,
            "band": band,
            "min_rssi": min_rssi,
        })

    def scan_summary(self):
        'High-level RF environment summary (AP counts, band stats, strongest AP).'
        return _try_call("scan_summary", self.token)
