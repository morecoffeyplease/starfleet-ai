"""Real CLI reproduction of enrollment when native process inspection is denied."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from _test_support import isolate_state_home

SCRIPT = Path(__file__).with_name("pattern_buffer.py")


class RestrictedEnrollmentTests(unittest.TestCase):
    def setUp(self):
        isolate_state_home(self)

    def test_denied_ps_returns_briefing_without_guessing_or_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "repo"
            root.mkdir()
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
            base = [sys.executable, str(SCRIPT), "--cwd", str(root)]
            subprocess.run([*base, "init", "--default-role", "orchestrator"],
                           check=True, capture_output=True)
            tools = Path(folder) / "restricted-tools"
            tools.mkdir()
            (tools / "git").symlink_to(shutil.which("git"))
            denied = tools / "ps"
            denied.write_text("#!/bin/sh\nexit 1\n")
            denied.chmod(0o600)
            env = {**os.environ, "CODEX_THREAD_ID": "restricted-session", "PATH": str(tools)}
            result = subprocess.run([*base, "checkpoint-enroll", "--harness", "codex"],
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["state"], "briefing")
            self.assertEqual(report["session"], "restricted-session")
            self.assertIsNone(report["native_pid"])
            self.assertIsNone(report["compact_at"])
            self.assertIn("Take stock", report["next_action"])
            self.assertFalse(root.joinpath(".git/context-injector/checkpoints").exists())


if __name__ == "__main__":
    unittest.main()
