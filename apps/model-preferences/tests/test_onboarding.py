"""Owner-bound onboarding, adoption receipts and complete bounded review output."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from test_preferences import mp, SCRIPT


class OnboardingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config"
        self.state = self.root / "state"
        self.app = mp.App(self.config, self.state)
        self.call("init", "--examples")
        self.call("authority-set", "fixture authority")
        self.activate("Codex-only")
        self.recipient = {"workspace": str(uuid.uuid4()), "surface": str(uuid.uuid4()),
                          "session": "native-owner", "harness": "codex"}

    def call(self, *args):
        return mp.execute(mp.parser().parse_args(["--config-dir", str(self.config), "--state-dir", str(self.state), *args]))

    def activate(self, profile):
        with patch.object(mp.App, "current", return_value={"items": [], "truncated": False}):
            return self.call("activate", profile, "--orchestrators", "future-only")

    def inventory(self, recipient=None):
        r = recipient or self.recipient
        return {"truncated": False, "items": [{"lifecycle": "running", "freshness": {"state": "current"},
                "projections": [{"workspace_id": r["workspace"], "panel_id": r["surface"]}],
                "agents": [{"session_id": r["session"], "kind": r["harness"], "state": "working"}]}]}

    def identify(self, recipient=None):
        r = recipient or self.recipient
        return {"caller": {"surface_type": "terminal", "workspace_id": r["workspace"], "surface_id": r["surface"]}}

    def enroll(self):
        with patch.object(mp, "run_json", return_value=self.identify()), patch.object(mp.App, "current", return_value=self.inventory()):
            return self.call("enroll-self", "lane")

    def adopt(self, token=None):
        token = token or self.call("status")["active"]["selection"]
        with patch.object(mp, "run_json", return_value=self.identify()), patch.object(mp.App, "current", return_value=self.inventory()):
            return self.call("adopt", "lane", "--selection", token)

    def test_startup_compares_intent_without_changing_shared_selection(self):
        before = self.app.config_path.read_bytes()
        report = self.call("startup", "--expect-profile", "Codex-Muse")
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["active_profile"], "Codex-only")
        self.assertTrue(report["profile_mismatch"])
        self.assertEqual([r["harness"] for r in report["roles"]["advanced_review"]], ["codex"])
        self.assertEqual(self.call("startup", "--expect-profile", "Codex-only")["exit_code"], 0)
        self.assertEqual(before, self.app.config_path.read_bytes())
        cli = subprocess.run([sys.executable, str(SCRIPT), "--config-dir", str(self.config), "--state-dir", str(self.state),
                              "startup", "--expect-profile", "Claude-only"], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 2)
        self.assertTrue(json.loads(cli.stdout)["profile_mismatch"])

    def test_no_profile_and_unknown_intent_cannot_claim_dispatch_readiness(self):
        config = self.app.config(); config["active"] = None; mp.write(self.app.config_path, config)
        self.assertFalse(self.call("startup")["ready_for_dispatch"])
        with self.assertRaises(mp.PreferenceError):
            self.call("startup", "--expect-profile", "unknown")

    def test_successor_enrollment_requires_explicit_replacement_and_does_not_send_commands(self):
        outgoing = dict(self.recipient, session="outgoing", surface=str(uuid.uuid4()))
        self.app.save_state("orchestrators", {"lane": outgoing})
        with patch.object(mp, "run_json", return_value=self.identify()) as identity, \
                patch.object(mp.App, "current", return_value=self.inventory()), patch.object(mp, "run_command") as command:
            with self.assertRaises(mp.PreferenceError):
                self.call("enroll-self", "lane")
            self.assertEqual(self.app.state("orchestrators", {})["lane"], outgoing)
            result = self.call("enroll-self", "lane", "--replace")
        self.assertEqual(result["replaced"], outgoing)
        self.assertEqual(self.app.state("orchestrators", {})["lane"], self.recipient)
        self.assertTrue(all(c.args[0][-1] == "identify" for c in identity.call_args_list))
        command.assert_not_called()

    def test_self_enrollment_rejects_ambiguous_stale_truncated_and_nonterminal_callers(self):
        variants = []
        ambiguous = self.inventory(); ambiguous["items"][0]["agents"].append({"session_id": "worker", "kind": "codex", "state": "working"})
        variants.append(ambiguous)
        stale = self.inventory(); stale["items"][0]["freshness"]["state"] = "stale"; variants.append(stale)
        truncated = self.inventory(); truncated["truncated"] = True; variants.append(truncated)
        for inventory in variants:
            with self.subTest(inventory=inventory), patch.object(mp, "run_json", return_value=self.identify()), \
                    patch.object(mp.App, "current", return_value=inventory), self.assertRaises(mp.PreferenceError):
                self.call("enroll-self", "lane")
        identity = self.identify(); identity["caller"]["surface_type"] = "browser"
        for packet in (identity, {"caller": None}, {"caller": "unverified"}):
            with patch.object(mp, "run_json", return_value=packet), self.assertRaises(mp.PreferenceError):
                self.call("enroll-self", "lane")
        self.assertEqual(self.app.state("orchestrators", {}), {})

    def test_adoption_is_separate_idempotent_and_invalidated_by_selection_or_owner(self):
        self.enroll()
        self.assertEqual(self.call("status")["adoption"]["lane"]["status"], "not_acknowledged")
        first = self.adopt()
        self.assertEqual(first["at"], self.adopt()["at"])
        self.assertEqual(self.call("status")["adoption"]["lane"]["status"], "acknowledged")
        self.activate("Claude-only")
        self.assertEqual(self.call("status")["adoption"]["lane"]["status"], "stale_selection")
        with self.assertRaises(mp.PreferenceError):
            self.adopt(first["selection"])
        self.app.save_state("orchestrators", {"lane": dict(self.recipient, session="successor")})
        self.assertEqual(self.call("status")["adoption"]["lane"]["status"], "stale_owner")
        with self.assertRaises(mp.PreferenceError):
            self.adopt()

    def test_uuid_case_does_not_change_recipient_ownership(self):
        upper = {**self.recipient, "workspace": self.recipient["workspace"].upper(), "surface": self.recipient["surface"].upper()}
        self.app.save_state("orchestrators", {"lane": upper})
        self.assertIsNone(self.enroll()["replaced"])
        self.adopt()
        self.app.save_state("orchestrators", {"lane": upper})
        self.assertEqual(self.call("status")["adoption"]["lane"]["status"], "acknowledged")

    def test_notification_retry_targets_replacement_even_when_old_owner_was_queued(self):
        self.enroll()
        with patch.object(mp.App, "current", return_value=self.inventory()), \
                patch.object(mp, "run_json", return_value={"id": str(uuid.uuid4()), "state": "queued"}):
            self.call("notify", "--retry")
        notice = self.app.state("notification", {})
        self.assertEqual(notice["recipients"]["lane"]["recipient"], self.recipient)
        successor = dict(self.recipient, surface=str(uuid.uuid4()), session="successor")
        self.app.save_state("orchestrators", {"lane": successor})
        with patch.object(mp.App, "current", return_value=self.inventory(successor)), \
                patch.object(mp, "run_json", return_value={"id": str(uuid.uuid4()), "state": "queued"}) as send:
            result = self.call("notify", "--retry")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0][3], successor["surface"])
        self.assertEqual(result["notification"]["recipients"]["lane"]["recipient"], successor)
        self.assertEqual(self.call("status")["adoption"]["lane"]["status"], "not_acknowledged")

    def test_legacy_queue_receipt_is_reconciled_once_without_claiming_adoption(self):
        self.enroll()
        notice = self.app.state("notification", {})
        notice["recipients"]["lane"] = {"status": "queued", "receipt": {"id": str(uuid.uuid4())}}
        self.app.save_state("notification", notice)
        with patch.object(mp.App, "current", return_value=self.inventory()), \
                patch.object(mp, "run_json", return_value={"id": str(uuid.uuid4()), "state": "queued"}) as send:
            self.call("notify", "--retry")
            self.call("notify", "--retry")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.call("status")["adoption"]["lane"]["status"], "not_acknowledged")

    def test_owner_changed_during_notification_is_not_sent_the_old_notice(self):
        self.enroll()
        def replace_before_send(_app):
            self.app.save_state("orchestrators", {"lane": dict(self.recipient, session="successor")})
            return self.inventory()
        with patch.object(mp.App, "current", replace_before_send), patch.object(mp, "run_json") as send:
            result = self.call("notify", "--retry")
        send.assert_not_called()
        self.assertIn("Registration changed", result["notification"]["recipients"]["lane"]["error"])

    def test_large_untracked_scope_is_bounded_but_artifact_mutation_still_invalidates_approval(self):
        repo = self.root / "repo"; repo.mkdir()
        for argv in (("init", "-q"), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid")):
            subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True)
        (repo / "product.py").write_text("original\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
        (repo / "product.py").write_text("changed\n")
        artifacts = repo / ".planning/administrator/8199"; artifacts.mkdir(parents=True)
        for n in range(150): (artifacts / f"capture-{n}.json").write_text(str(n))
        facts = self.root / "facts.json"; facts.write_text(json.dumps(dict.fromkeys(mp.FLAGS, False)))
        plan = self.call("review-plan", "--cwd", str(repo), "--base", "HEAD", "--facts", str(facts))
        self.assertEqual(plan["paths_count"], 151)
        self.assertEqual(plan["untracked_paths_count"], 150)
        self.assertEqual(len(plan["paths"]), 20)
        self.assertEqual(plan["tracked_paths"], ["product.py"])
        full = self.call("review-inspect", plan["id"], "--full")
        self.assertEqual(len(full["paths"]), 151)
        self.assertEqual(full["scope"], plan["scope"])
        self.assertIn("file_threshold", plan["reasons"])
        approval = self.call("approve-review", plan["id"], "--authorization", "Fixture authorization")
        (artifacts / "capture-0.json").write_text("unrelated capture changed")
        brief = self.root / "brief.md"; brief.write_text("Fixture review")
        with self.assertRaisesRegex(mp.PreferenceError, "scope/profile changed"):
            self.call("dispatch", "--role", "advanced_review", "--slot", "1", "--cwd", str(repo),
                      "--task-file", str(brief), "--review-plan", plan["id"], "--approval", approval["id"])
        self.assertNotEqual(mp.review_scope(self.app.config(), repo, "HEAD", json.loads(facts.read_text()))["scope"], plan["scope"])

    def test_legacy_review_plan_is_readable_without_inventing_path_classification(self):
        record_id = uuid.uuid4().hex
        legacy = {"id": record_id, "paths": ["old.py"], "counted_files": ["old.py"], "scope": "original"}
        self.app.save_state("reviews/" + record_id, legacy)
        result = self.call("review-inspect", record_id)
        self.assertEqual(result["scope"], "original")
        self.assertEqual(result["path_classification"], "unavailable_in_legacy_plan")
        self.assertIsNone(result["untracked_paths_count"])


if __name__ == "__main__":
    unittest.main()
