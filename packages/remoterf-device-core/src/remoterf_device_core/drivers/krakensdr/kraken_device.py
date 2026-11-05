"""
kraken_device.py - Python interface for KrakenSDR (5-channel coherent SDR)

KrakenSDR contains 5x RTL2832U tuners sharing a common clock (TCXO) and an
onboard calibration noise source controlled via GPIO on Tuner 0 (serial 1000).

Default Serial Mapping:
  - Channel 0: Serial 1000 (controls noise source switch via bias tee GPIO)
  - Channel 1: Serial 1001
  - Channel 2: Serial 1002
  - Channel 3: Serial 1003
  - Channel 4: Serial 1004
"""

import time
import concurrent.futures
from typing import List, Optional, Tuple, Dict
import numpy as np
from scipy import signal
from rtlsdr import RtlSdr


class KrakenSDR:
    """Interface for KrakenSDR 5-channel receiver."""

    DEFAULT_SERIALS = ['1000', '1001', '1002', '1003', '1004']

    def __init__(
        self,
        serials: Optional[List[str]] = None,
        sample_rate: float = 2.048e6,
        center_freq: float = 100.0e6,
        gain: float = 25.4,
    ):
        """
        Initialize KrakenSDR.

        :param serials: List of tuner serial numbers to open (default: ['1000', ..., '1004'])
        :param sample_rate: Sample rate in Hz (e.g. 2.048e6 or 2.4e6)
        :param center_freq: Center frequency in Hz (e.g. 100e6)
        :param gain: Tuner gain in dB or 'auto'
        """
        self.target_serials = serials or self.DEFAULT_SERIALS
        self.num_channels = len(self.target_serials)
        self.sdrs: List[RtlSdr] = []
        self.active_serials: List[str] = []
        self._sample_rate = sample_rate
        self._center_freq = center_freq
        self._gain = gain
        self._noise_source_active = False
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.num_channels, thread_name_prefix="KrakenRead"
        )

        self._connect()

    def _connect(self):
        """Find and open all target RTL-SDR tuners by serial number."""
        available_serials = RtlSdr.get_device_serial_addresses()
        print(f"[KrakenSDR] Detected {len(available_serials)} RTL-SDR devices: {available_serials}")

        for s in self.target_serials:
            if s not in available_serials:
                raise RuntimeError(
                    f"[KrakenSDR] Expected tuner with serial '{s}' not found. "
                    f"Available serials: {available_serials}"
                )
            idx = RtlSdr.get_device_index_by_serial(s)
            sdr = RtlSdr(idx)
            sdr.sample_rate = self._sample_rate
            sdr.center_freq = self._center_freq
            sdr.gain = self._gain
            self.sdrs.append(sdr)
            self.active_serials.append(s)

        # Ensure noise source is OFF initially
        self.set_noise_source(False)
        print(f"[KrakenSDR] Successfully opened {len(self.sdrs)} tuners (Serials: {self.active_serials}).")

    @property
    def sample_rate(self) -> float:
        return self._sample_rate

    @sample_rate.setter
    def sample_rate(self, rate: float):
        self._sample_rate = rate
        for sdr in self.sdrs:
            sdr.sample_rate = rate

    @property
    def center_freq(self) -> float:
        return self._center_freq

    @center_freq.setter
    def center_freq(self, freq: float):
        self._center_freq = freq
        for sdr in self.sdrs:
            sdr.center_freq = freq

    @property
    def gain(self):
        return self._gain

    @gain.setter
    def gain(self, gain):
        self._gain = gain
        for sdr in self.sdrs:
            sdr.gain = gain

    def set_noise_source(self, enable: bool):
        """
        Turn the onboard broadband calibration noise source ON or OFF.
        Controlled via Tuner 0 (Serial 1000) GPIO / bias tee.
        When ON: all 5 channels are disconnected from antennas and connected
                 to the internal noise source for phase/delay calibration.
        When OFF: all 5 channels are connected to their respective external antennas.
        """
        if not self.sdrs:
            return
        # Channel 0 (serial 1000) controls the noise generator switch
        self.sdrs[0].set_bias_tee(bool(enable))
        self._noise_source_active = bool(enable)
        # Brief settling time for RF switch and noise diode
        time.sleep(0.05)

    @property
    def noise_source_active(self) -> bool:
        return self._noise_source_active

    def read_samples(self, num_samples: int = 32768, parallel: bool = True) -> np.ndarray:
        """
        Read I/Q samples from all channels.

        :param num_samples: Number of complex samples to read per channel.
        :param parallel: Whether to read all channels concurrently using threads.
        :return: 2D numpy array of complex64 with shape (num_channels, num_samples).
        """
        if parallel:
            futures = [self._executor.submit(sdr.read_samples, num_samples) for sdr in self.sdrs]
            results = [f.result() for f in futures]
        else:
            results = [sdr.read_samples(num_samples) for sdr in self.sdrs]

        return np.array(results, dtype=np.complex64)

    def get_powers_dbfs(self, samples: np.ndarray) -> np.ndarray:
        """
        Compute mean power in dBFS for each channel.
        :param samples: Array of shape (num_channels, num_samples).
        :return: Array of shape (num_channels,) containing power in dBFS.
        """
        mean_pwr = np.mean(np.abs(samples) ** 2, axis=1)
        return 10.0 * np.log10(mean_pwr + 1e-12)

    def calibrate_delays_and_phases(
        self, num_samples: int = 65536, max_lag_search: Optional[int] = None
    ) -> Dict[str, np.ndarray]:
        """
        Perform sample delay synchronization and phase calibration using the onboard noise source.

        1. Enables onboard noise source (all channels receive identical broadband noise).
        2. Captures simultaneous sample buffers across all 5 channels.
        3. Disables onboard noise source.
        4. Cross-correlates Channel 0 against Channels 1..N-1.
        5. Calculates the exact integer sample delay (lag) and phase offset for each channel.

        :param num_samples: Number of samples to capture for calibration.
        :param max_lag_search: Optional limit on lag search range in samples.
        :return: Dictionary containing 'delays' (integer sample lags) and 'phase_offsets_deg'.
        """
        print("[KrakenSDR] Enabling noise source for calibration...")
        self.set_noise_source(True)
        try:
            # Discard one read to flush any previous buffers
            _ = self.read_samples(8192, parallel=True)
            noise_samples = self.read_samples(num_samples, parallel=True)
        finally:
            self.set_noise_source(False)
            print("[KrakenSDR] Noise source disabled.")

        ref = noise_samples[0]
        ref_norm = np.linalg.norm(ref)

        delays = np.zeros(self.num_channels, dtype=int)
        phase_offsets_deg = np.zeros(self.num_channels, dtype=float)
        corr_peaks = np.zeros(self.num_channels, dtype=float)

        for ch in range(self.num_channels):
            if ch == 0:
                corr_peaks[0] = 1.0
                delays[0] = 0
                phase_offsets_deg[0] = 0.0
                continue

            sig = noise_samples[ch]
            sig_norm = np.linalg.norm(sig)
            
            # Cross-correlation via FFT
            corr = signal.correlate(sig, ref, mode='full')
            lags = signal.correlation_lags(len(sig), len(ref))

            if max_lag_search is not None:
                mask = np.abs(lags) <= max_lag_search
                corr_sub = corr[mask]
                lags_sub = lags[mask]
                peak_idx = np.argmax(np.abs(corr_sub))
                best_lag = lags_sub[peak_idx]
                peak_val = corr_sub[peak_idx]
            else:
                peak_idx = np.argmax(np.abs(corr))
                best_lag = lags[peak_idx]
                peak_val = corr[peak_idx]

            norm_peak = np.abs(peak_val) / (sig_norm * ref_norm + 1e-12)
            phase_deg = np.angle(peak_val, deg=True)

            delays[ch] = best_lag
            phase_offsets_deg[ch] = phase_deg
            corr_peaks[ch] = norm_peak

        return {
            'delays': delays,
            'phase_offsets_deg': phase_offsets_deg,
            'correlation_peaks': corr_peaks,
            'noise_samples': noise_samples,
        }

    def close(self):
        """Close all tuners safely."""
        self.set_noise_source(False)
        for sdr in self.sdrs:
            try:
                sdr.close()
            except Exception:
                pass
        self.sdrs.clear()
        self._executor.shutdown(wait=False)
        print("[KrakenSDR] All SDR devices closed.")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
