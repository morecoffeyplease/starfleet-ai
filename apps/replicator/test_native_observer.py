import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "model-preferences"))
sys.dont_write_bytecode = True

import handoff_adapter as model_preferences_handoff_adapter
import handoff_engine
import access_observer
import native_observer
import replicator
import successor_intents


SESSION = "11111111-1111-4111-8111-111111111111"
PANEL = "22222222-2222-4222-8222-222222222222"
WORKSPACE = "33333333-3333-4333-8333-333333333333"


class NativeObserverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="replicator-native-observer-")
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.git = shutil.which("git")
        initialized = subprocess.run(
            [self.git, "init", "--quiet", str(self.worktree)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.scope = {
            "seat": {
                "name": "lane",
                "harness": "codex",
                "session_id": SESSION,
                "workspace_id": WORKSPACE,
                "panel_id": PANEL,
            },
            "worktree": replicator.canonical_worktree_identity(str(self.worktree)),
        }
        self.caller = {
            "harness": "codex",
            "session_id": SESSION,
            "panel_id": PANEL,
            "workspace_id": WORKSPACE,
        }
        self.now = datetime.now(timezone.utc)
        self.process_started = self.now - timedelta(hours=2)
        self.turn_time = self.now - timedelta(minutes=1)
        self.pid = 4242
        self.executable = self.root / "bin" / "codex"
        self.executable.parent.mkdir()
        self.executable.write_text("fixture executable", encoding="utf-8")
        self.executable.chmod(0o755)
        self.rollout = self.home / ".codex" / "sessions" / "2026" / "11" / "14" / (
            f"rollout-2026-11-14T00-00-00-{SESSION}.jsonl"
        )
        self.rollout.parent.mkdir(parents=True)
        self._write_rollout()
        hook_dir = self.home / ".cmuxterm"
        hook_dir.mkdir()
        self.hook_path = hook_dir / "codex-hook-sessions.json"
        self.hook_record = {
            "sessionId": SESSION,
            "surfaceId": PANEL,
            "workspaceId": WORKSPACE,
            "cwd": str(self.worktree),
            "pid": self.pid,
            "startedAt": self.process_started.timestamp() - 86400,
            "updatedAt": self.now.timestamp() - 60,
        }
        self._write_hook()
        self.environment = {"CODEX_HOME": str(self.home / ".codex")}
        self.ps_output = self._ps_line(self.process_started)
        self.lsof_paths = [self.rollout]

    def tearDown(self):
        self.temporary.cleanup()

    def _iso(self, value):
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _ps_line(self, started):
        local = started.astimezone()
        return (
            f"{self.pid} 1610 {local.strftime('%a %b %d %H:%M:%S %Y')} "
            f"{self.executable}\n"
        )

    def _write_hook(self):
        document = {"version": 1, "sessions": {SESSION: self.hook_record}}
        self.hook_path.write_text(json.dumps(document), encoding="utf-8")
        self.hook_path.chmod(0o600)

    def _write_rollout(self, *, session_id=SESSION, turn_time=None, model="gpt-6.1-sol",
                       effort="medium", cli_version="0.166.0", sandbox=None,
                       include_body=True, turn_id="turn-native-1"):
        turn_time = self.turn_time if turn_time is None else turn_time
        sandbox = {"type": "danger-full-access"} if sandbox is None else sandbox
        metadata = {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": str(self.worktree),
                "cli_version": cli_version,
                "originator": "codex-tui",
                "source": "cli",
                "model_provider": "openai",
            },
        }
        turn = {
            "type": "turn_context",
            "timestamp": self._iso(turn_time),
            "payload": {
                "turn_id": turn_id,
                "cwd": str(self.worktree),
                "model": model,
                "effort": effort,
                "approval_policy": "never",
                "sandbox_policy": sandbox,
                "debug_only": "not stored",
            },
        }
        rows = [metadata]
        if include_body:
            rows.append({"type": "response_item", "payload": {"message": "BODY_SECRET_SHOULD_NOT_BE_RETURNED"}})
        rows.append(turn)
        self.rollout.parent.mkdir(parents=True, exist_ok=True)
        self.rollout.write_text(
            "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
            encoding="utf-8",
        )
        self.rollout.chmod(0o600)

    def _runner(self, *, process_starts=None, extra_rollouts=()):
        process_starts = process_starts or [self.process_started, self.process_started]
        counts = {"ps": 0, "lsof": 0}

        def run(argv):
            if argv[0] == "ps":
                index = min(counts["ps"], len(process_starts) - 1)
                started = process_starts[index]
                counts["ps"] += 1
                return "ready", self._ps_line(started)
            if argv[0] == "lsof":
                counts["lsof"] += 1
                paths = [self.rollout, *extra_rollouts]
                output = f"p{self.pid}\n" + "".join(
                    f"f{index}\nn{path}\n" for index, path in enumerate(paths)
                )
                return "ready", output
            raise AssertionError(f"Unexpected command: {argv[0]}")

        return run

    def observe(self, *, runner=None, caller=None, worktree=None, clock=None):
        return native_observer.observe_codex_predecessor(
            self.scope,
            self.caller if caller is None else caller,
            self.scope["worktree"] if worktree is None else worktree,
            home=self.home,
            environment=self.environment,
            runner=self._runner() if runner is None else runner,
            clock=(lambda: self.now.timestamp()) if clock is None else clock,
        )

    def frozen_with_snapshot(self, state_dir):
        intent = successor_intents.configure_intent(
            state_dir, self.scope, {"kind": "preserve_observed_predecessor"},
        )
        handoff = handoff_engine.freeze_handoff(state_dir, intent["id"], self.scope, None)["handoff"]
        snapshot = self.observe()
        handoff = handoff_engine.record_predecessor_observation(state_dir, handoff["id"], snapshot)["handoff"]
        return handoff, snapshot

    def test_observer_reads_exact_process_rollout_and_keeps_access_unknown(self):
        snapshot = self.observe()
        self.assertEqual(snapshot["protocol"], native_observer.OBSERVATION_PROTOCOL)
        self.assertEqual(snapshot["subject"]["session_id"], SESSION)
        self.assertEqual(snapshot["subject"]["worktree"], self.scope["worktree"])
        self.assertEqual(snapshot["hook_generation"], f"{self.hook_record['startedAt']:.17g}")
        self.assertNotEqual(snapshot["hook_generation"], snapshot["process_identity"]["started_at"])
        self.assertEqual(snapshot["process_identity"]["pid"], self.pid)
        self.assertEqual(snapshot["rollout_source"]["cli_version"], "0.166.0")
        self.assertEqual(snapshot["turn"]["id"], "turn-native-1")
        self.assertEqual(snapshot["settings"], {
            "model": "gpt-6.1-sol",
            "effort": "medium",
            "approval_policy": "never",
            "sandbox_policy": {"type": "danger-full-access"},
        })
        self.assertNotIn("BODY_SECRET_SHOULD_NOT_BE_RETURNED", repr(snapshot))

    def test_observer_rejects_wrong_owner_and_ambiguous_open_rollout(self):
        wrong_caller = {**self.caller, "panel_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
        with self.assertRaisesRegex(native_observer.NativeObservationError, "caller"):
            self.observe(caller=wrong_caller)
        second_rollout = self.rollout.parent / f"rollout-copy-{SESSION}.jsonl"
        with self.assertRaisesRegex(native_observer.NativeObservationError, "one open rollout"):
            self.observe(runner=self._runner(extra_rollouts=(second_rollout,)))
        actual_runner = self._runner()

        def wrong_process_files(argv):
            status, output = actual_runner(argv)
            return status, output.replace(f"p{self.pid}\n", "p12345\n") if argv[0] == "lsof" else output

        with self.assertRaisesRegex(native_observer.NativeObservationError, "file list"):
            self.observe(runner=wrong_process_files)

    def test_observer_rejects_changed_process_and_stale_or_unsupported_source(self):
        changed_start = self.process_started + timedelta(seconds=1)
        with self.assertRaisesRegex(native_observer.NativeObservationError, "changed"):
            self.observe(runner=self._runner(process_starts=[self.process_started, changed_start]))
        self._write_rollout(turn_time=self.process_started - timedelta(seconds=1))
        with self.assertRaisesRegex(native_observer.NativeObservationError, "process generation"):
            self.observe()
        self._write_rollout(cli_version="0.170.0")
        with self.assertRaisesRegex(native_observer.NativeObservationError, "unsupported version"):
            self.observe()

    def test_observer_rejects_session_and_settings_mismatch(self):
        self._write_rollout(session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        with self.assertRaisesRegex(native_observer.NativeObservationError, "session metadata"):
            self.observe()
        self._write_rollout(sandbox={})
        with self.assertRaisesRegex(native_observer.NativeObservationError, "sandbox setting"):
            self.observe()

    def test_observer_rejects_malformed_and_oversized_rollout_records(self):
        content = self.rollout.read_text(encoding="utf-8")
        self.rollout.write_text(content.replace('"effort":"medium"', '"effort":"medium",'), encoding="utf-8")
        with self.assertRaisesRegex(native_observer.NativeObservationError, "malformed JSON"):
            self.observe()
        self._write_rollout(include_body=False)
        with self.rollout.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": {"message": "x" * native_observer.ROLLOUT_MAX_LINE_BYTES}}) + "\n")
        with self.assertRaisesRegex(native_observer.NativeObservationError, "exceeds the observation limit"):
            self.observe()

    def test_persisted_snapshot_is_idempotent_and_never_claims_access_parity(self):
        intent = successor_intents.configure_intent(self.root / "state", self.scope, {"kind": "preserve_observed_predecessor"})
        frozen = handoff_engine.freeze_handoff(
            self.root / "state", intent["id"], self.scope, None,
        )["handoff"]
        snapshot = self.observe()
        first = handoff_engine.record_predecessor_observation(self.root / "state", frozen["id"], snapshot)
        second = handoff_engine.record_predecessor_observation(self.root / "state", frozen["id"], snapshot)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        stored = successor_intents.read_store(self.root / "state")
        saved = stored["handoffs"][frozen["id"]]
        self.assertEqual(saved["native_predecessor_snapshot"], snapshot)
        self.assertEqual(saved["observations"]["predecessor_model"]["value"], "gpt-6.1-sol")
        self.assertEqual(saved["observations"]["predecessor_effort"]["value"], "medium")
        self.assertEqual(saved["observations"]["effective_permissions"], {
            "status": "unknown", "value": None, "evidence": None,
        })
        self.assertEqual(saved["readiness"], {
            "status": "blocked", "reason": "effective_access_unobserved",
        })
        phase_readiness = handoff_engine.phase_readiness(saved)
        self.assertEqual(phase_readiness["launch"]["checks"]["predecessor_owner_binding"], "unknown")
        self.assertNotIn("successor_binding", phase_readiness["launch"]["checks"])
        self.assertEqual(phase_readiness["transfer"]["status"], "blocked")
        self.assertEqual(model_preferences_handoff_adapter._validate_handoff(saved), saved)
        changed = json.loads(json.dumps(snapshot))
        changed["settings"]["model"] = "gpt-6-luna"
        with self.assertRaisesRegex(handoff_engine.HandoffError, "already"):
            handoff_engine.record_predecessor_observation(self.root / "state", frozen["id"], changed)

    def test_cli_observation_uses_exact_caller_and_persists_without_launch(self):
        intent_state = self.root / "cli-state"
        intent = successor_intents.configure_intent(intent_state, self.scope, {"kind": "preserve_observed_predecessor"})
        frozen = handoff_engine.freeze_handoff(intent_state, intent["id"], self.scope, None)["handoff"]
        fake_bin = self.root / "bin"
        fake_bin.mkdir(exist_ok=True)
        cmux = fake_bin / "cmux"
        current = {
            "truncated": False,
            "owner_availability": {"agent_sessions": "available"},
            "items": [{
                "agents": [{"kind": "codex", "session_id": SESSION, "state": "idle"}],
                "projections": [{"panel_id": PANEL, "workspace_id": WORKSPACE}],
                "placement": {"kind": "local"},
                "cwd": str(self.worktree),
            }],
        }
        cmux.write_text(
            "#!" + sys.executable + "\nimport json\nprint(json.dumps(" + repr(current) + "))\n",
            encoding="utf-8",
        )
        cmux.chmod(0o755)
        ps = fake_bin / "ps"
        ps.write_text(
            "#!" + sys.executable + "\n"
            "from datetime import datetime\n"
            "import sys\n"
            f"print({self._ps_line(self.process_started)!r}, end='')\n",
            encoding="utf-8",
        )
        ps.chmod(0o755)
        lsof = fake_bin / "lsof"
        lsof.write_text(
            "#!" + sys.executable + "\n"
            "import sys\n"
            f"print('p{self.pid}\\nf1\\nn{self.rollout}\\n', end='')\n",
            encoding="utf-8",
        )
        lsof.chmod(0o755)
        environment = dict(os.environ)
        environment.update({
            "HOME": str(self.home),
            "CODEX_HOME": str(self.home / ".codex"),
            "CODEX_THREAD_ID": SESSION,
            "CMUX_SURFACE_ID": PANEL,
            "CMUX_WORKSPACE_ID": WORKSPACE,
            "PATH": str(fake_bin) + os.pathsep + str(Path(self.git).parent),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        result = subprocess.run(
            [sys.executable, str(ROOT / "replicator.py"), "--state-dir", str(intent_state),
             "handoff", "observe-predecessor", frozen["id"], "--json"],
            cwd=self.worktree, env=environment, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertFalse(response["reused"])
        self.assertEqual(response["handoff"]["observations"]["predecessor_model"]["value"], "gpt-6.1-sol")
        self.assertEqual(response["handoff"]["readiness"]["status"], "blocked")
        self.assertEqual(response["handoff"]["launch"], {"status": "not_started"})
        self.assertEqual(
            successor_intents.read_store(intent_state)["handoffs"][frozen["id"]]["native_predecessor_snapshot"],
            response["handoff"]["native_predecessor_snapshot"],
        )

        context_file = self.root / "required-context.md"
        guidance_file = self.root / "AGENTS.md"
        context_file.write_text("context bytes stay private", encoding="utf-8")
        guidance_file.write_text("guidance bytes stay private", encoding="utf-8")
        access_result = subprocess.run(
            [sys.executable, str(ROOT / "replicator.py"), "--state-dir", str(intent_state),
             "handoff", "observe-access", frozen["id"], "--context-file", str(context_file),
             "--guidance-file", str(guidance_file), "--probe-write", "--json"],
            cwd=self.worktree, env=environment, capture_output=True, text=True, check=False,
        )
        self.assertEqual(access_result.returncode, 0, access_result.stderr)
        access_response = json.loads(access_result.stdout)
        access = access_response["observation"]
        self.assertEqual(access["checks"]["owner_binding"]["status"], "ready")
        self.assertEqual(access["checks"]["native_settings_match"]["status"], "ready")
        self.assertEqual(access["checks"]["context_access"]["status"], "ready")
        self.assertEqual(access["checks"]["guidance_access"]["status"], "ready")
        self.assertEqual(access["checks"]["git_metadata_access"]["status"], "ready")
        self.assertEqual(access["checks"]["write_access"]["status"], "ready")
        self.assertEqual(access["checks"]["native_trust"]["status"], "unknown")
        self.assertEqual(access["checks"]["effective_permissions"]["status"], "unknown")
        self.assertEqual(access["cleanup"]["status"], "verified")
        self.assertNotIn("context bytes stay private", access_result.stdout)
        self.assertNotIn("guidance bytes stay private", access_result.stdout)
        self.assertEqual(access_response["handoff"]["phase_readiness"]["transfer"]["status"], "blocked")
        self.assertNotIn("successor_binding", access_response["handoff"]["phase_readiness"]["launch"]["checks"])
        self.assertEqual(access_response["handoff"]["phase_readiness"]["launch"]["checks"]["native_trust"],
                         "unknown")
        self.assertEqual(list(self.worktree.rglob(".replicator-access-probe-*")), [])
        self.assertEqual(list((self.worktree / ".git").rglob(".replicator-access-probe-*")), [])
        original_access = json.loads(json.dumps(access))
        context_file.write_text("changed after the recorded observation", encoding="utf-8")
        repeated_access = subprocess.run(
            [sys.executable, str(ROOT / "replicator.py"), "--state-dir", str(intent_state),
             "handoff", "observe-access", frozen["id"], "--context-file", str(context_file),
             "--guidance-file", str(guidance_file), "--probe-write", "--json"],
            cwd=self.worktree, env=environment, capture_output=True, text=True, check=False,
        )
        self.assertEqual(repeated_access.returncode, 0, repeated_access.stderr)
        repeated_response = json.loads(repeated_access.stdout)
        self.assertTrue(repeated_response["reused"])
        self.assertEqual(repeated_response["observation"], original_access)
        self.assertEqual(len(successor_intents.read_store(intent_state)["handoffs"][frozen["id"]]
                             ["access_observations"]["predecessor"]), 1)
        stored_state = intent_state / successor_intents.STORE_NAME
        before_inspection = stored_state.read_bytes()
        shown = subprocess.run(
            [sys.executable, str(ROOT / "replicator.py"), "--state-dir", str(intent_state),
             "handoff", "show", frozen["id"], "--json"],
            cwd=self.root, env=environment, capture_output=True, text=True, check=False,
        )
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["handoff"]["phase_readiness"]["transfer"]["status"], "blocked")
        self.assertEqual(before_inspection, stored_state.read_bytes())
        reconciled = subprocess.run(
            [sys.executable, str(ROOT / "replicator.py"), "--state-dir", str(intent_state),
             "handoff", "reconcile", frozen["id"], "--json"],
            cwd=self.root, env=environment, capture_output=True, text=True, check=False,
        )
        self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
        reconciliation = json.loads(reconciled.stdout)["reconciliation"]
        self.assertFalse(reconciliation["native_queries_performed"])
        self.assertFalse(reconciliation["writes_performed"])
        self.assertEqual(reconciliation["phase_readiness"]["transfer"]["status"], "blocked")
        self.assertEqual(before_inspection, stored_state.read_bytes())

    def test_access_snapshot_rejects_scope_and_settings_drift_and_keeps_checks_separate(self):
        handoff, baseline = self.frozen_with_snapshot(self.root / "access-drift-state")
        same = access_observer.compare_native_snapshots(handoff["scope"], baseline, baseline)
        self.assertEqual(same["owner_binding"]["status"], "ready")
        self.assertEqual(same["native_settings_match"]["status"], "ready")

        changed_owner = json.loads(json.dumps(baseline))
        changed_owner["process_identity"]["pid"] += 1
        self.assertEqual(access_observer.compare_native_snapshots(
            handoff["scope"], baseline, changed_owner,
        )["owner_binding"]["status"], "blocked")
        changed_generation = json.loads(json.dumps(baseline))
        changed_generation["hook_generation"] = "123.456"
        self.assertEqual(access_observer.compare_native_snapshots(
            handoff["scope"], baseline, changed_generation,
        )["owner_binding"]["status"], "blocked")
        changed_worktree = json.loads(json.dumps(baseline))
        changed_worktree["subject"]["worktree"]["root"] += "-elsewhere"
        self.assertEqual(access_observer.compare_native_snapshots(
            handoff["scope"], baseline, changed_worktree,
        )["owner_binding"]["status"], "blocked")
        changed_settings = json.loads(json.dumps(baseline))
        changed_settings["settings"]["effort"] = "xhigh"
        comparison = access_observer.compare_native_snapshots(handoff["scope"], baseline, changed_settings)
        self.assertEqual(comparison["owner_binding"]["status"], "ready")
        self.assertEqual(comparison["native_settings_match"]["status"], "blocked")

    def test_access_probe_retry_reconciles_exact_pending_file_and_preserves_phase_gates(self):
        state_dir = self.root / "access-retry-state"
        handoff, snapshot = self.frozen_with_snapshot(state_dir)
        context_file = self.root / "retry-context.md"
        context_file.write_text("retry context", encoding="utf-8")
        request = access_observer.normalize_request([str(context_file)], probe_write=True)
        inputs = access_observer.read_inputs(request)
        identity = replicator.canonical_worktree_identity(str(self.worktree))
        git_check = access_observer.git_metadata_check(identity, handoff["scope"]["worktree"])
        origin_check = access_observer.git_origin_check(
            request, str(self.worktree), lambda argv, **kwargs: (_ for _ in ()).throw(AssertionError(argv)),
        )
        with mock.patch.object(access_observer, "_remove_exact_probe", side_effect=OSError("fixture cleanup hold")):
            first = handoff_engine.record_access_observation(
                state_dir, handoff["id"], snapshot, request, inputs, git_check, origin_check, identity,
                observed_at=snapshot["observed_at"], identity_resolver=replicator.canonical_worktree_identity,
            )
        saved = successor_intents.read_store(state_dir)["handoffs"][handoff["id"]]
        pending_id = saved["access_observations"]["pending"]
        self.assertEqual(pending_id, first["observation"]["id"])
        self.assertEqual(first["observation"]["state"], "cleanup_incomplete")
        self.assertTrue(list(self.worktree.glob(".replicator-access-probe-*")))

        retried = handoff_engine.record_access_observation(
            state_dir, handoff["id"], snapshot, request, inputs, git_check, origin_check, identity,
            observed_at=snapshot["observed_at"], identity_resolver=replicator.canonical_worktree_identity,
        )
        self.assertTrue(retried["reused"])
        self.assertEqual(retried["observation"]["id"], pending_id)
        self.assertEqual(retried["observation"]["state"], "complete")
        self.assertEqual(retried["observation"]["cleanup"]["status"], "verified")
        self.assertIsNone(retried["handoff"]["access_observations"]["pending"])
        self.assertEqual(list(self.worktree.glob(".replicator-access-probe-*")), [])
        self.assertEqual(list((self.worktree / ".git").glob(".replicator-access-probe-*")), [])

        repeated = handoff_engine.record_access_observation(
            state_dir, handoff["id"], snapshot, request, inputs, git_check, origin_check, identity,
            observed_at=snapshot["observed_at"], identity_resolver=replicator.canonical_worktree_identity,
        )
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["observation"]["id"], pending_id)
        phases = repeated["handoff"]["phase_readiness"]
        self.assertNotIn("successor_binding", phases["launch"]["checks"])
        self.assertEqual(phases["transfer"]["checks"]["successor_binding"], "unknown")
        self.assertEqual(phases["transfer"]["status"], "blocked")
        self.assertEqual(phases["launch"]["checks"]["native_trust"], "unknown")
        self.assertEqual(phases["launch"]["checks"]["effective_permissions"], "unknown")

    def test_access_probe_preserves_an_existing_filename_collision(self):
        state_dir = self.root / "access-collision-state"
        handoff, snapshot = self.frozen_with_snapshot(state_dir)
        request = access_observer.normalize_request(probe_write=True)
        inputs = access_observer.read_inputs(request)
        identity = replicator.canonical_worktree_identity(str(self.worktree))
        git_check = access_observer.git_metadata_check(identity, handoff["scope"]["worktree"])
        origin_check = access_observer.git_origin_check(request, str(self.worktree), lambda *args, **kwargs: None)
        candidate = access_observer.make_observation(
            handoff, snapshot, request, inputs, git_check, origin_check,
            observed_at=snapshot["observed_at"],
        )
        probe_path = self.worktree / f"{access_observer.PROBE_PREFIX}{candidate['id']}-0"
        probe_path.write_bytes(b"pre-existing fixture data")
        first = handoff_engine.record_access_observation(
            state_dir, handoff["id"], snapshot, request, inputs, git_check, origin_check, identity,
            observed_at=snapshot["observed_at"], identity_resolver=replicator.canonical_worktree_identity,
        )
        self.assertEqual(first["observation"]["state"], "cleanup_incomplete")
        self.assertEqual(first["observation"]["write_plan"][0]["stage"], "identity_unverified")
        self.assertEqual(probe_path.read_bytes(), b"pre-existing fixture data")
        retried = handoff_engine.record_access_observation(
            state_dir, handoff["id"], snapshot, request, inputs, git_check, origin_check, identity,
            observed_at=snapshot["observed_at"], identity_resolver=replicator.canonical_worktree_identity,
        )
        self.assertEqual(retried["observation"]["id"], candidate["id"])
        self.assertEqual(probe_path.read_bytes(), b"pre-existing fixture data")

    def test_access_inputs_are_actual_bounded_reads_and_network_failures_stay_unknown(self):
        good = self.root / "readable.md"
        good.write_bytes(b"actual fixture bytes")
        symlink = self.root / "link.md"
        symlink.symlink_to(good)
        request = access_observer.normalize_request([str(good), str(symlink)], [str(good)])
        checks = access_observer.read_inputs(request)
        self.assertEqual(checks["context_access"]["items"][0]["status"], "ready")
        self.assertEqual(checks["context_access"]["items"][0]["bytes_read"], len(b"actual fixture bytes"))
        self.assertEqual(checks["context_access"]["items"][1]["status"], "blocked")
        self.assertEqual(checks["guidance_access"]["items"][0]["sha256"],
                         hashlib.sha256(b"actual fixture bytes").hexdigest())
        captured = {}

        def failed_network(argv, *, closed_stdin=False):
            captured["argv"] = argv
            captured["closed_stdin"] = closed_stdin
            return "failed", None

        network_request = access_observer.normalize_request(probe_origin=True)
        network = access_observer.git_origin_check(network_request, str(self.worktree), failed_network)
        self.assertEqual(captured["argv"], ["git", "-C", str(self.worktree), "ls-remote", "--refs", "origin"])
        self.assertTrue(captured["closed_stdin"])
        self.assertEqual(network["status"], "unknown")
        self.assertIsNone(network["refs_sha256"])

        def successful_network(argv, *, closed_stdin=False):
            captured["closed_stdin"] = closed_stdin
            return "ready", "abc123\trefs/heads/main\n"

        successful = access_observer.git_origin_check(network_request, str(self.worktree), successful_network)
        self.assertTrue(captured["closed_stdin"])
        self.assertEqual(successful["status"], "ready")
        self.assertEqual(successful["refs_bytes"], len(b"abc123\trefs/heads/main\n"))
        self.assertNotIn("refs/heads/main", repr(successful))
        oversized = access_observer.git_origin_check(
            network_request, str(self.worktree), lambda *args, **kwargs: ("ready", "x" * (2 * 1024 * 1024 + 1)),
        )
        self.assertEqual(oversized["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
