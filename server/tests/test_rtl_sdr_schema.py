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

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from remoteRF_server import serverrf_cli
from remoteRF_server.drivers.rtl_sdr.rtl_sdr_schema import (
    RtlSdrSchema,
    _MAX_COMPLEX64_SAMPLES,
    _MAX_REMOTE_PAYLOAD_BYTES,
    _RtlSdrAdapter,
    _connect_rtl_sdr,
)


class FakeRtlSdr:
    def __init__(self):
        self.center_freq = 100_000_000.0
        self.sample_rate = 2_048_000.0
        self.gain = 12.5
        self.valid_gains_db = [0.0, 12.5, 49.6]
        self.freq_correction = 1
        self.bandwidth = 0.0
        self.agc_mode = False
        self.bias_tee = False
        self.direct_sampling = 0
        self.offset_tuning = False
        self.dithering = True
        self.closed = False
        self.reset_count = 0

    def get_tuner_type(self):
        return "R820T"

    def set_agc_mode(self, enabled):
        self.agc_mode = enabled

    def set_bias_tee(self, enabled):
        self.bias_tee = enabled

    def get_direct_sampling(self):
        return self.direct_sampling

    def set_direct_sampling(self, value):
        self.direct_sampling = value

    def get_offset_tuning(self):
        return self.offset_tuning

    def set_offset_tuning(self, enabled):
        self.offset_tuning = enabled

    def set_dithering(self, enabled):
        self.dithering = enabled

    def reset_buffer(self):
        self.reset_count += 1

    def read_samples(self, count):
        return np.arange(count, dtype=np.float32).astype(np.complex64) + 1j

    def read_bytes(self, count):
        return bytes(index % 256 for index in range(count))

    def close(self):
        self.closed = True


class PyRtlSdrLikeFake(FakeRtlSdr):
    """pyrtlsdr 0.5.0: a failed read closes the device but keeps the freed
    handle, and librtlsdr fails a read that ends mid USB packet."""

    def __init__(self):
        super().__init__()
        self.device_opened = True
        self.fail_next = False
        self.reads = []

    def read_bytes(self, count):
        assert self.device_opened, "read through a freed librtlsdr handle (crashes the lab)"
        self.reads.append(count)
        if count % 512 or self.fail_next:
            self.close()
            raise OSError(f"Could not read {count} bytes")
        return super().read_bytes(count)

    def read_samples(self, count):
        self.read_bytes(2 * count)
        return super().read_samples(count)

    def close(self):
        super().close()
        self.device_opened = False


def _bound_schema(native=None):
    native = native or FakeRtlSdr()
    adapter = _RtlSdrAdapter(
        native,
        device_index=2,
        serial_number="00000002",
        usb_strings=("Realtek", "RTL2838UHIDIR", "00000002"),
        test_mode_enabled=False,
        dithering_enabled=True,
    )
    schema = RtlSdrSchema()
    schema.bind(adapter)
    return schema, adapter, native


