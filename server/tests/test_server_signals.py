"""Real process SIGTERM with durable ownership and fake radio/transport state."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


CHILD = r'''
from pathlib import Path
import json, os, signal, sys, time
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from remoterf_device_core.integrations import Manifest, Registry
from remoterf_device_core.integrations.ownership import Gate, Journal
from remoterf_device_core.integrations.runtime import Binding, Runtime
from remoteRF_server.server import grpc_server as server
from remoteRF_server.server import console

path, entrypoint = Path(sys.argv[1]), sys.argv[2]
mode = sys.argv[3] if len(sys.argv) > 3 else 'running'
journal = Journal(path / 'ownership')
gate = Gate('qualification:fake-radio', journal)
authorized, application = Event(), Event()
authorized.set()
class Adapter:
    def validate(self, profile, value): return value
    def start(self, profile, value): application.set()
    def status(self): return {'ready': application.is_set()}
    def stop(self):
        time.sleep(0.05)
        application.clear()
definition = Manifest.from_dict(dict(id='org.example.signal',version='1.0.0',contract_version='0.1',
    factory='fixture_only:create',device_types=['usrp'],profiles=['test'],actions=[]))
registry = Registry([definition],enabled={'1':[definition.integration_id]})
registry.create = lambda *args: Adapter()
binding = Binding(gate,'1','usrp','fake-session',1,path,authorized.is_set,
    lambda:None,lambda:True,lambda:None,lambda:not application.is_set())
apps = Runtime(registry,workers=1,capacity=1)
operation = apps.start(binding,definition.integration_id,'test',{},request_key='start')['operation_id']
limit = time.monotonic()+3
while apps.status(binding,operation)['state'] != 'RUNNING':
    assert time.monotonic()<limit
    time.sleep(0.005)
admission_closed = Event()
def close_all(*,stop_admission=False):
    assert stop_admission
    admission_closed.set()
    authorized.clear()
    apps.shutdown()
def close_apps():
    limit = time.monotonic()+0.5
    while not apps.drained():
        assert time.monotonic()<limit
        time.sleep(0.005)
    apps.close()
    assert journal.records()=={}
    journal.close()
server.session_manager = SimpleNamespace(close_all=close_all,integrations=SimpleNamespace(close=close_apps))
server.host_session_proxy = Mock()
server.close_local_devices = Mock()
server.direct_path = Mock()
server.transport_counters = Mock()
transport = Mock()
def stop_transport(*args):
    assert admission_closed.is_set(), 'transport drain preceded admission fence'
    if mode != 'restart_without_term':
        # In restart mode this is the first TERM, delivered during cleanup.
        signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGTERM)  # Repeated TERM must not interrupt cleanup.
    return Mock()
transport.stop.side_effect = stop_transport
service = server.ServerRuntime(transport,None,Event(),None,'127.0.0.1',0,0)
server.start_server_runtime = lambda:service
server.start_agent = lambda:None
server.welcome = lambda:None
server._exit_action = 'restart' if mode.startswith('restart_') else None
server._reexec = Mock()
def waiting(*args):
    if mode.startswith('restart_'):
        return 'restart'
    (path/'ready').write_text('owned')
    Event().wait(30)
service.wait = waiting
server.session = SimpleNamespace(prompt=waiting)
console.attached = lambda:False
previous = signal.getsignal(signal.SIGTERM)
getattr(server,entrypoint)()
assert signal.getsignal(signal.SIGTERM) is previous
assert service._stopped and not application.is_set()
assert json.loads((path/'ownership'/'ownership.json').read_text())=={}
result = {'cleaned':True,'handler_restored':True,'admission_closed':True}
if mode.startswith('restart_'):
    result['reexec_calls'] = server._reexec.call_count
(path/'result.json').write_text(json.dumps(result))
'''


@pytest.mark.parametrize('entrypoint', ['headless_main', 'main', 'serve'])
def test_real_sigterm_cleans_running_application_and_restores_handler(tmp_path, entrypoint):
    env = {**os.environ, 'GRPC_PORT': '50051', 'CERT_PORT': '50052', 'REMOTERF_BIND_HOST': '127.0.0.1',
           'XDG_CONFIG_HOME': str(tmp_path / 'config'), 'XDG_DATA_HOME': str(tmp_path / 'data')}
    child = subprocess.Popen([sys.executable, '-c', CHILD, str(tmp_path), entrypoint], env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        limit = time.monotonic() + 10
        while not (tmp_path / 'ready').exists():
            assert child.poll() is None, child.communicate(timeout=1)
            assert time.monotonic() < limit
            time.sleep(0.01)
        assert json.loads((tmp_path / 'ownership' / 'ownership.json').read_text())
        started = time.monotonic()
        child.send_signal(signal.SIGTERM)
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, (stdout, stderr)
        assert time.monotonic() - started < 3
        assert json.loads((tmp_path / 'result.json').read_text()) == {
            'cleaned': True, 'handler_restored': True, 'admission_closed': True}
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=3)


def test_signal_handler_restored_after_agent_start_failure(monkeypatch):
    from unittest.mock import Mock
    from remoteRF_server.server import grpc_server as server
    runtime = Mock()
    monkeypatch.setattr(server, 'start_server_runtime', lambda: runtime)
    monkeypatch.setattr(server, 'start_agent', Mock(side_effect=RuntimeError('agent failed')))
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(RuntimeError, match='agent failed'):
        server.headless_main()
    runtime.stop.assert_called_once()
    assert signal.getsignal(signal.SIGTERM) is previous


@pytest.mark.parametrize('entrypoint', ['headless_main', 'main'])
@pytest.mark.parametrize('during_cleanup', [False, True])
def test_pending_restart_honors_real_sigterm_during_cleanup(tmp_path, entrypoint, during_cleanup):
    env = {**os.environ, 'GRPC_PORT': '50051', 'CERT_PORT': '50052', 'REMOTERF_BIND_HOST': '127.0.0.1',
           'XDG_CONFIG_HOME': str(tmp_path / 'config'), 'XDG_DATA_HOME': str(tmp_path / 'data')}
    mode = 'restart_during_term' if during_cleanup else 'restart_without_term'
    child = subprocess.run([sys.executable, '-c', CHILD, str(tmp_path), entrypoint, mode], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    assert child.returncode == 0, (child.stdout, child.stderr)
    assert json.loads((tmp_path / 'result.json').read_text()) == {
        'cleaned': True, 'handler_restored': True, 'admission_closed': True,
        'reexec_calls': 0 if during_cleanup else 1}
