import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

import test_preferences as fixtures
from test_preferences import mp


class HandoffAdapterTests(unittest.TestCase):
    setUp = fixtures.PreferencesTest.setUp
    args = fixtures.PreferencesTest.args
    call = fixtures.PreferencesTest.call
    activate = fixtures.PreferencesTest.activate
    repo = fixtures.PreferencesTest.repo
    run_cli = fixtures.PreferencesTest.run_cli

    def register_outgoing(self):
        self.app.save_state("orchestrators", {"lane": self.recipient})
        return self.recipient

    def choice(self, kind="preserve_observed_predecessor"):
        if kind == "preserve_observed_predecessor":
            return {"kind": kind}
        return {
            "kind": "model_preferences_candidate",
            "snapshot": {
                "role": "orchestrator", "family": "claude-sonnet", "harness": "claude",
                "model": "claude-sonnet-5-5", "effort": "high",
            },
            "active_profile": "Claude-Codex",
            "active_selection_digest": "d" * 64,
            "candidate_matches_active_orchestrator": False,
            "candidate_digest": "e" * 64,
            "provenance": {"kind": "configured_model_family", "family_source": "fixture"},
        }

    def request_fixture(self, choice_kind="preserve_observed_predecessor"):
        outgoing = self.register_outgoing()
        worktree = self.root / "repo"
        if not worktree.exists():
            worktree = self.repo()
        scope = {
            "seat": {
                "name": "lane", "harness": outgoing["harness"], "session_id": outgoing["session"],
                "workspace_id": str(uuid.UUID(outgoing["workspace"])),
                "panel_id": str(uuid.UUID(outgoing["surface"])),
            },
            "worktree": {
                "root": str(worktree.resolve()), "git_dir": str(worktree / ".git"),
                "common_dir": str(worktree / ".git"),
            },
        }
        choice = self.choice(choice_kind)
        intent = {
            "schema_version": 1,
            "protocol": "starfleet.replicator.successor-intent/v1",
            "id": "a" * 32,
            "scope": scope,
            "policy": "once",
            "choice": choice,
            "request_digest": hashlib.sha256(json.dumps({
                "protocol": "starfleet.replicator.successor-intent/v1",
                "policy": "once",
                "scope": scope,
                "choice": choice,
            }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest(),
            "state": "configured",
            "native_generation": {"status": "unknown", "value": None, "evidence": None},
            "observations": {
                "predecessor_model": {"status": "unknown", "value": None, "evidence": None},
                "predecessor_effort": {"status": "unknown", "value": None, "evidence": None},
                "effective_permissions": {"status": "unknown", "value": None, "evidence": None},
            },
            "permission_requirement": {"kind": "match_predecessor_effective", "status": "unknown"},
            "readiness": {"status": "unknown"},
            "custody_acceptance": {"status": "not_applicable"},
            "predecessor_cleanup": {"status": "not_applicable"},
            "lifecycle_state": "configured",
        }
        return worktree, intent

    def handoff_record(self, intent, handoff_id="b" * 32, session_id=None):
        unknown = {"status": "unknown", "value": None, "evidence": None}
        snapshot = {key: value for key, value in intent.items() if key not in ("lifecycle_state", "handoff")}
        scope = json.loads(json.dumps(intent["scope"]))
        if session_id is not None:
            scope["seat"]["session_id"] = session_id
            snapshot["scope"] = scope
            snapshot["request_digest"] = hashlib.sha256(json.dumps({
                "protocol": "starfleet.replicator.successor-intent/v1",
                "policy": "once",
                "scope": scope,
                "choice": intent["choice"],
            }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        intent_digest = hashlib.sha256(json.dumps(
            snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        return {
            "schema_version": 1,
            "protocol": "starfleet.replicator.handoff/v1",
            "id": handoff_id,
            "handoff_generation": "f" * 32,
            "intent_id": intent["id"],
            "state": "frozen",
            "frozen_at": 1.0,
            "intent_digest": intent_digest,
            "intent_snapshot": snapshot,
            "scope": scope,
            "choice": intent["choice"],
            "native_generation": unknown,
            "native_predecessor_binding": {"status": "unknown", "hook_generation": None, "evidence": None},
            "native_successor_binding": {"status": "unknown", "hook_generation": None, "evidence": None},
            "observations": {
                "predecessor_model": unknown,
                "predecessor_effort": unknown,
                "successor_model": unknown,
                "successor_effort": unknown,
                "effective_permissions": unknown,
            },
            "permission_requirement": {"kind": "match_predecessor_effective", "status": "unknown"},
            "readiness": {"status": "blocked", "reason": "native_observations_unavailable"},
            "launch": {"status": "not_started"},
            "transfer": {"status": "not_applicable"},
            "predecessor_cleanup": {"status": "not_applicable"},
        }

    def write_replicator(self, responses):
        fake_bin = self.root / "fake-replicator-bin"
        fake_bin.mkdir(exist_ok=True)
        response_path = self.root / "replicator-responses.json"
        response_path.write_text(json.dumps(responses), encoding="utf-8")
        call_log = self.root / "replicator-calls.jsonl"
        executable = fake_bin / "replicator"
        executable.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            f"with open({str(call_log)!r}, 'a', encoding='utf-8') as log:\n"
            "    log.write(json.dumps(args) + '\\n')\n"
            "command_args = args[2:] if args[:1] == ['--state-dir'] else args\n"
            f"with open({str(response_path)!r}, encoding='utf-8') as stream:\n"
            "    responses = json.load(stream)\n"
            "if command_args[:2] == ['successor', 'show']:\n"
            "    result = responses.get('successor')\n"
            "    exit_code = responses.get('successor_exit', 0)\n"
            "elif command_args[:2] == ['handoff', 'freeze']:\n"
            "    result = responses.get('freeze')\n"
            "    exit_code = responses.get('freeze_exit', 0)\n"
            "elif command_args[:2] == ['handoff', 'show']:\n"
            "    result = responses.get('show')\n"
            "    exit_code = responses.get('show_exit', 0 if result is not None else 1)\n"
            "    if result is None and exit_code != 0:\n"
            "        sys.stderr.write('replicator: Unknown handoff.\\n')\n"
            "        raise SystemExit(exit_code)\n"
            "else:\n"
            "    sys.stderr.write('unsupported fixture command\\n')\n"
            "    raise SystemExit(91)\n"
            "if result is not None:\n"
            "    sys.stdout.write(json.dumps(result))\n"
            "raise SystemExit(exit_code)\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        environment = dict(os.environ)
        environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
        environment["MP_REPLICATOR_RESPONSES"] = str(response_path)
        return environment, call_log

    def handoff_start_args(self, worktree):
        brief = self.root / "brief.md"
        roadmap = self.root / "roadmap.md"
        brief.write_text("Saved synthetic handoff brief.\n", encoding="utf-8")
        roadmap.write_text("Saved synthetic roadmap.\n", encoding="utf-8")
        return ["handoff-start", "lane", "--brief", str(brief), "--roadmap", str(roadmap),
                "--cwd", str(worktree)]

    def start_responses(self, intent, handoff=None):
        record = self.handoff_record(intent) if handoff is None else handoff
        return {
            "successor": {"schema_version": 2, "intent": intent},
            "freeze": {"handoff": record, "reused": False},
        }

    def test_start_delegates_saved_preserve_intent_before_lock_and_returns_blocked(self):
        self.activate("Claude-Codex")
        worktree, intent = self.request_fixture()
        replicator_state_dir = self.root / "replicator-state"
        args = self.args(*self.handoff_start_args(worktree), "--replicator-state-dir", str(replicator_state_dir))
        environment, call_log = self.write_replicator(self.start_responses(intent))
        original_config = self.app.config_path.read_bytes()
        original_registration = (self.state_dir / "orchestrators.json").read_bytes()
        with patch.dict(os.environ, environment), \
                patch.object(mp, "locked", side_effect=AssertionError("handoff start must be pre-lock")), \
                patch.object(mp, "run_json", side_effect=AssertionError("handoff start must not query CMUX")):
            result = mp.execute(args)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["protocol"], "starfleet.model-preferences.replicator-handoff/v1")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["exit_code"], 2)
        self.assertFalse(result["native_launch_performed"])
        self.assertFalse(result["briefing_recorded"])
        self.assertEqual(result["handoff"]["handle"], "replicator-v1:" + "b" * 32)
        self.assertEqual(result["handoff"]["engine_id"], "b" * 32)
        self.assertEqual(result["handoff"]["record"]["choice"], {"kind": "preserve_observed_predecessor"})
        self.assertEqual(result["handoff"]["record"]["readiness"]["status"], "blocked")
        calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:4], ["--state-dir", str(replicator_state_dir.resolve()), "successor", "show"])
        self.assertIn("--json", calls[0])
        self.assertEqual(calls[1][:4], ["--state-dir", str(replicator_state_dir.resolve()), "handoff", "freeze"])
        self.assertEqual(calls[1][4], intent["id"])
        self.assertIn("--json", calls[1])
        self.assertNotIn("--family", calls[0])
        self.assertNotIn("handoff_id", intent)
        self.assertEqual(original_config, self.app.config_path.read_bytes())
        self.assertEqual(original_registration, (self.state_dir / "orchestrators.json").read_bytes())
        self.assertFalse((self.state_dir / "handoffs" / f"{intent['id']}.json").exists())
        self.assertFalse((self.state_dir / "launches" / f"{intent['id']}.json").exists())

    def test_actual_cli_reports_freeze_only_result_with_blocked_exit_code(self):
        worktree, intent = self.request_fixture()
        environment, _call_log = self.write_replicator(self.start_responses(intent))
        result = self.run_cli(environment, *self.handoff_start_args(worktree))
        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["protocol"], "starfleet.model-preferences.replicator-handoff/v1")
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["native_launch_performed"])
        self.assertFalse((self.state_dir / "handoffs").exists())
        self.assertFalse((self.state_dir / "launches").exists())

    def test_explicit_saved_candidate_provenance_is_forwarded_without_active_profile_selection(self):
        self.activate("Claude-Codex")
        worktree, intent = self.request_fixture("model_preferences_candidate")
        environment, _call_log = self.write_replicator(self.start_responses(intent))
        args = self.args(*self.handoff_start_args(worktree))
        with patch.dict(os.environ, environment), patch.object(mp, "locked", side_effect=AssertionError()):
            result = mp.execute(args)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["handoff"]["record"]["choice"], intent["choice"])
        self.assertEqual(result["handoff"]["record"]["choice"]["active_profile"], "Claude-Codex")
        self.assertEqual(result["handoff"]["record"]["choice"]["candidate_digest"], "e" * 64)
        self.assertNotEqual(result["handoff"]["record"]["choice"]["candidate_digest"],
                            result["handoff"]["record"]["choice"]["active_selection_digest"])

    def test_new_start_fails_closed_when_replicator_is_missing_or_protocol_is_invalid(self):
        worktree, intent = self.request_fixture()
        args = self.args(*self.handoff_start_args(worktree))
        with patch.dict(os.environ, {"PATH": ""}):
            with self.assertRaisesRegex(mp.handoff_adapter.ReplicatorUnavailable, "not installed"):
                mp.execute(args)
        self.assertFalse((self.state_dir / "handoffs").exists())
        self.assertFalse((self.state_dir / "launches").exists())

        invalid_responses = {"successor": {"schema_version": 99, "intent": intent}}
        environment, call_log = self.write_replicator(invalid_responses)
        with patch.dict(os.environ, environment), patch.object(mp, "locked", side_effect=AssertionError()):
            with self.assertRaisesRegex(mp.handoff_adapter.HandoffAdapterError, "unsupported protocol"):
                mp.execute(args)
        self.assertEqual(len(call_log.read_text(encoding="utf-8").splitlines()), 1)
        self.assertFalse((self.state_dir / "handoffs").exists())
        self.assertFalse((self.state_dir / "launches").exists())

    def test_start_refuses_a_handoff_id_collision_without_mutating_either_record(self):
        worktree, intent = self.request_fixture()
        record = self.handoff_record(intent)
        self.app.save_state(f"handoffs/{record['id']}", {"id": record["id"], "status": "briefing"})
        legacy_before = (self.state_dir / "handoffs" / f"{record['id']}.json").read_bytes()
        environment, _call_log = self.write_replicator(self.start_responses(intent, record))
        args = self.args(*self.handoff_start_args(worktree))
        with patch.dict(os.environ, environment):
            with self.assertRaisesRegex(mp.handoff_adapter.HandoffAdapterError, "both Replicator and legacy"):
                mp.execute(args)
        self.assertEqual(legacy_before, (self.state_dir / "handoffs" / f"{record['id']}.json").read_bytes())
        self.assertEqual(set(path.name for path in (self.state_dir / "handoffs").glob("*.json")),
                         {f"{record['id']}.json"})
        self.assertFalse((self.state_dir / "launches").exists())

    def test_versioned_transition_and_collision_never_route_into_legacy_engine(self):
        _worktree, intent = self.request_fixture()
        record = self.handoff_record(intent)
        responses = {"show": {"schema_version": 1, "protocol": "starfleet.replicator.handoff/v1",
                               "handoff": record}}
        environment, call_log = self.write_replicator(responses)
        handle = "replicator-v1:" + record["id"]
        replicator_state_dir = self.root / "replicator-state"
        args = self.args("handoff-ready", handle, "--remaining-questions", "none",
                         "--replicator-state-dir", str(replicator_state_dir))
        with patch.dict(os.environ, environment), \
                patch.object(mp, "locked", side_effect=AssertionError("versioned record must stay pre-lock")), \
                patch.object(mp, "run_json", side_effect=AssertionError("versioned transition must not query CMUX")):
            with self.assertRaisesRegex(mp.handoff_adapter.HandoffAdapterError, "readiness blocked"):
                mp.execute(args)
        calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(calls[0][:4], ["--state-dir", str(replicator_state_dir.resolve()), "handoff", "show"])

        invalid_generation = self.handoff_record(intent, session_id="f" * 32)
        invalid_generation["handoff_generation"] = "f" * 32
        bad_environment, _call_log = self.write_replicator({
            "show": {"schema_version": 1, "protocol": "starfleet.replicator.handoff/v1",
                     "handoff": invalid_generation},
        })
        with patch.dict(os.environ, bad_environment), \
                patch.object(mp, "locked", side_effect=AssertionError("malformed generation stays pre-lock")):
            with self.assertRaisesRegex(mp.handoff_adapter.HandoffAdapterError, "generation binding"):
                mp.execute(args)
        environment, _call_log = self.write_replicator({
            "show": {"schema_version": 1, "protocol": "starfleet.replicator.handoff/v1",
                     "handoff": record},
        })

        self.app.save_state(f"handoffs/{record['id']}", {"id": record["id"], "status": "briefing"})
        legacy_before = (self.state_dir / "handoffs" / f"{record['id']}.json").read_bytes()
        with patch.dict(os.environ, environment), patch.object(mp, "locked", side_effect=AssertionError()):
            with self.assertRaisesRegex(mp.handoff_adapter.HandoffAdapterError, "both Replicator and legacy"):
                mp.execute(args)
        self.assertEqual(legacy_before, (self.state_dir / "handoffs" / f"{record['id']}.json").read_bytes())
        with patch.dict(os.environ, {"PATH": ""}), \
                patch.object(mp, "locked", side_effect=AssertionError("versioned handle must not fall back")), \
                patch.object(mp, "handoff", side_effect=AssertionError("versioned handle cannot use legacy handler")):
            with self.assertRaisesRegex(mp.handoff_adapter.ReplicatorUnavailable, "not installed"):
                mp.execute(args)

    def test_legacy_route_requires_replicator_absence_and_holds_uncertain_records(self):
        handoff_id = "c" * 32
        legacy = {"id": handoff_id, "status": "briefing", "name": "lane"}
        self.app.save_state(f"handoffs/{handoff_id}", legacy)
        environment, _call_log = self.write_replicator({"show": None})
        args = self.args("handoff-ready", handoff_id, "--remaining-questions", "none")
        with patch.dict(os.environ, environment), patch.object(mp, "handoff", return_value={"legacy": handoff_id}) as old:
            result = mp.execute(args)
        self.assertEqual(result, {"legacy": handoff_id})
        old.assert_called_once()

        for status, command in (("opening-unconfirmed", "handoff-ready"),
                                ("close-unconfirmed", "handoff-close")):
            with self.subTest(status=status):
                record = {"id": handoff_id, "status": status, "name": "lane"}
                self.app.save_state(f"handoffs/{handoff_id}", record)
                before = (self.state_dir / "handoffs" / f"{handoff_id}.json").read_bytes()
                call_args = ([command, handoff_id, "--remaining-questions", "none"]
                             if command == "handoff-ready" else
                             [command, handoff_id, "--dependent-jobs-cleared", "--authorization", "explicit fixture"])
                with patch.dict(os.environ, environment), \
                        patch.object(mp, "locked", side_effect=AssertionError("uncertain legacy state stays pre-lock")), \
                        patch.object(mp, "handoff", side_effect=AssertionError("uncertain legacy state cannot replay")):
                    with self.assertRaisesRegex(mp.handoff_adapter.HandoffAdapterError, "inspect-only"):
                        mp.execute(self.args(*call_args))
                self.assertEqual(before, (self.state_dir / "handoffs" / f"{handoff_id}.json").read_bytes())

    def test_legacy_transition_survives_missing_runtime_and_blocks_invalid_protocol(self):
        handoff_id = "d" * 32
        legacy = {"id": handoff_id, "status": "briefing", "name": "lane"}
        self.app.save_state(f"handoffs/{handoff_id}", legacy)
        before = (self.state_dir / "handoffs" / f"{handoff_id}.json").read_bytes()
        args = self.args("handoff-ready", handoff_id, "--remaining-questions", "none")
        with patch.dict(os.environ, {"PATH": ""}), \
                patch.object(mp, "handoff", return_value={"legacy": handoff_id}) as old:
            self.assertEqual(mp.execute(args), {"legacy": handoff_id})
        old.assert_called_once()

        environment, _call_log = self.write_replicator({"show": {"schema_version": 1, "protocol": "wrong"}})
        with patch.dict(os.environ, environment), \
                patch.object(mp, "locked", side_effect=AssertionError("invalid protocol must stay pre-lock")), \
                patch.object(mp, "handoff", side_effect=AssertionError("invalid protocol cannot fall back")):
            with self.assertRaisesRegex(mp.handoff_adapter.HandoffAdapterError, "unsupported versioned protocol"):
                mp.execute(args)
        self.assertEqual(before, (self.state_dir / "handoffs" / f"{handoff_id}.json").read_bytes())

    def test_replicator_handoff_adapter_is_packaged_by_isolated_model_preferences_installer(self):
        installer = Path(__file__).resolve().parents[1] / "install.py"
        runtime_dir = self.root / "installed-runtime"
        bin_dir = self.root / "installed-bin"
        result = subprocess.run(
            [sys.executable, str(installer), "--runtime-dir", str(runtime_dir), "--bin-dir", str(bin_dir),
             "--config-dir", str(self.config_dir), "--state-dir", str(self.state_dir)],
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = json.loads(result.stdout)
        packaged = Path(installed["runtime"]).parent / "handoff_adapter.py"
        source = installer.with_name("handoff_adapter.py")
        self.assertEqual(packaged.read_bytes(), source.read_bytes())
        self.assertEqual(installed["handoff_adapter_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        help_result = subprocess.run([installed["command"], "--help"], capture_output=True, text=True, check=False,
                                      timeout=10, env=dict(os.environ, HOME=str(self.root)))
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("handoff-start", help_result.stdout)


if __name__ == "__main__":
    unittest.main()
