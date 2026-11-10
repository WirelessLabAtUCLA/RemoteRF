"""Failure and race contracts for ownership; all hardware is deterministic fake state."""
from dataclasses import replace
from threading import Event
import time
from unittest.mock import Mock

import pytest

from remoterf_device_core.integrations import IntegrationError, Manifest, Registry
from remoterf_device_core.integrations.ownership import Gate, Journal
from remoterf_device_core.integrations.runtime import Binding, Runtime


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.005)
    raise AssertionError('operation did not reach the expected state')


@pytest.fixture
def rig(tmp_path, monkeypatch):
    journal = Journal(tmp_path / 'journal')
    gate = Gate('serial:one', journal)
    state = dict(native=True, application=False, authorized=True, starts=0, stops=0, restores=0)
    entered, proceed = Event(), Event()
    proceed.set()

    class Adapter:
        def validate(self, profile, configuration):
            return configuration
        def start(self, profile, configuration):
            state['starts'] += 1
            entered.set()
            proceed.wait()
            state['application'] = True
        def status(self):
            return {'ready': state['application']}
        def call(self, action, parameters):
            return {}
        def stop(self):
            state['stops'] += 1
            state['application'] = False

    adapter = Adapter()
    definition = Manifest.from_dict(dict(id='org.example.test', version='1.0.0', contract_version='0.1',
        factory='external_adapter:create', device_types=['usrp'], profiles=['test'], actions=['increment']))
    registry = Registry([definition], enabled={'7': [definition.integration_id]})
    monkeypatch.setattr(registry, 'create', lambda *args: adapter)

    def restore():
        state['native'] = True
        state['restores'] += 1
    binding = Binding(gate, '7', 'usrp', 'session', 1, tmp_path,
        lambda: state['authorized'], lambda: state.update(native=False), lambda: not state['native'],
        restore, lambda: not state['application'])
    runtime = Runtime(registry, capacity=2, workers=1, timeout=2)
    def start(key='start', config=None, bound=binding):
        return runtime.start(bound, definition.integration_id, 'test', config or {}, request_key=key)
    yield runtime, binding, state, adapter, entered, proceed, start
    proceed.set()
    state['authorized'] = False
    runtime.shutdown()
    wait_for(lambda: all(not row['active'] and not row['cleanup'] for row in runtime._operations.values()))
    for row in list(runtime._operations.values()):
        if not row['released']:
            # Explicit fake-hardware reconciliation, never assume timeout freed it.
            state.update(native=False, application=False)
            runtime.recover(row['binding'], row['id'], verify=lambda: True)
    runtime.close()
    journal.close()


