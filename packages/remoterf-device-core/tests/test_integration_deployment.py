"""Operator configuration never turns metadata into hardware qualification."""
from dataclasses import replace
from importlib import metadata
import json
import sys
from types import SimpleNamespace

import pytest

from remoterf_device_core.hardware_access import AccessTable
from remoterf_device_core.integrations import IntegrationError, Manifest, Registry
from remoterf_device_core.integrations import fingerprint
from remoterf_device_core.integrations.deployment import Configuration, REQUIRED_CHECKS
from remoterf_device_core.integrations.ownership import Journal


@pytest.fixture
def installed(tmp_path, monkeypatch):
    package = tmp_path / 'qualified_adapter'
    package.mkdir()
    (package / '__init__.py').write_text("raise RuntimeError('metadata lookup must never import this package')\n")
    (package / 'factory.py').write_text('def create(context): return None\n')
    (package / 'native.so').write_bytes(b'native-image')
    (package / 'profile.json').write_text('{"rx":true}')
    monkeypatch.syspath_prepend(str(tmp_path))
    distributions = {'qualified-adapter': SimpleNamespace(version='1.2.3', requires=['native-dependency>=2']),
                     'native-dependency': SimpleNamespace(version='2.1', requires=[])}
    monkeypatch.setattr(metadata, 'packages_distributions', lambda: {'qualified_adapter': ['qualified-adapter']})
    def distribution(name):
        try:
            return distributions[name]
        except KeyError:
            raise metadata.PackageNotFoundError(name)
    monkeypatch.setattr(metadata, 'distribution', distribution)
    definition = Manifest.from_dict(dict(id='org.example.adapter', version='1.2.3', contract_version='0.1',
        factory='qualified_adapter.factory:create', device_types=['usrp'], profiles=['rx'], actions=['stats']))
    return definition, package, distributions


def configuration(definition, digest='a' * 64, *, enabled=True):
    registry = Registry([definition], enabled={'7': [definition.integration_id]})
    return Configuration(registry=registry, enabled=enabled,
        devices={7: {'serial': 'RADIO_ONE', 'physical_id': 'usrp:serial:RADIO_ONE'}},
        adapter_hashes={definition.integration_id: digest})


def receipt(definition, **changes):
    value = dict(qualification_version=1, serial='RADIO_ONE', integration_id=definition.integration_id,
        adapter_version=definition.version, adapter_hash='a' * 64, profile='rx', schema_hash='schema',
        uhd_version='4.8', framework_hash='framework', passed=True,
        checks={check: True for check in REQUIRED_CHECKS}, run_id='physical-radio-run-1')
    value.update(changes)
    return value


def admit(config, definition):
    config.require_qualified(7, definition.integration_id, 'rx', schema_hash='schema',
                             uhd_version='4.8', framework_hash='framework')


def assert_code(code, function):
    with pytest.raises(IntegrationError) as caught:
        function()
    assert caught.value.code == code


def test_fingerprint_never_imports_parent_or_factory_and_is_stable(installed):
    definition, package, distributions = installed
    first = fingerprint.adapter_digest(definition)
    assert len(first) == 64
    assert fingerprint.adapter_digest(definition) == first
    assert 'qualified_adapter' not in sys.modules
    assert 'qualified_adapter.factory' not in sys.modules
    (package / '__pycache__').mkdir()
    (package / '__pycache__' / 'factory.cpython.pyc').write_bytes(b'cache')
    assert fingerprint.adapter_digest(definition) == first


@pytest.mark.parametrize('change', ['factory', 'source', 'native', 'json', 'dependency', 'optional_dependency'])
def test_fingerprint_binds_factory_package_and_dependency_versions(installed, change):
    definition, package, distributions = installed
    before = fingerprint.adapter_digest(definition)
    if change == 'factory':
        definition = replace(definition, factory='qualified_adapter.factory:other')
    elif change == 'source':
        (package / 'factory.py').write_text('def create(context): return 1\n')
    elif change == 'native':
        (package / 'native.so').write_bytes(b'changed-native-image')
    elif change == 'json':
        (package / 'profile.json').write_text('{"rx":false}')
    elif change == 'dependency':
        distributions['native-dependency'].version = '2.2'
    else:
        distributions['qualified-adapter'].requires.append('optional-plugin; extra == "optional"')
    assert fingerprint.adapter_digest(definition) != before


def test_missing_distribution_or_factory_fails_closed(installed, monkeypatch):
    definition, package, distributions = installed
    del distributions['qualified-adapter']
    assert_code('adapter_fingerprint_unavailable', lambda: fingerprint.adapter_digest(definition))
    assert_code('adapter_fingerprint_unavailable', lambda: fingerprint.adapter_digest(
        replace(definition, factory='not_installed.factory:create')))


