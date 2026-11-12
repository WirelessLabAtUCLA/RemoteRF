#!/usr/bin/env python3
"""One fresh-process native-open qualification through unmodified local auth.

Use a fresh private state directory for EVERY invocation. Hardware invocations
must run inside an independently guarded cgroup under an external reservation.
This checks the normal local account/calendar/token resolver, not Global routing.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import importlib.util
import json
import math
import re
import os
from pathlib import Path
import secrets
import sys
from threading import Event, Thread
import time
import traceback

PROCESS_STARTED = time.monotonic()
INITIAL_NATIVE_MODULES = {name: name in sys.modules for name in ('numpy', 'uhd')}
_spec = importlib.util.spec_from_file_location('_cold_service_helpers', Path(__file__).with_name('usrp_service_stop.py'))
helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helpers)


def mark(evidence, phase, **details):
    evidence['phase'] = phase
    evidence['events'].append({'phase': phase, 'elapsed_seconds': time.monotonic() - PROCESS_STARTED,
                               'observed_at': time.time(), **details})


def guarded_scope_proof(args):
    """Require actual kernel membership in a bounded supervisor-owned domain."""
    if args.backend == 'fake':
        return {'required': False, 'reason': 'explicit fake native backend'}
    requested = args.guarded_scope.resolve(strict=True)
    relative = Path('/proc/self/cgroup').read_text().strip().splitlines()
    entries = [line.split('::', 1)[1] for line in relative if line.startswith('0::')]
    if len(entries) != 1:
        raise RuntimeError('hardware qualification requires unified cgroup membership')
    actual = (Path('/sys/fs/cgroup') / entries[0].lstrip('/')).resolve(strict=True)
    if (actual != requested or not actual.name.startswith('rrf-')
            or (actual / 'cgroup.type').read_text().strip() != 'domain'
            or not (actual / 'cgroup.kill').is_file()):
        raise RuntimeError('hardware process is outside the declared supervised scope')
    limits = {}
    for filename in ('memory.max', 'pids.max'):
        value = (actual / filename).read_text().strip()
        if not value.isdecimal() or int(value) <= 0:
            raise RuntimeError('hardware process scope lacks finite resource limits')
        limits[filename] = int(value)
    info = actual.stat()
    return {'scope': str(actual), 'identity': [info.st_dev, info.st_ino],
            'kernel_membership_verified': True, 'limits': limits}


def external_proof(args):
    """One bounded read-only booking check. Never used as the native resolver."""
    if args.backend == 'fake':
        return {'required': False, 'reason': 'explicit fake native backend'}
    verifier = object.__new__(helpers.handoff.Authority)
    verifier.command = [str(args.authority_command), '--reservation-id', args.reservation_id,
                        '--device-id', str(args.device_id), '--serial', args.serial]
    verifier._process = None
    record = verifier._query()
    if (record.get('active') is not True
            or (record.get('reservation_id'), record.get('device_id'), record.get('serial'))
            != (args.reservation_id, args.device_id, args.serial)
            or type(record.get('expires_at')) not in (int, float)
            or not math.isfinite(record['expires_at'])
            or record['expires_at'] < time.time() + 15):
        raise RuntimeError('external booking is not active with sufficient cleanup time')
    return {k: record[k] for k in ('reservation_id', 'device_id', 'serial', 'expires_at')}


def configure_private(args):
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    helpers.private_environment(args.state_dir)
    # Ordinary native image preload stays enabled. No test-only prewarming.
    os.environ['REMOTERF_USRP_PRELOAD'] = '1'
    config = Path(os.environ['REMOTERF_CONFIG_DIR'])
    config.mkdir(mode=0o700, parents=True)
    config.joinpath('devices.yml').write_text(json.dumps({'devices': [{
        'device_id': args.device_id, 'name': 'cold_start_reserved_usrp', 'device_type': 'usrp',
        'init': {'profile': 'usrp2901', 'type': 'b200', 'serial': args.serial}}]}))
    os.chmod(config / 'devices.yml', 0o600)
    if (config / 'integrations.json').exists():
        raise RuntimeError('qualification must not activate integrations')


def install_fake_native(args, entered):
    """Only the native driver is fake; account/calendar/auth/session code is real."""
    from remoterf_device_core.idl.schema import register_driver
    from remoterf_device_core.drivers.usrp.usrp_schema import UsrpSchema
    instances = []
    class Native:
        def __init__(self): self.closed = False
        def close(self): self.closed = True
    class Fake(UsrpSchema):
        def prepare_for_holder(self): pass
        def reset_for_next_holder(self): self.close_native_device()
        def open_native_device(self):
            native = Native(); instances.append(native); entered.set()
            time.sleep(args.fake_open_delay)
            return native
        def build_session_metadata_v2(self): return {'uhd_version': 'fake-normal-auth-cold-check'}
        def close_native_device(self):
            native, self.device = self.device, None
            if native is not None: native.close()
    register_driver('usrp')(Fake)
    return instances


def create_account(args, reservation):
    from remoteRF_server.common.utils import map_arg
    from remoteRF_server.server.user_group_handler import user_group_handler as groups
    username = 'cold_' + secrets.token_hex(6)
    code = 'cold_code_' + secrets.token_hex(6)
    group = groups.create_user_group(group_name='cold_group_' + secrets.token_hex(6),
        devices_whitelist=[args.device_id], max_reservation_time_sec=3600,
        max_reservations=1, lifetime_sec=0)
    if group is None:
        raise RuntimeError('private local group creation failed')
    created = groups.create_enrollment_codes(code_names=[code], group_id=group, uses=1, duration_sec=600)
    if not created.startswith('Created 1'):
        raise RuntimeError('private enrollment creation failed')
    result = reservation.create_user(un=map_arg(username), pw=map_arg(secrets.token_urlsafe(24)),
        em=map_arg(username + '@example.test'), ec=map_arg(code))
    if 'UC' not in result or reservation.get_account_security_state(username) != 'native':
        raise RuntimeError('normal private account registration failed')
    return username


def reserve(args, reservation, username, seconds):
    from remoteRF_server.common.utils import unmap_arg
    now = datetime.now()
    # Normal policy requires a >=600-second interval. A past start permits a
    # short real expiry without modifying a clock, database row or auth callback.
    result = reservation.reserve_device(username, args.device_id, now - timedelta(seconds=600),
                                         now + timedelta(seconds=seconds))
    if 'Token' not in result:
        raise RuntimeError('normal private reservation creation was refused')
    rows = reservation.grab_all_reservations_server()
    if len(rows) != 1:
        raise RuntimeError('private calendar contains an unexpected reservation')
    booking = next(iter(rows))
    reservation.update_devices()
    return unmap_arg(result['Token']), booking


def wait_until(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate(): return
        time.sleep(.01)
    raise RuntimeError('qualification transition exceeded deadline')


def normal_auth_snapshot(dm, v2, token, gid):
    if v2.resolve_gid_for_token is not dm.resolve_gid_for_token or v2.token_matches_device is not dm.token_matches_device:
        raise RuntimeError('native authorization functions were replaced')
    return {'resolver_unmodified': True, 'token_matches': dm.resolve_gid_for_token(token) == gid,
            'match_function_agrees': dm.token_matches_device(token, gid),
            'four_second_authority_cache_used': False}


def cleanup_observation(args, dm, manager, fake_instances):
    with manager._lock:
        sessions, closing, opening = len(manager._sessions), len(manager._closing_sessions), len(manager._opening_gids)
    with dm._state_lock:
        states = [s for s in dm._devices.values() if s.origin == 'local']
        native_inventory = sum(getattr(s.dev, 'device', None) is not None for s in states)
        deferred = len(dm._deferred_native_hooks)
    gate = dm.hardware_access.gate(args.device_id).snapshot()
    owner = gate['owner']
    if gate['active_native'] or sessions or closing or opening or native_inventory or deferred or owner is not None or manager.cleanup_pending(args.device_id):
        raise RuntimeError('native cleanup is still unresolved')
    if any(not native.closed for native in fake_instances):
        raise RuntimeError('fake native resource was not closed')
    return {'sessions': sessions, 'closing_sessions': closing, 'opening_devices': opening,
            'inventory_native_owners': native_inventory, 'deferred_native_hooks': deferred,
            'hardware_owner': owner, 'active_native': gate['active_native'], 'cleanup_pending': False,
            'fake_native_instances_closed': len(fake_instances) if args.backend == 'fake' else None}


def wait_clean(args, dm, manager, fake_instances, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return cleanup_observation(args, dm, manager, fake_instances)
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.01)


def receive(args, manager, session):
    if args.backend == 'fake':
        root = manager.get_handle(session, session.root_handle)
        with manager.admitted(session, root, session.control_lock):
            if root.value.closed: raise RuntimeError('fake native resource already closed')
            return {'received': 4096, 'fake_native': True}
    from rrf_example_usrp_probe import configuration
    settings = configuration('receive', {'rate': 1e6, 'frequency': 100e6, 'gain': 0})
    return helpers.handoff.receive_native(manager, session, settings, args.serial)


def run(args, evidence):
    evidence['guarded_scope'] = guarded_scope_proof(args)
    evidence['external_booking_before'] = external_proof(args)
    mark(evidence, 'normal_server_imports_started', initial_native_modules=INITIAL_NATIVE_MODULES)
    from remoteRF_server.server import device_manager as dm
    from remoteRF_server.server import dynamic_v2 as v2
    from remoteRF_server.server.reservation import reservation_handler as reservation
    from remoterf_device_core.drivers.usrp import usrp_schema
    manager = v2.session_manager
    evidence['source_identity'] = helpers.source_identity()
    evidence['source_identity']['cold_harness_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    evidence['native_startup'] = {'test_only_prewarm': False, 'ordinary_preload_enabled': True,
        'preloaded_entries_after_normal_imports': len(usrp_schema._preloaded),
        'native_modules_after_normal_imports': {n: n in sys.modules for n in INITIAL_NATIVE_MODULES}}
    if usrp_schema._preloaded:
        raise RuntimeError('fresh-process image preload bookkeeping was not empty')
    mark(evidence, 'normal_server_imports_complete')
    entered = Event()
    fake_instances = install_fake_native(args, entered) if args.backend == 'fake' else []
    dm.reload_devices()
    if set(dm.get_all_devices()) != {args.device_id}:
        raise RuntimeError('private inventory did not match the one intended radio')
    username = create_account(args, reservation)
    mark(evidence, 'private_account_created')
    stop_calendar, calendar_errors, calendar_ticks = Event(), [], []
    def tick():
        while not stop_calendar.wait(1.0):
            try:
                reservation.update_devices(); calendar_ticks.append(time.monotonic())
            except BaseException as exc:
                calendar_errors.append(type(exc).__name__); return
    calendar = Thread(target=tick, name='qualification-local-calendar', daemon=True)
    case = args.case
    token = None
    try:
        token, booking = reserve(args, reservation, username, 1.0 if case == 'expire_during_open' else 600)
        mark(evidence, 'normal_calendar_authorized', **normal_auth_snapshot(dm, v2, token, args.device_id))
        try:
            manager.open('invalid-' + secrets.token_urlsafe(24))
        except v2.RemoteRFReservationError:
            evidence['invalid_token_refused'] = True
        else:
            raise RuntimeError('unrelated token unexpectedly opened native hardware')
        calendar.start()
        cancel_thread = None
        if case == 'cancel_during_open':
            def cancel():
                if not entered.wait(10):
                    calendar_errors.append('fake_native_open_not_entered'); return
                reservation.remove_reservation(res_id=booking)
                reservation.update_devices()
            cancel_thread = Thread(target=cancel, name='qualification-private-cancel', daemon=True)
            cancel_thread.start()
        open_start = time.monotonic()
        with manager._lock:
            evidence['epoch_before_first_open'] = manager._epochs.get(args.device_id, 0)
        mark(evidence, 'first_native_open_started', **normal_auth_snapshot(dm, v2, token, args.device_id))
        failure = None
        try:
            session = manager.open(token)
            evidence['first_open_seconds'] = time.monotonic() - open_start
            mark(evidence, 'first_native_open_complete', **normal_auth_snapshot(dm, v2, token, args.device_id))
            evidence['first_receive'] = receive(args, manager, session)
            mark(evidence, 'first_receive_complete')
        except Exception as exc:
            failure = exc
            with manager._lock:
                failure_state = {'epoch': manager._epochs.get(args.device_id, 0), 'stopping': manager._stopping,
                    'opening_devices': len(manager._opening_gids), 'closing_sessions': len(manager._closing_sessions)}
            evidence['first_attempt_refusal'] = {'type': type(exc).__name__, 'code': getattr(exc, 'code', None),
                'elapsed_seconds': time.monotonic() - open_start,
                'normal_authorization': normal_auth_snapshot(dm, v2, token, args.device_id),
                'session_state': failure_state,
                'trace': [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
                          for f in traceback.extract_tb(exc.__traceback__)[-8:]]}
        if cancel_thread is not None:
            cancel_thread.join(timeout=10)
            if cancel_thread.is_alive(): raise RuntimeError('private cancellation did not finish')
        if calendar_errors: raise RuntimeError('normal local calendar update failed')
        if case == 'valid':
            if failure is not None: raise failure
            if dm.resolve_gid_for_token(token) != args.device_id: raise RuntimeError('valid authorization was lost')
        else:
            if failure is None or not isinstance(failure, (v2.RemoteRFSessionExpiredError, v2.RemoteRFReservationError)):
                raise RuntimeError('ended reservation did not refuse the delayed native open')
            if dm.resolve_gid_for_token(token) is not None: raise RuntimeError('ended reservation still resolves')
        manager.close_all()
        wait_until(lambda: not manager.cleanup_pending(args.device_id))
        evidence['cleanup_after_first_attempt'] = wait_clean(args, dm, manager, fake_instances)
        if case != 'valid':
            token, booking = reserve(args, reservation, username, 600)
            mark(evidence, 'next_holder_authorized', **normal_auth_snapshot(dm, v2, token, args.device_id))
            session = manager.open(token)
            evidence['next_holder_receive'] = receive(args, manager, session)
            mark(evidence, 'next_holder_receive_complete')
        evidence['normal_auth_final'] = normal_auth_snapshot(dm, v2, token, args.device_id)
    finally:
        stop_calendar.set()
        if calendar.ident is not None: calendar.join(timeout=5)
        if calendar.is_alive(): raise RuntimeError('calendar worker did not stop')
        # Cancel only this private calendar; external booking is coordinator-owned.
        for booking in reservation.grab_all_reservations_server():
            reservation.remove_reservation(res_id=booking)
        reservation.update_devices()
        manager.close_all(stop_admission=True)
        wait_until(lambda: not manager.cleanup_pending(args.device_id))
        dm.close_local_devices()
        wait_until(lambda: not dm._native_cleanup_pending(args.device_id) and not dm._deferred_native_hooks)
        # Wait for any ordinary reservation prepare/reset hook holding its I/O lock.
        with dm._state_lock: states = list(dm._devices.values())
        for state in states:
            if not state.io_lock.acquire(timeout=15): raise RuntimeError('native inventory hook did not finish')
            state.io_lock.release()
        evidence['cleanup_final'] = wait_clean(args, dm, manager, fake_instances)
        evidence['private_calendar_empty'] = not reservation.grab_all_reservations_server()
        evidence['calendar_updates'] = len(calendar_ticks)
        evidence['calendar_interval_seconds'] = 1.0
        evidence['calendar_errors'] = calendar_errors
    evidence['external_booking_after'] = external_proof(args)
    if not evidence['private_calendar_empty'] or calendar_errors:
        raise RuntimeError('private calendar cleanup failed')
    if dm.resolve_gid_for_token(token) is not None:
        raise RuntimeError('cancelled final token still resolves')
    mark(evidence, 'complete')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('usrp', 'fake'), required=True)
    parser.add_argument('--case', choices=('valid', 'expire_during_open', 'cancel_during_open'), default='valid')
    parser.add_argument('--serial', required=True)
    parser.add_argument('--device-id', type=int, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--fake-open-delay', type=float, default=2.0)
    parser.add_argument('--authority-command', type=Path)
    parser.add_argument('--reservation-id')
    parser.add_argument('--guarded-scope', type=Path)
    args = parser.parse_args()
    if not args.state_dir.is_absolute() or not args.evidence.is_absolute() or args.device_id < 1:
        parser.error('private paths must be absolute and device id positive')
    if args.backend == 'usrp' and (args.case != 'valid' or not args.authority_command or not args.reservation_id or not args.guarded_scope):
        parser.error('hardware requires external authority/id, a guarded scope and the valid case')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.serial):
        parser.error('serial must be one explicit safe serial identifier')
    if args.backend == 'usrp' and not args.guarded_scope.is_absolute():
        parser.error('guarded scope must be absolute')
    if args.backend == 'usrp' and (not args.authority_command.is_absolute() or not args.authority_command.is_file()
                                  or not os.access(args.authority_command, os.X_OK)):
        parser.error('external authority must be an absolute executable file')
    if not 0 <= args.fake_open_delay <= 10:
        parser.error('fake native delay must be bounded to 0..10 seconds')
    configure_private(args)
    evidence = {'format': 'remoterf-normal-auth-cold-start-1', 'passed': False, 'events': [],
        'backend': args.backend, 'case': args.case, 'serial': args.serial, 'device_id': args.device_id,
        'started_at': time.time(), 'transmit': False, 'integrations_enabled': False,
        'route': 'normal_local_account_calendar_and_session_manager_no_public_listener',
        'global_calendar_or_rpc_qualified': False}
    try:
        run(args, evidence)
        evidence['passed'] = True
    except BaseException as exc:
        evidence['failure'] = {'type': type(exc).__name__, 'code': getattr(exc, 'code', None),
            'trace': [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
                      for f in traceback.extract_tb(exc.__traceback__)[-8:]]}
        evidence['independent_cleanup_required'] = True
    finally:
        evidence['finished_at'] = time.time()
        helpers.handoff.write_evidence(args.evidence, evidence)
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
