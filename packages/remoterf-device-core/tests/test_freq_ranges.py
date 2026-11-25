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

"""Tests for shared multi-range frequency limits across Pluto, HackRF, and USRP."""
from __future__ import annotations

import string
import sys
import types
from unittest.mock import patch

import numpy as np
import pytest

sys.modules.setdefault("adi", types.ModuleType("adi"))

from remoterf_device_core import manifest
from remoterf_device_core.drivers.adalm_pluto.lockout_keys import LOCKOUT_KEYS
from remoterf_device_core.drivers.adalm_pluto.pluto_mimo_schema import PlutoMIMOSchema
from remoterf_device_core.drivers.adalm_pluto.pluto_schema import PlutoSchema, parse_lockouts
from remoterf_device_core.drivers.hackrf.hackrf_schema import HackRFSchema
from remoterf_device_core.drivers.usrp.usrp_schema import UsrpSchema, _usrp_args
from remoterf_device_core.drivers.usrp_phaser.usrp_phaser_schema import UsrpPhaserSchema
from remoterf_device_core.freq_ranges import (
    FREQ_RANGE_KEYS,
    NON_PLUTO_LOCKOUT_KEYS,
    DirectionFreqLimits,
    merge_ranges,
    parse_freq_limits,
    parse_range_list,
    strip_freq_limit_keys,
)
from remoterf_device_core.v2_errors import RemoteRFPolicyError


# ── Parsing and range math ────────────────────────────────────────────


def test_parse_lists_tuples_and_scientific_strings():
    limits = parse_freq_limits(
        {
            "tx_allow": (("902e6", 928_000_000), [2.40e9, "2.48e9"]),
            "tx_deny": [(2.45e9, 2.46e9)],
            "rx_allow": [[70e6, 6e9]],
            "rx_deny": (),
            "admin_group_override": True,
        }
    )
    assert limits is not None
    assert limits.tx.allow == ((902e6, 928e6), (2.40e9, 2.48e9))
    assert limits.tx.deny == ((2.45e9, 2.46e9),)
    assert limits.rx.allow == ((70e6, 6e9),)
    assert limits.rx.deny == ()
    assert limits.admin_group_override is True


def test_parse_no_keys_returns_none():
    assert parse_freq_limits(None) is None
    assert parse_freq_limits({}) is None
    assert parse_freq_limits({"serial": "abc", "profile": "b205mini"}) is None


def test_merge_overlapping_touching_and_unsorted_ranges():
    merged = merge_ranges(
        [
            (2.44e9, 2.48e9),
            (915e6, 928e6),
            (902e6, 915e6),  # touches 915e6
            (2.40e9, 2.45e9),  # overlaps 2.44e9..2.48e9
            (2.41e9, 2.42e9),  # contained
        ]
    )
    assert merged == ((902e6, 928e6), (2.40e9, 2.48e9))
    assert parse_range_list("tx_allow", [(915e6, 915e6)]) == ((915e6, 915e6),)


def test_legacy_min_max_aliases_and_deny_combination():
    limits = parse_freq_limits(
        {
            "tx_min_freq": 902e6,
            "tx_max_freq": 928e6,
            "tx_deny": [[910e6, 912e6]],
            "rx_min_freq": "70e6",
            "rx_max_freq": "6e9",
        }
    )
    assert limits is not None
    assert limits.tx.allow == ((902e6, 928e6),)
    assert limits.tx.deny == ((910e6, 912e6),)
    assert limits.rx.allow == ((70e6, 6e9),)
    assert limits.rx.deny == ()


@pytest.mark.parametrize(
    "init",
    [
        {"tx_min_freq": 902e6, "tx_max_freq": 928e6, "tx_allow": [[902e6, 928e6]]},
        {"tx_min_freq": 902e6, "tx_allow": [[902e6, 928e6]]},
        {"tx_max_freq": 928e6, "tx_allow": [[902e6, 928e6]]},
        {"rx_min_freq": 70e6, "rx_max_freq": 6e9, "rx_allow": [[70e6, 6e9]]},
        {"rx_min_freq": 70e6, "rx_allow": [[70e6, 6e9]]},
    ],
)
def test_mixed_form_raises_config_error(init):
    with pytest.raises(ValueError, match="cannot specify both"):
        parse_freq_limits(init)
    with pytest.raises(ValueError, match="cannot specify both"):
        parse_lockouts(init)


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {1: [[902e6, 928e6]]},
        {"TX_ALLOW": [[902e6, 928e6]]},
        {"Tx_Deny": [[902e6, 928e6]]},
        {"RX_ALLOW": [[902e6, 928e6]]},
        {"tx_allow": "902e6"},
        {"tx_allow": 902e6},
        {"tx_allow": None},
        {"tx_allow": {}},
        {"tx_allow": [[902e6]]},
        {"tx_allow": [[902e6, 915e6, 928e6]]},
        {"tx_allow": ["902e6"]},
        {"tx_allow": [[928e6, 902e6]]},  # low > high
        {"tx_allow": [[float("nan"), 928e6]]},
        {"tx_allow": [[902e6, float("inf")]]},
        {"tx_allow": [[float("-inf"), 928e6]]},
        {"tx_allow": [[-1.0, 928e6]]},
        {"tx_allow": [[True, 928e6]]},
        {"tx_allow": [[902e6, False]]},
        {"tx_allow": [[902e6, "not-a-number"]]},
        {"tx_allow": [[902e6, 1j]]},
        {"tx_deny": [[2.46e9, 2.45e9]]},
        {"rx_allow": [[None, 6e9]]},
        {"tx_min_freq": 902e6},
        {"rx_max_freq": 6e9},
        {"admin_group_override": "true"},
        {"admin_group_override": 1},
    ],
)
def test_invalid_inputs_fail_closed(bad):
    with pytest.raises(ValueError) as exc:
        parse_freq_limits(bad)
    assert "\n" not in str(exc.value)


