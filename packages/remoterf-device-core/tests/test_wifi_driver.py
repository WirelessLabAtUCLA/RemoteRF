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

import time
import unittest
from unittest import mock

from remoterf_device_core.drivers.wifi import wifi_schema
from remoterf_device_core.drivers.wifi.rssi import safety, scanner as scanner_mod
from remoterf_device_core.drivers.wifi.rssi.scanner import AccessPoint, RSSIScanner


def make_ap(ssid="lab", bssid="aa:bb:cc:dd:ee:ff", signal=-60.0, channel=6):
    return AccessPoint(bssid=bssid, ssid=ssid, signal_dbm=signal, quality_pct=80, freq_mhz=2437,
                       channel=channel, band="2.4 GHz", security="WPA2", last_seen_ms=0, timestamp=time.time())


def make_schema(aps=(), auto_rescan=True):
    """A schema over a fake wlan0 whose scanner returns aps without running iw/nmcli."""
    with mock.patch.object(wifi_schema, "RSSIScanner") as scanner_cls:
        scanner_cls.return_value.interface = "wlan0"
        device = wifi_schema.WifiDongleDevice(interface="wlan0", auto_rescan=auto_rescan)
    scanner = RSSIScanner.__new__(RSSIScanner)
    scanner.interface = "wlan0"
    scanner._trigger_rescan = mock.Mock()
    scanner._parse_iw_dump = mock.Mock(return_value=list(aps))
    scanner._parse_nmcli_fallback = mock.Mock(return_value=[])
    device.scanner = scanner
    schema = wifi_schema.WifiDongleSchema()
    schema.device = device
    return schema


