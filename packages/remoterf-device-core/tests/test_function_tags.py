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

import json
import subprocess
import sys
import types
import unittest

sys.modules.setdefault("adi", types.ModuleType("adi"))
try:
    import serial as _serial  # noqa: F401
    import serial.tools.list_ports as _serial_list_ports  # noqa: F401
except ImportError:
    _serial_stub = types.ModuleType("serial")
    _serial_stub.Serial = object
    _serial_tools = types.ModuleType("serial.tools")
    _serial_list_ports_stub = types.ModuleType("serial.tools.list_ports")
    _serial_tools.list_ports = _serial_list_ports_stub
    _serial_stub.tools = _serial_tools
    sys.modules.setdefault("serial", _serial_stub)
    sys.modules.setdefault("serial.tools", _serial_tools)
    sys.modules.setdefault("serial.tools.list_ports", _serial_list_ports_stub)

from remoterf_device_core import manifest
from remoterf_device_core.drivers.adalm_pluto.pluto_mimo_schema import PlutoMIMOSchema
from remoterf_device_core.drivers.adalm_pluto.pluto_schema import PlutoSchema
from remoterf_device_core.drivers.hackrf.hackrf_schema import HackRFSchema
from remoterf_device_core.drivers.rfbeam_kmd7.rfbeam_kmd7_schema import RFBeamKMD7Schema
from remoterf_device_core.drivers.ti_awr6843.ti_awr6843_schema import (
    TiMmWaveSchema as TiAwr6843Schema,
)
from remoterf_device_core.drivers.ti_mmwave.ti_mmwave_schema import TiMmWaveSchema
from remoterf_device_core.drivers.usrp.uhd_manifest import (
    TX_INTEGRATION_METHODS,
    TX_MULTIUSRP_METHODS,
    build_usrp_schema_v2,
)
from remoterf_device_core.drivers.usrp.usrp_schema import UsrpSchema
from remoterf_device_core.drivers.usrp_phaser.usrp_phaser_schema import UsrpPhaserSchema
from remoterf_device_core.idl import (
    BLOCK_ALL_TAG,
    DeviceSchema,
    blocked_tag_message,
    find_blocked_tag,
    idl_expose,
    normalize_tags,
    v1_function_label,
    v1_method_name,
)
from remoterf_device_core.idl.v2 import (
    build_schema,
    get_method_tags,
    get_schema_tags,
    method,
    object_type,
    overload,
    parameter,
    validate_schema,
)


class FunctionTagHelperTests(unittest.TestCase):
    def test_normalize_tags_single_multiple_case_insensitive_and_errors(self):
        self.assertEqual(normalize_tags(None), ())
        self.assertEqual(normalize_tags([]), ())
        self.assertEqual(normalize_tags(()), ())
        self.assertEqual(normalize_tags("TX"), ("TX",))
        self.assertEqual(normalize_tags("  TX  "), ("TX",))
        self.assertEqual(normalize_tags(("TX", "Cal", "tx")), ("TX", "Cal"))
        self.assertEqual(normalize_tags(["tx", "TX", "HighPower"]), ("tx", "HighPower"))

        for bad in ("", "   ", ["TX", ""], ["TX", 1], 123, {"TX"}):
            with self.assertRaises((TypeError, ValueError)):
                normalize_tags(bad)

    def test_find_blocked_tag_case_insensitive_and_display_text(self):
        self.assertEqual(find_blocked_tag(("TX",), ["tx"]), "TX")
        self.assertEqual(find_blocked_tag(("Cal", "HighPower"), ["highpower"]), "HighPower")
        self.assertEqual(find_blocked_tag(("Cal", "HighPower"), [BLOCK_ALL_TAG]), "Cal")
        self.assertIsNone(find_blocked_tag((), [BLOCK_ALL_TAG]))
        self.assertIsNone(find_blocked_tag(("RX",), ["TX"]))
        self.assertIsNone(find_blocked_tag((), ["TX"]))
        self.assertIsNone(find_blocked_tag(("TX",), ()))
        self.assertEqual(
            blocked_tag_message("set_tx_lo", "TX"),
            "set_tx_lo is blocked for your user groups on this device (tag TX)",
        )
        self.assertEqual(v1_method_name("tx_lo", "SET"), "set_tx_lo")
        self.assertEqual(v1_method_name("tx_lo", "GET"), "get_tx_lo")
        self.assertEqual(v1_method_name("tx", "CALL1"), "call_tx")
        self.assertEqual(v1_function_label("tx", "CALL1"), "tx")
        self.assertEqual(v1_function_label("tx_lo", "SET"), "set_tx_lo")
        self.assertEqual(v1_function_label("tx_lo", "GET"), "get_tx_lo")


