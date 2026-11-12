"""Control envelopes must be checked before the existing native value decoder."""
import json

import pytest

from remoterf_device_core.grpc import grpc_pb2
from remoterf_device_core.integrations.payload import IntegrationError, RESERVED_KEYS
from remoterf_device_core.integrations.wire import (
    INTEGRATION_METHODS, MAX_INVOKE_BYTES, decode_request,
    validate_arguments, validate_result,
)


def invoke(raw='{}', *, name='request', method='integration_start', overload='request'):
    return grpc_pb2.InvokeRequest(session_id='session', handle='root', method=method,
        overload_id=overload, args=[grpc_pb2.NamedValue(name=name,
        value=grpc_pb2.DynamicValue(json_value=raw))])


@pytest.mark.parametrize('method', INTEGRATION_METHODS)
@pytest.mark.parametrize('positional', [False, True])
def test_generic_request_round_trips_without_native_conversion(method, positional):
    payload = {'integration_id': 'org.example.app', 'configuration': {'count': 3, 'values': [True, None, 2.5]}}
    request = invoke(json.dumps([payload] if positional else payload),
                     name='__args__' if positional else 'request', method=method)
    assert decode_request(request) == payload


@pytest.mark.parametrize('key', sorted(RESERVED_KEYS))
def test_native_and_codec_tags_are_rejected_at_any_depth(key):
    request = invoke(json.dumps({'configuration': {'nested': [{key: 'TimeSpec'}]}}))
    with pytest.raises(IntegrationError) as caught:
        decode_request(request)
    assert caught.value.code == 'invalid_payload'


@pytest.mark.parametrize('raw', [
    '{"x":1,"x":2}', '{"x":{"y":1,"y":2}}', '{broken',
    '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}',
    '{"x":1e999}', '{"x":9223372036854775808}',
    '{"x":"\\ud800"}', '[]', 'null', '"text"',
    '[' * 2000 + '0' + ']' * 2000,
])
def test_invalid_json_or_non_plain_values_fail_closed(raw):
    with pytest.raises(IntegrationError):
        decode_request(invoke(raw))


@pytest.mark.parametrize('value', [
    grpc_pb2.DynamicValue(bytes_value=b'{}'), grpc_pb2.DynamicValue(string_value='{}'),
    grpc_pb2.DynamicValue(null_value=True), grpc_pb2.DynamicValue(),
    grpc_pb2.DynamicValue(ndarray_value=grpc_pb2.NDArrayValue(dtype='f4', shape=[1], data=b'\0'*4)),
])
def test_non_json_wire_variants_are_never_decoded(value):
    request = invoke()
    request.args[0].value.CopyFrom(value)
    with pytest.raises(IntegrationError, match='plain JSON'):
        decode_request(request)


@pytest.mark.parametrize('name,raw', [
    ('__kwargs__', '{"request":{}}'), ('__args__', '[{},{}]'),
    ('__args__', '{}'), ('__args__', '[]'), ('other', '{}'),
])
def test_only_one_named_or_positional_request_is_accepted(name, raw):
    with pytest.raises(IntegrationError):
        decode_request(invoke(raw, name=name))


def test_duplicate_arguments_unknown_method_and_overload_are_rejected():
    duplicate = invoke()
    duplicate.args.add(name='request', value=grpc_pb2.DynamicValue(json_value='{}'))
    for request in [duplicate, invoke(method='get_time_now'), invoke(overload='arbitrary')]:
        with pytest.raises(IntegrationError):
            decode_request(request)


def test_encoded_envelope_and_full_protobuf_frame_have_independent_limits():
    for request in [invoke(' ' * 65536 + '{}'), invoke(json.dumps({'s': '\u2603' * 22000}))]:
        with pytest.raises(IntegrationError) as caught:
            decode_request(request)
        assert caught.value.code == 'payload_limit'
    request = invoke()
    request.session_id = 'x' * MAX_INVOKE_BYTES
    with pytest.raises(IntegrationError) as caught:
        decode_request(request)
    assert caught.value.code == 'payload_limit'


def test_internal_dispatch_and_results_use_the_same_plain_data_contract():
    value = {'count': 1}
    result = validate_arguments({'request': value})
    result['count'] = 2
    assert value == {'count': 1}
    assert validate_arguments({}, [value]) == value
    for args, positional in [({'request': value}, [value]), ({}, []), ({'__kwargs__': value}, []), ({}, (value,))]:
        with pytest.raises(IntegrationError):
            validate_arguments(args, positional)
    for result in [{'value': b'bytes'}, {'__uhd_type__': 'TimeSpec'}, {'v': 'x'*65536}]:
        with pytest.raises(IntegrationError):
            validate_result(result)
