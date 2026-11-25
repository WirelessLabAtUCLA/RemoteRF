"""OAI gNB and UE adapter; BOTH profiles TRANSMIT.

The operator-supplied executables, configurations and radio arguments are not
managed by this adapter; the operator installs and reviews them. Callers cannot
set any RF parameter (band, frequency, gain): configuration must be empty.

Operator environment (server process only):
  gnb:  REMOTERF_OAI_SOFTMODEM, REMOTERF_OAI_GNB_CONF[_<SERIAL>],
        REMOTERF_OAI_GNB_ARGS (default "--continuous-tx"),
        REMOTERF_OAI_GNB_PCAP_IFNAME (required for gNB capture_air_pcap)
  ue:   REMOTERF_OAI_UESOFTMODEM, REMOTERF_OAI_UE_CONF[_<SERIAL>],
        REMOTERF_OAI_UE_ARGS (required), REMOTERF_OAI_UE_IFNAME (oaitun_ue1)
  both: REMOTERF_OAI_SHLIBPATH (optional),
        REMOTERF_OAI_DN_IP (ping default 192.168.70.135; required for iperf3),
        REMOTERF_OAI_IPERF3 (optional path), REMOTERF_OAI_IPERF3_PORT (5201),
        REMOTERF_OAI_ALLOW_PCAP ("1" to enable capture_air_pcap; off by default),
        REMOTERF_OAI_TCPDUMP (optional path)
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

Ring-buffer protocol (what a patched OAI must write; read in this process, no
job): one mmapped file per stream, $OAI_SHM_DIR/<stream>.ring. Little-endian.
Header (64 B): magic b"RRFCSI\\0\\0", u32 version (1), u32 entry_bytes,
u32 n_entries, u32 reserved, u64 write_seq (seq of the last complete entry,
0 = none), zero padding. Entry seq s lives at index s % n_entries:
u64 lock (seqlock; odd while writing), u64 seq, u32 frame, u32 slot, u32 rnti,
f32 snr_db, i32 ta, u32 dims[3], then dims product interleaved int16 I/Q pairs.
A writer bumps lock, memcpys, bumps lock, then stores write_seq; no syscalls
per slot.
Streams:
  - ul_pusch, ul_srs, dl_ssb: CSI tensors read by read_csi / capture_csi_burst.
  - ofdm_grid: 14-symbol slot resource grid read by read_ofdm_grid; dims is
    [n_ant, 14, n_sc] (dims[1] must equal 14).
  - constellation: equalized IQ symbols read by read_constellation; dims is
    [n_layers, n_sym, n_re]; f32 holds snr_db and i32 (ta slot) holds RMS EVM
    in thousandths of a percent (evm_x1000, <= 0 means unset and the adapter
    derives EVM from snr_db).
"""
from __future__ import annotations

import fcntl
import ipaddress
import json
import math
import mmap
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import struct
import sys
import time

import numpy as np
from remoterf_device_core.integrations.artifacts import MAX_ENTRIES, QUOTA_BYTES

from .config import (
    extract_gnb_cell_config,
    hz_to_nr_arfcn,
    nr_arfcn_to_hz,
    oai_fft_size,
    parse_libconfig,
    parse_ue_cell_config,
)
from .stats import (
    merge_gnb_stats,
    parse_l1_stats,
    parse_mac_stats,
    read_gnb_stats,
    read_ue_stats,
)
from .telnet import (
    ControlError,
    DEFAULT_TELNET_HOST,
    DEFAULT_TELNET_PORT,
    LIVE_CONTROL_KEYS,
    MAX_LDPC_ITERATIONS,
    MAX_MCS,
    TELNET_TIMEOUT_S,
    TelnetClient,
    parse_telnet_endpoint,
    validate_live_control,
)


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
EXTRA_RING_STREAMS = ("ofdm_grid", "constellation")
RING_STREAMS = CSI_STREAMS + EXTRA_RING_STREAMS
OFDM_SYMBOLS_PER_SLOT = 14
CSI_MAGIC = b"RRFCSI\0\0"
CSI_VERSION = 1
CSI_HEADER = struct.Struct("<8sIIIIQ")
CSI_HEADER_BYTES = 64
CSI_ENTRY = struct.Struct("<QQIIIfi3I")
MAX_CSI_BURST = 256
CSI_POLL_S = 0.001
CSI_RETRIES = 100
# Traffic generation bounds.
MAX_THROUGHPUT_SECONDS = 60
DEFAULT_THROUGHPUT_SECONDS = 10
MAX_TRAFFIC_SECONDS = 600
DEFAULT_TRAFFIC_SECONDS = 60
TRAFFIC_GRACE_S = 10.0
DEFAULT_IPERF3_PORT = 5201
MAX_IPERF3_JSON_BYTES = 2 * 1024 * 1024
# Packet capture bounds.
MAX_PCAP_SECONDS = 60
DEFAULT_PCAP_SECONDS = 10
MAX_PCAP_BYTES = 64 * 1024 * 1024
DEFAULT_PCAP_BYTES = 16 * 1024 * 1024
PCAP_GRACE_S = 5.0
CAP_NET_RAW_BIT = 13
# SIGALRM ends the xApp at duration_s; the bounded wait is the fallback.
XAPP_BOOTSTRAP = ("import runpy,signal,sys;signal.alarm(int(sys.argv[2]));"
                  "sys.argv=sys.argv[1:2];runpy.run_path(sys.argv[0],run_name='__main__')")
