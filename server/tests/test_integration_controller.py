"""Session/controller boundaries with fake hardware and trusted fake supervision."""
from dataclasses import replace
import json
from threading import Event, Thread
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from remoterf_device_core.hardware_access import AccessTable
from remoterf_device_core.integrations import IntegrationError, Manifest, Registry
from remoterf_device_core.integrations import deployment
from remoterf_device_core.integrations.deployment import Configuration, REQUIRED_CHECKS
from remoterf_device_core.integrations.ownership import Journal
from remoteRF_server.server import dynamic_v2 as v2
from remoteRF_server.server import integration_controller as control
from test_native_lifetime import Inventory, Native, opened, until


class Scope:
    def __init__(self, owner):
        self.owner = owner
        self.stops = 0
        self.closed = False
    def resources_released(self):
        return True
    def stop(self):
        self.stops += 1
        return True
    def close(self):
        self.closed = True


class Supervisor:
    def __init__(self):
        self.scopes = []
        self.closed = False
    def create(self, owner):
        result = Scope(owner)
        self.scopes.append(result)
        return result
    def close(self):
        self.closed = True


@pytest.fixture
def controlled(opened, tmp_path, monkeypatch):
    manager, session, inventory = opened
    definition = Manifest.from_dict(dict(id='org.example.adapter', version='1.0.0', contract_version='0.1',
        factory='test_only_adapter:create', device_types=['usrp'], profiles=['rx'], actions=['stats']))
    registry = Registry([definition], enabled={str(session.gid): [definition.integration_id]})
    receipt = dict(qualification_version=1, serial='RADIO_ONE', integration_id=definition.integration_id,
        adapter_version=definition.version, adapter_hash='a' * 64, profile='rx',
        schema_hash=session.schema_v2['schema_hash'], uhd_version='test', framework_hash='framework',
        passed=True, checks={check: True for check in REQUIRED_CHECKS}, run_id='test-fixture-only')
    config = Configuration(registry=registry, enabled=True,
        devices={session.gid: {'serial': 'RADIO_ONE', 'physical_id': 'usrp:serial:RADIO_ONE'}},
        receipts=(receipt,), adapter_hashes={definition.integration_id: 'a' * 64})
    monkeypatch.setattr(deployment, 'adapter_digest', lambda definition: 'a' * 64)
    monkeypatch.setattr(control, 'code_digest', lambda *args: 'framework')
    access = AccessTable()
    journal = Journal(tmp_path / 'journal')
    access.configure({session.gid: 'usrp:serial:RADIO_ONE'}, journal)
    monkeypatch.setattr(v2, 'hardware_access', access)
    supervisor = Supervisor()
    controller = control.Controller(config, state_dir=tmp_path, journal=journal, supervisor=supervisor,
                                    framework_hash='framework')
    manager.integrations = controller
    entered, proceed = Event(), Event()
    proceed.set()
    adapter = Mock()
    adapter.validate.side_effect = lambda profile, configuration: configuration
    adapter.status.return_value = {'ready': True}
    adapter.call.return_value = {'count': 1}
    def start(profile, configuration):
        entered.set()
        assert proceed.wait(3)
    adapter.start.side_effect = start
    monkeypatch.setattr(registry, 'create', lambda *args: adapter)
    def invoke(method, request):
        return manager.invoke(session.session_id, session.root_handle, 'integration_' + method,
                              {'request': request})[0]
    def start_request(key='start', generation=None):
        return dict(integration_id=definition.integration_id, profile='rx', configuration={}, request_key=key,
                    generation=generation or session.handles[session.root_handle].generation)
    rig = SimpleNamespace(manager=manager, session=session, inventory=inventory, definition=definition,
        config=config, controller=controller, access=access, journal=journal, supervisor=supervisor,
        adapter=adapter, entered=entered, proceed=proceed, invoke=invoke, start_request=start_request)
    yield rig
    proceed.set()
    controller.shutdown()
    manager.close_all()
    until(lambda: not manager._closing_sessions)
    if controller._runtime is not None:
        until(lambda: all(row['released'] for row in controller._runtime._operations.values()))
    controller.close()


