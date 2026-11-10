"""Synthetic native values only. This module never imports or opens native UHD."""
import math
import re
import sys
import time
from types import ModuleType, SimpleNamespace

UHD_VERSION = 'dev-fixture-no-hardware'
SERIAL = re.compile(r'DEV_[A-Z0-9_]{1,60}')


def serial(value):
    if type(value) is not str or SERIAL.fullmatch(value) is None:
        raise RuntimeError('dev fixture requires a DEV_ synthetic serial')
    return value


class time_spec:
    def __init__(self, secs=0):
        self.secs = float(secs)
    def get_real_secs(self):
        return self.secs
    def get_full_secs(self):
        return int(self.secs)
    def get_frac_secs(self):
        return self.secs - int(self.secs)


class rx_metadata:
    def __init__(self):
        self.reset()
    def reset(self):
        self.error_code = 'none'
        self.has_time_spec = True
        self.time_spec = time_spec()
        self.more_fragments = False
        self.fragment_offset = 0
        self.out_of_sequence = False
    def strerror(self):
        return str(self.error_code)


class tx_metadata:
    def __init__(self):
        self.has_time_spec = False
        self.time_spec = time_spec()
        self.start_of_burst = False
        self.end_of_burst = False


class async_metadata:
    def __init__(self):
        self.event_code = 'burst_ack'
        self.has_time_spec = False
        self.time_spec = time_spec()
        self.channel = 0
        self.user_payload = []


class stream_args:
    def __init__(self, cpu_format='fc32', otw_format='sc16'):
        self.cpu_format, self.otw_format = cpu_format, otw_format
        self.channels, self.args = [], ''


class stream_cmd:
    def __init__(self, mode):
        self.stream_mode = self.mode = mode
        self.num_samps = 0
        self.stream_now = True
        self.time_spec = time_spec()


class tune_request:
    def __init__(self, target_freq, lo_offset=0):
        self.target_freq, self.lo_offset = float(target_freq), float(lo_offset)
        self.rf_freq = self.target_freq + self.lo_offset
        self.dsp_freq = 0.0
        self.rf_freq_policy = self.dsp_freq_policy = 'AUTO'
        self.args = ''


class tune_result:
    def __init__(self, frequency):
        self.clipped_rf_freq = self.target_rf_freq = self.actual_rf_freq = frequency
        self.target_dsp_freq = self.actual_dsp_freq = 0.0


class FakeStreamer:
    def __init__(self, owner, direction, arguments):
        self.owner, self.direction = owner, direction
        self.channels = list(arguments.channels or [0])
        if self.channels != [0] or arguments.cpu_format not in {'fc32', 'fc64'}:
            raise ValueError('fixture supports one complex channel only')
        self.closed = False
        self.remaining = None
    def get_num_channels(self):
        return 1
    def get_max_num_samps(self):
        return 4096
    def issue_stream_cmd(self, command):
        if self.closed or self.owner.closed:
            raise RuntimeError('synthetic stream is closed')
        mode = str(command.stream_mode).split('.')[-1]
        if mode == 'stop_cont':
            self.remaining = 0
        elif mode in {'num_done', 'num_more'}:
            self.remaining = int(command.num_samps)
        elif mode == 'start_cont':
            self.remaining = None
        else:
            raise ValueError('unsupported synthetic stream command')
    def recv(self, buffer, metadata, timeout=0.1):
        import numpy as np
        if self.direction != 'rx' or self.closed or self.owner.closed:
            raise RuntimeError('synthetic RX stream is closed')
        time.sleep(min(max(float(timeout), 0), 0.002))
        count = min(buffer.shape[-1], 4096, self.remaining if self.remaining is not None else 4096)
        metadata.reset()
        if not count:
            metadata.error_code = 'timeout'
            return 0
        buffer[..., :count] = np.exp(2j * np.pi * np.arange(count) / 8)
        metadata.time_spec = time_spec(time.monotonic())
        if self.remaining is not None:
            self.remaining -= count
        return count
    def send(self, buffer, metadata, timeout=0.1):
        if self.direction != 'tx' or self.closed or self.owner.closed:
            raise RuntimeError('synthetic TX stream is closed')
        return int(buffer.shape[-1])  # memory-only sink; there is no RF backend
    def recv_async_msg(self, metadata, timeout=0.01):
        time.sleep(min(max(float(timeout), 0), 0.005))
        return False
    def close(self):
        self.closed = True


