import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "away_mode.py"


def snapshot(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        result[str(path.relative_to(root))] = (
            info.st_mode, info.st_mtime_ns,
            os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None,
        )
    return result


class AwayDoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.caller = {"harness": "codex", "session": "11111111-1111-4111-8111-111111111111",
                       "workspace": "22222222-2222-4222-8222-222222222222",
                       "surface": "33333333-3333-4333-8333-333333333333"}
        self.environment = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                            "HOME": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"}

    def run_cli(self, *args, payload=""):
        return subprocess.run([sys.executable, str(SCRIPT), "--state-dir", str(self.state),
                               *args], cwd=self.root, capture_output=True, text=True,
                              env=self.environment, input=payload, check=False)

    def test_missing_state_does_not_create_state_or_lock(self):
        before = snapshot(self.root)
        payload = json.dumps({"schema_version": 1, "caller": self.caller})
        result = self.run_cli("doctor", "--starfleet-json", payload=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["checks"]["configuration"], "missing")
        self.assertEqual(report["checks"]["registration"], "missing")
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.state / ".lock").exists())

    def test_exact_lane_match_and_wrong_owner_are_redacted_and_read_only(self):
        recipient = {key: self.caller[key] for key in ("harness", "session", "workspace", "surface")}
        (self.state / "state.json").write_text(json.dumps({
            "schema_version": 1, "presence": {"mode": "back", "transition": None},
            "lanes": {"synthetic-owner": {**recipient, "worktree": "/private/example"}},
        }), encoding="utf-8")
        before = snapshot(self.root)
        ready = self.run_cli("doctor", "--starfleet-json",
                             payload=json.dumps({"schema_version": 1, "caller": self.caller}))
        self.assertEqual(ready.returncode, 0, ready.stderr)
        report = json.loads(ready.stdout)
        self.assertEqual(report["checks"]["registration"], "ready")
        self.assertNotIn("private", ready.stdout)
        wrong = {**self.caller, "session": "44444444-4444-4444-8444-444444444444"}
        result = self.run_cli("doctor", "--starfleet-json",
                              payload=json.dumps({"schema_version": 1, "caller": wrong}))
        self.assertEqual(json.loads(result.stdout)["checks"]["registration"], "missing")
        self.assertEqual(snapshot(self.root), before)

    def test_public_doctor_does_not_read_stdin_or_follow_state_symlink(self):
        before = snapshot(self.root)
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["checks"]["configuration"], "missing")
        self.assertEqual(snapshot(self.root), before)
        secret = self.root / "secret.json"
        secret.write_text(json.dumps({"schema_version": 1, "presence": {"mode": "back"}, "lanes": {}}))
        state_path = self.state / "state.json"
        state_path.symlink_to(secret)
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["checks"]["configuration"], "blocked")


if __name__ == "__main__":
    unittest.main()
