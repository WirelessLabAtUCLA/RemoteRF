"""Operator configuration and hardware qualification, separate from RPC data.

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
from .fingerprint import adapter_digest

REQUIRED_CHECKS = frozenset({
    'native_rx_before', 'application_rx', 'native_rx_after', 'explicit_stop',
    'startup_failure', 'expired_start', 'expired_running', 'process_crash',
    'cleanup_descendants', 'next_holder_rx',
})


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


def code_digest(extra_paths=()):
    digest = sha256()
    core = Path(__file__).parent.parent
    paths = sorted(Path(__file__).parent.glob('*.py')) + [
        core / 'hardware_access.py', core / 'native_lifetime.py',
        core / 'drivers' / 'usrp' / 'usrp_schema.py',
    ] + [Path(p) for p in extra_paths]
    for path in paths:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@dataclass
class Configuration:
    registry: Registry = field(default_factory=lambda: Registry([]))
    enabled: bool = False
    devices: dict = field(default_factory=dict)
    receipts: tuple = ()
    cgroup_root: Path | None = None
    errors: tuple = ()
    adapter_hashes: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.exists():
            return cls()
        raw = read_object(path)
        if (set(raw) - {'version', 'enabled', 'manifests', 'devices', 'qualifications', 'cgroup_root'}
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
                raise IntegrationError('invalid_configuration', 'invalid qualified device declaration')
            serial = inventory_serial({'TYPE': 'usrp', 'INIT': {'serial': row['serial']}})
            ids = row['integrations']
            if (serial is None or type(ids) is not list or len(ids) > 64
                    or any(type(i) is not str for i in ids)):
                raise IntegrationError('invalid_configuration', 'invalid qualified device selector')
            devices[int(gid)] = {'serial': serial, 'physical_id': f'usrp:serial:{serial}'}
            enabled[gid] = [i for i in ids if i in declared]
            if any(i not in declared for i in ids):
                errors.append('adapter_metadata_unavailable')
        receipts = []
        receipt_paths = raw.get('qualifications', [])
        if type(receipt_paths) is not list or len(receipt_paths) > 64:
            raise IntegrationError('invalid_configuration', 'invalid qualification paths')
        for entry in receipt_paths:
            if type(entry) is not str or not entry:
                raise IntegrationError('invalid_configuration', 'invalid qualification path')
            try:
                receipts.append(read_object(path.parent / entry))
            except (OSError, IntegrationError):
                errors.append('qualification_unavailable')
        cgroup = raw.get('cgroup_root')
        if cgroup is not None and (type(cgroup) is not str or not Path(cgroup).is_absolute()):
            raise IntegrationError('invalid_configuration', 'cgroup_root must be an absolute operator path')
        adapter_hashes = {}
        for definition in manifests:
            try:
                adapter_hashes[definition.integration_id] = adapter_digest(definition)
            except IntegrationError:
                errors.append('adapter_fingerprint_unavailable')
        return cls(Registry(manifests, enabled=enabled), raw.get('enabled', False), devices,
                   tuple(receipts), Path(cgroup) if cgroup else None, tuple(errors), adapter_hashes)

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

    def verify_adapter(self, integration_id):
        expected = self.adapter_hashes.get(integration_id)
        if type(expected) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise IntegrationError('qualification_required', 'installed adapter identity has not been verified')
        try:
            actual = adapter_digest(self.registry._definition(integration_id))
        except IntegrationError:
            raise IntegrationError('qualification_required', 'installed adapter identity could not be verified') from None
        if actual != expected:
            raise IntegrationError('qualification_required', 'installed adapter changed after qualification')

    def require_qualified(self, gid, integration_id, profile, *, schema_hash, uhd_version, framework_hash):
        if not self.enabled:
            raise IntegrationError('integration_disabled', 'application integrations are disabled')
        device = self.devices.get(int(gid))
        if device is None:
            raise IntegrationError('unsupported_device', 'this physical radio is not configured for integrations')
        definition = self.registry.describe(integration_id)
        adapter_hash = self.adapter_hashes.get(integration_id)
        if type(adapter_hash) is not str or not re.fullmatch(r'[0-9a-f]{64}', adapter_hash):
            raise IntegrationError('qualification_required', 'installed adapter identity has not been verified')
        expected = {'adapter_hash': adapter_hash, 'qualification_version': 1, 'serial': device['serial'], 'integration_id': integration_id,
                    'adapter_version': definition['version'], 'profile': profile, 'schema_hash': schema_hash,
                    'uhd_version': uhd_version, 'framework_hash': framework_hash, 'passed': True}
        for receipt in self.receipts:
            if (all(type(receipt.get(k)) is type(v) and receipt.get(k) == v for k, v in expected.items())
                    and type(receipt.get('checks')) is dict
                    and all(receipt['checks'].get(check) is True for check in REQUIRED_CHECKS)
                    and type(receipt.get('run_id')) is str and receipt['run_id']):
                return
        raise IntegrationError('qualification_required', 'real-radio handoff and cleanup qualification is required')
