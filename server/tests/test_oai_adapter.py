import importlib
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
from threading import Event
import time
from types import SimpleNamespace

import numpy as np
import pytest

from remoteRF_server.adapters import oai
from remoterf_device_core.integrations.ownership import Gate, Journal
from remoterf_device_core.integrations.payload import object_data
from remoterf_device_core.integrations.registry import Manifest, Registry
from remoterf_device_core.integrations.runtime import Binding, Runtime

SAMPLES = Path(__file__).parent / "data" / "oai"


class Processes:
    def __init__(self, log=b"[PHY] Signaling main thread that RU 0 is ready, sl_ahead 6\n", err=b""):
        self.launches = []
        self.log = log
        self.err = err
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
        return self.log if stream == "stdout" else self.err

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
    ("receive", {}), ("gnb", {"frequency": 1}), ("gnb", []), ("ue", {"band": 41}), ("ue", None),
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
        ([str(softmodem), "-O", str(conf), "--continuous-tx", "--RUs.[0].sdr_addrs", "serial=ABC123"], {
            "env": {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": str(tmp_path),
                    "OAI_SHM_DIR": str(tmp_path / "shm"),
                    "OAI_PLUGIN_CTRL": str(tmp_path / "plugin_ctrl.json"),
                    "UHD_IMAGES_DIR": "/operator/uhd-images"},
            "cwd": tmp_path,
            "required_while_running": True,
        })
    ]
    assert adapter.status() == {"ready": True, "profile": "gnb"}
    assert adapter.call("link_status", {}) == {"state": "running"}
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


def ue_files(monkeypatch, tmp_path, args="--band 41 -C 2593350000 -r 52"):
    uesoftmodem = tmp_path / "nr-uesoftmodem"
    conf = tmp_path / "ue.conf"
    uesoftmodem.write_text("")
    conf.write_text("")
    monkeypatch.setenv("REMOTERF_OAI_UESOFTMODEM", str(uesoftmodem))
    monkeypatch.setenv("REMOTERF_OAI_UE_CONF", str(conf))
    if args is not None:
        monkeypatch.setenv("REMOTERF_OAI_UE_ARGS", args)
    return uesoftmodem, conf


def test_validate_accepts_both_profiles(tmp_path):
    adapter = oai.OAI(context(tmp_path))
    assert adapter.validate("gnb", {}) == {} and adapter.validate("ue", {}) == {}


def test_gnb_args_override_conf_per_serial_and_shlibpath(monkeypatch, tmp_path):
    softmodem, _ = operator_files(monkeypatch, tmp_path)
    siso = tmp_path / "gnb-siso.conf"
    siso.write_text("")
    monkeypatch.setenv("REMOTERF_OAI_GNB_CONF_ABC123", str(siso))
    monkeypatch.setenv("REMOTERF_OAI_GNB_ARGS", "--continuous-tx --thread-pool '0,1'")
    monkeypatch.setenv("REMOTERF_OAI_SHLIBPATH", str(tmp_path))
    ctx = context(tmp_path)
    oai.OAI(ctx).start("gnb", {})
    assert ctx.processes.launches[0][0] == [
        str(softmodem), "-O", str(siso), "--continuous-tx", "--thread-pool", "0,1",
        "--loader.shlibpath", str(tmp_path), "--RUs.[0].sdr_addrs", "serial=ABC123"]


def test_gnb_ready_marker_on_stderr(monkeypatch, tmp_path):
    operator_files(monkeypatch, tmp_path)
    processes = Processes(log=b"", err=b"TYPE <CTRL-C> TO TERMINATE\n")
    adapter = oai.OAI(context(tmp_path, processes=processes))
    adapter.start("gnb", {})
    assert adapter.status()["ready"] is True


def test_ue_requires_operator_args(monkeypatch, tmp_path):
    ue_files(monkeypatch, tmp_path, args=None)
    with pytest.raises(RuntimeError, match="REMOTERF_OAI_UE_ARGS"):
        oai.OAI(context(tmp_path)).start("ue", {})


def test_ue_start_waits_for_interface_ip(monkeypatch, tmp_path):
    uesoftmodem, conf = ue_files(monkeypatch, tmp_path)
    answers = iter([None, None, "10.0.0.2"])
    seen = []
    monkeypatch.setattr(oai, "interface_ipv4", lambda name: seen.append(name) or next(answers, "10.0.0.2"))
    ctx = context(tmp_path, processes=Processes(log=b""))
    adapter = oai.OAI(ctx)

    adapter.start("ue", {})

    argv, kwargs = ctx.processes.launches[0]
    assert argv == [str(uesoftmodem), "-O", str(conf), "--band", "41", "-C", "2593350000",
                    "-r", "52", "--usrp-args", "serial=ABC123"]
    assert kwargs["required_while_running"] is True and kwargs["cwd"] == tmp_path
    assert seen[:3] == ["oaitun_ue1"] * 3
    assert adapter.status() == {"ready": True, "profile": "ue", "ip": "10.0.0.2"}
    assert adapter.call("link_status", {}) == {"state": "ip_up", "ifname": "oaitun_ue1", "ip": "10.0.0.2"}


def test_ue_conf_per_serial_and_custom_ifname(monkeypatch, tmp_path):
    ue_files(monkeypatch, tmp_path)
    own = tmp_path / "ue-own.conf"
    own.write_text("")
    monkeypatch.setenv("REMOTERF_OAI_UE_CONF_ABC123", str(own))
    monkeypatch.setenv("REMOTERF_OAI_UE_IFNAME", "oaitun_ue7")
    seen = []
    monkeypatch.setattr(oai, "interface_ipv4", lambda name: seen.append(name) or "10.0.0.9")
    ctx = context(tmp_path)
    oai.OAI(ctx).start("ue", {})
    assert ctx.processes.launches[0][0][2] == str(own) and seen == ["oaitun_ue7"]


@pytest.mark.parametrize("name", ["bad name; rm", "..", ".", "-x", "a" * 16])
def test_ue_rejects_bad_ifname(monkeypatch, tmp_path, name):
    ue_files(monkeypatch, tmp_path)
    monkeypatch.setenv("REMOTERF_OAI_UE_IFNAME", name)
    ctx = context(tmp_path)
    with pytest.raises(RuntimeError, match="IFNAME"):
        oai.OAI(ctx).start("ue", {})
    assert not ctx.processes.launches


def test_ue_exit_before_ip_fails(monkeypatch, tmp_path):
    ue_files(monkeypatch, tmp_path)
    monkeypatch.setattr(oai, "interface_ipv4", lambda name: None)
    processes = Processes(log=b"")
    processes.alive = False
    with pytest.raises(RuntimeError, match="ue process is no longer running"):
        oai.OAI(context(tmp_path, processes=processes)).start("ue", {})


def test_ue_link_status_syncing(monkeypatch, tmp_path):
    ue_files(monkeypatch, tmp_path)
    ips = iter(["10.0.0.2"])
    monkeypatch.setattr(oai, "interface_ipv4", lambda name: next(ips, None))
    adapter = oai.OAI(context(tmp_path))
    adapter.start("ue", {})
    assert adapter.call("link_status", {}) == {"state": "syncing", "ifname": "oaitun_ue1", "ip": None}


def started_ue(monkeypatch, tmp_path, ip="10.0.0.2"):
    ue_files(monkeypatch, tmp_path)
    monkeypatch.setattr(oai, "interface_ipv4", lambda name: ip)
    ctx = context(tmp_path)
    adapter = oai.OAI(ctx)
    adapter.start("ue", {})
    return ctx, adapter


def test_ping_runs_bounded_job_and_budget(monkeypatch, tmp_path):
    ctx, adapter = started_ue(monkeypatch, tmp_path)
    monkeypatch.setenv("REMOTERF_OAI_DN_IP", "127.0.0.1")
    ping = tmp_path / "ping-argv"
    real_launch = ctx.processes.launch

    def launch(argv, **kwargs):
        # Record the real ping argv, then run a stand-in through the fake's subprocess.run.
        if argv[0] == "/usr/bin/ping":
            ping.write_text(json.dumps(argv))
            argv = [sys.executable, "-c", "print('3 packets transmitted, 3 received')"]
        return real_launch(argv, **kwargs)

    ctx.processes.launch = launch
    result = adapter.call("ping", {"count": 3})
    # The ping deadline (-w) ends before the action wait (30 s default - MARGIN).
    assert json.loads(ping.read_text()) == ["/usr/bin/ping", "-n", "-I", "oaitun_ue1", "-c", "3",
                                            "-W", "2", "-w", "24", "127.0.0.1"]
    assert result["ok"] is True and "3 received" in result["output"]
    assert adapter.call("ping", {})["ok"] is True
    assert adapter.call("ping", {})["ok"] is True
    assert adapter.call("ping", {}) == {"ok": False, "error": "job_budget_exhausted"}