def test_lifecycle_keeps_native_blocked_until_verified_restore(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    op = start()['operation_id']
    wait_for(lambda: runtime.status(binding, op)['state'] == 'RUNNING')
    assert not state['native'] and state['application']
    with pytest.raises(IntegrationError, match='owned'):
        with binding.gate.native():
            pass
    runtime.stop(binding, op)
    wait_for(lambda: runtime.status(binding, op)['released'])
    assert state['native'] and not state['application']
    assert state['restores'] == state['stops'] == 1
    runtime.stop(binding, op)
    assert state['stops'] == 1
    with binding.gate.native():
        pass


def test_duplicate_start_and_mismatched_retry(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    first = start()
    assert start()['operation_id'] == first['operation_id']
    with pytest.raises(IntegrationError) as caught:
        start(config={'changed': True})
    assert caught.value.code == 'request_conflict'
    wait_for(entered.is_set)
    assert state['starts'] == 1


def test_expiry_during_start_cleans_late_success_without_reacquiring(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    proceed.clear()
    op = start()['operation_id']
    wait_for(entered.is_set)
    state['authorized'] = False
    runtime.poll()
    assert runtime.status(binding, op)['state'] == 'STOPPING'
    assert binding.gate.snapshot()['blocked']
    assert not state['stops']
    proceed.set()
    wait_for(lambda: runtime.status(binding, op)['released'])
    assert state['restores'] == 0
    assert not state['application'] and not state['native']


def test_hung_start_times_out_but_never_releases_ownership(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    runtime.timeout = 0.03
    proceed.clear()
    op = start()['operation_id']
    wait_for(entered.is_set)
    wait_for(lambda: runtime.status(binding, op)['error'] == 'timeout')
    assert binding.gate.snapshot()['blocked']
    with pytest.raises(IntegrationError):
        runtime.recover(binding, op, verify=lambda: True)
    assert state['stops'] == 0
    proceed.set()
    wait_for(lambda: runtime.status(binding, op)['released'])


@pytest.mark.parametrize('failure', ['stop', 'verify', 'native_release', 'restore', 'bad_result'])
def test_cleanup_failures_stay_owned_until_explicit_reconciliation(rig, failure):
    runtime, binding, state, adapter, entered, proceed, start = rig
    broken = Mock(side_effect=RuntimeError('private credentials must not be exposed'))
    if failure == 'native_release':
        binding = replace(binding, release_native=broken)
    elif failure == 'restore':
        binding = replace(binding, restore_native=broken)
    elif failure == 'verify':
        binding = replace(binding, resources_released=lambda: False)
    elif failure == 'stop':
        adapter.stop = broken
    elif failure == 'bad_result':
        adapter.status = lambda: {'__uhd_type__': 'TimeSpec'}
    op = start(bound=binding)['operation_id']
    if failure not in {'native_release', 'bad_result'}:
        wait_for(lambda: runtime.status(binding, op)['state'] == 'RUNNING')
        runtime.stop(binding, op)
    wait_for(lambda: runtime.status(binding, op)['error'] is not None)
    wait_for(lambda: not runtime._operations[op]['active'] and not runtime._operations[op]['cleanup'])
    snapshot = runtime.status(binding, op)
    assert 'private' not in str(snapshot)
    if failure == 'bad_result':
        assert snapshot['released']  # failed serialization still stops the successful start
    else:
        assert not snapshot['released'] and binding.gate.snapshot()['blocked']
        state.update(native=False, application=False)
        runtime.recover(binding, op, verify=lambda: True)
        assert not binding.gate.snapshot()['blocked']


def test_authority_loss_during_restore_releases_new_native_owner(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    binding = replace(binding, restore_native=lambda: state.update(native=True, authorized=False))
    op = start(bound=binding)['operation_id']
    wait_for(lambda: runtime.status(binding, op)['state'] == 'RUNNING')
    runtime.stop(binding, op)
    wait_for(lambda: runtime.status(binding, op)['released'])
    assert not state['native']


def test_operation_identity_and_bounded_retry_retention(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    for key in ['one', 'two']:
        op = start(key)['operation_id']
        wait_for(lambda: runtime.status(binding, op)['state'] == 'RUNNING')
        runtime.stop(binding, op)
        wait_for(lambda: runtime.status(binding, op)['released'])
    with pytest.raises(IntegrationError) as caught:
        start('three')
    assert caught.value.code == 'overloaded'
    with pytest.raises(IntegrationError):
        runtime.status(replace(binding, generation=2), op)
    runtime.retire(binding)
    assert start('three')['operation_id'] != op


def test_journal_restart_requires_positive_release_proof(tmp_path):
    journal = Journal(tmp_path)
    gate = Gate('serial:one', journal)
    lease = gate.claim(session='s', generation=1, integration='org.example.test')
    with pytest.raises(IntegrationError):
        Journal(tmp_path)
    journal.close()
    restarted = Journal(tmp_path)
    restored = Gate('serial:one', restarted)
    assert restored.snapshot()['blocked']
    with pytest.raises(IntegrationError):
        restored.release(lease, verify=lambda: False)
    with pytest.raises(IntegrationError):
        restored.release('stale', verify=lambda: True)
    restored.release(lease, verify=lambda: True)
    assert restarted.records() == {}
    restarted.close()


def test_corrupt_journal_and_failed_persistence_fail_closed(tmp_path, monkeypatch):
    journal = Journal(tmp_path)
    gate = Gate('serial:one', journal)
    monkeypatch.setattr(journal, '_write', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError):
        gate.claim(session='s', generation=1, integration='org.example.test')
    assert gate.snapshot()['blocked']
    journal.close()
    (tmp_path / 'ownership.json').write_text('{broken')
    with pytest.raises(IntegrationError):
        Journal(tmp_path)


def test_native_admission_prevents_claim_and_lease_blocks_aliases(tmp_path):
    journal = Journal(tmp_path)
    gate = Gate('serial:one', journal)
    with gate.native():
        with pytest.raises(IntegrationError) as caught:
            gate.claim(session='s', generation=1, integration='org.example.test')
        assert caught.value.code == 'device_busy'
    lease = gate.claim(session='s', generation=1, integration='org.example.test')
    gate.release(lease, verify=lambda: True)
    journal.close()


def test_aliases_share_one_live_gate_and_maintenance_excludes_claims(tmp_path):
    journal = Journal(tmp_path)
    first = Gate('serial:one', journal)
    alias = Gate('serial:one', journal)
    assert first is alias
    with first.native():
        with pytest.raises(IntegrationError):
            alias.claim(session='s', generation=1, integration='org.example.test')
        with pytest.raises(IntegrationError):
            alias.maintenance().__enter__()
    with first.maintenance():
        with pytest.raises(IntegrationError):
            with alias.native():
                pass
        with pytest.raises(IntegrationError):
            alias.claim(session='s', generation=1, integration='org.example.test')
    journal.close()


def test_hung_cleanup_keeps_lease_and_cached_status_responsive(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    op = start()['operation_id']
    wait_for(lambda: runtime.status(binding, op)['state'] == 'RUNNING')
    stop_entered, finish = Event(), Event()
    def stop():
        stop_entered.set()
        finish.wait()
        state['application'] = False
    adapter.stop = stop
    runtime.timeout = 0.03
    try:
        runtime.stop(binding, op)
        wait_for(stop_entered.is_set)
        wait_for(lambda: runtime.status(binding, op)['error'] == 'timeout')
        runtime.stop(binding, op)  # attaches; never spawns a concurrent stop
        assert binding.gate.snapshot()['blocked']
        with pytest.raises(IntegrationError):
            runtime.recover(binding, op, verify=lambda: True)
    finally:
        finish.set()
    wait_for(lambda: runtime.status(binding, op)['released'])


def test_shutdown_cancels_and_refuses_new_starts(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    op = start()['operation_id']
    wait_for(lambda: runtime.status(binding, op)['state'] == 'RUNNING')
    runtime.shutdown()
    wait_for(lambda: runtime.status(binding, op)['released'])
    with pytest.raises(IntegrationError) as caught:
        start('new')
    assert caught.value.code == 'overloaded'


def test_slow_journal_never_blocks_status_or_runs_native_before_durability(rig, monkeypatch):
    runtime, binding, state, adapter, entered, proceed, start = rig
    writing, finish = Event(), Event()
    write = binding.gate.journal._write
    def delayed(records):
        writing.set()
        finish.wait()
        write(records)
    monkeypatch.setattr(binding.gate.journal, '_write', delayed)
    runtime.timeout = 0.03
    try:
        op = start()['operation_id']
        wait_for(writing.is_set)
        assert runtime.status(binding, op)['state'] == 'STARTING'
        assert binding.gate.snapshot()['blocked']
        wait_for(lambda: runtime.status(binding, op)['error'] == 'timeout')
        assert state['native'] and state['starts'] == 0
    finally:
        finish.set()
    wait_for(lambda: runtime.status(binding, op)['released'])
    assert state['starts'] == state['restores'] == 0


def test_one_stuck_cleanup_does_not_stop_an_independent_device(tmp_path):
    journal = Journal(tmp_path / 'journal')
    registry = Mock()
    registry.validate_start.side_effect = lambda *args, **kwargs: kwargs['configuration']
    runtime = Runtime(registry, capacity=2, workers=2)
    blocked, finish = Event(), Event()
    bindings, adapters, states = [], [], []
    for index in range(2):
        state = {'native': True, 'application': False}
        states.append(state)
        adapter = Mock()
        adapter.validate.side_effect = lambda profile, config: config
        adapter.start.side_effect = lambda *args, state=state: state.update(application=True)
        adapter.status.return_value = {}
        adapter.stop.side_effect = lambda state=state: state.update(application=False)
        adapters.append(adapter)
        bindings.append(Binding(Gate(f'serial:{index}', journal), str(index), 'usrp', f'session{index}',
            1, tmp_path, lambda: True, lambda state=state: state.update(native=False),
            lambda state=state: not state['native'], lambda state=state: state.update(native=True),
            lambda state=state: not state['application']))
    registry.create.side_effect = lambda integration, context: adapters[int(context.device_id)]
    def stuck_stop():
        blocked.set()
        finish.wait()
        states[0]['application'] = False
    adapters[0].stop.side_effect = stuck_stop
    ops = []
    try:
        for binding in bindings:
            op = runtime.start(binding, 'org.example.test', 'test', {}, request_key='start')['operation_id']
            ops.append(op)
            wait_for(lambda: runtime.status(binding, op)['state'] == 'RUNNING')
        runtime.stop(bindings[0], ops[0])
        wait_for(blocked.is_set)
        runtime.stop(bindings[1], ops[1])
        wait_for(lambda: runtime.status(bindings[1], ops[1])['released'])
        assert bindings[0].gate.snapshot()['blocked']
        with bindings[1].gate.native():
            assert states[1]['native']
    finally:
        finish.set()
        runtime.shutdown()
        wait_for(lambda: all(row['released'] and not row['cleanup'] for row in runtime._operations.values()))
        runtime.close()
        journal.close()


@pytest.mark.parametrize('text', [
    '{"serial:one":{},"serial:one":{}}',
    '{"serial:one":{"lease":"a","lease":"b","session":"s","generation":1,"integration":"i"}}',
])
def test_duplicate_journal_keys_require_recovery(tmp_path, text):
    (tmp_path / 'ownership.json').write_text(text)
    with pytest.raises(IntegrationError) as caught:
        Journal(tmp_path)
    assert caught.value.code == 'recovery_required'


def test_runtime_close_is_idempotent():
    runtime = Runtime(Mock(), capacity=1, workers=1)
    runtime.close()
    runtime.close()


def test_abrupt_process_exit_preserves_the_durable_owner(tmp_path):
    import subprocess
    import sys
    child = subprocess.run([sys.executable, '-c', '''
import os, sys
from pathlib import Path
from remoterf_device_core.integrations.ownership import Gate, Journal
journal = Journal(Path(sys.argv[1]))
Gate('serial:one', journal).claim(session='s', generation=1, integration='org.example.test')
os._exit(23)
''', str(tmp_path)], timeout=5, capture_output=True)
    assert child.returncode == 23, child.stderr.decode()
    journal = Journal(tmp_path)  # the dead process's writer lock is released
    try:
        gate = Gate('serial:one', journal)
        assert gate.snapshot()['blocked']
        with pytest.raises(IntegrationError):
            with gate.native():
                pass
        lease = journal.records()['serial:one']['lease']
        gate.release(lease, verify=lambda: True)  # the child created no external resources
    finally:
        journal.close()



def running(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    operation_id = start()['operation_id']
    wait_for(lambda: runtime.status(binding, operation_id)['state'] == 'RUNNING'
             and not runtime._operations[operation_id]['active'])
    return runtime, binding, state, adapter, operation_id


def test_actions_are_serial_retry_safe_and_refresh_cached_status(rig):
    runtime, binding, state, adapter, operation_id = running(rig)
    entered, finish = Event(), Event()
    calls = []
    def call(action, parameters):
        calls.append((action, parameters))
        entered.set()
        finish.wait()
        state['counter'] = parameters['delta']
        return {'value': state['counter']}
    adapter.call = call
    adapter.status = lambda: {'value': state.get('counter', 0)}
    try:
        first = runtime.call(binding, operation_id, 'increment', {'delta': 3}, request_key='increment')
        action_id = first['action_id']
        wait_for(entered.is_set)
        assert runtime.status(binding, operation_id)['state'] == 'RUNNING'
        assert runtime.status(binding, operation_id)['active_action_id'] == action_id
        assert runtime.call(binding, operation_id, 'increment', {'delta': 3},
                            request_key='increment')['action_id'] == action_id
        with pytest.raises(IntegrationError) as caught:
            runtime.call(binding, operation_id, 'increment', {'delta': 4}, request_key='increment')
        assert caught.value.code == 'request_conflict'
        with pytest.raises(IntegrationError) as caught:
            runtime.call(binding, operation_id, 'increment', {'delta': 4}, request_key='different')
        assert caught.value.code == 'operation_busy'
    finally:
        finish.set()
    wait_for(lambda: runtime.action_status(binding, operation_id, action_id)['state'] == 'SUCCEEDED')
    assert calls == [('increment', {'delta': 3})]
    assert runtime.action_status(binding, operation_id, action_id)['result'] == {'value': 3}
    assert runtime.status(binding, operation_id)['details'] == {'value': 3}
    assert runtime.status(binding, operation_id)['active_action_id'] is None
    # Mutating a returned status cannot corrupt retry outcomes.
    result = runtime.action_status(binding, operation_id, action_id)
    result['result']['value'] = 'changed'
    assert runtime.call(binding, operation_id, 'increment', {'delta': 3},
                        request_key='increment')['result'] == {'value': 3}


@pytest.mark.parametrize('reason', ['stop', 'expiry', 'shutdown', 'timeout'])
def test_action_cancellation_never_races_cleanup_or_resurrects_late_result(rig, reason):
    runtime, binding, state, adapter, operation_id = running(rig)
    entered, finish = Event(), Event()
    def call(*args):
        entered.set()
        finish.wait()
        return {'late': True}
    adapter.call = call
    try:
        if reason == 'timeout':
            runtime.timeout = 0.03
        action_id = runtime.call(binding, operation_id, 'increment', {}, request_key='call')['action_id']
        wait_for(entered.is_set)
        if reason == 'stop':
            runtime.stop(binding, operation_id)
        elif reason == 'expiry':
            state['authorized'] = False
            runtime.poll()
        elif reason == 'shutdown':
            runtime.shutdown()
        else:
            wait_for(lambda: runtime.action_status(binding, operation_id, action_id)['error'] == 'timeout')
        snapshot = runtime.action_status(binding, operation_id, action_id)
        assert snapshot['state'] in {'CANCELLED', 'ERROR'} and snapshot['result'] == {}
        assert binding.gate.snapshot()['blocked'] and not runtime.drained(binding)
        assert state['stops'] == 0
        with pytest.raises(IntegrationError):
            runtime.recover(binding, operation_id, verify=lambda: True)
    finally:
        finish.set()
    wait_for(lambda: runtime.drained(binding))
    snapshot = runtime.action_status(binding, operation_id, action_id)
    assert snapshot['state'] in {'CANCELLED', 'ERROR'} and snapshot['result'] == {}
    assert state['stops'] == 1
    if reason == 'timeout':
        assert snapshot['error'] == runtime.status(binding, operation_id)['error'] == 'timeout'
    if reason == 'expiry':
        assert state['restores'] == 0


@pytest.mark.parametrize('failure', ['exception', 'reserved_result', 'oversized_result', 'bad_status'])
def test_action_failure_is_safe_and_stops_owner(rig, failure):
    runtime, binding, state, adapter, operation_id = running(rig)
    if failure == 'exception':
        adapter.call = Mock(side_effect=RuntimeError('secret credentials'))
    elif failure == 'reserved_result':
        adapter.call = lambda *args: {'__uhd_type__': 'TimeSpec'}
    elif failure == 'oversized_result':
        adapter.call = lambda *args: {'data': 'x' * (48 * 1024)}
    else:
        adapter.status = lambda: {'__uhd_type__': 'TimeSpec'}
    action_id = runtime.call(binding, operation_id, 'increment', {}, request_key='call')['action_id']
    wait_for(lambda: runtime.drained(binding))
    snapshot = runtime.action_status(binding, operation_id, action_id)
    assert snapshot['state'] == 'ERROR' and snapshot['result'] == {}
    assert 'secret' not in str(snapshot)
    assert state['stops'] == 1 and state['restores'] == 1


def test_action_validation_precedes_callbacks_and_ids_are_owner_scoped(rig):
    runtime, binding, state, adapter, operation_id = running(rig)
    adapter.call = Mock(return_value={})
    for action, parameters in [('missing', {}), ('increment', {'nested': {'__uhd_type__': 'TimeSpec'}})]:
        with pytest.raises(IntegrationError):
            runtime.call(binding, operation_id, action, parameters, request_key='key')
    adapter.call.assert_not_called()
    action_id = runtime.call(binding, operation_id, 'increment', {}, request_key='key')['action_id']
    wait_for(lambda: runtime.action_status(binding, operation_id, action_id)['state'] == 'SUCCEEDED')
    with pytest.raises(IntegrationError):
        runtime.action_status(replace(binding, generation=2), operation_id, action_id)
    with pytest.raises(IntegrationError):
        runtime.action_status(binding, operation_id, 'missing')
    state['authorized'] = False
    with pytest.raises(IntegrationError) as caught:
        runtime.call(binding, operation_id, 'increment', {}, request_key='key')
    assert caught.value.code == 'expired'
    assert adapter.call.call_count == 1


def test_action_capacity_retains_outcomes_until_generation_retirement(rig):
    runtime, binding, state, adapter, operation_id = running(rig)
    runtime.action_capacity = 1
    action_id = runtime.call(binding, operation_id, 'increment', {}, request_key='first')['action_id']
    wait_for(lambda: runtime.action_status(binding, operation_id, action_id)['state'] == 'SUCCEEDED')
    with pytest.raises(IntegrationError) as caught:
        runtime.call(binding, operation_id, 'increment', {}, request_key='second')
    assert caught.value.code == 'overloaded'
    assert runtime.call(binding, operation_id, 'increment', {}, request_key='first')['action_id'] == action_id
    assert len(runtime.operations(binding)) == 1
    assert runtime.operations(replace(binding, generation=2)) == []
    runtime.stop(binding, operation_id)
    wait_for(lambda: runtime.drained(binding))
    assert runtime.call(binding, operation_id, 'increment', {}, request_key='first')['action_id'] == action_id
    runtime.retire(binding)
    assert runtime.drained() and runtime.operations() == []
    assert runtime._actions == {}
    with pytest.raises(IntegrationError):
        runtime.action_status(binding, operation_id, action_id)


def test_preparation_runs_after_durable_claim_and_cancellation_preserves_native(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    preparing, finish = Event(), Event()
    def prepare():
        assert binding.gate.journal.records()
        assert state['native']
        preparing.set()
        finish.wait()
    binding = replace(binding, prepare=prepare)
    try:
        operation_id = start(bound=binding)['operation_id']
        wait_for(preparing.is_set)
        assert runtime.status(binding, operation_id)['phase'] == 'preparing_resources'
        runtime.stop(binding, operation_id)
        assert state['native'] and not state['starts']
    finally:
        finish.set()
    wait_for(lambda: runtime.drained(binding))
    assert state['native'] and not state['starts'] and not state['restores']


def test_failed_uncertain_preparation_retains_lease(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    binding = replace(binding, prepare=Mock(side_effect=OSError('disk full')),
                      resources_released=lambda: False)
    operation_id = start(bound=binding)['operation_id']
    wait_for(lambda: runtime.status(binding, operation_id)['error'] == 'recovery_required')
    assert binding.gate.snapshot()['blocked'] and state['native'] and not state['starts']
    assert not runtime.drained(binding)


def test_selector_is_validated_before_claim_and_copied_into_context(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    with pytest.raises(IntegrationError):
        start(bound=replace(binding, selector={'__uhd_type__': 'TimeSpec'}))
    assert not binding.gate.snapshot()['blocked']
    selector = {'serial': 'one'}
    processes = object()
    created = []
    runtime.registry.create = lambda integration, context: created.append(context) or adapter
    operation_id = start(bound=replace(binding, selector=selector, processes=processes))['operation_id']
    wait_for(lambda: runtime.status(binding, operation_id)['state'] == 'RUNNING')
    selector['serial'] = 'different'
    assert created[0].selector == {'serial': 'one'}
    assert created[0].processes is processes


def test_durable_claim_records_resource_owner_and_finalizer_runs_after_stop_error(rig):
    runtime, binding, state, adapter, entered, proceed, start = rig
    finalized = []
    def finalize():
        finalized.append(True)
        state['application'] = False
    binding = replace(binding, resource_owner='scope-owner-one', finalize=finalize)
    operation_id = start(bound=binding)['operation_id']
    wait_for(lambda: runtime.status(binding, operation_id)['state'] == 'RUNNING')
    assert binding.gate.journal.records()['serial:one']['resources'] == 'scope-owner-one'
    adapter.stop = Mock(side_effect=RuntimeError('adapter teardown failed'))
    runtime.stop(binding, operation_id)
    wait_for(lambda: finalized and not runtime._operations[operation_id]['cleanup'])
    assert finalized == [True] and not state['application']
    assert binding.gate.snapshot()['blocked']  # failed adapter cleanup still needs reconciliation
