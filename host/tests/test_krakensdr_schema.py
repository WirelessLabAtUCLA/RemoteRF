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

import sys
import types
import unittest

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
