"""Bounded MAC/PHY live control validation and local OAI telnet socket client."""
from __future__ import annotations

import ipaddress
import re
import socket
import time


DEFAULT_TELNET_HOST = "127.0.0.1"
DEFAULT_TELNET_PORT = 9090
TELNET_TIMEOUT_S = 2.0
MAX_TELNET_REPLY = 8192

MAX_MCS = 28
MAX_LDPC_ITERATIONS = 20
LIVE_CONTROL_KEYS = frozenset({
    "mcs_range",
    "ul_prb_blacklist",
    "dl_prb_blacklist",
    "max_ldpc_iterations",
    "tpmi",
    "ri",
})

_IAC_RE = re.compile(rb"\xff(?:[\xfb-\xfe].|[\xf0-\xfa])")
_PROMPT_LINE_RE = re.compile(r"^[A-Za-z0-9_.:/-]*>\s*$")
_ERROR_RE = re.compile(r"\b(?:error|unknown\s+command|invalid|failed|not\s+found|usage:)\b", re.IGNORECASE)


class ControlError(Exception):
    """Typed control/telnet failure converted into an adapter error dict."""

    def __init__(self, code: str, detail: str, **extra):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = extra


def parse_telnet_endpoint(args: list[str] | tuple[str, ...]) -> dict:
    """Inspect operator args to determine whether OAI's telnet server is enabled locally."""
    enabled = False
    host = DEFAULT_TELNET_HOST
    port = DEFAULT_TELNET_PORT
    tokens = list(args)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--telnetsrv":
            enabled = True
            i += 1
            continue
        if tok.startswith("--telnetsrv="):
            raw = tok.split("=", 1)[1].strip().lower()
            enabled = raw in {"1", "true", "yes", "on"}
            i += 1
            continue
        if tok == "--telnetsrv.listenaddr" and i + 1 < len(tokens):
            host = tokens[i + 1].strip()
            i += 2
            continue
        if tok.startswith("--telnetsrv.listenaddr="):
            host = tok.split("=", 1)[1].strip()
            i += 1
            continue
        if tok == "--telnetsrv.listenport" and i + 1 < len(tokens):
            raw_port = tokens[i + 1].strip()
            i += 2
            port = _parse_port(raw_port)
            continue
        if tok.startswith("--telnetsrv.listenport="):
            port = _parse_port(tok.split("=", 1)[1].strip())
            i += 1
            continue
        i += 1

    if not enabled:
        return {"enabled": False, "host": None, "port": None}

    if host in {"0.0.0.0", "localhost"}:
        connect_host = "127.0.0.1"
    elif host == "::":
        connect_host = "::1"
    else:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            raise ControlError("telnet_non_local", "OAI telnet listenaddr is not a valid local IP") from None
        if not addr.is_loopback:
            raise ControlError("telnet_non_local", "OAI telnet socket must be a loopback address")
        connect_host = str(addr)

    return {"enabled": True, "host": connect_host, "port": port}


def _parse_port(raw: str) -> int:
    try:
        value = int(raw, 10)
    except ValueError:
        raise ControlError("telnet_invalid_port", "OAI telnet listenport is not an integer") from None
    if not 1 <= value <= 65535:
        raise ControlError("telnet_invalid_port", "OAI telnet listenport must be in 1..65535")
    return value


def _validate_prb_list(name: str, value, prb_count: int | None) -> list[int]:
    if prb_count is None or type(prb_count) is not int or prb_count < 1:
        raise ControlError("cell_config_unavailable", f"cannot validate {name} without cell PRB count")
    if type(value) is not list:
        raise ControlError("invalid_parameters", f"{name} must be a list of PRB indices")
    if len(value) > prb_count:
        raise ControlError("invalid_parameters", f"{name} exceeds cell PRB count ({prb_count})")
    seen: set[int] = set()
    for item in value:
        if type(item) is not int or not 0 <= item < prb_count:
            raise ControlError(
                "invalid_parameters",
                f"{name} PRB index out of bounds (expected 0..{prb_count - 1})",
            )
        if item in seen:
            raise ControlError("invalid_parameters", f"{name} contains duplicate PRB index {item}")
        seen.add(item)
    if len(seen) >= prb_count:
        raise ControlError("invalid_parameters", f"{name} cannot blacklist all {prb_count} PRBs")
    return sorted(seen)