# Supervised background iperf3 wrapper: watches stop_file, sends SIGINT for clean JSON flush.
TRAFFIC_BOOTSTRAP = (
    "import os,signal,subprocess,sys,time\n"
    "sf,of,lim=sys.argv[1],sys.argv[2],int(sys.argv[3])\n"
    "tmp=of+'.tmp'\n"
    "with open(tmp,'wb') as fh:\n"
    " try:\n"
    "  p=subprocess.Popen(sys.argv[4:],stdout=fh,stderr=subprocess.PIPE)\n"
    " except OSError as exc:\n"
    "  sys.stderr.write(str(exc)+'\\n');sys.exit(127)\n"
    " dl=time.monotonic()+lim+5\n"
    " stopped=False\n"
    " while p.poll() is None:\n"
    "  if os.path.exists(sf) and not stopped:\n"
    "   try: p.send_signal(signal.SIGINT)\n"
    "   except ProcessLookupError: pass\n"
    "   stopped=True;dl=min(dl,time.monotonic()+5)\n"
    "  if time.monotonic()>=dl:\n"
    "   try: p.kill()\n"
    "   except ProcessLookupError: pass\n"
    "   break\n"
    "  time.sleep(0.05)\n"
    " try:\n"
    "  _,err=p.communicate(timeout=5)\n"
    " except subprocess.TimeoutExpired:\n"
    "  try: p.kill()\n"
    "  except ProcessLookupError: pass\n"
    "  _,err=p.communicate()\n"
    "os.replace(tmp,of)\n"
    "sys.stderr.buffer.write(err)\n"
    "sys.exit(p.returncode if p.returncode is not None else 1)\n"
)
# Supervised tcpdump wrapper: enforces wall-clock duration even when idle, plus max byte cap.
PCAP_BOOTSTRAP = (
    "import os,signal,subprocess,sys,time\n"
    "out,dur,maxb=sys.argv[1],int(sys.argv[2]),int(sys.argv[3])\n"
    "try:\n"
    " p=subprocess.Popen(sys.argv[4:],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)\n"
    "except OSError as exc:\n"
    " sys.stderr.write(str(exc)+'\\n');sys.exit(127)\n"
    "dl=time.monotonic()+dur\n"
    "stopped=False\n"
    "while p.poll() is None:\n"
    " try: sz=os.path.getsize(out)\n"
    " except OSError: sz=0\n"
    " if time.monotonic()>=dl or sz>=maxb:\n"
    "  if not stopped:\n"
    "   try: p.send_signal(signal.SIGINT)\n"
    "   except ProcessLookupError: pass\n"
    "   stopped=True;dl=time.monotonic()+3\n"
    "  elif time.monotonic()>=dl:\n"
    "   try: p.kill()\n"
    "   except ProcessLookupError: pass\n"
    "   break\n"
    " time.sleep(0.05)\n"
    "try:\n"
    " _,err=p.communicate(timeout=5)\n"
    "except subprocess.TimeoutExpired:\n"
    " try: p.kill()\n"
    " except ProcessLookupError: pass\n"
    " _,err=p.communicate()\n"
    "sys.stderr.buffer.write(err)\n"
    "rc=p.returncode if p.returncode is not None else 1\n"
    "sys.exit(0 if stopped and rc in (0,-2,130) else rc)\n"
)

_SAFE_ACTIONS = frozenset({
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
})


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


def _configured_dn_ip():
    """Return (ip_str, None) when REMOTERF_OAI_DN_IP is explicitly configured and valid."""
    value = os.environ.get("REMOTERF_OAI_DN_IP")
    if value is None or not value.strip():
        return None, "dn_ip_missing"
    try:
        return str(ipaddress.ip_address(value.strip())), None
    except ValueError:
        return None, "dn_ip_invalid"


def _find_executable(env_name, binary_name, fallback_paths):
    if env_name in os.environ:
        value = os.environ[env_name].strip()
        path = Path(value) if value else Path()
        return str(path) if value and path.is_absolute() and path.is_file() else None
    found = shutil.which(binary_name)
    if found:
        path = Path(found)
        if path.is_absolute() and path.is_file():
            return str(path)
    for candidate in fallback_paths:
        path = Path(candidate)
        if path.is_absolute() and path.is_file():
            return str(path)
    return None


def _allow_pcap():
    return os.environ.get("REMOTERF_OAI_ALLOW_PCAP", "").strip().lower() in {"1", "true", "yes"}


def _proc_status_has_cap(status_text: str, cap_bit: int) -> bool:
    mask = 1 << cap_bit
    for line in status_text.splitlines():
        if line.startswith(("CapEff:", "CapAmb:")):
            parts = line.split(":", 1)
            if len(parts) == 2:
                try:
                    if int(parts[1].strip(), 16) & mask:
                        return True
                except ValueError:
                    continue
    return False


