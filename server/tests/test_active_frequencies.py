"""ACC:active_frequencies: reserved Plutos (local and host), anonymous, no hardware reads."""

import json
import sqlite3
import sys
import tempfile
import threading
import types
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.modules.setdefault("adi", types.ModuleType("adi"))

from remoterf_device_core.drivers.adalm_pluto.pluto_mimo_schema import PlutoMIMOSchema
from remoterf_device_core.drivers.adalm_pluto.pluto_schema import PlutoSchema
from remoterf_device_core.drivers.adalm_pluto.poe import PoePluto
from remoteRF_server.common.utils import map_arg, unmap_arg
from remoteRF_server.server import device_manager as dm

from test_device_handles import _Devices, _state

LOCAL_PLUTO = 4400
LOCAL_MIMO = 4401
LOCAL_IDLE = 4402
LOCAL_USRP = 4403
LOCAL_POE = 4404
HOST_PLUTO_A = 4410
HOST_PLUTO_B = 4411
HOST_IDLE = 4412
HOST_USRP = 4413
PARTNER_ALIAS = 1_000_005


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class _FakeRadio:
    def __init__(self):
        self.rx_lo = 915_000_000
        self.tx_lo = 915_000_000
        self.sample_rate = 1_000_000
        self.rx_rf_bandwidth = 1_000_000
        self.tx_rf_bandwidth = 1_000_000
        self.dds_enabled = [0] * 8
        self.dds_scales = [0.0] * 8
        self.tx_cyclic_buffer = False
        self.loopback = 0

    def tx(self, samples):
        self.last_tx = samples

    def tx_destroy_buffer(self):
        pass

    def rx_destroy_buffer(self):
        pass

    def disable_dds(self):
        self.dds_enabled = [0] * 8


class _OffPoe(PoePluto):
    def __init__(self):
        vars(self).update(_pluto=None, fault=None, _lock=threading.RLock(), ups=0, uri="fake:poe", _port="ether1")

    def power_up(self):
        vars(self)["ups"] += 1
        raise AssertionError("active_frequencies must not power on a PoE Pluto")

    def __getattr__(self, name):
        raise AssertionError(f"active_frequencies must not read hardware attribute {name!r}")


def _pluto_state(schema, *, name, dtype="adalm_pluto"):
    return dm._DeviceState(
        dev=schema,
        origin="local",
        online=True,
        salt="",
        hsh="",
        name=name,
        ident=name,
        dtype=dtype,
        handle=dm._name_handle(name),
    )


def _host_state(gid, *, host_id, name, dtype="adalm_pluto"):
    return dm._DeviceState(
        dev=dm.VirtualDevice(gid=gid, host_id=host_id, device_id=str(gid), label=name, kind=dtype),
        origin="host",
        online=True,
        salt="",
        hsh="",
        name=name,
        ident=name,
        dtype=dtype,
        handle=dm._name_handle(name),
    )


