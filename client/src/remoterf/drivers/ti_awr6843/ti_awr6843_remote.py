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
# device_type: ti_awr6843  driver_version: 0.1.0  schema_hash: sha256:a052b7e435ea138af9e8b2467a79fb0fcd47094b9cd01d8afe385a7c3b3db0e8

_PREFIX = "Ti_awr6843"
_SCHEMA_HASH = "sha256:a052b7e435ea138af9e8b2467a79fb0fcd47094b9cd01d8afe385a7c3b3db0e8"
_CLIENT_MODULES = {'ti_mmwave': 'remoterf.drivers.support.ti_mmwave'}
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

class TiMmWave:

    def __init__(self, token: str = None, *, virtual: bool = False):
        self.virtual = bool(virtual)
        if self.virtual:
            self.token = make_virtual_token(token, device_type="ti_awr6843")
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
    def clutter_removal(self):
        'Static clutter removal filter state (True = filter walls/furniture).'
        return _try_get("clutter_removal", self.token)

    @clutter_removal.setter
    def clutter_removal(self, value):
        _try_set("clutter_removal", value, self.token)

    @property
    def config(self):
        'Active configuration parameters (preset, mode, clutter, fps).'
        return _try_get("config", self.token)

    @property
    def device_info(self):
        'Serial ports, identity, and firmware profile.'
        return _try_get("device_info", self.token)

    @property
    def firmware_profile(self):
        'Firmware profile identifier (e.g. xwr68xx_oob_sdk3).'
        return _try_get("firmware_profile", self.token)

    @property
    def fps(self):
        'Target frame capture rate in Hz (FPS).'
        return _try_get("fps", self.token)

    @fps.setter
    def fps(self, value):
        _try_set("fps", value, self.token)

    @property
    def is_running(self):
        'Whether the radar is active and transmitting.'
        return _try_get("is_running", self.token)

    @property
    def max_range(self):
        'Maximum detection range in meters.'
        return _try_get("max_range", self.token)

    @max_range.setter
    def max_range(self, value):
        _try_set("max_range", value, self.token)

    @property
    def min_range(self):
        'Minimum detection range in meters.'
        return _try_get("min_range", self.token)

    @min_range.setter
    def min_range(self, value):
        _try_set("min_range", value, self.token)

    @property
    def mode(self):
        "Chirp dimension mode ('3d' with elevation or '2d' azimuth-only)."
        return _try_get("mode", self.token)

    @mode.setter
    def mode(self, value):
        _try_set("mode", value, self.token)

    @property
    def preset(self):
        "Active sensing profile preset ('default', 'indoor-people', 'gesture', 'fast-motion', 'heatmap')."
        return _try_get("preset", self.token)

    @preset.setter
    def preset(self, value):
        _try_set("preset", value, self.token)

    @property
    def sensitivity(self):
        "CFAR detection threshold sensitivity ('low', 'medium', 'high')."
        return _try_get("sensitivity", self.token)

    @sensitivity.setter
    def sensitivity(self, value):
        _try_set("sensitivity", value, self.token)

    @property
    def stream_stats(self):
        'Frame rate, packet counts, and reader buffer health counters.'
        return _try_get("stream_stats", self.token)

    def apply_config(self, config_text, start=_NO_ARG, command_timeout=_NO_ARG):
        'Apply newline-delimited TI CLI configuration commands in order.'
        return _try_calln("apply_config", self.token, {
            "config_text": config_text,
            "start": start,
            "command_timeout": command_timeout,
        })

    def capture_heatmap(self, num_angle_bins=_NO_ARG, max_range=_NO_ARG, timeout=_NO_ARG):
        'Capture 2D Range-Azimuth heatmap with coordinate vectors in dB.'
        return _try_calln("capture_heatmap", self.token, {
            "num_angle_bins": num_angle_bins,
            "max_range": max_range,
            "timeout": timeout,
        })

    def capture_point_cloud(self, timeout=_NO_ARG):
        'Capture detected 3D point cloud with position, velocity, and SNR.'
        return _try_calln("capture_point_cloud", self.token, {
            "timeout": timeout,
        })

    def capture_points(self, timeout=_NO_ARG):
        'Capture detected points as an (N, 4) NumPy array [x, y, z, velocity].'
        return _try_calln("capture_points", self.token, {
            "timeout": timeout,
        })

    def configure(self, preset=_NO_ARG, mode=_NO_ARG, clutter_removal=_NO_ARG, fps=_NO_ARG, start=_NO_ARG):
        'Configure radar parameters via presets (indoor-people, gesture, fast-motion).'
        return _try_calln("configure", self.token, {
            "preset": preset,
            "mode": mode,
            "clutter_removal": clutter_removal,
            "fps": fps,
            "start": start,
        })

    def flush_frames(self):
        'Discard queued and partially assembled data frames.'
        return _try_call("flush_frames", self.token)

    def query_version(self, timeout=_NO_ARG):
        'Query radar firmware version string.'
        return _try_calln("query_version", self.token, {
            "timeout": timeout,
        })

    def read_frame(self, timeout=_NO_ARG):
        'Return one complete raw TI UART packet as bytes.'
        return _try_calln("read_frame", self.token, {
            "timeout": timeout,
        })

    def reset(self):
        'Reset radar state and flush buffers.'
        return _try_call("reset", self.token)

    def send_command(self, command, timeout=_NO_ARG):
        'Send one runtime CLI command and return the textual response.'
        return _try_calln("send_command", self.token, {
            "command": command,
            "timeout": timeout,
        })

    def start(self, timeout=_NO_ARG):
        'Start radar RF transmission and data streaming.'
        return _try_calln("start", self.token, {
            "timeout": timeout,
        })

    def stop(self, timeout=_NO_ARG):
        'Stop radar RF transmission.'
        return _try_calln("stop", self.token, {
            "timeout": timeout,
        })