def code_from(call, expected):
    with pytest.raises(v2.RemoteRFProtocolError) as caught:
        call()
    assert caught.value.details['integration_code'] == expected


def running(rig, key='start'):
    result = rig.invoke('start', rig.start_request(key))
    until(lambda: rig.invoke('status', {'operation_id': result['operation_id']})['state'] == 'RUNNING')
    return result


def stopped(rig, operation):
    rig.invoke('stop', {'operation_id': operation['operation_id']})
    until(lambda: rig.invoke('status', {'operation_id': operation['operation_id']})['released'])
    return rig.invoke('status', {'operation_id': operation['operation_id']})


@pytest.mark.parametrize('failure', ['disabled', 'missing_receipt', 'old_hash', 'broken_package', 'supervisor'])
def test_unqualified_or_broken_optional_adapter_never_releases_native(controlled, monkeypatch, failure):
    rig = controlled
    native = rig.session.schema.device
    expected = 'qualification_required'
    if failure == 'disabled':
        rig.config.enabled = False
        expected = 'integration_disabled'
    elif failure == 'missing_receipt':
        rig.config.receipts = ()
    elif failure == 'old_hash':
        rig.config.adapter_hashes[rig.definition.integration_id] = 'b' * 64
    elif failure == 'broken_package':
        rig.config.adapter_hashes.clear()
    else:
        rig.controller.supervisor = None
        expected = 'supervision_unavailable'
    code_from(lambda: rig.invoke('start', rig.start_request()), expected)
    assert rig.controller._runtime is None
    assert rig.session.schema.device is native and not native.closed
    assert not rig.supervisor.scopes and not (rig.controller.state_dir / 'work').exists()
    assert rig.manager.invoke(rig.session.session_id, rig.session.root_handle, 'get_time_now', {})[0] == 1.5
    page = rig.invoke('list', {})
    assert page['enabled'] is False and page['integrations'][0]['enabled'] is False


def test_worker_detects_adapter_change_before_native_release_or_resource_creation(controlled, monkeypatch):
    rig = controlled
    native = rig.session.schema.device
    monkeypatch.setattr(deployment, 'adapter_digest', lambda definition: 'b' * 64)
    operation = rig.invoke('start', rig.start_request())
    until(lambda: rig.invoke('status', {'operation_id': operation['operation_id']})['released'])
    result = rig.invoke('status', {'operation_id': operation['operation_id']})
    assert result['error'] == 'qualification_required'
    assert not native.closed and rig.session.schema.device is native
    assert not rig.supervisor.scopes and not (rig.controller.state_dir / 'work').exists()
    assert not rig.journal.records()


def test_start_action_stop_restores_generation_and_keeps_retry_identity(controlled):
    rig = controlled
    original_generation = rig.session.handles[rig.session.root_handle].generation
    request = rig.start_request()
    operation = running(rig)
    assert rig.session.schema.device is None
    action_request = dict(operation_id=operation['operation_id'], action='stats', parameters={}, request_key='action')
    action = rig.invoke('call', action_request)
    until(lambda: rig.invoke('status', {'operation_id': operation['operation_id'], 'action_id': action['action_id']})['state'] == 'SUCCEEDED')
    assert rig.invoke('call', action_request)['action_id'] == action['action_id']
    result = stopped(rig, operation)
    assert result['native_generation'] > original_generation
    assert result['native_capabilities']['uhd_version'] == 'test'
    assert rig.invoke('start', request)['operation_id'] == operation['operation_id']
    rig.adapter.start.assert_called_once()
    rig.adapter.call.assert_called_once()
    assert rig.supervisor.scopes[0].stops == 1
    assert rig.manager.invoke(rig.session.session_id, rig.session.root_handle, 'get_time_now', {})[0] == 1.5


