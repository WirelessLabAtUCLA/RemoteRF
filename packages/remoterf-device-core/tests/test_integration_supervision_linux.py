"""Explicit delegated-cgroup qualification; no radios or deployment services.

Set REMOTERF_CGROUP_TEST_ROOT to an isolated, writable cgroup v2 domain with
memory and pids subtree controllers enabled. The test process belongs in a
separate manager subgroup. A transient systemd --user service with Delegate=yes
and DelegateSubgroup=manager supplies an isolated owner for the entire check.
Never point this test at a live application's cgroup.
"""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from remoterf_device_core.integrations import supervision as mod


ROOT = os.environ.get('REMOTERF_CGROUP_TEST_ROOT')
pytestmark = pytest.mark.skipif(sys.platform != 'linux' or not ROOT,
                                reason='requires an explicitly delegated isolated cgroup v2 root')


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('live cgroup qualification did not reach expected state')


def test_real_descendants_logs_independent_owner_and_crash_recovery(tmp_path, monkeypatch):
    root = Path(ROOT)
    state = tmp_path / 'supervision-state'
    supervisor = mod.CgroupSupervisor(root, state, max_log_bytes=1024)
    first = supervisor.create('first')
    second = supervisor.create('second')
    monkeypatch.setenv('REMOTERF_PARENT_SECRET', 'must-not-be-inherited')
    program = '''
import os, subprocess, sys, time
assert 'REMOTERF_PARENT_SECRET' not in os.environ
assert os.environ['EXPLICIT'] == 'test'
subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(60)'], start_new_session=True)
os.write(1, b'a' * 100000)
os.write(2, b'b' * 100000)
print('READY', flush=True)
time.sleep(60)
'''
    try:
        first_id = first.launch([sys.executable, '-I', '-S', '-c', program],
                                env={'EXPLICIT': 'test'}, cwd=tmp_path)
        second_id = second.launch([sys.executable, '-I', '-S', '-c', 'print("SECOND", flush=True)'],
                                  env={}, cwd=tmp_path)
        wait_for(lambda: b'READY' in first.log_tail(first_id, 'stdout')
                 and first.snapshot()['processes'][0]['stderr_bytes'] == 100000)
        wait_for(lambda: second.snapshot()['processes'][0]['logs_drained'])
        first_row = first.snapshot()['processes'][0]
        assert first_row['stdout_bytes'] >= 100000 and first_row['stderr_bytes'] == 100000
        assert len(first.log_tail(first_id, 'stdout')) <= 1024
        assert len(first.log_tail(first_id, 'stderr')) == 1024
        assert not first.resources_released()
        assert first.stop(timeout=5)
        assert first.snapshot()['processes'][0]['returncode'] == -9
        # Kernel populated covers descendants, including the grandchild that
        # created its own session/process group and outlived its direct parent.
        assert first.resources_released()
        assert second.snapshot()['processes'][0]['returncode'] == 0
        assert b'SECOND' in second.log_tail(second_id, 'stdout')
        assert second.resources_released()
        first.close()
        second.close()
        supervisor.close()
    finally:
        for scope in (first, second):
            if not scope.snapshot()['closed']:
                assert scope.stop(timeout=5), 'isolated test resources did not drain'
                scope.close()
        supervisor.close()

    # An independently running supervisor exits abruptly without running stop,
    # leaving a real descendant in the persisted cgroup. Recovery uses cgroup
    # identity and kernel populated/kill, never a PID from the journal.
    creator = '''
import os, sys, time
sys.path.insert(0, sys.argv[1])
from remoterf_device_core.integrations.supervision import CgroupSupervisor
supervisor = CgroupSupervisor(sys.argv[2], sys.argv[3])
scope = supervisor.create('orphan')
process = scope.launch([sys.executable, '-I', '-S', '-c', 'import time; print("READY", flush=True); time.sleep(60)'],
                      env={}, cwd=sys.argv[4])
limit = time.monotonic() + 5
while b'READY' not in scope.log_tail(process, 'stdout'):
    assert time.monotonic() < limit
    time.sleep(0.01)
os._exit(23)
'''
    child = subprocess.run([sys.executable, '-I', '-S', '-c', creator,
                            str(Path(mod.__file__).resolve().parents[2]), str(root), str(state), str(tmp_path)],
                           stdin=subprocess.DEVNULL, capture_output=True, env={}, timeout=10)
    assert child.returncode == 23, child.stderr.decode()
    recovered = mod.CgroupSupervisor(root, state)
    scope = recovered.reopen('orphan')
    try:
        assert not scope.snapshot()['accepting']
        assert not scope.resources_released()
        assert scope.stop(timeout=5)
        assert scope.resources_released()
        scope.close()
        recovered.close()
    finally:
        if not scope.snapshot()['closed']:
            assert scope.stop(timeout=5), 'isolated orphan did not drain'
            scope.close()
        recovered.close()
    assert not any(path.name.startswith('rrf-') for path in root.iterdir())


