"""Host transports refuse integrations explicitly without touching native UHD."""
import json
from unittest.mock import Mock

import pytest

from test_native_lifetime import opened
from remoteRF_host.common.grpc import grpc_pb2
from remoteRF_host.common.utils import map_arg, unmap_arg
from remoteRF_host.server import dynamic_v2 as v2
from remoteRF_host.server import dynamic_v2_bridge as bridge
from remoterf_device_core.integrations.wire import INTEGRATION_METHODS


@pytest.fixture
def routed(opened, monkeypatch):
    manager, session, inventory = opened
    monkeypatch.setattr(v2, 'session_manager', manager)
    monkeypatch.setattr(bridge, 'session_manager', manager)
    return manager, session


def request_for(session, method, raw='{}'):
    return grpc_pb2.InvokeRequest(session_id=session.session_id, handle=session.root_handle,
        method=method, overload_id='request', args=[grpc_pb2.NamedValue(name='request',
        value=grpc_pb2.DynamicValue(json_value=raw))])


def send(request, *, route, gid):
    if route == 'local':
        return v2.DynamicControlV2Servicer().Invoke(request, None)
    results = bridge.run_bridge(operation='Invoke', gid=gid,
                               args={'wire': map_arg(request.SerializeToString())})
    return grpc_pb2.InvokeResponse.FromString(unmap_arg(results['wire']))


@pytest.mark.parametrize('method', INTEGRATION_METHODS)
@pytest.mark.parametrize('route', ['local', 'bridge'])
def test_host_refuses_every_generic_method_and_preserves_native_calls(routed, monkeypatch, route, method):
    manager, session = routed
    decoder = Mock(side_effect=AssertionError('native decoder must not run'))
    monkeypatch.setattr(v2, 'decode_value', decoder)
    response = send(request_for(session, method), route=route, gid=session.gid)
    assert response.error.code == 'RemoteRFProtocolError'
    assert json.loads(response.error.details_json)['integration_code'] == 'unsupported_host'
    decoder.assert_not_called()
    session.schema.invoke_native.assert_not_called()
    assert manager.invoke(session.session_id, session.root_handle, 'get_time_now', {})[0] == 1.5


@pytest.mark.parametrize('route', ['local', 'bridge'])
@pytest.mark.parametrize('raw', ['{"configuration":{"__uhd_type__":"TimeSpec"}}', '{"x":1,"x":2}'])
def test_host_rejects_unsafe_envelopes_before_native_decoding(routed, monkeypatch, route, raw):
    manager, session = routed
    decoder = Mock(side_effect=AssertionError('native decoder must not run'))
    monkeypatch.setattr(v2, 'decode_value', decoder)
    response = send(request_for(session, 'integration_start', raw), route=route, gid=session.gid)
    assert response.error.code == 'RemoteRFProtocolError'
    assert json.loads(response.error.details_json)['integration_code'] == 'invalid_payload'
    decoder.assert_not_called()
    session.schema.invoke_native.assert_not_called()


def test_host_bridge_rejects_another_device_session(routed):
    manager, session = routed
    response = send(request_for(session, 'integration_status'), route='bridge', gid=session.gid + 1)
    assert response.error.code == 'RemoteRFProtocolError'
    assert 'does not belong to this device' in response.error.message
    session.schema.invoke_native.assert_not_called()
