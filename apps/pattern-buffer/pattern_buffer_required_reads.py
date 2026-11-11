"""Required-read list validation and per-session receipt state for Pattern Buffer."""
from dataclasses import dataclass
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import time


LIST_LIMIT = 24 * 1024
DOCUMENT_LIMIT = 24 * 1024
STATE_LIMIT = 512 * 1024
ENTRY_LIMIT = 64
NAME_LIMIT = 80
REASON_LIMIT = 500
PART_SIZE = {"claude": 8192, "codex": 8192}
PENDING_READ_LIMIT = 64
ROLES = {"implementer", "orchestrator"}
OWNER_SESSION_VARIABLES = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID")
RESET_SOURCES = {"startup", "compact", "clear", "fork"}


class IntegrityError(ValueError):
    """A required-read source or state failed an integrity check."""


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    data: bytes
    identity: tuple[int, int, int, int, int]

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class Entry:
    name: str
    path: Path
    roles: tuple[str, ...]

    def applies_to(self, role):
        return role is None or role in self.roles


@dataclass(frozen=True)
class RequiredList:
    registration_path: Path
    list_path: Path
    digest: str
    entries: tuple[Entry, ...]
    registration_identity: tuple[int, int, int, int, int]
    list_identity: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class ReadSelection:
    entry: Entry
    snapshot: FileSnapshot
    part: int
    part_count: int
    piece: bytes


def registration_path(gitdir):
    return Path(gitdir) / "context-injector" / "required-reads.json"


def state_path(gitdir, harness, session):
    if harness not in PART_SIZE:
        raise ValueError("required reads support Claude and Codex only")
    if not isinstance(session, str) or not session or len(session) > 512:
        raise ValueError("a nonempty native session ID is required")
    key = hashlib.sha256(json.dumps([harness, session]).encode()).hexdigest()
    return Path(gitdir) / "context-injector" / "required-reads" / f"{key}.json"


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _snapshot(path, limit, label):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise IntegrityError(f"{label} is missing, unreadable, or unsafe: {path}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise IntegrityError(f"{label} is not a regular file: {path}")
        if before.st_size > limit:
            raise IntegrityError(f"{label} exceeds {limit} bytes: {path}")
        chunks = []
        total = 0
        while total <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after) or len(data) != after.st_size:
            raise IntegrityError(f"{label} changed while it was being read: {path}")
        if len(data) > limit:
            raise IntegrityError(f"{label} exceeds {limit} bytes: {path}")
        return FileSnapshot(path, data, _identity(after))
    except OSError as error:
        raise IntegrityError(f"{label} could not be read: {path}") from error
    finally:
        os.close(descriptor)


def _home_path(value, label):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise IntegrityError(f"{label} path must be a nonempty string")
    if value == "~" or value.startswith("~/"):
        home = Path.home().absolute()
        path = (home / value[2:]).absolute() if value.startswith("~/") else home
        if not path.is_relative_to(home):
            raise IntegrityError(f"{label} path must stay inside the current home directory")
    elif value.startswith("~"):
        raise IntegrityError(f"{label} path may use only the current home directory")
    else:
        path = Path(value)
        if not path.is_absolute():
            home = Path.home().absolute()
            path = (home / path).absolute()
            if not path.is_relative_to(home):
                raise IntegrityError(f"{label} relative path must stay inside the current home directory")
    return path.absolute()


def _entries(raw, source):
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise IntegrityError(f"required-reads list is malformed: {source}") from error
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or not isinstance(data.get("entries"), list)):
        raise IntegrityError("required-reads list must use schema_version 1 and an entries array")
    rows = data["entries"]
    if not rows:
        raise IntegrityError("required-reads list is empty")
    if len(rows) > ENTRY_LIMIT:
        raise IntegrityError(f"required-reads list exceeds {ENTRY_LIMIT} entries")
    entries = []
    names = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) != {"name", "path", "roles"}:
            raise IntegrityError(f"required-reads entry {index} must contain name, path, and roles")
        name = row["name"]
        try:
            encoded_name = name.encode("utf-8") if isinstance(name, str) else b""
        except UnicodeError as error:
            raise IntegrityError(f"required-reads entry {index} has an invalid name") from error
        if (not isinstance(name, str) or not name.strip() or len(encoded_name) > NAME_LIMIT
                or any(ord(character) < 32 or ord(character) == 127 for character in name)):
            raise IntegrityError(f"required-reads entry {index} has an invalid name")
        if name in names:
            raise IntegrityError(f"required-reads entry name is repeated: {name}")
        names.add(name)
        roles = row["roles"]
        if (not isinstance(roles, list) or not roles or
                any(not isinstance(role, str) or role not in ROLES for role in roles) or
                len(set(roles)) != len(roles)):
            raise IntegrityError(f"required-reads entry {name} has invalid roles")
        path = _home_path(row["path"], f"required-reads entry {name}")
        entries.append(Entry(name, path, tuple(roles)))
    return tuple(entries)


def _check_entry_files(entries):
    for entry in entries:
        snapshot = _snapshot(entry.path, DOCUMENT_LIMIT, f"required file {entry.name}")
        try:
            snapshot.data.decode("utf-8")
        except UnicodeError as error:
            raise IntegrityError(f"required file {entry.name} is not valid UTF-8: {entry.path}") from error


