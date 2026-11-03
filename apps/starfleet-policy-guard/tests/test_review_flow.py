"""Review/repair boundaries through CLI, native hook events, and real child processes."""

import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys

from test_guard import GUARD, GuardFixture


class ReviewFlowTest(GuardFixture):
    def prepare(self):
        self.batch()
        (self.repo / "product.txt").write_text("round two\n")
        self.git("add", ".")
        self.git("commit", "-m", "Second fix\n\nStarfleet-Fix-Round: 2")

    def start(self, *workers):
        self.prepare()
        argv = ["review", "start", "--review", "review-2"]
        for worker in workers or ("sol",):
            argv += ["--reviewer", worker]
        self.call(*argv)

    def agent(self, kind="review", identity="review-2", worker="sol", event="PreToolUse", token="agent-1", **extra):
        prompt = "Starfleet-Dispatch: " + kind + " " + identity + (" " + worker if kind == "review" else "") + "\nRead the fixture."
        payload = {"prompt": prompt, **extra}
        return self.call("hook", ok=False, input=json.dumps({"hook_event_name": event, "cwd": str(self.repo),
                         "tool_name": "Agent", "tool_use_id": token, "tool_input": payload,
                         "tool_response": {"output": "Observed round two"} if event != "PreToolUse" else None}))

    def receipt(self, worker="sol"):
        self.assertEqual(self.agent(worker=worker, token=worker).returncode, 0)
        self.assertEqual(self.agent(worker=worker, token=worker, event="PostToolUse").returncode, 0)

    def synthesis(self, verdict="not-ready", findings=None, **overrides):
        value = {"review": "review-2", "head": self.git("rev-parse", "HEAD").stdout.strip(),
                 "verdict": verdict, "findings": findings if findings is not None else
                 [{"id": "F1", "summary": "Correct fixture text", "evidence": "product.txt:1"}]}
        value.update(overrides)
        path = Path(self.status()["evidence_dir"]) / "synthesis.json"
        path.write_text(json.dumps(value))
        return path

    def decide(self, accepted=None, dismissed=None, **overrides):
        value = {"review": "review-2", "accepted": ["F1"] if accepted is None else accepted,
                 "dismissed": dismissed or {}}
        if value["accepted"]:
            value["batch"] = "fix-3"
        value.update(overrides)
        path = Path(self.status()["evidence_dir"]) / "decision.json"
        path.write_text(json.dumps(value))
        return path

    def fake_codex(self):
        binary = self.base / "fake-bin/codex"
        binary.parent.mkdir(exist_ok=True)
        binary.write_text("#!" + sys.executable + "\n" +
                          "from pathlib import Path\nimport sys\n" +
                          "Path(" + repr(str(self.base / "child-ran")) + ").write_text('ran')\n" +
                          "args=sys.argv[1:]\n" +
                          "if '-o' in args: Path(args[args.index('-o')+1]).write_text('Observed round two')\n")
        binary.chmod(0o755)
        return binary

    def child(self, binary, kind=None, identity="review-2", worker="sol", sandbox="read-only"):
        env = dict(os.environ)
        for key in ("STARFLEET_DISPATCH_KIND", "STARFLEET_BATCH_ID", "STARFLEET_REVIEW_ID", "STARFLEET_REVIEWER_ID"):
            env.pop(key, None)
        if kind:
            env.update(STARFLEET_DISPATCH_KIND=kind, STARFLEET_REVIEW_ID=identity,
                       STARFLEET_BATCH_ID=identity, STARFLEET_REVIEWER_ID=worker)
        report = Path(self.status()["evidence_dir"]) / (worker + "-report.md")
        return self.call("codex-child", "--real-binary", binary, "--", "exec", "-s", sandbox,
                         "-o", report, "-", input="Read fixture", env=env, ok=False)

    def test_review_start_closes_old_batch_and_blocks_edit_dispatch_push(self):
        self.start()
        item = self.status()
        self.assertIsNone(item["active"])
        self.assertEqual(item["history"][-1]["id"], "round-2")
        for action in ("edit", "dispatch", "push"):
            self.assertEqual(self.call("check", action, ok=False).returncode, 2)
        for kind in ("initial", "product-fix", "proof-harness", "rebase"):
            self.assertEqual(self.call("begin", "--batch", "bypass", "--kind", kind, "--finding", "Reason", ok=False).returncode, 2)

    def test_accepting_findings_opens_round_three_once_even_with_concurrent_retries(self):
        self.start()
        self.receipt()
        self.call("review", "complete", "--review", "review-2", "--report", self.synthesis())
        path = self.decide()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.call("review", "decide", "--decision", path, ok=False), range(3)))
        self.assertTrue(all(r.returncode == 0 for r in results), [r.stderr for r in results])
        self.assertEqual(self.status()["count"], 3)
        self.assertEqual(self.status()["active"]["id"], "fix-3")
        self.assertEqual(self.call("check", "edit", ok=False).returncode, 2)
        self.call("record", "--report", self.report())
        self.call("check", "edit")

    def test_clean_recheck_has_no_new_count_and_completion_is_idempotent(self):
        self.start()
        self.receipt()
        report = self.synthesis(verdict="ready", findings=[])
        self.call("review", "complete", "--review", "review-2", "--report", report)
        self.call("review", "complete", "--review", "review-2", "--report", report)
        self.assertEqual(self.status()["count"], 2)
        self.assertIsNone(self.status()["current_review"])
        self.call("check", "push")
        self.assertEqual(self.call("check", "edit", ok=False).returncode, 2)

    def test_all_findings_need_dispositions_and_dismissal_does_not_count(self):
        self.start()
        self.receipt()
        self.call("review", "complete", "--review", "review-2", "--report", self.synthesis())
        self.assertEqual(self.call("review", "decide", "--decision", self.decide(accepted=[]), ok=False).returncode, 2)
        path = self.decide(accepted=[], dismissed={"F1": "The existing purpose excludes this request"})
        self.call("review", "decide", "--decision", path)
        self.call("review", "decide", "--decision", path)
        self.assertEqual(self.status()["count"], 2)
        self.assertIsNone(self.status()["active"])

    def test_absent_reviewer_cannot_be_reported_as_clean(self):
        self.start("sol", "opus")
        self.receipt("sol")
        report = self.synthesis(verdict="ready", findings=[])
        self.assertEqual(self.call("review", "complete", "--review", "review-2", "--report", report, ok=False).returncode, 2)
        self.receipt("opus")
        self.call("review", "complete", "--review", "review-2", "--report", report)

    def test_stale_candidate_or_empty_not_ready_synthesis_is_rejected(self):
        self.start()
        self.receipt()
        self.assertEqual(self.call("review", "complete", "--review", "review-2", "--report", self.synthesis(findings=[]), ok=False).returncode, 2)
        self.assertEqual(self.call("review", "complete", "--review", "review-2", "--report", self.synthesis(head="wrong"), ok=False).returncode, 2)

    def test_codex_review_requires_registration_identity_and_read_only_sandbox(self):
        self.start()
        binary = self.fake_codex()
        for kind, identity, worker, sandbox in [(None, "review-2", "sol", "read-only"),
                                               ("review", "wrong", "sol", "read-only"),
                                               ("review", "review-2", "unknown", "read-only"),
                                               ("review", "review-2", "sol", "workspace-write")]:
            self.assertEqual(self.child(binary, kind, identity, worker, sandbox).returncode, 2)
        self.assertFalse((self.base / "child-ran").exists())
        self.assertEqual(self.child(binary, "review").returncode, 0)
        job = self.status()["reviews"]["review-2"]["jobs"]["sol"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["report"], "Observed round two")

    def test_opaque_script_is_caught_by_fixture_path_wrapper(self):
        self.prepare()
        binary = self.fake_codex()
        env = dict(os.environ, PATH=str(binary.parent) + os.pathsep + os.environ["PATH"])
        self.call("install", "--fixture", env=env)
        wrapper_dir = self.repo / ".git/starfleet-policy-guard/bin"
        env["PATH"] = str(wrapper_dir) + os.pathsep + env["PATH"]
        for key in ("STARFLEET_DISPATCH_KIND", "STARFLEET_BATCH_ID", "STARFLEET_REVIEW_ID"):
            env.pop(key, None)
        script = self.base / "opaque.sh"
        script.write_text("codex exec -s read-only -o report.md -\n")
        result = subprocess.run(["bash", str(script)], cwd=self.repo, env=env, text=True, input="Review", capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.base / "child-ran").exists())

    def test_implementation_dispatch_needs_current_batch_even_after_zoom_out(self):
        self.complete_two()
        self.batch("fix-3")
        self.call("record", "--report", self.report())
        binary = self.fake_codex()
        self.assertEqual(self.child(binary, "implement", identity="round-2", sandbox="workspace-write").returncode, 2)
        self.assertFalse((self.base / "child-ran").exists())
        self.assertEqual(self.child(binary, "implement", identity="fix-3", sandbox="workspace-write").returncode, 0)

    def test_pending_implementation_job_prevents_review_start(self):
        self.prepare()
        self.assertEqual(self.agent(kind="implement", identity="round-2", token="job-1").returncode, 0)
        self.assertEqual(self.call("review", "start", "--review", "review-2", "--reviewer", "sol", ok=False).returncode, 2)
        self.assertEqual(self.agent(kind="implement", identity="round-2", token="job-1", event="PostToolUse").returncode, 0)
        self.call("review", "start", "--review", "review-2", "--reviewer", "sol")

    def test_background_agent_acceptance_is_not_completion(self):
        self.start()
        self.assertEqual(self.agent(run_in_background=True).returncode, 2)
        self.assertNotIn("sol", self.status()["reviews"]["review-2"]["jobs"])

    def test_empty_native_agent_content_is_not_a_reviewer_report(self):
        self.start()
        self.assertEqual(self.agent().returncode, 0)
        result = self.call("hook", ok=False, input=json.dumps({"hook_event_name": "PostToolUse", "cwd": str(self.repo),
                          "tool_name": "Agent", "tool_use_id": "agent-1",
                          "tool_input": {"prompt": "Starfleet-Dispatch: review review-2 sol\nRead the fixture."},
                          "tool_response": {"status": "completed", "agentId": "native-id", "content": []}}))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.status()["reviews"]["review-2"]["jobs"]["sol"]["status"], "failed")

    def test_handoff_to_another_worktree_preserves_review_and_count(self):
        self.start()
        self.receipt()
        self.call("review", "complete", "--review", "review-2", "--report", self.synthesis())
        sibling = self.base / "handoff"
        self.git("worktree", "add", "-b", "handoff", sibling)
        self.call("bind", "--pr-url", self.url, cwd=sibling)
        self.call("review", "decide", "--decision", self.decide(), cwd=sibling)
        self.assertEqual(self.status()["count"], 3)
        self.assertEqual(self.call("check", "edit", cwd=sibling, ok=False).returncode, 2)

    def test_startup_reminder_does_not_dump_archived_reviewer_content(self):
        self.start()
        self.receipt()
        result = self.call("hook", input=json.dumps({"hook_event_name": "SessionStart", "cwd": str(self.repo)}))
        context = json.loads(json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(context["current_review"], "review-2")
        self.assertEqual(context["reviewer_status"], {"sol": "completed"})
        self.assertNotIn("Observed round two", result.stdout)
