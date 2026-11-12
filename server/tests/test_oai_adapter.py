import importlib
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from remoteRF_server.adapters import oai
from remoterf_device_core.integrations.registry import Manifest


class Processes:
    def __init__(self, log=b"ALL RUs ready\n"):
        self.launches = []
        self.log = log
        self.alive = True
        self.stop_ok = True
        self.stop_calls = []

    def launch(self, argv, **kwargs):
        self.launches.append((argv, kwargs))
        return "owned-process"

    def snapshot(self):
        return {"processes": [{"id": "owned-process",
                               "returncode": None if self.alive else 1}]}

    def log_tail(self, process_id, stream):
        return self.log

    def stop(self, **kwargs):
        self.stop_calls.append(kwargs)
        self.alive = False
        return self.stop_ok


def context(tmp_path, *, processes=None):
    return SimpleNamespace(selector={"serial": "ABC123"},
                           processes=processes or Processes(),
                           work_dir=tmp_path, cancelled=Event())


def operator_files(monkeypatch, tmp_path):
    softmodem = tmp_path / "nr-softmodem"
    conf = tmp_path / "gnb.conf"
    softmodem.write_text("")
    conf.write_text("")
    monkeypatch.setenv("REMOTERF_OAI_SOFTMODEM", str(softmodem))
    monkeypatch.setenv("REMOTERF_OAI_GNB_CONF", str(conf))
    return softmodem, conf


@pytest.mark.parametrize("profile,configuration", [
    ("receive", {}), ("gnb", {"frequency": 1}), ("gnb", []),
])
def test_validate_rejects_unsupported_inputs(tmp_path, profile, configuration):
    with pytest.raises(ValueError):
        oai.OAI(context(tmp_path)).validate(profile, configuration)


@pytest.mark.parametrize("name", ["REMOTERF_OAI_SOFTMODEM", "REMOTERF_OAI_GNB_CONF"])
@pytest.mark.parametrize("value", [None, "relative-file", "/missing/operator/file"])
def test_start_rejects_invalid_operator_file(monkeypatch, tmp_path, name, value):
    operator_files(monkeypatch, tmp_path)
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        oai.OAI(context(tmp_path)).start("gnb", {})


def test_start_launches_expected_process_and_becomes_ready(monkeypatch, tmp_path):
    softmodem, conf = operator_files(monkeypatch, tmp_path)
    monkeypatch.setenv("UHD_IMAGES_DIR", "/operator/uhd-images")
    ctx = context(tmp_path)
    adapter = oai.OAI(ctx)

    adapter.start("gnb", {})

    assert ctx.processes.launches == [
        ([str(softmodem), "-O", str(conf), "--RUs.[0].sdr_addrs", "serial=ABC123"], {
            "env": {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                    "UHD_IMAGES_DIR": "/operator/uhd-images"},
            "cwd": tmp_path,
            "required_while_running": True,
        })
    ]
    assert adapter.status() == {"ready": True}


def test_start_raises_when_process_exits_before_ready(monkeypatch, tmp_path):
    operator_files(monkeypatch, tmp_path)
    processes = Processes(log=b"")
    processes.alive = False
    with pytest.raises(RuntimeError, match="no longer running"):
        oai.OAI(context(tmp_path, processes=processes)).start("gnb", {})


def test_start_raises_when_cancelled(monkeypatch, tmp_path):
    operator_files(monkeypatch, tmp_path)
    ctx = context(tmp_path)
    ctx.cancelled.set()
    with pytest.raises(RuntimeError, match="cancelled"):
        oai.OAI(ctx).start("gnb", {})
    assert not ctx.processes.launches


def test_start_times_out(monkeypatch, tmp_path):
    operator_files(monkeypatch, tmp_path)
    monkeypatch.setattr(oai, "STARTUP_TIMEOUT", 0.01)
    with pytest.raises(RuntimeError, match="did not reach readiness"):
        oai.OAI(context(tmp_path, processes=Processes(log=b""))).start("gnb", {})


def test_stop_failure_idempotence_and_call_validation(tmp_path):
    processes = Processes()
    processes.stop_ok = False
    adapter = oai.OAI(context(tmp_path, processes=processes))
    with pytest.raises(RuntimeError, match="cleanup could not be verified"):
        adapter.stop()

    processes.stop_ok = True
    adapter.stop()
    adapter.stop()
    assert processes.stop_calls == [{"timeout": 10.0}, {"timeout": 10.0}]
    assert adapter.status() == {"ready": False, "stopped": True}
    with pytest.raises(ValueError):
        adapter.call("unknown", {})


def test_bundled_manifest_and_factory():
    path = Path(oai.__file__).with_name("integration.json")
    manifest = Manifest.from_file(path)
    module_name, factory_name = manifest.factory.split(":")
    factory = getattr(importlib.import_module(module_name), factory_name)
    assert manifest.integration_id == "org.remoterf.oai"
    assert factory is oai.create_adapter
