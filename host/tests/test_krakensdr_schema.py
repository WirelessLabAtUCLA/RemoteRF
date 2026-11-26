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

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

try:
    import remoterf_device_core.drivers.krakensdr.kraken_device  # noqa: F401
except ImportError:
    # kraken_device needs scipy and pyrtlsdr; the schema only needs the name.
    _stub = types.ModuleType("remoterf_device_core.drivers.krakensdr.kraken_device")
    _stub.KrakenSDR = type("KrakenSDR", (), {"DEFAULT_SERIALS": ["1000", "1001", "1002", "1003", "1004"]})
    sys.modules[_stub.__name__] = _stub

from remoteRF_host.drivers.krakensdr.krakensdr_schema import (
    KrakenSDRDevice,
    KrakenSDRSchema,
    _MAX_SAMPLES_PER_CHANNEL,
)


class FakeKrakenSDR:
    num_channels = 5
    active_serials = ["1000", "1001", "1002", "1003", "1004"]

    def __init__(self):
        self.center_freq = 100.0e6
        self.sample_rate = 2.048e6
        self.gain = 25.4
        self.noise_source_active = False
        self.reads = []
        self.calibrations = []

    def set_noise_source(self, enable):
        self.noise_source_active = bool(enable)

    def read_samples(self, num_samples=32768, parallel=True):
        self.reads.append(num_samples)
        return np.ones((self.num_channels, num_samples), dtype=np.complex64)

    def get_powers_dbfs(self, samples):
        return np.zeros(samples.shape[0], dtype=np.float32)

    def calibrate_delays_and_phases(self, num_samples=65536):
        self.calibrations.append(num_samples)
        zeros = np.zeros(self.num_channels)
        return {"delays": zeros, "phase_offsets_deg": zeros, "correlation_peaks": zeros}


def _load_real_kraken_sdr_class():
    import remoterf_device_core.drivers.krakensdr as kraken_pkg

    kraken_path = Path(kraken_pkg.__file__).resolve().parent / "kraken_device.py"
    fake_rtlsdr = types.ModuleType("rtlsdr")
    fake_rtlsdr.RtlSdr = type("RtlSdr", (), {})
    fake_scipy = types.ModuleType("scipy")
    fake_scipy.signal = types.ModuleType("scipy.signal")
    spec = importlib.util.spec_from_file_location("_test_kraken_device_isolated", kraken_path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {"rtlsdr": fake_rtlsdr, "scipy": fake_scipy, "scipy.signal": fake_scipy.signal},
    ):
        spec.loader.exec_module(mod)
    return mod.KrakenSDR


def _bound_schema():
    device = KrakenSDRDevice(center_freq=433.92e6, sample_rate=2.4e6, gain=10.0)
    native = FakeKrakenSDR()
    device.sdr = native
    schema = KrakenSDRSchema()
    schema.bind(device)
    return schema, device, native


