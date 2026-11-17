"""Durable, exact-session continuation timers for Hailing Frequencies."""
from datetime import datetime, timezone
import contextlib
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import subprocess
import time
import uuid
from typing import Optional, Tuple


CHECKPOINT_IDLE_KEY = "pattern-buffer-checkpoint-idle"
RETIREMENT_PLAN_LIMIT = 1024 * 1024
RETIREMENT_TIMER_LIMIT = 256


def require(condition, message):
    if not condition:
        raise ValueError(message)


def duration(value):
    require(isinstance(value, str) and value.strip(), "Duration is required, for example 15m or 2h")
    text = value.strip().lower()
    unit = text[-1]
    factors = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    require(unit in factors, "Use a duration ending in s, m, h, or d")
    try:
        amount = float(text[:-1])
    except ValueError:
        raise ValueError("Invalid duration") from None
    require(math.isfinite(amount) and amount > 0, "Duration must be finite and greater than zero")
    seconds = amount * factors[unit]
    require(math.isfinite(seconds) and seconds > 0, "Duration must resolve to a finite positive number of seconds")
    return seconds


def offset_datetime(value):
    require(isinstance(value, str) and value.strip(), "Timestamp is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Use an ISO 8601 timestamp with an explicit UTC offset") from None
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
            "Timestamp needs an explicit UTC offset, such as -07:00 or Z")
    try:
        return parsed.timestamp(), parsed
    except (OverflowError, OSError, ValueError):
        raise ValueError("Timestamp is outside the supported range") from None


def timestamp(value):
    try:
        return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        raise ValueError("Timestamp is outside the supported range") from None


@dataclass(frozen=True)
class OwnerSnapshot:
    harness: str
    session: str
    surface: str
    workspace: str
    started_at: float
    owner_key: str
    canonical_worktree: Optional[str]


@dataclass(frozen=True)
class OccurrenceSnapshot:
    id: str
    sequence: int
    due_at: float
    status: str
    attempted_at: Optional[float]
    received_at: Optional[float]
    continuation_sha256: str
    detail_sha256: Optional[str]


@dataclass(frozen=True)
class TimerSnapshot:
    id: str
    owner: OwnerSnapshot
    worktree_root: str
    git_dir: str
    git_dir_device: int
    git_dir_inode: int
    continuation_sha256: str
    schedule: str
    interval: Optional[float]
    due_at: float
    until_at: Optional[float]
    status: str
    request_id: Optional[str]
    replacement_key: str
    created_at: float
    updated_at: float
    schedule_spec_sha256: str
    occurrences: Tuple[OccurrenceSnapshot, ...]


@dataclass(frozen=True)
class RetirementPlan:
    version: int
    state_directory: str
    created_at: float
    authorization_ref: str
    coordinator: OwnerSnapshot
    timers: Tuple[TimerSnapshot, ...]
    sha256: str

    def body(self):
        return {
            "version": self.version,
            "state_directory": self.state_directory,
            "created_at": self.created_at,
            "authorization_ref": self.authorization_ref,
            "coordinator": asdict(self.coordinator),
            "timers": [asdict(timer) for timer in self.timers],
        }

    def document(self):
        return {**self.body(), "sha256": self.sha256}

    @staticmethod
    def digest(body):
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


