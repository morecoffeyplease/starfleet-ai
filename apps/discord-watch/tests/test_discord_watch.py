import base64
import contextlib
from datetime import datetime, timezone
import inspect
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import queue
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import discord_watch as dw
from discord_gateway import GatewayClient
import hailing_frequencies as hf


THREAD = "100"
USER_ID = "500"
SESSION_A = "11111111-1111-4111-8111-111111111111".upper()
SESSION_B = "22222222-2222-4222-8222-222222222222".upper()
SURFACE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper()
SURFACE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb".upper()
WORKSPACE_A = "cccccccc-cccc-4ccc-8ccc-cccccccccccc".upper()
WORKSPACE_B = "dddddddd-dddd-4ddd-8ddd-dddddddddddd".upper()
TEST_TIMESTAMP = "2026-11-01T12:00:00.000000+00:00"
TEST_NOW = dw.timestamp_epoch(TEST_TIMESTAMP)


def target(session=SESSION_A, surface=SURFACE_A, workspace=WORKSPACE_A, harness="codex", **extra):
    value = {"harness": harness, "session": session, "surface": surface,
             "workspace": workspace, "started_at": 1234.5,
             "label": "owner-label", "cutoff": 1234.5}
    value.update(extra)
    return value


def message(message_id, author_id=USER_ID, content="Configured user reply", author_name="Configured user", bot=False,
            webhook_id=None, attachments=None, timestamp=TEST_TIMESTAMP):
    return {"id": str(message_id), "author": {"id": str(author_id), "username": author_name,
                                                  "global_name": author_name, "bot": bot},
            "content": content, "timestamp": timestamp,
            "webhook_id": webhook_id, "attachments": attachments or []}