def test_cancelled_before_release_can_retry_same_generation_with_new_resource_owner(controlled, monkeypatch):
    rig = controlled
    entered, proceed = Event(), Event()
    create = rig.supervisor.create
    def blocking_create(owner):
        result = create(owner)
        entered.set()
        assert proceed.wait(3)
        return result
    monkeypatch.setattr(rig.supervisor, 'create', blocking_create)
    original = rig.session.handles[rig.session.root_handle].generation
    first = rig.invoke('start', rig.start_request('first'))
    try:
        assert entered.wait(1)
        assert rig.journal.records()['usrp:serial:RADIO_ONE']['resources'] == rig.supervisor.scopes[0].owner
        rig.invoke('stop', {'operation_id': first['operation_id']})
    finally:
        proceed.set()
    until(lambda: rig.invoke('status', {'operation_id': first['operation_id']})['released'])
    assert rig.session.handles[rig.session.root_handle].generation == original
    second = running(rig, 'second')
    assert len(rig.supervisor.scopes) == 2
    assert rig.supervisor.scopes[0].owner != rig.supervisor.scopes[1].owner
    assert all((rig.controller.state_dir / 'work' / scope.owner).is_dir() for scope in rig.supervisor.scopes)
    stopped(rig, second)


def test_failed_native_admission_does_not_consume_owner_capacity(controlled):
    rig = controlled
    child = rig.manager.register_handle(rig.session, Native(), 'test.Child', parent=rig.session.root_handle)
    for index in range(20):
        code_from(lambda: rig.invoke('start', rig.start_request(f'rejected-{index}')), 'device_busy')
    assert not rig.controller._bindings and not rig.controller._operations
    assert not rig.supervisor.scopes and not rig.journal.records()
    rig.manager.close_handle(rig.session.session_id, child.handle)
    until(lambda: not rig.session.closing_handles)
    stopped(rig, running(rig, 'now-valid'))


def test_status_is_bounded_even_when_native_metadata_is_large(controlled):
    rig = controlled
    operation = running(rig)
    stopped(rig, operation)
    rig.session.capabilities['large_native_metadata'] = 'x' * 20000
    result = rig.invoke('status', {'operation_id': operation['operation_id']})
    assert result['released'] and result['native_capabilities_truncated'] is True
    assert 'native_capabilities' not in result


@pytest.mark.parametrize('method', ['status', 'stop', 'call'])
def test_operation_id_is_not_authority_for_another_session(controlled, monkeypatch, method):
    rig = controlled
    operation = running(rig)
    monkeypatch.setattr(v2, 'resolve_gid_for_token', lambda token: 4243)
    other = rig.manager.open('other')
    request = {'operation_id': operation['operation_id']}
    if method == 'call':
        request.update(action='stats', parameters={}, request_key='foreign')
    code_from(lambda: rig.manager.invoke(other.session_id, other.root_handle, 'integration_' + method,
                                         {'request': request}), 'unknown_operation')
    stopped(rig, operation)


@pytest.mark.parametrize('method', ['status', 'stop', 'call'])
def test_current_reservation_rechecked_for_every_operation(controlled, monkeypatch, method):
    rig = controlled
    operation = running(rig)
    request = {'operation_id': operation['operation_id']}
    if method == 'call':
        request.update(action='stats', parameters={}, request_key='late')
    monkeypatch.setattr(v2, 'token_matches_device', lambda *args: False)
    with pytest.raises(v2.RemoteRFSessionExpiredError):
        rig.invoke(method, request)
    until(lambda: rig.controller._runtime._operations[operation['operation_id']]['released'])
    assert not rig.journal.records() and rig.session.schema.device is None


def test_session_close_stops_resources_without_status_polling(controlled):
    rig = controlled
    operation = running(rig)
    scope = rig.supervisor.scopes[0]
    assert rig.manager.close(rig.session.session_id, wait=False)
    until(lambda: rig.controller._runtime._operations[operation['operation_id']]['released'])
    assert scope.stops == 1
    assert not rig.journal.records() and rig.session.schema.device is None


