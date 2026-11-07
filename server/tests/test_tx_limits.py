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

import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.modules.setdefault("adi", types.ModuleType("adi"))

from remoteRF_server.common.utils.process_arg import map_arg, unmap_arg
from remoteRF_server.server import device_manager as dm
from remoteRF_server.server import rpc_manager as rm
from remoteRF_server.server.rpc_manager import RpcManager
from remoteRF_server.server.tx_limits import TxFreqRule, parse_limits, parse_tx_freq
from remoteRF_server.server.v2_errors import RemoteRFPolicyError

ISM = [[2.40e9, 2.48e9], [902e6, 928e6]]
BLOCK = [[2.45e9, 2.46e9]]
MESSAGE = (
    "Transmitting at 2.455 GHz is not allowed by this device's limits. "
    "Whitelist: 2.40–2.48 GHz, 902–928 MHz. Blacklist: 2.45–2.46 GHz."
)


class RangeCheckTests(unittest.TestCase):
    def test_whitelist_only(self):
        rule = parse_tx_freq({"whitelist": ISM})
        self.assertTrue(rule.allows(2.42e9))
        self.assertTrue(rule.allows(915e6))
        self.assertFalse(rule.allows(1e9))

    def test_blacklist_only(self):
        rule = parse_tx_freq({"blacklist": BLOCK})
        self.assertTrue(rule.allows(5.8e9))
        self.assertFalse(rule.allows(2.455e9))

    def test_blacklist_wins_and_message_lists_both(self):
        rule = parse_tx_freq({"whitelist": ISM, "blacklist": BLOCK})
        self.assertFalse(rule.allows(2.455e9))
        with self.assertRaises(ValueError) as cm:
            rule.check(2.455e9)
        self.assertEqual(str(cm.exception), MESSAGE)

    def test_edges_are_inclusive(self):
        rule = parse_tx_freq({"whitelist": ISM, "blacklist": BLOCK})
        for f in (2.40e9, 2.48e9, 902e6, 928e6):
            self.assertTrue(rule.allows(f), f)
        for f in (2.45e9, 2.46e9, 2.3999e9, 928.001e6):
            self.assertFalse(rule.allows(f), f)

    def test_no_lists_allows_everything(self):
        self.assertTrue(TxFreqRule().allows(6e9))
        self.assertEqual(parse_limits(None, gid=1), {})

    def test_message_shows_only_the_lists_that_are_set(self):
        rule = parse_tx_freq({"whitelist": [[902e6, 928e6]]})
        self.assertEqual(
            rule.message(1e9),
            "Transmitting at 1.00 GHz is not allowed by this device's limits. Whitelist: 902–928 MHz.",
        )

    def test_default_mode_is_refused(self):
        with self.assertRaises(ValueError):
            parse_tx_freq({"whitelist": ISM, "on_violation": "default"})


class ConfigParsingTests(unittest.TestCase):
    def parse(self, tx_freq):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            limits = parse_limits({"tx_freq": tx_freq}, gid=3)
        return limits.get("tx_freq"), out.getvalue()

    def test_invalid_rules_disable_tx(self):
        for bad in (
            "nope",
            {"whitelist": [[2.48e9, 2.40e9]]},
            {"whitelist": [[2.4e9]]},
            {"blacklist": ["x"]},
            {"whitelist": 5},
            {"on_violation": "ignore"},
            {"whitelist": ISM, "on_violation": "default"},
            {"whitelist": ISM, "on_violation": "default", "default": 1e9},
            {"blacklist": BLOCK, "on_violation": "default", "default": 2.455e9},
        ):
            rule, log = self.parse(bad)
            with self.assertRaises(ValueError):
                rule.check(2.42e9)
            self.assertIn("TX disabled for device gid=3", log)

    def test_limits_not_a_mapping_is_skipped(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(parse_limits([1], gid=3)["tx_freq"].error)
        self.assertIn("gid=3", out.getvalue())

    def test_devices_yaml_limits_reach_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "devices.yml").write_text(yaml.safe_dump({"devices": [
                {"device_id": 1, "device_type": "adalm_pluto", "limits": {"tx_freq": {"whitelist": ISM}}},
                {"device_id": 2, "device_type": "adalm_pluto"},
            ]}))
            with mock.patch.dict(os.environ, {"REMOTERF_CONFIG_DIR": tmp}):
                recs = dm._load_device_records()
        self.assertEqual(recs[1]["LIMITS"]["tx_freq"].whitelist, ((2.40e9, 2.48e9), (902e6, 928e6)))
        self.assertEqual(recs[2]["LIMITS"], {})


class FakeSchema:
    device_type = "adalm_pluto"

    def __init__(self):
        self.calls = []
        self.device = types.SimpleNamespace(center_freq=2.455e9, _remoterf_tx_active=False)

    def dispatch(self, method_name, args):
        self.calls.append((method_name, args))
        if method_name == "get_center_freq":
            return self.device.center_freq
        return None


