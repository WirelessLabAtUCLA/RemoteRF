#!/usr/bin/env python3
"""Isolated Linux process-only qualification; never opens or selects a radio.

Run in a transient systemd --user unit named remoterf-supervision-soak-*, with
Delegate=yes and DelegateSubgroup=manager. Enable +pids +memory in that unit's
parent cgroup before invoking this script. The supplied root must be precisely
the parent of this process's manager subgroup. A failed run preserves evidence
and cannot authorize hardware activation or substitute for deployed-route tests.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import threading
import time

from remoterf_device_core.integrations import IntegrationError
from remoterf_device_core.integrations import operator, supervision
from remoterf_device_core.integrations.ownership import Gate, Journal


THRESHOLDS = {
    'all_cycles_pass': True,
    'cycle_min_interval_seconds': 1.0,
    'cleanup_budget_seconds': 5.0,
    'cleanup_observed_max_seconds': 5.5,
    'log_tail_max_bytes_per_stream': 4096,
    'output_bytes_per_stream': 131072,
    'idle_fd_growth_max': 2,
    'idle_thread_growth_max': 0,
    'parent_rss_growth_max_bytes': 32 * 1024**2,
    'parent_rss_latter_half_growth_max_bytes': 16 * 1024**2,
    'unit_memory_peak_max_bytes': 256 * 1024**2,
    'remaining_scopes': 0,
    'remaining_hardware_leases': 0,
    'remaining_process_records': 0,
    'concurrent_owner_wave': 16,
    'max_scopes': 64,
    'max_processes_per_scope': 4,
    'scope_pids_max': 128,
    'scope_memory_max_bytes': 512 * 1024**2,
    'normal_and_restart_workers_required_while_running': True,
}

BENIGN = '''
import os, subprocess, sys, time
assert 'REMOTERF_SOAK_PARENT_SECRET' not in os.environ
assert os.environ['EXPLICIT'] == 'process-soak'
subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(120)'],
                 start_new_session=True)
os.write(1, b'o' * 131072)
os.write(2, b'e' * 131072)
print('READY', flush=True)
time.sleep(120)
'''
SLEEPER = 'import time; print("READY", flush=True); time.sleep(600)'


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('bounded readiness check timed out')


def snapshot():
    return {
        'fds': len(list(Path('/proc/self/fd').iterdir())),
        'threads': len(list(Path('/proc/self/task').iterdir())),
        'rss_bytes': int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE'),
    }


def persist(path, report):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = path.with_suffix('.tmp')
    with staging.open('w') as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    staging.replace(path)


def installed_identity():
    package = Path(supervision.__file__).resolve().parent
    files = {path.name: sha256(path.read_bytes()).hexdigest()
             for path in sorted(package.glob('*.py'))}
    return {'core_version': importlib.metadata.version('remoterf-device-core'),
            'module_path': str(package), 'module_sha256': files,
            'script_sha256': sha256(Path(__file__).read_bytes()).hexdigest()}


def validate_root(root):
    check(sys.platform == 'linux', 'Linux is required')
    relative = next(line.split(':', 2)[2].strip() for line in Path('/proc/self/cgroup').read_text().splitlines()
                    if line.startswith('0::'))
    current = Path('/sys/fs/cgroup') / relative.lstrip('/')
    check(current.name == 'manager' and current.parent == root,
          'root must be this isolated unit manager subgroup parent')
    check(root.name.startswith('remoterf-supervision-soak-') and root.name.endswith('.service'),
          'a dedicated remoterf-supervision-soak-* transient service is required')
    check((root / 'cgroup.procs').read_text().strip() == '', 'delegated parent must contain no direct process')
    check({'memory', 'pids'} <= set((root / 'cgroup.subtree_control').read_text().split()),
          'memory and pids must already be enabled by the operator wrapper')
    check(not any(path.name.startswith('rrf-') for path in root.iterdir()), 'existing owned scopes are forbidden')


def launch(scope, work, program=SLEEPER):
    return scope.launch([sys.executable, '-I', '-S', '-c', program],
                        env={'EXPLICIT': 'process-soak'}, cwd=work, required_while_running=True)


def stop_close(scope, durations):
    started = time.monotonic()
    check(scope.stop(timeout=THRESHOLDS['cleanup_budget_seconds']), 'owned scope did not prove release')
    elapsed = time.monotonic() - started
    durations.append(elapsed)
    check(elapsed <= THRESHOLDS['cleanup_observed_max_seconds'], 'cleanup exceeded its bounded budget')
    check(scope.resources_released(), 'independent scope release failed')
    scope.close()


def crash_child(root, state, work):
    journal = Journal(state / 'ownership')
    gate = Gate('qualification:process-only', journal)
    lease = gate.reserve(session='process-soak-not-a-bearer', generation=1,
                         integration='org.example.process_soak', resources='orphan')
    gate.persist(lease)
    supervisor = supervision.CgroupSupervisor(root, state / 'processes')
    scope = supervisor.create('orphan')
    process = launch(scope, work)
    wait_for(lambda: b'READY' in scope.log_tail(process, 'stdout'))
    os._exit(23)


def recover_crash(root, state, work, durations):
    child = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--crash-child',
                            '--cgroup-root', str(root), '--state-dir', str(state)],
                           cwd=work, env={}, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    check(child.returncode == 23, 'crash fixture did not exit at the intended point')
    view = operator.inspect(state, root)
    check(view['backend_verified'] and view['hardware'][0]['held']
          and view['processes'][0]['populated'], 'crash did not retain a real owned process and lease')
    check('process-soak-not-a-bearer' not in json.dumps(view), 'inspection exposed the fixture session')
    lease = view['hardware'][0]['lease']
    # The actual offline entry point must obtain both writer locks and match the
    # recorded cgroup; no PID from this script is ever used to terminate a child.
    started = time.monotonic()
    result = subprocess.run([sys.executable, '-I', '-m', 'remoterf_device_core.integrations.operator', 'recover',
                             '--state-dir', str(state), '--cgroup-root', str(root),
                             '--physical-id', 'qualification:process-only', '--lease', lease, '--timeout', '5'],
                            cwd=work, env={}, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    durations.append(time.monotonic() - started)
    check(result.returncode == 0 and json.loads(result.stdout)['recovered'], 'offline recovery failed')
    check(durations[-1] <= THRESHOLDS['cleanup_observed_max_seconds'], 'offline recovery exceeded its budget')
    check(json.loads((state / 'ownership' / 'ownership.json').read_text()) == {}, 'hardware lease remains')
    check(json.loads((state / 'processes' / 'scopes.json').read_text())['scopes'] == {}, 'process registry remains')
    check(not any(path.name.startswith('rrf-') for path in root.iterdir()), 'recovered cgroup remains')


def admission_and_concurrency(supervisor, sentinel, work, durations):
    # The existing sentinel accounts for one of the supervisor's 64 records.
    empty = [supervisor.create(f'capacity-{index}') for index in range(63)]
    try:
        try:
            supervisor.create('scope-over-limit')
        except IntegrationError as exc:
            check(exc.code == 'overloaded', 'scope limit returned the wrong refusal')
        else:
            raise AssertionError('scope limit admitted a 65th owner')
    finally:
        for scope in empty:
            scope.close()
    limited = supervisor.create('launch-capacity')
    try:
        for _ in range(4):
            launch(limited, work)
        try:
            launch(limited, work)
        except IntegrationError as exc:
            check(exc.code == 'overloaded', 'launch limit returned the wrong refusal')
        else:
            raise AssertionError('launch limit admitted a fifth process')
    finally:
        stop_close(limited, durations)
    wave = [supervisor.create(f'wave-{index}') for index in range(16)]
    try:
        processes = [(scope, launch(scope, work, BENIGN)) for scope in wave]
        for scope, process in processes:
            wait_for(lambda: b'READY' in scope.log_tail(process, 'stdout'))
        check(sentinel.snapshot()['processes'][0]['returncode'] is None, 'independent sentinel was killed')
    finally:
        for scope in wave:
            stop_close(scope, durations)


def run(args):
    validate_root(args.cgroup_root)
    check(args.iterations >= 100 and args.iterations <= 1000, 'iterations must be between 100 and 1000')
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.environ['REMOTERF_SOAK_PARENT_SECRET'] = 'must-not-be-inherited'
    report = {'passed': False, 'started_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'isolated-real-cgroup-process-only; no radios, no deployed service, no activation',
              'thresholds': THRESHOLDS, 'requested_iterations': args.iterations,
              'python': sys.version, 'kernel': platform.release(), 'identity': installed_identity(),
              'cycles': [], 'cleanup_verified': False}
    persist(args.evidence, report)  # Thresholds precede every exercised process.
    print(json.dumps({'event': 'thresholds_recorded', 'thresholds': THRESHOLDS}), flush=True)
    started = time.monotonic()
    baseline = snapshot()
    recovery_root = args.cgroup_root / 'recovery-test'
    recovery_root.mkdir()
    (recovery_root / 'cgroup.subtree_control').write_text('+pids +memory')
    supervisor = supervision.CgroupSupervisor(args.cgroup_root, args.state_dir / 'processes', max_log_bytes=4096)
    durations = []
    sentinel = supervisor.create('sentinel')
    sentinel_id = launch(sentinel, args.state_dir)
    wait_for(lambda: b'READY' in sentinel.log_tail(sentinel_id, 'stdout'))
    active = None
    try:
        admission_and_concurrency(supervisor, sentinel, args.state_dir, durations)
        idle_baseline = snapshot()
        for index in range(args.iterations):
            cycle_started = time.monotonic()
            mode = ('normal' if index % 10 < 6 else
                    {6: 'target_exec_failure', 7: 'pre_spawn_failure', 8: 'injected_pre_ack_timeout', 9: 'restart_recovery'}[index % 10])
            if mode == 'restart_recovery':
                recover_crash(recovery_root, args.state_dir / f'restart-{index}', args.state_dir, durations)
            else:
                active = supervisor.create(f'cycle-{index}')
                group_name = supervisor.describe()['owners'][active.owner_id]['name']
                group = args.cgroup_root / group_name
                check((group / 'pids.max').read_text().strip() == '128', 'unexpected pids containment')
                check((group / 'memory.max').read_text().strip() == str(512 * 1024**2), 'unexpected memory containment')
                if mode == 'normal':
                    process = launch(active, args.state_dir, BENIGN)
                    wait_for(lambda: b'READY' in active.log_tail(process, 'stdout')
                             and active.snapshot()['processes'][0]['stderr_bytes'] == 131072)
                    check(len(active.log_tail(process, 'stdout')) <= 4096
                          and len(active.log_tail(process, 'stderr')) == 4096, 'unbounded retained output')
                    check(not active.resources_released(), 'live descendants were reported released')
                elif mode == 'target_exec_failure':
                    active.launch(['/does-not-exist/remoterf-process-soak'], env={}, cwd=args.state_dir)
                    wait_for(lambda: active.snapshot()['processes'][0]['returncode'] == 126)
                    check(active.resource_failure() is None, 'optional finite failure changed required-worker policy')
                elif mode == 'pre_spawn_failure':
                    try:
                        active.launch([sys.executable, '-I', '-S', '-c', SLEEPER], env={},
                                      cwd=args.state_dir / 'does-not-exist')
                    except FileNotFoundError:
                        pass
                    else:
                        raise AssertionError('missing cwd unexpectedly launched')
                else:
                    previous = supervision._wait_readable
                    supervision._wait_readable = lambda fd, timeout: False
                    try:
                        try:
                            launch(active, args.state_dir)
                        except IntegrationError as exc:
                            check(exc.code == 'process_launch_failed', 'pre-ack failure returned the wrong refusal')
                        else:
                            raise AssertionError('injected readiness refusal unexpectedly succeeded')
                    finally:
                        supervision._wait_readable = previous
                stop_close(active, durations)
                active = None
            check(list(supervisor.describe()['owners']) == ['sentinel'], 'scope record leaked between cycles')
            check(sentinel.snapshot()['processes'][0]['returncode'] is None, 'independent owner was killed')
            current = snapshot()
            check(current['fds'] <= idle_baseline['fds'] + 2, 'idle file descriptors accumulated')
            check(current['threads'] <= idle_baseline['threads'], 'idle threads accumulated')
            report['cycles'].append({'iteration': index + 1, 'mode': mode, **current,
                                     'work_seconds': time.monotonic() - cycle_started})
            remaining = THRESHOLDS['cycle_min_interval_seconds'] - (time.monotonic() - cycle_started)
            if remaining > 0:
                time.sleep(remaining)
            if (index + 1) % 10 == 0:
                print(json.dumps({'event': 'progress', 'completed': index + 1, **current}), flush=True)
                persist(args.evidence, report)
        readings = report['cycles'][10:]
        rss = [row['rss_bytes'] for row in readings]
        check(max(rss) - min(rss) <= THRESHOLDS['parent_rss_growth_max_bytes'], 'RSS grew beyond warmup allowance')
        midpoint = len(readings) // 2
        latter_growth = statistics.median(rss[midpoint:]) - statistics.median(rss[:midpoint])
        check(latter_growth <= THRESHOLDS['parent_rss_latter_half_growth_max_bytes'], 'late RSS trend exceeded limit')
        report['metrics'] = {'idle_baseline': idle_baseline, 'max_idle_fds': max(row['fds'] for row in readings),
            'max_idle_threads': max(row['threads'] for row in readings), 'rss_range_bytes': max(rss) - min(rss),
            'rss_latter_half_growth_bytes': latter_growth, 'cleanup_max_seconds': max(durations),
            'cleanup_median_seconds': statistics.median(durations), 'cleanup_count': len(durations),
            'cycle_counts': dict(Counter(row['mode'] for row in report['cycles']))}
        report['passed'] = True
    except BaseException as exc:
        report['error'] = {'type': type(exc).__name__, 'message': str(exc)[:512]}
        raise
    finally:
        try:
            if active is not None:
                stop_close(active, durations)
            stop_close(sentinel, durations)
            supervisor.close()
            wait_for(lambda: not any(thread.name == 'remoterf-process-logs' for thread in threading.enumerate()))
            check(not any(path.name.startswith('rrf-') for path in args.cgroup_root.iterdir()), 'final owned cgroups remain')
            check(not any(path.name.startswith('rrf-') for path in recovery_root.iterdir()), 'recovery cgroups remain')
            check(json.loads((args.state_dir / 'processes' / 'scopes.json').read_text())['scopes'] == {},
                  'final process registry remains')
            recovery_root.rmdir()
            final = snapshot()
            check(final['fds'] <= baseline['fds'] + 2 and final['threads'] <= baseline['threads'],
                  'shutdown left descriptors or threads')
            unit_peak = int((args.cgroup_root / 'memory.peak').read_text())
            check(unit_peak <= THRESHOLDS['unit_memory_peak_max_bytes'], 'unit memory exceeded the predeclared bound')
            report.update(cleanup_verified=True, final_resources=final, unit_memory_peak_bytes=unit_peak)
        except BaseException as exc:
            report['passed'] = False
            report['cleanup_error'] = {'type': type(exc).__name__, 'message': str(exc)[:512]}
            raise
        finally:
            report['elapsed_seconds'] = time.monotonic() - started
            report['finished_at'] = datetime.now(timezone.utc).isoformat()
            persist(args.evidence, report)
    print(json.dumps({'event': 'complete', 'passed': report['passed'], 'metrics': report['metrics']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cgroup-root', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--iterations', type=int, default=100)
    parser.add_argument('--crash-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.crash_child:
        crash_child(args.cgroup_root, args.state_dir, Path.cwd())
    if args.evidence is None:
        parser.error('--evidence is required')
    run(args)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
