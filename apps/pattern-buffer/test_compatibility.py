import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_checkpoints import NOTE


ROOT = Path(__file__).resolve().parents[2]
CURRENT_CLI = Path(__file__).with_name("pattern_buffer.py")
LEGACY_CLI = ROOT / "apps" / "context-injector" / "context_injector.py"


def cli(script, cwd, *args, success=True, env=None):
    result = subprocess.run([sys.executable, str(script), "--cwd", str(cwd), *args],
                            capture_output=True, text=True, env={**os.environ, **(env or {})})
    if success and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    if not success and result.returncode == 0:
        raise AssertionError("command unexpectedly succeeded: " + result.stdout)
    return result


class RenameCompatibilityTests(unittest.TestCase):
    def test_standard_install_uses_the_selected_home_for_runtime_and_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            home = folder / "home"
            data = home / "data"
            settings = home / "hooks.json"
            env = {**os.environ, "HOME": str(home), "XDG_DATA_HOME": str(data),
                   "CODEX_HOME": str(home / ".codex")}
            result = subprocess.run([sys.executable, str(CURRENT_CLI), "install", "--harness", "codex",
                                     "--settings", str(settings)], capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            runtime = data / "starfleet-ai" / "context-injector"
            launcher = home / ".local" / "bin" / "pattern-buffer"
            self.assertTrue(launcher.is_file())
            self.assertEqual(launcher.read_bytes(), (runtime / "pattern-buffer").read_bytes())
            self.assertIn(str(runtime / "context_injector_codex.py"), settings.read_text())
            self.assertEqual((runtime / "pattern_buffer.py").read_bytes(), CURRENT_CLI.read_bytes())

    def test_legacy_installer_redeploys_the_registered_runtime_and_stable_hook(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            isolated_env = {"HOME": str(folder), "XDG_DATA_HOME": str(folder / "data"),
                            "CODEX_HOME": str(folder / ".codex")}
            worktree = folder / "repo"
            worktree.mkdir()
            self.git(worktree, "init", "-q")
            self.git(worktree, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "--allow-empty", "-qm", "Fixture")
            cli(CURRENT_CLI, worktree, "init", "--default-role", "implementer")
            settings = folder / "hooks.json"
            runtime = folder / "runtime"
            install = subprocess.run([sys.executable, str(LEGACY_CLI), "install", "--harness", "codex",
                                      "--settings", str(settings), "--runtime-dir", str(runtime)],
                                     capture_output=True, text=True,
                                     env={**os.environ, **isolated_env})
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

            data = json.loads(settings.read_text())
            handlers = [handler for group in data["hooks"]["SessionStart"] for handler in group["hooks"]]
            self.assertEqual(len(handlers), 1)
            command = handlers[0]["command"]
            self.assertIn(str(runtime / "context_injector_codex.py"), command)
            self.assertTrue(command.endswith("starfleet-context-injector:codex:v1"))
            for name in ("context_injector_codex.py", "context_injector_codex_checkpoint.py",
                         "context_injector_codex_enrollment.py", "pattern_buffer.py",
                         "pattern_buffer_checkpoint.py", "pattern_buffer_enrollment.py",
                         "pattern_buffer_codex.py",
                         "pattern_buffer_codex_checkpoint.py", "pattern_buffer_codex_enrollment.py",
                         "pattern-buffer.template",
                         "guide.md", "pattern-buffer"):
                self.assertTrue((runtime / name).is_file(), name)

            payload = {"hook_event_name": "SessionStart", "source": "startup",
                       "session_id": "existing-session", "cwd": str(worktree)}
            event = subprocess.run(command, shell=True, input=json.dumps(payload), capture_output=True,
                                   text=True, cwd=worktree,
                                   env={**os.environ, **isolated_env})
            self.assertEqual(event.returncode, 0, event.stderr)
            self.assertIn("pattern-buffer | role: implementer", event.stdout)
            self.assertIn("Worktree:", event.stdout)

            first_settings = settings.read_bytes()
            reinstall = subprocess.run([sys.executable, str(CURRENT_CLI), "install", "--harness", "codex",
                                        "--settings", str(settings), "--runtime-dir", str(runtime)],
                                       capture_output=True, text=True,
                                       env={**os.environ, **isolated_env})
            self.assertEqual(reinstall.returncode, 0, reinstall.stdout + reinstall.stderr)
            self.assertEqual(settings.read_bytes(), first_settings)
            self.assertEqual(sum(handler["command"].endswith("starfleet-context-injector:codex:v1")
                                 for group in json.loads(first_settings)["hooks"]["SessionStart"]
                                 for handler in group["hooks"]), 1)

    def test_old_and_new_clis_read_the_same_existing_checkpoint_and_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            isolated_env = {"HOME": str(folder), "XDG_DATA_HOME": str(folder / "data"),
                            "CODEX_HOME": str(folder / ".codex")}
            owner = folder / "owner"
            owner.mkdir()
            self.git(owner, "init", "-q")
            self.git(owner, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "--allow-empty", "-qm", "Fixture")
            cli(CURRENT_CLI, owner, "init", "--default-role", "orchestrator", env=isolated_env)
            cli(CURRENT_CLI, owner, "checkpoint-init", "--harness", "codex", "--session",
                "existing-session", "--compact-at", "95", env=isolated_env)
            note = folder / "checkpoint-note.md"
            note.write_text(NOTE)
            cli(CURRENT_CLI, owner, "checkpoint-save", "--harness", "codex", "--session",
                "existing-session", "--file", str(note), env=isolated_env)

            worker = folder / "worker"
            self.git(owner, "worktree", "add", "-qb", "worker", str(worker))
            brief = folder / "task.md"
            brief.write_text("Existing task remains attached to its assigned worktree.\n")
            ticket = json.loads(cli(CURRENT_CLI, worker, "dispatch", "--harness", "codex",
                                    "--owner-worktree", str(owner), "--task-file", str(brief),
                                    "--prepare-only", env=isolated_env).stdout)

            checkpoint = json.loads(cli(CURRENT_CLI, owner, "checkpoint-status", "--harness", "codex",
                                        "--session", "existing-session", env=isolated_env).stdout)
            task = json.loads(cli(CURRENT_CLI, worker, "task-status", "--task-id", ticket["task_id"],
                                  env=isolated_env).stdout)
            checkpoint_path = Path(checkpoint["checkpoint_path"])
            task_path = Path(task["gitdir"]) / "context-injector" / "tasks" / ticket["task_id"] / "task.json"
            checkpoint_bytes = checkpoint_path.read_bytes()
            task_bytes = task_path.read_bytes()

            for script in (LEGACY_CLI, CURRENT_CLI):
                with self.subTest(script=script.name):
                    old_checkpoint = json.loads(cli(script, owner, "checkpoint-status", "--harness", "codex",
                                                    "--session", "existing-session", env=isolated_env).stdout)
                    old_task = json.loads(cli(script, worker, "task-status", "--task-id", ticket["task_id"],
                                              env=isolated_env).stdout)
                    # A status read advances elapsed time without changing the stored checkpoint.
                    self.assertEqual({k: v for k, v in old_checkpoint.items() if k != "seconds_since_review"},
                                     {k: v for k, v in checkpoint.items() if k != "seconds_since_review"})
                    self.assertEqual(old_task, task)
                    self.assertEqual(checkpoint_path.read_bytes(), checkpoint_bytes)
                    self.assertEqual(task_path.read_bytes(), task_bytes)

    @staticmethod
    def git(cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                              capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
