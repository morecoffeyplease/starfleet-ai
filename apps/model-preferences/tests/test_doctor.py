import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "model_preferences.py"


def snapshot(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        result[str(path.relative_to(root))] = (
            info.st_mode, info.st_mtime_ns,
            os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None,
        )
    return result


class ReadOnlyDoctorTests(unittest.TestCase):
    def invoke(self, root, args, payload=None):
        config = Path(root) / "config"
        state = Path(root) / "state"
        config.mkdir(exist_ok=True)
        state.mkdir(exist_ok=True)
        env = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
               "HOME": str(root), "XDG_CONFIG_HOME": str(config),
               "XDG_STATE_HOME": str(state), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(SOURCE), "--config-dir", str(config / "starfleet-ai/model-preferences"),
                               "--state-dir", str(state / "starfleet-ai/model-preferences"), *args],
                              input=payload, capture_output=True, text=True, env=env, check=False)

    def test_star_fleet_protocol_reads_bounded_caller_and_creates_no_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("config").mkdir()
            root.joinpath("state").mkdir()
            before = snapshot(root)
            payload = json.dumps({"schema_version": 1, "caller": {
                "harness": "codex", "session": "00000000-0000-4000-8000-000000000001",
                "workspace": "00000000-0000-4000-8000-000000000002",
                "surface": "00000000-0000-4000-8000-000000000003"}})
            result = self.invoke(root, ["doctor", "--starfleet-json"], payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["checks"]["configuration"], "missing")
            self.assertEqual(report["checks"]["registration"], "missing")
            self.assertNotIn("workspace", result.stdout)
            self.assertEqual(snapshot(root), before)

    def test_public_doctor_does_not_wait_for_or_read_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("config").mkdir()
            root.joinpath("state").mkdir()
            before = snapshot(root)
            result = self.invoke(root, ["doctor"])
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["checks"]["configuration"], "missing")
            self.assertEqual(report["checks"]["registration"], "unknown")
            self.assertEqual(snapshot(root), before)

    def test_oversized_protocol_input_is_ignored_without_a_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("config").mkdir()
            root.joinpath("state").mkdir()
            before = snapshot(root)
            result = self.invoke(root, ["doctor", "--starfleet-json"], "x" * 17000)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["checks"]["registration"], "unknown")
            self.assertEqual(snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
