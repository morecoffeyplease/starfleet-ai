"""Collect exact-owner scoped access evidence for a frozen handoff."""

import errno
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import uuid


ACCESS_PROTOCOL = "starfleet.replicator.access-observation/v1"
ACCESS_SCHEMA_VERSION = 1
ACCESS_STATES = {"ready", "blocked", "unknown", "not_assessed"}
MAX_ACCESS_HISTORY = 8
MAX_ACCESS_PATHS = 16
MAX_ACCESS_PATH_LENGTH = 2048
MAX_ACCESS_INPUT_BYTES = 1024 * 1024
MAX_ORIGIN_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_PROBE_TARGETS = 3
PROBE_PREFIX = ".replicator-access-probe-"
PROBE_MARKER_PREFIX = "starfleet.replicator.access-probe/v1:"


class AccessObservationError(ValueError):
    pass


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def empty_access_observations():
    return {"schema_version": ACCESS_SCHEMA_VERSION, "predecessor": [], "successor": [], "pending": None}


def normalize_request(context_files=(), guidance_files=(), *, probe_write=False, probe_origin=False):
    if type(probe_write) is not bool or type(probe_origin) is not bool:
        raise AccessObservationError("Access probe options are malformed.")
    normalized = {}
    for field, values in (("context_files", context_files), ("guidance_files", guidance_files)):
        if not isinstance(values, (list, tuple)) or len(values) > MAX_ACCESS_PATHS:
            raise AccessObservationError("Pass no more than 16 context files and 16 guidance files.")
        paths = []
        for value in values:
            if not isinstance(value, str) or not value:
                raise AccessObservationError("Access input paths must be non-empty strings.")
            path = os.path.abspath(os.path.expanduser(value))
            if len(path) > MAX_ACCESS_PATH_LENGTH:
                raise AccessObservationError("An access input path exceeds the path limit.")
            if path not in paths:
                paths.append(path)
        normalized[field] = paths
    normalized["probe_write"] = probe_write
    normalized["probe_origin"] = probe_origin
    return normalized


def request_digest(request):
    return digest(request)


