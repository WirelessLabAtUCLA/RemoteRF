"""No-radio unit coverage; successful fixtures never count as hardware approval."""
import json
from pathlib import Path
from types import SimpleNamespace
from threading import Event
import subprocess
import sys

import numpy as np
import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SOURCE))
from rrf_dev_integration_fixture import Adapter
from rrf_dev_integration_fixture import backend, server


@pytest.fixture
def guarded(tmp_path, monkeypatch):
    root = tmp_path / 'canary'
    config = root / 'config' / 'remoterf'
    config.mkdir(parents=True)
    inventory = {'devices': [{'device_id': 99, 'device_type': 'sim_sdr', 'init': {}},
        *[{'device_id': gid, 'device_type': 'usrp', 'init': {'serial': serial, 'profile': 'usrp2901'}}
          for gid, serial in server.EXPECTED_SERIALS.items()]]}
    (config / 'devices.yml').write_text(json.dumps(inventory))
    monkeypatch.setattr(server, 'CANARY_ROOT', root)
    monkeypatch.setattr(server.socket, 'gethostname', lambda: 'remoterf-global-dev')
    monkeypatch.setattr(server.pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_name='testlab'))
    monkeypatch.setenv('REMOTERF_DEV_INTEGRATION_FIXTURE', '1')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config.parent))
    monkeypatch.setenv('REMOTERF_CONFIG_DIR', str(config))
    monkeypatch.delenv('REMOTERF_DEV_INTEGRATION_ENABLE', raising=False)
    monkeypatch.delenv('REMOTERF_DEV_INTEGRATION_BROKEN', raising=False)
    return root, config, inventory


def test_guard_is_disabled_by_default_before_backend_import(monkeypatch):
    monkeypatch.delenv('REMOTERF_DEV_INTEGRATION_FIXTURE', raising=False)
    before = sys.modules.get('uhd')
    with pytest.raises(RuntimeError):
        backend.install_backend()
    assert sys.modules.get('uhd') is before


def test_guard_accepts_only_explicit_clone(guarded):
    root, config, _ = guarded
    assert server.guard() == (root, config)


@pytest.mark.parametrize('change', ['hostname', 'user', 'flag', 'config', 'xdg', 'physical_serial',
                                   'ambiguous', 'other_driver', 'missing_radio', 'duplicate', 'enable', 'broken_enabled'])
def test_guard_rejects_unsafe_or_ambiguous_state(guarded, monkeypatch, change):
    root, config, inventory = guarded
    if change == 'hostname':
        monkeypatch.setattr(server.socket, 'gethostname', lambda: 'production')
    elif change == 'user':
        monkeypatch.setattr(server.pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_name='root'))
    elif change == 'flag':
        monkeypatch.setenv('REMOTERF_DEV_INTEGRATION_FIXTURE', '0')
    elif change in {'config', 'xdg'}:
        monkeypatch.setenv('REMOTERF_CONFIG_DIR' if change == 'config' else 'XDG_CONFIG_HOME', str(root.parent))
    elif change == 'enable':
        monkeypatch.setenv('REMOTERF_DEV_INTEGRATION_ENABLE', 'true')
    elif change == 'broken_enabled':
        monkeypatch.setenv('REMOTERF_DEV_INTEGRATION_ENABLE', '1')
        monkeypatch.setenv('REMOTERF_DEV_INTEGRATION_BROKEN', '1')
    else:
        if change == 'physical_serial':
            inventory['devices'][1]['init']['serial'] = '32C78EE'
        elif change == 'ambiguous':
            inventory['devices'][1]['init']['addr'] = '10.0.0.1'
        elif change == 'other_driver':
            inventory['devices'][0]['device_type'] = 'usrp_phaser'
        elif change == 'missing_radio':
            inventory['devices'].pop()
        else:
            inventory['devices'].append(inventory['devices'][0])
        (config / 'devices.yml').write_text(json.dumps(inventory))
    with pytest.raises(RuntimeError):
        server.guard()


def test_synthetic_native_helpers_streams_and_close():
    radio = backend.FakeUSRP('DEV_TEST_01')
    assert radio.get_usrp_rx_info()['mboard_serial'] == 'DEV_TEST_01'
    radio.set_rx_rate(rate=2e6, chan=0)
    assert radio.get_rx_rate() == 2e6
    tuned = radio.set_rx_freq(tune_request=backend.tune_request(100e6), chan=0)
    assert tuned.actual_rf_freq == radio.get_rx_freq() == 100e6
    stream = radio.get_rx_stream(backend.stream_args())
    command = backend.stream_cmd('num_done')
    command.num_samps = 4096
    stream.issue_stream_cmd(command)
    buffer = np.zeros(4096, dtype=np.complex64)
    metadata = backend.rx_metadata()
    assert stream.recv(buffer, metadata) == 4096
    assert np.isfinite(buffer).all() and np.any(buffer)
    assert np.argmax(np.abs(np.fft.fft(buffer))) == 512
    assert metadata.time_spec.get_real_secs() > 0
    assert stream.recv(buffer, metadata) == 0 and metadata.error_code == 'timeout'
    tx = radio.get_tx_stream(backend.stream_args())
    assert tx.send(buffer, backend.tx_metadata()) == 4096
    radio.close()
    assert stream.closed and tx.closed and radio.closed
    with pytest.raises(RuntimeError):
        stream.recv(buffer, metadata)
    with pytest.raises(RuntimeError):
        backend.FakeUSRP('ACTUAL_SERIAL')


