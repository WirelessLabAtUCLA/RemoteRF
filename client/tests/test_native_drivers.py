"""Every driver the README lists imports from a fresh `pip install remoterf`,
and works against the schema it ships with and against what a lab serves
today (ucla_driver_schemas.json: ucla's own copies, as of Server 1.7.16)."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import remoterf.drivers as drivers_pkg
from remoterf.common.grpc import grpc_pb2
from remoterf.common.utils import map_arg
from remoterf.core import v2_errors
from remoterf.drivers import dynamic_device

IMPORTS = {
    "from remoterf.drivers.adalm_pluto import adi": ("adalm_pluto", "Pluto"),
    "from remoterf.drivers.adalm_pluto_mimo import adi": ("adalm_pluto_mimo", "PlutoMIMO"),
    "from remoterf.drivers.usrp import uhd": None,
    "from remoterf.drivers.rtlsdr import RtlSdr": ("rtl_sdr", "RtlSdr"),
    "from remoterf.drivers.hackrf import HackRF": ("hackrf", "HackRF"),
    "from remoterf.drivers.krakensdr import KrakenSDR": ("krakensdr", "KrakenSDR"),
    "from remoterf.drivers.ti_awr6843 import TiMmWave": ("ti_awr6843", "TiMmWave"),
    "from remoterf.drivers.rfbeam_kmd7 import RFBeamKMD7": ("rfbeam_kmd7", "RFBeamKMD7"),
    "from remoterf.drivers.wifi import WifiDongle": ("wifi", "WifiDongle"),
    "from remoterf.drivers.bluetooth import BluetoothModule": ("bluetooth", "BluetoothModule"),
    "from remoterf.drivers.gps import GpsModule": ("gps", "GpsModule"),
}
LAB = json.loads((Path(__file__).with_name("ucla_driver_schemas.json")).read_text(encoding="utf-8"))
# The shipped package a lab's device type is reached from.
SHIPPED = {"kraken_sdr": ("krakensdr", "KrakenSDR"), "rfbeam_kmd7": ("rfbeam_kmd7", "RFBeamKMD7"),
           "wifi": ("wifi", "WifiDongle"), "bluetooth": ("bluetooth", "BluetoothModule"),
           "gps": ("gps", "GpsModule")}


@pytest.mark.parametrize("line", sorted(IMPORTS))
def test_each_listed_import_works_in_a_fresh_interpreter(line, tmp_path):
    subprocess.run([sys.executable, "-c", line], cwd=tmp_path, check=True)


def test_kmd7_is_also_exported_for_labs_whose_schema_names_it():
    from remoterf.drivers.rfbeam_kmd7 import KMD7, RFBeamKMD7

    assert KMD7 is RFBeamKMD7


def _lab(monkeypatch, schema):
    """A lab serving ``schema`` (v1 only); returns the device RPCs made."""
    calls = []

    def rpc_client(*, function_name, args, connection=None):
        if function_name == "IDL:get_drivers":
            return grpc_pb2.GenericRPCResponse(results={"schema": map_arg(json.dumps(schema))})
        calls.append(function_name)
        return grpc_pb2.GenericRPCResponse(results={})

    def v1_only(token):
        raise v2_errors.RemoteRFProtocolError("this device publishes v1 only")

    # sys.modules' copies: the ones the drivers import (a test may re-import grpc_client)
    monkeypatch.setattr(importlib.import_module("remoterf.core.grpc_client"), "rpc_client", rpc_client)
    monkeypatch.setattr(importlib.import_module("remoterf.drivers.dynamic_v2"), "fetch_schema_v2", v1_only)
    monkeypatch.setattr(dynamic_device, "_verified", set())
    return calls


@pytest.fixture
def drivers_dir(tmp_path, monkeypatch):
    """Driver packages written here, and imported from here first."""
    saved = {k: v for k, v in sys.modules.items() if k.startswith("remoterf.drivers.")}
    monkeypatch.setattr(dynamic_device, "_DRIVERS_DIR", tmp_path)
    monkeypatch.setattr(drivers_pkg, "__path__", [str(tmp_path), *drivers_pkg.__path__])
    yield tmp_path
    for name in [k for k in sys.modules if k.startswith("remoterf.drivers.")]:
        if name not in saved:
            del sys.modules[name]
    sys.modules.update(saved)
    for child in {k.split(".")[2] for k in saved} | {p.name for p in tmp_path.iterdir()}:
        module = saved.get(f"remoterf.drivers.{child}")
        if module is not None:
            setattr(drivers_pkg, child, module)
        elif hasattr(drivers_pkg, child):
            delattr(drivers_pkg, child)


@pytest.mark.parametrize("line", sorted(line for line, spec in IMPORTS.items() if spec))
def test_each_class_constructs_against_the_schema_it_ships_with(line, monkeypatch):
    package, name = IMPORTS[line]
    module = importlib.import_module(f"remoterf.drivers.{package}")
    cls = getattr(module, name)
    remote = sys.modules[cls.__module__]
    calls = _lab(monkeypatch, {"device_type": package, "schema_hash": remote._SCHEMA_HASH})

    def no_write(*args, **kwargs):
        raise AssertionError("a matching driver is not rewritten")

    monkeypatch.setattr(dynamic_device, "_write_driver_files", no_write)
    device = cls("tok")
    assert type(device) is cls
    assert all(call.startswith(remote._PREFIX + ":") for call in calls)


@pytest.mark.parametrize("lab_type", sorted(LAB))
def test_each_class_becomes_the_labs_own_on_what_ucla_serves(lab_type, drivers_dir, monkeypatch, capsys):
    schema = LAB[lab_type]
    package, name = SHIPPED[lab_type]
    cls = getattr(importlib.import_module(f"remoterf.drivers.{package}"), name)
    calls = _lab(monkeypatch, schema)

    device = cls("tok")
    lab_cls = type(device)
    assert lab_cls.__module__ == f"remoterf.drivers.{lab_type}.{lab_type}_remote"
    assert lab_cls.__name__ == schema["client_class"] and device.token == "tok"
    assert "updated to this lab's version" in capsys.readouterr().out

    # Its methods are the lab's: the first call goes out under the lab's type.
    method = sorted(schema["calls"])[0][len("call_"):]
    required = [a for a in schema["calls"]["call_" + method]["args"] if a["required"]]
    getattr(device, method)(*[0] * len(required))
    assert calls[-1].split(":")[:2] == [lab_type[0].upper() + lab_type[1:], method]

    # A second device: the lab's driver is on disk already, nothing rewritten.
    written = []
    monkeypatch.setattr(dynamic_device, "_write_driver_files", lambda *a, **k: written.append(a))
    assert type(cls("tok2")) is lab_cls and written == []

    # The documented import still works from the reinstalled package.
    reloaded = importlib.import_module(f"remoterf.drivers.{lab_type}")
    if lab_type == package:
        assert getattr(reloaded, name) is lab_cls


def test_a_user_subclass_keeps_its_class(drivers_dir, monkeypatch):
    from remoterf.drivers.rfbeam_kmd7 import RFBeamKMD7

    class Mine(RFBeamKMD7):
        pass

    _lab(monkeypatch, LAB["rfbeam_kmd7"])
    assert type(Mine("tok")) is Mine


def test_a_package_updated_by_another_process_asks_for_a_reimport(drivers_dir, monkeypatch, capsys):
    from remoterf.drivers.bluetooth import BluetoothModule

    dynamic_device._write_v1_driver_files(LAB["bluetooth"])  # on disk, not in this process
    _lab(monkeypatch, LAB["bluetooth"])
    assert type(BluetoothModule("tok")) is BluetoothModule
    assert "Reimport before continuing" in capsys.readouterr().out


@pytest.mark.parametrize("lab_type", sorted(LAB))
def test_ensure_driver_after_a_native_import_leaves_one_driver_per_type(lab_type, drivers_dir, monkeypatch):
    package, name = SHIPPED[lab_type]
    shutil.copytree(Path(dynamic_device.__file__).with_name(package), drivers_dir / package)
    cls = getattr(importlib.import_module(f"remoterf.drivers.{package}"), name)
    _lab(monkeypatch, LAB[lab_type])

    dynamic_device.ensure_driver(token="tok")
    assert dynamic_device._read_schema_hash(drivers_dir / lab_type / f"{lab_type}_remote.py") == LAB[lab_type]["schema_hash"]
    types = [p.read_text(encoding="utf-8").split("# device_type: ", 1)[1].split()[0]
             for p in drivers_dir.glob("*/*_remote.py")]
    assert sorted(types) == sorted({package, lab_type}) and len(set(types)) == len(types)
    if lab_type == package:
        assert hasattr(importlib.import_module(f"remoterf.drivers.{lab_type}"), name)

    # The class imported before ensure_driver still constructs, as the lab's.
    assert type(cls("tok")).__module__ == f"remoterf.drivers.{lab_type}.{lab_type}_remote"