@pytest.mark.parametrize("parameters", [{"count": 0}, {"count": 21}, {"count": True}, {"host": "x"}])
def test_ping_validation(monkeypatch, tmp_path, parameters):
    _, adapter = started_ue(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        adapter.call("ping", parameters)


def test_ping_refused_for_gnb(monkeypatch, tmp_path):
    operator_files(monkeypatch, tmp_path)
    adapter = oai.OAI(context(tmp_path))
    adapter.start("gnb", {})
    assert adapter.call("ping", {}) == {"ok": False, "error": "ue_profile_required"}
    assert adapter.status()["ready"] is True


def test_invalid_dn_ip(monkeypatch, tmp_path):
    monkeypatch.setenv("REMOTERF_OAI_DN_IP", "not-an-ip")
    with pytest.raises(RuntimeError, match="DN_IP"):
        oai._dn_ip()


def test_ue_stop_waits_for_interface_removal(monkeypatch, tmp_path):
    _, adapter = started_ue(monkeypatch, tmp_path)
    gone = iter([True, True, False])
    monkeypatch.setattr(oai, "interface_exists", lambda name: next(gone))
    adapter.stop()
    adapter.stop()
    assert adapter.status() == {"ready": False, "stopped": True}


def test_ue_stop_raises_when_interface_persists(monkeypatch, tmp_path):
    _, adapter = started_ue(monkeypatch, tmp_path)
    monkeypatch.setattr(oai, "UE_IFACE_GONE_S", 0.2)
    monkeypatch.setattr(oai, "interface_exists", lambda name: True)
    with pytest.raises(RuntimeError, match="UE interface still present"):
        adapter.stop()


def test_code_actions_respect_job_budget(tmp_path):
    adapter = oai.OAI(context(tmp_path, user_code=True))
    adapter._jobs = oai.MAX_JOBS
    assert adapter.call("run_xapp", {"code": "pass", "duration_s": 1}) == {
        "ok": False, "error": "job_budget_exhausted"}
    assert adapter.call("load_l1_plugin", {"stage": "pre_fft", "c_code": "int x;"}) == {
        "ok": False, "error": "job_budget_exhausted"}


def test_bundled_manifest_declares_both_profiles():
    manifest = json.loads(Path(oai.__file__).with_name("integration.json").read_text())
    assert manifest["profiles"] == ["gnb", "ue"]
    assert {"health", "link_status", "ping"} <= set(manifest["actions"])


def test_ue_refuses_interface_owned_by_another_ue(monkeypatch, tmp_path):
    ue_files(monkeypatch, tmp_path)
    monkeypatch.setattr(oai, "interface_exists", lambda name: True)
    monkeypatch.setattr(oai, "interface_ipv4", lambda name: "10.0.0.2")
    ctx = context(tmp_path)
    adapter = oai.OAI(ctx)
    with pytest.raises(RuntimeError, match="already in use"):
        adapter.start("ue", {})
    assert not ctx.processes.launches
    adapter.stop()  # nothing launched, so no wait on the other UE's interface
    assert adapter.status() == {"ready": False, "stopped": True}


class Ring:
    """Python stand-in for the patched OAI CSI ring writer."""

    def __init__(self, tmp_path, stream="ul_pusch", n_entries=8, dims=(1, 2, 3), entry_bytes=None,
                 magic=oai.CSI_MAGIC, version=oai.CSI_VERSION):
        self.dims, self.n_entries = dims, n_entries
        self.entry_bytes = entry_bytes or oai.CSI_ENTRY.size + 4 * dims[0] * dims[1] * dims[2]
        self.path = tmp_path / "shm" / f"{stream}.ring"
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_bytes(bytes(oai.CSI_HEADER_BYTES + self.entry_bytes * n_entries))
        self._header(magic, version, 0)

    def _header(self, magic, version, write_seq):
        with open(self.path, "r+b") as handle:
            handle.write(oai.CSI_HEADER.pack(magic, version, self.entry_bytes, self.n_entries, 0, write_seq))

    def write(self, seq, lock=2, publish=True, snr_db=12.5, dims=None, ta=3):
        dims = self.dims if dims is None else dims
        n = dims[0] * dims[1] * dims[2]
        iq = (np.arange(2 * n, dtype=np.int64) + (seq & 0xFF)).astype("<i2")
        entry = oai.CSI_ENTRY.pack(
            lock, seq, (seq // 20) & 0xFFFFFFFF, seq % 20, 0x4601, snr_db, ta, *dims
        ) + iq.tobytes()
        with open(self.path, "r+b") as handle:
            handle.seek(oai.CSI_HEADER_BYTES + (seq % self.n_entries) * self.entry_bytes)
            handle.write(entry)
        if publish:
            self._header(oai.CSI_MAGIC, oai.CSI_VERSION, seq)

    def expected(self, seq):
        iq = (np.arange(2 * self.dims[0] * self.dims[1] * self.dims[2]) + seq).astype(np.float32)
        return (iq[0::2] + 1j * iq[1::2]).astype(np.complex64).reshape(self.dims)


def csi_artifact(tmp_path, result):
    artifact = result["artifact"]
    data = (tmp_path / "artifacts" / artifact["name"]).read_bytes()
    # Same decode as remoterf.integrations.App._decode.
    return np.frombuffer(data, dtype=artifact["dtype"]).reshape(artifact["shape"])


def test_read_csi_returns_newest_entry_without_user_code_or_jobs(tmp_path):
    ring = Ring(tmp_path)
    for seq in (1, 2, 3):
        ring.write(seq)
    ctx = context(tmp_path)
    result = oai.OAI(ctx).call("read_csi", {"stream": "ul_pusch"})

    assert result["ok"] and result["dropped"] == 0
    assert result["artifact"] == {"name": "csi-ul_pusch-1.c8", "dtype": "<c8", "shape": [1, 2, 3]}
    assert {k: v for k, v in result["metadata"].items() if k != "timestamps_s"} == {
        "seq": [3], "frame": [0], "slot": [3], "snr_db": [12.5], "rnti": [0x4601], "ta": [3]}
    assert len(result["metadata"]["timestamps_s"]) == 1
    values = csi_artifact(tmp_path, result)
    assert values.dtype == np.complex64 and np.array_equal(values, ring.expected(3))
    assert not ctx.processes.launches
    assert sorted(p.name for p in (tmp_path / "artifacts").iterdir()) == ["csi-ul_pusch-1.c8"]


def test_read_csi_retries_entry_being_written(tmp_path):
    ring = Ring(tmp_path)
    ring.write(1, lock=3)
    timer = threading.Timer(0.05, ring.write, args=(1,))
    timer.start()
    try:
        result = oai.OAI(context(tmp_path)).call("read_csi", {"stream": "ul_pusch"})
    finally:
        timer.join()
    assert result["ok"] and np.array_equal(csi_artifact(tmp_path, result), ring.expected(1))


def test_capture_csi_burst_reports_gap_and_torn_entry_as_dropped(tmp_path):
    ring = Ring(tmp_path)
    ring.write(1)

    def writer():
        time.sleep(0.05)
        for seq in (2, 3, 5, 6):  # 4 is never written
            ring.write(seq, lock=3 if seq == 5 else 2)  # 5 stays mid-write
        ring.write(7)
        ring.write(8)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        result = oai.OAI(context(tmp_path)).call(
            "capture_csi_burst", {"stream": "ul_pusch", "count": 5, "timeout_s": 5})
    finally:
        thread.join()

    assert result["ok"] and result["dropped"] == 2
    assert result["metadata"]["seq"] == [2, 3, 6, 7, 8]
    assert result["artifact"]["shape"] == [5, 1, 2, 3]
    values = csi_artifact(tmp_path, result)
    assert values.dtype == np.complex64
    assert all(np.array_equal(values[i], ring.expected(seq)) for i, seq in enumerate([2, 3, 6, 7, 8]))


def test_capture_csi_burst_times_out_without_new_entries(tmp_path):
    Ring(tmp_path).write(1)
    result = oai.OAI(context(tmp_path)).call(
        "capture_csi_burst", {"stream": "ul_pusch", "count": 1, "timeout_s": 0.05})
    assert result == {"ok": False, "error": "timeout", "dropped": 0}


def test_csi_missing_or_invalid_ring_returns_error_without_raising(tmp_path):
    adapter = oai.OAI(context(tmp_path))
    assert adapter.call("read_csi", {"stream": "ul_srs"}) == {"ok": False, "error": "ring_missing"}
    Ring(tmp_path, stream="ul_srs", magic=b"OTHERCSI")
    assert adapter.call("read_csi", {"stream": "ul_srs"}) == {"ok": False, "error": "ring_invalid"}
    Ring(tmp_path, stream="ul_srs", version=2)
    assert adapter.call("read_csi", {"stream": "ul_srs"}) == {"ok": False, "error": "ring_invalid"}
    (tmp_path / "shm" / "ul_srs.ring").write_bytes(b"")
    assert adapter.call("read_csi", {"stream": "ul_srs"}) == {"ok": False, "error": "ring_invalid"}


def test_csi_refused_past_artifact_limit(tmp_path):
    Ring(tmp_path, entry_bytes=1024 * 1024).write(1)
    result = oai.OAI(context(tmp_path)).call("capture_csi_burst", {"stream": "ul_pusch", "count": 200})
    assert result == {"ok": False, "error": "artifact_limit"}
    assert not list((tmp_path / "artifacts").iterdir())


@pytest.mark.parametrize("action,parameters", [
    ("read_csi", {}),
    ("read_csi", {"stream": "dl_csirs"}),
    ("read_csi", {"stream": "ul_pusch", "count": 1}),
    ("capture_csi_burst", {"stream": "ul_pusch"}),
    ("capture_csi_burst", {"stream": "ul_pusch", "count": 0}),
    ("capture_csi_burst", {"stream": "ul_pusch", "count": oai.MAX_CSI_BURST + 1}),
    ("capture_csi_burst", {"stream": "ul_pusch", "count": True}),
    ("capture_csi_burst", {"stream": "ul_pusch", "count": 1, "timeout_s": 0}),
    ("capture_csi_burst", {"stream": "ul_pusch", "count": 1, "extra": 1}),
])
def test_csi_parameters_validated(tmp_path, action, parameters):
    with pytest.raises(ValueError):
        oai.OAI(context(tmp_path)).call(action, parameters)


def test_csi_artifact_decodes_through_client(tmp_path):
    integrations = pytest.importorskip("remoterf.integrations")
    ring = Ring(tmp_path, dims=(2, 1, 4))
    ring.write(9)
    result = oai.OAI(context(tmp_path)).call("read_csi", {"stream": "ul_pusch"})
    app = integrations.App(None, "org.remoterf.oai", "gnb")
    app.__dict__["fetch"] = lambda name: (tmp_path / "artifacts" / name).read_bytes()
    decoded = app._decode(result)
    assert decoded["artifact"].dtype == np.complex64
    assert np.array_equal(decoded["artifact"], ring.expected(9))
    assert decoded["metadata"]["seq"] == [9]


@pytest.mark.parametrize("snr_db", [float("nan"), float("inf"), float("-inf")])
def test_read_csi_non_finite_snr_is_none_and_valid_payload(tmp_path, snr_db):
    ring = Ring(tmp_path)
    ring.write(1, snr_db=snr_db)
    result = oai.OAI(context(tmp_path)).call("read_csi", {"stream": "ul_pusch"})
    assert result["ok"] is True
    assert result["metadata"]["snr_db"] == [None]
    # Runtime._call validates every action result with object_data(..., max_bytes=48 * 1024).
    assert object_data(result, max_bytes=48 * 1024)["metadata"]["snr_db"] == [None]


@pytest.mark.parametrize("stop_by", ["deadline", "cancel"])
def test_csi_burst_inner_loop_checks_deadline_and_cancellation(monkeypatch, tmp_path, stop_by):
    ring = Ring(tmp_path, n_entries=8)
    ring.write(1)
    ctx = context(tmp_path)
    now = [0.0]
    reads = [0]

    def sleep(seconds):
        if now[0] == 0.0:
            for seq in (2, 3, 4, 5):
                ring.write(seq)
        now[0] += seconds

    def wall_time():
        reads[0] += 1
        if stop_by == "deadline":
            now[0] = 100.0
        else:
            ctx.cancelled.set()
        return 1.0

    monkeypatch.setattr(oai, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=sleep, time=wall_time))

    if stop_by == "deadline":
        result = oai.OAI(ctx).call("capture_csi_burst", {"stream": "ul_pusch", "count": 4, "timeout_s": 5})
        assert result == {"ok": False, "error": "timeout", "dropped": 0}
    else:
        with pytest.raises(RuntimeError, match="cancelled"):
            oai.OAI(ctx).call("capture_csi_burst", {"stream": "ul_pusch", "count": 4, "timeout_s": 5})
    assert reads[0] == 1


def test_csi_burst_retry_loop_stops_at_deadline(monkeypatch, tmp_path):
    ring = Ring(tmp_path, n_entries=8)
    ring.write(1)
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            for seq in (2, 3, 4, 5):
                ring.write(seq, lock=3)  # mid-write
        now[0] += seconds

    monkeypatch.setattr(oai, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=sleep, time=time.time))
    monkeypatch.setattr(oai, "CSI_POLL_S", 10.0)  # retry sleep is 1.0 s

    result = oai.OAI(context(tmp_path)).call(
        "capture_csi_burst", {"stream": "ul_pusch", "count": 4, "timeout_s": 15})

    assert result == {"ok": False, "error": "timeout", "dropped": 0}
    assert sleeps == [10.0, 1.0, 1.0, 1.0, 1.0, 1.0]


@pytest.mark.parametrize("stop_by", ["deadline", "cancel"])
def test_read_csi_retry_loop_stops_on_deadline_and_cancellation(monkeypatch, tmp_path, stop_by):
    ring = Ring(tmp_path)
    ring.write(1, lock=3)  # mid-write
    ctx = context(tmp_path)
    ctx.action_timeout_s = 10.0  # _bound -> 5.0 s
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        if stop_by == "cancel" and len(sleeps) == 2:
            ctx.cancelled.set()

    monkeypatch.setattr(oai, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=sleep, time=time.time))
    monkeypatch.setattr(oai, "CSI_POLL_S", 10.0)  # retry sleep is 1.0 s

    if stop_by == "deadline":
        result = oai.OAI(ctx).call("read_csi", {"stream": "ul_pusch"})
        assert result == {"ok": False, "error": "timeout", "dropped": 0}
        assert sleeps == [1.0] * 5
    else:
        with pytest.raises(RuntimeError, match="cancelled"):
            oai.OAI(ctx).call("read_csi", {"stream": "ul_pusch"})
        assert sleeps == [1.0, 1.0]


def test_stop_unlinks_csi_rings_and_removes_empty_shm(tmp_path):
    Ring(tmp_path, stream="ul_pusch")
    Ring(tmp_path, stream="dl_ssb")
    processes = Processes()
    processes.stop_ok = False
    adapter = oai.OAI(context(tmp_path, processes=processes))

    with pytest.raises(RuntimeError, match="cleanup could not be verified"):
        adapter.stop()
    assert (tmp_path / "shm" / "ul_pusch.ring").exists()

    processes.stop_ok = True
    adapter.stop()
    assert not (tmp_path / "shm").exists()


def test_stop_keeps_non_empty_shm_after_unlinking_rings(tmp_path):
    Ring(tmp_path, stream="ul_pusch")
    other = tmp_path / "shm" / "xapp.shm"
    other.write_bytes(b"keep")
    oai.OAI(context(tmp_path)).stop()
    assert not (tmp_path / "shm" / "ul_pusch.ring").exists()
    assert other.is_file()


