"""Exercise session isolation and blocking alarms through the installed CLI/hook path."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from test_checkpoints import NOTE
from _test_support import isolate_state_home


SCRIPT = Path(__file__).with_name("pattern_buffer.py")


class SessionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.state_home = isolate_state_home(self)
        self.root = self.home / "repo"
        self.root.mkdir()
        self.session = str(uuid.uuid4())
        self.other = str(uuid.uuid4())
        self.env = {**os.environ, "HOME": str(self.home), "XDG_STATE_HOME": str(self.state_home),
                    "CODEX_THREAD_ID": self.session, "CMUX_AGENT_LAUNCH_KIND": "codex",
                    "CMUX_SURFACE_ID": str(uuid.uuid4())}
        for command in (("init", "-q"), ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                         "commit", "--allow-empty", "-qm", "Fixture")):
            subprocess.run(["git", "-C", str(self.root), *command], check=True, capture_output=True)
        self.cli("init")
        self.default = self.root / ".context-injector/context.md"
        self.default.write_text(self.context("DEFAULT_MISSION"))
        self.alpha = self.home / "alpha.md"
        self.alpha.write_text(self.context("ALPHA_MISSION"))
        self.beta = self.home / "beta.md"
        self.beta.write_text(self.context("BETA_MISSION"))

    def context(self, mission):
        return f"# shared\n- Mission: {mission}\n# implementer\n- Implement only assigned work.\n# orchestrator\n- Preserve human holds.\n"

    def cli(self, *args, payload=None, success=True, env=None):
        result = subprocess.run([sys.executable, str(SCRIPT), "--cwd", str(self.root), *args],
                                env={**self.env, **(env or {})}, capture_output=True, text=True,
                                input=json.dumps(payload) if payload else None)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def enroll(self, session=None, source=None):
        return json.loads(self.cli("session-enroll", "--harness", "codex", "--session", session or self.session,
                                   "--context-file", str(source or self.alpha)).stdout)

    def hook(self, session=None, event="SessionStart"):
        session = session or self.session
        result = self.cli("hook", "--harness", "codex", payload={
            "hook_event_name": event, "source": "compact", "cwd": str(self.root), "session_id": session})
        return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_two_sessions_receive_distinct_missions_without_mutating_default(self):
        original = self.default.read_bytes()
        first = self.enroll()
        second = self.enroll(self.other, self.beta)
        self.assertNotEqual(first["enrollment_id"], second["enrollment_id"])
        self.assertIn(self.session, first["path"])
        self.assertIn("ALPHA_MISSION", self.hook())
        self.assertNotIn("BETA_MISSION", self.hook())
        self.assertIn("BETA_MISSION", self.hook(self.other))
        self.assertNotIn("ALPHA_MISSION", self.hook(self.other))
        self.assertEqual(self.default.read_bytes(), original)
        ignored = subprocess.run(["git", "-C", str(self.root), "check-ignore", "-q", first["path"]])
        self.assertEqual(ignored.returncode, 0)

    def test_required_file_missing_alarms_once_and_recovers_without_fallback(self):
        binding = self.enroll()
        path = Path(binding["path"])
        text = path.read_text()
        launcher = self.home / ".local/bin/hailing-frequencies"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!" + sys.executable + "\n" + '''import json, sys, os
from pathlib import Path
with (Path(os.environ["HOME"])/"attention.jsonl").open("a") as f: f.write(json.dumps(sys.argv)+"\\n")
print(json.dumps({"event_key":sys.argv[-1],"outcome":"submitted","attempted":True,"detail":"fixture"}))
''')
        launcher.chmod(0o700)
        path.unlink()
        for event in ("SessionStart", "PostToolUse", "UserPromptSubmit"):
            alarm = self.hook(event=event)
            self.assertIn("STOP:", alarm)
            self.assertIn(str(path), alarm)
            self.assertNotIn("DEFAULT_MISSION", alarm)
        self.assertEqual(len((self.home / "attention.jsonl").read_text().splitlines()), 1)
        path.write_text(text)
        self.assertIn("ALPHA_MISSION", self.hook())
        report = json.loads(self.cli("reliability", "--since", "7d", "--json").stdout)
        self.assertEqual(report["summary"]["by_event"]["context.missing_alarm"], 1)
        self.assertEqual(report["summary"]["by_event"]["context.recovered"], 1)

    def test_invalid_or_oversized_required_context_never_uses_default(self):
        binding = self.enroll()
        for text in ("# unknown\nwrong\n", self.context("X" * 9000)):
            Path(binding["path"]).write_text(text)
            alarm = self.hook()
            self.assertIn("STOP:", alarm)
            self.assertNotIn("DEFAULT_MISSION", alarm)

    def test_explicit_reviewed_update_repairs_changed_file_without_manual_metadata_edits(self):
        binding = self.enroll()
        Path(binding["path"]).write_text("# invalid\nDamaged published file.\n")
        self.assertIn("STOP:", self.hook())
        repaired = json.loads(self.cli("session-enroll", "--harness", "codex", "--session", self.session,
                                       "--context-file", str(self.alpha), "--reviewed-update",
                                       "--evidence", "approved-context-restoration").stdout)
        self.assertEqual(repaired["enrollment_id"], binding["enrollment_id"])
        self.assertIn("ALPHA_MISSION", self.hook())

    def test_explicit_handoff_retires_old_binding_and_preserves_enrollment(self):
        binding = self.enroll()
        self.cli("enrollment-release", "--harness", "codex", "--session", self.session,
                 "--enrollment-id", binding["enrollment_id"], "--next-harness", "codex",
                 "--next-session", self.other, "--evidence", "approved-test-handoff")
        self.assertIn("STOP:", self.hook())
        successor_alarm = self.hook(self.other)
        self.assertIn("STOP:", successor_alarm)
        self.assertIn("named pending successor of an approved release", successor_alarm)
        self.assertIn("enrollment-adopt", successor_alarm)
        self.assertIn("required fresh checkpoint setup", successor_alarm)
        self.assertIn("Do no product work until validation succeeds", successor_alarm)
        self.assertIn("notify the context owner", successor_alarm)
        self.assertNotIn("DEFAULT_MISSION", successor_alarm)
        note = self.home / "fresh-checkpoint.md"
        note.write_text(NOTE)
        adopted = json.loads(self.cli("enrollment-adopt", "--harness", "codex", "--session", self.other,
                                     "--enrollment-id", binding["enrollment_id"], "--context-file", str(self.beta),
                                     "--checkpoint-file", str(note), "--evidence", "reviewed-successor",
                                     env={"CODEX_THREAD_ID": self.other}).stdout)
        self.assertEqual(adopted["enrollment"]["enrollment_id"], binding["enrollment_id"])
        report = json.loads(self.cli("reliability", "--since", "7d", "--json").stdout)
        adoption_events = [event for event in report["events"] if event["event"] == "enrollment.enrollment-adopt"]
        self.assertEqual(len(adoption_events), 1)
        self.assertEqual(adoption_events[0]["session"], self.other)
        self.assertEqual(adoption_events[0]["enrollment_id"], binding["enrollment_id"])
        self.assertIn("BETA_MISSION", self.hook(self.other))
        self.assertIn("STOP:", self.hook())

    def test_unenrolled_session_keeps_default_and_failed_update_preserves_good_file(self):
        self.assertIn("DEFAULT_MISSION", self.hook(self.other))
        binding = self.enroll()
        old = Path(binding["path"]).read_bytes()
        invalid = self.home / "invalid.md"
        invalid.write_text("# invalid\n")
        self.cli("session-enroll", "--harness", "codex", "--session", self.session,
                 "--context-file", str(invalid), "--reviewed-update", success=False)
        self.assertEqual(Path(binding["path"]).read_bytes(), old)
        self.assertIn("ALPHA_MISSION", self.hook())

    def test_reliability_reads_only_owned_idle_receipts_without_continuation_bodies(self):
        self.enroll()
        self.hook()
        database = self.home / ".local/state/starfleet-ai/hailing-frequencies/receipts.sqlite3"
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as db:
            db.executescript("CREATE TABLE timers(id,canonical_worktree,replacement_key,harness,session);"
                             "CREATE TABLE timer_occurrences(id,timer_id,status,attempted_at,received_at,due_at);")
            for name, root, key in (("owned", str(self.root), "pattern-buffer-checkpoint-idle"),
                                    ("foreign", str(self.home / "other"), "pattern-buffer-checkpoint-idle"),
                                    ("manual", str(self.root), "unrelated-manual-timer")):
                db.execute("INSERT INTO timers VALUES(?,?,?,?,?)", (name, root, key, "codex", self.session))
                db.execute("INSERT INTO timer_occurrences VALUES(?,?,?,?,?,?)",
                           (name + "-receipt", name, "received", time.time() - 1, time.time(), time.time() - 2))
        before = database.read_bytes()
        report = json.loads(self.cli("reliability", "--json").stdout)
        receipts = report["idle_timer_receipts"]
        self.assertEqual(receipts["counts"], {"received": 1})
        self.assertEqual([row["timer_id"] for row in receipts["receipts"]], ["owned"])
        self.assertNotIn("continuation", json.dumps(receipts))
        self.assertEqual(database.read_bytes(), before)

    def test_dispatch_does_not_substitute_default_for_isolated_parent_mission(self):
        self.enroll()
        child = self.home / "child"
        subprocess.run(["git", "-C", str(self.root), "worktree", "add", "-b", "worker", str(child)],
                       check=True, capture_output=True)
        task = self.home / "task.md"
        task.write_text("Bounded child task.")
        result = subprocess.run([sys.executable, str(SCRIPT), "--cwd", str(child), "dispatch",
                                 "--owner-worktree", str(self.root), "--harness", "codex",
                                 "--task-file", str(task), "--prepare-only"],
                                env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot substitute a worktree default", result.stderr)
        gitdir = Path(subprocess.check_output(["git", "-C", str(child), "rev-parse", "--absolute-git-dir"],
                                             text=True).strip())
        self.assertFalse((gitdir / "context-injector/config.json").exists())


if __name__ == "__main__":
    unittest.main()
