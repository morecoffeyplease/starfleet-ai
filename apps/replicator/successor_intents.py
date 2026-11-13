"""Persist one-shot successor intent without claiming native readiness."""

import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid


STORE_NAME = "successor-intents.json"
LOCK_NAME = ".successor-intents.lock"
STORE_SCHEMA_VERSION = 1
INTENT_PROTOCOL = "starfleet.replicator.successor-intent/v1"
MAX_STORE_BYTES = 1024 * 1024
MAX_TOTAL_INTENTS = 256
MAX_INTENTS_PER_SCOPE = 32
LOCK_TIMEOUT_SECONDS = 5
DOCTOR_STATES = {"ready", "missing", "blocked", "unknown", "not_applicable", "skipped"}


class IntentError(ValueError):
    pass


def empty_store():
    return {"schema_version": STORE_SCHEMA_VERSION, "scopes": {}}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _validate_store(value):
    if not isinstance(value, dict) or value.get("schema_version") != STORE_SCHEMA_VERSION:
        raise IntentError("Successor intent state has an unsupported format.")
    scopes = value.get("scopes")
    if not isinstance(scopes, dict):
        raise IntentError("Successor intent state is malformed.")
    total = 0
    for key, records in scopes.items():
        if not isinstance(key, str) or len(key) != 64 or not isinstance(records, list):
            raise IntentError("Successor intent state is malformed.")
        if len(records) > MAX_INTENTS_PER_SCOPE:
            raise IntentError("Successor intent state exceeds its record limit.")
        for record in records:
            if (not isinstance(record, dict) or record.get("schema_version") != STORE_SCHEMA_VERSION
                    or record.get("protocol") != INTENT_PROTOCOL
                    or not isinstance(record.get("id"), str)
                    or len(record["id"]) != 32
                    or record.get("state") not in ("configured", "cancelled")):
                raise IntentError("Successor intent state is malformed.")
            if any(character not in "0123456789abcdef" for character in record["id"]):
                raise IntentError("Successor intent state is malformed.")
            scope = record.get("scope")
            choice = record.get("choice")
            request_digest = record.get("request_digest")
            if (not isinstance(scope, dict) or set(scope) != {"seat", "worktree"}
                    or not isinstance(choice, dict) or record.get("policy") != "once"
                    or not isinstance(request_digest, str) or len(request_digest) != 64
                    or any(character not in "0123456789abcdef" for character in request_digest)
                    or scope_key(scope) != key or _request_digest(scope, choice) != request_digest):
                raise IntentError("Successor intent state is malformed.")
            seat = scope["seat"]
            worktree = scope["worktree"]
            if (not isinstance(seat, dict)
                    or not all(isinstance(seat.get(field), str) and seat[field]
                               for field in ("name", "harness", "session_id", "workspace_id", "panel_id"))
                    or seat["harness"] not in ("claude", "codex", "muse")
                    or not isinstance(worktree, dict)
                    or not all(isinstance(worktree.get(field), str) and worktree[field]
                               for field in ("root", "git_dir", "common_dir"))):
                raise IntentError("Successor intent state is malformed.")
            try:
                uuid.UUID(seat["workspace_id"])
                uuid.UUID(seat["panel_id"])
            except (ValueError, TypeError, AttributeError) as exc:
                raise IntentError("Successor intent state is malformed.") from exc
            kind = choice.get("kind")
            if kind == "preserve_observed_predecessor":
                if set(choice) != {"kind"}:
                    raise IntentError("Successor intent state is malformed.")
            elif kind == "model_preferences_candidate":
                if set(choice) != {
                        "kind", "snapshot", "active_profile", "active_selection_digest",
                        "candidate_matches_active_orchestrator", "candidate_digest", "provenance"}:
                    raise IntentError("Successor intent state is malformed.")
                snapshot = choice.get("snapshot")
                provenance = choice.get("provenance")
                candidate_digest = choice.get("candidate_digest")
                if (not isinstance(snapshot, dict)
                        or snapshot.get("role") != "orchestrator"
                        or snapshot.get("harness") != seat["harness"]
                        or not all(isinstance(snapshot.get(field), str) and snapshot[field]
                                   for field in ("family", "model", "effort"))
                        or not isinstance(provenance, dict)
                        or provenance.get("kind") != "configured_model_family"
                        or not isinstance(candidate_digest, str) or len(candidate_digest) != 64
                        or any(character not in "0123456789abcdef" for character in candidate_digest)):
                    raise IntentError("Successor intent state is malformed.")
                active_profile = choice.get("active_profile")
                active_selection_digest = choice.get("active_selection_digest")
                matches_active = choice.get("candidate_matches_active_orchestrator")
                if (active_profile is not None and (not isinstance(active_profile, str) or not active_profile)
                        or active_selection_digest is not None
                        and (not isinstance(active_selection_digest, str) or len(active_selection_digest) != 64
                             or any(character not in "0123456789abcdef" for character in active_selection_digest))
                        or (active_profile is None) != (active_selection_digest is None)
                        or matches_active is not None and type(matches_active) is not bool
                        or (active_profile is None) != (matches_active is None)
                        or matches_active is False and candidate_digest == active_selection_digest):
                    raise IntentError("Successor intent state is malformed.")
            else:
                raise IntentError("Successor intent state is malformed.")
            generation = record.get("native_generation")
            if generation != {"status": "unknown", "value": None, "evidence": None}:
                raise IntentError("Successor intent state is malformed.")
            observations = record.get("observations")
            expected_observation = {"status": "unknown", "value": None, "evidence": None}
            if (not isinstance(observations, dict)
                    or set(observations) != {"predecessor_model", "predecessor_effort", "effective_permissions"}
                    or any(observations.get(name) != expected_observation for name in observations)):
                raise IntentError("Successor intent state is malformed.")
            if (record.get("permission_requirement") != {
                    "kind": "match_predecessor_effective", "status": "unknown"}
                    or record.get("readiness") != {"status": "unknown"}
                    or record.get("custody_acceptance") != {"status": "not_applicable"}
                    or record.get("predecessor_cleanup") != {"status": "not_applicable"}):
                raise IntentError("Successor intent state is malformed.")
        total += len(records)
    if total > MAX_TOTAL_INTENTS:
        raise IntentError("Successor intent state exceeds its record limit.")
    return value