@pytest.mark.parametrize("bad_dims", [(0, 2, 3), (1, 0, 3), (1, 2, 0)])
def test_csi_rejects_zero_dim_entry(monkeypatch, tmp_path, bad_dims):
    ring = Ring(tmp_path, dims=(1, 2, 3))
    ring.write(1)
    now = [0.0]

    def sleep(seconds):
        if now[0] == 0.0:
            ring.write(2, dims=bad_dims)
            ring.write(3)
        now[0] += seconds

    monkeypatch.setattr(oai, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=sleep, time=time.time))
    result = oai.OAI(context(tmp_path)).call(
        "capture_csi_burst", {"stream": "ul_pusch", "count": 1, "timeout_s": 5})

    assert result["ok"] is True and result["dropped"] == 1
    assert result["metadata"]["seq"] == [3]
    assert np.array_equal(csi_artifact(tmp_path, result), ring.expected(3)[None])


@pytest.mark.parametrize("seq", [1 << 63, (1 << 64) - 1])
def test_csi_rejects_seq_outside_int64(monkeypatch, tmp_path, seq):
    ring = Ring(tmp_path)
    ring.write(seq)
    ctx = context(tmp_path)
    ctx.action_timeout_s = 6.0  # _bound -> 1.0 s
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(oai, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=sleep, time=time.time))
    monkeypatch.setattr(oai, "CSI_POLL_S", 1.0)

    assert oai.OAI(ctx).call("read_csi", {"stream": "ul_pusch"}) == {
        "ok": False, "error": "timeout", "dropped": 0}


def test_csi_accepts_max_int64_seq(tmp_path):
    ring = Ring(tmp_path)
    max_seq = (1 << 63) - 1
    ring.write(max_seq)
    result = oai.OAI(context(tmp_path)).call("read_csi", {"stream": "ul_pusch"})
    assert result["ok"] is True
    assert result["metadata"]["seq"] == [max_seq]
    assert object_data(result, max_bytes=48 * 1024)["metadata"]["seq"] == [max_seq]



# --- OAI expansion tests ---

EXPANSION_ACTIONS = {
    "get_cell_config",
    "get_link_stats",
    "get_ue_mac_table",
    "read_ofdm_grid",
    "read_constellation",
    "throughput_test",
    "start_traffic",
    "stop_traffic",
    "capture_air_pcap",
    "set_live_control",
}


def started_gnb(monkeypatch, tmp_path, conf_path=None, args=None):
    softmodem, default_conf = operator_files(monkeypatch, tmp_path)
    if conf_path is not None:
        monkeypatch.setenv("REMOTERF_OAI_GNB_CONF", str(conf_path))
    if args is not None:
        monkeypatch.setenv("REMOTERF_OAI_GNB_ARGS", args)
    else:
        monkeypatch.delenv("REMOTERF_OAI_GNB_ARGS", raising=False)
    ctx = context(tmp_path)
    adapter = oai.OAI(ctx)
    adapter.start("gnb", {})
    return ctx, adapter


def test_bundled_manifest_declares_expansion_actions():
    path = Path(oai.__file__).with_name("integration.json")
    manifest = Manifest.from_file(path)
    assert EXPANSION_ACTIONS <= set(manifest.actions)


@pytest.mark.parametrize("arfcn,expected_hz", [
    (0, 0),
    (517734, 2_588_670_000),
    (518670, 2_593_350_000),
    (599999, 2_999_995_000),
    (600000, 3_000_000_000),
    (641280, 3_619_200_000),
    (2016666, 24_249_990_000),
    (2016667, 24_250_080_000),
    (2070833, 27_500_040_000),
    (3279165, 99_999_960_000),
])
def test_nr_arfcn_to_hz_all_three_3gpp_ranges(arfcn, expected_hz):
    assert oai.nr_arfcn_to_hz(arfcn) == expected_hz
    assert oai.hz_to_nr_arfcn(expected_hz) == arfcn


@pytest.mark.parametrize("bad_arfcn", [-1, 3_279_166, True, False, None, "518670", 518670.0])
def test_nr_arfcn_to_hz_rejects_invalid_values(bad_arfcn):
    assert oai.nr_arfcn_to_hz(bad_arfcn) is None
    assert oai.hz_to_nr_arfcn(100_000_020_000) is None
    assert oai.hz_to_nr_arfcn(-5_000) is None


@pytest.mark.parametrize("prbs,mu,threeq,expected", [
    (25, 0, False, 512),
    (24, 1, False, 512),
    (52, 0, False, 1024),
    (52, 0, True, 768),
    (79, 0, False, 1536),
    (78, 1, False, 1536),
    (106, 1, False, 2048),
    (106, 1, True, 1536),
    (162, 1, False, 3072),
    (273, 1, False, 4096),
    (273, 1, True, 4096),
    (0, 0, False, None),
    (276, 1, False, None),
])
def test_oai_fft_size_derivation(prbs, mu, threeq, expected):
    assert oai.oai_fft_size(prbs, mu, threequarter_fs=threeq) == expected


@pytest.mark.parametrize("conf_name", ["gnb-band41-1x1-rfb-915.conf", "gnb-band41-1x1.conf"])
def test_get_cell_config_parses_sample_gnb_confs(monkeypatch, tmp_path, conf_name):
    ctx, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / conf_name)
    res = adapter.call("get_cell_config", {})

    assert res["ok"] is True
    assert res["profile"] == "gnb"
    assert res["band"] == 41
    assert res["ssb_arfcn"] == 518670
    assert res["ssb_frequency_hz"] == 2_593_350_000
    assert res["point_a_arfcn"] == 517734
    assert res["point_a_frequency_hz"] == 2_588_670_000
    assert res["dl_center_frequency_hz"] == 2_593_350_000
    assert res["ul_center_frequency_hz"] == 2_593_350_000
    assert res["if_freq_hz"] == 915_000_000
    assert res["numerology"] == 0
    assert res["scs_khz"] == 15
    assert res["scs_hz"] == 15_000
    assert res["prb_count"] == 52
    assert res["dl_prb_count"] == 52
    assert res["ul_prb_count"] == 52
    assert res["bandwidth_hz"] == 9_360_000
    assert res["fft_size"] == 1024
    assert res["sample_rate_hz"] == 15_360_000
    assert res["tdd_pattern"] == {
        "periodicity_enum": 7,
        "periodicity_ms": 10.0,
        "reference_scs_khz": 15,
        "dl_slots": 6,
        "dl_symbols": 6,
        "ul_slots": 3,
        "ul_symbols": 4,
    }
    assert res["mimo_mode"] == {
        "mode": "1x1",
        "pdsch_antenna_ports": 1,
        "pdsch_antenna_ports_xp": 1,
        "pdsch_antenna_ports_n1": None,
        "pdsch_antenna_ports_n2": None,
        "pusch_antenna_ports": 1,
        "nb_tx": 1,
        "nb_rx": 1,
    }
    assert res["phys_cell_id"] == 0
    assert res["nr_cell_id"] == 12345678
    assert res["mcs_limits"] == {"dl_min_mcs": 4, "dl_max_mcs": 20, "ul_min_mcs": 4, "ul_max_mcs": 16}
    assert res["max_ldpc_iterations"] == 10
    assert len(ctx.processes.launches) == 1  # read-only, in-process, zero jobs
    assert object_data(res, max_bytes=48 * 1024)["band"] == 41


def test_get_cell_config_fdd_without_ul_point_a_leaves_ul_freq_null(tmp_path):
    conf = tmp_path / "fdd.conf"
    conf.write_text(
        "gNBs = ({ servingCellConfigCommon = ({"
        " dl_frequencyBand = 7; ul_frequencyBand = 7;"
        " dl_absoluteFrequencyPointA = 530000; dl_subcarrierSpacing = 0; dl_carrierBandwidth = 52;"
        " ul_subcarrierSpacing = 0; ul_carrierBandwidth = 52;"
        "}); });\n",
        encoding="utf-8",
    )
    res = oai.extract_gnb_cell_config(conf)
    assert res["band"] == 7
    assert res["dl_center_frequency_hz"] is not None
    assert res["ul_center_frequency_hz"] is None


def test_get_cell_config_applies_arg_overrides_and_per_serial_conf(monkeypatch, tmp_path):
    operator_files(monkeypatch, tmp_path)
    monkeypatch.setenv("REMOTERF_OAI_GNB_CONF_ABC123", str(SAMPLES / "gnb-band41-1x1.conf"))
    monkeypatch.setenv(
        "REMOTERF_OAI_GNB_ARGS",
        "--continuous-tx -E "
        "--gNBs.[0].servingCellConfigCommon.[0].dl_carrierBandwidth 106 "
        "--gNBs.[0].servingCellConfigCommon.[0].dl_subcarrierSpacing 1 "
        "--gNBs.[0].pdsch_AntennaPorts_XP 2 "
        "--gNBs.[0].pusch_AntennaPorts 2 "
        "--RUs.[0].nb_tx 2 --RUs.[0].nb_rx 2",
    )
    ctx = context(tmp_path)
    adapter = oai.OAI(ctx)
    adapter.start("gnb", {})

    res = adapter.call("get_cell_config", {})
    assert res["ok"] is True
    assert res["prb_count"] == 106
    assert res["numerology"] == 1
    assert res["scs_khz"] == 30
    assert res["fft_size"] == 1536
    assert res["sample_rate_hz"] == 46_080_000
    assert res["threequarter_fs"] is True
    assert res["mimo_mode"]["mode"] == "2x2"
    assert res["mimo_mode"]["pdsch_antenna_ports"] == 2
    assert res["mimo_mode"]["pusch_antenna_ports"] == 2
    assert res["mimo_mode"]["nb_tx"] == 2 and res["mimo_mode"]["nb_rx"] == 2


def test_get_cell_config_ue_parses_production_ue_args(monkeypatch, tmp_path):
    ue_args = "-r 52 --numerology 0 --band 41 -C 2593350000 --ssb 192 --ue-fo-compensation --if_freq 915000000"
    ue_files(monkeypatch, tmp_path, args=ue_args)
    monkeypatch.setattr(oai, "interface_ipv4", lambda name: "10.0.0.2")
    ctx = context(tmp_path)
    adapter = oai.OAI(ctx)
    adapter.start("ue", {})

    res = adapter.call("get_cell_config", {})
    assert res["ok"] is True
    assert res["profile"] == "ue"
    assert res["band"] == 41
    assert res["dl_center_frequency_hz"] == 2_593_350_000
    assert res["ul_center_frequency_hz"] == 2_593_350_000
    assert res["point_a_frequency_hz"] == 2_588_670_000
    assert res["point_a_arfcn"] == 517734
    assert res["ssb_frequency_hz"] == 2_593_350_000
    assert res["ssb_arfcn"] == 518670
    assert res["if_freq_hz"] == 915_000_000
    assert res["numerology"] == 0
    assert res["scs_khz"] == 15
    assert res["prb_count"] == 52
    assert res["fft_size"] == 1024
    assert res["sample_rate_hz"] == 15_360_000
    assert res["tdd_pattern"]["dl_slots"] is None
    assert res["mimo_mode"]["pdsch_antenna_ports"] is None
    assert res["mimo_mode"]["nb_tx"] is None
    assert object_data(res, max_bytes=48 * 1024)["profile"] == "ue"


