"""Generic integration requests keep the same protections on every wire route.

All hardware/controller resources here are fake. gRPC and the QUIC loopback use
real local sockets; bridge/direct-dispatch cases do not claim a live Global test.
"""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import grpc
import pytest

from test_native_lifetime import opened
from remoteRF_server.common.grpc import grpc_pb2, grpc_pb2_grpc
from remoteRF_server.common.grpc.v2_codec import decode_value
from remoteRF_server.common.utils import map_arg, unmap_arg
from remoteRF_server.server import dynamic_v2 as v2
from remoteRF_server.server import dynamic_v2_bridge as bridge
from remoteRF_server.server.rpc_manager import RpcManager
from remoterf_device_core.integrations.ownership import Gate, Journal


def request_for(session, method='integration_list', payload=None, *, raw=None):
    return grpc_pb2.InvokeRequest(session_id=session.session_id, handle=session.root_handle,
        method=method, overload_id='request', args=[grpc_pb2.NamedValue(name='request',
        value=grpc_pb2.DynamicValue(json_value=json.dumps(payload or {}) if raw is None else raw))])


@pytest.fixture
def routed(opened, monkeypatch):
    manager, session, inventory = opened
    controller = Mock()
    controller.dispatch.return_value = {'enabled': False, 'integrations': []}
    monkeypatch.setattr(manager, 'integrations', controller, raising=False)
    monkeypatch.setattr(v2, 'session_manager', manager)
    monkeypatch.setattr(bridge, 'session_manager', manager)
    monkeypatch.setattr(v2.host_session_proxy, 'is_remote_session', lambda session_id: False)
    yield manager, session, controller


def send(request, *, route, gid):
    if route == 'local':
        return v2.DynamicControlV2Servicer().Invoke(request, None)
    args = {'wire': map_arg(request.SerializeToString())}
    if route == 'bridge':
        result = bridge.run_bridge(operation='Invoke', args=args, gid=gid)
    elif route == 'device_rpc':
        result = RpcManager().run_device_rpc_for_gid(gid=gid,
            function_name='DynamicV2:Invoke:WIRE', args=args)
    elif route == 'direct_dispatch':
        from remoteRF_server.server import direct_path
        result = direct_path._dispatch(grpc_pb2.GenericRPCRequest(
            function_name='DynamicV2:Invoke:WIRE', args=args), '127.0.0.1', bound={
                'authorize': lambda name: None,
                'run': lambda name, arguments: RpcManager().run_device_rpc_for_gid(
                    gid=gid, function_name=name, args=arguments),
            })
    else:
        raise AssertionError(route)
    return grpc_pb2.InvokeResponse.FromString(unmap_arg(result['wire']))


@pytest.mark.parametrize('route', ['local', 'bridge', 'device_rpc', 'direct_dispatch'])
def test_generic_routes_never_use_native_decode_or_dispatch(routed, monkeypatch, route):
    manager, session, controller = routed
    monkeypatch.setattr(v2, 'decode_value', Mock(side_effect=AssertionError('native decoder used')))
    monkeypatch.setattr(manager, '_resolve_handle_argument', Mock(side_effect=AssertionError('native helper used')))
    payload = {'cursor': 'after-id', 'limit': 3}
    response = send(request_for(session, payload=payload), route=route, gid=session.gid)
    assert not response.error.code, response.error
    assert decode_value(response.result) == {'enabled': False, 'integrations': []}
    controller.dispatch.assert_called_once_with(manager, session, 'integration_list', payload)
    session.schema.invoke_native.assert_not_called()


@pytest.mark.parametrize('route', ['local', 'bridge', 'device_rpc', 'direct_dispatch'])
@pytest.mark.parametrize('raw', ['{"configuration":{"__uhd_type__":"TimeSpec"}}',
                                  '{"x":1,"x":2}', '{"x":NaN}', '{"x":"' + 'a'*131072 + '"}'])
