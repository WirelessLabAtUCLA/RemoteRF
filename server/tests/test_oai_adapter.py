import importlib
import json
from pathlib import Path
import subprocess
import sys
import threading
from threading import Event
import time
from types import SimpleNamespace

import numpy as np
import pytest

from remoteRF_server.adapters import oai
from remoterf_device_core.integrations.payload import object_data
from remoterf_device_core.integrations.registry import Manifest


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

    def write(self, seq, lock=2, publish=True, snr_db=12.5, dims=None):
        dims = self.dims if dims is None else dims
        n = dims[0] * dims[1] * dims[2]
        iq = (np.arange(2 * n, dtype=np.int64) + (seq & 0xFF)).astype("<i2")
        entry = oai.CSI_ENTRY.pack(
            lock, seq, (seq // 20) & 0xFFFFFFFF, seq % 20, 0x4601, snr_db, 3, *dims
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
