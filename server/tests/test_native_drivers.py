# Copyright (C) 2026 RemoteRF
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"

NATIVE = {
    "bluetooth": "BluetoothModule",
    "gps": "GpsModule",
    "krakensdr": "KrakenSDR",
    "rfbeam_kmd7": "RFBeamKMD7",
    "ti_awr6843": "TiMmWave",
    "wifi": "WifiDongle",
}


def lab_driver(device_type: str, client_class: str) -> str:
    return textwrap.dedent(
        f"""
        from remoteRF_server.common.idl import DeviceSchema, idl_register

        @idl_register({device_type!r})
        class LabSchema(DeviceSchema):
            device_type = {device_type!r}
            client_class = {client_class!r}
            driver_version = "lab-copy"
        """
    )


# What a lab with its own copies keeps in its drivers dir (types and client classes as they are there).
LAB_COPIES = {
    "bluetooth_schema.py": lab_driver("bluetooth", "BluetoothModule"),
    "gps_schema.py": lab_driver("gps", "GpsModule"),
    "kraken_sdr_schema.py": lab_driver("kraken_sdr", "KrakenSDR"),
    "rfbeam_kmd7_schema.py": lab_driver("rfbeam_kmd7", "KMD7"),
    "wifi_schema.py": lab_driver("wifi", "WifiDongle"),
}

# Stand-ins for whatever driver libraries this machine lacks, so every built-in imports.
PRELUDE = "from remoterf_device_core.manifest import stub_missing_libraries\nstub_missing_libraries()\n"


class NativeDriverTests(unittest.TestCase):
    def _run(self, script, *, lab_files=None, prelude=PRELUDE):
        with tempfile.TemporaryDirectory() as raw_temp:
            temp = Path(raw_temp)
            config_root = temp / "xdg"
            drivers_dir = config_root / "remoterf" / "drivers"
            drivers_dir.mkdir(parents=True)
            for name, text in (lab_files or {}).items():
                (drivers_dir / name).write_text(text, encoding="utf-8")
            env = dict(os.environ)
            env["HOME"] = str(temp / "home")
            env["XDG_CONFIG_HOME"] = str(config_root)
            env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(SOURCE_ROOT), env.get("PYTHONPATH"))))
            result = subprocess.run(
                [sys.executable, "-c", prelude + textwrap.dedent(script)],
                cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=60, check=False,
            )
        self.assertEqual(result.returncode, 0, msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return result.stdout

    def test_fresh_server_serves_every_new_type_with_no_drivers_file(self):
        out = self._run(
            f"""
            from remoteRF_server.common.idl.schema import get_driver_class
            from remoteRF_server.common.utils import get_drivers_dir
            from remoteRF_server.server import device_manager

            for device_type, client_class in {NATIVE!r}.items():
                cls = get_driver_class(device_type)
                assert cls.__module__ == f"remoterf_device_core.drivers.{{device_type}}.{{device_type}}_schema", cls
                assert cls.client_class == client_class, cls
                assert not (get_drivers_dir() / f"{{device_type}}_schema.py").exists()
            print("NATIVE_OK")
            """
        )
        self.assertIn("NATIVE_OK", out)

    def test_lab_copies_win_and_stay_untouched(self):
        out = self._run(
            f"""
            from remoteRF_server.common.idl.schema import get_driver_class
            from remoteRF_server.common.utils import get_drivers_dir
            from remoteRF_server.server import device_manager

            lab = {LAB_COPIES!r}
            drivers = get_drivers_dir()
            for name, text in lab.items():
                assert (drivers / name).read_text(encoding="utf-8") == text, name
                assert not (drivers / f".{{name}}.remoterf-managed").exists(), name
            for device_type in ("bluetooth", "gps", "kraken_sdr", "rfbeam_kmd7", "wifi"):
                served = device_manager.schema_served(device_type)
                assert served["module"].startswith(str(drivers)), served
                assert served["hash"] == get_driver_class(device_type)().get_idl_hash()
                assert get_driver_class(device_type).driver_version == "lab-copy"
            assert get_driver_class("rfbeam_kmd7").client_class == "KMD7"
            # No drivers file appears for the new types.
            for device_type in ("krakensdr", "ti_awr6843"):
                assert not (drivers / f"{{device_type}}_schema.py").exists()
            assert all(row["ok"] for row in device_manager.last_load_results()), device_manager.last_load_results()
            print("LAB_COPIES_OK")
            """,
            lab_files=LAB_COPIES,
        )
        self.assertIn("LAB_COPIES_OK", out)

    def test_missing_library_leaves_only_that_type_unserved(self):
        out = self._run(
            """
            from remoteRF_server.common.idl.schema import get_driver_class, last_load_results
            from remoteRF_server.server import device_manager

            try:
                get_driver_class("bluetooth")
            except KeyError:
                pass
            else:
                raise AssertionError("bluetooth registered without its library")
            rows = {row["file"]: row for row in last_load_results()}
            bad = rows["remoteRF_server.drivers.bluetooth.bluetooth_schema"]
            assert not bad["ok"] and "dbus_fast" in bad["error"], bad
            for device_type in ("gps", "krakensdr", "rfbeam_kmd7", "ti_awr6843", "wifi", "rtl_sdr", "hackrf"):
                get_driver_class(device_type)
            print("MISSING_LIBRARY_OK")
            """,
            # bluetooth's library is not installed
            prelude='import sys\nsys.modules["dbus_fast"] = None\n' + PRELUDE,
        )
        self.assertIn("MISSING_LIBRARY_OK", out)
        self.assertNotIn("Bluetooth", out)

    def test_missing_library_warns_only_when_a_device_needs_it(self):
        out = self._run(
            """
            from pathlib import Path
            from remoteRF_server.common.utils import get_remoterf_root

            Path(get_remoterf_root(), "devices.yml").write_text(
                "devices:\\n  - device_id: 1\\n    device_type: bluetooth\\n    name: bt\\n", encoding="utf-8")
            from remoteRF_server.server import device_manager
            """,
            prelude='import sys\nsys.modules["dbus_fast"] = None\n' + PRELUDE,
        )
        self.assertIn("could not load built-in Bluetooth driver", out)
        self.assertNotIn("GPS", out)


if __name__ == "__main__":
    unittest.main()