def test_get_link_stats_and_ue_mac_table_from_sample_logs(monkeypatch, tmp_path):
    ctx, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf")
    mac_dst = tmp_path / "nrMAC_stats.log"
    l1_dst = tmp_path / "nrL1_stats.log"
    shutil.copyfile(SAMPLES / "nrMAC_stats.log", mac_dst)
    shutil.copyfile(SAMPLES / "nrL1_stats.log", l1_dst)
    stale_ts = time.time() - 12.5
    os.utime(mac_dst, (stale_ts, stale_ts))

    stats = adapter.call("get_link_stats", {})
    table = adapter.call("get_ue_mac_table", {})

    assert stats["ok"] is True and table["ok"] is True
    assert stats["ue_count"] == 1 and len(stats["ues"]) == 1
    assert table["ues"] == stats["ues"]
    assert "cell" not in table

    ue = stats["ues"][0]
    assert ue["rnti"] == "4eca"
    assert ue["in_sync"] is True
    assert ue["cqi"] == 0
    assert ue["ri"] == 1
    assert ue["pmi"] == [0, 0]
    assert ue["ul_ri"] == 1
    assert ue["tpmi"] == 0
    assert ue["ph_db"] == 7
    assert ue["pcmax_dbm"] == 20
    assert ue["dlsch_rounds"] == [69, 46, 34, 28, 17, 15, 13, 10]
    assert ue["dlsch_errors"] == 10
    assert ue["ulsch_rounds"] == [255, 49, 42, 42, 41, 41, 41, 41]
    assert ue["ulsch_errors"] == 41
    assert ue["dl_bler"] == pytest.approx(0.896)
    assert ue["ul_bler"] == pytest.approx(0.9867)
    assert ue["dl_mcs"] == 4 and ue["ul_mcs"] == 4
    assert ue["dl_qm"] == 2 and ue["ul_qm"] == 2
    assert ue["nprb"] == 5
    assert ue["dl_snr_db"] == pytest.approx(14.3)
    assert ue["ul_snr_db"] == pytest.approx(25.8)
    assert ue["snr_db"] == pytest.approx(25.8)
    assert ue["dl_rssi_dbm"] == pytest.approx(-49.4)
    assert ue["ul_rssi_dbm"] == pytest.approx(-33.2)
    assert ue["rssi_dbm"] == pytest.approx(-33.2)
    assert ue["goodput_dl_mbps"] == pytest.approx(0.0)
    assert ue["goodput_ul_mbps"] == pytest.approx(0.01)
    assert ue["dl_total_bytes_tx"] == 16404
    assert ue["ul_total_bytes_rx"] == 0
    assert ue["ul_total_bytes_sched"] == 369

    assert stats["cell"]["prach_i0_db"] == pytest.approx(27.3)
    assert stats["cell"]["avg_i0_db"] == pytest.approx(40.0)
    assert stats["cell"]["blacklisted_prb_count"] == 0
    assert stats["cell"]["total_prb_count"] == 52
    assert stats["files"]["mac_stats_age_s"] >= 10.0
    assert stats["file_age_s"] >= 10.0
    assert len(ctx.processes.launches) == 1  # in-process, zero jobs
    assert object_data(stats, max_bytes=48 * 1024)["ue_count"] == 1
    assert object_data(table, max_bytes=48 * 1024)["ue_count"] == 1


def test_link_stats_truncated_and_missing_files(monkeypatch, tmp_path):
    _, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf")

    missing_stats = adapter.call("get_link_stats", {})
    missing_table = adapter.call("get_ue_mac_table", {})
    assert missing_stats["ok"] is False and missing_stats["error"] == "stats_missing"
    assert missing_table["ok"] is False and missing_table["error"] == "stats_missing"

    # Truncated MAC file mid-line and no L1 file.
    (tmp_path / "nrMAC_stats.log").write_text(
        "=== Cell 0 ===\n"
        "UE RNTI 4eca PCI 0 in-sync PH 7 dB\n"
        "UE 4eca: CSI [CQI 5\n",
        encoding="utf-8",
    )
    partial = adapter.call("get_link_stats", {})
    assert partial["ok"] is True
    assert partial["files"]["mac_stats_present"] is True
    assert partial["files"]["l1_stats_present"] is False
    assert partial["cell"]["prach_i0_db"] is None
    assert partial["cell"]["blacklisted_prb_count"] is None
    ue = partial["ues"][0]
    assert ue["rnti"] == "4eca" and ue["ph_db"] == 7 and ue["cqi"] == 5
    assert ue["pcmax_dbm"] is None and ue["dlsch_rounds"] is None and ue["goodput_dl_mbps"] is None
    assert object_data(partial, max_bytes=48 * 1024)["ok"] is True

    # Only truncated L1 file.
    (tmp_path / "nrMAC_stats.log").unlink()
    (tmp_path / "nrL1_stats.log").write_text(
        "Blacklisted PRBs 3/52\nPRACH I0 = 27.3 dB\n",
        encoding="utf-8",
    )
    l1_only = adapter.call("get_link_stats", {})
    assert l1_only["ok"] is True
    assert l1_only["cell"]["blacklisted_prb_count"] == 3
    assert l1_only["cell"]["prach_i0_db"] == pytest.approx(27.3)
    assert l1_only["cell"]["avg_i0_db"] is None
    assert l1_only["ues"] == []


def test_link_stats_on_ue_profile(monkeypatch, tmp_path):
    _, adapter = started_ue(monkeypatch, tmp_path)
    no_stats = adapter.call("get_link_stats", {})
    no_table = adapter.call("get_ue_mac_table", {})
    assert no_stats["ok"] is False and no_stats["error"] == "not_available_on_ue"
    assert no_table["ok"] is False and no_table["error"] == "not_available_on_ue"

    (tmp_path / "nrUE_stats.log").write_text(
        "RNTI 4eca RSRP -95.5 SNR 18.2 CQI 12 BLER 0.01\n",
        encoding="utf-8",
    )
    present = adapter.call("get_link_stats", {})
    assert present["ok"] is True and present["profile"] == "ue"
    assert present["ue_stats"]["rnti"] == "4eca"
    assert present["ue_stats"]["rsrp_dbm"] == pytest.approx(-95.5)
    assert present["ue_stats"]["snr_db"] == pytest.approx(18.2)
    assert present["ue_stats"]["cqi"] == 12


def test_read_ofdm_grid_and_constellation_streams(monkeypatch, tmp_path):
    ctx, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf")

    assert adapter.call("read_ofdm_grid", {})["error"] == "ring_missing"
    assert adapter.call("read_constellation", {})["error"] == "ring_missing"
    assert adapter.call("read_ofdm_grid", {"timeout_s": 0})["error"] == "invalid_parameters"
    assert adapter.call("read_constellation", {"timeout_s": True})["error"] == "invalid_parameters"
    assert adapter.call("read_constellation", {"extra": 1})["error"] == "invalid_parameters"

    # Empty ring (write_seq == 0) times out cleanly without raising.
    Ring(tmp_path, stream="ofdm_grid", dims=(1, 14, 12))
    assert adapter.call("read_ofdm_grid", {"timeout_s": 0.02}) == {"ok": False, "error": "timeout", "dropped": 0}

    grid_ring = Ring(tmp_path, stream="ofdm_grid", dims=(1, 14, 12))
    grid_ring.write(1)
    grid_ring.write(2)
    grid_res = adapter.call("read_ofdm_grid", {})
    assert grid_res["ok"] is True and grid_res["dropped"] == 0
    assert grid_res["artifact"] == {"name": "ofdm_grid-1.c8", "dtype": "<c8", "shape": [1, 14, 12]}
    assert grid_res["metadata"]["seq"] == [2]
    assert np.array_equal(csi_artifact(tmp_path, grid_res), grid_ring.expected(2))
    assert object_data(grid_res, max_bytes=48 * 1024)["ok"] is True

    # Non-14-symbol grid is rejected as ring_invalid.
    bad_grid = Ring(tmp_path, stream="ofdm_grid", dims=(1, 7, 12))
    bad_grid.write(1)
    assert adapter.call("read_ofdm_grid", {})["error"] == "ring_invalid"

    # Constellation with explicit writer EVM (ta = 4250 -> 4.25%).
    const_ring = Ring(tmp_path, stream="constellation", dims=(1, 1, 16))
    const_ring.write(5, snr_db=20.0, ta=4250)
    const_res = adapter.call("read_constellation", {})
    assert const_res["ok"] is True
    assert const_res["artifact"] == {"name": "constellation-1.c8", "dtype": "<c8", "shape": [1, 1, 16]}
    assert const_res["metadata"]["evm_pct"] == pytest.approx(4.25)
    assert const_res["metadata"]["evm_db"] == pytest.approx(-27.43)
    assert const_res["metadata"]["evm_source"] == "writer"
    assert np.array_equal(csi_artifact(tmp_path, const_res), const_ring.expected(5))
    assert object_data(const_res, max_bytes=48 * 1024)["metadata"]["evm_pct"] == pytest.approx(4.25)

    # Constellation with unset writer EVM (ta = 0) derives EVM from SNR.
    const_ring.write(6, snr_db=20.0, ta=0)
    derived = adapter.call("read_constellation", {})
    assert derived["ok"] is True
    assert derived["metadata"]["evm_pct"] == pytest.approx(10.0)
    assert derived["metadata"]["evm_db"] == pytest.approx(-20.0)
    assert derived["metadata"]["evm_source"] == "snr_derived"

    # Non-finite SNR with unset writer EVM stays valid JSON payload.
    const_ring.write(7, snr_db=float("nan"), ta=0)
    nan_res = adapter.call("read_constellation", {})
    assert nan_res["ok"] is True
    assert nan_res["metadata"]["evm_pct"] is None and nan_res["metadata"]["evm_db"] is None
    assert object_data(nan_res, max_bytes=48 * 1024)["metadata"]["evm_pct"] is None

    assert len(ctx.processes.launches) == 1  # zero jobs spent
    adapter.stop()
    assert not (tmp_path / "shm" / "ofdm_grid.ring").exists()
    assert not (tmp_path / "shm" / "constellation.ring").exists()


def _write_python_wrapper_script(tmp_path, name, py_body):
    py_path = tmp_path / f"{name}.py"
    sh_path = tmp_path / name
    py_path.write_text(py_body, encoding="utf-8")
    sh_path.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(py_path))} \"$@\"\n",
        encoding="utf-8",
    )
    sh_path.chmod(0o755)
    return sh_path


def test_throughput_test_tcp_and_udp_parsing_with_fake_job_runner(monkeypatch, tmp_path):
    ctx, adapter = started_ue(monkeypatch, tmp_path)
    fake_iperf = tmp_path / "iperf3"
    fake_iperf.write_text("", encoding="utf-8")
    monkeypatch.setenv("REMOTERF_OAI_IPERF3", str(fake_iperf))
    monkeypatch.setenv("REMOTERF_OAI_DN_IP", "192.168.70.135")

    tcp_payload = {
        "end": {
            "sum_sent": {"bytes": 12_500_000, "bits_per_second": 25_000_000.0, "retransmits": 3, "seconds": 4.0},
            "sum_received": {"bytes": 12_400_000, "bits_per_second": 24_800_000.0, "seconds": 4.0},
        }
    }
    udp_payload = {
        "end": {
            "sum": {
                "bytes": 6_250_000,
                "bits_per_second": 12_500_000.0,
                "jitter_ms": 0.412,
                "lost_packets": 5,
                "packets": 4000,
                "lost_percent": 0.125,
                "seconds": 4.0,
            }
        }
    }
    err_payload = {"error": "unable to connect to server: Connection refused"}
    responses = iter([
        (0, json.dumps(tcp_payload)),
        (0, json.dumps(udp_payload)),
        (1, json.dumps(err_payload)),
    ])
    recorded_argvs = []
    real_launch = ctx.processes.launch

    def fake_launch(argv, **kwargs):
        if str(fake_iperf) in argv:
            idx = argv.index(str(fake_iperf))
            recorded_argvs.append(argv[idx:])
            rc, body = next(responses)
            argv = [*argv[:idx], sys.executable, "-c", f"import sys; sys.stdout.write({body!r}); sys.exit({rc})"]
        return real_launch(argv, **kwargs)

    ctx.processes.launch = fake_launch

    tcp_res = adapter.call("throughput_test", {"duration_s": 4, "direction": "dl", "protocol": "tcp"})
    assert tcp_res["ok"] is True
    assert tcp_res["direction"] == "dl" and tcp_res["protocol"] == "tcp"
    assert tcp_res["throughput_mbps"] == pytest.approx(24.8)
    assert tcp_res["sent_mbps"] == pytest.approx(25.0)
    assert tcp_res["received_mbps"] == pytest.approx(24.8)
    assert tcp_res["retransmits"] == 3
    assert tcp_res["jitter_ms"] is None and tcp_res["loss_percent"] is None
    assert recorded_argvs[0] == [
        str(fake_iperf), "-c", "192.168.70.135", "-B", "10.0.0.2", "-p", "5201",
        "-t", "4", "-P", "1", "-i", "0", "-J", "--connect-timeout", "5000", "-R",
    ]

    udp_res = adapter.call("throughput_test", {"duration_s": 4, "protocol": "udp", "bitrate_mbps": 15})
    assert udp_res["ok"] is True
    assert udp_res["throughput_mbps"] == pytest.approx(12.5)
    assert udp_res["jitter_ms"] == pytest.approx(0.412)
    assert udp_res["lost_packets"] == 5 and udp_res["total_packets"] == 4000
    assert udp_res["loss_percent"] == pytest.approx(0.125)
    assert "-u" in recorded_argvs[1] and "-b" in recorded_argvs[1] and "15M" in recorded_argvs[1]

    err_res = adapter.call("throughput_test", {"duration_s": 2})
    assert err_res["ok"] is False and err_res["error"] == "iperf3_failed"
    assert "Connection refused" in err_res["detail"]

    # 3 jobs spent -> 4th hits shared MAX_JOBS budget.
    assert adapter.call("throughput_test", {"duration_s": 1}) == {"ok": False, "error": "job_budget_exhausted"}


