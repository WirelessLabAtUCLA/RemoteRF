"""Required-worker monitoring is opt-in; status RPCs never run adapter hooks.

Hardware/process state here is deterministic fake state. Actual descendant
cleanup and restart recovery are qualified separately in Linux cgroups.
"""
from dataclasses import replace
from threading import Event
import time

import pytest

from remoterf_device_core.integrations import IntegrationError, Manifest, Registry
from remoterf_device_core.integrations.ownership import Gate, Journal
from remoterf_device_core.integrations.runtime import Binding, Runtime
from test_integration_runtime import rig


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('application lifecycle did not finish')


def test_unmonitored_idle_worker_crash_keeps_fence_until_explicit_health_action(tmp_path, monkeypatch):
    journal = Journal(tmp_path / 'ownership')
    gate = Gate('qualification:fake-radio', journal)
    worker_alive, authorized = Event(), Event()
    authorized.set()
    native = {'held': True}
    calls = {'status': 0, 'stop': 0}

    class Adapter:
        def validate(self, profile, configuration):
            return configuration

        def start(self, profile, configuration):
            worker_alive.set()

        def status(self):
            calls['status'] += 1
            if not worker_alive.is_set():
                raise RuntimeError('the required worker exited')
            return {'ready': True}

        def call(self, action, parameters):
            assert action == 'health' and parameters == {}
            return self.status()

        def stop(self):
            calls['stop'] += 1
            worker_alive.clear()

    definition = Manifest.from_dict(dict(id='org.example.process_health', version='1.0.0', contract_version='0.1',
        factory='fixture_only:create', device_types=['usrp'], profiles=['receive'], actions=['health']))
    registry = Registry([definition], enabled={'1': [definition.integration_id]})
    monkeypatch.setattr(registry, 'create', lambda *args: Adapter())
    binding = Binding(gate, '1', 'usrp', 'fake-session', 1, tmp_path, authorized.is_set,
                      lambda: native.update(held=False), lambda: not native['held'],
                      lambda: native.update(held=True), lambda: not worker_alive.is_set())
    runtime = Runtime(registry, capacity=1, workers=1, timeout=1)
    try:
        operation = runtime.start(binding, definition.integration_id, 'receive', {}, request_key='start')['operation_id']
        wait_for(lambda: runtime.status(binding, operation)['state'] == 'RUNNING')
        assert calls['status'] == 1
        worker_alive.clear()  # Deterministic idle crash after successful readiness.
        for _ in range(3):
            runtime.poll()
            cached = runtime.status(binding, operation)
            assert cached['state'] == 'RUNNING' and cached['details'] == {'ready': True}
            assert not cached['released'] and journal.records()
            with pytest.raises(IntegrationError) as caught:
                with gate.native():
                    pytest.fail('a dead worker must not silently release the hardware fence')
            assert caught.value.code == 'device_owned'
        assert calls == {'status': 1, 'stop': 0}
        action = runtime.call(binding, operation, 'health', {}, request_key='detect-exit')['action_id']
        wait_for(lambda: runtime.drained(binding))
        assert runtime.action_status(binding, operation, action)['state'] == 'ERROR'
        assert runtime.status(binding, operation)['released']
        assert calls == {'status': 2, 'stop': 1}
        assert native['held'] and journal.records() == {}
        with gate.native():
            pass
    finally:
        authorized.clear()
        runtime.shutdown()
        wait_for(lambda: runtime.drained(binding))
        runtime.close()
        journal.close()


