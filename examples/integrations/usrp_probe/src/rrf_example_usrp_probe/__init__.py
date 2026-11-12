"""Receive-only example adapter; native UHD imports stay in the child.

This package installs no UHD version and grants no hardware access by itself.
The operator supplies an exact serial selector and the runtime supplies an
owner-bound process scope. Neither caller configuration nor actions select an
executable, environment, path, device address or transmit operation.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import sys
import time


SERIAL = re.compile(r"[A-Za-z0-9]{1,64}")
MARKER = "RRF_USRP_PROBE "
_DEFAULTS = {"rate": 1e6, "frequency": 100e6, "gain": 0.0, "samples": 4096,
             "channel": 0, "startup_timeout": 15.0}


def configuration(profile, value):
    if profile != "receive" or type(value) is not dict or set(value) - set(_DEFAULTS):
        raise ValueError("unsupported receive configuration")
    result = {**_DEFAULTS, **value}
    for name, low, high in (("rate", 1000, 2e6), ("frequency", 1e6, 6e9), ("gain", 0, 10),
                            ("startup_timeout", 1, 20)):
        number = result[name]
        if type(number) not in (int, float) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f"invalid receive {name}")
    for name, low, high in (("samples", 64, 65536), ("channel", 0, 1)):
        number = result[name]
        if type(number) is not int or not low <= number <= high:
            raise ValueError(f"invalid receive {name}")
    return result


def serial_selector(value):
    if type(value) is not dict or set(value) != {"serial"} or type(value["serial"]) is not str:
        raise ValueError("one explicit USRP serial is required")
    if not SERIAL.fullmatch(value["serial"]):
        raise ValueError("invalid USRP serial")
    return value["serial"]


class Probe:
    def __init__(self, context):
        self._context = context
        self._process_id = None
        self._ready = None
        self._stopped = False

    def validate(self, profile, value):
        return configuration(profile, value)

    def _process(self):
        rows = self._context.processes.snapshot()["processes"]
        row = next((row for row in rows if row["id"] == self._process_id), None)
        if row is None or row["returncode"] is not None:
            raise RuntimeError("receive probe process is no longer running")
        return row

    def start(self, profile, value):
        value = self.validate(profile, value)
        serial = serial_selector(self._context.selector)
        if self._context.processes is None or not Path(self._context.work_dir).is_absolute():
            raise RuntimeError("owned process supervision is required")
        if self._process_id is not None or self._stopped:
            raise RuntimeError("receive probe has already started or stopped")
        if self._context.cancelled.is_set():
            raise RuntimeError("receive probe was cancelled")
        self._process_id = self._context.processes.launch(
            [str(Path(sys.executable).absolute()), "-I", "-u", "-m", "rrf_example_usrp_probe.worker",
             "--serial", serial, "--configuration", json.dumps(value, separators=(",", ":"))],
            env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "OPENBLAS_NUM_THREADS": "1",
                 "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"},
            cwd=Path(self._context.work_dir).absolute(), required_while_running=True)
        deadline = time.monotonic() + value["startup_timeout"]
        while time.monotonic() < deadline:
            if self._context.cancelled.is_set():
                raise RuntimeError("receive probe was cancelled")
            self._process()
            tail = self._context.processes.log_tail(self._process_id, "stdout")[-16384:]
            for line in tail.decode("utf-8", "replace").splitlines():
                if not line.startswith(MARKER) or len(line) > 4096:
                    continue
                try:
                    ready = json.loads(line[len(MARKER):])
                except ValueError:
                    continue
                if (type(ready) is dict and ready.get("event") == "ready" and ready.get("serial") == serial
                        and type(ready.get("received")) is int and ready["received"] >= value["samples"]):
                    self._ready = {name: ready[name] for name in
                        ("serial", "received", "rate", "frequency", "gain", "channel", "uhd_version")}
                    return
            time.sleep(0.02)
        raise RuntimeError("receive probe did not reach readiness")

    def status(self):
        if self._stopped:
            return {"ready": False, "stopped": True}
        if self._ready is None:
            return {"ready": False}
        self._process()
        return {"ready": True, **self._ready}

    def call(self, action, parameters):
        if action != "health" or type(parameters) is not dict or parameters:
            raise ValueError("health accepts no parameters")
        return self.status()

    def stop(self):
        if self._context.processes is None:
            self._stopped = True
            return
        if not self._context.processes.stop(timeout=5.0):
            raise RuntimeError("receive probe cleanup could not be verified")
        self._stopped = True


def create_adapter(context):
    return Probe(context)
