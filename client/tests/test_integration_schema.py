"""The generic surface uses the existing UHD root binder and plain dict values."""
from pathlib import Path

import pytest

from remoterf.drivers.dynamic_v2 import build_uhd_bindings, render_stub, validate_schema_v2
from remoterf.drivers.usrp import usrp_remote


METHODS = (
    'integration_list', 'integration_describe', 'integration_start',
    'integration_status', 'integration_stop', 'integration_call',
)


class Transport:
    def __init__(self):
        self.calls = []
    def negotiate(self):
        pass
    def open_session(self, token, schema_hash):
        assert schema_hash == usrp_remote._SCHEMA['schema_hash']
        return dict(session_id='session', device_handle='root', schema=usrp_remote._SCHEMA,
                    capabilities={'application_integrations': {'enabled': False}})
    def invoke(self, session_id, handle, method, bound, *, overload_id):
        self.calls.append((session_id, handle, method, bound, overload_id))
        return {'enabled': False}, {}
    def close_session(self, session_id):
        return True


@pytest.mark.parametrize('method', METHODS)
def test_generic_method_binds_positional_and_named_dict_on_existing_root(method):
    transport = Transport()
    _, MultiUSRP = build_uhd_bindings(usrp_remote._SCHEMA, transport_factory=lambda: transport)
    with_device = MultiUSRP('token')
    try:
        request = {'id': 'org.example.adapter', 'configuration': {'value': 2}}
        assert getattr(with_device, method)(request) == {'enabled': False}
        assert getattr(with_device, method)(request=request) == {'enabled': False}
        assert transport.calls == [('session', 'root', method, {'request': request}, 'request')] * 2
        with pytest.raises(TypeError):
            getattr(with_device, method)('not an object')
        assert len(transport.calls) == 2
    finally:
        with_device.close()


def test_generic_methods_do_not_require_uhd_native_members_or_change_protocol_kinds():
    validate_schema_v2(usrp_remote._SCHEMA)
    methods = {row['name']: row for row in usrp_remote._SCHEMA['methods']
               if row['owner'] == 'uhd.usrp.MultiUSRP'}
    for name in METHODS:
        descriptor = methods[name]
        assert descriptor['execution'] == 'server_control'
        assert 'runtime_member' not in descriptor.get('availability', {})
        assert descriptor['overloads'][0]['returns'] == 'dict'
    assert usrp_remote._SCHEMA['schema_version'] == '2.0'
    assert 'set_rx_freq' in methods and 'get_rx_stream' in methods


def test_generated_stub_has_typed_generic_signatures_and_is_current():
    stub = render_stub(usrp_remote._SCHEMA)
    assert Path(usrp_remote.__file__).with_suffix('.pyi').read_text() == stub
    for name in METHODS:
        assert f'def {name}(self, request: dict[str, Any]) -> dict[str, Any]: ...' in stub
    compile(stub, '<generated stub>', 'exec')
