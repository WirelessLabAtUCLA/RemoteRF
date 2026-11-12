"""Convenience wrapper for application integrations on an owning USRP root.

Wraps an existing ``MultiUSRP`` (never a token) and its seven
``integration_*`` methods::

    with OaiGnb(radio) as gnb:          # starts, waits for RUNNING, stops on exit
        print(gnb.health())

Before ``start`` set configuration as attributes (``app.band = 41``). Declared
actions are methods (``app.capture(n=4096)``). Every start/call request key is
generated once and reused when the RPC is retried after a lost reply.
"""
from __future__ import annotations

import base64
import json
import time
import uuid

import numpy as np

# Must not exceed remoterf_device_core.integrations.artifacts.MAX_FETCH_BYTES.
MAX_FETCH_BYTES = 384 * 1024
POLL_S = 0.2
# Longer than the server's largest per-integration action timeout (600 s).
CALL_WAIT_S = 660.0
_DONE = {"SUCCEEDED", "ERROR", "CANCELLED"}


class IntegrationError(RuntimeError):
    def __init__(self, code, message=None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class App:
    def __init__(self, radio, integration_id, profile, **config):
        self.__dict__.update(_radio=radio, _id=integration_id, _profile=profile,
                             _config=dict(config), _start=None, _op=None, _actions=None)

    def __setattr__(self, name, value):
        if name.startswith("_") or hasattr(type(self), name):
            raise AttributeError(f"{name} is reserved; pass it to the constructor instead")
        if self._start is not None:
            raise AttributeError("configuration is fixed once start was requested")
        self._config[name] = value

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._config:
            return self._config[name]
        if name in self.actions:
            return lambda **params: self.call(name, **params)
        raise AttributeError(name)

    @property
    def actions(self):
        if self._actions is None:
            described = self._rpc("integration_describe", {"integration_id": self._id})
            self.__dict__["_actions"] = tuple(described["integration"]["actions"])
        return self._actions

    @property
    def operation_id(self):
        return self._op

    def _rpc(self, method, request):
        """A server refusal carries details.integration_code; raise it as IntegrationError."""
        try:
            return getattr(self._radio, method)(request)
        except Exception as exc:
            details = getattr(exc, "details", None)
            code = details.get("integration_code") if isinstance(details, dict) else None
            if code:
                raise IntegrationError(code, str(exc)) from exc
            raise

    def _send(self, method, request):
        # Same request (and key) on retry: the server attaches to the recorded outcome.
        # Only transport failures are retried; a refusal would just be refused again.
        try:
            return self._rpc(method, request)
        except IntegrationError:
            raise
        except Exception:
            return self._rpc(method, request)

    def _poll(self, request, done, timeout_s):
        deadline = time.monotonic() + timeout_s
        while True:
            snapshot = self._rpc("integration_status", request)
            if done(snapshot):
                return snapshot
            if time.monotonic() >= deadline:
                raise TimeoutError(f"integration did not finish within {timeout_s} s")
            time.sleep(POLL_S)

    def start(self, timeout_s=120.0, wait=True):
        if self._start is None:
            generation = self._rpc("integration_list", {})["generation"]
            self.__dict__["_start"] = {"integration_id": self._id, "profile": self._profile,
                                       "configuration": dict(self._config),
                                       "request_key": uuid.uuid4().hex, "generation": generation}
        snapshot = self._send("integration_start", self._start)
        self.__dict__["_op"] = snapshot["operation_id"]
        if not wait:
            return snapshot
        snapshot = self._poll({"operation_id": self._op},
                              lambda s: s["state"] != "STARTING", timeout_s)
        if snapshot["state"] != "RUNNING":
            raise IntegrationError(snapshot.get("error") or snapshot["state"])
        return snapshot

    def status(self):
        return self._rpc("integration_status", {"operation_id": self._require()})

    def _require(self):
        if self._op is None:
            raise IntegrationError("not_started")
        return self._op

    def call(self, action, /, **params):
        operation_id = self._require()
        request = {"operation_id": operation_id, "action": action,
                   "parameters": params, "request_key": uuid.uuid4().hex}
        snapshot = self._send("integration_call", request)
        snapshot = self._poll({"operation_id": operation_id, "action_id": snapshot["action_id"]},
                              lambda s: s["state"] in _DONE, CALL_WAIT_S)
        if snapshot["state"] != "SUCCEEDED":
            raise IntegrationError(snapshot.get("error") or snapshot["state"])
        return self._decode(snapshot["result"])

    def _decode(self, result):
        """An "artifact" entry is fetched and decoded in place; alone, it is returned directly."""
        artifact = result.get("artifact") if isinstance(result, dict) else None
        if not isinstance(artifact, dict) or "name" not in artifact:
            return result
        data = self.fetch(artifact["name"])
        if "dtype" in artifact:
            value = np.frombuffer(data, dtype=artifact["dtype"]).reshape(artifact["shape"])
        elif artifact.get("format") == "json":
            value = json.loads(data)
        elif artifact.get("format") in {"bytes", "pcap"}:
            value = data
        else:
            return result
        return value if len(result) == 1 else {**result, "artifact": value}

    def fetch(self, name):
        operation_id, data = self._require(), bytearray()
        while True:
            chunk = self._rpc("integration_fetch", {"operation_id": operation_id, "artifact": name,
                                                "offset": len(data), "limit": MAX_FETCH_BYTES})
            raw = base64.b64decode(chunk["data"])
            data += raw
            if chunk["eof"]:
                return bytes(data)
            if not raw:
                raise IntegrationError("fetch_stalled")

    def stop(self, timeout_s=120.0):
        if self._op is None:
            return None
        self._rpc("integration_stop", {"operation_id": self._op})
        return self._poll({"operation_id": self._op}, lambda s: s["released"], timeout_s)

    def __enter__(self):
        if self._op is None:
            self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


class OaiGnb(App):
    def __init__(self, radio, **config):
        super().__init__(radio, "org.remoterf.oai", "gnb", **config)

    start_cell = App.start
    stop_cell = App.stop


class OaiUe(App):
    def __init__(self, radio, **config):
        super().__init__(radio, "org.remoterf.oai", "ue", **config)

    attach = App.start
    detach = App.stop
