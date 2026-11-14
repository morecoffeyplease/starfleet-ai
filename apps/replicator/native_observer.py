"""Read a bounded Codex settings snapshot from the exact current native process."""

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import time


OBSERVATION_PROTOCOL = "starfleet.replicator.native-observation/codex-rollout/v1"
HOOK_STORE_NAME = "codex-hook-sessions.json"
HOOK_STORE_MAX_BYTES = 8 * 1024 * 1024
ROLLOUT_HEAD_BYTES = 256 * 1024
ROLLOUT_TAIL_BYTES = 8 * 1024 * 1024
ROLLOUT_MAX_LINE_BYTES = 1024 * 1024
SUPPORTED_CLI_VERSIONS = {"0.166.0", "0.169.0"}
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z", re.I)


class NativeObservationError(ValueError):
    pass


def _utc_timestamp(value):
    if not isinstance(value, str) or not value:
        raise NativeObservationError("The native rollout timestamp is unavailable.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NativeObservationError("The native rollout timestamp is malformed.") from exc
    if parsed.tzinfo is None:
        raise NativeObservationError("The native rollout timestamp has no timezone.")
    return parsed.astimezone(timezone.utc)


def _skip_json_value(data, offset):
    size = len(data)
    while offset < size and data[offset] in b" \t\r\n":
        offset += 1
    if offset >= size:
        raise NativeObservationError("The native rollout contains incomplete JSON.")
    first = data[offset]
    if first == ord('"'):
        offset += 1
        while offset < size:
            current = data[offset]
            if current == ord('\\'):
                offset += 2
                continue
            if current == ord('"'):
                return offset + 1
            offset += 1
        raise NativeObservationError("The native rollout contains an incomplete string.")
    if first in (ord('{'), ord('[')):
        stack = [ord('}') if first == ord('{') else ord(']')]
        offset += 1
        in_string = False
        while offset < size and stack:
            current = data[offset]
            if in_string:
                if current == ord('\\'):
                    offset += 2
                    continue
                if current == ord('"'):
                    in_string = False
            elif current == ord('"'):
                in_string = True
            elif current == ord('{'):
                stack.append(ord('}'))
            elif current == ord('['):
                stack.append(ord(']'))
            elif current in (ord('}'), ord(']')):
                if not stack or stack.pop() != current:
                    raise NativeObservationError("The native rollout contains malformed JSON.")
            offset += 1
        if stack or in_string:
            raise NativeObservationError("The native rollout contains incomplete JSON.")
        return offset
    while offset < size and data[offset] not in b",}] \t\r\n":
        offset += 1
    return offset


def _object_raw_fields(data, wanted):
    data = data.strip()
    if not data.startswith(b"{") or not data.endswith(b"}"):
        raise NativeObservationError("The native rollout record is not a JSON object.")
    try:
        json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise NativeObservationError("The native rollout record contains malformed JSON.") from exc
    result = {}
    offset = 1
    while offset < len(data) - 1:
        while offset < len(data) and data[offset] in b" \t\r\n,":
            offset += 1
        if offset >= len(data) - 1:
            break
        key_end = _skip_json_value(data, offset)
        try:
            key = json.loads(data[offset:key_end].decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise NativeObservationError("The native rollout record has a malformed key.") from exc
        if not isinstance(key, str):
            raise NativeObservationError("The native rollout record has a non-text key.")
        offset = key_end
        while offset < len(data) and data[offset] in b" \t\r\n":
            offset += 1
        if offset >= len(data) or data[offset] != ord(':'):
            raise NativeObservationError("The native rollout record is malformed.")
        start = offset + 1
        end = _skip_json_value(data, start)
        if key in wanted:
            if key in result:
                raise NativeObservationError("The native rollout record repeats a required field.")
            result[key] = data[start:end].strip()
        offset = end
    return result


def _decode_field(fields, name, default=None):
    raw = fields.get(name)
    if raw is None:
        return default
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise NativeObservationError("The native rollout contains malformed settings.") from exc


def _event(data):
    fields = _object_raw_fields(data, {"type", "timestamp", "turn_id", "payload"})
    event_type = _decode_field(fields, "type")
    if event_type not in ("session_meta", "turn_context"):
        return None
    payload_raw = fields.get("payload")
    if payload_raw is None:
        raise NativeObservationError("A required native rollout event has no payload.")
    wanted = ({"id", "cwd", "cli_version", "originator", "source", "model_provider"}
              if event_type == "session_meta" else
              {"model", "effort", "approval_policy", "sandbox_policy", "turn_id", "cwd", "recorded_at"})
    payload = _object_raw_fields(payload_raw, wanted)
    decoded = {name: _decode_field(payload, name) for name in wanted if name in payload}
    decoded["type"] = event_type
    timestamp = _decode_field(fields, "timestamp")
    decoded["recorded_at"] = decoded.get("recorded_at") or timestamp
    decoded["turn_id"] = decoded.get("turn_id") or _decode_field(fields, "turn_id")
    return decoded


def _line_events(data, event_type, *, allow_partial_final=False):
    result = []
    lines = data.splitlines()
    if allow_partial_final and data and not data.endswith(b"\n"):
        lines = lines[:-1]
    for line in lines:
        if not line.strip():
            continue
        if len(line) > ROLLOUT_MAX_LINE_BYTES:
            raise NativeObservationError("A native rollout record exceeds the observation limit.")
        event = _event(line)
        if event is not None and event["type"] == event_type:
            result.append(event)
    return result


def _read_bounded(fd, limit):
    chunks = []
    remaining = limit
    while remaining:
        chunk = os.read(fd, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _private_json(path, max_bytes):
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise NativeObservationError("The exact CMUX Codex hook record is unavailable.") from exc
    if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
        raise NativeObservationError("The CMUX Codex hook record is not a regular file.")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise NativeObservationError("The exact CMUX Codex hook record could not be opened.") from exc
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_size > max_bytes):
            raise NativeObservationError("The CMUX Codex hook record is not a bounded owner file.")
        data = _read_bounded(fd, max_bytes + 1)
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or len(data) != before.st_size or len(data) > max_bytes):
            raise NativeObservationError("The CMUX Codex hook record changed while it was read.")
        current_path = path.lstat()
        if (current_path.st_dev, current_path.st_ino) != (before.st_dev, before.st_ino):
            raise NativeObservationError("The CMUX Codex hook record changed while it was read.")
        return json.loads(data.decode("utf-8")), before
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, NativeObservationError):
            raise
        raise NativeObservationError("The CMUX Codex hook record is malformed.") from exc
    finally:
        os.close(fd)


