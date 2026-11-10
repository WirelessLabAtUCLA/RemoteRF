"""Supervision contracts with fake cgroup files; not Linux containment proof.

Bootstrap/pipe/exec tests run real benign children, but this fixture cannot
establish actual descendant containment. A delegated-cgroup/radio qualification
must separately exercise the kernel-backed path before integration activation.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from remoterf_device_core.integrations import IntegrationError
from remoterf_device_core.integrations import supervision as mod


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('supervised child did not finish')


def controls(path):
    path.mkdir(exist_ok=True)
    for name, value in {'cgroup.type': 'domain', 'cgroup.events': 'populated 0\nfrozen 0\n',
                        'cgroup.procs': '', 'cgroup.kill': '', 'pids.max': 'max', 'memory.max': 'max',
                        'cgroup.max.descendants': 'max'}.items():
        (path / name).write_text(value)


@pytest.fixture
def fake_cgroups(tmp_path, monkeypatch):
    root, state = tmp_path / 'cgroups', tmp_path / 'state'
    controls(root)
    monkeypatch.setattr(mod, '_require_linux', lambda: None)
    monkeypatch.setattr(mod, '_assert_cgroup2', lambda fd: None)
    monkeypatch.setattr(mod, '_boot_id', lambda: '11111111-1111-1111-1111-111111111111')
    monkeypatch.setattr(mod.CgroupSupervisor, '_create_cgroup', lambda self, name: controls(self.root / name))
    real_rmdir = os.rmdir
    def remove(path, *, dir_fd=None):
        resolved = root / path if dir_fd is not None else Path(path)
        # Real cgroup control files are virtual and disappear with rmdir.
        if resolved.parent == root and resolved.name.startswith('rrf-'):
            for file in resolved.iterdir():
                if file.is_file():
                    file.unlink()
        real_rmdir(path, dir_fd=dir_fd)
    monkeypatch.setattr(mod.os, 'rmdir', remove)
    supervisors = []
    def create(registry_state=None, **kwargs):
        instance = mod.CgroupSupervisor(root, registry_state or state, **kwargs)
        supervisors.append(instance)
        return instance
    yield create, root, state
    # Test-owned children are short-lived; if an assertion fails, do not leave
    # them or our descriptors behind. This is not the production release proof.
    for supervisor in supervisors:
        for scope in supervisor._scopes.values():
            for row in scope._processes.values():
                child = row['popen']
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=2)
            if not scope._closed and not scope._detached:
                os.close(scope._fd)
        if supervisor._logs:
            supervisor._logs.close()
        if not supervisor._closed:
            supervisor._file.close()
            os.close(supervisor._root_fd)
            supervisor._closed = True


def test_no_process_group_fallback_on_unsupported_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.sys, 'platform', 'not-linux')
    popen = Mock(side_effect=AssertionError('must not launch'))
    monkeypatch.setattr(mod.subprocess, 'Popen', popen)
    with pytest.raises(IntegrationError) as caught:
        mod.CgroupSupervisor(tmp_path / 'root', tmp_path / 'state')
    assert caught.value.code == 'supervision_unavailable'
    popen.assert_not_called()


def test_ordinary_files_are_not_a_delegated_cgroup(tmp_path):
    if sys.platform != 'linux':
        pytest.skip('kernel filesystem identity check runs on Linux')
    controls(tmp_path / 'fake')
    with pytest.raises(IntegrationError) as caught:
        mod.CgroupSupervisor(tmp_path / 'fake', tmp_path / 'state')
    assert caught.value.code == 'supervision_unavailable'


def test_scope_is_persisted_and_idempotent_before_any_launch(fake_cgroups):
    create, root, state = fake_cgroups
    supervisor = create()
    scope = supervisor.create('owner')
    assert supervisor.create('owner') is scope
    row = json.loads((state / 'scopes.json').read_text())['scopes']['owner']
    assert row['state'] == 'ready' and row['identity']
    assert (root / row['name'] / 'pids.max').read_text() == '128'
    assert scope.resources_released()
    scope.close()
    assert supervisor.describe()['owners'] == {}
    supervisor.close()
    supervisor.close()


def test_real_child_uses_explicit_environment_and_bounded_independent_logs(fake_cgroups, tmp_path, monkeypatch):
    create, root, state = fake_cgroups
    supervisor = create(max_log_bytes=1024)
    scope = supervisor.create('owner')
    monkeypatch.setenv('REMOTERF_PARENT_SECRET', 'must-not-be-inherited')
    child = scope.launch([sys.executable, '-I', '-S', '-c', '''
import os, sys
assert 'REMOTERF_PARENT_SECRET' not in os.environ
assert os.environ['EXPLICIT'] == 'only-this'
assert sys.stdin.read() == ''
os.write(1, b'a' * 100000)
os.write(2, b'b' * 100000)
'''], env={'EXPLICIT': 'only-this'}, cwd=tmp_path)
    wait_for(lambda: scope.snapshot()['processes'][0]['logs_drained'])
    row = scope.snapshot()['processes'][0]
    assert row['returncode'] == 0
    assert row['stdout_bytes'] == row['stderr_bytes'] == 100000
    assert row['stdout_truncated'] and row['stderr_truncated']
    assert scope.log_tail(child, 'stdout') == b'a' * 1024
    assert scope.log_tail(child, 'stderr') == b'b' * 1024
    assert scope.resources_released()
    scope.close()
    supervisor.close()


def test_bootstrap_cannot_inherit_target_loader_environment(fake_cgroups, tmp_path, monkeypatch):
    create, root, state = fake_cgroups
    supervisor = create()
    scope = supervisor.create('owner')
    popen = mod.subprocess.Popen
    calls = []
    def observe(*args, **kwargs):
        calls.append(kwargs)
        return popen(*args, **kwargs)
    monkeypatch.setattr(mod.subprocess, 'Popen', observe)
    scope.launch([sys.executable, '-I', '-S', '-c', 'pass'],
                 env={'PYTHONPATH': '/never/used/by/bootstrap'}, cwd=tmp_path)
    wait_for(lambda: scope.snapshot()['processes'][0]['logs_drained'])
    assert calls[0]['env'] == {} and calls[0]['close_fds']
    assert calls[0]['stdin'] == subprocess.DEVNULL
    assert scope.resources_released()


def test_descendant_populated_state_blocks_release_and_stop_is_bounded(fake_cgroups):
    create, root, state = fake_cgroups
    supervisor = create()
    scope = supervisor.create('owner')
    name = supervisor.describe()['owners']['owner']['name']
    (root / name / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
    assert not scope.resources_released()
    assert not scope.stop(timeout=0.02)
    assert (root / name / 'cgroup.kill').read_text() == '1'
    assert scope.snapshot()['failure'] == 'cleanup_timeout'
    assert supervisor.describe()['owners']['owner']['state'] == 'ready'
    (root / name / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
    assert scope.stop(timeout=0)
    scope.close()


def test_scope_identity_mismatch_never_signals_replacement(fake_cgroups, tmp_path):
    create, root, state = fake_cgroups
    supervisor = create()
    scope = supervisor.create('owner')
    name = supervisor.describe()['owners']['owner']['name']
    (root / name).rename(root / 'previous')
    controls(root / name)
    assert not scope.stop(timeout=0)
    assert (root / name / 'cgroup.kill').read_text() == ''
    assert not scope.resources_released()
    with pytest.raises(IntegrationError):
        scope.launch([sys.executable, '-c', 'pass'], env={}, cwd=tmp_path)


def test_recovered_scope_cannot_launch_and_missing_identity_stays_fenced(fake_cgroups, tmp_path):
    create, root, state = fake_cgroups
    supervisor = create()
    scope = supervisor.create('owner')
    # Simulate losing the old process's userspace state, preserving its journal.
    supervisor._logs.close()
    supervisor._file.close()
    supervisor._scopes.pop('owner')
    os.close(scope._fd)
    os.close(supervisor._root_fd)
    supervisor._closed = True
    recovered = create()
    new_scope = recovered.reopen('owner')
    assert new_scope.resources_released()
    with pytest.raises(IntegrationError) as caught:
        new_scope.launch([sys.executable, '-c', 'pass'], env={}, cwd=tmp_path)
    assert caught.value.code == 'cancelled'
    new_scope.close()
    recovered.close()


def test_orphan_scope_without_metadata_and_corrupt_metadata_fail_closed(fake_cgroups):
    create, root, state = fake_cgroups
    controls(root / ('rrf-' + 'a' * 32))
    with pytest.raises(IntegrationError) as caught:
        create()
    assert caught.value.code == 'recovery_required'
    for file in (root / ('rrf-' + 'a' * 32)).iterdir():
        file.unlink()
    (root / ('rrf-' + 'a' * 32)).rmdir()
    (state / 'scopes.json').write_text('{broken')
    with pytest.raises(IntegrationError):
        create()


def test_storage_failure_precedes_process_and_fences_further_launch(fake_cgroups, tmp_path, monkeypatch):
    create, root, state = fake_cgroups
    supervisor = create()
    with monkeypatch.context() as patch:
        patch.setattr(mod.os, 'fsync', Mock(side_effect=OSError('disk full')))
        patch.setattr(mod.subprocess, 'Popen', Mock(side_effect=AssertionError('must not launch')))
        with pytest.raises(OSError):
            supervisor.create('owner')
    assert supervisor.describe()['recovery_required']
    assert supervisor.describe()['owners']['owner']['state'] == 'creating'
    with pytest.raises(IntegrationError):
        supervisor.create('owner')


def test_missing_delegated_controls_refuse_supervision(fake_cgroups, monkeypatch):
    create, root, state = fake_cgroups
    supervisor = create()
    def incomplete(name):
        controls(root / name)
        (root / name / 'memory.max').unlink()
    monkeypatch.setattr(supervisor, '_create_cgroup', incomplete)
    with pytest.raises(IntegrationError) as caught:
        supervisor.create('owner')
    assert caught.value.code == 'supervision_unavailable'
    assert supervisor.describe()['owners']['owner']['state'] == 'creating'


def test_launch_limit_and_release_seal_are_enforced(fake_cgroups, tmp_path):
    create, root, state = fake_cgroups
    supervisor = create(max_processes=1)
    scope = supervisor.create('owner')
    scope.launch([sys.executable, '-c', 'pass'], env={}, cwd=tmp_path)
    wait_for(lambda: scope.snapshot()['processes'][0]['logs_drained'])
    with pytest.raises(IntegrationError) as caught:
        scope.launch([sys.executable, '-c', 'pass'], env={}, cwd=tmp_path)
    assert caught.value.code == 'overloaded'
    assert scope.resources_released()
    with pytest.raises(IntegrationError) as caught:
        scope.launch([sys.executable, '-c', 'pass'], env={}, cwd=tmp_path)
    assert caught.value.code == 'cancelled'


@pytest.mark.parametrize('required,exit_code', [(False, 0), (False, 7), (True, 0), (True, 7)])
def test_required_exit_monitor_preserves_optional_finite_jobs(fake_cgroups, tmp_path, required, exit_code):
    create, root, state = fake_cgroups
    supervisor = create()
    scope = supervisor.create('health')
    scope.launch([sys.executable, '-I', '-S', '-c', f'raise SystemExit({exit_code})'],
                 env={}, cwd=tmp_path, required_while_running=required)
    wait_for(lambda: scope.snapshot()['processes'][0]['logs_drained'])
    if required:
        wait_for(lambda: scope.resource_failure() == 'process_exited')
    else:
        assert scope.resource_failure() is None
    assert scope.snapshot()['processes'][0]['required_while_running'] is required
    assert scope.snapshot()['processes'][0]['returncode'] == exit_code
    assert scope.resources_released()
    scope.close()
    supervisor.close()


def test_required_exit_detected_before_inherited_log_pipes_close(fake_cgroups, tmp_path):
    create, root, state = fake_cgroups
    scope = create().create('health')
    program = '''
import subprocess, sys
subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(0.7)'])
'''
    scope.launch([sys.executable, '-I', '-S', '-c', program], env={}, cwd=tmp_path, required_while_running=True)
    wait_for(lambda: scope.resource_failure() == 'process_exited')
    assert not scope.snapshot()['processes'][0]['logs_drained']
    wait_for(lambda: scope.snapshot()['processes'][0]['logs_drained'])


def test_cached_health_probe_never_waits_for_cleanup_scope_lock(fake_cgroups):
    from threading import Event, Thread
    create, root, state = fake_cgroups
    scope = create().create('health')
    done, observed = Event(), []
    def probe():
        observed.append(scope.resource_failure())
        done.set()
    with scope._lock:
        scope._required_exit.set()
        thread = Thread(target=probe)
        thread.start()
        assert done.wait(0.5), 'shared watcher blocked behind scope cleanup'
    thread.join(1)
    assert observed == ['process_exited']


def test_required_flag_rejects_non_boolean_without_launch(fake_cgroups, tmp_path, monkeypatch):
    create, root, state = fake_cgroups
    scope = create().create('health')
    monkeypatch.setattr(mod.subprocess, 'Popen', Mock(side_effect=AssertionError('must not launch')))
    with pytest.raises(IntegrationError) as caught:
        scope.launch([sys.executable], env={}, cwd=tmp_path, required_while_running='true')
    assert caught.value.code == 'invalid_process'


def test_selector_failure_latches_for_all_scopes_and_retains_unverified_logs(fake_cgroups, tmp_path, monkeypatch):
    create, root, state = fake_cgroups
    supervisor = create()
    required = supervisor.create('required')
    optional = supervisor.create('optional')
    required.launch([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(60)'],
                    env={}, cwd=tmp_path, required_while_running=True)
    with supervisor._logs.lock:
        monkeypatch.setattr(supervisor._logs.selector, 'select', Mock(side_effect=OSError('selector failed')))
    wait_for(lambda: not supervisor._logs.thread.is_alive())
    assert supervisor._logs.failed.is_set()
    assert required.resource_failure() == optional.resource_failure() == 'resource_monitor_failed'
    assert all(row['log_error'] for row in required._processes.values())
    assert not required.resources_released()
    assert supervisor.describe()['owners']


def test_launch_after_monitor_failure_is_refused_before_process_creation(fake_cgroups, tmp_path, monkeypatch):
    create, root, state = fake_cgroups
    supervisor = create()
    with supervisor._logs.lock:
        monkeypatch.setattr(supervisor._logs.selector, 'select', Mock(side_effect=OSError('selector failed')))
    wait_for(lambda: supervisor._logs.failed.is_set())
    scope = supervisor.create('created-after-monitor-failure')
    popen = Mock(side_effect=AssertionError('must not create an unmonitored process'))
    monkeypatch.setattr(mod.subprocess, 'Popen', popen)
    with pytest.raises(IntegrationError) as caught:
        scope.launch([sys.executable], env={}, cwd=tmp_path)
    assert caught.value.code == 'resource_monitor_failed'
    assert scope.resource_failure() == 'resource_monitor_failed'
    assert not scope._processes and scope._launching == 0
    popen.assert_not_called()
    scope.close()
    supervisor.close()


@pytest.mark.parametrize('failure_point', ['registration', 'bootstrap_ready'])
def test_monitor_failure_during_launch_never_grants_target_exec(fake_cgroups, tmp_path, monkeypatch, failure_point):
    create, root, state = fake_cgroups
    supervisor = create()
    scope = supervisor.create('launch-race')
    marker = tmp_path / 'target-executed'
    if failure_point == 'registration':
        original = supervisor._logs.add
        def fail(record):
            supervisor._logs.failed.set()
            return original(record)
        monkeypatch.setattr(supervisor._logs, 'add', fail)
    else:
        original = mod._wait_readable
        def fail(fd, timeout):
            ready = original(fd, timeout)
            supervisor._logs.failed.set()
            return ready
        monkeypatch.setattr(mod, '_wait_readable', fail)
    with pytest.raises(IntegrationError) as caught:
        scope.launch([sys.executable, '-I', '-S', '-c',
                      'import pathlib,sys; pathlib.Path(sys.argv[1]).touch()', str(marker)],
                     env={}, cwd=tmp_path, required_while_running=True)
    assert caught.value.code == 'resource_monitor_failed'
    assert len(scope._processes) == 1 and scope._launching == 0
    record = next(iter(scope._processes.values()))
    wait_for(lambda: record['popen'].poll() is not None)
    assert not marker.exists()
    if failure_point == 'registration':
        assert record['log_error'] and record['open_logs'] == 0
        assert not scope.resources_released()


def test_orderly_monitor_close_does_not_report_failure():
    monitor = mod._LogDrainer(1024)
    monitor.close()
    monitor.thread.join(timeout=1)
    assert not monitor.thread.is_alive() and not monitor.failed.is_set()


@pytest.mark.parametrize('spec', [
    (['relative-program'], {}),
    ([sys.executable, '\0'], {}),
    ([sys.executable], {'BAD=KEY': 'x'}),
    ([sys.executable], {'SECRET': 'x' * 5000}),
])
def test_invalid_launch_specs_never_spawn(fake_cgroups, tmp_path, monkeypatch, spec):
    create, root, state = fake_cgroups
    scope = create().create('owner')
    popen = Mock(side_effect=AssertionError('must not spawn'))
    monkeypatch.setattr(mod.subprocess, 'Popen', popen)
    with pytest.raises(IntegrationError):
        scope.launch(spec[0], env=spec[1], cwd=tmp_path)
    popen.assert_not_called()
    assert scope.resources_released()


def test_child_bootstrap_parent_loss_before_grant_never_executes(fake_cgroups, tmp_path):
    create, root, state = fake_cgroups
    scope = create().create('owner')
    import select
    import tempfile
    ready_read, ready_write = os.pipe()
    grant_read, grant_write = os.pipe()
    child = None
    try:
        with tempfile.TemporaryFile() as environment:
            environment.write(b'{}')
            environment.seek(0)
            output = tmp_path / 'must-not-exist'
            child = subprocess.Popen([
                sys.executable, '-I', '-S', str(Path(mod.__file__).with_name('_process_worker.py')),
                str(scope._fd), str(ready_write), str(grant_read), str(environment.fileno()),
                sys.executable, '-I', '-S', '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).touch()', str(output),
            ], pass_fds=(scope._fd, ready_write, grant_read, environment.fileno()),
                close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={})
        os.close(ready_write)
        ready_write = None
        os.close(grant_read)
        grant_read = None
        ready, _, _ = select.select([ready_read], [], [], 3)
        assert ready and os.read(ready_read, 1) == b'R'
        os.close(grant_write)  # exactly what losing the parent does
        grant_write = None
        assert child.wait(timeout=3) == 125
        assert not output.exists()
    finally:
        for fd in (ready_read, ready_write, grant_read, grant_write):
            if fd is not None:
                os.close(fd)
        if child is not None:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            child.stdout.close()
            child.stderr.close()


def test_inheritable_parent_descriptor_is_not_given_to_adapter(fake_cgroups, tmp_path):
    create, root, state = fake_cgroups
    scope = create().create('owner')
    fd = os.open(tmp_path / 'parent-secret', os.O_CREAT | os.O_RDWR, 0o600)
    os.set_inheritable(fd, True)
    try:
        scope.launch([sys.executable, '-I', '-S', '-c', """
