"""Native lifetime races, independent of UHD hardware or application packages."""
from threading import Event, Thread
import time
from unittest.mock import Mock

import pytest

from remoteRF_server.server import dynamic_v2 as v2
from remoteRF_server.drivers.usrp.uhd_manifest import build_usrp_schema_v2


def until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('native cleanup did not complete')


class Native:
    def __init__(self):
        self.closed = False
    def close(self):
        self.closed = True


class Inventory:
    device = None
    def get_idl_v2(self):
        return build_usrp_schema_v2()
    def create_session_schema(self):
        live = Mock()
        live.device = Native()
        live.build_session_metadata_v2.return_value = {'uhd_version': 'test'}
        live.invoke_native.return_value = 1.5
        def close():
            if live.device is not None:
                live.device.close()
                live.device = None
        live.close_native_device.side_effect = close
        return live


@pytest.fixture
def opened(monkeypatch):
    manager = v2.SessionManager()
    inventory = Inventory()
    monkeypatch.setattr(v2, 'resolve_gid_for_token', lambda token: 4242)
    monkeypatch.setattr(v2, 'token_matches_device', lambda *args: True)
    monkeypatch.setattr(v2, 'get_local_device_context', lambda gid: {'schema': inventory, 'dtype': 'usrp'})
    session = manager.open('token')
    yield manager, session, inventory
    manager.close_all()
    until(lambda: not manager._closing_sessions)


def test_expiry_after_waiting_for_control_lock_does_not_dispatch(opened):
    manager, session, inventory = opened
    started = Event()
    errors = []
    def invoke():
        started.set()
        try:
            manager.invoke(session.session_id, session.root_handle, 'get_time_now', {})
        except Exception as exc:
            errors.append(exc)
    with session.control_lock:
        thread = Thread(target=invoke)
        thread.start()
        assert started.wait(1)
        manager.close(session.session_id, wait=False)
    thread.join(2)
    assert not thread.is_alive() and errors
    session.schema.invoke_native.assert_not_called()


def test_late_child_publication_is_disposed_before_hardware_becomes_free(opened):
    manager, session, inventory = opened
    entered, finish = Event(), Event()
    child = Native()
    native = session.schema.device
    def invoke(*args, **kwargs):
        entered.set()
        assert finish.wait(2)
        return child
    session.schema.invoke_native.side_effect = invoke
    errors = []
    def call():
        try:
            manager.invoke(session.session_id, session.root_handle, 'get_tree', {})
        except Exception as exc:
            errors.append(exc)
    thread = Thread(target=call)
    thread.start()
    try:
        assert entered.wait(1)
        manager.close(session.session_id, wait=False)
        assert manager.cleanup_pending(4242)
        assert not native.closed
        with pytest.raises(v2.RemoteRFReservationError):
            manager.open('token')
    finally:
        finish.set()
        thread.join(2)
    until(lambda: not manager.cleanup_pending(4242))
    assert errors and child.closed and native.closed
    assert not session.handles and not session.closing_handles


@pytest.mark.parametrize('worker_field', ['rx_worker', 'worker'])
def test_closed_handle_retains_live_worker_and_blocks_handoff(opened, worker_field):
    manager, session, inventory = opened
    entered, finish = Event(), Event()
    resource = Native()
    entry = manager.register_handle(session, resource, 'test.Child', parent=session.root_handle)
    def work():
        entered.set()
        finish.wait()
    thread = Thread(target=work)
    setattr(entry, worker_field, thread)
    thread.start()
    assert entered.wait(1)
    try:
        assert manager.close_handle(session.session_id, entry.handle)
        assert manager.close_handle(session.session_id, entry.handle)
        assert entry.handle in session.closing_handles
        assert not resource.closed
        assert manager.invoke(session.session_id, session.root_handle, 'get_time_now', {})[0] == 1.5
    finally:
        finish.set()
        thread.join(2)
    until(lambda: not session.closing_handles)
    assert resource.closed and entry.value is None


