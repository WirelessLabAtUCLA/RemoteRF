"""OAI gNB and UE adapter; BOTH profiles TRANSMIT.

The operator-supplied executables, configurations and radio arguments are not
managed by this adapter; the operator installs and reviews them. Callers cannot
set any RF parameter (band, frequency, gain): configuration must be empty.

Operator environment (server process only):
  gnb: REMOTERF_OAI_SOFTMODEM, REMOTERF_OAI_GNB_CONF[_<SERIAL>],
       REMOTERF_OAI_GNB_ARGS (default "--continuous-tx")
  ue:  REMOTERF_OAI_UESOFTMODEM, REMOTERF_OAI_UE_CONF[_<SERIAL>],
       REMOTERF_OAI_UE_ARGS (required), REMOTERF_OAI_UE_IFNAME (oaitun_ue1)
  both: REMOTERF_OAI_SHLIBPATH (optional), REMOTERF_OAI_DN_IP (ping target)
A _<SERIAL> variable, when set, overrides the shared conf for that radio.
The UE start returns only once its interface has an IPv4 address.

load_l1_plugin and run_xapp run caller-supplied code UNSANDBOXED as the server
user inside the operation's cgroup. They are refused unless the operator lists
this integration under integrations.json "user_code" (context.user_code).

Plugin control file protocol (what a patched OAI must read): the softmodem env
names OAI_PLUGIN_CTRL, a JSON file replaced atomically (os.replace) on every
change. {"seq": n, "stage": name, "path": abs .so} asks for that shared object
at that stage; {"seq": n, "stage": null} unloads. seq increases by one per
write; act only on a seq newer than the last applied. OAI_SHM_DIR is a
directory shared with xApps for any shared-memory files.

CSI ring protocol (what a patched OAI must write; read by read_csi and
capture_csi_burst in this process, no job): one mmapped file per stream,
$OAI_SHM_DIR/<stream>.ring. Little-endian. Header (64 B): magic b"RRFCSI\\0\\0",
u32 version (1), u32 entry_bytes, u32 n_entries, u32 reserved, u64 write_seq
(seq of the last complete entry, 0 = none), zero padding. Entry seq s lives at
index s % n_entries: u64 lock (seqlock; odd while writing), u64 seq, u32 frame,
u32 slot, u32 rnti, f32 snr_db, i32 ta, u32 dims[3], then dims product
interleaved int16 I/Q pairs. A writer bumps lock, memcpys, bumps lock, then
stores write_seq; no syscalls per slot.
"""
from __future__ import annotations

import fcntl
import ipaddress
import json
import mmap
import os
from pathlib import Path
import re
import shlex
import socket
import struct
import sys
import time

import numpy as np
from remoterf_device_core.integrations.artifacts import MAX_ENTRIES, QUOTA_BYTES


SERIAL = re.compile(r"[A-Za-z0-9]{1,64}")
IFNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")
PROFILES = ("gnb", "ue")
DEFAULT_GNB_ARGS = ("--continuous-tx",)
DEFAULT_UE_IFNAME = "oaitun_ue1"
DEFAULT_DN_IP = "192.168.70.135"
# OAI LOG lines are write(2)s; the printf banner can sit in a pipe's stdio buffer.
GNB_READY_MARKERS = ("Signaling main thread that RU", "TYPE <CTRL-C> TO TERMINATE")
# The framework launches at most 4 processes per operation; one is the softmodem.
MAX_JOBS = 3
MAX_PING_COUNT = 20
UE_IFACE_GONE_S = 5.0
SIOCGIFADDR = 0x8915
# Waits end this long before the runtime's per-integration start/action/stop deadline.
MARGIN = 5.0
XAPP_GRACE = 5.0
MAX_XAPP_SECONDS = 600
OUTPUT_BYTES = 8192
STAGE = re.compile(r"[a-z][a-z0-9_]{0,63}")
CSI_STREAMS = ("ul_pusch", "ul_srs", "dl_ssb")
CSI_MAGIC = b"RRFCSI\0\0"
CSI_VERSION = 1
CSI_HEADER = struct.Struct("<8sIIIIQ")
CSI_HEADER_BYTES = 64
CSI_ENTRY = struct.Struct("<QQIIIfi3I")
MAX_CSI_BURST = 256
CSI_POLL_S = 0.001
CSI_RETRIES = 100
# SIGALRM ends the xApp at duration_s; the bounded wait is the fallback.
XAPP_BOOTSTRAP = ("import runpy,signal,sys;signal.alarm(int(sys.argv[2]));"
                  "sys.argv=sys.argv[1:2];runpy.run_path(sys.argv[0],run_name='__main__')")