def test_close_while_waiting_for_controller_lock_returns_typed_session_failure(controlled):
    rig = controlled
    entered = Event()
    errors = []
    def call():
        entered.set()
        try:
            rig.invoke('list', {})
        except Exception as exc:
            errors.append(exc)
    with rig.controller._lock:
        worker = Thread(target=call)
        worker.start()
        assert entered.wait(1)
        # Calling close on this thread is reentrant for controller notification.
        rig.manager.close(rig.session.session_id, wait=False)
    worker.join(2)
    assert not worker.is_alive() and len(errors) == 1
    assert isinstance(errors[0], (v2.RemoteRFSessionExpiredError, v2.RemoteRFProtocolError))


def test_native_alias_and_legacy_inventory_reference_prevent_handoff(controlled, monkeypatch):
    rig = controlled
    rig.access.configure({4242: 'usrp:serial:RADIO_ONE', 4243: 'usrp:serial:RADIO_ONE'}, rig.journal)
    alias = Inventory()
    alias.device = Native()
    monkeypatch.setattr(v2, 'get_local_device_context', lambda gid: {'dtype': 'usrp', 'schema': rig.inventory if gid == 4242 else alias})
    code_from(lambda: rig.invoke('start', rig.start_request()), 'device_busy')
    assert not rig.journal.records() and rig.session.schema.device is not None
    alias.device = None
    stopped(rig, running(rig))


def test_claim_is_atomic_with_alias_publication(controlled, monkeypatch):
    rig = controlled
    rig.access.configure({4242: 'usrp:serial:RADIO_ONE', 4243: 'usrp:serial:RADIO_ONE'}, rig.journal)
    alias = Inventory()
    monkeypatch.setattr(v2, 'get_local_device_context', lambda gid: {'dtype': 'usrp', 'schema': rig.inventory if gid == 4242 else alias})
    gate = rig.access.gate(4242)
    reserve = gate.reserve
    attempted, completed = Event(), Event()
    outcome = []
    def alias_publish():
        attempted.set()
        try:
            with gate.native():
                alias.device = Native()
            outcome.append('published')
        except IntegrationError as exc:
            outcome.append(exc.code)
        completed.set()
    workers = []
    def racing_reserve(**kwargs):
        worker = Thread(target=alias_publish)
        workers.append(worker)
        worker.start()
        assert attempted.wait(1)
        # Before the fix, the alias could publish and drop its native ticket in
        # this gap. A single transition lock keeps publication behind reserve.
        completed.wait(0.03)
        return reserve(**kwargs)
    monkeypatch.setattr(gate, 'reserve', racing_reserve)
    operation = running(rig)
    for worker in workers:
        worker.join(2)
    assert outcome == ['device_owned'] and alias.device is None
    stopped(rig, operation)


def test_default_startup_opens_no_journal_or_adapter(tmp_path, monkeypatch):
    manager = SimpleNamespace(integrations=control.Controller())
    journal = Mock(side_effect=AssertionError('raw-only startup must not create a journal'))
    monkeypatch.setattr(control, 'Journal', journal)
    controller = control.initialize(manager, AccessTable(), {7: {'TYPE': 'usrp', 'INIT': {}}}, tmp_path)
    assert controller.journal is None and controller.capabilities()['reason'] == 'integration_disabled'
    journal.assert_not_called()


def test_broken_optional_configuration_keeps_raw_startup_working(tmp_path):
    (tmp_path / 'integrations.json').write_text('{"version":true}')
    manager = SimpleNamespace(integrations=control.Controller())
    access = AccessTable()
    controller = control.initialize(manager, access, {7: {'TYPE': 'usrp', 'INIT': {}}}, tmp_path)
    try:
        assert controller.capabilities()['reason'] == 'invalid_configuration'
        with access.native(7):
            pass
    finally:
        controller.close()


def test_orphan_owner_blocks_startup_even_after_configuration_removed(tmp_path):
    state = tmp_path / 'db' / 'integrations'
    journal = Journal(state / 'ownership')
    journal.claim('usrp:serial:RADIO_ONE', {'lease': 'lease', 'session': 'old', 'generation': 1, 'integration': 'org.example.adapter'})
    journal.close()
    manager = SimpleNamespace(integrations=control.Controller())
    with pytest.raises(IntegrationError) as caught:
        control.initialize(manager, AccessTable(), {}, tmp_path)
    assert caught.value.code == 'recovery_required'
    # Failed startup releases its writer lock, never erases the owner record.
    reopened = Journal(state / 'ownership')
    assert reopened.records()
    reopened.close()


