"""Qualification harness plumbing tests; deliberately no hardware claims."""
import importlib.util
import json
from pathlib import Path
import sys
import time

import pytest

HARNESS = Path(__file__).resolve().parents[1] / 'usrp_handoff.py'
spec = importlib.util.spec_from_file_location('usrp_handoff_qualification', HARNESS)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def provider(tmp_path, value):
    command = tmp_path / 'authority'
    command.write_text('#!' + sys.executable + '\nimport json\nprint(' + repr(json.dumps(value)) + ')\n')
    command.chmod(0o700)
    return str(command)


def test_authority_is_exact_fresh_and_expiring(tmp_path):
    command = provider(tmp_path, dict(active=True, reservation_id='booking', device_id=40,
                                     serial='ABC123', expires_at=time.time() + 60))
    authority = harness.Authority(command, 'booking', 40, 'ABC123')
    try:
        assert authority.active()
        assert authority.last['reservation_id'] == 'booking'
        authority._valid_until = time.monotonic() - 1
        assert not authority.active()
    finally:
        authority.close()


@pytest.mark.parametrize('changed', [dict(active=False), dict(device_id=41), dict(serial='OTHER'),
                                   dict(reservation_id='another'), dict(expires_at=0)])
def test_wrong_or_expired_reservation_refuses_all_hardware_work(tmp_path, changed):
    value = dict(active=True, reservation_id='booking', device_id=40,
                 serial='ABC123', expires_at=time.time() + 60)
    value.update(changed)
    with pytest.raises(RuntimeError, match='not active'):
        harness.Authority(provider(tmp_path, value), 'booking', 40, 'ABC123')


def test_invalid_or_excessive_authority_output_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match='not active'):
        harness.Authority(provider(tmp_path, {'data': 'x' * 5000}), 'booking', 40, 'ABC123')


def test_evidence_does_not_become_an_activation_approval(tmp_path):
    target = tmp_path / 'evidence.json'
    value = {'passed': True, 'activation_changed': False, 'checks': {'fake_fixture_only': True}}
    harness.write_evidence(target, value)
    assert json.loads(target.read_text()) == value
    assert target.stat().st_mode & 0o777 == 0o600
    assert set(tmp_path.iterdir()) == {target}


def test_serial_lock_refuses_another_owner_and_accepts_inherited_descriptor():
    import os
    from uuid import uuid4
    serial = 'TEST' + uuid4().hex
    fd = harness.acquire_radio_lock(serial)
    try:
        with pytest.raises(BlockingIOError):
            harness.acquire_radio_lock(serial.lower())
        assert harness.acquire_radio_lock(serial, fd) == fd
    finally:
        os.close(fd)
    replacement = harness.acquire_radio_lock(serial)
    os.close(replacement)


@pytest.mark.parametrize('events,processes,expected', [
    ('populated 1\nfrozen 0\n', '123\n456\n', (1, 2)),
    ('populated 0\nfrozen 0\n', '', (0, 0)),
])
def test_positive_scope_population_is_read_only_and_omits_pids(tmp_path, events, processes, expected):
    import os
    from threading import RLock
    from types import SimpleNamespace
    from unittest.mock import Mock
    (tmp_path / 'cgroup.events').write_text(events)
    (tmp_path / 'cgroup.procs').write_text(processes)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    verify = Mock()
    try:
        scope = SimpleNamespace(_fd=fd, _lock=RLock(), _verify_identity=verify)
        observed = harness.observe_scope_population(scope)
        assert observed == {'scope_identity_verified': True, 'kernel_populated': expected[0],
                            'process_count': expected[1]}
        assert verify.call_count == 2
        assert (tmp_path / 'cgroup.events').read_text() == events
        assert (tmp_path / 'cgroup.procs').read_text() == processes
    finally:
        os.close(fd)


