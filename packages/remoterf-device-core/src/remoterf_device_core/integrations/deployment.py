"""Operator integration configuration, separate from RPC data.

A missing config is disabled. Malformed optional adapter metadata cannot break
raw UHD; ownership/recovery ambiguity must still prevent native acquisition.
Only explicit, serial-addressed single USRPs are eligible in this release.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re

from .ownership import _unique_object
from .payload import IntegrationError, object_data
from .registry import Manifest, Registry


def read_object(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise IntegrationError('payload_limit', 'operator configuration exceeds its budget')
    try:
        return object_data(json.loads(raw, object_pairs_hook=_unique_object))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise IntegrationError('invalid_configuration', 'invalid operator configuration') from exc


def inventory_serial(record):
    """Reject broad discovery, address/composite/custom selectors, even aliases."""
    init = record.get('INIT') or {}
    if (record.get('TYPE') != 'usrp' or type(init) is not dict or 'serial' not in init
            or set(init) - {'serial', 'type', 'profile'}
            or any(type(init[key]) is not str or not re.fullmatch(r'[A-Za-z0-9_]{1,64}', init[key])
                   for key in ('type', 'profile') if key in init)):
        return None
    serial = init['serial']
    return serial if type(serial) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', serial) else None


def scope_id(physical_id, session_id, generation, request_key=''):
    return sha256(json.dumps([physical_id, session_id, generation, request_key]).encode()).hexdigest()


@dataclass
class Configuration:
    registry: Registry = field(default_factory=lambda: Registry([]))
    enabled: bool = False
    devices: dict = field(default_factory=dict)
    cgroup_root: Path | None = None
    errors: tuple = ()
    limits: dict = field(default_factory=dict)
    timeouts: dict = field(default_factory=dict)
    user_code: frozenset = frozenset()

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.exists():
            return cls()
        raw = read_object(path)
        if (set(raw) - {'version', 'enabled', 'manifests', 'devices', 'cgroup_root', 'limits', 'user_code'}
                or type(raw.get('version')) is not int or raw['version'] != 1
                or type(raw.get('enabled', False)) is not bool):
            raise IntegrationError('invalid_configuration', 'invalid integration deployment configuration')
        manifests, errors = [], []
        paths = raw.get('manifests', [])
        if type(paths) is not list or len(paths) > 64:
            raise IntegrationError('invalid_configuration', 'invalid manifest paths')
        for entry in paths:
            if type(entry) is not str or not entry:
                raise IntegrationError('invalid_configuration', 'invalid manifest path')
            try:
                manifests.append(Manifest.from_file(path.parent / entry))
            except (OSError, IntegrationError):
                errors.append('adapter_metadata_unavailable')
        declared = {m.integration_id for m in manifests}
        devices, enabled = {}, {}
        rows = raw.get('devices', {})
        if type(rows) is not dict or len(rows) > 64:
            raise IntegrationError('invalid_configuration', 'invalid device configuration')
        for gid, row in rows.items():
            if (not re.fullmatch(r'[1-9][0-9]{0,17}', gid) or type(row) is not dict
                    or set(row) != {'serial', 'integrations'}):
                raise IntegrationError('invalid_configuration', 'invalid device declaration')
            serial = inventory_serial({'TYPE': 'usrp', 'INIT': {'serial': row['serial']}})
            ids = row['integrations']
            if (serial is None or type(ids) is not list or len(ids) > 64
                    or any(type(i) is not str for i in ids)):
                raise IntegrationError('invalid_configuration', 'invalid device selector')
            devices[int(gid)] = {'serial': serial, 'physical_id': f'usrp:serial:{serial}'}
            enabled[gid] = [i for i in ids if i in declared]
            if any(i not in declared for i in ids):
                errors.append('adapter_metadata_unavailable')
        cgroup = raw.get('cgroup_root')
        if cgroup is not None and (type(cgroup) is not str or not Path(cgroup).is_absolute()):
            raise IntegrationError('invalid_configuration', 'cgroup_root must be an absolute operator path')
        rows = raw.get('limits', {})
        if type(rows) is not dict or len(rows) > 64:
            raise IntegrationError('invalid_configuration', 'invalid resource limits')
        limits, timeouts = {}, {}
        timeout_keys = ('start_timeout_s', 'action_timeout_s', 'stop_timeout_s')
        for integration_id, row in rows.items():
            if (type(row) is not dict or set(row) - {'memory_mib', 'max_pids', *timeout_keys}
                    or any(type(row[key]) is not int for key in row)
                    or not 16 <= row.get('memory_mib', 16) <= 65536
                    or not 1 <= row.get('max_pids', 1) <= 4096
                    or any(not 1 <= row.get(key, 30) <= 600 for key in timeout_keys)):
                raise IntegrationError('invalid_configuration', 'invalid resource limits')
            if integration_id in declared:
                timeouts[integration_id] = {key: row[key] for key in timeout_keys if key in row}
                # Stored as CgroupSupervisor.create keyword overrides.
                limits[integration_id] = entry = {}
                if 'max_pids' in row:
                    entry['max_pids'] = row['max_pids']
                if 'memory_mib' in row:
                    entry['memory_bytes'] = row['memory_mib'] << 20
        # Operator-only: integrations allowed to run caller-supplied code.
        user_code = raw.get('user_code', [])
        if type(user_code) is not list or len(user_code) > 64 or any(type(i) is not str for i in user_code):
            raise IntegrationError('invalid_configuration', 'invalid user_code integrations')
        if any(i not in declared for i in user_code):
            errors.append('adapter_metadata_unavailable')
        return cls(Registry(manifests, enabled=enabled), raw.get('enabled', False), devices,
                   Path(cgroup) if cgroup else None, tuple(errors), limits, timeouts,
                   frozenset(i for i in user_code if i in declared))

    def physical_mapping(self, records, *, unresolved=()):
        if not self.enabled and not unresolved:
            # A disabled optional package must not impose new radio selectors
            # on existing raw UHD inventories. Durable owners still fail closed.
            return {}
        if not self.devices and not unresolved:
            return {}
        configured = {row['serial'] for row in self.devices.values()}
        mapping = {}
        for gid, row in records.items():
            serial = inventory_serial(row)
            if row.get('TYPE') in {'usrp', 'usrp_phaser'} and serial is None:
                # A broad/composite selector may overlap any configured radio.
                raise IntegrationError('ambiguous_hardware', 'integration inventory has an ambiguous radio selector')
            if serial in configured:
                mapping[int(gid)] = f'usrp:serial:{serial}'
        for gid, row in self.devices.items():
            if mapping.get(gid) != row['physical_id']:
                raise IntegrationError('inventory_changed', 'configured integration radio does not match inventory')
        if set(unresolved) - set(mapping.values()):
            raise IntegrationError('recovery_required', 'unresolved owners are missing from physical inventory')
        return mapping

    def require_enabled(self, gid):
        if not self.enabled:
            raise IntegrationError('integration_disabled', 'application integrations are disabled')
        if int(gid) not in self.devices:
            raise IntegrationError('unsupported_device', 'this physical radio is not configured for integrations')
