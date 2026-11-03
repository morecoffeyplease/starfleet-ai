"""Exercise self-service enrollment through the installed CLI and recovery hook."""
import contextlib
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_checkpoints import NOTE

SCRIPT = Path(__file__).with_name("pattern_buffer.py")
sys.path.insert(0, str(SCRIPT.parent))
import pattern_buffer as ci
import pattern_buffer_enrollment as enrollment


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".self-enroll-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.root = self.folder / "repo"
        self.root.mkdir()
        self.env = {**os.environ, "CODEX_THREAD_ID": "", "CLAUDE_SESSION_ID": ""}
        self.git("init", "-q")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--allow-empty", "-qm", "Fixture")
        self.cli("init", "--default-role", "orchestrator")
        self.note = self.folder / "working.md"
        self.note.write_text(NOTE)
        self.settings = self.folder / "hooks.json"
        self.runtime = self.folder / "runtime"
        self.options = ["--settings", str(self.settings), "--runtime-dir", str(self.runtime)]

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              text=True, capture_output=True)

    def cli(self, *args, script=SCRIPT, env=None, ok=True, data=None):
        result = subprocess.run([sys.executable, str(script), "--cwd", str(self.root), *args],
                                env={**self.env, **(env or {})}, text=True, capture_output=True,
                                input=json.dumps(data) if data else None)
        self.assertEqual(result.returncode, 0 if ok else 1, result.stdout + result.stderr)
        return result

    def enroll(self, *args, harness="codex", **kwargs):
        return self.cli("checkpoint-enroll", "--harness", harness, "--session", "pilot",
                        *self.options, *args, **kwargs)

    def test_briefing_is_read_only_and_session_is_inherited(self):
        before = self.git("status", "--porcelain").stdout
        result = json.loads(self.cli("checkpoint-enroll", "--harness", "codex",
                                    env={"CODEX_THREAD_ID": "pilot"}).stdout)
        self.assertEqual(result["session"], "pilot")
        self.assertEqual(result["state"], "briefing")
        self.assertFalse(result["approval_required"])
        self.assertIn("full context", result["briefing"])
        self.assertIn("human holds", result["briefing"])
        self.assertIn("## Next action", result["template"])
        self.assertEqual(before, self.git("status", "--porcelain").stdout)
        self.assertFalse((self.root / ".git/context-injector/checkpoints").exists())
        self.assertFalse(self.settings.exists())

    def test_installed_enrollment_publishes_and_recovers_exact_note(self):
        self.settings.write_text(json.dumps({"keep": 17, "hooks": {"PostToolUse": [
            {"hooks": [{"type": "command", "command": "printf unrelated"}]}]}}))
        self.cli("install", "--harness", "codex", *self.options)
        report = json.loads(self.enroll("--file", str(self.note), "--compact-at", "95").stdout)
        self.assertEqual(report["state"], "prepared")
        self.assertEqual(report["hooks_loaded"], "unverified")
        self.assertFalse(report["recovery_observed"])
        self.assertTrue(report["checkpoint"]["enabled"])
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["keep"], 17)
        self.assertEqual(data["hooks"]["PostToolUse"][0]["hooks"][0]["command"], "printf unrelated")
        installed = self.runtime / "context_injector_codex.py"
        self.assertTrue(self.runtime.joinpath("context_injector_codex_enrollment.py").exists())
        before = Path(report["checkpoint"]["checkpoint_path"]).read_bytes()
        read = json.loads(self.cli("checkpoint-read", "--harness", "codex", "--session", "pilot",
                                   script=installed).stdout)
        self.assertEqual(ci.checkpoints()._note_body(read["note"]), NOTE)
        self.assertTrue(read["note"].startswith(ci.checkpoints().HEADER))
        other_input = self.folder / "invalid-input.md"
        other_input.write_text("invalid attempted overwrite")
        again = json.loads(self.enroll("--file", str(other_input), "--compact-at", "80",
                                       script=installed).stdout)
        self.assertEqual(again["state"], "already_enrolled")
        self.assertEqual(again["checkpoint"]["compact_at"], 95)
        self.assertEqual(Path(report["checkpoint"]["checkpoint_path"]).read_bytes(), before)
        trace = self.folder / "trace.jsonl"
        trace.write_text(json.dumps({"type": "session_meta", "payload": {"id": "pilot"}}) + "\n")
        hook = self.cli("hook", "--harness", "codex", script=installed, data={
            "session_id": "pilot", "cwd": str(self.root), "hook_event_name": "SessionStart",
            "source": "compact", "transcript_path": str(trace)})
        self.assertIn(str(self.note), hook.stdout)
        self.assertNotIn("working.md", self.git("status", "--porcelain").stdout)

    def test_invalid_input_or_unknown_threshold_changes_no_hooks(self):
        self.enroll("--file", str(self.note), ok=False)
        self.assertFalse(self.settings.exists())
        self.note.write_text("bad note")
        self.enroll("--file", str(self.note), "--compact-at", "95", ok=False)
        self.assertFalse(self.settings.exists())

    def test_default_codex_enrollment_reuses_stable_hooks_without_session_definitions(self):
        home = self.folder / "codex-home"
        env = {"CODEX_HOME": str(home)}
        self.cli("install", "--harness", "codex", "--runtime-dir", str(self.runtime), env=env)
        settings = home / "hooks.json"
        before = settings.read_bytes()
        report = json.loads(self.cli("checkpoint-enroll", "--harness", "codex", "--session", "pilot",
            "--compact-at", "95", "--file", str(self.note), env=env).stdout)
        self.assertEqual(settings.read_bytes(), before)
        self.assertEqual(report["pressure_source"], "selected settings file")
        self.assertEqual(report["hook_config"], {"hooks": {}})
        self.assertIsNone(report["launcher"])
        self.assertFalse(report["restart_required"])
        self.assertFalse(Path(report["checkpoint"]["checkpoint_path"]).parent.joinpath("resume.json").exists())

    def test_existing_global_pressure_source_is_reused_without_duplicate_fragments(self):
        home = self.folder / "codex-home"
        env = {"CODEX_HOME": str(home)}
        self.cli("install", "--harness", "codex", "--checkpoints", "--runtime-dir", str(self.runtime), env=env)
        settings = home / "hooks.json"
        before = settings.read_bytes()
        report = json.loads(self.cli("checkpoint-enroll", "--harness", "codex", "--session", "pilot",
            "--compact-at", "95", "--file", str(self.note), env=env).stdout)
        self.assertEqual(settings.read_bytes(), before)
        self.assertEqual(report["hook_config"], {"hooks": {}})
        self.assertEqual(report["pressure_source"], "selected settings file")

    def test_standard_hooks_are_inert_until_enrolled_and_definitions_survive_two_sessions(self):
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                settings = self.folder / (harness + "-standard.json")
                self.cli("install", "--harness", harness, "--settings", str(settings),
                         "--runtime-dir", str(self.runtime))
                before = settings.read_bytes()
                for event in ("PreCompact", "PostToolUse", "UserPromptSubmit"):
                    output = self.cli("hook", "--harness", harness, data={"session_id": "fresh",
                        "cwd": str(self.root), "hook_event_name": event}).stdout
                    self.assertEqual(output, "")
                    outside = self.cli("hook", "--harness", harness, data={"session_id": "fresh",
                        "cwd": str(self.folder), "hook_event_name": event})
                    self.assertEqual(outside.stdout, "")
                    self.assertEqual(outside.stderr, "")
                self.assertFalse(ci.checkpoints()._state_path(self.root / ".git", harness, "fresh").exists())
                for session in ("first", "second"):
                    note = self.folder / (harness + "-" + session + ".md")
                    note.write_text(NOTE)
                    report = json.loads(self.cli("checkpoint-enroll", "--harness", harness,
                        "--session", session, "--file", str(note), "--compact-at", "95",
                        "--settings", str(settings)).stdout)
                    self.assertEqual(settings.read_bytes(), before)
                    self.assertEqual(report["hook_config"], {"hooks": {}})
                    self.assertIsNone(report["launcher"])
                    self.assertFalse(report["restart_required"])
                    self.assertEqual(Path(report["checkpoint"]["draft_path"]).read_text(), NOTE)
                    self.assertEqual(next(iter(report)), "edit_this_file")
                    self.assertEqual(report["edit_this_file"], report["checkpoint"]["draft_path"])
                    self.assertEqual(report["published_copy"], report["checkpoint"]["checkpoint_path"])
                    self.assertLess(report["draft_body_limit_bytes"], 8192)

    @unittest.skipUnless(sys.version_info >= (3, 11), "native TOML adapter needs Python3.11")
    def test_legacy_migration_preserves_note_and_only_prepares_guarded_restart(self):
        self.cli("install", "--harness", "codex", *self.options)
        registered = json.loads(self.enroll("--file", str(self.note), "--compact-at", "95").stdout)
        root, gitdir = ci.checkout(self.root)
        state = ci.checkpoints()._state_path(gitdir, "codex", "pilot")
        before = state.read_bytes(), self.note.read_bytes(), self.settings.read_bytes()
        args = argparse.Namespace(harness="codex", settings=str(self.settings), runtime_dir=None)
        original = ["/native/codex", "-c", 'hooks.SessionStart=[{hooks=[{type="command",command="cmux-start"},{type="command",command="old # starfleet-context-injector:codex:v1"}]}]',
                    "-c", 'hooks.PostToolUse=[{hooks=[{type="command",command="old # starfleet-context-injector:codex:v1"}]}]',
                    "resume", "pilot", "--yolo"]
        with mock.patch.object(ci, "settings_path", return_value=self.settings), mock.patch.object(enrollment, "_native_cwd", return_value=root):
            result = enrollment._activation(ci, ci.checkpoints(), root, gitdir, args, "pilot", os.getpid(),
                original, {"model": "saved-model", "effort": "medium"}, prepare=True)
        self.assertTrue(result["legacy_native_hooks"])
        self.assertTrue(result["restart_required"])
        self.assertTrue(result["launcher"])
        plan = json.loads(state.with_name("resume.json").read_text())
        self.assertNotIn("starfleet-context-injector", " ".join(plan["argv"]))
        self.assertIn("cmux-start", " ".join(plan["argv"]))
        self.assertIn("--yolo", plan["argv"])
        self.assertEqual(before, (state.read_bytes(), self.note.read_bytes(), self.settings.read_bytes()))
        attempt = subprocess.run([sys.executable, str(state.with_name("resume.py"))], capture_output=True, text=True)
        self.assertNotEqual(attempt.returncode, 0)
        self.assertIn("Exit the original agent", attempt.stderr)

    def test_rejects_wrong_session_implementer_and_disabled_worktree(self):
        self.enroll(env={"CODEX_THREAD_ID": "someone-else"}, ok=False)
        self.cli("assign", "--harness", "codex", "--session", "pilot", "--role", "implementer")
        self.enroll(ok=False)
        self.cli("assign", "--harness", "codex", "--session", "pilot", "--role", "orchestrator")
        self.cli("disable")
        self.enroll(ok=False)
        self.assertFalse(self.settings.exists())

    def test_claimed_note_rejected_before_hook_installation(self):
        Path(str(self.note) + ".owner.json").write_text(json.dumps({"session": "other"}))
        result = self.enroll("--file", str(self.note), "--compact-at", "95", ok=False)
        self.assertIn("claimed", result.stderr)
        self.assertFalse(self.settings.exists())

    def test_claude_wraps_status_line_and_keeps_other_settings(self):
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "printf STATUS"},
                                             "env": {"UNCHANGED": "yes"}}))
        self.cli("install", "--harness", "claude", *self.options)
        before = self.settings.read_bytes()
        report = json.loads(self.enroll("--file", str(self.note), harness="claude",
                                       env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "65"}).stdout)
        self.assertEqual(report["checkpoint"]["reminder_threshold"], 60)
        self.assertIsNone(report["launcher"])
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["env"], {"UNCHANGED": "yes"})
        self.assertIn("printf STATUS", settings["statusLine"]["command"])
        self.assertEqual(self.settings.read_bytes(), before)

    @unittest.skipUnless(sys.version_info >= (3, 11), "native TOML adapter needs Python3.11")
    def test_native_restart_preserves_cmux_hooks_permissions_and_session(self):
        import tomllib
        original = ["/native/codex", "--dangerously-bypass-hook-trust", "--disable", "computer_use",
                    "-c", 'hooks.SessionStart=[{hooks=[{type="command",command="cmux-start"}]}]',
                    "-c", 'hooks.PostToolUse=[{hooks=[{type="command",command="cmux-tool",timeout=5000}]}]',
                    "resume", "pilot", "--yolo"]
        result, detail = enrollment._restart_plan(ci, "codex", "pilot", original,
            ci.settings_path("codex", None), Path("/runtime/context_injector_codex.py"),
            {"model": "saved-model", "effort": "medium"})
        self.assertIn("Prepared only", detail)
        self.assertNotIn("--dangerously-bypass-hook-trust", result)
        self.assertIn("--yolo", result)
        self.assertEqual(result[result.index("resume") + 1], "pilot")
        self.assertIn(original[5], result)
        post = next(x for x in result if x.startswith("hooks.PostToolUse="))
        handlers = tomllib.loads(post)["hooks"]["PostToolUse"]
        self.assertEqual(handlers[0]["hooks"][0]["command"], "cmux-tool")
        self.assertEqual(sum(ci.owned(h, "codex") for g in handlers for h in g["hooks"]), 1)
        self.assertEqual(original[1], "--dangerously-bypass-hook-trust")
        self.assertIsNone(enrollment._restart_plan(ci, "codex", "other", original,
            ci.settings_path("codex", None), Path("/runtime/ci.py"), {})[0])
        self.assertIsNone(enrollment._restart_plan(ci, "codex", "pilot", original,
            ci.settings_path("codex", None), Path("/runtime/ci.py"), {},
            root=self.root, native_cwd=self.folder)[0])
        disabled = [*original[:1], "--disable=hooks", *original[1:]]
        self.assertIsNone(enrollment._restart_plan(ci, "codex", "pilot", disabled,
            ci.settings_path("codex", None), Path("/runtime/ci.py"), {})[0])
        # With user-level pressure hooks, remove an old native copy instead of adding another.
        old = list(original)
        old[7] = 'hooks.PostToolUse=[{hooks=[{type="command",command="old # starfleet-context-injector:codex:v1"}]}]'
        plan, _ = enrollment._restart_plan(ci, "codex", "pilot", old,
            ci.settings_path("codex", None), Path("/runtime/ci.py"),
            {"model": "saved-model", "effort": "medium"}, native_pressure=False)
        post = next(x for x in plan if x.startswith("hooks.PostToolUse="))
        self.assertEqual(tomllib.loads(post)["hooks"]["PostToolUse"], [])

    def test_runtime_detection_does_not_guess_other_session_or_scope(self):
        import sqlite3
        home = self.folder / "codex"
        home.mkdir()
        with contextlib.closing(sqlite3.connect(home / "logs_2.sqlite")) as db, db:
            db.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY, thread_id TEXT, ts INTEGER, process_uuid TEXT, feedback_log_body TEXT)")
            body = ("model=saved-model codex.turn.reasoning_effort=medium "
                    "auto_compact_scope_limit=Some(450000) auto_compact_limit_scope=Total "
                    "full_context_window_limit=Some(475000)")
            db.execute("INSERT INTO logs VALUES(1,'pilot',100,'pid:123:x',?)", (body,))
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), mock.patch.object(enrollment, "_started_at", return_value=50):
            self.assertAlmostEqual(enrollment._codex_runtime("pilot", 123)["compact_at"], 94.7368421)
            self.assertEqual(enrollment._codex_runtime("other", 123), {})
            self.assertEqual(enrollment._codex_runtime("pilot", 456), {})
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}), mock.patch.object(enrollment, "_started_at", return_value=150):
            self.assertEqual(enrollment._codex_runtime("pilot", 123), {})


if __name__ == "__main__":
    unittest.main()
