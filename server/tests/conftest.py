"""The whole session runs against a throwaway HOME.

Server state (DBs, drivers, certs, super_token.txt, devices.yml) lives under
``~/.config/remoterf``. Several modules bind those paths at import time
(the reservation/user-group/perms singletons, the activity-log path, the
driver seeding in device_manager), so a per-test fixture is too late: the
environment is redirected here, before any test module imports the server.
Subprocess tests inherit it through ``os.environ``. Tests that set these
variables themselves still win, because monkeypatch restores to these values.
"""

import atexit
import os
import shutil
import tempfile

_home = tempfile.mkdtemp(prefix="remoterf-server-tests-")
atexit.register(shutil.rmtree, _home, ignore_errors=True)
os.environ["HOME"] = _home
os.environ["USERPROFILE"] = _home
os.environ["XDG_CONFIG_HOME"] = os.path.join(_home, ".config")
# Unset rather than redirected: with it unset every resolver falls back to the
# XDG root above, and subprocess tests that only set XDG_CONFIG_HOME keep working.
os.environ.pop("REMOTERF_CONFIG_DIR", None)
os.environ.pop("XDG_DATA_HOME", None)
