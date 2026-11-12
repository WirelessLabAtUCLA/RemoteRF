"""Safety invariants of the operator-only descendant qualification injection."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'usrp_e2e_descendants.py'
spec = importlib.util.spec_from_file_location('receiver_descendant_harness_test', SCRIPT)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


@pytest.mark.parametrize('subgroup,accepted', [('manager', True), ('runner', True), ('outside', False)])
def test_guarded_qualifier_subgroup_must_belong_to_exact_root(monkeypatch, tmp_path, subgroup, accepted):
    root = tmp_path / 'owned'; root.mkdir()
    (root / 'cgroup.type').write_text('domain\n')
    (root / subgroup).mkdir()
    membership = tmp_path / 'membership'; membership.write_text('0::/owned/' + subgroup + '\n')
    def path(value):
        if value == '/proc/self/cgroup': return membership
        if value == '/sys/fs/cgroup': return tmp_path
        return Path(value)
    monkeypatch.setattr(h, 'Path', path)
    if accepted:
        proof = h.root_proof(root)
        assert proof['qualifier_subgroup'] == subgroup
        assert proof['guardian_required_until_independent_release'] is True
    else:
        with pytest.raises(RuntimeError): h.root_proof(root)


def test_libc_pidfd_fallback_preserves_flags_and_errno(monkeypatch):
    monkeypatch.delattr(h.os, 'pidfd_open', raising=False)
    monkeypatch.delattr(h.signal, 'pidfd_send_signal', raising=False)
    library = SimpleNamespace(pidfd_open=Mock(return_value=73), pidfd_send_signal=Mock(return_value=0))
    monkeypatch.setattr(h, '_pidfd_library', lambda: library)
    assert h.pidfd_open(421) == 73
    library.pidfd_open.assert_called_once_with(421, 0)
    h.pidfd_send_signal(73, h.signal.SIGKILL)
    library.pidfd_send_signal.assert_called_once_with(73, h.signal.SIGKILL, None, 0)
    library.pidfd_open.return_value = -1
    monkeypatch.setattr(h.ctypes, 'get_errno', lambda: 3)
    with pytest.raises(OSError) as error: h.pidfd_open(421)
    assert error.value.errno == 3
    library.pidfd_send_signal.return_value = -1
    with pytest.raises(OSError) as error: h.pidfd_send_signal(73, 0)
    assert error.value.errno == 3


def test_cli_help_and_serial_preflight_do_not_load_hardware(tmp_path):
    help_run = subprocess.run([sys.executable, '-I', str(SCRIPT), '--help'], capture_output=True, text=True)
    assert help_run.returncode == 0 and '--lock-fd' in help_run.stdout
    command = [sys.executable, '-I', str(SCRIPT), '--serial', 'unsafe,selector',
        '--reservation-id', 'test-only',
        '--device-id', '40']
    for name in ('state-dir', 'evidence', 'cgroup-root', 'authority-command'):
        command.extend(['--' + name, str(tmp_path / name)])
    bad = subprocess.run(command, capture_output=True, text=True)
    assert bad.returncode == 2 and 'one safe explicit USRP serial is required' in bad.stderr
    assert not (tmp_path / 'state-dir').exists()


def test_fixed_wrapper_launches_only_unchanged_probe_with_inherited_logs(monkeypatch):
    child = Mock(); child.wait.return_value = 71
    popen = Mock(return_value=child)
    monkeypatch.setattr(h.subprocess, 'Popen', popen)
    assert h.wrapper('32C78EE', '{}') == 71
    argv = popen.call_args.args[0]; kwargs = popen.call_args.kwargs
    assert argv[:5] == [h.sys.executable, '-I', '-u', '-m', 'rrf_example_usrp_probe.worker']
    assert argv[5:8] == ['--serial', '32C78EE', '--configuration']
    assert json.loads(argv[8])['samples'] == 4096
    assert kwargs['start_new_session'] and kwargs['close_fds']
    assert 'stdout' not in kwargs and 'stderr' not in kwargs


@pytest.mark.parametrize('configuration', ['{"executable":"other"}', '{"gain":50}', '{"samples":true}'])
def test_wrapper_rejects_unbounded_or_custom_launch_configuration(monkeypatch, configuration):
    popen = Mock(); monkeypatch.setattr(h.subprocess, 'Popen', popen)
    with pytest.raises(ValueError): h.wrapper('32C78EE', configuration)
    popen.assert_not_called()


def fake_scope(monkeypatch, tmp_path):
    popen = Mock(pid=421); popen.poll.return_value = None
    underlying = SimpleNamespace(_lock=RLock(), _verify_identity=Mock(),
        _processes={'owned': {'popen': popen}}, launch=Mock(return_value='owned'))
    wrapped = h.DescendantScope(None, 'owner', tmp_path, SCRIPT)
    wrapped.scope = underlying
    monkeypatch.setattr(h, 'kernel_processes', lambda scope: ({421, 422}, 1))
    opened = Mock(return_value=88); monkeypatch.setattr(h.os, 'pidfd_open', opened, raising=False)
    return wrapped, underlying, popen, opened


def test_launch_captures_pidfd_for_exact_owned_required_wrapper(monkeypatch, tmp_path):
    wrapped, scope, popen, opened = fake_scope(monkeypatch, tmp_path)
    command = [h.sys.executable, '-I', '-u', '-m', 'rrf_example_usrp_probe.worker',
               '--serial', '32C78EE', '--configuration', '{}']
    assert wrapped.launch(command, env={'LANG':'C.UTF-8'}, cwd=tmp_path, required_while_running=True) == 'owned'
    assert scope.launch.call_args.args[0] == [h.sys.executable, '-I', '-u', str(SCRIPT), '--worker-wrapper', '32C78EE', '{}']
    assert scope.launch.call_args.kwargs['required_while_running'] is True
    opened.assert_called_once_with(421)
    assert wrapped.process is popen and wrapped.pidfd == 88
    signal = Mock(); monkeypatch.setattr(h.signal, 'pidfd_send_signal', signal, raising=False)
    wrapped.kill_required_wrapper()
    signal.assert_called_once_with(88, h.signal.SIGKILL)


def test_launch_refuses_wrapper_exit_during_pidfd_capture(monkeypatch, tmp_path):
    wrapped, scope, popen, opened = fake_scope(monkeypatch, tmp_path)
    popen.poll.side_effect = [None, 0]
    closed = Mock(); monkeypatch.setattr(h.os, 'close', closed)
    command = [h.sys.executable, '-I', '-u', '-m', 'rrf_example_usrp_probe.worker',
               '--serial', '32C78EE', '--configuration', '{}']
    with pytest.raises(RuntimeError, match='during pidfd capture'):
        wrapped.launch(command, env={}, cwd=tmp_path, required_while_running=True)
    closed.assert_called_once_with(88)
    assert wrapped.process is None and wrapped.pidfd is None


@pytest.mark.parametrize('already_exited,owned', [(True, True), (False, False)])
def test_wrapper_kill_refuses_missing_liveness_or_scope_membership(monkeypatch, tmp_path, already_exited, owned):
    wrapped, scope, popen, opened = fake_scope(monkeypatch, tmp_path)
    wrapped.pidfd = 88; wrapped.process = popen
    popen.poll.return_value = 1 if already_exited else None
    monkeypatch.setattr(h, 'kernel_processes', lambda scope: ({421} if owned else {422}, 1))
    signal = Mock(); monkeypatch.setattr(h.signal, 'pidfd_send_signal', signal, raising=False)
    with pytest.raises(RuntimeError): wrapped.kill_required_wrapper()
    signal.assert_not_called()


@pytest.mark.parametrize('holder,processes,populated,passes', [
    (b'421\n', {421}, 1, True), (b'999\n', {421}, 1, False),
    (b'', {421}, 1, False), (b'421\n', set(), 0, False),
])
def test_positive_usb_descendant_proof_requires_current_owned_population(monkeypatch, holder, processes, populated, passes):
    scope = object()
    monkeypatch.setattr(h, 'kernel_processes', lambda s: (processes, populated))
    monkeypatch.setattr(h, 'usb_node', lambda serial: Path('/dev/bus/usb/003/015'))
    monkeypatch.setattr(h.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=holder))
    if passes:
        result = h.receiver_observation(scope, '32C78EE', minimum_processes=1)
        assert result['usb_owners_inside_scope'] and result['process_count'] == 1
        assert 'pids' not in result
    else:
        with pytest.raises(RuntimeError): h.receiver_observation(scope, '32C78EE', minimum_processes=1)
