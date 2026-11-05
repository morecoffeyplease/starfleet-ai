#!/usr/bin/env python3
"""Queue replies from a configured Discord user and wake the exact enrolled CMUX owner."""

import argparse
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
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
MAX_RECEIVE_MESSAGES = 20
MAX_RECEIVE_BYTES = 64 * 1024
MAX_WAKE_ATTEMPTS = 3
REMIND_AFTER = 120
ALLOWED_LAUNCH_KINDS = {"claude", "codex", "opencode"}
ACTIVE_MESSAGE_STATES = ("pending", "awaiting_receipt", "uncertain", "stalled")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def snowflake(value, label="Discord snowflake"):
    require(isinstance(value, (str, int)) and not isinstance(value, bool), f"Invalid {label}")
    value = str(value)
    require(re.fullmatch(r"[1-9][0-9]{0,24}", value) is not None, f"Invalid {label}")
    return value


def id_order(value):
    return int(snowflake(value))


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
    def __init__(self, token, timeout=HTTP_TIMEOUT, opener=None):
        require(isinstance(token, str) and token.strip(), "DISCORD_BOT_TOKEN is required")
        self.token = token.strip()
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen

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
            with self.opener(request, timeout=self.timeout) as response:
                body = response.read(MAX_API_RESPONSE_BYTES + 1)
                require(len(body) <= MAX_API_RESPONSE_BYTES,
                        "Discord response exceeded the bounded response size")
        except urllib.error.HTTPError as error:
            body = error.read(16 * 1024)
            headers = error.headers or {}
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
                 discord_factory=Discord, cmux_factory=None):
        self.directory = Path(state_dir).expanduser().absolute()
        self.hailing_state = Path(hailing_state).expanduser().absolute()
        self.notify_home = Path(notify_home).expanduser().absolute()
        self.cmux_executable = cmux
        self.clock = clock
        self.discord_factory = discord_factory
        self.cmux_factory = cmux_factory or (lambda: hf.Cmux(cmux))
        self.global_not_before = 0.0
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
          is_open INTEGER NOT NULL DEFAULT 1,
          registered_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          last_poll_at REAL,
          next_poll_at REAL NOT NULL DEFAULT 0,
          catchup_before TEXT,
          catchup_max TEXT,
          last_error_json TEXT,
          remote_json TEXT NOT NULL DEFAULT '{}',
          owner_generation INTEGER NOT NULL DEFAULT 1
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
          PRIMARY KEY(thread_id, message_id)
        );
        CREATE INDEX IF NOT EXISTS messages_queue ON messages(thread_id, state, message_id);
        CREATE TABLE IF NOT EXISTS ownership_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          thread_id TEXT NOT NULL,
          from_session TEXT NOT NULL,
          to_session TEXT NOT NULL,
          at REAL NOT NULL,
          owner_generation INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        thread_columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
        if "catchup_before" not in thread_columns:
            db.execute("ALTER TABLE threads ADD COLUMN catchup_before TEXT")
        if "catchup_max" not in thread_columns:
            db.execute("ALTER TABLE threads ADD COLUMN catchup_max TEXT")
        message_columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        if "attachments_json" not in message_columns:
            db.execute("ALTER TABLE messages ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'")
        db.commit()
        db.close()
        path.chmod(0o600)
        db = sqlite3.connect(path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

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

    def _receipt_owner(self, session_hint=None):
        current = self.current_owner(verify=False, session_hint=session_hint)
        cmux = self.cmux_factory()
        if current["harness"] == "opencode":
            cmux.enrollment_binding(current)
        else:
            cmux.binding(current)
        return current

    @staticmethod
    def binding_snapshot(target):
        keys = ("harness", "session", "surface", "workspace", "started_at")
        if target.get("harness") == "opencode":
            keys += ("api_url", "password_file", "expected_server_pid", "session_directory",
                     "worktree_directory", "parent_task_id")
        require(all(key in target for key in keys), "Hailing Frequencies owner binding is incomplete")
        return {key: target[key] for key in keys}

    def validate_registered_owner(self, target, verify=True):
        config = self.hailing_config()
        snapshot = self.binding_snapshot(target)
        matches = [item for item in config["targets"]
                   if self.binding_snapshot(item) == snapshot]
        require(len(matches) == 1, "Registered owner is no longer enrolled in Hailing Frequencies")
        if verify:
            self.cmux_factory().binding(snapshot)

    def read_credentials(self):
        values = read_notify_credentials(self.notify_home)
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
        owner = self.current_owner(session_hint=session_hint)
        credentials, api = self.read_credentials()
        remote = self.remote_thread(api, thread_id)
        require(not self.thread_closed(remote, credentials["closed_tag"]),
                "Discord thread is archived or tagged Closed")
        if after is not None:
            cursor = snowflake(after, "after cursor")
        else:
            cursor = thread_id
        require(id_order(cursor) >= id_order(thread_id), "Cursor cannot precede thread creation")
        now = self.clock()
        with contextlib.closing(self.database()) as db:
            existing = db.execute("SELECT owner_json,cursor,is_open FROM threads WHERE thread_id=?",
                                  (thread_id,)).fetchone()
            if existing is not None:
                existing_owner = json.loads(existing[0])
                require(existing_owner == owner,
                        "Thread already belongs to another exact session; use transfer explicitly")
                require(bool(existing[2]), "Thread registration is closed")
                if after is not None:
                    require(cursor == existing[1], "Thread is already registered with a different cursor")
                with db:
                    db.execute("UPDATE threads SET updated_at=?,remote_json=? WHERE thread_id=?",
                               (now, json.dumps(remote, separators=(",", ":")), thread_id))
                return {"thread": thread_id, "registered": True, "already_registered": True,
                        "owner_session": owner["session"], "cursor": existing[1]}
            with db:
                db.execute("INSERT INTO threads(thread_id,owner_json,cursor,is_open,registered_at,updated_at,remote_json) "
                           "VALUES(?,?,?,1,?,?,?)",
                           (thread_id, json.dumps(owner, separators=(",", ":")), cursor, now, now,
                            json.dumps(remote, separators=(",", ":"))))
        return {"thread": thread_id, "registered": True, "already_registered": False,
                "owner_session": owner["session"], "cursor": cursor,
                "cursor_source": "explicit" if after is not None else "thread-created"}

    def _thread(self, db, thread_id, require_open=True):
        thread_id = snowflake(thread_id, "thread ID")
        row = db.execute("SELECT thread_id,owner_json,cursor,is_open,registered_at,updated_at,last_poll_at,"
                         "next_poll_at,catchup_before,catchup_max,last_error_json,remote_json,owner_generation "
                         "FROM threads WHERE thread_id=?",
                         (thread_id,)).fetchone()
        require(row is not None, "Thread is not registered")
        result = dict(zip(("thread_id", "owner_json", "cursor", "is_open", "registered_at", "updated_at",
                           "last_poll_at", "next_poll_at", "catchup_before", "catchup_max",
                           "last_error_json", "remote_json", "owner_generation"), row))
        result["owner"] = json.loads(result.pop("owner_json"))
        result["last_error"] = json.loads(result["last_error_json"]) if result["last_error_json"] else None
        result["remote"] = json.loads(result.pop("remote_json"))
        require(not require_open or result["is_open"], "Thread registration is closed")
        return result

    def managed(self, thread_id, session_hint=None):
        thread_id = snowflake(thread_id, "thread ID")
        current = self.current_owner(session_hint=session_hint)
        with contextlib.closing(self.database(create=False)) as db:
            thread = self._thread(db, thread_id)
            require(thread["owner"] == current,
                    "Thread belongs to another exact session; use explicit transfer")
            active = db.execute("SELECT state,COUNT(*) FROM messages WHERE thread_id=? GROUP BY state",
                                (thread_id,)).fetchall()
            counts = {state: count for state, count in active}
            status = self._health(db, thread_id)
        return {"thread": thread_id, "managed": True, "owner_session": current["session"],
                "queue": counts, "worker_health": status}

    def close(self, thread_id, session_hint=None):
        thread_id = snowflake(thread_id, "thread ID")
        current = self.current_owner(session_hint=session_hint)
        now = self.clock()
        with contextlib.closing(self.database(create=False)) as db:
            thread = self._thread(db, thread_id)
            require(thread["owner"] == current,
                    "Only the registered current owner can close this Discord Watch registration")
            with db:
                db.execute("UPDATE threads SET is_open=0,updated_at=? WHERE thread_id=?",
                           (now, thread_id))
        return {"thread": thread_id, "closed": True, "pending_preserved": True}

    def transfer(self, thread_id, from_session, session_hint=None):
        thread_id = snowflake(thread_id, "thread ID")
        old_session = hf.session_identity(from_session)
        current = self.current_owner(session_hint=session_hint)
        now = self.clock()
        with contextlib.closing(self.database(create=False)) as db:
            thread = self._thread(db, thread_id)
            require(thread["is_open"], "Thread registration is closed")
            require(thread["owner"].get("session") == old_session,
                    "--from-session does not match the registered owner")
            require(thread["owner"] != current, "Thread already belongs to this exact session")
            generation = thread["owner_generation"] + 1
            with db:
                db.execute("UPDATE threads SET owner_json=?,owner_generation=?,updated_at=? WHERE thread_id=?",
                           (json.dumps(current, separators=(",", ":")), generation, now, thread_id))
                db.execute("INSERT INTO ownership_events(thread_id,from_session,to_session,at,owner_generation) "
                           "VALUES(?,?,?,?,?)", (thread_id, old_session, current["session"], now, generation))
                db.execute("UPDATE messages SET state='pending',wake_attempts=0,last_wake_at=NULL,submitted_at=NULL "
                           "WHERE thread_id=? AND state='awaiting_receipt'", (thread_id,))
        return {"thread": thread_id, "transferred": True, "from_session": old_session,
                "to_session": current["session"], "pending_preserved": True}

    def _api_pages(self, api, thread_id, cursor, owner_id, db):
        pages = 0
        total = 0
        thread_row = db.execute("SELECT cursor,catchup_before,catchup_max FROM threads WHERE thread_id=?",
                                (thread_id,)).fetchone()
        require(thread_row is not None, "Thread registration disappeared during polling")
        current_cursor, boundary, caught_up_to = thread_row

        def attachments(row):
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

        def commit_page(normalized, next_boundary, next_max, complete):
            nonlocal current_cursor, total
            page_ids = [item[0] for item in normalized]
            if complete:
                final_cursor = next_max or (page_ids[-1] if page_ids else current_cursor)
                next_boundary = None
                next_max = None
            else:
                final_cursor = current_cursor
            db.execute("SAVEPOINT discord_watch_page")
            try:
                seen = set()
                for message_id, row in normalized:
                    require(message_id not in seen, "Discord returned duplicate message IDs")
                    seen.add(message_id)
                    author = row.get("author") if isinstance(row.get("author"), dict) else {}
                    author_id = str(author.get("id", ""))
                    if author_id == owner_id and not author.get("bot", False) and row.get("webhook_id") is None:
                        body = row.get("content", "")
                        require(isinstance(body, str), "Discord message content is not text")
                        require(len(body.encode("utf-8")) <= MAX_MESSAGE_BYTES,
                                "Discord message content exceeds the bounded storage size")
                        attachment_json = attachments(row)
                        author_name = str(author.get("global_name") or author.get("username") or "Configured user")[:256]
                        created_at = row.get("timestamp", "")
                        require(isinstance(created_at, str) and len(created_at) <= 64,
                                "Discord returned an invalid message timestamp")
                        db.execute("INSERT OR IGNORE INTO messages(thread_id,message_id,author_id,author_name,body,"
                                   "attachments_json,created_at) VALUES(?,?,?,?,?,?,?)",
                                   (thread_id, message_id, author_id, author_name, body, attachment_json, created_at))
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

        while pages < MAX_CATCHUP_PAGES:
            if boundary:
                query = {"before": boundary, "limit": MESSAGE_LIMIT}
            else:
                query = {"after": current_cursor, "limit": MESSAGE_LIMIT}
            rows = api.get(f"/channels/{thread_id}/messages", query)
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
                "catchup_limited": row[1] is not None and pages >= MAX_CATCHUP_PAGES}

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

    def _poll_thread(self, db, thread, api, credentials, now):
        thread_id = thread["thread_id"]
        try:
            with self.locked(timeout=0, name=f"poll-{thread_id}.lock"):
                latest = self._thread(db, thread_id, require_open=False)
                return self._poll_thread_locked(db, latest, api, credentials, now)
        except ValueError as error:
            if "operation owns this state" in str(error):
                return {"thread": thread_id, "status": "poll_busy"}
            raise

    def _poll_thread_locked(self, db, thread, api, credentials, now):
        thread_id = thread["thread_id"]
        if not thread["is_open"]:
            return {"thread": thread_id, "status": "closed"}
        if now < self.global_not_before:
            return {"thread": thread_id, "status": "global_backoff",
                    "retry_after": max(0, int(self.global_not_before - now))}
        if now < thread["next_poll_at"]:
            return {"thread": thread_id, "status": "backoff",
                    "retry_after": max(0, int(thread["next_poll_at"] - now))}
        try:
            remote = self.remote_thread(api, thread_id)
            if self.thread_closed(remote, credentials["closed_tag"]):
                with db:
                    db.execute("UPDATE threads SET is_open=0,remote_json=?,last_poll_at=?,last_error_json=NULL,updated_at=? "
                               "WHERE thread_id=?", (json.dumps(remote, separators=(",", ":")), now, now, thread_id))
                return {"thread": thread_id, "status": "closed_remote"}
            result = self._api_pages(api, thread_id, thread["cursor"], credentials["user_id"], db)
            with db:
                db.execute("UPDATE threads SET remote_json=?,last_poll_at=?,next_poll_at=0,last_error_json=NULL,updated_at=? "
                           "WHERE thread_id=?", (json.dumps(remote, separators=(",", ":")), now, now, thread_id))
            return {"thread": thread_id, "status": "polled", **result}
        except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
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
        if not thread["is_open"]:
            return {"state": "closed", "would_wake": False}
        # An uncertain or exhausted wake blocks later prompts until the owner
        # explicitly receives the queued reply.
        blocked = db.execute("SELECT message_id,state FROM messages WHERE thread_id=? "
                             "AND state IN ('uncertain','stalled') ORDER BY length(message_id),message_id LIMIT 1",
                             (thread_id,)).fetchone()
        if blocked:
            return {"message": blocked[0], "state": blocked[1], "would_wake": False}
        row = db.execute("SELECT message_id,state,wake_attempts,last_wake_at FROM messages "
                         "WHERE thread_id=? AND state IN ('pending','awaiting_receipt') "
                         "ORDER BY length(message_id),message_id LIMIT 1", (thread_id,)).fetchone()
        if row is None:
            return None
        message_id, state, attempts, last_wake_at = row
        if state == "awaiting_receipt":
            if last_wake_at is None or now - last_wake_at < REMIND_AFTER:
                return {"message": message_id, "state": state, "would_wake": False}
            if attempts >= MAX_WAKE_ATTEMPTS:
                with db:
                    db.execute("UPDATE messages SET state='stalled' WHERE thread_id=? AND message_id=? "
                               "AND state='awaiting_receipt'", (thread_id, message_id))
                return {"message": message_id, "state": "stalled", "would_wake": False}
        if state == "pending" and attempts >= MAX_WAKE_ATTEMPTS:
            with db:
                db.execute("UPDATE messages SET state='stalled' WHERE thread_id=? AND message_id=? "
                           "AND state='pending'", (thread_id, message_id))
            return {"message": message_id, "state": "stalled", "would_wake": False}
        owner = thread["owner"]
        self.validate_registered_owner(owner)
        prompt = self._wake_prompt(thread_id, message_id, owner)

        def mark_transport_start():
            owner_json = json.dumps(owner, separators=(",", ":"))
            with db:
                updated = db.execute(
                    "UPDATE messages SET state='uncertain',wake_attempts=wake_attempts+1,last_wake_at=?, "
                    "submitted_at=NULL,last_error_json=NULL WHERE thread_id=? AND message_id=? "
                    "AND state IN ('pending','awaiting_receipt') AND EXISTS ("
                    "SELECT 1 FROM threads WHERE thread_id=? AND is_open=1 AND owner_json=?)",
                    (now, thread_id, message_id, thread_id, owner_json))
                require(updated.rowcount == 1,
                        "Reply or ownership changed before the CMUX submit; wake held")

        try:
            self.cmux_factory().submit(owner, prompt, on_transport_start=mark_transport_start)
        except hf.DeliveryHoldError as error:
            latest = db.execute("SELECT state FROM messages WHERE thread_id=? AND message_id=?",
                                (thread_id, message_id)).fetchone()
            state_now = latest[0] if latest else "missing"
            with db:
                db.execute("UPDATE messages SET last_error_json=? WHERE thread_id=? AND message_id=? "
                           "AND state!='received'", (json.dumps({"held": str(error)[:500]}),
                                                       thread_id, message_id))
            return {"message": message_id, "state": state_now, "held": str(error)[:300]}
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            latest = db.execute("SELECT state FROM messages WHERE thread_id=? AND message_id=?",
                                (thread_id, message_id)).fetchone()
            state_now = latest[0] if latest else "missing"
            if state_now == "uncertain":
                with db:
                    db.execute("UPDATE messages SET last_error_json=? WHERE thread_id=? AND message_id=? "
                               "AND state='uncertain'", (json.dumps({"uncertain": str(error)[:500]}),
                                                           thread_id, message_id))
                return {"message": message_id, "state": "uncertain", "error": str(error)[:300]}
            with db:
                db.execute("UPDATE messages SET last_error_json=? WHERE thread_id=? AND message_id=? "
                           "AND state!='received'", (json.dumps({"held": str(error)[:500]}),
                                                       thread_id, message_id))
            return {"message": message_id, "state": state_now, "held": str(error)[:300]}
        owner_json = json.dumps(owner, separators=(",", ":"))
        with db:
            db.execute("UPDATE messages SET state='awaiting_receipt',submitted_at=?,last_wake_at=?,last_error_json=NULL "
                       "WHERE thread_id=? AND message_id=? AND state='uncertain' AND EXISTS ("
                       "SELECT 1 FROM threads WHERE thread_id=? AND is_open=1 AND owner_json=?)",
                       (now, now, thread_id, message_id, thread_id, owner_json))
        final = db.execute("SELECT state FROM messages WHERE thread_id=? AND message_id=?",
                           (thread_id, message_id)).fetchone()
        if final and final[0] == "received":
            return {"message": message_id, "state": "received", "submitted": True,
                    "received_during_submit": True}
        if final and final[0] == "uncertain":
            return {"message": message_id, "state": "uncertain", "submitted": True,
                    "owner_changed_during_submit": True}
        return {"message": message_id, "state": "awaiting_receipt", "submitted": True}

    def scan(self, send=False, only_thread=None, origin="manual"):
        credentials, api = self.read_credentials()
        now = self.clock()
        report = {"at": now, "mode": "send" if send else "observe", "origin": origin, "threads": []}
        with contextlib.closing(self.database(create=False)) as db:
            global_row = db.execute("SELECT value FROM meta WHERE key='global_not_before'").fetchone()
            self.global_not_before = float(global_row[0]) if global_row else 0.0
            if only_thread is not None:
                thread_ids = [snowflake(only_thread, "thread ID")]
            else:
                thread_ids = [row[0] for row in db.execute(
                    "SELECT thread_id FROM threads WHERE is_open=1 ORDER BY registered_at,thread_id")]
            send_scope = list(thread_ids) if send else []
            for thread_id in thread_ids:
                try:
                    thread = self._thread(db, thread_id)
                except ValueError as error:
                    report["threads"].append({"thread": thread_id, "status": "error",
                                               "error": {"error": str(error)[:300]}})
                    continue
                polled = self._poll_thread(db, thread, api, credentials, now)
                row = {**polled}
                if polled.get("status") == "polled":
                    queue_count = db.execute("SELECT COUNT(*) FROM messages WHERE thread_id=? AND state='pending'",
                                             (thread_id,)).fetchone()[0]
                    row["pending"] = queue_count
                    if send:
                        try:
                            latest_thread = self._thread(db, thread_id)
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
            api_success = any(item.get("status") in ("polled", "closed_remote") for item in report["threads"])
            self._record_scan_meta(db, report, api_success, send_scope)
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
                values["last_worker_tick_at"] = str(at)
            errors = [item for item in report["threads"] if item.get("status") == "error"]
            values["last_scan_errors"] = str(len(errors))
            for key, value in values.items():
                db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            if origin == "worker":
                db.execute("DELETE FROM meta WHERE key='last_worker_error'")

    def _record_worker_failure(self, send, only_thread, error):
        try:
            with contextlib.closing(self.database(create=False)) as db:
                now = self.clock()
                scope = [snowflake(only_thread, "thread ID")] if only_thread else [
                    row[0] for row in db.execute("SELECT thread_id FROM threads WHERE is_open=1")]
                report = {"at": now, "mode": "send" if send else "observe", "origin": "worker",
                          "threads": [{"thread": item, "status": "error"} for item in scope]}
                self._record_scan_meta(db, report, False, scope if send else [])
                with db:
                    db.execute("INSERT INTO meta(key,value) VALUES('last_worker_error',?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(error)[:500],))
        except (ValueError, OSError, sqlite3.Error):
            return

    def receive(self, thread_id, message_id=None, output=None, session_hint=None):
        thread_id = snowflake(thread_id, "thread ID")
        current = self._receipt_owner(session_hint=session_hint)
        output = output or sys.stdout
        with contextlib.closing(self.database(create=False)) as db:
            thread = self._thread(db, thread_id, require_open=False)
            require(thread["owner"] == current,
                    "Only the registered current owner can receive this thread's replies")
            rows = db.execute("SELECT message_id,author_name,body,attachments_json,state FROM messages WHERE thread_id=? "
                              "AND state!='received' ORDER BY length(message_id),message_id",
                              (thread_id,)).fetchall()
            if message_id is not None:
                message_id = snowflake(message_id, "message ID")
                rows = [row for row in rows if row[0] == message_id]
                require(len(rows) == 1, "Reply is missing or already received")
            else:
                rows = rows[:MAX_RECEIVE_MESSAGES]
            if not rows:
                return {"thread": thread_id, "received": [], "pending": False}
            rendered_parts = []
            for row in rows:
                rendered_parts.append(f"[discord:{thread_id}:{row[0]}] {row[1]}: {row[2]}")
                for attachment in json.loads(row[3]):
                    filename = re.sub(r"[\x00-\x1f\x7f]", " ", attachment["filename"])
                    rendered_parts.append(f"[attachment] {filename}: {attachment['url']}")
            rendered = "\n".join(rendered_parts) + "\n"
            payload = rendered.encode("utf-8")
            require(len(payload) <= MAX_RECEIVE_BYTES,
                    "Reply output exceeds 64 KiB; receive one message by exact ID")
            written = output.write(rendered)
            require(written == len(rendered), "Reply output was incomplete; no replies were acknowledged")
            output.flush()
            # Revalidate the exact live owner after the output boundary.
            latest = self._receipt_owner(session_hint=session_hint)
            require(latest == current, "Owner changed during reply output; no replies were acknowledged")
            now = self.clock()
            owner_json = json.dumps(current, separators=(",", ":"))
            with db:
                for row in rows:
                    updated = db.execute(
                        "UPDATE messages SET state='received',received_at=?,last_error_json=NULL "
                        "WHERE thread_id=? AND message_id=? AND state!='received' AND EXISTS ("
                        "SELECT 1 FROM threads WHERE thread_id=? AND owner_json=?)",
                        (now, thread_id, row[0], thread_id, owner_json))
                    require(updated.rowcount == 1,
                            "Reply or ownership changed during output; no replies were acknowledged")
        return {"thread": thread_id, "received": [row[0] for row in rows],
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
                "last_api_success_at", "last_worker_tick_at", "last_send_scope", "last_worker_error")
        meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN (" +
                               ",".join("?" for _ in keys) + ")", keys))
        now = self.clock()
        def age_of(key):
            try:
                return max(0, now - float(meta[key])) if meta.get(key) else None
            except (ValueError, TypeError):
                return None
        worker_age = age_of("last_worker_tick_at")
        api_age = age_of("last_api_success_at")
        heartbeat_fresh = worker_age is not None and worker_age <= max(60, POLL_INTERVAL * 5)
        api_fresh = api_age is not None and api_age <= max(60, POLL_INTERVAL * 5)
        worker_running = self._lock_is_held("worker.lock")
        supervisor_running = self._lock_is_held("supervise.lock")
        send_scope = []
        try:
            send_scope = json.loads(meta.get("last_send_scope", "[]"))
        except ValueError:
            pass
        in_scope = thread_id in send_scope if thread_id is not None else bool(send_scope)
        send_enabled = meta.get("last_scan_mode") == "send"
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
        ready = worker_running and heartbeat_fresh and api_fresh and send_enabled and in_scope and thread_api_fresh
        warnings = []
        if not worker_running:
            warnings.append("No Discord Watch worker currently holds the worker lock")
        if worker_running and not heartbeat_fresh:
            warnings.append("Worker heartbeat is missing or stale")
        if not api_fresh:
            warnings.append("No successful Discord API poll is fresh")
        if not send_enabled:
            warnings.append("The latest scan was observe-only or has not run")
        if thread_id is not None and not in_scope:
            warnings.append("This thread is outside the latest send scope")
        if thread_id is not None and not thread_api_fresh:
            warnings.append("This thread has no fresh successful API poll")
        if thread_error:
            warnings.append("This thread has a recorded Discord API or polling error")
        state = "ready_for_wakes" if ready and thread_error is None else (
            "worker_running" if worker_running else "worker_stopped")
        return {"state": state, "ready_for_wakes": ready and thread_error is None,
                "worker_running": worker_running, "supervisor_running": supervisor_running,
                "worker_heartbeat_age_seconds": worker_age,
                "worker_heartbeat_fresh": heartbeat_fresh,
                "api_last_success_age_seconds": api_age, "api_success_fresh": api_fresh,
                "last_scan_at": float(meta["last_scan_at"]) if meta.get("last_scan_at") else None,
                "last_scan_origin": meta.get("last_scan_origin"),
                "last_scan_mode": meta.get("last_scan_mode"),
                "last_scan_errors": int(meta.get("last_scan_errors", "0")),
                "last_worker_error": meta.get("last_worker_error"),
                "last_send_scope": send_scope,
                "thread_in_send_scope": in_scope if thread_id is not None else None,
                "thread_last_poll_age_seconds": thread_poll_age,
                "thread_error": thread_error, "warnings": warnings,
                "note": "A successful API poll and confirmed CMUX submit or OpenCode user-message admission are distinct from explicit reply receipt."}

    def status(self):
        with contextlib.closing(self.database(create=False)) as db:
            threads = []
            for row in db.execute("SELECT thread_id,owner_json,cursor,is_open,registered_at,last_poll_at,next_poll_at,"
                                  "last_error_json,owner_generation FROM threads ORDER BY registered_at,thread_id"):
                thread_id, owner_json, cursor, is_open, registered_at, last_poll_at, next_poll_at, last_error, generation = row
                counts = {state: count for state, count in db.execute(
                    "SELECT state,COUNT(*) FROM messages WHERE thread_id=? GROUP BY state", (thread_id,))}
                recent = [{"message": message_id, "state": state, "wake_attempts": attempts,
                           "submitted_at": submitted_at, "received_at": received_at,
                           "last_error": json.loads(message_error) if message_error else None}
                          for message_id, state, attempts, submitted_at, received_at, message_error in db.execute(
                              "SELECT message_id,state,wake_attempts,submitted_at,received_at,last_error_json "
                              "FROM messages WHERE thread_id=? ORDER BY length(message_id) DESC,message_id DESC LIMIT 20",
                              (thread_id,))]
                threads.append({"thread": thread_id, "owner": json.loads(owner_json), "cursor": cursor,
                                "open": bool(is_open), "registered_at": registered_at,
                                "last_poll_at": last_poll_at, "next_poll_at": next_poll_at,
                                "last_error": json.loads(last_error) if last_error else None,
                                "owner_generation": generation, "queue": counts, "recent": recent})
            return {"health": self._health(db), "threads": threads}

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

    def serve(self, send=False, only_thread=None):
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
            original_handlers = {}
            def stop(signum, frame):
                nonlocal stopping
                stopping = True
            for sig in (signal.SIGINT, signal.SIGTERM):
                original_handlers[sig] = signal.signal(sig, stop)
            try:
                while not stopping:
                    try:
                        report = self.scan(send=send, only_thread=only_thread, origin="worker")
                    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
                        report = {"at": self.clock(), "mode": "send" if send else "observe",
                                  "worker_error": str(error)[:500]}
                        self._record_worker_failure(send, only_thread, error)
                    print(json.dumps(report, ensure_ascii=False), flush=True)
                    deadline = time.monotonic() + POLL_INTERVAL
                    while not stopping and time.monotonic() < deadline:
                        time.sleep(min(1, max(0, deadline - time.monotonic())))
            finally:
                for sig, handler in original_handlers.items():
                    signal.signal(sig, handler)
                fcntl.flock(lock, fcntl.LOCK_UN)
        return {"stopped": True}

    def supervise(self, send=False, only_thread=None):
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
    receive.add_argument("--message")
    receive.add_argument("--session", help="Select the exact enrolled OpenCode ses_ owner")
    close = sub.add_parser("close", help="Close this exact owner's local thread registration")
    close.add_argument("thread")
    close.add_argument("--session", help="Select an exact OpenCode ses_ owner when the surface is ambiguous")
    transfer = sub.add_parser("transfer", help="Explicitly hand an open thread to this exact current owner")
    transfer.add_argument("thread")
    transfer.add_argument("--from-session", required=True)
    transfer.add_argument("--session", help="Select an exact OpenCode ses_ owner when the surface is ambiguous")
    sub.add_parser("status", help="Show worker health and private queue metadata")
    sub.add_parser("doctor", help="Check state and configuration without printing credentials")
    for name in ("scan", "serve", "supervise"):
        child = sub.add_parser(name)
        child.add_argument("--send", action="store_true", help="Enable guarded CMUX wakes")
        child.add_argument("--only-thread", help="Limit polling and wakes to one registered thread")
    return command


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    if args.command == "guide":
        print(Path(__file__).with_name("guide.md").read_text(encoding="utf-8"), end="")
        return 0
    app = Watch(args.state_dir, args.hailing_state, args.notify_home, args.cmux)
    if args.command == "register":
        with app.locked():
            result = app.register(args.thread, args.after, args.session)
    elif args.command == "managed":
        result = app.managed(args.thread, args.session)
    elif args.command == "receive":
        with app.locked(timeout=LOCK_TIMEOUT, name="receive.lock"):
            result = app.receive(args.thread, args.message, session_hint=args.session)
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
        result = app.serve(args.send, args.only_thread)
    elif args.command == "supervise":
        result = app.supervise(args.send, args.only_thread)
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
