# Copyright (C) 2026 RemoteRF
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from __future__ import annotations

import hashlib
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from rssi.scanner import dbm_to_quality_percent, freq_to_channel


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"


class WifiPackagingTests(unittest.TestCase):
    def _run_fresh_server_import(self, script, *, managed_legacy_driver=None):
        with tempfile.TemporaryDirectory() as raw_temp:
            temp = Path(raw_temp)
            config_root = temp / "xdg"
            drivers_dir = config_root / "remoterf" / "drivers"
            if managed_legacy_driver is not None:
                drivers_dir.mkdir(parents=True)
                target = drivers_dir / "wifi_dongle_schema.py"
                target.write_text(managed_legacy_driver, encoding="utf-8")
                marker = drivers_dir / ".wifi_dongle_schema.py.remoterf-managed"
                marker.write_text(
                    hashlib.sha256(managed_legacy_driver.encode("utf-8")).hexdigest() + "\n",
                    encoding="utf-8",
                )

            env = dict(os.environ)
            env["HOME"] = str(temp / "home")
            env["XDG_CONFIG_HOME"] = str(config_root)
            python_path = [str(SOURCE_ROOT)]
            if env.get("PYTHONPATH"):
                python_path.append(env["PYTHONPATH"])
            env["PYTHONPATH"] = os.pathsep.join(python_path)

            return subprocess.run(
                [sys.executable, "-c", textwrap.dedent(script)],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

    def test_bundled_rssi_package(self):
        self.assertEqual(freq_to_channel(2412), 1)
        self.assertEqual(dbm_to_quality_percent(-75.0), 50)

    def test_schema_module_imports(self):
        module = importlib.import_module("remoteRF_server.drivers.wifi.wifi_schema")
        self.assertEqual(module.WifiSchema.device_type, "wifi")
        # No client support module, so any client can import remoterf.drivers.wifi.
        self.assertEqual(getattr(module.WifiSchema, "client_modules", {}), {})

    def test_fresh_server_seeds_wifi_schema(self):
        result = self._run_fresh_server_import(
            """
            from remoteRF_server.common.idl.schema import get_driver_class
            from remoteRF_server.common.utils import get_drivers_dir
            from remoteRF_server.server import device_manager

            seeded = get_drivers_dir() / "wifi_schema.py"
            assert seeded.is_file(), seeded
            assert (get_drivers_dir() / ".wifi_schema.py.remoterf-managed").is_file()
            assert get_driver_class("wifi").device_type == "wifi"
            print("DEFAULT_WIFI_SCHEMA_OK")
            """
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("DEFAULT_WIFI_SCHEMA_OK", result.stdout)

    def test_managed_legacy_wifi_schema_is_replaced_without_duplicate(self):
        managed_legacy_driver = textwrap.dedent(
            """
            from remoteRF_server.common.idl import DeviceSchema, idl_register

            @idl_register("wifi_dongle")
            class LegacyWifiSchema(DeviceSchema):
                device_type = "wifi_dongle"
                driver_version = "managed-legacy"
            """
        )
        result = self._run_fresh_server_import(
            """
            from remoteRF_server.common.idl.schema import get_driver_class
            from remoteRF_server.common.utils import get_drivers_dir
            from remoteRF_server.server import device_manager

            drivers_dir = get_drivers_dir()
            assert not (drivers_dir / "wifi_dongle_schema.py").exists()
            assert not (drivers_dir / ".wifi_dongle_schema.py.remoterf-managed").exists()
            assert (drivers_dir / "wifi_schema.py").is_file()
            assert get_driver_class("wifi").device_type == "wifi"
            try:
                get_driver_class("wifi_dongle")
            except KeyError:
                pass
            else:
                raise AssertionError("legacy managed Wi-Fi driver was still registered")
            print("MANAGED_WIFI_SCHEMA_MIGRATED")
            """,
            managed_legacy_driver=managed_legacy_driver,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("MANAGED_WIFI_SCHEMA_MIGRATED", result.stdout)

    def test_legacy_inventory_type_maps_to_wifi(self):
        result = self._run_fresh_server_import(
            """
            from remoteRF_server.server import device_manager

            record = {"TYPE": "wifi_dongle", "IDENT": "wlan1", "INIT": {}}
            _, _, device_type = device_manager._connect_from_record(record, connect=False)
            assert device_type == "wifi"
            print("LEGACY_WIFI_INVENTORY_MAPPED")
            """
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("LEGACY_WIFI_INVENTORY_MAPPED", result.stdout)


if __name__ == "__main__":
    unittest.main()