class TemporalBeacon:
    def __init__(self, app, clock=time.time):
        self.app = app
        self.directory = app.directory
        self.clock = clock

    @contextlib.contextmanager
    def locked(self):
        path = self.directory / "timers.lock"
        require(not path.is_symlink(), "Refusing symlink timer lock")
        with path.open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another Temporal Beacon operation owns timer state; retry shortly") from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def database(self):
        path = self.directory / "receipts.sqlite3"
        require(not path.is_symlink(), "Refusing symlink database")
        db = sqlite3.connect(path, timeout=15, isolation_level=None)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS timers (
          id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, harness TEXT NOT NULL, session TEXT NOT NULL,
          surface TEXT NOT NULL, workspace TEXT NOT NULL, started_at REAL NOT NULL,
          canonical_worktree TEXT, continuation TEXT NOT NULL, schedule TEXT NOT NULL,
          interval REAL, due_at REAL NOT NULL, until_at REAL, status TEXT NOT NULL,
          request_id TEXT UNIQUE, replacement_key TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
          schedule_spec TEXT NOT NULL DEFAULT '{}'
        );
        CREATE UNIQUE INDEX IF NOT EXISTS timers_live_replacement
          ON timers(owner_key,replacement_key) WHERE replacement_key IS NOT NULL AND status='active';
        CREATE TABLE IF NOT EXISTS timer_occurrences (
          id TEXT PRIMARY KEY, timer_id TEXT NOT NULL REFERENCES timers(id), sequence INTEGER NOT NULL,
          due_at REAL NOT NULL, status TEXT NOT NULL, continuation TEXT NOT NULL,
          attempted_at REAL, received_at REAL, detail TEXT,
          UNIQUE(timer_id,sequence)
        );
        CREATE INDEX IF NOT EXISTS timer_occurrences_current
          ON timer_occurrences(timer_id,sequence DESC);
        CREATE TABLE IF NOT EXISTS retired_checkpoint_timer_keys (
          harness TEXT NOT NULL, session TEXT NOT NULL, surface TEXT NOT NULL, workspace TEXT NOT NULL,
          started_at REAL NOT NULL, worktree_root TEXT NOT NULL, replacement_key TEXT NOT NULL,
          git_dir TEXT NOT NULL, plan_sha256 TEXT NOT NULL, retired_at REAL NOT NULL,
          PRIMARY KEY(harness,session,surface,workspace,started_at,worktree_root,replacement_key)
        );
        CREATE TABLE IF NOT EXISTS timer_retirement_receipts (
          plan_sha256 TEXT PRIMARY KEY, coordinator_owner_key TEXT NOT NULL,
          authorization_ref TEXT NOT NULL, applied_at REAL NOT NULL, result_json TEXT NOT NULL
        );
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(timers)")}
        if "schedule_spec" not in columns:
            db.execute("ALTER TABLE timers ADD COLUMN schedule_spec TEXT NOT NULL DEFAULT '{}'")
        return db

    def binding(self, target, expected_worktree=None):
        self.app.cmux.binding(target)
        return self._verify_expected_worktree(target, expected_worktree)

    def _verify_expected_worktree(self, target, expected_worktree):
        if expected_worktree is not None:
            path = self.app.cmux.hook_directory / (target["harness"] + "-hook-sessions.json")
            store = json.loads(path.read_text())
            record = next((value for key, value in store["sessions"].items()
                           if (key == target["session"] if target["harness"] == "opencode"
                               else key.upper() == target["session"])), {})
            current = record.get("cwd") or record.get("workingDirectory")
            require(isinstance(current, str) and current,
                    "Canonical worktree is no longer present in the native session record")
            require(str(Path(current).expanduser().resolve()) == expected_worktree,
                    "Canonical worktree changed")
        return True

    def _creation_owner(self):
        """Verify the exact owner while allowing OpenCode's API session to be busy."""
        target, owner_key, worktree = self.owner(verify=False)
        if target["harness"] == "opencode":
            self.app.cmux.enrollment_binding(target)
            self._verify_expected_worktree(target, worktree)
        else:
            self.binding(target, worktree)
        return target, owner_key, worktree

    @staticmethod
    def owner_key(target, worktree):
        value = ":".join((target["harness"], target["session"], target["surface"],
                           target["workspace"], str(target["started_at"]), worktree or "unknown-worktree"))
        if target["harness"] == "opencode":
            binding = {key: target[key] for key in (
                "api_url", "password_file", "expected_server_pid", "session_directory",
                "worktree_directory", "parent_task_id")}
            encoded = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
            value += ":opencode-binding:" + hashlib.sha256(encoded).hexdigest()
        return value

    def owner(self, verify=True):
        import hailing_frequencies as hf
        harness = os.environ.get("CMUX_AGENT_LAUNCH_KIND", "").lower()
        require(harness in ("claude", "codex", "opencode"),
                "Cannot verify this caller. Enroll the exact Claude, Codex, or OpenCode session before using Temporal Beacon.")
        env_key = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID",
                   "opencode": "OPENCODE_SESSION_ID"}[harness]
        session_value = os.environ.get(env_key)
        surface_value = os.environ.get("CMUX_SURFACE_ID")
        require(surface_value, "Missing verified CMUX_SURFACE_ID")
        surface = hf.identity(surface_value)
        config = self.app.config()
        workspace_value = os.environ.get("CMUX_WORKSPACE_ID")
        workspace = hf.identity(workspace_value) if workspace_value else None
        if harness == "opencode":
            require(session_value, "OPENCODE_SESSION_ID is required for exact OpenCode timer ownership")
            require(workspace is not None, "CMUX_WORKSPACE_ID is required for exact OpenCode timer ownership")
            session = hf.session_identity(session_value, harness)
            candidates = [item for item in config["targets"] if item["harness"] == harness and
                          item["surface"] == surface and
                          item["workspace"] == workspace and item["session"] == session]
            require(len(candidates) == 1,
                    "OpenCode owner is missing or does not match the exact session, surface, and workspace")
            target = candidates[0]
            require(os.environ.get("CMUX_AGENT_LAUNCH_KIND", "").lower() == "opencode",
                    "Caller launch kind does not match the enrolled OpenCode session")
            require(workspace == target["workspace"], "CMUX_WORKSPACE_ID is required and must match OpenCode enrollment")
        else:
            require(session_value,
                    f"Missing verified {env_key}. Enroll the observed session with hailing-frequencies enroll --harness {harness} --surface SURFACE --session SESSION --label NAME.")
            session = hf.session_identity(session_value, harness)
            target = next((item for item in config["targets"] if item["session"] == session), None)
        require(target is not None,
                f"Session {session} is not enrolled. Enroll it with hailing-frequencies enroll --harness {harness} --surface {surface} --session {session} --label temporal-beacon.")
        require(target["harness"] == harness and target["surface"] == surface,
                "Caller harness or surface differs from its enrolled exact owner")
        hook_path = self.app.cmux.hook_directory / (harness + "-hook-sessions.json")
        store = json.loads(hook_path.read_text())
        record = next((value for key, value in store["sessions"].items()
                       if (key == session if harness == "opencode" else key.upper() == session)), {})
        worktree = record.get("cwd") or record.get("workingDirectory")
        if isinstance(worktree, str) and worktree:
            worktree = str(Path(worktree).expanduser().resolve())
        else:
            worktree = None
        if verify:
            self.binding(target, worktree)
        key = self.owner_key(target, worktree)
        return target, key, worktree

    def _retirement_coordinator(self):
        target, owner_key, worktree = self.owner()
        config = self.app.config()
        require(config.get("coordinator") == target["session"],
                "Timer retirement requires the currently enrolled Hailing Frequencies coordinator")
        return OwnerSnapshot(target["harness"], target["session"], target["surface"],
                             target["workspace"], target["started_at"], owner_key, worktree)

    @staticmethod
    def _git_identity(worktree):
        require(isinstance(worktree, str) and worktree,
                "Checkpoint idle timer retirement requires a recorded Git worktree")
        candidate = Path(worktree).expanduser()
        require(candidate.is_absolute(), "Recorded checkpoint worktree is not absolute")
        try:
            resolved = candidate.resolve(strict=True)
            require(resolved.is_dir(), "Recorded checkpoint worktree is not a directory")
            result = subprocess.run(["git", "-C", str(resolved), "rev-parse",
                                     "--show-toplevel", "--absolute-git-dir"],
                                    capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError, RuntimeError):
            raise ValueError("Cannot verify the recorded checkpoint Git worktree") from None
        require(result.returncode == 0, "Cannot verify the recorded checkpoint Git worktree")
        lines = result.stdout.splitlines()
        require(len(lines) == 2 and all(lines), "Git returned an incomplete worktree identity")
        try:
            root = Path(lines[0]).resolve(strict=True)
            git_dir = Path(lines[1]).resolve(strict=True)
            contained = resolved == root or root in resolved.parents
        except (OSError, RuntimeError):
            raise ValueError("Recorded checkpoint Git identity changed") from None
        require(root.is_dir() and git_dir.is_dir() and contained,
                "Recorded checkpoint path is outside its Git worktree")
        git_info = git_dir.stat()
        return str(root), str(git_dir), git_info.st_dev, git_info.st_ino

    @staticmethod
    def _sha256_text(value):
        require(isinstance(value, str), "Timer text metadata is malformed")
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _timer_snapshot(self, db, timer_id, require_active=True):
        row = db.execute("SELECT * FROM timers WHERE id=?", (timer_id,)).fetchone()
        require(row is not None, "A named timer no longer exists")
        timer = self._timer_dict(row)
        require(timer["harness"] == "claude" and timer["schedule"] == "idle"
                and timer["replacement_key"] == CHECKPOINT_IDLE_KEY,
                "Only Pattern Buffer checkpoint idle timers can be retired")
        if require_active:
            require(timer["status"] == "active",
                    "Only active Pattern Buffer checkpoint idle timers can be retired")
        require(timer["canonical_worktree"], "Timer has no recorded Git worktree")
        target = {key: timer[key] for key in ("harness", "session", "surface", "workspace", "started_at")}
        expected_owner_key = self.owner_key(target, timer["canonical_worktree"])
        require(timer["owner_key"] == expected_owner_key,
                "Timer owner snapshot does not match its recorded fields")
        worktree_root, git_dir, git_dir_device, git_dir_inode = self._git_identity(timer["canonical_worktree"])
        occurrences = []
        for item in db.execute(
                "SELECT id,timer_id,sequence,due_at,status,continuation,attempted_at,received_at,detail "
                "FROM timer_occurrences WHERE timer_id=? ORDER BY sequence", (timer_id,)).fetchall():
            occurrence_id, _parent_id, sequence, due_at, status, continuation, attempted_at, received_at, detail = item
            require(status in ("pending", "held", "submitted", "uncertain", "received", "canceled", "expired"),
                    "Timer occurrence has an unknown state")
            occurrences.append(OccurrenceSnapshot(
                occurrence_id, sequence, due_at, status, attempted_at, received_at,
                self._sha256_text(continuation), self._sha256_text(detail) if detail is not None else None))
        return TimerSnapshot(
            timer["id"],
            OwnerSnapshot(timer["harness"], timer["session"], timer["surface"], timer["workspace"],
                          timer["started_at"], timer["owner_key"], timer["canonical_worktree"]),
            worktree_root, git_dir, git_dir_device, git_dir_inode,
            self._sha256_text(timer["continuation"]), timer["schedule"],
            timer["interval"], timer["due_at"], timer["until_at"], timer["status"],
            timer["request_id"], timer["replacement_key"], timer["created_at"], timer["updated_at"],
            self._sha256_text(timer["schedule_spec"]), tuple(occurrences))

    @staticmethod
    def _scope(snapshot):
        owner = snapshot.owner
        return (owner.harness, owner.session, owner.surface, owner.workspace, owner.started_at,
                snapshot.worktree_root, snapshot.replacement_key)

    def _active_timer_ids_for_scope(self, db, scope):
        harness, session, surface, workspace, started_at, worktree_root, replacement_key = scope
        rows = db.execute(
            "SELECT id,canonical_worktree FROM timers WHERE harness=? AND session=? AND surface=? "
            "AND workspace=? AND started_at=? AND replacement_key=? AND status='active'",
            (harness, session, surface, workspace, started_at, replacement_key)).fetchall()
        active = set()
        for timer_id, worktree in rows:
            resolved_root, _git_dir, _git_dir_device, _git_dir_inode = self._git_identity(worktree)
            if resolved_root == worktree_root:
                active.add(timer_id)
        return active

    @staticmethod
    def _authorization_ref(value):
        require(isinstance(value, str) and value.strip(), "An explicit authorization reference is required")
        value = value.strip()
        require(len(value.encode("utf-8")) <= 512 and not any(ord(char) < 32 for char in value),
                "Authorization reference must be one line of at most 512 UTF-8 bytes")
        return value

    def _plan_directory(self):
        require(not self.directory.is_symlink() and self.directory.is_dir(),
                "Hailing Frequencies state directory must be a real directory")
        directory = self.directory / "retirement-plans"
        require(not directory.is_symlink(), "Refusing symlink retirement-plan directory")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                and info.st_mode & 0o077 == 0,
                "Retirement-plan directory must be private and owned by the current user")
        return directory

    def _write_plan(self, plan):
        directory = self._plan_directory()
        path = directory / (plan.sha256 + ".json")
        raw = (json.dumps(plan.document(), indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if path.exists():
            require(not path.is_symlink() and path.read_bytes() == raw,
                    "An existing retirement plan does not match its digest")
            return path
        temporary = directory / ("." + plan.sha256 + "." + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except FileExistsError:
            require(path.is_file() and not path.is_symlink() and path.read_bytes() == raw,
                    "An existing retirement plan does not match its digest")
        finally:
            temporary.unlink(missing_ok=True)
        return path

    @staticmethod
    def _owner_from_dict(value):
        expected = {"harness", "session", "surface", "workspace", "started_at", "owner_key",
                    "canonical_worktree"}
        require(isinstance(value, dict) and set(value) == expected, "Retirement plan owner is malformed")
        require(value["harness"] in ("claude", "codex", "opencode")
                and all(isinstance(value[key], str) and value[key] for key in
                        ("session", "surface", "workspace", "owner_key")),
                "Retirement plan owner identity is malformed")
        require(type(value["started_at"]) in (int, float) and math.isfinite(value["started_at"])
                and (value["canonical_worktree"] is None or
                     isinstance(value["canonical_worktree"], str) and value["canonical_worktree"]),
                "Retirement plan owner timestamps or worktree are malformed")
        return OwnerSnapshot(**value)

    @staticmethod
    def _plan_hash(value, label):
        require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
                f"Retirement plan {label} is malformed")

    @staticmethod
    def _optional_timestamp(value, label):
        require(value is None or type(value) in (int, float) and math.isfinite(value),
                f"Retirement plan {label} is malformed")

    @classmethod
    def _plan_from_document(cls, document):
        expected = {"version", "state_directory", "created_at", "authorization_ref", "coordinator",
                    "timers", "sha256"}
        require(isinstance(document, dict) and set(document) == expected,
                "Retirement plan has unknown or missing fields")
        owner = cls._owner_from_dict
        timer_fields = {"id", "owner", "worktree_root", "git_dir", "git_dir_device", "git_dir_inode",
                        "continuation_sha256", "schedule",
                        "interval", "due_at", "until_at", "status", "request_id", "replacement_key",
                        "created_at", "updated_at", "schedule_spec_sha256", "occurrences"}
        occurrence_fields = {"id", "sequence", "due_at", "status", "attempted_at", "received_at",
                             "continuation_sha256", "detail_sha256"}
        timers = []
        require(isinstance(document["timers"], list) and 0 < len(document["timers"]) <= RETIREMENT_TIMER_LIMIT,
                "Retirement plan timer list is malformed")
        for value in document["timers"]:
            require(isinstance(value, dict) and set(value) == timer_fields,
                    "Retirement plan timer entry is malformed")
            require(isinstance(value["occurrences"], list), "Retirement plan occurrences are malformed")
            require(all(isinstance(value[key], str) and value[key] for key in
                        ("id", "worktree_root", "git_dir", "schedule", "status", "replacement_key"))
                    and Path(value["worktree_root"]).is_absolute() and Path(value["git_dir"]).is_absolute(),
                    "Retirement plan timer identity is malformed")
            require(value["schedule"] == "idle" and value["status"] == "active"
                    and value["replacement_key"] == CHECKPOINT_IDLE_KEY,
                    "Retirement plan contains a timer outside the checkpoint idle scope")
            require(type(value["git_dir_device"]) is int and value["git_dir_device"] >= 0
                    and type(value["git_dir_inode"]) is int and value["git_dir_inode"] >= 0,
                    "Retirement plan Git directory identity is malformed")
            require(value["request_id"] is None or isinstance(value["request_id"], str),
                    "Retirement plan request ID is malformed")
            for key in ("interval", "until_at"):
                cls._optional_timestamp(value[key], key)
            for key in ("due_at", "created_at", "updated_at"):
                require(type(value[key]) in (int, float) and math.isfinite(value[key]),
                        f"Retirement plan {key} is malformed")
            cls._plan_hash(value["continuation_sha256"], "continuation digest")
            cls._plan_hash(value["schedule_spec_sha256"], "schedule digest")
            occurrences = []
            for item in value["occurrences"]:
                require(isinstance(item, dict) and set(item) == occurrence_fields,
                        "Retirement plan occurrence is malformed")
                require(isinstance(item["id"], str) and item["id"]
                        and type(item["sequence"]) is int and item["sequence"] > 0
                        and type(item["due_at"]) in (int, float) and math.isfinite(item["due_at"])
                        and item["status"] in ("pending", "held", "submitted", "uncertain", "received",
                                                "canceled", "expired"),
                        "Retirement plan occurrence identity is malformed")
                cls._optional_timestamp(item["attempted_at"], "occurrence attempt time")
                cls._optional_timestamp(item["received_at"], "occurrence receipt time")
                cls._plan_hash(item["continuation_sha256"], "occurrence continuation digest")
                if item["detail_sha256"] is not None:
                    cls._plan_hash(item["detail_sha256"], "occurrence detail digest")
                occurrences.append(OccurrenceSnapshot(**item))
            require(len({item.id for item in occurrences}) == len(occurrences)
                    and len({item.sequence for item in occurrences}) == len(occurrences),
                    "Retirement plan contains duplicate occurrences")
            timers.append(TimerSnapshot(**{
                **{key: value[key] for key in timer_fields - {"owner", "occurrences"}},
                "owner": owner(value["owner"]), "occurrences": tuple(occurrences)}))
        require(all(timer.owner.harness == "claude" and timer.owner.canonical_worktree
                    and Path(timer.owner.canonical_worktree).is_absolute() for timer in timers),
                "Retirement plan owner worktree is malformed")
        require(type(document["version"]) is int and type(document["created_at"]) in (int, float)
                and math.isfinite(document["created_at"])
                and isinstance(document["state_directory"], str)
                and Path(document["state_directory"]).is_absolute(),
                "Retirement plan metadata is malformed")
        authorization_ref = cls._authorization_ref(document["authorization_ref"])
        require(authorization_ref == document["authorization_ref"],
                "Retirement plan authorization reference is malformed")
        cls._plan_hash(document["sha256"], "SHA-256")
        plan = RetirementPlan(document["version"], document["state_directory"], document["created_at"],
                              authorization_ref, owner(document["coordinator"]),
                              tuple(timers), document["sha256"])
        require(plan.version == 1 and RetirementPlan.digest(plan.body()) == plan.sha256,
                "Retirement plan digest is invalid")
        require(len({timer.id for timer in plan.timers}) == len(plan.timers),
                "Retirement plan contains duplicate timers")
        return plan

    def _read_plan(self, path):
        path = Path(path).expanduser()
        require(not path.is_symlink(), "Refusing symlink retirement plan")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            raise ValueError("Retirement plan is unavailable") from None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and info.st_mode & 0o077 == 0 and info.st_size <= RETIREMENT_PLAN_LIMIT,
                    "Retirement plan must be a private regular file under one MiB")
            raw = stream.read(RETIREMENT_PLAN_LIMIT + 1)
        require(len(raw) <= RETIREMENT_PLAN_LIMIT, "Retirement plan exceeds one MiB")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise ValueError("Retirement plan is not valid UTF-8 JSON") from None
        plan = self._plan_from_document(document)
        directory = self._plan_directory().resolve()
        require(path.resolve(strict=True).parent == directory
                and path.name == plan.sha256 + ".json",
                "Retirement plan must remain in this Hailing Frequencies state directory")
        return plan

    def prepare_retirement_plan(self, timer_ids, authorization_ref):
        authorization_ref = self._authorization_ref(authorization_ref)
        require(isinstance(timer_ids, list) and 0 < len(timer_ids) <= RETIREMENT_TIMER_LIMIT
                and all(isinstance(value, str) and value for value in timer_ids),
                "Name between one and 256 exact timer IDs")
        require(len(set(timer_ids)) == len(timer_ids), "Timer IDs must be unique")
        coordinator = self._retirement_coordinator()
        state_directory = str(self.directory.resolve())
        with self.locked(), contextlib.closing(self.database()) as db:
            db.execute("BEGIN")
            timers = tuple(self._timer_snapshot(db, timer_id) for timer_id in timer_ids)
            scopes = {self._scope(timer) for timer in timers}
            for scope in scopes:
                matching = [timer for timer in timers if self._scope(timer) == scope]
                planned = {timer.id for timer in matching}
                require(len({(timer.git_dir, timer.git_dir_device, timer.git_dir_inode)
                             for timer in matching}) == 1,
                        "Timer aliases resolve to different Git identities")
                require(self._active_timer_ids_for_scope(db, scope) == planned,
                        "The exact active timer set changed; include every matching timer and create a new plan")
            db.commit()
        base = RetirementPlan(1, state_directory, self.clock(), authorization_ref, coordinator, timers, "")
        plan = RetirementPlan(**{**base.__dict__, "sha256": RetirementPlan.digest(base.body())})
        path = self._write_plan(plan)
        cancelable = sum(item.status in ("pending", "held") for timer in timers for item in timer.occurrences)
        preserved = {}
        for timer in timers:
            for item in timer.occurrences:
                if item.status not in ("pending", "held"):
                    preserved[item.status] = preserved.get(item.status, 0) + 1
        return {"plan": str(path), "sha256": plan.sha256, "timer_count": len(timers),
                "cancelable_occurrences": cancelable, "preserved_occurrences": preserved,
                "authorization_ref": authorization_ref}

    def apply_retirement_plan(self, path, confirmation, authorization_ref):
        coordinator = self._retirement_coordinator()
        plan = self._read_plan(path)
        require(confirmation == plan.sha256, "Confirm the exact retirement plan SHA-256")
        require(self._authorization_ref(authorization_ref) == plan.authorization_ref,
                "Authorization reference differs from the reviewed plan")
        require(plan.state_directory == str(self.directory.resolve()),
                "Retirement plan belongs to another Hailing Frequencies state directory")
        require(coordinator == plan.coordinator,
                "Retirement plan belongs to another exact coordinator identity")
        now = self.clock()
        with self.app.locked(), self.locked(), contextlib.closing(self.database()) as db:
            current_coordinator = self._retirement_coordinator()
            require(current_coordinator == coordinator,
                    "Coordinator identity changed before timer retirement")
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT coordinator_owner_key,authorization_ref,result_json "
                "FROM timer_retirement_receipts WHERE plan_sha256=?", (plan.sha256,)).fetchone()
            if prior is not None:
                require(prior[0] == coordinator.owner_key and prior[1] == plan.authorization_ref,
                        "Retirement receipt conflicts with the exact coordinator or authorization")
                result = json.loads(prior[2])
                db.commit()
                return {**result, "idempotent": True}

            scopes = {self._scope(timer) for timer in plan.timers}
            for timer in plan.timers:
                current = self._timer_snapshot(db, timer.id, require_active=False)
                require(current == timer, "Timer or occurrence state changed after plan review")
            for scope in scopes:
                matching = [timer for timer in plan.timers if self._scope(timer) == scope]
                planned = {timer.id for timer in matching}
                require(len({(timer.git_dir, timer.git_dir_device, timer.git_dir_inode)
                             for timer in matching}) == 1,
                        "Timer aliases resolve to different Git identities")
                require(self._active_timer_ids_for_scope(db, scope) == planned,
                        "The exact active timer set changed after plan review")
                harness, session, surface, workspace, started_at, worktree_root, replacement_key = scope
                existing = db.execute(
                    "SELECT plan_sha256 FROM retired_checkpoint_timer_keys WHERE harness=? AND session=? "
                    "AND surface=? AND workspace=? AND started_at=? AND worktree_root=? AND replacement_key=?",
                    (harness, session, surface, workspace, started_at, worktree_root, replacement_key)).fetchone()
                require(existing is None, "This exact checkpoint timer key already has a retirement tombstone")

            for scope in scopes:
                timer = next(timer for timer in plan.timers if self._scope(timer) == scope)
                db.execute(
                    "INSERT INTO retired_checkpoint_timer_keys "
                    "(harness,session,surface,workspace,started_at,worktree_root,replacement_key,git_dir,plan_sha256,retired_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (*scope[:7], timer.git_dir, plan.sha256, now))
            canceled_occurrences = {"pending": 0, "held": 0}
            preserved_occurrences = {}
            for timer in plan.timers:
                updated = db.execute(
                    "UPDATE timers SET status='canceled',updated_at=? WHERE id=? AND owner_key=? AND status='active'",
                    (now, timer.id, timer.owner.owner_key))
                require(updated.rowcount == 1, "A planned timer changed before retirement")
                for occurrence in timer.occurrences:
                    if occurrence.status in canceled_occurrences:
                        changed = db.execute(
                            "UPDATE timer_occurrences SET status='canceled' WHERE id=? AND timer_id=? AND status=?",
                            (occurrence.id, timer.id, occurrence.status))
                        require(changed.rowcount == 1, "A planned occurrence changed before retirement")
                        canceled_occurrences[occurrence.status] += 1
                    else:
                        preserved_occurrences[occurrence.status] = preserved_occurrences.get(occurrence.status, 0) + 1

            result = {"plan_sha256": plan.sha256, "timer_ids": [timer.id for timer in plan.timers],
                      "retired_timer_count": len(plan.timers),
                      "canceled_occurrences": canceled_occurrences,
                      "preserved_occurrences": preserved_occurrences,
                      "submitted_input_recallable": False,
                      "preserved_submitted_occurrences_retrievable": False,
                      "authorization_ref": plan.authorization_ref}
            db.execute(
                "INSERT INTO timer_retirement_receipts "
                "(plan_sha256,coordinator_owner_key,authorization_ref,applied_at,result_json) VALUES (?,?,?,?,?)",
                (plan.sha256, coordinator.owner_key, plan.authorization_ref, now,
                 json.dumps(result, sort_keys=True, separators=(",", ":"))))
            db.commit()
            return {**result, "idempotent": False}

    def _timer(self, db, timer_id):
        row = db.execute("SELECT * FROM timers WHERE id=?", (timer_id,)).fetchone()
        require(row is not None, "Timer not found")
        return self._timer_dict(row)

    @staticmethod
    def _timer_dict(row):
        keys = ("id", "owner_key", "harness", "session", "surface", "workspace", "started_at",
                "canonical_worktree", "continuation", "schedule", "interval", "due_at", "until_at",
                "status", "request_id", "replacement_key", "created_at", "updated_at", "schedule_spec")
        value = dict(zip(keys, row))
        for key in ("due_at", "until_at", "created_at", "updated_at"):
            if value[key] is not None:
                value[key + "_local"] = timestamp(value[key])
        return value

    @staticmethod
    def _occurrence_dict(row):
        keys = ("id", "timer_id", "sequence", "due_at", "status", "continuation",
                "attempted_at", "received_at", "detail")
        value = dict(zip(keys, row))
        value["due_at_local"] = timestamp(value["due_at"])
        return value

    def capability(self, target):
        try:
            service_state = json.loads((self.directory / "service.json").read_text())
            scan = json.loads((self.directory / "last-scan.json").read_text())
        except (OSError, ValueError, TypeError):
            service_state, scan = {}, {}
        try:
            timer_scan = json.loads((self.directory / "timer-last-scan.json").read_text())
            timer_scan_age = self.clock() - timer_scan["at"]
            interval = self.app.config()["interval"]
            timer_worker_ready = (type(timer_scan["at"]) in (int, float) and math.isfinite(timer_scan["at"])
                                  and 0 <= timer_scan_age <= max(120, interval * 4))
        except (OSError, ValueError, KeyError, TypeError):
            timer_scan_age, timer_worker_ready = None, False
        sending = scan.get("mode") == "send"
        scope = scan.get("sending_sessions")
        covered = scope is None or target["session"] in scope
        running = service_state.get("state") == "running"
        reason = None
        if not running:
            reason = "start the existing supervisor inside CMUX"
        elif not sending:
            reason = "supervisor is observe-only; restart it with --send"
        elif not covered:
            reason = "exact owner is outside the supervisor send scope"
        elif not timer_worker_ready:
            reason = "no fresh Temporal Beacon worker heartbeat; restart the current installed worker"
        transport = "guarded_opencode_api" if target["harness"] == "opencode" else "guarded_cmux"
        return {"transport": transport, "worker_running": running, "send_enabled": sending,
                "owner_in_send_scope": covered,
                "timer_worker_ready": timer_worker_ready, "timer_scan_age_seconds": timer_scan_age,
                "currently_covered": running and sending and covered and timer_worker_ready,
                "action": reason}

    def idle_due(self, target, seconds, now):
        activity = self.app.cmux.activity(target)
        require(isinstance(activity, dict), "Native activity is unavailable")
        require(activity.get("state") in ("idle", "working"), "Native idle state is unknown or inactive")
        last, _ = offset_datetime(activity.get("last_activity_at"))
        require(target["started_at"] <= last <= now + 2, "Native activity timestamp is stale or in the future")
        return (last if activity["state"] == "idle" else now) + seconds

    def require_idle(self, target, seconds):
        import hailing_frequencies as hf
        try:
            require(self.idle_due(target, seconds, self.clock()) <= self.clock(),
                    "Session became active before idle delivery")
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise hf.DeliveryHoldError(str(exc)) from exc

    def require_idle_worker(self, now):
        try:
            scan = json.loads((self.directory / "timer-last-scan.json").read_text())
            service = json.loads((self.directory / "service.json").read_text())
            require(isinstance(scan, dict) and isinstance(service, dict), "Worker records are malformed")
            age = now - scan["at"]
            ready = (scan.get("supports_idle") is True and 0 <= age <= max(120, self.app.config()["interval"] * 4)
                     and service.get("state") == "running" and isinstance(service.get("worker_token"), str)
                     and bool(service["worker_token"]) and scan.get("worker_token") == service["worker_token"]
                     and type(service.get("worker_pid")) is int and service["worker_pid"] > 0
                     and scan.get("worker_pid") == service["worker_pid"]
                     and service.get("at", float("inf")) <= scan["at"]
                     and self.worker_leases_held())
        except (OSError, ValueError, TypeError, KeyError):
            ready = False
        require(ready, "Idle-aware worker is not ready; coordinate the existing Hailing Frequencies worker upgrade")

    def worker_leases_held(self):
        """Kernel-owned lifetime locks disappear on exit even while a heartbeat stays fresh."""
        for name in ("supervisor.lock", "serve.lock"):
            path = self.directory / name
            require(path.is_file() and not path.is_symlink(), "Worker lifetime lock is unavailable")
            with path.open("r") as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)
                    return False
        return True

    def set(self, after=None, at=None, every=None, until=None, continuation="", request_id=None, replacement_key=None,
            after_idle=None):
        import hailing_frequencies as hf
        selected = [after is not None, at is not None, every is not None, after_idle is not None]
        require(sum(selected) == 1, "Choose exactly one of --after, --at, --every, or --after-idle")
        require(isinstance(continuation, str) and continuation.strip(), "Continuation message cannot be empty")
        now = self.clock()
        if after_idle is not None:
            # Older workers would otherwise treat an unknown schedule as an ordinary due timer.
            self.require_idle_worker(now)
            schedule_value = duration(after_idle)
            schedule, interval, due = "idle", None, now + schedule_value
        elif after is not None:
            after_seconds = duration(after)
            schedule, interval, due, schedule_value = "after", None, now + after_seconds, after_seconds
        elif at is not None:
            due, parsed = offset_datetime(at)
            schedule, interval, schedule_value = "at", None, due
        else:
            interval = duration(every)
            due, schedule, schedule_value = now + interval, "every", interval
        deadline = offset_datetime(until)[0] if until is not None else None
        require(schedule == "every" or deadline is None, "--until is only valid with --every")
        resolved_due = timestamp(due)
        target, owner_key, worktree = self._creation_owner()
        if schedule == "idle":
            due = max(due, self.idle_due(target, schedule_value, now))
            resolved_due = timestamp(due)
        timer_id = str(uuid.uuid4())
        request_id = request_id.strip() if request_id is not None else None
        replacement_key = replacement_key.strip() if replacement_key is not None else None
        require(request_id is None or bool(request_id), "Request ID cannot be empty")
        require(replacement_key is None or bool(replacement_key), "Replacement key cannot be empty")
        checkpoint_git_identity = (self._git_identity(worktree)
                                   if replacement_key == CHECKPOINT_IDLE_KEY else None)
        schedule_spec = json.dumps({"schedule": schedule, "value": schedule_value,
                                    "until_at": deadline, "continuation": continuation.strip()},
                                   sort_keys=True, separators=(",", ":"))
        spec = (owner_key, target["harness"], target["session"], target["surface"], target["workspace"],
                target["started_at"], worktree, continuation.strip(), schedule, interval, due, deadline,
                request_id, replacement_key, schedule_spec)
        with self.locked(), contextlib.closing(self.database()) as db:
            db.execute("BEGIN IMMEDIATE")
            if checkpoint_git_identity is not None:
                worktree_root, _git_dir, _git_dir_device, _git_dir_inode = checkpoint_git_identity
                retired = db.execute(
                    "SELECT 1 FROM retired_checkpoint_timer_keys WHERE harness=? AND session=? AND surface=? "
                    "AND workspace=? AND started_at=? AND worktree_root=? AND replacement_key=?",
                    (target["harness"], target["session"], target["surface"], target["workspace"],
                     target["started_at"], worktree_root, replacement_key)).fetchone()
                require(retired is None,
                        "This exact checkpoint owner and Git worktree retired the idle timer key")
            if request_id:
                existing = db.execute("SELECT * FROM timers WHERE request_id=?", (request_id,)).fetchone()
                if existing:
                    timer = self._timer_dict(existing)
                    prior = tuple(timer[key] for key in ("owner_key", "harness", "session", "surface", "workspace",
                                  "started_at", "canonical_worktree", "continuation", "request_id", "replacement_key"))
                    expected = (owner_key, target["harness"], target["session"], target["surface"], target["workspace"],
                                target["started_at"], worktree, continuation.strip(), request_id, replacement_key)
                    require(prior == expected and timer["schedule_spec"] == schedule_spec,
                            "Request ID already exists with different timer parameters")
                    db.commit()
                    return {"timer": timer, "idempotent": True}
            require(schedule == "idle" or due > now, "Timer due time must be in the future")
            require(deadline is None or deadline > now, "--until must be in the future")
            if deadline is not None and due >= deadline:
                raise ValueError("First recurrence is at or after --until, so no occurrence can run")
            if replacement_key:
                db.execute("UPDATE timers SET status='canceled',updated_at=? WHERE owner_key=? AND replacement_key=? AND status='active'",
                           (now, owner_key, replacement_key))
            try:
                db.execute("INSERT INTO timers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (timer_id, *spec[:12], "active", request_id, replacement_key, now, now, schedule_spec))
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise ValueError("Timer request conflicts with existing live state") from exc
            db.commit()
            return {"timer": self._timer(db, timer_id), "idempotent": False,
                    "resolved_due": resolved_due, "timezone": datetime.now().astimezone().tzname(),
                    "delivery_capability": self.capability(target)}

    def list(self, owner_key):
        with self.locked(), contextlib.closing(self.database()) as db:
            rows = db.execute("SELECT * FROM timers WHERE owner_key=? ORDER BY created_at DESC", (owner_key,)).fetchall()
            return {"timers": [self._timer_dict(row) for row in rows]}

    def inspect(self, owner_key, timer_id):
        with self.locked(), contextlib.closing(self.database()) as db:
            timer = self._timer(db, timer_id)
            require(timer["owner_key"] == owner_key, "Timer belongs to another exact owner")
            rows = db.execute("SELECT id,timer_id,sequence,due_at,status,continuation,attempted_at,received_at,detail FROM timer_occurrences WHERE timer_id=? ORDER BY sequence", (timer_id,)).fetchall()
            return {"timer": timer, "occurrences": [self._occurrence_dict(row) for row in rows]}

    def cancel(self, owner_key, timer_id):
        now = self.clock()
        with self.locked(), contextlib.closing(self.database()) as db:
            db.execute("BEGIN IMMEDIATE")
            timer = self._timer(db, timer_id)
            require(timer["owner_key"] == owner_key, "Timer belongs to another exact owner")
            if timer["status"] == "active":
                db.execute("UPDATE timers SET status='canceled',updated_at=? WHERE id=? AND status='active'", (now, timer_id))
                db.execute("UPDATE timer_occurrences SET status='canceled' WHERE timer_id=? AND status IN ('pending','held')", (timer_id,))
            db.commit()
            return {"timer_id": timer_id, "status": "canceled" if timer["status"] == "active" else timer["status"],
                    "submitted_input_recallable": False}

    def receive(self, owner_key, output, timer_id=None):
        target, _, _ = self.owner()
        with self.locked(), contextlib.closing(self.database()) as db:
            db.execute("BEGIN IMMEDIATE")
            query = "SELECT o.id,o.timer_id,o.sequence,o.due_at,o.status,o.continuation,o.attempted_at,o.received_at,o.detail,t.status,t.owner_key,t.until_at,t.interval,t.due_at,t.canonical_worktree FROM timer_occurrences o JOIN timers t ON t.id=o.timer_id WHERE t.owner_key=? AND t.session=? AND t.surface=? AND t.workspace=? AND t.started_at=? AND o.status IN ('submitted','uncertain')"
            params = [owner_key, target["session"], target["surface"], target["workspace"], target["started_at"]]
            if timer_id:
                query += " AND t.id=?"
                params.append(timer_id)
            query += " ORDER BY o.sequence DESC LIMIT 1"
            row = db.execute(query, params).fetchone()
            require(row is not None, "No submitted continuation is waiting for receipt")
            occurrence = self._occurrence_dict(row[:9])
            timer_status, stored_owner, until_at, interval, due_at, worktree = row[9:]
            require(stored_owner == owner_key, "Timer owner changed")
            require(timer_status == "active", "Timer is canceled, expired, or no longer current; its continuation cannot be retrieved")
            self.binding(target, worktree)
            payload = {"timer_id": occurrence["timer_id"], "occurrence_id": occurrence["id"],
                       "sequence": occurrence["sequence"], "due_at": occurrence["due_at_local"],
                       "continuation": occurrence["continuation"], "receipt_means_task_completed": False}
            rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
            written = output.write(rendered)
            require(written == len(rendered), "Output was incomplete; receipt not recorded")
            output.flush()
            self.binding(target, worktree)
            now = self.clock()
            db.execute("UPDATE timer_occurrences SET status='received',received_at=? WHERE id=? AND status IN ('submitted','uncertain')",
                       (now, occurrence["id"]))
            if timer_status == "active" and interval is not None:
                next_due = due_at + (max(0, math.floor((now - due_at) / interval)) + 1) * interval
                if until_at is not None and next_due >= until_at:
                    db.execute("UPDATE timers SET status='expired',updated_at=? WHERE id=?", (now, occurrence["timer_id"]))
                else:
                    db.execute("UPDATE timers SET due_at=?,updated_at=? WHERE id=? AND status='active'",
                               (next_due, now, occurrence["timer_id"]))
            else:
                db.execute("UPDATE timers SET status='completed',updated_at=? WHERE id=? AND status='active'",
                           (now, occurrence["timer_id"]))
            db.commit()
            return {"received": occurrence["id"], "receipt_means_task_completed": False}

    def receive_command(self, timer_id):
        launcher = self.app.installed_launcher()
        if launcher is not None:
            return shlex.join([str(launcher), "--state-dir", str(self.directory), "temporal", "receive", "--timer-id", timer_id])
        return shlex.join(["python3", str(Path(__file__).with_name("hailing_frequencies.py").resolve()),
                           "--state-dir", str(self.directory), "--cmux", self.app.cmux.executable,
                           "temporal", "receive", "--timer-id", timer_id])

    def tick(self, send=True, only_sessions=None):
        now = self.clock()
        allowed = None
        if only_sessions is not None:
            import hailing_frequencies as hf
            allowed = {hf.session_identity(session) for session in only_sessions}
        with self.locked(), contextlib.closing(self.database()) as db:
            timers = db.execute("SELECT * FROM timers WHERE status='active' AND due_at<=? ORDER BY due_at", (now,)).fetchall()
            results = []
            for raw in timers:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT * FROM timers WHERE id=? AND status='active'", (raw[0],)).fetchone()
                if current is None:
                    db.commit()
                    continue
                timer = self._timer_dict(current)
                prior = db.execute("SELECT id,status FROM timer_occurrences WHERE timer_id=? ORDER BY sequence DESC LIMIT 1", (timer["id"],)).fetchone()
                if prior and prior[1] in ("submitted", "uncertain"):
                    db.commit()
                    continue
                try:
                    matches = [item for item in self.app.config()["targets"]
                               if all(item.get(key) == timer[key] for key in
                                      ("surface", "workspace", "session", "harness", "started_at"))]
                    matches = [item for item in matches
                               if self.owner_key(item, timer["canonical_worktree"]) == timer["owner_key"]]
                    require(len(matches) == 1, "Timer's exact session is no longer enrolled")
                    target = matches[0]
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    detail = str(exc)[:500]
                    results.append({"timer_id": timer["id"], "status": "held", "detail": detail})
                    db.commit()
                    continue
                idle_seconds = None
                if timer["schedule"] == "idle":
                    idle_seconds = json.loads(timer["schedule_spec"])["value"]
                    try:
                        due = self.idle_due(target, idle_seconds, now)
                    except (ValueError, OSError, subprocess.SubprocessError) as exc:
                        results.append({"timer_id": timer["id"], "status": "held", "detail": str(exc)[:500]})
                        db.commit()
                        continue
                    if due > now:
                        # An old held occurrence must not fire after real activity resumed.
                        db.execute("UPDATE timer_occurrences SET status='canceled',detail='activity reset idle deadline' WHERE timer_id=? AND status IN ('pending','held')", (timer["id"],))
                        db.execute("UPDATE timers SET due_at=?,updated_at=? WHERE id=?", (due, now, timer["id"]))
                        results.append({"timer_id": timer["id"], "status": "waiting_for_idle", "due_at": due})
                        db.commit()
                        continue
                if timer["until_at"] is not None and now >= timer["until_at"]:
                    if prior and prior[1] in ("pending", "held"):
                        db.execute("UPDATE timer_occurrences SET status='expired',detail='until deadline passed' WHERE id=?", (prior[0],))
                    db.execute("UPDATE timers SET status='expired',updated_at=? WHERE id=?", (now, timer["id"]))
                    results.append({"timer_id": timer["id"], "occurrence_id": prior[0] if prior else None, "status": "expired"})
                    db.commit()
                    continue
                if timer["until_at"] is not None and timer["due_at"] >= timer["until_at"]:
                    db.execute("UPDATE timers SET status='expired',updated_at=? WHERE id=?", (now, timer["id"]))
                    results.append({"timer_id": timer["id"], "status": "expired"})
                    db.commit()
                    continue
                if not send:
                    results.append({"timer_id": timer["id"], "status": "would_submit"})
                    db.commit()
                    continue
                if allowed is not None and timer["session"] not in allowed:
                    results.append({"timer_id": timer["id"], "status": "outside_send_scope"})
                    db.commit()
                    continue
                if prior and prior[1] in ("held", "pending"):
                    occurrence_id = prior[0]
                    db.execute("UPDATE timer_occurrences SET status='pending',detail=NULL WHERE id=?", (occurrence_id,))
                else:
                    sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM timer_occurrences WHERE timer_id=?", (timer["id"],)).fetchone()[0]
                    occurrence_id = str(uuid.uuid4())
                    db.execute("INSERT INTO timer_occurrences(id,timer_id,sequence,due_at,status,continuation) VALUES (?,?,?,?,?,?)",
                               (occurrence_id, timer["id"], sequence, timer["due_at"], "pending", timer["continuation"]))
                try:
                    # The same validator used by Hailing Frequencies rejects drafts, human input and stale generations.
                    self.binding(target, timer["canonical_worktree"])
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    detail = str(exc)[:500]
                    db.execute("UPDATE timer_occurrences SET status='held',detail=? WHERE id=?", (detail, occurrence_id))
                    results.append({"timer_id": timer["id"], "occurrence_id": occurrence_id, "status": "held", "detail": detail})
                    db.commit()
                    continue
                # Commit the no-replay intent before crossing the external CMUX boundary.
                db.execute("UPDATE timer_occurrences SET status='uncertain',attempted_at=? WHERE id=?", (now, occurrence_id))
                db.commit()
                import hailing_frequencies as hf
                try:
                    command = self.receive_command(timer["id"])
                    wake = f"Temporal Beacon timer {timer['id']} is due. Retrieve and receipt the continuation with:\n{command}\n\nThe continuation is stored locally and is not included in this wake prompt."
                    if idle_seconds is None:
                        self.app.cmux.submit(target, wake)
                    else:
                        self.app.cmux.submit(target, wake, on_transport_start=lambda: self.require_idle(target, idle_seconds))
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("UPDATE timer_occurrences SET status='submitted',detail='CMUX confirmed guarded submission' WHERE id=? AND status='uncertain'", (occurrence_id,))
                    db.commit()
                    results.append({"timer_id": timer["id"], "occurrence_id": occurrence_id, "status": "submitted"})
                except hf.DeliveryHoldError as exc:
                    detail = str(exc)[:500]
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("UPDATE timer_occurrences SET status='held',attempted_at=NULL,detail=? WHERE id=?", (detail, occurrence_id))
                    db.commit()
                    results.append({"timer_id": timer["id"], "occurrence_id": occurrence_id, "status": "held", "detail": detail})
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    detail = str(exc)[:500]
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("UPDATE timer_occurrences SET status='uncertain',detail=? WHERE id=?", (detail, occurrence_id))
                    db.commit()
                    results.append({"timer_id": timer["id"], "occurrence_id": occurrence_id, "status": "uncertain", "detail": detail})
            report = {"at": now, "timers": results, "supports_idle": True,
                      "worker_pid": os.getpid(), "worker_token": os.environ.get("STARFLEET_HF_WORKER_TOKEN")}
            import hailing_frequencies as hf
            hf.atomic_json(self.directory / "timer-last-scan.json", report)
            return report

    def doctor(self):
        import service
        health = service.health(self.directory)
        with self.locked(), contextlib.closing(self.database()) as db:
            counts = dict(db.execute("SELECT status,COUNT(*) FROM timers GROUP BY status").fetchall())
            occurrence_counts = dict(db.execute("SELECT status,COUNT(*) FROM timer_occurrences GROUP BY status").fetchall())
        issues = []
        if not health.get("healthy"):
            issues.append("Hailing Frequencies worker health is not current; start or inspect `hailing-frequencies supervise --send` in a CMUX terminal.")
        try:
            scan = json.loads((self.directory / "last-scan.json").read_text())
        except (OSError, ValueError, TypeError):
            scan = None
        service_path = self.directory / "service.json"
        try:
            service_state = json.loads(service_path.read_text())
        except (OSError, ValueError, TypeError):
            service_state = {}
        if service_state.get("state") != "running":
            issues.append("The supervised worker is not running. Start `hailing-frequencies --state-dir STATE supervise --send` inside the authorized CMUX terminal; terminal closure stops coverage.")
        try:
            timer_scan = json.loads((self.directory / "timer-last-scan.json").read_text())
            timer_age = self.clock() - timer_scan["at"]
            interval = self.app.config()["interval"]
            timer_fresh = (type(timer_scan["at"]) in (int, float) and math.isfinite(timer_scan["at"])
                           and 0 <= timer_age <= max(120, interval * 4))
        except (OSError, ValueError, KeyError, TypeError):
            timer_age, timer_fresh = None, False
        if not timer_fresh:
            issues.append("No fresh Temporal Beacon worker heartbeat is present. Restart the current installed Hailing Frequencies worker and check `doctor` again.")
        try:
            target, owner_key, worktree = self.owner(verify=False)
            if not scan or scan.get("mode") != "send":
                issues.append("The latest successful worker scan is observe-only. Restart the existing supervisor with --send to enable timer delivery.")
            scope = scan.get("sending_sessions") if scan else None
            if scope is not None and target["session"] not in scope:
                issues.append("The current exact owner is outside the worker's active send scope.")
            try:
                self.app.cmux.binding(target)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                issues.append("Current owner is held from delivery: " + str(exc))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            owner_key = None
            issues.append("Exact caller identity is unavailable: " + str(exc))
        if occurrence_counts.get("held"):
            issues.append("Held occurrences need an exact idle session with an empty composer; inspect timer IDs with `temporal-beacon list`.")
        if occurrence_counts.get("uncertain"):
            issues.append("Uncertain submissions are never replayed; inspect the exact session composer and timer with `temporal-beacon inspect ID`.")
        return {"healthy": not issues, "worker_health": health, "service": service_state,
                "exact_owner_verified": owner_key is not None, "timers": counts,
                "occurrences": occurrence_counts, "issues": issues,
                "coverage": "Only while the existing Hailing Frequencies supervisor remains in its CMUX terminal."}
