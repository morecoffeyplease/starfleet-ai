"""Checkpoint support for Pattern Buffer. Standard library only."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import selectors
import re
import shlex
import subprocess
import tempfile
import time
import uuid

NOTE_LIMIT = 8192
TRACE_LIMIT = 256 * 1024
REMINDER_REARM_HYSTERESIS = 5.0
ATTENTION_INPUT_LIMIT = 4096
ATTENTION_OUTPUT_LIMIT = 8192
ATTENTION_TIMEOUT_SECONDS = 3
ATTENTION_DETAIL_LIMIT = 512
SECTIONS = ("Current state", "Next action", "Decisions and uncertainty",
            "Active work", "Evidence", "Holds and guidance")
SNAPSHOT_PRUNING = (
    "Refresh this as a current snapshot. Replace outdated state rather than append. "
    "Remove completed work, superseded decisions and obsolete commands. Keep current decisions and relevant "
    "rejected approaches, exact commands still needed for the next action or recovery, active job IDs, evidence "
    "pointers, uncertainties and human holds. "
    "Link useful history instead of repeating it."
)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _record(ci, root, state, event, result=None, correlation_id=None, details=None):
    if not hasattr(ci, "record_event"):
        return
    identity = state.get("identity") or {}
    try:
        binding = ci.session_binding(root, Path(identity["gitdir"]), identity["harness"], identity["session"])
        ci.record_event(root, event, identity["harness"], identity["session"],
                        binding.enrollment_id if binding else None, result, correlation_id, details)
    except Exception as error:
        print("pattern-buffer checkpoint telemetry unavailable: " + type(error).__name__, file=os.sys.stderr)


def _identity(root, gitdir, harness, session):
    return {"root": str(root), "gitdir": str(gitdir), "harness": harness, "session": session}


def _dir(gitdir, harness, session):
    key = _digest(json.dumps([harness, session], ensure_ascii=False,
                             separators=(",", ":")).encode())
    return gitdir / "context-injector" / "checkpoints" / key


def _state_path(gitdir, harness, session):
    return _dir(gitdir, harness, session) / "state.json"


def _json(path, ci):
    value = json.loads(ci.bounded(path, 64 * 1024))
    if not isinstance(value, dict):
        raise ValueError(f"invalid checkpoint metadata: {path}")
    return value


def _write_json(path, data, ci):
    ci.atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _attention_detail(value, fallback):
    if not isinstance(value, str):
        value = fallback
    value = value.replace("\x00", "")
    return value.encode("utf-8")[:ATTENTION_DETAIL_LIMIT].decode("utf-8", "ignore")


def _attention_record(event_key, outcome, attempted, detail):
    return {"event_key": event_key, "outcome": outcome, "attempted": attempted,
            "detail": _attention_detail(detail, "No delivery detail was returned.")}


def _attention_unavailable(event_key, detail):
    return _attention_record(event_key, "unavailable", False, detail)


def _bounded_attention_process(command, message_bytes, output_limit=ATTENTION_OUTPUT_LIMIT):
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    output = bytearray()
    oversized = False
    timed_out = False
    deadline = time.monotonic() + ATTENTION_TIMEOUT_SECONDS
    try:
        try:
            process.stdin.write(message_bytes)
            process.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if selector.get_map():
                events = selector.select(remaining)
                if not events:
                    timed_out = True
                    break
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(),
                                    min(4096, output_limit + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > output_limit:
                        oversized = True
                        break
                if oversized:
                    break
            else:
                try:
                    process.wait(timeout=remaining)
                    break
                except subprocess.TimeoutExpired:
                    timed_out = True
                    break
        if timed_out or oversized:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait()
        return process.returncode, bytes(output), timed_out, oversized
    finally:
        selector.close()
        try:
            process.stdout.close()
        except OSError:
            pass
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()
        if process.stdin and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass


def _attention_once(harness, session, event_key, message):
    if harness not in ("claude", "codex"):
        return _attention_unavailable(event_key, "Caller harness is not verified.")
    session_name = "CLAUDE_CODE_SESSION_ID" if harness == "claude" else "CODEX_THREAD_ID"
    try:
        normalized_session = str(uuid.UUID(session)).upper()
        surface = str(uuid.UUID(os.environ.get("CMUX_SURFACE_ID", ""))).upper()
    except (ValueError, TypeError, AttributeError):
        return _attention_unavailable(event_key, "Hook session or CMUX surface UUID is unavailable.")
    launch_kind = os.environ.get("CMUX_AGENT_LAUNCH_KIND", "").strip().lower()
    caller_session = os.environ.get(session_name, "").strip()
    accepted_launch_kinds = {harness}
    if harness == "claude":
        accepted_launch_kinds.add("claudeteams")
    if launch_kind and launch_kind not in accepted_launch_kinds:
        return _attention_unavailable(event_key, "Caller launch identity does not match this hook session.")
    if caller_session:
        try:
            caller_session = str(uuid.UUID(caller_session)).upper()
        except (ValueError, TypeError, AttributeError):
            return _attention_unavailable(event_key, "Caller session identity is not a valid UUID.")
        if caller_session != normalized_session:
            return _attention_unavailable(event_key, "Caller session identity does not match this hook session.")
    launcher = Path.home() / ".local" / "bin" / "hailing-frequencies"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        return _attention_unavailable(event_key, "The stable Hailing Frequencies launcher is unavailable.")
    message_bytes = message.encode("utf-8")
    if len(message_bytes) > ATTENTION_INPUT_LIMIT:
        message_bytes = message_bytes[:ATTENTION_INPUT_LIMIT].decode("utf-8", "ignore").encode("utf-8")
    command = [str(launcher), "attention-once", "--session", normalized_session,
               "--event-key", event_key]
    try:
        returncode, output, timed_out, oversized = _bounded_attention_process(command, message_bytes)
    except OSError as error:
        return _attention_unavailable(event_key, f"Hailing Frequencies could not be started: {error}")
    if timed_out:
        return _attention_record(event_key, "uncertain", True,
                                 "Hailing Frequencies timed out; delivery may have been attempted. No retry was made.")
    if oversized:
        return _attention_record(event_key, "uncertain", True,
                                 "Hailing Frequencies returned oversized output; no retry was made.")
    try:
        response = json.loads(output.decode("utf-8"))
    except (ValueError, UnicodeError):
        return _attention_record(event_key, "uncertain", True,
                                 "Hailing Frequencies returned no valid receipt; no retry was made.")
    if not isinstance(response, dict) or response.get("event_key") != event_key:
        return _attention_record(event_key, "uncertain", True,
                                 "Hailing Frequencies returned a mismatched receipt; no retry was made.")
    outcome = response.get("outcome")
    attempted = response.get("attempted")
    detail = response.get("detail")
    if outcome not in ("submitted", "uncertain", "held", "unavailable", "rejected") or type(attempted) is not bool:
        return _attention_record(event_key, "uncertain", True,
                                 "Hailing Frequencies returned an incomplete receipt; no retry was made.")
    if outcome == "submitted" and not attempted:
        return _attention_record(event_key, "uncertain", True,
                                 "Hailing Frequencies returned an inconsistent receipt; no retry was made.")
    if returncode != 0 and not (returncode == 2 and outcome == "rejected"):
        return _attention_record(event_key, "uncertain", True,
                                 "Hailing Frequencies exited without a confirmed outcome; no retry was made.")
    return _attention_record(event_key, outcome, attempted, detail)


def _is_timer_wake(prompt):
    """Recognize the native Claude paste envelope as well as plain timer wakes."""
    text = prompt.strip()
    pasted = re.fullmatch(r'<pasted_content(?: id="[^"]*")?>\s*(.*?)\s*'
                          r'</pasted_content(?: id="[^"]*")?>', text, re.DOTALL)
    if pasted:
        text = pasted.group(1)
    return bool(re.match(r"\s*Temporal Beacon timer [0-9a-fA-F-]+ is due\.", text))


def _arm_idle_timer(state, harness, session, intent, ci=None):
    """Arm one idle episode; timer wakes themselves never rearm it."""
    minutes = state.get("idle_warning_minutes", 0)
    if harness != "claude" or not minutes or not state.get("reminders") or not state.get("enabled"):
        return
    kind = os.environ.get("CMUX_AGENT_LAUNCH_KIND", "").lower()
    if kind not in ("claude", "claudeteams"):
        return
    inherited = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if inherited and inherited.lower() != session.lower():
        return
    launcher = Path.home() / ".local/bin/hailing-frequencies"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        state["idle_timer"] = {"status": "unavailable", "detail": "Hailing Frequencies is not installed"}
        return
    request = str(uuid.uuid5(uuid.NAMESPACE_URL, f"pattern-buffer-idle:{session}:{intent}:{minutes}"))
    message = (f"You have been idle for {minutes:g} minutes. Automatic compaction may occur in about five minutes; "
               "this timing is an estimate, and waking can reset the idle clock. "
               f"Review {state['checkpoint_path']} and refresh {state['draft_path']} now. "
               f"{SNAPSHOT_PRUNING} Do not start product work merely to service this reminder.")
    command = [str(launcher), "temporal", "set", "--after-idle", f"{minutes:g}m", "--message", message,
               "--request-id", request, "--replace-key", "pattern-buffer-checkpoint-idle"]
    try:
        code, output, timeout, oversized = _bounded_attention_process(command, b"")
        if timeout or oversized:
            state["idle_timer"] = {"status": "uncertain", "request_id": request,
                                   "detail": "Timer creation result was not confirmed; do not blindly retry"}
            return
        result = json.loads(output)
        timer = result.get("timer", {})
        if code or not timer.get("id"):
            raise ValueError("Timer creation was refused; inspect Temporal Beacon enrollment and doctor")
        state["idle_timer"] = {"status": "armed", "timer_id": timer["id"], "request_id": request,
                               "due_at": timer.get("due_at"), "delivery_capability": result.get("delivery_capability")}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        state["idle_timer"] = {"status": "unavailable", "request_id": request, "detail": str(error)[:256]}
    finally:
        if ci is not None:
            _record(ci, Path(state["identity"]["root"]), state, "reminder.armed",
                    state.get("idle_timer", {}).get("status"), request,
                    {"trigger": "idle", "timer_id": state.get("idle_timer", {}).get("timer_id")})


def _cancel_idle_timer(state, harness, session):
    timer = state.get("idle_timer") or {}
    if not timer.get("timer_id"):
        return
    if (harness != "claude" or os.environ.get("CMUX_AGENT_LAUNCH_KIND", "").lower() not in ("claude", "claudeteams")
            or os.environ.get("CLAUDE_CODE_SESSION_ID", session).lower() != session.lower()):
        state["idle_timer"] = {**timer, "status": "cancel_unconfirmed", "detail": "Cancel from the exact owning Claude session"}
        return
    command = [str(Path.home() / ".local/bin/hailing-frequencies"), "temporal", "cancel", timer["timer_id"]]
    try:
        code, output, timeout, oversized = _bounded_attention_process(command, b"")
        result = json.loads(output) if not timeout and not oversized else {}
        if code or result.get("status") not in ("canceled", "completed", "expired"):
            raise ValueError("Inspect Temporal Beacon: idle cancellation was not confirmed")
        state["idle_timer"] = {**timer, "status": result["status"], "submitted_input_recallable": False}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        state["idle_timer"] = {**timer, "status": "cancel_unconfirmed", "detail": str(error)[:256]}


def _claim(path, identity, ci):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if _json(path, ci) != identity:
            raise ValueError("external checkpoint path is claimed by another session")
        return
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(identity, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _check_path(path, missing_ok=False):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError(f"checkpoint path must be absolute: {path}")
    for item in reversed((path, *path.parents)):
        if item.is_symlink():
            raise ValueError(f"checkpoint paths must not contain symlinks: {item}")
        if not item.exists():
            if item == path and missing_ok:
                return False
            raise ValueError(f"checkpoint path component is missing: {item}")
        if item == path:
            if not item.is_file():
                raise ValueError(f"checkpoint destination must be a regular file: {item}")
        elif not item.is_dir():
            raise ValueError(f"checkpoint parent must be a directory: {item}")
    return True


def _ensure_directory_chain(path):
    path = Path(path)
    for item in reversed(path.parents):
        if item.is_symlink():
            raise ValueError(f"checkpoint metadata parent must not be a symlink: {item}")
        if item.exists() and not item.is_dir():
            raise ValueError(f"checkpoint metadata parent must be a directory: {item}")
    if path.is_symlink():
        raise ValueError(f"checkpoint metadata parent must not be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise ValueError(f"checkpoint metadata parent must be a directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def _lock_path(path):
    lock = path.with_suffix(".lock")
    _check_path(lock, missing_ok=True)
    return lock


def _draft_path(root, gitdir, harness, session):
    return root / ".context-injector" / "checkpoints" / _dir(gitdir, harness, session).name / "draft.md"


def _ensure_draft_directories(root, draft):
    current = root
    relative = draft.parent.relative_to(root)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"checkpoint draft parent must not be a symlink: {current}")
        if current.exists():
            if not current.is_dir():
                raise ValueError(f"checkpoint draft parent must be a directory: {current}")
        else:
            current.mkdir(mode=0o700)


def _git_path_ignored(ci, root, relative):
    try:
        ci.git(root, "ls-files", "--error-unmatch", "--", relative)
    except ValueError:
        pass
    else:
        raise ValueError(f"checkpoint draft is tracked: {relative}")
    try:
        ci.git(root, "check-ignore", "-q", "--", relative)
        return True
    except ValueError:
        return False


def _ensure_local_exclude(ci, root, relative):
    if _git_path_ignored(ci, root, relative):
        return
    common = Path(ci.git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    exclude = common / "info" / "exclude"
    lock = exclude.with_name("exclude.context-injector.lock")
    _check_path(exclude, missing_ok=True)
    _check_path(lock, missing_ok=True)
    with ci.locked(lock):
        _check_path(lock)
        _check_path(exclude, missing_ok=True)
        if _git_path_ignored(ci, root, relative):
            return
        existing = ci.bounded(exclude, 1024 * 1024) if exclude.exists() else ""
        rule = "/.context-injector/checkpoints/"
        content = existing + ("" if not existing or existing.endswith("\n") else "\n") + rule + "\n"
        ci.atomic(exclude, content)
        if not _git_path_ignored(ci, root, relative):
            raise ValueError("failed to ignore only the local checkpoint draft subtree")


def _verify_draft(ci, root, gitdir, harness, session, state, missing_ok=False):
    expected = _draft_path(root, gitdir, harness, session)
    draft = Path(state["draft_path"])
    if draft != expected:
        raise ValueError("checkpoint draft path changed from this session's local draft")
    _check_path(draft, missing_ok=missing_ok)
    relative = draft.relative_to(root).as_posix()
    if not _git_path_ignored(ci, root, relative):
        raise ValueError("checkpoint draft must remain locally ignored")
    return draft


def _draft_snapshot(draft):
    before = draft.stat()
    with draft.open("rb") as handle:
        raw = handle.read(NOTE_LIMIT + 1)
    after = draft.stat()
    signature = [after.st_mtime_ns, after.st_size, _digest(raw)]
    if (before.st_mtime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino):
        return None, signature, "checkpoint draft changed while it was being read"
    if after.st_size > NOTE_LIMIT or len(raw) > NOTE_LIMIT:
        return None, signature, f"checkpoint draft exceeds {NOTE_LIMIT} bytes"
    try:
        return raw.decode("utf-8"), signature, None
    except UnicodeError:
        return None, signature, "checkpoint draft is not valid UTF-8"


def _signature_time(signature):
    return datetime.datetime.fromtimestamp(signature[0] / 1_000_000_000,
                                           datetime.timezone.utc).isoformat(timespec="microseconds")


def _template():
    return "# Working checkpoint\n\n" + "\n\n".join("## " + section + "\nNone." for section in SECTIONS) + "\n"


def _note_budget(text):
    used = len(text.encode("utf-8")) if text is not None else None
    return {"note_bytes": used, "note_limit_bytes": NOTE_LIMIT,
            "note_remaining_bytes": NOTE_LIMIT - used if used is not None else None}


HEADER = "---\n# context-injector publication metadata\nkind: working-checkpoint\n"


def _note_body(text):
    """Strip only our fixed publication envelope, never arbitrary user YAML."""
    if not text.startswith(HEADER):
        return text
    lines = text[len(HEADER):].split("\n", 4)
    if len(lines) != 5 or lines[3] != "---":
        raise ValueError("invalid Pattern Buffer publication header")
    for line, key in zip(lines[:3], ("harness", "session", "published_at")):
        prefix = key + ": "
        if not line.startswith(prefix) or not isinstance(json.loads(line[len(prefix):]), str):
            raise ValueError("invalid Pattern Buffer publication header")
    return lines[4]


def _publication_header(identity, published_at):
    return HEADER + "".join(key + ": " + json.dumps(value, ensure_ascii=False) + "\n"
        for key, value in (("harness", identity["harness"]), ("session", identity["session"]),
                           ("published_at", published_at))) + "---\n"


def _published_note(text, identity, published_at):
    note = _publication_header(identity, published_at) + _note_body(text)
    _validate_note(note)
    return note


def _draft_digest(state):
    # Old headerless publications used one digest for both files.
    return state.get("published_draft_sha256", state.get("note_sha256"))


def _generation_text(state):
    if state.get("generation_uncertain") or state.get("pending_compact_boundary"):
        return "Compaction count not yet confirmed. This does not mean no compaction occurred."
    return f"Observed compaction generation since enrollment: {state.get('observed_generation', 0)}."


def _publish(ci, root, state, text, reviewed_at):
    published_at = _now()
    note = _published_note(text, state["identity"], published_at)
    revision = ci.git(root, "rev-parse", "HEAD")
    note_path = Path(state["checkpoint_path"])
    previous_path = Path(state["previous_path"])
    _check_path(note_path, missing_ok=True)
    _check_path(previous_path, missing_ok=True)
    old, warning = _read_note(state, ci)
    if old is not None and not warning:
        ci.atomic(previous_path, old)
    ci.atomic(note_path, note)
    state.update(note_sha256=_digest(note.encode()), reviewed_at=reviewed_at,
                 published_draft_sha256=_digest(text.encode()),
                 published_at=published_at, saved_revision=revision,
                 saved_generation=state.get("observed_generation", 0))
    _record(ci, root, state, "note.published", "published",
            state.get("pending_warning_correlation") or state["note_sha256"],
            {"bytes": len(note.encode()), "source_sha256": state["note_sha256"],
             "reviewed_at": reviewed_at, "published_at": published_at,
             "saved_generation": state.get("saved_generation")})
    return note


def _primary(ci, root, gitdir, harness, session):
    config, _ = ci.load(root, gitdir)
    role, _, binding = ci.resolve_role(root, gitdir, config, harness, session)
    if role != "orchestrator":
        raise ValueError("checkpoints require a primary session assigned to orchestrator")
    if binding.get("task_id"):
        raise ValueError("task-bound sessions cannot own checkpoints")
    return config


def _validate_note(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > NOTE_LIMIT:
        raise ValueError(f"checkpoint must be Markdown no larger than {NOTE_LIMIT} bytes")
    found = {}
    current = None
    first_heading = True
    for line in _note_body(text).splitlines():
        if first_heading and line == "# Working checkpoint":
            first_heading = False
            continue
        if line.startswith("## "):
            first_heading = False
            current = line[3:].strip()
            if current not in SECTIONS or current in found:
                raise ValueError("checkpoint must use each required level-two heading once")
            found[current] = []
        elif current:
            found[current].append(line)
        elif line.strip():
            raise ValueError("checkpoint must begin with ## Current state")
    if tuple(found) != SECTIONS:
        raise ValueError("checkpoint headings must appear in the required order: " + ", ".join(SECTIONS))
    for title, lines in found.items():
        body = "\n".join(lines).strip()
        if not body:
            raise ValueError(f"checkpoint section is empty: {title}")
    return text


def _validate_settings(compact_at, lead_points):
    if isinstance(compact_at, bool) or not isinstance(compact_at, (int, float)):
        raise ValueError("compact-at must be a number")
    if isinstance(lead_points, bool) or not isinstance(lead_points, (int, float)):
        raise ValueError("lead-points must be a number")
    if not 0 < lead_points < compact_at <= 100:
        raise ValueError("thresholds must satisfy 0 < lead-points < compact-at <= 100")


def _safe_checkpoint_path(path, root, ci, companions):
    path = Path(path).expanduser().absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("checkpoint paths must not be symlinks")
    resolved = path.resolve(strict=False)
    temp_roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp"), Path("/private/tmp")}
    if any(resolved == tmp or tmp in resolved.parents for tmp in temp_roots):
        raise ValueError("checkpoint paths under temporary directories are not allowed")
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise ValueError("checkpoint parent must not be a symlink")
    candidates = [path, *companions]
    checkouts = {root}
    parent = resolved.parent
    while parent != parent.parent:
        if (parent / ".git").exists():
            try:
                checkouts.add(ci.checkout(parent)[0])
            except (OSError, ValueError):
                pass
        parent = parent.parent
    for checkout in checkouts:
        for candidate in candidates:
            try:
                rel = candidate.resolve(strict=False).relative_to(checkout)
            except ValueError:
                continue
            try:
                ci.git(checkout, "ls-files", "--error-unmatch", str(rel))
                raise ValueError(f"checkpoint path is tracked in a Git checkout: {candidate}")
            except ValueError as error:
                if str(error).startswith("checkpoint path is tracked"):
                    raise
            try:
                ci.git(checkout, "check-ignore", "-q", "--", str(rel))
            except ValueError:
                raise ValueError(f"checkpoint path inside a Git checkout must be ignored: {candidate}")
    return path


def _read_note(state, ci):
    path = Path(state["checkpoint_path"])
    if not _check_path(path, missing_ok=True):
        return None, "checkpoint note is missing or is a symlink"
    try:
        note = ci.bounded(path, NOTE_LIMIT)
        if not state.get("note_sha256"):
            return note, "checkpoint note has not been reviewed with checkpoint-save"
        _validate_note(note)
    except (OSError, ValueError, UnicodeError) as error:
        return None, f"checkpoint note is corrupt: {error}"
    if _digest(note.encode()) != state.get("note_sha256"):
        return None, "checkpoint note changed outside checkpoint-save; save explicitly to review it again"
    return note, None


def _load_state(ci, root, gitdir, harness, session):
    path = _state_path(gitdir, harness, session)
    if not path.exists():
        raise ValueError("checkpoint is not initialized for this session")
    _check_path(path)
    state = _json(path, ci)
    if isinstance(state.get("version"), bool) or state.get("version") != 1:
        raise ValueError("unsupported checkpoint metadata version")
    if state.get("identity") != _identity(root, gitdir, harness, session):
        raise ValueError("checkpoint identity changed")
    required = ("checkpoint_path", "previous_path", "compact_at", "lead_points", "threshold_source",
                "reminders", "enabled", "reviewed_at", "saved_revision", "note_sha256",
                "saved_generation", "external", "observed_generation", "generation_uncertain",
                "generation_scope", "uncertainty", "transcript", "reminded_generation", "usage",
                "draft_path", "draft_signature", "published_at")
    missing = [key for key in required if key not in state]
    if missing:
        raise ValueError("checkpoint metadata is missing fields: " + ", ".join(missing))
    if not isinstance(state["checkpoint_path"], str) or not isinstance(state["previous_path"], str):
        raise ValueError("checkpoint paths in metadata must be strings")
    if not isinstance(state["enabled"], bool) or not isinstance(state["reminders"], bool):
        raise ValueError("checkpoint enable and reminder flags must be booleans")
    if not isinstance(state["external"], bool):
        raise ValueError("checkpoint external flag must be a boolean")
    if (not isinstance(state["observed_generation"], int) or state["observed_generation"] < 0
            or not isinstance(state["generation_uncertain"], bool)):
        raise ValueError("checkpoint generation metadata is invalid")
    if (state["reviewed_at"] is not None and not isinstance(state["reviewed_at"], str)
            or state["saved_revision"] is not None and not isinstance(state["saved_revision"], str)
            or state["note_sha256"] is not None and not isinstance(state["note_sha256"], str)):
        raise ValueError("checkpoint review metadata is invalid")
    if state["published_at"] is not None and not isinstance(state["published_at"], str):
        raise ValueError("checkpoint publication timestamp is invalid")
    if "published_draft_sha256" in state and not isinstance(state["published_draft_sha256"], str):
        raise ValueError("checkpoint draft digest is invalid")
    if state["saved_generation"] is not None and (
            isinstance(state["saved_generation"], bool) or not isinstance(state["saved_generation"], int)):
        raise ValueError("checkpoint saved generation is invalid")
    if state["reminded_generation"] is not None and (
            isinstance(state["reminded_generation"], bool) or not isinstance(state["reminded_generation"], int)):
        raise ValueError("checkpoint reminder generation is invalid")
    if state["uncertainty"] is not None and not isinstance(state["uncertainty"], str):
        raise ValueError("checkpoint uncertainty metadata is invalid")
    if not isinstance(state["generation_scope"], str) or not isinstance(state["threshold_source"], str):
        raise ValueError("checkpoint source metadata is invalid")
    if state["transcript"] is not None and (
            not isinstance(state["transcript"], dict) or
            not all(key in state["transcript"] for key in ("path", "dev", "ino", "offset"))):
        raise ValueError("checkpoint transcript metadata is invalid")
    if state["transcript"] is not None:
        transcript = state["transcript"]
        if (not isinstance(transcript["path"], str) or
                any(isinstance(transcript[key], bool) or not isinstance(transcript[key], int)
                    for key in ("dev", "ino", "offset"))):
            raise ValueError("checkpoint transcript metadata is invalid")
    if state["usage"] is not None and (
            not isinstance(state["usage"], dict) or
            not isinstance(state["usage"].get("timestamp"), (int, float)) or
            isinstance(state["usage"].get("used_percentage"), bool) or
            not isinstance(state["usage"].get("used_percentage"), (int, float))):
        raise ValueError("checkpoint usage metadata is invalid")
    idle_minutes = state.get("idle_warning_minutes", 0)
    if type(idle_minutes) not in (int, float) or not 0 <= idle_minutes <= 1440:
        raise ValueError("checkpoint idle warning duration is invalid")
    attention = state.get("attention_delivery")
    if attention is not None:
        if (not isinstance(attention, dict) or
                not all(key in attention for key in ("event_key", "outcome", "attempted", "detail")) or
                not isinstance(attention["event_key"], str) or
                attention["outcome"] not in ("pending", "submitted", "uncertain", "held", "unavailable", "rejected") or
                type(attention["attempted"]) is not bool or
                not isinstance(attention["detail"], str) or
                len(attention["detail"].encode("utf-8")) > ATTENTION_DETAIL_LIMIT):
            raise ValueError("checkpoint attention delivery metadata is invalid")
        try:
            uuid.UUID(attention["event_key"])
        except (ValueError, TypeError, AttributeError):
            raise ValueError("checkpoint attention event key is invalid")
    if (not isinstance(state["draft_path"], str) or not isinstance(state["draft_signature"], list)
            or len(state["draft_signature"]) != 3
            or any(isinstance(value, bool) or not isinstance(value, int)
                   for value in state["draft_signature"][:2])
            or not isinstance(state["draft_signature"][2], str)):
        raise ValueError("checkpoint draft metadata is invalid")
    _validate_settings(state["compact_at"], state["lead_points"])
    note_path = Path(state["checkpoint_path"])
    previous_path = Path(state["previous_path"])
    if not state["external"] and note_path != _dir(gitdir, harness, session) / "checkpoint.md":
        raise ValueError("canonical checkpoint path changed from this session's private note")
    expected_previous = (Path(str(note_path) + ".previous.md") if state.get("external")
                         else note_path.parent / "previous.md")
    if previous_path != expected_previous:
        raise ValueError("checkpoint previous-note path changed")
    _check_path(note_path, missing_ok=True)
    _check_path(previous_path, missing_ok=True)
    if state.get("external"):
        owner_path = Path(state["checkpoint_path"] + ".owner.json")
        _check_path(owner_path)
        if _json(owner_path, ci) != state["identity"]:
            raise ValueError("external checkpoint ownership claim changed")
    _verify_draft(ci, root, gitdir, harness, session, state, missing_ok=True)
    return path, state


def _effective_threshold(state, harness):
    compact = state["compact_at"]
    source = state.get("threshold_source", "operator configuration")
    if harness == "claude":
        override = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
        cached = state.get("usage")
        if (not override and isinstance(cached, dict)
                        and 0 <= time.time() - cached.get("timestamp", 0) <= 300):
            override = cached.get("compact_at")
        if override:
            try:
                value = float(override)
                _validate_settings(value, state["lead_points"])
                compact, source = value, "runtime Claude override"
            except (ValueError, TypeError):
                pass
    return compact, compact - state["lead_points"], source


def _report(ci, root, gitdir, harness, session, state, include_note=False):
    note, warning = _read_note(state, ci)
    revision = ci.git(root, "rev-parse", "HEAD")
    compact, reminder_threshold, effective_source = _effective_threshold(state, harness)
    elapsed = None
    if state.get("reviewed_at"):
        try:
            reviewed = datetime.datetime.fromisoformat(state["reviewed_at"].replace("Z", "+00:00"))
            elapsed = max(0, int((datetime.datetime.now(datetime.timezone.utc) - reviewed).total_seconds()))
        except (ValueError, TypeError):
            pass
    result = {"initialized": True, "checkpoint_path": state["checkpoint_path"],
              "identity": state.get("identity"), "enabled": state.get("enabled", False),
              "reminders": state.get("reminders", False),
              "reviewed_at": state.get("reviewed_at"), "saved_revision": state.get("saved_revision"),
              "published_at": state.get("published_at"),
              "seconds_since_review": elapsed,
              "current_revision": revision,
              "revision_mismatch": (state.get("saved_revision") != revision
                                    if state.get("saved_revision") is not None else None),
              "observed_generation": state.get("observed_generation", 0),
              "generation_scope": state.get("generation_scope", "since enrollment"),
              "saved_generation": state.get("saved_generation"),
              "generation_gap": (state.get("observed_generation", 0) - state["saved_generation"]
                                 if isinstance(state.get("saved_generation"), int) else None),
              "compact_at": state.get("compact_at"), "effective_compact_at": compact,
              "lead_points": state.get("lead_points"), "reminder_threshold": reminder_threshold,
              "threshold_source": state.get("threshold_source"),
              "effective_threshold_source": effective_source,
              "generation_uncertain": bool(state.get("generation_uncertain", False) or state.get("pending_compact_boundary")),
              "pending_compact_boundary": bool(state.get("pending_compact_boundary")),
              "uncertainty": state.get("uncertainty") or ("compaction boundary not yet persisted or duplicate recovery hook" if state.get("pending_compact_boundary") else None),
              "warnings": [warning] if warning else [], "delivery_state": "not_tracked"}
    result["idle_warning_minutes"] = state.get("idle_warning_minutes", 0)
    result["idle_timer"] = state.get("idle_timer")
    if (state.get("idle_timer") or {}).get("status") == "cancel_unconfirmed":
        result["warnings"].append("Idle timer cancellation is unconfirmed; inspect or cancel it from its exact owner.")
    if state.get("attention_delivery") is not None:
        result["attention_delivery"] = dict(state["attention_delivery"])
    result.update(_note_budget(note))
    result["draft_body_limit_bytes"] = NOTE_LIMIT - len(_publication_header(state["identity"], _now()).encode())
    script = os.path.abspath(ci.__file__)
    common = [os.sys.executable, script, "--cwd", str(root)]
    result["read_command"] = shlex.join([*common, "checkpoint-read", "--harness", harness,
                                         "--session", session])
    result["save_command"] = shlex.join([*common, "checkpoint-save", "--harness", harness,
                                         "--session", session, "--file", "<MARKDOWN_FILE>"])
    result["draft_path"] = state["draft_path"]
    draft = Path(state["draft_path"])
    result["draft_pending"] = False
    if draft.exists():
        _, signature, draft_error = _draft_snapshot(draft)
        result["draft_pending"] = signature[2] != _draft_digest(state) or signature != state["draft_signature"]
        if result["draft_pending"]:
            result["warnings"].append("Editable draft has unpublished content; the recovery note is still the last reviewed version.")
        if draft_error:
            result["warnings"].append(draft_error)
    else:
        result["warnings"].append("Editable draft is missing; recreate it from the reviewed note before updating.")
    if include_note:
        result["note"] = note
    return result


def handle_cli(ci, root, gitdir, args):
    harness, session, command = args.harness, args.session, args.command
    if harness not in ("claude", "codex") or not session:
        raise ValueError("checkpoint commands require a supported harness and session")
    _primary(ci, root, gitdir, harness, session)
    state_path = _state_path(gitdir, harness, session)
    if command == "checkpoint-init":
        _ensure_directory_chain(state_path.parent)
    _check_path(state_path, missing_ok=True)
    lock = _lock_path(state_path)
    with ci.locked(lock):
        if command == "checkpoint-init":
            if state_path.exists():
                raise ValueError("checkpoint is already initialized")
            compact = args.compact_at
            source = "operator configuration"
            if compact is None:
                if harness == "codex":
                    raise ValueError("Codex requires explicit --compact-at because its runtime threshold is unknown")
                compact, source = 60.0, "operator fallback 60; runtime override is honored"
            lead = args.lead_points
            _validate_settings(compact, lead)
            folder = state_path.parent
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            note_path = folder / "checkpoint.md"
            if args.path:
                note_path = _safe_checkpoint_path(args.path, root, ci,
                                                  [Path(str(args.path) + ".previous.md"),
                                                   Path(str(args.path) + ".owner.json")])
            owner_path = Path(str(note_path) + ".owner.json")
            previous_path = Path(str(note_path) + ".previous.md") if args.path else note_path.parent / "previous.md"
            owner = _identity(root, gitdir, harness, session)
            if any(Path(item).is_symlink() for item in (note_path, previous_path, owner_path)):
                raise ValueError("checkpoint files and companions must not be symlinks")
            _check_path(note_path, missing_ok=True)
            _check_path(previous_path, missing_ok=True)
            if args.path:
                _check_path(owner_path, missing_ok=True)
            if note_path.exists() and not args.path:
                raise ValueError("checkpoint note already exists; choose a new path")
            draft = _draft_path(root, gitdir, harness, session)
            if note_path == draft:
                raise ValueError("canonical checkpoint and editable draft must use separate paths")
            relative_draft = draft.relative_to(root).as_posix()
            _ensure_draft_directories(root, draft)
            _ensure_local_exclude(ci, root, relative_draft)
            if draft.exists() or draft.is_symlink():
                raise ValueError(f"checkpoint draft already exists and will not be overwritten: {draft}")
            if args.path:
                _claim(owner_path, owner, ci)
            template = _template()
            ci.atomic(draft, template)
            _, draft_signature, draft_error = _draft_snapshot(draft)
            if draft_error:
                raise ValueError(draft_error)
            state = {"version": 1, "identity": owner, "checkpoint_path": str(note_path),
                     "previous_path": str(previous_path),
                     "compact_at": float(compact), "lead_points": float(lead),
                     "threshold_source": source, "reminders": True, "enabled": True,
                     "reviewed_at": None, "saved_revision": None, "note_sha256": None,
                     "saved_generation": None, "external": bool(args.path),
                     "observed_generation": 0, "generation_uncertain": True,
                     "generation_scope": "since enrollment",
                     "uncertainty": "no transcript provenance observed yet", "transcript": None,
                     "reminded_generation": None, "usage": None,
                     "draft_path": str(draft), "draft_signature": draft_signature,
                     "published_at": None}
            state["idle_warning_minutes"] = 50 if harness == "claude" else 0
            _arm_idle_timer(state, harness, session, "enrollment", ci)
            _write_json(state_path, state, ci)
            result = _report(ci, root, gitdir, harness, session, state)
            result["template"] = template
            return result
        _, state = _load_state(ci, root, gitdir, harness, session)
        if command == "checkpoint-config":
            if args.idle_warning_minutes is not None:
                minutes = args.idle_warning_minutes
                if harness != "claude" or not 0 <= minutes <= 1440:
                    raise ValueError("idle warnings require Claude and a finite duration from 0 to 1440 minutes")
                state["idle_warning_minutes"] = minutes
            if args.compact_at is not None:
                state["compact_at"] = args.compact_at
                state["threshold_source"] = "operator configuration"
            if args.lead_points is not None:
                state["lead_points"] = args.lead_points
            _validate_settings(state["compact_at"], state["lead_points"])
            if args.reminders is not None:
                state["reminders"] = args.reminders == "on"
            if args.enabled is not None:
                state["enabled"] = args.enabled == "on"
            if harness == "claude":
                if not state.get("idle_warning_minutes", 0) or not state["enabled"] or not state["reminders"]:
                    _cancel_idle_timer(state, harness, session)
                elif args.idle_warning_minutes is not None:
                    _arm_idle_timer(state, harness, session, "configuration:" + _now(), ci)
            _write_json(state_path, state, ci)
            return _report(ci, root, gitdir, harness, session, state)
        if command == "checkpoint-save":
            draft = _verify_draft(ci, root, gitdir, harness, session, state, missing_ok=True)
            raw = _note_body(ci.bounded(Path(args.file).expanduser().absolute(), NOTE_LIMIT))
            _validate_note(raw)
            _verify_draft(ci, root, gitdir, harness, session, state, missing_ok=True)
            _publish(ci, root, state, raw, _now())
            ci.atomic(draft, raw)
            _, signature, error = _draft_snapshot(draft)
            if error:
                raise ValueError(error)
            state["draft_signature"] = signature
            _write_json(state_path, state, ci)
            return _report(ci, root, gitdir, harness, session, state)
        if command == "checkpoint-status":
            return _report(ci, root, gitdir, harness, session, state)
        if command == "checkpoint-read":
            return _report(ci, root, gitdir, harness, session, state, True)
    raise ValueError("unknown checkpoint command")


def _transcript_identity(ci, root, harness, session, payload):
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not Path(path).is_absolute():
        return None, None
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return None, None
    stat = path.stat()
    with path.open("rb") as handle:
        head = handle.read(TRACE_LIMIT)
    meta_id = None
    transcript_session = None
    for line in head.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(row, dict) and row.get("type") == "session_meta":
            meta_id = row.get("payload", {}).get("id") if isinstance(row.get("payload"), dict) else row.get("id")
        if isinstance(row, dict) and isinstance(row.get("sessionId"), str):
            transcript_session = row["sessionId"]
    if harness == "codex":
        if meta_id != session:
            return None, None
    else:
        declared = payload.get("sessionId")
        if ((declared is not None and declared != session) or
                (transcript_session is not None and transcript_session != session)):
            return None, None
    return path, {"path": str(path.resolve()), "dev": stat.st_dev, "ino": stat.st_ino}


def _usage(state, harness, payload, transcript, identity):
    now = time.time()
    if harness == "claude":
        cached = state.get("usage")
        if isinstance(cached, dict):
            age = now - cached.get("timestamp", 0)
            if 0 <= age <= 300:
                return cached.get("used_percentage")
        return None
    if transcript is None or identity is None:
        return None
    try:
        size = transcript.stat().st_size
        start = max(0, size - TRACE_LIMIT)
        with transcript.open("rb") as handle:
            handle.seek(start)
            data = handle.read(TRACE_LIMIT)
        for line in reversed(data.splitlines()):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict):
                continue
            payload = row.get("payload", {})
            if row.get("type") != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            info = payload.get("info", {})
            if not isinstance(info, dict):
                continue
            usage = info.get("last_token_usage", {})
            if not isinstance(usage, dict):
                continue
            count = usage.get("total_tokens")
            window = info.get("model_context_window")
            stamp = row.get("timestamp")
            if (isinstance(count, (int, float)) and count >= 0 and
                    isinstance(window, (int, float)) and window > 0):
                parsed = (datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                          if isinstance(stamp, str) else float(stamp)
                          if isinstance(stamp, (int, float)) else 0)
                age = now - parsed
                if 0 <= age <= 300:
                    return min(100.0, 100.0 * count / window)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, AttributeError):
        pass
    return None


def _count_boundaries(harness, data, session):
    count = 0
    uncertain = False
    if data and not data.endswith(b"\n"):
        data = data[:data.rfind(b"\n") + 1]
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            uncertain = True
            continue
        if not isinstance(row, dict):
            continue
        if harness == "codex":
            if row.get("type") == "compacted":
                count += 1
        elif row.get("type") == "system" and row.get("subtype") == "compact_boundary":
            count += 1
    return count, uncertain


def _ends_with_boundary(harness, data):
    if data and not data.endswith(b"\n"):
        data = data[:data.rfind(b"\n") + 1]
    for line in reversed(data.splitlines()):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(row, dict):
            continue
        return (row.get("type") == "compacted" if harness == "codex" else
                row.get("type") == "system" and row.get("subtype") == "compact_boundary")
    return False


def event(ci, root, gitdir, harness, payload):
    name = payload.get("hook_event_name")
    if name not in ("SessionStart", "PreCompact", "PostToolUse", "UserPromptSubmit"):
        return ""
    session = payload.get("session_id")
    if not isinstance(session, str) or not session or payload.get("agent_id"):
        return ""
    if not (gitdir / "context-injector" / "config.json").exists():
        return ""
    config, _ = ci.load(root, gitdir)
    if not ci.enabled(config):
        return ""
    role, _, binding = ci.resolve_role(root, gitdir, config, harness, session)
    if role != "orchestrator" or binding.get("task_id"):
        return ""
    state_path = _state_path(gitdir, harness, session)
    if not state_path.exists():
        return ""
    _check_path(state_path)
    try:
        with ci.locked(_lock_path(state_path)):
            _, state = _load_state(ci, root, gitdir, harness, session)
            if not state.get("enabled"):
                return ""
            prompt = payload.get("prompt")
            if (name == "UserPromptSubmit" and isinstance(prompt, str) and prompt.strip()
                    and not _is_timer_wake(prompt)):
                trace = state.get("transcript") or {}
                intent = _digest(json.dumps([prompt, trace.get("offset"), trace.get("path")]).encode())
                _arm_idle_timer(state, harness, session, intent, ci)
                _write_json(state_path, state, ci)
            generation_before = state["observed_generation"]
            transcript, identity = _transcript_identity(ci, root, harness, session, payload)
            if transcript is None and name == "SessionStart" and not payload.get("transcript_path"):
                state["generation_uncertain"] = True
                state["uncertainty"] = "transcript provenance unavailable at SessionStart"
                _write_json(state_path, state, ci)
            if name == "PreCompact":
                if transcript is not None:
                    stat = transcript.stat()
                    with transcript.open("rb") as handle:
                        handle.seek(max(0, stat.st_size - TRACE_LIMIT))
                        capture = handle.read(TRACE_LIMIT)
                    state["precompact"] = {"identity": identity, "size": stat.st_size,
                                            "mtime_ns": stat.st_mtime_ns, "tail_sha256": _digest(capture),
                                            "captured_at": _now(), "note_sha256": state.get("note_sha256"),
                                            "reviewed_at": state.get("reviewed_at")}
                    _write_json(state_path, state, ci)
                return ""
            if transcript is not None:
                previous = state.get("transcript")
                size = transcript.stat().st_size
                if previous is None:
                    if name == "SessionStart" and payload.get("source") == "compact":
                        begin = max(0, size - TRACE_LIMIT)
                        with transcript.open("rb") as handle:
                            handle.seek(begin)
                            data = handle.read(TRACE_LIMIT)
                        count, uncertain = _count_boundaries(harness, data, session)
                        boundary = _ends_with_boundary(harness, data)
                        state["observed_generation"] += 1 if boundary else 0
                        state["generation_uncertain"] = uncertain or not boundary
                        state["uncertainty"] = "compact boundary provenance incomplete" if state["generation_uncertain"] else None
                    else:
                        state["observed_generation"] = 0
                        state["generation_uncertain"] = False
                        state["uncertainty"] = None
                    state["transcript"] = {**identity, "offset": size}
                    _write_json(state_path, state, ci)
                elif (previous.get("path") != identity["path"] or previous.get("dev") != identity["dev"]
                      or previous.get("ino") != identity["ino"] or size < previous.get("offset", 0)):
                    state["generation_uncertain"] = True
                    state["uncertainty"] = "transcript replaced, truncated, or reset; generation count may be incomplete"
                    state["transcript"] = {**identity, "offset": size}
                    _write_json(state_path, state, ci)
                else:
                    offset = previous.get("offset", size)
                    gap = size - offset
                    if gap > TRACE_LIMIT:
                        state["generation_uncertain"] = True
                        state["uncertainty"] = "unread transcript gap exceeded bounded scan"
                        offset = size - TRACE_LIMIT
                    with transcript.open("rb") as handle:
                        handle.seek(offset)
                        data = handle.read(TRACE_LIMIT)
                        end = handle.tell()
                    count, malformed = _count_boundaries(harness, data, session)
                    state["observed_generation"] += count
                    if malformed:
                        state["generation_uncertain"] = True
                        state["uncertainty"] = "transcript contains an incomplete or unreadable record"
                    if data and not data.endswith(b"\n"):
                        complete = data.rfind(b"\n") + 1
                        end = offset + complete if complete else offset
                    state["transcript"] = {**identity, "offset": end}
                    _write_json(state_path, state, ci)
            generation_changed = state["observed_generation"] > generation_before
            if generation_changed:
                state["pending_compact_boundary"] = False
            elif name == "SessionStart" and payload.get("source") == "compact":
                # Some hosts persist the boundary only after this hook returns.
                # A duplicate hook is indistinguishable without new provenance.
                state["pending_compact_boundary"] = True
            _write_json(state_path, state, ci)
            if name == "SessionStart" and payload.get("source") == "compact":
                correlation = _digest(json.dumps([session, state.get("transcript"),
                                                 state.get("observed_generation")], sort_keys=True).encode())
                uncertain = bool(state.get("generation_uncertain") or state.get("pending_compact_boundary"))
                _record(ci, root, state, "compaction.unknown" if uncertain else "compaction.observed",
                        "uncertain" if uncertain else "observed", correlation,
                        {"generation": state.get("observed_generation"), "generation_uncertain": uncertain,
                         "reviewed_at": state.get("reviewed_at"), "trigger": "compact"})
                _record(ci, root, state, "recovery.observed", "hook_requested", correlation,
                        {"reviewed_at": state.get("reviewed_at"), "generation_uncertain": uncertain})
            if name == "PostToolUse" and Path(state["draft_path"]).exists():
                draft = _verify_draft(ci, root, gitdir, harness, session, state)
                draft_text, signature, draft_error = _draft_snapshot(draft)
                if signature != state["draft_signature"]:
                    _record(ci, root, state, "note.draft_changed", "changed", signature[2],
                            {"bytes": len(draft_text.encode()) if draft_text else 0})
                    state["draft_signature"] = signature
                    if draft_error:
                        _record(ci, root, state, "note.rejected", "invalid", signature[2])
                        _write_json(state_path, state, ci)
                        return f"Checkpoint draft was not published: {draft_error}. Fix {draft} and retry with a new edit."
                    try:
                        note = _publish(ci, root, state, draft_text, _signature_time(signature))
                    except ValueError as error:
                        _record(ci, root, state, "note.rejected", "invalid", signature[2])
                        _write_json(state_path, state, ci)
                        return f"Checkpoint draft was not published: {error}. Fix {draft} and retry with a new edit."
                    _write_json(state_path, state, ci)
                    budget = _note_budget(note)
                    return (f"Checkpoint published: {state['checkpoint_path']}. "
                            f"Reviewed at {state['reviewed_at']}; published at {state['published_at']}. "
                            f"Note budget: {budget['note_bytes']}/{NOTE_LIMIT} UTF-8 bytes; "
                            f"{budget['note_remaining_bytes']} bytes remaining. " +
                            _generation_text(state) + " " +
                            "This records the reviewed draft, not verification of its claims or new authority.")
            usage = _usage(state, harness, payload, transcript, identity)
            compact = state["compact_at"]
            threshold_source = state.get("threshold_source", "operator configuration")
            if harness == "claude":
                override = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
                cached = state.get("usage")
                if (not override and isinstance(cached, dict)
                        and 0 <= time.time() - cached.get("timestamp", 0) <= 300):
                    override = cached.get("compact_at")
                if override:
                    try:
                        compact = float(override)
                        _validate_settings(compact, state["lead_points"])
                        threshold_source = "runtime Claude override"
                    except (ValueError, TypeError):
                        compact = state["compact_at"]
            threshold_note = (f"Configured threshold is {state['compact_at']:.1f}%, "
                              f"runtime threshold is {compact:.1f}% ({threshold_source}).")
            threshold = compact - state["lead_points"]
            if name == "SessionStart":
                note, warning = _read_note(state, ci)
                command = shlex.join([os.sys.executable, os.path.abspath(ci.__file__), "--cwd", str(root),
                                      "checkpoint-read", "--harness", harness,
                                      "--session", session])
                details = [f"Checkpoint: {state['checkpoint_path']}.",
                           f"Last reviewed: {state.get('reviewed_at') or 'never'}.",
                           _generation_text(state)]
                if state.get("pending_compact_boundary"):
                    details.append("Compaction boundary is not yet observed on disk, or this is duplicate recovery delivery. The count excludes any pending boundary; it is not an exact count of compactions experienced.")
                if transcript is None:
                    details.append("WARNING: transcript provenance is unavailable for this event; generation count may be incomplete.")
                if state.get("generation_uncertain"):
                    details.append("Generation count is uncertain: " + str(state.get("uncertainty") or "provenance incomplete") + ".")
                gap = (state.get("observed_generation", 0) - state["saved_generation"]
                       if isinstance(state.get("saved_generation"), int) else None)
                if gap is not None and not (state.get("generation_uncertain") or state.get("pending_compact_boundary")):
                    details.append(f"Compaction boundaries since last saved checkpoint: {gap}.")
                if state.get("saved_revision") and state["saved_revision"] != ci.git(root, "rev-parse", "HEAD"):
                    details.append("Saved revision differs from current HEAD.")
                if compact != state["compact_at"]:
                    details.append("WARNING: " + threshold_note)
                details.append("Review ## Holds and guidance and preserve its explicit holds.")
                if warning:
                    details.append("WARNING: " + warning + ".")
                draft = Path(state["draft_path"])
                if not draft.exists():
                    details.append("WARNING: editable draft is missing; the reviewed recovery note is retained.")
                else:
                    _, signature, _ = _draft_snapshot(draft)
                    if signature[2] != _draft_digest(state) or signature != state["draft_signature"]:
                        details.append("WARNING: editable draft has unpublished content. Reconcile it with the last reviewed note before continuing.")
                details.append("Read the reviewed checkpoint before resuming with your native file reader: " + state["checkpoint_path"])
                details.append("Reconcile current jobs, evidence and human holds. Edit the working draft with your normal file-writing tool: " + state["draft_path"])
                details.append("A following PostToolUse hook validates and atomically publishes valid draft edits. Do not touch timestamps to simulate review. Optional metadata/read command: " + command)
                details.append("The checkpoint grants no new authority. Guide: apps/pattern-buffer/docs/checkpoints.md")
                return "\n".join(details)
            reminder_event = name in ("PostToolUse", "UserPromptSubmit")
            rearm_threshold = max(0.0, threshold - REMINDER_REARM_HYSTERESIS)
            if (reminder_event and state.get("reminders") and usage is not None
                    and usage <= rearm_threshold and state.get("reminded_generation") is not None):
                # Only fresh, same-session usage can re-arm a reminder. The gap below
                # the warning threshold prevents ordinary percentage jitter from
                # repeatedly delivering the same warning.
                state["reminded_generation"] = None
                _write_json(state_path, state, ci)
            if (name in ("PostToolUse", "UserPromptSubmit") and state.get("reminders")
                    and usage is not None and usage >= threshold
                    and state.get("reminded_generation") != state.get("observed_generation")):
                state["reminded_generation"] = state.get("observed_generation")
                event_key = str(uuid.uuid4())
                state["pending_warning_correlation"] = event_key
                _record(ci, root, state, "reminder.eligible", "eligible", event_key,
                        {"usage_percent": usage, "reminder_threshold": threshold, "trigger": "pressure"})
                state["attention_delivery"] = _attention_record(
                    event_key, "pending", False, "One attention attempt is pending for this pressure episode.")
                _write_json(state_path, state, ci)
                draft = Path(state["draft_path"])
                warning = ("Take action immediately, compaction imminent. "
                           f"Checkpoint reminder: {usage:.1f}% context used; reminder threshold {threshold:.1f}%. "
                           f"Before further substantial work, review {state['checkpoint_path']} and edit {draft} "
                           "with your normal file-writing tool. A following PostToolUse hook validates and atomically publishes valid edits. "
                           f"{SNAPSHOT_PRUNING} "
                           "Retain all required headings and meaningful content. If the draft is invalid, the hook gives one advisory for that version. "
                           "This reminder does not authorize product work or job changes.")
                try:
                    state["attention_delivery"] = _attention_once(harness, session, event_key, warning)
                except Exception as error:
                    state["attention_delivery"] = _attention_record(
                        event_key, "uncertain", True,
                        f"Attention adapter failed ({type(error).__name__}); no retry was made.")
                outcome = state["attention_delivery"].get("outcome")
                _record(ci, root, state, "delivery." + {
                    "submitted": "submitted", "held": "held", "uncertain": "uncertain"
                }.get(outcome, "refused"), outcome, event_key, {"trigger": "pressure"})
                _record(ci, root, state, "delivery.emitted", "emitted", event_key, {"trigger": "pressure"})
                try:
                    _write_json(state_path, state, ci)
                except (OSError, ValueError, TypeError):
                    pass
                return warning
            if name == "PostToolUse" and generation_changed:
                return (f"Checkpoint freshness update: observed compaction generation is now {state['observed_generation']}. "
                        f"The last reviewed note was saved in observed generation {state.get('saved_generation')}. "
                        "The previously pending transcript boundary is now visible; reconcile the note with live state.")
    except (OSError, ValueError, TypeError, KeyError):
        raise
    return ""


def statusline(ci, harness, payload):
    if harness != "claude" or not isinstance(payload, dict):
        return None
    session = payload.get("session_id") or payload.get("sessionId")
    workspace = payload.get("workspace")
    cwd = payload.get("cwd") or payload.get("current_dir")
    if not cwd and isinstance(workspace, dict):
        cwd = workspace.get("project_dir") or workspace.get("current_dir")
    window_info = payload.get("context_window")
    percentage = (window_info.get("used_percentage") if isinstance(window_info, dict)
                  else payload.get("used_percentage"))
    if not isinstance(session, str) or not session or not isinstance(cwd, str):
        return None
    if isinstance(percentage, bool) or not isinstance(percentage, (int, float)):
        return None
    try:
        root, gitdir = ci.checkout(cwd)
        config, _ = ci.load(root, gitdir)
        if not ci.enabled(config):
            return None
        role, _, binding = ci.resolve_role(root, gitdir, config, harness, session)
        if role != "orchestrator" or binding.get("task_id"):
            return None
        path = _state_path(gitdir, harness, session)
        _check_path(path)
        with ci.locked(_lock_path(path)):
            _, state = _load_state(ci, root, gitdir, harness, session)
            if state.get("enabled"):
                compact_at = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
                if compact_at:
                    try:
                        compact_at = float(compact_at)
                        _validate_settings(compact_at, state["lead_points"])
                    except (ValueError, TypeError):
                        compact_at = None
                model = payload.get("model")
                if isinstance(model, dict):
                    model = model.get("id")
                window_size = (window_info.get("context_window_size") if isinstance(window_info, dict)
                               else payload.get("context_window"))
                state["usage"] = {"used_percentage": float(percentage), "timestamp": time.time(),
                                  "model": model,
                                  "context_window": window_size,
                                  "compact_at": compact_at}
                _write_json(path, state, ci)
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return None
