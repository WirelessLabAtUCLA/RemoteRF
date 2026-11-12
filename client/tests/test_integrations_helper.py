import base64
import json

import numpy as np
import pytest

from remoterf import integrations
from remoterf.integrations import App, IntegrationError, OaiGnb, OaiUe


class FakeRadio:
    """In-memory integration_* dict API; no network."""

    def __init__(self, actions=("health", "capture", "dump", "load_l1_plugin"), fail_once=()):
        self.actions = list(actions)
        self.fail_once = set(fail_once)
        self.requests = []
        self.starts = {}
        self.calls = {}
        self.artifacts = {}
        self.start_state = "RUNNING"
        self.released = False

    def _record(self, method, request):
        self.requests.append((method, request))
        if method in self.fail_once:
            self.fail_once.discard(method)
            raise ConnectionError("lost reply")

    def integration_list(self, request):
        self._record("integration_list", request)
        return {"generation": 3, "integrations": []}

    def integration_describe(self, request):
        self._record("integration_describe", request)
        return {"integration": {"id": request["integration_id"], "actions": self.actions}}

    def integration_start(self, request):
        self._record("integration_start", request)
        op = self.starts.setdefault(request["request_key"], f"op-{len(self.starts)}")
        return {"operation_id": op, "state": "STARTING"}

    def integration_status(self, request):
        self._record("integration_status", request)
        if "action_id" in request:
            action, params = self.calls[request["action_id"]]
            if action == "fail":
                return {"state": "ERROR", "error": "action_failed", "result": {}}
            return {"state": "SUCCEEDED", "error": None, "result": self.result(action, params)}
        return {"operation_id": request["operation_id"], "state": self.start_state,
                "error": "process_exited" if self.start_state == "ERROR" else None,
                "released": self.released}

    def result(self, action, params):
        if action == "capture":
            self.artifacts["iq.bin"] = np.arange(params["n"], dtype=np.complex64).tobytes()
            return {"artifact": {"name": "iq.bin", "dtype": "complex64", "shape": [params["n"]]}}
        if action == "dump":
            self.artifacts["x.json"] = json.dumps({"ok": 1}).encode()
            return {"returncode": 0, "artifact": {"name": "x.json", "format": "json"}}
        return {"action": action, "params": params}

    def integration_call(self, request):
        self._record("integration_call", request)
        action_id = f"a-{request['request_key']}"
        self.calls[action_id] = (request["action"], request["parameters"])
        return {"action_id": action_id, "state": "QUEUED"}

    def integration_fetch(self, request):
        self._record("integration_fetch", request)
        data = self.artifacts[request["artifact"]]
        chunk = data[request["offset"]:request["offset"] + min(request["limit"], 1000)]
        return {"data": base64.b64encode(chunk).decode(),
                "eof": request["offset"] + len(chunk) >= len(data)}

    def integration_stop(self, request):
        self._record("integration_stop", request)
        self.released = True
        return {"operation_id": request["operation_id"], "state": "STOPPING"}


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(integrations, "POLL_S", 0)


def test_lifecycle_config_actions_and_artifacts():
    radio = FakeRadio()
    app = App(radio, "org.example.app", "receive", rate=1)
    app.band = 41
    assert app.band == 41
    with pytest.raises(AttributeError):
        app.status = 1

    with app:
        start = next(r for m, r in radio.requests if m == "integration_start")
        assert start["configuration"] == {"rate": 1, "band": 41} and start["generation"] == 3
        with pytest.raises(AttributeError):
            app.band = 7
        assert app.health(x=1) == {"action": "health", "params": {"x": 1}}
        iq = app.capture(n=600)
        assert iq.dtype == np.complex64 and np.array_equal(iq, np.arange(600, dtype=np.complex64))
        assert app.dump() == {"returncode": 0, "artifact": {"ok": 1}}
        with pytest.raises(AttributeError):
            app.not_declared
        with pytest.raises(IntegrationError, match="action_failed"):
            app.call("fail")
    assert radio.released
    assert all(r["limit"] == integrations.MAX_FETCH_BYTES
               for m, r in radio.requests if m == "integration_fetch")


def test_request_keys_reused_on_retry():
    radio = FakeRadio(fail_once={"integration_start", "integration_call"})
    app = App(radio, "org.example.app", "receive")
    app.start()
    app.call("health")
    for method in ("integration_start", "integration_call"):
        keys = [r["request_key"] for m, r in radio.requests if m == method]
        assert len(keys) == 2 and keys[0] == keys[1]
    assert len(radio.starts) == 1


def test_start_failure_raises_error_code():
    radio = FakeRadio()
    radio.start_state = "ERROR"
    with pytest.raises(IntegrationError) as err:
        App(radio, "org.example.app", "receive").start()
    assert err.value.code == "process_exited"


def test_oai_subclasses_and_aliases():
    radio = FakeRadio()
    gnb = OaiGnb(radio)
    gnb.start_cell()
    assert radio.requests[-2][1]["profile"] == "gnb"
    assert gnb.load_l1_plugin(stage="pre_fft", c_code="int x;")["params"]["stage"] == "pre_fft"
    gnb.stop_cell()
    ue = OaiUe(FakeRadio())
    ue.attach(wait=False)
    assert ue._start["integration_id"] == "org.remoterf.oai" and ue._start["profile"] == "ue"
    ue.detach()