def test_throughput_test_missing_dn_ip_or_iperf3_and_bounds(monkeypatch, tmp_path):
    ctx, adapter = started_ue(monkeypatch, tmp_path)
    monkeypatch.delenv("REMOTERF_OAI_DN_IP", raising=False)
    res = adapter.call("throughput_test", {"duration_s": 5})
    assert res["ok"] is False and res["error"] == "dn_ip_missing"

    monkeypatch.setenv("REMOTERF_OAI_DN_IP", "not-an-ip")
    assert adapter.call("throughput_test", {})["error"] == "dn_ip_invalid"

    monkeypatch.setenv("REMOTERF_OAI_DN_IP", "192.168.70.135")
    monkeypatch.setenv("REMOTERF_OAI_IPERF3", "")
    assert adapter.call("throughput_test", {})["error"] == "iperf3_missing"

    assert adapter.call("throughput_test", {"duration_s": 61})["error"] == "invalid_parameters"
    assert adapter.call("start_traffic", {"duration_s": 601})["error"] == "invalid_parameters"
    assert len(ctx.processes.launches) == 1  # zero jobs spent


class AsyncProcesses(Processes):
    """Fake supervisor running finite jobs asynchronously via Popen."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._procs = {}
        self._outs = {}

    def launch(self, argv, **kwargs):
        self.launches.append((argv, kwargs))
        if kwargs.get("required_while_running"):
            return "owned-process"
        job_id = f"job-{len(self._procs)}"
        proc = subprocess.Popen(
            argv, env=kwargs["env"], cwd=kwargs["cwd"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._procs[job_id] = proc
        return job_id

    def snapshot(self):
        rows = [{"id": "owned-process", "returncode": None if self.alive else 1, "logs_drained": False}]
        for job_id, proc in self._procs.items():
            rc = proc.poll()
            if rc is not None and job_id not in self._outs:
                self._outs[job_id] = proc.communicate()
            rows.append({"id": job_id, "returncode": rc, "logs_drained": job_id in self._outs})
        return {"processes": rows}

    def log_tail(self, process_id, stream):
        if process_id in self._outs:
            return self._outs[process_id][0 if stream == "stdout" else 1]
        if process_id in self._procs:
            return b""
        return super().log_tail(process_id, stream)

    def stop(self, **kwargs):
        for proc in self._procs.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        return super().stop(**kwargs)


def test_start_and_stop_traffic_background_lifecycle(monkeypatch, tmp_path):
    ue_files(monkeypatch, tmp_path)
    monkeypatch.setattr(oai, "interface_ipv4", lambda name: "10.0.0.2")
    monkeypatch.setenv("REMOTERF_OAI_DN_IP", "192.168.70.135")
    ready_file = tmp_path / "iperf_ready"
    fake_iperf = _write_python_wrapper_script(
        tmp_path,
        "fake-iperf3",
        f"""
import json, signal, sys, time
from pathlib import Path
done = False
def on_sigint(signum, frame):
    global done
    done = True
signal.signal(signal.SIGINT, on_sigint)
Path({str(ready_file)!r}).write_text("ready")
deadline = time.monotonic() + 5.0
while not done and time.monotonic() < deadline:
    time.sleep(0.02)
print(json.dumps({{
    "error": "interrupt - the client has terminated",
    "end": {{
        "sum": {{
            "bytes": 5312500,
            "bits_per_second": 42500000.0,
            "jitter_ms": 0.25,
            "lost_packets": 2,
            "packets": 400,
            "lost_percent": 0.5,
            "seconds": 1.0,
        }}
    }}
}}))
sys.exit(1)
""",
    )
    monkeypatch.setenv("REMOTERF_OAI_IPERF3", str(fake_iperf))
    procs = AsyncProcesses()
    ctx = context(tmp_path, processes=procs)
    adapter = oai.OAI(ctx)
    adapter.start("ue", {})
    try:
        assert adapter.call("stop_traffic", {})["error"] == "traffic_not_running"

        started = adapter.call("start_traffic", {"duration_s": 120, "protocol": "udp", "bitrate_mbps": 50})
        assert started["ok"] is True and started["state"] == "running"
        assert started["duration_s"] == 120

        wait_dl = time.monotonic() + 5.0
        while not ready_file.exists() and time.monotonic() < wait_dl:
            time.sleep(0.02)
        assert ready_file.exists()

        # Second background traffic run while active is rejected.
        assert adapter.call("start_traffic", {"duration_s": 10})["error"] == "traffic_already_running"

        stopped = adapter.call("stop_traffic", {})
        assert stopped["ok"] is True and stopped["stopped"] is True
        assert stopped["throughput_mbps"] == pytest.approx(42.5)
        assert stopped["jitter_ms"] == pytest.approx(0.25)
        assert stopped["loss_percent"] == pytest.approx(0.5)
        assert object_data(stopped, max_bytes=48 * 1024)["stopped"] is True

        # Only one background run per operation.
        assert adapter.call("start_traffic", {"duration_s": 10})["error"] == "traffic_already_used"
        assert adapter.call("stop_traffic", {})["error"] == "traffic_not_running"

        # Interrupt error without an end summary still fails cleanly.
        metrics, err_code, _ = oai.parse_iperf3_json(
            json.dumps({"error": "interrupt - the client has terminated", "end": {}}),
            allow_interrupt=True,
        )
        assert metrics is None and err_code == "iperf3_failed"
    finally:
        adapter.stop()


def test_capture_air_pcap_gating_and_cap_net_raw(monkeypatch, tmp_path):
    ctx, adapter = started_ue(monkeypatch, tmp_path)
    monkeypatch.delenv("REMOTERF_OAI_ALLOW_PCAP", raising=False)
    assert adapter.call("capture_air_pcap", {})["error"] == "pcap_disabled"

    monkeypatch.setenv("REMOTERF_OAI_ALLOW_PCAP", "1")
    fake_tcpdump = tmp_path / "tcpdump"
    fake_tcpdump.write_text("", encoding="utf-8")
    monkeypatch.setenv("REMOTERF_OAI_TCPDUMP", str(fake_tcpdump))
    monkeypatch.setattr(oai, "has_cap_net_raw", lambda executable=None: False)
    assert adapter.call("capture_air_pcap", {})["error"] == "cap_net_raw_missing"

    assert len(ctx.processes.launches) == 1  # zero jobs spent when gated off
    assert oai._proc_status_has_cap("Name:\tpython\nCapEff:\t0000000000002000\n", oai.CAP_NET_RAW_BIT) is True
    assert oai._proc_status_has_cap("Name:\tpython\nCapEff:\t0000000000001000\n", oai.CAP_NET_RAW_BIT) is False


def test_capture_air_pcap_ue_and_gnb_publishes_pcap_artifact(monkeypatch, tmp_path):
    pcap_bytes = (
        struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 101)
        + struct.pack("<IIII", 1, 0, 4, 4)
        + b"\x45\x00\x00\x04"
    )
    seen_ifaces = tmp_path / "ifaces.txt"
    fake_tcpdump = _write_python_wrapper_script(
        tmp_path,
        "fake-tcpdump",
        f"""
import sys
from pathlib import Path
args = sys.argv[1:]
iface = args[args.index("-i") + 1]
out = args[args.index("-w") + 1]
with open({str(seen_ifaces)!r}, "a", encoding="utf-8") as fh:
    fh.write(iface + "\\n")
Path(out).write_bytes({pcap_bytes!r})
""",
    )
    monkeypatch.setenv("REMOTERF_OAI_ALLOW_PCAP", "1")
    monkeypatch.setenv("REMOTERF_OAI_TCPDUMP", str(fake_tcpdump))
    monkeypatch.setattr(oai, "has_cap_net_raw", lambda executable=None: True)

    # UE capture on oaitun_ue1.
    _, ue_adapter = started_ue(monkeypatch, tmp_path)
    assert ue_adapter.call("capture_air_pcap", {"duration_s": 61})["error"] == "invalid_parameters"
    assert ue_adapter.call("capture_air_pcap", {"max_bytes": oai.MAX_PCAP_BYTES + 1})["error"] == "invalid_parameters"

    ue_res = ue_adapter.call("capture_air_pcap", {"duration_s": 2, "max_bytes": 4096})
    assert ue_res["ok"] is True
    assert ue_res["artifact"] == {"name": "capture-1.pcap", "format": "pcap"}
    assert ue_res["interface"] == "oaitun_ue1"
    assert ue_res["bytes"] == len(pcap_bytes)
    assert (tmp_path / "artifacts" / "capture-1.pcap").read_bytes() == pcap_bytes
    assert not (tmp_path / ".capture-1.pcap.tmp").exists()

    # gNB capture requires REMOTERF_OAI_GNB_PCAP_IFNAME.
    gnb_dir = tmp_path / "gnb_work"
    gnb_dir.mkdir()
    _, gnb_adapter = started_gnb(monkeypatch, gnb_dir, conf_path=SAMPLES / "gnb-band41-1x1.conf")
    monkeypatch.delenv("REMOTERF_OAI_GNB_PCAP_IFNAME", raising=False)
    assert gnb_adapter.call("capture_air_pcap", {"duration_s": 2})["error"] == "gnb_pcap_ifname_missing"

    monkeypatch.setenv("REMOTERF_OAI_GNB_PCAP_IFNAME", "demo-oai")
    gnb_res = gnb_adapter.call("capture_air_pcap", {"duration_s": 2, "max_bytes": 4096})
    assert gnb_res["ok"] is True and gnb_res["interface"] == "demo-oai"
    assert gnb_res["capped"] is False
    assert (gnb_dir / "artifacts" / "capture-1.pcap").read_bytes() == pcap_bytes
    assert seen_ifaces.read_text(encoding="utf-8").splitlines() == ["oaitun_ue1", "demo-oai"]

    # Size-capped capture reports capped=True and respects shared job budget.
    big_pcap = pcap_bytes + b"\x00" * 1024
    big_tcpdump = _write_python_wrapper_script(
        tmp_path,
        "fake-tcpdump-big",
        f"""
import sys
from pathlib import Path
args = sys.argv[1:]
Path(args[args.index("-w") + 1]).write_bytes({big_pcap!r})
""",
    )
    monkeypatch.setenv("REMOTERF_OAI_TCPDUMP", str(big_tcpdump))
    cap_res = gnb_adapter.call("capture_air_pcap", {"duration_s": 2, "max_bytes": 1024})
    assert cap_res["ok"] is True and cap_res["capped"] is True

    # Runtime tcpdump permission error maps to cap_net_raw_missing.
    denied_tcpdump = _write_python_wrapper_script(
        tmp_path,
        "fake-tcpdump-denied",
        "import sys; sys.stderr.write('tcpdump: demo-oai: You don\\'t have permission to capture (Operation not permitted)\\n'); sys.exit(1)\n",
    )
    monkeypatch.setenv("REMOTERF_OAI_TCPDUMP", str(denied_tcpdump))
    denied = gnb_adapter.call("capture_air_pcap", {"duration_s": 2, "max_bytes": 4096})
    assert denied["ok"] is False and denied["error"] == "cap_net_raw_missing"
    # 3 jobs spent on gnb_adapter -> 4th hits shared MAX_JOBS budget.
    assert gnb_adapter.call("capture_air_pcap", {"duration_s": 2}) == {"ok": False, "error": "job_budget_exhausted"}


def test_pcap_artifact_decodes_through_client(monkeypatch, tmp_path):
    integrations = pytest.importorskip("remoterf.integrations")
    pcap_bytes = (
        struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 101)
        + struct.pack("<IIII", 1, 0, 4, 4)
        + b"\x45\x00\x00\x04"
    )
    fake_tcpdump = _write_python_wrapper_script(
        tmp_path,
        "fake-tcpdump-client",
        f"""
