"""A lab restart mid-session looks like a pause: reads and setters wait it out, a TX does not."""

import time as real_time

import grpc
import pytest

from remoterf.common.grpc import grpc_pb2
from remoterf.common.utils import map_arg, unmap_arg
from remoterf.core import grpc_client
from remoterf.deployment.http import AccountBackendError

TOKEN = "Zm9vYmFy@lab"


class Down(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "Socket closed"


class Clock:
    """time.sleep advances it; everything else is the real module."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(real_time, name)


class Lab:
    """Restarts at `restart_at`: refuses for `down` s, then has no devices for `starting` s."""

    def __init__(self, clock, restart_at=10.0, down=35.0, starting=5.0, relay=False):
        self.clock, self.t0 = clock, clock.now
        self.restart_at, self.down, self.starting, self.relay = restart_at, down, starting, relay
        self.state, self.calls = {}, []

    def __call__(self, function_name, args, connection):
        self.calls.append(function_name)
        t = self.clock.now - self.t0 - self.restart_at
        if 0 <= t < self.down:
            if self.relay:
                raise AccountBackendError("route_unavailable", provenance="transport")
            raise Down()
        if self.down <= t < self.down + self.starting:
            raise RuntimeError("No schema available for device 14 (device_unavailable)")
        return self.run(function_name, args)

    def run(self, function_name, args):
        if function_name.count(":") != 2:
            return grpc_pb2.GenericRPCResponse()
        _, prop, verb = function_name.split(":")
        if prop == "batch":
            ops = [grpc_pb2.GenericRPCRequest.FromString(a.bytes_value) for k, a in args.items() if k.startswith("op")]
            for op in ops:
                response = self.run(op.function_name, dict(op.args) | {"a": args["a"]})
            response.results["batch_done"].CopyFrom(map_arg(len(ops)))
            return response
        if verb == "SET":
            self.state[prop] = unmap_arg(args[prop])
            return grpc_pb2.GenericRPCResponse()
        if verb == "GET":
            return grpc_pb2.GenericRPCResponse(results={prop: map_arg(self.state.get(prop, 0))})
        return grpc_pb2.GenericRPCResponse(results={prop: map_arg("called")})


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.delenv("REMOTERF_RX_READAHEAD", raising=False)
    monkeypatch.delenv("REMOTERF_RESTART_WAIT", raising=False)
    monkeypatch.setattr(grpc_client, "time", clock)
    monkeypatch.setattr(grpc_client, "_batch_local", grpc_client._BatchState())
    return clock


def _lab(monkeypatch, clock, **kw):
    lab = Lab(clock, **kw)
    monkeypatch.setattr(grpc_client, "_dispatch_once", lab)
    return lab


def _call(function_name, **args):
    return grpc_client.rpc_client(function_name=function_name, args={**{k: map_arg(v) for k, v in args.items()}, "a": map_arg(TOKEN)})


@pytest.mark.parametrize("relay", [False, True])
def test_setter_and_getter_ride_through_a_restart(monkeypatch, clock, relay):
    lab = _lab(monkeypatch, clock, relay=relay)
    _call("Pluto:rx_lo:SET", rx_lo=2.4e9)
    clock.now += 10.0  # the script works for a while, then the lab restarts under it
    _call("Pluto:tx_lo:SET", tx_lo=905e6)
    assert lab.state["tx_lo"] == 905e6
    assert unmap_arg(_call("Pluto:tx_lo:GET").results["tx_lo"]) == 905e6
    assert clock.now - lab.t0 < 10 + 35 + 5 + 10  # waited for the restart, not the whole window


def test_rx_rides_through_but_tx_fails_at_once(monkeypatch, clock):
    lab = _lab(monkeypatch, clock)
    clock.now += 10.0
    with pytest.raises(grpc.RpcError):
        _call("Pluto:tx:CALL1", arg1=[1, 2, 3])
    assert lab.calls == ["Pluto:tx:CALL1"]
    assert unmap_arg(_call("Pluto:rx:CALL0").results["rx"]) == "called"


def test_batch_of_setters_rides_through_but_a_batch_with_tx_does_not(monkeypatch, clock):
    lab = _lab(monkeypatch, clock)
    monkeypatch.setattr(grpc_client, "_batch_support", {})
    clock.now += 10.0
    token = TOKEN
    with grpc_client.batch(token):
        _call("Pluto:tx_lo:SET", tx_lo=905e6)
        _call("Pluto:rx_lo:SET", rx_lo=2.4e9)
    assert lab.state == {"tx_lo": 905e6, "rx_lo": 2.4e9}
    lab.state.clear()
    lab.t0, lab.calls = clock.now - 10.0, []  # the lab restarts again
    with pytest.raises(grpc.RpcError):
        with grpc_client.batch(token):
            _call("Pluto:tx_lo:SET", tx_lo=1e9)
            _call("Pluto:tx:CALL1", arg1=[1])
    assert lab.state == {}  # nothing ran twice, and nothing ran at all


def test_an_outage_longer_than_the_window_still_fails(monkeypatch, clock):
    _lab(monkeypatch, clock, down=500.0)
    clock.now += 10.0
    start = clock.now
    with pytest.raises(grpc.RpcError):
        _call("Pluto:tx_lo:SET", tx_lo=905e6)
    assert 90 <= clock.now - start <= 91


def test_restart_wait_zero_fails_at_once(monkeypatch, clock):
    lab = _lab(monkeypatch, clock)
    monkeypatch.setenv("REMOTERF_RESTART_WAIT", "0")
    clock.now += 10.0
    with pytest.raises(grpc.RpcError):
        _call("Pluto:tx_lo:SET", tx_lo=905e6)
    assert len(lab.calls) == 1


def test_a_device_that_is_simply_missing_is_not_retried(monkeypatch, clock):
    calls = []

    def lab(function_name, args, connection):
        calls.append(function_name)
        raise RuntimeError("No schema available for device 14 (device_unavailable)")

    monkeypatch.setattr(grpc_client, "_dispatch_once", lab)
    with pytest.raises(RuntimeError):
        _call("Pluto:tx_lo:GET")
    assert len(calls) == 1
