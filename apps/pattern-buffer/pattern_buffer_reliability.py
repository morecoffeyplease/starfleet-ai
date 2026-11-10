"""Bounded, private reliability events for Pattern Buffer.

This module records small lifecycle facts only. It never reads transcripts or
stores free-form event text. Callers can use :func:`emit` on recovery paths;
every failure is returned to the caller and does not raise from that API.
"""

from contextlib import contextmanager
from bisect import bisect_right
import datetime as _datetime
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid


DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
_FORMAT_VERSION = 1
_RUNTIME_VERSION = "pattern-buffer-reliability/1"
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_HEX_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")
_DATE_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_DETAIL_TYPES = {
    "role": "token", "source_sha256": "hash", "bytes": "integer",
    "over_budget_bytes": "integer", "failure_kind": "token",
    "usage_percent": "percent", "reminder_threshold": "percent", "generation": "integer",
    "generation_uncertain": "boolean", "reviewed_at": "timestamp", "published_at": "timestamp",
    "saved_generation": "integer", "trigger": "token", "outcome": "token",
    "error_code": "token", "path_hash": "hash", "timer_id": "token", "source": "token",
    "source_identity": "token", "source_hash": "hash", "byte_count": "integer",
    "runtime_version": "token", "runtime_fingerprint": "hash", "failure_code": "token",
    "phase": "token", "status": "token", "attempt": "integer", "threshold_percent": "percent",
    "age_seconds": "number", "duration_seconds": "number", "note_age_seconds": "number",
    "warning_age_seconds": "number", "publication_age_seconds": "number",
    "compaction_gap_seconds": "number",
    "usage_anchor_tokens": "integer", "warning_threshold_tokens": "integer",
    "ceiling_tokens": "integer", "warning_lead_tokens": "integer",
    "usage_age_seconds": "number", "pressure_basis": "token", "reason": "token",
    "descriptor_sha256": "hash",
}
_MAX_EVENT_BYTES = 16 * 1024
_LIFECYCLE_EVENTS = {
    "reminder.armed",
    "reminder.eligible",
    "pressure.configured",
    "pressure.observed",
    "delivery.emitted",
    "delivery.submitted",
    "delivery.held",
    "delivery.refused",
    "delivery.uncertain",
    "receipt.received",
    "note.draft_changed",
    "note.published",
    "note.rejected",
    "compaction.observed",
    "compaction.attempted",
    "compaction.failed",
    "compaction.unknown",
    "standing.injection_emitted",
    "standing.injection_failed",
    "context.missing_alarm",
    "context.recovered",
    "recovery.observed",
    "enrollment.session-enroll",
    "enrollment.assign",
    "enrollment.enrollment-release",
    "enrollment.enrollment-adopt",
    "enrollment-release",
    "enrollment-adopt",
}
_DURATION_PAIRS = {
    "emission_to_receipt": ("delivery.emitted", "receipt.received"),
    "alarm_to_recovery": ("context.missing_alarm", "context.recovered"),
}