def test_real_offline_operator_recovery_uses_writer_locks_and_owned_cgroup(tmp_path):
    import json
    root = Path(ROOT)
    state = tmp_path / 'operator-state'
    creator = '''
import os, sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from remoterf_device_core.integrations.supervision import CgroupSupervisor
from remoterf_device_core.integrations.ownership import Gate, Journal
state = Path(sys.argv[3])
journal = Journal(state / 'ownership')
gate = Gate('qualification:non-radio', journal)
lease = gate.reserve(session='qualification-not-a-bearer', generation=1,
                     integration='org.example.process_check', resources='operator-test')
gate.persist(lease)
supervisor = CgroupSupervisor(sys.argv[2], state / 'processes')
scope = supervisor.create('operator-test')
process = scope.launch([sys.executable, '-I', '-S', '-c', 'import time; print("READY", flush=True); time.sleep(60)'],
                      env={}, cwd=sys.argv[4])
limit = time.monotonic() + 5
while b'READY' not in scope.log_tail(process, 'stdout'):
    assert time.monotonic() < limit
    time.sleep(0.01)
os._exit(23)
'''
    source = str(Path(mod.__file__).resolve().parents[2])
    child = subprocess.run([sys.executable, '-I', '-S', '-c', creator, source, str(root), str(state), str(tmp_path)],
                           stdin=subprocess.DEVNULL, capture_output=True, env={}, timeout=10)
    assert child.returncode == 23, child.stderr.decode()
    lease = json.loads((state / 'ownership' / 'ownership.json').read_text())['qualification:non-radio']['lease']
    invoke = '''
import sys
sys.path.insert(0, sys.argv.pop(1))
from remoterf_device_core.integrations.operator import main
raise SystemExit(main(sys.argv[1:]))
'''
    inspect = subprocess.run([sys.executable, '-I', '-S', '-c', invoke, source, 'inspect',
                              '--state-dir', str(state), '--cgroup-root', str(root)],
                             capture_output=True, env={}, timeout=10)
    assert inspect.returncode == 0, inspect.stderr.decode()
    snapshot = json.loads(inspect.stdout)
    assert snapshot['hardware'][0]['held'] and snapshot['processes'][0]['populated']
    assert 'qualification-not-a-bearer' not in inspect.stdout.decode()
    recovered = subprocess.run([sys.executable, '-I', '-S', '-c', invoke, source, 'recover',
                                '--state-dir', str(state), '--cgroup-root', str(root),
                                '--physical-id', 'qualification:non-radio', '--lease', lease],
                               capture_output=True, env={}, timeout=10)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert json.loads(recovered.stdout)['recovered']
    assert json.loads((state / 'ownership' / 'ownership.json').read_text()) == {}
    assert json.loads((state / 'processes' / 'scopes.json').read_text())['scopes'] == {}
    assert not any(path.name.startswith('rrf-') for path in root.iterdir())