def test_tx_occupied_band_and_rx_center_freq_rules():
    limits = parse_freq_limits(
        {
            "tx_allow": [[902e6, 915e6], [915e6, 928e6], [2.40e9, 2.48e9]],
            "tx_deny": [[2.45e9, 2.46e9]],
            "rx_allow": [[70e6, 6e9]],
            "rx_deny": [[1.57e9, 1.58e9]],
        }
    )
    assert limits is not None
    # Inside one merged range (touching 902..915 and 915..928 merged into 902..928).
    assert limits.tx.allows_band(910e6, 920e6)
    assert limits.tx.allows_band(902e6, 928e6)
    assert limits.tx.allows_band(2.41e9, 2.44e9)
    assert limits.tx.allows_band(2.465e9, 2.475e9)

    # Straddling two disjoint allow ranges -> refused.
    assert not limits.tx.allows_band(920e6, 2.41e9)
    # Extending past an allow boundary -> refused.
    assert not limits.tx.allows_band(901e6, 910e6)
    # Overlapping or touching a deny range -> refused (deny wins over allow).
    assert not limits.tx.allows_band(2.452e9, 2.458e9)
    assert not limits.tx.allows_band(2.44e9, 2.45e9)  # touches left edge of deny
    assert not limits.tx.allows_band(2.46e9, 2.47e9)  # touches right edge of deny
    assert not limits.tx.allows_band(2.44e9, 2.47e9)  # straddles deny

    # RX center frequency checks.
    assert limits.rx.allows_freq(915e6)
    assert limits.rx.allows_freq(70e6)
    assert limits.rx.allows_freq(6e9)
    assert not limits.rx.allows_freq(69e6)
    assert not limits.rx.allows_freq(1.57e9)  # deny boundary
    assert not limits.rx.allows_freq(1.575e9)  # inside deny
    assert not limits.rx.allows_freq(1.58e9)  # deny boundary


def test_no_allow_list_means_everything_outside_deny_is_allowed():
    limits = parse_freq_limits({"tx_deny": [[2.45e9, 2.46e9]], "rx_deny": [[100e6, 108e6]]})
    assert limits is not None
    assert limits.tx.allow is None
    assert limits.tx.allows_band(905e6, 925e6)
    assert not limits.tx.allows_band(2.44e9, 2.45e9)  # touches deny
    assert not limits.tx.allows_band(2.451e9, 2.455e9)
    assert limits.rx.allows_freq(99e6)
    assert not limits.rx.allows_freq(100e6)
    assert not limits.rx.allows_freq(104e6)


def test_default_park_freq_selection():
    # Multi-range: midpoint of first range when uncovered.
    d1 = DirectionFreqLimits(allow=((902e6, 928e6), (2.40e9, 2.48e9)), deny=((2.45e9, 2.46e9),))
    assert d1.default_park_freq() == 915e6

    # First range completely covered by deny: midpoint of second range.
    d2 = DirectionFreqLimits(allow=((902e6, 928e6), (2.40e9, 2.48e9)), deny=((900e6, 930e6),))
    assert d2.default_park_freq() == 2.44e9

    # First range split by deny: midpoint of first uncovered segment (not inside deny).
    d3 = DirectionFreqLimits(allow=((902e6, 928e6),), deny=((910e6, 920e6),))
    park = d3.default_park_freq()
    assert park == 906e6
    assert d3.allows_freq(park)

    # All allowed ranges covered by deny: returns None.
    d4 = DirectionFreqLimits(allow=((902e6, 928e6),), deny=((900e6, 930e6),))
    assert d4.default_park_freq() is None


def test_strip_freq_limit_keys():
    cleaned = strip_freq_limit_keys(
        {
            "serial": "31A2B3C",
            "profile": "usrp2901",
            "tx_allow": [[902e6, 928e6]],
            "tx_deny": [[2.45e9, 2.46e9]],
            "rx_allow": [[70e6, 6e9]],
            "rx_deny": [],
            "tx_min_freq": 902e6,
            "admin_group_override": True,
        }
    )
    assert cleaned == {"serial": "31A2B3C", "profile": "usrp2901"}


# ── Pluto & Pluto MIMO multi-range enforcement ────────────────────────


class _FakePluto:
    def __init__(self):
        self.tx_lo = 915_000_000
        self.rx_lo = 915_000_000
        self.sample_rate = 2_000_000
        self.tx_rate = 2_000_000
        self.tx_hardwaregain_chan0 = -20
        self.tx_hardwaregain_chan1 = -20
        self.tx_enabled_channels = [0, 1]
        self.rx_enabled_channels = [0, 1]
        self.dds_scales = [0.0] * 8
        self.tx_cyclic_buffer = False
        self.loopback = 0
        self.tx_calls = 0

    def _get_iio_attr(self, channel, name, output):
        assert (channel, name) == ("voltage0", "sampling_frequency")
        return self.tx_rate if output else self.sample_rate

    def disable_dds(self):
        pass

    def tx_destroy_buffer(self):
        pass

    def tx(self, samples):
        self.tx_calls += 1


