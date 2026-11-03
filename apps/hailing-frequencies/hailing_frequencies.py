#!/usr/bin/env python3
"""Monitor native CMUX inboxes and nudge explicitly enrolled Claude/Codex sessions."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

DEFAULT_STATE = Path.home() / ".local/state/starfleet-ai/hailing-frequencies"
SCAN_LIMIT = 1000
INBOX_MESSAGE_LIMIT = 20
INBOX_OUTPUT_LIMIT = 64 * 1024
ATTENTION_MESSAGE_LIMIT = 4 * 1024
ACTIVE_LIFECYCLES = ("running", "idle", "backgroundWorkPending")
INSTALL_MARKER = "# starfleet-hailing-frequencies:v1"
RUNTIME_SOURCE_NAMES = ("hailing_frequencies.py", "service.py", "temporal_beacon.py", "guide.md",
                        "agent-notify", "agent-notify-guide.md")
RUNTIME_SKILL_NAMES = ("skills/temporal-beacon/SKILL.md", "skills/agent-notify/SKILL.md")


class LockBusyError(ValueError):
    """A state operation already owns the monitor lock."""


class DeliveryHoldError(ValueError):
    """The final guarded binding check held delivery before paste began."""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def identity(value):
    return str(uuid.UUID(value)).upper()


def atomic_json(path, value):
    require(not path.is_symlink(), f"Refusing symlink: {path}")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with open(temporary, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Cmux:
    def __init__(self, executable="cmux", hook_directory=None):
        self.executable = executable
        self.hook_directory = Path(hook_directory or Path.home() / ".cmuxterm")
        self.stopping = lambda: False

    def command(self, *args):
        require(not self.stopping(), "Monitor stopping; no more CMUX operations")
        result = subprocess.run([self.executable, "--json", *args], capture_output=True,
                                text=True, timeout=15)
        require(result.returncode == 0, result.stderr.strip() or "CMUX command failed")
        return json.loads(result.stdout)

    def rpc(self, method, **params):
        return self.command("rpc", method, json.dumps(params))

    def messages(self, target):
        # Omitting state requests the full inbox, including native read records
        # needed to correlate exact replies. Never change native read state.
        rows = self.rpc("agent.message.list", surface=target["surface"], limit=SCAN_LIMIT)["messages"]
        require(len(rows) < SCAN_LIMIT, "Inbox scan saturated; no nudges sent. Reconcile native inboxes first.")
        return rows

    def activity(self, target):
        """Read activity for one exact native session without changing its helper."""
        current = self.command("current", "--limit", "200")
        require(not current.get("truncated", True), "CMUX activity snapshot is incomplete")
        require(current.get("owner_availability", {}).get("agent_sessions") == "available",
                "CMUX activity identities unavailable")
        matches = [agent for item in current["items"]
                   if item.get("placement", {}).get("kind") == "local"
                   and any(p.get("panel_id", "").upper() == target["surface"]
                           and p.get("workspace_id", "").upper() == target["workspace"]
                           for p in item.get("projections", []))
                   for agent in item.get("agents", [])
                   if agent.get("session_id", "").upper() == target["session"]
                   and agent.get("kind") == target["harness"]]
        require(len(matches) == 1, "Exact session activity is unavailable or ambiguous")
        return {"state": matches[0].get("state"), "last_activity_at": matches[0].get("last_activity_at")}

    def binding(self, target):
        """Require a live projection AND its owning harness's exact hook binding."""
        current = self.command("current", "--limit", "200")
        require(not current.get("truncated", True), "CMUX current snapshot is incomplete")
        require(current.get("owner_availability", {}).get("agent_sessions") == "available",
                "CMUX agent identities unavailable")
        matches = []
        for item in current["items"]:
            if item.get("placement", {}).get("kind") != "local":
                continue
            for projection in item.get("projections", []):
                if projection.get("panel_id", "").upper() == target["surface"]:
                    matches.append((item, projection))
        require(len(matches) == 1, "Surface is missing or ambiguous")
        item, projection = matches[0]
        require(identity(projection["workspace_id"]) == target["workspace"], "Workspace changed")
        require(any(a.get("session_id", "").upper() == target["session"] and
                    a.get("kind") == target["harness"] and a.get("state") in ("working", "idle")
                    for a in item.get("agents", [])), "Session is absent, ended or waiting for input")
        store = json.loads((self.hook_directory / (target["harness"] + "-hook-sessions.json")).read_text())
        records = {k.upper(): v for k, v in store["sessions"].items()}
        record = records.get(target["session"], {})
        require(record.get("surfaceId", "").upper() == target["surface"] and
                record.get("workspaceId", "").upper() == target["workspace"], "Hook binding changed")
        if target["harness"] == "claude":
            active = store.get("activeSessionsBySurface", {}).get(target["surface"], {})
            require(active.get("sessionId", "").upper() == target["session"], "Claude session replaced")
        require(record.get("agentLifecycle") in ACTIVE_LIFECYCLES, "Harness is not running, idle or awaiting background work")
        started = record.get("startedAt")
        require(isinstance(started, (int, float)) and math.isfinite(started), "Missing session start evidence")
        require(started == target["started_at"], "Session generation changed")
        guard = self.rpc("surface.input_state", surface_id=target["surface"])
        require(guard.get("surface_id", "").upper() == target["surface"] and
                guard.get("workspace_id", "").upper() == target["workspace"], "Input owner changed")
        require(guard.get("agent") is True and guard.get("terminal") is True and
                guard.get("state") == "empty" and guard.get("blocks_typing") is False and
                guard.get("waiting_on_human") is False and guard.get("lifecycle") in ACTIVE_LIFECYCLES,
                "Human draft, approval, unknown input state or inactive session")
        settings = self.rpc("agent.message.settings", target=target["surface"])
        require(all(settings.get(k) is True for k in ("messages_enabled", "receiving", "workspace_receiving")),
                "Native messaging is disabled")
        return True

    def submit(self, target, text, on_transport_start=None):
        # One host paste/submit operation, never a force flag or separate Escape/Ctrl-C.
        # A multiline Claude paste selects the host's agent-aware submit key.
        try:
            self.binding(target)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise DeliveryHoldError(str(exc)) from exc
        if on_transport_start is not None:
            on_transport_start()
        result = self.command("paste", "--surface", target["surface"], "--submit", "--", text)
        require(result.get("submitted") is True, "Paste outcome uncertain: host did not confirm submit")
        return result


