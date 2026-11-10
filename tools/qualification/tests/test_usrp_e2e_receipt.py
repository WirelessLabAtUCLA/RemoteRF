"""Receipt assembly rejects incomplete or mismatched synthetic test evidence."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / 'tools/qualification/usrp_e2e_receipt.py'
spec = importlib.util.spec_from_file_location('e2e_receipt_under_test', SCRIPT)
h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)


@pytest.fixture
def fixture():
    # Existing retained evidence supplies identities; the new descendant record
    # below is deliberately SYNTHETIC and exists only inside these unit tests.
    handoff = json.loads((ROOT / 'docs/qualification/phase1c-ucla-exact-handoff-2026-11-09.json').read_text())
    service = json.loads((ROOT / 'docs/qualification/phase1c-ucla-exact-service-stop-2026-11-09.json').read_text())
    source = deepcopy(service['authoritative_run']['source_identity'])
    old = handoff['authoritative_run']
    observation = {'scope_identity_verified': True, 'kernel_populated': 1, 'process_count': 2,
                   'usb_owners_inside_scope': True, 'usb_owner_count': 1}
    record = {'format': 'remoterf-receiver-descendant-qualification-1', 'passed': True, 'transmit': False,
        'phase': 'complete', 'serial': old['serial'], 'device_id': old['device_id'], 'finished_at': 100,
        'fault_injection': 'required_wrapper_parent_exits_real_receiver_descendant_keeps_usb',
        'normal_auth': {'resolver_unmodified': True, 'token_matches': True,
            'match_function_agrees': True, 'four_second_authority_cache_used': False}, 'source_identity': source,
        'uhd_version': old['checks']['application_rx']['uhd_version'], 'schema_hash': 'sha256:' + '1' * 64,
        'adapter_id': 'org.remoterf.usrp_probe', 'adapter_version': '0.1.0',
        'private_calendar_empty': True, 'guarded_root': {'root': '/test/private/cgroup', 'identity':[1,2]},
        'checks': {'native_rx_before': {'received':4096}, 'application_rx': {'received':4096},
                  'native_rx_after': {'received':4096}, 'before_parent_death': deepcopy(observation),
                  'receiver_after_parent_death': deepcopy(observation),
                  'native_open_refused_while_descendant_alive': True,
                  'cleanup_descendants': {'passed':True, 'automatic_required_parent_failure':True,
                    'verified_cgroup_cleanup':True, 'journal_empty':True, 'health_action_used':False,
                    'outcome':{'error':'process_exited','released':True}}}}
    cleanup = {'format':'remoterf-receiver-descendant-independent-cleanup-1','passed':True,
        'serial': record['serial'], 'device_id':record['device_id'],'evidence_sha256':'synthetic-test-digest',
        'hardware_records':0,'process_records':0,'owned_cgroups':0,'usb_owner_count':0,
        'root_identity_verified':True,'production_unchanged':True,'observed_at':101,
        'guarded_root':deepcopy(record['guarded_root'])}
    return handoff, service, record, cleanup


def assemble(fixture):
    return h.assemble(*fixture, evidence_sha256='synthetic-test-digest', run_id='unit-test-only')


def test_all_required_hardware_assertions_must_be_supplied(fixture):
    from remoterf_device_core.integrations.deployment import REQUIRED_CHECKS
    receipt = assemble(fixture)
    assert set(receipt['checks']) == REQUIRED_CHECKS
    assert all(v is True for v in receipt['checks'].values())
    assert receipt['dev_canary_only'] is True and receipt['production_approved'] is False
    assert 'not established' in receipt['evidence_scope']['public_generic_api_and_global_calendar']


@pytest.mark.parametrize('mutation', [
    lambda x: x[2]['checks']['receiver_after_parent_death'].update(usb_owners_inside_scope=False),
    lambda x: x[2]['checks']['receiver_after_parent_death'].update(process_count=0),
    lambda x: x[2]['checks']['receiver_after_parent_death'].update(kernel_populated=True),
    lambda x: x[2]['checks']['receiver_after_parent_death'].update(usb_owner_count=True),
    lambda x: x[2]['checks']['cleanup_descendants'].update(health_action_used=True),
    lambda x: x[2]['checks']['cleanup_descendants'].update(journal_empty=False),
    lambda x: x[2]['source_identity'].update(framework_hash='2'*64),
    lambda x: x[2].update(schema_hash='wrong-schema'),
    lambda x: x[2].update(private_calendar_empty=False),
    lambda x: x[3].update(evidence_sha256='another-run'),
    lambda x: x[3].update(observed_at=99),
    lambda x: x[3].update(observed_at=float('inf')),
    lambda x: x[3].update(observed_at=True),
    lambda x: x[2].update(finished_at=float('nan')),
    lambda x: x[2]['normal_auth'].update(token_matches=False),
    lambda x: x[2]['normal_auth'].update(four_second_authority_cache_used=True),
    lambda x: x[0]['independent_cleanup'].update(usb_owner_count=False),
    lambda x: x[0]['independent_cleanup'].update(remaining_owned_scopes=False),
    lambda x: x[3].update(hardware_records=1),
    lambda x: x[3].update(usb_owner_count=False),
    lambda x: x[3]['guarded_root'].update(identity=[1,3]),
    lambda x: x[0]['authoritative_run']['checks']['expired_start'].update(cleanup_serialized=False),
])
def test_missing_or_mismatched_evidence_cannot_become_an_approval(fixture, mutation):
    mutation(fixture)
    with pytest.raises(ValueError): assemble(fixture)