class ActiveFrequenciesTests(_Devices):
    def setUp(self):
        super().setUp()
        from remoteRF_server.server import acc_perms, reservation, user_group_handler as ugh

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target in (reservation, acc_perms, ugh):
            patcher = mock.patch.object(target, "get_db_dir", return_value=Path(tmp.name))
            patcher.start()
            self.addCleanup(patcher.stop)
        groups = ugh.UserGroupHandler()
        self.h = reservation.ReservationHandler()
        for name, value in (("perms", acc_perms.perms_db()), ("user_group_handler", groups)):
            patcher = mock.patch.object(reservation, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (
            mock.patch.dict(dm.devices_info, clear=True),
            mock.patch.dict(dm.device_serialization, clear=True),
            mock.patch.object(dm, "_prepare_for_holder"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # jdoe is only permitted LOCAL_PLUTO; asmith can reserve the others too.
        g_jdoe = groups.create_user_group(
            group_name="g_jdoe", devices_whitelist=[LOCAL_PLUTO], max_reservation_time_sec=3600,
            max_reservations=4, lifetime_sec=0,
        )
        g_asmith = groups.create_user_group(
            group_name="g_asmith",
            devices_whitelist=[LOCAL_PLUTO, LOCAL_MIMO, LOCAL_IDLE, LOCAL_USRP, LOCAL_POE, HOST_PLUTO_A, HOST_PLUTO_B],
            max_reservation_time_sec=3600,
            max_reservations=10,
            lifetime_sec=0,
        )
        groups.create_enrollment_codes(code_names=["C1"], group_id=g_jdoe, uses=1, duration_sec=0)
        groups.create_enrollment_codes(code_names=["C2"], group_id=g_asmith, uses=1, duration_sec=0)
        for user, code in (("jdoe", "C1"), ("asmith", "C2")):
            self.assertIn(
                "UC",
                self.h.create_user(
                    un=map_arg(user), pw=map_arg("password1"),
                    em=map_arg(f"{user}@example.org"), ec=map_arg(code),
                ),
            )
        self.reservation = reservation

    def _book(self, user, start_min, end_min, gid):
        now = datetime.now()
        with sqlite3.connect(self.h.filepath) as conn:
            conn.execute(
                "INSERT INTO Reservations (username, device_id, start_time, end_time, salt, hash) "
                "VALUES (?, ?, ?, ?, 's', 'h')",
                (user, gid, _ts(now + timedelta(minutes=start_min)), _ts(now + timedelta(minutes=end_min))),
            )

    def _call(self, user="jdoe", pw="password1"):
        result = self.h.handle_call(
            function_name="active_frequencies", args={"un": map_arg(user), "pw": map_arg(pw)}
        )
        return {k: unmap_arg(v) for k, v in result.items()}

    def test_requires_valid_credentials(self):
        self.assertEqual(self._call(pw="wrong"), {"ace": "Invalid login credentials."})
        self.assertEqual(self._call(user="nobody"), {"ace": "Invalid login credentials."})

    def test_only_currently_reserved_plutos_and_no_usernames_or_res_ids(self):
        s_local = PlutoSchema()
        s_local.bind(_FakeRadio())
        s_local.dispatch("set_rx_lo", {"value": 915_000_000})
        s_local.dispatch("set_tx_lo", {"value": 2_450_000_000})
        s_local.dispatch("set_sample_rate", {"value": 2_000_000})
        s_local.dispatch("set_rx_rf_bandwidth", {"value": 1_500_000})
        s_local.dispatch("call_tx", {"value": np.zeros(8, dtype=np.complex64)})

        s_mimo = PlutoMIMOSchema()
        s_mimo.bind(_FakeRadio())
        s_mimo.dispatch("set_rx_lo", {"value": 1_575_420_000})

        s_idle = PlutoSchema()
        s_idle.bind(_FakeRadio())
        s_idle.dispatch("set_tx_lo", {"value": 902_000_000})

        dm._devices[LOCAL_PLUTO] = _pluto_state(s_local, name="Pluto A")
        dm._devices[LOCAL_MIMO] = _pluto_state(s_mimo, name="Pluto MIMO 1", dtype="adalm_pluto_mimo")
        dm._devices[LOCAL_IDLE] = _pluto_state(s_idle, name="Pluto Idle")
        dm._devices[LOCAL_USRP] = _state(name="USRP 1")
        dm._devices[LOCAL_USRP].dtype = "usrp"

        # Active reservations on LOCAL_PLUTO, LOCAL_MIMO, LOCAL_USRP, and a partner alias;
        # expired and future reservations on LOCAL_IDLE.
        self._book("asmith", -10, 30, LOCAL_PLUTO)
        self._book("asmith", -5, 25, LOCAL_MIMO)
        self._book("asmith", -5, 25, LOCAL_USRP)
        self._book("asmith", -5, 25, PARTNER_ALIAS)
        self._book("asmith", -60, -10, LOCAL_IDLE)
        self._book("asmith", 30, 60, LOCAL_IDLE)

        # jdoe (even without LOCAL_MIMO in their group whitelist) sees every reserved Pluto.
        reply = self._call(user="jdoe")
        self.assertEqual(set(reply), {"UC"})
        raw_json = reply["UC"]
        self.assertNotIn("asmith", raw_json)
        self.assertNotIn("jdoe", raw_json)
        self.assertNotIn("res_id", raw_json)
        self.assertNotIn("username", raw_json)

        payload = json.loads(raw_json)
        devices = payload["devices"]
        self.assertEqual([d["device_id"] for d in devices], [LOCAL_PLUTO, LOCAL_MIMO])

        first, second = devices
        self.assertEqual(first["handle"], "Pluto_A")
        self.assertEqual(first["name"], "Pluto A")
        self.assertEqual(first["rx_hz"], 915_000_000)
        self.assertEqual(first["tx_hz"], 2_450_000_000)
        self.assertTrue(first["tx_active"])
        self.assertEqual(first["sample_rate"], 2_000_000)
        self.assertEqual(first["rx_rf_bandwidth"], 1_500_000)
        self.assertIsNone(first["tx_rf_bandwidth"])
        self.assertIsNotNone(first["updated_at"])

        self.assertEqual(second["handle"], "Pluto_MIMO_1")
        self.assertEqual(second["rx_hz"], 1_575_420_000)
        self.assertIsNone(second["tx_hz"])
        self.assertFalse(second["tx_active"])
        self.assertIsNone(second["sample_rate"])

    def test_off_poe_pluto_reports_nulls_without_booting(self):
        poe = _OffPoe()
        s_poe = PlutoSchema()
        s_poe.bind(poe)
        dm._devices[LOCAL_POE] = _pluto_state(s_poe, name="Pluto PoE")
        self._book("asmith", -5, 25, LOCAL_POE)

        data = json.loads(self._call()["UC"])
        [row] = data["devices"]
        self.assertEqual(row["device_id"], LOCAL_POE)
        self.assertEqual(row["handle"], "Pluto_PoE")
        self.assertIsNone(row["rx_hz"])
        self.assertIsNone(row["tx_hz"])
        self.assertFalse(row["tx_active"])
        self.assertIsNone(row["sample_rate"])
        self.assertIsNone(row["updated_at"])
        self.assertEqual(poe.ups, 0)

    def test_satellite_host_plutos_queried_over_tunnel_and_sanitized(self):
        dm._devices[HOST_PLUTO_A] = _host_state(HOST_PLUTO_A, host_id="host-a", name="Host Pluto A")
        dm._devices[HOST_PLUTO_B] = _host_state(HOST_PLUTO_B, host_id="host-b", name="Host Pluto B", dtype="pluto_plus")
        dm._devices[HOST_IDLE] = _host_state(HOST_IDLE, host_id="host-a", name="Host Pluto Idle")
        dm._devices[HOST_USRP] = _host_state(HOST_USRP, host_id="host-a", name="Host USRP", dtype="usrp")

        self._book("asmith", -5, 25, HOST_PLUTO_A)
        self._book("asmith", -5, 25, HOST_PLUTO_B)
        self._book("asmith", -5, 25, HOST_USRP)

        calls = []

        class _FakeRegistry:
            def device_directory_cached(self, ttl_ms=0):
                return {
                    str(HOST_PLUTO_A): ("host-a", SimpleNamespace(label="Host Pluto A", serial="A", kind="adalm_pluto"), True),
                    str(HOST_PLUTO_B): ("host-b", SimpleNamespace(label="Host Pluto B", serial="B", kind="pluto_plus"), True),
                    str(HOST_IDLE): ("host-a", SimpleNamespace(label="Host Pluto Idle", serial="I", kind="adalm_pluto"), True),
                    str(HOST_USRP): ("host-a", SimpleNamespace(label="Host USRP", serial="U", kind="usrp"), True),
                }

            def request_control(self, host_id, *, kind, device_id, payload, timeout):
                calls.append((host_id, kind, device_id, payload))
                if host_id == "host-a":
                    return {
                        "devices": {
                            str(HOST_PLUTO_A): {
                                "rx_hz": 915_000_000,
                                "tx_hz": 920_000_000.0,
                                "tx_active": True,
                                "sample_rate": 2_400_000,
                                "rx_rf_bandwidth": float("nan"),
                                "tx_rf_bandwidth": True,
                                "updated_at": 1700000000.5,
                                "username": "should-never-leak",
                                "res_id": 99,
                            }
                        }
                    }
                return None  # unreachable / older host

        with mock.patch.object(dm, "_get_host_registry", return_value=_FakeRegistry()), \
             mock.patch.object(dm, "_host_sync_last_ms", 0):
            reply = self._call()

        self.assertNotIn("should-never-leak", reply["UC"])
        self.assertNotIn("res_id", reply["UC"])
        self.assertEqual(
            sorted((h, k, p) for h, k, _d, p in calls),
            [
                ("host-a", "active_frequencies", {"device_ids": [str(HOST_PLUTO_A)]}),
                ("host-b", "active_frequencies", {"device_ids": [str(HOST_PLUTO_B)]}),
            ],
        )

        rows = {d["device_id"]: d for d in json.loads(reply["UC"])["devices"]}
        self.assertEqual(set(rows), {HOST_PLUTO_A, HOST_PLUTO_B})
        a = rows[HOST_PLUTO_A]
        self.assertEqual((a["rx_hz"], a["tx_hz"], a["tx_active"], a["sample_rate"]), (915_000_000, 920_000_000, True, 2_400_000))
        self.assertIsNone(a["rx_rf_bandwidth"])
        self.assertIsNone(a["tx_rf_bandwidth"])
        self.assertEqual(a["updated_at"], 1700000000.5)

        b = rows[HOST_PLUTO_B]
        self.assertIsNone(b["rx_hz"])
        self.assertIsNone(b["tx_hz"])
        self.assertIsNone(b["tx_active"])
        self.assertIsNone(b["sample_rate"])
        self.assertIsNone(b["updated_at"])

    def test_set_device_clears_local_frequencies_on_reservation_end(self):
        s_local = PlutoSchema()
        s_local.bind(_FakeRadio())
        dm._devices[LOCAL_PLUTO] = _pluto_state(s_local, name="Pluto A")

        dm.set_device(LOCAL_PLUTO, "salt1", "hash1")
        s_local.dispatch("set_rx_lo", {"value": 915_000_000})
        s_local.dispatch("call_tx", {"value": np.zeros(8, dtype=np.complex64)})
        self.assertEqual(s_local.active_frequencies()["rx_hz"], 915_000_000)

        dm.set_device(LOCAL_PLUTO, "", "")
        self.assertIsNone(s_local.active_frequencies()["rx_hz"])
        self.assertFalse(s_local.active_frequencies()["tx_active"])

    def test_activity_log_redacts_password(self):
        entry = self.reservation._activity_log_entry(
            function_name="active_frequencies",
            args={"un": map_arg("jdoe"), "pw": map_arg("top-secret-pw")},
            result={"UC": map_arg("{}")},
        )
        self.assertEqual(entry["action"], "active_frequencies")
        self.assertEqual(entry["username"], "jdoe")
        self.assertEqual(set(entry["details"]["args"]), {"un"})
        self.assertNotIn("top-secret-pw", json.dumps(entry))
