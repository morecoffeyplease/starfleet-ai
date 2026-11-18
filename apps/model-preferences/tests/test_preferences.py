import concurrent.futures
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

SCRIPT = Path(__file__).resolve().parents[1] / "model_preferences.py"
spec = importlib.util.spec_from_file_location("preferences", SCRIPT)
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)


class PreferencesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config_dir = self.root / "config"
        self.state_dir = self.root / "state"
        self.app = mp.App(self.config_dir, self.state_dir)
        self.call("init", "--examples")
        self.call("authority-set", "user")
        real_popen = mp.subprocess.Popen
        def isolated_popen(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and argv and argv[0] in ("codex", "claude", "muse"):
                self.fail("Unit tests must replace native harness execution explicitly.")
            return real_popen(argv, *args, **kwargs)
        native_guard = patch.object(mp.subprocess, "Popen", side_effect=isolated_popen)
        native_guard.start(); self.addCleanup(native_guard.stop)
        self.workspace, self.surface, self.pane = [str(uuid.uuid4()) for _ in range(3)]
        caller_guard = patch.object(mp.App, "caller", return_value={
            "workspace": self.workspace, "surface": self.surface,
            "session": "fixture-caller-session", "harness": "codex"})
        caller_guard.start(); self.addCleanup(caller_guard.stop)
        self.recipient = {"workspace": self.workspace, "surface": self.surface, "session": "outgoing", "harness": "claude"}

    def args(self, *args):
        return mp.parser().parse_args(["--config-dir", str(self.config_dir), "--state-dir", str(self.state_dir), *args])

    def call(self, *args):
        return mp.execute(self.args(*args))

    def activate(self, name="Claude-Codex"):
        with patch.object(mp.App, "current", return_value={"items": [], "truncated": False}):
            return self.call("activate", name, "--orchestrators", "future-only")

    def inventory(self, recipient=None):
        recipient = recipient or self.recipient
        return {"items": [{"lifecycle": "running", "freshness": {"state": "current"}, "projections": [{"workspace_id": recipient["workspace"],
                  "panel_id": recipient["surface"]}], "agents": [{"session_id": recipient["session"],
                  "kind": recipient["harness"], "state": "idle"}]}], "truncated": False}

    def repo(self):
        repo = self.root / "repo"
        repo.mkdir()
        for args in (("init", "-q"), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        (repo / "app.py").write_text("original\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
        return repo

    def facts(self, **changes):
        facts = dict.fromkeys(mp.FLAGS, False)
        facts.update(changes)
        p = self.root / "facts.json"
        p.write_text(json.dumps(facts))
        return p

    def plan(self, repo, **changes):
        return self.call("review-plan", "--cwd", str(repo), "--base", "HEAD", "--facts", str(self.facts(**changes)),
                         "--purpose", "later")

    def initial_plan(self, repo, **changes):
        return self.call("review-plan", "--cwd", str(repo), "--base", "HEAD", "--facts", str(self.facts(**changes)),
                         "--purpose", "initial")

    def fake_native_provider(self, harness):
        provider_bin = self.root / "fake-native-bin"
        provider_bin.mkdir(exist_ok=True)
        capture = self.root / f"{harness}-prompt.json"
        script = provider_bin / harness
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "argv = sys.argv[1:]\n"
            "resume = bool(argv and argv[-1] == '-' and 'resume' in argv)\n"
            "prompt = sys.stdin.read() if resume else argv[-1]\n"
            "pathlib.Path(os.environ['MODEL_PREFERENCES_TEST_CAPTURE']).write_text("
            "json.dumps({'argv': argv, 'prompt': prompt}))\n"
            "if resume:\n"
            "    session = argv[argv.index('resume') + 1]\n"
            "    print(json.dumps({'type': 'thread.started', 'thread_id': session}), flush=True)\n"
        )
        script.chmod(0o755)
        cmux = provider_bin / "cmux"
        cmux.write_text("#!/bin/sh\nprintf '%s\\n' '{\"truncated\":false,\"items\":[]}'\n")
        cmux.chmod(0o755)
        env = dict(os.environ, PATH=str(provider_bin) + os.pathsep + os.environ.get("PATH", ""),
                   MODEL_PREFERENCES_TEST_CAPTURE=str(capture))
        return capture, env

    def run_cli(self, env, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "--config-dir", str(self.config_dir),
                               "--state-dir", str(self.state_dir), *args],
                              capture_output=True, text=True, timeout=30, env=env)

    def create_review_target(self, repo, target_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"):
        return self.call("review-target", "create", "--cwd", str(repo), "--id", target_id)

    def light_plan(self, repo, target, key, previous=None, **changes):
        args = ["review-plan", "--cwd", str(repo), "--base", "HEAD", "--facts", str(self.facts(**changes)),
                "--target", target, "--cycle-key", key]
        if previous:
            args.extend(("--previous-cycle", previous))
        return self.call(*args)

    def dispatch_light_review(self, repo, target, cycle, plan, slot, exit_code=0, retry=False, brief_text=None,
                              role="standard_review"):
        brief = self.root / f"light-review-{slot}.txt"
        brief.write_text(brief_text or f"Review slot {slot} for the saved plan.")
        args = ["dispatch", "--role", role, "--slot", str(slot), "--cwd", str(repo),
                "--task-file", str(brief), "--review-plan", plan, "--review-target", target,
                "--review-cycle", cycle]
        if retry:
            args.append("--retry-failed")
        original_run = subprocess.run

        def run(argv, *positional, **keywords):
            if argv and argv[0] in ("codex", "claude"):
                return subprocess.CompletedProcess(argv, exit_code)
            return original_run(argv, *positional, **keywords)

        with patch.object(mp.shutil, "which", return_value="native"), \
                patch.object(mp.subprocess, "run", side_effect=run), \
                patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd, **kwargs:
                             subprocess.CompletedProcess(argv, exit_code)):
            return self.call(*args)

    def target_state(self, target):
        return self.app.state(f"review-targets/{target}", None)

    def test_light_review_cycle_requires_pair_and_uses_sol_after_proven_predecessor(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        target = self.create_review_target(repo)["id"]
        first = self.light_plan(repo, target, "first-review")
        repeated = self.light_plan(repo, target, "first-review")
        self.assertEqual(first["id"], repeated["id"])
        cycle = self.target_state(target)["cycles"][0]
        self.assertEqual(first["review_cycle"], cycle["id"])
        self.assertEqual([slot["number"] for slot in cycle["slots"]], [1, 2])
        self.assertEqual([slot["choice"]["model"] for slot in cycle["slots"]],
                         ["gpt-6.1-sol", "claude-opus-5-5"])
        self.dispatch_light_review(repo, target, cycle["id"], first["id"], 1)
        reused = self.dispatch_light_review(repo, target, cycle["id"], first["id"], 1)
        self.assertTrue(reused["reused"])
        with self.assertRaisesRegex(mp.PreferenceError, "predecessor|complete|slot"):
            self.light_plan(repo, target, "second-review", cycle["id"])
        self.dispatch_light_review(repo, target, cycle["id"], first["id"], 2)
        second = self.light_plan(repo, target, "second-review", cycle["id"])
        next_cycle = self.target_state(target)["cycles"][1]
        self.assertEqual(second["review_cycle"], next_cycle["id"])
        self.assertEqual([slot["number"] for slot in next_cycle["slots"]], [1])
        self.assertEqual(next_cycle["slots"][0]["choice"]["model"], "gpt-6.1-sol")
        with self.assertRaisesRegex(mp.PreferenceError, "slot|unavailable|required"):
            self.call("resolve", "--role", "standard_review", "--slot", "2", "--review-target", target,
                      "--review-cycle", next_cycle["id"], "--review-plan", second["id"])

    def test_initial_policy_plan_resolve_dispatch_paths_for_each_supported_profile(self):
        cases = (
            ("Codex-only", ["gpt-6.1-sol"], "complex_initial_review", ["gpt-6-astra"]),
            ("Claude-only", ["claude-opus-5-5"], "complex_initial_review", ["claude-fable-5-1"]),
            ("Claude-Codex", ["gpt-6.1-sol", "claude-opus-5-5"], "complex_initial_review",
             ["gpt-6-astra", "claude-opus-5-5"]),
            ("claude-codex-light", ["gpt-6.1-sol", "claude-opus-5-5"], "complex_initial_review",
             ["gpt-6-astra", "claude-opus-5-5"]),
        )
        repo = self.repo()
        for profile, simple_models, complex_role, complex_models in cases:
            with self.subTest(profile=profile):
                self.activate(profile)

                def plan_for(kind, facts):
                    target = (self.create_review_target(repo, str(uuid.uuid4()))["id"]
                              if profile == "claude-codex-light" else None)
                    command = ["review-plan", "--cwd", str(repo), "--base", "HEAD",
                               "--facts", str(self.facts(**facts)), "--purpose", "initial"]
                    if target:
                        command.extend(("--target", target, "--cycle-key", f"{profile}-{kind}"))
                    result = self.run_cli(dict(os.environ), *command)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    plan = json.loads(result.stdout)
                    plan["test_target"] = target
                    return plan

                simple = plan_for("simple", {})
                self.assertEqual((simple["purpose"], simple["classification"], simple["role"]),
                                 ("initial", "straightforward", "standard_review"))
                self.assertEqual([slot["model"] for slot in simple["review_guidance"]["review_slots"]],
                                 simple_models)
                complex_plan = plan_for("complex", {"new_design": True})
                self.assertEqual((complex_plan["purpose"], complex_plan["classification"], complex_plan["role"]),
                                 ("initial", "complex", complex_role))
                self.assertFalse(complex_plan["review_guidance"]["approval_required"])
                self.assertEqual([slot["model"] for slot in complex_plan["review_guidance"]["review_slots"]],
                                 complex_models)

                for plan, role, models in ((simple, "standard_review", simple_models),
                                           (complex_plan, complex_role, complex_models)):
                    cycle_id = plan.get("review_cycle")
                    plan_target = plan["test_target"]
                    for slot, expected_model in enumerate(models, 1):
                        resolve_args = ["resolve", "--role", role, "--slot", str(slot),
                                        "--review-plan", plan["id"]]
                        dispatch_args = ["dispatch", "--role", role, "--slot", str(slot),
                                         "--cwd", str(repo), "--task-file"]
                        brief = self.root / f"{profile}-{role}-{slot}.txt"
                        brief.write_text(f"Approved initial {role} slot {slot} for {profile}.")
                        dispatch_args.append(str(brief))
                        dispatch_args.extend(("--review-plan", plan["id"]))
                        if plan_target:
                            resolve_args.extend(("--review-target", plan_target, "--review-cycle", cycle_id))
                            dispatch_args.extend(("--review-target", plan_target, "--review-cycle", cycle_id))
                        resolved = self.run_cli(dict(os.environ), *resolve_args)
                        self.assertEqual(resolved.returncode, 0, resolved.stderr)
                        resolved_choice = json.loads(resolved.stdout)
                        self.assertEqual(resolved_choice["model"], expected_model)
                        capture, env = self.fake_native_provider(resolved_choice["harness"])
                        dispatched = self.run_cli(env, *dispatch_args)
                        self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
                        result = json.loads(dispatched.stdout)
                        launch = self.app.state(f"launches/{result['launch']}", None)
                        self.assertEqual(launch["choice"], {key: resolved_choice[key] for key in
                                         ("profile", "selection", "role", "family", "harness", "model",
                                          "effort", "contributor_mode")})
                        self.assertEqual(launch["choice"]["model"], expected_model)
                        received = json.loads(capture.read_text())
                        self.assertIn("Reviewer judgment guidance", received["prompt"])
                        if role == "skeptical_review":
                            self.assertIn("Skeptical continuation proposal review", received["prompt"])
                        self.assertTrue(received["prompt"].endswith(brief.read_text()))

    def test_completed_light_complex_initial_cycle_leads_to_one_sol_later_cycle(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        target = self.create_review_target(repo)["id"]
        facts = self.facts(new_design=True)
        first_cli = self.run_cli(dict(os.environ), "review-plan", "--cwd", str(repo), "--base", "HEAD",
                                 "--facts", str(facts), "--purpose", "initial", "--target", target,
                                 "--cycle-key", "complex-initial")
        self.assertEqual(first_cli.returncode, 0, first_cli.stderr)
        first = json.loads(first_cli.stdout)
        cycle = self.target_state(target)["cycles"][0]
        self.assertEqual((cycle["purpose"], cycle["classification"]), ("initial", "complex"))
        self.assertEqual([slot["choice"]["model"] for slot in cycle["slots"]],
                         ["gpt-6-astra", "claude-opus-5-5"])
        for slot in (1, 2):
            role = "complex_initial_review"
            brief = self.root / f"complex-initial-{slot}.txt"
            brief.write_text(f"Review complex initial slot {slot}.")
            choice = cycle["slots"][slot - 1]["choice"]
            capture, env = self.fake_native_provider(choice["harness"])
            result = self.run_cli(env, "dispatch", "--role", role, "--slot", str(slot), "--cwd", str(repo),
                                  "--task-file", str(brief), "--review-plan", first["id"],
                                  "--review-target", target, "--review-cycle", cycle["id"])
            self.assertEqual(result.returncode, 0, result.stderr)
            launch = self.app.state(f"launches/{json.loads(result.stdout)['launch']}", None)
            self.assertEqual(launch["review_cycle"]["cycle_id"], cycle["id"])
            self.assertEqual(launch["choice"], choice)
            self.assertIn("Reviewer judgment guidance", json.loads(capture.read_text())["prompt"])

        later_cli = self.run_cli(dict(os.environ), "review-plan", "--cwd", str(repo), "--base", "HEAD",
                                 "--facts", str(self.facts()), "--target", target,
                                 "--cycle-key", "later-ordinary", "--previous-cycle", cycle["id"])
        self.assertEqual(later_cli.returncode, 0, later_cli.stderr)
        later = json.loads(later_cli.stdout)
        later_cycle = self.target_state(target)["cycles"][1]
        self.assertEqual((later["purpose"], later["classification"], later["role"]),
                         ("later", "straightforward", "standard_review"))
        self.assertEqual([slot["number"] for slot in later_cycle["slots"]], [1])
        self.assertEqual(later_cycle["slots"][0]["choice"]["model"], "gpt-6.1-sol")
        resolved = self.run_cli(dict(os.environ), "resolve", "--role", "standard_review", "--slot", "1",
                                "--review-target", target, "--review-cycle", later_cycle["id"],
                                "--review-plan", later["id"])
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(json.loads(resolved.stdout)["model"], "gpt-6.1-sol")
        brief = self.root / "later-light-review.txt"
        brief.write_text("Review the ordinary later cycle.")
        capture, env = self.fake_native_provider("codex")
        dispatched = self.run_cli(env, "dispatch", "--role", "standard_review", "--slot", "1",
                                  "--cwd", str(repo), "--task-file", str(brief), "--review-plan", later["id"],
                                  "--review-target", target, "--review-cycle", later_cycle["id"])
        self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
        launch = self.app.state(f"launches/{json.loads(dispatched.stdout)['launch']}", None)
        self.assertEqual(launch["choice"]["model"], "gpt-6.1-sol")

    def test_review_policy_migration_is_explicit_and_preserves_saved_families_efforts_and_other_profiles(self):
        config = self.app.config()
        config["profiles"]["Codex-only"].pop("review_policy")
        config["profiles"]["Codex-only"]["advanced_review"] = [{"model": "codex-luna", "effort": "xhigh"}]
        config["profiles"]["Codex-only"]["standard_review"] = [{"model": "codex-sol", "effort": "medium"}]
        other_profiles = json.loads(json.dumps({key: value for key, value in config["profiles"].items()
                                                if key != "Codex-only"}))
        mp.write(self.app.config_path, config)
        before = self.app.config_path.read_bytes()
        preview = self.call("review-policy-migrate", "Codex-only")
        self.assertFalse(preview["applied"])
        self.assertFalse(preview["already_configured"])
        self.assertEqual(preview["review_policy"]["complex_initial"],
                         [{"model": "codex-luna", "effort": "xhigh"}])
        self.assertEqual(preview["review_policy"]["skeptical_proposal"],
                         [{"model": "codex-luna", "effort": "xhigh"}])
        self.assertEqual(before, self.app.config_path.read_bytes())
        result = self.call("review-policy-migrate", "Codex-only", "--apply",
                           "--policy-sha256", preview["policy_sha256"])
        self.assertTrue(result["applied"])
        saved = self.app.config()["profiles"]
        self.assertEqual(saved["Codex-only"]["review_policy"], preview["review_policy"])
        self.assertEqual({key: value for key, value in saved.items() if key != "Codex-only"}, other_profiles)

    def test_review_policy_validation_and_missing_adoption_fail_without_fallback(self):
        for mutate in (
            lambda policy: policy.update(version=99),
            lambda policy: policy.update(complex_initial=[{"model": "missing-family", "effort": "high"}]),
            lambda policy: policy.update(skeptical_proposal=[
                {"model": "codex-astra", "effort": "high"}, {"model": "codex-astra", "effort": "high"}]),
        ):
            config = mp.example_config()
            policy = config["profiles"]["Codex-only"]["review_policy"]
            mutate(policy)
            with self.assertRaises(mp.PreferenceError):
                mp.validate(config)

        self.activate("Codex-only")
        repo = self.repo()
        config = self.app.config()
        config["profiles"]["Codex-only"].pop("review_policy")
        mp.write(self.app.config_path, config)
        with self.assertRaisesRegex(mp.PreferenceError, "explicit review policy|review-policy-migrate"):
            self.initial_plan(repo, new_design=True)
        self.assertFalse((self.state_dir / "launches").exists())
        self.assertNotIn("review_policy", self.call("status")["active"])

        self.activate("Codex-Qwen")
        unchanged = self.initial_plan(repo, new_design=True)
        self.assertEqual(unchanged["role"], "advanced_review")
        self.assertNotIn("review_policy", self.call("status")["active"])

    def test_complex_initial_scope_staleness_and_uncertain_launch_prevent_fallback_or_duplicate(self):
        self.activate("Codex-only")
        repo = self.repo()
        plan = self.initial_plan(repo, new_design=True)
        self.assertEqual(plan["role"], "complex_initial_review")
        brief = self.root / "complex-initial-boundary.txt"
        brief.write_text("Review the configured complex initial panel.")
        capture, env = self.fake_native_provider("codex")
        args = ["dispatch", "--role", "complex_initial_review", "--slot", "1", "--cwd", str(repo),
                "--task-file", str(brief), "--review-plan", plan["id"]]
        first = self.run_cli(env, *args)
        self.assertEqual(first.returncode, 0, first.stderr)
        launch_id = json.loads(first.stdout)["launch"]
        launch = self.app.state(f"launches/{launch_id}", None)
        launch.update(status="uncertain", exit_code=None)
        self.app.save_state(f"launches/{launch_id}", launch)
        capture.unlink()
        duplicate = self.run_cli(env, *args)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("active or uncertain", duplicate.stderr)
        self.assertFalse(capture.exists())
        self.assertEqual(len(list((self.state_dir / "launches").glob("*.json"))), 1)

        self.activate("Claude-Codex")
        stale_plan = self.initial_plan(repo, new_design=True)
        self.activate("Codex-only")
        (repo / "app.py").write_text("scope changed after planning\n")
        stale_capture, stale_env = self.fake_native_provider("codex")
        stale = self.run_cli(stale_env, "dispatch", "--role", "complex_initial_review", "--slot", "1",
                             "--cwd", str(repo), "--task-file", str(brief), "--review-plan", stale_plan["id"])
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("Review scope/profile changed", stale.stderr)
        self.assertFalse(stale_capture.exists())

    def test_active_review_policy_migration_requires_reviewed_notification_choice_and_preserves_launches(self):
        self.activate("Codex-only")
        config = self.app.config()
        config["profiles"]["Codex-only"].pop("review_policy")
        mp.write(self.app.config_path, config)
        launch_id = uuid.uuid4().hex
        fake_launch = {"id": launch_id, "choice": {"profile": "Codex-only", "model": "saved-job"},
                       "status": "exited", "exit_code": 0}
        self.app.save_state(f"launches/{launch_id}", fake_launch)
        preview = self.call("review-policy-migrate", "Codex-only")
        before = self.app.config_path.read_bytes()
        with self.assertRaisesRegex(mp.PreferenceError, "Active review choices change"):
            self.call("review-policy-migrate", "Codex-only", "--apply",
                      "--policy-sha256", preview["policy_sha256"])
        self.assertEqual(before, self.app.config_path.read_bytes())
        with patch.object(mp.App, "current", return_value={"items": [], "truncated": False}):
            result = self.call("review-policy-migrate", "Codex-only", "--apply",
                               "--policy-sha256", preview["policy_sha256"],
                               "--orchestrators", "future-only")
        self.assertTrue(result["active_selection_changed"])
        self.assertEqual(self.app.state(f"launches/{launch_id}", None), fake_launch)

    def test_skeptical_review_requires_exact_opt_in_and_distinct_bounded_continuation(self):
        self.activate("Claude-Codex")
        repo = self.repo()
        target = self.create_review_target(repo)["id"]
        proposal = self.root / "proposal.md"
        proposal.write_text("Continue one bounded attempt that changes only the retry state transition.\n")
        plan_cli = self.run_cli(dict(os.environ), "review-plan", "--cwd", str(repo), "--base", "HEAD",
                                "--facts", str(self.facts(repairing_previous_fix=True)),
                                "--purpose", "skeptical_proposal", "--target", target,
                                "--proposal-file", str(proposal))
        self.assertEqual(plan_cli.returncode, 0, plan_cli.stderr)
        plan = json.loads(plan_cli.stdout)
        self.assertEqual((plan["purpose"], plan["role"], plan["proposal_sha256"] != None),
                         ("skeptical_proposal", "skeptical_review", True))
        self.assertEqual([slot["model"] for slot in plan["review_guidance"]["review_slots"]],
                         ["claude-fable-5-1"])
        self.assertTrue(plan["review_guidance"]["approval_required"])

        task = self.root / "skeptical-review-task.md"
        task.write_text("Three repair rounds have not resolved the work. Review the bound continuation proposal.")
        capture, env = self.fake_native_provider("claude")
        dispatch = ["dispatch", "--role", "skeptical_review", "--slot", "1", "--cwd", str(repo),
                    "--task-file", str(task), "--review-plan", plan["id"], "--review-target", target,
                    "--proposal-file", str(proposal)]
        no_approval = self.run_cli(env, *dispatch)
        self.assertNotEqual(no_approval.returncode, 0)
        self.assertFalse(capture.exists())

        initial = self.initial_plan(repo, new_design=True)
        self.assertEqual(initial["role"], "complex_initial_review")
        with self.assertRaisesRegex(mp.PreferenceError, "Advanced review plan required"):
            self.call("approve-review", initial["id"], "--authorization", "Initial policy is standing default.")
        later_advanced = self.plan(repo, new_design=True)
        generic = self.call("approve-review", later_advanced["id"], "--authorization", "Generic advanced review grant.")
        wrong_approval = self.run_cli(env, *dispatch, "--approval", generic["id"])
        self.assertNotEqual(wrong_approval.returncode, 0)
        self.assertIn("does not match this exact review plan", wrong_approval.stderr)
        self.assertFalse(capture.exists())

        approved = self.call("approve-review", plan["id"], "--authorization", "Tyler approved this exact skeptical review.")
        with self.assertRaisesRegex(mp.PreferenceError, "launch to exit successfully"):
            self.call("approve-review", plan["id"], "--continue-repair", "--authorization", "Separate continuation decision.",
                      "--continuation-batch-file", str(proposal), "--review-result-file", str(proposal))
        proposal.write_text("Changed proposal bytes after approval.\n")
        changed = self.run_cli(env, *dispatch, "--approval", approved["id"])
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("Proposal bytes changed", changed.stderr)
        self.assertFalse(capture.exists())

        proposal.write_text("Continue one bounded attempt that changes only the retry state transition.\n")
        dispatched = self.run_cli(env, *dispatch, "--approval", approved["id"])
        self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
        launch = self.app.state(f"launches/{json.loads(dispatched.stdout)['launch']}", None)
        self.assertEqual(launch["choice"]["model"], "claude-fable-5-1")
        received = json.loads(capture.read_text())
        self.assertIn("Skeptical continuation proposal review", received["prompt"])
        self.assertIn("Three repair rounds have not resolved the work.", received["prompt"])
        self.assertIn("Exact continuation proposal under review", received["prompt"])
        self.assertTrue(received["prompt"].endswith(task.read_text()))
        self.assertLess(received["prompt"].index(proposal.read_text()),
                        received["prompt"].index(task.read_text()))
        after_review = self.call("review-inspect", plan["id"])["review_guidance"]
        self.assertEqual(after_review["approval_status"], "review_complete")
        self.assertEqual(after_review["continuation_status"], "missing")
        self.assertIn("distinct bounded continuation decision", after_review["next_action"])

        result_file = self.root / "skeptical-review-result.md"
        result_file.write_text("Reviewer recommends one bounded attempt and records unresolved uncertainty.\n")
        batch_file = self.root / "bounded-repair-batch.md"
        batch_file.write_text("Edit only retry-state transition; add focused regression test; stop if invariant fails.\n")
        continuation = self.call("approve-review", plan["id"], "--continue-repair",
                                 "--authorization", "Tyler approved the exact bounded repair batch.",
                                 "--review-result-file", str(result_file),
                                 "--continuation-batch-file", str(batch_file))
        self.assertEqual(continuation["role"], "repair_continuation")
        self.assertEqual(continuation["review_result_sha256"], mp.hashlib.sha256(result_file.read_bytes()).hexdigest())
        self.assertEqual(continuation["continuation_batch_sha256"], mp.hashlib.sha256(batch_file.read_bytes()).hexdigest())
        final = self.call("review-inspect", plan["id"])["review_guidance"]
        self.assertEqual(final["approval_status"], "continuation_approved")
        self.assertEqual(final["continuation_status"], "approved")

    def test_declined_skeptical_review_launches_no_model_and_keeps_separate_route_decision(self):
        self.activate("Codex-only")
        repo = self.repo()
        target = self.create_review_target(repo)["id"]
        proposal = self.root / "declined-proposal.md"
        proposal.write_text("Proposed next step after round three.\n")
        plan = self.call("review-plan", "--cwd", str(repo), "--base", "HEAD", "--facts", str(self.facts()),
                         "--purpose", "skeptical_proposal", "--target", target,
                         "--proposal-file", str(proposal))
        capture, env = self.fake_native_provider("codex")
        declined = self.call("approve-review", plan["id"], "--decline-skeptical",
                             "--authorization", "Tyler declined the optional review.")
        self.assertEqual(declined["role"], "skeptical_review_declined")
        self.assertFalse(capture.exists())
        status = self.call("review-inspect", plan["id"])["review_guidance"]
        self.assertEqual(status["approval_status"], "declined")
        self.assertIsNone(status["next_action_command"])
        route = self.call("approve-review", plan["id"], "--route", "defer",
                          "--authorization", "Tyler separately chose to defer.")
        self.assertEqual(route["route"], "defer")
        with self.assertRaisesRegex(mp.PreferenceError, "requires a prior skeptical-review decline|revert or defer route"):
            self.call("approve-review", plan["id"], "--continue-repair",
                      "--authorization", "Conflicting continuation.",
                      "--continuation-batch-file", str(proposal))
        self.assertFalse((self.state_dir / "launches").exists())

    def test_light_cycle_refresh_keeps_pair_and_old_scope_launches_do_not_complete_new_plan(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        target = self.create_review_target(repo)["id"]
        first = self.light_plan(repo, target, "same-pass")
        cycle = self.target_state(target)["cycles"][0]
        self.dispatch_light_review(repo, target, cycle["id"], first["id"], 1)
        (repo / "app.py").write_text("changed scope\n")
        refreshed = self.light_plan(repo, target, "same-pass")
        current = self.target_state(target)["cycles"][0]
        self.assertEqual(current["id"], cycle["id"])
        self.assertEqual([slot["number"] for slot in current["slots"]], [1, 2])
        self.assertNotEqual(refreshed["id"], first["id"])
        self.assertNotEqual(refreshed["scope"], first["scope"])
        with self.assertRaisesRegex(mp.PreferenceError, "predecessor|complete|slot"):
            self.light_plan(repo, target, "later-pass", current["id"])
        self.dispatch_light_review(repo, target, current["id"], refreshed["id"], 1)
        self.dispatch_light_review(repo, target, current["id"], refreshed["id"], 2)
        later = self.light_plan(repo, target, "later-pass", current["id"])
        self.assertEqual([slot["number"] for slot in self.target_state(target)["cycles"][1]["slots"]], [1])
        self.assertNotEqual(later["id"], refreshed["id"])

    def test_light_cycle_retries_only_a_known_nonzero_exit(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        target = self.create_review_target(repo)["id"]
        plan = self.light_plan(repo, target, "retry-pass")
        cycle = self.target_state(target)["cycles"][0]
        failed = self.dispatch_light_review(repo, target, cycle["id"], plan["id"], 1, exit_code=7)
        duplicate = self.dispatch_light_review(repo, target, cycle["id"], plan["id"], 1, exit_code=0)
        self.assertEqual(failed["launch"], duplicate["launch"])
        with self.assertRaisesRegex(mp.PreferenceError, "retry|failed|known|different task brief"):
            self.dispatch_light_review(repo, target, cycle["id"], plan["id"], 1, exit_code=0,
                                       retry=True, brief_text="A changed review brief.")
        retried = self.dispatch_light_review(repo, target, cycle["id"], plan["id"], 1, exit_code=0, retry=True)
        self.assertNotEqual(failed["launch"], retried["launch"])
        slot = self.target_state(target)["cycles"][0]["slots"][0]
        self.assertEqual(len(slot["launches"]), 2)
        launch = self.app.state(f"launches/{failed['launch']}", None)
        launch["status"] = "starting"
        self.app.save_state(f"launches/{failed['launch']}", launch)
        with self.assertRaisesRegex(mp.PreferenceError, "uncertain|unresolved|starting"):
            self.dispatch_light_review(repo, target, cycle["id"], plan["id"], 1, exit_code=0, retry=True)

    def test_light_retry_recovers_an_unlinked_interrupted_attempt_before_retrying(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        target = self.create_review_target(repo)["id"]
        plan = self.light_plan(repo, target, "retry-recovery")
        cycle = self.target_state(target)["cycles"][0]
        startup = self.call("startup", "--expect-profile", "claude-codex-light")
        selected = self.call("resolve", "--role", "standard_review", "--slot", "1")
        self.assertTrue(startup["ready_for_dispatch"])
        self.assertEqual((selected["role"], selected["model"], selected["effort"]),
                         ("standard_review", "gpt-6.1-sol", "high"))
        failed = self.dispatch_light_review(repo, target, cycle["id"], plan["id"], 1, exit_code=7)
        with patch.object(mp, "save_target", side_effect=mp.PreferenceError("interrupted aggregate write")):
            with self.assertRaisesRegex(mp.PreferenceError, "interrupted aggregate write"):
                self.dispatch_light_review(repo, target, cycle["id"], plan["id"], 1,
                                           exit_code=0, retry=True)
        state = self.target_state(target)
        linked = {link["launch_id"] for link in state["cycles"][0]["slots"][0]["launches"]}
        membership = {"target_id": target, "cycle_id": cycle["id"], "plan_id": plan["id"], "slot": 1}
        records = [self.app.state(f"launches/{path.stem}", None)
                   for path in sorted((self.state_dir / "launches").glob("*.json"))]
        orphaned = [record for record in records if record.get("review_cycle") == membership
                    and record["id"] not in linked]
        self.assertEqual(len(orphaned), 1)
        self.assertEqual(orphaned[0]["status"], "starting")
        with self.assertRaisesRegex(mp.PreferenceError, "known nonzero"):
            self.dispatch_light_review(repo, target, cycle["id"], plan["id"], 1,
                                       exit_code=0, retry=True)
        links = self.target_state(target)["cycles"][0]["slots"][0]["launches"]
        self.assertEqual([link["launch_id"] for link in links], [failed["launch"], orphaned[0]["id"]])
        records_after = [self.app.state(f"launches/{path.stem}", None)
                         for path in sorted((self.state_dir / "launches").glob("*.json"))]
        self.assertEqual(len([record for record in records_after if record.get("review_cycle") == membership]), 2)
        recovered = self.app.state(f"launches/{orphaned[0]['id']}", None)
        self.assertEqual(recovered["status"], "starting")

    def test_light_cycle_plan_refresh_holds_an_active_or_uncertain_launch(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        target = self.create_review_target(repo)["id"]
        plan = self.light_plan(repo, target, "held-refresh")
        cycle = self.target_state(target)["cycles"][0]
        launch = self.dispatch_light_review(repo, target, cycle["id"], plan["id"], 1)
        record = self.app.state(f"launches/{launch['launch']}", None)
        record["status"] = "starting"
        self.app.save_state(f"launches/{launch['launch']}", record)
        (repo / "app.py").write_text("changed scope\n")
        with self.assertRaisesRegex(mp.PreferenceError, "active|uncertain|unresolved"):
            self.light_plan(repo, target, "held-refresh")
        self.assertEqual(self.target_state(target)["cycles"][0]["current_plan"]["id"], plan["id"])

    def test_light_cycle_recovers_plan_and_launch_writes_before_aggregate_links(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        target = self.create_review_target(repo)["id"]
        plan = self.light_plan(repo, target, "recover-write")
        state = self.target_state(target)
        state["cycles"] = []
        self.app.save_state(f"review-targets/{target}", state)
        recovered_plan = self.light_plan(repo, target, "recover-write")
        self.assertEqual(recovered_plan["id"], plan["id"])
        cycle = self.target_state(target)["cycles"][0]
        first = self.dispatch_light_review(repo, target, cycle["id"], plan["id"], 1)
        state = self.target_state(target)
        state["cycles"][0]["slots"][0]["launches"] = []
        self.app.save_state(f"review-targets/{target}", state)
        recovered_launch = self.dispatch_light_review(repo, target, cycle["id"], plan["id"], 1)
        self.assertEqual(recovered_launch["launch"], first["launch"])
        self.assertTrue(recovered_launch["reused"])
        self.assertEqual(len(self.target_state(target)["cycles"][0]["slots"][0]["launches"]), 1)

    def test_light_cycle_duplicate_dispatches_serialize_to_one_launch(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        target = self.create_review_target(repo)["id"]
        plan = self.light_plan(repo, target, "concurrent-slot")
        cycle = self.target_state(target)["cycles"][0]
        brief = self.root / "concurrent-review.txt"
        brief.write_text("Review the saved scope.")
        args = ("dispatch", "--role", "standard_review", "--slot", "1", "--cwd", str(repo),
                "--task-file", str(brief), "--review-plan", plan["id"], "--review-target", target,
                "--review-cycle", cycle["id"])
        with patch.object(mp.shutil, "which", return_value="native"), \
                patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd, **kwargs:
                             subprocess.CompletedProcess(argv, 0)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.call(*args), range(2)))
        self.assertEqual(results[0]["launch"], results[1]["launch"])
        self.assertEqual(len(self.target_state(target)["cycles"][0]["slots"][0]["launches"]), 1)

    def test_light_target_creation_binding_and_plan_survive_app_restart(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        target_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        first = self.call("review-target", "create", "--cwd", str(repo), "--id", target_id,
                          "--pr-locator", "https://example.invalid/org/repo/pull/27")
        repeated = self.call("review-target", "create", "--cwd", str(repo), "--id", target_id,
                             "--pr-locator", "https://example.invalid/org/repo/pull/27")
        self.assertEqual(first["id"], repeated["id"])
        with self.assertRaisesRegex(mp.PreferenceError, "already|conflict|bound"):
            self.call("review-target", "bind-pr", target_id, "--pr-locator",
                      "https://example.invalid/org/repo/pull/28")
        plan = self.light_plan(repo, target_id, "handoff-pass")
        restarted = mp.App(self.config_dir, self.state_dir)
        self.assertEqual(restarted.state(f"review-targets/{target_id}", None)["cycles"][0]["current_plan"]["id"],
                         plan["id"])
        self.assertEqual(restarted.state(f"reviews/{plan['id']}", None)["review_target"], target_id)

    def test_light_target_and_cycle_follow_another_worktree_and_outputs_name_selection_kind(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        target = self.create_review_target(repo)["id"]
        first = self.light_plan(repo, target, "handoff")
        cycle = self.target_state(target)["cycles"][0]
        other = self.root / "other-worktree"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(other), "HEAD"],
                       check=True, capture_output=True)
        handed_off = self.light_plan(other, target, "handoff")
        self.assertEqual(handed_off["review_cycle"], cycle["id"])
        self.assertNotEqual(handed_off["id"], first["id"])
        self.assertEqual([slot["number"] for slot in self.target_state(target)["cycles"][0]["slots"]], [1, 2])
        configured = self.call("resolve", "--role", "standard_review")
        self.assertEqual(configured["selection_kind"], "configured_candidates")
        status = self.call("status")
        self.assertEqual(status["review_selection"]["kind"], "configured_candidates")
        self.assertEqual(configured["configured_candidates"], status["active"]["roles"]["standard_review"])
        contextual = self.call("resolve", "--role", "standard_review", "--review-target", target,
                               "--review-cycle", cycle["id"], "--review-plan", handed_off["id"], "--slot", "1")
        self.assertEqual(contextual["selection_kind"], "cycle")
        self.assertEqual(contextual["model"], "gpt-6.1-sol")
        status = self.call("status", "--review-target", target, "--review-cycle", cycle["id"])
        self.assertTrue(status["review_selection"]["selection_matches_active"])
        self.assertEqual(status["review_selection"]["contextual_cycle"]["slots"][0]["status"], "not_started")
        payload = {"session_id": "fixture-session", "hook_event_name": "SessionStart",
                   "source": "startup", "cwd": str(other)}
        with patch.object(mp.sys, "stdin", io.StringIO(json.dumps(payload))):
            hook = self.call("hook", "--harness", "codex")
        text = hook["hookSpecificOutput"]["additionalContext"]
        self.assertIn("configured candidates", text)
        launch = self.dispatch_light_review(other, target, cycle["id"], handed_off["id"], 1)
        with patch.dict(mp.os.environ, {"STARFLEET_MODEL_LAUNCH": launch["launch"]}), \
                patch.object(mp.sys, "stdin", io.StringIO(json.dumps(payload))):
            hook = self.call("hook", "--harness", "codex")
        text = hook["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"cycle {cycle['id']}", text)
        self.assertIn(f"plan {handed_off['id']}", text)

    def test_light_predecessor_conflicts_hold_and_advanced_approval_stays_required(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        target = self.create_review_target(repo)["id"]
        first = self.light_plan(repo, target, "first")
        cycle = self.target_state(target)["cycles"][0]
        self.dispatch_light_review(repo, target, cycle["id"], first["id"], 1)
        self.dispatch_light_review(repo, target, cycle["id"], first["id"], 2)
        state = self.target_state(target)
        state["cycles"][0]["slots"][1]["launches"][0]["launch_id"] = \
            state["cycles"][0]["slots"][0]["launches"][0]["launch_id"]
        self.app.save_state(f"review-targets/{target}", state)
        with self.assertRaisesRegex(mp.PreferenceError, "conflict|slot|membership"):
            self.light_plan(repo, target, "later", cycle["id"])
        advanced = self.plan(repo, money=True)
        with self.assertRaisesRegex(mp.PreferenceError, "explicit permission|approval"):
            self.call("dispatch", "--role", "advanced_review", "--slot", "1", "--cwd", str(repo),
                      "--task-file", str(self.facts()), "--review-plan", advanced["id"])
        approval = self.call("approve-review", advanced["id"], "--authorization", "Fixture approval")
        brief = self.root / "advanced-review.txt"
        brief.write_text("Review this advanced scope.")
        original_run = subprocess.run

        def run(argv, *positional, **keywords):
            if argv and argv[0] in ("codex", "claude"):
                return subprocess.CompletedProcess(argv, 0)
            return original_run(argv, *positional, **keywords)

        with patch.object(mp.shutil, "which", return_value="native"), \
                patch.object(mp.subprocess, "run", side_effect=run), \
                patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd, **kwargs:
                             subprocess.CompletedProcess(argv, 0)):
            result = self.call("dispatch", "--role", "advanced_review", "--slot", "1", "--cwd", str(repo),
                               "--task-file", str(brief), "--review-plan", advanced["id"],
                               "--approval", approval["id"])
        self.assertEqual(self.app.state(f"launches/{result['launch']}", None)["choice"]["role"], "advanced_review")

    def test_light_advanced_standard_instead_dispatch_uses_selected_configured_slot(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        (repo / "app.py").write_text("MODE = 'synthetic new design'\n")
        plan = self.plan(repo, new_design=True)
        approval = self.call("approve-review", plan["id"], "--standard-instead",
                             "--authorization", "Fixture authority selected standard review.")
        brief = self.root / "standard-instead-review.txt"
        task = "Review the synthetic new design in app.py."
        brief.write_text(task)
        capture, env = self.fake_native_provider("codex")

        cli_result = self.run_cli(env, "dispatch", "--role", "standard_review", "--slot", "1",
                                  "--cwd", str(repo), "--task-file", str(brief),
                                  "--review-plan", plan["id"], "--approval", approval["id"])
        self.assertEqual(cli_result.returncode, 0, cli_result.stderr)
        result = json.loads(cli_result.stdout)
        self.assertEqual(result["exit_code"], 0)
        received = json.loads(capture.read_text())
        launch = self.app.state(f"launches/{result['launch']}", None)
        self.assertEqual(launch["choice"]["role"], "standard_review")
        self.assertEqual((launch["choice"]["model"], launch["choice"]["effort"]),
                         ("gpt-6.1-sol", "high"))
        self.assertEqual(launch["slot"], 1)
        self.assertNotIn("review_cycle", launch)
        self.assertEqual(received["prompt"][-len(task):], task)

    def test_light_standard_instead_validation_and_cycle_guards_precede_provider(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        brief = self.root / "standard-instead-review.txt"
        brief.write_text("Review the synthetic design change in app.py.")
        capture, env = self.fake_native_provider("codex")

        def dispatch_args(plan_id, approval_id=None, extras=()):
            args = ["dispatch", "--role", "standard_review", "--slot", "1", "--cwd", str(repo),
                    "--task-file", str(brief), "--review-plan", plan_id]
            if approval_id:
                args.extend(("--approval", approval_id))
            args.extend(extras)
            return args

        def reject(message, plan_id, approval_id=None, extras=()):
            if capture.exists():
                capture.unlink()
            with patch.dict(os.environ, env):
                with self.assertRaisesRegex(mp.PreferenceError, message):
                    self.call(*dispatch_args(plan_id, approval_id, extras))
            self.assertFalse(capture.exists())

        (repo / "app.py").write_text("MODE = 'synthetic new design'\n")
        advanced = self.plan(repo, new_design=True)
        standard_approval = self.call("approve-review", advanced["id"], "--standard-instead",
                                     "--authorization", "Fixture authority selected standard review.")
        advanced_approval = self.call("approve-review", advanced["id"],
                                      "--authorization", "Fixture authority approved advanced review.")
        reject("explicit permission", advanced["id"])
        reject("does not cover this scope", advanced["id"], advanced_approval["id"])
        reject("Review target context", advanced["id"], standard_approval["id"],
               ("--review-target", "fixture-target", "--review-cycle", "fixture-cycle"))
        reject("Review target context", advanced["id"], standard_approval["id"], ("--retry-failed",))

        (repo / "app.py").write_text("MODE = 'changed after plan'\n")
        reject("Review scope/profile changed", advanced["id"], standard_approval["id"])

        self.activate("Claude-Codex")
        ordinary = self.plan(repo)
        self.assertEqual(ordinary["role"], "standard_review")
        self.activate("claude-codex-light")
        reject("requires --review-target, --review-cycle, and --review-plan", ordinary["id"])

        stale = self.plan(repo, new_design=True)
        stale_approval = self.call("approve-review", stale["id"], "--standard-instead",
                                   "--authorization", "Fixture authority selected standard review.")
        self.activate("Claude-Codex")
        reject("Review scope/profile changed", stale["id"], stale_approval["id"])

    def test_eight_profiles_and_muse_and_qwen_roles(self):
        self.assertEqual(len(self.call("list")["profiles"]), 8)
        examples = mp.example_config()
        self.assertIsNone(examples["active"])
        self.assertEqual(examples["models"]["local-qwen3.6-35b"], {
            "harness": "opencode", "provider_id": "trial-local", "slug": "qwen3.6:35b", "efforts": ["max"],
            "source": "opt-in example; native pilot verified the local OpenAI-compatible provider's max reasoning request and 32768 output limit"})
        self.assertEqual(examples["profiles"]["Codex-Qwen"]["orchestrator"], {"model": "codex-sol", "effort": "medium"})
        self.assertEqual(examples["profiles"]["Codex-Qwen"]["implementer"], {"model": "local-qwen3.6-35b", "effort": "max"})
        self.assertEqual(examples["profiles"]["Qwen-only"]["orchestrator"], {"model": "local-qwen3.6-35b", "effort": "max"})
        self.assertEqual(examples["profiles"]["Qwen-only"]["standard_review"], [{"model": "codex-sol", "effort": "high"}])
        self.assertEqual(examples["profiles"]["claude-codex-light"]["standard_review_policy"],
                         {"initial_slots": [1, 2], "later_slots": [1]})
        self.assertEqual(examples["profiles"]["claude-codex-light"]["advanced_review"],
                         examples["profiles"]["Claude-Codex"]["advanced_review"])
        claude = self.call("show", "Claude-only")
        self.assertEqual(claude["implementer"], {"model": "claude-haiku", "effort": "xhigh"})
        self.assertEqual(claude["researcher"], {"model": "claude-haiku", "effort": "high"})
        self.activate("Codex-Muse")
        for role, effort in (("implementer", "xhigh"), ("researcher", "high")):
            choice = self.call("resolve", "--role", role)
            self.assertEqual(choice["model"], "muse-spark-1.3-contributor")
            self.assertEqual(choice["effort"], effort)
            with patch.object(mp.shutil, "which", return_value="muse"):
                argv = mp.argv_for(choice, "brief", self.root)
            self.assertIn("--contributor-mode", argv)
            self.assertEqual(argv[argv.index("--contributor-mode") + 1], "on")

    def test_init_is_empty_and_presets_are_explicit_examples(self):
        empty_dir = self.root / "empty-config"
        empty_args = mp.parser().parse_args([
            "--config-dir", str(empty_dir), "--state-dir", str(self.root / "empty-state"), "init"])
        mp.execute(empty_args)
        empty = mp.App(empty_dir, self.root / "empty-state")
        self.assertEqual(empty.config()["models"], {})
        self.assertEqual(empty.config()["profiles"], {})
        self.assertIsNone(empty.config()["approval_authority"]["label"])
        examples = mp.example_config()
        self.assertIsNone(examples["active"])
        self.assertIsNone(examples["approval_authority"]["label"])
        self.assertEqual(examples["models"]["codex-luna"]["slug"], "gpt-6-luna")
        self.assertIn("opt-in example", examples["models"]["codex-luna"]["source"])

    def test_legacy_schema_v1_config_gets_neutral_authority_without_read_write(self):
        config = self.app.config()
        config.pop("approval_authority")
        mp.write(self.app.config_path, config)
        before = self.app.config_path.read_bytes()
        self.assertEqual(self.call("list")["profiles"], list(config["profiles"]))
        self.assertEqual(self.app.config()["approval_authority"], {"label": None})
        self.assertEqual(before, self.app.config_path.read_bytes())

    def test_advanced_review_approval_fails_closed_until_authority_is_configured(self):
        config_dir, state_dir = self.root / "unconfigured", self.root / "unconfigured-state"
        mp.execute(mp.parser().parse_args(["--config-dir", str(config_dir), "--state-dir", str(state_dir),
                                           "init", "--examples"]))
        app = mp.App(config_dir, state_dir)
        with patch.object(mp.App, "current", return_value={"items": [], "truncated": False}):
            mp.execute(mp.parser().parse_args(["--config-dir", str(config_dir), "--state-dir", str(state_dir),
                                               "activate", "Codex-only", "--orchestrators", "future-only"]))
        repo = self.repo()
        plan = mp.execute(mp.parser().parse_args([
            "--config-dir", str(config_dir), "--state-dir", str(state_dir), "review-plan", "--cwd", str(repo),
            "--base", "HEAD", "--facts", str(self.facts(money=True))]))
        with self.assertRaisesRegex(mp.PreferenceError, "Configure the human approval authority"):
            mp.execute(mp.parser().parse_args(["--config-dir", str(config_dir), "--state-dir", str(state_dir),
                                               "approve-review", plan["id"], "--authorization", "fixture claim"]))

    def test_qwen_profile_review_requires_explicit_authorization_for_each_launch(self):
        self.activate("Qwen-only")
        repo = self.repo()
        plan = self.plan(repo)
        brief = self.root / "review-brief.txt"
        brief.write_text("Review the exact scoped diff.")
        command = ["dispatch", "--role", "standard_review", "--cwd", str(repo),
                   "--task-file", str(brief), "--review-plan", plan["id"]]
        with self.assertRaisesRegex(mp.PreferenceError, "fresh --review-authorization"):
            self.call(*command)
        self.assertFalse((self.state_dir / "launches").exists())

        original = subprocess.run
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0) if argv[0] == "codex" else original(argv, **kwargs)
        with patch.object(mp.shutil, "which", return_value="codex"), \
                patch.object(mp.subprocess, "run", side_effect=run), \
                patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd, **kwargs: mp.subprocess.run(argv, cwd=cwd, env=env)):
            result = self.call(*command, "--review-authorization", "Authorize this one standard review launch")
        launch = self.app.state(f"launches/{result['launch']}", None)
        self.assertEqual(launch["choice"]["model"], "gpt-6.1-sol")
        self.assertEqual(launch["choice"]["effort"], "high")
        self.assertEqual(launch["review_authorization_sha256"], mp.hashlib.sha256(
            b"Authorize this one standard review launch").hexdigest())
        with self.assertRaisesRegex(mp.PreferenceError, "fresh --review-authorization"):
            self.call(*command)

    def test_model_set_adds_exact_family_without_catalog_lookup(self):
        config_dir = self.root / "empty-config"
        empty_args = mp.parser().parse_args([
            "--config-dir", str(config_dir), "--state-dir", str(self.root / "empty-state"), "init"])
        mp.execute(empty_args)
        args = mp.parser().parse_args([
            "--config-dir", str(config_dir), "--state-dir", str(self.root / "empty-state"),
            "model-set", "team-model", "--harness", "codex", "--slug", "exact-provider-model-2026",
            "--efforts", "low", "high", "--source", "operator confirmed exact CLI slug"])
        mp.execute(args)
        saved = mp.App(config_dir, self.root / "empty-state").config()["models"]["team-model"]
        self.assertEqual(saved, {"harness": "codex", "slug": "exact-provider-model-2026",
                                 "efforts": ["low", "high"],
                                 "source": "operator confirmed exact CLI slug"})

    def test_model_set_requires_exact_provider_id_for_opencode(self):
        with self.assertRaisesRegex(mp.PreferenceError, "--provider-id"):
            self.call("model-set", "local-qwen", "--harness", "opencode", "--slug", "qwen3.6:35b",
                      "--efforts", "max", "--source", "exact provider model")
        self.call("model-set", "local-qwen", "--harness", "opencode", "--provider-id", "trial-local",
                  "--slug", "qwen3.6:35b", "--efforts", "max", "--source", "exact provider model")
        self.assertEqual(self.app.config()["models"]["local-qwen"], {
            "harness": "opencode", "provider_id": "trial-local", "slug": "qwen3.6:35b",
            "efforts": ["max"], "source": "exact provider model"})

    def test_single_reviewer_defaults_resolve_without_a_slot_and_reject_slot_two(self):
        for profile, standard, advanced in (("Codex-only", "gpt-6.1-sol", "gpt-6-astra"),
                                            ("Codex-Muse", "gpt-6.1-sol", "gpt-6-astra"),
                                            ("Claude-only", "claude-opus-5-5", "claude-fable-5-1")):
            self.activate(profile)
            for role, model in (("standard_review", standard), ("advanced_review", advanced)):
                with self.subTest(profile=profile, role=role):
                    self.assertEqual(len(self.call("show", profile)[role]), 1)
                    self.assertEqual(self.call("resolve", "--role", role)["model"], model)
                    self.assertEqual(self.call("resolve", "--role", role, "--slot", "1")["model"], model)
                    with self.assertRaisesRegex(mp.PreferenceError, "slot 2 is unavailable"):
                        self.call("resolve", "--role", role, "--slot", "2")
        for profile in ("Claude-Codex", "Claude-Muse"):
            self.activate(profile)
            for role in ("standard_review", "advanced_review"):
                self.assertEqual(len(self.call("show", profile)[role]), 2)
                with self.assertRaisesRegex(mp.PreferenceError, "Select reviewer"):
                    self.call("resolve", "--role", role)
                self.call("resolve", "--role", role, "--slot", "2")

    def test_legacy_review_pairs_remain_valid_until_explicit_profile_update(self):
        self.activate("Codex-only")
        config = self.app.config()
        for role in ("standard_review", "advanced_review"):
            config["profiles"]["Codex-only"][role] *= 2
        mp.validate(config); mp.write(self.app.config_path, config)
        self.assertEqual(len(self.call("show", "Codex-only")["advanced_review"]), 2)
        old_selection = self.call("status")["active"]["selection"]
        updated = self.root / "single.json"
        updated.write_text(json.dumps(mp.example_config()["profiles"]["Codex-only"]))
        with patch.object(mp.App, "current", return_value={"items": [], "truncated": False}):
            result = self.call("profile-put", "Codex-only", "--file", str(updated), "--orchestrators", "future-only")
        self.assertNotEqual(old_selection, result["active"]["selection"])
        self.assertEqual(len(self.call("show", "Codex-only")["advanced_review"]), 1)

    def test_review_groups_reject_empty_or_more_than_two_entries(self):
        for count in (0, 3):
            config = mp.example_config()
            entry = config["profiles"]["Codex-only"]["standard_review"][0]
            config["profiles"]["Codex-only"]["standard_review"] = [entry] * count
            with self.assertRaises(mp.PreferenceError):
                mp.validate(config)

    def test_reserved_profiles_retain_provider_boundaries_without_restricting_custom_names(self):
        cases = (
            ("Codex-only", "implementer", "claude-opus"),
            ("Claude-only", "implementer", "codex-luna"),
            ("Claude-Muse", "implementer", "codex-luna"),
            ("Claude-Muse", "standard_review", "muse-spark"),
            ("Codex-Muse", "researcher", "claude-opus"),
            ("Codex-Muse", "advanced_review", "claude-fable"),
        )
        for profile_name, role, model in cases:
            with self.subTest(profile=profile_name, role=role, model=model):
                config = mp.example_config()
                entries = config["profiles"][profile_name][role]
                entry = entries[0] if isinstance(entries, list) else entries
                entry["model"] = model
                with self.assertRaisesRegex(mp.PreferenceError, "forbids|requires"):
                    mp.validate(config)

        config = mp.example_config()
        custom = json.loads(json.dumps(config["profiles"]["Codex-only"]))
        custom["implementer"]["model"] = "claude-opus"
        custom.pop("review_policy")
        config["profiles"]["team-default"] = custom
        mp.validate(config)
        custom["review_policy"] = json.loads(json.dumps(config["profiles"]["Codex-only"]["review_policy"]))
        with self.assertRaisesRegex(mp.PreferenceError, "supported only for"):
            mp.validate(config)

    def assert_single_advanced_review_requires_permission(self, profile):
        self.activate(profile); repo = self.repo(); plan = self.plan(repo, money=True)
        brief = self.root / "single-review.txt"; brief.write_text("Fixture review")
        args = ("dispatch", "--role", "advanced_review", "--cwd", str(repo), "--task-file", str(brief), "--review-plan", plan["id"])
        with self.assertRaisesRegex(mp.PreferenceError, "configured authority"):
            self.call(*args)
        self.assertFalse((self.state_dir / "launches").exists())
        approval = self.call("approve-review", plan["id"], "--authorization", "Authority approved this fixture")
        original = subprocess.run
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0) if argv[0] == "codex" else original(argv, **kwargs)
        with patch.object(mp.shutil, "which", return_value="codex"), patch.object(mp.subprocess, "run", side_effect=run), patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd, **kwargs: mp.subprocess.run(argv, cwd=cwd, env=env)):
            result = self.call(*args, "--approval", approval["id"])
        self.assertEqual(self.app.state(f"launches/{result['launch']}", None)["choice"]["model"], "gpt-6-astra")

    def test_single_advanced_review_still_requires_permission_before_launch(self):
        self.assert_single_advanced_review_requires_permission("Codex-only")

    def test_configured_authority_is_in_selection_and_stales_old_review_approval(self):
        self.activate("Codex-only")
        repo = self.repo()
        plan = self.plan(repo, money=True)
        approval = self.call("approve-review", plan["id"], "--authorization", "Fixture approval")
        self.assertEqual(approval["authority"], "user")
        with patch.object(mp.App, "current", return_value={"items": [], "truncated": False}):
            updated = self.call("authority-set", "release owner", "--orchestrators", "future-only")
        self.assertEqual(updated["active"]["approval_authority"]["label"], "release owner")
        brief = self.root / "authority-review.txt"
        brief.write_text("Fixture review")
        with self.assertRaisesRegex(mp.PreferenceError, "Review scope/profile changed"):
            self.call("dispatch", "--role", "advanced_review", "--cwd", str(repo),
                      "--task-file", str(brief), "--review-plan", plan["id"],
                      "--approval", approval["id"])

    def test_review_cli_guidance_covers_approval_authority_and_staleness_read_only(self):
        self.activate("Codex-only")
        repo = self.repo()
        env = dict(os.environ)

        def cli(*args):
            result = self.run_cli(env, *args)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        def make_plan(**changes):
            facts = self.facts(**changes)
            return cli("review-plan", "--cwd", str(repo), "--base", "HEAD", "--facts", str(facts),
                       "--purpose", "later")

        def inspect_readonly(plan_id, full=False):
            def snapshot(directory):
                return {str(path.relative_to(directory)): path.read_bytes()
                        for path in directory.rglob("*") if path.is_file()}

            before_config = snapshot(self.config_dir)
            before_state = snapshot(self.state_dir)
            report = cli("review-inspect", plan_id, *( ["--full"] if full else []))
            self.assertEqual(snapshot(self.config_dir), before_config)
            self.assertEqual(snapshot(self.state_dir), before_state)
            return report["review_guidance"]

        standard = make_plan()
        standard_guidance = inspect_readonly(standard["id"])
        self.assertEqual(standard["role"], "standard_review")
        self.assertEqual(standard_guidance["approval_required"], False)
        self.assertEqual(standard_guidance["approval_status"], "not_required")
        self.assertEqual(standard_guidance["plan_status"], "current")
        self.assertEqual(standard_guidance["review_slots"][0]["harness"], "codex")
        self.assertEqual(standard_guidance["review_slots"][0]["model"], "gpt-6.1-sol")
        self.assertEqual(inspect_readonly(standard["id"], full=True), standard_guidance)

        advanced = make_plan(money=True)
        advanced_guidance = inspect_readonly(advanced["id"])
        self.assertEqual(advanced["role"], "advanced_review")
        self.assertEqual(advanced_guidance["approval_status"], "missing")
        self.assertEqual(advanced_guidance["approval_authority"], {"status": "configured", "label": "user"})
        self.assertIn("--config-dir", advanced_guidance["next_action_command"])
        self.assertIn("--state-dir", advanced_guidance["next_action_command"])

        standard_decision_plan = make_plan(money=True)
        decision_result = cli("approve-review", standard_decision_plan["id"], "--standard-instead",
                              "--authorization", "Synthetic authority selected standard review.")
        decision_guidance = inspect_readonly(standard_decision_plan["id"])
        self.assertEqual(decision_guidance["approval_status"], "standard_instead")
        self.assertEqual(decision_guidance["review_role"], "standard_review")
        self.assertIn(decision_result["id"], decision_guidance["next_action_command"])

        legacy = make_plan()
        legacy_state = self.app.state(f"reviews/{legacy['id']}", None)
        legacy_state.pop("selection_snapshot")
        self.app.save_state(f"reviews/{legacy['id']}", legacy_state)
        config = self.app.config()
        config["approval_authority"] = {"label": "new authority"}
        mp.write(self.app.config_path, config)
        stale_selection = inspect_readonly(advanced["id"])
        self.assertEqual(stale_selection["selection_status"], "stale")
        self.assertEqual(stale_selection["plan_status"], "stale")
        self.assertEqual(stale_selection["approval_authority"], {"status": "configured", "label": "user"})
        self.assertEqual(stale_selection["approval_status"], "stale")
        legacy_stale = inspect_readonly(legacy["id"])
        self.assertEqual(legacy_stale["approval_authority"], {"status": "unknown", "label": None})
        self.assertEqual(legacy_stale["selection_source"], "unknown")
        self.assertEqual(legacy_stale["review_options"], [])

        config["approval_authority"] = {"label": None}
        mp.write(self.app.config_path, config)
        unset_plan = make_plan(money=True)
        unset_guidance = inspect_readonly(unset_plan["id"])
        self.assertEqual(unset_guidance["approval_authority"], {"status": "unset", "label": None})
        self.assertIn("create a fresh plan", unset_guidance["next_action"].lower())
        self.assertIn("authority-set", unset_guidance["next_action_command"])

        config["approval_authority"] = {"label": "user"}
        mp.write(self.app.config_path, config)
        (repo / "app.py").write_text("changed after the saved plan\n")
        stale_scope = inspect_readonly(standard["id"])
        self.assertEqual(stale_scope["selection_status"], "current")
        self.assertEqual(stale_scope["scope_status"], "stale")
        self.assertEqual(stale_scope["plan_status"], "stale")
        self.assertFalse((self.state_dir / "launches").exists())

        approval_plan = make_plan(money=True)
        approval_directory = self.state_dir / "approvals"
        approval_directory.mkdir(exist_ok=True)
        (approval_directory / "unreadable.json").write_text("{")
        unreadable = inspect_readonly(approval_plan["id"])
        self.assertEqual(unreadable["approval_records_status"], "unknown")
        self.assertEqual(unreadable["approval_status"], "unknown")

    def test_review_cli_guidance_ignores_only_proven_unrelated_legacy_records(self):
        self.activate("Codex-only")
        repo = self.repo()
        env = dict(os.environ)

        def cli(*args):
            result = self.run_cli(env, *args)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        plan = cli("review-plan", "--cwd", str(repo), "--base", "HEAD",
                   "--facts", str(self.facts(money=True)), "--purpose", "later")
        approvals = self.state_dir / "approvals"
        approvals.mkdir(exist_ok=True)

        def snapshot(directory):
            return {str(path.relative_to(directory)): path.read_bytes()
                    for path in directory.rglob("*") if path.is_file()}

        def inspect_readonly():
            before_config = snapshot(self.config_dir)
            before_state = snapshot(self.state_dir)
            report = cli("review-inspect", plan["id"])
            self.assertEqual(snapshot(self.config_dir), before_config)
            self.assertEqual(snapshot(self.state_dir), before_state)
            return report["review_guidance"]

        advanced = cli("approve-review", plan["id"], "--authorization", "Synthetic authority approved advanced review.")
        advanced_guidance = inspect_readonly()
        self.assertEqual(advanced_guidance["approval_status"], "approved")
        self.assertEqual(advanced_guidance["approval_records_status"], "available")
        (approvals / f"{advanced['id']}.json").unlink()

        standard = cli("approve-review", plan["id"], "--standard-instead",
                       "--authorization", "Synthetic authority selected standard review.")
        standard_guidance = inspect_readonly()
        self.assertEqual(standard_guidance["approval_status"], "standard_instead")
        self.assertEqual(standard_guidance["review_role"], "standard_review")
        self.assertIn(standard["id"], standard_guidance["next_action_command"])

        unrelated_records = {
            "different_scope": {
                "id": "f" * 32,
                "scope": mp.digest("unrelated scope"),
                "selection": plan["selection"],
                "role": "advanced_review",
            },
            "different_selection": {
                "id": "e" * 32,
                "scope": plan["scope"],
                "selection": mp.digest("unrelated selection"),
                "role": "advanced_review",
            },
        }
        for label, record in unrelated_records.items():
            path = approvals / f"000-{label}.json"
            path.write_text(json.dumps(record))
            guidance = inspect_readonly()
            self.assertEqual(guidance["approval_records_status"], "available", label)
            self.assertEqual(guidance["approval_status"], "standard_instead", label)
            path.unlink()

        unknown_records = {
            "matching_incomplete": {
                "id": "d" * 32,
                "scope": plan["scope"],
                "selection": plan["selection"],
                "role": "advanced_review",
            },
            "missing_identity": {
                "id": "c" * 32,
                "scope": mp.digest("unrelated scope"),
                "role": "advanced_review",
            },
            "invalid_identity": {
                "id": "b" * 32,
                "scope": "not-a-digest",
                "selection": mp.digest("unrelated selection"),
                "role": "advanced_review",
            },
            "nonobject": [],
        }
        for label, record in unknown_records.items():
            path = approvals / f"000-{label}.json"
            path.write_text(json.dumps(record))
            guidance = inspect_readonly()
            self.assertEqual(guidance["approval_records_status"], "unknown", label)
            self.assertEqual(guidance["approval_status"], "unknown", label)
            path.unlink()

        unreadable = approvals / "000-unreadable.json"
        unreadable.write_text("{")
        guidance = inspect_readonly()
        self.assertEqual(guidance["approval_records_status"], "unknown")
        self.assertEqual(guidance["approval_status"], "unknown")

    def test_light_review_cli_guidance_uses_frozen_cycle_slots_and_context(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        env = dict(os.environ)

        def cli(*args):
            result = self.run_cli(env, *args)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        target_id = str(uuid.uuid4())
        cli("review-target", "create", "--cwd", str(repo), "--id", target_id)
        facts = self.facts()
        first = cli("review-plan", "--cwd", str(repo), "--base", "HEAD", "--facts", str(facts),
                    "--target", target_id, "--cycle-key", "first")
        first_guidance = first["review_guidance"]
        self.assertEqual([item["slot"] for item in first_guidance["review_slots"]], [1, 2])
        self.assertIn("--review-target", first_guidance["next_action_command"])
        self.assertIn("--review-cycle", first_guidance["next_action_command"])

        target = self.app.state(f"review-targets/{target_id}", None)
        cycle = target["cycles"][0]
        for slot in cycle["slots"]:
            launch_id = uuid.uuid4().hex
            self.app.save_state(f"launches/{launch_id}", {
                "id": launch_id,
                "review_cycle": {"target_id": target_id, "cycle_id": cycle["id"],
                                 "plan_id": first["id"], "slot": slot["number"]},
                "choice": slot["choice"], "slot": slot["number"], "task_digest": "a" * 64,
                "status": "exited", "exit_code": 0})
            slot["launches"].append({"plan_id": first["id"], "launch_id": launch_id})
        mp.save_target(self.app, target)

        later = cli("review-plan", "--cwd", str(repo), "--base", "HEAD", "--facts", str(facts),
                    "--target", target_id, "--cycle-key", "later", "--previous-cycle", cycle["id"])
        later_guidance = later["review_guidance"]
        self.assertEqual([item["slot"] for item in later_guidance["review_slots"]], [1])
        self.assertEqual([item["slot"] for item in later_guidance["review_options"][0]["slots"]], [1])
        self.assertIn("--review-target", later_guidance["next_action_command"])
        self.assertIn("--review-cycle", later_guidance["next_action_command"])

        (repo / "app.py").write_text("changed to refresh the active cycle\n")
        cli("review-plan", "--cwd", str(repo), "--base", "HEAD", "--facts", str(facts),
            "--target", target_id, "--cycle-key", "later", "--previous-cycle", cycle["id"])
        (repo / "app.py").write_text("original\n")
        historical = cli("review-inspect", later["id"])["review_guidance"]
        self.assertEqual(historical["selection_status"], "current")
        self.assertEqual(historical["scope_status"], "current")
        self.assertEqual(historical["review_cycle_status"], "stale")
        self.assertEqual(historical["plan_status"], "stale")
        self.assertEqual([item["slot"] for item in historical["review_slots"]], [1])
        self.assertIsNone(historical["next_action_command"])

    def test_qwen_review_guidance_keeps_fresh_authorization_per_launch(self):
        self.activate("Codex-Qwen")
        repo = self.repo()
        facts = self.facts()
        result = self.run_cli(dict(os.environ), "review-plan", "--cwd", str(repo), "--base", "HEAD",
                              "--facts", str(facts))
        self.assertEqual(result.returncode, 0, result.stderr)
        guidance = json.loads(result.stdout)["review_guidance"]
        self.assertEqual(guidance["fresh_review_authorization_required"], True)
        self.assertIn("--review-authorization", guidance["next_action"])
        self.assertIn("--review-authorization", guidance["next_action_command"])

    def test_codex_muse_single_advanced_review_requires_permission_before_launch(self):
        self.assert_single_advanced_review_requires_permission("Codex-Muse")

    def test_profile_switch_updates_future_choice(self):
        self.activate()
        before = self.call("resolve", "--role", "orchestrator")
        self.activate("Codex-only")
        after = self.call("resolve", "--role", "orchestrator")
        self.assertEqual(before["harness"], "claude")
        self.assertEqual(after["harness"], "codex")

    def test_invalid_and_unknown_profile_leave_active_untouched(self):
        self.activate()
        before = self.app.config_path.read_bytes()
        with self.assertRaises(mp.PreferenceError):
            self.call("activate", "missing", "--orchestrators", "ask")
        profile = self.call("show", "Codex-only")
        profile["implementer"]["model"] = "claude-opus"
        p = self.root / "bad.json"; p.write_text(json.dumps(profile))
        with self.assertRaises(mp.PreferenceError):
            self.call("profile-put", "Codex-only", "--file", str(p))
        self.assertEqual(before, self.app.config_path.read_bytes())

    def test_create_profile(self):
        profile = self.call("show", "Claude-Codex")
        profile.pop("review_policy")
        p = self.root / "new.json"; p.write_text(json.dumps(profile))
        self.call("profile-put", "My-profile", "--file", str(p))
        self.activate("My-profile")
        self.assertEqual(self.call("status")["active"]["profile"], "My-profile")

    def test_noop_activation_sends_nothing(self):
        self.activate()
        with patch.object(mp.App, "current", side_effect=AssertionError("notification storm")):
            self.assertFalse(self.call("activate", "Claude-Codex", "--orchestrators", "ask")["changed"])

    def test_exact_model_update_changes_all_profiles_without_lookup(self):
        self.activate("Codex-only")
        with patch.object(mp.App, "current", return_value={"items": [], "truncated": False}):
            self.call("model-set", "codex-luna", "--harness", "codex", "--slug", "fixture-luna-99", "--efforts", "xhigh", "high",
                      "--source", "fixture provider selection", "--orchestrators", "future-only")
        self.assertEqual(self.call("resolve", "--role", "implementer")["model"], "fixture-luna-99")
        self.activate("Claude-Codex")
        self.assertEqual(self.call("resolve", "--role", "implementer")["model"], "fixture-luna-99")

    def test_light_profile_can_upgrade_sol_with_exact_reviewer_guards(self):
        self.activate("claude-codex-light")
        old = self.call("resolve", "--role", "standard_review", "--slot", "1")
        self.assertEqual((old["model"], old["effort"]), ("gpt-6.1-sol", "high"))
        original = self.app.config_path.read_bytes()
        for harness, slug, efforts in [
            ("codex", "gpt-6.2-astra", ["medium", "high"]),
            ("claude", "gpt-6.2-sol", ["medium", "high"]),
            ("codex", "gpt-6.2-sol", ["medium"]),
        ]:
            with self.subTest(harness=harness, slug=slug, efforts=efforts):
                with self.assertRaises(mp.PreferenceError):
                    self.call("model-set", "codex-sol", "--harness", harness, "--slug", slug,
                              "--efforts", *efforts, "--source", "fixture", "--orchestrators", "future-only")
                self.assertEqual(self.app.config_path.read_bytes(), original)

        with patch.object(mp.App, "current", return_value={"items": [], "truncated": False}):
            change = self.call("model-set", "codex-sol", "--harness", "codex", "--slug", "gpt-6.2-sol",
                               "--efforts", "medium", "high", "--source", "fixture",
                               "--orchestrators", "future-only")
        self.call("validate")
        sol = self.call("resolve", "--role", "standard_review", "--slot", "1")
        opus = self.call("resolve", "--role", "standard_review", "--slot", "2")
        self.assertEqual((sol["model"], sol["effort"]), ("gpt-6.2-sol", "high"))
        self.assertEqual((opus["model"], opus["effort"]), ("claude-opus-5-5", "high"))
        self.assertEqual(change["notification"]["orchestrators"], "future-only")
        self.assertNotEqual(old["selection"], sol["selection"])

    def test_active_exact_model_update_requires_switch_intent(self):
        self.activate()
        before = self.app.config_path.read_bytes()
        with self.assertRaises(mp.PreferenceError):
            self.call("model-set", "codex-luna", "--harness", "codex", "--slug", "fixture-luna-99", "--efforts", "xhigh", "high", "--source", "fixture")
        self.assertEqual(before, self.app.config_path.read_bytes())

    def test_unsupported_effort_and_muse_standard_rejected(self):
        config = self.app.config()
        config["models"]["codex-luna"]["efforts"] = ["medium"]
        with self.assertRaises(mp.PreferenceError): mp.validate(config)
        config = self.app.config()
        config["models"]["muse-spark"]["slug"] = "muse-spark-1.3"
        with self.assertRaises(mp.PreferenceError): mp.validate(config)

    def test_concurrent_process_switches_publish_complete_selection(self):
        def switch(name):
            return subprocess.run([sys.executable, str(SCRIPT), "--config-dir", str(self.config_dir),
                                   "--state-dir", str(self.state_dir), "activate", name,
                                   "--orchestrators", "future-only"], capture_output=True)
        # No enrolled recipients, so no actual messages are possible.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(switch, ["Codex-only", "Claude-only"] * 3))
        config = self.app.config()
        snapshot = mp.selection(config)
        for role in mp.ROLES:
            entries = snapshot["roles"][role]
            for c in entries if isinstance(entries, list) else [entries]:
                self.assertEqual(c["harness"], "codex" if config["active"] == "Codex-only" else "claude")
        self.assertTrue(all(r.returncode in (0, 1, 2) for r in results))
        self.assertTrue(any(r.returncode in (0, 2) for r in results))

    def test_switching_away_and_back_cannot_reuse_old_receipts(self):
        self.activate("Codex-only")
        first = self.call("status")["active"]["selection"]
        self.activate("Claude-only"); self.activate("Codex-only")
        self.assertNotEqual(first, self.call("status")["active"]["selection"])

    def test_explicit_enrollment_and_session_liveness(self):
        with patch.object(mp.App, "current", return_value=self.inventory()):
            result = self.call("enroll", "lane", "--workspace", self.workspace, "--surface", self.surface,
                               "--session", "outgoing", "--harness", "claude")
        self.assertEqual(result["session"], "outgoing")
        wrong = dict(self.recipient, session="other")
        self.assertEqual(self.app.live(wrong, self.inventory()), "unknown")
        inv = self.inventory(); inv["items"][0]["agents"][0]["state"] = "ended"
        self.assertEqual(self.app.live(self.recipient, inv), "inactive")

    def test_notify_all_enrolled_active_receipts_and_retry(self):
        other = dict(self.recipient, surface=str(uuid.uuid4()), session="second")
        self.app.save_state("orchestrators", {"one": self.recipient, "two": other})
        inventory = self.inventory(); inventory["items"].extend(self.inventory(other)["items"])
        calls = []
        def send(argv, stdin=None):
            calls.append(argv)
            if argv[3] == self.surface:
                raise mp.PreferenceError("fixture send failed")
            return {"message_id": "queued-id"}
        with patch.object(mp.App, "current", return_value=inventory), patch.object(mp, "run_json", side_effect=send):
            result = self.call("activate", "Codex-only", "--orchestrators", "ask")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["notification"]["recipients"]["one"]["status"], "failed")
        self.assertEqual(result["notification"]["recipients"]["two"]["status"], "queued")
        self.assertEqual(self.app.config()["active"], "Codex-only")
        with patch.object(mp.App, "current", return_value=inventory), patch.object(mp, "run_json", return_value={"id": "retry"}) as sender:
            retried = self.call("notify", "--retry")
        self.assertEqual(sender.call_count, 1)
        self.assertTrue(all(r["status"] == "queued" for r in retried["notification"]["recipients"].values()))

    def test_future_only_model_notice_keeps_scope_exclusions_on_retry(self):
        scoped = dict(self.recipient, surface=str(uuid.uuid4()), session="scoped")
        enrollments = {"shared": self.recipient, "uses-other-registry": scoped}
        self.app.save_state("orchestrators", enrollments)
        self.activate("Codex-only")
        inventory = self.inventory()
        inventory["items"].extend(self.inventory(scoped)["items"])
        update = ("model-set", "codex-sol", "--harness", "codex", "--slug", "gpt-6.2-sol",
                  "--efforts", "medium", "high", "--source", "fixture")
        with patch.object(mp.App, "current", return_value=inventory), patch.object(mp, "run_json") as sender:
            before = self.app.config_path.read_bytes()
            with self.assertRaises(mp.PreferenceError):
                self.call(*update, "--orchestrators", "ask", "--exclude-orchestrator", "uses-other-registry")
            with self.assertRaises(mp.PreferenceError):
                self.call(*update, "--orchestrators", "future-only", "--exclude-orchestrator", "typo")
            self.assertEqual(self.app.config_path.read_bytes(), before)
            sender.assert_not_called()

            sender.side_effect = mp.PreferenceError("fixture send failed")
            notice = self.call(*update, "--orchestrators", "future-only",
                               "--exclude-orchestrator", "uses-other-registry")["notification"]
            self.assertEqual(set(notice["recipients"]), {"shared"})
            sender.assert_called_once()
            self.assertEqual(sender.call_args.args[0][3], self.surface)

            sender.reset_mock(side_effect=True)
            sender.return_value = {"id": "queued"}
            retried = self.call("notify", "--retry")["notification"]
            sender.assert_called_once()
            self.assertEqual(sender.call_args.args[0][3], self.surface)
            self.assertEqual(retried["recipients"]["shared"]["status"], "queued")
            self.assertEqual(retried["excluded_orchestrators"], ["uses-other-registry"])
        self.assertEqual(self.app.state("orchestrators", {}), enrollments)

    def test_unknown_recipient_never_targeted(self):
        self.app.save_state("orchestrators", {"lane": dict(self.recipient, session="stale")})
        with patch.object(mp.App, "current", return_value=self.inventory()), patch.object(mp, "run_json") as sender:
            result = self.call("activate", "Claude-Codex", "--orchestrators", "ask")
        sender.assert_not_called()
        self.assertEqual(result["notification"]["recipients"]["lane"]["status"], "unknown")

    def test_action_message_uses_native_delivery_without_prompt_submit(self):
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                recipient = dict(self.recipient, harness=harness)
                receipt = {"id": str(uuid.uuid4()), "state": "queued"}
                with patch.object(mp, "run_json", return_value=receipt) as sender, patch.object(mp, "run_command") as submit:
                    result = self.app.send_action(recipient, "Handoff action within existing authorization.")
                self.assertEqual(result["receipt"], receipt)
                self.assertEqual(result["wake"]["status"], "native")
                self.assertEqual(sender.call_args.args, (["cmux", "agent", "message", recipient["surface"], "--from", "model-preferences", "--json", "--", "-"], "Handoff action within existing authorization."))
                submit.assert_not_called()

    def test_legacy_wake_recheck_never_submits_or_polls_native_inventory(self):
        recipient = dict(self.recipient, harness="codex")
        with patch.object(mp.App, "current", side_effect=AssertionError("unexpected inventory request")), patch.object(mp, "run_command") as submit:
            result = self.app.native_delivery(recipient, {"id": str(uuid.uuid4()), "state": "queued"})
        self.assertEqual(result["status"], "native")
        submit.assert_not_called()

    def test_native_send_failure_is_not_reported_as_queued(self):
        with patch.object(mp, "run_json", side_effect=mp.PreferenceError("native unavailable")), patch.object(mp, "run_command") as submit:
            with self.assertRaises(mp.PreferenceError):
                self.app.send_action(self.recipient, "Action notification.")
        submit.assert_not_called()

    def test_future_only_announcement_never_requests_additional_wake(self):
        self.app.save_state("orchestrators", {"lane": dict(self.recipient, harness="codex")})
        with patch.object(mp.App, "current", return_value=self.inventory(dict(self.recipient, harness="codex"))), patch.object(mp, "run_json", return_value={"id": str(uuid.uuid4())}), patch.object(mp, "run_command") as submit:
            result = self.call("activate", "Codex-only", "--orchestrators", "future-only")
        submit.assert_not_called()
        self.assertEqual(result["notification"]["recipients"]["lane"]["wake"]["status"], "not_requested")

    def test_retry_keeps_existing_native_message_without_duplicate_send(self):
        recipient = dict(self.recipient, harness="codex")
        self.app.save_state("orchestrators", {"lane": recipient})
        receipt = {"id": str(uuid.uuid4()), "state": "queued"}
        with patch.object(mp.App, "current", return_value=self.inventory(recipient)), patch.object(mp, "run_json", return_value=receipt) as sender, patch.object(mp, "run_command") as submit:
            first = self.call("activate", "Codex-only", "--orchestrators", "handoff")
            retried = self.call("notify", "--retry")
        self.assertEqual(sender.call_count, 1)
        submit.assert_not_called()
        self.assertEqual(first["notification"]["recipients"]["lane"]["wake"]["status"], "native")
        self.assertEqual(retried["notification"]["recipients"]["lane"]["receipt"]["id"], receipt["id"])

    def test_native_or_already_delivered_messages_need_no_additional_submit(self):
        with patch.object(mp.App, "current", side_effect=AssertionError("unexpected inventory request")), patch.object(mp, "run_command") as submit:
            self.assertEqual(self.app.native_delivery(self.recipient, {})["status"], "native")
            self.assertEqual(self.app.native_delivery(dict(self.recipient, harness="codex"), {"state": "delivered"})["status"], "unnecessary")
        submit.assert_not_called()

    def test_facilitator_records_and_sends_close_approval_without_closing(self):
        self.activate()
        successor = dict(self.recipient, harness="codex", surface=str(uuid.uuid4()), session="successor")
        hid = uuid.uuid4().hex
        self.app.save_state(f"handoffs/{hid}", {"id": hid, "status": "transferred", "outgoing": self.recipient,
            "choice": {"harness": "codex"}, "successor_surface": successor["surface"], "successor_session": successor["session"]})
        def cmux(argv, stdin=None):
            if "identify" in argv: return {"caller": {"surface_id": self.surface}}
            self.assertIn("message", argv)
            saved = self.app.state(f"handoffs/{hid}", {})
            self.assertEqual(saved["close_approval"]["authorization"], "Authority approved this fixture")
            return {"id": str(uuid.uuid4()), "state": "queued"}
        with patch.object(mp.App, "current", return_value=self.inventory(successor)), patch.object(mp, "run_json", side_effect=cmux), patch.object(mp, "run_command", return_value="OK"):
            with self.assertRaises(mp.PreferenceError): self.call("handoff-approve-close", hid, "--authorization", "")
            result = self.call("handoff-approve-close", hid, "--authorization", "Authority approved this fixture")
        self.assertEqual(result["status"], "transferred")
        self.assertEqual(result["notification"]["wake"]["status"], "native")

    def test_recover_interrupted_notification(self):
        config = self.app.config(); config["active"] = "Codex-only"; mp.write(self.app.config_path, config)
        self.assertTrue(self.call("status")["notification_pending"])
        with patch.object(mp.App, "current", return_value={"items": [], "truncated": False}):
            self.call("notify", "--retry", "--orchestrators", "ask")
        self.assertFalse(self.call("status")["notification_pending"])

    def test_review_triggers_and_ten_file_threshold(self):
        self.activate(); repo = self.repo()
        self.assertEqual(self.plan(repo)["role"], "standard_review")
        self.assertEqual(self.plan(repo, repairing_previous_fix=True)["role"], "advanced_review")
        for n in range(9): (repo / f"source{n}.py").write_text("change\n")
        self.assertEqual(self.plan(repo)["role"], "standard_review")
        (repo / "tenth.py").write_text("change\n")
        self.assertIn("file_threshold", self.plan(repo)["reasons"])
        (repo / "docs").mkdir()
        for n in range(20): (repo / "docs" / f"{n}.md").write_text("text\n")
        self.assertEqual(len(self.plan(repo)["counted_files"]), 10)

    def test_real_cli_dispatch_composes_guidance_for_each_supported_native_reviewer(self):
        repo = self.repo()
        config = self.app.config()
        muse_profile = json.loads(json.dumps(config["profiles"]["Codex-only"]))
        muse_profile.pop("review_policy")
        for role in ("standard_review", "advanced_review"):
            muse_profile[role] = [{"model": "muse-spark", "effort": "high"}]
        config["profiles"]["Muse-review"] = muse_profile
        mp.write(self.app.config_path, config)

        cases = (("Codex-only", "standard_review", "codex"),
                 ("Codex-only", "advanced_review", "codex"),
                 ("Claude-only", "standard_review", "claude"),
                 ("Claude-only", "advanced_review", "claude"),
                 ("Muse-review", "standard_review", "muse"),
                 ("Muse-review", "advanced_review", "muse"))
        for profile, role, harness in cases:
            with self.subTest(profile=profile, role=role, harness=harness):
                self.activate(profile)
                task_bytes = (f"Approved purpose for {profile}/{role}.\r\n"
                              "Preserve this exact line ending and unicode: ✓.\r\n").encode("utf-8")
                brief = self.root / f"{profile}-{role}-brief.txt"
                brief.write_bytes(task_bytes)
                plan = self.plan(repo, repairing_previous_fix=role == "advanced_review")
                configured_choice = self.call("resolve", "--role", role, "--slot", "1")
                args = ["dispatch", "--role", role, "--slot", "1", "--cwd", str(repo),
                        "--task-file", str(brief), "--review-plan", plan["id"]]
                if role == "advanced_review":
                    approval = self.call("approve-review", plan["id"], "--authorization",
                                         "Fixture authority approved this review.")
                    args.extend(("--approval", approval["id"]))
                capture, env = self.fake_native_provider(harness)
                result = self.run_cli(env, *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                received = json.loads(capture.read_text())
                task_text = task_bytes.decode("utf-8")
                expected = (mp.DELEGATION + " Use the current researcher role for authorized research. "
                            "Verify its findings.\n\n" +
                            mp.reviewer_guidance.compose_reviewer_prompt(role, task_text))
                self.assertEqual(received["prompt"], expected)
                self.assertTrue(received["prompt"].endswith(task_text))
                launch_id = json.loads(result.stdout)["launch"]
                launch = self.app.state(f"launches/{launch_id}", None)
                self.assertEqual(launch["choice"]["role"], role)
                self.assertEqual(launch["choice"], configured_choice)
                self.assertEqual(launch["task_digest"], mp.hashlib.sha256(task_bytes).hexdigest())
                self.assertEqual(brief.read_bytes(), task_bytes)
                if harness == "claude":
                    self.assertEqual(received["argv"][0], "-p")
                elif harness == "muse":
                    self.assertEqual(received["argv"][0], "exec")
                    self.assertEqual(received["argv"][received["argv"].index("--contributor-mode") + 1], "on")
                else:
                    self.assertEqual(received["argv"][0], "exec")

    def test_real_cli_light_cycle_dispatch_composes_guidance_after_slot_resolution(self):
        self.activate("claude-codex-light")
        repo = self.repo()
        target = self.create_review_target(repo)["id"]
        plan = self.light_plan(repo, target, "review-guidance-first-cycle")
        cycle = self.target_state(target)["cycles"][0]
        task_bytes = b"Review the approved cycle scope.\r\n"
        brief = self.root / "light-cycle-review.txt"
        brief.write_bytes(task_bytes)
        capture, env = self.fake_native_provider("codex")
        result = self.run_cli(env, "dispatch", "--role", "standard_review", "--slot", "1",
                              "--cwd", str(repo), "--task-file", str(brief), "--review-plan", plan["id"],
                              "--review-target", target, "--review-cycle", cycle["id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        received = json.loads(capture.read_text())
        task_text = task_bytes.decode()
        expected = (mp.DELEGATION + " Use the current researcher role for authorized research. "
                    "Verify its findings.\n\n" +
                    mp.reviewer_guidance.compose_reviewer_prompt("standard_review", task_text))
        self.assertEqual(received["prompt"], expected)
        self.assertTrue(received["prompt"].endswith(task_text))
        launch_id = json.loads(result.stdout)["launch"]
        launch = self.app.state(f"launches/{launch_id}", None)
        self.assertEqual(launch["task_digest"], mp.hashlib.sha256(task_bytes).hexdigest())
        self.assertEqual(launch["review_cycle"]["cycle_id"], cycle["id"])
        self.assertEqual(launch["review_cycle"]["slot"], 1)
        self.assertEqual((launch["choice"]["model"], launch["choice"]["effort"]),
                         ("gpt-6.1-sol", "high"))

    def test_real_cli_nonreview_prompt_is_unchanged_and_review_checks_precede_provider(self):
        repo = self.repo()
        self.activate("Codex-only")
        capture, env = self.fake_native_provider("codex")
        task_bytes = b"Unchanged implementer assignment.\r\n"
        brief = self.root / "implementer-brief.txt"
        brief.write_bytes(task_bytes)
        result = self.run_cli(env, "dispatch", "--role", "implementer", "--cwd", str(repo),
                              "--task-file", str(brief))
        self.assertEqual(result.returncode, 0, result.stderr)
        received = json.loads(capture.read_text())
        self.assertEqual(received["prompt"], mp.DELEGATION +
                         " Use the current researcher role for authorized research. Verify its findings.\n\n" +
                         task_bytes.decode())
        launch = self.app.state(f"launches/{json.loads(result.stdout)['launch']}", None)
        self.assertEqual(launch["task_digest"], mp.hashlib.sha256(task_bytes).hexdigest())

        capture.unlink()
        review_brief = self.root / "unplanned-review.txt"
        review_brief.write_text("Review without an approved plan.")
        refused = self.run_cli(env, "dispatch", "--role", "standard_review", "--slot", "1",
                               "--cwd", str(repo), "--task-file", str(review_brief))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("requires a review-plan", refused.stderr)
        self.assertFalse(capture.exists())

        plan = self.plan(repo, repairing_previous_fix=True)
        refused = self.run_cli(env, "dispatch", "--role", "advanced_review", "--slot", "1",
                               "--cwd", str(repo), "--task-file", str(review_brief),
                               "--review-plan", plan["id"])
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("explicit permission", refused.stderr)
        self.assertFalse(capture.exists())

        config = self.app.config()
        config["models"]["opencode-review"] = {
            "harness": "opencode", "provider_id": "fixture-provider", "slug": "fixture-review-model",
            "efforts": ["max"], "source": "synthetic unsupported-review route fixture"}
        opencode_profile = json.loads(json.dumps(config["profiles"]["Codex-only"]))
        opencode_profile.pop("review_policy")
        opencode_profile["standard_review"] = [{"model": "opencode-review", "effort": "max"}]
        opencode_profile["advanced_review"] = [{"model": "opencode-review", "effort": "max"}]
        config["profiles"]["OpenCode-review"] = opencode_profile
        mp.write(self.app.config_path, config)
        self.activate("OpenCode-review")
        plan = self.plan(repo)
        refused = self.run_cli(env, "dispatch", "--role", "standard_review", "--slot", "1",
                               "--cwd", str(repo), "--task-file", str(review_brief),
                               "--review-plan", plan["id"], "--opencode-manifest", str(self.root / "unused.json"))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("OpenCode dispatch supports", refused.stderr)
        self.assertFalse(capture.exists())

    def test_review_guidance_cli_prints_without_config_or_state(self):
        config_dir = self.root / "print-config-does-not-exist"
        state_dir = self.root / "print-state-does-not-exist"
        result = subprocess.run([sys.executable, str(SCRIPT), "--config-dir", str(config_dir),
                                 "--state-dir", str(state_dir), "review-guidance", "--print"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, mp.reviewer_guidance.REVIEWER_GUIDANCE)
        self.assertFalse(config_dir.exists())
        self.assertFalse(state_dir.exists())

    def test_money_and_timezone_paths_trigger_even_one_file(self):
        self.activate(); repo = self.repo()
        (repo / "money").mkdir(); (repo / "money" / "round.py").write_text("change")
        self.assertIn("money_path", self.plan(repo)["reasons"])
        (repo / "money" / "round.py").unlink()
        (repo / "timezone").mkdir(); (repo / "timezone" / "offset.py").write_text("change")
        self.assertIn("time_zone_path", self.plan(repo)["reasons"])

    def test_advanced_permission_bound_to_scope_and_profile(self):
        self.activate(); repo = self.repo(); plan = self.plan(repo, money=True)
        approval = self.call("approve-review", plan["id"], "--authorization", "Authority approved this fixture")
        brief = self.root / "brief"; brief.write_text("Check the calculation.")
        args = ["dispatch", "--role", "advanced_review", "--slot", "1", "--cwd", str(repo),
                "--task-file", str(brief), "--review-plan", plan["id"]]
        with self.assertRaises(mp.PreferenceError): self.call(*args)
        (repo / "app.py").write_text("new change")
        with self.assertRaises(mp.PreferenceError): self.call(*args, "--approval", approval["id"])
        self.assertFalse((self.state_dir / "launches").exists())

    def test_dispatch_records_actual_choice_and_exit_status(self):
        self.activate("Codex-only")
        brief = self.root / "brief"; brief.write_text("fixture task")
        with patch.dict(os.environ, STARFLEET_CONTEXT_DISPATCH="parent-fresh-token"), patch.object(mp.shutil, "which", return_value="codex"), patch.object(mp.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)) as runner, patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd, **kwargs: mp.subprocess.run(argv, cwd=cwd, env=env)):
            result = self.call("dispatch", "--role", "implementer", "--cwd", str(self.root), "--task-file", str(brief))
        self.assertEqual(result["exit_code"], 7)
        record = self.app.state(f"launches/{result['launch']}", None)
        self.assertEqual(record["choice"]["model"], "gpt-6-luna")
        self.assertEqual(runner.call_args.kwargs["env"]["STARFLEET_MODEL_LAUNCH"], result["launch"])
        self.assertNotIn("STARFLEET_CONTEXT_DISPATCH", runner.call_args.kwargs["env"])
        self.activate("Claude-only")
        recovered = self.call("context", "--launch", result["launch"])
        self.assertTrue(recovered["different_selection"])
        self.assertEqual(recovered["running"]["choice"]["harness"], "codex")

    def test_configured_authority_can_decline_advanced_and_choose_standard_for_same_scope(self):
        self.activate(); repo = self.repo(); plan = self.plan(repo, money=True)
        decision = self.call("approve-review", plan["id"], "--standard-instead", "--authorization", "Authority chose standard review for this fixture")
        brief = self.root / "brief"; brief.write_text("Check correctness.")
        original = subprocess.run
        for slot in ("1", "2"):
            with patch.object(mp.shutil, "which", return_value="harness"), patch.object(mp.subprocess, "run", wraps=mp.subprocess.run) as runner, patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd, **kwargs: mp.subprocess.run(argv, cwd=cwd, env=env)):
                # Keep real Git scope checks while replacing only the harness invocation.
                def run(argv, **kwargs):
                    return subprocess.CompletedProcess(argv, 0) if argv[0] in ("codex", "claude") else original(argv, **kwargs)
                runner.side_effect = run
                result = self.call("dispatch", "--role", "standard_review", "--slot", slot, "--cwd", str(repo),
                                   "--task-file", str(brief), "--review-plan", plan["id"], "--approval", decision["id"])
            self.assertEqual(result["exit_code"], 0)

    def test_missing_harness_no_fallback(self):
        self.activate("Codex-only")
        choice = self.call("resolve", "--role", "implementer")
        with patch.object(mp.shutil, "which", return_value=None), self.assertRaises(mp.PreferenceError):
            mp.argv_for(choice, "task", self.root)

    def test_hook_startup_compact_resume_preserves_running_choice(self):
        self.activate("Codex-only")
        choice = self.call("resolve", "--role", "implementer")
        ticket = uuid.uuid4().hex
        self.app.save_state(f"launches/{ticket}", {"choice": choice, "cwd": str(self.root)})
        payload = {"session_id": "task-session", "cwd": str(self.root), "source": "startup"}
        with patch.dict(os.environ, STARFLEET_MODEL_LAUNCH=ticket), patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            started = self.call("hook", "--harness", "codex")
        self.assertIn("gpt-6-luna", started["hookSpecificOutput"]["additionalContext"])
        self.activate("Claude-only")
        for source in ("compact", "resume"):
            payload["source"] = source
            with patch.dict(os.environ, {}, clear=True), patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                text = self.call("hook", "--harness", "codex")["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Claude-only", text)
            self.assertIn("gpt-6-luna", text)
        payload.update(session_id="nested", source="startup")
        with patch.dict(os.environ, STARFLEET_MODEL_LAUNCH=ticket), patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), self.assertRaises(mp.PreferenceError):
            self.call("hook", "--harness", "codex")

    def test_claude_prompt_hook_refreshes_choices_without_rebinding_task(self):
        self.activate("Claude-only")
        self.assertEqual(set(self.call("hook-config", "--harness", "claude")["hooks"]), {"UserPromptSubmit"})
        choice = self.call("resolve", "--role", "implementer")
        ticket = uuid.uuid4().hex
        self.app.save_state(f"launches/{ticket}", {"choice": choice, "cwd": str(self.root)})
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "claude-task", "cwd": str(self.root), "prompt": "Continue."}
        with patch.dict(os.environ, STARFLEET_MODEL_LAUNCH=ticket), patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            started = self.call("hook", "--harness", "claude")["hookSpecificOutput"]
        self.assertEqual(started["hookEventName"], "UserPromptSubmit")
        self.assertIn("claude-haiku-5-5", started["additionalContext"])
        self.activate("Codex-only")
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            text = self.call("hook", "--harness", "claude")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("active profile Codex-only", text)
        self.assertIn("This running task actually launched with profile Claude-only", text)
        self.assertIn("claude-haiku-5-5", text)
        self.assertLess(len(text.encode()), 2000)

    def test_codex_config_keeps_session_hook_and_rejects_unverified_prompt_event(self):
        self.activate()
        config = self.call("hook-config", "--harness", "codex")
        self.assertEqual(set(config["hooks"]), {"SessionStart"})
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "unverified", "cwd": str(self.root)}
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), self.assertRaises(mp.PreferenceError):
            self.call("hook", "--harness", "codex")

    def test_handoff_new_tab_same_workspace_and_permission_gates(self):
        self.activate(); self.app.save_state("orchestrators", {"lane": self.recipient})
        successor = str(uuid.uuid4())
        brief = self.root / "brief"; brief.write_text("Current task and evidence.")
        roadmap = self.root / "roadmap"; roadmap.write_text("Authoritative goal.")
        tree = {"windows": [{"workspaces": [{"id": self.workspace, "panes": [{"id": self.pane, "surfaces": [{"id": self.surface}]}]}]}]}
        def cmux(argv, stdin=None):
            if "tree" in argv: return tree
            if "new-surface" in argv:
                self.assertEqual(argv[argv.index("--workspace") + 1], self.workspace)
                self.assertEqual(argv[argv.index("--pane") + 1], self.pane)
                self.assertNotIn("new-workspace", argv)
                self.assertIn("STARFLEET_MODEL_LAUNCH=", argv[argv.index("--command") + 1])
                return {"surface_id": successor, "workspace_id": self.workspace}
            return {"caller": {"surface_id": successor}}
        with patch.object(mp.App, "current", return_value=self.inventory()), patch.object(mp, "run_json", side_effect=cmux), patch.object(mp.shutil, "which", return_value="claude"):
            record = self.call("handoff-start", "lane", "--brief", str(brief), "--roadmap", str(roadmap), "--cwd", str(self.root))
        self.assertEqual(record["status"], "briefing")
        incoming = dict(self.recipient, surface=successor, session="successor")
        inv = self.inventory(); inv["items"].extend(self.inventory(incoming)["items"])
        with patch.object(mp.App, "current", return_value=inv), patch.object(mp, "run_json", return_value={"caller": {"surface_id": successor}}):
            with self.assertRaises(mp.PreferenceError): self.call("handoff-transfer", record["id"], "--outgoing-paused")
            ready = self.call("handoff-ready", record["id"], "--remaining-questions", "none")
        self.assertEqual(ready["status"], "ready")
        with patch.object(mp.App, "current", return_value=inv), patch.object(mp, "run_json", return_value={"caller": {"surface_id": self.surface}}):
            transferred = self.call("handoff-transfer", record["id"], "--outgoing-paused")
        self.assertEqual(transferred["status"], "transferred")
        self.assertEqual(self.app.state("orchestrators", {})["lane"]["surface"], successor)
        with patch.object(mp.App, "current", return_value=inv), patch.object(mp, "run_json", return_value={"caller": {"surface_id": successor}}) as close:
            with self.assertRaises(mp.PreferenceError):
                self.call("handoff-close", record["id"], "--dependent-jobs-cleared", "--authorization", "")
            self.assertFalse(any("close-surface" in c.args[0] for c in close.call_args_list))
            closed = self.call("handoff-close", record["id"], "--dependent-jobs-cleared", "--authorization", "Authority approved fixture closure")
            self.assertTrue(any("close-surface" in c.args[0] and "--force" in c.args[0] for c in close.call_args_list))
        self.assertEqual(closed["status"], "closed")

    def test_close_error_after_tab_removal_is_reconciled_without_retry(self):
        self.activate()
        successor = str(uuid.uuid4())
        hid = uuid.uuid4().hex
        record = {"id": hid, "name": "lane", "status": "transferred", "outgoing": self.recipient,
                  "successor_surface": successor}
        self.app.save_state(f"handoffs/{hid}", record)
        calls = []
        def cmux(argv, stdin=None):
            calls.append(argv)
            if "identify" in argv: return {"caller": {"surface_id": successor}}
            if "close-surface" in argv: raise mp.PreferenceError("error after removal")
            if "tree" in argv: return {"windows": []}
            self.fail(str(argv))
        with patch.object(mp.App, "current", return_value=self.inventory()), patch.object(mp, "run_json", side_effect=cmux):
            result = self.call("handoff-close", hid, "--dependent-jobs-cleared", "--authorization", "Authority approved fixture closure")
        self.assertEqual(result["status"], "closed")
        self.assertIn("error after removal", result["close_error"])
        self.assertEqual(sum("close-surface" in argv for argv in calls), 1)

    def test_context_injector_adapter_preserves_brief_and_path_harness(self):
        self.activate("Codex-only")
        brief = self.root / "brief"; brief.write_text("Exact authorized brief.")
        injector = self.root / "context_injector.py"
        # This checks the boundary contract without changing or installing the owning app.
        injector.write_text("# fixture interface\n")
        with patch.object(mp.shutil, "which", return_value="codex"), \
                patch.object(mp, "process_birth_identity", return_value=None), \
                patch.object(mp.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as runner, \
                patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd, **kwargs:
                             mp.subprocess.run(argv, cwd=cwd, env=env)):
            self.call("dispatch", "--role", "implementer", "--cwd", str(self.root), "--task-file", str(brief),
                      "--context-injector", str(injector), "--owner-worktree", str(self.root))
        argv = runner.call_args.args[0]
        self.assertEqual(argv[argv.index("--task-file") + 1], str(brief))
        self.assertEqual(argv[argv.index("--") + 1], "codex")
        self.assertEqual(brief.read_text(), "Exact authorized brief.")

    def test_pattern_buffer_alias_preserves_existing_adapter_contract(self):
        self.activate("Codex-only")
        brief = self.root / "pattern-buffer-brief"; brief.write_text("Exact authorized brief.")
        injector = self.root / "pattern_buffer.py"; injector.write_text("# fixture interface\n")
        with patch.object(mp.shutil, "which", return_value="codex"), \
                patch.object(mp, "process_birth_identity", return_value=None), \
                patch.object(mp.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as runner, \
                patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd, **kwargs:
                             mp.subprocess.run(argv, cwd=cwd, env=env)):
            self.call("dispatch", "--role", "implementer", "--cwd", str(self.root), "--task-file", str(brief),
                      "--pattern-buffer", str(injector), "--owner-worktree", str(self.root))
        argv = runner.call_args.args[0]
        self.assertEqual(argv[argv.index("--task-file") + 1], str(brief))
        self.assertEqual(argv[argv.index("--") + 1], "codex")
        self.assertEqual(brief.read_text(), "Exact authorized brief.")

    def test_unsupported_adapter_names_role_harness_and_plain_path_before_launch(self):
        brief = self.root / "brief"; brief.write_text("Exact authorized brief.")
        for profile, role, harness in (("Codex-Muse", "implementer", "muse"),
                                       ("Codex-only", "researcher", "codex"),
                                       ("Claude-only", "researcher", "claude")):
            self.activate(profile)
            with self.subTest(profile=profile, role=role), patch.object(mp, "argv_for") as argv:
                with self.assertRaises(mp.PreferenceError) as error:
                    self.call("dispatch", "--role", role, "--cwd", str(self.root), "--task-file", str(brief),
                              "--context-injector", str(self.root / "ci.py"), "--owner-worktree", str(self.root))
                self.assertIn(f"role {role} on harness {harness}", str(error.exception))
                self.assertIn("plain Model Preferences dispatch", str(error.exception))
                self.assertIn("retaining applicable review/permission flags", str(error.exception))
                self.assertIn("#pattern-buffer-support", str(error.exception))
                argv.assert_not_called()
            self.assertFalse((self.state_dir / "launches").exists())
            self.assertEqual(brief.read_text(), "Exact authorized brief.")

    def test_reviewer_adapter_preserves_review_permission_checks(self):
        self.activate("Codex-only")
        repo = self.repo()
        plan = self.plan(repo, money=True)
        brief = self.root / "review.txt"; brief.write_text("Authorized review scope.")
        args = ("dispatch", "--role", "advanced_review", "--cwd", str(repo), "--task-file", str(brief),
                "--review-plan", plan["id"], "--context-injector", str(self.root / "ci.py"), "--owner-worktree", str(repo))
        with self.assertRaisesRegex(mp.PreferenceError, "configured authority"):
            self.call(*args)
        approval = self.call("approve-review", plan["id"], "--authorization", "Authority approved this fixture")
        with patch.object(mp, "argv_for") as argv, self.assertRaisesRegex(mp.PreferenceError, "role advanced_review on harness codex"):
            self.call(*args, "--approval", approval["id"])
        argv.assert_not_called()
        self.assertFalse((self.state_dir / "launches").exists())

    def test_real_cli_with_fake_harness_executes_exact_slug(self):
        self.activate("Codex-Muse")
        bin_dir = self.root / "bin"; bin_dir.mkdir()
        fake = bin_dir / "muse"
        fake.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
        fake.chmod(0o755)
        brief = self.root / "brief"; brief.write_text("Read-only fixture.")
        env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
        result = subprocess.run([sys.executable, str(SCRIPT), "--config-dir", str(self.config_dir), "--state-dir", str(self.state_dir),
                                 "dispatch", "--role", "implementer", "--cwd", str(self.root), "--task-file", str(brief)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("muse-spark-1.3-contributor", result.stdout)
        self.assertIn('"--contributor-mode", "on"', result.stdout)

    def test_receipt_requires_context_injector_before_recording_a_launch(self):
        self.activate("Codex-only")
        brief = self.root / "brief"; brief.write_text("Exact authorized brief.")
        with self.assertRaisesRegex(mp.PreferenceError, "--receipt-file requires"):
            self.call("dispatch", "--role", "implementer", "--cwd", str(self.root),
                      "--task-file", str(brief), "--receipt-file", str(self.root / "receipt.json"))
        self.assertFalse((self.state_dir / "launches").exists())

    def combined_receipt_cli(self, profile, harness):
        self.activate(profile)
        owner = self.repo()
        worker = self.root / "worker with spaces"
        subprocess.run(["git", "-C", str(owner), "worktree", "add", "-qb", "worker", str(worker)],
                       check=True, capture_output=True)
        injector = SCRIPT.parent.parent / "pattern-buffer/pattern_buffer.py"
        home = self.root / "home"
        settings = home / (".codex/hooks.json" if harness == "codex" else ".claude/settings.json")
        runtime = self.root / "runtime"
        env = dict(os.environ, HOME=str(home), CODEX_HOME=str(home / ".codex"))
        def ci(*args):
            result = subprocess.run([sys.executable, str(injector), *map(str, args)],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result
        ci("--cwd", owner, "init", "--default-role", "orchestrator")
        ci("install", "--harness", harness, "--settings", settings, "--runtime-dir", runtime)
        brief = self.root / "brief.txt"; brief.write_text("Exact approved task.\n")
        receipt = self.root / "receipts with spaces" / "launch.json"
        calls = self.root / "harness-calls.txt"
        bin_dir = self.root / "bin"; bin_dir.mkdir()
        fake = bin_dir / harness
        installed = runtime / f"context_injector_{harness}.py"
        fake.write_text(f'''#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path
saved=json.loads(Path({str(receipt)!r}).read_text())
assert saved['status']=='pending' and saved['session_id'] is None
with Path({str(calls)!r}).open('a') as handle: handle.write('launched\\n')
payload=json.dumps({{'hook_event_name':'SessionStart','source':'startup','session_id':'combined-child','cwd':os.getcwd()}})
result=subprocess.run([sys.executable,{str(installed)!r},'hook','--harness',{harness!r}],input=payload,capture_output=True,text=True,check=True)
print('HARNESS_RESULT='+json.dumps({{'stdin':sys.stdin.read(),'argv':sys.argv,'context':json.loads(result.stdout)}}))
''')
        fake.chmod(0o700)
        env["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]
        argv = [sys.executable, str(SCRIPT), "--config-dir", str(self.config_dir), "--state-dir", str(self.state_dir),
                "dispatch", "--role", "implementer", "--cwd", str(worker), "--task-file", str(brief),
                "--pattern-buffer", str(injector), "--owner-worktree", str(owner), "--receipt-file", str(receipt)]
        result = subprocess.run(argv, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(next(line.removeprefix("HARNESS_RESULT=") for line in result.stdout.splitlines()
                                 if line.startswith("HARNESS_RESULT=")))
        self.assertEqual(output["stdin"], brief.read_text())
        context = output["context"]["hookSpecificOutput"]["additionalContext"]
        self.assertIn("role: implementer", context)
        self.assertIn(brief.read_text(), context)
        self.assertNotIn("Track ownership", context)
        saved = json.loads(receipt.read_text())
        self.assertEqual(saved["receipt_type"], "immutable_dispatch_snapshot")
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertIn(saved["task_status_command"], result.stderr)
        current = json.loads(ci("--cwd", worker, "task-status", "--task-id", saved["task_id"]).stdout)
        self.assertEqual(current["session_id"], "combined-child")
        self.assertEqual(current["status"], "active")
        original = receipt.read_bytes()
        ci("--cwd", worker, "finish-task", "--task-id", saved["task_id"])
        # A repeated receipt or dangling symlink must fail before another harness starts.
        rejected = subprocess.run(argv, env=env, capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 1, rejected.stderr)
        self.assertEqual(receipt.read_bytes(), original)
        link = receipt.parent / "symlink.json"
        target = receipt.parent / "missing.json"
        link.symlink_to(target)
        argv[-1] = str(link)
        rejected = subprocess.run(argv, env=env, capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 1, rejected.stderr)
        self.assertFalse(target.exists())
        self.assertTrue(link.is_symlink())
        self.assertEqual(calls.read_text().splitlines(), ["launched"])

    def test_combined_codex_cli_receipt_survives_finished_task_and_rejects_overwrite(self):
        self.combined_receipt_cli("Codex-only", "codex")

    def test_combined_claude_cli_receipt_survives_finished_task_and_rejects_overwrite(self):
        self.combined_receipt_cli("Claude-only", "claude")


if __name__ == "__main__":
    unittest.main()
