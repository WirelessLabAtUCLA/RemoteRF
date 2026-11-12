"""Operator enablement in integrations.json is the only activation gate."""
from importlib import metadata
import json
import sys
from types import SimpleNamespace

import pytest

from remoterf_device_core.hardware_access import AccessTable
from remoterf_device_core.integrations import IntegrationError, Manifest, Registry
from remoterf_device_core.integrations.deployment import Configuration
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


def configuration(definition, *, enabled=True):
    registry = Registry([definition], enabled={'7': [definition.integration_id]})
    return Configuration(registry=registry, enabled=enabled,
        devices={7: {'serial': 'RADIO_ONE', 'physical_id': 'usrp:serial:RADIO_ONE'}})


def assert_code(code, function):
    with pytest.raises(IntegrationError) as caught:
        function()
    assert caught.value.code == code


def test_configuration_loads_optional_metadata_without_importing_adapter(installed, tmp_path):
    definition, package, distributions = installed
    (tmp_path / 'adapter.json').write_text(json.dumps({**definition.describe(), 'factory': definition.factory}))
    (tmp_path / 'integrations.json').write_text(json.dumps(dict(version=1, enabled=False,
        manifests=['adapter.json', 'missing.json'], devices={'7': {'serial': 'RADIO_ONE',
        'integrations': [definition.integration_id, 'org.missing.adapter']}})))
    config = Configuration.load(tmp_path / 'integrations.json')
    assert not config.enabled and config.registry.describe(definition.integration_id)
    assert set(config.errors) == {'adapter_metadata_unavailable'}
    assert 'qualified_adapter' not in sys.modules


def test_legacy_qualifications_key_is_rejected(tmp_path):
    path = tmp_path / 'integrations.json'
    path.write_text(json.dumps({'version': 1, 'enabled': True, 'qualifications': []}))
    assert_code('invalid_configuration', lambda: Configuration.load(path))


def limits_config(tmp_path, definition, limits, **extra):
    (tmp_path / 'adapter.json').write_text(json.dumps({**definition.describe(), 'factory': definition.factory}))
    (tmp_path / 'integrations.json').write_text(json.dumps(dict(version=1, manifests=['adapter.json'],
                                                                limits=limits, **extra)))
    return Configuration.load(tmp_path / 'integrations.json')


def test_operator_timeouts_stay_out_of_cgroup_limits(installed, tmp_path):
    definition, package, distributions = installed
    iid = definition.integration_id
    config = limits_config(tmp_path, definition, {iid: {'max_pids': 8, 'start_timeout_s': 600, 'action_timeout_s': 1,
                                                        'stop_timeout_s': 90}, 'org.missing.adapter': {'stop_timeout_s': 5}})
    assert config.limits == {iid: {'max_pids': 8}}
    assert config.timeouts == {iid: {'start_timeout_s': 600, 'action_timeout_s': 1, 'stop_timeout_s': 90}}
    assert Configuration().timeouts == {}


@pytest.mark.parametrize('row', [{'start_timeout_s': 0}, {'action_timeout_s': 601}, {'stop_timeout_s': True},
                                 {'start_timeout_s': 30.0}, {'stop_timeout_s': '30'}, {'start_timeout_s': None}])
def test_malformed_operator_timeouts_fail_closed(installed, tmp_path, row):
    definition, package, distributions = installed
    assert_code('invalid_configuration', lambda: limits_config(tmp_path, definition, {definition.integration_id: row}))


def test_user_code_is_operator_listed_and_declared_only(installed, tmp_path):
    definition, package, distributions = installed
    assert Configuration().user_code == frozenset()
    assert limits_config(tmp_path, definition, {}).user_code == frozenset()
    config = limits_config(tmp_path, definition, {}, user_code=[definition.integration_id, 'org.missing.adapter'])
    assert config.user_code == {definition.integration_id}
    assert config.errors == ('adapter_metadata_unavailable',)
    for value in ['org.example.adapter', [1], [True], {'org.example.adapter': True}, ['x'] * 65]:
        assert_code('invalid_configuration', lambda: limits_config(tmp_path, definition, {}, user_code=value))


def test_operator_limits_are_converted_and_undeclared_ids_ignored(installed, tmp_path):
    definition, package, distributions = installed
    config = limits_config(tmp_path, definition, {definition.integration_id: {'memory_mib': 1024, 'max_pids': 256},
                                                  'org.missing.adapter': {'max_pids': 4096}})
    assert config.limits == {definition.integration_id: {'max_pids': 256, 'memory_bytes': 1 << 30}}
    assert config.errors == ()
    assert limits_config(tmp_path, definition, {definition.integration_id: {'memory_mib': 16}}).limits == {
        definition.integration_id: {'memory_bytes': 16 << 20}}
    assert limits_config(tmp_path, definition, {definition.integration_id: {}}).limits == {
        definition.integration_id: {}}
    assert Configuration().limits == {}


@pytest.mark.parametrize('limits', [[], {'org.example.adapter': []}, {'org.example.adapter': {'memory_mib': 15}},
    {'org.example.adapter': {'memory_mib': 65537}}, {'org.example.adapter': {'max_pids': 0}},
    {'org.example.adapter': {'max_pids': 4097}}, {'org.example.adapter': {'max_pids': True}},
    {'org.example.adapter': {'memory_mib': 1024.0}}, {'org.example.adapter': {'artifact_mib': 1}},
    {'org.missing.adapter': {'max_pids': '8'}}, {f'org.example.a{i}': {} for i in range(65)}])
def test_malformed_operator_limits_fail_closed(installed, tmp_path, limits):
    definition, package, distributions = installed
    assert_code('invalid_configuration', lambda: limits_config(tmp_path, definition, limits))


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
    assert not config.enabled and config.errors == ()
    assert config.physical_mapping({7: {'TYPE': 'usrp', 'INIT': {}}}) == {}


def test_operator_enablement_is_the_only_gate(installed):
    definition, *_ = installed
    config = configuration(definition)
    config.require_enabled(7)
    assert_code('unsupported_device', lambda: config.require_enabled(8))
    config.enabled = False
    assert_code('integration_disabled', lambda: config.require_enabled(7))


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
