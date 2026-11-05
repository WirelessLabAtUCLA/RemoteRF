import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest import mock

from remoteRF_server.common.grpc import grpc_pb2
from remoteRF_server.common.utils import map_arg, unmap_arg
from remoteRF_server.server import direct_path, grpc_server
from remoteRF_server.server.transport_counters import TransportCounters, create_table


class Clock:
    def __init__(self):
        self.elapsed = 0.0
        self.now = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)

    def monotonic(self):
        return self.elapsed

    def utcnow(self):
        return self.now

    def advance(self, seconds):
        self.elapsed += seconds
        self.now += timedelta(seconds=seconds)


class Context:
    def __init__(self, peer):
        self._peer = peer

    def peer(self):
        return self._peer

    def invocation_metadata(self):
        return ()


class Writer:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    def write_eof(self):
        pass

    def close(self):
        pass


def _table(path):
    with sqlite3.connect(path) as db:
        create_table(db)


def test_flushes_accumulated_calls_at_most_once_per_minute(tmp_path):
    database = tmp_path / "reservations.db"
    _table(database)
    clock = Clock()
    counters = TransportCounters(database, monotonic=clock.monotonic, utcnow=clock.utcnow)

    counters.record("lan", "Adalm_pluto:rx:CALL0", 10, 20)
    clock.advance(59)
    counters.record("lan", "Adalm_pluto:rx:CALL0", 11, 21)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM TransportDays").fetchone()[0] == 0

    clock.advance(1)
    counters.record("lan", "Adalm_pluto:rx:CALL0", 12, 22)
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT day, path, type, fn, calls, bytes_in, bytes_out FROM TransportDays"
        ).fetchone() == (
            "2026-11-04", "lan", "adalm_pluto", "Adalm_pluto:rx:CALL0", 3, 33, 63
        )


def test_reservation_database_migrates_transport_days():
    with sqlite3.connect(grpc_server.reservation_handler.filepath) as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='TransportDays'"
        ).fetchone() == ("TransportDays",)


def test_direct_relay_and_lan_dispatches_count_serialized_frames():
    seen = []
    request = grpc_pb2.GenericRPCRequest(
        function_name="Adalm_pluto:ip:CALL0", args={"a": map_arg("token")}
    )

    def record(path, fn, bytes_in, bytes_out):
        seen.append((path, fn, bytes_in, bytes_out))

    with mock.patch.object(grpc_server.transport_counters, "record", side_effect=record), \
            mock.patch.object(grpc_server.rpc_manager, "run_rpc", return_value={"ip": map_arg("device")}) as run:
        lan = grpc_server.GenericRPCServicer().Call(request, Context("ipv4:192.168.1.10:50000"))
        relay = grpc_server.GenericRPCServicer().Call(request, Context("ipv4:127.0.0.1:50000"))
    assert unmap_arg(lan.results["ip"]) == "device"
    assert unmap_arg(relay.results["ip"]) == "device"
    assert run.call_count == 2

    async def direct_call():
        reader = asyncio.StreamReader()
        reader.feed_data(direct_path.frame(request.SerializeToString()))
        reader.feed_eof()
        writer = Writer()
        path = mock.Mock(peer=("198.51.100.2", 50000), bound=None, calls=0)
        await direct_path._serve_call(path, reader, writer)
        return writer

    with mock.patch.object(direct_path.transport_counters, "record", side_effect=record), \
            mock.patch.object(direct_path, "_dispatch", return_value={"ip": map_arg("device")}):
        writer = asyncio.run(direct_call())

    response_size = len(grpc_pb2.GenericRPCResponse(results={"ip": map_arg("device")}).SerializeToString())
    assert seen == [
        ("lan", request.function_name, len(request.SerializeToString()), response_size),
        ("relay", request.function_name, len(request.SerializeToString()), response_size),
        ("direct", request.function_name, len(request.SerializeToString()), response_size),
    ]
    assert len(writer.data) == response_size + 4


def test_database_error_during_flush_does_not_break_the_rpc(tmp_path):
    clock = Clock()
    counters = TransportCounters(
        tmp_path / "missing" / "reservations.db",
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
    )
    clock.advance(60)
    request = grpc_pb2.GenericRPCRequest(function_name="Adalm_pluto:ip:CALL0")
    with mock.patch.object(grpc_server, "transport_counters", counters), \
            mock.patch.object(grpc_server.rpc_manager, "run_rpc", return_value={"ip": map_arg("device")}):
        response = grpc_server.GenericRPCServicer().Call(
            request, Context("ipv4:192.168.1.10:50000")
        )
    assert unmap_arg(response.results["ip"]) == "device"

    (tmp_path / "missing").mkdir()
    _table(tmp_path / "missing" / "reservations.db")
    counters.flush(force=True)
    with sqlite3.connect(tmp_path / "missing" / "reservations.db") as db:
        assert db.execute("SELECT calls FROM TransportDays").fetchone() == (1,)


def test_failed_rpc_is_still_counted():
    request = grpc_pb2.GenericRPCRequest(function_name="Adalm_pluto:ip:CALL0")
    seen = []
    with mock.patch.object(grpc_server.transport_counters, "record", side_effect=lambda *args: seen.append(args)), \
            mock.patch.object(grpc_server.rpc_manager, "run_rpc", side_effect=RuntimeError("failed")):
        try:
            grpc_server.GenericRPCServicer().Call(request, Context("ipv4:192.168.1.10:50000"))
        except RuntimeError:
            pass
        else:
            raise AssertionError("RPC failure did not propagate")
    assert seen == [("lan", request.function_name, request.ByteSize(), 0)]