class Monitor:
    def __init__(self, directory, cmux=None, clock=time.time):
        self.directory = Path(directory).expanduser().absolute()
        self.cmux = cmux or Cmux()
        self.clock = clock

    @contextlib.contextmanager
    def locked(self, timeout=0):
        require(self.directory.is_dir() and not self.directory.is_symlink(), "Initialize a real state directory first")
        require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout >= 0,
                "Lock timeout must be a finite nonnegative number")
        path = self.directory / "monitor.lock"
        require(not path.is_symlink(), "Refusing symlink lock")
        with path.open("a") as stream:
            deadline = time.monotonic() + timeout
            try:
                while True:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            if timeout:
                                raise LockBusyError("Another Hailing Frequencies operation owns this state; retry at the next checkpoint") from None
                            raise LockBusyError("Another Hailing Frequencies operation owns this state") from None
                        time.sleep(min(0.1, remaining))
            except BaseException:
                stream.close()
                raise
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def config(self):
        value = json.loads((self.directory / "config.json").read_text())
        require(value.get("version") == 1, "Unsupported configuration version")
        for key in ("remind_after", "escalate_after", "cooldown", "interval"):
            require(type(value.get(key)) in (int, float) and math.isfinite(value[key]) and value[key] > 0,
                    f"Invalid {key}")
        require(value["escalate_after"] >= value["remind_after"], "Escalation precedes reminder")
        require(value["interval"] >= 5, "Interval must be at least 5 seconds")
        require(isinstance(value.get("targets"), list), "Targets must be an array")
        surfaces, sessions = set(), set()
        for target in value["targets"]:
            require(target["harness"] in ("claude", "codex"), "Unsupported harness")
            for key in ("surface", "workspace", "session"):
                require(identity(target[key]) == target[key], "Use canonical UUIDs")
            for key in ("cutoff", "started_at"):
                require(type(target[key]) in (int, float) and math.isfinite(target[key]) and target[key] >= 0,
                        f"Invalid {key}")
            require(target["surface"] not in surfaces and target["session"] not in sessions,
                    "Duplicate surface or session enrollment")
            surfaces.add(target["surface"])
            sessions.add(target["session"])
        require(value.get("coordinator") is None or value["coordinator"] in sessions, "Coordinator is not enrolled")
        return value

    def database(self):
        path = self.directory / "receipts.sqlite3"
        require(not path.is_symlink(), "Refusing symlink database")
        db = sqlite3.connect(path)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS attempts (session TEXT, message TEXT, kind TEXT, at REAL, outcome TEXT, PRIMARY KEY(session,message,kind))")
        db.execute("CREATE TABLE IF NOT EXISTS batches (token TEXT PRIMARY KEY, session TEXT, at REAL, outcome TEXT, detail TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS acknowledgements (session TEXT, message TEXT, at REAL, evidence TEXT, PRIMARY KEY(session,message))")
        db.execute("CREATE TABLE IF NOT EXISTS attention_intents (session TEXT, started_at REAL, event_key TEXT, body_sha256 TEXT, outcome TEXT, attempted INTEGER, at REAL, detail TEXT, PRIMARY KEY(session,started_at,event_key))")
        db.commit()
        return db

    def prior_attention_intent(self, target, event_key):
        path = self.directory / "receipts.sqlite3"
        require(not path.is_symlink(), "Refusing symlink database")
        if not path.is_file():
            return None
        uri = path.resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attention_intents'").fetchone()
            if table is None:
                return None
            return db.execute(
                "SELECT body_sha256,outcome,attempted,detail FROM attention_intents "
                "WHERE session=? AND started_at=? AND event_key=?",
                (target["session"], target["started_at"], event_key)).fetchone()

    def attention_result(self, outcome, attempted, detail, event_key):
        return {"outcome": outcome, "attempted": bool(attempted),
                "detail": str(detail)[:500], "event_key": str(event_key)[:128]}

    def attention_once(self, session_id, event_key, body, surface_id):
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", event_key) is None:
            return self.attention_result("rejected", False,
                                         "Event key must be 1-128 ASCII letters, digits, dots, underscores, colons or hyphens",
                                         event_key)
        session = identity(session_id)
        config = self.config()
        target = next((item for item in config["targets"] if item["session"] == session), None)
        if target is None:
            return self.attention_result("unavailable", False, "Session is not enrolled", event_key)
        if not surface_id:
            return self.attention_result("unavailable", False, "CMUX_SURFACE_ID is required", event_key)
        try:
            surface = identity(surface_id)
        except ValueError:
            return self.attention_result("unavailable", False, "CMUX_SURFACE_ID is invalid", event_key)
        if surface != target["surface"]:
            return self.attention_result("unavailable", False,
                                         "CMUX_SURFACE_ID does not match the enrolled surface", event_key)
        launch_kind = os.environ.get("CMUX_AGENT_LAUNCH_KIND", "").strip().casefold()
        # Native Claude team surfaces identify their launcher as "claudeTeams".
        launch_kind = {"claudeteams": "claude"}.get(launch_kind, launch_kind)
        if launch_kind and launch_kind != target["harness"]:
            return self.attention_result("unavailable", False,
                                         "Caller launch kind does not match the enrolled session", event_key)
        session_env = "CLAUDE_CODE_SESSION_ID" if target["harness"] == "claude" else "CODEX_THREAD_ID"
        caller_session = os.environ.get(session_env)
        if caller_session:
            try:
                caller_session = identity(caller_session)
            except ValueError:
                return self.attention_result("unavailable", False,
                                             f"Caller {session_env} is invalid", event_key)
            if caller_session != session:
                return self.attention_result("unavailable", False,
                                             "Caller session does not match the enrolled session", event_key)

        body_sha256 = hashlib.sha256(body).hexdigest()
        previous = self.prior_attention_intent(target, event_key)
        if previous is not None:
            saved_hash, outcome, attempted, detail = previous
            if saved_hash != body_sha256:
                return self.attention_result("rejected", False,
                                             "Event key was already used with a different message", event_key)
            return self.attention_result(outcome, attempted, detail, event_key)

        try:
            self.cmux.binding(target)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            return self.attention_result("held", False, f"{error}; no intent recorded", event_key)

        message = body.decode("utf-8")
        now = self.clock()
        with contextlib.closing(self.database()) as db:
            with db:
                db.execute("INSERT INTO attention_intents VALUES (?,?,?,?,?,?,?,?)",
                           (session, target["started_at"], event_key, body_sha256,
                            "uncertain", 0, now, "Intent committed before submit"))

            attempted = False

            def mark_transport_start():
                nonlocal attempted
                with db:
                    db.execute("UPDATE attention_intents SET attempted=1 WHERE session=? AND started_at=? AND event_key=?",
                               (session, target["started_at"], event_key))
                attempted = True

            try:
                self.cmux.submit(target, message, on_transport_start=mark_transport_start)
            except DeliveryHoldError as error:
                outcome, detail, attempted = "held", f"{error}; event key is reserved and will not be retried", False
            except Exception:
                outcome = "uncertain"
                detail = "CMUX submit was not confirmed; delivery is uncertain"
            else:
                outcome, detail, attempted = "submitted", "CMUX confirmed submit", True
            with db:
                db.execute("UPDATE attention_intents SET outcome=?,attempted=?,detail=? "
                           "WHERE session=? AND started_at=? AND event_key=?",
                           (outcome, int(attempted), detail[:500], session, target["started_at"], event_key))
        return self.attention_result(outcome, attempted, detail, event_key)

    def source_messages(self, target, messages):
        sources = []
        for message in messages:
            if str(message.get("recipient_surface_id") or "").upper() != target["surface"] or \
                    str(message.get("recipient_workspace_id") or "").upper() != target["workspace"]:
                continue
            # Never treat our own reminders or outbound messages as an inbound request.
            if message.get("sender_name") == "hailing-frequencies" or \
                    str(message.get("sender_surface_id") or "").upper() == target["surface"]:
                continue
            identity(message["id"])
            created = message.get("created_at")
            require(type(created) in (int, float) and math.isfinite(created), "Malformed message timestamp")
            if created < max(target["started_at"], target["cutoff"]):
                continue
            sources.append(message)
        return sources

    def enrolled_target(self, session_id, surface_id):
        config = self.config()
        session = identity(session_id)
        require(surface_id, "CMUX_SURFACE_ID is required")
        surface = identity(surface_id)
        target = next((item for item in config["targets"] if item["session"] == session), None)
        require(target is not None, "Session is not enrolled")
        require(surface == target["surface"], "CMUX_SURFACE_ID does not match the enrolled surface")
        return target

    def select_inbox_messages(self, target, message_ids, rows):
        by_id = {}
        for message in self.source_messages(target, rows):
            by_id.setdefault(identity(message["id"]), []).append(message)
        selected = []
        for message_id in message_ids:
            matches = by_id.get(message_id, [])
            require(len(matches) == 1, "Message is missing, ambiguous, or outside this session's enrollment")
            message = matches[0]
            require(isinstance(message.get("body"), str), "Message body is unavailable")
            require(isinstance(message.get("sender_name"), str), "Message sender name is unavailable")
            require(isinstance(message.get("sender_surface_id"), str), "Message sender surface is unavailable")
            require(isinstance(message.get("sender_workspace_id"), str), "Message sender workspace is unavailable")
            identity(message.get("sender_surface_id"))
            identity(message.get("sender_workspace_id"))
            require(message.get("in_reply_to") is None or isinstance(message.get("in_reply_to"), str),
                    "Message reply reference is malformed")
            if message.get("in_reply_to") is not None:
                identity(message["in_reply_to"])
            selected.append(message)
        return selected

    def inbox_command(self, target, message_ids):
        return self._inbox_command(target, message_ids, self.installed_launcher())

    def _inbox_command(self, target, message_ids, launcher):
        if launcher is not None:
            command = [str(launcher), "--state-dir", str(self.directory), "inbox", "--session", target["session"]]
        else:
            command = [sys.executable, str(Path(__file__).resolve()),
                       "--state-dir", str(self.directory), "--cmux", self.cmux.executable,
                       "inbox", "--session", target["session"]]
        for message_id in message_ids:
            command.extend(("--message", identity(message_id)))
        return shlex.join(command)

    def installed_launcher(self):
        if self.directory != DEFAULT_STATE.expanduser().absolute() or self.cmux.executable != "cmux":
            return None
        home = Path.home()
        runtime_root = home / ".local/share/starfleet-ai/hailing-frequencies"
        script = Path(__file__).resolve()
        try:
            if script.parent.parent != runtime_root.resolve():
                return None
            digest_input = b"".join(name.encode() + b"\0" + (script.parent / name).read_bytes()
                                    for name in RUNTIME_SOURCE_NAMES)
            for skill_name in RUNTIME_SKILL_NAMES:
                skill = script.parent / skill_name
                if skill.is_file():
                    digest_input += skill_name.encode() + b"\0" + skill.read_bytes()
            digest = hashlib.sha256(digest_input).hexdigest()
            metadata = json.loads(script.with_name("installation.json").read_text())
            if script.parent.name != digest or metadata.get("sha256") != digest:
                return None
            python = metadata.get("python")
            if not isinstance(python, str) or not python:
                return None
            launcher = home / ".local/bin/hailing-frequencies"
            if launcher.is_symlink() or not launcher.is_file() or not os.access(launcher, os.X_OK):
                return None
            expected = ("#!/bin/sh\n" + INSTALL_MARKER + "\nexec " + shlex.quote(python) + " " +
                        shlex.quote(str(script)) + ' "$@"\n')
            return launcher if launcher.read_text() == expected else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def compact_inbox_messages(self, selected):
        return [{
            "id": identity(message["id"]),
            "sender_name": message["sender_name"],
            "sender_surface_id": identity(message["sender_surface_id"]),
            "sender_workspace_id": identity(message["sender_workspace_id"]),
            "created_at": message["created_at"],
            "in_reply_to": identity(message["in_reply_to"]) if message.get("in_reply_to") else None,
            "body": message["body"],
        } for message in selected]

    def inbox_commands(self, target, message_ids):
        launcher = self.installed_launcher()
        batches = (message_ids[offset:offset + INBOX_MESSAGE_LIMIT]
                   for offset in range(0, len(message_ids), INBOX_MESSAGE_LIMIT))
        return "\n".join(self._inbox_command(target, batch, launcher) for batch in batches)

    def consume_inbox(self, session_id, message_ids, surface_id, output, details=False):
        require(isinstance(message_ids, list) and 0 < len(message_ids) <= INBOX_MESSAGE_LIMIT,
                f"Select between 1 and {INBOX_MESSAGE_LIMIT} message IDs")
        normalized_ids = [identity(message_id) for message_id in message_ids]
        require(len(set(normalized_ids)) == len(normalized_ids), "Duplicate message IDs are not allowed")
        target = self.enrolled_target(session_id, surface_id)
        self.cmux.binding(target)
        rows = self.cmux.messages(target)
        require(len(rows) < SCAN_LIMIT, "Inbox scan saturated; no messages output or acknowledged")
        selected = self.select_inbox_messages(target, normalized_ids, rows)
        rendered = selected if details else self.compact_inbox_messages(selected)
        payload = json.dumps({"messages": rendered}, indent=2, allow_nan=False) + "\n"
        require(len(payload.encode("utf-8")) <= INBOX_OUTPUT_LIMIT,
                "Inbox output exceeds 64 KiB; select a smaller batch. No messages acknowledged")
        written = output.write(payload)
        require(written == len(payload), "Inbox output was incomplete; no messages acknowledged")
        output.flush()

        current_target = self.enrolled_target(session_id, surface_id)
        require(current_target == target, "Enrollment changed; no messages acknowledged")
        self.cmux.binding(current_target)
        current_rows = self.cmux.messages(current_target)
        require(len(current_rows) < SCAN_LIMIT, "Inbox scan saturated; no messages acknowledged")
        current = self.select_inbox_messages(current_target, normalized_ids, current_rows)
        for previous, latest in zip(selected, current):
            stable_fields = ("id", "body", "sender_surface_id", "sender_workspace_id", "sender_name",
                             "recipient_surface_id", "recipient_workspace_id", "created_at", "in_reply_to")
            require(all(previous.get(field) == latest.get(field) for field in stable_fields),
                    "Message changed during output; no messages acknowledged")
        self.cmux.binding(current_target)

        acknowledged_at = self.clock()
        with contextlib.closing(self.database()) as db:
            with db:
                for message_id in normalized_ids:
                    db.execute("INSERT INTO acknowledgements VALUES (?,?,?,?) "
                               "ON CONFLICT(session,message) DO UPDATE SET at=excluded.at,evidence='inbox'",
                               (target["session"], message_id, acknowledged_at, "inbox"))
        return {"session": target["session"], "messages": normalized_ids, "acknowledged": True,
                "evidence": "inbox", "output_bytes": len(payload.encode("utf-8"))}

    def reply_acknowledgements(self, target, sources, messages):
        by_reference = {}
        for message in messages:
            try:
                reference = identity(message.get("in_reply_to"))
            except (ValueError, TypeError, AttributeError):
                continue
            by_reference.setdefault(reference, []).append(message)

        acknowledged = set()
        for source in sources:
            source_id = identity(source["id"])
            sender_surface = str(source.get("sender_surface_id") or "").upper()
            sender_workspace = str(source.get("sender_workspace_id") or "").upper()
            if not sender_surface or not sender_workspace:
                continue
            for reply in by_reference.get(source_id, []):
                created = reply.get("created_at")
                if type(created) not in (int, float) or not math.isfinite(created) or \
                        created <= source["created_at"] or created < target["started_at"]:
                    continue
                if str(reply.get("sender_surface_id") or "").upper() != target["surface"] or \
                        str(reply.get("sender_workspace_id") or "").upper() != target["workspace"] or \
                        str(reply.get("recipient_surface_id") or "").upper() != sender_surface or \
                        str(reply.get("recipient_workspace_id") or "").upper() != sender_workspace:
                    continue
                acknowledged.add(source_id)
                break
        return acknowledged

    def record_acknowledgements(self, db, target, message_ids, evidence, now):
        with db:
            for message_id in message_ids:
                db.execute("INSERT OR IGNORE INTO acknowledgements VALUES (?,?,?,?)",
                           (target["session"], message_id, now, evidence))

    def record_explicit_acknowledgement(self, db, target, message_id, now):
        with db:
            db.execute("INSERT INTO acknowledgements VALUES (?,?,?,?) "
                       "ON CONFLICT(session,message) DO UPDATE SET at=excluded.at,evidence='explicit'",
                       (target["session"], message_id, now, "explicit"))

    def target_snapshot(self, sources, acknowledged, now, remind_after):
        native_unread = [message for message in sources if
                         message.get("state") in ("queued", "delivered") and message.get("read_at") is None]
        acknowledged_ids = {identity(message["id"]) for message in sources
                            if identity(message["id"]) in acknowledged}
        overdue = [message for message in native_unread if identity(message["id"]) not in acknowledged and
                   now - message["created_at"] >= remind_after]
        return {
            "native_unread": len(native_unread),
            "acknowledged": len(acknowledged_ids),
            "overdue": len(overdue),
            "oldest_seconds": round(max((now - message["created_at"] for message in overdue), default=0)),
            "overdue_messages": overdue,
            "acknowledged_ids": acknowledged_ids,
        }

    def acknowledgement_command(self, target, message_id):
        return shlex.join([sys.executable, str(Path(__file__).resolve()),
                           "--state-dir", str(self.directory), "--cmux", self.cmux.executable,
                           "ack", "--session", target["session"], "--message", identity(message_id)])

    def acknowledge(self, session_id, message_id, surface_id):
        config = self.config()
        session = identity(session_id)
        source_id = identity(message_id)
        require(surface_id, "CMUX_SURFACE_ID is required")
        surface = identity(surface_id)
        target = next((item for item in config["targets"] if item["session"] == session), None)
        require(target is not None, "Session is not enrolled")
        require(surface == target["surface"], "CMUX_SURFACE_ID does not match the enrolled surface")
        self.cmux.binding(target)
        rows = self.cmux.messages(target)
        require(len(rows) < SCAN_LIMIT, "Inbox scan saturated; no acknowledgment recorded")
        sources = self.source_messages(target, rows)
        matches = [message for message in sources if identity(message["id"]) == source_id]
        require(len(matches) == 1, "Message is missing, ambiguous, or outside this session's enrollment")
        self.cmux.binding(target)
        with contextlib.closing(self.database()) as db:
            self.record_explicit_acknowledgement(db, target, source_id, self.clock())
        return {"session": session, "message": source_id, "acknowledged": True,
                "task_completed": False, "evidence": "explicit"}

    def reserve(self, db, target, messages, kind, now):
        token = uuid.uuid4().hex
        with db:
            for message in messages:
                db.execute("INSERT INTO attempts VALUES (?,?,?,?,?)",
                           (target["session"], message["id"], kind, now, "uncertain"))
            db.execute("INSERT INTO batches VALUES (?,?,?,?,?)", (token, target["session"], now, "uncertain", kind))
        return token

    def deliver(self, db, target, messages, kind, destination, now, remind_after):
        # Recheck source and reply evidence after planning; the recipient may have
        # acknowledged, read, or answered the message while the scan was running.
        current_rows = self.cmux.messages(target)
        current_sources = self.source_messages(target, current_rows)
        reply_ids = self.reply_acknowledgements(target, current_sources, current_rows)
        if reply_ids:
            try:
                self.cmux.binding(target)
            except (ValueError, OSError, subprocess.SubprocessError):
                reply_ids = set()
        self.record_acknowledgements(db, target, reply_ids, "reply", now)
        acknowledged = {row[0] for row in db.execute(
            "SELECT message FROM acknowledgements WHERE session=?", (target["session"],))}
        snapshot = self.target_snapshot(current_sources, acknowledged, now, remind_after)
        current = {identity(m["id"]) for m in current_sources if
                   m.get("state") in ("queued", "delivered") and m.get("read_at") is None and
                   identity(m["id"]) not in acknowledged}
        planned_ids = {identity(m["id"]) for m in messages}
        messages = [m for m in messages if identity(m["id"]) in current]
        if not messages:
            outcome = "already_acknowledged" if planned_ids & acknowledged else "already_read"
            return {"kind": kind, "outcome": outcome, "ids": [], "_snapshot": snapshot}
        token = self.reserve(db, target, messages, kind, now)
        ids = [m["id"] for m in messages]
        if kind == "escalation":
            text = (f"Hailing Frequencies escalation [{token}]: recipient {target['session']}, overdue IDs "
                    f"{', '.join(ids)}. Investigate source metadata and current binding, then contact the recipient. "
                    "Do not read or consume messages as the recipient.")
        else:
            text = ("CMUX still records these messages as unread. Review them at your next safe checkpoint; "
                    "acknowledgment resolves only this queue alert, not task completion or authority.\n"
                    f"{self.inbox_commands(target, ids)}")
        try:
            self.cmux.submit(destination, text)
            outcome, detail = "submitted", kind
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            # Even a timeout may have delivered. Retain the intent; never automatically replay.
            outcome, detail = "uncertain", str(exc)[:500]
        with db:
            db.execute("UPDATE batches SET outcome=?,detail=? WHERE token=?", (outcome, detail, token))
            for mid in ids:
                db.execute("UPDATE attempts SET outcome=? WHERE session=? AND message=? AND kind=?",
                           (outcome, target["session"], mid, kind))
        return {"token": token, "kind": kind, "outcome": outcome, "detail": detail,
                "ids": ids, "_snapshot": snapshot}

    def scan(self, send=False, only_sessions=None):
        now = self.clock()
        config = self.config()
        if only_sessions is not None:
            only_sessions = {identity(s) for s in only_sessions}
            require(only_sessions <= {t["session"] for t in config["targets"]},
                    "Send scope contains a session that is not enrolled")
        messages_by_id = {}
        for target in config["targets"]:
            rows = self.cmux.messages(target)
            require(len(rows) < SCAN_LIMIT, "Inbox scan saturated")
            messages_by_id.update((row["id"], row) for row in rows)
        messages = list(messages_by_id.values())
        results = []
        with contextlib.closing(self.database()) as db:
            for target in config["targets"]:
                sending = send and (only_sessions is None or target["session"] in only_sessions)
                sources = self.source_messages(target, messages)
                reply_ids = self.reply_acknowledgements(target, sources, messages)
                binding_checked = False
                reply_hold = None
                if reply_ids:
                    binding_checked = True
                    try:
                        self.cmux.binding(target)
                    except (ValueError, OSError, subprocess.SubprocessError) as exc:
                        reply_hold = str(exc)
                        reply_ids = set()
                self.record_acknowledgements(db, target, reply_ids, "reply", now)
                acknowledged = {row[0] for row in db.execute(
                    "SELECT message FROM acknowledgements WHERE session=?", (target["session"],))}
                snapshot = self.target_snapshot(sources, acknowledged, now, config["remind_after"])
                overdue = snapshot["overdue_messages"]
                result = {"label": target["label"], "session": target["session"],
                          "native_unread": snapshot["native_unread"],
                          "acknowledged": snapshot["acknowledged"],
                          "overdue": snapshot["overdue"],
                          "sending_enabled": sending,
                          "oldest_seconds": snapshot["oldest_seconds"], "attempts": []}
                if reply_hold:
                    result["acknowledgment_hold"] = reply_hold
                results.append(result)
                if not overdue:
                    continue
                if reply_hold:
                    result["hold"] = reply_hold
                    continue
                if not binding_checked:
                    try:
                        self.cmux.binding(target)
                    except (ValueError, OSError, subprocess.SubprocessError) as exc:
                        result["hold"] = str(exc)
                        continue
                for kind, age in (("reminder", config["remind_after"]), ("escalation", config["escalate_after"])):
                    eligible = [m for m in overdue if now - m["created_at"] >= age and not db.execute(
                        "SELECT 1 FROM attempts WHERE session=? AND message=? AND kind=?",
                        (target["session"], m["id"], kind)).fetchone()]
                    if not eligible:
                        continue
                    last = db.execute("SELECT MAX(at) FROM attempts WHERE session=? AND kind=?",
                                      (target["session"], kind)).fetchone()[0]
                    if last is not None and now - last < config["cooldown"]:
                        result[kind + "_hold"] = "cooldown"
                        continue
                    destination = target
                    if kind == "escalation":
                        destination = next((t for t in config["targets"] if t["session"] == config.get("coordinator")), None)
                        if destination is None or destination["session"] == target["session"]:
                            result["escalation_hold"] = "No distinct enrolled coordinator; see this local report"
                            continue
                        if only_sessions is not None and destination["session"] not in only_sessions:
                            result["escalation_hold"] = "Coordinator is outside the active send scope"
                            continue
                        try:
                            self.cmux.binding(destination)
                        except (ValueError, OSError, subprocess.SubprocessError) as exc:
                            result["escalation_hold"] = str(exc)
                            continue
                    if sending:
                        attempt = self.deliver(db, target, eligible, kind, destination, now,
                                               config["remind_after"])
                        fresh = attempt.pop("_snapshot")
                        result.update({key: fresh[key] for key in
                                       ("native_unread", "acknowledged", "overdue", "oldest_seconds")})
                        overdue = fresh["overdue_messages"]
                        result["attempts"].append(attempt)
                    else:
                        result["attempts"].append({"kind": kind, "outcome": "would_submit", "ids": [m["id"] for m in eligible]})
        report = {"at": now, "completed_at": self.clock(), "mode": "send" if send else "observe",
                  "sending_sessions": sorted(only_sessions) if only_sessions is not None else None,
                  "targets": results}
        atomic_json(self.directory / "last-scan.json", report)
        return report


