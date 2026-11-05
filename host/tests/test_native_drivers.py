# Copyright (C) 2026 RemoteRF
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
TYPES = ("bluetooth", "gps", "krakensdr", "rfbeam_kmd7", "ti_awr6843", "wifi")
LAB_GPS = textwrap.dedent(
    """
    from remoteRF_server.common.idl import DeviceSchema, idl_register

    @idl_register("gps")
    class LabGps(DeviceSchema):
        device_type = "gps"
        client_class = "GpsModule"
        driver_version = "lab-copy"
    """
)


class HostNativeDriverTests(unittest.TestCase):
    def test_native_types_load_lab_copy_wins_and_a_missing_library_is_reported(self):
        with tempfile.TemporaryDirectory() as raw_temp:
            temp = Path(raw_temp)
            drivers = temp / "drivers"
            drivers.mkdir()
            (drivers / "gps_schema.py").write_text(LAB_GPS, encoding="utf-8")
            env = dict(os.environ)
            env["REMOTERF_CONFIG_DIR"] = str(temp / "host")
            env["REMOTERF_DRIVERS_DIR"] = str(drivers)
            env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(SOURCE_ROOT), env.get("PYTHONPATH"))))
            script = textwrap.dedent(
                f"""
                import sys
                sys.modules["dbus_fast"] = None  # bluetooth's library is not installed
                from remoterf_device_core.manifest import stub_missing_libraries
                stub_missing_libraries()

                from pathlib import Path
                from remoteRF_host.common.idl.schema import get_driver_class, last_load_results
                from remoteRF_host.server import device_manager

                device_manager._load_all_drivers(set({TYPES!r}))

                gps = get_driver_class("gps")
                assert gps.driver_version == "lab-copy", gps
                assert Path({str(drivers / "gps_schema.py")!r}).read_text(encoding="utf-8") == {LAB_GPS!r}
                for device_type, client_class in (("krakensdr", "KrakenSDR"), ("rfbeam_kmd7", "RFBeamKMD7"),
                                                  ("ti_awr6843", "TiMmWave"), ("wifi", "WifiDongle")):
                    cls = get_driver_class(device_type)
                    assert cls.client_class == client_class, cls
                    assert cls.__module__.startswith("remoterf_device_core.drivers."), cls
                try:
                    get_driver_class("bluetooth")
                except KeyError:
                    pass
                else:
                    raise AssertionError("bluetooth registered without its library")
                rows = {{row["file"]: row for row in last_load_results()}}
                bad = rows["remoteRF_host.drivers.bluetooth.bluetooth_schema"]
                assert not bad["ok"] and "dbus_fast" in bad["error"], bad
                assert rows["gps_schema.py"]["ok"], rows
                # Nothing copied into the drivers dir for these types.
                assert not any((Path({str(drivers)!r}) / f"{{t}}_schema.py").exists() for t in {TYPES!r} if t != "gps")
                print("HOST_NATIVE_OK")
                """
            )
            result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True,
                                    timeout=60, check=False)
        self.assertEqual(result.returncode, 0, msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertIn("HOST_NATIVE_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
