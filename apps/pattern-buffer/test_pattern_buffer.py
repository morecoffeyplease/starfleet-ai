"""CLI integration checks with real Git repositories and linked worktrees."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import pattern_buffer_reliability as reliability
from _test_support import isolate_state_home

SCRIPT = Path(__file__).with_name("pattern_buffer.py")
GUIDE = SCRIPT.with_name("guide.md")
_SPEC = importlib.util.spec_from_file_location("pattern_buffer_under_test", SCRIPT)
ci = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ci)


class PatternBufferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_home = isolate_state_home(self)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("-c", "maintenance.auto=false", "-c", "gc.auto=0",
                 "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--allow-empty", "-qm", "Initial")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True)

    def git_at(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True)

    def create_repo(self, root):
        root.mkdir(parents=True)
        self.git_at(root, "init", "-q")
        self.git_at(root, "-c", "maintenance.auto=false", "-c", "gc.auto=0",
                    "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "--allow-empty", "-qm", "Initial")

    def registration(self, root=None):
        worktree = root or self.root
        gitdir = Path(self.git_at(worktree, "rev-parse", "--absolute-git-dir").stdout.strip())
        return gitdir / "context-injector/config.json"

    def repo_profile(self, name="Example controls", origin="https://github.com/example/team.git"):
        path = self.root.parent / "repo-profile.json"
        path.write_text(json.dumps({"schema_version": 1, "name": name, "origin": origin,
                                    "fragment": "**Example controls**\n- Keep repository-specific rules here."}))
        return path

    def cli(self, *args, root=None, payload=None, success=True, env=None):
        result = subprocess.run([sys.executable, str(SCRIPT), "--cwd", str(root or self.root),
                                 *args], input=json.dumps(payload) if payload else None,
                                capture_output=True, text=True, env={**os.environ, **(env or {})})
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        return result

    def snapshot(self, root):
        result = {}
        for path in [root, *sorted(root.rglob("*"))]:
            stat = path.lstat()
            key = str(path.relative_to(root)) if path != root else "."
            if path.is_symlink():
                value = ("symlink", os.readlink(path))
            elif path.is_dir():
                value = ("directory",)
            else:
                value = ("file", path.read_bytes())
            result[key] = (value, stat.st_mode & 0o777, stat.st_mtime_ns)
        return result

    def event(self, session="one", root=None, source="compact", **extra):
        payload = {"hook_event_name": "SessionStart", "source": source,
                   "session_id": session, "cwd": str(root or self.root), **extra}
        return self.cli("hook", "--harness", "codex", payload=payload)

    def test_reliability_destination_stays_inside_test_owned_state_home(self):
        result = reliability.emit(self.root, "delivery.held", harness="codex",
                                  session="fixture-session")
        self.assertTrue(result["ok"], result)
        state_dir, _ = reliability._state_path(self.root)
        self.assertTrue(state_dir.resolve().is_relative_to(self.state_home.resolve()),
                        f"reliability destination {state_dir} escapes test state {self.state_home}")
        self.assertTrue(any(state_dir.glob("*.jsonl")))

    def assign(self, session, role, **kw):
        return self.cli("assign", "--harness", "codex", "--session", session,
                        "--role", role, **kw)

    def content(self, result):
        return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_fresh_implementer_default_delivers_delegation_rule_without_rewriting_existing_text(self):
        self.cli("init", "--default-role", "implementer")
        preview = self.cli("preview", "--harness", "codex", "--session", "new-worker", "--role", "implementer")
        self.assertIn("Ask your parent orchestrator for more agents", preview.stdout)
        self.assertIn("delegation still follows current Model Preferences role choices", preview.stdout)
        self.assertIn("Every explanation is a hypothesis", preview.stdout)
        confirmation = "In your own chat, confirm receipt of the injection"
        self.assertEqual(preview.stdout.count(confirmation), 1)
        self.assertNotIn("confirm completion in your own chat", preview.stdout)
        self.assertNotIn("Repair rounds: don't spiral", preview.stdout)
        self.assertLess(preview.stdout.index("Verify completed changes through the user's path."),
                        preview.stdout.index(confirmation))
        orchestrator = self.cli("preview", "--harness", "codex", "--session", "new-orchestrator",
                                "--role", "orchestrator")
        self.assertIn("Every explanation is a hypothesis", orchestrator.stdout)
        self.assertIn("Check that observation on the failing run AND a passing control", orchestrator.stdout)
        self.assertIn("published-guidance-attestations", orchestrator.stdout)
        self.assertIn("attestation-start-check", orchestrator.stdout)
        self.assertIn("Repair rounds: don't spiral", orchestrator.stdout)
        self.assertEqual(orchestrator.stdout.count(confirmation), 1)
        self.assertNotIn("confirm completion in your own chat", orchestrator.stdout)
        self.assertLess(orchestrator.stdout.index("Repair rounds: don't spiral"),
                        orchestrator.stdout.index(confirmation))
        source = self.root / ".context-injector/context.md"
        approved = source.read_bytes()
        self.cli("init", "--default-role", "implementer")
        self.assertEqual(source.read_bytes(), approved)
        source.write_text("# shared\nApproved lane text.\n# implementer\nApproved assignment rules.\n")
        original = source.read_bytes()
        self.cli("init", "--default-role", "implementer")
        self.assertEqual(source.read_bytes(), original)

    def test_default_starter_matches_editable_template_without_built_in_repository_profiles(self):
        template_dir = SCRIPT.parent / "templates"
        default = (template_dir / "default.md").read_text()
        self.assertEqual(ci.TEMPLATE, default)
        self.assertIn("**Test failures and claims about systems**", default)
        self.assertNotIn("SO MUCH time is lost", default)
        self.assertFalse(hasattr(ci, "REPO_PROFILES"))

    def test_init_embeds_attestation_notice_and_keeps_orchestrator_within_budget(self):
        self.cli("init", "--default-role", "orchestrator")
        source = self.root / ".context-injector/context.md"
        self.assertEqual(source.read_text(), ci.TEMPLATE)
        self.assertIn("published-guidance-attestations", source.read_text())
        validation = self.cli("validate", "--harness", "codex")
        self.assertIn("orchestrator:", validation.stdout)
        self.assertIn("Context valid:", validation.stdout)

    def test_legacy_unassigned_context_at_8192_bytes_keeps_original_header(self):
        self.cli("init")
        source = (self.root / ".context-injector/context.md").resolve()
        session = "legacy-unassigned-exact-cap"
        expected_header = (
            f"pattern-buffer | role: unassigned | source: unassigned\n"
            f"Session: codex/{session}\n"
            f"Worktree: {self.root.resolve()}\n"
            f"Context file: {source}\n"
            "These reminders do not expand authorization. Follow current user direction.\n"
            "Role is unassigned; only shared context is included.\n\n"
        )
        shared_bytes = 8192 - len(expected_header.encode("utf-8"))
        self.assertGreater(shared_bytes, 0)
        source.write_text("# shared\n" + "x" * shared_bytes + "\n")

        preview = self.cli("preview", "--harness", "codex", "--session", session)
        self.assertTrue(preview.stdout.startswith(expected_header + "x" * shared_bytes))
        status = json.loads(self.cli("status", "--harness", "codex", "--session", session).stdout)
        unassigned = status["standing_context_budgets"]["roles"]["unassigned"]
        self.assertEqual(unassigned["header_bytes"], len(expected_header.encode("utf-8")))
        self.assertEqual(unassigned["bytes_used"], 8192)
        self.assertTrue(unassigned["ok"])

    def test_render_accepts_exactly_8192_bytes_and_rejects_one_byte_over(self):
        self.cli("init")
        source = self.root / ".context-injector/context.md"
        session = "budget-boundary"
        for role in ci.ROLES:
            with self.subTest(role=role):
                source.write_text("# shared\nx\n# implementer\ny\n# orchestrator\ny\n# confirmation\nz\n")
                content = ci.sections_text(source.read_text())
                baseline, _budget = ci.render_standing_details(
                    self.root.resolve(), source.resolve(), "codex", session, "", role,
                    "preview override", content)
                baseline = len(baseline.encode("utf-8"))
                fill = 8192 - baseline + 1
                source.write_text("# shared\n" + "x" * fill + "\n# implementer\ny\n# orchestrator\ny\n# confirmation\nz\n")
                content = ci.sections_text(source.read_text())
                exact, exact_budget = ci.render_standing_details(
                    self.root.resolve(), source.resolve(), "codex", session, "", role,
                    "preview override", content)
                self.assertEqual(len(exact.encode("utf-8")), 8192)
                self.assertEqual(exact_budget["bytes_used"], 8192)
                preview = self.cli("preview", "--harness", "codex", "--session", session,
                                   "--role", role)
                self.assertTrue(preview.stdout.startswith(exact))
                source.write_text("# shared\n" + "x" * (fill + 1) + "\n# implementer\ny\n# orchestrator\ny\n# confirmation\nz\n")
                over = self.cli("preview", "--harness", "codex", "--session", session,
                                "--role", role, success=False)
                self.assertIn("8193 / 8192 bytes", over.stderr)
        self.assertEqual((ci.LIMIT, ci.FILE_LIMIT, ci.TASK_LIMIT, ci.INJECTION_LIMIT),
                         (8192, 32768, 16384, 24576))

    def test_guide_print_is_plain_text_read_only_without_git_or_home_state(self):
        outside = self.root.parent / "outside-git"
        outside.mkdir()
        home = self.root.parent / "read-only-home"
        home.mkdir()
        home.chmod(0o500)
        self.addCleanup(home.chmod, 0o700)
        home_before = self.snapshot(home)
        outside_before = self.snapshot(outside)
        expected = GUIDE.read_text()
        env = {"HOME": str(home), "XDG_DATA_HOME": str(home / "data"),
               "CODEX_HOME": str(home / "codex")}
        self.assertFalse((outside / ".git").exists())
        self.assertFalse((outside / ".context-injector").exists())
        for arguments in (("guide",), ("guide", "--print")):
            with self.subTest(arguments=arguments):
                result = self.cli(*arguments, root=outside, env=env)
                self.assertEqual(result.stdout, expected)
                self.assertEqual(result.stderr, "")
                self.assertEqual(self.snapshot(outside), outside_before)
                self.assertEqual(self.snapshot(home), home_before)

    def test_external_repo_profile_accepts_origin_forms_and_composes_only_new_context(self):
        urls = ("git@github.com:example/team.git", "ssh://git@github.com/example/team.git",
                "https://github.com/example/team.git")
        for index, url in enumerate(urls):
            with self.subTest(url=url):
                root = self.root if index == 0 else self.root.parent / f"profile-repo-{index}"
                if root != self.root:
                    self.create_repo(root)
                self.git_at(root, "remote", "add", "origin", url)
                profile_path = self.repo_profile(origin="https://github.com/example/team.git")
                result = self.cli("init", "--repo-profile-file", str(profile_path),
                                  "--default-role", "orchestrator", root=root)
                source = root / ".context-injector/context.md"
                self.assertEqual(source.read_text(), ci.compose_template(
                    "**Example controls**\n- Keep repository-specific rules here."))
                self.assertIn("repository profile: Example controls", result.stdout)
                saved = json.loads(self.registration(root).read_text())["repo_profile"]
                self.assertEqual(saved["name"], "Example controls")
                self.assertEqual(saved["origin"], "https://github.com/example/team.git")
                status = json.loads(self.cli("status", root=root).stdout)
                self.assertEqual(status["repo_profile"], "Example controls")
                self.assertIn("Keep repository-specific rules here", self.cli(
                    "preview", "--harness", "codex", "--session", f"profile-{index}",
                    "--role", "implementer", root=root).stdout)

    def test_external_repo_profile_refuses_wrong_or_ambiguous_origin_before_writes(self):
        self.assertFalse((self.root / ".context-injector").exists())
        profile_path = self.repo_profile()
        before = self.snapshot(self.root.parent)
        missing = self.cli("init", "--repo-profile-file", str(profile_path), success=False)
        self.assertIn("origin must match exactly", missing.stderr)
        self.assertEqual(self.snapshot(self.root.parent), before)

        for url in ("https://github.com/example/another.git", "http://github.com/example/team.git",
                    "ssh://git@example.com/example/team.git"):
            with self.subTest(url=url):
                if self.git_at(self.root, "remote").stdout:
                    self.git_at(self.root, "remote", "remove", "origin")
                self.git_at(self.root, "remote", "add", "origin", url)
                before = self.snapshot(self.root.parent)
                result = self.cli("init", "--repo-profile-file", str(profile_path), success=False)
                self.assertIn("origin must match exactly", result.stderr)
                self.assertEqual(self.snapshot(self.root.parent), before)
                self.assertFalse((self.root / ".context-injector").exists())
                self.assertFalse(self.registration().exists())
        self.git_at(self.root, "remote", "set-url", "origin", "https://github.com/example/team.git")
        self.git_at(self.root, "remote", "set-url", "--add", "origin", "https://github.com/example/team.git")
        before = self.snapshot(self.root.parent)
        result = self.cli("init", "--repo-profile-file", str(profile_path), success=False)
        self.assertIn("origin must match exactly", result.stderr)
        self.assertEqual(self.snapshot(self.root.parent), before)

    def test_external_repo_profile_records_but_never_migrates_existing_context(self):
        self.cli("init", "--default-role", "orchestrator")
        source = self.root / ".context-injector/context.md"
        source.write_text("# shared\nOwner-approved text.\n# implementer\nKeep this assignment rule.\n# orchestrator\nKeep this owner rule.\n")
        original = source.read_bytes()
        original_registration = self.registration().read_bytes()
        sibling = self.root.parent / "unprofiled-sibling"
        self.git("worktree", "add", "-qb", "unprofiled-sibling", str(sibling))
        self.cli("init", root=sibling)
        sibling_source = sibling / ".context-injector/context.md"
        sibling_context = sibling_source.read_bytes()
        sibling_registration = self.registration(sibling).read_bytes()
        self.git("remote", "add", "origin", "https://github.com/example/team.git")
        profile_path = self.repo_profile()
        self.git("remote", "set-url", "origin", "https://github.com/example/other.git")
        self.cli("init", "--repo-profile-file", str(profile_path), success=False)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self.registration().read_bytes(), original_registration)
        self.assertEqual(sibling_source.read_bytes(), sibling_context)
        self.assertEqual(self.registration(sibling).read_bytes(), sibling_registration)
        self.git("remote", "set-url", "origin", "https://github.com/example/team.git")
        result = self.cli("init", "--repo-profile-file", str(profile_path))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(sibling_source.read_bytes(), sibling_context)
        self.assertEqual(self.registration(sibling).read_bytes(), sibling_registration)
        self.assertIn("existing context preserved", result.stdout)
        self.assertIn("controls were not applied", result.stdout)
        config = json.loads(self.registration().read_text())
        self.assertEqual(config["repo_profile"]["name"], "Example controls")
        self.assertEqual(config["default_role"], "orchestrator")
        self.assertNotIn("Keep repository-specific rules here", source.read_text())
        self.cli("init")
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(json.loads(self.registration().read_text())["repo_profile"]["name"], "Example controls")

    def test_external_repo_profile_is_idempotent_and_inherited_by_dispatch_children(self):
        self.git("remote", "add", "origin", "git@github.com:example/team.git")
        profile_path = self.repo_profile()
        self.cli("init", "--repo-profile-file", str(profile_path), "--default-role", "orchestrator")
        source = self.root / ".context-injector/context.md"
        generated = source.read_bytes()
        self.cli("init", "--repo-profile-file", str(profile_path))
        self.assertEqual(source.read_bytes(), generated)
        self.cli("init")
        self.assertEqual(source.read_bytes(), generated)
        self.assertEqual(json.loads(self.registration().read_text())["repo_profile"]["name"], "Example controls")

        worker, task_file = self.implementer("Profile inheritance task")
        ticket = self.prepare(worker, task_file)
        delivered = self.content(self.dispatch_event(worker, ticket))
        self.assertIn("Keep repository-specific rules here", delivered)
        context_file = next(line.removeprefix("Context file: ") for line in delivered.splitlines()
                            if line.startswith("Context file: "))
        self.assertEqual(Path(context_file).resolve(), source.resolve())
        self.assertFalse((worker / ".context-injector").exists())
        status = json.loads(self.cli("status", root=worker).stdout)
        self.assertEqual(Path(status["context_file"]).resolve(), source.resolve())
        self.assertEqual(status["repo_profile"], "Example controls")
        self.cli("init", "--repo-profile-file", str(profile_path), root=worker, success=False)

    def test_external_repo_profile_digest_change_requires_explicit_review(self):
        self.git("remote", "add", "origin", "https://github.com/example/team.git")
        profile_path = self.repo_profile()
        self.cli("init", "--repo-profile-file", str(profile_path))
        source = self.root / ".context-injector/context.md"
        original = source.read_bytes()
        profile_path.write_text(json.dumps({"schema_version": 1, "name": "Example controls",
                                            "origin": "https://github.com/example/team.git",
                                            "fragment": "Changed controls."}))
        context_before = source.read_bytes()
        registration_before = self.registration().read_bytes()
        result = self.cli("init", "--repo-profile-file", str(profile_path), success=False)
        self.assertIn("a different repository profile is already registered", result.stderr)
        self.assertEqual(source.read_bytes(), context_before)
        self.assertEqual(self.registration().read_bytes(), registration_before)

    def test_no_opt_in_is_silent(self):
        result = self.event()
        self.assertEqual((result.stdout, result.stderr), ("", ""))

    def test_persisted_role_and_unassigned_session(self):
        self.cli("init")
        self.assign("one", "orchestrator")
        orchestrator = self.content(self.event())
        self.assertIn("Track ownership", orchestrator)
        confirmation = "In your own chat, confirm receipt of the injection"
        self.assertEqual(orchestrator.count(confirmation), 1)
        self.assertNotIn("confirm completion in your own chat", orchestrator)
        other = self.content(self.event(session="two"))
        self.assertIn("role: unassigned", other)
        self.assertNotIn("Track ownership", other)
        self.assertNotIn("Repair rounds: don't spiral", other)
        self.assertEqual(other.count(confirmation), 1)
        self.assertNotIn("confirm completion in your own chat", other)

    def test_opencode_public_assignment_and_checkpoint_enrollment_accept_exact_native_session(self):
        session = "ses_cliBoundary20261105"
        env = {"OPENCODE_SESSION_ID": ""}
        self.cli("init", "--default-role", "orchestrator", env=env)
        self.cli("enable", env=env)
        source = (self.root / ".context-injector/context.md").resolve()
        assigned = self.cli("assign", "--harness", "opencode", "--session", session,
                            "--role", "orchestrator", "--context-file", str(source), env=env)
        binding = json.loads(assigned.stdout)
        self.assertEqual((binding["harness"], binding["session_id"], binding["role"]),
                         ("opencode", session, "orchestrator"))

        delivered = json.loads(self.cli("context", "--harness", "opencode", "--session", session,
                                        env=env).stdout)
        self.assertEqual(delivered["status"], "context")
        self.assertIn("Track ownership", delivered["text"])

        briefing = json.loads(self.cli("checkpoint-enroll", "--harness", "opencode",
                                       "--session", session, env=env).stdout)
        self.assertEqual(briefing["state"], "briefing")
        self.assertIsNone(briefing["compact_at"])
        self.assertIn("unqualified exact occupancy", briefing["threshold_source"])

        malformed = self.cli("assign", "--harness", "opencode", "--session", "SES_wrong",
                             "--role", "orchestrator", "--context-file", str(source),
                             success=False, env=env)
        self.assertIn("exact case-sensitive ses_ session ID", malformed.stderr)

        gitdir = Path(self.git("rev-parse", "--absolute-git-dir").stdout.strip())
        task_assignment = ci.state_path(gitdir, "opencode", session)
        task_assignment.parent.mkdir(parents=True, exist_ok=True)
        task_assignment.write_text(json.dumps({"root": str(self.root.resolve()),
                                               "role": "implementer", "task_id": "invalid-task-id"}))
        collision = self.cli("context", "--harness", "opencode", "--session", session,
                             success=False, env=env)
        self.assertIn("invalid task ID", collision.stderr)
        replacement = self.cli("assign", "--harness", "opencode", "--session", session,
                               "--role", "orchestrator", "--context-file", str(source),
                               success=False, env=env)
        self.assertIn("a dispatched task cannot be replaced by a session enrollment", replacement.stderr)

    def test_default_and_child_isolation(self):
        self.cli("init", "--default-role", "orchestrator")
        self.assertIn("Track ownership", self.content(self.event()))
        self.assertNotIn("Track ownership", self.content(self.event(agent_id="child")))
        self.cli("assign", "--harness", "codex", "--session", "one", "--agent", "child",
                 "--role", "implementer")
        self.assertIn("Reproduce", self.content(self.event(agent_id="child")))

    def test_worktree_and_subdirectory_isolation(self):
        sibling = self.root.parent / "sibling"
        self.git("worktree", "add", "-qb", "sibling", str(sibling))
        self.cli("init", "--default-role", "orchestrator")
        self.assertEqual(self.event(root=sibling).stdout, "")
        self.cli("init", "--default-role", "implementer", root=sibling)
        nested = sibling / "nested"
        nested.mkdir()
        self.assertIn("Reproduce", self.content(self.event(root=nested)))
        self.assertIn("Track ownership", self.content(self.event()))

    def test_each_compaction_rereads_file(self):
        self.cli("init")
        first = self.event().stdout
        self.assertEqual(first, self.event().stdout)
        source = self.root / ".context-injector/context.md"
        source.write_text(source.read_text().replace(
            "Stay within the user's authorized scope.",
            "NEW REMINDER: Stay within the user's authorized scope."))
        self.assertIn("NEW REMINDER", self.content(self.event()))

    def test_resume_and_fork(self):
        self.cli("init")
        self.assign("one", "orchestrator")
        self.assertIn("Track ownership", self.content(self.event(source="resume")))
        self.assertNotIn("Track ownership", self.content(self.event(session="fork-id", source="fork")))

    def test_malformed_or_oversized_content_emits_no_context(self):
        self.cli("init")
        source = self.root / ".context-injector/context.md"
        for text in ("# shared\na\n# shared\nb", "# shared\n" + "x" * 9000):
            source.write_text(text)
            result = self.event()
            self.assertEqual(result.stdout, "")
            self.assertIn("pattern-buffer:", result.stderr)
            self.cli("validate", success=False)

    def test_validation_reports_both_budgets_and_errors_for_unused_role(self):
        self.cli("init", "--default-role", "implementer")
        source = self.root / ".context-injector/context.md"
        text = "# shared\nHello\n# implementer\nSmall\n# orchestrator\n" + "é" * 3800
        source.write_text(text)
        result = self.cli("validate", "--harness", "claude", "--session", "real-session")
        self.assertIn("implementer:", result.stdout)
        self.assertIn("orchestrator:", result.stdout)
        self.assertIn("remaining", result.stdout)
        self.assertIn("WARNING: orchestrator", result.stderr)
        # A longer actual identity can exceed the cap even when the sample fits.
        result = self.cli("validate", "--session", "s" * 512, success=False)
        self.assertRegex(result.stderr,
                         r"orchestrator: Header \d+ bytes; content 7607 / \d+ usable bytes; -\d+ bytes remaining\.")
        source.write_text(text + "é" * 500)
        result = self.cli("validate", success=False)
        self.assertRegex(result.stderr,
                         r"orchestrator: Header \d+ bytes; content 8607 / \d+ usable bytes; -\d+ bytes remaining\.")
        # The active implementer still fits; lint must catch the other role too.
        self.assertIn("implementer:", result.stdout)

    def test_enrollment_validation_distinguishes_missing_role_section_from_oversize(self):
        self.cli("init")
        source = (self.root.parent / "reviewed-context.md").resolve()
        source.write_text("# shared\nApproved mission.\n")
        missing = self.cli("session-enroll", "--harness", "codex", "--session", "validation-missing",
                           "--context-file", str(source), "--role", "orchestrator", success=False)
        self.assertIn("standing context validation failed", missing.stderr)
        self.assertIn("missing section for assigned role: orchestrator", missing.stderr)
        self.assertNotIn("exceeds its exact rendered UTF-8 byte budget", missing.stderr)

        source.write_text("# shared\n" + "x" * 9000 +
                          "\n# implementer\ny\n# orchestrator\nz\n")
        oversized = self.cli("session-enroll", "--harness", "codex", "--session", "validation-oversized",
                             "--context-file", str(source), "--role", "orchestrator", success=False)
        self.assertIn("standing context validation failed", oversized.stderr)
        self.assertIn("content", oversized.stderr)
        self.assertRegex(oversized.stderr, r"-\d+ bytes remaining")

    def test_exact_budget_matches_render_for_both_harnesses_with_long_unicode_paths(self):
        long_root = self.root.parent / ("worktree-" + "x" * 96 + "é" * 55) / "nested" / "checkout"
        self.create_repo(long_root)
        self.cli("init", "--default-role", "orchestrator", root=long_root)
        source = long_root / ".context-injector/context.md"
        source.write_text("# shared\n" + "Shared-é-🛰️\n" * 20 +
                          "# implementer\n" + "Implementation-ø\n" * 12 +
                          "# orchestrator\n" + "Decision-Ω\n" * 13)
        gitdir = Path(self.git_at(long_root, "rev-parse", "--absolute-git-dir").stdout.strip())
        session = "session-" + "é" * 35

        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                for role in ci.ROLES:
                    with self.subTest(role=role):
                        self.cli("init", "--default-role", role, root=long_root)
                        report = ci.current_context_budgets(long_root.resolve(), gitdir, harness, session)
                        config, source = ci.load(long_root.resolve(), gitdir)
                        selected_role, origin, _saved = ci.resolve_role(
                            long_root.resolve(), gitdir, config, harness, session)
                        standing, standing_budget = ci.render_standing_details(
                            long_root.resolve(), source.resolve(), harness, session, "",
                            selected_role, origin, ci.sections_text(source.read_text()))
                        rendered = ci.render(long_root, harness, session)
                        budget = report["roles"][role]
                        self.assertEqual(selected_role, role)
                        self.assertEqual(len(standing.encode("utf-8")), budget["bytes_used"])
                        self.assertEqual(standing_budget["bytes_used"], budget["bytes_used"])
                        self.assertTrue(rendered.startswith(standing))
                        self.assertGreater(len(rendered.encode("utf-8")), budget["bytes_used"])
                        self.assertLessEqual(len(rendered.encode("utf-8")), ci.INJECTION_LIMIT)
                        self.assertEqual(budget["header_bytes"] + budget["content_bytes"], budget["bytes_used"])
                        self.assertEqual(budget["usable_content_bytes"], ci.LIMIT - budget["header_bytes"])
                        self.assertEqual(budget["remaining_bytes"], ci.LIMIT - budget["bytes_used"])
                        self.assertLessEqual(budget["bytes_used"], ci.LIMIT)

                future = ci.auxiliary("sessions")._session_path(long_root.resolve(), harness, session)
                validator = ci.context_validator(long_root.resolve(), harness, session)
                validation = validator(source.read_text(), future, "orchestrator")
                self.assertEqual(validation["budgets"]["source"], str(future))
                future_header = ci.standing_parts(long_root.resolve(), future, harness, session, "",
                                                  "orchestrator", "session enrollment",
                                                  ci.sections_text(source.read_text()))[0]
                future_budget = validation["budgets"]["roles"]["orchestrator"]
                self.assertEqual(future_budget["header_bytes"], len(future_header.encode("utf-8")))
                self.assertNotEqual(future, source)

    def test_unassigned_budget_validation_and_render_agree_for_both_harnesses(self):
        self.cli("init")
        source = self.root / ".context-injector/context.md"
        session = "unassigned-budget-check"

        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                seed = "# shared\nseed\n# implementer\ny\n# orchestrator\nz\n"
                content = ci.sections_text(seed)
                assigned_header = ci.standing_parts(
                    self.root.resolve(), source.resolve(), harness, session, "",
                    "orchestrator", "unassigned", content)[0]
                shared_bytes = ci.LIMIT - len(assigned_header.encode("utf-8")) - len("\n\nz".encode("utf-8"))
                source.write_text("# shared\n" + "x" * shared_bytes +
                                  "\n# implementer\ny\n# orchestrator\nz\n")
                unassigned_header = ci.standing_parts(
                    self.root.resolve(), source.resolve(), harness, session, "",
                    None, "unassigned", ci.sections_text(source.read_text()))[0]
                expected_unassigned_bytes = len(unassigned_header.encode("utf-8")) + shared_bytes

                validation = self.cli("validate", "--harness", harness, "--session", session, success=False)
                self.assertIn("implementer: 8191 / 8192 bytes", validation.stdout)
                self.assertIn("orchestrator: 8192 / 8192 bytes", validation.stdout)
                self.assertIn("unassigned:", validation.stderr)
                self.assertIn("remaining", validation.stderr)
                doctor = self.cli("doctor", "--harness", harness, "--session", session, success=False)
                self.assertIn("unassigned:", doctor.stderr)

                status = json.loads(self.cli("status", "--harness", harness, "--session", session).stdout)
                budgets = status["standing_context_budgets"]
                self.assertFalse(budgets["ok"])
                self.assertEqual(set(budgets["roles"]), {"implementer", "orchestrator", "unassigned"})
                self.assertFalse(budgets["roles"]["unassigned"]["ok"])
                self.assertEqual(budgets["roles"]["unassigned"]["bytes_used"], expected_unassigned_bytes)

                preview = self.cli("preview", "--harness", harness, "--session", session, success=False)
                self.assertEqual(preview.stdout, "")
                self.assertIn(f"unassigned context uses {expected_unassigned_bytes} / 8192 bytes", preview.stderr)

    def test_enrolled_session_budgets_still_match_preview_for_both_harnesses(self):
        self.cli("init")
        source = self.root.parent / "approved-standing-context.md"
        source.write_text("# shared\nApproved mission.\n# implementer\nImplement assigned work.\n"
                          "# orchestrator\nPreserve current authority and holds.\n")
        sessions = {
            "codex": {
                "implementer": "11111111-1111-4111-8111-111111111111",
                "orchestrator": "11111111-1111-4111-8111-111111111112",
            },
            "claude": {
                "implementer": "22222222-2222-4222-8222-222222222221",
                "orchestrator": "22222222-2222-4222-8222-222222222222",
            },
        }

        for harness, role_sessions in sessions.items():
            for role, session in role_sessions.items():
                with self.subTest(harness=harness, role=role):
                    enrolled = json.loads(self.cli(
                        "session-enroll", "--harness", harness, "--session", session,
                        "--context-file", str(source.resolve()), "--role", role).stdout)
                    expected = enrolled["standing_context_budgets"]["roles"][role]["bytes_used"]
                    self.assertNotIn("unassigned", enrolled["standing_context_budgets"]["roles"])

                    preview = self.cli("preview", "--harness", harness, "--session", session)
                    self.assertTrue(preview.stdout.endswith("\n"))
                    preview_text = preview.stdout[:-1]
                    self.assertEqual(len(preview_text.encode("utf-8")),
                                     expected + len(ci.compose_task_context("").encode("utf-8")))
                    self.assertIn("After compaction, read the sections relevant to current work in full and confirm completion in your own chat.",
                                  preview_text)
                    self.assertNotIn("In your own chat, confirm receipt of the injection", preview_text)
                    self.assertLessEqual(len(preview_text.encode("utf-8")), ci.INJECTION_LIMIT)
                    status = json.loads(self.cli("status", "--harness", harness, "--session", session).stdout)
                    budgets = status["standing_context_budgets"]
                    self.assertEqual(budgets["role"], role)
                    self.assertEqual(budgets["source"], enrolled["path"])
                    self.assertEqual(budgets["roles"][role]["bytes_used"], expected)
                    self.assertNotIn("unassigned", budgets["roles"])
                    self.cli("validate", "--harness", harness, "--session", session)

    def test_install_preflights_missing_helper_source_before_creating_settings_or_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            runtime = folder / "runtime"
            settings = folder / "hooks.json"
            missing = folder / "missing-sessions.py"
            args = SimpleNamespace(command="install", harness="claude", settings=str(settings),
                                   runtime_dir=str(runtime), bin_dir=str(runtime), checkpoints=True)
            original = ci.auxiliary_source

            def source(name, script=None):
                if name == "sessions" and script is None:
                    return missing
                return original(name, script)

            with mock.patch.object(ci, "auxiliary_source", side_effect=source):
                with self.assertRaisesRegex(ValueError, "expected a regular, non-symlink file"):
                    ci.configure_hooks(args)
            self.assertFalse(runtime.exists())
            self.assertFalse(settings.exists())
            self.assertFalse(settings.with_name(settings.name + ".context-injector.lock").exists())

    def test_concurrent_sessions_and_harness_namespace(self):
        self.cli("init")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.assign(str(i), ROLES[i % 2]), range(8)))
        for i in range(8):
            self.assertIn(f"role: {ROLES[i % 2]}", self.content(self.event(session=str(i))))
        result = self.cli("preview", "--harness", "claude", "--session", "0")
        self.assertIn("role: unassigned", result.stdout)

    def test_missing_role_section_is_an_error(self):
        self.cli("init")
        (self.root / ".context-injector/context.md").write_text("# shared\nHello")
        self.assign("one", "orchestrator", success=False)

    def test_init_preserves_existing_text(self):
        self.cli("init")
        source = self.root / ".context-injector/context.md"
        source.write_text("# shared\nOperator text")
        self.cli("init")
        self.assertEqual(source.read_text(), "# shared\nOperator text")

    def test_hook_config_is_parseable_and_does_not_install(self):
        for harness in ("claude", "codex"):
            config = json.loads(self.cli("hook-config", "--harness", harness).stdout)
            command = config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            self.assertIn(f"--harness {harness}", command)
        self.assertFalse((self.root / ".codex").exists())

    def test_claude_protocol_and_bad_payload(self):
        self.cli("init")
        source = self.root / ".context-injector/context.md"
        source.write_text(
            "# shared\nCLAUDE_SHARED_MARKER\n"
            "# implementer\nCLAUDE_IMPLEMENTER_ONLY_MARKER\n"
            "# orchestrator\nCLAUDE_ORCHESTRATOR_ONLY_MARKER\n"
            "# confirmation\nCLAUDE_CONFIRMATION_MARKER\n"
        )
        result = self.cli("hook", "--harness", "claude", payload={
            "hook_event_name": "SessionStart", "source": "compact", "session_id": "c",
            "cwd": str(self.root)})
        delivered = self.content(result)
        self.assertIn("CLAUDE_SHARED_MARKER", delivered)
        self.assertNotIn("CLAUDE_IMPLEMENTER_ONLY_MARKER", delivered)
        self.assertNotIn("CLAUDE_ORCHESTRATOR_ONLY_MARKER", delivered)
        self.assertIn("CLAUDE_CONFIRMATION_MARKER", delivered)
        result = self.cli("hook", "--harness", "codex", payload={
            "hook_event_name": "SessionStart", "source": "compact", "cwd": str(self.root)})
        self.assertEqual(result.stdout, "")
        self.assertIn("session ID", result.stderr)

    def test_enable_disable_preserves_text_roles_and_other_worktree(self):
        self.cli("init")
        self.assign("one", "orchestrator")
        source = self.root / ".context-injector/context.md"
        original = source.read_text()
        sibling = self.root.parent / "sibling"
        self.git("worktree", "add", "-qb", "sibling", str(sibling))
        self.cli("init", "--default-role", "implementer", root=sibling)
        self.cli("disable")
        self.assertEqual(self.event().stdout, "")
        self.assertFalse(json.loads(self.cli("status").stdout)["enabled"])
        self.assertIn("Reproduce", self.content(self.event(root=sibling)))
        self.cli("init")
        self.assertEqual(self.event().stdout, "")  # init must not silently undo disable.
        self.cli("enable")
        self.assertIn("Track ownership", self.content(self.event()))
        self.assertEqual(source.read_text(), original)

    def test_disable_works_with_broken_context_and_enable_rejects_it(self):
        self.cli("init")
        source = self.root / ".context-injector/context.md"
        source.write_text("broken")
        self.cli("disable")
        self.assertEqual(self.event().stdout, "")
        self.cli("enable", success=False)
        self.assertFalse(json.loads(self.cli("status").stdout)["enabled"])

    def test_status_unregistered_and_reinit_keeps_default(self):
        self.assertFalse(json.loads(self.cli("status").stdout)["registered"])
        self.cli("enable", success=False)
        self.cli("init", "--default-role", "orchestrator")
        self.cli("init")
        self.assertEqual(json.loads(self.cli("status").stdout)["default_role"], "orchestrator")

    def test_status_explains_delivery_is_not_tracked(self):
        self.cli("init", "--default-role", "orchestrator")
        settings, runtime, options = self.install_args()
        self.cli("install", *options, "--runtime-dir", str(runtime))
        status = json.loads(self.cli("status", *options).stdout)
        self.assertEqual(status["delivery_state"], "not_tracked")
        self.assertFalse(status["delivery_verified"])
        self.assertIn("Model reception is not recorded", status["delivery_note"])
        self.assertIn("does not mean reception failed", status["delivery_note"])

    def test_handoff_check_success_is_read_only_and_reports_local_readiness(self):
        self.cli("init", "--default-role", "orchestrator")
        settings, runtime, options = self.install_args()
        self.cli("install", *options, "--runtime-dir", str(runtime))
        gitdir = Path(self.git("rev-parse", "--absolute-git-dir").stdout.strip())
        task_record = gitdir / "context-injector/tasks" / ("a" * 32) / "task.json"
        task_record.parent.mkdir(parents=True)
        task_record.write_text('{"preserve": true}')
        before = self.snapshot(self.root.parent)

        result = self.cli("handoff-check", "--harness", "codex", "--session", "successor-1",
                          "--settings", str(settings))
        report = json.loads(result.stdout)

        self.assertEqual(report["target_worktree"], str(self.root.resolve()))
        self.assertEqual(report["harness"], "codex")
        self.assertEqual(report["session"], "successor-1")
        self.assertTrue(report["configuration_ready"])
        self.assertEqual(report["resolved_role"], {"role": "orchestrator", "source": "worktree default"})
        self.assertTrue(all(check["ok"] for check in report["checks"].values()))
        self.assertTrue(all(check.get("next_action") for check in report["checks"].values()))
        self.assertEqual(set(report["checks"]["context"]["budgets"]), {"implementer", "orchestrator"})
        for budget in report["checks"]["context"]["budgets"].values():
            self.assertLessEqual(budget["bytes_used"], budget["limit_bytes"])
        self.assertEqual(report["delivery_state"], "not_tracked")
        self.assertEqual(report["checkpoint"]["state"], "not_enrolled")
        self.assertFalse(report["checkpoint"]["required"])
        self.assertEqual(report["native_binding"]["state"], "unknown")
        self.assertEqual(report["delivery_evidence"]["state"], "unknown")
        self.assertEqual(report["installed_hooks"]["state"], "compatible")
        self.assertIn("actual harness cwd must match", " ".join(report["notes"]).lower())
        self.assertIn("manual observation", " ".join(report["notes"]))
        self.assertIn("Hook trust", " ".join(report["notes"]))
        self.assertNotIn("Track ownership", result.stdout)
        self.assertEqual(self.snapshot(self.root.parent), before)

        preview = self.cli("preview", "--harness", "codex", "--session", "successor-1").stdout
        self.assertIn("role: orchestrator | source: worktree default", preview)

    def test_handoff_check_reads_enrollment_without_creating_registry_lock(self):
        self.cli("init", "--default-role", "implementer")
        settings, runtime, options = self.install_args()
        self.cli("install", *options, "--runtime-dir", str(runtime))
        source = self.root / ".context-injector/context.md"
        source.write_text("# shared\nApproved session context.\n# implementer\nImplement this session.\n"
                          "# orchestrator\nPreserve this session's holds.\n")
        self.cli("session-enroll", "--harness", "codex", "--session", "successor-enrolled",
                 "--context-file", str(source.resolve()), "--role", "orchestrator")
        gitdir = Path(self.git("rev-parse", "--absolute-git-dir").stdout.strip())
        registry_lock = gitdir / "context-injector/session-enrollments/registry.lock"
        self.assertTrue(registry_lock.exists())
        registry_lock.unlink()
        before = self.snapshot(self.root.parent)

        result = self.cli("handoff-check", "--harness", "codex", "--session", "successor-enrolled",
                          "--settings", str(settings))
        report = json.loads(result.stdout)

        self.assertTrue(report["configuration_ready"])
        self.assertEqual(report["resolved_role"], {"role": "orchestrator", "source": "session enrollment"})
        self.assertEqual(report["standing_context"]["enrollment_binding"]["state"], "active")
        self.assertEqual(report["standing_context"]["source"],
                         str((self.root / ".context-injector/sessions/codex-successor-enrolled.md").resolve()))
        self.assertFalse(registry_lock.exists())
        self.assertEqual(self.snapshot(self.root.parent), before)

    def test_handoff_check_does_not_fall_back_when_read_only_binding_lookup_fails(self):
        self.cli("init", "--default-role", "orchestrator")
        settings, runtime, options = self.install_args()
        self.cli("install", *options, "--runtime-dir", str(runtime))
        gitdir = Path(self.git("rev-parse", "--absolute-git-dir").stdout.strip())
        registry = gitdir / "context-injector/session-enrollments/registry.json"
        registry.parent.mkdir(parents=True)
        registry.write_text("not a registry")

        result = self.cli("handoff-check", "--harness", "codex", "--session", "successor-corrupt",
                          "--settings", str(settings), success=False)
        report = json.loads(result.stdout)

        self.assertEqual(report["resolved_role"], {"role": None, "source": None})
        self.assertIn("binding could not be read", report["checks"]["resolved_role"]["detail"])
        self.assertFalse(report["checks"]["context"]["ok"])
        self.assertEqual(report["standing_context"]["enrollment_binding"]["state"], "unknown")
        self.assertEqual(report["checkpoint"]["state"], "unknown")
        self.assertFalse(report["checkpoint"]["required"])
        self.assertIn("checkpoint-status", report["checkpoint"]["next_action"])
        self.assertFalse(report["configuration_ready"])

    def test_handoff_check_reports_existing_claude_idle_warning_disabled_without_changing_it(self):
        home = self.root.parent / "isolated-home"
        launcher = home / ".local/bin/hailing-frequencies"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!" + sys.executable + "\n" + '''import json, sys
if sys.argv[2] == "set":
    print(json.dumps({"timer": {"id": "11111111-1111-4111-8111-111111111111", "due_at": 1800003000}}))
else:
    print(json.dumps({"status": "canceled"}))
''')
        launcher.chmod(0o700)
        session = "11111111-1111-4111-8111-111111111111"
        env = {"HOME": str(home), "CMUX_AGENT_LAUNCH_KIND": "claude", "CLAUDE_CODE_SESSION_ID": session}
        self.cli("init", "--default-role", "orchestrator")
        settings = self.root.parent / "claude-settings.json"
        runtime = self.root.parent / "claude-runtime"
        self.cli("install", "--harness", "claude", "--settings", str(settings),
                 "--runtime-dir", str(runtime), env=env)
        self.cli("checkpoint-init", "--harness", "claude", "--session", session,
                 "--compact-at", "60", env=env)
        self.cli("checkpoint-config", "--harness", "claude", "--session", session,
                 "--idle-warning-minutes", "0", env=env)
        gitdir = Path(self.git("rev-parse", "--absolute-git-dir").stdout.strip())
        checkpoint_before = ci.checkpoints()._state_path(gitdir, "claude", session).read_bytes()

        report = json.loads(self.cli("handoff-check", "--harness", "claude", "--session", session,
                                     "--settings", str(settings), env=env).stdout)

        self.assertTrue(report["configuration_ready"])
        self.assertEqual(report["checkpoint"]["state"], "invalid")
        self.assertEqual(report["checkpoint"]["idle_warning"]["state"], "disabled")
        self.assertIn("checkpoint-config", report["checkpoint"]["idle_warning"]["next_action"])
        self.assertIn("--idle-warning-minutes 50", report["checkpoint"]["idle_warning"]["next_action"])
        self.assertEqual(ci.checkpoints()._state_path(gitdir, "claude", session).read_bytes(), checkpoint_before)

    def test_handoff_check_reports_registration_enabled_role_context_and_hook_failures(self):
        settings, runtime, options = self.install_args()
        self.cli("install", *options, "--runtime-dir", str(runtime))
        missing = self.cli("handoff-check", "--harness", "codex", "--session", "successor",
                           "--settings", str(settings), success=False)
        report = json.loads(missing.stdout)
        self.assertFalse(report["configuration_ready"])
        self.assertFalse(report["checks"]["registration"]["ok"])
        self.assertIn("No Pattern Buffer registration", report["checks"]["registration"]["detail"])
        self.assertEqual(report["checkpoint"]["state"], "unknown")
        self.assertFalse(report["checkpoint"]["required"])
        self.assertIn("checkpoint-status", report["checkpoint"]["next_action"])

        self.cli("init", "--default-role", "orchestrator")
        self.cli("disable")
        report = json.loads(self.cli("handoff-check", "--harness", "codex", "--session", "successor",
                                     "--settings", str(settings), success=False).stdout)
        self.assertFalse(report["checks"]["enabled"]["ok"])
        self.assertIn("disabled", report["checks"]["enabled"]["detail"])

        self.cli("enable")
        self.assign("successor", "implementer")
        report = json.loads(self.cli("handoff-check", "--harness", "codex", "--session", "successor",
                                     "--settings", str(settings), success=False).stdout)
        self.assertFalse(report["checks"]["resolved_role"]["ok"])
        self.assertEqual(report["resolved_role"], {"role": "implementer", "source": "session assignment"})
        self.assertIn("expected orchestrator", report["checks"]["resolved_role"]["detail"])
        self.assertEqual(report["checkpoint"]["state"], "not_applicable")

        self.assign("successor", "orchestrator")
        source = self.root / ".context-injector/context.md"
        source.write_text("# shared\nValid shared.\n# implementer\nValid implementer.\n")
        report = json.loads(self.cli("handoff-check", "--harness", "codex", "--session", "successor",
                                     "--settings", str(settings), success=False).stdout)
        self.assertFalse(report["checks"]["context"]["ok"])
        self.assertIn("orchestrator", report["checks"]["context"]["detail"])
        self.assertIn("missing section", report["checks"]["context"]["budgets"]["orchestrator"]["detail"])

    def test_handoff_check_reports_absent_stale_and_malformed_hook_settings(self):
        self.cli("init", "--default-role", "orchestrator")
        settings, runtime, options = self.install_args()
        recovery_only_options = (*options, "--no-checkpoints")
        recovery_home = self.root.parent / "recovery home"
        recovery_env = {"HOME": str(recovery_home), "XDG_DATA_HOME": str(recovery_home / "data"),
                        "CODEX_HOME": str(recovery_home / ".codex")}
        subprocess_env = {**os.environ, **recovery_env}
        settings.write_text(json.dumps({"unrelated": {"setting": True}, "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo untouched"}]}]}}))
        absent = self.cli("handoff-check", "--harness", "codex", "--session", "successor",
                          "--settings", str(settings), success=False)
        report = json.loads(absent.stdout)
        self.assertFalse(report["checks"]["hook_runtime"]["ok"])
        self.assertIn("exactly one installed Pattern Buffer hook", report["checks"]["hook_runtime"]["detail"])

        self.cli("install", *recovery_only_options, "--runtime-dir", str(runtime))
        old_launcher = runtime / "pattern-buffer"
        old_launcher_bytes = old_launcher.read_bytes()
        damaged_entrypoints = [runtime / "context_injector_codex.py", runtime / "pattern_buffer_codex.py",
                               runtime / "pattern_buffer.py"]
        for entrypoint in damaged_entrypoints:
            entrypoint.write_text("stale runtime")
        damaged_bytes = [entrypoint.read_bytes() for entrypoint in damaged_entrypoints]
        old_launcher_before = subprocess.run([str(old_launcher), "guide", "--print"],
                                             capture_output=True, text=True, env=subprocess_env)
        self.assertNotEqual(old_launcher_before.returncode, 0)
        stale = self.cli("handoff-check", "--harness", "codex", "--session", "successor",
                         "--settings", str(settings), success=False, env=recovery_env)
        report = json.loads(stale.stdout)
        self.assertFalse(report["checks"]["hook_runtime"]["ok"])
        self.assertIn("runtime differs", report["checks"]["hook_runtime"]["detail"])
        self.assertIn("Same-settings recovery command:", report["checks"]["hook_runtime"]["next_action"])
        self.assertIn(str(settings), report["checks"]["hook_runtime"]["next_action"])
        next_action = report["checks"]["hook_runtime"]["next_action"]
        recovery_text, recovery_command = next_action.split("Same-settings recovery command: ", 1)
        recovery_args = shlex.split(recovery_command)
        recovery_runtime = Path(recovery_args[recovery_args.index("--runtime-dir") + 1])
        self.assertIn("Any existing PATH launcher remains unchanged", recovery_text)
        self.assertIn(shlex.quote(str(recovery_runtime / "pattern-buffer")), recovery_text)
        self.assertIn("--no-checkpoints", recovery_args)

        settings_before_refusal = settings.read_bytes()
        refused = self.cli("install", *recovery_only_options, "--runtime-dir", str(runtime),
                           success=False, env=recovery_env)
        self.assertIn("refusing to update unowned runtime entrypoint", refused.stderr)
        self.assertIn("Same-settings recovery command:", refused.stderr)
        self.assertIn("--no-checkpoints", refused.stderr)
        self.assertEqual(settings.read_bytes(), settings_before_refusal)
        self.assertEqual([entrypoint.read_bytes() for entrypoint in damaged_entrypoints], damaged_bytes)

        self.assertEqual(old_launcher.read_bytes(), old_launcher_bytes)
        self.assertFalse(recovery_runtime.exists())
        recovery = subprocess.run(recovery_args, capture_output=True, text=True, env=subprocess_env)
        self.assertEqual(recovery.returncode, 0, recovery.stdout + recovery.stderr)
        self.assertNotEqual(settings.read_bytes(), settings_before_refusal)
        self.assertEqual([entrypoint.read_bytes() for entrypoint in damaged_entrypoints], damaged_bytes)
        old_launcher_after = subprocess.run([str(old_launcher), "guide", "--print"],
                                            capture_output=True, text=True, env=subprocess_env)
        self.assertEqual((old_launcher_after.returncode, old_launcher_after.stdout, old_launcher_after.stderr),
                         (old_launcher_before.returncode, old_launcher_before.stdout, old_launcher_before.stderr))
        self.assertEqual(old_launcher.read_bytes(), old_launcher_bytes)
        data = json.loads(settings.read_text())
        self.assertEqual(data["unrelated"], {"setting": True})
        self.assertEqual(data["hooks"]["Stop"], [{"hooks": [{"type": "command", "command": "echo untouched"}]}])
        handler = data["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertIn(str(recovery_runtime / "context_injector_codex.py"), handler["command"])
        new_launcher = recovery_runtime / "pattern-buffer"
        guide = subprocess.run([str(new_launcher), "guide", "--print"], capture_output=True, text=True,
                               env=subprocess_env)
        self.assertEqual(guide.returncode, 0, guide.stdout + guide.stderr)
        self.assertTrue(guide.stdout.startswith("# Pattern Buffer guide\n"))
        status = subprocess.run([str(new_launcher), "--cwd", str(self.root), "status", "--harness", "codex",
                                 "--settings", str(settings)], capture_output=True, text=True, env=subprocess_env)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertTrue(json.loads(status.stdout)["registered"])
        self.assertEqual(json.loads(status.stdout)["owned_hook_handlers"], 1)
        recovered = subprocess.run([str(new_launcher), "--cwd", str(self.root), "handoff-check",
                                    "--harness", "codex", "--session", "successor", "--settings", str(settings)],
                                   capture_output=True, text=True, env=subprocess_env)
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        recovered_report = json.loads(recovered.stdout)
        self.assertTrue(recovered_report["checks"]["hook_runtime"]["ok"])
        self.assertTrue(recovered_report["configuration_ready"])
        command, marker = handler["command"].rsplit(" # ", 1)
        argv = shlex.split(command)
        argv[0] = str(self.root.parent / "missing-interpreter")
        handler["command"] = shlex.join(argv) + " # " + marker
        settings.write_text(json.dumps(data))
        missing_interpreter = self.cli("handoff-check", "--harness", "codex", "--session", "successor",
                                       "--settings", str(settings), success=False)
        report = json.loads(missing_interpreter.stdout)
        self.assertFalse(report["checks"]["hook_runtime"]["ok"])
        self.assertIn("interpreter is not available", report["checks"]["hook_runtime"]["detail"])

        settings.write_text("not json")
        malformed = self.cli("handoff-check", "--harness", "codex", "--session", "successor",
                             "--settings", str(settings), success=False)
        report = json.loads(malformed.stdout)
        self.assertFalse(report["checks"]["hook_runtime"]["ok"])
        self.assertIn("Local hook/runtime preflight failed", report["checks"]["hook_runtime"]["detail"])

    def install_args(self, harness="codex"):
        settings = self.root.parent / f"{harness} settings.json"
        runtime = self.root.parent / "runtime with spaces"
        return settings, runtime, ("--harness", harness, "--settings", str(settings))

    def test_install_preserves_owned_hook_slot_and_mixed_group_fields(self):
        settings, runtime, options = self.install_args()
        self.cli("install", *options, "--runtime-dir", str(runtime))
        data = json.loads(settings.read_text())
        groups = data["hooks"]["SessionStart"]
        self.assertEqual(len(groups), 1)
        groups[0]["customFilter"] = {"source": "fixture"}
        groups[0]["hooks"][0]["timeout"] = 1
        groups[0]["hooks"][0]["additionalContextLimit"] = 16384
        mixed_group_handler = {"type": "command", "command": "echo same-group foreign handler"}
        groups[0]["hooks"].append(mixed_group_handler)
        foreign_after_group = {"matcher": "custom-event", "customGroup": "keep",
                               "hooks": [{"type": "command", "command": "echo later foreign group"}]}
        groups.append(foreign_after_group)
        settings.write_text(json.dumps(data))
        expected_groups = json.loads(settings.read_text())["hooks"]["SessionStart"]
        expected_groups[0]["hooks"][0]["timeout"] = 5
        expected_groups[0]["hooks"][0]["additionalContextLimit"] = 0

        self.cli("install", *options, "--runtime-dir", str(runtime))

        installed_groups = json.loads(settings.read_text())["hooks"]["SessionStart"]
        self.assertEqual(installed_groups, expected_groups)
        self.assertTrue(ci.owned(installed_groups[0]["hooks"][0], "codex"))
        self.assertEqual(installed_groups[0]["hooks"][1], mixed_group_handler)
        self.assertEqual(installed_groups[0]["customFilter"], {"source": "fixture"})
        self.assertEqual(installed_groups[1], foreign_after_group)

    def test_install_refreshes_owned_only_group_fields_at_the_existing_slot(self):
        cases = (
            ("claude", "SessionStart", "startup|resume|compact", "startup|resume|compact|fork|clear"),
            ("codex", "PreCompact", "legacy-checkpoint-filter", None),
        )
        for harness, event, stale_matcher, expected_matcher in cases:
            with self.subTest(harness=harness, event=event):
                settings, _, options = self.install_args(harness)
                old_runtime = self.root.parent / f"{harness} old runtime"
                new_runtime = self.root.parent / f"{harness} new runtime"
                self.cli("install", *options, "--runtime-dir", str(old_runtime))
                data = json.loads(settings.read_text())
                groups = data["hooks"][event]
                owned_index = next(index for index, group in enumerate(groups)
                                   if any(ci.owned(handler, harness) for handler in group["hooks"]))
                owned_group = groups[owned_index]
                self.assertEqual(len(owned_group["hooks"]), 1)
                owned_handler = owned_group["hooks"][0]
                old_command = owned_handler["command"]
                owned_handler["timeout"] = 1
                owned_group["matcher"] = stale_matcher
                foreign_after_group = {"matcher": "foreign-after-owned", "customGroup": event,
                                       "hooks": [{"type": "command", "command": "echo later group"}]}
                groups.insert(owned_index + 1, foreign_after_group)
                settings.write_text(json.dumps(data))

                self.cli("install", *options, "--runtime-dir", str(new_runtime))

                updated_groups = json.loads(settings.read_text())["hooks"][event]
                canonical_group = ci.hook_config(harness, new_runtime / f"context_injector_{harness}.py", True)["hooks"][event][0]
                self.assertEqual(updated_groups[owned_index], canonical_group)
                self.assertEqual(updated_groups[owned_index + 1], foreign_after_group)
                updated_handler = updated_groups[owned_index]["hooks"][0]
                self.assertTrue(ci.owned(updated_handler, harness))
                self.assertNotEqual(updated_handler["command"], old_command)
                self.assertEqual(updated_handler["timeout"], 5)
                if expected_matcher is None:
                    self.assertNotIn("matcher", updated_groups[owned_index])
                    self.assertNotIn("additionalContextLimit", updated_handler)
                else:
                    self.assertEqual(updated_groups[owned_index]["matcher"], expected_matcher)

    def test_install_preserves_existing_settings_and_is_idempotent(self):
        for harness in ("codex", "claude"):
            settings, runtime, options = self.install_args(harness)
            existing = {"unrelated": {"setting": True}, "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}],
                "SessionStart": [{"matcher": "startup", "hooks": [
                    {"type": "command", "command": "echo existing"}]}]}}
            settings.write_text(json.dumps(existing))
            os.chmod(settings, 0o640)
            self.cli("install", *options, "--runtime-dir", str(runtime))
            first = settings.read_bytes()
            installed = json.loads(first)
            self.assertEqual(installed["unrelated"], existing["unrelated"])
            self.assertEqual(installed["hooks"]["Stop"], existing["hooks"]["Stop"])
            self.assertTrue(any(handler["command"] == "echo existing"
                                for group in installed["hooks"]["SessionStart"]
                                for handler in group["hooks"]))
            self.assertEqual(settings.stat().st_mode & 0o777, 0o640)
            self.assertEqual(len(list(settings.parent.glob(settings.name + ".context-injector-backup-*"))), 1)
            self.cli("install", *options, "--runtime-dir", str(runtime))
            self.assertEqual(settings.read_bytes(), first)
            status = json.loads(self.cli("status", *options).stdout)
            self.assertEqual(status["owned_hook_handlers"], 1)
            command = next(handler["command"] for group in json.loads(first)["hooks"]["SessionStart"]
                           for handler in group["hooks"]
                           if handler["command"].endswith(f"starfleet-context-injector:{harness}:v1"))
            self.assertIn(f"context_injector_{harness}.py", command)
            self.assertTrue(command.endswith(f"starfleet-context-injector:{harness}:v1"))
            self.assertTrue((runtime / "pattern_buffer.py").is_file())
            self.assertTrue((runtime / f"pattern_buffer_{harness}.py").is_file())
            self.assertTrue((runtime / "pattern-buffer").is_file())
            self.cli("uninstall", *options)
            self.assertEqual(json.loads(settings.read_text()), existing)
            self.assertTrue((runtime / "pattern-buffer").is_file())
            before = settings.read_bytes()
            self.cli("uninstall", *options)
            self.assertEqual(settings.read_bytes(), before)

    def test_installer_refuses_to_replace_an_unowned_launcher(self):
        settings, runtime, options = self.install_args()
        runtime.mkdir()
        launcher = runtime / "pattern-buffer"
        launcher.write_text("user-owned command\n")
        before = launcher.read_bytes()
        self.cli("install", *options, "--runtime-dir", str(runtime), success=False)
        self.assertEqual(launcher.read_bytes(), before)
        self.assertFalse(settings.exists())

    def test_installed_command_runs_without_source_checkout(self):
        self.cli("init", "--default-role", "orchestrator")
        settings, runtime, options = self.install_args()
        _claude_settings, _claude_runtime, claude_options = self.install_args("claude")
        package = self.root.parent / "temporary-source"
        package.mkdir()
        source = package / "pattern_buffer.py"
        source.write_bytes(SCRIPT.read_bytes())
        guide = package / "guide.md"
        guide.write_bytes(GUIDE.read_bytes())
        launcher_source = package / "pattern-buffer"
        launcher_source.write_bytes(SCRIPT.with_name("pattern-buffer").read_bytes())
        launcher_template = package / "pattern-buffer.template"
        launcher_template.write_bytes(SCRIPT.with_name("pattern-buffer.template").read_bytes())
        companion = package / "pattern_buffer_checkpoint.py"
        companion.write_bytes(SCRIPT.with_name("pattern_buffer_checkpoint.py").read_bytes())
        enrollment_support = package / "pattern_buffer_enrollment.py"
        enrollment_support.write_bytes(SCRIPT.with_name("pattern_buffer_enrollment.py").read_bytes())
        session_support = package / "pattern_buffer_sessions.py"
        session_support.write_bytes(SCRIPT.with_name("pattern_buffer_sessions.py").read_bytes())
        reliability_support = package / "pattern_buffer_reliability.py"
        reliability_support.write_bytes(SCRIPT.with_name("pattern_buffer_reliability.py").read_bytes())
        result = subprocess.run([sys.executable, str(source), "install", *options,
                                 "--runtime-dir", str(runtime)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, str(source), "install", *claude_options,
                                 "--runtime-dir", str(runtime)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source.unlink()
        guide.unlink()
        launcher_source.unlink()
        launcher_template.unlink()
        companion.unlink()
        enrollment_support.unlink()
        session_support.unlink()
        reliability_support.unlink()
        package.rmdir()
        outside = self.root.parent / "outside-git"
        outside.mkdir()
        home = self.root.parent / "read-only-home"
        home.mkdir()
        home.chmod(0o500)
        self.addCleanup(home.chmod, 0o700)
        outside_before = self.snapshot(outside)
        home_before = self.snapshot(home)
        guide_text = GUIDE.read_text()
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                for installed in (runtime / f"context_injector_{harness}.py",
                                  runtime / "pattern_buffer.py",
                                  runtime / f"pattern_buffer_{harness}.py",
                                  runtime / "pattern-buffer", runtime / "pattern-buffer.template"):
                    result = subprocess.run([sys.executable, str(installed), "--cwd", str(outside),
                                             "guide", "--print"], capture_output=True, text=True,
                                            env={**os.environ, "HOME": str(home),
                                                 "XDG_DATA_HOME": str(home / "data")})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, guide_text)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(self.snapshot(outside), outside_before)
                    self.assertEqual(self.snapshot(home), home_before)
        settings_before_reinstall = settings.read_bytes()
        for installed in (runtime / "context_injector_codex.py", runtime / "pattern_buffer.py",
                          runtime / "pattern_buffer_codex.py", runtime / "pattern-buffer"):
            result = subprocess.run([sys.executable, str(installed), "install", *options,
                                     "--runtime-dir", str(runtime)], capture_output=True, text=True,
                                    env={**os.environ, "HOME": str(home),
                                         "XDG_DATA_HOME": str(home / "data")})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(settings.read_bytes(), settings_before_reinstall)
        enrollment = subprocess.run([
            sys.executable, str(runtime / "context_injector_codex.py"), "--cwd", str(self.root),
            "checkpoint-init", "--harness", "codex", "--session", "real-id", "--compact-at", "95"],
            capture_output=True, text=True)
        self.assertEqual(enrollment.returncode, 0, enrollment.stderr)
        command = json.loads(settings.read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        payload = {"hook_event_name": "SessionStart", "source": "compact", "session_id": "real-id",
                   "cwd": str(self.root)}
        result = subprocess.run(command, shell=True, input=json.dumps(payload),
                                capture_output=True, text=True, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Track ownership", self.content(result))
        self.assertIn("Checkpoint:", self.content(result))

    def test_uninstall_preserves_other_handlers_in_owned_group(self):
        settings, runtime, options = self.install_args()
        self.cli("install", *options, "--runtime-dir", str(runtime))
        data = json.loads(settings.read_text())
        other = {"type": "command", "command": "echo untouched"}
        data["hooks"]["SessionStart"][0]["hooks"].append(other)
        settings.write_text(json.dumps(data))
        self.cli("uninstall", *options)
        self.assertEqual(json.loads(settings.read_text())["hooks"]["SessionStart"][0]["hooks"], [other])

    def test_installer_refuses_malformed_settings_without_changing_them(self):
        settings, runtime, options = self.install_args()
        for original in ("not json", "[]", '{"hooks":[]}', '{"hooks":{"SessionStart":{}}}'):
            settings.write_text(original)
            self.cli("install", *options, "--runtime-dir", str(runtime), success=False)
            self.assertEqual(settings.read_text(), original)
            self.assertFalse(runtime.exists())

    def test_parallel_installs_keep_one_owned_entry(self):
        settings, runtime, options = self.install_args()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.cli("install", *options, "--runtime-dir", str(runtime)), range(4)))
        self.assertEqual(json.loads(self.cli("status", *options).stdout)["owned_hook_handlers"], 1)

    def test_parallel_cross_harness_installs_share_runtime_lock(self):
        codex_settings, runtime, codex_options = self.install_args("codex")
        claude_settings, claude_runtime, claude_options = self.install_args("claude")
        self.assertEqual(runtime, claude_runtime)
        installs = [("install", *codex_options, "--runtime-dir", str(runtime)),
                    ("install", *claude_options, "--runtime-dir", str(runtime))]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda args: self.cli(*args), installs))

        codex_status = json.loads(self.cli("status", *codex_options).stdout)
        claude_status = json.loads(self.cli("status", *claude_options).stdout)
        self.assertEqual(codex_status["owned_hook_handlers"], 1)
        self.assertEqual(claude_status["owned_hook_handlers"], 1)
        cores = [(runtime / "pattern_buffer.py").read_bytes()]
        for harness in ("codex", "claude"):
            for script in (runtime / f"context_injector_{harness}.py",
                           runtime / f"pattern_buffer_{harness}.py"):
                self.assertTrue(script.is_file())
                self.assertIn("def configure_hooks(args):", script.read_text())
                cores.append(script.read_bytes())
        self.assertTrue(all(core == cores[0] for core in cores[1:]))
        for helper in ("checkpoint", "enrollment", "sessions", "reliability"):
            copies = [runtime / f"{prefix}_{harness}_{helper}.py"
                      for harness in ("codex", "claude")
                      for prefix in ("context_injector", "pattern_buffer")]
            self.assertTrue(all(path.is_file() for path in copies))
            contents = [path.read_bytes() for path in copies]
            self.assertTrue(all(content == contents[0] for content in contents[1:]))

    def implementer(self, brief="Implement the assigned change. Verify the result."):
        self.cli("init", "--default-role", "orchestrator")
        worker = self.root.parent / "worker"
        self.git("worktree", "add", "-qb", "worker", str(worker))
        task_file = self.root.parent / "brief.md"
        task_file.write_text(brief)
        return worker, task_file

    def prepare(self, worker, task_file):
        return json.loads(self.cli("dispatch", "--harness", "codex", "--owner-worktree", str(self.root),
                                   "--task-file", str(task_file), "--prepare-only", root=worker).stdout)

    def dispatch_event(self, worker, ticket, session="child", source="startup", token=True):
        return self.cli("hook", "--harness", "codex", root=worker, payload={
            "hook_event_name": "SessionStart", "source": source, "session_id": session,
            "cwd": str(worker)}, env=ticket["environment"] if token else {})

    def test_dispatch_enrolls_owner_and_replays_exact_task_without_copying_context(self):
        brief = "Work item: fix dates.\nKeep `literal` syntax and Unicode: café.\n"
        worker, task_file = self.implementer(brief)
        original = (self.root / ".context-injector/context.md").read_bytes()
        ticket = self.prepare(worker, task_file)
        text = self.content(self.dispatch_event(worker, ticket))
        self.assertIn("role: implementer", text)
        self.assertIn("Stay within the user's authorized scope", text)
        self.assertIn("Reproduce", text)
        self.assertNotIn("Track ownership", text)
        self.assertTrue(text.endswith(brief))
        self.assertEqual(text.count(brief), 1)
        self.assertFalse((worker / ".context-injector").exists())
        self.assertEqual((self.root / ".context-injector/context.md").read_bytes(), original)
        self.assertIn("Track ownership", self.content(self.event()))

    def test_dispatch_resume_and_compaction_use_binding_without_environment(self):
        worker, task_file = self.implementer("EXACT_SAVED_TASK")
        ticket = self.prepare(worker, task_file)
        self.dispatch_event(worker, ticket)
        task_file.write_text("Unapproved change to original brief file")
        for source in ("resume", "compact", "compact"):
            text = self.content(self.dispatch_event(worker, ticket, source=source, token=False))
            self.assertIn("EXACT_SAVED_TASK", text)
            self.assertNotIn("Unapproved change", text)
        source = self.root / ".context-injector/context.md"
        source.write_text(source.read_text().replace(
            "Stay within the user's authorized scope.",
            "FRESH_OPERATOR_TEXT Stay within the user's authorized scope."))
        text = self.content(self.dispatch_event(worker, ticket, source="compact", token=False))
        self.assertIn("FRESH_OPERATOR_TEXT", text)

    def test_concurrent_dispatches_in_one_worktree_have_distinct_task_bindings(self):
        worker, task_file = self.implementer("TASK_A")
        another = self.root.parent / "other-brief.md"
        another.write_text("TASK_B")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda p: self.prepare(worker, p), (task_file, another)))
        self.assertNotEqual(first["task_id"], second["task_id"])
        a = self.content(self.dispatch_event(worker, first, session="A"))
        b = self.content(self.dispatch_event(worker, second, session="B"))
        self.assertIn("TASK_A", a)
        self.assertNotIn("TASK_B", a)
        self.assertIn("TASK_B", b)
        self.assertNotIn("TASK_A", b)

    def test_inherited_dispatch_token_cannot_claim_another_session(self):
        worker, task_file = self.implementer("PARENT_TASK")
        ticket = self.prepare(worker, task_file)
        self.dispatch_event(worker, ticket)
        result = self.dispatch_event(worker, ticket, session="unintended-nested-session")
        self.assertFalse(json.loads(result.stdout)["continue"])
        self.assertIn("already belongs", result.stderr)
        self.assertNotIn("additionalContext", result.stdout)

    def test_update_and_finish_task_are_explicit_and_finished_tasks_do_not_revive(self):
        worker, task_file = self.implementer("OLD_TASK")
        ticket = self.prepare(worker, task_file)
        self.dispatch_event(worker, ticket)
        task_file.write_text("NEW_TASK")
        update = json.loads(self.cli("update-task", "--task-id", ticket["task_id"],
                                     "--task-file", str(task_file), root=worker).stdout)
        self.assertEqual(update["delivery_state"], "pending_recovery")
        self.assertIn("does not interrupt", update["delivery_note"])
        self.assertIn("confirm model reception", update["delivery_note"])
        self.assertTrue(update["urgent_change_guide"].endswith("#urgent-scope-changes"))
        # Saving an update neither calls a hook nor changes the bound session.
        self.assertEqual(update["session_id"], "child")
        self.assertEqual(update["status"], "active")
        path = Path(subprocess.check_output(["git", "-C", str(worker), "rev-parse", "--absolute-git-dir"], text=True).strip())
        manifest = json.loads((path / "context-injector/tasks" / ticket["task_id"] / "task.json").read_text())
        self.assertNotIn("delivery_state", manifest)
        text = self.content(self.dispatch_event(worker, ticket, source="compact", token=False))
        self.assertIn("NEW_TASK", text)
        self.assertNotIn("OLD_TASK", text)
        self.cli("finish-task", "--task-id", ticket["task_id"], root=worker)
        text = self.content(self.dispatch_event(worker, ticket, source="resume", token=False))
        self.assertNotIn("NEW_TASK", text)
        self.assertIn("await a new assignment", text)
        self.cli("update-task", "--task-id", ticket["task_id"], "--task-file", str(task_file), root=worker, success=False)
        result = self.dispatch_event(worker, ticket, session="new-session")
        self.assertFalse(json.loads(result.stdout)["continue"])

    def test_owner_disable_stops_child_injection_and_child_controls_preserve_owner(self):
        worker, task_file = self.implementer()
        ticket = self.prepare(worker, task_file)
        self.dispatch_event(worker, ticket)
        self.cli("disable")
        self.assertEqual(self.dispatch_event(worker, ticket, source="compact", token=False).stdout, "")
        self.cli("enable")
        self.cli("disable", root=worker)
        self.assertTrue(json.loads(self.cli("status").stdout)["enabled"])
        self.cli("enable", root=worker)
        self.assertIn("Active task", self.content(self.dispatch_event(worker, ticket, source="compact", token=False)))
        self.cli("init", root=worker, success=False)
        self.cli("assign", "--harness", "codex", "--session", "child", "--role", "orchestrator", root=worker, success=False)

    def test_dispatch_rejects_other_repository_owner_and_oversized_task(self):
        worker, task_file = self.implementer()
        unrelated = self.root.parent / "unrelated"
        unrelated.mkdir()
        subprocess.run(["git", "init", "-q", str(unrelated)], check=True)
        self.cli("dispatch", "--harness", "codex", "--owner-worktree", str(self.root),
                 "--task-file", str(task_file), "--prepare-only", root=unrelated, success=False)
        task_file.write_text("x" * 17000)
        self.cli("dispatch", "--harness", "codex", "--owner-worktree", str(self.root),
                 "--task-file", str(task_file), "--prepare-only", root=worker, success=False)
        self.assertFalse((worker / ".context-injector").exists())

    def test_dispatch_preflights_uuid_session_assignment_budget_before_launch(self):
        worker, task_file = self.implementer()
        settings, runtime, options = self.install_args()
        self.cli("install", *options, "--runtime-dir", str(runtime))
        source = self.root / ".context-injector/context.md"
        source.write_text("# shared\nx\n# implementer\ny\n# orchestrator\nz\n")
        session = "11111111-1111-4111-8111-111111111111"
        content = ci.sections_text(source.read_text())
        preview_header = ci.standing_parts(
            worker.resolve(), source.resolve(), "codex", "dispatch-validation", "",
            "implementer", "preview override", content)[0]
        shared_bytes = ci.LIMIT - len(preview_header.encode("utf-8")) - len("\n\ny".encode("utf-8"))
        source.write_text("# shared\n" + "x" * shared_bytes +
                          "\n# implementer\ny\n# orchestrator\nz\n")
        content = ci.sections_text(source.read_text())
        old_text, old_budget = ci.render_standing_details(
            worker.resolve(), source.resolve(), "codex", "dispatch-validation", "",
            "implementer", "preview override", content)
        self.assertEqual(len(old_text.encode("utf-8")), ci.LIMIT)
        self.assertEqual(old_budget["bytes_used"], ci.LIMIT)
        _actual_text, actual_budget = ci.render_standing_details(
            worker.resolve(), source.resolve(), "codex", session, "",
            "implementer", "session assignment", content)
        self.assertEqual(actual_budget["bytes_used"] - ci.LIMIT, 19)

        started = self.root.parent / "codex-started"
        harness = self.root.parent / "codex"
        harness.write_text("#!" + sys.executable + "\nfrom pathlib import Path\n" +
                           f"Path({str(started)!r}).write_text('started')\n")
        harness.chmod(0o700)
        rejected = self.cli("dispatch", "--harness", "codex", "--owner-worktree", str(self.root),
                            "--task-file", str(task_file), "--settings", str(settings), "--",
                            str(harness), "exec", "-C", str(worker), "-", root=worker, success=False)
        self.assertIn(f"{actual_budget['bytes_used']} / 8192 bytes", rejected.stderr)
        self.assertFalse(started.exists())
        self.assertTrue(self.registration(worker).exists())

    def test_dispatch_preflights_composed_task_limit_before_launch_with_shorter_control(self):
        worker, task_file = self.implementer()
        settings, runtime, options = self.install_args()
        self.cli("install", *options, "--runtime-dir", str(runtime))
        source = self.root / ".context-injector/context.md"
        source.write_text("# shared\nx\n# implementer\ny\n# orchestrator\nz\n")
        session = "88888888-8888-4888-8888-888888888888"
        content = ci.sections_text(source.read_text())
        header = ci.standing_parts(
            worker.resolve(), source.resolve(), "codex", session, "",
            "implementer", "session assignment", content)[0]
        shared_bytes = 8111 - len(header.encode("utf-8")) - len("\n\ny".encode("utf-8"))
        source.write_text("# shared\n" + "x" * shared_bytes +
                          "\n# implementer\ny\n# orchestrator\nz\n")
        content = ci.sections_text(source.read_text())
        standing, standing_budget = ci.render_standing_details(
            worker.resolve(), source.resolve(), "codex", session, "",
            "implementer", "session assignment", content)
        self.assertEqual(len(standing.encode("utf-8")), 8111)
        self.assertEqual(standing_budget["bytes_used"], 8111)

        started = self.root.parent / "codex-combined-started"
        harness = self.root.parent / "codex"
        harness.write_text("#!" + sys.executable + "\n" + f'''import json, os, subprocess, sys
from pathlib import Path
Path({str(started)!r}).write_text("started")
task = sys.stdin.read()
settings = json.loads(Path({str(settings)!r}).read_text())
command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
payload = {{"hook_event_name": "SessionStart", "source": "startup",
           "session_id": {session!r}, "cwd": os.getcwd()}}
result = subprocess.run(command, shell=True, input=json.dumps(payload), capture_output=True, text=True)
print(json.dumps({{"task": task, "hook_returncode": result.returncode, "hook_stdout": result.stdout}}))
''')
        harness.chmod(0o700)

        task_file.write_text("x" * ci.TASK_LIMIT)
        rejected = self.cli("dispatch", "--harness", "codex", "--owner-worktree", str(self.root),
                            "--task-file", str(task_file), "--settings", str(settings), "--",
                            str(harness), "exec", "-C", str(worker), "-", root=worker, success=False)
        self.assertIn("combined reminder and task exceed the 24 KiB injection limit", rejected.stderr)
        self.assertFalse(started.exists())

        recovery_bytes = len(ci.compose_task_context("").encode("utf-8"))
        shorter_brief = "y" * (15884 - recovery_bytes)
        self.assertGreater(len(shorter_brief), 0)
        task_file.write_text(shorter_brief)
        accepted = self.cli("dispatch", "--harness", "codex", "--owner-worktree", str(self.root),
                            "--task-file", str(task_file), "--settings", str(settings), "--",
                            str(harness), "exec", "-C", str(worker), "-", root=worker)
        self.assertTrue(started.exists())
        result = json.loads(accepted.stdout)
        self.assertEqual(result["task"], shorter_brief)
        self.assertEqual(result["hook_returncode"], 0)
        hook = json.loads(result["hook_stdout"])
        self.assertIn("additionalContext", hook["hookSpecificOutput"])
        self.assertTrue(hook["hookSpecificOutput"]["additionalContext"].endswith(shorter_brief))

    def test_dispatch_exec_preserves_path_shim_arguments_and_exact_stdin(self):
        worker, task_file = self.implementer("The exact original task.\n")
        settings, runtime, options = self.install_args()
        self.cli("install", *options, "--runtime-dir", str(runtime))
        receipt = self.root.parent / "receipts" / "launch.json"
        binary = self.root.parent / "codex"
        binary.write_text(f'''#!{sys.executable}
import json, os, subprocess, sys
receipt=json.loads(open({str(receipt)!r}).read())
assert receipt['status']=='pending' and receipt['session_id'] is None
settings=json.loads(open({str(settings)!r}).read())
command=settings['hooks']['SessionStart'][0]['hooks'][0]['command']
result=subprocess.run(command,shell=True,input=json.dumps({{'hook_event_name':'SessionStart','source':'startup','session_id':'exec-child','cwd':os.getcwd()}}),capture_output=True,text=True)
print(json.dumps({{'stdin':sys.stdin.read(),'argv':sys.argv,'context':json.loads(result.stdout)}}))
''')
        binary.chmod(0o700)
        result = self.cli("dispatch", "--harness", "codex", "--owner-worktree", str(self.root),
                          "--task-file", str(task_file), "--settings", str(settings),
                          "--receipt-file", str(receipt), "--",
                          str(binary), "exec", "-C", str(worker), "-", root=worker)
        data = json.loads(result.stdout)
        self.assertEqual(data["stdin"], "The exact original task.\n")
        self.assertIn("--no-daemon", data["argv"])
        self.assertIn("The exact original task.", data["context"]["hookSpecificOutput"]["additionalContext"])
        saved = json.loads(receipt.read_text())
        self.assertEqual(saved["receipt_type"], "immutable_dispatch_snapshot")
        self.assertIn("not live task state", saved["note"])
        self.assertIn(saved["task_status_command"], result.stderr)
        self.assertIn("immutable dispatch snapshot", result.stderr)
        self.assertEqual(saved["status"], "pending")
        self.assertIsNone(saved["session_id"])
        self.assertEqual(saved["worktree"], str(worker.resolve()))
        current = json.loads(self.cli("task-status", "--task-id", saved["task_id"], root=worker).stdout)
        self.assertEqual(current["session_id"], "exec-child")
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["brief_sha256"], saved["brief_sha256"])
        original = receipt.read_bytes()
        self.cli("finish-task", "--task-id", saved["task_id"], root=worker)
        lookup = subprocess.run(shlex.split(saved["task_status_command"]), capture_output=True, text=True)
        self.assertEqual(lookup.returncode, 0, lookup.stderr)
        self.assertEqual(json.loads(lookup.stdout)["status"], "finished")
        self.assertEqual(receipt.read_bytes(), original)

    def test_receipt_is_private_and_never_overwrites_existing_paths(self):
        worker, task_file = self.implementer()
        receipt = self.root.parent / "receipt.json"
        args = ("dispatch", "--harness", "codex", "--owner-worktree", str(self.root),
                "--task-file", str(task_file), "--receipt-file", str(receipt), "--prepare-only")
        ticket = json.loads(self.cli(*args, root=worker).stdout)
        self.assertEqual(json.loads(receipt.read_text())["task_id"], ticket["task_id"])
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        original = receipt.read_bytes()
        self.cli(*args, root=worker, success=False)
        self.assertEqual(receipt.read_bytes(), original)
        receipt.unlink()
        receipt.symlink_to(self.root.parent / "missing")
        self.cli(*args, root=worker, success=False)
        self.assertFalse((self.root.parent / "missing").exists())
        tasks = json.loads(self.cli("tasks", root=self.root).stdout)["tasks"]
        self.assertEqual(len(tasks), 1)

    def test_missing_task_points_to_child_without_creating_owner_task_metadata(self):
        worker, task_file = self.implementer()
        ticket = self.prepare(worker, task_file)
        task_id = ticket["task_id"]
        before = self.snapshot(self.root.parent)
        for command in ("task-status", "finish-task", "update-task"):
            args = (command, "--task-id", task_id)
            if command == "update-task":
                args += ("--task-file", str(task_file))
            result = self.cli(*args, success=False)
            self.assertIn("not found in this worktree", result.stderr)
            self.assertIn("tasks from the registered owner", result.stderr)
            self.assertIn(f"--cwd CHILD_WORKTREE before {command}", result.stderr)
            self.assertIn("keeping its other arguments", result.stderr)
        self.assertEqual(self.snapshot(self.root.parent), before)
        listed = json.loads(self.cli("tasks").stdout)["tasks"]
        self.assertEqual(listed[0]["root"], str(worker.resolve()))
        self.cli("finish-task", "--task-id", task_id, root=worker)
        self.assertEqual(json.loads(self.cli("task-status", "--task-id", task_id, root=worker).stdout)["status"], "finished")

    def test_corrupt_or_symlink_task_does_not_claim_wrong_worktree(self):
        worker, task_file = self.implementer()
        ticket = self.prepare(worker, task_file)
        gitdir = Path(subprocess.check_output(["git", "-C", str(worker), "rev-parse", "--absolute-git-dir"], text=True).strip())
        path = gitdir / "context-injector/tasks" / ticket["task_id"] / "task.json"
        path.write_text("not json")
        result = self.cli("task-status", "--task-id", ticket["task_id"], root=worker, success=False)
        self.assertNotIn("not found in this worktree", result.stderr)
        path.unlink()
        path.symlink_to(path.with_name("absent.json"))
        result = self.cli("finish-task", "--task-id", ticket["task_id"], root=worker, success=False)
        self.assertIn("expected a regular, non-symlink file", result.stderr)
        self.assertNotIn("CHILD_WORKTREE", result.stderr)

    def test_owner_task_listing_filters_lanes_and_tracks_lifecycle(self):
        worker, task_file = self.implementer("PRIVATE_BRIEF")
        ticket = self.prepare(worker, task_file)
        self.dispatch_event(worker, ticket, session="listed-child")
        second = self.prepare(self.root, task_file)
        self.cli("finish-task", "--task-id", ticket["task_id"], root=worker)
        other = self.root.parent / "another owner with spaces"
        self.git("worktree", "add", "-qb", "other", str(other))
        self.cli("init", root=other)
        self.cli("dispatch", "--harness", "codex", "--owner-worktree", str(other),
                 "--task-file", str(task_file), "--prepare-only", root=other)
        result = self.cli("tasks", "--owner-worktree", str(self.root), root=worker)
        listing = json.loads(result.stdout)
        self.assertEqual(listing["errors"], [])
        self.assertEqual({task["task_id"] for task in listing["tasks"]},
                         {ticket["task_id"], second["task_id"]})
        self.assertNotIn("PRIVATE_BRIEF", result.stdout)
        finished = json.loads(self.cli("tasks", "--status", "finished").stdout)["tasks"]
        self.assertEqual([task["session_id"] for task in finished], ["listed-child"])
        self.assertEqual(json.loads(self.cli("tasks", "--status", "active").stdout)["tasks"], [])

    def test_task_listing_reports_corruption_without_claiming_complete_success(self):
        worker, task_file = self.implementer()
        ticket = self.prepare(worker, task_file)
        status = json.loads(self.cli("task-status", "--task-id", ticket["task_id"], root=worker).stdout)
        path = Path(status["gitdir"]) / "context-injector/tasks" / ticket["task_id"] / "task.json"
        path.write_text("broken json")
        result = self.cli("tasks", success=False)
        data = json.loads(result.stdout)
        self.assertEqual(data["tasks"], [])
        self.assertEqual(data["errors"][0]["task_id"], ticket["task_id"])

    def test_task_listing_preserves_available_rows_when_owned_worktree_is_missing(self):
        worker, task_file = self.implementer()
        missing = self.prepare(worker, task_file)
        available = self.prepare(self.root, task_file)
        worker.rename(worker.with_name("moved-without-git"))
        result = json.loads(self.cli("tasks", success=False).stdout)
        self.assertEqual([task["task_id"] for task in result["tasks"]], [available["task_id"]])
        self.assertEqual(result["errors"][0]["task_id"], missing["task_id"])

    def test_dispatch_cli_rejects_stale_installed_runtime_before_launch(self):
        worker, task_file = self.implementer()
        settings, runtime, options = self.install_args()
        self.cli("install", *options, "--runtime-dir", str(runtime))
        for name in ("context_injector_codex.py", "pattern_buffer_codex.py", "pattern_buffer.py"):
            (runtime / name).write_text("stale runtime")
        harness = self.root.parent / "codex"
        started = self.root.parent / "harness-started"
        harness.write_text("#!/bin/sh\nprintf launched > " + shlex.quote(str(started)) + "\nexit 99\n")
        harness.chmod(0o755)
        result = self.cli("dispatch", "--harness", "codex", "--owner-worktree", str(self.root),
                          "--task-file", str(task_file), "--settings", str(settings), "--",
                          str(harness), "exec", "-C", str(worker), "-", root=worker, success=False)
        self.assertIn("runtime differs", result.stderr)
        self.assertIn("Same-settings recovery command:", result.stderr)
        recovery = shlex.split(result.stderr.split("Same-settings recovery command: ", 1)[1])
        self.assertIn("--settings", recovery)
        self.assertEqual(recovery[recovery.index("--settings") + 1], str(settings))
        recovery_runtime = Path(recovery[recovery.index("--runtime-dir") + 1])
        recovery_guidance_text = result.stderr.split("Same-settings recovery command: ", 1)[0]
        self.assertIn("Any existing PATH launcher remains unchanged", recovery_guidance_text)
        self.assertIn(shlex.quote(str(recovery_runtime / "pattern-buffer")), recovery_guidance_text)
        self.assertNotEqual(recovery_runtime, runtime)
        self.assertFalse(recovery_runtime.exists())
        self.assertFalse(started.exists())
        self.assertFalse(json.loads(self.cli("status", root=worker).stdout)["registered"])

    def test_two_dispatches_cannot_race_to_replace_one_session_binding(self):
        worker, task_file = self.implementer("One task")
        first, second = self.prepare(worker, task_file), self.prepare(worker, task_file)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda t: self.dispatch_event(worker, t, session="same-session"), (first, second)))
        self.assertEqual(sum("additionalContext" in r.stdout for r in results), 1)
        self.assertEqual(sum('"continue": false' in r.stdout for r in results), 1)

    def test_dispatch_rejects_wrong_worktree_arguments_before_enrollment(self):
        worker, task_file = self.implementer()
        for flags, expected in ((("-C", str(self.root)), "does not match --cwd"),
                                (("-C" + str(self.root),), "separate -C DIR")):
            result = self.cli("dispatch", "--harness", "codex", "--owner-worktree", str(self.root),
                "--task-file", str(task_file), "--", "codex", "exec", *flags, "-", root=worker, success=False)
            # The contract is rejection of the mismatched target before dispatch,
            # with the diagnostic appropriate to each supported argument shape.
            self.assertIn(expected, result.stderr)

    def test_large_task_is_replayed_completely_and_expired_tickets_are_rejected(self):
        brief = "TASK_BOUNDARY " + "x" * 15000 + " END_BOUNDARY"
        worker, task_file = self.implementer(brief)
        ticket = self.prepare(worker, task_file)
        text = self.content(self.dispatch_event(worker, ticket))
        self.assertTrue(text.endswith(brief))
        another = self.prepare(worker, task_file)
        status = json.loads(self.cli("task-status", "--task-id", another["task_id"], root=worker).stdout)
        path = Path(status["gitdir"]) / "context-injector/tasks" / another["task_id"] / "task.json"
        data = json.loads(path.read_text())
        data["created_at"] = 0
        path.write_text(json.dumps(data))
        result = self.dispatch_event(worker, another, session="expired")
        self.assertFalse(json.loads(result.stdout)["continue"])
        self.assertIn("expired", result.stderr)

    def test_dispatched_resume_stops_when_its_task_snapshot_is_missing(self):
        worker, task_file = self.implementer()
        ticket = self.prepare(worker, task_file)
        self.dispatch_event(worker, ticket)
        status = json.loads(self.cli("task-status", "--task-id", ticket["task_id"], root=worker).stdout)
        snapshot = Path(status["gitdir"]) / "context-injector/tasks" / ticket["task_id"] / (status["brief_sha256"] + ".txt")
        snapshot.unlink()
        result = self.dispatch_event(worker, ticket, source="compact", token=False)
        self.assertFalse(json.loads(result.stdout)["continue"])
        self.assertNotIn("additionalContext", result.stdout)


ROLES = ("implementer", "orchestrator")
if __name__ == "__main__":
    unittest.main()
