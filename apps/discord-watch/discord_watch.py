#!/usr/bin/env python3
"""Queue replies from a configured Discord user and wake the exact enrolled CMUX owner."""

import argparse
import contextlib
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import shlex
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True

RUNTIME_VENDOR = Path(__file__).resolve().parent / "vendor"
if RUNTIME_VENDOR.is_dir() and str(RUNTIME_VENDOR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_VENDOR))

try:
    import hailing_frequencies as hf
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hailing-frequencies"))
    import hailing_frequencies as hf


DEFAULT_STATE = Path.home() / ".local/state/starfleet-ai/discord-watch"
DEFAULT_NOTIFY_HOME = Path.home() / ".config/agent-notify"
DEFAULT_HAILING_STATE = Path.home() / ".local/state/starfleet-ai/hailing-frequencies"
API = "https://discord.com/api/v10"
HTTP_TIMEOUT = 10
CMUX_TIMEOUT = 15
LOCK_TIMEOUT = 5
POLL_INTERVAL = 12
MESSAGE_LIMIT = 100
MAX_CATCHUP_PAGES = 50
MAX_MESSAGE_BYTES = 64 * 1024
MAX_API_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_RATE_LIMIT_WAIT = 24 * 60 * 60
MAX_RECEIVE_MESSAGES = 20
MAX_RECEIVE_BYTES = 256 * 1024
GROUP_QUIET_SECONDS = 3
GROUP_MAX_DELAY = 10
ROUTINE_REST_INTERVAL = 30
REST_TURN_INTERVAL = 1
WORKER_REST_THREADS_PER_TURN = 8
WORKER_REST_PAGES_PER_THREAD = 2
SENDER_INTERVAL = 0.25
LANE_JOIN_TIMEOUT = 20
MAX_WAKE_ATTEMPTS = 3
REMIND_AFTER = 120
DORMANT_AFTER = 7 * 24 * 60 * 60
ALLOWED_LAUNCH_KINDS = {"claude", "codex", "opencode"}
ACTIVE_MESSAGE_STATES = ("pending", "awaiting_receipt", "uncertain", "stalled")
STARFLEET_DIAGNOSTIC_LIMIT = 256 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _sha256_owner(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snowflake(value, label="Discord snowflake"):
    require(isinstance(value, (str, int)) and not isinstance(value, bool), f"Invalid {label}")
    value = str(value)
    require(re.fullmatch(r"[1-9][0-9]{0,24}", value) is not None, f"Invalid {label}")
    return value


def id_order(value):
    return int(snowflake(value))


def timestamp_epoch(value):
    require(isinstance(value, str) and 1 <= len(value) <= 64,
            "Discord message timestamp is invalid")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError("Discord message timestamp is invalid") from None
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
            "Discord message timestamp must include a timezone")
    try:
        epoch = parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        raise ValueError("Discord message timestamp is invalid") from None
    require(math.isfinite(epoch), "Discord message timestamp is invalid")
    return epoch


def ensure_private_file(path, label):
    path = Path(path)
    require(not path.is_symlink(), f"Refusing symlink {label}")
    info = path.stat()
    require(path.is_file(), f"{label} is not a regular file")
    require(info.st_uid == os.getuid(), f"{label} is not owned by this user")
    require(info.st_mode & 0o077 == 0, f"{label} must be private to this user")
    return path


def require_private_directory(path):
    path = Path(path).expanduser().absolute()
    require(not path.is_symlink(), f"Refusing symlink state directory: {path}")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
    require(path.is_dir(), f"State path is not a directory: {path}")
    if path.stat().st_uid != os.getuid():
        raise ValueError("State directory is not owned by this user")
    path.chmod(0o700)
    return path


class DiscordAPIError(ValueError):
    def __init__(self, status, message, code=None, retry_after=None, global_rate_limit=False):
        self.status = status
        self.discord_code = code
        self.retry_after = retry_after
        self.global_rate_limit = bool(global_rate_limit)
        self.message = str(message)[:500]
        super().__init__(self.message)

    def record(self):
        return {"http_status": self.status, "discord_code": self.discord_code,
                "message": self.message, "retry_after": self.retry_after,
                "global_rate_limit": self.global_rate_limit}


class Discord:
    def __init__(self, token, timeout=HTTP_TIMEOUT, opener=None, clock=time.time):
        require(isinstance(token, str) and token.strip(), "DISCORD_BOT_TOKEN is required")
        self.token = token.strip()
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen
        self.clock = clock
        self.request_count = 0
        self.route_limits = {}
        self.rate_updates = []
        self.global_not_before = 0.0

    @staticmethod
    def route_key(path):
        return "GET " + path.split("?", 1)[0]

    def load_route_limits(self, values, global_not_before=0.0):
        self.route_limits = {row[0]: {"bucket_key": row[1], "remaining": row[2],
                                      "reset_at": row[3]} for row in values}
        self.global_not_before = float(global_not_before or 0.0)

    def deferred_until(self, path, now):
        if now < self.global_not_before:
            return self.global_not_before
        route = self.route_key(path)
        limit = self.route_limits.get(route)
        candidates = [limit] if limit else []
        if limit and limit.get("bucket_key"):
            candidates.extend(item for item in self.route_limits.values()
                              if item.get("bucket_key") == limit["bucket_key"])
        waits = [item["reset_at"] for item in candidates
                 if item and item.get("remaining") == 0 and item.get("reset_at", 0) > now]
        return max(waits, default=0.0)

    def _remember_headers(self, path, headers, now):
        if headers is None:
            return
        route = self.route_key(path)
        bucket = headers.get("X-RateLimit-Bucket")
        major = path.split("/", 3)[2] if path.startswith("/channels/") else ""
        bucket_key = f"{bucket}:{major}" if bucket else route
        def number(name):
            raw = headers.get(name)
            try:
                value = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                return None
            return value if value is not None and math.isfinite(value) and value >= 0 else None
        remaining_value = number("X-RateLimit-Remaining")
        limit_value = number("X-RateLimit-Limit")
        reset_after = number("X-RateLimit-Reset-After")
        reset_epoch = number("X-RateLimit-Reset")
        if reset_after is not None:
            reset_at = now + min(MAX_RATE_LIMIT_WAIT, reset_after)
        elif reset_epoch is not None:
            reset_at = min(now + MAX_RATE_LIMIT_WAIT, reset_epoch)
        else:
            reset_at = None
        if remaining_value is not None or reset_at is not None:
            value = {"route": route, "bucket_key": bucket_key,
                     "remaining": int(remaining_value) if remaining_value is not None else None,
                     "limit_total": int(limit_value) if limit_value is not None else None,
                     "reset_at": reset_at or 0.0, "updated_at": now}
            self.route_limits[route] = value
            self.rate_updates.append(value)
        if str(headers.get("X-RateLimit-Global", "")).lower() == "true" or \
                headers.get("X-RateLimit-Scope") == "global":
            wait = min(MAX_RATE_LIMIT_WAIT, reset_after or number("Retry-After") or 0)
            self.global_not_before = max(self.global_not_before, now + wait)

    def get(self, path, query=None):
        url = API + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={
            "Authorization": "Bot " + self.token,
            "User-Agent": "DiscordBot (https://github.com/morecoffeyplease/starfleet-ai, discord-watch)",
            "Accept": "application/json",
        })
        try:
            self.request_count += 1
            with self.opener(request, timeout=self.timeout) as response:
                self._remember_headers(path, getattr(response, "headers", None), self.clock())
                body = response.read(MAX_API_RESPONSE_BYTES + 1)
                require(len(body) <= MAX_API_RESPONSE_BYTES,
                        "Discord response exceeded the bounded response size")
        except urllib.error.HTTPError as error:
            body = error.read(16 * 1024)
            headers = error.headers or {}
            self._remember_headers(path, headers, self.clock())
            parsed = {}
            try:
                parsed = json.loads(body.decode("utf-8", "replace"))
            except (ValueError, UnicodeError):
                pass
            retry_after = parsed.get("retry_after")
            if retry_after is None:
                retry_after = headers.get("Retry-After")
            try:
                retry_after = float(retry_after) if retry_after is not None else None
                if retry_after is not None and (not math.isfinite(retry_after) or retry_after < 0):
                    retry_after = None
            except (ValueError, TypeError):
                retry_after = None
            raise DiscordAPIError(error.code, parsed.get("message", "Discord API request failed"),
                                  parsed.get("code"), retry_after,
                                  parsed.get("global") is True or
                                  str(headers.get("X-RateLimit-Global", "")).lower() == "true" or
                                  headers.get("X-RateLimit-Scope") == "global") from None
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            reason = getattr(error, "reason", None)
            category = type(reason or error).__name__
            raise ValueError(f"Discord network request failed ({category})") from None
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ValueError("Discord returned invalid JSON") from None


def read_notify_credentials(notify_home):
    """Source the existing agent-notify shell config without forwarding its output."""
    home = Path(notify_home).expanduser().absolute()
    config = ensure_private_file(home / "env", "agent-notify env file")
    script = (
        "set -a; set +x; source \"$1\" >/dev/null 2>&1; "
        "printf '%s\\0%s\\0' \"${DISCORD_BOT_TOKEN-}\" \"${DISCORD_USER_ID-}\""
    )
    try:
        result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", script,
                                 "discord-watch-config", str(config)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Could not read agent-notify credentials") from None
    require(result.returncode == 0, "Could not read agent-notify credentials")
    require(len(result.stdout) <= 16 * 1024, "agent-notify credential output is invalid")
    fields = result.stdout.split(b"\0")
    require(len(fields) == 3 and fields[-1] == b"", "agent-notify credential output is invalid")
    try:
        token, configured_user = (item.decode("utf-8") for item in fields[:2])
    except UnicodeDecodeError:
        raise ValueError("agent-notify credentials are not valid UTF-8") from None
    require(token.strip(), "DISCORD_BOT_TOKEN is missing from agent-notify env")
    user_id = configured_user.strip()
    if not user_id:
        cache_path = home / "cache.json"
        require(cache_path.exists(),
                "The configured Discord user is missing; run agent-notify doctor first")
        cache_path = ensure_private_file(cache_path, "agent-notify cache")
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            user_id = str(cache.get("user", ""))
        except (OSError, ValueError, TypeError):
            raise ValueError("agent-notify cached Discord user is invalid") from None
    user_id = snowflake(user_id, "cached Discord user ID")
    tags = {}
    cache_path = home / "cache.json"
    if cache_path.exists():
        cache_path = ensure_private_file(cache_path, "agent-notify cache")
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            tags = cache.get("tags") if isinstance(cache.get("tags"), dict) else {}
        except (OSError, ValueError, TypeError):
            tags = {}
    closed_tag = tags.get("closed")
    if closed_tag is not None:
        closed_tag = snowflake(closed_tag, "cached Closed tag ID")
    return {"token": token, "user_id": user_id, "closed_tag": closed_tag}


