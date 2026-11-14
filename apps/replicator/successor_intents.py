"""Persist one-shot successor intent without claiming native readiness."""

import contextlib
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import shlex
import tempfile
import time
import uuid
from datetime import datetime

import access_observer


STORE_NAME = "successor-intents.json"
LOCK_NAME = ".successor-intents.lock"
STORE_SCHEMA_VERSION = 2
INTENT_SCHEMA_VERSION = 1
INTENT_PROTOCOL = "starfleet.replicator.successor-intent/v1"
HANDOFF_PROTOCOL = "starfleet.replicator.handoff/v1"
MAX_STORE_BYTES = 1024 * 1024
MAX_TOTAL_INTENTS = 256
MAX_INTENTS_PER_SCOPE = 32
MAX_TOTAL_HANDOFFS = 256
LOCK_TIMEOUT_SECONDS = 5
DOCTOR_STATES = {"ready", "missing", "blocked", "unknown", "not_applicable", "skipped"}
NATIVE_OBSERVATION_PROTOCOL = "starfleet.replicator.native-observation/codex-rollout/v1"


class IntentError(ValueError):
    pass


def empty_store():
    return {"schema_version": STORE_SCHEMA_VERSION, "scopes": {}, "handoffs": {}}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _finite_generation(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        number = float(value)
    except ValueError:
        return False
    return math.isfinite(number) and format(number, ".17g") == value


def _json_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_value(entry) for entry in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_value(entry) for key, entry in value.items())
    return False


