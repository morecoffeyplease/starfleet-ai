"""CLI coverage for the task preparation upstream overlap advisory."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("pattern_buffer.py")


class DispatchUpstreamOverlapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base_dir = Path(self.temp.name)
        self.repo = self.base_dir / "repo"
        self.owner = self.repo
        self.overlap = self.base_dir / "worktrees" / "overlap"
        self.control = self.base_dir / "worktrees" / "control"
        self.brief = self.base_dir / "task.md"
        self.brief.write_text("Prepare a disposable task.\n")
        self._init_repo(self.repo, "main")
        self.git(self.repo, "config", "user.name", "Fixture")
        self.git(self.repo, "config", "user.email", "fixture@example.invalid")
        self.git(self.repo, "config", "gc.auto", "0")
        self.git(self.repo, "config", "maintenance.auto", "false")
        self.git(self.repo, "config", "core.hooksPath", "/dev/null")
        (self.repo / ".gitignore").write_text(".context-injector/\n")
        (self.repo / "apps").mkdir()
        (self.repo / "apps" / "old_editor.py").write_text(
            "def render():\n    return 'base'\n\n\ndef reset():\n    return None\n"
        )
        (self.repo / "README.md").write_text("Fixture repository.\n")
        self.base = self.commit(self.repo, "fixture fork")
        self.git(self.repo, "branch", "overlap", self.base)
        self.git(self.repo, "branch", "control", self.base)
        (self.repo / "apps" / "old_editor.py").write_text(
            "def render():\n    return 'upstream edit'\n\n\ndef reset():\n    return None\n"
        )
        self.upstream = self.commit(self.repo, "edit old editor on main")
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", self.upstream)
        self.git(self.repo, "config", "remote.origin.url", str(self.base_dir / "remote.git"))
        for branch in ("overlap", "control"):
            self.git(self.repo, "config", f"branch.{branch}.remote", "origin")
            self.git(self.repo, "config", f"branch.{branch}.merge", "refs/heads/main")
        context = self.owner / ".context-injector" / "context.md"
        context.parent.mkdir(parents=True)
        context.write_text(
            "# shared\nApproved fixture context.\n# implementer\nImplement the assigned task.\n"
            "# orchestrator\nPrepare the assigned task.\n"
        )
        self.cli(self.owner, "init", "--default-role", "orchestrator")
        self.git(self.repo, "worktree", "add", str(self.overlap), "overlap")
        self.git(self.repo, "worktree", "add", str(self.control), "control")
        self.real_git = shutil.which("git")

    def _init_repo(self, path, branch):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["git", "-c", f"init.defaultBranch={branch}", "init", "-q", str(path)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def git(self, root, *args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, f"git {args!r}: {result.stderr}")
        return result.stdout.strip()

    def commit(self, root, message):
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", message)
        return self.git(root, "rev-parse", "HEAD")

    def update_upstream_ref(self):
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", self.git(self.repo, "rev-parse", "HEAD"))

    def cli(self, root, *args, env=None):
        command_env = os.environ.copy()
        for key in ("CMUX_AGENT_LAUNCH_KIND", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID"):
            command_env.pop(key, None)
        command_env.update(env or {})
        result = subprocess.run([sys.executable, str(SCRIPT), "--cwd", str(root), *args],
                                capture_output=True, text=True, timeout=20, env=command_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def prepare(self, target=None, env=None):
        return self.cli(target or self.overlap, "dispatch", "--owner-worktree", str(self.owner),
                        "--harness", "codex", "--task-file", str(self.brief), "--prepare-only", env=env)

    def delete_old_editor(self, commit=True):
        (self.overlap / "apps" / "old_editor.py").unlink()
        return self.commit(self.overlap, "delete old editor on task branch") if commit else None

    def task_gitdir(self, root):
        return Path(self.git(root, "rev-parse", "--absolute-git-dir"))

    def test_prepare_warns_for_upstream_edit_to_branch_deleted_old_editor(self):
        task_head = self.delete_old_editor()
        result = self.prepare()
        payload = json.loads(result.stdout)
        self.assertTrue(payload["task_id"])
        self.assertIn("Pattern Buffer upstream overlap advisory", result.stderr)
        self.assertIn(f"refs/remotes/origin/main at {self.upstream}", result.stderr)
        self.assertIn(f"fork {self.base}", result.stderr)
        self.assertIn(f"task HEAD {task_head}", result.stderr)
        self.assertIn('Shared path "apps/old_editor.py"', result.stderr)
        self.assertIn("task D", result.stderr)
        self.assertIn("upstream M", result.stderr)
        self.assertIn(f"git -C {self.overlap.resolve()} diff --find-renames", result.stderr)
        self.assertIn("does not establish semantic impact or current product behavior", result.stderr)
        self.assertTrue((self.task_gitdir(self.overlap) / "context-injector/upstream-overlap.json").is_file())

    def test_tracked_uncommitted_deletion_is_a_task_path(self):
        self.delete_old_editor(commit=False)
        result = self.prepare()
        self.assertIn('Shared path "apps/old_editor.py"', result.stderr)
        self.assertIn("task D", result.stderr)
        self.assertEqual(json.loads(result.stdout)["environment"]["STARFLEET_CONTEXT_DISPATCH"],
                         json.loads(result.stdout)["task_id"])

    def test_no_overlap_is_quiet_and_does_not_read_a_sibling_worktree(self):
        self.delete_old_editor()
        self.assertIn("Pattern Buffer upstream overlap advisory", self.prepare().stderr)
        (self.control / "README.md").write_text("Control branch change with no overlap.\n")
        self.commit(self.control, "change unrelated control path")
        result = self.prepare(self.control)
        self.assertEqual(result.stderr, "")
        self.assertTrue(json.loads(result.stdout)["task_id"])
        self.assertFalse((self.task_gitdir(self.control) / "context-injector/upstream-overlap.json").exists())

    def test_repeated_overlap_stays_quiet_after_unrelated_branch_and_main_commits(self):
        self.delete_old_editor()
        self.assertIn("Pattern Buffer upstream overlap advisory", self.prepare().stderr)
        self.assertEqual(self.prepare().stderr, "")
        (self.overlap / "notes.md").write_text("Unrelated task branch commit.\n")
        self.commit(self.overlap, "unrelated task branch change")
        self.assertEqual(self.prepare().stderr, "")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "unrelated.md").write_text("Unrelated upstream main commit.\n")
        self.commit(self.repo, "unrelated main change")
        self.update_upstream_ref()
        self.assertEqual(self.prepare().stderr, "")

    def test_new_upstream_edit_to_same_path_warns_again(self):
        self.delete_old_editor()
        self.assertIn("Pattern Buffer upstream overlap advisory", self.prepare().stderr)
        (self.repo / "apps" / "old_editor.py").write_text(
            "def render():\n    return 'second upstream edit'\n\n\ndef reset():\n    return None\n"
        )
        self.commit(self.repo, "second old editor edit on main")
        self.update_upstream_ref()
        result = self.prepare()
        self.assertIn("Pattern Buffer upstream overlap advisory", result.stderr)
        self.assertIn('Shared path "apps/old_editor.py"', result.stderr)

    def test_new_overlapping_path_warns_without_repeating_known_overlap(self):
        self.delete_old_editor()
        self.assertIn("Pattern Buffer upstream overlap advisory", self.prepare().stderr)
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "new-rule.md").write_text("A new upstream path.\n")
        self.commit(self.repo, "add new upstream rule")
        self.update_upstream_ref()
        (self.overlap / "docs").mkdir()
        (self.overlap / "docs" / "new-rule.md").write_text("A task change on the new path.\n")
        self.commit(self.overlap, "change newly overlapping path")
        result = self.prepare()
        self.assertIn('Shared path "docs/new-rule.md"', result.stderr)
        self.assertNotIn('Shared path "apps/old_editor.py"', result.stderr)

    def test_upstream_rename_overlaps_task_deletion_of_old_path(self):
        self.delete_old_editor()
        self.git(self.repo, "mv", "apps/old_editor.py", "apps/new_editor.py")
        (self.repo / "apps" / "new_editor.py").write_text(
            "def render():\n    return 'renamed upstream editor'\n\n\ndef reset():\n    return None\n"
        )
        self.commit(self.repo, "rename old editor on main")
        self.update_upstream_ref()
        result = self.prepare()
        self.assertIn('Shared path "apps/old_editor.py"', result.stderr)
        self.assertIn('apps/old_editor.py" -> "apps/new_editor.py', result.stderr)

    def test_task_rename_overlaps_upstream_edit_to_old_path(self):
        self.git(self.overlap, "mv", "apps/old_editor.py", "apps/task_editor.py")
        self.commit(self.overlap, "rename old editor on task branch")
        result = self.prepare()
        self.assertIn('Shared path "apps/old_editor.py"', result.stderr)
        self.assertIn('apps/old_editor.py" -> "apps/task_editor.py', result.stderr)

    def test_open_code_prepare_dispatch_uses_the_same_advisory(self):
        self.delete_old_editor()
        receipt = self.base_dir / "opencode-receipt.json"
        result = self.cli(
            self.overlap,
            "prepare-dispatch",
            "--owner-worktree", str(self.owner),
            "--harness", "opencode",
            "--task-file", str(self.brief),
            "--receipt-file", str(receipt),
            "--role", "implementer",
        )
        self.assertEqual(json.loads(result.stdout)["harness"], "opencode")
        self.assertTrue(receipt.is_file())
        self.assertIn("Pattern Buffer upstream overlap advisory", result.stderr)
        self.assertIn('Shared path "apps/old_editor.py"', result.stderr)

    def test_missing_known_upstream_ref_is_unavailable_even_with_local_main(self):
        repo = self.base_dir / "no-known-upstream"
        self._init_repo(repo, "main")
        self.git(repo, "config", "user.name", "Fixture")
        self.git(repo, "config", "user.email", "fixture@example.invalid")
        self.git(repo, "config", "gc.auto", "0")
        (repo / "tracked.txt").write_text("base\n")
        unknown_base = self.commit(repo, "local main base")
        self.git(repo, "branch", "task", unknown_base)
        context = repo / ".context-injector" / "context.md"
        context.parent.mkdir(parents=True)
        context.write_text("# shared\nFixture context.\n# implementer\nTask context.\n# orchestrator\nOwner context.\n")
        self.cli(repo, "init", "--default-role", "orchestrator")
        child = self.base_dir / "no-known-upstream-child"
        self.git(repo, "worktree", "add", str(child), "task")
        (child / "tracked.txt").write_text("local change\n")
        result = self.cli(child, "dispatch", "--owner-worktree", str(repo), "--harness", "codex",
                          "--task-file", str(self.brief), "--prepare-only")
        self.assertTrue(json.loads(result.stdout)["task_id"])
        self.assertIn("Pattern Buffer upstream overlap check unavailable", result.stderr)
        self.assertIn(f"task HEAD {self.git(child, 'rev-parse', 'HEAD')}", result.stderr)
        self.assertIn("No overlap conclusion is available", result.stderr)
        self.assertIn("Task preparation continues", result.stderr)

    def test_no_overlap_does_not_spawn_path_identity_git_calls(self):
        for index in range(8):
            path = self.repo / "docs" / f"upstream-{index}.md"
            path.parent.mkdir(exist_ok=True)
            path.write_text(f"Unrelated upstream change {index}.\n")
        self.commit(self.repo, "add unrelated upstream paths")
        self.update_upstream_ref()
        (self.control / "README.md").write_text("Control branch change with no overlap.\n")
        self.commit(self.control, "change unrelated control path")
        wrapper_dir = self.base_dir / "git-wrapper"
        wrapper_dir.mkdir()
        wrapper = wrapper_dir / "git"
        call_log = self.base_dir / "git-calls.jsonl"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "with open(os.environ['PB_GIT_CALL_LOG'], 'a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "real = os.environ['PB_REAL_GIT']\n"
            "os.execv(real, [real, *sys.argv[1:]])\n"
        )
        wrapper.chmod(0o755)
        env = {
            "PATH": str(wrapper_dir) + os.pathsep + os.environ.get("PATH", ""),
            "PB_REAL_GIT": self.real_git,
            "PB_GIT_CALL_LOG": str(call_log),
        }
        result = self.prepare(self.control, env=env)
        self.assertEqual(result.stderr, "")
        calls = [json.loads(line) for line in call_log.read_text().splitlines()]
        self.assertEqual([call for call in calls if "ls-tree" in call], [])


if __name__ == "__main__":
    unittest.main()
