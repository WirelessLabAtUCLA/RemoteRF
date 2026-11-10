"""Opt-in launcher for one isolated dev clone. Never a production entry point."""
import json
import os
from pathlib import Path
import pwd
import socket
import sys
import tempfile

CANARY_ROOT = Path('/opt/remoterf-integration-canary-20261109')
EXPECTED_SERIALS = {401: 'DEV_INTEGRATION_01', 402: 'DEV_RAW_02'}


def guard():
    """Validate before importing any native driver or creating any test state."""
    if socket.gethostname() != 'remoterf-global-dev' or os.environ.get('REMOTERF_DEV_INTEGRATION_FIXTURE') != '1':
        raise RuntimeError('dev fixture requires the exact dev host and explicit fixture flag')
    if pwd.getpwuid(os.geteuid()).pw_name != 'testlab':
        raise RuntimeError('dev fixture must run as the isolated testlab service user')
    root = CANARY_ROOT
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError('dev clone root is missing or a symlink')
    root = root.resolve(strict=True)
    xdg = Path(os.environ.get('XDG_CONFIG_HOME', '')).resolve(strict=True)
    config = Path(os.environ.get('REMOTERF_CONFIG_DIR', '')).resolve(strict=True)
    if not xdg.is_relative_to(root) or config != xdg / 'remoterf':
        raise RuntimeError('both configuration variables must target the isolated dev clone')
    if os.environ.get('REMOTERF_DEV_INTEGRATION_ENABLE', '0') not in {'0', '1'}:
        raise RuntimeError('invalid explicit fixture enablement')
    if os.environ.get('REMOTERF_DEV_INTEGRATION_BROKEN', '0') not in {'0', '1'}:
        raise RuntimeError('invalid broken-package fixture flag')
    if os.environ.get('REMOTERF_DEV_INTEGRATION_ENABLE') == '1' and os.environ.get('REMOTERF_DEV_INTEGRATION_BROKEN') == '1':
        raise RuntimeError('broken-package fixture must remain disabled')
    path = config / 'devices.yml'
    if not path.resolve(strict=True).is_relative_to(root) or path.stat().st_size > 65536:
        raise RuntimeError('invalid isolated inventory file')
    import yaml
    data = yaml.safe_load(path.read_text())
    if type(data) is not dict or set(data) != {'devices'} or type(data['devices']) is not list or len(data['devices']) > 16:
        raise RuntimeError('unexpected dev fixture inventory')
    seen, usrp = set(), {}
    for row in data['devices']:
        if type(row) is not dict or type(row.get('device_id')) is not int or row['device_id'] in seen:
            raise RuntimeError('invalid or duplicate dev fixture device')
        seen.add(row['device_id'])
        kind = row.get('device_type')
        if kind == 'sim_sdr':
            if row['device_id'] not in {95, 96, 97, 98, 99} or row.get('init') not in ({}, None):
                raise RuntimeError('unexpected existing simulator declaration')
        elif kind == 'usrp':
            init = row.get('init')
            if (type(init) is not dict or set(init) - {'serial', 'profile', 'type'}
                    or init.get('serial') != EXPECTED_SERIALS.get(row['device_id'])
                    or init.get('profile') != 'usrp2901' or init.get('type', 'b200') != 'b200'):
                raise RuntimeError('physical or ambiguous radio forbidden in dev fixture')
            usrp[row['device_id']] = init['serial']
        else:
            raise RuntimeError('non-synthetic device forbidden in dev fixture')
    if usrp != EXPECTED_SERIALS:
        raise RuntimeError('both expected synthetic radios are required')
    return root, config


