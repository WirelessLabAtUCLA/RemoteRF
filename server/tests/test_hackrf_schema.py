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

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from remoteRF_server.drivers.hackrf.hackrf_schema import (
    HackRFSchema,
    _MAX_COMPLEX64_SAMPLES,
    _correct_pyhackrf2_iq,
    connect_hackrf,
)


class _FakeTransfer(ctypes.Structure):
    _fields_ = [
        ("buffer", ctypes.POINTER(ctypes.c_byte)),
        ("buffer_length", ctypes.c_int),
        ("valid_length", ctypes.c_int),
    ]


_FakeTxCallback = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(_FakeTransfer))


class FakeHackRF:
    def __init__(self):
        self.center_freq = 100_000_000
        self.sample_rate = 20_000_000
        self.filter_bandwidth = 15_000_000
        self._lna_gain = 16
        self.vga_gain = 16
        self.txvga_gain = 10
        self._amplifier_on = False
        self.bias_tee_on = False
        self.sample_count_limit = 0
        self.buffer = bytearray()
        self.tx_started = False
        self.tx_stop_count = 0
        self._cfunc_tx_callback = _FakeTxCallback(lambda _transfer: 0)

    @property
    def lna_gain(self):
        raise RecursionError("mirrors the broken pyhackrf2 1.0.3 getter")

    @lna_gain.setter
    def lna_gain(self, value):
        value = min(max(int(value), 0), 40)
        self._lna_gain = value - (value % 8)

    @property
    def amplifier_on(self):
        return self._amplifier_on

    @amplifier_on.setter
    def amplifier_on(self, _value):
        # pyhackrf2 1.0.3 sends the hardware command but forgets to track it.
        pass

    def read_samples(self, count):
        return np.arange(count, dtype=np.float32).astype(np.complex64) + 1j

    def get_serial_no(self):
        return "SERIAL0"

    def start_tx(self):
        self.tx_started = True

    def stop_tx(self):
        self.tx_started = False
        self.tx_stop_count += 1


def _bound_schema():
    native = FakeHackRF()
    schema = HackRFSchema()
    schema.bind(native)
    return schema, native