def _max_rank_from_config(cell_config: dict) -> int:
    mimo = cell_config.get("mimo_mode") or {}
    nb_tx = mimo.get("nb_tx")
    nb_rx = mimo.get("nb_rx")
    pdsch = mimo.get("pdsch_antenna_ports")
    if type(nb_tx) is int and nb_tx >= 1 and type(nb_rx) is int and nb_rx >= 1:
        return max(1, min(nb_tx, nb_rx, 8))
    if type(pdsch) is int and pdsch >= 1:
        return max(1, min(pdsch, 8))
    return 4


def _max_tpmi_from_config(cell_config: dict, ri: int | None) -> int:
    mimo = cell_config.get("mimo_mode") or {}
    ports = mimo.get("pusch_antenna_ports")
    if type(ports) is not int or ports < 1:
        nb_tx = mimo.get("nb_tx")
        nb_rx = mimo.get("nb_rx")
        if type(nb_tx) is int and nb_tx >= 1 and type(nb_rx) is int and nb_rx >= 1:
            ports = min(nb_tx, nb_rx)
    # 3GPP TS 38.211 Table 6.3.1.5-1..7 codebook bounds.
    if ports == 1:
        return 0
    if ports == 2:
        return 2 if ri == 2 else 5
    if ports == 4:
        return {2: 21, 3: 6, 4: 4}.get(ri, 27)
    return 27


def validate_live_control(parameters: dict, cell_config: dict) -> tuple[dict, list[str]]:
    """Validate bounded MAC/PHY controls strictly against cell config and 3GPP limits."""
    if type(parameters) is not dict or not parameters:
        raise ControlError("invalid_parameters", "at least one live control parameter is required")
    unknown = sorted(set(parameters) - LIVE_CONTROL_KEYS)
    if unknown:
        raise ControlError(
            "invalid_parameters",
            f"unsupported or forbidden control parameter(s): {', '.join(unknown)}",
        )

    normalized: dict = {}
    commands: list[str] = []

    if "mcs_range" in parameters:
        val = parameters["mcs_range"]
        if (type(val) is not list or len(val) != 2
                or type(val[0]) is not int or type(val[1]) is not int
                or not (0 <= val[0] <= val[1] <= MAX_MCS)):
            raise ControlError(
                "invalid_parameters",
                f"mcs_range must be [min, max] integers with 0 <= min <= max <= {MAX_MCS}",
            )
        lo, hi = val
        normalized["mcs_range"] = [lo, hi]
        commands.extend([
            f"mac dl_min_mcs {lo}",
            f"mac dl_max_mcs {hi}",
            f"mac ul_min_mcs {lo}",
            f"mac ul_max_mcs {hi}",
        ])

    if "ul_prb_blacklist" in parameters:
        ul_prbs = cell_config.get("ul_prb_count")
        if ul_prbs is None:
            ul_prbs = cell_config.get("prb_count")
        prbs = _validate_prb_list("ul_prb_blacklist", parameters["ul_prb_blacklist"], ul_prbs)
        normalized["ul_prb_blacklist"] = prbs
        arg = ",".join(str(p) for p in prbs) if prbs else "none"
        commands.append(f"mac ul_prb_blacklist {arg}")

    if "dl_prb_blacklist" in parameters:
        dl_prbs = cell_config.get("dl_prb_count")
        if dl_prbs is None:
            dl_prbs = cell_config.get("prb_count")
        prbs = _validate_prb_list("dl_prb_blacklist", parameters["dl_prb_blacklist"], dl_prbs)
        normalized["dl_prb_blacklist"] = prbs
        arg = ",".join(str(p) for p in prbs) if prbs else "none"
        commands.append(f"mac dl_prb_blacklist {arg}")

    if "max_ldpc_iterations" in parameters:
        val = parameters["max_ldpc_iterations"]
        if type(val) is not int or not 1 <= val <= MAX_LDPC_ITERATIONS:
            raise ControlError(
                "invalid_parameters",
                f"max_ldpc_iterations must be an integer in 1..{MAX_LDPC_ITERATIONS}",
            )
        normalized["max_ldpc_iterations"] = val
        commands.append(f"phy max_ldpc_iterations {val}")

    ri_val: int | None = None
    if "ri" in parameters:
        val = parameters["ri"]
        max_ri = _max_rank_from_config(cell_config)
        if type(val) is not int or not 1 <= val <= max_ri:
            raise ControlError(
                "invalid_parameters",
                f"ri must be an integer in 1..{max_ri} for this cell MIMO configuration",
            )
        ri_val = val
        normalized["ri"] = val
        commands.append(f"mac ri {val}")

    if "tpmi" in parameters:
        val = parameters["tpmi"]
        max_tpmi = _max_tpmi_from_config(cell_config, ri_val)
        if type(val) is not int or not 0 <= val <= max_tpmi:
            raise ControlError(
                "invalid_parameters",
                f"tpmi must be an integer in 0..{max_tpmi} for this cell antenna configuration",
            )
        normalized["tpmi"] = val
        commands.append(f"mac tpmi {val}")

    return normalized, commands