def serial_selector(value):
    if type(value) is not dict or set(value) != {"serial"} or type(value["serial"]) is not str:
        raise ValueError("one explicit USRP serial is required")
    if not SERIAL.fullmatch(value["serial"]):
        raise ValueError("invalid USRP serial")
    return value["serial"]


def _operator_file(name):
    value = os.environ.get(name)
    path = Path(value) if value is not None else Path()
    if value is None or not path.is_absolute() or not path.is_file():
        raise RuntimeError(f"{name} must name an absolute existing regular file")
    return str(path)


def _operator_conf(name, serial):
    return _operator_file(f"{name}_{serial}" if f"{name}_{serial}" in os.environ else name)


def _operator_args(name, default=None):
    value = os.environ.get(name)
    args = list(default) if value is None and default is not None else shlex.split(value or "")
    if not args and default is None:
        raise RuntimeError(f"{name} must list the operator's radio arguments")
    return args


def _ue_ifname():
    name = os.environ.get("REMOTERF_OAI_UE_IFNAME", DEFAULT_UE_IFNAME)
    if not IFNAME.fullmatch(name):
        raise RuntimeError("REMOTERF_OAI_UE_IFNAME is not a valid interface name")
    return name


def _dn_ip():
    value = os.environ.get("REMOTERF_OAI_DN_IP", DEFAULT_DN_IP)
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise RuntimeError("REMOTERF_OAI_DN_IP is not an IP address") from None


def interface_ipv4(name):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            raw = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, struct.pack("256s", name.encode()[:15]))
        except OSError:
            return None
    return socket.inet_ntoa(raw[20:24])


def interface_exists(name):
    return Path("/sys/class/net", name).exists()