class HackRFSchemaTests(unittest.TestCase):
    def test_corrects_pyhackrf2_103_iq_dc_offset_only_for_affected_version(self):
        shifted = np.array([-1.5 - 0.5j, -0.75 - 1.25j], dtype=np.complex64)
        with patch(
            "remoteRF_server.drivers.hackrf.hackrf_schema.version",
            return_value="1.0.3",
        ):
            corrected = _correct_pyhackrf2_iq(shifted)
        np.testing.assert_allclose(corrected, shifted + np.complex64(1 + 1j))

        with patch(
            "remoteRF_server.drivers.hackrf.hackrf_schema.version",
            return_value="1.0.4",
        ):
            unchanged = _correct_pyhackrf2_iq(shifted)
        np.testing.assert_array_equal(unchanged, shifted)

    def test_idl_publishes_six_new_control_pairs(self):
        idl = HackRFSchema().get_idl()

        self.assertEqual(idl["schema_version"], "1.0")
        self.assertEqual(idl["device_type"], "hackrf")
        self.assertEqual(idl["client_class"], "HackRF")
        for name in (
            "center_freq",
            "sample_rate",
            "filter_bandwidth",
            "lna_gain",
            "vga_gain",
            "txvga_gain",
            "amplifier_on",
            "bias_tee_on",
            "sample_count_limit",
        ):
            self.assertIn(f"get_{name}", idl["getters"])
            self.assertIn(f"set_{name}", idl["setters"])
        for name in (
            "call_read_samples",
            "call_load_tx_iq",
            "call_setup",
            "call_set_freq",
            "call_set_sample_rate",
            "call_start_tx",
            "call_stop_tx",
            "call_clear_buffer",
            "call_close",
            "call_enumerate",
            "call_get_serial_no",
        ):
            self.assertIn(name, idl["calls"])
        self.assertNotIn("call_get_capabilities", idl["calls"])
        self.assertEqual(
            idl["calls"]["call_read_samples"]["args"][0]["name"],
            "num_samples",
        )

    def test_dispatch_controls_and_pyhackrf_tracking_workarounds(self):
        schema, native = _bound_schema()

        self.assertIsNone(schema.dispatch("call_setup", {}))
        schema.dispatch("set_center_freq", {"value": "433200000"})
        schema.dispatch("set_sample_rate", {"value": 10_000_000})
        schema.dispatch("set_filter_bandwidth", {"value": 7_300_000})
        schema.dispatch("set_lna_gain", {"value": 23})
        schema.dispatch("set_vga_gain", {"value": 24})
        schema.dispatch("set_txvga_gain", {"value": 40})
        schema.dispatch("set_amplifier_on", {"value": "on"})
        schema.dispatch("set_bias_tee_on", {"value": True})
        schema.dispatch("set_sample_count_limit", {"value": 65_536})

        self.assertEqual(schema.dispatch("get_center_freq", {}), 433_200_000)
        self.assertEqual(schema.dispatch("get_sample_rate", {}), 10_000_000)
        self.assertEqual(schema.dispatch("get_filter_bandwidth", {}), 7_000_000)
        self.assertEqual(schema.dispatch("get_lna_gain", {}), 16)
        self.assertEqual(schema.dispatch("get_vga_gain", {}), 24)
        self.assertEqual(schema.dispatch("get_txvga_gain", {}), 40)
        self.assertTrue(schema.dispatch("get_amplifier_on", {}))
        self.assertTrue(schema.dispatch("get_bias_tee_on", {}))
        self.assertEqual(schema.dispatch("get_sample_count_limit", {}), 65_536)
        self.assertTrue(native._amplifier_on)

        self.assertIsNone(schema.dispatch("call_set_freq", {"value": 915_000_000}))
        self.assertIsNone(schema.dispatch("call_set_sample_rate", {"value": 2_000_000}))
        self.assertEqual(schema.dispatch("get_center_freq", {}), 915_000_000)
        self.assertEqual(schema.dispatch("get_sample_rate", {}), 2_000_000)

    def test_iq_calls_are_wire_safe_and_bounded(self):
        schema, native = _bound_schema()

        samples = schema.dispatch("call_read_samples", {"num_samples": 16})
        self.assertEqual(samples.dtype, np.dtype(np.complex64))
        self.assertEqual(samples.shape, (16,))
        self.assertTrue(samples.flags.c_contiguous)

        written = schema.dispatch(
            "call_load_tx_iq",
            {
                "samples": np.array(
                    [1 + 0j, 0 + 1j, -1 - 1j],
                    dtype=np.complex64,
                )
            },
        )
        self.assertEqual(written, 6)
        self.assertEqual(native.buffer, bytearray([127, 0, 0, 127, 129, 129]))

        transfer_buffer = (ctypes.c_byte * 10)()
        transfer = _FakeTransfer(
            ctypes.cast(transfer_buffer, ctypes.POINTER(ctypes.c_byte)),
            10,
            0,
        )
        self.assertEqual(native._cfunc_tx_callback(ctypes.byref(transfer)), 0)
        self.assertEqual(transfer.valid_length, 10)
        self.assertEqual(
            ctypes.string_at(transfer_buffer, 10),
            bytes([127, 0, 0, 127, 129, 129, 127, 0, 0, 127]),
        )

        self.assertEqual(schema.dispatch("call_start_tx", {}), 6)
        self.assertTrue(native.tx_started)
        with self.assertRaisesRegex(RuntimeError, "already active"):
            schema.dispatch("call_start_tx", {})
        with self.assertRaisesRegex(RuntimeError, "stop HackRF TX"):
            schema.dispatch("call_clear_buffer", {})
        self.assertIsNone(schema.dispatch("call_stop_tx", {}))
        self.assertFalse(native.tx_started)
        self.assertEqual(native.tx_stop_count, 1)
        self.assertIsNone(schema.dispatch("call_stop_tx", {}))
        self.assertEqual(native.tx_stop_count, 1)
        self.assertEqual(schema.dispatch("call_clear_buffer", {}), 0)
        self.assertEqual(native.buffer, bytearray())

        raw_iq = np.array([127, 0, -128, 127], dtype=np.int8)
        self.assertEqual(
            schema.dispatch("call_load_tx_iq", {"samples": raw_iq}),
            raw_iq.nbytes,
        )
        self.assertEqual(native.buffer, bytearray([127, 0, 128, 127]))
        schema.dispatch("call_start_tx", {})
        self.assertIsNone(schema.dispatch("call_close", {}))
        self.assertFalse(native.tx_started)
        self.assertEqual(native.buffer, bytearray())

        for value in (0, -1, _MAX_COMPLEX64_SAMPLES + 1):
            with self.subTest(samples=value), self.assertRaises(ValueError):
                schema.dispatch("call_read_samples", {"num_samples": value})
        with self.assertRaises(ValueError):
            schema.dispatch("call_load_tx_iq", {"samples": np.array([], dtype=np.complex64)})
        with self.assertRaisesRegex(ValueError, "I/Q pairs"):
            schema.dispatch(
                "call_load_tx_iq",
                {"samples": np.array([1, 2, 3], dtype=np.int8)},
            )
        with self.assertRaises(RuntimeError):
            schema.dispatch("call_start_tx", {})
        with self.assertRaises(ValueError):
            schema.dispatch("set_sample_count_limit", {"value": -1})
        with self.assertRaises(ValueError):
            schema.dispatch("set_amplifier_on", {"value": "sometimes"})

    def test_reset_for_next_holder_stops_tx_and_turns_off_bias_tee_and_amp(self):
        schema, native = _bound_schema()
        schema.dispatch("set_bias_tee_on", {"value": True})
        schema.dispatch("set_amplifier_on", {"value": True})
        schema.dispatch("call_load_tx_iq", {"samples": np.ones(4, dtype=np.complex64)})
        schema.dispatch("call_start_tx", {})

        schema.reset_for_next_holder()

        self.assertFalse(native.tx_started)
        self.assertFalse(native._remoterf_tx_active)
        self.assertEqual(len(native.buffer), 0)
        self.assertFalse(native.bias_tee_on)
        self.assertFalse(native._amplifier_on)

    def test_reset_for_next_holder_turns_off_amp_when_bias_tee_fails(self):
        schema, native = _bound_schema()
        schema.dispatch("set_amplifier_on", {"value": True})

        with patch.object(schema, "set_bias_tee_on", side_effect=OSError("usb")):
            with self.assertRaises(OSError):
                schema.reset_for_next_holder()

        self.assertFalse(native._amplifier_on)

    def test_stop_tx_failure_empties_payload_keeps_tx_active_and_runs_remaining_reset(self):
        schema, native = _bound_schema()
        schema.dispatch("set_bias_tee_on", {"value": True})
        schema.dispatch("set_amplifier_on", {"value": True})
        schema.dispatch("set_txvga_gain", {"value": 47})
        schema.dispatch("set_sample_count_limit", {"value": 65_536})
        schema.dispatch("call_load_tx_iq", {"samples": np.ones(4, dtype=np.complex64)})
        schema.dispatch("call_start_tx", {})

        with patch.object(native, "stop_tx", side_effect=OSError("usb stop_tx failed")):
            with self.assertRaisesRegex(OSError, "usb stop_tx failed"):
                schema.reset_for_next_holder()

        self.assertEqual(native._remoterf_tx_payload, b"")
        self.assertEqual(native.buffer, bytearray())
        transfer_buffer = (ctypes.c_byte * 8)()
        transfer = _FakeTransfer(
            ctypes.cast(transfer_buffer, ctypes.POINTER(ctypes.c_byte)),
            8,
            0,
        )
        self.assertEqual(native._cfunc_tx_callback(ctypes.byref(transfer)), 1)
        self.assertEqual(transfer.valid_length, 0)

        self.assertTrue(native._remoterf_tx_active)
        self.assertFalse(native.bias_tee_on)
        self.assertFalse(native._amplifier_on)
        self.assertEqual(native.txvga_gain, 0)
        self.assertEqual(native.sample_count_limit, 0)
        with self.assertRaisesRegex(RuntimeError, "stop HackRF TX before reading"):
            schema.dispatch("call_read_samples", {"num_samples": 16})

        schema.dispatch("call_stop_tx", {})
        self.assertFalse(native._remoterf_tx_active)
        self.assertEqual(native.tx_stop_count, 1)

    def test_reset_for_next_holder_restores_gains_tuning_filter_and_sample_limit_with_tx_gain_first(self):
        schema, _native = _bound_schema()
        schema.dispatch("set_txvga_gain", {"value": 47})
        schema.dispatch("set_lna_gain", {"value": 40})
        schema.dispatch("set_vga_gain", {"value": 62})
        schema.dispatch("set_center_freq", {"value": 2_450_000_000})
        schema.dispatch("set_sample_rate", {"value": 2_000_000})
        schema.dispatch("set_filter_bandwidth", {"value": 1_750_000})
        schema.dispatch("set_sample_count_limit", {"value": 65_536})

        order = []
        orig_txvga = schema.set_txvga_gain
        orig_lna = schema.set_lna_gain
        orig_freq = schema.set_center_freq
        orig_rate = schema.set_sample_rate
        orig_bw = schema.set_filter_bandwidth

        def record(name, fn):
            def wrapped(value):
                order.append(name)
                return fn(value)
            return wrapped

        with (
            patch.object(schema, "set_txvga_gain", side_effect=record("txvga", orig_txvga)),
            patch.object(schema, "set_lna_gain", side_effect=record("lna", orig_lna)),
            patch.object(schema, "set_center_freq", side_effect=record("freq", orig_freq)),
            patch.object(schema, "set_sample_rate", side_effect=record("rate", orig_rate)),
            patch.object(schema, "set_filter_bandwidth", side_effect=record("bw", orig_bw)),
        ):
            schema.reset_for_next_holder()

        self.assertEqual(order[0], "txvga")
        self.assertLess(order.index("rate"), order.index("bw"))
        self.assertEqual(schema.dispatch("get_txvga_gain", {}), 0)
        self.assertEqual(schema.dispatch("get_lna_gain", {}), 16)
        self.assertEqual(schema.dispatch("get_vga_gain", {}), 16)
        self.assertEqual(schema.dispatch("get_center_freq", {}), 100_000_000)
        self.assertEqual(schema.dispatch("get_sample_rate", {}), 20_000_000)
        self.assertEqual(schema.dispatch("get_filter_bandwidth", {}), 15_000_000)
        self.assertEqual(schema.dispatch("get_sample_count_limit", {}), 0)

    def test_read_samples_refused_while_tx_active(self):
        schema, native = _bound_schema()
        native._remoterf_tx_active = True

        with patch.object(native, "read_samples") as read_samples:
            with self.assertRaisesRegex(RuntimeError, "stop HackRF TX before reading"):
                schema.dispatch("call_read_samples", {"num_samples": 16})
        read_samples.assert_not_called()

    def test_enumeration_identity_and_serial_connector(self):
        schema, _native = _bound_schema()

        class ConstructorFake(FakeHackRF):
            opened_with = None

            @staticmethod
            def enumerate():
                return ["SERIAL0", "SERIAL1"]

            def __init__(self, device_index=0):
                super().__init__()
                type(self).opened_with = device_index

        with patch(
            "remoteRF_server.drivers.hackrf.hackrf_schema._require_hackrf",
            return_value=ConstructorFake,
        ):
            connected = connect_hackrf(serial="SERIAL1")
            serials = json.loads(schema.dispatch("call_enumerate", {}))

        self.assertIsInstance(connected, ConstructorFake)
        self.assertEqual(ConstructorFake.opened_with, 1)
        self.assertEqual(serials, ["SERIAL0", "SERIAL1"])
        self.assertEqual(schema.dispatch("call_get_serial_no", {}), "SERIAL0")

        with patch(
            "remoteRF_server.drivers.hackrf.hackrf_schema._require_hackrf",
            return_value=ConstructorFake,
        ):
            with self.assertRaisesRegex(RuntimeError, "MISSING"):
                connect_hackrf(serial="MISSING")
            with self.assertRaisesRegex(ValueError, "either serial or device_index"):
                connect_hackrf(serial="SERIAL0", device_index=0)