@pytest.mark.parametrize('events,processes', [
    ('frozen 0\n', '123\n'), ('populated 2\n', '123\n'),
    ('populated 1\npopulated 0\n', '123\n'), ('populated 1\n', 'not-a-pid\n'),
    ('populated 1\n', '0\n'), ('populated 1\n', '1\n' * 3000),
])
def test_unverified_population_cannot_be_positive_process_evidence(tmp_path, events, processes):
    import os
    from threading import RLock
    from types import SimpleNamespace
    from remoterf_device_core.integrations import IntegrationError
    (tmp_path / 'cgroup.events').write_text(events)
    (tmp_path / 'cgroup.procs').write_text(processes)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        scope = SimpleNamespace(_fd=fd, _lock=RLock(), _verify_identity=lambda: None)
        with pytest.raises((RuntimeError, IntegrationError)):
            harness.observe_scope_population(scope)
    finally:
        os.close(fd)


def test_scope_population_refuses_identity_failure_before_reading(monkeypatch):
    from threading import RLock
    from types import SimpleNamespace
    from unittest.mock import Mock
    from remoterf_device_core.integrations import supervision
    read = Mock(side_effect=AssertionError('unverified cgroup must not be read'))
    monkeypatch.setattr(supervision, '_read_control', read)
    scope = SimpleNamespace(_fd=-1, _lock=RLock(),
                            _verify_identity=Mock(side_effect=RuntimeError('identity changed')))
    with pytest.raises(RuntimeError, match='identity changed'):
        harness.observe_scope_population(scope)
    read.assert_not_called()