def test_required_idle_worker_exit_triggers_cleanup_without_followup_rpc(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    exited = Event()
    binding = replace(binding, resource_failure=lambda: 'process_exited' if exited.is_set() else None)
    operation = start(bound=binding)['operation_id']
    wait_for(lambda: runtime.status(binding, operation)['state'] == 'RUNNING')
    exited.set()
    wait_for(lambda: runtime.drained(binding))
    status = runtime.status(binding, operation)
    assert status['error'] == 'process_exited' and status['released']
    assert state['stops'] == state['restores'] == 1 and binding.gate.journal.records() == {}


def test_required_exit_during_start_cannot_publish_ready_or_race_stop(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    exited = Event()
    binding = replace(binding, resource_failure=lambda: 'process_exited' if exited.is_set() else None)
    proceed.clear()
    operation = start(bound=binding)['operation_id']
    assert entered.wait(1)
    exited.set()
    runtime.poll()
    assert runtime.status(binding, operation)['error'] == 'process_exited'
    assert binding.gate.snapshot()['blocked'] and state['stops'] == 0
    proceed.set()
    wait_for(lambda: runtime.drained(binding))
    assert runtime.status(binding, operation)['error'] == 'process_exited'
    assert state['stops'] == 1


def test_required_exit_waits_for_active_action_then_cleans(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    exited, action_entered, action_finish = Event(), Event(), Event()
    binding = replace(binding, resource_failure=lambda: 'process_exited' if exited.is_set() else None)
    def call(*args):
        action_entered.set()
        assert action_finish.wait(2)
        return {}
    adapter.call = call
    operation = start(bound=binding)['operation_id']
    wait_for(lambda: runtime.status(binding, operation)['state'] == 'RUNNING')
    runtime.call(binding, operation, 'increment', {}, request_key='active')
    try:
        assert action_entered.wait(1)
        exited.set()
        runtime.poll()
        assert runtime.status(binding, operation)['error'] == 'process_exited'
        assert state['stops'] == 0 and binding.gate.snapshot()['blocked']
    finally:
        action_finish.set()
    wait_for(lambda: runtime.drained(binding))
    assert state['stops'] == 1


def test_stop_induced_exit_does_not_replace_intentional_stop(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    exited, stop_entered, stop_finish = Event(), Event(), Event()
    binding = replace(binding, resource_failure=lambda: 'process_exited' if exited.is_set() else None)
    original = adapter.stop
    def stop():
        stop_entered.set()
        exited.set()
        assert stop_finish.wait(2)
        original()
    adapter.stop = stop
    operation = start(bound=binding)['operation_id']
    wait_for(lambda: runtime.status(binding, operation)['state'] == 'RUNNING')
    runtime.stop(binding, operation)
    try:
        assert stop_entered.wait(1)
        runtime.poll()
        assert runtime.status(binding, operation)['error'] is None
    finally:
        stop_finish.set()
    wait_for(lambda: runtime.drained(binding))
    assert runtime.status(binding, operation)['state'] == 'IDLE'


def test_broken_resource_monitor_cancels_without_losing_fence(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    broken = Event()
    def probe():
        if broken.is_set():
            raise RuntimeError('monitor unavailable')
    binding = replace(binding, resource_failure=probe, resources_released=lambda: False)
    operation = start(bound=binding)['operation_id']
    wait_for(lambda: runtime.status(binding, operation)['state'] == 'RUNNING')
    broken.set()
    wait_for(lambda: state['stops'] == 1)
    wait_for(lambda: not runtime._operations[operation]['cleanup'])
    assert runtime.status(binding, operation)['error'] == 'recovery_required'
    assert not runtime.status(binding, operation)['released'] and binding.gate.snapshot()['blocked']


def test_required_exit_does_not_erase_an_existing_hook_timeout(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    exited = Event()
    binding = replace(binding, resource_failure=lambda: 'process_exited' if exited.is_set() else None)
    runtime.timeout = 0.03
    proceed.clear()
    operation = start(bound=binding)['operation_id']
    assert entered.wait(1)
    wait_for(lambda: runtime.status(binding, operation)['error'] == 'timeout')
    exited.set()
    runtime.poll()
    assert runtime.status(binding, operation)['error'] == 'timeout'
    assert state['stops'] == 0 and binding.gate.snapshot()['blocked']
    proceed.set()
    wait_for(lambda: runtime.drained(binding))
    assert runtime.status(binding, operation)['error'] == 'timeout'
