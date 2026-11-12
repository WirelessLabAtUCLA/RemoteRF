"""The whole session runs against a throwaway HOME.

Host state (devices.yml, certs, drivers) lives under ``~/.config/remoterf``.
Importing the device manager reloads devices and seeds driver files, which
happens during collection, so a per-test fixture is too late: the environment
is redirected here, before any test module imports the host. Subprocess tests
inherit it through ``os.environ``. Tests that set these variables themselves
still win, because monkeypatch restores to these values.
"""

import atexit
import os
import shutil
import tempfile

_home = tempfile.mkdtemp(prefix="remoterf-host-tests-")
atexit.register(shutil.rmtree, _home, ignore_errors=True)
os.environ["HOME"] = _home
os.environ["USERPROFILE"] = _home
os.environ["XDG_CONFIG_HOME"] = os.path.join(_home, ".config")
# Unset rather than redirected so every resolver falls back to the XDG root
# above, and tests that set only one of them keep their current behaviour.
os.environ.pop("REMOTERF_CONFIG_DIR", None)
os.environ.pop("REMOTERF_DRIVERS_DIR", None)