@pytest.mark.parametrize("cls", [PlutoSchema, PlutoMIMOSchema])
def test_pluto_multi_range_and_deny_enforcement(cls):
    assert FREQ_RANGE_KEYS.isdisjoint(LOCKOUT_KEYS)
    cfg = parse_lockouts(
        {
            "tx_allow": [[902e6, 928e6], [2.40e9, 2.48e9]],
            "tx_deny": [[2.45e9, 2.46e9]],
            "rx_allow": [[70e6, 6e9]],
            "rx_deny": [[1.57e9, 1.58e9]],
            "admin_group_override": True,
        }
    )
    assert cfg is not None
    assert cfg["park_tx_lo"] == 915e6
    assert not cfg["allow_rf_loopback"]

    radio = _FakePluto()
    radio.tx_lo = 2_455_000_000  # inside deny: settle should park it
    schema = cls()
    schema.bind(radio)
    schema.configure_lockouts(cfg)
    assert radio.tx_lo == 915_000_000

    # Valid TX in first and second ranges.
    schema.dispatch("call_tx", {"value": [1j, 2j]})
    assert radio.tx_calls == 1
    schema.dispatch("call_tx_destroy_buffer", {})
    schema.dispatch("set_tx_lo", {"value": 2_420_000_000})
    schema.dispatch("call_tx", {"value": [1j, 2j]})
    assert radio.tx_calls == 2
    schema.dispatch("call_tx_destroy_buffer", {})

    # Center inside deny -> refused.
    with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz.*2400–2480 MHz.*2450–2460 MHz"):
        schema.dispatch("set_tx_lo", {"value": 2_455_000_000})
    assert radio.tx_lo == 2_420_000_000

    # Occupied band touching deny edge (2449 MHz ± 1 MHz = 2448–2450 MHz) -> refused.
    schema.dispatch("set_tx_lo", {"value": 2_449_000_000})
    with pytest.raises(RemoteRFPolicyError, match=r"2448–2450 MHz.*2450–2460 MHz"):
        schema.dispatch("call_tx", {"value": [1j]})
    assert radio.tx_calls == 2

    # RX checks.
    schema.dispatch("set_rx_lo", {"value": 915_000_000})
    with pytest.raises(RemoteRFPolicyError, match=r"1575 MHz.*1570–1580 MHz"):
        schema.dispatch("set_rx_lo", {"value": 1_575_000_000})

    # Admin bypass works when both admin_group_override and lockout_bypass are True.
    schema.lockout_bypass = True
    schema.dispatch("set_tx_lo", {"value": 2_455_000_000})
    schema.dispatch("call_tx", {"value": [1j]})
    assert radio.tx_calls == 3


def test_pluto_park_tx_lo_skips_covered_first_range_and_fails_if_empty():
    cfg = parse_lockouts(
        {
            "tx_allow": [[902e6, 928e6], [2.40e9, 2.48e9]],
            "tx_deny": [[900e6, 930e6]],
        }
    )
    assert cfg is not None
    assert cfg["park_tx_lo"] == 2.44e9

    with pytest.raises(ValueError, match="park_tx_lo"):
        parse_lockouts({"tx_allow": [[902e6, 928e6]], "tx_deny": [[900e6, 930e6]]})

    # RX-only range config keeps allow_rf_loopback=True (matching legacy rx_min_freq/rx_max_freq).
    rx_only = parse_lockouts({"rx_allow": [[70e6, 6e9]], "rx_deny": []})
    assert rx_only is not None and rx_only["allow_rf_loopback"] is True


def test_pluto_deny_ranges_not_restricted_to_hardware_bounds():
    tx_cfg = parse_lockouts({"tx_deny": [[0, 400e6]]})
    assert tx_cfg is not None
    assert tx_cfg["tx_deny"] == ((0.0, 400e6),)
    assert tx_cfg["park_tx_lo"] == 915e6

    rx_cfg = parse_lockouts({"rx_deny": [[5.9e9, 7.2e9]]})
    assert rx_cfg is not None
    assert rx_cfg["rx_deny"] == ((5.9e9, 7.2e9),)

    # Pluto runtime enforcement works with out-of-hardware-bounds deny ranges.
    radio = _FakePluto()
    schema = PlutoSchema()
    schema.bind(radio)
    schema.configure_lockouts(parse_lockouts({"tx_deny": [[0, 400e6]], "rx_deny": [[5.9e9, 7.2e9]]}))
    with pytest.raises(RemoteRFPolicyError, match=r"0–400 MHz"):
        schema.dispatch("set_tx_lo", {"value": 300_000_000})
    with pytest.raises(RemoteRFPolicyError, match=r"5900–7200 MHz"):
        schema.dispatch("set_rx_lo", {"value": 5_950_000_000})
    schema.dispatch("set_rx_lo", {"value": 5_800_000_000})

    # Parking selects a valid frequency inside Pluto's 70 MHz-6 GHz range, or fails if nothing remains.
    park_cfg = parse_lockouts({"tx_deny": [[0, 1e9]]})
    assert park_cfg is not None and 1e9 < park_cfg["park_tx_lo"] < 6e9
    with pytest.raises(ValueError, match="park_tx_lo"):
        parse_lockouts({"tx_deny": [[0, 10e9]]})

    # Hardware bounds still apply to allow ranges, and negative deny frequencies still fail.
    with pytest.raises(ValueError, match="between"):
        parse_lockouts({"tx_allow": [[0, 400e6]]})
    with pytest.raises(ValueError, match="between"):
        parse_lockouts({"rx_allow": [[5.9e9, 7.2e9]]})
    with pytest.raises(ValueError, match=">= 0"):
        parse_lockouts({"tx_deny": [[-1.0, 400e6]]})