def test_clean_controller_close_releases_journal_for_restart(tmp_path):
    (tmp_path / 'integrations.json').write_text('{"version":1,"enabled":false}')
    manager = SimpleNamespace(integrations=control.Controller())
    access = AccessTable()
    first = control.initialize(manager, access, {}, tmp_path)
    second = control.initialize(manager, access, {}, tmp_path)
    assert second is not first
    second.close()
    journal = Journal(tmp_path / 'db' / 'integrations' / 'ownership')
    journal.close()


def test_discovery_pages_all_bounded_identifiers_without_import(controlled, monkeypatch):
    rig = controlled
    definitions = [replace(rig.definition, integration_id=f'org.{chr(97 + n)}' + 'x' * 123) for n in range(12)]
    registry = Registry(definitions)
    monkeypatch.setattr(registry, 'create', Mock(side_effect=AssertionError('discovery cannot import')))
    rig.controller.registry = registry
    first = rig.invoke('list', {'limit': 8})
    assert len(first['integrations']) == 8 and len(first['next_cursor']) == 128
    second = rig.invoke('list', {'cursor': first['next_cursor']})
    assert len(second['integrations']) == 4 and second['next_cursor'] is None
    found = first['integrations'] + second['integrations']
    assert [row['id'] for row in found] == [definition.integration_id for definition in definitions]
    assert rig.invoke('describe', {'integration_id': found[-1]['id']})['integration']['id'] == found[-1]['id']
    registry.create.assert_not_called()


def test_start_status_and_stop_do_not_wait_for_journal_disk(controlled, monkeypatch):
    rig = controlled
    entered, proceed = Event(), Event()
    original = rig.journal.claim
    def slow_claim(*args):
        entered.set()
        assert proceed.wait(3)
        return original(*args)
    monkeypatch.setattr(rig.journal, 'claim', slow_claim)
    native = rig.session.schema.device
    operation = rig.invoke('start', rig.start_request())
    try:
        assert entered.wait(1)
        replies, done = [], Event()
        def poll_and_stop():
            replies.append(rig.invoke('status', {'operation_id': operation['operation_id']}))
            replies.append(rig.invoke('stop', {'operation_id': operation['operation_id']}))
            done.set()
        worker = Thread(target=poll_and_stop)
        worker.start()
        assert done.wait(0.5), 'RPC bookkeeping waited for journal I/O'
        worker.join(1)
        assert len(replies) == 2 and replies[0]['phase'] == 'persisting_owner'
        assert not native.closed and not rig.supervisor.scopes
    finally:
        proceed.set()
    until(lambda: rig.invoke('status', {'operation_id': operation['operation_id']})['released'])
    assert rig.session.schema.device is native and not rig.journal.records()


def test_hung_adapter_keeps_shutdown_bounded_and_ownership_fenced(controlled):
    rig = controlled
    rig.proceed.clear()
    operation = rig.invoke('start', rig.start_request())
    try:
        assert rig.entered.wait(1)
        rig.controller.shutdown()
        with pytest.raises(IntegrationError) as caught:
            rig.controller.close()
        assert caught.value.code == 'recovery_required'
        assert rig.access.blocked(rig.session.gid) and rig.journal.records()
        assert rig.invoke('status', {'operation_id': operation['operation_id']})['released'] is False
        rig.adapter.stop.assert_not_called()
    finally:
        rig.proceed.set()
    until(lambda: rig.invoke('status', {'operation_id': operation['operation_id']})['released'])


def test_authority_watcher_cleans_without_any_followup_rpc(controlled, monkeypatch):
    rig = controlled
    operation = running(rig)
    monkeypatch.setattr(v2, 'token_matches_device', lambda *args: False)
    until(lambda: rig.controller._runtime._operations[operation['operation_id']]['released'])
    assert not rig.journal.records() and rig.session.schema.device is None
    rig.adapter.stop.assert_called_once()