def has_cap_net_raw(executable: str | None = None) -> bool:
    """Return True when the process or target binary has CAP_NET_RAW."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    try:
        status_text = Path("/proc/self/status").read_text(encoding="utf-8", errors="replace")
        if _proc_status_has_cap(status_text, CAP_NET_RAW_BIT):
            return True
    except OSError:
        pass
    if executable:
        try:
            st = os.stat(executable)
            if st.st_uid == 0 and bool(st.st_mode & stat.S_ISUID):
                return True
        except OSError:
            pass
        if hasattr(os, "getxattr"):
            try:
                raw = os.getxattr(executable, "security.capability")
                if len(raw) >= 12:
                    _magic_etc, permitted_lo = struct.unpack_from("<II", raw, 0)
                    if permitted_lo & (1 << CAP_NET_RAW_BIT):
                        return True
            except OSError:
                pass
    return False


def _finite_num(value):
    if type(value) in (int, float) and math.isfinite(value):
        return float(value)
    return None


def _finite_round(value, digits):
    num = _finite_num(value)
    return round(num, digits) if num is not None else None


def _opt_int_val(value):
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def parse_iperf3_json(raw: bytes | str, *, allow_interrupt: bool = False) -> tuple[dict | None, str | None, str | None]:
    """Parse iperf3 -J output into (metrics_dict, error_code, error_detail)."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    stripped = text.strip()
    if not stripped:
        return None, "iperf3_invalid_output", "iperf3 produced no JSON output"
    if not stripped.startswith("{"):
        brace = stripped.find("{")
        if brace != -1:
            stripped = stripped[brace:]
    try:
        data = json.loads(stripped)
    except ValueError:
        return None, "iperf3_invalid_output", "iperf3 output was not valid JSON"
    if type(data) is not dict:
        return None, "iperf3_invalid_output", "iperf3 JSON root must be an object"
    err_msg = str(data["error"])[:512] if data.get("error") else None
    if err_msg is not None and not (allow_interrupt and "interrupt" in err_msg.lower()):
        return None, "iperf3_failed", err_msg
    end = data.get("end")
    if type(end) is not dict:
        if err_msg is not None:
            return None, "iperf3_failed", err_msg
        return None, "iperf3_invalid_output", "iperf3 JSON is missing end summary"

    sum_sent = end.get("sum_sent") if type(end.get("sum_sent")) is dict else None
    sum_recv = end.get("sum_received") if type(end.get("sum_received")) is dict else None
    sum_udp = end.get("sum") if type(end.get("sum")) is dict else None
    primary = sum_recv or sum_udp or sum_sent
    if primary is None:
        if err_msg is not None:
            return None, "iperf3_failed", err_msg
        return None, "iperf3_invalid_output", "iperf3 JSON end section has no summary"

    bps = _finite_num(primary.get("bits_per_second"))
    sent_bps = _finite_num((sum_sent or sum_udp or {}).get("bits_per_second"))
    recv_bps = _finite_num((sum_recv or sum_udp or {}).get("bits_per_second"))

    jitter_src = (sum_recv if (sum_recv and "jitter_ms" in sum_recv)
                  else (sum_udp if (sum_udp and "jitter_ms" in sum_udp) else None))
    loss_src = (sum_recv if (sum_recv and "lost_percent" in sum_recv)
                else (sum_udp if (sum_udp and "lost_percent" in sum_udp) else None))

    metrics = {
        "throughput_mbps": round(bps / 1_000_000.0, 3) if bps is not None else None,
        "sent_mbps": round(sent_bps / 1_000_000.0, 3) if sent_bps is not None else None,
        "received_mbps": round(recv_bps / 1_000_000.0, 3) if recv_bps is not None else None,
        "bytes": _opt_int_val(primary.get("bytes")),
        "duration_s": _finite_round(primary.get("seconds"), 3),
        "jitter_ms": _finite_round(jitter_src.get("jitter_ms"), 3) if jitter_src else None,
        "lost_packets": _opt_int_val(loss_src.get("lost_packets")) if loss_src else None,
        "total_packets": _opt_int_val(loss_src.get("packets")) if loss_src else None,
        "loss_percent": _finite_round(loss_src.get("lost_percent"), 4) if loss_src else None,
        "retransmits": _opt_int_val(sum_sent.get("retransmits")) if sum_sent else None,
    }
    return metrics, None, None


