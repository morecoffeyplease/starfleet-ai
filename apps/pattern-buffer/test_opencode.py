"""Synthetic OpenCode adapter, dispatch, and plugin callback checks."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

SCRIPT = Path(__file__).with_name("pattern_buffer.py")
PLUGIN = Path(__file__).parent / "opencode-plugin/server.js"
NODE = shutil.which("node")
CHECKPOINT_NOTE = "# Working checkpoint\n\n" + "\n\n".join(
    "## " + title + "\n" + body for title, body in [
        ("Current state", "The exact OpenCode checkpoint fixture is published."),
        ("Next action", "Read the published note before continuing."),
        ("Decisions and uncertainty", "Pressure qualification remains unknown."),
        ("Active work", "No active work is owned by this fixture."),
        ("Evidence", "Only synthetic offline callback evidence exists."),
        ("Holds and guidance", "Preserve the explicit hold and do not infer model reception.")]) + "\n"


@unittest.skipUnless(NODE, "Node.js is required for the synthetic plugin callback fixture")
class OpenCodePatternBufferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.owner = self.base / "owner"
        self.owner.mkdir()
        self.git(self.owner, "init", "-q")
        self.git(self.owner, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--allow-empty", "-qm", "Initial")
        self.child = self.base / "child"
        self.git(self.owner, "worktree", "add", "-qb", "child", str(self.child))
        self.call(self.owner, "init", "--default-role", "orchestrator")
        self.brief = self.base / "brief.md"
        self.brief.write_text("Implement the exact synthetic task and report evidence.\n")
        self.receipt = self.base / "receipt.json"
        self.parent_session = "ses_parent012345"
        self.parent_task_id = "mp-parent-task-issue5"
        self.surface_id = str(uuid.uuid4()).upper()
        self.workspace_id = str(uuid.uuid4()).upper()
        self.env = {**os.environ, "CMUX_AGENT_LAUNCH_KIND": "opencode",
                    "OPENCODE_SESSION_ID": self.parent_session,
                    "CMUX_SURFACE_ID": self.surface_id,
                    "CMUX_WORKSPACE_ID": self.workspace_id,
                    "XDG_STATE_HOME": str(self.base / "state"),
                    "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": ""}
        self.call(self.owner, "assign", "--harness", "opencode", "--session", self.parent_session,
                  "--role", "orchestrator")

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                              capture_output=True, text=True, env=self.env if hasattr(self, "env") else None)

    def call(self, cwd, *args, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), "--cwd", str(cwd), *args],
                                capture_output=True, text=True, env=self.env if hasattr(self, "env") else None)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        return result

    def prepare(self, receipt=None, task_file=None, parent_task_id="default"):
        parent_task_id = self.parent_task_id if parent_task_id == "default" else parent_task_id
        args = ["prepare-dispatch", "--harness", "opencode", "--owner-worktree", str(self.owner),
                "--task-file", str(task_file or self.brief), "--receipt-file", str(receipt or self.receipt),
                "--role", "implementer"]
        if parent_task_id is not None:
            args.extend(("--parent-task-id", parent_task_id))
        result = self.call(self.child, *args)
        return json.loads(result.stdout)

    def parent_assignment_path(self):
        gitdir = Path(self.git(self.owner, "rev-parse", "--absolute-git-dir").stdout.strip())
        key = hashlib.sha256(json.dumps(["opencode", self.parent_session, ""]).encode()).hexdigest()
        return gitdir / "context-injector" / "sessions" / f"{key}.json"

    def bind(self, task, session="ses_012345abcdef", receipt=None, role="implementer", cwd=None, success=True):
        return self.call(cwd or self.child, "bind-dispatch", "--harness", "opencode",
                         "--session", session, "--task-id", task["task_id"],
                         "--receipt-file", str(receipt or self.receipt), "--role", role,
                         success=success)

    def context(self, session="ses_unassigned", cwd=None):
        return json.loads(self.call(cwd or self.child, "context", "--harness", "opencode",
                                    "--session", session).stdout)

    def enroll_checkpoint(self, note_path):
        note_path.write_text(CHECKPOINT_NOTE)
        return json.loads(self.call(self.owner, "checkpoint-enroll", "--harness", "opencode",
                                    "--session", self.parent_session, "--file", str(note_path)).stdout)

    def plugin_call(self, session, parent_id=None, cwd=None, hook_name="context"):
        fixture = r'''import plugin from %s;
const callbacks = {};
const ctx = { location: { directory: %s }, session: { hook: async (name, fn) => { callbacks[name] = fn; } } };
await plugin.setup(ctx);
const event = { sessionID: %s, parentID: %s, system: [{type:"text",text:"stale owned text",metadata:{"starfleet.pattern-buffer":"v1"}},{type:"text",text:"unowned"}] };
let error;
try { await callbacks[%s](event); } catch (failure) { error = failure.message; }
console.log(JSON.stringify({system:event.system,error,hooks:Object.keys(callbacks).sort()}));
''' % (json.dumps(PLUGIN.as_uri()), json.dumps(str(cwd or self.owner)), json.dumps(session),
       json.dumps(parent_id), json.dumps(hook_name))
        result = subprocess.run([NODE, "--input-type=module", "-e", fixture], capture_output=True,
                                text=True, env=self.env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_unassigned_open_code_session_does_not_inherit_worktree_default_or_parent_id(self):
        result = self.context("ses_unassigned", cwd=self.owner)
        self.assertEqual(result["status"], "context")
        self.assertIsNone(result["assignment_sha256"])
        self.assertIn("role: unassigned", result["text"])
        self.assertNotIn("orchestrator context", result["text"])
        parts = self.plugin_call("ses_unassigned", "ses_parent", cwd=self.owner)
        self.assertEqual(parts["hooks"], ["compaction", "context"])
        self.assertEqual([part["text"] for part in parts["system"] if part.get("metadata", {}).get(
            "starfleet.pattern-buffer") == "v1"], [result["text"]])
        self.assertIn("unowned", [part["text"] for part in parts["system"]])

    def test_prepare_bind_replay_update_finish_and_immutable_receipt(self):
        ticket = self.prepare()
        receipt_before = self.receipt.read_bytes()
        saved = json.loads(receipt_before)
        self.assertEqual(ticket["parent"], saved["parent"])
        self.assertEqual(ticket["parent"]["session_id"], self.parent_session)
        self.assertEqual(ticket["parent"]["task_id"], self.parent_task_id)
        self.assertEqual(ticket["parent"]["role"], "orchestrator")
        self.assertEqual(ticket["parent"]["owner_worktree"], str(self.owner.resolve()))
        self.assertEqual(ticket["parent"]["owner_gitdir"], str(Path(
            self.git(self.owner, "rev-parse", "--absolute-git-dir").stdout.strip()).resolve()))
        self.assertEqual(ticket["parent"]["surface_id"], self.surface_id)
        self.assertEqual(ticket["parent"]["workspace_id"], self.workspace_id)
        self.assertRegex(ticket["parent"]["assignment_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(ticket["ticket_version"], 2)
        self.assertEqual(ticket["parent_kind"], "opencode")
        parent_context = self.context(self.parent_session, cwd=self.owner)
        self.assertIn("role: orchestrator", parent_context["text"])
        self.assertEqual(parent_context["assignment_sha256"], ticket["parent"]["assignment_sha256"])
        self.assertEqual(saved["status"], "pending")
        self.assertEqual(saved["session_id"], None)
        self.assertEqual(saved["role"], "implementer")
        self.call(self.child, "context", "--harness", "opencode", "--session", "ses_012345abcdef",
                  success=False)
        pending = self.plugin_call("ses_012345abcdef", cwd=self.child)
        self.assertIn("Pattern Buffer OpenCode context failed", pending["error"])
        self.bind(ticket)
        self.assertEqual(self.receipt.read_bytes(), receipt_before)
        active = self.context("ses_012345abcdef")
        self.assertIn("role: implementer", active["text"])
        self.assertIn("Implement the exact synthetic task", active["text"])
        changed = self.base / "revised.md"
        changed.write_text("Revised synthetic scope within the same assignment.\n")
        self.call(self.child, "update-task", "--task-id", ticket["task_id"],
                  "--task-file", str(changed))
        refreshed = self.plugin_call("ses_012345abcdef", "ses_parent", cwd=self.child,
                                     hook_name="compaction")
        owned = [part["text"] for part in refreshed["system"] if part.get("metadata", {}).get(
            "starfleet.pattern-buffer") == "v1"]
        self.assertEqual(len(owned), 1)
        self.assertIn("Revised synthetic scope", owned[0])
        self.assertNotIn("Implement the exact synthetic task", owned[0])
        self.call(self.child, "finish-task", "--task-id", ticket["task_id"])
        finished = self.context("ses_012345abcdef")["text"]
        self.assertIn("Task " + ticket["task_id"] + " is finished", finished)
        self.assertNotIn("Revised synthetic scope", finished)
        self.assertEqual(self.receipt.read_bytes(), receipt_before)

    def test_bind_rejects_exact_identity_role_receipt_and_worktree_mismatches(self):
        ticket = self.prepare()
        self.bind(ticket, session="SES_012345abcdef", success=False)
        other_receipt = self.base / "other-receipt.json"
        other_ticket = self.prepare(receipt=other_receipt)
        self.bind(ticket, receipt=other_receipt, success=False)
        role_result = subprocess.run([sys.executable, str(SCRIPT), "--cwd", str(self.child),
                                      "bind-dispatch", "--harness", "opencode", "--session",
                                      "ses_012345abcdef", "--task-id", ticket["task_id"],
                                      "--receipt-file", str(self.receipt), "--role", "orchestrator"],
                                     capture_output=True, text=True, env=self.env)
        self.assertEqual(role_result.returncode, 2)
        self.bind(ticket, cwd=self.owner, success=False)
        self.bind(ticket)
        self.bind(other_ticket, session="ses_012345abcdef", receipt=other_receipt, success=False)

    def test_context_requires_exact_case_sensitive_native_session_id(self):
        result = self.call(self.child, "context", "--harness", "opencode", "--session", "SEs_abc", success=False)
        self.assertIn("case-sensitive ses_", result.stderr)

    def test_checkpoint_enrollment_recovery_and_pressure_stay_separate(self):
        folder = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".opencode-pb-checkpoint-")
        self.addCleanup(folder.cleanup)
        note_path = Path(folder.name) / "working-checkpoint.md"
        enrolled = self.enroll_checkpoint(note_path)
        self.assertEqual(enrolled["state"], "prepared")
        self.assertEqual(enrolled["harness"], "opencode")
        self.assertFalse(enrolled["pressure_qualified"])
        self.assertEqual(enrolled["pressure_status"], "unqualified; live warnings disabled")
        report = enrolled["checkpoint"]
        self.assertIsNone(report["compact_at"])
        self.assertIsNone(report["reminder_threshold"])
        self.assertFalse(report["reminders"])
        self.assertFalse(report["pressure_qualified"])
        self.assertIn("OpenCode pressure warnings remain unqualified and disabled", self.context(self.parent_session, self.owner)["text"])
        self.assertIn(str(note_path), self.context(self.parent_session, self.owner)["text"])
        self.call(self.owner, "checkpoint-config", "--harness", "opencode", "--session",
                  self.parent_session, "--reminders", "on", success=False)
        pressure_session = "ses_pressure012345"
        self.call(self.owner, "assign", "--harness", "opencode", "--session", pressure_session,
                  "--role", "orchestrator")
        rejected = self.call(self.owner, "checkpoint-init", "--harness", "opencode", "--session",
                             pressure_session, "--compact-at", "90", success=False)
        self.assertIn("pressure is unqualified", rejected.stderr)
        rejected_lead = self.call(self.owner, "checkpoint-init", "--harness", "opencode", "--session",
                                  pressure_session, "--lead-points", "5", success=False)
        self.assertIn("do not supply compact-at or lead values", rejected_lead.stderr)

    def test_tool_completion_publishes_and_compaction_completion_is_deduplicated(self):
        folder = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".opencode-pb-checkpoint-")
        self.addCleanup(folder.cleanup)
        note_path = Path(folder.name) / "working-checkpoint.md"
        enrolled = self.enroll_checkpoint(note_path)
        draft = Path(enrolled["edit_this_file"])
        draft.write_text(CHECKPOINT_NOTE.replace("exact OpenCode checkpoint fixture", "updated OpenCode checkpoint fixture"))
        event_session = self.parent_session
        fixture = r'''import plugin from %s;
const callbacks = {};
const sessionID = %s;
const event = (type, id, seq, eventSession = sessionID) => ({ id, type,
  durable: { aggregateID: sessionID, seq }, data: { sessionID: eventSession } });
const events = [
  event("session.compaction.started", "evt_attempt0001", 10),
  event("session.compaction.ended", "evt_completed001", 11),
  event("session.compaction.ended", "evt_completed001", 11),
  event("session.compaction.failed", "evt_failed0001", 12),
  event("session.compaction.ended", "evt_foreign0001", 13, "ses_other012345"),
];
const ctx = {
  location: { directory: %s },
  session: { hook: async (name, fn) => { callbacks[name] = fn; return { dispose: async () => {} }; } },
  tool: { hook: async (name, fn) => { callbacks[name] = fn; return { dispose: async () => {} }; } },
  event: { subscribe: () => (async function* () { yield* events; })() },
};
const cleanup = await plugin.setup(ctx);
await callbacks["execute.after"]({ status: "completed", sessionID });
await cleanup();
console.log(JSON.stringify({ hooks: Object.keys(callbacks).sort() }));
''' % (json.dumps(PLUGIN.as_uri()), json.dumps(event_session), json.dumps(str(self.owner)))
        result = subprocess.run([NODE, "--input-type=module", "-e", fixture], capture_output=True,
                                text=True, env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("execute.after", json.loads(result.stdout)["hooks"])
        published = Path(enrolled["published_copy"]).read_text()
        self.assertIn("updated OpenCode checkpoint fixture", published)
        self.assertIn("session: \"" + self.parent_session + "\"", published)
        context = self.context(self.parent_session, self.owner)["text"]
        self.assertIn("Observed 1 successful OpenCode compaction completion event", context)
        self.assertIn("complete event coverage remains uncertain", context)
        self.assertIn("Read the reviewed current-state checkpoint before resuming", context)
        report = json.loads(self.call(self.owner, "checkpoint-status", "--harness", "opencode",
                                      "--session", self.parent_session).stdout)
        self.assertEqual(report["observed_generation"], 1)
        self.assertTrue(report["generation_uncertain"])
        self.assertFalse(report["pressure_qualified"])

    def test_invalid_opencode_draft_keeps_last_good_note_and_advises_once(self):
        folder = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".opencode-pb-checkpoint-")
        self.addCleanup(folder.cleanup)
        note_path = Path(folder.name) / "working-checkpoint.md"
        enrolled = self.enroll_checkpoint(note_path)
        published = Path(enrolled["published_copy"])
        original = published.read_bytes()
        draft = Path(enrolled["edit_this_file"])
        draft.write_text("## Current state\nMissing the other required sections.\n")
        result = self.call(self.owner, "opencode-checkpoint-event", "--session", self.parent_session,
                           "--event", "tool-completed")
        self.assertEqual(json.loads(result.stdout)["status"], "rejected")
        self.assertEqual(published.read_bytes(), original)
        first = self.context(self.parent_session, self.owner)["text"]
        second = self.context(self.parent_session, self.owner)["text"]
        self.assertIn("latest changed draft was rejected", first)
        self.assertNotIn("latest changed draft was rejected", second)
        self.assertEqual(published.read_bytes(), original)

    def test_corrupt_published_note_is_advisory_and_does_not_replace_standing_context(self):
        folder = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".opencode-pb-checkpoint-")
        self.addCleanup(folder.cleanup)
        note_path = Path(folder.name) / "working-checkpoint.md"
        enrolled = self.enroll_checkpoint(note_path)
        published = Path(enrolled["published_copy"])
        published.write_text("tampered outside checkpoint-save\n")
        context = self.context(self.parent_session, self.owner)["text"]
        self.assertIn("Track ownership", context)
        self.assertNotIn("tampered outside checkpoint-save", context)
        self.assertIn("reviewed checkpoint is unavailable or changed", context)
        self.assertIn("Inspect checkpoint-status for this exact session", context)

    def test_prepare_rejects_reused_receipt_path_before_creating_another_task(self):
        self.prepare()
        self.call(self.child, "prepare-dispatch", "--harness", "opencode", "--owner-worktree",
                  str(self.owner), "--task-file", str(self.brief), "--receipt-file", str(self.receipt),
                  "--role", "implementer", "--parent-task-id", self.parent_task_id, success=False)

    def test_prepare_rejects_receipt_symlink_without_following_it(self):
        target = self.base / "receipt-target.json"
        symlink = self.base / "receipt-link.json"
        symlink.symlink_to(target)
        self.call(self.child, "prepare-dispatch", "--harness", "opencode", "--owner-worktree",
                  str(self.owner), "--task-file", str(self.brief), "--receipt-file", str(symlink),
                  "--role", "implementer", "--parent-task-id", self.parent_task_id, success=False)
        self.assertFalse(target.exists())
        self.assertEqual(json.loads(self.call(self.owner, "tasks").stdout)["tasks"], [])

    def test_receipt_write_failure_leaves_the_pending_task_inspectable(self):
        file_parent = self.base / "not-a-directory"
        file_parent.write_text("synthetic fixture")
        failed_receipt = file_parent / "receipt.json"
        failed = self.call(self.child, "prepare-dispatch", "--harness", "opencode", "--owner-worktree",
                           str(self.owner), "--task-file", str(self.brief), "--receipt-file", str(failed_receipt),
                           "--role", "implementer", "--parent-task-id", self.parent_task_id, success=False)
        listing = json.loads(self.call(self.owner, "tasks").stdout)
        pending = [task for task in listing["tasks"] if Path(task["root"]).resolve() == self.child.resolve()]
        self.assertEqual(len(pending), 1, f"prepare: {failed.stderr}; task listing: {listing}")
        self.assertEqual(pending[0]["status"], "pending")
        self.assertEqual(pending[0]["receipt_file"], str(failed_receipt.resolve()))

    def test_prepare_requires_exact_open_code_parent_scope_and_task_reference(self):
        for key, value, message in (
                ("CMUX_AGENT_LAUNCH_KIND", "codex", "OPENCODE_SESSION_ID requires CMUX_AGENT_LAUNCH_KIND=opencode"),
                ("OPENCODE_SESSION_ID", "", "OPENCODE_SESSION_ID"),
                ("CMUX_SURFACE_ID", "", "CMUX_SURFACE_ID"),
                ("CMUX_SURFACE_ID", "not-a-uuid", "exact CMUX UUID"),
                ("CMUX_WORKSPACE_ID", "", "CMUX_WORKSPACE_ID")):
            with self.subTest(key=key):
                original = self.env[key]
                self.env[key] = value
                result = self.call(self.child, "prepare-dispatch", "--harness", "opencode",
                                   "--owner-worktree", str(self.owner), "--task-file", str(self.brief),
                                   "--receipt-file", str(self.base / (key + ".json")), "--role", "implementer",
                                   "--parent-task-id", self.parent_task_id, success=False)
                self.assertIn(message, result.stderr)
                self.env[key] = original
        for invalid in ("", " padded", "padded ", "line\nbreak", "x" * 257):
            with self.subTest(parent_task_id=repr(invalid)):
                result = self.call(self.child, "prepare-dispatch", "--harness", "opencode",
                                   "--owner-worktree", str(self.owner), "--task-file", str(self.brief),
                                   "--receipt-file", str(self.base / (str(len(invalid)) + "-invalid.json")),
                                   "--role", "implementer", "--parent-task-id", invalid, success=False)
                self.assertEqual(result.returncode, 1)
        listing = json.loads(self.call(self.owner, "tasks").stdout)
        self.assertEqual(listing["tasks"], [])

    def test_prepare_rejects_missing_foreign_ambiguous_and_non_orchestrator_parent(self):
        assignment = self.parent_assignment_path()
        original = assignment.read_bytes()
        assignment.unlink()
        self.call(self.child, "prepare-dispatch", "--harness", "opencode", "--owner-worktree",
                  str(self.owner), "--task-file", str(self.brief), "--receipt-file",
                  str(self.base / "missing.json"), "--role", "implementer",
                  "--parent-task-id", self.parent_task_id, success=False)
        assignment.write_bytes(original)

        assignment.write_text(json.dumps({"root": str(self.base / "foreign-owner"), "role": "orchestrator"}))
        foreign = self.call(self.child, "prepare-dispatch", "--harness", "opencode", "--owner-worktree",
                            str(self.owner), "--task-file", str(self.brief), "--receipt-file",
                            str(self.base / "foreign.json"), "--role", "implementer",
                            "--parent-task-id", self.parent_task_id, success=False)
        self.assertIn("foreign, task-bound or not an orchestrator", foreign.stderr)
        assignment.write_bytes(original)

        alternate = self.base / "alternate-parent.json"
        alternate.write_bytes(original)
        assignment.unlink()
        assignment.symlink_to(alternate)
        ambiguous = self.call(self.child, "prepare-dispatch", "--harness", "opencode", "--owner-worktree",
                              str(self.owner), "--task-file", str(self.brief), "--receipt-file",
                              str(self.base / "ambiguous.json"), "--role", "implementer",
                              "--parent-task-id", self.parent_task_id, success=False)
        self.assertIn("no session assignment", ambiguous.stderr)
        assignment.unlink()
        assignment.write_bytes(original)

        sessions_dir = assignment.parent
        detached_sessions = self.base / "detached-parent-sessions"
        sessions_dir.rename(detached_sessions)
        sessions_dir.symlink_to(detached_sessions, target_is_directory=True)
        linked_directory = self.call(self.child, "prepare-dispatch", "--harness", "opencode",
                                     "--owner-worktree", str(self.owner), "--task-file", str(self.brief),
                                     "--receipt-file", str(self.base / "ambiguous-directory.json"),
                                     "--role", "implementer", "--parent-task-id", self.parent_task_id,
                                     success=False)
        self.assertIn("assignment path must not contain a symlink", linked_directory.stderr)
        sessions_dir.unlink()
        detached_sessions.rename(sessions_dir)

        self.call(self.owner, "assign", "--harness", "opencode", "--session", self.parent_session,
                  "--role", "implementer")
        role = self.call(self.child, "prepare-dispatch", "--harness", "opencode", "--owner-worktree",
                         str(self.owner), "--task-file", str(self.brief), "--receipt-file",
                         str(self.base / "wrong-role.json"), "--role", "implementer",
                         "--parent-task-id", self.parent_task_id, success=False)
        self.assertIn("not an orchestrator", role.stderr)
        listing = json.loads(self.call(self.owner, "tasks").stdout)
        self.assertEqual(listing["tasks"], [])

    def test_bind_rejects_parent_assignment_removed_or_changed_after_prepare(self):
        ticket = self.prepare()
        assignment = self.parent_assignment_path()
        assignment.unlink()
        result = self.bind(ticket, success=False)
        self.assertIn("no session assignment", result.stderr)
        current = json.loads(self.call(self.child, "task-status", "--task-id", ticket["task_id"]).stdout)
        self.assertEqual(current["status"], "pending")
        self.assertIsNone(current["session_id"])

        assignment.write_text(json.dumps({"root": str(self.owner.resolve()), "role": "orchestrator"}))
        second_receipt = self.base / "changed-parent-receipt.json"
        second_ticket = self.prepare(receipt=second_receipt)
        self.call(self.owner, "assign", "--harness", "opencode", "--session", self.parent_session,
                  "--role", "implementer")
        changed = self.bind(second_ticket, receipt=second_receipt, success=False)
        self.assertIn("not an orchestrator", changed.stderr)
        current = json.loads(self.call(self.child, "task-status", "--task-id",
                                       second_ticket["task_id"]).stdout)
        self.assertEqual(current["status"], "pending")

    def test_opencode_parent_contract_cannot_be_downgraded_by_removing_parent_record(self):
        ticket = self.prepare()
        child_gitdir = Path(self.git(self.child, "rev-parse", "--absolute-git-dir").stdout.strip())
        task_file = child_gitdir / "context-injector" / "tasks" / ticket["task_id"] / "task.json"
        saved = json.loads(task_file.read_text())
        saved.pop("parent")
        task_file.write_text(json.dumps(saved))
        result = self.bind(ticket, success=False)
        self.assertIn("parent contract is incomplete or ambiguous", result.stderr)
        self.assertEqual(json.loads(task_file.read_text())["status"], "pending")

    def test_codex_and_claude_parents_keep_existing_open_code_ticket_and_receipt_contract(self):
        for harness, launch_kind, session_env, parent_session in (
                ("codex", "codex", "CODEX_THREAD_ID", "codex_parent0123"),
                ("claude", "claude", "CLAUDE_CODE_SESSION_ID", "claude_parent0123")):
            with self.subTest(harness=harness):
                for key in ("OPENCODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"):
                    self.env.pop(key, None)
                self.env["CMUX_AGENT_LAUNCH_KIND"] = launch_kind
                self.env[session_env] = parent_session
                ticket_receipt = self.base / (harness + "-legacy-receipt.json")
                self.call(self.owner, "session-enroll", "--harness", harness, "--session", parent_session,
                          "--context-file", str((self.owner / ".context-injector" / "context.md").resolve()))
                ticket = self.prepare(receipt=ticket_receipt, parent_task_id=None)
                self.assertNotIn("parent", ticket)
                self.assertEqual(ticket["ticket_version"], 2)
                self.assertEqual(ticket["parent_kind"], harness)
                saved_receipt = json.loads(ticket_receipt.read_text())
                self.assertNotIn("parent", saved_receipt)
                task_id_path = (Path(self.git(self.child, "rev-parse", "--absolute-git-dir").stdout.strip()) /
                                "context-injector" / "tasks" / ticket["task_id"] / "task.json")
                legacy_task = json.loads(task_id_path.read_text())
                legacy_task.pop("ticket_version")
                legacy_task.pop("parent_kind")
                task_id_path.write_text(json.dumps(legacy_task))
                saved_receipt.pop("ticket_version")
                saved_receipt.pop("parent_kind")
                ticket_receipt.write_text(json.dumps(saved_receipt))
                child_session = "ses_" + harness.title() + "Child012345"
                self.bind(ticket, session=child_session, receipt=ticket_receipt)
                self.assertIn("role: implementer", self.context(child_session)["text"])
                revised = self.base / (harness + "-revised.md")
                revised.write_text("Legacy parent ticket remains explicitly updatable.\n")
                self.call(self.child, "update-task", "--task-id", ticket["task_id"],
                          "--task-file", str(revised))
                self.assertIn("Legacy parent ticket remains", self.context(child_session)["text"])
                self.call(self.child, "finish-task", "--task-id", ticket["task_id"])
                self.assertIn("is finished", self.context(child_session)["text"])

    def test_install_and_hook_config_are_explicit_and_preserve_other_settings(self):
        settings = self.base / "isolated-opencode.json"
        settings.write_text(json.dumps({"model": "fixture/local", "plugins": ["existing-plugin"]}))
        runtime = self.base / "runtime"
        config = json.loads(self.call(self.owner, "hook-config", "--harness", "opencode",
                                      "--runtime-dir", str(runtime)).stdout)
        self.assertEqual(config, {"plugins": [str((runtime / "opencode-plugin").resolve())]})
        self.assertFalse(runtime.exists())
        self.call(self.owner, "install", "--harness", "opencode", "--settings", str(settings),
                  "--runtime-dir", str(runtime))
        saved = json.loads(settings.read_text())
        self.assertEqual(saved["model"], "fixture/local")
        self.assertEqual(saved["plugins"], ["existing-plugin", str((runtime / "opencode-plugin").resolve())])
        self.assertTrue((runtime / "pattern_buffer.py").is_file())
        self.assertTrue((runtime / "opencode-plugin/server.js").is_file())
        status = json.loads(self.call(self.owner, "status", "--harness", "opencode",
                                      "--settings", str(settings), "--runtime-dir", str(runtime)).stdout)
        self.assertEqual(status["owned_plugin_entries"], 1)
        self.call(self.owner, "uninstall", "--harness", "opencode", "--settings", str(settings),
                  "--runtime-dir", str(runtime))
        self.assertEqual(json.loads(settings.read_text())["plugins"], ["existing-plugin"])
        self.assertTrue(runtime.exists())


if __name__ == "__main__":
    unittest.main()