class V1DecoratorTagTests(unittest.TestCase):
    def test_v1_tags_on_get_set_call_keep_schema_hash_and_json_unchanged(self):
        class UntaggedRadio(DeviceSchema):
            device_type = "demo_radio"
            driver_version = "0.1.0"

            @idl_expose(kind="get")
            def get_tx_lo(self) -> int:
                return 0

            @idl_expose(kind="set")
            def set_tx_lo(self, value: int) -> None:
                pass

            @idl_expose(kind="call")
            def call_tx(self, samples) -> None:
                pass

            @idl_expose(kind="call")
            def call_rx(self):
                return []

        class TaggedRadio(DeviceSchema):
            device_type = "demo_radio"
            driver_version = "0.1.0"

            @idl_expose(kind="get", tag="DIAG")
            def get_tx_lo(self) -> int:
                return 0

            @idl_expose(kind="set", tag="TX")
            def set_tx_lo(self, value: int) -> None:
                pass

            @idl_expose(kind="call", tag=("TX", "RF", "tx"))
            def call_tx(self, samples) -> None:
                pass

            @idl_expose(kind="call")
            def call_rx(self):
                return []

        class ChildRadio(TaggedRadio):
            @idl_expose(kind="call", tag=["TX", "Cal"])
            def call_calibrate_tx(self) -> None:
                pass

        untagged = UntaggedRadio()
        tagged = TaggedRadio()
        child = ChildRadio()

        self.assertEqual(tagged.get_idl_hash(), untagged.get_idl_hash())
        self.assertEqual(tagged.get_idl_json(), untagged.get_idl_json())
        self.assertNotIn("tag", tagged.get_idl_json().lower())

        self.assertEqual(tagged.get_method_tags("get_tx_lo"), ("DIAG",))
        self.assertEqual(tagged.get_method_tags("set_tx_lo"), ("TX",))
        self.assertEqual(tagged.get_method_tags("call_tx"), ("TX", "RF"))
        self.assertEqual(tagged.get_method_tags("call_rx"), ())
        self.assertEqual(untagged.get_method_tags("set_tx_lo"), ())
        self.assertEqual(child.get_method_tags("set_tx_lo"), ("TX",))
        self.assertEqual(child.get_method_tags("call_calibrate_tx"), ("TX", "Cal"))

    def test_v1_decorator_rejects_invalid_tag(self):
        with self.assertRaises(ValueError):
            idl_expose(kind="call", tag="  ")
        with self.assertRaises(TypeError):
            idl_expose(kind="call", tag=42)


class V2MethodTagTests(unittest.TestCase):
    def _build(self, *, tagged: bool) -> dict:
        methods = [
            method(
                "set_tx_freq",
                "demo.Radio",
                [overload("default", [parameter("freq", "float")])],
                tag="TX" if tagged else None,
            ),
            method(
                "burst_tx",
                "demo.Radio",
                [overload("default", [])],
                tag=["TX", "HighPower", "tx"] if tagged else None,
            ),
            method(
                "get_rx_freq",
                "demo.Radio",
                [overload("default", [], returns="float")],
            ),
        ]
        return build_schema(
            device_type="demo_v2",
            client_class="DemoV2",
            driver_version="0.1.0",
            native_api={"name": "demo", "target_version": "1.0"},
            hardware_profiles=["default"],
            objects=[
                object_type(
                    "demo.Radio",
                    kind="remote_handle",
                    mutability="remote_handle",
                    methods=["set_tx_freq", "burst_tx", "get_rx_freq"],
                )
            ],
            methods=methods,
            errors=[],
            capability_fields=[],
            coverage={
                "demo.Radio.set_tx_freq": "implemented",
                "demo.Radio.burst_tx": "implemented",
                "demo.Radio.get_rx_freq": "implemented",
            },
        )

    def test_v2_tags_keep_schema_hash_and_json_unchanged(self):
        untagged = self._build(tagged=False)
        tagged = self._build(tagged=True)

        validate_schema(untagged)
        validate_schema(tagged)
        self.assertEqual(tagged["schema_hash"], untagged["schema_hash"])
        tagged_json = json.dumps(tagged, sort_keys=True, separators=(",", ":"))
        untagged_json = json.dumps(untagged, sort_keys=True, separators=(",", ":"))
        self.assertEqual(tagged_json, untagged_json)
        self.assertNotIn("tag", tagged_json.lower())

        self.assertEqual(get_method_tags(tagged, "demo.Radio", "set_tx_freq"), ("TX",))
        self.assertEqual(
            get_method_tags(tagged, "demo.Radio", "burst_tx"),
            ("TX", "HighPower"),
        )
        self.assertEqual(get_method_tags(tagged, "demo.Radio", "get_rx_freq"), ())
        self.assertEqual(get_method_tags(untagged, "demo.Radio", "set_tx_freq"), ())
        self.assertEqual(
            get_schema_tags(tagged),
            {
                ("demo.Radio", "set_tx_freq"): ("TX",),
                ("demo.Radio", "burst_tx"): ("TX", "HighPower"),
            },
        )


