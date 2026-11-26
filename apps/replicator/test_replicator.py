import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
SOURCE_NAMES = ("replicator.py", "install.py", "guide.md", "README.md")
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
import replicator


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

    def test_installed_retirement_preview_preserves_snapshots_and_exact_generation(self):
        installation = self.install_copy()
        shutil.rmtree(self.source)
        snapshot = self.root / "snapshots"
        snapshot.mkdir()
        base = {"harness": "codex", "surface": "old-pane", "workspace": "workspace", "started_at": 10}
        bindings = {session: dict(base, session=session) for session in (
            "hold", "recover", "review", "stale", "old-scan", "open-post", "warp-gap", "warp-conflict", "current",
        )}
        hailing = {
            "config": {"targets": list(bindings.values())},
            "health": {"stale_after": 120},
            "last_scan": {
                "completed_at": time.time(),
                "target_errors": [{"session": session, "stage": "initial_scan", "error": "Error: not_found:"}
                                  for session in bindings if session != "current"],
                "targets": [dict(binding, inbox_status="complete" if session == "current" else "incomplete",
                                 started_at=9 if session == "old-scan" else 10)
                            for session, binding in bindings.items()],
            },
        }
        cmux = {
            "truncated": False,
            "owner_availability": {"agent_sessions": "available"},
            "items": [{
                "lifecycle": "running", "placement": {"kind": "local"},
                "projections": [{"panel_id": "new-pane", "workspace_id": "workspace"}],
                "agents": [{"kind": "codex", "session_id": "recover", "state": "idle"}],
            }],
        }
        discord = {"threads": [
            {"thread": "1", "owner": bindings["hold"], "open": False, "queue": {"pending": 2}},
            {"thread": "2", "owner": bindings["open-post"], "open": True, "queue": {}},
        ]}
        warp = {
            "reservations": [],
            "completion_route_gaps": [
                {"owner": bindings["warp-gap"], "request_ids": ["job"]},
                {"owner": dict(bindings["warp-conflict"], harness="claude"), "request_ids": ["disputed"]},
            ],
        }
        evidence = {"retirements": [
            {"binding": dict(bindings[session], started_at=9 if session == "stale" else 10),
             "exit_evidence": "exit receipt", "obligations_evidence": "checked owner obligations"}
            for session in ("hold", "review", "stale", "open-post", "warp-gap", "warp-conflict")
        ]}
        for name, value in (("hailing", hailing), ("cmux", cmux), ("discord", discord),
                            ("warp", warp), ("evidence", evidence)):
            (snapshot / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
        command = [installation["command"], "retirement-preview", "--snapshot-dir", str(snapshot),
                   "--retirement-evidence", str(snapshot / "evidence.json"), "--json"]
        before = self.tree_snapshot()
        result = self.run_process(command)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        rows = {row["binding"]["session"]: row for row in json.loads(result.stdout)["registrations"]}
        self.assertEqual({session: row["disposition"] for session, row in rows.items()}, {
            "HOLD": "hold", "RECOVER": "recover", "REVIEW": "review_retirement",
            "STALE": "hold", "OPEN-POST": "review_retirement", "WARP-GAP": "hold", "WARP-CONFLICT": "hold",
        })
        self.assertEqual(rows["HOLD"]["discord_pending"], [{"thread": "1", "pending": 2}])
        self.assertEqual(rows["RECOVER"]["current_panel"], {"panel_id": "new-pane", "workspace_id": "workspace"})
        self.assertEqual(rows["WARP-GAP"]["warp_route_gaps"], ["job"])
        self.assertEqual(rows["WARP-CONFLICT"]["warp_route_gaps"], [])
        self.assertEqual(rows["WARP-CONFLICT"]["warp_identity_conflicts"], ["disputed"])
        self.assertEqual(self.tree_snapshot(), before)

        cmux["truncated"] = True
        (snapshot / "cmux.json").write_text(json.dumps(cmux), encoding="utf-8")
        result = self.run_process(command)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"complete CMUX agent snapshot", result.stderr)

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
        guide_result = self.run_process([str(launcher), "guide", "--print"])
        after = self.tree_snapshot()

        self.assertEqual(help_result.returncode, 0, help_result.stderr.decode())
        self.assertIn(b"usage: replicator", help_result.stdout)
        self.assertIn(b"guide", help_result.stdout)
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

    def test_installed_handoff_report_keeps_retirement_separate(self):
        repo = self.init_repo(self.root / "target-worktree")
        installation, environment = self.install_test_environment(self.cmux_fixture(repo), repo)
        handoff_file = self.root / "evidence.json"
        old_request = {
            "tool": "warp-drive", "id": "11111111-2222-4333-8444-555555555555",
            "state": "unknown", "affects": [],
            "evidence": "Verified stop and no remaining dependent work or delivery",
        }
        reply = {
            "tool": "discord-watch", "id": "1234567890123456789",
            "state": "pending", "affects": ["outgoing_retirement"],
            "evidence": "Exact thread reports a pending reply",
        }
        record = {
            "checkout": {"worktree": str(repo), "head": "a" * 40, "acceptance": "accepted",
                         "evidence": "Exact incoming acceptance receipt"},
            "resources_complete": True,
            "resources": [old_request, reply],
        }

        def run_report(*args):
            return subprocess.run(
                [installation["command"], "preflight", "--handoff-file", str(handoff_file), *args],
                cwd=repo, env=environment, capture_output=True, check=False,
            )

        handoff_file.write_text(json.dumps(record), encoding="utf-8")
        before = self.tree_snapshot()
        result = run_report("--json")
        payload = json.loads(result.stdout)
        report = payload["handoff"]
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["operational_readiness"], "unknown")
        self.assertEqual(payload["custody_acceptance"], "not_assessed")
        self.assertEqual(report["source"], "explicit_owner_record")
        self.assertEqual(report["checkout_acceptance"]["status"], "recorded_accepted")
        self.assertEqual(report["outgoing_retirement"]["status"], "blocked")
        self.assertTrue(report["resources"][0]["historical_unknown"])
        self.assertEqual(report["resources"][1]["inspect_command"], "discord-watch managed " + reply["id"])
        self.assertIn(b"Outgoing retirement: blocked", run_report().stdout)
        self.assertEqual(before, self.tree_snapshot())

        record["resources"] = [old_request]
        handoff_file.write_text(json.dumps(record), encoding="utf-8")
        report = json.loads(run_report("--json").stdout)["handoff"]
        self.assertEqual(report["checkout_acceptance"]["status"], "recorded_accepted")
        self.assertEqual(report["outgoing_retirement"]["status"], "review")

        old_request["affects"] = ["outgoing_retirement"]
        del old_request["evidence"]
        handoff_file.write_text(json.dumps(record), encoding="utf-8")
        report = json.loads(run_report("--json").stdout)["handoff"]
        self.assertEqual(report["checkout_acceptance"]["status"], "recorded_accepted")
        self.assertEqual(report["outgoing_retirement"]["status"], "unknown")
        self.assertFalse(report["resources"][0]["historical_unknown"])

        old_request["affects"] = []
        handoff_file.write_text(json.dumps(record), encoding="utf-8")
        result = run_report("--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"Handoff evidence references must be nonempty", result.stderr)

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


if __name__ == "__main__":
    unittest.main()
