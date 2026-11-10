"""Bound qualification to installed adapter contents without importing code.

The factory's complete top-level package (including native/resource files) and
installed distribution/dependency metadata contribute to the digest. Namespace
packages may span a bounded number of roots. File reads use directory-relative
no-follow descriptors; symlinks, mutable scans, missing metadata and excessive
work fail closed. Bytecode caches and VCS state are not release inputs.
"""
from __future__ import annotations

from hashlib import sha256
from importlib import metadata
from importlib.machinery import PathFinder
import json
import os
from pathlib import Path
import re
import stat

from .payload import IntegrationError

MAX_FILES = 4096
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_DISTRIBUTIONS = 256
MAX_ROOTS = 16
MAX_DEPTH = 32
_IGNORED = frozenset({'__pycache__', '.git', '.hg', '.svn'})


def _fail():
    raise IntegrationError('adapter_fingerprint_unavailable', 'installed adapter identity could not be verified')


def _specs(module):
    """PathFinder does not execute parent __init__ like util.find_spec can."""
    search = None
    top = None
    parts = module.split('.')
    for index, part in enumerate(parts):
        # The search path already identifies the parent. Use a leaf name so a
        # namespace spec never consults sys.modules for an unimported parent.
        spec = PathFinder.find_spec(part, search)
        if spec is None:
            _fail()
        if top is None:
            top = spec
        if index < len(parts) - 1:
            locations = spec.submodule_search_locations
            if locations is None:
                _fail()
            search = list(locations)
    return top, spec


def _distribution_metadata(top_name):
    names = metadata.packages_distributions().get(top_name, ())
    if not names or len(names) > MAX_ROOTS:
        _fail()
    pending = list(names)
    result = {}
    while pending:
        name = pending.pop()
        key = re.sub(r'[-_.]+', '-', name).lower()
        if key in result:
            continue
        if len(result) >= MAX_DISTRIBUTIONS:
            _fail()
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            # Optional/marker-qualified requirements can legitimately be absent;
            # absence is part of identity, and installing them changes it.
            if name in names:
                _fail()
            result[key] = {'version': None, 'requires': []}
            continue
        version, requires = distribution.version, list(distribution.requires or ())
        if (type(version) is not str or len(version) > 256 or len(requires) > 256
                or any(type(row) is not str or len(row) > 2048 for row in requires)):
            _fail()
        result[key] = {'version': version, 'requires': sorted(requires)}
        for requirement in requires:
            match = re.match(r'\s*([A-Za-z0-9][A-Za-z0-9_.-]*)', requirement)
            if match is None:
                _fail()
            pending.append(match.group(1))
    return result


def adapter_digest(definition):
    """Return an SHA-256 identity for a manifest and its installed package.

    This does not import the adapter, load a native library or run executable
    code. It is startup/worker work, never an operation-status/RPC-thread hook.
    External application executables require their own qualified image identity.
    """
    try:
        module = definition.factory.split(':', 1)[0]
        top, factory = _specs(module)
        if not factory.origin or factory.origin in {'built-in', 'frozen'}:
            _fail()
        roots = list(top.submodule_search_locations or ())
        if len(roots) > MAX_ROOTS:
            _fail()
        digest = sha256()
        def piece(value):
            value = value if isinstance(value, bytes) else value.encode()
            digest.update(len(value).to_bytes(8, 'big'))
            digest.update(value)
        piece(json.dumps({**definition.describe(), 'factory': definition.factory}, sort_keys=True, separators=(',', ':')))
        piece(json.dumps(_distribution_metadata(module.split('.')[0]), sort_keys=True, separators=(',', ':')))
        count, total = 0, 0
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)

        def identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

        def file(fd, name, label):
            nonlocal total
            descriptor = os.open(name, flags, dir_fd=fd)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                    _fail()
                piece(label)
                piece(str(before.st_size))
                read = 0
                while True:
                    block = os.read(descriptor, 1024 * 1024)
                    if not block:
                        break
                    read += len(block)
                    total += len(block)
                    if read > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                        _fail()
                    digest.update(block)
                if (read != before.st_size or identity(before) != identity(os.fstat(descriptor))
                        or identity(before) != identity(os.stat(name, dir_fd=fd, follow_symlinks=False))):
                    _fail()
            finally:
                os.close(descriptor)

        def directory(fd, prefix, depth=0):
            nonlocal count
            if depth > MAX_DEPTH:
                _fail()
            before = os.fstat(fd)
            names = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    if entry.name in _IGNORED or entry.name.endswith(('.pyc', '.pyo')):
                        continue
                    count += 1
                    if count > MAX_FILES:
                        _fail()
                    names.append(entry.name)
            for name in sorted(names):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                label = f'{prefix}/{name}'
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, flags | os.O_DIRECTORY, dir_fd=fd)
                    try:
                        if identity(info) != identity(os.fstat(child)):
                            _fail()
                        directory(child, label, depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    file(fd, name, label)
                else:
                    _fail()
            if identity(before) != identity(os.fstat(fd)):
                _fail()

        if roots:
            for index, root in enumerate(sorted(roots)):
                original = Path(root)
                if original.is_symlink():
                    _fail()
                path = original.resolve(strict=True)
                descriptor = os.open(path, flags | os.O_DIRECTORY)
                try:
                    directory(descriptor, f'package:{index}')
                    if identity(os.fstat(descriptor)) != identity(path.stat()):
                        _fail()
                finally:
                    os.close(descriptor)
        else:
            origin = Path(top.origin)
            descriptor = os.open(origin.parent.resolve(strict=True), flags | os.O_DIRECTORY)
            try:
                file(descriptor, origin.name, 'module')
            finally:
                os.close(descriptor)
        return digest.hexdigest()
    except IntegrationError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, ImportError, RecursionError):
        raise IntegrationError('adapter_fingerprint_unavailable', 'installed adapter identity could not be verified') from None
