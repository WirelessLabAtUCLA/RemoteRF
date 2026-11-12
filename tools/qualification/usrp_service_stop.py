#!/usr/bin/env python3
"""Receive-only isolated service-lifetime qualification; no deployment activation.

The guardian must keep this delegated cgroup root alive after a failed payload.
Run only while holding a verified external reservation. This supplement uses an
internal Binding, never deployment enablement or the deployed RPC path.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from dataclasses import replace
import hashlib
import importlib
import importlib.util
from importlib.metadata import distribution, version
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

_spec = importlib.util.spec_from_file_location('_service_handoff_helpers', Path(__file__).with_name('usrp_handoff.py'))
handoff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handoff)


def private_environment(state):
    """Keep module-import side effects out of the installed service's config."""
    root = state / 'config'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ['XDG_CONFIG_HOME'] = str(root)
    os.environ['XDG_DATA_HOME'] = str(state / 'data')
    os.environ['XDG_CACHE_HOME'] = str(state / 'cache')
    os.environ['REMOTERF_CONFIG_DIR'] = str(root / 'remoterf')
    os.environ['GRPC_PORT'] = '50051'
    os.environ['CERT_PORT'] = '50052'
    os.environ['REMOTERF_BIND_HOST'] = '127.0.0.1'
    os.environ['REMOTERF_DIRECT'] = '0'


def source_identity():
    from remoteRF_server.server import integration_controller
    base = Path(integration_controller.__file__)
    extra = [base, base.with_name('dynamic_v2.py'), base.with_name('device_manager.py'),
             base.with_name('grpc_server.py')]
    import remoterf_device_core.integrations as integrations
    import rrf_example_usrp_probe
    probe = Path(rrf_example_usrp_probe.__file__).parent
    core = Path(integrations.__file__).parent.parent
    files = sorted(Path(integrations.__file__).parent.glob('*.py')) + extra + [
        core / 'hardware_access.py', core / 'native_lifetime.py',
        core / 'drivers/usrp/usrp_schema.py',
    ] + sorted(probe.glob('*.py')) + [probe / 'integration.json']
    return {'module_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
            'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'helper_sha256': hashlib.sha256(Path(handoff.__file__).read_bytes()).hexdigest(),
            'versions': {name: version(name) for name in
                         ('remoterf-device-core', 'remoterf-server', 'remoterf-example-usrp-probe')}}


def prewarm_native():
    """Load libraries, without opening hardware, before starting freshness TTLs."""
    started = time.monotonic()
    modules = {}
    for name in ('numpy', 'uhd', 'remoteRF_server.server.dynamic_v2'):
        module = importlib.import_module(name)
        modules[name] = str(Path(module.__file__).absolute())
    return {'seconds': time.monotonic() - started, 'modules': modules,
            'hardware_opened': False}


def authority_snapshot(authority):
    """Read-only diagnostics; this never refreshes or extends authority."""
    with getattr(authority, '_lock', nullcontext()):
        valid_until = getattr(authority, '_valid_until', None)
        last = dict(getattr(authority, 'last', None) or {})
    now, monotonic = time.time(), time.monotonic()
    return {'observed_at': now, 'cache_active': authority.active(),
            'cache_remaining_seconds': None if valid_until is None else max(0.0, valid_until - monotonic),
            'external_expires_at': last.get('expires_at'),
            'diagnostic_only': True}


