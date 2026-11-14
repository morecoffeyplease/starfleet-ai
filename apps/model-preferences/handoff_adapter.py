"""Versioned delegation from Model Preferences to Replicator handoff records."""

import hashlib
import json
import math
import os
import re
import shlex
from pathlib import Path
import shutil
import subprocess
import uuid
from datetime import datetime


HANDOFF_PROTOCOL = "starfleet.replicator.handoff/v1"
INTENT_PROTOCOL = "starfleet.replicator.successor-intent/v1"
ADAPTER_PROTOCOL = "starfleet.model-preferences.replicator-handoff/v1"
HANDOFF_HANDLE_PREFIX = "replicator-v1:"
UNKNOWN = {"status": "unknown", "value": None, "evidence": None}
UNKNOWN_BINDING = {"status": "unknown", "hook_generation": None, "evidence": None}
HEX_32 = re.compile(r"[0-9a-f]{32}\Z")
HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
MAX_OUTPUT_BYTES = 256 * 1024
COMMAND_TIMEOUT_SECONDS = 5
UNCERTAIN_LEGACY_STATES = {"opening", "opening-unconfirmed", "close-requested", "close-unconfirmed"}
NATIVE_OBSERVATION_PROTOCOL = "starfleet.replicator.native-observation/codex-rollout/v1"


class HandoffAdapterError(ValueError):
    pass


class ReplicatorUnavailable(HandoffAdapterError):
    pass