def _hook_record(home, environment, caller, worktree):
    override = environment.get("CMUX_AGENT_HOOK_STATE_DIR")
    state_dir = Path(override).expanduser() if isinstance(override, str) and override else Path(home) / ".cmuxterm"
    path = state_dir / HOOK_STORE_NAME
    document, info = _private_json(path, HOOK_STORE_MAX_BYTES)
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("sessions"), dict):
        raise NativeObservationError("The CMUX Codex hook store has an unsupported format.")
    record = document["sessions"].get(caller["session_id"])
    if (not isinstance(record, dict)
            or record.get("sessionId") != caller["session_id"]
            or not isinstance(record.get("surfaceId"), str)
            or record["surfaceId"].upper() != caller["panel_id"].upper()
            or not isinstance(record.get("workspaceId"), str)
            or record["workspaceId"].upper() != caller["workspace_id"].upper()
            or not isinstance(record.get("cwd"), str)
            or os.path.realpath(record["cwd"]) != worktree["root"]
            or type(record.get("pid")) is not int or record["pid"] <= 0
            or type(record.get("startedAt")) not in (int, float)
            or not math.isfinite(record["startedAt"]) or record["startedAt"] <= 0
            or type(record.get("updatedAt")) not in (int, float)
            or not math.isfinite(record["updatedAt"]) or record["updatedAt"] <= 0):
        raise NativeObservationError("The CMUX Codex hook record does not match the exact caller and worktree.")
    return record, path, info


def _command(runner, argv):
    try:
        status, output = runner(argv)
    except (OSError, ValueError, TimeoutError) as exc:
        raise NativeObservationError("A bounded native process query failed.") from exc
    if status != "ready" or not isinstance(output, str):
        raise NativeObservationError("A bounded native process query is unavailable.")
    return output


