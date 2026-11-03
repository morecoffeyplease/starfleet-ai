"""Live CLI boundary exercised through a fake gh process, not a fake guard."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

GUARD = Path(__file__).resolve().parents[1] / "guard.py"
PR = "https://github.com/starfleet/guard/pull/41"
TRACKER = "https://github.com/starfleet/guard/issues/42"
PR_API = "repos/starfleet/guard/pulls/41"
TRACKER_API = "repos/starfleet/guard/issues/42"
PURPOSE = "Users can finish their task reliably."


class LiveRecordTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="starfleet-live-records-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.git("init", "-b", "feature", self.repo, cwd=self.base)
        self.git("config", "user.name", "Policy Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "product.txt").write_text("fixture\n")
        self.git("add", ".")
        self.git("commit", "-m", "Initial fixture")
        self.git("remote", "add", "origin", "git@github.com:starfleet/guard.git")
        self.db_path = self.base / "github.json"
        self.db = {"records": {
            PR_API: {"html_url": PR, "body": PURPOSE + "\n\nFix rounds: 2\n\nUnrelated PR content.\n",
                     "state": "open", "draft": True, "updated_at": "fixture-v1",
                     "head": {"ref": "feature", "sha": self.git("rev-parse", "HEAD"), "repo": {"full_name": "starfleet/guard"}},
                     "base": {"repo": {"full_name": "starfleet/guard"}}},
            TRACKER_API: {"html_url": TRACKER, "state": "open", "updated_at": "fixture-v1",
                          "body": "Tracker purpose.\n\n| Item | Status |\n| --- | --- |\n| Mine <!-- starfleet-policy-guard:mine --> | Fix rounds: 2 |\n| Other owner | Fix rounds: 12 |\n"},
        }}
        self.save()
        binary = self.base / "bin/gh"
        binary.parent.mkdir()
        binary.write_text("#!" + sys.executable + "\n" + (Path(__file__).parent / "fake_gh.py").read_text())
        binary.chmod(0o755)
        self.env = dict(os.environ, PATH=str(binary.parent) + os.pathsep + os.environ["PATH"], FAKE_GITHUB_DB=str(self.db_path))

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *map(str, args)], cwd=cwd or self.repo, text=True, stderr=subprocess.DEVNULL).strip()

    def save(self):
        self.db_path.write_text(json.dumps(self.db))

    def load(self):
        self.db = json.loads(self.db_path.read_text())
        return self.db

    def call(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(GUARD), "--cwd", str(self.repo), *map(str, args)],
                                env=self.env, text=True, capture_output=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def enroll(self):
        return self.call("enroll", "--pr-url", PR, "--tracker-url", TRACKER, "--tracker-marker", "mine", "--single-writer")

    def status(self):
        return json.loads(self.call("status").stdout)

    def begin(self):
        self.enroll()
        self.call("begin", "--batch", "accepted-3", "--kind", "product-fix", "--finding", "Accepted source finding")

    def report(self, **overrides):
        value = {
            "batch": "accepted-3", "round": 3, "purpose_quote": PURPOSE, "inventory": [],
            "observed": "product.txt exists", "concluded": "Minimal fixture represents the task",
            "runtime_failure": False, "finding_quote": "Correct fixture wording", "source_path": "product.txt:1",
            "unproven": "Source finding only", "simplest_design": "One plain file", "design_reason": "Already minimal",
            "decision": "keep", "adds_scope": False, "adds_mechanism": False,
        }
        value.update(overrides)
        path = Path(self.status()["evidence_dir"]) / "report.json"
        path.write_text(json.dumps(value))
        return path

    def test_enroll_reads_live_identity_purpose_and_count_without_mutations(self):
        self.enroll()
        item = self.status()
        self.assertFalse(item["fixture"])
        self.assertEqual(item["count"], 2)
        self.assertEqual(item["purpose"], PURPOSE)
        self.assertTrue(all(c[0] == "GET" for c in self.load()["calls"]))

    def test_live_round_and_assessment_preserve_other_content(self):
        self.begin()
        self.assertEqual(self.call("check", "edit", ok=False).returncode, 2)
        self.call("record", "--report", self.report())
        self.call("check", "edit")
        db = self.load()
        pr, tracker = db["records"][PR_API]["body"], db["records"][TRACKER_API]["body"]
        self.assertIn("Fix rounds: 3 (zoom-out done", pr)
        self.assertIn("Unrelated PR content.", pr)
        self.assertIn("| Other owner | Fix rounds: 12 |", tracker)
        self.assertIn('"purpose_quote": "' + PURPOSE + '"', pr)
        self.assertTrue(pr.startswith(PURPOSE))
        self.assertNotIn("pending_sync", self.status())

    def test_tracker_failure_keeps_stop_and_sync_retries_without_recount(self):
        self.begin()
        self.load()["fail_tracker_once"] = True
        self.save()
        result = self.call("record", "--report", self.report(), ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("pending_sync", self.status())
        self.assertNotIn("assessment", self.status()["active"])
        self.assertEqual(self.call("check", "push", ok=False).returncode, 2)
        self.call("sync")
        self.call("check", "edit")
        self.assertEqual(self.status()["count"], 3)
        self.call("sync")
        self.assertEqual(self.status()["count"], 3)

    def test_lost_write_acknowledgement_is_recovered_by_reading(self):
        self.begin()
        self.load()["ambiguous_pr_once"] = True
        self.save()
        self.assertEqual(self.call("record", "--report", self.report(), ok=False).returncode, 2)
        before = sum(c == ["PATCH", PR_API] for c in self.load()["calls"])
        self.call("sync")
        after = sum(c == ["PATCH", PR_API] for c in self.load()["calls"])
        self.assertEqual(before, after, "Already-applied PR write must not be blindly repeated")
        self.assertIn("assessment", self.status()["active"])

    def test_external_count_change_does_not_reset_local_history(self):
        self.enroll()
        self.load()["records"][PR_API]["body"] = PURPOSE + "\n\nFix rounds: 9\n"
        self.save()
        result = self.call("begin", "--batch", "new", "--kind", "product-fix", "--finding", "Finding", ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.status()["count"], 2)

    def test_record_uses_fresh_purpose_not_enrollment_cache(self):
        self.begin()
        self.load()
        self.db["records"][PR_API]["body"] = self.db["records"][PR_API]["body"].replace(PURPOSE, "A newly approved purpose.")
        self.save()
        self.assertEqual(self.call("record", "--report", self.report(), ok=False).returncode, 2)
        self.call("record", "--report", self.report(purpose_quote="A newly approved purpose."))
        self.assertEqual(self.status()["purpose"], "A newly approved purpose.")

    def test_draft_head_and_tracker_must_match_at_enrollment(self):
        self.db["records"][PR_API]["draft"] = False
        self.save()
        args = ["enroll", "--pr-url", PR, "--tracker-url", TRACKER, "--tracker-marker", "mine", "--single-writer"]
        self.assertEqual(self.call(*args, ok=False).returncode, 2)
        self.db["records"][PR_API]["draft"] = True
        self.db["records"][PR_API]["head"]["sha"] = "wrong-head"
        self.save()
        self.assertEqual(self.call(*args, ok=False).returncode, 2)
        self.db["records"][PR_API]["head"]["sha"] = self.git("rev-parse", "HEAD")
        self.db["records"][TRACKER_API]["body"] = "| Mine <!-- starfleet-policy-guard:mine --> | Fix rounds: 5 |\n"
        self.save()
        self.assertEqual(self.call(*args, ok=False).returncode, 2)

    def test_observed_concurrent_edit_refuses_patch_and_preserves_pending(self):
        self.begin()
        self.load()["mutate_on_get"] = {"endpoint": PR_API, "remaining": 3}
        self.save()
        before = sum(c[0] == "PATCH" for c in self.db["calls"])
        result = self.call("record", "--report", self.report(), ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("changed before writing", result.stderr)
        self.assertEqual(sum(c[0] == "PATCH" for c in self.load()["calls"]), before)
        self.assertIn("Concurrent owner's note.", self.db["records"][PR_API]["body"])
        self.call("sync")
        self.assertIn("Concurrent owner's note.", self.load()["records"][PR_API]["body"])

    def test_remote_head_change_during_recovery_keeps_stop(self):
        self.begin()
        self.load()["fail_tracker_once"] = True
        self.save()
        self.call("record", "--report", self.report(), ok=False)
        self.load()["records"][PR_API]["head"]["sha"] = "new-candidate"
        self.save()
        self.assertEqual(self.call("sync", ok=False).returncode, 2)
        self.assertIn("pending_sync", self.status())
        self.assertNotIn("assessment", self.status()["active"])

    def test_hook_checks_remain_local_even_for_live_enrollment(self):
        self.begin()
        calls = len(self.load()["calls"])
        self.call("check", "edit", ok=False)
        self.assertEqual(len(self.load()["calls"]), calls)

    def test_live_abandon_restores_reserved_count_in_both_records(self):
        self.begin()
        self.call("abandoned", "--batch", "accepted-3")
        self.assertEqual(self.status()["count"], 2)
        self.assertIn("Fix rounds: 2", self.load()["records"][PR_API]["body"])
        self.assertIn("Fix rounds: 2", self.db["records"][TRACKER_API]["body"])

    def test_review_decision_retries_partial_live_registration_without_recount(self):
        self.enroll()
        self.call("review", "start", "--review", "review-2", "--reviewer", "sol")
        for event in ("PreToolUse", "PostToolUse"):
            payload = {"hook_event_name": event, "cwd": str(self.repo), "tool_name": "Agent", "tool_use_id": "sol-attempt",
                       "tool_input": {"prompt": "Starfleet-Dispatch: review review-2 sol\nRead the fixture."},
                       "tool_response": {"output": "Observed fixture text"}}
            result = subprocess.run([sys.executable, str(GUARD), "hook"], env=self.env, input=json.dumps(payload), text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        evidence = Path(self.status()["evidence_dir"])
        report = evidence / "review.json"
        report.write_text(json.dumps({"review": "review-2", "head": self.git("rev-parse", "HEAD"), "verdict": "not-ready",
                                      "findings": [{"id": "F1", "summary": "Correct the wording", "evidence": "product.txt:1"}]}))
        self.call("review", "complete", "--review", "review-2", "--report", report)
        decision = evidence / "decision.json"
        decision.write_text(json.dumps({"review": "review-2", "batch": "accepted-3", "accepted": ["F1"], "dismissed": {}}))
        self.load()["fail_tracker_once"] = True
        self.save()
        self.assertEqual(self.call("review", "decide", "--decision", decision, ok=False).returncode, 2)
        self.assertEqual(self.status()["count"], 3)
        self.assertIn("pending_sync", self.status())
        self.call("review", "decide", "--decision", decision)
        self.assertEqual(self.status()["count"], 3)
        self.assertNotIn("pending_sync", self.status())
        self.assertEqual(self.status()["active"]["review"], "review-2")
        self.assertEqual(self.call("check", "edit", ok=False).returncode, 2)


if __name__ == "__main__":
    unittest.main()