def _call(arguments, runner=None):
    executable = shutil.which("replicator")
    if executable is None:
        raise ReplicatorUnavailable("Replicator is not installed or is not available on PATH.")
    invoke = subprocess.run if runner is None else runner
    try:
        result = invoke([executable, *arguments], stdin=subprocess.DEVNULL, capture_output=True,
                        text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HandoffAdapterError("The bounded Replicator command could not be completed.") from exc
    if not isinstance(result.stdout, str) or not isinstance(result.stderr, str):
        raise HandoffAdapterError("Replicator returned an unsupported command result.")
    stdout_size = len(result.stdout.encode("utf-8", "replace"))
    stderr_size = len(result.stderr.encode("utf-8", "replace"))
    if stdout_size + stderr_size > MAX_OUTPUT_BYTES:
        raise HandoffAdapterError("Replicator returned an oversized response.")
    return result


def _json_result(result):
    if result.returncode != 0:
        raise HandoffAdapterError("Replicator rejected the handoff request; Model Preferences made no legacy record.")
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise HandoffAdapterError("Replicator returned an unreadable handoff response.") from exc
    if not isinstance(value, dict):
        raise HandoffAdapterError("Replicator returned an unsupported handoff response.")
    return value


def _unknown_handoff_result(result):
    if result.returncode == 0:
        return False
    return result.stderr.strip() == "replicator: Unknown handoff."


def _digest(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _public_handle(handoff_id):
    return HANDOFF_HANDLE_PREFIX + handoff_id


def _engine_id(handle):
    if not isinstance(handle, str) or not handle.startswith(HANDOFF_HANDLE_PREFIX):
        return None
    handoff_id = handle[len(HANDOFF_HANDLE_PREFIX):]
    if HEX_32.fullmatch(handoff_id) is None:
        raise HandoffAdapterError("Invalid versioned Replicator handoff handle.")
    return handoff_id


def _read_json(arguments, runner=None):
    return _json_result(_call(arguments, runner=runner))


def _validate_intent_report(report, seat_name, worktree_root):
    if report.get("schema_version") != 2 or set(report) != {"schema_version", "intent"}:
        raise HandoffAdapterError("Replicator successor intent response has an unsupported protocol.")
    intent = report.get("intent")
    if intent is None:
        raise HandoffAdapterError("No one-shot Replicator successor intent is configured for this exact seat and worktree.")
    if (not isinstance(intent, dict) or intent.get("schema_version") != 1
            or intent.get("protocol") != INTENT_PROTOCOL or intent.get("policy") != "once"
            or intent.get("state") != "configured"
            or intent.get("lifecycle_state") not in ("configured", "frozen")
            or not isinstance(intent.get("id"), str) or HEX_32.fullmatch(intent["id"]) is None):
        raise HandoffAdapterError("Replicator successor intent has an unsupported or non-one-shot protocol.")
    scope = intent.get("scope")
    if (not isinstance(scope, dict) or set(scope) != {"seat", "worktree"}
            or not isinstance(scope.get("seat"), dict)
            or not isinstance(scope.get("worktree"), dict)
            or set(scope["seat"]) != {"name", "harness", "session_id", "workspace_id", "panel_id"}
            or set(scope["worktree"]) != {"root", "git_dir", "common_dir"}
            or scope["seat"].get("name") != seat_name
            or not all(isinstance(scope["worktree"].get(key), str) and scope["worktree"][key]
                       for key in ("root", "git_dir", "common_dir"))
            or scope["worktree"].get("root") != worktree_root
            or not isinstance(intent.get("request_digest"), str)
            or HEX_64.fullmatch(intent["request_digest"]) is None
            or intent["request_digest"] != _digest({
                "protocol": INTENT_PROTOCOL,
                "policy": "once",
                "scope": scope,
                "choice": intent.get("choice"),
            })):
        raise HandoffAdapterError("Replicator successor intent scope does not match this handoff request.")
    choice = intent.get("choice")
    if not isinstance(choice, dict):
        raise HandoffAdapterError("Replicator successor intent choice is malformed.")
    if choice.get("kind") == "preserve_observed_predecessor":
        if set(choice) != {"kind"}:
            raise HandoffAdapterError("Replicator preserve-observed choice is malformed.")
    elif choice.get("kind") == "model_preferences_candidate":
        snapshot = choice.get("snapshot")
        provenance = choice.get("provenance")
        active_profile = choice.get("active_profile")
        active_digest = choice.get("active_selection_digest")
        matches_active = choice.get("candidate_matches_active_orchestrator")
        if (set(choice) != {
                "kind", "snapshot", "active_profile", "active_selection_digest",
                "candidate_matches_active_orchestrator", "candidate_digest", "provenance"}
                or not isinstance(snapshot, dict)
                or set(snapshot) != {"role", "family", "harness", "model", "effort"}
                or snapshot.get("role") != "orchestrator"
                or snapshot.get("harness") != scope["seat"].get("harness")
                or not isinstance(snapshot.get("family"), str)
                or not isinstance(snapshot.get("model"), str)
                or not isinstance(snapshot.get("effort"), str)
                or not isinstance(provenance, dict)
                or provenance.get("kind") != "configured_model_family"
                or active_profile is not None and (not isinstance(active_profile, str) or not active_profile)
                or active_digest is not None and (not isinstance(active_digest, str)
                                                  or HEX_64.fullmatch(active_digest) is None)
                or (active_profile is None) != (active_digest is None)
                or matches_active is not None and type(matches_active) is not bool
                or (active_profile is None) != (matches_active is None)
                or not isinstance(choice.get("candidate_digest"), str)
                or HEX_64.fullmatch(choice["candidate_digest"]) is None
                or matches_active is False and choice["candidate_digest"] == active_digest):
            raise HandoffAdapterError("Replicator candidate choice or provenance is malformed.")
    else:
        raise HandoffAdapterError("Replicator successor intent choice kind is unsupported.")
    return intent


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


def _valid_observed_snapshot(record, scope, snapshot, observations):
    if not isinstance(snapshot, dict):
        return False
    if set(snapshot) != {
            "protocol", "observed_at", "subject", "hook_generation", "process_identity",
            "hook_source", "rollout_source", "turn", "settings"}:
        return False
    subject = snapshot.get("subject")
    process = snapshot.get("process_identity")
    hook_source = snapshot.get("hook_source")
    rollout_source = snapshot.get("rollout_source")
    turn = snapshot.get("turn")
    settings = snapshot.get("settings")
    if (snapshot.get("protocol") != NATIVE_OBSERVATION_PROTOCOL
            or not _timestamp(snapshot.get("observed_at"))
            or not isinstance(subject, dict)
            or set(subject) != {"harness", "session_id", "panel_id", "workspace_id", "worktree"}
            or subject.get("harness") != "codex"
            or subject.get("session_id") != scope["seat"]["session_id"]
            or subject.get("panel_id") != scope["seat"]["panel_id"]
            or subject.get("workspace_id") != scope["seat"]["workspace_id"]
            or subject.get("worktree") != scope["worktree"]
            or not _finite_generation(snapshot.get("hook_generation"))):
        return False
    if (not isinstance(process, dict)
            or set(process) != {"pid", "parent_pid", "started_at", "executable"}
            or type(process.get("pid")) is not int or process["pid"] <= 0
            or type(process.get("parent_pid")) is not int or process["parent_pid"] <= 0
            or not _timestamp(process.get("started_at"))
            or not isinstance(process.get("executable"), str)
            or not Path(process["executable"]).is_absolute()
            or Path(process["executable"]).name != "codex"):
        return False
    if (not isinstance(hook_source, dict)
            or set(hook_source) != {"kind", "path", "device", "inode", "modified_at_ns", "updated_at"}
            or hook_source.get("kind") != "cmux_codex_hook_store_v1"
            or not isinstance(hook_source.get("path"), str)
            or not Path(hook_source["path"]).is_absolute()
            or any(type(hook_source.get(key)) is not int or hook_source[key] < 0
                   for key in ("device", "inode", "modified_at_ns"))
            or type(hook_source.get("updated_at")) not in (int, float)
            or not math.isfinite(hook_source["updated_at"])):
        return False
    if (not isinstance(rollout_source, dict)
            or set(rollout_source) != {
                "kind", "path", "device", "inode", "size", "modified_at_ns", "cli_version",
                "session_meta_cwd", "session_meta_source", "session_meta_originator",
            }
            or rollout_source.get("kind") != "open_codex_rollout"
            or not isinstance(rollout_source.get("path"), str)
            or not Path(rollout_source["path"]).is_absolute()
            or any(type(rollout_source.get(key)) is not int or rollout_source[key] < 0
                   for key in ("device", "inode", "size", "modified_at_ns"))
            or not isinstance(rollout_source.get("cli_version"), str)
            or not isinstance(rollout_source.get("session_meta_cwd"), str)
            or os.path.realpath(rollout_source["session_meta_cwd"]) != scope["worktree"]["root"]
            or rollout_source.get("session_meta_source") != "cli"
            or rollout_source.get("session_meta_originator") != "codex-tui"):
        return False
    if (not isinstance(turn, dict)
            or set(turn) != {"id", "recorded_at", "cwd"}
            or not isinstance(turn.get("id"), str) or not turn["id"]
            or not _timestamp(turn.get("recorded_at"))
            or not isinstance(turn.get("cwd"), str)
            or os.path.realpath(turn["cwd"]) != scope["worktree"]["root"]
            or not isinstance(settings, dict)
            or set(settings) != {"model", "effort", "approval_policy", "sandbox_policy"}
            or any(not isinstance(settings.get(key), str) or not settings[key]
                   for key in ("model", "effort", "approval_policy"))
            or not isinstance(settings.get("sandbox_policy"), dict)
            or not settings["sandbox_policy"]):
        return False
    try:
        observed_at = datetime.fromisoformat(snapshot["observed_at"].replace("Z", "+00:00"))
        process_started = datetime.fromisoformat(process["started_at"].replace("Z", "+00:00"))
        turn_recorded = datetime.fromisoformat(turn["recorded_at"].replace("Z", "+00:00"))
    except ValueError:
        return False
    if turn_recorded <= process_started or turn_recorded > observed_at:
        return False
    snapshot_digest = _digest(snapshot)
    setting_evidence = {
        "source": "open_codex_rollout",
        "snapshot_digest": snapshot_digest,
        "turn_id": turn["id"],
        "recorded_at": turn["recorded_at"],
    }
    return (
        record.get("native_generation") == {
            "status": "observed", "value": process,
            "evidence": {
                "source": "ps", "snapshot_digest": snapshot_digest,
                "observed_at": snapshot["observed_at"],
            },
        }
        and record.get("native_predecessor_binding") == {
            "status": "observed", "hook_generation": snapshot["hook_generation"],
            "evidence": {
                "source": "cmux_codex_hook_store_v1", "snapshot_digest": snapshot_digest,
            },
        }
        and isinstance(observations, dict)
        and set(observations) == {
            "predecessor_model", "predecessor_effort", "successor_model",
            "successor_effort", "effective_permissions",
        }
        and observations.get("predecessor_model") == {
            "status": "observed", "value": settings["model"], "evidence": setting_evidence,
        }
        and observations.get("predecessor_effort") == {
            "status": "observed", "value": settings["effort"], "evidence": setting_evidence,
        }
        and observations.get("effective_permissions") == UNKNOWN
        and record.get("readiness") == {
            "status": "blocked", "reason": "effective_access_unobserved",
        }
    )


def _snapshot_bound_record(snapshot):
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("settings"), dict)
            or not isinstance(snapshot.get("process_identity"), dict)
            or not isinstance(snapshot.get("turn"), dict)):
        return {}
    settings = snapshot["settings"]
    process = snapshot["process_identity"]
    turn = snapshot["turn"]
    snapshot_digest = _digest(snapshot)
    evidence = {
        "source": "open_codex_rollout",
        "snapshot_digest": snapshot_digest,
        "turn_id": turn["id"],
        "recorded_at": turn["recorded_at"],
    }
    return {
        "native_generation": {
            "status": "observed", "value": process,
            "evidence": {"source": "ps", "snapshot_digest": snapshot_digest,
                         "observed_at": snapshot["observed_at"]},
        },
        "native_predecessor_binding": {
            "status": "observed", "hook_generation": snapshot["hook_generation"],
            "evidence": {"source": "cmux_codex_hook_store_v1", "snapshot_digest": snapshot_digest},
        },
        "observations": {
            "predecessor_model": {"status": "observed", "value": settings["model"], "evidence": evidence},
            "predecessor_effort": {"status": "observed", "value": settings["effort"], "evidence": evidence},
            "successor_model": UNKNOWN,
            "successor_effort": UNKNOWN,
            "effective_permissions": UNKNOWN,
        },
        "readiness": {"status": "blocked", "reason": "effective_access_unobserved"},
    }