import sys
from pathlib import Path
args = sys.argv[1:]
Path(args[args.index("-w") + 1]).write_bytes({pcap_bytes!r})
""",
    )
    monkeypatch.setenv("REMOTERF_OAI_ALLOW_PCAP", "1")
    monkeypatch.setenv("REMOTERF_OAI_TCPDUMP", str(fake_tcpdump))
    monkeypatch.setattr(oai, "has_cap_net_raw", lambda executable=None: True)
    _, ue_adapter = started_ue(monkeypatch, tmp_path)
    ue_res = ue_adapter.call("capture_air_pcap", {"duration_s": 2, "max_bytes": 4096})
    app = integrations.App(None, "org.remoterf.oai", "ue")
    app.__dict__["fetch"] = lambda name: (tmp_path / "artifacts" / name).read_bytes()
    decoded = app._decode(ue_res)
    assert decoded["artifact"] == pcap_bytes


class FakeTelnetServer:
    """Minimal local socket server emulating OAI's telnetsrv prompt/reply loop."""

    def __init__(self, monkeypatch, *, reply_fn=None, hang=False, port=19090,
                 banner=b"softmodem_gnb> ", close_after=None):
        self.reply_fn = reply_fn or (lambda cmd: "OK")
        self.hang = hang
        self.port = port
        self.banner = banner
        self.close_after = close_after
        self.commands = []
        self.connections = 0
        self.connect_calls = []
        self._monkeypatch = monkeypatch
        self._mp_ctx = None
        self._stop = Event()
        self._clients = []
        self._threads = []

    def _connect(self, address, timeout=None, *args, **kwargs):
        host, port = address
        self.connect_calls.append((host, port, timeout))
        if port != self.port or self._stop.is_set():
            raise ConnectionRefusedError("connection refused")
        client_sock, server_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        if timeout is not None:
            client_sock.settimeout(timeout)
        self.connections += 1
        self._clients.append(server_sock)
        t = threading.Thread(target=self._handle, args=(server_sock,), daemon=True)
        self._threads.append(t)
        t.start()
        return client_sock

    def __enter__(self):
        self._mp_ctx = self._monkeypatch.context()
        mp = self._mp_ctx.__enter__()
        mp.setattr(oai.telnet.socket, "create_connection", self._connect)
        return self

    def __exit__(self, *exc):
        self._stop.set()
        for conn in self._clients:
            try:
                conn.close()
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=2.0)
        if self._mp_ctx is not None:
            self._mp_ctx.__exit__(*exc)
            self._mp_ctx = None

    def _handle(self, conn):
        handled = 0
        try:
            conn.settimeout(0.2)
            if self.banner:
                conn.sendall(self.banner)
            buf = b""
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = line.decode("utf-8", errors="replace").strip()
                    self.commands.append(cmd)
                    if self.hang:
                        self._stop.wait(5.0)
                        return
                    reply = self.reply_fn(cmd)
                    if isinstance(reply, bytes):
                        conn.sendall(reply + b"\r\nsoftmodem_gnb> ")
                    else:
                        conn.sendall(f"{reply}\r\nsoftmodem_gnb> ".encode("utf-8"))
                    handled += 1
                    if self.close_after is not None and handled >= self.close_after:
                        return
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def test_set_live_control_requires_telnetsrv_arg(monkeypatch, tmp_path):
    _, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf")
    res = adapter.call("set_live_control", {"mcs_range": [4, 20]})
    assert res["ok"] is False and res["error"] == "telnet_not_enabled"

    # Non-loopback listenaddr and invalid port are rejected cleanly.
    monkeypatch.setenv("REMOTERF_OAI_GNB_ARGS", "--telnetsrv --telnetsrv.listenaddr 192.168.1.50")
    assert adapter.call("set_live_control", {"max_ldpc_iterations": 8})["error"] == "telnet_non_local"
    monkeypatch.setenv("REMOTERF_OAI_GNB_ARGS", "--telnetsrv --telnetsrv.listenport 99999")
    assert adapter.call("set_live_control", {"max_ldpc_iterations": 8})["error"] == "telnet_invalid_port"


def test_set_live_control_applies_valid_controls_over_persistent_socket(monkeypatch, tmp_path):
    with FakeTelnetServer(monkeypatch, banner=b"\xff\xfb\x01softmodem_gnb> ") as srv:
        args = f"--continuous-tx --telnetsrv --telnetsrv.listenaddr 127.0.0.1 --telnetsrv.listenport {srv.port}"
        ctx, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)

        res1 = adapter.call("set_live_control", {
            "mcs_range": [4, 20],
            "ul_prb_blacklist": [10, 2, 51],
            "dl_prb_blacklist": [],
            "max_ldpc_iterations": 8,
            "ri": 1,
            "tpmi": 0,
        })
        assert res1["ok"] is True
        assert res1["applied"] == {
            "mcs_range": [4, 20],
            "ul_prb_blacklist": [2, 10, 51],
            "dl_prb_blacklist": [],
            "max_ldpc_iterations": 8,
            "ri": 1,
            "tpmi": 0,
        }
        assert srv.commands == [
            "mac dl_min_mcs 4",
            "mac dl_max_mcs 20",
            "mac ul_min_mcs 4",
            "mac ul_max_mcs 20",
            "mac ul_prb_blacklist 2,10,51",
            "mac dl_prb_blacklist none",
            "phy max_ldpc_iterations 8",
            "mac ri 1",
            "mac tpmi 0",
        ]

        res2 = adapter.call("set_live_control", {"max_ldpc_iterations": 6})
        assert res2["ok"] is True
        assert srv.connections == 1  # persistent local socket reused
        assert srv.connect_calls == [("127.0.0.1", srv.port, oai.TELNET_TIMEOUT_S)]
        assert len(ctx.processes.launches) == 1  # zero jobs spent
        assert object_data(res1, max_bytes=48 * 1024)["ok"] is True
        adapter.stop()


def test_set_live_control_reconnects_once_on_stale_socket(monkeypatch, tmp_path):
    with FakeTelnetServer(monkeypatch, close_after=1) as srv:
        args = f"--continuous-tx --telnetsrv --telnetsrv.listenport {srv.port}"
        _, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)
        assert adapter.call("set_live_control", {"max_ldpc_iterations": 8})["ok"] is True
        # Wait for the server thread to close the idle first connection.
        srv._threads[0].join(timeout=2.0)
        assert adapter.call("set_live_control", {"max_ldpc_iterations": 6})["ok"] is True
        assert srv.connections == 2
        adapter.stop()


@pytest.mark.parametrize("bad_params", [
    {},
    {"mcs_range": [-1, 10]},
    {"mcs_range": [10, 29]},
    {"mcs_range": [20, 10]},
    {"mcs_range": [True, 10]},
    {"ul_prb_blacklist": [52]},
    {"ul_prb_blacklist": [-1]},
    {"ul_prb_blacklist": [1, 1]},
    {"ul_prb_blacklist": list(range(52))},
    {"dl_prb_blacklist": [52]},
    {"max_ldpc_iterations": 0},
    {"max_ldpc_iterations": 21},
    {"max_ldpc_iterations": True},
    {"ri": 0},
    {"ri": 2},
    {"tpmi": -1},
    {"tpmi": 1},
    {"frequency": 2_500_000_000},
    {"bandwidth": 20},
    {"tx_power": 10},
    {"att_tx": 5},
    {"if_freq": 915_000_000},
])
def test_set_live_control_rejects_bounds_and_rf_params(monkeypatch, tmp_path, bad_params):
    with FakeTelnetServer(monkeypatch) as srv:
        args = f"--continuous-tx --telnetsrv --telnetsrv.listenport {srv.port}"
        _, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)
        res = adapter.call("set_live_control", bad_params)
        assert res["ok"] is False and res["error"] == "invalid_parameters"
        assert srv.commands == []
        assert adapter.status()["ready"] is True


def test_set_live_control_2x2_mimo_allows_rank2_and_validates_tpmi(monkeypatch, tmp_path):
    with FakeTelnetServer(monkeypatch) as srv:
        args = (
            f"--continuous-tx --telnetsrv --telnetsrv.listenport {srv.port} "
            "--gNBs.[0].pdsch_AntennaPorts_XP 2 --gNBs.[0].pusch_AntennaPorts 2 "
            "--RUs.[0].nb_tx 2 --RUs.[0].nb_rx 2"
        )
        _, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)
        assert adapter.call("set_live_control", {"ri": 2, "tpmi": 2})["ok"] is True
        assert adapter.call("set_live_control", {"ri": 2, "tpmi": 3})["error"] == "invalid_parameters"
        assert adapter.call("set_live_control", {"ri": 3})["error"] == "invalid_parameters"
        adapter.stop()

    with FakeTelnetServer(monkeypatch) as srv:
        args = (
            f"--continuous-tx --telnetsrv --telnetsrv.listenport {srv.port} "
            "--gNBs.[0].pdsch_AntennaPorts_XP 2 --gNBs.[0].pdsch_AntennaPorts_N1 2 "
            "--gNBs.[0].pusch_AntennaPorts 4 --RUs.[0].nb_tx 4 --RUs.[0].nb_rx 4"
        )
        mimo4_dir = tmp_path / "mimo4"
        mimo4_dir.mkdir()
        _, adapter = started_gnb(monkeypatch, mimo4_dir, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)
        assert adapter.call("set_live_control", {"ri": 4, "tpmi": 4})["ok"] is True
        assert adapter.call("set_live_control", {"ri": 4, "tpmi": 5})["error"] == "invalid_parameters"
        adapter.stop()


def test_set_live_control_handles_rejection_timeout_and_unreachable(monkeypatch, tmp_path):
    with FakeTelnetServer(monkeypatch, reply_fn=lambda cmd: "ERROR: unknown command") as srv:
        args = f"--continuous-tx --telnetsrv --telnetsrv.listenport {srv.port}"
        _, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)
        rej = adapter.call("set_live_control", {"max_ldpc_iterations": 8})
        assert rej["ok"] is False and rej["error"] == "telnet_command_rejected"
        assert adapter.status()["ready"] is True
        adapter.stop()

    monkeypatch.setattr(oai, "TELNET_TIMEOUT_S", 0.2)
    with FakeTelnetServer(monkeypatch, hang=True) as srv:
        args = f"--continuous-tx --telnetsrv --telnetsrv.listenport {srv.port}"
        hang_dir = tmp_path / "hang"
        hang_dir.mkdir()
        _, adapter = started_gnb(monkeypatch, hang_dir, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)
        t0 = time.monotonic()
        timed = adapter.call("set_live_control", {"max_ldpc_iterations": 8})
        assert timed["ok"] is False and timed["error"] == "telnet_timeout"
        assert time.monotonic() - t0 < 2.0
        assert adapter.status()["ready"] is True
        adapter.stop()

    # Reserve and immediately close a loopback port so connect fails immediately.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    down_dir = tmp_path / "down"
    down_dir.mkdir()
    args = f"--continuous-tx --telnetsrv --telnetsrv.listenport {closed_port}"
    _, adapter = started_gnb(monkeypatch, down_dir, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)
    down = adapter.call("set_live_control", {"max_ldpc_iterations": 8})
    assert down["ok"] is False and down["error"] == "telnet_unreachable"
    assert adapter.status()["ready"] is True
    adapter.stop()


@pytest.mark.parametrize("profile,action,params", [
    ("gnb", "get_cell_config", {"unexpected": 1}),
    ("gnb", "get_link_stats", {}),
    ("gnb", "get_ue_mac_table", {}),
    ("gnb", "read_ofdm_grid", {}),
    ("gnb", "read_constellation", {}),
    ("gnb", "throughput_test", {}),
    ("ue", "throughput_test", {}),
    ("ue", "start_traffic", {}),
    ("ue", "stop_traffic", {}),
    ("gnb", "capture_air_pcap", {}),
    ("ue", "capture_air_pcap", {}),
    ("gnb", "set_live_control", {"mcs_range": [4, 20]}),
    ("ue", "set_live_control", {"mcs_range": [4, 20]}),
    ("gnb", "ping", {}),
    ("gnb", "read_csi", {"stream": "ul_pusch"}),
])
def test_every_action_error_never_tears_down_cell(monkeypatch, tmp_path, profile, action, params):
    monkeypatch.delenv("REMOTERF_OAI_DN_IP", raising=False)
    monkeypatch.delenv("REMOTERF_OAI_ALLOW_PCAP", raising=False)
    if profile == "gnb":
        ctx, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf")
    else:
        ctx, adapter = started_ue(monkeypatch, tmp_path)

    result = adapter.call(action, params)
    assert isinstance(result, dict) and result.get("ok") is False
    assert isinstance(result.get("error"), str) and result["error"]
    assert object_data(result, max_bytes=48 * 1024)["ok"] is False
    assert adapter.status()["ready"] is True
    assert ctx.processes.alive is True and ctx.processes.stop_calls == []


