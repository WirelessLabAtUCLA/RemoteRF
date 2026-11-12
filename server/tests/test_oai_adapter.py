import importlib
import json
from pathlib import Path
import subprocess
import sys
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
        self.jobs = {}

    def launch(self, argv, **kwargs):
        self.launches.append((argv, kwargs))
        if kwargs.get("required_while_running"):
            return "owned-process"
        # Finite jobs really run, synchronously, so argv/env are exercised.
        job_id = f"job-{len(self.jobs)}"
        self.jobs[job_id] = subprocess.run(argv, env=kwargs["env"], cwd=kwargs["cwd"],
                                           capture_output=True, timeout=30)
        return job_id

    def snapshot(self):
        rows = [{"id": "owned-process", "returncode": None if self.alive else 1, "logs_drained": False}]
        rows += [{"id": key, "returncode": job.returncode, "logs_drained": True}
                 for key, job in self.jobs.items()]
        return {"processes": rows}

    def log_tail(self, process_id, stream):
        if process_id in self.jobs:
            return getattr(self.jobs[process_id], stream)
        return self.log

    def stop(self, **kwargs):
        self.stop_calls.append(kwargs)
        self.alive = False
        return self.stop_ok


def context(tmp_path, *, processes=None, user_code=False):
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    return SimpleNamespace(selector={"serial": "ABC123"},
                           processes=processes or Processes(),
                           work_dir=tmp_path, cancelled=Event(), user_code=user_code)


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
                    "OAI_SHM_DIR": str(tmp_path / "shm"),
                    "OAI_PLUGIN_CTRL": str(tmp_path / "plugin_ctrl.json"),
                    "UHD_IMAGES_DIR": "/operator/uhd-images"},
            "cwd": tmp_path,
            "required_while_running": True,
        })
    ]
    assert adapter.status() == {"ready": True}
    assert (tmp_path / "shm").is_dir()


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
    monkeypatch.setattr(oai, "MARGIN", 0.99)
    ctx = context(tmp_path, processes=Processes(log=b""))
    ctx.start_timeout_s = 1
    with pytest.raises(RuntimeError, match="did not reach readiness"):
        oai.OAI(ctx).start("gnb", {})


def test_stop_failure_idempotence_and_call_validation(tmp_path):
    processes = Processes()
    processes.stop_ok = False
    ctx = context(tmp_path, processes=processes)
    ctx.stop_timeout_s = 60
    adapter = oai.OAI(ctx)
    with pytest.raises(RuntimeError, match="cleanup could not be verified"):
        adapter.stop()

    processes.stop_ok = True
    adapter.stop()
    adapter.stop()
    assert processes.stop_calls == [{"timeout": 55}, {"timeout": 55}]
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


@pytest.mark.parametrize("action,parameters", [
    ("load_l1_plugin", {"stage": "pre_fft", "c_code": "int x;"}),
    ("unload_l1_plugin", {}),
    ("run_xapp", {"code": "pass", "duration_s": 1}),
])
def test_code_actions_refused_without_user_code(tmp_path, action, parameters):
    ctx = context(tmp_path)
    with pytest.raises(PermissionError):
        oai.OAI(ctx).call(action, parameters)
    assert not ctx.processes.launches and not (tmp_path / "plugin_ctrl.json").exists()


@pytest.mark.parametrize("action,parameters", [
    ("load_l1_plugin", {"stage": "Bad Stage", "c_code": "int x;"}),
    ("load_l1_plugin", {"stage": "pre_fft", "c_code": ""}),
    ("unload_l1_plugin", {"stage": "pre_fft"}),
    ("run_xapp", {"code": "pass", "duration_s": True}),
    ("run_xapp", {"code": "pass", "duration_s": 601}),
    ("run_xapp", {"code": "pass"}),
    ("health", {"extra": 1}),
])
def test_code_action_parameters_validated(tmp_path, action, parameters):
    with pytest.raises(ValueError):
        oai.OAI(context(tmp_path, user_code=True)).call(action, parameters)


