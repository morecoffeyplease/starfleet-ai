"""Durable, session-named standing context enrollment for Pattern Buffer."""

from contextlib import contextmanager
from dataclasses import dataclass
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid


VERSION = 1
SESSION_FILE_LIMIT = 64 * 1024
REGISTRY_LIMIT = 2 * 1024 * 1024
MAX_ENROLLMENTS = 1024
MAX_BINDINGS = 128
MAX_EVENTS = 2048
MAX_EVIDENCE_BYTES = 1024
LOCK_TIMEOUT_SECONDS = 5
HARNESS_NAMES = ("claude", "codex", "opencode")
ROLE_NAMES = ("implementer", "orchestrator")

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_SAFE_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_OPENCODE_SESSION = re.compile(r"ses_[A-Za-z0-9]+\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_META_PARTS = ("context-injector", "session-enrollments")


@dataclass(frozen=True)
class Binding:
    enrollment_id: str
    root: str
    gitdir: str
    harness: str
    session_id: str
    role: str
    state: str
    path: str
    source_file: str | None
    source_sha256: str | None
    source_bytes: int | None
    file_status: str
    history: tuple[dict, ...]
    predecessor: dict | None = None
    successor: dict | None = None

    @property
    def expected_path(self):
        return self.path


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _lexists(path):
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False


def _check_path(path, *, missing_final=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("path must be absolute and traversal-free")
    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:], 1):
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if missing_final and index == len(path.parts) - 1:
                return
            raise ValueError(f"path component is missing: {current}")
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"path component must not be a symlink: {current}")
        if index < len(path.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"path parent must be a directory: {current}")
    return path


def _ensure_dirs(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("directory path must be absolute and traversal-free")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"directory component is unsafe: {current}")
    return path


def _read_file(path, limit, *, missing_ok=False):
    path = Path(path)
    try:
        _check_path(path, missing_final=missing_ok)
    except ValueError as error:
        if missing_ok and "path component is missing" in str(error):
            return None
        raise
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ValueError(f"file is missing: {path}")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"expected a regular file no larger than {limit} bytes: {path}")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(16 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise ValueError(f"file exceeds {limit} bytes: {path}")
        return bytes(data)
    finally:
        os.close(fd)


def _atomic_write(path, data):
    path = Path(path)
    _ensure_dirs(path.parent)
    _check_path(path, missing_final=True)
    fd, temp_name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _check_path(path, missing_final=True)
        try:
            current = os.lstat(path)
        except FileNotFoundError:
            current = None
        if current is not None and (stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)):
            raise ValueError(f"refusing to replace a non-regular file: {path}")
        os.replace(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _canonical_dir(value, label):
    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError, OSError) as error:
        raise ValueError(f"{label} is not a usable path") from error
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be an absolute canonical path")
    _check_path(path)
    try:
        if path.resolve(strict=True) != path or not path.is_dir():
            raise ValueError(f"{label} must be a real canonical directory")
    except OSError as error:
        raise ValueError(f"{label} is unavailable") from error
    return path


def _root_gitdir(root, gitdir):
    root = _canonical_dir(root, "worktree root")
    gitdir = _canonical_dir(gitdir, "Git directory")
    marker = root / ".git"
    try:
        info = os.lstat(marker)
    except OSError as error:
        raise ValueError("worktree root has no safe Git marker") from error
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("worktree Git marker must not be a symlink")
    if stat.S_ISDIR(info.st_mode):
        observed = marker.resolve(strict=True)
    elif stat.S_ISREG(info.st_mode):
        data = _read_file(marker, 4096)
        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeError as error:
            raise ValueError("worktree Git pointer is invalid") from error
        if len(lines) != 1 or not lines[0].startswith("gitdir: ") or not lines[0][8:]:
            raise ValueError("worktree Git pointer is invalid")
        pointer = Path(lines[0][8:])
        if not pointer.is_absolute():
            pointer = root / pointer
        observed = pointer.resolve(strict=True)
    else:
        raise ValueError("worktree Git marker must be a directory or regular file")
    if observed != gitdir:
        raise ValueError("Git directory does not belong to this exact worktree root")
    return root, gitdir


