"""Durable, exact-session continuation timers for Hailing Frequencies."""
from datetime import datetime, timezone
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import time
import uuid


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
        CREATE TABLE IF NOT EXISTS custody_adoption_operations (
          operation_id TEXT PRIMARY KEY, plan_json TEXT NOT NULL, plan_digest TEXT NOT NULL,
          approval_reference TEXT NOT NULL, state TEXT NOT NULL, accepted_owner_json TEXT,
          accepted_at REAL, apply_started_at REAL, last_error TEXT,
          created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS custody_adoption_holds (
          operation_id TEXT NOT NULL, resource_kind TEXT NOT NULL, resource_id TEXT NOT NULL,
          plan_digest TEXT NOT NULL, PRIMARY KEY(operation_id,resource_kind,resource_id)
        );
        CREATE TABLE IF NOT EXISTS custody_adoption_events (
          operation_id TEXT NOT NULL, resource_kind TEXT NOT NULL, resource_id TEXT NOT NULL,
          plan_digest TEXT NOT NULL, old_owner_digest TEXT NOT NULL, new_owner_digest TEXT NOT NULL,
          before_digest TEXT NOT NULL, after_preserved_digest TEXT NOT NULL,
          committed_at REAL NOT NULL, PRIMARY KEY(operation_id,resource_kind,resource_id)
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
        schedule_spec = json.dumps({"schedule": schedule, "value": schedule_value,
                                    "until_at": deadline, "continuation": continuation.strip()},
                                   sort_keys=True, separators=(",", ":"))
        spec = (owner_key, target["harness"], target["session"], target["surface"], target["workspace"],
                target["started_at"], worktree, continuation.strip(), schedule, interval, due, deadline,
                request_id, replacement_key, schedule_spec)
        with self.locked(), contextlib.closing(self.database()) as db:
            if self.app.custody_adoption_hold("hailing_session", target["session"]):
                raise ValueError("Session is held by an explicit custody adoption")
            db.execute("BEGIN IMMEDIATE")
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
            require(not self.app.custody_adoption_hold("temporal_timer", timer_id),
                    "Timer is held by an explicit custody adoption")
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
            require(not self.app.custody_adoption_hold("temporal_timer", occurrence["timer_id"]),
                    "Timer is held by an explicit custody adoption")
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
                if self.app.custody_adoption_hold("temporal_timer", timer["id"]):
                    results.append({"timer_id": timer["id"], "status": "custody_adoption_hold",
                                    "would_submit": False})
                    db.commit()
                    continue
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