def _choice_model_effort(record, predecessor_snapshot):
    choice = record.get("choice")
    if isinstance(choice, dict) and choice.get("kind") == "model_preferences_candidate":
        candidate = choice.get("snapshot")
        if isinstance(candidate, dict):
            return candidate.get("model"), candidate.get("effort")
    settings = predecessor_snapshot.get("settings") if isinstance(predecessor_snapshot, dict) else None
    if isinstance(choice, dict) and choice.get("kind") == "preserve_observed_predecessor":
        if isinstance(settings, dict):
            return settings.get("model"), settings.get("effort")
    return None, None


def _valid_opening_attempt(record, launch, predecessor_snapshot):
    if not isinstance(launch, dict) or set(launch) != {"status", "attempt", "returned_surface"}:
        return False
    attempt = launch.get("attempt")
    if (not isinstance(attempt, dict)
            or set(attempt) != {"id", "at", "request_digest", "native_command", "final_native_argv",
                                "cmux", "packet", "preflight"}
            or not isinstance(attempt.get("id"), str) or HEX_32.fullmatch(attempt["id"]) is None
            or not _timestamp(attempt.get("at"))
            or not isinstance(attempt.get("request_digest"), str)
            or HEX_64.fullmatch(attempt["request_digest"]) is None):
        return False
    native_command = attempt.get("native_command")
    model, effort = _choice_model_effort(record, predecessor_snapshot)
    if (record.get("scope", {}).get("seat", {}).get("harness") != "codex"
            or not isinstance(model, str) or not model or not isinstance(effort, str) or not effort
            or not isinstance(native_command, dict) or set(native_command) != {"executable", "argv"}
            or not isinstance(native_command.get("executable"), str)
            or not Path(native_command["executable"]).is_absolute()
            or Path(native_command["executable"]).name != "codex"
            or native_command.get("argv") != [native_command["executable"], "-m", model, "-c",
                                                 f'model_reasoning_effort="{effort}"']):
        return False
    packet = attempt.get("packet")
    if (not isinstance(packet, dict) or set(packet) != {"path", "bytes", "sha256"}
            or not isinstance(packet.get("path"), str) or not Path(packet["path"]).is_absolute()
            or type(packet.get("bytes")) is not int or not 0 < packet["bytes"] <= 48 * 1024
            or not isinstance(packet.get("sha256"), str) or HEX_64.fullmatch(packet["sha256"]) is None):
        return False
    expected_request_digest = _digest({"native_command": native_command, "packet": packet})
    if attempt.get("request_digest") != expected_request_digest:
        return False
    final_argv = attempt.get("final_native_argv")
    handoff_id = record.get("id")
    if not isinstance(handoff_id, str):
        return False
    expected_bootstrap = (
        f"You are a same-harness successor candidate for handoff {handoff_id}. "
        f"Read the packet at {packet['path']} only if its SHA-256 is {packet['sha256']}. "
        "Start with read-only preparation and print replicator guide --print. "
        "Do not edit files, dispatch work, contact agents, transfer custody, close the predecessor, "
        "or claim that permissions or trust have been verified."
    )
    if (not isinstance(final_argv, list) or final_argv != [*native_command["argv"], expected_bootstrap]):
        return False
    scope = record.get("scope", {})
    seat = scope.get("seat", {})
    worktree = scope.get("worktree", {})
    cmux = attempt.get("cmux")
    if (not isinstance(cmux, dict) or set(cmux) != {"executable", "argv", "cwd", "workspace_id"}
            or not isinstance(cmux.get("executable"), str)
            or not Path(cmux["executable"]).is_absolute()
            or Path(cmux["executable"]).name != "cmux"
            or cmux.get("cwd") != worktree.get("root")
            or cmux.get("workspace_id", "").upper() != seat.get("workspace_id", "").upper()):
        return False
    expected_cmux_argv = [
        cmux["executable"], "--json", "--id-format", "uuids", "new-surface", "--type", "terminal",
        "--workspace", seat["workspace_id"], "--working-directory", worktree["root"],
        "--focus", "false", "--command", shlex.join(final_argv),
    ]
    if cmux.get("argv") != expected_cmux_argv:
        return False
    returned = launch.get("returned_surface")
    if launch.get("status") == "opening_unconfirmed":
        if returned is not None:
            return False
    else:
        if (not isinstance(returned, dict) or set(returned) != {"surface_id", "workspace_id"}
                or not isinstance(returned.get("surface_id"), str)
                or not isinstance(returned.get("workspace_id"), str)):
            return False
        try:
            if (str(uuid.UUID(returned["surface_id"])) != returned["surface_id"]
                    or str(uuid.UUID(returned["workspace_id"])) != returned["workspace_id"]
                    or returned["workspace_id"].upper() != seat.get("workspace_id", "").upper()
                    or returned["surface_id"].upper() == seat.get("panel_id", "").upper()):
                return False
        except (ValueError, TypeError, AttributeError):
            return False
    preflight = attempt.get("preflight")
    preflight_fields = {
        "status", "checked_at", "checks", "predecessor_snapshot_digest", "access_observation_id",
        "access_observation_digest", "current_snapshot_digest", "current_native_snapshot",
        "current_hook_generation", "current_process_identity", "context_access_digest",
        "guidance_access_digest", "git_identity_digest", "packet_sha256",
    }
    check_names = {
        "frozen_intent", "predecessor_owner_binding", "predecessor_settings", "context_access",
        "guidance_access", "context_unchanged", "guidance_unchanged", "git_metadata_access", "packet_access",
    }
    if (not isinstance(preflight, dict) or set(preflight) != preflight_fields
            or preflight.get("status") != "ready" or not _timestamp(preflight.get("checked_at"))
            or not isinstance(preflight.get("checks"), dict)
            or set(preflight["checks"]) != check_names
            or any(value != "ready" for value in preflight["checks"].values())
            or preflight.get("predecessor_snapshot_digest") != _digest(predecessor_snapshot)
            or not isinstance(preflight.get("access_observation_id"), str)
            or HEX_32.fullmatch(preflight["access_observation_id"]) is None
            or preflight.get("packet_sha256") != packet["sha256"]):
        return False
    for name in ("access_observation_digest", "current_snapshot_digest", "context_access_digest",
                 "guidance_access_digest", "git_identity_digest"):
        if not isinstance(preflight.get(name), str) or HEX_64.fullmatch(preflight[name]) is None:
            return False
    current_snapshot = preflight.get("current_native_snapshot")
    if not isinstance(current_snapshot, dict):
        return False
    current_record = _snapshot_bound_record(current_snapshot)
    if not _valid_observed_snapshot(current_record, scope, current_snapshot, current_record["observations"]):
        return False
    if (preflight.get("current_snapshot_digest") != _digest(current_snapshot)
            or preflight.get("current_hook_generation") != current_snapshot.get("hook_generation")
            or preflight.get("current_process_identity") != current_snapshot.get("process_identity")
            or current_snapshot.get("subject") != predecessor_snapshot.get("subject")
            or current_snapshot.get("process_identity") != predecessor_snapshot.get("process_identity")
            or current_snapshot.get("hook_generation") != predecessor_snapshot.get("hook_generation")
            or current_snapshot.get("settings") != predecessor_snapshot.get("settings")):
        return False
    access = record.get("access_observations")
    if (not isinstance(access, dict) or set(access) != {"schema_version", "predecessor", "successor", "pending"}
            or access.get("schema_version") != 1 or access.get("pending") is not None
            or not isinstance(access.get("predecessor"), list) or not access["predecessor"]
            or not isinstance(access.get("successor"), list)):
        return False
    latest = access["predecessor"][-1]
    latest_checks = latest.get("checks") if isinstance(latest, dict) else None
    context_access = latest_checks.get("context_access") if isinstance(latest_checks, dict) else None
    guidance_access = latest_checks.get("guidance_access") if isinstance(latest_checks, dict) else None
    if (not isinstance(latest, dict) or latest.get("id") != preflight.get("access_observation_id")
            or _digest(latest) != preflight.get("access_observation_digest")
            or not isinstance(latest_checks, dict)
            or not isinstance(context_access, dict) or context_access.get("status") != "ready"
            or not isinstance(guidance_access, dict) or guidance_access.get("status") != "ready"
            or latest_checks.get("native_trust") != {
                "status": "unknown", "reason": "no_supported_exact_trust_source"}
            or latest_checks.get("effective_permissions") != {
                "status": "unknown", "reason": "scoped_checks_do_not_prove_complete_permissions"}
            or _digest(context_access) != preflight.get("context_access_digest")
            or _digest(guidance_access) != preflight.get("guidance_access_digest")
            or _digest(record.get("scope", {}).get("worktree")) != preflight.get("git_identity_digest")):
        return False
    return True


