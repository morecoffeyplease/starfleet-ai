"""Exercise installation through the real command using isolated user directories."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


APP = Path(__file__).resolve().parents[1]


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="model-preferences-install-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source checkout"
        self.source.mkdir()
        for name in ("install.py", "model_preferences.py", "opencode_dispatch.py", "reviewer_guidance.py"):
            shutil.copyfile(APP / name, self.source / name)
        shutil.copyfile(APP / "cmux_transport.py", self.source / "cmux_transport.py")
        shutil.copyfile(APP.parent / "opencode-adapter" / "opencode_adapter.py", self.source / "opencode_adapter.py")
        self.runtime = self.root / "data"
        self.bin = self.root / "bin"
        self.config = self.root / "config"
        self.state = self.root / "state"
        self.claude = self.root / "claude.json"
        self.codex = self.root / "codex.json"
        self.command = self.bin / "model-preferences"
        self.cmux_bin = self.root / "cmux-bin"
        self.cmux_bin.mkdir()
        self.cmux = self.cmux_bin / "cmux"
        self.cmux.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--json\" ] && [ \"$2\" = \"current\" ]; then\n"
            "  printf '%s\\n' '{\"items\":[],\"truncated\":false}'\n"
            "  exit 0\n"
            "fi\n"
            "printf 'Unexpected CMUX fixture command: %s\\n' \"$*\" >&2\n"
            "exit 2\n"
        )
        self.cmux.chmod(0o755)
        for name in ("home", "xdg-config", "xdg-cache", "xdg-state"):
            (self.root / name).mkdir()
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("CMUX_")}
        self.env.update({
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "XDG_CACHE_HOME": str(self.root / "xdg-cache"),
            "XDG_STATE_HOME": str(self.root / "xdg-state"),
            "TMPDIR": str(self.root),
            "PATH": str(self.cmux_bin) + os.pathsep + self.env.get("PATH", os.defpath),
        })

    def install(self, *extra, ok=True):
        argv = [sys.executable, str(self.source / "install.py")]
        for key, value in (("runtime-dir", self.runtime), ("bin-dir", self.bin),
                           ("config-dir", self.config), ("state-dir", self.state),
                           ("claude-settings", self.claude), ("codex-settings", self.codex)):
            argv.extend(["--" + key, str(value)])
        result = subprocess.run([*argv, *extra], capture_output=True, text=True, timeout=20, env=self.env)
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return json.loads(result.stdout) if ok else result.stderr

    def run_command(self, *args, stdin=None):
        result = subprocess.run([str(self.command), *args], input=stdin, capture_output=True, text=True,
                                timeout=10, env=self.env)
        self.assertEqual(result.returncode, 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return json.loads(result.stdout)

    def active(self):
        self.install()
        self.run_command("init", "--examples")
        self.run_command("authority-set", "fixture authority")
        self.run_command("activate", "Claude-Codex", "--orchestrators", "future-only")

    def test_durable_command_and_startup_context_without_source_checkout(self):
        self.active()
        self.install("--harness", "claude", "--harness", "codex")
        self.source.rename(self.root / "removed-source")
        self.assertEqual(self.run_command("status")["active"]["profile"], "Claude-Codex")
        printed = subprocess.run([sys.executable, str(self.runtime / "reviewer_guidance.py"), "--print"],
                                 capture_output=True, text=True, timeout=10, env=self.env)
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertIn("Reviewer judgment guidance", printed.stdout)
        self.assertIn("The parent owns repair acceptance", printed.stdout)
        for harness, path, event in (("claude", self.claude, "UserPromptSubmit"),
                                     ("codex", self.codex, "SessionStart")):
            command = json.loads(path.read_text())["hooks"][event][0]["hooks"][0]["command"]
            result = subprocess.run(command, shell=True, input=json.dumps({
                "session_id": "startup-" + harness, "hook_event_name": event,
                "source": "startup", "cwd": str(self.root)}), text=True, capture_output=True,
                timeout=10, env=self.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("active profile Claude-Codex", context)
            self.assertIn("gpt-6-luna", context)

    def test_preserves_other_hooks_exact_backup_and_idempotency(self):
        self.active()
        other = {"permissions": {"allow": ["Read"]}, "hooks": {"SessionStart": [
            {"matcher": "startup", "timeout": 17, "hooks": [{"type": "command", "command": "other-tool hook"}]}]}}
        original = json.dumps(other, separators=(",", ":")).encode()
        self.claude.write_bytes(original)
        self.claude.chmod(0o640)
        first = self.install("--harness", "claude", "--harness", "codex")
        actual = json.loads(self.claude.read_text())
        self.assertEqual(actual["hooks"]["SessionStart"], other["hooks"]["SessionStart"])
        self.assertEqual(actual["permissions"], other["permissions"])
        self.assertEqual(stat.S_IMODE(self.claude.stat().st_mode), 0o640)
        backup = Path(next(x["backup"] for x in first["changed_settings"] if x["harness"] == "claude"))
        self.assertEqual(backup.read_bytes(), original)
        paths = (self.claude, self.codex, self.command, self.runtime / "model_preferences.py",
                 self.runtime / "reviewer_guidance.py",
                 self.runtime / "opencode_dispatch.py", self.runtime / "opencode_adapter.py")
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
        second = self.install("--harness", "claude", "--harness", "codex")
        self.assertEqual(second["changed_settings"], [])
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths])
        self.assertEqual(len(list(self.root.glob("*.model-preferences-backup-*"))), 1)

    def test_hooks_require_active_profile(self):
        error = self.install("--harness", "claude", ok=False)
        self.assertIn("activate a profile", error)
        self.assertFalse(self.command.exists())
        self.assertFalse(self.claude.exists())

    def test_modified_shared_transport_is_not_overwritten(self):
        self.active()
        helper = self.runtime / "cmux_transport.py"
        helper.write_text("modified helper")
        before = (self.runtime / "model_preferences.py").read_bytes()
        error = self.install(ok=False)
        self.assertIn("transport runtime is unowned or modified", error)
        self.assertEqual(helper.read_text(), "modified helper")
        self.assertEqual((self.runtime / "model_preferences.py").read_bytes(), before)

    def test_modified_reviewer_guidance_runtime_is_not_overwritten(self):
        self.active()
        helper = self.runtime / "reviewer_guidance.py"
        helper.write_text("locally modified reviewer guidance")
        before = (self.runtime / "model_preferences.py").read_bytes()
        error = self.install(ok=False)
        self.assertIn("reviewer guidance runtime is unowned or modified", error)
        self.assertEqual(helper.read_text(), "locally modified reviewer guidance")
        self.assertEqual((self.runtime / "model_preferences.py").read_bytes(), before)

    def test_malformed_settings_leave_existing_runtime_and_settings_untouched(self):
        self.active()
        original = self.command.read_bytes()
        self.claude.write_text('{"hooks": []}')
        error = self.install("--harness", "claude", ok=False)
        self.assertIn("Settings/hooks", error)
        self.assertEqual(self.command.read_bytes(), original)
        self.assertEqual(self.claude.read_text(), '{"hooks": []}')

    def test_refuses_unowned_command_and_modified_runtime(self):
        self.bin.mkdir()
        self.command.write_text("#!/bin/sh\necho unrelated\n")
        self.assertIn("not installer-owned", self.install(ok=False))
        self.command.unlink()
        self.install()
        runtime = self.runtime / "model_preferences.py"
        runtime.write_bytes(runtime.read_bytes() + b"\n# local modification\n")
        self.assertIn("unowned or modified", self.install(ok=False))


if __name__ == "__main__":
    unittest.main()
