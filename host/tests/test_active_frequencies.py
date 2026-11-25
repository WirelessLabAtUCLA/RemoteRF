"""Host-side reporting of Pluto active frequencies over tunnel control frames."""

from __future__ import annotations

import json
import queue
import sys
import threading
import types
import unittest
from unittest import mock

import numpy as np

sys.modules.setdefault("adi", types.ModuleType("adi"))

from remoterf_device_core.drivers.adalm_pluto.pluto_schema import PlutoSchema
from remoterf_device_core.drivers.adalm_pluto.poe import PoePluto
from remoteRF_host.common.grpc import grpc_host_pb2 as pb
from remoteRF_host.server import device_manager as host_dm
from remoteRF_host.server.tunnel_agent import HostTunnelAgent


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
        raise AssertionError(f"active_frequencies must not touch hardware attribute {name!r}")


class _NonPlutoSchema:
    device_type = "usrp"


class HostActiveFrequenciesTests(unittest.TestCase):
    def _agent(self, gids):
        agent = HostTunnelAgent.__new__(HostTunnelAgent)
        agent._direct_host = None
        agent._devices = {gid: {} for gid in gids}
        agent._out_q = queue.Queue()
        agent._host_id = "lab-host"
        agent._host_token = "token"
        agent.cfg = mock.Mock(version="test")
        return agent

    def test_control_active_frequencies_reports_live_plutos_without_booting_poe(self):
        tuned = PlutoSchema()
        tuned.bind(_FakeRadio())
        tuned.dispatch("set_rx_lo", {"value": 915_000_000})
        tuned.dispatch("set_tx_lo", {"value": 2_450_000_000})
        tuned.dispatch("set_sample_rate", {"value": 2_000_000})
        tuned.dispatch("call_tx", {"value": np.zeros(8, dtype=np.complex64)})

        poe = _OffPoe()
        standby = PlutoSchema()
        standby.bind(poe)

        live = {7: tuned, 8: standby, 9: _NonPlutoSchema()}
        agent = self._agent((7, 8, 9))

        with mock.patch(
            "remoteRF_host.server.tunnel_agent.get_device",
            side_effect=lambda *, id: live.get(int(id)),
        ):
            agent._handle_control(pb.Control(
                kind="active_frequencies",
                req_id="req-1",
                payload=json.dumps({"device_ids": ["7", "8", "9", "999"]}),
            ))

        frame = agent._out_q.get(timeout=2)
        ctl = frame.control
        self.assertEqual(ctl.kind, "active_frequencies")
        self.assertEqual(ctl.req_id, "req-1")
        body = json.loads(ctl.payload)
        self.assertEqual(set(body["devices"]), {"7", "8"})

        row7 = body["devices"]["7"]
        self.assertEqual(row7["rx_hz"], 915_000_000)
        self.assertEqual(row7["tx_hz"], 2_450_000_000)
        self.assertTrue(row7["tx_active"])
        self.assertEqual(row7["sample_rate"], 2_000_000)
        self.assertIsNotNone(row7["updated_at"])

        row8 = body["devices"]["8"]
        self.assertIsNone(row8["rx_hz"])
        self.assertIsNone(row8["tx_hz"])
        self.assertFalse(row8["tx_active"])
        self.assertIsNone(row8["updated_at"])
        self.assertEqual(poe.ups, 0)

    def test_control_active_frequencies_handles_omitted_ids_and_bad_payload(self):
        tuned = PlutoSchema()
        tuned.bind(_FakeRadio())
        tuned.dispatch("set_rx_lo", {"value": 1_000_000_000})
        agent = self._agent((7,))

        with mock.patch(
            "remoteRF_host.server.tunnel_agent.get_device",
            side_effect=lambda *, id: tuned if int(id) == 7 else None,
        ):
            agent._handle_control(pb.Control(kind="active_frequencies", req_id="req-all", payload=""))
            reply_all = json.loads(agent._out_q.get(timeout=2).control.payload)
            self.assertEqual(reply_all["devices"]["7"]["rx_hz"], 1_000_000_000)

            agent._handle_control(pb.Control(kind="active_frequencies", req_id="req-bad", payload="{bad json"))
            reply_bad = json.loads(agent._out_q.get(timeout=2).control.payload)
            self.assertIn("error", reply_bad)

            agent._handle_control(pb.Control(
                kind="active_frequencies", req_id="req-type", payload=json.dumps({"device_ids": "7"}),
            ))
            reply_type = json.loads(agent._out_q.get(timeout=2).control.payload)
            self.assertIn("error", reply_type)

    def test_end_reservation_clears_recorded_frequencies_immediately(self):
        tuned = PlutoSchema()
        tuned.bind(_FakeRadio())
        tuned.dispatch("set_rx_lo", {"value": 915_000_000})
        tuned.dispatch("call_tx", {"value": np.zeros(8, dtype=np.complex64)})
        st = host_dm._DeviceState(schema=tuned, name="Pluto 7", ident="7", dtype="adalm_pluto", init={})

        with mock.patch.dict(host_dm._devices, {7: st}, clear=True), \
             mock.patch.object(host_dm, "_reservation_end_callbacks", []), \
             mock.patch.object(host_dm, "_run_hook", return_value=None):
            host_dm.end_reservation(7)

        self.assertIsNone(tuned.active_frequencies()["rx_hz"])
        self.assertFalse(tuned.active_frequencies()["tx_active"])

    def test_prepare_reservation_clears_stale_frequencies_unless_resync(self):
        tuned = PlutoSchema()
        tuned.bind(_FakeRadio())
        tuned.dispatch("set_rx_lo", {"value": 915_000_000})
        tuned.dispatch("set_tx_lo", {"value": 2_450_000_000})
        tuned.dispatch("call_tx", {"value": np.zeros(8, dtype=np.complex64)})
        st = host_dm._DeviceState(schema=tuned, name="Pluto 7", ident="7", dtype="adalm_pluto", init={})

        with mock.patch.dict(host_dm._devices, {7: st}, clear=True), \
             mock.patch.object(host_dm, "_run_hook", return_value=None):
            host_dm.prepare_reservation(7, resync=False)
            self.assertIsNone(tuned.active_frequencies()["rx_hz"])
            self.assertIsNone(tuned.active_frequencies()["tx_hz"])
            self.assertFalse(tuned.active_frequencies()["tx_active"])
            self.assertIsNone(tuned.active_frequencies()["updated_at"])

            # Tunnel reconnect resync must preserve active frequencies for the ongoing reservation.
            tuned.dispatch("set_rx_lo", {"value": 920_000_000})
            tuned.dispatch("call_tx", {"value": np.zeros(8, dtype=np.complex64)})
            host_dm.prepare_reservation(7, resync=True)
            self.assertEqual(tuned.active_frequencies()["rx_hz"], 920_000_000)
            self.assertTrue(tuned.active_frequencies()["tx_active"])

    def test_hello_advertises_active_frequencies_capability(self):
        hello = self._agent((7,))._hello_frame().hello
        self.assertIn("active_frequencies", list(hello.capabilities))