def _valid_successor_observation(record, scope, launch, predecessor_snapshot, observations):
    if (not isinstance(observations, dict)
            or observations.get("effective_permissions") != UNKNOWN):
        return False
    launch_status = launch.get("status") if isinstance(launch, dict) else None
    successor_snapshot = record.get("native_successor_snapshot")
    successor_generation = record.get("native_successor_generation", UNKNOWN)
    successor_binding = record.get("native_successor_binding", UNKNOWN_BINDING)
    if launch_status != "successor_observed":
        return (successor_snapshot is None and successor_generation == UNKNOWN
                and successor_binding == UNKNOWN_BINDING
                and observations.get("successor_model") == UNKNOWN
                and observations.get("successor_effort") == UNKNOWN)
    returned = launch.get("returned_surface")
    if not isinstance(returned, dict) or not isinstance(successor_snapshot, dict):
        return False
    subject = successor_snapshot.get("subject")
    if (not isinstance(subject, dict) or subject.get("harness") != "codex"
            or not isinstance(subject.get("session_id"), str) or not subject["session_id"]
            or subject.get("session_id") == scope["seat"].get("session_id")
            or subject.get("panel_id", "").upper() != returned.get("surface_id", "").upper()
            or subject.get("workspace_id", "").upper() != returned.get("workspace_id", "").upper()
            or subject.get("worktree") != scope.get("worktree")):
        return False
    successor_scope = {
        "seat": {**scope["seat"], "session_id": subject["session_id"],
                 "panel_id": subject["panel_id"], "workspace_id": subject["workspace_id"]},
        "worktree": scope["worktree"],
    }
    snapshot_record = _snapshot_bound_record(successor_snapshot)
    if not _valid_observed_snapshot(snapshot_record, successor_scope, successor_snapshot,
                                    snapshot_record["observations"]):
        return False
    process = successor_snapshot["process_identity"]
    predecessor_process = predecessor_snapshot.get("process_identity")
    hook_generation = successor_snapshot["hook_generation"]
    if (process == predecessor_process
            or hook_generation == predecessor_snapshot.get("hook_generation")):
        return False
    snapshot_digest = _digest(successor_snapshot)
    expected_generation = {
        "status": "observed", "value": process,
        "evidence": {"source": "ps", "snapshot_digest": snapshot_digest,
                     "observed_at": successor_snapshot["observed_at"]},
    }
    expected_binding = {
        "status": "observed", "hook_generation": hook_generation,
        "evidence": {"source": "cmux_codex_hook_store_v1", "snapshot_digest": snapshot_digest},
    }
    turn = successor_snapshot["turn"]
    evidence = {"source": "open_codex_rollout", "snapshot_digest": snapshot_digest,
                "turn_id": turn["id"], "recorded_at": turn["recorded_at"]}
    return (
        successor_generation == expected_generation
        and successor_binding == expected_binding
        and observations.get("successor_model") == {
            "status": "observed", "value": successor_snapshot["settings"]["model"], "evidence": evidence,
        }
        and observations.get("successor_effort") == {
            "status": "observed", "value": successor_snapshot["settings"]["effort"], "evidence": evidence,
        }
    )