def test_malformed_application_payload_is_rejected_before_any_native_decoder(routed, monkeypatch, route, raw):
    manager, session, controller = routed
    native_decode = Mock(side_effect=AssertionError('native decoder must not run'))
    monkeypatch.setattr(v2, 'decode_value', native_decode)
    response = send(request_for(session, 'integration_start', raw=raw), route=route, gid=session.gid)
    assert response.error.code == 'RemoteRFProtocolError'
    assert json.loads(response.error.details_json)['integration_code'] in {'invalid_payload', 'payload_limit'}
    native_decode.assert_not_called()
    controller.dispatch.assert_not_called()
    session.schema.invoke_native.assert_not_called()


@pytest.mark.parametrize('method', ['integration_status', 'integration_stop'])
def test_status_and_stop_remain_available_while_native_gate_is_owned(routed, tmp_path, monkeypatch, method):
    manager, session, controller = routed
    journal = Journal(tmp_path / 'owners')
    gate = Gate('fake:route-owner', journal)
    monkeypatch.setitem(v2.hardware_access._gates, session.gid, gate)
    lease = gate.claim(session=session.session_id, generation=1, integration='org.example.fake')
    try:
        response = send(request_for(session, method, {'operation_id': 'op'}), route='local', gid=session.gid)
        assert not response.error.code, response.error
        assert controller.dispatch.call_count == 1
        assert gate.snapshot()['blocked']
    finally:
        gate.release(lease, verify=lambda: True)  # no resource was created by this fake owner
        journal.close()


def test_generic_calls_require_the_live_root_and_reservation(routed, monkeypatch):
    manager, session, controller = routed
    child = manager.register_handle(session, Mock(), 'test.Child', parent=session.root_handle)
    request = request_for(session)
    request.handle = child.handle
    assert send(request, route='local', gid=session.gid).error.code
    controller.dispatch.assert_not_called()
    request.handle = session.root_handle
    monkeypatch.setattr(v2, 'token_matches_device', lambda *args: False)
    assert send(request, route='local', gid=session.gid).error.code
    controller.dispatch.assert_not_called()


def test_federation_does_not_authorize_an_operation_id_from_another_device(routed):
    manager, session, controller = routed
    response = send(request_for(session, 'integration_status', {'operation_id': 'guessed'}),
                    route='bridge', gid=session.gid + 1)
    assert response.error.code
    assert 'does not belong to this device' in response.error.message
    controller.dispatch.assert_not_called()


@pytest.mark.parametrize('result', [{'__uhd_type__': 'TimeSpec'}, {'data': b'bytes'}, {'data': 'x'*65536}])
def test_invalid_controller_results_are_not_native_snapshots(routed, result):
    manager, session, controller = routed
    controller.dispatch.return_value = result
    response = send(request_for(session), route='local', gid=session.gid)
    assert response.error.code == 'RemoteRFProtocolError'
    assert json.loads(response.error.details_json)['integration_code'] in {'invalid_payload', 'payload_limit'}


def test_generic_list_round_trips_real_local_grpc_without_enabling_apps(routed):
    manager, session, controller = routed
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    grpc_pb2_grpc.add_DynamicControlV2Servicer_to_server(v2.DynamicControlV2Servicer(), server)
    port = server.add_insecure_port('127.0.0.1:0')
    assert port
    server.start()
    try:
        with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
            response = grpc_pb2_grpc.DynamicControlV2Stub(channel).Invoke(request_for(session), timeout=3)
            assert not response.error.code, response.error
            assert decode_value(response.result)['enabled'] is False
    finally:
        server.stop(0).wait(timeout=3)


