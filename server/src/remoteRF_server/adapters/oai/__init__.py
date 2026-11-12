"""gNB-only OAI scaffold; this adapter TRANSMITS (gNB).

The operator-supplied executable and configuration are not managed by this
adapter; the operator installs and reviews them.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import time


SERIAL = re.compile(r"[A-Za-z0-9]{1,64}")
EXTRA_ARGS = ()
READY_MARKER = "ALL RUs ready"
STARTUP_TIMEOUT = 25.0


def serial_selector(value):
    if type(value) is not dict or set(value) != {"serial"} or type(value["serial"]) is not str:
        raise ValueError("one explicit USRP serial is required")
    if not SERIAL.fullmatch(value["serial"]):
        raise ValueError("invalid USRP serial")
    return value["serial"]


def _operator_file(name):
    value = os.environ.get(name)
    path = Path(value) if value is not None else Path()
    if value is None or not path.is_absolute() or not path.is_file():
        raise RuntimeError(f"{name} must name an absolute existing regular file")
    return str(path)


class OAI:
    def __init__(self, context):
        self._context = context
        self._process_id = None
        self._ready = False
        self._stopped = False

    def validate(self, profile, configuration):
        if profile != "gnb" or type(configuration) is not dict or configuration:
            raise ValueError("unsupported gNB configuration")
        return {}

    def _process(self):
        rows = self._context.processes.snapshot()["processes"]
        row = next((row for row in rows if row["id"] == self._process_id), None)
        if row is None or row["returncode"] is not None:
            raise RuntimeError("OAI gNB process is no longer running")
        return row

    def start(self, profile, configuration):
        self.validate(profile, configuration)
        serial = serial_selector(self._context.selector)
        processes = self._context.processes
        work_dir = Path(self._context.work_dir)
        if processes is None or not work_dir.is_absolute():
            raise RuntimeError("owned process supervision is required")
        if self._process_id is not None or self._stopped:
            raise RuntimeError("OAI gNB has already started or stopped")
        if self._context.cancelled.is_set():
            raise RuntimeError("OAI gNB was cancelled")

        softmodem = _operator_file("REMOTERF_OAI_SOFTMODEM")
        conf = _operator_file("REMOTERF_OAI_GNB_CONF")
        env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        if "UHD_IMAGES_DIR" in os.environ:
            env["UHD_IMAGES_DIR"] = os.environ["UHD_IMAGES_DIR"]

        # EXTRA_ARGS, READY_MARKER, and sdr_addrs require verification against the pinned OAI build.
        argv = [softmodem, "-O", conf, *EXTRA_ARGS,
                "--RUs.[0].sdr_addrs", f"serial={serial}"]
        self._process_id = processes.launch(
            argv, env=env, cwd=work_dir, required_while_running=True)
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if self._context.cancelled.is_set():
                raise RuntimeError("OAI gNB was cancelled")
            self._process()
            tail = processes.log_tail(self._process_id, "stdout")[-65536:]
            if READY_MARKER in tail.decode("utf-8", errors="replace"):
                self._ready = True
                return
            time.sleep(0.1)
        raise RuntimeError("OAI gNB did not reach readiness")

    def status(self):
        if self._stopped:
            return {"ready": False, "stopped": True}
        if not self._ready:
            return {"ready": False}
        self._process()
        return {"ready": True}

    def call(self, action, parameters):
        if action != "health" or type(parameters) is not dict or parameters:
            raise ValueError("health accepts no parameters")
        return self.status()

    def stop(self):
        if self._stopped:
            return
        if self._context.processes is None:
            self._stopped = True
            return
        if not self._context.processes.stop(timeout=10.0):
            raise RuntimeError("OAI gNB cleanup could not be verified")
        self._stopped = True


def create_adapter(context):
    return OAI(context)
