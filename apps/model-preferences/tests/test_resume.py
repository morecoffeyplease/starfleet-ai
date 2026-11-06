"""Continuations pin identities, models and task ownership before native execution."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

from test_preferences import mp
import test_preferences as fixtures


class ResumeTest(unittest.TestCase):
    setUp = fixtures.PreferencesTest.setUp
    args = fixtures.PreferencesTest.args
    call = fixtures.PreferencesTest.call
    activate = fixtures.PreferencesTest.activate
    repo = fixtures.PreferencesTest.repo
    plan = fixtures.PreferencesTest.plan
    facts = fixtures.PreferencesTest.facts
    fake_native_provider = fixtures.PreferencesTest.fake_native_provider
    run_cli = fixtures.PreferencesTest.run_cli

    def fixture(self, role="researcher"):
        self.activate("Codex-only")
        choice = self.call("resolve", "--role", role)
        launch = {"id": uuid.uuid4().hex, "choice": choice, "cwd": str(self.root),
                  "status": "exited", "exit_code": 0, "context_injector": None, "slot": None}
        self.app.save_state(f"launches/{launch['id']}", launch)
        session = str(uuid.uuid4())
        mp.bind_native(self.app, launch["id"], session)
        self.brief = self.root / "updated.txt"; self.brief.write_text("Authorized updated assignment.\n")
        self.launch, self.session = launch, session
        return launch, session

    def resume(self, *args):
        return self.call("resume", self.launch["id"], "--task-file", str(self.brief), *args)

    def stopped(self):
        return patch.object(mp.App, "current", return_value={"truncated": False, "items": []})

    def test_exact_session_model_effort_and_updated_brief_with_original_record_preserved(self):
        launch, session = self.fixture()
        original = self.app.state(f"launches/{launch['id']}", None)
        with self.stopped(), patch.dict(os.environ, STARFLEET_CONTEXT_DISPATCH="parent-fresh-token"), \
                patch.object(mp.shutil, "which", return_value="codex"), \
                patch.object(mp, "stream_codex", return_value=subprocess.CompletedProcess([], 0)) as child:
            first = self.resume()
            second = self.resume()
        argv = child.call_args.args[2]
        self.assertEqual(argv, ["codex", "exec", "-C", str(self.root), "-m", "gpt-6-luna", "-c",
                                'model_reasoning_effort="high"', "resume", session, "--json", "-"])
        self.assertIn(self.brief.read_text(), child.call_args.kwargs["prompt"])
        self.assertIn("Ask your parent orchestrator", child.call_args.kwargs["prompt"])
        self.assertEqual(child.call_args.kwargs["expected"], session)
        self.assertEqual(child.call_args.args[3]["STARFLEET_MODEL_LAUNCH"], launch["id"])
        self.assertNotIn("STARFLEET_CONTEXT_DISPATCH", child.call_args.args[3])
        self.assertEqual(self.app.state(f"launches/{launch['id']}", None), original)
        attempt = self.app.state(f"attempts/{second['continuation']}", None)
        self.assertEqual(attempt["previous"], first["continuation"])
        self.assertEqual(attempt["status"], "exited")

    def test_missing_binding_identity_non_codex_and_unfinished_launch_are_refused(self):
        self.fixture()
        for change, error in (({"native_session": None}, "Missing native"),
                              ({"status": "starting"}, "not exited"),
                              ({"context_injector": "remove"}, "Legacy launch"),
                              ({"choice": {"harness": "claude"}}, "Codex launches")):
            original = self.app.state(f"launches/{self.launch['id']}", None)
            altered = {**original, **change}
            if change.get("context_injector") == "remove":del altered["context_injector"]
            self.app.save_state(f"launches/{self.launch['id']}", altered)
            with self.stopped(), patch.object(mp, "stream_codex") as child, self.assertRaisesRegex(mp.PreferenceError, error):
                self.resume()
            child.assert_not_called()
            self.app.save_state(f"launches/{self.launch['id']}", original)
        key = mp.digest({"harness": "codex", "session": self.session})
        self.app.save_state(f"bindings/{key}", {"launch": "0" * 32})
        with self.assertRaisesRegex(mp.PreferenceError, "matching launch binding"):self.resume()

    def test_profile_switch_refuses_before_task_update_or_native_launch(self):
        self.fixture(); self.activate("Claude-only")
        with patch.object(mp.App, "run_json") as ci, patch.object(mp, "stream_codex") as child, \
                self.assertRaisesRegex(mp.PreferenceError, "profile instructions conflict"):
            self.resume()
        ci.assert_not_called(); child.assert_not_called()

    def test_live_stale_and_incomplete_inventory_cannot_resume(self):
        self.fixture()
        for inventory in ({"truncated": True, "items": []},
                          {"truncated": False, "items": [{"freshness": {"state": "stale"}, "agents": [
                              {"kind": "codex", "session_id": self.session, "state": "ended"}]}]},
                          {"truncated": False, "items": [{"freshness": {"state": "current"}, "agents": [
                              {"kind": "codex", "session_id": self.session, "state": "idle"}]}]}):
            with patch.object(mp.App, "current", return_value=inventory), patch.object(mp, "stream_codex") as child, self.assertRaises(mp.PreferenceError):
                self.resume()
            child.assert_not_called()
        self.assertFalse((self.state_dir / "attempts").exists())

    def test_lock_and_uncertain_attempt_block_duplicate_execution(self):
        self.fixture()
        lock = self.state_dir / "resume-locks" / f"{self.session}.lock"
        lock.parent.mkdir()
        with lock.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(mp.PreferenceError, "in flight"):self.resume()
        with self.stopped(), patch.object(mp.shutil, "which", return_value="codex"), \
                patch.object(mp, "stream_codex", side_effect=OSError("fixture transport failed")), self.assertRaises(OSError):
            self.resume()
        attempt = self.app.state(f"continuations/{self.session}", None)
        self.assertEqual(attempt["status"], "uncertain")
        with self.stopped(), self.assertRaisesRegex(mp.PreferenceError, "unresolved"):self.resume()

    def ci_fixture(self):
        self.fixture("implementer")
        receipt = self.root / "receipt.json"
        receipt.write_text(json.dumps({"task_id": "c" * 32, "worktree": str(self.root), "harness": "codex",
                                       "owner_worktree": str(self.root), "status": "pending", "session_id": None}))
        launch = self.app.state(f"launches/{self.launch['id']}", None)
        launch["context_injector"] = {"receipt": str(receipt), "script": str(self.root / "ci.py"), "owner_worktree": str(self.root)}
        self.app.save_state(f"launches/{self.launch['id']}", launch)
        self.task = {"status": "active", "session_id": self.session, "harness": "codex", "root": str(self.root),
                     "owner": {"root": str(self.root), "gitdir": "owner-gitdir"}, "brief_sha256": "old"}
        self.status = {"registered": True, "enabled": True, "owned_hook_handlers": 1, "context_owner": None}
        return receipt

    def test_ci_uses_live_task_not_snapshot_and_preserves_exact_brief(self):
        receipt = self.ci_fixture(); original = receipt.read_bytes(); calls = []
        def ci(argv, stdin=None):
            calls.append(argv)
            if "task-status" in argv:return self.task
            if "status" in argv:return self.status
            self.assertIn("update-task", argv)
            saved = Path(argv[-1]).read_bytes()
            self.assertEqual(saved, self.brief.read_bytes())
            return {**self.task, "brief_sha256": mp.hashlib.sha256(saved).hexdigest()}
        with self.stopped(), patch.object(mp.shutil, "which", return_value="codex"), \
                patch.object(mp.App, "run_json", side_effect=ci), \
                patch.object(mp, "stream_codex", return_value=subprocess.CompletedProcess([], 0)) as child:
            result = self.resume()
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(self.app.state(f"attempts/{result['continuation']}", None)["context_injector_task"], "c" * 32)
        self.assertNotIn("dispatch", child.call_args.args[2])
        self.assertIn('model_reasoning_effort="xhigh"', child.call_args.args[2])

    def test_finished_foreign_task_or_disabled_recovery_cannot_update_or_run(self):
        self.ci_fixture()
        for field, value in (("status", "finished"), ("session_id", str(uuid.uuid4())), ("owner", {"root": "foreign"})):
            task = {**self.task, field: value}
            with self.stopped(), patch.object(mp.App, "run_json", return_value=task) as ci, \
                    patch.object(mp, "stream_codex") as child, self.assertRaises(mp.PreferenceError):self.resume()
            self.assertFalse(any("update-task" in c.args[0] for c in ci.call_args_list)); child.assert_not_called()
        with self.stopped(), patch.object(mp.App, "run_json", side_effect=[self.task, {**self.status, "enabled": False}]), \
                self.assertRaisesRegex(mp.PreferenceError, "recovery is unavailable"):self.resume()

    def test_stream_binds_native_event_and_refuses_changed_identity(self):
        launch, session = self.fixture()
        script = self.root / "native.py"
        script.write_text('import json\nprint(json.dumps({"type":"thread.started","thread_id":' + repr(session) + '}))\n')
        with contextlib.redirect_stdout(io.StringIO()):
            result = mp.stream_codex(self.app, launch, [sys.executable, str(script)], os.environ.copy(), self.root, expected=session)
        self.assertEqual(result.returncode, 0)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(mp.PreferenceError, "unexpected native"):
            mp.stream_codex(self.app, launch, [sys.executable, str(script)], os.environ.copy(), self.root, expected=str(uuid.uuid4()))

    def test_review_resume_rechecks_scope_and_permission(self):
        self.fixture("advanced_review")
        repo = self.repo()
        record = self.app.state(f"launches/{self.launch['id']}", None); record["cwd"] = str(repo)
        self.app.save_state(f"launches/{self.launch['id']}", record)
        plan = self.plan(repo, money=True)
        with self.assertRaisesRegex(mp.PreferenceError, "review-plan"):self.resume()
        with self.assertRaisesRegex(mp.PreferenceError, "permission"):
            self.resume("--review-plan", plan["id"])
        approval = self.call("approve-review", plan["id"], "--authorization", "Fixture only")
        (repo / "app.py").write_text("changed scope\n")
        with self.assertRaisesRegex(mp.PreferenceError, "scope/profile changed"):
            self.resume("--review-plan", plan["id"], "--approval", approval["id"])

    def test_real_cli_resume_composes_for_codex_reviewer_continuations(self):
        repo = self.repo()
        for role in ("standard_review", "advanced_review"):
            with self.subTest(role=role):
                launch, session = self.fixture(role)
                launch_record = self.app.state(f"launches/{launch['id']}", None)
                launch_record["cwd"] = str(repo)
                self.app.save_state(f"launches/{launch['id']}", launch_record)
                plan = self.plan(repo, repairing_previous_fix=role == "advanced_review")
                args = ["resume", launch["id"], "--task-file", str(self.brief), "--review-plan", plan["id"]]
                if role == "advanced_review":
                    approval = self.call("approve-review", plan["id"], "--authorization", "Fixture authority approved this continuation.")
                    args.extend(("--approval", approval["id"]))
                task_bytes = (f"Updated {role} continuation.\r\n"
                              "Preserve this complete brief: ✓.\r\n").encode("utf-8")
                self.brief.write_bytes(task_bytes)
                capture, env = self.fake_native_provider("codex")
                env["MODEL_PREFERENCES_TEST_SESSION"] = session
                result = self.run_cli(env, *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                received = json.loads(capture.read_text())
                task_text = task_bytes.decode("utf-8")
                expected = (mp.DELEGATION + " Use the current researcher role for authorized research.\n\n" +
                            mp.reviewer_guidance.compose_reviewer_prompt(role, task_text))
                self.assertEqual(received["prompt"], expected)
                self.assertTrue(received["prompt"].endswith(task_text))
                result_start = result.stdout.rfind('{\n  "launch"')
                self.assertGreaterEqual(result_start, 0, result.stdout)
                output = json.loads(result.stdout[result_start:])
                attempt = self.app.state(f"attempts/{output['continuation']}", None)
                self.assertEqual(attempt["choice"]["role"], role)
                self.assertEqual(attempt["task_digest"], mp.hashlib.sha256(task_bytes).hexdigest())
