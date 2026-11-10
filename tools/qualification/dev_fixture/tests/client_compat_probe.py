"""Offline published-client probe. Run using the selected installed client only."""
from contextlib import redirect_stdout
import importlib.metadata
import io
import json
from pathlib import Path
import sys
import tempfile

import remoterf
from remoterf.core.v2_errors import RemoteRFProtocolError
from remoterf.drivers import dynamic_device
from remoterf.drivers.dynamic_v2 import build_uhd_bindings, render_stub, validate_schema_v2

source = Path(sys.argv[1])
output = Path(sys.argv[2])
schemas = {path.stem: json.loads(path.read_text()) for path in source.glob('*.json')}
methods = ('integration_list', 'integration_describe', 'integration_start',
           'integration_status', 'integration_stop', 'integration_call')


class Transport:
    def __init__(self, schema, stale=False):
        self.schema, self.stale, self.calls = schema, stale, []
    def negotiate(self):
        pass
    def open_session(self, token, schema_hash):
        if self.stale:
            raise RemoteRFProtocolError('schema changed', details={'current': self.schema['schema_hash']})
        assert schema_hash == self.schema['schema_hash']
        return {'session_id': 'offline', 'device_handle': 'root', 'schema': self.schema, 'capabilities': {}}
    def invoke(self, session_id, handle, method, bound, *, overload_id):
        self.calls.append((method, bound, overload_id))
        return (1e6 if method == 'get_rx_rate' else {'enabled': False}), {}
    def close_session(self, session_id):
        return True


results = {'client_version': importlib.metadata.version('remoterf'),
           'client_path': remoterf.__file__, 'scope': 'OFFLINE_NON_RADIO_NO_NETWORK', 'checks': {}}
for name in ('usrp', 'usrp_phaser'):
    schema = schemas[f'{name}-v2']
    validate_schema_v2(schema)
    transport = Transport(schema)
    _, cls = build_uhd_bindings(schema, transport_factory=lambda: transport)
    with cls('offline-unit-token') as device:
        assert device.get_rx_rate(0) == 1e6
        for method in methods:
            assert callable(getattr(cls, method))
            request = {'configuration': {'sample': 1}}
            assert getattr(device, method)(request) == {'enabled': False}
            assert getattr(device, method)(request=request) == {'enabled': False}
            assert transport.calls[-2:] == [(method, {'request': request}, 'request')] * 2
    compile(render_stub(schema), '<published-client-stub>', 'exec')
    results['checks'][f'{name}_v2_binder_and_six_plain_dict_methods'] = True

with tempfile.TemporaryDirectory(prefix='rrf-client-offline-') as temporary:
    dynamic_device._DRIVERS_DIR = Path(temporary)
    for name in ('usrp', 'usrp_phaser'):
        for version in ('v1', 'v2'):
            schema = schemas[f'{name}-{version}']
            directory = dynamic_device._write_driver_files(schema, token='offline-unit-token')
            module = directory / f'{name}_remote.py'
            assert dynamic_device._read_schema_hash(module) == schema['schema_hash']
            compile(module.read_text(), str(module), 'exec')
            results['checks'][f'{name}_{version}_generated_source_compiles'] = True
    old, new = schemas['usrp-old-v2'], schemas['usrp-v2']
    assert old['schema_hash'] != new['schema_hash']
    dynamic_device._write_driver_files(old, token='offline-unit-token')
    fetched = []
    def fetch(**kwargs):
        fetched.append(kwargs['prefer_v2'])
        return new if kwargs['prefer_v2'] else schemas['usrp-v1']
    dynamic_device.fetch_idl = fetch
    transport = Transport(new, stale=True)
    _, old_class = build_uhd_bindings(old, transport_factory=lambda: transport)
    with redirect_stdout(io.StringIO()):
        try:
            old_class('offline-unit-token')
        except RemoteRFProtocolError as exc:
            assert 'reimport' in str(exc).lower()
            assert exc.details['current'] == new['schema_hash']
        else:
            raise AssertionError('stale schema did not require reimport')
    module = Path(temporary) / 'usrp' / 'usrp_remote.py'
    assert fetched == [False, True]
    assert dynamic_device._read_schema_hash(module) == new['schema_hash']
    modified = module.stat().st_mtime_ns
    assert dynamic_device.install_driver_if_stale(token='offline-unit-token', current_hash=new['schema_hash']) is False
    assert module.stat().st_mtime_ns == modified
    results['checks']['old_cached_schema_replaced_then_requires_reimport'] = True
    results['checks']['current_schema_cache_no_rewrite'] = True

results['hashes'] = {name: schema['schema_hash'] for name, schema in schemas.items()}
output.write_text(json.dumps(results, sort_keys=True, indent=2) + '\n')
print(json.dumps(results, sort_keys=True))
