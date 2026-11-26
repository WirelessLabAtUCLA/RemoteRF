"""Bluetooth driver with a fake sysfs tree and a fake system bus: BlueZ is never touched."""

import enum
import importlib.util
import sys
import time
import types

import pytest

import remoterf_device_core.drivers.bluetooth as bluetooth_pkg
from remoterf_device_core.idl import schema as idl_schema

GONE = "org.freedesktop.DBus.Error.UnknownObject"


class MessageType(enum.Enum):
    METHOD_RETURN = 2
    ERROR = 3


class Variant:
    def __init__(self, signature, value):
        self.signature, self.value = signature, value


class Message:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class FakeBus:
    """BlueZ on the system bus: adapters by name, every call it saw, and its disconnects."""

    adapters: dict = {}
    calls: list = []
    disconnects = 0
    fail = False

    def __init__(self, bus_type=None):
        pass

    async def connect(self):
        return self

    def disconnect(self):
        FakeBus.disconnects += 1

    async def call(self, msg):
        name = msg.path.rsplit("/", 1)[1]
        FakeBus.calls.append((msg.member, name))
        if FakeBus.fail:
            raise TimeoutError("no reply")
        props = FakeBus.adapters.get(name)
        if props is None:
            return Message(message_type=MessageType.ERROR, error_name=GONE, body=["gone"])
        if msg.member == "GetAll":
            body = [{"Address": Variant("s", "00:11:22:33:44:55"), "Version": Variant("y", 6)}]
        elif msg.member == "Get":
            body = [Variant("b", props[msg.body[1]])]
        else:
            props[msg.body[1]] = msg.body[2].value
            body = []
        return Message(message_type=MessageType.METHOD_RETURN, error_name=None, body=body)