def verify_empty_state(state, root):
    """Evidence only: never change a journal to make a check pass."""
    hardware = json.loads((state / 'ownership' / 'ownership.json').read_text())
    processes = json.loads((state / 'processes' / 'scopes.json').read_text())
    if type(hardware) is not dict or hardware or type(processes.get('scopes')) is not dict or processes['scopes']:
        raise RuntimeError('service left unresolved hardware/process ownership')
    info = root.stat()
    if (processes['boot'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            or processes['root'] != [info.st_dev, info.st_ino]):
        raise RuntimeError('delegated root identity changed before cleanup verification')
    if any(p.is_dir() for p in root.glob('rrf-*')):
        raise RuntimeError('owned cgroup remained after service cleanup')
    return {'hardware_records': 0, 'process_records': 0, 'owned_cgroups': 0,
            'root_identity_verified': True}


class TransportStop:
    """Forward to an actual unbound gRPC server; gate one stop for fault timing."""
    def __init__(self, server, args, manager, evidence):
        self.server, self.args, self.manager, self.evidence = server, args, manager, evidence

    def stop(self, grace):
        if not self.manager._stopping:
            raise RuntimeError('transport stopped before native admission was fenced')
        self.evidence['admission_closed_before_transport'] = True
        if self.args.child_case == 'pending_restart':
            handoff.write_evidence(self.args.state_dir / 'cleanup-ready.json', {'cleanup_started': True})
            handoff.wait_for(lambda: (self.args.state_dir / 'release-transport').exists(), timeout=10)
        return self.server.stop(grace)


def run_child(args, authority, evidence):
    """Actual controller/session/service cleanup, with no public listener/startup."""
    import grpc
    from remoterf_device_core.integrations import Manifest, Registry
    from remoterf_device_core.integrations.deployment import Configuration, scope_id
    from remoterf_device_core.integrations.ownership import Journal
    from remoterf_device_core.integrations.runtime import Runtime
    from remoterf_device_core.integrations.supervision import CgroupSupervisor
    from remoteRF_server.server import grpc_server as service_module
    from remoteRF_server.server.integration_controller import Controller, DeferredScope
    from rrf_example_usrp_probe import configuration

    definition = Manifest.from_file(distribution('remoterf-example-usrp-probe').locate_file(
        'rrf_example_usrp_probe/integration.json'))
    registry = Registry([definition], enabled={str(args.device_id): [definition.integration_id]})
    settings = configuration('receive', {'rate': args.rate, 'frequency': args.frequency, 'gain': args.gain})
    journal = Journal(args.state_dir / 'ownership')
    supervisor = CgroupSupervisor(args.cgroup_root, args.state_dir / 'processes')
    # Delegation, limits and kill support are checked before any radio opens.
    preflight = supervisor.create('preflight-' + uuid4().hex)
    if not preflight.resources_released():
        raise RuntimeError('empty preflight scope was not independently verifiable')
    preflight.close()
    controller = Controller(Configuration(registry=registry), state_dir=args.state_dir,
                            journal=journal, supervisor=supervisor)
    controller._runtime = Runtime(registry, capacity=16, workers=2, timeout=30)
    executor = ThreadPoolExecutor(max_workers=1)
    transport = grpc.server(executor)  # Deliberately no add_*_port call.
    transport.start()
    reexecutions = []
    with ExitStack() as stack:
        manager, local, gate = handoff.configure_local(args, authority, stack, journal)
        manager.integrations = controller
        stack.enter_context(patch.object(service_module, 'session_manager', manager))
        # The supplement has no legacy inventory, host sessions, direct paths,
        # federation agent or console. Only those unused surroundings are stubbed.
        stack.enter_context(patch.object(service_module, 'close_local_devices', lambda: None))
        stack.enter_context(patch.object(service_module, 'host_session_proxy', SimpleNamespace(close_all=lambda: None)))
        stack.enter_context(patch.object(service_module, 'direct_path', None))
        stack.enter_context(patch.object(service_module, 'start_agent', lambda: None))
        stack.enter_context(patch.object(service_module, 'welcome', lambda: None))
        stack.enter_context(patch.object(service_module, 'printf', lambda *a, **k: None))
        stack.enter_context(patch.object(service_module, '_exit_action',
                                        'restart' if args.child_case == 'pending_restart' else None))
        def reexec_forbidden():
            reexecutions.append(True)
            raise RuntimeError('service attempted to re-execute after termination')
        stack.enter_context(patch.object(service_module, '_reexec', reexec_forbidden))
        service = service_module.ServerRuntime(TransportStop(transport, args, manager, evidence),
            None, Event(), None, '127.0.0.1', 0, 0)
        stack.enter_context(patch.object(service_module, 'start_server_runtime', lambda: service))
        previous_handler = signal.getsignal(signal.SIGTERM)
        def run_until_termination():
            session = manager.open(local['token'])
            evidence['native_rx_before'] = handoff.receive_native(manager, session, settings, args.serial)
            generation = session.handles[session.root_handle].generation
            owner = scope_id(gate.physical_id, session.session_id, generation, args.child_case)
            scoped = DeferredScope(supervisor, owner, args.state_dir / 'work' / owner)
            binding = manager.integration_binding(session.session_id, work_dir=scoped.work_dir,
                                                  resources_released=scoped.resources_released)
            binding = replace(binding, selector={'serial': args.serial}, processes=scoped,
                prepare=scoped.prepare, resource_owner=owner, finalize=scoped.stop_if_prepared,
                resource_failure=scoped.resource_failure)
            # Internal harness setup, explicitly not a public RPC start.
            # Production enablement remains untouched.
            key = (session.session_id, generation, args.child_case)
            controller._bindings[key] = (binding, scoped)
            operation = controller._runtime.start(binding, definition.integration_id, 'receive', settings,
                                                   request_key=args.child_case)
            controller._operations[operation['operation_id']] = binding
            controller._starts[key] = binding
            def running():
                value = controller._runtime.status(binding, operation['operation_id'])
                if value['state'] == 'ERROR':
                    raise RuntimeError('real receive probe failed before service termination')
                return value if value['state'] == 'RUNNING' else None
            ready = handoff.wait_for(running, authority=authority)
            handoff.expected_refusal(lambda: manager.open(local['token']))
            evidence['application_rx'] = ready['details']
            handoff.write_evidence(args.state_dir / 'ready.json', {
                'pid': os.getpid(), 'case': args.child_case, 'application_rx': ready['details'],
                'ready_at': time.time(), 'transmit': False})
            if args.child_case == 'pending_restart':
                handoff.wait_for(lambda: (args.state_dir / 'request-restart').exists(), authority=authority)
            else:
                while not service.stop_event.wait(0.05):
                    if not authority.active():
                        raise RuntimeError('external reservation ended while awaiting termination')
        service.wait = run_until_termination
        try:
            service_module.headless_main()
            if not service._stopped or signal.getsignal(signal.SIGTERM) is not previous_handler:
                raise RuntimeError('service did not stop or restore its signal handler')
            if reexecutions:
                raise RuntimeError('pending restart survived termination')
            if manager.cleanup_pending(args.device_id):
                raise RuntimeError('native session cleanup remained pending')
            evidence.update(verify_empty_state(args.state_dir, args.cgroup_root))
            evidence.update(signal_handler_restored=True, reexec_calls=0, service_stopped=True)
        finally:
            # If ordinary cleanup fails, preserve ownership for the guardian's
            # identity-checked recovery. Do not erase or force-release a record.
            transport.stop(0).wait(timeout=3)
            executor.shutdown(wait=False)


def child_command(args, case, state):
    return [sys.executable, '-I', '-u', str(Path(__file__).absolute()),
        '--child-case', case, '--lock-fd', str(args._lock_fd), '--serial', args.serial,
        '--device-id', str(args.device_id), '--reservation-id', args.reservation_id,
        '--authority-command', str(args.authority_command), '--cgroup-root', str(args.cgroup_root),
        '--state-dir', str(state), '--evidence', str(state / 'child-evidence.json'),
        '--profile', args.profile,
        '--rate', str(args.rate), '--frequency', str(args.frequency), '--gain', str(args.gain)]


def receive_after(args, authority, state, progress):
    from remoterf_device_core.integrations.ownership import Journal
    from rrf_example_usrp_probe import configuration
    journal = Journal(state / 'ownership')
    manager = None
    started = time.monotonic()
    details = {'phase': 'before_configure', 'authority_before': authority_snapshot(authority)}
    progress['native_reopen'] = details
    try:
        if journal.records():
            raise RuntimeError('fresh native owner refused while a durable owner remains')
        with ExitStack() as stack:
            manager, local, _gate = handoff.configure_local(args, authority, stack, journal)
            details.update(phase='opening', authority_before_open=authority_snapshot(authority))
            session = manager.open(local['token'])
            details.update(phase='receiving', open_seconds=time.monotonic() - started,
                           authority_after_open=authority_snapshot(authority))
            settings = configuration('receive', {'rate': args.rate, 'frequency': args.frequency, 'gain': args.gain})
            result = handoff.receive_native(manager, session, settings, args.serial)
            manager.close_all(stop_admission=True)
            handoff.wait_for(lambda: not manager.cleanup_pending(args.device_id))
            manager = None
            details.update(phase='complete', seconds=time.monotonic() - started,
                           authority_after=authority_snapshot(authority))
            return result
    except BaseException:
        details.update(seconds=time.monotonic() - started,
                       authority_at_failure=authority_snapshot(authority))
        raise
    finally:
        if manager is not None:
            manager.close_all(stop_admission=True)
            handoff.wait_for(lambda: not manager.cleanup_pending(args.device_id))
        journal.close()


def run_case(args, authority, case, progress):
    state = args.state_dir / case
    state.mkdir(mode=0o700)
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    log_path = state / 'child.log'
    started = time.monotonic()
    with log_path.open('x') as log:
        os.chmod(log_path, 0o600)
        child = subprocess.Popen(child_command(args, case, state), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, env=env, close_fds=True, pass_fds=(args._lock_fd,))
        try:
            def ready():
                if child.poll() is not None:
                    raise RuntimeError('service child exited before real RX readiness')
                return (state / 'ready.json').is_file()
            handoff.wait_for(ready, authority=authority)
            marker = json.loads((state / 'ready.json').read_text())
            if (marker['pid'] != child.pid or marker['case'] != case
                    or marker['application_rx'].get('serial') != args.serial
                    or marker['application_rx'].get('received', 0) < 4096):
                raise RuntimeError('service child readiness did not match the reserved radio')
            if case == 'pending_restart':
                (state / 'request-restart').touch()
                handoff.wait_for(lambda: (state / 'cleanup-ready.json').is_file(), authority=authority)
            signal_at = time.monotonic()
            child.send_signal(signal.SIGTERM)  # Exact Popen child, never a PID from a file.
            if case == 'pending_restart':
                time.sleep(0.05)
                child.send_signal(signal.SIGTERM)
                (state / 'release-transport').touch()
            result = child.wait(timeout=15)
            stop_seconds = time.monotonic() - signal_at
            child_result = json.loads((state / 'child-evidence.json').read_text())
            # Preserve the independently observed child cleanup even if the
            # subsequent fresh native open is refused or fails.
            progress.update(stop_seconds=stop_seconds, signal='SIGTERM', child_exit=result,
                            child_passed=child_result.get('passed'), child_checks=child_result.get('checks', {}))
            if result != 0 or child_result.get('passed') is not True:
                raise RuntimeError('isolated service termination failed qualification')
            proof = verify_empty_state(state, args.cgroup_root)
            progress.update(cleanup=proof, phase='next_holder_rx')
            if not authority.active():
                raise RuntimeError('reservation ended before next-holder receive')
            native = receive_after(args, authority, state, progress)
            verify_empty_state(state, args.cgroup_root)
            progress.update(total_seconds=time.monotonic() - started, native_rx_after=native, phase='complete')
            return progress
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            # A failed child can leave radio/process journals. The caller's
            # guardian must preserve this root until the operator reconciles them.


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('serial', 'reservation-id'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--device-id', type=int, required=True)
    for name in ('authority-command', 'cgroup-root', 'state-dir', 'evidence'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--profile', default='usrp2901', choices=['usrp2901'])
    p.add_argument('--rate', type=float, default=1e6)
    p.add_argument('--frequency', type=float, default=100e6)
    p.add_argument('--gain', type=float, default=0)
    p.add_argument('--child-case', choices=['running', 'pending_restart'], help=argparse.SUPPRESS)
    p.add_argument('--lock-fd', type=int, help=argparse.SUPPRESS)
    return p


def main():
    args = parser().parse_args()
    from rrf_example_usrp_probe import serial_selector
    serial_selector({'serial': args.serial})
    for name in ('authority_command', 'cgroup_root', 'state_dir', 'evidence'):
        if not getattr(args, name).is_absolute():
            raise SystemExit('qualification paths must be absolute')
    if args.device_id < 1 or not args.authority_command.is_file() or not os.access(args.authority_command, os.X_OK):
        raise SystemExit('invalid device or authority provider')
    if bool(args.child_case) != (args.lock_fd is not None):
        raise SystemExit('only a qualification child can inherit the radio lock')
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=bool(args.child_case))
    if any((args.state_dir / name).exists() for name in ('ownership', 'processes', 'running', 'pending_restart')):
        raise SystemExit('previous state must be reconciled; use a fresh qualification directory')
    private_environment(args.state_dir)
    args._lock_fd = handoff.acquire_radio_lock(args.serial, args.lock_fd)
    evidence = {'format': 'remoterf-usrp-service-stop-evidence-1', 'passed': False,
        'serial': args.serial, 'device_id': args.device_id, 'reservation_id': args.reservation_id,
        'route': 'isolated_internal_binding_and_actual_server_lifetime_no_listener',
        'transmit': False, 'activation_changed': False,
        'public_rpc_or_startup_qualified': False, 'started_at': time.time(), 'checks': {}}
    authority = None
    try:
        evidence['source_identity'] = source_identity()
        evidence['native_library_prewarm'] = prewarm_native()
        authority = handoff.Authority(str(args.authority_command), args.reservation_id, args.device_id, args.serial)
        evidence['external_reservation'] = authority.last
        if args.child_case:
            run_child(args, authority, evidence['checks'])
        else:
            for case in ('running', 'pending_restart'):
                evidence['stage'] = case
                progress = evidence['checks'].setdefault(case, {})
                run_case(args, authority, case, progress)
        evidence.update(passed=True, stage='complete')
    except BaseException as exc:
        evidence['failure'] = {'type': type(exc).__name__, 'code': getattr(exc, 'code', 'qualification_failed')}
        evidence['failure']['trace'] = [
            {'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
            for frame in traceback.extract_tb(exc.__traceback__)[-8:]]
        evidence['recovery_required_before_guardian_release'] = True
        print('Service qualification failed. Preserve delegation and inspect recorded ownership before reuse.',
              file=sys.stderr)
    finally:
        if authority is not None:
            authority.close()
        evidence['finished_at'] = time.time()
        handoff.write_evidence(args.evidence, evidence)
        os.close(args._lock_fd)
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