def test_unknown_or_misspelled_freq_limit_keys_fail_closed():
    for bad_key in (
        "tx_alow",
        "tx_deny_ranges",
        "rx_alow",
        "rx_denied",
        "tx_range",
        "freq_allow",
        "TX_ALOW",
        "admin_override",
        "tx_lockout",
        "rx_only",
    ):
        with pytest.raises(ValueError, match="unknown frequency-limit init key") as exc:
            parse_freq_limits({bad_key: [[902e6, 928e6]]})
        assert "\n" not in str(exc.value)

    # Every single-edit typo of every non-Pluto lockout key fails closed.
    characters = string.ascii_lowercase + "_-"
    for key in sorted(NON_PLUTO_LOCKOUT_KEYS):
        typos = {key[:i] + key[i + 1:] for i in range(len(key))}
        typos.update(key[:i] + key[i + 1] + key[i] + key[i + 2:] for i in range(len(key) - 1))
        typos.update(key[:i] + ch + key[i + 1:] for i in range(len(key)) for ch in characters)
        typos.update(key[:i] + ch + key[i:] for i in range(len(key) + 1) for ch in characters)
        for typo in sorted(typos - NON_PLUTO_LOCKOUT_KEYS):
            with pytest.raises(ValueError):
                parse_freq_limits({typo: 1})

    # Legitimate HackRF / USRP / Phaser init keys are unaffected.
    assert parse_freq_limits(
        {
            "serial": "31A2B3C",
            "addr": "192.168.10.2",
            "device_index": 0,
            "profile": "usrp2901",
            "type": "b200",
            "args": "num_recv_frames=256",
            "master_clock_rate": 16e6,
            "num_recv_frames": 256,
            "phaser_uri": "ip:localhost",
        }
    ) is None


# ── HackRF enforcement ────────────────────────────────────────────────


class _FakeHackRF:
    def __init__(self):
        self.center_freq = 915_000_000
        self.sample_rate = 4_000_000
        self.buffer = bytearray(b"\x01\x02\x03\x04")
        self.tx_starts = 0
        self.rx_reads = 0

    def start_tx(self):
        self.tx_starts += 1

    def stop_tx(self):
        pass

    def read_samples(self, count):
        self.rx_reads += 1
        return np.zeros(count, dtype=np.complex64)


def test_hackrf_no_limits_unchanged():
    dev = _FakeHackRF()
    schema = HackRFSchema()
    schema.bind(dev)
    schema.set_center_freq(100_000_000)
    assert dev.center_freq == 100_000_000
    assert schema.call_start_tx() == 4
    schema.call_stop_tx()
    assert schema.call_read_samples(8).size == 8


def test_hackrf_tx_rx_half_duplex_and_bypass():
    dev = _FakeHackRF()
    schema = HackRFSchema()
    schema.bind(dev)
    schema.configure_lockouts(
        parse_freq_limits(
            {
                "tx_allow": [[902e6, 928e6], [2.40e9, 2.48e9]],
                "tx_deny": [[2.45e9, 2.46e9]],
                "rx_allow": [[88e6, 108e6], [902e6, 928e6]],
                "rx_deny": [[98e6, 100e6]],
                "admin_group_override": True,
            }
        )
    )

    # Half-duplex: RX-only band (104 MHz) can tune while idle and RX, but cannot start TX.
    schema.set_center_freq(104_000_000)
    assert dev.center_freq == 104_000_000
    assert schema.call_read_samples(4).size == 4
    with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz.*2400–2480 MHz"):
        schema.call_start_tx()
    assert dev.tx_starts == 0

    # Frequency forbidden for BOTH TX and RX refuses right at set_center_freq.
    with pytest.raises(RemoteRFPolicyError, match=r"500 MHz"):
        schema.set_center_freq(500_000_000)
    assert dev.center_freq == 104_000_000

    # RX deny blocks read_samples even inside rx_allow.
    dev.center_freq = 99_000_000
    with pytest.raises(RemoteRFPolicyError, match=r"99 MHz.*98–100 MHz"):
        schema.call_read_samples(4)
    assert dev.rx_reads == 1

    # TX band touching deny edge (2448 MHz ± 2 MHz = 2446–2450 MHz) refused on start_tx.
    schema.set_center_freq(2_448_000_000)
    with pytest.raises(RemoteRFPolicyError, match=r"2446–2450 MHz.*2450–2460 MHz"):
        schema.call_start_tx()
    assert dev.tx_starts == 0

    # Valid TX start, then live retune / rate widening into deny or outside band refused.
    schema.set_center_freq(2_420_000_000)
    schema.call_start_tx()
    assert dev.tx_starts == 1
    with pytest.raises(RemoteRFPolicyError, match=r"2450–2460 MHz"):
        schema.set_center_freq(2_448_000_000)
    assert dev.center_freq == 2_420_000_000  # no silent retune
    with pytest.raises(RemoteRFPolicyError, match=r"2450–2460 MHz"):
        schema.set_sample_rate(60_000_000)  # 2420 ± 30 -> 2390..2450
    assert dev.sample_rate == 4_000_000

    # Admin bypass allows live retune into denied band when enabled.
    schema.lockout_bypass = True
    schema.set_center_freq(2_455_000_000)
    assert dev.center_freq == 2_455_000_000
    schema.call_stop_tx()

    # Without admin_group_override, lockout_bypass has no effect.
    schema.configure_lockouts(parse_freq_limits({"tx_allow": [[902e6, 928e6]]}))
    schema.lockout_bypass = True
    with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
        schema.call_start_tx()


# ── USRP v1 & v2 enforcement ──────────────────────────────────────────


