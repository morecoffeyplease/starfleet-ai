"""Exact-owner reconciliation for interrupted Codex launches."""
import contextlib
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import test_preferences as fixtures
from test_preferences import mp

_REAL_POPEN = subprocess.Popen


class LaunchRecoveryTest(unittest.TestCase):
    setUp = fixtures.PreferencesTest.setUp
    args = fixtures.PreferencesTest.args
    call = fixtures.PreferencesTest.call
    activate = fixtures.PreferencesTest.activate
    run_cli = fixtures.PreferencesTest.run_cli

    def owner(self):
        return {"harness": "codex", "session": "owner-session",
                "workspace": str(uuid.uuid4()), "surface": str(uuid.uuid4())}

    def launch(self, *, provenance=True, context_injector=None):
        self.activate("Codex-only")
        choice = self.call("resolve", "--role", "implementer")
        launch_id = uuid.uuid4().hex
        session = str(uuid.uuid4())
        identity = self.owner()
        record = {"id": launch_id, "choice": choice, "cwd": str(self.root),
                  "created_at": mp.now(), "context_injector": context_injector,
                  "slot": None, "task_digest": hashlib.sha256(b"original brief").hexdigest(),
                  "status": "starting", "pattern_buffer_task": {"status": "none"}}
        if provenance:
            record["recovery_provenance"] = {
                "version": 1,
                "owner": {"status": "verified", "identity": identity},
                "dispatcher": {"pid": 10001, "birth": "dispatcher-start", "status": "observed"},
                "child": {"pid": 10002, "birth": "child-start", "status": "observed"},
            }
        self.app.save_state(f"launches/{launch_id}", record)
        mp.bind_native(self.app, launch_id, session)
        return self.app.state(f"launches/{launch_id}", None), identity

    def inventory(self, launch, identity=None, child_state="ended", child_lifecycle="ended", freshness="current"):
        provenance = launch.get("recovery_provenance", {})
        owner = provenance.get("owner", {})
        identity = identity or owner.get("identity") or self.owner()
        other_surface = str(uuid.uuid4())
        return {"truncated": False, "items": [
            {"lifecycle": "running", "freshness": {"state": "current"},
             "projections": [{"workspace_id": identity["workspace"], "panel_id": identity["surface"]}],
             "agents": [{"kind": identity["harness"], "session_id": identity["session"], "state": "idle"}]},
            {"lifecycle": child_lifecycle, "freshness": {"state": freshness},
             "projections": [{"workspace_id": str(uuid.uuid4()), "panel_id": other_surface}],
             "agents": [{"kind": "codex", "session_id": launch.get("native_session"), "state": child_state}]},
        ]}

    @contextlib.contextmanager
    def stopped_owner(self, launch, identity=None, child_state="ended", freshness="current"):
        identity = identity or launch["recovery_provenance"]["owner"]["identity"]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(mp.App, "caller", return_value=identity))
            stack.enter_context(patch.object(mp.App, "current", return_value=self.inventory(
                launch, identity, child_state=child_state, freshness=freshness)))
            stack.enter_context(patch.object(mp, "observe_process", return_value="stopped"))
            yield

    def pb_task(self, launch, status):
        task_id = uuid.uuid4().hex
        adapter = {"script": str(self.root / "pattern_buffer.py"),
                   "owner_worktree": str(self.root),
                   "receipt": str(self.root / f"receipt-{task_id}.json")}
        receipt = {"version": 1, "receipt_type": "immutable_dispatch_snapshot",
                   "task_id": task_id, "owner_worktree": str(self.root),
                   "worktree": launch["cwd"], "harness": "codex", "session_id": None,
                   "brief_sha256": launch["task_digest"], "created_at": 1, "status": "pending"}
        raw = json.dumps(receipt, sort_keys=True).encode()
        Path(adapter["receipt"]).write_bytes(raw)
        launch["context_injector"] = adapter
        launch["pattern_buffer_task"] = {"status": "linked", "task_id": task_id,
                                         "receipt_sha256": hashlib.sha256(raw).hexdigest(),
                                         "initial_brief_sha256": launch["task_digest"]}
        self.app.save_state(f"launches/{launch['id']}", launch)
        task = {"task_id": task_id, "root": launch["cwd"], "harness": "codex",
                "owner": {"root": str(self.root), "gitdir": str(self.root / ".git")},
                "status": status,
                "session_id": launch["native_session"] if status in ("active", "finished") else None,
                "brief_sha256": launch["task_digest"]}
        return launch, task

    def reconcile(self, launch, disposition="interrupted", key=None):
        return self.call("launch-reconcile", launch["id"], "--disposition", disposition,
                         "--request-key", key or str(uuid.uuid4()))

    def test_launch_process_probe_distinguishes_absent_reused_and_unknown(self):
        record = {"pid": 54321, "birth": "saved-start"}
        with patch.object(mp.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["ps"], 0, "different-start\n", "")) as ps:
            observed = mp.observe_process(record)
            ps.assert_called_once()
            self.assertEqual(observed, "pid_reused", ps.call_args)
        with patch.object(mp.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["ps"], 1, "", "")), patch.object(mp.os, "kill", side_effect=ProcessLookupError):
            self.assertEqual(mp.observe_process(record), "stopped")
        with patch.object(mp.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["ps"], 1, "", "")), patch.object(mp.os, "kill", side_effect=PermissionError):
            self.assertEqual(mp.observe_process(record), "unknown")
        self.assertEqual(mp.observe_process({"pid": 54321, "birth": None}), "unknown")

    def test_legacy_starting_launch_is_inspect_only_even_for_present_caller(self):
        launch, _ = self.launch(provenance=False)
        before = self.app.state(f"launches/{launch['id']}", None)
        with patch.object(mp.App, "caller", return_value=self.owner()), \
                patch.object(mp.App, "current", return_value=self.inventory(launch)), \
                patch.object(mp, "observe_process", return_value="stopped"):
            inspected = self.call("launch-inspect", launch["id"])
            self.assertEqual(inspected["evidence"]["owner"], "unavailable")
            with self.assertRaisesRegex(mp.PreferenceError, "exact recorded launch owner"):
                self.reconcile(launch)
        self.assertEqual(self.app.state(f"launches/{launch['id']}", None), before)

    def test_codex_dispatch_continues_when_optional_owner_capture_fails(self):
        self.activate("Codex-only")
        brief = self.root / "brief"
        brief.write_text("Ordinary dispatch remains available.\n")
        completed = subprocess.CompletedProcess(["codex"], 0)
        with patch.object(mp.App, "caller", side_effect=mp.PreferenceError("no current caller")), \
                patch.object(mp.shutil, "which", return_value="codex"), \
                patch.object(mp, "process_birth_identity", return_value=None), \
                patch.object(mp, "stream_codex", return_value=completed):
            result = self.call("dispatch", "--role", "implementer", "--cwd", str(self.root),
                               "--task-file", str(brief))
        saved = self.app.state(f"launches/{result['launch']}", None)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(saved["status"], "exited")
        self.assertEqual(saved["recovery_provenance"]["owner"]["status"], "unavailable")

    def test_wrong_owner_live_reused_stale_and_missing_native_refuse_without_writes(self):
        launch, identity = self.launch()
        before = self.app.state(f"launches/{launch['id']}", None)
        cases = [
            ("wrong owner", identity | {"session": "different-owner"}, "stopped", "current", "exact recorded launch owner"),
            ("live process", identity, "live", "current", "process stop is not proven"),
            ("reused pid", identity, "pid_reused", "current", "process stop is not proven"),
            ("stale native", identity, "stopped", "stale", "fresh exact native-session"),
        ]
        for name, caller, process_state, freshness, message in cases:
            with self.subTest(name=name), patch.object(mp.App, "caller", return_value=caller), \
                    patch.object(mp.App, "current", return_value=self.inventory(launch, identity, freshness=freshness)), \
                    patch.object(mp, "observe_process", return_value=process_state):
                with self.assertRaisesRegex(mp.PreferenceError, message):
                    self.reconcile(launch)
                self.assertEqual(self.app.state(f"launches/{launch['id']}", None), before)
        with patch.object(mp.App, "caller", return_value=identity), \
                patch.object(mp.App, "current", return_value=self.inventory(
                    launch, identity, child_state="ended", child_lifecycle="running")), \
                patch.object(mp, "observe_process", return_value="stopped"):
            with self.assertRaisesRegex(mp.PreferenceError, "fresh exact native-session"):
                self.reconcile(launch)
        self.assertEqual(self.app.state(f"launches/{launch['id']}", None), before)
        with patch.object(mp.App, "caller", return_value=identity), \
                patch.object(mp.App, "current", return_value={"truncated": False, "items": []}), \
                patch.object(mp, "observe_process", return_value="stopped"):
            inspected = self.call("launch-inspect", launch["id"])
            self.assertEqual(inspected["evidence"]["native_session"], "missing")
            self.assertFalse(inspected["stopped_state_proven"])
            with self.assertRaisesRegex(mp.PreferenceError, "fresh exact native-session"):
                self.reconcile(launch)
        self.assertEqual(self.app.state(f"launches/{launch['id']}", None), before)

    def test_partial_native_binding_refuses_reconciliation(self):
        launch, identity = self.launch()
        key = mp.digest({"harness": "codex", "session": launch["native_session"]})
        (self.state_dir / f"bindings/{key}.json").unlink()
        with self.stopped_owner(launch, identity):
            inspected = self.call("launch-inspect", launch["id"])
            self.assertEqual(inspected["evidence"]["native_binding"], "mismatch")
            with self.assertRaisesRegex(mp.PreferenceError, "Native launch binding"):
                self.reconcile(launch)
        self.assertEqual(self.app.state(f"launches/{launch['id']}", None)["status"], "starting")

    def test_no_native_inventory_and_missing_process_provenance_are_not_stopped(self):
        launch, identity = self.launch()
        launch["recovery_provenance"]["child"] = None
        self.app.save_state(f"launches/{launch['id']}", launch)
        with self.stopped_owner(launch, identity), patch.object(
                mp, "observe_process", side_effect=lambda record: "unknown" if record is None else "stopped"):
            inspected = self.call("launch-inspect", launch["id"])
            self.assertEqual(inspected["evidence"]["child_process"], "unknown")
            with self.assertRaisesRegex(mp.PreferenceError, "process stop is not proven"):
                self.reconcile(launch)

    def test_interrupted_reconciliation_is_atomic_idempotent_and_keeps_exit_unknown(self):
        launch, identity = self.launch()
        key = str(uuid.uuid4())
        with self.stopped_owner(launch, identity):
            first = self.reconcile(launch, key=key)
            second = self.reconcile(launch, key=key)
        self.assertEqual(first["status"], "interrupted")
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        saved = self.app.state(f"launches/{launch['id']}", None)
        self.assertEqual(saved["status"], "interrupted")
        self.assertNotIn("exit_code", saved)
        self.assertNotIn("finished_at", saved)
        self.assertEqual(saved["native_session"], launch["native_session"])
        self.assertEqual(saved["choice"], launch["choice"])
        self.assertEqual(len(saved["recovery"]["history"]), 1)
        self.assertEqual(saved["recovery"]["history"][0]["native_exit_code"], "unknown")
        with self.assertRaisesRegex(mp.PreferenceError, "different disposition"):
            with self.stopped_owner(launch, identity):
                self.reconcile(launch, disposition="abandoned", key=key)

    def test_concurrent_same_request_key_records_one_history_entry(self):
        launch, identity = self.launch()
        key = str(uuid.uuid4())
        def invoke():
            return self.reconcile(launch, key=key)
        with self.stopped_owner(launch, identity), concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: invoke(), range(2)))
        self.assertEqual([result["status"] for result in results], ["interrupted", "interrupted"])
        saved = self.app.state(f"launches/{launch['id']}", None)
        self.assertEqual(len(saved["recovery"]["history"]), 1)

    def test_pending_pattern_buffer_task_prints_supported_owner_commands_and_holds(self):
        launch, identity = self.launch()
        launch, task = self.pb_task(launch, "pending")
        before = self.app.state(f"launches/{launch['id']}", None)
        with self.stopped_owner(launch, identity), patch.object(mp.App, "run_json", return_value=task):
            inspected = self.call("launch-inspect", launch["id"])
            self.assertEqual(inspected["evidence"]["pattern_buffer_task"], "pending_unbound")
            commands = inspected["next_commands"]
            self.assertTrue(any("finish-task" in command for command in commands))
            self.assertTrue(any("task-status" in command for command in commands))
            self.assertFalse(any("launch-reconcile" in command for command in commands))
            with self.assertRaisesRegex(mp.PreferenceError, "active task bound"):
                self.reconcile(launch)
            with self.assertRaisesRegex(mp.PreferenceError, "Finish the linked Pattern Buffer task"):
                self.reconcile(launch, disposition="abandoned")
        self.assertEqual(self.app.state(f"launches/{launch['id']}", None), before)

    def test_active_pattern_buffer_task_can_continue_then_resume_checks_again(self):
        launch, identity = self.launch()
        launch, task = self.pb_task(launch, "active")
        task_owner = task["owner"]
        with self.stopped_owner(launch, identity), patch.object(mp.App, "run_json", return_value=task):
            result = self.reconcile(launch)
        self.assertEqual(result["status"], "interrupted")
        followup = self.root / "follow-up.txt"
        followup.write_text("Continue the existing authorized task.\n")
        current_task = dict(task)
        def pattern_buffer(argv, stdin=None):
            if "task-status" in argv:
                return dict(current_task)
            if "status" in argv:
                return {"registered": True, "enabled": True, "owned_hook_handlers": 1,
                        "context_owner": None}
            if "update-task" in argv:
                task_file = Path(argv[argv.index("--task-file") + 1])
                current_task["brief_sha256"] = hashlib.sha256(task_file.read_bytes()).hexdigest()
                current_task["status"] = "active"
                return {"session_id": launch["native_session"], "owner": task_owner,
                        "brief_sha256": current_task["brief_sha256"]}
            self.fail("Unexpected Pattern Buffer command")
        with self.stopped_owner(launch, identity), patch.object(mp.App, "run_json", side_effect=pattern_buffer), \
                patch.object(mp, "stream_codex", return_value=subprocess.CompletedProcess([], 0)) as child:
            result = self.call("resume", launch["id"], "--task-file", str(followup))
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(current_task["status"], "active")
        child.assert_called_once()
        saved_launch = self.app.state(f"launches/{launch['id']}", None)
        self.assertEqual(saved_launch["status"], "interrupted")
        self.assertNotIn("exit_code", saved_launch)

    def test_finished_pattern_buffer_task_can_only_be_abandoned(self):
        launch, identity = self.launch()
        launch, task = self.pb_task(launch, "finished")
        with self.stopped_owner(launch, identity), patch.object(mp.App, "run_json", return_value=task):
            result = self.reconcile(launch, disposition="abandoned")
        self.assertEqual(result["status"], "abandoned")
        followup = self.root / "follow-up.txt"
        followup.write_text("Do not resume a finished task.\n")
        with self.stopped_owner(launch, identity), patch.object(mp.App, "run_json", return_value=task), \
                patch.object(mp, "stream_codex") as child, self.assertRaisesRegex(mp.PreferenceError, "not exited"):
            self.call("resume", launch["id"], "--task-file", str(followup))
        child.assert_not_called()
        self.assertEqual(self.app.state(f"launches/{launch['id']}", None)["status"], "abandoned")

    def test_interrupted_launch_with_finished_task_cannot_resume(self):
        launch, identity = self.launch()
        launch, task = self.pb_task(launch, "finished")
        launch["status"] = "interrupted"
        launch["recovery"] = {"version": 1, "history": [{
            "request_key": str(uuid.uuid4()), "disposition": "interrupted", "observed_at": mp.now(),
            "owner": "matched", "dispatcher_process": "stopped", "child_process": "stopped",
            "native_session": "ended", "native_binding": "verified",
            "pattern_buffer_task": "active_exact", "native_exit_code": "unknown"}]}
        self.app.save_state(f"launches/{launch['id']}", launch)
        followup = self.root / "follow-up.txt"
        followup.write_text("Do not continue a finished task.\n")
        with self.stopped_owner(launch, identity), patch.object(mp.App, "run_json", return_value=task), \
                patch.object(mp, "stream_codex") as child, self.assertRaisesRegex(mp.PreferenceError, "not active"):
            self.call("resume", launch["id"], "--task-file", str(followup))
        child.assert_not_called()

    def test_public_cli_interrupt_reconcile_and_resume_with_fake_native_processes(self):
        self.activate("Codex-only")
        owner = self.owner()
        native_session = str(uuid.uuid4())
        provider_bin = self.root / "fake-bin"
        provider_bin.mkdir()
        provider = provider_bin / "codex"
        provider.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            "argv = sys.argv[1:]\n"
            "if 'resume' in argv:\n"
            "    sys.stdin.read()\n"
            "    print(json.dumps({'type': 'thread.started', 'thread_id': argv[argv.index('resume') + 1]}), flush=True)\n"
            "else:\n"
            "    print(json.dumps({'type': 'thread.started', 'thread_id': os.environ['FAKE_NATIVE_SESSION']}), flush=True)\n"
            "    time.sleep(60)\n")
        provider.chmod(0o755)
        cmux = provider_bin / "cmux"
        cmux.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"owner = {owner!r}\n"
            "args = sys.argv[1:]\n"
            "if 'identify' in args:\n"
            "    print(json.dumps({'caller': {'surface_type': 'terminal', 'workspace_id': owner['workspace'], 'surface_id': owner['surface']}}))\n"
            "else:\n"
            "    parent = {'lifecycle': 'running', 'freshness': {'state': 'current'}, 'projections': [{'workspace_id': owner['workspace'], 'panel_id': owner['surface']}], 'agents': [{'kind': owner['harness'], 'session_id': owner['session'], 'state': 'idle'}]}\n"
            "    child = {'lifecycle': 'ended', 'freshness': {'state': 'current'}, 'projections': [], 'agents': [{'kind': 'codex', 'session_id': os.environ['FAKE_NATIVE_SESSION'], 'state': 'ended'}]}\n"
            "    print(json.dumps({'truncated': False, 'items': [parent, child]}))\n")
        cmux.chmod(0o755)
        initial = self.root / "initial.txt"
        initial.write_text("A bounded synthetic task.\n")
        followup = self.root / "follow-up.txt"
        followup.write_text("Continue the same task after inspection.\n")
        env = dict(os.environ, PATH=str(provider_bin) + os.pathsep + os.environ.get("PATH", ""),
                   FAKE_NATIVE_SESSION=native_session)
        command = [sys.executable, str(fixtures.SCRIPT), "--config-dir", str(self.config_dir),
                   "--state-dir", str(self.state_dir), "dispatch", "--role", "implementer",
                   "--cwd", str(self.root), "--task-file", str(initial)]
        process = _REAL_POPEN(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=env, start_new_session=True)
        launch = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            records = list((self.state_dir / "launches").glob("*.json"))
            if records:
                candidate = json.loads(records[0].read_text())
                if candidate.get("native_session") == native_session and candidate.get("recovery_provenance", {}).get("child"):
                    launch = candidate
                    break
            time.sleep(0.05)
        self.assertIsNotNone(launch, "fake native dispatch did not bind its session before interruption")
        os.killpg(process.pid, signal.SIGTERM)
        _stdout, _stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, -signal.SIGTERM)
        before = self.app.state(f"launches/{launch['id']}", None)
        self.assertEqual(before["status"], "starting")
        self.assertNotIn("exit_code", before)
        inspect = self.run_cli(env, "launch-inspect", launch["id"])
        self.assertEqual(inspect.returncode, 0, inspect.stderr)
        inspected = json.loads(inspect.stdout)
        self.assertEqual(inspected["evidence"]["owner"], "matched")
        self.assertEqual(inspected["evidence"]["dispatcher_process"], "stopped")
        self.assertEqual(inspected["evidence"]["child_process"], "stopped")
        self.assertEqual(inspected["evidence"]["native_session"], "ended")
        request_key = str(uuid.uuid4())
        reconcile = self.run_cli(env, "launch-reconcile", launch["id"], "--disposition", "interrupted",
                                 "--request-key", request_key)
        self.assertEqual(reconcile.returncode, 0, reconcile.stderr)
        saved = self.app.state(f"launches/{launch['id']}", None)
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["native_session"], native_session)
        self.assertNotIn("exit_code", saved)
        self.assertNotIn("finished_at", saved)
        resume = self.run_cli(env, "resume", launch["id"], "--task-file", str(followup))
        self.assertEqual(resume.returncode, 0, resume.stderr)
        result_start = resume.stdout.rfind("\n{") + 1
        self.assertGreater(result_start, 0, resume.stdout)
        self.assertEqual(json.loads(resume.stdout[result_start:])["exit_code"], 0)
        self.assertEqual(self.app.state(f"launches/{launch['id']}", None)["status"], "interrupted")
        attempts = list((self.state_dir / "attempts").glob("*.json"))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(json.loads(attempts[0].read_text())["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
