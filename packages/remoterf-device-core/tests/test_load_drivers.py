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

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

from remoterf_device_core.idl import schema


def driver(device_type: str, version: str, tail: str = "") -> str:
    return (
        "from remoterf_device_core.idl.schema import DeviceSchema, idl_register\n\n"
        f"@idl_register({device_type!r})\n"
        "class Probe(DeviceSchema):\n"
        f"    device_type = {device_type!r}\n"
        f"    driver_version = {version!r}\n"
        f"{tail}"
    )


def guard(hexdigest: str | None = None) -> str:
    if hexdigest is None:
        hexdigest = hashlib.sha256(schema.builtin_driver_file("rtl_sdr").read_bytes()).hexdigest()
    return f"# rrfops-override base-sha256={hexdigest}\n"


STEMS = ("rtl_sdr_schema", "zz_rrfops_rtl_sdr", "zzz_late", "aa_exit")


class LoadDriversTests(unittest.TestCase):
    def setUp(self):
        registry = dict(schema._DRIVER_REGISTRY)
        modules = {stem: sys.modules.get(stem) for stem in STEMS}

        def restore():
            schema._DRIVER_REGISTRY.clear()
            schema._DRIVER_REGISTRY.update(registry)
            for stem, module in modules.items():
                if module is None:
                    sys.modules.pop(stem, None)
                else:
                    sys.modules[stem] = module

        self.addCleanup(restore)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        (self.dir / "rtl_sdr_schema.py").write_text(driver("rtl_sdr", "live"))

    def results(self):
        return {row["file"]: row for row in schema.last_load_results()}

    def test_override_loads_last_and_wins(self):
        (self.dir / "zzz_late.py").write_text(driver("rtl_sdr", "late"))
        (self.dir / "zz_rrfops_rtl_sdr.py").write_text(guard() + driver("rtl_sdr", "override"))
        schema.load_drivers(self.dir)
        self.assertEqual(schema.get_driver_class("rtl_sdr").driver_version, "override")
        self.assertFalse(schema.override_stale(self.dir / "zz_rrfops_rtl_sdr.py"))
        self.assertEqual(schema.last_load_results(), [
            {"file": "rtl_sdr_schema.py", "ok": True, "error": None},
            {"file": "zzz_late.py", "ok": True, "error": None},
            {"file": "zz_rrfops_rtl_sdr.py", "ok": True, "error": None},
        ])

    def test_stale_or_unguarded_override_does_not_load(self):
        for header in (guard("0" * 64), "", "# rrfops-override base-sha256=abc\n"):
            with self.subTest(header=header):
                (self.dir / "zz_rrfops_rtl_sdr.py").write_text(header + driver("rtl_sdr", "override"))
                schema.load_drivers(self.dir)
                self.assertEqual(schema.get_driver_class("rtl_sdr").driver_version, "live")
                self.assertTrue(schema.override_stale(self.dir / "zz_rrfops_rtl_sdr.py"))
                row = self.results()["zz_rrfops_rtl_sdr.py"]
                self.assertFalse(row["ok"])
                self.assertIn("stale override", row["error"])
        self.assertTrue(schema.override_stale(self.dir / "zz_rrfops_no_such_type.py"))

    def test_failing_override_falls_back_with_nothing_half_registered(self):
        tail = "\n@idl_register('extra_probe')\nclass Extra(Probe):\n    pass\n\nraise RuntimeError('boom\\nsecond line')\n"
        (self.dir / "zz_rrfops_rtl_sdr.py").write_text(guard() + driver("rtl_sdr", "override", tail))
        schema.load_drivers(self.dir)
        self.assertEqual(schema.get_driver_class("rtl_sdr").driver_version, "live")
        self.assertNotIn("extra_probe", schema._DRIVER_REGISTRY)
        self.assertNotIn("zz_rrfops_rtl_sdr", sys.modules)
        self.assertEqual(self.results()["zz_rrfops_rtl_sdr.py"],
                         {"file": "zz_rrfops_rtl_sdr.py", "ok": False, "error": "boom second line"})

    def test_system_exit_in_a_driver_is_contained(self):
        (self.dir / "aa_exit.py").write_text(driver("exit_probe", "1", "\nimport sys\nsys.exit(3)\n"))
        schema.load_drivers(self.dir)
        self.assertNotIn("exit_probe", schema._DRIVER_REGISTRY)
        self.assertEqual(schema.get_driver_class("rtl_sdr").driver_version, "live")
        self.assertEqual(self.results()["aa_exit.py"]["error"], "SystemExit: 3")


if __name__ == "__main__":
    unittest.main()