def test_action_id_is_bound_to_its_operation(controlled):
    rig = controlled
    first = running(rig, 'first')
    action = rig.invoke('call', {'operation_id': first['operation_id'], 'action': 'stats', 'parameters': {}, 'request_key': 'a'})
    until(lambda: rig.invoke('status', {'operation_id': first['operation_id'], 'action_id': action['action_id']})['state'] == 'SUCCEEDED')
    stopped(rig, first)
    second = running(rig, 'second')
    code_from(lambda: rig.invoke('status', {'operation_id': second['operation_id'], 'action_id': action['action_id']}), 'unknown_action')
    stopped(rig, second)


def test_server_shutdown_closes_application_runtime_even_when_inventory_fails(monkeypatch):
    from remoteRF_server.server import grpc_server as server
    manager = Mock()
    local = Mock(side_effect=RuntimeError('native cleanup failed'))
    monkeypatch.setattr(server, 'close_local_devices', local)
    monkeypatch.setattr(server, 'session_manager', manager)
    monkeypatch.setattr(server, 'host_session_proxy', Mock())
    monkeypatch.setattr(server, 'direct_path', Mock())
    monkeypatch.setattr(server, 'transport_counters', Mock())
    runtime = server.ServerRuntime(Mock(), None, Event(), None, '127.0.0.1', 0, 0)
    runtime.stop()
    manager.close_all.assert_called_once_with(stop_admission=True)
    manager.integrations.close.assert_called_once()


def test_reload_rejects_new_physical_alias_before_inventory_acquisition(tmp_path, monkeypatch):
    from remoteRF_server.server import device_manager as devices
    # A metadata-only fixture suffices: unknown adapters leave no start path.
    (tmp_path / 'integrations.json').write_text(json.dumps({'version': 1, 'enabled': True,
        'devices': {'7': {'serial': 'RADIO_ONE', 'integrations': []}}}))
    records = {7: {'TYPE': 'usrp', 'INIT': {'serial': 'RADIO_ONE'}}}
    manager = SimpleNamespace(integrations=control.Controller())
    access = AccessTable()
    controller = control.initialize(manager, access, records, tmp_path)
    monkeypatch.setattr(v2, 'session_manager', manager)
    monkeypatch.setattr(devices, '_load_device_records', lambda: {**records, 8: {'TYPE': 'usrp', 'INIT': {'serial': 'RADIO_ONE'}}})
    acquire = Mock(side_effect=AssertionError('changed mapping must fail before native inventory opens'))
    monkeypatch.setattr(devices, '_reload_devices_unfenced', acquire)
    try:
        with pytest.raises(IntegrationError) as caught:
            devices._reload_devices_locked()
        assert caught.value.code == 'inventory_changed'
        acquire.assert_not_called()
    finally:
        controller.close()


@pytest.mark.parametrize('error', ['recovery_required', 'journal_busy'])
def test_unresolved_supervisor_state_blocks_native_startup(tmp_path, monkeypatch, error):
    from remoterf_device_core.integrations import supervision
    (tmp_path / 'integrations.json').write_text(json.dumps({'version': 1, 'enabled': False,
                                                         'cgroup_root': str(tmp_path / 'cgroup')}))
    monkeypatch.setattr(supervision, 'CgroupSupervisor', Mock(side_effect=IntegrationError(error, 'orphan resources')))
    manager = SimpleNamespace(integrations=control.Controller())
    with pytest.raises(IntegrationError) as caught:
        control.initialize(manager, AccessTable(), {}, tmp_path)
    assert caught.value.code == 'recovery_required'
    journal = Journal(tmp_path / 'db' / 'integrations' / 'ownership')
    journal.close()


