#!/usr/bin/env python3
"""Assemble a dev-canary-only operator receipt from retained exact-build evidence.

This does not enable a deployment or grant production approval. Every boolean
is derived from named hardware observations, including independent post-run
cleanup bound by digest to the new receiver-descendant evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from pathlib import Path
import stat


def require(value, message):
    if not value: raise ValueError(message)


def passed_run(value):
    require(value.get('passed') is True, 'a hardware evidence run did not pass')
    require(value.get('transmit') is False, 'only receive-only evidence is accepted')
    require(not value.get('cleanup_errors') and not value.get('cleanup_failures'), 'cleanup error is retained')


def rx(value):
    return type(value) is dict and type(value.get('received')) is int and value['received'] >= 4096


def normalized_hashes(identity):
    rows = identity['module_sha256']
    result = {}
    for path, digest in rows.items():
        matches = [prefix + path.split(prefix, 1)[1] for prefix in
                   ('remoterf_device_core/', 'remoteRF_server/', 'rrf_example_usrp_probe/') if prefix in path]
        require(len(matches) == 1 and matches[0] not in result, 'ambiguous recorded package path')
        result[matches[0]] = digest
    return result


def assemble(handoff_bundle, service_bundle, descendants, cleanup, *, evidence_sha256, run_id):
    old, service = handoff_bundle['authoritative_run'], service_bundle['authoritative_run']
    for record in (old, service, descendants): passed_run(record)
    require(old.get('stage') == service.get('stage') == 'complete', 'prior qualification was incomplete')
    require(descendants.get('phase') == 'complete', 'descendant qualification was incomplete')
    require(descendants.get('format') == 'remoterf-receiver-descendant-qualification-1', 'wrong descendant evidence format')
    require(descendants.get('fault_injection') == 'required_wrapper_parent_exits_real_receiver_descendant_keeps_usb', 'wrong descendant topology')
    auth = descendants.get('normal_auth', {})
    require(all(auth.get(k) is True for k in ('resolver_unmodified', 'token_matches', 'match_function_agrees'))
            and auth.get('four_second_authority_cache_used') is False, 'descendant test did not prove normal auth')
    serial, gid = descendants['serial'], descendants['device_id']
    require(all(v.get('serial') == serial and v.get('device_id') == gid for v in (old, service)), 'radio identity differs between runs')
    source, previous_source = descendants['source_identity'], service['source_identity']
    require(all(source[key] == previous_source[key] for key in ('framework_hash', 'adapter_hash', 'versions')), 'framework or adapter differs from prior hardware qualification')
    require(normalized_hashes(source) == normalized_hashes(previous_source), 'recorded package source differs')
    require(old['versions'] == source['versions'], 'handoff package versions differ')
    for name, digest in old['source_sha256'].items():
        if name == 'qualification_harness':
            require(source['helper_sha256'] == digest, 'historical handoff harness differs')
            continue
        package_path = ('remoterf_device_core/integrations/_process_worker.py' if name == 'supervised_process_bootstrap'
            else name.replace('.', '/') + (('/__init__.py') if name == 'rrf_example_usrp_probe' else '.py'))
        require(normalized_hashes(source).get(package_path) == digest, 'handoff implementation hash differs')
    require(all(type(source[k]) is str and re.fullmatch(r'[0-9a-f]{64}', source[k])
                for k in ('framework_hash', 'adapter_hash')), 'invalid framework/adapter digest')
    require(type(descendants['schema_hash']) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', descendants['schema_hash']),
            'invalid native schema digest')
    require(descendants['uhd_version'] == old['checks']['application_rx']['uhd_version'], 'native UHD version differs')
    previous_cleanup = handoff_bundle['independent_cleanup']
    require(previous_cleanup.get('passed') is True and previous_cleanup.get('root_identity_verified') is True,
            'prior hardware cleanup lacks independent proof')
    require(all(type(previous_cleanup.get(k)) is int and previous_cleanup[k] == 0
                for k in ('remaining_owned_scopes', 'usb_owner_count')),
            'prior hardware cleanup was not empty')
    require(type(previous_cleanup.get('empty_ledgers')) is dict and len(previous_cleanup['empty_ledgers']) == 4
            and all(type(v) is int and v == 0 for v in previous_cleanup['empty_ledgers'].values()),
            'prior ownership journals were not all empty')
    require(cleanup.get('format') == 'remoterf-receiver-descendant-independent-cleanup-1', 'wrong independent cleanup format')
    require(cleanup.get('passed') is True and cleanup.get('root_identity_verified') is True
            and cleanup.get('production_unchanged') is True, 'independent cleanup did not pass')
    require(cleanup.get('serial') == serial and cleanup.get('device_id') == gid
            and cleanup.get('evidence_sha256') == evidence_sha256, 'independent cleanup belongs to another run')
    for key in ('hardware_records', 'process_records', 'owned_cgroups', 'usb_owner_count'):
        require(type(cleanup.get(key)) is int and cleanup[key] == 0, 'independent cleanup retains resources')
    require(all(type(v) in (int, float) and math.isfinite(v) and v > 0
                for v in (cleanup.get('observed_at'), descendants.get('finished_at')))
            and cleanup['observed_at'] >= descendants['finished_at'], 'independent cleanup predates the result or has an invalid time')
    require(cleanup.get('guarded_root') == {k: descendants['guarded_root'][k] for k in ('root', 'identity')},
            'independent cleanup root identity differs')
    checks = old['checks']; current = descendants['checks']
    require(rx(checks.get('native_rx_before')) and rx(checks.get('application_rx')) and rx(checks.get('native_rx_after')),
            'prior receive handoff proof is missing')
    stop = checks.get('explicit_stop', {})
    require(stop.get('state') == 'IDLE' and stop.get('error') is None and stop.get('released') is True, 'explicit stop did not release')
    failure = checks.get('partial_start_cleanup', {})
    require(failure.get('error') == 'starting_failed' and failure.get('released') is True
            and rx(checks.get('rx_after_partial_start')), 'startup failure recovery did not pass')
    expired = checks.get('expired_start', {})
    require(expired.get('ownership_retained_until_hook_return') is True and expired.get('cleanup_serialized') is True
            and rx(expired.get('rx_before_local_revocation')) and expired.get('outcome', {}).get('released') is True
            and expired.get('outcome', {}).get('error') == 'cancelled' and rx(checks.get('rx_after_expired_start')),
            'expired startup proof is missing')
    require(checks.get('injected_local_expiry_cleanup', {}).get('released') is True
            and rx(checks.get('next_local_holder_rx')), 'expired running/next-holder proof is missing')
    before, after = current.get('before_parent_death', {}), current.get('receiver_after_parent_death', {})
    for observation, minimum in ((before, 2), (after, 1)):
        require(observation.get('scope_identity_verified') is True and type(observation.get('kernel_populated')) is int and observation['kernel_populated'] == 1
                and type(observation.get('process_count')) is int and observation['process_count'] >= minimum
                and observation.get('usb_owners_inside_scope') is True and type(observation.get('usb_owner_count')) is int and observation['usb_owner_count'] >= 1,
                'real receiving descendant survival was not positively observed')
    released = current.get('cleanup_descendants', {})
    require(all(released.get(k) is True for k in ('passed', 'automatic_required_parent_failure', 'verified_cgroup_cleanup', 'journal_empty'))
            and released.get('health_action_used') is False and released.get('outcome', {}).get('error') == 'process_exited'
            and released.get('outcome', {}).get('released') is True,
            'autonomous parent-failure descendant cleanup did not pass')
    require(current.get('native_open_refused_while_descendant_alive') is True
            and rx(current.get('native_rx_before')) and rx(current.get('application_rx')) and rx(current.get('native_rx_after')),
            'fresh descendant fence or native reuse proof is missing')
    require(descendants.get('private_calendar_empty') is True, 'private reservation remains')
    require(type(run_id) is str and 1 <= len(run_id) <= 128, 'bounded operator run id is required')
    names = ('native_rx_before', 'application_rx', 'native_rx_after', 'explicit_stop', 'startup_failure',
             'expired_start', 'expired_running', 'process_crash', 'cleanup_descendants', 'next_holder_rx')
    return {'qualification_version': 1, 'serial': serial, 'integration_id': descendants['adapter_id'],
        'adapter_version': descendants['adapter_version'], 'adapter_hash': source['adapter_hash'],
        'profile': 'receive', 'schema_hash': descendants['schema_hash'], 'uhd_version': descendants['uhd_version'],
        'framework_hash': source['framework_hash'], 'run_id': run_id, 'passed': True,
        'checks': dict.fromkeys(names, True), 'dev_canary_only': True, 'production_approved': False,
        'evidence_scope': {'historical_expiry': 'local injected revocation while external booking held',
            'descendants': 'qualification wrapper parent dies; unchanged real receiver child survives until verified cleanup',
            'public_generic_api_and_global_calendar': 'not established by this receipt; the isolated dev canary tests them next'}}


def read(path):
    data = path.read_bytes()
    require(len(data) <= 1024 * 1024, 'evidence file exceeds one MiB')
    return json.loads(data), hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('handoff', 'service', 'descendants', 'cleanup', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--dev-canary-only', action='store_true', required=True)
    args = parser.parse_args()
    require(all(getattr(args, k).is_absolute() for k in ('handoff', 'service', 'descendants', 'cleanup', 'output')), 'absolute operator paths required')
    values, digests = {}, {}
    for key in ('handoff', 'service', 'descendants', 'cleanup'):
        values[key], digests[key] = read(getattr(args, key))
    receipt = assemble(values['handoff'], values['service'], values['descendants'], values['cleanup'],
                       evidence_sha256=digests['descendants'], run_id=args.run_id)
    receipt['evidence_sha256'] = digests
    parent = args.output.parent
    info = parent.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
            'receipt destination must be an existing private operator directory')
    data = (json.dumps(receipt, indent=2) + '\n').encode()
    require(len(data) <= 65536, 'receipt exceeds operator configuration budget')
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush(); os.fsync(stream.fileno())
    print(json.dumps({'created': str(args.output), 'sha256': hashlib.sha256(data).hexdigest(),
        'production_approved': False, 'activation_changed': False}))


if __name__ == '__main__':
    main()
