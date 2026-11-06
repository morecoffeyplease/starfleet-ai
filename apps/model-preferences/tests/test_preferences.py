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
        return self.call("review-plan", "--cwd", str(repo), "--base", "HEAD", "--facts", str(self.facts(**changes)))

    def create_review_target(self, repo, target_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"):
        return self.call("review-target", "create", "--cwd", str(repo), "--id", target_id)

    def light_plan(self, repo, target, key, previous=None, **changes):
        args = ["review-plan", "--cwd", str(repo), "--base", "HEAD", "--facts", str(self.facts(**changes)),
                "--target", target, "--cycle-key", key]
        if previous:
            args.extend(("--previous-cycle", previous))
        return self.call(*args)

    def dispatch_light_review(self, repo, target, cycle, plan, slot, exit_code=0, retry=False, brief_text=None):
        brief = self.root / f"light-review-{slot}.txt"
        brief.write_text(brief_text or f"Review slot {slot} for the saved plan.")
        args = ["dispatch", "--role", "standard_review", "--slot", str(slot), "--cwd", str(repo),
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
                patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd:
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
                patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd:
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
                patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd:
                             subprocess.CompletedProcess(argv, 0)):
            result = self.call("dispatch", "--role", "advanced_review", "--slot", "1", "--cwd", str(repo),
                               "--task-file", str(brief), "--review-plan", advanced["id"],
                               "--approval", approval["id"])
        self.assertEqual(self.app.state(f"launches/{result['launch']}", None)["choice"]["role"], "advanced_review")

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
                patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd: mp.subprocess.run(argv, cwd=cwd, env=env)):
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
        config["profiles"]["team-default"] = custom
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
        with patch.object(mp.shutil, "which", return_value="codex"), patch.object(mp.subprocess, "run", side_effect=run), patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd: mp.subprocess.run(argv, cwd=cwd, env=env)):
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
        p = self.root / "new.json"; p.write_text(json.dumps(self.call("show", "Claude-Codex")))
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
        with patch.dict(os.environ, STARFLEET_CONTEXT_DISPATCH="parent-fresh-token"), patch.object(mp.shutil, "which", return_value="codex"), patch.object(mp.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)) as runner, patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd: mp.subprocess.run(argv, cwd=cwd, env=env)):
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
            with patch.object(mp.shutil, "which", return_value="harness"), patch.object(mp.subprocess, "run", wraps=mp.subprocess.run) as runner, patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd: mp.subprocess.run(argv, cwd=cwd, env=env)):
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
        with patch.object(mp.shutil, "which", return_value="codex"), patch.object(mp.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as runner, patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd: mp.subprocess.run(argv, cwd=cwd, env=env)):
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
        with patch.object(mp.shutil, "which", return_value="codex"), patch.object(mp.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as runner, patch.object(mp, "stream_codex", side_effect=lambda app, launch, argv, env, cwd: mp.subprocess.run(argv, cwd=cwd, env=env)):
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