class DefaultHackRFSchemaTests(unittest.TestCase):
    def test_fresh_server_seeds_schema_without_native_pyhackrf(self):
        project_root = Path(__file__).resolve().parents[1]
        source_root = project_root / "src"
        with tempfile.TemporaryDirectory() as raw_temp:
            temp = Path(raw_temp)
            blocker = temp / "blocker"
            blocker.mkdir()
            (blocker / "pyhackrf2.py").write_text(
                "raise ImportError('HackRF deliberately unavailable in test')\n",
                encoding="utf-8",
            )
            config_root = temp / "xdg"
            env = dict(os.environ)
            env["HOME"] = str(temp / "home")
            env["XDG_CONFIG_HOME"] = str(config_root)
            env["PYTHONPATH"] = os.pathsep.join((str(blocker), str(source_root)))

            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    textwrap.dedent(
                        """
                        from remoteRF_server.common.idl.schema import get_driver_class
                        from remoteRF_server.common.utils import get_drivers_dir
                        from remoteRF_server.server import device_manager

                        seeded = get_drivers_dir() / "hackrf_schema.py"
                        assert seeded.is_file(), seeded
                        marker = get_drivers_dir() / ".hackrf_schema.py.remoterf-managed"
                        assert marker.is_file(), marker

                        schema = get_driver_class("hackrf")()
                        idl = schema.get_idl()
                        assert idl["client_class"] == "HackRF"
                        assert "get_lna_gain" in idl["getters"]
                        assert "set_bias_tee_on" in idl["setters"]
                        assert "call_start_tx" in idl["calls"]
                        assert "call_stop_tx" in idl["calls"]
                        assert "call_clear_buffer" in idl["calls"]

                        try:
                            schema.make_device(device_index=0)
                        except RuntimeError as exc:
                            assert "pyhackrf2" in str(exc).lower()
                        else:
                            raise AssertionError("native open should require pyhackrf2")

                        print("DEFAULT_HACKRF_SCHEMA_OK")
                        """
                    ),
                ],
                cwd=project_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("DEFAULT_HACKRF_SCHEMA_OK", result.stdout)
        self.assertNotIn("Driver load hackrf_schema.py: error", result.stdout)


if __name__ == "__main__":
    unittest.main()