class FakeUSRP:
    def __init__(self, selector):
        self.serial = serial(selector)
        self.closed, self.streams = False, []
        self.values = {f'{direction}_{name}': value for direction in ('rx', 'tx')
                       for name, value in [('rate', 1e6), ('freq', 100e6), ('gain', 0.0), ('bandwidth', 1e6)]}
    def __repr__(self):
        return f'DEV ONLY synthetic USRP {self.serial}; NO RADIO'
    def get_pp_string(self):
        return repr(self)
    def get_mboard_name(self, mboard=0):
        return 'DEV SYNTHETIC NO HARDWARE'
    def get_rx_num_channels(self):
        return 1
    get_tx_num_channels = get_rx_num_channels
    def get_num_mboards(self):
        return 1
    def get_time_now(self, mboard=0):
        return time_spec(time.monotonic())
    def get_usrp_rx_info(self, chan=0):
        return {'mboard_serial': self.serial, 'mboard_name': 'DEV SYNTHETIC',
                'mboard_id': 'B200', 'rx_subdev_name': 'DEV', 'tx_subdev_name': 'DEV'}
    get_usrp_tx_info = get_usrp_rx_info
    def get_rx_stream(self, stream_args):
        child = FakeStreamer(self, 'rx', stream_args)
        self.streams.append(child)
        return child
    def get_tx_stream(self, stream_args):
        child = FakeStreamer(self, 'tx', stream_args)
        self.streams.append(child)
        return child
    def __getattr__(self, method):
        prefix, _, name = method.partition('_')
        if name not in self.values or prefix not in {'get', 'set'}:
            raise AttributeError(method)
        if prefix == 'get':
            return lambda chan=0, **kwargs: self.values[name]
        def setter(value=None, chan=0, **kwargs):
            if value is None:
                value = next((kwargs[k] for k in ('rate', 'freq', 'gain', 'bandwidth', 'tune_request') if k in kwargs), None)
            if isinstance(value, tune_request):
                value = value.target_freq
            number = float(value)
            if not math.isfinite(number):
                raise ValueError('nonfinite synthetic setting')
            self.values[name] = number
            return tune_result(number) if name.endswith('_freq') else None
        return setter
    def close(self):
        for stream in self.streams:
            stream.close()
        self.closed = True


def install_backend():
    """Guard before installing any synthetic native state in this process."""
    from .server import guard
    guard()
    if 'uhd' in sys.modules or any(name.startswith('uhd.') for name in sys.modules):
        raise RuntimeError('refusing to replace an already imported UHD module')
    fake = ModuleType('uhd')
    fake.__dev_fixture_no_hardware__ = True
    def never_open(*args, **kwargs):
        raise RuntimeError('all generic native UHD construction is forbidden in this fixture')
    fake.usrp = SimpleNamespace(MultiUSRP=never_open, StreamArgs=stream_args, SubdevSpec=lambda value: value)
    fake.types = SimpleNamespace(TimeSpec=time_spec, RXMetadata=rx_metadata, TXMetadata=tx_metadata,
        TXAsyncMetadata=async_metadata, AsyncMetadata=async_metadata, StreamCMD=stream_cmd,
        StreamMode=SimpleNamespace(start_cont='start_cont', stop_cont='stop_cont', num_done='num_done', num_more='num_more'),
        TuneRequest=tune_request, DeviceAddr=lambda value: value)
    fake.libpyuhd = SimpleNamespace(types=SimpleNamespace(tune_request=tune_request))
    fake.get_version_string = lambda: UHD_VERSION
    sys.modules['uhd'] = fake
    from remoterf_device_core.drivers.usrp import usrp_schema
    usrp_schema.uhd = fake
    UsrpSchema = usrp_schema.UsrpSchema
    def open_native(self):
        init = self._inventory
        if set(init) - {'serial', 'type', 'profile'}:
            raise RuntimeError('ambiguous selector is forbidden in dev fixture')
        return FakeUSRP(init.get('serial'))
    def metadata(self):
        if self.device is None:
            raise RuntimeError('synthetic native device is closed')
        return {'remote_device_type': 'usrp', 'hardware_profile': 'usrp2901',
            'native_serial': self.device.serial, 'native_name': 'DEV SYNTHETIC NO HARDWARE',
            'uhd_version': UHD_VERSION, 'uhd_abi': 'synthetic', 'rx_channel_count': 1, 'tx_channel_count': 1,
            'supported_cpu_formats': ['fc32', 'fc64'], 'supported_otw_formats': ['sc16'],
            'supported_streaming_protocol_versions': ['1.0'], 'maximum_remote_payload_bytes': 4 * 1024 * 1024}
    def patch_schema(cls):
        cls.open_native_device = open_native
        cls.prepare_for_holder = lambda self: serial(self._inventory.get('serial'))
        cls.build_session_metadata_v2 = metadata
    patch_schema(UsrpSchema)
    # The ordinary startup loader seeds a stock driver into the user's drivers
    # directory, then imports that as a distinct class. Reapply only our native
    # test hooks after the normal loader has selected the final registry class.
    # The schema, registry decisions and native-constructor refusal stay intact.
    from remoterf_device_core.idl import schema as registry
    def wrap_loader(original):
        def load(*args, **kwargs):
            result = original(*args, **kwargs)
            cls = registry.get_driver_class('usrp')
            if cls is not None:
                patch_schema(cls)
            return result
        return load
    registry.load_builtin_drivers = wrap_loader(registry.load_builtin_drivers)
    registry.load_drivers = wrap_loader(registry.load_drivers)
    return UsrpSchema