def test_cleanup_scheduler_refusal_preserves_scope_for_retry(controlled, monkeypatch):
    rig = controlled
    stopped(rig, running(rig))
    rig.manager.close(rig.session.session_id, wait=False)
    until(lambda: not rig.manager._closing_sessions)
    with monkeypatch.context() as patch:
        patch.setattr(control, 'submit_cleanup', Mock(side_effect=RuntimeError('cleanup capacity exhausted')))
        try:
            rig.controller._reap()
        except (RuntimeError, IntegrationError):
            pass
        assert rig.controller._bindings, 'refused cleanup must not forget its resource owner'
    rig.controller._reap()
    until(lambda: all(scope.closed for scope in rig.supervisor.scopes))
    # Scope.close can finish just before the cleanup worker publishes done.
    # Reaping is intentionally forbidden until that completion proof arrives.
    until(lambda: all(job.done.is_set() for job in rig.controller._pending_closes.values()))
    rig.controller._reap()
    assert not rig.controller._bindings


def test_qualified_discovery_reports_only_profiles_with_matching_evidence(controlled, monkeypatch):
    rig = controlled
    definition = replace(rig.definition, profiles=('rx', 'unqualified'))
    registry = Registry([definition], enabled={str(rig.session.gid): [definition.integration_id]})
    rig.config.registry = rig.controller.registry = registry
    no_io = Mock(side_effect=AssertionError('discovery must use cached qualification identity'))
    monkeypatch.setattr(deployment, 'adapter_digest', no_io)
    monkeypatch.setattr(control, 'code_digest', no_io)
    result = rig.invoke('list', {})
    assert result['enabled'] is True and result['reason'] is None
    assert result['integrations'][0]['qualified_profiles'] == ['rx']
    assert result['integrations'][0]['enabled'] is True
    assert rig.controller.capabilities()['enabled'] is False
    rig.controller.shutdown()
    result = rig.invoke('list', {})
    assert result['enabled'] is False and result['integrations'][0]['qualified_profiles'] == []
    no_io.assert_not_called()


def test_hung_scope_close_does_not_block_discovery_and_close_has_deadline(controlled, monkeypatch):
    rig = controlled
    stopped(rig, running(rig))
    deferred = next(iter(rig.controller._bindings.values()))[1]
    close = deferred.close
    entered, proceed = Event(), Event()
    def slow_close():
        entered.set()
        assert proceed.wait(3)
        close()
    monkeypatch.setattr(deferred, 'close', slow_close)
    rig.manager.close(rig.session.session_id, wait=False)
    until(lambda: not rig.manager._closing_sessions)
    next_session = rig.manager.open('next-holder')
    def list_for_next_holder():
        return rig.manager.invoke(next_session.session_id, next_session.root_handle, 'integration_list', {'request': {}})[0]
    try:
        assert list_for_next_holder()['integrations']
        assert entered.wait(1)
        done = Event()
        worker = Thread(target=lambda: (list_for_next_holder(), done.set()))
        worker.start()
        assert done.wait(0.5), 'discovery waited for filesystem cleanup'
        worker.join(1)
        started = time.monotonic()
        with pytest.raises(IntegrationError) as caught:
            rig.controller.close()
        assert caught.value.code == 'recovery_required'
        assert time.monotonic() - started < 1.5
        assert rig.controller._pending_closes and not rig.supervisor.closed
    finally:
        proceed.set()
    until(lambda: all(job.done.is_set() for job in rig.controller._pending_closes.values()))
    rig.controller._reap()
    assert not rig.controller._pending_closes and not rig.controller._bindings


def test_failed_scope_close_is_retained_without_automatic_destructive_retry():
    controller = control.Controller()
    controller._runtime = Mock()
    controller._runtime.drained.return_value = True
    controller._runtime.operations.return_value = []
    binding = SimpleNamespace(authority=lambda: False)
    scope = Mock()
    scope.close.side_effect = RuntimeError('operator reconciliation required')
    key = ('old-session', 1, 'start')
    controller._bindings[key] = (binding, scope)
    controller._reap()
    job = controller._pending_closes[key]
    assert job.done.wait(1)
    for _ in range(3):
        controller._reap()
    with pytest.raises(IntegrationError) as caught:
        controller.close()
    assert caught.value.code == 'recovery_required'
    scope.close.assert_called_once()
    assert controller._bindings[key][0] is binding
    assert controller._pending_closes[key].error is not None
    controller._runtime.retire.assert_not_called()