@pytest.fixture(scope="module")
def bt():
    dbus = types.ModuleType("dbus_fast")
    dbus.Message, dbus.Variant = Message, Variant
    aio = types.ModuleType("dbus_fast.aio")
    aio.MessageBus = FakeBus
    constants = types.ModuleType("dbus_fast.constants")
    constants.BusType = types.SimpleNamespace(SYSTEM="system")
    constants.MessageType = MessageType
    fakes = {"dbus_fast": dbus, "dbus_fast.aio": aio, "dbus_fast.constants": constants}
    saved_modules = {key: sys.modules.get(key) for key in fakes}
    saved_registry = dict(idl_schema._DRIVER_REGISTRY)
    sys.modules.update(fakes)
    try:
        path = bluetooth_pkg.__path__[0] + "/bluetooth_schema.py"
        spec = importlib.util.spec_from_file_location("test_bluetooth_schema", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for key, value in saved_modules.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
        idl_schema._DRIVER_REGISTRY.clear()
        idl_schema._DRIVER_REGISTRY.update(saved_registry)
    return module


@pytest.fixture
def sysfs(bt, tmp_path, monkeypatch):
    base = tmp_path / "class" / "bluetooth"
    base.mkdir(parents=True)
    monkeypatch.setattr(bt, "_SYSFS_BLUETOOTH", base)
    monkeypatch.setattr(FakeBus, "adapters", {})
    monkeypatch.setattr(FakeBus, "calls", [])
    monkeypatch.setattr(FakeBus, "disconnects", 0)
    monkeypatch.setattr(FakeBus, "fail", False)

    def plug(name, vendor, product, product_name, removable=None):
        usb = tmp_path / "devices" / f"usb-{name}"
        (usb / "bluetooth" / name).mkdir(parents=True)
        for key, value in (("idVendor", vendor), ("idProduct", product), ("product", product_name), ("removable", removable)):
            if value is not None:
                (usb / key).write_text(value + "\n")
        (base / name).symlink_to(usb / "bluetooth" / name)
        FakeBus.adapters[name] = {"Powered": True, "Discovering": False}

    def unplug(name):
        (base / name).unlink()
        FakeBus.adapters.pop(name)

    return plug, unplug


def test_built_in_adapter_only_with_allow_fallback_and_csr_first(bt, sysfs):
    plug, _ = sysfs
    plug("hci0", "8087", "0026", "Built-in Bluetooth")
    assert bt._find_adapter_sysfs() is None
    assert bt.BluetoothSchema.make_device() is None
    assert bt.BluetoothSchema.make_device(allow_fallback=True).adapter_name == "hci0"
    plug("hci1", "0bda", "8771", "Bluetooth Radio", "removable")
    plug("hci2", "0a12", "0001", "CSR8510 A10")
    dev = bt.BluetoothSchema.make_device(allow_fallback=True)
    assert (dev.adapter_name, dev.is_zexmte, dev.bd_address) == ("hci2", True, "00:11:22:33:44:55")
    assert bt.BluetoothSchema.driver_version == "0.1.4"


def test_renumbered_adapter_is_retried_once_on_its_new_name(bt, sysfs):
    plug, unplug = sysfs
    plug("hci1", "0a12", "0001", "CSR8510 A10")
    dev = bt.BluetoothSchema.make_device()
    unplug("hci1")
    plug("hci2", "0a12", "0001", "CSR8510 A10")
    FakeBus.calls.clear()
    assert bt._run_coroutine_sync(dev.get_powered_async) is True
    assert dev.adapter_name == "hci2"
    assert FakeBus.calls == [("Get", "hci1"), ("Get", "hci2")]


def test_bus_is_disconnected_when_a_call_raises(bt, sysfs):
    plug, _ = sysfs
    plug("hci1", "0a12", "0001", "CSR8510 A10")
    dev = bt.BluetoothSchema.make_device()
    FakeBus.fail = True
    FakeBus.disconnects = 0
    assert bt._run_coroutine_sync(dev.get_powered_async) is False
    assert bt._run_coroutine_sync(dev.set_powered_async, True) is False
    assert bt._run_coroutine_sync(dev.get_discovering_async) is False
    assert bt._run_coroutine_sync(bt._query_bluez_props, "hci1") == {}
    assert FakeBus.disconnects == 4


@pytest.mark.parametrize("timeout, reported", [(100, 20.0), (0.2, 1.0), (5, 5.0)])
def test_scan_summary_reports_the_clamped_duration(bt, timeout, reported):
    durations = []

    async def scan_async(duration):
        durations.append(duration)
        return [{"address": "AA", "name": "x", "rssi": -50, "vendor": "Apple, Inc."}]

    schema = bt.BluetoothSchema()
    schema.bind(types.SimpleNamespace(scan_async=scan_async))
    summary = schema.call_scan_summary(timeout=timeout)
    assert summary["scan_duration_sec"] == reported
    assert durations == [reported]
    assert summary["total_devices"] == 1


def test_scan_caps_total_wait_at_20s_including_power_on_dwell(bt, sysfs, monkeypatch):
    plug, _ = sysfs
    plug("hci1", "0a12", "0001", "CSR8510 A10")
    dev = bt.BluetoothSchema.make_device()
    FakeBus.adapters["hci1"]["Powered"] = False

    clock = [100.0]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    class FakeScanner:
        def __init__(self, **_):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr(bt, "BleakScanner", FakeScanner)
    monkeypatch.setattr(bt.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(bt, "time", types.SimpleNamespace(monotonic=lambda: clock[0], time=time.time))

    schema = bt.BluetoothSchema()
    schema.bind(dev)
    assert bt.MAX_SCAN_SECONDS == 20.0
    assert schema.call_scan(timeout=60.0) == []
    assert sleeps == [0.5, 19.5]
    assert sum(sleeps) <= 20.0
    assert FakeBus.adapters["hci1"]["Powered"] is True


def test_reset_for_next_holder_clears_the_cache_and_powers_on(bt, sysfs):
    plug, _ = sysfs
    plug("hci1", "0a12", "0001", "CSR8510 A10")
    dev = bt.BluetoothSchema.make_device()
    FakeBus.adapters["hci1"]["Powered"] = False
    dev.device_cache["AA"] = {"address": "AA"}
    dev.last_scan_time = 123.0
    schema = bt.BluetoothSchema()
    schema.bind(dev)
    schema.reset_for_next_holder()
    assert (dev.device_cache, dev.last_scan_time) == ({}, 0.0)
    assert FakeBus.adapters["hci1"]["Powered"] is True
    FakeBus.fail = True
    with pytest.raises(RuntimeError, match="could not power hci1 on"):
        schema.reset_for_next_holder()