def _registration_data(snapshot):
    try:
        data = json.loads(snapshot.data.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise IntegrityError("required-reads registration is malformed") from error
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or not isinstance(data.get("path"), str)
            or not isinstance(data.get("sha256"), str)
            or len(data["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in data["sha256"])):
        raise IntegrityError("required-reads registration is malformed")
    path = Path(data["path"])
    if not path.is_absolute():
        raise IntegrityError("registered required-reads list path is not absolute")
    return path, data["sha256"]


def load_list(gitdir):
    path = registration_path(gitdir)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        raise IntegrityError(f"required-reads registration is unsafe: {path}")
    registration = _snapshot(path, LIST_LIMIT, "required-reads registration")
    list_path, expected_digest = _registration_data(registration)
    source = _snapshot(list_path, LIST_LIMIT, "registered required-reads list")
    if source.sha256 != expected_digest:
        raise IntegrityError(f"registered required-reads list changed since registration: {list_path}")
    entries = _entries(source.data, list_path)
    return RequiredList(path, list_path, expected_digest, entries,
                        registration.identity, source.identity)


def _state_parent(path):
    parent = path.parent
    for candidate in (parent.parent, parent):
        if candidate.is_symlink():
            raise IntegrityError(f"required-reads state directory is unsafe: {candidate}")
        if candidate.exists() and not candidate.is_dir():
            raise IntegrityError(f"required-reads state directory is not a directory: {candidate}")


def _new_state(harness, session):
    return {"schema_version": 1, "harness": harness, "session_id": session,
            "generation": 0, "receipts": [], "override": None,
            "gate_opened_at": None, "list_sha256": None, "pending_reads": []}


def _read_state(path, harness, session):
    if not path.exists() and not path.is_symlink():
        return _new_state(harness, session)
    if path.is_symlink():
        raise IntegrityError("required-reads session state is unsafe")
    try:
        raw = _snapshot(path, STATE_LIMIT, "required-reads session state")
        state = json.loads(raw.data.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise IntegrityError("required-reads session state is malformed") from error
    if (not isinstance(state, dict) or state.get("schema_version") != 1
            or state.get("harness") != harness or state.get("session_id") != session
            or not isinstance(state.get("generation"), int) or state["generation"] < 0
            or not isinstance(state.get("receipts"), list)):
        raise IntegrityError("required-reads session state is malformed")
    if any(not isinstance(item, dict) or "content" in item for item in state["receipts"]):
        raise IntegrityError("required-reads receipts are malformed")
    if state.get("override") is not None and not isinstance(state["override"], dict):
        raise IntegrityError("required-reads override state is malformed")
    pending = state.setdefault("pending_reads", [])
    if not isinstance(pending, list) or len(pending) > PENDING_READ_LIMIT:
        raise IntegrityError("required-reads pending snapshots are malformed")
    pending_keys = {"tool_use_id", "command_sha256", "generation", "role", "list_sha256",
                    "registration_identity", "list_identity", "name", "path", "part",
                    "part_count", "file_sha256", "file_identity", "file_byte_count",
                    "byte_count", "prepared_at"}
    for item in pending:
        if (not isinstance(item, dict) or set(item) != pending_keys
                or not isinstance(item.get("tool_use_id"), str)
                or not item["tool_use_id"] or len(item["tool_use_id"]) > 512
                or not isinstance(item.get("command_sha256"), str)
                or len(item["command_sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in item["command_sha256"])
                or not isinstance(item.get("generation"), int)
                or item["generation"] < 1
                or item.get("role") is not None and item.get("role") not in ROLES
                or not isinstance(item.get("list_sha256"), str)
                or len(item["list_sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in item["list_sha256"])
                or not isinstance(item.get("file_sha256"), str)
                or len(item["file_sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in item["file_sha256"])
                or not isinstance(item.get("registration_identity"), list)
                or not isinstance(item.get("list_identity"), list)
                or not isinstance(item.get("file_identity"), list)
                or any(not isinstance(identity, list) or len(identity) != 5
                       or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                              for value in identity)
                       for identity in (item["registration_identity"], item["list_identity"],
                                        item["file_identity"]))
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("part"), int) or isinstance(item["part"], bool)
                or not isinstance(item.get("part_count"), int) or isinstance(item["part_count"], bool)
                or not 1 <= item["part"] <= item["part_count"]
                or not isinstance(item.get("file_byte_count"), int)
                or not 0 <= item["file_byte_count"] <= DOCUMENT_LIMIT
                or not isinstance(item.get("byte_count"), int)
                or not 0 <= item["byte_count"] <= PART_SIZE[harness]
                or not isinstance(item.get("prepared_at"), (int, float))):
            raise IntegrityError("required-reads pending snapshots are malformed")
    return state


def _write_state(core, path, state):
    _state_parent(path)
    core.atomic(path, json.dumps(state, sort_keys=True, separators=(",", ":")))


def _state_lock(core, path):
    _state_parent(path)
    return core.locked(path.with_suffix(".lock"))


def session_start(core, root, gitdir, harness, session, source):
    if not session or not registration_path(gitdir).exists():
        return None
    if source not in RESET_SOURCES | {"resume", "pretool_gate"}:
        return None
    path = state_path(gitdir, harness, session)
    started_at_gate = False
    with _state_lock(core, path):
        state = _read_state(path, harness, session)
        if source == "pretool_gate" and state["generation"] > 0:
            return state
        started_at_gate = source == "pretool_gate" and state["generation"] == 0
        if source in RESET_SOURCES or state["generation"] == 0:
            state.update(generation=state["generation"] + 1, receipts=[], override=None,
                         gate_opened_at=None, pending_reads=[])
        try:
            required = load_list(gitdir)
            state["list_sha256"] = required.digest if required else None
        except IntegrityError:
            state["list_sha256"] = None
        _write_state(core, path, state)
    if started_at_gate:
        core.record_event(root, "required_reads.generation_started", harness=harness,
                          session=session, result="pretool_gate",
                          details={"source": "pretool_gate", "generation": state["generation"]})
    return state


def _current_state(core, gitdir, harness, session, create=True):
    path = state_path(gitdir, harness, session)
    if not create:
        return path, _read_state(path, harness, session)
    with _state_lock(core, path):
        state = _read_state(path, harness, session)
        if create and state["generation"] == 0:
            state["generation"] = 1
            required = load_list(gitdir)
            state["list_sha256"] = required.digest if required else None
            _write_state(core, path, state)
        return path, state


def _part_ranges(data, size):
    if not data:
        return [(0, 0)]
    ranges = []
    start = 0
    while start < len(data):
        end = min(start + size, len(data))
        while end < len(data) and end > start and data[end] & 0xC0 == 0x80:
            end -= 1
        if end == start:
            raise IntegrityError("required-read part size cannot contain a UTF-8 character")
        ranges.append((start, end))
        start = end
    return ranges


def _active_override(state, now=None):
    override = state.get("override")
    if not isinstance(override, dict):
        return None
    if (override.get("generation") != state.get("generation") or
            not isinstance(override.get("expires_at"), (int, float)) or
            override["expires_at"] <= (time.time() if now is None else now)):
        return None
    return override


def _receipt_parts(state, entry, required, digest, part_count):
    values = set()
    stale = False
    for receipt in state["receipts"]:
        if receipt.get("name") != entry.name:
            continue
        if receipt.get("generation") != state["generation"]:
            continue
        if receipt.get("path") != str(entry.path):
            stale = True
            continue
        if receipt.get("list_sha256") != required.digest or receipt.get("sha256") != digest:
            stale = True
            continue
        if receipt.get("part_count") != part_count:
            stale = True
            continue
        part = receipt.get("part")
        if isinstance(part, int) and 1 <= part <= part_count:
            values.add(part)
    return values, stale


def _entry_state(core, entry, required, state, harness):
    try:
        snapshot = _snapshot(entry.path, DOCUMENT_LIMIT, f"required file {entry.name}")
        snapshot.data.decode("utf-8")
    except IntegrityError as error:
        return {"name": entry.name, "path": str(entry.path), "roles": list(entry.roles),
                "state": "integrity_error", "detail": str(error), "parts": []}
    except UnicodeError:
        return {"name": entry.name, "path": str(entry.path), "roles": list(entry.roles),
                "state": "integrity_error", "detail": f"required file {entry.name} is not valid UTF-8",
                "parts": []}
    ranges = _part_ranges(snapshot.data, PART_SIZE[harness])
    receipts, stale = _receipt_parts(state, entry, required, snapshot.sha256, len(ranges))
    parts = [{"part": index, "state": "current" if index in receipts else "pending"}
             for index in range(1, len(ranges) + 1)]
    if len(receipts) == len(ranges):
        status = "current"
    elif stale:
        status = "stale"
    else:
        status = "pending"
    return {"name": entry.name, "path": str(entry.path), "roles": list(entry.roles),
            "state": status, "sha256": snapshot.sha256, "byte_count": len(snapshot.data),
            "parts": parts}


def status(core, root, gitdir, harness, session, role):
    try:
        required = load_list(gitdir)
    except IntegrityError as error:
        return {
            "registered": True,
            "ok": False,
            "list_path": None,
            "registration_path": str(registration_path(gitdir)),
            "list_digest_matches": False,
            "integrity_errors": [str(error)],
            "generation": None,
            "harness": harness,
            "session_id": session,
            "role": role,
            "entries": [],
            "pending_count": 1,
            "pending_read_count": 0,
            "gate_open": False,
            "override": None,
            "next_command": shlex.join([
                "pattern-buffer", "--cwd", str(root), "required-reads", "status",
                "--harness", harness, "--session", session,
            ]),
        }
    if required is None:
        return None
    _path, state = _current_state(core, gitdir, harness, session, create=False)
    return _status_from_state(core, root, required, state, harness, session, role)


def _status_from_state(core, root, required, state, harness, session, role):
    entries = []
    for entry in required.entries:
        if not entry.applies_to(role):
            entries.append({"name": entry.name, "path": str(entry.path), "roles": list(entry.roles),
                            "state": "not_applicable", "parts": []})
        else:
            entries.append(_entry_state(core, entry, required, state, harness))
    missing = [item for item in entries if item["state"] in ("pending", "stale", "integrity_error")]
    pending_reads = sum(
        1 if item["state"] == "integrity_error" else
        sum(part["state"] != "current" for part in item["parts"])
        for item in entries if item["state"] != "not_applicable")
    next_command = None
    for item in entries:
        if item["state"] in ("pending", "stale"):
            part = next((part["part"] for part in item["parts"] if part["state"] == "pending"), 1)
            argv = ["pattern-buffer", "--cwd", str(root), "read-required", "--harness", harness,
                    "--session", session, "--name", item["name"]]
            if len(item["parts"]) > 1 or part > 1:
                argv.extend(("--part", str(part)))
            next_command = shlex.join(argv)
            break
    override = _active_override(state)
    integrity_errors = [item.get("detail", "required file integrity check failed")
                        for item in entries if item["state"] == "integrity_error"]
    return {"registered": True, "ok": not integrity_errors,
            "integrity_errors": integrity_errors,
            "list_path": str(required.list_path),
            "list_sha256": required.digest, "list_digest_matches": True,
            "generation": state["generation"], "harness": harness, "session_id": session,
            "role": role, "entries": entries, "pending_count": len(missing),
            "pending_read_count": pending_reads,
            "gate_open": not missing or override is not None,
            "override": ({"active": True, "reason": override.get("reason"),
                          "expires_at": override.get("expires_at")}
                         if override is not None else None),
            "next_command": next_command}


def resolve_session(harness, explicit=None, environ=None):
    environ = os.environ if environ is None else environ
    matching = (("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID") if harness == "claude"
                else ("CODEX_THREAD_ID",))
    other = set(OWNER_SESSION_VARIABLES) - set(matching)
    active_other = [name for name in other if name in environ]
    native_values = [environ[name] for name in matching if name in environ and environ[name]]
    if len(set(native_values)) > 1:
        raise ValueError("conflicting native session IDs are present in the environment")
    if active_other:
        raise ValueError("native caller harness does not match --harness")
    native = native_values[0] if native_values else None
    if explicit is not None and not explicit:
        raise ValueError("--session must not be empty")
    if explicit is not None and native and explicit != native:
        raise ValueError("--session conflicts with the current native harness session")
    session = explicit if explicit is not None else native
    if not isinstance(session, str) or not session or len(session) > 512:
        raise ValueError("no native session ID resolved; pass --session when running outside a harness")
    return session


def _allowed_command_parser():
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--cwd")
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("read-required", add_help=False, allow_abbrev=False)
    read.add_argument("--harness", choices=("claude", "codex"), required=True)
    read.add_argument("--session")
    choice = read.add_mutually_exclusive_group(required=True)
    choice.add_argument("--name")
    choice.add_argument("--next", action="store_true", dest="next_entry")
    read.add_argument("--part", type=int)
    required = commands.add_parser("required-reads", add_help=False, allow_abbrev=False)
    required_actions = required.add_subparsers(dest="required_action", required=True)
    required_status = required_actions.add_parser("status", add_help=False, allow_abbrev=False)
    required_status.add_argument("--harness", choices=("claude", "codex"), required=True)
    required_status.add_argument("--session", required=True)
    status = commands.add_parser("status", add_help=False, allow_abbrev=False)
    status.add_argument("--harness", choices=("claude", "codex"))
    status.add_argument("--session")
    validate = commands.add_parser("validate", add_help=False, allow_abbrev=False)
    validate.add_argument("--harness", choices=("claude", "codex"))
    validate.add_argument("--session")
    doctor = commands.add_parser("doctor", add_help=False, allow_abbrev=False)
    doctor.add_argument("--harness", choices=("claude", "codex"), default="codex")
    doctor.add_argument("--session", default="validation")
    doctor.add_argument("--settings")
    guide = commands.add_parser("guide", add_help=False, allow_abbrev=False)
    guide.add_argument("--print", action="store_true")
    return parser


def _parse_allowed_cli_command(command, root, harness, session, core):
    if not isinstance(command, str) or not command or any(ord(char) < 32 for char in command):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens:
        return None
    shell_metacharacters = set("$`;&|<>()*?[]~")
    if any(any(character in shell_metacharacters for character in token) for token in tokens):
        return None
    executable = tokens[0]
    if executable == "pattern-buffer":
        arguments = tokens[1:]
    elif Path(executable).is_absolute() and Path(executable).name == "pattern-buffer":
        arguments = tokens[1:]
    elif (Path(executable).name in ("python", "python3") and len(tokens) >= 2
          and Path(tokens[1]).is_absolute()
          and Path(tokens[1]).resolve() == Path(core.__file__).resolve()):
        arguments = tokens[2:]
    else:
        return None
    try:
        parsed = _allowed_command_parser().parse_args(arguments)
    except SystemExit:
        return None
    if parsed.cwd is not None:
        cwd = Path(parsed.cwd).expanduser()
        if not cwd.is_absolute() or not cwd.resolve().is_relative_to(Path(root).resolve()):
            return None
    if parsed.command == "guide":
        return parsed
    selected_harness = getattr(parsed, "harness", None)
    if selected_harness is not None and selected_harness != harness:
        return None
    selected_session = getattr(parsed, "session", None)
    if selected_session is not None and selected_session != session:
        return None
    return parsed


def allowed_cli_command(command, root, harness, session, core):
    parsed = _parse_allowed_cli_command(command, root, harness, session, core)
    if parsed is None:
        return False
    if parsed.command == "required-reads":
        return parsed.required_action == "status"
    if parsed.command == "read-required":
        return not parsed.next_entry or parsed.part is None
    return parsed.command in ("status", "validate", "doctor", "guide")


def allowed_diagnostic_cli_command(command, root, harness, session, core):
    parsed = _parse_allowed_cli_command(command, root, harness, session, core)
    if parsed is None:
        return False
    if parsed.command == "required-reads":
        return parsed.required_action == "status"
    return parsed.command in ("status", "validate", "doctor", "guide")


def _hook_result(message, decision=None, warning=False):
    output = {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}
    if decision == "deny":
        output["hookSpecificOutput"].update(permissionDecision="deny",
                                            permissionDecisionReason=message)
    elif warning:
        output["systemMessage"] = message
        output["hookSpecificOutput"]["additionalContext"] = message
    elif message:
        output["hookSpecificOutput"]["additionalContext"] = message
    return output


def _deny(core, root, harness, session, message, missing=(), result="pending"):
    core.record_event(root, "required_reads.gate_denied", harness=harness, session=session,
                      result=result, details={"missing_entries": list(missing)})
    return _hook_result(message, decision="deny")


def _override_notice(core, gitdir, harness, session):
    path = state_path(gitdir, harness, session)
    with _state_lock(core, path):
        state = _read_state(path, harness, session)
        override_state = _active_override(state)
        if override_state is None or override_state.get("notice_emitted"):
            return None
        override_state["notice_emitted"] = True
        _write_state(core, path, state)
        return dict(override_state)


def _mark_gate_open(core, root, gitdir, harness, session):
    path = state_path(gitdir, harness, session)
    opened = False
    generation = None
    with _state_lock(core, path):
        state = _read_state(path, harness, session)
        generation = state["generation"]
        if generation > 0 and state.get("gate_opened_at") is None:
            state["gate_opened_at"] = time.time()
            _write_state(core, path, state)
            opened = True
    if opened:
        core.record_event(root, "required_reads.gate_opened", harness=harness, session=session,
                          result="receipts_complete", details={"generation": generation})


def pretool_gate(core, root, gitdir, harness, payload):
    if payload.get("agent_id"):
        return None
    session = payload.get("session_id")
    try:
        tool = payload.get("tool_name")
        command = (payload.get("tool_input", {}).get("command")
                   if isinstance(payload.get("tool_input"), dict) else None)
        if (tool == "Bash" and allowed_diagnostic_cli_command(
                command, root, harness, session, core)):
            return None
        required = load_list(gitdir)
        if required is None:
            return None
        if not isinstance(session, str) or not session or len(session) > 512:
            return _deny(core, root, harness, session,
                         "Required reads cannot identify this native session because its native session ID "
                         "is missing or malformed. Retry from the active harness session. If the ID remains "
                         "unavailable, run `pattern-buffer guide --print` and notify the worktree owner. "
                         "The gate remains closed.",
                         result="identity_missing")
        try:
            session_start(core, root, gitdir, harness, session, "pretool_gate")
        except (OSError, ValueError, TypeError, UnicodeError) as error:
            return _deny(core, root, harness, session,
                         "Required-read session state could not be initialized. "
                         f"Reason: {type(error).__name__}. "
                         "Run `pattern-buffer guide --print` and notify the worktree owner. "
                         "The gate remains closed.", result="state_initialization_failed")
        try:
            config, _source = core.load(root, gitdir)
            role, _origin, _saved = core.resolve_role(root, gitdir, config, harness, session)
        except (OSError, ValueError, TypeError, UnicodeError):
            role = None
        report = status(core, root, gitdir, harness, session, role)
        integrity = [entry for entry in report["entries"] if entry["state"] == "integrity_error"]
        if integrity:
            details = "; ".join(f"{entry['name']}: {entry.get('detail', 'integrity check failed')}"
                                 for entry in integrity)
            names = [entry["name"] for entry in report["entries"]
                     if entry["state"] in {"pending", "stale", "integrity_error"}]
            status_command = shlex.join(["pattern-buffer", "--cwd", str(root), "required-reads", "status",
                                         "--harness", harness, "--session", session])
            message = (f"Required-read integrity failure. {details}. "
                       "Unread entries: " + ", ".join(names) + ". "
                       f"Run: {status_command}. Notify the worktree owner. The gate remains closed.")
            return _deny(core, root, harness, session, message,
                         missing=names, result="integrity_failure")
        override_state = report["override"]
        if override_state is not None:
            notice = _override_notice(core, gitdir, harness, session)
            if notice:
                reason = notice.get("reason", "Owner override")
                expires = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(notice["expires_at"]))
                return _hook_result(f"Required reads are open by owner override until {expires}. Reason: {reason}.")
            return None
        if report["pending_count"] == 0:
            _mark_gate_open(core, root, gitdir, harness, session)
            return None
        if harness == "claude" and tool in {"Read", "Glob", "Grep"}:
            return None
        if tool == "Bash" and allowed_cli_command(command, root, harness, session, core):
            parsed = _parse_allowed_cli_command(command, root, harness, session, core)
            if parsed is not None and parsed.command == "read-required":
                try:
                    prepared = prepare_pending_read(core, root, gitdir, harness, payload,
                                                    parsed, command, role)
                except (IntegrityError, ValueError) as error:
                    return _deny(core, root, harness, session,
                                 f"Required read could not be bound because {error}. "
                                 "Run `pattern-buffer guide --print` for recovery steps, then retry "
                                 "from the same harness session. Notify the worktree owner if it still fails. "
                                 "The gate remains closed.", result="binding_failure")
                if not prepared:
                    if load_list(gitdir) is None:
                        return None
                    return _deny(core, root, harness, session,
                                 "Required read could not be bound because Pattern Buffer did not save "
                                 "a pending snapshot for this native tool call. Retry the read in the same "
                                 "harness session, then run `pattern-buffer guide --print` for recovery steps. "
                                 "Notify the worktree owner if it still fails. The gate remains closed.",
                                 result="snapshot_unavailable")
            return None
        names = [entry["name"] for entry in report["entries"]
                 if entry["state"] in {"pending", "stale", "integrity_error"}]
        next_command = report["next_command"]
        if next_command is None:
            next_command = shlex.join(["pattern-buffer", "--cwd", str(root), "doctor",
                                       "--harness", harness, "--session", session])
        command_note = f"Next run: {next_command}."
        if harness == "codex":
            command_note += " If the shell output is truncated, retry with an 8000-token output allowance."
        message = ("Required reads are pending for this context window: " + ", ".join(names) + ". "
                   + command_note + " Read the Required reads section in the Pattern Buffer guide.")
        message += " Print the guide with `pattern-buffer guide --print`."
        return _deny(core, root, harness, session, message, missing=names)
    except IntegrityError as error:
        message = f"Required-read integrity failure: {error}. Notify the worktree owner. The gate remains closed."
        return _deny(core, root, harness, session, message, result="integrity_failure")
    except Exception as error:
        message = ("Pattern Buffer required-reads gate encountered an internal error and allowed this tool call. "
                   f"Notify the worktree owner. Error: {type(error).__name__}.")
        core.record_event(root, "required_reads.gate_error", harness=harness, session=session,
                          result="allowed_with_warning", details={"error_code": type(error).__name__})
        return _hook_result(message, warning=True)


def session_start_notice(core, root, gitdir, harness, session, agent=""):
    if agent or not session:
        return ""
    try:
        required = load_list(gitdir)
        if required is None:
            return ""
        config, _source = core.load(root, gitdir)
        role, _origin, _saved = core.resolve_role(root, gitdir, config, harness, session)
        report = status(core, root, gitdir, harness, session, role)
    except (OSError, ValueError, TypeError, UnicodeError, IntegrityError):
        return ""
    count = report["pending_read_count"]
    if count <= 0:
        return ""
    return (f"Required reads: {count} reads pending. Next run: "
            f"pattern-buffer read-required --harness {harness} --next.")


def _require_owner_terminal():
    present = [name for name in OWNER_SESSION_VARIABLES if name in os.environ]
    if present:
        raise ValueError("only the worktree owner can change required reads from a terminal outside a harness session")
    if not sys.stdin.isatty():
        raise ValueError("only the worktree owner can change required reads from an interactive terminal")


def _confirm(prompt):
    return input(prompt)


def set_list(core, root, gitdir, file_path, confirm=None):
    _require_owner_terminal()
    source_path = _home_path(file_path, "required-reads list")
    source = _snapshot(source_path, LIST_LIMIT, "required-reads list")
    entries = _entries(source.data, source_path)
    _check_entry_files(entries)
    if (confirm or _confirm)(f"Type {Path(root).name} to confirm this worktree: ") != Path(root).name:
        raise ValueError("worktree confirmation did not match; registration was unchanged")
    path = registration_path(gitdir)
    with core.locked(path.with_suffix(".lock")):
        latest = _snapshot(source_path, LIST_LIMIT, "required-reads list")
        if latest.identity != source.identity or latest.sha256 != source.sha256:
            raise ValueError("required-reads list changed during confirmation; registration was unchanged")
        entries = _entries(latest.data, source_path)
        _check_entry_files(entries)
        registration = {"schema_version": 1, "path": str(source_path),
                        "sha256": latest.sha256, "updated_at": time.time()}
        core.atomic(path, json.dumps(registration, sort_keys=True, separators=(",", ":")))
    core.record_event(root, "required_reads.list_changed", result="set",
                      details={"path": str(source_path), "sha256": source.sha256,
                               "entry_count": len(entries)})
    return {"registered": True, "path": str(source_path), "sha256": source.sha256,
            "entry_count": len(entries)}


def clear_list(core, root, gitdir, confirm=None):
    _require_owner_terminal()
    if (confirm or _confirm)(f"Type {Path(root).name} to confirm this worktree: ") != Path(root).name:
        raise ValueError("worktree confirmation did not match; registration was unchanged")
    path = registration_path(gitdir)
    with core.locked(path.with_suffix(".lock")):
        if path.is_symlink():
            raise IntegrityError("required-reads registration is unsafe")
        if path.exists():
            path.unlink()
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    core.record_event(root, "required_reads.list_changed", result="clear")
    return {"registered": False, "cleared": True}


def override(core, root, gitdir, harness, session, reason, minutes, confirm=None):
    _require_owner_terminal()
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 60:
        raise ValueError("--minutes must be a whole number from 1 to 60")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > REASON_LIMIT or "\x00" in reason:
        raise ValueError(f"--reason must contain 1 to {REASON_LIMIT} characters")
    required = load_list(gitdir)
    if required is None:
        raise ValueError("no required-reads list is registered for this worktree")
    if (confirm or _confirm)(f"Type target session ID {session} to confirm: ") != session:
        raise ValueError("session confirmation did not match; override was not applied")
    path = state_path(gitdir, harness, session)
    with _state_lock(core, path):
        state = _read_state(path, harness, session)
        if state["generation"] < 1:
            raise ValueError("the target session has no current context-window generation")
        created = time.time()
        state["override"] = {"generation": state["generation"], "reason": reason.strip(),
                              "created_at": created, "expires_at": created + minutes * 60}
        core.atomic(path, json.dumps(state, sort_keys=True, separators=(",", ":")))
    core.record_event(root, "required_reads.override_used", harness=harness, session=session,
                      result="opened", details={"reason": reason.strip(), "minutes": minutes,
                                                "generation": state["generation"],
                                                "expires_at": state["override"]["expires_at"]})
    core.record_event(root, "required_reads.gate_opened", harness=harness, session=session,
                      result="override", details={"generation": state["generation"]})
    return {"harness": harness, "session_id": session, "generation": state["generation"],
            "override": {"active": True, "reason": state["override"]["reason"],
                         "expires_at": state["override"]["expires_at"]}}


def _same_list_snapshot(before, after):
    return (before.registration_path == after.registration_path
            and before.list_path == after.list_path and before.digest == after.digest
            and before.registration_identity == after.registration_identity
            and before.list_identity == after.list_identity)


def _select_read(core, root, gitdir, harness, session, role, required, state,
                 name=None, part=None, next_entry=False):
    if next_entry:
        report = _status_from_state(core, root, required, state, harness, session, role)
        for item in report["entries"]:
            if item["state"] in ("pending", "stale"):
                name = item["name"]
                if part is None:
                    part = next((row["part"] for row in item["parts"] if row["state"] == "pending"), 1)
                break
        else:
            raise ValueError("all role-applicable required reads are current")
    if not isinstance(name, str) or not name:
        raise ValueError("--name is required unless --next is used")
    entry = next((item for item in required.entries if item.name == name), None)
    if entry is None:
        raise ValueError(f"required-read entry was not found: {name}")
    if not entry.applies_to(role):
        raise ValueError(f"required-read entry does not apply to the session role: {name}")
    snapshot = _snapshot(entry.path, DOCUMENT_LIMIT, f"required file {entry.name}")
    try:
        snapshot.data.decode("utf-8")
    except UnicodeError as error:
        raise IntegrityError(f"required file {entry.name} is not valid UTF-8: {entry.path}") from error
    ranges = _part_ranges(snapshot.data, PART_SIZE[harness])
    if part is None:
        part = 1
    if not isinstance(part, int) or isinstance(part, bool) or not 1 <= part <= len(ranges):
        raise ValueError(f"--part must be from 1 to {len(ranges)} for {name}")
    offset, end = ranges[part - 1]
    return ReadSelection(entry, snapshot, part, len(ranges), snapshot.data[offset:end])


def _read_frame(selection):
    header = (f"--- Pattern Buffer required read: {selection.entry.name} "
              f"(part {selection.part} of {selection.part_count}, "
              f"{len(selection.piece)} bytes) ---\n").encode("utf-8")
    footer = f"\n--- End required read: sha256={selection.snapshot.sha256} ---\n".encode("utf-8")
    return header + selection.piece + footer


def _write_frame(stream, frame):
    try:
        stream.write(frame)
    except TypeError:
        stream.write(frame.decode("utf-8"))
    stream.flush()


def read_required(core, root, gitdir, harness, session, name=None, part=None, next_entry=False,
                  role=None, output=None):
    required = load_list(gitdir)
    if required is None:
        raise ValueError("no required-reads list is registered for this worktree")
    state = _read_state(state_path(gitdir, harness, session), harness, session)
    selection = _select_read(core, root, gitdir, harness, session, role, required, state,
                             name=name, part=part, next_entry=next_entry)
    stream = output or getattr(sys.stdout, "buffer", sys.stdout)
    _write_frame(stream, _read_frame(selection))


def _read_command_snapshot(core, root, gitdir, harness, session, role, parsed, tool_use_id,
                           command):
    if (not isinstance(tool_use_id, str) or not tool_use_id or len(tool_use_id) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in tool_use_id)):
        raise ValueError("the native tool call ID is missing or malformed")
    state_file = state_path(gitdir, harness, session)
    with _state_lock(core, state_file):
        state = _read_state(state_file, harness, session)
        if state["generation"] < 1:
            raise ValueError("the current session has no active required-read generation")
        registration_file = registration_path(gitdir)
        with core.locked(registration_file.with_suffix(".lock")):
            required = load_list(gitdir)
            if required is None:
                return None
            selection = _select_read(
                core, root, gitdir, harness, session, role, required, state,
                name=parsed.name, part=parsed.part, next_entry=parsed.next_entry)
            latest_required = load_list(gitdir)
            latest_file = _snapshot(selection.entry.path, DOCUMENT_LIMIT,
                                    f"required file {selection.entry.name}")
            if (latest_required is None or not _same_list_snapshot(required, latest_required)
                    or latest_file.identity != selection.snapshot.identity
                    or latest_file.sha256 != selection.snapshot.sha256):
                raise IntegrityError("required-read inputs changed before the read command")
            snapshot = {
                "tool_use_id": tool_use_id,
                "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
                "generation": state["generation"],
                "role": role,
                "list_sha256": required.digest,
                "registration_identity": list(required.registration_identity),
                "list_identity": list(required.list_identity),
                "name": selection.entry.name,
                "path": str(selection.entry.path),
                "part": selection.part,
                "part_count": selection.part_count,
                "file_sha256": selection.snapshot.sha256,
                "file_identity": list(selection.snapshot.identity),
                "file_byte_count": len(selection.snapshot.data),
                "byte_count": len(selection.piece),
                "prepared_at": time.time(),
            }
            pending = [item for item in state["pending_reads"]
                       if item.get("tool_use_id") != tool_use_id]
            pending.append(snapshot)
            state["pending_reads"] = pending[-PENDING_READ_LIMIT:]
            _write_state(core, state_file, state)
    return snapshot


def prepare_pending_read(core, root, gitdir, harness, payload, parsed, command, role):
    if payload.get("agent_id"):
        return False
    session = payload.get("session_id")
    if not isinstance(session, str) or not session or len(session) > 512:
        return False
    return _read_command_snapshot(core, root, gitdir, harness, session, role, parsed,
                                   payload.get("tool_use_id"), command) is not None


def _response_stdout(harness, payload):
    response = payload.get("tool_response")
    if harness == "codex" and isinstance(response, str):
        try:
            return response.encode("utf-8"), False
        except UnicodeError:
            return None, False
    if harness != "claude" or not isinstance(response, dict):
        return None, False
    stdout = response.get("stdout")
    if not isinstance(stdout, str):
        return None, True
    stderr = response.get("stderr", "")
    if not isinstance(stderr, str) or stderr:
        return None, True
    for flag in ("interrupted", "isImage", "noOutputExpected"):
        value = response.get(flag, False)
        if not isinstance(value, bool) or value:
            return None, True
    try:
        return stdout.encode("utf-8"), True
    except UnicodeError:
        return None, True


def _pending_matches_state(snapshot, required, selection, state):
    return (snapshot.get("generation") == state["generation"]
            and snapshot.get("list_sha256") == required.digest
            and snapshot.get("registration_identity") == list(required.registration_identity)
            and snapshot.get("list_identity") == list(required.list_identity)
            and snapshot.get("name") == selection.entry.name
            and snapshot.get("path") == str(selection.entry.path)
            and snapshot.get("part") == selection.part
            and snapshot.get("part_count") == selection.part_count
            and snapshot.get("file_sha256") == selection.snapshot.sha256
            and snapshot.get("file_identity") == list(selection.snapshot.identity)
            and snapshot.get("file_byte_count") == len(selection.snapshot.data)
            and snapshot.get("byte_count") == len(selection.piece))


def posttool_finalize(core, root, gitdir, harness, payload):
    if payload.get("agent_id"):
        return False
    session = payload.get("session_id")
    tool_use_id = payload.get("tool_use_id")
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if (not isinstance(session, str) or not session or len(session) > 512
            or not isinstance(tool_use_id, str) or not tool_use_id or len(tool_use_id) > 512
            or payload.get("tool_name") != "Bash" or not isinstance(command, str)):
        return False
    parsed = _parse_allowed_cli_command(command, root, harness, session, core)
    if parsed is None or parsed.command != "read-required":
        return False
    if load_list(gitdir) is None:
        return False
    state_file = state_path(gitdir, harness, session)
    if not state_file.exists() and not state_file.is_symlink():
        return False
    command_digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
    stdout, normalize_footer = _response_stdout(harness, payload)
    if not isinstance(stdout, bytes):
        stdout = None
    opened = False
    with _state_lock(core, state_file):
        current = _read_state(state_file, harness, session)
        pending = next((item for item in current["pending_reads"]
                        if item.get("tool_use_id") == tool_use_id), None)
        if pending is None or pending.get("command_sha256") != command_digest:
            return False
        current["pending_reads"] = [item for item in current["pending_reads"]
                                    if item.get("tool_use_id") != tool_use_id]
        success = False
        required = None
        selection = None
        if stdout is not None:
            registration_file = registration_path(gitdir)
            with core.locked(registration_file.with_suffix(".lock")):
                try:
                    required = load_list(gitdir)
                    if (required is not None
                            and pending.get("generation") == current["generation"]
                            and pending.get("list_sha256") == required.digest
                            and pending.get("registration_identity") == list(required.registration_identity)
                            and pending.get("list_identity") == list(required.list_identity)):
                        entry = next((item for item in required.entries
                                      if item.name == pending.get("name")
                                      and str(item.path) == pending.get("path")), None)
                        if entry is not None:
                            snapshot = _snapshot(entry.path, DOCUMENT_LIMIT,
                                                 f"required file {entry.name}")
                            snapshot.data.decode("utf-8")
                            ranges = _part_ranges(snapshot.data, PART_SIZE[harness])
                            part = pending.get("part")
                            if isinstance(part, int) and not isinstance(part, bool) and 1 <= part <= len(ranges):
                                offset, end = ranges[part - 1]
                                selection = ReadSelection(entry, snapshot, part, len(ranges),
                                                          snapshot.data[offset:end])
                                frame = _read_frame(selection)
                                success = (_pending_matches_state(pending, required, selection, current)
                                           and (stdout == frame or
                                                normalize_footer and frame.endswith(b"\n")
                                                and stdout == frame[:-1]))
                except (IntegrityError, UnicodeError, OSError, ValueError):
                    success = False
        if success and required is not None and selection is not None:
            generation = current["generation"]
            receipt = {"generation": generation, "list_sha256": required.digest,
                       "name": selection.entry.name, "path": str(selection.entry.path),
                       "sha256": selection.snapshot.sha256,
                       "file_identity": list(selection.snapshot.identity),
                       "file_byte_count": len(selection.snapshot.data),
                       "byte_count": len(selection.piece), "part": selection.part,
                       "part_count": selection.part_count, "recorded_at": time.time()}
            current["list_sha256"] = required.digest
            current["receipts"] = [item for item in current["receipts"]
                                   if not (item.get("name") == selection.entry.name
                                           and item.get("part") == selection.part)]
            current["receipts"].append(receipt)
            current["receipts"].sort(key=lambda item: (item.get("name", ""), item.get("part", 0)))
            report = _status_from_state(core, root, required, current, harness, session,
                                        role=pending.get("role"))
            if report["pending_count"] == 0 and current.get("gate_opened_at") is None:
                current["gate_opened_at"] = time.time()
                opened = True
        _write_state(core, state_file, current)
    if not success or selection is None:
        return False
    core.record_event(root, "required_reads.receipt_recorded", harness=harness, session=session,
                      result="recorded", details={"name": selection.entry.name,
                                                   "sha256": selection.snapshot.sha256,
                                                   "byte_count": len(selection.piece),
                                                   "part": selection.part,
                                                   "part_count": selection.part_count,
                                                   "generation": generation})
    if opened:
        core.record_event(root, "required_reads.gate_opened", harness=harness, session=session,
                          result="receipts_complete", details={"generation": generation})
    return True