def test_runtime_keeps_cell_running_on_expansion_action_errors(monkeypatch, tmp_path):
    operator_files(monkeypatch, tmp_path)
    monkeypatch.setenv("REMOTERF_OAI_GNB_CONF", str(SAMPLES / "gnb-band41-1x1.conf"))
    monkeypatch.delenv("REMOTERF_OAI_GNB_ARGS", raising=False)
    monkeypatch.delenv("REMOTERF_OAI_ALLOW_PCAP", raising=False)
    monkeypatch.delenv("REMOTERF_OAI_DN_IP", raising=False)

    journal = Journal(tmp_path / "journal")
    gate = Gate("phy-abc123", journal)
    manifest = Manifest.from_file(Path(oai.__file__).with_name("integration.json"))
    registry = Registry([manifest], enabled={"dev-1": ["org.remoterf.oai"]})
    runtime = Runtime(registry, timeout=15.0)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "artifacts").mkdir()
    procs = Processes()
    binding = Binding(
        gate=gate,
        device_id="dev-1",
        device_type="usrp",
        session_id="sess-1",
        generation=1,
        work_dir=work_dir,
        authority=lambda: True,
        release_native=lambda: None,
        native_released=lambda: True,
        restore_native=lambda: None,
        resources_released=lambda: not procs.alive,
        selector={"serial": "ABC123"},
        processes=procs,
    )

    def wait_until(fn, deadline_s=5.0):
        dl = time.monotonic() + deadline_s
        while time.monotonic() < dl:
            val = fn()
            if val:
                return val
            time.sleep(0.02)
        raise AssertionError("timed out waiting for runtime state")

    def running_snap():
        snap = runtime.status(binding, op_id)
        return snap if snap["state"] != "STARTING" else None

    def done_action(aid):
        snap = runtime.action_status(binding, op_id, aid)
        return snap if snap["state"] in {"SUCCEEDED", "ERROR", "CANCELLED"} else None

    op = runtime.start(binding, "org.remoterf.oai", "gnb", {}, request_key="start-1")
    op_id = op["operation_id"]
    try:
        snap = wait_until(running_snap)
        assert snap["state"] == "RUNNING"

        failing_calls = [
            ("get_cell_config", {"bad": 1}),
            ("get_link_stats", {}),
            ("get_ue_mac_table", {}),
            ("read_ofdm_grid", {}),
            ("read_constellation", {}),
            ("throughput_test", {}),
            ("start_traffic", {}),
            ("stop_traffic", {}),
            ("capture_air_pcap", {}),
            ("set_live_control", {"mcs_range": [4, 20]}),
        ]
        for idx, (action, params) in enumerate(failing_calls):
            rec = runtime.call(binding, op_id, action, params, request_key=f"err-{idx}")
            done = wait_until(lambda aid=rec["action_id"]: done_action(aid))
            assert done["state"] == "SUCCEEDED"
            assert done["result"]["ok"] is False
            assert runtime.status(binding, op_id)["state"] == "RUNNING"
    finally:
        runtime.stop(binding, op_id)
        wait_until(lambda: runtime.status(binding, op_id)["released"])
        runtime.close()
        journal.close()


def test_csi_burst_survives_a_corrupt_write_seq_and_payload_stays_valid(tmp_path):
    from remoterf_device_core.integrations.payload import object_data

    ring = Ring(tmp_path)
    ring.write(1)
    timer = threading.Timer(0.05, ring._header, args=(oai.CSI_MAGIC, oai.CSI_VERSION, (1 << 64) - 1))
    timer.start()
    try:
        result = oai.OAI(context(tmp_path)).call(
            "capture_csi_burst", {"stream": "ul_pusch", "count": 2, "timeout_s": 1})
    finally:
        timer.join()
    assert result["ok"] is False and result["dropped"] <= oai.CSI_MAX_DROPPED
    object_data(result, max_bytes=48 * 1024)  # what the runtime does; raising here tears the cell down


def test_csi_burst_stops_when_the_ring_shrinks(tmp_path):
    ring = Ring(tmp_path)
    ring.write(1)
    timer = threading.Timer(0.05, lambda: open(ring.path, "r+b").truncate(oai.CSI_HEADER_BYTES))
    timer.start()
    try:
        result = oai.OAI(context(tmp_path)).call(
            "capture_csi_burst", {"stream": "ul_pusch", "count": 2, "timeout_s": 1})
    finally:
        timer.join()
    assert result["ok"] is False


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_file(path, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return path.exists()


@pytest.mark.parametrize("trigger", ["timeout", "cancelled"])
def test_capture_air_pcap_stops_process_before_unlinking_tmp_pcap(monkeypatch, tmp_path, trigger):
    pid_file = tmp_path / "tcpdump.pid"
    race_file = tmp_path / "unlinked_while_alive"
    fake_tcpdump = _write_python_wrapper_script(
        tmp_path,
        "fake-tcpdump-slow",
        f"""
import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
args = sys.argv[1:]
out = Path(args[args.index("-w") + 1])
out.write_bytes(b"pcap-header")
Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")
deadline = time.monotonic() + 10.0
while time.monotonic() < deadline:
    if not out.exists():
        Path({str(race_file)!r}).write_text("unlinked", encoding="utf-8")
        break
    time.sleep(0.02)
""",
    )
    monkeypatch.setenv("REMOTERF_OAI_ALLOW_PCAP", "1")
    monkeypatch.setenv("REMOTERF_OAI_TCPDUMP", str(fake_tcpdump))
    monkeypatch.setattr(oai, "has_cap_net_raw", lambda executable=None: True)
    ue_files(monkeypatch, tmp_path)
    monkeypatch.setattr(oai, "interface_ipv4", lambda name: "10.0.0.2")
    procs = AsyncProcesses()
    ctx = context(tmp_path, processes=procs)
    adapter = oai.OAI(ctx)
    adapter.start("ue", {})

    cancel_thread = None
    if trigger == "timeout":
        monkeypatch.setattr(oai, "PCAP_GRACE_S", 0.0)
        duration_s = 1
    else:
        duration_s = 5

        def trigger_cancel():
            if _wait_for_file(pid_file):
                ctx.cancelled.set()

        cancel_thread = threading.Thread(target=trigger_cancel, daemon=True)
        cancel_thread.start()

    try:
        res = adapter.call("capture_air_pcap", {"duration_s": duration_s, "max_bytes": 4096})
        if cancel_thread is not None:
            cancel_thread.join(timeout=5.0)
        assert res == {"ok": False, "error": trigger}
        assert _wait_for_file(pid_file, timeout_s=1.0)
        tcpdump_pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert procs._procs["job-0"].poll() is not None
        assert not _pid_alive(tcpdump_pid)
        assert not race_file.exists()
        assert not (tmp_path / ".capture-1.pcap.tmp").exists()
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text(encoding="utf-8").strip()), 9)
            except OSError:
                pass
        adapter.stop()


def test_capture_air_pcap_keeps_tmp_pcap_if_process_does_not_exit(monkeypatch, tmp_path):
    fake_tcpdump = tmp_path / "tcpdump"
    fake_tcpdump.write_text("", encoding="utf-8")
    monkeypatch.setenv("REMOTERF_OAI_ALLOW_PCAP", "1")
    monkeypatch.setenv("REMOTERF_OAI_TCPDUMP", str(fake_tcpdump))
    monkeypatch.setattr(oai, "has_cap_net_raw", lambda executable=None: True)
    monkeypatch.setattr(oai, "FINITE_JOB_STOP_WAIT_S", 0.05, raising=False)
    ue_files(monkeypatch, tmp_path)
    monkeypatch.setattr(oai, "interface_ipv4", lambda name: "10.0.0.2")
    procs = Processes()
    ctx = context(tmp_path, processes=procs)
    tmp_pcap = tmp_path / ".capture-1.pcap.tmp"

    def stuck_launch(argv, **kwargs):
        procs.launches.append((argv, kwargs))
        if kwargs.get("required_while_running"):
            return "owned-process"
        tmp_pcap.write_bytes(b"live-capture")
        ctx.cancelled.set()
        return "stuck-job"

    procs.launch = stuck_launch
    procs.snapshot = lambda: {
        "processes": [
            {"id": "owned-process", "returncode": None, "logs_drained": False},
            {"id": "stuck-job", "returncode": None, "logs_drained": False},
        ]
    }
    adapter = oai.OAI(ctx)
    adapter.start("ue", {})

    res = adapter.call("capture_air_pcap", {"duration_s": 2, "max_bytes": 4096})
    assert res == {"ok": False, "error": "cancelled"}
    assert tmp_pcap.exists()


def test_capture_air_pcap_rejects_duration_exceeding_action_timeout_bound(monkeypatch, tmp_path):
    fake_tcpdump = tmp_path / "tcpdump"
    fake_tcpdump.write_text("", encoding="utf-8")
    monkeypatch.setenv("REMOTERF_OAI_ALLOW_PCAP", "1")
    monkeypatch.setenv("REMOTERF_OAI_TCPDUMP", str(fake_tcpdump))
    monkeypatch.setattr(oai, "has_cap_net_raw", lambda executable=None: True)
    ctx, adapter = started_ue(monkeypatch, tmp_path)
    # Default action_timeout_s=30 -> bound=25.0, PCAP_GRACE_S=5.0 -> max duration_s=20.
    for duration_s in (21, 60):
        res = adapter.call("capture_air_pcap", {"duration_s": duration_s})
        assert res["ok"] is False and res["error"] == "invalid_parameters"
    assert len(ctx.processes.launches) == 1


@pytest.mark.parametrize("trigger", ["timeout", "cancelled"])
def test_throughput_test_stops_process_on_timeout_and_cancel(monkeypatch, tmp_path, trigger):
    pid_file = tmp_path / "iperf3.pid"
    ignore_sigterm = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if trigger == "timeout" else ""
    fake_iperf = _write_python_wrapper_script(
        tmp_path,
        "fake-iperf3-slow",
        f"""
import os, signal, time
from pathlib import Path
{ignore_sigterm}Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(10.0)
""",
    )
    monkeypatch.setenv("REMOTERF_OAI_DN_IP", "192.168.70.135")
    monkeypatch.setenv("REMOTERF_OAI_IPERF3", str(fake_iperf))
    ue_files(monkeypatch, tmp_path)
    monkeypatch.setattr(oai, "interface_ipv4", lambda name: "10.0.0.2")
    procs = AsyncProcesses()
    ctx = context(tmp_path, processes=procs)
    adapter = oai.OAI(ctx)
    adapter.start("ue", {})

    cancel_thread = None
    if trigger == "timeout":
        monkeypatch.setattr(oai, "TRAFFIC_GRACE_S", -0.5)
        duration_s = 1
    else:
        duration_s = 5

        def trigger_cancel():
            if _wait_for_file(pid_file):
                ctx.cancelled.set()

        cancel_thread = threading.Thread(target=trigger_cancel, daemon=True)
        cancel_thread.start()

    try:
        res = adapter.call("throughput_test", {"duration_s": duration_s})
        if cancel_thread is not None:
            cancel_thread.join(timeout=5.0)
        assert res == {"ok": False, "error": trigger}
        assert _wait_for_file(pid_file, timeout_s=1.0)
        iperf_pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert procs._procs["job-0"].poll() is not None
        assert not _pid_alive(iperf_pid)
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text(encoding="utf-8").strip()), 9)
            except OSError:
                pass
        adapter.stop()