def _read_file(path):
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NONBLOCK", 0))
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return {"path": path, "status": "unknown", "reason": "missing", "bytes_read": None, "sha256": None}
    except PermissionError:
        return {"path": path, "status": "blocked", "reason": "read_failed", "bytes_read": None, "sha256": None}
    except OSError as exc:
        reason = "symlink_refused" if exc.errno == errno.ELOOP else "read_failed"
        return {"path": path, "status": "blocked", "reason": reason, "bytes_read": None, "sha256": None}
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return {"path": path, "status": "blocked", "reason": "not_regular_file",
                    "bytes_read": None, "sha256": None}
        if before.st_size > MAX_ACCESS_INPUT_BYTES:
            return {"path": path, "status": "unknown", "reason": "size_limit",
                    "bytes_read": None, "sha256": None}
        chunks = []
        remaining = MAX_ACCESS_INPUT_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError:
            current = None
        stable_before_after = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        stable_path = current is not None and (before.st_dev, before.st_ino) == (current.st_dev, current.st_ino)
        if not stable_before_after or not stable_path or len(data) != before.st_size:
            return {"path": path, "status": "unknown", "reason": "changed_during_read",
                    "bytes_read": None, "sha256": None}
        if not data or len(data) > MAX_ACCESS_INPUT_BYTES:
            return {"path": path, "status": "unknown", "reason": "empty_or_size_limit",
                    "bytes_read": len(data), "sha256": None}
        return {"path": path, "status": "ready", "reason": "bytes_read",
                "bytes_read": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    except PermissionError:
        return {"path": path, "status": "blocked", "reason": "read_failed", "bytes_read": None, "sha256": None}
    except OSError:
        return {"path": path, "status": "unknown", "reason": "read_failed", "bytes_read": None, "sha256": None}
    finally:
        os.close(fd)


def _aggregate(items):
    if not items or any(item["status"] == "unknown" for item in items):
        return "unknown"
    if any(item["status"] == "blocked" for item in items):
        return "blocked"
    return "ready"


def read_inputs(request):
    checks = {}
    for field, name in (("context_files", "context_access"), ("guidance_files", "guidance_access")):
        items = [_read_file(path) for path in request[field]]
        checks[name] = {
            "status": _aggregate(items),
            "reason": "no_explicit_paths" if not items else None,
            "items": items,
        }
    return checks


def git_metadata_check(identity, expected):
    if not isinstance(identity, dict):
        return {"status": "unknown", "reason": "git_identity_unavailable", "identity_digest": None}
    if identity != expected:
        return {"status": "blocked", "reason": "git_identity_mismatch", "identity_digest": digest(identity)}
    return {"status": "ready", "reason": "canonical_git_identity_read", "identity_digest": digest(identity)}


def git_origin_check(request, root, runner):
    if not request["probe_origin"]:
        return {"status": "unknown", "reason": "not_requested", "refs_bytes": None, "refs_sha256": None}
    try:
        status, output = runner(["git", "-C", root, "ls-remote", "--refs", "origin"], closed_stdin=True)
    except (OSError, ValueError, TimeoutError):
        status, output = "unavailable", None
    if status != "ready" or not isinstance(output, str):
        return {"status": "unknown", "reason": "origin_probe_failed", "refs_bytes": None, "refs_sha256": None}
    data = output.encode("utf-8", "replace")
    if len(data) > MAX_ORIGIN_OUTPUT_BYTES:
        return {"status": "unknown", "reason": "origin_probe_failed", "refs_bytes": None, "refs_sha256": None}
    return {"status": "ready", "reason": "origin_refs_read", "refs_bytes": len(data),
            "refs_sha256": hashlib.sha256(data).hexdigest()}


def _source_identity(snapshot, key):
    source = snapshot.get(key) if isinstance(snapshot, dict) else None
    if not isinstance(source, dict):
        return None
    fields = ("kind", "path", "device", "inode")
    return {name: source.get(name) for name in fields}


def _hook_owner_identity(snapshot):
    source = _source_identity(snapshot, "hook_source")
    if not isinstance(source, dict):
        return None
    return {name: source[name] for name in ("kind", "path", "device")}


def _matches_legacy_inode_only_owner_block(checks, baseline, observed, expected_comparisons):
    if expected_comparisons["owner_binding"]["status"] != "ready":
        return False
    baseline_hook = _source_identity(baseline, "hook_source")
    observed_hook = _source_identity(observed, "hook_source")
    if (not isinstance(baseline_hook, dict) or not isinstance(observed_hook, dict)
            or baseline_hook["inode"] == observed_hook["inode"]
            or any(baseline_hook[name] != observed_hook[name] for name in ("kind", "path", "device"))):
        return False
    legacy_owner_check = {
        **expected_comparisons["owner_binding"],
        "status": "blocked",
        "differences": ["hook_source"],
    }
    return (checks.get("owner_binding") == legacy_owner_check
            and checks.get("native_settings_match") == expected_comparisons["native_settings_match"])


def _rollout_identity(snapshot):
    source = snapshot.get("rollout_source") if isinstance(snapshot, dict) else None
    if not isinstance(source, dict):
        return None
    fields = ("kind", "path", "device", "inode", "cli_version", "session_meta_cwd",
              "session_meta_source", "session_meta_originator")
    return {name: source.get(name) for name in fields}


def _expected_subject(scope):
    return {
        "harness": scope["seat"]["harness"],
        "session_id": scope["seat"]["session_id"],
        "panel_id": scope["seat"]["panel_id"],
        "workspace_id": scope["seat"]["workspace_id"],
        "worktree": scope["worktree"],
    }


def access_baseline(handoff, actor):
    if actor == "predecessor":
        baseline = handoff.get("native_predecessor_snapshot")
        if not isinstance(baseline, dict):
            raise AccessObservationError("Record the exact predecessor settings snapshot before observing access.")
        return handoff["scope"], baseline
    if actor != "successor":
        raise AccessObservationError("The scoped access actor is unsupported.")
    launch = handoff.get("launch", {})
    baseline = handoff.get("native_successor_snapshot")
    if launch.get("status") != "successor_observed" or not isinstance(baseline, dict):
        raise AccessObservationError("Record the exact opened successor settings snapshot before observing successor access.")
    subject = baseline.get("subject")
    returned = launch.get("returned_surface")
    predecessor = handoff.get("scope", {}).get("seat", {})
    if (not isinstance(subject, dict) or not isinstance(returned, dict)
            or subject.get("harness") != "codex"
            or subject.get("session_id") == predecessor.get("session_id")
            or subject.get("panel_id", "").upper() != returned.get("surface_id", "").upper()
            or subject.get("workspace_id", "").upper() != returned.get("workspace_id", "").upper()
            or subject.get("worktree") != handoff["scope"]["worktree"]):
        raise AccessObservationError("The recorded successor does not match the exact opened surface and worktree.")
    scope = {
        "seat": {
            **handoff["scope"]["seat"],
            "harness": subject["harness"],
            "session_id": subject["session_id"],
            "panel_id": subject["panel_id"],
            "workspace_id": subject["workspace_id"],
        },
        "worktree": handoff["scope"]["worktree"],
    }
    return scope, baseline


def opening_access_observation(handoff):
    launch = handoff.get("launch", {})
    attempt = launch.get("attempt", {})
    preflight = attempt.get("preflight", {})
    access = handoff.get("access_observations", {})
    observations = access.get("predecessor", []) if isinstance(access, dict) else []
    observation_id = preflight.get("access_observation_id") if isinstance(preflight, dict) else None
    recorded = next((item for item in observations if item.get("id") == observation_id), None)
    if (not isinstance(recorded, dict) or preflight.get("status") != "ready"
            or preflight.get("access_observation_digest") != digest(recorded)):
        raise AccessObservationError("The immutable opening access reference is unavailable or malformed.")
    return recorded


def validate_successor_request(handoff, request):
    opening = opening_access_observation(handoff)["request"]
    same_paths = all(
        len(request[name]) == len(opening[name]) and set(request[name]) == set(opening[name])
        for name in ("context_files", "guidance_files")
    )
    if (not same_paths or request["probe_write"] != opening["probe_write"]
            or request["probe_origin"] != opening["probe_origin"]):
        raise AccessObservationError(
            "Successor access must use the opening's exact context, guidance, and probe request."
        )
    return opening


def pending_access_entry(value):
    pending_id = value.get("pending") if isinstance(value, dict) else None
    if pending_id is None:
        return None
    matches = [(actor, observation) for actor in ("predecessor", "successor")
               for observation in value.get(actor, []) if observation.get("id") == pending_id]
    if len(matches) != 1:
        raise AccessObservationError("The pending access identity does not select one exact actor observation.")
    return matches[0]


def compare_native_snapshots(scope, baseline, observed):
    baseline = baseline if isinstance(baseline, dict) else {}
    observed = observed if isinstance(observed, dict) else {}
    subject_matches = (isinstance(baseline, dict) and isinstance(observed, dict)
                       and baseline.get("subject") == _expected_subject(scope)
                       and observed.get("subject") == _expected_subject(scope))
    baseline_process = baseline.get("process_identity")
    observed_process = observed.get("process_identity")
    process_matches = (isinstance(baseline_process, dict) and bool(baseline_process)
                       and baseline_process == observed_process)
    baseline_hook_generation = baseline.get("hook_generation")
    hook_generation_matches = (isinstance(baseline_hook_generation, str) and bool(baseline_hook_generation)
                               and baseline_hook_generation == observed.get("hook_generation"))
    baseline_hook_source = _hook_owner_identity(baseline)
    hook_source_matches = (isinstance(baseline_hook_source, dict) and bool(baseline_hook_source)
                           and baseline_hook_source == _hook_owner_identity(observed))
    baseline_rollout = _rollout_identity(baseline)
    rollout_matches = (isinstance(baseline_rollout, dict) and bool(baseline_rollout)
                       and baseline_rollout == _rollout_identity(observed))
    owner_matches = all((subject_matches, process_matches, hook_generation_matches,
                         hook_source_matches, rollout_matches))
    baseline_settings = baseline.get("settings") if isinstance(baseline, dict) else None
    observed_settings = observed.get("settings") if isinstance(observed, dict) else None
    setting_names = ("model", "effort", "approval_policy", "sandbox_policy")
    settings_matches = (isinstance(baseline_settings, dict) and isinstance(observed_settings, dict)
                        and all(baseline_settings.get(name) == observed_settings.get(name)
                                for name in setting_names))
    owner_differences = [
        name for name, matches in (
            ("subject", subject_matches), ("process_identity", process_matches),
            ("hook_generation", hook_generation_matches), ("hook_source", hook_source_matches),
            ("rollout_source", rollout_matches),
        ) if not matches
    ]
    settings_differences = [name for name in setting_names
                            if not isinstance(baseline_settings, dict) or not isinstance(observed_settings, dict)
                            or baseline_settings.get(name) != observed_settings.get(name)]
    return {
        "owner_binding": {"status": "ready" if owner_matches else "blocked",
                           "baseline_snapshot_digest": digest(baseline),
                           "observed_snapshot_digest": digest(observed),
                           "differences": owner_differences},
        "native_settings_match": {"status": "ready" if settings_matches else "blocked",
                                  "baseline_snapshot_digest": digest(baseline),
                                  "observed_snapshot_digest": digest(observed),
                                  "differences": settings_differences},
    }


def operation_identity(snapshot):
    native = {
        "subject": snapshot.get("subject"),
        "hook_generation": snapshot.get("hook_generation"),
        "process_identity": snapshot.get("process_identity"),
        "hook_source": _source_identity(snapshot, "hook_source"),
        "rollout_source": _rollout_identity(snapshot),
        "settings": snapshot.get("settings"),
        "turn": snapshot.get("turn"),
    }
    return digest(native)


def operation_key(request, snapshot, *, retry_nonce=None):
    return digest({"request_digest": request_digest(request),
                   "native_operation_identity": operation_identity(snapshot),
                   "nonce": retry_nonce})


def make_observation(handoff, snapshot, request, inputs, git_check, origin_check, *, observed_at,
                     actor="predecessor", retry=False):
    scope, baseline = access_baseline(handoff, actor)
    if actor == "successor":
        validate_successor_request(handoff, request)
    comparisons = compare_native_snapshots(scope, baseline, snapshot)
    write_status = "not_assessed"
    write_reason = "not_requested"
    targets = []
    if request["probe_write"]:
        if (comparisons["owner_binding"]["status"] != "ready"
                or comparisons["native_settings_match"]["status"] != "ready"):
            write_status = "unknown"
            write_reason = "owner_or_settings_mismatch"
        else:
            write_status = "unknown"
            write_reason = "probe_pending"
    checks = {
        **comparisons,
        **inputs,
        "git_metadata_access": git_check,
        "write_access": {"status": write_status, "reason": write_reason, "targets": targets},
        "git_origin_access": origin_check,
        "native_trust": {"status": "unknown", "reason": "no_supported_exact_trust_source"},
        "effective_permissions": {"status": "unknown",
                                  "reason": "scoped_checks_do_not_prove_complete_permissions"},
    }
    nonce = uuid.uuid4().hex if retry else None
    key = operation_key(request, snapshot, retry_nonce=nonce)
    return {
        "schema_version": ACCESS_SCHEMA_VERSION,
        "protocol": ACCESS_PROTOCOL,
        "id": key[:32],
        "operation_key": key,
        "request_digest": request_digest(request),
        "request": request,
        "state": "complete",
        "subject": snapshot["subject"],
        "baseline_snapshot_digest": digest(baseline),
        "native_snapshot": snapshot,
        "native_settings_ref": {"snapshot_digest": digest(snapshot),
                                "operation_identity": operation_identity(snapshot)},
        "observed_at": observed_at,
        "checks": checks,
        "write_plan": [],
        "cleanup": {"status": "not_requested", "targets": []},
        "revalidations": [],
        "retry": retry,
    }


def make_write_plan(worktree_identity, operation_id):
    unique = []
    for label, path in (("worktree", worktree_identity["root"]),
                        ("git_dir", worktree_identity["git_dir"]),
                        ("common_dir", worktree_identity["common_dir"])):
        target = next((item for item in unique if item["path"] == path), None)
        if target is None:
            target = {"index": len(unique), "path": path, "scopes": [], "device": None, "inode": None,
                      "probe_name": None, "marker": None, "file_identity": None, "stage": "planned",
                      "status": "unknown", "cleanup_status": "not_started", "reason": "probe_pending",
                      "evidence": None}
            unique.append(target)
        target["scopes"].append(label)
    if len(unique) > MAX_PROBE_TARGETS:
        raise AccessObservationError("The frozen Git identity contains too many probe targets.")
    for target in unique:
        try:
            info = os.stat(target["path"], follow_symlinks=False)
            resolved = os.path.realpath(target["path"])
        except OSError:
            target.update({"stage": "failed", "status": "unknown", "cleanup_status": "verified",
                           "reason": "scope_directory_unavailable"})
            continue
        if not stat.S_ISDIR(info.st_mode) or resolved != target["path"]:
            target.update({"stage": "failed", "status": "blocked", "cleanup_status": "verified",
                           "reason": "scope_directory_mismatch"})
            continue
        target["device"] = info.st_dev
        target["inode"] = info.st_ino
        target["probe_name"] = f"{PROBE_PREFIX}{operation_id}-{target['index']}"
        target["marker"] = f"{PROBE_MARKER_PREFIX}{operation_id}:{target['index']}"
    return unique


def _open_target_directory(target):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target["path"], flags)
    info = os.fstat(fd)
    if ((info.st_dev, info.st_ino) != (target["device"], target["inode"])
            or not stat.S_ISDIR(info.st_mode) or os.path.realpath(target["path"]) != target["path"]):
        os.close(fd)
        raise AccessObservationError("The scoped probe directory changed before the access check.")
    return fd


