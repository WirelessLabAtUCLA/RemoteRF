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

"""
RemoteRF Custom Device Schema for KrakenSDR (5-Channel Coherent SDR).

Follows the RemoteRF Custom Device Specification:
https://remoterf.net/docs/server/device-setup/custom/

This schema bridges the physical KrakenSDR 5-channel coherent receiver hardware to RemoteRF clients.
It exposes:
  - Readable device properties: status, connection identity, tuner serials, stream stats, RF parameters
  - Runtime setters: center_freq, sample_rate, gain, noise_source
  - Actions/Calls:
      * capture_samples(): Multi-channel raw IQ samples as a 2D complex64 NumPy array of shape (5, N)
      * capture_spectrum(): Multi-channel FFT power spectral density (PSD) with frequency axis
      * capture_powers(): Instantaneous channel RSSI power (dBFS) for all 5 channels
      * calibrate(): Automatic sample-delay and phase calibration using the onboard noise source
      * set_noise_source(): Toggles the onboard broadband calibration noise source ON or OFF
      * configure(): High-level RF parameter tuning (frequency, sample rate, gain)
      * read_frame(): Synchronized multi-channel frame dictionary with metadata
      * start() / stop(): Control SDR streaming acquisition
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


from remoterf_device_core.idl import DeviceSchema, idl_expose, idl_register


# -----------------------------------------------------------------------------
# Device Controller Implementation
# -----------------------------------------------------------------------------
from remoterf_device_core.drivers.krakensdr.kraken_device import KrakenSDR


class KrakenSDRDevice:
    """
    Hardware controller managing the KrakenSDR receiver for RemoteRF.

    Wraps physical USB communication with the 5 RTL-SDR tuners, multi-channel
    sample acquisition, noise source calibration, and structured conversions.
    """

    def __init__(
        self,
        serials: Optional[List[str]] = None,
        sample_rate: float = 2.048e6,
        center_freq: float = 100.0e6,
        gain: float = 25.4,
    ) -> None:
        self.serials = serials or KrakenSDR.DEFAULT_SERIALS
        self.sample_rate_init = sample_rate
        self.center_freq_init = center_freq
        self.gain_init = gain

        self.sdr: Optional[KrakenSDR] = None
        self._is_running = False

        # Telemetry & performance statistics
        self._total_frames = 0
        self._total_samples = 0
        self._dropped_frames = 0
        self._last_frame_time = 0.0
        self._fps_ema = 0.0
        self._smoothed_psd: Optional[np.ndarray] = None

    def connect(self) -> None:
        """Connect to the 5 physical tuners and initialize hardware."""
        if self.sdr is None:
            self.sdr = KrakenSDR(
                serials=self.serials,
                sample_rate=self.sample_rate_init,
                center_freq=self.center_freq_init,
                gain=self.gain_init,
            )
            # Flush initial buffer
            _ = self.sdr.read_samples(8192, parallel=True)
            self._is_running = True

    def close(self) -> None:
        """Disconnect cleanly from all SDR tuners."""
        self.stop()
        if self.sdr is not None:
            self.sdr.close()
            self.sdr = None

    def start(self) -> bool:
        """Start acquisition streaming."""
        self._is_running = True
        return True

    def stop(self) -> bool:
        """Stop acquisition streaming."""
        self._is_running = False
        if self.sdr is not None:
            self.sdr.set_noise_source(False)
        return True

    @property
    def is_running(self) -> bool:
        return self._is_running and (self.sdr is not None)

    @property
    def num_channels(self) -> int:
        return self.sdr.num_channels if self.sdr else len(self.serials)

    @property
    def active_serials(self) -> List[str]:
        return self.sdr.active_serials if self.sdr else self.serials

    @property
    def center_freq(self) -> float:
        return float(self.sdr.center_freq) if self.sdr else float(self.center_freq_init)

    @center_freq.setter
    def center_freq(self, freq: float) -> None:
        val = float(freq)
        self.center_freq_init = val
        if self.sdr is not None:
            self.sdr.center_freq = val

    @property
    def sample_rate(self) -> float:
        return float(self.sdr.sample_rate) if self.sdr else float(self.sample_rate_init)

    @sample_rate.setter
    def sample_rate(self, rate: float) -> None:
        val = float(rate)
        self.sample_rate_init = val
        if self.sdr is not None:
            self.sdr.sample_rate = val

    @property
    def gain(self) -> float:
        return float(self.sdr.gain) if self.sdr else float(self.gain_init)

    @gain.setter
    def gain(self, gain_val: float) -> None:
        val = float(gain_val)
        self.gain_init = val
        if self.sdr is not None:
            self.sdr.gain = val

    @property
    def noise_source_active(self) -> bool:
        return self.sdr.noise_source_active if self.sdr else False

    def set_noise_source(self, enable: bool) -> bool:
        """Toggle the onboard broadband calibration noise source."""
        if self.sdr is not None:
            self.sdr.set_noise_source(bool(enable))
            return self.sdr.noise_source_active
        return False

    def get_device_info(self) -> Dict[str, Any]:
        """Query connection identity and hardware properties."""
        return {
            "device_type": "krakensdr",
            "model": "KrakenSDR 5-Channel Coherent Receiver",
            "vendor": "KrakenRF",
            "num_channels": self.num_channels,
            "tuners": "5x RTL2832U / Rafael Micro R820T",
            "active_serials": self.active_serials,
            "shared_tcxo": True,
            "internal_noise_source": True,
            "driver_version": "0.1.0",
        }

    def get_stream_stats(self) -> Dict[str, Any]:
        """Return streaming throughput and health metrics."""
        return {
            "total_frames": self._total_frames,
            "total_samples": self._total_samples,
            "dropped_frames": self._dropped_frames,
            "current_fps": round(self._fps_ema, 1),
            "is_running": self.is_running,
            "noise_source_active": self.noise_source_active,
        }

    def _update_stats(self, num_samples: int) -> None:
        now = time.time()
        self._total_frames += 1
        self._total_samples += num_samples
        if self._last_frame_time > 0.0:
            dt = now - self._last_frame_time
            if dt > 0.0:
                inst_fps = 1.0 / dt
                self._fps_ema = 0.8 * self._fps_ema + 0.2 * inst_fps if self._fps_ema > 0.0 else inst_fps
        self._last_frame_time = now

    def capture_samples(self, num_samples: int = 16384, parallel: bool = True) -> np.ndarray:
        """
        Capture simultaneous IQ samples across all 5 channels.
        Returns a 2D complex64 NumPy array of shape (5, num_samples).
        """
        if self.sdr is None:
            raise RuntimeError("KrakenSDR hardware is not connected.")
        samples = self.sdr.read_samples(num_samples=num_samples, parallel=parallel)
        self._update_stats(num_samples)
        return samples

    def capture_spectrum(
        self,
        fft_size: int = 4096,
        decay: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Compute multi-channel FFT power spectral density (PSD) in dBFS.
        Returns:
            frequencies_mhz: 1D array of frequency bins in MHz
            psd_dbfs: 2D float32 array of shape (5, fft_size)
            powers_dbfs: 1D float32 array of instantaneous channel RSSI in dBFS
        """
        samples = self.capture_samples(num_samples=fft_size, parallel=True)

        window = np.hanning(fft_size)
        window_norm = np.sum(window)
        fft_raw = np.fft.fft(samples * window, axis=1)
        fft_shifted = np.fft.fftshift(fft_raw, axes=1)
        inst_psd = (20.0 * np.log10(np.abs(fft_shifted) / window_norm + 1e-12)).astype(np.float32)

        if self._smoothed_psd is None or decay <= 0.0:
            self._smoothed_psd = inst_psd
        else:
            d = max(0.0, min(0.95, float(decay)))
            self._smoothed_psd = (d * self._smoothed_psd + (1.0 - d) * inst_psd).astype(np.float32)

        freq_axis_mhz = (
            np.fft.fftshift(np.fft.fftfreq(fft_size, d=1.0 / self.sample_rate)) / 1e6
            + (self.center_freq / 1e6)
        ).astype(np.float32)

        powers = self.sdr.get_powers_dbfs(samples).astype(np.float32)

        return {
            "frequencies_mhz": freq_axis_mhz,
            "freqs_mhz": freq_axis_mhz,
            "psd_dbfs": self._smoothed_psd,
            "psd_db": self._smoothed_psd,
            "powers_dbfs": powers,
            "noise_source_active": self.noise_source_active,
            "center_freq_mhz": float(self.center_freq / 1e6),
            "sample_rate_msps": float(self.sample_rate / 1e6),
        }

    def capture_powers(self) -> np.ndarray:
        """Return 1D array of channel powers in dBFS."""
        samples = self.capture_samples(num_samples=8192, parallel=True)
        return self.sdr.get_powers_dbfs(samples).astype(np.float32)

    def calibrate(self, num_samples: int = 65536) -> Dict[str, Any]:
        """
        Perform sample delay synchronization and phase calibration using the onboard noise source.
        Returns dictionary with integer sample delays, phase offsets in degrees, and correlation peaks.
        """
        if self.sdr is None:
            raise RuntimeError("KrakenSDR hardware is not connected.")
        res = self.sdr.calibrate_delays_and_phases(num_samples=num_samples)
        return {
            "delays": res["delays"].astype(np.int32),
            "phase_offsets_deg": res["phase_offsets_deg"].astype(np.float32),
            "correlation_peaks": res["correlation_peaks"].astype(np.float32),
            "sample_rate_hz": float(self.sample_rate),
        }

    def read_frame_raw(self, num_samples: int = 8192) -> Dict[str, Any]:
        """Read a synchronized multi-channel frame payload."""
        samples = self.capture_samples(num_samples=num_samples, parallel=True)
        powers = self.sdr.get_powers_dbfs(samples)
        return {
            "frame_number": self._total_frames,
            "timestamp": time.time(),
            "center_freq": self.center_freq,
            "sample_rate": self.sample_rate,
            "noise_source_active": self.noise_source_active,
            "num_channels": self.num_channels,
            "powers_dbfs": powers.tolist(),
            "samples_shape": list(samples.shape),
        }


