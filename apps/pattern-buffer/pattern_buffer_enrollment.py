"""Self-service checkpoint enrollment and conservative native restart preparation."""
import argparse
import contextlib
import ctypes
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import struct
import subprocess
import sys
import time


BRIEFING = """Pause and take stock before writing your note to your future self.
Re-read your mission, assigned roadmap, latest user direction and relevant guidance.
Reconcile current implementation state, worker results, dependencies, evidence and human holds.
Ask whether you have the full context and appropriate resources, including useful parallel work in temporary worktrees while respecting dependencies, ownership and shared limits.
Resolve gaps within existing authority where possible; record remaining uncertainty explicitly.
Then write a concise, accurate checkpoint focused on what your future self needs to resume safely.
Maintain a current snapshot, not a running history: replace outdated state and prune completed or superseded details, preserving facts still needed for safe recovery.
Aim for 400-700 words and at most {note_budget_bytes} UTF-8 bytes, linking detailed evidence rather than copying it.
Leave room for later updates; be brief without sacrificing accurate next actions, decisions, uncertainties, evidence or human holds.
Use the six required sections; only the exact optional # Working checkpoint title may precede ## Current state, so put dates and disclaimers inside a section.
This is a judgment step, not a requirement to launch agents or invent extra work.
Enrollment needs no separate human approval and grants no product, mission, merge, deployment or job-control authority.
Maintain the designated draft after meaningful state changes, batching related updates.
Reconcile actual jobs and evidence before acting on a recovered note; timestamps and counters do not prove factual freshness."""


def _argv(pid):
    """Read only argc/argv, never the adjacent process environment."""
    if sys.platform == "darwin":
        lib = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)
        size = ctypes.c_size_t()
        if lib.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or size.value > 1024 * 1024:
            raise ValueError("native launch arguments unavailable")
        buf = ctypes.create_string_buffer(size.value)
        if lib.sysctl(mib, 3, buf, ctypes.byref(size), None, 0):
            raise ValueError("native launch arguments unavailable")
        raw = buf.raw[:size.value]
        argc = struct.unpack_from("i", raw)[0]
        pos = raw.index(b"\0", 4) + 1
        while pos < len(raw) and raw[pos] == 0:
            pos += 1
        result = []
        for _ in range(argc):
            end = raw.index(b"\0", pos)
            result.append(raw[pos:end].decode())
            pos = end + 1
        return result
    path = Path(f"/proc/{pid}/cmdline")
    with path.open("rb") as handle:
        data = handle.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("native launch arguments exceed the inspection limit")
    return [part.decode() for part in data.rstrip(b"\0").split(b"\0")]


def _native(harness):
    pid = os.getppid()
    for _ in range(16):
        try:
            result = subprocess.run(["ps", "-p", str(pid), "-o", "ppid=,comm="],
                                    text=True, capture_output=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return None, None
        parts = result.stdout.strip().split(None, 1)
        if len(parts) != 2:
            break
        if Path(parts[1]).name == harness:
            try:
                return pid, _argv(pid)
            except (OSError, ValueError, struct.error):
                return pid, None
        pid = int(parts[0])
        if pid <= 1:
            break
    return None, None


def _started_at(pid):
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], text=True,
                                capture_output=True, timeout=3, env={**os.environ, "LC_ALL": "C"})
        stamp = datetime.datetime.strptime(result.stdout.strip(), "%a %b %d %H:%M:%S %Y")
        return int(time.mktime(stamp.timetuple()))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _codex_runtime(session, pid):
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "logs_2.sqlite"
    if pid is None or not path.is_file():
        return {}
    started = _started_at(pid)
    if started is None:
        return {}
    try:
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            row = db.execute("SELECT feedback_log_body FROM logs WHERE thread_id=? AND ts>=? "
                             "AND process_uuid LIKE ? AND feedback_log_body LIKE "
                             "'%auto_compact_scope_limit=%' ORDER BY id DESC LIMIT 1",
                             (session, started, f"pid:{pid}:%")).fetchone()
        text = row[0] if row else ""
        limit = re.search(r"auto_compact_scope_limit=Some\((\d+)\)", text)
        window = re.search(r"full_context_window_limit=Some\((\d+)\)", text)
        model = re.search(r"\bmodel=([\w.-]+)", text)
        effort = re.search(r"codex.turn.reasoning_effort=([\w]+)", text)
        result = {}
        if limit and window and "auto_compact_limit_scope=Total" in text and int(window[1]) > 0:
            result["compact_at"] = 100 * int(limit[1]) / int(window[1])
        if model and effort:
            result.update(model=model[1], effort=effort[1])
        return result
    except (OSError, sqlite3.Error):
        return {}