def test_generic_payload_round_trips_real_loopback_quic(routed, monkeypatch):
    # Reuse the established transport fixture, but send the new Invoke payload
    # all the way through the ordinary owner-side device RPC bridge.
    import test_gate_i_direct_path as direct_tests
    from remoteRF_server.server import rpc_manager as rpc_module
    manager, session, controller = routed
    case = direct_tests.LoopbackPathTests()
    case.setUp()
    monkeypatch.setenv('REMOTERF_STUN_SERVER', 'off')
    monkeypatch.setattr(rpc_module.rpc_manager, 'run_rpc',
        lambda *, function_name, args: RpcManager().run_device_rpc_for_gid(
            gid=session.gid, function_name=function_name, args=args))
    async def exercise():
        ice, protocol, pump = await case._client(offer_id='integration', session='integration-test')
        try:
            response = await case._call(protocol, 'DynamicV2:Invoke:WIRE',
                                       {'wire': map_arg(request_for(session).SerializeToString())})
            invoked = grpc_pb2.InvokeResponse.FromString(unmap_arg(response.results['wire']))
            assert not invoked.error.code, invoked.error
            assert decode_value(invoked.result)['enabled'] is False
        finally:
            pump.cancel()
            protocol.close()
            await ice.close()
    try:
        asyncio.run(exercise())
    finally:
        case.doCleanups()


def test_host_forwarding_preserves_generic_unsupported_error(routed, monkeypatch):
    from remoteRF_server.server import host_dynamic_v2 as host_route
    from remoteRF_server.server.v2_errors import RemoteRFProtocolError, error_envelope
    manager, session, controller = routed
    proxy = host_route.HostDynamicV2Proxy()
    proxy._sessions['host-session'] = host_route.HostSessionRoute('host-session', 42, 'fake-host', '42')
    monkeypatch.setattr(v2, 'host_session_proxy', proxy)
    monkeypatch.setattr(host_route.hts, 'get_tunnel_registry', lambda: object())
    received = []
    def forward(registry, *, host_id, device_id, function_name, args, timeout_sec):
        invoked = grpc_pb2.InvokeRequest.FromString(unmap_arg(args['wire']))
        received.append(invoked)
        assert (host_id, device_id, function_name) == ('fake-host', '42', 'DynamicV2:Invoke')
        response = grpc_pb2.InvokeResponse(error=error_envelope(RemoteRFProtocolError(
            'application integrations are unsupported on hosts', details={'integration_code': 'unsupported_host'})))
        return {'wire': map_arg(response.SerializeToString())}
    monkeypatch.setattr(host_route.hts, 'handle_host_device', forward)
    request = request_for(session, 'integration_start', {'request_key': 'retry'})
    request.session_id, request.handle = 'host-session', 'host-root'
    response = v2.DynamicControlV2Servicer().Invoke(request, None)
    assert response.error.code == 'RemoteRFProtocolError'
    assert json.loads(response.error.details_json)['integration_code'] == 'unsupported_host'
    assert len(received) == 1 and received[0] == request
    controller.dispatch.assert_not_called()


def test_real_client_default_controller_stays_disabled_and_raw_uhd_still_works(opened, monkeypatch):
    # The required loopback module sets up the current client source import.
    from test_usrp_client_server_localhost import DynamicV2Transport, build_uhd_bindings, client_grpc
    from remoterf.core.v2_errors import RemoteRFProtocolError
    manager, original, inventory = opened
    monkeypatch.setattr(v2, 'session_manager', manager)
    manager.close(original.session_id)
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    grpc_pb2_grpc.add_DynamicControlV2Servicer_to_server(v2.DynamicControlV2Servicer(), server)
    port = server.add_insecure_port('127.0.0.1:0')
    assert port
    server.start()
    try:
        with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
            transport = DynamicV2Transport(control_stub=client_grpc.DynamicControlV2Stub(channel),
                sample_stub=client_grpc.SampleDataV1Stub(channel))
            schema = transport.get_schema('token')
            _, MultiUSRP = build_uhd_bindings(schema, transport_factory=lambda: transport)
            with MultiUSRP('token') as device:
                assert device.get_time_now() == 1.5
                discovery = device.integration_list({})
                assert discovery['enabled'] is False and discovery['integrations'] == []
                assert discovery['reason'] == 'integration_disabled'
                with pytest.raises(RemoteRFProtocolError) as caught:
                    device.integration_start({'integration_id': 'org.example.fake', 'profile': 'test',
                        'configuration': {}, 'request_key': 'must-not-start', 'generation': discovery['generation']})
                assert caught.value.details['integration_code'] == 'integration_disabled'
                assert device.get_time_now() == 1.5
    finally:
        server.stop(0).wait(timeout=3)
