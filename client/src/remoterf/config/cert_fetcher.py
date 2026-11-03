# Copyright (C) 2026 RemoteRF
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# cert_fetcher.py

from __future__ import annotations

import hashlib
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

CERT_FILENAME_DEFAULT = "ca.crt"

def _default_config_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".config" / "remoterf-client"

def _default_env_path() -> Path:
    return _default_config_dir() / ".env"

def _ensure_parent_dir(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)

def _looks_like_pem_cert(data: bytes) -> bool:
    return b"BEGIN CERTIFICATE" in data and b"END CERTIFICATE" in data


def looks_like_pem_cert(data: bytes) -> bool:
    """Public alias of `_looks_like_pem_cert` for generic callers."""
    return _looks_like_pem_cert(data)


def fetch_ca_bytes(host: str, port: int, *, timeout_sec: float = 3.0) -> bytes:
    """Fetch raw CA certificate bytes (HTTP first, then raw-TCP fallback),
    without writing anything to disk. Shared by direct-mode's
    `fetch_and_save_ca_cert` below.
    """
    try:
        return _fetch_http(host, port, timeout_sec)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (OSError, socket.timeout)):
            raise  # nothing answered: a raw retry would only wait the timeout again
        return _fetch_raw_tcp(host, port, timeout_sec)
    except Exception:
        return _fetch_raw_tcp(host, port, timeout_sec)

def sha256_fingerprint_pem(pem_bytes: bytes) -> str:
    h = hashlib.sha256(pem_bytes).hexdigest()
    return ":".join(h[i:i+2] for i in range(0, len(h), 2))

def normalize_sha256(value: str) -> str:
    """'sha256:AB:CD..' / 'abcd..' -> 'abcd..'; '' when it is not a sha256."""
    v = (value or "").strip().lower()
    if v.startswith("sha256:"):
        v = v[len("sha256:"):]
    v = v.replace(":", "")
    return v if re.fullmatch(r"[0-9a-f]{64}", v) else ""

def _dotenv_get_key(env_path: Path, key: str) -> str:
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.startswith(key + "="):
            return stripped.split("=", 1)[1].strip().strip("'\"")
    return ""

def _fetch_http(host: str, port: int, timeout_sec: float) -> bytes:
    url = f"http://{host}:{port}/ca.crt"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return resp.read()

def _bare_host(host: str) -> str:
    """Strip IPv6 literal brackets (e.g. '[::1]' -> '::1') for raw-socket use."""
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host

def _fetch_raw_tcp(host: str, port: int, timeout_sec: float) -> bytes:
    chunks: list[bytes] = []
    with socket.create_connection((_bare_host(host), port), timeout=timeout_sec) as s:
        s.settimeout(timeout_sec)
        while True:
            try:
                b = s.recv(4096)
            except socket.timeout:
                break
            if not b:
                break
            chunks.append(b)
    return b"".join(chunks)

def _dotenv_escape_value_no_quotes(value: str) -> str:
    """
    Returns a .env-safe value WITHOUT surrounding quotes.
    We use forward slashes to avoid backslash-escape weirdness.
    """
    # Trim outer whitespace; keep internal spaces.
    v = value.strip()

    # Convert Windows backslashes to forward slashes.
    v = v.replace("\\", "/")

    # Remove any accidental surrounding quotes.
    if (len(v) >= 2) and ((v[0] == v[-1]) and v[0] in ("'", '"')):
        v = v[1:-1]

    # Disallow newlines (dotenv lines are single-line)
    v = v.replace("\r", "").replace("\n", "")

    return v

def _dotenv_set_key(env_path: Path, key: str, value: str) -> None:
    """
    Create/update KEY=value in env_path. Writes value WITHOUT quotes.
    Preserves unrelated lines. Comments are kept.
    """
    _ensure_parent_dir(env_path)

    new_line = f"{key}={_dotenv_escape_value_no_quotes(value)}\n"

    if not env_path.exists():
        env_path.write_text(new_line, encoding="utf-8")
        return

    lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)

    # Replace existing KEY=... (ignores leading whitespace); doesn't touch commented-out keys.
    replaced = False
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            out.append(line)
            continue

        # Match "KEY=" at start (after lstrip). Keep any leading whitespace.
        if stripped.startswith(key + "="):
            prefix_ws = line[: len(line) - len(stripped)]
            out.append(prefix_ws + new_line)
            replaced = True
        else:
            out.append(line)

    if not replaced:
        # Append with a newline boundary
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(new_line)

    env_path.write_text("".join(out), encoding="utf-8")

def fetch_and_save_ca_cert(
    host: str,
    port: int,
    *,
    out_path: Optional[str | Path] = None,
    profile: Optional[str] = None,
    timeout_sec: float = 3.0,
    overwrite: bool = True,
    write_env: bool = True,
    env_path: Optional[str | Path] = None,
    expected_sha256: Optional[str] = None,
) -> bool:
    """
    Fetch CA cert and save. Optionally writes REMOTERF_CA_CERT into ~/.config/remoterf-client/.env
    WITHOUT quotes.

    Args:
        write_env: if True, updates REMOTERF_CA_CERT in env file to point at saved cert
            and pins its digest as REMOTERF_CA_SHA256
        env_path: override env file location; default ~/.config/remoterf-client/.env
        expected_sha256: the lab's CA fingerprint; without it the CA pinned
            by an earlier fetch (REMOTERF_CA_SHA256) must still match.
    """
    try:
        if not isinstance(port, int):
            port = int(port)

        if expected_sha256 is not None and not normalize_sha256(expected_sha256):
            print(f"Not a sha256 fingerprint: {expected_sha256!r}", file=sys.stderr)
            return False

        ep = Path(env_path).expanduser().resolve() if env_path else _default_env_path()

        # Determine destination path
        if out_path is not None:
            dest = Path(out_path).expanduser().resolve()
        else:
            cfg = _default_config_dir()
            certs_dir = cfg / "certs"
            name = f"{profile}.crt" if profile else CERT_FILENAME_DEFAULT
            dest = certs_dir / name

        _ensure_parent_dir(dest)

        if dest.exists() and not overwrite:
            # Still ensure env points to it (helpful for idempotency)
            if write_env:
                _dotenv_set_key(ep, "REMOTERF_CA_CERT", dest.as_posix())
            return True

        # Fetch (HTTP first, then raw TCP fallback)
        data = fetch_ca_bytes(host, port, timeout_sec=timeout_sec)

        if not data or not _looks_like_pem_cert(data):
            return False

        # The fetch is plain HTTP: trust only a fingerprint from the lab admin,
        # or the one pinned on first use.
        digest = hashlib.sha256(data).hexdigest()
        pinned = _dotenv_get_key(ep, "REMOTERF_CA_SHA256")
        want = normalize_sha256(expected_sha256 if expected_sha256 is not None else pinned)
        if want and digest != want:
            print(
                f"CA fingerprint mismatch: got {digest}, expected {want}.\n"
                "If the lab changed its CA, pass the new fingerprint from the lab admin:\n"
                "  remoterf --config --addr <host:port> --ca-sha256 <hex>\n"
                "or reset the local config with: remoterf --config --wipe",
                file=sys.stderr,
            )
            return False

        dest.write_bytes(data)

        # Update .env without quotes
        if write_env:
            _dotenv_set_key(ep, "REMOTERF_CA_SHA256", digest)
            _dotenv_set_key(ep, "REMOTERF_CA_CERT", dest.as_posix())

        return True

    except Exception:
        return False
