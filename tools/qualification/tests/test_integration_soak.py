"""Check evidence accounting and fail-closed fixtures, without a long soak."""
import importlib.util
import json
from pathlib import Path
import sys
from threading import Event, Lock
from types import SimpleNamespace

import pytest


PATH = Path(__file__).resolve().parents[1] / 'integration_soak.py'
SPEC = importlib.util.spec_from_file_location('integration_soak_test', PATH)
soak = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(soak)


def test_latency_measurements_are_bounded_and_labeled_estimates():
    samples = soak.Samples(capacity=32)
    for index in range(10000):
        samples.add(index / 1000000)
    report = samples.report()
    assert len(samples.values) == report['reservoir_count'] == 32
    assert report['count'] == 10000 and report['max_ms'] == pytest.approx(9.999)
    assert report['p50_ms'] <= report['p95_ms'] <= report['p99_ms'] <= report['max_ms']
    assert report['percentile_estimator'] == 'uniform_bounded_reservoir'


def test_injected_disk_exhaustion_preserves_real_admission_fence(tmp_path):
    assert soak.preflight(tmp_path) == {'injected_enospc_retains_fence': True,
                                        'release_after_verified_cleanup': True}
    from remoterf_device_core.integrations.ownership import Journal
    recovered = Journal(tmp_path / 'enospc')
    try:
        assert recovered.records() == {}
    finally:
        recovered.close()


def test_owner_and_action_capacity_reject_before_side_effects_and_allow_retry(tmp_path):
    report = soak.backpressure(tmp_path)
    assert report['owner_capacity'] == report['action_capacity'] == 2
    assert report['overload_before_side_effects'] and report['retry_at_capacity']
    assert report['admission_after_retirement']


def test_closed_session_does_not_hide_unexpected_poll_errors():
    rig = soak.Rig.__new__(soak.Rig)
    rig.stop, rig.lock, rig.metrics = Event(), Lock(), soak.Metrics()
    rig.current = {2: (SimpleNamespace(closed=True), 'operation')}
    def unexpected(*args):
        raise RuntimeError('unrelated status bug')
    rig.generic = unexpected
    with pytest.raises(RuntimeError, match='unrelated status bug'):
        rig.status_worker(2)
    assert not rig.metrics.expected


@pytest.mark.parametrize('seconds', [0, -1, 3601])
def test_invalid_duration_creates_no_state(tmp_path, seconds):
    directory = tmp_path / 'untouched'
    with pytest.raises(ValueError, match='bounds'):
        soak.run(directory, seconds=seconds)
    assert not directory.exists()


@pytest.mark.skipif(sys.platform != 'linux', reason='soak resource accounting uses Linux /proc')
def test_native_failure_cannot_publish_success_evidence(tmp_path, monkeypatch):
    def failed_native(self, session, **kwargs):
        raise RuntimeError('injected raw native failure')
    monkeypatch.setattr(soak.Rig, 'raw', failed_native)
    directory = tmp_path / 'failure'
    with pytest.raises(RuntimeError, match='injected raw native failure'):
        soak.run(directory, seconds=.1, baseline_seconds=.1)
    report = json.loads((directory / 'evidence.json').read_text())
    assert report['passed'] is report['hardware_qualification'] is report['activation_changed'] is False
    assert report['unexpected_errors'] and not report.get('cleanup_failed', False)
