"""Offline checks of the operator-only dev client qualification boundaries."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

HARNESS = Path(__file__).resolve().parents[1] / 'usrp_dev_e2e.py'
spec = importlib.util.spec_from_file_location('usrp_dev_e2e_test', HARNESS)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def args(tmp_path, mode='enabled'):
    home = tmp_path / 'remoterf-phase1c-dev-client-offline'
    home.mkdir(mode=0o700)
    return SimpleNamespace(mode=mode, deployment_id=h.DEPLOYMENT, deployment_name=h.DEPLOYMENT_NAME,
        client_home=home, evidence=tmp_path/'evidence.json', observation_request=tmp_path/'request.json',
        observation_confirmed=tmp_path/'confirmed.json')


def catalog():
    return {'devices': [{'device_id': f'{h.DEPLOYMENT}:40', 'deployment_id': h.DEPLOYMENT,
        'deployment_name': h.DEPLOYMENT_NAME, 'local_device_id': '40', 'device_type': 'usrp', 'online': True}]}


@pytest.mark.parametrize('change', [
    {'deployment_id': '00000000-0000-0000-0000-000000000000'},
    {'deployment_name': 'ucla'},
])
def test_rejects_other_destination_before_loading_account(tmp_path, change):
    value = args(tmp_path)
    for key, item in change.items():
        setattr(value, key, item)
    with pytest.raises(RuntimeError, match='unexpected dev deployment'):
        h.validate_args(value)


def test_home_must_be_private_and_observation_paths_fresh(tmp_path):
    value = args(tmp_path, 'expiry')
    value.client_home.chmod(0o755)
    with pytest.raises(RuntimeError, match='private'):
        h.validate_args(value)
    value.client_home.chmod(0o700)
    assert h.validate_args(value) == value.client_home
    value.observation_confirmed.write_text('{}')
    with pytest.raises(RuntimeError, match='fresh'):
        h.validate_args(value)


@pytest.mark.parametrize('field,value', [('deployment_name', 'Another lab'), ('device_type', 'pluto'),
                                        ('local_device_id', '41'), ('online', False)])
def test_catalog_must_match_exact_private_radio(field, value):
    payload = catalog()
    payload['devices'][0][field] = value
    with pytest.raises(RuntimeError, match='provenance or readiness'):
        h.verify_catalog(payload, h.DEPLOYMENT_NAME)


def test_calendar_uses_normal_minimum_duration_and_held_booking_cleanup(monkeypatch):
    calls = []
    monkeypatch.setattr(h.time, 'time', lambda: 1000.0)
    class Backend:
        def reserve(self, ref, start, end):
            calls.append((ref, start, end))
            return {'reservation_id': h.DEPLOYMENT+':123', 'device_id': ref, 'token': 'not-recorded'}
        def cancel(self, ref):
            calls.append(('cancel', ref))
    bookings = h.Bookings(Backend())
    value = bookings.reserve(expiring=True)
    assert calls == [(f'{h.DEPLOYMENT}:40', 540, 1140)]
    assert value['_calendar_end'] == 1140
    assert bookings.cleanup() == []
    assert bookings.held == set()
    assert calls[-1] == ('cancel', h.DEPLOYMENT+':123')


def observation():
    request = {'run_id': 'one', 'mode': 'expiry', 'deployment_id': h.DEPLOYMENT,
        'device_id': 40, 'serial': h.SERIAL, 'operation_id': 'operation', 'reservation_id': 'booking',
        'event_at': 100, 'calendar_end': 100, 'quiet_started_at': 100}
    proof = {**request, 'observed_at': 111, 'root_identity_verified': True,
        'hardware_owners': 0, 'populated_owned_scopes': 0,
        'unknown_owned_scopes': 0, 'observable_usb_owners': 0,
        'usb_observation_scope': 'current uid only'}
    return request, proof


@pytest.mark.parametrize('field,value', [('run_id', 'old'), ('observed_at', 109),
    ('observed_at', float('nan')), ('observed_at', float('inf')), ('root_identity_verified', False),
    ('observable_usb_owners', 1), ('hardware_owners', False), ('unknown_owned_scopes', 1),
    ('usb_observation_scope', 'all users')])
def test_old_early_untrusted_or_unreleased_observation_refused(monkeypatch, field, value):
    monkeypatch.setattr(h.time, 'time', lambda: 120)
    request, proof = observation()
    proof[field] = value
    with pytest.raises(RuntimeError):
        h.validate_observation(request, proof)


def test_filesystem_only_observation_wait_requires_exact_fresh_proof(tmp_path, monkeypatch):
    value = args(tmp_path, 'expiry')
    request, proof = observation()
    monkeypatch.setattr(h.time, 'time', lambda: 120)
    def independent_observer(predicate, seconds):
        assert seconds == 180
        assert json.loads(value.observation_request.read_text()) == request
        assert predicate() is False
        h.write_json_new(value.observation_confirmed, proof)
        assert predicate() is True
    monkeypatch.setattr(h, 'until', independent_observer)
    assert h.observe_without_rpc(value, request) == proof


def test_missing_receipt_never_creates_owner_and_native_still_receives(tmp_path, monkeypatch):
    calls = []
    class Rejected(Exception):
        details = {'integration_code': 'qualification_required'}
    class Radio:
        def integration_list(self, request):
            calls.append('list')
            return {'enabled': False, 'generation': 7, 'integrations': [{'id': h.INTEGRATION, 'enabled': False}]}
        def integration_describe(self, request):
            calls.append('describe')
            return {'integration': {'id': h.INTEGRATION}}
        def integration_start(self, request):
            calls.append('start-denied')
            assert request['generation'] == 7 and request['profile'] == 'receive'
            raise Rejected()
    @contextmanager
    def connection(token, checks):
        assert token == 'private'
        try:
            yield Radio(), None, [False]
        finally:
            calls.append('close')
    monkeypatch.setattr(h, 'connection', connection)
    monkeypatch.setattr(h, 'native_rx', lambda *a: calls.append('rx') or {'received': 4096})
    bookings = SimpleNamespace(reserve=lambda: {'token': 'private', 'reservation_id': 'one'},
                               cancel=lambda rid: calls.append('cancel'))
    checks = {}
    h.lifecycle('receipt-missing', bookings, checks, 'retry-key')
    assert calls == ['rx', 'list', 'describe', 'start-denied', 'rx', 'close', 'cancel']
    assert checks['unqualified_start_refused']['integration_code'] == 'qualification_required'


def test_calendar_no_device_calls_or_new_booking_until_independent_release(tmp_path, monkeypatch):
    from remoterf.core.v2_errors import RemoteRFReservationError
    value = args(tmp_path, 'cancel')
    calls = []
    class Radio:
        def integration_status(self, request):
            calls.append('first-stale-rpc')
            raise RemoteRFReservationError('revoked')
    @contextmanager
    def connection(token, checks):
        calls.append('open')
        try:
            yield Radio(), None, [False]
        finally:
            calls.append('close')
    monkeypatch.setattr(h, 'connection', connection)
    monkeypatch.setattr(h, 'native_rx', lambda *a: calls.append('rx') or {'received': 4096})
    monkeypatch.setattr(h, 'discovery', lambda *a, **kw: {})
    monkeypatch.setattr(h, 'start_running', lambda *a: calls.append('running') or {'operation_id': 'op'})
    def reject_unproven(args, request):
        assert calls == ['reserve', 'open', 'rx', 'running', 'cancel']
        calls.append('independent-proof-failed')
        raise RuntimeError('no independent proof')
    monkeypatch.setattr(h, 'observe_without_rpc', reject_unproven)
    def reserve(**kw):
        calls.append('reserve')
        return {'token': 'private', 'reservation_id': 'one', '_calendar_end': 99999}
    bookings = SimpleNamespace(reserve=reserve, cancel=lambda rid: calls.append('cancel'))
    with pytest.raises(RuntimeError, match='no independent proof'):
        h.calendar(value, bookings, {}, 'this-run')
    # Failure may safely close the old client; it may never count that close as
    # an autonomous release proof or proceed to a next holder.
    assert calls == ['reserve', 'open', 'rx', 'running', 'cancel', 'independent-proof-failed', 'close']


def test_new_evidence_never_overwrites_existing_file(tmp_path):
    path = tmp_path/'proof.json'
    h.write_json_new(path, {'passed': False})
    with pytest.raises(FileExistsError):
        h.write_json_new(path, {'passed': True})
    assert json.loads(path.read_text()) == {'passed': False}
