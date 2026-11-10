"""Offline, local-only recovery for managed application process ownership.

Usage: python -m remoterf_device_core.integrations.operator inspect|recover|cleanup-orphan ...
STATE is the controller's db/integrations directory, containing ownership/ and
processes/. Inspection is read-only and can run while the server holds its
locks. Its snapshots are not atomic across journals and do not authorize work.

Recovery takes both exclusive writer locks. The framework holds its ownership
lock for the lifetime of managed native ownership; obtaining it excludes that
managed controller. This does NOT audit external/unmanaged hardware owners,
interfaces, shared memory or files. The installed qualified adapter must use
only the resource classes whose release is independently verified here.
There is no remote recovery endpoint and no arbitrary PID signalling.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

from .ownership import Gate, Journal, _unique_object
from .payload import IntegrationError, object_data
from . import supervision as backend


class RecoveryError(IntegrationError):
    def __init__(self, *, physical_released=False):
        super().__init__('process_recovery_required', 'managed process ownership requires operator reconciliation')
        self.physical_released = physical_released


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise RecoveryError() from None
    try:
        with os.fdopen(fd, 'rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        return object_data(json.loads(raw, object_pairs_hook=_unique_object))
    except (OSError, ValueError, TypeError, RecursionError, IntegrationError):
        raise RecoveryError() from None


def _ownership_snapshot(state_dir):
    rows = _read(Path(state_dir) / 'ownership' / 'ownership.json') or {}
    if len(rows) > 64:
        raise RecoveryError()
    for physical, row in rows.items():
        fields = {'lease', 'session', 'generation', 'integration'}
        if (not 1 <= len(physical) <= 256 or type(row) is not dict or not fields <= set(row)
                or set(row) - fields - {'resources'}
                or any(type(row[key]) is not str or not row[key] for key in ('lease', 'session', 'integration'))
                or type(row['generation']) is not int or row['generation'] < 1
                or ('resources' in row and (type(row['resources']) is not str
                    or not backend._OWNER.fullmatch(row['resources'])))):
            raise RecoveryError()
    return rows


def _process_snapshot(state_dir):
    data = _read(Path(state_dir) / 'processes' / 'scopes.json')
    if data is None:
        return None
    if (set(data) != {'boot', 'root', 'scopes'} or type(data['boot']) is not str
            or not re.fullmatch(r'[0-9a-f-]{36}', data['boot'])
            or type(data['root']) is not list or len(data['root']) != 2
            or any(type(value) is not int or value < 0 for value in data['root'])
            or type(data['scopes']) is not dict or len(data['scopes']) > 64):
        raise RecoveryError()
    names = set()
    for owner, row in data['scopes'].items():
        if (not backend._OWNER.fullmatch(owner) or type(row) is not dict
                or set(row) != {'name', 'identity', 'state'} or type(row['name']) is not str
                or not backend._SCOPE.fullmatch(row['name']) or row['name'] in names
                or type(row['state']) is not str or row['state'] not in {'creating', 'ready', 'released'}):
            raise RecoveryError()
        identity = row['identity']
        if (row['state'] == 'creating' and identity is not None
                or row['state'] != 'creating' and (type(identity) is not list or len(identity) != 2
                    or any(type(value) is not int or value < 0 for value in identity))):
            raise RecoveryError()
        names.add(row['name'])
    return data


def inspect(state_dir, cgroup_root):
    """Bounded read-only metadata; deliberately omit bearer session IDs/logs."""
    hardware = _ownership_snapshot(state_dir)
    process_data = _process_snapshot(state_dir)
    output = {'inspection_only': True, 'managed_resources_only': True,
              'hardware': [{'physical_id': physical, 'lease': row['lease'],
                            'integration': row['integration'], 'generation': row['generation'],
                            'resource_owner': row.get('resources'), 'held': True}
                           for physical, row in sorted(hardware.items())],
              'processes': [], 'unknown_scopes': [], 'backend_verified': False}
    scopes = process_data['scopes'] if process_data else {}
    root_fd = None
    try:
        backend._require_linux()
        root_fd = backend._open_dir(cgroup_root)
        backend._assert_cgroup2(root_fd)
        if process_data and (process_data['boot'] != backend._boot_id()
                             or process_data['root'] != backend._identity(root_fd)):
            raise RecoveryError()
        output['backend_verified'] = True
        known = {row['name'] for row in scopes.values()}
        with os.scandir(root_fd) as entries:
            for index, entry in enumerate(entries):
                if index >= 256:
                    raise RecoveryError()
                if entry.name.startswith(backend._PREFIX) and entry.name not in known:
                    if len(output['unknown_scopes']) >= 64:
                        raise RecoveryError()
                    output['unknown_scopes'].append(entry.name)
    except (OSError, IntegrationError):
        output['backend_verified'] = False
    for owner, row in sorted(scopes.items()):
        item = {'owner_id': owner, 'state': row['state'], 'identity_verified': False, 'populated': None}
        if output['backend_verified'] and row['state'] != 'creating':
            fd = None
            try:
                fd = backend._open_dir(row['name'], dir_fd=root_fd)
                if backend._identity(fd) != row['identity']:
                    raise RecoveryError()
                item.update(identity_verified=True, populated=backend._populated(fd))
            except (OSError, ValueError, IntegrationError):
                pass
            finally:
                if fd is not None:
                    os.close(fd)
        output['processes'].append(item)
    if root_fd is not None:
        os.close(root_fd)
    output['recovery_required'] = bool(hardware or scopes or output['unknown_scopes'] or not output['backend_verified'])
    return object_data(output)


def _journal(state_dir):
    path = Path(state_dir) / 'ownership' / 'ownership.json'
    # Missing hardware records are not evidence that prior ownership was empty.
    # A normal post-release journal persists an explicit empty object.
    if not path.is_file() or path.is_symlink():
        raise RecoveryError()
    try:
        return Journal(path.parent)
    except IntegrationError as exc:
        if exc.code == 'journal_busy':
            raise
        raise RecoveryError() from None
    except OSError:
        raise RecoveryError() from None


def _supervisor(cgroup_root, state_dir):
    try:
        return backend.CgroupSupervisor(cgroup_root, Path(state_dir) / 'processes')
    except IntegrationError as exc:
        if exc.code == 'journal_busy':
            raise
        raise RecoveryError() from None
    except OSError:
        raise RecoveryError() from None


def _arguments(physical_id=None, lease=None, owner_id=None, timeout=5):
    if (type(timeout) not in (int, float) or not 0 <= timeout <= 30
            or physical_id is not None and (type(physical_id) is not str or not 1 <= len(physical_id) <= 256)
            or lease is not None and (type(lease) is not str or not 1 <= len(lease) <= 128)
            or owner_id is not None and (type(owner_id) is not str or not backend._OWNER.fullmatch(owner_id))):
        raise IntegrationError('invalid_request', 'invalid bounded recovery request')


def recover(state_dir, cgroup_root, *, physical_id, lease, timeout=5):
    _arguments(physical_id=physical_id, lease=lease, timeout=timeout)
    journal = _journal(state_dir)
    supervisor = None
    physical_released = False
    try:
        rows = journal.records()
        row = rows.get(physical_id)
        if row is None or row['lease'] != lease:
            raise IntegrationError('stale_owner', 'the requested hardware lease is not current')
        owner = row.get('resources')
        if (type(owner) is not str or not backend._OWNER.fullmatch(owner)
                or sum(other.get('resources') == owner for other in rows.values()) != 1):
            raise RecoveryError()
        supervisor = _supervisor(cgroup_root, state_dir)
        scope = supervisor.reopen(owner)
        if not scope.stop(timeout=timeout) or not scope.resources_released():
            raise RecoveryError()
        Gate(physical_id, journal).release(lease, verify=scope.resources_released)
        physical_released = True
        scope.close()
        return {'recovered': True, 'physical_id': physical_id, 'resource_owner': owner,
                'managed_resources_only': True}
    except IntegrationError as exc:
        if exc.code in {'stale_owner', 'journal_busy'}:
            raise
        raise RecoveryError(physical_released=physical_released) from None
    except (OSError, ValueError):
        raise RecoveryError(physical_released=physical_released) from None
    finally:
        try:
            if supervisor is not None:
                supervisor.disconnect()
        finally:
            journal.close()


def cleanup_orphan(state_dir, cgroup_root, *, owner_id, timeout=5):
    """Stop one unreferenced process owner without modifying hardware leases."""
    _arguments(owner_id=owner_id, timeout=timeout)
    journal = _journal(state_dir)
    supervisor = None
    try:
        rows = journal.records()
        if any('resources' not in row or row['resources'] == owner_id for row in rows.values()):
            raise RecoveryError()
        supervisor = _supervisor(cgroup_root, state_dir)
        row = supervisor.describe()['owners'].get(owner_id)
        if row is None:
            raise RecoveryError()
        reconciled = row['state'] == 'released' and supervisor.reconcile_released(owner_id)
        if not reconciled:
            scope = supervisor.reopen(owner_id)
            if not scope.stop(timeout=timeout) or not scope.resources_released():
                raise RecoveryError()
            scope.close()
        return {'cleaned': True, 'resource_owner': owner_id, 'hardware_leases_changed': False}
    except IntegrationError as exc:
        if exc.code == 'journal_busy':
            raise
        raise RecoveryError() from None
    except (OSError, ValueError):
        raise RecoveryError() from None
    finally:
        try:
            if supervisor is not None:
                supervisor.disconnect()
        finally:
            journal.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'recover', 'cleanup-orphan'):
        sub = commands.add_parser(name)
        sub.add_argument('--state-dir', type=Path, required=True)
        sub.add_argument('--cgroup-root', type=Path, required=True)
        if name == 'recover':
            sub.add_argument('--physical-id', required=True)
            sub.add_argument('--lease', required=True)
        if name == 'cleanup-orphan':
            sub.add_argument('--owner-id', required=True)
        if name != 'inspect':
            sub.add_argument('--timeout', type=float, default=5)
    args = parser.parse_args(argv)
    try:
        if args.command == 'inspect':
            result = inspect(args.state_dir, args.cgroup_root)
        elif args.command == 'recover':
            result = recover(args.state_dir, args.cgroup_root, physical_id=args.physical_id,
                             lease=args.lease, timeout=args.timeout)
        else:
            result = cleanup_orphan(args.state_dir, args.cgroup_root, owner_id=args.owner_id, timeout=args.timeout)
        print(json.dumps(object_data(result), sort_keys=True))
        return 0
    except (OSError, ValueError, IntegrationError) as exc:
        code = exc.code if isinstance(exc, IntegrationError) else 'process_recovery_required'
        print(json.dumps({'error': code, 'physical_released': getattr(exc, 'physical_released', False)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
