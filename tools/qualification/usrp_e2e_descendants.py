#!/usr/bin/env python3
"""Real receiver-descendant cleanup evidence; no receipt or activation is created.

The unchanged probe runs as a child of a qualification-only required wrapper.
Kill only that wrapper, observe the receiving descendant behind a held cleanup
barrier, then let the stock adapter/runtime perform verified scope cleanup.
"""
from __future__ import annotations

import argparse
import ctypes
from dataclasses import replace
import hashlib
import importlib.util
from importlib.metadata import distribution
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event, Thread
import time
import traceback
from uuid import uuid4


def _pidfd_library():
    if sys.platform != 'linux':
        raise RuntimeError('Linux pidfd supervision is required')
    library = ctypes.CDLL(None, use_errno=True)
    opened, signaled = library.pidfd_open, library.pidfd_send_signal
    opened.argtypes, opened.restype = [ctypes.c_int, ctypes.c_uint], ctypes.c_int
    signaled.argtypes, signaled.restype = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint], ctypes.c_int
    return library


def pidfd_supported():
    if sys.platform != 'linux': return False
    if hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'): return True
    try: _pidfd_library(); return True
    except (OSError, AttributeError, RuntimeError): return False


def pidfd_open(pid):
    if hasattr(os, 'pidfd_open'): return os.pidfd_open(pid)
    result = _pidfd_library().pidfd_open(pid, 0)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def pidfd_send_signal(fd, sig):
    if hasattr(signal, 'pidfd_send_signal'): return signal.pidfd_send_signal(fd, sig)
    result = _pidfd_library().pidfd_send_signal(fd, sig, None, 0)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def wrapper(serial, configuration):
    """Fixed receive-only target; no caller-supplied executable or environment."""
    from rrf_example_usrp_probe import configuration as validate, serial_selector
    serial_selector({'serial': serial})
    value = validate('receive', json.loads(configuration))
    child = subprocess.Popen([sys.executable, '-I', '-u', '-m', 'rrf_example_usrp_probe.worker',
        '--serial', serial, '--configuration', json.dumps(value, separators=(',', ':'))],
        stdin=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    # stdout/stderr are inherited directly: the supervisor still drains them
    # after this parent dies. The real receiver remains in the same cgroup.
    return child.wait()


def load_helpers():
    spec = importlib.util.spec_from_file_location('_descendant_cold_helpers',
        Path(__file__).with_name('usrp_cold_start.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def root_proof(root):
    lines = Path('/proc/self/cgroup').read_text().splitlines()
    rows = [line.split('::', 1)[1] for line in lines if line.startswith('0::')]
    if len(rows) != 1:
        raise RuntimeError('unified delegated cgroup is required')
    current = Path('/sys/fs/cgroup') / rows[0].lstrip('/')
    if current.name not in {'manager', 'runner'} or current.parent.resolve() != root.resolve():
        raise RuntimeError('qualifier is outside the guarded qualifier subgroup')
    if (root / 'cgroup.type').read_text().strip() != 'domain':
        raise RuntimeError('guarded root must be a domain')
    return {'root': str(root), 'identity': [root.stat().st_dev, root.stat().st_ino],
            'qualifier_subgroup': current.name,
            'guardian_required_until_independent_release': True}


def kernel_processes(scope):
    from remoterf_device_core.integrations.supervision import _read_control
    with scope._lock:
        scope._verify_identity()
        raw = _read_control(scope._fd, 'cgroup.procs').splitlines()
        if any(not v.isdecimal() or int(v) <= 0 for v in raw):
            raise RuntimeError('invalid bounded process observation')
        pids = {int(v) for v in raw}
        events = dict(line.split() for line in _read_control(scope._fd, 'cgroup.events').splitlines())
        if events.get('populated') not in {'0', '1'}:
            raise RuntimeError('invalid kernel population observation')
        scope._verify_identity()
    return pids, int(events['populated'])


def usb_node(serial):
    usb = []
    for file in Path('/sys/bus/usb/devices').glob('*/serial'):
        try:
            if file.read_text().strip() == serial: usb.append(file.parent)
        except OSError:
            pass
    if len(usb) != 1:
        raise RuntimeError('reserved USB serial is not uniquely observable')
    device = usb[0]
    node = Path('/dev/bus/usb') / ('%03d' % int((device / 'busnum').read_text())) / ('%03d' % int((device / 'devnum').read_text()))
    return node


def receiver_observation(scope, serial, *, minimum_processes):
    pids, populated = kernel_processes(scope)
    node = usb_node(serial)
    query = subprocess.run(['fuser', str(node)], capture_output=True, timeout=2)
    fields = query.stdout.decode('ascii').split()
    if query.returncode != 0 or not fields or any(not v.isdecimal() for v in fields):
        raise RuntimeError('native USB ownership was not positively observed')
    holders = {int(v) for v in fields}
    pids_after, populated_after = kernel_processes(scope)
    if (populated != 1 or populated_after != 1 or len(pids_after) < minimum_processes
            or not holders <= pids or not holders <= pids_after):
        raise RuntimeError('receiver ownership is outside the verified process scope')
    # PIDs are observation only. They are never used as authority to signal.
    return {'scope_identity_verified': True, 'kernel_populated': populated_after,
            'process_count': len(pids_after), 'usb_owner_count': len(holders),
            'usb_owners_inside_scope': True, 'usb_device_node': str(node)}


class DescendantScope:
    def __init__(self, supervisor, owner, work, script):
        self.supervisor, self.owner, self.work_dir, self.script = supervisor, owner, work, script
        self.scope = None
        self.pidfd = None
        self.process = None
        self.process_id = None
        self.attempted = False

    def prepare(self):
        self.attempted = True
        self.work_dir.mkdir(mode=0o700, parents=True)
        self.scope = self.supervisor.create(self.owner)

    def launch(self, argv, *, env, cwd, timeout=3, required_while_running=False):
        if (self.scope is None or self.process is not None or not required_while_running
                or len(argv) != 9 or argv[1:5] != ['-I', '-u', '-m', 'rrf_example_usrp_probe.worker']
                or argv[5] != '--serial' or argv[7] != '--configuration'):
            raise RuntimeError('unexpected probe launch specification')
        command = [argv[0], '-I', '-u', str(self.script), '--worker-wrapper', argv[6], argv[8]]
        process_id = self.scope.launch(command, env=env, cwd=cwd, timeout=timeout,
                                      required_while_running=True)
        with self.scope._lock:
            self.scope._verify_identity()
            process = self.scope._processes[process_id]['popen']
            if process.poll() is not None:
                raise RuntimeError('required wrapper exited before pidfd capture')
            pidfd = pidfd_open(process.pid)
            if process.poll() is not None:
                os.close(pidfd)
                raise RuntimeError('required wrapper exited during pidfd capture')
            pids, populated = kernel_processes(self.scope)
            if process.pid not in pids or populated != 1:
                os.close(pidfd)
                raise RuntimeError('required wrapper was outside its verified scope')
            self.process, self.pidfd, self.process_id = process, pidfd, process_id
        return process_id

    def kill_required_wrapper(self):
        if self.pidfd is None or self.process is None or self.process.poll() is not None:
            raise RuntimeError('required wrapper is not alive for fault injection')
        with self.scope._lock:
            self.scope._verify_identity()
            pids, populated = kernel_processes(self.scope)
            if self.process.pid not in pids or populated != 1:
                raise RuntimeError('required wrapper identity cannot be verified')
            pidfd_send_signal(self.pidfd, signal.SIGKILL)

    def resources_released(self):
        return self.scope.resources_released() if self.scope is not None else not self.attempted

    def resource_failure(self):
        return self.scope.resource_failure() if self.scope is not None else None

    def snapshot(self): return self.scope.snapshot()
    def log_tail(self, *args): return self.scope.log_tail(*args)
    def stop(self, *args, **kwargs): return self.scope.stop(*args, **kwargs) if self.scope else not self.attempted
    def close(self):
        if self.scope is not None: self.scope.close()
        if self.pidfd is not None:
            os.close(self.pidfd); self.pidfd = None


def run(args, evidence, cold):
    from remoterf_device_core.integrations import Manifest, Registry
    from remoterf_device_core.integrations.ownership import Journal
    from remoterf_device_core.integrations.runtime import Runtime
    from remoterf_device_core.integrations.supervision import CgroupSupervisor
    from remoteRF_server.server import device_manager as dm, dynamic_v2 as v2
    from remoteRF_server.server.reservation import reservation_handler as reservation
    from rrf_example_usrp_probe import configuration
    helpers = cold.helpers
    evidence['source_identity'] = helpers.source_identity(args.expected_framework_hash)
    evidence['source_identity']['descendant_harness_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    journal = Journal(args.state_dir / 'ownership')
    supervisor = CgroupSupervisor(args.cgroup_root, args.state_dir / 'processes')
    dm.hardware_access.configure({args.device_id: 'usrp:serial:' + args.serial}, journal)
    manager = v2.session_manager
    definition = Manifest.from_file(distribution('remoterf-example-usrp-probe').locate_file('rrf_example_usrp_probe/integration.json'))
    registry = Registry([definition], enabled={str(args.device_id): [definition.integration_id]})
    settings = configuration('receive', {'rate': 1e6, 'frequency': 100e6, 'gain': 0})
    runtime = Runtime(registry, capacity=16, workers=2, timeout=30)
    scope = DescendantScope(supervisor, uuid4().hex, args.state_dir / 'work', Path(__file__).absolute())
    entered_stop, release_stop, end_calendar = Event(), Event(), Event()
    calendar_errors = []
    def tick():
        while not end_calendar.wait(1):
            try: reservation.update_devices()
            except BaseException as exc:
                calendar_errors.append(type(exc).__name__); return
    calendar = Thread(target=tick, daemon=True)
    create = registry.create
    def held_create(integration, context):
        adapter = create(integration, context)
        normal_stop = adapter.stop
        def held_stop():
            entered_stop.set()
            if not release_stop.wait(10):
                raise RuntimeError('qualification cleanup barrier exceeded its bound')
            normal_stop()
        adapter.stop = held_stop
        return adapter
    registry.create = held_create
    try:
        dm.reload_devices()
        username = cold.create_account(args, reservation)
        token, booking = cold.reserve(args, reservation, username, 600)
        evidence['normal_auth'] = cold.normal_auth_snapshot(dm, v2, token, args.device_id)
        calendar.start()
        evidence['phase'] = 'native_receive_before'
        session = manager.open(token)
        evidence['schema_hash'] = session.schema_v2['schema_hash']
        evidence['uhd_version'] = session.capabilities['uhd_version']
        evidence['adapter_id'], evidence['adapter_version'] = definition.integration_id, definition.version
        evidence['checks']['native_rx_before'] = helpers.handoff.receive_native(manager, session, settings, args.serial)
        binding = manager.integration_binding(session.session_id, work_dir=scope.work_dir,
                                              resources_released=scope.resources_released)
        binding = replace(binding, selector={'serial': args.serial}, processes=scope, prepare=scope.prepare,
            resource_owner=scope.owner, finalize=scope.stop, resource_failure=scope.resource_failure)
        operation = runtime.start(binding, definition.integration_id, 'receive', settings, request_key='descendant-fault')
        def ready():
            status = runtime.status(binding, operation['operation_id'])
            if status['state'] == 'ERROR': raise RuntimeError('real receiver failed before fault injection')
            return status if status['state'] == 'RUNNING' else None
        started = helpers.handoff.wait_for(ready)
        evidence['checks']['application_rx'] = started['details']
        evidence['checks']['before_parent_death'] = receiver_observation(scope.scope, args.serial, minimum_processes=2)
        evidence['phase'] = 'required_parent_death'
        scope.kill_required_wrapper()
        helpers.handoff.wait_for(entered_stop.is_set, timeout=10)
        if scope.process.poll() != -signal.SIGKILL: raise RuntimeError('wrapper did not exit by the injected signal')
        evidence['checks']['receiver_after_parent_death'] = receiver_observation(scope.scope, args.serial, minimum_processes=1)
        if not dm.hardware_access.gate(args.device_id).snapshot()['blocked']:
            raise RuntimeError('radio ownership was released while a descendant held USB')
        helpers.handoff.expected_refusal(lambda: manager.open(token))
        evidence['checks']['native_open_refused_while_descendant_alive'] = True
        release_stop.set()
        helpers.handoff.wait_for(lambda: runtime.drained(binding))
        outcome = runtime.status(binding, operation['operation_id'])
        if outcome['error'] != 'process_exited' or outcome['released'] is not True:
            raise RuntimeError('autonomous required-parent failure cleanup did not finish')
        if not scope.resources_released(): raise RuntimeError('descendant scope still owns resources')
        scope.close()
        evidence['checks']['cleanup_descendants'] = {'passed': True, 'automatic_required_parent_failure': True,
            'health_action_used': False, 'verified_cgroup_cleanup': True, 'journal_empty': journal.records() == {},
            'outcome': outcome}
        evidence['checks']['native_rx_after'] = helpers.handoff.receive_native(manager, session, settings, args.serial)
        if journal.records(): raise RuntimeError('hardware ownership journal remains nonempty')
        evidence['phase'] = 'complete'
    finally:
        release_stop.set()
        errors = []
        def attempt(fn):
            try: fn()
            except BaseException as exc: errors.append(type(exc).__name__)
        attempt(runtime.shutdown)
        attempt(lambda: helpers.handoff.wait_for(runtime.drained, timeout=40))
        if not runtime.drained(): attempt(scope.stop)
        attempt(lambda: manager.close_all(stop_admission=True))
        attempt(lambda: cold.wait_until(lambda: not manager.cleanup_pending(args.device_id)))
        end_calendar.set()
        if calendar.ident is not None: calendar.join(timeout=5)
        if calendar.is_alive(): errors.append('CalendarStillRunning')
        for booking in reservation.grab_all_reservations_server():
            attempt(lambda booking=booking: reservation.remove_reservation(res_id=booking))
        attempt(reservation.update_devices)
        attempt(dm.close_local_devices)
        attempt(lambda: cold.wait_clean(args, dm, manager, []))
        if runtime.drained():
            attempt(scope.close); attempt(runtime.close); attempt(supervisor.close); attempt(journal.close)
        if errors or calendar_errors:
            evidence['cleanup_errors'] = errors + calendar_errors
            raise RuntimeError('qualification cleanup was not fully verified')
        evidence['cleanup'] = helpers.verify_empty_state(args.state_dir, args.cgroup_root)
        evidence['private_calendar_empty'] = not reservation.grab_all_reservations_server()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('serial', 'reservation-id', 'expected-framework-hash'): parser.add_argument('--' + name, required=True)
    parser.add_argument('--device-id', type=int, required=True)
    for name in ('state-dir', 'evidence', 'cgroup-root', 'authority-command'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--lock-fd', type=int)
    args = parser.parse_args()
    for name in ('state_dir', 'evidence', 'cgroup_root', 'authority_command'):
        if not getattr(args, name).is_absolute(): parser.error('qualification paths must be absolute')
    if not re.fullmatch(r'[A-Za-z0-9]{1,64}', args.serial): parser.error('one safe explicit USRP serial is required')
    if args.device_id < 1 or not pidfd_supported():
        parser.error('Linux pidfd supervision and a positive device id are required')
    cold = load_helpers()
    args.backend = 'usrp'
    cold.configure_private(args)
    evidence = {'format': 'remoterf-receiver-descendant-qualification-1', 'passed': False,
        'started_at': time.time(), 'serial': args.serial, 'device_id': args.device_id,
        'reservation_id': args.reservation_id, 'transmit': False, 'activation_changed': False,
        'qualification_receipt_created': False, 'route': 'isolated_normal_local_auth_internal_runtime',
        'fault_injection': 'required_wrapper_parent_exits_real_receiver_descendant_keeps_usb', 'checks': {}}
    lock = None
    try:
        evidence['guarded_root'] = root_proof(args.cgroup_root)
        lock = cold.helpers.handoff.acquire_radio_lock(args.serial, args.lock_fd)
        evidence['external_booking_before'] = cold.external_proof(args)
        run(args, evidence, cold)
        evidence['external_booking_after'] = cold.external_proof(args)
        evidence['passed'] = True
    except BaseException as exc:
        evidence['failure'] = {'type': type(exc).__name__, 'code': getattr(exc, 'code', None),
            'trace': [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
                      for f in traceback.extract_tb(exc.__traceback__)[-8:]]}
        evidence['independent_cleanup_required'] = True
    finally:
        evidence['finished_at'] = time.time()
        cold.helpers.handoff.write_evidence(args.evidence, evidence)
        if lock is not None: os.close(lock)
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--worker-wrapper':
        raise SystemExit(wrapper(sys.argv[2], sys.argv[3]))
    raise SystemExit(main())
