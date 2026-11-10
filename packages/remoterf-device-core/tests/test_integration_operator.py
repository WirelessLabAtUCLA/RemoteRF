"""Local operator recovery tests: fake hardware/cgroups, real writer locks."""
import json
from unittest.mock import Mock

import pytest

from remoterf_device_core.integrations import IntegrationError
from remoterf_device_core.integrations import operator as admin
from remoterf_device_core.integrations import supervision as backend
from remoterf_device_core.integrations.ownership import Gate, Journal
from test_integration_supervision import fake_cgroups


def prepared(fake_cgroups, tmp_path, *, resources=True):
    create, root, _ = fake_cgroups
    state = tmp_path / 'operator-state'
    supervisor = create(registry_state=state / 'processes')
    scope = supervisor.create('owner')
    journal = Journal(state / 'ownership')
    gate = Gate('serial:radio', journal)
    lease = gate.reserve(session='bearer-session-never-print', generation=1, integration='org.example.test',
                         resources='owner' if resources else None)
    gate.persist(lease)
    journal.close()
    supervisor.disconnect()
    return state, root, lease


def records(state):
    return json.loads((state / 'ownership' / 'ownership.json').read_text())


def test_inspection_is_read_only_while_controller_writer_is_live(fake_cgroups, tmp_path, capsys):
    state, root, lease = prepared(fake_cgroups, tmp_path)
    journal = Journal(state / 'ownership')
    supervisor = backend.CgroupSupervisor(root, state / 'processes')
    before = (state / 'ownership' / 'ownership.json').read_bytes()
    try:
        result = admin.inspect(state, root)
        assert result['inspection_only'] and result['hardware'][0]['held']
        assert result['processes'][0]['identity_verified']
        assert 'bearer-session-never-print' not in json.dumps(result)
        assert admin.main(['inspect', '--state-dir', str(state), '--cgroup-root', str(root)]) == 0
        assert 'bearer-session-never-print' not in capsys.readouterr().out
        assert (state / 'ownership' / 'ownership.json').read_bytes() == before
    finally:
        supervisor.disconnect()
        journal.close()


def test_recovery_refuses_live_controller_and_stale_lease(fake_cgroups, tmp_path):
    state, root, lease = prepared(fake_cgroups, tmp_path)
    journal = Journal(state / 'ownership')
    try:
        with pytest.raises(IntegrationError) as caught:
            admin.recover(state, root, physical_id='serial:radio', lease=lease)
        assert caught.value.code == 'journal_busy'
    finally:
        journal.close()
    with pytest.raises(IntegrationError) as caught:
        admin.recover(state, root, physical_id='serial:radio', lease='wrong')
    assert caught.value.code == 'stale_owner'
    assert records(state)['serial:radio']['lease'] == lease


def test_recovery_releases_only_verified_matching_managed_owner(fake_cgroups, tmp_path):
    state, root, lease = prepared(fake_cgroups, tmp_path)
    result = admin.recover(state, root, physical_id='serial:radio', lease=lease)
    assert result['recovered'] and result['managed_resources_only']
    assert records(state) == {}
    assert json.loads((state / 'processes' / 'scopes.json').read_text())['scopes'] == {}
    assert not admin.inspect(state, root)['recovery_required']


def test_old_hardware_record_without_resource_identity_is_not_waived(fake_cgroups, tmp_path):
    state, root, lease = prepared(fake_cgroups, tmp_path, resources=False)
    with pytest.raises(IntegrationError) as caught:
        admin.recover(state, root, physical_id='serial:radio', lease=lease)
    assert caught.value.code == 'process_recovery_required'
    assert records(state)['serial:radio']['lease'] == lease


def test_wrong_boot_or_unknown_scope_keeps_hardware_fenced(fake_cgroups, tmp_path):
    state, root, lease = prepared(fake_cgroups, tmp_path)
    path = state / 'processes' / 'scopes.json'
    payload = json.loads(path.read_text())
    payload['boot'] = '22222222-2222-2222-2222-222222222222'
    path.write_text(json.dumps(payload))
    with pytest.raises(IntegrationError) as caught:
        admin.recover(state, root, physical_id='serial:radio', lease=lease)
    assert caught.value.code == 'process_recovery_required'
    assert records(state)['serial:radio']['lease'] == lease


