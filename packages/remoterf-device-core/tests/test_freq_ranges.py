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
from remoterf_device_core.drivers.usrp.usrp_schema import (
    UsrpSchema,
    _extract_tune_bounds,
    _usrp_args,
)
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
        self.filter_bandwidth = 2_500_000
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
    schema.set_center_freq(2_444_000_000)
    schema.call_start_tx()
    assert dev.tx_starts == 1
    with pytest.raises(RemoteRFPolicyError, match=r"2450–2460 MHz"):
        schema.set_center_freq(2_448_000_000)
    assert dev.center_freq == 2_444_000_000  # no silent retune
    with pytest.raises(RemoteRFPolicyError, match=r"2450–2460 MHz"):
        schema.set_sample_rate(16_000_000)  # 2444 ± 8 -> 2436..2452
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


class _FakeDeviceAddr:
    def __init__(self, raw=""):
        self._text = str(raw or "")

    def to_string(self):
        return self._text

    def keys(self):
        return [
            part.split("=", 1)[0].strip()
            for part in self._text.split(",")
            if part.strip()
        ]


class _FakeTuneRequest:
    def __init__(self, target_freq=0.0, lo_offset=0.0):
        self.target_freq = float(target_freq)
        self.rf_freq = float(target_freq) + float(lo_offset)
        self.dsp_freq = 0.0
        self.rf_freq_policy = "MANUAL" if lo_offset else "AUTO"
        self.dsp_freq_policy = "AUTO"
        self.args = _FakeDeviceAddr("")


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

    def send_waveform(
        self,
        waveform_proto,
        duration,
        freq,
        rate=1e6,
        channels=(0,),
        gain=10,
        start_time=None,
        streamer=None,
    ):
        self.waveforms_sent += 1
        return len(waveform_proto)

    def recv_num_samps(
        self,
        num_samps,
        freq,
        rate=1e6,
        channels=(0,),
        gain=10,
        start_time=None,
        streamer=None,
    ):
        self.rx_samps_calls += 1
        return np.zeros((max(len(channels), 1), num_samps), dtype=np.complex64)


class _FakeTimeSpec:
    def __init__(self, secs=0.0):
        self.secs = float(secs)

    def get_real_secs(self):
        return self.secs


class _FakeStreamMode:
    start_cont = "start_cont"
    stop_cont = "stop_cont"
    num_done = "num_done"
    num_more = "num_more"


class _FakeStreamCMD:
    def __init__(self, mode):
        self.mode = mode
        self.stream_now = True
        self.time_spec = None
        self.num_samps = 0


class _FakeSubdevSpec:
    def __init__(self, spec=""):
        self.spec = str(spec)

    def __str__(self):
        return self.spec


class _FakeFilterType:
    DIGITAL_FIR_I16 = "DIGITAL_FIR_I16"


class _FakeDigitalFilterFIRI16:
    def __init__(
        self,
        filter_type,
        bypassed,
        position,
        input_rate,
        interpolation,
        decimation,
        tap_full_scale,
        max_num_taps,
        taps,
    ):
        self.filter_type = filter_type
        self.bypassed = bypassed
        self.position = position
        self.input_rate = input_rate
        self.interpolation = interpolation
        self.decimation = decimation
        self.tap_full_scale = tap_full_scale
        self.max_num_taps = max_num_taps
        self.taps = list(taps)