def _native_cwd(pid):
    try:
        if sys.platform == "darwin":
            result = subprocess.run(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                    capture_output=True, text=True, timeout=3)
            paths = [line[1:] for line in result.stdout.splitlines() if line.startswith("n/")]
            return Path(paths[0]).resolve() if len(paths) == 1 else None
        return Path(f"/proc/{pid}/cwd").resolve(strict=True)
    except (OSError, subprocess.SubprocessError):
        return None


def _restart_plan(ci, harness, session, argv, settings, runtime, observed, root=None, native_cwd=None,
                  native_pressure=True):
    if not argv:
        return None, "Native launch arguments unavailable; resume with your existing launcher and staged hooks."
    if harness != "codex":
        return None, "Restart/resume Claude through its existing launcher after reviewing its hook controls."
    if settings != ci.settings_path(harness, None):
        return None, "Custom settings were prepared; integrate them through the launcher that actually loads that settings file."
    if root is not None and native_cwd != root:
        return None, "Native process worktree differs or is unknown; use the original launcher for the intended worktree."
    if root is not None:
        for i, arg in enumerate(argv):
            target = (argv[i + 1] if arg in ("--cd", "-C") and i + 1 < len(argv)
                      else arg.split("=", 1)[1] if arg.startswith("--cd=") else None)
            if target and ci.checkout(Path(target) if Path(target).is_absolute() else root / target)[0] != root:
                return None, "Native --cd override targets a different worktree; reconcile it manually."
    # Only reproduce a known same-session resume, never reinterpret a fresh CLI prompt.
    if "resume" not in argv or argv.index("resume") + 1 >= len(argv) or argv[argv.index("resume") + 1] != session:
        return None, "Native argv does not identify this exact resumed session; use your original launcher."
    if (any(argv[i] == "--disable" and "hooks" in argv[i + 1].split(",") for i in range(len(argv) - 1))
            or any(a.startswith("--disable=") and "hooks" in a.split("=", 1)[1].split(",") for a in argv)):
        return None, "Native hooks are explicitly disabled; preserve that setting and reconcile it before activation."
    try:
        import tomllib
    except ImportError:
        return None, "Preparing native Codex hook overrides requires Python 3.11 or newer."
    result = list(argv)
    for event in ("SessionStart", "PreCompact", "PostToolUse", "UserPromptSubmit"):
        prefix = "hooks." + event + "="
        found = [i for i in range(len(result) - 1) if result[i] in ("-c", "--config")
                 and result[i + 1].startswith(prefix)]
        if len(found) > 1:
            return None, "Repeated native hook overrides need manual reconciliation; no launcher was prepared."
        add_native = native_pressure and event != "SessionStart"
        if not found and not add_native:
            continue
        groups = tomllib.loads(result[found[0] + 1])["hooks"][event] if found else []
        # Use the normal installer validation before manipulating native groups.
        if not isinstance(groups, list) or any(not isinstance(g, dict) or
                not isinstance(g.get("hooks"), list) or
                any(not isinstance(h, dict) for h in g["hooks"]) for g in groups):
            raise ValueError("native hook override is malformed")
        if event == "SessionStart" and not any(ci.owned(h, harness) for g in groups for h in g["hooks"]):
            continue
        preserved = []
        for group in groups:
            handlers = [h for h in group["hooks"] if not ci.owned(h, harness)]
            if handlers:
                preserved.append({**group, "hooks": handlers})
        additions = ci.hook_config(harness, runtime, True)["hooks"][event] if add_native else []
        value = prefix + json.dumps(preserved + additions, separators=(",", "="))
        tomllib.loads(value)
        if found:
            result[found[0] + 1] = value
        else:
            result[result.index("resume"):result.index("resume")] = ["-c", value]
    result = [a for a in result if a != "--dangerously-bypass-hook-trust"]
    if not any(result[i] == "--enable" and "hooks" in result[i + 1].split(",") for i in range(len(result) - 1)):
        index = result.index("resume")
        result[index:index] = ["--enable", "hooks"]
    if observed.get("model"):
        has_model = False
        for i, argument in enumerate(result[:-1]):
            if argument in ("--model", "-m"):
                result[i + 1] = observed["model"]
                has_model = True
        index = result.index("resume")
        result[index:index] = ([] if has_model else ["--model", observed["model"]]) + [
            "-c", "model_reasoning_effort=" + json.dumps(observed["effort"])]
    else:
        return None, "Effective model is unknown; use the original launcher without changing models."
    return result, "Prepared only. At a safe boundary, exit the agent, run the launcher, then review /hooks trust."