import os,sys
try:
    os.fstat(int(sys.argv[1]))
except OSError:
    raise SystemExit(0)
raise SystemExit(9)
""", str(fd)], env={}, cwd=tmp_path)
        wait_for(lambda: scope.snapshot()['processes'][0]['logs_drained'])
        assert scope.snapshot()['processes'][0]['returncode'] == 0
        assert scope.resources_released()
    finally:
        os.close(fd)


def test_two_owners_never_share_processes_logs_or_kill_target(fake_cgroups, tmp_path):
    create, root, state = fake_cgroups
    supervisor = create()
    first, second = supervisor.create('one'), supervisor.create('two')
    pid = first.launch([sys.executable, '-c', 'print("first")'], env={}, cwd=tmp_path)
    second.launch([sys.executable, '-c', 'print("second")'], env={}, cwd=tmp_path)
    wait_for(lambda: all(scope.snapshot()['processes'][0]['logs_drained'] for scope in (first, second)))
    with pytest.raises(IntegrationError):
        second.log_tail(pid, 'stdout')
    assert first.stop(timeout=0)
    rows = supervisor.describe()['owners']
    assert (root / rows['one']['name'] / 'cgroup.kill').read_text() == '1'
    assert (root / rows['two']['name'] / 'cgroup.kill').read_text() == ''
    assert second.snapshot()['processes'][0]['stdout_bytes'] > 0


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux deployment descriptor-pressure check')
def test_pipe_readiness_and_logs_support_high_descriptors():
    import fcntl
    import resource
    previous = resource.getrlimit(resource.RLIMIT_NOFILE)
    if previous[1] != resource.RLIM_INFINITY and previous[1] <= 1100:
        pytest.skip('host hard descriptor limit cannot exercise fd > 1024')
    stdout_read = stdout_write = stderr_read = stderr_write = high = None
    drainer = None
    try:
        if previous[0] != resource.RLIM_INFINITY and previous[0] <= 1100:
            resource.setrlimit(resource.RLIMIT_NOFILE,
                               (min(2048, previous[1]) if previous[1] != resource.RLIM_INFINITY else 2048,
                                previous[1]))
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        high = fcntl.fcntl(stdout_read, fcntl.F_DUPFD_CLOEXEC, 1100)
        assert high > 1024
        os.close(stdout_read)
        stdout_read = None
        popen = Mock(stdout=os.fdopen(high, 'rb'), stderr=os.fdopen(stderr_read, 'rb'))
        high = stderr_read = None  # now owned by the file objects/drainer
        record = {'id': 'test', 'popen': popen, 'stdout': bytearray(), 'stderr': bytearray(),
                  'stdout_bytes': 0, 'stderr_bytes': 0, 'open_logs': 2, 'log_error': False}
        drainer = mod._LogDrainer(1024)
        os.write(stdout_write, b'output')
        assert mod._wait_readable(popen.stdout.fileno(), 1)
        drainer.add(record)
        os.close(stdout_write)
        stdout_write = None
        os.close(stderr_write)
        stderr_write = None
        wait_for(lambda: drainer.closed(record))
        assert drainer.tail(record, 'stdout') == b'output'
    finally:
        for fd in (stdout_read, stdout_write, stderr_read, stderr_write, high):
            if fd is not None:
                os.close(fd)
        if drainer is not None:
            drainer.close()
        resource.setrlimit(resource.RLIMIT_NOFILE, previous)


def test_disconnect_preserves_recovered_owner_and_never_proves_release(fake_cgroups):
    create, root, state = fake_cgroups
    supervisor = create()
    scope = supervisor.create('owner')
    name = supervisor.describe()['owners']['owner']['name']
    (root / name / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
    supervisor.disconnect()
    assert not scope.resources_released()
    assert json.loads((state / 'scopes.json').read_text())['scopes']['owner']['state'] == 'ready'
    recovered = create()
    reopened = recovered.reopen('owner')
    assert not reopened.resources_released()
    recovered.disconnect()


def test_disconnect_refuses_locally_launched_processes(fake_cgroups, tmp_path):
    create, root, state = fake_cgroups
    supervisor = create()
    scope = supervisor.create('owner')
    scope.launch([sys.executable, '-c', 'pass'], env={}, cwd=tmp_path)
    wait_for(lambda: scope.snapshot()['processes'][0]['logs_drained'])
    with pytest.raises(IntegrationError):
        supervisor.disconnect()
    assert not supervisor.describe()['closed']
    assert scope.resources_released()
    scope.close()
    supervisor.close()


def test_offline_reconcile_only_accepts_persisted_released_missing_scope(fake_cgroups):
    create, root, state = fake_cgroups
    supervisor = create()
    scope = supervisor.create('owner')
    row = supervisor.describe()['owners']['owner']
    supervisor.disconnect()
    payload = json.loads((state / 'scopes.json').read_text())
    payload['scopes']['owner']['state'] = 'released'
    (state / 'scopes.json').write_text(json.dumps(payload))
    (root / row['name']).rmdir()
    recovered = create()
    assert recovered.reconcile_released('owner')
    assert recovered.describe()['owners'] == {}
    recovered.close()
