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

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from remoterf_device_core import manifest
from remoterf_device_core.idl import schema

# type -> (client_class, client_modules), as the owner's schema files have them
NEW_BUILTINS = {
    "bluetooth": ("BluetoothModule", None),
    "gps": ("GpsModule", None),
    "krakensdr": ("KrakenSDR", {"krakensdr": "remoteRF.drivers.support.krakensdr"}),
    "rfbeam_kmd7": ("RFBeamKMD7", {"rfbeam_kmd7": "remoteRF.drivers.support.rfbeam_kmd7"}),
    "ti_awr6843": ("TiMmWave", {"ti_mmwave": "remoteRF.drivers.support.ti_mmwave"}),
    "wifi": ("WifiDongle", {"wifi": "remoteRF.drivers.support.wifi"}),
}

PROBE = (
    "from remoterf_device_core.idl.schema import DeviceSchema, idl_register\n\n"
    "@idl_register('probe_{kind}')\n"
    "class Probe(DeviceSchema):\n"
    "    device_type = 'probe_{kind}'\n"
    "    driver_version = '{version}'\n"
)


class NewBuiltinTests(unittest.TestCase):
    def test_each_new_builtin_loads_with_its_type_class_and_manifest_hash(self):
        # A fresh process: missing hardware libraries are stubbed, as the manifest build does.
        script = (
            "import importlib, json\n"
            "from remoterf_device_core import manifest\n"
            "manifest.stub_missing_libraries()\n"
            "from remoterf_device_core.idl import get_driver_class\n"
            "out = {}\n"
            f"for t in {sorted(NEW_BUILTINS)!r}:\n"
            "    importlib.import_module(f'remoterf_device_core.drivers.{t}.{t}_schema')\n"
            "    cls = get_driver_class(t)\n"
            "    out[t] = [cls.device_type, cls.client_class, getattr(cls, 'client_modules', None), cls().get_idl_hash()]\n"
            "print(json.dumps(out))\n"
        )
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
        got = json.loads(done.stdout)
        hashes = {row["device_type"]: row["v1"] for row in manifest.load()}
        for device_type, (client_class, client_modules) in NEW_BUILTINS.items():
            with self.subTest(device_type):
                dtype, cls_name, modules, idl_hash = got[device_type]
                self.assertEqual((dtype, cls_name), (device_type, client_class))
                if client_modules is not None:
                    self.assertEqual(modules, client_modules)
                self.assertEqual(idl_hash, hashes[device_type])
                self.assertIn(idl_hash, manifest.builtin_hashes())

    def test_ti_awr6843_configs_ship_in_the_package(self):
        from remoterf_device_core import drivers

        configs = Path(drivers.__file__).parent / "ti_awr6843" / "configs"
        self.assertEqual(sorted(p.name for p in configs.glob("*.cfg")),
                         ["awr6843_2d_default.cfg", "awr6843_3d_default.cfg", "awr6843_heatmap_default.cfg"])


class LoadBuiltinDriversTests(unittest.TestCase):
    def setUp(self):
        registry = dict(schema._DRIVER_REGISTRY)
        builtin_rows = list(schema._BUILTIN_RESULTS)
        temp = tempfile.TemporaryDirectory()
        self.root = Path(temp.name)
        sys.path.insert(0, str(self.root / "pkgs"))

        def restore():
            schema._DRIVER_REGISTRY.clear()
            schema._DRIVER_REGISTRY.update(registry)
            schema._BUILTIN_RESULTS[:] = builtin_rows
            sys.path.remove(str(self.root / "pkgs"))
            for name in ("rrf_probe_good", "rrf_probe_bad", "probe_good_schema"):
                sys.modules.pop(name, None)
            temp.cleanup()

        self.addCleanup(restore)
        (self.root / "pkgs").mkdir()
        (self.root / "pkgs" / "rrf_probe_good.py").write_text(PROBE.format(kind="good", version="builtin"))
        # Registers, then its library turns out to be missing.
        (self.root / "pkgs" / "rrf_probe_bad.py").write_text(
            PROBE.format(kind="bad", version="builtin") + "import rrf_no_such_library\n")

    def test_missing_library_fails_only_that_driver_and_is_reported(self):
        results = schema.load_builtin_drivers(["rrf_probe_bad", "rrf_probe_good"])
        self.assertEqual(results["rrf_probe_good"], "ok")
        self.assertIn("rrf_no_such_library", results["rrf_probe_bad"])
        self.assertEqual(schema.get_driver_class("probe_good").driver_version, "builtin")
        with self.assertRaises(KeyError):
            schema.get_driver_class("probe_bad")
        rows = {row["file"]: row for row in schema.last_load_results()}
        self.assertTrue(rows["rrf_probe_good"]["ok"])
        self.assertFalse(rows["rrf_probe_bad"]["ok"])
        self.assertIn("rrf_no_such_library", rows["rrf_probe_bad"]["error"])

    def test_drivers_dir_file_of_the_same_type_wins_over_the_builtin(self):
        drivers = self.root / "drivers"
        drivers.mkdir()
        (drivers / "probe_good_schema.py").write_text(PROBE.format(kind="good", version="lab copy"))
        schema.load_builtin_drivers(["rrf_probe_good"])
        self.assertEqual(schema.load_drivers(drivers), {"probe_good_schema.py": "ok"})
        self.assertEqual(schema.get_driver_class("probe_good").driver_version, "lab copy")
        # Both loads stay reported.
        self.assertEqual([row["file"] for row in schema.last_load_results()],
                         ["rrf_probe_good", "probe_good_schema.py"])


if __name__ == "__main__":
    unittest.main()