def _activation(ci, checkpoints, root, gitdir, args, session, pid, argv, observed, prepare=False):
    settings = ci.settings_path(args.harness, args.settings)
    try:
        ci.check_dispatch_hook(args.harness, args.settings)
    except ValueError as error:
        raise ValueError("Install or upgrade the standard hooks with install --harness " + args.harness + ": " + str(error)) from error
    data = ci.read_settings(settings)
    events = ("SessionStart", "PreCompact", "PostToolUse", "UserPromptSubmit")
    owned = {event: [h for group in data.get("hooks", {}).get(event, [])
                     for h in group["hooks"] if ci.owned(h, args.harness)] for event in events}
    if any(len(owned[event]) != 1 for event in events):
        raise ValueError("standard checkpoint hooks are missing or duplicated; run install --harness " + args.harness)
    command = owned["SessionStart"][0]["command"]
    if any(owned[event][0]["command"] != command for event in events):
        raise ValueError("standard checkpoint hooks use mixed runtimes; reinstall the complete package")
    status_line = data.get("statusLine")
    if args.harness == "claude" and (not isinstance(status_line, dict) or
            not isinstance(status_line.get("command"), str) or
            not status_line["command"].endswith(" # starfleet-context-statusline:v1")):
        raise ValueError("Claude checkpoint telemetry is missing; run install --harness claude")
    runtime = Path(shlex.split(command, comments=True)[1])
    if args.runtime_dir and runtime.parent != Path(args.runtime_dir).expanduser().absolute():
        raise ValueError("selected runtime differs; choose --runtime-dir during install first")
    native_cwd = _native_cwd(pid) if pid and argv else None
    inspection_available = bool(argv and native_cwd)
    legacy = bool(args.harness == "codex" and argv and native_cwd == root and any(
        argv[i] in ("-c", "--config") and argv[i + 1].startswith("hooks.")
        and ci.marker("codex").strip() in argv[i + 1] for i in range(len(argv) - 1)))
    result = {"standard_hooks_ready": True, "hooks_loaded": "unverified", "recovery_observed": False,
              "restart_required": legacy if args.harness != "codex" or inspection_available else None,
              "legacy_native_hooks": legacy if args.harness != "codex" or inspection_available else None,
              "pressure_source": "selected settings file", "hook_config": {"hooks": {}},
              "launcher": None, "trust": "Review the stable definitions once in native hook controls; unchanged definitions can be reused by future sessions.",
              "restart_instructions": "Enrollment adds no hook definitions. If the standard hooks are already loaded and trusted, no enrollment-specific restart is needed. Otherwise review native hook controls and resume through the existing launcher if required."}
    if not legacy:
        if args.harness == "codex" and not inspection_available:
            result["restart_instructions"] = "Native launch inspection is unavailable; legacy copies are unknown. Standard hooks are installed, but inspect their actual loading/trust through the original launcher before claiming activation."
        return result
    result["restart_instructions"] = "Legacy native copies would duplicate the standard source. Repeat with --prepare-restart at a safe boundary, then use the prepared launcher; no checkpoint note is changed."
    if not prepare:
        return result
    restart, detail = _restart_plan(ci, args.harness, session, argv, settings, runtime, observed,
                                    root, native_cwd, native_pressure=False)
    result["restart_instructions"] = detail
    if restart:
        folder = checkpoints._dir(gitdir, args.harness, session)
        checkpoints._ensure_directory_chain(folder)
        with ci.locked(folder / "enrollment.lock"):
            ci.atomic(folder / "resume.json", json.dumps(
                {"cwd": str(root), "session": session, "source_pid": pid, "argv": restart}, indent=2) + "\n")
            ci.atomic(folder / "resume.py", 'import json, os\nfrom pathlib import Path\n'
                      'd = json.loads(Path(__file__).with_name("resume.json").read_text())\n'
                      'try:\n    os.kill(d["source_pid"], 0)\n'
                      'except ProcessLookupError:\n    pass\n'
                      'else:\n    raise SystemExit("Exit the original agent at a safe boundary before resuming.")\n'
                      'os.chdir(d["cwd"])\nos.execv(d["argv"][0], d["argv"])\n')
        result["launcher"] = shlex.join([sys.executable, str(folder / "resume.py")])
    return result


