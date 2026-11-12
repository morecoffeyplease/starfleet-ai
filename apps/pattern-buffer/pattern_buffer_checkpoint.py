"""Checkpoint support for Pattern Buffer. Standard library only."""
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import re
import shlex
import stat
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urlsplit

NOTE_LIMIT = 8192
TRACE_LIMIT = 256 * 1024
RECOVERY_PART_BYTES = 2048
RECOVERY_MAX_PARTS = math.ceil(NOTE_LIMIT / (RECOVERY_PART_BYTES - 3))
RECOVERY_PENDING_READ_LIMIT = 32
REMINDER_REARM_HYSTERESIS = 5.0
ATTENTION_INPUT_LIMIT = 4096
ATTENTION_OUTPUT_LIMIT = 8192
ATTENTION_TIMEOUT_SECONDS = 3
ATTENTION_DETAIL_LIMIT = 512
SECTIONS = ("Current state", "Next action", "Decisions and uncertainty",
            "Active work", "Evidence", "Holds and guidance")
OPENCODE_SESSION_ID = re.compile(r"^ses_[A-Za-z0-9]+$")
OPENCODE_EVENT_TYPES = {"compaction-started", "compaction-ended", "compaction-failed"}
OPENCODE_USAGE_MAX_AGE_SECONDS = 300
OPENCODE_PRESSURE_DESCRIPTOR_MAX_AGE_SECONDS = 900
OPENCODE_PRESSURE_DESCRIPTOR_LIMIT = 16 * 1024
OPENCODE_PRESSURE_OBSERVATION_LIMIT = 8 * 1024
OPENCODE_PRESSURE_STATUSES = {
    "unknown", "configured", "awaiting_usage", "below_threshold",
    "warning_eligible", "warning_emitted", "warning_deduplicated",
}
OPENCODE_PRESSURE_REASONS = {
    None, "descriptor_missing", "descriptor_expired", "context_read_failed",
    "assistant_missing", "usage_invalid", "usage_stale", "identity_mismatch",
    "auto_compaction_disabled", "new_generation", "checkpoint_disabled",
}
SNAPSHOT_PRUNING = (
    "Refresh this as a current snapshot. "
    "Preserve the specific facts needed to continue correctly without relying on the automatic summary, and record each fact once. "
    "Keep the next action and active work, current decisions and relevant rejected approaches, "
    "exact commands needed to continue or recover, exact active job IDs, "
    "evidence pointers, uncertainties and specific human grants or holds. "
    "Reference mandatory guidance instead of repeating general procedures, and prune superseded state and completed history. "
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
    if state.get("identity", {}).get("harness") == "opencode":
        return (f"Observed {state.get('observed_generation', 0)} successful OpenCode compaction completion event(s) "
                "since enrollment; complete event coverage remains uncertain.")
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


def _empty_recovery_state():
    return {"generation": 0, "event_correlation": None, "required": False,
            "freshness": "unknown", "freshness_reason": "no observed recovery generation",
            "boundary_generation": None, "received": None, "receipt": None,
            "pending_reads": []}


def _validate_recovery_state(state):
    recovery = state.setdefault("recovery", _empty_recovery_state())
    fields = {"generation", "event_correlation", "required", "freshness", "freshness_reason",
              "boundary_generation", "received", "receipt", "pending_reads"}
    if not isinstance(recovery, dict) or set(recovery) != fields:
        raise ValueError("checkpoint recovery metadata is invalid")
    generation = recovery.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("checkpoint recovery generation is invalid")
    correlation = recovery.get("event_correlation")
    if correlation is not None and (not isinstance(correlation, str)
                                    or re.fullmatch(r"[0-9a-f]{64}", correlation) is None):
        raise ValueError("checkpoint recovery event correlation is invalid")
    if not isinstance(recovery.get("required"), bool):
        raise ValueError("checkpoint recovery requirement flag is invalid")
    if recovery.get("freshness") not in ("unknown", "current", "stale"):
        raise ValueError("checkpoint recovery freshness is invalid")
    if recovery.get("freshness_reason") is not None and not isinstance(recovery["freshness_reason"], str):
        raise ValueError("checkpoint recovery freshness reason is invalid")
    boundary = recovery.get("boundary_generation")
    if boundary is not None and (isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 0):
        raise ValueError("checkpoint recovery boundary generation is invalid")
    for key in ("received", "receipt"):
        record = recovery.get(key)
        if record is None:
            continue
        expected = ({"note_sha256", "published_at", "part_count", "parts"}
                    if key == "received" else
                    {"generation", "note_sha256", "published_at", "part_count", "completed_at"})
        if not isinstance(record, dict) or set(record) != expected:
            raise ValueError("checkpoint recovery receipt is invalid")
        digest = record.get("note_sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("checkpoint recovery receipt digest is invalid")
        if not isinstance(record.get("published_at"), str):
            raise ValueError("checkpoint recovery receipt publication time is invalid")
        part_count = record.get("part_count")
        if (isinstance(part_count, bool) or not isinstance(part_count, int)
                or not 1 <= part_count <= RECOVERY_MAX_PARTS):
            raise ValueError("checkpoint recovery receipt part count is invalid")
        if key == "received":
            parts = record.get("parts")
            if (not isinstance(parts, list) or len(parts) > part_count
                    or any(isinstance(part, bool) or not isinstance(part, int)
                           or not 1 <= part <= part_count for part in parts)
                    or len(set(parts)) != len(parts)):
                raise ValueError("checkpoint recovery read parts are invalid")
        else:
            receipt_generation = record.get("generation")
            if (isinstance(receipt_generation, bool) or not isinstance(receipt_generation, int)
                    or receipt_generation < 1 or not isinstance(record.get("completed_at"), str)):
                raise ValueError("checkpoint recovery receipt generation is invalid")
    pending = recovery.get("pending_reads")
    if not isinstance(pending, list) or len(pending) > RECOVERY_PENDING_READ_LIMIT:
        raise ValueError("checkpoint pending recovery reads are invalid")
    for item in pending:
        if not isinstance(item, dict) or set(item) != {
                "tool_use_id", "command_sha256", "generation", "note_sha256",
                "published_at", "part", "part_count"}:
            raise ValueError("checkpoint pending recovery read is invalid")
        if not isinstance(item.get("tool_use_id"), str) or not item["tool_use_id"] or len(item["tool_use_id"]) > 512:
            raise ValueError("checkpoint pending recovery tool ID is invalid")
        if any(not isinstance(item.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", item[key]) is None
               for key in ("command_sha256", "note_sha256")):
            raise ValueError("checkpoint pending recovery digest is invalid")
        if not isinstance(item.get("published_at"), str):
            raise ValueError("checkpoint pending recovery publication time is invalid")
        pending_generation = item.get("generation")
        if isinstance(pending_generation, bool) or not isinstance(pending_generation, int) or pending_generation < 1:
            raise ValueError("checkpoint pending recovery generation is invalid")
        for key in ("part", "part_count"):
            value = item.get(key)
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not 1 <= value <= RECOVERY_MAX_PARTS):
                raise ValueError("checkpoint pending recovery part is invalid")
        if item["part"] > item["part_count"]:
            raise ValueError("checkpoint pending recovery part is out of range")
    return recovery


def _checkpoint_part_ranges(data, size=RECOVERY_PART_BYTES):
    if not data:
        return [(0, 0)]
    ranges = []
    start = 0
    while start < len(data):
        end = min(start + size, len(data))
        while end < len(data) and end > start and data[end] & 0xC0 == 0x80:
            end -= 1
        if end == start:
            raise ValueError("checkpoint read part size cannot contain a UTF-8 character")
        ranges.append((start, end))
        start = end
    return ranges


def _checkpoint_read_frame(state, note, part):
    data = note.encode("utf-8")
    ranges = _checkpoint_part_ranges(data)
    if isinstance(part, bool) or not isinstance(part, int) or not 1 <= part <= len(ranges):
        raise ValueError(f"--part must be from 1 to {len(ranges)}")
    start, end = ranges[part - 1]
    piece = data[start:end]
    identity = state["identity"]
    frame = (f"--- Pattern Buffer checkpoint read v1 ---\n"
             f"harness: {identity['harness']}\n"
             f"session: {identity['session']}\n"
             f"recovery-generation: {_validate_recovery_state(state)['generation']}\n"
             f"checkpoint-sha256: {state['note_sha256']}\n"
             f"published-at: {state['published_at']}\n"
             f"part: {part} of {len(ranges)}\n"
             f"part-sha256: {_digest(piece)}\n"
             f"part-bytes: {len(piece)}\n"
             "--- checkpoint content begins ---\n").encode("utf-8")
    footer = b"\n--- checkpoint content ends ---\n"
    return frame + piece + footer


def _empty_opencode_pressure():
    return {"descriptor": None, "descriptor_sha256": None, "status": "unknown",
            "reason": "descriptor_missing", "last_observed_at": None,
            "last_anchor_tokens": None, "last_threshold_tokens": None,
            "last_usage_age_seconds": None, "warning_correlation_id": None,
            "warning_output_emitted": False, "generation_started_at_ms": None}


def _timestamp_seconds(value, label):
    if not isinstance(value, str):
        raise ValueError(label + " must be an ISO timestamp with a timezone")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(label + " must be an ISO timestamp with a timezone") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(label + " must include a timezone")
    return parsed.timestamp()


def _pressure_token_count(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        raise ValueError(label + " must be a positive token count" if positive else
                         label + " must be a nonnegative token count")
    return value


def _validate_opencode_pressure_descriptor(value, root, session, require_fresh=False, now=None):
    required = {"version", "native_api", "session_id", "worktree", "provider_id", "model_id",
                "variant", "agent", "observed_at", "expires_at", "usable_window_tokens",
                "compaction", "warning_lead_tokens"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("pressure descriptor fields do not match the reviewed schema")
    if isinstance(value.get("version"), bool) or value.get("version") != 1:
        raise ValueError("unsupported pressure descriptor version")
    if value.get("session_id") != session or not OPENCODE_SESSION_ID.fullmatch(str(value.get("session_id", ""))):
        raise ValueError("pressure descriptor does not match the exact OpenCode session")
    if value.get("worktree") != str(root):
        raise ValueError("pressure descriptor does not match the canonical worktree")
    for key in ("provider_id", "model_id", "variant", "agent"):
        item = value.get(key)
        if (not isinstance(item, str) or not item or item.strip() != item or len(item) > 256
                or any(ord(character) < 32 for character in item)):
            raise ValueError("pressure descriptor " + key + " is invalid")
    api = value.get("native_api")
    if not isinstance(api, dict) or set(api) != {"origin", "pid", "started_at"}:
        raise ValueError("pressure descriptor native API identity is incomplete")
    origin = api.get("origin")
    if not isinstance(origin, str) or len(origin) > 512:
        raise ValueError("pressure descriptor native API origin is invalid")
    try:
        parsed_origin = urlsplit(origin)
        valid_origin = (parsed_origin.scheme in ("http", "https") and bool(parsed_origin.hostname)
                        and parsed_origin.username is None and parsed_origin.password is None
                        and parsed_origin.path in ("", "/") and not parsed_origin.query
                        and not parsed_origin.fragment)
        _ = parsed_origin.port
    except ValueError:
        valid_origin = False
    if not valid_origin:
        raise ValueError("pressure descriptor native API origin must be a credential-free HTTP origin")
    pid = api.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise ValueError("pressure descriptor native API process identity is invalid")
    started_at = _timestamp_seconds(api.get("started_at"), "native API start time")
    observed_at = _timestamp_seconds(value.get("observed_at"), "descriptor observation time")
    expires_at = _timestamp_seconds(value.get("expires_at"), "descriptor expiry time")
    now = time.time() if now is None else now
    if expires_at <= observed_at or observed_at > now + 60 or started_at > observed_at:
        raise ValueError("pressure descriptor timestamps are inconsistent")
    if (expires_at - observed_at > OPENCODE_PRESSURE_DESCRIPTOR_MAX_AGE_SECONDS
            or require_fresh and (expires_at <= now or now - observed_at > OPENCODE_PRESSURE_DESCRIPTOR_MAX_AGE_SECONDS)):
        raise ValueError("pressure descriptor is expired")
    usable_window = _pressure_token_count(value.get("usable_window_tokens"), "usable context window", True)
    compaction = value.get("compaction")
    if not isinstance(compaction, dict) or set(compaction) != {
            "auto", "buffer_setting", "buffer_tokens", "ceiling_tokens", "config_sha256"}:
        raise ValueError("pressure descriptor compaction settings are incomplete")
    if type(compaction.get("auto")) is not bool:
        raise ValueError("pressure descriptor automatic compaction setting is invalid")
    buffer_setting = compaction.get("buffer_setting")
    if (buffer_setting is not None and
            (isinstance(buffer_setting, bool) or not isinstance(buffer_setting, (str, int, float))
             or isinstance(buffer_setting, float) and not math.isfinite(buffer_setting))):
        raise ValueError("pressure descriptor merged buffer setting is invalid")
    buffer_tokens = _pressure_token_count(compaction.get("buffer_tokens"), "effective buffer", False)
    ceiling = _pressure_token_count(compaction.get("ceiling_tokens"), "automatic compaction ceiling", True)
    if ceiling >= usable_window or usable_window - buffer_tokens != ceiling:
        raise ValueError("pressure descriptor window, effective buffer and compaction ceiling are inconsistent")
    config_sha = compaction.get("config_sha256")
    if not isinstance(config_sha, str) or re.fullmatch(r"[0-9a-f]{64}", config_sha) is None:
        raise ValueError("pressure descriptor merged compaction settings digest is invalid")
    lead = _pressure_token_count(value.get("warning_lead_tokens"), "warning lead", True)
    if lead >= ceiling:
        raise ValueError("warning lead must leave a positive lower-bound warning threshold")
    return {
        "version": 1,
        "native_api": {"origin": origin, "pid": pid, "started_at": api["started_at"]},
        "session_id": session,
        "worktree": str(root),
        "provider_id": value["provider_id"],
        "model_id": value["model_id"],
        "variant": value["variant"],
        "agent": value["agent"],
        "observed_at": value["observed_at"],
        "expires_at": value["expires_at"],
        "usable_window_tokens": usable_window,
        "compaction": {"auto": compaction["auto"], "buffer_setting": buffer_setting,
                       "buffer_tokens": buffer_tokens, "ceiling_tokens": ceiling,
                       "config_sha256": config_sha},
        "warning_lead_tokens": lead,
    }


def _read_opencode_pressure_descriptor(path, root, session):
    candidate = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (candidate, *candidate.parents)):
        raise ValueError("pressure descriptor path must not contain symlinks")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError("pressure descriptor file is unavailable: " + type(error).__name__) from None
    if resolved != candidate or not resolved.is_file():
        raise ValueError("pressure descriptor must be a canonical regular file")
    temp_roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp"), Path("/private/tmp")}
    if any(resolved == temporary or temporary in resolved.parents for temporary in temp_roots):
        raise ValueError("pressure descriptor must be durable and outside temporary directories")
    if resolved == root or root in resolved.parents:
        raise ValueError("pressure descriptor must be stored outside the worktree")
    for parent in resolved.parents:
        if (parent / ".git").exists():
            raise ValueError("pressure descriptor must be stored outside Git checkouts")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(resolved, flags)
    except OSError as error:
        raise ValueError("pressure descriptor could not be opened safely: " + type(error).__name__) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("pressure descriptor must be a private owner-only regular file")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ValueError("pressure descriptor must be owned by the current user")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(OPENCODE_PRESSURE_DESCRIPTOR_LIMIT + 1)
        if len(raw) > OPENCODE_PRESSURE_DESCRIPTOR_LIMIT:
            raise ValueError("pressure descriptor exceeds its bounded size")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ValueError("pressure descriptor is not valid UTF-8 JSON") from None
        descriptor = _validate_opencode_pressure_descriptor(value, root, session, require_fresh=True)
        return descriptor, _digest(raw)
    finally:
        os.close(fd)


def _pressure_unknown(state, reason, now=None):
    pressure = state["opencode_pressure"]
    pressure.update(status="unknown", reason=reason,
                    last_observed_at=_now() if now is None else now,
                    last_anchor_tokens=None, last_threshold_tokens=None,
                    last_usage_age_seconds=None)


def configure_opencode_pressure(ci, root, gitdir, session, descriptor_path):
    """Import a private, evidence-backed descriptor for one enrolled native session."""
    if not _opencode_checkpoint_owner(ci, root, gitdir, session):
        raise ValueError("pressure configuration requires this exact session's orchestrator assignment")
    descriptor, descriptor_sha = _read_opencode_pressure_descriptor(descriptor_path, root, session)
    state_path = _state_path(gitdir, "opencode", session)
    if not state_path.exists():
        raise ValueError("pressure configuration requires an initialized exact-session checkpoint")
    with ci.locked(_lock_path(state_path)):
        _, state = _load_state(ci, root, gitdir, "opencode", session)
        if not state.get("enabled"):
            raise ValueError("pressure configuration requires an enabled exact-session checkpoint")
        pressure = state["opencode_pressure"]
        pressure.update(descriptor=descriptor, descriptor_sha256=descriptor_sha,
                        status="configured" if descriptor["compaction"]["auto"] else "unknown",
                        reason=None if descriptor["compaction"]["auto"] else "auto_compaction_disabled",
                        last_observed_at=None, last_anchor_tokens=None,
                        last_threshold_tokens=None, last_usage_age_seconds=None,
                        warning_correlation_id=None, warning_output_emitted=False)
        _record(ci, root, state, "pressure.configured",
                "configured" if descriptor["compaction"]["auto"] else "disabled",
                descriptor_sha,
                {"descriptor_sha256": descriptor_sha,
                 "ceiling_tokens": descriptor["compaction"]["ceiling_tokens"],
                 "warning_lead_tokens": descriptor["warning_lead_tokens"],
                 "pressure_basis": "last_measured_anchor"})
        _write_json(state_path, state, ci)
    return {"status": pressure["status"], "reason": pressure["reason"],
            "harness": "opencode", "session_id": session, "worktree": str(root),
            "descriptor_sha256": descriptor_sha,
            "ceiling_tokens": descriptor["compaction"]["ceiling_tokens"],
            "warning_lead_tokens": descriptor["warning_lead_tokens"]}


def opencode_pressure_status(ci, root, gitdir, session):
    """Return a data-only view of pressure readiness for the plugin adapter."""
    result = {"status": "unknown", "reason": "checkpoint_disabled",
              "observation_required": False}
    if not _opencode_checkpoint_owner(ci, root, gitdir, session):
        return result
    state_path = _state_path(gitdir, "opencode", session)
    if not state_path.exists():
        return result
    try:
        with ci.locked(_lock_path(state_path)):
            _, state = _load_state(ci, root, gitdir, "opencode", session)
            pressure = state["opencode_pressure"]
            descriptor = pressure.get("descriptor")
            if not state.get("enabled"):
                result["reason"] = "checkpoint_disabled"
            elif descriptor is None:
                result["reason"] = "descriptor_missing"
            else:
                try:
                    descriptor = _validate_opencode_pressure_descriptor(
                        descriptor, root, session, require_fresh=True)
                except ValueError:
                    pressure.update(status="unknown", reason="descriptor_expired")
                    _write_json(state_path, state, ci)
                    result["reason"] = "descriptor_expired"
                else:
                    if not descriptor["compaction"]["auto"]:
                        pressure.update(status="unknown", reason="auto_compaction_disabled")
                        _write_json(state_path, state, ci)
                        result["reason"] = "auto_compaction_disabled"
                    else:
                        result.update(status=pressure["status"], reason=pressure["reason"],
                                      observation_required=True)
            result["descriptor_sha256"] = pressure.get("descriptor_sha256")
            result["ceiling_tokens"] = ((descriptor or {}).get("compaction", {}).get("ceiling_tokens")
                                        if descriptor is not None else None)
            result["warning_lead_tokens"] = ((descriptor or {}).get("warning_lead_tokens")
                                             if descriptor is not None else None)
    except (OSError, ValueError, UnicodeError, TypeError, KeyError):
        result["reason"] = "context_read_failed"
    return result


def _pressure_usage_identity(value, label):
    if not isinstance(value, dict) or set(value) != {
            "session_id", "worktree", "provider_id", "model_id", "variant", "agent"}:
        raise ValueError(label + " identity is incomplete")
    for key in ("session_id", "worktree", "provider_id", "model_id", "variant", "agent"):
        item = value.get(key)
        if not isinstance(item, str) or not item or item.strip() != item:
            raise ValueError(label + " identity is incomplete")
    return value


def _pressure_observation_age(completed_at_ms, now):
    if isinstance(completed_at_ms, bool) or not isinstance(completed_at_ms, (int, float)):
        raise ValueError("assistant completion time is missing")
    if not math.isfinite(completed_at_ms):
        raise ValueError("assistant completion time is invalid")
    completed = completed_at_ms / 1000
    age = now - completed
    if age < -5 or age > OPENCODE_USAGE_MAX_AGE_SECONDS:
        raise ValueError("assistant usage is stale")
    return max(0.0, age)


def opencode_pressure_observe(ci, root, gitdir, session, payload):
    """Evaluate only the latest native assistant's exact reported token counts."""
    if not _opencode_checkpoint_owner(ci, root, gitdir, session):
        return {"status": "unknown", "reason": "checkpoint_disabled", "warning": None}
    state_path = _state_path(gitdir, "opencode", session)
    if not state_path.exists():
        return {"status": "unknown", "reason": "checkpoint_disabled", "warning": None}
    if not isinstance(payload, dict) or set(payload) not in (
            {"request", "assistant"}, {"read_error"}):
        raise ValueError("pressure observation payload fields are invalid")
    with ci.locked(_lock_path(state_path)):
        _, state = _load_state(ci, root, gitdir, "opencode", session)
        pressure = state["opencode_pressure"]
        if not state.get("enabled"):
            _pressure_unknown(state, "checkpoint_disabled")
            _write_json(state_path, state, ci)
            return {"status": "unknown", "reason": "checkpoint_disabled", "warning": None}
        descriptor = pressure.get("descriptor")
        if descriptor is None:
            _pressure_unknown(state, "descriptor_missing")
            _write_json(state_path, state, ci)
            return {"status": "unknown", "reason": "descriptor_missing", "warning": None}
        try:
            descriptor = _validate_opencode_pressure_descriptor(
                descriptor, root, session, require_fresh=True)
        except ValueError:
            _pressure_unknown(state, "descriptor_expired")
            _write_json(state_path, state, ci)
            return {"status": "unknown", "reason": "descriptor_expired", "warning": None}
        if not descriptor["compaction"]["auto"]:
            _pressure_unknown(state, "auto_compaction_disabled")
            _write_json(state_path, state, ci)
            return {"status": "unknown", "reason": "auto_compaction_disabled", "warning": None}
        if set(payload) == {"read_error"}:
            reason = payload["read_error"]
            if reason not in ("context_read_failed", "assistant_missing"):
                reason = "context_read_failed"
            _pressure_unknown(state, reason)
            _record(ci, root, state, "pressure.observed", "unknown", None,
                    {"reason": reason, "descriptor_sha256": pressure["descriptor_sha256"]})
            _write_json(state_path, state, ci)
            return {"status": "unknown", "reason": reason, "warning": None}
        now = time.time()
        try:
            request = _pressure_usage_identity(payload["request"], "current request")
            assistant = payload["assistant"]
            if not isinstance(assistant, dict) or set(assistant) != {"identity", "completed_at_ms", "tokens"}:
                raise ValueError("assistant usage record is incomplete")
            assistant_identity = _pressure_usage_identity(assistant["identity"], "assistant")
            expected = {"session_id": session, "worktree": str(root),
                        "provider_id": descriptor["provider_id"], "model_id": descriptor["model_id"],
                        "variant": descriptor["variant"], "agent": descriptor["agent"]}
            if request != expected or assistant_identity != expected:
                raise ValueError("request or assistant identity does not match the reviewed descriptor")
            usage_age = _pressure_observation_age(assistant["completed_at_ms"], now)
            generation_boundary = pressure.get("generation_started_at_ms")
            if (generation_boundary is not None and
                    assistant["completed_at_ms"] < generation_boundary):
                raise ValueError("assistant usage predates the successful compaction generation boundary")
            tokens = assistant["tokens"]
            token_fields = {"input", "output", "reasoning", "cache_read", "cache_write"}
            if not isinstance(tokens, dict) or set(tokens) != token_fields:
                raise ValueError("assistant usage fields are incomplete")
            counts = {key: _pressure_token_count(tokens[key], "assistant " + key + " usage")
                      for key in token_fields}
            anchor = sum(counts.values())
            if anchor <= 0:
                raise ValueError("assistant usage is not positive")
        except ValueError as error:
            message = str(error)
            if "generation boundary" in message:
                reason = "new_generation"
            elif "stale" in message or "completion time" in message:
                reason = "usage_stale"
            elif "usage" in message or "token" in message:
                reason = "usage_invalid"
            else:
                reason = "identity_mismatch"
            _pressure_unknown(state, reason)
            _record(ci, root, state, "pressure.observed", "unknown", None,
                    {"reason": reason, "descriptor_sha256": pressure["descriptor_sha256"]})
            _write_json(state_path, state, ci)
            return {"status": "unknown", "reason": reason, "warning": None}
        threshold = descriptor["compaction"]["ceiling_tokens"] - descriptor["warning_lead_tokens"]
        pressure.update(last_observed_at=_now(), last_anchor_tokens=anchor,
                        last_threshold_tokens=threshold, last_usage_age_seconds=usage_age,
                        reason=None)
        details = {"usage_anchor_tokens": anchor, "warning_threshold_tokens": threshold,
                   "ceiling_tokens": descriptor["compaction"]["ceiling_tokens"],
                   "warning_lead_tokens": descriptor["warning_lead_tokens"],
                   "usage_age_seconds": usage_age, "pressure_basis": "last_measured_anchor",
                   "descriptor_sha256": pressure["descriptor_sha256"]}
        if anchor < threshold:
            pressure.update(status="below_threshold", warning_correlation_id=None,
                            warning_output_emitted=False)
            _record(ci, root, state, "pressure.observed", "below_threshold", None, details)
            _write_json(state_path, state, ci)
            return {"status": "below_threshold", "reason": None, "warning": None,
                    "usage_anchor_tokens": anchor, "threshold_tokens": threshold,
                    "ceiling_tokens": descriptor["compaction"]["ceiling_tokens"]}
        _record(ci, root, state, "pressure.observed", "threshold_reached", None, details)
        if state.get("reminded_generation") == state.get("observed_generation"):
            pressure.update(status="warning_deduplicated")
            _write_json(state_path, state, ci)
            return {"status": "warning_deduplicated", "reason": None, "warning": None,
                    "usage_anchor_tokens": anchor, "threshold_tokens": threshold,
                    "ceiling_tokens": descriptor["compaction"]["ceiling_tokens"]}
        generation = state.get("observed_generation", 0)
        correlation = _digest((session + "\0" + str(generation) + "\0" +
                               pressure["descriptor_sha256"]).encode("utf-8"))
        state["reminded_generation"] = generation
        pressure.update(status="warning_eligible", warning_correlation_id=correlation,
                        warning_output_emitted=False)
        _record(ci, root, state, "reminder.eligible", "eligible", correlation, details)
        _write_json(state_path, state, ci)
        warning = (
            "Pattern Buffer early checkpoint warning. The last measured lower-bound usage anchor is "
            f"{anchor} tokens, at or above the configured warning threshold of {threshold} tokens; "
            f"the observed automatic compaction ceiling is {descriptor['compaction']['ceiling_tokens']} tokens. "
            "This is a lower-bound last-measured reminder, not exact current occupancy or guaranteed headroom. "
            "A sudden large unseen context tail can still trigger automatic compaction without a warning. "
            "Review and prune the editable checkpoint draft now, then let a successful tool completion publish it."
        )
        return {"status": "warning_eligible", "reason": None, "warning": warning,
                "correlation_id": correlation, "usage_anchor_tokens": anchor,
                "threshold_tokens": threshold, "ceiling_tokens": descriptor["compaction"]["ceiling_tokens"]}


def opencode_pressure_emitted(ci, root, gitdir, session, correlation_id):
    if not _opencode_checkpoint_owner(ci, root, gitdir, session):
        return {"status": "checkpoint_disabled", "session_id": session, "worktree": str(root)}
    if not isinstance(correlation_id, str) or re.fullmatch(r"[0-9a-f]{64}", correlation_id) is None:
        raise ValueError("pressure warning correlation ID is invalid")
    state_path = _state_path(gitdir, "opencode", session)
    if not state_path.exists():
        return {"status": "checkpoint_disabled", "session_id": session, "worktree": str(root)}
    with ci.locked(_lock_path(state_path)):
        _, state = _load_state(ci, root, gitdir, "opencode", session)
        pressure = state["opencode_pressure"]
        if pressure.get("warning_correlation_id") != correlation_id:
            return {"status": "unknown_warning", "session_id": session, "worktree": str(root)}
        if pressure.get("warning_output_emitted"):
            result = "duplicate"
        else:
            pressure["status"] = "warning_emitted"
            pressure["warning_output_emitted"] = True
            _record(ci, root, state, "delivery.emitted", "emitted", correlation_id,
                    {"source": "opencode", "outcome": "emitted"})
            result = "emitted"
        _write_json(state_path, state, ci)
    return {"status": result, "session_id": session, "worktree": str(root)}


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
    state.setdefault("recovery_precompact_identity", None)
    if (state["recovery_precompact_identity"] is not None
            and (not isinstance(state["recovery_precompact_identity"], str)
                 or re.fullmatch(r"[0-9a-f]{64}", state["recovery_precompact_identity"]) is None)):
        raise ValueError("checkpoint recovery precompact identity is invalid")
    _validate_recovery_state(state)
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
    pressure_qualified = state.get("pressure_qualified", harness != "opencode")
    if not isinstance(pressure_qualified, bool):
        raise ValueError("checkpoint pressure qualification metadata is invalid")
    if harness == "opencode":
        if (pressure_qualified or state["compact_at"] is not None or state["lead_points"] is not None
                or state["reminders"]):
            raise ValueError("OpenCode pressure must remain unqualified until native usage evidence is reviewed")
        pressure = state.setdefault("opencode_pressure", _empty_opencode_pressure())
        pressure_fields = {"descriptor", "descriptor_sha256", "status", "reason", "last_observed_at",
                           "last_anchor_tokens", "last_threshold_tokens", "last_usage_age_seconds",
                           "warning_correlation_id", "warning_output_emitted",
                           "generation_started_at_ms"}
        if not isinstance(pressure, dict) or set(pressure) != pressure_fields:
            raise ValueError("OpenCode pressure metadata is invalid")
        if pressure.get("status") not in OPENCODE_PRESSURE_STATUSES or pressure.get("reason") not in OPENCODE_PRESSURE_REASONS:
            raise ValueError("OpenCode pressure state is invalid")
        if pressure.get("last_observed_at") is not None:
            _timestamp_seconds(pressure["last_observed_at"], "OpenCode pressure observation time")
        for key in ("last_anchor_tokens", "last_threshold_tokens"):
            item = pressure.get(key)
            if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 0):
                raise ValueError("OpenCode pressure token metadata is invalid")
        age = pressure.get("last_usage_age_seconds")
        if age is not None and (isinstance(age, bool) or not isinstance(age, (int, float))
                                or not math.isfinite(age) or age < 0):
            raise ValueError("OpenCode pressure usage age is invalid")
        correlation = pressure.get("warning_correlation_id")
        if correlation is not None and (not isinstance(correlation, str)
                                        or re.fullmatch(r"[0-9a-f]{64}", correlation) is None):
            raise ValueError("OpenCode warning correlation metadata is invalid")
        if type(pressure.get("warning_output_emitted")) is not bool:
            raise ValueError("OpenCode warning emission metadata is invalid")
        generation_boundary = pressure.get("generation_started_at_ms")
        if (generation_boundary is not None and
                (isinstance(generation_boundary, bool) or not isinstance(generation_boundary, int)
                 or generation_boundary < 0)):
            raise ValueError("OpenCode successful-generation boundary metadata is invalid")
        descriptor = pressure.get("descriptor")
        descriptor_sha = pressure.get("descriptor_sha256")
        if descriptor is None:
            if descriptor_sha is not None:
                raise ValueError("OpenCode pressure descriptor digest has no descriptor")
        else:
            _validate_opencode_pressure_descriptor(descriptor, root, session)
            if not isinstance(descriptor_sha, str) or re.fullmatch(r"[0-9a-f]{64}", descriptor_sha) is None:
                raise ValueError("OpenCode pressure descriptor digest is invalid")
        recent = state.get("recent_compaction_event_ids", [])
        if (not isinstance(recent, list) or len(recent) > 64 or
                any(not isinstance(item, dict) or set(item) != {"event_id", "event_type"}
                    or not isinstance(item.get("event_id"), str)
                    or not re.fullmatch(r"evt_[A-Za-z0-9]+", item["event_id"])
                    or item.get("event_type") not in OPENCODE_EVENT_TYPES for item in recent)):
            raise ValueError("OpenCode compaction event metadata is invalid")
        sequence = state.get("last_compaction_event_sequence")
        if sequence is not None and (isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0):
            raise ValueError("OpenCode compaction event sequence is invalid")
        for key in ("invalid_draft_sha256", "advised_draft_sha256"):
            value = state.get(key)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
                raise ValueError("OpenCode draft advisory metadata is invalid")
    else:
        if not pressure_qualified:
            raise ValueError("pressure qualification metadata is invalid for this harness")
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
    if not state.get("pressure_qualified", harness != "opencode"):
        return None, None, "unqualified; live pressure warnings disabled"
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
    pressure_qualified = state.get("pressure_qualified", harness != "opencode")
    elapsed = None
    if state.get("reviewed_at"):
        try:
            reviewed = datetime.datetime.fromisoformat(state["reviewed_at"].replace("Z", "+00:00"))
            elapsed = max(0, int((datetime.datetime.now(datetime.timezone.utc) - reviewed).total_seconds()))
        except (ValueError, TypeError):
            pass
    pressure_status = "qualified" if pressure_qualified else "unqualified; exact occupancy warnings disabled"
    pressure_basis = None
    pressure_reason = None
    if harness == "opencode":
        pressure = state.get("opencode_pressure", _empty_opencode_pressure())
        pressure_reason = pressure.get("reason")
        pressure_basis = "last_measured_anchor" if pressure.get("descriptor") is not None else None
        pressure_status = "unqualified; advisory " + pressure.get("status", "unknown")
        if pressure_reason:
            pressure_status += " (" + pressure_reason + ")"
    result = {"initialized": True, "checkpoint_path": state["checkpoint_path"],
              "identity": state.get("identity"), "enabled": state.get("enabled", False),
              "reminders": state.get("reminders", False),
              "pressure_qualified": pressure_qualified,
              "pressure_status": pressure_status,
              "pressure_basis": pressure_basis,
              "pressure_reason": pressure_reason,
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
    recovery = _validate_recovery_state(state)
    received = recovery.get("received")
    receipt = recovery.get("receipt")
    current_recovery_digest = state.get("note_sha256")
    current_recovery_parts = (_checkpoint_part_ranges(note.encode("utf-8"))
                              if note is not None and warning is None else [])
    recovery_read_command = None
    if recovery.get("required") and current_recovery_parts:
        if (received is None or received.get("note_sha256") != current_recovery_digest
                or received.get("published_at") != state.get("published_at")
                or received.get("part_count") != len(current_recovery_parts)):
            current_parts = []
        else:
            current_parts = received.get("parts", [])
        next_part = next((part for part in range(1, len(current_recovery_parts) + 1)
                          if part not in current_parts), 1)
        recovery_read_command = shlex.join([
            "pattern-buffer", "--cwd", str(root), "checkpoint-read", "--harness", harness,
            "--session", session, "--part", str(next_part)])
    result["recovery"] = {
        "required": recovery.get("required", False),
        "generation": recovery.get("generation", 0),
        "freshness": recovery.get("freshness", "unknown"),
        "freshness_reason": recovery.get("freshness_reason"),
        "boundary_generation": recovery.get("boundary_generation"),
        "received_parts": (list(received.get("parts", []))
                           if received and received.get("note_sha256") == current_recovery_digest
                           and received.get("published_at") == state.get("published_at") else []),
        "part_count": len(current_recovery_parts),
        "receipt": ({"generation": receipt["generation"],
                     "note_sha256": receipt["note_sha256"],
                     "published_at": receipt["published_at"],
                     "completed_at": receipt["completed_at"],
                     "part_count": receipt["part_count"],
                     "current_generation": receipt["generation"] == recovery.get("generation"),
                     "latest_checkpoint_matches_read": receipt["note_sha256"] == current_recovery_digest}
                    if receipt else None),
        "next_command": recovery_read_command,
    }
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
    if harness not in ("claude", "codex", "opencode") or not session:
        raise ValueError("checkpoint commands require a supported harness and session")
    if harness == "opencode" and not OPENCODE_SESSION_ID.fullmatch(session):
        raise ValueError("OpenCode checkpoints require the exact case-sensitive ses_ session ID")
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
            if harness == "opencode":
                if compact is not None or args.lead_points is not None:
                    raise ValueError("OpenCode pressure is unqualified; do not supply compact-at or lead values")
                source = "unqualified; verified provider usage, usable window, and active compaction settings are required"
            elif compact is None:
                if harness == "codex":
                    raise ValueError("Codex requires explicit --compact-at because its runtime threshold is unknown")
                compact, source = 60.0, "operator fallback 60; runtime override is honored"
            lead = args.lead_points if args.lead_points is not None else 5.0
            if harness != "opencode":
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
                     "compact_at": float(compact) if compact is not None else None,
                     "lead_points": float(lead) if harness != "opencode" else None,
                     "threshold_source": source, "pressure_qualified": harness != "opencode",
                     "reminders": harness != "opencode", "enabled": True,
                     "reviewed_at": None, "saved_revision": None, "note_sha256": None,
                     "saved_generation": None, "external": bool(args.path),
                     "observed_generation": 0, "generation_uncertain": True,
                     "generation_scope": ("observed completion events; event coverage uncertain"
                                          if harness == "opencode" else "since enrollment"),
                     "recovery_precompact_identity": None,
                     "uncertainty": ("native compaction completion events may be missed while the plugin is inactive"
                                     if harness == "opencode" else "no transcript provenance observed yet"),
                     "transcript": None,
                     "reminded_generation": None, "usage": None,
                     "draft_path": str(draft), "draft_signature": draft_signature,
                     "published_at": None, "recovery": _empty_recovery_state()}
            if harness == "opencode":
                state["recent_compaction_event_ids"] = []
                state["last_compaction_event_sequence"] = None
                state["invalid_draft_sha256"] = None
                state["advised_draft_sha256"] = None
                state["opencode_pressure"] = _empty_opencode_pressure()
            state["idle_warning_minutes"] = 50 if harness == "claude" else 0
            _arm_idle_timer(state, harness, session, "enrollment", ci)
            _write_json(state_path, state, ci)
            result = _report(ci, root, gitdir, harness, session, state)
            result["template"] = template
            return result
        _, state = _load_state(ci, root, gitdir, harness, session)
        if command == "checkpoint-config":
            if harness == "opencode" and (args.reminders is not None or args.compact_at is not None
                                           or args.lead_points is not None or args.idle_warning_minutes is not None):
                raise ValueError("OpenCode percentage pressure settings remain disabled; reviewed native evidence may enable lower-bound token reminders separately")
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
            if harness != "opencode":
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
            part = getattr(args, "part", None)
            if part is not None:
                note, warning = _read_note(state, ci)
                if note is None or warning:
                    raise ValueError(warning or "checkpoint note is unavailable")
                return _checkpoint_read_frame(state, note, part).decode("utf-8")
            return _report(ci, root, gitdir, harness, session, state, True)
    raise ValueError("unknown checkpoint command")


def _opencode_checkpoint_owner(ci, root, gitdir, session):
    if not isinstance(session, str) or not OPENCODE_SESSION_ID.fullmatch(session):
        raise ValueError("OpenCode checkpoints require the exact case-sensitive ses_ session ID")
    config_path = gitdir / "context-injector" / "config.json"
    if not config_path.exists():
        return False
    config, _ = ci.load(root, gitdir)
    if not ci.enabled(config):
        return False
    role, _, binding = ci.resolve_role(root, gitdir, config, "opencode", session)
    return role == "orchestrator" and not binding.get("task_id")


def opencode_recovery_context(ci, root, gitdir, session):
    """Return exact-session recovery guidance from the shared checkpoint state."""
    if not _opencode_checkpoint_owner(ci, root, gitdir, session):
        return ""
    state_path = _state_path(gitdir, "opencode", session)
    if not state_path.exists():
        return ""
    with ci.locked(_lock_path(state_path)):
        try:
            _, state = _load_state(ci, root, gitdir, "opencode", session)
        except (OSError, ValueError, UnicodeError, TypeError, KeyError):
            return ("Pattern Buffer checkpoint recovery for this exact OpenCode session.\n"
                    "WARNING: Checkpoint metadata failed integrity checks; no checkpoint contents were injected. "
                    "Inspect checkpoint-status for this exact session and repair its reviewed publication before relying on recovery.\n"
                    "This advisory does not change standing context, task authority, or human holds.")
        if not state.get("enabled"):
            return ""
        note, warning = _read_note(state, ci)
        draft = Path(state["draft_path"])
        draft_notice = None
        if not draft.exists():
            draft_notice = "The editable draft is missing; recreate it from the reviewed note before updating."
        else:
            draft_text, signature, draft_error = _draft_snapshot(draft)
            if not draft_error and signature != state["draft_signature"]:
                try:
                    _validate_note(draft_text)
                except ValueError as error:
                    draft_error = str(error)
            if draft_error:
                if (signature != state["draft_signature"] and
                        state.get("invalid_draft_sha256") != signature[2]):
                    state["invalid_draft_sha256"] = signature[2]
                    _write_json(state_path, state, ci)
            elif signature != state["draft_signature"]:
                draft_notice = "The editable draft has unpublished changes; reconcile it with the published note."
        lines = ["Pattern Buffer checkpoint recovery for this exact OpenCode session.",
                 "Read the reviewed current-state checkpoint before resuming: " + state["checkpoint_path"],
                 "Reconcile its claims with live evidence, preserve its human holds, and do not adopt another session's note.",
                 "Maintain the separate editable draft as a pruned current snapshot: " + state["draft_path"],
                 "A successful OpenCode tool-completion callback publishes a changed valid draft atomically.",
                 _generation_text(state),
                 "OpenCode pressure reminders require a fresh, exact-session descriptor and matching recent assistant usage. "
                 "Any reminder uses a last-measured lower-bound token anchor, not exact occupancy or guaranteed headroom; "
                 "unknown or stale evidence produces no warning."]
        if note is None:
            lines.append("WARNING: The reviewed checkpoint is unavailable or changed outside checkpoint-save; no note contents are injected. Inspect checkpoint-status for this exact session before relying on recovery.")
        if draft_notice:
            lines.append("WARNING: " + draft_notice)
        elif warning:
            lines.append("WARNING: " + warning)
        invalid_digest = state.get("invalid_draft_sha256")
        if invalid_digest and invalid_digest != state.get("advised_draft_sha256"):
            lines.append("WARNING: The latest changed draft was rejected or is invalid; the last good checkpoint remains published. Review and correct the draft before further substantial work.")
            state["advised_draft_sha256"] = invalid_digest
            _write_json(state_path, state, ci)
        lines.append("This checkpoint records current state. It does not grant authority or prove model reception.")
        return "\n".join(lines)


def opencode_event(ci, root, gitdir, session, event_type, event_id=None, aggregate_id=None,
                   event_sequence=None):
    """Apply native OpenCode lifecycle observations through shared checkpoint publication semantics."""
    if event_type not in ("tool-completed", *OPENCODE_EVENT_TYPES):
        raise ValueError("unsupported OpenCode checkpoint lifecycle event")
    if not _opencode_checkpoint_owner(ci, root, gitdir, session):
        return {"status": "not_checkpoint_owner", "harness": "opencode", "session_id": session,
                "worktree": str(root)}
    state_path = _state_path(gitdir, "opencode", session)
    if not state_path.exists():
        return {"status": "not_enrolled", "harness": "opencode", "session_id": session,
                "worktree": str(root)}
    with ci.locked(_lock_path(state_path)):
        _, state = _load_state(ci, root, gitdir, "opencode", session)
        if not state.get("enabled"):
            result = "disabled"
        elif event_type == "tool-completed":
            draft = _verify_draft(ci, root, gitdir, "opencode", session, state, missing_ok=True)
            if not draft.exists():
                result = "draft_missing"
            else:
                draft_text, signature, draft_error = _draft_snapshot(draft)
                if signature == state["draft_signature"]:
                    result = "unchanged"
                else:
                    digest = signature[2]
                    state["draft_signature"] = signature
                    _record(ci, root, state, "note.draft_changed", "changed", digest,
                            {"bytes": len(draft_text.encode()) if draft_text else 0, "source": "opencode"})
                    if draft_error:
                        state["invalid_draft_sha256"] = digest
                        _record(ci, root, state, "note.rejected", "invalid", digest, {"source": "opencode"})
                        _write_json(state_path, state, ci)
                        result = "rejected"
                    else:
                        try:
                            note = _publish(ci, root, state, draft_text, _signature_time(signature))
                        except ValueError:
                            state["invalid_draft_sha256"] = digest
                            _record(ci, root, state, "note.rejected", "invalid", digest, {"source": "opencode"})
                            _write_json(state_path, state, ci)
                            result = "rejected"
                        else:
                            state["invalid_draft_sha256"] = None
                            _write_json(state_path, state, ci)
                            result = "published"
        else:
            if (not isinstance(event_id, str) or not re.fullmatch(r"evt_[A-Za-z0-9]+", event_id)
                    or aggregate_id != session or isinstance(event_sequence, bool)
                    or not isinstance(event_sequence, int) or event_sequence < 0):
                return {"status": "unknown_identity", "harness": "opencode", "session_id": session,
                        "worktree": str(root)}
            recent = state.get("recent_compaction_event_ids", [])
            last_sequence = state.get("last_compaction_event_sequence")
            if (any(item["event_id"] == event_id for item in recent) or
                    last_sequence is not None and event_sequence <= last_sequence):
                result = "duplicate"
            else:
                recent.append({"event_id": event_id, "event_type": event_type})
                state["recent_compaction_event_ids"] = recent[-64:]
                state["last_compaction_event_sequence"] = event_sequence
                if event_type == "compaction-ended":
                    state["observed_generation"] += 1
                    state["generation_uncertain"] = True
                    state["uncertainty"] = "OpenCode event subscription has no replay guarantee; completion coverage may be incomplete"
                    pressure = state["opencode_pressure"]
                    pressure.update(status="awaiting_usage", reason="new_generation",
                                    last_observed_at=None, last_anchor_tokens=None,
                                    last_threshold_tokens=None, last_usage_age_seconds=None,
                                    warning_correlation_id=None, warning_output_emitted=False,
                                    generation_started_at_ms=int(time.time() * 1000))
                    _record(ci, root, state, "compaction.observed", "completed", event_id,
                            {"generation": state["observed_generation"], "generation_uncertain": True,
                             "trigger": "opencode"})
                    result = "completed"
                elif event_type == "compaction-failed":
                    _record(ci, root, state, "compaction.failed", "failed", event_id, {"trigger": "opencode"})
                    result = "failed"
                else:
                    _record(ci, root, state, "compaction.attempted", "started", event_id,
                            {"trigger": "opencode"})
                    result = "attempted"
                _write_json(state_path, state, ci)
    return {"status": result, "harness": "opencode", "session_id": session,
            "worktree": str(root), "pressure_qualified": False,
            "observed_generation": state.get("observed_generation", 0)}


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


def _precompact_capture_identity(state):
    capture = state.get("precompact")
    if not isinstance(capture, dict):
        return None
    identity = capture.get("identity")
    if not isinstance(identity, dict) or set(identity) != {"path", "dev", "ino"}:
        return None
    if (not isinstance(identity.get("path"), str)
            or any(isinstance(identity.get(key), bool) or not isinstance(identity.get(key), int)
                   for key in ("dev", "ino"))):
        return None
    size, mtime_ns = capture.get("size"), capture.get("mtime_ns")
    if (isinstance(size, bool) or not isinstance(size, int) or size < 0
            or isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int)
            or not isinstance(capture.get("captured_at"), str)
            or not isinstance(capture.get("tail_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", capture["tail_sha256"]) is None):
        return None
    value = {"identity": identity, "size": size, "mtime_ns": mtime_ns,
             "captured_at": capture["captured_at"], "tail_sha256": capture["tail_sha256"]}
    return _digest(json.dumps(value, sort_keys=True).encode())


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
            pending_boundary_before = bool(state.get("pending_compact_boundary"))
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
            generation_delta = state["observed_generation"] - generation_before
            generation_changed = generation_delta > 0
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
                recovery_provenance = ({key: identity.get(key) for key in ("path", "dev", "ino")}
                                       if transcript is not None and isinstance(identity, dict) else None)
                recovery = _validate_recovery_state(state)
                precompact_identity = _precompact_capture_identity(state)
                new_precompact_capture = (precompact_identity is not None
                                          and precompact_identity != state.get("recovery_precompact_identity"))
                pending_boundary_confirmed = (
                    pending_boundary_before and generation_delta == 1 and not new_precompact_capture
                    and not state.get("generation_uncertain")
                    and recovery.get("freshness_reason") ==
                    "the latest compaction boundary is pending or may be a duplicate recovery event")
                if pending_boundary_confirmed:
                    recovery_correlation = recovery.get("event_correlation")
                elif generation_changed:
                    recovery_correlation = _digest(json.dumps(
                        [session, recovery_provenance, state.get("observed_generation")],
                        sort_keys=True).encode())
                elif new_precompact_capture:
                    recovery_correlation = _digest(json.dumps(
                        [session, "precompact", precompact_identity], sort_keys=True).encode())
                elif precompact_identity is not None and recovery.get("event_correlation"):
                    recovery_correlation = recovery["event_correlation"]
                else:
                    recovery_correlation = _digest(json.dumps(
                        [session, recovery_provenance, state.get("observed_generation")],
                        sort_keys=True).encode())
                same_event = recovery.get("event_correlation") == recovery_correlation
                conservative_unknown_retry = (
                    same_event and bool(state.get("generation_uncertain"))
                    and not generation_changed and not pending_boundary_confirmed)
                uncertain = bool(state.get("generation_uncertain") or state.get("pending_compact_boundary"))
                _start_recovery_generation(ci, root, state, recovery_correlation, generation_changed,
                                           conservative_unknown_retry)
                if precompact_identity is not None:
                    state["recovery_precompact_identity"] = precompact_identity
                _write_json(state_path, state, ci)
                _record(ci, root, state, "compaction.unknown" if uncertain else "compaction.observed",
                        "uncertain" if uncertain else "observed", correlation,
                        {"generation": state.get("observed_generation"), "generation_uncertain": uncertain,
                         "reviewed_at": state.get("reviewed_at"), "trigger": "compact"})
                _record(ci, root, state, "recovery.observed", "hook_requested", correlation,
                        {"reviewed_at": state.get("reviewed_at"), "generation_uncertain": uncertain,
                         "recovery_generation": state.get("recovery", {}).get("generation")})
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
                recovery = _validate_recovery_state(state)
                if payload.get("source") == "compact" and recovery.get("required"):
                    details.append(_recovery_guidance(ci, root, harness, session, state, note, warning))
                else:
                    details.append("Read the reviewed checkpoint before resuming with your native file reader: " + state["checkpoint_path"])
                details.append("Reconcile current jobs, evidence and human holds. Edit the working draft with your normal file-writing tool: " + state["draft_path"])
                details.append("A following PostToolUse hook validates and atomically publishes valid draft edits. Do not touch timestamps to simulate review.")
                if payload.get("source") == "compact" and recovery.get("required"):
                    details.append("Only the framed checkpoint-read --part command above can satisfy this recovery read. Use checkpoint-status to inspect progress.")
                else:
                    details.append("Optional metadata/read command: " + command)
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
                recovery = _validate_recovery_state(state)
                if (recovery.get("required") and recovery.get("freshness_reason") ==
                        "the latest compaction boundary is pending or may be a duplicate recovery event"):
                    freshness, reason, boundary = _recovery_freshness(
                        state, state.get("observed_generation", 0) - 1)
                    recovery.update(freshness=freshness, freshness_reason=reason,
                                    boundary_generation=boundary)
                    _write_json(state_path, state, ci)
                    note, warning = _read_note(state, ci)
                    return (f"Checkpoint freshness update: observed compaction generation is now {state['observed_generation']}. "
                            "Checkpoint boundary is now observed. " +
                            _recovery_guidance(ci, root, harness, session, state, note, warning))
                return (f"Checkpoint freshness update: observed compaction generation is now {state['observed_generation']}. "
                        f"The last reviewed note was saved in observed generation {state.get('saved_generation')}. "
                        "The previously pending transcript boundary is now visible; reconcile the note with live state.")
    except (OSError, ValueError, TypeError, KeyError):
        raise
    return ""


def _recovery_freshness(state, boundary_generation=None):
    if state.get("generation_uncertain"):
        return "unknown", state.get("uncertainty") or "compaction history is incomplete", None
    if state.get("pending_compact_boundary"):
        return "unknown", "the latest compaction boundary is pending or may be a duplicate recovery event", None
    if boundary_generation is None:
        boundary_generation = state.get("observed_generation")
    saved = state.get("saved_generation")
    observed = state.get("observed_generation")
    if (isinstance(boundary_generation, bool) or not isinstance(boundary_generation, int)
            or boundary_generation < 0 or isinstance(observed, bool) or not isinstance(observed, int)
            or observed < 0 or isinstance(saved, bool)):
        return "unknown", "checkpoint publication or compaction history is incomplete", boundary_generation
    if boundary_generation == 0:
        if isinstance(saved, int) and saved == observed and observed > 0:
            return "current", None, boundary_generation
        return "unknown", "no previous confirmed compaction boundary is available for comparison", boundary_generation
    if saved is None:
        return "stale", "no successful checkpoint publication is recorded since the last confirmed compaction boundary", boundary_generation
    if not isinstance(saved, int):
        return "unknown", "checkpoint publication metadata is incomplete", boundary_generation
    if boundary_generation <= saved <= observed:
        return "current", None, boundary_generation
    if saved < boundary_generation:
        return "stale", "no successful checkpoint publication is recorded since the last confirmed compaction boundary", boundary_generation
    return "unknown", "checkpoint publication metadata is newer than the confirmed compaction history", boundary_generation


def _start_recovery_generation(ci, root, state, correlation, generation_changed,
                               conservative_unknown_retry=False):
    recovery = _validate_recovery_state(state)
    same_event = recovery.get("event_correlation") == correlation
    if same_event and not conservative_unknown_retry:
        return False
    boundary_generation = state.get("observed_generation", 0) - (1 if generation_changed else 0)
    freshness, reason, boundary_generation = _recovery_freshness(state, boundary_generation)
    recovery.update(generation=recovery["generation"] + 1,
                    event_correlation=correlation, required=True,
                    freshness=freshness, freshness_reason=reason,
                    boundary_generation=boundary_generation,
                    received=None, pending_reads=[])
    _record(ci, root, state, "recovery.read_required", "pending", correlation,
            {"recovery_generation": recovery["generation"],
             "observed_generation": state.get("observed_generation"),
             "freshness": freshness, "freshness_reason": reason})
    return True


def _recovery_freshness_text(recovery):
    boundary = recovery.get("boundary_generation")
    if recovery.get("freshness") == "stale":
        return (f"WARNING: No successful checkpoint publication is recorded since confirmed compaction generation {boundary}. "
                "The checkpoint may omit an entire work interval. Older decisions and human holds may remain authoritative "
                "even when current-state facts are stale. Reconcile current-state facts with the automatic summary, "
                "live checkout, tracker and job evidence; preserve decisions and holds unless their owner changes them.")
    if recovery.get("freshness") == "current":
        if boundary == 0:
            return ("Publication metadata records a save after the first confirmed compaction boundary. "
                    "Reconcile it normally with the current automatic summary, live checkout, tracker and job evidence.")
        return (f"Publication metadata records a checkpoint save since confirmed compaction generation {boundary}. "
                "Reconcile it normally with the current automatic summary, live checkout, tracker and job evidence.")
    reason = recovery.get("freshness_reason") or "compaction or publication history is incomplete"
    return (f"WARNING: Checkpoint freshness is unknown because {reason}. "
            "Do not assume it covers the last work interval. Reconcile current-state facts with the automatic summary "
            "and live evidence, while preserving approved scope, explicit decisions and human holds. "
            "Unknown authority or completion facts remain unresolved.")


def _recovery_guidance(ci, root, harness, session, state, note, note_warning):
    recovery = _validate_recovery_state(state)
    lines = [f"Pattern Buffer checkpoint recovery is required for exact session {session}, "
             f"recovery generation {recovery['generation']}.",
             _recovery_freshness_text(recovery)]
    lines.append("Publication metadata identifies the published bytes; it does not verify checkpoint claims or prove a substantive rewrite.")
    lines.append("Preserve standing context, task scope and human holds. A newer automatic summary does not override checkpoint decisions, and unknown authority or completion facts remain unresolved.")
    if note is None or note_warning:
        status_command = shlex.join(["pattern-buffer", "--cwd", str(root), "checkpoint-status",
                                     "--harness", harness, "--session", session])
        save_command = shlex.join(["pattern-buffer", "--cwd", str(root), "checkpoint-save",
                                   "--harness", harness, "--session", session,
                                   "--file", state["draft_path"]])
        lines.append("WARNING: The reviewed checkpoint is missing or unreadable, so the prerequisite remains unresolved.")
        lines.append("Status command for this exact session: " + status_command + ".")
        lines.append("You may inspect only this session's checkpoint, previous copy and owned draft; create or edit only the owned draft at " + state["draft_path"] + ".")
        lines.append("Reconstruct only from current live evidence, then let PostToolUse publish a valid draft or run this supported save command when the independent static read gate allows it: " + save_command + ".")
        lines.append("Read every framed part of the new publication before ordinary work resumes.")
        lines.append("Do not invent completed work, replace running jobs, or treat a command invocation as a completed recovery read.")
        return " ".join(lines)
    data = note.encode("utf-8")
    ranges = _checkpoint_part_ranges(data)
    received = recovery.get("received")
    parts = (received.get("parts", []) if received
             and received.get("note_sha256") == state.get("note_sha256")
             and received.get("published_at") == state.get("published_at")
             and received.get("part_count") == len(ranges) else [])
    next_part = next((part for part in range(1, len(ranges) + 1) if part not in parts), 1)
    command = shlex.join(["pattern-buffer", "--cwd", str(root), "checkpoint-read",
                          "--harness", harness, "--session", session,
                          "--part", str(next_part)])
    lines.append("Read every bounded part of the latest published checkpoint through its exact framed command. "
                 "Only complete matching PostToolUse output for all parts of one published version opens this gate.")
    lines.append("Next command: " + command + ".")
    lines.append("Use checkpoint-status to see the remaining parts. Preserve standing context, task scope and human holds.")
    return " ".join(lines)


def _parse_hook_cli(ci, root, harness, session, payload):
    if payload.get("tool_name") != "Bash":
        return None, None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return None, command
    required_reads = ci.auxiliary("required_reads")
    return required_reads._parse_allowed_cli_command(command, root, harness, session, ci), command


def _recovery_repair_tool_allowed(payload, state):
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    target = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(target, str) or not Path(target).is_absolute():
        return False
    if tool == "Read":
        return target in {state["checkpoint_path"], state["previous_path"], state["draft_path"]}
    if tool in ("Edit", "Write"):
        return target == state["draft_path"]
    return False


def recovery_pretool_gate(ci, root, gitdir, harness, payload):
    if harness not in ("claude", "codex") or payload.get("agent_id"):
        return None
    session = payload.get("session_id")
    if not isinstance(session, str) or not session or len(session) > 512:
        return None
    required_reads = ci.auxiliary("required_reads")
    parsed, command = _parse_hook_cli(ci, root, harness, session, payload)
    read_part = (parsed.part if parsed is not None and parsed.command == "checkpoint-read" else None)
    if parsed is not None:
        if parsed.command == "checkpoint-status":
            return None
        if parsed.command == "read-required" and required_reads.allowed_cli_command(
                command, root, harness, session, ci):
            return None
        if required_reads.allowed_diagnostic_cli_command(command, root, harness, session, ci):
            if parsed.command != "checkpoint-read" or read_part is None:
                return None
    config_path = gitdir / "context-injector" / "config.json"
    state_path = _state_path(gitdir, harness, session)
    if not config_path.exists() or not state_path.exists():
        return None
    try:
        config, _ = ci.load(root, gitdir)
        if not ci.enabled(config):
            return None
        role, _, binding = ci.resolve_role(root, gitdir, config, harness, session)
        if role != "orchestrator" or binding.get("task_id"):
            return None
        with ci.locked(_lock_path(state_path)):
            _, state = _load_state(ci, root, gitdir, harness, session)
            recovery = _validate_recovery_state(state)
            if parsed is not None and parsed.command == "checkpoint-save":
                note, warning = _read_note(state, ci)
                source = Path(parsed.file).expanduser()
                draft = Path(state["draft_path"])
                if (not recovery.get("required")
                        or (note is None or warning) and source.is_absolute()
                        and not source.is_symlink()
                        and source.resolve(strict=False) == draft.resolve(strict=False)):
                    return None
            if not state.get("enabled") or not recovery.get("required"):
                return None
            note, warning = _read_note(state, ci)
            if (note is None or warning) and _recovery_repair_tool_allowed(payload, state):
                return None
            if parsed is not None and parsed.command == "checkpoint-read" and read_part is not None:
                if note is not None and warning is None:
                    ranges = _checkpoint_part_ranges(note.encode("utf-8"))
                    tool_use_id = payload.get("tool_use_id")
                    if (1 <= read_part <= len(ranges) and isinstance(tool_use_id, str)
                            and tool_use_id and len(tool_use_id) <= 512
                            and not any(ord(character) < 32 or ord(character) == 127 for character in tool_use_id)):
                        pending = [item for item in recovery["pending_reads"]
                                   if item.get("tool_use_id") != tool_use_id]
                        pending.append({"tool_use_id": tool_use_id,
                                        "command_sha256": _digest(command.encode("utf-8")),
                                        "generation": recovery["generation"],
                                        "note_sha256": state["note_sha256"],
                                        "published_at": state["published_at"],
                                        "part": read_part, "part_count": len(ranges)})
                        recovery["pending_reads"] = pending[-RECOVERY_PENDING_READ_LIMIT:]
                        _write_json(state_path, state, ci)
                return None
            message = _recovery_guidance(ci, root, harness, session, state, *_read_note(state, ci))
            _record(ci, root, state, "recovery.gate_denied", "pending",
                    recovery.get("event_correlation"),
                    {"recovery_generation": recovery["generation"],
                     "freshness": recovery["freshness"]})
            return required_reads._hook_result(message, decision="deny")
    except (OSError, ValueError, TypeError, UnicodeError, KeyError) as error:
        message = ("Pattern Buffer could not verify checkpoint recovery state for this enrolled session. "
                   f"Reason: {type(error).__name__}. Run pattern-buffer guide --print and checkpoint-status, "
                   "preserve the current note and notify the owner. The recovery gate remains closed.")
        return required_reads._hook_result(message, decision="deny")


def recovery_posttool_finalize(ci, root, gitdir, harness, payload):
    if harness not in ("claude", "codex") or payload.get("agent_id"):
        return False
    session = payload.get("session_id")
    tool_use_id = payload.get("tool_use_id")
    if (not isinstance(session, str) or not session or len(session) > 512
            or not isinstance(tool_use_id, str) or not tool_use_id or len(tool_use_id) > 512):
        return False
    required_reads = ci.auxiliary("required_reads")
    parsed, command = _parse_hook_cli(ci, root, harness, session, payload)
    if parsed is None or parsed.command != "checkpoint-read" or parsed.part is None:
        return False
    state_path = _state_path(gitdir, harness, session)
    if not state_path.exists() and not state_path.is_symlink():
        return False
    stdout, normalize_footer = required_reads._response_stdout(harness, payload)
    command_digest = _digest(command.encode("utf-8"))
    with ci.locked(_lock_path(state_path)):
        _, state = _load_state(ci, root, gitdir, harness, session)
        recovery = _validate_recovery_state(state)
        pending = next((item for item in recovery["pending_reads"]
                        if item.get("tool_use_id") == tool_use_id), None)
        if pending is None:
            return False
        recovery["pending_reads"] = [item for item in recovery["pending_reads"]
                                     if item.get("tool_use_id") != tool_use_id]
        success = False
        note = None
        ranges = []
        if (pending.get("command_sha256") == command_digest
                and pending.get("generation") == recovery["generation"]
                and recovery.get("required") and parsed.part == pending.get("part")):
            note, warning = _read_note(state, ci)
            if note is not None and warning is None:
                data = note.encode("utf-8")
                ranges = _checkpoint_part_ranges(data)
                if (pending.get("note_sha256") == state.get("note_sha256")
                        and pending.get("published_at") == state.get("published_at")
                        and pending.get("part_count") == len(ranges)
                        and 1 <= pending.get("part", 0) <= len(ranges)):
                    frame = _checkpoint_read_frame(state, note, pending["part"])
                    success = (isinstance(stdout, bytes) and
                               (stdout == frame or normalize_footer and frame.endswith(b"\n")
                                and stdout == frame[:-1]))
        if success and note is not None:
            received = recovery.get("received")
            if (received is None or received.get("note_sha256") != state["note_sha256"]
                    or received.get("published_at") != state["published_at"]
                    or received.get("part_count") != len(ranges)):
                received = {"note_sha256": state["note_sha256"],
                            "published_at": state["published_at"],
                            "part_count": len(ranges), "parts": []}
            if pending["part"] not in received["parts"]:
                received["parts"].append(pending["part"])
                received["parts"].sort()
            recovery["received"] = received
            if len(received["parts"]) == received["part_count"]:
                recovery["required"] = False
                recovery["receipt"] = {"generation": recovery["generation"],
                                        "note_sha256": received["note_sha256"],
                                        "published_at": received["published_at"],
                                        "part_count": received["part_count"],
                                        "completed_at": _now()}
                _record(ci, root, state, "recovery.read_completed", "complete",
                        recovery.get("event_correlation"),
                        {"recovery_generation": recovery["generation"],
                         "note_sha256": received["note_sha256"],
                         "part_count": received["part_count"]})
            else:
                _record(ci, root, state, "recovery.read_part_received", "partial",
                        recovery.get("event_correlation"),
                        {"recovery_generation": recovery["generation"],
                         "note_sha256": received["note_sha256"],
                         "part": pending["part"], "part_count": received["part_count"]})
        else:
            _record(ci, root, state, "recovery.read_rejected", "incomplete",
                    recovery.get("event_correlation"),
                    {"recovery_generation": recovery["generation"],
                     "part": pending.get("part")})
        _write_json(state_path, state, ci)
    return success


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