def _validate_launch_state(record, scope, predecessor_snapshot, observations):
    launch = record.get("launch")
    if launch == {"status": "not_started"}:
        if (record.get("native_successor_snapshot") is not None
                or record.get("native_successor_generation", UNKNOWN) != UNKNOWN
                or record.get("native_successor_binding") != UNKNOWN_BINDING
                or observations.get("successor_model") != UNKNOWN
                or observations.get("successor_effort") != UNKNOWN):
            raise HandoffAdapterError("Replicator handoff has malformed successor observation state.")
    elif (not isinstance(launch, dict)
          or launch.get("status") not in ("opening_unconfirmed", "opened_unobserved", "successor_observed")
          or not _valid_opening_attempt(record, launch, predecessor_snapshot)):
        raise HandoffAdapterError("Replicator handoff has malformed candidate opening evidence.")
    if not _valid_successor_observation(record, scope, launch, predecessor_snapshot, observations):
        raise HandoffAdapterError("Replicator handoff has malformed successor native observation evidence.")


def _validate_handoff(record, *, intent=None, handoff_id=None):
    if (not isinstance(record, dict) or record.get("schema_version") != 1
            or record.get("protocol") != HANDOFF_PROTOCOL
            or record.get("state") != "frozen"
            or not isinstance(record.get("id"), str)
            or HEX_32.fullmatch(record["id"]) is None
            or handoff_id is not None and record["id"] != handoff_id
            or not isinstance(record.get("handoff_generation"), str)
            or HEX_32.fullmatch(record["handoff_generation"]) is None
            or record["handoff_generation"] == record["id"]
            or not isinstance(record.get("intent_id"), str)
            or HEX_32.fullmatch(record["intent_id"]) is None
            or not isinstance(record.get("intent_digest"), str)
            or HEX_64.fullmatch(record["intent_digest"]) is None):
        raise HandoffAdapterError("Replicator handoff record has an unsupported protocol or identity.")
    snapshot = record.get("intent_snapshot")
    expected_snapshot_keys = {
        "schema_version", "protocol", "id", "scope", "policy", "choice", "request_digest",
        "state", "native_generation", "observations", "permission_requirement", "readiness",
        "custody_acceptance", "predecessor_cleanup",
    }
    expected_observations = {
        "predecessor_model", "predecessor_effort", "effective_permissions",
    }
    if (not isinstance(snapshot, dict)
            or set(snapshot) != expected_snapshot_keys
            or snapshot.get("schema_version") != 1
            or snapshot.get("protocol") != INTENT_PROTOCOL
            or snapshot.get("id") != record["intent_id"]
            or snapshot.get("policy") != "once"
            or snapshot.get("state") != "configured"
            or not isinstance(snapshot.get("request_digest"), str)
            or HEX_64.fullmatch(snapshot["request_digest"]) is None
            or snapshot.get("request_digest") != _digest({
                "protocol": INTENT_PROTOCOL,
                "policy": "once",
                "scope": snapshot.get("scope"),
                "choice": snapshot.get("choice"),
            })
            or snapshot.get("native_generation") != UNKNOWN
            or not isinstance(snapshot.get("observations"), dict)
            or set(snapshot["observations"]) != expected_observations
            or any(value != UNKNOWN for value in snapshot["observations"].values())
            or snapshot.get("permission_requirement") != {
                "kind": "match_predecessor_effective", "status": "unknown"}
            or snapshot.get("readiness") != {"status": "unknown"}
            or snapshot.get("custody_acceptance") != {"status": "not_applicable"}
            or snapshot.get("predecessor_cleanup") != {"status": "not_applicable"}
            or record.get("intent_digest") != _digest(snapshot)
            or record.get("scope") != snapshot.get("scope")
            or record.get("choice") != snapshot.get("choice")):
        raise HandoffAdapterError("Replicator handoff snapshot is malformed or does not match its digest.")
    scope = record.get("scope")
    if (not isinstance(scope, dict) or set(scope) != {"seat", "worktree"}
            or not isinstance(scope.get("seat"), dict)
            or set(scope["seat"]) != {"name", "harness", "session_id", "workspace_id", "panel_id"}
            or not all(isinstance(scope["seat"].get(key), str) and scope["seat"][key]
                       for key in ("name", "harness", "session_id", "workspace_id", "panel_id"))
            or scope["seat"].get("harness") not in ("claude", "codex", "muse")
            or not isinstance(scope.get("worktree"), dict)
            or set(scope["worktree"]) != {"root", "git_dir", "common_dir"}
            or not all(isinstance(scope["worktree"].get(key), str) and scope["worktree"][key]
                       for key in ("root", "git_dir", "common_dir"))
            or record["handoff_generation"] == scope["seat"]["session_id"]):
        raise HandoffAdapterError("Replicator handoff scope or generation binding is malformed.")
    if intent is not None:
        if (record.get("intent_id") != intent["id"]
                or record.get("scope") != intent["scope"]
                or record.get("choice") != intent["choice"]
                or snapshot.get("id") != intent["id"]
                or snapshot.get("scope") != intent["scope"]
                or snapshot.get("choice") != intent["choice"]
                or snapshot.get("request_digest") != intent["request_digest"]):
            raise HandoffAdapterError("Replicator handoff does not preserve the exact configured intent.")
    observations = record.get("observations")
    expected_observations = {"predecessor_model", "predecessor_effort", "successor_model",
                             "successor_effort", "effective_permissions"}
    native_snapshot = record.get("native_predecessor_snapshot")
    if native_snapshot is None:
        if (record.get("native_generation") != UNKNOWN
                or record.get("native_predecessor_binding") != UNKNOWN_BINDING
                or record.get("native_successor_binding") != UNKNOWN_BINDING
                or record.get("native_successor_generation", UNKNOWN) != UNKNOWN
                or record.get("native_successor_snapshot") is not None
                or not isinstance(observations, dict)
                or set(observations) != expected_observations
                or any(observations.get(key) != UNKNOWN for key in observations)
                or record.get("readiness") != {
                    "status": "blocked", "reason": "native_observations_unavailable"}):
            raise HandoffAdapterError("Replicator handoff has malformed native observation state.")
    else:
        if not _valid_observed_snapshot(record, scope, native_snapshot, observations):
            raise HandoffAdapterError("Replicator handoff has malformed native observation state.")
    _validate_launch_state(record, scope, native_snapshot, observations)
    if (record.get("permission_requirement") != {
                "kind": "match_predecessor_effective", "status": "unknown"}
            or record.get("transfer") != {"status": "not_applicable"}
            or record.get("predecessor_cleanup") != {"status": "not_applicable"}):
        raise HandoffAdapterError("Replicator handoff has an unsupported readiness or lifecycle state.")
    return record