def serve_iteration(app, send=False, only_sessions=None):
    try:
        with app.locked():
            report = app.scan(send, only_sessions)
    except LockBusyError:
        report = {"at": app.clock(), "skipped": "lock_busy"}
    except (ValueError, OSError, subprocess.SubprocessError, sqlite3.Error) as exc:
        error = {"at": app.clock(), "error": str(exc)[:500], "sending_paused": True}
        atomic_json(app.directory / "last-error.json", error)
        report = error
    try:
        from temporal_beacon import TemporalBeacon
        report["temporal_beacon"] = TemporalBeacon(app, clock=app.clock).tick(send, only_sessions)
        (app.directory / "timer-last-error.json").unlink(missing_ok=True)
    except (ValueError, OSError, subprocess.SubprocessError, sqlite3.Error) as exc:
        error = {"at": app.clock(), "error": str(exc)[:500], "sending_paused": True}
        atomic_json(app.directory / "timer-last-error.json", error)
        report["temporal_beacon"] = error
    report["at"] = app.clock()
    return report


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    p.add_argument("--cmux", default="cmux")
    sub = p.add_subparsers(dest="command", required=True)
    guide = sub.add_parser("guide", help="Print the maintained safe-setup guide")
    guide.add_argument("--print", action="store_true", help="Print the guide (also the default behavior)")
    init = sub.add_parser("init", help="Create private observe-only configuration")
    init.add_argument("--remind-after", type=float, default=300, help="Seconds (default 300)")
    init.add_argument("--escalate-after", type=float, default=900)
    init.add_argument("--cooldown", type=float, default=600)
    init.add_argument("--interval", type=float, default=30)
    enroll = sub.add_parser("enroll", help="Explicitly enroll one exact currently bound session")
    enroll.add_argument("--surface", required=True)
    enroll.add_argument("--session", required=True)
    enroll.add_argument("--harness", choices=("claude", "codex"), required=True)
    enroll.add_argument("--label", required=True)
    enroll.add_argument("--include-existing", action="store_true", help="Include this session's existing messages")
    enroll.add_argument("--coordinator", action="store_true", help="Receive escalation prompts")
    remove = sub.add_parser("unenroll")
    remove.add_argument("--session", required=True)
    for name in ("scan", "serve", "supervise"):
        command = sub.add_parser(name)
        command.add_argument("--send", action="store_true", help="Enable guarded prompt submission; default observe only")
        command.add_argument("--only-session", action="append", help="Limit sends to this enrolled UUID; repeat for canaries")
    sub.add_parser("status")
    sub.add_parser("health")
    watcher = sub.add_parser("health-watch")
    watcher.add_argument("--no-notify", action="store_true")
    service = sub.add_parser("health-service")
    service.add_argument("action", choices=("install", "uninstall", "status"))
    inbox = sub.add_parser("inbox", help="Output exact enrolled messages and acknowledge queue alerts")
    inbox.add_argument("--session", required=True)
    inbox.add_argument("--message", required=True, action="append")
    inbox.add_argument("--details", action="store_true", help="Include the full native message records")
    ack = sub.add_parser("ack", help="Acknowledge your own queue alerts, without completing the task")
    ack.add_argument("--session", required=True)
    ack.add_argument("--message", required=True, action="append")
    attention = sub.add_parser("attention-once", help="Submit one deduplicated prompt to your own enrolled session")
    attention.add_argument("--session", required=True)
    attention.add_argument("--event-key", required=True)
    temporal = sub.add_parser("temporal", help="Manage durable exact-session wake timers")
    temporal.add_argument("--state-dir", type=Path, default=argparse.SUPPRESS)
    temporal.add_argument("--cmux", default=argparse.SUPPRESS)
    temporal_sub = temporal.add_subparsers(dest="temporal_command", required=True)
    set_timer = temporal_sub.add_parser("set", help="Schedule one continuation timer")
    schedule = set_timer.add_mutually_exclusive_group(required=True)
    schedule.add_argument("--after")
    schedule.add_argument("--after-idle", help="one continuation after confirmed native inactivity; activity resets the deadline")
    schedule.add_argument("--at")
    schedule.add_argument("--every")
    set_timer.add_argument("--until")
    set_timer.add_argument("--message", required=True, help="Continuation to retrieve when woken")
    set_timer.add_argument("--request-id")
    set_timer.add_argument("--replace-key")
    temporal_sub.add_parser("list", help="List this exact owner's timers")
    inspect = temporal_sub.add_parser("inspect", help="Inspect a timer and its occurrence history")
    inspect.add_argument("timer_id")
    cancel = temporal_sub.add_parser("cancel", help="Cancel a timer and any held occurrence")
    cancel.add_argument("timer_id")
    temporal_sub.add_parser("receive", help="Output the current continuation and record its receipt")
    temporal_sub.choices["receive"].add_argument("--timer-id")
    temporal_sub.add_parser("doctor", help="Diagnose timer worker and delivery state")
    return p


