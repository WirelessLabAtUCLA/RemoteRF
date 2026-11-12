#!/usr/bin/env python3
"""Opt-in dev Global canary; synthetic radios only, no production destination.

Uses an independently registered/verified dev account whose saved client state
is private to this run. Normal Global reservation, schema approval and transport
authorization remain in force. No token or password is written into evidence.
"""
import argparse
from contextlib import contextmanager
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import time
from unittest.mock import patch

ORIGIN = 'https://dev.global.remoterf.net'
DEPLOYMENT = '9ad48dc2-aa41-4e05-82f5-e50f36dbdbdb'


def until(fn, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError('bounded canary condition did not complete')


def refused(fn, code=None, error_type=None):
    try:
        fn()
    except Exception as exc:
        if error_type is not None:
            assert isinstance(exc, error_type), type(exc).__name__
        actual = getattr(exc, 'details', {}).get('integration_code')
        if code is not None:
            assert actual == code, (type(exc).__name__, actual, code)
        return {'type': type(exc).__name__, 'integration_code': actual}
    raise AssertionError('an invalid operation was accepted')


@contextmanager
def owning_radio(factory, token, revoked):
    from remoterf.core.v2_errors import RemoteRFReservationError
    radio = factory(token)
    try:
        yield radio
    finally:
        try:
            radio.close()
        except RemoteRFReservationError:
            if not revoked[0]:
                raise


def native_rx(radio, uhd):
    import numpy as np
    radio.set_rx_rate(1e6, 0)
    radio.set_rx_freq(uhd.types.TuneRequest(100e6), 0)
    radio.set_rx_gain(0.0, 0)
    args = uhd.usrp.StreamArgs('fc32', 'sc16')
    args.channels = [0]
    stream = radio.get_rx_stream(args)
    try:
        command = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
        command.num_samps = 4096
        command.stream_now = True
        stream.issue_stream_cmd(command)
        samples = np.zeros((1, 4096), dtype=np.complex64)
        metadata = uhd.types.RXMetadata()
        count = stream.recv(samples, metadata, 1.0)
        assert count == 4096 and np.isfinite(samples).all() and np.any(samples)
        return {'received': count, 'rate': radio.get_rx_rate(0), 'finite': True}
    finally:
        stream.close()


def direct_ready(token, radio):
    from remoterf.core import direct_path
    os.environ['REMOTERF_DIRECT'] = '1'
    radio.get_time_now()
    return until(lambda: direct_path.global_path(token), seconds=20)


def baseline(backend, reserve, checks, direct=True):
    import numpy as np
    from remoterf.drivers.sim_sdr import SimSDR
    from remoterf.core import direct_path
    reservation = reserve(99)
    token = reservation['token']
    radio = SimSDR(token)
    def capture():
        radio.rx_buffer_size = 4096
        data = np.asarray(radio.rx())
        assert data.shape == (4096,) and np.isfinite(data).all()
        assert int(np.argmax(np.abs(np.fft.fft(data)))) == 512
    capture()
    checks['existing_sim_relay_rx'] = True
    if not direct:
        return
    os.environ['REMOTERF_DIRECT'] = '1'
    radio.ip()
    try:
        path = until(lambda: direct_path.global_path(token), seconds=20)
    except AssertionError:
        checks['direct_unavailable'] = direct_path.describe()
        raise
    calls = path.calls
    capture()
    assert path.calls - calls == 2 and path.describe().startswith('direct ')
    checks['existing_sim_direct_rx'] = {'calls': path.calls - calls, 'fixed': path.fixed}
    os.environ['REMOTERF_DIRECT'] = '0'
    direct_path.stop()


def generic(mode, backend, reserve, cancel, checks):
    from remoterf.core.dynamic_v2_transport import DynamicV2Transport
    from remoterf.drivers.dynamic_v2 import build_uhd_bindings
    from remoterf.core import direct_path
    from remoterf.core.v2_errors import RemoteRFNativeUHDError, RemoteRFReservationError
    reservation = reserve(401)
    token = reservation['token']
    transport = DynamicV2Transport(token=token)
    schema = transport.get_schema(token)
    checks['schema_hash'] = schema['schema_hash']
    if mode == 'schema':
        return
    uhd, MultiUSRP = build_uhd_bindings(schema, transport_factory=lambda: transport)
    revoked = [False]
    with owning_radio(MultiUSRP, token, revoked) as radio:
        assert radio.get_usrp_rx_info(0)['mboard_serial'] == 'DEV_INTEGRATION_01'
        checks['native_rx_before'] = native_rx(radio, uhd)
        page = radio.integration_list({})
        checks['discovery'] = page
        if mode == 'disabled':
            assert page['enabled'] is False
            checks['disabled_start'] = refused(lambda: radio.integration_start({
                'integration_id': 'org.remoterf.dev_fixture', 'profile': 'test', 'configuration': {},
                'generation': page['generation'], 'request_key': 'disabled'}), 'integration_disabled')
            checks['native_rx_after_disabled_refusal'] = native_rx(radio, uhd)
            return
        assert page['enabled'] is True
        description = radio.integration_describe({'integration_id': 'org.remoterf.dev_fixture'})
        assert description['integration']['id'] == 'org.remoterf.dev_fixture'
        checks['describe'] = True
        request = {'integration_id': 'org.remoterf.dev_fixture', 'profile': 'test',
                   'configuration': {}, 'generation': page['generation'], 'request_key': 'relay-start'}
        operation = radio.integration_start(request)
        assert radio.integration_start(request)['operation_id'] == operation['operation_id']
        def status(op):
            value = radio.integration_status({'operation_id': op['operation_id']})
            assert value['state'] != 'ERROR', value.get('error')
            return value
        until(lambda: status(operation)['state'] == 'RUNNING')
        checks['relay_start_retry'] = True
        other_reservation = reserve(402)
        other_transport = DynamicV2Transport(token=other_reservation['token'])
        other_uhd, OtherRadio = build_uhd_bindings(schema, transport_factory=lambda: other_transport)
        with OtherRadio(other_reservation['token']) as other:
            checks['independent_raw_rx_during_application'] = native_rx(other, other_uhd)
            checks['other_session_operation_refused'] = refused(lambda: other.integration_status({
                'operation_id': operation['operation_id']}), 'unknown_operation')
        cancel(other_reservation['reservation_id'])
        checks['owned_native_refusal'] = refused(radio.get_time_now, error_type=RemoteRFNativeUHDError)
        assert status(operation)['state'] == 'RUNNING'
        checks['reserved_payload_refusal'] = refused(lambda: radio.integration_call({
            'operation_id': operation['operation_id'], 'action': 'echo',
            'parameters': {'__uhd_type__': 'TimeSpec'}, 'request_key': 'bad'}), 'invalid_payload')
        parameters = {'value': 'generic-route-check'}
        action_request = {'operation_id': operation['operation_id'], 'action': 'echo',
                          'parameters': parameters, 'request_key': 'echo-once'}
        action = radio.integration_call(action_request)
        assert radio.integration_call(action_request)['action_id'] == action['action_id']
        result = until(lambda: (r if (r := radio.integration_status({
            'operation_id': operation['operation_id'], 'action_id': action['action_id']}))['state']
            in {'SUCCEEDED', 'ERROR'} else None))
        assert result['state'] == 'SUCCEEDED'
        assert result['result'] == {'echo': parameters, 'calls': 1, 'fixture': 'NON_RADIO_TEST_FIXTURE'}
        checks['relay_action_retry'] = {'result': result['result']}
        radio.integration_stop({'operation_id': operation['operation_id']})
        until(lambda: status(operation)['released'])
        checks['native_rx_after_relay'] = native_rx(radio, uhd)

        path = direct_ready(token, radio)
        before = path.calls
        page = radio.integration_list({})
        assert path.calls > before
        request.update(generation=page['generation'], request_key='lost-direct-reply')
        original = path.call
        dispatched = []
        def lose_reply(*args, **kwargs):
            response = original(*args, **kwargs)
            dispatched.append(response)
            raise direct_path.PathError('injected lost direct reply after owner execution')
        with patch.object(path, 'call', lose_reply):
            operation = radio.integration_start(request)
        assert len(dispatched) == 1
        from remoterf.common.utils import unmap_arg
        from remoterf.common.grpc import grpc_pb2 as wire_pb2
        from remoterf.common.grpc.v2_codec import decode_value
        from remoterf.core.v2_errors import raise_for_envelope
        executed = wire_pb2.InvokeResponse.FromString(unmap_arg(dispatched[0].results['wire']))
        raise_for_envelope(executed.error)
        assert decode_value(executed.result)['operation_id'] == operation['operation_id']
        assert radio.integration_start(request)['operation_id'] == operation['operation_id']
        until(lambda: status(operation)['state'] == 'RUNNING')
        checks['direct_reply_loss_relay_retry'] = {'direct_dispatches': len(dispatched),
                                                 'same_operation': True, 'fault_injected': True}
        before = path.calls
        radio.integration_stop({'operation_id': operation['operation_id']})
        until(lambda: status(operation)['released'])
        assert path.calls > before
        checks['generic_direct_calls'] = {'calls': path.calls - before, 'fixed': path.fixed}
        checks['native_rx_after_direct_fallback'] = native_rx(radio, uhd)
        direct_path.stop()
        os.environ['REMOTERF_DIRECT'] = '0'

        for name, configuration, expected_error in (
                ('partial_start_failure', {'fail_start': True}, 'starting_failed'),
                ('autonomous_required_worker_exit', {'exit_after': 1.0}, 'process_exited')):
            page = radio.integration_list({})
            request.update(generation=page['generation'], request_key=name, configuration=configuration)
            failed = radio.integration_start(request)
            if name == 'autonomous_required_worker_exit':
                until(lambda: status(failed)['state'] == 'RUNNING')
            def released():
                value = radio.integration_status({'operation_id': failed['operation_id']})
                return value if value['released'] else None
            outcome = until(released, seconds=15)
            assert outcome['state'] == 'ERROR' and outcome['error'] == expected_error
            checks[name] = {'error': outcome['error'], 'released': True,
                            'health_action_called': False, 'native_rx_after': native_rx(radio, uhd)}

        # Real dev calendar cancellation revokes the active application; the
        # operator checks persistent journals externally before rollback.
        page = radio.integration_list({})
        request.update(generation=page['generation'], request_key='calendar-cancel', configuration={})
        operation = radio.integration_start(request)
        until(lambda: status(operation)['state'] == 'RUNNING')
        cancel(reservation['reservation_id'])
        revoked[0] = True
        checks['revoked_operation_refused'] = refused(lambda: radio.integration_status({
            'operation_id': operation['operation_id']}), error_type=RemoteRFReservationError)


def calendar_expiry(reserve, cancel, checks, observation=None):
    from remoterf.core.dynamic_v2_transport import DynamicV2Transport
    from remoterf.drivers.dynamic_v2 import build_uhd_bindings
    from remoterf.core.v2_errors import RemoteRFReservationError
    reservation = reserve(401, expiring=True)
    token = reservation['token']
    transport = DynamicV2Transport(token=token)
    schema = transport.get_schema(token)
    uhd, Radio = build_uhd_bindings(schema, transport_factory=lambda: transport)
    revoked = [False]
    with owning_radio(Radio, token, revoked) as radio:
        page = radio.integration_list({})
        operation = radio.integration_start({
            'integration_id': 'org.remoterf.dev_fixture', 'profile': 'test',
            'configuration': {}, 'generation': page['generation'], 'request_key': 'expiry'})
        until(lambda: radio.integration_status({'operation_id': operation['operation_id']})['state']
              == 'RUNNING')
        end = reservation['_canary_end']
        while time.time() < end + 1:
            time.sleep(min(0.2, max(0.01, end + 1 - time.time())))
        if observation is not None:
            observation.write_text(json.dumps({'calendar_end': end, 'observed_at': time.time(),
                                               'no_client_rpc_for_seconds': 10}) + '\n')
            time.sleep(10)
        revoked[0] = True
        checks['first_post_expiry_rpc_at'] = time.time()
        try:
            radio.integration_status({'operation_id': operation['operation_id']})
        except RemoteRFReservationError as exc:
            checks['expired_session_refused'] = {'type': type(exc).__name__,
                                                 'real_calendar_end': end}
        else:
            raise AssertionError('expired reservation retained access')
    cancel(reservation['reservation_id'], expired=True)
    next_reservation = reserve(401)
    next_transport = DynamicV2Transport(token=next_reservation['token'])
    next_uhd, NextRadio = build_uhd_bindings(schema, transport_factory=lambda: next_transport)
    with NextRadio(next_reservation['token']) as next_radio:
        checks['next_holder_native_rx'] = native_rx(next_radio, next_uhd)
        checks['old_operation_hidden_from_next_holder'] = refused(
            lambda: next_radio.integration_status({'operation_id': operation['operation_id']}),
            'unknown_operation')


def service_shutdown(reserve, checks, ready, confirmed):
    """Hold a real dev session while the operator stops/restarts its test unit.

    The confirmation is external observation, not framework release proof:
    require empty journals and the inactive unit before restarting. No client
    close or cancellation may reclaim the application before that observation.
    main's finally cancels the booking after the original authority is online.
    """
    assert ready is not None and confirmed is not None
    assert not ready.exists() and not confirmed.exists()
    from remoterf.core.dynamic_v2_transport import DynamicV2Transport
    from remoterf.drivers.dynamic_v2 import build_uhd_bindings
    reservation = reserve(401)
    token = reservation['token']
    transport = DynamicV2Transport(token=token)
    schema = transport.get_schema(token)
    _, Radio = build_uhd_bindings(schema, transport_factory=lambda: transport)
    radio = Radio(token)
    page = radio.integration_list({})
    operation = radio.integration_start({
        'integration_id': 'org.remoterf.dev_fixture', 'profile': 'test', 'configuration': {},
        'generation': page['generation'], 'request_key': 'service-shutdown'})
    until(lambda: radio.integration_status({'operation_id': operation['operation_id']})['state'] == 'RUNNING')
    ready.write_text(json.dumps({'running': True, 'ready_at': time.time()}) + '\n')
    until(confirmed.exists, seconds=120)
    result = json.loads(confirmed.read_text())
    assert result['journals_empty'] is True and result['unit_inactive'] is True
    assert result['service_restarted'] is True
    checks['service_shutdown'] = result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=['baseline', 'baseline-relay', 'schema', 'disabled', 'enabled', 'expiry', 'shutdown'], required=True)
    p.add_argument('--client-home', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--expiry-observation', type=Path)
    p.add_argument('--shutdown-ready', type=Path)
    p.add_argument('--shutdown-confirmed', type=Path)
    args = p.parse_args()
    home = args.client_home.resolve()
    assert home.is_absolute() and home.name.startswith('remoterf-phase1c-dev-client')
    assert home.stat().st_mode & 0o077 == 0 and stat.S_ISDIR(home.stat().st_mode)
    # This process owns disposable client storage; leave the shell HOME and
    # the operator's saved production login untouched.
    Path.home = classmethod(lambda cls: home)
    os.environ['REMOTERF_GLOBAL_ORIGIN'] = ORIGIN
    os.environ['REMOTERF_DIRECT'] = '0'
    from remoterf.deployment.backend import HttpsJsonAccountBackend
    from remoterf.core import direct_path
    backend = HttpsJsonAccountBackend(ORIGIN)
    backend.resume()
    assert backend.transport.origin == ORIGIN
    held = []
    def reserve(gid, expiring=False):
        assert gid in {99, 401, 402}
        now = int(time.time()) // 60 * 60
        end = now + 600
        if expiring:
            end = (int(time.time()) + 80) // 60 * 60
            now = end - 600
        value = backend.reserve(f'{DEPLOYMENT}:{gid}', now, end)
        value['_canary_end'] = end
        held.append(value['reservation_id'])
        return value
    def cancel(rid, expired=False):
        from remoterf.deployment.http import AccountBackendError
        try:
            backend.cancel(rid)
        except AccountBackendError as exc:
            if not expired or exc.code != 'reservation_not_found':
                raise
        held.remove(rid)
    evidence = {'passed': False, 'mode': args.mode, 'origin': ORIGIN, 'radio_backend': 'synthetic',
                'production_changed': False, 'client_version': importlib.metadata.version('remoterf'),
                'started_at': time.time(), 'checks': {}}
    try:
        if args.mode in {'baseline', 'baseline-relay'}:
            baseline(backend, reserve, evidence['checks'], direct=args.mode == 'baseline')
        elif args.mode == 'expiry':
            calendar_expiry(reserve, cancel, evidence['checks'], args.expiry_observation)
        elif args.mode == 'shutdown':
            service_shutdown(reserve, evidence['checks'], args.shutdown_ready, args.shutdown_confirmed)
        else:
            generic(args.mode, backend, reserve, cancel, evidence['checks'])
        evidence['passed'] = True
    except Exception as exc:
        evidence['failure'] = {'type': type(exc).__name__, 'code': getattr(exc, 'code', None)}
        print('Dev check failed:', type(exc).__name__, getattr(exc, 'code', None), flush=True)
        raise
    finally:
        direct_path.stop()
        cleanup = []
        for rid in held:
            try:
                backend.cancel(rid)
            except Exception as exc:
                if getattr(exc, 'code', None) not in {'reservation_not_found', 'not_found'}:
                    cleanup.append(type(exc).__name__)
        if cleanup:
            evidence.update(passed=False, cleanup_errors=cleanup)
        evidence['finished_at'] = time.time()
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