def _process_identity(runner, pid):
    output = _command(runner, ["ps", "-p", str(pid), "-o", "pid=,ppid=,lstart=,command="])
    rows = [line.strip() for line in output.splitlines() if line.strip()]
    if len(rows) != 1:
        raise NativeObservationError("The exact Codex process is missing or ambiguous.")
    pieces = rows[0].split(maxsplit=7)
    if len(pieces) != 8:
        raise NativeObservationError("The exact Codex process identity is incomplete.")
    try:
        observed_pid = int(pieces[0])
        parent_pid = int(pieces[1])
        started = datetime.strptime(" ".join(pieces[2:7]), "%a %b %d %H:%M:%S %Y").astimezone(timezone.utc)
        command = shlex.split(pieces[7])
    except (ValueError, IndexError) as exc:
        raise NativeObservationError("The exact Codex process identity is malformed.") from exc
    if observed_pid != pid or parent_pid <= 0 or not command or not Path(command[0]).is_absolute():
        raise NativeObservationError("The exact Codex process identity is malformed.")
    executable = os.path.realpath(command[0])
    if Path(executable).name != "codex":
        raise NativeObservationError("The exact live process is not the Codex executable.")
    return {
        "pid": observed_pid,
        "parent_pid": parent_pid,
        "started_at": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "executable": executable,
    }, started


def _open_rollout(runner, pid, session_id, home, environment):
    output = _command(runner, ["lsof", "-n", "-P", "-p", str(pid), "-Fn"])
    listed_pids = [line[1:] for line in output.splitlines() if line.startswith("p")]
    if listed_pids != [str(pid)]:
        raise NativeObservationError("The open Codex file list does not match the exact process.")
    candidates = set()
    for line in output.splitlines():
        if not line.startswith("n"):
            continue
        raw = line[1:].removesuffix(" (deleted)")
        path = Path(raw)
        if (path.is_absolute() and path.name.startswith("rollout-")
                and path.name.endswith(f"-{session_id}.jsonl")):
            candidates.add(path)
    if len(candidates) != 1:
        raise NativeObservationError("The Codex process does not have one open rollout for this session.")
    path = next(iter(candidates))
    codex_home = Path(environment.get("CODEX_HOME", Path(home) / ".codex")).expanduser()
    sessions_root = (codex_home / "sessions").resolve()
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(sessions_root)
    except (OSError, ValueError) as exc:
        raise NativeObservationError("The open Codex rollout is outside the caller's session store.") from exc
    if path.is_symlink() or resolved.name != path.name or path.name.endswith(" (deleted)"):
        raise NativeObservationError("The open Codex rollout path is unstable.")
    return resolved


def _read_rollout(path, process_started, observed_at):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise NativeObservationError("The exact open Codex rollout could not be read.") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise NativeObservationError("The exact Codex rollout is not an owner file.")
        head = _read_bounded(fd, min(before.st_size, ROLLOUT_HEAD_BYTES))
        metadata = _line_events(
            head, "session_meta", allow_partial_final=len(head) < before.st_size,
        )
        if len(metadata) != 1:
            raise NativeObservationError("The open Codex rollout has missing or ambiguous session metadata.")
        tail_start = max(0, before.st_size - ROLLOUT_TAIL_BYTES)
        os.lseek(fd, tail_start, os.SEEK_SET)
        tail = _read_bounded(fd, before.st_size - tail_start)
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise NativeObservationError("The open Codex rollout changed while it was read.")
        if tail_start:
            tail = tail.partition(b"\n")[2]
        contexts = _line_events(tail, "turn_context")
        if not contexts:
            raise NativeObservationError("The open Codex rollout has no bounded native turn settings.")
        parsed = []
        for context in contexts:
            timestamp = _utc_timestamp(context.get("recorded_at"))
            if timestamp <= process_started or timestamp > observed_at:
                continue
            parsed.append((timestamp, context))
        if not parsed:
            raise NativeObservationError("The latest native turn settings do not belong to this process generation.")
        latest_timestamp = max(timestamp for timestamp, _ in parsed)
        latest = [context for timestamp, context in parsed if timestamp == latest_timestamp]
        if len(latest) != 1:
            raise NativeObservationError("The latest native turn settings are ambiguous.")
        context = latest[0]
        meta = metadata[0]
        if (not isinstance(meta.get("id"), str) or not isinstance(meta.get("cwd"), str)
                or meta.get("cli_version") not in SUPPORTED_CLI_VERSIONS
                or meta.get("source") != "cli" or meta.get("originator") != "codex-tui"
                or meta.get("model_provider") != "openai"):
            raise NativeObservationError("The Codex rollout metadata has an unsupported version or source.")
        for name in ("model", "effort", "approval_policy", "turn_id", "cwd"):
            if not isinstance(context.get(name), str) or not context[name]:
                raise NativeObservationError("The latest native turn settings are incomplete.")
        sandbox_policy = context.get("sandbox_policy")
        if not isinstance(sandbox_policy, dict) or not sandbox_policy:
            raise NativeObservationError("The latest native sandbox setting is unavailable.")
        source = {
            "kind": "open_codex_rollout",
            "path": str(path),
            "device": before.st_dev,
            "inode": before.st_ino,
            "size": before.st_size,
            "modified_at_ns": before.st_mtime_ns,
            "cli_version": meta["cli_version"],
            "session_meta_cwd": meta["cwd"],
            "session_meta_source": meta["source"],
            "session_meta_originator": meta["originator"],
        }
        turn = {
            "id": context["turn_id"],
            "recorded_at": latest_timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "cwd": context["cwd"],
        }
        settings = {
            "model": context["model"],
            "effort": context["effort"],
            "approval_policy": context["approval_policy"],
            "sandbox_policy": sandbox_policy,
        }
        return source, turn, settings, meta["id"], after
    finally:
        os.close(fd)


