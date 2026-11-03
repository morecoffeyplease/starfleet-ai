"""Behavior checks using disposable real Git repositories and worktrees."""

import concurrent.futures
import json
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

GUARD = Path(__file__).resolve().parents[1] / "guard.py"


def run(*args, cwd=None, input=None, env=None):
    return subprocess.run(list(map(str, args)), cwd=cwd, input=input, env=env, text=True, capture_output=True)


class GuardFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="starfleet-policy-test-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.git("init", "-b", "lane-a", self.repo, outside=True)
        self.git("config", "user.name", "Policy Guard Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / ".gitignore").write_text(".claude/\n.codex/\n")
        (self.repo / "product.txt").write_text("original\n")
        self.git("add", ".")
        self.git("commit", "-m", "Initialize fixture")
        self.url = "https://fixture.invalid/starfleet/guard/pull/901"
        self.body, self.tracker = self.fixture("a", 1)
        self.call("enroll", "--fixture", "--pr-url", self.url, "--body", self.body,
                  "--tracker", self.tracker, "--count", 1)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, outside=False, ok=True):
        r = run("git", *args, cwd=self.base if outside else self.repo)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def fixture(self, suffix, count):
        body, tracker = self.base / (suffix + "-body.md"), self.base / (suffix + "-tracker.md")
        body.write_text(f"Users can finish their task reliably.\n\nFix rounds: {count}\n")
        tracker.write_text(f"Lane {suffix}\n\nFix rounds: {count}\n")
        return body, tracker

    def call(self, *args, cwd=None, ok=True, input=None, env=None):
        r = run(sys.executable, GUARD, "--cwd", cwd or self.repo, *args, input=input, env=env)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def batch(self, identifier="round-2"):
        self.call("begin", "--batch", identifier, "--kind", "product-fix", "--finding", "Accepted review finding")

    def status(self, cwd=None):
        return json.loads(self.call("status", cwd=cwd).stdout)

    def complete_two(self):
        self.batch()
        (self.repo / "product.txt").write_text("round two\n")
        self.git("add", "product.txt")
        self.git("commit", "-m", "Fix the fixture\n\nStarfleet-Fix-Round: 2")
        self.call("completed", "--batch", "round-2")

    def due_three(self):
        self.complete_two()
        self.batch("round-3")

    def report(self, **overrides):
        item = self.status()
        report = {
            "batch": item["active"]["id"], "round": item["active"]["round"],
            "purpose_quote": "Users can finish their task reliably.",
            "inventory": [{"name": "Fixture text", "lines": 1, "round": 2,
                           "needed_because": "Represents the user outcome", "deletion_effect": "Fixture disappears"}],
            "observed": "product.txt has one line", "concluded": "Keep the minimal fixture",
            "runtime_failure": False, "finding_quote": "Text needs correction", "source_path": "product.txt:1",
            "unproven": "This is a synthetic source-only finding, not a runtime failure",
            "simplest_design": "One plain file", "design_reason": "Already using it", "decision": "keep",
            "adds_scope": False, "adds_mechanism": False,
        }
        report.update(overrides)
        path = Path(item["evidence_dir"]) / "assessment.json"
        path.write_text(json.dumps(report))
        return path

    def event(self, name, payload, cwd=None):
        return self.call("hook", ok=False, input=json.dumps({"hook_event_name": "PreToolUse", "cwd": str(cwd or self.repo),
                                                           "tool_name": name, "tool_input": payload}))


