"""Sample package tests use no radio; the real receive check is opt-in."""
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace

import pytest

SAMPLE = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SAMPLE))
from rrf_example_usrp_probe import Probe, configuration


class Processes:
    def __init__(self):
        self.launches = []
        self.alive = True
        self.stop_ok = True
    def launch(self, argv, **kwargs):
        self.launches.append((argv, kwargs))
        return 'owned-process'
    def snapshot(self):
        return {'processes': [{'id': 'owned-process', 'returncode': None if self.alive else 1}]}
    def log_tail(self, process_id, stream):
        return b'RRF_USRP_PROBE {"event":"ready","serial":"ABC123","received":4096,"rate":1000000,"frequency":100000000,"gain":0,"channel":0,"uhd_version":"fake"}\n'
    def stop(self, **kwargs):
        self.alive = False
        return self.stop_ok


def context(tmp_path):
    return SimpleNamespace(selector={'serial': 'ABC123'}, processes=Processes(),
                           work_dir=tmp_path, cancelled=Event())


def test_probe_launch_is_explicit_receive_only_and_cleanup_is_repeatable(tmp_path):
    ctx = context(tmp_path)
    adapter = Probe(ctx)
    assert not ctx.processes.launches
    adapter.start('receive', {})
    argv, kwargs = ctx.processes.launches[0]
    assert Path(argv[0]).is_absolute()
    assert argv[1:5] == ['-I', '-u', '-m', 'rrf_example_usrp_probe.worker']
    assert '--serial' in argv and 'ABC123' in argv
    assert kwargs['env'] == {'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'OPENBLAS_NUM_THREADS': '1',
                             'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1'}
    assert kwargs['required_while_running'] is True
    assert adapter.call('health', {})['ready']
    adapter.stop()
    adapter.stop()
    assert adapter.status() == {'ready': False, 'stopped': True}


@pytest.mark.parametrize('value', [{'executable': '/bin/true'}, {'rate': True}, {'gain': float('nan')},
                                 {'samples': 0}, {'channel': 2}, {'frequency': 7e9}])
def test_invalid_configuration_cannot_launch(value, tmp_path):
    ctx = context(tmp_path)
    with pytest.raises(ValueError):
        Probe(ctx).start('receive', value)
    assert not ctx.processes.launches


@pytest.mark.parametrize('selector', [{'addr': '1.2.3.4'}, {'serial': 'a,addr=b'}, {'serial': 'x', 'addr': 'y'}])
def test_ambiguous_or_injectable_selector_cannot_launch(selector, tmp_path):
    ctx = context(tmp_path)
    ctx.selector = selector
    with pytest.raises(ValueError):
        Probe(ctx).start('receive', {})
    assert not ctx.processes.launches


def test_cancelled_start_never_launches(tmp_path):
    ctx = context(tmp_path)
    ctx.cancelled.set()
    with pytest.raises(RuntimeError):
        Probe(ctx).start('receive', {})
    assert not ctx.processes.launches


def test_crash_and_cleanup_failure_are_not_reported_as_success(tmp_path):
    ctx = context(tmp_path)
    adapter = Probe(ctx)
    adapter.start('receive', {})
    ctx.processes.alive = False
    with pytest.raises(RuntimeError, match='no longer running'):
        adapter.call('health', {})
    ctx.processes.stop_ok = False
    with pytest.raises(RuntimeError, match='could not be verified'):
        adapter.stop()
