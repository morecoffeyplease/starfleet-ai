import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "pattern_buffer.py"
sys.path.insert(0, str(ROOT))
import pattern_buffer_sessions as sessions


def snapshot(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        result[str(path.relative_to(root))] = (
            info.st_mode, info.st_mtime_ns,
            os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None,
        )
    return result


class StarfleetDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".starfleet-pb-diagnostic-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        self.gitdir = self.root / ".git"
        self.home = self.base / "home"
        self.home.mkdir()
        self.settings = self.home / "settings.json"
        self.session = "00000000-0000-4000-8000-000000000042"

    def invoke(self):
        environment = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                       "HOME": str(self.home), "CODEX_HOME": str(self.home),
                       "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([
            sys.executable, str(SCRIPT), "--cwd", str(self.root), "status",
            "--harness", "codex", "--session", self.session,
            "--settings", str(self.settings), "--starfleet-json",
        ], capture_output=True, text=True, env=environment, check=False)

    def test_missing_registry_is_reported_without_creating_state_or_lock(self):
        before = snapshot(self.root)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["checks"]["configuration"], "missing")
        self.assertEqual(report["checks"]["registration"], "missing")
        self.assertFalse((self.gitdir / "context-injector" / "registry.lock").exists())
        self.assertEqual(snapshot(self.root), before)

    def test_existing_registry_lookup_preserves_all_files_and_lock_metadata(self):
        source = self.base / "standing.md"
        source.write_text("# Mission\n\nKeep this synthetic enrollment scoped.\n", encoding="utf-8")
        sessions.enroll(self.root, self.gitdir, "codex", self.session, source, "orchestrator",
                        lambda _text, _path, _role: {"valid": True})
        before = snapshot(self.root)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["checks"]["registration"], "ready")
        self.assertEqual(report["checks"]["worker"], "not_applicable")
        self.assertNotIn(self.session, result.stdout)
        self.assertEqual(snapshot(self.root), before)

    def test_normal_status_behavior_does_not_require_diagnostic_protocol(self):
        command = [sys.executable, str(SCRIPT), "--cwd", str(self.root), "status",
                   "--harness", "codex", "--session", self.session,
                   "--settings", str(self.settings)]
        environment = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                       "HOME": str(self.home), "CODEX_HOME": str(self.home),
                       "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(command, capture_output=True, text=True, env=environment, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"registered": false', result.stdout)
        self.assertNotIn('"schema_version":1', result.stdout)


if __name__ == "__main__":
    unittest.main()
