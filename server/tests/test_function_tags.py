# Copyright (C) 2026 RemoteRF
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID
from datetime import timezone
from remoterf_device_core.idl import (
    BLOCK_ALL_TAG,
    DeviceSchema,
    HOST_FUNCTION_TAGS_CAPABILITY,
    OLD_HOST_TAGS_ERROR,
    idl_expose,
)
from remoterf_device_core.v2_errors import RemoteRFPolicyError
from remoterf_federation_core import verify_eddsa_jwt

from remoteRF_server.common.grpc import grpc_pb2
from remoteRF_server.common.grpc.v2_codec import encode_value
from remoteRF_server.common.utils import map_arg, unmap_arg
from remoteRF_server.drivers.usrp.uhd_manifest import TARGET_UHD_VERSION, build_usrp_schema_v2
from remoteRF_server.drivers.usrp.usrp_schema import UsrpSchema
from remoteRF_server.host import host_tunnel_server as hts
from remoteRF_server.server import device_manager, host_direct, host_dynamic_v2, reservation
from remoteRF_server.server.dynamic_v2 import (
    DynamicControlV2Servicer,
    SampleDataV1Servicer,
    SessionManager,
)
from remoteRF_server.server.federation.identity import (
    DeploymentState,
    init_identity,
    load_private_key,
    save_state,
)
from remoteRF_server.server.rpc_manager import RpcManager
from remoteRF_server.server.user_group_handler import UserGroupHandler


class _TaggedRadioSchema(DeviceSchema):
    device_type = "tagged_radio"
    driver_version = "0.1.0"

    def __init__(self):
        super().__init__()
        self.tx_lo_val = 915_000_000
        self.tx_calls = []
        self.rx_calls = 0

    @idl_expose(kind="get", tag="TX")
    def get_tx_power(self) -> float:
        return -10.0

    @idl_expose(kind="set", tag="TX")
    def set_tx_lo(self, value: int) -> None:
        self.tx_lo_val = int(value)

    @idl_expose(kind="get")
    def get_rx_lo(self) -> int:
        return 915_000_000

    @idl_expose(kind="call", tag=("TX", "RF"))
    def call_tx(self, value) -> None:
        self.tx_calls.append(value)

    @idl_expose(kind="call")
    def call_rx(self) -> int:
        self.rx_calls += 1
        return self.rx_calls


class _FakeRXStreamer:
    def get_max_num_samps(self):
        return 256

    def recv(self, buffer, metadata, timeout):
        buffer[..., :2] = np.array([1 + 2j, 3 + 4j], dtype=buffer.dtype)
        metadata.error_code = "none"
        metadata.has_time_spec = False
        return 2


class _FakeTXStreamer:
    def __init__(self):
        self.sent = []

    def get_max_num_samps(self):
        return 256

    def send(self, samples, metadata, timeout):
        self.sent.append(samples.copy())
        return int(samples.shape[-1])


class _FakeMetadata:
    def __init__(self):
        self.error_code = "none"
        self.has_time_spec = False
        self.time_spec = None
        self.more_fragments = False
        self.fragment_offset = 0
        self.out_of_sequence = False


def _fake_uhd_module():
    return types.SimpleNamespace(
        types=types.SimpleNamespace(
            RXMetadata=_FakeMetadata,
            TXMetadata=_FakeMetadata,
            TXAsyncMetadata=None,
            StreamCMD=lambda mode: mode,
            StreamMode=types.SimpleNamespace(stop_cont="stop_cont"),
        )
    )


class _FakeLiveUsrp:
    def __init__(self):
        self.device = object()
        self.calls = []
        self.tx_streamer = _FakeTXStreamer()
        self.rx_streamer = _FakeRXStreamer()
        self.closed = False

    def build_session_metadata_v2(self, policy_restrictions=None):
        return {
            "hardware_profile": "usrp2901",
            "uhd_version": TARGET_UHD_VERSION,
            "uhd_abi": "test",
            "policy_restrictions": dict(policy_restrictions or {}),
        }

    def invoke_native(self, method, args, positional_args=None):
        self.calls.append((method, args, list(positional_args or ())))
        if method == "get_rx_stream":
            return self.rx_streamer
        if method == "get_tx_stream":
            return self.tx_streamer
        if method == "get_time_now":
            return 1.5
        if method == "send_waveform":
            return 4
        return None

    def close_native_device(self):
        self.closed = True


class _FakeInventoryUsrp:
    def __init__(self):
        self.live = None

    def get_idl_v2(self):
        return build_usrp_schema_v2()

    def create_session_schema(self):
        self.live = _FakeLiveUsrp()
        return self.live


