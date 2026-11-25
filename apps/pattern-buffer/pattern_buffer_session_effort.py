"""Read exact-session model effort evidence for Pattern Buffer."""

import datetime
import json
import os
from pathlib import Path
import re
import stat
import uuid


MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024
MAX_DISCOVERY_ENTRIES = 50000
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}\Z")
_EFFORTS = {
    "claude": frozenset(("low", "medium", "high", "xhigh", "max")),
    "codex": frozenset(("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")),
}


def _session_id(value):
    if not isinstance(value, str):
        return None
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return None
    return canonical if canonical == value else None


def _model(value):
    return value if isinstance(value, str) and _MODEL.fullmatch(value) else None


def _effort(harness, value):
    return value if isinstance(value, str) and value in _EFFORTS[harness] else None


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(datetime.timezone.utc)


def _format_timestamp(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _report(harness, session, model=None, effort=None, source=None, observed_at=None, now=None):
    parsed = _timestamp(observed_at) if observed_at else None
    current = now or datetime.datetime.now(datetime.timezone.utc)
    age = max(0, int((current - parsed).total_seconds())) if parsed else None
    return {
        "schema_version": 1,
        "harness": harness,
        "session": session,
        "model": model,
        "effort": effort,
        "source": source,
        "observed_at": _format_timestamp(parsed) if parsed else None,
        "age_seconds": age,
        "state": "last_observed" if effort is not None and parsed else "unknown",
    }


def _state_directory(state_dir=None):
    if state_dir is not None:
        value = Path(state_dir).expanduser()
        if not value.is_absolute():
            return None
        base = value
    else:
        configured = os.environ.get("XDG_STATE_HOME")
        if configured:
            base = Path(configured).expanduser()
            if not base.is_absolute():
                return None
        else:
            base = Path.home() / ".local" / "state"
        base = base / "starfleet-ai" / "pattern-buffer" / "session-effort"
    if ".." in base.parts:
        return None
    return base


def observe_claude_statusline(sessions, payload, state_dir=None):
    if not isinstance(payload, dict):
        return False
    session = _session_id(payload.get("session_id"))
    if session is None:
        return False
    model_data = payload.get("model")
    effort_data = payload.get("effort")
    model = _model(model_data.get("id")) if isinstance(model_data, dict) else None
    effort = _effort("claude", effort_data.get("level")) if isinstance(effort_data, dict) else None
    directory = _state_directory(state_dir)
    if directory is None:
        raise ValueError("private state directory is invalid")
    record = {
        "schema_version": 1,
        "harness": "claude",
        "session": session,
        "model": model,
        "effort": effort,
        "source": "claude_statusline",
        "observed_at": _format_timestamp(datetime.datetime.now(datetime.timezone.utc)),
    }
    path = directory / f"claude-{session}.json"
    sessions._atomic_write(path, json.dumps(record, separators=(",", ":")).encode("utf-8"))
    return True


def _read_claude(sessions, session, state_dir=None, now=None):
    directory = _state_directory(state_dir)
    if directory is None:
        return _report("claude", session, now=now)
    path = directory / f"claude-{session}.json"
    try:
        raw = sessions._read_file(path, 16 * 1024, missing_ok=True)
        if raw is None:
            return _report("claude", session, now=now)
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {
            "schema_version", "harness", "session", "model", "effort", "source", "observed_at"
        }:
            return _report("claude", session, now=now)
        if (data["schema_version"] != 1 or data["harness"] != "claude"
                or data["session"] != session or data["source"] != "claude_statusline"):
            return _report("claude", session, now=now)
        observed = _timestamp(data["observed_at"])
        if observed is None:
            return _report("claude", session, now=now)
        model = _model(data["model"])
        effort = _effort("claude", data["effort"])
        return _report("claude", session, model, effort, "claude_statusline",
                       _format_timestamp(observed), now)
    except (OSError, ValueError, TypeError, UnicodeError):
        return _report("claude", session, now=now)


def _directory(path):
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return False
    return True


def _discover_transcript(codex_home, session):
    home = Path(codex_home).expanduser().absolute()
    if ".." in home.parts:
        return None
    root = home / "sessions"
    if not _directory(root):
        return None
    entries = 0
    matches = []
    try:
        with os.scandir(root) as years:
            for year in years:
                entries += 1
                if entries > MAX_DISCOVERY_ENTRIES:
                    return None
                if not re.fullmatch(r"[0-9]{4}", year.name) or not year.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(year.path) as months:
                    for month in months:
                        entries += 1
                        if entries > MAX_DISCOVERY_ENTRIES:
                            return None
                        if not re.fullmatch(r"(?:0[1-9]|1[0-2])", month.name) or not month.is_dir(follow_symlinks=False):
                            continue
                        with os.scandir(month.path) as days:
                            for day in days:
                                entries += 1
                                if entries > MAX_DISCOVERY_ENTRIES:
                                    return None
                                if not re.fullmatch(r"(?:0[1-9]|[12][0-9]|3[01])", day.name) or not day.is_dir(follow_symlinks=False):
                                    continue
                                with os.scandir(day.path) as files:
                                    for item in files:
                                        entries += 1
                                        if entries > MAX_DISCOVERY_ENTRIES:
                                            return None
                                        if (item.name.startswith("rollout-")
                                                and item.name.endswith(f"-{session}.jsonl")
                                                and item.is_file(follow_symlinks=False)):
                                            matches.append(Path(item.path))
                                            if len(matches) > 1:
                                                return None
    except OSError:
        return None
    return matches[0] if matches else None


def _transcript_window(sessions, path, session):
    sessions._check_path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return None
        head = os.read(descriptor, MAX_METADATA_BYTES)
        first, newline, _rest = head.partition(b"\n")
        if not newline:
            return None
        metadata = json.loads(first)
        if (not isinstance(metadata, dict) or metadata.get("type") != "session_meta"
                or not isinstance(metadata.get("payload"), dict)
                or _session_id(metadata["payload"].get("id")) != session):
            return None
        offset = max(0, before.st_size - MAX_TRANSCRIPT_BYTES)
        os.lseek(descriptor, offset, os.SEEK_SET)
        remaining = before.st_size - offset
        chunks = []
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if ((current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                or after.st_size < before.st_size or not raw.endswith(b"\n")):
            return None
        if offset:
            raw = raw.partition(b"\n")[2]
        return raw.decode("utf-8")
    finally:
        os.close(descriptor)


def _read_codex(sessions, session, transcript=None, codex_home=None, now=None):
    if transcript is None:
        home = codex_home or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        path = _discover_transcript(home, session)
        if path is None:
            return _report("codex", session, now=now)
    else:
        path = Path(transcript).expanduser().absolute()
    if ".." in path.parts:
        return _report("codex", session, now=now)
    try:
        text = _transcript_window(sessions, path, session)
        if text is None:
            return _report("codex", session, now=now)
        latest = None
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except (ValueError, TypeError):
                return _report("codex", session, now=now)
            if not isinstance(item, dict):
                return _report("codex", session, now=now)
            kind = item.get("type")
            payload = item.get("payload")
            if kind == "session_meta":
                if not isinstance(payload, dict):
                    return _report("codex", session, now=now)
                observed_session = _session_id(payload.get("id"))
                if observed_session != session:
                    return _report("codex", session, now=now)
            elif kind == "turn_context":
                if not isinstance(payload, dict):
                    return _report("codex", session, now=now)
                timestamp = _timestamp(item.get("timestamp"))
                if timestamp is None:
                    return _report("codex", session, now=now)
                model = _model(payload.get("model"))
                effort = _effort("codex", payload.get("effort"))
                if latest is None or timestamp >= latest[0]:
                    latest = (timestamp, model, effort)
        if latest is None:
            return _report("codex", session, now=now)
        timestamp, model, effort = latest
        return _report("codex", session, model, effort, "codex_turn_context",
                       _format_timestamp(timestamp), now)
    except (OSError, ValueError, TypeError, UnicodeError):
        return _report("codex", session, now=now)


def read_session_effort(sessions, harness, session, *, transcript=None, state_dir=None, codex_home=None, now=None):
    if harness not in _EFFORTS:
        raise ValueError("harness must be claude or codex")
    canonical = _session_id(session)
    if canonical is None:
        raise ValueError("session must be a canonical UUID")
    if harness == "claude":
        if transcript is not None or codex_home is not None:
            raise ValueError("Codex transcript options do not apply to Claude")
        return _read_claude(sessions, canonical, state_dir, now)
    if state_dir is not None:
        raise ValueError("Claude state options do not apply to Codex")
    return _read_codex(sessions, canonical, transcript, codex_home, now)
