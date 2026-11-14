import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
SOURCE_NAMES = (
    "replicator.py", "successor_intents.py", "handoff_engine.py", "native_observer.py", "access_observer.py",
    "install.py", "guide.md", "README.md",
)
MODEL_PREFERENCES_SCRIPT = ROOT.parents[0] / "model-preferences" / "model_preferences.py"
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
import replicator
import handoff_engine


class ReplicatorCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="replicator-test-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "copied-source"
        self.source.mkdir()
        for name in SOURCE_NAMES:
            shutil.copyfile(ROOT / name, self.source / name)
        self.home = self.root / "empty-home"
        self.home.mkdir()
        self.bin_dir = self.root / "bin"
        self.runtime_root = self.root / "runtime"
        self.unrelated = self.root / "unrelated-cwd"
        self.unrelated.mkdir()
        self.environment = {
            "HOME": str(self.home),
            "PATH": "",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def run_process(self, command, cwd=None):
        return subprocess.run(
            command,
            cwd=cwd or self.unrelated,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def tree_snapshot(self):
        result = {}
        for path in sorted(self.root.rglob("*")):
            relative = str(path.relative_to(self.root))
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                result[relative] = ("symlink", os.readlink(path), mode)
            elif path.is_dir():
                result[relative] = ("directory", mode)
            else:
                result[relative] = ("file", path.read_bytes(), mode)
        return result

    def install_copy(self):
        result = self.run_process([
            sys.executable,
            str(self.source / "install.py"),
            "--runtime-root",
            str(self.runtime_root),
            "--bin-dir",
            str(self.bin_dir),
        ])
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return json.loads(result.stdout)

    def init_repo(self, path):
        path.mkdir()
        result = subprocess.run(
            [shutil.which("git"), "init", "--quiet", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return path

    def model_preferences_fixture(self, harness="codex"):
        config_dir = self.root / f"model-preferences-config-{harness}"
        state_dir = self.root / f"model-preferences-state-{harness}"
        initialized = subprocess.run(
            [sys.executable, str(MODEL_PREFERENCES_SCRIPT), "--config-dir", str(config_dir),
             "--state-dir", str(state_dir), "init", "--examples"],
            env=self.environment, capture_output=True, text=True, check=False,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        state_dir.mkdir(mode=0o700)
        (state_dir / "orchestrators.json").write_text(json.dumps({"lane": {
            "workspace": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "surface": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "session": f"fixture-{harness}-session", "harness": harness,
        }}), encoding="utf-8")
        bin_dir = self.root / f"model-preferences-bin-{harness}"
        bin_dir.mkdir()
        launcher = bin_dir / "model-preferences"
        launcher.write_text(
            "#!" + sys.executable + "\n"
            "import os, sys\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, {str(MODEL_PREFERENCES_SCRIPT)!r}, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        git_path = shutil.which("git")
        environment = dict(self.environment)
        environment["PATH"] = str(bin_dir) + os.pathsep + str(Path(git_path).parent)
        return config_dir, state_dir, environment

    def run_successor(self, installation, environment, state_dir, *args):
        return subprocess.run(
            [installation["command"], "--state-dir", str(state_dir), *args],
            cwd=self.unrelated, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=30, check=False,
        )

    def successor_scope_args(self, operation, worktree, config_dir, mp_state_dir, seat="lane"):
        return [operation, "--seat", seat, "--worktree", str(worktree),
                "--mp-config-dir", str(config_dir), "--mp-state-dir", str(mp_state_dir)]

    def install_test_environment(self, fixture, repo):
        installation = self.install_copy()
        fixture_path = self.root / "cmux-fixture.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        cmux_path = fake_bin / "cmux"
        cmux_path.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "if sys.argv[1:] != ['current', '--json', '--limit', '200']:\n"
            "    raise SystemExit(91)\n"
            "with open(os.environ['REPLICATOR_CMUX_FIXTURE'], encoding='utf-8') as source:\n"
            "    sys.stdout.write(source.read())\n",
            encoding="utf-8",
        )
        cmux_path.chmod(0o755)
        marker = self.root / "forbidden-launch.txt"
        for name in (
            "claude", "codex", "opencode", "hailing-frequencies", "pattern-buffer",
            "agent-notify", "warp-drive", "temporal-beacon", "discord-watch", "discord",
        ):
            forbidden = fake_bin / name
            forbidden.write_text(
                "#!" + sys.executable + "\n"
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('forbidden launch', encoding='utf-8')\n",
                encoding="utf-8",
            )
            forbidden.chmod(0o755)
        git_path = shutil.which("git")
        guarded_git = fake_bin / "git"
        guarded_git.write_text(
            "#!" + sys.executable + "\n"
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            "tail = args[2:] if len(args) >= 2 and args[0] == '-C' else []\n"
            "allowed = {\n"
            "    ('rev-parse', '--show-toplevel'),\n"
            "    ('rev-parse', '--absolute-git-dir'),\n"
            "    ('rev-parse', '--git-common-dir'),\n"
            "    ('worktree', 'list', '--porcelain'),\n"
            "}\n"
            "if tuple(tail) not in allowed:\n"
            f"    from pathlib import Path; Path({str(marker)!r}).write_text('forbidden git command', encoding='utf-8')\n"
            "    raise SystemExit(92)\n"
            f"os.execv({str(git_path)!r}, [{str(git_path)!r}, *args])\n",
            encoding="utf-8",
        )
        guarded_git.chmod(0o755)
        environment = dict(self.environment)
        environment.update({
            "PATH": str(fake_bin) + os.pathsep + str(Path(git_path).parent),
            "REPLICATOR_CMUX_FIXTURE": str(fixture_path),
            "CODEX_THREAD_ID": "codex-fixture-session",
            "CMUX_SURFACE_ID": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            "CMUX_WORKSPACE_ID": "BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB",
        })
        return installation, environment

    def cmux_fixture(self, cwd):
        return {
            "truncated": False,
            "owner_availability": {"agent_sessions": "available"},
            "items": [{
                "placement": {"kind": "local"},
                "cwd": str(cwd),
                "projections": [{
                    "panel_id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
                    "workspace_id": "BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB",
                }],
                "agents": [{
                    "kind": "codex",
                    "session_id": "codex-fixture-session",
                    "state": "working",
                }],
            }],
        }

    def test_installed_cli_prints_the_packaged_guide_without_writes(self):
        expected_guide = (self.source / "guide.md").read_bytes()
        installation = self.install_copy()
        runtime = Path(installation["runtime"])
        self.assertEqual((runtime / "guide.md").read_bytes(), expected_guide)
        self.assertEqual((runtime / "README.md").read_bytes(), (self.source / "README.md").read_bytes())
        self.assertEqual((stat.S_IMODE((runtime / "guide.md").stat().st_mode) & 0o222), 0)

        second_install = self.install_copy()
        self.assertEqual(second_install["runtime"], installation["runtime"])
        self.assertEqual((runtime / "guide.md").read_bytes(), expected_guide)

        shutil.rmtree(self.source)
        launcher = Path(installation["command"])
        before = self.tree_snapshot()
        help_result = self.run_process([str(launcher), "--help"])
        handoff_help = self.run_process([str(launcher), "handoff", "--help"])
        guide_result = self.run_process([str(launcher), "guide", "--print"])
        after = self.tree_snapshot()

        self.assertEqual(help_result.returncode, 0, help_result.stderr.decode())
        self.assertIn(b"usage: replicator", help_result.stdout)
        self.assertIn(b"guide", help_result.stdout)
        self.assertEqual(handoff_help.returncode, 0, handoff_help.stderr.decode())
        self.assertIn(b"open", handoff_help.stdout)
        self.assertIn(b"observe-successor", handoff_help.stdout)
        self.assertEqual(guide_result.returncode, 0, guide_result.stderr.decode())
        self.assertEqual(guide_result.stdout, expected_guide)
        self.assertEqual(before, after)

    def test_installed_preflight_runs_outside_source_checkout_with_synthetic_cmux(self):
        repo = self.init_repo(self.root / "target-worktree")
        fixture = self.cmux_fixture(repo)
        fixture["items"].append({
            "placement": {"kind": "local"},
            "cwd": str(self.root / "private-peer-worktree"),
            "projections": [{
                "panel_id": "CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC",
                "workspace_id": "DDDDDDDD-DDDD-4DDD-8DDD-DDDDDDDDDDDD",
            }],
            "agents": [{
                "kind": "codex",
                "session_id": "private-peer-session",
                "state": "working",
            }],
        })
        installation, environment = self.install_test_environment(fixture, repo)
        context = self.root / "standing-context.md"
        guidance = self.root / "AGENTS.md"
        context.write_text("private context marker", encoding="utf-8")
        guidance.write_text("private guidance marker", encoding="utf-8")
        before = self.tree_snapshot()

        result = subprocess.run(
            [
                installation["command"], "preflight", "--json",
                "--context-file", str(context),
                "--guidance-file", str(guidance),
                "--skip-tool", "temporal-beacon",
            ],
            cwd=repo,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        report = json.loads(result.stdout)
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["operational_readiness"], "unknown")
        self.assertEqual(checks["caller_identity"]["status"], "ready")
        self.assertEqual(checks["worktree_git_identity"]["status"], "ready")
        self.assertEqual(checks["context_access"]["status"], "ready")
        self.assertEqual(checks["guidance_access"]["status"], "ready")
        self.assertEqual(checks["native_model"]["status"], "unknown")
        self.assertEqual(checks["native_effort"]["status"], "unknown")
        self.assertEqual(checks["native_permissions"]["status"], "unknown")
        self.assertEqual(checks["tool_exclusion:temporal-beacon"]["status"], "skipped")
        self.assertEqual(report["custody_acceptance"], "not_assessed")
        self.assertEqual(report["predecessor_cleanup"], "not_assessed")
        self.assertEqual(report["native_delivery"], "not_assessed")
        self.assertEqual(report["caller"], {
            "harness": "codex",
            "session_id": "codex-fixture-session",
            "panel_id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            "workspace_id": "BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB",
        })
        self.assertNotIn(str(context), result.stdout.decode())
        self.assertNotIn(str(guidance), result.stdout.decode())
        self.assertNotIn("private context marker", result.stdout.decode())
        self.assertNotIn("private guidance marker", result.stdout.decode())
        self.assertNotIn("private-peer-session", result.stdout.decode())
        self.assertNotIn("CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC", result.stdout.decode())
        self.assertNotIn("private-peer-worktree", result.stdout.decode())
        self.assertEqual(before, self.tree_snapshot())

        human_result = subprocess.run(
            [installation["command"], "preflight", "--context-file", str(context)],
            cwd=repo,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(human_result.returncode, 1)
        self.assertIn(b"Operational readiness: unknown", human_result.stdout)
        self.assertIn(b"Custody acceptance: not assessed", human_result.stdout)
        self.assertNotIn(b"private-peer-session", human_result.stdout)
        self.assertEqual(before, self.tree_snapshot())

    def test_cmux_refusal_stays_unknown_and_does_not_leak_command_output(self):
        repo = self.init_repo(self.root / "refusal-worktree")
        installation = self.install_copy()
        fake_bin = self.root / "refusal-bin"
        fake_bin.mkdir()
        cmux_path = fake_bin / "cmux"
        cmux_path.write_text(
            "#!" + sys.executable + "\n"
            "import sys\n"
            "sys.stderr.write('EPERM sandbox refusal')\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        cmux_path.chmod(0o755)
        environment = dict(self.environment)
        environment.update({
            "PATH": str(fake_bin) + os.pathsep + str(Path(shutil.which("git")).parent),
            "CODEX_THREAD_ID": "codex-fixture-session",
            "CMUX_SURFACE_ID": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            "CMUX_WORKSPACE_ID": "BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB",
        })

        result = subprocess.run(
            [installation["command"], "preflight", "--json"],
            cwd=repo,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        report = json.loads(result.stdout)
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(result.returncode, 1)
        self.assertEqual(checks["caller_identity"]["status"], "unknown")
        self.assertEqual(checks["worktree_git_identity"]["status"], "unknown")
        self.assertEqual(report["operational_readiness"], "unknown")
        self.assertIsNone(report["caller"])
        self.assertNotIn("EPERM sandbox refusal", result.stdout.decode())
        self.assertNotIn("EPERM sandbox refusal", result.stderr.decode())

    def test_preflight_marks_missing_context_unknown(self):
        repo = self.init_repo(self.root / "missing-context-worktree")
        installation, environment = self.install_test_environment(self.cmux_fixture(repo), repo)

        result = subprocess.run(
            [installation["command"], "preflight", "--json", "--guidance-file", str(self.source / "guide.md")],
            cwd=repo,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        report = json.loads(result.stdout)
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["context_access"]["status"], "unknown")
        self.assertEqual(checks["guidance_access"]["status"], "ready")
        self.assertEqual(report["operational_readiness"], "unknown")

    def test_preflight_blocks_a_different_native_worktree(self):
        repo = self.init_repo(self.root / "requested-worktree")
        other_repo = self.init_repo(self.root / "native-other-worktree")
        installation, environment = self.install_test_environment(self.cmux_fixture(other_repo), repo)

        result = subprocess.run(
            [installation["command"], "preflight", "--json"],
            cwd=repo,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        report = json.loads(result.stdout)
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["caller_identity"]["status"], "ready")
        self.assertEqual(checks["worktree_git_identity"]["status"], "blocked")
        self.assertEqual(report["operational_readiness"], "blocked")
        self.assertNotIn(str(other_repo), result.stdout.decode())

    def test_ambiguous_native_identity_stays_unknown_without_peer_output(self):
        repo = self.init_repo(self.root / "ambiguous-worktree")
        fixture = self.cmux_fixture(repo)
        duplicate = dict(fixture["items"][0])
        duplicate["projections"] = [{
            "panel_id": "CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC",
            "workspace_id": "DDDDDDDD-DDDD-4DDD-8DDD-DDDDDDDDDDDD",
        }]
        fixture["items"].append(duplicate)
        installation, environment = self.install_test_environment(fixture, repo)

        result = subprocess.run(
            [installation["command"], "preflight", "--json"],
            cwd=repo,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        report = json.loads(result.stdout)
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["caller_identity"]["status"], "unknown")
        self.assertIsNone(report["caller"])
        self.assertEqual(report["operational_readiness"], "unknown")
        self.assertNotIn("codex-fixture-session", result.stdout.decode())
        self.assertNotIn("CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC", result.stdout.decode())

    def test_denied_read_blocks_even_when_stat_succeeds(self):
        context = self.root / "read-denied-context.md"
        context.write_text("private context marker", encoding="utf-8")
        stat_succeeded = []

        def denied_read(path):
            Path(path).stat()
            stat_succeeded.append(True)
            raise PermissionError("private read error")

        with patch.object(replicator, "read_input_bytes", side_effect=denied_read):
            check = replicator.input_check("context_access", [context], "--context-file")

        self.assertEqual(stat_succeeded, [True])
        self.assertEqual(check.status, "blocked")
        self.assertIn("repeat the same read after access recovers.", check.next_step.casefold())

    def test_aggregate_prefers_blocked_then_unknown_and_ignores_explicit_skips(self):
        ready = replicator.Check("worktree", "ready", "fixture", "continue")
        unknown = replicator.Check("model", "unknown", "fixture", "inspect")
        blocked = replicator.Check("context", "blocked", "fixture", "recover")
        skipped = replicator.Check("tool_exclusion:example", "skipped", "fixture", "record")

        self.assertEqual(replicator.aggregate([ready]), "ready")
        self.assertEqual(replicator.aggregate([ready, skipped]), "ready")
        self.assertEqual(replicator.aggregate([ready, unknown, skipped]), "unknown")
        self.assertEqual(replicator.aggregate([ready, unknown, blocked, skipped]), "blocked")

    def test_invalid_arguments_fail_without_filesystem_writes(self):
        installation = self.install_copy()
        before = self.tree_snapshot()

        result = self.run_process([installation["command"], "unknown"])

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr)
        self.assertEqual(before, self.tree_snapshot())

    def test_installer_preserves_an_unowned_launcher(self):
        self.bin_dir.mkdir()
        launcher = self.bin_dir / "replicator"
        launcher.write_bytes(b"#!/bin/sh\necho owned by another tool\n")
        before = self.tree_snapshot()

        result = self.run_process([
            sys.executable,
            str(self.source / "install.py"),
            "--runtime-root",
            str(self.runtime_root),
            "--bin-dir",
            str(self.bin_dir),
        ])

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(launcher.read_bytes(), b"#!/bin/sh\necho owned by another tool\n")
        self.assertFalse(self.runtime_root.exists())
        self.assertEqual(before, self.tree_snapshot())

    def test_successor_explicit_choice_is_snapshotted_idempotently_and_survives_profile_change(self):
        installation = self.install_copy()
        config_dir, mp_state_dir, environment = self.model_preferences_fixture()
        repo = self.init_repo(self.root / "successor-worktree")
        config_path = config_dir / "profiles.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["active"] = "Codex-only"
        config["activation"] = "fixture-activation"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        intent_state = self.root / "replicator-state"
        prefix = self.successor_scope_args("configure", repo, config_dir, mp_state_dir)
        choice_args = ["--family", "codex-luna", "--effort", "xhigh"]

        configured = self.run_successor(installation, environment, intent_state,
                                        "successor", *prefix, *choice_args)
        self.assertEqual(configured.returncode, 0, configured.stderr)
        first = json.loads(configured.stdout)
        self.assertEqual(first["policy"], "once")
        self.assertEqual(first["choice"]["kind"], "model_preferences_candidate")
        self.assertEqual(first["choice"]["snapshot"], {
            "role": "orchestrator", "family": "codex-luna", "harness": "codex",
            "model": "gpt-6-luna", "effort": "xhigh",
        })
        self.assertEqual(first["choice"]["active_profile"], "Codex-only")
        self.assertFalse(first["choice"]["candidate_matches_active_orchestrator"])
        self.assertNotEqual(first["choice"]["candidate_digest"], first["choice"]["active_selection_digest"])
        self.assertEqual(first["native_generation"], {"status": "unknown", "value": None, "evidence": None})
        self.assertEqual(first["observations"]["predecessor_model"]["status"], "unknown")
        self.assertEqual(first["observations"]["predecessor_effort"]["status"], "unknown")
        self.assertEqual(first["observations"]["effective_permissions"]["status"], "unknown")
        self.assertEqual(first["readiness"]["status"], "unknown")
        self.assertEqual(stat.S_IMODE((intent_state / "successor-intents.json").stat().st_mode), 0o600)

        repeated = self.run_successor(installation, environment, intent_state,
                                      "successor", *prefix, *choice_args)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        second = json.loads(repeated.stdout)
        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["reused"])

        human = self.run_successor(installation, environment, intent_state,
                                   "successor", *self.successor_scope_args(
                                       "show", repo, config_dir, mp_state_dir))
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("Configured choice: codex-luna (gpt-6-luna) at xhigh", human.stdout)
        self.assertIn("Active profile: Codex-only", human.stdout)
        self.assertIn(f"Active selection digest: {first['choice']['active_selection_digest']}", human.stdout)
        self.assertIn(f"Candidate digest: {first['choice']['candidate_digest']}", human.stdout)
        self.assertIn("Native generation: unknown", human.stdout)
        self.assertIn("Predecessor model: unknown", human.stdout)
        self.assertIn("Predecessor effort: unknown", human.stdout)
        self.assertIn("Effective permissions: unknown", human.stdout)
        self.assertIn("Readiness: unknown", human.stdout)

        config["active"] = "Claude-Codex"
        config["activation"] = "changed-activation"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        changed_profile = subprocess.run(
            ["model-preferences", "--config-dir", str(config_dir), "--state-dir", str(mp_state_dir),
             "resolve-successor", "--seat", "lane"],
            env=environment, capture_output=True, text=True, check=False,
        )
        self.assertEqual(changed_profile.returncode, 0, changed_profile.stderr)
        current_profile = json.loads(changed_profile.stdout)
        self.assertEqual(current_profile["active_profile"], "Claude-Codex")
        self.assertNotEqual(current_profile["active_selection_digest"],
                            first["choice"]["active_selection_digest"])
        shown = self.run_successor(installation, environment, intent_state,
                                   "successor", *self.successor_scope_args(
                                       "show", repo, config_dir, mp_state_dir), "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        stored = json.loads(shown.stdout)["intent"]
        self.assertEqual(stored["id"], first["id"])
        self.assertEqual(stored["choice"], first["choice"])
        self.assertEqual(stored["choice"]["active_profile"], "Codex-only")
        self.assertEqual(stored["choice"]["active_selection_digest"],
                         first["choice"]["active_selection_digest"])
        self.assertEqual(stored["native_generation"]["status"], "unknown")

        config["active"] = None
        config["activation"] = None
        config_path.write_text(json.dumps(config), encoding="utf-8")
        no_profile_state = self.root / "replicator-state-no-profile"
        no_profile = self.run_successor(
            installation, environment, no_profile_state, "successor",
            *self.successor_scope_args("configure", repo, config_dir, mp_state_dir),
            "--family", "codex-luna", "--effort", "xhigh",
        )
        self.assertEqual(no_profile.returncode, 0, no_profile.stderr)
        no_profile_choice = json.loads(no_profile.stdout)["choice"]
        self.assertIsNone(no_profile_choice["active_profile"])
        self.assertIsNone(no_profile_choice["active_selection_digest"])

    def test_successor_preserve_default_is_unknown_and_scope_mismatch_does_not_bind(self):
        installation = self.install_copy()
        config_dir, mp_state_dir, environment = self.model_preferences_fixture()
        repo = self.init_repo(self.root / "successor-preserve-worktree")
        other_repo = self.init_repo(self.root / "successor-other-worktree")
        intent_state = self.root / "preserve-state"
        configured = self.run_successor(
            installation, environment, intent_state, "successor",
            *self.successor_scope_args("configure", repo, config_dir, mp_state_dir),
        )
        self.assertEqual(configured.returncode, 0, configured.stderr)
        intent = json.loads(configured.stdout)
        self.assertEqual(intent["choice"], {"kind": "preserve_observed_predecessor"})
        self.assertEqual(intent["native_generation"]["status"], "unknown")
        self.assertIsNone(intent["native_generation"]["value"])
        self.assertEqual(intent["observations"]["predecessor_model"]["value"], None)
        self.assertEqual(intent["observations"]["predecessor_effort"]["value"], None)
        self.assertNotEqual(intent["native_generation"]["value"], intent["scope"]["seat"]["session_id"])

        wrong_worktree = self.run_successor(
            installation, environment, intent_state, "successor",
            *self.successor_scope_args("show", other_repo, config_dir, mp_state_dir), "--json",
        )
        self.assertEqual(wrong_worktree.returncode, 0, wrong_worktree.stderr)
        self.assertIsNone(json.loads(wrong_worktree.stdout)["intent"])
        wrong_seat = self.run_successor(
            installation, environment, intent_state, "successor",
            *self.successor_scope_args("show", repo, config_dir, mp_state_dir, seat="unregistered"),
        )
        self.assertEqual(wrong_seat.returncode, 1)
        self.assertIn("could not resolve this successor scope and choice", wrong_seat.stderr)

    def test_successor_rejects_cross_harness_family_and_unsupported_effort_without_mutation(self):
        installation = self.install_copy()
        config_dir, mp_state_dir, environment = self.model_preferences_fixture()
        repo = self.init_repo(self.root / "successor-invalid-worktree")
        intent_state = self.root / "invalid-state"
        base = self.successor_scope_args("configure", repo, config_dir, mp_state_dir)
        for choice in (
            ["--family", "claude-opus", "--effort", "high"],
            ["--family", "not-configured", "--effort", "high"],
            ["--family", "codex-luna", "--effort", "ultra"],
        ):
            with self.subTest(choice=choice):
                result = self.run_successor(installation, environment, intent_state,
                                            "successor", *base, *choice)
                self.assertEqual(result.returncode, 1)
                self.assertIn("could not resolve this successor scope and choice", result.stderr)
        self.assertFalse(intent_state.exists())

    def test_successor_cancel_is_idempotent_and_allows_a_new_one_shot_intent(self):
        installation = self.install_copy()
        config_dir, mp_state_dir, environment = self.model_preferences_fixture()
        repo = self.init_repo(self.root / "successor-cancel-worktree")
        intent_state = self.root / "cancel-state"
        scope = self.successor_scope_args("configure", repo, config_dir, mp_state_dir)
        original = self.run_successor(installation, environment, intent_state,
                                      "successor", *scope, "--family", "codex-luna", "--effort", "high")
        self.assertEqual(original.returncode, 0, original.stderr)
        first = json.loads(original.stdout)
        conflict = self.run_successor(installation, environment, intent_state,
                                      "successor", *scope, "--family", "codex-astra", "--effort", "high")
        self.assertEqual(conflict.returncode, 1)
        self.assertIn("cancel it first", conflict.stderr)
        cancelled = self.run_successor(installation, environment, intent_state,
                                       "successor", "cancel", first["id"])
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertEqual(json.loads(cancelled.stdout)["state"], "cancelled")
        repeated_cancel = self.run_successor(installation, environment, intent_state,
                                            "successor", "cancel", first["id"])
        self.assertEqual(repeated_cancel.returncode, 0, repeated_cancel.stderr)
        self.assertEqual(json.loads(repeated_cancel.stdout)["state"], "cancelled")
        replacement = self.run_successor(installation, environment, intent_state,
                                         "successor", *scope, "--family", "codex-astra", "--effort", "high")
        self.assertEqual(replacement.returncode, 0, replacement.stderr)
        self.assertNotEqual(json.loads(replacement.stdout)["id"], first["id"])

    def test_handoff_freeze_is_versioned_atomic_idempotent_and_read_only_on_reconcile(self):
        installation = self.install_copy()
        config_dir, mp_state_dir, environment = self.model_preferences_fixture()
        repo = self.init_repo(self.root / "handoff-worktree")
        config_path = config_dir / "profiles.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["active"] = "Codex-only"
        config["activation"] = "handoff-fixture"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        intent_state = self.root / "handoff-state"
        configured = self.run_successor(
            installation, environment, intent_state, "successor",
            *self.successor_scope_args("configure", repo, config_dir, mp_state_dir),
            "--family", "codex-luna", "--effort", "xhigh",
        )
        self.assertEqual(configured.returncode, 0, configured.stderr)
        intent = json.loads(configured.stdout)
        saved_choice = intent["choice"]

        config["active"] = "Claude-Codex"
        config["activation"] = "changed-after-snapshot"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        legacy_store = json.loads((intent_state / "successor-intents.json").read_text(encoding="utf-8"))
        legacy_store["schema_version"] = 1
        legacy_store.pop("handoffs")
        (intent_state / "successor-intents.json").write_text(json.dumps(legacy_store), encoding="utf-8")
        (intent_state / "successor-intents.json").chmod(0o600)

        trap_dir = self.root / "handoff-traps"
        trap_dir.mkdir()
        side_effect_marker = self.root / "native-side-effect.txt"
        for name in ("cmux", "claude", "codex", "opencode", "pattern-buffer", "warp-drive",
                     "hailing-frequencies", "temporal-beacon", "discord-watch"):
            trap = trap_dir / name
            trap.write_text(
                "#!" + sys.executable + "\n"
                "from pathlib import Path\n"
                f"Path({str(side_effect_marker)!r}).write_text('unexpected native action', encoding='utf-8')\n",
                encoding="utf-8",
            )
            trap.chmod(0o755)
        guarded_environment = dict(environment)
        guarded_environment["PATH"] = str(trap_dir) + os.pathsep + environment["PATH"]
        freeze_args = ("handoff", "freeze", intent["id"], "--worktree", str(repo),
                       "--mp-config-dir", str(config_dir), "--mp-state-dir", str(mp_state_dir), "--json")
        frozen_result = self.run_successor(
            installation, guarded_environment, intent_state, *freeze_args,
        )
        self.assertEqual(frozen_result.returncode, 0, frozen_result.stderr)
        frozen_response = json.loads(frozen_result.stdout)
        self.assertFalse(frozen_response["reused"])
        frozen = frozen_response["handoff"]
        self.assertEqual(frozen["schema_version"], 1)
        self.assertEqual(frozen["protocol"], "starfleet.replicator.handoff/v1")
        self.assertEqual(frozen["state"], "frozen")
        self.assertEqual(frozen["choice"], saved_choice)
        self.assertEqual(frozen["intent_snapshot"]["id"], intent["id"])
        self.assertEqual(frozen["handoff_generation"], frozen["handoff_generation"].lower())
        self.assertEqual(len(frozen["handoff_generation"]), 32)
        self.assertNotEqual(frozen["handoff_generation"], frozen["id"])
        self.assertNotEqual(frozen["handoff_generation"], intent["scope"]["seat"]["session_id"])
        self.assertEqual(frozen["native_generation"], {"status": "unknown", "value": None, "evidence": None})
        self.assertEqual(frozen["native_predecessor_binding"], {
            "status": "unknown", "hook_generation": None, "evidence": None,
        })
        self.assertEqual(frozen["native_successor_binding"], {
            "status": "unknown", "hook_generation": None, "evidence": None,
        })
        self.assertEqual(frozen["readiness"], {
            "status": "blocked", "reason": "native_observations_unavailable",
        })
        self.assertEqual(frozen["launch"], {"status": "not_started"})
        self.assertEqual(frozen["transfer"], {"status": "not_applicable"})
        self.assertEqual(frozen["predecessor_cleanup"], {"status": "not_applicable"})
        self.assertEqual(frozen["observations"]["predecessor_model"]["status"], "unknown")
        self.assertEqual(frozen["observations"]["predecessor_effort"]["status"], "unknown")
        self.assertEqual(frozen["observations"]["successor_model"]["status"], "unknown")
        self.assertEqual(frozen["observations"]["successor_effort"]["status"], "unknown")
        self.assertEqual(frozen["observations"]["effective_permissions"]["status"], "unknown")
        self.assertFalse(side_effect_marker.exists())

        saved_store = json.loads((intent_state / "successor-intents.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_store["schema_version"], 2)
        self.assertEqual(set(saved_store["handoffs"]), {frozen["id"]})
        saved_intent = saved_store["scopes"][next(iter(saved_store["scopes"]))][0]
        self.assertEqual(saved_intent["id"], intent["id"])
        self.assertEqual(saved_intent["handoff_id"], frozen["id"])
        self.assertEqual(saved_intent["choice"], saved_choice)
        self.assertEqual(saved_intent["state"], "configured")

        git_parent = str(Path(shutil.which("git")).parent)
        no_mp_environment = dict(guarded_environment)
        no_mp_environment["PATH"] = str(trap_dir) + os.pathsep + git_parent
        reused = self.run_successor(installation, no_mp_environment, intent_state, *freeze_args)
        self.assertEqual(reused.returncode, 0, reused.stderr)
        reused_response = json.loads(reused.stdout)
        self.assertTrue(reused_response["reused"])
        self.assertEqual(reused_response["handoff"]["id"], frozen["id"])
        self.assertEqual(reused_response["handoff"]["handoff_generation"], frozen["handoff_generation"])
        self.assertFalse(side_effect_marker.exists())

        show_human = self.run_successor(installation, no_mp_environment, intent_state,
                                        "handoff", "show", frozen["id"])
        self.assertEqual(show_human.returncode, 0, show_human.stderr)
        self.assertIn("Configured choice: codex-luna (gpt-6-luna) at xhigh", show_human.stdout)
        self.assertIn("Candidate digest:", show_human.stdout)
        self.assertIn("Native hook generation: unknown", show_human.stdout)
        self.assertIn("Effective permissions: unknown", show_human.stdout)
        self.assertIn("Readiness: blocked (native_observations_unavailable)", show_human.stdout)
        show_json = self.run_successor(installation, no_mp_environment, intent_state,
                                       "handoff", "show", frozen["id"], "--json")
        self.assertEqual(show_json.returncode, 0, show_json.stderr)
        shown_handoff = json.loads(show_json.stdout)["handoff"]
        expected_handoff = {**frozen, "phase_readiness": handoff_engine.phase_readiness(frozen)}
        self.assertEqual(shown_handoff, expected_handoff)
        self.assertNotIn("successor_binding", shown_handoff["phase_readiness"]["launch"]["checks"])
        self.assertEqual(shown_handoff["phase_readiness"]["transfer"]["status"], "blocked")
        before_reconcile = self.tree_snapshot()
        local_store_bytes = (intent_state / "successor-intents.json").read_bytes()
        reconciled = self.run_successor(installation, no_mp_environment, intent_state,
                                        "handoff", "reconcile", frozen["id"], "--json")
        self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
        reconciliation = json.loads(reconciled.stdout)["reconciliation"]
        self.assertEqual(reconciliation["status"], "blocked")
        self.assertFalse(reconciliation["native_queries_performed"])
        self.assertFalse(reconciliation["writes_performed"])
        self.assertIn("native_generation_unavailable", reconciliation["blockers"])
        self.assertEqual(reconciliation["phase_readiness"]["transfer"]["status"], "blocked")
        self.assertEqual(before_reconcile, self.tree_snapshot())
        self.assertEqual(local_store_bytes, (intent_state / "successor-intents.json").read_bytes())
        self.assertFalse(side_effect_marker.exists())

        cancelled = self.run_successor(installation, no_mp_environment, intent_state,
                                       "successor", "cancel", intent["id"])
        self.assertEqual(cancelled.returncode, 1)
        self.assertIn("Only a configured successor intent can be cancelled", cancelled.stderr)
        changed = self.run_successor(
            installation, guarded_environment, intent_state, "successor",
            *self.successor_scope_args("configure", repo, config_dir, mp_state_dir),
            "--family", "codex-astra", "--effort", "xhigh",
        )
        self.assertEqual(changed.returncode, 1)
        self.assertIn("already configured or frozen", changed.stderr)
        same_choice_new_provenance = self.run_successor(
            installation, guarded_environment, intent_state, "successor",
            *self.successor_scope_args("configure", repo, config_dir, mp_state_dir),
            "--family", "codex-luna", "--effort", "xhigh",
        )
        self.assertEqual(same_choice_new_provenance.returncode, 1)
        self.assertIn("already configured or frozen", same_choice_new_provenance.stderr)
        after_reconfigure = json.loads((intent_state / "successor-intents.json").read_text(encoding="utf-8"))
        saved_after_reconfigure = after_reconfigure["scopes"][next(iter(after_reconfigure["scopes"]))][0]
        self.assertEqual(saved_after_reconfigure["choice"], saved_choice)
        self.assertEqual(saved_after_reconfigure["choice"]["active_profile"], "Codex-only")
        self.assertEqual(after_reconfigure["handoffs"][frozen["id"]], frozen)
        unsupported = self.run_successor(installation, no_mp_environment, intent_state,
                                         "handoff", "launch", frozen["id"])
        self.assertNotEqual(unsupported.returncode, 0)
        self.assertFalse(side_effect_marker.exists())

    def test_handoff_freeze_rejects_wrong_scope_and_changed_configured_candidate(self):
        installation = self.install_copy()
        config_dir, mp_state_dir, environment = self.model_preferences_fixture()
        repo = self.init_repo(self.root / "handoff-exact-worktree")
        other_repo = self.init_repo(self.root / "handoff-wrong-worktree")
        intent_state = self.root / "handoff-reject-state"
        configured = self.run_successor(
            installation, environment, intent_state, "successor",
            *self.successor_scope_args("configure", repo, config_dir, mp_state_dir),
            "--family", "codex-luna", "--effort", "xhigh",
        )
        self.assertEqual(configured.returncode, 0, configured.stderr)
        intent = json.loads(configured.stdout)
        wrong_worktree = self.run_successor(
            installation, environment, intent_state, "handoff", "freeze", intent["id"],
            "--worktree", str(other_repo), "--mp-config-dir", str(config_dir),
            "--mp-state-dir", str(mp_state_dir), "--json",
        )
        self.assertEqual(wrong_worktree.returncode, 1)
        self.assertIn("canonical worktree does not match", wrong_worktree.stderr)

        config_path = config_dir / "profiles.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["models"]["codex-luna"]["slug"] = "changed-configured-model"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        changed_candidate = self.run_successor(
            installation, environment, intent_state, "handoff", "freeze", intent["id"],
            "--worktree", str(repo), "--mp-config-dir", str(config_dir),
            "--mp-state-dir", str(mp_state_dir), "--json",
        )
        self.assertEqual(changed_candidate.returncode, 1)
        self.assertIn("configured candidate changed", changed_candidate.stderr)
        store = json.loads((intent_state / "successor-intents.json").read_text(encoding="utf-8"))
        self.assertEqual(store["handoffs"], {})
        record = store["scopes"][next(iter(store["scopes"]))][0]
        self.assertNotIn("handoff_id", record)
        self.assertEqual(record["choice"]["snapshot"]["model"], "gpt-6-luna")

        config["models"]["codex-luna"]["slug"] = "gpt-6-luna"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        seat_path = mp_state_dir / "orchestrators.json"
        registered_seats = json.loads(seat_path.read_text(encoding="utf-8"))
        registered_seats["lane"]["session"] = "changed-fixture-session"
        seat_path.write_text(json.dumps(registered_seats), encoding="utf-8")
        seat_path.chmod(0o600)
        wrong_seat = self.run_successor(
            installation, environment, intent_state, "handoff", "freeze", intent["id"],
            "--worktree", str(repo), "--mp-config-dir", str(config_dir),
            "--mp-state-dir", str(mp_state_dir), "--json",
        )
        self.assertEqual(wrong_seat.returncode, 1)
        self.assertIn("registered Model Preferences seat changed", wrong_seat.stderr)
        unchanged = json.loads((intent_state / "successor-intents.json").read_text(encoding="utf-8"))
        self.assertEqual(unchanged["handoffs"], {})

    def test_preserve_observed_freeze_stays_blocked_and_transfer_receipt_reducer_is_idempotent(self):
        installation = self.install_copy()
        config_dir, mp_state_dir, environment = self.model_preferences_fixture()
        repo = self.init_repo(self.root / "handoff-preserve-worktree")
        intent_state = self.root / "handoff-preserve-state"
        configured = self.run_successor(
            installation, environment, intent_state, "successor",
            *self.successor_scope_args("configure", repo, config_dir, mp_state_dir),
        )
        self.assertEqual(configured.returncode, 0, configured.stderr)
        intent = json.loads(configured.stdout)
        frozen_result = self.run_successor(
            installation, environment, intent_state, "handoff", "freeze", intent["id"],
            "--worktree", str(repo), "--mp-config-dir", str(config_dir),
            "--mp-state-dir", str(mp_state_dir), "--json",
        )
        self.assertEqual(frozen_result.returncode, 0, frozen_result.stderr)
        frozen = json.loads(frozen_result.stdout)["handoff"]
        self.assertEqual(frozen["choice"], {"kind": "preserve_observed_predecessor"})
        self.assertEqual(frozen["observations"]["predecessor_model"]["value"], None)
        self.assertEqual(frozen["observations"]["predecessor_effort"]["value"], None)
        self.assertEqual(frozen["readiness"]["status"], "blocked")
        self.assertEqual(frozen["native_generation"]["status"], "unknown")

        synthetic_intent = {"id": "intent", "lifecycle_state": "frozen"}
        synthetic_handoff = {
            "id": "handoff", "intent_id": "intent", "intent_digest": "snapshot-digest",
            "state": "transfer_prepared",
        }
        receipt = handoff_engine._ConfirmedTransferReceipt(
            "handoff", "intent", "snapshot-digest", "receipt-1",
        )
        consumed_intent, transferred, duplicate = handoff_engine._consume_confirmed_transfer(
            synthetic_intent, synthetic_handoff, receipt,
        )
        self.assertFalse(duplicate)
        self.assertEqual(consumed_intent["lifecycle_state"], "consumed")
        self.assertEqual(transferred["state"], "transferred")
        replay_intent, replay_handoff, duplicate = handoff_engine._consume_confirmed_transfer(
            consumed_intent, transferred, receipt,
        )
        self.assertTrue(duplicate)
        self.assertEqual(replay_intent, consumed_intent)
        self.assertEqual(replay_handoff, transferred)
        wrong_receipt = handoff_engine._ConfirmedTransferReceipt(
            "handoff", "intent", "snapshot-digest", "receipt-2",
        )
        with self.assertRaises(handoff_engine.HandoffError):
            handoff_engine._consume_confirmed_transfer(consumed_intent, transferred, wrong_receipt)

    def test_successor_doctor_is_read_only_outside_git_and_installed_module_is_packaged(self):
        installation = self.install_copy()
        runtime = Path(installation["runtime"])
        self.assertTrue((runtime / "successor_intents.py").is_file())
        self.assertTrue((runtime / "handoff_engine.py").is_file())
        state_dir = self.root / "missing-state"
        before = self.tree_snapshot()
        result = self.run_successor(installation, self.environment, state_dir,
                                    "doctor", "--starfleet-json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        expected = {"schema_version": 1, "checks": {
            "configuration": "ready", "registration": "not_applicable", "worker": "not_applicable",
            "intended_delivery": "not_applicable", "observed_delivery": "unknown",
        }}
        self.assertEqual(report, expected)
        self.assertTrue(all(type(state) is str for state in report["checks"].values()))
        self.assertNotIn(str(state_dir), result.stdout)
        self.assertEqual(before, self.tree_snapshot())

        human_doctor = self.run_successor(installation, self.environment, state_dir, "doctor")
        self.assertEqual(human_doctor.returncode, 0, human_doctor.stderr)
        self.assertIn("configuration: ready", human_doctor.stdout)
        self.assertIn("observed_delivery: unknown", human_doctor.stdout)
        self.assertEqual(before, self.tree_snapshot())

        guide = self.run_process([installation["command"], "guide", "--print"])
        self.assertEqual(guide.returncode, 0, guide.stderr.decode())
        self.assertIn(b"one-shot successor intent", guide.stdout)
        self.assertEqual(before, self.tree_snapshot())

        state_dir.mkdir(mode=0o700)
        (state_dir / "successor-intents.json").write_text("{invalid", encoding="utf-8")
        (state_dir / "successor-intents.json").chmod(0o600)
        invalid = self.run_successor(installation, self.environment, state_dir,
                                     "doctor", "--starfleet-json")
        self.assertEqual(invalid.returncode, 0, invalid.stderr)
        invalid_report = json.loads(invalid.stdout)
        self.assertEqual(invalid_report["checks"]["configuration"], "unknown")
        self.assertNotIn("invalid", invalid.stdout)

    def test_successor_has_no_persistent_mode_or_generation_claim(self):
        installation = self.install_copy()
        config_dir, mp_state_dir, environment = self.model_preferences_fixture()
        repo = self.init_repo(self.root / "successor-one-shot-worktree")
        intent_state = self.root / "one-shot-state"
        result = self.run_successor(
            installation, environment, intent_state, "successor",
            *self.successor_scope_args("configure", repo, config_dir, mp_state_dir), "--persistent",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(intent_state.exists())


if __name__ == "__main__":
    unittest.main()
