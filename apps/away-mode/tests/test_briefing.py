import copy
import unittest
from unittest.mock import patch
import test_away_mode as base
am = base.am


class BriefingTests(unittest.TestCase):
    setUp = base.AwayTest.setUp
    git = base.AwayTest.git
    call = base.AwayTest.call
    request = base.AwayTest.request
    submit = base.AwayTest.submit

    def modern(self):
        self.update.update(headline="Issue #123: implementation advanced; qualification next.",
                           shipped=["PR #42 merged in this window"],
                           brief_blockers=copy.deepcopy(self.update["items"][0]["blockers"]),
                           roadmap={"source":"roadmap.md", "complete":True, "items":[
                               {"reference":"Issue #123", "title":"Recovery", "status":"Qualification pending",
                                "order":1, "parked":False}]})
        effort = {"before":[40,50], "current":[60,75], "accomplished":[15,25],
                  "basis":"Estimated total issue effort including qualification; medium confidence", "revision":""}
        self.update["items"][0]["effort"] = copy.deepcopy(effort)
        self.update["roadmap"]["effort"] = dict(effort, current=[30,40], before=[25,35], accomplished=[3,8],
                                              basis="Roadmap effort weighted by remaining complexity")

    def test_default_summary_has_effort_and_window_progress_not_full_queue(self):
        self.modern()
        self.update["items"][0]["progress"] = "DETAILEDEVIDENCE " * 80
        rid = self.request(); self.submit(rid)
        original = copy.deepcopy(self.app.read())
        text = self.call("render", rid)
        self.assertIn("60-75% complete (before: 40-50%)", text)
        self.assertIn("15-25 percentage points", text)
        self.assertIn("Roadmap: 30-40%", text)
        self.assertIn("PR \\#42 merged", text)
        self.assertIn("user approval", text)
        self.assertNotIn("## Roadmap queues", text)
        self.assertNotIn("DETAILEDEVIDENCE", text)
        details = self.call("render", rid, "--details")
        self.assertIn("DETAILEDEVIDENCE", details)
        self.assertIn("## Roadmap queues", details)
        self.assertEqual(self.app.read(), original)

    def test_empty_sections_omitted_unknown_estimates_not_invented(self):
        self.update["attention"] = []
        self.update["items"][0]["blockers"] = []
        rid = self.request(); self.submit(rid)
        text = self.call("render", rid)
        for heading in ("Needs your attention", "## Shipped", "## Blocked"):
            self.assertNotIn(heading, text)
        self.assertIn("Effort estimate unknown", text)
        self.assertIn("Full roadmap coverage missing", text)

    def test_scope_revision_and_unknown_baseline_explicit(self):
        self.modern()
        effort = self.update["items"][0]["effort"]
        effort.update(before=None, accomplished=None, revision="New verification work increased scope")
        rid = self.request(); self.submit(rid)
        text = self.call("render", rid)
        self.assertIn("before: unknown", text)
        self.assertIn("this window: unknown", text)
        self.assertIn("New verification work increased scope", text)

    def test_effort_validation_rejects_false_precision_and_malformed_ranges(self):
        self.modern()
        for bounds in ([80,20], [-1,50], [0,101], [True,50], [float('nan'),50], [40], "50%"):
            invalid=copy.deepcopy(self.update)
            invalid["items"][0]["effort"]["current"] = bounds
            with self.subTest(bounds=bounds), self.assertRaises(am.Error):
                am.validate_update(invalid)
        invalid=copy.deepcopy(self.update); invalid["roadmap"]["effort"]["basis"]=""
        with self.assertRaises(am.Error): am.validate_update(invalid)

    def test_missing_lane_remains_unknown(self):
        text=self.call("render", self.request())
        self.assertIn("No response yet; progress and blockers are unknown", text)
        self.assertIn("0 of 1", text)

    def test_request_explains_estimate_denominators_and_window(self):
        rid=self.request()
        text=self.app.request_text(self.app.read()["reports"][rid], "fixture")
        for expected in ("percentage points", "fixed window", "null for unknown", "verification and delivery"):
            self.assertIn(expected, text)

    def test_phrase_only_accepts_complete_direct_intent_and_no_launch(self):
        self.call("phrase-config", "--away", "step away", "--back", "return to desk")
        for phrase, mode in (("Return to desk!", "back"), ("step away", "away")):
            with patch.object(am.App, "launch") as launch, patch.object(am.App, "send_action") as send:
                self.call("phrase", "--text", phrase, "--request-id", mode+str(len(phrase)))
                self.assertEqual(self.call("context")["presence"]["mode"], mode)
                launch.assert_not_called(); send.assert_not_called()
        for phrase in ("I should step away", '"step away"', "return to desk but wait"):
            with self.assertRaises(am.Error):
                am.phrase_mode(phrase, {"away": ["step away"], "back": ["return to desk"]})
        with self.assertRaises(am.Error):
            am.phrase_mode("step away", {"away": [], "back": []})
        before = self.app.path.read_bytes()
        with self.assertRaisesRegex(am.Error, "must be unique"):
            self.call("phrase-config", "--away", "Step away", "--back", "step away!")
        self.assertEqual(before, self.app.path.read_bytes())

    def test_legacy_state_without_phrase_configuration_remains_read_only(self):
        state = self.app.read()
        state.pop("phrases")
        self.app.save(state)
        before = self.app.path.read_bytes()
        result = self.call("context")
        self.assertIn("No phrases are configured", result["instructions"])
        self.assertEqual(before, self.app.path.read_bytes())

    def test_location_read_only_and_exact_surface(self):
        before=self.app.path.read_bytes()
        w,s=self.lane["workspace"],self.lane["surface"]
        tree={"windows":[{"workspaces":[{"id":w, "title":"Product workspace", "panes":[
            {"surfaces":[{"id":s, "title":"Planner"}]}]}]}]}
        with patch.object(am.mp, "run_json", return_value=tree):
            result=self.call("notification-context", "--workspace",w,"--surface",s)
            self.assertEqual((result["workspace"], result["window"]), ("Product workspace", "Planner"))
            with self.assertRaises(am.Error): self.call("notification-context", "--workspace",w,"--surface",str(__import__('uuid').uuid4()))
        self.assertEqual(self.app.path.read_bytes(),before)

if __name__ == "__main__": unittest.main()
