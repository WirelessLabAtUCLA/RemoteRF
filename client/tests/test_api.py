"""The top-level reservation API over the fake homes of test_ops: default
lab, slot checks, cancel by dict or id, the cached session, no stdout."""

from datetime import timedelta

import pytest

import remoterf
from remoterf import api, ops

from test_ops import DAY, DEP, home, lab  # noqa: F401 - fixtures


@pytest.fixture
def session(monkeypatch, lab):  # noqa: F811
    """`lab` as the saved login; `loads` counts saved_session() calls."""
    loads = []

    def saved():
        loads.append(1)
        return lab

    monkeypatch.setattr(ops, "saved_session", saved)
    find_slots = ops.find_slots
    monkeypatch.setattr(ops, "find_slots", lambda s, h, **kw: find_slots(s, h, now=DAY.astimezone(), **kw))
    monkeypatch.setattr(api, "_session", None)
    monkeypatch.setattr(api, "_default", None)
    monkeypatch.delenv("REMOTERF_DEPLOYMENT", raising=False)
    lab.loads = loads
    return lab


def _code(call, *args, **kwargs):
    with pytest.raises(ops.RemoteRFOpsError) as raised:
        call(*args, **kwargs)
    return raised.value


def test_a_bare_name_takes_use_then_env_then_the_lab_home(session, monkeypatch):
    seen = []
    monkeypatch.setattr(ops, "find_slots", lambda s, h, **kw: seen.append(h))
    remoterf.slots("Pluto_A")
    monkeypatch.setenv("REMOTERF_DEPLOYMENT", "MIT")
    remoterf.slots("Pluto_A")
    remoterf.use("Caltech")
    remoterf.slots("Pluto_A")
    remoterf.slots("Pluto_A@ucla")
    remoterf.use(None)
    remoterf.slots("Pluto_A")
    assert seen == ["Pluto_A@ucla", "Pluto_A@mit", "Pluto_A@caltech", "Pluto_A@ucla", "Pluto_A@mit"]

    monkeypatch.delenv("REMOTERF_DEPLOYMENT")
    session.home_kind, session.home = "global", "global"
    err = _code(remoterf.slots, "Pluto_A")
    assert err.code == "invalid_argument" and "Name@deployment" in err.message and "remoterf.use(" in err.message


def test_reserve_minutes_books_consecutive_slots_and_keeps_full_handles(session):
    start = DAY.replace(hour=14).astimezone()
    out = remoterf.reserve("Pluto_A", start, minutes=120)
    assert (out["handle"], out["reservation_id"]) == ("Pluto_A@ucla", "9")
    assert out["end_ts"] - out["start_ts"] == 7200 and out["start_ts"] == int(start.timestamp())
    assert _code(remoterf.reserve, "Pluto_A", start).code == "invalid_argument"
    assert _code(remoterf.reserve, "Pluto_A", start, start + timedelta(hours=1), minutes=60).code == "invalid_argument"


def test_reserve_checks_the_grid_before_asking_the_lab(session, monkeypatch):
    calls = []
    monkeypatch.setattr(session.account, "reserve_device", lambda *a: calls.append(a))

    err = _code(remoterf.reserve, "Pluto_A", DAY.replace(hour=14, minute=30), minutes=60)
    assert err.code == "invalid_argument" and "60-minute" in err.message and "next free one" in err.message
    err = _code(remoterf.reserve, "Pluto_A", DAY.replace(hour=11), minutes=120)  # 12:00 is taken
    assert err.code == "slot_unavailable" and "60-minute" in err.message and "next free one" in err.message
    assert calls == []


def test_cancel_takes_a_dict_or_an_id(session, home, monkeypatch):  # noqa: F811
    assert remoterf.cancel({"reservation_id": "7"}) == {"cancelled": True, "reservation_id": "7"}
    assert remoterf.cancel("7@ucla")["reservation_id"] == "7"
    assert _code(remoterf.cancel, {"reservation_id": None}).code == "invalid_argument"
    seen = []
    monkeypatch.setattr(api, "_session", home)
    monkeypatch.setattr(home.account.backend, "cancel", seen.append)
    remoterf.cancel("10@ucla"), remoterf.cancel({"reservation_id": f"{DEP}:11"})
    assert seen == [f"{DEP}:10", f"{DEP}:11"]


def test_the_session_loads_once_and_reloads_once_when_expired(session, monkeypatch):
    remoterf.whoami(), remoterf.reservations()
    assert len(session.loads) == 1
    answers = [ops.RemoteRFOpsError("session_expired"), {"me": 1}]

    def whoami(s):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(ops, "whoami", whoami)
    assert remoterf.whoami() == {"me": 1} and len(session.loads) == 2
    monkeypatch.setattr(ops, "whoami", lambda s: (_ for _ in ()).throw(ops.RemoteRFOpsError("session_expired")))
    assert _code(remoterf.whoami).code == "session_expired" and len(session.loads) == 3


def test_no_saved_login_propagates(monkeypatch):
    monkeypatch.setattr(api, "_session", None)
    monkeypatch.setattr(ops, "saved_session", lambda: (_ for _ in ()).throw(ops.RemoteRFOpsError("not_logged_in")))
    assert _code(remoterf.whoami).code == "not_logged_in"


def test_nothing_reaches_stdout(session, capsys):
    remoterf.whoami(), remoterf.list_devices(), remoterf.slots("Pluto_A"), remoterf.reservations()
    out = remoterf.reserve("Pluto_A", DAY.replace(hour=20).astimezone() + timedelta(days=1), minutes=60)
    remoterf.cancel(out)
    with pytest.raises(remoterf.RemoteRFOpsError):
        remoterf.slots("Nope")
    assert capsys.readouterr().out == ""


def test_session_reload_is_shared_across_threads(monkeypatch):
    import threading
    from remoterf import api, ops

    loads = []
    monkeypatch.setattr(api, "_session", None)
    monkeypatch.setattr(ops, "saved_session", lambda: loads.append(1) or object())
    seen = []
    barrier = threading.Barrier(8)

    def work():
        barrier.wait()
        seen.append(api._call(lambda s: s))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(loads) == 1 and len(set(map(id, seen))) == 1
