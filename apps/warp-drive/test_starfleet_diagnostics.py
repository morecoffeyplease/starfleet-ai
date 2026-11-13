"""Synthetic read-only contract tests for Warp-drive's Starfleet adapter."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
import warp_drive


def tree_snapshot(path):
    path = Path(path)
    if not path.exists():
        return None
    result = {}
    for item in sorted(path.rglob("*")):
        info = item.lstat()
        result[str(item.relative_to(path))] = (
            stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
            info.st_size, info.st_mtime_ns,
            item.read_bytes() if stat.S_ISREG(info.st_mode) else None,
        )
    return result


def caller_payload():
    return {
        "schema_version": 1,
        "caller": {
            "harness": "codex",
            "session": str(uuid.uuid4()).upper(),
            "workspace": str(uuid.uuid4()).upper(),
            "surface": str(uuid.uuid4()).upper(),
        },
    }


class WarpDriveDiagnosticTests(unittest.TestCase):
    def test_healthy_local_state_is_read_without_tree_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "warp-state"
            db = warp_drive.connect(state)
            db.close()
            hailing = Path(temporary) / "hailing-state"
            hailing.mkdir(mode=0o700)
            caller = caller_payload()
            (hailing / "config.json").write_text(json.dumps({"version": 1, "targets": []}))
            old_hailing = os.environ.get("WARP_DRIVE_HAILING_STATE")
            os.environ["WARP_DRIVE_HAILING_STATE"] = str(hailing)
            try:
                before = tree_snapshot(state)
                report = warp_drive.warp_drive_diagnostic_report(state, caller["caller"])
                self.assertEqual(report["checks"]["configuration"], "ready")
                self.assertEqual(report["checks"]["worker"], "unknown")
                self.assertEqual(report["checks"]["intended_delivery"], "missing")
                self.assertEqual(tree_snapshot(state), before)
                self.assertFalse((state / "scheduler.lock").exists())
                self.assertFalse((state / "warp-drive.sqlite3-wal").exists())
                self.assertFalse((state / "warp-drive.sqlite3-shm").exists())
            finally:
                if old_hailing is None:
                    os.environ.pop("WARP_DRIVE_HAILING_STATE", None)
                else:
                    os.environ["WARP_DRIVE_HAILING_STATE"] = old_hailing

    def test_missing_malformed_and_sidecar_state_remain_unmodified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing"
            self.assertEqual(
                warp_drive.warp_drive_diagnostic_report(missing)["checks"]["configuration"],
                "missing",
            )
            self.assertFalse(missing.exists())

            state = root / "bad-state"
            state.mkdir(mode=0o700)
            database = state / "warp-drive.sqlite3"
            database.write_text("not a sqlite database")
            os.chmod(database, 0o640)
            before = tree_snapshot(state)
            report = warp_drive.warp_drive_diagnostic_report(state)
            self.assertEqual(report["checks"]["configuration"], "blocked")
            self.assertEqual(tree_snapshot(state), before)

            sidecar = Path(str(database) + "-wal")
            sidecar.write_bytes(b"synthetic wal")
            before = tree_snapshot(state)
            report = warp_drive.warp_drive_diagnostic_report(state)
            self.assertEqual(report["checks"]["configuration"], "unknown")
            self.assertEqual(tree_snapshot(state), before)

    def test_cli_safe_mode_does_not_read_or_create_runtime_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "not-created"
            command = [sys.executable, str(ROOT / "warp_drive.py"), "--state-dir", str(state),
                       "doctor", "--starfleet-json"]
            result = subprocess.run(command, input="{}", capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["checks"]["configuration"], "missing")
            self.assertFalse(state.exists())

    def test_symlinked_database_is_blocked_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.sqlite3"
            target.write_text("private synthetic marker")
            os.chmod(target, 0o640)
            state = root / "state"
            state.mkdir(mode=0o700)
            os.symlink(target, state / "warp-drive.sqlite3")
            before = tree_snapshot(root)
            report = warp_drive.warp_drive_diagnostic_report(state)
            self.assertEqual(report["checks"]["configuration"], "blocked")
            self.assertEqual(tree_snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
