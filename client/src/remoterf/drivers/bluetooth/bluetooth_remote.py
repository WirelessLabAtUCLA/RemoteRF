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
# device_type: bluetooth  driver_version: 0.1.0  schema_hash: sha256:72644c2af2d5e202f76fd07b9a30554f4deca2fdceffddaa21e2c3eaca9056d9

_PREFIX = "Bluetooth"
_SCHEMA_HASH = "sha256:72644c2af2d5e202f76fd07b9a30554f4deca2fdceffddaa21e2c3eaca9056d9"
_CLIENT_MODULES = {}
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

class BluetoothModule:

    def __init__(self, token: str = None, *, virtual: bool = False):
        self.virtual = bool(virtual)
        if self.virtual:
            self.token = make_virtual_token(token, device_type="bluetooth")
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
    def adapter(self):
        "HCI adapter name (e.g. 'hci1')."
        return _try_get("adapter", self.token)

    @property
    def address(self):
        'Bluetooth hardware BD_ADDR (MAC address).'
        return _try_get("address", self.token)

    @property
    def bd_address(self):
        'Bluetooth hardware BD_ADDR alias.'
        return _try_get("bd_address", self.token)

    @property
    def chipset(self):
        'Chipset or device model name.'
        return _try_get("chipset", self.token)

    @property
    def device_count(self):
        'Number of devices currently cached from recent scans.'
        return _try_get("device_count", self.token)

    @property
    def discovering(self):
        'Whether the adapter is actively discovering.'
        return _try_get("discovering", self.token)

    @property
    def is_zexmte(self):
        'Whether the adapter is a ZEXMTE CSR8510 module.'
        return _try_get("is_zexmte", self.token)

    @property
    def powered(self):
        'RF power state of the Bluetooth controller (True=ON, False=OFF).'
        return _try_get("powered", self.token)

    @powered.setter
    def powered(self, value):
        _try_set("powered", value, self.token)

    @property
    def version(self):
        'Bluetooth Core specification version string.'
        return _try_get("version", self.token)

    def get_device(self, address):
        'Retrieve cached information for a specific MAC address.'
        return _try_call("get_device", self.token, address)

    def get_devices(self, *, named_only=_NO_ARG, min_rssi=_NO_ARG, limit=_NO_ARG):
        'Retrieve all currently cached devices without scanning.'
        return _try_calln("get_devices", self.token, {
            "named_only": named_only,
            "min_rssi": min_rssi,
            "limit": limit,
        })

    def info(self):
        'Get full adapter hardware and driver metadata.'
        return _try_call("info", self.token)

    def reset(self):
        'Reset Bluetooth adapter discovery state and clear cache.'
        return _try_call("reset", self.token)

    def scan(self, timeout=_NO_ARG, *, named_only=_NO_ARG, min_rssi=_NO_ARG, limit=_NO_ARG):
        'Scan for Bluetooth devices in the vicinity.'
        return _try_calln("scan", self.token, {
            "timeout": timeout,
            "named_only": named_only,
            "min_rssi": min_rssi,
            "limit": limit,
        })

    def scan_summary(self, timeout=_NO_ARG):
        'Perform a scan and return high-level vicinity statistics.'
        return _try_calln("scan_summary", self.token, {
            "timeout": timeout,
        })