def _normalize_session(value, *, strict):
    message = "native session identifier must be a UUID or a safe opaque token"
    if not isinstance(value, str) or not value or len(value) > 128:
        if strict:
            raise ValueError(message)
        return None
    if _UUID.fullmatch(value):
        return value.lower()
    if _SAFE_SESSION.fullmatch(value) and ".." not in value:
        return value
    if strict:
        raise ValueError(message)
    return None


def _harness(value):
    if not isinstance(value, str) or value.casefold() not in HARNESS_NAMES:
        raise ValueError("harness must be claude, codex or opencode")
    return value.casefold()


def _role(value):
    if value not in ROLE_NAMES:
        raise ValueError("role must be implementer or orchestrator")
    return value


def _evidence(value):
    if not isinstance(value, str):
        raise ValueError("explicit handoff evidence is required")
    value = value.strip()
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise ValueError("handoff evidence must be valid UTF-8") from error
    if (not value or "\x00" in value or len(encoded) > MAX_EVIDENCE_BYTES
            or any(ord(char) < 32 and char not in "\n\t" for char in value)):
        raise ValueError(f"handoff evidence must be 1 to {MAX_EVIDENCE_BYTES} safe UTF-8 bytes")
    return value


def _identity(root, harness, session_id):
    harness = _harness(harness)
    session_id = _normalize_session(session_id, strict=True)
    if harness == "opencode" and not _OPENCODE_SESSION.fullmatch(session_id):
        raise ValueError("OpenCode requires the exact case-sensitive ses_ session ID")
    return {"root": str(root), "harness": harness, "session_id": session_id}


def _key(identity):
    return identity["harness"], identity["session_id"]


def _session_path(root, harness, session_id):
    return root / ".context-injector" / "sessions" / f"{harness}-{session_id}.md"


def _meta_path(gitdir):
    return Path(gitdir).joinpath(*_META_PARTS)


def _empty_registry(root, gitdir):
    return {"version": VERSION, "root": str(root), "gitdir": str(gitdir), "enrollments": {}}


def _valid_identity(value, root):
    if not isinstance(value, dict) or value.get("root") != str(root):
        raise ValueError("enrollment identity belongs to another worktree")
    harness = _harness(value.get("harness"))
    session_id = _normalize_session(value.get("session_id"), strict=True)
    if value.get("harness") != harness or value.get("session_id") != session_id:
        raise ValueError("enrollment identity is not canonical")
    return _identity(root, harness, session_id)


def _validate_registry(value, root, gitdir):
    if (not isinstance(value, dict) or value.get("version") != VERSION
            or value.get("root") != str(root) or value.get("gitdir") != str(gitdir)):
        raise ValueError("session enrollment registry is corrupt or bound to another worktree")
    records = value.get("enrollments")
    if not isinstance(records, dict) or len(records) > MAX_ENROLLMENTS:
        raise ValueError("session enrollment registry is invalid")
    seen, pending = set(), set()
    for enrollment_id, record in records.items():
        try:
            valid_id = str(uuid.UUID(enrollment_id))
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError("session enrollment registry has an invalid ID") from error
        if valid_id != enrollment_id or not isinstance(record, dict):
            raise ValueError("session enrollment registry has a noncanonical record")
        bindings, events = record.get("bindings"), record.get("events")
        if (record.get("enrollment_id") != enrollment_id or not isinstance(bindings, list)
                or not 1 <= len(bindings) <= MAX_BINDINGS
                or not isinstance(events, list) or len(events) > MAX_EVENTS):
            raise ValueError("session enrollment history is invalid")
        active = []
        for binding in bindings:
            if not isinstance(binding, dict):
                raise ValueError("session enrollment binding is invalid")
            identity = _valid_identity(binding.get("identity"), root)
            key = _key(identity)
            if key in seen:
                raise ValueError("native session identity is bound more than once")
            seen.add(key)
            digest = binding.get("source_sha256")
            size = binding.get("source_bytes")
            if (binding.get("role") not in ROLE_NAMES
                    or binding.get("state") not in ("active", "released", "retired")
                    or not isinstance(binding.get("source_file"), str)
                    or len(binding["source_file"].encode("utf-8")) > 4096
                    or "\x00" in binding["source_file"]
                    or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                    or isinstance(size, bool) or not isinstance(size, int)
                    or not 1 <= size <= SESSION_FILE_LIMIT):
                raise ValueError("session enrollment binding is invalid")
            if binding["state"] == "active":
                active.append(identity)
        owner, successor = record.get("active_identity"), record.get("pending_successor")
        if owner is None:
            if active or not isinstance(successor, dict):
                raise ValueError("released enrollment must name an explicit successor")
            key = _key(_valid_identity(successor, root))
            if key in seen or key in pending:
                raise ValueError("pending successor identity is already in use")
            pending.add(key)
        else:
            owner = _valid_identity(owner, root)
            if len(active) != 1 or active[0] != owner or successor is not None:
                raise ValueError("enrollment must have exactly one active owner")
        if any(not isinstance(event, dict) or not isinstance(event.get("action"), str)
               or not isinstance(event.get("at"), str) for event in events):
            raise ValueError("session enrollment event history is invalid")
    if seen.intersection(pending):
        raise ValueError("pending successor identity is already bound")
    return value


