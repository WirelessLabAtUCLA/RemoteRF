"""Artifact confinement, bounds and discard; local files only."""
import base64
import contextlib
import os
import shutil
import socket

import pytest

from remoterf_device_core.integrations import IntegrationError
from remoterf_device_core.integrations import artifacts
from remoterf_device_core.integrations.payload import MAX_FETCH_REPLY_BYTES
from remoterf_device_core.integrations.wire import validate_result


@pytest.fixture
def work(tmp_path):
    (tmp_path / 'artifacts').mkdir(mode=0o700)
    return tmp_path


def refused(code, call, *args):
    with pytest.raises(IntegrationError) as caught:
        call(*args)
    assert caught.value.code == code
    return str(caught.value)


def test_chunked_read_eof_and_offset_past_end(work):
    payload = os.urandom(500000)
    (work / 'artifacts' / 'grid-0001.bin').write_bytes(payload)
    data, size = artifacts.read_chunk(work, 'grid-0001.bin', 0, artifacts.MAX_FETCH_BYTES)
    assert artifacts.MAX_FETCH_BYTES == 384 * 1024
    assert size == len(payload) and data == payload[:artifacts.MAX_FETCH_BYTES]
    # A full chunk with worst-case names fits the fetch reply budget, not the normal one.
    reply = {'operation_id': 'x' * 128, 'artifact': 'a' * 128, 'offset': 2**62, 'size': 2**62,
             'eof': False, 'data': base64.b64encode(data).decode()}
    assert validate_result(reply, 'integration_fetch') == reply
    with pytest.raises(IntegrationError):
        validate_result(reply)
    # Encoded reply keeps >=25% headroom under the relay's 768 KiB response frame.
    assert MAX_FETCH_REPLY_BYTES <= 0.75 * 786432
    data, size = artifacts.read_chunk(work, 'grid-0001.bin', 400000, artifacts.MAX_FETCH_BYTES)
    assert data == payload[400000:] and 400000 + len(data) >= size
    assert artifacts.read_chunk(work, 'grid-0001.bin', 10**6, 16) == (b'', size)


@pytest.mark.parametrize('name', ['../x', '.hidden', 'a/b', '.', '..', '', 'x' * 129, 7, None])
def test_names_are_validated_before_io(work, name):
    refused('invalid_request', artifacts.read_chunk, work, name, 0, 1)


@pytest.mark.parametrize('offset,limit', [(-1, 1), (2**63, 1), (True, 1), (0, 0), (0, 384 * 1024 + 1), (0, False), (1.0, 1)])
def test_offset_and_limit_bounds(work, offset, limit):
    (work / 'artifacts' / 'a').write_bytes(b'x')
    refused('invalid_request', artifacts.read_chunk, work, 'a', offset, limit)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'socket', 'fifo', 'directory', 'missing', 'missing_dir',
                                  'linked_dir'])
def test_non_regular_and_unsafe_entries_are_unknown_without_paths(work, tmp_path, kind, monkeypatch):
    outside = tmp_path / 'secret.txt'
    outside.write_text('secret')
    directory = work / 'artifacts'
    if kind == 'symlink':
        (directory / 'a').symlink_to(outside)
    elif kind == 'hardlink':
        os.link(outside, directory / 'a')
    elif kind == 'socket':
        monkeypatch.chdir(directory)  # AF_UNIX paths are short
        server = socket.socket(socket.AF_UNIX)
        server.bind('a')
        monkeypatch.chdir(tmp_path)
    elif kind == 'fifo':
        os.mkfifo(directory / 'a')
    elif kind == 'directory':
        (directory / 'a').mkdir()
    elif kind == 'missing_dir':
        directory.rmdir()
    elif kind == 'linked_dir':
        directory.rmdir()
        (tmp_path / 'elsewhere').mkdir()
        (tmp_path / 'elsewhere' / 'a').write_text('secret')
        directory.symlink_to(tmp_path / 'elsewhere')
    message = refused('unknown_artifact', artifacts.read_chunk, work, 'a', 0, 16)
    assert '/' not in message
    if kind == 'socket':
        server.close()


def test_usage_bounds(work, monkeypatch):
    artifacts.usage(work)
    artifacts.usage(work / 'never-prepared')
    (work / 'artifacts' / 'a').write_bytes(b'x' * 10)
    monkeypatch.setattr(artifacts, 'QUOTA_BYTES', 9)
    refused('artifact_limit', artifacts.usage, work)
    monkeypatch.setattr(artifacts, 'QUOTA_BYTES', 10)
    artifacts.usage(work)
    monkeypatch.setattr(artifacts, 'MAX_ENTRIES', 1)
    (work / 'artifacts' / 'b').write_bytes(b'')
    refused('artifact_limit', artifacts.usage, work)


def test_usage_tolerates_entries_renamed_during_scan(work, monkeypatch):
    (work / 'artifacts' / 'a').write_bytes(b'x')
    (work / 'artifacts' / 'gone.tmp').write_bytes(b'x')
    real = os.scandir

    def racing(fd):
        with real(fd) as entries:
            listed = list(entries)
        os.unlink(work / 'artifacts' / 'gone.tmp')
        return contextlib.nullcontext(listed)

    monkeypatch.setattr(os, 'scandir', racing)
    artifacts.usage(work)


@pytest.mark.parametrize('kind', ['directory', 'symlink', 'hardlink', 'fifo'])
def test_usage_counts_non_regular_entries_as_violations(work, tmp_path, kind):
    target = tmp_path / 'outside'
    target.write_text('x')
    entry = work / 'artifacts' / 'a'
    {'directory': lambda: entry.mkdir(), 'symlink': lambda: entry.symlink_to(target),
     'hardlink': lambda: os.link(target, entry), 'fifo': lambda: os.mkfifo(entry)}[kind]()
    message = refused('artifact_limit', artifacts.usage, work)
    assert str(tmp_path) not in message


def test_discard_removes_unwritable_content_without_following_links(work, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    directory = work / 'artifacts'
    (directory / 'a').write_text('x')
    (directory / 'link').symlink_to(outside)
    (directory / 'locked').mkdir()
    (directory / 'locked' / 'f').write_text('x')
    (directory / 'locked' / 'inner').mkdir()
    os.chmod(directory / 'locked' / 'inner', 0)
    os.chmod(directory / 'locked', 0o500)
    artifacts.discard(work)
    assert sorted(path.name for path in work.iterdir()) == ['outside']
    assert (outside / 'keep').read_text() == 'keep'
    artifacts.discard(work)  # already gone: no-op
    artifacts.discard(tmp_path / 'never-prepared')


def test_discard_only_a_failed_rename_raises(work, monkeypatch):
    (work / 'artifacts' / 'a').write_text('x')
    with monkeypatch.context() as patch:
        patch.setattr(artifacts.os, 'rename', lambda *args: (_ for _ in ()).throw(PermissionError(13, 'denied', str(work))))
        message = refused('recovery_required', artifacts.discard, work)
        assert str(work) not in message
    with monkeypatch.context() as patch:
        patch.setattr(artifacts.shutil, 'rmtree', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('busy')))
        artifacts.discard(work)
    assert not (work / 'artifacts').exists()
    leftover = [path for path in work.iterdir() if path.name.startswith('.discarded-')]
    assert len(leftover) == 1 and (leftover[0] / 'a').exists()
    shutil.rmtree(leftover[0])