def test_backend_preserves_manifest_and_normal_native_codec_in_isolated_process(guarded):
    root, config, _ = guarded
    code = '''
import sys
from pathlib import Path
from rrf_dev_integration_fixture import server, backend
server.guard = lambda: (Path(sys.argv[1]), Path(sys.argv[2]))
assert 'uhd' not in sys.modules
cls = backend.install_backend()
from remoterf_device_core.drivers.usrp.usrp_schema import _construct
from remoterf_device_core.drivers.usrp.uhd_manifest import build_usrp_schema_v2
from remoteRF_server.server.dynamic_v2 import snapshot_native
inventory = cls()
inventory.bind_inventory({'serial': 'DEV_TEST_01', 'profile':'usrp2901'})
live = inventory.create_session_schema()
assert live.get_idl_v2()['schema_hash'] == build_usrp_schema_v2()['schema_hash']
assert live.build_session_metadata_v2()['uhd_version'] == 'dev-fixture-no-hardware'
assert snapshot_native(live.invoke_native('get_time_now', {}))['__uhd_type__'] == 'TimeSpec'
assert _construct({'__uhd_type__':'StreamArgs','cpu_format':'fc32','otw_format':'sc16'}).cpu_format == 'fc32'
assert _construct({'__uhd_type__':'RXMetadata'}).error_code == 'none'
assert _construct({'__uhd_type__':'TXMetadata'}).has_time_spec is False
assert _construct({'__uhd_type__':'StreamCMD','mode':'num_done','num_samps':4096}).num_samps == 4096
live.close_native_device()
assert live.device is None
assert sys.modules['uhd'].__dev_fixture_no_hardware__
'''
    result = subprocess.run([sys.executable, '-c', code, str(root), str(config)],
        env={**__import__('os').environ, 'PYTHONPATH': str(SOURCE)}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_generated_client_raw_control_rx_tx_and_disabled_list_cross_real_loopback(guarded):
    root, config, _ = guarded
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('loopback_probe.py')), str(root), str(config)],
        env={**__import__('os').environ, 'PYTHONPATH': str(SOURCE)}, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


class Processes:
    def __init__(self):
        self.alive = False
        self.launches = []
    def launch(self, argv, **kwargs):
        self.launches.append((argv, kwargs))
        self.alive = True
        return 'owned-child'
    def snapshot(self):
        return {'processes': [{'id': 'owned-child', 'returncode': None if self.alive else 0}]}
    def log_tail(self, *args):
        return b'NON_RADIO_TEST_FIXTURE_READY\n'
    def stop(self, **kwargs):
        self.alive = False
        return True


@pytest.fixture
def adapter(guarded):
    root, config, _ = guarded
    context = SimpleNamespace(selector={'serial': 'DEV_INTEGRATION_01'}, work_dir=root / 'work',
                              processes=Processes(), cancelled=Event())
    context.work_dir.mkdir()
    return Adapter(context)


def test_adapter_lifecycle_actions_and_explicit_environment(adapter):
    adapter.start('test', {})
    assert adapter.status()['ready'] and adapter.call('health', {})['ready']
    assert adapter.call('echo', {'number': 2})['echo'] == {'number': 2}
    assert adapter.call('echo', {})['calls'] == 2
    argv, options = adapter.context.processes.launches[0]
    assert Path(argv[0]).is_absolute() and argv[-2] == 'rrf_dev_integration_fixture.worker'
    assert set(options['env']) == {'LANG', 'LC_ALL'}
    assert options['required_while_running'] is True
    adapter.stop()
    adapter.stop()
    assert adapter.status()['stopped'] and not adapter.context.processes.alive


@pytest.mark.parametrize('value', [{'command': '/bin/sh'}, {'start_delay': 11}, {'stop_delay': 36},
                                  {'exit_after': float('inf')}, {'fail_start': 1}, {'fail_stop': 'true'}])
def test_adapter_rejects_command_and_unbounded_failure_controls(adapter, value):
    with pytest.raises(ValueError):
        adapter.validate('test', value)


def test_partial_start_failure_and_child_exit_still_support_cleanup(adapter):
    with pytest.raises(RuntimeError, match='startup failure'):
        adapter.start('test', {'fail_start': True})
    assert adapter.context.processes.alive
    adapter.stop()
    assert not adapter.context.processes.alive


def test_cancel_before_start_never_launches(adapter):
    adapter.context.cancelled.set()
    with pytest.raises(RuntimeError, match='cancelled'):
        adapter.start('test', {'start_delay': 1})
    assert not adapter.context.processes.launches


def test_unexpected_child_exit_is_reported(adapter):
    adapter.start('test', {})
    adapter.context.processes.alive = False
    with pytest.raises(RuntimeError, match='exited'):
        adapter.status()


def test_guarded_configuration_is_disabled_by_default(guarded):
    root, config, _ = guarded
    server.configure_fixture(config)
    declaration = json.loads((config / 'integrations.json').read_text())
    assert declaration['enabled'] is False and 'qualifications' not in declaration
    assert set(declaration['devices']) == {'401'}


def test_enabled_fixture_enables_only_the_dev_serial(guarded, monkeypatch):
    from remoterf_device_core.integrations.deployment import Configuration
    from remoterf_device_core.integrations import IntegrationError
    root, config, _ = guarded
    monkeypatch.setenv('REMOTERF_DEV_INTEGRATION_ENABLE', '1')
    server.configure_fixture(config, '/sys/fs/cgroup/isolated-test/apps')
    configuration = Configuration.load(config / 'integrations.json')
    configuration.require_enabled(401)
    assert configuration.registry.discover(device_id='401', device_type='usrp')[0]['enabled'] is True
    with pytest.raises(IntegrationError):
        configuration.require_enabled(402)