class RtlSdrSchemaTests(unittest.TestCase):
    def test_idl_publishes_rtlsdr_compatible_controls_and_receive_calls(self):
        idl = RtlSdrSchema().get_idl()

        self.assertEqual(idl["schema_version"], "1.0")
        self.assertEqual(idl["device_type"], "rtl_sdr")
        self.assertEqual(idl["client_class"], "RtlSdr")
        for name in (
            "get_center_freq",
            "get_sample_rate",
            "get_gain",
            "get_valid_gains_db",
            "get_freq_correction",
            "get_bandwidth",
            "get_tuner_type",
            "get_agc_mode",
            "get_bias_tee",
            "get_direct_sampling",
            "get_offset_tuning",
            "get_dithering",
        ):
            self.assertIn(name, idl["getters"])
        for name in ("call_read_samples", "call_read_bytes", "call_reset_buffer"):
            self.assertIn(name, idl["calls"])
        self.assertNotIn("call_get_capabilities", idl["calls"])
        self.assertEqual(
            idl["calls"]["call_read_samples"]["args"][0]["default"],
            1024,
        )

    def test_dispatch_controls_and_returns_wire_safe_iq(self):
        schema, _adapter, native = _bound_schema()

        schema.dispatch("set_center_freq", {"value": 101_100_000})
        schema.dispatch("set_sample_rate", {"value": 1_024_000})
        schema.dispatch("set_gain", {"value": "auto"})
        schema.dispatch("set_freq_correction", {"value": -3})
        schema.dispatch("set_bandwidth", {"value": 200_000})
        schema.dispatch("set_agc_mode", {"value": True})
        schema.dispatch("set_bias_tee", {"value": False})
        schema.dispatch("set_direct_sampling", {"value": 2})
        schema.dispatch("set_offset_tuning", {"value": True})
        schema.dispatch("set_dithering", {"value": False})

        self.assertEqual(schema.dispatch("get_center_freq", {}), 101_100_000.0)
        self.assertEqual(schema.dispatch("get_sample_rate", {}), 1_024_000.0)
        self.assertEqual(native.gain, "auto")
        self.assertEqual(schema.dispatch("get_freq_correction", {}), -3)
        self.assertEqual(schema.dispatch("get_bandwidth", {}), 200_000.0)
        self.assertTrue(schema.dispatch("get_agc_mode", {}))
        self.assertFalse(schema.dispatch("get_bias_tee", {}))
        self.assertEqual(schema.dispatch("get_direct_sampling", {}), 2)
        self.assertTrue(schema.dispatch("get_offset_tuning", {}))
        self.assertFalse(schema.dispatch("get_dithering", {}))

        samples = schema.dispatch("call_read_samples", {"num_samples": 16})
        self.assertEqual(samples.dtype, np.dtype(np.complex64))
        self.assertEqual(samples.shape, (16,))
        self.assertTrue(samples.flags.c_contiguous)
        self.assertEqual(len(schema.dispatch("call_read_bytes", {"num_bytes": 17})), 17)
        schema.dispatch("call_reset_buffer", {})
        self.assertEqual(native.reset_count, 1)

    def _assert_defaults(self, schema, native):
        self.assertEqual(native.center_freq, 915e6)
        self.assertEqual(native.sample_rate, 2.048e6)
        self.assertEqual(native.gain, "auto")
        self.assertEqual(native.bandwidth, 0)
        self.assertEqual(native.freq_correction, 0)
        self.assertEqual(schema.get_center_freq(), 915e6)
        self.assertEqual(schema.get_sample_rate(), 2.048e6)
        self.assertEqual(schema.get_gain(), "auto")
        self.assertEqual(schema.get_bandwidth(), 0)
        self.assertEqual(schema.get_freq_correction(), 0)
        self.assertIs(schema.get_bias_tee(), False)
        self.assertIs(schema.get_offset_tuning(), False)
        self.assertIs(schema.get_agc_mode(), False)
        self.assertEqual(schema.get_direct_sampling(), 0)
        self.assertIs(schema.get_dithering(), True)

    def test_reset_for_next_holder_restores_fixed_defaults(self):
        schema, _adapter, native = _bound_schema()
        schema.set_center_freq(433_920_000)
        schema.set_sample_rate(1_024_000)
        schema.set_gain(40.2)
        schema.set_bandwidth(300_000)
        schema.set_freq_correction(7)
        schema.set_bias_tee(True)
        schema.set_direct_sampling(2)
        schema.set_offset_tuning(True)
        schema.set_agc_mode(True)
        schema.set_dithering(False)

        schema.reset_for_next_holder()

        self._assert_defaults(schema, native)
        self.assertEqual(native.reset_count, 1)

    def test_reset_for_next_holder_skips_missing_native_controls(self):
        class NoBiasTeeFake(FakeRtlSdr):
            set_bias_tee = None
            set_dithering = None

        schema, adapter, native = _bound_schema(NoBiasTeeFake())
        native.center_freq = 433_920_000.0

        schema.reset_for_next_holder()

        self.assertEqual(native.center_freq, 915e6)
        self.assertIsNone(adapter.control_state["bias_tee"])

    def test_reset_for_next_holder_keeps_going_after_a_failure_and_raises_it(self):
        class FailingRateFake(FakeRtlSdr):
            def __setattr__(self, name, value):
                if name == "sample_rate" and getattr(self, "armed", False):
                    raise OSError("Could not set sample rate")
                super().__setattr__(name, value)

            def set_bias_tee(self, enabled):
                raise OSError("Could not set bias tee")

        native = FailingRateFake()
        schema, adapter, _native = _bound_schema(native)
        native.center_freq = 433_920_000.0
        native.gain = 40.2
        native.armed = True

        with self.assertRaisesRegex(OSError, "bias tee"):
            schema.reset_for_next_holder()
        self.assertEqual(native.center_freq, 915e6)
        self.assertEqual(native.gain, "auto")
        self.assertEqual(native.reset_count, 1)
        self.assertIsNone(adapter.control_state["bias_tee"])

    def test_reset_for_next_holder_on_a_closed_device_is_a_no_op(self):
        native = PyRtlSdrLikeFake()
        schema, _adapter, _native = _bound_schema(native)
        native.center_freq = 433_920_000.0
        native.close()

        schema.reset_for_next_holder()

        self.assertEqual(native.center_freq, 433_920_000.0)
        self.assertEqual(native.reset_count, 0)
        unbound = RtlSdrSchema()
        unbound.reset_for_next_holder()

    def test_r820t_offset_tuning_skipped_at_open_and_reset_and_closed_open_raises(self):
        class R820TFake(PyRtlSdrLikeFake):
            def __init__(self, **kwargs):
                super().__init__()
                self.offset_calls = []

            def set_offset_tuning(self, enabled):
                self.offset_calls.append(enabled)
                self.close()
                raise OSError("R820T does not support offset tuning")

        with patch(
            "remoteRF_server.drivers.rtl_sdr.rtl_sdr_schema._require_rtlsdr",
            return_value=R820TFake,
        ):
            adapter = _connect_rtl_sdr(device_index=0)
        schema = RtlSdrSchema()
        schema.bind(adapter)
        self.assertTrue(adapter.native.device_opened)
        self.assertEqual(adapter.native.offset_calls, [])
        self._assert_defaults(schema, adapter.native)

        adapter.native.center_freq = 433_920_000.0
        schema.reset_for_next_holder()
        self.assertTrue(adapter.native.device_opened)
        self.assertEqual(adapter.native.offset_calls, [])
        self._assert_defaults(schema, adapter.native)

        class NoGetterR820TFake(R820TFake):
            get_offset_tuning = None
            get_direct_sampling = None

        with patch(
            "remoteRF_server.drivers.rtl_sdr.rtl_sdr_schema._require_rtlsdr",
            return_value=NoGetterR820TFake,
        ):
            no_getter_adapter = _connect_rtl_sdr(device_index=0)
        no_getter_schema = RtlSdrSchema()
        no_getter_schema.bind(no_getter_adapter)
        self.assertTrue(no_getter_adapter.native.device_opened)
        self.assertEqual(no_getter_adapter.native.offset_calls, [])
        self.assertIs(no_getter_schema.get_offset_tuning(), False)
        self.assertEqual(no_getter_schema.get_direct_sampling(), 0)

        class ClosingAtOpenFake(PyRtlSdrLikeFake):
            def __init__(self, **kwargs):
                super().__init__()

            def set_bias_tee(self, enabled):
                self.close()
                raise OSError("bias tee closed device")

        with patch(
            "remoteRF_server.drivers.rtl_sdr.rtl_sdr_schema._require_rtlsdr",
            return_value=ClosingAtOpenFake,
        ):
            with self.assertRaisesRegex(OSError, "bias tee closed device"):
                _connect_rtl_sdr(device_index=0)

    def test_reset_mid_failure_device_opened_guard_and_freq_correction_zero_skip(self):
        class CloseOnErrorFake(PyRtlSdrLikeFake):
            def __init__(self):
                self._guard = False
                self._freq_correction = 0
                self.freq_correction_writes = 0
                super().__init__()
                self._freq_correction = 0
                self.freq_correction_writes = 0

            @property
            def freq_correction(self):
                if self._guard:
                    assert self.device_opened, "freq_correction read through freed handle"
                return self._freq_correction

            @freq_correction.setter
            def freq_correction(self, value):
                if not self._guard:
                    self._freq_correction = int(value)
                    return
                assert self.device_opened, "freq_correction write through freed handle"
                self.freq_correction_writes += 1
                if int(value) == self._freq_correction:
                    self.close()
                    raise OSError("librtlsdr rejects unchanged freq_correction")
                self._freq_correction = int(value)

            def __setattr__(self, name, value):
                if name in ("sample_rate", "center_freq", "bandwidth", "gain") and getattr(self, "_guard", False):
                    assert self.device_opened, f"{name} write through freed handle"
                super().__setattr__(name, value)

            def set_agc_mode(self, enabled):
                if self._guard:
                    assert self.device_opened, "set_agc_mode through freed handle"
                super().set_agc_mode(enabled)

            def set_dithering(self, enabled):
                if self._guard:
                    assert self.device_opened, "set_dithering through freed handle"
                super().set_dithering(enabled)

            def reset_buffer(self):
                if self._guard:
                    assert self.device_opened, "reset_buffer through freed handle"
                super().reset_buffer()

        native = CloseOnErrorFake()
        native._guard = True
        schema, _adapter, _native = _bound_schema(native)
        schema.reset_for_next_holder()
        self.assertTrue(native.device_opened)
        self.assertEqual(native.freq_correction_writes, 0)
        self._assert_defaults(schema, native)

        class CloseOnBiasTeeFake(CloseOnErrorFake):
            def set_bias_tee(self, enabled):
                self.close()
                raise OSError("Could not set bias tee")

        closing = CloseOnBiasTeeFake()
        closing.center_freq = 433_920_000.0
        closing._guard = True
        closing_schema, _adapter2, _native2 = _bound_schema(closing)
        with self.assertRaisesRegex(OSError, "bias tee"):
            closing_schema.reset_for_next_holder()
        self.assertFalse(closing.device_opened)
        self.assertEqual(closing.center_freq, 433_920_000.0)
        self.assertEqual(closing.reset_count, 0)

    def test_open_applies_the_same_defaults(self):
        class OpenFake(FakeRtlSdr):
            def __init__(self, **kwargs):
                super().__init__()
                self.center_freq = 80e6
                self.bias_tee = True

        with patch(
            "remoteRF_server.drivers.rtl_sdr.rtl_sdr_schema._require_rtlsdr",
            return_value=OpenFake,
        ):
            adapter = _connect_rtl_sdr(device_index=0)
        schema = RtlSdrSchema()
        schema.bind(adapter)

        self._assert_defaults(schema, adapter.native)
        self.assertFalse(adapter.native.bias_tee)

    def test_reset_buffer_without_native_support_discards_one_usb_packet(self):
        class NoResetFake(PyRtlSdrLikeFake):
            reset_buffer = None

        native = NoResetFake()
        schema, _adapter, _native = _bound_schema(native)

        schema.dispatch("call_reset_buffer", {})
        self.assertEqual(native.reads, [512])
        self.assertEqual(native.reset_count, 0)

    def test_unaligned_reads_and_a_self_closed_device_never_reach_the_freed_handle(self):
        # 2026-10-13 E2E B1: read_samples(98000) closed the device, the next read crashed rrf2.
        native = PyRtlSdrLikeFake()
        schema, _adapter, _native = _bound_schema(native)

        self.assertEqual(schema.dispatch("call_read_samples", {"num_samples": 98000}).shape, (98000,))
        self.assertEqual(len(schema.dispatch("call_read_bytes", {"num_bytes": 17})), 17)
        self.assertEqual(native.reads, [196096, 512])

        native.fail_next = True
        with self.assertRaises(OSError):
            schema.dispatch("call_read_samples", {"num_samples": 98304})
        for call, args in (("call_read_samples", {"num_samples": 98304}), ("call_read_bytes", {"num_bytes": 512})):
            with self.subTest(call=call), self.assertRaisesRegex(RuntimeError, "closed"):
                schema.dispatch(call, args)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            schema.dispatch("get_center_freq", {})

    def test_capture_limits_prevent_unbounded_rpc_payloads(self):
        schema, _adapter, _native = _bound_schema()

        for value in (0, -1, _MAX_COMPLEX64_SAMPLES + 1):
            with self.subTest(samples=value), self.assertRaises(ValueError):
                schema.dispatch("call_read_samples", {"num_samples": value})
        with self.assertRaises(ValueError):
            schema.dispatch(
                "call_read_bytes",
                {"num_bytes": _MAX_REMOTE_PAYLOAD_BYTES + 1},
            )

    def test_connector_resolves_serial_before_opening_device(self):
        class ConstructorFake(FakeRtlSdr):
            opened_with = None

            @staticmethod
            def get_device_index_by_serial(serial):
                return 3 if serial == "SERIAL3" else -1

            @staticmethod
            def get_device_usb_strings(index):
                return ("Realtek", "RTL2838", f"SERIAL{index}")

            def __init__(self, **kwargs):
                super().__init__()
                type(self).opened_with = kwargs

        with patch(
            "remoteRF_server.drivers.rtl_sdr.rtl_sdr_schema._require_rtlsdr",
            return_value=ConstructorFake,
        ):
            adapter = _connect_rtl_sdr(serial="SERIAL3", dithering_enabled=False)

        self.assertEqual(adapter.device_index, 3)
        self.assertEqual(adapter.serial_number, "SERIAL3")
        self.assertEqual(
            ConstructorFake.opened_with,
            {
                "device_index": 3,
                "test_mode_enabled": False,
                "dithering_enabled": False,
            },
        )
        adapter.close()
        self.assertTrue(adapter.native is None)

    def test_connector_omits_dithering_for_older_pyrtlsdr_constructor(self):
        class OlderConstructorFake(FakeRtlSdr):
            opened_with = None

            @staticmethod
            def get_device_index_by_serial(serial):
                return 0

            def __init__(
                self,
                device_index=0,
                test_mode_enabled=False,
                serial_number=None,
            ):
                super().__init__()
                type(self).opened_with = {
                    "device_index": device_index,
                    "test_mode_enabled": test_mode_enabled,
                    "serial_number": serial_number,
                }

        with patch(
            "remoteRF_server.drivers.rtl_sdr.rtl_sdr_schema._require_rtlsdr",
            return_value=OlderConstructorFake,
        ):
            adapter = _connect_rtl_sdr(device_index=0, dithering_enabled=False)

        self.assertEqual(
            OlderConstructorFake.opened_with,
            {
                "device_index": 0,
                "test_mode_enabled": False,
                "serial_number": None,
            },
        )

    def test_cli_writes_serial_and_index_inventory(self):
        cases = (
            (
                "4:rtl_fm:serial=00000001",
                "serial",
                "00000001",
                {"serial": "00000001"},
            ),
            (
                "4:rtl_fm:index=0",
                "device_index",
                "0",
                {"device_index": 0},
            ),
        )
        for spec, kind, ident, init in cases:
            with self.subTest(spec=spec), tempfile.TemporaryDirectory() as tmp:
                with patch.dict(
                    os.environ,
                    {"REMOTERF_CONFIG_DIR": tmp},
                    clear=False,
                ):
                    self.assertEqual(serverrf_cli._device_add_rtl_sdr(spec), 0)
                    record = serverrf_cli._read_device_records()[4]
                    self.assertEqual(record["TYPE"], "rtl_sdr")
                    self.assertEqual(record["IDENT_KIND"], kind)
                    self.assertEqual(record["IDENT"], ident)
                    self.assertEqual(record["INIT"], init)
                    text = Path(tmp, "devices.yml").read_text(encoding="utf-8")
                    self.assertIn("device_type: rtl_sdr", text)

    def test_cli_flag_routes_to_rtl_sdr_inventory_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(
                    os.environ,
                    {"REMOTERF_CONFIG_DIR": tmp},
                    clear=False,
                ),
                patch.object(
                    sys,
                    "argv",
                    [
                        "serverrf",
                        "--device",
                        "--add",
                        "--rtl-sdr",
                        "4:rtl_fm:index=0",
                    ],
                ),
            ):
                self.assertEqual(serverrf_cli.main(), 0)
                self.assertEqual(
                    serverrf_cli._read_device_records()[4]["TYPE"],
                    "rtl_sdr",
                )


if __name__ == "__main__":
    unittest.main()