def prepare_cgroups():
    """Use systemd DelegateSubgroup=manager; no parent/system cgroup changes."""
    guard()
    rows = [line.split(':', 2) for line in Path('/proc/self/cgroup').read_text().splitlines()]
    locations = [parts[2] for parts in rows if parts[:2] == ['0', '']]
    if len(locations) != 1 or not locations[0].startswith('/'):
        raise RuntimeError('cgroup v2 delegation is required')
    manager = Path('/sys/fs/cgroup') / locations[0].lstrip('/')
    parent = manager.parent
    if (manager.name != 'manager' or not parent.name.startswith('remoterf-integration-canary-20261109')
            or not parent.name.endswith('.service') or parent.is_symlink()):
        raise RuntimeError('expected isolated systemd DelegateSubgroup=manager')
    if (parent / 'cgroup.procs').read_text().strip():
        raise RuntimeError('delegated parent must contain no processes')
    if not {'memory', 'pids'} <= set((parent / 'cgroup.controllers').read_text().split()):
        raise RuntimeError('memory and pids controllers must be delegated')
    (parent / 'cgroup.subtree_control').write_text('+memory +pids')
    applications = parent / 'apps'
    applications.mkdir(exist_ok=True)
    if applications.is_symlink() or (applications / 'cgroup.procs').read_text().strip():
        raise RuntimeError('application cgroup parent is not empty and private')
    (applications / 'cgroup.subtree_control').write_text('+memory +pids')
    return applications


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='fixture-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure_fixture(config, cgroup_root=None):
    """Synthetic receipt is valid only for guarded DEV serial/fake UHD identity."""
    _root, expected_config = guard()
    if Path(config).resolve() != expected_config:
        raise RuntimeError('receipt/config must remain in the guarded dev clone')
    from remoterf_device_core.integrations.deployment import REQUIRED_CHECKS, code_digest
    from remoterf_device_core.integrations.fingerprint import adapter_digest
    from remoterf_device_core.integrations.registry import Manifest
    from remoterf_device_core.drivers.usrp.uhd_manifest import build_usrp_schema_v2
    from remoteRF_server.server import integration_controller
    from .backend import UHD_VERSION
    definition = Manifest.from_file(Path(__file__).with_name('integration.json'))
    enabled = os.environ.get('REMOTERF_DEV_INTEGRATION_ENABLE', '0') == '1'
    broken = os.environ.get('REMOTERF_DEV_INTEGRATION_BROKEN', '0') == '1'
    manifest = {**definition.describe(), 'factory': definition.factory}
    if broken:
        manifest['factory'] = 'rrf_dev_missing_adapter:create_adapter'
    _write(config / 'integrations' / 'dev-fixture-manifest.json', manifest)
    controller_file = Path(integration_controller.__file__)
    framework_hash = code_digest([controller_file, controller_file.with_name('dynamic_v2.py'),
                                  controller_file.with_name('device_manager.py'),
                                  controller_file.with_name('grpc_server.py')])
    receipt = {'qualification_version': 1, 'serial': EXPECTED_SERIALS[401],
        'integration_id': definition.integration_id, 'adapter_version': definition.version,
        'adapter_hash': adapter_digest(definition), 'profile': 'test',
        'schema_hash': build_usrp_schema_v2()['schema_hash'], 'uhd_version': UHD_VERSION,
        'framework_hash': framework_hash, 'run_id': 'NON_RADIO_TEST_FIXTURE_20261109',
        'passed': enabled, 'checks': {name: enabled for name in REQUIRED_CHECKS},
        'evidence_kind': 'NON_RADIO_TEST_FIXTURE', 'physical_radio_qualification': False}
    _write(config / 'integrations' / 'dev-fixture-receipt.json', receipt)
    deployment = {'version': 1, 'enabled': enabled,
        'manifests': ['integrations/dev-fixture-manifest.json'],
        'devices': {'401': {'serial': EXPECTED_SERIALS[401], 'integrations': [definition.integration_id]}},
        'qualifications': ['integrations/dev-fixture-receipt.json'] if enabled else []}
    if enabled:
        if cgroup_root is None:
            raise RuntimeError('enabled fixture requires owned cgroups')
        deployment['cgroup_root'] = str(cgroup_root)
    _write(config / 'integrations.json', deployment)


def main():
    _root, config = guard()
    from .backend import install_backend
    install_backend()
    enabled = os.environ.get('REMOTERF_DEV_INTEGRATION_ENABLE', '0') == '1'
    cgroup_root = prepare_cgroups() if enabled else None
    configure_fixture(config, cgroup_root)
    from remoteRF_server.serverrf_cli import main as server_main
    sys.argv = [sys.argv[0], '--serve', '--headless']
    return server_main()


if __name__ == '__main__':
    main()
