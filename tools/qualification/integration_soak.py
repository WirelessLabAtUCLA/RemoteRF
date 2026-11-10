#!/usr/bin/env python3
"""Bounded, fake-radio scheduling soak; never a hardware/activation receipt.

Uses real SessionManager, Controller, Runtime, native admission and durable
ownership journals. Radio methods, reservation authority, adapter qualification
and process scopes are explicit in-memory fixtures. Run only in an isolated
process/environment: the fixture replaces module-local authority callbacks.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
import errno
import gc
from hashlib import sha256
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import random
from threading import Event, Lock, Thread, enumerate as threads
import time
from unittest.mock import patch


class Samples:
    """Bounded deterministic reservoir; percentiles are estimates, counts exact."""
    def __init__(self, capacity=8192):
        self.values, self.count, self.total, self.maximum = [], 0, 0.0, 0.0
        self.capacity, self.random = capacity, random.Random(91271)

    def add(self, value):
        self.count += 1
        self.total += value
        self.maximum = max(self.maximum, value)
        if len(self.values) < self.capacity:
            self.values.append(value)
        else:
            index = self.random.randrange(self.count)
            if index < self.capacity:
                self.values[index] = value

    def report(self):
        values = sorted(self.values)
        percentile = lambda p: values[min(len(values) - 1, math.ceil(p * len(values)) - 1)] if values else 0
        return dict(count=self.count, reservoir_count=len(values), percentile_estimator='uniform_bounded_reservoir',
                    p50_ms=percentile(.50) * 1000, p95_ms=percentile(.95) * 1000,
                    p99_ms=percentile(.99) * 1000, max_ms=self.maximum * 1000)


class Metrics:
    def __init__(self):
        self.lock, self.phase = Lock(), 'warmup'
        self.timings, self.expected, self.failures, self.cycles = {}, Counter(), [], Counter()

    def timed(self, kind, action):
        started = time.monotonic()
        try:
            return action()
        finally:
            with self.lock:
                self.timings.setdefault(self.phase + ':' + kind, Samples()).add(time.monotonic() - started)

    def expect(self, action, codes):
        try:
            action()
        except Exception as exc:
            code = getattr(exc, 'details', {}).get('integration_code') or getattr(exc, 'code', None)
            if code not in codes:
                raise AssertionError(f'unexpected refusal type {type(exc).__name__}/{code}') from exc
            with self.lock:
                self.expected[str(code)] += 1
        else:
            raise AssertionError('operation unexpectedly admitted')

    def fail(self, exc):
        with self.lock:
            if len(self.failures) < 32:
                self.failures.append({'type': type(exc).__name__, 'message': str(exc)[:500]})


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.001)
    raise AssertionError('bounded transition did not complete')


def resources():
    status = Path('/proc/self/status').read_text()
    rss = next(int(line.split()[1]) * 1024 for line in status.splitlines() if line.startswith('VmRSS:'))
    return {'rss_bytes': rss, 'threads': len(threads()), 'fds': len(os.listdir('/proc/self/fd'))}


class Scope:
    def __init__(self, supervisor, owner):
        self.supervisor, self.owner, self.alive = supervisor, owner, False
        self.failure = None
    def resources_released(self):
        return not self.alive
    def resource_failure(self):
        return self.failure
    def stop(self):
        self.alive = False
        return True
    def close(self):
        assert not self.alive
        with self.supervisor.lock:
            self.supervisor.scopes.pop(self.owner, None)


class Supervisor:
    def __init__(self):
        self.scopes, self.lock = {}, Lock()
    def create(self, owner):
        with self.lock:
            scope = Scope(self, owner)
            self.scopes[owner] = scope
            return scope
    def close(self):
        assert not self.scopes


class Adapter:
    def __init__(self, context):
        self.context, self.mode = context, ''
        self.entered, self.release = Event(), Event()
        self.stopped, self.calls = False, 0
    def validate(self, profile, configuration):
        assert profile == 'fake' and set(configuration) == {'mode'}
        return configuration
    def start(self, profile, configuration):
        self.mode = configuration['mode']
        self.context.processes.scope.alive = True
        if self.mode == 'cancel_start':
            self.entered.set()
            assert self.release.wait(10), 'test failed to release held start'
    def status(self):
        return {'fake_radio': True, 'calls': self.calls}
    def call(self, action, parameters):
        self.calls += 1
        if action == 'hold':
            self.entered.set()
            assert self.release.wait(10), 'test failed to release held action'
        if action == 'fail':
            raise RuntimeError('injected adapter exception')
        return {'sequence': self.calls}
    def stop(self):
        if self.mode == 'hold_stop':
            self.entered.set()
            assert self.release.wait(10), 'test failed to release held stop'
        self.stopped = True
        self.context.processes.scope.alive = False


class FakeNative:
    def __init__(self, sample):
        self.device, self.sample = object(), sample
    def build_session_metadata_v2(self, **kwargs):
        return {'uhd_version': 'SOAK_FAKE', 'fake_hardware': True}
    def invoke_native(self, method, args, positional_args=None):
        assert self.device is not None, 'native call after release'
        if method == 'recv_num_samps':
            return self.sample
        if method == 'send_waveform':
            return len(args['waveform_proto'])
        if method == 'get_time_now':
            return 1.5
        raise AssertionError('unexpected native method')
    def close_native_device(self):
        self.device = None


class Inventory:
    device = None
    def __init__(self, schema, sample):
        self.schema, self.sample = schema, sample
    def get_idl_v2(self):
        return self.schema
    def create_session_schema(self):
        return FakeNative(self.sample)


class Rig:
    def __init__(self, directory, metrics):
        import numpy as np
        from remoterf_device_core.hardware_access import AccessTable
        from remoterf_device_core.integrations import Manifest, Registry
        from remoterf_device_core.integrations import deployment
        from remoterf_device_core.integrations.deployment import Configuration, REQUIRED_CHECKS
        from remoterf_device_core.integrations.ownership import Journal
        from remoteRF_server.server import dynamic_v2 as v2, integration_controller as control
        from remoteRF_server.drivers.usrp.uhd_manifest import build_usrp_schema_v2
        self.metrics, self.stack, self.directory = metrics, ExitStack(), directory
        self.lock, self.current, self.adapters = Lock(), {}, {}
        self.stop, self.workers = Event(), []
        self.sample = np.ones(1024, dtype=np.complex64)
        schema = build_usrp_schema_v2()
        self.journal = Journal(directory / 'ownership')
        self.access = AccessTable()
        self.access.configure({gid: f'usrp:serial:SOAK{gid}' for gid in range(4)}, self.journal)
        inventory = {gid: Inventory(schema, self.sample) for gid in range(4)}
        tokens = {f'fake-{gid}': gid for gid in range(4)}
        self.stack.enter_context(patch.object(v2, 'hardware_access', self.access))
        self.stack.enter_context(patch.object(v2, 'resolve_gid_for_token', tokens.get))
        self.stack.enter_context(patch.object(v2, 'token_matches_device', lambda token, gid: tokens.get(token) == gid))
        self.stack.enter_context(patch.object(v2, 'get_local_device_context', lambda gid:
            {'schema': inventory[gid], 'dtype': 'usrp', 'ident': f'SOAK{gid}'} if gid in inventory else None))
        self.manager = v2.SessionManager()
        self.raw_sessions = [self.manager.open(f'fake-{gid}') for gid in (0, 1)]
        definition = Manifest.from_dict(dict(id='org.remoterf.soak_fixture', version='0.0.0', contract_version='0.1',
            factory='soak_fixture:create', device_types=['usrp'], profiles=['fake'], actions=['stats', 'hold', 'fail']))
        self.definition = definition
        registry = Registry([definition], enabled={str(gid): [definition.integration_id] for gid in (2, 3)})
        # Explicit fake test receipts, held in memory only; never written to an
        # activation file or represented as hardware evidence.
        receipts = tuple(dict(qualification_version=1, serial=f'SOAK{gid}', integration_id=definition.integration_id,
            adapter_version=definition.version, adapter_hash='a' * 64, profile='fake',
            schema_hash=self.raw_sessions[0].schema_v2['schema_hash'], uhd_version='SOAK_FAKE', framework_hash='fake',
            passed=True, checks={key: True for key in REQUIRED_CHECKS}, run_id='FAKE-SOAK-NOT-QUALIFICATION') for gid in (2, 3))
        config = Configuration(registry=registry, enabled=True,
            devices={gid: {'serial': f'SOAK{gid}', 'physical_id': f'usrp:serial:SOAK{gid}'} for gid in (2, 3)},
            receipts=receipts, adapter_hashes={definition.integration_id: 'a' * 64})
        self.stack.enter_context(patch.object(deployment, 'adapter_digest', lambda _: 'a' * 64))
        self.supervisor = Supervisor()
        self.controller = control.Controller(config, state_dir=directory, journal=self.journal,
                                             supervisor=self.supervisor, framework_hash='fake')
        self.manager.integrations = self.controller
        def create(integration, context):
            adapter = Adapter(context)
            with self.lock:
                self.adapters[int(context.device_id)] = adapter
            return adapter
        registry.create = create

    def generic(self, session, method, request):
        return self.metrics.timed('status' if method == 'status' else 'generic', lambda:
            self.manager.invoke(session.session_id, session.root_handle, 'integration_' + method, {'request': request})[0])

    def raw(self, session, *, independent=False):
        methods = [('get_time_now', {}), ('recv_num_samps', {'num_samps': 1024, 'freq': 100e6}),
            ('send_waveform', {'waveform_proto': self.sample, 'duration': .001, 'freq': 100e6})]
        for name, request in methods:
            result = self.metrics.timed(('raw_' if independent else 'recovery_') + name, lambda:
                self.manager.invoke(session.session_id, session.root_handle, name, request)[0])
            if name == 'get_time_now':
                assert result == 1.5
            elif name == 'recv_num_samps':
                assert result.shape == (1024,) and result[0] == 1 + 0j
            else:
                assert result == 1024

    def cycle(self, gid, iteration):
        modes = ['normal', 'cancel_start', 'hold_action', 'refused', 'action_error', 'hold_stop', 'expiry',
                 'worker_exit', 'monitor_failure']
        mode = modes[iteration % len(modes)]
        session = self.manager.open(f'fake-{gid}')
        adapter = None
        request = {'integration_id': self.definition.integration_id, 'profile': 'fake', 'configuration': {'mode': mode},
                   'request_key': 'cycle', 'generation': session.handles[session.root_handle].generation}
        operation = self.generic(session, 'start', request)
        operation_id = operation['operation_id']
        with self.lock:
            self.current[gid] = (session, operation_id)
        try:
            assert self.generic(session, 'start', request)['operation_id'] == operation_id
            adapter = wait_for(lambda: self.adapters.get(gid) if self.adapters.get(gid) is not None
                               and self.adapters[gid].context.generation == operation['generation']
                               and self.adapters[gid].context.work_dir.name == self.controller._operations[operation_id].work_dir.name else None)
            status = lambda: self.generic(session, 'status', {'operation_id': operation_id})
            if mode == 'cancel_start':
                assert adapter.entered.wait(2)
                self.generic(session, 'stop', {'operation_id': operation_id})
                assert self.access.gate(gid).snapshot()['blocked'] and not adapter.stopped
                adapter.release.set()
            elif mode in {'worker_exit', 'monitor_failure'}:
                wait_for(lambda: status()['state'] == 'RUNNING')
                scope = adapter.context.processes.scope
                failure = 'process_exited' if mode == 'worker_exit' else 'resource_monitor_failed'
                scope.failure = failure
                if mode == 'worker_exit':
                    scope.alive = False
                # Cached process exit must trigger cleanup without a health
                # action or adapter.status invocation from the watcher.
                wait_for(lambda: status()['released'])
                assert status()['error'] == failure and adapter.calls == 0
                assert adapter.stopped and not scope.alive
            else:
                wait_for(lambda: status()['state'] == 'RUNNING')
                self.metrics.expect(lambda: self.manager.open(f'fake-{gid}'), {'device_owned', 'device_busy'})
                if mode == 'refused':
                    self.metrics.expect(lambda: self.generic(session, 'call', {'operation_id': operation_id,
                        'action': 'not_declared', 'parameters': {}, 'request_key': 'bad'}), {'unsupported_action'})
                if mode in {'hold_action', 'expiry', 'action_error'}:
                    action_name = 'fail' if mode == 'action_error' else 'hold'
                else:
                    action_name = 'stats'
                action_request = {'operation_id': operation_id, 'action': action_name, 'parameters': {}, 'request_key': 'action'}
                action = self.generic(session, 'call', action_request)
                assert self.generic(session, 'call', action_request)['action_id'] == action['action_id']
                if mode in {'hold_action', 'expiry'}:
                    assert adapter.entered.wait(2)
                    self.metrics.expect(lambda: self.generic(session, 'call', {**action_request, 'request_key': 'competing'}),
                                        {'operation_busy'})
                    if mode == 'expiry':
                        with self.lock:
                            self.current.pop(gid, None)
                        self.manager.close(session.session_id, wait=False)
                        self.controller._runtime.poll()
                    else:
                        wait_for(lambda: status()['error'] == 'timeout')
                    assert self.access.gate(gid).snapshot()['blocked'] and not adapter.stopped
                    adapter.release.set()
                elif mode != 'action_error':
                    wait_for(lambda: self.generic(session, 'status', {'operation_id': operation_id,
                        'action_id': action['action_id']})['state'] == 'SUCCEEDED')
                    self.generic(session, 'stop', {'operation_id': operation_id})
                    if mode == 'hold_stop':
                        assert adapter.entered.wait(2)
                        wait_for(lambda: status()['error'] == 'timeout')
                        assert self.access.gate(gid).snapshot()['blocked']
                        adapter.release.set()
            if mode != 'expiry':
                wait_for(lambda: status()['released'])
                self.raw(session)
            else:
                wait_for(lambda: not self.access.gate(gid).snapshot()['blocked'])
            with self.metrics.lock:
                self.metrics.cycles[mode] += 1
        finally:
            if adapter is not None:
                adapter.release.set()
            with self.lock:
                self.current.pop(gid, None)
            self.manager.close(session.session_id, wait=False)
            wait_for(lambda: not self.manager.cleanup_pending(gid))
            def retired():
                self.generic(self.raw_sessions[0], 'list', {})
                with self.controller._lock:
                    return all(binding.session_id != session.session_id for binding, scope in self.controller._bindings.values())
            wait_for(retired)

    def run_thread(self, target, name):
        def guarded():
            try:
                target()
            except BaseException as exc:
                self.metrics.fail(exc)
                self.stop.set()
        thread = Thread(target=guarded, name='soak-' + name)
        self.workers.append(thread)
        thread.start()

    def raw_worker(self, session, rate):
        while not self.stop.is_set():
            started = time.monotonic()
            self.raw(session, independent=True)
            self.stop.wait(max(0, 1 / rate - (time.monotonic() - started)))

    def integration_worker(self, gid):
        iteration = 0
        while not self.stop.is_set():
            self.cycle(gid, iteration)
            iteration += 1
            self.stop.wait(.01)

    def status_worker(self, gid):
        from remoteRF_server.server import dynamic_v2 as v2
        while not self.stop.wait(.005):
            with self.lock:
                current = self.current.get(gid)
            if current is None:
                continue
            session, operation_id = current
            try:
                self.generic(session, 'status', {'operation_id': operation_id})
            except Exception as exc:
                # A copied poll target may close before admission. Only this
                # explicitly observed expiry race is expected, not arbitrary errors.
                expected = isinstance(exc, (v2.RemoteRFSessionExpiredError, v2.RemoteRFStaleHandleError))
                expected = expected or (isinstance(exc, v2.RemoteRFProtocolError)
                    and exc.details.get('integration_code') in {'expired', 'unknown_operation'})
                if not session.closed or not expected:
                    raise
                with self.metrics.lock:
                    self.metrics.expected['closed_session_poll_race:' + type(exc).__name__] += 1

    def gauges(self):
        with self.controller._lock:
            runtime = self.controller._runtime
            with runtime._lock:
                result = {'bindings': len(self.controller._bindings), 'operations': len(runtime._operations),
                    'actions': len(runtime._actions), 'work_queue': runtime._work.queue.qsize(),
                    'cleanup_queue': runtime._cleanup.queue.qsize()}
        result.update(resources())
        return result

    def close(self):
        self.stop.set()
        for adapter in tuple(self.adapters.values()):
            adapter.release.set()
        for worker in self.workers:
            worker.join(12)
            assert not worker.is_alive(), 'soak worker did not stop'
        self.manager.close_all(stop_admission=True)
        wait_for(lambda: all(not self.manager.cleanup_pending(gid) for gid in range(4)))
        self.controller.shutdown()
        wait_for(lambda: self.controller._runtime.drained())
        wait_for(lambda: (self.controller._reap() or not self.controller._bindings))
        assert not self.journal.records()
        self.controller.close()
        self.stack.close()


def preflight(directory):
    """Real durable fence behavior with injected ENOSPC; no disk filling."""
    from remoterf_device_core.integrations import IntegrationError
    from remoterf_device_core.integrations.ownership import Journal, Gate
    journal = Journal(directory / 'enospc')
    gate = Gate('fake:disk-full', journal)
    lease = gate.reserve(session='fake', generation=1, integration='org.remoterf.fake')
    with patch.object(journal, '_write', side_effect=OSError(errno.ENOSPC, 'injected disk-full')):
        try:
            gate.persist(lease)
        except OSError as exc:
            assert exc.errno == errno.ENOSPC
        else:
            raise AssertionError('disk-full injection did not execute')
    assert gate.snapshot()['blocked'] and journal.records()
    try:
        with gate.native():
            raise AssertionError('uncertain journal persistence admitted native access')
    except IntegrationError as exc:
        assert exc.code in {'device_owned', 'device_busy'}
    gate.release(lease, verify=lambda: True)
    assert not gate.snapshot()['blocked'] and not journal.records()
    journal.close()
    return {'injected_enospc_retains_fence': True, 'release_after_verified_cleanup': True}


def backpressure(directory):
    from remoterf_device_core.integrations import IntegrationError, Manifest, Registry
    from remoterf_device_core.integrations.ownership import Journal, Gate
    from remoterf_device_core.integrations.runtime import Binding, Runtime
    definition = Manifest.from_dict(dict(id='org.remoterf.pressure_fixture', version='0.0.0', contract_version='0.1',
        factory='soak_fixture:create', device_types=['usrp'], profiles=['fake'], actions=['stats']))
    registry = Registry([definition], enabled={str(index): [definition.integration_id] for index in range(3)})
    class EmptyAdapter:
        def validate(self, profile, configuration): return configuration
        def start(self, profile, configuration): pass
        def status(self): return {}
        def call(self, action, parameters): return {}
        def stop(self): pass
    registry.create = lambda *args: EmptyAdapter()
    journal = Journal(directory / 'pressure')
    runtime = Runtime(registry, capacity=2, workers=1, action_capacity=2)
    bindings = [Binding(Gate(f'fake:pressure:{index}', journal), str(index), 'usrp', str(index), 1, directory,
        lambda: True, lambda: None, lambda: True, lambda: None, lambda: True) for index in range(3)]
    operations = []
    try:
        for binding in bindings[:2]:
            operation = runtime.start(binding, definition.integration_id, 'fake', {}, request_key='start')
            operations.append((binding, operation['operation_id']))
        wait_for(lambda: all(runtime.status(binding, operation_id)['state'] == 'RUNNING' for binding, operation_id in operations))
        try:
            runtime.start(bindings[2], definition.integration_id, 'fake', {}, request_key='overflow')
        except IntegrationError as exc:
            assert exc.code == 'overloaded'
        else:
            raise AssertionError('owner capacity was not enforced')
        assert not bindings[2].gate.snapshot()['blocked']
        for binding, operation_id in operations:
            action = runtime.call(binding, operation_id, 'stats', {}, request_key='action')
            wait_for(lambda: runtime.action_status(binding, operation_id, action['action_id'])['state'] == 'SUCCEEDED')
            assert runtime.call(binding, operation_id, 'stats', {}, request_key='action')['action_id'] == action['action_id']
        try:
            runtime.call(*operations[0], 'stats', {}, request_key='overflow')
        except IntegrationError as exc:
            assert exc.code == 'overloaded'
        else:
            raise AssertionError('action capacity was not enforced')
        for binding, operation_id in operations:
            runtime.stop(binding, operation_id)
        wait_for(runtime.drained)
        for binding, operation_id in operations:
            runtime.retire(binding)
        operation = runtime.start(bindings[2], definition.integration_id, 'fake', {}, request_key='overflow')
        operations.append((bindings[2], operation['operation_id']))
        wait_for(lambda: runtime.status(bindings[2], operation['operation_id'])['state'] == 'RUNNING')
        runtime.stop(bindings[2], operation['operation_id'])
        wait_for(runtime.drained)
        assert not journal.records()
        return {'owner_capacity': 2, 'action_capacity': 2, 'overload_before_side_effects': True,
                'retry_at_capacity': True, 'admission_after_retirement': True}
    finally:
        runtime.shutdown()
        wait_for(runtime.drained)
        runtime.close()
        journal.close()


def run(directory, *, seconds=600, baseline_seconds=30, raw_rate=100):
    if not 0 < seconds <= 3600 or not 0 < baseline_seconds <= 120 or not 1 <= raw_rate <= 1000:
        raise ValueError('invalid soak bounds')
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    metrics = Metrics()
    from remoterf_device_core.integrations import runtime, ownership
    from remoteRF_server.server import dynamic_v2, integration_controller
    source_paths = [Path(__file__), *(Path(module.__file__) for module in
                    (runtime, ownership, dynamic_v2, integration_controller))]
    source_paths.extend([Path(runtime.__file__).with_name('supervision.py'),
                         Path(runtime.__file__).with_name('contract.py'),
                         Path(dynamic_v2.__file__).with_name('grpc_server.py')])
    report = {'passed': False, 'hardware_qualification': False, 'activation_changed': False,
        'scope': 'in-process production controller/runtime with fake radios, authority, qualification and process scopes',
        'not_covered': ['physical UHD performance', 'network/Global routes', 'real reservation expiry', 'kernel process containment'],
        'python': platform.python_version(), 'versions': {name: version(name) for name in ('remoterf-device-core', 'remoterf-server')},
        'sources': {str(path): sha256(path.read_bytes()).hexdigest() for path in source_paths},
        'requested_seconds': seconds, 'baseline_seconds': baseline_seconds,
        'thresholds': {'status_p99_ms': 50, 'raw_p99_ms': 'max(5, 4 * measured baseline)',
            'minimum_raw_throughput_ratio': .9, 'rss_growth_bytes': 64 * 1024 * 1024,
            'thread_growth': 4, 'fd_growth': 8, 'operations': 16, 'actions': 64},
        'preflight': preflight(directory), 'backpressure': backpressure(directory), 'samples': []}
    rig = Rig(directory / 'runtime', metrics)
    try:
        # Exercise every fault once before measuring the resident baseline.
        # Timeout is intentionally short only in this simulated workload.
        from remoterf_device_core.integrations.runtime import Runtime
        rig.controller._runtime = Runtime(rig.controller.registry, timeout=.08)
        for index in range(9):
            rig.cycle(2, index)
        metrics.cycles.clear()
        for session in rig.raw_sessions:
            rig.run_thread(lambda session=session: rig.raw_worker(session, raw_rate), f'raw-{session.gid}')
        metrics.phase = 'baseline'
        started = time.monotonic()
        assert not rig.stop.wait(baseline_seconds), metrics.failures
        baseline_elapsed = time.monotonic() - started
        # The four fixed workload threads are part of the declared baseline.
        launch = Event()
        for gid in (2, 3):
            rig.run_thread(lambda gid=gid: (launch.wait(), rig.integration_worker(gid)), f'application-{gid}')
            rig.run_thread(lambda gid=gid: (launch.wait(), rig.status_worker(gid)), f'status-{gid}')
        gc.collect()
        initial = rig.gauges()
        metrics.phase = 'mixed'
        started = time.monotonic()
        launch.set()
        print(json.dumps({'phase': 'mixed_started', 'requested_seconds': seconds, 'initial_resources': initial}), flush=True)
        while time.monotonic() - started < seconds and not rig.stop.is_set():
            gauge = rig.gauges()
            gauge['elapsed_seconds'] = time.monotonic() - started
            report['samples'].append(gauge)
            assert gauge['operations'] <= 16 and gauge['actions'] <= 64 and gauge['bindings'] <= 16
            if len(report['samples']) % 60 == 0:
                print(json.dumps({'phase': 'mixed_progress', 'resources': gauge,
                                  'cycles': dict(metrics.cycles), 'unexpected_errors': metrics.failures}), flush=True)
            rig.stop.wait(min(1, max(0, seconds - (time.monotonic() - started))))
        elapsed = time.monotonic() - started
        rig.stop.set()
        for worker in rig.workers:
            worker.join(12)
            assert not worker.is_alive()
        metrics.phase = 'recovery'
        for gid in (2, 3):
            session = rig.manager.open(f'fake-{gid}')
            rig.raw(session)
            rig.manager.close(session.session_id)
        for session in rig.raw_sessions:
            rig.raw(session)
        final = rig.gauges()
        assert not rig.journal.records() and not rig.supervisor.scopes
        assert final['operations'] == final['bindings'] == final['actions'] == 0
        with metrics.lock:
            timings = {key: value.report() for key, value in metrics.timings.items()}
            for key, value in timings.items():
                if key.startswith(('baseline:', 'mixed:')):
                    value['calls_per_second'] = value['count'] / (baseline_elapsed if key.startswith('baseline:') else elapsed)
            report.update(timings=timings, expected_refusals=dict(metrics.expected), unexpected_errors=metrics.failures,
                          cycles=dict(metrics.cycles), elapsed_seconds=elapsed, measured_baseline_seconds=baseline_elapsed,
                          initial_resources=initial, final_resources=final, post_soak_native_recovery=True)
        checks = {'no_unexpected_errors': not metrics.failures, 'completed_duration': elapsed >= seconds * .99,
            'status_latency': timings.get('mixed:status', {}).get('p99_ms', float('inf')) <= 50,
            'resource_bounds': all(row['rss_bytes'] - initial['rss_bytes'] <= 64 * 1024 * 1024
                and row['threads'] - initial['threads'] <= 4 and row['fds'] - initial['fds'] <= 8 for row in report['samples'])}
        for name in ('get_time_now', 'recv_num_samps', 'send_waveform'):
            baseline, mixed = timings['baseline:raw_' + name], timings['mixed:raw_' + name]
            ratio = (mixed['count'] / elapsed) / (baseline['count'] / baseline_elapsed)
            checks['raw_' + name] = mixed['p99_ms'] <= max(5, 4 * baseline['p99_ms']) and ratio >= .9
            mixed['throughput_vs_baseline'] = ratio
        report['checks'] = checks
        report['passed'] = all(checks.values())
    except BaseException as exc:
        metrics.fail(exc)
        report['unexpected_errors'] = metrics.failures
        raise
    finally:
        try:
            rig.close()
        except BaseException as exc:
            metrics.fail(exc)
            report.update(passed=False, unexpected_errors=metrics.failures, cleanup_failed=True)
        target = directory / 'evidence.json'
        target.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
        target.chmod(0o600)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=600)
    parser.add_argument('--baseline-seconds', type=float, default=30)
    parser.add_argument('--raw-rate', type=float, default=100)
    args = parser.parse_args()
    if platform.system() != 'Linux':
        parser.error('Linux /proc resource measurements are required')
    report = run(args.state_dir, seconds=args.seconds, baseline_seconds=args.baseline_seconds, raw_rate=args.raw_rate)
    print(json.dumps({key: report[key] for key in ('passed', 'elapsed_seconds', 'checks', 'cycles', 'unexpected_errors')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
