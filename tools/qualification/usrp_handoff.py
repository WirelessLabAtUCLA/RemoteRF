#!/usr/bin/env python3
"""Opt-in receive-only real-USRP ownership qualification; never activates a service.

Run on the radio's lab host while holding an actual reservation. The required
operator-owned authority provider must verify that reservation independently.
This harness exercises isolated local SessionManager/Runtime instances, not the
deployed Global RPC/authentication route. Local expiry is an explicit injected
fault while the real external reservation remains held for safe cleanup.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import replace
import hashlib
from importlib.metadata import distribution, version
import json
import math
import os
from pathlib import Path
import select
import subprocess
import sys
import stat
from threading import Event, Lock, Thread
import time
from unittest.mock import patch
from uuid import uuid4


class Authority:
    """Fresh cached proof from a bounded external verifier, never an RPC callback."""
    def __init__(self, command, reservation_id, device_id, serial):
        self.command = [command, '--reservation-id', reservation_id, '--device-id', str(device_id), '--serial', serial]
        self.expected = (reservation_id, device_id, serial)
        self._lock = Lock()
        self._valid_until = 0.0
        self._stop = Event()
        self._process = None
        self.last = None
        self.refresh()
        if not self.active():
            raise RuntimeError('the external reservation is not active for this serial/device')
        self._thread = Thread(target=self._run, name='qualification-reservation', daemon=True)
        self._thread.start()

    def _query(self):
        process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, close_fds=True)
        self._process = process
        output = bytearray()
        deadline = time.monotonic() + 2
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], max(0.0, min(0.05, deadline - time.monotonic())))
                if ready:
                    data = os.read(process.stdout.fileno(), 4097 - len(output))
                    output.extend(data)
                    if len(output) > 4096:
                        raise RuntimeError('authority output exceeds its bound')
                    if not data:
                        break
            else:
                raise RuntimeError('authority verifier timed out')
            if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                raise RuntimeError('authority verifier failed')
            return json.loads(output)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            process.stdout.close()
            self._process = None

    def refresh(self):
        valid_until = 0.0
        record = None
        try:
            value = self._query()
            expires = value.get('expires_at')
            if (value.get('active') is not True
                    or (value.get('reservation_id'), value.get('device_id'), value.get('serial')) != self.expected
                    or type(expires) not in (int, float) or not math.isfinite(expires)):
                raise RuntimeError('invalid reservation proof')
            remaining = expires - time.time()
            if remaining <= 0:
                raise RuntimeError('reservation ended')
            valid_until = time.monotonic() + min(4.0, remaining)
            record = {'reservation_id': self.expected[0], 'device_id': self.expected[1],
                      'serial': self.expected[2], 'expires_at': expires}
        except (Exception, SystemExit):
            pass
        with self._lock:
            self._valid_until = valid_until
            if record is not None:
                self.last = record

    def _run(self):
        while not self._stop.wait(1.0):
            self.refresh()

    def active(self):
        with self._lock:
            return time.monotonic() < self._valid_until

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)


class PreparedScope:
    """Provisioning happens only after Runtime has persisted the radio claim."""
    def __init__(self, supervisor, owner, work_dir):
        self.supervisor, self.owner, self.work_dir = supervisor, owner, work_dir
        self.scope = None
        self.attempted = False
    def prepare(self):
        self.attempted = True
        self.work_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.scope = self.supervisor.create(self.owner)
    def resources_released(self):
        return self.scope.resources_released() if self.scope is not None else not self.attempted
    def resource_failure(self):
        return self.scope.resource_failure() if self.scope is not None else None
    def launch(self, *args, **kwargs):
        if self.scope is None:
            raise RuntimeError('owned process scope has not been prepared')
        return self.scope.launch(*args, **kwargs)
    def snapshot(self):
        return self.scope.snapshot()
    def log_tail(self, *args):
        return self.scope.log_tail(*args)
    def stop(self, *args, **kwargs):
        return self.scope.stop(*args, **kwargs) if self.scope is not None else not self.attempted
    def close(self):
        if self.scope is not None:
            self.scope.close()


def wait_for(predicate, *, timeout=40, authority=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if authority is not None and not authority.active():
            raise RuntimeError('external reservation ended during qualification')
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise RuntimeError('qualification transition exceeded its deadline')


def receive_native(manager, session, settings, serial):
    import numpy as np
    import uhd
    from rrf_example_usrp_probe.worker import receive_burst
    root = manager.get_handle(session, session.root_handle)
    with manager.admitted(session, root, session.control_lock):
        actual = str(session.schema.device.get_usrp_rx_info(settings['channel']).get('mboard_serial', ''))
        if actual.casefold() != serial.casefold():
            raise RuntimeError('native session opened an unexpected serial')
        return receive_burst(session.schema.device, settings, uhd=uhd, np=np)


def configure_local(args, authority, stack, journal):
    """Isolated inventory/token resolver backed by a real externally checked lease."""
    from remoteRF_server.server import dynamic_v2 as v2
    from remoteRF_server.drivers.usrp.usrp_schema import UsrpSchema
    from remoterf_device_core.hardware_access import AccessTable
    inventory = UsrpSchema()
    inventory.bind_inventory({'serial': args.serial, 'type': 'b200', 'profile': args.profile})
    access = AccessTable()
    access.configure({args.device_id: 'usrp:serial:' + args.serial}, journal)
    local = {'enabled': True, 'token': uuid4().hex}
    def resolve(token):
        return args.device_id if local['enabled'] and token == local['token'] and authority.active() else None
    context = {'schema': inventory, 'dtype': 'usrp', 'ident': args.serial, 'name': 'isolated_qualification'}
    stack.enter_context(patch.object(v2, 'hardware_access', access))
    stack.enter_context(patch.object(v2, 'resolve_gid_for_token', resolve))
    stack.enter_context(patch.object(v2, 'token_matches_device', lambda token, gid: gid == resolve(token)))
    stack.enter_context(patch.object(v2, 'get_local_device_context', lambda gid: context if gid == args.device_id else None))
    manager = v2.SessionManager()
    return manager, local, access.gate(args.device_id)


def fail_unless(value, message):
    if not value:
        raise RuntimeError(message)


def expected_refusal(action):
    from remoterf_device_core.integrations import IntegrationError
    try:
        action()
    except IntegrationError as exc:
        if exc.code not in {'device_owned', 'device_busy'}:
            raise
    else:
        raise RuntimeError('a competing native owner was incorrectly admitted')


def run_lifecycle(args, authority, evidence, *, orphan=False):
    from remoterf_device_core.integrations import Manifest, Registry
    from remoterf_device_core.integrations.ownership import Journal
    from remoterf_device_core.integrations.runtime import Runtime
    from remoterf_device_core.integrations.supervision import CgroupSupervisor
    from rrf_example_usrp_probe import configuration
    settings = configuration('receive', {'rate': args.rate, 'frequency': args.frequency, 'gain': args.gain})
    manifest = Manifest.from_file(distribution('remoterf-example-usrp-probe').locate_file(
        'rrf_example_usrp_probe/integration.json'))
    registry = Registry([manifest], enabled={str(args.device_id): [manifest.integration_id]})
    journal = Journal(args.state_dir / 'radio-owners')
    supervisor = CgroupSupervisor(args.cgroup_root, args.state_dir / 'process-owners')
    # Check actual delegation/limits/kill support before opening any radio.
    preflight = supervisor.create('preflight-' + uuid4().hex)
    fail_unless(preflight.resources_released(), 'empty process-scope preflight was not verifiable')
    preflight.close()
    runtime = Runtime(registry, capacity=16, workers=2, timeout=30)
    scopes = []
    session = None
    release_stop = Event()
    release_start = Event()
    manager = None
    try:
        with ExitStack() as stack:
            manager, local, gate = configure_local(args, authority, stack, journal)
            evidence['stage'] = 'native_rx_before'
            session = manager.open(local['token'])
            evidence['checks']['native_rx_before'] = receive_native(manager, session, settings, args.serial)
            def start_case(name):
                evidence['stage'] = name
                scoped = PreparedScope(supervisor, uuid4().hex, args.state_dir / ('work-' + name))
                scopes.append(scoped)
                binding = manager.integration_binding(session.session_id, work_dir=scoped.work_dir,
                                                      resources_released=scoped.resources_released)
                binding = replace(binding, selector={'serial': args.serial}, processes=scoped, prepare=scoped.prepare,
                                  resource_owner=scoped.owner, finalize=scoped.stop, resource_failure=scoped.resource_failure)
                response = runtime.start(binding, manifest.integration_id, 'receive', settings, request_key=name)
                return binding, scoped, response['operation_id']
            def running(binding, operation_id):
                def ready():
                    status = runtime.status(binding, operation_id)
                    if status['state'] == 'ERROR':
                        evidence['failed_operation'] = status
                        raise RuntimeError('probe startup failed: ' + str(status['error']))
                    return status if status['state'] == 'RUNNING' else None
                return wait_for(ready, authority=authority)
            def stopped(binding, scoped, operation_id):
                wait_for(lambda: runtime.drained(binding), authority=authority)
                fail_unless(scoped.resources_released(), 'owned processes remain after stop')
                fail_unless(not gate.snapshot()['blocked'], 'radio remained fenced after verified cleanup')
                scoped.close()
                return runtime.status(binding, operation_id)

            binding, scoped, operation_id = start_case('normal')
            ready = running(binding, operation_id)
            evidence['checks']['application_rx'] = ready['details']
            expected_refusal(lambda: manager.open(local['token']))
            duplicate = runtime.start(binding, manifest.integration_id, 'receive', settings, request_key='normal')
            fail_unless(duplicate['operation_id'] == operation_id, 'retry started a second application')
            if orphan:
                # Deliberately skip Python finally/cleanup. The parent holds the
                # reservation and will reconcile the persisted cgroup identity.
                (args.state_dir / 'orphan-ready.json').write_text(json.dumps(
                    {'operation_id': operation_id, 'scope_owner': scoped.owner, 'rx': ready['details']}))
                os._exit(71)
            action = runtime.call(binding, operation_id, 'health', {}, request_key='health')
            wait_for(lambda: runtime.action_status(binding, operation_id, action['action_id'])['state'] == 'SUCCEEDED',
                     authority=authority)
            generation = binding.generation
            runtime.stop(binding, operation_id)
            evidence['checks']['explicit_stop'] = stopped(binding, scoped, operation_id)
            fail_unless(evidence['checks']['explicit_stop']['state'] == 'IDLE'
                        and evidence['checks']['explicit_stop']['error'] is None, 'normal stop failed or timed out')
            fail_unless(session.handles[session.root_handle].generation > generation, 'native generation did not advance')
            evidence['checks']['native_rx_after'] = receive_native(manager, session, settings, args.serial)

            # Partial startup really acquired/received before raising. Fault
            # injection stays in this operator harness, outside the public adapter.
            create = registry.create
            partial_ready = Event()
            def failing_create(integration, context):
                adapter = create(integration, context)
                start = adapter.start
                def fail_after_ready(profile, configuration):
                    start(profile, configuration)
                    partial_ready.set()
                    raise RuntimeError('injected failure after actual RX readiness')
                adapter.start = fail_after_ready
                return adapter
            registry.create = failing_create
            binding, scoped, operation_id = start_case('partial-start')
            wait_for(lambda: runtime.drained(binding), authority=authority)
            fail_unless(runtime.status(binding, operation_id)['state'] == 'ERROR', 'partial startup was reported successful')
            fail_unless(partial_ready.is_set(), 'partial-start fault did not follow actual receive readiness')
            evidence['checks']['partial_start_cleanup'] = stopped(binding, scoped, operation_id)
            evidence['checks']['rx_after_partial_start'] = receive_native(manager, session, settings, args.serial)
            registry.create = create

            binding, scoped, operation_id = start_case('process-crash')
            running(binding, operation_id)
            fail_unless(scoped.stop(), 'could not inject supervised child termination')
            wait_for(lambda: runtime.drained(binding), authority=authority)
            fail_unless(runtime.status(binding, operation_id)['error'] == 'process_exited',
                        'required child exit was not detected without a health action')
            evidence['checks']['process_exit_cleanup'] = stopped(binding, scoped, operation_id)
            evidence['checks']['rx_after_process_exit'] = receive_native(manager, session, settings, args.serial)

            entered_stop = Event()
            def delayed_create(integration, context):
                adapter = create(integration, context)
                stop = adapter.stop
                def delayed_stop():
                    entered_stop.set()
                    release_stop.wait()
                    stop()
                adapter.stop = delayed_stop
                return adapter
            registry.create = delayed_create
            binding, scoped, operation_id = start_case('delayed-stop')
            running(binding, operation_id)
            runtime.timeout = 0.2
            runtime.stop(binding, operation_id)
            wait_for(entered_stop.is_set, authority=authority)
            wait_for(lambda: runtime.status(binding, operation_id)['error'] == 'timeout', authority=authority)
            fail_unless(gate.snapshot()['blocked'] and not scoped.resources_released(), 'timeout falsely released ownership')
            expected_refusal(lambda: manager.open(local['token']))
            release_stop.set()
            evidence['checks']['stop_timeout_kept_ownership'] = stopped(binding, scoped, operation_id)
            evidence['checks']['rx_after_delayed_stop'] = receive_native(manager, session, settings, args.serial)
            runtime.timeout = 30
            registry.create = create

            # Expire a local session while its start hook is still active, but
            # only after the real child has acquired the radio and received RX.
            entered_start, expired_stop_called = Event(), Event()
            expired_start_rx = {}
            def pending_create(integration, context):
                adapter = create(integration, context)
                start, stop = adapter.start, adapter.stop
                def pending_start(profile, configuration):
                    start(profile, configuration)
                    expired_start_rx.update(adapter.status())
                    entered_start.set()
                    release_start.wait()
                def observed_stop():
                    expired_stop_called.set()
                    stop()
                adapter.start, adapter.stop = pending_start, observed_stop
                return adapter
            registry.create = pending_create
            binding, scoped, operation_id = start_case('expired-start')
            wait_for(entered_start.is_set, authority=authority)
            local['enabled'] = False
            manager.close_gid(args.device_id, wait=False)
            runtime.poll()
            fail_unless(gate.snapshot()['blocked'] and not runtime.drained(binding)
                        and not scoped.resources_released(), 'pending expired start falsely released ownership')
            fail_unless(not expired_stop_called.is_set(), 'cleanup raced the active start hook')
            fail_unless(session.schema.device is None, 'expired start reacquired native hardware')
            local.update(enabled=True, token=uuid4().hex)
            expected_refusal(lambda: manager.open(local['token']))
            release_start.set()
            outcome = stopped(binding, scoped, operation_id)
            fail_unless(outcome['state'] == 'ERROR' and outcome['error'] == 'cancelled',
                        'expired start was not cancelled')
            fail_unless(expired_stop_called.is_set() and session.schema.device is None,
                        'expired start cleanup did not preserve native release')
            evidence['checks']['expired_start'] = {'rx_before_local_revocation': expired_start_rx,
                'ownership_retained_until_hook_return': True, 'cleanup_serialized': True,
                'outcome': outcome}
            wait_for(lambda: not manager.cleanup_pending(args.device_id), authority=authority)
            session = manager.open(local['token'])
            evidence['checks']['rx_after_expired_start'] = receive_native(manager, session, settings, args.serial)
            registry.create = create

            binding, scoped, operation_id = start_case('local-expiry')
            running(binding, operation_id)
            local['enabled'] = False
            manager.close_gid(args.device_id, wait=False)
            runtime.poll()
            evidence['checks']['injected_local_expiry_cleanup'] = stopped(binding, scoped, operation_id)
            fail_unless(session.schema.device is None, 'expired session reacquired native hardware')
            local.update(enabled=True, token=uuid4().hex)
            session = manager.open(local['token'])
            evidence['checks']['next_local_holder_rx'] = receive_native(manager, session, settings, args.serial)
            evidence['checks']['reservation_still_external'] = authority.active()
            fail_unless(authority.active(), 'external booking ended before qualification completed')
            manager.close_all(stop_admission=True)
            wait_for(lambda: not manager.cleanup_pending(args.device_id))
            session = None
    finally:
        release_stop.set()
        release_start.set()
        cleanup_errors = []
        def attempt(action):
            try:
                action()
            except BaseException as exc:
                cleanup_errors.append(type(exc).__name__)
        attempt(runtime.shutdown)
        attempt(lambda: wait_for(runtime.drained, timeout=40))
        if not runtime.drained():
            # Try the trusted per-owner process stop independently. Do not drop
            # its registry record while an adapter/native hook remains active.
            for scoped in scopes:
                attempt(scoped.stop)
        if manager is not None:
            attempt(lambda: manager.close_all(stop_admission=True))
            attempt(lambda: wait_for(lambda: not manager.cleanup_pending(args.device_id)))
        if runtime.drained():
            for scoped in scopes:
                attempt(scoped.close)
            attempt(runtime.close)
            attempt(supervisor.close)
            attempt(journal.close)
        else:
            cleanup_errors.append('UnresolvedOwnership')
        if cleanup_errors:
            evidence['cleanup_failures'] = cleanup_errors
            raise RuntimeError('qualification cleanup remained unresolved')


def observe_scope_population(scope):
    """Read positive kernel population evidence from the verified pinned scope.

    This is an observation, never release proof or permission to signal a PID.
    Persist only the count; cleanup still uses the independently verified cgroup.
    An empty or unreadable scope must not be described as a surviving process.
    """
    from remoterf_device_core.integrations.supervision import _read_control
    with scope._lock:
        scope._verify_identity()
        events = {}
        for line in _read_control(scope._fd, 'cgroup.events').splitlines():
            fields = line.split()
            if len(fields) != 2 or fields[0] in events or not fields[1].isdecimal():
                raise RuntimeError('invalid bounded cgroup population observation')
            events[fields[0]] = fields[1]
        if events.get('populated') not in {'0', '1'}:
            raise RuntimeError('missing or invalid kernel population observation')
        processes = _read_control(scope._fd, 'cgroup.procs').splitlines()
        if any(not pid.isdecimal() or int(pid) <= 0 for pid in processes):
            raise RuntimeError('invalid bounded cgroup process observation')
        scope._verify_identity()
        return {'scope_identity_verified': True, 'kernel_populated': int(events['populated']),
                'process_count': len(set(processes))}


def run_restart(args, authority, evidence):
    """Crash an owned harness child, then reconcile actual surviving processes."""
    from remoterf_device_core.integrations.ownership import Journal
    from remoterf_device_core.integrations.supervision import CgroupSupervisor
    from rrf_example_usrp_probe import configuration
    evidence['stage'] = 'abrupt_restart_recovery'
    state = args.state_dir / 'restart-case'
    command = [sys.executable, '-u', str(Path(__file__).absolute()), '--orphan-child',
               '--serial', args.serial, '--device-id', str(args.device_id),
               '--reservation-id', args.reservation_id, '--authority-command', str(args.authority_command),
               '--cgroup-root', str(args.cgroup_root), '--state-dir', str(state),
               '--evidence', str(state / 'child-evidence.json'), '--profile', args.profile,
               '--rate', str(args.rate), '--frequency', str(args.frequency), '--gain', str(args.gain),
               '--reservation-lock-fd', str(args._lock_fd)]
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, pass_fds=(args._lock_fd,))
    try:
        result = child.wait(timeout=90)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)
        result = None
    # Even a failed/timed-out child may have created durable owners. Open its
    # journals and reconcile only identities actually recorded by this runtime.
    journal = Journal(state / 'radio-owners')
    supervisor = CgroupSupervisor(args.cgroup_root, state / 'process-owners')
    manager = None
    try:
        owners = journal.records()
        rows = supervisor.describe()['owners']
        fail_unless(len(owners) == 1 and len(rows) == 1, 'restart did not retain exactly one physical/process owner')
        owner = next(iter(rows))
        radio_owner = next(iter(owners.values()))
        fail_unless(radio_owner.get('resources') == owner, 'radio and process owner identities did not match')
        scope = supervisor.reopen(owner)
        # A refusal of release can also mean uncertainty; it is not positive
        # proof that an application survived the controller crash. Observe the
        # matching kernel scope separately, but always attempt verified cleanup
        # even if this extra evidence read fails.
        observation = {'scope_identity_verified': False, 'kernel_populated': None, 'process_count': None}
        try:
            observation = observe_scope_population(scope)
        except BaseException as exc:
            observation['observation_error'] = type(exc).__name__
        was_alive = (observation['scope_identity_verified'] is True
                     and observation['kernel_populated'] == 1 and observation['process_count'] > 0)
        restart_evidence = {'scope_before_cleanup': observation, 'surviving_process_detected': was_alive}
        evidence['checks']['abrupt_restart_recovery'] = restart_evidence
        with ExitStack() as stack:
            manager, local, gate = configure_local(args, authority, stack, journal)
            expected_refusal(lambda: manager.open(local['token']))
            # This kills only the cgroup verified by boot/root/scope identity;
            # there is no PID file, process-name matching or broad kill command.
            fail_unless(scope.stop(timeout=10), 'surviving process cleanup was not verified')
            fail_unless(scope.resources_released(), 'recovered scope still owns a process')
            gate.release(radio_owner['lease'], verify=scope.resources_released)
            scope.close()
            restart_evidence.update(startup_native_open_refused=True,
                verified_cgroup_cleanup=True, journal_cleared=journal.records() == {})
            session = manager.open(local['token'])
            settings = configuration('receive', {'rate': args.rate, 'frequency': args.frequency, 'gain': args.gain})
            evidence['checks']['rx_after_abrupt_restart'] = receive_native(manager, session, settings, args.serial)
            manager.close_all(stop_admission=True)
            wait_for(lambda: not manager.cleanup_pending(args.device_id))
            manager = None
            fail_unless(result == 71 and (state / 'orphan-ready.json').is_file(),
                        'orphan child did not confirm real receive readiness before abrupt exit')
            fail_unless(was_alive, 'no surviving process remained to exercise restart cleanup')
    finally:
        if manager is not None:
            manager.close_all(stop_admission=True)
            wait_for(lambda: not manager.cleanup_pending(args.device_id))
        # Uncertain records deliberately make these close calls refuse. The
        # operator must inspect them before allowing any new hardware owner.
        supervisor.close()
        journal.close()


def write_evidence(path, evidence):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.dumps(evidence, sort_keys=True, indent=2) + '\n'
    temporary = path.with_name(path.name + '.tmp-' + uuid4().hex)
    with temporary.open('x') as stream:
        os.chmod(temporary, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--serial', required=True)
    result.add_argument('--device-id', type=int, required=True)
    result.add_argument('--reservation-id', required=True)
    result.add_argument('--authority-command', type=Path, required=True)
    result.add_argument('--cgroup-root', type=Path, required=True)
    result.add_argument('--state-dir', type=Path, required=True)
    result.add_argument('--evidence', type=Path, required=True)
    result.add_argument('--profile', default='usrp2901', choices=['usrp2901'])
    result.add_argument('--rate', type=float, default=1e6)
    result.add_argument('--frequency', type=float, default=100e6)
    result.add_argument('--gain', type=float, default=0)
    result.add_argument('--orphan-child', action='store_true', help=argparse.SUPPRESS)
    result.add_argument('--reservation-lock-fd', type=int, help=argparse.SUPPRESS)
    return result


def acquire_radio_lock(serial, inherited=None):
    """One canonical per-user serial lock, independent of chosen evidence paths."""
    import fcntl
    directory = Path('/tmp') / ('remoterf-qualification-' + str(os.getuid()))
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('qualification lock directory is not private')
    path = directory / ('usrp-' + serial.upper() + '.lock')
    fd = inherited
    if fd is None:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        actual = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino)):
            raise RuntimeError('qualification radio lock identity changed')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        if inherited is None:
            os.close(fd)
        raise


def main():
    args = parser().parse_args()
    from rrf_example_usrp_probe import serial_selector
    serial_selector({'serial': args.serial})
    for name in ('authority_command', 'cgroup_root', 'state_dir', 'evidence'):
        value = getattr(args, name)
        if not value.is_absolute():
            raise SystemExit(f'--{name.replace("_", "-")} must be absolute')
    if not args.authority_command.is_file() or not os.access(args.authority_command, os.X_OK):
        raise SystemExit('the authority provider must be an executable file')
    if args.device_id < 1 or not 1 <= len(args.reservation_id) <= 128:
        raise SystemExit('invalid reservation identity')
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any((args.state_dir / name).exists() for name in ('radio-owners', 'process-owners', 'restart-case')):
        raise SystemExit('state directory contains a previous run; reconcile it and use a new directory')
    if args.reservation_lock_fd is not None and not args.orphan_child:
        raise SystemExit('inherited reservation locks are for the isolated restart child only')
    args._lock_fd = acquire_radio_lock(args.serial, args.reservation_lock_fd)
    authority = None
    evidence = {'format': 'remoterf-usrp-handoff-evidence-1', 'passed': False,
        'serial': args.serial, 'device_id': args.device_id, 'reservation_id': args.reservation_id,
        'route': 'isolated_local_session_manager_and_runtime', 'transmit': False,
        'expiry_test': 'injected_local_revocation_under_real_external_reservation',
        'activation_changed': False, 'started_at': time.time(), 'checks': {}}
    try:
        authority = Authority(str(args.authority_command), args.reservation_id, args.device_id, args.serial)
        evidence['external_reservation'] = authority.last
        evidence['versions'] = {name: version(name) for name in
            ('remoterf-device-core', 'remoterf-server', 'remoterf-example-usrp-probe')}
        from remoterf_device_core.integrations import runtime, ownership, supervision
        from remoteRF_server.server import dynamic_v2
        import rrf_example_usrp_probe
        from rrf_example_usrp_probe import worker
        evidence['source_sha256'] = {module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (runtime, ownership, supervision, dynamic_v2, rrf_example_usrp_probe, worker)}
        evidence['source_sha256']['supervised_process_bootstrap'] = hashlib.sha256(
            Path(supervision.__file__).with_name('_process_worker.py').read_bytes()).hexdigest()
        evidence['source_sha256']['qualification_harness'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        run_lifecycle(args, authority, evidence, orphan=args.orphan_child)
        run_restart(args, authority, evidence)
        evidence['stage'] = 'complete'
        evidence['passed'] = True
    except BaseException as exc:
        evidence['failure'] = {'type': type(exc).__name__, 'code': getattr(exc, 'code', 'qualification_failed')}
        # Local operator gets a concise stage failure; evidence avoids native
        # exception text, paths and credentials. Unresolved journals are retained.
        print('Qualification failed; ownership records were retained for inspection.', file=sys.stderr)
    finally:
        if authority is not None:
            authority.close()
        evidence['finished_at'] = time.time()
        write_evidence(args.evidence, evidence)
        os.close(args._lock_fd)
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
