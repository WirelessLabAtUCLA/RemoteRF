"""`getdev` on a gRPC home: the home's devices first, partner labs after,
grouped by lab; server ids stay internal."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import remoterf.core.app as client_app
from remoterf.common.utils import map_arg


def test_native_rows_group_partner_devices_by_lab_after_the_home():
    response = SimpleNamespace(results={
        "16": map_arg("PlutoSDR (online)"),
        "1000001": map_arg("Pluto SDR (OTA) @ucla-deva (offline)"),
        "1000000": map_arg("USRP B210 @ucla-deva (online)"),
        "2": map_arg("HackRF (online, reserved)"),
    })
    fake = Mock(is_https_home=False)
    fake.get_devices.return_value = response
    with patch.object(client_app, "account", fake), patch.object(client_app, "home_name", "ucla"), \
         patch.object(client_app, "current_route", None):
        rows = client_app._native_device_rows()
    assert [(r["lab"], r["name"], r["state"], r["id"]) for r in rows] == [
        ("ucla", "HackRF", "online, reserved", 2),
        ("ucla", "PlutoSDR", "online", 16),
        ("ucla-deva", "Pluto SDR (OTA)", "offline", 1000001),
        ("ucla-deva", "USRP B210", "online", 1000000),
    ]


def test_enroll_strips_the_lab_prefix_from_a_pasted_code():
    from types import SimpleNamespace
    fake = Mock(is_https_home=False)
    fake.set_enroll.return_value = SimpleNamespace(results={"UC": map_arg("ok")})
    with patch.object(client_app, "account", fake), patch.object(client_app, "home_name", "otheruni"):
        assert client_app.enroll("otheruni.global.remoterf.net/cluster.875IO19HSA") is True
        assert fake.enrollment_code == "cluster.875IO19HSA"
        # A code for a different lab is refused here rather than sent.
        assert client_app.enroll("ucla/ABC123") is False
        assert fake.enrollment_code == "cluster.875IO19HSA"


def test_many_devices_across_labs_ask_for_a_lab_first():
    rows = [{"id": i, "name": f"d{i}", "lab": "ucla" if i < 8 else "otheruni", "state": "online", "away": i >= 8}
            for i in range(12)]
    with patch.object(client_app, "session") as session:
        session.prompt.return_value = "2"
        kept = client_app._pick_lab(rows)
    assert {r["lab"] for r in kept} == {"otheruni"} and len(kept) == 4
    with patch.object(client_app, "session") as session:
        session.prompt.return_value = ""
        assert client_app._pick_lab(rows) == rows  # Enter keeps everything
    # Few devices, or a single lab: no extra step at all.
    with patch.object(client_app, "session") as session:
        assert client_app._pick_lab(rows[:5]) == rows[:5]
        assert client_app._pick_lab([r for r in rows if r["lab"] == "ucla"] * 2) is not None
        session.prompt.assert_not_called()


def test_a_handle_in_brackets_stays_part_of_a_local_name():
    label = "Pluto SDR [Pl)ut(o] (online, reserved)"
    assert not client_app._PARTNER_LABEL.match(label)
    local = client_app._LOCAL_LABEL.match(label)
    assert (local["name"], local["state"]) == ("Pluto SDR [Pl)ut(o]", "online, reserved")


def test_enroll_through_global_names_the_password_and_a_working_login_command():
    fake = Mock(is_https_home=True, username="jdoe")
    fake.backend.enroll.return_value = {"status": "enrolled", "lab": {"linked": True, "slug": "ucla"}}
    asked, notes = [], []
    with patch.object(client_app, "account", fake), patch.object(client_app, "home_name", "global"), \
         patch.object(client_app, "_ask", side_effect=lambda text, **kw: asked.append(text) or "pw-123456"), \
         patch.object(client_app, "_note", side_effect=lambda text, *a, **kw: notes.append(text)), \
         patch.object(client_app, "_sync_labs"):
        assert client_app.enroll("ucla/ABC123") is not False
    assert "RemoteRF Global password" in asked[0] and " ucla " in asked[0] and ".global.remoterf.net" not in asked[0]
    assert fake.backend.enroll.call_args.kwargs["password"] == "pw-123456"
    assert any("remoterf --login ucla" in n for n in notes) and not any("-l jdoe@ucla" in n for n in notes)


def test_enroll_prompt_names_a_non_global_lab_by_its_saved_name():
    fake = Mock(is_https_home=True, username="jdoe")
    fake.backend.enroll.return_value = {"status": "enrolled", "lab": {}}
    asked = []
    with patch.object(client_app, "account", fake), patch.object(client_app, "home_name", "global"), \
         patch.object(client_app, "_ask", side_effect=lambda text, **kw: asked.append(text) or ""), \
         patch.object(client_app, "_note"), patch.object(client_app, "_sync_labs"):
        client_app.enroll("rf.lab.example.edu:8443/ABC123")
    assert " rf-lab-example-edu " in asked[0] and " rf " not in asked[0]