def fake_compiler(tmp_path, monkeypatch, body):
    cc = tmp_path / "cc"
    cc.write_text("#!/bin/sh\n" + body)
    cc.chmod(0o755)
    monkeypatch.setenv("REMOTERF_OAI_CC", str(cc))
    return cc


def test_load_compiles_and_writes_ctrl_then_unload(monkeypatch, tmp_path):
    # $5 is the -o output path in "cc -O3 -shared -fPIC -o OUT SRC".
    cc = fake_compiler(tmp_path, monkeypatch, 'echo compiled; cp "$6" "$5"\n')
    ctx = context(tmp_path, user_code=True)
    adapter = oai.OAI(ctx)

    result = adapter.call("load_l1_plugin", {"stage": "pre_fft", "c_code": "int x;"})

    shared = tmp_path / "plugins" / "plugin-1.so"
    argv, kwargs = ctx.processes.launches[0]
    assert argv == [str(cc), "-O3", "-shared", "-fPIC", "-o", str(shared),
                    str(tmp_path / "plugins" / "plugin-1.c")]
    assert kwargs["cwd"] == tmp_path and "required_while_running" not in kwargs
    assert shared.read_text() == "int x;"
    assert result == {"ok": True, "seq": 1, "stage": "pre_fft", "path": str(shared),
                      "returncode": 0, "output": "compiled\n"}
    ctrl = tmp_path / "plugin_ctrl.json"
    assert json.loads(ctrl.read_text()) == {"seq": 1, "stage": "pre_fft", "path": str(shared)}

    assert adapter.call("unload_l1_plugin", {}) == {"ok": True, "seq": 2, "stage": None}
    assert json.loads(ctrl.read_text()) == {"seq": 2, "stage": None}


def test_failed_compile_returns_output_without_ctrl(monkeypatch, tmp_path):
    fake_compiler(tmp_path, monkeypatch, "echo 'error: nope' >&2; exit 1\n")
    result = oai.OAI(context(tmp_path, user_code=True)).call(
        "load_l1_plugin", {"stage": "pre_fft", "c_code": "bad"})
    assert result == {"ok": False, "returncode": 1, "output": "error: nope\n"}
    assert not (tmp_path / "plugin_ctrl.json").exists()


def test_run_xapp_writes_json_artifact(tmp_path):
    code = ("import json, os\n"
            "open(os.environ['XAPP_OUTPUT'], 'w').write(json.dumps({'shm': os.environ['OAI_SHM_DIR']}))\n")
    ctx = context(tmp_path, user_code=True)
    result = oai.OAI(ctx).call("run_xapp", {"code": code, "duration_s": 5})

    argv, kwargs = ctx.processes.launches[0]
    assert argv[:3] == [sys.executable, "-I", "-c"] and argv[4:] == [str(tmp_path / "xapps" / "xapp-1.py"), "5"]
    assert result == {"returncode": 0, "output": "", "artifact": {"name": "xapp-1.json", "format": "json"}}
    assert json.loads((tmp_path / "artifacts" / "xapp-1.json").read_text()) == {"shm": str(tmp_path / "shm")}


def test_run_xapp_is_ended_at_duration(tmp_path):
    result = oai.OAI(context(tmp_path, user_code=True)).call(
        "run_xapp", {"code": "while True:\n    pass\n", "duration_s": 1})
    assert result["returncode"] == -14 and "artifact" not in result


def test_job_over_bounded_wait_fails(monkeypatch, tmp_path):
    processes = Processes()
    processes.launch = lambda argv, **kwargs: "owned-process"
    monkeypatch.setattr(oai, "XAPP_GRACE", -1.0)
    with pytest.raises(RuntimeError, match="bounded wait"):
        oai.OAI(context(tmp_path, processes=processes, user_code=True)).call(
            "run_xapp", {"code": "pass", "duration_s": 1})
