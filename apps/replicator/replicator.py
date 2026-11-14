#!/usr/bin/env python3
"""Print the Replicator handoff guide or a read-only readiness report."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import uuid

import access_observer
import successor_intents
import handoff_engine
import native_observer


STATUSES = ("ready", "blocked", "unknown", "skipped")
ACTIVE_CMUX_STATES = ("working", "idle")
MAX_COMMAND_OUTPUT = 2 * 1024 * 1024
MAX_INPUT_BYTES = 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 5
TOOL_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}\Z")
MAX_MODEL_PREFERENCES_OUTPUT = 256 * 1024
MAX_HANDOFF_PACKET_BYTES = 48 * 1024


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    source: str
    next_step: str

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError("Unsupported check status")

    def as_dict(self):
        return {
            "name": self.name,
            "status": self.status,
            "source": self.source,
            "next_step": self.next_step,
        }


def aggregate_statuses(statuses):
    statuses = set(statuses) - {"skipped"}
    if "blocked" in statuses:
        return "blocked"
    if "unknown" in statuses or not statuses:
        return "unknown"
    return "ready"


def aggregate(checks):
    return aggregate_statuses(check.status for check in checks)


def build_report(checks, caller=None):
    return {
        "schema_version": 1,
        "caller": caller,
        "operational_readiness": aggregate(checks),
        "custody_acceptance": "not_assessed",
        "predecessor_cleanup": "not_assessed",
        "native_delivery": "not_assessed",
        "pilot": "not_assessed",
        "checks": [check.as_dict() for check in checks],
    }


def read_input_bytes(path):
    with Path(path).expanduser().open("rb") as stream:
        return stream.read(MAX_INPUT_BYTES + 1)


def input_check(name, paths, option):
    if not paths:
        return Check(name, "unknown", "No explicit input was provided.", f"Pass a readable file with {option}.")

    statuses = []
    for path in paths:
        try:
            data = read_input_bytes(path)
        except FileNotFoundError:
            statuses.append("unknown")
        except OSError:
            statuses.append("blocked")
        else:
            statuses.append("ready" if data and len(data) <= MAX_INPUT_BYTES else "unknown")

    status = aggregate_statuses(statuses)
    if status == "ready":
        return Check(
            name,
            status,
            f"Bytes were read from explicit {option} input files.",
            "Keep this input current as the authorized handoff changes.",
        )
    if status == "blocked":
        return Check(
            name,
            status,
            f"A read of an explicit {option} input failed.",
            "Keep dependent work blocked and repeat the same read after access recovers. Do not change permissions from this result.",
        )
    return Check(
        name,
        status,
        f"An explicit {option} input is missing, empty, or exceeds the read limit.",
        f"Provide an existing, non-empty file with {option}. The report omits file paths and contents.",
    )


def run_bounded(argv, cwd=None, *, closed_stdin=False):
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL if closed_stdin else None,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable", None
    if len(result.stdout.encode("utf-8", "replace")) > MAX_COMMAND_OUTPUT:
        return "unavailable", None
    if result.returncode != 0:
        return "failed", None
    return "ready", result.stdout


def identity_problem(source, next_step, status="unknown"):
    return None, None, Check("caller_identity", status, source, next_step)


def caller_identity(environment):
    candidates = [
        (harness, environment.get(variable))
        for harness, variable in (
            ("claude", "CLAUDE_CODE_SESSION_ID"),
            ("codex", "CODEX_THREAD_ID"),
        )
        if environment.get(variable)
    ]
    opencode_session = environment.get("OPENCODE_SESSION_ID")
    opencode_launch = environment.get("CMUX_AGENT_LAUNCH_KIND") == "opencode"
    if opencode_session or opencode_launch:
        if not opencode_session or not opencode_launch:
            return identity_problem(
                "The exact OpenCode caller identity is incomplete.",
                "Run preflight from the exact managed OpenCode session with its supported identity.",
            )
        candidates.append(("opencode", opencode_session))
    if len(candidates) != 1:
        return identity_problem(
            "The process does not expose one exact supported native session identity.",
            "Run preflight from the exact live Claude, Codex, or managed OpenCode session.",
        )

    harness, session_id = candidates[0]
    panel_id = environment.get("CMUX_SURFACE_ID")
    workspace_id = environment.get("CMUX_WORKSPACE_ID")
    if not panel_id or not workspace_id:
        return identity_problem(
            "The process does not expose its CMUX panel and workspace identities.",
            "Run preflight from the exact live CMUX panel and workspace.",
        )
    cmux = shutil.which("cmux")
    if cmux is None:
        return identity_problem(
            "The read-only CMUX current command is unavailable.",
            "Use the existing CMUX installation and repeat preflight from the exact live caller.",
        )

    status, output = run_bounded([cmux, "current", "--json", "--limit", "200"])
    if status != "ready":
        return identity_problem(
            "CMUX current did not provide a complete read-only observation.",
            "Repeat the same read-only observation after CMUX access recovers. Do not infer the refusal cause or use another session's identity.",
        )
    try:
        current = json.loads(output)
    except (TypeError, ValueError):
        return identity_problem(
            "CMUX current returned an unreadable identity snapshot.",
            "Repeat the read-only observation and resolve the exact caller identity before handoff.",
        )

    availability = current.get("owner_availability") if isinstance(current, dict) else None
    items = current.get("items") if isinstance(current, dict) else None
    if (
        not isinstance(current, dict)
        or current.get("truncated") is not False
        or not isinstance(availability, dict)
        or availability.get("agent_sessions") != "available"
        or not isinstance(items, list)
    ):
        return identity_problem(
            "CMUX current did not provide complete agent identity evidence.",
            "Resolve the exact live session and panel through supported CMUX evidence.",
        )

    matches = []
    for item in items:
        agents = item.get("agents") if isinstance(item, dict) else None
        projections = item.get("projections") if isinstance(item, dict) else None
        if (
            not isinstance(agents, list)
            or not isinstance(projections, list)
            or any(not isinstance(agent, dict) for agent in agents)
            or any(not isinstance(projection, dict) for projection in projections)
        ):
            return identity_problem(
                "CMUX current identity evidence is incomplete or ambiguous.",
                "Resolve the exact caller session and panel without using a label or sibling session.",
            )
        matches.extend(
            (item, agent, projections)
            for agent in agents
            if agent.get("kind") == harness and agent.get("session_id") == session_id
        )
    if len(matches) != 1:
        return identity_problem(
            "CMUX current identity evidence is missing or ambiguous.",
            "Resolve the exact caller session and panel without using a label or sibling session.",
        )

    item, agent, projections = matches[0]
    placement = item.get("placement")
    if not isinstance(placement, dict) or placement.get("kind") != "local":
        return identity_problem(
            "CMUX current did not confirm a supported local caller projection.",
            "Use the exact supported local caller session and its current panel.",
        )
    panels = [
        projection for projection in projections
        if isinstance(projection.get("panel_id"), str)
        and projection["panel_id"].upper() == panel_id.upper()
    ]
    if len(panels) != 1:
        return identity_problem(
            "CMUX current did not identify one projection for the caller's panel.",
            "Resolve the exact live panel through supported CMUX evidence.",
        )
    observed_workspace = panels[0].get("workspace_id")
    if not isinstance(observed_workspace, str) or observed_workspace.upper() != workspace_id.upper():
        return identity_problem(
            "The live caller panel does not match the current process workspace.",
            "Resolve the exact live panel and workspace before dependent handoff work.",
        )
    if agent.get("state") not in ACTIVE_CMUX_STATES:
        return identity_problem(
            "CMUX current reports that the exact caller is not working or idle.",
            "Resolve the session's native hold or inactive state before dependent handoff work.",
            "blocked",
        )
    native_cwd = item.get("cwd")
    if not isinstance(native_cwd, str) or not native_cwd:
        return identity_problem(
            "CMUX current did not provide the exact caller working directory.",
            "Resolve the caller's current worktree through supported CMUX evidence.",
        )

    caller = {
        "harness": harness,
        "session_id": session_id,
        "panel_id": panels[0]["panel_id"],
        "workspace_id": observed_workspace,
    }
    return native_cwd, caller, Check(
        "caller_identity",
        "ready",
        "The process identity matches one live CMUX session, panel, and workspace.",
        "Continue with the separate custody and acceptance steps in the handoff guide.",
    )


def git_value(git, cwd, *arguments):
    status, output = run_bounded([git, "-C", str(cwd), *arguments])
    if status != "ready" or output is None:
        return status, None
    lines = output.splitlines()
    return ("ready", lines[0]) if len(lines) == 1 and lines[0] else ("unavailable", None)


def resolve_path(value, base=None, strict=True):
    path = Path(value)
    if not path.is_absolute() and base is not None:
        path = base / path
    return path.resolve(strict=strict)


def git_directories(git, root):
    git_status, git_text = git_value(git, root, "rev-parse", "--absolute-git-dir")
    common_status, common_text = git_value(git, root, "rev-parse", "--git-common-dir")
    if git_status != "ready" or common_status != "ready":
        status = "blocked" if "failed" in (git_status, common_status) else "unknown"
        return status, None
    try:
        git_dir = resolve_path(git_text, root)
        common_dir = resolve_path(common_text, root)
    except (OSError, RuntimeError, ValueError):
        return "unknown", None
    if git_dir != common_dir and common_dir not in git_dir.parents:
        return "blocked", None
    return "ready", (git_dir, common_dir)


def worktree_check(native_cwd):
    def result(status, source, next_step):
        return Check("worktree_git_identity", status, source, next_step)

    try:
        caller_cwd = Path.cwd().resolve(strict=True)
    except (OSError, RuntimeError):
        return result("unknown", "The current process working directory is unavailable.", "Run preflight from the exact requested worktree.")
    if not native_cwd:
        return result("unknown", "The exact native caller working directory is unavailable.", "Resolve the exact caller through CMUX before assessing its Git worktree.")
    try:
        native_path = Path(native_cwd).resolve(strict=True)
    except FileNotFoundError:
        return result("unknown", "The CMUX caller working directory is missing.", "Resolve the caller's current worktree and repeat the read-only Git checks.")
    except PermissionError:
        return result("blocked", "The CMUX caller working directory could not be read.", "Repeat the same access check after access recovers. Do not change permissions or Git metadata.")
    except (OSError, RuntimeError):
        return result("unknown", "The CMUX caller working directory could not be resolved.", "Resolve the exact caller worktree and repeat the read-only Git checks.")

    git = shutil.which("git")
    if git is None:
        return result("unknown", "The read-only Git command is unavailable.", "Use the existing Git installation and repeat preflight from the target worktree.")
    roots = []
    for path in (caller_cwd, native_path):
        status, value = git_value(git, path, "rev-parse", "--show-toplevel")
        if status != "ready":
            if status == "failed":
                return result("blocked", "A required read-only Git root query failed. The cause is unknown.", "Repeat the same bounded checks after access recovers. Do not repair Git metadata from this result.")
            return result("unknown", "A complete read-only Git root observation is unavailable.", "Repeat preflight from the exact requested worktree.")
        try:
            roots.append(resolve_path(value))
        except (OSError, RuntimeError, ValueError):
            return result("unknown", "A Git root path could not be resolved.", "Resolve the exact worktree path and repeat the read-only Git checks.")
    if roots[0] != roots[1]:
        return result("blocked", "The current process and exact CMUX caller resolve to different Git roots.", "Run preflight from the authorized target worktree and retain predecessor custody until the roots match.")

    status, directories = git_directories(git, roots[0])
    if status != "ready":
        if status == "blocked":
            return result("blocked", "A required Git directory consistency check failed. The cause is unknown.", "Repeat the same bounded checks after access recovers. Do not repair Git metadata from this result.")
        return result("unknown", "Git root or common-directory evidence is incomplete.", "Resolve the exact worktree and common repository directory before handoff.")

    status, output = run_bounded([git, "-C", str(roots[0]), "worktree", "list", "--porcelain"])
    if status != "ready" or output is None:
        if status == "failed":
            return result("blocked", "The required read-only Git worktree query failed. The cause is unknown.", "Repeat the same bounded checks after access recovers. Do not repair Git metadata from this result.")
        return result("unknown", "The complete Git worktree list is unavailable.", "Resolve the target worktree identity through the read-only Git commands.")
    listed = []
    for line in output.splitlines():
        if line.startswith("worktree "):
            try:
                listed.append(resolve_path(line[len("worktree "):], strict=False))
            except (OSError, RuntimeError, ValueError):
                continue
    matches = [path for path in listed if path == roots[0]]
    if len(matches) != 1:
        status = "blocked" if not matches else "unknown"
        return result(status, "The Git worktree list did not identify one exact target worktree.", "Resolve the target worktree identity before handoff. Do not run Git repair commands from preflight.")
    return result(
        "ready",
        "Git root, worktree git directory, common directory, and worktree list agree for the exact caller.",
        "Repeat these read-only checks after access recovery or a worktree change.",
    )


def native_configuration_checks(identity_status):
    source = (
        "CMUX current confirms the exact session but provides no supported setting observation."
        if identity_status == "ready"
        else "The exact native caller could not be scoped to authoritative configuration evidence."
    )
    return [
        Check("native_model", "unknown", source, "Verify the actual provider and model through supported native controls before transferring custody."),
        Check("native_effort", "unknown", source, "Verify the actual reasoning effort through supported native controls before transferring custody."),
        Check("native_permissions", "unknown", source, "Verify the exact session's effective permissions through supported native controls before transferring custody."),
    ]


def skipped_tool_checks(tool_names):
    checks = []
    for name in sorted(set(tool_names)):
        if not TOOL_NAME.fullmatch(name):
            raise ValueError("Tool exclusions must use letters, numbers, dots, underscores, or hyphens")
        checks.append(Check(
            f"tool_exclusion:{name}",
            "skipped",
            "The caller explicitly excluded this tool from preflight.",
            "Keep this exclusion in the handoff record. Do not enroll or auto-register this tool.",
        ))
    return checks


def preflight_report(context_files=(), guidance_files=(), skipped_tools=(), environment=None):
    environment = os.environ if environment is None else environment
    native_cwd, caller, identity_check = caller_identity(environment)
    checks = [
        identity_check,
        worktree_check(native_cwd),
        input_check("context_access", context_files, "--context-file"),
        input_check("guidance_access", guidance_files, "--guidance-file"),
        *native_configuration_checks(identity_check.status),
        *skipped_tool_checks(skipped_tools),
    ]
    return build_report(checks, caller)


def canonical_worktree_identity(value):
    try:
        worktree = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise successor_intents.IntentError("Worktree is unavailable or cannot be resolved.") from exc
    if not worktree.is_dir():
        raise successor_intents.IntentError("Worktree must be an existing Git worktree root.")
    git = shutil.which("git")
    if git is None:
        raise successor_intents.IntentError("The read-only Git command is unavailable.")
    status, root_text = git_value(git, worktree, "rev-parse", "--show-toplevel")
    if status != "ready":
        raise successor_intents.IntentError("The exact Git worktree root could not be confirmed.")
    try:
        root = resolve_path(root_text)
    except (OSError, RuntimeError, ValueError) as exc:
        raise successor_intents.IntentError("The Git worktree root could not be resolved.") from exc
    if root != worktree:
        raise successor_intents.IntentError("Pass the exact Git worktree root, not a nested path.")
    status, directories = git_directories(git, root)
    if status != "ready" or directories is None:
        raise successor_intents.IntentError("The exact Git worktree metadata could not be confirmed.")
    git_dir, common_dir = directories
    return {"root": str(root), "git_dir": str(git_dir), "common_dir": str(common_dir)}


def model_preferences_successor_choice(args):
    executable = shutil.which("model-preferences")
    if executable is None:
        raise successor_intents.IntentError("The configured Model Preferences command is unavailable.")
    family = getattr(args, "family", None)
    effort = getattr(args, "effort", None)
    argv = [executable]
    if args.mp_config_dir:
        argv.extend(("--config-dir", args.mp_config_dir))
    if args.mp_state_dir:
        argv.extend(("--state-dir", args.mp_state_dir))
    argv.extend(("resolve-successor", "--seat", args.seat))
    if family is not None:
        argv.extend(("--family", family, "--effort", effort))
    status, output = run_bounded(argv)
    if status != "ready" or output is None:
        raise successor_intents.IntentError("Model Preferences could not resolve this successor scope and choice.")
    if len(output.encode("utf-8", "replace")) > MAX_MODEL_PREFERENCES_OUTPUT:
        raise successor_intents.IntentError("Model Preferences returned an oversized successor resolution.")
    try:
        result = json.loads(output)
    except (TypeError, ValueError) as exc:
        raise successor_intents.IntentError("Model Preferences returned an unreadable successor resolution.") from exc
    if (not isinstance(result, dict) or result.get("schema_version") != 1
            or result.get("protocol") != "starfleet.model-preferences.successor-choice/v1"):
        raise successor_intents.IntentError("Model Preferences returned an unsupported successor resolution.")
    active_profile = result.get("active_profile")
    active_selection_digest = result.get("active_selection_digest")
    if (active_profile is not None and (not isinstance(active_profile, str) or not active_profile)
            or (active_profile is None) != (active_selection_digest is None)
            or active_selection_digest is not None
            and (not isinstance(active_selection_digest, str) or len(active_selection_digest) != 64
                 or any(character not in "0123456789abcdef" for character in active_selection_digest))):
        raise successor_intents.IntentError("Model Preferences returned invalid profile provenance.")
    seat = result.get("seat")
    if (not isinstance(seat, dict) or seat.get("name") != args.seat
            or not all(isinstance(seat.get(key), str) and seat[key]
                       for key in ("harness", "session_id", "workspace_id", "panel_id"))):
        raise successor_intents.IntentError("Model Preferences did not return one complete configured seat.")
    choice = result.get("choice")
    if family is None:
        if choice is not None or result.get("candidate_matches_active_orchestrator") is not None:
            raise successor_intents.IntentError("Model Preferences returned an unexpected model choice.")
    elif (not isinstance(choice, dict) or choice.get("family") != family
          or choice.get("effort") != effort or choice.get("harness") != seat["harness"]):
        raise successor_intents.IntentError("The configured choice is unsupported for this exact seat and harness.")
    elif ((active_profile is None) != (result.get("candidate_matches_active_orchestrator") is None)
          or result.get("candidate_matches_active_orchestrator") is not None
          and type(result["candidate_matches_active_orchestrator"]) is not bool):
        raise successor_intents.IntentError("Model Preferences returned invalid candidate comparison provenance.")
    return result


def successor_scope(args):
    resolved = model_preferences_successor_choice(args)
    worktree = canonical_worktree_identity(args.worktree)
    scope = {"seat": resolved["seat"], "worktree": worktree}
    family = getattr(args, "family", None)
    if family is None:
        choice = {"kind": "preserve_observed_predecessor"}
    else:
        choice = {
            "kind": "model_preferences_candidate",
            "snapshot": resolved["choice"],
            "active_profile": resolved["active_profile"],
            "active_selection_digest": resolved.get("active_selection_digest"),
            "candidate_matches_active_orchestrator": resolved.get("candidate_matches_active_orchestrator"),
            "candidate_digest": resolved.get("candidate_digest"),
            "provenance": resolved.get("provenance"),
        }
        if not isinstance(choice["candidate_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", choice["candidate_digest"]):
            raise successor_intents.IntentError("Model Preferences did not provide candidate provenance evidence.")
    return scope, choice


def successor_configure(args):
    if (args.family is None) != (args.effort is None):
        raise successor_intents.IntentError("Pass both --family and --effort for an explicit model choice.")
    scope, choice = successor_scope(args)
    return successor_intents.configure_intent(args.state_dir, scope, choice)


def successor_show(args):
    if getattr(args, "family", None) is not None or getattr(args, "effort", None) is not None:
        raise successor_intents.IntentError("Do not pass a model choice to successor show.")
    scope, _ = successor_scope(args)
    return successor_intents.show_intent(args.state_dir, scope)


def handoff_freeze(args):
    store = successor_intents.read_store(args.state_dir)
    _key, _index, intent = successor_intents._find_intent(store, args.intent_id)
    if intent is None:
        raise handoff_engine.HandoffError("Unknown successor intent.")
    worktree = canonical_worktree_identity(args.worktree)
    if worktree != intent["scope"]["worktree"]:
        raise handoff_engine.HandoffError("The canonical worktree does not match this exact successor intent.")
    existing = handoff_engine.existing_handoff_for_intent(args.state_dir, args.intent_id)
    if existing is not None:
        return {"handoff": existing, "reused": True}

    choice = intent["choice"]
    expected_choice = None
    family = None
    effort = None
    if choice["kind"] == "model_preferences_candidate":
        expected_choice = choice["snapshot"]
        family = expected_choice["family"]
        effort = expected_choice["effort"]
    resolve_args = argparse.Namespace(
        seat=intent["scope"]["seat"]["name"],
        family=family,
        effort=effort,
        mp_config_dir=args.mp_config_dir,
        mp_state_dir=args.mp_state_dir,
    )
    resolved = model_preferences_successor_choice(resolve_args)
    if resolved["seat"] != intent["scope"]["seat"]:
        raise handoff_engine.HandoffError("The registered Model Preferences seat changed; no handoff was frozen.")
    revalidated_scope = {"seat": resolved["seat"], "worktree": worktree}
    revalidated_choice = resolved["choice"] if expected_choice is not None else None
    return handoff_engine.freeze_handoff(
        args.state_dir, args.intent_id, revalidated_scope, revalidated_choice,
    )


def handoff_observe_predecessor(args):
    handoff = handoff_engine.show_handoff(args.state_dir, args.handoff_id)["handoff"]
    native_cwd, caller, identity_check = caller_identity(os.environ)
    if identity_check.status != "ready" or caller is None or native_cwd is None:
        raise native_observer.NativeObservationError(identity_check.source)
    seat = handoff["scope"]["seat"]
    expected_caller = {
        "harness": seat["harness"],
        "session_id": seat["session_id"],
        "panel_id": seat["panel_id"],
        "workspace_id": seat["workspace_id"],
    }
    if caller != expected_caller:
        raise native_observer.NativeObservationError("The live caller does not match this frozen predecessor seat.")
    worktree = canonical_worktree_identity(native_cwd)
    if worktree != handoff["scope"]["worktree"]:
        raise native_observer.NativeObservationError("The live caller worktree does not match this frozen handoff.")
    snapshot = native_observer.observe_codex_predecessor(
        handoff["scope"],
        caller,
        worktree,
        home=Path.home(),
        environment=os.environ,
        runner=run_bounded,
    )
    return handoff_engine.record_predecessor_observation(args.state_dir, args.handoff_id, snapshot)


def handoff_observe_access(args):
    handoff = handoff_engine.show_handoff(args.state_dir, args.handoff_id)["handoff"]
    request = access_observer.normalize_request(
        args.context_file, args.guidance_file, probe_write=args.probe_write, probe_origin=args.probe_origin,
    )
    access_state = handoff["access_observations"]
    pending = None
    if access_state.get("pending") is not None:
        pending = next(item for item in access_state["predecessor"]
                       if item["id"] == access_state["pending"])
        if pending["request_digest"] != access_observer.request_digest(request):
            raise handoff_engine.HandoffError(
                "An access probe has incomplete cleanup; repeat its exact request to reconcile it."
            )
    native_cwd, caller, identity_check = caller_identity(os.environ)
    if identity_check.status != "ready" or caller is None or native_cwd is None:
        raise native_observer.NativeObservationError(identity_check.source)
    seat = handoff["scope"]["seat"]
    expected_caller = {
        "harness": seat["harness"],
        "session_id": seat["session_id"],
        "panel_id": seat["panel_id"],
        "workspace_id": seat["workspace_id"],
    }
    if caller != expected_caller:
        raise native_observer.NativeObservationError("The live caller does not match this frozen predecessor seat.")
    worktree = canonical_worktree_identity(native_cwd)
    if worktree != handoff["scope"]["worktree"]:
        raise native_observer.NativeObservationError("The live caller worktree does not match this frozen handoff.")
    snapshot = native_observer.observe_codex_predecessor(
        handoff["scope"], caller, worktree, home=Path.home(), environment=os.environ, runner=run_bounded,
    )
    if not args.retry and pending is None:
        existing = handoff_engine.existing_access_observation(
            args.state_dir, args.handoff_id, snapshot, request,
        )
        if existing is not None:
            return existing
    if pending is not None:
        inputs = {name: pending["checks"][name] for name in ("context_access", "guidance_access")}
        git_check = pending["checks"]["git_metadata_access"]
        origin_check = pending["checks"]["git_origin_access"]
    else:
        inputs = access_observer.read_inputs(request)
        try:
            git_identity = canonical_worktree_identity(handoff["scope"]["worktree"]["root"])
        except (OSError, ValueError):
            git_identity = None
        git_check = access_observer.git_metadata_check(git_identity, handoff["scope"]["worktree"])
        origin_check = access_observer.git_origin_check(
            request, handoff["scope"]["worktree"]["root"],
            lambda argv, *, closed_stdin=False: run_bounded(argv, cwd=handoff["scope"]["worktree"]["root"],
                                                             closed_stdin=closed_stdin),
        )
    return handoff_engine.record_access_observation(
        args.state_dir, args.handoff_id, snapshot, request, inputs, git_check, origin_check,
        worktree, observed_at=snapshot["observed_at"], identity_resolver=canonical_worktree_identity,
        retry=args.retry,
    )


def _resolve_codex_executable(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise handoff_engine.HandoffError("The native command executable is malformed.")
    candidate = value if Path(value).is_absolute() else shutil.which(value)
    if not candidate:
        raise handoff_engine.HandoffError("The exact Codex executable is unavailable; no opening attempt was recorded.")
    path = Path(os.path.abspath(candidate))
    try:
        path.stat()
    except (OSError, RuntimeError) as exc:
        raise handoff_engine.HandoffError("The exact Codex executable could not be verified; no opening attempt was recorded.") from exc
    if (path.name != "codex" or not path.is_file() or not os.access(path, os.X_OK)):
        raise handoff_engine.HandoffError("Only an executable named codex is supported; no opening attempt was recorded.")
    return str(path)


def _native_command(args, handoff):
    raw = getattr(args, "native_command", None)
    if not isinstance(raw, str) or len(raw.encode("utf-8", "replace")) > 4096:
        raise handoff_engine.HandoffError("Pass the exact supported Codex argv as --native-command JSON; no opening attempt was recorded.")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise handoff_engine.HandoffError("--native-command must be a JSON argv array; no opening attempt was recorded.") from exc
    if (not isinstance(value, list) or len(value) != 5
            or any(not isinstance(token, str) or not token or "\x00" in token for token in value)
            or handoff.get("scope", {}).get("seat", {}).get("harness") != "codex"):
        raise handoff_engine.HandoffError("Only the exact five-token Codex model and effort argv is supported; no opening attempt was recorded.")
    expected_model, expected_effort = handoff_engine._choice_model_effort(handoff)
    if expected_model is None or expected_effort is None:
        raise handoff_engine.HandoffError("Record the predecessor model and effort before opening a successor; no opening attempt was recorded.")
    executable = _resolve_codex_executable(value[0])
    expected = [executable, "-m", expected_model, "-c", f'model_reasoning_effort="{expected_effort}"']
    if value[1:] != expected[1:]:
        raise handoff_engine.HandoffError("--native-command must match the frozen model and effort exactly; no opening attempt was recorded.")
    return {"executable": executable, "argv": expected}


def _read_handoff_packet(path_value):
    if not isinstance(path_value, str) or not path_value:
        raise handoff_engine.HandoffError("Pass a handoff packet file; no opening attempt was recorded.")
    path = os.path.abspath(os.path.expanduser(path_value))
    if len(path) > access_observer.MAX_ACCESS_PATH_LENGTH:
        raise handoff_engine.HandoffError("The handoff packet path exceeds the supported path limit; no opening attempt was recorded.")
    result = access_observer._read_file(path)
    if result.get("status") != "ready" or not isinstance(result.get("bytes_read"), int):
        raise handoff_engine.HandoffError("The handoff packet could not be read as stable regular-file bytes; no opening attempt was recorded.")
    if result["bytes_read"] > MAX_HANDOFF_PACKET_BYTES:
        raise handoff_engine.HandoffError("The handoff packet exceeds 48 KiB; no opening attempt was recorded.")
    return {"path": path, "bytes": result["bytes_read"], "sha256": result["sha256"]}


def _opening_request(args, handoff):
    native_command = _native_command(args, handoff)
    packet = _read_handoff_packet(args.packet_file)
    request_digest = successor_intents.digest({"native_command": native_command, "packet": packet})
    return native_command, packet, request_digest


def _candidate_bootstrap(handoff, packet):
    return (
        f"You are a same-harness successor candidate for handoff {handoff['id']}. "
        f"Read the packet at {packet['path']} only if its SHA-256 is {packet['sha256']}. "
        "Start with read-only preparation and print replicator guide --print. "
        "Do not edit files, dispatch work, contact agents, transfer custody, close the predecessor, "
        "or claim that permissions or trust have been verified."
    )


def _opening_preflight(args, handoff, packet):
    native_cwd, caller, identity_check = caller_identity(os.environ)
    if identity_check.status != "ready" or caller is None or native_cwd is None:
        raise handoff_engine.HandoffError("The exact active predecessor identity is unavailable; no opening attempt was recorded.")
    seat = handoff["scope"]["seat"]
    expected_caller = {key: seat[key] for key in ("harness", "session_id", "panel_id", "workspace_id")}
    if caller != expected_caller:
        raise handoff_engine.HandoffError("The active caller does not match the exact frozen predecessor; no opening attempt was recorded.")
    worktree = canonical_worktree_identity(native_cwd)
    if worktree != handoff["scope"]["worktree"]:
        raise handoff_engine.HandoffError("The active predecessor is outside the frozen canonical worktree; no opening attempt was recorded.")
    snapshot = native_observer.observe_codex_predecessor(
        handoff["scope"], caller, worktree, home=Path.home(), environment=os.environ, runner=run_bounded,
    )
    access = handoff.get("access_observations", {})
    if not isinstance(access, dict) or access.get("pending") is not None:
        raise handoff_engine.HandoffError("Resolve any pending scoped access cleanup before candidate opening.")
    history = access.get("predecessor")
    if not isinstance(history, list) or not history:
        raise handoff_engine.HandoffError("Record scoped context, guidance, and Git access before candidate opening.")
    latest = history[-1]
    request = latest.get("request") if isinstance(latest, dict) else None
    if not isinstance(request, dict):
        raise handoff_engine.HandoffError("The exact scoped access request is unavailable; no opening attempt was recorded.")
    fresh_inputs = access_observer.read_inputs(request)
    git_identity = canonical_worktree_identity(handoff["scope"]["worktree"]["root"])
    packet_check = {"status": "ready" if 0 < packet["bytes"] <= MAX_HANDOFF_PACKET_BYTES else "blocked"}
    assessment = handoff_engine.candidate_opening_checks(
        handoff, snapshot, fresh_inputs, git_identity, packet_check,
    )
    if assessment["status"] != "ready":
        refused = ", ".join(name for name, state in assessment["checks"].items() if state != "ready")
        raise handoff_engine.HandoffError(f"Candidate opening is blocked by current evidence: {refused}.")
    preflight = {
        "status": "ready",
        "checked_at": snapshot["observed_at"],
        "checks": assessment["checks"],
        "predecessor_snapshot_digest": successor_intents.digest(handoff["native_predecessor_snapshot"]),
        "access_observation_id": latest["id"],
        "access_observation_digest": successor_intents.digest(latest),
        "current_snapshot_digest": successor_intents.digest(snapshot),
        "current_native_snapshot": snapshot,
        "current_hook_generation": snapshot["hook_generation"],
        "current_process_identity": snapshot["process_identity"],
        "context_access_digest": successor_intents.digest(latest["checks"]["context_access"]),
        "guidance_access_digest": successor_intents.digest(latest["checks"]["guidance_access"]),
        "git_identity_digest": successor_intents.digest(handoff["scope"]["worktree"]),
        "packet_sha256": packet["sha256"],
    }
    return preflight


def handoff_open(args):
    handoff = handoff_engine.show_handoff(args.state_dir, args.handoff_id)["handoff"]
    native_command, packet, request_digest = _opening_request(args, handoff)
    launch = handoff.get("launch", {})
    if launch.get("status") != "not_started":
        attempt = launch.get("attempt", {})
        if attempt.get("request_digest") != request_digest:
            raise handoff_engine.HandoffError("A different candidate opening was already attempted; inspect it without retrying.")
        return {"handoff": handoff, "reused": True}
    cmux = shutil.which("cmux")
    if not cmux:
        raise handoff_engine.HandoffError("CMUX is unavailable; no opening attempt was recorded.")
    cmux_path = os.path.abspath(cmux)
    worktree = handoff["scope"]["worktree"]["root"]
    workspace_id = handoff["scope"]["seat"]["workspace_id"]
    final_native_argv = [*native_command["argv"], _candidate_bootstrap(handoff, packet)]
    cmux_argv = [cmux_path, "--json", "--id-format", "uuids", "new-surface", "--type", "terminal",
                 "--workspace", workspace_id, "--working-directory", worktree, "--focus", "false",
                 "--command", shlex.join(final_native_argv)]
    preflight = _opening_preflight(args, handoff, packet)
    attempt = {
        "id": uuid.uuid4().hex,
        "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "request_digest": request_digest,
        "native_command": native_command,
        "final_native_argv": final_native_argv,
        "cmux": {"executable": cmux_path, "argv": cmux_argv, "cwd": worktree, "workspace_id": workspace_id},
        "packet": packet,
        "preflight": preflight,
    }
    opening = handoff_engine.begin_candidate_opening(args.state_dir, args.handoff_id, attempt)
    if opening["reused"]:
        return opening
    status, output = run_bounded(cmux_argv, cwd=worktree, closed_stdin=True)
    if status != "ready" or not isinstance(output, str):
        raise handoff_engine.HandoffError(
            f"Opening attempt {attempt['id']} is unconfirmed; inspect the exact CMUX outcome and do not retry."
        )
    try:
        returned = json.loads(output)
        surface_id = str(uuid.UUID(returned.get("surface_id")))
        returned_workspace = str(uuid.UUID(returned.get("workspace_id")))
    except (AttributeError, TypeError, ValueError) as exc:
        raise handoff_engine.HandoffError(
            f"Opening attempt {attempt['id']} is unconfirmed because CMUX returned no exact surface identity; do not retry."
        ) from exc
    if returned_workspace.upper() != workspace_id.upper():
        raise handoff_engine.HandoffError(
            f"Opening attempt {attempt['id']} is unconfirmed because CMUX returned a different workspace; do not retry."
        )
    result = handoff_engine.record_candidate_opened(
        args.state_dir, args.handoff_id, request_digest, surface_id, returned_workspace,
    )
    return result


def handoff_observe_successor(args):
    handoff = handoff_engine.show_handoff(args.state_dir, args.handoff_id)["handoff"]
    launch = handoff.get("launch", {})
    returned = launch.get("returned_surface") if isinstance(launch, dict) else None
    if launch.get("status") not in ("opened_unobserved", "successor_observed") or not isinstance(returned, dict):
        raise handoff_engine.HandoffError("Successor observation requires one recorded CMUX surface return.")
    native_cwd, caller, identity_check = caller_identity(os.environ)
    if identity_check.status != "ready" or caller is None or native_cwd is None:
        raise native_observer.NativeObservationError(identity_check.source)
    if (caller["harness"] != "codex"
            or caller["panel_id"].upper() != returned["surface_id"].upper()
            or caller["workspace_id"].upper() != returned["workspace_id"].upper()
            or caller["session_id"] == handoff["scope"]["seat"]["session_id"]):
        raise native_observer.NativeObservationError("The live caller is not the distinct Codex session on the exact returned surface.")
    worktree = canonical_worktree_identity(native_cwd)
    if worktree != handoff["scope"]["worktree"]:
        raise native_observer.NativeObservationError("The live successor worktree does not match the frozen handoff.")
    successor_scope = {
        "seat": {"name": handoff["scope"]["seat"]["name"], **caller},
        "worktree": worktree,
    }
    snapshot = native_observer.observe_codex_predecessor(
        successor_scope, caller, worktree, home=Path.home(), environment=os.environ, runner=run_bounded,
    )
    return handoff_engine.record_successor_observation(args.state_dir, args.handoff_id, snapshot)


def print_handoff_summary(result):
    handoff = result["handoff"]
    seat = handoff["scope"]["seat"]
    choice = handoff["choice"]
    print(f"Handoff: {handoff['state']}")
    print(f"Handoff ID: {handoff['id']}")
    print(f"Handoff generation: {handoff['handoff_generation']}")
    print(f"Intent ID: {handoff['intent_id']}")
    print(f"Seat: {seat['name']} ({seat['harness']})")
    if choice["kind"] == "preserve_observed_predecessor":
        print("Configured choice: preserve the predecessor's observed model and effort")
    else:
        snapshot = choice["snapshot"]
        print(f"Configured choice: {snapshot['family']} ({snapshot['model']}) at {snapshot['effort']}")
        print(f"Model Preferences profile: {choice['active_profile'] or 'none'}")
        print(f"Active selection digest: {choice['active_selection_digest'] or 'none'}")
        print(f"Candidate digest: {choice['candidate_digest']}")
    print(f"Native hook generation: {handoff['native_generation']['status']}")
    print(f"Observed predecessor model: {handoff['observations']['predecessor_model']['status']}")
    print(f"Observed predecessor effort: {handoff['observations']['predecessor_effort']['status']}")
    predecessor_snapshot = handoff.get("native_predecessor_snapshot")
    if predecessor_snapshot is not None:
        settings = predecessor_snapshot["settings"]
        process = predecessor_snapshot["process_identity"]
        print(f"Native process: PID {process['pid']} started {process['started_at']}")
        print(f"Native turn: {predecessor_snapshot['turn']['id']} at {predecessor_snapshot['turn']['recorded_at']}")
        print(f"Native approval policy snapshot: {settings['approval_policy']}")
        print(f"Native sandbox policy snapshot: {json.dumps(settings['sandbox_policy'], sort_keys=True)}")
    print(f"Observed successor model: {handoff['observations']['successor_model']['status']}")
    print(f"Observed successor effort: {handoff['observations']['successor_effort']['status']}")
    print(f"Effective permissions: {handoff['observations']['effective_permissions']['status']}")
    print(f"Readiness: {handoff['readiness']['status']} ({handoff['readiness']['reason']})")
    print(f"Launch: {handoff['launch']['status']}")
    phases = handoff.get("phase_readiness", handoff_engine.phase_readiness(handoff))
    print(f"Candidate opening readiness: {phases['candidate_opening']['status']}")
    print(f"Launch readiness: {phases['launch']['status']}")
    print(f"Transfer readiness: {phases['transfer']['status']}")
    returned = handoff.get("launch", {}).get("returned_surface")
    if returned:
        print(f"Returned CMUX surface: {returned['surface_id']} in {returned['workspace_id']}")
        print("A returned CMUX surface does not confirm that Codex started or received the packet.")
    elif handoff.get("launch", {}).get("status") == "opening_unconfirmed":
        print("The opening attempt is unconfirmed. Inspect the exact CMUX outcome before any new action.")
    else:
        print("No CMUX candidate opening was attempted.")
    print("No custody transfer or predecessor close was performed.")


def print_access_summary(result):
    observation = result["observation"]
    checks = observation["checks"]
    print(f"Access observation: {observation['state']}")
    print(f"Observation ID: {observation['id']}")
    for name in ("owner_binding", "native_settings_match", "context_access", "guidance_access",
                 "git_metadata_access", "write_access", "git_origin_access", "native_trust",
                 "effective_permissions"):
        print(f"{name.replace('_', ' ').capitalize()}: {checks[name]['status']}")
    print(f"Probe cleanup: {observation['cleanup']['status']}")
    phases = handoff_engine.phase_readiness(result["handoff"])
    print(f"Launch readiness: {phases['launch']['status']}")
    print(f"Transfer readiness: {phases['transfer']['status']}")
    print("No successor launch, custody transfer, or predecessor close was performed.")


def handoff_show(args):
    return handoff_engine.show_handoff(args.state_dir, args.handoff_id)


def handoff_reconcile(args):
    return handoff_engine.reconcile_handoff(args.state_dir, args.handoff_id)


def print_handoff_reconciliation(result):
    print_handoff_summary({"handoff": result["handoff"]})
    reconciliation = result["reconciliation"]
    print(f"Reconciliation: {reconciliation['status']}")
    print(f"Native queries performed: {str(reconciliation['native_queries_performed']).lower()}")
    print(f"Writes performed: {str(reconciliation['writes_performed']).lower()}")
    for blocker in reconciliation["blockers"]:
        print(f"Blocked: {blocker}")


def print_successor_intent(report):
    intent = report.get("intent")
    if intent is None:
        print("No successor intent is configured for this seat and worktree.")
        return
    scope = intent["scope"]
    seat = scope["seat"]
    worktree = scope["worktree"]
    choice = intent["choice"]
    print(f"Successor intent: {intent['state']} ({intent['policy']})")
    print(f"Intent ID: {intent['id']}")
    print(f"Seat: {seat['name']} ({seat['harness']})")
    print(f"Configured session identity: {seat['session_id']}")
    print(f"Worktree: {worktree['root']}")
    if choice["kind"] == "preserve_observed_predecessor":
        print("Configured choice: preserve the predecessor's observed model and effort")
    else:
        snapshot = choice["snapshot"]
        provenance = choice["provenance"]
        print(f"Configured choice: {snapshot['family']} ({snapshot['model']}) at {snapshot['effort']}")
        print("Model Preferences provenance:")
        print(f"  Active profile: {choice['active_profile'] or 'none'}")
        print(f"  Active selection digest: {choice['active_selection_digest'] or 'none'}")
        print(f"  Candidate digest: {choice['candidate_digest']}")
        matches = choice["candidate_matches_active_orchestrator"]
        print(f"  Matches active orchestrator: {matches if matches is not None else 'not compared'}")
        print(f"  Configured family source: {provenance.get('family_source') or 'unavailable'}")
    print("Native observations:")
    print(f"  Native generation: {intent['native_generation']['status']}")
    print(f"  Predecessor model: {intent['observations']['predecessor_model']['status']}")
    print(f"  Predecessor effort: {intent['observations']['predecessor_effort']['status']}")
    print(f"  Effective permissions: {intent['observations']['effective_permissions']['status']}")
    print(f"  Readiness: {intent['readiness']['status']}")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="replicator",
        description="Configure one-shot successor intent, print the handoff guide, or inspect readiness.",
    )
    default_state_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "starfleet-ai/replicator"
    parser.add_argument("--state-dir", default=str(default_state_dir), help="private Replicator state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    guide = commands.add_parser("guide", help="print the maintained handoff guide")
    guide.add_argument("--print", dest="print_guide", action="store_true", required=True)
    successor = commands.add_parser("successor", help="configure and inspect one-shot successor intent")
    successor_commands = successor.add_subparsers(dest="successor_command", required=True)
    configure = successor_commands.add_parser("configure", help="configure intent without launching a successor")
    configure.add_argument("--seat", required=True)
    configure.add_argument("--worktree", required=True)
    configure.add_argument("--family", help="existing Model Preferences model family for an explicit one-shot choice")
    configure.add_argument("--effort", help="supported effort for --family")
    configure.add_argument("--mp-config-dir")
    configure.add_argument("--mp-state-dir")
    show = successor_commands.add_parser("show", help="inspect intent for the exact seat and worktree")
    show.add_argument("--seat", required=True)
    show.add_argument("--worktree", required=True)
    show.add_argument("--mp-config-dir")
    show.add_argument("--mp-state-dir")
    show.add_argument("--json", action="store_true", help="print the intent as JSON")
    cancel = successor_commands.add_parser("cancel", help="cancel a configured intent by its exact ID")
    cancel.add_argument("intent_id")
    handoff = commands.add_parser("handoff", help="freeze and inspect versioned handoffs without native actions")
    handoff_commands = handoff.add_subparsers(dest="handoff_command", required=True)
    freeze = handoff_commands.add_parser("freeze", help="freeze one configured intent without launching a successor")
    freeze.add_argument("intent_id")
    freeze.add_argument("--worktree", required=True, help="exact canonical Git worktree for this intent")
    freeze.add_argument("--mp-config-dir")
    freeze.add_argument("--mp-state-dir")
    freeze.add_argument("--json", action="store_true", help="print the frozen handoff as JSON")
    open_candidate = handoff_commands.add_parser(
        "open", help="open one same-harness Codex candidate after exact predecessor preflight",
    )
    open_candidate.add_argument("handoff_id")
    open_candidate.add_argument("--native-command", required=True,
                                help="JSON argv with executable, -m frozen-model, and -c model_reasoning_effort selection")
    open_candidate.add_argument("--packet-file", required=True, help="bounded handoff packet whose exact bytes are hashed")
    open_candidate.add_argument("--json", action="store_true", help="print the recorded opening outcome as JSON")
    show_handoff_parser = handoff_commands.add_parser("show", help="inspect a frozen handoff without native queries")
    show_handoff_parser.add_argument("handoff_id")
    show_handoff_parser.add_argument("--json", action="store_true", help="print the handoff as JSON")
    reconcile = handoff_commands.add_parser("reconcile", help="read local handoff state without native queries or writes")
    reconcile.add_argument("handoff_id")
    reconcile.add_argument("--json", action="store_true", help="print the reconciliation as JSON")
    observe = handoff_commands.add_parser(
        "observe-predecessor", help="record a scoped Codex settings snapshot from the exact predecessor",
    )
    observe.add_argument("handoff_id")
    observe.add_argument("--json", action="store_true", help="print the persisted observation as JSON")
    observe_successor = handoff_commands.add_parser(
        "observe-successor", help="record the exact Codex successor's own native settings snapshot",
    )
    observe_successor.add_argument("handoff_id")
    observe_successor.add_argument("--json", action="store_true", help="print the persisted observation as JSON")
    access = handoff_commands.add_parser(
        "observe-access", help="record bounded access evidence from the exact Codex predecessor",
    )
    access.add_argument("handoff_id")
    access.add_argument("--context-file", action="append", default=[], metavar="PATH")
    access.add_argument("--guidance-file", action="append", default=[], metavar="PATH")
    access.add_argument("--probe-write", action="store_true",
                        help="write, read, and remove unique probes in the frozen worktree and Git metadata dirs")
    access.add_argument("--probe-origin", action="store_true",
                        help="run bounded git ls-remote --refs origin with stdin closed")
    access.add_argument("--retry", action="store_true", help="record a fresh observation after a completed attempt")
    access.add_argument("--json", action="store_true", help="print the persisted access observation as JSON")
    doctor = commands.add_parser("doctor", help="inspect Replicator configuration without writes or native queries")
    doctor.add_argument("--starfleet-json", action="store_true", help="print the agreed structured diagnostic protocol")
    preflight = commands.add_parser("preflight", help="inspect the current caller and worktree without writes")
    preflight.add_argument("--context-file", action="append", default=[], metavar="PATH")
    preflight.add_argument("--guidance-file", action="append", default=[], metavar="PATH")
    preflight.add_argument("--skip-tool", action="append", default=[], metavar="NAME")
    preflight.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def print_human_report(report):
    print(f"Operational readiness: {report['operational_readiness']}")
    print(f"Custody acceptance: {report['custody_acceptance'].replace('_', ' ')}")
    print(f"Predecessor cleanup: {report['predecessor_cleanup'].replace('_', ' ')}")
    print(f"Native delivery: {report['native_delivery'].replace('_', ' ')}")
    print(f"Pilot: {report['pilot'].replace('_', ' ')}")
    for check in report["checks"]:
        print()
        print(f"{check['name']}: {check['status']}")
        print(f"  Source: {check['source']}")
        print(f"  Next step: {check['next_step']}")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "guide" and args.print_guide:
        sys.stdout.buffer.write(Path(__file__).with_name("guide.md").read_bytes())
        return 0
    if args.command == "doctor":
        report = successor_intents.doctor_report(args.state_dir)
        if args.starfleet_json:
            print(json.dumps(report, indent=2))
        else:
            for name, state in report["checks"].items():
                print(f"{name}: {state}")
        return 0
    if args.command == "successor":
        try:
            if args.successor_command == "configure":
                result = successor_configure(args)
            elif args.successor_command == "show":
                result = successor_show(args)
                if not args.json:
                    print_successor_intent(result)
                    return 0
            else:
                result = successor_intents.cancel_intent(args.state_dir, args.intent_id)
            print(json.dumps(result, indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print(f"replicator: {exc}", file=sys.stderr)
            return 1
    if args.command == "handoff":
        try:
            if args.handoff_command == "freeze":
                result = handoff_freeze(args)
                if not args.json:
                    print_handoff_summary(result)
                    print(f"Existing handoff reused: {str(result['reused']).lower()}")
                    return 0
            elif args.handoff_command == "show":
                result = handoff_show(args)
                if not args.json:
                    print_handoff_summary({"handoff": result["handoff"]})
                    return 0
            elif args.handoff_command == "open":
                result = handoff_open(args)
                if not args.json:
                    print_handoff_summary(result)
                    print(f"Opening request reused: {str(result['reused']).lower()}")
                    return 0
            elif args.handoff_command == "observe-predecessor":
                result = handoff_observe_predecessor(args)
                if not args.json:
                    print_handoff_summary(result)
                    print(f"Observation reused: {str(result['reused']).lower()}")
                    return 0
            elif args.handoff_command == "observe-successor":
                result = handoff_observe_successor(args)
                if not args.json:
                    print_handoff_summary(result)
                    print(f"Successor observation reused: {str(result['reused']).lower()}")
                    return 0
            elif args.handoff_command == "observe-access":
                result = handoff_observe_access(args)
                if not args.json:
                    print_access_summary(result)
                    print(f"Observation reused or reconciled: {str(result['reused']).lower()}")
                    return 0
            else:
                result = handoff_reconcile(args)
                if not args.json:
                    print_handoff_reconciliation(result)
                    return 0
            print(json.dumps(result, indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print(f"replicator: {exc}", file=sys.stderr)
            return 1
    if args.command == "preflight":
        try:
            report = preflight_report(args.context_file, args.guidance_file, args.skip_tool)
        except ValueError as exc:
            parser.error(str(exc))
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_human_report(report)
        return 0 if report["operational_readiness"] == "ready" else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