# -----------------------------------------------------------------------------
# RemoteRF Device Schema Class
# -----------------------------------------------------------------------------
@idl_register("krakensdr")
class KrakenSDRSchema(DeviceSchema):
    """
    RemoteRF Device Schema for KrakenSDR 5-Channel Coherent SDR.

    Exposes RF tuning, multi-channel coherent IQ streaming, spectrum computation,
    and automatic noise-source delay/phase calibration to RemoteRF clients.
    """
    device_type = "krakensdr"
    client_class = "KrakenSDR"
    driver_version = "0.1.0"

    client_modules = {"krakensdr": "remoteRF.drivers.support.krakensdr"}

    @staticmethod
    def make_device(**kwargs) -> Optional[KrakenSDRDevice]:
        """
        Factory method called by RemoteRF server/host when initializing device inventory.

        Parameters from devices.yml 'init' dictionary:
            serials (list of str, optional): Tuner serial numbers (default: ['1000'..'1004'])
            center_freq (float, optional): Center frequency in Hz (default: 100.0 MHz)
            sample_rate (float, optional): Sample rate in Hz (default: 2.048 MSPS)
            gain (float, optional): Tuner gain in dB (default: 25.4 dB)
            auto_start (bool, optional): Start streaming automatically (default: True)

        Returns:
            KrakenSDRDevice instance if connection succeeds, or None if hardware is unavailable.
        """
        serials = kwargs.get("serials")
        center_freq = float(kwargs.get("center_freq", 100.0e6))
        sample_rate = float(kwargs.get("sample_rate", 2.048e6))
        gain = float(kwargs.get("gain", 25.4))
        auto_start = bool(kwargs.get("auto_start", True))

        try:
            print(f"[kraken_sdr] Initializing KrakenSDR (Freq: {center_freq/1e6:.2f} MHz, Rate: {sample_rate/1e6:.2f} MSPS)...")
            dev = KrakenSDRDevice(
                serials=serials,
                sample_rate=sample_rate,
                center_freq=center_freq,
                gain=gain,
            )
            dev.connect()

            if auto_start:
                dev.start()

            print(f"[kraken_sdr] Successfully initialized KrakenSDR with {dev.num_channels} channels.")
            return dev

        except Exception as exc:
            print(f"[kraken_sdr] Failed to initialize KrakenSDR hardware: {exc}")
            return None

    # -------------------------------------------------------------------------
    # Exposed Getters
    # -------------------------------------------------------------------------

    @idl_expose(kind="get", doc="Whether the KrakenSDR is active and streaming.")
    def get_is_running(self) -> bool:
        return self.device.is_running

    @idl_expose(kind="get", doc="Hardware model, channel count, and connection information.")
    def get_device_info(self) -> Dict[str, Any]:
        return self.device.get_device_info()

    @idl_expose(kind="get", doc="List of tuner serial numbers (Channels 0 to 4).")
    def get_tuner_serials(self) -> List[str]:
        return self.device.active_serials

    @idl_expose(kind="get", doc="List of tuner serial numbers (Channels 0 to 4).")
    def get_serials(self) -> List[str]:
        return self.device.active_serials

    @idl_expose(kind="get", doc="Number of coherent receiver channels (typically 5).")
    def get_num_channels(self) -> int:
        return self.device.num_channels

    @idl_expose(kind="get", doc="Throughput, sample counters, and FPS telemetry statistics.")
    def get_stream_stats(self) -> Dict[str, Any]:
        return self.device.get_stream_stats()

    @idl_expose(kind="get", doc="Current center frequency in Hz.")
    def get_center_freq(self) -> float:
        return self.device.center_freq

    @idl_expose(kind="set", doc="Set center frequency in Hz across all channels.")
    def set_center_freq(self, value: float) -> None:
        self.device.center_freq = float(value)

    @idl_expose(kind="get", doc="Current sample rate in Hz.")
    def get_sample_rate(self) -> float:
        return self.device.sample_rate

    @idl_expose(kind="set", doc="Set sample rate in Hz across all channels.")
    def set_sample_rate(self, value: float) -> None:
        self.device.sample_rate = float(value)

    @idl_expose(kind="get", doc="Current tuner gain in dB.")
    def get_gain(self) -> float:
        return self.device.gain

    @idl_expose(kind="set", doc="Set tuner gain in dB across all channels.")
    def set_gain(self, value: float) -> None:
        self.device.gain = float(value)

    @idl_expose(kind="get", doc="Whether the internal calibration noise source is active.")
    def get_noise_source_active(self) -> bool:
        return self.device.noise_source_active

    @idl_expose(kind="set", doc="Turn the internal calibration noise source ON or OFF.")
    def set_noise_source_active(self, value: bool) -> None:
        self.device.set_noise_source(bool(value))

    @idl_expose(kind="get", doc="Whether the internal calibration noise source is active.")
    def get_noise_source(self) -> bool:
        return self.device.noise_source_active

    @idl_expose(kind="set", doc="Turn the internal calibration noise source ON or OFF.")
    def set_noise_source(self, value: bool) -> None:
        self.device.set_noise_source(bool(value))

    # -------------------------------------------------------------------------
    # Exposed Calls
    # -------------------------------------------------------------------------

    @idl_expose(kind="call", doc="Capture simultaneous raw IQ samples as a 2D complex64 NumPy array of shape (5, N).")
    def call_capture_samples(self, num_samples: int = 16384, parallel: bool = True) -> np.ndarray:
        return self.device.capture_samples(num_samples=int(num_samples), parallel=bool(parallel))

    @idl_expose(kind="call", doc="Capture simultaneous raw IQ samples as a 2D complex64 NumPy array of shape (5, N).")
    def call_read_samples(self, num_samples: int = 16384, parallel: bool = True) -> np.ndarray:
        return self.device.capture_samples(num_samples=int(num_samples), parallel=bool(parallel))

    @idl_expose(kind="call", doc="Capture multi-channel FFT power spectral density (PSD) with frequency axis.")
    def call_capture_spectrum(self, fft_size: int = 4096, decay: float = 0.0) -> Dict[str, Any]:
        return self.device.capture_spectrum(fft_size=int(fft_size), decay=float(decay))

    @idl_expose(kind="call", doc="Capture instantaneous channel RSSI power in dBFS for all 5 channels.")
    def call_capture_powers(self) -> np.ndarray:
        return self.device.capture_powers()

    @idl_expose(kind="call", doc="Perform automated sample-delay and phase calibration using the noise source.")
    def call_calibrate(self, num_samples: int = 65536) -> Dict[str, Any]:
        return self.device.calibrate(num_samples=int(num_samples))

    @idl_expose(kind="call", doc="Toggle internal calibration noise source ON (True) or OFF (False).")
    def call_set_noise_source(self, enable: bool = True) -> bool:
        return self.device.set_noise_source(bool(enable))

    @idl_expose(kind="call", doc="Configure RF parameters (frequency, sample rate, gain).")
    def call_configure(
        self,
        freq: Optional[float] = None,
        rate: Optional[float] = None,
        gain: Optional[float] = None,
    ) -> Dict[str, Any]:
        if freq is not None:
            self.device.center_freq = float(freq)
        if rate is not None:
            self.device.sample_rate = float(rate)
        if gain is not None:
            self.device.gain = float(gain)
        return {
            "center_freq": self.device.center_freq,
            "sample_rate": self.device.sample_rate,
            "gain": self.device.gain,
        }

    @idl_expose(kind="call", doc="Read synchronized multi-channel frame dictionary with metadata.")
    def call_read_frame(self, num_samples: int = 8192) -> Dict[str, Any]:
        return self.device.read_frame_raw(num_samples=int(num_samples))

    @idl_expose(kind="call", doc="Start SDR acquisition.")
    def call_start(self) -> bool:
        return self.device.start()

    @idl_expose(kind="call", doc="Stop SDR acquisition.")
    def call_stop(self) -> bool:
        return self.device.stop()