class RpcManagerLimitTests(unittest.TestCase):
    def run_set(self, rule, value, *, dtype="adalm_pluto", function_name="adalm_pluto:tx_lo:SET", key="tx_lo"):
        dev = FakeSchema()
        dev.device_type = dtype
        with mock.patch.object(rm, "get_tx_freq_limit", return_value=rule):
            out = RpcManager()._dispatch_acquired(
                function_name=function_name, args={key: map_arg(value)}, gid=1, dev=dev
            )
        return out, dev

    def test_allowed_set_reaches_dispatch(self):
        out, dev = self.run_set(parse_tx_freq({"whitelist": ISM}), 2.42e9)
        self.assertEqual(dev.calls, [("set_tx_lo", {"value": 2.42e9})])
        self.assertNotIn("warning", out)

    def test_disallowed_set_raises_and_never_dispatches(self):
        dev = FakeSchema()
        rule = parse_tx_freq({"whitelist": ISM, "blacklist": BLOCK})
        with mock.patch.object(rm, "get_tx_freq_limit", return_value=rule):
            with self.assertRaises(ValueError) as cm:
                RpcManager()._dispatch_acquired(
                    function_name="adalm_pluto:tx_lo:SET", args={"tx_lo": map_arg(2.455e9)}, gid=1, dev=dev
                )
        self.assertEqual(str(cm.exception), MESSAGE)
        self.assertEqual(dev.calls, [])

    def test_disallowed_set_is_an_error_reply_through_the_token_path(self):
        dev = FakeSchema()

        @contextlib.contextmanager
        def acquire(_token):
            yield 1, dev

        rule = parse_tx_freq({"whitelist": ISM, "blacklist": BLOCK})
        with mock.patch.object(rm, "get_tx_freq_limit", return_value=rule), \
                mock.patch.object(rm, "acquire_device", acquire), \
                mock.patch.object(RpcManager, "_federated_route", return_value=None):
            out = RpcManager().run_rpc(
                function_name="adalm_pluto:tx_lo:SET", args={"a": map_arg("tok"), "tx_lo": map_arg(2.455e9)}
            )
        self.assertEqual(unmap_arg(out["a"]), MESSAGE)
        self.assertEqual(dev.calls, [])

    def test_other_properties_and_unlimited_devices_are_untouched(self):
        rule = parse_tx_freq({"whitelist": ISM})
        out, dev = self.run_set(rule, 1e9, function_name="adalm_pluto:rx_lo:SET", key="rx_lo")
        self.assertEqual(dev.calls, [("set_rx_lo", {"value": 1e9})])
        out, dev = self.run_set(None, 1e9)
        self.assertEqual(dev.calls, [("set_tx_lo", {"value": 1e9})])
        with self.assertRaises(ValueError):
            self.run_set(rule, 1e9, dtype="adalm_pluto_mimo_ykush", function_name="adalm_pluto_mimo_ykush:tx_lo:SET")

    def hackrf(self, function_name, args, *, active):
        dev = FakeSchema()
        dev.device_type = "hackrf"
        dev.device._remoterf_tx_active = active
        rule = parse_tx_freq({"whitelist": ISM, "blacklist": BLOCK})
        with mock.patch.object(rm, "get_tx_freq_limit", return_value=rule):
            RpcManager()._dispatch_acquired(function_name=function_name, args=args, gid=1, dev=dev)
        return dev

    def test_hackrf_start_tx_checks_current_center_freq_and_never_defaults(self):
        with self.assertRaisesRegex(ValueError, "2.455 GHz"):
            self.hackrf("hackrf:start_tx:CALL0", {}, active=False)

    def test_hackrf_retune_is_checked_only_while_transmitting(self):
        dev = self.hackrf("hackrf:center_freq:SET", {"center_freq": map_arg(1e9)}, active=False)
        self.assertEqual(dev.calls, [("set_center_freq", {"value": 1e9})])
        with self.assertRaisesRegex(ValueError, "1.00 GHz"):
            self.hackrf("hackrf:center_freq:SET", {"center_freq": map_arg(1e9)}, active=True)
        with self.assertRaisesRegex(ValueError, "1.00 GHz"):
            self.hackrf("hackrf:set_freq:CALL1", {"arg1": map_arg(1e9)}, active=True)
        dev = self.hackrf("hackrf:set_freq:CALL1", {"arg1": map_arg(915e6)}, active=True)
        self.assertEqual(dev.calls, [("call_set_freq", {"value": 915e6})])


class DynamicV2LimitTests(unittest.TestCase):
    def test_disallowed_set_tx_freq_is_refused(self):
        from test_dynamic_v2 import FakeInventorySchema
        from remoteRF_server.drivers.usrp.policy import USRPPolicy
        from remoteRF_server.server.dynamic_v2 import SessionManager

        inventory = FakeInventorySchema()
        context = {"schema": inventory, "dtype": "usrp", "ident": "SERIAL", "name": "test"}
        rule = parse_tx_freq({"whitelist": ISM, "blacklist": BLOCK})
        manager = SessionManager()
        p = "remoteRF_server.server.dynamic_v2."
        with mock.patch(p + "resolve_gid_for_token", return_value=7), \
                mock.patch(p + "get_local_device_context", return_value=context), \
                mock.patch(p + "token_matches_device", return_value=True), \
                mock.patch(p + "USRPPolicy.load", return_value=USRPPolicy({})), \
                mock.patch(p + "get_tx_freq_limit", return_value=rule):
            session = manager.open("token")
            inventory.live.device_type = "usrp"
            with self.assertRaises(RemoteRFPolicyError) as cm:
                manager.invoke(session.session_id, session.root_handle, "set_tx_freq",
                               {"tune_request": 2.455e9}, overload_id="default")
            self.assertEqual(str(cm.exception), MESSAGE)
            with self.assertRaises(RemoteRFPolicyError):
                manager.invoke(session.session_id, session.root_handle, "set_tx_freq",
                               {"tune_request": {"__uhd_type__": "TuneRequest", "target_freq": 1e9}},
                               overload_id="default")
            manager.invoke(session.session_id, session.root_handle, "set_tx_freq",
                           {"tune_request": 2.42e9}, overload_id="default")
        self.assertEqual([c[0] for c in inventory.live.calls], ["set_tx_freq"])
        manager.close(session.session_id)


if __name__ == "__main__":
    unittest.main()