class BurstTests(unittest.TestCase):
    def test_absent_target_returns_within_the_cap(self):
        schema = make_schema()
        clock = [1000.0]
        # Each scan "takes" 5 s (bounded by its timeout), so the loop must stop at the cap.
        schema.device.scanner._parse_iw_dump.side_effect = (
            lambda timeout=10.0: clock.__setitem__(0, clock[0] + min(5.0, timeout)) or []
        )
        with mock.patch.object(wifi_schema.time, "time", lambda: clock[0]), \
             mock.patch.object(wifi_schema.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s)):
            result = schema.call_get_rssi_burst("missing", max_time=1e9)
        self.assertEqual(result["samples_collected"], 0)
        self.assertLessEqual(wifi_schema.MAX_BURST_SECONDS, 20.0)
        self.assertLessEqual(result["elapsed_seconds"], wifi_schema.MAX_BURST_SECONDS)
        self.assertIsNone(result["linear_avg_dbm"])

    def test_burst_cap_bounds_subprocess_timeouts_within_20s(self):
        schema = make_schema()
        scanner = RSSIScanner.__new__(RSSIScanner)
        scanner.interface = "wlan0"
        schema.device.scanner = scanner
        clock = [1000.0]
        seen = []

        def fake_run(cmd, **kw):
            seen.append((cmd[0], kw["timeout"]))
            clock[0] += min(8.0, kw["timeout"])

        def fake_check_output(cmd, **kw):
            seen.append((cmd[0], kw["timeout"]))
            clock[0] += min(7.0, kw["timeout"])
            return ""

        with mock.patch.object(wifi_schema.time, "time", lambda: clock[0]), \
             mock.patch.object(wifi_schema.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s)), \
             mock.patch.object(scanner_mod.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(scanner_mod.subprocess, "check_output", side_effect=fake_check_output):
            result = schema.call_get_rssi_burst("missing", max_time=30.0, rescan=True)

        self.assertEqual(wifi_schema.MAX_BURST_SECONDS, 20.0)
        self.assertLessEqual(result["elapsed_seconds"], 20.0)
        self.assertEqual([name for name, _ in seen], ["nmcli", "iw", "nmcli"])
        self.assertTrue(all(0 < t <= 10.0 for _, t in seen), seen)
        self.assertAlmostEqual(seen[0][1], 10.0)
        self.assertAlmostEqual(seen[1][1], 10.0)
        self.assertAlmostEqual(seen[2][1], 5.0)

        # Single scan() also stays within the 20 s cap across nmcli + iw + fallback.
        seen.clear()
        clock[0] = 2000.0
        with mock.patch.object(wifi_schema.time, "time", lambda: clock[0]), \
             mock.patch.object(scanner_mod.subprocess, "run", side_effect=lambda cmd, **kw: (seen.append((cmd[0], kw["timeout"])), clock.__setitem__(0, clock[0] + kw["timeout"]))), \
             mock.patch.object(scanner_mod.subprocess, "check_output", side_effect=lambda cmd, **kw: (seen.append((cmd[0], kw["timeout"])), clock.__setitem__(0, clock[0] + kw["timeout"])) and ""):
            self.assertEqual(schema.call_scan(rescan=True), [])
        self.assertLessEqual(clock[0] - 2000.0, 20.0)
        self.assertEqual([name for name, _ in seen], ["nmcli", "iw"])

    def test_non_positive_max_time_is_rejected(self):
        schema = make_schema()
        for bad in (0, -1, float("nan")):
            with self.assertRaises(ValueError):
                schema.call_get_rssi_burst("lab", max_time=bad)

    def test_rescans_only_before_the_first_sample(self):
        schema = make_schema([make_ap()])
        with mock.patch.object(wifi_schema.time, "sleep"):
            result = schema.call_get_rssi_burst("lab", count=5, rescan=True)
        self.assertEqual(result["samples_collected"], 5)
        self.assertEqual(schema.device.scanner._trigger_rescan.call_count, 1)


class AutoRescanTests(unittest.TestCase):
    def test_auto_rescan_off_skips_the_rescan(self):
        schema = make_schema([make_ap()])
        schema.set_auto_rescan(False)
        schema.call_scan()
        schema.call_get_rssi("lab")
        schema.call_get_ap("lab")
        schema.device.scanner._trigger_rescan.assert_not_called()

    def test_auto_rescan_on_rescans_and_explicit_false_wins(self):
        schema = make_schema([make_ap()])
        schema.call_scan()
        self.assertEqual(schema.device.scanner._trigger_rescan.call_count, 1)
        schema.call_scan(rescan=False)
        self.assertEqual(schema.device.scanner._trigger_rescan.call_count, 1)

    def test_scan_summary_and_channel_distribution_honor_auto_rescan_false(self):
        schema = make_schema([make_ap()], auto_rescan=False)
        summary = schema.call_scan_summary()
        self.assertEqual(summary["total_access_points"], 1)
        schema.device.scanner._trigger_rescan.assert_not_called()

        schema.reset_for_next_holder()
        self.assertEqual(schema.device._last_results, [])
        dist = schema.call_get_channel_distribution()
        self.assertIn("6", dist)
        schema.device.scanner._trigger_rescan.assert_not_called()


class ResetTests(unittest.TestCase):
    def test_reset_for_next_holder_clears_state(self):
        schema = make_schema([make_ap()], auto_rescan=True)
        schema.call_scan()
        schema.set_auto_rescan(False)
        self.assertEqual(schema.get_scan_count(), 1)
        self.assertTrue(schema.call_get_history())

        schema.reset_for_next_holder()
        self.assertEqual(schema.get_scan_count(), 0)
        self.assertEqual(schema.call_get_history(), [])
        self.assertEqual(schema.device._last_results, [])
        self.assertTrue(schema.get_auto_rescan())


class StatsTests(unittest.TestCase):
    def test_empty_stats_use_none(self):
        stats = wifi_schema.compute_linear_stats([])
        self.assertIsNone(stats["mean_dbm"])
        self.assertIsNone(stats["median_dbm"])

    def test_channel_distribution_has_string_keys(self):
        schema = make_schema([make_ap(channel=6), make_ap(bssid="11:22:33:44:55:66", channel=None)])
        self.assertEqual(set(schema.call_get_channel_distribution()), {"0", "6"})


class DefaultRouteTests(unittest.TestCase):
    def route_info(self, output):
        with mock.patch.object(safety.subprocess, "check_output", return_value=output):
            return safety.get_default_route_info()

    def test_interface_names_are_not_cut_short(self):
        info = self.route_info("default via 192.168.1.1 dev br-lan proto dhcp metric 100\n")
        self.assertEqual((info["gateway"], info["interface"]), ("192.168.1.1", "br-lan"))
        info = self.route_info("default via 10.0.0.1 dev eth0.100 proto static\n")
        self.assertEqual(info["interface"], "eth0.100")

    def test_default_without_via(self):
        info = self.route_info("default dev wg0 scope link\n")
        self.assertEqual((info["gateway"], info["interface"]), (None, "wg0"))

    def test_every_default_route_counts(self):
        output = ("default via 10.0.0.1 dev eth0 proto dhcp metric 100\n"
                  "default via 192.168.4.1 dev wlan0 proto dhcp metric 600\n")
        info = self.route_info(output)
        self.assertEqual(info["interface"], "eth0")
        self.assertEqual(info["interfaces"], ["eth0", "wlan0"])
        with mock.patch.object(safety.subprocess, "check_output", return_value=output):
            with self.assertRaises(RuntimeError):
                safety.verify_network_safety("wlan0")
            schema = make_schema()
            self.assertFalse(schema.get_default_route_safe())


if __name__ == "__main__":
    unittest.main()
