import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


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
    @staticmethod
    def _write_fake_cmux(path, selectors, identity, current):
        script = (
            f"#!{sys.executable}\n"
            "import json,os,sys\n"
            f"expected={selectors!r}\n"
            "if any(os.environ.get(key) != value for key,value in expected.items()): sys.exit(41)\n"
            "if any(key in os.environ for key in ('CMUX_AGENT_LAUNCH_KIND','CLAUDE_CODE_SESSION_ID',"
            "'CODEX_THREAD_ID','OPENCODE_SESSION_ID')): sys.exit(42)\n"
            f"identity={identity!r}\ncurrent={current!r}\n"
            "print(json.dumps(identity if sys.argv[-1] == 'identify' else current))\n"
        )
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)

    @staticmethod
    def _caller_fixture():
        workspace = "11111111-1111-4111-8111-111111111111"
        surface = "22222222-2222-4222-8222-222222222222"
        session = "33333333-3333-4333-8333-333333333333"
        identity = {"caller": {"surface_type": "terminal", "workspace_id": workspace,
                               "surface_id": surface}}
        current = {"truncated": False, "items": [{
            "lifecycle": "running", "freshness": {"state": "current"},
            "projections": [{"workspace_id": workspace, "panel_id": surface}],
            "agents": [{"kind": "codex", "session_id": session, "state": "working"}],
        }]}
        selectors = {"CMUX_SURFACE_ID": surface, "CMUX_PANEL_ID": surface,
                     "CMUX_WORKSPACE_ID": workspace}
        return workspace, surface, session, identity, current, selectors

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

    def test_unexpected_component_skip_is_unknown(self):
        adapter = doctor.ADAPTER_BY_KEY["hailing-frequencies"]
        normalized = doctor.normalize_component_protocol(adapter, json.dumps({
            "schema_version": doctor.SCHEMA_VERSION,
            "checks": {name: "skipped" for name in doctor.STATUS_NAMES},
        }))
        self.assertEqual(normalized["configuration"], doctor.CheckState.UNKNOWN)
        self.assertEqual(normalized["registration"], doctor.CheckState.UNKNOWN)
        self.assertEqual(normalized["worker"], doctor.CheckState.UNKNOWN)
        self.assertEqual(normalized["intended_delivery"], doctor.CheckState.UNKNOWN)

    def test_orchestrator_checkpoint_is_required_but_implementer_checkpoint_is_not(self):
        adapter = doctor.ADAPTER_BY_KEY["pattern-buffer"]
        states = doctor.no_evidence_states(adapter)
        states["configuration"] = doctor.CheckState.READY
        states["registration"] = doctor.CheckState.READY
        states["worker"] = doctor.CheckState.READY
        states["intended_delivery"] = doctor.CheckState.READY
        states["checkpoint"] = doctor.CheckState.MISSING
        orchestrator = doctor.tool_status(adapter, states, "fixture")
        checkpoint = next(check for check in orchestrator["checks"]
                          if check["name"] == "checkpoint")
        self.assertTrue(checkpoint["required"])
        self.assertEqual(orchestrator["status"], "missing")

        states["checkpoint"] = doctor.CheckState.NOT_APPLICABLE
        implementer = doctor.tool_status(adapter, states, "fixture")
        checkpoint = next(check for check in implementer["checks"]
                          if check["name"] == "checkpoint")
        self.assertFalse(checkpoint["required"])
        self.assertEqual(implementer["status"], "ready")

        states["registration"] = doctor.CheckState.UNKNOWN
        states["checkpoint"] = doctor.CheckState.UNKNOWN
        unresolved = doctor.tool_status(adapter, states, "fixture")
        checkpoint = next(check for check in unresolved["checks"]
                          if check["name"] == "checkpoint")
        self.assertFalse(checkpoint["required"])

    def test_native_observation_gets_terminal_selectors_but_uses_current_snapshot_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cmux = root / "cmux"
            workspace, surface, session, identity, current, selectors = self._caller_fixture()
            self._write_fake_cmux(cmux, {**selectors, "CMUX_SOCKET": str(root / "native.sock")},
                                  identity, current)
            environment = {"PATH": directory, "HOME": directory,
                           "CMUX_SOCKET": str(root / "native.sock"), **selectors,
                           "CMUX_AGENT_LAUNCH_KIND": "claude",
                           "CLAUDE_CODE_SESSION_ID": "44444444-4444-4444-8444-444444444444",
                           "CODEX_THREAD_ID": "55555555-5555-4555-8555-555555555555"}
            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(doctor.shutil, "which", return_value=str(cmux)):
                caller = doctor.discover_caller()
                ordinary = doctor.inspection_environment(native_connection=False)
            self.assertEqual(caller.state, doctor.CheckState.READY)
            self.assertEqual((caller.harness, caller.session, caller.workspace, caller.surface),
                             ("codex", session, workspace, surface))
            self.assertNotIn("CMUX_SURFACE_ID", ordinary)
            self.assertNotIn("CMUX_SOCKET", ordinary)

            absent_selectors = {key: value for key, value in environment.items()
                                if key not in selectors}
            with patch.dict(os.environ, absent_selectors, clear=True), \
                    patch.object(doctor.shutil, "which", return_value=str(cmux)):
                absent = doctor.discover_caller()
            self.assertEqual(absent.state, doctor.CheckState.UNKNOWN)

            conflicting_current = json.loads(json.dumps(current))
            conflicting_current["items"][0]["projections"][0]["panel_id"] = (
                "66666666-6666-4666-8666-666666666666")
            self._write_fake_cmux(cmux, {**selectors, "CMUX_SOCKET": str(root / "native.sock")},
                                  identity, conflicting_current)
            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(doctor.shutil, "which", return_value=str(cmux)):
                conflicting = doctor.discover_caller()
            self.assertEqual(conflicting.state, doctor.CheckState.UNKNOWN)

    def test_public_cli_does_not_treat_unexpected_provider_skip_as_user_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            cmux = bin_dir / "cmux"
            workspace, surface, session, identity, current, selectors = self._caller_fixture()
            self._write_fake_cmux(cmux, {**selectors, "CMUX_SOCKET": str(root / "native.sock")},
                                  identity, current)
            provider = bin_dir / "hailing-frequencies"
            checks = {name: "ready" for name in doctor.STATUS_NAMES}
            checks["observed_delivery"] = "unknown"

            def write_provider(response_checks):
                provider.write_text(
                    f"#!{sys.executable}\nimport json,sys\n"
                    "sys.stdin.buffer.read(16 * 1024 + 1)\n"
                    f"print(json.dumps({{'schema_version':1,'checks':{response_checks!r}}}))\n",
                    encoding="utf-8")
                provider.chmod(0o755)

            skip = [key for key in doctor.expected_components() if key != "hailing-frequencies"]
            command = [sys.executable, str(ROOT / "apps" / "starfleet" / "starfleet.py"),
                       "doctor", "--cwd", directory, "--json"]
            for key in skip:
                command.extend(("--skip", key))
            environment = {"PATH": str(bin_dir), "HOME": directory,
                           "CMUX_SOCKET": str(root / "native.sock"), **selectors,
                           "CMUX_AGENT_LAUNCH_KIND": "claude",
                           "CLAUDE_CODE_SESSION_ID": "44444444-4444-4444-8444-444444444444"}

            write_provider(checks)
            healthy = subprocess.run(command, cwd=directory, env=environment,
                                     capture_output=True, text=True, timeout=5)
            self.assertEqual(healthy.returncode, 0, healthy.stderr)
            healthy_report = json.loads(healthy.stdout)
            self.assertEqual(healthy_report["overall_status"], "ready")
            self.assertEqual(healthy_report["selected_tools"], ["hailing-frequencies"])

            write_provider({name: "skipped" for name in doctor.STATUS_NAMES})
            unexpected_skip = subprocess.run(command, cwd=directory, env=environment,
                                             capture_output=True, text=True, timeout=5)
            self.assertEqual(unexpected_skip.returncode, 4, unexpected_skip.stderr)
            skip_report = json.loads(unexpected_skip.stdout)
            self.assertEqual(skip_report["overall_status"], "unknown")
            selected = next(tool for tool in skip_report["tools"] if tool["selected"])
            self.assertEqual(selected["status"], "unknown")

    def test_public_cli_reports_skipped_dependency_as_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            cmux = bin_dir / "cmux"
            workspace, surface, _session, identity, current, selectors = self._caller_fixture()
            self._write_fake_cmux(cmux, {**selectors, "CMUX_SOCKET": str(root / "native.sock")},
                                  identity, current)
            provider = bin_dir / "warp-drive"
            checks = {name: "ready" for name in doctor.STATUS_NAMES}
            checks["observed_delivery"] = "unknown"
            provider.write_text(
                f"#!{sys.executable}\nimport json,sys\n"
                "sys.stdin.buffer.read(16 * 1024 + 1)\n"
                f"print(json.dumps({{'schema_version':1,'checks':{checks!r}}}))\n",
                encoding="utf-8")
            provider.chmod(0o755)
            skip = [key for key in doctor.expected_components()
                    if key not in ("warp-drive", "hailing-frequencies")]
            command = [sys.executable, str(ROOT / "apps" / "starfleet" / "starfleet.py"),
                       "doctor", "--cwd", directory, "--json"]
            for key in skip:
                command.extend(("--skip", key))
            command.extend(("--skip", "hailing-frequencies"))
            environment = {"PATH": str(bin_dir), "HOME": directory,
                           "CMUX_SOCKET": str(root / "native.sock"), **selectors}
            result = subprocess.run(command, cwd=directory, env=environment,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 3, result.stderr)
            report = json.loads(result.stdout)
            warp = next(tool for tool in report["tools"] if tool["key"] == "warp-drive")
            dependency = next(check for check in warp["checks"]
                              if check["name"] == "dependency:hailing-frequencies")
            self.assertEqual(dependency["status"], "blocked")
            self.assertIn("skipped by user instruction", dependency["summary"])

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
            source = Path(directory) / "source"
            fake_bin = Path(directory) / "misleading-bin"
            outside.mkdir()
            source.mkdir()
            for name in installer.FILES:
                shutil.copyfile(ROOT / "apps" / "starfleet" / name, source / name)
            launcher, runtime = installer.install(prefix, source)
            shutil.rmtree(source)
            fake_bin.mkdir()
            wrong_python = fake_bin / "python3"
            marker = Path(directory) / "wrong-python-used"
            wrong_python.write_text(
                f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n"
                "raise SystemExit(91)\n", encoding="utf-8")
            wrong_python.chmod(0o755)
            before = tree_snapshot(prefix)
            environment = {"PATH": str(fake_bin), "HOME": directory}
            guide = subprocess.run([str(launcher), "guide", "--print"], cwd=outside,
                                    capture_output=True, text=True, check=False,
                                    env=environment)
            self.assertEqual(guide.returncode, 0, guide.stderr)
            self.assertIn("Starfleet orchestrator setup guide", guide.stdout)
            command = [str(launcher), "doctor", "--cwd", str(outside), "--json"]
            for key in doctor.expected_components():
                command.extend(("--skip", key))
            inspected = subprocess.run(command, cwd=outside, capture_output=True, text=True,
                                       check=False, env=environment)
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            self.assertEqual(json.loads(inspected.stdout)["overall_status"], "not_applicable")
            self.assertFalse(marker.exists())
            self.assertEqual(tree_snapshot(prefix), before)
            self.assertTrue(os.access(launcher, os.X_OK))
            self.assertTrue((runtime / "guide.md").is_file())
            self.assertIn(sys.executable, launcher.read_text(encoding="utf-8"))

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
            with self.assertRaisesRegex(RuntimeError, "Python 3.10"):
                installer.ensure_supported_python((3, 9, 18))

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