class _FakeTuneRequest:
    def __init__(self, target_freq=0.0, lo_offset=0.0):
        self.target_freq = float(target_freq)
        self.rf_freq = float(target_freq) + float(lo_offset)
        self.dsp_freq = 0.0
        self.rf_freq_policy = "MANUAL" if lo_offset else "AUTO"
        self.dsp_freq_policy = "AUTO"


class _FakeStreamArgs:
    def __init__(self, cpu_format, otw_format):
        self.cpu_format = cpu_format
        self.otw_format = otw_format
        self.channels = []
        self.args = ""


class _FakeTXMetadata:
    def __init__(self):
        self.has_time_spec = False
        self.start_of_burst = False
        self.end_of_burst = False


class _FakeRXMetadata:
    def __init__(self):
        self.error_code = "none"
        self.has_time_spec = False

    def reset(self):
        pass


class _FakeStreamer:
    def __init__(self):
        self.sent = 0
        self.recvd = 0

    def send(self, samples, metadata, timeout):
        self.sent += 1
        return len(samples)

    def recv(self, buf, metadata, timeout):
        self.recvd += 1
        return len(buf)


class _FakeUSRP:
    def __init__(self):
        self.tx_freqs = {0: 915e6, 1: 915e6}
        self.rx_freqs = {0: 915e6, 1: 915e6}
        self.tx_rates = {0: 4e6, 1: 4e6}
        self.rx_rates = {0: 4e6, 1: 4e6}
        self.tx_streamer = _FakeStreamer()
        self.rx_streamer = _FakeStreamer()
        self.waveforms_sent = 0
        self.rx_samps_calls = 0

    def get_tx_freq(self, chan=0):
        return self.tx_freqs[chan]

    def set_tx_freq(self, tune_request, chan=0):  # UHD's own keyword
        self.tx_freqs[chan] = float(getattr(tune_request, "target_freq", tune_request))
        return self.tx_freqs[chan]

    def set_tx_lo_freq(self, freq, name, chan=0):
        self.tx_freqs[chan] = float(freq)
        return float(freq)

    def get_rx_freq(self, chan=0):
        return self.rx_freqs[chan]

    def set_rx_freq(self, tune_request, chan=0):  # UHD's own keyword
        self.rx_freqs[chan] = float(getattr(tune_request, "target_freq", tune_request))
        return self.rx_freqs[chan]

    def set_rx_lo_freq(self, freq, name, chan=0):
        self.rx_freqs[chan] = float(freq)
        return float(freq)

    def get_tx_rate(self, chan=0):
        return self.tx_rates[chan]

    def set_tx_rate(self, rate, chan=0):
        if chan >= (1 << 32):
            for ch in self.tx_rates:
                self.tx_rates[ch] = float(rate)
        else:
            self.tx_rates[chan] = float(rate)

    def get_tx_stream(self, stream_args):
        return self.tx_streamer

    def get_rx_stream(self, stream_args):
        return self.rx_streamer

    def send_waveform(self, waveform_proto, duration, freq, rate=1e6, channels=(0,), gain=10):
        self.waveforms_sent += 1
        return len(waveform_proto)

    def recv_num_samps(self, num_samps, freq, rate=1e6, channels=(0,), gain=10):
        self.rx_samps_calls += 1
        return np.zeros((len(channels), num_samps), dtype=np.complex64)


def _fake_uhd_module():
    types_ns = types.SimpleNamespace(
        TuneRequest=_FakeTuneRequest,
        TXMetadata=_FakeTXMetadata,
        RXMetadata=_FakeRXMetadata,
    )
    return types.SimpleNamespace(
        types=types_ns,
        usrp=types.SimpleNamespace(StreamArgs=_FakeStreamArgs),
        libpyuhd=types.SimpleNamespace(types=types.SimpleNamespace(tune_request=_FakeTuneRequest)),
    )


def test_usrp_args_and_inventory_strip_freq_limit_keys():
    init = {
        "profile": "usrp2901",
        "type": "b200",
        "serial": "31A2B3C",
        "tx_allow": [[902e6, 928e6]],
        "tx_deny": [[910e6, 912e6]],
        "rx_allow": [[70e6, 6e9]],
        "rx_deny": [],
        "admin_group_override": True,
    }
    args = _usrp_args(**init)
    assert "tx_allow" not in args
    assert "tx_deny" not in args
    assert "rx_allow" not in args
    assert "admin_group_override" not in args
    assert "serial=31A2B3C" in args

    schema = UsrpSchema()
    schema.bind_inventory(init)
    assert schema._inventory == {"profile": "usrp2901", "type": "b200", "serial": "31A2B3C"}
    assert schema._freq_limits is not None and schema._freq_limits.tx.allow == ((902e6, 928e6),)