def _constellation_evm(snr_db, raw_evm_x1000):
    if type(raw_evm_x1000) is int and raw_evm_x1000 > 0:
        evm_pct = round(raw_evm_x1000 / 1000.0, 3)
        evm_db = round(20.0 * math.log10(evm_pct / 100.0), 2) if evm_pct > 0 else None
        if math.isfinite(evm_pct) and (evm_db is None or math.isfinite(evm_db)):
            return evm_pct, evm_db, "writer"
    if snr_db is not None and math.isfinite(snr_db) and -200.0 <= snr_db <= 200.0:
        evm_pct = round(100.0 * (10.0 ** (-snr_db / 20.0)), 3)
        evm_db = round(-snr_db, 2)
        if math.isfinite(evm_pct) and math.isfinite(evm_db):
            return evm_pct, evm_db, "snr_derived"
    return None, None, None


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
        self._ring_counts = {"ofdm_grid": 0, "constellation": 0}
        self._pcap_count = 0
        self._traffic = None
        self._traffic_used = False
        self._telnet = None
        self._telnet_endpoint = None

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
        if action in _SAFE_ACTIONS:
            return self._dispatch_safe(action, parameters)
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

    def _dispatch_safe(self, action, parameters):
        """Run an expansion action so failures always return an error dict, never tearing down the cell."""
        try:
            if action == "get_cell_config":
                return self._get_cell_config(parameters)
            if action == "get_link_stats":
                return self._get_link_stats(parameters)
            if action == "get_ue_mac_table":
                return self._get_ue_mac_table(parameters)
            if action == "read_ofdm_grid":
                return self._read_extra_ring("ofdm_grid", parameters)
            if action == "read_constellation":
                return self._read_extra_ring("constellation", parameters)
            if action == "throughput_test":
                return self._throughput_test(parameters)
            if action == "start_traffic":
                return self._start_traffic(parameters)
            if action == "stop_traffic":
                return self._stop_traffic(parameters)
            if action == "capture_air_pcap":
                return self._capture_air_pcap(parameters)
            if action == "set_live_control":
                return self._set_live_control(parameters)
            return {"ok": False, "error": "unsupported_action"}
        except Exception as exc:
            return {"ok": False, "error": "action_error", "detail": type(exc).__name__}

    def _resolve_cell_config(self):
        if self._profile is None:
            return {"ok": False, "error": "not_started", "detail": "OAI profile has not started"}
        if self._profile == "gnb":
            try:
                serial = serial_selector(self._context.selector)
                conf = _operator_conf("REMOTERF_OAI_GNB_CONF", serial)
                args = _operator_args("REMOTERF_OAI_GNB_ARGS", DEFAULT_GNB_ARGS)
            except (RuntimeError, ValueError) as exc:
                return {"ok": False, "error": "config_unavailable", "detail": str(exc)}
            try:
                return extract_gnb_cell_config(conf, args)
            except (OSError, ValueError):
                return {"ok": False, "error": "config_parse_error", "detail": "failed to parse gNB config"}
        try:
            args = _operator_args("REMOTERF_OAI_UE_ARGS")
        except RuntimeError as exc:
            return {"ok": False, "error": "config_unavailable", "detail": str(exc)}
        return parse_ue_cell_config(args)

    def _get_cell_config(self, parameters):
        if parameters:
            return {"ok": False, "error": "invalid_parameters", "detail": "get_cell_config accepts no parameters"}
        return self._resolve_cell_config()

    def _get_link_stats(self, parameters):
        if parameters:
            return {"ok": False, "error": "invalid_parameters", "detail": "get_link_stats accepts no parameters"}
        if self._profile == "ue":
            ue_data = read_ue_stats(self._context.work_dir)
            if ue_data is None:
                return {"ok": False, "error": "not_available_on_ue",
                        "detail": "link stats are not available on the UE profile"}
            return {"ok": True, "profile": "ue", **ue_data}
        data = read_gnb_stats(self._context.work_dir)
        if data is None:
            return {"ok": False, "error": "stats_missing",
                    "detail": "nrMAC_stats.log and nrL1_stats.log were not found in work_dir"}
        return {"ok": True, "profile": self._profile or "gnb", **data}

    def _get_ue_mac_table(self, parameters):
        if parameters:
            return {"ok": False, "error": "invalid_parameters", "detail": "get_ue_mac_table accepts no parameters"}
        if self._profile == "ue":
            ue_data = read_ue_stats(self._context.work_dir)
            if ue_data is None:
                return {"ok": False, "error": "not_available_on_ue",
                        "detail": "UE MAC table is not available on the UE profile"}
            if "ues" in ue_data:
                return {"ok": True, "profile": "ue", "ues": ue_data["ues"],
                        "ue_count": ue_data["ue_count"], "truncated": ue_data["truncated"],
                        "file_age_s": ue_data["file_age_s"], "files": ue_data["files"]}
            return {"ok": True, "profile": "ue", **ue_data}
        data = read_gnb_stats(self._context.work_dir)
        if data is None:
            return {"ok": False, "error": "stats_missing",
                    "detail": "nrMAC_stats.log and nrL1_stats.log were not found in work_dir"}
        return {
            "ok": True,
            "profile": self._profile or "gnb",
            "ues": data["ues"],
            "ue_count": data["ue_count"],
            "truncated": data["truncated"],
            "file_age_s": data["file_age_s"],
            "files": data["files"],
        }

    def _read_extra_ring(self, stream, parameters):
        wait = self._bound("action_timeout_s")
        if not set(parameters) <= {"timeout_s"}:
            return {"ok": False, "error": "invalid_parameters",
                    "detail": f"read_{stream} accepts optional timeout_s only"}
        timeout = parameters.get("timeout_s", wait)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 600:
            return {"ok": False, "error": "invalid_parameters", "detail": "timeout_s must be in (0, 600]"}
        bounded_wait = min(float(timeout), wait)
        path = Path(self._context.work_dir) / "shm" / f"{stream}.ring"
        try:
            with open(path, "rb") as handle:
                ring = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        except FileNotFoundError:
            return {"ok": False, "error": "ring_missing",
                    "detail": f"{stream} ring not found; requires patched OAI"}
        except (OSError, ValueError):
            return {"ok": False, "error": "ring_invalid"}

        with ring:
            if len(ring) < CSI_HEADER_BYTES:
                return {"ok": False, "error": "ring_invalid"}
            magic, version, entry_bytes, n_entries, _, _ = CSI_HEADER.unpack_from(ring)
            if (magic != CSI_MAGIC or version != CSI_VERSION or entry_bytes < CSI_ENTRY.size
                    or n_entries < 1 or len(ring) < CSI_HEADER_BYTES + entry_bytes * n_entries):
                return {"ok": False, "error": "ring_invalid"}
            if not self._artifact_room((entry_bytes - CSI_ENTRY.size) * 2):
                return {"ok": False, "error": "artifact_limit"}
            try:
                entries = self._csi_latest(ring, entry_bytes, n_entries, bounded_wait)
            except RuntimeError:
                return {"ok": False, "error": "cancelled"}

        if entries is None:
            return {"ok": False, "error": "timeout", "dropped": 0}
        entry = entries[0]
        if stream == "ofdm_grid" and entry["dims"][1] != OFDM_SYMBOLS_PER_SLOT:
            return {"ok": False, "error": "ring_invalid",
                    "detail": f"ofdm_grid expects {OFDM_SYMBOLS_PER_SLOT} OFDM symbols in dims[1]"}
        return self._publish_extra_ring(stream, entry)

    def _publish_extra_ring(self, stream, entry):
        self._ring_counts[stream] += 1
        name = f"{stream}-{self._ring_counts[stream]}.c8"
        shape = list(entry["dims"])
        iq = np.frombuffer(entry["iq"], dtype="<i2")
        values = iq.astype("<f4").view("<c8").reshape(shape)
        directory = Path(self._context.work_dir) / "artifacts"
        directory.mkdir(exist_ok=True)
        tmp = directory / f".{name}.tmp"
        tmp.write_bytes(values.tobytes())
        os.replace(tmp, directory / name)
        metadata = {
            "seq": [entry["seq"]],
            "frame": [entry["frame"]],
            "slot": [entry["slot"]],
            "snr_db": [entry["snr_db"]],
            "rnti": [entry["rnti"]],
            "timestamps_s": [entry["timestamp_s"]],
        }
        if stream == "ofdm_grid":
            metadata["ta"] = [entry["ta"]]
        else:
            evm_pct, evm_db, evm_source = _constellation_evm(entry["snr_db"], entry["ta"])
            metadata["evm_pct"] = evm_pct
            metadata["evm_db"] = evm_db
            metadata["evm_source"] = evm_source
        return {"ok": True, "artifact": {"name": name, "dtype": "<c8", "shape": shape},
                "metadata": metadata, "dropped": 0}

    def _validate_iperf_params(self, parameters, *, max_duration, default_duration):
        allowed = {"duration_s", "direction", "protocol", "bitrate_mbps", "parallel", "port"}
        unknown = sorted(set(parameters) - allowed)
        if unknown:
            return None, {"ok": False, "error": "invalid_parameters",
                          "detail": f"unsupported parameter(s): {', '.join(unknown)}"}
        duration_s = parameters.get("duration_s", default_duration)
        if type(duration_s) is not int or not 1 <= duration_s <= max_duration:
            return None, {"ok": False, "error": "invalid_parameters",
                          "detail": f"duration_s must be an integer in 1..{max_duration}"}
        direction = parameters.get("direction", "ul")
        if direction not in {"ul", "dl"}:
            return None, {"ok": False, "error": "invalid_parameters",
                          "detail": "direction must be 'ul' or 'dl'"}
        protocol = parameters.get("protocol", "tcp")
        if protocol not in {"tcp", "udp"}:
            return None, {"ok": False, "error": "invalid_parameters",
                          "detail": "protocol must be 'tcp' or 'udp'"}
        bitrate = parameters.get("bitrate_mbps")
        if bitrate is not None and (type(bitrate) not in (int, float)
                                    or not math.isfinite(bitrate) or not 0 < bitrate <= 1000):
            return None, {"ok": False, "error": "invalid_parameters",
                          "detail": "bitrate_mbps must be in (0, 1000]"}
        parallel = parameters.get("parallel", 1)
        if type(parallel) is not int or not 1 <= parallel <= 8:
            return None, {"ok": False, "error": "invalid_parameters",
                          "detail": "parallel must be an integer in 1..8"}
        default_port = DEFAULT_IPERF3_PORT
        env_port = os.environ.get("REMOTERF_OAI_IPERF3_PORT")
        if env_port is not None and env_port.strip():
            try:
                default_port = int(env_port.strip(), 10)
            except ValueError:
                return None, {"ok": False, "error": "invalid_configuration",
                              "detail": "REMOTERF_OAI_IPERF3_PORT must be an integer"}
        port = parameters.get("port", default_port)
        if type(port) is not int or not 1 <= port <= 65535:
            return None, {"ok": False, "error": "invalid_parameters",
                          "detail": "port must be an integer in 1..65535"}
        return {
            "duration_s": duration_s,
            "direction": direction,
            "protocol": protocol,
            "bitrate_mbps": bitrate,
            "parallel": parallel,
            "port": port,
        }, None

    def _prepare_iperf_argv(self, spec):
        if self._profile != "ue":
            return None, None, {"ok": False, "error": "ue_profile_required",
                                "detail": "traffic generation requires the ue profile"}
        dn_ip, ip_err = _configured_dn_ip()
        if ip_err is not None:
            return None, None, {"ok": False, "error": ip_err,
                                "detail": "REMOTERF_OAI_DN_IP must be configured to a valid IP address"}
        iperf3 = _find_executable("REMOTERF_OAI_IPERF3", "iperf3",
                                  ("/usr/bin/iperf3", "/usr/local/bin/iperf3"))
        if iperf3 is None:
            return None, None, {"ok": False, "error": "iperf3_missing",
                                "detail": "iperf3 executable not found"}
        ue_ip = interface_ipv4(self._ifname) if self._ifname else None
        if ue_ip is None:
            return None, None, {"ok": False, "error": "ue_interface_no_ip",
                                "detail": "UE tunnel interface has no IPv4 address"}
        argv = [
            iperf3, "-c", dn_ip, "-B", ue_ip, "-p", str(spec["port"]),
            "-t", str(spec["duration_s"]), "-P", str(spec["parallel"]),
            "-i", "0", "-J", "--connect-timeout", "5000",
        ]
        if spec["direction"] == "dl":
            argv.append("-R")
        if spec["protocol"] == "udp":
            argv.append("-u")
        if spec["bitrate_mbps"] is not None:
            argv.extend(["-b", f"{spec['bitrate_mbps']}M"])
        return argv, dn_ip, None

    def _run_finite_job(self, argv, env, wait):
        """Run one finite supervised job and return a plain dict without raising."""
        processes = self._context.processes
        if processes is None or not Path(self._context.work_dir).is_absolute():
            return {"ok": False, "error": "supervision_unavailable"}
        try:
            process_id = processes.launch(argv, env=env, cwd=Path(self._context.work_dir))
        except Exception:
            return {"ok": False, "error": "job_launch_failed"}
        deadline = time.monotonic() + wait
        while True:
            if self._context.cancelled.is_set():
                return {"ok": False, "error": "cancelled"}
            rows = processes.snapshot()["processes"]
            row = next((r for r in rows if r["id"] == process_id), None)
            if row is None:
                return {"ok": False, "error": "job_missing"}
            done = time.monotonic() >= deadline
            if row["returncode"] is not None and (row["logs_drained"] or done):
                break
            if done:
                return {"ok": False, "error": "timeout"}
            time.sleep(0.05)
        return {
            "ok": True,
            "returncode": row["returncode"],
            "stdout": processes.log_tail(process_id, "stdout"),
            "stderr": processes.log_tail(process_id, "stderr"),
        }

    def _throughput_test(self, parameters):
        spec, err = self._validate_iperf_params(
            parameters,
            max_duration=MAX_THROUGHPUT_SECONDS,
            default_duration=DEFAULT_THROUGHPUT_SECONDS,
        )
        if err is not None:
            return err
        argv, dn_ip, err = self._prepare_iperf_argv(spec)
        if err is not None:
            return err
        if self._jobs >= MAX_JOBS:
            return {"ok": False, "error": "job_budget_exhausted"}
        self._jobs += 1
        wait = min(spec["duration_s"] + TRAFFIC_GRACE_S, self._bound("action_timeout_s"))
        run = self._run_finite_job(argv, {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}, wait)
        if not run["ok"]:
            return run
        metrics, parse_err, detail = parse_iperf3_json(run["stdout"])
        if parse_err is not None:
            err_tail = (run["stderr"] or run["stdout"])[-OUTPUT_BYTES:].decode("utf-8", errors="replace").strip()
            return {"ok": False, "error": parse_err, "detail": detail,
                    "returncode": run["returncode"], "output": err_tail[:512]}
        if run["returncode"] != 0:
            return {"ok": False, "error": "iperf3_failed", "returncode": run["returncode"], **metrics}
        return {
            "ok": True,
            "direction": spec["direction"],
            "protocol": spec["protocol"],
            "target_ip": dn_ip,
            **metrics,
        }

    def _start_traffic(self, parameters):
        spec, err = self._validate_iperf_params(
            parameters,
            max_duration=MAX_TRAFFIC_SECONDS,
            default_duration=DEFAULT_TRAFFIC_SECONDS,
        )
        if err is not None:
            return err
        if self._traffic is not None:
            return {"ok": False, "error": "traffic_already_running",
                    "detail": "background traffic is already running"}
        if self._traffic_used:
            return {"ok": False, "error": "traffic_already_used",
                    "detail": "only one background traffic run is allowed per operation"}
        argv, dn_ip, err = self._prepare_iperf_argv(spec)
        if err is not None:
            return err
        if self._jobs >= MAX_JOBS:
            return {"ok": False, "error": "job_budget_exhausted"}
        processes = self._context.processes
        work_dir = Path(self._context.work_dir)
        if processes is None or not work_dir.is_absolute():
            return {"ok": False, "error": "supervision_unavailable"}

        stop_file = work_dir / "traffic.stop"
        out_file = work_dir / "traffic.json"
        stop_file.unlink(missing_ok=True)
        out_file.unlink(missing_ok=True)
        wrapper_argv = [
            sys.executable, "-I", "-c", TRAFFIC_BOOTSTRAP,
            str(stop_file), str(out_file), str(spec["duration_s"]), *argv,
        ]
        try:
            process_id = processes.launch(
                wrapper_argv,
                env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
                cwd=work_dir,
            )
        except Exception:
            return {"ok": False, "error": "job_launch_failed"}

        self._jobs += 1
        self._traffic_used = True
        self._traffic = {
            "process_id": process_id,
            "stop_file": stop_file,
            "out_file": out_file,
            "direction": spec["direction"],
            "protocol": spec["protocol"],
            "duration_s": spec["duration_s"],
            "target_ip": dn_ip,
        }
        return {
            "ok": True,
            "state": "running",
            "direction": spec["direction"],
            "protocol": spec["protocol"],
            "duration_s": spec["duration_s"],
            "target_ip": dn_ip,
        }

    def _stop_traffic(self, parameters):
        if parameters:
            return {"ok": False, "error": "invalid_parameters", "detail": "stop_traffic accepts no parameters"}
        traffic = self._traffic
        if traffic is None:
            return {"ok": False, "error": "traffic_not_running",
                    "detail": "no background traffic job is running"}

        processes = self._context.processes
        process_id = traffic["process_id"]
        stop_file = traffic["stop_file"]
        out_file = traffic["out_file"]
        try:
            stop_file.write_text("stop", encoding="utf-8")
        except OSError:
            pass

        wait = min(TRAFFIC_GRACE_S, self._bound("action_timeout_s"))
        deadline = time.monotonic() + wait
        row = None
        while True:
            if self._context.cancelled.is_set():
                self._traffic = None
                return {"ok": False, "error": "cancelled"}
            rows = processes.snapshot()["processes"]
            row = next((r for r in rows if r["id"] == process_id), None)
            if row is None:
                self._traffic = None
                return {"ok": False, "error": "job_missing"}
            done = time.monotonic() >= deadline
            if row["returncode"] is not None and (row["logs_drained"] or done):
                break
            if done:
                return {"ok": False, "error": "timeout",
                        "detail": "background traffic process did not exit before deadline"}
            time.sleep(0.05)

        self._traffic = None
        raw_json = b""
        if out_file.is_file():
            try:
                with out_file.open("rb") as handle:
                    raw_json = handle.read(MAX_IPERF3_JSON_BYTES)
            except OSError:
                raw_json = b""
        if not raw_json:
            raw_json = processes.log_tail(process_id, "stdout")
        stderr = processes.log_tail(process_id, "stderr")

        metrics, parse_err, detail = parse_iperf3_json(raw_json, allow_interrupt=True)
        if parse_err is not None:
            err_tail = (stderr or raw_json)[-OUTPUT_BYTES:].decode("utf-8", errors="replace").strip()
            return {"ok": False, "error": parse_err, "detail": detail,
                    "stopped": True, "returncode": row["returncode"], "output": err_tail[:512]}
        return {
            "ok": True,
            "stopped": True,
            "direction": traffic["direction"],
            "protocol": traffic["protocol"],
            "target_ip": traffic["target_ip"],
            "returncode": row["returncode"],
            **metrics,
        }

    def _capture_air_pcap(self, parameters):
        if not _allow_pcap():
            return {"ok": False, "error": "pcap_disabled",
                    "detail": "packet capture is disabled; set REMOTERF_OAI_ALLOW_PCAP=1 to enable"}
        allowed = {"duration_s", "max_bytes", "snaplen"}
        unknown = sorted(set(parameters) - allowed)
        if unknown:
            return {"ok": False, "error": "invalid_parameters",
                    "detail": f"unsupported parameter(s): {', '.join(unknown)}"}
        duration_s = parameters.get("duration_s", DEFAULT_PCAP_SECONDS)
        if type(duration_s) is not int or not 1 <= duration_s <= MAX_PCAP_SECONDS:
            return {"ok": False, "error": "invalid_parameters",
                    "detail": f"duration_s must be an integer in 1..{MAX_PCAP_SECONDS}"}
        max_bytes = parameters.get("max_bytes", DEFAULT_PCAP_BYTES)
        if type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_PCAP_BYTES:
            return {"ok": False, "error": "invalid_parameters",
                    "detail": f"max_bytes must be an integer in 1024..{MAX_PCAP_BYTES}"}
        snaplen = parameters.get("snaplen", 0)
        if type(snaplen) is not int or not (snaplen == 0 or 64 <= snaplen <= 65535):
            return {"ok": False, "error": "invalid_parameters",
                    "detail": "snaplen must be 0 or an integer in 64..65535"}

        if self._profile == "ue":
            ifname = self._ifname
            if not ifname or not IFNAME.fullmatch(ifname):
                return {"ok": False, "error": "ue_interface_missing",
                        "detail": "UE tunnel interface is not available"}
        elif self._profile == "gnb":
            ifname = os.environ.get("REMOTERF_OAI_GNB_PCAP_IFNAME", "").strip()
            if not ifname:
                return {"ok": False, "error": "gnb_pcap_ifname_missing",
                        "detail": "REMOTERF_OAI_GNB_PCAP_IFNAME must be configured for gNB capture"}
            if not IFNAME.fullmatch(ifname):
                return {"ok": False, "error": "invalid_configuration",
                        "detail": "REMOTERF_OAI_GNB_PCAP_IFNAME is not a valid interface name"}
        else:
            return {"ok": False, "error": "not_started", "detail": "OAI profile has not started"}

        tcpdump = _find_executable("REMOTERF_OAI_TCPDUMP", "tcpdump",
                                   ("/usr/bin/tcpdump", "/usr/sbin/tcpdump"))
        if tcpdump is None:
            return {"ok": False, "error": "tcpdump_missing", "detail": "tcpdump executable not found"}
        if not has_cap_net_raw(tcpdump):
            return {"ok": False, "error": "cap_net_raw_missing",
                    "detail": "CAP_NET_RAW is required for packet capture"}
        if not self._artifact_room(max_bytes):
            return {"ok": False, "error": "artifact_limit"}
        if self._jobs >= MAX_JOBS:
            return {"ok": False, "error": "job_budget_exhausted"}

        work_dir = Path(self._context.work_dir)
        self._jobs += 1
        self._pcap_count += 1
        name = f"capture-{self._pcap_count}.pcap"
        tmp_pcap = work_dir / f".{name}.tmp"
        tmp_pcap.unlink(missing_ok=True)
        argv = [
            sys.executable, "-I", "-c", PCAP_BOOTSTRAP,
            str(tmp_pcap), str(duration_s), str(max_bytes),
            tcpdump, "-i", ifname, "-U", "-n", "-s", str(snaplen), "-w", str(tmp_pcap),
        ]
        wait = min(duration_s + PCAP_GRACE_S, self._bound("action_timeout_s"))
        try:
            run = self._run_finite_job(argv, {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}, wait)
            if not run["ok"]:
                return run
            stderr_text = run["stderr"][-OUTPUT_BYTES:].decode("utf-8", errors="replace")
            if run["returncode"] != 0:
                lower = stderr_text.lower()
                if "permission denied" in lower or "operation not permitted" in lower or "cap_net_raw" in lower:
                    return {"ok": False, "error": "cap_net_raw_missing",
                            "detail": "tcpdump lacked permission to capture packets"}
                return {"ok": False, "error": "tcpdump_failed",
                        "returncode": run["returncode"], "output": stderr_text.strip()[:512]}
            if not tmp_pcap.is_file():
                return {"ok": False, "error": "pcap_missing", "detail": "tcpdump produced no capture file"}
            size = tmp_pcap.stat().st_size
            if size > MAX_PCAP_BYTES or not self._artifact_room(size):
                return {"ok": False, "error": "artifact_limit"}
            directory = work_dir / "artifacts"
            directory.mkdir(exist_ok=True)
            os.replace(tmp_pcap, directory / name)
            return {
                "ok": True,
                "artifact": {"name": name, "format": "pcap"},
                "interface": ifname,
                "bytes": size,
                "duration_s": duration_s,
                "capped": size >= max_bytes,
            }
        finally:
            tmp_pcap.unlink(missing_ok=True)

    def _set_live_control(self, parameters):
        if self._profile != "gnb":
            return {"ok": False, "error": "gnb_profile_required",
                    "detail": "set_live_control is supported on the gnb profile only"}
        cell_config = self._resolve_cell_config()
        if not cell_config.get("ok"):
            return {"ok": False, "error": "cell_config_unavailable",
                    "detail": cell_config.get("detail") or cell_config.get("error", "config unavailable")}
        try:
            normalized, commands = validate_live_control(parameters, cell_config)
        except ControlError as exc:
            return {"ok": False, "error": exc.code, "detail": exc.detail, **exc.extra}

        try:
            args = _operator_args("REMOTERF_OAI_GNB_ARGS", DEFAULT_GNB_ARGS)
            endpoint = parse_telnet_endpoint(args)
        except ControlError as exc:
            return {"ok": False, "error": exc.code, "detail": exc.detail, **exc.extra}
        except RuntimeError as exc:
            return {"ok": False, "error": "config_unavailable", "detail": str(exc)}

        if not endpoint["enabled"]:
            return {"ok": False, "error": "telnet_not_enabled",
                    "detail": "OAI telnet server is not enabled in REMOTERF_OAI_GNB_ARGS (--telnetsrv)"}

        target = (endpoint["host"], endpoint["port"])
        if self._telnet is None or self._telnet_endpoint != target:
            if self._telnet is not None:
                self._telnet.close()
            self._telnet = TelnetClient(endpoint["host"], endpoint["port"], timeout_s=TELNET_TIMEOUT_S)
            self._telnet_endpoint = target

        try:
            executed = self._telnet.execute(commands, cancelled=self._context.cancelled)
        except ControlError as exc:
            return {"ok": False, "error": exc.code, "detail": exc.detail, **exc.extra}
        return {"ok": True, "applied": normalized, "commands": executed}

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

    def _csi_entry(self, ring, entry_bytes, n_entries, seq, deadline=None):
        """Copy entry seq; None if it is being written, torn, overwritten or malformed."""
        if seq >= 1 << 63:
            return None
        offset = CSI_HEADER_BYTES + (seq % n_entries) * entry_bytes
        for _ in range(CSI_RETRIES):
            if self._context.cancelled.is_set() or (deadline is not None and time.monotonic() >= deadline):
                return None
            lock = struct.unpack_from("<Q", ring, offset)[0]
            if lock & 1:
                time.sleep(CSI_POLL_S / 10)
                continue
            data = ring[offset:offset + entry_bytes]
            if struct.unpack_from("<Q", ring, offset)[0] != lock:
                continue
            _, got, frame, slot, rnti, snr_db, ta, *dims = CSI_ENTRY.unpack_from(data)
            size = 4 * dims[0] * dims[1] * dims[2]
            if got != seq or size == 0 or CSI_ENTRY.size + size > entry_bytes:
                return None
            return {"seq": seq, "frame": frame, "slot": slot, "rnti": rnti,
                    "snr_db": round(snr_db, 2) if math.isfinite(snr_db) else None,
                    "ta": ta, "dims": tuple(dims), "timestamp_s": time.time(),
                    "iq": data[CSI_ENTRY.size:CSI_ENTRY.size + size]}
        return None

    def _csi_latest(self, ring, entry_bytes, n_entries, wait):
        deadline = time.monotonic() + wait
        while True:
            if self._context.cancelled.is_set():
                raise RuntimeError("OAI CSI read was cancelled")
            if time.monotonic() >= deadline:
                return None
            seq = CSI_HEADER.unpack_from(ring)[5]
            entry = self._csi_entry(ring, entry_bytes, n_entries, seq, deadline) if seq else None
            if entry is not None:
                return [entry]
            if self._context.cancelled.is_set() or time.monotonic() >= deadline:
                continue
            time.sleep(CSI_POLL_S)

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
                if self._context.cancelled.is_set():
                    raise RuntimeError("OAI CSI capture was cancelled")
                if time.monotonic() >= deadline:
                    return None, dropped
                entry = self._csi_entry(ring, entry_bytes, n_entries, seq, deadline)
                if entry is None:
                    if self._context.cancelled.is_set() or time.monotonic() >= deadline:
                        break
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
        if self._telnet is not None:
            self._telnet.close()
            self._telnet = None
            self._telnet_endpoint = None
        self._traffic = None
        if self._context.processes is None:
            self._stopped = True
            return
        if not self._context.processes.stop(timeout=self._bound("stop_timeout_s")):
            raise RuntimeError("OAI cleanup could not be verified")
        work_dir = Path(self._context.work_dir)
        if work_dir.is_absolute():
            shm = work_dir / "shm"
            for stream in RING_STREAMS:
                (shm / f"{stream}.ring").unlink(missing_ok=True)
            try:
                shm.rmdir()
            except OSError:
                pass
        if self._ifname is not None and self._process_id is not None:
            deadline = time.monotonic() + UE_IFACE_GONE_S
            while interface_exists(self._ifname):
                if time.monotonic() >= deadline:
                    raise RuntimeError("UE interface still present")
                time.sleep(0.1)
        self._stopped = True


def create_adapter(context):
    return OAI(context)