def test_cleanup_timeout_keeps_lease_and_releases_operator_writer_locks(fake_cgroups, tmp_path):
    state, root, lease = prepared(fake_cgroups, tmp_path)
    name = json.loads((state / 'processes' / 'scopes.json').read_text())['scopes']['owner']['name']
    (root / name / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
    with pytest.raises(IntegrationError) as caught:
        admin.recover(state, root, physical_id='serial:radio', lease=lease, timeout=0)
    assert caught.value.code == 'process_recovery_required'
    assert records(state)['serial:radio']['lease'] == lease
    (root / name / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
    assert admin.recover(state, root, physical_id='serial:radio', lease=lease)['recovered']


def test_partial_metadata_cleanup_is_reported_and_orphan_cleanup_finishes(fake_cgroups, tmp_path, monkeypatch):
    state, root, lease = prepared(fake_cgroups, tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(backend.Scope, 'close', Mock(side_effect=OSError('injected metadata failure')))
        with pytest.raises(admin.RecoveryError) as caught:
            admin.recover(state, root, physical_id='serial:radio', lease=lease)
    assert caught.value.physical_released
    assert records(state) == {}
    assert admin.cleanup_orphan(state, root, owner_id='owner')['cleaned']
    assert records(state) == {}


def test_orphan_command_refuses_hardware_referenced_scope(fake_cgroups, tmp_path):
    state, root, lease = prepared(fake_cgroups, tmp_path)
    with pytest.raises(IntegrationError):
        admin.cleanup_orphan(state, root, owner_id='owner')
    assert records(state)['serial:radio']['lease'] == lease


def test_corrupt_or_oversized_inspection_never_dumps_contents(tmp_path, capsys):
    (tmp_path / 'ownership').mkdir()
    (tmp_path / 'ownership' / 'ownership.json').write_text('sensitive' * 10000)
    assert admin.main(['inspect', '--state-dir', str(tmp_path), '--cgroup-root', str(tmp_path / 'missing')]) == 2
    output = capsys.readouterr().out
    assert 'sensitive' not in output
    assert json.loads(output)['error'] == 'process_recovery_required'


def test_missing_hardware_journal_is_not_assumed_empty_for_recovery(tmp_path):
    with pytest.raises(IntegrationError):
        admin.cleanup_orphan(tmp_path, tmp_path / 'cgroups', owner_id='owner')
    assert not (tmp_path / 'ownership').exists()


def test_orphan_cleanup_preserves_an_unrelated_hardware_owner(fake_cgroups, tmp_path):
    create, root, _ = fake_cgroups
    state = tmp_path / 'operator-state'
    supervisor = create(registry_state=state / 'processes')
    supervisor.create('orphan')
    supervisor.create('held')
    held_name = supervisor.describe()['owners']['held']['name']
    (root / held_name / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
    journal = Journal(state / 'ownership')
    gate = Gate('serial:other', journal)
    lease = gate.reserve(session='other-session', generation=1, integration='org.example.other', resources='held')
    gate.persist(lease)
    journal.close()
    supervisor.disconnect()
    before = (state / 'ownership' / 'ownership.json').read_bytes()
    assert admin.cleanup_orphan(state, root, owner_id='orphan')['cleaned']
    assert (state / 'ownership' / 'ownership.json').read_bytes() == before
    assert (root / held_name / 'cgroup.kill').read_text() == ''


def test_orphan_cleanup_repairs_completed_release_metadata_window(fake_cgroups, tmp_path, monkeypatch):
    state, root, lease = prepared(fake_cgroups, tmp_path)
    write = backend.CgroupSupervisor._write
    calls = []
    def fail_final_removal(supervisor):
        calls.append(1)
        if len(calls) == 2:
            supervisor._broken = True
            raise OSError('final scope metadata write failed')
        return write(supervisor)
    with monkeypatch.context() as patch:
        patch.setattr(backend.CgroupSupervisor, '_write', fail_final_removal)
        with pytest.raises(admin.RecoveryError) as caught:
            admin.recover(state, root, physical_id='serial:radio', lease=lease)
    assert caught.value.physical_released
    assert records(state) == {}
    assert json.loads((state / 'processes' / 'scopes.json').read_text())['scopes']['owner']['state'] == 'released'
    assert admin.cleanup_orphan(state, root, owner_id='owner')['cleaned']
    assert json.loads((state / 'processes' / 'scopes.json').read_text())['scopes'] == {}
