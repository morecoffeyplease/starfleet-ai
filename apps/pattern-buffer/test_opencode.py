"""Synthetic OpenCode adapter, dispatch, and plugin callback checks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("pattern_buffer.py")
PLUGIN = Path(__file__).parent / "opencode-plugin/server.js"
NODE = shutil.which("node")


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
        self.env = {**os.environ, "CMUX_AGENT_LAUNCH_KIND": "", "CODEX_THREAD_ID": "",
                    "CLAUDE_CODE_SESSION_ID": ""}

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                              capture_output=True, text=True, env=self.env if hasattr(self, "env") else None)

    def call(self, cwd, *args, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), "--cwd", str(cwd), *args],
                                capture_output=True, text=True, env=self.env if hasattr(self, "env") else None)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        return result

    def prepare(self, receipt=None, task_file=None):
        result = self.call(self.child, "prepare-dispatch", "--harness", "opencode",
                           "--owner-worktree", str(self.owner), "--task-file", str(task_file or self.brief),
                           "--receipt-file", str(receipt or self.receipt), "--role", "implementer")
        return json.loads(result.stdout)

    def bind(self, task, session="ses_012345abcdef", receipt=None, role="implementer", cwd=None, success=True):
        return self.call(cwd or self.child, "bind-dispatch", "--harness", "opencode",
                         "--session", session, "--task-id", task["task_id"],
                         "--receipt-file", str(receipt or self.receipt), "--role", role,
                         success=success)

    def context(self, session="ses_unassigned", cwd=None):
        return json.loads(self.call(cwd or self.child, "context", "--harness", "opencode",
                                    "--session", session).stdout)

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

    def test_prepare_rejects_reused_receipt_path_before_creating_another_task(self):
        self.prepare()
        self.call(self.child, "prepare-dispatch", "--harness", "opencode", "--owner-worktree",
                  str(self.owner), "--task-file", str(self.brief), "--receipt-file", str(self.receipt),
                  "--role", "implementer", success=False)

    def test_prepare_rejects_receipt_symlink_without_following_it(self):
        target = self.base / "receipt-target.json"
        symlink = self.base / "receipt-link.json"
        symlink.symlink_to(target)
        self.call(self.child, "prepare-dispatch", "--harness", "opencode", "--owner-worktree",
                  str(self.owner), "--task-file", str(self.brief), "--receipt-file", str(symlink),
                  "--role", "implementer", success=False)
        self.assertFalse(target.exists())
        self.assertEqual(json.loads(self.call(self.owner, "tasks").stdout)["tasks"], [])

    def test_receipt_write_failure_leaves_the_pending_task_inspectable(self):
        file_parent = self.base / "not-a-directory"
        file_parent.write_text("synthetic fixture")
        failed_receipt = file_parent / "receipt.json"
        failed = self.call(self.child, "prepare-dispatch", "--harness", "opencode", "--owner-worktree",
                           str(self.owner), "--task-file", str(self.brief), "--receipt-file", str(failed_receipt),
                           "--role", "implementer", success=False)
        listing = json.loads(self.call(self.owner, "tasks").stdout)
        pending = [task for task in listing["tasks"] if Path(task["root"]).resolve() == self.child.resolve()]
        self.assertEqual(len(pending), 1, f"prepare: {failed.stderr}; task listing: {listing}")
        self.assertEqual(pending[0]["status"], "pending")
        self.assertEqual(pending[0]["receipt_file"], str(failed_receipt.resolve()))

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
