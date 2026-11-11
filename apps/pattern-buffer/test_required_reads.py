"""Focused required-read CLI and hook controls with isolated state."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pattern_buffer as ci
import pattern_buffer_reliability as reliability
from _test_support import isolate_state_home


SCRIPT = Path(__file__).with_name("pattern_buffer.py")
OWNER_SESSION_VARIABLES = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID")


class RequiredReadsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pattern-buffer-required-reads-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.home_patch = mock.patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.state_home = isolate_state_home(self)
        self.root = self.home / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--allow-empty", "-qm", "Fixture")
        self.session = "codex-required-reads-session"
        self.env = {**os.environ, "HOME": str(self.home), "XDG_STATE_HOME": str(self.state_home)}
        for name in OWNER_SESSION_VARIABLES:
            self.env.pop(name, None)
        self.cli("init", "--default-role", "orchestrator")
        self.gitdir = Path(self.git("rev-parse", "--absolute-git-dir").stdout.strip())
        self.helper = ci.auxiliary("required_reads")
        self.manifest_path = self.home / "required-reads.json"
        self.document = self.home / "engineering-guide.md"
        self.document.write_text("# Engineering guide\nRead this complete fixture.\n", encoding="utf-8")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True, env=self.env if hasattr(self, "env") else None)

    def cli(self, *args, payload=None, success=True, env=None):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--cwd", str(self.root), *args],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            env={**self.env, **(env or {})},
        )
        expected = 0 if success else 1
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def register(self, entries=None):
        values = entries if entries is not None else [{
            "name": "engineering-guide",
            "path": str(self.document),
            "roles": ["orchestrator"],
        }]
        self.manifest_path.write_text(
            json.dumps({"schema_version": 1, "entries": values}), encoding="utf-8")
        with mock.patch.object(self.helper, "_require_owner_terminal", return_value=None):
            return self.helper.set_list(
                ci, self.root, self.gitdir, str(self.manifest_path),
                confirm=lambda _prompt: self.root.name)

    def hook(self, event, *, source="startup", tool="Edit", command=None, session=None, env=None):
        payload = {
            "hook_event_name": event,
            "source": source,
            "cwd": str(self.root),
            "session_id": session or self.session,
            "tool_name": tool,
            "tool_input": {"command": command} if command is not None else {},
        }
        return self.cli("hook", "--harness", "codex", payload=payload, env=env)

    def start(self, source="startup"):
        return self.hook("SessionStart", source=source)

    def gate_edit(self):
        result = self.hook("PreToolUse", tool="Edit")
        return json.loads(result.stdout) if result.stdout else None

    def read(self, *, name="engineering-guide", part=None, session=None, env=None, success=True):
        arguments = ["read-required", "--harness", "codex", "--session", session or self.session,
                     "--name", name]
        if part is not None:
            arguments.extend(("--part", str(part)))
        return self.cli(*arguments, success=success, env=env)

    def status(self, session=None):
        result = self.cli("required-reads", "status", "--harness", "codex",
                          "--session", session or self.session)
        return json.loads(result.stdout)

    def test_no_registered_list_keeps_gate_inert_and_silent(self):
        result = self.hook("PreToolUse", tool="Edit")
        self.assertEqual(result.stdout, "")
        status = json.loads(self.cli("status", "--harness", "codex", "--session", self.session).stdout)
        self.assertNotIn("required_reads", status)

    def test_no_list_status_does_not_require_native_session_identity(self):
        cases = (
            ("codex", {"CODEX_THREAD_ID": "different-native-session"}),
            ("claude", {"CLAUDE_CODE_SESSION_ID": "different-native-session"}),
            ("claude", {"CODEX_THREAD_ID": "different-native-session"}),
        )
        for harness, env in cases:
            with self.subTest(harness=harness, env=env):
                result = self.cli("status", "--harness", harness, "--session", "synthetic-status-session",
                                  env=env)
                report = json.loads(result.stdout)
                self.assertNotIn("required_reads", report)

    def test_registered_list_status_still_refuses_native_session_mismatch(self):
        self.register()
        refusal = self.cli("status", "--harness", "codex", "--session", self.session,
                           success=False, env={"CODEX_THREAD_ID": "different-native-session"})
        self.assertIn("conflicts with the current native harness session", refusal.stderr)

    def test_pending_gate_allows_guide_but_denies_work_without_receipt(self):
        self.register()
        self.start("startup")
        before = self.status()
        self.assertEqual(before["pending_read_count"], 1)
        self.assertEqual(before["entries"][0]["state"], "pending")

        command = "pattern-buffer guide --print"
        allowed = self.hook("PreToolUse", tool="Bash", command=command)
        self.assertEqual(allowed.stdout, "")
        printed = self.cli("guide", "--print")
        self.assertIn("## Required reads", printed.stdout)

        after_guide = self.status()
        self.assertEqual(after_guide["pending_read_count"], 1)
        self.assertEqual(after_guide["entries"][0]["state"], "pending")
        denied = self.gate_edit()
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        after_denial = self.status()
        self.assertEqual(after_denial["entries"][0]["state"], "pending")

    def test_receipt_opens_current_generation_and_compaction_resets_it(self):
        self.register()
        startup = self.start("startup")
        self.assertIn("Required reads", startup.stdout)
        denied = self.gate_edit()
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

        self.read()
        opened = self.status()
        self.assertTrue(opened["gate_open"])
        self.assertEqual(opened["pending_read_count"], 0)
        self.assertIsNone(self.gate_edit())

        compact = self.start("compact")
        reset = self.status()
        self.assertIn("Required reads", compact.stdout)
        self.assertEqual(reset["generation"], opened["generation"] + 1)
        self.assertEqual(reset["entries"][0]["state"], "pending")
        denied_again = self.gate_edit()
        self.assertEqual(denied_again["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_resume_keeps_receipts_while_clear_and_fork_reset_generation(self):
        self.register()
        self.start("startup")
        self.read()
        before_resume = self.status()

        self.start("resume")
        resumed = self.status()
        self.assertEqual(resumed["generation"], before_resume["generation"])
        self.assertTrue(resumed["gate_open"])
        self.assertIsNone(self.gate_edit())

        self.start("clear")
        after_clear = self.status()
        self.assertEqual(after_clear["generation"], resumed["generation"] + 1)
        self.assertEqual(after_clear["entries"][0]["state"], "pending")
        self.assertEqual(self.gate_edit()["hookSpecificOutput"]["permissionDecision"], "deny")

        self.start("fork")
        after_fork = self.status()
        self.assertEqual(after_fork["generation"], after_clear["generation"] + 1)
        self.assertEqual(after_fork["entries"][0]["state"], "pending")
        self.assertEqual(self.gate_edit()["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_changed_and_missing_documents_stay_closed_while_diagnostics_work(self):
        self.register()
        self.start("startup")
        self.read()
        self.document.write_text("# Changed guide\n", encoding="utf-8")
        stale = self.status()
        self.assertEqual(stale["entries"][0]["state"], "stale")
        self.assertEqual(self.gate_edit()["hookSpecificOutput"]["permissionDecision"], "deny")

        self.document.unlink()
        missing = self.status()
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["entries"][0]["state"], "integrity_error")
        self.assertEqual(self.gate_edit()["hookSpecificOutput"]["permissionDecision"], "deny")

        root_status = json.loads(self.cli("status", "--harness", "codex",
                                          "--session", self.session).stdout)
        self.assertFalse(root_status["required_reads"]["ok"])
        settings = self.home / "codex-settings.json"
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [{
            "type": "command", "command": f"{SCRIPT} {ci.marker('codex')}"
        }]}]}}), encoding="utf-8")
        doctor = self.cli("doctor", "--harness", "codex", "--session", self.session,
                          "--settings", str(settings), success=False)
        self.assertIn("Required reads list", doctor.stdout)
        self.assertIn("PreToolUse hook configured for codex: 1", doctor.stdout)
        self.assertIn("native activation and delivery remain unverified", doctor.stdout)

        diagnostics = (
            shlex.join(["pattern-buffer", "--cwd", str(self.root), "status", "--harness", "codex",
                        "--session", self.session]),
            "pattern-buffer guide --print",
            shlex.join(["pattern-buffer", "--cwd", str(self.root), "required-reads", "status",
                        "--harness", "codex", "--session", self.session]),
        )
        for command in diagnostics:
            with self.subTest(command=command):
                self.assertEqual(self.hook("PreToolUse", tool="Bash", command=command).stdout, "")
        self.assertIn("## Required reads", self.cli("guide", "--print").stdout)

    def test_owner_registration_refuses_a_live_harness_session(self):
        self.manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "entries": [{"name": "engineering-guide", "path": str(self.document),
                         "roles": ["orchestrator"]}],
        }), encoding="utf-8")
        refusal = self.cli("required-reads", "set", "--file", str(self.manifest_path),
                           success=False, env={"CODEX_THREAD_ID": self.session})
        self.assertIn("only the worktree owner", refusal.stderr)
        self.assertFalse(self.helper.registration_path(self.gitdir).exists())

    def test_receipt_refuses_an_explicit_session_that_conflicts_with_native_identity(self):
        self.register()
        refusal = self.read(session="different-session", success=False,
                            env={"CODEX_THREAD_ID": self.session})
        self.assertIn("conflicts with the current native harness session", refusal.stderr)
        self.assertEqual(self.status()["entries"][0]["state"], "pending")

    def test_override_accepts_both_duration_bounds_and_resets_with_generation(self):
        self.register()
        self.start("startup")
        for minutes in (0, 61):
            with self.subTest(minutes=minutes):
                with mock.patch.object(self.helper, "_require_owner_terminal", return_value=None):
                    with self.assertRaisesRegex(ValueError, "whole number from 1 to 60"):
                        self.helper.override(
                            ci, self.root, self.gitdir, "codex", self.session, "Fixture override", minutes,
                            confirm=lambda _prompt: self.session)

        for minutes in (1, 60):
            with self.subTest(minutes=minutes):
                with mock.patch.object(self.helper, "_require_owner_terminal", return_value=None):
                    override = self.helper.override(
                        ci, self.root, self.gitdir, "codex", self.session,
                        f"Fixture override {minutes}", minutes,
                        confirm=lambda _prompt: self.session)
                self.assertTrue(override["override"]["active"])
                self.assertEqual(override["override"]["reason"], f"Fixture override {minutes}")
                status = self.status()
                self.assertTrue(status["override"]["active"])
                self.assertIsNone(self.gate_edit()["hookSpecificOutput"].get("permissionDecision"))

        before_compact = self.status()
        self.start("compact")
        after_compact = self.status()
        self.assertEqual(after_compact["generation"], before_compact["generation"] + 1)
        self.assertIsNone(after_compact["override"])
        self.assertEqual(self.gate_edit()["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_multipart_receipts_require_each_utf8_safe_part(self):
        self.document.write_text("λ" * 5000, encoding="utf-8")
        self.register()
        self.start("startup")
        initial = self.status()
        self.assertEqual(len(initial["entries"][0]["parts"]), 2)

        self.read(part=1)
        after_first = self.status()
        self.assertEqual([part["state"] for part in after_first["entries"][0]["parts"]],
                         ["current", "pending"])
        self.assertEqual(self.gate_edit()["hookSpecificOutput"]["permissionDecision"], "deny")

        self.read(part=2)
        self.assertTrue(self.status()["gate_open"])
        self.assertIsNone(self.gate_edit())

    def test_unexpected_gate_failure_allows_once_records_event_and_returns_warning(self):
        self.register()
        payload = {"hook_event_name": "PreToolUse", "session_id": self.session,
                   "tool_name": "Edit", "tool_input": {}}
        with mock.patch.object(self.helper, "load_list", side_effect=RuntimeError("fixture failure")):
            result = self.helper.pretool_gate(ci, self.root, self.gitdir, "codex", payload)

        self.assertNotIn("permissionDecision", result["hookSpecificOutput"])
        self.assertIn("allowed this tool call", result["systemMessage"])
        report = reliability.report(self.root, since="7d")
        self.assertEqual(report["summary"]["by_event"]["required_reads.gate_error"], 1)


if __name__ == "__main__":
    unittest.main()