@pytest.mark.parametrize('kind', ['file', 'directory'])
def test_fingerprint_rejects_symlinks_even_inside_package(installed, tmp_path, kind):
    definition, package, distributions = installed
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'config.json').write_text('{}')
    (package / 'linked').symlink_to(outside if kind == 'directory' else outside / 'config.json')
    assert_code('adapter_fingerprint_unavailable', lambda: fingerprint.adapter_digest(definition))


@pytest.mark.parametrize('limit,value', [('MAX_FILES', 1), ('MAX_FILE_BYTES', 2), ('MAX_TOTAL_BYTES', 3), ('MAX_DISTRIBUTIONS', 1), ('MAX_DEPTH', 0)])
def test_fingerprint_work_is_bounded(installed, monkeypatch, limit, value):
    definition, package, distributions = installed
    (package / 'nested').mkdir()
    monkeypatch.setattr(fingerprint, limit, value)
    assert_code('adapter_fingerprint_unavailable', lambda: fingerprint.adapter_digest(definition))


def test_configuration_loads_optional_metadata_without_importing_adapter(installed, tmp_path):
    definition, package, distributions = installed
    (tmp_path / 'adapter.json').write_text(json.dumps({**definition.describe(), 'factory': definition.factory}))
    (tmp_path / 'integrations.json').write_text(json.dumps(dict(version=1, enabled=False,
        manifests=['adapter.json', 'missing.json'], devices={'7': {'serial': 'RADIO_ONE',
        'integrations': [definition.integration_id, 'org.missing.adapter']}}, qualifications=['missing-receipt.json'])))
    config = Configuration.load(tmp_path / 'integrations.json')
    assert not config.enabled and config.registry.describe(definition.integration_id)
    assert config.adapter_hashes[definition.integration_id] == fingerprint.adapter_digest(definition)
    assert set(config.errors) == {'adapter_metadata_unavailable', 'qualification_unavailable'}
    assert 'qualified_adapter' not in sys.modules


@pytest.mark.parametrize('version', [True, False, 1.0, '1', None, 2])
def test_configuration_version_requires_exact_integer(tmp_path, version):
    path = tmp_path / 'integrations.json'
    path.write_text(json.dumps({'version': version}))
    assert_code('invalid_configuration', lambda: Configuration.load(path))


def test_missing_or_broken_optional_packages_leave_disabled_raw_configuration(installed, tmp_path):
    definition, package, distributions = installed
    assert not Configuration.load(tmp_path / 'missing.json').enabled
    (tmp_path / 'adapter.json').write_text(json.dumps({**definition.describe(), 'factory': 'missing_package:create'}))
    (tmp_path / 'integrations.json').write_text(json.dumps({'version': 1, 'manifests': ['adapter.json']}))
    config = Configuration.load(tmp_path / 'integrations.json')
    assert not config.enabled and config.adapter_hashes == {}
    assert config.errors == ('adapter_fingerprint_unavailable',)
    assert config.physical_mapping({7: {'TYPE': 'usrp', 'INIT': {}}}) == {}


def test_all_receipt_fields_and_checks_required(installed):
    definition, *_ = installed
    config = configuration(definition)
    valid = receipt(definition)
    config.receipts = (valid,)
    admit(config, definition)
    for field in valid:
        config.receipts = ({k: v for k, v in valid.items() if k != field},)
        assert_code('qualification_required', lambda: admit(config, definition))
    for check in REQUIRED_CHECKS:
        for incorrect in [False, 1, 'true', None]:
            config.receipts = (receipt(definition, checks={**valid['checks'], check: incorrect}),)
            assert_code('qualification_required', lambda: admit(config, definition))


@pytest.mark.parametrize('field,value', [('qualification_version', True), ('qualification_version', 1.0),
    ('serial', 'OTHER'), ('integration_id', 'org.other.adapter'), ('adapter_version', '1.2.4'),
    ('adapter_hash', 'b' * 64), ('profile', 'tx'), ('schema_hash', 'old-schema'), ('uhd_version', '4.9'),
    ('framework_hash', 'old-code'), ('passed', 1), ('passed', 'true'), ('run_id', '')])
def test_receipts_cannot_be_reused_for_different_identity(installed, field, value):
    definition, *_ = installed
    config = configuration(definition)
    config.receipts = (receipt(definition, **{field: value}),)
    assert_code('qualification_required', lambda: admit(config, definition))


def test_disabled_and_unknown_device_cannot_be_enabled_by_receipt(installed):
    definition, *_ = installed
    config = configuration(definition, enabled=False)
    config.receipts = (receipt(definition),)
    assert_code('integration_disabled', lambda: admit(config, definition))
    config.enabled = True
    config.devices.clear()
    assert_code('unsupported_device', lambda: admit(config, definition))


