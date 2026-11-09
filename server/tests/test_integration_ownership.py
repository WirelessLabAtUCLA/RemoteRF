"""Server binding checks using qualified fake hardware, never a physical radio."""
from unittest.mock import Mock

import pytest

from test_native_lifetime import opened, until
from remoteRF_server.server import dynamic_v2 as v2
from remoterf_device_core.integrations import IntegrationError
from remoterf_device_core.integrations.ownership import Journal, Gate
from remoterf_device_core.integrations.runtime import Runtime


@pytest.fixture
def integration(opened, tmp_path, monkeypatch):
    manager, session, inventory = opened
    journal = Journal(tmp_path / 'owners')
    gate = Gate('qualified-serial:fake', journal)
    monkeypatch.setitem(v2.hardware_access._gates, session.gid, gate)
    alive = {'app': False}
    adapter = Mock()
    adapter.validate.side_effect = lambda profile, configuration: configuration
    adapter.start.side_effect = lambda *args: alive.update(app=True)
    adapter.stop.side_effect = lambda: alive.update(app=False)
    adapter.status.side_effect = lambda: {'ready': alive['app']}
    registry = Mock()
    registry.validate_start.side_effect = lambda *args, **kwargs: kwargs['configuration']
    registry.create.return_value = adapter
    runtime = Runtime(registry, workers=1, capacity=4)
    binding = manager.integration_binding(session.session_id, work_dir=tmp_path, resources_released=lambda: not alive['app'])
    yield manager, session, runtime, binding, adapter
    runtime.shutdown()
    until(lambda: all(row['released'] and not row['cleanup'] for row in runtime._operations.values()))
    runtime.close()
    journal.close()


def test_child_handles_refuse_claim_without_interrupting_native_use(integration):
    manager, session, runtime, binding, adapter = integration
    native = Mock()
    entry = manager.register_handle(session, native, 'test.Child', parent=session.root_handle)
    with pytest.raises(IntegrationError) as caught:
        runtime.start(binding, 'org.example.test', 'test', {}, request_key='start')
    assert caught.value.code == 'device_busy'
    assert not binding.gate.snapshot()['blocked']
    assert entry.value is native and not entry.closed
    assert manager.invoke(session.session_id, session.root_handle, 'get_time_now', {})[0] == 1.5
    adapter.start.assert_not_called()


def test_handoff_drops_native_root_and_refreshes_generation_on_return(integration):
    manager, session, runtime, binding, adapter = integration
    original = session.schema.device
    generation = session.handles[session.root_handle].generation
    op = runtime.start(binding, 'org.example.test', 'test', {}, request_key='start')['operation_id']
    until(lambda: runtime.status(binding, op)['state'] == 'RUNNING')
    assert original.closed and session.schema.device is None
    assert session.handles[session.root_handle].value is None
    with pytest.raises(IntegrationError):
        manager.invoke(session.session_id, session.root_handle, 'get_time_now', {})
    with pytest.raises(IntegrationError):
        manager.open('token')
    runtime.stop(binding, op)
    until(lambda: runtime.status(binding, op)['released'])
    assert session.schema.device is not original
    assert session.handles[session.root_handle].generation > generation
    assert manager.invoke(session.session_id, session.root_handle, 'get_time_now', {})[0] == 1.5
    with pytest.raises(IntegrationError) as caught:
        runtime.start(binding, 'org.example.test', 'test', {}, request_key='stale-start')
    assert caught.value.code == 'stale_owner'


def test_session_close_reclaims_application_before_next_open(integration):
    manager, session, runtime, binding, adapter = integration
    op = runtime.start(binding, 'org.example.test', 'test', {}, request_key='start')['operation_id']
    until(lambda: runtime.status(binding, op)['state'] == 'RUNNING')
    manager.close(session.session_id)
    runtime.poll()
    until(lambda: runtime.status(binding, op)['released'])
    adapter.stop.assert_called_once()
    assert session.schema.device is None
    assert manager.open('token')


def test_default_inventory_cannot_enable_application_handoffs(opened, tmp_path):
    manager, session, inventory = opened
    with pytest.raises(IntegrationError) as caught:
        manager.integration_binding(session.session_id, work_dir=tmp_path, resources_released=lambda: True)
    assert caught.value.code == 'unsupported'


def test_owned_radio_blocks_legacy_reload_availability_and_defers_reset(integration, monkeypatch):
    from remoteRF_server.server import device_manager as dm
    from remoteRF_server.server.rpc_manager import RpcManager
    manager, session, runtime, binding, adapter = integration
    inventory = v2.get_local_device_context(session.gid)['schema']
    inventory.dispatch = Mock()
    inventory.reset_for_next_holder = Mock()
    state = dm._DeviceState(dev=inventory, origin='local', online=True, salt='', hsh='',
                           name='fake', ident='fake', dtype='usrp', reset_pending=True)
    monkeypatch.setattr(v2, 'session_manager', manager)
    monkeypatch.setattr(dm, '_devices', {session.gid: state})
    monkeypatch.setattr(dm, '_sync_host_devices', lambda *args, **kwargs: None)
    op = runtime.start(binding, 'org.example.test', 'test', {}, request_key='start')['operation_id']
    until(lambda: runtime.status(binding, op)['state'] == 'RUNNING')
    assert not dm.device_is_available(session.gid)
    with pytest.raises(IntegrationError):
        with dm.acquire_device_by_gid(session.gid):
            pass
    with pytest.raises(IntegrationError):
        RpcManager()._dispatch_local_schema(function_name='Usrp:rate:GET', args={}, dev=inventory, gid=session.gid)
    with pytest.raises(IntegrationError):
        dm.reload_devices()
    inventory.dispatch.assert_not_called()
    dm._reset_if_pending(session.gid)
    inventory.reset_for_next_holder.assert_not_called()
    assert state.reset_pending
    # End authority first: cleanup must release the app before reset/next owner.
    manager.close(session.session_id)
    runtime.poll()
    until(lambda: runtime.status(binding, op)['released'])
    until(lambda: inventory.reset_for_next_holder.call_count == 1)
