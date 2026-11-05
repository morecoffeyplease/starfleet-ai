import json
import os
from pathlib import Path
import shlex
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
    def test_install_refreshes_existing_owned_alias_bundle_within_selected_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            runtime = folder / "runtime"
            codex_settings = folder / "codex.json"
            claude_settings = folder / "claude.json"
            env = {**os.environ, "HOME": str(folder), "XDG_DATA_HOME": str(folder / "data"),
                   "CODEX_HOME": str(folder / ".codex")}
            first = subprocess.run([sys.executable, str(CURRENT_CLI), "install", "--harness", "codex",
                                    "--settings", str(codex_settings), "--runtime-dir", str(runtime)],
                                   capture_output=True, text=True, env=env)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

            owned = [runtime / name for name in (
                "context_injector_codex.py", "pattern_buffer_codex.py",
                "context_injector_codex_checkpoint.py", "pattern_buffer_codex_checkpoint.py",
                "context_injector_codex_enrollment.py", "pattern_buffer_codex_enrollment.py",
                "context_injector_codex_sessions.py", "pattern_buffer_codex_sessions.py",
                "context_injector_codex_reliability.py", "pattern_buffer_codex_reliability.py")]
            for path in owned:
                path.write_bytes(path.read_bytes() + b"\n# controlled older runtime\n")
            old_codex_settings = codex_settings.read_bytes()

            second = subprocess.run([sys.executable, str(CURRENT_CLI), "install", "--harness", "claude",
                                     "--settings", str(claude_settings), "--runtime-dir", str(runtime)],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            sources = {
                "context_injector_codex.py": CURRENT_CLI,
                "pattern_buffer_codex.py": CURRENT_CLI,
                "context_injector_codex_checkpoint.py": CURRENT_CLI.with_name("pattern_buffer_checkpoint.py"),
                "pattern_buffer_codex_checkpoint.py": CURRENT_CLI.with_name("pattern_buffer_checkpoint.py"),
                "context_injector_codex_enrollment.py": CURRENT_CLI.with_name("pattern_buffer_enrollment.py"),
                "pattern_buffer_codex_enrollment.py": CURRENT_CLI.with_name("pattern_buffer_enrollment.py"),
                "context_injector_codex_sessions.py": CURRENT_CLI.with_name("pattern_buffer_sessions.py"),
                "pattern_buffer_codex_sessions.py": CURRENT_CLI.with_name("pattern_buffer_sessions.py"),
                "context_injector_codex_reliability.py": CURRENT_CLI.with_name("pattern_buffer_reliability.py"),
                "pattern_buffer_codex_reliability.py": CURRENT_CLI.with_name("pattern_buffer_reliability.py"),
            }
            for name, source in sources.items():
                with self.subTest(path=name):
                    self.assertEqual((runtime / name).read_bytes(), source.read_bytes())
            self.assertEqual(codex_settings.read_bytes(), old_codex_settings)
            self.assertTrue(claude_settings.exists())

    def test_install_upgrades_recognized_legacy_singleton_and_remains_repeatable(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            runtime = folder / "runtime"
            codex_settings = folder / "codex.json"
            claude_settings = folder / "claude.json"
            env = {**os.environ, "HOME": str(folder), "XDG_DATA_HOME": str(folder / "data"),
                   "CODEX_HOME": str(folder / ".codex")}
            first = subprocess.run([sys.executable, str(CURRENT_CLI), "install", "--harness", "codex",
                                    "--settings", str(codex_settings), "--runtime-dir", str(runtime)],
                                   capture_output=True, text=True, env=env)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

            for path in runtime.glob("pattern_buffer_codex*.py"):
                path.unlink()
            for name in ("context_injector_codex_sessions.py", "context_injector_codex_reliability.py"):
                (runtime / name).unlink()
            checkpoint = runtime / "context_injector_codex_checkpoint.py"
            checkpoint.write_text(checkpoint.read_text().replace(
                "Checkpoint support for Pattern Buffer.", "Checkpoint support for Context Injector."))
            old_codex_settings = codex_settings.read_bytes()

            for attempt in range(2):
                installed = subprocess.run([sys.executable, str(CURRENT_CLI), "install", "--harness", "claude",
                                            "--settings", str(claude_settings), "--runtime-dir", str(runtime)],
                                           capture_output=True, text=True, env=env)
                self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
                self.assertEqual(codex_settings.read_bytes(), old_codex_settings)
                if attempt == 0:
                    self.assertTrue((runtime / "pattern_buffer_codex.py").is_file())

            sources = {
                "context_injector_codex.py": CURRENT_CLI,
                "pattern_buffer_codex.py": CURRENT_CLI,
                "context_injector_codex_checkpoint.py": CURRENT_CLI.with_name("pattern_buffer_checkpoint.py"),
                "pattern_buffer_codex_checkpoint.py": CURRENT_CLI.with_name("pattern_buffer_checkpoint.py"),
                "context_injector_codex_enrollment.py": CURRENT_CLI.with_name("pattern_buffer_enrollment.py"),
                "pattern_buffer_codex_enrollment.py": CURRENT_CLI.with_name("pattern_buffer_enrollment.py"),
                "context_injector_codex_sessions.py": CURRENT_CLI.with_name("pattern_buffer_sessions.py"),
                "pattern_buffer_codex_sessions.py": CURRENT_CLI.with_name("pattern_buffer_sessions.py"),
                "context_injector_codex_reliability.py": CURRENT_CLI.with_name("pattern_buffer_reliability.py"),
                "pattern_buffer_codex_reliability.py": CURRENT_CLI.with_name("pattern_buffer_reliability.py"),
            }
            for name, source in sources.items():
                with self.subTest(path=name):
                    self.assertEqual((runtime / name).read_bytes(), source.read_bytes())

    def test_install_refusal_preflights_alias_symlinks_without_writing_runtime_or_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            runtime = folder / "runtime"
            runtime.mkdir()
            target = folder / "target.py"
            target.write_text("owned-looking alias\n")
            (runtime / "context_injector_codex.py").symlink_to(target)
            (runtime / "pattern_buffer_codex.py").write_text("owned-looking alias\n")
            settings = folder / "claude.json"
            before = {path.name: (path.is_symlink(), path.read_bytes() if path.is_file() and not path.is_symlink()
                                  else os.readlink(path) if path.is_symlink() else None)
                      for path in runtime.iterdir()}
            env = {**os.environ, "HOME": str(folder), "XDG_DATA_HOME": str(folder / "data"),
                   "CODEX_HOME": str(folder / ".codex")}

            refused = subprocess.run([sys.executable, str(CURRENT_CLI), "install", "--harness", "claude",
                                      "--settings", str(settings), "--runtime-dir", str(runtime)],
                                     capture_output=True, text=True, env=env)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("unsafe codex runtime alias bundle", refused.stderr)
            after = {path.name: (path.is_symlink(), path.read_bytes() if path.is_file() and not path.is_symlink()
                                 else os.readlink(path) if path.is_symlink() else None)
                     for path in runtime.iterdir()}
            self.assertEqual(after, before)
            self.assertFalse(settings.exists())
            self.assertFalse(settings.with_name(settings.name + ".context-injector.lock").exists())

    def test_other_harness_alias_refusal_reports_same_settings_recovery_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            runtime = folder / "runtime"
            codex_settings = folder / "codex.json"
            claude_settings = folder / "claude.json"
            env = {**os.environ, "HOME": str(folder), "XDG_DATA_HOME": str(folder / "data"),
                   "CODEX_HOME": str(folder / ".codex")}
            for harness, settings in (("codex", codex_settings), ("claude", claude_settings)):
                installed = subprocess.run([sys.executable, str(CURRENT_CLI), "install", "--harness", harness,
                                            "--settings", str(settings), "--runtime-dir", str(runtime)],
                                           capture_output=True, text=True, env=env)
                self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)

            damaged = runtime / "pattern_buffer_claude.py"
            damaged.write_text("unowned other-harness runtime\n")
            runtime_before = {path.name: path.read_bytes() for path in runtime.iterdir() if path.is_file()}
            settings_before = {path: path.read_bytes() for path in (codex_settings, claude_settings)}
            settings_locks = [path.with_name(path.name + ".context-injector.lock")
                              for path in (codex_settings, claude_settings)]

            def lock_state(path):
                if not path.exists():
                    return None
                stat = path.stat()
                return path.read_bytes(), stat.st_mode & 0o777, stat.st_mtime_ns

            locks_before = {path: lock_state(path) for path in settings_locks}
            refused = subprocess.run([sys.executable, str(CURRENT_CLI), "install", "--harness", "codex",
                                      "--settings", str(codex_settings), "--runtime-dir", str(runtime)],
                                     capture_output=True, text=True, env=env)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("refusing to update unowned runtime entrypoint", refused.stderr)
            self.assertIn("Same-settings recovery command:", refused.stderr)
            guidance, command = refused.stderr.split("Same-settings recovery command: ", 1)
            recovery = shlex.split(command)
            self.assertEqual(recovery[recovery.index("--harness") + 1], "codex")
            self.assertEqual(recovery[recovery.index("--settings") + 1], str(codex_settings))
            recovery_runtime = Path(recovery[recovery.index("--runtime-dir") + 1])
            self.assertIn("Any existing PATH launcher remains unchanged", guidance)
            self.assertIn(shlex.quote(str(recovery_runtime / "pattern-buffer")), guidance)
            self.assertFalse(recovery_runtime.exists())
            self.assertEqual({path.name: path.read_bytes() for path in runtime.iterdir() if path.is_file()},
                             runtime_before)
            self.assertEqual({path: path.read_bytes() for path in settings_before}, settings_before)
            self.assertEqual({path: lock_state(path) for path in settings_locks}, locks_before)

    def test_install_refuses_unowned_entrypoints_and_orphan_helpers_before_any_write(self):
        for filename, content, expected in (
                ("context_injector_claude.py", "third-party entrypoint\n", "unowned runtime entrypoint"),
                ("pattern_buffer_sessions.py", "third-party helper\n", "without an owned entrypoint")):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                runtime = folder / "runtime"
                runtime.mkdir()
                (runtime / filename).write_text(content)
                settings = folder / "claude.json"
                before = {path.name: path.read_bytes() for path in runtime.iterdir()}
                env = {**os.environ, "HOME": str(folder), "XDG_DATA_HOME": str(folder / "data"),
                       "CODEX_HOME": str(folder / ".codex")}

                refused = subprocess.run([sys.executable, str(CURRENT_CLI), "install", "--harness", "claude",
                                          "--settings", str(settings), "--runtime-dir", str(runtime)],
                                         capture_output=True, text=True, env=env)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn(expected, refused.stderr)
                self.assertEqual({path.name: path.read_bytes() for path in runtime.iterdir()}, before)
                self.assertFalse(settings.exists())
                self.assertFalse(settings.with_name(settings.name + ".context-injector.lock").exists())

    def test_handoff_refusal_suggests_only_a_verified_installed_cli_with_full_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            env = {**os.environ, "HOME": str(folder), "XDG_DATA_HOME": str(folder / "data"),
                   "CODEX_HOME": str(folder / ".codex")}
            worktree = folder / "repo"
            worktree.mkdir()
            self.git(worktree, "init", "-q")
            self.git(worktree, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "--allow-empty", "-qm", "Fixture")
            cli(CURRENT_CLI, worktree, "init", "--default-role", "orchestrator", env=env)
            runtime = folder / "runtime"
            settings = folder / "hooks.json"
            installed = subprocess.run([sys.executable, str(CURRENT_CLI), "install", "--harness", "codex",
                                        "--settings", str(settings), "--runtime-dir", str(runtime)],
                                       capture_output=True, text=True, env=env)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)

            for prefix in ("context_injector", "pattern_buffer"):
                for name in ("", "_checkpoint", "_enrollment", "_sessions", "_reliability"):
                    path = runtime / f"{prefix}_codex{name}.py"
                    path.write_bytes(path.read_bytes() + b"\n# controlled older runtime\n")
            refused = cli(CURRENT_CLI, worktree, "handoff-check", "--harness", "codex", "--session",
                          "session with spaces", "--settings", str(settings), success=False, env=env)
            report = json.loads(refused.stdout)
            self.assertEqual(report["installed_hooks"]["state"], "compatible_alternate_cli")
            command = report["installed_hooks"]["next_action"]
            argv = shlex.split(command)
            self.assertEqual(argv[0], sys.executable)
            self.assertEqual(argv[1], str(runtime / "pattern_buffer_codex.py"))
            self.assertEqual(argv[2:], ["--cwd", str(worktree), "handoff-check", "--harness", "codex",
                                        "--session", "session with spaces", "--settings", str(settings)])
            self.assertIn("complete command with the verified compatible installed entrypoint",
                          report["installed_hooks"]["detail"])

            (runtime / "pattern_buffer_codex_sessions.py").write_bytes(b"unverified candidate\n")
            refused = cli(CURRENT_CLI, worktree, "handoff-check", "--harness", "codex", "--session",
                          "session with spaces", "--settings", str(settings), success=False, env=env)
            report = json.loads(refused.stdout)
            self.assertEqual(report["installed_hooks"]["state"], "compatible_alternate_cli")
            command = report["installed_hooks"]["next_action"]
            self.assertEqual(shlex.split(command)[1], str(runtime / "context_injector_codex.py"))

            hook_alias = runtime / "context_injector_codex.py"
            hook_alias.write_text(hook_alias.read_text().replace(
                "starfleet-context-injector:", "unverified-runtime:"))
            refused = cli(CURRENT_CLI, worktree, "handoff-check", "--harness", "codex", "--session",
                          "session with spaces", "--settings", str(settings), success=False, env=env)
            report = json.loads(refused.stdout)
            self.assertEqual(report["installed_hooks"]["state"], "problem")
            self.assertIn("No compatible installed entrypoint was verified", report["installed_hooks"]["detail"])

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
