"""Fresh processes, real local calendar/auth; only native hardware is fake."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

HARNESS = Path(__file__).resolve().parents[1] / 'usrp_cold_start.py'
spec = importlib.util.spec_from_file_location('normal_auth_cold_test', HARNESS)
h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)


@pytest.mark.parametrize('case,delay', [('valid', 5), ('expire_during_open', 2), ('cancel_during_open', .2)])
def test_fresh_process_normal_calendar_auth_and_cleanup(tmp_path, case, delay):
    evidence = tmp_path / 'evidence.json'
    result = subprocess.run([sys.executable, str(HARNESS), '--backend', 'fake', '--case', case,
        '--serial', 'FAKE123', '--device-id', '40', '--state-dir', str(tmp_path / 'state'),
        '--evidence', str(evidence),
        '--fake-open-delay', str(delay)], capture_output=True, text=True, timeout=25)
    data = json.loads(evidence.read_text())
    assert result.returncode == 0, (data.get('failure'), result.stdout[-2000:], result.stderr[-2000:])
    assert data['passed'] and data['invalid_token_refused']
    assert data['normal_auth_final']['resolver_unmodified']
    assert data['normal_auth_final']['four_second_authority_cache_used'] is False
    assert data['events'][0]['initial_native_modules'] == {'numpy': False, 'uhd': False}
    assert data['native_startup']['test_only_prewarm'] is False
    assert data['native_startup']['ordinary_preload_enabled'] is True
    assert data['private_calendar_empty'] and not data['calendar_errors']
    assert data['cleanup_final']['sessions'] == data['cleanup_final']['closing_sessions'] == 0
    assert data['cleanup_final']['opening_devices'] == data['cleanup_final']['inventory_native_owners'] == 0
    assert data['cleanup_final']['hardware_owner'] is None
    assert data['cleanup_final']['active_native'] == 0
    if case == 'valid':
        assert data['first_open_seconds'] > 4
        assert data['first_receive']['received'] == 4096
        assert 'first_attempt_refusal' not in data
    else:
        assert data['first_attempt_refusal']['type'] in {'RemoteRFReservationError', 'RemoteRFSessionExpiredError'}
        assert data['next_holder_receive']['received'] == 4096
        assert data['cleanup_final']['fake_native_instances_closed'] == 2
    # No raw token/password enters the retained JSON.
    assert not any('token' in key.lower() and isinstance(value, str)
                   for row in data['events'] for key, value in row.items())


@pytest.mark.parametrize('expires', [float('nan'), float('inf'), 0])
def test_external_proof_refuses_nonfinite_or_expired_booking(tmp_path, monkeypatch, expires):
    from types import SimpleNamespace
    args = SimpleNamespace(backend='usrp', authority_command=tmp_path/'provider', reservation_id='booking',
                           device_id=40, serial='RADIO')
    monkeypatch.setattr(h.helpers.handoff.Authority, '_query', lambda self: {
        'active': True, 'reservation_id': 'booking', 'device_id': 40, 'serial': 'RADIO', 'expires_at': expires})
    with pytest.raises(RuntimeError, match='external booking'):
        h.external_proof(args)


def test_unguarded_hardware_is_refused_before_provider_or_server_import(tmp_path):
    provider = tmp_path / 'provider'
    marker = tmp_path / 'provider-called'
    provider.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 1\n')
    provider.chmod(0o700)
    scope = tmp_path / 'rrf-not-a-kernel-scope'; scope.mkdir()
    evidence = tmp_path / 'evidence.json'
    result = subprocess.run([sys.executable, str(HARNESS), '--backend', 'usrp', '--serial', 'FAKE123',
        '--device-id', '40', '--state-dir', str(tmp_path / 'state'), '--evidence', str(evidence),
        '--authority-command', str(provider),
        '--reservation-id', 'not-a-real-reservation', '--guarded-scope', str(scope)],
        capture_output=True, timeout=10)
    data = json.loads(evidence.read_text())
    assert result.returncode != 0 and not data['passed']
    assert data['events'] == [] and 'source_identity' not in data
    assert not marker.exists()


def test_cleanup_observation_retains_admitted_native_work():
    from threading import RLock
    from types import SimpleNamespace as NS
    args = NS(device_id=40, backend='fake')
    manager = NS(_lock=RLock(), _sessions={}, _closing_sessions={}, _opening_gids=set(), cleanup_pending=lambda gid: False)
    gate = NS(snapshot=lambda: {'owner': None, 'active_native': 1})
    dm = NS(_state_lock=RLock(), _devices={}, _deferred_native_hooks={}, hardware_access=NS(gate=lambda gid: gate))
    with pytest.raises(RuntimeError, match='cleanup is still unresolved'):
        h.cleanup_observation(args, dm, manager, [])
