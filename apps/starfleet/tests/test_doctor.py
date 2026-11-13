import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


doctor = load_module("starfleet_doctor_under_test", ROOT / "apps" / "starfleet" / "starfleet.py")
installer = load_module("starfleet_installer_under_test", ROOT / "apps" / "starfleet" / "install.py")


def tree_snapshot(root):
    snapshot = {}
    for path in sorted(Path(root).rglob("*")):
        relative = str(path.relative_to(root))
        info = path.lstat()
        entry = ("link" if path.is_symlink() else "dir" if path.is_dir() else "file",
                 info.st_mode, info.st_mtime_ns)
        if path.is_symlink():
            entry += (os.readlink(path),)
        elif path.is_file():
            entry += (path.read_bytes(),)
        snapshot[relative] = entry
    return snapshot


class DoctorTests(unittest.TestCase):
    def test_caller_requires_one_exact_fresh_current_agent_on_the_surface(self):
        workspace = "11111111-1111-4111-8111-111111111111"
        surface = "22222222-2222-4222-8222-222222222222"
        value = {
            "workspace_id": workspace,
            "surface_id": surface,
            "items": [{
                "lifecycle": "running",
                "freshness": {"state": "current"},
                "projections": [{"workspace_id": workspace, "panel_id": surface}],
                "agents": [{"kind": "codex", "session_id": "33333333-3333-4333-8333-333333333333",
                            "state": "working"}],
            }],
        }
        caller = doctor.validate_caller_value(value)
        self.assertEqual(caller.state, doctor.CheckState.READY)
        self.assertEqual(caller.harness, "codex")
        self.assertNotIn("session", caller.public())
        value["items"][0]["freshness"] = None
        with self.assertRaises(ValueError):
            doctor.validate_caller_value(value)

    def test_protocol_is_reduced_to_allowed_states(self):
        adapter = doctor.ADAPTER_BY_KEY["hailing-frequencies"]
        normalized = doctor.normalize_component_protocol(adapter, json.dumps({
            "schema_version": doctor.SCHEMA_VERSION,
            "checks": {"configuration": "ready", "registration": "ready",
                       "worker": "ready", "intended_delivery": "ready",
                       "observed_delivery": "ready", "private_path": "/secret"},
            "private_content": "must not escape",
        }))
        self.assertEqual(normalized["configuration"], doctor.CheckState.READY)
        self.assertEqual(normalized["observed_delivery"], doctor.CheckState.UNKNOWN)
        self.assertNotIn("private_path", normalized)

    def test_invalid_oversized_and_slow_subprocess_output_is_suppressed(self):
        invalid = doctor.run_bounded(
            [sys.executable, "-c", "import os; os.write(1, b'\\xff')"], None, None, 128, 2)
        oversized = doctor.run_bounded(
            [sys.executable, "-c", "print('x' * 2048)"], None, None, 64, 2)
        slow = doctor.run_bounded(
            [sys.executable, "-c", "import time; time.sleep(2)"], None, None, 128, 0.05)
        self.assertEqual(invalid, ("malformed", b"", 0))
        self.assertEqual(oversized[0:2], ("oversized", b""))
        self.assertEqual(slow[0:2], ("timeout", b""))

    def test_all_tools_skipped_is_not_applicable_and_does_not_probe_cmux(self):
        with tempfile.TemporaryDirectory() as directory:
            report = doctor.build_report(doctor.expected_components(), Path(directory))
            self.assertEqual(report["overall_status"], "not_applicable")
            self.assertEqual(report["exit_code"], 0)
            self.assertEqual(report["selected_tools"], [])
            self.assertEqual(len(report["skipped_tools"]), len(doctor.ADAPTERS))
            self.assertTrue(all(tool["status"] == "skipped" for tool in report["tools"]))

    def test_installed_guide_works_outside_source_and_reads_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "prefix"
            outside = Path(directory) / "outside"
            outside.mkdir()
            launcher, runtime = installer.install(prefix)
            before = tree_snapshot(prefix)
            environment = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                           "HOME": directory}
            result = subprocess.run([str(launcher), "guide", "--print"], cwd=outside,
                                    capture_output=True, text=True, check=False,
                                    env=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Starfleet orchestrator setup guide", result.stdout)
            self.assertEqual(tree_snapshot(prefix), before)
            self.assertTrue(os.access(launcher, os.X_OK))
            self.assertTrue((runtime / "guide.md").is_file())

    def test_installer_preserves_unowned_launcher_and_refuses_unsealed_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "prefix"
            bin_dir = prefix / "bin"
            bin_dir.mkdir(parents=True)
            launcher = bin_dir / "starfleet"
            launcher.write_text("#!/bin/sh\nprivate existing command\n", encoding="utf-8")
            launcher.chmod(0o755)
            before = tree_snapshot(prefix)
            with self.assertRaises(ValueError):
                installer.install(prefix)
            self.assertEqual(tree_snapshot(prefix), before)

            owned_prefix = Path(directory) / "owned-prefix"
            owned_launcher, runtime = installer.install(owned_prefix)
            compromised_file = runtime / "guide.md"
            compromised_file.chmod(0o644)
            before = tree_snapshot(owned_prefix)
            with self.assertRaises(ValueError):
                installer.install(owned_prefix)
            self.assertEqual(tree_snapshot(owned_prefix), before)
            self.assertTrue(owned_launcher.is_file())

    def test_doctor_cli_is_read_only_for_an_all_skipped_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory) / "worktree"
            cwd.mkdir()
            before = tree_snapshot(directory)
            command = [sys.executable, str(ROOT / "apps" / "starfleet" / "starfleet.py"),
                       "doctor", "--cwd", str(cwd), "--json"]
            for key in doctor.expected_components():
                command.extend(("--skip", key))
            result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                    env={"PATH": os.defpath, "HOME": directory})
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["overall_status"], "not_applicable")
            self.assertEqual(report["selected_tools"], [])
            self.assertEqual(tree_snapshot(directory), before)


if __name__ == "__main__":
    unittest.main()
