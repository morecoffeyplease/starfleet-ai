import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "model-preferences"))
sys.dont_write_bytecode = True

import handoff_adapter as model_preferences_handoff_adapter
import handoff_engine
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


if __name__ == "__main__":
    unittest.main()