def test_reservation_end_during_open_never_publishes_live_session(opened):
    manager, previous, inventory = opened
    manager.close(previous.session_id)
    entered, finish = Event(), Event()
    create = inventory.create_session_schema
    created, errors = [], []
    def opening():
        live = create()
        created.append(live)
        entered.set()
        assert finish.wait(2)
        return live
    inventory.create_session_schema = opening
    def run():
        try:
            manager.open('token')
        except Exception as exc:
            errors.append(exc)
    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(1)
        manager.close_gid(4242, wait=False)
    finally:
        finish.set()
        thread.join(2)
    until(lambda: not manager._closing_sessions)
    assert errors and not manager._sessions and not manager._opening_gids
    assert created[0].device is None


def test_failed_open_cleanup_cannot_poison_opening_slot(opened):
    manager, previous, inventory = opened
    manager.close(previous.session_id)
    create = inventory.create_session_schema
    broken = create()
    broken.build_session_metadata_v2.side_effect = ValueError('original metadata error')
    broken.close_native_device.side_effect = RuntimeError('secondary cleanup error')
    inventory.create_session_schema = lambda: broken
    with pytest.raises(ValueError, match='original metadata error'):
        manager.open('token')
    until(lambda: not manager._closing_sessions)
    assert not manager._opening_gids
    inventory.create_session_schema = create
    assert manager.open('token')  # preserve the raw-only reopen-after-error policy


def test_shutdown_attempts_every_device_despite_close_failure(opened, monkeypatch):
    manager, first, inventory = opened
    first.schema.close_native_device.side_effect = RuntimeError('failed close')
    monkeypatch.setattr(v2, 'resolve_gid_for_token', lambda token: 4243)
    second = manager.open('other')
    native = second.schema.device
    assert manager.close_all(stop_admission=True) == 2
    until(lambda: not manager._closing_sessions)
    assert native.closed
    with pytest.raises(v2.RemoteRFReservationError, match='stopping'):
        manager.open('other')


def test_reservation_reset_waits_for_closing_native_calls(opened, monkeypatch):
    from remoteRF_server.server import device_manager as dm
    manager, session, inventory = opened
    monkeypatch.setattr(v2, 'session_manager', manager)
    reset = Mock()
    inventory.reset_for_next_holder = reset
    state = dm._DeviceState(dev=inventory, origin='local', online=True, salt='', hsh='',
                            name='fake', ident='fake', dtype='usrp', reset_pending=True)
    monkeypatch.setitem(dm._devices, 4242, state)
    entered, finish = Event(), Event()
    def native():
        with manager.admitted(session, session.handles[session.root_handle]):
            entered.set()
            finish.wait()
    thread = Thread(target=native)
    thread.start()
    try:
        assert entered.wait(1)
        manager.close(session.session_id, wait=False)
        dm._reset_if_pending(4242)
        reset.assert_not_called()
        assert state.reset_pending
    finally:
        finish.set()
        thread.join(2)
    until(lambda: reset.call_count == 1)
    assert not state.reset_pending


def test_shutdown_failure_still_attempts_other_cleanup_and_releases_writer_lock(monkeypatch):
    from remoteRF_server.server import grpc_server as server
    transport = Mock()
    transport.stop.side_effect = RuntimeError('transport stop failed')
    local = Mock(side_effect=RuntimeError('native cleanup failed'))
    monkeypatch.setattr(server, 'close_local_devices', local)
    monkeypatch.setattr(server, 'session_manager', Mock())
    monkeypatch.setattr(server, 'host_session_proxy', Mock())
    monkeypatch.setattr(server, 'direct_path', Mock())
    monkeypatch.setattr(server, 'transport_counters', Mock())
    writer, federation, control = Mock(), Mock(), Mock()
    runtime = server.ServerRuntime(transport, None, Event(), None, '127.0.0.1', 0, 0,
                                   writer_lock=writer, federation_devices=federation, control_server=control)
    with pytest.raises(RuntimeError, match='transport stop failed'):
        runtime.stop()
    writer.close.assert_called_once()
    federation.close.assert_called_once()
    control.stop.assert_called_once()
    local.assert_called_once()