def test_throughput_test_rejects_duration_exceeding_action_timeout_bound(monkeypatch, tmp_path):
    fake_iperf = tmp_path / "iperf3"
    fake_iperf.write_text("", encoding="utf-8")
    monkeypatch.setenv("REMOTERF_OAI_DN_IP", "192.168.70.135")
    monkeypatch.setenv("REMOTERF_OAI_IPERF3", str(fake_iperf))
    ctx, adapter = started_ue(monkeypatch, tmp_path)
    # Default action_timeout_s=30 -> bound=25.0, TRAFFIC_GRACE_S=10.0 -> max duration_s=15.
    for duration_s in (16, 60):
        res = adapter.call("throughput_test", {"duration_s": duration_s})
        assert res["ok"] is False and res["error"] == "invalid_parameters"
    assert len(ctx.processes.launches) == 1


def test_iperf_rejects_student_port_and_uses_operator_env_port(monkeypatch, tmp_path):
    argv_file = tmp_path / "iperf_argv.json"
    fake_iperf = _write_python_wrapper_script(
        tmp_path,
        "fake-iperf3-port",
        f"""
import json, sys
from pathlib import Path
Path({str(argv_file)!r}).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
print(json.dumps({{"end": {{"sum_received": {{"bytes": 1000, "bits_per_second": 8000000.0, "seconds": 1.0}}}}}}))
""",
    )
    monkeypatch.setenv("REMOTERF_OAI_DN_IP", "192.168.70.135")
    monkeypatch.setenv("REMOTERF_OAI_IPERF3", str(fake_iperf))
    monkeypatch.setenv("REMOTERF_OAI_IPERF3_PORT", "6201")
    ctx, adapter = started_ue(monkeypatch, tmp_path)

    for action in ("throughput_test", "start_traffic"):
        res = adapter.call(action, {"duration_s": 1, "port": 22})
        assert res["ok"] is False and res["error"] == "invalid_parameters"
    assert len(ctx.processes.launches) == 1

    ok_res = adapter.call("throughput_test", {"duration_s": 1})
    assert ok_res["ok"] is True
    seen = json.loads(argv_file.read_text(encoding="utf-8"))
    assert seen[seen.index("-p") + 1] == "6201"

    monkeypatch.setenv("REMOTERF_OAI_IPERF3_PORT", "70000")
    bad_cfg = adapter.call("throughput_test", {"duration_s": 1})
    assert bad_cfg["ok"] is False and bad_cfg["error"] == "invalid_configuration"


@pytest.mark.parametrize("action,params", [
    ("throughput_test", {"direction": []}),
    ("throughput_test", {"protocol": {}}),
    ("start_traffic", {"direction": {}}),
    ("start_traffic", {"protocol": []}),
])
def test_iperf_rejects_unhashable_direction_and_protocol_as_invalid_parameters(
    monkeypatch, tmp_path, action, params
):
    ctx, adapter = started_ue(monkeypatch, tmp_path)
    res = adapter.call(action, params)
    assert res["ok"] is False and res["error"] == "invalid_parameters"
    assert len(ctx.processes.launches) == 1


def test_set_live_control_detects_usage_rejection(monkeypatch, tmp_path):
    with FakeTelnetServer(monkeypatch, reply_fn=lambda cmd: "Usage: mac ri <val>") as srv:
        args = f"--continuous-tx --telnetsrv --telnetsrv.listenport {srv.port}"
        _, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)
        res = adapter.call("set_live_control", {"ri": 1})
        assert res["ok"] is False and res["error"] == "telnet_command_rejected"
        adapter.stop()


def test_set_live_control_eof_after_echo_returns_telnet_disconnected(monkeypatch, tmp_path):
    server_socks = []
    threads = []

    def fake_connect(address, timeout=None, *args, **kwargs):
        client_sock, server_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        if timeout is not None:
            client_sock.settimeout(timeout)
        server_socks.append(server_sock)

        def handle():
            try:
                server_sock.settimeout(2.0)
                server_sock.sendall(b"softmodem_gnb> ")
                data = server_sock.recv(4096)
                if data:
                    cmd = data.decode("utf-8", errors="replace").strip()
                    # Echo command and close without sending a prompt.
                    server_sock.sendall((cmd + "\r\n").encode("utf-8"))
            except OSError:
                pass
            finally:
                server_sock.close()

        t = threading.Thread(target=handle, daemon=True)
        threads.append(t)
        t.start()
        return client_sock

    monkeypatch.setattr(oai.telnet.socket, "create_connection", fake_connect)
    args = "--continuous-tx --telnetsrv --telnetsrv.listenport 19091"
    _, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)
    try:
        res = adapter.call("set_live_control", {"max_ldpc_iterations": 8})
        assert res["ok"] is False and res["error"] == "telnet_disconnected"
    finally:
        for sock in server_socks:
            try:
                sock.close()
            except OSError:
                pass
        for t in threads:
            t.join(timeout=2.0)
        adapter.stop()


def test_set_live_control_rank_uses_min_of_ru_and_pdsch_ports(monkeypatch, tmp_path):
    with FakeTelnetServer(monkeypatch) as srv:
        args = (
            f"--continuous-tx --telnetsrv --telnetsrv.listenport {srv.port} "
            "--gNBs.[0].pdsch_AntennaPorts_XP 1 --RUs.[0].nb_tx 2 --RUs.[0].nb_rx 2"
        )
        _, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)
        rejected = adapter.call("set_live_control", {"ri": 2})
        assert rejected["ok"] is False and rejected["error"] == "invalid_parameters"
        assert srv.commands == []
        allowed = adapter.call("set_live_control", {"ri": 1})
        assert allowed["ok"] is True
        adapter.stop()


def test_set_live_control_tracks_ri_and_enforces_tpmi_across_calls(monkeypatch, tmp_path):
    with FakeTelnetServer(monkeypatch) as srv:
        args = (
            f"--continuous-tx --telnetsrv --telnetsrv.listenport {srv.port} "
            "--gNBs.[0].pdsch_AntennaPorts_XP 2 --gNBs.[0].pusch_AntennaPorts 2 "
            "--RUs.[0].nb_tx 2 --RUs.[0].nb_rx 2"
        )
        _, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)
        # Before ri is set (rank unknown), permissive 2-port max (5) applies to tpmi.
        assert adapter.call("set_live_control", {"tpmi": 5})["ok"] is True
        assert adapter.call("set_live_control", {"tpmi": 6})["error"] == "invalid_parameters"

        # Setting ri=2 then tpmi=5 in a separate call must enforce rank-2 max tpmi=2.
        assert adapter.call("set_live_control", {"ri": 2})["ok"] is True
        sent_before = len(srv.commands)
        assert adapter.call("set_live_control", {"tpmi": 5})["error"] == "invalid_parameters"
        assert len(srv.commands) == sent_before
        assert adapter.call("set_live_control", {"tpmi": 2})["ok"] is True

        # Setting ri=1 allows rank-1 tpmi=5 on a subsequent call; switching back to ri=2 tightens again.
        assert adapter.call("set_live_control", {"ri": 1})["ok"] is True
        assert adapter.call("set_live_control", {"tpmi": 5})["ok"] is True
        assert adapter.call("set_live_control", {"ri": 2})["ok"] is True
        assert adapter.call("set_live_control", {"tpmi": 3})["error"] == "invalid_parameters"
        adapter.stop()
        assert adapter._live_ri is None


def test_set_live_control_4port_unknown_ri_uses_permissive_tpmi_bound(monkeypatch, tmp_path):
    with FakeTelnetServer(monkeypatch) as srv:
        args = (
            f"--continuous-tx --telnetsrv --telnetsrv.listenport {srv.port} "
            "--gNBs.[0].pdsch_AntennaPorts_XP 2 --gNBs.[0].pdsch_AntennaPorts_N1 2 "
            "--gNBs.[0].pusch_AntennaPorts 4 --RUs.[0].nb_tx 4 --RUs.[0].nb_rx 4"
        )
        _, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)
        # With ri unknown on a 4-port cell, tpmi alone is accepted up to the old maximum (27).
        assert adapter.call("set_live_control", {"tpmi": 10})["ok"] is True
        assert adapter.call("set_live_control", {"tpmi": 27})["ok"] is True
        assert adapter.call("set_live_control", {"tpmi": 28})["error"] == "invalid_parameters"
        assert "mac tpmi 28" not in srv.commands

        # Once ri is confirmed, the tight per-rank bound applies across calls.
        assert adapter.call("set_live_control", {"ri": 4})["ok"] is True
        assert adapter.call("set_live_control", {"tpmi": 5})["error"] == "invalid_parameters"
        assert adapter.call("set_live_control", {"tpmi": 4})["ok"] is True
        assert adapter.call("set_live_control", {"ri": 2})["ok"] is True
        assert adapter.call("set_live_control", {"tpmi": 22})["error"] == "invalid_parameters"
        assert adapter.call("set_live_control", {"tpmi": 21})["ok"] is True
        adapter.stop()
        assert adapter._live_ri is None


def test_set_live_control_partial_failure_after_ri_clears_tracker(monkeypatch, tmp_path):
    fail_tpmi = {"active": False}

    def reply_fn(cmd):
        if fail_tpmi["active"] and cmd.startswith("mac tpmi "):
            return "ERROR: failed to apply tpmi"
        return "OK"

    with FakeTelnetServer(monkeypatch, reply_fn=reply_fn) as srv:
        args = (
            f"--continuous-tx --telnetsrv --telnetsrv.listenport {srv.port} "
            "--gNBs.[0].pdsch_AntennaPorts_XP 2 --gNBs.[0].pusch_AntennaPorts 2 "
            "--RUs.[0].nb_tx 2 --RUs.[0].nb_rx 2"
        )
        _, adapter = started_gnb(monkeypatch, tmp_path, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)

        # Confirm ri=2 so tight rank-2 bound (2) is active.
        assert adapter.call("set_live_control", {"ri": 2})["ok"] is True
        assert adapter._live_ri == 2
        assert adapter.call("set_live_control", {"tpmi": 5})["error"] == "invalid_parameters"

        # Validation error before telnet execution keeps the tracked ri intact.
        assert adapter.call("set_live_control", {"ri": 2, "tpmi": 3})["error"] == "invalid_parameters"
        assert adapter._live_ri == 2

        # A partial failure after "mac ri" clears the tracker to None instead of guessing.
        fail_tpmi["active"] = True
        partial = adapter.call("set_live_control", {"ri": 1, "tpmi": 5})
        assert partial["ok"] is False and partial["error"] == "telnet_command_rejected"
        assert "mac ri 1" in srv.commands
        assert adapter._live_ri is None

        # With tracker cleared (rank unknown), permissive 2-port max (5) applies again.
        fail_tpmi["active"] = False
        assert adapter.call("set_live_control", {"tpmi": 5})["ok"] is True
        assert adapter._live_ri is None

        # Same when moving ri=1 -> ri=2 and "mac tpmi" fails after "mac ri 2": tracker clears to None.
        assert adapter.call("set_live_control", {"ri": 1})["ok"] is True
        assert adapter._live_ri == 1
        fail_tpmi["active"] = True
        partial2 = adapter.call("set_live_control", {"ri": 2, "tpmi": 2})
        assert partial2["ok"] is False and partial2["error"] == "telnet_command_rejected"
        assert "mac ri 2" in srv.commands
        assert adapter._live_ri is None

        # A subsequent confirmed ri restores tight tracking.
        fail_tpmi["active"] = False
        assert adapter.call("set_live_control", {"ri": 2})["ok"] is True
        assert adapter._live_ri == 2
        assert adapter.call("set_live_control", {"tpmi": 3})["error"] == "invalid_parameters"
        adapter.stop()

    # Partial disconnect after "mac ri" also clears the tracked ri.
    with FakeTelnetServer(monkeypatch, close_after=2) as srv:
        disc_dir = tmp_path / "disc"
        disc_dir.mkdir()
        args = (
            f"--continuous-tx --telnetsrv --telnetsrv.listenport {srv.port} "
            "--gNBs.[0].pdsch_AntennaPorts_XP 2 --gNBs.[0].pusch_AntennaPorts 2 "
            "--RUs.[0].nb_tx 2 --RUs.[0].nb_rx 2"
        )
        _, adapter = started_gnb(monkeypatch, disc_dir, conf_path=SAMPLES / "gnb-band41-1x1.conf", args=args)
        assert adapter.call("set_live_control", {"ri": 1})["ok"] is True
        assert adapter._live_ri == 1
        partial = adapter.call("set_live_control", {"ri": 2, "tpmi": 2})
        assert partial["ok"] is False and partial["error"] == "telnet_disconnected"
        assert "mac ri 2" in srv.commands
        assert adapter._live_ri is None
        adapter.stop()