def observe_codex_predecessor(scope, caller, worktree, *, home, environment, runner, clock=None):
    now = time.time if clock is None else clock
    seat = scope.get("seat") if isinstance(scope, dict) else None
    if (not isinstance(seat, dict) or seat.get("harness") != "codex"
            or caller.get("harness") != "codex"):
        raise NativeObservationError("Native predecessor observation currently supports Codex only.")
    expected_caller = {
        "harness": seat.get("harness"),
        "session_id": seat.get("session_id"),
        "panel_id": seat.get("panel_id"),
        "workspace_id": seat.get("workspace_id"),
    }
    if caller != expected_caller or worktree != scope.get("worktree"):
        raise NativeObservationError("The exact native caller or canonical worktree does not match this handoff.")
    session_id = seat.get("session_id")
    if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
        raise NativeObservationError("The frozen predecessor session identity is malformed.")
    hook, hook_path, hook_info = _hook_record(home, environment, caller, worktree)
    process_before, process_started = _process_identity(runner, hook["pid"])
    rollout = _open_rollout(runner, hook["pid"], session_id, home, environment)
    observed_time = datetime.fromtimestamp(now(), timezone.utc)
    source, turn, settings, metadata_session_id, rollout_info = _read_rollout(
        rollout, process_started, observed_time,
    )
    if metadata_session_id != session_id:
        raise NativeObservationError("The rollout session metadata does not match the exact caller.")
    rollout_again = _open_rollout(runner, hook["pid"], session_id, home, environment)
    process_after, _ = _process_identity(runner, hook["pid"])
    current_hook_info = hook_path.lstat()
    current_rollout_info = rollout.stat()
    if (rollout_again != rollout or process_before != process_after
            or (hook_info.st_dev, hook_info.st_ino, hook_info.st_mtime_ns) !=
               (current_hook_info.st_dev, current_hook_info.st_ino, current_hook_info.st_mtime_ns)
            or (rollout_info.st_dev, rollout_info.st_ino, rollout_info.st_size, rollout_info.st_mtime_ns) !=
               (current_rollout_info.st_dev, current_rollout_info.st_ino,
                current_rollout_info.st_size, current_rollout_info.st_mtime_ns)):
        raise NativeObservationError("The native caller, process, or rollout changed during observation.")
    if (os.path.realpath(source["session_meta_cwd"]) != worktree["root"]
            or os.path.realpath(turn["cwd"]) != worktree["root"]):
        raise NativeObservationError("The native rollout worktree does not match the frozen handoff scope.")
    return {
        "protocol": OBSERVATION_PROTOCOL,
        "observed_at": observed_time.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "subject": {
            "harness": "codex",
            "session_id": session_id,
            "panel_id": caller["panel_id"],
            "workspace_id": caller["workspace_id"],
            "worktree": worktree,
        },
        "hook_generation": format(float(hook["startedAt"]), ".17g"),
        "process_identity": process_before,
        "hook_source": {
            "kind": "cmux_codex_hook_store_v1",
            "path": str(hook_path),
            "device": hook_info.st_dev,
            "inode": hook_info.st_ino,
            "modified_at_ns": hook_info.st_mtime_ns,
            "updated_at": float(hook["updatedAt"]),
        },
        "rollout_source": source,
        "turn": turn,
        "settings": settings,
    }