def _read_registry(meta):
    path = meta / "registry.json"
    data = _read_file(path, REGISTRY_LIMIT, missing_ok=True)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("session enrollment registry is corrupt") from error


def _write_registry(meta, registry):
    data = (json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > REGISTRY_LIMIT:
        raise ValueError(f"session enrollment registry exceeds {REGISTRY_LIMIT} bytes")
    _atomic_write(meta / "registry.json", data)


def _check_registry_size(registry):
    data = (json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > REGISTRY_LIMIT:
        raise ValueError(f"session enrollment registry exceeds {REGISTRY_LIMIT} bytes")


@contextmanager
def _locked(root, gitdir, *, create):
    meta = _meta_path(gitdir)
    if create:
        _ensure_dirs(meta)
    else:
        try:
            _check_path(meta)
        except ValueError as error:
            if "missing" in str(error):
                yield None, _empty_registry(root, gitdir)
                return
            raise
    lock_path = meta / "registry.lock"
    registry_path = meta / "registry.json"
    if not create and not _lexists(lock_path) and not _lexists(registry_path):
        yield meta, _empty_registry(root, gitdir)
        return
    _ensure_dirs(meta)
    _check_path(lock_path, missing_final=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("session enrollment lock is not a regular file")
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("another session enrollment operation holds the registry")
                time.sleep(0.025)
        try:
            value = _read_registry(meta)
            if value is None:
                value = _empty_registry(root, gitdir)
            else:
                _validate_registry(value, root, gitdir)
            yield meta, value
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _marker_path(meta, harness, session_id):
    key = hashlib.sha256(f"{harness}\0{session_id}".encode()).hexdigest()
    return meta / "bindings" / f"{key}.json"


def _read_marker(meta, harness, session_id):
    path = _marker_path(meta, harness, session_id)
    data = _read_file(path, 16 * 1024, missing_ok=True)
    if data is None:
        return None
    try:
        marker = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("exact-session required-binding marker is corrupt") from error
    if (not isinstance(marker, dict) or marker.get("harness") != harness
            or marker.get("session_id") != session_id or marker.get("state") not in
            ("active", "pending", "released", "retired")):
        raise ValueError("exact-session required-binding marker is invalid")
    return marker


def _write_marker(meta, enrollment_id, identity, state, role, digest=None, *,
                  predecessor=None, successor=None, release_evidence=None,
                  adoption_evidence=None, timers_safe=None):
    marker = {"enrollment_id": enrollment_id, **identity, "state": state, "role": role,
              "source_sha256": digest, "predecessor": predecessor, "successor": successor,
              "release_evidence": release_evidence, "adoption_evidence": adoption_evidence,
              "timers_safe": timers_safe}
    _atomic_write(_marker_path(meta, identity["harness"], identity["session_id"]),
                  (json.dumps(marker, sort_keys=True) + "\n").encode("utf-8"))


def _check_marker(meta, root, enrollment_id, binding):
    identity = binding["identity"]
    marker = _read_marker(meta, identity["harness"], identity["session_id"])
    if (marker is None or marker.get("root") != str(root)
            or marker.get("enrollment_id") != enrollment_id
            or marker.get("state") != binding["state"] or marker.get("role") != binding["role"]
            or marker.get("source_sha256") != binding["source_sha256"]
            or marker.get("predecessor") != binding.get("predecessor")
            or marker.get("successor") != binding.get("successor")):
        raise ValueError("exact-session required-binding marker is missing or inconsistent")
    return marker


def _find(record, harness, session_id):
    if record:
        for binding in record["bindings"]:
            identity = binding["identity"]
            if identity["harness"] == harness and identity["session_id"] == session_id:
                return binding
    return None


def _find_record(registry, harness, session_id):
    for record in registry["enrollments"].values():
        binding = _find(record, harness, session_id)
        if binding:
            return record, binding
    return None, None


def _pending_record(registry, harness, session_id):
    for record in registry["enrollments"].values():
        successor = record.get("pending_successor")
        if successor and successor["harness"] == harness and successor["session_id"] == session_id:
            return record, record["bindings"][-1]
    return None, None


def _file_status(root, binding):
    identity = binding["identity"]
    path = _session_path(root, identity["harness"], identity["session_id"])
    try:
        data = _read_file(path, SESSION_FILE_LIMIT, missing_ok=True)
    except ValueError:
        return "unsafe"
    except OSError:
        return "unreadable"
    if data is None:
        return "missing"
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return "invalid"
    if not text or "\x00" in text:
        return "invalid"
    return "valid" if _digest(data) == binding["source_sha256"] else "changed"


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _public(root, gitdir, enrollment_id, record, binding):
    identity = binding["identity"]
    return Binding(enrollment_id, str(root), str(gitdir), identity["harness"], identity["session_id"],
                   binding["role"], binding["state"],
                   str(_session_path(root, identity["harness"], identity["session_id"])),
                   binding["source_file"], binding["source_sha256"], binding["source_bytes"],
                   _file_status(root, binding), tuple(_copy(event) for event in record["events"]),
                   _copy(binding["predecessor"]) if binding.get("predecessor") else None,
                   _copy(binding["successor"]) if binding.get("successor") else None)


def _pending_binding(root, gitdir, enrollment_id, record, outgoing):
    identity = record["pending_successor"]
    return Binding(enrollment_id, str(root), str(gitdir), identity["harness"], identity["session_id"],
                   outgoing["role"], "pending",
                   str(_session_path(root, identity["harness"], identity["session_id"])),
                   None, None, None, "pending", tuple(_copy(event) for event in record["events"]),
                   _copy(outgoing["identity"]), None)


def _all_keys(registry):
    result = set()
    for record in registry["enrollments"].values():
        result.update(_key(binding["identity"]) for binding in record["bindings"])
        if record.get("pending_successor"):
            result.add(_key(record["pending_successor"]))
    return result


def _check_collision(registry, harness, session_id):
    root = Path(registry["root"])
    expected = _session_path(root, harness, session_id).name.casefold()
    for record in registry["enrollments"].values():
        identities = [binding["identity"] for binding in record["bindings"]]
        if record.get("pending_successor"):
            identities.append(record["pending_successor"])
        for identity in identities:
            path = _session_path(root, identity["harness"], identity["session_id"]).name
            if identity["harness"] == harness and path.casefold() == expected and identity["session_id"] != session_id:
                raise ValueError("session filename collides with another native identity")


def _append_event(record, action, **fields):
    if len(record["events"]) >= MAX_EVENTS:
        raise ValueError("handoff history is full; preserve it and create a new enrollment")
    record["events"].append({"event_id": str(uuid.uuid4()), "action": action, "at": _now(), **fields})
    record["updated_at"] = record["events"][-1]["at"]


def _read_session(root, harness, session_id):
    return _read_file(_session_path(root, harness, session_id), SESSION_FILE_LIMIT, missing_ok=True)


def _read_reviewable_session(root, harness, session_id):
    """Read a safe registered target for explicit repair, without following symlinks."""
    path = _session_path(root, harness, session_id)
    try:
        _check_path(path.parent)
    except ValueError as error:
        if "path component is missing" in str(error):
            return None, None, None
        raise
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, None, None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("registered context path is unsafe to replace")
    if info.st_size > SESSION_FILE_LIMIT:
        return None, info.st_size, None
    data = _read_file(path, SESSION_FILE_LIMIT)
    return data, len(data), _digest(data)


def _write_session(root, harness, session_id, data):
    _atomic_write(_session_path(root, harness, session_id), data)


def _enrollment_id(value):
    try:
        result = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("durable enrollment ID must be a canonical UUID") from error
    if result != value:
        raise ValueError("durable enrollment ID must be lowercase canonical UUID text")
    return result


def _record(registry, enrollment_id):
    record = registry["enrollments"].get(enrollment_id)
    if record is None:
        raise ValueError("durable enrollment ID is missing from this worktree")
    return record


def _validate(text, path, role, validator):
    if not callable(validator):
        raise ValueError("a validation callback is required")
    result = validator(text, path, role)
    if result is False or (isinstance(result, dict) and result.get("valid") is False):
        raise ValueError("standing context validation was rejected")
    if getattr(result, "valid", True) is False:
        raise ValueError("standing context validation was rejected")


def _source(source_file):
    try:
        path = Path(source_file).expanduser()
    except (TypeError, ValueError, OSError) as error:
        raise ValueError("source file must be an absolute regular file") from error
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("source file must be absolute and traversal-free")
    path = Path(os.path.abspath(os.fspath(path)))
    if len(str(path).encode("utf-8")) > 4096:
        raise ValueError("source path is too long")
    data = _read_file(path, SESSION_FILE_LIMIT)
    if not data or b"\0" in data:
        raise ValueError("source file must contain nonempty UTF-8 Markdown without NUL bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise ValueError("source file must be valid UTF-8") from error
    return path, text, data


def lookup(root, gitdir, harness, session_id):
    """Return the exact binding, including released, retired, or pending state."""
    session_id = _normalize_session(session_id, strict=False)
    if session_id is None:
        return None
    harness = _harness(harness)
    root, gitdir = _root_gitdir(root, gitdir)
    with _locked(root, gitdir, create=False) as (meta, registry):
        if meta is None:
            return None
        record, binding = _find_record(registry, harness, session_id)
        if binding:
            _check_marker(meta, root, record["enrollment_id"], binding)
            return _public(root, gitdir, record["enrollment_id"], record, binding)
        record, outgoing = _pending_record(registry, harness, session_id)
        if record:
            marker = _read_marker(meta, harness, session_id)
            if (marker is None or marker.get("root") != str(root)
                    or marker.get("enrollment_id") != record["enrollment_id"]
                    or marker.get("state") != "pending" or marker.get("role") != outgoing["role"]
                    or marker.get("predecessor") != outgoing["identity"]):
                raise ValueError("pending successor marker is missing or inconsistent")
            return _pending_binding(root, gitdir, record["enrollment_id"], record, outgoing)
        if _read_marker(meta, harness, session_id) is not None:
            raise ValueError("required-binding marker references missing enrollment metadata")
        return None


def lookup_readonly(root, gitdir, harness, session_id):
    """Inspect one binding without creating or acquiring a registry lock file."""
    session_id = _normalize_session(session_id, strict=False)
    if session_id is None:
        return None
    harness = _harness(harness)
    root, gitdir = _root_gitdir(root, gitdir)
    meta = _meta_path(gitdir)
    if not _lexists(meta):
        return None
    _check_path(meta)
    registry = _read_registry(meta)
    if registry is None:
        if _lexists(meta / "bindings"):
            marker = _read_marker(meta, harness, session_id)
            if marker is not None:
                raise ValueError("required-binding marker references missing enrollment metadata")
        return None
    _validate_registry(registry, root, gitdir)
    record, binding = _find_record(registry, harness, session_id)
    if binding:
        _check_marker(meta, root, record["enrollment_id"], binding)
        return _public(root, gitdir, record["enrollment_id"], record, binding)
    record, outgoing = _pending_record(registry, harness, session_id)
    if record:
        marker = _read_marker(meta, harness, session_id)
        if (marker is None or marker.get("root") != str(root)
                or marker.get("enrollment_id") != record["enrollment_id"]
                or marker.get("state") != "pending" or marker.get("role") != outgoing["role"]
                or marker.get("predecessor") != outgoing["identity"]):
            raise ValueError("pending successor marker is missing or inconsistent")
        return _pending_binding(root, gitdir, record["enrollment_id"], record, outgoing)
    if _read_marker(meta, harness, session_id) is not None:
        raise ValueError("required-binding marker references missing enrollment metadata")
    return None


def status(root, gitdir, harness, session_id):
    """Return status for one exact native identity."""
    return lookup(root, gitdir, harness, session_id)


def enroll(root, gitdir, harness, session_id, source_file, role, validator, *,
           reviewed_update=False, evidence=None):
    """Create an enrollment or explicitly review an update by its active owner."""
    harness, session_id, role = _harness(harness), _normalize_session(session_id, strict=True), _role(role)
    if type(reviewed_update) is not bool:
        raise ValueError("reviewed_update must be an explicit boolean")
    evidence = _evidence(evidence) if reviewed_update else None
    root, gitdir = _root_gitdir(root, gitdir)
    source_file, text, data = _source(source_file)
    expected = _session_path(root, harness, session_id)
    _validate(text, expected, role, validator)
    identity = _identity(root, harness, session_id)

    with _locked(root, gitdir, create=True) as (meta, registry):
        record, current = _find_record(registry, harness, session_id)
        if current:
            if (current["state"] != "active" or record["active_identity"] != identity
                    or not reviewed_update or current["role"] != role):
                raise ValueError("only the active same owner may submit a reviewed context update")
            _check_marker(meta, root, record["enrollment_id"], current)
            old, observed_size, observed_digest = _read_reviewable_session(root, harness, session_id)
            if (old is not None and observed_digest == current["source_sha256"]
                    and old == data and current["source_file"] == str(source_file)):
                return _public(root, gitdir, record["enrollment_id"], record, current)
            previous = current["source_sha256"]
            current.update(source_file=str(source_file), source_sha256=_digest(data),
                           source_bytes=len(data), updated_at=_now())
            _append_event(record, "reviewed_update", identity=identity, role=role,
                          previous_sha256=previous, source_sha256=current["source_sha256"],
                          observed_previous_sha256=observed_digest,
                          observed_previous_bytes=observed_size,
                          evidence=evidence)
            _check_registry_size(registry)
            _write_session(root, harness, session_id, data)
            _write_marker(meta, record["enrollment_id"], identity, "active", role,
                          current["source_sha256"], predecessor=current.get("predecessor"),
                          successor=current.get("successor"))
            _write_registry(meta, registry)
            return _public(root, gitdir, record["enrollment_id"], record, current)
        if reviewed_update:
            raise ValueError("reviewed update requires this exact session's active enrollment")
        if len(registry["enrollments"]) >= MAX_ENROLLMENTS:
            raise ValueError("session enrollment registry is full")
        _check_collision(registry, harness, session_id)
        if _read_marker(meta, harness, session_id) is not None:
            raise ValueError("required-binding marker exists without its registration")
        if (harness, session_id) in _all_keys(registry):
            raise ValueError("native session identity is already reserved")
        if _read_session(root, harness, session_id) is not None:
            raise ValueError("unregistered session file already exists; inspect it before enrollment")

        enrollment_id = str(uuid.uuid4())
        now = _now()
        binding = {"identity": identity, "role": role, "state": "active",
                   "source_file": str(source_file), "source_sha256": _digest(data),
                   "source_bytes": len(data), "created_at": now, "updated_at": now,
                   "predecessor": None, "successor": None}
        record = {"enrollment_id": enrollment_id, "created_at": now, "updated_at": now,
                  "active_identity": identity, "pending_successor": None,
                  "bindings": [binding], "events": []}
        _append_event(record, "enrolled", identity=identity, role=role,
                      source_sha256=binding["source_sha256"])
        registry["enrollments"][enrollment_id] = record
        _check_registry_size(registry)
        _write_marker(meta, enrollment_id, identity, "active", role, binding["source_sha256"])
        _write_session(root, harness, session_id, data)
        _write_registry(meta, registry)
        return _public(root, gitdir, enrollment_id, record, binding)


def release(root, gitdir, enrollment_id, outgoing_harness, outgoing_session_id,
            next_harness, next_session_id, evidence, timers_safe):
    """Release only the exact active owner after supported timer cleanup."""
    enrollment_id = _enrollment_id(enrollment_id)
    old_harness, old_session = _harness(outgoing_harness), _normalize_session(outgoing_session_id, strict=True)
    new_harness, new_session = _harness(next_harness), _normalize_session(next_session_id, strict=True)
    evidence = _evidence(evidence)
    if type(timers_safe) is not bool or not timers_safe:
        raise ValueError("timer cleanup must be confirmed before release")
    if (old_harness, old_session) == (new_harness, new_session):
        raise ValueError("replacement identity must differ from outgoing identity")
    root, gitdir = _root_gitdir(root, gitdir)
    old_id = _identity(root, old_harness, old_session)
    new_id = _identity(root, new_harness, new_session)

    with _locked(root, gitdir, create=False) as (meta, registry):
        if meta is None:
            raise ValueError("durable enrollment ID is missing from this worktree")
        record = _record(registry, enrollment_id)
        outgoing = _find(record, old_harness, old_session)
        if outgoing is None:
            raise ValueError("outgoing identity is not bound to this enrollment")
        prior = next((event for event in reversed(record["events"])
                      if event.get("action") == "released" and event.get("identity") == old_id), None)
        if prior:
            if prior.get("next_identity") != new_id or prior.get("evidence") != evidence:
                raise ValueError("conflicting duplicate release is rejected")
            _check_marker(meta, root, enrollment_id, outgoing)
            return _public(root, gitdir, enrollment_id, record, outgoing)
        if record["active_identity"] != old_id or outgoing["state"] != "active":
            raise ValueError("only the exact active owner may release this enrollment")
        if (new_harness, new_session) in _all_keys(registry):
            raise ValueError("successor identity is already bound or reserved")
        _check_collision(registry, new_harness, new_session)
        if len(record["events"]) >= MAX_EVENTS:
            raise ValueError("handoff history is full")

        old_marker = _read_marker(meta, old_harness, old_session)
        if (old_marker is None or old_marker.get("root") != str(root)
                or old_marker.get("enrollment_id") != enrollment_id
                or old_marker.get("state") not in ("active", "released")
                or old_marker.get("role") != outgoing["role"]
                or old_marker.get("source_sha256") != outgoing["source_sha256"]):
            raise ValueError("outgoing required-binding marker is missing or inconsistent")
        if old_marker.get("state") == "released" and (
                old_marker.get("successor") != new_id or old_marker.get("release_evidence") != evidence):
            raise ValueError("interrupted release marker conflicts with this request")
        if old_marker.get("state") == "released" and old_marker.get("timers_safe") is not True:
            raise ValueError("interrupted release marker lacks safe timer evidence")
        new_marker = _read_marker(meta, new_harness, new_session)
        if new_marker is not None and (
                new_marker.get("root") != str(root)
                or new_marker.get("enrollment_id") != enrollment_id
                or new_marker.get("state") != "pending"
                or new_marker.get("predecessor") != old_id
                or new_marker.get("role") != outgoing["role"]
                or new_marker.get("release_evidence") != evidence):
            raise ValueError("successor already has a conflicting required-binding marker")

        outgoing.update(state="released", successor=new_id, updated_at=_now())
        record.update(active_identity=None, pending_successor=new_id)
        _append_event(record, "released", identity=old_id, next_identity=new_id,
                      evidence=evidence, timers_safe=True)
        _check_registry_size(registry)
        _write_marker(meta, enrollment_id, new_id, "pending", outgoing["role"],
                      predecessor=old_id, release_evidence=evidence, timers_safe=True)
        _write_marker(meta, enrollment_id, old_id, "released", outgoing["role"],
                      outgoing["source_sha256"], successor=new_id,
                      release_evidence=evidence, timers_safe=True)
        _write_registry(meta, registry)
        return _public(root, gitdir, enrollment_id, record, outgoing)


def adopt(root, gitdir, enrollment_id, harness, session_id, source_file, role, evidence, validator):
    """Adopt a released enrollment under its explicitly named successor session."""
    enrollment_id = _enrollment_id(enrollment_id)
    harness, session_id, role = _harness(harness), _normalize_session(session_id, strict=True), _role(role)
    evidence = _evidence(evidence)
    root, gitdir = _root_gitdir(root, gitdir)
    source_file, text, data = _source(source_file)
    expected = _session_path(root, harness, session_id)
    _validate(text, expected, role, validator)
    identity = _identity(root, harness, session_id)

    with _locked(root, gitdir, create=False) as (meta, registry):
        if meta is None:
            raise ValueError("durable enrollment ID is missing from this worktree")
        record = _record(registry, enrollment_id)
        existing = _find(record, harness, session_id)
        if existing and record["active_identity"] == identity and existing["state"] == "active":
            prior = next((event for event in reversed(record["events"])
                          if event.get("action") == "adopted" and event.get("identity") == identity), None)
            if (prior and prior.get("source_sha256") == _digest(data)
                    and prior.get("source_file") == str(source_file)
                    and prior.get("role") == role and prior.get("evidence") == evidence):
                _check_marker(meta, root, enrollment_id, existing)
                return _public(root, gitdir, enrollment_id, record, existing)
            raise ValueError("successor is already active; conflicting adoption is rejected")
        if record["active_identity"] is not None or record["pending_successor"] != identity:
            raise ValueError("adoption must match the exact pending successor")
        if existing or any(_key(binding["identity"]) == (harness, session_id)
                           for candidate in registry["enrollments"].values()
                           for binding in candidate["bindings"]):
            raise ValueError("released or retired native identity cannot be adopted again")
        if len(record["bindings"]) >= MAX_BINDINGS or len(record["events"]) >= MAX_EVENTS:
            raise ValueError("handoff history is full")
        outgoing = record["bindings"][-1]
        if outgoing["state"] != "released" or outgoing["role"] != role:
            raise ValueError("adoption must preserve the outgoing assigned role")
        release_event = next((event for event in reversed(record["events"])
                              if event.get("action") == "released"
                              and event.get("identity") == outgoing["identity"]), None)
        if release_event is None or release_event.get("timers_safe") is not True:
            raise ValueError("explicit safe release evidence is missing")

        old_marker = _read_marker(meta, outgoing["identity"]["harness"], outgoing["identity"]["session_id"])
        if (old_marker is None or old_marker.get("enrollment_id") != enrollment_id
                or old_marker.get("root") != str(root)
                or old_marker.get("role") != outgoing["role"]
                or old_marker.get("source_sha256") != outgoing["source_sha256"]):
            raise ValueError("outgoing required-binding marker is missing or inconsistent")
        if old_marker.get("state") == "released":
            if (old_marker.get("successor") != identity
                    or old_marker.get("release_evidence") != release_event.get("evidence")):
                raise ValueError("outgoing marker conflicts with recorded release")
        elif old_marker.get("state") == "retired":
            if old_marker.get("successor") != identity or old_marker.get("adoption_evidence") != evidence:
                raise ValueError("interrupted adoption marker conflicts with this request")
        else:
            raise ValueError("outgoing marker is not released")

        next_marker = _read_marker(meta, harness, session_id)
        if (next_marker is None or next_marker.get("enrollment_id") != enrollment_id
                or next_marker.get("root") != str(root)
                or next_marker.get("role") != role
                or next_marker.get("predecessor") != outgoing["identity"]):
            raise ValueError("successor required-binding marker is missing or inconsistent")
        if next_marker.get("state") == "pending":
            if next_marker.get("release_evidence") != release_event.get("evidence"):
                raise ValueError("pending marker conflicts with recorded release")
        elif (next_marker.get("state") != "active"
              or next_marker.get("source_sha256") != _digest(data)
              or next_marker.get("adoption_evidence") != evidence):
            raise ValueError("interrupted adoption marker conflicts with this request")

        old_file = _read_session(root, harness, session_id)
        if old_file is not None and old_file != data:
            raise ValueError("prepared successor file differs from reviewed source")
        now = _now()
        binding = {"identity": identity, "role": role, "state": "active",
                   "source_file": str(source_file), "source_sha256": _digest(data),
                   "source_bytes": len(data), "created_at": now, "updated_at": now,
                   "predecessor": outgoing["identity"], "successor": None}
        outgoing.update(state="retired", successor=identity, updated_at=now)
        record["bindings"].append(binding)
        record.update(active_identity=identity, pending_successor=None)
        _append_event(record, "adopted", identity=identity, predecessor=outgoing["identity"],
                      role=role, evidence=evidence, source_sha256=binding["source_sha256"],
                      source_file=str(source_file))
        _check_registry_size(registry)
        _write_marker(meta, enrollment_id, identity, "active", role, binding["source_sha256"],
                      predecessor=outgoing["identity"], release_evidence=release_event["evidence"],
                      adoption_evidence=evidence, timers_safe=True)
        _write_marker(meta, enrollment_id, outgoing["identity"], "retired", outgoing["role"],
                      outgoing["source_sha256"], successor=identity,
                      release_evidence=release_event["evidence"],
                      adoption_evidence=evidence, timers_safe=True)
        if old_file != data:
            _write_session(root, harness, session_id, data)
        _write_registry(meta, registry)
        return _public(root, gitdir, enrollment_id, record, binding)
