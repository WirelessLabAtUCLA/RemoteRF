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
        def launch(self, *args, **kwargs):
            self.alive = True
            return 'fake-child'
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