class _ReliabilityError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _error(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


def _utc_now():
    return _datetime.datetime.now(_datetime.timezone.utc)


def _stamp(value=None):
    value = value or _utc_now()
    return value.astimezone(_datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(_datetime.timezone.utc)
    except (OverflowError, ValueError):
        return None


def _root_path(root):
    if not isinstance(root, (str, os.PathLike)):
        raise _ReliabilityError("invalid_root", "root must be a filesystem path")
    try:
        value = Path(root).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise _ReliabilityError("invalid_root", "root could not be resolved") from error
    if not value.is_absolute():
        raise _ReliabilityError("invalid_root", "root must resolve to an absolute path")
    return value


def _state_path(root):
    resolved = _root_path(root)
    identity = hashlib.sha256(os.fsencode(str(resolved))).hexdigest()
    configured = os.environ.get("XDG_STATE_HOME")
    try:
        if configured:
            base = Path(configured).expanduser()
            if not base.is_absolute():
                raise _ReliabilityError("invalid_state_home", "XDG_STATE_HOME must be an absolute path")
        else:
            base = Path.home() / ".local" / "state"
    except (OSError, RuntimeError, ValueError) as error:
        raise _ReliabilityError("invalid_state_home", "private state home could not be resolved") from error
    return base / "starfleet-ai" / "pattern-buffer" / "reliability" / identity, identity


def _check_directory_chain(path, create=False):
    """Create or inspect a directory path without following symlinks."""
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                return False
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as error:
                raise _ReliabilityError("state_unavailable", "private state directory could not be created") from error
            try:
                info = current.lstat()
            except OSError as error:
                raise _ReliabilityError("state_unavailable", "private state directory could not be inspected") from error
        except OSError as error:
            raise _ReliabilityError("state_unavailable", "private state directory could not be inspected") from error
        if stat.S_ISLNK(info.st_mode):
            raise _ReliabilityError("symlink_refused", "reliability state path contains a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise _ReliabilityError("unsafe_state_path", "reliability state path contains a non-directory")
        if current == path and hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise _ReliabilityError("unsafe_state_path", "reliability state directory has a different owner")
        if create and current == path:
            try:
                os.chmod(current, 0o700)
            except OSError as error:
                raise _ReliabilityError("state_unavailable", "private state directory permissions could not be set") from error
    return True


def _open_regular(path, flags, mode=0o600):
    path = Path(path)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, mode)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            raise _ReliabilityError("symlink_refused", "reliability file is a symlink") from error
        raise _ReliabilityError("state_unavailable", "reliability file could not be opened") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _ReliabilityError("unsafe_state_file", "reliability file is not a regular file")
        if info.st_nlink != 1 or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise _ReliabilityError("unsafe_state_file", "reliability file ownership is unsafe")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise _ReliabilityError("unsafe_state_file", "reliability file permissions are not private")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _locked(state_dir):
    lock_path = state_dir / ".lock"
    fd = _open_regular(lock_path, os.O_CREAT | os.O_RDWR)
    deadline = time.monotonic() + 2.0
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _ReliabilityError("lock_timeout", "another reliability operation holds the state lock")
                time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _atomic_json(path, value):
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temp = path.parent / (".pending-" + uuid.uuid4().hex)
    try:
        fd = _open_regular(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            _write_all(fd, data)
        finally:
            os.close(fd)
        if path.exists() or path.is_symlink():
            try:
                info = path.lstat()
            except OSError as error:
                raise _ReliabilityError("state_unavailable", "reliability metadata could not be inspected") from error
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise _ReliabilityError("symlink_refused", "reliability metadata is unsafe")
        os.replace(temp, path)
    except OSError as error:
        raise _ReliabilityError("state_unavailable", "reliability metadata could not be saved") from error
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _write_all(fd, data):
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise _ReliabilityError("write_failed", "reliability data could not be written")
        remaining = remaining[written:]


def _read_json(path, default):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return default
    except OSError as error:
        raise _ReliabilityError("state_unavailable", "reliability metadata could not be inspected") from error
    if stat.S_ISLNK(info.st_mode):
        raise _ReliabilityError("symlink_refused", "reliability metadata is a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise _ReliabilityError("unsafe_state_file", "reliability metadata is not a regular file")
    fd = _open_regular(path, os.O_RDONLY)
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            try:
                return json.load(handle)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise _ReliabilityError("invalid_state_metadata", "reliability metadata is malformed") from error
    except OSError as error:
        raise _ReliabilityError("state_unavailable", "reliability metadata could not be read") from error


def _config(state_dir):
    value = _read_json(state_dir / "config.json", None)
    if value is None:
        return {"retention_days": DEFAULT_RETENTION_DAYS, "max_bytes": DEFAULT_MAX_BYTES}
    if (not isinstance(value, dict) or value.get("version") != _FORMAT_VERSION
            or type(value.get("retention_days")) is not int or value["retention_days"] < 1
            or type(value.get("max_bytes")) is not int or value["max_bytes"] < 1):
        raise _ReliabilityError("invalid_state_metadata", "reliability configuration is malformed")
    return {"retention_days": value["retention_days"], "max_bytes": value["max_bytes"]}


def _daily_files(state_dir):
    files = []
    try:
        with os.scandir(state_dir) as iterator:
            entries = list(iterator)
    except OSError as error:
        raise _ReliabilityError("state_unavailable", "reliability history could not be listed") from error
    for entry in entries:
        match = _DATE_FILE.fullmatch(entry.name)
        if not match:
            continue
        try:
            day = _datetime.date.fromisoformat(match.group(1))
            info = entry.stat(follow_symlinks=False)
        except (OSError, ValueError) as error:
            raise _ReliabilityError("unsafe_history_file", "a reliability history file could not be inspected") from error
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
            raise _ReliabilityError("symlink_refused", "a reliability history file is not a regular file")
        files.append((day, Path(entry.path), info.st_size))
    return sorted(files, key=lambda item: (item[0], item[1].name))


def _usage_metadata(state_dir):
    value = _read_json(state_dir / "usage.json", None)
    if (isinstance(value, dict) and value.get("version") == _FORMAT_VERSION
            and isinstance(value.get("last_scan"), str)
            and type(value.get("used_bytes")) is int and value["used_bytes"] >= 0):
        return {"last_scan": value["last_scan"], "used_bytes": value["used_bytes"]}
    if value is not None:
        raise _ReliabilityError("invalid_state_metadata", "reliability usage metadata is malformed")
    return None


def _write_usage(state_dir, last_scan, used_bytes):
    _atomic_json(state_dir / "usage.json", {
        "version": _FORMAT_VERSION,
        "last_scan": last_scan,
        "used_bytes": max(0, int(used_bytes)),
    })


def _remove_log(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    except OSError as error:
        raise _ReliabilityError("state_unavailable", "an expired reliability file could not be inspected") from error
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1 or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
        raise _ReliabilityError("symlink_refused", "a reliability history file is not a regular file")
    try:
        path.unlink()
    except OSError as error:
        raise _ReliabilityError("state_unavailable", "an owned reliability file could not be pruned") from error
    return info.st_size


def _scan_and_prune(state_dir, config, today, needed_bytes=0):
    files = _daily_files(state_dir)
    cutoff = today - _datetime.timedelta(days=config["retention_days"] - 1)
    used = 0
    retained = []
    for day, path, size in files:
        if day < cutoff:
            _remove_log(path)
        else:
            used += size
            retained.append((day, path, size))
    if needed_bytes > config["max_bytes"]:
        _write_usage(state_dir, today.isoformat(), used)
        raise _ReliabilityError("capacity_exceeded", "one reliability event exceeds the configured size cap")
    for day, path, size in retained:
        if used + needed_bytes <= config["max_bytes"]:
            break
        used -= _remove_log(path)
    if used + needed_bytes > config["max_bytes"]:
        raise _ReliabilityError("capacity_exceeded", "one reliability event exceeds the configured size cap")
    _write_usage(state_dir, today.isoformat(), used)
    return used


def _maintenance(state_dir, config, today, incoming_bytes=0, force=False):
    metadata = _usage_metadata(state_dir)
    due = metadata is None or metadata["last_scan"] != today.isoformat()
    if force or due:
        used = _scan_and_prune(state_dir, config, today, incoming_bytes)
        return used
    used = metadata["used_bytes"]
    if used + incoming_bytes > config["max_bytes"]:
        used = _scan_and_prune(state_dir, config, today, incoming_bytes)
    return used


def _identifier(value, label, allow_none=True):
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not _SAFE_TOKEN.fullmatch(value):
        raise _ReliabilityError("invalid_" + label, label + " must be a short identifier")
    return value


def _detail_value(key, value):
    kind = _DETAIL_TYPES.get(key)
    if kind == "boolean":
        return value if isinstance(value, bool) else None
    if kind == "timestamp":
        parsed = _parse_timestamp(value)
        return _stamp(parsed) if parsed is not None else None
    if kind in ("number", "integer", "percent"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return None
        if not math.isfinite(number) or number < 0 or number > 10_000_000_000:
            return None
        if kind == "integer":
            if not number.is_integer() or number > 2**53:
                return None
            return int(number)
        if kind == "percent" and number > 100:
            return None
        return number
    if kind == "hash":
        return value.lower() if isinstance(value, str) and _HEX_DIGEST.fullmatch(value) else None
    if kind == "token":
        return value if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value) else None
    return None


def _safe_details(details):
    if details is None:
        return {}, 0
    if not isinstance(details, dict):
        raise _ReliabilityError("invalid_details", "details must be a mapping of safe metadata")
    safe = {}
    dropped = 0
    for key, value in details.items():
        if not isinstance(key, str) or key not in _DETAIL_TYPES:
            dropped += 1
            continue
        clean = _detail_value(key, value)
        if clean is None:
            dropped += 1
            continue
        safe[key] = clean
    return safe, dropped


def _runtime_fingerprint():
    cached = globals().get("_RUNTIME_FINGERPRINT_CACHE")
    if cached is not None:
        return cached
    try:
        digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        digest = hashlib.sha256(_RUNTIME_VERSION.encode("ascii")).hexdigest()
    globals()["_RUNTIME_FINGERPRINT_CACHE"] = digest
    return digest


def _append_event(state_dir, record, today):
    line = (json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")
    if len(line) > _MAX_EVENT_BYTES:
        raise _ReliabilityError("event_too_large", "reliability event exceeds the hard per-event limit")
    config = _config(state_dir)
    _maintenance(state_dir, config, today, len(line))
    path = state_dir / (today.isoformat() + ".jsonl")
    fd = _open_regular(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY)
    try:
        if os.fstat(fd).st_size + len(line) > config["max_bytes"]:
            _maintenance(state_dir, config, today, len(line), force=True)
            os.close(fd)
            fd = _open_regular(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY)
        usage = _usage_metadata(state_dir)
        if usage is None:
            used = _maintenance(state_dir, config, today, len(line), force=True)
        else:
            used = usage["used_bytes"]
        if used + len(line) > config["max_bytes"]:
            raise _ReliabilityError("capacity_exceeded", "reliability history is at its configured size cap")
        _write_usage(state_dir, usage["last_scan"] if usage else today.isoformat(), used + len(line))
        _write_all(fd, line)
    finally:
        os.close(fd)
    return len(line)


def emit(root, event, *, harness=None, session=None, enrollment_id=None,
         result=None, correlation_id=None, details=None):
    """Append one safe event, returning ``{ok: ...}`` even when logging fails.

    ``details`` accepts only allowlisted identifiers, hashes, counters and
    durations. Unknown or non-scalar detail values are dropped and counted in
    the successful result; no caller-supplied prose is written to disk.
    """
    try:
        if not isinstance(event, str) or not _EVENT_NAME.fullmatch(event):
            raise _ReliabilityError("invalid_event", "event must be a short lowercase event name")
        if harness not in (None, "claude", "codex", "opencode"):
            raise _ReliabilityError("invalid_harness", "harness must be claude, codex, or opencode")
        session = _identifier(session, "session")
        enrollment_id = _identifier(enrollment_id, "enrollment_id")
        correlation_id = _identifier(correlation_id, "correlation_id")
        result = _identifier(result, "result")
        safe_details, dropped = _safe_details(details)
        state_dir, worktree_id = _state_path(root)
        _check_directory_chain(state_dir, create=True)
        now = _utc_now()
        record = {
            "event_id": str(uuid.uuid4()),
            "timestamp": _stamp(now),
            "worktree_id": worktree_id,
            "runtime_version": _RUNTIME_VERSION,
            "runtime_fingerprint": _runtime_fingerprint(),
            "event": event,
        }
        for key, value in (("harness", harness), ("session", session),
                           ("enrollment_id", enrollment_id), ("result", result),
                           ("correlation_id", correlation_id)):
            if value is not None:
                record[key] = value
        if safe_details:
            record["details"] = safe_details
        with _locked(state_dir):
            _append_event(state_dir, record, now.date())
        result = {"ok": True, "event_id": record["event_id"], "timestamp": record["timestamp"],
                  "worktree_id": worktree_id}
        if dropped:
            result["dropped_detail_count"] = dropped
        return result
    except _ReliabilityError as error:
        return _error(error.code, error.message)
    except (OSError, ValueError, TypeError) as error:
        return _error("telemetry_error", "reliability event could not be recorded")


def configure(root, retention_days=None, max_bytes=None):
    """Update supplied per-worktree limits and enforce the effective settings.

    Unspecified limits retain their current values; a fresh state uses the
    module defaults.
    """
    try:
        if retention_days is not None and (type(retention_days) is not int
                                           or not 1 <= retention_days <= 36500):
            raise _ReliabilityError("invalid_retention", "retention_days must be between 1 and 36500")
        if max_bytes is not None and (type(max_bytes) is not int or not 1 <= max_bytes <= 2**63 - 1):
            raise _ReliabilityError("invalid_max_bytes", "max_bytes must be between 1 and 2^63-1")
        state_dir, worktree_id = _state_path(root)
        _check_directory_chain(state_dir, create=True)
        today = _utc_now().date()
        with _locked(state_dir):
            config = _config(state_dir)
            if retention_days is not None:
                config["retention_days"] = retention_days
            if max_bytes is not None:
                config["max_bytes"] = max_bytes
            _atomic_json(state_dir / "config.json", {"version": _FORMAT_VERSION, **config})
            used = _scan_and_prune(state_dir, config, today)
        return {"ok": True, "worktree_id": worktree_id, **config, "used_bytes": used}
    except _ReliabilityError as error:
        return _error(error.code, error.message)
    except (OSError, ValueError, TypeError) as error:
        return _error("telemetry_error", "reliability configuration could not be saved")


def _parse_since(since, now):
    if not isinstance(since, str) or not since:
        raise _ReliabilityError("invalid_since", "since must be a duration such as 7d or an ISO timestamp")
    match = re.fullmatch(r"([1-9][0-9]*)([dh])", since)
    if match:
        if len(match.group(1)) > 5:
            raise _ReliabilityError("invalid_since", "since duration is too large")
        amount = int(match.group(1))
        if amount > 36500:
            raise _ReliabilityError("invalid_since", "since duration is too large")
        delta = _datetime.timedelta(days=amount) if match.group(2) == "d" else _datetime.timedelta(hours=amount)
        return now - delta
    parsed = _parse_timestamp(since)
    if parsed is None:
        raise _ReliabilityError("invalid_since", "since must be a duration such as 7d or an ISO timestamp")
    if parsed > now or now - parsed > _datetime.timedelta(days=36500):
        raise _ReliabilityError("invalid_since", "since must fall within the last 36500 days")
    return parsed


def _clean_record(value, worktree_id):
    if not isinstance(value, dict):
        return None
    event = value.get("event")
    timestamp = _parse_timestamp(value.get("timestamp"))
    event_id = value.get("event_id")
    if (not isinstance(event, str) or not _EVENT_NAME.fullmatch(event) or timestamp is None
            or not isinstance(event_id, str) or not _SAFE_TOKEN.fullmatch(event_id)
            or value.get("worktree_id") != worktree_id
            or value.get("runtime_version") != _RUNTIME_VERSION
            or not isinstance(value.get("runtime_fingerprint"), str)
            or not _HEX_DIGEST.fullmatch(value["runtime_fingerprint"])):
        return None
    record = {
        "event_id": event_id,
        "timestamp": _stamp(timestamp),
        "worktree_id": worktree_id,
        "runtime_version": _RUNTIME_VERSION,
        "runtime_fingerprint": value["runtime_fingerprint"].lower(),
        "event": event,
    }
    for field in ("harness", "session", "enrollment_id", "result", "correlation_id"):
        item = value.get(field)
        if item is not None:
            if field == "harness":
                if item not in ("claude", "codex", "opencode"):
                    return None
            elif not isinstance(item, str) or not _SAFE_TOKEN.fullmatch(item):
                return None
            record[field] = item
    details = value.get("details", {})
    if not isinstance(details, dict):
        return None
    safe_details = {}
    for key, item in details.items():
        if key not in _DETAIL_TYPES:
            return None
        clean = _detail_value(key, item)
        if clean is None:
            return None
        safe_details[key] = clean
    if safe_details:
        record["details"] = safe_details
    return record


def _summary(events):
    by_event = {}
    incidents = set()
    signatures = set()
    duplicates = 0
    correlated = {}
    for record in events:
        name = record["event"]
        by_event[name] = by_event.get(name, 0) + 1
        correlation = record.get("correlation_id")
        if correlation is not None:
            incident_key = (*_identity(record), correlation)
            incidents.add(incident_key)
            signature = (*incident_key,
                         name, record.get("result"), json.dumps(record.get("details", {}), sort_keys=True))
            if signature in signatures:
                duplicates += 1
            signatures.add(signature)
            correlated.setdefault(incident_key, []).append(record)
        else:
            incidents.add(("event", record["event_id"]))
    emitted = sum(count for name, count in by_event.items()
                  if name in ("delivery.emitted", "standing.injection_emitted", "context.missing_alarm"))
    submitted = by_event.get("delivery.submitted", 0)
    received = by_event.get("receipt.received", 0)
    published = by_event.get("note.published", 0)
    arm_outcomes = {}
    for record in events:
        if record["event"] == "reminder.armed":
            outcome = record.get("result", "unknown")
            arm_outcomes[outcome] = arm_outcomes.get(outcome, 0) + 1
    arm_attempts = by_event.get("reminder.armed", 0)
    armed = arm_outcomes.get("armed", 0)
    eligible = by_event.get("reminder.eligible", 0)
    unknowns = []
    uncorrelated = sum(1 for record in events if not record.get("correlation_id"))
    if uncorrelated:
        unknowns.append({"code": "events_without_correlation", "count": uncorrelated})
    durations_by_pair = {}
    for key, (start_name, end_name) in _DURATION_PAIRS.items():
        durations = []
        for records in correlated.values():
            starts = [_parse_timestamp(row["timestamp"]) for row in records if row["event"] == start_name]
            ends = [_parse_timestamp(row["timestamp"]) for row in records if row["event"] == end_name]
            starts = [value for value in starts if value is not None]
            ends = [value for value in ends if value is not None]
            if starts and ends:
                seconds = (min(ends) - min(starts)).total_seconds()
                if seconds >= 0:
                    durations.append(round(seconds, 3))
        if durations:
            durations_by_pair[key] = durations
    for incident_key, records in correlated.items():
        correlation = incident_key[-1]
        names = {record["event"] for record in records}
        if "reminder.eligible" in names and "compaction.observed" not in names and "compaction.unknown" not in names:
            unknowns.append({"code": "compaction_boundary_unknown", "correlation_id": correlation})
        if "delivery.emitted" in names and "receipt.received" not in names:
            unknowns.append({"code": "receipt_not_observed", "correlation_id": correlation})
    return {
        "event_count": len(events),
        "unique_incidents": len(incidents),
        "duplicate_events": duplicates,
        "emitted": emitted,
        "submitted": submitted,
        "received": received,
        "published": published,
        "armed": armed,
        "timer_arm_attempts": arm_attempts,
        "arm_outcomes": arm_outcomes,
        "eligible": eligible,
        "by_event": by_event,
        "durations_seconds": durations_by_pair,
    }, unknowns


def _identity(record):
    return record.get("harness"), record.get("session"), record.get("enrollment_id")


def _binding(record):
    harness, session, enrollment_id = _identity(record)
    if harness not in ("claude", "codex") or not session or not enrollment_id:
        return None
    return harness, session, enrollment_id


def _cross_event_durations(events, history):
    """Pair retained lifecycle timestamps by exact active owner identity."""
    timeline = {}
    for stamp, record in history:
        binding = _binding(record)
        if binding is None:
            continue
        key = (binding, record["event"])
        timeline.setdefault(key, []).append((stamp, record))
    timeline = {key: ([stamp for stamp, _ in rows], rows) for key, rows in timeline.items()}

    def prior(event, record, before):
        binding = _binding(record)
        indexed = timeline.get((binding, event)) if binding else None
        if indexed is None:
            return None
        stamps, rows = indexed
        position = bisect_right(stamps, before) - 1
        if position < 0 or rows[position][0] >= before:
            return None
        return rows[position]

    warning_publication = []
    publication_compaction = []
    warning_unknowns = 0
    compaction_unknowns = 0
    recovery_ages = []
    recovery_unknowns = 0
    for record in events:
        stamp = _parse_timestamp(record["timestamp"])
        if record["event"] == "note.published":
            warning = prior("delivery.emitted", record, stamp)
            if warning:
                warning_publication.append(round((stamp - warning[0]).total_seconds(), 3))
            elif prior("reminder.eligible", record, stamp):
                warning_unknowns += 1
        elif record["event"] == "compaction.observed":
            publication = prior("note.published", record, stamp)
            if publication:
                publication_compaction.append(round((stamp - publication[0]).total_seconds(), 3))
            else:
                compaction_unknowns += 1
        elif record["event"] == "recovery.observed":
            reviewed_at = _parse_timestamp(record.get("details", {}).get("reviewed_at"))
            if reviewed_at is not None and reviewed_at <= stamp:
                recovery_ages.append(round((stamp - reviewed_at).total_seconds(), 3))
            else:
                recovery_unknowns += 1
    durations = {}
    if warning_publication:
        durations["warning_emission_to_publication"] = warning_publication
    if publication_compaction:
        durations["publication_to_compaction"] = publication_compaction
    unknowns = []
    if warning_unknowns:
        unknowns.append({"code": "warning_emission_boundary_unknown", "count": warning_unknowns})
    if compaction_unknowns:
        unknowns.append({"code": "publication_before_compaction_unknown", "count": compaction_unknowns})
    if recovery_unknowns:
        unknowns.append({"code": "recovery_note_age_unknown", "count": recovery_unknowns})
    return durations, recovery_ages, unknowns


def report(root, since="7d"):
    """Read this worktree's bounded history and report observed facts/gaps."""
    try:
        state_dir, worktree_id = _state_path(root)
        now = _utc_now()
        start = _parse_since(since, now)
        exists = _check_directory_chain(state_dir, create=False)
        if not exists:
            return {
                "ok": True,
                "worktree_id": worktree_id,
                "since": _stamp(start),
                "generated_at": _stamp(now),
                "summary": {"event_count": 0, "unique_incidents": 0, "duplicate_events": 0,
                            "emitted": 0, "submitted": 0, "received": 0, "published": 0,
                            "armed": 0, "timer_arm_attempts": 0, "arm_outcomes": {}, "eligible": 0,
                            "by_event": {}, "durations_seconds": {},
                            "note_age_at_recovery_seconds": []},
                "events": [],
                "coverage": {"covered_days": [], "missing_days": [], "history_covers_requested_start": False,
                             "retained_from": None, "retained_through": None,
                             "oldest_event": None, "newest_event": None, "malformed_lines": 0},
                "unknowns": [{"code": "no_telemetry_history"}],
            }
        with _locked(state_dir):
            config = _config(state_dir)
            _maintenance(state_dir, config, now.date())
            files = _daily_files(state_dir)
            all_events = []
            malformed = 0
            for day, path, _size in files:
                fd = _open_regular(path, os.O_RDONLY)
                file_malformed = 0
                try:
                    with os.fdopen(fd, "rb") as handle:
                        while True:
                            line = handle.readline(_MAX_EVENT_BYTES + 1)
                            if not line:
                                break
                            if len(line) > _MAX_EVENT_BYTES or not line.endswith(b"\n"):
                                while line and not line.endswith(b"\n"):
                                    line = handle.readline(_MAX_EVENT_BYTES + 1)
                                file_malformed += 1
                                continue
                            try:
                                line = line.decode("utf-8")
                            except UnicodeDecodeError:
                                file_malformed += 1
                                continue
                            try:
                                value = json.loads(line)
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                file_malformed += 1
                                continue
                            record = _clean_record(value, worktree_id)
                            if record is None:
                                file_malformed += 1
                                continue
                            stamp = _parse_timestamp(record["timestamp"])
                            all_events.append((stamp, record))
                except OSError as error:
                    raise _ReliabilityError("history_unavailable", "reliability history could not be read") from error
                if start.date() <= day <= now.date():
                    malformed += file_malformed
        all_events.sort(key=lambda item: (item[0], item[1]["event_id"]))
        events = [record for stamp, record in all_events if start <= stamp <= now]
        summary, unknowns = _summary(events)
        cross_durations, recovery_ages, cross_unknowns = _cross_event_durations(events, all_events)
        summary["durations_seconds"].update(cross_durations)
        summary["note_age_at_recovery_seconds"] = recovery_ages
        unknowns.extend(cross_unknowns)
        unknown_event_types = {}
        for record in events:
            if record["event"] not in _LIFECYCLE_EVENTS:
                unknown_event_types[record["event"]] = unknown_event_types.get(record["event"], 0) + 1
        if malformed:
            unknowns.append({"code": "malformed_events", "count": malformed})
        if unknown_event_types:
            unknowns.append({"code": "unrecognized_event_types", "counts": unknown_event_types})
        covered_dates = sorted({day.isoformat() for day, _path, _size in files
                                if start.date() <= day <= now.date()})
        cursor = start.date()
        today = now.date()
        missing_days = []
        covered_set = set(covered_dates)
        while cursor <= today:
            if cursor.isoformat() not in covered_set:
                missing_days.append(cursor.isoformat())
            cursor += _datetime.timedelta(days=1)
        oldest = events[0]["timestamp"] if events else None
        newest = events[-1]["timestamp"] if events else None
        retained_from = min((day.isoformat() for day, _path, _size in files), default=None)
        retained_through = max((day.isoformat() for day, _path, _size in files), default=None)
        history_covers_start = bool(retained_from and retained_from <= start.date().isoformat())
        if not history_covers_start:
            unknowns.append({"code": "requested_history_start_uncovered"})
        if missing_days:
            unknowns.append({"code": "days_without_observed_events", "count": len(missing_days)})
        return {
            "ok": True,
            "worktree_id": worktree_id,
            "since": _stamp(start),
            "generated_at": _stamp(now),
            "summary": summary,
            "events": events,
            "coverage": {
                "retention_days": config["retention_days"],
                "max_bytes": config["max_bytes"],
                "covered_days": covered_dates,
                "missing_days": missing_days,
                "history_covers_requested_start": history_covers_start,
                "retained_from": retained_from,
                "retained_through": retained_through,
                "oldest_event": oldest,
                "newest_event": newest,
                "malformed_lines": malformed,
            },
            "unknowns": unknowns,
        }
    except _ReliabilityError as error:
        return _error(error.code, error.message)
    except (OSError, ValueError, TypeError) as error:
        return _error("telemetry_error", "reliability history could not be reported")
