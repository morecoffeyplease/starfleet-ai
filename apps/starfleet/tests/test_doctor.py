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

    @staticmethod
    def _installer_cli_environment(directory, sitecustomize):
        hooks = Path(directory) / "python-hooks"
        hooks.mkdir()
        (hooks / "sitecustomize.py").write_text(sitecustomize, encoding="utf-8")
        environment = os.environ.copy()
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(hooks) + (
            os.pathsep + existing_pythonpath if existing_pythonpath else "")
        environment["HOME"] = str(directory)
        return environment

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
        with patch.object(doctor.shutil, "which", return_value="/synthetic/pattern-buffer"):
            orchestrator = doctor.tool_status(adapter, states, "fixture")
        checkpoint = next(check for check in orchestrator["checks"]
                          if check["name"] == "checkpoint")
        self.assertTrue(checkpoint["required"])
        self.assertEqual(orchestrator["status"], "missing")

        states["checkpoint"] = doctor.CheckState.NOT_APPLICABLE
        with patch.object(doctor.shutil, "which", return_value="/synthetic/pattern-buffer"):
            implementer = doctor.tool_status(adapter, states, "fixture")
        checkpoint = next(check for check in implementer["checks"]
                          if check["name"] == "checkpoint")
        self.assertFalse(checkpoint["required"])
        self.assertEqual(implementer["status"], "ready")

        states["registration"] = doctor.CheckState.UNKNOWN
        states["checkpoint"] = doctor.CheckState.UNKNOWN
        with patch.object(doctor.shutil, "which", return_value="/synthetic/pattern-buffer"):
            unresolved = doctor.tool_status(adapter, states, "fixture")
        checkpoint = next(check for check in unresolved["checks"]
                          if check["name"] == "checkpoint")
        self.assertFalse(checkpoint["required"])

        states["registration"] = doctor.CheckState.READY
        states["checkpoint"] = doctor.CheckState.NOT_APPLICABLE
        with patch.object(doctor.shutil, "which", return_value=None):
            missing_runtime = doctor.tool_status(adapter, states, "fixture")
        runtime = next(check for check in missing_runtime["checks"]
                       if check["name"] == "runtime")
        self.assertEqual(runtime["status"], "missing")
        self.assertEqual(missing_runtime["status"], "missing")

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

    def test_public_cli_uses_policy_guard_native_owner_context_only_when_it_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            cmux = bin_dir / "cmux"
            workspace, surface, session, identity, current, selectors = self._caller_fixture()
            self._write_fake_cmux(cmux, {**selectors, "CMUX_SOCKET": str(root / "native.sock")},
                                  identity, current)
            provider = root / "policy_guard.py"

            def write_status(lane_enrolled, owner_state="matches", omit_owner=False):
                value = {"installed_here": True, "lane_enrolled": lane_enrolled,
                         "attachment_state": "active-here",
                         "managed_adapter": {"state": "intact"},
                         "installed_source": {"integrity": "intact"},
                         "activation": {"state": "unobserved"}}
                if not omit_owner:
                    value["current_owner"] = {"state": owner_state}
                expected_environment = {**selectors, "CMUX_SOCKET": str(root / "native.sock")}
                provider.write_text(
                    f"import json,os,sys\nexpected={expected_environment!r}\n"
                    f"value={value!r}\n"
                    f"expected_owner={{'CMUX_AGENT_LAUNCH_KIND':'codex','CODEX_THREAD_ID':{session!r}}}\n"
                    "if any(os.environ.get(key) != item for key,item in expected.items()): sys.exit(61)\n"
                    "native_keys=('CMUX_AGENT_LAUNCH_KIND','CODEX_THREAD_ID','CLAUDE_CODE_SESSION_ID',"
                    "'OPENCODE_SESSION_ID')\n"
                    "present={key:os.environ[key] for key in native_keys if key in os.environ}\n"
                    "if present and present != expected_owner: sys.exit(62)\n"
                    f"owner_state=({owner_state!r} if present else "
                    f"('unavailable' if {lane_enrolled!r} else 'legacy-per-pr-no-native-owner'))\n"
                    "if 'current_owner' in value: value['current_owner']={'state':owner_state}\n"
                    "print(json.dumps(value))\n", encoding="utf-8")

            skip = [key for key in doctor.expected_components() if key != "policy-guard"]
            command = [sys.executable, str(ROOT / "apps" / "starfleet" / "starfleet.py"),
                       "doctor", "--cwd", directory, "--json", "--policy-guard-cli", str(provider)]
            for key in skip:
                command.extend(("--skip", key))
            environment = {"PATH": str(bin_dir), "HOME": directory,
                           "CMUX_SOCKET": str(root / "native.sock"), **selectors,
                           "CMUX_AGENT_LAUNCH_KIND": "codex",
                           "CODEX_THREAD_ID": session,
                           "OPENCODE_SESSION_ID": "ses_syntheticuntrusted"}

            write_status(True)
            healthy = subprocess.run(command, cwd=directory, env=environment,
                                     capture_output=True, text=True, timeout=5)
            self.assertEqual(healthy.returncode, 4, healthy.stderr)
            healthy_report = json.loads(healthy.stdout)
            self.assertEqual(healthy_report["overall_status"], "unknown")
            policy_guard = next(tool for tool in healthy_report["tools"]
                                if tool["key"] == "policy-guard")
            registration = next(check for check in policy_guard["checks"]
                                if check["name"] == "registration")
            self.assertEqual(registration["status"], "ready")
            self.assertTrue(registration["required"])
            intended = next(check for check in policy_guard["checks"]
                            if check["name"] == "intended_delivery")
            self.assertEqual(intended["status"], "unknown")

            write_status(True, "mismatch")
            mismatch = subprocess.run(command, cwd=directory, env=environment,
                                      capture_output=True, text=True, timeout=5)
            self.assertEqual(mismatch.returncode, 3, mismatch.stderr)
            mismatch_report = json.loads(mismatch.stdout)
            self.assertEqual(mismatch_report["overall_status"], "blocked")
            policy_guard = next(tool for tool in mismatch_report["tools"]
                                if tool["key"] == "policy-guard")
            registration = next(check for check in policy_guard["checks"]
                                if check["name"] == "registration")
            self.assertEqual(registration["status"], "blocked")
            self.assertTrue(registration["required"])

            write_status(True, "unavailable")
            unavailable = subprocess.run(command, cwd=directory, env=environment,
                                         capture_output=True, text=True, timeout=5)
            self.assertEqual(unavailable.returncode, 4, unavailable.stderr)
            unavailable_report = json.loads(unavailable.stdout)
            self.assertEqual(unavailable_report["overall_status"], "unknown")

            write_status(True, omit_owner=True)
            absent_owner = subprocess.run(command, cwd=directory, env=environment,
                                          capture_output=True, text=True, timeout=5)
            self.assertEqual(absent_owner.returncode, 4, absent_owner.stderr)
            self.assertEqual(json.loads(absent_owner.stdout)["overall_status"], "unknown")

            write_status(False, "legacy-per-pr-no-native-owner")
            standalone = subprocess.run(command, cwd=directory, env=environment,
                                        capture_output=True, text=True, timeout=5)
            self.assertEqual(standalone.returncode, 4, standalone.stderr)
            standalone_report = json.loads(standalone.stdout)
            policy_guard = next(tool for tool in standalone_report["tools"]
                                if tool["key"] == "policy-guard")
            registration = next(check for check in policy_guard["checks"]
                                if check["name"] == "registration")
            self.assertEqual(registration["status"], "not_applicable")
            self.assertFalse(registration["required"])

            for conflicting in (
                    {key: value for key, value in environment.items()
                     if key != "CMUX_AGENT_LAUNCH_KIND"},
                    {**environment, "CODEX_THREAD_ID": "44444444-4444-4444-8444-444444444444"},
                    {**environment, "CMUX_AGENT_LAUNCH_KIND": "claude",
                     "CLAUDE_CODE_SESSION_ID": "55555555-5555-4555-8555-555555555555"}):
                write_status(True)
                result = subprocess.run(command, cwd=directory, env=conflicting,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 4, result.stderr)
                result_report = json.loads(result.stdout)
                policy_guard = next(tool for tool in result_report["tools"]
                                    if tool["key"] == "policy-guard")
                configuration = next(check for check in policy_guard["checks"]
                                     if check["name"] == "configuration")
                self.assertEqual(configuration["status"], "ready")
                registration = next(check for check in policy_guard["checks"]
                                    if check["name"] == "registration")
                self.assertEqual(registration["status"], "unknown")

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

    def test_installer_cli_publishes_writable_runtime_before_sealing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / "prefix"
            loaded = root / "contract-loaded"
            rejected = root / "sealed-directory-rename-rejected"
            sitecustomize = (
                "import os, stat\nfrom pathlib import Path\n"
                f"Path({str(loaded)!r}).write_text('loaded', encoding='utf-8')\n"
                "_real_replace = os.replace\n"
                "def _guarded_replace(source, destination, *args, **kwargs):\n"
                "    info = os.lstat(source)\n"
                "    if stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o555:\n"
                f"        Path({str(rejected)!r}).write_text('rejected', encoding='utf-8')\n"
                "        raise OSError('injected contract rejects sealed-directory rename')\n"
                "    return _real_replace(source, destination, *args, **kwargs)\n"
                "os.replace = _guarded_replace\n")
            environment = self._installer_cli_environment(root, sitecustomize)
            command = [sys.executable, str(ROOT / "apps" / "starfleet" / "install.py"),
                       "--prefix", str(prefix)]
            result = subprocess.run(command, cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(loaded.is_file())
            self.assertFalse(rejected.exists())

            source = ROOT / "apps" / "starfleet"
            digest = installer.runtime_digest(source)
            runtime = prefix / "share" / "starfleet-ai" / "starfleet" / digest
            launcher = prefix / "bin" / "starfleet"
            self.assertEqual(runtime.stat().st_mode & 0o777, 0o555)
            for name in installer.FILES:
                installed = runtime / name
                self.assertEqual(installed.stat().st_mode & 0o777, 0o444)
                self.assertEqual(installed.read_bytes(), (source / name).read_bytes())
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
            self.assertIn(installer.LAUNCHER_MARKER, launcher.read_text(encoding="utf-8"))
            self.assertFalse(list(runtime.parent.glob(".starfleet-*")))

    def test_installer_cli_seal_failure_rolls_back_runtime_and_preserves_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / "prefix"
            bin_dir = prefix / "bin"
            bin_dir.mkdir(parents=True)
            launcher = bin_dir / "starfleet"
            original_launcher = ("#!/bin/sh\n" + installer.LAUNCHER_MARKER +
                                "\nexec /synthetic/previous-starfleet\n")
            launcher.write_text(original_launcher, encoding="utf-8")
            launcher.chmod(0o755)
            launcher_mode = launcher.stat().st_mode & 0o777

            source = ROOT / "apps" / "starfleet"
            digest = installer.runtime_digest(source)
            runtime_root = prefix / "share" / "starfleet-ai" / "starfleet"
            runtime = runtime_root / digest
            seal_attempted = root / "seal-attempted"
            sitecustomize = (
                "import os\nfrom pathlib import Path\n"
                "_real_chmod = os.chmod\n"
                "def _fail_runtime_seal(path, mode, *args, **kwargs):\n"
                f"    if Path(path) == Path({str(runtime)!r}) and mode == 0o555:\n"
                f"        Path({str(seal_attempted)!r}).write_text('attempted', encoding='utf-8')\n"
                "        raise OSError('injected runtime seal failure')\n"
                "    return _real_chmod(path, mode, *args, **kwargs)\n"
                "os.chmod = _fail_runtime_seal\n")
            environment = self._installer_cli_environment(root, sitecustomize)
            command = [sys.executable, str(ROOT / "apps" / "starfleet" / "install.py"),
                       "--prefix", str(prefix)]
            result = subprocess.run(command, cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(seal_attempted.is_file())
            self.assertFalse(runtime.exists())
            self.assertFalse(list(runtime_root.glob(".starfleet-*")))
            self.assertEqual(launcher.read_text(encoding="utf-8"), original_launcher)
            self.assertEqual(launcher.stat().st_mode & 0o777, launcher_mode)
            self.assertEqual([path.name for path in bin_dir.iterdir()], ["starfleet"])

    def test_installer_cleanup_preserves_a_replaced_runtime_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / "prefix"
            bin_dir = prefix / "bin"
            bin_dir.mkdir(parents=True)
            launcher = bin_dir / "starfleet"
            original_launcher = ("#!/bin/sh\n" + installer.LAUNCHER_MARKER +
                                "\nexec /synthetic/previous-starfleet\n")
            launcher.write_text(original_launcher, encoding="utf-8")
            launcher.chmod(0o755)

            source = ROOT / "apps" / "starfleet"
            digest = installer.runtime_digest(source)
            runtime_root = prefix / "share" / "starfleet-ai" / "starfleet"
            runtime = runtime_root / digest
            displaced = root / "displaced-installer-runtime"
            replacement = root / "replacement-created"
            sitecustomize = (
                "import os\nfrom pathlib import Path\n"
                "_real_chmod = os.chmod\n_real_replace = os.replace\n"
                "def _replace_runtime_before_seal(path, mode, *args, **kwargs):\n"
                f"    if Path(path) == Path({str(runtime)!r}) and mode == 0o555:\n"
                f"        _real_replace(path, {str(displaced)!r})\n"
                "        Path(path).mkdir()\n"
                f"        (Path(path) / 'unrelated.txt').write_text('preserve', encoding='utf-8')\n"
                f"        Path({str(replacement)!r}).write_text('replaced', encoding='utf-8')\n"
                "        raise OSError('injected runtime replacement before seal')\n"
                "    return _real_chmod(path, mode, *args, **kwargs)\n"
                "os.chmod = _replace_runtime_before_seal\n")
            environment = self._installer_cli_environment(root, sitecustomize)
            command = [sys.executable, str(ROOT / "apps" / "starfleet" / "install.py"),
                       "--prefix", str(prefix)]
            result = subprocess.run(command, cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(replacement.is_file())
            self.assertTrue(displaced.is_dir())
            self.assertEqual((runtime / "unrelated.txt").read_text(encoding="utf-8"), "preserve")
            self.assertEqual(launcher.read_text(encoding="utf-8"), original_launcher)
            self.assertFalse(list(bin_dir.glob(".starfleet-*")))

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