def _legacy_record(app, handoff_id):
    path = app.state_dir / "handoffs" / f"{handoff_id}.json"
    if path.is_symlink():
        raise HandoffAdapterError("Model Preferences handoff state is a symlink and cannot be routed safely.")
    if not path.exists():
        return None
    record = app.state(f"handoffs/{handoff_id}", None)
    if (not isinstance(record, dict) or record.get("id") != handoff_id
            or "protocol" in record or "schema_version" in record):
        raise HandoffAdapterError("Model Preferences handoff record is malformed or already versioned.")
    return record


def _canonical_request_scope(app, args):
    recipients = app.state("orchestrators", {})
    outgoing = recipients.get(args.name) if isinstance(recipients, dict) else None
    if (not isinstance(outgoing, dict)
            or not all(isinstance(outgoing.get(key), str) and outgoing[key]
                       for key in ("workspace", "surface", "session", "harness"))
            or outgoing.get("harness") not in ("claude", "codex", "muse")):
        raise HandoffAdapterError("The exact registered outgoing Model Preferences seat is unavailable.")
    try:
        seat = {
            "name": args.name,
            "harness": outgoing["harness"],
            "session_id": outgoing["session"],
            "workspace_id": str(uuid.UUID(outgoing["workspace"])),
            "panel_id": str(uuid.UUID(outgoing["surface"])),
        }
        worktree = Path(args.cwd).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HandoffAdapterError("The requested seat or worktree identity is unavailable.") from exc
    if not worktree.is_dir():
        raise HandoffAdapterError("The requested handoff worktree is not a directory.")
    return seat, str(worktree)


