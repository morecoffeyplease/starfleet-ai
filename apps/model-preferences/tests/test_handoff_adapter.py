import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
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
    inventory = fixtures.PreferencesTest.inventory
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

    def candidate_opening_record(self, launch_status="opened_unobserved"):
        self.recipient = dict(self.recipient, harness="codex")
        _worktree, intent = self.request_fixture()
        scope = intent["scope"]
        choice = {
            "kind": "model_preferences_candidate",
            "snapshot": {"role": "orchestrator", "family": "codex-luna", "harness": "codex",
                         "model": "gpt-6-luna", "effort": "xhigh"},
            "active_profile": "Codex-only",
            "active_selection_digest": "d" * 64,
            "candidate_matches_active_orchestrator": False,
            "candidate_digest": "e" * 64,
            "provenance": {"kind": "configured_model_family", "family_source": "fixture"},
        }
        intent["choice"] = choice
        intent["request_digest"] = mp.handoff_adapter._digest({
            "protocol": "starfleet.replicator.successor-intent/v1",
            "policy": "once", "scope": scope, "choice": choice,
        })
        record = self.handoff_record(intent)
        root = scope["worktree"]["root"]
        predecessor = self.native_snapshot(scope, scope["seat"]["session_id"],
                                           scope["seat"]["panel_id"], scope["seat"]["workspace_id"],
                                           "gpt-6.1-sol", "medium", 4242, "1700000000", "turn-predecessor")
        digest = mp.handoff_adapter._digest(predecessor)
        evidence = {"source": "open_codex_rollout", "snapshot_digest": digest,
                    "turn_id": predecessor["turn"]["id"],
                    "recorded_at": predecessor["turn"]["recorded_at"]}
        record.update({
            "native_predecessor_snapshot": predecessor,
            "native_generation": {
                "status": "observed", "value": predecessor["process_identity"],
                "evidence": {"source": "ps", "snapshot_digest": digest,
                             "observed_at": predecessor["observed_at"]},
            },
            "native_predecessor_binding": {
                "status": "observed", "hook_generation": predecessor["hook_generation"],
                "evidence": {"source": "cmux_codex_hook_store_v1", "snapshot_digest": digest},
            },
            "native_successor_generation": mp.handoff_adapter.UNKNOWN,
            "native_successor_snapshot": None,
            "observations": {
                "predecessor_model": {"status": "observed", "value": "gpt-6.1-sol", "evidence": evidence},
                "predecessor_effort": {"status": "observed", "value": "medium", "evidence": evidence},
                "successor_model": mp.handoff_adapter.UNKNOWN,
                "successor_effort": mp.handoff_adapter.UNKNOWN,
                "effective_permissions": mp.handoff_adapter.UNKNOWN,
            },
            "readiness": {"status": "blocked", "reason": "effective_access_unobserved"},
        })
        packet = {"path": str((self.root / "packet.md").resolve()), "bytes": 7, "sha256": "a" * 64}
        native_command = {"executable": "/usr/local/bin/codex",
                          "argv": ["/usr/local/bin/codex", "-m", "gpt-6-luna", "-c",
                                   'model_reasoning_effort="xhigh"']}
        bootstrap = (
            f"You are a same-harness successor candidate for handoff {record['id']}. "
            f"Read the packet at {packet['path']} only if its SHA-256 is {packet['sha256']}. "
            "Start with read-only preparation and print replicator guide --print. "
            "Do not edit files, dispatch work, contact agents, transfer custody, close the predecessor, "
            "or claim that permissions or trust have been verified."
        )
        final_argv = [*native_command["argv"], bootstrap]
        cmux_executable = "/usr/local/bin/cmux"
        cmux = {
            "executable": cmux_executable,
            "argv": [cmux_executable, "--json", "--id-format", "uuids", "new-surface", "--type", "terminal",
                     "--workspace", scope["seat"]["workspace_id"], "--working-directory", root,
                     "--focus", "false", "--command", shlex.join(final_argv)],
            "cwd": root, "workspace_id": scope["seat"]["workspace_id"],
        }
        context_access = {"status": "ready", "items": []}
        guidance_access = {"status": "ready", "items": []}
        access = {
            "id": "c" * 32,
            "checks": {
                "context_access": context_access, "guidance_access": guidance_access,
                "native_trust": {"status": "unknown", "reason": "no_supported_exact_trust_source"},
                "effective_permissions": {
                    "status": "unknown", "reason": "scoped_checks_do_not_prove_complete_permissions",
                },
            },
        }
        record["access_observations"] = {
            "schema_version": 1, "predecessor": [access], "successor": [], "pending": None,
        }
        checks = {
            "frozen_intent": "ready", "predecessor_owner_binding": "ready",
            "predecessor_settings": "ready", "context_access": "ready", "guidance_access": "ready",
            "context_unchanged": "ready", "guidance_unchanged": "ready",
            "git_metadata_access": "ready", "packet_access": "ready",
        }
        current_digest = mp.handoff_adapter._digest(predecessor)
        preflight = {
            "status": "ready", "checked_at": "2026-11-14T12:00:00Z", "checks": checks,
            "predecessor_snapshot_digest": digest, "access_observation_id": access["id"],
            "access_observation_digest": mp.handoff_adapter._digest(access),
            "current_snapshot_digest": current_digest, "current_native_snapshot": predecessor,
            "current_hook_generation": predecessor["hook_generation"],
            "current_process_identity": predecessor["process_identity"],
            "context_access_digest": mp.handoff_adapter._digest(context_access),
            "guidance_access_digest": mp.handoff_adapter._digest(guidance_access),
            "git_identity_digest": mp.handoff_adapter._digest(scope["worktree"]),
            "packet_sha256": packet["sha256"],
        }
        attempt = {
            "id": "9" * 32, "at": "2026-11-14T12:01:00Z",
            "request_digest": mp.handoff_adapter._digest({"native_command": native_command, "packet": packet}),
            "native_command": native_command, "final_native_argv": final_argv,
            "cmux": cmux, "packet": packet, "preflight": preflight,
        }
        returned = None
        if launch_status != "opening_unconfirmed":
            returned = {"surface_id": "44444444-4444-4444-8444-444444444444",
                        "workspace_id": scope["seat"]["workspace_id"]}
        record["launch"] = {"status": launch_status, "attempt": attempt, "returned_surface": returned}
        if launch_status == "successor_observed":
            successor = self.native_snapshot(
                scope, "55555555-5555-4555-8555-555555555555", returned["surface_id"], returned["workspace_id"],
                "gpt-6-luna", "xhigh", 4343, "1700000001", "turn-successor",
            )
            successor_digest = mp.handoff_adapter._digest(successor)
            successor_turn = successor["turn"]
            successor_evidence = {
                "source": "open_codex_rollout", "snapshot_digest": successor_digest,
                "turn_id": successor_turn["id"], "recorded_at": successor_turn["recorded_at"],
            }
            process = successor["process_identity"]
            record["native_successor_snapshot"] = successor
            record["native_successor_generation"] = {
                "status": "observed", "value": process,
                "evidence": {"source": "ps", "snapshot_digest": successor_digest,
                             "observed_at": successor["observed_at"]},
            }
            record["native_successor_binding"] = {
                "status": "observed", "hook_generation": successor["hook_generation"],
                "evidence": {"source": "cmux_codex_hook_store_v1", "snapshot_digest": successor_digest},
            }
            record["observations"]["successor_model"] = {
                "status": "observed", "value": "gpt-6-luna", "evidence": successor_evidence,
            }
            record["observations"]["successor_effort"] = {
                "status": "observed", "value": "xhigh", "evidence": successor_evidence,
            }
        return record

    def native_snapshot(self, scope, session_id, panel_id, workspace_id, model, effort, pid, hook_generation, turn_id):
        root = scope["worktree"]["root"]
        return {
            "protocol": "starfleet.replicator.native-observation/codex-rollout/v1",
            "observed_at": "2026-11-14T12:00:00Z",
            "subject": {"harness": "codex", "session_id": session_id, "panel_id": panel_id,
                        "workspace_id": workspace_id, "worktree": scope["worktree"]},
            "hook_generation": hook_generation,
            "process_identity": {"pid": pid, "parent_pid": 1, "started_at": "2026-11-14T11:00:00Z",
                                 "executable": "/usr/local/bin/codex"},
            "hook_source": {"kind": "cmux_codex_hook_store_v1", "path": str(self.root / "hook.json"),
                            "device": 1, "inode": pid, "modified_at_ns": pid, "updated_at": 1790000000.0},
            "rollout_source": {
                "kind": "open_codex_rollout", "path": str(self.root / f"{turn_id}.jsonl"),
                "device": 1, "inode": pid, "size": 10, "modified_at_ns": pid,
                "cli_version": "0.166.0", "session_meta_cwd": root,
                "session_meta_source": "cli", "session_meta_originator": "codex-tui",
            },
            "turn": {"id": turn_id, "recorded_at": "2026-11-14T11:30:00Z", "cwd": root},
            "settings": {"model": model, "effort": effort, "approval_policy": "never",
                          "sandbox_policy": {"type": "danger-full-access"}},
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

    def test_versioned_opening_and_successor_observations_are_inspectable_but_blocked(self):
        for status in ("opening_unconfirmed", "opened_unobserved", "successor_observed"):
            with self.subTest(status=status):
                record = self.candidate_opening_record(status)
                self.assertIs(mp.handoff_adapter._validate_handoff(record), record)
                self.assertEqual(record["observations"]["effective_permissions"], mp.handoff_adapter.UNKNOWN)
                self.assertEqual(record["permission_requirement"], {
                    "kind": "match_predecessor_effective", "status": "unknown",
                })
                self.assertEqual(record["readiness"], {
                    "status": "blocked", "reason": "effective_access_unobserved",
                })
                if status == "successor_observed":
                    self.assertEqual(record["observations"]["successor_model"]["value"], "gpt-6-luna")
                    self.assertEqual(record["observations"]["successor_effort"]["value"], "xhigh")
                    self.assertNotEqual(record["native_successor_generation"]["value"],
                                        record["native_generation"]["value"])
                    self.assertNotEqual(record["native_successor_binding"]["hook_generation"],
                                        record["native_predecessor_binding"]["hook_generation"])
                environment, call_log = self.write_replicator({
                    "show": {"schema_version": 1, "protocol": "starfleet.replicator.handoff/v1",
                             "handoff": record},
                })
                call_log.unlink(missing_ok=True)
                cmux_log = self.root / "blocked-cmux-calls.jsonl"
                cmux_path = Path(environment["PATH"].split(os.pathsep)[0]) / "cmux"
                cmux_path.write_text(
                    "#!" + sys.executable + "\n"
                    "import json, sys\n"
                    f"with open({str(cmux_log)!r}, 'a', encoding='utf-8') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                    "raise SystemExit(90)\n",
                    encoding="utf-8",
                )
                cmux_path.chmod(0o755)
                args = self.args("handoff-ready", "replicator-v1:" + record["id"],
                                 "--remaining-questions", "none")
                with patch.dict(os.environ, environment), \
                        patch.object(mp, "locked", side_effect=AssertionError("versioned inspection stays pre-lock")), \
                        patch.object(mp, "run_json", side_effect=AssertionError("versioned inspection does not query CMUX")), \
                        patch.object(mp, "handoff", side_effect=AssertionError("versioned state cannot route to legacy")):
                    with self.assertRaisesRegex(mp.handoff_adapter.HandoffAdapterError, "readiness blocked"):
                        mp.execute(args)
                self.assertEqual(len(call_log.read_text(encoding="utf-8").splitlines()), 1)
                call_log.unlink()
                result = self.run_cli(
                    environment, "handoff-ready", "replicator-v1:" + record["id"],
                    "--remaining-questions", "none", "--replicator-state-dir", str(self.root / "replicator-state"),
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("frozen with readiness blocked", result.stderr)
                self.assertFalse(cmux_log.exists())

    def test_versioned_opening_and_successor_observations_reject_tampered_evidence(self):
        opening = self.candidate_opening_record("opened_unobserved")
        successor = self.candidate_opening_record("successor_observed")
        invalid_records = []

        malformed = json.loads(json.dumps(opening))
        malformed["launch"]["attempt"]["request_digest"] = "0" * 64
        invalid_records.append(malformed)

        malformed = json.loads(json.dumps(opening))
        malformed["launch"]["attempt"]["preflight"]["current_native_snapshot"]["settings"]["model"] = "gpt-6-luna"
        invalid_records.append(malformed)

        malformed = json.loads(json.dumps(successor))
        malformed["native_successor_binding"]["hook_generation"] = "1700000000"
        invalid_records.append(malformed)

        malformed = json.loads(json.dumps(successor))
        malformed["observations"]["successor_effort"]["evidence"]["snapshot_digest"] = "0" * 64
        invalid_records.append(malformed)

        malformed = json.loads(json.dumps(successor))
        malformed["native_successor_snapshot"]["subject"]["panel_id"] = "66666666-6666-4666-8666-666666666666"
        invalid_records.append(malformed)

        malformed = json.loads(json.dumps(successor))
        malformed["observations"]["effective_permissions"] = {
            "status": "observed", "value": {"files": "all"}, "evidence": {"source": "fixture"},
        }
        invalid_records.append(malformed)

        malformed = json.loads(json.dumps(opening))
        access = malformed["access_observations"]["predecessor"][-1]
        access["checks"]["native_trust"] = {"status": "ready", "reason": "unsupported_fixture_claim"}
        malformed["launch"]["attempt"]["preflight"]["access_observation_digest"] = \
            mp.handoff_adapter._digest(access)
        invalid_records.append(malformed)

        for index, record in enumerate(invalid_records):
            with self.subTest(index=index):
                with self.assertRaises(mp.handoff_adapter.HandoffAdapterError):
                    mp.handoff_adapter._validate_handoff(record)

    def test_legacy_route_uses_exact_record_and_holds_uncertain_records(self):
        handoff_id = "c" * 32
        legacy = {"id": handoff_id, "status": "briefing", "name": "lane"}
        self.app.save_state(f"handoffs/{handoff_id}", legacy)
        environment, call_log = self.write_replicator({"show": {"schema_version": 1, "protocol": "wrong"}})
        args = self.args("handoff-ready", handoff_id, "--remaining-questions", "none")
        with patch.dict(os.environ, environment), patch.object(mp, "handoff", return_value={"legacy": handoff_id}) as old:
            result = mp.execute(args)
        self.assertEqual(result, {"legacy": handoff_id})
        old.assert_called_once()
        self.assertFalse(call_log.exists())

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
                self.assertFalse(call_log.exists())

    def test_legacy_namespace_ignores_unsupported_runtime_but_versioned_namespace_stays_strict(self):
        handoff_id = "d" * 32
        legacy = {"id": handoff_id, "status": "briefing", "name": "lane"}
        self.app.save_state(f"handoffs/{handoff_id}", legacy)
        before = (self.state_dir / "handoffs" / f"{handoff_id}.json").read_bytes()
        args = self.args("handoff-ready", handoff_id, "--remaining-questions", "none")
        environment, call_log = self.write_replicator({"show": {"schema_version": 1, "protocol": "wrong"}})
        with patch.dict(os.environ, environment), patch.object(mp, "handoff", return_value={"legacy": handoff_id}) as old:
            self.assertEqual(mp.execute(args), {"legacy": handoff_id})
        old.assert_called_once()
        self.assertFalse(call_log.exists())

        with patch.dict(os.environ, environment), \
                patch.object(mp, "locked", side_effect=AssertionError("invalid protocol must stay pre-lock")), \
                patch.object(mp, "handoff", side_effect=AssertionError("invalid protocol cannot fall back")):
            with self.assertRaisesRegex(mp.handoff_adapter.HandoffAdapterError, "unsupported versioned protocol"):
                mp.execute(self.args("handoff-ready", "replicator-v1:" + handoff_id,
                                     "--remaining-questions", "none"))
        self.assertEqual(len(call_log.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(before, (self.state_dir / "handoffs" / f"{handoff_id}.json").read_bytes())

    def test_actual_cli_legacy_transition_survives_an_unsupported_replicator(self):
        self.activate()
        successor = dict(self.recipient, harness="codex", surface=str(uuid.uuid4()), session="fixture-successor")
        handoff_id = uuid.uuid4().hex
        record = {
            "id": handoff_id,
            "name": "lane",
            "status": "transferred",
            "outgoing": self.recipient,
            "choice": {"harness": "codex"},
            "successor_surface": successor["surface"],
            "successor_session": successor["session"],
        }
        self.app.save_state(f"handoffs/{handoff_id}", record)
        handoff_path = self.state_dir / "handoffs" / f"{handoff_id}.json"
        original = handoff_path.read_bytes()
        fake_bin = self.root / "legacy-transition-bin"
        fake_bin.mkdir()
        replicator_trace = self.root / "replicator-trace.jsonl"
        replicator = fake_bin / "replicator"
        replicator.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, sys\n"
            f"with open({str(replicator_trace)!r}, 'a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:]) + chr(10))\n"
            "sys.stderr.write('unsupported installed Replicator handoff protocol' + chr(10))\n"
            "raise SystemExit(2)\n",
            encoding="utf-8",
        )
        replicator.chmod(0o755)
        cmux_trace = self.root / "cmux-trace.jsonl"
        fixture_path = self.root / "cmux-current.json"
        fixture_path.write_text(json.dumps(self.inventory(successor)), encoding="utf-8")
        cmux = fake_bin / "cmux"
        cmux.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            f"with open({str(cmux_trace)!r}, 'a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps(args) + chr(10))\n"
            "if 'current' in args:\n"
            "    sys.stdout.write(open(os.environ['CMUX_FIXTURE'], encoding='utf-8').read())\n"
            f"elif 'identify' in args: sys.stdout.write(json.dumps({{'caller': {{'surface_id': {successor['surface']!r}}}}}))\n"
            "elif 'message' in args:\n"
            "    sys.stdout.write(json.dumps({'id': 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', 'state': 'queued'}))\n"
            "else: sys.stderr.write('unexpected cmux command' + chr(10)); raise SystemExit(90)\n",
            encoding="utf-8",
        )
        cmux.chmod(0o755)
        env = dict(os.environ)
        env.update({
            "HOME": str(self.root / "home"),
            "XDG_STATE_HOME": str(self.root / "xdg-state"),
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "CMUX_FIXTURE": str(fixture_path),
        })
        empty_authorization = self.run_cli(
            env, "handoff-approve-close", handoff_id, "--authorization", "")
        self.assertEqual(empty_authorization.returncode, 1)
        self.assertIn("Record the configured authority's explicit approval", empty_authorization.stderr)
        self.assertEqual(handoff_path.read_bytes(), original)

        authorized = self.run_cli(
            env, "handoff-approve-close", handoff_id,
            "--authorization", "Fixture authority approved this close")
        self.assertEqual(authorized.returncode, 0, authorized.stderr)
        result = json.loads(authorized.stdout)
        self.assertEqual(result["status"], "transferred")
        self.assertEqual(result["close_approval"]["authorization"], "Fixture authority approved this close")
        self.assertTrue(cmux_trace.exists())
        self.assertFalse(replicator_trace.exists())
        self.assertFalse(any("close-surface" in line for line in cmux_trace.read_text(encoding="utf-8").splitlines()))

    def test_handoff_and_registry_adapters_are_packaged_by_isolated_model_preferences_installer(self):
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
        registry = Path(installed["runtime"]).parent / "orchestrator_registry.py"
        registry_source = installer.with_name("orchestrator_registry.py")
        self.assertEqual(registry.read_bytes(), registry_source.read_bytes())
        self.assertEqual(installed["orchestrator_registry_sha256"],
                         hashlib.sha256(registry_source.read_bytes()).hexdigest())
        help_result = subprocess.run([installed["command"], "--help"], capture_output=True, text=True, check=False,
                                      timeout=10, env=dict(os.environ, HOME=str(self.root)))
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("handoff-start", help_result.stdout)


if __name__ == "__main__":
    unittest.main()