class ReservationBlockedTagsTests(unittest.TestCase):
    def test_blocked_tags_for_holder_respects_roles_and_groups(self):
        handler = reservation.ReservationHandler.__new__(reservation.ReservationHandler)
        handler.get_uid = mock.Mock(return_value=7)

        fake_perms = mock.Mock()
        fake_groups = mock.Mock()
        fake_groups.get_users_blocked_tags.return_value = (True, ["TX"])

        with mock.patch.object(reservation, "perms", fake_perms), \
                mock.patch.object(reservation, "user_group_handler", fake_groups):
            # Normal user gets the group intersection.
            fake_perms.get_perms.return_value = [["Normal User"]]
            self.assertEqual(handler._blocked_tags_for_holder_locked("alice", 3), ("TX",))

            # Admin and Power User bypass group tag blacklists.
            fake_perms.get_perms.return_value = [["Admin"]]
            self.assertEqual(handler._blocked_tags_for_holder_locked("admin_u", 3), ())

            fake_perms.get_perms.return_value = [["Power User", 10, 3600, "[3]"]]
            self.assertEqual(handler._blocked_tags_for_holder_locked("power_u", 3), ())

            # Missing uid fails closed (blocks all tagged functions).
            handler.get_uid.return_value = -1
            fake_perms.get_perms.return_value = [["Normal User"]]
            self.assertEqual(handler._blocked_tags_for_holder_locked("ghost", 3), (BLOCK_ALL_TAG,))

            # No granting group fails closed.
            handler.get_uid.return_value = 7
            fake_groups.get_users_blocked_tags.return_value = (False, [])
            self.assertEqual(handler._blocked_tags_for_holder_locked("alice", 3), (BLOCK_ALL_TAG,))

            # Lookup exceptions fail closed and log.
            fake_groups.get_users_blocked_tags.side_effect = RuntimeError("db error")
            with self.assertLogs(reservation.logger, level="ERROR") as logs:
                self.assertEqual(handler._blocked_tags_for_holder_locked("alice", 3), (BLOCK_ALL_TAG,))
            self.assertTrue(any("failing closed" in msg for msg in logs.output))

            fake_groups.get_users_blocked_tags.side_effect = None
            fake_perms.get_perms.side_effect = RuntimeError("perms error")
            with self.assertLogs(reservation.logger, level="ERROR") as logs:
                self.assertEqual(handler._blocked_tags_for_holder_locked("alice", 3), (BLOCK_ALL_TAG,))
            self.assertTrue(any("failing closed" in msg for msg in logs.output))

    def test_blocked_tags_lan_only_group_does_not_lift_blacklist(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "usergroups.db"
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE UserGroups (
                    group_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL UNIQUE,
                    devices_whitelist TEXT NOT NULL DEFAULT '[]',
                    max_reservation_time_sec INTEGER NOT NULL DEFAULT 3600,
                    max_reservations INTEGER NOT NULL DEFAULT 1,
                    lifetime_sec INTEGER NOT NULL DEFAULT 0,
                    lan_only INTEGER NOT NULL DEFAULT 0,
                    max_horizon_sec INTEGER NOT NULL DEFAULT 604800,
                    blacklisted_tags TEXT NOT NULL DEFAULT '[]',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE UsersLookup (
                    user_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    PRIMARY KEY (user_id, group_id)
                );
                """
            )
            conn.execute(
                "INSERT INTO UserGroups (group_id, group_name, devices_whitelist, lan_only, blacklisted_tags) "
                "VALUES (1, 'remote-rx', '[3]', 0, '[\"TX\"]')"
            )
            conn.execute(
                "INSERT INTO UserGroups (group_id, group_name, devices_whitelist, lan_only, blacklisted_tags) "
                "VALUES (2, 'lan-full', '[3]', 1, '[]')"
            )
            conn.execute("INSERT INTO UsersLookup VALUES (10, 1), (10, 2), (11, 2)")

        groups = UserGroupHandler.__new__(UserGroupHandler)
        groups.filepath = db_path
        groups.lock = threading.RLock()

        handler = reservation.ReservationHandler.__new__(reservation.ReservationHandler)
        handler.get_uid = mock.Mock(side_effect=lambda u: 10 if u == "alice" else 11)
        fake_perms = mock.Mock()
        fake_perms.get_perms.return_value = [["Normal User"]]

        relayed_token = reservation.RELAYED.set(False)
        try:
            with mock.patch.object(reservation, "perms", fake_perms), \
                    mock.patch.object(reservation, "user_group_handler", groups):
                # LAN-only group without blacklist does NOT lift TX for alice.
                self.assertEqual(handler._blocked_tags_for_holder_locked("alice", 3), ("TX",))
                self.assertFalse(reservation.RELAYED.get())
                # User only in LAN-only group gets that group's own blacklist on LAN,
                # and still fails closed when evaluated as relayed_only (federated).
                self.assertEqual(handler._blocked_tags_for_holder_locked("bob", 3), ())
                self.assertEqual(handler._blocked_tags_for_holder_locked("bob", 3, relayed_only=True), (BLOCK_ALL_TAG,))
                self.assertFalse(reservation.RELAYED.get())
        finally:
            reservation.RELAYED.reset(relayed_token)

    def test_blocked_tags_lan_only_group_allowed_on_lan_but_not_for_federated(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "usergroups.db"
        res_db_path = Path(tmp.name) / "reservations.db"
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE UserGroups (
                    group_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL UNIQUE,
                    devices_whitelist TEXT NOT NULL DEFAULT '[]',
                    max_reservation_time_sec INTEGER NOT NULL DEFAULT 3600,
                    max_reservations INTEGER NOT NULL DEFAULT 1,
                    lifetime_sec INTEGER NOT NULL DEFAULT 0,
                    lan_only INTEGER NOT NULL DEFAULT 0,
                    max_horizon_sec INTEGER NOT NULL DEFAULT 604800,
                    blacklisted_tags TEXT NOT NULL DEFAULT '[]',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE UsersLookup (
                    user_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    PRIMARY KEY (user_id, group_id)
                );
                """
            )
            conn.execute(
                "INSERT INTO UserGroups (group_id, group_name, devices_whitelist, lan_only, blacklisted_tags) "
                "VALUES (1, 'remote-rx', '[3]', 0, '[\"TX\"]')"
            )
            conn.execute(
                "INSERT INTO UserGroups (group_id, group_name, devices_whitelist, lan_only, blacklisted_tags) "
                "VALUES (2, 'lan-full', '[3, 4]', 1, '[]')"
            )
            conn.execute(
                "INSERT INTO UserGroups (group_id, group_name, devices_whitelist, lan_only, blacklisted_tags) "
                "VALUES (3, 'lan-rx', '[5]', 1, '[\"TX\"]')"
            )
            conn.execute("INSERT INTO UsersLookup VALUES (10, 1), (10, 2), (11, 2), (12, 3)")

        groups = UserGroupHandler.__new__(UserGroupHandler)
        groups.filepath = db_path
        groups.lock = threading.RLock()

        now = datetime.now()
        with sqlite3.connect(res_db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE Reservations (
                    res_id INTEGER PRIMARY KEY, username TEXT, device_id INTEGER,
                    start_time TEXT, end_time TEXT, salt TEXT, hash TEXT
                );
                CREATE TABLE FederatedReservations (
                    res_id INTEGER PRIMARY KEY
                );
                CREATE TABLE RetiredTokens (
                    salt TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    retired_at INTEGER NOT NULL,
                    PRIMARY KEY (salt, hash)
                );
                """
            )
            start_s = (now - timedelta(minutes=1)).isoformat()
            end_s = (now + timedelta(minutes=10)).isoformat()
            # res 1: bob (LAN-only unrestricted) native LAN reservation on dev 3
            conn.execute("INSERT INTO Reservations VALUES (1, 'bob', 3, ?, ?, 'sb', 'hb')", (start_s, end_s))
            # res 2: bob_fed (linked account only in lan-full) federated reservation on dev 4
            conn.execute("INSERT INTO Reservations VALUES (2, 'bob', 4, ?, ?, 'sf', 'hf')", (start_s, end_s))
            conn.execute("INSERT INTO FederatedReservations VALUES (2)")
            # res 3: charlie (LAN-only TX-blacklisted) native LAN reservation on dev 5
            conn.execute("INSERT INTO Reservations VALUES (3, 'charlie', 5, ?, ?, 'sc', 'hc')", (start_s, end_s))
            # res 4: alice (remote-rx + lan-full) native/relayed reservation on dev 3 (skip conflict by using helper directly below)

        handler = reservation.ReservationHandler.__new__(reservation.ReservationHandler)
        handler.filepath = res_db_path
        handler.lock = threading.RLock()
        handler._clean_expired_reservations = mock.Mock()
        handler._account_security_state_locked = mock.Mock(return_value="native")
        uid_map = {"alice": 10, "bob": 11, "charlie": 12, "nobody": 13}
        handler.get_uid = mock.Mock(side_effect=lambda u: uid_map[u])
        fake_perms = mock.Mock()
        fake_perms.get_perms.return_value = [["Normal User"]]

        set_calls = []
        # Even if update_devices() runs while ambient RELAYED=True (e.g. triggered by a federated booking),
        # LAN holders must get their LAN-only group tags and federated holders must stay restricted.
        relayed_token = reservation.RELAYED.set(True)
        try:
            with mock.patch.object(reservation, "perms", fake_perms), \
                    mock.patch.object(reservation, "user_group_handler", groups), \
                    mock.patch.object(reservation, "get_all_devices", return_value={3: (object(), "", ""), 4: (object(), "", ""), 5: (object(), "", "")}), \
                    mock.patch.object(reservation, "get_registered_devices", return_value={3: (object(), "", ""), 4: (object(), "", ""), 5: (object(), "", "")}), \
                    mock.patch.object(reservation, "set_device", side_effect=lambda *a, **kw: set_calls.append((a, kw))):
                self.assertEqual(handler._blocked_tags_for_holder_locked("bob", 3), ())
                self.assertEqual(handler._blocked_tags_for_holder_locked("charlie", 5), ("TX",))
                self.assertEqual(handler._blocked_tags_for_holder_locked("alice", 3), ("TX",))
                self.assertEqual(handler._blocked_tags_for_holder_locked("nobody", 3), (BLOCK_ALL_TAG,))
                self.assertEqual(handler._blocked_tags_for_holder_locked("bob", 3, relayed_only=True), (BLOCK_ALL_TAG,))
                self.assertTrue(reservation.RELAYED.get())

                handler.update_devices()
        finally:
            reservation.RELAYED.reset(relayed_token)

        by_dev = {args[0]: kwargs for args, kwargs in set_calls}
        self.assertEqual(by_dev["3"], {"bypass": False})
        self.assertEqual(by_dev["4"], {"bypass": False, "blocked_tags": (BLOCK_ALL_TAG,)})
        self.assertEqual(by_dev["5"], {"bypass": False, "blocked_tags": ("TX",)})

    def test_parse_blacklisted_tags_fails_closed_on_malformed_json_and_invalid_entries(self):
        from remoteRF_server.server.user_group_handler import _parse_blacklisted_tags

        self.assertEqual(_parse_blacklisted_tags(None), [])
        self.assertEqual(_parse_blacklisted_tags(""), [])
        self.assertEqual(_parse_blacklisted_tags("   "), [])
        self.assertEqual(_parse_blacklisted_tags("[]"), [])
        self.assertEqual(_parse_blacklisted_tags('["TX", "tx", "Cal"]'), ["TX", "Cal"])
        self.assertEqual(_parse_blacklisted_tags(["TX", "Cal"]), ["TX", "Cal"])

        for malformed in (
            '["TX"',
            "TX,RX",
            "{bad",
            "{}",
            "123",
            '"TX"',
            "true",
            "null",
            "[null]",
            '[""]',
            '["   "]',
            "[123]",
            '["TX", null]',
            '["TX", 1]',
            '["TX", ""]',
            [None],
            [""],
            [42],
            ["TX", None],
            123,
            {"TX": True},
        ):
            self.assertEqual(_parse_blacklisted_tags(malformed), [BLOCK_ALL_TAG], repr(malformed))

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "usergroups.db"
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE UserGroups (
                    group_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL UNIQUE,
                    devices_whitelist TEXT NOT NULL DEFAULT '[]',
                    max_reservation_time_sec INTEGER NOT NULL DEFAULT 3600,
                    max_reservations INTEGER NOT NULL DEFAULT 1,
                    lifetime_sec INTEGER NOT NULL DEFAULT 0,
                    lan_only INTEGER NOT NULL DEFAULT 0,
                    max_horizon_sec INTEGER NOT NULL DEFAULT 604800,
                    blacklisted_tags TEXT NOT NULL DEFAULT '[]',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE UsersLookup (
                    user_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    PRIMARY KEY (user_id, group_id)
                );
                """
            )
            conn.execute(
                "INSERT INTO UserGroups (group_id, group_name, devices_whitelist, blacklisted_tags) "
                "VALUES (1, 'corrupt-json', '[3]', '[\"TX\"'), (2, 'corrupt-item', '[4]', '[null]')"
            )
            conn.execute("INSERT INTO UsersLookup VALUES (10, 1), (10, 2)")
        groups = UserGroupHandler.__new__(UserGroupHandler)
        groups.filepath = db_path
        groups.lock = threading.RLock()
        self.assertEqual(groups.get_users_blocked_tags(uid=10, device_id=3), (True, [BLOCK_ALL_TAG]))
        self.assertEqual(groups.get_users_blocked_tags(uid=10, device_id=4), (True, [BLOCK_ALL_TAG]))

    def test_malformed_devices_whitelist_cannot_lift_blacklisted_tags(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "usergroups.db"
        malformed_whitelists = (
            "{corrupt",
            "[3",
            "{}",
            "123",
            '"3"',
            "true",
            "null",
            "[null]",
            '[""]',
            '["   "]',
            "[true]",
            "[1.5]",
            '[3, "bad"]',
            '[3, null]',
            "",
            "   ",
            ",,",
            "3,",
            "abc",
            "3,abc",
        )
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE UserGroups (
                    group_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL UNIQUE,
                    devices_whitelist TEXT NOT NULL DEFAULT '[]',
                    max_reservation_time_sec INTEGER NOT NULL DEFAULT 3600,
                    max_reservations INTEGER NOT NULL DEFAULT 1,
                    lifetime_sec INTEGER NOT NULL DEFAULT 0,
                    lan_only INTEGER NOT NULL DEFAULT 0,
                    max_horizon_sec INTEGER NOT NULL DEFAULT 604800,
                    blacklisted_tags TEXT NOT NULL DEFAULT '[]',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE UsersLookup (
                    user_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    PRIMARY KEY (user_id, group_id)
                );
                """
            )
            conn.execute(
                "INSERT INTO UserGroups (group_id, group_name, devices_whitelist, blacklisted_tags) "
                "VALUES (1, 'rx-only', '[3]', '[\"TX\"]')"
            )
            conn.execute("INSERT INTO UsersLookup VALUES (10, 1)")
            for idx, bad_wl in enumerate(malformed_whitelists, start=2):
                conn.execute(
                    "INSERT INTO UserGroups (group_id, group_name, devices_whitelist, blacklisted_tags) "
                    "VALUES (?, ?, ?, '[]')",
                    (idx, f"corrupt-{idx}", bad_wl),
                )
                conn.execute("INSERT INTO UsersLookup VALUES (10, ?)", (idx,))
                conn.execute("INSERT INTO UsersLookup VALUES (11, ?)", (idx,))
            # Well-formed groups for regression parity: legacy CSV, string ints, explicit all-devices.
            conn.execute(
                "INSERT INTO UserGroups (group_id, group_name, devices_whitelist, blacklisted_tags) "
                "VALUES (100, 'csv-rx', '5, 6', '[\"TX\"]'), "
                "(101, 'str-ids', '[\"7\"]', '[\"Cal\"]'), "
                "(102, 'all-open', '[]', '[]')"
            )
            conn.execute("INSERT INTO UsersLookup VALUES (20, 100), (20, 101), (21, 1), (21, 102)")

        groups = UserGroupHandler.__new__(UserGroupHandler)
        groups.filepath = db_path
        groups.lock = threading.RLock()

        # Malformed unrestricted groups must not lift TX on device 3 or grant access to other devices.
        self.assertEqual(groups.get_users_blocked_tags(uid=10, device_id=3), (True, ["TX"]))
        self.assertEqual(groups.get_users_blocked_tags(uid=10, device_id=9), (False, []))

        # A user belonging only to malformed-whitelist groups is denied, and holder tag lookup fails closed.
        self.assertEqual(groups.get_users_blocked_tags(uid=11, device_id=3), (False, []))
        handler = reservation.ReservationHandler.__new__(reservation.ReservationHandler)
        handler.get_uid = mock.Mock(return_value=11)
        fake_perms = mock.Mock()
        fake_perms.get_perms.return_value = [["Normal User"]]
        with mock.patch.object(reservation, "perms", fake_perms), \
                mock.patch.object(reservation, "user_group_handler", groups):
            self.assertEqual(handler._blocked_tags_for_holder_locked("corrupt_only", 3), (BLOCK_ALL_TAG,))

        # Well-formed whitelists keep identical behavior.
        self.assertEqual(groups.get_users_blocked_tags(uid=20, device_id=5), (True, ["TX"]))
        self.assertEqual(groups.get_users_blocked_tags(uid=20, device_id=6), (True, ["TX"]))
        self.assertEqual(groups.get_users_blocked_tags(uid=20, device_id=7), (True, ["Cal"]))
        self.assertEqual(groups.get_users_blocked_tags(uid=20, device_id=8), (False, []))
        self.assertEqual(groups.get_users_blocked_tags(uid=21, device_id=3), (True, []))
        self.assertEqual(groups.get_users_blocked_tags(uid=21, device_id=9), (True, []))

    def test_update_devices_pushes_blocked_tags_and_admin_bypass(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "reservations.db"
        now = datetime.now()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE Reservations (res_id INTEGER PRIMARY KEY, username TEXT, "
                "device_id INTEGER, start_time TEXT, end_time TEXT, salt TEXT, hash TEXT)"
            )
            conn.execute(
                "CREATE TABLE RetiredTokens (salt TEXT NOT NULL, hash TEXT NOT NULL, "
                "retired_at INTEGER NOT NULL, PRIMARY KEY (salt, hash))"
            )
            conn.execute(
                "INSERT INTO Reservations VALUES (1, 'alice', 3, ?, ?, 's1', 'h1')",
                ((now - timedelta(minutes=1)).isoformat(), (now + timedelta(minutes=10)).isoformat()),
            )
            conn.execute(
                "INSERT INTO Reservations VALUES (2, 'boss', 4, ?, ?, 's2', 'h2')",
                ((now - timedelta(minutes=1)).isoformat(), (now + timedelta(minutes=10)).isoformat()),
            )

        handler = reservation.ReservationHandler.__new__(reservation.ReservationHandler)
        handler.filepath = db_path
        handler.lock = threading.RLock()
        handler._clean_expired_reservations = mock.Mock()
        handler._account_security_state_locked = mock.Mock(return_value="native")
        handler.get_uid = mock.Mock(side_effect=lambda u: 10 if u == "alice" else 20)

        fake_groups = mock.Mock()
        fake_groups.get_groups_user_in.side_effect = lambda uid: ["admin"] if uid == 20 else ["rx-only"]
        fake_groups.get_users_blocked_tags.side_effect = lambda uid, device_id: (True, ["TX"]) if uid == 10 else (True, [])
        fake_perms = mock.Mock()
        fake_perms.get_perms.return_value = [["Normal User"]]
        set_calls = []

        with mock.patch.object(reservation, "get_all_devices", return_value={3: (object(), "", ""), 4: (object(), "", "")}), \
                mock.patch.object(reservation, "get_registered_devices", return_value={3: (object(), "", ""), 4: (object(), "", "")}), \
                mock.patch.object(reservation, "user_group_handler", fake_groups), \
                mock.patch.object(reservation, "perms", fake_perms), \
                mock.patch.object(reservation, "set_device", side_effect=lambda *a, **kw: set_calls.append((a, kw))):
            handler.update_devices()

        by_dev = {args[0]: kwargs for args, kwargs in set_calls}
        self.assertEqual(by_dev["3"], {"bypass": False, "blocked_tags": ("TX",)})
        self.assertEqual(by_dev["4"], {"bypass": True})


class DeviceManagerHostPropagationTests(unittest.TestCase):
    def test_host_reservation_start_and_resync_carry_blocked_tags(self):
        sent = []

        class FakeRegistry:
            def send_control(self, host_id, *, kind, device_id, payload, **kwargs):
                sent.append((host_id, kind, device_id, payload))
                return True

        st = device_manager._DeviceState(
            dev=device_manager.VirtualDevice(gid=7, host_id="host-a", device_id="7", kind="adalm_pluto"),
            origin="host",
            online=True,
            salt="",
            hsh="",
            name="host-pluto",
            ident="serial-7",
            dtype="adalm_pluto",
        )
        with device_manager._state_lock:
            saved = device_manager._devices.get(7)
            device_manager._devices[7] = st
        try:
            with mock.patch.object(device_manager, "_sync_host_devices"), \
                    mock.patch.object(device_manager.hts, "get_tunnel_registry", return_value=FakeRegistry()), \
                    mock.patch.object(device_manager, "_reservation_end_callbacks", []), \
                    mock.patch.object(device_manager, "_reset_if_pending"):
                device_manager.set_device(7, "salt1", "hash1", bypass=False, blocked_tags=("TX", "Cal"))
                self.assertEqual(device_manager.get_device_blocked_tags(7), ("TX", "Cal"))
                with device_manager.acquire_device_by_gid(7) as (_gid, acquired_dev):
                    self.assertEqual(acquired_dev.blocked_tags, ("TX", "Cal"))
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0][1], "reservation_start")
                self.assertEqual(sent[0][3]["blocked_tags"], ["TX", "Cal"])

                sent.clear()
                device_manager.resync_host_reservations("host-a")
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0][1], "reservation_start")
                self.assertEqual(sent[0][3]["blocked_tags"], ["TX", "Cal"])
                self.assertTrue(sent[0][3]["resync"])

                # Clearing reservation resets blocked_tags.
                device_manager.set_device(7, "", "")
                self.assertEqual(device_manager.get_device_blocked_tags(7), ())
                self.assertEqual(st.blocked_tags, ())
        finally:
            with device_manager._state_lock:
                if saved is None:
                    device_manager._devices.pop(7, None)
                else:
                    device_manager._devices[7] = saved


class ServerV1DispatchTagEnforcementTests(unittest.TestCase):
    def test_v1_blocks_tagged_get_set_call_and_batch_allows_rx_and_unblocked(self):
        rpc = RpcManager()
        dev = _TaggedRadioSchema()
        dev.bind(object())
        dev.blocked_tags = ("tx",)  # case-insensitive match against TX

        with self.assertRaises(RemoteRFPolicyError) as ctx:
            rpc._dispatch_native_schema(function_name="tagged_radio:tx:CALL1", args={"arg1": map_arg(1)}, dev=dev)
        self.assertEqual(str(ctx.exception), "tx is blocked for your user groups on this device (tag TX)")
        self.assertEqual(dev.tx_calls, [])

        with self.assertRaises(RemoteRFPolicyError) as ctx:
            rpc._dispatch_native_schema(
                function_name="tagged_radio:tx_lo:SET",
                args={"tx_lo": map_arg(2_400_000_000)},
                dev=dev,
            )
        self.assertEqual(str(ctx.exception), "set_tx_lo is blocked for your user groups on this device (tag TX)")
        self.assertEqual(dev.tx_lo_val, 915_000_000)

        with self.assertRaises(RemoteRFPolicyError) as ctx:
            rpc._dispatch_native_schema(function_name="tagged_radio:tx_power:GET", args={}, dev=dev)
        self.assertEqual(str(ctx.exception), "get_tx_power is blocked for your user groups on this device (tag TX)")

        # Untagged RX functions succeed.
        res_rx = rpc._dispatch_native_schema(function_name="tagged_radio:rx:CALL0", args={}, dev=dev)
        self.assertEqual(unmap_arg(res_rx["rx"]), 1)
        res_lo = rpc._dispatch_native_schema(function_name="tagged_radio:rx_lo:GET", args={}, dev=dev)
        self.assertEqual(unmap_arg(res_lo["rx_lo"]), 915_000_000)

        # Batch containing an allowed op followed by a blocked op fails at op1 with clear message.
        op0 = grpc_pb2.GenericRPCRequest(function_name="tagged_radio:rx:CALL0").SerializeToString()
        op1 = grpc_pb2.GenericRPCRequest(
            function_name="tagged_radio:tx_lo:SET",
            args={"tx_lo": map_arg(2_400_000_000)},
        ).SerializeToString()
        batch_res = rpc._dispatch_acquired(
            function_name="tagged_radio:batch:CALLN",
            args={"op0": map_arg(op0), "op1": map_arg(op1)},
            gid=1,
            dev=dev,
        )
        self.assertEqual(unmap_arg(batch_res["batch_done"]), 1)
        self.assertEqual(unmap_arg(batch_res["batch_failed"]), 1)
        self.assertEqual(
            unmap_arg(batch_res["batch_error"]),
            "set_tx_lo is blocked for your user groups on this device (tag TX)",
        )

        # Fail-closed wildcard blocks any tagged method while still allowing untagged RX.
        dev.blocked_tags = (BLOCK_ALL_TAG,)
        with self.assertRaises(RemoteRFPolicyError) as ctx:
            rpc._dispatch_native_schema(function_name="tagged_radio:tx:CALL1", args={"arg1": map_arg(9)}, dev=dev)
        self.assertEqual(str(ctx.exception), "tx is blocked for your user groups on this device (tag TX)")
        res_rx_wild = rpc._dispatch_native_schema(function_name="tagged_radio:rx:CALL0", args={}, dev=dev)
        self.assertEqual(unmap_arg(res_rx_wild["rx"]), 3)

        # Unblocked holder calls everything normally.
        dev.blocked_tags = ()
        rpc._dispatch_native_schema(
            function_name="tagged_radio:tx_lo:SET",
            args={"tx_lo": map_arg(2_400_000_000)},
            dev=dev,
        )
        self.assertEqual(dev.tx_lo_val, 2_400_000_000)
        rpc._dispatch_native_schema(function_name="tagged_radio:tx:CALL1", args={"arg1": map_arg([1, 2])}, dev=dev)
        self.assertEqual([np.asarray(item).tolist() for item in dev.tx_calls], [[1, 2]])

    def test_v1_virtual_device_blocks_locally_when_lab_copy_matches(self):
        rpc = RpcManager()
        vdev = device_manager.VirtualDevice(
            gid=5, host_id="host-a", device_id="5", kind="tagged_radio", blocked_tags=("TX",)
        )
        with mock.patch("remoteRF_server.server.rpc_manager.hts.host_supports_function_tags", return_value=True), \
                mock.patch("remoteRF_server.server.host_dynamic_v2.lab_copy", return_value=_TaggedRadioSchema()), \
                mock.patch("remoteRF_server.server.rpc_manager.hts.handle_host_device") as forward:
            with self.assertRaises(RemoteRFPolicyError) as ctx:
                rpc._dispatch_acquired(
                    function_name="tagged_radio:tx:CALL1",
                    args={"arg1": map_arg(1)},
                    gid=5,
                    dev=vdev,
                )
        self.assertEqual(str(ctx.exception), "tx is blocked for your user groups on this device (tag TX)")
        forward.assert_not_called()

    def test_v1_virtual_device_refuses_old_host_when_blocked_tags_non_empty(self):
        rpc = RpcManager()
        old_vdev = device_manager.VirtualDevice(
            gid=5, host_id="old-host", device_id="5", kind="tagged_radio", blocked_tags=("TX",)
        )
        with mock.patch("remoteRF_server.server.rpc_manager.hts.host_supports_function_tags", return_value=False), \
                mock.patch("remoteRF_server.server.rpc_manager.hts.handle_host_device") as forward:
            with self.assertRaises(RemoteRFPolicyError) as ctx:
                rpc._dispatch_acquired(
                    function_name="tagged_radio:rx:CALL0",
                    args={},
                    gid=5,
                    dev=old_vdev,
                )
        self.assertEqual(str(ctx.exception), OLD_HOST_TAGS_ERROR)
        forward.assert_not_called()

        # Unblocked holder on an old host is not refused.
        unblocked_vdev = device_manager.VirtualDevice(
            gid=5, host_id="old-host", device_id="5", kind="tagged_radio", blocked_tags=()
        )
        with mock.patch("remoteRF_server.server.rpc_manager.hts.host_supports_function_tags", return_value=False), \
                mock.patch("remoteRF_server.server.rpc_manager.hts.handle_host_device", return_value={"rx": map_arg(1)}) as forward:
            res = rpc._dispatch_acquired(
                function_name="tagged_radio:rx:CALL0",
                args={},
                gid=5,
                dev=unblocked_vdev,
            )
        self.assertEqual(unmap_arg(res["rx"]), 1)
        forward.assert_called_once()

    def test_v1_virtual_device_forwards_master_bypass_and_strips_client_spoof(self):
        rpc = RpcManager()
        master_vdev = device_manager.VirtualDevice(
            gid=5, host_id="host-a", device_id="5", kind="tagged_radio", blocked_tags=(), is_master=True
        )
        user_vdev = device_manager.VirtualDevice(
            gid=5, host_id="host-a", device_id="5", kind="tagged_radio", blocked_tags=(), is_master=False
        )
        with mock.patch("remoteRF_server.server.rpc_manager.hts.handle_host_device", return_value={"rx": map_arg(1)}) as forward:
            rpc._dispatch_acquired(function_name="tagged_radio:rx:CALL0", args={}, gid=5, dev=master_vdev)
            master_args = forward.call_args.kwargs["args"]
            self.assertIs(unmap_arg(master_args["__rrf_master_bypass"]), True)

            forward.reset_mock()
            rpc._dispatch_acquired(
                function_name="tagged_radio:rx:CALL0",
                args={"__rrf_master_bypass": map_arg(True)},
                gid=5,
                dev=user_vdev,
            )
            user_args = forward.call_args.kwargs["args"]
            self.assertNotIn("__rrf_master_bypass", user_args)


    def test_v1_ti_awr6843_config_setters_blocked_for_tx_blacklisted_group(self):
        from remoterf_device_core.drivers.ti_awr6843.ti_awr6843_schema import (
            TiMmWaveSchema as TiAwr6843Schema,
        )

        rpc = RpcManager()
        fake_dev = mock.MagicMock()
        fake_dev.fps = 10.0
        schema = TiAwr6843Schema()
        schema.bind(fake_dev)
        schema.blocked_tags = ("TX",)

        for prop, val in (
            ("fps", 30.0),
            ("preset", "fast-motion"),
            ("mode", "2d"),
            ("sensitivity", "high"),
            ("min_range", 0.5),
            ("max_range", 5.0),
            ("clutter_removal", True),
        ):
            with self.assertRaises(RemoteRFPolicyError) as ctx:
                rpc._dispatch_native_schema(
                    function_name=f"ti_awr6843:{prop}:SET",
                    args={prop: map_arg(val)},
                    dev=schema,
                )
            self.assertEqual(
                str(ctx.exception),
                f"set_{prop} is blocked for your user groups on this device (tag TX)",
            )

        res = rpc._dispatch_native_schema(function_name="ti_awr6843:fps:GET", args={}, dev=schema)
        self.assertEqual(unmap_arg(res["fps"]), 10.0)

    def test_v1_batch_rx_n_and_slow_reset_cannot_bypass_blocked_tags_on_handover_or_expiry(self):
        from remoteRF_server.common.utils.api_token import hash_token

        gid = 4141
        sa, ha = hash_token("tokA")
        sb, hb = hash_token("tokB")

        class _RaceRadio(_TaggedRadioSchema):
            def __init__(self):
                super().__init__()
                self.rx_hook = None
                self.reset_hook = None

            @idl_expose(kind="call")
            def call_rx(self):
                if self.rx_hook is not None:
                    hook, self.rx_hook = self.rx_hook, None
                    hook()
                self.rx_calls += 1
                return np.array([self.rx_calls, self.rx_calls + 1], dtype=np.complex128)

            def reset_for_next_holder(self):
                if self.reset_hook is not None:
                    hook, self.reset_hook = self.reset_hook, None
                    hook()

        def _req(name, **req_args):
            return grpc_pb2.GenericRPCRequest(function_name=name, args=req_args).SerializeToString()

        def _setup_state():
            dev = _RaceRadio()
            dev.bind(object())
            st = device_manager._DeviceState(
                dev=dev, origin="local", online=True, salt="", hsh="",
                name="race-radio", ident="r", dtype="tagged_radio",
            )
            with device_manager._state_lock:
                device_manager._devices[gid] = st
            rpc = RpcManager()
            rpc._federated_route = lambda **kw: None
            return dev, st, rpc

        expected_changed = "The reservation on this device changed; call again."
        saved = device_manager._devices.get(gid)
        try:
            with mock.patch.object(device_manager, "_sync_host_devices"), \
                    mock.patch.object(device_manager, "_reservation_end_callbacks", []), \
                    mock.patch.object(device_manager, "_reset_if_pending"), \
                    mock.patch.object(device_manager, "_get_master_token", return_value=""):
                # 1) Batch [rx, tx] via acquire_device: mid-flight handover to unblocked B.
                dev, st, rpc = _setup_state()
                device_manager.set_device(gid, sa, ha, blocked_tags=("TX",))
                pre = rpc._handle_device_rpc(
                    function_name="tagged_radio:tx:CALL1",
                    args={"a": map_arg("tokA"), "arg1": map_arg([1, 2])},
                )
                self.assertEqual(unmap_arg(pre["a"]), "tx is blocked for your user groups on this device (tag TX)")
                self.assertEqual(dev.tx_calls, [])

                in_rx, handover_done = threading.Event(), threading.Event()

                def _pause_in_rx():
                    in_rx.set()
                    self.assertTrue(handover_done.wait(1.0))

                dev.rx_hook = _pause_in_rx
                batch_args = {
                    "a": map_arg("tokA"),
                    "op0": map_arg(_req("tagged_radio:rx:CALL0")),
                    "op1": map_arg(_req("tagged_radio:tx:CALL1", arg1=map_arg([9, 9]))),
                }
                result_box = []
                worker = threading.Thread(
                    target=lambda: result_box.append(
                        rpc._handle_device_rpc(function_name="tagged_radio:batch:CALLN", args=batch_args)
                    )
                )
                worker.start()
                self.assertTrue(in_rx.wait(1.0))
                device_manager.set_device(gid, sb, hb, blocked_tags=())
                handover_done.set()
                worker.join(2.0)
                self.assertFalse(worker.is_alive())
                self.assertEqual(dev.tx_calls, [])
                self.assertEqual(unmap_arg(result_box[0]["batch_done"]), 1)
                self.assertEqual(unmap_arg(result_box[0]["batch_failed"]), 1)
                self.assertEqual(unmap_arg(result_box[0]["batch_error"]), expected_changed)

                # 2) Batch [rx, tx] via acquire_device_by_gid: mid-flight reservation expiry.
                dev, st, rpc = _setup_state()
                device_manager.set_device(gid, sa, ha, blocked_tags=("TX",))
                in_rx, expiry_done = threading.Event(), threading.Event()
                dev.rx_hook = lambda: (in_rx.set(), self.assertTrue(expiry_done.wait(1.0)))
                result_box.clear()
                worker = threading.Thread(
                    target=lambda: result_box.append(
                        rpc.run_device_rpc_for_gid(
                            gid=gid,
                            function_name="tagged_radio:batch:CALLN",
                            args={
                                "op0": map_arg(_req("tagged_radio:rx:CALL0")),
                                "op1": map_arg(_req("tagged_radio:tx:CALL1", arg1=map_arg([5, 5]))),
                            },
                            expected_reservation=(sa, ha),
                        )
                    )
                )
                worker.start()
                self.assertTrue(in_rx.wait(1.0))
                device_manager.set_device(gid, "", "")
                expiry_done.set()
                worker.join(2.0)
                self.assertFalse(worker.is_alive())
                self.assertEqual(dev.tx_calls, [])
                self.assertEqual(unmap_arg(result_box[0]["batch_done"]), 1)
                self.assertEqual(unmap_arg(result_box[0]["batch_failed"]), 1)
                self.assertEqual(unmap_arg(result_box[0]["batch_error"]), expected_changed)

                # 3) rx_n mid-flight expiry stops after the in-flight op and refuses remaining ops.
                dev, st, rpc = _setup_state()
                device_manager.set_device(gid, sa, ha, blocked_tags=("TX",))
                in_rx, expiry_done = threading.Event(), threading.Event()
                dev.rx_hook = lambda: (in_rx.set(), self.assertTrue(expiry_done.wait(1.0)))
                result_box.clear()
                worker = threading.Thread(
                    target=lambda: result_box.append(
                        rpc._handle_device_rpc(
                            function_name="tagged_radio:rx_n:CALL1",
                            args={"a": map_arg("tokA"), "arg1": map_arg(4)},
                        )
                    )
                )
                worker.start()
                self.assertTrue(in_rx.wait(1.0))
                device_manager.set_device(gid, "", "")
                expiry_done.set()
                worker.join(2.0)
                self.assertFalse(worker.is_alive())
                self.assertEqual(dev.rx_calls, 1)
                self.assertEqual(unmap_arg(result_box[0]["rx_n"]).shape, (1, 2))

                # 4) Single TX call with a slow reset_for_next_holder hook during handover.
                dev, st, rpc = _setup_state()
                device_manager.set_device(gid, sa, ha, blocked_tags=("TX",))
                st.reset_pending = True
                in_reset, handover_done = threading.Event(), threading.Event()
                dev.reset_hook = lambda: (in_reset.set(), self.assertTrue(handover_done.wait(1.0)))
                result_box.clear()
                worker = threading.Thread(
                    target=lambda: result_box.append(
                        rpc._handle_device_rpc(
                            function_name="tagged_radio:tx:CALL1",
                            args={"a": map_arg("tokA"), "arg1": map_arg([7, 7])},
                        )
                    )
                )
                worker.start()
                self.assertTrue(in_reset.wait(1.0))
                device_manager.set_device(gid, sb, hb, blocked_tags=())
                handover_done.set()
                worker.join(2.0)
                self.assertFalse(worker.is_alive())
                self.assertEqual(dev.tx_calls, [])
                self.assertEqual(unmap_arg(result_box[0]["a"]), expected_changed)
        finally:
            with device_manager._state_lock:
                if saved is None:
                    device_manager._devices.pop(gid, None)
                else:
                    device_manager._devices[gid] = saved


class ServerV2DispatchTagEnforcementTests(unittest.TestCase):
    def test_v2_invoke_and_sample_stream_enforce_blocked_tags(self):
        inventory = _FakeInventoryUsrp()
        context = {
            "schema": inventory,
            "dtype": "usrp",
            "ident": "SERIAL",
            "name": "test-usrp",
            "blocked_tags": ("TX",),
        }
        manager = SessionManager()
        with mock.patch.dict(sys.modules, {"uhd": _fake_uhd_module()}), \
                mock.patch("remoteRF_server.server.dynamic_v2.resolve_gid_for_token", return_value=7), \
                mock.patch("remoteRF_server.server.dynamic_v2.get_local_device_context", return_value=context), \
                mock.patch("remoteRF_server.server.dynamic_v2.token_matches_device", return_value=True), \
                mock.patch("remoteRF_server.server.dynamic_v2.is_master_token", return_value=False), \
                mock.patch("remoteRF_server.server.dynamic_v2.session_manager", manager):
            session = manager.open("user-token")

            # TX methods blocked on MultiUSRP.
            for fn, args in (
                ("set_tx_freq", {"tune_request": 2.4e9}),
                ("get_tx_stream", {"stream_args": {}}),
                ("send_waveform", {"samples": [0.0], "duration": 0.1, "freq": 2.4e9}),
            ):
                with self.assertRaises(RemoteRFPolicyError) as ctx:
                    manager.invoke(session.session_id, session.root_handle, fn, args, overload_id="default")
                self.assertEqual(str(ctx.exception), f"{fn} is blocked for your user groups on this device (tag TX)")

            # Transmitting integration methods are also blocked by TX tag.
            for fn in ("integration_start", "integration_call"):
                with self.assertRaises(RemoteRFPolicyError) as ctx:
                    manager.invoke(
                        session.session_id,
                        session.root_handle,
                        fn,
                        {"request": {"adapter": "oai"}},
                        overload_id="request",
                    )
                self.assertEqual(str(ctx.exception), f"{fn} is blocked for your user groups on this device (tag TX)")

            # Non-transmitting integration methods (e.g. list/stop) are not blocked by TX tag.
            with mock.patch.object(manager.integrations, "dispatch", return_value={"ok": True}) as int_dispatch:
                res_list, _ = manager.invoke(
                    session.session_id,
                    session.root_handle,
                    "integration_list",
                    {"request": {}},
                    overload_id="request",
                )
                self.assertEqual(res_list, {"ok": True})
                int_dispatch.assert_called_once()

            # Servicer wraps the refusal as a normal Dynamic v2 policy error envelope.
            inv_req = grpc_pb2.InvokeRequest(
                session_id=session.session_id,
                handle=session.root_handle,
                method="set_tx_gain",
                overload_id="gain_chan",
            )
            inv_req.args.add(name="gain", value=encode_value(10.0))
            inv_resp = DynamicControlV2Servicer().Invoke(inv_req, None)
            self.assertEqual(inv_resp.error.code, "RemoteRFPolicyError")
            self.assertEqual(
                inv_resp.error.message,
                "set_tx_gain is blocked for your user groups on this device (tag TX)",
            )

            # Untagged / RX methods still succeed.
            rx_res, _ = manager.invoke(
                session.session_id, session.root_handle, "get_rx_stream", {}, positional_args=[{}], overload_id="default"
            )
            self.assertTrue(rx_res["__remoterf_handle__"].startswith("rx_"))
            t_val, _ = manager.invoke(session.session_id, session.root_handle, "get_time_now", {}, overload_id="default")
            self.assertEqual(t_val, 1.5)

            # Even if a TXStreamer handle exists on a TX-blocked session, SampleStream TX OPEN refuses it.
            manager.close(session.session_id)
            context["blocked_tags"] = ("tx",)
            session = manager.open("user-token")
            tx_entry = manager.register_handle(
                session,
                inventory.live.tx_streamer,
                "uhd.usrp.TXStreamer",
                parent=session.root_handle,
            )
            tx_handle = tx_entry.handle
            tx_gen = tx_entry.generation

            open_frame = grpc_pb2.SampleFrame(
                session_id=session.session_id,
                handle=tx_handle,
                generation=tx_gen,
                operation_id="tx-op-1",
                sequence=0,
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_OPEN,
                credits=1,
            )
            with mock.patch.object(SampleDataV1Servicer, "_metadata_object", return_value=_FakeMetadata()):
                responses = list(SampleDataV1Servicer().SampleStream(iter([open_frame]), None))
            self.assertEqual(len(responses), 1)
            self.assertEqual(responses[0].kind, grpc_pb2.SAMPLE_FRAME_ERROR)
            self.assertEqual(responses[0].error.code, "RemoteRFPolicyError")
            self.assertEqual(
                responses[0].error.message,
                "send is blocked for your user groups on this device (tag TX)",
            )
            self.assertEqual(inventory.live.tx_streamer.sent, [])

            # Master token bypasses blocked_tags.
            manager.close(session.session_id)
        with mock.patch.dict(sys.modules, {"uhd": _fake_uhd_module()}), \
                mock.patch("remoteRF_server.server.dynamic_v2.resolve_gid_for_token", return_value=7), \
                mock.patch("remoteRF_server.server.dynamic_v2.get_local_device_context", return_value=context), \
                mock.patch("remoteRF_server.server.dynamic_v2.token_matches_device", return_value=True), \
                mock.patch("remoteRF_server.server.dynamic_v2.is_master_token", return_value=True):
            master_session = manager.open("master_7")
            manager.invoke(
                master_session.session_id,
                master_session.root_handle,
                "set_tx_freq",
                {"tune_request": 2.4e9},
                overload_id="default",
            )
            manager.close(master_session.session_id)


    def test_v2_property_tree_writes_blocked_for_tx_blacklisted_group(self):
        class _FakeDoubleProp:
            def __init__(self):
                self.value = 1.0e9
                self.writes = []

            def get(self):
                return self.value

            def get_desired(self):
                return self.value

            def set(self, value):
                self.writes.append(("set", value))
                self.value = float(value)

            def set_coerced(self, value):
                self.writes.append(("set_coerced", value))
                self.value = float(value)

        class _FakeTree:
            def __init__(self, prop):
                self._prop = prop

            def access_double(self, path):
                return self._prop

        prop = _FakeDoubleProp()
        tree = _FakeTree(prop)

        class _TreeLiveUsrp(_FakeLiveUsrp):
            def invoke_native(self, method, args, positional_args=None):
                if method == "get_tree":
                    return tree
                return super().invoke_native(method, args, positional_args=positional_args)

        class _TreeInventoryUsrp(_FakeInventoryUsrp):
            def create_session_schema(self):
                self.live = _TreeLiveUsrp()
                return self.live

        inventory = _TreeInventoryUsrp()
        context = {
            "schema": inventory,
            "dtype": "usrp",
            "ident": "SERIAL",
            "name": "test-usrp",
            "blocked_tags": ("TX",),
        }
        manager = SessionManager()
        with mock.patch.dict(sys.modules, {"uhd": _fake_uhd_module()}), \
                mock.patch("remoteRF_server.server.dynamic_v2.resolve_gid_for_token", return_value=7), \
                mock.patch("remoteRF_server.server.dynamic_v2.get_local_device_context", return_value=context), \
                mock.patch("remoteRF_server.server.dynamic_v2.token_matches_device", return_value=True), \
                mock.patch("remoteRF_server.server.dynamic_v2.is_master_token", return_value=False), \
                mock.patch("remoteRF_server.server.dynamic_v2.session_manager", manager):
            session = manager.open("user-token")
            tree_res, _ = manager.invoke(
                session.session_id, session.root_handle, "get_tree", {}, overload_id="default"
            )
            tree_handle = tree_res["__remoterf_handle__"]
            prop_res, _ = manager.invoke(
                session.session_id,
                tree_handle,
                "access_double",
                {},
                positional_args=["/mboards/0/dboards/A/tx_frontends/0/freq/value"],
                overload_id="default",
            )
            prop_handle = prop_res["__remoterf_handle__"]

            # Reads stay allowed.
            val, _ = manager.invoke(session.session_id, prop_handle, "get", {}, overload_id="default")
            self.assertEqual(val, 1.0e9)
            val2, _ = manager.invoke(session.session_id, prop_handle, "get_desired", {}, overload_id="default")
            self.assertEqual(val2, 1.0e9)

            # Writes are refused under TX blacklist.
            for write_fn in ("set", "set_coerced"):
                with self.assertRaises(RemoteRFPolicyError) as ctx:
                    manager.invoke(
                        session.session_id,
                        prop_handle,
                        write_fn,
                        {},
                        positional_args=[2.45e9],
                        overload_id="default",
                    )
                self.assertEqual(
                    str(ctx.exception),
                    f"{write_fn} is blocked for your user groups on this device (tag TX)",
                )
            self.assertEqual(prop.writes, [])

            # Unrestricted holder opens its own session and can write.
            manager.close(session.session_id)
            context["blocked_tags"] = ()
            unrestricted = manager.open("unrestricted-token")
            tree_res2, _ = manager.invoke(
                unrestricted.session_id, unrestricted.root_handle, "get_tree", {}, overload_id="default"
            )
            prop_res2, _ = manager.invoke(
                unrestricted.session_id,
                tree_res2["__remoterf_handle__"],
                "access_double",
                {},
                positional_args=["/mboards/0/dboards/A/tx_frontends/0/freq/value"],
                overload_id="default",
            )
            prop_handle2 = prop_res2["__remoterf_handle__"]
            manager.invoke(
                unrestricted.session_id,
                prop_handle2,
                "set",
                {},
                positional_args=[2.45e9],
                overload_id="default",
            )
            manager.invoke(
                unrestricted.session_id,
                prop_handle2,
                "set_coerced",
                {},
                positional_args=[2.46e9],
                overload_id="default",
            )
            self.assertEqual(prop.writes, [("set", 2.45e9), ("set_coerced", 2.46e9)])
            manager.close(unrestricted.session_id)

    def test_host_sync_swapping_the_state_object_during_reset_is_not_a_reservation_change(self):
        import copy
        from remoteRF_server.common.utils.api_token import hash_token

        gid = 4142
        sa, ha = hash_token("tokA")

        class _SwapRadio(_TaggedRadioSchema):
            reset_hook = None

            def reset_for_next_holder(self):
                if self.reset_hook is not None:
                    hook, self.reset_hook = self.reset_hook, None
                    hook()

        dev = _SwapRadio()
        dev.bind(object())
        st = device_manager._DeviceState(
            dev=dev, origin="local", online=True, salt="", hsh="",
            name="swap-radio", ident="s", dtype="tagged_radio",
        )
        saved = device_manager._devices.get(gid)
        try:
            with mock.patch.object(device_manager, "_sync_host_devices"), \
                    mock.patch.object(device_manager, "_reservation_end_callbacks", []), \
                    mock.patch.object(device_manager, "_reset_if_pending"), \
                    mock.patch.object(device_manager, "_get_master_token", return_value=""):
                with device_manager._state_lock:
                    device_manager._devices[gid] = st
                device_manager.set_device(gid, sa, ha, blocked_tags=("TX",))
                device_manager._devices[gid].reset_pending = True

                def _swap():  # what _sync_host_devices does: same lock and reservation, new object
                    with device_manager._state_lock:
                        device_manager._devices[gid] = copy.copy(device_manager._devices[gid])

                dev.reset_hook = _swap
                rpc = RpcManager()
                rpc._federated_route = lambda **kw: None
                out = rpc._handle_device_rpc(function_name="tagged_radio:rx:CALL0", args={"a": map_arg("tokA")})
                self.assertIsNone(dev.reset_hook)  # the swap really happened mid-acquire
                self.assertEqual(dev.rx_calls, 1, out)
        finally:
            with device_manager._state_lock:
                if saved is None:
                    device_manager._devices.pop(gid, None)
                else:
                    device_manager._devices[gid] = saved

    def test_v2_session_snapshots_blocked_tags_and_refuses_midflight_handover(self):
        from remoteRF_server.common.utils.api_token import hash_token
        from remoteRF_server.server.v2_errors import RemoteRFSessionExpiredError

        gid = 4142
        sa, ha = hash_token("tokA")
        sb, hb = hash_token("tokB")
        inventory = _FakeInventoryUsrp()
        st = device_manager._DeviceState(
            dev=inventory, origin="local", online=True, salt="", hsh="",
            name="v2-usrp", ident="SERIAL", dtype="usrp",
        )
        manager = SessionManager()
        saved = device_manager._devices.get(gid)
        with device_manager._state_lock:
            device_manager._devices[gid] = st
        try:
            state_updated, allow_close = threading.Event(), threading.Event()

            def _delayed_close(_gid):
                state_updated.set()
                allow_close.wait(1.0)
                manager.close_gid(_gid, wait=False)

            with mock.patch.dict(sys.modules, {"uhd": _fake_uhd_module()}), \
                    mock.patch.object(device_manager, "_sync_host_devices"), \
                    mock.patch.object(device_manager, "_reset_if_pending"), \
                    mock.patch.object(device_manager, "_get_master_token", return_value=""), \
                    mock.patch.object(device_manager, "_reservation_end_callbacks", [_delayed_close]), \
                    mock.patch("remoteRF_server.server.dynamic_v2.session_manager", manager):
                device_manager.set_device(gid, sa, ha, blocked_tags=("TX",))
                session = manager.open("tokA")

                # Mutating live st.blocked_tags without changing reservation cannot weaken a TX-blocked session.
                with device_manager._state_lock:
                    st.blocked_tags = ()
                with self.assertRaises(RemoteRFPolicyError):
                    manager.invoke(
                        session.session_id,
                        session.root_handle,
                        "set_tx_freq",
                        {"tune_request": 2.4e9},
                        overload_id="default",
                    )
                self.assertEqual(inventory.live.calls, [])

                # Mid-flight handover before close_gid completes refuses immediately with session expiry.
                with device_manager._state_lock:
                    st.blocked_tags = ("TX",)
                handover = threading.Thread(
                    target=lambda: device_manager.set_device(gid, sb, hb, blocked_tags=())
                )
                handover.start()
                self.assertTrue(state_updated.wait(1.0))
                try:
                    with self.assertRaises(RemoteRFSessionExpiredError):
                        manager._check_blocked_tags(session, "uhd.usrp.MultiUSRP", "set_tx_freq")
                    with self.assertRaises(RemoteRFSessionExpiredError):
                        manager.invoke(
                            session.session_id,
                            session.root_handle,
                            "set_tx_freq",
                            {"tune_request": 2.4e9},
                            overload_id="default",
                        )
                    self.assertEqual(inventory.live.calls, [])
                finally:
                    allow_close.set()
                    handover.join(2.0)
                    self.assertFalse(handover.is_alive())
                    manager.close(session.session_id)
        finally:
            with device_manager._state_lock:
                if saved is None:
                    device_manager._devices.pop(gid, None)
                else:
                    device_manager._devices[gid] = saved


class HostDynamicV2TagEnforcementTests(unittest.TestCase):
    def test_old_host_refused_and_lab_copy_enforced_and_master_forwarded(self):
        proxy = host_dynamic_v2.HostDynamicV2Proxy()
        blocked_dev = device_manager.VirtualDevice(
            gid=7, host_id="host-a", device_id="7", kind="usrp", blocked_tags=("TX",)
        )
        master_dev = device_manager.VirtualDevice(
            gid=7, host_id="host-a", device_id="7", kind="usrp", blocked_tags=(), is_master=True
        )
        forwarded = []

        def fake_forward(registry, *, host_id, device_id, function_name, args, timeout_sec):
            op = function_name.split(":", 1)[1]
            forwarded.append((op, dict(args)))
            if op == "OpenSession":
                resp = grpc_pb2.OpenSessionResponse(
                    session_id="hs-1",
                    device_handle="root-1",
                    schema_hash="sha256:test",
                )
            elif op == "Invoke":
                resp = grpc_pb2.InvokeResponse(result=encode_value(1.25))
            elif op == "SampleFrame":
                req = grpc_pb2.SampleFrame.FromString(unmap_arg(args["wire"]))
                resp = grpc_pb2.SampleFrame()
                resp.CopyFrom(req)
                resp.kind = grpc_pb2.SAMPLE_FRAME_RESULT
            else:
                resp = grpc_pb2.CloseResponse(closed=True)
            return {"wire": map_arg(resp.SerializeToString())}

        old_session = hts.HostSession("host-a", capabilities={"reservation_tag"})
        new_session = hts.HostSession("host-a", capabilities={"reservation_tag", HOST_FUNCTION_TAGS_CAPABILITY})
        current_session = [old_session]

        class _Reg:
            def session_for_host(self, _hid):
                return current_session[0]

            def host_has_capability(self, _hid, cap):
                s = current_session[0]
                return bool(s is not None and s.alive and cap in s.capabilities)

        reg = _Reg()
        usrp_schema = UsrpSchema()

        # 1) Old host without function_tags capability is refused before forwarding.
        with mock.patch.object(proxy, "route_for_token", return_value=(7, blocked_dev)), \
                mock.patch("remoteRF_server.server.host_dynamic_v2.hts.get_tunnel_registry", return_value=reg), \
                mock.patch("remoteRF_server.server.host_dynamic_v2.hts.handle_host_device", side_effect=fake_forward):
            with self.assertRaises(RemoteRFPolicyError) as ctx:
                proxy.open(grpc_pb2.OpenSessionRequest(token="tok"))
            self.assertEqual(str(ctx.exception), OLD_HOST_TAGS_ERROR)
            with self.assertRaises(RemoteRFPolicyError) as ctx:
                proxy.get_schema(grpc_pb2.GetSchemaRequest(token="tok", schema_versions=["2.0"]))
            self.assertEqual(str(ctx.exception), OLD_HOST_TAGS_ERROR)
            self.assertEqual(forwarded, [])

        # 2) Capable host with lab_copy enforces blocked tags at the lab before forwarding.
        current_session[0] = new_session
        with mock.patch.object(proxy, "route_for_token", return_value=(7, blocked_dev)), \
                mock.patch("remoteRF_server.server.host_dynamic_v2.hts.get_tunnel_registry", return_value=reg), \
                mock.patch("remoteRF_server.server.host_dynamic_v2.lab_copy", return_value=usrp_schema), \
                mock.patch("remoteRF_server.server.host_dynamic_v2.hts.handle_host_device", side_effect=fake_forward):
            opened = proxy.open(grpc_pb2.OpenSessionRequest(token="tok"))
            self.assertEqual(opened.session_id, "hs-1")
            forwarded.clear()

            for blocked_method in ("set_tx_freq", "integration_start", "integration_call"):
                with self.assertRaises(RemoteRFPolicyError) as ctx:
                    proxy.invoke(grpc_pb2.InvokeRequest(session_id="hs-1", handle="root-1", method=blocked_method))
                self.assertEqual(
                    str(ctx.exception),
                    f"{blocked_method} is blocked for your user groups on this device (tag TX)",
                )
            self.assertEqual(forwarded, [])

            # TX SampleFrame is also enforced at the lab before forwarding.
            tx_open = grpc_pb2.SampleFrame(
                session_id="hs-1",
                handle="tx-1",
                operation_id="op-tx",
                direction=grpc_pb2.SAMPLE_DIRECTION_TX,
                kind=grpc_pb2.SAMPLE_FRAME_OPEN,
            )
            with self.assertRaises(RemoteRFPolicyError) as ctx:
                proxy.sample_frame(tx_open)
            self.assertEqual(str(ctx.exception), "send is blocked for your user groups on this device (tag TX)")
            self.assertEqual(forwarded, [])

            # Untagged methods and RX SampleFrame forward normally.
            proxy.invoke(grpc_pb2.InvokeRequest(session_id="hs-1", handle="root-1", method="get_time_now"))
            rx_open = grpc_pb2.SampleFrame(
                session_id="hs-1",
                handle="rx-1",
                operation_id="op-rx",
                direction=grpc_pb2.SAMPLE_DIRECTION_RX,
                kind=grpc_pb2.SAMPLE_FRAME_OPEN,
            )
            proxy.sample_frame(rx_open)
            self.assertEqual([op for op, _ in forwarded], ["Invoke", "SampleFrame"])
            self.assertNotIn("__rrf_master_bypass", forwarded[0][1])

            proxy.close_session(grpc_pb2.CloseSessionRequest(session_id="hs-1"))

        # 3) Master token forwards __rrf_master_bypass=True to host v2.
        forwarded.clear()
        with mock.patch.object(proxy, "route_for_token", return_value=(7, master_dev)), \
                mock.patch("remoteRF_server.server.host_dynamic_v2.hts.get_tunnel_registry", return_value=reg), \
                mock.patch("remoteRF_server.server.host_dynamic_v2.lab_copy", return_value=usrp_schema), \
                mock.patch("remoteRF_server.server.host_dynamic_v2.hts.handle_host_device", side_effect=fake_forward):
            proxy.open(grpc_pb2.OpenSessionRequest(token="master_7"))
            proxy.invoke(grpc_pb2.InvokeRequest(session_id="hs-1", handle="root-1", method="set_tx_freq"))
            self.assertEqual([op for op, _ in forwarded], ["OpenSession", "Invoke"])
            self.assertIs(unmap_arg(forwarded[1][1]["__rrf_master_bypass"]), True)
            proxy.close_session(grpc_pb2.CloseSessionRequest(session_id="hs-1"))


class HostDirectGrantBlockedTagsTests(unittest.TestCase):
    def test_issue_grant_and_offer_carry_blocked_tags_claim(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)
        identity_dir = tmp_path / "identity"
        init_identity(identity_dir)
        save_state(identity_dir, DeploymentState(deployment_id="deployment-1", slug="lab"))
        cert_dir = tmp_path / "certs"
        cert_dir.mkdir()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=2)).add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256())
        )
        (cert_dir / "ca.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (cert_dir / "ca.key").write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        config = tmp_path / "config"
        config.mkdir()
        (config / "global.env").write_text("GLOBAL_STATE=active\n")

        env = {
            "REMOTERF_FEDERATION_IDENTITY_DIR": str(identity_dir),
            "REMOTERF_CERT_DIR": str(cert_dir),
            "REMOTERF_CONFIG_DIR": str(config),
        }
        with mock.patch.dict("os.environ", env), mock.patch.object(host_direct.time, "time", return_value=1000.0):
            pub = load_private_key(identity_dir).public_key()
            claims_blocked = verify_eddsa_jwt(
                host_direct.issue_grant("lab-host", 7, "res-1", 2000, "tag-1", ("TX", "Cal")),
                pub,
            )
            self.assertEqual(claims_blocked["blocked_tags"], ["TX", "Cal"])

            claims_plain = verify_eddsa_jwt(
                host_direct.issue_grant("lab-host", 7, "res-1", 2000, "tag-1"),
                pub,
            )
            self.assertNotIn("blocked_tags", claims_plain)

            for bad in ("TX", "", {}, {"TX": True}, [""], [None], [123], ["TX", None]):
                claims_bad = verify_eddsa_jwt(
                    host_direct.issue_grant("lab-host", 7, "res-1", 2000, "tag-1", bad),
                    pub,
                )
                self.assertEqual(claims_bad["blocked_tags"], [BLOCK_ALL_TAG], repr(bad))

        # offer() forwards blocked_tags to issue_grant and _start_refresher when host advertises support.
        host_direct._offer_cooldowns.clear()
        session = hts.HostSession(
            "host-a",
            capabilities={"direct", HOST_FUNCTION_TAGS_CAPABILITY},
            tls_public_key="key",
        )
        session.direct_ready = True
        session.direct_lab_info = {"server_name": "host-a.global.remoterf.net", "ca_pem": "lab-ca"}

        class _Registry:
            def session_for_host(self, _host_id):
                return session

            def request_control(self, *args, **kwargs):
                return {"answer": {"ufrag": "u", "pwd": "p", "candidates": []}}

        grant_calls = []
        refresher_calls = []
        with mock.patch.object(hts, "get_tunnel_registry", return_value=_Registry()), \
                mock.patch.object(host_direct, "issue_grant", side_effect=lambda *a: grant_calls.append(a) or "g"), \
                mock.patch.object(host_direct, "_start_refresher", side_effect=lambda *a: refresher_calls.append(a)):
            ans = host_direct.offer(
                device_manager.VirtualDevice(gid=7, host_id="host-a", device_id="7"),
                "{}",
                rid="res-9",
                reservation_end=2000,
                still_live=lambda: True,
                tag="tag-1",
                blocked_tags=("TX",),
            )
        self.assertIsNotNone(ans)
        self.assertEqual(grant_calls[0][-1], ("TX",))
        self.assertEqual(refresher_calls[0][-1], ("TX",))

    def test_offer_refuses_old_host_when_blocked_tags_non_empty(self):
        host_direct._offer_cooldowns.clear()
        old_session = hts.HostSession("host-old", capabilities={"direct"}, tls_public_key="key")
        old_session.direct_ready = True
        old_session.direct_lab_info = {"server_name": "host-old.global.remoterf.net", "ca_pem": "lab-ca"}

        class _Registry:
            def session_for_host(self, _host_id):
                return old_session

            def request_control(self, *args, **kwargs):
                return {"answer": {"ufrag": "u", "pwd": "p", "candidates": []}}

        with mock.patch.object(hts, "get_tunnel_registry", return_value=_Registry()), \
                mock.patch.object(host_direct, "issue_grant") as issue_grant, \
                mock.patch.object(host_direct, "_start_refresher"):
            with self.assertRaises(RemoteRFPolicyError) as ctx:
                host_direct.offer(
                    device_manager.VirtualDevice(gid=7, host_id="host-old", device_id="7"),
                    "{}",
                    rid="res-9",
                    reservation_end=2000,
                    still_live=lambda: True,
                    tag="tag-1",
                    blocked_tags=("TX",),
                )
            self.assertEqual(str(ctx.exception), OLD_HOST_TAGS_ERROR)
            issue_grant.assert_not_called()

            # Without blocked tags, the old host still works.
            issue_grant.return_value = "g"
            ans = host_direct.offer(
                device_manager.VirtualDevice(gid=7, host_id="host-old", device_id="7"),
                "{}",
                rid="res-9",
                reservation_end=2000,
                still_live=lambda: True,
                tag="tag-1",
                blocked_tags=(),
            )
            self.assertIsNotNone(ans)

            # Malformed blocked_tags fails closed: still refuses old host and fails closed in device_manager.
            issue_grant.reset_mock()
            for bad in ("TX", "", {}, {"TX": True}, [""], [None], [123], ["TX", None]):
                self.assertEqual(device_manager._clean_blocked_tags(bad), (BLOCK_ALL_TAG,), repr(bad))
                with self.assertRaises(RemoteRFPolicyError):
                    host_direct.offer(
                        device_manager.VirtualDevice(gid=7, host_id="host-old", device_id="7"),
                        "{}",
                        rid="res-9",
                        reservation_end=2000,
                        still_live=lambda: True,
                        tag="tag-1",
                        blocked_tags=bad,
                    )
            issue_grant.assert_not_called()

    def test_federated_direct_open_raises_host_too_old_without_falling_back(self):
        from remoteRF_server.server import direct_path
        from remoteRF_server.server.federation.destination import FederationBusinessError
        from remoteRF_server.server.federation.devices import ServerDirectOpenHandler

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reservations_db = Path(tmp.name) / "reservations.sqlite"
        fake_res = mock.MagicMock()
        fake_res.federated_live_reservation.return_value = {
            "device_id": 7,
            "end_time": 2000,
            "salt": "salt",
            "hash": "hash",
            "username": "alice",
        }
        handler = ServerDirectOpenHandler(
            reservations_path=reservations_db,
            local_deployment_id="00000000-0000-4000-8000-000000000001",
            display_name="lab",
            reservation_handler=fake_res,
            rpc_manager=mock.MagicMock(),
        )
        action = types.SimpleNamespace(
            claims={
                "iss": "00000000-0000-4000-8000-000000000002",
                "sub": "remote-alice",
                "contract_id": "contract-1",
                "contract_version": 1,
            },
            request=types.SimpleNamespace(
                value={"payload": {"reservation_id": 9, "ice": "{}"}}
            ),
        )
        old_vdev = device_manager.VirtualDevice(gid=7, host_id="host-old", device_id="7")
        with mock.patch.object(device_manager, "get_device_by_id", return_value=(old_vdev, "salt", "hash")), \
                mock.patch.object(device_manager, "get_device_blocked_tags", return_value=("TX",)), \
                mock.patch.object(host_direct, "offer", side_effect=RemoteRFPolicyError(OLD_HOST_TAGS_ERROR)), \
                mock.patch.object(direct_path, "offer_bound") as offer_bound:
            with self.assertRaises(FederationBusinessError) as ctx:
                handler.finalize(action, {})
        self.assertEqual(ctx.exception.code, "host_too_old_for_restrictions")
        offer_bound.assert_not_called()

    def test_federated_direct_open_refreshes_stale_device_state_and_refuses_mismatch(self):
        from remoteRF_server.server import direct_path
        from remoteRF_server.server.federation.destination import FederationBusinessError
        from remoteRF_server.server.federation.devices import ServerDirectOpenHandler

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reservations_db = Path(tmp.name) / "reservations.sqlite"
        action = types.SimpleNamespace(
            claims={
                "iss": "00000000-0000-4000-8000-000000000002",
                "sub": "remote-alice",
                "contract_id": "contract-1",
                "contract_version": 1,
            },
            request=types.SimpleNamespace(
                value={"payload": {"reservation_id": 9, "ice": "{}"}}
            ),
        )

        vdev = device_manager.VirtualDevice(gid=7, host_id="host-a", device_id="7", kind="adalm_pluto")
        st = device_manager._DeviceState(
            dev=vdev,
            origin="host",
            online=True,
            salt="",
            hsh="",
            name="host-pluto",
            ident="serial-7",
            dtype="adalm_pluto",
            blocked_tags=(),
        )
        with device_manager._state_lock:
            saved = device_manager._devices.get(7)
            device_manager._devices[7] = st
        try:
            # 1) Stale idle state -> update_devices() applies live TX-blocked holder -> offer gets ("TX",).
            fake_res = mock.MagicMock()
            fake_res.federated_live_reservation.return_value = {
                "device_id": 7,
                "end_time": 2000,
                "salt": "live-salt",
                "hash": "live-hash",
                "username": "alice",
            }
            fake_res.update_devices.side_effect = lambda: device_manager.set_device(
                7, "live-salt", "live-hash", blocked_tags=("TX",)
            )
            handler = ServerDirectOpenHandler(
                reservations_path=reservations_db,
                local_deployment_id="00000000-0000-4000-8000-000000000001",
                display_name="lab",
                reservation_handler=fake_res,
                rpc_manager=mock.MagicMock(),
            )
            seen_blocked = []
            with mock.patch.object(device_manager, "_sync_host_devices"), \
                    mock.patch.object(device_manager, "_start_host_reservation"), \
                    mock.patch.object(device_manager, "_reset_if_pending"), \
                    mock.patch.object(device_manager, "_reservation_end_callbacks", []), \
                    mock.patch.object(
                        host_direct,
                        "offer",
                        side_effect=lambda *a, **kw: seen_blocked.append(kw["blocked_tags"]) or {"ufrag": "u", "pwd": "p", "candidates": []},
                    ), \
                    mock.patch.object(direct_path, "offer_bound") as offer_bound:
                out = handler.finalize(action, {})
            self.assertEqual(out["status"], "ok")
            fake_res.update_devices.assert_called_once()
            self.assertEqual(seen_blocked, [("TX",)])
            offer_bound.assert_not_called()

            # 2) Back-to-back handoff from TX-blocked prior holder to unrestricted holder -> offer gets ().
            seen_blocked.clear()
            fake_res.update_devices.reset_mock()
            fake_res.federated_live_reservation.return_value = {
                "device_id": 7,
                "end_time": 3000,
                "salt": "next-salt",
                "hash": "next-hash",
                "username": "bob",
            }
            fake_res.update_devices.side_effect = lambda: device_manager.set_device(
                7, "next-salt", "next-hash", blocked_tags=()
            )
            with mock.patch.object(device_manager, "_sync_host_devices"), \
                    mock.patch.object(device_manager, "_start_host_reservation"), \
                    mock.patch.object(device_manager, "_reset_if_pending"), \
                    mock.patch.object(device_manager, "_reservation_end_callbacks", []), \
                    mock.patch.object(
                        host_direct,
                        "offer",
                        side_effect=lambda *a, **kw: seen_blocked.append(kw["blocked_tags"]) or {"ufrag": "u", "pwd": "p", "candidates": []},
                    ), \
                    mock.patch.object(direct_path, "offer_bound") as offer_bound:
                out = handler.finalize(action, {})
            self.assertEqual(out["status"], "ok")
            fake_res.update_devices.assert_called_once()
            self.assertEqual(seen_blocked, [()])
            offer_bound.assert_not_called()

            # 3) If device_manager still does not carry live (salt, hash) after refresh, refuse cleanly.
            fake_res.update_devices.reset_mock()
            fake_res.update_devices.side_effect = None
            fake_res.federated_live_reservation.return_value = {
                "device_id": 7,
                "end_time": 4000,
                "salt": "unapplied-salt",
                "hash": "unapplied-hash",
                "username": "carol",
            }
            with mock.patch.object(device_manager, "_sync_host_devices"), \
                    mock.patch.object(host_direct, "offer") as offer, \
                    mock.patch.object(direct_path, "offer_bound") as offer_bound:
                with self.assertRaises(FederationBusinessError) as ctx:
                    handler.finalize(action, {})
            self.assertEqual(ctx.exception.code, "direct_path_unavailable")
            fake_res.update_devices.assert_called_once()
            offer.assert_not_called()
            offer_bound.assert_not_called()

            # 4) Atomic check rejects mismatched expected_reservation directly.
            with self.assertRaises(device_manager.ReservationChangedError):
                device_manager.get_device_blocked_tags(7, expected_reservation=("wrong-s", "wrong-h"))
        finally:
            with device_manager._state_lock:
                if saved is None:
                    device_manager._devices.pop(7, None)
                else:
                    device_manager._devices[7] = saved

    def test_tunnel_forward_request_strips_master_bypass_only_for_old_hosts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(hts, "cfg_dir_from_file", return_value=Path(tmp.name)):
            reg = hts.HostTunnelRegistry()

        captured = {}

        def _attach(host_id: str, device_id: str, capabilities: set[str]):
            sess = hts.HostSession(host_id, capabilities=capabilities)

            def _fake_send(frame, timeout=2.0):
                captured[host_id] = frame
                req_id = frame.rpc_request.req_id
                with sess.inflight_lock:
                    fut = sess.inflight.get(req_id)
                if fut is not None and not fut.done():
                    fut.set_result(hts.host_tunnel_pb2.RpcResponse(req_id=req_id, ok=True))
                return True

            sess.send = _fake_send
            with reg._lock:
                reg._hosts[host_id] = sess
                reg._routes[device_id] = hts.DeviceRoute(host_id=host_id, host_local_id=1)

        _attach("host-old", "10", {"reservation_tag"})
        _attach("host-new", "11", {"reservation_tag", HOST_FUNCTION_TAGS_CAPABILITY})

        req = grpc_pb2.GenericRPCRequest(function_name="tagged_radio:tx_lo:SET")
        req.args["__rrf_master_bypass"].CopyFrom(map_arg(True))
        req.args["tx_lo"].CopyFrom(map_arg(2_400_000_000))

        reg.forward_request(device_id="10", request=req, timeout_sec=1.0)
        reg.forward_request(device_id="11", request=req, timeout_sec=1.0)

        self.assertNotIn("__rrf_master_bypass", captured["host-old"].rpc_request.request.args)
        self.assertIn("__rrf_master_bypass", captured["host-new"].rpc_request.request.args)
        self.assertIs(unmap_arg(captured["host-new"].rpc_request.request.args["__rrf_master_bypass"]), True)


if __name__ == "__main__":
    unittest.main()