class BuiltinDriverTagTests(unittest.TestCase):
    def test_pluto_and_mimo_tx_tags_and_rx_untagged(self):
        pluto = PlutoSchema()
        expected_pluto = {
            "set_loopback",
            "set_tx_hardwaregain_chan0",
            "set_tx_rf_bandwidth",
            "set_tx_lo",
            "set_tx_cyclic_buffer",
            "call_tx",
            "call_tx_destroy_buffer",
            "call_disable_dds",
            "call_dds_single_tone",
            "call_dds_dual_tone",
            "set_dds_frequencies",
            "set_dds_scales",
            "set_dds_phases",
            "set_dds_enabled",
            "set_tx_enabled_channels",
            "set_tx_int8_filter_en",
        }
        actual_pluto = {
            name for name, entry in pluto._exposed.items() if "TX" in entry.tags
        }
        self.assertEqual(actual_pluto, expected_pluto)
        for rx_method in ("call_rx", "get_rx_lo", "set_rx_lo", "set_sample_rate", "call_rx_destroy_buffer"):
            self.assertEqual(pluto.get_method_tags(rx_method), (), rx_method)

        mimo = PlutoMIMOSchema()
        expected_mimo = expected_pluto | {"set_tx_hardwaregain_chan1"}
        actual_mimo = {
            name for name, entry in mimo._exposed.items() if "TX" in entry.tags
        }
        self.assertEqual(actual_mimo, expected_mimo)
        self.assertEqual(mimo.get_method_tags("set_rx_hardwaregain_chan1"), ())

    def test_hackrf_tx_tags_and_rx_untagged(self):
        hackrf = HackRFSchema()
        expected = {"set_txvga_gain", "call_load_tx_iq", "call_start_tx", "call_stop_tx"}
        actual = {name for name, entry in hackrf._exposed.items() if "TX" in entry.tags}
        self.assertEqual(actual, expected)
        for rx_method in ("call_start_rx", "call_stop_rx", "call_read_samples", "set_lna_gain", "set_vga_gain"):
            self.assertEqual(hackrf.get_method_tags(rx_method), (), rx_method)

    def test_usrp_and_usrp_phaser_v1_and_v2_tx_tags(self):
        usrp = UsrpSchema()
        expected_v1 = {
            "call_set_tx_subdev_spec",
            "call_set_tx_antenna",
            "call_set_tx_rate",
            "call_set_tx_freq",
            "call_set_tx_gain",
            "call_get_tx_stream",
            "call_stream_send",
        }
        actual_v1 = {name for name, entry in usrp._exposed.items() if "TX" in entry.tags}
        self.assertEqual(actual_v1, expected_v1)
        for rx_method in ("call_set_rx_freq", "call_get_rx_stream", "call_stream_recv", "call_get_tx_freq"):
            self.assertEqual(usrp.get_method_tags(rx_method), (), rx_method)

        schema_v2 = build_usrp_schema_v2()
        property_owners = (
            "uhd.property.bool",
            "uhd.property.int",
            "uhd.property.double",
            "uhd.property.str",
            "uhd.property.device_addr",
            "uhd.property.dboard_iface",
        )
        expected_v2 = (
            {("uhd.usrp.MultiUSRP", name) for name in TX_MULTIUSRP_METHODS}
            | {("uhd.usrp.MultiUSRP", name) for name in TX_INTEGRATION_METHODS}
            | {("uhd.usrp.TXStreamer", "send")}
            | {(owner, name) for owner in property_owners for name in ("set", "set_coerced")}
        )
        self.assertEqual(set(get_schema_tags(schema_v2)), expected_v2)
        self.assertEqual(get_method_tags(schema_v2, "uhd.usrp.MultiUSRP", "integration_start"), ("TX",))
        self.assertEqual(get_method_tags(schema_v2, "uhd.usrp.MultiUSRP", "integration_call"), ("TX",))
        for safe_int in ("integration_list", "integration_describe", "integration_status", "integration_stop", "integration_fetch"):
            self.assertEqual(get_method_tags(schema_v2, "uhd.usrp.MultiUSRP", safe_int), (), safe_int)
        self.assertEqual(get_method_tags(schema_v2, "uhd.usrp.MultiUSRP", "set_rx_freq"), ())
        self.assertEqual(get_method_tags(schema_v2, "uhd.usrp.MultiUSRP", "get_rx_stream"), ())
        self.assertEqual(get_method_tags(schema_v2, "uhd.usrp.RXStreamer", "recv"), ())

        phaser = UsrpPhaserSchema()
        expected_phaser_v1 = expected_v1 | {"call_phaser_set_tx_switch", "call_phaser_set_tx_path"}
        actual_phaser_v1 = {name for name, entry in phaser._exposed.items() if "TX" in entry.tags}
        self.assertEqual(actual_phaser_v1, expected_phaser_v1)
        self.assertEqual(phaser.get_method_tags("call_phaser_configure_rx"), ())
        self.assertEqual(phaser.get_method_tags("call_phaser_set_rx_beam_angle"), ())

        phaser_v2 = phaser.get_idl_v2()
        expected_phaser_v2 = expected_v2 | {
            ("uhd.usrp.MultiUSRP", "phaser_set_tx_switch"),
            ("uhd.usrp.MultiUSRP", "phaser_set_tx_path"),
        }
        self.assertEqual(set(get_schema_tags(phaser_v2)), expected_phaser_v2)
        self.assertEqual(get_method_tags(phaser_v2, "uhd.usrp.MultiUSRP", "phaser_configure_rx"), ())
        self.assertEqual(get_method_tags(phaser_v2, "uhd.usrp.MultiUSRP", "phaser_set_rx_beam_angle"), ())

    def test_radar_emitters_tx_tags_and_safe_methods_untagged(self):
        mmwave = TiMmWaveSchema()
        expected_mmwave = {"call_send_command", "call_apply_config", "call_start"}
        actual_mmwave = {name for name, entry in mmwave._exposed.items() if "TX" in entry.policy_tags}
        self.assertEqual(actual_mmwave, expected_mmwave)
        for safe in ("call_stop", "call_read_frame", "call_query_version", "call_flush_frames", "get_is_running"):
            self.assertEqual(mmwave.get_method_tags(safe), (), safe)

        awr = TiAwr6843Schema()
        expected_awr = {
            "call_apply_config",
            "call_configure",
            "call_send_command",
            "call_start",
            "set_clutter_removal",
            "set_fps",
            "set_preset",
            "set_mode",
            "set_sensitivity",
            "set_min_range",
            "set_max_range",
        }
        actual_awr = {name for name, entry in awr._exposed.items() if "TX" in entry.policy_tags}
        self.assertEqual(actual_awr, expected_awr)
        for safe in ("call_stop", "call_reset", "call_read_frame", "call_capture_point_cloud", "call_query_version", "get_is_running"):
            self.assertEqual(awr.get_method_tags(safe), (), safe)

        kmd7 = RFBeamKMD7Schema()
        expected_kmd7 = {
            "call_start",
            "call_read_frame",
            "call_capture_point_cloud",
            "call_capture_points",
            "call_capture_targets",
        }
        actual_kmd7 = {name for name, entry in kmd7._exposed.items() if "TX" in entry.policy_tags}
        self.assertEqual(actual_kmd7, expected_kmd7)
        for safe in ("call_stop", "call_flush_frames", "call_configure", "call_restore_factory_settings", "get_is_running"):
            self.assertEqual(kmd7.get_method_tags(safe), (), safe)

    def test_usrp_property_tree_writes_tagged_tx_and_reads_untagged(self):
        property_owners = (
            "uhd.property.bool",
            "uhd.property.int",
            "uhd.property.double",
            "uhd.property.str",
            "uhd.property.device_addr",
            "uhd.property.dboard_iface",
        )
        for schema in (build_usrp_schema_v2(), UsrpPhaserSchema().get_idl_v2()):
            self.assertEqual(get_method_tags(schema, "uhd.usrp.MultiUSRP", "get_tree"), ())
            for tree_fn in (
                "subtree",
                "exists",
                "list",
                "access_bool",
                "access_int",
                "access_double",
                "access_str",
                "access_device_addr",
                "access_dboard_iface",
            ):
                self.assertEqual(get_method_tags(schema, "uhd.property_tree", tree_fn), (), tree_fn)
            for owner in property_owners:
                for write_fn in ("set", "set_coerced"):
                    self.assertEqual(get_method_tags(schema, owner, write_fn), ("TX",), f"{owner}.{write_fn}")
                for read_fn in ("get", "get_desired"):
                    self.assertEqual(get_method_tags(schema, owner, read_fn), (), f"{owner}.{read_fn}")

    def test_ti_awr6843_config_setters_tagged_tx(self):
        awr = TiAwr6843Schema()
        for prop in ("fps", "preset", "mode", "sensitivity", "min_range", "max_range", "clutter_removal"):
            self.assertEqual(awr.get_method_tags(f"set_{prop}"), ("TX",), f"set_{prop}")
            self.assertEqual(awr.get_method_tags(f"get_{prop}"), (), f"get_{prop}")

    def test_schemas_json_is_unchanged(self):
        built = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json; from remoterf_device_core import manifest; print(json.dumps(manifest.build()))",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(built.stdout), manifest.load())


if __name__ == "__main__":
    unittest.main()
