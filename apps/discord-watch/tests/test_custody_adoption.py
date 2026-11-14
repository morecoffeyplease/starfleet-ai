import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import custody_adoption as adoption
import discord_watch as dw
import hailing_frequencies as hf
import temporal_beacon as tb


SESSION = "11111111-1111-4111-8111-111111111111".upper()
SURFACE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper()
WORKSPACE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc".upper()
THREAD = "100"
OPERATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class FixtureCmux:
    def __init__(self, hook_directory):
        self.hook_directory = Path(hook_directory)
        self.executable = "fixture-cmux"
        self.bindings = []
        self.submissions = []

    def binding(self, target, **_kwargs):
        self.bindings.append(dict(target))
        if target["started_at"] != 200.0:
            raise ValueError("fixture current generation mismatch")

    def owner_identity_binding(self, target):
        self.binding(target)

    def submit(self, target, message):
        self.submissions.append((dict(target), message))
        return {"submitted": True}


class CustodyAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.hailing = root / "hailing"
        self.watch_state = root / "watch"
        self.worktree = (root / "worktree").resolve()
        self.hailing.mkdir(mode=0o700)
        self.watch_state.mkdir(mode=0o700)
        self.worktree.mkdir(mode=0o700)
        self.now = time.time()
        self.old = {"harness": "codex", "session": SESSION, "surface": SURFACE,
                    "workspace": WORKSPACE, "started_at": 100.0,
                    "cutoff": 80.0, "label": "fixture-owner"}
        self.current = {"harness": "codex", "session": SESSION, "surface": SURFACE,
                        "workspace": WORKSPACE, "started_at": 200.0,
                        "cutoff": 80.0, "label": "fixture-owner"}
        hook = {"sessions": {SESSION: {"surfaceId": SURFACE, "workspaceId": WORKSPACE,
                                       "startedAt": 200.0, "cwd": str(self.worktree),
                                       "agentLifecycle": "idle"}}}
        hook_path = self.hailing / "codex-hook-sessions.json"
        hf.atomic_json(hook_path, hook)
        hook_path.chmod(0o600)
        config = {"version": 1, "targets": [self.old], "coordinator": None,
                  "remind_after": 300, "escalate_after": 900, "cooldown": 600, "interval": 30}
        hf.atomic_json(self.hailing / "config.json", config)
        self.cmux = FixtureCmux(self.hailing)
        self.app = dw.Watch(self.watch_state, self.hailing, root / "notify", "fixture-cmux",
                            clock=lambda: self.now, cmux_factory=lambda: self.cmux)
        self._seed_hailing()
        self._seed_watch()
        self.due_timer, self.uncertain_timer = self._seed_timers()
        self.env = {"CMUX_AGENT_LAUNCH_KIND": "codex", "CODEX_THREAD_ID": SESSION,
                    "CMUX_SURFACE_ID": SURFACE, "CMUX_WORKSPACE_ID": WORKSPACE}
        environment = mock.patch.dict(os.environ, self.env)
        environment.start()
        self.addCleanup(environment.stop)

    def _seed_hailing(self):
        monitor = hf.Monitor(self.hailing, cmux=self.cmux, clock=lambda: self.now)
        with contextlib.closing(monitor.database()) as db:
            db.execute("INSERT INTO attempts VALUES(?,?,?,?,?)",
                       (SESSION, "fixture-message", "prompt", self.now - 20, "uncertain"))
            db.execute("INSERT INTO acknowledgements VALUES(?,?,?,?)",
                       (SESSION, "fixture-message", self.now - 10, "explicit"))
            db.execute("INSERT INTO attention_intents VALUES(?,?,?,?,?,?,?,?)",
                       (SESSION, 100.0, "fixture-event", "a" * 64, "uncertain", 0,
                        self.now - 5, "synthetic uncertain attempt"))
            db.commit()

    def _seed_watch(self):
        with contextlib.closing(self.app.database()) as db:
            owner_json = json.dumps(self.app.binding_snapshot(self.old), separators=(",", ":"))
            db.execute("INSERT INTO threads(thread_id,owner_json,cursor,initial_cutoff,is_open,registered_at,"
                       "updated_at,last_poll_at,next_poll_at,catchup_before,catchup_max,last_error_json,"
                       "remote_json,owner_generation,subscription_state,last_activity_at,remote_closed) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (THREAD, owner_json, "140", "120", 1, self.now - 500, self.now - 30,
                        self.now - 20, self.now + 5, "160", "180", None,
                        json.dumps({"id": THREAD, "type": 11}), 1, "active", self.now - 25, 0))
            db.execute("INSERT INTO messages(thread_id,message_id,author_id,author_name,body,attachments_json,"
                       "created_at,state,wake_attempts,last_wake_at,submitted_at,received_at,last_error_json,"
                       "groupable,ingested_at,owner_generation,group_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (THREAD, "151", "500", "Fixture", "pending fixture body", "[]", "2026-11-13T12:00:00Z",
                        "pending", 0, None, None, None, None, 1, self.now, 1, None))
            db.execute("INSERT INTO messages(thread_id,message_id,author_id,author_name,body,attachments_json,"
                       "created_at,state,wake_attempts,last_wake_at,submitted_at,received_at,last_error_json,"
                       "groupable,ingested_at,owner_generation,group_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (THREAD, "152", "500", "Fixture", "uncertain fixture body",
                        json.dumps([{"filename": "kept.txt", "url": "https://example.invalid/kept.txt"}]),
                        "2026-11-13T12:01:00Z", "uncertain", 2, self.now - 3, None, None,
                        json.dumps({"uncertain": "preserved"}), 1, self.now - 3, 1, 1))
            db.execute("INSERT INTO delivery_groups(thread_id,owner_generation,owner_json,status,created_at,"
                       "first_message_id,last_message_id,attempt_count,last_wake_at,submitted_at,last_error_json,legacy) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (THREAD, 1, owner_json, "uncertain", self.now - 4, "152", "152", 1,
                        self.now - 3, None, json.dumps({"uncertain": "preserved"}), 0))
            db.execute("INSERT INTO delivery_group_members(group_id,thread_id,message_id,ordinal) VALUES(1,?,?,0)",
                       (THREAD, "152"))
            db.execute("INSERT INTO delivery_group_attempts(group_id,state,created_at,transport_started_at,"
                       "submitted_at,last_error_json,target_owner_json,target_owner_generation) "
                       "VALUES(1,'uncertain',?,?,?,?,?,1)",
                       (self.now - 4, self.now - 3, None, json.dumps({"uncertain": "preserved"}), owner_json))
            db.commit()

    def _seed_timers(self):
        monitor = hf.Monitor(self.hailing, cmux=self.cmux, clock=lambda: self.now)
        beacon = tb.TemporalBeacon(monitor, clock=lambda: self.now)
        due_id, uncertain_id = str(uuid.uuid4()), str(uuid.uuid4())
        owner_key = tb.TemporalBeacon.owner_key(self.old, str(self.worktree))
        with contextlib.closing(beacon.database()) as db:
            for timer_id, due_at, request_id in ((due_id, self.now - 1, "due-request"),
                                                 (uncertain_id, self.now + 100, "uncertain-request")):
                db.execute("INSERT INTO timers(id,owner_key,harness,session,surface,workspace,started_at,"
                           "canonical_worktree,continuation,schedule,interval,due_at,until_at,status,request_id,"
                           "replacement_key,created_at,updated_at,schedule_spec) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (timer_id, owner_key, "codex", SESSION, SURFACE, WORKSPACE, 100.0,
                            str(self.worktree), "private continuation text", "after", None, due_at,
                            None, "active", request_id, None, self.now - 50, self.now - 50,
                            json.dumps({"schedule": "after", "value": 60, "continuation": "private"})))
            occurrence_id = str(uuid.uuid4())
            db.execute("INSERT INTO timer_occurrences(id,timer_id,sequence,due_at,status,continuation,"
                       "attempted_at,received_at,detail) VALUES(?,?,?,?,?,?,?,?,?)",
                       (occurrence_id, uncertain_id, 1, self.now - 10, "uncertain",
                        "uncertain private continuation", self.now - 5, None, "preserve uncertain"))
            db.commit()
        return due_id, uncertain_id

    def _namespace(self, action, **values):
        defaults = {"adoption_action": action, "operation_id": OPERATION,
                    "thread": [THREAD], "timer_id": [self.due_timer, self.uncertain_timer],
                    "approval_reference": "private-fixture-approval-reference",
                    "plan_digest": None}
        defaults.update(values)
        return type("Args", (), defaults)()

    def _preview(self):
        return adoption.run(self.app, self._namespace("preview"))

    def _prepare_and_accept(self):
        preview = self._preview()
        prepared = adoption.run(self.app, self._namespace("prepare", plan_digest=preview["plan_digest"]))
        accepted = adoption.run(self.app, self._namespace("accept", plan_digest=preview["plan_digest"]))
        return preview, prepared, accepted

    def test_preview_is_read_only_and_summarizes_without_private_payloads(self):
        config = (self.hailing / "config.json").read_bytes()
        receipt_db = (self.hailing / "receipts.sqlite3").read_bytes()
        watch_db = (self.watch_state / "discord-watch.sqlite3").read_bytes()
        preview = self._preview()
        self.assertTrue(preview["dry_run"])
        self.assertFalse(preview["writes"])
        self.assertEqual(preview["continuity"], "not_established")
        self.assertEqual(preview["old_owner_tuple"]["started_at"], 100.0)
        self.assertEqual(preview["current_owner_tuple"]["started_at"], 200.0)
        uncertain_summary = next(row for row in preview["resource_summaries"]["temporal_timers"]
                                 if row["timer_id"] == self.uncertain_timer)
        self.assertEqual(uncertain_summary["uncertain_occurrences"], 1)
        rendered = json.dumps(preview)
        self.assertNotIn("private continuation text", rendered)
        self.assertNotIn("uncertain fixture body", rendered)
        self.assertEqual((self.hailing / "config.json").read_bytes(), config)
        self.assertEqual((self.hailing / "receipts.sqlite3").read_bytes(), receipt_db)
        self.assertEqual((self.watch_state / "discord-watch.sqlite3").read_bytes(), watch_db)

    def test_missing_approval_wrong_owner_and_stale_plan_refuse(self):
        with self.assertRaisesRegex(ValueError, "Approval reference is required"):
            adoption.run(self.app, self._namespace("preview", approval_reference=None))
        with self.assertRaisesRegex(ValueError, "at most 24"):
            adoption._normalize_inputs(OPERATION, [str(value) for value in range(1, 26)], [], "fixture")
        preview = self._preview()
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "22222222-2222-4222-8222-222222222222".upper()}):
            with self.assertRaisesRegex(ValueError, "no unique Hailing Frequencies registration"):
                adoption.run(self.app, self._namespace("prepare", plan_digest=preview["plan_digest"]))
        with contextlib.closing(self.app.database(create=False)) as db:
            db.execute("UPDATE messages SET body='changed after preview' WHERE thread_id=? AND message_id='151'",
                       (THREAD,))
            db.commit()
        with self.assertRaisesRegex(ValueError, "Exact custody adoption plan digest is required"):
            adoption.run(self.app, self._namespace("prepare", plan_digest=preview["plan_digest"]))

    def test_post_accept_snapshot_change_refuses_before_holding_resources(self):
        preview, _prepared, _accepted = self._prepare_and_accept()
        with contextlib.closing(self.app.database(create=False)) as db:
            db.execute("UPDATE messages SET body='changed after acceptance' "
                       "WHERE thread_id=? AND message_id='151'", (THREAD,))
            db.commit()
        with self.assertRaisesRegex(ValueError, "Selected state changed after acceptance"):
            adoption.run(self.app, self._namespace("apply", plan_digest=preview["plan_digest"]))
        status = adoption.run(self.app, self._namespace("status", plan_digest=preview["plan_digest"]))
        self.assertEqual(status["state"], "accepted")
        with contextlib.closing(hf.Monitor(self.hailing).database()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM custody_adoption_holds "
                                        "WHERE operation_id=?", (OPERATION,)).fetchone()[0], 0)
        with contextlib.closing(self.app.database(create=False)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM custody_adoption_holds "
                                        "WHERE operation_id=?", (OPERATION,)).fetchone()[0], 0)

    def test_competing_central_hold_refuses_without_a_partial_local_hold(self):
        preview, _prepared, _accepted = self._prepare_and_accept()
        other_operation = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        other_digest = "b" * 64
        with contextlib.closing(hf.Monitor(self.hailing).database()) as db:
            db.execute("INSERT INTO custody_adoption_operations(operation_id,plan_json,plan_digest,"
                       "approval_reference,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (other_operation, "{}", other_digest, "synthetic competing operation",
                        "applying", self.now, self.now))
            db.execute("INSERT INTO custody_adoption_holds(operation_id,resource_kind,resource_id,plan_digest) "
                       "VALUES(?,?,?,?)", (other_operation, "hailing_session", SESSION, other_digest))
            db.commit()
        with self.assertRaisesRegex(ValueError, "hailing_session resource has a competing custody hold"):
            adoption.run(self.app, self._namespace("apply", plan_digest=preview["plan_digest"]))
        status = adoption.run(self.app, self._namespace("status", plan_digest=preview["plan_digest"]))
        self.assertEqual(status["state"], "accepted")
        with contextlib.closing(self.app.database(create=False)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM custody_adoption_holds "
                                        "WHERE operation_id=?", (OPERATION,)).fetchone()[0], 0)

    def test_installer_packages_the_adoption_and_temporal_sources(self):
        spec = importlib.util.spec_from_file_location("discord_watch_install", APP_DIR / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        root = Path(self.temp.name) / "install-fixture"

        def provisioner(vendor, _lock_file):
            package = Path(vendor) / "websocket"
            package.mkdir()
            (package / "__init__.py").write_text("__version__ = '1.9.2'\n", encoding="utf-8")

        result = installer.install(root / "runtime", root / "bin", provisioner=provisioner)
        runtime = Path(result["runtime"])
        self.assertTrue((runtime / "custody_adoption.py").is_file())
        self.assertTrue((runtime / "temporal_beacon.py").is_file())
        integrity = json.loads((runtime / "runtime-integrity.json").read_text())
        self.assertIn("custody_adoption.py", integrity["files"])
        self.assertIn("temporal_beacon.py", integrity["files"])

    def test_partial_apply_holds_and_resumes_same_operation_without_replaying_uncertain_work(self):
        preview, prepared, accepted = self._prepare_and_accept()
        self.assertEqual(prepared["state"], "prepared")
        self.assertEqual(accepted["state"], "accepted")
        with mock.patch.object(adoption, "_apply_discord", side_effect=RuntimeError("fixture interruption")):
            with self.assertRaisesRegex(RuntimeError, "fixture interruption"):
                adoption.run(self.app, self._namespace("apply", plan_digest=preview["plan_digest"]))
        status = adoption.run(self.app, self._namespace("status", plan_digest=preview["plan_digest"]))
        self.assertEqual(status["state"], "applying")
        with contextlib.closing(self.app.database(create=False)) as db:
            hold = self.app._custody_adoption_hold(db, THREAD)
            self.assertEqual(hold["state"], "applying")
            rows = db.execute("SELECT state,wake_attempts,body FROM messages WHERE thread_id=? ORDER BY message_id",
                              (THREAD,)).fetchall()
            self.assertEqual(rows[1], ("uncertain", 2, "uncertain fixture body"))
        monitor = hf.Monitor(self.hailing, self.cmux, clock=lambda: self.now)
        beacon = tb.TemporalBeacon(monitor, clock=lambda: self.now)
        during = beacon.tick(send=True)
        self.assertTrue(any(row.get("status") == "custody_adoption_hold" for row in during["timers"]))
        self.assertEqual(self.cmux.submissions, [])
        with contextlib.closing(beacon.database()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM timer_occurrences WHERE timer_id=?",
                                        (self.due_timer,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM timer_occurrences WHERE timer_id=?",
                                        (self.uncertain_timer,)).fetchone()[0], "uncertain")
        output = io.StringIO()
        with self.assertRaisesRegex(ValueError, "held by an explicit custody adoption"):
            self.app.receive(THREAD, output=output, group_id=1)
        self.assertEqual(output.getvalue(), "")
        self.app = dw.Watch(self.watch_state, self.hailing, Path(self.temp.name) / "notify",
                            "fixture-cmux", clock=lambda: self.now,
                            cmux_factory=lambda: self.cmux)
        result = adoption.run(self.app, self._namespace("apply", plan_digest=preview["plan_digest"]))
        self.assertEqual(result["state"], "complete")
        duplicate = adoption.run(self.app, self._namespace("apply", plan_digest=preview["plan_digest"]))
        self.assertTrue(duplicate["idempotent"])
        with contextlib.closing(self.app.database(create=False)) as db:
            thread = self.app._thread(db, THREAD)
            self.assertEqual(thread["owner"], self.app.binding_snapshot(self.current))
            self.assertEqual(thread["owner_generation"], 2)
            self.assertTrue(self.app._generation_chain_valid(db, thread, 1,
                            json.loads(db.execute("SELECT owner_json FROM delivery_groups WHERE id=1").fetchone()[0])))
            rows = db.execute("SELECT state,wake_attempts FROM messages WHERE thread_id=? ORDER BY message_id",
                              (THREAD,)).fetchall()
            self.assertEqual(rows, [("pending", 0), ("uncertain", 2)])
            events = db.execute("SELECT event_type,continuity,from_worktree,to_worktree "
                                "FROM ownership_events WHERE thread_id=?", (THREAD,)).fetchall()
            self.assertEqual(events, [("custody_adoption", "not_established",
                                       "not_recorded_by_hf_or_dw", str(self.worktree))])
            self.assertIsNone(self.app._custody_adoption_hold(db, THREAD))
            wake = self.app._wake_one(db, thread, self.now)
            self.assertEqual((wake["state"], wake["would_wake"]), ("uncertain", False))
            self.assertEqual(self.cmux.submissions, [])
        after = beacon.tick(send=True)
        self.assertTrue(any(row.get("timer_id") == self.due_timer for row in after["timers"]))
        self.assertEqual(len(self.cmux.submissions), 1)
        with contextlib.closing(beacon.database()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM timer_occurrences WHERE timer_id=?",
                                        (self.due_timer,)).fetchone()[0], 1, after)
            self.assertEqual(db.execute("SELECT status FROM timer_occurrences WHERE timer_id=?",
                                        (self.uncertain_timer,)).fetchone()[0], "uncertain")
            hailing_target = json.loads((self.hailing / "config.json").read_text())["targets"][0]
            self.assertEqual(hailing_target["started_at"], 200.0)
            self.assertEqual(hailing_target["cutoff"], 80.0)


if __name__ == "__main__":
    unittest.main()