def _validate_brief_and_roadmap(args):
    try:
        brief = Path(args.brief).expanduser().read_bytes()
        roadmap = Path(args.roadmap).expanduser().read_bytes()
    except OSError as exc:
        raise HandoffAdapterError("The handoff briefing or roadmap could not be read.") from exc
    if not brief.strip() or not roadmap.strip() or len(brief) + len(roadmap) > 48 * 1024:
        raise HandoffAdapterError("Provide non-empty briefing and roadmap files within 48 KiB total.")


def _replicator_scope_options(worktree, config_dir, state_dir):
    return ["--worktree", worktree, "--mp-config-dir", str(Path(config_dir).resolve()),
            "--mp-state-dir", str(Path(state_dir).resolve())]


def _replicator_state_options(state_dir):
    if state_dir is None:
        return []
    try:
        resolved = Path(state_dir).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HandoffAdapterError("The requested Replicator state directory cannot be resolved.") from exc
    return ["--state-dir", str(resolved)]


def start(app, args, runner=None):
    _validate_brief_and_roadmap(args)
    seat, worktree = _canonical_request_scope(app, args)
    state_options = _replicator_state_options(getattr(args, "replicator_state_dir", None))
    show_arguments = [*state_options,
        "successor", "show", "--seat", args.name, "--worktree", worktree,
        "--mp-config-dir", str(app.config_dir.resolve()),
        "--mp-state-dir", str(app.state_dir.resolve()), "--json",
    ]
    report = _read_json(show_arguments, runner=runner)
    intent = _validate_intent_report(report, args.name, worktree)
    if intent["scope"]["seat"] != seat:
        raise HandoffAdapterError("The configured Replicator intent belongs to a different registered seat.")
    freeze_arguments = [*state_options, "handoff", "freeze", intent["id"],
                        *_replicator_scope_options(worktree, app.config_dir, app.state_dir), "--json"]
    frozen_report = _read_json(freeze_arguments, runner=runner)
    if set(frozen_report) != {"handoff", "reused"} or type(frozen_report.get("reused")) is not bool:
        raise HandoffAdapterError("Replicator freeze response has an unsupported protocol.")
    handoff = _validate_handoff(frozen_report.get("handoff"), intent=intent)
    legacy = _legacy_record(app, handoff["id"])
    if legacy is not None:
        raise HandoffAdapterError("Handoff ID exists in both Replicator and legacy Model Preferences state; both records were preserved.")
    return {
        "schema_version": 1,
        "protocol": ADAPTER_PROTOCOL,
        "status": "blocked",
        "handoff": {
            "handle": _public_handle(handoff["id"]),
            "engine_id": handoff["id"],
            "record": handoff,
        },
        "reused": frozen_report["reused"],
        "native_launch_performed": False,
        "briefing_recorded": False,
        "next_step": "Replicator froze this one-shot intent, but native launch and receiving are not implemented; retain the outgoing owner.",
        "exit_code": 2,
    }