def test_adapter_identity_must_exist_and_match_on_worker_recheck(installed):
    definition, package, distributions = installed
    config = configuration(definition, fingerprint.adapter_digest(definition))
    config.verify_adapter(definition.integration_id)
    (package / 'factory.py').write_text('def create(context): return "modified"\n')
    assert_code('qualification_required', lambda: config.verify_adapter(definition.integration_id))
    config.adapter_hashes.clear()
    assert_code('qualification_required', lambda: config.verify_adapter(definition.integration_id))
    config.receipts = (receipt(definition),)
    assert_code('qualification_required', lambda: admit(config, definition))


def test_inventory_maps_every_serial_alias_to_canonical_identity(installed, tmp_path):
    definition, *_ = installed
    config = configuration(definition)
    records = {7: {'TYPE': 'usrp', 'INIT': {'serial': 'RADIO_ONE'}},
               8: {'TYPE': 'usrp', 'INIT': {'serial': 'RADIO_ONE', 'type': 'b200'}},
               9: {'TYPE': 'usrp', 'INIT': {'serial': 'OTHER'}}}
    mapping = config.physical_mapping(records)
    assert mapping == {7: 'usrp:serial:RADIO_ONE', 8: 'usrp:serial:RADIO_ONE'}
    journal = Journal(tmp_path / 'journal')
    try:
        access = AccessTable()
        access.configure(mapping, journal)
        assert access.gate(7) is access.gate(8)
        assert set(access.aliases(access.gate(7))) == {7, 8}
        with access.native(8):
            assert_code('device_busy', lambda: access.gate(7).reserve(session='s', generation=1, integration='org.example.adapter'))
    finally:
        journal.close()


@pytest.mark.parametrize('record', [{'TYPE': 'usrp', 'INIT': {}},
    {'TYPE': 'usrp', 'INIT': {'addr': '10.0.0.1'}}, {'TYPE': 'usrp', 'INIT': {'serial': 'RADIO_ONE,OTHER'}},
    {'TYPE': 'usrp', 'INIT': {'serial': 'OTHER', 'addr': '10.0.0.1'}},
    {'TYPE': 'usrp_phaser', 'INIT': {'serial': 'OTHER'}}])
def test_ambiguous_inventory_rejected_only_when_activation_or_recovery_requires_it(installed, record):
    definition, *_ = installed
    config = configuration(definition)
    records = {7: {'TYPE': 'usrp', 'INIT': {'serial': 'RADIO_ONE'}}, 8: record}
    assert_code('ambiguous_hardware', lambda: config.physical_mapping(records))
    config.enabled = False
    assert config.physical_mapping(records) == {}
    assert_code('ambiguous_hardware', lambda: config.physical_mapping(records, unresolved=['usrp:serial:RADIO_ONE']))


def test_removed_or_changed_inventory_cannot_erase_unresolved_owner(installed):
    definition, *_ = installed
    config = configuration(definition)
    assert_code('inventory_changed', lambda: config.physical_mapping({7: {'TYPE': 'usrp', 'INIT': {'serial': 'OTHER'}}}))
    config.devices.clear()
    assert_code('recovery_required', lambda: config.physical_mapping({}, unresolved=['usrp:serial:RADIO_ONE']))
    config.enabled = False
    assert_code('recovery_required', lambda: config.physical_mapping({}, unresolved=['usrp:serial:RADIO_ONE']))


def test_active_native_mapping_cannot_be_reconfigured(tmp_path):
    access = AccessTable()
    journal = Journal(tmp_path / 'journal')
    try:
        with access.native(7):
            assert_code('device_busy', lambda: access.configure({7: 'usrp:serial:RADIO_ONE'}, journal))
    finally:
        journal.close()


@pytest.mark.parametrize('field,value', [('id', 'org.' + 'x' * 125), ('version', '1.0.0-' + 'x' * 123),
    ('factory', 'x' * 507 + ':create'), ('contract_version', '1' * 15 + '.1'),
    ('device_types', ['x' * 129]), ('profiles', ['x' * 129]), ('actions', ['x' * 129])])
def test_manifest_identifiers_fit_discovery_cursors_and_requests(installed, field, value):
    definition, *_ = installed
    raw = {**definition.describe(), 'factory': definition.factory, field: value}
    assert_code('invalid_manifest', lambda: Manifest.from_dict(raw))


def test_nested_namespace_package_lookup_does_not_import_parents(tmp_path, monkeypatch):
    definition = Manifest.from_dict(dict(id='org.example.namespace', version='1.0.0', contract_version='0.1',
        factory='qualified_namespace.nested.factory:create', device_types=['usrp'], profiles=['rx'], actions=[]))
    package = tmp_path / 'qualified_namespace' / 'nested'
    package.mkdir(parents=True)
    (package / 'factory.py').write_text("raise RuntimeError('must not import')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(metadata, 'packages_distributions', lambda: {'qualified_namespace': ['namespace-dist']})
    monkeypatch.setattr(metadata, 'distribution', lambda name: SimpleNamespace(version='1.0', requires=[]))
    assert len(fingerprint.adapter_digest(definition)) == 64
    assert not any(name.startswith('qualified_namespace') for name in sys.modules)
