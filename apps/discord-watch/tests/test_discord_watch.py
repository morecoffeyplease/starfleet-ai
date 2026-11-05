import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import discord_watch as dw
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

    def binding(self, current):
        if self.fail_binding:
            raise ValueError(self.fail_binding)
        return True

    def submit(self, current, text, on_transport_start=None):
        self.binding(current)
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
            "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID")}
        os.environ["CMUX_AGENT_LAUNCH_KIND"] = "codex"
        os.environ["CMUX_SURFACE_ID"] = SURFACE_A
        os.environ["CMUX_WORKSPACE_ID"] = WORKSPACE_A
        os.environ["CODEX_THREAD_ID"] = SESSION_A
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

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
        report = self.app.scan(send=True)
        self.assertEqual(report["threads"][0]["status"], "dormant")
        self.assertEqual(report["threads"][0]["wake"]["state"], "awaiting_receipt")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.cmux.submit_count, 1)

        self.app.ingest_gateway_message(
            THREAD, message("102", timestamp=timestamp_at(self.clock[0] + 1)))
        self.app.receive(THREAD, "101", io.StringIO())
        self.clock[0] += 1
        self.cmux.fail_binding = "stale exact owner projection"
        held = self.app.scan(send=True)
        self.assertIn("wake_error", held["threads"][0])
        self.assertEqual(self.cmux.submit_count, 1)
        self.assertEqual(self.messages()[-1][1], "pending")
        self.cmux.fail_binding = None
        self.targets.clear()
        self._write_hailing()
        missing = self.app.scan(send=True)
        self.assertIn("wake_error", missing["threads"][0])
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

    def test_caller_without_session_env_uses_unique_surface_enrollment_and_binding(self):
        os.environ.pop("CODEX_THREAD_ID", None)
        self.assertEqual(self.register()["owner_session"], SESSION_A)

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

    def test_held_input_does_not_mark_transport_or_consume_attempt(self):
        self.register()
        self.api.rows = [message("101")]
        self.app.scan()
        self.cmux.hold_submit = "Composer has a human draft"
        result = self.app.scan(send=True)
        self.assertEqual(result["threads"][0]["wake"]["state"], "pending")
        row = self.messages()[0]
        self.assertEqual(row[1:3], ("pending", 0))
        self.assertIn("last_error", self.app.status()["threads"][0]["recent"][0])
        self.assertEqual(self.cmux.submit_count, 0)

    def test_uncertain_transport_is_not_retried_and_explicit_receive_resolves_it(self):
        self.register()
        self.api.rows = [message("101", content="private reply body")]
        self.app.scan()
        self.cmux.uncertain = True
        self.app.scan(send=True)
        self.assertEqual(self.messages()[0][1], "uncertain")
        self.assertNotIn("private reply body", self.cmux.prompts[0])
        self.assertIn("--state-dir " + str(self.state), self.cmux.prompts[0])
        self.assertIn("receive --thread 100 --message 101", self.cmux.prompts[0])
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
        self.app.scan(send=True)
        self.app.receive(THREAD, "101", io.StringIO())
        self.api.remote["thread_metadata"]["archived"] = True
        self.api.rows.append(message("102", content="late archived reply"))
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
        self.assertEqual(self.messages()[0][1:3], ("pending", 0))
        self.assertEqual(self.messages()[0][3], "Configured user reply")
        managed = self.app.managed(THREAD)
        self.assertEqual(managed["owner_session"], SESSION_B)
        self.assertFalse(managed["open"])

    def test_closed_transfer_preserves_uncertain_submission_and_exact_generation(self):
        self.register()
        self.api.rows = [message("101")]
        self.app.scan()
        self.cmux.uncertain = True
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
        with mock.patch.object(dw.Watch, "current_owner", return_value=self.app.binding_snapshot(self.targets[0])):
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
    def test_installed_guide_prints_outside_checkout_without_creating_state(self):
        sys.path.insert(0, str(APP_DIR))
        import install
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            runtime_root = root / "runtime"
            result = install.install(runtime_root, bin_dir)
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