def timestamp_at(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class FakeAPI:
    def __init__(self, rows=None, remote=None):
        self.rows = list(rows or [])
        self.remote = remote or {"id": THREAD, "type": 11,
                                 "thread_metadata": {"archived": False}, "applied_tags": []}
        self.fail = None
        self.calls = []

    def get(self, path, query=None):
        self.calls.append((path, query))
        if self.fail:
            raise self.fail
        if path.startswith("/channels/") and not path.endswith("/messages"):
            remote = dict(self.remote)
            remote["id"] = path.split("/")[2]
            return remote
        if path.endswith("/messages"):
            limit = int(query["limit"])
            if "after" in query:
                cursor = int(query["after"])
                selected = (row for row in self.rows if int(row["id"]) > cursor)
            else:
                cursor = int(query["before"])
                selected = (row for row in self.rows if int(row["id"]) < cursor)
            rows = sorted(selected, key=lambda row: int(row["id"]), reverse=True)
            return rows[:limit]
        raise AssertionError(f"Unexpected API path: {path}")


class FakeOpenCodeAPI:
    def __init__(self, session, directory, password):
        self.session = session
        self.directory = str(directory)
        self.password = password
        self.active = {}
        self.malformed_info = False
        self.version = hf.opencode_adapter_module().SUPPORTED_OPENCODE_VERSION
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def respond(self, status, value, headers=None):
                body = json.dumps(value).encode("utf-8")
                self.send_response(status)
                for name, content in (headers or {}).items():
                    self.send_header(name, content)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                expected = "Basic " + base64.b64encode(
                    f"opencode:{owner.password}".encode("utf-8")).decode("ascii")
                if self.headers.get("Authorization") != expected:
                    self.respond(401, {}, {"WWW-Authenticate": 'Basic realm="OpenCode fixture"'})
                    return
                if self.path == "/api/info":
                    info = {"version": owner.version, "pid": os.getpid(), "urls": [],
                            "paths": {"tmp": "/tmp/opencode-fixture"}}
                    if owner.malformed_info:
                        info.pop("version")
                    self.respond(200, info)
                elif self.path == f"/api/session/{owner.session}":
                    self.respond(200, {"data": {"id": owner.session, "projectID": "prj_fixture",
                                                 "location": {"directory": owner.directory},
                                                 "time": {"archived": None}, "subpath": ""}})
                elif self.path == "/api/session/active":
                    self.respond(200, {"data": owner.active})
                elif self.path in (f"/api/session/{owner.session}/inbox",
                                   f"/api/session/{owner.session}/permission",
                                   f"/api/session/{owner.session}/form"):
                    self.respond(200, {"data": []})
                else:
                    self.respond(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class FakeCmux:
    def __init__(self):
        self.fail_binding = None
        self.hold_submit = None
        self.uncertain = False
        self.prompts = []
        self.submit_count = 0
        self.lock = threading.Lock()
        self.on_submit = None
        self.entered = None
        self.release = None

    def binding(self, current, *, readiness_policy=hf.NATIVE_PROMPT_READINESS):
        if self.fail_binding:
            raise ValueError(self.fail_binding)
        return True

    def owner_identity_binding(self, current):
        if current.get("harness") != "opencode":
            raise ValueError("Owner identity check requires OpenCode")
        return self.binding(current)

    def enrollment_binding(self, current):
        return self.binding(current)

    def submit(self, current, text, on_transport_start=None, *,
               readiness_policy=hf.NATIVE_PROMPT_READINESS):
        self.binding(current, readiness_policy=readiness_policy)
        if self.hold_submit:
            raise hf.DeliveryHoldError(self.hold_submit)
        if on_transport_start:
            on_transport_start()
        with self.lock:
            self.submit_count += 1
            self.prompts.append(text)
        if self.entered is not None:
            self.entered.set()
            self.release.wait(timeout=3)
        if self.on_submit:
            self.on_submit()
        if self.uncertain:
            raise ValueError("CMUX did not confirm the submit")
        return {"submitted": True}


class FakeWatch(dw.Watch):
    def __init__(self, *args, api=None, cmux=None, now=None, **kwargs):
        self.api = api or FakeAPI()
        self.values = {"token": "private-test-token", "user_id": USER_ID, "closed_tag": "900"}
        self.fake_cmux = cmux or FakeCmux()
        self.now = now if now is not None else [TEST_NOW]
        super().__init__(*args, clock=lambda: self.now[0], cmux_factory=lambda: self.fake_cmux, **kwargs)

    def read_credentials(self):
        self._configured_user_id = self.values["user_id"]
        return self.values, self.api


class DiscordWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.notify_home = self.root / "agent-notify"
        self.hailing = self.root / "hailing"
        self.notify_home.mkdir(mode=0o700)
        self.hailing.mkdir(mode=0o700)
        self.targets = [target()]
        self._write_hailing()
        self.api = FakeAPI()
        self.cmux = FakeCmux()
        self.clock = [TEST_NOW]
        self.app = FakeWatch(self.state, self.hailing, self.notify_home, api=self.api,
                             cmux=self.cmux, now=self.clock)
        self.old_env = {key: os.environ.get(key) for key in (
            "CMUX_AGENT_LAUNCH_KIND", "CMUX_SURFACE_ID", "CMUX_WORKSPACE_ID",
            "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID")}
        os.environ["CMUX_AGENT_LAUNCH_KIND"] = "codex"
        os.environ["CMUX_SURFACE_ID"] = SURFACE_A
        os.environ["CMUX_WORKSPACE_ID"] = WORKSPACE_A
        os.environ["CODEX_THREAD_ID"] = SESSION_A
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ.pop("OPENCODE_SESSION_ID", None)

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def _write_hailing(self):
        path = self.hailing / "config.json"
        path.write_text(json.dumps({"version": 1, "targets": self.targets, "coordinator": None,
                                   "remind_after": 300, "escalate_after": 900,
                                   "cooldown": 600, "interval": 30}), encoding="utf-8")
        path.chmod(0o600)

    def register(self, after=None):
        return self.app.register(THREAD, after)

    def messages(self):
        with contextlib.closing(self.app.database(create=False)) as db:
            return db.execute("SELECT message_id,state,wake_attempts,body FROM messages WHERE thread_id=? "
                              "ORDER BY length(message_id),message_id", (THREAD,)).fetchall()

    def thread_cursor(self):
        with contextlib.closing(self.app.database(create=False)) as db:
            return db.execute("SELECT cursor FROM threads WHERE thread_id=?", (THREAD,)).fetchone()[0]

    def thread_lifecycle(self):
        with contextlib.closing(self.app.database(create=False)) as db:
            return db.execute("SELECT is_open,subscription_state,last_activity_at,remote_closed,cursor,"
                              "catchup_before,catchup_max,initial_cutoff FROM threads WHERE thread_id=?",
                              (THREAD,)).fetchone()

    def wake(self):
        with contextlib.closing(self.app.database(create=False)) as db:
            thread = self.app._thread(db, THREAD, require_open=False)
            return self.app._wake_one(db, thread, self.clock[0])

    def gateway(self, message_id, content="reply", attachments=None, author_id=USER_ID):
        row = message(str(message_id), content=content, attachments=attachments,
                      author_id=author_id, timestamp=timestamp_at(self.clock[0]))
        return self.app.ingest_gateway_message(THREAD, row)

    def delivery_groups(self):
        with contextlib.closing(self.app.database(create=False)) as db:
            return db.execute("SELECT id,owner_generation,status,attempt_count,legacy FROM delivery_groups "
                              "WHERE thread_id=? ORDER BY id", (THREAD,)).fetchall()

    def group_members(self, group_id):
        with contextlib.closing(self.app.database(create=False)) as db:
            return db.execute("SELECT message_id FROM delivery_group_members WHERE group_id=? "
                              "ORDER BY ordinal", (group_id,)).fetchall()

    def test_legacy_closed_registration_migrates_without_losing_durable_state(self):
        self.state.mkdir(mode=0o700)
        path = self.state / "discord-watch.sqlite3"
        db = sqlite3.connect(path)
        db.executescript("""
        CREATE TABLE threads (
          thread_id TEXT PRIMARY KEY, owner_json TEXT NOT NULL, cursor TEXT NOT NULL,
          is_open INTEGER NOT NULL DEFAULT 1, registered_at REAL NOT NULL, updated_at REAL NOT NULL,
          last_poll_at REAL, next_poll_at REAL NOT NULL DEFAULT 0, catchup_before TEXT,
          catchup_max TEXT, last_error_json TEXT, remote_json TEXT NOT NULL DEFAULT '{}',
          owner_generation INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE messages (
          thread_id TEXT NOT NULL REFERENCES threads(thread_id), message_id TEXT NOT NULL,
          author_id TEXT NOT NULL, author_name TEXT NOT NULL, body TEXT NOT NULL,
          attachments_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', wake_attempts INTEGER NOT NULL DEFAULT 0,
          last_wake_at REAL, submitted_at REAL, received_at REAL, last_error_json TEXT,
          PRIMARY KEY(thread_id,message_id)
        );
        CREATE TABLE ownership_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL,
          from_session TEXT NOT NULL, to_session TEXT NOT NULL, at REAL NOT NULL,
          owner_generation INTEGER NOT NULL
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        owner_json = json.dumps(self.app.binding_snapshot(self.targets[0]), separators=(",", ":"))
        remote_json = json.dumps({"id": THREAD, "type": 11,
                                  "thread_metadata": {"archived": True}, "applied_tags": []})
        db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (THREAD, owner_json, "150", 0, TEST_NOW - 500, TEST_NOW - 10, TEST_NOW - 20,
                    TEST_NOW + 5, "180", "220", json.dumps({"error": "preserved"}), remote_json, 4))
        db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (THREAD, "151", USER_ID, "Configured user", "already received", "[]",
                    timestamp_at(TEST_NOW - 40), "received", 1, TEST_NOW - 35, TEST_NOW - 35,
                    TEST_NOW - 30, None))
        attachments = json.dumps([{"filename": "kept.png", "url": "https://example.invalid/kept.png"}])
        db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (THREAD, "152", USER_ID, "Configured user", "uncertain body", attachments,
                    timestamp_at(TEST_NOW - 25), "uncertain", 2, TEST_NOW - 20, None, None,
                    json.dumps({"uncertain": "preserved"})))
        db.commit()
        db.close()
        path.chmod(0o600)

        with contextlib.closing(self.app.database(create=False)) as migrated:
            thread = migrated.execute(
                "SELECT owner_json,cursor,is_open,registered_at,updated_at,last_poll_at,next_poll_at,"
                "catchup_before,catchup_max,last_error_json,owner_generation,subscription_state,"
                "last_activity_at,remote_closed,initial_cutoff FROM threads WHERE thread_id=?",
                (THREAD,)).fetchone()
            messages = migrated.execute(
                "SELECT message_id,body,attachments_json,state,wake_attempts,last_wake_at,submitted_at,"
                "received_at,last_error_json FROM messages WHERE thread_id=? ORDER BY message_id", (THREAD,)).fetchall()
        self.assertEqual(thread[:11], (owner_json, "150", 0, TEST_NOW - 500, TEST_NOW - 10,
                                       TEST_NOW - 20, TEST_NOW + 5, "180", "220",
                                       json.dumps({"error": "preserved"}), 4))
        self.assertEqual(thread[11:], ("active", TEST_NOW - 25, 1, "150"))
        self.assertEqual(messages[0][0:4], ("151", "already received", "[]", "received"))
        self.assertEqual(messages[1][0:5], ("152", "uncertain body", attachments, "uncertain", 2))
        self.assertEqual(messages[1][5:8], (TEST_NOW - 20, None, None))
        self.assertEqual(json.loads(messages[1][8]), {"uncertain": "preserved"})
        self.api.fail = dw.DiscordAPIError(403, "synthetic migrated-thread probe", code=50001)
        report = self.app.scan()
        self.assertEqual(report["threads"][0]["status"], "backoff")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.thread_lifecycle()[4:], ("150", "180", "220", "150"))
        self.clock[0] = TEST_NOW + 5
        report = self.app.scan()
        self.assertEqual(report["threads"][0]["status"], "error")
        self.assertEqual(self.api.calls[0][0], f"/channels/{THREAD}")
        self.assertEqual(self.thread_lifecycle()[4:], ("150", "180", "220", "150"))
        self.assertEqual([row[1] for row in self.messages()], ["received", "uncertain"])
        activity_before_replay = self.thread_lifecycle()[2]
        replay = message("149", timestamp=timestamp_at(self.clock[0] + 100))
        self.assertFalse(self.app.ingest_gateway_message(THREAD, replay)["ingested"])
        self.assertEqual(self.thread_lifecycle()[2], activity_before_replay)
        self.assertEqual([row[0] for row in self.messages()], ["151", "152"])

    def test_legacy_message_states_become_singleton_groups_without_resetting_attempts(self):
        self.register()
        states = (("101", "pending", 0), ("102", "awaiting_receipt", 2),
                  ("103", "stalled", 3), ("104", "uncertain", 1), ("105", "received", 1))
        with contextlib.closing(self.app.database(create=False)) as db:
            with db:
                for offset, (message_id, state, attempts) in enumerate(states):
                    last_wake_at = TEST_NOW - 20 + offset if attempts else None
                    submitted_at = last_wake_at if state == "awaiting_receipt" else None
                    received_at = last_wake_at if state == "received" else None
                    db.execute(
                        "INSERT INTO messages(thread_id,message_id,author_id,author_name,body,attachments_json,"
                        "created_at,state,wake_attempts,last_wake_at,submitted_at,received_at,groupable,ingested_at,"
                        "owner_generation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?,1)",
                        (THREAD, message_id, USER_ID, "Configured user", f"legacy {state}", "[]",
                         timestamp_at(TEST_NOW - 30 + offset), state, attempts, last_wake_at,
                         submitted_at, received_at, TEST_NOW - 30 + offset))
        groups = self.delivery_groups()
        self.assertEqual([(row[1], row[2], row[3], row[4]) for row in groups],
                         [(1, "eligible", 0, 1), (1, "awaiting_receipt", 2, 1),
                          (1, "stalled", 3, 1), (1, "uncertain", 1, 1)])
        self.assertEqual([(row[0], row[1], row[2]) for row in self.messages()],
                         [("101", "pending", 0), ("102", "awaiting_receipt", 2),
                          ("103", "stalled", 3), ("104", "uncertain", 1), ("105", "received", 1)])
        self.assertEqual([self.group_members(group[0])[0][0] for group in groups],
                         ["101", "102", "103", "104"])

    def test_poll_and_retry_bookkeeping_do_not_refresh_meaningful_activity(self):
        self.register()
        self.api.fail = dw.DiscordAPIError(403, "temporary synthetic denial", code=50001)
        self.app.scan()
        self.assertIsNone(self.thread_lifecycle()[2])
        self.api.fail = None
        self.clock[0] = TEST_NOW + dw.POLL_INTERVAL + 1
        self.app.scan()
        self.assertIsNone(self.thread_lifecycle()[2])
        with contextlib.closing(self.app.database(create=False)) as db:
            with db:
                db.execute("INSERT INTO meta(key,value) VALUES('last_worker_tick_at',?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(self.clock[0]),))
        self.clock[0] = TEST_NOW + dw.DORMANT_AFTER
        self.app.scan()
        self.assertEqual(self.thread_lifecycle()[1], "active")
        self.clock[0] += 1
        previous_poll = self.thread_lifecycle()[4]
        self.api.calls.clear()
        self.app.scan()
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.thread_lifecycle()[1:3], ("dormant", None))
        self.assertEqual(self.thread_lifecycle()[4], previous_poll)

    def test_gateway_replies_reset_seven_day_window_without_touching_rest_catchup(self):
        self.register(after="150")
        with contextlib.closing(self.app.database(create=False)) as db:
            with db:
                db.execute("UPDATE threads SET cursor='180',catchup_before='185',catchup_max='190' "
                           "WHERE thread_id=?",
                           (THREAD,))
        first_at = TEST_NOW + 24 * 60 * 60
        self.clock[0] = first_at
        first = message("170", timestamp=timestamp_at(first_at))
        self.assertTrue(self.app.ingest_gateway_message(THREAD, first)["ingested"])
        self.assertEqual(self.thread_lifecycle()[4:], ("180", "185", "190", "150"))
        self.assertEqual(self.thread_lifecycle()[2], first_at)

        replay = message("170", timestamp=timestamp_at(first_at + 5 * 24 * 60 * 60))
        self.assertFalse(self.app.ingest_gateway_message(THREAD, replay)["ingested"])
        older = message("160", timestamp=timestamp_at(first_at - 24 * 60 * 60))
        self.assertTrue(self.app.ingest_gateway_message(THREAD, older)["ingested"])
        self.assertEqual(self.thread_lifecycle()[2], first_at)
        self.assertEqual(self.thread_cursor(), "180")
        self.assertEqual([row[0] for row in self.messages()], ["160", "170"])

        second_at = first_at + 6 * 24 * 60 * 60
        self.clock[0] = second_at
        second = message("175", timestamp=timestamp_at(second_at))
        self.assertTrue(self.app.ingest_gateway_message(THREAD, second)["ingested"])
        self.assertEqual(self.thread_lifecycle()[2], second_at)
        restarted = FakeWatch(self.state, self.hailing, self.notify_home, api=self.api,
                              cmux=self.cmux, now=self.clock)
        self.app = restarted
        self.assertEqual(self.thread_lifecycle()[2], second_at)
        self.assertEqual(self.thread_lifecycle()[7], "150")
        self.assertEqual([row[0] for row in self.messages()], ["160", "170", "175"])
        self.clock[0] = second_at + dw.DORMANT_AFTER
        self.app.scan()
        self.assertEqual(self.thread_lifecycle()[1], "active")
        self.clock[0] += 1
        report = self.app.scan()
        self.assertEqual(report["threads"][0]["status"], "dormant")
        self.assertEqual(self.thread_lifecycle()[1], "dormant")

    def test_gateway_ingest_filters_unknown_threads_and_preserves_body_and_attachments(self):
        self.register()
        filtered = self.app.ingest_gateway_message(THREAD, message("101", author_id="999"))
        self.assertFalse(filtered["ingested"])
        with self.assertRaisesRegex(ValueError, "not registered"):
            self.app.ingest_gateway_message("999", message("102"))
        mismatched = message("102")
        mismatched["channel_id"] = "200"
        with self.assertRaisesRegex(ValueError, "channel ID does not match"):
            self.app.ingest_gateway_message(THREAD, mismatched)
        self.assertEqual(self.messages(), [])
        url = "https://cdn.discordapp.com/attachments/100/103/late.png"
        gateway_event = message("103", content="gateway body",
                                attachments=[{"filename": "late.png", "url": url}])
        gateway_event["channel_id"] = THREAD
        received = self.app.ingest_gateway_message(THREAD, gateway_event)
        self.assertTrue(received["ingested"])
        self.assertFalse(received["rest_cursor_advanced"])
        self.assertEqual(self.thread_cursor(), THREAD)
        self.api.rows = [gateway_event]
        self.app.scan()
        self.assertEqual(self.thread_cursor(), "103")
        self.assertEqual(len(self.messages()), 1)
        output = io.StringIO()
        self.app.receive(THREAD, "103", output)
        self.assertIn("gateway body", output.getvalue())
        self.assertIn("[attachment] late.png: " + url, output.getvalue())

    def test_gateway_dispatch_commits_sequence_with_intake_and_rolls_back_failures(self):
        self.register(after="100")
        invalid = message("101", content={"not": "text"})
        invalid["channel_id"] = THREAD
        with self.assertRaisesRegex(ValueError, "content is not text"):
            self.app.commit_gateway_dispatch(10, "MESSAGE_CREATE", invalid, USER_ID)
        self.assertIsNone(self.app.gateway_session()["sequence"])
        self.assertEqual(self.messages(), [])

        accepted = message("103", content="committed with sequence")
        accepted["channel_id"] = THREAD
        committed = self.app.commit_gateway_dispatch(10, "MESSAGE_CREATE", accepted, USER_ID)
        self.assertTrue(committed["committed"])
        self.assertTrue(committed["ingested"])
        self.assertEqual(self.app.gateway_session()["sequence"], "10")
        duplicate = message("102", content="must not replay")
        duplicate["channel_id"] = THREAD
        self.assertTrue(self.app.commit_gateway_dispatch(10, "MESSAGE_CREATE", duplicate, USER_ID)["duplicate"])
        self.assertEqual([row[0] for row in self.messages()], ["103"])

        unrelated = message("104", author_id="999")
        unrelated["channel_id"] = THREAD
        ignored = self.app.commit_gateway_dispatch(11, "MESSAGE_CREATE", unrelated, USER_ID)
        self.assertTrue(ignored["committed"])
        self.assertFalse(ignored["ingested"])
        self.assertEqual(self.app.gateway_session()["sequence"], "11")
        self.assertEqual([row[0] for row in self.messages()], ["103"])
        self.app = FakeWatch(self.state, self.hailing, self.notify_home, api=self.api,
                             cmux=self.cmux, now=self.clock)
        self.assertEqual(self.app.gateway_session()["sequence"], "11")
        self.assertTrue(self.app.commit_gateway_dispatch(10, "MESSAGE_CREATE", duplicate,
                                                         USER_ID)["duplicate"])
        self.assertEqual([row[0] for row in self.messages()], ["103"])

    def test_gateway_intake_serializes_with_authorized_owner_transfer(self):
        self.register(after="100")
        owner_b = self.app.binding_snapshot(
            target(session=SESSION_B, surface=SURFACE_B, workspace=WORKSPACE_B))
        original_database = self.app.database
        main_connection = None
        transfer_started = threading.Event()
        intake_owner_read = threading.Event()
        transfer_write_attempted = threading.Event()
        transfer_errors = []
        transfer_connection = threading.local()

        class InterleavingCursor:
            def __init__(self, cursor, after_fetch=None):
                self.cursor = cursor
                self.after_fetch = after_fetch

            def fetchone(self):
                row = self.cursor.fetchone()
                if self.after_fetch:
                    self.cursor.close()
                    self.after_fetch()
                return row

        class InterleavingConnection:
            def __init__(self, connection, transfer=False):
                self.connection = connection
                self.transfer = transfer

            def execute(self, statement, parameters=()):
                normalized = " ".join(statement.split())
                if self.transfer and (normalized == "BEGIN IMMEDIATE" or
                                      normalized.startswith("UPDATE threads SET owner_json")):
                    transfer_write_attempted.set()
                cursor = self.connection.execute(statement, parameters)
                if normalized.startswith("SELECT last_activity_at,initial_cutoff,owner_generation"):
                    def after_owner_read():
                        intake_owner_read.set()
                        if not transfer_write_attempted.wait(timeout=2):
                            raise AssertionError("Owner transfer did not reach the intake serialization boundary")
                    return InterleavingCursor(cursor, after_owner_read)
                return cursor

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def __enter__(self):
                self.connection.__enter__()
                return self

            def __exit__(self, *args):
                return self.connection.__exit__(*args)

            def close(self):
                self.connection.close()

        def instrumented_database(create=True):
            prepared = getattr(transfer_connection, "connection", None)
            if prepared is not None:
                transfer_connection.connection = None
                return prepared
            return main_connection

        def transfer():
            try:
                transfer_connection.connection = InterleavingConnection(
                    original_database(create=False), transfer=True)
                transfer_started.set()
                if not intake_owner_read.wait(timeout=2):
                    raise AssertionError("Gateway intake did not materialize its owner generation")
                self.app.transfer(THREAD, SESSION_A)
            except BaseException as error:
                transfer_errors.append(error)
            finally:
                connection = getattr(transfer_connection, "connection", None)
                if connection is not None:
                    with contextlib.suppress(sqlite3.Error):
                        connection.close()

        event = message("101", content="transfer race reply", timestamp=TEST_TIMESTAMP)
        event["channel_id"] = THREAD
        main_connection = InterleavingConnection(original_database(create=False))
        transfer_thread = threading.Thread(target=transfer)
        try:
            with mock.patch.object(self.app, "database", side_effect=instrumented_database), \
                    mock.patch.object(self.app, "current_owner", return_value=owner_b):
                transfer_thread.start()
                self.assertTrue(transfer_started.wait(timeout=2))
                result = self.app.commit_gateway_dispatch(10, "MESSAGE_CREATE", event, USER_ID)
                transfer_thread.join(timeout=3)
        finally:
            intake_owner_read.set()
            transfer_write_attempted.set()
            if transfer_thread.ident is not None:
                transfer_thread.join(timeout=3)
            with contextlib.suppress(sqlite3.Error):
                main_connection.close()

        self.assertFalse(transfer_thread.is_alive())
        self.assertEqual(transfer_errors, [])
        self.assertTrue(result["ingested"])
        self.assertEqual(self.app.gateway_session()["sequence"], "10")
        with contextlib.closing(original_database(create=False)) as db:
            generation = db.execute("SELECT owner_generation FROM messages WHERE thread_id=? AND message_id='101'",
                                     (THREAD,)).fetchone()[0]
            thread_owner, thread_generation, cutoff, activity = db.execute(
                "SELECT owner_json,owner_generation,initial_cutoff,last_activity_at FROM threads WHERE thread_id=?",
                (THREAD,)).fetchone()
        self.assertEqual(generation, 2)
        self.assertEqual(thread_generation, 2)
        self.assertEqual(json.loads(thread_owner), owner_b)
        self.assertEqual(cutoff, "100")
        self.assertEqual(activity, TEST_NOW)
        output = io.StringIO()
        with mock.patch.object(self.app, "current_owner", return_value=owner_b):
            received = self.app.receive(THREAD, output=output)
        self.assertEqual(received["received"], ["101"])
        self.assertIn("transfer race reply", output.getvalue())

    def test_competing_explicit_transfers_from_one_owner_commit_only_one_generation(self):
        owner_b_full = target(session=SESSION_B, surface=SURFACE_B, workspace=WORKSPACE_B)
        session_c = "33333333-3333-4333-8333-333333333333"
        surface_c = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee".upper()
        workspace_c = "ffffffff-ffff-4fff-8fff-ffffffffffff".upper()
        owner_c_full = target(session=session_c, surface=surface_c, workspace=workspace_c)
        owner_b = self.app.binding_snapshot(owner_b_full)
        owner_c = self.app.binding_snapshot(owner_c_full)
        self.targets.extend((owner_b_full, owner_c_full))
        self._write_hailing()
        self.register()
        self.gateway("101", "pending through one competing transfer")

        stale_reads = threading.Barrier(2, timeout=2)
        start_transfers = threading.Event()
        ready = [threading.Event(), threading.Event()]
        results = []
        outcomes = []
        adapters = [
            FakeWatch(self.state, self.hailing, self.notify_home, api=self.api,
                      cmux=self.cmux, now=self.clock),
            FakeWatch(self.state, self.hailing, self.notify_home, api=self.api,
                      cmux=self.cmux, now=self.clock),
        ]
        owners = (owner_b, owner_c)
        database_locals = (threading.local(), threading.local())
        original_databases = tuple(adapter.database for adapter in adapters)

        class InterleavingCursor:
            def __init__(self, cursor, wait_for_competitor):
                self.cursor = cursor
                self.wait_for_competitor = wait_for_competitor

            def fetchone(self):
                row = self.cursor.fetchone()
                self.cursor.close()
                if self.wait_for_competitor:
                    stale_reads.wait()
                return row

        class InterleavingConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, statement, parameters=()):
                normalized = " ".join(statement.split())
                wait_for_competitor = (
                    normalized.startswith("SELECT thread_id,owner_json,cursor,is_open") and
                    not self.connection.in_transaction)
                cursor = self.connection.execute(statement, parameters)
                if wait_for_competitor:
                    return InterleavingCursor(cursor, True)
                return cursor

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def __enter__(self):
                self.connection.__enter__()
                return self

            def __exit__(self, *args):
                return self.connection.__exit__(*args)

            def close(self):
                self.connection.close()

        def database_factory(index, create=True):
            local = database_locals[index]
            prepared = local.connection
            local.connection = None
            return prepared

        def transfer_worker(index):
            adapter = adapters[index]
            local = database_locals[index]
            try:
                local.connection = InterleavingConnection(original_databases[index](create=False))
                ready[index].set()
                if not start_transfers.wait(timeout=2):
                    raise AssertionError("Competing transfer start was not released")
                with mock.patch.object(adapter, "current_owner", return_value=owners[index]):
                    results.append(adapter.transfer(THREAD, SESSION_A))
            except BaseException as error:
                outcomes.append(error)
            finally:
                connection = getattr(local, "connection", None)
                if connection is not None:
                    with contextlib.suppress(sqlite3.Error):
                        connection.close()

        threads = [threading.Thread(target=transfer_worker, args=(index,)) for index in range(2)]
        patchers = []
        try:
            for index, adapter in enumerate(adapters):
                patcher = mock.patch.object(adapter, "database",
                                            side_effect=lambda create=True, i=index: database_factory(i, create))
                patcher.start()
                patchers.append(patcher)
            for thread in threads:
                thread.start()
            self.assertTrue(all(event.wait(timeout=3) for event in ready))
            start_transfers.set()
            for thread in threads:
                thread.join(timeout=4)
        finally:
            start_transfers.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=4)
            for patcher in patchers:
                patcher.stop()

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], ValueError)
        self.assertIn("--from-session does not match", str(outcomes[0]))
        self.assertEqual(results[0]["from_session"], SESSION_A)
        winner = owners[0] if results[0]["to_session"] == owners[0]["session"] else owners[1]
        with contextlib.closing(self.app.database(create=False)) as db:
            thread_owner, generation = db.execute(
                "SELECT owner_json,owner_generation FROM threads WHERE thread_id=?", (THREAD,)).fetchone()
            events = db.execute(
                "SELECT from_session,to_session,owner_generation FROM ownership_events WHERE thread_id=?",
                (THREAD,)).fetchall()
            message_row = db.execute(
                "SELECT state,owner_generation,wake_attempts,body FROM messages "
                "WHERE thread_id=? AND message_id='101'", (THREAD,)).fetchone()
        self.assertEqual(json.loads(thread_owner), winner)
        self.assertEqual(generation, 2)
        self.assertEqual(events, [(SESSION_A, winner["session"], 2)])
        self.assertEqual(message_row, ("pending", 2, 0, "pending through one competing transfer"))

    def test_ready_starts_durable_recovery_for_retained_dormant_threads(self):
        self.register()
        self.app.close(THREAD)
        self.app.register("101")
        self.clock[0] += dw.DORMANT_AFTER + 1
        self.app.scan()
        self.assertEqual(self.thread_lifecycle()[1], "dormant")
        ready = {"session_id": "synthetic-gateway-session",
                 "resume_gateway_url": "wss://gateway.discord.gg"}
        result = self.app.commit_gateway_dispatch(
            12, "READY", ready, USER_ID, validate_resume_url=lambda value: value)
        self.assertTrue(result["committed"])
        state = self.app.gateway_session()
        self.assertEqual(state["session_id"], "synthetic-gateway-session")
        self.assertEqual(state["sequence"], "12")
        with contextlib.closing(self.app.database(create=False)) as db:
            work = dict(db.execute("SELECT thread_id,state FROM gateway_recovery_work WHERE epoch=?",
                                   (state["recovery_epoch"],)))
        self.assertEqual(work, {THREAD: "queued", "101": "queued"})
        self.assertEqual(state["recovery_state"], "pending")
        self.app = FakeWatch(self.state, self.hailing, self.notify_home, api=self.api,
                             cmux=self.cmux, now=self.clock)
        self.assertEqual(self.app.gateway_session()["session_id"], "synthetic-gateway-session")
        self.assertEqual(self.app.gateway_session()["sequence"], "12")

    def test_status_separates_gateway_failure_from_thread_recovery_and_access(self):
        self.register()
        self.app.set_gateway_condition("fatal_configuration", "synthetic invalid Gateway URL")
        epoch = self.app.begin_gateway_recovery("synthetic reconnect")
        with contextlib.closing(self.app.database(create=False)) as db:
            with db:
                db.execute("UPDATE gateway_recovery_work SET state='blocked',last_error_json=? "
                           "WHERE epoch=? AND thread_id=?",
                           (json.dumps({"http_status": 403}), epoch, THREAD))
        status = self.app.status()
        self.assertEqual(status["gateway"]["condition"], "fatal_configuration")
        self.assertEqual(status["gateway"]["recovery"]["state"], "pending")
        self.assertIn("reported Gateway fatal condition", status["gateway"]["coordinator_guidance"])
        self.assertNotIn("credentials", status["gateway"]["coordinator_guidance"])
        self.assertNotIn("intents", status["gateway"]["coordinator_guidance"])
        thread = status["threads"][0]
        self.assertEqual(thread["rest_recovery"]["condition"], "inaccessible")
        self.assertIn("access", thread["coordinator_guidance"])

    def test_rest_worker_rotates_fairly_and_passes_a_two_page_budget(self):
        thread_ids = [str(value) for value in range(100, 110)]
        for thread_id in thread_ids:
            self.app.register(thread_id)
        observed = []

        def poll(db, thread, api, credentials, now, max_pages, routine, stop_event=None):
            observed.append((thread["thread_id"], max_pages, routine))
            return {"thread": thread["thread_id"], "status": "polled", "catchup_pending": False}

        with mock.patch.object(self.app, "_poll_thread", side_effect=poll):
            first = self.app._rest_recovery_turn()
            first_threads = [row[0] for row in observed]
            self.assertEqual(len(first_threads), dw.WORKER_REST_THREADS_PER_TURN)
            self.assertTrue(all(row[1:] == (dw.WORKER_REST_PAGES_PER_THREAD, True)
                                for row in observed))
            self.assertEqual(first["request_budget"],
                             dw.WORKER_REST_THREADS_PER_TURN *
                             (1 + dw.WORKER_REST_PAGES_PER_THREAD))
        self.app = FakeWatch(self.state, self.hailing, self.notify_home, api=self.api,
                             cmux=self.cmux, now=self.clock)
        observed.clear()
        with mock.patch.object(self.app, "_poll_thread", side_effect=poll):
            self.app._rest_recovery_turn()
        second_threads = [row[0] for row in observed]
        self.assertEqual(second_threads[:2], sorted(set(thread_ids) - set(first_threads)))

    def test_rest_worker_rotates_active_and_dormant_recovery_when_cursor_is_not_due(self):
        thread_ids = [str(value) for value in range(1000, 1032)]
        for thread_id in thread_ids:
            self.app.register(thread_id)
        self.app.begin_gateway_recovery("synthetic reconnect")

        anchor = thread_ids[15]

        def assert_rotation_continues():
            anchor_turn = self.app._rest_recovery_turn(only_thread=anchor)
            self.assertEqual([row["thread"] for row in anchor_turn["threads"]], [anchor])
            self.assertEqual(anchor_turn["threads"][0]["status"], "polled")

            self.clock[0] += 1
            first = self.app._rest_recovery_turn()
            self.assertEqual([row["thread"] for row in first["threads"]], thread_ids[16:24])
            self.assertTrue(all(row["status"] == "polled" for row in first["threads"]))

            self.clock[0] += 1
            second = self.app._rest_recovery_turn()
            self.assertEqual([row["thread"] for row in second["threads"]], thread_ids[24:32])
            self.assertTrue(all(row["status"] == "polled" for row in second["threads"]))

        assert_rotation_continues()

        self.clock[0] += dw.DORMANT_AFTER + 1
        self.app.begin_gateway_recovery("synthetic reconnect while dormant")
        assert_rotation_continues()

        with contextlib.closing(self.app.database(create=False)) as db:
            states = {row[0] for row in db.execute("SELECT DISTINCT subscription_state FROM threads")}
        self.assertEqual(states, {"dormant"})

    def test_recovery_turn_only_thread_scopes_requests_without_claiming_global_coverage(self):
        self.register()
        self.app.register("200")
        epoch = self.app.begin_gateway_recovery("synthetic reconnect")
        self.api.calls.clear()

        result = self.app._rest_recovery_turn(only_thread=THREAD)

        self.assertEqual([row["thread"] for row in result["threads"]], [THREAD])
        self.assertEqual([path for path, _ in self.api.calls],
                         [f"/channels/{THREAD}", f"/channels/{THREAD}/messages"])
        with contextlib.closing(self.app.database(create=False)) as db:
            states = dict(db.execute("SELECT thread_id,state FROM gateway_recovery_work WHERE epoch=?",
                                     (epoch,)))
        self.assertEqual(states, {THREAD: "complete", "200": "queued"})
        status = self.app.status()
        self.assertEqual(status["gateway"]["recovery"]["state"], "pending")
        self.assertEqual({row["thread"] for row in status["threads"]}, {THREAD, "200"})

    def test_complete_observe_only_scan_covers_its_thread_for_manual_receive(self):
        self.register()
        self.app.register("200")
        epoch = self.app.begin_gateway_recovery("synthetic reconnect")
        reply = message("101", content="reply recovered by explicit scan", timestamp=TEST_TIMESTAMP)
        reply["channel_id"] = THREAD
        self.assertTrue(self.app.commit_gateway_dispatch(1, "MESSAGE_CREATE", reply,
                                                         USER_ID)["ingested"])

        report = self.app.scan(only_thread=THREAD, send=False)

        self.assertEqual(report["threads"][0]["status"], "polled")
        with contextlib.closing(self.app.database(create=False)) as db:
            states = dict(db.execute("SELECT thread_id,state FROM gateway_recovery_work WHERE epoch=?",
                                     (epoch,)))
        self.assertEqual(states, {THREAD: "complete", "200": "queued"})
        self.assertEqual(self.app.gateway_session()["recovery_state"], "pending")
        output = io.StringIO()
        result = self.app.receive(THREAD, output=output)
        self.assertEqual(result["received"], ["101"])
        self.assertIn("reply recovered by explicit scan", output.getvalue())

    def test_recovery_order_barrier_holds_gateway_and_manual_pull_per_thread(self):
        self.register()
        frozen_reply = message("103", content="Frozen before recovery", timestamp=TEST_TIMESTAMP)
        frozen_reply["channel_id"] = THREAD
        self.assertTrue(self.app.commit_gateway_dispatch(1, "MESSAGE_CREATE", frozen_reply,
                                                         USER_ID)["ingested"])
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.cmux.uncertain = True
        self.assertEqual(self.wake()["state"], "uncertain")
        self.cmux.uncertain = False
        ready = {"session_id": "synthetic-recovery-session",
                 "resume_gateway_url": "wss://gateway.discord.gg"}
        self.assertTrue(self.app.commit_gateway_dispatch(
            2, "READY", ready, USER_ID, validate_resume_url=lambda value: value)["committed"])
        epoch = self.app.gateway_session()["recovery_epoch"]
        self.app = FakeWatch(self.state, self.hailing, self.notify_home, api=self.api,
                             cmux=self.cmux, now=self.clock)
        self.app.register("200")
        with contextlib.closing(self.app.database(create=False)) as db:
            recovery_work = dict(db.execute("SELECT thread_id,state FROM gateway_recovery_work WHERE epoch=?",
                                             (epoch,)))
            self.assertEqual(recovery_work, {THREAD: "queued", "200": "queued"})
            with db:
                db.execute("UPDATE gateway_recovery_work SET state='complete',completed_at=? "
                           "WHERE epoch=? AND thread_id='200'", (self.clock[0], epoch))
        unrelated_reply = message("201", content="Covered unrelated thread", timestamp=TEST_TIMESTAMP)
        unrelated_reply["channel_id"] = "200"
        self.assertTrue(self.app.ingest_gateway_message("200", unrelated_reply)["ingested"])

        newest = message("105", content="Gateway observed newer reply", timestamp=TEST_TIMESTAMP)
        newest["channel_id"] = THREAD
        self.assertTrue(self.app.commit_gateway_dispatch(3, "MESSAGE_CREATE", newest,
                                                         USER_ID)["ingested"])
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        with self.assertRaisesRegex(ValueError, "gateway_recovery_incomplete"):
            self.app.receive(THREAD, output=io.StringIO())
        frozen_output = io.StringIO()
        frozen_receipt = self.app.receive(THREAD, output=frozen_output, group_id=1)
        self.assertEqual(frozen_receipt["received"], ["103"])
        self.assertIn("Frozen before recovery", frozen_output.getvalue())
        with contextlib.closing(self.app.database(create=False)) as db:
            pending_group = db.execute("SELECT group_id FROM messages WHERE thread_id=? AND message_id='105'",
                                       (THREAD,)).fetchone()[0]
            current = self.app._thread(db, "200", require_open=False)
            unrelated = self.app._wake_one(db, current, self.clock[0])
        self.assertIsNone(pending_group)
        self.assertEqual(unrelated["state"], "awaiting_receipt")
        self.assertEqual(self.cmux.submit_count, 2)
        self.assertEqual(self.app.status()["threads"][0]["forming_group"]["hold_reason"],
                         "gateway_recovery_incomplete")

        earlier = message("101", content="REST recovered older reply", timestamp=TEST_TIMESTAMP)
        earlier["channel_id"] = THREAD
        self.api.rows = [newest, earlier]
        recovered = self.app._rest_recovery_turn(only_thread=THREAD)
        self.assertEqual(recovered["threads"][0]["status"], "polled")
        self.assertEqual(self.app.gateway_session()["recovery_state"], "complete")
        self.assertEqual(self.thread_cursor(), "105")
        output = io.StringIO()
        received = self.app.receive(THREAD, output=output)
        self.assertEqual(received["received"], ["101", "105"])
        self.assertLess(output.getvalue().index(":101]"), output.getvalue().index(":105]"))

    def test_recovery_commit_serializes_before_or_after_each_group_freeze(self):
        self.register()
        epoch_one = self.app.begin_gateway_recovery("synthetic baseline coverage")
        with contextlib.closing(self.app.database(create=False)) as db:
            with db:
                db.execute("UPDATE gateway_recovery_work SET state='complete',completed_at=? "
                           "WHERE epoch=? AND thread_id=?", (self.clock[0], epoch_one, THREAD))
                self.app._refresh_gateway_recovery_state(db, epoch_one, self.clock[0])
        self.gateway("105", "frozen before the next recovery epoch")

        original_database = self.app.database
        main_db = original_database(create=False)
        recovery_connection = [None]
        recovery_connection_lock = threading.Lock()
        recovery_ready = threading.Event()
        start_recovery = threading.Event()
        recovery_write_attempted = threading.Event()
        recovery_errors = []

        class RecoveryConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, statement, parameters=()):
                normalized = " ".join(statement.split())
                if normalized == "BEGIN IMMEDIATE":
                    recovery_write_attempted.set()
                return self.connection.execute(statement, parameters)

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def __enter__(self):
                self.connection.__enter__()
                return self

            def __exit__(self, *args):
                return self.connection.__exit__(*args)

            def close(self):
                self.connection.close()

        def database_factory(create=True):
            with recovery_connection_lock:
                prepared = recovery_connection[0]
                recovery_connection[0] = None
            if prepared is not None:
                return prepared
            return main_db

        def recover():
            connection = None
            try:
                connection = RecoveryConnection(original_database(create=False))
                with recovery_connection_lock:
                    recovery_connection[0] = connection
                recovery_ready.set()
                if not start_recovery.wait(timeout=2):
                    raise AssertionError("Synthetic recovery race was not released")
                self.app.begin_gateway_recovery("synthetic recovery committed after freeze")
            except BaseException as error:
                recovery_errors.append(error)
            finally:
                with recovery_connection_lock:
                    unclaimed = recovery_connection[0] is connection
                    if unclaimed:
                        recovery_connection[0] = None
                if unclaimed and connection is not None:
                    with contextlib.suppress(sqlite3.Error):
                        connection.close()

        recovery_thread = threading.Thread(target=recover)
        original_hold = self.app._gateway_recovery_hold_reason

        def start_recovery_after_predicate(db, thread):
            result = original_hold(db, thread)
            if thread["thread_id"] == THREAD and result is None:
                self.assertTrue(db.in_transaction)
                start_recovery.set()
                if not recovery_write_attempted.wait(timeout=2):
                    raise AssertionError("Recovery did not attempt its serialized write")
            return result

        try:
            with mock.patch.object(self.app, "database", side_effect=database_factory), \
                    mock.patch.object(self.app, "_gateway_recovery_hold_reason",
                                      side_effect=start_recovery_after_predicate):
                recovery_thread.start()
                self.assertTrue(recovery_ready.wait(timeout=2))
                thread = self.app._thread(main_db, THREAD, require_open=False)
                frozen = self.app._seal_delivery_groups(main_db, thread, self.clock[0],
                                                        force=True, max_groups=1)
                recovery_thread.join(timeout=3)
        finally:
            start_recovery.set()
            if recovery_thread.ident is not None:
                recovery_thread.join(timeout=3)
            with contextlib.suppress(sqlite3.Error):
                main_db.close()

        self.assertFalse(recovery_thread.is_alive())
        self.assertEqual(recovery_errors, [])
        self.assertEqual(frozen[0]["messages"], ["105"])
        self.assertEqual(self.app.gateway_session()["recovery_epoch"], epoch_one + 1)
        self.assertEqual(self.app.gateway_session()["recovery_state"], "pending")
        self.assertEqual(self.group_members(1), [("105",)])
        self.assertEqual(self.delivery_groups()[0][2], "eligible")

        self.gateway("106", "held after recovery committed")
        with contextlib.closing(self.app.database(create=False)) as db:
            thread = self.app._thread(db, THREAD, require_open=False)
            held = self.app._seal_delivery_groups(db, thread, self.clock[0], force=True,
                                                  max_groups=1)
        self.assertEqual(held[0]["state"], "held")
        self.assertEqual(held[0]["reason"], "gateway_recovery_incomplete")
        self.assertEqual(self.delivery_groups(), [(1, 1, "eligible", 0, 0)])

    def test_routine_rest_reconciliation_uses_the_thirty_second_due_boundary(self):
        self.register()
        calls = []

        def poll(db, thread, api, credentials, now, max_pages, routine, stop_event=None):
            calls.append(now)
            with db:
                db.execute("UPDATE threads SET last_poll_at=?,next_poll_at=? WHERE thread_id=?",
                           (now, now + dw.ROUTINE_REST_INTERVAL, thread["thread_id"]))
            return {"thread": thread["thread_id"], "status": "polled", "catchup_pending": False}

        with mock.patch.object(self.app, "_poll_thread", side_effect=poll):
            self.app._rest_recovery_turn()
            self.clock[0] = TEST_NOW + dw.ROUTINE_REST_INTERVAL - 1
            self.app._rest_recovery_turn()
            self.assertEqual(calls, [TEST_NOW])
            self.clock[0] = TEST_NOW + dw.ROUTINE_REST_INTERVAL
            self.app._rest_recovery_turn()
        self.assertEqual(calls, [TEST_NOW, TEST_NOW + dw.ROUTINE_REST_INTERVAL])

    def test_stopping_during_paginated_recovery_preserves_committed_page_and_boundary(self):
        self.register()
        stop_event = threading.Event()

        class StopAfterPageAPI(FakeAPI):
            def get(self, path, query=None):
                result = super().get(path, query)
                if path.endswith("/messages"):
                    stop_event.set()
                return result

        api = StopAfterPageAPI([message(str(value)) for value in range(101, 201)])
        self.app.api = api
        self.api = api
        credentials, selected_api = self.app.read_credentials()
        with contextlib.closing(self.app.database(create=False)) as db:
            thread = self.app._thread(db, THREAD, require_open=False)
            result = self.app._poll_thread(db, thread, selected_api, credentials, self.clock[0],
                                           max_pages=2, routine=True, stop_event=stop_event)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(self.thread_cursor(), THREAD)
        self.assertEqual(self.thread_lifecycle()[5], "101")
        self.assertEqual(len(self.messages()), 100)
        self.app = FakeWatch(self.state, self.hailing, self.notify_home, api=api,
                             cmux=self.cmux, now=self.clock)
        self.assertEqual(self.thread_cursor(), THREAD)
        self.assertEqual(self.thread_lifecycle()[5], "101")
        self.assertEqual(len(self.messages()), 100)

    def test_gateway_and_group_sender_progress_while_rest_lane_is_blocked(self):
        self.register()
        rest_entered = threading.Event()
        rest_release = threading.Event()
        ready_committed = threading.Event()
        rest_turn_completed = threading.Event()
        recovery_complete = threading.Event()
        message_ingested = threading.Event()
        heartbeat_sent = threading.Event()
        group_submitted = threading.Event()
        stop_event = threading.Event()
        class BlockingAPI(FakeAPI):
            def __init__(self):
                super().__init__()
                self.block_routine = False

            def get(self, path, query=None):
                if path == "/gateway/bot":
                    self.calls.append((path, query))
                    return {"url": "wss://gateway.discord.gg", "session_start_limit": {
                        "total": 1000, "remaining": 999, "reset_after": 3600000,
                        "max_concurrency": 1}}
                if self.block_routine and path == f"/channels/{THREAD}":
                    self.calls.append((path, query))
                    rest_entered.set()
                    rest_release.wait(timeout=8)
                return super().get(path, query)

        class ScriptedSocket:
            def __init__(self):
                self.packets = queue.Queue()
                self.packets.put({"op": 10, "d": {"heartbeat_interval": 1000}})
                self.packets.put({"op": 0, "s": 1, "t": "READY", "d": {
                    "session_id": "synthetic-session", "resume_gateway_url": "wss://gateway.discord.gg"}})
                self.aborted = threading.Event()

            def receive_packet(self, timeout):
                try:
                    packet = self.packets.get(timeout=timeout)
                except queue.Empty:
                    return None
                return packet

            def send_packet(self, packet):
                if packet.get("op") == 1:
                    heartbeat_sent.set()
                    self.packets.put({"op": 11})

            def push(self, packet):
                self.packets.put(packet)

            def abort(self):
                self.aborted.set()
                self.packets.put(None)

        api = BlockingAPI()
        self.app.api = api
        self.api = api
        socket = ScriptedSocket()
        self.cmux.on_submit = group_submitted.set
        original_rest = self.app._rest_recovery_turn
        original_commit = self.app.commit_gateway_dispatch

        def tracked_rest(*args, **kwargs):
            result = original_rest(*args, **kwargs)
            session = self.app.gateway_session()
            if (ready_committed.is_set() and result.get("recovery_state") == "complete" and
                    result.get("recovery_epoch") == session["recovery_epoch"]):
                recovery_complete.set()
            rest_turn_completed.set()
            return result

        def tracked_commit(*args, **kwargs):
            result = original_commit(*args, **kwargs)
            event_name = args[1] if len(args) > 1 else kwargs.get("event_name")
            if event_name == "READY" and result.get("committed"):
                ready_committed.set()
            if event_name == "MESSAGE_CREATE" and result.get("ingested"):
                message_ingested.set()
            return result

        worker = None
        worker_started = False
        with mock.patch.object(self.app, "_rest_recovery_turn", side_effect=tracked_rest), \
                mock.patch.object(self.app, "commit_gateway_dispatch", side_effect=tracked_commit):
            worker = threading.Thread(
                target=self.app.serve,
                kwargs={"send": True,
                        "gateway_factory": lambda watch: GatewayClient(
                            watch, connector=lambda url: socket, clock=lambda: self.clock[0],
                            random_value=lambda: 0.5),
                        "stop_event": stop_event})
            try:
                worker.start()
                worker_started = True
                self.assertTrue(ready_committed.wait(timeout=4))
                self.assertTrue(rest_turn_completed.wait(timeout=4))
                if not recovery_complete.is_set():
                    # READY can queue recovery behind the first poll's scheduled interval.
                    with contextlib.closing(self.app.database(create=False)) as db:
                        next_poll_at = db.execute(
                            "SELECT next_poll_at FROM threads WHERE thread_id=?", (THREAD,)).fetchone()[0]
                    self.clock[0] = max(self.clock[0], next_poll_at)
                self.assertTrue(recovery_complete.wait(timeout=4))
                api.block_routine = True
                self.clock[0] += dw.ROUTINE_REST_INTERVAL
                self.assertTrue(rest_entered.wait(timeout=4))

                heartbeat_sent.clear()
                socket.push({"op": 1})
                self.assertTrue(heartbeat_sent.wait(timeout=2))
                reply = message("101", content="recovered while REST is blocked",
                                timestamp=timestamp_at(self.clock[0]))
                reply["channel_id"] = THREAD
                socket.push({"op": 0, "s": 2, "t": "MESSAGE_CREATE", "d": reply})
                self.assertTrue(message_ingested.wait(timeout=3))
                self.clock[0] += dw.GROUP_QUIET_SECONDS
                self.assertTrue(group_submitted.wait(timeout=4))
                self.assertFalse(rest_release.is_set())
                self.assertEqual(self.cmux.submit_count, 1)
            finally:
                stop_event.set()
                rest_release.set()
                socket.abort()
                if worker_started:
                    worker.join(timeout=dw.LANE_JOIN_TIMEOUT + 2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(self.cmux.submit_count, 1)

    def test_rest_channel_mismatch_rolls_back_message_and_cursor_page(self):
        self.register()
        mismatched = message("101")
        mismatched["channel_id"] = "200"
        self.api.rows = [mismatched]
        report = self.app.scan()
        self.assertEqual(report["threads"][0]["status"], "error")
        self.assertEqual(self.thread_cursor(), THREAD)
        self.assertEqual(self.messages(), [])
        self.assertIsNone(self.thread_lifecycle()[2])

        self.clock[0] += dw.POLL_INTERVAL + 1
        matching = message("101")
        matching["channel_id"] = THREAD
        self.api.rows = [matching]
        report = self.app.scan()
        self.assertEqual(report["threads"][0]["status"], "polled")
        self.assertEqual(self.thread_cursor(), "101")
        self.assertEqual([row[0] for row in self.messages()], ["101"])

    def test_gateway_cutoff_ignores_handled_replay_but_accepts_later_out_of_order_event(self):
        self.register(after="150")
        with contextlib.closing(self.app.database(create=False)) as db:
            with db:
                db.execute("UPDATE threads SET cursor='200' WHERE thread_id=?", (THREAD,))
        self.clock[0] = TEST_NOW + dw.DORMANT_AFTER + 1
        handled_replay = message("149", timestamp=timestamp_at(self.clock[0]))
        ignored = self.app.ingest_gateway_message(THREAD, handled_replay)
        self.assertFalse(ignored["ingested"])
        self.assertEqual(self.thread_lifecycle()[1:3], ("dormant", None))
        self.assertEqual(self.messages(), [])

        fresh_at = self.clock[0]
        later_out_of_order = message("175", timestamp=timestamp_at(fresh_at))
        accepted = self.app.ingest_gateway_message(THREAD, later_out_of_order)
        self.assertTrue(accepted["ingested"])
        self.assertEqual(self.thread_lifecycle()[1:3], ("active", fresh_at))
        self.assertEqual(self.thread_lifecycle()[4], "200")
        self.assertEqual(self.thread_lifecycle()[7], "150")
        self.assertEqual([row[0] for row in self.messages()], ["175"])

    def test_new_gateway_reply_reactivates_dormancy_and_starts_a_fresh_window_again(self):
        self.register()
        self.clock[0] = TEST_NOW + dw.DORMANT_AFTER + 1
        self.app.scan()
        self.assertEqual(self.thread_lifecycle()[1], "dormant")

        first_at = self.clock[0]
        first = message("101", timestamp=timestamp_at(first_at))
        self.assertTrue(self.app.ingest_gateway_message(THREAD, first)["ingested"])
        self.assertEqual(self.thread_lifecycle()[1:3], ("active", first_at))
        self.clock[0] = first_at + dw.DORMANT_AFTER
        self.app.scan()
        self.assertEqual(self.thread_lifecycle()[1], "active")
        self.clock[0] += 1
        self.app.scan()
        self.assertEqual(self.thread_lifecycle()[1], "dormant")

        second_at = self.clock[0]
        second = message("102", timestamp=timestamp_at(second_at))
        self.assertTrue(self.app.ingest_gateway_message(THREAD, second)["ingested"])
        self.assertEqual(self.thread_lifecycle()[1:3], ("active", second_at))
        self.clock[0] = second_at + dw.DORMANT_AFTER
        self.app.scan()
        self.assertEqual(self.thread_lifecycle()[1], "active")
        self.clock[0] += 1
        self.app.scan()
        self.assertEqual(self.thread_lifecycle()[1], "dormant")

    def test_dormant_pending_reply_skips_rest_and_still_uses_exact_owner_guard(self):
        self.register()
        late = message("101", timestamp=timestamp_at(TEST_NOW))
        self.clock[0] = TEST_NOW + dw.DORMANT_AFTER + 1
        self.assertTrue(self.app.ingest_gateway_message(THREAD, late)["ingested"])
        self.assertEqual(self.thread_lifecycle()[1], "dormant")
        self.api.calls.clear()
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        report = self.app.scan(send=True)
        self.assertEqual(report["threads"][0]["status"], "dormant")
        self.assertEqual(report["threads"][0]["wake"]["state"], "awaiting_receipt")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.cmux.submit_count, 1)

        self.app.ingest_gateway_message(
            THREAD, message("102", timestamp=timestamp_at(self.clock[0] + 1)))
        self.app.receive(THREAD, "101", io.StringIO())
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.cmux.fail_binding = "stale exact owner projection"
        held = self.app.scan(send=True)
        self.assertEqual(held["threads"][0]["wake"]["state"], "held")
        self.assertEqual(self.cmux.submit_count, 1)
        self.assertEqual(self.messages()[-1][1], "pending")
        self.cmux.fail_binding = None
        self.targets.clear()
        self._write_hailing()
        missing = self.app.scan(send=True)
        self.assertEqual(missing["threads"][0]["wake"]["state"], "held")
        self.assertEqual(self.cmux.submit_count, 1)
        self.assertEqual(self.messages()[-1][1], "pending")

    def test_register_defaults_to_creation_and_ignores_legacy_cursor_without_proof(self):
        cursor_dir = self.notify_home / "state"
        cursor_dir.mkdir(mode=0o700)
        (cursor_dir / THREAD).write_text("150\n", encoding="ascii")
        (cursor_dir / THREAD).chmod(0o600)
        result = self.register()
        self.assertEqual(result["cursor"], THREAD)
        self.assertEqual(result["cursor_source"], "thread-created")
        self.assertEqual(self.thread_lifecycle()[7], THREAD)
        self.targets[0]["label"] = "renamed harmlessly"
        self.targets[0]["cutoff"] = 9999
        self._write_hailing()
        self.assertTrue(self.app.managed(THREAD)["managed"])

    def test_register_accepts_only_an_explicit_valid_starting_cursor(self):
        result = self.register(after="150")
        self.assertEqual(result["cursor"], "150")
        self.assertEqual(result["cursor_source"], "explicit")
        self.assertEqual(self.thread_lifecycle()[7], "150")
        self.api.remote["id"] = "200"
        with self.assertRaisesRegex(ValueError, "cannot precede"):
            self.app.register("200", after="199")

    def test_receive_keeps_output_and_session_hint_slots_before_keyword_only_group(self):
        parameters = inspect.signature(dw.Watch.receive).parameters
        self.assertEqual(parameters["output"].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        self.assertEqual(parameters["session_hint"].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        self.assertEqual(parameters["group_id"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_register_reuses_closed_same_owner_without_resetting_cursor_or_generation(self):
        self.register(after="150")
        self.app.close(THREAD)
        self.app.close(THREAD)
        with contextlib.closing(self.app.database(create=False)) as db:
            with db:
                db.execute("UPDATE threads SET cursor='200',catchup_before='210',catchup_max='220' "
                           "WHERE thread_id=?", (THREAD,))
        self.api.remote["thread_metadata"]["archived"] = True
        result = self.app.register(THREAD, after="150")
        self.assertTrue(result["already_registered"])
        self.assertEqual(result["cursor"], "200")
        self.assertEqual(self.thread_lifecycle()[:2], (0, "active"))
        self.assertEqual(self.thread_lifecycle()[4:], ("200", "210", "220", "150"))
        with contextlib.closing(self.app.database(create=False)) as db:
            self.assertEqual(db.execute("SELECT owner_generation FROM threads WHERE thread_id=?",
                                         (THREAD,)).fetchone()[0], 1)
        managed = self.app.managed(THREAD)
        self.assertTrue(managed["managed"])
        self.assertFalse(managed["open"])
        self.assertTrue(managed["remote_closed"])

    def test_register_accepts_current_cursor_without_changing_cutoff_or_queued_reply(self):
        self.register(after="150")
        self.api.rows = [message("151")]
        self.app.scan()
        activity_before = self.thread_lifecycle()[2]
        self.assertEqual(self.thread_lifecycle()[4], "151")
        self.assertEqual(self.thread_lifecycle()[7], "150")
        self.assertEqual(self.messages()[0][1], "pending")
        self.clock[0] += 60

        with self.assertRaisesRegex(ValueError, "different initial cutoff or current cursor"):
            self.app.register(THREAD, after="152")
        result = self.app.register(THREAD, after="151")
        self.assertTrue(result["already_registered"])
        self.assertEqual(result["cursor"], "151")
        self.assertEqual(self.thread_lifecycle()[2], activity_before)
        self.assertEqual(self.thread_lifecycle()[4], "151")
        self.assertEqual(self.thread_lifecycle()[7], "150")
        self.assertEqual(self.messages()[0][0:2], ("151", "pending"))

    def test_group_quiet_uses_unique_messages_and_ignores_duplicate_or_other_author_events(self):
        self.register()
        first_at = self.clock[0]
        self.gateway("102", "later snowflake")
        self.clock[0] = first_at + 1
        self.gateway("101", "earlier snowflake")
        self.clock[0] = first_at + 2
        duplicate = message("102", content="changed duplicate", timestamp=timestamp_at(first_at + 30))
        self.assertFalse(self.app.ingest_gateway_message(THREAD, duplicate)["ingested"])
        self.api.rows = [duplicate]
        self.app.scan()
        self.clock[0] = first_at + 3
        self.assertFalse(self.gateway("103", "unrelated author", author_id="999")["ingested"])
        self.assertEqual(self.wake()["state"], "forming")
        self.assertEqual(self.app.status()["threads"][0]["forming_group"]["state"], "forming")
        self.assertEqual(self.cmux.submit_count, 0)
        self.clock[0] = first_at + 4
        result = self.wake()
        self.assertTrue(result["submitted"])
        self.assertEqual(self.group_members(1), [("101",), ("102",)])
        self.assertEqual(self.cmux.submit_count, 1)
        delivery_status = self.app.status()["threads"][0]["delivery_groups"][0]
        self.assertEqual(delivery_status["state"], "awaiting_receipt")
        self.assertEqual([member["state"] for member in delivery_status["members"]],
                         ["awaiting_receipt", "awaiting_receipt"])
        output = io.StringIO()
        self.app.receive(THREAD, output=output, group_id=1)
        self.assertIn("earlier snowflake", output.getvalue())
        self.assertIn("later snowflake", output.getvalue())
        self.assertLess(output.getvalue().index(":101]"), output.getvalue().index(":102]"))
        self.assertEqual(self.app.status()["threads"][0]["delivery_groups"][0]["state"], "received")
        self.assertEqual(self.thread_lifecycle()[2], first_at + 1)

    def test_continuous_burst_becomes_eligible_at_first_message_maximum_delay(self):
        self.register()
        first_at = self.clock[0]
        for offset in range(0, 9, 2):
            self.clock[0] = first_at + offset
            self.gateway(str(101 + offset), f"reply at {offset}")
        self.clock[0] = first_at + dw.GROUP_MAX_DELAY - 1
        self.assertEqual(self.wake()["state"], "forming")
        self.clock[0] = first_at + dw.GROUP_MAX_DELAY
        result = self.wake()
        self.assertTrue(result["submitted"])
        self.assertEqual(self.group_members(1), [("101",), ("103",), ("105",), ("107",), ("109",)])

    def test_count_overflow_creates_ordered_subsequent_groups_without_loss(self):
        self.register()
        with mock.patch.object(dw, "MAX_RECEIVE_MESSAGES", 2):
            for message_id in ("105", "101", "104", "102", "103"):
                self.gateway(message_id, f"full body {message_id}")
            self.clock[0] += dw.GROUP_QUIET_SECONDS
            result = self.wake()
            self.assertTrue(result["submitted"])
            self.assertEqual(self.group_members(1), [("101",), ("102",)])
            self.assertEqual(self.group_members(2), [("103",), ("104",)])
            self.assertEqual(self.group_members(3), [("105",)])
            self.assertEqual(self.cmux.submit_count, 1)

    def test_rendered_utf8_attachment_overflow_splits_without_truncating_a_single_reply(self):
        self.register()
        filename = "界" * 170
        url = "https://example.invalid/" + "u" * 2024
        attachments = [{"filename": filename, "url": url}]
        with mock.patch.object(dw, "MAX_RECEIVE_BYTES", 5000):
            self.gateway("101", "🙂" * 100, attachments)
            self.gateway("102", "🙂" * 100, attachments)
            self.clock[0] += dw.GROUP_QUIET_SECONDS
            result = self.wake()
            self.assertTrue(result["submitted"])
            self.assertEqual(self.group_members(1), [("101",)])
            self.assertEqual(self.group_members(2), [("102",)])
            with contextlib.closing(self.app.database(create=False)) as db:
                rows = db.execute("SELECT message_id,author_name,body,attachments_json FROM messages "
                                  "WHERE thread_id=? ORDER BY message_id", (THREAD,)).fetchall()
            for row in rows:
                rendered = dw.Watch._render_messages(THREAD, [row])
                self.assertLessEqual(len(rendered.encode("utf-8")), dw.MAX_RECEIVE_BYTES)
                self.assertIn(filename, rendered)
                self.assertIn(url, rendered)

    def test_largest_accepted_single_reply_fits_the_shared_receive_output_bound(self):
        self.register()
        filename = "f" * 512
        url = "https://example.invalid/" + "u" * (2048 - len("https://example.invalid/"))
        attachments = [{"filename": filename, "url": url} for _ in range(6)]
        body = "b" * dw.MAX_MESSAGE_BYTES
        self.gateway("101", body, attachments)
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        result = self.wake()
        self.assertTrue(result["submitted"])
        output = io.StringIO()
        received = self.app.receive(THREAD, output=output, group_id=1)
        self.assertEqual(received["received"], ["101"])
        self.assertLessEqual(len(output.getvalue().encode("utf-8")), dw.MAX_RECEIVE_BYTES)
        self.assertIn(body, output.getvalue())
        self.assertEqual(output.getvalue().count("[attachment] " + filename), 6)

    def test_default_receive_freezes_and_reads_unsealed_intake_without_a_sender(self):
        self.register()
        self.api.rows = [message("101", content="observe-only durable reply")]
        report = self.app.scan()
        self.assertEqual(report["threads"][0]["status"], "polled")
        self.assertEqual(self.messages()[0][1], "pending")
        self.assertEqual(self.cmux.submit_count, 0)
        with contextlib.closing(self.app.database(create=False)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM delivery_groups WHERE thread_id=?",
                                        (THREAD,)).fetchone()[0], 0)

        output = io.StringIO()
        result = self.app.receive(THREAD, output=output)
        self.assertEqual(result["received"], ["101"])
        self.assertIn("observe-only durable reply", output.getvalue())
        self.assertEqual(self.cmux.submit_count, 0)
        self.assertEqual(self.messages()[0][1], "received")
        self.assertEqual(self.group_members(1), [("101",)])

    def test_default_receive_output_failure_keeps_its_new_frozen_group_unread(self):
        self.register()
        self.api.rows = [message("101", content="retryable manual output")]
        self.app.scan()

        class ShortWriter:
            def write(self, text):
                return len(text) - 1

            def flush(self):
                return None

        with self.assertRaisesRegex(ValueError, "output was incomplete"):
            self.app.receive(THREAD, output=ShortWriter())
        self.assertEqual(self.messages()[0][1], "pending")
        self.assertEqual(self.group_members(1), [("101",)])
        self.assertEqual(self.delivery_groups()[0][2], "eligible")
        output = io.StringIO()
        result = self.app.receive(THREAD, output=output)
        self.assertEqual(result["received"], ["101"])
        self.assertIn("retryable manual output", output.getvalue())

    def test_default_receive_finishes_older_frozen_group_before_unsealed_replies(self):
        self.register()
        self.gateway("101", "older frozen group")
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.wake()
        self.gateway("102", "later unsealed reply")
        output = io.StringIO()
        result = self.app.receive(THREAD, output=output)
        self.assertEqual(result["received"], ["101"])
        self.assertIn("older frozen group", output.getvalue())
        self.assertNotIn("later unsealed reply", output.getvalue())
        self.assertEqual(self.messages()[1][1], "pending")
        self.assertEqual(self.group_members(1), [("101",)])
        with contextlib.closing(self.app.database(create=False)) as db:
            self.assertIsNone(db.execute("SELECT group_id FROM messages WHERE thread_id=? AND message_id='102'",
                                         (THREAD,)).fetchone()[0])

    def test_incomplete_rest_catchup_holds_group_formation_and_submission(self):
        self.register()
        with contextlib.closing(self.app.database(create=False)) as db:
            with db:
                db.execute("UPDATE threads SET catchup_before='180',catchup_max='200' WHERE thread_id=?",
                           (THREAD,))
        self.gateway("101", "reply while catch-up is open")
        self.clock[0] += dw.GROUP_MAX_DELAY + 1
        result = self.wake()
        self.assertEqual(result["state"], "held")
        self.assertEqual(result["held"], "rest_catchup_incomplete")
        self.assertEqual(self.thread_cursor(), THREAD)
        self.assertEqual(self.delivery_groups(), [])
        self.assertEqual(self.messages()[0][1], "pending")
        output = io.StringIO()
        self.assertEqual(self.app.receive(THREAD, output=output)["received"], [])
        self.assertEqual(output.getvalue(), "")
        with self.assertRaisesRegex(ValueError, "REST catch-up is incomplete"):
            self.app.receive(THREAD, "101", io.StringIO())
        with contextlib.closing(self.app.database(create=False)) as db:
            self.assertIsNone(db.execute("SELECT group_id FROM messages WHERE thread_id=?", (THREAD,)).fetchone()[0])
            with db:
                db.execute("UPDATE threads SET catchup_before=NULL,catchup_max=NULL WHERE thread_id=?",
                           (THREAD,))
        result = self.wake()
        self.assertTrue(result["submitted"])
        self.assertEqual(self.group_members(1), [("101",)])

    def test_legacy_registration_written_after_cutoff_migration_is_initialized(self):
        self.register()
        owner_json = json.dumps(self.app.binding_snapshot(self.targets[0]), separators=(",", ":"))
        remote_json = json.dumps({"id": "200", "type": 11,
                                  "thread_metadata": {"archived": False}, "applied_tags": []})
        with contextlib.closing(self.app.database(create=False)) as db:
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM meta WHERE key='initial_intake_cutoff_migration_v1'").fetchone())
            with db:
                db.execute("INSERT INTO threads(thread_id,owner_json,cursor,is_open,registered_at,updated_at,"
                           "remote_json) VALUES(?,?,?,1,?,?,?)",
                           ("200", owner_json, "250", TEST_NOW, TEST_NOW, remote_json))
            self.assertIsNone(db.execute(
                "SELECT initial_cutoff FROM threads WHERE thread_id='200'").fetchone()[0])

        handled = message("249")
        handled["channel_id"] = "200"
        fresh = message("251")
        fresh["channel_id"] = "200"
        self.assertFalse(self.app.ingest_gateway_message("200", handled)["ingested"])
        self.assertTrue(self.app.ingest_gateway_message("200", fresh)["ingested"])
        with contextlib.closing(self.app.database(create=False)) as db:
            self.assertEqual(db.execute("SELECT cursor,initial_cutoff FROM threads WHERE thread_id='200'")
                             .fetchone(), ("250", "250"))
            self.assertEqual([row[0] for row in db.execute(
                "SELECT message_id FROM messages WHERE thread_id='200' ORDER BY message_id")], ["251"])

    def test_caller_without_session_env_uses_unique_surface_enrollment_and_binding(self):
        os.environ.pop("CODEX_THREAD_ID", None)
        self.assertEqual(self.register()["owner_session"], SESSION_A)

    def test_default_watch_binding_sends_and_receives_with_unknown_terminal_input(self):
        native_root = self.root / "native-binding"
        state = native_root / "state"
        hailing = native_root / "hailing"
        notify_home = native_root / "agent-notify"
        home = native_root / "home"
        hook_directory = home / ".cmuxterm"
        for directory in (hailing, notify_home, hook_directory):
            directory.mkdir(mode=0o700, parents=True)
        owner_config = {"version": 1, "targets": [target()], "coordinator": None,
                        "remind_after": 300, "escalate_after": 900,
                        "cooldown": 600, "interval": 30}
        config_path = hailing / "config.json"
        config_path.write_text(json.dumps(owner_config), encoding="utf-8")
        config_path.chmod(0o600)
        hook_record = {"surfaceId": SURFACE_A, "workspaceId": WORKSPACE_A,
                       "agentLifecycle": "idle", "startedAt": 1234.5}
        hook_path = hook_directory / "codex-hook-sessions.json"
        hook_path.write_text(json.dumps({"sessions": {SESSION_A: hook_record}}), encoding="utf-8")
        hook_path.chmod(0o600)
        current = {"truncated": False, "owner_availability": {"agent_sessions": "available"},
                   "items": [{"placement": {"kind": "local"},
                              "projections": [{"panel_id": SURFACE_A, "workspace_id": WORKSPACE_A}],
                              "agents": [{"session_id": SESSION_A, "kind": "codex", "state": "idle"}]}]}
        input_state = {"surface_id": SURFACE_A, "workspace_id": WORKSPACE_A, "state": "empty",
                       "agent": True, "terminal": True, "blocks_typing": False,
                       "waiting_on_human": False, "lifecycle": "idle"}
        settings = {"messages_enabled": True, "receiving": True, "workspace_receiving": True}
        cmux_calls = []
        paste_calls = []

        def run_cmux(args, **_kwargs):
            cmux_calls.append(args)
            if args[2] == "current":
                response = current
            elif args[2] == "rpc" and args[3] == "surface.input_state":
                response = input_state
            elif args[2] == "rpc" and args[3] == "agent.message.settings":
                response = settings
            elif args[2] == "paste":
                paste_calls.append(args[-1])
                response = {"submitted": True}
            else:
                raise AssertionError(f"Unexpected CMUX command: {args}")
            return subprocess.CompletedProcess(args, 0, json.dumps(response), "")

        api = FakeAPI([message("101", content="reply survives unknown terminal input")])
        now = [TEST_NOW]

        class NativeWatch(dw.Watch):
            def __init__(self):
                self.values = {"token": "synthetic-token", "user_id": USER_ID, "closed_tag": "900"}
                self.api = api
                super().__init__(state_dir=state, hailing_state=hailing, notify_home=notify_home,
                                 cmux="synthetic-cmux", clock=lambda: now[0])

            def read_credentials(self):
                self._configured_user_id = self.values["user_id"]
                return self.values, self.api

        environment = {"CMUX_AGENT_LAUNCH_KIND": "codex", "CMUX_SURFACE_ID": SURFACE_A,
                       "CMUX_WORKSPACE_ID": WORKSPACE_A, "CODEX_THREAD_ID": SESSION_A}
        with mock.patch.object(hf.Path, "home", return_value=home), \
                mock.patch.object(hf.subprocess, "run", side_effect=run_cmux), \
                mock.patch.dict(os.environ, environment, clear=False):
            watch = NativeWatch()
            self.assertIs(type(watch.cmux_factory()), hf.Cmux)
            registration = watch.register(THREAD)
            self.assertEqual(registration["owner_session"], SESSION_A)
            owner = watch.current_owner(verify=False)
            watch.scan()

            for composer_state in ("empty", "draft", "unknown"):
                input_state.update(state=composer_state, lifecycle="unknown")
                generic_cmux = watch.cmux_factory()
                with self.assertRaisesRegex(hf.DeliveryHoldError, "Human draft, approval"):
                    generic_cmux.submit(owner, "ordinary Hailing prompt")
            self.assertEqual(paste_calls, [])
            input_reads_before_reply = sum(
                len(args) > 3 and args[2] == "rpc" and args[3] == "surface.input_state"
                for args in cmux_calls)

            now[0] += dw.GROUP_QUIET_SECONDS + dw.ROUTINE_REST_INTERVAL
            result = watch.scan(send=True)
            self.assertEqual(result["threads"][0]["wake"]["state"], "awaiting_receipt")
            self.assertEqual(len(paste_calls), 1)
            self.assertIn("receive --thread 100 --group 1", paste_calls[0])
            self.assertNotIn("reply survives unknown terminal input", paste_calls[0])

            for composer_state in ("empty", "draft", "unknown"):
                input_state.update(state=composer_state, lifecycle="unknown")
                watch.validate_registered_owner(owner, readiness_policy=hf.DISCORD_REPLY_READINESS)

            projection = current["items"][0]["projections"][0]
            agent = current["items"][0]["agents"][0]
            projection["workspace_id"] = WORKSPACE_B
            with self.assertRaisesRegex(ValueError, "Workspace changed"):
                watch.validate_registered_owner(owner, readiness_policy=hf.DISCORD_REPLY_READINESS)
            projection["workspace_id"] = WORKSPACE_A
            projection["panel_id"] = SURFACE_B
            with self.assertRaisesRegex(ValueError, "Surface is missing or ambiguous"):
                watch.validate_registered_owner(owner, readiness_policy=hf.DISCORD_REPLY_READINESS)
            projection["panel_id"] = SURFACE_A
            agent["session_id"] = SESSION_B
            with self.assertRaisesRegex(ValueError, "Session is absent"):
                watch.validate_registered_owner(owner, readiness_policy=hf.DISCORD_REPLY_READINESS)
            agent["session_id"] = SESSION_A
            hook_record["startedAt"] = 1235.5
            hook_path.write_text(json.dumps({"sessions": {SESSION_A: hook_record}}), encoding="utf-8")
            hook_path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "Session generation changed"):
                watch.validate_registered_owner(owner, readiness_policy=hf.DISCORD_REPLY_READINESS)
            hook_record["startedAt"] = 1234.5
            hook_path.write_text(json.dumps({"sessions": {SESSION_A: hook_record}}), encoding="utf-8")
            hook_path.chmod(0o600)
            for setting in ("messages_enabled", "receiving", "workspace_receiving"):
                settings[setting] = False
                with self.assertRaisesRegex(ValueError, "Native messaging is disabled"):
                    watch.validate_registered_owner(owner, readiness_policy=hf.DISCORD_REPLY_READINESS)
                settings[setting] = True

            output = io.StringIO()
            receipt = watch.receive(THREAD, output=output)
            self.assertEqual(receipt["received"], ["101"])
            self.assertIn("reply survives unknown terminal input", output.getvalue())
            with contextlib.closing(watch.database(create=False)) as db:
                self.assertEqual(db.execute("SELECT state FROM messages WHERE thread_id=? AND message_id=?",
                                            (THREAD, "101")).fetchone()[0], "received")
            input_reads_after_receive = sum(
                len(args) > 3 and args[2] == "rpc" and args[3] == "surface.input_state"
                for args in cmux_calls)
            self.assertEqual(input_reads_after_receive, input_reads_before_reply)

    def test_opencode_session_self_registers_exact_owner_without_state_migration(self):
        opencode_session = "ses_DiscordWatchFixture"
        api_url = "http://127.0.0.1:4096"
        password_file = str(self.root / "opencode-password")
        self.targets = [target(session=opencode_session, harness="opencode",
                               api_url=api_url, password_file=password_file,
                               expected_server_pid=12345,
                               session_directory=str(self.root), worktree_directory=str(self.root),
                               parent_task_id="task:discord-watch-fixture")]
        self._write_hailing()
        os.environ["CMUX_AGENT_LAUNCH_KIND"] = "opencode"
        os.environ["OPENCODE_SESSION_ID"] = opencode_session
        owner = self.app.current_owner()
        self.assertEqual(owner["session"], opencode_session)
        self.assertEqual(owner["harness"], "opencode")
        self.assertEqual(owner["expected_server_pid"], 12345)
        self.assertEqual(owner["delivery_policy"], "native_empty_composer")
        self.assertEqual(self.register()["owner_session"], opencode_session)
        managed = self.app.managed(THREAD)
        self.assertTrue(managed["managed"])
        with contextlib.closing(self.app.database(create=False)) as db:
            row = db.execute("SELECT owner_json FROM threads WHERE thread_id=?", (THREAD,)).fetchone()
            tables = {item[0] for item in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        registered_owner = json.loads(row[0])
        self.assertEqual(registered_owner["session"], opencode_session)
        self.assertEqual(registered_owner["api_url"], api_url)
        self.assertIn("threads", tables)
        self.assertIn("messages", tables)
        self.assertNotIn("opencode_threads", tables)
        prompt = self.app._wake_prompt(THREAD, "501", owner)
        self.assertIn("receive --thread 100 --session ses_DiscordWatchFixture --message 501", prompt)

    def test_opencode_group_wake_targets_exact_session_for_group_receipt(self):
        session = "ses_DiscordWatchGroupReceipt"
        prompt = self.app._group_wake_prompt(
            THREAD, 17, {"harness": "opencode", "session": session})
        self.assertIn(f"receive --thread {THREAD} --session {session} --group 17", prompt)
        self.assertNotIn("private reply body", prompt)
        args = dw.parser().parse_args([
            "receive", "--thread", THREAD, "--session", session, "--group", "17"])
        self.assertEqual((args.session, args.group), (session, "17"))

    def test_opencode_current_owner_rejects_wrong_session_and_missing_workspace(self):
        opencode_session = "ses_DiscordWatchFixture"
        self.targets = [target(session=opencode_session, harness="opencode",
                               api_url="http://127.0.0.1:4096", password_file=str(self.root / "secret"),
                               expected_server_pid=12345, session_directory=str(self.root),
                               worktree_directory=str(self.root), parent_task_id="task:fixture")]
        self._write_hailing()
        os.environ["CMUX_AGENT_LAUNCH_KIND"] = "opencode"
        os.environ["OPENCODE_SESSION_ID"] = "ses_OtherSession"
        with self.assertRaisesRegex(ValueError, "exact enrolled"):
            self.app.current_owner()
        os.environ["OPENCODE_SESSION_ID"] = opencode_session
        os.environ.pop("CMUX_WORKSPACE_ID", None)
        with self.assertRaisesRegex(ValueError, "CMUX_WORKSPACE_ID"):
            self.app.current_owner()
        os.environ["CMUX_WORKSPACE_ID"] = WORKSPACE_A
        os.environ.pop("OPENCODE_SESSION_ID", None)
        with self.assertRaisesRegex(ValueError, "OPENCODE_SESSION_ID"):
            self.app.current_owner(session_hint=opencode_session)

    def test_binding_snapshot_normalizes_policy_only_for_opencode(self):
        opencode_fields = {"api_url": "http://127.0.0.1:4096",
                           "password_file": str(self.root / "password"),
                           "expected_server_pid": 12345,
                           "session_directory": str(self.root),
                           "worktree_directory": str(self.root),
                           "parent_task_id": "task:fixture"}
        defaulted = self.app.binding_snapshot(target(session="ses_PolicyFixture", harness="opencode",
                                                       **opencode_fields))
        self.assertEqual(defaulted["delivery_policy"], "native_empty_composer")
        explicit = self.app.binding_snapshot(target(session="ses_PolicyFixture", harness="opencode",
                                                      delivery_policy="api_sidechannel", **opencode_fields))
        self.assertEqual(explicit["delivery_policy"], "api_sidechannel")
        with self.assertRaisesRegex(ValueError, "delivery policy"):
            self.app.binding_snapshot(target(session="ses_PolicyFixture", harness="opencode",
                                               delivery_policy="future_policy", **opencode_fields))

        for harness in ("claude", "codex"):
            snapshot = self.app.binding_snapshot(target(harness=harness, delivery_policy="api_sidechannel"))
            self.assertNotIn("delivery_policy", snapshot)

    def _prepare_busy_opencode(self):
        session = "ses_DiscordWatchReceipt"
        worktree = self.root / "opencode-worktree"
        worktree.mkdir()
        password_file = self.root / "opencode-password"
        password_file.write_text("synthetic-opencode-password", encoding="utf-8")
        password_file.chmod(0o600)
        server = FakeOpenCodeAPI(session, worktree, "synthetic-opencode-password")
        self.addCleanup(server.close)
        self.targets = [target(session=session, harness="opencode", api_url=server.url,
                               password_file=str(password_file), expected_server_pid=os.getpid(),
                               session_directory=str(worktree), worktree_directory=str(worktree),
                               parent_task_id="task:discord-watch-fixture")]
        self._write_hailing()

        hook_directory = self.root / "cmux-hooks"
        hook_directory.mkdir(mode=0o700)
        hook_record = {"surfaceId": SURFACE_A, "workspaceId": WORKSPACE_A,
                       "agentLifecycle": "idle", "startedAt": 1234.5,
                       "cwd": str(worktree)}
        hook_path = hook_directory / "opencode-hook-sessions.json"

        def write_hook():
            hook_path.write_text(json.dumps({"sessions": {session: hook_record}}), encoding="utf-8")
            hook_path.chmod(0o600)

        write_hook()
        current = {"truncated": False, "owner_availability": {"agent_sessions": "available"},
                   "items": [{"placement": {"kind": "local"},
                              "projections": [{"panel_id": SURFACE_A, "workspace_id": WORKSPACE_A}],
                              "agents": [{"session_id": session, "kind": "opencode", "state": "idle"}]}]}
        guard = {"surface_id": SURFACE_A, "workspace_id": WORKSPACE_A, "state": "empty",
                 "agent": True, "terminal": True, "blocks_typing": False,
                 "waiting_on_human": False, "lifecycle": "idle"}
        settings = {"messages_enabled": True, "receiving": True, "workspace_receiving": True}
        cmux = hf.Cmux(hook_directory=hook_directory)

        def native_command(*args):
            if args[0] == "current":
                return current
            if args[0] == "rpc" and args[1] == "surface.input_state":
                return guard
            if args[0] == "rpc" and args[1] == "agent.message.settings":
                return settings
            raise AssertionError(f"Unexpected CMUX command: {args}")

        cmux.command = native_command
        self.app.fake_cmux = cmux
        os.environ["CMUX_AGENT_LAUNCH_KIND"] = "opencode"
        os.environ["OPENCODE_SESSION_ID"] = session
        return session, server, hook_record, write_hook, current, settings

    def test_busy_opencode_can_receive_with_identity_binding_while_send_stays_held(self):
        session, server, hook_record, write_hook, current, _settings = self._prepare_busy_opencode()
        self.app.register(THREAD, session_hint=session)
        self.api.rows = [message("101", content="busy-session reply")]
        self.app.scan()
        self.assertEqual(self.messages()[0][1], "pending")

        server.active[session] = {"type": "running"}
        current["items"][0]["agents"][0]["state"] = "working"
        hook_record["agentLifecycle"] = "running"
        write_hook()
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        report = self.app.scan(send=True)
        self.assertEqual(report["threads"][0]["wake"]["state"], "held")
        self.assertIn("session_busy", report["threads"][0]["wake"]["held"])
        self.assertEqual(self.messages()[0][1:3], ("pending", 0))

        with self.assertRaisesRegex(ValueError, "exact enrolled"):
            self.app.receive(THREAD, "101", io.StringIO(), session_hint="ses_OtherDiscordSession")
        self.targets[0]["started_at"] += 1
        self._write_hailing()
        with self.assertRaisesRegex(ValueError, "generation changed"):
            self.app.receive(THREAD, "101", io.StringIO(), session_hint=session)
        self.assertEqual(self.messages()[0][1], "pending")

        self.targets[0]["started_at"] = 1234.5
        self._write_hailing()
        output = io.StringIO()
        result = self.app.receive(THREAD, "101", output, session_hint=session)
        self.assertEqual(result["received"], ["101"])
        self.assertIn("[discord:100:101] Configured user: busy-session reply", output.getvalue())
        self.assertEqual(self.messages()[0][1], "received")

    def test_busy_opencode_can_receive_frozen_group_with_identity_binding(self):
        session, server, hook_record, write_hook, current, _settings = self._prepare_busy_opencode()
        self.app.register(THREAD, session_hint=session)
        self.api.rows = [message("101", content="busy-session group reply")]
        self.app.scan()
        server.active[session] = {"type": "running"}
        current["items"][0]["agents"][0]["state"] = "working"
        hook_record["agentLifecycle"] = "running"
        write_hook()

        output = io.StringIO()
        result = self.app.receive(THREAD, output=output, session_hint=session)

        self.assertEqual(result["group"], 1)
        self.assertEqual(result["received"], ["101"])
        self.assertIn("[discord:100:101] Configured user: busy-session group reply", output.getvalue())
        self.assertEqual(self.messages()[0][1], "received")

    def test_busy_opencode_can_register_manage_and_close_without_waking(self):
        session, server, hook_record, write_hook, current, _settings = self._prepare_busy_opencode()
        server.active[session] = {"type": "running"}
        current["items"][0]["agents"][0]["state"] = "working"
        hook_record["agentLifecycle"] = "running"
        write_hook()

        registered = self.app.register(THREAD, session_hint=session)
        self.assertTrue(registered["registered"])
        self.assertEqual(registered["owner_session"], session)
        managed = self.app.managed(THREAD, session_hint=session)
        self.assertTrue(managed["managed"])
        self.assertEqual(managed["owner_session"], session)

        self.api.rows = [message("101", content="preserve across close")]
        self.app.scan()
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        report = self.app.scan(send=True)
        self.assertEqual(report["threads"][0]["wake"]["state"], "held")
        self.assertIn("session_busy", report["threads"][0]["wake"]["held"])
        self.assertEqual(self.messages()[0][1:3], ("pending", 0))

        closed = self.app.close(THREAD, session_hint=session)
        self.assertEqual(closed, {"thread": THREAD, "closed": True,
                                  "subscribed": True, "pending_preserved": True})
        self.assertEqual(self.messages()[0][1], "pending")
        self.assertTrue(self.app.register(THREAD, session_hint=session)["already_registered"])
        self.assertFalse(self.app.managed(THREAD, session_hint=session)["open"])

    def test_busy_opencode_lifecycle_checks_identity_session_generation_and_api(self):
        session, server, hook_record, write_hook, current, settings = self._prepare_busy_opencode()
        server.active[session] = {"type": "running"}
        current["items"][0]["agents"][0]["state"] = "working"
        hook_record["agentLifecycle"] = "running"
        write_hook()
        self.app.register(THREAD, session_hint=session)

        with self.assertRaisesRegex(ValueError, "exact enrolled"):
            self.app.managed(THREAD, session_hint="ses_OtherDiscordSession")
        os.environ["CMUX_SURFACE_ID"] = SURFACE_B
        with self.assertRaisesRegex(ValueError, "exact enrolled"):
            self.app.managed(THREAD, session_hint=session)
        os.environ["CMUX_SURFACE_ID"] = SURFACE_A
        os.environ["CMUX_WORKSPACE_ID"] = WORKSPACE_B
        with self.assertRaisesRegex(ValueError, "exact enrolled"):
            self.app.managed(THREAD, session_hint=session)
        os.environ["CMUX_WORKSPACE_ID"] = WORKSPACE_A
        self.targets[0]["started_at"] += 1
        self._write_hailing()
        with self.assertRaisesRegex(ValueError, "generation changed"):
            self.app.managed(THREAD, session_hint=session)
        self.targets[0]["started_at"] = 1234.5
        self.targets[0]["expected_server_pid"] += 1
        self._write_hailing()
        with self.assertRaisesRegex(ValueError, "api_server_pid_mismatch"):
            self.app.managed(THREAD, session_hint=session)
        self.targets[0]["expected_server_pid"] = os.getpid()
        self._write_hailing()

        server.malformed_info = True
        with self.assertRaisesRegex(ValueError, "unsupported_api_schema"):
            self.app.managed(THREAD, session_hint=session)
        server.malformed_info = False
        settings["receiving"] = False
        self.assertTrue(self.app.managed(THREAD, session_hint=session)["managed"])
        settings["receiving"] = True
        self.assertTrue(self.app.managed(THREAD, session_hint=session)["managed"])

    def test_opencode_policy_is_persisted_and_live_policy_change_holds(self):
        session, _server, _hook, _write_hook, _current, _settings = self._prepare_busy_opencode()
        self.targets[0]["delivery_policy"] = "api_sidechannel"
        self._write_hailing()
        self.app.register(THREAD, session_hint=session)
        self.api.rows = [message("101", content="policy remains bound")]
        self.app.scan()
        with contextlib.closing(self.app.database(create=False)) as db:
            original_json = db.execute("SELECT owner_json FROM threads WHERE thread_id=?", (THREAD,)).fetchone()[0]
        original_owner = json.loads(original_json)
        self.assertEqual(original_owner["delivery_policy"], "api_sidechannel")

        self.targets[0]["delivery_policy"] = "native_empty_composer"
        self._write_hailing()
        with self.assertRaisesRegex(ValueError, "Registered owner is no longer enrolled"):
            with contextlib.closing(self.app.database(create=False)) as db:
                thread = self.app._thread(db, THREAD)
            self.app.validate_registered_owner(thread["owner"])
        with self.assertRaisesRegex(ValueError, "another exact session"):
            self.app.managed(THREAD, session_hint=session)
        with self.assertRaisesRegex(ValueError, "Only the registered current owner"):
            self.app.close(THREAD, session_hint=session)
        with self.assertRaisesRegex(ValueError, "another exact session"):
            self.app.register(THREAD, session_hint=session)
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        report = self.app.scan(send=True)
        self.assertEqual(report["threads"][0]["wake"]["state"], "held")
        self.assertIn("Registered owner is no longer enrolled", report["threads"][0]["wake"]["held"])
        self.assertEqual(self.messages()[0][1:3], ("pending", 0))
        with contextlib.closing(self.app.database(create=False)) as db:
            stored_json = db.execute("SELECT owner_json FROM threads WHERE thread_id=?", (THREAD,)).fetchone()[0]
        self.assertEqual(stored_json, original_json)

    def test_legacy_opencode_owner_without_policy_fails_closed_without_rewrite(self):
        session, _server, _hook, _write_hook, _current, _settings = self._prepare_busy_opencode()
        self.app.register(THREAD, session_hint=session)
        with contextlib.closing(self.app.database(create=False)) as db:
            raw_owner = json.loads(db.execute("SELECT owner_json FROM threads WHERE thread_id=?",
                                              (THREAD,)).fetchone()[0])
            raw_owner.pop("delivery_policy")
            with db:
                db.execute("UPDATE threads SET owner_json=? WHERE thread_id=?",
                           (json.dumps(raw_owner, separators=(",", ":")), THREAD))

        with self.assertRaisesRegex(ValueError, "another exact session"):
            self.app.managed(THREAD, session_hint=session)
        with self.assertRaisesRegex(ValueError, "another exact session"):
            self.app.register(THREAD, session_hint=session)
        with contextlib.closing(self.app.database(create=False)) as db:
            stored_owner = json.loads(db.execute("SELECT owner_json FROM threads WHERE thread_id=?",
                                                 (THREAD,)).fetchone()[0])
        self.assertNotIn("delivery_policy", stored_owner)

    def test_lifecycle_commands_reject_foreign_owner_and_preserve_existing_registration(self):
        self.register()
        self.api.rows = [message("101", content="keep for registered owner")]
        self.app.scan()
        self.targets.append(target(SESSION_B, SURFACE_B, WORKSPACE_B))
        self._write_hailing()
        os.environ["CMUX_SURFACE_ID"] = SURFACE_B
        os.environ["CMUX_WORKSPACE_ID"] = WORKSPACE_B
        os.environ["CODEX_THREAD_ID"] = SESSION_B

        with self.assertRaisesRegex(ValueError, "another exact session"):
            self.app.register(THREAD)
        with self.assertRaisesRegex(ValueError, "another exact session"):
            self.app.managed(THREAD)
        with self.assertRaisesRegex(ValueError, "Only the registered current owner"):
            self.app.close(THREAD)
        with contextlib.closing(self.app.database(create=False)) as db:
            owner = json.loads(db.execute("SELECT owner_json FROM threads WHERE thread_id=?",
                                           (THREAD,)).fetchone()[0])
        self.assertEqual(owner["session"], SESSION_A)
        self.assertEqual(self.messages()[0][1], "pending")

        os.environ["CMUX_SURFACE_ID"] = SURFACE_A
        os.environ["CMUX_WORKSPACE_ID"] = WORKSPACE_A
        os.environ["CODEX_THREAD_ID"] = SESSION_A
        self.assertTrue(self.app.close(THREAD)["closed"])
        self.assertEqual(self.messages()[0][1], "pending")

    def test_receive_output_failure_does_not_record_receipt(self):
        self.register()
        self.api.rows = [message("101", content="not acknowledged")]
        self.app.scan()

        class FailingOutput:
            def write(self, value):
                return len(value)

            def flush(self):
                raise OSError("synthetic stdout flush failure")

        with self.assertRaisesRegex(OSError, "synthetic stdout flush failure"):
            self.app.receive(THREAD, "101", FailingOutput())
        self.assertEqual(self.messages()[0][1], "pending")

    def test_receive_rechecks_owner_after_flush_before_recording_receipt(self):
        self.register()
        self.api.rows = [message("101", content="owner can change")]
        self.app.scan()

        class ReplacingOutput(io.StringIO):
            def flush(inner_self):
                super().flush()
                self.targets = [target(SESSION_B, SURFACE_B, WORKSPACE_B)]
                self._write_hailing()

        with self.assertRaisesRegex(ValueError, "exact enrolled"):
            self.app.receive(THREAD, "101", ReplacingOutput())
        self.assertEqual(self.messages()[0][1], "pending")

    def test_receive_for_other_harness_still_requires_full_binding(self):
        self.register()
        self.api.rows = [message("101")]
        self.app.scan()
        self.cmux.fail_binding = "session ended"
        with self.assertRaisesRegex(ValueError, "session ended"):
            self.app.register(THREAD)
        with self.assertRaisesRegex(ValueError, "session ended"):
            self.app.managed(THREAD)
        with self.assertRaisesRegex(ValueError, "session ended"):
            self.app.close(THREAD)
        with self.assertRaisesRegex(ValueError, "session ended"):
            self.app.receive(THREAD, "101", io.StringIO())
        self.assertEqual(self.messages()[0][1], "pending")
        with contextlib.closing(self.app.database(create=False)) as db:
            is_open = db.execute("SELECT is_open FROM threads WHERE thread_id=?", (THREAD,)).fetchone()[0]
        self.assertEqual(is_open, 1)

    def test_page_messages_and_cursor_roll_back_together_then_replay_after_restart(self):
        self.register()
        self.api.rows = [message("101"), message("102", content=7)]
        failed = self.app.scan()
        self.assertEqual(failed["threads"][0]["status"], "error")
        self.assertEqual(self.thread_cursor(), THREAD)
        self.assertEqual(self.messages(), [])

        self.api.rows = [message("101"), message("102", author_id="999", content="other")]
        self.clock[0] += dw.POLL_INTERVAL + 1
        self.app.scan()
        self.assertEqual(self.thread_cursor(), "102")
        self.assertEqual([row[0] for row in self.messages()], ["101"])
        restarted = FakeWatch(self.state, self.hailing, self.notify_home, api=self.api,
                              cmux=self.cmux, now=self.clock)
        restarted.scan()
        self.assertEqual([row[0] for row in self.messages()], ["101"])

        self.api.rows.append(message("103", content="second reply"))
        restarted.scan()
        self.assertEqual(self.thread_cursor(), "103")
        self.assertEqual([row[0] for row in self.messages()], ["101", "103"])

    def test_messages_are_filtered_to_configured_user_and_webhooks(self):
        self.register()
        self.api.rows = [message("101", author_id="999", content="other user"),
                         message("102", author_id=USER_ID, content="bot", bot=True),
                         message("103", author_id=USER_ID, content="webhook", webhook_id="77"),
                         message("104", author_id=USER_ID, content="Configured user")]
        self.app.scan()
        self.assertEqual([row[0] for row in self.messages()], ["104"])
        self.assertEqual(self.thread_cursor(), "104")

    def test_descending_api_pages_resume_durable_catchup_without_cursor_loss(self):
        self.register()
        self.api.rows = [message(str(message_id), content=f"reply {message_id}")
                         for message_id in range(101, 351)]
        with mock.patch.object(dw, "MAX_CATCHUP_PAGES", 2):
            partial = self.app.scan()
        self.assertTrue(partial["threads"][0]["catchup_pending"])
        self.assertEqual(self.thread_cursor(), THREAD)
        with contextlib.closing(self.app.database(create=False)) as db:
            boundary = db.execute("SELECT catchup_before,catchup_max FROM threads WHERE thread_id=?",
                                  (THREAD,)).fetchone()
        self.assertEqual(boundary, ("151", "350"))
        restarted = FakeWatch(self.state, self.hailing, self.notify_home, api=self.api,
                              cmux=self.cmux, now=self.clock)
        finished = restarted.scan()
        self.assertFalse(finished["threads"][0]["catchup_pending"])
        self.assertEqual(self.thread_cursor(), "350")
        self.assertEqual(len(self.messages()), 250)
        self.assertEqual(self.messages()[0][0], "101")
        self.assertEqual(self.messages()[-1][0], "350")

    def test_attachment_only_reply_preserves_filename_and_url_for_receive(self):
        self.register()
        url = "https://cdn.discordapp.com/attachments/100/101/screen.png"
        self.api.rows = [message("101", content="", attachments=[{"filename": "screen.png", "url": url}])]
        self.app.scan()
        output = io.StringIO()
        result = self.app.receive(THREAD, "101", output)
        self.assertEqual(result["received"], ["101"])
        self.assertIn("[discord:100:101] Configured user: ", output.getvalue())
        self.assertIn("[attachment] screen.png: " + url, output.getvalue())

    def test_exact_message_receipt_reconciles_without_reprinting_group_member(self):
        self.register()
        self.gateway("101", "first frozen body")
        self.gateway("102", "second frozen body")
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.wake()
        first = io.StringIO()
        self.app.receive(THREAD, "101", first)
        group_output = io.StringIO()
        result = self.app.receive(THREAD, output=group_output, group_id=1)
        self.assertEqual(result["received"], ["102"])
        self.assertIn("second frozen body", group_output.getvalue())
        self.assertNotIn("first frozen body", group_output.getvalue())
        self.assertEqual([row[1] for row in self.messages()], ["received", "received"])
        self.assertEqual(self.delivery_groups()[0][2], "received")

    def test_new_arrival_during_native_submit_stays_in_a_later_group(self):
        self.register()
        self.gateway("101", "frozen before submit")
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        output = io.StringIO()

        def receive_frozen_group():
            self.gateway("102", "arrived during submit")
            self.app.receive(THREAD, output=output, group_id=1)

        self.cmux.on_submit = receive_frozen_group
        result = self.wake()
        self.assertEqual(result["state"], "received")
        self.assertIn("frozen before submit", output.getvalue())
        self.assertNotIn("arrived during submit", output.getvalue())
        self.assertEqual(self.group_members(1), [("101",)])
        self.assertEqual(self.messages()[1][1], "pending")
        self.cmux.on_submit = None
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        second = self.wake()
        self.assertTrue(second["submitted"])
        self.assertEqual(self.group_members(2), [("102",)])

    def test_new_arrival_during_group_output_remains_unread_and_unassigned(self):
        self.register()
        self.gateway("101", "first frozen body")
        self.gateway("102", "second frozen body")
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.wake()

        class ArrivalOutput(io.StringIO):
            def write(inner_self, text):
                written = super().write(text)
                self.gateway("103", "arrived during receive")
                return written

        output = ArrivalOutput()
        result = self.app.receive(THREAD, output=output, group_id=1)
        self.assertEqual(result["received"], ["101", "102"])
        self.assertNotIn("arrived during receive", output.getvalue())
        self.assertEqual(self.group_members(1), [("101",), ("102",)])
        self.assertEqual(self.messages()[-1][0:2], ("103", "pending"))
        with contextlib.closing(self.app.database(create=False)) as db:
            self.assertIsNone(db.execute("SELECT group_id FROM messages WHERE thread_id=? AND message_id='103'",
                                         (THREAD,)).fetchone()[0])

    def test_short_write_or_flush_failure_leaves_every_group_receipt_unchanged(self):
        self.register()
        self.gateway("101", "first")
        self.gateway("102", "second")
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.wake()

        class ShortWriter:
            def write(self, text):
                return len(text) - 1

            def flush(self):
                return None

        class FlushFailure(io.StringIO):
            def flush(self):
                raise OSError("synthetic flush failure")

        for output in (ShortWriter(), FlushFailure()):
            with self.subTest(output=type(output).__name__), self.assertRaises((ValueError, OSError)):
                self.app.receive(THREAD, output=output, group_id=1)
            self.assertEqual([row[1] for row in self.messages()], ["awaiting_receipt", "awaiting_receipt"])
            self.assertNotEqual(self.delivery_groups()[0][2], "received")

    def test_two_receivers_share_one_lock_and_only_one_can_print_group(self):
        self.register()
        self.gateway("101", "single output body")
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.wake()
        entered = threading.Event()
        release = threading.Event()
        second_output = io.StringIO()
        outcomes = []

        class BlockingOutput(io.StringIO):
            def write(inner_self, text):
                entered.set()
                release.wait(timeout=3)
                return super().write(text)

        first_output = BlockingOutput()

        def first_receiver():
            try:
                outcomes.append(self.app.receive(THREAD, output=first_output, group_id=1))
            except Exception as error:
                outcomes.append(error)

        def second_receiver():
            try:
                outcomes.append(self.app.receive(THREAD, output=second_output, group_id=1))
            except Exception as error:
                outcomes.append(error)

        first = threading.Thread(target=first_receiver)
        first.start()
        self.assertTrue(entered.wait(timeout=2))
        second = threading.Thread(target=second_receiver)
        second.start()
        release.set()
        first.join(timeout=3)
        second.join(timeout=3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, ValueError) for item in outcomes), 1)
        self.assertEqual(first_output.getvalue().count("single output body"), 1)
        self.assertEqual(second_output.getvalue(), "")
        self.assertEqual(self.messages()[0][1], "received")

    def test_confirmed_group_is_not_resubmitted_and_later_reply_waits_for_receipt(self):
        self.register()
        self.gateway("101", "confirmed body")
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        first = self.wake()
        self.assertTrue(first["submitted"])
        self.assertEqual(self.delivery_groups()[0][2], "awaiting_receipt")
        self.clock[0] += 600
        self.gateway("102", "later body")
        blocked = self.wake()
        self.assertEqual(blocked["state"], "awaiting_receipt")
        self.assertEqual(self.cmux.submit_count, 1)
        self.assertEqual(self.group_members(1), [("101",)])
        self.assertEqual(self.messages()[1][1], "pending")
        self.app.receive(THREAD, output=io.StringIO(), group_id=1)
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        later = self.wake()
        self.assertTrue(later["submitted"])
        self.assertEqual(self.group_members(2), [("102",)])
        self.assertEqual(self.cmux.submit_count, 2)

    def test_group_wake_uses_active_runtime_even_when_path_has_an_older_cli(self):
        self.register()
        self.gateway("101", "private synthetic body")
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        with mock.patch.object(dw.shutil, "which", return_value="/older/discord-watch"):
            result = self.wake()
        self.assertTrue(result["submitted"])
        prompt = self.cmux.prompts[0]
        self.assertIn(sys.executable, prompt)
        self.assertIn(str(Path(dw.__file__).resolve()), prompt)
        self.assertIn("--group 1", prompt)
        self.assertNotIn("/older/discord-watch", prompt)
        self.assertNotIn("private synthetic body", prompt)

    def test_transferred_frozen_group_and_new_generation_messages_stay_separate(self):
        self.register()
        self.gateway("101", "generation one")
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.wake()
        self.targets.append(target(SESSION_B, SURFACE_B, WORKSPACE_B))
        self._write_hailing()
        os.environ["CMUX_SURFACE_ID"] = SURFACE_B
        os.environ["CMUX_WORKSPACE_ID"] = WORKSPACE_B
        os.environ["CODEX_THREAD_ID"] = SESSION_B
        self.app.transfer(THREAD, SESSION_A)
        self.gateway("102", "generation two")
        self.assertEqual(self.group_members(1), [("101",)])
        output = io.StringIO()
        self.app.receive(THREAD, output=output, group_id=1)
        self.assertIn("generation one", output.getvalue())
        self.assertNotIn("generation two", output.getvalue())
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.wake()
        self.assertEqual(self.group_members(2), [("102",)])
        self.assertEqual(self.delivery_groups()[1][1], 2)

    def test_generation_edit_without_ownership_event_holds_frozen_group(self):
        self.register()
        self.gateway("101", "frozen owner body")
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.wake()
        with contextlib.closing(self.app.database(create=False)) as db:
            with db:
                db.execute("UPDATE threads SET owner_generation=2 WHERE thread_id=?", (THREAD,))
        output = io.StringIO()
        with self.assertRaisesRegex(ValueError, "authorized transfer chain"):
            self.app.receive(THREAD, output=output, group_id=1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.messages()[0][1], "awaiting_receipt")

    def test_authorized_transfer_during_output_preserves_receipts_until_new_owner_receives(self):
        self.register()
        self.gateway("101", "transfer race body")
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.wake()
        self.targets.append(target(SESSION_B, SURFACE_B, WORKSPACE_B))

        class TransferOutput(io.StringIO):
            def write(inner_self, text):
                written = super().write(text)
                self._write_hailing()
                os.environ["CMUX_SURFACE_ID"] = SURFACE_B
                os.environ["CMUX_WORKSPACE_ID"] = WORKSPACE_B
                os.environ["CODEX_THREAD_ID"] = SESSION_B
                self.app.transfer(THREAD, SESSION_A)
                return written

        output = TransferOutput()
        with self.assertRaisesRegex(ValueError, "Owner changed during reply output"):
            self.app.receive(THREAD, output=output, group_id=1)
        self.assertIn("transfer race body", output.getvalue())
        self.assertEqual(self.messages()[0][1], "awaiting_receipt")
        new_owner_output = io.StringIO()
        result = self.app.receive(THREAD, output=new_owner_output, group_id=1)
        self.assertEqual(result["received"], ["101"])
        self.assertIn("transfer race body", new_owner_output.getvalue())

    def test_held_input_does_not_mark_transport_or_consume_attempt(self):
        self.register()
        self.api.rows = [message("101")]
        self.app.scan()
        self.cmux.hold_submit = "Composer has a human draft"
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        result = self.app.scan(send=True)
        self.assertEqual(result["threads"][0]["wake"]["state"], "held")
        row = self.messages()[0]
        self.assertEqual(row[1:3], ("pending", 0))
        self.assertIn("last_error", self.app.status()["threads"][0]["recent"][0])
        self.assertEqual(self.cmux.submit_count, 0)

    def test_uncertain_transport_is_not_retried_and_explicit_receive_resolves_it(self):
        self.register()
        self.api.rows = [message("101", content="private reply body")]
        self.app.scan()
        self.cmux.uncertain = True
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.app.scan(send=True)
        self.assertEqual(self.messages()[0][1], "uncertain")
        self.assertNotIn("private reply body", self.cmux.prompts[0])
        self.assertIn("--state-dir " + str(self.state), self.cmux.prompts[0])
        self.assertIn("receive --thread 100 --group 1", self.cmux.prompts[0])
        self.assertIn(str(Path(dw.__file__).resolve()), self.cmux.prompts[0])
        self.cmux.uncertain = False
        self.app.scan(send=True)
        self.assertEqual(self.cmux.submit_count, 1)
        output = io.StringIO()
        receipt = self.app.receive(THREAD, "101", output)
        self.assertEqual(receipt["received"], ["101"])
        self.assertIn("[discord:100:101] Configured user: private reply body", output.getvalue())
        self.assertEqual(self.messages()[0][1], "received")

    def test_concurrent_scans_submit_once_and_receive_does_not_get_overwritten(self):
        self.register()
        self.api.rows = [message("101", content="body")]
        self.app.scan()
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.cmux.entered = threading.Event()
        self.cmux.release = threading.Event()
        results = []
        first = threading.Thread(target=lambda: results.append(self.app.scan(send=True)))
        first.start()
        self.assertTrue(self.cmux.entered.wait(timeout=2))
        second = self.app.scan(send=True)
        self.assertEqual(self.cmux.submit_count, 1)
        self.assertEqual(second["threads"][0]["wake"]["state"], "wake_busy")
        self.cmux.release.set()
        first.join(timeout=3)
        self.assertFalse(first.is_alive())
        self.assertEqual(self.messages()[0][1], "awaiting_receipt")

    def test_receive_during_transport_keeps_receipt_state(self):
        self.register()
        self.api.rows = [message("101", content="body")]
        self.app.scan()
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        output = io.StringIO()
        self.cmux.on_submit = lambda: self.app.receive(THREAD, "101", output)
        result = self.app.scan(send=True)
        self.assertEqual(self.messages()[0][1], "received")
        self.assertEqual(result["threads"][0]["wake"]["state"], "received")

    def test_local_close_retains_subscription_and_allows_guarded_reply_delivery(self):
        self.register()
        self.app.close(THREAD)
        self.app.close(THREAD)
        self.assertEqual(self.thread_lifecycle()[:2], (0, "active"))
        managed = self.app.managed(THREAD)
        self.assertTrue(managed["managed"])
        self.assertFalse(managed["open"])
        self.api.rows = [message("101")]
        self.app.scan()
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        report = self.app.scan(send=True)
        self.assertEqual(report["threads"][0]["status"], "polled")
        self.assertTrue(report["threads"][0]["wake"]["submitted"])
        self.assertEqual(self.messages()[0][1], "awaiting_receipt")
        self.assertEqual(self.cmux.submit_count, 1)
        output = io.StringIO()
        self.app.receive(THREAD, "101", output)
        self.assertIn("Configured user reply", output.getvalue())

    def test_remote_archive_is_recorded_without_unsubscribing_or_blocking_late_reply(self):
        self.register()
        self.api.rows = [message("101")]
        self.app.scan()
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.app.scan(send=True)
        self.app.receive(THREAD, "101", io.StringIO())
        self.api.remote["thread_metadata"]["archived"] = True
        self.api.rows.append(message("102", content="late archived reply"))
        self.app.scan()
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        report = self.app.scan(send=True)
        self.assertEqual(report["threads"][0]["status"], "polled")
        self.assertTrue(report["threads"][0]["remote_closed"])
        self.assertEqual(report["threads"][0]["wake"]["state"], "awaiting_receipt")
        self.assertEqual(self.messages()[1][0:2], ("102", "awaiting_receipt"))
        self.assertEqual(self.app.managed(THREAD)["owner_session"], SESSION_A)
        self.assertTrue(self.app.status()["threads"][0]["remote_closed"])
        output = io.StringIO()
        self.app.receive(THREAD, "102", output)
        self.assertIn("late archived reply", output.getvalue())

    def test_transfer_requires_exact_from_session_and_preserves_unreceived_replies(self):
        self.register()
        self.api.rows = [message("101")]
        self.app.scan()
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.app.scan(send=True)
        self.app.close(THREAD)
        self.targets.append(target(SESSION_B, SURFACE_B, WORKSPACE_B))
        self._write_hailing()
        os.environ["CMUX_SURFACE_ID"] = SURFACE_B
        os.environ["CMUX_WORKSPACE_ID"] = WORKSPACE_B
        os.environ["CODEX_THREAD_ID"] = SESSION_B
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.app.transfer(THREAD, SESSION_B)
        result = self.app.transfer(THREAD, SESSION_A)
        self.assertEqual(result["to_session"], SESSION_B)
        self.assertEqual(self.messages()[0][1:3], ("awaiting_receipt", 1))
        self.assertEqual(self.messages()[0][3], "Configured user reply")
        managed = self.app.managed(THREAD)
        self.assertEqual(managed["owner_session"], SESSION_B)
        self.assertFalse(managed["open"])

    def test_closed_transfer_preserves_uncertain_submission_and_exact_generation(self):
        self.register()
        self.api.rows = [message("101")]
        self.app.scan()
        self.cmux.uncertain = True
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.app.scan(send=True)
        self.assertEqual(self.messages()[0][1], "uncertain")
        self.app.close(THREAD)
        self.targets.append(target(SESSION_B, SURFACE_B, WORKSPACE_B))
        self._write_hailing()
        os.environ["CMUX_SURFACE_ID"] = SURFACE_B
        os.environ["CMUX_WORKSPACE_ID"] = WORKSPACE_B
        os.environ["CODEX_THREAD_ID"] = SESSION_B
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.app.transfer(THREAD, SESSION_B)
        self.app.transfer(THREAD, SESSION_A)
        self.assertEqual(self.messages()[0][1:3], ("uncertain", 1))
        with contextlib.closing(self.app.database(create=False)) as db:
            self.assertEqual(db.execute("SELECT owner_generation,is_open FROM threads WHERE thread_id=?",
                                         (THREAD,)).fetchone(), (2, 0))
        managed = self.app.managed(THREAD)
        self.assertEqual(managed["owner_session"], SESSION_B)
        self.assertFalse(managed["open"])

    def test_each_attempt_preserves_its_exact_target_across_a_to_b_uncertain_to_c(self):
        self.register()
        self.gateway("101", "attempt attribution body")
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        self.cmux.hold_submit = "synthetic guarded hold"
        first = self.wake()
        self.assertEqual(first["state"], "held")
        self.assertEqual(self.cmux.submit_count, 0)
        self.assertEqual(self.delivery_groups()[0][2], "eligible")

        owner_a = self.app.binding_snapshot(self.targets[0])
        self.targets.append(target(SESSION_B, SURFACE_B, WORKSPACE_B))
        self._write_hailing()
        os.environ["CMUX_SURFACE_ID"] = SURFACE_B
        os.environ["CMUX_WORKSPACE_ID"] = WORKSPACE_B
        os.environ["CODEX_THREAD_ID"] = SESSION_B
        self.app.transfer(THREAD, SESSION_A)
        owner_b = self.app.binding_snapshot(self.targets[1])
        self.cmux.hold_submit = None
        self.cmux.uncertain = True
        second = self.wake()
        self.assertEqual(second["state"], "uncertain")
        self.assertEqual(self.cmux.submit_count, 1)
        status = self.app.status()["threads"][0]["delivery_groups"][0]
        self.assertEqual(status["owner_generation"], 1)
        self.assertEqual(status["attempts"][0]["target_owner"], owner_a)
        self.assertEqual(status["attempts"][0]["target_owner_generation"], 1)
        self.assertEqual(status["attempts"][0]["state"], "held")
        self.assertEqual(status["attempts"][1]["target_owner"], owner_b)
        self.assertEqual(status["attempts"][1]["target_owner_generation"], 2)
        self.assertEqual(status["attempts"][1]["state"], "uncertain")

        self.targets.append(target("33333333-3333-4333-8333-333333333333",
                                   "EEEEEEEE-EEEE-4EEE-8EEE-EEEEEEEEEEEE",
                                   "FFFFFFFF-FFFF-4FFF-8FFF-FFFFFFFFFFFF"))
        self._write_hailing()
        owner_c = self.app.binding_snapshot(self.targets[2])
        os.environ["CMUX_SURFACE_ID"] = owner_c["surface"]
        os.environ["CMUX_WORKSPACE_ID"] = owner_c["workspace"]
        os.environ["CODEX_THREAD_ID"] = owner_c["session"]
        self.app.transfer(THREAD, SESSION_B)
        self.cmux.uncertain = False
        blocked = self.wake()
        self.assertEqual(blocked["state"], "uncertain")
        self.assertEqual(self.cmux.submit_count, 1)
        output = io.StringIO()
        result = self.app.receive(THREAD, output=output, group_id=1)
        self.assertEqual(result["received"], ["101"])
        self.assertIn("attempt attribution body", output.getvalue())

        self.app = FakeWatch(self.state, self.hailing, self.notify_home, api=self.api,
                             cmux=self.cmux, now=self.clock)
        resumed = self.app.status()["threads"][0]["delivery_groups"][0]
        self.assertEqual(resumed["state"], "received")
        self.assertEqual([item["target_owner"] for item in resumed["attempts"]], [owner_a, owner_b])
        self.assertEqual([item["target_owner_generation"] for item in resumed["attempts"]], [1, 2])
        self.assertEqual(self.cmux.submit_count, 1)

    def test_migrated_attempt_history_keeps_unknown_target_as_null(self):
        self.register()
        self.gateway("101", "legacy attempt body")
        self.clock[0] += dw.GROUP_QUIET_SECONDS
        result = self.wake()
        self.assertTrue(result["submitted"])
        with contextlib.closing(self.app.database(create=False)) as db:
            with db:
                db.execute("ALTER TABLE delivery_group_attempts RENAME TO delivery_group_attempts_new")
                db.execute("""CREATE TABLE delivery_group_attempts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  group_id INTEGER NOT NULL REFERENCES delivery_groups(id),
                  state TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  transport_started_at REAL,
                  submitted_at REAL,
                  last_error_json TEXT
                )""")
                db.execute("INSERT INTO delivery_group_attempts(id,group_id,state,created_at,"
                           "transport_started_at,submitted_at,last_error_json) "
                           "SELECT id,group_id,state,created_at,transport_started_at,submitted_at,last_error_json "
                           "FROM delivery_group_attempts_new")
                db.execute("DROP TABLE delivery_group_attempts_new")
        migrated = self.app.status()["threads"][0]["delivery_groups"][0]["attempts"][0]
        self.assertEqual(migrated["state"], "confirmed")
        self.assertEqual(migrated["target_owner"], None)
        self.assertEqual(migrated["target_owner_generation"], None)

    def test_api_rate_limit_details_are_retained_without_token_leak(self):
        import io as io_module
        headers = {"Retry-After": "4.25"}
        body = json.dumps({"message": "slow down", "code": 20028,
                           "retry_after": 4.25, "global": True}).encode("utf-8")
        error = urllib.error.HTTPError("https://discord.com/api/v10/channels/100", 429,
                                       "Too Many Requests", headers, io_module.BytesIO(body))
        api = dw.Discord("do-not-print-this-token", opener=lambda request, timeout: (_ for _ in ()).throw(error))
        with self.assertRaises(dw.DiscordAPIError) as caught:
            api.get("/channels/100")
        record = caught.exception.record()
        self.assertEqual(record["http_status"], 429)
        self.assertEqual(record["discord_code"], 20028)
        self.assertEqual(record["retry_after"], 4.25)
        self.assertTrue(record["global_rate_limit"])
        self.assertNotIn("do-not-print-this-token", str(caught.exception))

    def test_successful_rate_limit_headers_persist_route_and_global_buckets(self):
        self.register()

        class Response:
            def __init__(self, headers):
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, maximum):
                return b"{}"

        route_api = dw.Discord("synthetic-token", opener=lambda request, timeout: Response({
            "X-RateLimit-Bucket": "messages-bucket", "X-RateLimit-Limit": "5",
            "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "10"}),
            clock=lambda: TEST_NOW)
        route_api.get("/channels/100/messages")
        self.assertEqual(route_api.deferred_until("/channels/100/messages", TEST_NOW), TEST_NOW + 10)
        with contextlib.closing(self.app.database(create=False)) as db:
            self.app._persist_api_limits(db, route_api, THREAD, TEST_NOW)
            stored = db.execute("SELECT bucket_key,remaining,reset_at FROM api_rate_limits WHERE route_key=?",
                                ("GET /channels/100/messages",)).fetchone()
        self.assertEqual(stored, ("messages-bucket:100", 0, TEST_NOW + 10))

        global_api = dw.Discord("synthetic-token", opener=lambda request, timeout: Response({
            "X-RateLimit-Global": "true", "X-RateLimit-Reset-After": "15"}),
            clock=lambda: TEST_NOW)
        global_api.get("/gateway/bot")
        self.assertEqual(global_api.global_not_before, TEST_NOW + 15)

    def test_managed_health_is_specific_to_the_requested_thread(self):
        self.register()
        self.app.scan(send=True, origin="worker")
        with mock.patch.object(self.app, "_lock_is_held", return_value=True):
            self.assertTrue(self.app.managed(THREAD)["worker_health"]["ready_for_wakes"])
            with contextlib.closing(self.app.database(create=False)) as db:
                with db:
                    db.execute("UPDATE meta SET value=? WHERE key='last_send_scope'", (json.dumps(["999"]),))
            health = self.app.managed(THREAD)["worker_health"]
            self.assertFalse(health["ready_for_wakes"])
            self.assertFalse(health["thread_in_send_scope"])

    def test_failed_api_scan_does_not_refresh_success_health(self):
        self.register()
        self.api.rows = [message("101")]
        self.app.scan(origin="worker")
        self.clock[0] = TEST_NOW + 70
        self.api.fail = dw.DiscordAPIError(403, "missing access", code=50001)
        self.app.scan(send=True, origin="worker")
        lock_path = self.state / "worker.lock"
        with lock_path.open("a") as stream:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
            with contextlib.closing(self.app.database(create=False)) as db:
                health = self.app._health(db, THREAD)
            fcntl.flock(stream, fcntl.LOCK_UN)
        self.assertTrue(health["worker_running"])
        self.assertFalse(health["api_success_fresh"])
        self.assertFalse(health["ready_for_wakes"])
        self.assertIsNotNone(health["thread_error"])

    def test_receive_empty_returns_three_without_stdout(self):
        self.register()
        output = io.StringIO()
        with mock.patch.object(dw.Watch, "_identity_owner",
                               return_value=self.app.binding_snapshot(self.targets[0])):
            with contextlib.redirect_stdout(output):
                result = dw.main(["--state-dir", str(self.state), "--hailing-state", str(self.hailing),
                                  "--notify-home", str(self.notify_home), "receive", "--thread", THREAD])
        self.assertEqual(result, 3)
        self.assertEqual(output.getvalue(), "")

    def test_config_reader_captures_credentials_without_printing_them(self):
        config = self.notify_home / "env"
        config.write_text(f"DISCORD_BOT_TOKEN=hidden-token\nDISCORD_USER_ID={USER_ID}\n", encoding="utf-8")
        config.chmod(0o600)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            values = dw.read_notify_credentials(self.notify_home)
        self.assertEqual(values["token"], "hidden-token")
        self.assertEqual(values["user_id"], USER_ID)
        self.assertEqual(output.getvalue(), "")


class InstallerTests(unittest.TestCase):
    @staticmethod
    def fixture_provisioner(vendor_dir, lock_file):
        package = Path(vendor_dir) / "websocket"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text('__version__ = "1.9.2"\n', encoding="utf-8")

    def test_runtime_is_published_before_sealing_and_launcher_replacement(self):
        sys.path.insert(0, str(APP_DIR))
        import install
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_root = root / "runtime"
            bin_dir = root / "bin"
            launcher = bin_dir / "discord-watch"
            events = []
            candidate = {}
            original_replace = install.os.replace
            original_seal = install._make_read_only

            def record_replace(source, destination):
                source, destination = Path(source), Path(destination)
                if source.name.startswith(".staging-") and destination.parent == runtime_root:
                    self.assertTrue(source.stat().st_mode & stat.S_IWUSR)
                    candidate["runtime"] = destination
                    events.append("publish")
                elif destination == launcher:
                    runtime = candidate["runtime"]
                    self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o555)
                    install._require_sealed_runtime(runtime)
                    events.append("launcher")
                return original_replace(source, destination)

            def record_seal(path):
                path = Path(path)
                self.assertEqual(path, candidate["runtime"])
                self.assertTrue(path.is_dir())
                self.assertTrue(path.stat().st_mode & stat.S_IWUSR)
                events.append("seal")
                return original_seal(path)

            with mock.patch.object(install.os, "replace", side_effect=record_replace), \
                    mock.patch.object(install, "_make_read_only", side_effect=record_seal):
                install.install(runtime_root, bin_dir, provisioner=self.fixture_provisioner)

            self.assertEqual(events, ["publish", "seal", "launcher"])

    def test_seal_failure_removes_only_new_runtime_and_preserves_previous_installation(self):
        sys.path.insert(0, str(APP_DIR))
        import install
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_root = root / "runtime"
            bin_dir = root / "bin"
            installed = install.install(runtime_root, bin_dir, provisioner=self.fixture_provisioner)
            launcher = bin_dir / "discord-watch"
            original_launcher = launcher.read_bytes()
            prior_runtime = Path(installed["runtime"])
            prior_snapshot = {
                path.relative_to(prior_runtime): (
                    path.read_bytes() if path.is_file() else None,
                    stat.S_IMODE(path.stat().st_mode),
                )
                for path in (prior_runtime, *prior_runtime.rglob("*"))
            }
            candidate_digest = "a" * 64
            self.assertNotEqual(candidate_digest, installed["sha256"])
            candidate = runtime_root / candidate_digest
            original_path_chmod = Path.chmod

            def fail_candidate_seal(path, mode):
                if Path(path) == candidate and mode == 0o555:
                    raise PermissionError("fixture runtime seal failure")
                return original_path_chmod(path, mode)

            with mock.patch.object(install, "_content_digest", return_value=candidate_digest), \
                    mock.patch.object(Path, "chmod", new=fail_candidate_seal):
                with self.assertRaisesRegex(PermissionError, "fixture runtime seal failure"):
                    install.install(runtime_root, bin_dir, provisioner=self.fixture_provisioner)

            self.assertFalse(os.path.lexists(candidate))
            self.assertEqual(launcher.read_bytes(), original_launcher)
            self.assertTrue(prior_runtime.is_dir())
            self.assertEqual(
                {
                    path.relative_to(prior_runtime): (
                        path.read_bytes() if path.is_file() else None,
                        stat.S_IMODE(path.stat().st_mode),
                    )
                    for path in (prior_runtime, *prior_runtime.rglob("*"))
                },
                prior_snapshot,
            )

            original_rmtree = install.shutil.rmtree

            def fail_candidate_cleanup(path, *args, **kwargs):
                if Path(path) == candidate:
                    raise PermissionError("fixture runtime cleanup failure")
                return original_rmtree(path, *args, **kwargs)

            with mock.patch.object(install, "_content_digest", return_value=candidate_digest), \
                    mock.patch.object(Path, "chmod", new=fail_candidate_seal), \
                    mock.patch.object(install.shutil, "rmtree", side_effect=fail_candidate_cleanup):
                with self.assertRaisesRegex(RuntimeError, "cleanup also failed"):
                    install.install(runtime_root, bin_dir, provisioner=self.fixture_provisioner)

            self.assertTrue(candidate.is_dir())
            self.assertEqual(launcher.read_bytes(), original_launcher)
            self.assertEqual(
                {
                    path.relative_to(prior_runtime): (
                        path.read_bytes() if path.is_file() else None,
                        stat.S_IMODE(path.stat().st_mode),
                    )
                    for path in (prior_runtime, *prior_runtime.rglob("*"))
                },
                prior_snapshot,
            )
            candidate_snapshot = {
                path.relative_to(candidate): (
                    path.read_bytes() if path.is_file() else None,
                    stat.S_IMODE(path.stat().st_mode),
                )
                for path in (candidate, *candidate.rglob("*"))
            }
            with mock.patch.object(install, "_content_digest", return_value=candidate_digest):
                with self.assertRaisesRegex(ValueError, "Existing runtime is not sealed"):
                    install.install(runtime_root, bin_dir, provisioner=self.fixture_provisioner)
            self.assertEqual(
                {
                    path.relative_to(candidate): (
                        path.read_bytes() if path.is_file() else None,
                        stat.S_IMODE(path.stat().st_mode),
                    )
                    for path in (candidate, *candidate.rglob("*"))
                },
                candidate_snapshot,
            )
            self.assertEqual(launcher.read_bytes(), original_launcher)

    def test_installed_guide_prints_outside_checkout_without_creating_state(self):
        sys.path.insert(0, str(APP_DIR))
        import install
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            runtime_root = root / "runtime"
            result = install.install(runtime_root, bin_dir, provisioner=self.fixture_provisioner)
            self.assertFalse(result["worker_started"])
            cwd = root / "outside"
            cwd.mkdir()
            runtime = Path(result["runtime"])
            before = {path.relative_to(runtime): (path.read_bytes(), path.stat().st_mode & 0o777)
                      for path in runtime.rglob("*") if path.is_file()}
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root)}
            completed = __import__("subprocess").run(
                [str(bin_dir / "discord-watch"), "guide", "--print"], cwd=cwd,
                env=env, stdout=__import__("subprocess").PIPE,
                stderr=__import__("subprocess").PIPE, text=True, timeout=10, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, (APP_DIR / "guide.md").read_text(encoding="utf-8"))
            self.assertFalse((root / ".local/state/starfleet-ai/discord-watch").exists())
            self.assertTrue((runtime / "hailing_frequencies.py").is_file())
            after = {path.relative_to(runtime): (path.read_bytes(), path.stat().st_mode & 0o777)
                     for path in runtime.rglob("*") if path.is_file()}
            self.assertEqual(after, before)
            self.assertFalse(any("__pycache__" in path.parts for path in runtime.rglob("*")))

    def test_dependency_provisioning_failure_keeps_existing_launcher_and_runtime(self):
        sys.path.insert(0, str(APP_DIR))
        import install
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            runtime_root = root / "runtime"
            installed = install.install(runtime_root, bin_dir, provisioner=self.fixture_provisioner)
            launcher = bin_dir / "discord-watch"
            original_launcher = launcher.read_bytes()
            original_runtime = Path(installed["runtime"])

            def fail_provision(vendor_dir, lock_file):
                raise ValueError("synthetic local fixture provisioning failure")

            with self.assertRaisesRegex(ValueError, "synthetic local fixture"):
                install.install(runtime_root, bin_dir, provisioner=fail_provision)
            self.assertEqual(launcher.read_bytes(), original_launcher)
            self.assertTrue(original_runtime.is_dir())

    def test_dependency_integrity_failure_keeps_existing_launcher_and_runtime(self):
        sys.path.insert(0, str(APP_DIR))
        import install
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            runtime_root = root / "runtime"
            installed = install.install(runtime_root, bin_dir, provisioner=self.fixture_provisioner)
            launcher = bin_dir / "discord-watch"
            original_launcher = launcher.read_bytes()
            original_runtime = Path(installed["runtime"])

            def bad_dependency(vendor_dir, lock_file):
                package = Path(vendor_dir) / "websocket"
                package.mkdir(parents=True)
                (package / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "integrity check failed"):
                install.install(runtime_root, bin_dir, provisioner=bad_dependency)
            self.assertEqual(launcher.read_bytes(), original_launcher)
            self.assertTrue(original_runtime.is_dir())


class ServiceCliTests(unittest.TestCase):
    def test_cli_validation_error_is_json_not_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            completed = __import__("subprocess").run(
                [sys.executable, str(APP_DIR / "discord_watch.py"), "--state-dir", temporary, "managed", "not-a-thread"],
                capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("error", json.loads(completed.stderr))
        self.assertNotIn("Traceback", completed.stderr)

    def test_graceful_service_stop_returns_zero(self):
        for command in ("serve", "supervise"):
            with self.subTest(command=command), mock.patch.object(dw.Watch, command, return_value={"stopped": True}):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(dw.main([command]), 0)
                self.assertEqual(json.loads(output.getvalue()), {"stopped": True})


if __name__ == "__main__":
    unittest.main()
