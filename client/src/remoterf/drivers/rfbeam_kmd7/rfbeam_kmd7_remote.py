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
# device_type: rfbeam_kmd7  driver_version: 0.2.0  schema_hash: sha256:a83ef756c00700b1be9eb4ec049725cbd66e2ebfb2f70a637fe397af4d1ddef7

_PREFIX = "Rfbeam_kmd7"
_SCHEMA_HASH = "sha256:a83ef756c00700b1be9eb4ec049725cbd66e2ebfb2f70a637fe397af4d1ddef7"
_CLIENT_MODULES = {'rfbeam_kmd7': 'remoterf.drivers.support.rfbeam_kmd7'}
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

class RFBeamKMD7:

    def __init__(self, token: str = None, *, virtual: bool = False):
        self.virtual = bool(virtual)
        if self.virtual:
            self.token = make_virtual_token(token, device_type="rfbeam_kmd7")
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
    def baudrate(self):
        'Active UART baud rate in bps (115200..3000000).'
        return _try_get("baudrate", self.token)

    @baudrate.setter
    def baudrate(self, value):
        _try_set("baudrate", value, self.token)

    @property
    def device_info(self):
        'Serial port, connection identity, and firmware version.'
        return _try_get("device_info", self.token)

    @property
    def direction_filter(self):
        'Detection direction filter: 0=Receding, 1=Approaching, 2=Both.'
        return _try_get("direction_filter", self.token)

    @direction_filter.setter
    def direction_filter(self, value):
        _try_set("direction_filter", value, self.token)

    @property
    def firmware_version(self):
        'Firmware version string (e.g. K-MD7_APP-RFB-0205).'
        return _try_get("firmware_version", self.token)

    @property
    def frequency_channel(self):
        'RF base frequency channel (0=Low, 1=Middle, 2=High).'
        return _try_get("frequency_channel", self.token)

    @frequency_channel.setter
    def frequency_channel(self, value):
        _try_set("frequency_channel", value, self.token)

    @property
    def is_running(self):
        'Whether the radar is active and streaming.'
        return _try_get("is_running", self.token)

    @property
    def max_range(self):
        'Maximum unambiguous detection range in meters (100, 200, 300).'
        return _try_get("max_range", self.token)

    @max_range.setter
    def max_range(self, value):
        _try_set("max_range", value, self.token)

    @property
    def max_speed(self):
        'Maximum measurable speed in km/h (50, 100, 200).'
        return _try_get("max_speed", self.token)

    @max_speed.setter
    def max_speed(self, value):
        _try_set("max_speed", value, self.token)

    @property
    def max_speed_filter(self):
        'Maximum detection speed filter as percentage of speed setting (0 - 100%).'
        return _try_get("max_speed_filter", self.token)

    @max_speed_filter.setter
    def max_speed_filter(self, value):
        _try_set("max_speed_filter", value, self.token)

    @property
    def max_zone_angle(self):
        'Maximum detection zone azimuth angle in degrees (-30 to +30).'
        return _try_get("max_zone_angle", self.token)

    @max_zone_angle.setter
    def max_zone_angle(self, value):
        _try_set("max_zone_angle", value, self.token)

    @property
    def max_zone_distance(self):
        'Maximum detection zone distance as percentage of range setting (0 - 100%).'
        return _try_get("max_zone_distance", self.token)

    @max_zone_distance.setter
    def max_zone_distance(self, value):
        _try_set("max_zone_distance", value, self.token)

    @property
    def min_speed_filter(self):
        'Minimum detection speed filter as percentage of speed setting (0 - 100%).'
        return _try_get("min_speed_filter", self.token)

    @min_speed_filter.setter
    def min_speed_filter(self, value):
        _try_set("min_speed_filter", value, self.token)

    @property
    def min_zone_angle(self):
        'Minimum detection zone azimuth angle in degrees (-30 to +30).'
        return _try_get("min_zone_angle", self.token)

    @min_zone_angle.setter
    def min_zone_angle(self, value):
        _try_set("min_zone_angle", value, self.token)

    @property
    def min_zone_distance(self):
        'Minimum detection zone distance as percentage of range setting (0 - 100%).'
        return _try_get("min_zone_distance", self.token)

    @min_zone_distance.setter
    def min_zone_distance(self, value):
        _try_set("min_zone_distance", value, self.token)

    @property
    def mode(self):
        "Active operating mode ('indoor' for pedestrian/gestures, 'traffic' for vehicles)."
        return _try_get("mode", self.token)

    @mode.setter
    def mode(self, value):
        _try_set("mode", value, self.token)

    @property
    def radar_parameters(self):
        'Complete 31-byte radar parameter structure (RPST).'
        return _try_get("radar_parameters", self.token)

    @property
    def stream_mask(self):
        'Active RDOT streaming output bitmask (0 when stopped).'
        return _try_get("stream_mask", self.token)

    @property
    def stream_stats(self):
        'Frame rate, packet counts, and reader health statistics.'
        return _try_get("stream_stats", self.token)

    @property
    def threshold_offset(self):
        'Detection threshold offset in dB (0 - 60 dB). Lower is more sensitive.'
        return _try_get("threshold_offset", self.token)

    @threshold_offset.setter
    def threshold_offset(self, value):
        _try_set("threshold_offset", value, self.token)

    @property
    def tracking_filter(self):
        'Tracking filter type: 0=Standard, 1=Fast detection, 2=Long visibility.'
        return _try_get("tracking_filter", self.token)

    @tracking_filter.setter
    def tracking_filter(self, value):
        _try_set("tracking_filter", value, self.token)

    def capture_full_frame(self, timeout=_NO_ARG, *, raw_adc=_NO_ARG, raw_fft=_NO_ARG, raw_targets=_NO_ARG, tracked_targets=_NO_ARG, use_handshake=_NO_ARG):
        'Capture RADC, RFFT, PDAT, and TDAT together from one radar frame.'
        return _try_calln("capture_full_frame", self.token, {
            "timeout": timeout,
            "raw_adc": raw_adc,
            "raw_fft": raw_fft,
            "raw_targets": raw_targets,
            "tracked_targets": tracked_targets,
            "use_handshake": use_handshake,
        })

    def capture_next_frame(self, *, raw_adc=_NO_ARG, raw_fft=_NO_ARG, raw_targets=_NO_ARG, tracked_targets=_NO_ARG, timeout=_NO_ARG):
        'Request one frame on demand via GNFD handshake mode.'
        return _try_calln("capture_next_frame", self.token, {
            "raw_adc": raw_adc,
            "raw_fft": raw_fft,
            "raw_targets": raw_targets,
            "tracked_targets": tracked_targets,
            "timeout": timeout,
        })

    def capture_point_cloud(self, timeout=_NO_ARG):
        'Capture structured 2D point cloud with positions, velocities, and power.'
        return _try_calln("capture_point_cloud", self.token, {
            "timeout": timeout,
        })

    def capture_points(self, timeout=_NO_ARG, *, include_raw=_NO_ARG, include_tracked=_NO_ARG):
        'Capture points as an (N, 6) NumPy array [x, y, dist, speed, angle, mag].'
        return _try_calln("capture_points", self.token, {
            "timeout": timeout,
            "include_raw": include_raw,
            "include_tracked": include_tracked,
        })

    def capture_raw_adc(self, timeout=_NO_ARG, *, use_handshake=_NO_ARG):
        'Capture raw ADC I/Q samples (RADC: 3 IF blocks x 512 I/Q uint16).'
        return _try_calln("capture_raw_adc", self.token, {
            "timeout": timeout,
            "use_handshake": use_handshake,
        })

    def capture_raw_fft(self, timeout=_NO_ARG, *, use_handshake=_NO_ARG):
        'Capture raw 512-point FFT spectrum and threshold in dB (RFFT).'
        return _try_calln("capture_raw_fft", self.token, {
            "timeout": timeout,
            "use_handshake": use_handshake,
        })

    def capture_targets(self, timeout=_NO_ARG):
        'Capture active tracked targets as a list of structured dictionaries.'
        return _try_calln("capture_targets", self.token, {
            "timeout": timeout,
        })

    def configure(self, mode=_NO_ARG, *, max_range=_NO_ARG, max_speed=_NO_ARG, threshold_offset=_NO_ARG, tracking_filter=_NO_ARG, min_speed_filter=_NO_ARG, max_speed_filter=_NO_ARG, direction_filter=_NO_ARG, frequency_channel=_NO_ARG, min_zone_distance=_NO_ARG, max_zone_distance=_NO_ARG, min_zone_angle=_NO_ARG, max_zone_angle=_NO_ARG):
        'Configure radar parameters via high-level presets and optional overrides.'
        return _try_calln("configure", self.token, {
            "mode": mode,
            "max_range": max_range,
            "max_speed": max_speed,
            "threshold_offset": threshold_offset,
            "tracking_filter": tracking_filter,
            "min_speed_filter": min_speed_filter,
            "max_speed_filter": max_speed_filter,
            "direction_filter": direction_filter,
            "frequency_channel": frequency_channel,
            "min_zone_distance": min_zone_distance,
            "max_zone_distance": max_zone_distance,
            "min_zone_angle": min_zone_angle,
            "max_zone_angle": max_zone_angle,
        })

    def flush_frames(self):
        'Flush serial input buffer.'
        return _try_call("flush_frames", self.token)

    def read_frame(self, timeout=_NO_ARG):
        'Read one synchronized radar frame summary.'
        return _try_calln("read_frame", self.token, {
            "timeout": timeout,
        })

    def restore_factory_settings(self):
        'Restore factory default settings (RFSE).'
        return _try_call("restore_factory_settings", self.token)

    def set_detection_zone(self, min_dist_pct=_NO_ARG, max_dist_pct=_NO_ARG, min_angle_deg=_NO_ARG, max_angle_deg=_NO_ARG):
        'Set detection zone distance (%) and azimuth angle (-30..+30 deg).'
        return _try_calln("set_detection_zone", self.token, {
            "min_dist_pct": min_dist_pct,
            "max_dist_pct": max_dist_pct,
            "min_angle_deg": min_angle_deg,
            "max_angle_deg": max_angle_deg,
        })

    def set_radar_parameters(self, params):
        'Update complete radar parameter structure atomically (SRPS).'
        return _try_call("set_radar_parameters", self.token, params)

    def start(self, raw_targets=_NO_ARG, tracked_targets=_NO_ARG, *, raw_adc=_NO_ARG, raw_fft=_NO_ARG, bitmask=_NO_ARG):
        'Start radar streaming with selectable output messages (RDOT).'
        return _try_calln("start", self.token, {
            "raw_targets": raw_targets,
            "tracked_targets": tracked_targets,
            "raw_adc": raw_adc,
            "raw_fft": raw_fft,
            "bitmask": bitmask,
        })

    def stop(self):
        'Stop radar streaming cleanly via RDOT(0x00).'
        return _try_call("stop", self.token)