class OAI:
    def __init__(self, context):
        self._context = context
        self._profile = None
        self._process_id = None
        self._ifname = None
        self._ready = False
        self._stopped = False
        self._jobs = 0
        self._seq = 0
        self._csi_count = 0

    def validate(self, profile, configuration):
        if profile not in PROFILES or type(configuration) is not dict or configuration:
            raise ValueError("unsupported OAI configuration")
        return {}

    def _bound(self, name):
        return max(1.0, getattr(self._context, name, 30) - MARGIN)

    def _process(self):
        rows = self._context.processes.snapshot()["processes"]
        row = next((row for row in rows if row["id"] == self._process_id), None)
        if row is None or row["returncode"] is not None:
            raise RuntimeError(f"OAI {self._profile} process is no longer running")
        return row

    def _argv(self, profile, serial):
        shlib = os.environ.get("REMOTERF_OAI_SHLIBPATH")
        if shlib is not None and (not Path(shlib).is_absolute() or not Path(shlib).is_dir()):
            raise RuntimeError("REMOTERF_OAI_SHLIBPATH must name an absolute existing directory")
        shlib = ["--loader.shlibpath", shlib] if shlib else []
        if profile == "gnb":
            return [_operator_file("REMOTERF_OAI_SOFTMODEM"), "-O",
                    _operator_conf("REMOTERF_OAI_GNB_CONF", serial),
                    *_operator_args("REMOTERF_OAI_GNB_ARGS", DEFAULT_GNB_ARGS), *shlib,
                    "--RUs.[0].sdr_addrs", f"serial={serial}"]
        return [_operator_file("REMOTERF_OAI_UESOFTMODEM"), "-O",
                _operator_conf("REMOTERF_OAI_UE_CONF", serial),
                *_operator_args("REMOTERF_OAI_UE_ARGS"), *shlib,
                "--usrp-args", f"serial={serial}"]

    def _became_ready(self):
        if self._profile == "ue":
            return interface_ipv4(self._ifname) is not None
        processes = self._context.processes
        tail = (processes.log_tail(self._process_id, "stdout")[-65536:]
                + processes.log_tail(self._process_id, "stderr")[-65536:])
        text = tail.decode("utf-8", errors="replace")
        return any(marker in text for marker in GNB_READY_MARKERS)

    def start(self, profile, configuration):
        self.validate(profile, configuration)
        serial = serial_selector(self._context.selector)
        processes = self._context.processes
        work_dir = Path(self._context.work_dir)
        if processes is None or not work_dir.is_absolute():
            raise RuntimeError("owned process supervision is required")
        if self._process_id is not None or self._stopped:
            raise RuntimeError("OAI has already started or stopped")
        if self._context.cancelled.is_set():
            raise RuntimeError(f"OAI {profile} was cancelled")

        argv = self._argv(profile, serial)
        self._ifname = _ue_ifname() if profile == "ue" else None
        # Another UE's interface would fake readiness and block this stop.
        if self._ifname is not None and interface_exists(self._ifname):
            raise RuntimeError("UE interface already in use")
        (work_dir / "shm").mkdir(exist_ok=True)
        # UHD aborts device discovery without $HOME; the work dir keeps it per-operation.
        env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": str(work_dir),
               "OAI_SHM_DIR": str(work_dir / "shm"),
               "OAI_PLUGIN_CTRL": str(work_dir / "plugin_ctrl.json")}
        if "UHD_IMAGES_DIR" in os.environ:
            env["UHD_IMAGES_DIR"] = os.environ["UHD_IMAGES_DIR"]

        self._profile = profile
        self._process_id = processes.launch(
            argv, env=env, cwd=work_dir, required_while_running=True)
        deadline = time.monotonic() + self._bound("start_timeout_s")
        while time.monotonic() < deadline:
            if self._context.cancelled.is_set():
                raise RuntimeError(f"OAI {profile} was cancelled")
            self._process()
            if self._became_ready():
                self._ready = True
                return
            time.sleep(0.1)
        raise RuntimeError(f"OAI {profile} did not reach readiness")

    def status(self):
        if self._stopped:
            return {"ready": False, "stopped": True}
        result = {"ready": self._ready, "profile": self._profile}
        if self._process_id is not None:
            self._process()
        if self._profile == "ue":
            result["ip"] = interface_ipv4(self._ifname)
        return result

    def _link_status(self):
        if self._profile != "ue":
            if self._process_id is not None:
                self._process()
            return {"state": "running" if self._ready else "starting"}
        ip = self.status()["ip"]
        return {"state": "ip_up" if ip else "syncing", "ifname": self._ifname, "ip": ip}

    def call(self, action, parameters):
        if type(parameters) is not dict:
            raise ValueError("parameters must be an object")
        if action == "health" and not parameters:
            return self.status()
        if action == "link_status" and not parameters:
            return self._link_status()
        if action == "ping":
            count = parameters.get("count", 4)
            if not set(parameters) <= {"count"} or type(count) is not int or not 1 <= count <= MAX_PING_COUNT:
                raise ValueError("ping accepts count 1-20")
            # A raised action tears the operation down; a gNB cell must survive a misdirected ping.
            if self._profile != "ue":
                return {"ok": False, "error": "ue_profile_required"}
            if self._jobs >= MAX_JOBS:
                return {"ok": False, "error": "job_budget_exhausted"}
            self._jobs += 1
            wait = self._bound("action_timeout_s")
            result = self._job(["/usr/bin/ping", "-n", "-I", self._ifname, "-c", str(count),
                                "-W", "2", "-w", str(max(1, int(wait) - 1)), _dn_ip()],
                               {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}, wait)
            return {"ok": result["returncode"] == 0, **result}
        if action in {"read_csi", "capture_csi_burst"}:
            return self._csi(action, parameters)
        if action not in {"load_l1_plugin", "unload_l1_plugin", "run_xapp"}:
            raise ValueError("unsupported action or parameters")
        if getattr(self._context, "user_code", False) is not True:
            raise PermissionError("user code is disabled for this integration")
        if self._context.processes is None or not Path(self._context.work_dir).is_absolute():
            raise RuntimeError("owned process supervision is required")
        if action == "unload_l1_plugin":
            if parameters:
                raise ValueError("unload_l1_plugin accepts no parameters")
            return self._control(None, None)
        if action == "load_l1_plugin":
            if (set(parameters) != {"stage", "c_code"} or type(parameters["stage"]) is not str
                    or not STAGE.fullmatch(parameters["stage"]) or type(parameters["c_code"]) is not str
                    or not parameters["c_code"]):
                raise ValueError("load_l1_plugin requires stage and c_code")
            if self._jobs >= MAX_JOBS:
                return {"ok": False, "error": "job_budget_exhausted"}
            return self._load(parameters["stage"], parameters["c_code"])
        duration = parameters.get("duration_s")
        if (set(parameters) != {"code", "duration_s"} or type(parameters["code"]) is not str
                or not parameters["code"] or type(duration) is not int
                or not 1 <= duration <= MAX_XAPP_SECONDS):
            raise ValueError("run_xapp requires code and duration_s")
        if self._jobs >= MAX_JOBS:
            return {"ok": False, "error": "job_budget_exhausted"}
        return self._xapp(parameters["code"], duration)

    def _source(self, folder, name, text, suffix):
        self._jobs += 1
        path = Path(self._context.work_dir) / folder / f"{name}-{self._jobs}{suffix}"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _job(self, argv, env, wait):
        """Run one finite owned job; past the wait the action fails (cgroup cleanup)."""
        processes = self._context.processes
        process_id = processes.launch(argv, env=env, cwd=Path(self._context.work_dir))
        deadline = time.monotonic() + wait
        while True:
            if self._context.cancelled.is_set():
                raise RuntimeError("OAI job was cancelled")
            row = next(row for row in processes.snapshot()["processes"] if row["id"] == process_id)
            done = time.monotonic() >= deadline
            if row["returncode"] is not None and (row["logs_drained"] or done):
                break
            if done:
                raise RuntimeError("OAI job exceeded its bounded wait")
            time.sleep(0.05)
        output = processes.log_tail(process_id, "stdout") + processes.log_tail(process_id, "stderr")
        return {"returncode": row["returncode"],
                "output": output[-OUTPUT_BYTES:].decode("utf-8", errors="replace")}

    def _control(self, stage, path):
        self._seq += 1
        value = {"seq": self._seq, "stage": stage}
        if stage is not None:
            value["path"] = path
        ctrl = Path(self._context.work_dir) / "plugin_ctrl.json"
        tmp = ctrl.with_name(f".{ctrl.name}.tmp")
        tmp.write_text(json.dumps(value), encoding="utf-8")
        os.replace(tmp, ctrl)
        return {"ok": True, **value}

    def _load(self, stage, code):
        c_file = self._source("plugins", "plugin", code, ".c")
        shared = c_file.with_suffix(".so")
        cc = os.environ.get("REMOTERF_OAI_CC", "/usr/bin/gcc")
        if not Path(cc).is_absolute() or not Path(cc).is_file():
            raise RuntimeError("REMOTERF_OAI_CC must name an absolute existing regular file")
        result = self._job([cc, "-O3", "-shared", "-fPIC", "-o", str(shared), str(c_file)],
                           {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
                           self._bound("action_timeout_s"))
        if result["returncode"] != 0 or not shared.is_file():
            return {"ok": False, **result}
        return {**self._control(stage, str(shared)), **result}

    def _xapp(self, code, duration):
        script = self._source("xapps", "xapp", code, ".py")
        name = f"xapp-{self._jobs}.json"
        work_dir = Path(self._context.work_dir)
        (work_dir / "artifacts").mkdir(exist_ok=True)
        result = self._job([sys.executable, "-I", "-c", XAPP_BOOTSTRAP, str(script), str(duration)],
                           {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                            "OAI_SHM_DIR": str(work_dir / "shm"),
                            "XAPP_OUTPUT": str(work_dir / "artifacts" / name)},
                           min(duration + XAPP_GRACE, self._bound("action_timeout_s")))
        if (work_dir / "artifacts" / name).is_file():
            result["artifact"] = {"name": name, "format": "json"}
        return result

    def _csi(self, action, parameters):
        """Read-only, in-process: no job budget and no user code."""
        stream = parameters.get("stream")
        if stream not in CSI_STREAMS:
            raise ValueError(f"stream must be one of {', '.join(CSI_STREAMS)}")
        wait = self._bound("action_timeout_s")
        count, timeout = 1, wait
        if action == "read_csi":
            if set(parameters) != {"stream"}:
                raise ValueError("read_csi accepts stream only")
        else:
            count, timeout = parameters.get("count"), parameters.get("timeout_s", wait)
            if (not set(parameters) <= {"stream", "count", "timeout_s"} or type(count) is not int
                    or not 1 <= count <= MAX_CSI_BURST or type(timeout) not in (int, float)
                    or not 0 < timeout <= 600):
                raise ValueError(f"capture_csi_burst requires stream, count 1-{MAX_CSI_BURST}"
                                 " and optional timeout_s (0-600]")
        path = Path(self._context.work_dir) / "shm" / f"{stream}.ring"
        try:
            with open(path, "rb") as handle:
                ring = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        except FileNotFoundError:
            # A raised action tears the operation down; stock OAI writes no ring.
            return {"ok": False, "error": "ring_missing"}
        except (OSError, ValueError):
            return {"ok": False, "error": "ring_invalid"}
        with ring:
            if len(ring) < CSI_HEADER_BYTES:
                return {"ok": False, "error": "ring_invalid"}
            magic, version, entry_bytes, n_entries, _, _ = CSI_HEADER.unpack_from(ring)
            if (magic != CSI_MAGIC or version != CSI_VERSION or entry_bytes < CSI_ENTRY.size
                    or n_entries < 1 or len(ring) < CSI_HEADER_BYTES + entry_bytes * n_entries):
                return {"ok": False, "error": "ring_invalid"}
            # Upper bound from entry_bytes: int16 pairs become complex64, twice the size.
            if not self._artifact_room(count * (entry_bytes - CSI_ENTRY.size) * 2):
                return {"ok": False, "error": "artifact_limit"}
            if action == "read_csi":
                entries, dropped = self._csi_latest(ring, entry_bytes, n_entries, wait), 0
            else:
                entries, dropped = self._csi_burst(ring, entry_bytes, n_entries, count,
                                                   min(timeout, wait))
        if entries is None:
            return {"ok": False, "error": "timeout", "dropped": dropped}
        if len({entry["dims"] for entry in entries}) != 1:
            return {"ok": False, "error": "dims_changed", "dropped": dropped}
        return self._publish_csi(stream, entries, action == "read_csi", dropped)

    def _artifact_room(self, size):
        directory = Path(self._context.work_dir) / "artifacts"
        files = [entry.stat().st_size for entry in directory.iterdir()] if directory.is_dir() else []
        return len(files) < MAX_ENTRIES and sum(files) + size <= QUOTA_BYTES

    def _csi_entry(self, ring, entry_bytes, n_entries, seq):
        """Copy entry seq; None if it is being written, torn, overwritten or malformed."""
        offset = CSI_HEADER_BYTES + (seq % n_entries) * entry_bytes
        for _ in range(CSI_RETRIES):
            lock = struct.unpack_from("<Q", ring, offset)[0]
            if lock & 1:
                time.sleep(CSI_POLL_S / 10)
                continue
            data = ring[offset:offset + entry_bytes]
            if struct.unpack_from("<Q", ring, offset)[0] != lock:
                continue
            _, got, frame, slot, rnti, snr_db, ta, *dims = CSI_ENTRY.unpack_from(data)
            size = 4 * dims[0] * dims[1] * dims[2]
            if got != seq or CSI_ENTRY.size + size > entry_bytes:
                return None
            return {"seq": seq, "frame": frame, "slot": slot, "rnti": rnti, "snr_db": round(snr_db, 2),
                    "ta": ta, "dims": tuple(dims), "timestamp_s": time.time(),
                    "iq": data[CSI_ENTRY.size:CSI_ENTRY.size + size]}
        return None

    def _csi_latest(self, ring, entry_bytes, n_entries, wait):
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self._context.cancelled.is_set():
                raise RuntimeError("OAI CSI read was cancelled")
            seq = CSI_HEADER.unpack_from(ring)[5]
            entry = self._csi_entry(ring, entry_bytes, n_entries, seq) if seq else None
            if entry is not None:
                return [entry]
            time.sleep(CSI_POLL_S)
        return None

    def _csi_burst(self, ring, entry_bytes, n_entries, count, wait):
        """Wait for count entries newer than now; seqs lost to the writer are dropped."""
        deadline = time.monotonic() + wait
        last = CSI_HEADER.unpack_from(ring)[5]
        entries, dropped = [], 0
        while len(entries) < count:
            if self._context.cancelled.is_set():
                raise RuntimeError("OAI CSI capture was cancelled")
            if time.monotonic() >= deadline:
                return None, dropped
            newest = CSI_HEADER.unpack_from(ring)[5]
            if newest <= last:
                time.sleep(CSI_POLL_S)
                continue
            # Older than one ring length is already overwritten.
            first = max(last + 1, newest - n_entries + 1)
            dropped += first - last - 1
            for seq in range(first, newest + 1):
                if len(entries) == count:
                    break
                entry = self._csi_entry(ring, entry_bytes, n_entries, seq)
                if entry is None:
                    dropped += 1
                else:
                    entries.append(entry)
            last = newest
        return entries, dropped

    def _publish_csi(self, stream, entries, single, dropped):
        self._csi_count += 1
        name = f"csi-{stream}-{self._csi_count}.c8"
        dims = list(entries[0]["dims"])
        shape = dims if single else [len(entries), *dims]
        iq = np.frombuffer(b"".join(entry["iq"] for entry in entries), dtype="<i2")
        values = iq.astype("<f4").view("<c8").reshape(shape)
        directory = Path(self._context.work_dir) / "artifacts"
        directory.mkdir(exist_ok=True)
        tmp = directory / f".{name}.tmp"
        tmp.write_bytes(values.tobytes())
        os.replace(tmp, directory / name)
        metadata = {key: [entry[key] for entry in entries]
                    for key in ("seq", "frame", "slot", "snr_db", "rnti", "ta")}
        metadata["timestamps_s"] = [entry["timestamp_s"] for entry in entries]
        return {"ok": True, "artifact": {"name": name, "dtype": "<c8", "shape": shape},
                "metadata": metadata, "dropped": dropped}

    def stop(self):
        if self._stopped:
            return
        if self._context.processes is None:
            self._stopped = True
            return
        if not self._context.processes.stop(timeout=self._bound("stop_timeout_s")):
            raise RuntimeError("OAI cleanup could not be verified")
        if self._ifname is not None and self._process_id is not None:
            deadline = time.monotonic() + UE_IFACE_GONE_S
            while interface_exists(self._ifname):
                if time.monotonic() >= deadline:
                    raise RuntimeError("UE interface still present")
                time.sleep(0.1)
        self._stopped = True


def create_adapter(context):
    return OAI(context)
