"""Service supplement orchestration with explicit fake radio/kernel resources."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

HARNESS = Path(__file__).resolve().parents[1] / 'usrp_service_stop.py'
spec = importlib.util.spec_from_file_location('service_stop_qualification_test', HARNESS)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


FAKE_CHILD = r'''
import importlib.util,json,os,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
script,state,mode=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
state.mkdir()
spec=importlib.util.spec_from_file_location('service_stop_under_test',script)
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
h.private_environment(state)
root=script.parents[2]
sys.path.insert(0,str(root/'server/tests'))
sys.path.insert(0,str(root/'examples/integrations/usrp_probe/src'))
from test_native_lifetime import Inventory
from remoterf_device_core.hardware_access import AccessTable
from remoterf_device_core.integrations import supervision
from remoteRF_server.server import dynamic_v2 as v2
sample=root/'examples/integrations/usrp_probe/src'
h.distribution=lambda name:SimpleNamespace(locate_file=lambda path:sample/path)
class Scope:
 def __init__(self,supervisor,owner):self.supervisor,self.owner,self.alive=supervisor,owner,False;self.launched=False
 def launch(self,*a,**k):
  assert k['required_while_running'] is True
  self.alive=True;self.launched=True;return 'fake-child'
 def resource_failure(self):return 'process_exited' if self.launched and not self.alive else None
 def snapshot(self):return {'processes':[{'id':'fake-child','returncode':None if self.alive else 1}]}
 def log_tail(self,*a):return b'RRF_USRP_PROBE {"event":"ready","serial":"FAKE123","received":4096,"rate":1000000,"frequency":100000000,"gain":0,"channel":0,"uhd_version":"fake"}\n'
 def stop(self,*a,**k):self.alive=False;return True
 def resources_released(self):return not self.alive
 def close(self):
  assert not self.alive
  self.supervisor.scopes.pop(self.owner);self.supervisor.write()
class Supervisor:
 def __init__(self,root,path):
  self.scopes={};self.path=path;path.mkdir();self.write()
 def write(self):
  (self.path/'scopes.json').write_text(json.dumps({'scopes':{k:{'state':'ready'} for k in self.scopes}}))
 def create(self,owner):
  scope=Scope(self,owner);self.scopes[owner]=scope;self.write();return scope
 def close(self):assert not self.scopes
supervision.CgroupSupervisor=Supervisor

def configure(args,authority,stack,journal):
 access=AccessTable();access.configure({args.device_id:'usrp:serial:'+args.serial},journal)
 local={'enabled':True,'token':'fake-authority'};inventory=Inventory()
 def resolve(token):return args.device_id if token==local['token'] and local['enabled'] else None
 stack.enter_context(patch.object(v2,'hardware_access',access))
 stack.enter_context(patch.object(v2,'resolve_gid_for_token',resolve))
 stack.enter_context(patch.object(v2,'token_matches_device',lambda token,gid:gid==resolve(token)))
 stack.enter_context(patch.object(v2,'get_local_device_context',lambda gid:{'schema':inventory,'dtype':'usrp'}))
 return v2.SessionManager(),local,access.gate(args.device_id)
h.handoff.configure_local=configure
h.handoff.receive_native=lambda manager,session,settings,serial:{'received':4096,'fake_radio':True}
def verify(state,root):
 assert json.loads((state/'ownership/ownership.json').read_text())=={}
 assert json.loads((state/'processes/scopes.json').read_text())['scopes']=={}
 return {'hardware_records':0,'process_records':0,'owned_cgroups':0,'fake_kernel':True}
h.verify_empty_state=verify
args=SimpleNamespace(state_dir=state,cgroup_root=state/'fake-cgroup',serial='FAKE123',device_id=40,
 profile='usrp2901',rate=1e6,frequency=100e6,gain=0,child_case=mode)
authority=SimpleNamespace(active=lambda:True)
evidence={'explicit_fake_resources':True}
h.run_child(args,authority,evidence)
(state/'result.json').write_text(json.dumps(evidence))
'''


def wait_file(path, child):
    deadline = time.monotonic() + 10
    while not path.exists():
        assert child.poll() is None, child.communicate(timeout=1)
        assert time.monotonic() < deadline
        time.sleep(0.01)


@pytest.mark.parametrize('case', ['running', 'pending_restart'])
def test_real_signal_drives_actual_controller_and_service_cleanup_with_fake_radio(tmp_path, case):
    state = tmp_path / case
    child = subprocess.Popen([sys.executable, '-c', FAKE_CHILD, str(HARNESS), str(state), case],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env={**os.environ, 'GRPC_PORT': '50051', 'CERT_PORT': '50052',
                                  'REMOTERF_BIND_HOST': '127.0.0.1'})
    try:
        wait_file(state / 'ready.json', child)
        assert json.loads((state / 'ownership/ownership.json').read_text())
        if case == 'pending_restart':
            (state / 'request-restart').touch()
            wait_file(state / 'cleanup-ready.json', child)
        child.send_signal(signal.SIGTERM)
        if case == 'pending_restart':
            time.sleep(0.05)
            child.send_signal(signal.SIGTERM)
            (state / 'release-transport').touch()
        out, err = child.communicate(timeout=10)
        assert child.returncode == 0, (out, err)
        evidence = json.loads((state / 'result.json').read_text())
        assert evidence['explicit_fake_resources']
        assert evidence['native_rx_before']['fake_radio']
        assert evidence['hardware_records'] == evidence['process_records'] == 0
        assert evidence['admission_closed_before_transport']
        assert evidence['service_stopped'] and evidence['signal_handler_restored']
        assert evidence['reexec_calls'] == 0
    finally:
        if child.poll() is None:
            child.kill(); child.communicate(timeout=3)


@pytest.mark.parametrize('kind', ['hardware', 'processes'])
def test_nonempty_fence_cannot_be_relabelled_as_clean(tmp_path, kind):
    (tmp_path / 'ownership').mkdir(); (tmp_path / 'processes').mkdir()
    hardware = {'usrp:serial:FAKE123': {'lease': 'test'}} if kind == 'hardware' else {}
    processes = {'scopes': {'owner': {'state': 'ready'}} if kind == 'processes' else {}}
    hw = tmp_path / 'ownership/ownership.json'; proc = tmp_path / 'processes/scopes.json'
    hw.write_text(json.dumps(hardware)); proc.write_text(json.dumps(processes))
    with pytest.raises(RuntimeError, match='unresolved'):
        harness.verify_empty_state(tmp_path, tmp_path)
    assert json.loads(hw.read_text()) == hardware
    assert json.loads(proc.read_text()) == processes


def test_native_prewarm_only_imports_libraries(monkeypatch):
    imported = []
    def import_module(name):
        imported.append(name)
        return SimpleNamespace(__file__='/fake/' + name + '.py')
    monkeypatch.setattr(harness.importlib, 'import_module', import_module)
    evidence = harness.prewarm_native()
    assert imported == ['numpy', 'uhd', 'remoteRF_server.server.dynamic_v2']
    assert evidence['hardware_opened'] is False


def test_prewarm_precedes_authority_and_failed_reopen_preserves_child_evidence(tmp_path, monkeypatch):
    provider = tmp_path / 'provider'
    provider.write_text('unused')
    provider.chmod(0o700)
    args = SimpleNamespace(serial='FAKE123', reservation_id='fake-booking', device_id=40,
        authority_command=provider, cgroup_root=tmp_path / 'cgroup',
        state_dir=tmp_path / 'state', evidence=tmp_path / 'evidence.json', child_case=None, lock_fd=None)
    monkeypatch.setattr(harness, 'parser', lambda: SimpleNamespace(parse_args=lambda: args))
    monkeypatch.setitem(sys.modules, 'rrf_example_usrp_probe', SimpleNamespace(serial_selector=lambda value: value))
    monkeypatch.setattr(harness, 'private_environment', lambda state: None)
    monkeypatch.setattr(harness, 'source_identity', lambda: {})
    monkeypatch.setattr(harness.handoff, 'acquire_radio_lock', lambda *a: os.open(os.devnull, os.O_RDONLY))
    order = []
    def prewarm():
        order.append('prewarm')
        return {'hardware_opened': False}
    class Authority:
        def __init__(self, *args):
            assert order == ['prewarm']
            order.append('authority')
            self.last = {'reservation_id': 'fake-booking'}
        def close(self):
            order.append('close')
    def failed_reopen(args, authority, case, progress):
        progress.update(child_passed=True, child_exit=0, cleanup={'hardware_records': 0}, phase='next_holder_rx')
        raise RuntimeError('fake post-child receive rejection')
    monkeypatch.setattr(harness, 'prewarm_native', prewarm)
    monkeypatch.setattr(harness.handoff, 'Authority', Authority)
    monkeypatch.setattr(harness, 'run_case', failed_reopen)
    assert harness.main() == 1
    evidence = json.loads(args.evidence.read_text())
    assert order == ['prewarm', 'authority', 'close']
    assert evidence['passed'] is False
    assert evidence['checks']['running']['child_passed'] is True
    assert evidence['checks']['running']['phase'] == 'next_holder_rx'
    assert evidence['recovery_required_before_guardian_release'] is True
    assert evidence['failure']['trace'][-1]['function'] == 'failed_reopen'
    assert 'message' not in evidence['failure']