class TelnetClient:
    """Persistent short-timeout local TCP client for OAI's telnet server."""

    def __init__(self, host: str, port: int, *, timeout_s: float = TELNET_TIMEOUT_S):
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._sock: socket.socket | None = None

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _connect(self, cancelled=None) -> None:
        self.close()
        if cancelled is not None and cancelled.is_set():
            raise ControlError("cancelled", "live control was cancelled")
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        except socket.timeout:
            raise ControlError("telnet_timeout", "timed out connecting to OAI telnet server") from None
        except OSError:
            raise ControlError("telnet_unreachable", "could not connect to OAI telnet server") from None
        self._sock = sock
        # Drain initial banner/prompt; tolerate servers that wait for the first command.
        self._read_until_prompt(timeout_s=min(self.timeout_s, 1.0), allow_empty_timeout=True, cancelled=cancelled)

    def _read_until_prompt(self, *, timeout_s: float, allow_empty_timeout: bool = False, cancelled=None) -> str:
        assert self._sock is not None
        deadline = time.monotonic() + timeout_s
        buf = bytearray()
        while True:
            if cancelled is not None and cancelled.is_set():
                self.close()
                raise ControlError("cancelled", "live control was cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if allow_empty_timeout and not buf:
                    return ""
                self.close()
                raise ControlError("telnet_timeout", "timed out waiting for OAI telnet prompt")
            self._sock.settimeout(min(0.25, remaining))
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                self.close()
                raise ControlError("telnet_disconnected", "OAI telnet connection failed") from None
            if not chunk:
                self.close()
                if buf:
                    break
                raise ControlError("telnet_disconnected", "OAI telnet server closed connection")
            buf.extend(chunk)
            if len(buf) > MAX_TELNET_REPLY:
                self.close()
                raise ControlError("telnet_reply_too_large", "OAI telnet reply exceeded size limit")
            cleaned = _IAC_RE.sub(b"", bytes(buf)).decode("utf-8", errors="replace").replace("\r\n", "\n")
            last_line = cleaned.rsplit("\n", 1)[-1]
            if _PROMPT_LINE_RE.fullmatch(last_line):
                return cleaned
        return _IAC_RE.sub(b"", bytes(buf)).decode("utf-8", errors="replace").replace("\r\n", "\n")

    def _send_once(self, command: str, *, cancelled=None) -> str:
        if "\r" in command or "\n" in command:
            raise ControlError("invalid_parameters", "control command must be single-line")
        if self._sock is None:
            self._connect(cancelled=cancelled)
        assert self._sock is not None
        try:
            self._sock.settimeout(self.timeout_s)
            self._sock.sendall((command + "\n").encode("ascii"))
        except socket.timeout:
            self.close()
            raise ControlError("telnet_timeout", "timed out sending OAI telnet command") from None
        except OSError:
            self.close()
            raise ControlError("telnet_disconnected", "failed writing to OAI telnet server") from None
        raw = self._read_until_prompt(timeout_s=self.timeout_s, cancelled=cancelled)
        return _clean_reply(command, raw)

    def execute(self, commands: list[str], *, cancelled=None) -> list[dict]:
        """Run validated commands on the persistent socket, reconnecting once if stale."""
        results = []
        for idx, cmd in enumerate(commands):
            reused = self._sock is not None
            try:
                reply = self._send_once(cmd, cancelled=cancelled)
            except ControlError as exc:
                if idx == 0 and reused and exc.code == "telnet_disconnected":
                    # Reconnect once when an idle persistent socket was closed by OAI.
                    self._connect(cancelled=cancelled)
                    reply = self._send_once(cmd, cancelled=cancelled)
                else:
                    raise
            if _ERROR_RE.search(reply):
                raise ControlError(
                    "telnet_command_rejected",
                    f"OAI telnet rejected command {cmd!r}",
                    failed_command=cmd,
                    response=reply[:512],
                )
            results.append({"command": cmd, "response": reply[:512]})
        return results


def _clean_reply(command: str, raw: str) -> str:
    lines = raw.split("\n")
    if lines and _PROMPT_LINE_RE.fullmatch(lines[-1]):
        lines = lines[:-1]
    if lines and lines[0].strip() == command.strip():
        lines = lines[1:]
    return "\n".join(line.rstrip() for line in lines).strip()