def enroll(ci, checkpoints, root, gitdir, args):
    harness = args.harness
    env_name = ("CODEX_THREAD_ID" if harness == "codex" else
                "OPENCODE_SESSION_ID" if harness == "opencode" else "CLAUDE_SESSION_ID")
    inherited = os.environ.get(env_name)
    session = args.session or inherited
    if not session:
        raise ValueError(f"exact session unknown; provide --session (or {env_name} when supplied by the harness)")
    if inherited and args.session and inherited != args.session:
        raise ValueError("explicit session differs from this agent's inherited native session")
    # Existing standing enrollment is a prerequisite, never invented by this workflow.
    checkpoints._primary(ci, root, gitdir, harness, session)
    config, _ = ci.load(root, gitdir)
    if not ci.enabled(config):
        raise ValueError("Pattern Buffer is disabled; self-enrollment does not override that hold")
    if harness == "opencode" and args.prepare_restart:
        raise ValueError("OpenCode context plugins do not use native hook restart preparation")
    if harness == "opencode" and (args.compact_at is not None or args.lead_points is not None):
        raise ValueError("OpenCode pressure is unqualified; do not supply compact-at or lead values")
    pid, argv = (None, None) if harness == "opencode" else _native(harness)
    observed = _codex_runtime(session, pid) if harness == "codex" else {}
    compact = args.compact_at if args.compact_at is not None else observed.get("compact_at")
    lead_points = args.lead_points if args.lead_points is not None else 5.0
    if harness == "claude" and compact is None:
        override = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
        compact = float(override) if override else 60.0
    result = {"worktree": str(root), "harness": harness, "session": session,
              "approval_required": False, "briefing": BRIEFING.format(note_budget_bytes=checkpoints.NOTE_LIMIT),
              "note_budget_bytes": checkpoints.NOTE_LIMIT,
              "template": checkpoints._template(), "compact_at": compact,
              "threshold_source": ("unqualified; live OpenCode pressure warnings are disabled"
                  if harness == "opencode" else "explicit confirmation" if args.compact_at is not None else
                  "same-session native runtime" if observed.get("compact_at") is not None else
                  "Claude environment or fallback" if harness == "claude" else "unknown"),
              "native_pid": pid, "state": "briefing", "recovery_observed": False,
              "guide": "apps/pattern-buffer/docs/checkpoints.md"}
    state_path = checkpoints._state_path(gitdir, harness, session)
    if state_path.exists():
        report = checkpoints.handle_cli(ci, root, gitdir, argparse.Namespace(
            command="checkpoint-status", harness=harness, session=session))
        result.update(state="already_enrolled", checkpoint=report,
                      next_action="Keep the existing note and settings; use checkpoint-save/config for deliberate updates.")
        if args.prepare_restart:
            result.update(_activation(ci, checkpoints, root, gitdir, args, session, pid, argv, observed, prepare=True))
        return _authoring_result(result, report)
    if not args.file:
        result["next_action"] = "Take stock, write or review a private note, then repeat with --file PATH. No files or hooks changed."
        return result
    if compact is None and harness != "opencode":
        raise ValueError("Codex threshold is unknown; provide confirmed --compact-at on the context-usage scale")
    if harness != "opencode":
        checkpoints._validate_settings(compact, lead_points)
    note_path = Path(args.file).expanduser().absolute()
    note = ci.bounded(note_path, checkpoints.NOTE_LIMIT)
    checkpoints._validate_note(note)
    checkpoints._published_note(note, {"harness": harness, "session": session}, checkpoints._now())
    # Validate the reusable path before installing hooks or creating a draft.
    checkpoints._safe_checkpoint_path(str(note_path), root, ci,
        [Path(str(note_path) + ".previous.md"), Path(str(note_path) + ".owner.json")])
    owner = Path(str(note_path) + ".owner.json")
    if owner.exists() and checkpoints._json(owner, ci) != checkpoints._identity(root, gitdir, harness, session):
        raise ValueError("external checkpoint path is claimed by another session")
    if harness == "opencode":
        activation = {"plugin_setup": "requires the existing opt-in OpenCode context plugin",
                      "hooks_loaded": "unverified", "recovery_observed": False,
                      "pressure_qualified": False,
                      "pressure_status": "unqualified; live warnings disabled"}
    else:
        activation = _activation(ci, checkpoints, root, gitdir, args, session, pid, argv, observed)
    folder = checkpoints._dir(gitdir, harness, session)
    checkpoints._ensure_directory_chain(folder)
    with ci.locked(folder / "enrollment.lock"):
        if state_path.exists():
            raise ValueError("session was enrolled concurrently; rerun to inspect its existing registration")
        checkpoints.handle_cli(ci, root, gitdir, argparse.Namespace(
            command="checkpoint-init", harness=harness, session=session,
            compact_at=compact, lead_points=lead_points if harness != "opencode" else None,
            path=str(note_path)))
        report = checkpoints.handle_cli(ci, root, gitdir, argparse.Namespace(
            command="checkpoint-save", harness=harness, session=session, file=str(note_path)))
    result.update(activation, state="prepared", checkpoint=report)
    if harness == "opencode":
        result["next_action"] = ("Use the exact OpenCode context plugin in the same session. "
                                 "Pressure warnings remain disabled until native usage and runtime settings are reviewed.")
    else:
        result["hook_installation"] = ["Reusing stable installed hooks; no definitions or trust records changed."]
        result["next_action"] = ("Read the activation instructions, preserve native trust and existing jobs, and "
                                 "confirm real hook reception during ordinary work.")
    return _authoring_result(result, report)


def _authoring_result(result, report):
    # Put the actual authoring target before the long take-stock briefing.
    return {"edit_this_file": report["draft_path"],
            "authoring_guidance": "Edit this ignored draft as a pruned current snapshot; the hook publishes it. Do not maintain the published copy or its generated header by hand.",
            "published_copy": report["checkpoint_path"],
            "draft_body_limit_bytes": report["draft_body_limit_bytes"], **result}