class Watch:
    def __init__(self, state_dir=DEFAULT_STATE, hailing_state=DEFAULT_HAILING_STATE,
                 notify_home=DEFAULT_NOTIFY_HOME, cmux="cmux", clock=time.time,
                 discord_factory=Discord, cmux_factory=None, gateway_factory=None):
        self.directory = Path(state_dir).expanduser().absolute()
        self.hailing_state = Path(hailing_state).expanduser().absolute()
        self.notify_home = Path(notify_home).expanduser().absolute()
        self.cmux_executable = cmux
        self.clock = clock
        self.discord_factory = discord_factory
        self.cmux_factory = cmux_factory or (lambda: hf.Cmux(cmux))
        self.gateway_factory = gateway_factory
        self._configured_user_id = None
        self.global_not_before = 0.0
        self.worker_generation = None
        os.umask(0o077)

    @contextlib.contextmanager
    def locked(self, timeout=LOCK_TIMEOUT, name="state.lock"):
        require_private_directory(self.directory)
        path = self.directory / name
        require(not path.is_symlink(), "Refusing symlink state lock")
        with path.open("a") as stream:
            os.chmod(path, 0o600)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError("Another Discord Watch operation owns this state") from None
                    time.sleep(min(0.1, remaining))
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def database(self, create=True):
        if create:
            require_private_directory(self.directory)
        else:
            require(not self.directory.is_symlink() and self.directory.is_dir(),
                    "Discord Watch state is not initialized")
            require(self.directory.stat().st_uid == os.getuid(),
                    "State directory is not owned by this user")
            self.directory.chmod(0o700)
        path = self.directory / "discord-watch.sqlite3"
        require(not path.is_symlink(), "Refusing symlink Discord Watch database")
        if not create and not path.exists():
            raise ValueError("Discord Watch state is not initialized")
        db = sqlite3.connect(path, timeout=10)
        os.chmod(path, 0o600)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS threads (
          thread_id TEXT PRIMARY KEY,
          owner_json TEXT NOT NULL,
          cursor TEXT NOT NULL,
          initial_cutoff TEXT,
          is_open INTEGER NOT NULL DEFAULT 1,
          registered_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          last_poll_at REAL,
          next_poll_at REAL NOT NULL DEFAULT 0,
          catchup_before TEXT,
          catchup_max TEXT,
          last_error_json TEXT,
          remote_json TEXT NOT NULL DEFAULT '{}',
          owner_generation INTEGER NOT NULL DEFAULT 1,
          subscription_state TEXT NOT NULL DEFAULT 'active',
          last_activity_at REAL,
          remote_closed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
          thread_id TEXT NOT NULL REFERENCES threads(thread_id),
          message_id TEXT NOT NULL,
          author_id TEXT NOT NULL,
          author_name TEXT NOT NULL,
          body TEXT NOT NULL,
          attachments_json TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending',
          wake_attempts INTEGER NOT NULL DEFAULT 0,
          last_wake_at REAL,
          submitted_at REAL,
          received_at REAL,
          last_error_json TEXT,
          groupable INTEGER NOT NULL DEFAULT 1,
          ingested_at REAL,
          owner_generation INTEGER,
          group_id INTEGER,
          PRIMARY KEY(thread_id, message_id)
        );
        CREATE INDEX IF NOT EXISTS messages_queue ON messages(thread_id, state, message_id);
        CREATE TABLE IF NOT EXISTS ownership_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          thread_id TEXT NOT NULL,
          from_session TEXT NOT NULL,
          to_session TEXT NOT NULL,
          at REAL NOT NULL,
          owner_generation INTEGER NOT NULL,
          event_type TEXT NOT NULL DEFAULT 'transfer',
          operation_id TEXT,
          plan_digest TEXT,
          from_owner_json TEXT,
          to_owner_json TEXT,
          old_owner_digest TEXT,
          new_owner_digest TEXT,
          before_digest TEXT,
          after_preserved_digest TEXT,
          from_worktree TEXT,
          to_worktree TEXT,
          continuity TEXT,
          UNIQUE(operation_id,thread_id)
        );
        CREATE TABLE IF NOT EXISTS custody_adoption_holds (
          operation_id TEXT NOT NULL, thread_id TEXT NOT NULL, plan_digest TEXT NOT NULL,
          PRIMARY KEY(operation_id,thread_id)
        );
        CREATE TABLE IF NOT EXISTS delivery_groups (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          thread_id TEXT NOT NULL,
          owner_generation INTEGER NOT NULL,
          owner_json TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at REAL NOT NULL,
          first_message_id TEXT NOT NULL,
          last_message_id TEXT NOT NULL,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          last_wake_at REAL,
          submitted_at REAL,
          last_error_json TEXT,
          legacy INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS delivery_groups_queue ON delivery_groups(thread_id,status,id);
        CREATE TABLE IF NOT EXISTS delivery_group_members (
          group_id INTEGER NOT NULL REFERENCES delivery_groups(id),
          thread_id TEXT NOT NULL,
          message_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL,
          PRIMARY KEY(group_id,message_id),
          UNIQUE(thread_id,message_id)
        );
        CREATE TABLE IF NOT EXISTS delivery_group_attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          group_id INTEGER NOT NULL REFERENCES delivery_groups(id),
          state TEXT NOT NULL,
          created_at REAL NOT NULL,
          transport_started_at REAL,
          submitted_at REAL,
          last_error_json TEXT,
          target_owner_json TEXT,
          target_owner_generation INTEGER
        );
        CREATE TRIGGER IF NOT EXISTS delivery_group_members_no_update
        BEFORE UPDATE ON delivery_group_members BEGIN
          SELECT RAISE(ABORT, 'delivery group membership is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS delivery_group_members_no_delete
        BEFORE DELETE ON delivery_group_members BEGIN
          SELECT RAISE(ABORT, 'delivery group membership is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS delivery_groups_identity_no_update
        BEFORE UPDATE OF thread_id,owner_generation,owner_json,created_at,first_message_id,last_message_id,legacy
        ON delivery_groups BEGIN
          SELECT RAISE(ABORT, 'delivery group identity is immutable');
        END;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS gateway_state (
          id INTEGER PRIMARY KEY CHECK(id=1),
          condition TEXT NOT NULL DEFAULT 'degraded',
          session_id TEXT,
          resume_url TEXT,
          sequence TEXT,
          session_start_total INTEGER,
          session_start_remaining INTEGER,
          session_start_reset_at REAL,
          last_identify_at REAL,
          last_event_at REAL,
          last_error_json TEXT,
          recovery_epoch INTEGER NOT NULL DEFAULT 0,
          recovery_state TEXT NOT NULL DEFAULT 'pending',
          recovery_reason TEXT,
          recovery_started_at REAL,
          recovery_completed_at REAL,
          updated_at REAL NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO gateway_state(id,condition,updated_at) VALUES(1,'degraded',0);
        CREATE TABLE IF NOT EXISTS gateway_recovery_work (
          epoch INTEGER NOT NULL,
          thread_id TEXT NOT NULL,
          owner_generation INTEGER NOT NULL,
          state TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          updated_at REAL NOT NULL,
          completed_at REAL,
          last_error_json TEXT,
          PRIMARY KEY(epoch,thread_id,owner_generation)
        );
        CREATE INDEX IF NOT EXISTS gateway_recovery_queue
        ON gateway_recovery_work(epoch,state,thread_id);
        CREATE TABLE IF NOT EXISTS api_rate_limits (
          route_key TEXT PRIMARY KEY,
          bucket_key TEXT NOT NULL,
          remaining INTEGER,
          limit_total INTEGER,
          reset_at REAL NOT NULL DEFAULT 0,
          updated_at REAL NOT NULL
        );
        """)
        db.execute("BEGIN IMMEDIATE")
        attempt_columns = {row[1] for row in db.execute("PRAGMA table_info(delivery_group_attempts)")}
        if "target_owner_json" not in attempt_columns:
            db.execute("ALTER TABLE delivery_group_attempts ADD COLUMN target_owner_json TEXT")
        if "target_owner_generation" not in attempt_columns:
            db.execute("ALTER TABLE delivery_group_attempts ADD COLUMN target_owner_generation INTEGER")
        ownership_columns = {row[1] for row in db.execute("PRAGMA table_info(ownership_events)")}
        for name, declaration in (
                ("event_type", "TEXT NOT NULL DEFAULT 'transfer'"),
                ("operation_id", "TEXT"), ("plan_digest", "TEXT"),
                ("from_owner_json", "TEXT"), ("to_owner_json", "TEXT"),
                ("old_owner_digest", "TEXT"), ("new_owner_digest", "TEXT"),
                ("before_digest", "TEXT"), ("after_preserved_digest", "TEXT"),
                ("from_worktree", "TEXT"), ("to_worktree", "TEXT"),
                ("continuity", "TEXT")):
            if name not in ownership_columns:
                db.execute(f"ALTER TABLE ownership_events ADD COLUMN {name} {declaration}")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ownership_adoption_operation "
                   "ON ownership_events(operation_id,thread_id) WHERE operation_id IS NOT NULL")
        thread_columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
        if "catchup_before" not in thread_columns:
            db.execute("ALTER TABLE threads ADD COLUMN catchup_before TEXT")
        if "catchup_max" not in thread_columns:
            db.execute("ALTER TABLE threads ADD COLUMN catchup_max TEXT")
        if "subscription_state" not in thread_columns:
            db.execute("ALTER TABLE threads ADD COLUMN subscription_state TEXT NOT NULL DEFAULT 'active'")
        if "last_activity_at" not in thread_columns:
            db.execute("ALTER TABLE threads ADD COLUMN last_activity_at REAL")
        if "remote_closed" not in thread_columns:
            db.execute("ALTER TABLE threads ADD COLUMN remote_closed INTEGER NOT NULL DEFAULT 0")
        if "initial_cutoff" not in thread_columns:
            db.execute("ALTER TABLE threads ADD COLUMN initial_cutoff TEXT")
        message_columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        if "attachments_json" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'")
        if "groupable" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN groupable INTEGER NOT NULL DEFAULT 1")
            db.execute("UPDATE messages SET groupable=0")
        if "ingested_at" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN ingested_at REAL")
        if "owner_generation" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN owner_generation INTEGER")
        if "group_id" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN group_id INTEGER")
        db.execute("CREATE INDEX IF NOT EXISTS messages_group_queue "
                   "ON messages(thread_id,groupable,owner_generation,group_id,state,message_id)")
        activity_migrated = db.execute(
            "SELECT 1 FROM meta WHERE key='meaningful_activity_migration_v1'").fetchone()
        if activity_migrated is None:
            for thread_id, registered_at, remote_json in db.execute(
                    "SELECT thread_id,registered_at,remote_json FROM threads").fetchall():
                activity_at = None
                for (created_at,) in db.execute(
                        "SELECT created_at FROM messages WHERE thread_id=? "
                        "ORDER BY length(message_id) DESC,message_id DESC", (thread_id,)):
                    try:
                        activity_at = timestamp_epoch(created_at)
                        break
                    except ValueError:
                        continue
                try:
                    remote = json.loads(remote_json)
                except (TypeError, ValueError):
                    remote = {}
                metadata = remote.get("thread_metadata") if isinstance(remote, dict) else None
                remote_closed = isinstance(metadata, dict) and metadata.get("archived") is True
                db.execute("UPDATE threads SET last_activity_at=?,subscription_state='active',remote_closed=? "
                           "WHERE thread_id=?", (activity_at, int(remote_closed), thread_id))
            db.execute("INSERT INTO meta(key,value) VALUES('meaningful_activity_migration_v1','complete')")
        cutoff_migrated = db.execute(
            "SELECT 1 FROM meta WHERE key='initial_intake_cutoff_migration_v1'").fetchone()
        if cutoff_migrated is None:
            db.execute("UPDATE threads SET initial_cutoff=cursor WHERE initial_cutoff IS NULL")
            db.execute("INSERT INTO meta(key,value) VALUES('initial_intake_cutoff_migration_v1','complete')")
        # Older supported CLI versions can still insert registrations without this column.
        db.execute("UPDATE threads SET initial_cutoff=cursor WHERE initial_cutoff IS NULL")
        # Older supported CLI versions can write rows without the new intake metadata.
        db.execute("UPDATE messages SET owner_generation=(SELECT owner_generation FROM threads "
                   "WHERE threads.thread_id=messages.thread_id) WHERE owner_generation IS NULL")
        for thread_id, message_id, created_at in db.execute(
                "SELECT thread_id,message_id,created_at FROM messages WHERE ingested_at IS NULL").fetchall():
            try:
                ingested_at = timestamp_epoch(created_at)
            except ValueError:
                ingested_at = db.execute("SELECT registered_at FROM threads WHERE thread_id=?",
                                         (thread_id,)).fetchone()[0]
            db.execute("UPDATE messages SET ingested_at=? WHERE thread_id=? AND message_id=? "
                       "AND ingested_at IS NULL", (ingested_at, thread_id, message_id))
        # Existing per-message delivery evidence becomes immutable singleton groups.
        for row in db.execute(
                "SELECT m.thread_id,m.message_id,m.state,m.wake_attempts,m.last_wake_at,m.submitted_at,"
                "m.ingested_at,t.owner_json,t.owner_generation FROM messages m JOIN threads t "
                "ON t.thread_id=m.thread_id WHERE (m.groupable=0 OR m.state IN "
                "('awaiting_receipt','uncertain','stalled') OR m.wake_attempts>0) AND m.group_id IS NULL "
                "AND m.state!='received' ORDER BY m.thread_id,length(m.message_id),m.message_id").fetchall():
            thread_id, message_id, state, attempts, last_wake_at, submitted_at, ingested_at, owner_json, generation = row
            group_state = state if state in ("awaiting_receipt", "uncertain", "stalled") else "eligible"
            if state == "pending" and attempts >= MAX_WAKE_ATTEMPTS:
                group_state = "stalled"
            created_at = last_wake_at if last_wake_at is not None else ingested_at
            cursor = db.execute(
                "INSERT INTO delivery_groups(thread_id,owner_generation,owner_json,status,created_at,"
                "first_message_id,last_message_id,attempt_count,last_wake_at,submitted_at,legacy) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                (thread_id, generation, owner_json, group_state, created_at, message_id, message_id,
                 attempts, last_wake_at, submitted_at))
            group_id = cursor.lastrowid
            db.execute("INSERT INTO delivery_group_members(group_id,thread_id,message_id,ordinal) VALUES(?,?,?,0)",
                       (group_id, thread_id, message_id))
            db.execute("UPDATE messages SET group_id=? WHERE thread_id=? AND message_id=?",
                       (group_id, thread_id, message_id))
        db.execute("UPDATE delivery_groups SET status='received' WHERE status!='received' AND NOT EXISTS ("
                   "SELECT 1 FROM delivery_group_members gm JOIN messages m "
                   "ON m.thread_id=gm.thread_id AND m.message_id=gm.message_id "
                   "WHERE gm.group_id=delivery_groups.id AND m.state!='received')")
        db.commit()
        db.close()
        path.chmod(0o600)
        db = sqlite3.connect(path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def gateway_session(self):
        with contextlib.closing(self.database(create=False)) as db:
            row = db.execute(
                "SELECT condition,session_id,resume_url,sequence,session_start_total,"
                "session_start_remaining,session_start_reset_at,last_identify_at,last_event_at,"
                "recovery_epoch,recovery_state,recovery_reason,last_error_json "
                "FROM gateway_state WHERE id=1").fetchone()
        return {"condition": row[0], "session_id": row[1], "resume_url": row[2],
                "sequence": row[3], "session_start_total": row[4],
                "session_start_remaining": row[5], "session_start_reset_at": row[6],
                "last_identify_at": row[7], "last_event_at": row[8],
                "recovery_epoch": row[9], "recovery_state": row[10],
                "recovery_reason": row[11],
                "last_error": json.loads(row[12]) if row[12] else None}

    def set_gateway_condition(self, condition, detail=None):
        allowed = {"connected", "connecting", "degraded", "reconnecting",
                   "fatal_configuration", "offline_rest"}
        require(condition in allowed, "Invalid Gateway condition")
        now = self.clock()
        error_json = json.dumps({"detail": str(detail)[:300]}, separators=(",", ":")) if detail else None
        with contextlib.closing(self.database(create=False)) as db:
            with db:
                db.execute("UPDATE gateway_state SET condition=?,last_error_json=?,"
                           "last_event_at=CASE WHEN ?='connected' THEN ? ELSE last_event_at END,"
                           "updated_at=? WHERE id=1",
                           (condition, error_json, condition, now, now))

    @staticmethod
    def _begin_gateway_recovery_tx(db, reason, now):
        row = db.execute("SELECT recovery_epoch FROM gateway_state WHERE id=1").fetchone()
        epoch = row[0] + 1
        threads = db.execute("SELECT thread_id,owner_generation FROM threads "
                             "ORDER BY registered_at,thread_id").fetchall()
        for thread_id, generation in threads:
            db.execute("INSERT OR IGNORE INTO gateway_recovery_work("
                       "epoch,thread_id,owner_generation,state,updated_at) VALUES(?,?,?,'queued',?)",
                       (epoch, thread_id, generation, now))
        state = "pending" if threads else "complete"
        db.execute("UPDATE gateway_state SET recovery_epoch=?,recovery_state=?,recovery_reason=?,"
                   "recovery_started_at=?,recovery_completed_at=?,updated_at=? WHERE id=1",
                   (epoch, state, str(reason)[:100], now, now if not threads else None, now))
        return epoch

    def begin_gateway_recovery(self, reason):
        now = self.clock()
        with contextlib.closing(self.database(create=False)) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                epoch = self._begin_gateway_recovery_tx(db, reason, now)
        return epoch

    def save_gateway_start_limit(self, limit, now=None):
        now = self.clock() if now is None else now
        reset_at = now + limit["reset_after"] / 1000
        with contextlib.closing(self.database(create=False)) as db:
            with db:
                db.execute("UPDATE gateway_state SET session_start_total=?,session_start_remaining=?,"
                           "session_start_reset_at=?,updated_at=? WHERE id=1",
                           (limit["total"], limit["remaining"], reset_at, now))

    def reserve_gateway_identify(self, now, limit):
        with contextlib.closing(self.database(create=False)) as db:
            with db:
                row = db.execute("SELECT session_start_remaining,session_start_reset_at "
                                 "FROM gateway_state WHERE id=1").fetchone()
                remaining = row[0] if row[0] is not None else limit["remaining"]
                require(remaining > 0, "Discord Gateway session-start limit is exhausted")
                db.execute("UPDATE gateway_state SET session_start_remaining=?,last_identify_at=?,"
                           "updated_at=? WHERE id=1", (remaining - 1, now, now))

    def clear_gateway_session(self, reset_sequence=True):
        now = self.clock()
        sequence_sql = ",sequence=NULL" if reset_sequence else ""
        with contextlib.closing(self.database(create=False)) as db:
            with db:
                db.execute("UPDATE gateway_state SET session_id=NULL,resume_url=NULL" + sequence_sql +
                           ",updated_at=? WHERE id=1", (now,))

    def commit_gateway_dispatch(self, sequence, event_name, data, configured_user_id,
                                validate_resume_url=None):
        require(isinstance(sequence, int) and not isinstance(sequence, bool) and
                0 <= sequence <= 10 ** 18, "Discord Gateway dispatch sequence is invalid")
        sequence = str(sequence)
        now = self.clock()
        inserted = False
        with contextlib.closing(self.database(create=False)) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                state = db.execute("SELECT sequence FROM gateway_state WHERE id=1").fetchone()
                current = int(state[0]) if state[0] is not None else -1
                if int(sequence) <= current:
                    return {"committed": False, "duplicate": True, "ingested": False}
                if event_name == "READY":
                    require(isinstance(data, dict), "Discord Gateway READY payload is invalid")
                    session_id = data.get("session_id")
                    resume_url = data.get("resume_gateway_url")
                    require(isinstance(session_id, str) and 1 <= len(session_id) <= 128 and
                            not any(ord(char) < 32 for char in session_id),
                            "Discord Gateway READY session ID is invalid")
                    require(callable(validate_resume_url), "Discord Gateway resume URL validator is unavailable")
                    resume_url = validate_resume_url(resume_url)
                    self._begin_gateway_recovery_tx(db, "fresh_identify_ready", now)
                    db.execute("UPDATE gateway_state SET session_id=?,resume_url=? WHERE id=1",
                               (session_id, resume_url))
                elif event_name == "RESUMED":
                    self._begin_gateway_recovery_tx(db, "resume_ready", now)
                elif event_name == "MESSAGE_CREATE":
                    require(isinstance(data, dict), "Discord Gateway MESSAGE_CREATE payload is invalid")
                    thread_id = snowflake(data.get("channel_id"), "Gateway message channel ID")
                    known = db.execute("SELECT 1 FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
                    if known and not self._custody_adoption_hold(db, thread_id):
                        inserted = self._ingest_message(db, thread_id, data, configured_user_id, now,
                                                        source="gateway")
                db.execute("UPDATE gateway_state SET sequence=?,last_event_at=?,updated_at=? WHERE id=1",
                           (sequence, now, now))
        return {"committed": True, "duplicate": False, "ingested": inserted}

    @staticmethod
    def _prepare_api_limits(db, api, global_not_before=0.0):
        if not hasattr(api, "load_route_limits"):
            return
        rows = db.execute("SELECT route_key,bucket_key,remaining,reset_at FROM api_rate_limits").fetchall()
        api.load_route_limits(rows, global_not_before)

    @staticmethod
    def _persist_api_limits(db, api, thread_id=None, now=None):
        updates = getattr(api, "rate_updates", None)
        if not updates:
            return
        now = time.time() if now is None else now
        with db:
            for row in updates:
                db.execute("INSERT INTO api_rate_limits(route_key,bucket_key,remaining,limit_total,reset_at,updated_at) "
                           "VALUES(?,?,?,?,?,?) ON CONFLICT(route_key) DO UPDATE SET "
                           "bucket_key=excluded.bucket_key,remaining=excluded.remaining,"
                           "limit_total=excluded.limit_total,reset_at=excluded.reset_at,"
                           "updated_at=excluded.updated_at",
                           (row["route"], row["bucket_key"], row["remaining"],
                            row["limit_total"], row["reset_at"], row["updated_at"]))
                if thread_id is not None and row["remaining"] == 0 and row["reset_at"] > now:
                    db.execute("UPDATE threads SET next_poll_at=MAX(next_poll_at,?) WHERE thread_id=?",
                               (row["reset_at"], thread_id))
            del updates[:]
        global_until = getattr(api, "global_not_before", 0.0)
        if global_until > now:
            with db:
                db.execute("INSERT INTO meta(key,value) VALUES('global_not_before',?) "
                           "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS REAL),CAST(excluded.value AS REAL))",
                           (str(global_until),))

    @staticmethod
    def _api_deferred_until(db, api, path, now):
        if hasattr(api, "deferred_until"):
            return api.deferred_until(path, now)
        return 0.0

    def record_api_retry(self, api, path, retry_after, global_limit=False, now=None):
        now = self.clock() if now is None else now
        wait = retry_after if isinstance(retry_after, (int, float)) and math.isfinite(retry_after) else 0
        deadline = now + min(MAX_RATE_LIMIT_WAIT, max(0, wait))
        if global_limit:
            self.global_not_before = max(self.global_not_before, deadline)
            with contextlib.closing(self.database(create=False)) as db:
                with db:
                    db.execute("INSERT INTO meta(key,value) VALUES('global_not_before',?) "
                               "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS REAL),"
                               "CAST(excluded.value AS REAL))", (str(deadline),))
            if hasattr(api, "global_not_before"):
                api.global_not_before = max(api.global_not_before, deadline)
            return
        route = api.route_key(path) if hasattr(api, "route_key") else "GET " + path
        with contextlib.closing(self.database(create=False)) as db:
            with db:
                db.execute("INSERT INTO api_rate_limits(route_key,bucket_key,remaining,limit_total,reset_at,updated_at) "
                           "VALUES(?,?,0,NULL,?,?) ON CONFLICT(route_key) DO UPDATE SET "
                           "remaining=0,reset_at=MAX(reset_at,excluded.reset_at),updated_at=excluded.updated_at",
                           (route, route, deadline, now))

    def hailing_config(self):
        path = self.hailing_state / "config.json"
        path = ensure_private_file(path, "Hailing Frequencies config")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("Hailing Frequencies config is invalid") from None
        require(value.get("version") == 1 and isinstance(value.get("targets"), list),
                "Unsupported Hailing Frequencies config")
        return value

    @staticmethod
    def _launch_kind():
        launch = os.environ.get("CMUX_AGENT_LAUNCH_KIND", "").strip().lower()
        launch = {"claudeteams": "claude"}.get(launch, launch)
        require(launch in ALLOWED_LAUNCH_KINDS,
                "Cannot verify this caller; run Discord Watch from an enrolled Claude, Codex, or OpenCode session")
        return launch

    def current_owner(self, verify=True, session_hint=None):
        harness = self._launch_kind()
        env_key = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID",
                   "opencode": "OPENCODE_SESSION_ID"}[harness]
        session_value = os.environ.get(env_key)
        surface_value = os.environ.get("CMUX_SURFACE_ID")
        require(surface_value, "Missing verified CMUX_SURFACE_ID")
        surface = hf.identity(surface_value)
        config = self.hailing_config()
        matches = [item for item in config["targets"]
                   if item.get("surface") == surface and item.get("harness") == harness]
        workspace = os.environ.get("CMUX_WORKSPACE_ID")
        if workspace:
            workspace = hf.identity(workspace)
            matches = [item for item in matches if item.get("workspace") == workspace]
        if harness == "opencode":
            require(workspace is not None, "OpenCode owner lookup requires the exact CMUX_WORKSPACE_ID")
            require(isinstance(session_value, str) and session_value,
                    "OpenCode owner lookup requires the exact OPENCODE_SESSION_ID")
        selected_session = session_hint or session_value
        if selected_session:
            session = hf.session_identity(selected_session, harness)
            matches = [item for item in matches if item.get("session") == session]
        require(len(matches) == 1, "Caller is not an exact enrolled Hailing Frequencies target")
        target = matches[0]
        if workspace is not None:
            require(workspace == target.get("workspace"), "Caller workspace differs from its enrolled owner")
        if harness == "opencode":
            require(session_value is None or hf.session_identity(session_value, harness) == target["session"],
                    "Caller OpenCode session differs from the registered owner")
        if session_hint and session_value:
            require(hf.session_identity(session_hint, harness) == hf.session_identity(session_value, harness),
                    "Selected session differs from the current harness session")
        target = self.binding_snapshot(target)
        if verify:
            self.cmux_factory().binding(target)
        return target

    def _identity_owner(self, session_hint=None):
        """Verify the exact Discord Watch owner without prompt input readiness."""
        current = self.current_owner(verify=False, session_hint=session_hint)
        cmux = self.cmux_factory()
        if current["harness"] == "opencode":
            cmux.owner_identity_binding(current)
        else:
            cmux.binding(current, readiness_policy=hf.DISCORD_REPLY_READINESS)
        return current

    @staticmethod
    def binding_snapshot(target):
        keys = ("harness", "session", "surface", "workspace", "started_at")
        if target.get("harness") == "opencode":
            keys += ("api_url", "password_file", "expected_server_pid", "session_directory",
                     "worktree_directory", "parent_task_id")
        require(all(key in target for key in keys), "Hailing Frequencies owner binding is incomplete")
        snapshot = {key: target[key] for key in keys}
        if target.get("harness") == "opencode":
            snapshot["delivery_policy"] = hf.opencode_delivery_policy(target)
        return snapshot

    def validate_registered_owner(self, target, verify=True, *,
                                  readiness_policy=hf.NATIVE_PROMPT_READINESS):
        config = self.hailing_config()
        snapshot = self.binding_snapshot(target)
        matches = [item for item in config["targets"]
                   if self.binding_snapshot(item) == snapshot]
        require(len(matches) == 1, "Registered owner is no longer enrolled in Hailing Frequencies")
        if verify:
            cmux = self.cmux_factory()
            if readiness_policy == hf.NATIVE_PROMPT_READINESS:
                cmux.binding(snapshot)
            else:
                cmux.binding(snapshot, readiness_policy=readiness_policy)

    def read_credentials(self):
        values = read_notify_credentials(self.notify_home)
        self._configured_user_id = values["user_id"]
        return values, self.discord_factory(values["token"])

    @staticmethod
    def thread_closed(remote, closed_tag):
        metadata = remote.get("thread_metadata")
        archived = isinstance(metadata, dict) and metadata.get("archived") is True
        applied = remote.get("applied_tags")
        tagged_closed = closed_tag is not None and isinstance(applied, list) and closed_tag in applied
        return archived or tagged_closed

    def remote_thread(self, api, thread_id):
        remote = api.get(f"/channels/{thread_id}")
        require(isinstance(remote, dict) and snowflake(remote.get("id"), "remote thread ID") == thread_id,
                "Discord channel response does not match the requested thread")
        require(remote.get("type") in (11, 12), "Discord channel is not a thread")
        return remote

    def register(self, thread_id, after=None, session_hint=None):
        thread_id = snowflake(thread_id, "thread ID")
        database_path = self.directory / "discord-watch.sqlite3"
        if database_path.is_file() and not database_path.is_symlink():
            with contextlib.closing(self.database(create=False)) as existing_db:
                require(not self._custody_adoption_hold(existing_db, thread_id),
                        "Thread is held by an explicit custody adoption")
        owner = self._identity_owner(session_hint=session_hint)
        credentials, api = self.read_credentials()
        remote = self.remote_thread(api, thread_id)
        remote_closed = self.thread_closed(remote, credentials["closed_tag"])
        if after is not None:
            cursor = snowflake(after, "after cursor")
        else:
            cursor = thread_id
        require(id_order(cursor) >= id_order(thread_id), "Cursor cannot precede thread creation")
        now = self.clock()
        with contextlib.closing(self.database()) as db:
            existing = db.execute("SELECT owner_json,cursor,initial_cutoff FROM threads WHERE thread_id=?",
                                  (thread_id,)).fetchone()
            if existing is not None:
                existing_owner = json.loads(existing[0])
                require(existing_owner == owner,
                        "Thread already belongs to another exact session; use transfer explicitly")
                if after is not None:
                    require(cursor in (existing[2], existing[1]),
                            "Thread is already registered with a different initial cutoff or current cursor")
                with db:
                    db.execute("UPDATE threads SET updated_at=?,remote_json=?,remote_closed=? WHERE thread_id=?",
                               (now, json.dumps(remote, separators=(",", ":")), int(remote_closed), thread_id))
                return {"thread": thread_id, "registered": True, "already_registered": True,
                        "owner_session": owner["session"], "cursor": existing[1]}
            require(not remote_closed, "Discord thread is archived or tagged Closed")
            with db:
                db.execute("INSERT INTO threads(thread_id,owner_json,cursor,initial_cutoff,is_open,registered_at,"
                           "updated_at,remote_json) VALUES(?,?,?,?,1,?,?,?)",
                           (thread_id, json.dumps(owner, separators=(",", ":")), cursor, cursor, now, now,
                            json.dumps(remote, separators=(",", ":"))))
                self._ensure_gateway_recovery_coverage(db, now)
        return {"thread": thread_id, "registered": True, "already_registered": False,
                "owner_session": owner["session"], "cursor": cursor,
                "cursor_source": "explicit" if after is not None else "thread-created"}

    def _thread(self, db, thread_id, require_open=True):
        thread_id = snowflake(thread_id, "thread ID")
        row = db.execute("SELECT thread_id,owner_json,cursor,is_open,registered_at,updated_at,last_poll_at,"
                         "next_poll_at,catchup_before,catchup_max,last_error_json,remote_json,owner_generation,"
                         "subscription_state,last_activity_at,remote_closed,initial_cutoff "
                         "FROM threads WHERE thread_id=?",
                         (thread_id,)).fetchone()
        require(row is not None, "Thread is not registered")
        result = dict(zip(("thread_id", "owner_json", "cursor", "is_open", "registered_at", "updated_at",
                           "last_poll_at", "next_poll_at", "catchup_before", "catchup_max",
                           "last_error_json", "remote_json", "owner_generation", "subscription_state",
                           "last_activity_at", "remote_closed", "initial_cutoff"), row))
        result["owner"] = json.loads(result.pop("owner_json"))
        result["last_error"] = json.loads(result["last_error_json"]) if result["last_error_json"] else None
        result["remote"] = json.loads(result.pop("remote_json"))
        require(not require_open or result["is_open"], "Thread registration is closed")
        return result

    def _custody_adoption_hold(self, db, thread_id):
        rows = db.execute(
            "SELECT operation_id,plan_digest FROM custody_adoption_holds WHERE thread_id=?",
            (thread_id,)).fetchall()
        path = self.hailing_state / "receipts.sqlite3"
        try:
            if path.is_symlink() or not path.is_file():
                return None if not rows else {"operation_id": rows[0][0], "state": "missing"}
            with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                                    uri=True, timeout=1)) as journal:
                table = journal.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    ("custody_adoption_holds",)).fetchone()
                if table is None:
                    return None if not rows else {"operation_id": rows[0][0], "state": "missing"}
                central = journal.execute(
                    "SELECT operation_id,plan_digest FROM custody_adoption_holds "
                    "WHERE resource_kind='discord_thread' AND resource_id=?",
                    (thread_id,)).fetchall()
        except (OSError, sqlite3.Error):
            return {"operation_id": rows[0][0] if rows else "unknown", "state": "unavailable"}
        rows = list(dict.fromkeys(rows + central))
        if not rows:
            return None
        for operation_id, hold_digest in rows:
            try:
                with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                                        uri=True, timeout=1)) as journal:
                    operation = journal.execute(
                        "SELECT plan_digest,state FROM custody_adoption_operations WHERE operation_id=?",
                        (operation_id,)).fetchone()
            except (OSError, sqlite3.Error):
                return {"operation_id": operation_id, "state": "unavailable"}
            if operation is None or operation[0] != hold_digest or operation[1] != "complete":
                return {"operation_id": operation_id, "state": operation[1] if operation else "missing"}
        return None

    def _refresh_dormancy(self, db, now):
        local_holds = db.execute(
            "SELECT operation_id,thread_id,plan_digest FROM custody_adoption_holds").fetchall()
        held = set()
        path = self.hailing_state / "receipts.sqlite3"
        if path.is_symlink():
            return
        if path.is_file():
            try:
                with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                                        uri=True, timeout=1)) as journal:
                    tables = {row[0] for row in journal.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                        "('custody_adoption_holds','custody_adoption_operations')")}
                    if local_holds and not tables:
                        return
                    if tables and tables != {"custody_adoption_holds", "custody_adoption_operations"}:
                        return
                    if tables:
                        for operation_id, thread_id, hold_digest in local_holds:
                            operation = journal.execute(
                                "SELECT plan_digest,state FROM custody_adoption_operations "
                                "WHERE operation_id=?", (operation_id,)).fetchone()
                            if operation is None or operation != (hold_digest, "complete"):
                                held.add(thread_id)
                        held.update(row[0] for row in journal.execute(
                            "SELECT h.resource_id FROM custody_adoption_holds h "
                            "JOIN custody_adoption_operations o USING(operation_id) "
                            "WHERE h.resource_kind='discord_thread' "
                            "AND (h.plan_digest!=o.plan_digest OR o.state!='complete')"))
            except (OSError, sqlite3.Error):
                return
        elif local_holds:
            return
        exclusion = ""
        values = [now - DORMANT_AFTER]
        if held:
            exclusion = " AND thread_id NOT IN (" + ",".join("?" for _ in held) + ")"
            values.extend(sorted(held))
        with db:
            db.execute("UPDATE threads SET subscription_state='dormant' "
                       "WHERE subscription_state='active' "
                       "AND COALESCE(last_activity_at,registered_at)<?" + exclusion,
                       values)

    @staticmethod
    def _catchup_incomplete(thread):
        return thread["catchup_before"] is not None or thread["catchup_max"] is not None

    @staticmethod
    def _gateway_recovery_hold_reason(db, thread):
        epoch = db.execute("SELECT recovery_epoch FROM gateway_state WHERE id=1").fetchone()[0]
        if epoch <= 0:
            return None
        work = db.execute("SELECT state FROM gateway_recovery_work WHERE epoch=? AND thread_id=? "
                          "AND owner_generation=?", (epoch, thread["thread_id"],
                                                       thread["owner_generation"])).fetchone()
        if work is None:
            return "gateway_recovery_coverage_missing"
        if work[0] not in ("complete", "superseded"):
            return "gateway_recovery_incomplete"
        return None

    def managed(self, thread_id, session_hint=None):
        thread_id = snowflake(thread_id, "thread ID")
        current = self._identity_owner(session_hint=session_hint)
        with contextlib.closing(self.database(create=False)) as db:
            thread = self._thread(db, thread_id, require_open=False)
            require(not self._custody_adoption_hold(db, thread_id),
                    "Thread is held by an explicit custody adoption")
            require(thread["owner"] == current,
                    "Thread belongs to another exact session; use explicit transfer")
            active = db.execute("SELECT state,COUNT(*) FROM messages WHERE thread_id=? GROUP BY state",
                                (thread_id,)).fetchall()
            counts = {state: count for state, count in active}
            status = self._health(db, thread_id)
        return {"thread": thread_id, "managed": True, "owner_session": current["session"],
                "open": bool(thread["is_open"]), "subscription_state": thread["subscription_state"],
                "remote_closed": bool(thread["remote_closed"]),
                "queue": counts, "worker_health": status}

    def close(self, thread_id, session_hint=None):
        thread_id = snowflake(thread_id, "thread ID")
        current = self._identity_owner(session_hint=session_hint)
        now = self.clock()
        with contextlib.closing(self.database(create=False)) as db:
            thread = self._thread(db, thread_id, require_open=False)
            require(not self._custody_adoption_hold(db, thread_id),
                    "Thread is held by an explicit custody adoption")
            require(thread["owner"] == current,
                    "Only the registered current owner can close this Discord Watch registration")
            with db:
                db.execute("UPDATE threads SET is_open=0,updated_at=? WHERE thread_id=?",
                           (now, thread_id))
        return {"thread": thread_id, "closed": True, "subscribed": True, "pending_preserved": True}

    def ingest_gateway_message(self, thread_id, message):
        """Durably ingest one Gateway message without changing the REST cursor."""
        thread_id = snowflake(thread_id, "thread ID")
        if self._configured_user_id is None:
            credentials, _ = self.read_credentials()
            self._configured_user_id = credentials["user_id"]
        now = self.clock()
        with contextlib.closing(self.database(create=False)) as db:
            self._refresh_dormancy(db, now)
            if self._custody_adoption_hold(db, thread_id):
                return {"thread": thread_id, "message": None, "ingested": False,
                        "held": "custody_adoption"}
            with db:
                db.execute("BEGIN IMMEDIATE")
                inserted = self._ingest_message(db, thread_id, message, self._configured_user_id, now,
                                                source="gateway")
            message_id = snowflake(message.get("id"), "message ID") if isinstance(message, dict) else None
        return {"thread": thread_id, "message": message_id, "ingested": inserted,
                "rest_cursor_advanced": False}

    def transfer(self, thread_id, from_session, session_hint=None):
        thread_id = snowflake(thread_id, "thread ID")
        old_session = hf.session_identity(from_session)
        # Resolve and verify the caller before taking the short SQLite write lock.
        current = self._identity_owner(session_hint=session_hint)
        now = self.clock()
        with contextlib.closing(self.database(create=False)) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                thread = self._thread(db, thread_id, require_open=False)
                require(not self._custody_adoption_hold(db, thread_id),
                        "Thread is held by an explicit custody adoption")
                require(thread["owner"].get("session") == old_session,
                        "--from-session does not match the registered owner")
                require(thread["owner"] != current, "Thread already belongs to this exact session")
                generation = thread["owner_generation"] + 1
                stored_owner_json = db.execute("SELECT owner_json FROM threads WHERE thread_id=?",
                                               (thread_id,)).fetchone()[0]
                updated = db.execute("UPDATE threads SET owner_json=?,owner_generation=?,updated_at=? "
                                     "WHERE thread_id=? AND owner_json=? AND owner_generation=?",
                                     (json.dumps(current, separators=(",", ":")), generation, now,
                                      thread_id, stored_owner_json, thread["owner_generation"]))
                require(updated.rowcount == 1,
                        "Thread owner changed before the explicit transfer could commit")
                db.execute("INSERT INTO ownership_events(thread_id,from_session,to_session,at,owner_generation) "
                           "VALUES(?,?,?,?,?)", (thread_id, old_session, current["session"], now, generation))
                db.execute("UPDATE messages SET state='pending',wake_attempts=0,last_wake_at=NULL,submitted_at=NULL "
                           "WHERE thread_id=? AND group_id IS NULL AND state='awaiting_receipt'", (thread_id,))
                db.execute("UPDATE messages SET owner_generation=? WHERE thread_id=? AND group_id IS NULL "
                           "AND state!='received'", (generation, thread_id))
                self._ensure_gateway_recovery_coverage(db, now)
        return {"thread": thread_id, "transferred": True, "from_session": old_session,
                "to_session": current["session"], "pending_preserved": True}

    @staticmethod
    def _attachment_json(row):
        values = row.get("attachments", [])
        require(isinstance(values, list) and len(values) <= 10,
                "Discord attachment list exceeds the bounded storage size")
        normalized = []
        for item in values:
            require(isinstance(item, dict), "Discord returned a malformed attachment")
            filename, url = item.get("filename"), item.get("url")
            require(isinstance(filename, str) and isinstance(url, str),
                    "Discord attachment filename or URL is missing")
            require(len(filename.encode("utf-8")) <= 512 and len(url.encode("utf-8")) <= 2048 and
                    url.startswith("https://") and not any(ord(c) < 32 for c in filename + url),
                    "Discord attachment metadata exceeds the bounded format")
            normalized.append({"filename": filename, "url": url})
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        require(len(encoded.encode("utf-8")) <= 16 * 1024,
                "Discord attachment metadata exceeds the bounded storage size")
        return encoded

    def _ingest_message(self, db, thread_id, row, configured_user_id, now, source="rest"):
        thread_id = snowflake(thread_id, "thread ID")
        require(isinstance(row, dict), "Discord returned a malformed message")
        message_id = snowflake(row.get("id"), "message ID")
        require(source in ("rest", "gateway"), "Unknown Discord message source")
        thread = db.execute("SELECT last_activity_at,initial_cutoff,owner_generation FROM threads WHERE thread_id=?",
                            (thread_id,)).fetchone()
        require(thread is not None, "Thread is not registered")
        channel_id = row.get("channel_id")
        if channel_id is not None:
            require(snowflake(channel_id, "message channel ID") == thread_id,
                    "Discord message channel ID does not match the selected thread")
        author = row.get("author") if isinstance(row.get("author"), dict) else {}
        author_id = str(author.get("id", ""))
        if (author_id != snowflake(configured_user_id, "configured Discord user ID") or
                author.get("bot", False) or row.get("webhook_id") is not None):
            return False
        if source == "gateway" and id_order(message_id) <= id_order(
                snowflake(thread[1], "initial intake cutoff")):
            return False
        body = row.get("content", "")
        require(isinstance(body, str), "Discord message content is not text")
        require(len(body.encode("utf-8")) <= MAX_MESSAGE_BYTES,
                "Discord message content exceeds the bounded storage size")
        attachment_json = self._attachment_json(row)
        author_name = str(author.get("global_name") or author.get("username") or "Configured user")[:256]
        created_at = row.get("timestamp", "")
        activity_at = timestamp_epoch(created_at)
        inserted = db.execute(
            "INSERT OR IGNORE INTO messages(thread_id,message_id,author_id,author_name,body,attachments_json,"
            "created_at,groupable,ingested_at,owner_generation) VALUES(?,?,?,?,?,?,?,1,?,?)",
            (thread_id, message_id, author_id, author_name, body, attachment_json, created_at,
             now, thread[2])).rowcount == 1
        if inserted:
            previous_activity = thread[0]
            if previous_activity is None or activity_at >= previous_activity:
                latest_activity = activity_at if previous_activity is None else max(activity_at, previous_activity)
                subscription_state = "active" if now <= activity_at + DORMANT_AFTER else "dormant"
                db.execute("UPDATE threads SET last_activity_at=?,subscription_state=? WHERE thread_id=?",
                           (latest_activity, subscription_state, thread_id))
        return inserted

    def _api_pages(self, api, thread_id, cursor, owner_id, db, max_pages=None,
                   stop_event=None):
        max_pages = MAX_CATCHUP_PAGES if max_pages is None else max_pages
        pages = 0
        total = 0
        deferred_until = 0.0
        thread_row = db.execute("SELECT cursor,catchup_before,catchup_max FROM threads WHERE thread_id=?",
                                (thread_id,)).fetchone()
        require(thread_row is not None, "Thread registration disappeared during polling")
        current_cursor, boundary, caught_up_to = thread_row

        def commit_page(normalized, next_boundary, next_max, complete):
            nonlocal current_cursor, total
            page_ids = [item[0] for item in normalized]
            if complete:
                final_cursor = next_max or (page_ids[-1] if page_ids else current_cursor)
                next_boundary = None
                next_max = None
            else:
                final_cursor = current_cursor
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            db.execute("SAVEPOINT discord_watch_page")
            try:
                seen = set()
                for message_id, row in normalized:
                    require(message_id not in seen, "Discord returned duplicate message IDs")
                    seen.add(message_id)
                    self._ingest_message(db, thread_id, row, owner_id, self.clock())
                    total += 1
                db.execute("UPDATE threads SET cursor=CASE "
                           "WHEN length(cursor)<length(?) OR (length(cursor)=length(?) AND cursor<?) THEN ? "
                           "ELSE cursor END,catchup_before=?,catchup_max=?,updated_at=? WHERE thread_id=?",
                           (final_cursor, final_cursor, final_cursor, final_cursor,
                            next_boundary, next_max, self.clock(), thread_id))
                db.execute("RELEASE SAVEPOINT discord_watch_page")
                db.commit()
                current_cursor = final_cursor
            except BaseException:
                db.execute("ROLLBACK TO SAVEPOINT discord_watch_page")
                db.execute("RELEASE SAVEPOINT discord_watch_page")
                db.rollback()
                raise

        while pages < max_pages:
            if stop_event is not None and stop_event.is_set():
                break
            if boundary:
                query = {"before": boundary, "limit": MESSAGE_LIMIT}
            else:
                query = {"after": current_cursor, "limit": MESSAGE_LIMIT}
            path = f"/channels/{thread_id}/messages"
            deferred_until = self._api_deferred_until(db, api, path, self.clock())
            if deferred_until > self.clock():
                break
            rows = api.get(path, query)
            self._persist_api_limits(db, api, thread_id, self.clock())
            require(isinstance(rows, list), "Discord messages response was not an array")
            require(len(rows) <= MESSAGE_LIMIT, "Discord returned more messages than requested")
            pages += 1
            normalized = []
            all_ids = []
            for row in rows:
                require(isinstance(row, dict), "Discord returned a malformed message")
                message_id = snowflake(row.get("id"), "message ID")
                all_ids.append(message_id)
                if boundary:
                    require(id_order(message_id) < id_order(boundary),
                            "Discord before-pagination did not move toward the committed cursor")
                if id_order(message_id) > id_order(current_cursor):
                    normalized.append((message_id, row))
            normalized.sort(key=lambda item: id_order(item[0]))
            if len(set(all_ids)) != len(all_ids):
                raise ValueError("Discord returned duplicate message IDs")
            if not rows:
                if boundary:
                    commit_page([], None, caught_up_to, True)
                break
            if boundary:
                next_max = caught_up_to
                complete = len(rows) < MESSAGE_LIMIT or (
                    all_ids and id_order(min(all_ids, key=id_order)) <= id_order(current_cursor))
                next_boundary = (min(all_ids, key=id_order) if all_ids else None)
            else:
                complete = len(rows) < MESSAGE_LIMIT
                next_max = max(all_ids, key=id_order) if all_ids else current_cursor
                next_boundary = min(all_ids, key=id_order) if all_ids and not complete else None
            if len(rows) == MESSAGE_LIMIT and not normalized and not complete:
                raise ValueError("Discord pagination did not advance toward the committed cursor")
            commit_page(normalized, next_boundary, next_max, complete)
            boundary = None if complete else next_boundary
            caught_up_to = None if complete else next_max
            if complete:
                break
        row = db.execute("SELECT cursor,catchup_before FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
        return {"pages": pages, "messages_seen": total, "cursor": row[0],
                "catchup_before": row[1], "catchup_pending": row[1] is not None,
                "catchup_limited": row[1] is not None and pages >= max_pages,
                "deferred_until": deferred_until}

    def _record_thread_error(self, db, thread_id, error, now):
        if isinstance(error, DiscordAPIError):
            record = error.record()
            wait = error.retry_after
            next_poll = now + max(POLL_INTERVAL, wait or 0)
            if error.global_rate_limit:
                self.global_not_before = max(self.global_not_before,
                                             now + max(POLL_INTERVAL, wait or 0))
                with db:
                    db.execute("INSERT INTO meta(key,value) VALUES('global_not_before',?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (str(self.global_not_before),))
        else:
            record = {"error": str(error)[:500]}
            next_poll = now + POLL_INTERVAL
        with db:
            db.execute("UPDATE threads SET last_error_json=?,next_poll_at=?,updated_at=? WHERE thread_id=?",
                       (json.dumps(record, separators=(",", ":")), next_poll, now, thread_id))
        return record

    def _poll_thread(self, db, thread, api, credentials, now, max_pages=None,
                     routine=False, stop_event=None):
        max_pages = MAX_CATCHUP_PAGES if max_pages is None else max_pages
        thread_id = thread["thread_id"]
        try:
            with self.locked(timeout=0, name=f"poll-{thread_id}.lock"):
                latest = self._thread(db, thread_id, require_open=False)
                return self._poll_thread_locked(db, latest, api, credentials, now,
                                                max_pages=max_pages, routine=routine,
                                                stop_event=stop_event)
        except ValueError as error:
            if "operation owns this state" in str(error):
                return {"thread": thread_id, "status": "poll_busy"}
            raise

    def _poll_thread_locked(self, db, thread, api, credentials, now, max_pages=None,
                            routine=False, stop_event=None):
        max_pages = MAX_CATCHUP_PAGES if max_pages is None else max_pages
        thread_id = thread["thread_id"]
        if self._custody_adoption_hold(db, thread_id):
            return {"thread": thread_id, "status": "custody_adoption_hold", "poll_skipped": True}
        if stop_event is not None and stop_event.is_set():
            return {"thread": thread_id, "status": "stopped"}
        if now < self.global_not_before:
            return {"thread": thread_id, "status": "global_backoff",
                    "retry_after": max(0, int(self.global_not_before - now))}
        if now < thread["next_poll_at"]:
            return {"thread": thread_id, "status": "backoff",
                    "retry_after": max(0, int(thread["next_poll_at"] - now))}
        remote_path = f"/channels/{thread_id}"
        deferred = self._api_deferred_until(db, api, remote_path, now)
        if deferred > now:
            with db:
                db.execute("UPDATE threads SET next_poll_at=MAX(next_poll_at,?) WHERE thread_id=?",
                           (deferred, thread_id))
            return {"thread": thread_id, "status": "rate_limited", "retry_at": deferred}
        try:
            remote = self.remote_thread(api, thread_id)
            self._persist_api_limits(db, api, thread_id, now)
            if stop_event is not None and stop_event.is_set():
                return {"thread": thread_id, "status": "stopped"}
            remote_closed = self.thread_closed(remote, credentials["closed_tag"])
            remote_json = json.dumps(remote, separators=(",", ":"))
            with db:
                db.execute("UPDATE threads SET remote_json=?,remote_closed=?,updated_at=? WHERE thread_id=?",
                           (remote_json, int(remote_closed), now, thread_id))
            result = self._api_pages(api, thread_id, thread["cursor"], credentials["user_id"], db,
                                     max_pages=max_pages, stop_event=stop_event)
            if stop_event is not None and stop_event.is_set():
                return {"thread": thread_id, "status": "stopped", **result}
            if result["deferred_until"] > self.clock():
                with db:
                    db.execute("UPDATE threads SET next_poll_at=MAX(next_poll_at,?) WHERE thread_id=?",
                               (result["deferred_until"], thread_id))
                return {"thread": thread_id, "status": "rate_limited",
                        "retry_at": result["deferred_until"], **result}
            with db:
                next_poll = now + ROUTINE_REST_INTERVAL if routine else 0
                db.execute("UPDATE threads SET last_poll_at=?,next_poll_at=MAX(next_poll_at,?),"
                           "last_error_json=NULL,updated_at=? "
                           "WHERE thread_id=?", (now, next_poll, now, thread_id))
            return {"thread": thread_id, "status": "polled", "remote_closed": remote_closed, **result}
        except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
            self._persist_api_limits(db, api, thread_id, now)
            record = self._record_thread_error(db, thread_id, error, now)
            return {"thread": thread_id, "status": "error", "error": record}

    def _wake_prompt(self, thread_id, message_id, owner):
        launcher = shutil.which("discord-watch") or "discord-watch"
        args = [launcher, "--state-dir", str(self.directory),
                "--notify-home", str(self.notify_home),
                "--hailing-state", str(self.hailing_state),
                "--cmux", self.cmux_executable,
                "receive", "--thread", thread_id]
        if owner.get("harness") == "opencode":
            args.extend(("--session", owner["session"]))
        args.extend(("--message", message_id))
        command = shlex.join(args)
        return ("A reply from the configured Discord user is ready in Discord Watch. At a safe checkpoint, retrieve the exact reply with:\n"
                + command)

    @staticmethod
    def _render_messages(thread_id, rows):
        rendered_parts = []
        for message_id, author_name, body, attachments_json in rows:
            rendered_parts.append(f"[discord:{thread_id}:{message_id}] {author_name}: {body}")
            for attachment in json.loads(attachments_json):
                filename = re.sub(r"[\x00-\x1f\x7f]", " ", attachment["filename"])
                rendered_parts.append(f"[attachment] {filename}: {attachment['url']}")
        return "\n".join(rendered_parts) + "\n"

    def _generation_chain_valid(self, db, thread, group_generation, group_owner):
        current_generation = thread["owner_generation"]
        if (not isinstance(group_owner, dict) or type(group_generation) is not int or
                group_generation < 1 or group_generation > current_generation):
            return False
        if current_generation == 1:
            return group_generation == 1 and group_owner == thread["owner"]
        events = db.execute(
            "SELECT owner_generation,from_session,to_session,event_type,from_owner_json,to_owner_json,"
            "operation_id,plan_digest,old_owner_digest,new_owner_digest,before_digest,"
            "after_preserved_digest,from_worktree,to_worktree,continuity "
            "FROM ownership_events WHERE thread_id=? AND owner_generation<=? ORDER BY owner_generation",
            (thread["thread_id"], current_generation)).fetchall()
        if len(events) != current_generation - 1:
            return False
        previous = events[0][1]
        sessions = {1: previous}
        exact_owners = {}
        for expected_generation, event in enumerate(events, start=2):
            (generation, from_session, to_session, event_type, from_owner_json, to_owner_json,
             operation_id, plan_digest, old_owner_digest, new_owner_digest, before_digest,
             after_preserved_digest, from_worktree, to_worktree, continuity) = event
            if generation != expected_generation or from_session != previous:
                return False
            if event_type == "custody_adoption":
                try:
                    from_owner = json.loads(from_owner_json)
                    to_owner = json.loads(to_owner_json)
                except (TypeError, ValueError):
                    return False
                old_harness = from_owner.get("harness") if isinstance(from_owner, dict) else None
                if old_harness == "opencode" and isinstance(from_owner, dict):
                    try:
                        old_worktree_matches = (
                            isinstance(from_owner.get("worktree_directory"), str) and
                            str(Path(from_owner["worktree_directory"]).expanduser().resolve(strict=True)) ==
                            from_worktree)
                    except (OSError, RuntimeError):
                        old_worktree_matches = False
                else:
                    old_worktree_matches = from_worktree == "not_recorded_by_hf_or_dw"
                if (not isinstance(from_owner, dict) or not isinstance(to_owner, dict) or
                        from_owner.get("session") != from_session or
                        to_owner.get("session") != to_session or
                        continuity != "not_established" or
                        not old_worktree_matches or
                        not isinstance(to_worktree, str) or not to_worktree):
                    return False
                try:
                    if (self.binding_snapshot(from_owner) != from_owner or
                            self.binding_snapshot(to_owner) != to_owner):
                        return False
                except (ValueError, KeyError, TypeError):
                    return False
                if (not isinstance(operation_id, str) or
                        re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                                     operation_id) is None or
                        not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                                for value in (plan_digest, old_owner_digest, new_owner_digest,
                                              before_digest, after_preserved_digest))):
                    return False
                if (_sha256_owner(from_owner) != old_owner_digest or
                        _sha256_owner(to_owner) != new_owner_digest or
                        (from_owner["harness"], from_owner["session"], from_owner["surface"],
                         from_owner["workspace"]) !=
                        (to_owner["harness"], to_owner["session"], to_owner["surface"],
                         to_owner["workspace"]) or
                        from_owner["started_at"] == to_owner["started_at"]):
                    return False
                if generation - 1 in exact_owners and exact_owners[generation - 1] != from_owner:
                    return False
                exact_owners[generation - 1] = from_owner
                exact_owners[generation] = to_owner
            elif event_type != "transfer":
                return False
            sessions[generation - 1] = from_session
            sessions[generation] = to_session
            previous = to_session
        if previous != thread["owner"].get("session"):
            return False
        if sessions.get(group_generation) != group_owner.get("session"):
            return False
        if group_generation in exact_owners:
            return group_owner == exact_owners[group_generation]
        if group_generation == current_generation:
            return group_owner == thread["owner"]
        return True

    @staticmethod
    def _reconcile_group_receipts(db, thread_id=None):
        clause = " AND thread_id=?" if thread_id is not None else ""
        params = (thread_id,) if thread_id is not None else ()
        db.execute("UPDATE delivery_groups SET status='received' WHERE status!='received'" + clause +
                   " AND NOT EXISTS (SELECT 1 FROM delivery_group_members gm JOIN messages m "
                   "ON m.thread_id=gm.thread_id AND m.message_id=gm.message_id "
                   "WHERE gm.group_id=delivery_groups.id AND m.state!='received')", params)

    def _seal_delivery_groups(self, db, thread, now, force=False, max_groups=None):
        formed = []
        while max_groups is None or len(formed) < max_groups:
            require(not db.in_transaction,
                    "Group freezing requires a fresh bounded SQLite transaction")
            db.execute("BEGIN IMMEDIATE")
            try:
                latest = self._thread(db, thread["thread_id"], require_open=False)
                require(latest["owner"] == thread["owner"] and
                        latest["owner_generation"] == thread["owner_generation"],
                        "Exact owner changed before a delivery group could be frozen")
                if self._catchup_incomplete(latest):
                    db.commit()
                    return formed
                if not self._generation_chain_valid(db, latest, latest["owner_generation"],
                                                    latest["owner"]):
                    db.commit()
                    return formed + [{"state": "held", "reason": "owner_generation_event_chain_invalid"}]
                self._ensure_gateway_recovery_coverage(db, now)
                candidates = db.execute(
                    "SELECT message_id,author_name,body,attachments_json,ingested_at FROM messages "
                    "WHERE thread_id=? AND groupable=1 AND group_id IS NULL AND state='pending' "
                    "AND owner_generation=? ORDER BY length(message_id),message_id",
                    (latest["thread_id"], latest["owner_generation"])).fetchall()
                if not candidates:
                    db.commit()
                    return formed
                recovery_hold = self._gateway_recovery_hold_reason(db, latest)
                if recovery_hold:
                    db.commit()
                    return formed + [{"state": "held", "reason": recovery_hold}]
                prefix = []
                for row in candidates[:MAX_RECEIVE_MESSAGES]:
                    tentative = prefix + [row]
                    rendered = self._render_messages(latest["thread_id"],
                                                     [item[:4] for item in tentative])
                    if len(rendered.encode("utf-8")) > MAX_RECEIVE_BYTES:
                        if not prefix:
                            raise ValueError("A stored reply exceeds the bounded group output size")
                        break
                    prefix = tentative
                if not prefix:
                    db.commit()
                    return formed
                first_at = min(row[4] for row in prefix)
                last_at = max(row[4] for row in prefix)
                if not force and now - first_at < GROUP_MAX_DELAY and now - last_at < GROUP_QUIET_SECONDS:
                    db.commit()
                    return formed
                # Recheck the barriers immediately before the immutable membership write.
                latest = self._thread(db, latest["thread_id"], require_open=False)
                require(latest["owner"] == thread["owner"] and
                        latest["owner_generation"] == thread["owner_generation"],
                        "Exact owner changed while a delivery group was being frozen")
                require(not self._catchup_incomplete(latest),
                        "REST catch-up began while a delivery group was being frozen")
                self._ensure_gateway_recovery_coverage(db, now)
                recovery_hold = self._gateway_recovery_hold_reason(db, latest)
                if recovery_hold:
                    db.commit()
                    return formed + [{"state": "held", "reason": recovery_hold}]
                require(self._generation_chain_valid(db, latest, latest["owner_generation"],
                                                     latest["owner"]),
                        "Owner generation changed while a delivery group was being frozen")
                ids = [row[0] for row in prefix]
                cursor = db.execute(
                    "INSERT INTO delivery_groups(thread_id,owner_generation,owner_json,status,created_at,"
                    "first_message_id,last_message_id) VALUES(?,?,?,'eligible',?,?,?)",
                    (latest["thread_id"], latest["owner_generation"],
                     json.dumps(latest["owner"], separators=(",", ":")), now, ids[0], ids[-1]))
                group_id = cursor.lastrowid
                for ordinal, row in enumerate(prefix):
                    message_id = row[0]
                    db.execute("INSERT INTO delivery_group_members(group_id,thread_id,message_id,ordinal) "
                               "VALUES(?,?,?,?)", (group_id, latest["thread_id"], message_id, ordinal))
                    updated = db.execute("UPDATE messages SET group_id=? WHERE thread_id=? AND message_id=? "
                                         "AND group_id IS NULL AND state='pending' AND owner_generation=?",
                                         (group_id, latest["thread_id"], message_id,
                                          latest["owner_generation"]))
                    require(updated.rowcount == 1, "Reply changed while a delivery group was being frozen")
                db.commit()
            except BaseException:
                if db.in_transaction:
                    db.rollback()
                raise
            formed.append({"group": group_id, "messages": ids, "state": "eligible"})
        return formed

    def _group_wake_prompt(self, thread_id, group_id, owner):
        args = [sys.executable, str(Path(__file__).resolve()),
                "--state-dir", str(self.directory),
                "--notify-home", str(self.notify_home),
                "--hailing-state", str(self.hailing_state),
                "--cmux", self.cmux_executable,
                "receive", "--thread", thread_id]
        if owner.get("harness") == "opencode":
            args.extend(("--session", owner["session"]))
        args.extend(("--group", str(group_id)))
        command = shlex.join(args)
        return ("A reply group from the configured Discord user is ready in Discord Watch. "
                "At a safe checkpoint, retrieve the exact group with:\n" + command)

    def _wake_one(self, db, thread, now):
        thread_id = thread["thread_id"]
        try:
            with self.locked(timeout=0, name=f"wake-{thread_id}.lock"):
                latest_thread = self._thread(db, thread_id, require_open=False)
                return self._wake_one_locked(db, latest_thread, now)
        except ValueError as error:
            if "operation owns this state" in str(error):
                return {"state": "wake_busy", "would_wake": False}
            raise

    def _wake_one_locked(self, db, thread, now):
        thread_id = thread["thread_id"]
        if self._custody_adoption_hold(db, thread_id):
            return {"state": "custody_adoption_hold", "would_wake": False}
        with db:
            self._reconcile_group_receipts(db, thread_id)
        formed = self._seal_delivery_groups(db, thread, now)
        row = db.execute("SELECT id,owner_generation,owner_json,status,attempt_count,legacy "
                         "FROM delivery_groups WHERE thread_id=? AND status!='received' "
                         "ORDER BY id LIMIT 1", (thread_id,)).fetchone()
        if row is None:
            candidates = db.execute("SELECT message_id,author_name,body,attachments_json,ingested_at FROM messages "
                                    "WHERE thread_id=? "
                                    "AND groupable=1 AND group_id IS NULL AND state='pending' "
                                    "AND owner_generation=? ORDER BY length(message_id),message_id",
                                    (thread_id, thread["owner_generation"])).fetchall()
            if candidates:
                prefix = []
                for item in candidates[:MAX_RECEIVE_MESSAGES]:
                    if len(self._render_messages(thread_id, [row[:4] for row in prefix + [item]])
                            .encode("utf-8")) > MAX_RECEIVE_BYTES:
                        break
                    prefix.append(item)
                first_at = min(item[4] for item in prefix)
                last_at = max(item[4] for item in prefix)
                eligible_at = min(first_at + GROUP_MAX_DELAY, last_at + GROUP_QUIET_SECONDS)
                held = next((item for item in formed if item.get("state") == "held"), None)
                state = "held" if self._catchup_incomplete(thread) or held else (
                    "eligible" if now >= eligible_at else "forming")
                return {"state": state, "would_wake": False,
                        "forming_messages": [item[0] for item in candidates],
                        "eligible_at": eligible_at,
                        "held": held.get("reason") if held else (
                            "rest_catchup_incomplete" if self._catchup_incomplete(thread) else None),
                        "groups_formed": formed}
            if formed:
                return {"state": formed[0]["state"], "groups_formed": formed, "would_wake": False}
            return None
        group_id, generation, group_owner_json, state, attempt_count, legacy = row
        if state in ("uncertain", "stalled", "awaiting_receipt"):
            return {"group": group_id, "state": state, "would_wake": False,
                    "groups_formed": formed}
        if state not in ("eligible", "intent"):
            return {"group": group_id, "state": "held", "would_wake": False,
                    "groups_formed": formed}
        if self._catchup_incomplete(thread) and not legacy:
            return {"group": group_id, "state": "held", "held": "rest_catchup_incomplete",
                    "would_wake": False, "groups_formed": formed}
        recovery_hold = self._gateway_recovery_hold_reason(db, thread)
        if recovery_hold and not legacy:
            return {"group": group_id, "state": "held", "held": recovery_hold,
                    "would_wake": False, "groups_formed": formed}
        group_owner = json.loads(group_owner_json)
        if not self._generation_chain_valid(db, thread, generation, group_owner):
            return {"group": group_id, "state": "held", "held": "owner_generation_event_chain_invalid",
                    "would_wake": False, "groups_formed": formed}
        owner = thread["owner"]
        readiness_policy = (hf.DISCORD_REPLY_READINESS
                            if owner["harness"] in ("claude", "codex")
                            else hf.NATIVE_PROMPT_READINESS)
        try:
            self.validate_registered_owner(owner, readiness_policy=readiness_policy)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            with db:
                db.execute("UPDATE delivery_group_attempts SET state='abandoned_before_transport' "
                           "WHERE group_id=? AND state='intent'", (group_id,))
                db.execute("UPDATE delivery_groups SET status='eligible',last_error_json=? "
                           "WHERE id=? AND status IN ('eligible','intent')",
                           (json.dumps({"held": str(error)[:500]}), group_id))
            return {"group": group_id, "state": "held", "held": str(error)[:300],
                    "would_wake": False, "groups_formed": formed}
        owner_json = json.dumps(owner, separators=(",", ":"))
        with db:
            if state == "intent":
                db.execute("UPDATE delivery_group_attempts SET state='abandoned_before_transport' "
                           "WHERE group_id=? AND state='intent'", (group_id,))
            attempt = db.execute(
                "INSERT INTO delivery_group_attempts(group_id,state,created_at,target_owner_json,"
                "target_owner_generation) VALUES(?,'intent',?,?,?)",
                (group_id, now, owner_json, thread["owner_generation"]))
            attempt_id = attempt.lastrowid
            updated = db.execute("UPDATE delivery_groups SET status='intent',last_error_json=NULL "
                                 "WHERE id=? AND status IN ('eligible','intent') AND EXISTS ("
                                 "SELECT 1 FROM threads WHERE thread_id=? AND owner_json=? AND owner_generation=?)",
                                 (group_id, thread_id, owner_json, thread["owner_generation"]))
            require(updated.rowcount == 1, "Reply group or exact owner changed before CMUX submit")
        prompt = self._group_wake_prompt(thread_id, group_id, owner)

        def mark_transport_start():
            with db:
                updated = db.execute(
                    "UPDATE delivery_groups SET status='uncertain',attempt_count=attempt_count+1,"
                    "last_wake_at=?,submitted_at=NULL,last_error_json=NULL WHERE id=? AND status='intent' "
                    "AND EXISTS (SELECT 1 FROM threads WHERE thread_id=? AND owner_json=? AND owner_generation=?)",
                    (now, group_id, thread_id, owner_json, thread["owner_generation"]))
                require(updated.rowcount == 1,
                        "Reply group or exact owner changed before the CMUX submit; wake held")
                db.execute("UPDATE delivery_group_attempts SET state='transport_started',transport_started_at=? "
                           "WHERE id=? AND state='intent'", (now, attempt_id))
                db.execute("UPDATE messages SET state='uncertain',wake_attempts=wake_attempts+1,last_wake_at=?,"
                           "submitted_at=NULL,last_error_json=NULL WHERE group_id=? AND state!='received'",
                           (now, group_id))

        try:
            cmux = self.cmux_factory()
            if readiness_policy == hf.NATIVE_PROMPT_READINESS:
                cmux.submit(owner, prompt, on_transport_start=mark_transport_start)
            else:
                cmux.submit(owner, prompt, on_transport_start=mark_transport_start,
                            readiness_policy=readiness_policy)
        except hf.DeliveryHoldError as error:
            with db:
                db.execute("UPDATE delivery_group_attempts SET state='held',last_error_json=? "
                           "WHERE id=? AND state='intent'", (json.dumps({"held": str(error)[:500]}), attempt_id))
                db.execute("UPDATE delivery_groups SET status='eligible',last_error_json=? "
                           "WHERE id=? AND status='intent'",
                           (json.dumps({"held": str(error)[:500]}), group_id))
            return {"group": group_id, "state": "held", "held": str(error)[:300],
                    "would_wake": False, "groups_formed": formed}
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            latest = db.execute("SELECT status FROM delivery_groups WHERE id=?", (group_id,)).fetchone()
            current_status = latest[0] if latest else "missing"
            with db:
                db.execute("UPDATE delivery_group_attempts SET state=?,last_error_json=? WHERE id=? "
                           "AND state IN ('intent','transport_started')",
                           ("uncertain" if current_status == "uncertain" else "held",
                            json.dumps({"uncertain" if current_status == "uncertain" else "held": str(error)[:500]}),
                            attempt_id))
                if current_status == "uncertain":
                    db.execute("UPDATE delivery_groups SET last_error_json=? WHERE id=? AND status='uncertain'",
                               (json.dumps({"uncertain": str(error)[:500]}), group_id))
                elif current_status == "intent":
                    db.execute("UPDATE delivery_groups SET status='eligible',last_error_json=? "
                               "WHERE id=? AND status='intent'",
                               (json.dumps({"held": str(error)[:500]}), group_id))
            return {"group": group_id, "state": current_status, "error": str(error)[:300],
                    "would_wake": False, "groups_formed": formed}
        with db:
            db.execute("UPDATE delivery_group_attempts SET state='confirmed',submitted_at=? "
                       "WHERE id=? AND state='transport_started'", (now, attempt_id))
            db.execute("UPDATE delivery_groups SET status='awaiting_receipt',submitted_at=?,last_wake_at=?,"
                       "last_error_json=NULL WHERE id=? AND status='uncertain' AND EXISTS ("
                       "SELECT 1 FROM threads WHERE thread_id=? AND owner_json=? AND owner_generation=?)",
                       (now, now, group_id, thread_id, owner_json, thread["owner_generation"]))
            db.execute("UPDATE messages SET state='awaiting_receipt',submitted_at=?,last_wake_at=?,last_error_json=NULL "
                       "WHERE group_id=? AND state='uncertain' AND EXISTS ("
                       "SELECT 1 FROM delivery_groups WHERE id=? AND status='awaiting_receipt')",
                       (now, now, group_id, group_id))
        final = db.execute("SELECT status FROM delivery_groups WHERE id=?", (group_id,)).fetchone()
        if final and final[0] == "received":
            return {"group": group_id, "state": "received", "submitted": True,
                    "received_during_submit": True, "groups_formed": formed}
        if final and final[0] == "uncertain":
            return {"group": group_id, "state": "uncertain", "submitted": True,
                    "owner_changed_during_submit": True, "groups_formed": formed}
        return {"group": group_id, "state": final[0] if final else "missing",
                "submitted": True, "groups_formed": formed}

    def scan(self, send=False, only_thread=None, origin="manual"):
        credentials, api = self.read_credentials()
        now = self.clock()
        report = {"at": now, "mode": "send" if send else "observe", "origin": origin, "threads": []}
        with contextlib.closing(self.database(create=False)) as db:
            self._refresh_dormancy(db, now)
            global_row = db.execute("SELECT value FROM meta WHERE key='global_not_before'").fetchone()
            self.global_not_before = float(global_row[0]) if global_row else 0.0
            self._prepare_api_limits(db, api, self.global_not_before)
            if only_thread is not None:
                thread_ids = [snowflake(only_thread, "thread ID")]
            else:
                thread_ids = [row[0] for row in db.execute(
                    "SELECT thread_id FROM threads WHERE subscription_state='active' OR EXISTS ("
                    "SELECT 1 FROM messages WHERE messages.thread_id=threads.thread_id "
                    "AND state IN ('pending','awaiting_receipt','uncertain','stalled')) "
                    "ORDER BY registered_at,thread_id")]
            send_scope = list(thread_ids) if send else []
            for thread_id in thread_ids:
                if self._custody_adoption_hold(db, thread_id):
                    report["threads"].append({"thread": thread_id,
                                               "status": "custody_adoption_hold",
                                               "poll_skipped": True, "wake_skipped": True})
                    continue
                try:
                    thread = self._thread(db, thread_id, require_open=False)
                except ValueError as error:
                    report["threads"].append({"thread": thread_id, "status": "error",
                                               "error": {"error": str(error)[:300]}})
                    continue
                manual_dormant_poll = only_thread is not None and origin == "manual"
                queue_count = db.execute(
                    "SELECT COUNT(*) FROM messages WHERE thread_id=? "
                    "AND state IN ('pending','awaiting_receipt','uncertain','stalled')",
                    (thread_id,)).fetchone()[0]
                dormant = thread["subscription_state"] == "dormant"
                if dormant and not manual_dormant_poll:
                    polled = {"thread": thread_id, "status": "dormant", "poll_skipped": True}
                else:
                    polled = self._poll_thread(db, thread, api, credentials, now)
                if polled.get("status") == "polled" and not polled.get("catchup_pending"):
                    self._mark_gateway_thread_reconciled(db, thread_id, thread["owner_generation"],
                                                         self.clock())
                row = {**polled}
                row["subscription_state"] = thread["subscription_state"]
                row["pending"] = db.execute(
                    "SELECT COUNT(*) FROM messages WHERE thread_id=? AND state='pending'",
                    (thread_id,)).fetchone()[0]
                should_wake = polled.get("status") == "polled" or (
                    dormant and not manual_dormant_poll and queue_count > 0)
                if send and should_wake:
                    try:
                        latest_thread = self._thread(db, thread_id, require_open=False)
                        row["wake"] = self._wake_one(db, latest_thread, now)
                    except (ValueError, OSError, subprocess.SubprocessError, sqlite3.Error) as error:
                        detail = str(error)[:500]
                        row["wake_error"] = detail
                        pending = db.execute("SELECT message_id FROM messages WHERE thread_id=? AND state='pending' "
                                             "ORDER BY length(message_id),message_id LIMIT 1", (thread_id,)).fetchone()
                        if pending:
                            with db:
                                db.execute("UPDATE messages SET last_error_json=? WHERE thread_id=? AND message_id=?",
                                           (json.dumps({"held": detail}), thread_id, pending[0]))
                report["threads"].append(row)
            api_success = any(item.get("status") == "polled" for item in report["threads"])
            self._record_scan_meta(db, report, api_success, send_scope)
        return report

    def _ensure_gateway_recovery_coverage(self, db, now):
        state = db.execute("SELECT recovery_epoch FROM gateway_state WHERE id=1").fetchone()
        epoch = state[0]
        if epoch <= 0:
            return epoch
        rows = db.execute("SELECT thread_id,owner_generation FROM threads").fetchall()
        for thread_id, generation in rows:
            db.execute("UPDATE gateway_recovery_work SET state='superseded',updated_at=? "
                       "WHERE epoch=? AND thread_id=? AND owner_generation!=? "
                       "AND state NOT IN ('complete','superseded')",
                       (now, epoch, thread_id, generation))
            inserted = db.execute(
                "INSERT OR IGNORE INTO gateway_recovery_work(epoch,thread_id,owner_generation,state,updated_at) "
                "VALUES(?,?,?,'queued',?)", (epoch, thread_id, generation, now)).rowcount
            if inserted:
                db.execute("UPDATE gateway_state SET recovery_state='pending',recovery_completed_at=NULL,"
                           "updated_at=? WHERE id=1", (now,))
        return epoch

    def _mark_gateway_thread_reconciled(self, db, thread_id, owner_generation, now):
        epoch = self._ensure_gateway_recovery_coverage(db, now)
        if epoch <= 0:
            return
        with db:
            current = db.execute("SELECT owner_generation FROM threads WHERE thread_id=?",
                                 (thread_id,)).fetchone()
            if current is not None and current[0] == owner_generation:
                db.execute("UPDATE gateway_recovery_work SET state='complete',updated_at=?,completed_at=?,"
                           "last_error_json=NULL WHERE epoch=? AND thread_id=? AND owner_generation=? "
                           "AND state NOT IN ('complete','superseded')",
                           (now, now, epoch, thread_id, owner_generation))
            self._refresh_gateway_recovery_state(db, epoch, now)

    @staticmethod
    def _refresh_gateway_recovery_state(db, epoch, now):
        counts = dict(db.execute(
            "SELECT w.state,COUNT(*) FROM gateway_recovery_work w JOIN threads t "
            "ON t.thread_id=w.thread_id AND t.owner_generation=w.owner_generation "
            "WHERE w.epoch=? GROUP BY w.state", (epoch,)))
        incomplete = sum(value for state, value in counts.items() if state not in ("complete", "superseded"))
        if incomplete == 0:
            recovery_state = "complete"
            completed_at = now
        elif counts.get("blocked", 0):
            recovery_state = "blocked"
            completed_at = None
        else:
            recovery_state = "pending"
            completed_at = None
        db.execute("UPDATE gateway_state SET recovery_state=?,recovery_completed_at=?,updated_at=? "
                   "WHERE id=1 AND recovery_epoch=?",
                   (recovery_state, completed_at, now, epoch))
        return recovery_state

    @staticmethod
    def _rest_condition(error):
        if not isinstance(error, dict):
            return "healthy"
        if error.get("state") in ("rate_limited", "backoff", "global_backoff"):
            return "rate_limited"
        status = error.get("http_status")
        if status == 404:
            return "remote_missing"
        if status == 403:
            return "inaccessible"
        if status == 429:
            return "rate_limited"
        if isinstance(status, int) and status >= 500:
            return "unavailable"
        if error.get("error"):
            return "unavailable"
        return "authentication_or_request_error"

    def _rest_recovery_turn(self, only_thread=None, stop_event=None):
        credentials, api = self.read_credentials()
        now = self.clock()
        results = []
        with contextlib.closing(self.database(create=False)) as db:
            self._refresh_dormancy(db, now)
            global_row = db.execute("SELECT value FROM meta WHERE key='global_not_before'").fetchone()
            self.global_not_before = float(global_row[0]) if global_row else 0.0
            self._prepare_api_limits(db, api, self.global_not_before)
            epoch = self._ensure_gateway_recovery_coverage(db, now)
            rotation_row = db.execute("SELECT value FROM meta WHERE key='rest_rotation_after'").fetchone()
            rotation_after = rotation_row[0] if rotation_row else None

            due = {}
            if epoch > 0:
                for thread_id, generation, state in db.execute(
                        "SELECT w.thread_id,w.owner_generation,w.state FROM gateway_recovery_work w "
                        "JOIN threads t ON t.thread_id=w.thread_id AND t.owner_generation=w.owner_generation "
                        "WHERE w.epoch=? "
                        "AND w.state NOT IN ('complete','superseded') AND t.next_poll_at<=? "
                        "ORDER BY t.registered_at,t.thread_id", (epoch, now)):
                    if only_thread is not None and thread_id != only_thread:
                        continue
                    due[thread_id] = {"recovery": True, "generation": generation, "state": state}
            routine_rows = db.execute(
                "SELECT thread_id,owner_generation FROM threads WHERE subscription_state='active' "
                "AND next_poll_at<=? AND (last_poll_at IS NULL OR last_poll_at<=?) "
                "ORDER BY registered_at,thread_id", (now, now - ROUTINE_REST_INTERVAL)).fetchall()
            for thread_id, generation in routine_rows:
                if only_thread is not None and thread_id != only_thread:
                    continue
                due.setdefault(thread_id, {"recovery": False, "generation": generation, "state": None})

            ordered = [row[0] for row in db.execute(
                "SELECT thread_id FROM threads ORDER BY registered_at,thread_id")]
            if rotation_after in ordered:
                split = ordered.index(rotation_after) + 1
                ordered = ordered[split:] + ordered[:split]
            ordered = [thread_id for thread_id in ordered if thread_id in due]
            selected = ordered[:WORKER_REST_THREADS_PER_TURN]
            with db:
                self._ensure_gateway_recovery_coverage(db, now)

            for thread_id in selected:
                if stop_event is not None and stop_event.is_set():
                    break
                thread = self._thread(db, thread_id, require_open=False)
                work = due[thread_id]
                current_generation = thread["owner_generation"]
                if work["recovery"]:
                    old_generation = work["generation"]
                    if old_generation != current_generation:
                        with db:
                            db.execute("UPDATE gateway_recovery_work SET state='superseded',updated_at=? "
                                       "WHERE epoch=? AND thread_id=? AND owner_generation=?",
                                       (now, epoch, thread_id, old_generation))
                            db.execute("INSERT OR IGNORE INTO gateway_recovery_work("
                                       "epoch,thread_id,owner_generation,state,updated_at) "
                                       "VALUES(?,?,?,'queued',?)",
                                       (epoch, thread_id, current_generation, now))
                        work["generation"] = current_generation
                if self.global_not_before > self.clock():
                    results.append({"thread": thread_id, "status": "global_backoff"})
                    continue
                with db:
                    if work["recovery"]:
                        db.execute("UPDATE gateway_recovery_work SET state='working',attempts=attempts+1,"
                                   "updated_at=?,last_error_json=NULL WHERE epoch=? AND thread_id=? "
                                   "AND owner_generation=? AND state NOT IN ('complete','superseded')",
                                   (now, epoch, thread_id, work["generation"]))
                polled = self._poll_thread(db, thread, api, credentials, self.clock(),
                                           max_pages=WORKER_REST_PAGES_PER_THREAD,
                                           routine=True, stop_event=stop_event)
                results.append(polled)
                latest = self._thread(db, thread_id, require_open=False)
                if work["recovery"]:
                    if latest["owner_generation"] != work["generation"]:
                        state = "superseded"
                        error_json = None
                        with db:
                            db.execute("INSERT OR IGNORE INTO gateway_recovery_work("
                                       "epoch,thread_id,owner_generation,state,updated_at) "
                                       "VALUES(?,?,?,'queued',?)",
                                       (epoch, thread_id, latest["owner_generation"], self.clock()))
                    elif polled.get("status") == "polled" and not polled.get("catchup_pending"):
                        state = "complete"
                        error_json = None
                    elif polled.get("status") in ("error", "rate_limited", "backoff", "global_backoff"):
                        state = "blocked"
                        error_json = json.dumps(polled.get("error") or {"state": polled.get("status")},
                                                separators=(",", ":"))
                    else:
                        state = "queued"
                        error_json = None
                    with db:
                        db.execute("UPDATE gateway_recovery_work SET state=?,updated_at=?,completed_at=?,"
                                   "last_error_json=? WHERE epoch=? AND thread_id=? AND owner_generation=? "
                                   "AND state!='superseded'",
                                   (state, self.clock(), self.clock() if state == "complete" else None,
                                    error_json, epoch, thread_id, work["generation"]))
                with db:
                    db.execute("INSERT INTO meta(key,value) VALUES('rest_rotation_after',?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (thread_id,))
            with db:
                recovery_state = self._refresh_gateway_recovery_state(db, epoch, self.clock()) if epoch > 0 else "idle"
                report = {"at": self.clock(), "mode": "recovery", "origin": "worker",
                          "threads": results, "recovery_epoch": epoch,
                          "recovery_state": recovery_state,
                          "request_budget": WORKER_REST_THREADS_PER_TURN *
                          (1 + WORKER_REST_PAGES_PER_THREAD)}
                api_success = any(row.get("status") == "polled" for row in results)
                values = {"last_rest_recovery_at": str(report["at"])}
                if api_success:
                    values["last_api_success_at"] = str(report["at"])
                for key, value in values.items():
                    db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        return report

    def _dispatch_turn(self, send=False, only_thread=None):
        now = self.clock()
        result_rows = []
        with contextlib.closing(self.database(create=False)) as db:
            self._refresh_dormancy(db, now)
            if only_thread is None:
                rows = db.execute(
                    "SELECT thread_id FROM threads WHERE EXISTS(SELECT 1 FROM messages "
                    "WHERE messages.thread_id=threads.thread_id AND state IN "
                    "('pending','awaiting_receipt','uncertain','stalled')) OR EXISTS("
                    "SELECT 1 FROM delivery_groups WHERE delivery_groups.thread_id=threads.thread_id "
                    "AND status!='received') ORDER BY registered_at,thread_id").fetchall()
                thread_ids = [row[0] for row in rows]
            else:
                thread_ids = [snowflake(only_thread, "thread ID")]
            send_scope = thread_ids if send else []
            rotation_row = db.execute("SELECT value FROM meta WHERE key='sender_rotation_after'").fetchone()
            rotation_after = rotation_row[0] if rotation_row else None
            if rotation_after in thread_ids and only_thread is None:
                split = thread_ids.index(rotation_after) + 1
                thread_ids = thread_ids[split:] + thread_ids[:split]
            for thread_id in thread_ids:
                if not send:
                    break
                if self._custody_adoption_hold(db, thread_id):
                    continue
                delay_row = db.execute("SELECT value FROM meta WHERE key=?",
                                       (f"sender_retry_after:{thread_id}",)).fetchone()
                if delay_row and float(delay_row[0]) > now:
                    continue
                try:
                    thread = self._thread(db, thread_id, require_open=False)
                    wake = self._wake_one(db, thread, now)
                    state = wake.get("state") if wake else None
                    with db:
                        db.execute("INSERT INTO meta(key,value) VALUES('sender_rotation_after',?) "
                                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (thread_id,))
                    if wake and state not in ("forming",):
                        with db:
                            db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                       (f"sender_retry_after:{thread_id}", str(now + POLL_INTERVAL)))
                    if wake and state not in ("forming",):
                        result_rows.append({"thread": thread_id, "wake": wake})
                    if wake and wake.get("submitted"):
                        break
                except (ValueError, OSError, subprocess.SubprocessError, sqlite3.Error) as error:
                    with db:
                        db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                   (f"sender_retry_after:{thread_id}", str(now + POLL_INTERVAL)))
                    result_rows.append({"thread": thread_id, "wake_error": str(error)[:300]})
                    with db:
                        db.execute("INSERT INTO meta(key,value) VALUES('sender_rotation_after',?) "
                                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (thread_id,))
            report = {"at": now, "mode": "send" if send else "observe", "origin": "worker",
                      "threads": result_rows, "send_scope": send_scope}
            self._record_scan_meta(db, report, False, send_scope)
        return report

    def _record_scan_meta(self, db, report, api_success, send_scope=None):
        at = report["at"]
        mode = report["mode"]
        origin = report.get("origin", "manual")
        send_scope = send_scope or []
        with db:
            values = {"last_scan_at": str(at), "last_scan_mode": mode,
                      "last_scan_origin": origin,
                      "last_send_scope": json.dumps(send_scope, separators=(",", ":"))}
            if api_success:
                values["last_api_success_at"] = str(at)
            if origin == "worker":
                values["last_worker_tick_at"] = (
                    json.dumps({"at": at, "generation": self.worker_generation}, separators=(",", ":"))
                    if self.worker_generation is not None else str(at))
            errors = [item for item in report["threads"] if item.get("status") == "error"]
            values["last_scan_errors"] = str(len(errors))
            for key, value in values.items():
                db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            if origin == "worker":
                db.execute("DELETE FROM meta WHERE key='last_worker_error'")

    def _record_worker_start(self, send, only_thread):
        generation = os.urandom(16).hex()
        worker_config = {"generation": generation, "send": bool(send),
                         "only_thread": None if only_thread is None else snowflake(only_thread, "thread ID")}
        with contextlib.closing(self.database(create=False)) as db:
            with db:
                db.execute("INSERT INTO meta(key,value) VALUES('worker_sender_config',?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (json.dumps(worker_config, separators=(",", ":")),))
                db.execute("DELETE FROM meta WHERE key='last_worker_tick_at'")
                db.execute("DELETE FROM meta WHERE key='last_worker_error'")
        self.worker_generation = generation

    def _record_worker_failure(self, send, only_thread, error):
        try:
            with contextlib.closing(self.database(create=False)) as db:
                now = self.clock()
                self._refresh_dormancy(db, now)
                scope = [snowflake(only_thread, "thread ID")] if only_thread else [
                    row[0] for row in db.execute(
                        "SELECT thread_id FROM threads WHERE subscription_state='active' OR EXISTS ("
                        "SELECT 1 FROM messages WHERE messages.thread_id=threads.thread_id "
                        "AND state IN ('pending','awaiting_receipt','uncertain','stalled'))")]
                report = {"at": now, "mode": "send" if send else "observe", "origin": "worker",
                          "threads": [{"thread": item, "status": "error"} for item in scope]}
                self._record_scan_meta(db, report, False, scope if send else [])
                with db:
                    db.execute("INSERT INTO meta(key,value) VALUES('last_worker_error',?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(error)[:500],))
        except (ValueError, OSError, sqlite3.Error):
            return

    def receive(self, thread_id, message_id=None, output=None, session_hint=None, *, group_id=None):
        thread_id = snowflake(thread_id, "thread ID")
        current = self._identity_owner(session_hint=session_hint)
        output = output if output is not None else sys.stdout
        require(message_id is None or group_id is None,
                "Select either an exact message or a delivery group, not both")
        with self.locked(timeout=LOCK_TIMEOUT, name="receive.lock"):
            with contextlib.closing(self.database(create=False)) as db:
                require(not self._custody_adoption_hold(db, thread_id),
                        "Thread is held by an explicit custody adoption")
                with db:
                    self._reconcile_group_receipts(db, thread_id)
                thread = self._thread(db, thread_id, require_open=False)
                require(thread["owner"] == current,
                        "Only the registered current owner can receive this thread's replies")
                if message_id is None and group_id is None:
                    older_group = db.execute(
                        "SELECT 1 FROM delivery_groups WHERE thread_id=? AND status!='received' LIMIT 1",
                        (thread_id,)).fetchone()
                    if older_group is None:
                        formed = self._seal_delivery_groups(db, thread, self.clock(), force=True, max_groups=1)
                        held = next((item for item in formed if item.get("state") == "held"), None)
                        if held:
                            pending_groupable = db.execute(
                                "SELECT 1 FROM messages WHERE thread_id=? AND groupable=1 "
                                "AND group_id IS NULL AND state='pending' AND owner_generation=? LIMIT 1",
                                (thread_id, thread["owner_generation"])).fetchone()
                            if pending_groupable:
                                raise ValueError("Reply remains held: " + held["reason"])
                selected_group = None
                if group_id is not None:
                    require(isinstance(group_id, (str, int)) and not isinstance(group_id, bool) and
                            re.fullmatch(r"[1-9][0-9]{0,18}", str(group_id)) is not None,
                            "Invalid delivery group ID")
                    group_id = int(group_id)
                    selected_group = db.execute(
                        "SELECT id,owner_generation,owner_json,status,legacy FROM delivery_groups "
                        "WHERE id=? AND thread_id=?", (group_id, thread_id)).fetchone()
                    require(selected_group is not None and selected_group[3] != "received",
                            "Delivery group is missing or already received")
                    group_owner = json.loads(selected_group[2])
                    require(self._generation_chain_valid(db, thread, selected_group[1], group_owner),
                            "Delivery group owner generation has no authorized transfer chain")
                    require(not self._catchup_incomplete(thread) or selected_group[4],
                            "REST catch-up is incomplete; this new group remains held")
                    rows = db.execute(
                        "SELECT m.message_id,m.author_name,m.body,m.attachments_json,m.state "
                        "FROM delivery_group_members gm JOIN messages m "
                        "ON m.thread_id=gm.thread_id AND m.message_id=gm.message_id "
                        "WHERE gm.group_id=? AND m.state!='received' ORDER BY gm.ordinal",
                        (group_id,)).fetchall()
                elif message_id is not None:
                    message_id = snowflake(message_id, "message ID")
                    rows = db.execute(
                        "SELECT message_id,author_name,body,attachments_json,state FROM messages "
                        "WHERE thread_id=? AND message_id=? AND state!='received'",
                        (thread_id, message_id)).fetchall()
                    if rows:
                        membership = db.execute(
                            "SELECT g.id,g.owner_generation,g.owner_json,g.status,g.legacy FROM messages m "
                            "JOIN delivery_groups g ON g.id=m.group_id "
                            "WHERE m.thread_id=? AND m.message_id=?", (thread_id, message_id)).fetchone()
                        if membership:
                            selected_group = membership
                            require(self._generation_chain_valid(db, thread, membership[1],
                                                                json.loads(membership[2])),
                                    "Delivery group owner generation has no authorized transfer chain")
                            require(not self._catchup_incomplete(thread) or membership[4],
                                    "REST catch-up is incomplete; this new group remains held")
                        else:
                            message_generation = db.execute(
                                "SELECT owner_generation FROM messages WHERE thread_id=? AND message_id=?",
                                (thread_id, message_id)).fetchone()[0]
                            require(message_generation == thread["owner_generation"] and
                                    self._generation_chain_valid(db, thread, message_generation, current),
                                    "Reply owner generation has no authorized transfer chain")
                            require(not self._catchup_incomplete(thread),
                                    "REST catch-up is incomplete; this reply remains held")
                            require(self._gateway_recovery_hold_reason(db, thread) is None,
                                    "Gateway recovery is incomplete; this reply remains held")
                else:
                    selected_group = db.execute(
                        "SELECT id,owner_generation,owner_json,status,legacy FROM delivery_groups "
                        "WHERE thread_id=? AND status!='received' ORDER BY id LIMIT 1",
                        (thread_id,)).fetchone()
                    if selected_group:
                        require(self._generation_chain_valid(db, thread, selected_group[1],
                                                             json.loads(selected_group[2])),
                                "Delivery group owner generation has no authorized transfer chain")
                        recovery_hold = self._gateway_recovery_hold_reason(db, thread)
                        require(not recovery_hold or selected_group[4],
                                "Reply remains held: " + str(recovery_hold))
                        require(not self._catchup_incomplete(thread) or selected_group[4],
                                "REST catch-up is incomplete; this new group remains held")
                        rows = db.execute(
                            "SELECT m.message_id,m.author_name,m.body,m.attachments_json,m.state "
                            "FROM delivery_group_members gm JOIN messages m "
                            "ON m.thread_id=gm.thread_id AND m.message_id=gm.message_id "
                            "WHERE gm.group_id=? AND m.state!='received' ORDER BY gm.ordinal",
                            (selected_group[0],)).fetchall()
                    else:
                        rows = db.execute(
                            "SELECT message_id,author_name,body,attachments_json,state FROM messages "
                            "WHERE thread_id=? AND group_id IS NULL AND groupable=0 AND state!='received' "
                            "ORDER BY length(message_id),message_id LIMIT ?",
                            (thread_id, MAX_RECEIVE_MESSAGES)).fetchall()
                if message_id is not None:
                    require(len(rows) == 1, "Reply is missing or already received")
                if not rows:
                    return {"thread": thread_id, "group": selected_group[0] if selected_group else None,
                            "received": [], "pending": False}
                rendered = self._render_messages(thread_id, [row[:4] for row in rows])
                payload = rendered.encode("utf-8")
                require(len(payload) <= MAX_RECEIVE_BYTES,
                        "Reply output exceeds the bounded output size; receive one message by exact ID")
                written = output.write(rendered)
                require(written == len(rendered),
                        "Reply output was incomplete; no replies were acknowledged")
                output.flush()
                latest = self._identity_owner(session_hint=session_hint)
                require(latest == current, "Owner changed during reply output; no replies were acknowledged")
                now = self.clock()
                owner_json = json.dumps(current, separators=(",", ":"))
                with db:
                    latest_thread = self._thread(db, thread_id, require_open=False)
                    require(latest_thread["owner"] == current,
                            "Owner changed during reply output; no replies were acknowledged")
                    if selected_group:
                        current_group = db.execute(
                            "SELECT owner_generation,owner_json,status,legacy FROM delivery_groups "
                            "WHERE id=? AND thread_id=?", (selected_group[0], thread_id)).fetchone()
                        require(current_group is not None and self._generation_chain_valid(
                            db, latest_thread, current_group[0], json.loads(current_group[1])),
                            "Delivery group owner changed during output; no replies were acknowledged")
                        require(not self._catchup_incomplete(latest_thread) or current_group[3],
                                "REST catch-up began during output; no replies were acknowledged")
                    else:
                        require(not self._catchup_incomplete(latest_thread),
                                "REST catch-up began during output; no replies were acknowledged")
                    for row in rows:
                        updated = db.execute(
                            "UPDATE messages SET state='received',received_at=?,last_error_json=NULL "
                            "WHERE thread_id=? AND message_id=? AND state!='received' AND EXISTS ("
                            "SELECT 1 FROM threads WHERE thread_id=? AND owner_json=?)",
                            (now, thread_id, row[0], thread_id, owner_json))
                        require(updated.rowcount == 1,
                                "Reply or ownership changed during output; no replies were acknowledged")
                    if selected_group:
                        db.execute("UPDATE delivery_groups SET status='received' WHERE id=? AND status!='received' "
                                   "AND NOT EXISTS (SELECT 1 FROM delivery_group_members gm JOIN messages m "
                                   "ON m.thread_id=gm.thread_id AND m.message_id=gm.message_id "
                                   "WHERE gm.group_id=? AND m.state!='received')",
                                   (selected_group[0], selected_group[0]))
        return {"thread": thread_id, "group": selected_group[0] if selected_group else None,
                "received": [row[0] for row in rows],
                "delivery_boundary": "stdout_flushed", "comprehension_confirmed": False}

    def _lock_is_held(self, name):
        path = self.directory / name
        if path.is_symlink() or not path.is_file():
            return False
        try:
            with path.open("r") as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(stream, fcntl.LOCK_UN)
        except OSError:
            return False
        return False

    def _health(self, db, thread_id=None):
        keys = ("last_scan_at", "last_scan_mode", "last_scan_origin", "last_scan_errors",
                "last_api_success_at", "last_worker_tick_at", "last_send_scope",
                "last_worker_error", "worker_sender_config")
        meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN (" +
                               ",".join("?" for _ in keys) + ")", keys))
        now = self.clock()
        def age_of(key):
            try:
                return max(0, now - float(meta[key])) if meta.get(key) else None
            except (ValueError, TypeError):
                return None
        worker_age = None
        worker_tick_generation = None
        try:
            worker_tick = json.loads(meta.get("last_worker_tick_at", "null"))
            if isinstance(worker_tick, dict):
                tick_at = worker_tick.get("at")
                generation = worker_tick.get("generation")
                if (isinstance(tick_at, (int, float)) and not isinstance(tick_at, bool) and
                        math.isfinite(tick_at) and isinstance(generation, str) and
                        len(generation) == 32 and
                        all(char in "0123456789abcdef" for char in generation)):
                    worker_age = max(0, now - tick_at)
                    worker_tick_generation = generation
            elif isinstance(worker_tick, (int, float)) and not isinstance(worker_tick, bool) and math.isfinite(worker_tick):
                worker_age = max(0, now - worker_tick)
        except (TypeError, ValueError):
            pass
        api_age = age_of("last_api_success_at")
        heartbeat_fresh = worker_age is not None and worker_age <= max(60, POLL_INTERVAL * 5)
        api_fresh = api_age is not None and api_age <= max(60, POLL_INTERVAL * 5)
        worker_running = self._lock_is_held("worker.lock")
        supervisor_running = self._lock_is_held("supervise.lock")
        gateway_row = db.execute(
            "SELECT condition,recovery_epoch,recovery_state,recovery_reason,last_event_at,last_error_json "
            "FROM gateway_state WHERE id=1").fetchone()
        send_scope = []
        try:
            parsed_send_scope = json.loads(meta.get("last_send_scope", "[]"))
            if isinstance(parsed_send_scope, list) and all(isinstance(item, str) for item in parsed_send_scope):
                send_scope = parsed_send_scope
        except ValueError:
            pass
        in_scope = thread_id in send_scope if thread_id is not None else bool(send_scope)
        worker_config = None
        try:
            parsed_config = json.loads(meta.get("worker_sender_config", ""))
            if (isinstance(parsed_config, dict) and
                    isinstance(parsed_config.get("generation"), str) and
                    len(parsed_config["generation"]) == 32 and
                    all(char in "0123456789abcdef" for char in parsed_config["generation"]) and
                    isinstance(parsed_config.get("send"), bool) and
                    (parsed_config.get("only_thread") is None or
                     isinstance(parsed_config.get("only_thread"), str))):
                only_thread = parsed_config["only_thread"]
                if only_thread is not None:
                    only_thread = snowflake(only_thread, "thread ID")
                worker_config = {"generation": parsed_config["generation"],
                                 "send": parsed_config["send"], "only_thread": only_thread}
        except (TypeError, ValueError):
            pass
        worker_configuration_matches_heartbeat = (
            worker_config is not None and
            worker_tick_generation == worker_config["generation"])
        send_enabled = worker_config["send"] if worker_config is not None else None
        configured_scope_covers_thread = None
        if worker_config is not None:
            configured_scope_covers_thread = bool(
                worker_config["send"] and
                (thread_id is None or worker_config["only_thread"] is None or
                 worker_config["only_thread"] == thread_id))
        worker_send_scope = None
        if worker_config is not None:
            worker_send_scope = ({"type": "all_threads"} if worker_config["only_thread"] is None else
                                 {"type": "thread", "thread_id": worker_config["only_thread"]})
        thread_error, thread_poll_age = None, None
        if thread_id is not None:
            row = db.execute("SELECT last_error_json,last_poll_at FROM threads WHERE thread_id=?",
                             (thread_id,)).fetchone()
            if row:
                thread_error = json.loads(row[0]) if row[0] else None
                try:
                    thread_poll_age = max(0, now - row[1]) if row[1] is not None else None
                except (TypeError, ValueError):
                    thread_poll_age = None
        thread_api_fresh = thread_id is None or (
            thread_poll_age is not None and thread_poll_age <= max(60, POLL_INTERVAL * 5))
        ready = (worker_running and heartbeat_fresh and api_fresh and send_enabled is True and
                 worker_configuration_matches_heartbeat and configured_scope_covers_thread is True and
                 thread_api_fresh)
        warnings = []
        if not worker_running:
            warnings.append("No Discord Watch worker currently holds the worker lock")
        if worker_running and not heartbeat_fresh:
            warnings.append("Worker heartbeat is missing or stale")
        if not api_fresh:
            warnings.append("No successful Discord API poll is fresh")
        if worker_config is None:
            warnings.append("Worker sender configuration is missing or invalid")
        elif not worker_configuration_matches_heartbeat:
            warnings.append("Worker sender configuration does not match its latest heartbeat")
        if send_enabled is False:
            warnings.append("The configured worker is observe-only")
        if thread_id is not None and worker_config is not None and send_enabled and not configured_scope_covers_thread:
            warnings.append("This thread is outside the worker's configured send scope")
        if thread_id is not None and not thread_api_fresh:
            warnings.append("This thread has no fresh successful API poll")
        if thread_error:
            warnings.append("This thread has a recorded Discord API or polling error")
        state = "ready_for_wakes" if ready and thread_error is None else (
            "worker_running" if worker_running else "worker_stopped")
        return {"state": state, "ready_for_wakes": ready and thread_error is None,
                "worker_running": worker_running, "supervisor_running": supervisor_running,
                "gateway": {"condition": gateway_row[0], "recovery_epoch": gateway_row[1],
                            "recovery_state": gateway_row[2], "recovery_reason": gateway_row[3],
                            "last_event_at": gateway_row[4],
                            "last_error": json.loads(gateway_row[5]) if gateway_row[5] else None},
                "worker_heartbeat_age_seconds": worker_age,
                "worker_heartbeat_fresh": heartbeat_fresh,
                "api_last_success_age_seconds": api_age, "api_success_fresh": api_fresh,
                "last_scan_at": float(meta["last_scan_at"]) if meta.get("last_scan_at") else None,
                "last_scan_origin": meta.get("last_scan_origin"),
                "last_scan_mode": meta.get("last_scan_mode"),
                "last_scan_errors": int(meta.get("last_scan_errors", "0")),
                "last_worker_error": meta.get("last_worker_error"),
                "worker_send_enabled": send_enabled,
                "worker_send_scope": worker_send_scope,
                "worker_configuration_matches_heartbeat": worker_configuration_matches_heartbeat,
                "thread_covered_by_worker_scope": (configured_scope_covers_thread
                                                    if thread_id is not None else None),
                "last_send_scope": send_scope,
                "thread_in_send_scope": in_scope if thread_id is not None else None,
                "thread_last_poll_age_seconds": thread_poll_age,
                "thread_error": thread_error, "warnings": warnings,
                "note": "A successful API poll and confirmed CMUX submit or OpenCode user-message admission are distinct from explicit reply receipt."}

    def status(self):
        with contextlib.closing(self.database(create=False)) as db:
            gateway_row = db.execute(
                "SELECT condition,sequence,session_id,recovery_epoch,recovery_state,recovery_reason,"
                "recovery_started_at,recovery_completed_at,last_event_at,last_error_json,"
                "session_start_total,session_start_remaining,session_start_reset_at "
                "FROM gateway_state WHERE id=1").fetchone()
            gateway_epoch = gateway_row[3]
            coverage_counts = dict(db.execute(
                "SELECT w.state,COUNT(*) FROM gateway_recovery_work w JOIN threads t "
                "ON t.thread_id=w.thread_id AND t.owner_generation=w.owner_generation "
                "WHERE w.epoch=? GROUP BY w.state", (gateway_epoch,))) if gateway_epoch else {}
            gateway = {
                "condition": gateway_row[0],
                "has_resumable_session": bool(gateway_row[2]),
                "last_committed_sequence": int(gateway_row[1]) if gateway_row[1] is not None else None,
                "recovery": {"epoch": gateway_epoch, "state": gateway_row[4],
                             "reason": gateway_row[5], "started_at": gateway_row[6],
                             "completed_at": gateway_row[7],
                             "threads_total": sum(coverage_counts.values()),
                             "threads_complete": coverage_counts.get("complete", 0),
                             "threads_pending": coverage_counts.get("queued", 0) +
                             coverage_counts.get("working", 0),
                             "threads_blocked": coverage_counts.get("blocked", 0)},
                "session_start_limit": {"total": gateway_row[10], "remaining": gateway_row[11],
                                        "reset_at": gateway_row[12]},
                "last_event_at": gateway_row[8],
                "last_error": json.loads(gateway_row[9]) if gateway_row[9] else None,
                "coordinator_guidance": (
                    "Inspect the reported Gateway fatal condition and correct that specific cause in the existing component configuration."
                    if gateway_row[0] == "fatal_configuration" else (
                        "Restart serve without --rest-only after Gateway configuration is available."
                        if gateway_row[0] == "offline_rest" else None))
            }
            threads = []
            for row in db.execute("SELECT thread_id,owner_json,cursor,is_open,registered_at,last_poll_at,next_poll_at,"
                                  "last_error_json,owner_generation,subscription_state,last_activity_at,remote_closed,"
                                  "initial_cutoff,catchup_before,catchup_max "
                                  "FROM threads ORDER BY registered_at,thread_id"):
                (thread_id, owner_json, cursor, is_open, registered_at, last_poll_at, next_poll_at,
                 last_error, generation, subscription_state, last_activity_at, remote_closed, initial_cutoff,
                 catchup_before, catchup_max) = row
                status_thread = {"thread_id": thread_id, "owner": json.loads(owner_json),
                                 "owner_generation": generation, "catchup_before": catchup_before,
                                 "catchup_max": catchup_max}
                catchup_incomplete = self._catchup_incomplete(status_thread)
                recovery_work = db.execute(
                    "SELECT state,owner_generation,updated_at,completed_at,last_error_json "
                    "FROM gateway_recovery_work WHERE epoch=? AND thread_id=? AND owner_generation=?",
                    (gateway_epoch, thread_id, generation)).fetchone()
                rest_recovery = None
                if recovery_work:
                    rest_recovery = {"state": recovery_work[0],
                                     "condition": self._rest_condition(
                                         json.loads(recovery_work[4]) if recovery_work[4] else None),
                                     "covered_generation": recovery_work[1],
                                     "updated_at": recovery_work[2],
                                     "completed_at": recovery_work[3],
                                     "last_error": json.loads(recovery_work[4]) if recovery_work[4] else None}
                recovery_order_hold = self._gateway_recovery_hold_reason(db, status_thread)
                if rest_recovery is not None:
                    rest_recovery["delivery_order_hold"] = recovery_order_hold
                counts = {state: count for state, count in db.execute(
                    "SELECT state,COUNT(*) FROM messages WHERE thread_id=? GROUP BY state", (thread_id,))}
                recent = [{"message": message_id, "state": state, "wake_attempts": attempts,
                           "submitted_at": submitted_at, "received_at": received_at,
                           "last_error": json.loads(message_error) if message_error else None}
                          for message_id, state, attempts, submitted_at, received_at, message_error in db.execute(
                              "SELECT message_id,state,wake_attempts,submitted_at,received_at,last_error_json "
                              "FROM messages WHERE thread_id=? ORDER BY length(message_id) DESC,message_id DESC LIMIT 20",
                              (thread_id,))]
                delivery_groups = []
                for group in db.execute(
                        "SELECT id,owner_generation,owner_json,status,created_at,first_message_id,last_message_id,"
                        "attempt_count,last_wake_at,submitted_at,last_error_json,legacy "
                        "FROM delivery_groups WHERE thread_id=? ORDER BY id", (thread_id,)):
                    (group_id, group_generation, group_owner_json, group_state, group_created_at,
                     first_message_id, last_message_id, attempts, last_wake_at, submitted_at,
                     group_error, legacy) = group
                    group_owner = json.loads(group_owner_json)
                    if not self._generation_chain_valid(db, status_thread, group_generation, group_owner):
                        visible_state = "held"
                        hold_reason = "owner_generation_event_chain_invalid"
                    elif catchup_incomplete and not legacy and group_state != "received":
                        visible_state = "held"
                        hold_reason = "rest_catchup_incomplete"
                    elif (recovery_order_hold and not legacy and
                          group_state in ("eligible", "intent")):
                        visible_state = "held"
                        hold_reason = recovery_order_hold
                    elif group_state in ("eligible", "intent") and group_error:
                        error_record = json.loads(group_error)
                        if isinstance(error_record, dict) and "held" in error_record:
                            visible_state = "held"
                            hold_reason = "guard_or_owner_hold"
                        else:
                            visible_state = group_state
                            hold_reason = None
                    else:
                        visible_state = group_state
                        hold_reason = None
                    members = [{"message": message_id, "state": state, "received_at": received_at}
                               for message_id, state, received_at in db.execute(
                                   "SELECT m.message_id,m.state,m.received_at FROM delivery_group_members gm "
                                   "JOIN messages m ON m.thread_id=gm.thread_id AND m.message_id=gm.message_id "
                                   "WHERE gm.group_id=? ORDER BY gm.ordinal", (group_id,))]
                    group_attempts = [{"attempt": attempt_id, "state": attempt_state,
                                       "created_at": created_at,
                                       "transport_started_at": transport_started_at,
                                       "submitted_at": attempt_submitted_at,
                                       "target_owner": json.loads(target_owner_json) if target_owner_json else None,
                                       "target_owner_generation": target_owner_generation,
                                       "last_error": json.loads(attempt_error) if attempt_error else None}
                                      for attempt_id, attempt_state, created_at, transport_started_at,
                                      attempt_submitted_at, target_owner_json, target_owner_generation,
                                      attempt_error in db.execute(
                                          "SELECT id,state,created_at,transport_started_at,submitted_at,"
                                          "target_owner_json,target_owner_generation,last_error_json "
                                          "FROM delivery_group_attempts WHERE group_id=? ORDER BY id", (group_id,))]
                    delivery_groups.append({"group": group_id, "owner_generation": group_generation,
                                            "state": visible_state, "stored_state": group_state,
                                            "created_at": group_created_at,
                                            "first_message": first_message_id, "last_message": last_message_id,
                                            "attempt_count": attempts, "last_wake_at": last_wake_at,
                                            "submitted_at": submitted_at,
                                            "last_error": json.loads(group_error) if group_error else None,
                                            "legacy": bool(legacy), "hold_reason": hold_reason,
                                            "members": members, "attempts": group_attempts})
                forming_rows = db.execute(
                    "SELECT message_id,author_name,body,attachments_json,ingested_at FROM messages "
                    "WHERE thread_id=? AND groupable=1 "
                    "AND group_id IS NULL AND state='pending' AND owner_generation=? "
                    "ORDER BY length(message_id),message_id", (thread_id, generation)).fetchall()
                forming = None
                if forming_rows:
                    prefix = []
                    for item in forming_rows[:MAX_RECEIVE_MESSAGES]:
                        if len(self._render_messages(thread_id, [row[:4] for row in prefix + [item]])
                                .encode("utf-8")) > MAX_RECEIVE_BYTES:
                            break
                        prefix.append(item)
                    first_at = min(item[4] for item in prefix)
                    last_at = max(item[4] for item in prefix)
                    eligible_at = min(first_at + GROUP_MAX_DELAY, last_at + GROUP_QUIET_SECONDS)
                    generation_valid = self._generation_chain_valid(db, status_thread, generation,
                                                                    status_thread["owner"])
                    group_state = "held" if catchup_incomplete or recovery_order_hold or not generation_valid else (
                        "eligible" if self.clock() >= eligible_at else "forming")
                    forming_hold_reason = None
                    if catchup_incomplete:
                        forming_hold_reason = "rest_catchup_incomplete"
                    elif recovery_order_hold:
                        forming_hold_reason = recovery_order_hold
                    elif not generation_valid:
                        forming_hold_reason = "owner_generation_event_chain_invalid"
                    forming = {"state": group_state, "messages": [item[0] for item in forming_rows],
                               "next_group_messages": [item[0] for item in prefix],
                               "first_at": first_at, "last_at": last_at, "eligible_at": eligible_at,
                               "hold_reason": forming_hold_reason}
                thread_error_record = json.loads(last_error) if last_error else None
                recovery_error_record = json.loads(recovery_work[4]) if recovery_work and recovery_work[4] else None
                rest_condition = self._rest_condition(thread_error_record or recovery_error_record)
                exact_owner_hold = any(group["hold_reason"] in
                                       ("owner_generation_event_chain_invalid", "guard_or_owner_hold")
                                       for group in delivery_groups)
                exact_owner_hold = exact_owner_hold or bool(
                    forming and forming["hold_reason"] == "owner_generation_event_chain_invalid")
                if exact_owner_hold:
                    coordinator_guidance = (
                        "Restore the exact enrolled owner binding or arrange an explicit authorized transfer.")
                elif rest_condition == "inaccessible":
                    coordinator_guidance = "Restore the configured bot's access to this retained Discord thread."
                elif rest_condition == "remote_missing":
                    coordinator_guidance = "Restore access to the retained Discord thread before reconciliation can continue."
                elif rest_condition == "rate_limited":
                    coordinator_guidance = "Wait until the reported Discord route or global retry deadline."
                elif rest_condition == "unavailable":
                    coordinator_guidance = "Wait for the Discord REST route to recover; the thread remains retained."
                else:
                    coordinator_guidance = None
                threads.append({"thread": thread_id, "owner": json.loads(owner_json), "cursor": cursor,
                                "open": bool(is_open), "registered_at": registered_at,
                                "subscription_state": subscription_state,
                                "last_activity_at": last_activity_at,
                                "remote_closed": bool(remote_closed),
                                "initial_cutoff": initial_cutoff,
                                "last_poll_at": last_poll_at, "next_poll_at": next_poll_at,
                                "last_error": thread_error_record,
                                "rest_condition": rest_condition,
                                "coordinator_guidance": coordinator_guidance,
                                "owner_generation": generation, "queue": counts, "recent": recent,
                                "catchup_before": catchup_before, "catchup_max": catchup_max,
                                "rest_recovery": rest_recovery,
                                "forming_group": forming,
                                "delivery_groups": delivery_groups})
            return {"health": self._health(db), "gateway": gateway, "threads": threads}

    def doctor(self):
        result = {"state_directory": str(self.directory), "state_initialized": False,
                  "notify_config": "unavailable", "hailing_config": "unavailable",
                  "cmux": "not_checked", "checks": []}
        try:
            with contextlib.closing(self.database(create=False)) as db:
                result["state_initialized"] = True
                result["health"] = self._health(db)
                result["registered_threads"] = db.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        except (ValueError, OSError, sqlite3.Error) as error:
            result["checks"].append({"name": "state", "ok": False, "detail": str(error)[:300]})
        try:
            values = read_notify_credentials(self.notify_home)
            result["notify_config"] = "available"
            result["discord_user_configured"] = True
            result["checks"].append({"name": "credentials", "ok": True,
                                     "detail": "Bot token and configured Discord user ID are available; values are not printed"})
            del values
        except (ValueError, OSError) as error:
            result["checks"].append({"name": "credentials", "ok": False, "detail": str(error)[:300]})
        try:
            self.hailing_config()
            result["hailing_config"] = "available"
            result["checks"].append({"name": "hailing", "ok": True, "detail": "Hailing Frequencies config is readable"})
        except (ValueError, OSError) as error:
            result["checks"].append({"name": "hailing", "ok": False, "detail": str(error)[:300]})
        return result

    def serve(self, send=False, only_thread=None, rest_only=False, *, gateway_factory=None, stop_event=None):
        with contextlib.closing(self.database(create=False)):
            pass
        lock_path = self.directory / "worker.lock"
        require(not lock_path.is_symlink(), "Refusing symlink worker lock")
        with lock_path.open("a") as lock:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("A Discord Watch worker already owns this state") from None
            stopping = False
            stop_event = stop_event or threading.Event()
            self._record_worker_start(send, only_thread)
            original_handlers = {}
            def stop(signum, frame):
                nonlocal stopping
                stopping = True
                stop_event.set()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    original_handlers[sig] = signal.signal(sig, stop)
                except ValueError:
                    break
            gateway = None
            if rest_only:
                self.set_gateway_condition("offline_rest", "Gateway disabled by explicit REST-only mode")
            else:
                from discord_gateway import GatewayClient
                factory = gateway_factory or self.gateway_factory or GatewayClient
                gateway = factory(self)
            lanes = []
            if gateway is not None:
                lanes.append(threading.Thread(target=gateway.run, args=(stop_event,),
                                              name="discord-watch-gateway", daemon=True))

            def rest_loop():
                while not stop_event.is_set():
                    try:
                        self._rest_recovery_turn(only_thread, stop_event)
                    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
                        self._record_worker_failure(send, only_thread, error)
                    stop_event.wait(REST_TURN_INTERVAL)

            lanes.append(threading.Thread(target=rest_loop, name="discord-watch-rest", daemon=True))
            for lane in lanes:
                lane.start()
            last_report = 0.0
            try:
                while not stopping:
                    if stop_event.is_set():
                        stopping = True
                        break
                    try:
                        report = self._dispatch_turn(send=send, only_thread=only_thread)
                    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
                        report = {"at": self.clock(), "mode": "send" if send else "observe",
                                  "worker_error": str(error)[:500]}
                        self._record_worker_failure(send, only_thread, error)
                    now = time.monotonic()
                    if report.get("threads") or now - last_report >= POLL_INTERVAL:
                        print(json.dumps(report, ensure_ascii=False), flush=True)
                        last_report = now
                    stop_event.wait(SENDER_INTERVAL)
            finally:
                stop_event.set()
                if gateway is not None:
                    gateway.stop()
                for lane in lanes:
                    lane.join(timeout=LANE_JOIN_TIMEOUT)
                for sig, handler in original_handlers.items():
                    signal.signal(sig, handler)
                fcntl.flock(lock, fcntl.LOCK_UN)
        return {"stopped": True}

    def supervise(self, send=False, only_thread=None, rest_only=False):
        with contextlib.closing(self.database(create=False)):
            pass
        lock_path = self.directory / "supervise.lock"
        require(not lock_path.is_symlink(), "Refusing symlink supervisor lock")
        with lock_path.open("a") as lock:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("A Discord Watch supervisor already owns this state") from None
            stopping = False
            child = None
            original_handlers = {}
            def stop(signum, frame):
                nonlocal stopping
                stopping = True
                if child is not None and child.poll() is None:
                    child.terminate()
            for sig in (signal.SIGINT, signal.SIGTERM):
                original_handlers[sig] = signal.signal(sig, stop)
            delay = 1
            try:
                while not stopping:
                    command = [sys.executable, str(Path(__file__).resolve()), "--state-dir", str(self.directory),
                               "--notify-home", str(self.notify_home), "--hailing-state", str(self.hailing_state),
                               "--cmux", self.cmux_executable, "serve"]
                    if send:
                        command.append("--send")
                    if only_thread:
                        command.extend(("--only-thread", only_thread))
                    if rest_only:
                        command.append("--rest-only")
                    child = subprocess.Popen(command)
                    code = child.wait()
                    if stopping:
                        break
                    print(json.dumps({"supervisor": "worker_exited", "exit_code": code,
                                      "restart_after_seconds": delay}), flush=True)
                    deadline = time.monotonic() + delay
                    while not stopping and time.monotonic() < deadline:
                        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                    delay = min(30, delay * 2)
            finally:
                if child is not None and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=CMUX_TIMEOUT + 2)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                for sig, handler in original_handlers.items():
                    signal.signal(sig, handler)
                fcntl.flock(lock, fcntl.LOCK_UN)
        return {"stopped": True}


def _read_starfleet_bytes(path, maximum=STARFLEET_DIAGNOSTIC_LIMIT):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("diagnostic file is unavailable") from None
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode), "diagnostic file is not regular")
        require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0,
                "diagnostic file has unsafe ownership or permissions")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            value = stream.read(maximum + 1)
        require(len(value) <= maximum, "diagnostic file exceeds its size limit")
        return value
    except OSError:
        raise ValueError("diagnostic file is unreadable") from None
    finally:
        os.close(descriptor)


def _read_starfleet_json(path, maximum=STARFLEET_DIAGNOSTIC_LIMIT):
    raw = _read_starfleet_bytes(path, maximum)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise ValueError("diagnostic JSON is malformed") from None
    require(isinstance(value, dict), "diagnostic JSON must be an object")
    return value


def read_starfleet_caller(stream):
    raw = stream.read(16 * 1024 + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes) or len(raw) > 16 * 1024:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    if (not isinstance(value, dict) or set(value) != {"schema_version", "caller"}
            or value.get("schema_version") != 1 or not isinstance(value.get("caller"), dict)
            or set(value["caller"]) != {"harness", "session", "workspace", "surface"}):
        return None
    caller = value["caller"]
    if (caller.get("harness") not in ALLOWED_LAUNCH_KINDS
            or any(not isinstance(caller.get(key), str) or not caller[key]
                   for key in ("session", "workspace", "surface"))):
        return None
    return caller


def _diagnostic_owner_matches(owner, caller):
    if not isinstance(owner, dict) or caller is None:
        return False
    try:
        return (owner.get("harness") == caller["harness"]
                and hf.session_identity(owner.get("session"), caller["harness"])
                == hf.session_identity(caller["session"], caller["harness"])
                and hf.identity(owner.get("surface")) == hf.identity(caller["surface"])
                and hf.identity(owner.get("workspace")) == hf.identity(caller["workspace"]))
    except (KeyError, TypeError, ValueError):
        return False


def discord_watch_diagnostic_report(state_dir, hailing_state, notify_home, caller=None):
    checks = {"configuration": "unknown", "registration": "unknown", "worker": "unknown",
              "intended_delivery": "unknown", "observed_delivery": "unknown"}
    state = Path(state_dir).expanduser().absolute()
    database_path = state / "discord-watch.sqlite3"
    hailing_directory = Path(hailing_state).expanduser().absolute()
    hailing_path = hailing_directory / "config.json"
    if state.is_symlink() or hailing_directory.is_symlink():
        checks["configuration"] = "blocked"
        return {"schema_version": 1, "checks": checks}

    notify_state = "unknown"
    try:
        notify = hf.agent_notify_diagnostic_report(
            notify_home, require_webhook=False, require_cached_user=True)
        notify_state = notify.get("checks", {}).get("configuration", "unknown")
        if notify_state not in ("ready", "missing", "blocked", "unknown"):
            notify_state = "unknown"
    except (AttributeError, TypeError, ValueError, OSError):
        notify_state = "unknown"

    hailing_state_status = "unknown"
    hailing_config = None
    try:
        if not hailing_directory.exists():
            hailing_state_status = "missing"
        else:
            info = hailing_directory.stat(follow_symlinks=False)
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) & 0o077 == 0,
                    "Hailing Frequencies state has unsafe ownership or permissions")
            hailing_config = _read_starfleet_json(hailing_path)
            if hailing_config is None:
                hailing_state_status = "missing"
            else:
                validator = hf.Monitor(hailing_directory, cmux=object())
                hailing_config = validator.config(hailing_config)
                hailing_state_status = "ready"
    except (OSError, ValueError, KeyError, TypeError):
        hailing_state_status = "blocked"

    database_state = "unknown"
    worker_config = None
    owned_threads = []
    thread_count = None
    meta = {}
    if not state.exists():
        database_state = "missing"
    else:
        try:
            state_info = state.stat(follow_symlinks=False)
            require(stat.S_ISDIR(state_info.st_mode) and state_info.st_uid == os.getuid()
                    and stat.S_IMODE(state_info.st_mode) & 0o077 == 0,
                    "Discord Watch state has unsafe ownership or permissions")
            if database_path.is_symlink():
                raise ValueError("Discord Watch database is a symlink")
            if not database_path.exists():
                database_state = "missing"
            elif any(Path(str(database_path) + suffix).exists()
                     or Path(str(database_path) + suffix).is_symlink()
                     for suffix in ("-wal", "-shm", "-journal")):
                database_state = "unknown"
            else:
                database_info = database_path.stat(follow_symlinks=False)
                require(stat.S_ISREG(database_info.st_mode) and database_info.st_uid == os.getuid()
                        and stat.S_IMODE(database_info.st_mode) & 0o077 == 0,
                        "Discord Watch database has unsafe ownership or permissions")
                uri = database_path.as_uri() + "?mode=ro&immutable=1"
                with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as db:
                    db.execute("PRAGMA query_only=ON")
                    tables = {row[0] for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                    require({"threads", "meta", "gateway_state"}.issubset(tables),
                            "Discord Watch schema is incomplete")
                    columns = {table: {row[1] for row in db.execute(
                        f"PRAGMA table_info({table})")} for table in
                        ("threads", "meta", "gateway_state")}
                    require({"thread_id", "owner_json", "owner_generation", "last_poll_at",
                             "last_error_json"}.issubset(columns["threads"])
                            and {"key", "value"}.issubset(columns["meta"])
                            and {"condition", "recovery_state"}.issubset(columns["gateway_state"]),
                            "Discord Watch schema requires a migration")
                    meta_keys = ("last_worker_tick_at", "worker_sender_config", "last_api_success_at",
                                 "last_worker_error")
                    meta = dict(db.execute(
                        "SELECT key,substr(value,1,4097) FROM meta WHERE key IN (" +
                        ",".join("?" for _ in meta_keys) + ")", meta_keys))
                    meta_sizes = dict(db.execute(
                        "SELECT key,length(value) FROM meta WHERE key IN (" +
                        ",".join("?" for _ in meta_keys) + ")", meta_keys))
                    require(all(size <= 4096 for size in meta_sizes.values()),
                            "Discord Watch metadata exceeds its diagnostic size limit")
                    rows = db.execute(
                        "SELECT thread_id,substr(owner_json,1,16385),length(owner_json) "
                        "FROM threads LIMIT 257").fetchall()
                    owner_rows_complete = len(rows) <= 256
                    thread_count = len(rows)
                    for thread_id, owner_json, owner_size in rows[:256]:
                        require(isinstance(thread_id, str) and re.fullmatch(r"[1-9][0-9]{0,24}", thread_id)
                                and owner_size <= 16 * 1024,
                                "Discord Watch owner record exceeds its diagnostic limits")
                        owner = json.loads(owner_json)
                        require(isinstance(owner, dict)
                                and owner.get("harness") in ALLOWED_LAUNCH_KINDS,
                                "Discord Watch owner record is incomplete")
                        hf.session_identity(owner.get("session"), owner["harness"])
                        hf.identity(owner.get("surface"))
                        hf.identity(owner.get("workspace"))
                        if _diagnostic_owner_matches(owner, caller):
                            owned_threads.append(thread_id)
                    if len(rows) > 256:
                        owner_rows_complete = False
                    database_state = "ready" if owner_rows_complete else "unknown"
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
            database_state = "blocked"

    if database_state in ("missing", "blocked", "unknown"):
        checks["configuration"] = database_state
        checks["registration"] = ("unknown" if caller is None else
                                   "missing" if database_state == "missing" else "unknown")
        checks["worker"] = "missing" if database_state == "missing" else database_state
        checks["intended_delivery"] = "unknown" if caller is None else (
            "missing" if database_state == "missing" else "unknown")
        return {"schema_version": 1, "checks": checks}

    states = (database_state, notify_state, hailing_state_status)
    checks["configuration"] = ("blocked" if "blocked" in states else
                                "missing" if "missing" in states else
                                "unknown" if "unknown" in states else "ready")
    if thread_count == 0 or (caller is not None and not owned_threads):
        checks["registration"] = "not_applicable"
        checks["intended_delivery"] = "not_applicable"
    elif caller is None:
        checks["registration"] = "unknown"
        checks["intended_delivery"] = "unknown"
    elif hailing_state_status == "blocked":
        checks["registration"] = "blocked"
        checks["intended_delivery"] = "blocked"
    elif notify_state == "blocked":
        checks["registration"] = "blocked" if not owned_threads else "ready"
        checks["intended_delivery"] = "blocked"
    else:
        exact_hailing = []
        if hailing_config is not None:
            for target in hailing_config.get("targets", []):
                if _diagnostic_owner_matches(target, caller):
                    exact_hailing.append(target)
        exact_registration = bool(owned_threads and len(exact_hailing) == 1)
        checks["registration"] = "ready" if exact_registration else "missing"
        checks["intended_delivery"] = "missing" if not exact_registration else "unknown"

    tick_raw = meta.get("last_worker_tick_at")
    config_raw = meta.get("worker_sender_config")
    worker_error = meta.get("last_worker_error")
    try:
        worker_tick = json.loads(tick_raw) if tick_raw is not None else None
        parsed_worker_config = json.loads(config_raw) if config_raw is not None else None
        if (not isinstance(worker_tick, dict) or type(worker_tick.get("at")) not in (int, float)
                or not math.isfinite(worker_tick["at"])
                or not isinstance(worker_tick.get("generation"), str)
                or re.fullmatch(r"[0-9a-f]{32}", worker_tick["generation"]) is None
                or not isinstance(parsed_worker_config, dict)
                or parsed_worker_config.get("generation") != worker_tick["generation"]
                or type(parsed_worker_config.get("send")) is not bool
                or (parsed_worker_config.get("only_thread") is not None and
                    (not isinstance(parsed_worker_config["only_thread"], str) or
                     re.fullmatch(r"[1-9][0-9]{0,24}", parsed_worker_config["only_thread"]) is None))):
            raise ValueError("Worker diagnostic metadata is invalid")
        worker_config = parsed_worker_config
        age = time.time() - worker_tick["at"]
        api_at = float(meta.get("last_api_success_at", "nan"))
        api_age = time.time() - api_at
        if not math.isfinite(api_at) or age < -5 or api_age < -5:
            raise ValueError("Worker diagnostic timestamp is invalid")
        if age > max(60, POLL_INTERVAL * 5) or api_age > max(60, POLL_INTERVAL * 5) or worker_error:
            checks["worker"] = "blocked"
        else:
            checks["worker"] = "ready"
    except (TypeError, ValueError, OverflowError):
        checks["worker"] = "missing" if tick_raw is None else "blocked"

    if caller is not None and checks["registration"] == "ready":
        if notify_state == "missing" or hailing_state_status == "missing":
            checks["intended_delivery"] = "missing"
        elif notify_state != "ready":
            checks["intended_delivery"] = "blocked" if notify_state == "blocked" else "unknown"
        elif worker_config is None:
            checks["intended_delivery"] = "missing"
        elif worker_config["send"] is not True:
            checks["intended_delivery"] = "blocked"
        elif (worker_config["only_thread"] is not None and
              worker_config["only_thread"] not in owned_threads):
            checks["intended_delivery"] = "blocked"
        else:
            checks["intended_delivery"] = "ready"
    return {"schema_version": 1, "checks": checks}


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    command.add_argument("--notify-home", type=Path, default=DEFAULT_NOTIFY_HOME)
    command.add_argument("--hailing-state", type=Path, default=DEFAULT_HAILING_STATE)
    command.add_argument("--cmux", default="cmux")
    sub = command.add_subparsers(dest="command", required=True)
    guide = sub.add_parser("guide", help="Print the safe setup guide")
    guide.add_argument("--print", action="store_true")
    register = sub.add_parser("register", help="Register this thread to the exact current owner")
    register.add_argument("thread")
    register.add_argument("--after")
    register.add_argument("--session", help="Select an exact OpenCode ses_ owner when the surface is ambiguous")
    managed = sub.add_parser("managed", help="Check exact current-owner registration")
    managed.add_argument("thread")
    managed.add_argument("--session", help="Select an exact OpenCode ses_ owner when the surface is ambiguous")
    receive = sub.add_parser("receive", help="Output and acknowledge exact pending replies")
    receive.add_argument("--thread", required=True)
    receive_selection = receive.add_mutually_exclusive_group()
    receive_selection.add_argument("--message")
    receive_selection.add_argument("--group")
    receive.add_argument("--session", help="Select the exact enrolled OpenCode ses_ owner")
    close = sub.add_parser("close", help="Close this exact owner's local thread registration")
    close.add_argument("thread")
    close.add_argument("--session", help="Select an exact OpenCode ses_ owner when the surface is ambiguous")
    transfer = sub.add_parser("transfer", help="Explicitly hand an open thread to this exact current owner")
    transfer.add_argument("thread")
    transfer.add_argument("--from-session", required=True)
    transfer.add_argument("--session", help="Select an exact OpenCode ses_ owner when the surface is ambiguous")
    adoption = sub.add_parser("custody-adoption",
                              help="Explicitly adopt selected obligations for a verified current generation")
    adoption_sub = adoption.add_subparsers(dest="adoption_action", required=True)
    for action in ("preview", "prepare"):
        child = adoption_sub.add_parser(action)
        child.add_argument("--operation-id", required=True)
        child.add_argument("--thread", action="append", default=[])
        child.add_argument("--timer-id", action="append", default=[])
        child.add_argument("--approval-reference", required=True,
                           help="Cooperative audit reference; this is not authenticated proof")
        if action == "prepare":
            child.add_argument("--plan-digest", required=True)
    for action in ("accept", "apply", "status"):
        child = adoption_sub.add_parser(action)
        child.add_argument("--operation-id", required=True)
        child.add_argument("--plan-digest", required=True)
    sub.add_parser("status", help="Show worker health and private queue metadata")
    doctor = sub.add_parser("doctor", help="Check state and configuration without printing credentials")
    doctor.add_argument("--starfleet-json", action="store_true",
                        help="emit normalized read-only setup states for Starfleet Doctor")
    for name in ("scan", "serve", "supervise"):
        child = sub.add_parser(name)
        child.add_argument("--send", action="store_true", help="Enable guarded CMUX wakes")
        child.add_argument("--only-thread", help="Limit polling and wakes to one registered thread")
        if name in ("serve", "supervise"):
            child.add_argument("--rest-only", action="store_true",
                               help="Disable Gateway intake and report degraded REST-only operation")
    return command


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    if args.command == "guide":
        print(Path(__file__).with_name("guide.md").read_text(encoding="utf-8"), end="")
        return 0
    if args.command == "doctor" and args.starfleet_json:
        caller = None if sys.stdin.isatty() else read_starfleet_caller(sys.stdin.buffer)
        notify_home = args.notify_home
        if args.notify_home == DEFAULT_NOTIFY_HOME and os.environ.get("AGENT_NOTIFY_HOME"):
            notify_home = Path(os.environ["AGENT_NOTIFY_HOME"])
        report = discord_watch_diagnostic_report(args.state_dir, args.hailing_state,
                                                 notify_home, caller)
        print(json.dumps(report, separators=(",", ":")))
        return 0
    app = Watch(args.state_dir, args.hailing_state, args.notify_home, args.cmux)
    if args.command == "custody-adoption":
        import custody_adoption
        result = custody_adoption.run(app, args)
    elif args.command == "register":
        with app.locked():
            result = app.register(args.thread, args.after, args.session)
    elif args.command == "managed":
        result = app.managed(args.thread, args.session)
    elif args.command == "receive":
        result = app.receive(args.thread, args.message, session_hint=args.session, group_id=args.group)
        if not result.get("received"):
            return 3
        return 0
    elif args.command == "close":
        with app.locked():
            result = app.close(args.thread, args.session)
    elif args.command == "transfer":
        with app.locked():
            result = app.transfer(args.thread, args.from_session, args.session)
    elif args.command == "status":
        result = app.status()
    elif args.command == "doctor":
        result = app.doctor()
    elif args.command == "scan":
        result = app.scan(args.send, args.only_thread)
    elif args.command == "serve":
        result = app.serve(args.send, args.only_thread, args.rest_only)
    elif args.command == "supervise":
        result = app.supervise(args.send, args.only_thread, args.rest_only)
    else:
        raise ValueError("Unknown command")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError, KeyError) as error:
        print(json.dumps({"error": str(error)[:500]}), file=sys.stderr)
        sys.exit(1)