def test_restart_waits_for_native_drain_and_then_allows_open(opened):
    manager, session, inventory = opened
    with session.activity.use():
        manager.close_all(stop_admission=True)
        with pytest.raises(v2.RemoteRFReservationError, match='previous runtime'):
            manager.resume_admission()
        assert session.retired_handles
        assert not session.schema.device.closed
    until(lambda: not manager._closing_sessions)
    assert not session.retired_handles
    manager.resume_admission()
    assert manager.open('token')


def test_cleanup_scheduler_failure_retains_native_references(opened, monkeypatch):
    manager, session, inventory = opened
    native = session.schema.device
    with monkeypatch.context() as patch:
        patch.setattr(v2, 'submit_cleanup', Mock(side_effect=RuntimeError('capacity exhausted')))
        assert manager.close_all(stop_admission=True) == 1
        assert manager.cleanup_pending(4242)
        assert session.retired_handles[0].value is native
        assert not native.closed
        with pytest.raises(v2.RemoteRFReservationError):
            manager.resume_admission()
    # Explicit test-only reconciliation of the failed scheduler admission.
    session.schema.close_native_device()
    session.retired_handles.clear()
    manager._closing_sessions.pop(session.session_id)


def test_inventory_shutdown_waits_for_native_use_and_attempts_other_devices(monkeypatch):
    from remoteRF_server.server import device_manager as dm
    first, second = Inventory().create_session_schema(), Inventory().create_session_schema()
    def state(schema):
        return dm._DeviceState(dev=schema, origin='local', online=True, salt='', hsh='',
                               name='fake', ident='fake', dtype='usrp')
    monkeypatch.setattr(dm, '_devices', {4242: state(first), 4243: state(second)})
    with dm.hardware_access.native(4242):
        assert dm.close_local_devices() == 1
        assert first.device is not None
        assert second.device is None
    until(lambda: first.device is None)


def test_hung_inventory_close_does_not_block_other_shutdown_work(monkeypatch):
    from remoteRF_server.server import device_manager as dm
    first, second = Inventory().create_session_schema(), Inventory().create_session_schema()
    entered, finish = Event(), Event()
    def stuck():
        entered.set()
        finish.wait()
        first.device = None
    first.close_native_device.side_effect = stuck
    def state(schema):
        return dm._DeviceState(dev=schema, origin='local', online=True, salt='', hsh='',
                               name='fake', ident='fake', dtype='usrp')
    monkeypatch.setattr(dm, '_devices', {4242: state(first), 4243: state(second)})
    try:
        assert dm.close_local_devices() == 1
        assert entered.is_set()
        assert dm.hardware_access.blocked(4242)
        assert second.device is None
    finally:
        finish.set()
    until(lambda: not dm.hardware_access.blocked(4242))


@pytest.mark.parametrize('change', ['authority', 'inventory'])
def test_open_cannot_switch_device_after_native_admission(opened, monkeypatch, change):
    manager, previous, inventory = opened
    create = Mock(wraps=inventory.create_session_schema)
    inventory.create_session_schema = create
    if change == 'authority':
        monkeypatch.setattr(v2, 'resolve_gid_for_token', Mock(side_effect=[4242, 4243]))
    else:
        monkeypatch.setattr(v2, 'get_local_device_context', Mock(side_effect=[
            {'schema': inventory, 'dtype': 'usrp'},
            {'schema': Inventory(), 'dtype': 'usrp'},
        ]))
    with pytest.raises(v2.RemoteRFReservationError, match='changed; retry open'):
        manager.open('token')
    create.assert_not_called()
    assert not previous.closed and not previous.schema.device.closed