def read_store(state_dir):
    root = Path(state_dir).expanduser()
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return empty_store()
    except PermissionError:
        raise
    except OSError as exc:
        raise IntentError("Successor intent state directory could not be read.") from exc
    if stat.S_ISLNK(root_info.st_mode):
        raise IntentError("Successor intent state directory is a symlink.")
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_IMODE(root_info.st_mode) & 0o077:
        raise IntentError("Successor intent state directory must be private.")
    path = root / STORE_NAME
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return empty_store()
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise IntentError("Successor intent state file is a symlink.") from exc
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise PermissionError("Successor intent state could not be read.") from exc
        raise IntentError("Successor intent state could not be read.") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > MAX_STORE_BYTES):
            raise IntentError("Successor intent state is not a bounded regular file.")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            data = stream.read(MAX_STORE_BYTES + 1)
        if len(data) > MAX_STORE_BYTES:
            raise IntentError("Successor intent state exceeds its size limit.")
        return _validate_store(json.loads(data.decode("utf-8")))
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, IntentError):
            raise
        raise IntentError("Successor intent state could not be decoded.") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def ensure_private_directory(state_dir):
    root = Path(state_dir).expanduser()
    if root.is_symlink():
        raise IntentError("Successor intent state directory is a symlink.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise IntentError("Successor intent state directory must be private.")
    return root


@contextlib.contextmanager
def mutation_lock(state_dir):
    root = ensure_private_directory(state_dir)
    lock_path = root / LOCK_NAME
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise IntentError("Successor intent mutation lock is unavailable.") from exc
    with os.fdopen(fd, "a") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise IntentError("Successor intent mutation lock must be a private regular file.")
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise IntentError("Successor intent state is busy; retry the command.") from exc
                time.sleep(0.02)
        try:
            yield root
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def write_store(root, value):
    data = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(data) > MAX_STORE_BYTES:
        raise IntentError("Successor intent state exceeds its size limit.")
    fd, temporary = tempfile.mkstemp(prefix=".successor-intents-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / STORE_NAME)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def scope_key(scope):
    return digest({"seat": scope["seat"], "worktree": scope["worktree"]})


def _request_digest(scope, choice):
    return digest({"protocol": INTENT_PROTOCOL, "policy": "once", "scope": scope, "choice": choice})


def configure_intent(state_dir, scope, choice):
    key = scope_key(scope)
    request_digest = _request_digest(scope, choice)
    with mutation_lock(state_dir) as root:
        store = read_store(root)
        records = store["scopes"].setdefault(key, [])
        active = next((record for record in reversed(records) if record["state"] == "configured"), None)
        if active:
            if active["request_digest"] == request_digest:
                return {**active, "reused": True}
            raise IntentError("A different successor intent is already configured for this exact scope; cancel it first.")
        total = sum(len(items) for items in store["scopes"].values())
        if total >= MAX_TOTAL_INTENTS or len(records) >= MAX_INTENTS_PER_SCOPE:
            raise IntentError("Successor intent state reached its record limit.")
        intent = {
            "schema_version": STORE_SCHEMA_VERSION,
            "protocol": INTENT_PROTOCOL,
            "id": uuid.uuid4().hex,
            "scope": scope,
            "policy": "once",
            "choice": choice,
            "request_digest": request_digest,
            "state": "configured",
            "native_generation": {"status": "unknown", "value": None, "evidence": None},
            "observations": {
                "predecessor_model": {"status": "unknown", "value": None, "evidence": None},
                "predecessor_effort": {"status": "unknown", "value": None, "evidence": None},
                "effective_permissions": {"status": "unknown", "value": None, "evidence": None},
            },
            "permission_requirement": {"kind": "match_predecessor_effective", "status": "unknown"},
            "readiness": {"status": "unknown"},
            "custody_acceptance": {"status": "not_applicable"},
            "predecessor_cleanup": {"status": "not_applicable"},
        }
        records.append(intent)
        write_store(root, store)
        return {**intent, "reused": False}


def show_intent(state_dir, scope):
    key = scope_key(scope)
    records = read_store(state_dir)["scopes"].get(key, [])
    if not records:
        return {"schema_version": STORE_SCHEMA_VERSION, "intent": None}
    return {"schema_version": STORE_SCHEMA_VERSION, "intent": records[-1]}


def _find_intent(store, intent_id):
    for key, records in store["scopes"].items():
        for index, record in enumerate(records):
            if record["id"] == intent_id:
                return key, index, record
    return None, None, None


def cancel_intent(state_dir, intent_id):
    if len(intent_id) != 32 or any(character not in "0123456789abcdef" for character in intent_id):
        raise IntentError("Invalid successor intent ID.")
    store = read_store(state_dir)
    _, _, current = _find_intent(store, intent_id)
    if current is None:
        raise IntentError("Unknown successor intent.")
    if current["state"] == "cancelled":
        return current
    if current["state"] != "configured":
        raise IntentError("Only a configured successor intent can be cancelled.")
    with mutation_lock(state_dir) as root:
        store = read_store(root)
        key, index, current = _find_intent(store, intent_id)
        if current is None:
            raise IntentError("Unknown successor intent.")
        if current["state"] == "cancelled":
            return current
        if current["state"] != "configured":
            raise IntentError("Only a configured successor intent can be cancelled.")
        store["scopes"][key][index] = {**current, "state": "cancelled"}
        write_store(root, store)
        return store["scopes"][key][index]


def doctor_report(state_dir):
    configuration = "ready"
    try:
        read_store(state_dir)
    except PermissionError:
        configuration = "blocked"
    except OSError:
        configuration = "blocked"
    except IntentError:
        configuration = "unknown"
    checks = {
        "configuration": configuration,
        "registration": "not_applicable",
        "worker": "not_applicable",
        "intended_delivery": "not_applicable",
        "observed_delivery": "unknown",
    }
    if any(state not in DOCTOR_STATES for state in checks.values()):
        raise IntentError("Doctor state is invalid.")
    return {"schema_version": 1, "checks": checks}
