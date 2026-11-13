#!/usr/bin/env python3
"""Print the Replicator handoff guide or a read-only readiness report."""

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


STATUSES = ("ready", "blocked", "unknown", "skipped")
ACTIVE_CMUX_STATES = ("working", "idle")
MAX_COMMAND_OUTPUT = 2 * 1024 * 1024
MAX_INPUT_BYTES = 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 5
TOOL_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}\Z")


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


def run_bounded(argv, cwd=None):
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
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


def build_parser():
    parser = argparse.ArgumentParser(
        prog="replicator",
        description="Print the handoff guide or a read-only operational readiness report.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    guide = commands.add_parser("guide", help="print the maintained handoff guide")
    guide.add_argument("--print", dest="print_guide", action="store_true", required=True)
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
