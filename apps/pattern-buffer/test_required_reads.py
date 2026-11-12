"""Focused required-read CLI and hook controls with isolated state."""

import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import pattern_buffer as ci
import pattern_buffer_reliability as reliability
from _test_support import isolate_state_home


SCRIPT = Path(__file__).with_name("pattern_buffer.py")
OWNER_SESSION_VARIABLES = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID")
MISSING = object()


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
        self.env.pop(ci.DISPATCH_ENV, None)
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

    def hook(self, event, *, source="startup", tool="Edit", command=None, session=None,
             harness="codex", tool_use_id=None, tool_response=MISSING, env=None):
        payload = {
            "hook_event_name": event,
            "source": source,
            "cwd": str(self.root),
            "session_id": session or self.session,
            "tool_name": tool,
            "tool_input": {"command": command} if command is not None else {},
        }
        if tool_use_id is not None:
            payload["tool_use_id"] = tool_use_id
        if tool_response is not MISSING:
            payload["tool_response"] = tool_response
        return self.cli("hook", "--harness", harness, payload=payload, env=env)

    def start(self, source="startup", *, harness="codex", session=None):
        return self.hook("SessionStart", source=source, harness=harness, session=session)

    def gate_edit(self):
        result = self.hook("PreToolUse", tool="Edit")
        return json.loads(result.stdout) if result.stdout else None

    def read(self, *, name="engineering-guide", part=None, session=None, env=None, success=True):
        arguments = ["read-required", "--harness", "codex", "--session", session or self.session,
                     "--name", name]
        if part is not None:
            arguments.extend(("--part", str(part)))
        return self.cli(*arguments, success=success, env=env)

    def prepare_read(self, *, harness="codex", name="engineering-guide", part=None,
                     session=None, tool_use_id="required-read-tool-use"):
        session = session or self.session
        command = ["pattern-buffer", "--cwd", str(self.root), "read-required",
                   "--harness", harness, "--session", session, "--name", name]
        arguments = ["read-required", "--harness", harness, "--session", session, "--name", name]
        if part is not None:
            command.extend(("--part", str(part)))
            arguments.extend(("--part", str(part)))
        command_text = shlex.join(command)
        prepared = self.hook("PreToolUse", tool="Bash", command=command_text,
                             session=session, harness=harness, tool_use_id=tool_use_id)
        self.assertEqual(prepared.stdout, "")
        return command_text, arguments

    def post_read(self, command, *, harness="codex", session=None, tool_use_id="required-read-tool-use",
                  tool_response=MISSING):
        return self.hook("PostToolUse", tool="Bash", command=command,
                         session=session or self.session, harness=harness,
                         tool_use_id=tool_use_id, tool_response=tool_response)

    def native_read(self, *, harness="codex", name="engineering-guide", part=None,
                    session=None, tool_use_id="required-read-tool-use", response_shape=None):
        session = session or self.session
        command_text, arguments = self.prepare_read(
            harness=harness, name=name, part=part, session=session, tool_use_id=tool_use_id)
        printed = self.cli(*arguments)
        stdout = printed.stdout[:-1] if response_shape == "claude" else printed.stdout
        if response_shape == "claude":
            tool_response = {"stdout": stdout, "stderr": "", "interrupted": False,
                             "isImage": False, "noOutputExpected": False}
        else:
            tool_response = stdout
        finalized = self.post_read(command_text, session=session, harness=harness,
                                   tool_use_id=tool_use_id, tool_response=tool_response)
        return printed, finalized

    def status(self, session=None, harness="codex"):
        result = self.cli("required-reads", "status", "--harness", harness,
                          "--session", session or self.session)
        return json.loads(result.stdout)

    def receipt_event_count(self):
        return reliability.report(self.root, since="7d")["summary"]["by_event"].get(
            "required_reads.receipt_recorded", 0)

    def invoke_hook(self, payload, *, harness="codex", args=()):
        stream = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")), encoding="utf-8")
        output = io.StringIO()
        with mock.patch.object(ci.sys, "argv", [str(SCRIPT), "hook", "--harness", harness, *args]):
            with mock.patch.object(ci.sys, "stdin", stream), redirect_stdout(output):
                result = ci.main()
        return result, output.getvalue()

    def test_no_registered_list_keeps_gate_inert_and_silent(self):
        state_file = self.helper.state_path(self.gitdir, "codex", self.session)
        result = self.hook("PreToolUse", tool="Edit")
        self.assertEqual(result.stdout, "")
        self.assertFalse(state_file.exists())
        status = json.loads(self.cli("status", "--harness", "codex", "--session", self.session).stdout)
        self.assertNotIn("required_reads", status)

    def test_required_reads_status_reports_explicit_no_list_with_or_without_json_flag(self):
        arguments = ("required-reads", "status", "--harness", "codex", "--session", self.session)
        default = self.cli(*arguments)
        explicit = self.cli(*arguments, "--json")
        expected = json.dumps({"registered": False}, indent=2) + "\n"

        self.assertEqual(default.stdout, expected)
        self.assertEqual(explicit.stdout, expected)

        help_text = self.cli("required-reads", "status", "--help").stdout
        self.assertIn("--json", help_text)
        self.assertIn("--session SESSION_ID --json", help_text)

    def test_uninitialized_git_status_reports_no_list_without_creating_worktree_state(self):
        root = self.home / "plain-git-repo"
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=self.env)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm",
                        "Fixture"], check=True, env=self.env)
        gitdir = Path(subprocess.run(["git", "-C", str(root), "rev-parse", "--absolute-git-dir"],
                                     check=True, capture_output=True, text=True,
                                     env=self.env).stdout.strip())
        arguments = [sys.executable, str(SCRIPT), "--cwd", str(root), "required-reads", "status",
                     "--harness", "codex", "--session", self.session]
        context_dir = gitdir / "context-injector"
        self.assertFalse(context_dir.exists())

        uninitialized = subprocess.run(arguments, capture_output=True, text=True, env=self.env)
        self.assertEqual(uninitialized.returncode, 0, uninitialized.stdout + uninitialized.stderr)
        self.assertEqual(json.loads(uninitialized.stdout), {"registered": False})
        self.assertFalse(context_dir.exists())

        initialized = subprocess.run([sys.executable, str(SCRIPT), "--cwd", str(root), "init",
                                      "--default-role", "orchestrator"],
                                     capture_output=True, text=True, env=self.env)
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        config_path = context_dir / "config.json"
        config_before = config_path.read_bytes()
        registered = subprocess.run(arguments, capture_output=True, text=True, env=self.env)
        self.assertEqual(registered.returncode, 0, registered.stdout + registered.stderr)
        self.assertEqual(json.loads(registered.stdout), {"registered": False})
        self.assertEqual(config_path.read_bytes(), config_before)
        self.assertFalse((context_dir / "required-reads").exists())

    def test_uninitialized_status_preserves_required_read_registration_integrity_errors(self):
        root = self.home / "invalid-registration-repo"
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=self.env)
        gitdir = Path(subprocess.run(["git", "-C", str(root), "rev-parse", "--absolute-git-dir"],
                                     check=True, capture_output=True, text=True,
                                     env=self.env).stdout.strip())
        context_dir = gitdir / "context-injector"
        context_dir.mkdir()
        registration = context_dir / "required-reads.json"
        arguments = [sys.executable, str(SCRIPT), "--cwd", str(root), "required-reads", "status",
                     "--harness", "codex", "--session", self.session]
        cases = (
            ("malformed", "required-reads registration is malformed"),
            ("symlink", "required-reads registration is unsafe"),
            ("missing-list", "registered required-reads list is missing, unreadable, or unsafe"),
            ("changed-list", "registered required-reads list changed since registration"),
        )
        for name, expected_error in cases:
            with self.subTest(registration=name):
                if registration.exists() or registration.is_symlink():
                    registration.unlink()
                if name == "malformed":
                    registration.write_text("not json\n", encoding="utf-8")
                elif name == "symlink":
                    target = self.home / "registration-target.json"
                    target.write_text("{}\n", encoding="utf-8")
                    registration.symlink_to(target)
                else:
                    missing_list = self.home / "missing-required-reads.json"
                    if name == "changed-list":
                        missing_list = self.home / "changed-required-reads.json"
                        missing_list.write_text('{"schema_version":1,"entries":[]}', encoding="utf-8")
                    registration.write_text(json.dumps({
                        "schema_version": 1,
                        "path": str(missing_list),
                        "sha256": "0" * 64,
                    }), encoding="utf-8")

                result = subprocess.run(arguments, capture_output=True, text=True, env=self.env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertTrue(report["registered"])
                self.assertFalse(report["ok"])
                self.assertFalse(report["gate_open"])
                self.assertTrue(
                    any(error == expected_error or error.startswith(expected_error + ": ")
                        for error in report["integrity_errors"]),
                    report["integrity_errors"],
                )
                self.assertFalse((context_dir / "config.json").exists())
                self.assertFalse((context_dir / "required-reads").exists())

        document = self.home / "registered-required-read.md"
        document.write_text("# Registered guide\n", encoding="utf-8")
        list_path = self.home / "valid-required-reads.json"
        list_bytes = json.dumps({
            "schema_version": 1,
            "entries": [{"name": "registered-guide", "path": str(document),
                         "roles": ["orchestrator"]}],
        }).encode("utf-8")
        list_path.write_bytes(list_bytes)
        registration.write_text(json.dumps({
            "schema_version": 1,
            "path": str(list_path),
            "sha256": hashlib.sha256(list_bytes).hexdigest(),
        }), encoding="utf-8")
        valid_registered = subprocess.run(arguments, capture_output=True, text=True, env=self.env)
        self.assertEqual(valid_registered.returncode, 1, valid_registered.stdout + valid_registered.stderr)
        self.assertIn("config.json", valid_registered.stderr)
        self.assertFalse((context_dir / "required-reads").exists())

    def test_subagent_pretool_does_not_start_required_read_state(self):
        self.register()
        session = "synthetic-required-read-subagent"
        state_file = self.helper.state_path(self.gitdir, "codex", session)
        payload = {
            "hook_event_name": "PreToolUse",
            "cwd": str(self.root),
            "session_id": session,
            "agent_id": "synthetic-subagent",
            "tool_name": "Edit",
            "tool_input": {},
        }

        result = self.cli("hook", "--harness", "codex", payload=payload)

        self.assertEqual(result.stdout, "")
        self.assertFalse(state_file.exists())

    def test_unbound_read_names_missing_native_tool_id_and_recovery(self):
        self.register()
        session = "synthetic-missing-native-tool-id"
        state_file = self.helper.state_path(self.gitdir, "codex", session)
        command = shlex.join([
            "pattern-buffer", "--cwd", str(self.root), "read-required",
            "--harness", "codex", "--session", session, "--name", "engineering-guide",
        ])

        denied = self.hook("PreToolUse", harness="codex", session=session, tool="Bash",
                           command=command)
        denial = json.loads(denied.stdout)["hookSpecificOutput"]

        self.assertEqual(denial["permissionDecision"], "deny")
        self.assertIn("native tool call ID is missing or malformed",
                      denial["permissionDecisionReason"])
        self.assertIn("pattern-buffer guide --print", denial["permissionDecisionReason"])
        self.assertIn("The gate remains closed", denial["permissionDecisionReason"])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["generation"], 1)
        self.assertEqual(state["receipts"], [])
        self.assertIsNone(state["gate_opened_at"])

    def test_running_session_starts_generation_before_first_gate_denial(self):
        self.register()
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                session = f"running-before-registration-{harness}"
                state_file = self.helper.state_path(self.gitdir, harness, session)
                self.assertFalse(state_file.exists())

                work_command = "git status"
                denied = self.hook("PreToolUse", harness=harness, session=session, tool="Bash",
                                   command=work_command, tool_use_id="original-work-call")
                denial = json.loads(denied.stdout)["hookSpecificOutput"]
                self.assertEqual(denial["permissionDecision"], "deny")

                state = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual(state["generation"], 1)
                self.assertEqual(state["receipts"], [])
                self.assertEqual(state["pending_reads"], [])
                self.assertIsNone(state["gate_opened_at"])
                self.assertIn("engineering-guide", denial["permissionDecisionReason"])
                self.assertIn("Next run:", denial["permissionDecisionReason"])
                self.assertIn("pattern-buffer guide --print", denial["permissionDecisionReason"])

                tool_use_id = "running-session-read"
                read_command = shlex.join([
                    "pattern-buffer", "--cwd", str(self.root), "read-required",
                    "--harness", harness, "--session", session, "--name", "engineering-guide",
                ])
                arguments = ["read-required", "--harness", harness, "--session", session,
                             "--name", "engineering-guide"]
                prepared = self.hook("PreToolUse", harness=harness, session=session, tool="Bash",
                                     command=read_command, tool_use_id=tool_use_id)
                self.assertEqual(prepared.stdout, "")
                printed = self.cli(*arguments).stdout
                response = ({"stdout": printed[:-1], "stderr": "", "interrupted": False,
                             "isImage": False, "noOutputExpected": False}
                            if harness == "claude" else printed)
                self.hook("PostToolUse", harness=harness, session=session, tool="Bash",
                          command=read_command, tool_use_id=tool_use_id, tool_response=response)

                status = self.status(session, harness)
                self.assertTrue(status["gate_open"])
                self.assertEqual(status["generation"], 1)
                allowed = self.hook("PreToolUse", harness=harness, session=session, tool="Bash",
                                    command=work_command, tool_use_id="original-work-call")
                self.assertEqual(allowed.stdout, "")

                self.start("resume", harness=harness, session=session)
                resumed = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual(resumed["generation"], 1)
                self.assertEqual(len(resumed["receipts"]), 1)

                self.start("compact", harness=harness, session=session)
                reset = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual(reset["generation"], 2)
                self.assertEqual(reset["receipts"], [])
                self.assertIsNone(reset["gate_opened_at"])

                report = reliability.report(self.root, since="7d")
                events = [event for event in report["events"]
                          if event.get("session") == session
                          and event["event"] == "required_reads.generation_started"]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["result"], "pretool_gate")
                self.assertEqual(events[0]["details"]["source"], "pretool_gate")
                self.assertEqual(events[0]["details"]["generation"], 1)
                self.assertNotIn("content", events[0])
                self.assertNotIn("path", events[0]["details"])
                unknown_event_types = next(
                    (item for item in report["unknowns"]
                     if item.get("code") == "unrecognized_event_types"), None)
                if unknown_event_types:
                    self.assertNotIn("required_reads.generation_started",
                                     unknown_event_types["counts"])

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
        self.assertIn("pattern-buffer guide --print",
                      denied["hookSpecificOutput"]["permissionDecisionReason"])
        after_denial = self.status()
        self.assertEqual(after_denial["generation"], before["generation"])
        self.assertEqual(after_denial["entries"][0]["state"], "pending")
        self.assertEqual(reliability.report(self.root, since="7d")["summary"]["by_event"].get(
            "required_reads.generation_started", 0), 0)

    def test_receipt_opens_current_generation_and_compaction_resets_it(self):
        self.register()
        startup = self.start("startup")
        self.assertIn("Required reads", startup.stdout)
        denied = self.gate_edit()
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

        self.native_read()
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

    def test_both_recorded_native_response_shapes_finalize_the_complete_frame(self):
        self.register()
        cases = (("codex", "codex-recorded-session", None),
                 ("claude", "claude-recorded-session", "claude"))
        for harness, session, response_shape in cases:
            with self.subTest(harness=harness):
                self.start(harness=harness, session=session)
                printed, _ = self.native_read(
                    harness=harness, session=session, response_shape=response_shape,
                    tool_use_id=f"{harness}-tool-use")
                expected_document = b"# Engineering guide\nRead this complete fixture.\n"
                digest = hashlib.sha256(expected_document).hexdigest()
                expected = (f"--- Pattern Buffer required read: engineering-guide "
                            f"(part 1 of 1, {len(expected_document)} bytes) ---\n"
                            f"{expected_document.decode('utf-8')}\n"
                            f"--- End required read: sha256={digest} ---\n")
                self.assertEqual(printed.stdout, expected)
                report = self.status(session, harness)
                self.assertEqual(report["pending_read_count"], 0)
                self.assertEqual(report["entries"][0]["state"], "current")
        self.assertEqual(self.receipt_event_count(), 2)

    def test_standalone_print_is_stdout_only_under_readonly_worktree_metadata(self):
        self.register()
        state_file = self.helper.state_path(self.gitdir, "codex", self.session)
        state_directory = state_file.parent
        context_directory = state_directory.parent
        self.assertFalse(state_directory.exists())
        before_receipts = self.receipt_event_count()
        original_mode = context_directory.stat().st_mode & 0o777
        os.chmod(context_directory, original_mode & ~0o222)
        try:
            printed = self.read()
        finally:
            os.chmod(context_directory, original_mode)

        expected_document = b"# Engineering guide\nRead this complete fixture.\n"
        digest = hashlib.sha256(expected_document).hexdigest()
        expected = (f"--- Pattern Buffer required read: engineering-guide "
                    f"(part 1 of 1, {len(expected_document)} bytes) ---\n"
                    f"{expected_document.decode('utf-8')}\n"
                    f"--- End required read: sha256={digest} ---\n")
        self.assertEqual(printed.stdout, expected)
        self.assertEqual(printed.stderr, "")
        self.assertFalse(state_directory.exists())
        self.assertFalse(state_file.exists())
        self.assertEqual(self.receipt_event_count(), before_receipts)

    def test_status_doctor_and_handoff_remain_readonly_without_writable_metadata(self):
        self.register()
        self.start()
        settings = self.home / "codex-settings.json"
        settings.write_text(json.dumps(ci.hook_config("codex", SCRIPT, with_checkpoints=False)),
                            encoding="utf-8")
        context_directory = self.gitdir / "context-injector"
        paths = [context_directory, *context_directory.rglob("*")]
        modes = {path: path.stat().st_mode & 0o777 for path in paths}
        before = {path.relative_to(context_directory).as_posix():
                  path.read_bytes() if path.is_file() else None for path in paths
                  if path != context_directory}
        for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
            os.chmod(path, modes[path] & ~0o222)
        try:
            status = self.cli("required-reads", "status", "--harness", "codex",
                              "--session", self.session)
            doctor = self.cli("doctor", "--harness", "codex", "--session", self.session,
                              "--settings", str(settings))
            handoff = self.cli("handoff-check", "--harness", "codex", "--session", self.session,
                               "--settings", str(settings))
        finally:
            for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
                os.chmod(path, modes[path])

        self.assertEqual(json.loads(status.stdout)["pending_read_count"], 1)
        self.assertIn("Required reads list", doctor.stdout)
        self.assertTrue(json.loads(handoff.stdout)["configuration_ready"])
        after_paths = [context_directory, *context_directory.rglob("*")]
        after = {path.relative_to(context_directory).as_posix():
                 path.read_bytes() if path.is_file() else None for path in after_paths
                 if path != context_directory}
        self.assertEqual(after, before)

    def test_posttool_rejects_truncated_mismatched_and_unpaired_frames(self):
        self.register()
        self.start()
        for tool_use_id, transform in (
                ("truncated-frame", lambda text: text[:-1]),
                ("mismatched-frame", lambda text: text.replace("sha256=", "sha256=0", 1))):
            with self.subTest(tool_use_id=tool_use_id):
                command, arguments = self.prepare_read(tool_use_id=tool_use_id)
                printed = self.cli(*arguments)
                self.post_read(command, tool_use_id=tool_use_id,
                               tool_response=transform(printed.stdout))
                self.assertEqual(self.status()["entries"][0]["state"], "pending")
        command = shlex.join(["pattern-buffer", "--cwd", str(self.root), "read-required",
                              "--harness", "codex", "--session", self.session,
                              "--name", "engineering-guide"])
        printed = self.read()
        self.post_read(command, tool_use_id="unknown-tool-use", tool_response=printed.stdout)
        self.assertEqual(self.status()["entries"][0]["state"], "pending")
        self.assertEqual(self.receipt_event_count(), 0)

    def test_completion_requires_exact_session_tool_id_and_command_binding(self):
        self.register()
        self.start()
        tool_use_id = "exact-caller-tool-use"
        command, arguments = self.prepare_read(tool_use_id=tool_use_id)
        printed = self.cli(*arguments)
        self.post_read(command, session="foreign-session", tool_use_id=tool_use_id,
                       tool_response=printed.stdout)
        self.post_read(command, tool_use_id="foreign-tool-use", tool_response=printed.stdout)
        self.hook("PostToolUse", tool="Read", command=command, tool_use_id=tool_use_id,
                  tool_response=printed.stdout)
        self.post_read(command + " --part 1", tool_use_id=tool_use_id,
                       tool_response=printed.stdout)
        self.assertEqual(self.status()["entries"][0]["state"], "pending")

        self.post_read(command, tool_use_id=tool_use_id, tool_response=printed.stdout)
        self.assertEqual(self.status()["entries"][0]["state"], "current")
        self.assertEqual(self.receipt_event_count(), 1)

    def test_posttool_rejects_available_claude_failure_flags(self):
        self.register()
        session = "claude-failure-session"
        self.start(harness="claude", session=session)
        cases = (("interrupted", True), ("isImage", True),
                 ("noOutputExpected", True), ("stderr", "fixture failure"))
        for index, (key, value) in enumerate(cases):
            with self.subTest(flag=key):
                tool_use_id = f"claude-failure-{index}"
                command, arguments = self.prepare_read(
                    harness="claude", session=session, tool_use_id=tool_use_id)
                printed = self.cli(*arguments)
                response = {"stdout": printed.stdout, "stderr": "", "interrupted": False,
                            "isImage": False, "noOutputExpected": False}
                response[key] = value
                self.post_read(command, harness="claude", session=session,
                               tool_use_id=tool_use_id, tool_response=response)
                self.assertEqual(self.status(session, "claude")["entries"][0]["state"], "pending")
        self.assertEqual(self.receipt_event_count(), 0)

    def test_posttool_rechecks_generation_list_and_file_identity(self):
        self.register()
        self.start()
        for scenario in ("generation", "list", "file"):
            with self.subTest(scenario=scenario):
                if scenario == "file":
                    self.register()
                tool_use_id = f"changed-{scenario}"
                command, arguments = self.prepare_read(tool_use_id=tool_use_id)
                printed = self.cli(*arguments)
                if scenario == "generation":
                    self.start("compact")
                    state_file = self.helper.state_path(self.gitdir, "codex", self.session)
                    self.assertEqual(json.loads(state_file.read_text(encoding="utf-8"))["pending_reads"], [])
                elif scenario == "list":
                    self.register(entries=[
                        {"name": "engineering-guide", "path": str(self.document),
                         "roles": ["orchestrator"]},
                        {"name": "second-guide", "path": str(self.document),
                         "roles": ["orchestrator"]},
                    ])
                else:
                    self.document.write_text("# Changed synthetic guide\n", encoding="utf-8")
                self.post_read(command, tool_use_id=tool_use_id, tool_response=printed.stdout)
                self.assertEqual(self.receipt_event_count(), 0)
                self.assertEqual(self.status()["pending_read_count"], 1 if scenario != "list" else 2)

    def test_same_tool_call_and_completion_retries_are_idempotent(self):
        self.register()
        self.start()
        tool_use_id = "idempotent-tool-use"
        command, arguments = self.prepare_read(tool_use_id=tool_use_id)
        self.prepare_read(tool_use_id=tool_use_id)
        state_file = self.helper.state_path(self.gitdir, "codex", self.session)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(len(state["pending_reads"]), 1)

        printed = self.cli(*arguments)
        self.post_read(command, tool_use_id=tool_use_id, tool_response=printed.stdout)
        self.post_read(command, tool_use_id=tool_use_id, tool_response=printed.stdout)
        self.assertEqual(self.status()["entries"][0]["state"], "current")
        self.assertEqual(self.receipt_event_count(), 1)

    def test_no_checkpoints_installs_receipt_only_posttool_handler(self):
        self.register()
        config = ci.hook_config("codex", SCRIPT, with_checkpoints=False)
        self.assertEqual(set(config["hooks"]), {"SessionStart", "PreToolUse", "PostToolUse"})
        handler = config["hooks"]["PostToolUse"][0]["hooks"][0]
        self.assertIn("--required-reads-only", shlex.split(handler["command"], comments=True))
        self.assertFalse(ci.installed_checkpoints_enabled(config, "codex"))

        self.start()
        tool_use_id = "recovery-only-tool-use"
        command, arguments = self.prepare_read(tool_use_id=tool_use_id)
        printed = self.cli(*arguments)
        payload = {"hook_event_name": "PostToolUse", "cwd": str(self.root),
                   "session_id": self.session, "tool_name": "Bash", "tool_use_id": tool_use_id,
                   "tool_input": {"command": command}, "tool_response": printed.stdout}
        with mock.patch.object(ci, "checkpoint_event", side_effect=AssertionError("checkpoint processing ran")):
            result, output = self.invoke_hook(payload, args=("--required-reads-only",))
        self.assertEqual(result, 0)
        self.assertEqual(output, "")
        self.assertEqual(self.status()["entries"][0]["state"], "current")

    def test_resume_keeps_receipts_while_clear_and_fork_reset_generation(self):
        self.register()
        self.start("startup")
        self.native_read()
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
        self.native_read()
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

        self.native_read(part=1, tool_use_id="multipart-part-1")
        after_first = self.status()
        self.assertEqual([part["state"] for part in after_first["entries"][0]["parts"]],
                         ["current", "pending"])
        self.assertEqual(self.gate_edit()["hookSpecificOutput"]["permissionDecision"], "deny")

        self.native_read(part=2, tool_use_id="multipart-part-2")
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