def _fake_uhd_module():
    types_ns = types.SimpleNamespace(
        TuneRequest=_FakeTuneRequest,
        DeviceAddr=_FakeDeviceAddr,
        TXMetadata=_FakeTXMetadata,
        RXMetadata=_FakeRXMetadata,
        TimeSpec=_FakeTimeSpec,
        StreamMode=_FakeStreamMode,
        StreamCMD=_FakeStreamCMD,
    )
    filters_ns = types.SimpleNamespace(
        FilterType=_FakeFilterType,
        DigitalFilterFIRI16=_FakeDigitalFilterFIRI16,
    )
    return types.SimpleNamespace(
        types=types_ns,
        filters=filters_ns,
        usrp=types.SimpleNamespace(
            StreamArgs=_FakeStreamArgs,
            SubdevSpec=_FakeSubdevSpec,
        ),
        libpyuhd=types.SimpleNamespace(
            types=types.SimpleNamespace(
                tune_request=_FakeTuneRequest,
                device_addr=_FakeDeviceAddr,
            )
        ),
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
    class _LoReportingUSRP(_FakeUSRP):
        def get_tx_lo_freq(self, name="all", chan=0):
            return self.tx_freqs[int(chan)]

        def get_rx_lo_freq(self, name="all", chan=0):
            return self.rx_freqs[int(chan)]

    dev = _LoReportingUSRP()
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

        # Manual TuneRequest attempting to spoof target_freq while setting RF LO out of band -> refused for 500 MHz band.
        with pytest.raises(RemoteRFPolicyError, match=r"498–502 MHz.*902–928 MHz"):
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
        # Same (MANUAL, NONE) request with in-band rf_freq=915 MHz succeeds (proving refusal above was on 500 MHz, not unreadable LO).
        schema.call_set_tx_freq(
            {
                "__uhd_type__": "TuneRequest",
                "target_freq": 915e6,
                "rf_freq": 915e6,
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

            # Defence-in-depth on UsrpPhaserSchema: phaser LO/TX/init calls are refused while limits are active.
            if isinstance(session_schema, UsrpPhaserSchema):
                for method, call_args in (
                    ("phaser_init", {"uri": "ip:10.0.0.1"}),
                    ("phaser_set_lo_freq", {"signal_freq": 10.25e9, "if_freq": 2.1e9}),
                    ("phaser_set_tx_switch", {"value": 1}),
                    ("phaser_set_tx_path", {"enabled": True}),
                ):
                    with pytest.raises(RemoteRFPolicyError, match=r"usrp_phaser"):
                        session_schema.invoke_native(method, call_args)


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


def test_usrp_wrapped_tune_request_bypass_refused():
    """ranges-2: wrapped TuneRequest payloads must be validated after _construct."""
    fake_uhd = _fake_uhd_module()
    dev = _FakeUSRP()
    dev.tx_rates[0] = 1e6
    schema = UsrpSchema()
    schema.bind(dev)
    schema.configure_lockouts(
        parse_freq_limits(
            {
                "tx_allow": [[902e6, 928e6]],
                "rx_allow": [[902e6, 928e6]],
            }
        )
    )

    wrapped_oob = {
        "__remoterf_uhd_helper__": "stream",
        "stream_args": {"__uhd_type__": "TuneRequest", "target_freq": 2.45e9},
        "target_freq": 915e6,
    }
    nested_oob = {
        "__remoterf_uhd_helper__": "stream",
        "stream_args": wrapped_oob,
        "target_freq": 915e6,
    }
    wrapped_float_oob = {
        "__remoterf_uhd_helper__": "stream",
        "stream_args": 2.45e9,
        "target_freq": 915e6,
    }
    wrapped_valid = {
        "__remoterf_uhd_helper__": "stream",
        "stream_args": {"__uhd_type__": "TuneRequest", "target_freq": 916e6},
        "target_freq": 2.45e9,
    }

    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        for payload in (wrapped_oob, nested_oob, wrapped_float_oob):
            with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
                schema.call_set_tx_freq(payload, 0)
            assert dev.tx_freqs[0] == 915e6

            with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
                schema.call_set_rx_freq(payload, 0)
            assert dev.rx_freqs[0] == 915e6

            with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
                schema.invoke_native("set_tx_freq", {"tune_request": payload, "chan": 0})
            assert dev.tx_freqs[0] == 915e6

            with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
                schema.invoke_native("set_rx_freq", {"tune_request": payload, "chan": 0})
            assert dev.rx_freqs[0] == 915e6

        # Valid inner TuneRequest succeeds even with an out-of-range outer decoy.
        schema.call_set_tx_freq(wrapped_valid, 0)
        assert dev.tx_freqs[0] == 916e6
        schema.call_set_rx_freq(wrapped_valid, 0)
        assert dev.rx_freqs[0] == 916e6


def test_usrp_timed_commands_refused_while_limits_active():
    """ranges-3: set_command_time / clear_command_time refused while limits are active."""

    class _TimedUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.cmd_times = []
            self.clears = 0

        def set_command_time(self, time_spec, mboard=0):
            self.cmd_times.append((time_spec, mboard))

        def clear_command_time(self, mboard=0):
            self.clears += 1

    fake_uhd = _fake_uhd_module()
    dev = _TimedUSRP()
    schema = UsrpSchema()
    schema.bind(dev)

    # Without limits, timed commands work.
    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        schema.invoke_native("set_command_time", {"time_spec": {"__uhd_type__": "TimeSpec", "secs": 5.0}})
        schema.invoke_native("clear_command_time", {})
    assert len(dev.cmd_times) == 1
    assert dev.clears == 1

    # With TX or RX limits active, both are refused.
    for limit_cfg in (
        {"tx_allow": [[902e6, 928e6]], "admin_group_override": True},
        {"rx_allow": [[902e6, 928e6]], "admin_group_override": True},
    ):
        schema.configure_lockouts(parse_freq_limits(limit_cfg))
        schema.lockout_bypass = False
        with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
            with pytest.raises(RemoteRFPolicyError, match=r"[Tt]imed command"):
                schema.invoke_native("set_command_time", {"time_spec": {"__uhd_type__": "TimeSpec", "secs": 10.0}})
            with pytest.raises(RemoteRFPolicyError, match=r"[Tt]imed command"):
                schema.invoke_native("clear_command_time", {})
        assert len(dev.cmd_times) == 1
        assert dev.clears == 1

    # Admin bypass allows timed commands when enabled.
    schema.lockout_bypass = True
    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        schema.invoke_native("set_command_time", {"time_spec": {"__uhd_type__": "TimeSpec", "secs": 10.0}})
        schema.invoke_native("clear_command_time", {})
    assert len(dev.cmd_times) == 2
    assert dev.clears == 2


def test_usrp_shared_tx_lo_and_multi_channel_rate_change():
    """ranges-4: B210/2901 shared TX LO checks all TX channels and all active channels on rate change."""

    class _B210USRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_lo = 910e6
            self.tx_dsp = {0: 0.0, 1: 15e6}
            self.tx_rates = {0: 1e6, 1: 1e6}
            self.tx_freqs = {0: 910e6, 1: 925e6}

        def get_tx_num_channels(self):
            return 2

        def get_tx_lo_freq(self, name="all", chan=0):
            return self.tx_lo

        def get_tx_freq(self, chan=0):
            return self.tx_lo + self.tx_dsp[int(chan)]

        def set_tx_freq(self, tune_request, chan=0):
            target = float(getattr(tune_request, "target_freq", tune_request))
            self.tx_lo = target
            self.tx_dsp[int(chan)] = 0.0
            for ch in (0, 1):
                self.tx_freqs[ch] = self.tx_lo + self.tx_dsp[ch]
            return self.tx_freqs[int(chan)]

        def set_tx_lo_freq(self, freq, name, chan=0):
            self.tx_lo = float(freq)
            for ch in (0, 1):
                self.tx_freqs[ch] = self.tx_lo + self.tx_dsp[ch]
            return self.tx_lo

    fake_uhd = _fake_uhd_module()
    dev = _B210USRP()
    schema = UsrpSchema()
    schema.bind(dev)
    schema.configure_lockouts(parse_freq_limits({"tx_allow": [[902e6, 928e6]]}))

    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # Scenario 1: chan 1 has DSP +15 MHz (LO 910 -> 925 MHz); tuning chan 0 to 925 MHz
        # would move shared LO to 925 MHz and push chan 1 to 940 MHz -> refused.
        with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
            schema.call_set_tx_freq(925e6, 0)
        assert dev.tx_lo == 910e6

        with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
            schema.invoke_native("set_tx_freq", {"tune_request": 925e6, "chan": 0})
        assert dev.tx_lo == 910e6

        with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
            schema.invoke_native("set_tx_lo_freq", {"freq": 925e6, "name": "all", "chan": 0})
        assert dev.tx_lo == 910e6

        # Tuning chan 0 to 912 MHz keeps chan 1 at 927 ± 0.5 MHz -> allowed.
        schema.call_set_tx_freq(912e6, 0)
        assert dev.tx_lo == 912e6

        # Scenario 2: chan 1 has DSP 0 and tx_rate=20 MS/s while chan 0 has tx_rate=1 MS/s;
        # tuning chan 0 to 925 MHz would make chan 1 occupy 915..935 MHz -> refused.
        dev.tx_lo = 915e6
        dev.tx_dsp = {0: 0.0, 1: 0.0}
        dev.tx_rates = {0: 1e6, 1: 20e6}
        with pytest.raises(RemoteRFPolicyError, match=r"915–935 MHz"):
            schema.call_set_tx_freq(925e6, 0)
        assert dev.tx_lo == 915e6

        # Scenario 3: active TX streamer on chan 1 at 927 MHz / 1 MS/s;
        # set_tx_rate(20e6, chan=0) must check active chan 1 too -> refused.
        dev.tx_lo = 912e6
        dev.tx_dsp = {0: 0.0, 1: 15e6}
        dev.tx_rates = {0: 1e6, 1: 1e6}
        stream = schema.call_get_tx_stream({"cpu_format": "fc32", "otw_format": "sc16", "channels": [1]})
        assert stream["handle"].startswith("tx:")
        with pytest.raises(RemoteRFPolicyError, match=r"917–937 MHz"):
            schema.call_set_tx_rate(20e6, 0)
        assert dev.tx_rates[0] == 1e6
        with pytest.raises(RemoteRFPolicyError, match=r"917–937 MHz"):
            schema.check_tx_rate_change(20e6, 0, [1])

        # Property-tree RF LO fallback works when get_tx_lo_freq raises (e.g. B210/2901 without /los/all).
        class _TreeFallbackB210(_B210USRP):
            def get_tx_lo_freq(self, name="all", chan=0):
                raise RuntimeError("lookup_error: /los/all not present")

            def get_tx_subdev_spec(self, mboard=0):
                return "A:A A:B"

            def get_tree(self):
                lo = self.tx_lo

                class _Prop:
                    def get(self):
                        return lo

                class _Tree:
                    def access_double(self, path):
                        assert path in {
                            "/mboards/0/dboards/A/tx_frontends/A/freq/value",
                            "/mboards/0/dboards/A/tx_frontends/B/freq/value",
                        }
                        return _Prop()

                return _Tree()

        tree_dev = _TreeFallbackB210()
        tree_schema = UsrpSchema()
        tree_schema.bind(tree_dev)
        tree_schema.configure_lockouts(parse_freq_limits({"tx_allow": [[902e6, 928e6]]}))
        with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
            tree_schema.call_set_tx_freq(925e6, 0)
        tree_schema.call_set_tx_freq(912e6, 0)
        assert tree_dev.tx_lo == 912e6

        # If a 2-channel device provides neither get_tx_lo_freq nor property-tree fallback, fail closed.
        class _NoLoTwoChanUSRP(_FakeUSRP):
            def get_tx_num_channels(self):
                return 2

        no_lo_2ch = _NoLoTwoChanUSRP()
        no_lo_2ch_schema = UsrpSchema()
        no_lo_2ch_schema.bind(no_lo_2ch)
        no_lo_2ch_schema.configure_lockouts(parse_freq_limits({"tx_allow": [[902e6, 928e6]]}))
        with pytest.raises(RemoteRFPolicyError, match=r"shared-LO"):
            no_lo_2ch_schema.call_set_tx_freq(915e6, 0)


def test_usrp_set_lo_freq_checks_dsp_offset_channel_and_name():
    """ranges-5: set_tx_lo_freq / set_rx_lo_freq route through tune checks and require name == 'all'."""

    class _DualLoUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_lo_freqs = {0: 910e6, 1: 910e6}
            self.rx_lo_freqs = {0: 915e6, 1: 925e6}
            self.tx_freqs = {0: 925e6, 1: 910e6}  # chan 0 has TX DSP +15 MHz
            self.rx_freqs = {0: 915e6, 1: 910e6}  # chan 1 has RX DSP +15 MHz (925 - 15)
            self.tx_rates = {0: 2e6, 1: 2e6}

        def get_tx_lo_freq(self, name="all", chan=0):
            return self.tx_lo_freqs[int(chan)]

        def get_rx_lo_freq(self, name="all", chan=0):
            return self.rx_lo_freqs[int(chan)]

        def set_tx_lo_freq(self, freq, name, chan=0):
            ch = int(chan)
            dsp = self.tx_freqs[ch] - self.tx_lo_freqs[ch]
            self.tx_lo_freqs[ch] = float(freq)
            self.tx_freqs[ch] = float(freq) + dsp
            return float(freq)

        def set_rx_lo_freq(self, freq, name, chan=0):
            ch = int(chan)
            shift = self.rx_freqs[ch] - self.rx_lo_freqs[ch]
            self.rx_lo_freqs[ch] = float(freq)
            self.rx_freqs[ch] = float(freq) + shift
            return float(freq)

    dev = _DualLoUSRP()
    schema = UsrpSchema()
    schema.bind(dev)
    schema.configure_lockouts(
        parse_freq_limits(
            {
                "tx_allow": [[902e6, 928e6]],
                "rx_allow": [[902e6, 928e6]],
            }
        )
    )

    # TX: RF LO 925 MHz + existing DSP +15 MHz = 940 MHz -> refused.
    with pytest.raises(RemoteRFPolicyError, match=r"939–941 MHz"):
        schema.invoke_native("set_tx_lo_freq", {"freq": 925e6, "name": "all", "chan": 0})
    assert dev.tx_lo_freqs[0] == 910e6

    # TX: RF LO 911 MHz + existing DSP +15 MHz = 926 MHz (±1 MHz = 925..927) -> allowed.
    schema.invoke_native("set_tx_lo_freq", {"freq": 911e6, "name": "all", "chan": 0})
    assert dev.tx_lo_freqs[0] == 911e6

    # RX: chan 1 has RX DSP +15 MHz (LO 925 -> 910); LO 910 MHz would tune chan 1 to 895 MHz -> refused.
    with pytest.raises(RemoteRFPolicyError, match=r"895 MHz"):
        schema.invoke_native("set_rx_lo_freq", {"freq": 910e6, "name": "all", "chan": 1})
    assert dev.rx_lo_freqs[1] == 925e6

    # RX: chan 1 LO 920 MHz -> 905 MHz -> allowed.
    schema.invoke_native("set_rx_lo_freq", {"freq": 920e6, "name": "all", "chan": 1})
    assert dev.rx_lo_freqs[1] == 920e6

    # Non-"all" LO names are refused on both TX and RX.
    with pytest.raises(RemoteRFPolicyError, match=r"all"):
        schema.invoke_native("set_tx_lo_freq", {"freq": 911e6, "name": "lo1", "chan": 0})
    with pytest.raises(RemoteRFPolicyError, match=r"all"):
        schema.invoke_native("set_rx_lo_freq", {"freq": 915e6, "name": "lo1", "chan": 0})

    # Without LO readback, both set_tx_lo_freq and set_rx_lo_freq fail closed even for in-range freq.
    no_lo_dev = _FakeUSRP()
    no_lo_schema = UsrpSchema()
    no_lo_schema.bind(no_lo_dev)
    no_lo_schema.configure_lockouts(
        parse_freq_limits({"tx_allow": [[902e6, 928e6]], "rx_allow": [[902e6, 928e6]]})
    )
    with pytest.raises(RemoteRFPolicyError):
        no_lo_schema.invoke_native("set_tx_lo_freq", {"freq": 915e6, "name": "all", "chan": 0})
    with pytest.raises(RemoteRFPolicyError):
        no_lo_schema.invoke_native("set_rx_lo_freq", {"freq": 915e6, "name": "all", "chan": 0})


def test_usrp_send_waveform_shared_lo_check_on_b210():
    """Item 1: send_waveform runs the shared-LO other-channel check on B210-like devices."""

    class _B210WaveformUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_lo = 910e6
            self.tx_dsp = {0: 0.0, 1: 15e6}
            self.tx_rates = {0: 1e6, 1: 1e6}
            self.tx_freqs = {0: 910e6, 1: 925e6}

        def get_mboard_name(self, mboard=0):
            return "B210"

        def get_tx_num_channels(self):
            return 2

        def get_tx_lo_freq(self, name="all", chan=0):
            return self.tx_lo

        def get_tx_freq(self, chan=0):
            return self.tx_lo + self.tx_dsp[int(chan)]

    dev = _B210WaveformUSRP()
    schema = UsrpSchema()
    schema.bind(dev)
    schema.configure_lockouts(parse_freq_limits({"tx_allow": [[902e6, 928e6]]}))
    proto = np.zeros(8, dtype=np.complex64)

    # send_waveform is refused while TX limits are active (coerced rate / LO cannot be read back).
    with pytest.raises(RemoteRFPolicyError, match=r"send_waveform"):
        schema.invoke_native(
            "send_waveform",
            {"waveform_proto": proto, "duration": 0.1, "freq": 925e6, "rate": 1e6, "channels": [0]},
        )
    assert dev.waveforms_sent == 0

    with pytest.raises(RemoteRFPolicyError, match=r"send_waveform"):
        schema.invoke_native("send_waveform", {}, positional_args=[proto, 0.1, 925e6, 1e6, [0]])
    assert dev.waveforms_sent == 0

    with pytest.raises(RemoteRFPolicyError, match=r"send_waveform"):
        schema.invoke_native(
            "send_waveform",
            {"waveform_proto": proto, "duration": 0.1, "freq": 912e6, "rate": 4e6, "channels": [0]},
        )
    assert dev.waveforms_sent == 0

    with pytest.raises(RemoteRFPolicyError, match=r"send_waveform"):
        schema.invoke_native(
            "send_waveform",
            {"waveform_proto": proto, "duration": 0.1, "freq": 912e6, "rate": 1e6, "channels": [0]},
        )
    assert dev.waveforms_sent == 0

    # Without TX limits, send_waveform works normally.
    schema.configure_lockouts(None)
    schema.invoke_native(
        "send_waveform",
        {"waveform_proto": proto, "duration": 0.1, "freq": 912e6, "rate": 1e6, "channels": [0]},
    )
    assert dev.waveforms_sent == 1


def test_usrp_lo_source_and_export_cannot_couple_independent_channels():
    """An X310 is independent-LO only while LOs stay internal and unexported."""

    class _X310ExportUSRP(_FakeUSRP):
        def __init__(self, export=False, source="internal"):
            super().__init__()
            self.tx_lo = {0: 910e6, 1: 910e6}
            self.tx_dsp = {0: 0.0, 1: 15e6}
            self.tx_rates = {0: 1e6, 1: 1e6}
            self.export, self.source = export, source
            self.lo_calls = []

        def get_mboard_name(self, mboard=0):
            return "X310"

        def get_tx_num_channels(self):
            return 2

        def get_tx_lo_names(self, chan=0):
            return ["lo1"]

        def get_tx_lo_source(self, name="lo1", chan=0):
            return self.source if int(chan) == 1 else "internal"

        def get_tx_lo_export_enabled(self, name="lo1", chan=0):
            return self.export and int(chan) == 0

        def get_tx_lo_freq(self, name="all", chan=0):
            return self.tx_lo[int(chan)]

        def get_tx_freq(self, chan=0):
            return self.tx_lo[int(chan)] + self.tx_dsp[int(chan)]

        def set_tx_freq(self, tune_request, chan=0):
            target = float(getattr(tune_request, "target_freq", tune_request))
            shared = self.export or self.source != "internal"
            for ch in ((0, 1) if shared else (int(chan),)):
                self.tx_lo[ch] = target
            self.tx_dsp[int(chan)] = 0.0
            return target

        def set_tx_lo_source(self, src, name="lo1", chan=0):
            self.lo_calls.append(("source", src, chan))

        def set_tx_lo_export_enabled(self, enabled, name="lo1", chan=0):
            self.lo_calls.append(("export", enabled, chan))

    fake_uhd = _fake_uhd_module()
    limits = parse_freq_limits({"tx_allow": [[902e6, 928e6]]})
    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # Students cannot re-source or export an LO while limits are active.
        dev = _X310ExportUSRP()
        schema = UsrpSchema()
        schema.bind(dev)
        schema.configure_lockouts(limits)
        with pytest.raises(RemoteRFPolicyError, match="LO source and export"):
            schema.invoke_native("set_tx_lo_export_enabled", {"enabled": True, "name": "lo1", "chan": 0})
        with pytest.raises(RemoteRFPolicyError, match="LO source and export"):
            schema.invoke_native("set_tx_lo_source", {"src": "companion", "name": "lo1", "chan": 1})
        assert dev.lo_calls == []

        # Default routing: channels are independent, so tuning chan 0 to 925 MHz is allowed.
        schema.call_set_tx_freq(925e6, 0)
        assert dev.tx_lo[0] == 925e6

        # LO sharing set up before limits applied (export or a companion source) makes the
        # channels coupled: chan 1 (LO 910 + DSP 15 MHz) would land at 940 MHz -> refused.
        for kwargs in ({"export": True}, {"source": "companion"}):
            dev = _X310ExportUSRP(**kwargs)
            schema = UsrpSchema()
            schema.bind(dev)
            schema.configure_lockouts(limits)
            with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
                schema.call_set_tx_freq(925e6, 0)
            assert dev.tx_lo == {0: 910e6, 1: 910e6}


def test_usrp_x310_independent_lo_and_rate_allows_valid_per_channel_ops():
    """Item 2: X310/N210-like dual-channel devices have independent LOs and per-channel rates."""

    class _X310USRP(_FakeUSRP):
        def __init__(self, mboard_name="X310"):
            super().__init__()
            self._mboard_name = mboard_name
            self.tx_freqs = {0: 915e6, 1: 926e6}
            self.rx_freqs = {0: 915e6, 1: 926e6}
            self.tx_rates = {0: 1e6, 1: 20e6}
            self.rx_rates = {0: 1e6, 1: 20e6}
            self.unreadable_ch1_lo = True

        def get_mboard_name(self, mboard=0):
            return self._mboard_name

        def get_tx_num_channels(self):
            return 2

        def get_rx_num_channels(self):
            return 2

        def get_tx_lo_freq(self, name="all", chan=0):
            if int(chan) == 1 and self.unreadable_ch1_lo:
                raise RuntimeError("chan 1 LO unreadable")
            return self.tx_freqs[int(chan)]

        def get_rx_lo_freq(self, name="all", chan=0):
            if int(chan) == 1 and self.unreadable_ch1_lo:
                raise RuntimeError("chan 1 LO unreadable")
            return self.rx_freqs[int(chan)]

    fake_uhd = _fake_uhd_module()
    for mboard in ("X310", "X300", "N210r4", "N200"):
        dev = _X310USRP(mboard_name=mboard)
        schema = UsrpSchema()
        schema.bind(dev)
        schema.configure_lockouts(
            parse_freq_limits({"tx_allow": [[902e6, 928e6]], "rx_allow": [[902e6, 928e6]]})
        )
        with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
            # Chan 1 is idle at 20 MS/s with unreadable LO, yet valid chan 0 tunes succeed (send_waveform refused under TX limits).
            schema.call_set_tx_freq(925e6, 0)
            assert dev.tx_freqs[0] == 925e6
            schema.invoke_native("set_tx_freq", {"tune_request": 915e6, "chan": 0})
            assert dev.tx_freqs[0] == 915e6
            with pytest.raises(RemoteRFPolicyError, match=r"send_waveform"):
                schema.invoke_native(
                    "send_waveform",
                    {
                        "waveform_proto": np.zeros(8, dtype=np.complex64),
                        "duration": 0.1,
                        "freq": 925e6,
                        "rate": 1e6,
                        "channels": [0],
                    },
                )
            assert dev.waveforms_sent == 0
            schema.call_set_rx_freq(925e6, 0)
            assert dev.rx_freqs[0] == 925e6

            # Starting a stream on chan 1 while its LO is unreadable succeeds when live freq/rate are in-band (N1).
            dev.tx_freqs = {0: 915e6, 1: 926e6}
            dev.tx_rates = {0: 1e6, 1: 1e6}
            schema.call_get_tx_stream({"cpu_format": "fc32", "otw_format": "sc16", "channels": [0, 1]})

            # Per-channel rate changes: chan 0 (915 MHz) and chan 1 (926 MHz / 1 MS/s) both streaming.
            dev.unreadable_ch1_lo = False
            schema.call_get_tx_stream({"cpu_format": "fc32", "otw_format": "sc16", "channels": [0, 1]})
            schema.call_set_tx_rate(5e6, 0)  # 912.5..917.5 MHz on chan 0 only -> allowed
            assert dev.tx_rates[0] == 5e6
            schema.check_tx_rate_change(5e6, 0, [0, 1])

            # Rate change on chan 1 itself (923.5..928.5 MHz) -> refused.
            with pytest.raises(RemoteRFPolicyError, match=r"923\.5–928\.5 MHz"):
                schema.call_set_tx_rate(5e6, 1)
            assert dev.tx_rates[1] == 1e6

            # ALL_CHANS rate change affects chan 1 too -> refused.
            with pytest.raises(RemoteRFPolicyError, match=r"923\.5–928\.5 MHz"):
                schema.call_set_tx_rate(5e6, (1 << 64) - 1)
            with pytest.raises(RemoteRFPolicyError, match=r"923\.5–928\.5 MHz"):
                schema.check_tx_rate_change(5e6, None, [0, 1])

            # Per-channel rate check still enforces chan 0's own band even when only chan 1 is in active_channels,
            # while allowing in-band chan 0 widening independent of chan 1 (Hunt N1).
            schema.check_tx_rate_change(10e6, 0, [1])
            with pytest.raises(RemoteRFPolicyError, match=r"895–935 MHz"):
                schema.check_tx_rate_change(40e6, 0, [1])


def test_usrp_mboard_coupling_ad9361_n310_and_unknown_fail_closed():
    """Item 2: motherboard coupling helper covers AD9361, N300/N310 pairs, daughterboards, and fail-closed unknown."""
    from remoterf_device_core.drivers.usrp.usrp_schema import _classify_mboard_coupling

    for name in ("B200", "B205mini", "B210", "NI USRP-2900", "NI USRP-2901", "E310", "E312", "E313", "E320"):
        assert _classify_mboard_coupling(name) == ("all", True), name
    for name in ("N300", "N310", "n310", "USRP N300"):
        assert _classify_mboard_coupling(name) == ("pair", False), name
    for name in ("X300", "X310", "N200", "N210", "N210r4", "USRP2", "NI-2944R", "X410", "N320"):
        assert _classify_mboard_coupling(name) == ("none", False), name
    for name in (None, "", "   ", "MysterySDR"):
        assert _classify_mboard_coupling(name) == ("all", True), name

    # 4-channel N310: 0/1 share an LO, 2/3 share an LO, rates are per-channel.
    class _N310USRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_lo = {0: 910e6, 1: 910e6, 2: 910e6, 3: 910e6}
            self.tx_dsp = {0: 0.0, 1: 15e6, 2: 0.0, 3: 0.0}
            self.tx_rates = {0: 1e6, 1: 1e6, 2: 1e6, 3: 1e6}
            self.rx_lo = {0: 910e6, 1: 910e6, 2: 910e6, 3: 910e6}
            self.rx_shift = {0: 0.0, 1: 15e6, 2: 0.0, 3: 0.0}

        def get_mboard_name(self, mboard=0):
            return "n310"

        def get_tx_num_channels(self):
            return 4

        def get_rx_num_channels(self):
            return 4

        def get_tx_lo_freq(self, name="all", chan=0):
            return self.tx_lo[int(chan)]

        def get_tx_freq(self, chan=0):
            ch = int(chan)
            return self.tx_lo[ch] + self.tx_dsp[ch]

        def set_tx_freq(self, tune_request, chan=0):
            ch = int(chan)
            target = float(getattr(tune_request, "target_freq", tune_request))
            self.tx_lo[ch] = target
            self.tx_lo[ch ^ 1] = target
            self.tx_dsp[ch] = 0.0
            return target

        def get_rx_lo_freq(self, name="all", chan=0):
            return self.rx_lo[int(chan)]

        def get_rx_freq(self, chan=0):
            ch = int(chan)
            return self.rx_lo[ch] + self.rx_shift[ch]

        def set_rx_freq(self, tune_request, chan=0):
            ch = int(chan)
            target = float(getattr(tune_request, "target_freq", tune_request))
            self.rx_lo[ch] = target
            self.rx_lo[ch ^ 1] = target
            self.rx_shift[ch] = 0.0
            return target

    fake_uhd = _fake_uhd_module()
    n310 = _N310USRP()
    n310_schema = UsrpSchema()
    n310_schema.bind(n310)
    n310_schema.configure_lockouts(
        parse_freq_limits({"tx_allow": [[902e6, 928e6]], "rx_allow": [[902e6, 928e6]]})
    )
    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # Chan 1 has +15 MHz DSP: tuning chan 0 to 925 MHz is refused, but tuning chan 2 to 925 MHz is allowed.
        with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
            n310_schema.call_set_tx_freq(925e6, 0)
        n310_schema.call_set_tx_freq(925e6, 2)
        assert n310.tx_lo[2] == 925e6

        # Same for RX pairs on N310: chan 1 has +15 MHz shift -> chan 0 refused, chan 2 allowed.
        with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
            n310_schema.call_set_rx_freq(925e6, 0)
        n310_schema.call_set_rx_freq(925e6, 2)
        assert n310.rx_lo[2] == 925e6

        # Pair 2/3 coupling: chan 3 +15 MHz DSP blocks chan 2, not chan 0 (once chan 1 DSP is 0).
        n310.tx_lo = {0: 910e6, 1: 910e6, 2: 910e6, 3: 910e6}
        n310.tx_dsp = {0: 0.0, 1: 0.0, 2: 0.0, 3: 15e6}
        with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
            n310_schema.call_set_tx_freq(925e6, 2)
        n310_schema.call_set_tx_freq(925e6, 0)

        # Rates on N310 are per-channel: widening chan 0 at 915 MHz does not fail on chan 1 at 926 MHz.
        n310.tx_lo = {0: 915e6, 1: 915e6, 2: 915e6, 3: 915e6}
        n310.tx_dsp = {0: 0.0, 1: 11e6, 2: 0.0, 3: 0.0}
        n310_schema.check_tx_rate_change(5e6, 0, [0, 1])
        with pytest.raises(RemoteRFPolicyError, match=r"923\.5–928\.5 MHz"):
            n310_schema.check_tx_rate_change(5e6, 1, [0, 1])

    # AD9361 (B210) also enforces shared-LO coupling on RX.
    class _B210RxUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.rx_lo = 910e6
            self.rx_shift = {0: 0.0, 1: 15e6}

        def get_mboard_name(self, mboard=0):
            return "NI USRP-2901"

        def get_rx_num_channels(self):
            return 2

        def get_rx_lo_freq(self, name="all", chan=0):
            return self.rx_lo

        def get_rx_freq(self, chan=0):
            return self.rx_lo + self.rx_shift[int(chan)]

        def set_rx_freq(self, tune_request, chan=0):
            target = float(getattr(tune_request, "target_freq", tune_request))
            self.rx_lo = target
            self.rx_shift[int(chan)] = 0.0
            return target

    b210_rx = _B210RxUSRP()
    b210_rx_schema = UsrpSchema()
    b210_rx_schema.bind(b210_rx)
    b210_rx_schema.configure_lockouts(parse_freq_limits({"rx_allow": [[902e6, 928e6]]}))
    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        with pytest.raises(RemoteRFPolicyError, match=r"940 MHz"):
            b210_rx_schema.call_set_rx_freq(925e6, 0)
        assert b210_rx.rx_lo == 910e6
        b210_rx_schema.call_set_rx_freq(912e6, 0)
        assert b210_rx.rx_lo == 912e6

    # Unreadable get_mboard_name() fails closed (treats LO and rate as shared).
    class _BrokenMboardUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_freqs = {0: 915e6, 1: 926e6}
            self.tx_rates = {0: 1e6, 1: 1e6}

        def get_mboard_name(self, mboard=0):
            raise RuntimeError("EEPROM unreadable")

        def get_tx_num_channels(self):
            return 2

    broken = _BrokenMboardUSRP()
    broken_schema = UsrpSchema()
    broken_schema.bind(broken)
    broken_schema.configure_lockouts(parse_freq_limits({"tx_allow": [[902e6, 928e6]]}))
    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        with pytest.raises(RemoteRFPolicyError, match=r"shared-LO"):
            broken_schema.call_set_tx_freq(915e6, 0)
        with pytest.raises(RemoteRFPolicyError, match=r"923\.5–928\.5 MHz"):
            broken_schema.check_tx_rate_change(5e6, 0, [0, 1])


def test_usrp_named_lo_stages_checked_or_refused():
    """Item 4: accept device-reported LO names when their stage frequency can be checked; refuse others."""

    class _UbxX310USRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_lo_stages = {0: {"all": 910e6, "lo1": 910e6, "lo2": 2.1e9}}
            self.rx_lo_stages = {0: {"all": 915e6, "lo1": 915e6, "lo2": 2.1e9}}
            self.tx_freqs = {0: 920e6}  # TX DSP +10 MHz
            self.rx_freqs = {0: 910e6}  # RX shift -5 MHz
            self.tx_rates = {0: 2e6}
            self.unreadable_stage = False

        def get_mboard_name(self, mboard=0):
            return "X310"

        def get_tx_num_channels(self):
            return 2

        def get_rx_num_channels(self):
            return 2

        def get_tx_lo_names(self, chan=0):
            return ["lo1", "lo2"]

        def get_rx_lo_names(self, chan=0):
            return ["lo1", "lo2"]

        def get_tx_lo_freq(self, name="all", chan=0):
            if self.unreadable_stage and name == "lo1":
                raise RuntimeError("lo1 unreadable")
            return self.tx_lo_stages[int(chan)][name]

        def get_rx_lo_freq(self, name="all", chan=0):
            if self.unreadable_stage and name == "lo1":
                raise RuntimeError("lo1 unreadable")
            return self.rx_lo_stages[int(chan)][name]

        def set_tx_lo_freq(self, freq, name, chan=0):
            ch = int(chan)
            dsp = self.tx_freqs[ch] - self.tx_lo_stages[ch]["all"]
            self.tx_lo_stages[ch][name] = float(freq)
            if name in {"all", "lo1"}:
                self.tx_lo_stages[ch]["all"] = float(freq)
                self.tx_lo_stages[ch]["lo1"] = float(freq)
                self.tx_freqs[ch] = float(freq) + dsp
            return float(freq)

        def set_rx_lo_freq(self, freq, name, chan=0):
            ch = int(chan)
            shift = self.rx_freqs[ch] - self.rx_lo_stages[ch]["all"]
            self.rx_lo_stages[ch][name] = float(freq)
            if name in {"all", "lo1"}:
                self.rx_lo_stages[ch]["all"] = float(freq)
                self.rx_lo_stages[ch]["lo1"] = float(freq)
                self.rx_freqs[ch] = float(freq) + shift
            return float(freq)

    dev = _UbxX310USRP()
    schema = UsrpSchema()
    schema.bind(dev)
    schema.configure_lockouts(
        parse_freq_limits({"tx_allow": [[902e6, 928e6]], "rx_allow": [[902e6, 928e6]]})
    )

    # Reported direct stage 'lo1' allowed when resulting RF + DSP ± rate/2 is in range.
    schema.invoke_native("set_tx_lo_freq", {"freq": 915e6, "name": "lo1", "chan": 0})
    assert dev.tx_lo_stages[0]["lo1"] == 915e6
    assert dev.tx_freqs[0] == 925e6

    # 'lo1' refused when RF + DSP (+10 MHz) exceeds tx_allow.
    with pytest.raises(RemoteRFPolicyError, match=r"929–931 MHz"):
        schema.invoke_native("set_tx_lo_freq", {"freq": 920e6, "name": "lo1", "chan": 0})
    assert dev.tx_lo_stages[0]["lo1"] == 915e6

    # Reported RX stage 'lo1' allowed when in range, refused when RX shift (-5 MHz) exits rx_allow.
    schema.invoke_native("set_rx_lo_freq", {"freq": 916e6, "name": "lo1", "chan": 0})
    assert dev.rx_lo_stages[0]["lo1"] == 916e6
    with pytest.raises(RemoteRFPolicyError, match=r"900 MHz"):
        schema.invoke_native("set_rx_lo_freq", {"freq": 905e6, "name": "lo1", "chan": 0})
    assert dev.rx_lo_stages[0]["lo1"] == 916e6

    # Intermediate mixing stage 'lo2' (2.1 GHz != RF freq) cannot be checked -> refused.
    with pytest.raises(RemoteRFPolicyError):
        schema.invoke_native("set_tx_lo_freq", {"freq": 915e6, "name": "lo2", "chan": 0})
    with pytest.raises(RemoteRFPolicyError):
        schema.invoke_native("set_rx_lo_freq", {"freq": 915e6, "name": "lo2", "chan": 0})

    # Unreported LO name refused.
    with pytest.raises(RemoteRFPolicyError, match=r"lo3"):
        schema.invoke_native("set_tx_lo_freq", {"freq": 915e6, "name": "lo3", "chan": 0})
    with pytest.raises(RemoteRFPolicyError, match=r"lo3"):
        schema.invoke_native("set_rx_lo_freq", {"freq": 915e6, "name": "lo3", "chan": 0})

    # Stage whose current frequency cannot be read fails closed.
    dev.unreadable_stage = True
    with pytest.raises(RemoteRFPolicyError):
        schema.invoke_native("set_tx_lo_freq", {"freq": 915e6, "name": "lo1", "chan": 0})
    with pytest.raises(RemoteRFPolicyError):
        schema.invoke_native("set_rx_lo_freq", {"freq": 915e6, "name": "lo1", "chan": 0})

    # On a shared-LO device reporting 'lo1', named LO still runs the shared-LO other-channel check.
    class _B210NamedLoUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_lo = 910e6
            self.tx_dsp = {0: 0.0, 1: 15e6}
            self.tx_rates = {0: 1e6, 1: 1e6}

        def get_mboard_name(self, mboard=0):
            return "B210"

        def get_tx_num_channels(self):
            return 2

        def get_tx_lo_names(self, chan=0):
            return ["lo1"]

        def get_tx_lo_freq(self, name="all", chan=0):
            return self.tx_lo

        def get_tx_freq(self, chan=0):
            return self.tx_lo + self.tx_dsp[int(chan)]

        def set_tx_lo_freq(self, freq, name, chan=0):
            self.tx_lo = float(freq)
            return self.tx_lo

    b210 = _B210NamedLoUSRP()
    b210_schema = UsrpSchema()
    b210_schema.bind(b210)
    b210_schema.configure_lockouts(parse_freq_limits({"tx_allow": [[902e6, 928e6]]}))
    with pytest.raises(RemoteRFPolicyError, match=r"902–928 MHz"):
        b210_schema.invoke_native("set_tx_lo_freq", {"freq": 925e6, "name": "lo1", "chan": 0})
    assert b210.tx_lo == 910e6
    b210_schema.invoke_native("set_tx_lo_freq", {"freq": 912e6, "name": "lo1", "chan": 0})
    assert b210.tx_lo == 912e6


def test_usrp_subdev_spec_remap_refused_while_limits_active():
    """Round 4 item 1: B210 5-step subdev remap sequence refused at the subdev step on v2 and v1."""

    class _B210SubdevUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_subdev = "A:A"
            self.rx_subdev = "A:A"
            self.tx_lo = 915e6
            self.tx_dsp = {"A:A": 0.0, "A:B": 0.0}
            self.tx_rate = 1e6

        def get_mboard_name(self, mboard=0):
            return "B210"

        def get_tx_num_channels(self):
            return len(self.tx_subdev.split())

        def get_tx_subdev_spec(self, mboard=0):
            return self.tx_subdev

        def set_tx_subdev_spec(self, spec, mboard=0):
            self.tx_subdev = str(spec)

        def get_rx_subdev_spec(self, mboard=0):
            return self.rx_subdev

        def set_rx_subdev_spec(self, spec, mboard=0):
            self.rx_subdev = str(spec)

        def get_tx_lo_freq(self, name="all", chan=0):
            return self.tx_lo

        def get_tx_freq(self, chan=0):
            fe = self.tx_subdev.split()[int(chan)]
            return self.tx_lo + self.tx_dsp[fe]

        def get_tx_rate(self, chan=0):
            return self.tx_rate

        def set_tx_freq(self, tune_request, chan=0):
            fe = self.tx_subdev.split()[int(chan)]
            rf_pol = str(getattr(tune_request, "rf_freq_policy", "AUTO")).upper()
            dsp_pol = str(getattr(tune_request, "dsp_freq_policy", "AUTO")).upper()
            if rf_pol == "MANUAL":
                self.tx_lo = float(tune_request.rf_freq)
            elif rf_pol == "AUTO":
                self.tx_lo = float(getattr(tune_request, "target_freq", tune_request))
            if dsp_pol == "MANUAL":
                self.tx_dsp[fe] = float(tune_request.dsp_freq)
            elif dsp_pol == "AUTO":
                target = float(getattr(tune_request, "target_freq", tune_request))
                self.tx_dsp[fe] = target - self.tx_lo
            return self.get_tx_freq(chan)

        def get_tx_stream(self, stream_args):
            bound_fe = self.tx_subdev.split()[int(stream_args.channels[0])]
            streamer = _FakeStreamer()
            streamer.bound_fe = bound_fe
            return streamer

    fake_uhd = _fake_uhd_module()
    subdev_b = {"__uhd_type__": "SubdevSpec", "spec": "A:B"}
    tune_a = {
        "__uhd_type__": "TuneRequest",
        "rf_freq_policy": "MANUAL",
        "rf_freq": 915e6,
        "dsp_freq_policy": "MANUAL",
        "dsp_freq": 14e6,
    }
    stream_args = {
        "__uhd_type__": "StreamArgs",
        "cpu_format": "fc32",
        "otw_format": "sc16",
        "channels": [0],
    }

    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # Dynamic v2 native path: 5-step B210 sequence refused at step 3 (set_tx_subdev_spec).
        dev = _B210SubdevUSRP()
        schema = UsrpSchema()
        schema.bind(dev)
        schema.configure_lockouts(
            parse_freq_limits({"tx_allow": [[900e6, 930e6]], "admin_group_override": True})
        )
        # Step 1: tune A:A to LO=915 MHz + DSP=+14 MHz = 929 MHz (checked span 914.5..929.5 MHz).
        schema.invoke_native("set_tx_freq", {"tune_request": tune_a, "chan": 0})
        assert dev.get_tx_freq(0) == 929e6
        # Step 2: create TX streamer bound to A:A.
        tx_streamer = schema.invoke_native("get_tx_stream", {}, positional_args=[stream_args])
        assert tx_streamer.bound_fe == "A:A"
        # Step 3: attempt to remap ch0 to A:B -> refused before reaching hardware.
        with pytest.raises(RemoteRFPolicyError, match=r"[Ss]ubdev"):
            schema.invoke_native("set_tx_subdev_spec", {"spec": subdev_b})
        assert dev.tx_subdev == "A:A"

        # v1 IDL path: same sequence refused at call_set_tx_subdev_spec.
        schema.call_set_tx_freq(tune_a, 0)
        schema.call_get_tx_stream(stream_args)
        with pytest.raises(RemoteRFPolicyError, match=r"[Ss]ubdev"):
            schema.call_set_tx_subdev_spec(subdev_b)
        assert dev.tx_subdev == "A:A"

        # RX subdev spec stays allowed when only TX limits are active.
        schema.invoke_native("set_rx_subdev_spec", {"spec": subdev_b})
        assert dev.rx_subdev == "A:B"
        schema.call_set_rx_subdev_spec({"__uhd_type__": "SubdevSpec", "spec": "A:A"})
        assert dev.rx_subdev == "A:A"

        # Admin bypass lets TX subdev spec through on both v2 and v1.
        schema.lockout_bypass = True
        schema.invoke_native("set_tx_subdev_spec", {"spec": subdev_b})
        assert dev.tx_subdev == "A:B"
        schema.call_set_tx_subdev_spec({"__uhd_type__": "SubdevSpec", "spec": "A:A"})
        assert dev.tx_subdev == "A:A"

        # With only RX limits active: RX subdev spec refused on v2 and v1; TX subdev spec allowed.
        schema.lockout_bypass = False
        schema.configure_lockouts(
            parse_freq_limits({"rx_allow": [[900e6, 930e6]], "admin_group_override": True})
        )
        with pytest.raises(RemoteRFPolicyError, match=r"[Ss]ubdev"):
            schema.invoke_native("set_rx_subdev_spec", {"spec": subdev_b})
        with pytest.raises(RemoteRFPolicyError, match=r"[Ss]ubdev"):
            schema.call_set_rx_subdev_spec(subdev_b)
        assert dev.rx_subdev == "A:A"
        schema.invoke_native("set_tx_subdev_spec", {"spec": subdev_b})
        assert dev.tx_subdev == "A:B"
        schema.call_set_tx_subdev_spec({"__uhd_type__": "SubdevSpec", "spec": "A:A"})
        assert dev.tx_subdev == "A:A"

        # Without admin_group_override, lockout_bypass does not allow subdev spec changes.
        schema.configure_lockouts(parse_freq_limits({"tx_allow": [[900e6, 930e6]]}))
        schema.lockout_bypass = True
        with pytest.raises(RemoteRFPolicyError, match=r"[Ss]ubdev"):
            schema.invoke_native("set_tx_subdev_spec", {"spec": subdev_b})
        with pytest.raises(RemoteRFPolicyError, match=r"[Ss]ubdev"):
            schema.call_set_tx_subdev_spec(subdev_b)

        # Without any limits active, both TX and RX subdev spec changes are allowed.
        schema.configure_lockouts(None)
        schema.lockout_bypass = False
        schema.invoke_native("set_tx_subdev_spec", {"spec": subdev_b})
        schema.call_set_rx_subdev_spec(subdev_b)
        assert dev.tx_subdev == "A:B"
        assert dev.rx_subdev == "A:B"


def test_usrp_set_tx_iq_balance_refused_with_tx_limits_and_allowed_without():
    """Round 4 item 2: set_tx_iq_balance refused while TX limits are active, allowed without."""

    class _IqBalanceUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_iq_calls = []

        def set_tx_iq_balance(self, correction, chan=0):
            self.tx_iq_calls.append((correction, chan))

    dev = _IqBalanceUSRP()
    schema = UsrpSchema()
    schema.bind(dev)

    # Without limits, set_tx_iq_balance is allowed.
    schema.invoke_native("set_tx_iq_balance", {"correction": complex(-1, 0), "chan": 0})
    assert dev.tx_iq_calls == [(complex(-1, 0), 0)]

    # With TX limits active (allow or deny), set_tx_iq_balance is refused (keyword and positional).
    for cfg in (
        {"tx_allow": [[900e6, 930e6]], "admin_group_override": True},
        {"tx_deny": [[910e6, 912e6]], "admin_group_override": True},
    ):
        schema.configure_lockouts(parse_freq_limits(cfg))
        schema.lockout_bypass = False
        with pytest.raises(RemoteRFPolicyError, match=r"IQ balance"):
            schema.invoke_native("set_tx_iq_balance", {"correction": complex(-1, 0), "chan": 0})
        with pytest.raises(RemoteRFPolicyError, match=r"IQ balance"):
            schema.invoke_native("set_tx_iq_balance", {}, positional_args=[complex(-1, 0), 0])
    assert len(dev.tx_iq_calls) == 1

    # Admin bypass allows set_tx_iq_balance when admin_group_override is enabled.
    schema.lockout_bypass = True
    schema.invoke_native("set_tx_iq_balance", {"correction": complex(0.5, 0), "chan": 0})
    assert dev.tx_iq_calls[-1] == (complex(0.5, 0), 0)

    # With only RX limits active, set_tx_iq_balance is allowed.
    schema.lockout_bypass = False
    schema.configure_lockouts(parse_freq_limits({"rx_allow": [[900e6, 930e6]]}))
    schema.invoke_native("set_tx_iq_balance", {"correction": complex(0.25, 0), "chan": 0})
    assert dev.tx_iq_calls[-1] == (complex(0.25, 0), 0)


def test_usrp_set_tx_filter_refused_while_set_tx_bandwidth_allowed():
    """Round 4 item 3: set_tx_filter refused while TX limits are active; set_tx_bandwidth stays allowed."""

    class _FilterUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_filter_calls = []
            self.tx_bandwidth_calls = []

        def set_tx_filter(self, name, filter_obj, chan=0):
            self.tx_filter_calls.append((name, filter_obj, chan))

        def set_tx_bandwidth(self, bandwidth, chan=0):
            self.tx_bandwidth_calls.append((bandwidth, chan))

    fake_uhd = _fake_uhd_module()
    fir_payload = {
        "__uhd_type__": "FilterInfoBase",
        "native_class": "digital_filter_fir_i16",
        "filter_type": "DIGITAL_FIR_I16",
        "bypassed": False,
        "position": 0,
        "input_rate": 16e6,
        "interpolation": 2,
        "decimation": 1,
        "tap_full_scale": 32767,
        "max_num_taps": 128,
        "taps": [32767] + [0] * 15,
    }
    dev = _FilterUSRP()
    schema = UsrpSchema()
    schema.bind(dev)
    schema.configure_lockouts(
        parse_freq_limits({"tx_allow": [[900e6, 930e6]], "admin_group_override": True})
    )

    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # set_tx_filter refused (positional as in manifest, and keyword).
        with pytest.raises(RemoteRFPolicyError, match=r"[Tt][Xx] filter"):
            schema.invoke_native("set_tx_filter", {}, positional_args=["FIR_1", fir_payload, 0])
        with pytest.raises(RemoteRFPolicyError, match=r"[Tt][Xx] filter"):
            schema.invoke_native(
                "set_tx_filter", {"name": "FIR_1", "filter": fir_payload, "chan": 0}
            )
        assert dev.tx_filter_calls == []

        # set_tx_bandwidth remains allowed while TX limits are active.
        schema.invoke_native("set_tx_bandwidth", {"bandwidth": 56e6, "chan": 0})
        assert dev.tx_bandwidth_calls == [(56e6, 0)]

        # Admin bypass allows set_tx_filter.
        schema.lockout_bypass = True
        schema.invoke_native("set_tx_filter", {}, positional_args=["FIR_1", fir_payload, 0])
        assert len(dev.tx_filter_calls) == 1

        # Without TX limits (RX-only or None), set_tx_filter is allowed.
        schema.lockout_bypass = False
        schema.configure_lockouts(parse_freq_limits({"rx_allow": [[900e6, 930e6]]}))
        schema.invoke_native("set_tx_filter", {}, positional_args=["FIR_1", fir_payload, 0])
        assert len(dev.tx_filter_calls) == 2

        schema.configure_lockouts(None)
        schema.invoke_native("set_tx_filter", {}, positional_args=["FIR_1", fir_payload, 0])
        assert len(dev.tx_filter_calls) == 3


def test_hackrf_filter_bandwidth_widening_refused():
    """Round 4 item 4: HackRF TX band uses max(sample_rate, filter_bandwidth) and checks live widening."""
    dev = _FakeHackRF()
    schema = HackRFSchema()
    schema.bind(dev)
    schema.configure_lockouts(
        parse_freq_limits(
            {
                "tx_allow": [[902e6, 928e6]],
                "tx_deny": [[918e6, 920e6]],
                "admin_group_override": True,
            }
        )
    )

    # Idle widening: 2 MS/s with 28 MHz filter at 903 MHz -> 889..917 MHz -> refused on start_tx.
    schema.set_sample_rate(2_000_000)
    schema.set_filter_bandwidth(28_000_000)
    schema.set_center_freq(903_000_000)
    with pytest.raises(RemoteRFPolicyError, match=r"889–917 MHz.*902–928 MHz"):
        schema.call_start_tx()
    assert dev.tx_starts == 0

    # Narrow filter (1.75 MHz) at 903 MHz / 2 MS/s -> 902..904 MHz -> start_tx allowed.
    schema.set_filter_bandwidth(1_750_000)
    schema.call_start_tx()
    assert dev.tx_starts == 1

    # Live widening past tx_allow while TX is active -> refused, filter unchanged.
    with pytest.raises(RemoteRFPolicyError, match=r"900\.5–905\.5 MHz.*902–928 MHz"):
        schema.set_filter_bandwidth(5_000_000)
    assert dev.filter_bandwidth == 1_750_000
    with pytest.raises(RemoteRFPolicyError, match=r"889–917 MHz.*902–928 MHz"):
        schema.set_filter_bandwidth(28_000_000)
    assert dev.filter_bandwidth == 1_750_000

    # Move center to 914 MHz while TX is active (913..915 MHz allowed).
    schema.set_center_freq(914_000_000)
    # In-band widening (5 MHz -> 911.5..916.5 MHz) succeeds.
    schema.set_filter_bandwidth(5_000_000)
    assert dev.filter_bandwidth == 5_000_000
    # Widening into tx_deny [918, 920] (8 MHz -> 910..918 MHz, including snap from 7.6 MHz) refused.
    with pytest.raises(RemoteRFPolicyError, match=r"910–918 MHz.*918–920 MHz"):
        schema.set_filter_bandwidth(7_600_000)
    assert dev.filter_bandwidth == 5_000_000
    # Live retune also uses filter_bandwidth (916 ± 2.5 = 913.5..918.5 touches 918..920 deny) -> refused.
    with pytest.raises(RemoteRFPolicyError, match=r"918–920 MHz"):
        schema.set_center_freq(916_000_000)
    assert dev.center_freq == 914_000_000

    # Admin bypass permits live filter widening when enabled.
    schema.lockout_bypass = True
    schema.set_filter_bandwidth(28_000_000)
    assert dev.filter_bandwidth == 28_000_000
    schema.call_stop_tx()

    # Unreadable/non-finite filter_bandwidth fails closed on start_tx.
    schema.lockout_bypass = False
    dev.filter_bandwidth = float("nan")
    with pytest.raises(RemoteRFPolicyError, match=r"filter bandwidth"):
        schema.call_start_tx()
    dev.filter_bandwidth = None
    with pytest.raises(RemoteRFPolicyError, match=r"filter bandwidth"):
        schema.call_start_tx()

    # Without limits, widening while TX is active is allowed.
    dev.filter_bandwidth = 1_750_000
    schema.configure_lockouts(None)
    schema.call_start_tx()
    schema.set_filter_bandwidth(28_000_000)
    assert dev.filter_bandwidth == 28_000_000
    schema.call_stop_tx()


# ── Regression tests for RF-safety review findings (H1, M2, M3, H4) ───


def test_usrp_lo_checked_when_dsp_policy_is_none_or_lo_freq_set():
    """H1: TX/RX LO itself and transmitted band must stay inside allow and outside deny."""

    class _FullTuneUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_lo_freqs = {0: 915e6}
            self.rx_lo_freqs = {0: 915e6}
            self.tx_dsp = {0: 0.0}
            self.rx_dsp = {0: 0.0}
            self.tx_rates = {0: 2e6}

        def get_tx_lo_freq(self, name="all", chan=0):
            return self.tx_lo_freqs[int(chan)]

        def get_rx_lo_freq(self, name="all", chan=0):
            return self.rx_lo_freqs[int(chan)]

        def get_tx_freq(self, chan=0):
            ch = int(chan)
            return self.tx_lo_freqs[ch] + self.tx_dsp[ch]

        def get_rx_freq(self, chan=0):
            ch = int(chan)
            return self.rx_lo_freqs[ch] + self.rx_dsp[ch]

        def set_tx_freq(self, tune_request, chan=0):
            ch = int(chan)
            rf_pol = str(getattr(tune_request, "rf_freq_policy", "AUTO")).upper()
            dsp_pol = str(getattr(tune_request, "dsp_freq_policy", "AUTO")).upper()
            if rf_pol == "MANUAL":
                self.tx_lo_freqs[ch] = float(tune_request.rf_freq)
            elif rf_pol == "AUTO":
                self.tx_lo_freqs[ch] = float(getattr(tune_request, "target_freq", tune_request))
            if dsp_pol == "AUTO":
                target = float(getattr(tune_request, "target_freq", tune_request))
                self.tx_dsp[ch] = target - self.tx_lo_freqs[ch]
            elif dsp_pol == "MANUAL":
                self.tx_dsp[ch] = float(tune_request.dsp_freq)
            return self.get_tx_freq(ch)

        def set_rx_freq(self, tune_request, chan=0):
            ch = int(chan)
            rf_pol = str(getattr(tune_request, "rf_freq_policy", "AUTO")).upper()
            dsp_pol = str(getattr(tune_request, "dsp_freq_policy", "AUTO")).upper()
            if rf_pol == "MANUAL":
                self.rx_lo_freqs[ch] = float(tune_request.rf_freq)
            elif rf_pol == "AUTO":
                self.rx_lo_freqs[ch] = float(getattr(tune_request, "target_freq", tune_request))
            if dsp_pol == "AUTO":
                target = float(getattr(tune_request, "target_freq", tune_request))
                self.rx_dsp[ch] = target - self.rx_lo_freqs[ch]
            elif dsp_pol == "MANUAL":
                self.rx_dsp[ch] = -float(tune_request.dsp_freq)
            return self.get_rx_freq(ch)

        def set_tx_lo_freq(self, freq, name, chan=0):
            self.tx_lo_freqs[int(chan)] = float(freq)
            return float(freq)

        def set_rx_lo_freq(self, freq, name, chan=0):
            self.rx_lo_freqs[int(chan)] = float(freq)
            return float(freq)

    fake_uhd = _fake_uhd_module()
    dev = _FullTuneUSRP()
    schema = UsrpSchema()
    schema.bind(dev)
    schema.configure_lockouts(
        parse_freq_limits(
            {
                "tx_allow": [[902e6, 928e6]],
                "tx_deny": [[920e6, 922e6]],
                "rx_allow": [[902e6, 928e6]],
                "rx_deny": [[920e6, 922e6]],
            }
        )
    )

    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # Exact H1 TX repro: tune rf=925 MHz, target=915 MHz (DSP=-10 MHz) in a no-deny band first.
        schema.configure_lockouts(
            parse_freq_limits({"tx_allow": [[902e6, 928e6]], "rx_allow": [[902e6, 928e6]]})
        )
        schema.invoke_native(
            "set_tx_freq",
            {
                "tune_request": {
                    "__uhd_type__": "TuneRequest",
                    "target_freq": 915e6,
                    "rf_freq_policy": "MANUAL",
                    "rf_freq": 925e6,
                    "dsp_freq_policy": "AUTO",
                },
                "chan": 0,
            },
        )
        assert dev.tx_lo_freqs[0] == 925e6
        assert dev.get_tx_freq(0) == 915e6

        # Moving TX LO to 935 MHz would leave signal at 925 MHz (in band) but LO at 935 MHz -> refused.
        with pytest.raises(RemoteRFPolicyError, match=r"935 MHz"):
            schema.invoke_native("set_tx_lo_freq", {"freq": 935e6, "name": "all", "chan": 0})
        assert dev.tx_lo_freqs[0] == 925e6

        # Same via (AUTO, NONE) and (MANUAL, NONE) TuneRequests.
        with pytest.raises(RemoteRFPolicyError, match=r"935 MHz"):
            schema.call_set_tx_freq(
                {
                    "__uhd_type__": "TuneRequest",
                    "target_freq": 935e6,
                    "rf_freq_policy": "AUTO",
                    "dsp_freq_policy": "NONE",
                },
                0,
            )
        with pytest.raises(RemoteRFPolicyError, match=r"935 MHz"):
            schema.call_set_tx_freq(
                {
                    "__uhd_type__": "TuneRequest",
                    "rf_freq": 935e6,
                    "rf_freq_policy": "MANUAL",
                    "dsp_freq_policy": "NONE",
                },
                0,
            )
        assert dev.tx_lo_freqs[0] == 925e6

        # Same for RX: rf=925 MHz, target=915 MHz (shift=-10 MHz), then set_rx_lo_freq(935e6) -> refused.
        schema.invoke_native(
            "set_rx_freq",
            {
                "tune_request": {
                    "__uhd_type__": "TuneRequest",
                    "target_freq": 915e6,
                    "rf_freq_policy": "MANUAL",
                    "rf_freq": 925e6,
                    "dsp_freq_policy": "AUTO",
                },
                "chan": 0,
            },
        )
        with pytest.raises(RemoteRFPolicyError, match=r"935 MHz"):
            schema.invoke_native("set_rx_lo_freq", {"freq": 935e6, "name": "all", "chan": 0})
        with pytest.raises(RemoteRFPolicyError, match=r"935 MHz"):
            schema.call_set_rx_freq(
                {
                    "__uhd_type__": "TuneRequest",
                    "target_freq": 935e6,
                    "rf_freq_policy": "AUTO",
                    "dsp_freq_policy": "NONE",
                },
                0,
            )
        assert dev.rx_lo_freqs[0] == 925e6

        # LO landing inside a deny range while signal stays outside deny -> refused on TX and RX.
        schema.configure_lockouts(
            parse_freq_limits(
                {
                    "tx_allow": [[902e6, 928e6]],
                    "tx_deny": [[920e6, 922e6]],
                    "rx_allow": [[902e6, 928e6]],
                    "rx_deny": [[920e6, 922e6]],
                }
            )
        )
        with pytest.raises(RemoteRFPolicyError, match=r"921 MHz"):
            schema.invoke_native("set_tx_lo_freq", {"freq": 921e6, "name": "all", "chan": 0})
        with pytest.raises(RemoteRFPolicyError, match=r"921 MHz"):
            schema.invoke_native("set_rx_lo_freq", {"freq": 921e6, "name": "all", "chan": 0})

        # Live stream / (NONE, NONE) checks refuse if hardware LO was left outside allow.
        dev.tx_lo_freqs[0] = 935e6
        dev.tx_dsp[0] = -20e6  # get_tx_freq == 915 MHz, LO == 935 MHz
        with pytest.raises(RemoteRFPolicyError, match=r"935 MHz"):
            schema.check_tx_channels([0])
        with pytest.raises(RemoteRFPolicyError, match=r"935 MHz"):
            schema.call_set_tx_freq(
                {"__uhd_type__": "TuneRequest", "rf_freq_policy": "NONE", "dsp_freq_policy": "NONE"},
                0,
            )
        dev.rx_lo_freqs[0] = 935e6
        dev.rx_dsp[0] = -20e6  # get_rx_freq == 915 MHz, LO == 935 MHz
        with pytest.raises(RemoteRFPolicyError, match=r"935 MHz"):
            schema.check_rx_channels([0])


def test_usrp_manual_dsp_checks_both_signs_for_inverted_iq():
    """M2: MANUAL DSP offset checks [rf - |dsp|, rf + |dsp|] so inverted-IQ boards cannot escape."""

    class _LoUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_lo_freqs = {0: 906e6}
            self.rx_lo_freqs = {0: 906e6}
            self.tx_rates = {0: 2e6}

        def get_tx_lo_freq(self, name="all", chan=0):
            return self.tx_lo_freqs[int(chan)]

        def get_rx_lo_freq(self, name="all", chan=0):
            return self.rx_lo_freqs[int(chan)]

    fake_uhd = _fake_uhd_module()
    dev = _LoUSRP()
    schema = UsrpSchema()
    schema.bind(dev)
    schema.configure_lockouts(
        parse_freq_limits({"tx_allow": [[902e6, 928e6]], "rx_allow": [[902e6, 928e6]]})
    )

    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # TX rf=906 MHz, dsp=+10 MHz: +sign lands at 916 MHz (in band), inverted -sign lands at 896 MHz -> refused.
        for req in (
            {
                "__uhd_type__": "TuneRequest",
                "target_freq": 906e6,
                "rf_freq_policy": "AUTO",
                "dsp_freq_policy": "MANUAL",
                "dsp_freq": 10e6,
            },
            {
                "__uhd_type__": "TuneRequest",
                "rf_freq": 906e6,
                "rf_freq_policy": "MANUAL",
                "dsp_freq_policy": "MANUAL",
                "dsp_freq": 10e6,
            },
            {
                "__uhd_type__": "TuneRequest",
                "rf_freq_policy": "NONE",
                "dsp_freq_policy": "MANUAL",
                "dsp_freq": 10e6,
            },
        ):
            with pytest.raises(RemoteRFPolicyError, match=r"895–917 MHz"):
                schema.call_set_tx_freq(req, 0)

        # RX rf=906 MHz, dsp=-10 MHz: old -sign landed at 916 MHz (in band), inverted +sign lands at 896 MHz -> refused.
        for req in (
            {
                "__uhd_type__": "TuneRequest",
                "target_freq": 906e6,
                "rf_freq_policy": "AUTO",
                "dsp_freq_policy": "MANUAL",
                "dsp_freq": -10e6,
            },
            {
                "__uhd_type__": "TuneRequest",
                "rf_freq": 906e6,
                "rf_freq_policy": "MANUAL",
                "dsp_freq_policy": "MANUAL",
                "dsp_freq": -10e6,
            },
            {
                "__uhd_type__": "TuneRequest",
                "rf_freq_policy": "NONE",
                "dsp_freq_policy": "MANUAL",
                "dsp_freq": -10e6,
            },
        ):
            with pytest.raises(RemoteRFPolicyError, match=r"896–916 MHz"):
                schema.call_set_rx_freq(req, 0)

        # Symmetric in-band shift (915 ± 5 MHz, widened 909..921 MHz) remains allowed.
        schema.call_set_tx_freq(
            {
                "__uhd_type__": "TuneRequest",
                "rf_freq": 915e6,
                "rf_freq_policy": "MANUAL",
                "dsp_freq_policy": "MANUAL",
                "dsp_freq": 5e6,
            },
            0,
        )
        schema.call_set_rx_freq(
            {
                "__uhd_type__": "TuneRequest",
                "rf_freq": 915e6,
                "rf_freq_policy": "MANUAL",
                "dsp_freq_policy": "MANUAL",
                "dsp_freq": -5e6,
            },
            0,
        )


@pytest.mark.parametrize("cls", [PlutoSchema, PlutoMIMOSchema])
def test_pluto_rx_checked_on_call_rx_and_parked_on_hygiene(cls):
    """M3: Pluto checks rx_lo ± sample_rate/2 on call_rx() and parks rx_lo in _lockout_hygiene."""

    class _RxFakePluto(_FakePluto):
        def __init__(self):
            super().__init__()
            self.rx_lo = 2_450_000_000  # boot default outside rx_allow
            self.rx_calls = 0

        def rx(self):
            self.rx_calls += 1
            return np.zeros(8, dtype=np.complex64)

        def rx_destroy_buffer(self):
            pass

    cfg = parse_lockouts(
        {
            "rx_allow": [[902e6, 928e6]],
            "rx_deny": [[920e6, 922e6]],
            "admin_group_override": True,
        }
    )
    assert cfg is not None

    radio = _RxFakePluto()
    schema = cls()
    schema.bind(radio)
    schema.configure_lockouts(cfg)
    # Initial settle parks out-of-band boot default rx_lo into legal range.
    assert radio.rx_lo == 911_000_000

    # If rx_lo is left out of band (e.g. set by an admin or hardware default), call_rx() refuses.
    schema.lockout_bypass = True
    schema.dispatch("set_rx_lo", {"value": 2_450_000_000})
    assert radio.rx_lo == 2_450_000_000
    schema.lockout_bypass = False
    with pytest.raises(RemoteRFPolicyError, match=r"2449–2451 MHz.*902–928 MHz"):
        schema.dispatch("call_rx", {})
    assert radio.rx_calls == 0

    # Between-holders reset parks rx_lo back into a legal frequency.
    schema.reset_for_next_holder()
    assert radio.rx_lo == 911_000_000
    assert schema.dispatch("call_rx", {}).size == 8
    assert radio.rx_calls == 1

    # Edge spillover past rx_allow (902 MHz ± 1 MHz = 901..903) -> call_rx() refused.
    schema.dispatch("set_rx_lo", {"value": 902_000_000})
    with pytest.raises(RemoteRFPolicyError, match=r"901–903 MHz.*902–928 MHz"):
        schema.dispatch("call_rx", {})
    assert radio.rx_calls == 1

    # Edge touching rx_deny [920, 922] (919 MHz ± 1 MHz = 918..920) -> call_rx() refused.
    schema.dispatch("set_rx_lo", {"value": 919_000_000})
    with pytest.raises(RemoteRFPolicyError, match=r"918–920 MHz.*920–922 MHz"):
        schema.dispatch("call_rx", {})
    assert radio.rx_calls == 1

    # When no allowed RX range remains, reset still succeeds and call_rx() stays refused.
    no_rx_cfg = parse_lockouts({"rx_allow": []})
    schema.configure_lockouts(no_rx_cfg)
    schema.reset_for_next_holder()
    with pytest.raises(RemoteRFPolicyError):
        schema.dispatch("call_rx", {})
    assert radio.rx_calls == 1


def test_hackrf_reset_for_next_holder_succeeds_under_limits_excluding_default_freq():
    """H4: HackRF reset_for_next_holder succeeds even when limits exclude 100 MHz."""

    class _ResettableHackRF(_FakeHackRF):
        def __init__(self):
            super().__init__()
            self.bias_tee_on = True
            self.amplifier_on = True
            self._amplifier_on = True
            self.txvga_gain = 40
            self.lna_gain = 32
            self._lna_gain = 32
            self.vga_gain = 40
            self.sample_count_limit = 1024
            self.tx_stopped = 0
            self.fail_stop_tx = False

        def stop_tx(self):
            if self.fail_stop_tx:
                raise OSError("usb stop_tx failed")
            self.tx_stopped += 1

    dev = _ResettableHackRF()
    schema = HackRFSchema()
    schema.bind(dev)
    schema.configure_lockouts(
        parse_freq_limits(
            {
                "tx_allow": [[902e6, 928e6]],
                "rx_allow": [[902e6, 928e6]],
            }
        )
    )
    schema.set_center_freq(920_000_000)
    schema.call_start_tx()

    # Must succeed without RemoteRFPolicyError and leave hardware clean + parked in-band.
    schema.reset_for_next_holder()
    assert dev.tx_stopped == 1
    assert dev.buffer == bytearray()
    assert dev.bias_tee_on is False
    assert dev._amplifier_on is False
    assert dev.txvga_gain == 0
    assert dev.lna_gain == 16
    assert dev.vga_gain == 16
    assert dev.center_freq == 915_000_000
    assert dev.sample_rate == 20_000_000
    assert dev.filter_bandwidth == 15_000_000
    assert dev.sample_count_limit == 0

    # User calls remain strictly checked after reset.
    with pytest.raises(RemoteRFPolicyError, match=r"100 MHz"):
        schema.set_center_freq(100_000_000)

    # Even with empty allow lists (no legal frequency exists), reset succeeds and user calls fail.
    schema.configure_lockouts(parse_freq_limits({"tx_allow": [], "rx_allow": []}))
    schema.reset_for_next_holder()
    assert dev.center_freq == 100_000_000
    with pytest.raises(RemoteRFPolicyError):
        schema.set_center_freq(915_000_000)
    with pytest.raises(RemoteRFPolicyError):
        schema.call_read_samples(4)

    # If stop_tx fails while TX stays marked active, reset propagates the USB error (not policy error)
    # and still completes the rest of the reset.
    dev2 = _ResettableHackRF()
    dev2.fail_stop_tx = True
    schema2 = HackRFSchema()
    schema2.bind(dev2)
    schema2.configure_lockouts(
        parse_freq_limits({"tx_allow": [[902e6, 928e6]], "rx_allow": [[902e6, 928e6]]})
    )
    schema2.set_center_freq(920_000_000)
    schema2.call_start_tx()
    with pytest.raises(OSError, match="usb stop_tx failed"):
        schema2.reset_for_next_holder()
    assert dev2.bias_tee_on is False
    assert dev2.txvga_gain == 0
    assert dev2.center_freq == 915_000_000
    assert dev2.sample_rate == 20_000_000
    assert dev2.filter_bandwidth == 15_000_000


# ── Round-2 regression tests (R1/F1, R2, R3, R4/F4, R6, F3) ──────────


def test_usrp_phaser_refuses_phaser_controls_while_limits_active():
    """R1/F1: UsrpPhaserSchema marks supports_freq_limits=False and refuses phaser_* while limits are active."""

    class _FakeGpios:
        def __init__(self):
            self.gpio_tx_sw = 0
            self.gpio_vctrl_1 = 0
            self.gpio_vctrl_2 = 0

    class _FakePhaser:
        def __init__(self, uri="ip:localhost"):
            self.uri = uri
            self._gpios = _FakeGpios()
            self.enable = 1
            self.frequency = None
            self.SignalFreq = None

    assert UsrpPhaserSchema.supports_freq_limits is False
    fake = _FakePhaser()
    dev = _FakeUSRP()
    schema = UsrpPhaserSchema()
    schema.bind(dev)
    schema.phaser = fake

    for limit_cfg in (
        {"tx_allow": [[902e6, 928e6]], "admin_group_override": True},
        {"rx_deny": [[10.6e9, 10.7e9]], "admin_group_override": True},
    ):
        schema.configure_lockouts(parse_freq_limits(limit_cfg))
        schema.lockout_bypass = False
        for method, kw in (
            ("phaser_init", {"uri": "ip:10.0.0.99"}),
            ("phaser_set_lo_freq", {"signal_freq": 10.25e9, "if_freq": 2.1e9}),
            ("phaser_set_tx_switch", {"value": 1}),
            ("phaser_set_tx_path", {"enabled": True}),
        ):
            with pytest.raises(RemoteRFPolicyError, match=r"usrp_phaser"):
                schema.invoke_native(method, kw)
            with pytest.raises(RemoteRFPolicyError, match=r"usrp_phaser"):
                schema.dispatch(f"call_{method}", kw)
        assert fake.frequency is None
        assert fake.SignalFreq is None
        assert fake._gpios.gpio_tx_sw == 0
        assert fake._gpios.gpio_vctrl_1 == 0
        assert fake.enable == 1
        assert schema._phaser_uri == "ip:localhost"

    # Without limits, phaser controls work normally.
    schema.configure_lockouts(None)
    with patch(
        "remoterf_device_core.drivers.usrp_phaser.usrp_phaser_schema._require_phaser_module",
        return_value=_FakePhaser,
    ):
        init_res = schema.invoke_native("phaser_init", {"uri": "ip:10.0.0.2"})
    assert init_res == {"connected": True, "uri": "ip:10.0.0.2"}
    lo_res = schema.invoke_native("phaser_set_lo_freq", {"signal_freq": 10.25e9, "if_freq": 2.1e9})
    assert lo_res["signal_freq"] == 10.25e9
    assert schema.invoke_native("phaser_set_tx_switch", {"value": 1}) == 1
    assert schema.invoke_native("phaser_set_tx_path", {"enabled": True}) is True


def test_usrp_send_waveform_and_recv_num_samps_refuse_empty_channels_and_streamer():
    """R2: send_waveform and recv_num_samps refuse empty channels and caller-supplied streamers."""
    dev = _FakeUSRP()
    schema = UsrpSchema()
    schema.bind(dev)
    schema.configure_lockouts(
        parse_freq_limits({"tx_allow": [[902e6, 928e6]], "rx_allow": [[902e6, 928e6]]})
    )
    proto = np.zeros(8, dtype=np.complex64)

    # send_waveform refuses empty/None channels and caller-supplied streamer (keyword & positional).
    for kw, pos in (
        ({"waveform_proto": proto, "duration": 0.1, "freq": 915e6, "rate": 1e6, "channels": []}, None),
        ({"waveform_proto": proto, "duration": 0.1, "freq": 915e6, "rate": 1e6, "channels": None}, None),
        ({"waveform_proto": proto, "duration": 0.1, "freq": 915e6, "rate": 1e6, "channels": [0], "streamer": dev.tx_streamer}, None),
        ({}, [proto, 0.1, 915e6, 1e6, [], 10, None, None]),
        ({}, [proto, 0.1, 915e6, 1e6, [0], 10, None, dev.tx_streamer]),
    ):
        with pytest.raises(RemoteRFPolicyError):
            schema.invoke_native("send_waveform", kw, positional_args=pos)
    assert dev.waveforms_sent == 0

    # recv_num_samps refuses empty/None channels and caller-supplied streamer (keyword & positional).
    for kw, pos in (
        ({"num_samps": 8, "freq": 915e6, "rate": 1e6, "channels": []}, None),
        ({"num_samps": 8, "freq": 915e6, "rate": 1e6, "channels": None}, None),
        ({"num_samps": 8, "freq": 915e6, "rate": 1e6, "channels": [0], "streamer": dev.rx_streamer}, None),
        ({}, [8, 915e6, 1e6, [], 10, None, None]),
        ({}, [8, 915e6, 1e6, [0], 10, None, dev.rx_streamer]),
    ):
        with pytest.raises(RemoteRFPolicyError):
            schema.invoke_native("recv_num_samps", kw, positional_args=pos)
    assert dev.rx_samps_calls == 0

    # Valid non-empty channels with no caller-supplied streamer: recv_num_samps succeeds;
    # send_waveform stays refused while TX limits are active and works without TX limits.
    with pytest.raises(RemoteRFPolicyError, match=r"send_waveform"):
        schema.invoke_native(
            "send_waveform",
            {"waveform_proto": proto, "duration": 0.1, "freq": 915e6, "rate": 1e6, "channels": [0], "streamer": None},
        )
    schema.invoke_native(
        "recv_num_samps",
        {"num_samps": 8, "freq": 915e6, "rate": 1e6, "channels": [0], "streamer": None},
    )
    assert dev.waveforms_sent == 0
    assert dev.rx_samps_calls == 1
    schema.configure_lockouts(parse_freq_limits({"rx_allow": [[902e6, 928e6]]}))
    schema.invoke_native(
        "send_waveform",
        {"waveform_proto": proto, "duration": 0.1, "freq": 915e6, "rate": 1e6, "channels": [0], "streamer": None},
    )
    assert dev.waveforms_sent == 1


def test_usrp_timed_tx_refused_and_v1_reset_closes_legacy_device():
    """R3: timed TX metadata and send_waveform(start_time) refused under TX limits; v1 reset closes legacy device."""

    class _ClosableLoUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.closed = False

        def get_tx_lo_freq(self, name="all", chan=0):
            return self.tx_freqs[int(chan)]

        def close(self):
            self.closed = True

    fake_uhd = _fake_uhd_module()
    dev = _ClosableLoUSRP()
    schema = UsrpSchema()
    schema.bind_inventory({"serial": "LEGACY1", "tx_allow": [[902e6, 928e6]]})

    with (
        patch("remoterf_device_core.drivers.usrp.usrp_schema._connect_usrp", return_value=dev),
        patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd),
    ):
        # Lazy v1 open via dispatch sets _legacy_device.
        stream = schema.dispatch(
            "call_get_tx_stream",
            {"stream_args": {"cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}},
        )
        assert schema._legacy_device is True

        # Timed TXMetadata (has_time_spec=True or non-zero time_spec) refused in call_stream_send.
        for bad_md in (
            {"has_time_spec": True, "time_spec": {"__uhd_type__": "TimeSpec", "secs": 2.0}},
            {"has_time_spec": True},
            {"has_time_spec": False, "time_spec": {"__uhd_type__": "TimeSpec", "secs": 2.0}},
        ):
            with pytest.raises(RemoteRFPolicyError, match=r"time_spec"):
                schema.dispatch(
                    "call_stream_send",
                    {
                        "value": {
                            "handle": stream["handle"],
                            "samples": np.zeros(8, dtype=np.complex64),
                            "metadata": bad_md,
                        }
                    },
                )
        assert dev.tx_streamer.sent == 0

        # Default untimed metadata (including TimeSpec(0.0) with has_time_spec=False) is allowed.
        schema.dispatch(
            "call_stream_send",
            {
                "value": {
                    "handle": stream["handle"],
                    "samples": np.zeros(8, dtype=np.complex64),
                    "metadata": {
                        "has_time_spec": False,
                        "time_spec": {"__uhd_type__": "TimeSpec", "secs": 0.0},
                    },
                }
            },
        )
        assert dev.tx_streamer.sent == 1

        # send_waveform with start_time refused (keyword & positional).
        proto = np.zeros(8, dtype=np.complex64)
        ts = {"__uhd_type__": "TimeSpec", "secs": 2.0}
        with pytest.raises(RemoteRFPolicyError, match=r"send_waveform"):
            schema.invoke_native(
                "send_waveform",
                {"waveform_proto": proto, "duration": 0.1, "freq": 915e6, "rate": 1e6, "start_time": ts},
            )
        with pytest.raises(RemoteRFPolicyError, match=r"send_waveform"):
            schema.invoke_native("send_waveform", {}, positional_args=[proto, 0.1, 915e6, 1e6, [0], 10, ts])
        assert dev.waveforms_sent == 0

        # reset_for_next_holder closes the lazy v1 device and clears streamers.
        schema.reset_for_next_holder()
        assert dev.closed is True
        assert schema.device is None
        assert schema._legacy_device is False
        assert schema._streamers() == {}


def test_usrp_tune_request_nonempty_args_and_unreadable_stream_lo_refused():
    """R4/F4: non-empty TuneRequest.args and unreadable LO at stream start are refused."""

    class _NoLoUSRP(_FakeUSRP):
        def issue_stream_cmd(self, rate, chan=0):
            self.last_cmd = (rate, chan)

    fake_uhd = _fake_uhd_module()
    dev = _NoLoUSRP()
    schema = UsrpSchema()
    schema.bind(dev)
    schema.configure_lockouts(
        parse_freq_limits({"tx_allow": [[902e6, 928e6]], "rx_allow": [[902e6, 928e6]]})
    )

    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # Non-empty TuneRequest.args (string, DeviceAddr payload, dict, or native DeviceAddr) refused on TX & RX.
        bad_reqs = (
            {"__uhd_type__": "TuneRequest", "target_freq": 915e6, "args": "mode_n=integer"},
            {
                "__uhd_type__": "TuneRequest",
                "target_freq": 915e6,
                "args": {"__uhd_type__": "DeviceAddr", "items": {"mode_n": "integer", "int_n_step": "100e6"}},
            },
            {"__uhd_type__": "TuneRequest", "target_freq": 915e6, "args": {"mode_n": "integer"}},
        )
        for req in bad_reqs:
            assert _extract_tune_bounds(req, side="tx") is None
            with pytest.raises(RemoteRFPolicyError):
                schema.call_set_tx_freq(req, 0)
            with pytest.raises(RemoteRFPolicyError):
                schema.call_set_rx_freq(req, 0)
            with pytest.raises(RemoteRFPolicyError):
                schema.invoke_native("set_tx_freq", {"tune_request": req, "chan": 0})
            with pytest.raises(RemoteRFPolicyError):
                schema.invoke_native("set_rx_freq", {"tune_request": req, "chan": 0})

        native_tr = _FakeTuneRequest(915e6)
        native_tr.args = _FakeDeviceAddr("mode_n=integer")
        assert _extract_tune_bounds(native_tr, side="tx") is None
        with pytest.raises(RemoteRFPolicyError):
            schema.invoke_native("set_tx_freq", {"tune_request": native_tr, "chan": 0})

        # Empty args (normal v2 TuneRequest snapshot) remain allowed.
        for ok_req in (
            {"__uhd_type__": "TuneRequest", "target_freq": 915e6, "args": ""},
            {"__uhd_type__": "TuneRequest", "target_freq": 915e6, "args": {}},
            {"__uhd_type__": "TuneRequest", "target_freq": 915e6, "args": {"__uhd_type__": "DeviceAddr", "items": {}}},
        ):
            schema.call_set_tx_freq(ok_req, 0)
            schema.call_set_rx_freq(ok_req, 0)

        # In-band streaming works on USRPs whose LO cannot be read (e.g. N210/WBX/SBX) (N1).
        stream_args = {"__uhd_type__": "StreamArgs", "cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}
        tx_stream = schema.call_get_tx_stream(stream_args)
        schema.call_stream_send({"handle": tx_stream["handle"], "samples": np.zeros(4, dtype=np.complex64)})
        assert dev.tx_streamer.sent == 1
        assert schema.invoke_native("get_tx_stream", {"stream_args": stream_args}) is dev.tx_streamer
        schema.check_tx_channels([0])
        schema.check_rx_channels([0])
        rx_stream = schema.call_get_rx_stream(stream_args)
        schema.call_stream_recv({"handle": rx_stream["handle"], "recv_buffer": np.zeros(4, dtype=np.complex64)})
        schema.invoke_native(
            "issue_stream_cmd",
            {"rate": {"__uhd_type__": "StreamCMD", "mode": "start_cont", "stream_now": True}, "chan": 0},
        )
        assert dev.last_cmd[1] == 0


# ── Round-3 regression tests (R5, N1, Hunt N1, Hunt N2) ───────────────


def test_usrp_stream_cmd_with_stream_mode_only_attribute():
    """R5: real UHD StreamCMD exposes .stream_mode (not .mode) on v1 and v2 issue_stream_cmd."""

    class _StreamModeOnlyCMD:
        def __init__(self, mode):
            self.stream_mode = mode
            self.stream_now = True
            self.time_spec = None
            self.num_samps = 0

    class _CmdUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.root_cmds = []
            self.rx_streamer.commands = []
            self.rx_streamer.issue_stream_cmd = self.rx_streamer.commands.append

        def issue_stream_cmd(self, rate, chan=(1 << 64) - 1):
            self.root_cmds.append((rate, chan))

    fake_uhd = _fake_uhd_module()
    fake_uhd.types.StreamCMD = _StreamModeOnlyCMD
    dev = _CmdUSRP()
    dev.rx_freqs[0] = 2.45e9  # out of rx_allow [902, 928]
    schema = UsrpSchema()
    schema.bind(dev)
    schema.configure_lockouts(parse_freq_limits({"rx_allow": [[902e6, 928e6]]}))

    stop_payload = {"__uhd_type__": "StreamCMD", "mode": "stop_cont", "stream_now": True}
    start_payload = {"__uhd_type__": "StreamCMD", "mode": "start_cont", "stream_now": True}

    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # v2 MultiUSRP.issue_stream_cmd: stop_cont allowed out of band, start_cont refused.
        schema.invoke_native("issue_stream_cmd", {"rate": stop_payload, "chan": 0})
        assert len(dev.root_cmds) == 1
        assert not hasattr(dev.root_cmds[0][0], "mode")
        assert dev.root_cmds[0][0].stream_mode == "stop_cont"
        with pytest.raises(RemoteRFPolicyError, match=r"2450 MHz"):
            schema.invoke_native("issue_stream_cmd", {"rate": start_payload, "chan": 0})
        assert len(dev.root_cmds) == 1

        # v1 RXStreamer.issue_stream_cmd: stop_cont allowed out of band, start_cont refused.
        rx_stream = schema.call_get_rx_stream({"cpu_format": "fc32", "otw_format": "sc16", "channels": [0]})
        schema.call_stream_issue_stream_cmd({"handle": rx_stream["handle"], "stream_cmd": stop_payload})
        assert len(dev.rx_streamer.commands) == 1
        assert not hasattr(dev.rx_streamer.commands[0], "mode")
        with pytest.raises(RemoteRFPolicyError, match=r"2450 MHz"):
            schema.call_stream_issue_stream_cmd({"handle": rx_stream["handle"], "stream_cmd": start_payload})
        assert len(dev.rx_streamer.commands) == 1


def test_usrp_unreadable_lo_streams_in_band_and_single_named_lo_fallback():
    """N1: unreadable LO allows in-band streaming; single named LO stage ('lo1') is read when 'all' fails."""

    class _N210WbxUSRP(_FakeUSRP):
        def get_mboard_name(self, mboard=0):
            return "N210r4"

        def get_tx_num_channels(self):
            return 1

        def get_rx_num_channels(self):
            return 1

        def get_tx_lo_freq(self, name="all", chan=0):
            raise RuntimeError("lookup_error: LO readback unsupported on WBX")

        def get_rx_lo_freq(self, name="all", chan=0):
            raise RuntimeError("lookup_error: LO readback unsupported on WBX")

    fake_uhd = _fake_uhd_module()
    n210 = _N210WbxUSRP()
    schema = UsrpSchema()
    schema.bind(n210)
    schema.configure_lockouts(
        parse_freq_limits({"tx_allow": [[902e6, 928e6]], "rx_allow": [[902e6, 928e6]]})
    )
    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # In-band tune, rate change, TX streaming, and RX streaming all succeed without LO readback.
        schema.call_set_tx_freq(915e6, 0)
        schema.call_set_tx_rate(2e6, 0)
        tx_stream = schema.call_get_tx_stream({"cpu_format": "fc32", "otw_format": "sc16", "channels": [0]})
        schema.call_stream_send({"handle": tx_stream["handle"], "samples": np.zeros(8, dtype=np.complex64)})
        assert n210.tx_streamer.sent == 1

        schema.call_set_rx_freq(915e6, 0)
        rx_stream = schema.call_get_rx_stream({"cpu_format": "fc32", "otw_format": "sc16", "channels": [0]})
        schema.call_stream_recv({"handle": rx_stream["handle"], "recv_buffer": np.zeros(8, dtype=np.complex64)})
        assert n210.rx_streamer.recvd == 1

        # Out-of-band live frequency is still refused.
        n210.tx_freqs[0] = 2.45e9
        with pytest.raises(RemoteRFPolicyError, match=r"2449–2451 MHz"):
            schema.call_stream_send({"handle": tx_stream["handle"], "samples": np.zeros(8, dtype=np.complex64)})

    # RFNoC radio whose LO stage is named 'lo1' (not 'all') and has no legacy /mboards/.../dboards/... tree path.
    class _RfnocSingleStageLoUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_lo1 = 915e6
            self.rx_lo1 = 915e6

        def get_mboard_name(self, mboard=0):
            return "X310"

        def get_tx_lo_names(self, chan=0):
            return ["lo1"]

        def get_rx_lo_names(self, chan=0):
            return ["lo1"]

        def get_tx_lo_freq(self, name="all", chan=0):
            if name != "lo1":
                raise RuntimeError("lookup_error: 'all' not supported")
            return self.tx_lo1

        def get_rx_lo_freq(self, name="all", chan=0):
            if name != "lo1":
                raise RuntimeError("lookup_error: 'all' not supported")
            return self.rx_lo1

    rfnoc = _RfnocSingleStageLoUSRP()
    rfnoc_schema = UsrpSchema()
    rfnoc_schema.bind(rfnoc)
    rfnoc_schema.configure_lockouts(
        parse_freq_limits({"tx_allow": [[902e6, 928e6]], "rx_allow": [[902e6, 928e6]]})
    )
    assert rfnoc_schema._read_device_lo_freq("tx", 0) == 915e6
    assert rfnoc_schema._read_device_lo_freq("rx", 0) == 915e6
    rfnoc_schema.check_tx_channels([0])
    rfnoc_schema.check_rx_channels([0])

    # Out-of-band 'lo1' LO is detected via the single-stage fallback even when center freq is in-band.
    rfnoc.tx_lo1 = 935e6
    with pytest.raises(RemoteRFPolicyError, match=r"935 MHz"):
        rfnoc_schema.check_tx_channels([0])
    rfnoc.rx_lo1 = 935e6
    with pytest.raises(RemoteRFPolicyError, match=r"935 MHz"):
        rfnoc_schema.check_rx_channels([0])


def test_usrp_set_tx_rate_without_open_handle_and_master_clock_rate_refused():
    """Hunt N1: set_tx_rate checks live band even after closing TX handles; set_master_clock_rate refused under TX limits."""

    class _FifoClockUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_rates = {0: 1e6, 1: 1e6}
            self.mcr_calls = []

        def get_tx_num_channels(self):
            return 2

        def get_mboard_name(self, mboard=0):
            return "B210"

        def get_tx_lo_freq(self, name="all", chan=0):
            return self.tx_freqs[int(chan)]

        def set_master_clock_rate(self, rate, mboard=None):
            self.mcr_calls.append((rate, mboard))

    fake_uhd = _fake_uhd_module()
    dev = _FifoClockUSRP()
    schema = UsrpSchema()
    schema.bind(dev)
    schema.configure_lockouts(
        parse_freq_limits({"tx_allow": [[902e6, 928e6]], "admin_group_override": True})
    )

    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # Probe p1_fifo_rate sequence: tune near band edge, send, close streamer, then widen rate.
        schema.call_set_tx_rate(1e6, 0)
        schema.call_set_tx_freq(927.5e6, 0)
        st = schema.call_get_tx_stream({"cpu_format": "fc32", "otw_format": "sc16", "channels": [0]})
        schema.call_stream_send({"handle": st["handle"], "samples": np.zeros(8, dtype=np.complex64)})
        schema.call_stream_close(st["handle"])

        # After stream_close (no open TX handle), widening rate must still be refused on both v1 and v2.
        with pytest.raises(RemoteRFPolicyError, match=r"907\.5–947\.5 MHz"):
            schema.call_set_tx_rate(40e6, 0)
        with pytest.raises(RemoteRFPolicyError, match=r"907\.5–947\.5 MHz"):
            schema.invoke_native("set_tx_rate", {"rate": 40e6, "chan": 0})
        assert dev.tx_rates[0] == 1e6

        # In-band rate change without open handle remains allowed.
        schema.call_set_tx_rate(0.5e6, 0)
        assert dev.tx_rates[0] == 0.5e6

        # set_master_clock_rate is refused while TX limits are active (v1 and v2).
        with pytest.raises(RemoteRFPolicyError, match=r"[Mm]aster clock rate"):
            schema.call_set_master_clock_rate(192e6)
        with pytest.raises(RemoteRFPolicyError, match=r"[Mm]aster clock rate"):
            schema.invoke_native("set_master_clock_rate", {"rate": 192e6})
        assert dev.mcr_calls == []

        # Admin bypass allows set_master_clock_rate when enabled.
        schema.lockout_bypass = True
        schema.call_set_master_clock_rate(32e6, 0)
        schema.invoke_native("set_master_clock_rate", {"rate": 32e6})
        assert len(dev.mcr_calls) == 2

        # RX-only limits or no limits allow set_master_clock_rate and unchecked set_tx_rate.
        schema.lockout_bypass = False
        schema.configure_lockouts(parse_freq_limits({"rx_allow": [[902e6, 928e6]]}))
        schema.call_set_master_clock_rate(16e6)
        schema.invoke_native("set_tx_rate", {"rate": 40e6, "chan": 0})
        assert dev.tx_rates[0] == 40e6
        assert len(dev.mcr_calls) == 3


def test_usrp_fresh_session_rate_and_mcr_allowed_until_tx_in_flight():
    """Fresh USRP with out-of-band default LO and wide default rate can configure TX before streaming, then enforces rate/MCR."""

    class _WideBootUSRP(_FakeUSRP):
        def __init__(self):
            super().__init__()
            self.tx_freqs = {0: 2.45e9, 1: 2.45e9}
            self.tx_rates = {0: 200e6, 1: 200e6}
            self.mcr_calls = []

        def get_mboard_name(self, mboard=0):
            return "X310"

        def get_tx_num_channels(self):
            return 2

        def get_tx_lo_freq(self, name="all", chan=0):
            return self.tx_freqs[int(chan)]

        def set_master_clock_rate(self, rate, mboard=None):
            self.mcr_calls.append((rate, mboard))

    fake_uhd = _fake_uhd_module()
    limits = parse_freq_limits({"tx_allow": [[902e6, 928e6]], "admin_group_override": True})
    stream_args = {"cpu_format": "fc32", "otw_format": "sc16", "channels": [0]}

    # 1. v1 path: fresh schema with OOB LO (2.45 GHz) and wide default rate (200 MS/s).
    dev_v1 = _WideBootUSRP()
    s_v1 = UsrpSchema()
    s_v1.bind(dev_v1)
    s_v1.configure_lockouts(limits)

    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        # Tuning before lowering rate is refused (wide band spills out of tx_allow);
        # premature get_tx_stream is also refused and must not mark TX in flight.
        with pytest.raises(RemoteRFPolicyError):
            s_v1.call_set_tx_freq(915e6, 0)
        with pytest.raises(RemoteRFPolicyError):
            s_v1.call_get_tx_stream(stream_args)

        # Before TX starts, set_master_clock_rate and set_tx_rate succeed without band check.
        s_v1.call_set_master_clock_rate(184.32e6)
        assert dev_v1.mcr_calls == [(184.32e6, None)]
        s_v1.call_set_tx_rate(1e6, 0)
        assert dev_v1.tx_rates[0] == 1e6

        # Now set_tx_freq(915e6), get_tx_stream, and send succeed.
        s_v1.call_set_tx_freq(915e6, 0)
        st = s_v1.call_get_tx_stream(stream_args)
        s_v1.call_stream_send({"handle": st["handle"], "samples": np.zeros(8, dtype=np.complex64)})
        assert dev_v1.tx_streamer.sent == 1

        # After closing the TX streamer (and even close_all_streams), TX remains in-flight
        # for the rest of the reservation: out-of-band rate widening and MCR are refused.
        s_v1.call_stream_close(st["handle"])
        s_v1.call_close_all_streams()
        with pytest.raises(RemoteRFPolicyError, match=r"895–935 MHz"):
            s_v1.call_set_tx_rate(40e6, 0)
        assert dev_v1.tx_rates[0] == 1e6
        with pytest.raises(RemoteRFPolicyError, match=r"[Mm]aster clock rate"):
            s_v1.call_set_master_clock_rate(200e6)
        assert len(dev_v1.mcr_calls) == 1
        # In-band rate adjustment stays allowed.
        s_v1.call_set_tx_rate(2e6, 0)
        assert dev_v1.tx_rates[0] == 2e6

        # reset_for_next_holder clears TX in-flight state for the next reservation.
        s_v1.reset_for_next_holder()
        dev_v1.tx_freqs[0] = 2.45e9
        dev_v1.tx_rates[0] = 200e6
        s_v1.call_set_master_clock_rate(184.32e6)
        s_v1.call_set_tx_rate(1e6, 0)
        assert dev_v1.tx_rates[0] == 1e6
        assert len(dev_v1.mcr_calls) == 2

    # 2. v2 invoke_native path: same fresh-session sequence + send_waveform marking TX in flight.
    dev_v2 = _WideBootUSRP()
    s_v2 = UsrpSchema()
    s_v2.bind(dev_v2)
    s_v2.configure_lockouts(limits)

    with patch("remoterf_device_core.drivers.usrp.usrp_schema.uhd", fake_uhd):
        with pytest.raises(RemoteRFPolicyError):
            s_v2.invoke_native("set_tx_freq", {"tune_request": 915e6, "chan": 0})
        with pytest.raises(RemoteRFPolicyError):
            s_v2.invoke_native("get_tx_stream", {"stream_args": stream_args})

        s_v2.invoke_native("set_master_clock_rate", {"rate": 184.32e6})
        s_v2.invoke_native("set_tx_rate", {"rate": 1e6, "chan": 0})
        s_v2.invoke_native("set_tx_freq", {"tune_request": 915e6, "chan": 0})
        assert s_v2.invoke_native("get_tx_stream", {"stream_args": stream_args}) is dev_v2.tx_streamer

        with pytest.raises(RemoteRFPolicyError, match=r"895–935 MHz"):
            s_v2.invoke_native("set_tx_rate", {"rate": 40e6, "chan": 0})
        with pytest.raises(RemoteRFPolicyError, match=r"[Mm]aster clock rate"):
            s_v2.invoke_native("set_master_clock_rate", {"rate": 200e6})

        # close_native_device clears TX in-flight state.
        s_v2.close_native_device()
        dev_v2.tx_freqs[0] = 2.45e9
        dev_v2.tx_rates[0] = 200e6
        s_v2.bind(dev_v2)
        s_v2.invoke_native("set_master_clock_rate", {"rate": 184.32e6})
        s_v2.invoke_native("set_tx_rate", {"rate": 1e6, "chan": 0})
        assert dev_v2.tx_rates[0] == 1e6

        # Running send_waveform (under admin bypass) also marks TX in flight once bypass ends.
        s_v2.lockout_bypass = True
        s_v2.invoke_native(
            "send_waveform",
            {"waveform_proto": np.zeros(8, dtype=np.complex64), "duration": 0.1, "freq": 915e6, "rate": 1e6, "channels": [0]},
        )
        s_v2.lockout_bypass = False
        dev_v2.tx_freqs[0] = 915e6
        with pytest.raises(RemoteRFPolicyError, match=r"895–935 MHz"):
            s_v2.invoke_native("set_tx_rate", {"rate": 40e6, "chan": 0})
        with pytest.raises(RemoteRFPolicyError, match=r"[Mm]aster clock rate"):
            s_v2.invoke_native("set_master_clock_rate", {"rate": 200e6})


def test_usrp_send_waveform_refused_while_tx_limits_active():
    """Hunt N2: send_waveform is refused while TX limits are active because UHD coerces rate/LO without readback."""
    dev = _FakeUSRP()
    schema = UsrpSchema()
    schema.bind(dev)
    schema.configure_lockouts(
        parse_freq_limits({"tx_allow": [[902e6, 928e6]], "admin_group_override": True})
    )
    proto = np.zeros(8, dtype=np.complex64)

    # Probe p2_send_waveform_coerce call (and even nominal in-band call) refused under TX limits.
    for freq, rate in ((902.05e6, 1e3), (915e6, 1e6)):
        with pytest.raises(RemoteRFPolicyError, match=r"send_waveform"):
            schema.invoke_native(
                "send_waveform",
                {"waveform_proto": proto, "duration": 0.1, "freq": freq, "rate": rate, "channels": [0]},
            )
    assert dev.waveforms_sent == 0

    # Admin bypass allows send_waveform when enabled.
    schema.lockout_bypass = True
    schema.invoke_native(
        "send_waveform",
        {"waveform_proto": proto, "duration": 0.1, "freq": 915e6, "rate": 1e6, "channels": [0]},
    )
    assert dev.waveforms_sent == 1

    # Without TX limits (RX-only or None), send_waveform works normally.
    schema.lockout_bypass = False
    schema.configure_lockouts(parse_freq_limits({"rx_allow": [[902e6, 928e6]]}))
    schema.invoke_native(
        "send_waveform",
        {"waveform_proto": proto, "duration": 0.1, "freq": 915e6, "rate": 1e6, "channels": [0]},
    )
    schema.configure_lockouts(None)
    schema.invoke_native(
        "send_waveform",
        {"waveform_proto": proto, "duration": 0.1, "freq": 915e6, "rate": 1e6, "channels": [0]},
    )
    assert dev.waveforms_sent == 3


def test_pluto_park_prefers_ad9363_bounds_and_tolerates_rx_park_oserror():
    """R6: Pluto prefers AD9363 bounds (325 MHz-3.8 GHz) for park frequencies and tolerates rx_lo park OSError."""

    class _AD9363Pluto(_FakePluto):
        def __init__(self):
            super().__init__()
            self._rx_lo = 2_450_000_000
            self._tx_lo = 2_450_000_000
            self.rx_calls = 0

        @property
        def rx_lo(self):
            return self._rx_lo

        @rx_lo.setter
        def rx_lo(self, value):
            if not 325_000_000 <= int(value) <= 3_800_000_000:
                raise OSError(22, "Invalid argument")
            self._rx_lo = int(value)

        @property
        def tx_lo(self):
            return self._tx_lo

        @tx_lo.setter
        def tx_lo(self, value):
            if not 325_000_000 <= int(value) <= 3_800_000_000:
                raise OSError(22, "Invalid argument")
            self._tx_lo = int(value)

        def rx(self):
            self.rx_calls += 1
            return np.zeros(8, dtype=np.complex64)

        def rx_destroy_buffer(self):
            pass

    # Deny-only configs choose AD9363-safe default park frequencies instead of 285 MHz / 4.25 GHz.
    cfg1 = parse_lockouts({"tx_deny": [[500e6, 950e6]], "rx_deny": [[500e6, 950e6]]})
    assert cfg1 is not None and 325e6 <= cfg1["park_tx_lo"] <= 3.8e9
    cfg2 = parse_lockouts({"tx_deny": [[0, 2.5e9]], "rx_deny": [[0, 2.5e9]]})
    assert cfg2 is not None and 325e6 <= cfg2["park_tx_lo"] <= 3.8e9

    for cfg in (cfg1, cfg2):
        radio = _AD9363Pluto()
        schema = PlutoSchema()
        schema.bind(radio)
        schema.configure_lockouts(cfg)
        assert not schema._broken
        assert 325_000_000 <= radio.tx_lo <= 3_800_000_000
        schema.reset_for_next_holder()
        assert not schema._broken
        assert 325_000_000 <= radio.rx_lo <= 3_800_000_000

    # If rx_deny covers all of AD9363 (325 MHz-3.8 GHz), rx_lo park OSError leaves rx_lo unparked
    # instead of marking the device broken, while call_rx still refuses.
    radio3 = _AD9363Pluto()
    schema3 = PlutoSchema()
    schema3.bind(radio3)
    schema3.configure_lockouts(parse_lockouts({"rx_deny": [[300e6, 4.0e9]]}))
    assert not schema3._broken
    assert radio3.rx_lo == 2_450_000_000
    with pytest.raises(RemoteRFPolicyError, match=r"300–4000 MHz"):
        schema3.dispatch("call_rx", {})
    assert radio3.rx_calls == 0
    schema3.reset_for_next_holder()
    assert not schema3._broken


def test_hackrf_hardware_bounds_rejected_before_policy_and_device_write():
    """F3: HackRF rejects <=0 or >7.25 GHz frequencies and out-of-range sample rate/bandwidth before policy & write."""
    import ctypes

    class _WrappingHackRF(_FakeHackRF):
        def __init__(self):
            super().__init__()
            self.writes = []
            self.hw_freq = self.center_freq

        def __setattr__(self, name, value):
            if name in ("center_freq", "sample_rate", "filter_bandwidth") and "writes" in self.__dict__:
                self.writes.append((name, value))
                if name == "center_freq":
                    object.__setattr__(self, "hw_freq", ctypes.c_uint64(int(value)).value)
            super().__setattr__(name, value)

    dev = _WrappingHackRF()
    schema = HackRFSchema()
    schema.bind(dev)
    schema.configure_lockouts(parse_freq_limits({"tx_deny": [[915e6, 916e6]]}))
    dev.writes.clear()

    for bad_freq in (
        0,
        -100_000_000,
        0.5,
        7_250_000_001,
        2**64 + 915_500_000,
        float(2**64 + 915.5e6),
        str(2**64 + 915_500_000),
    ):
        with pytest.raises(RemoteRFPolicyError, match=r"7250 MHz"):
            schema.set_center_freq(bad_freq)
        with pytest.raises(RemoteRFPolicyError, match=r"7250 MHz"):
            schema.call_set_freq(bad_freq)

    for bad_rate in (0, -2_000_000, 20_000_001, 2**64 + 2_000_000):
        with pytest.raises(RemoteRFPolicyError, match=r"20 MHz"):
            schema.set_sample_rate(bad_rate)
        with pytest.raises(RemoteRFPolicyError, match=r"20 MHz"):
            schema.call_set_sample_rate(bad_rate)

    for bad_bw in (0, -1_750_000, 28_000_001, 2**64 + 2_500_000):
        with pytest.raises(RemoteRFPolicyError, match=r"28 MHz"):
            schema.set_filter_bandwidth(bad_bw)

    assert dev.writes == []
    assert dev.hw_freq == 915_000_000

    # Exact hardware max (7.25 GHz) is allowed when policy permits it.
    schema.set_center_freq(7_250_000_000)
    assert dev.center_freq == 7_250_000_000

    # Out-of-range live device values are also refused at start_tx and read_samples.
    schema.configure_lockouts(parse_freq_limits({"tx_deny": [[915e6, 916e6]], "rx_deny": [[915e6, 916e6]]}))
    object.__setattr__(dev, "center_freq", 2**64 + 915_500_000)
    with pytest.raises(RemoteRFPolicyError, match=r"HackRF hardware limits"):
        schema.call_start_tx()
    with pytest.raises(RemoteRFPolicyError, match=r"HackRF hardware limits"):
        schema.call_read_samples(4)


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