def _validate_launch(handoff):
    launch = handoff.get("launch")
    if launch == {"status": "not_started"}:
        return
    if (not isinstance(launch, dict)
            or launch.get("status") not in ("opening_unconfirmed", "opened_unobserved", "successor_observed")
            or set(launch) != {"status", "attempt", "returned_surface"}):
        raise IntentError("Successor handoff launch state is malformed.")
    attempt = launch.get("attempt")
    attempt_fields = {
        "id", "at", "request_digest", "native_command", "final_native_argv", "cmux", "packet", "preflight",
    }
    if (not isinstance(attempt, dict) or set(attempt) != attempt_fields
            or not isinstance(attempt.get("id"), str) or len(attempt["id"]) != 32
            or any(char not in "0123456789abcdef" for char in attempt["id"])
            or not _timestamp(attempt.get("at"))
            or not isinstance(attempt.get("request_digest"), str)
            or len(attempt["request_digest"]) != 64
            or any(char not in "0123456789abcdef" for char in attempt["request_digest"])):
        raise IntentError("Successor handoff opening attempt is malformed.")
    native_command = attempt.get("native_command")
    if (not isinstance(native_command, dict) or set(native_command) != {"executable", "argv"}
            or not isinstance(native_command.get("executable"), str)
            or not Path(native_command["executable"]).is_absolute()
            or Path(native_command["executable"]).name != "codex"
            or not isinstance(native_command.get("argv"), list)
            or any(not isinstance(value, str) for value in native_command["argv"])):
        raise IntentError("Successor handoff native command is malformed.")
    model, effort = _handoff_choice_model_effort(handoff)
    configured = f'model_reasoning_effort="{effort}"' if isinstance(effort, str) else None
    if (handoff.get("scope", {}).get("seat", {}).get("harness") != "codex"
            or not isinstance(model, str) or not isinstance(effort, str)
            or native_command["argv"] != [native_command["executable"], "-m", model, "-c", configured]):
        raise IntentError("Successor handoff native command does not match its frozen choice.")
    final_argv = attempt.get("final_native_argv")
    if (not isinstance(final_argv, list) or len(final_argv) != 6
            or final_argv[:5] != native_command["argv"]
            or not isinstance(final_argv[5], str) or not final_argv[5]
            or "replicator guide --print" not in final_argv[5]):
        raise IntentError("Successor handoff final native command is malformed.")
    cmux = attempt.get("cmux")
    seat = handoff.get("scope", {}).get("seat", {})
    worktree = handoff.get("scope", {}).get("worktree", {})
    if (not isinstance(cmux, dict) or set(cmux) != {"executable", "argv", "cwd", "workspace_id"}
            or not isinstance(cmux.get("executable"), str)
            or not Path(cmux["executable"]).is_absolute()
            or Path(cmux["executable"]).name != "cmux"
            or cmux.get("cwd") != worktree.get("root")
            or cmux.get("workspace_id", "").upper() != seat.get("workspace_id", "").upper()
            or not isinstance(cmux.get("argv"), list)
            or any(not isinstance(value, str) for value in cmux["argv"])):
        raise IntentError("Successor handoff CMUX route is malformed.")
    command = shlex.join(final_argv)
    expected_cmux_argv = [
        cmux["executable"], "--json", "--id-format", "uuids", "new-surface", "--type", "terminal",
        "--workspace", seat["workspace_id"], "--working-directory", worktree["root"],
        "--focus", "false", "--command", command,
    ]
    if cmux["argv"] != expected_cmux_argv:
        raise IntentError("Successor handoff CMUX route does not match its frozen command and scope.")
    packet = attempt.get("packet")
    preflight = attempt.get("preflight")
    if (not isinstance(packet, dict) or set(packet) != {"path", "bytes", "sha256"}
            or not isinstance(packet.get("path"), str) or not Path(packet["path"]).is_absolute()
            or type(packet.get("bytes")) is not int or not 0 < packet["bytes"] <= 48 * 1024
            or not isinstance(packet.get("sha256"), str) or len(packet["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in packet["sha256"])
            or not isinstance(preflight, dict)
            or set(preflight) != {
                "status", "checked_at", "checks", "predecessor_snapshot_digest", "access_observation_id",
                "access_observation_digest", "current_snapshot_digest", "current_native_snapshot", "current_hook_generation",
                "current_process_identity", "context_access_digest", "guidance_access_digest",
                "git_identity_digest", "packet_sha256",
            }
            or preflight.get("status") != "ready"
            or not _timestamp(preflight.get("checked_at"))
            or preflight.get("predecessor_snapshot_digest") != digest(handoff.get("native_predecessor_snapshot"))
            or not isinstance(preflight.get("access_observation_id"), str)
            or len(preflight["access_observation_id"]) != 32
            or any(char not in "0123456789abcdef" for char in preflight["access_observation_id"])
            or any(not isinstance(preflight.get(name), str) or len(preflight[name]) != 64
                   or any(char not in "0123456789abcdef" for char in preflight[name])
                   for name in ("access_observation_digest", "current_snapshot_digest",
                                "context_access_digest", "guidance_access_digest", "git_identity_digest"))
            or preflight.get("packet_sha256") != packet["sha256"]
            or not isinstance(preflight.get("current_hook_generation"), str)
            or not _finite_generation(preflight["current_hook_generation"])
            or preflight.get("current_process_identity") != handoff.get("native_predecessor_snapshot", {}).get("process_identity")
            or preflight.get("current_hook_generation") != handoff.get("native_predecessor_binding", {}).get("hook_generation")
            or not isinstance(preflight.get("current_native_snapshot"), dict)
            or preflight.get("current_snapshot_digest") != digest(preflight.get("current_native_snapshot"))
            or preflight.get("current_native_snapshot", {}).get("process_identity") != handoff.get("native_predecessor_snapshot", {}).get("process_identity")
            or preflight.get("current_native_snapshot", {}).get("hook_generation") != handoff.get("native_predecessor_binding", {}).get("hook_generation")
            or not isinstance(preflight.get("checks"), dict)
            or set(preflight["checks"]) != {
                "frozen_intent", "predecessor_owner_binding", "predecessor_settings", "context_access",
                "guidance_access", "context_unchanged", "guidance_unchanged", "git_metadata_access",
                "packet_access",
            }
            or any(status != "ready" for status in preflight["checks"].values())):
        raise IntentError("Successor handoff opening preflight is malformed or incomplete.")
    _validate_launch_snapshot(preflight["current_native_snapshot"], handoff)
    expected_request_digest = digest({"native_command": native_command, "packet": packet})
    if attempt["request_digest"] != expected_request_digest:
        raise IntentError("Successor handoff opening request digest is malformed.")
    returned = launch.get("returned_surface")
    if launch["status"] == "opening_unconfirmed":
        if returned is not None:
            raise IntentError("Unconfirmed successor opening cannot claim a returned surface.")
    else:
        if (not isinstance(returned, dict) or set(returned) != {"surface_id", "workspace_id"}
                or not isinstance(returned.get("surface_id"), str)
                or not isinstance(returned.get("workspace_id"), str)):
            raise IntentError("Successor handoff returned surface is malformed.")
        try:
            if (str(uuid.UUID(returned["surface_id"])) != returned["surface_id"]
                    or str(uuid.UUID(returned["workspace_id"])) != returned["workspace_id"]
                    or returned["workspace_id"].upper() != seat.get("workspace_id", "").upper()
                    or returned["surface_id"].upper() == seat.get("panel_id", "").upper()):
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise IntentError("Successor handoff returned surface is malformed.") from exc


def _handoff_choice_model_effort(handoff):
    choice = handoff.get("choice")
    if isinstance(choice, dict) and choice.get("kind") == "model_preferences_candidate":
        snapshot = choice.get("snapshot")
        if isinstance(snapshot, dict):
            return snapshot.get("model"), snapshot.get("effort")
    predecessor = handoff.get("native_predecessor_snapshot")
    settings = predecessor.get("settings") if isinstance(predecessor, dict) else None
    if isinstance(settings, dict):
        return settings.get("model"), settings.get("effort")
    return None, None


def _validate_launch_snapshot(snapshot, handoff):
    process = snapshot.get("process_identity")
    turn = snapshot.get("turn")
    settings = snapshot.get("settings")
    if not isinstance(process, dict) or not isinstance(turn, dict) or not isinstance(settings, dict):
        raise IntentError("Successor handoff opening snapshot is malformed.")
    snapshot_digest = digest(snapshot)
    evidence = {"source": "open_codex_rollout", "snapshot_digest": snapshot_digest,
                "turn_id": turn.get("id"), "recorded_at": turn.get("recorded_at")}
    temporary = {
        "scope": handoff["scope"],
        "native_predecessor_snapshot": snapshot,
        "native_generation": {
            "status": "observed", "value": process,
            "evidence": {"source": "ps", "snapshot_digest": snapshot_digest,
                         "observed_at": snapshot.get("observed_at")},
        },
        "native_predecessor_binding": {
            "status": "observed", "hook_generation": snapshot.get("hook_generation"),
            "evidence": {"source": "cmux_codex_hook_store_v1", "snapshot_digest": snapshot_digest},
        },
        "native_successor_generation": {"status": "unknown", "value": None, "evidence": None},
        "native_successor_binding": {"status": "unknown", "hook_generation": None, "evidence": None},
        "native_successor_snapshot": None,
        "observations": {
            "predecessor_model": {"status": "observed", "value": settings.get("model"), "evidence": evidence},
            "predecessor_effort": {"status": "observed", "value": settings.get("effort"), "evidence": evidence},
            "successor_model": {"status": "unknown", "value": None, "evidence": None},
            "successor_effort": {"status": "unknown", "value": None, "evidence": None},
            "effective_permissions": {"status": "unknown", "value": None, "evidence": None},
        },
        "readiness": {"status": "blocked", "reason": "effective_access_unobserved"},
        "launch": {"status": "not_started"},
    }
    _validate_native_predecessor_snapshot(temporary)
    baseline = handoff.get("native_predecessor_snapshot")
    comparisons = access_observer.compare_native_snapshots(handoff["scope"], baseline, snapshot)
    if (comparisons["owner_binding"]["status"] != "ready"
            or comparisons["native_settings_match"]["status"] != "ready"):
        raise IntentError("Successor handoff opening snapshot does not match the exact predecessor generation and settings.")


def _validate_native_successor_snapshot(handoff):
    unknown = {"status": "unknown", "value": None, "evidence": None}
    unknown_binding = {"status": "unknown", "hook_generation": None, "evidence": None}
    snapshot = handoff.get("native_successor_snapshot")
    generation = handoff.get("native_successor_generation", unknown)
    binding = handoff.get("native_successor_binding")
    observations = handoff.get("observations")
    if snapshot is None:
        if (generation != unknown or binding != unknown_binding
                or observations.get("successor_model") != unknown
                or observations.get("successor_effort") != unknown
                or handoff.get("launch", {}).get("status") == "successor_observed"):
            raise IntentError("Successor handoff native successor state is malformed.")
        return
    fields = {"protocol", "observed_at", "subject", "hook_generation", "process_identity",
              "hook_source", "rollout_source", "turn", "settings"}
    subject_fields = {"harness", "session_id", "panel_id", "workspace_id", "worktree"}
    process_fields = {"pid", "parent_pid", "started_at", "executable"}
    hook_fields = {"kind", "path", "device", "inode", "modified_at_ns", "updated_at"}
    rollout_fields = {"kind", "path", "device", "inode", "size", "modified_at_ns", "cli_version",
                      "session_meta_cwd", "session_meta_source", "session_meta_originator"}
    turn_fields = {"id", "recorded_at", "cwd"}
    settings_fields = {"model", "effort", "approval_policy", "sandbox_policy"}
    launch = handoff.get("launch", {})
    returned = launch.get("returned_surface") if isinstance(launch, dict) else None
    seat = handoff.get("scope", {}).get("seat", {})
    worktree = handoff.get("scope", {}).get("worktree", {})
    if (not isinstance(snapshot, dict) or set(snapshot) != fields
            or snapshot.get("protocol") != NATIVE_OBSERVATION_PROTOCOL
            or not _timestamp(snapshot.get("observed_at"))
            or not isinstance(snapshot.get("subject"), dict)
            or set(snapshot["subject"]) != subject_fields
            or snapshot["subject"].get("harness") != "codex"
            or snapshot["subject"].get("session_id") == seat.get("session_id")
            or snapshot["subject"].get("panel_id", "").upper() != (returned or {}).get("surface_id", "").upper()
            or snapshot["subject"].get("workspace_id", "").upper() != (returned or {}).get("workspace_id", "").upper()
            or snapshot["subject"].get("worktree") != worktree
            or not _finite_generation(snapshot.get("hook_generation"))
            or not isinstance(snapshot.get("process_identity"), dict)
            or set(snapshot["process_identity"]) != process_fields
            or type(snapshot["process_identity"].get("pid")) is not int or snapshot["process_identity"]["pid"] <= 0
            or type(snapshot["process_identity"].get("parent_pid")) is not int
            or snapshot["process_identity"]["parent_pid"] <= 0
            or not _timestamp(snapshot["process_identity"].get("started_at"))
            or not isinstance(snapshot["process_identity"].get("executable"), str)
            or not Path(snapshot["process_identity"]["executable"]).is_absolute()
            or Path(snapshot["process_identity"]["executable"]).name != "codex"
            or not isinstance(snapshot.get("hook_source"), dict)
            or set(snapshot["hook_source"]) != hook_fields
            or snapshot["hook_source"].get("kind") != "cmux_codex_hook_store_v1"
            or not isinstance(snapshot["hook_source"].get("path"), str)
            or not Path(snapshot["hook_source"]["path"]).is_absolute()
            or any(type(snapshot["hook_source"].get(name)) is not int or snapshot["hook_source"][name] < 0
                   for name in ("device", "inode", "modified_at_ns"))
            or type(snapshot["hook_source"].get("updated_at")) not in (int, float)
            or not math.isfinite(snapshot["hook_source"]["updated_at"])
            or not isinstance(snapshot.get("rollout_source"), dict)
            or set(snapshot["rollout_source"]) != rollout_fields
            or snapshot["rollout_source"].get("kind") != "open_codex_rollout"
            or not isinstance(snapshot["rollout_source"].get("path"), str)
            or not Path(snapshot["rollout_source"]["path"]).is_absolute()
            or any(type(snapshot["rollout_source"].get(name)) is not int or snapshot["rollout_source"][name] < 0
                   for name in ("device", "inode", "size", "modified_at_ns"))
            or snapshot["rollout_source"].get("session_meta_source") != "cli"
            or snapshot["rollout_source"].get("session_meta_originator") != "codex-tui"
            or not isinstance(snapshot.get("turn"), dict)
            or set(snapshot["turn"]) != turn_fields
            or not isinstance(snapshot["turn"].get("id"), str) or not snapshot["turn"]["id"]
            or not _timestamp(snapshot["turn"].get("recorded_at"))
            or not isinstance(snapshot["turn"].get("cwd"), str)
            or not isinstance(snapshot.get("settings"), dict)
            or set(snapshot["settings"]) != settings_fields
            or any(not isinstance(snapshot["settings"].get(name), str) or not snapshot["settings"][name]
                   for name in ("model", "effort", "approval_policy"))
            or not isinstance(snapshot["settings"].get("sandbox_policy"), dict)
            or not snapshot["settings"]["sandbox_policy"]
            or not _json_value(snapshot["settings"]["sandbox_policy"])):
        raise IntentError("Successor handoff native successor snapshot is malformed.")
    if (os.path.realpath(snapshot["rollout_source"]["session_meta_cwd"]) != worktree.get("root")
            or os.path.realpath(snapshot["turn"]["cwd"]) != worktree.get("root")):
        raise IntentError("Successor handoff native successor worktree does not match its scope.")
    try:
        observed_at = datetime.fromisoformat(snapshot["observed_at"].replace("Z", "+00:00"))
        started = datetime.fromisoformat(snapshot["process_identity"]["started_at"].replace("Z", "+00:00"))
        recorded = datetime.fromisoformat(snapshot["turn"]["recorded_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntentError("Successor handoff native successor timestamps are malformed.") from exc
    predecessor = handoff.get("native_predecessor_snapshot")
    if (recorded <= started or recorded > observed_at
            or snapshot["process_identity"] == (predecessor or {}).get("process_identity")
            or snapshot["hook_generation"] == handoff.get("native_predecessor_binding", {}).get("hook_generation")):
        raise IntentError("Successor handoff native successor generation is malformed.")
    snapshot_digest = digest(snapshot)
    process = snapshot["process_identity"]
    settings = snapshot["settings"]
    turn = snapshot["turn"]
    evidence = {"source": "open_codex_rollout", "snapshot_digest": snapshot_digest,
                "turn_id": turn["id"], "recorded_at": turn["recorded_at"]}
    expected_generation = {"status": "observed", "value": process,
                           "evidence": {"source": "ps", "snapshot_digest": snapshot_digest,
                                        "observed_at": snapshot["observed_at"]}}
    expected_binding = {"status": "observed", "hook_generation": snapshot["hook_generation"],
                        "evidence": {"source": "cmux_codex_hook_store_v1", "snapshot_digest": snapshot_digest}}
    if (launch.get("status") != "successor_observed"
            or generation != expected_generation or binding != expected_binding
            or observations.get("successor_model") != {
                "status": "observed", "value": settings["model"], "evidence": evidence}
            or observations.get("successor_effort") != {
                "status": "observed", "value": settings["effort"], "evidence": evidence}):
        raise IntentError("Successor handoff native successor evidence is malformed.")


def _validate_native_predecessor_snapshot(handoff):
    unknown = {"status": "unknown", "value": None, "evidence": None}
    unknown_binding = {"status": "unknown", "hook_generation": None, "evidence": None}
    snapshot = handoff.get("native_predecessor_snapshot")
    if snapshot is None:
        expected = {
            "predecessor_model": unknown,
            "predecessor_effort": unknown,
            "successor_model": unknown,
            "successor_effort": unknown,
            "effective_permissions": unknown,
        }
        if (handoff.get("native_generation") != unknown
                or handoff.get("native_predecessor_binding") != unknown_binding
                or handoff.get("native_successor_binding") != unknown_binding
                or handoff.get("observations") != expected
                or handoff.get("readiness") != {
                    "status": "blocked", "reason": "native_observations_unavailable"}):
            raise IntentError("Successor handoff native observation state is malformed.")
        _validate_native_successor_snapshot(handoff)
        return
    snapshot_fields = {
        "protocol", "observed_at", "subject", "hook_generation", "process_identity",
        "hook_source", "rollout_source", "turn", "settings",
    }
    subject_fields = {"harness", "session_id", "panel_id", "workspace_id", "worktree"}
    process_fields = {"pid", "parent_pid", "started_at", "executable"}
    hook_source_fields = {"kind", "path", "device", "inode", "modified_at_ns", "updated_at"}
    rollout_fields = {
        "kind", "path", "device", "inode", "size", "modified_at_ns", "cli_version",
        "session_meta_cwd", "session_meta_source", "session_meta_originator",
    }
    turn_fields = {"id", "recorded_at", "cwd"}
    settings_fields = {"model", "effort", "approval_policy", "sandbox_policy"}
    if (not isinstance(snapshot, dict) or set(snapshot) != snapshot_fields
            or snapshot.get("protocol") != NATIVE_OBSERVATION_PROTOCOL
            or not _timestamp(snapshot.get("observed_at"))
            or not isinstance(snapshot.get("subject"), dict)
            or set(snapshot["subject"]) != subject_fields
            or snapshot["subject"].get("harness") != "codex"
            or snapshot["subject"].get("worktree") != handoff.get("scope", {}).get("worktree")
            or snapshot["subject"].get("session_id") != handoff.get("scope", {}).get("seat", {}).get("session_id")
            or snapshot["subject"].get("panel_id") != handoff.get("scope", {}).get("seat", {}).get("panel_id")
            or snapshot["subject"].get("workspace_id") != handoff.get("scope", {}).get("seat", {}).get("workspace_id")
            or not _finite_generation(snapshot.get("hook_generation"))
            or not isinstance(snapshot.get("process_identity"), dict)
            or set(snapshot["process_identity"]) != process_fields
            or type(snapshot["process_identity"].get("pid")) is not int
            or snapshot["process_identity"]["pid"] <= 0
            or type(snapshot["process_identity"].get("parent_pid")) is not int
            or snapshot["process_identity"]["parent_pid"] <= 0
            or not _timestamp(snapshot["process_identity"].get("started_at"))
            or not isinstance(snapshot["process_identity"].get("executable"), str)
            or not Path(snapshot["process_identity"]["executable"]).is_absolute()
            or Path(snapshot["process_identity"]["executable"]).name != "codex"
            or not isinstance(snapshot.get("hook_source"), dict)
            or set(snapshot["hook_source"]) != hook_source_fields
            or snapshot["hook_source"].get("kind") != "cmux_codex_hook_store_v1"
            or not isinstance(snapshot["hook_source"].get("path"), str)
            or not Path(snapshot["hook_source"]["path"]).is_absolute()
            or any(type(snapshot["hook_source"].get(name)) is not int or snapshot["hook_source"][name] < 0
                   for name in ("device", "inode", "modified_at_ns"))
            or type(snapshot["hook_source"].get("updated_at")) not in (int, float)
            or not math.isfinite(snapshot["hook_source"]["updated_at"])
            or not isinstance(snapshot.get("rollout_source"), dict)
            or set(snapshot["rollout_source"]) != rollout_fields
            or snapshot["rollout_source"].get("kind") != "open_codex_rollout"
            or not isinstance(snapshot["rollout_source"].get("path"), str)
            or not Path(snapshot["rollout_source"]["path"]).is_absolute()
            or any(type(snapshot["rollout_source"].get(name)) is not int or snapshot["rollout_source"][name] < 0
                   for name in ("device", "inode", "size", "modified_at_ns"))
            or not isinstance(snapshot["rollout_source"].get("cli_version"), str)
            or not snapshot["rollout_source"]["cli_version"]
            or not isinstance(snapshot["rollout_source"].get("session_meta_cwd"), str)
            or not isinstance(snapshot["rollout_source"].get("session_meta_source"), str)
            or not isinstance(snapshot["rollout_source"].get("session_meta_originator"), str)
            or not isinstance(snapshot.get("turn"), dict)
            or set(snapshot["turn"]) != turn_fields
            or not isinstance(snapshot["turn"].get("id"), str)
            or not snapshot["turn"]["id"]
            or not _timestamp(snapshot["turn"].get("recorded_at"))
            or not isinstance(snapshot["turn"].get("cwd"), str)
            or not isinstance(snapshot.get("settings"), dict)
            or set(snapshot["settings"]) != settings_fields
            or any(not isinstance(snapshot["settings"].get(name), str) or not snapshot["settings"][name]
                   for name in ("model", "effort", "approval_policy"))
            or not isinstance(snapshot["settings"].get("sandbox_policy"), dict)
            or not snapshot["settings"]["sandbox_policy"]
            or not _json_value(snapshot["settings"]["sandbox_policy"])):
        raise IntentError("Successor handoff native predecessor snapshot is malformed.")
    scope_worktree = handoff["scope"]["worktree"]["root"]
    if (os.path.realpath(snapshot["rollout_source"]["session_meta_cwd"]) != scope_worktree
            or os.path.realpath(snapshot["turn"]["cwd"]) != scope_worktree):
        raise IntentError("Successor handoff native predecessor worktree does not match its scope.")
    try:
        observed_at = datetime.fromisoformat(snapshot["observed_at"].replace("Z", "+00:00"))
        process_started = datetime.fromisoformat(snapshot["process_identity"]["started_at"].replace("Z", "+00:00"))
        turn_recorded = datetime.fromisoformat(snapshot["turn"]["recorded_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntentError("Successor handoff native predecessor timestamps are malformed.") from exc
    if turn_recorded <= process_started or turn_recorded > observed_at:
        raise IntentError("Successor handoff native turn does not match its process generation.")
    snapshot_digest = digest(snapshot)
    process = snapshot["process_identity"]
    turn = snapshot["turn"]
    expected_generation = {
        "status": "observed",
        "value": process,
        "evidence": {
            "source": "ps",
            "snapshot_digest": snapshot_digest,
            "observed_at": snapshot["observed_at"],
        },
    }
    expected_predecessor_binding = {
        "status": "observed",
        "hook_generation": snapshot["hook_generation"],
        "evidence": {
            "source": "cmux_codex_hook_store_v1",
            "snapshot_digest": snapshot_digest,
        },
    }
    expected_model_effort_evidence = {
        "source": "open_codex_rollout",
        "snapshot_digest": snapshot_digest,
        "turn_id": turn["id"],
        "recorded_at": turn["recorded_at"],
    }
    _validate_native_successor_snapshot(handoff)
    successor_snapshot = handoff.get("native_successor_snapshot")
    if isinstance(successor_snapshot, dict):
        successor_turn = successor_snapshot["turn"]
        successor_settings = successor_snapshot["settings"]
        successor_digest = digest(successor_snapshot)
        successor_evidence = {
            "source": "open_codex_rollout",
            "snapshot_digest": successor_digest,
            "turn_id": successor_turn["id"],
            "recorded_at": successor_turn["recorded_at"],
        }
        successor_model = {
            "status": "observed", "value": successor_settings["model"],
            "evidence": successor_evidence,
        }
        successor_effort = {
            "status": "observed", "value": successor_settings["effort"],
            "evidence": successor_evidence,
        }
        successor_binding = handoff.get("native_successor_binding")
    else:
        successor_model = successor_effort = unknown
        successor_binding = unknown_binding
    expected_observations = {
        "predecessor_model": {
            "status": "observed", "value": snapshot["settings"]["model"],
            "evidence": expected_model_effort_evidence,
        },
        "predecessor_effort": {
            "status": "observed", "value": snapshot["settings"]["effort"],
            "evidence": expected_model_effort_evidence,
        },
        "successor_model": successor_model,
        "successor_effort": successor_effort,
        "effective_permissions": unknown,
    }
    if (handoff.get("native_generation") != expected_generation
            or handoff.get("native_predecessor_binding") != expected_predecessor_binding
            or handoff.get("native_successor_binding") != successor_binding
            or handoff.get("observations") != expected_observations
            or handoff.get("readiness") != {
                "status": "blocked", "reason": "effective_access_unobserved"}):
        raise IntentError("Successor handoff native observation state is malformed.")


def _validate_store(value):
    if (not isinstance(value, dict)
            or value.get("schema_version") not in (1, STORE_SCHEMA_VERSION)):
        raise IntentError("Successor intent state has an unsupported format.")
    scopes = value.get("scopes")
    if not isinstance(scopes, dict):
        raise IntentError("Successor intent state is malformed.")
    handoffs = value.get("handoffs", {})
    if not isinstance(handoffs, dict):
        raise IntentError("Successor handoff state is malformed.")
    value["schema_version"] = STORE_SCHEMA_VERSION
    value["handoffs"] = handoffs
    total = 0
    intents = {}
    for key, records in scopes.items():
        if not isinstance(key, str) or len(key) != 64 or not isinstance(records, list):
            raise IntentError("Successor intent state is malformed.")
        if len(records) > MAX_INTENTS_PER_SCOPE:
            raise IntentError("Successor intent state exceeds its record limit.")
        for record in records:
            if (not isinstance(record, dict) or record.get("schema_version") != INTENT_SCHEMA_VERSION
                    or record.get("protocol") != INTENT_PROTOCOL
                    or not isinstance(record.get("id"), str)
                    or len(record["id"]) != 32
                    or record.get("state") not in ("configured", "cancelled")):
                raise IntentError("Successor intent state is malformed.")
            if any(character not in "0123456789abcdef" for character in record["id"]):
                raise IntentError("Successor intent state is malformed.")
            if "handoff_id" in record:
                handoff_id = record["handoff_id"]
                if (not isinstance(handoff_id, str) or len(handoff_id) != 32
                        or any(character not in "0123456789abcdef" for character in handoff_id)):
                    raise IntentError("Successor intent handoff link is malformed.")
            scope = record.get("scope")
            choice = record.get("choice")
            request_digest = record.get("request_digest")
            if (not isinstance(scope, dict) or set(scope) != {"seat", "worktree"}
                    or not isinstance(choice, dict) or record.get("policy") != "once"
                    or not isinstance(request_digest, str) or len(request_digest) != 64
                    or any(character not in "0123456789abcdef" for character in request_digest)
                    or scope_key(scope) != key or _request_digest(scope, choice) != request_digest):
                raise IntentError("Successor intent state is malformed.")
            seat = scope["seat"]
            worktree = scope["worktree"]
            if (not isinstance(seat, dict)
                    or not all(isinstance(seat.get(field), str) and seat[field]
                               for field in ("name", "harness", "session_id", "workspace_id", "panel_id"))
                    or seat["harness"] not in ("claude", "codex", "muse")
                    or not isinstance(worktree, dict)
                    or not all(isinstance(worktree.get(field), str) and worktree[field]
                               for field in ("root", "git_dir", "common_dir"))):
                raise IntentError("Successor intent state is malformed.")
            try:
                uuid.UUID(seat["workspace_id"])
                uuid.UUID(seat["panel_id"])
            except (ValueError, TypeError, AttributeError) as exc:
                raise IntentError("Successor intent state is malformed.") from exc
            kind = choice.get("kind")
            if kind == "preserve_observed_predecessor":
                if set(choice) != {"kind"}:
                    raise IntentError("Successor intent state is malformed.")
            elif kind == "model_preferences_candidate":
                if set(choice) != {
                        "kind", "snapshot", "active_profile", "active_selection_digest",
                        "candidate_matches_active_orchestrator", "candidate_digest", "provenance"}:
                    raise IntentError("Successor intent state is malformed.")
                snapshot = choice.get("snapshot")
                provenance = choice.get("provenance")
                candidate_digest = choice.get("candidate_digest")
                if (not isinstance(snapshot, dict)
                        or snapshot.get("role") != "orchestrator"
                        or snapshot.get("harness") != seat["harness"]
                        or not all(isinstance(snapshot.get(field), str) and snapshot[field]
                                   for field in ("family", "model", "effort"))
                        or not isinstance(provenance, dict)
                        or provenance.get("kind") != "configured_model_family"
                        or not isinstance(candidate_digest, str) or len(candidate_digest) != 64
                        or any(character not in "0123456789abcdef" for character in candidate_digest)):
                    raise IntentError("Successor intent state is malformed.")
                active_profile = choice.get("active_profile")
                active_selection_digest = choice.get("active_selection_digest")
                matches_active = choice.get("candidate_matches_active_orchestrator")
                if (active_profile is not None and (not isinstance(active_profile, str) or not active_profile)
                        or active_selection_digest is not None
                        and (not isinstance(active_selection_digest, str) or len(active_selection_digest) != 64
                             or any(character not in "0123456789abcdef" for character in active_selection_digest))
                        or (active_profile is None) != (active_selection_digest is None)
                        or matches_active is not None and type(matches_active) is not bool
                        or (active_profile is None) != (matches_active is None)
                        or matches_active is False and candidate_digest == active_selection_digest):
                    raise IntentError("Successor intent state is malformed.")
            else:
                raise IntentError("Successor intent state is malformed.")
            generation = record.get("native_generation")
            if generation != {"status": "unknown", "value": None, "evidence": None}:
                raise IntentError("Successor intent state is malformed.")
            observations = record.get("observations")
            expected_observation = {"status": "unknown", "value": None, "evidence": None}
            if (not isinstance(observations, dict)
                    or set(observations) != {"predecessor_model", "predecessor_effort", "effective_permissions"}
                    or any(observations.get(name) != expected_observation for name in observations)):
                raise IntentError("Successor intent state is malformed.")
            if (record.get("permission_requirement") != {
                    "kind": "match_predecessor_effective", "status": "unknown"}
                    or record.get("readiness") != {"status": "unknown"}
                    or record.get("custody_acceptance") != {"status": "not_applicable"}
                    or record.get("predecessor_cleanup") != {"status": "not_applicable"}):
                raise IntentError("Successor intent state is malformed.")
            intents[record["id"]] = record
        total += len(records)
    if total > MAX_TOTAL_INTENTS:
        raise IntentError("Successor intent state exceeds its record limit.")
    if len(handoffs) > MAX_TOTAL_HANDOFFS:
        raise IntentError("Successor handoff state exceeds its record limit.")
    linked_intents = set()
    handoff_generations = set()
    for handoff_id, handoff in handoffs.items():
        if (not isinstance(handoff_id, str) or len(handoff_id) != 32
                or any(character not in "0123456789abcdef" for character in handoff_id)
                or not isinstance(handoff, dict)
                or handoff.get("schema_version") != 1
                or handoff.get("protocol") != HANDOFF_PROTOCOL
                or handoff.get("id") != handoff_id
                or not isinstance(handoff.get("handoff_generation"), str)
                or len(handoff["handoff_generation"]) != 32
                or any(character not in "0123456789abcdef" for character in handoff["handoff_generation"])
                or handoff["handoff_generation"] == handoff_id
                or handoff.get("state") != "frozen"):
            raise IntentError("Successor handoff state is malformed.")
        if handoff["handoff_generation"] in handoff_generations:
            raise IntentError("Successor handoff generation is duplicated.")
        handoff_generations.add(handoff["handoff_generation"])
        intent_id = handoff.get("intent_id")
        if (not isinstance(intent_id, str) or len(intent_id) != 32
                or any(character not in "0123456789abcdef" for character in intent_id)):
            raise IntentError("Successor handoff link is malformed.")
        intent = intents.get(intent_id)
        if (intent is None or intent.get("state") != "configured"
                or intent.get("handoff_id") != handoff_id
                or intent_id in linked_intents):
            raise IntentError("Successor handoff link is malformed.")
        linked_intents.add(intent_id)
        snapshot = handoff.get("intent_snapshot")
        if (not isinstance(snapshot, dict) or snapshot.get("id") != intent_id
                or snapshot.get("scope") != intent.get("scope")
                or snapshot.get("choice") != intent.get("choice")
                or handoff.get("scope") != intent.get("scope")
                or handoff.get("choice") != intent.get("choice")
                or handoff.get("intent_digest") != digest(snapshot)):
            raise IntentError("Successor handoff snapshot is malformed.")
        _validate_native_predecessor_snapshot(handoff)
        _validate_launch(handoff)
        if "access_observations" in handoff:
            try:
                access_observer.validate_access_history(handoff["access_observations"], handoff)
            except access_observer.AccessObservationError as exc:
                raise IntentError("Successor handoff scoped access evidence is malformed.") from exc
            launch = handoff.get("launch", {})
            if launch.get("status") != "not_started":
                attempt = launch.get("attempt", {})
                preflight = attempt.get("preflight", {})
                history = handoff["access_observations"].get("predecessor", [])
                latest = history[-1] if history else None
                if (not isinstance(latest, dict)
                        or handoff["access_observations"].get("pending") is not None
                        or preflight.get("access_observation_id") != latest.get("id")
                        or preflight.get("access_observation_digest") != digest(latest)
                        or preflight.get("context_access_digest") != digest(latest.get("checks", {}).get("context_access"))
                        or preflight.get("guidance_access_digest") != digest(latest.get("checks", {}).get("guidance_access"))
                        or preflight.get("git_identity_digest") != digest(handoff.get("scope", {}).get("worktree"))):
                    raise IntentError("Successor handoff opening preflight no longer matches its scoped access evidence.")
        if (handoff.get("permission_requirement") != {
                    "kind": "match_predecessor_effective", "status": "unknown"}
                or handoff.get("transfer") != {"status": "not_applicable"}
                or handoff.get("predecessor_cleanup") != {"status": "not_applicable"}):
            raise IntentError("Successor handoff readiness is malformed.")
        frozen_at = handoff.get("frozen_at")
        if type(frozen_at) not in (int, float) or not math.isfinite(frozen_at) or frozen_at < 0:
            raise IntentError("Successor handoff timestamp is malformed.")
    for intent_id, intent in intents.items():
        handoff_id = intent.get("handoff_id")
        if handoff_id is not None:
            handoff = handoffs.get(handoff_id)
            if handoff is None or handoff.get("intent_id") != intent_id:
                raise IntentError("Successor intent handoff link is malformed.")
    return value


def read_store(state_dir):
    root = Path(state_dir).expanduser()
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return empty_store()
    except PermissionError:
        raise
    except OSError as exc:
        raise IntentError("Successor intent state directory could not be read.") from exc
    if stat.S_ISLNK(root_info.st_mode):
        raise IntentError("Successor intent state directory is a symlink.")
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_IMODE(root_info.st_mode) & 0o077:
        raise IntentError("Successor intent state directory must be private.")
    path = root / STORE_NAME
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return empty_store()
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise IntentError("Successor intent state file is a symlink.") from exc
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise PermissionError("Successor intent state could not be read.") from exc
        raise IntentError("Successor intent state could not be read.") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > MAX_STORE_BYTES):
            raise IntentError("Successor intent state is not a bounded regular file.")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            data = stream.read(MAX_STORE_BYTES + 1)
        if len(data) > MAX_STORE_BYTES:
            raise IntentError("Successor intent state exceeds its size limit.")
        return _validate_store(json.loads(data.decode("utf-8")))
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, IntentError):
            raise
        raise IntentError("Successor intent state could not be decoded.") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def ensure_private_directory(state_dir):
    root = Path(state_dir).expanduser()
    if root.is_symlink():
        raise IntentError("Successor intent state directory is a symlink.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise IntentError("Successor intent state directory must be private.")
    return root


@contextlib.contextmanager
def mutation_lock(state_dir):
    root = ensure_private_directory(state_dir)
    lock_path = root / LOCK_NAME
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise IntentError("Successor intent mutation lock is unavailable.") from exc
    with os.fdopen(fd, "a") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise IntentError("Successor intent mutation lock must be a private regular file.")
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise IntentError("Successor intent state is busy; retry the command.") from exc
                time.sleep(0.02)
        try:
            yield root
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def write_store(root, value):
    _validate_store(value)
    data = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(data) > MAX_STORE_BYTES:
        raise IntentError("Successor intent state exceeds its size limit.")
    fd, temporary = tempfile.mkstemp(prefix=".successor-intents-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / STORE_NAME)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def scope_key(scope):
    return digest({"seat": scope["seat"], "worktree": scope["worktree"]})


def _request_digest(scope, choice):
    return digest({"protocol": INTENT_PROTOCOL, "policy": "once", "scope": scope, "choice": choice})


def intent_lifecycle_state(store, record):
    if record["state"] == "cancelled":
        return "cancelled"
    handoff_id = record.get("handoff_id")
    if handoff_id is None:
        return "configured"
    handoff = store["handoffs"].get(handoff_id)
    if handoff is None:
        raise IntentError("Successor intent handoff link is unavailable.")
    if handoff["state"] == "transferred":
        return "consumed"
    return "frozen"


def public_intent(store, record):
    result = {**record, "lifecycle_state": intent_lifecycle_state(store, record)}
    handoff_id = record.get("handoff_id")
    if handoff_id is not None:
        handoff = store["handoffs"].get(handoff_id)
        result["handoff"] = ({"id": handoff_id, "protocol": handoff["protocol"],
                              "state": handoff["state"]} if handoff else None)
    return result


def configure_intent(state_dir, scope, choice):
    key = scope_key(scope)
    request_digest = _request_digest(scope, choice)
    with mutation_lock(state_dir) as root:
        store = read_store(root)
        records = store["scopes"].setdefault(key, [])
        active = next((record for record in reversed(records)
                       if intent_lifecycle_state(store, record) in ("configured", "frozen")), None)
        if active:
            if active["request_digest"] == request_digest:
                return {**public_intent(store, active), "reused": True}
            raise IntentError("A different successor intent is already configured or frozen for this exact scope; cancel it first.")
        total = sum(len(items) for items in store["scopes"].values())
        if total >= MAX_TOTAL_INTENTS or len(records) >= MAX_INTENTS_PER_SCOPE:
            raise IntentError("Successor intent state reached its record limit.")
        intent = {
            "schema_version": INTENT_SCHEMA_VERSION,
            "protocol": INTENT_PROTOCOL,
            "id": uuid.uuid4().hex,
            "scope": scope,
            "policy": "once",
            "choice": choice,
            "request_digest": request_digest,
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
        }
        records.append(intent)
        write_store(root, store)
        return {**public_intent(store, intent), "reused": False}


def show_intent(state_dir, scope):
    key = scope_key(scope)
    store = read_store(state_dir)
    records = store["scopes"].get(key, [])
    if not records:
        return {"schema_version": STORE_SCHEMA_VERSION, "intent": None}
    return {"schema_version": STORE_SCHEMA_VERSION, "intent": public_intent(store, records[-1])}


def _find_intent(store, intent_id):
    for key, records in store["scopes"].items():
        for index, record in enumerate(records):
            if record["id"] == intent_id:
                return key, index, record
    return None, None, None


def cancel_intent(state_dir, intent_id):
    if len(intent_id) != 32 or any(character not in "0123456789abcdef" for character in intent_id):
        raise IntentError("Invalid successor intent ID.")
    with mutation_lock(state_dir) as root:
        store = read_store(root)
        key, index, current = _find_intent(store, intent_id)
        if current is None:
            raise IntentError("Unknown successor intent.")
        if current["state"] == "cancelled":
            return public_intent(store, current)
        if intent_lifecycle_state(store, current) != "configured":
            raise IntentError("Only a configured successor intent can be cancelled before it is frozen.")
        store["scopes"][key][index] = {**current, "state": "cancelled"}
        write_store(root, store)
        return public_intent(store, store["scopes"][key][index])


def doctor_report(state_dir):
    configuration = "ready"
    try:
        read_store(state_dir)
    except PermissionError:
        configuration = "blocked"
    except OSError:
        configuration = "blocked"
    except IntentError:
        configuration = "unknown"
    checks = {
        "configuration": configuration,
        "registration": "not_applicable",
        "worker": "not_applicable",
        "intended_delivery": "not_applicable",
        "observed_delivery": "unknown",
    }
    if any(state not in DOCTOR_STATES for state in checks.values()):
        raise IntentError("Doctor state is invalid.")
    return {"schema_version": 1, "checks": checks}