def test_usrp_v1_enforcement_and_bypass():
    dev = _FakeUSRP()
    schema = UsrpSchema()
    schema.bind(dev)
    schema.configure_lockouts(
        parse_freq_limits(
            {
                "tx_allow": [[902e6, 928e6], [2.40e9, 2.48e9]],
                "tx_deny": [[2.45e9, 2.46e9]],
                "rx_allow": [[902e6, 928e6]],
                "rx_deny": [[920e6, 922e6]],
                "admin_group_override": True,
            }
        )
    )
    fake_uhd = _fake_uhd_module()
    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # Valid TX tune and stream.
        schema.call_set_tx_freq(915e6, 0)
        stream = schema.call_get_tx_stream({"cpu_format": "fc32", "otw_format": "sc16", "channels": [0]})
        schema.call_stream_send({"handle": stream["handle"], "samples": np.zeros(8, dtype=np.complex64)})
        assert dev.tx_streamer.sent == 1

        # Occupied band touching deny edge (2448 ± 2 = 2446..2450) refused on set_tx_freq.
        with pytest.raises(RemoteRFPolicyError, match=r"2446–2450 MHz.*2450–2460 MHz"):
            schema.call_set_tx_freq(2.448e9, 0)
        assert dev.tx_freqs[0] == 915e6

        # Manual TuneRequest attempting to spoof target_freq while setting RF LO out of band -> refused.
        with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
            schema.call_set_tx_freq(
                {
                    "__uhd_type__": "TuneRequest",
                    "target_freq": 915e6,
                    "rf_freq": 500e6,
                    "rf_freq_policy": "MANUAL",
                    "dsp_freq_policy": "NONE",
                },
                0,
            )

        # Widening TX rate while TX streamer is open -> refused.
        with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
            schema.call_set_tx_rate(30e6, 0)
        assert dev.tx_rates[0] == 4e6

        # If hardware rate was widened while closed, get_tx_stream and stream_send refuse.
        dev.tx_rates[0] = 30e6
        with pytest.raises(RemoteRFPolicyError, match=r"900–930 MHz"):
            schema.call_stream_send({"handle": stream["handle"], "samples": np.zeros(8, dtype=np.complex64)})
        with pytest.raises(RemoteRFPolicyError, match=r"900–930 MHz"):
            schema.call_get_tx_stream({"cpu_format": "fc32", "otw_format": "sc16", "channels": [0]})

        # RX checks on set_rx_freq and stream_recv.
        schema.call_set_rx_freq(915e6, 0)
        with pytest.raises(RemoteRFPolicyError, match=r"921 MHz.*920–922 MHz"):
            schema.call_set_rx_freq(921e6, 0)
        with pytest.raises(RemoteRFPolicyError, match=r"800 MHz.*902–928 MHz"):
            schema.call_set_rx_freq(800e6, 0)
        rx_stream = schema.call_get_rx_stream({"cpu_format": "fc32", "otw_format": "sc16", "channels": [0]})
        dev.rx_freqs[0] = 921e6
        with pytest.raises(RemoteRFPolicyError, match=r"921 MHz"):
            schema.call_stream_recv({"handle": rx_stream["handle"], "recv_buffer": np.zeros(4, dtype=np.complex64)})

        # Admin bypass works.
        schema.lockout_bypass = True
        schema.call_set_tx_freq(2.455e9, 0)
        assert dev.tx_freqs[0] == 2.455e9