def route_transition(app, handoff_id, runner=None, replicator_state_dir=None):
    engine_id = _engine_id(handoff_id)
    if engine_id is None:
        if not isinstance(handoff_id, str) or HEX_32.fullmatch(handoff_id) is None:
            raise HandoffAdapterError("Invalid handoff handle.")
        legacy = _legacy_record(app, handoff_id)
        if legacy is None:
            raise HandoffAdapterError("Unknown legacy handoff; use its versioned Replicator handle if applicable.")
        if legacy.get("status") in UNCERTAIN_LEGACY_STATES:
            raise HandoffAdapterError("Uncertain legacy handoffs remain inspect-only and cannot be replayed.")
        return "legacy"

    legacy = _legacy_record(app, engine_id)
    record = _lookup_versioned_handoff(engine_id, runner=runner, replicator_state_dir=replicator_state_dir)
    if record is None:
        raise HandoffAdapterError("The versioned Replicator handoff handle has no matching handoff record.")
    if legacy is not None:
        raise HandoffAdapterError("Handoff ID exists in both Replicator and legacy Model Preferences state; both records were preserved.")
    raise HandoffAdapterError(
        f"Replicator handoff {engine_id} is frozen with readiness blocked; Model Preferences will not run a legacy transition."
    )


def _lookup_versioned_handoff(handoff_id, runner=None, replicator_state_dir=None):
    arguments = [*_replicator_state_options(replicator_state_dir), "handoff", "show", handoff_id, "--json"]
    result = _call(arguments, runner=runner)
    if _unknown_handoff_result(result):
        return None
    report = _json_result(result)
    if (report.get("schema_version") != 1 or report.get("protocol") != HANDOFF_PROTOCOL
            or set(report) != {"schema_version", "protocol", "handoff"}):
        raise HandoffAdapterError("Replicator handoff response has an unsupported versioned protocol.")
    handoff = _validate_handoff(report.get("handoff"), handoff_id=handoff_id)
    return handoff