def _probe_file(dir_fd, target, *, writing=False):
    flags = (os.O_WRONLY if writing else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target["probe_name"], flags, dir_fd=dir_fd)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) & 0o077
                or (target["file_identity"] is not None
                    and (before.st_dev, before.st_ino) !=
                    (target["file_identity"]["device"], target["file_identity"]["inode"]))):
            raise AccessObservationError("The exact probe file identity or ownership changed.")
        if writing:
            if stat.S_IMODE(before.st_mode) & 0o077:
                raise AccessObservationError("The exact probe file is not owner-only storage.")
            data = b""
        else:
            data = os.read(fd, len(target["marker"].encode("utf-8")) + 1)
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise AccessObservationError("The exact probe file changed while it was read.")
        return data, before
    finally:
        os.close(fd)


def _revalidate_scope(scope, identity_resolver):
    identity = identity_resolver(scope["worktree"]["root"])
    if identity != scope["worktree"]:
        raise AccessObservationError("The canonical worktree identity changed during the write probe.")


def _remove_exact_probe(dir_fd, target, scope, identity_resolver):
    current = os.stat(target["probe_name"], dir_fd=dir_fd, follow_symlinks=False)
    expected = target.get("file_identity")
    if (not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid() or current.st_nlink != 1
            or expected is None or (current.st_dev, current.st_ino) !=
            (expected["device"], expected["inode"])):
        raise AccessObservationError("The exact probe file identity could not be verified for cleanup.")
    data, opened = _probe_file(dir_fd, target)
    marker = target["marker"].encode("utf-8")
    if data != marker or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise AccessObservationError("The probe file contents changed; the file was preserved.")
    _revalidate_scope(scope, identity_resolver)
    latest = os.stat(target["probe_name"], dir_fd=dir_fd, follow_symlinks=False)
    if (latest.st_dev, latest.st_ino) != (current.st_dev, current.st_ino):
        raise AccessObservationError("The exact probe file changed before cleanup.")
    os.unlink(target["probe_name"], dir_fd=dir_fd)
    os.fsync(dir_fd)
    try:
        os.stat(target["probe_name"], dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise AccessObservationError("The exact probe file remains after cleanup.")


def _owned_file_identity(dir_fd, target):
    try:
        info = os.stat(target["probe_name"], dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        return None
    return {"device": info.st_dev, "inode": info.st_ino}


def _complete_target(target, *, reason="probe_bytes_written_read_and_removed"):
    return {**target, "stage": "complete", "status": "ready", "cleanup_status": "verified",
            "reason": reason}


def _write_or_resume(target, dir_fd, persist):
    marker = target["marker"].encode("utf-8")
    if target["stage"] == "planned":
        try:
            fd = os.open(target["probe_name"], os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dir_fd)
        except FileExistsError:
            return {**target, "stage": "identity_unverified", "status": "unknown",
                    "cleanup_status": "incomplete", "reason": "probe_collision_preserved"}
        except PermissionError:
            return {**target, "stage": "failed", "status": "blocked", "cleanup_status": "verified",
                    "reason": "probe_write_failed"}
        except OSError:
            return {**target, "stage": "failed", "status": "unknown", "cleanup_status": "verified",
                    "reason": "probe_write_failed"}
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
                return {**target, "stage": "created", "status": "unknown", "cleanup_status": "incomplete",
                        "reason": "probe_file_identity_unverified",
                        "file_identity": {"device": info.st_dev, "inode": info.st_ino}}
            target = {**target, "stage": "created", "file_identity": {"device": info.st_dev,
                                                                            "inode": info.st_ino},
                      "cleanup_status": "incomplete", "reason": "probe_write_pending"}
            persist(target)
            os.fsync(dir_fd)
            offset = 0
            while offset < len(marker):
                written = os.write(fd, marker[offset:])
                if written <= 0:
                    raise OSError("Probe write made no progress.")
                offset += written
            os.fsync(fd)
        except OSError:
            current = _owned_file_identity(dir_fd, target)
            if current is None:
                return {**target, "stage": "created", "status": "unknown", "cleanup_status": "incomplete",
                        "reason": "probe_write_incomplete"}
            return {**target, "stage": "created", "file_identity": current, "status": "unknown",
                    "cleanup_status": "incomplete", "reason": "probe_write_incomplete"}
        finally:
            os.close(fd)
    if target["stage"] == "created":
        try:
            identity = target.get("file_identity") or _owned_file_identity(dir_fd, target)
            if identity is None:
                return {**target, "stage": "planned", "status": "unknown", "cleanup_status": "not_started",
                        "reason": "probe_file_missing_before_write"}
            target = {**target, "file_identity": identity}
            data, info = _probe_file(dir_fd, target)
            if not marker.startswith(data):
                return {**target, "status": "unknown", "cleanup_status": "incomplete",
                        "reason": "probe_contents_unverified"}
            if data != marker:
                fd = os.open(target["probe_name"], os.O_WRONLY | os.O_APPEND
                             | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
                try:
                    current = os.fstat(fd)
                    if (current.st_dev, current.st_ino) != (identity["device"], identity["inode"]):
                        raise AccessObservationError("The exact probe file changed before retry.")
                    offset = len(data)
                    while offset < len(marker):
                        written = os.write(fd, marker[offset:])
                        if written <= 0:
                            raise OSError("Probe retry write made no progress.")
                        offset += written
                    os.fsync(fd)
                finally:
                    os.close(fd)
                data, info = _probe_file(dir_fd, target)
            if data != marker or (info.st_dev, info.st_ino) != (identity["device"], identity["inode"]):
                return {**target, "status": "unknown", "cleanup_status": "incomplete",
                        "reason": "probe_bytes_unverified"}
            evidence = {"bytes_written": len(marker), "bytes_read": len(data),
                        "marker_sha256": hashlib.sha256(marker).hexdigest(), "file_identity": identity}
            target = {**target, "stage": "access_verified", "status": "ready",
                      "cleanup_status": "incomplete", "reason": "probe_bytes_verified", "evidence": evidence}
            persist(target)
        except FileNotFoundError:
            return {**target, "stage": "planned", "status": "unknown", "cleanup_status": "not_started",
                    "reason": "probe_file_missing_before_write"}
        except (OSError, AccessObservationError):
            return {**target, "status": "unknown", "cleanup_status": "incomplete",
                    "reason": "probe_state_unverified"}
    return target


def probe_target(target, scope, identity_resolver, persist):
    if target["stage"] in ("failed", "complete", "identity_unverified"):
        return target
    try:
        dir_fd = _open_target_directory(target)
    except PermissionError:
        return {**target, "stage": "failed", "status": "blocked", "cleanup_status": "verified",
                "reason": "scope_directory_open_failed"}
    except OSError:
        return {**target, "stage": "failed", "status": "unknown", "cleanup_status": "verified",
                "reason": "scope_directory_open_failed"}
    try:
        if target["stage"] == "access_verified":
            try:
                _remove_exact_probe(dir_fd, target, scope, identity_resolver)
            except FileNotFoundError:
                return _complete_target(target, reason="previous_cleanup_verified")
            except (OSError, AccessObservationError):
                return {**target, "status": "ready", "cleanup_status": "incomplete",
                        "reason": "probe_cleanup_unverified"}
            return _complete_target(target)
        if target["stage"] == "failed":
            return target
        if target["stage"] not in ("planned", "created"):
            return {**target, "status": "unknown", "cleanup_status": "incomplete",
                    "reason": "probe_stage_unsupported"}
        if target["stage"] == "planned":
            try:
                _revalidate_scope(scope, identity_resolver)
            except (OSError, ValueError):
                return {**target, "stage": "failed", "status": "unknown", "cleanup_status": "verified",
                        "reason": "scope_identity_revalidation_failed"}
        current = _write_or_resume(target, dir_fd, persist)
        if current["stage"] == "access_verified":
            try:
                _remove_exact_probe(dir_fd, current, scope, identity_resolver)
            except FileNotFoundError:
                return _complete_target(current, reason="probe_bytes_written_read_and_removed")
            except (OSError, AccessObservationError):
                return {**current, "status": "ready", "cleanup_status": "incomplete",
                        "reason": "probe_cleanup_unverified"}
            return _complete_target(current)
        if current["stage"] == "planned" and target["stage"] == "created":
            return probe_target(current, scope, identity_resolver, persist)
        return current
    finally:
        os.close(dir_fd)


def summarize_write_targets(targets):
    if any(target["status"] == "blocked" for target in targets):
        status = "blocked"
    elif any(target["status"] != "ready" for target in targets):
        status = "unknown"
    else:
        status = "ready"
    cleanup = "verified" if all(target["cleanup_status"] == "verified" for target in targets) else "incomplete"
    return {"status": status, "reason": "scoped_write_probe", "targets": targets}, cleanup


def validate_access_history(value, handoff):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "predecessor", "successor", "pending"}
            or value.get("schema_version") != ACCESS_SCHEMA_VERSION
            or not isinstance(value.get("predecessor"), list)
            or not isinstance(value.get("successor"), list)
            or len(value["predecessor"]) > MAX_ACCESS_HISTORY
            or len(value["successor"]) > MAX_ACCESS_HISTORY):
        raise AccessObservationError("The scoped access observation history is malformed.")
    pending_id = value.get("pending")
    if pending_id is not None and (not isinstance(pending_id, str) or len(pending_id) != 32
                                   or any(char not in "0123456789abcdef" for char in pending_id)):
        raise AccessObservationError("The pending access operation identity is malformed.")
    seen = set()
    pending_matches = []
    for actor in ("predecessor", "successor"):
        observations = value[actor]
        if actor == "successor" and observations and handoff.get("launch", {}).get("status") != "successor_observed":
            raise AccessObservationError("Successor access evidence requires the exact observed native successor.")
        try:
            _scope, baseline = access_baseline(handoff, actor)
        except AccessObservationError:
            if observations:
                raise
            baseline = None
        for observation in observations:
            validate_access_observation(observation, handoff, baseline, actor=actor)
            if actor == "successor":
                validate_successor_request(handoff, observation["request"])
            if observation["id"] in seen:
                raise AccessObservationError("The scoped access observation identity is duplicated.")
            seen.add(observation["id"])
            if observation["id"] == pending_id:
                pending_matches.append((actor, observation))
    if pending_id is not None and (len(pending_matches) != 1
                                   or pending_matches[0][1]["state"] != "cleanup_incomplete"):
        raise AccessObservationError("The pending access operation does not match retained cleanup evidence.")
    if pending_id is None and any(item["state"] == "cleanup_incomplete"
                                  for actor in ("predecessor", "successor") for item in value[actor]):
        raise AccessObservationError("Incomplete access cleanup cannot be detached from its pending operation.")
    return True


def validate_access_observation(observation, handoff, baseline, *, actor="predecessor"):
    required = {
        "schema_version", "protocol", "id", "operation_key", "request_digest", "request", "state",
        "subject", "baseline_snapshot_digest", "native_snapshot", "native_settings_ref", "observed_at",
        "checks", "write_plan", "cleanup", "revalidations", "retry",
    }
    if (not isinstance(observation, dict) or set(observation) != required
            or observation.get("schema_version") != ACCESS_SCHEMA_VERSION
            or observation.get("protocol") != ACCESS_PROTOCOL
            or not _hex(observation.get("id"), 32) or not _hex(observation.get("operation_key"), 64)
            or observation.get("id") != observation.get("operation_key", "")[:32]
            or not _hex(observation.get("request_digest"), 64)
            or observation.get("state") not in ("complete", "cleanup_incomplete")
            or not isinstance(baseline, dict)
            or observation.get("baseline_snapshot_digest") != digest(baseline)
            or not _timestamp(observation.get("observed_at"))
            or type(observation.get("retry")) is not bool
            or not isinstance(observation.get("native_snapshot"), dict)
            or observation.get("subject") != observation["native_snapshot"].get("subject")
            or observation.get("subject") != _expected_subject(access_baseline(handoff, actor)[0])):
        raise AccessObservationError("The scoped access observation identity is malformed.")
    request = observation.get("request")
    if (not isinstance(request, dict)
            or set(request) != {"context_files", "guidance_files", "probe_write", "probe_origin"}
            or request_digest(request) != observation["request_digest"]
            or type(request.get("probe_write")) is not bool or type(request.get("probe_origin")) is not bool
            or any(not isinstance(request.get(name), list) or len(request[name]) > MAX_ACCESS_PATHS
                   or any(not isinstance(path, str) or not os.path.isabs(path)
                          or len(path) > MAX_ACCESS_PATH_LENGTH for path in request[name])
                   for name in ("context_files", "guidance_files"))):
        raise AccessObservationError("The scoped access request is malformed.")
    if actor == "successor":
        validate_successor_request(handoff, request)
    native = observation.get("native_snapshot")
    if not _valid_native_snapshot(native, observation["subject"]):
        raise AccessObservationError("The native snapshot referenced by access evidence is malformed.")
    settings_ref = observation.get("native_settings_ref")
    if (not isinstance(settings_ref, dict) or set(settings_ref) != {"snapshot_digest", "operation_identity"}
            or settings_ref.get("snapshot_digest") != digest(native)
            or settings_ref.get("operation_identity") != operation_identity(native)):
        raise AccessObservationError("The native settings reference is malformed.")
    checks = observation.get("checks")
    names = {"owner_binding", "native_settings_match", "context_access", "guidance_access",
             "git_metadata_access", "write_access", "git_origin_access", "native_trust",
             "effective_permissions"}
    if not isinstance(checks, dict) or set(checks) != names:
        raise AccessObservationError("The scoped access checks are malformed.")
    for name, check in checks.items():
        if not isinstance(check, dict) or check.get("status") not in ACCESS_STATES:
            raise AccessObservationError("The scoped access check status is malformed.")
    actor_scope, actor_baseline = access_baseline(handoff, actor)
    if actor_baseline != baseline:
        raise AccessObservationError("The scoped access baseline does not match its recorded actor.")
    expected_comparisons = compare_native_snapshots(actor_scope, baseline, native)
    comparisons_match = all(checks[name] == expected_comparisons[name] for name in expected_comparisons)
    legacy_inode_only_block = _matches_legacy_inode_only_owner_block(
        checks, baseline, native, expected_comparisons,
    ) if actor == "predecessor" else False
    if not comparisons_match and not legacy_inode_only_block:
        raise AccessObservationError("The native owner or settings comparison does not match its snapshots.")
    _validate_input_check(checks["context_access"], request["context_files"])
    _validate_input_check(checks["guidance_access"], request["guidance_files"])
    metadata = checks["git_metadata_access"]
    if (set(metadata) != {"status", "reason", "identity_digest"}
            or metadata.get("status") not in ACCESS_STATES
            or metadata.get("reason") not in ("git_identity_unavailable", "git_identity_mismatch",
                                               "canonical_git_identity_read")
            or metadata.get("identity_digest") is not None and not _hex(metadata["identity_digest"], 64)):
        raise AccessObservationError("The Git metadata check is malformed.")
    if metadata["status"] == "ready" and (metadata["reason"] != "canonical_git_identity_read"
                                           or metadata["identity_digest"] is None):
        raise AccessObservationError("A successful Git metadata check is malformed.")
    if metadata["status"] == "blocked" and metadata["reason"] != "git_identity_mismatch":
        raise AccessObservationError("A blocked Git metadata check is malformed.")
    if metadata["status"] == "unknown" and (metadata["reason"] != "git_identity_unavailable"
                                              or metadata["identity_digest"] is not None):
        raise AccessObservationError("An unknown Git metadata check is malformed.")
    origin = checks["git_origin_access"]
    if (set(origin) != {"status", "reason", "refs_bytes", "refs_sha256"}
            or origin.get("refs_sha256") is not None and not _hex(origin["refs_sha256"], 64)
            or origin.get("refs_bytes") is not None
            and (type(origin["refs_bytes"]) is not int or origin["refs_bytes"] < 0)):
        raise AccessObservationError("The Git origin check is malformed.")
    if request["probe_origin"]:
        if origin.get("status") == "ready":
            if origin.get("reason") != "origin_refs_read" or origin.get("refs_bytes") is None or origin.get("refs_sha256") is None:
                raise AccessObservationError("A successful Git origin check is malformed.")
        elif origin != {"status": "unknown", "reason": "origin_probe_failed",
                        "refs_bytes": None, "refs_sha256": None}:
            raise AccessObservationError("An unsuccessful Git origin check cannot claim a remote denial.")
    elif origin != {"status": "unknown", "reason": "not_requested", "refs_bytes": None, "refs_sha256": None}:
        raise AccessObservationError("An unrequested Git origin check cannot contain evidence.")
    if checks["native_trust"].get("status") != "unknown" or checks["effective_permissions"].get("status") != "unknown":
        raise AccessObservationError("Scoped access evidence cannot establish native trust or complete permissions.")
    if (not isinstance(observation.get("write_plan"), list)
            or len(observation["write_plan"]) > MAX_PROBE_TARGETS
            or not isinstance(observation.get("cleanup"), dict)
            or observation["cleanup"].get("status") not in ("not_requested", "verified", "incomplete")
            or not isinstance(observation.get("revalidations"), list)
            or len(observation["revalidations"]) > 8):
        raise AccessObservationError("The scoped access cleanup state is malformed.")
    expected_targets = []
    for label, path in (("worktree", handoff["scope"]["worktree"]["root"]),
                        ("git_dir", handoff["scope"]["worktree"]["git_dir"]),
                        ("common_dir", handoff["scope"]["worktree"]["common_dir"])):
        item = next((entry for entry in expected_targets if entry["path"] == path), None)
        if item is None:
            item = {"path": path, "scopes": []}
            expected_targets.append(item)
        item["scopes"].append(label)
    if request["probe_write"] and checks["owner_binding"]["status"] == "ready" and checks["native_settings_match"]["status"] == "ready":
        if (len(observation["write_plan"]) != len(expected_targets)
                or any((target.get("index"), target.get("path"), target.get("scopes")) !=
                       (index, expected["path"], expected["scopes"])
                       for index, (target, expected) in enumerate(zip(observation["write_plan"], expected_targets)))):
            raise AccessObservationError("The scoped write targets do not match the frozen Git identity.")
    elif observation["write_plan"]:
        raise AccessObservationError("Write probes require matching predecessor owner and native settings.")
    for revalidation in observation["revalidations"]:
        if (not isinstance(revalidation, dict)
                or set(revalidation) != {"operation_identity", "snapshot_digest", "native_snapshot", "turn_id",
                                         "turn_recorded_at", "observed_at"}
                or not _hex(revalidation.get("operation_identity"), 64)
                or not _hex(revalidation.get("snapshot_digest"), 64)
                or not isinstance(revalidation.get("native_snapshot"), dict)
                or not isinstance(revalidation["native_snapshot"].get("turn"), dict)
                or not isinstance(revalidation.get("turn_id"), str) or not revalidation["turn_id"]
                or not _timestamp(revalidation.get("turn_recorded_at"))
                or not _timestamp(revalidation.get("observed_at"))
                or digest(revalidation["native_snapshot"]) != revalidation["snapshot_digest"]
                or operation_identity(revalidation["native_snapshot"]) != revalidation["operation_identity"]
                or revalidation["native_snapshot"].get("turn", {}).get("id") != revalidation["turn_id"]
                or revalidation["native_snapshot"].get("turn", {}).get("recorded_at") != revalidation["turn_recorded_at"]
                or revalidation["native_snapshot"].get("observed_at") != revalidation["observed_at"]
                or compare_native_snapshots(actor_scope, baseline,
                                            revalidation["native_snapshot"])["owner_binding"]["status"] != "ready"
                or compare_native_snapshots(actor_scope, baseline,
                                            revalidation["native_snapshot"])["native_settings_match"]["status"] != "ready"):
            raise AccessObservationError("A pending access revalidation is malformed.")
    for target in observation["write_plan"]:
        _validate_target(target, observation["id"])
    write_check = checks["write_access"]
    if set(write_check) != {"status", "reason", "targets"} or not isinstance(write_check.get("targets"), list):
        raise AccessObservationError("The scoped write check is malformed.")
    if not request["probe_write"]:
        if (observation["write_plan"] or write_check != {
                "status": "not_assessed", "reason": "not_requested", "targets": []}):
            raise AccessObservationError("An unrequested write check cannot contain probe evidence.")
    elif observation["write_plan"] and write_check.get("reason") != "probe_pending":
        expected_write, _cleanup_status = summarize_write_targets(observation["write_plan"])
        if write_check != expected_write:
            raise AccessObservationError("The scoped write result does not match its retained probe targets.")
    elif request["probe_write"] and not observation["write_plan"]:
        if (write_check != {"status": "unknown", "reason": "owner_or_settings_mismatch", "targets": []}
                and checks["owner_binding"]["status"] == "ready"
                and checks["native_settings_match"]["status"] == "ready"):
            raise AccessObservationError("A matching owner and settings require a retained write-probe plan.")
        if write_check.get("status") != "unknown" or write_check.get("reason") != "owner_or_settings_mismatch":
            raise AccessObservationError("An unperformed write probe cannot claim access evidence.")
    if (not isinstance(observation.get("cleanup"), dict)
            or not isinstance(observation["cleanup"].get("targets"), list)):
        raise AccessObservationError("The scoped cleanup target list is malformed.")
    if observation["write_plan"] and observation["checks"]["write_access"].get("reason") != "probe_pending":
        expected_cleanup = [{"index": target["index"], "scopes": target["scopes"],
                             "status": target["cleanup_status"], "reason": target["reason"]}
                            for target in observation["write_plan"]]
        if observation["cleanup"]["targets"] != expected_cleanup:
            raise AccessObservationError("The cleanup summary does not match its probe targets.")
    pending = observation["state"] == "cleanup_incomplete"
    if pending != (observation["cleanup"]["status"] == "incomplete"):
        raise AccessObservationError("Incomplete cleanup must stay attached to its access operation.")
    if pending and (not observation["write_plan"]
                    or all(target["cleanup_status"] == "verified" for target in observation["write_plan"])):
        raise AccessObservationError("An access operation cannot claim pending cleanup without a retained target.")
    return True


def _validate_target(target, observation_id):
    fields = {"index", "path", "scopes", "device", "inode", "probe_name", "marker", "file_identity",
              "stage", "status", "cleanup_status", "reason", "evidence"}
    if (not isinstance(target, dict) or set(target) != fields
            or type(target.get("index")) is not int or target["index"] < 0
            or not isinstance(target.get("path"), str) or not os.path.isabs(target["path"])
            or not isinstance(target.get("scopes"), list) or not target["scopes"]
            or any(value not in ("worktree", "git_dir", "common_dir") for value in target["scopes"])
            or target.get("stage") not in ("planned", "created", "access_verified", "complete", "failed",
                                              "identity_unverified")
            or target.get("status") not in ACCESS_STATES
            or target.get("cleanup_status") not in ("not_started", "incomplete", "verified")
            or not isinstance(target.get("reason"), str)):
        raise AccessObservationError("A scoped write-probe target is malformed.")
    for name in ("device", "inode"):
        if target[name] is not None and (type(target[name]) is not int or target[name] < 0):
            raise AccessObservationError("A scoped write-probe target identity is malformed.")
    identity = target.get("file_identity")
    if identity is not None and (not isinstance(identity, dict) or set(identity) != {"device", "inode"}
                                 or any(type(identity[name]) is not int or identity[name] < 0
                                        for name in ("device", "inode"))):
        raise AccessObservationError("A scoped write-probe file identity is malformed.")
    if target["stage"] != "failed" and (not isinstance(target.get("probe_name"), str)
                                          or target["probe_name"] != f"{PROBE_PREFIX}{observation_id}-{target['index']}"
                                          or not isinstance(target.get("marker"), str)
                                          or target["marker"] != f"{PROBE_MARKER_PREFIX}{observation_id}:{target['index']}" ):
        raise AccessObservationError("A scoped write-probe path is malformed.")
    if target["stage"] in ("created", "access_verified", "complete") and identity is None:
        raise AccessObservationError("A scoped write-probe file identity is required after creation.")
    if target["stage"] == "identity_unverified" and target["cleanup_status"] != "incomplete":
        raise AccessObservationError("An unverified probe collision must keep cleanup incomplete.")
    if target["stage"] == "complete" and target["cleanup_status"] != "verified":
        raise AccessObservationError("A completed write probe must have verified cleanup.")
    if target["stage"] == "complete" and (target["status"] != "ready" or not isinstance(target.get("evidence"), dict)):
        raise AccessObservationError("A completed write probe must retain verified byte evidence.")


def _validate_input_check(check, paths):
    if (not isinstance(check, dict) or set(check) != {"status", "reason", "items"}
            or check.get("status") not in ACCESS_STATES or not isinstance(check.get("items"), list)
            or len(check["items"]) != len(paths)):
        raise AccessObservationError("An explicit byte-read check is malformed.")
    allowed = {
        "ready": {"bytes_read"},
        "blocked": {"read_failed", "symlink_refused", "not_regular_file"},
        "unknown": {"missing", "size_limit", "changed_during_read", "empty_or_size_limit", "read_failed"},
    }
    for item, path in zip(check["items"], paths):
        if (not isinstance(item, dict) or set(item) != {"path", "status", "reason", "bytes_read", "sha256"}
                or item.get("path") != path or item.get("status") not in allowed
                or item.get("reason") not in allowed[item["status"]]):
            raise AccessObservationError("An explicit byte-read item is malformed.")
        size = item.get("bytes_read")
        checksum = item.get("sha256")
        if item["status"] == "ready":
            if type(size) is not int or not (0 < size <= MAX_ACCESS_INPUT_BYTES) or not _hex(checksum, 64):
                raise AccessObservationError("A successful byte-read item is malformed.")
        elif checksum is not None or size is not None and (type(size) is not int or size < 0):
            raise AccessObservationError("An unsuccessful byte-read item contains invalid evidence.")
    expected_status = _aggregate(check["items"])
    if check["status"] != expected_status:
        raise AccessObservationError("A byte-read aggregate does not match its item results.")
    if not paths and check.get("reason") != "no_explicit_paths":
        raise AccessObservationError("An omitted byte-read class must remain unidentified.")


def _valid_native_snapshot(snapshot, expected_subject):
    if not isinstance(snapshot, dict):
        return False
    snapshot_fields = {"protocol", "observed_at", "subject", "hook_generation", "process_identity",
                       "hook_source", "rollout_source", "turn", "settings"}
    process_fields = {"pid", "parent_pid", "started_at", "executable"}
    hook_fields = {"kind", "path", "device", "inode", "modified_at_ns", "updated_at"}
    rollout_fields = {"kind", "path", "device", "inode", "size", "modified_at_ns", "cli_version",
                      "session_meta_cwd", "session_meta_source", "session_meta_originator"}
    turn_fields = {"id", "recorded_at", "cwd"}
    settings_fields = {"model", "effort", "approval_policy", "sandbox_policy"}
    process = snapshot.get("process_identity")
    hook = snapshot.get("hook_source")
    rollout = snapshot.get("rollout_source")
    turn = snapshot.get("turn")
    settings = snapshot.get("settings")
    if (set(snapshot) != snapshot_fields
            or snapshot.get("protocol") != "starfleet.replicator.native-observation/codex-rollout/v1"
            or not _timestamp(snapshot.get("observed_at"))
            or snapshot.get("subject") != expected_subject
            or not isinstance(snapshot.get("hook_generation"), str) or not snapshot["hook_generation"]
            or not isinstance(process, dict) or set(process) != process_fields
            or type(process.get("pid")) is not int or process["pid"] <= 0
            or type(process.get("parent_pid")) is not int or process["parent_pid"] <= 0
            or not _timestamp(process.get("started_at"))
            or not isinstance(process.get("executable"), str) or not os.path.isabs(process["executable"])
            or Path(process["executable"]).name != "codex"
            or not isinstance(hook, dict) or set(hook) != hook_fields
            or hook.get("kind") != "cmux_codex_hook_store_v1"
            or not isinstance(hook.get("path"), str) or not os.path.isabs(hook["path"])
            or any(type(hook.get(name)) is not int or hook[name] < 0
                   for name in ("device", "inode", "modified_at_ns"))
            or type(hook.get("updated_at")) not in (int, float) or not math.isfinite(hook["updated_at"])
            or not isinstance(rollout, dict) or set(rollout) != rollout_fields
            or rollout.get("kind") != "open_codex_rollout"
            or not isinstance(rollout.get("path"), str) or not os.path.isabs(rollout["path"])
            or any(type(rollout.get(name)) is not int or rollout[name] < 0
                   for name in ("device", "inode", "size", "modified_at_ns"))
            or not all(isinstance(rollout.get(name), str) and rollout[name]
                       for name in ("cli_version", "session_meta_cwd", "session_meta_source", "session_meta_originator"))
            or not isinstance(turn, dict) or set(turn) != turn_fields
            or not isinstance(turn.get("id"), str) or not turn["id"]
            or not _timestamp(turn.get("recorded_at"))
            or not isinstance(turn.get("cwd"), str) or not isinstance(settings, dict)
            or set(settings) != settings_fields
            or any(not isinstance(settings.get(name), str) or not settings[name]
                   for name in ("model", "effort", "approval_policy"))
            or not isinstance(settings.get("sandbox_policy"), dict) or not settings["sandbox_policy"]
            or not _json_value(settings["sandbox_policy"])):
        return False
    return True


def _json_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    return False


def _timestamp(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        from datetime import datetime
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _hex(value, length):
    return (isinstance(value, str) and len(value) == length
            and all(character in "0123456789abcdef" for character in value))
