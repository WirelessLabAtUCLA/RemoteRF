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
# device_type: krakensdr  driver_version: 0.1.0  schema_hash: sha256:5ddfde1322658a3d0cea2941554300198b3a226c0702cf17b8e3fc109652747d

_PREFIX = "Krakensdr"
_SCHEMA_HASH = "sha256:5ddfde1322658a3d0cea2941554300198b3a226c0702cf17b8e3fc109652747d"
_CLIENT_MODULES = {'krakensdr': 'remoterf.drivers.support.krakensdr'}
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

class KrakenSDR:

    def __init__(self, token: str = None, *, virtual: bool = False):
        self.virtual = bool(virtual)
        if self.virtual:
            self.token = make_virtual_token(token, device_type="krakensdr")
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
    def center_freq(self):
        'Current center frequency in Hz.'
        return _try_get("center_freq", self.token)

    @center_freq.setter
    def center_freq(self, value):
        _try_set("center_freq", value, self.token)

    @property
    def device_info(self):
        'Hardware model, channel count, and connection information.'
        return _try_get("device_info", self.token)

    @property
    def gain(self):
        'Current tuner gain in dB.'
        return _try_get("gain", self.token)

    @gain.setter
    def gain(self, value):
        _try_set("gain", value, self.token)

    @property
    def is_running(self):
        'Whether the KrakenSDR is active and streaming.'
        return _try_get("is_running", self.token)

    @property
    def noise_source(self):
        'Whether the internal calibration noise source is active.'
        return _try_get("noise_source", self.token)

    @noise_source.setter
    def noise_source(self, value):
        _try_set("noise_source", value, self.token)

    @property
    def noise_source_active(self):
        'Whether the internal calibration noise source is active.'
        return _try_get("noise_source_active", self.token)

    @noise_source_active.setter
    def noise_source_active(self, value):
        _try_set("noise_source_active", value, self.token)

    @property
    def num_channels(self):
        'Number of coherent receiver channels (typically 5).'
        return _try_get("num_channels", self.token)

    @property
    def sample_rate(self):
        'Current sample rate in Hz.'
        return _try_get("sample_rate", self.token)

    @sample_rate.setter
    def sample_rate(self, value):
        _try_set("sample_rate", value, self.token)

    @property
    def serials(self):
        'List of tuner serial numbers (Channels 0 to 4).'
        return _try_get("serials", self.token)

    @property
    def stream_stats(self):
        'Throughput, sample counters, and FPS telemetry statistics.'
        return _try_get("stream_stats", self.token)

    @property
    def tuner_serials(self):
        'List of tuner serial numbers (Channels 0 to 4).'
        return _try_get("tuner_serials", self.token)

    def calibrate(self, num_samples=_NO_ARG):
        'Perform automated sample-delay and phase calibration using the noise source.'
        return _try_calln("calibrate", self.token, {
            "num_samples": num_samples,
        })

    def capture_powers(self):
        'Capture instantaneous channel RSSI power in dBFS for all 5 channels.'
        return _try_call("capture_powers", self.token)

    def capture_samples(self, num_samples=_NO_ARG, parallel=_NO_ARG):
        'Capture simultaneous raw IQ samples as a 2D complex64 NumPy array of shape (5, N).'
        return _try_calln("capture_samples", self.token, {
            "num_samples": num_samples,
            "parallel": parallel,
        })

    def capture_spectrum(self, fft_size=_NO_ARG, decay=_NO_ARG):
        'Capture multi-channel FFT power spectral density (PSD) with frequency axis.'
        return _try_calln("capture_spectrum", self.token, {
            "fft_size": fft_size,
            "decay": decay,
        })

    def configure(self, freq=_NO_ARG, rate=_NO_ARG, gain=_NO_ARG):
        'Configure RF parameters (frequency, sample rate, gain).'
        return _try_calln("configure", self.token, {
            "freq": freq,
            "rate": rate,
            "gain": gain,
        })

    def read_frame(self, num_samples=_NO_ARG):
        'Read synchronized multi-channel frame dictionary with metadata.'
        return _try_calln("read_frame", self.token, {
            "num_samples": num_samples,
        })

    def read_samples(self, num_samples=_NO_ARG, parallel=_NO_ARG):
        'Capture simultaneous raw IQ samples as a 2D complex64 NumPy array of shape (5, N).'
        return _try_calln("read_samples", self.token, {
            "num_samples": num_samples,
            "parallel": parallel,
        })

    def set_noise_source(self, enable=_NO_ARG):
        'Toggle internal calibration noise source ON (True) or OFF (False).'
        return _try_calln("set_noise_source", self.token, {
            "enable": enable,
        })

    def start(self):
        'Start SDR acquisition.'
        return _try_call("start", self.token)

    def stop(self):
        'Stop SDR acquisition.'
        return _try_call("stop", self.token)