def main(argv=None):
    os.umask(0o077)
    if argv is None:
        argv = sys.argv[1:]
        if Path(sys.argv[0]).name == "temporal-beacon":
            argv = ["temporal", *argv]
    args = parser().parse_args(argv)
    if args.command == "guide":
        sys.stdout.write(Path(__file__).with_name("guide.md").read_text(encoding="utf-8"))
        return None
    app = Monitor(args.state_dir, Cmux(args.cmux))
    if args.command == "attention-once":
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        try:
            body = stream.read(ATTENTION_MESSAGE_LIMIT + 1)
        except OSError:
            return app.attention_result("rejected", False, "Could not read the message from stdin", args.event_key)
        if isinstance(body, str):
            body = body.encode("utf-8")
        if not isinstance(body, bytes) or len(body) > ATTENTION_MESSAGE_LIMIT:
            return app.attention_result("rejected", False, "Message must be at most 4096 UTF-8 bytes", args.event_key)
        try:
            message = body.decode("utf-8")
        except UnicodeDecodeError:
            return app.attention_result("rejected", False, "Message must be valid UTF-8", args.event_key)
        if not message.strip() or "\x00" in message:
            return app.attention_result("rejected", False, "Message must be nonempty UTF-8 text", args.event_key)
        config_path = app.directory / "config.json"
        if app.directory.is_symlink() or not app.directory.is_dir() or not config_path.is_file():
            return app.attention_result("unavailable", False, "Hailing Frequencies is not initialized", args.event_key)
        try:
            with app.locked():
                return app.attention_once(args.session, args.event_key, body,
                                          os.environ.get("CMUX_SURFACE_ID"))
        except LockBusyError:
            return app.attention_result("held", False, "Hailing Frequencies state is busy", args.event_key)
    if args.command == "init":
        require(not app.directory.is_symlink(), "Refusing symlink state directory")
        app.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with app.locked():
            require(not (app.directory / "config.json").exists(), "Configuration already exists")
            config = {"version": 1, "targets": [], "coordinator": None,
                      **{k: getattr(args, k) for k in ("remind_after", "escalate_after", "cooldown", "interval")}}
            # Validate without leaving an invalid initialized configuration.
            atomic_json(app.directory / "config.json", config)
            try:
                app.config()
            except ValueError:
                (app.directory / "config.json").unlink()
                raise
        return {"initialized": str(app.directory), "sending_enabled": False}
    if args.command in ("health", "health-watch", "health-service", "supervise"):
        import service
        if args.command == "health":
            return service.health(app.directory)
        if args.command == "health-watch":
            return service.watchdog(app.directory, not args.no_notify)
        if args.command == "health-service":
            return service.health_service(app.directory, args.action)
        app.config()
        return service.supervise(app.directory, args.cmux, args.send, args.only_session)
    if args.command == "status":
        import service
        health_report = service.health(app.directory)
        with app.locked():
            config = app.config()
            last = app.directory / "last-scan.json"
            with contextlib.closing(app.database()) as db:
                attempts = [dict(zip(("token", "session", "at", "outcome", "detail"), row)) for row in
                            db.execute("SELECT * FROM batches ORDER BY at DESC LIMIT 50")]
            return {"config": config, "last_scan": json.loads(last.read_text()) if last.exists() else None,
                    "health": health_report, "attempts": attempts}
    if args.command == "temporal":
        from temporal_beacon import TemporalBeacon
        beacon = TemporalBeacon(app)
        if args.temporal_command == "doctor":
            return beacon.doctor()
        target, owner_key, _worktree = beacon.owner()
        if args.temporal_command == "set":
            return beacon.set(args.after, args.at, args.every, args.until, args.message,
                              args.request_id, args.replace_key, after_idle=args.after_idle)
        if args.temporal_command == "list":
            return beacon.list(owner_key)
        if args.temporal_command == "inspect":
            return beacon.inspect(owner_key, args.timer_id)
        if args.temporal_command == "cancel":
            return beacon.cancel(owner_key, args.timer_id)
        if args.temporal_command == "receive":
            receipt = beacon.receive(owner_key, sys.stdout, args.timer_id)
            print(json.dumps(receipt), file=sys.stderr)
            return None
    with app.locked(timeout=30 if args.command in ("ack", "inbox") else 0):
        config = app.config()
        if args.command == "inbox":
            try:
                app.consume_inbox(args.session, args.message, os.environ.get("CMUX_SURFACE_ID"), sys.stdout,
                                  details=args.details)
            except BrokenPipeError as exc:
                try:
                    sys.stdout.close()
                except OSError:
                    pass
                sys.stdout = open(os.devnull, "w")
                raise ValueError("Inbox output failed; no messages were acknowledged") from exc
            return None
        if args.command == "ack":
            return {"acknowledgments": [app.acknowledge(args.session, mid, os.environ.get("CMUX_SURFACE_ID"))
                                         for mid in args.message]}
        if args.command == "enroll":
            surface, session = identity(args.surface), identity(args.session)
            store = json.loads((app.cmux.hook_directory / (args.harness + "-hook-sessions.json")).read_text())
            record = next((v for k, v in store["sessions"].items() if k.upper() == session), {})
            target = {"surface": surface, "workspace": identity(record["workspaceId"]), "session": session,
                      "harness": args.harness, "label": args.label, "started_at": record["startedAt"],
                      "cutoff": record["startedAt"] if args.include_existing else app.clock()}
            app.cmux.binding(target)
            existing = next((t for t in config["targets"] if t["session"] == session), None)
            require(existing is None or existing == {**target, "cutoff": existing["cutoff"]},
                    "Session enrollment differs; unenroll explicitly before changing it")
            require(not any(t["surface"] == surface and t["session"] != session for t in config["targets"]),
                    "Surface has another enrolled session; reconcile ownership first")
            if existing is None:
                config["targets"].append(target)
            if args.coordinator:
                config["coordinator"] = session
            atomic_json(app.directory / "config.json", config)
            return {"enrolled": existing or target}
        if args.command == "unenroll":
            session = identity(args.session)
            config["targets"] = [t for t in config["targets"] if t["session"] != session]
            if config.get("coordinator") == session:
                config["coordinator"] = None
            atomic_json(app.directory / "config.json", config)
            return {"unenrolled": session, "receipts_preserved": True}
        if args.command == "scan":
            return app.scan(args.send, args.only_session)
    # Serve owns a different lifetime lock; short configuration/scan transactions remain available.
    lock = app.directory / "serve.lock"
    require(not lock.is_symlink(), "Refusing symlink service lock")
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A monitor already serves this state") from None
        stopping = False

        def stop(_signal, _frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        app.cmux.stopping = lambda: stopping
        while not stopping:
            report = serve_iteration(app, args.send, args.only_session)
            print(json.dumps(report), flush=True)
            deadline = time.monotonic() + app.config()["interval"]
            while not stopping and time.monotonic() < deadline:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
    return {"stopped": True}


if __name__ == "__main__":
    try:
        result = main()
        if result is not None:
            print(json.dumps(result, indent=2))
            if result.get("outcome") == "rejected" and "event_key" in result:
                sys.exit(2)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        sys.exit(1)