def test_restart_observation_failure_still_cleans_verified_scope(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from remoterf_device_core.integrations import supervision
    from remoterf_device_core.integrations.ownership import Gate, Journal
    root = HARNESS.parents[2]
    monkeypatch.syspath_prepend(str(root / 'examples/integrations/usrp_probe/src'))
    state = tmp_path / 'restart-case'
    state.mkdir()
    (state / 'orphan-ready.json').write_text('{}')
    journal = Journal(state / 'radio-owners')
    journal.claim('fake:serial', dict(lease='lease', session='old', generation=1,
                                    integration='org.remoterf.usrp_probe', resources='owner'))
    journal.close()
    stopped = []
    scope = SimpleNamespace(stop=lambda **kwargs: stopped.append('verified-stop') or True,
                            resources_released=lambda: bool(stopped), close=lambda: stopped.append('scope-close'))
    class Supervisor:
        def __init__(self, *a, **k): pass
        def describe(self): return {'owners': {'owner': {}}}
        def reopen(self, owner): assert owner == 'owner'; return scope
        def close(self): assert stopped
    monkeypatch.setattr(supervision, 'CgroupSupervisor', Supervisor)
    monkeypatch.setattr(harness.subprocess, 'Popen', lambda *a, **k: SimpleNamespace(wait=lambda **kwargs: 71))
    monkeypatch.setattr(harness, 'observe_scope_population', Mock(side_effect=OSError('read failed')))
    def configure(args, authority, stack, journal):
        gate = Gate('fake:serial', journal)
        class Manager:
            def open(self, token):
                with gate.native():
                    assert stopped
                    return object()
            def close_all(self, **kwargs): pass
            def cleanup_pending(self, gid): return False
        return Manager(), {'token': 'fake-token'}, gate
    monkeypatch.setattr(harness, 'configure_local', configure)
    monkeypatch.setattr(harness, 'receive_native', lambda *a, **k: {'received': 4096, 'fake_hardware': True})
    args = SimpleNamespace(state_dir=tmp_path, cgroup_root=tmp_path, serial='FAKE123', device_id=40,
        reservation_id='booking', authority_command=tmp_path / 'authority', profile='usrp2901',
        rate=1e6, frequency=100e6, gain=0, _lock_fd=0)
    evidence = {'checks': {}}
    with pytest.raises(RuntimeError, match='no surviving process'):
        harness.run_restart(args, SimpleNamespace(active=lambda: True), evidence)
    assert stopped == ['verified-stop', 'scope-close']
    proof = evidence['checks']['abrupt_restart_recovery']
    assert proof['verified_cgroup_cleanup'] and proof['journal_cleared']
    assert proof['surviving_process_detected'] is False
    assert proof['scope_before_cleanup']['observation_error'] == 'OSError'
    check = Journal(state / 'radio-owners')
    try:
        assert check.records() == {}
    finally:
        check.close()


def test_lifecycle_orchestration_with_fake_processes_never_claims_hardware_proof(tmp_path, monkeypatch):
    """Exercise every normal/fault branch; mocks are explicitly not qualification."""
    from types import SimpleNamespace
    from threading import Event
    from remoterf_device_core.integrations.ownership import Gate
    from remoterf_device_core.integrations.runtime import Binding
    from remoterf_device_core.integrations import supervision
    sample = HARNESS.parents[2] / 'examples/integrations/usrp_probe/src'
    monkeypatch.syspath_prepend(str(sample))
    monkeypatch.setattr(harness, 'distribution', lambda name: SimpleNamespace(locate_file=lambda path: sample / path))

    class Scope:
        def __init__(self):
            self.alive = False
            self.required = False
        def launch(self, *args, **kwargs):
            self.alive = True
            self.required = kwargs.get('required_while_running', False)
            return 'fake-child'
        def resource_failure(self):
            return 'process_exited' if self.required and not self.alive else None
        def snapshot(self):
            return {'processes': [{'id': 'fake-child', 'returncode': None if self.alive else 1}]}
        def log_tail(self, *args):
            return b'RRF_USRP_PROBE {"event":"ready","serial":"FAKE123","received":4096,"rate":1000000,"frequency":100000000,"gain":0,"channel":0,"uhd_version":"fake"}\n'
        def stop(self, *args, **kwargs):
            self.alive = False
            return True
        def resources_released(self):
            return not self.alive
        def close(self):
            assert not self.alive
    class Supervisor:
        def __init__(self, *args, **kwargs):
            self.scopes = []
        def create(self, owner):
            scope = Scope()
            self.scopes.append(scope)
            return scope
        def close(self):
            assert all(not scope.alive for scope in self.scopes)
    monkeypatch.setattr(supervision, 'CgroupSupervisor', Supervisor)
    gates = []
    def configure(args, authority, stack, journal):
        gate = Gate('fake:serial', journal)
        gates.append(gate)
        local = {'enabled': True, 'token': 'local'}
        class Manager:
            def __init__(self):
                self.sessions = []
            def open(self, token):
                assert token == local['token'] and local['enabled'] and authority.active()
                with gate.native():
                    session = SimpleNamespace(session_id=str(len(self.sessions)), closed=False,
                        schema=SimpleNamespace(device=object()), root_handle='root',
                        handles={'root': SimpleNamespace(generation=1)})
                    self.sessions.append(session)
                    return session
            def integration_binding(self, session_id, *, work_dir, resources_released):
                session = self.sessions[int(session_id)]
                def release():
                    session.schema.device = None
                def restore():
                    session.schema.device = object()
                    session.handles['root'].generation += 1
                return Binding(gate, str(args.device_id), 'usrp', session_id,
                    session.handles['root'].generation, work_dir,
                    lambda: not session.closed and local['enabled'] and authority.active(), release,
                    lambda: session.schema.device is None, restore, resources_released)
            def close_gid(self, gid, **kwargs):
                self.close_all()
            def close_all(self, **kwargs):
                for session in self.sessions:
                    session.closed = True
                    session.schema.device = None
            def cleanup_pending(self, gid):
                return False
        return Manager(), local, gate
    monkeypatch.setattr(harness, 'configure_local', configure)
    def rx(manager, session, settings, serial):
        with gates[-1].native():
            assert session.schema.device is not None
        return {'received': 4096, 'fake_hardware': True}
    monkeypatch.setattr(harness, 'receive_native', rx)
    args = SimpleNamespace(serial='FAKE123', device_id=40, state_dir=tmp_path, cgroup_root=tmp_path,
                           profile='usrp2901', rate=1e6, frequency=100e6, gain=0)
    authority = SimpleNamespace(active=lambda: True)
    evidence = {'checks': {}, 'passed': False}
    harness.run_lifecycle(args, authority, evidence)
    assert evidence['passed'] is False  # only the real full harness may publish a success report
    assert evidence['checks']['native_rx_after']['fake_hardware']
    assert evidence['checks']['stop_timeout_kept_ownership']['error'] == 'timeout'
    assert evidence['checks']['expired_start']['ownership_retained_until_hook_return']
    assert evidence['checks']['expired_start']['cleanup_serialized']
    assert evidence['checks']['expired_start']['outcome']['error'] == 'cancelled'
    assert evidence['checks']['rx_after_expired_start']['fake_hardware']
    assert evidence['checks']['next_local_holder_rx']['fake_hardware']
