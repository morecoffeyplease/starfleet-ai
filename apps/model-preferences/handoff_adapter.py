"""Versioned delegation from Model Preferences to Replicator handoff records."""

import hashlib
import json
import re
from pathlib import Path
import shutil
import subprocess
import uuid


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
    if (record.get("native_generation") != UNKNOWN
            or record.get("native_predecessor_binding") != UNKNOWN_BINDING
            or record.get("native_successor_binding") != UNKNOWN_BINDING):
        raise HandoffAdapterError("Replicator handoff claims unsupported native generation evidence.")
    observations = record.get("observations")
    if (not isinstance(observations, dict)
            or set(observations) != {"predecessor_model", "predecessor_effort", "successor_model",
                                     "successor_effort", "effective_permissions"}
            or any(observations.get(key) != UNKNOWN for key in observations)
            or record.get("permission_requirement") != {
                "kind": "match_predecessor_effective", "status": "unknown"}
            or record.get("readiness") != {
                "status": "blocked", "reason": "native_observations_unavailable"}
            or record.get("launch") != {"status": "not_started"}
            or record.get("transfer") != {"status": "not_applicable"}
            or record.get("predecessor_cleanup") != {"status": "not_applicable"}):
        raise HandoffAdapterError("Replicator handoff does not match the supported freeze-only state.")
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
        try:
            record = _lookup_versioned_handoff(
                handoff_id, runner=runner, replicator_state_dir=replicator_state_dir)
        except ReplicatorUnavailable:
            return "legacy"
        if record is not None:
            raise HandoffAdapterError("Handoff ID exists in both Replicator and legacy Model Preferences state; both records were preserved.")
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
