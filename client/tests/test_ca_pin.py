"""`remoterf -c -a host:port` fetches the lab CA over plain HTTP: it must pin
the fingerprint on first use and refuse a changed CA unless told its digest."""

import hashlib

import pytest

from remoterf.config import cert_fetcher
from remoterf.config import config as cfg

PEM_A = b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
PEM_B = b"-----BEGIN CERTIFICATE-----\nBBBB\n-----END CERTIFICATE-----\n"
SHA_A = hashlib.sha256(PEM_A).hexdigest()
SHA_B = hashlib.sha256(PEM_B).hexdigest()


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "ca.crt", tmp_path / ".env"


def _fetch(monkeypatch, paths, served, expected=None):
    monkeypatch.setattr(cert_fetcher, "fetch_ca_bytes", lambda *a, **k: served)
    cert, env = paths
    return cert_fetcher.fetch_and_save_ca_cert(
        "lab.example.org", 61006, out_path=cert, env_path=env, expected_sha256=expected,
    )


def test_fresh_config_pins_the_digest(monkeypatch, paths):
    assert _fetch(monkeypatch, paths, PEM_A)
    cert, env = paths
    assert cert.read_bytes() == PEM_A
    assert f"REMOTERF_CA_SHA256={SHA_A}" in env.read_text()


def test_same_cert_again_succeeds(monkeypatch, paths):
    assert _fetch(monkeypatch, paths, PEM_A)
    assert _fetch(monkeypatch, paths, PEM_A)


def test_changed_ca_without_flag_is_refused_and_leaves_files(monkeypatch, paths, capsys):
    assert _fetch(monkeypatch, paths, PEM_A)
    cert, env = paths
    before = env.read_text()
    assert not _fetch(monkeypatch, paths, PEM_B)
    assert cert.read_bytes() == PEM_A
    assert env.read_text() == before
    err = capsys.readouterr().err
    assert SHA_A in err and SHA_B in err and "--ca-sha256" in err


def test_matching_flag_succeeds_and_repins(monkeypatch, paths):
    assert _fetch(monkeypatch, paths, PEM_A)
    assert _fetch(monkeypatch, paths, PEM_B, expected=SHA_B)
    cert, env = paths
    assert cert.read_bytes() == PEM_B
    assert f"REMOTERF_CA_SHA256={SHA_B}" in env.read_text()


def test_mismatching_flag_is_refused(monkeypatch, paths):
    assert not _fetch(monkeypatch, paths, PEM_A, expected=SHA_B)
    cert, env = paths
    assert not cert.exists() and not env.exists()


@pytest.mark.parametrize("bad", ["", "sha256:", ":", "  ", "abc123", "zz" * 32])
def test_malformed_fingerprint_is_rejected(monkeypatch, paths, bad):
    assert not _fetch(monkeypatch, paths, PEM_A, expected=bad)
    assert not paths[0].exists()


def test_colon_separated_fingerprint_is_accepted(monkeypatch, paths):
    colons = cert_fetcher.sha256_fingerprint_pem(PEM_A).upper()
    assert _fetch(monkeypatch, paths, PEM_A, expected=colons)


def _configure(monkeypatch, served, **kwargs):
    monkeypatch.setattr(cfg, "_host_resolves", lambda host: True)
    monkeypatch.setattr(cfg, "_confirm_tos", lambda: True)
    monkeypatch.setattr(cert_fetcher, "fetch_ca_bytes", lambda *a, **k: served)
    return cfg.configure("192.168.1.40", 61005, 61006, **kwargs)


def test_configure_keeps_the_pin_across_reruns(monkeypatch):
    summaries = []
    monkeypatch.setattr(cfg, "_print_config_summary", lambda *a: summaries.append(a))
    assert _configure(monkeypatch, PEM_A) in (None, 0)
    assert f"REMOTERF_CA_SHA256={SHA_A}" in cfg._env_path().read_text()
    assert summaries[-1][-1] == SHA_A  # the fingerprint shown to compare with the lab's
    assert _configure(monkeypatch, PEM_A) in (None, 0)
    assert _configure(monkeypatch, PEM_B) == 1
    assert (cfg._certs_dir() / "default.crt").read_bytes() == PEM_A
    assert _configure(monkeypatch, PEM_B, ca_sha256=SHA_B) in (None, 0)
    assert f"REMOTERF_CA_SHA256={SHA_B}" in cfg._env_path().read_text()