class KrakenSDRSchemaTests(unittest.TestCase):
    def test_reset_turns_noise_source_off_and_restores_defaults(self):
        schema, device, native = _bound_schema()
        schema.call_configure(freq=915e6, rate=1.024e6, gain=40.0)
        schema.set_noise_source(True)
        schema.call_capture_spectrum(fft_size=1024, decay=0.5)

        schema.reset_for_next_holder()

        self.assertFalse(native.noise_source_active)
        self.assertEqual(native.center_freq, 433.92e6)
        self.assertEqual(native.sample_rate, 2.4e6)
        self.assertEqual(native.gain, 10.0)
        self.assertIsNone(device._smoothed_psd)

    def test_reset_without_device_is_a_noop(self):
        KrakenSDRSchema().reset_for_next_holder()

    def test_device_opened_guards_all_five_tuners_and_skips_closed_during_reset(self):
        KrakenSDR = _load_real_kraken_sdr_class()

        class FakeTuner:
            def __init__(self, idx):
                self.idx = idx
                self.device_opened = True
                self._sample_rate = 1.024e6
                self._center_freq = 915e6
                self._gain = 40.0
                self.bias_tee = True

            def _check(self, op):
                assert self.device_opened, f"tuner {self.idx} {op} through freed handle"

            @property
            def sample_rate(self):
                return self._sample_rate

            @sample_rate.setter
            def sample_rate(self, value):
                self._check("sample_rate")
                self._sample_rate = value

            @property
            def center_freq(self):
                return self._center_freq

            @center_freq.setter
            def center_freq(self, value):
                self._check("center_freq")
                self._center_freq = value

            @property
            def gain(self):
                return self._gain

            @gain.setter
            def gain(self, value):
                self._check("gain")
                self._gain = value

            def set_bias_tee(self, enable):
                self._check("set_bias_tee")
                self.bias_tee = bool(enable)

            def read_samples(self, count):
                self._check("read_samples")
                return np.ones(count, dtype=np.complex64)

            def close(self):
                self._check("close")
                self.device_opened = False

        class DummyExecutor:
            def shutdown(self, wait=False):
                pass

        sdr = KrakenSDR.__new__(KrakenSDR)
        sdr.sdrs = [FakeTuner(i) for i in range(5)]
        sdr.num_channels = 5
        sdr.active_serials = ["1000", "1001", "1002", "1003", "1004"]
        sdr._sample_rate = 1.024e6
        sdr._center_freq = 915e6
        sdr._gain = 40.0
        sdr._noise_source_active = True
        sdr._executor = DummyExecutor()

        sdr.sdrs[2].device_opened = False

        with self.assertRaisesRegex(RuntimeError, "closed"):
            sdr.read_samples(512, parallel=False)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            sdr.calibrate_delays_and_phases(num_samples=512)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            sdr.center_freq = 100e6
        with self.assertRaisesRegex(RuntimeError, "closed"):
            sdr.sample_rate = 2.048e6
        with self.assertRaisesRegex(RuntimeError, "closed"):
            sdr.gain = 20.0

        device = KrakenSDRDevice(center_freq=433.92e6, sample_rate=2.4e6, gain=10.0)
        device.sdr = sdr
        schema = KrakenSDRSchema()
        schema.bind(device)

        with patch("time.sleep"):
            schema.reset_for_next_holder()

        self.assertFalse(sdr.noise_source_active)
        self.assertFalse(sdr.sdrs[0].bias_tee)
        for idx in (0, 1, 3, 4):
            self.assertEqual(sdr.sdrs[idx].sample_rate, 2.4e6)
            self.assertEqual(sdr.sdrs[idx].center_freq, 433.92e6)
            self.assertEqual(sdr.sdrs[idx].gain, 10.0)
        self.assertEqual(sdr.sdrs[2].center_freq, 915e6)

        sdr.sdrs[0].device_opened = False
        with self.assertRaisesRegex(RuntimeError, "closed"):
            sdr.set_noise_source(False)
        sdr.close()
        self.assertEqual(sdr.sdrs, [])

        # Real KrakenSDR.reset_defaults isolates a single tuner setter failure
        # and does not overwrite the tracked attribute when any tuner failed.
        class FailingRateTuner(FakeTuner):
            @FakeTuner.sample_rate.setter
            def sample_rate(self, value):
                self._check("sample_rate")
                raise OSError("tuner 1 rate write failed")

        sdr2 = KrakenSDR.__new__(KrakenSDR)
        sdr2.sdrs = [FailingRateTuner(i) if i == 1 else FakeTuner(i) for i in range(5)]
        sdr2.num_channels = 5
        sdr2.active_serials = ["1000", "1001", "1002", "1003", "1004"]
        sdr2._sample_rate = 1.024e6
        sdr2._center_freq = 915e6
        sdr2._gain = 40.0
        sdr2._noise_source_active = True
        sdr2._executor = DummyExecutor()

        device2 = KrakenSDRDevice(center_freq=433.92e6, sample_rate=2.4e6, gain=10.0)
        device2.sdr = sdr2
        schema2 = KrakenSDRSchema()
        schema2.bind(device2)
        with patch("time.sleep"), self.assertRaisesRegex(OSError, "tuner 1 rate write failed"):
            schema2.reset_for_next_holder()
        self.assertFalse(sdr2.noise_source_active)
        self.assertEqual(sdr2.sample_rate, 1.024e6)
        self.assertEqual(sdr2.center_freq, 433.92e6)
        self.assertEqual(sdr2.gain, 10.0)
        for idx in (0, 2, 3, 4):
            self.assertEqual(sdr2.sdrs[idx].sample_rate, 2.4e6)
        for idx in range(5):
            self.assertEqual(sdr2.sdrs[idx].center_freq, 433.92e6)
            self.assertEqual(sdr2.sdrs[idx].gain, 10.0)

    def test_reset_isolates_failures_clears_psd_and_restores_is_running(self):
        class FailingNoiseSourceFake(FakeKrakenSDR):
            def set_noise_source(self, enable):
                if not enable:
                    raise OSError("bias tee off failed")
                super().set_noise_source(enable)

        device = KrakenSDRDevice(
            center_freq=433.92e6,
            sample_rate=2.4e6,
            gain=10.0,
            auto_start=True,
        )
        native = FailingNoiseSourceFake()
        device.sdr = native
        device._is_running = True
        schema = KrakenSDRSchema()
        schema.bind(device)

        schema.call_configure(freq=915e6, rate=1.024e6, gain=40.0)
        schema.call_capture_spectrum(fft_size=1024, decay=0.5)
        self.assertEqual(device._smoothed_psd.shape, (5, 1024))

        spec_big = schema.call_capture_spectrum(fft_size=2048, decay=0.5)
        self.assertEqual(spec_big["psd_dbfs"].shape, (5, 2048))

        device._is_running = False
        self.assertFalse(schema.get_is_running())

        with self.assertRaisesRegex(OSError, "bias tee off failed"):
            schema.reset_for_next_holder()

        self.assertEqual(native.sample_rate, 2.4e6)
        self.assertEqual(native.center_freq, 433.92e6)
        self.assertEqual(native.gain, 10.0)
        self.assertIsNone(device._smoothed_psd)
        self.assertTrue(schema.get_is_running())

        spec_next = schema.call_capture_spectrum(fft_size=4096, decay=0.5)
        self.assertEqual(spec_next["psd_dbfs"].shape, (5, 4096))

        schema2, device2, _native2 = _bound_schema()
        device2._is_running = True
        schema2.call_stop()
        self.assertFalse(schema2.get_is_running())
        schema2.reset_for_next_holder()
        self.assertTrue(schema2.get_is_running())

    def test_unaligned_counts_read_whole_packets_and_slice(self):
        schema, _device, native = _bound_schema()

        samples = schema.call_capture_samples(num_samples=1000)
        self.assertEqual(samples.shape, (5, 1000))
        self.assertTrue(samples.flags["C_CONTIGUOUS"])
        self.assertEqual(schema.call_read_samples(num_samples=257).shape, (5, 257))
        self.assertEqual(schema.call_read_frame(num_samples=300)["samples_shape"], [5, 300])
        spectrum = schema.call_capture_spectrum(fft_size=1000)
        self.assertEqual(spectrum["psd_dbfs"].shape, (5, 1000))
        schema.call_calibrate(num_samples=1000)

        self.assertEqual(native.reads, [1024, 512, 512, 1024])
        self.assertEqual(native.calibrations, [1024])

    def test_rejects_out_of_range_counts_before_reading(self):
        schema, _device, native = _bound_schema()
        calls = (
            lambda n: schema.call_capture_samples(num_samples=n),
            lambda n: schema.call_read_samples(num_samples=n),
            lambda n: schema.call_read_frame(num_samples=n),
            lambda n: schema.call_capture_spectrum(fft_size=n),
            lambda n: schema.call_calibrate(num_samples=n),
        )
        for call in calls:
            for bad in (0, -256, _MAX_SAMPLES_PER_CHANNEL + 1):
                with self.assertRaises(ValueError):
                    call(bad)
        self.assertEqual(native.reads, [])
        self.assertEqual(native.calibrations, [])


if __name__ == "__main__":
    unittest.main()
