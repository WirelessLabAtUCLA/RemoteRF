import json
import socket
import sqlite3
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from remoteRF_server.server import control


def _insert_reservation(handler, *, username="alice", ended=False, federated=False):
    now = datetime.now()
    start = now - timedelta(hours=2 if ended else 1)
    end = now - timedelta(hours=1) if ended else now + timedelta(hours=1)
    with sqlite3.connect(handler.filepath) as conn:
        res_id = conn.execute(
            "INSERT INTO Reservations (username, device_id, start_time, end_time, salt, hash) "
            "VALUES (?, 7, ?, ?, 'salt', 'hash')",
            (username, start, end),
        ).lastrowid
        if federated:
            conn.execute(
                "INSERT INTO FederatedReservations "
                "(res_id, issuer_deployment_id, remote_subject_id, local_account_id, contract_id, "
                "contract_version, request_id, created_at) VALUES (?, ?, ?, 1, ?, 1, ?, 1)",
                (res_id,
                 "11111111-1111-4111-8111-111111111111",
                 "22222222-2222-4222-8222-222222222222",
                 "33333333-3333-4333-8333-333333333333",
                 "44444444-4444-4444-8444-444444444444"),
            )
    return res_id


def _rows(handler, table):
    with sqlite3.connect(handler.filepath) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY res_id").fetchall()


def _control_round_trip(handler, argv):
    server_socket, client_socket = socket.socketpair()
    daemon = control.ControlServer(handler)
    thread = threading.Thread(target=daemon._handle, args=(server_socket,))
    thread.start()
    try:
        client_socket.sendall((json.dumps({"argv": argv}) + "\n").encode())
        reply = json.loads(client_socket.makefile("rb").readline())
    finally:
        client_socket.close()
        server_socket.close()
        thread.join()
    return int(reply["rc"]), str(reply["out"])


def test_cancel_over_control_socket_removes_exactly_one_and_releases_device(tmp_path, monkeypatch):
    from remoteRF_server.server import grpc_server, reservation

    monkeypatch.setattr(reservation, "get_db_dir", lambda: tmp_path)
    handler = reservation.ReservationHandler()
    target = _insert_reservation(handler, federated=True)
    other = _insert_reservation(handler, username="jdoe")
    released = mock.Mock()
    monkeypatch.setattr(handler, "update_devices", released)
    monkeypatch.setattr(grpc_server, "reservation_handler", handler)
    rc, output = _control_round_trip(
        grpc_server.control_handler, ["reservations", "cancel", str(target), "-y"]
    )

    assert rc == 0
    assert output.count("\n") == 1
    assert output.startswith(f"{target} alice 7 ")
    assert [row[0] for row in _rows(handler, "Reservations")] == [other]
    assert _rows(handler, "FederatedReservations") == []
    released.assert_called_once_with()


def test_missing_or_ended_reservation_returns_one_and_changes_nothing(tmp_path, monkeypatch):
    from remoteRF_server.server import grpc_server, reservation

    monkeypatch.setattr(reservation, "get_db_dir", lambda: tmp_path)
    handler = reservation.ReservationHandler()
    ended = _insert_reservation(handler, ended=True, federated=True)
    before = (_rows(handler, "Reservations"), _rows(handler, "FederatedReservations"))
    released = mock.Mock()
    monkeypatch.setattr(handler, "update_devices", released)
    monkeypatch.setattr(grpc_server, "reservation_handler", handler)
    ended_reply = _control_round_trip(
        grpc_server.control_handler, ["reservations", "cancel", str(ended)]
    )
    missing_reply = _control_round_trip(
        grpc_server.control_handler, ["reservations", "cancel", "999999"]
    )

    assert ended_reply[0] == missing_reply[0] == 1
    assert "not found or has already ended" in ended_reply[1]
    assert "not found or has already ended" in missing_reply[1]
    assert (_rows(handler, "Reservations"), _rows(handler, "FederatedReservations")) == before
    released.assert_not_called()


def test_cluster_info_includes_member_ids(monkeypatch):
    from remoteRF_server.common.utils import panel
    from remoteRF_server.server.federation import cli, cluster

    owner = "11111111-1111-4111-8111-111111111111"
    member = "22222222-2222-4222-8222-222222222222"
    sharing = {"groups": 1, "detail": [{
        "name": "bench", "max_reservation_time_sec": 3600, "max_reservations": 1,
        "lifetime_sec": 0, "max_horizon_sec": 0, "devices": [],
    }]}
    monkeypatch.setattr(cli, "load_config", lambda: {"display_name": "Lab A", "identity_dir": "/unused"})
    monkeypatch.setattr(cli, "load_state", lambda path: SimpleNamespace(deployment_id=owner))
    monkeypatch.setattr(cluster, "list_clusters", lambda: [{
        "name": "shared", "role": "owner", "groups": [],
        "members": [{"display_name": "Lab B", "deployment_id": member, "sharing": sharing}],
    }])
    shown = {}
    monkeypatch.setattr(panel, "print_panel", lambda title, rows, **kwargs: shown.update(title=title, rows=rows))

    assert cli.cmd_cluster_info("shared") == 0
    text = "\n".join(shown["rows"])
    assert f"Lab A  admin  {owner}  (you)" in text
    assert f"Lab B  member  {member}" in text


def test_partner_lab_booking_and_oversized_id_are_refused(tmp_path, monkeypatch):
    from remoteRF_server.server import grpc_server, reservation

    monkeypatch.setattr(reservation, "get_db_dir", lambda: tmp_path)
    handler = reservation.ReservationHandler()
    home = _insert_reservation(handler)
    with sqlite3.connect(handler.filepath) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS HomeFederatedReservations (res_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO HomeFederatedReservations (res_id) VALUES (?)", (home,))
    before = _rows(handler, "Reservations")
    released = mock.Mock()
    monkeypatch.setattr(handler, "update_devices", released)
    monkeypatch.setattr(grpc_server, "reservation_handler", handler)

    rc, output = _control_round_trip(grpc_server.control_handler, ["reservations", "cancel", str(home)])
    assert rc == 3 and "partner lab" in output
    rc, _ = _control_round_trip(grpc_server.control_handler, ["reservations", "cancel", str(2**63)])
    assert rc == 2
    assert _rows(handler, "Reservations") == before
    released.assert_not_called()
