import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import dev_smoke
import pytest


def test_direct_rx_retries_path_timeout_once(monkeypatch):
    path = SimpleNamespace(calls=0, describe=lambda: "direct test path")
    direct_path = SimpleNamespace(
        global_path=Mock(side_effect=[None, path]),
        stop=Mock(),
        describe=lambda: "test path timed out",
    )
    core = ModuleType("remoterf.core")
    core.direct_path = direct_path
    remoterf = ModuleType("remoterf")
    remoterf.core = core
    monkeypatch.setitem(sys.modules, "remoterf", remoterf)
    monkeypatch.setitem(sys.modules, "remoterf.core", core)
    monkeypatch.setattr(dev_smoke.time, "monotonic", Mock(side_effect=[0, 16, 16]))

    def tone(sdr, n):
        path.calls += 2
        return f"rx {n} ok"

    monkeypatch.setattr(dev_smoke, "tone", tone)
    sdr = SimpleNamespace(ip=Mock())

    ctx = {"sdr": sdr, "res": {"token": "token"}}
    assert dev_smoke.direct_rx(ctx) == "rx 4096 ok over direct test path"
    assert sdr.ip.call_count == 2
    assert direct_path.global_path.call_count == 2
    assert direct_path.stop.call_count == 2

    direct_path.global_path = Mock(side_effect=[None, None])
    direct_path.stop.reset_mock()
    sdr.ip.reset_mock()
    monkeypatch.setattr(dev_smoke.time, "monotonic", Mock(side_effect=[0, 16, 16, 32]))

    with pytest.raises(dev_smoke.Fail, match="no direct path in 15 s: test path timed out"):
        dev_smoke.direct_rx(ctx)
    assert sdr.ip.call_count == 2
    assert direct_path.global_path.call_count == 2
    assert direct_path.stop.call_count == 2