class GuardTest(GuardFixture):
    def test_round_two_allows_and_three_blocks_all_mutation_paths(self):
        self.batch()
        for action in ("edit", "dispatch", "push"):
            self.call("check", action)
        (self.repo / "product.txt").write_text("two\n")
        self.git("add", ".")
        self.git("commit", "-m", "Second fix\n\nStarfleet-Fix-Round: 2")
        self.call("completed", "--batch", "round-2")
        self.batch("round-3")
        for name, payload in (("Edit", {"file_path": str(self.repo / "product.txt")}),
                              ("Write", {"file_path": str(self.repo / "new.txt")}),
                              ("apply_patch", {"command": "*** Begin Patch\n*** Update File: product.txt\n@@\n-old\n+new\n*** End Patch"}),
                              ("Agent", {"prompt": "Implement fixes"}),
                              ("Bash", {"command": "bash /tmp/launch-implementation.sh"}),
                              ("Bash", {"command": "git push origin HEAD"})):
            r = self.event(name, payload)
            self.assertEqual(r.returncode, 2, (name, r.stdout, r.stderr))
            self.assertIn("Fix round 3 on PR #901", r.stderr)
            self.assertIn("record --report", r.stderr)

    def test_record_clears_only_current_round_and_retains_report(self):
        self.due_three()
        path = self.report()
        self.call("record", "--report", path)
        self.call("check", "edit")
        self.assertIn("zoom-out done", self.body.read_text())
        self.assertIn("zoom-out done", self.tracker.read_text())
        path.unlink()
        self.assertEqual(self.status()["active"]["assessment"]["report"]["decision"], "keep")
        (self.repo / "product.txt").write_text("three\n")
        self.git("add", ".")
        self.git("commit", "-m", "Third fix\n\nStarfleet-Fix-Round: 3")
        self.call("completed", "--batch", "round-3")
        self.batch("round-4")
        self.assertEqual(self.call("check", "edit", ok=False).returncode, 2)
        self.assertIn("zoom-out due", self.body.read_text())
        stale = self.report(batch="round-3", round=3)
        self.assertEqual(self.call("record", "--report", stale, ok=False).returncode, 2)

    def test_restart_and_worktree_binding_preserve_pause(self):
        self.due_three()
        sibling = self.base / "sibling"
        self.git("worktree", "add", "-b", "lane-a-copy", sibling)
        self.call("bind", "--pr-url", self.url, cwd=sibling)
        self.assertEqual(self.status(sibling)["count"], 3)
        self.assertEqual(self.call("check", "dispatch", cwd=sibling, ok=False).returncode, 2)

    def test_other_work_item_unblocked_even_from_paused_session(self):
        self.due_three()
        sibling = self.base / "other"
        self.git("worktree", "add", "-b", "lane-b", sibling)
        body, tracker = self.fixture("b", 0)
        self.call("enroll", "--fixture", "--pr-url", "https://fixture.invalid/starfleet/guard/pull/902",
                  "--body", body, "--tracker", tracker, cwd=sibling)
        self.call("begin", "--batch", "initial-b", "--kind", "initial", "--finding", "Approved original purpose", cwd=sibling)
        self.assertEqual(self.event("Write", {"file_path": str(sibling / "product.txt")}).returncode, 0)
        self.assertEqual(self.event("Bash", {"command": f"git -C {sibling} push origin HEAD"}).returncode, 0)
        self.assertEqual(self.event("Bash", {"command": "git push", "workdir": str(sibling)}).returncode, 0)

    def test_registration_required_before_edits_and_retry_counts_once(self):
        self.assertEqual(self.call("check", "edit", ok=False).returncode, 2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.call("begin", "--batch", "same", "--kind", "product-fix", "--finding", "Same accepted finding", ok=False), range(4)))
        self.assertTrue(all(r.returncode == 0 for r in results))
        self.assertEqual(self.status()["count"], 2)
        self.assertEqual(self.call("begin", "--batch", "same", "--kind", "product-fix", "--finding", "Different finding", ok=False).returncode, 2)

    def test_native_git_hooks_reject_missing_trailer_and_paused_push(self):
        remote = self.base / "remote.git"
        self.git("init", "--bare", remote, outside=True)
        self.git("remote", "add", "origin", remote)
        self.git("push", "-u", "origin", "HEAD")
        self.call("install", "--fixture")
        self.batch()
        (self.repo / "product.txt").write_text("two\n")
        self.git("add", ".")
        self.assertNotEqual(self.git("commit", "-m", "Missing trailer", ok=False).returncode, 0)
        self.git("commit", "-m", "Second fix\n\nStarfleet-Fix-Round: 2")
        self.call("completed", "--batch", "round-2")
        self.batch("round-3")
        before = self.git("ls-remote", "origin", "refs/heads/lane-a").stdout
        r = self.git("push", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Fix round 3", r.stderr)
        self.assertEqual(before, self.git("ls-remote", "origin", "refs/heads/lane-a").stdout)

    def test_reads_and_narrow_evidence_writes_remain_available(self):
        self.due_three()
        self.assertEqual(self.event("Bash", {"command": "git status --short"}).returncode, 0)
        self.assertEqual(self.event("Read", {"file_path": str(self.repo / "product.txt")}).returncode, 0)
        evidence = Path(self.status()["evidence_dir"]) / "assessment.json"
        self.assertEqual(self.event("Write", {"file_path": str(evidence)}).returncode, 0)
        for command in ("git status; touch product.txt", "git diff --output=product.txt", "rg --pre sh text", "python3 -c 'open(\"product.txt\",\"w\")'"):
            self.assertEqual(self.event("Bash", {"command": command}).returncode, 2)

    def test_evidence_runs_in_detached_copy_without_unlocking_source(self):
        self.due_three()
        before = (self.repo / "product.txt").read_text()
        r = self.call("evidence", "--revision", "HEAD", "--", sys.executable, "-c",
                      "from pathlib import Path; Path('product.txt').write_text('proof-only copy')")
        receipt = json.loads(Path(json.loads(r.stdout)["receipt"]).read_text())
        self.assertEqual(receipt["exit_code"], 0)
        self.assertIn("product.txt", receipt["changed_copy"])
        self.assertEqual((self.repo / "product.txt").read_text(), before)
        self.assertEqual(self.call("check", "edit", ok=False).returncode, 2)

    def test_malformed_state_and_report_fail_closed(self):
        self.due_three()
        report = self.report(purpose_quote="Invented purpose")
        self.assertEqual(self.call("record", "--report", report, ok=False).returncode, 2)
        report = self.report(runtime_failure=True)
        self.assertEqual(self.call("record", "--report", report, ok=False).returncode, 2)
        state = self.repo / ".git/starfleet-policy-guard/state.json"
        state.write_text("broken")
        self.assertEqual(self.event("Write", {"file_path": str(self.repo / "product.txt")}).returncode, 2)

    def test_hook_launcher_converts_missing_script_to_blocking_exit(self):
        r = run("/bin/sh", GUARD.parent / "hook.sh", sys.executable, self.base / "missing.py", input="{}")
        self.assertEqual(r.returncode, 2)

    def test_installed_hook_denies_lost_state_without_blocking_reads_or_other_repo(self):
        self.due_three()
        self.call("install", "--fixture")
        config = json.loads((self.repo / ".codex/hooks.json").read_text())
        argv = shlex.split(config["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
        location = self.repo / ".git/starfleet-policy-guard"
        backup = self.base / "preserved-state"
        location.rename(backup)
        def invoke(tool, payload):
            return run(*argv, cwd=self.repo, input=json.dumps({"hook_event_name": "PreToolUse",
                "cwd": str(self.repo), "tool_name": tool, "tool_input": payload}))
        try:
            for tool, payload in (("Write", {"file_path": str(self.repo / "product.txt")}),
                                  ("Agent", {"prompt": "Implement a change"}),
                                  ("collaborationspawn_agent", {"message": "Implement a change"})):
                result = invoke(tool, payload)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Guard state is unavailable", result.stderr)
            self.assertEqual(invoke("Bash", {"command": "cat product.txt"}).returncode, 0)
            other = self.base / "other"
            self.git("init", "-b", "other", other, outside=True)
            self.assertEqual(invoke("Write", {"file_path": str(other / "product.txt")}).returncode, 0)
            self.assertFalse(location.exists())
        finally:
            backup.rename(location)
        self.assertEqual(self.status()["count"], 3)
        ledger = location / "state.json"
        saved = self.base / "saved-state.json"
        ledger.rename(saved)
        try:
            self.assertEqual(invoke("Bash", {"command": "cat product.txt"}).returncode, 0)
            self.assertEqual(invoke("Write", {"file_path": str(self.repo / "product.txt")}).returncode, 2)
            self.assertFalse(ledger.exists(), "A read must not recreate missing history")
        finally:
            saved.rename(ledger)
        self.assertEqual(self.status()["count"], 3)

    def test_scope_or_split_decision_cannot_clear_without_human_authority(self):
        self.due_three()
        for changes in ({"adds_scope": True}, {"adds_mechanism": True}, {"decision": "split out"}):
            r = self.call("record", "--report", self.report(**changes), ok=False)
            self.assertEqual(r.returncode, 2)
            self.assertIn("user approval required", r.stderr)

    def test_high_count_enrollment_cannot_push_without_assessment(self):
        sibling = self.base / "high-count"
        self.git("worktree", "add", "-b", "high-count", sibling)
        body, tracker = self.fixture("high-count", 7)
        self.call("enroll", "--fixture", "--pr-url", "https://fixture.invalid/starfleet/guard/pull/907",
                  "--body", body, "--tracker", tracker, "--count", 7, cwd=sibling)
        self.assertEqual(self.call("check", "push", cwd=sibling, ok=False).returncode, 2)

    def test_push_from_other_branch_checks_actual_pushed_ref(self):
        self.due_three()
        self.call("install", "--fixture")
        sibling = self.base / "push-other"
        self.git("worktree", "add", "-b", "push-other", sibling)
        body, tracker = self.fixture("push-other", 0)
        self.call("enroll", "--fixture", "--pr-url", "https://fixture.invalid/starfleet/guard/pull/908",
                  "--body", body, "--tracker", tracker, cwd=sibling)
        # Native pre-push checks the actual ref even from an otherwise allowed branch.
        r = self.call("check", "push", "--refs-stdin", cwd=sibling, ok=False,
                      input="refs/heads/lane-a abc refs/heads/lane-a def\n")
        self.assertEqual(r.returncode, 2)
        self.assertIn("Fix round 3", r.stderr)
        # Conversely, paused A must not deny a push containing only B's clean ref.
        self.call("check", "push", "--refs-stdin", input="refs/heads/push-other abc refs/heads/push-other def\n")

    def test_abandon_cannot_erase_committed_work(self):
        self.batch()
        (self.repo / "product.txt").write_text("two\n")
        self.git("add", ".")
        self.git("commit", "-m", "Second fix\n\nStarfleet-Fix-Round: 2")
        self.assertEqual(self.call("abandoned", "--batch", "round-2", ok=False).returncode, 2)

    def test_excluded_batch_does_not_increment_and_initial_cannot_reset(self):
        self.call("begin", "--batch", "proof", "--kind", "proof-harness", "--finding", "Only the runner needs repair")
        self.assertEqual(self.status()["count"], 1)
        self.call("completed", "--batch", "proof")
        self.assertEqual(self.call("begin", "--batch", "reset", "--kind", "initial", "--finding", "Initial", ok=False).returncode, 2)


if __name__ == "__main__":
    unittest.main()
