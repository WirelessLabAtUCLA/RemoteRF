#!/usr/bin/env python3
"""Receive-only, normal-auth qualification of one isolated dev Global USRP.

The operator independently holds the UCLA booking and supervises the physical
radio. This runner cannot create that outer booking or contact production.
Calendar cases require an independent, matching resource observation before
the first post-revocation device RPC; a client close cannot manufacture proof.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib.metadata
import json
import math
import os
from pathlib import Path
import stat
import time
import traceback
import uuid

ORIGIN = 'https://dev.global.remoterf.net'
DEPLOYMENT = 'c59791c0-7645-4a4a-94d8-55c5551061a9'
DEPLOYMENT_SLUG = 'ucla-integrations-20261110'
DEPLOYMENT_NAME = 'Isolated UCLA Integration Qualification'
DEVICE_ID = 40
SERIAL = '32C78EE'
INTEGRATION = 'org.remoterf.usrp_probe'
SCHEMA_HASH = 'sha256:be1ffefaf1a6141aba81e396e76eb30226f50f46fea9f0ebd9f06eb2c32f4500'
MODES = ('disabled', 'enabled', 'cancel', 'expiry')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def until(fn, seconds=35):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(.1)
    raise RuntimeError('qualification transition exceeded deadline')


def refused(fn, *, code=None, error_type=None):
    try:
        fn()
    except Exception as exc:
        require(error_type is None or isinstance(exc, error_type), 'unexpected refusal type')
        actual = getattr(exc, 'details', {}).get('integration_code')
        require(code is None or actual == code, 'unexpected integration refusal code')
        return {'type': type(exc).__name__, 'integration_code': actual}
    raise RuntimeError('operation that should have been refused was accepted')


def write_json_new(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('x') as output:
        os.chmod(path, 0o600)
        json.dump(value, output, indent=2, sort_keys=True, allow_nan=False)
        output.write('\n')


def validate_args(args):
    require(args.deployment_id == DEPLOYMENT, 'unexpected dev deployment identity')
    require(args.deployment_name == DEPLOYMENT_NAME, 'unexpected dev deployment name')
    home = args.client_home
    require(home.is_absolute() and not home.is_symlink(), 'private client home must be an absolute real directory')
    require(home.name.startswith('remoterf-phase1c-dev-client'), 'unexpected private client home name')
    info = home.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
            'client home must be owned by the current user and private')
    require(not args.evidence.exists(), 'refusing to overwrite qualification evidence')
    if args.mode in ('cancel', 'expiry'):
        require(args.observation_request is not None and args.observation_confirmed is not None,
                'calendar cases require independent observation paths')
        paths = [args.observation_request, args.observation_confirmed, args.evidence]
        require(len({p.absolute() for p in paths}) == 3, 'observation and evidence paths must differ')
        require(not any(p.exists() for p in paths), 'calendar observation paths must be fresh')
    return home.resolve(strict=True)


def verify_catalog(catalog, deployment_name):
    device_ref = f'{DEPLOYMENT}:{DEVICE_ID}'
    rows = [row for row in catalog['devices'] if row.get('device_id') == device_ref]
    require(len(rows) == 1, 'expected exactly one matching dev radio')
    row = rows[0]
    require(row.get('deployment_id') == DEPLOYMENT
            and row.get('deployment_name') == deployment_name
            and str(row.get('local_device_id')) == str(DEVICE_ID)
            and row.get('device_type') == 'usrp'
            and row.get('online') is True, 'dev radio catalog provenance or readiness mismatch')
    return {key: row[key] for key in ('device_id', 'deployment_id', 'deployment_name',
                                     'local_device_id', 'device_type', 'online')}


class Bookings:
    def __init__(self, backend):
        self.backend = backend
        self.held = set()

    def reserve(self, *, expiring=False):
        now = int(time.time()) // 60 * 60
        end = now + (180 if expiring else 600)
        # Normal policy permits an interval whose start is in the past. Its
        # duration remains >=600 seconds; neither clocks nor DB rows change.
        start = end - 600 if expiring else now
        value = self.backend.reserve(f'{DEPLOYMENT}:{DEVICE_ID}', start, end)
        # Keep cleanup responsibility before validating the returned identity.
        # No second reservation is attempted after an ambiguous write failure.
        if isinstance(value.get('reservation_id'), str):
            self.held.add(value['reservation_id'])
        require(value['reservation_id'].startswith(DEPLOYMENT + ':'), 'wrong booking provenance')
        require(value['device_id'] == f'{DEPLOYMENT}:{DEVICE_ID}', 'wrong reserved radio')
        value['_calendar_end'] = end
        return value

    def cancel(self, rid, *, expired=False):
        try:
            self.backend.cancel(rid)
        except Exception as exc:
            if not expired or getattr(exc, 'code', None) != 'reservation_not_found':
                raise
        self.held.discard(rid)

    def cleanup(self):
        errors = []
        for rid in tuple(self.held):
            try:
                self.cancel(rid, expired=True)
            except Exception as exc:
                errors.append({'reservation_id': rid, 'type': type(exc).__name__})
        return errors


@contextmanager
def connection(token, checks):
    from remoterf.core.dynamic_v2_transport import DynamicV2Transport
    from remoterf.drivers.dynamic_v2 import build_uhd_bindings
    from remoterf.core.v2_errors import RemoteRFReservationError, RemoteRFSessionExpiredError
    from remoterf.core.grpc_client import is_global_token
    require(is_global_token(token) and '@' not in token, 'expected an unqualified Global token')
    transport = DynamicV2Transport(token=token)
    schema = transport.get_schema(token)
    require(schema['schema_hash'] == SCHEMA_HASH, 'unexpected USRP schema for exact-build qualification')
    checks['schema_hash'] = schema['schema_hash']
    uhd, Radio = build_uhd_bindings(schema, transport_factory=lambda: transport)
    radio = Radio(token)
    revoked = [False]
    try:
        require(radio.get_usrp_rx_info(0)['mboard_serial'] == SERIAL, 'wrong physical radio serial')
        yield radio, uhd, revoked
    finally:
        try:
            radio.close()
        except (RemoteRFReservationError, RemoteRFSessionExpiredError):
            if not revoked[0]:
                raise


def native_rx(radio, uhd):
    import numpy as np
    require(radio.get_usrp_rx_info(0)['mboard_serial'] == SERIAL, 'wrong physical radio before receive')
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
        total, deadline = 0, time.monotonic() + 15
        metadata = uhd.types.RXMetadata()
        while total < 4096 and time.monotonic() < deadline:
            samples = np.zeros((1, 4096 - total), dtype=np.complex64)
            count = stream.recv(samples, metadata, 1.0)
            require(type(count) is int and 0 <= count <= samples.shape[1], 'invalid receive count')
            require(np.isfinite(samples[:, :count]).all(), 'receive returned nonfinite samples')
            total += count
        require(total == 4096, 'receive did not complete within its deadline')
        return {'received': total, 'rate': radio.get_rx_rate(0), 'finite': True, 'serial': SERIAL}
    finally:
        stream.close()


def discovery(radio, *, enabled):
    page = radio.integration_list({})
    rows = [row for row in page['integrations'] if row['id'] == INTEGRATION]
    require(len(rows) == 1, 'probe integration absent or duplicated')
    require(page['enabled'] is enabled and rows[0]['enabled'] is enabled, 'unexpected integration availability')
    desc = radio.integration_describe({'integration_id': INTEGRATION})
    require(desc['integration']['id'] == INTEGRATION, 'describe returned another integration')
    return page


def start_request(page, key):
    return {'integration_id': INTEGRATION, 'profile': 'receive', 'configuration': {},
            'generation': page['generation'], 'request_key': key}


def start_running(radio, page, checks, key):
    from remoterf.core.v2_errors import RemoteRFNativeUHDError
    request = start_request(page, key)
    operation = radio.integration_start(request)
    require(radio.integration_start(request)['operation_id'] == operation['operation_id'],
            'duplicate start created another operation')
    def running():
        value = radio.integration_status({'operation_id': operation['operation_id']})
        require(value['state'] != 'ERROR', 'application failed during startup')
        return value if value['state'] == 'RUNNING' else None
    until(running)
    checks['duplicate_start_same_operation'] = True
    checks['native_refused_while_owned'] = refused(radio.get_time_now, error_type=RemoteRFNativeUHDError)
    require(running() is not None, 'native refusal disrupted application ownership')
    action_request = {'operation_id': operation['operation_id'], 'action': 'health',
                      'parameters': {}, 'request_key': key + '-health'}
    action = radio.integration_call(action_request)
    require(radio.integration_call(action_request)['action_id'] == action['action_id'],
            'duplicate action created another action')
    def complete():
        value = radio.integration_status({'operation_id': operation['operation_id'], 'action_id': action['action_id']})
        return value if value['state'] in ('SUCCEEDED', 'ERROR') else None
    result = until(complete)
    require(result['state'] == 'SUCCEEDED', 'health action failed')
    payload = result['result']
    require(payload.get('ready') is True and payload.get('serial') == SERIAL and payload.get('received') == 4096,
            'health did not prove receive on the expected radio')
    checks['duplicate_health_same_action'] = True
    checks['health'] = payload
    return operation


def lifecycle(mode, bookings, checks, key):
    reservation = bookings.reserve()
    with connection(reservation['token'], checks) as (radio, uhd, _):
        checks['native_rx_before'] = native_rx(radio, uhd)
        enabled = mode == 'enabled'
        page = discovery(radio, enabled=enabled)
        checks['discovery'] = page
        checks['describe'] = True
        if not enabled:
            checks['disabled_start_refused'] = refused(
                lambda: radio.integration_start(start_request(page, key)), code='integration_disabled')
            checks['native_rx_after_refusal'] = native_rx(radio, uhd)
        else:
            operation = start_running(radio, page, checks, key)
            radio.integration_stop({'operation_id': operation['operation_id']})
            def released():
                value = radio.integration_status({'operation_id': operation['operation_id']})
                require(value['state'] != 'ERROR', 'application stop failed')
                return value if value['released'] else None
            outcome = until(released)
            checks['stop_released'] = outcome['released']
            checks['native_rx_after_stop'] = native_rx(radio, uhd)
    bookings.cancel(reservation['reservation_id'])


def validate_observation(request, proof):
    for field in ('run_id', 'mode', 'deployment_id', 'device_id', 'serial', 'operation_id', 'reservation_id'):
        require(proof.get(field) == request[field], 'independent observation does not match this run')
    observed = proof.get('observed_at')
    require(type(observed) in (int, float) and math.isfinite(observed)
            and request['quiet_started_at'] + 10 <= observed <= time.time() + 1,
            'independent observation predates the quiet interval or is from the future')
    require(proof.get('root_identity_verified') is True, 'supervisor identity was not independently verified')
    require(proof.get('usb_observation_scope') == 'current uid only',
            'USB observation visibility must be explicitly bounded')
    for field in ('hardware_owners', 'populated_owned_scopes', 'unknown_owned_scopes', 'observable_usb_owners'):
        require(type(proof.get(field)) is int and proof[field] == 0, 'independent observation still reports resources')
    return proof


def observe_without_rpc(args, request):
    write_json_new(args.observation_request, request)
    # This loop performs filesystem reads only. No device RPC, root close,
    # cancellation retry or next booking may help the calendar release ownership.
    until(args.observation_confirmed.exists, seconds=180)
    require(args.observation_confirmed.stat().st_size <= 65536, 'independent observation is oversized')
    proof = json.loads(args.observation_confirmed.read_text())
    return validate_observation(request, proof)


def calendar(args, bookings, checks, run_id):
    from remoterf.core.v2_errors import RemoteRFReservationError, RemoteRFSessionExpiredError
    reservation = bookings.reserve(expiring=args.mode == 'expiry')
    with connection(reservation['token'], checks) as (radio, uhd, revoked):
        checks['native_rx_before'] = native_rx(radio, uhd)
        page = discovery(radio, enabled=True)
        operation = start_running(radio, page, checks, run_id)
        quiet_started = time.time()
        if args.mode == 'cancel':
            bookings.cancel(reservation['reservation_id'])
            event_at = time.time()
        else:
            event_at = reservation['_calendar_end']
            require(event_at > quiet_started, 'reservation expired before the no-RPC interval began')
            while time.time() <= event_at + 1:
                time.sleep(.2)
        revoked[0] = True
        request = {'run_id': run_id, 'mode': args.mode, 'deployment_id': DEPLOYMENT,
                   'device_id': DEVICE_ID, 'serial': SERIAL, 'operation_id': operation['operation_id'],
                   'reservation_id': reservation['reservation_id'], 'event_at': event_at,
                   'calendar_end': reservation['_calendar_end'], 'quiet_started_at': max(quiet_started, event_at)}
        checks['independent_release_observation'] = observe_without_rpc(args, request)
        checks['release_observation_scope'] = {
            'source': 'independent ownership journal, kernel cgroups and USB owners visible to the service uid',
            'in_process_runtime_counters_observed': False,
            'cross_uid_usb_owners_observed': False}
        checks['first_post_revocation_device_rpc_at'] = time.time()
        checks['no_device_rpc_seconds_after_revocation'] = time.time() - request['quiet_started_at']
        checks['revoked_session_refused'] = refused(
            lambda: radio.integration_status({'operation_id': operation['operation_id']}),
            error_type=(RemoteRFReservationError, RemoteRFSessionExpiredError))
    if args.mode == 'expiry':
        bookings.cancel(reservation['reservation_id'], expired=True)
    next_reservation = bookings.reserve()
    with connection(next_reservation['token'], checks) as (radio, uhd, _):
        checks['next_holder_native_rx'] = native_rx(radio, uhd)
        checks['old_operation_hidden_from_next_holder'] = refused(
            lambda: radio.integration_status({'operation_id': operation['operation_id']}), code='unknown_operation')
    bookings.cancel(next_reservation['reservation_id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=MODES, required=True)
    parser.add_argument('--deployment-id', required=True)
    parser.add_argument('--deployment-name', default=DEPLOYMENT_NAME)
    parser.add_argument('--client-home', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--observation-request', type=Path)
    parser.add_argument('--observation-confirmed', type=Path)
    args = parser.parse_args()
    home = validate_args(args)
    # Isolate only this process's client persistence. The operator's actual HOME
    # and saved account remain unchanged. Keyring and direct routes are disabled.
    Path.home = classmethod(lambda cls: home)
    os.environ['REMOTERF_GLOBAL_ORIGIN'] = ORIGIN
    os.environ['REMOTERF_USE_KEYRING'] = '0'
    os.environ['REMOTERF_DIRECT'] = '0'
    from remoterf.deployment.backend import HttpsJsonAccountBackend
    from remoterf.deployment.state import select_target
    from remoterf.core.grpc_client import bind_federated_backend
    from remoterf.core import direct_path
    backend = HttpsJsonAccountBackend(ORIGIN)
    bookings = Bookings(backend)
    run_id = uuid.uuid4().hex
    evidence = {'passed': False, 'run_id': run_id, 'mode': args.mode, 'origin': ORIGIN,
                'deployment_id': DEPLOYMENT, 'deployment_slug': DEPLOYMENT_SLUG,
                'deployment_name': DEPLOYMENT_NAME, 'device_id': DEVICE_ID, 'serial': SERIAL,
                'radio_backend': 'usrp', 'route': 'relay', 'transmit': False,
                'production_changed': False, 'client_version': importlib.metadata.version('remoterf'),
                'started_at': time.time(), 'checks': {}}
    try:
        require(backend.resume() is True, 'private dev account has no saved authenticated session')
        require(backend.transport.origin == ORIGIN, 'account origin changed unexpectedly')
        select_target({'origin': ORIGIN, 'transport': 'https-json',
                       'deployment_id': backend.capabilities['deployment_id']})
        bind_federated_backend(backend)
        evidence['checks']['catalog'] = verify_catalog(backend.devices(), args.deployment_name)
        if args.mode in ('cancel', 'expiry'):
            calendar(args, bookings, evidence['checks'], run_id)
        else:
            lifecycle(args.mode, bookings, evidence['checks'], run_id)
        evidence['passed'] = True
    except Exception as exc:
        evidence['failure'] = {'type': type(exc).__name__, 'code': getattr(exc, 'code', None),
            'frames': [{'function': frame.name, 'line': frame.lineno}
                       for frame in traceback.extract_tb(exc.__traceback__)]}
        # Do not persist exception messages, RPC contents, account state or tokens.
        print('Dev USRP qualification failed:', type(exc).__name__, flush=True)
    finally:
        direct_path.stop()
        errors = bookings.cleanup()
        if errors:
            evidence.update(passed=False, cleanup_errors=errors)
        evidence['finished_at'] = time.time()
        write_json_new(args.evidence, evidence)
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