def test_usrp_v2_invoke_native_and_session_cloning():
    fake_uhd = _fake_uhd_module()
    init = {
        "serial": "31A2B3C",
        "tx_allow": [[902e6, 928e6], [2.40e9, 2.48e9]],
        "tx_deny": [[2.45e9, 2.46e9]],
        "rx_allow": [[902e6, 928e6]],
        "rx_deny": [],
        "admin_group_override": True,
    }
    for cls in (UsrpSchema, UsrpPhaserSchema):
        dev = _FakeUSRP()
        inv = cls()
        inv.bind_inventory(init)
        with (
            patch("remoterf_device_core.drivers.usrp.usrp_schema._connect_usrp", return_value=dev),
            patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd),
        ):
            session_schema = inv.create_session_schema()
            assert session_schema._freq_limits == inv._freq_limits

            # set_tx_freq via dict TuneRequest.
            session_schema.invoke_native(
                "set_tx_freq",
                {"tune_request": {"__uhd_type__": "TuneRequest", "target_freq": 2.42e9}, "chan": 0},
            )
            assert dev.tx_freqs[0] == 2.42e9

            # Straddling two allow ranges or touching deny refused.
            with pytest.raises(RemoteRFPolicyError, match=r"2450–2460 MHz"):
                session_schema.invoke_native(
                    "set_tx_freq",
                    {"tune_request": {"__uhd_type__": "TuneRequest", "target_freq": 2.448e9}, "chan": 0},
                )
            with pytest.raises(RemoteRFPolicyError, match=r"2450–2460 MHz"):
                session_schema.invoke_native("set_tx_lo_freq", {"freq": 2.455e9, "name": "all", "chan": 0})
            with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
                session_schema.invoke_native(
                    "send_waveform",
                    {"waveform_proto": np.zeros(4, dtype=np.complex64), "duration": 0.1, "freq": 2.455e9, "rate": 1e6},
                )
            assert dev.waveforms_sent == 0

            # RX invoke checks.
            with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
                session_schema.invoke_native("set_rx_freq", {"tune_request": 2.42e9, "chan": 0})
            with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
                session_schema.invoke_native("set_rx_lo_freq", {"freq": 2.42e9, "name": "all", "chan": 0})
            with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
                session_schema.invoke_native("recv_num_samps", {"num_samps": 8, "freq": 2.42e9})
            assert dev.rx_samps_calls == 0

            # get_tx_stream checks live band (both keyword and positional_args paths).
            dev.tx_freqs[0] = 915e6
            dev.tx_rates[0] = 30e6  # 900..930 spills past 902..928
            stream_payload = {"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}
            with pytest.raises(RemoteRFPolicyError, match=r"900–930 MHz"):
                session_schema.invoke_native("get_tx_stream", {"stream_args": stream_payload})
            with pytest.raises(RemoteRFPolicyError, match=r"900–930 MHz"):
                session_schema.invoke_native("get_tx_stream", {}, positional_args=[stream_payload])


def test_usrp_tune_precheck_all_policies_and_legacy_args():
    class _LoAwareUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_lo_freqs = {0: 915e6}
            self.rx_lo_freqs = {0: 915e6}

        def get_tx_lo_freq(self, name="all", chan=0):
            return self.tx_lo_freqs[chan]

        def get_rx_lo_freq(self, name="all", chan=0):
            return self.rx_lo_freqs[chan]

    fake_uhd = _fake_uhd_module()
    limits = parse_freq_limits(
        {
            "tx_allow": [[902e6, 928e6]],
            "tx_deny": [[917e6, 918e6]],
            "rx_allow": [[902e6, 928e6]],
            "rx_deny": [[920e6, 922e6]],
        }
    )

    # Without LO readback: policies depending on unknown RF LO or DSP offset fail closed.
    plain_dev = _FakeUSRP()
    plain_dev.tx_rates[0] = 2e6
    plain_schema = UsrpSchema()
    plain_schema.bind(plain_dev)
    plain_schema.configure_lockouts(limits)

    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # Dict carrying both target_freq and legacy args uses args[0] (matching _construct).
        with pytest.raises(RemoteRFPolicyError):
            plain_schema.call_set_tx_freq(
                {"__uhd_type__": "TuneRequest", "target_freq": 915e6, "args": [2.45e9]},
                0,
            )
        assert plain_dev.tx_freqs[0] == 915e6

        # Old-client keep_rf envelope with huge lo_offset is refused.
        with pytest.raises(RemoteRFPolicyError):
            plain_schema.call_set_tx_freq(
                {
                    "__uhd_type__": "TuneRequest",
                    "target_freq": 915e6,
                    "lo_offset": 1.5e9,
                    "rf_freq": 0.0,
                    "rf_freq_policy": "AUTO",
                    "dsp_freq_policy": "AUTO",
                },
                0,
            )

        # (AUTO, MANUAL): RF at target + manual DSP offset.
        with pytest.raises(RemoteRFPolicyError):
            plain_schema.call_set_tx_freq(
                {
                    "__uhd_type__": "TuneRequest",
                    "target_freq": 915e6,
                    "rf_freq_policy": "AUTO",
                    "dsp_freq_policy": "MANUAL",
                    "dsp_freq": 30e6,
                },
                0,
            )
        # (AUTO, MANUAL) clamping spoof (RF at 2.45 GHz, huge negative DSP to fake 915 MHz).
        with pytest.raises(RemoteRFPolicyError):
            plain_schema.call_set_tx_freq(
                {
                    "__uhd_type__": "TuneRequest",
                    "target_freq": 2.45e9,
                    "rf_freq_policy": "AUTO",
                    "dsp_freq_policy": "MANUAL",
                    "dsp_freq": -1.535e9,
                },
                0,
            )
        # Constructed TuneRequest object on v2 path with (AUTO, MANUAL) +30 MHz refused.
        tr_auto_manual = _FakeTuneRequest(915e6)
        tr_auto_manual.dsp_freq_policy = "MANUAL"
        tr_auto_manual.dsp_freq = 30e6
        with pytest.raises(RemoteRFPolicyError):
            plain_schema.invoke_native("set_tx_freq", {"tune_request": tr_auto_manual, "chan": 0})
        # Valid small (AUTO, MANUAL) in-band shift passes.
        plain_schema.call_set_tx_freq(
            {
                "__uhd_type__": "TuneRequest",
                "target_freq": 912e6,
                "rf_freq_policy": "AUTO",
                "dsp_freq_policy": "MANUAL",
                "dsp_freq": 2e6,
            },
            0,
        )

        # (MANUAL, AUTO): RF at 2.45 GHz with target 915 MHz refused (DSP clamps to ±MCR/2).
        with pytest.raises(RemoteRFPolicyError):
            plain_schema.call_set_tx_freq(
                {
                    "__uhd_type__": "TuneRequest",
                    "target_freq": 915e6,
                    "rf_freq": 2.45e9,
                    "rf_freq_policy": "MANUAL",
                    "dsp_freq_policy": "AUTO",
                },
                0,
            )
        # (MANUAL, AUTO) whose RF..target hull crosses tx_deny [917, 918] refused.
        with pytest.raises(RemoteRFPolicyError):
            plain_schema.call_set_tx_freq(
                {"__uhd_type__": "TuneRequest", "target_freq": 915e6, "lo_offset": 5e6},
                0,
            )
        # Valid (MANUAL, AUTO) LO offset completely inside allow and outside deny passes.
        plain_schema.call_set_tx_freq(
            {"__uhd_type__": "TuneRequest", "target_freq": 910e6, "lo_offset": 2e6},
            0,
        )

        # (MANUAL, MANUAL) clamping spoof refused; valid in-band passes.
        with pytest.raises(RemoteRFPolicyError):
            plain_schema.call_set_tx_freq(
                {
                    "__uhd_type__": "TuneRequest",
                    "rf_freq": 2.45e9,
                    "rf_freq_policy": "MANUAL",
                    "dsp_freq": -1.535e9,
                    "dsp_freq_policy": "MANUAL",
                },
                0,
            )
        plain_schema.call_set_tx_freq(
            {
                "__uhd_type__": "TuneRequest",
                "rf_freq": 910e6,
                "rf_freq_policy": "MANUAL",
                "dsp_freq": 2e6,
                "dsp_freq_policy": "MANUAL",
            },
            0,
        )

        # Policies requiring unknown RF LO / current DSP fail closed when LO readback is absent.
        for req in (
            {"__uhd_type__": "TuneRequest", "target_freq": 910e6, "rf_freq_policy": "AUTO", "dsp_freq_policy": "NONE"},
            {"__uhd_type__": "TuneRequest", "rf_freq": 910e6, "rf_freq_policy": "MANUAL", "dsp_freq_policy": "NONE"},
            {"__uhd_type__": "TuneRequest", "target_freq": 910e6, "rf_freq_policy": "NONE", "dsp_freq_policy": "AUTO"},
            {"__uhd_type__": "TuneRequest", "rf_freq_policy": "NONE", "dsp_freq_policy": "MANUAL", "dsp_freq": 1e6},
            {"__uhd_type__": "TuneRequest", "target_freq": 910e6, "rf_freq_policy": "BOGUS"},
        ):
            with pytest.raises(RemoteRFPolicyError):
                plain_schema.call_set_tx_freq(req, 0)

        # (NONE, NONE) checks live current_freq.
        plain_dev.tx_freqs[0] = 910e6
        plain_schema.call_set_tx_freq(
            {"__uhd_type__": "TuneRequest", "target_freq": 2.45e9, "rf_freq_policy": "NONE", "dsp_freq_policy": "NONE"},
            0,
        )
        plain_dev.tx_freqs[0] = 2.45e9
        with pytest.raises(RemoteRFPolicyError):
            plain_schema.call_set_tx_freq(
                {"__uhd_type__": "TuneRequest", "target_freq": 910e6, "rf_freq_policy": "NONE", "dsp_freq_policy": "NONE"},
                0,
            )

    # With LO readback: NONE policies use real RF LO / current DSP, never stale get_tx_freq + dsp.
    lo_dev = _LoAwareUSRP()
    lo_dev.tx_rates[0] = 2e6
    lo_schema = UsrpSchema()
    lo_schema.bind(lo_dev)
    lo_schema.configure_lockouts(limits)

    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # Old DSP = -10 MHz (rf_lo=915 MHz, get_tx_freq=905 MHz); rf=NONE, dsp=MANUAL(+20 MHz)
        # tunes UHD to 915 + 20 = 935 MHz (NOT 905 + 20 = 925 MHz) -> refused.
        lo_dev.tx_lo_freqs[0] = 915e6
        lo_dev.tx_freqs[0] = 905e6
        with pytest.raises(RemoteRFPolicyError):
            lo_schema.call_set_tx_freq(
                {"__uhd_type__": "TuneRequest", "rf_freq_policy": "NONE", "dsp_freq_policy": "MANUAL", "dsp_freq": 20e6},
                0,
            )
        # With RF LO at 910 MHz and dsp=+2 MHz -> 910..912 MHz -> allowed.
        lo_dev.tx_lo_freqs[0] = 910e6
        lo_schema.call_set_tx_freq(
            {"__uhd_type__": "TuneRequest", "rf_freq_policy": "NONE", "dsp_freq_policy": "MANUAL", "dsp_freq": 2e6},
            0,
        )

        # (AUTO, NONE) and (MANUAL, NONE) preserve current DSP = current_freq - current_rf.
        lo_dev.tx_lo_freqs[0] = 900e6
        lo_dev.tx_freqs[0] = 915e6  # current DSP = +15 MHz
        with pytest.raises(RemoteRFPolicyError):
            lo_schema.call_set_tx_freq(
                {"__uhd_type__": "TuneRequest", "target_freq": 915e6, "rf_freq_policy": "AUTO", "dsp_freq_policy": "NONE"},
                0,
            )
        with pytest.raises(RemoteRFPolicyError):
            lo_schema.call_set_tx_freq(
                {"__uhd_type__": "TuneRequest", "rf_freq": 915e6, "rf_freq_policy": "MANUAL", "dsp_freq_policy": "NONE"},
                0,
            )
        lo_dev.tx_lo_freqs[0] = 910e6
        lo_dev.tx_freqs[0] = 910e6  # current DSP = 0
        lo_schema.call_set_tx_freq(
            {"__uhd_type__": "TuneRequest", "rf_freq": 912e6, "rf_freq_policy": "MANUAL", "dsp_freq_policy": "NONE"},
            0,
        )

        # (NONE, AUTO) checks current RF LO -> target hull.
        lo_dev.tx_lo_freqs[0] = 2.45e9
        with pytest.raises(RemoteRFPolicyError):
            lo_schema.call_set_tx_freq(
                {"__uhd_type__": "TuneRequest", "target_freq": 910e6, "rf_freq_policy": "NONE", "dsp_freq_policy": "AUTO"},
                0,
            )
        lo_dev.tx_lo_freqs[0] = 910e6
        lo_schema.call_set_tx_freq(
            {"__uhd_type__": "TuneRequest", "target_freq": 912e6, "rf_freq_policy": "NONE", "dsp_freq_policy": "AUTO"},
            0,
        )

        # RX sign: (AUTO, MANUAL) with dsp=-30 MHz tunes RX to 915 - (-30) = 945 MHz -> refused.
        with pytest.raises(RemoteRFPolicyError):
            lo_schema.call_set_rx_freq(
                {"__uhd_type__": "TuneRequest", "target_freq": 915e6, "rf_freq_policy": "AUTO", "dsp_freq_policy": "MANUAL", "dsp_freq": -30e6},
                0,
            )


# ── schemas.json unchanged check ──────────────────────────────────────


def test_schemas_json_hashes_unchanged_for_sdr_drivers():
    rows = {row["device_type"]: row for row in manifest.load()}
    for cls in (PlutoSchema, PlutoMIMOSchema, HackRFSchema, UsrpSchema, UsrpPhaserSchema):
        schema = cls()
        row = rows[cls.device_type]
        assert schema.driver_version == row["driver_version"]
        assert schema.get_idl_hash() == row["v1"]
        if row["v2"] is not None:
            assert schema.get_idl_v2()["schema_hash"] == row["v2"]
