#!/usr/bin/env python3
"""Small local reservation queue for agent-owned CPU work."""

import argparse
import contextlib
import fcntl
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

import host
import policy


DEFAULT_STATE = Path.home() / ".local/state/starfleet-ai/warp-drive"
DEFAULT_HAILING_STATE = Path.home() / ".local/state/starfleet-ai/hailing-frequencies"
HELPER_HEARTBEAT_SECONDS = 10
HELPER_STALE_SECONDS = 30
SCHEDULER_INTERVAL_SECONDS = 2
WORKTREE_IDENTITY_VERSION = 1
NOT_PROVIDED = object()
ATTACH_RECOVERY_HELP = (
    "Recovery, not observation. Attaching to waiting work can replace its current helper in foreground or detached mode. "
    "Foreground attach starts the command in this CLI, and redirected stdout can lose its output. "
    "With --detach, command output is appended to the private request log. Use status or inspect to observe."
)


class WarpError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise WarpError(message)


def now():
    return time.time()


def json_text(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def owner_from_args(args):
    harness = getattr(args, "harness", None)
    session = getattr(args, "session", None)
    surface = getattr(args, "surface", None)
    return canonical_owner(harness, session, surface)


def canonical_owner(harness, session, surface):
    require(harness in ("claude", "codex"), "Specify --harness claude|codex")
    require(session and surface, "Specify the enrolled owner --session and --surface")
    return {"harness": harness, "session": canonical_uuid(session, "--session"),
            "surface": canonical_uuid(surface, "--surface")}


def canonical_uuid(value, name):
    try:
        return str(uuid.UUID(str(value))).upper()
    except (ValueError, AttributeError, TypeError) as exc:
        raise WarpError(f"{name} must be an observed UUID") from exc


@contextlib.contextmanager
def immediate_transaction(db):
    """Serialize a state transition before its first read without committing a caller's transaction."""
    if db.in_transaction:
        yield
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.rollback()
        raise
    else:
        db.commit()


def retry_sqlite_busy(operation):
    while True:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not any(word in str(exc).lower() for word in ("locked", "busy")):
                raise
            time.sleep(0.25)


def safe_state_dir(path):
    path = Path(path).expanduser().absolute()
    require(not path.is_symlink(), f"Refusing symlink state directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def connect(state_dir):
    state_dir = safe_state_dir(state_dir)
    path = state_dir / "warp-drive.sqlite3"
    require(not path.is_symlink(), f"Refusing symlink database: {path}")
    db = sqlite3.connect(path, timeout=10)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS worktrees (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            device INTEGER NOT NULL,
            inode INTEGER NOT NULL,
            order_position INTEGER NOT NULL,
            served_round INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            UNIQUE(path, device, inode)
        );
        CREATE TABLE IF NOT EXISTS reservations (
            id TEXT PRIMARY KEY,
            request_key TEXT NOT NULL UNIQUE,
            content_sha256 TEXT NOT NULL,
            sequence INTEGER NOT NULL UNIQUE,
            worktree_id TEXT NOT NULL REFERENCES worktrees(id),
            repo TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            owner_harness TEXT NOT NULL,
            owner_session TEXT NOT NULL,
            owner_surface TEXT NOT NULL,
            size TEXT NOT NULL CHECK(size IN ('light','heavy')),
            family TEXT NOT NULL,
            workers INTEGER NOT NULL,
            expected_runtime REAL NOT NULL,
            command_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('waiting','reserved','finished','cancelled')),
            outcome TEXT,
            created_at REAL NOT NULL,
            eligible_at REAL,
            credit_seconds REAL NOT NULL DEFAULT 0,
            credit_started_at REAL,
            helper_id TEXT,
            helper_generation INTEGER NOT NULL DEFAULT 0,
            heartbeat_at REAL,
            reservation_claim TEXT,
            claim_helper_id TEXT,
            claim_generation INTEGER,
            granted_at REAL,
            started_at REAL,
            launch_intent_at REAL,
            ended_at REAL,
            exit_code INTEGER,
            lease_active INTEGER NOT NULL DEFAULT 0,
            wait_reason TEXT,
            priority INTEGER NOT NULL DEFAULT 0,
            priority_actor TEXT,
            priority_reason TEXT,
            wake_status TEXT,
            wake_detail TEXT
        );
        CREATE INDEX IF NOT EXISTS reservations_waiting ON reservations(state, eligible_at, sequence);
        CREATE INDEX IF NOT EXISTS reservations_worktree ON reservations(worktree_id, state, priority, sequence);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at REAL NOT NULL,
            reservation_id TEXT,
            kind TEXT NOT NULL,
            data_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS events_reservation ON events(reservation_id, id);
        CREATE TABLE IF NOT EXISTS host_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at REAL NOT NULL,
            logical_cpus INTEGER NOT NULL,
            cpu_busy REAL,
            memory_pressure TEXT,
            swap_used_mb REAL,
            thermal_state TEXT,
            availability_json TEXT NOT NULL,
            source TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS host_samples_at ON host_samples(at);
        CREATE TABLE IF NOT EXISTS incidents (
            id TEXT PRIMARY KEY,
            condition_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            opened_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            fingerprint TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            notification_status TEXT,
            notification_detail TEXT,
            notification_at REAL,
            resolved_at REAL
        );
    """)
    defaults = {
        "schema_version": 1,
        "target_cpu_fraction": 0.95,
        "operator": os.environ.get("USER") or getpass.getuser() or "operator",
        "delegates": [],
        "current_round": 1,
        "last_heavy_grant": None,
        "protected_turn_request_id": None,
        "notifications_enabled": True,
        "policy_version": policy.POLICY_VERSION,
        "idle_unadmitted_since": None,
    }
    with db:
        for key, value in defaults.items():
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)",
                       (key, json_text(value)))
    return db


def get_setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def set_setting(db, key, value):
    db.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json_text(value)))


def event(db, kind, reservation_id=None, **data):
    db.execute("INSERT INTO events(at,reservation_id,kind,data_json) VALUES (?,?,?,?)",
               (now(), reservation_id, kind, json_text(data)))


def request_dict(row):
    result = dict(row)
    result["command"] = json.loads(result.pop("command_json"))
    result["owner"] = {"harness": result.pop("owner_harness"),
                       "session": result.pop("owner_session"),
                       "surface": result.pop("owner_surface")}
    return result


def owner_matches(row, owner):
    return (row["owner_harness"] == owner["harness"] and
            row["owner_session"] == owner["session"] and
            row["owner_surface"] == owner["surface"])


def owner_content(owner):
    return {"harness": owner["harness"], "session": owner["session"], "surface": owner["surface"]}


def worktree_identity(db, path):
    resolved = Path(path).expanduser().resolve()
    st = resolved.stat()
    row = db.execute("SELECT * FROM worktrees WHERE path=? AND device=? AND inode=?",
                     (str(resolved), st.st_dev, st.st_ino)).fetchone()
    if row:
        return row["id"]
    next_pos = db.execute("SELECT COALESCE(MAX(order_position),-1)+1 FROM worktrees").fetchone()[0]
    identifier = str(uuid.uuid4())
    db.execute("INSERT INTO worktrees(id,path,device,inode,order_position,created_at) VALUES (?,?,?,?,?,?)",
               (identifier, str(resolved), st.st_dev, st.st_ino, next_pos, now()))
    event(db, "worktree_registered", None, worktree_id=identifier, path=str(resolved),
          identity_version=WORKTREE_IDENTITY_VERSION)
    return identifier


def current_repo(path):
    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=1.5, check=True)
        return str(Path(result.stdout.strip()).resolve())
    except Exception:
        return str(Path(path).expanduser().resolve())


def request_content(args, owner, command):
    worktree_path = str(Path(args.worktree or os.getcwd()).expanduser().resolve())
    repo = str(Path(args.repo).expanduser().resolve()) if args.repo else current_repo(worktree_path)
    values = {
        "repo": repo,
        "worktree_path": worktree_path,
        "size": args.size,
        "family": args.family.strip(),
        "workers": int(args.workers),
        "expected_runtime": float(args.expected_runtime),
        "command": command,
    }
    require(values["size"] in ("light", "heavy"), "--size must be light or heavy")
    require(values["family"], "--family must be nonempty")
    require(values["workers"] > 0, "--workers must be positive")
    require(0 < values["expected_runtime"] <= 7 * 24 * 3600,
            "--expected-runtime must be positive and no more than seven days")
    require(isinstance(command, list) and command and all(isinstance(part, str) for part in command),
            "Provide a command after --")
    return values


def submit_one(db, request_key, content, owner):
    require(request_key and len(request_key) <= 200, "--key must be 1-200 characters")
    worktree_id = worktree_identity(db, content["worktree_path"])
    digest = hashlib.sha256(json_text(content).encode()).hexdigest()
    existing = db.execute("SELECT * FROM reservations WHERE request_key=?", (request_key,)).fetchone()
    if existing:
        require(existing["worktree_id"] == worktree_id,
                f"Request key {request_key!r} belongs to a different checkout identity")
        require(existing["content_sha256"] == digest,
                f"Request key {request_key!r} already names different work")
        require(owner_matches(existing, owner),
                "Request already belongs to another owner; use transfer before attaching")
        return existing["id"], False
    identifier = str(uuid.uuid4())
    sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM reservations").fetchone()[0]
    created = now()
    start_at = created
    db.execute("""INSERT INTO reservations
        (id,request_key,content_sha256,sequence,worktree_id,repo,worktree_path,
         owner_harness,owner_session,owner_surface,size,family,workers,expected_runtime,
         command_json,state,created_at,eligible_at,wait_reason)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'waiting',?,?,?)""",
        (identifier, request_key, digest, sequence, worktree_id, content["repo"],
         content["worktree_path"], owner["harness"], owner["session"], owner["surface"],
         content["size"], content["family"], content["workers"], content["expected_runtime"],
         json_text(content["command"]), created, start_at, "awaiting attached helper"))
    event(db, "submitted", identifier, request_key=request_key, sequence=sequence,
          worktree_id=worktree_id, repo=content["repo"], path=content["worktree_path"],
          size=content["size"], family=content["family"], workers=content["workers"],
          expected_runtime=content["expected_runtime"], command=content["command"],
          policy_version=policy.POLICY_VERSION)
    return identifier, True


def freeze_credit(db, row, at):
    if row["credit_started_at"] is None:
        return
    stop = at
    if row["heartbeat_at"] is not None:
        stop = min(stop, row["heartbeat_at"] + HELPER_STALE_SECONDS)
    start = max(row["credit_started_at"], row["eligible_at"] or at)
    gained = max(0.0, stop - start)
    db.execute("UPDATE reservations SET credit_seconds=credit_seconds+?,credit_started_at=NULL WHERE id=?",
               (gained, row["id"]))


def attach_helper(db, request_id, owner, helper_id):
    at = now()
    with immediate_transaction(db):
        row = db.execute("SELECT * FROM reservations WHERE id=? OR request_key=?",
                         (request_id, request_id)).fetchone()
        require(row is not None, "Reservation not found")
        require(owner_matches(row, owner), "Current owner does not match; transfer this reservation first")
        require(row["state"] not in ("cancelled",), "Reservation is cancelled")
        if row["state"] == "finished":
            return row["id"], row["helper_generation"], "finished"
        freeze_credit(db, row, at)
        generation = row["helper_generation"] + 1
        if row["state"] == "reserved" and row["launch_intent_at"] is not None:
            event(db, "helper_attached_to_running_or_uncertain", row["id"], helper_id=helper_id,
                  existing_claim=row["reservation_claim"], launch_intent_at=row["launch_intent_at"])
            return row["id"], generation - 1, "monitor-only"
        claim = str(uuid.uuid4()) if row["state"] == "reserved" else row["reservation_claim"]
        credit_start = max(at, row["eligible_at"] or at) if row["state"] == "waiting" else None
        cursor = db.execute("""UPDATE reservations SET helper_id=?,helper_generation=?,heartbeat_at=?,
            credit_started_at=?,reservation_claim=COALESCE(?,reservation_claim),
            claim_helper_id=CASE WHEN state='reserved' THEN ? ELSE claim_helper_id END,
            claim_generation=CASE WHEN state='reserved' THEN ? ELSE claim_generation END
            WHERE id=? AND state IN ('waiting','reserved') AND launch_intent_at IS NULL""",
            (helper_id, generation, at, credit_start, claim, helper_id, generation, row["id"]))
        require(cursor.rowcount == 1, "Reservation changed while attaching; retry with current status")
        event(db, "helper_attached", row["id"], helper_id=helper_id, generation=generation,
              preserved_credit_seconds=row["credit_seconds"])
        return row["id"], generation, "attached"


def helper_heartbeat(db, request_id, helper_id, generation):
    at = now()
    with immediate_transaction(db):
        cur = db.execute("UPDATE reservations SET heartbeat_at=? WHERE id=? AND helper_id=? AND helper_generation=? AND state IN ('waiting','reserved')",
                         (at, request_id, helper_id, generation))
        return cur.rowcount == 1


def current_request(db, request_id):
    row = db.execute("SELECT * FROM reservations WHERE id=? OR request_key=?", (request_id, request_id)).fetchone()
    require(row is not None, "Reservation not found")
    return row


def host_sample(db, source="native", fixture=None):
    sample = fixture or host.snapshot()
    at = float(sample.get("at", now()))
    cpus = int(sample.get("logical_cpus") or os.cpu_count() or 1)
    busy = sample.get("cpu_busy")
    pressure = sample.get("memory_pressure")
    swap_used = sample.get("swap_used_mb")
    thermal = sample.get("thermal_state")
    availability = sample.get("availability") or {
        "cpu": busy is not None, "memory_pressure": pressure is not None,
        "swap": swap_used is not None, "thermal": thermal is not None,
    }
    if busy is not None:
        busy = max(0.0, min(1.0, float(busy)))
    with immediate_transaction(db):
        db.execute("INSERT INTO host_samples(at,logical_cpus,cpu_busy,memory_pressure,swap_used_mb,thermal_state,availability_json,source) VALUES (?,?,?,?,?,?,?,?)",
                   (at, cpus, busy, pressure, swap_used, thermal, json_text(availability), source))
        if source == "native":
            event(db, "host_sample", None, logical_cpus=cpus, cpu_busy=busy,
                  memory_pressure=pressure, swap_used_mb=swap_used, thermal_state=thermal,
                  availability=availability)
    return {"at": at, "logical_cpus": cpus, "cpu_busy": busy,
            "memory_pressure": pressure, "swap_used_mb": swap_used,
            "thermal_state": thermal, "availability": availability, "source": source}


def fresh_host_window(db, at):
    rows = db.execute("SELECT * FROM host_samples WHERE at>=? ORDER BY at", (at - 30,)).fetchall()
    if not rows:
        return None, None, False
    latest = rows[-1]
    latest_age = max(0.0, at - latest["at"])
    values = [row["cpu_busy"] for row in rows if row["cpu_busy"] is not None]
    mean_busy = sum(values) / len(values) if values else None
    fresh = latest_age <= 6 and latest["cpu_busy"] is not None
    return latest, mean_busy, fresh


def waiting_credit(row, at):
    accrued = row["credit_seconds"]
    if row["credit_started_at"] is not None:
        start = max(row["credit_started_at"], row["eligible_at"] or at)
        accrued += max(0.0, min(at, (row["heartbeat_at"] or at) + HELPER_STALE_SECONDS) - start)
    return accrued


def set_wait_reason(db, row, reason):
    if row["wait_reason"] == reason:
        return
    db.execute("UPDATE reservations SET wait_reason=? WHERE id=?", (reason, row["id"]))
    event(db, "wait_reason_changed", row["id"], old_reason=row["wait_reason"], reason=reason)


def active_aged_escape_request_ids(db, active):
    escaped = []
    for job in active:
        if job["size"] != "heavy":
            continue
        grant = db.execute("""SELECT data_json FROM events WHERE reservation_id=? AND kind='granted'
            ORDER BY id DESC LIMIT 1""", (job["id"],)).fetchone()
        if grant and json.loads(grant["data_json"]).get("decision") == \
                "aged heavy escape with meaningful host headroom":
            escaped.append(job["id"])
    return sorted(escaped)


def protected_turn_target(db, ready, unserved, *, at, target_cores, projected_cores,
                          current_projected_cores, light_ready, pressure, sample_fresh,
                          last_heavy_grant, active_heavy_cores, active_managed_cores,
                          active_worktree_ids, active_aged_escape_count):
    """Return (target, continuing, settling) from one live admission view."""
    if active_aged_escape_count > 0:
        return None, False, False

    def decide(row, grant_at=last_heavy_grant):
        return policy.admission_decision(
            {"size": row["size"], "workers": row["workers"]},
            target_cores=target_cores, projected_cores=projected_cores,
            light_ready=light_ready, pressure=pressure,
            sample_fresh=sample_fresh, now=at, last_heavy_grant=grant_at,
            heavy_wait_seconds=waiting_credit(row, at),
            active_heavy_cores=active_heavy_cores,
            active_managed_cores=active_managed_cores,
            active_aged_escape_count=active_aged_escape_count)

    unserved_by_id = {row["id"]: row for row in unserved}
    stored_id = get_setting(db, "protected_turn_request_id")
    if stored_id:
        candidate = unserved_by_id.get(stored_id)
        if candidate and candidate["size"] == "heavy" and \
                waiting_credit(candidate, at) >= 120 and \
                candidate["worktree_id"] not in active_worktree_ids:
            allowed, reason, _estimate, should_drain = decide(candidate)
            if not sample_fresh or pressure not in ("normal", None):
                return candidate, True, False
            escape_allowed = allowed and reason == "aged heavy escape with meaningful host headroom"
            heavy_budget = target_cores
            if light_ready:
                heavy_budget -= min(1.0, target_cores / 2)
            settling = (current_projected_cores is not None and
                        current_projected_cores <= heavy_budget / 2 and
                        (projected_cores > heavy_budget / 2 or
                         reason == "heavy startup observation interval"))
            if should_drain or escape_allowed or settling:
                return candidate, True, settling and not (escape_allowed or should_drain)

    aged_candidates = sorted((row for row in unserved if row["size"] == "heavy" and
                              waiting_credit(row, at) >= 120 and
                              row["worktree_id"] not in active_worktree_ids),
                             key=lambda row: (-waiting_credit(row, at), row["order_position"],
                                              row["sequence"], row["id"]))
    for candidate in aged_candidates:
        _allowed, reason, _estimate, should_drain = decide(candidate)
        if not should_drain:
            continue
        serviceable_peer = False
        for peer in unserved:
            if peer["id"] == candidate["id"] or peer["size"] != "heavy" or \
                    peer["worktree_id"] == candidate["worktree_id"] or \
                    peer["worktree_id"] in active_worktree_ids:
                continue
            peer_allowed, peer_reason, _peer_estimate, _peer_drain = decide(peer)
            if peer_allowed:
                serviceable_peer = True
                break
            if peer_reason == "heavy startup observation interval":
                allowed_without_spacing, _reason, _estimate, _drain = decide(peer, grant_at=None)
                if allowed_without_spacing:
                    serviceable_peer = True
                    break
        if not serviceable_peer:
            return candidate, False, False
    return None, False, False


def scheduler_pass(db, sample=None, permit_grants=True):
    if get_setting(db, "policy_version") != policy.POLICY_VERSION:
        set_setting(db, "policy_version", policy.POLICY_VERSION)
    at = now()
    if sample is None:
        sample = host_sample(db)
    latest, mean_busy, fresh = fresh_host_window(db, at)
    if latest is not None:
        cpus = latest["logical_cpus"]
        pressure = latest["memory_pressure"]
        current_busy = latest["cpu_busy"]
    else:
        cpus, pressure, current_busy = os.cpu_count() or 1, None, None
    target_cores = cpus * float(get_setting(db, "target_cpu_fraction") or 0.95)
    sample_busy = max(current_busy or 0.0, mean_busy or 0.0) if mean_busy is not None else current_busy
    observed_cores = (sample_busy or 0.0) * cpus
    active = db.execute("SELECT * FROM reservations WHERE state='reserved' AND lease_active=1").fetchall()
    # Keep new grants projected until a later sample has time to reflect command startup.
    unreflected = 0.0
    for job in active:
        start = job["started_at"] or job["granted_at"]
        if latest is None or start is None or latest["at"] < start + 10:
            unreflected += policy.estimated_cores(job["size"], job["workers"], target_cores)
    projected = observed_cores + unreflected
    ready = db.execute("""SELECT r.*,w.order_position,w.served_round FROM reservations r
        JOIN worktrees w ON w.id=r.worktree_id
        WHERE r.state='waiting' AND r.eligible_at IS NOT NULL AND r.eligible_at<=?
        AND r.heartbeat_at>=? ORDER BY w.order_position,r.sequence""",
        (at, at - HELPER_STALE_SECONDS)).fetchall()
    eligible = []
    for row in ready:
        if row["credit_started_at"] is None:
            db.execute("UPDATE reservations SET credit_started_at=? WHERE id=?", (max(at, row["eligible_at"]), row["id"]))
            row = db.execute("SELECT r.*,w.order_position,w.served_round FROM reservations r JOIN worktrees w ON w.id=r.worktree_id WHERE r.id=?", (row["id"],)).fetchone()
        eligible.append(row)
    if get_setting(db, "current_round") is None:
        set_setting(db, "current_round", 1)
    current_round = int(get_setting(db, "current_round"))
    eligible_worktrees = {row["worktree_id"] for row in eligible}
    unserved = [row for row in eligible if row["served_round"] < current_round]
    if not unserved and eligible:
        current_round += 1
        set_setting(db, "current_round", current_round)
        unserved = eligible
    request_rows = []
    for row in unserved:
        request_rows.append({"row": row, "boosted": bool(row["priority"]),
                             "credit": waiting_credit(row, at),
                             "sequence": row["sequence"],
                             "position": row["order_position"]})
    request_rows.sort(key=lambda x: (-int(x["boosted"]), x["position"], -x["credit"],
                                     x["sequence"], x["row"]["id"]))
    light_ready = any(row["size"] == "light" for row in ready)
    active_heavy_cores = sum(policy.estimated_cores(job["size"], job["workers"], target_cores)
                             for job in active if job["size"] == "heavy")
    active_managed_cores = sum(policy.estimated_cores(job["size"], job["workers"], target_cores)
                               for job in active)
    active_light_jobs = [job for job in active if job["size"] == "light"]
    active_worktree_ids = {job["worktree_id"] for job in active}
    active_aged_escape_count = len(active_aged_escape_request_ids(db, active))
    current_projected = ((current_busy or 0.0) * cpus + unreflected
                         if fresh and current_busy is not None else None)
    stored_target_id = get_setting(db, "protected_turn_request_id")
    protected_target, continuing_protection, settling_protection = protected_turn_target(
        db, ready, unserved, at=at, target_cores=target_cores,
        projected_cores=projected, current_projected_cores=current_projected,
        light_ready=light_ready, pressure=pressure,
        sample_fresh=fresh and mean_busy is not None,
        last_heavy_grant=get_setting(db, "last_heavy_grant"),
        active_heavy_cores=active_heavy_cores,
        active_managed_cores=active_managed_cores,
        active_worktree_ids=active_worktree_ids,
        active_aged_escape_count=active_aged_escape_count)
    if stored_target_id and (protected_target is None or
                             protected_target["id"] != stored_target_id):
        set_setting(db, "protected_turn_request_id", None)
    if continuing_protection:
        target_id = protected_target["id"]
        request_rows.sort(key=lambda item: (0 if item["row"]["id"] == target_id else 1,
                                            -int(item["boosted"]), item["position"],
                                            -item["credit"], item["sequence"],
                                            item["row"]["id"]))
    granted = None
    decision_log = []
    def try_grant(items):
        nonlocal projected
        for item in items:
            row = item["row"]
            if continuing_protection and row["id"] != protected_target["id"]:
                reason = None
                if row["size"] == "heavy":
                    reason = f"heavy backfill held for aged request {protected_target['id']}"
                elif row["size"] == "light" and active_light_jobs:
                    reason = (f"light backfill held for aged request {protected_target['id']} "
                              "while light work is active")
                elif row["size"] == "light" and settling_protection:
                    reason = (f"light backfill held for aged request {protected_target['id']} "
                              "while the observed CPU window settles")
                if reason is not None:
                    estimate = policy.estimated_cores(row["size"], row["workers"], target_cores,
                                                      light_ready=light_ready)
                    set_wait_reason(db, row, reason)
                    decision_log.append({"request_id": row["id"], "reason": reason,
                                         "estimate_cores": estimate, "allowed": False})
                    continue
            allowed, reason, estimate, _draining = policy.admission_decision(
                {"size": row["size"], "workers": row["workers"]},
                target_cores=target_cores, projected_cores=projected,
                light_ready=light_ready, pressure=pressure,
                sample_fresh=fresh and mean_busy is not None, now=at,
                last_heavy_grant=get_setting(db, "last_heavy_grant"),
                heavy_wait_seconds=waiting_credit(row, at),
                active_heavy_cores=active_heavy_cores,
                active_managed_cores=active_managed_cores,
                active_aged_escape_count=active_aged_escape_count)
            if allowed and permit_grants:
                claim = str(uuid.uuid4())
                db.execute("UPDATE reservations SET state='reserved',reservation_claim=?,claim_helper_id=helper_id,claim_generation=helper_generation,granted_at=?,lease_active=1,wait_reason=NULL WHERE id=? AND state='waiting'",
                           (claim, at, row["id"]))
                db.execute("UPDATE worktrees SET served_round=? WHERE id=?",
                           (current_round, row["worktree_id"]))
                if row["size"] == "heavy":
                    set_setting(db, "last_heavy_grant", at)
                event(db, "granted", row["id"], claim=claim, worktree_id=row["worktree_id"],
                      round=current_round, estimated_cores=estimate,
                      policy_version=policy.POLICY_VERSION, cpu_busy=current_busy,
                      cpu_30s_mean=mean_busy, projected_cores=projected,
                      target_cores=target_cores, decision=reason)
                projected += estimate
                return row["id"]
            set_wait_reason(db, row, reason)
            decision_log.append({"request_id": row["id"], "reason": reason,
                                 "estimate_cores": estimate, "allowed": allowed})
        return None

    granted = try_grant(request_rows)
    if granted is not None:
        if continuing_protection and protected_target is not None and granted != protected_target["id"]:
            # A light backfill may use its one available slot without erasing the aged turn.
            reason = (f"settling observed CPU window for aged request {protected_target['id']}"
                      if settling_protection else
                      f"draining managed work for aged request {protected_target['id']}")
            set_wait_reason(db, protected_target, reason)
        else:
            protected_target = None
            set_setting(db, "protected_turn_request_id", None)
    if granted is None and protected_target is not None:
        target_id = protected_target["id"]
        set_setting(db, "protected_turn_request_id", target_id)
        target_reason = (f"settling observed CPU window for aged request {target_id}"
                         if settling_protection else
                         f"draining managed work for aged request {target_id}")
        set_wait_reason(db, protected_target, target_reason)
        for row in eligible:
            if row["size"] != "heavy" or row["id"] == target_id:
                continue
            allowed, reason, estimate, _draining = policy.admission_decision(
                {"size": row["size"], "workers": row["workers"]},
                target_cores=target_cores, projected_cores=projected,
                light_ready=light_ready, pressure=pressure,
                sample_fresh=fresh and mean_busy is not None, now=at,
                last_heavy_grant=get_setting(db, "last_heavy_grant"),
                heavy_wait_seconds=waiting_credit(row, at),
                active_heavy_cores=active_heavy_cores,
                active_managed_cores=active_managed_cores,
                active_aged_escape_count=active_aged_escape_count)
            if row["size"] == "heavy":
                reason = f"heavy backfill held for aged request {target_id}"
            set_wait_reason(db, row, reason)
            if row["size"] == "heavy" and allowed:
                decision_log.append({"request_id": row["id"], "reason": reason,
                                     "estimate_cores": estimate, "allowed": False})
        light_backfill = [row for row in eligible if row["size"] == "light" and
                          row["served_round"] >= current_round]
        if settling_protection:
            for row in light_backfill:
                set_wait_reason(db, row,
                    f"light backfill held for aged request {target_id} while the observed CPU window settles")
            light_backfill = []
        elif active_light_jobs:
            for row in light_backfill:
                set_wait_reason(db, row,
                    f"light backfill held for aged request {target_id} while light work is active")
            light_backfill = []
        light_items = [{"row": row, "boosted": bool(row["priority"]),
                        "credit": waiting_credit(row, at), "sequence": row["sequence"],
                        "position": row["order_position"]} for row in light_backfill]
        light_items.sort(key=lambda item: (-int(item["boosted"]), item["position"],
                                           -item["credit"], item["sequence"],
                                           item["row"]["id"]))
        granted = try_grant(light_items)
    if (granted is None and protected_target is None and request_rows and
            any(row["served_round"] >= current_round for row in ready)):
        # A worktree skipped for capacity must not pin the round ahead of already-served peers.
        current_round += 1
        set_setting(db, "current_round", current_round)
        event(db, "round_advanced_after_capacity_skip", None, round=current_round,
              skipped_request_ids=[item["row"]["id"] for item in request_rows])
        backfill = [{"row": row, "boosted": bool(row["priority"]),
                     "credit": waiting_credit(row, at), "sequence": row["sequence"],
                     "position": row["order_position"]} for row in eligible]
        backfill.sort(key=lambda item: (-int(item["boosted"]), item["position"],
                                        -item["credit"], item["sequence"],
                                        item["row"]["id"]))
        granted = try_grant(backfill)
    headroom_available = False
    if fresh and mean_busy is not None and pressure in (None, "normal"):
        for row in ready:
            estimate = policy.estimated_cores(row["size"], row["workers"], target_cores,
                                              light_ready=light_ready)
            budget = target_cores
            if row["size"] == "heavy" and light_ready:
                budget -= min(1.0, target_cores / 2)
            if projected + estimate <= budget:
                headroom_available = True
                break
    stalled_candidates = []
    if (granted is None and headroom_available and not active and
            protected_target is None):
        for row in ready:
            allowed, reason, estimate, _draining = policy.admission_decision(
                {"size": row["size"], "workers": row["workers"]},
                target_cores=target_cores, projected_cores=projected,
                light_ready=light_ready, pressure=pressure,
                sample_fresh=fresh and mean_busy is not None, now=at,
                last_heavy_grant=get_setting(db, "last_heavy_grant"),
                heavy_wait_seconds=waiting_credit(row, at),
                active_heavy_cores=active_heavy_cores,
                active_managed_cores=active_managed_cores,
                active_aged_escape_count=active_aged_escape_count)
            stalled_candidates.append({"request_id": row["id"],
                "estimate_cores": estimate,
                "reason": ("policy permitted admission but scheduler did not grant"
                           if allowed else reason)})
    incident = db.execute("SELECT * FROM incidents WHERE condition_key='ready-demand-unadmitted' AND status='open'").fetchone()
    continuing_incident = False
    if incident:
        prior_ids = set(json.loads(incident["evidence_json"]).get("ready_request_ids", []))
        if headroom_available and protected_target is None:
            for row in ready:
                if row["id"] not in prior_ids or row["id"] == granted:
                    continue
                allowed, _reason, _estimate, _draining = policy.admission_decision(
                    {"size": row["size"], "workers": row["workers"]},
                    target_cores=target_cores, projected_cores=projected,
                    light_ready=light_ready, pressure=pressure,
                    sample_fresh=fresh and mean_busy is not None, now=at,
                    last_heavy_grant=get_setting(db, "last_heavy_grant"),
                    heavy_wait_seconds=waiting_credit(row, at),
                    active_heavy_cores=active_heavy_cores,
                    active_managed_cores=active_managed_cores,
                    active_aged_escape_count=active_aged_escape_count)
                if allowed:
                    continuing_incident = True
                    break
    if stalled_candidates:
        started = get_setting(db, "idle_unadmitted_since")
        if started is None:
            set_setting(db, "idle_unadmitted_since", at)
        elif at - float(started) >= 60:
            record_incident(db, "ready-demand-unadmitted", {
                "ready_request_ids": [item["request_id"] for item in stalled_candidates],
                "estimated_cores": {item["request_id"]: item["estimate_cores"]
                                    for item in stalled_candidates},
                "waiting_reasons": {item["request_id"]: item["reason"]
                                    for item in stalled_candidates},
                "protected_target_id": None,
            })
    else:
        if not continuing_incident:
            set_setting(db, "idle_unadmitted_since", None)
            if incident:
                resolve_incident(db, "ready-demand-unadmitted")
    return {"sample": sample, "cpu_30s_mean": mean_busy, "projected_cores": projected,
            "target_cores": target_cores, "ready_count": len(ready), "granted": granted,
            "protected_target_id": protected_target["id"] if protected_target else None,
            "decisions": decision_log}


def record_incident(db, condition_key, evidence):
    at = now()
    fingerprint = hashlib.sha256(json_text(evidence).encode()).hexdigest()
    old = db.execute("SELECT * FROM incidents WHERE condition_key=?", (condition_key,)).fetchone()
    if old and old["status"] == "open":
        if condition_key == "ready-demand-unadmitted" and old["fingerprint"] != fingerprint:
            db.execute("UPDATE incidents SET updated_at=?,fingerprint=?,evidence_json=? WHERE id=?",
                       (at, fingerprint, json_text(evidence), old["id"]))
            event(db, "incident_observation", None, incident_id=old["id"],
                  condition=condition_key, evidence=evidence)
        return old["id"]
    if old:
        db.execute("""UPDATE incidents SET status='open',opened_at=?,updated_at=?,fingerprint=?,
            evidence_json=?,notification_status=NULL,notification_detail=NULL,notification_at=NULL,
            resolved_at=NULL WHERE id=?""",
            (at, at, fingerprint, json_text(evidence), old["id"]))
        event(db, "incident_reopened", None, incident_id=old["id"], condition=condition_key,
              evidence=evidence)
        return old["id"]
    incident_id = str(uuid.uuid4())
    db.execute("INSERT INTO incidents(id,condition_key,status,opened_at,updated_at,fingerprint,evidence_json) VALUES (?,?, 'open',?,?,?,?)",
               (incident_id, condition_key, at, at, fingerprint, json_text(evidence)))
    event(db, "incident_opened", None, incident_id=incident_id, condition=condition_key,
          evidence=evidence)
    return incident_id


def resolve_incident(db, condition_key, detail="condition cleared"):
    old = db.execute("SELECT id FROM incidents WHERE condition_key=? AND status='open'", (condition_key,)).fetchone()
    if old:
        db.execute("UPDATE incidents SET status='resolved',resolved_at=?,updated_at=? WHERE id=?",
                   (now(), now(), old["id"]))
        event(db, "incident_resolved", None, incident_id=old["id"], condition=condition_key, detail=detail)


def admission_diagnostics(db, at=None, focus_request_id=None):
    at = now() if at is None else at
    latest, mean_busy, fresh = fresh_host_window(db, at)
    cpus = int(latest["logical_cpus"]) if latest else os.cpu_count() or 1
    target_cores = cpus * float(get_setting(db, "target_cpu_fraction") or 0.95)
    pressure = latest["memory_pressure"] if latest else None
    current_busy = latest["cpu_busy"] if latest else None
    sample_busy = max(current_busy or 0.0, mean_busy or 0.0) if mean_busy is not None else current_busy
    observed_cores = (sample_busy or 0.0) * cpus
    active = db.execute("""SELECT r.*,w.order_position,w.served_round FROM reservations r
        JOIN worktrees w ON w.id=r.worktree_id
        WHERE r.state='reserved' AND r.lease_active=1 ORDER BY r.granted_at,r.id""").fetchall()
    unreflected = 0.0
    active_contributors = []
    for job in active:
        start = job["started_at"] or job["granted_at"]
        estimate = policy.estimated_cores(job["size"], job["workers"], target_cores)
        unreflected_job = latest is None or start is None or latest["at"] < start + 10
        unreflected_estimate = estimate if unreflected_job else 0.0
        unreflected += unreflected_estimate
        active_contributors.append({
            "request_id": job["id"], "worktree_id": job["worktree_id"],
            "size": job["size"], "workers": job["workers"],
            "estimated_cores": round(estimate, 2),
            "unreflected_cores": round(unreflected_estimate, 2),
            "started_at": job["started_at"], "granted_at": job["granted_at"]})
    projected = observed_cores + unreflected
    ready = db.execute("""SELECT r.*,w.order_position,w.served_round FROM reservations r
        JOIN worktrees w ON w.id=r.worktree_id
        WHERE r.state='waiting' AND r.eligible_at IS NOT NULL AND r.eligible_at<=?
        AND r.heartbeat_at>=? ORDER BY w.order_position,r.sequence""",
        (at, at - HELPER_STALE_SECONDS)).fetchall()
    light_ready = any(row["size"] == "light" for row in ready)
    heavy_budget = target_cores
    if light_ready:
        heavy_budget -= min(1.0, target_cores / 2)
    active_heavy_cores = sum(policy.estimated_cores(job["size"], job["workers"], target_cores)
                             for job in active if job["size"] == "heavy")
    active_managed_cores = sum(policy.estimated_cores(job["size"], job["workers"], target_cores)
                               for job in active)
    active_light_jobs = [job for job in active if job["size"] == "light"]
    active_escape_ids = active_aged_escape_request_ids(db, active)
    active_aged_escape_count = len(active_escape_ids)
    current_projected = ((current_busy or 0.0) * cpus + unreflected
                         if fresh and current_busy is not None else None)
    current_round = int(get_setting(db, "current_round") or 1)
    unserved = [row for row in ready if row["served_round"] < current_round]
    has_unserved = bool(unserved)
    selection_round = current_round
    if not unserved and ready:
        selection_round += 1
        unserved = ready
    protected_target, continuing_protection, settling_protection = protected_turn_target(
        db, ready, unserved, at=at, target_cores=target_cores,
        projected_cores=projected, current_projected_cores=current_projected,
        light_ready=light_ready, pressure=pressure,
        sample_fresh=fresh and mean_busy is not None,
        last_heavy_grant=get_setting(db, "last_heavy_grant"),
        active_heavy_cores=active_heavy_cores,
        active_managed_cores=active_managed_cores,
        active_worktree_ids={job["worktree_id"] for job in active},
        active_aged_escape_count=active_aged_escape_count)
    protected_target_id = protected_target["id"] if protected_target else None
    protected_wait_reason = None
    if protected_target is not None:
        if not fresh or mean_busy is None:
            protected_wait_reason = "host sample stale or unavailable"
        elif pressure not in ("normal", None):
            protected_wait_reason = "memory pressure observed"
        elif settling_protection:
            protected_wait_reason = f"settling observed CPU window for aged request {protected_target_id}"
        else:
            protected_wait_reason = f"draining managed work for aged request {protected_target_id}"
    worktree_turns = {}
    candidates = []
    last_heavy_grant = get_setting(db, "last_heavy_grant")
    for row in ready:
        worktree_turn = worktree_turns.setdefault(row["worktree_id"], {
            "worktree_id": row["worktree_id"],
            "manual_order_position": row["order_position"],
            "served_round": row["served_round"],
            "eligible_request_ids": [],
            "eligible_this_selection_round": row["served_round"] < selection_round})
        worktree_turn["eligible_request_ids"].append(row["id"])
        estimate = policy.estimated_cores(row["size"], row["workers"], target_cores,
                                          light_ready=light_ready)
        _allowed, policy_reason, _estimate, _draining = policy.admission_decision(
            {"size": row["size"], "workers": row["workers"]},
            target_cores=target_cores, projected_cores=projected,
            light_ready=light_ready, pressure=pressure,
            sample_fresh=fresh and mean_busy is not None, now=at,
            last_heavy_grant=last_heavy_grant,
            heavy_wait_seconds=waiting_credit(row, at),
            active_heavy_cores=active_heavy_cores,
            active_managed_cores=active_managed_cores,
            active_aged_escape_count=active_aged_escape_count)
        reason = policy_reason
        target_id = None
        if row["id"] == protected_target_id:
            reason = protected_wait_reason
            target_id = protected_target_id
        elif protected_target_id is not None and continuing_protection and (
                row["size"] == "heavy" or
                (row["size"] == "light" and (active_light_jobs or settling_protection))):
            target_id = protected_target_id
            if row["size"] == "heavy":
                reason = f"heavy backfill held for aged request {protected_target_id}"
            elif active_light_jobs:
                reason = (f"light backfill held for aged request {protected_target_id} "
                          "while light work is active")
            else:
                reason = (f"light backfill held for aged request {protected_target_id} "
                          "while the observed CPU window settles")
        elif has_unserved and row["served_round"] >= current_round:
            reason = "worktree already received its turn this round"
        blocker = None
        if target_id is not None and row["id"] != protected_target_id:
            blocker = {"kind": "protected_backfill_target", "request_ids": [target_id]}
        elif reason == "worktree already received its turn this round":
            blocker = {"kind": "worktree_round", "request_ids": []}
        elif reason == "aged escape limit reached":
            blocker = {"kind": "aged_escape_limit", "request_ids": active_escape_ids}
        elif reason == "host target leaves insufficient heavy capacity":
            blocker = {"kind": "host_capacity", "request_ids": []}
        elif policy_reason == "host sample stale or unavailable":
            blocker = {"kind": "host_sample", "request_ids": []}
        elif policy_reason == "memory pressure observed":
            blocker = {"kind": "memory_pressure", "request_ids": []}
        elif policy_reason in ("host target has no room for light work",
                               "host target leaves insufficient heavy capacity"):
            blocker = {"kind": "host_capacity", "request_ids": []}
        elif policy_reason == "heavy startup observation interval":
            blocker = {"kind": "startup_observation", "request_ids": []}
        candidates.append({"request_id": row["id"], "worktree_id": row["worktree_id"],
                           "size": row["size"], "workers": row["workers"],
                           "estimated_cores": estimate, "policy_reason": policy_reason,
                           "blocking_reason": reason, "blocking_target_id": target_id,
                           "diagnostic_blocker": blocker,
                           "selected_protected_target": row["id"] == protected_target_id,
                           "manual_order_position": row["order_position"],
                           "served_round": row["served_round"],
                           "eligible_this_selection_round": row["served_round"] < selection_round,
                           "priority": row["priority"],
                           "waiting_credit_seconds": round(waiting_credit(row, at), 1)})
    diagnostics = {
        "snapshot_at": at,
        "interpretation": "point-in-time diagnostic snapshot; not a FIFO rank, reservation promise or ETA",
        "logical_cpus": cpus, "cpu_busy": current_busy,
        "cpu_30s_mean": mean_busy, "observed_cpu_cores": round(observed_cores, 2),
        "projected_cores": round(projected, 2), "target_cores": round(target_cores, 2),
        "memory_pressure": pressure, "sample_fresh": fresh,
        "protected_target_id": protected_target_id,
        "protected_target_reason": protected_wait_reason,
        "active_aged_escape_request_ids": active_escape_ids,
        "active_aged_escape_count": active_aged_escape_count,
        "aged_escape_limit": policy.MAX_ACTIVE_AGED_ESCAPES,
        "host_constraints": {
            "sample_state": "unavailable" if latest is None else ("fresh" if fresh else "stale"),
            "sample_age_seconds": round(max(0.0, at - latest["at"]), 2) if latest else None,
            "cpu_30s_mean": mean_busy,
            "memory_pressure_restricts_admission": pressure not in ("normal", None),
            "heavy_budget_cores": round(heavy_budget, 2),
            "active_managed_estimated_cores": round(active_managed_cores, 2),
            "unreflected_managed_estimated_cores": round(unreflected, 2),
            "active_managed_contributors": active_contributors,
            "attribution_note": "Host measurements are aggregate and do not prove which command caused observed CPU or memory pressure."},
        "round_context": {
            "current_round": current_round,
            "selection_round_if_scheduler_runs_now": selection_round,
            "would_advance_before_selection": bool(ready and not has_unserved),
            "worktrees": list(worktree_turns.values()),
            "note": "This reflects the initial selection round; a capacity skip may advance the round later in a scheduler pass."},
        "candidates": candidates}
    if focus_request_id is not None:
        focus = db.execute("""SELECT r.id,r.state,r.eligible_at,r.heartbeat_at,r.wait_reason,
            r.priority,w.id AS worktree_id,w.order_position,w.served_round
            FROM reservations r JOIN worktrees w ON w.id=r.worktree_id WHERE r.id=?""",
            (focus_request_id,)).fetchone()
        if focus is not None:
            helper_fresh = (focus["heartbeat_at"] is not None and
                            focus["heartbeat_at"] >= at - HELPER_STALE_SECONDS)
            eligible_now = (focus["state"] == "waiting" and focus["eligible_at"] is not None and
                            focus["eligible_at"] <= at and helper_fresh)
            if focus["state"] != "waiting":
                not_eligible_reason = f"request state is {focus['state']}"
            elif focus["eligible_at"] is None:
                not_eligible_reason = "request is held or has no eligibility time"
            elif focus["eligible_at"] > at:
                not_eligible_reason = "eligibility time has not arrived"
            elif not helper_fresh:
                not_eligible_reason = "helper heartbeat is stale or unavailable"
            else:
                not_eligible_reason = None
            diagnostics["request_context"] = {
                "request_id": focus["id"], "state": focus["state"],
                "eligible_now": eligible_now, "not_eligible_reason": not_eligible_reason,
                "wait_reason": focus["wait_reason"],
                "worktree_id": focus["worktree_id"],
                "manual_order_position": focus["order_position"],
                "served_round": focus["served_round"],
                "current_round": current_round,
                "selection_round_if_scheduler_runs_now": selection_round,
                "eligible_this_selection_round": eligible_now and focus["served_round"] < selection_round,
                "priority": focus["priority"]}
    return diagnostics


def completion_route_info(row, targets):
    if row["state"] != "finished" or row["wake_status"] not in (None, "pending", "unavailable"):
        return None
    owner = {"harness": row["owner_harness"], "session": row["owner_session"],
             "surface": row["owner_surface"]}
    if hailing_target_for_owner(owner, targets) is not None:
        return {"status": "enrolled", "request_ids": [row["id"]], "owner": owner}
    enroll = ("hailing-frequencies enroll --harness {harness} --surface {surface} "
              "--session {session} --label warp-drive-owner").format(**owner)
    return {"status": "missing_exact_hailing_route", "request_ids": [row["id"]],
            "owner": owner, "owner_setup_action": enroll,
            "handoff_action": ("The current owner may authorize `warp-drive transfer REQUEST_ID "
                               "--harness claude|codex --session NEW_SESSION --surface NEW_SURFACE "
                               "--from-session CURRENT_OWNER_SESSION --reason 'completion handoff'` "
                               "to a live enrolled owner.")}


def completion_route_gaps(db, targets=NOT_PROVIDED):
    if targets is NOT_PROVIDED:
        targets = read_hailing_targets()
    rows = db.execute("""SELECT * FROM reservations WHERE state='finished'
        AND wake_status IN ('pending','unavailable') ORDER BY ended_at,sequence""").fetchall()
    gaps = {}
    for row in rows:
        info = completion_route_info(row, targets)
        if info is None or info["status"] != "missing_exact_hailing_route":
            continue
        owner_key = (info["owner"]["harness"], info["owner"]["session"], info["owner"]["surface"])
        group = gaps.get(owner_key)
        if group is None:
            group = dict(info)
            group["request_ids"] = []
            gaps[owner_key] = group
        group["request_ids"].append(row["id"])
    return list(gaps.values())


def singleton_lock(state_dir):
    path = safe_state_dir(state_dir) / "scheduler.lock"
    require(not path.is_symlink(), f"Refusing symlink scheduler lock: {path}")
    stream = path.open("a")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise WarpError("Another warp-drive scheduler owns this state directory")
    return stream


def monitor_reservations(db):
    at = now()
    rows = db.execute("SELECT * FROM reservations WHERE state IN ('waiting','reserved')").fetchall()
    for row in rows:
        if row["heartbeat_at"] is not None and at - row["heartbeat_at"] > HELPER_STALE_SECONDS:
            if row["credit_started_at"] is not None:
                freeze_credit(db, row, at)
            if row["state"] == "waiting":
                db.execute("UPDATE reservations SET helper_id=NULL,heartbeat_at=NULL WHERE id=?", (row["id"],))
                set_wait_reason(db, row, "helper heartbeat stale; work is ineligible until reattached")
            elif row["state"] == "reserved" and row["lease_active"]:
                if row["launch_intent_at"] is None:
                    db.execute("UPDATE reservations SET state='waiting',lease_active=0,reservation_claim=NULL,claim_helper_id=NULL,claim_generation=NULL,granted_at=NULL,helper_id=NULL,heartbeat_at=NULL WHERE id=?",
                               (row["id"],))
                    event(db, "unstarted_reservation_released", row["id"], reason="helper heartbeat stale before launch intent")
                    current = current_request(db, row["id"])
                    set_wait_reason(db, current, "helper heartbeat stale before launch; reattach to continue")
                else:
                    db.execute("UPDATE reservations SET lease_active=0,outcome='unknown',wake_status=COALESCE(wake_status,'pending') WHERE id=?",
                               (row["id"],))
                    event(db, "outcome_uncertain", row["id"], reason="helper heartbeat stale after launch intent; command not replayed")
                    event(db, "child_accounting_unavailable", row["id"], **child_accounting_unavailable(
                        "helper heartbeat expired before this helper observed and waited for the child"))
                    record_incident(db, f"command-uncertain:{row['id']}", {
                        "reservation_id": row["id"], "last_heartbeat_at": row["heartbeat_at"],
                        "launch_intent_at": row["launch_intent_at"], "expected_runtime": row["expected_runtime"]})
        if row["state"] == "reserved" and row["launch_intent_at"] is not None and row["started_at"] and \
                at - row["started_at"] > row["expected_runtime"]:
            record_incident(db, f"runtime-overdue:{row['id']}", {
                "reservation_id": row["id"], "started_at": row["started_at"],
                "expected_runtime": row["expected_runtime"], "elapsed_seconds": round(at-row["started_at"], 1),
                "last_heartbeat_at": row["heartbeat_at"], "outcome": row["outcome"] or "unknown"})


def scheduler(state_dir, once=False, send=False):
    lock = singleton_lock(state_dir)
    stopped = threading.Event()
    def stop(*_):
        stopped.set()
    old_handlers = {}
    if not once:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            old_handlers[sig] = signal.signal(sig, stop)
    try:
        while not stopped.is_set():
            sample = host.snapshot()
            with contextlib.closing(connect(state_dir)) as db:
                with immediate_transaction(db):
                    host_sample(db, fixture=sample)
                    monitor_reservations(db)
                    result = scheduler_pass(db, sample=sample)
                    notifications_enabled = bool(get_setting(db, "notifications_enabled"))
            if send and notifications_enabled:
                try:
                    targets = read_hailing_targets()
                    cmux = cmux_executable()
                    sender_surface = observed_sender_surface()
                except Exception as exc:
                    print(f"warp-drive notification readiness check failed: {exc}", file=sys.stderr)
                    targets, cmux, sender_surface = [], None, None
                try:
                    dispatch_pending_completions(state_dir, targets, cmux, sender_surface)
                except Exception as exc:
                    print(f"warp-drive completion send pass failed: {exc}", file=sys.stderr)
                try:
                    deliver_open_incidents(state_dir, targets, cmux)
                except Exception as exc:
                    print(f"warp-drive incident send pass failed: {exc}", file=sys.stderr)
            if once:
                print(json.dumps(result, indent=2, default=str))
                return result
            stopped.wait(SCHEDULER_INTERVAL_SECONDS)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def claim_launch(db, request_id, helper_id, generation):
    require(not db.in_transaction, "Launch claim must own and commit its transaction before spawning the command")
    at = now()
    with immediate_transaction(db):
        row = current_request(db, request_id)
        require(row["state"] == "reserved", "Reservation has not been granted")
        require(row["launch_intent_at"] is None,
                "Launch intent already exists; command will not be replayed")
        require(row["helper_id"] == helper_id and row["helper_generation"] == generation,
                "Helper was replaced; this claim is fenced")
        require(row["claim_helper_id"] == helper_id and row["claim_generation"] == generation,
                "Reservation claim belongs to another helper")
        token = row["reservation_claim"]
        cursor = db.execute("""UPDATE reservations SET launch_intent_at=?,outcome='unknown'
            WHERE id=? AND state='reserved' AND launch_intent_at IS NULL AND reservation_claim=?
            AND helper_id=? AND helper_generation=? AND claim_helper_id=? AND claim_generation=?""",
            (at, row["id"], token, helper_id, generation, helper_id, generation))
        require(cursor.rowcount == 1, "Reservation claim changed before launch; command will not run")
        event(db, "launch_intent", request_id, claim=token, helper_id=helper_id, generation=generation,
              command=json.loads(row["command_json"]))
    return token, json.loads(row["command_json"]), row["worktree_path"]


def mark_started(db, request_id, claim):
    at = now()
    with immediate_transaction(db):
        row = current_request(db, request_id)
        if row["reservation_claim"] != claim or row["launch_intent_at"] is None:
            return False
        cur = db.execute("UPDATE reservations SET started_at=COALESCE(started_at,?),lease_active=1 WHERE id=? AND state='reserved' AND reservation_claim=? AND launch_intent_at IS NOT NULL",
                         (at, row["id"], claim))
        if cur.rowcount != 1:
            return False
        event(db, "command_started", request_id, claim=claim)
    return True


def child_accounting_unavailable(detail):
    return {"measurement": "unavailable", "child_cpu_seconds": None,
            "peak_memory_bytes": None, "cpu_unit": "seconds", "peak_memory_unit": "bytes",
            "accounting_scope": "direct child plus descendants it waited for; excludes Docker, daemons, and orphaned processes",
            "peak_memory_scope": "peak RSS for direct child plus descendants it waited for; not an aggregate",
            "accounting_detail": detail}


def child_accounting_from_wait4(usage):
    peak = None
    detail = None
    raw_peak = max(0, int(usage.ru_maxrss))
    if sys.platform == "darwin":
        peak = raw_peak
    elif sys.platform.startswith("linux"):
        peak = raw_peak * 1024
    else:
        detail = "wait4 supplied ru_maxrss in platform-specific units that warp-drive does not normalize"
    return {"measurement": "wait4 child accounting",
            "child_cpu_seconds": max(0.0, float(usage.ru_utime) + float(usage.ru_stime)),
            "peak_memory_bytes": peak, "cpu_unit": "seconds", "peak_memory_unit": "bytes",
            "accounting_scope": "direct child plus descendants it waited for; excludes Docker, daemons, and orphaned processes",
            "peak_memory_scope": "peak RSS for direct child plus descendants it waited for; not an aggregate",
            "accounting_detail": detail}


def poll_child_with_accounting(child):
    wait4 = getattr(os, "wait4", None)
    waitstatus_to_exitcode = getattr(os, "waitstatus_to_exitcode", None)
    if wait4 is None or waitstatus_to_exitcode is None:
        code = child.poll()
        if code is None:
            return None, None
        return code, child_accounting_unavailable("os.wait4 child resource accounting is unsupported")
    try:
        waited_pid, status, usage = wait4(child.pid, os.WNOHANG)
    except InterruptedError:
        return None, None
    except (ChildProcessError, OSError) as exc:
        code = child.poll()
        if code is None:
            return None, None
        return code, child_accounting_unavailable(f"os.wait4 could not return child usage: {exc}")
    if waited_pid == 0:
        return None, None
    code = waitstatus_to_exitcode(status)
    child.returncode = code
    return code, child_accounting_from_wait4(usage)


def record_completion(db, request_id, claim, exit_code, accounting=None):
    if accounting is None:
        accounting = child_accounting_unavailable("completion was recorded without a waited child usage sample")
    at = now()
    with immediate_transaction(db):
        row = current_request(db, request_id)
        require(row["reservation_claim"] == claim and row["launch_intent_at"] is not None,
                "Completion claim is stale or command was never launched")
        if row["state"] == "finished":
            return False
        require(row["state"] == "reserved", "Reservation is no longer completable")
        outcome = "succeeded" if exit_code == 0 else "failed"
        cur = db.execute("UPDATE reservations SET state='finished',outcome=?,exit_code=?,ended_at=?,lease_active=0,wake_status='pending',wake_detail=NULL WHERE id=? AND state='reserved' AND reservation_claim=? AND launch_intent_at IS NOT NULL",
                         (outcome, int(exit_code), at, row["id"], claim))
        require(cur.rowcount == 1, "Reservation changed before completion could be recorded")
        event(db, "finished", request_id, claim=claim, outcome=outcome, exit_code=int(exit_code),
              duration_seconds=max(0, at - (row["started_at"] or at)), **accounting)
        event(db, "completion_wake_pending", request_id, current_owner={
            "harness": row["owner_harness"], "session": row["owner_session"],
            "surface": row["owner_surface"]})
        resolve_incident(db, f"command-uncertain:{request_id}")
        resolve_incident(db, f"runtime-overdue:{request_id}")
    return True


def hailing_launcher():
    configured = os.environ.get("WARP_DRIVE_HAILING")
    if configured:
        return configured
    found = shutil.which("hailing-frequencies")
    default = Path.home() / ".local/bin/hailing-frequencies"
    return found or (str(default) if default.exists() else None)


def hailing_state_dir():
    return Path(os.environ.get("WARP_DRIVE_HAILING_STATE", str(DEFAULT_HAILING_STATE))).expanduser()


def cmux_executable():
    return os.environ.get("WARP_DRIVE_CMUX") or shutil.which("cmux")


def read_hailing_targets():
    path = hailing_state_dir() / "config.json"
    try:
        config = json.loads(path.read_text())
        if not isinstance(config, dict) or config.get("version") != 1 or not isinstance(config.get("targets"), list):
            return []
    except (OSError, json.JSONDecodeError):
        return []
    valid = []
    for item in config["targets"]:
        if not isinstance(item, dict):
            continue
        try:
            owner = canonical_owner(item.get("harness"), item.get("session"), item.get("surface"))
            require(owner["session"] == item["session"] and owner["surface"] == item["surface"],
                    "Hailing target identity is not canonical")
            workspace = canonical_uuid(item.get("workspace"), "Hailing workspace")
            require(workspace == item["workspace"], "Hailing workspace identity is not canonical")
            for field in ("cutoff", "started_at"):
                value = item.get(field)
                require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
                        f"Hailing target {field} is invalid")
            require("label" not in item or isinstance(item["label"], str),
                    "Hailing target label is invalid")
            valid.append(item)
        except (WarpError, TypeError, ValueError, OverflowError):
            continue
    return valid


def hailing_target_for_owner(owner, targets):
    return next((item for item in targets
                 if item["harness"] == owner["harness"] and
                 item["session"] == owner["session"] and
                 item["surface"] == owner["surface"]), None)


def observed_sender_surface():
    value = os.environ.get("CMUX_SURFACE_ID")
    if not value:
        return None
    try:
        return canonical_uuid(value, "CMUX_SURFACE_ID")
    except WarpError:
        return None


def deliver_completion_wake(db, state_dir, row, target, cmux, sender_surface):
    """Commit one send intent, call CMUX without a write lock, then record its outcome."""
    request_id = row["id"]
    current_owner = {"harness": row["owner_harness"], "session": row["owner_session"],
                     "surface": row["owner_surface"]}
    event_key = "warp-drive-complete-" + request_id.replace("-", "")
    body = (f"Warp-drive completion {event_key}: reservation {request_id} finished "
            f"with outcome {row['outcome']} and exit code {row['exit_code']}. "
            f"Inspect it with: warp-drive --state-dir {shlex.quote(str(state_dir))} inspect {request_id}")
    with immediate_transaction(db):
        current = current_request(db, request_id)
        if current["state"] != "finished" or current["wake_status"] not in (None, "pending", "unavailable"):
            return {"outcome": current["wake_status"] or "pending", "detail": current["wake_detail"]}
        if (current["owner_harness"] != current_owner["harness"] or
                current["owner_session"] != current_owner["session"] or
                current["owner_surface"] != current_owner["surface"]):
            return {"outcome": "pending", "detail": "owner changed during dispatch; next scheduler pass will resolve it"}
        if current["owner_surface"] == sender_surface or target["surface"] != current["owner_surface"]:
            return {"outcome": "pending", "detail": "recipient no longer matches an eligible Hailing target"}
        cur = db.execute("""UPDATE reservations SET wake_status='attempting',
            wake_detail='native CMUX queue attempt started'
            WHERE id=? AND state='finished' AND wake_status IN ('pending','unavailable')""",
            (current["id"],))
        if cur.rowcount != 1:
            return {"outcome": current["wake_status"] or "pending", "detail": current["wake_detail"]}
        event(db, "completion_message_intent", current["id"], event_key=event_key,
              sender_surface=sender_surface, target_session=current_owner["session"],
              target_surface=current_owner["surface"])

    try:
        completed = subprocess.run([cmux, "agent", "message", current_owner["surface"], body],
                                   capture_output=True, text=True, timeout=15)
        if completed.returncode == 0:
            result = {"outcome": "queued", "detail": "native CMUX inbox message queued; Hailing prompt and receipt are separate"}
        else:
            result = {"outcome": "uncertain", "detail": completed.stderr.strip()[:400] or
                      "CMUX did not confirm the queue operation; no automatic retry"}
    except (OSError, subprocess.SubprocessError) as exc:
        result = {"outcome": "uncertain", "detail": f"CMUX queue result is uncertain; no automatic retry: {exc}"[:500]}
    with immediate_transaction(db):
        cur = db.execute("UPDATE reservations SET wake_status=?,wake_detail=? WHERE id=? AND wake_status='attempting'",
                         (result["outcome"], str(result.get("detail", ""))[:500], request_id))
        if cur.rowcount == 1:
            event(db, "completion_wake_result", request_id, outcome=result["outcome"],
                  attempted=True, target_session=current_owner["session"],
                  target_surface=current_owner["surface"], detail=result.get("detail"))
    return result


def dispatch_pending_completions(state_dir, targets=NOT_PROVIDED, cmux=NOT_PROVIDED,
                                 sender_surface=NOT_PROVIDED):
    with contextlib.closing(connect(state_dir)) as db:
        if not get_setting(db, "notifications_enabled"):
            return []
        if targets is NOT_PROVIDED:
            targets = read_hailing_targets()
        if cmux is NOT_PROVIDED:
            cmux = cmux_executable()
        if sender_surface is NOT_PROVIDED:
            sender_surface = observed_sender_surface()
        if not targets or not cmux or not sender_surface:
            return []
        target_by_owner = {(item["harness"], item["session"], item["surface"]): item
                           for item in targets}
        rows = db.execute("""SELECT * FROM reservations WHERE state='finished'
            AND wake_status IN ('pending','unavailable') ORDER BY ended_at,sequence""").fetchall()
        results = []
        for row in rows:
            owner_key = (row["owner_harness"], row["owner_session"], row["owner_surface"])
            target = target_by_owner.get(owner_key)
            if target is None or target["surface"] == sender_surface:
                continue
            results.append(deliver_completion_wake(db, state_dir, row, target, cmux, sender_surface))
            break
        return results

def run_helper(state_dir, request_id, owner, timeout=None, product_umask=0o077):
    if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGCHLD"):
        # This helper owns the product child and must retain its wait status.
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    helper_id = str(uuid.uuid4())
    with contextlib.closing(connect(state_dir)) as db:
        request_id, generation, attach_state = attach_helper(db, request_id, owner, helper_id)
    if attach_state == "finished":
        with contextlib.closing(connect(state_dir)) as db:
            row = current_request(db, request_id)
            result = {"request_id": request_id, "state": "finished", "outcome": row["outcome"],
                      "exit_code": row["exit_code"], "wake_status": row["wake_status"]}
        print(json.dumps(result, indent=2))
        return result
    deadline = None if timeout is None else now() + timeout
    claim = None
    child = None
    last_heartbeat = 0.0
    monitor_only = attach_state == "monitor-only"
    while True:
        at = now()
        try:
            with contextlib.closing(connect(state_dir)) as db:
                row = current_request(db, request_id)
                if row["state"] == "finished":
                    result = {"request_id": request_id, "state": "finished", "outcome": row["outcome"],
                              "exit_code": row["exit_code"], "wake_status": row["wake_status"]}
                    print(json.dumps(result, indent=2))
                    return result
                if row["state"] == "cancelled":
                    result = {"request_id": request_id, "state": "cancelled"}
                    print(json.dumps(result, indent=2))
                    return result
                if monitor_only and row["state"] == "reserved" and not row["lease_active"]:
                    result = {"request_id": request_id, "state": "reserved", "outcome": row["outcome"] or "unknown",
                              "detail": "launch intent exists and the accounting lease expired; inspect and reconcile, do not replay"}
                    print(json.dumps(result, indent=2))
                    return result
                if row["state"] == "reserved" and row["launch_intent_at"] is None and not monitor_only:
                    try:
                        claim, command, cwd = claim_launch(db, request_id, helper_id, generation)
                    except WarpError:
                        command = None
                    if command is not None:
                        try:
                            child = subprocess.Popen(command, cwd=cwd, close_fds=True,
                                                     umask=product_umask)
                        except OSError as exc:
                            missing_accounting = child_accounting_unavailable(
                                "command spawn failed before a child process could be measured")
                            def record_spawn_failure_event():
                                with immediate_transaction(db):
                                    event(db, "launch_spawn_failed", request_id, claim=claim, detail=str(exc)[:500])
                            retry_sqlite_busy(record_spawn_failure_event)
                            retry_sqlite_busy(lambda: record_completion(db, request_id, claim, 127,
                                                                         missing_accounting))
                            continue
                        retry_sqlite_busy(lambda: mark_started(db, request_id, claim))
                if at - last_heartbeat >= HELPER_HEARTBEAT_SECONDS and not monitor_only:
                    if helper_heartbeat(db, request_id, helper_id, generation):
                        last_heartbeat = at
                    elif row["state"] == "waiting":
                        raise WarpError("Helper was replaced while waiting; this helper is fenced")
        except sqlite3.OperationalError as exc:
            if child is None or not any(word in str(exc).lower() for word in ("locked", "busy")):
                raise
            time.sleep(0.25)
            continue
        if child is not None:
            code, accounting = poll_child_with_accounting(child)
            if code is not None:
                def record_child_exit():
                    with contextlib.closing(connect(state_dir)) as db:
                        record_completion(db, request_id, claim, code, accounting)
                retry_sqlite_busy(record_child_exit)
                child = None
            else:
                # Continue heartbeating while the agent-owned foreground command runs.
                time.sleep(1)
        elif deadline is not None and at >= deadline:
            result = {"request_id": request_id, "state": "waiting",
                      "detail": "attach timeout; reservation remains intact"}
            print(json.dumps(result, indent=2))
            return result
        else:
            time.sleep(1)


def detach_helper(args, state_dir, request_id, owner, product_umask=0o077):
    logs = safe_state_dir(Path(state_dir) / "logs")
    log_path = logs / f"{request_id}.log"
    argv = [sys.executable, str(Path(__file__).resolve()), "--state-dir", str(state_dir),
            "attach", request_id, "--harness", owner["harness"],
            "--session", owner["session"], "--surface", owner["surface"]]
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    close_fds=True, start_new_session=True, umask=product_umask)
    print(json.dumps({"request_id": request_id, "helper_pid": process.pid, "log": str(log_path),
                      "survival": "separate POSIX session; verify in the target harness"}, indent=2))
    return {"request_id": request_id, "helper_pid": process.pid, "log": str(log_path)}


def cli_submit(args, state_dir, detach=False, product_umask=0o077):
    owner = owner_from_args(args)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    content = request_content(args, owner, command)
    with contextlib.closing(connect(state_dir)) as db:
        with immediate_transaction(db):
            request_id, created = submit_one(db, args.key, content, owner)
    print(json.dumps({"request_id": request_id, "request_key": args.key,
                      "created": created, "state": "waiting"}, indent=2))
    if detach:
        return detach_helper(args, state_dir, request_id, owner, product_umask=product_umask)
    return run_helper(state_dir, request_id, owner, timeout=args.attach_timeout,
                      product_umask=product_umask)


def cli_attach(args, state_dir, product_umask=0o077):
    owner = owner_from_args(args)
    if args.detach:
        return detach_helper(args, state_dir, args.request_id, owner,
                             product_umask=product_umask)
    return run_helper(state_dir, args.request_id, owner, timeout=args.timeout,
                      product_umask=product_umask)


def helper_exit_code(result):
    if result.get("state") == "finished":
        if result.get("exit_code") is None:
            return 0 if result.get("outcome") == "succeeded" else 1
        code = int(result["exit_code"])
        return code if code >= 0 else min(255, 128 + abs(code))
    return 4


def resolve_actor(db, actor):
    require(actor, "Priority and manual order changes require --actor")
    authorized = {get_setting(db, "operator"), *get_setting(db, "delegates")}
    require(actor in authorized, f"{actor!r} is not the configured operator or an explicit delegate")


def update_order(db, worktree_id, before=None, after=None, expected_revision=None, actor=None):
    revision = int(get_setting(db, "order_revision") or 0)
    if expected_revision is not None:
        require(expected_revision == revision, f"Stale order revision {expected_revision}; current revision is {revision}")
    def find_worktree(value):
        row = db.execute("SELECT * FROM worktrees WHERE id=?", (value,)).fetchone()
        if row:
            return row
        return db.execute("SELECT * FROM worktrees WHERE path=? ORDER BY created_at DESC LIMIT 1",
                          (str(Path(value).expanduser().resolve()),)).fetchone()

    row = find_worktree(worktree_id)
    require(row is not None, "Worktree ID or path not found")
    anchor = None
    if before or after:
        target = before or after
        anchor = find_worktree(target)
        require(anchor is not None and anchor["id"] != row["id"], "Order target not found or identical")
    rows = db.execute("SELECT id FROM worktrees ORDER BY order_position,id").fetchall()
    ordered = [item["id"] for item in rows if item["id"] != row["id"]]
    if anchor is None:
        ordered.append(row["id"])
    else:
        index = ordered.index(anchor["id"])
        ordered.insert(index if before else index + 1, row["id"])
    for position, identifier in enumerate(ordered):
        db.execute("UPDATE worktrees SET order_position=? WHERE id=?", (position, identifier))
    revision += 1
    set_setting(db, "order_revision", revision)
    event(db, "worktree_reordered", None, worktree_id=row["id"], before=before, after=after,
          actor=actor, order_revision=revision, order=ordered)
    return revision


def hailing_engineer_target(targets=NOT_PROVIDED):
    if targets is NOT_PROVIDED:
        targets = read_hailing_targets()
    return next((item for item in targets if item.get("label") == "warp-drive-engineer"), None)


def queue_engineer_message(incident_id, condition_key, evidence, target=NOT_PROVIDED,
                           cmux=NOT_PROVIDED):
    if target is NOT_PROVIDED:
        target = hailing_engineer_target()
    if target is None:
        return {"status": "unavailable", "detail": "warp-drive-engineer is not enrolled in Hailing Frequencies"}
    if cmux is NOT_PROVIDED:
        cmux = cmux_executable()
    if not cmux:
        return {"status": "unavailable", "detail": "cmux executable not found"}
    body = (f"Warp-drive incident {incident_id} ({condition_key}) needs bounded review. "
            f"Evidence: {json_text(evidence)[:1800]}. Inspect with `warp-drive incident {incident_id}`. "
            "Do not kill or replay commands; record evidence, action and outcome.")
    try:
        result = subprocess.run([cmux, "agent", "message", target["surface"], body],
                                capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return {"status": "uncertain", "detail": result.stderr.strip()[:400] or
                    "CMUX did not confirm the queue operation; no automatic retry"}
        return {"status": "queued", "detail": "native CMUX message queued; Hailing prompt and receipt are separate"}
    except OSError as exc:
        return {"status": "unavailable", "detail": str(exc)[:400]}
    except subprocess.SubprocessError as exc:
        return {"status": "uncertain", "detail": f"CMUX queue result is uncertain; no automatic retry: {exc}"[:400]}


def deliver_open_incidents(state_dir, targets=NOT_PROVIDED, cmux=NOT_PROVIDED):
    if targets is NOT_PROVIDED:
        targets = read_hailing_targets()
    target = hailing_engineer_target(targets)
    if cmux is NOT_PROVIDED:
        cmux = cmux_executable()
    if target is None or not cmux:
        return []
    results = []
    with contextlib.closing(connect(state_dir)) as db:
        if not get_setting(db, "notifications_enabled"):
            return []
        incidents = [dict(row) for row in db.execute("""SELECT * FROM incidents WHERE status='open'
            AND (notification_status IS NULL OR notification_status='unavailable') ORDER BY opened_at LIMIT 1""")]
        for item in incidents:
            with immediate_transaction(db):
                cur = db.execute("""UPDATE incidents SET notification_status='attempting',
                    notification_detail='native CMUX queue attempt started'
                    WHERE id=? AND status='open' AND (notification_status IS NULL OR notification_status='unavailable')""",
                    (item["id"],))
                if cur.rowcount != 1:
                    continue
                event(db, "incident_notification_intent", None, incident_id=item["id"])
            result = queue_engineer_message(item["id"], item["condition_key"],
                                            json.loads(item["evidence_json"]), target, cmux)
            with immediate_transaction(db):
                cur = db.execute("""UPDATE incidents SET notification_status=?,notification_detail=?,notification_at=?
                    WHERE id=? AND status='open' AND notification_status='attempting'""",
                    (result["status"], result["detail"], now(), item["id"]))
                if cur.rowcount == 1:
                    event(db, "incident_notification", None, incident_id=item["id"], **result)
            results.append(result)
    return results


def command_batch(args, state_dir):
    raw = json.loads(Path(args.file).read_text())
    require(isinstance(raw, list) and raw, "Batch file must contain a nonempty JSON array")
    prepared = []
    for item in raw:
        require(isinstance(item, dict), "Each batch item must be an object")
        required = {"key", "harness", "session", "surface", "size", "family", "workers",
                    "expected_runtime", "worktree", "command"}
        require(required <= set(item), f"Each item needs: {', '.join(sorted(required))}")
        owner = canonical_owner(item["harness"], item["session"], item["surface"])
        class Args: pass
        entry = Args()
        entry.repo = item.get("repo")
        entry.worktree = item["worktree"]
        entry.size = item["size"]
        entry.family = item["family"]
        entry.workers = item["workers"]
        entry.expected_runtime = item["expected_runtime"]
        content = request_content(entry, owner, item["command"])
        prepared.append((item["key"], content, owner))
    require(len({entry[0] for entry in prepared}) == len(prepared), "Batch contains duplicate request keys")
    results = []
    with contextlib.closing(connect(state_dir)) as db:
        with immediate_transaction(db):
            # Validate all existing keys before committing any new member.
            for key, content, owner in prepared:
                existing = db.execute("SELECT * FROM reservations WHERE request_key=?", (key,)).fetchone()
                if existing:
                    digest = hashlib.sha256(json_text(content).encode()).hexdigest()
                    require(existing["content_sha256"] == digest and owner_matches(existing, owner),
                            f"Batch conflicts with existing request key {key!r}; no rows inserted")
            for key, content, owner in prepared:
                identifier, created = submit_one(db, key, content, owner)
                results.append({"request_id": identifier, "request_key": key, "created": created})
    print(json.dumps({"requests": results}, indent=2))
    return results


def status_data(db):
    at = now()
    latest, mean, fresh = fresh_host_window(db, at)
    targets = read_hailing_targets()
    rows = db.execute("""SELECT r.id,r.request_key,r.repo,r.worktree_path,r.owner_harness,
        r.owner_session,r.size,r.family,r.workers,r.expected_runtime,r.state,r.outcome,
        r.exit_code,r.created_at,r.eligible_at,r.credit_seconds,r.heartbeat_at,
        r.granted_at,r.started_at,r.ended_at,r.wait_reason,r.priority,r.wake_status,
        r.worktree_id,w.order_position,w.served_round
        FROM reservations r JOIN worktrees w ON w.id=r.worktree_id
        WHERE r.state IN ('waiting','reserved') ORDER BY w.order_position,r.sequence""").fetchall()
    return {"at": at, "target_cpu_fraction": get_setting(db, "target_cpu_fraction"),
            "logical_cpus": latest["logical_cpus"] if latest else os.cpu_count(),
            "cpu_busy": latest["cpu_busy"] if latest else None,
            "cpu_30s_mean": mean, "sample_age_seconds": round(at-latest["at"], 2) if latest else None,
            "memory_pressure": latest["memory_pressure"] if latest else None,
            "sample_fresh": fresh, "round": get_setting(db, "current_round"),
            "worktrees": [dict(item) for item in db.execute("SELECT id,path,order_position,served_round FROM worktrees ORDER BY order_position")],
            "reservations": [dict(item) for item in rows],
            "admission": admission_diagnostics(db, at),
            "completion_route_gaps": completion_route_gaps(db, targets),
            "open_incidents": [dict(item) for item in db.execute("SELECT id,condition_key,opened_at,updated_at,notification_status FROM incidents WHERE status='open' ORDER BY opened_at")],
            "notifications_enabled": get_setting(db, "notifications_enabled"),
            "policy_version": get_setting(db, "policy_version"),
            "order_revision": int(get_setting(db, "order_revision") or 0)}


def inspect_data(db, key):
    row = current_request(db, key)
    targets = read_hailing_targets()
    value = request_dict(row)
    route = completion_route_info(row, targets)
    if route is not None:
        value["completion_route"] = route
    return {"reservation": value,
            "admission": admission_diagnostics(db, focus_request_id=row["id"]),
            "events": [{"at": item["at"], "kind": item["kind"], "data": json.loads(item["data_json"])}
                       for item in db.execute("SELECT * FROM events WHERE reservation_id=? ORDER BY id", (row["id"],))]}


def doctor_data(db):
    at = now()
    latest, mean, fresh = fresh_host_window(db, at)
    hailing = hailing_launcher()
    targets = read_hailing_targets()
    stale = [dict(row) for row in db.execute("SELECT id,state,heartbeat_at,launch_intent_at,outcome FROM reservations WHERE state IN ('waiting','reserved') AND heartbeat_at IS NOT NULL AND heartbeat_at<?",
                                             (now()-HELPER_STALE_SECONDS,))]
    lock_path = Path(db.execute("PRAGMA database_list").fetchone()[2]).parent / "scheduler.lock"
    scheduler_state = "not_running"
    if lock_path.exists() and not lock_path.is_symlink():
        with lock_path.open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream, fcntl.LOCK_UN)
            except BlockingIOError:
                scheduler_state = "running"
    return {"ok": True, "database": "ok", "scheduler": scheduler_state,
            "sample_fresh": fresh, "sample_age_seconds": round(now()-latest["at"], 2) if latest else None,
            "sample_count": db.execute("SELECT count(*) FROM host_samples").fetchone()[0],
            "stale_helpers": stale, "hailing_launcher": hailing,
            "hailing_engineer_enrolled": hailing_engineer_target(targets) is not None,
            "admission": admission_diagnostics(db, at),
            "completion_route_gaps": completion_route_gaps(db, targets),
            "open_incidents": db.execute("SELECT count(*) FROM incidents WHERE status='open'").fetchone()[0],
            "pending_completion_wakes": db.execute("SELECT count(*) FROM reservations WHERE state='finished' AND wake_status IN ('pending','unavailable')").fetchone()[0],
            "uncertain_completion_wakes": db.execute("SELECT count(*) FROM reservations WHERE state='finished' AND wake_status IN ('attempting','uncertain')").fetchone()[0],
            "configured_notifications_enabled": get_setting(db, "notifications_enabled"),
            "delivery_receipt_tracking": "not provided; actual prompt reception requires native pilot observation"}


def build_parser():
    parser = argparse.ArgumentParser(prog="warp-drive", description="Deterministic local CPU reservations for agent-owned work")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE,
                        help=f"private state directory (default: {DEFAULT_STATE})")
    sub = parser.add_subparsers(dest="command_name", required=True)

    sub.add_parser("init", help="initialize private local state")
    guide = sub.add_parser("guide", help="print the agent guide without opening state")
    guide.add_argument("--print", dest="print_guide", action="store_true", required=True)
    submit = sub.add_parser("submit", help="create or reuse one stable request for a CPU phase")
    _add_request_args(submit)
    run = sub.add_parser("run", help="submit and attach a CPU phase; use status or inspect for current wait diagnostics",
                         description="Run output cannot predict a later scheduler decision; use status or inspect for a passive point-in-time wait snapshot.")
    _add_request_args(run)
    run.add_argument("--detach", action="store_true", help="launch helper in a separate POSIX session and write a log")
    run.add_argument("--attach-timeout", type=float, help="stop waiting after this many seconds; reservation remains intact")
    attach = sub.add_parser("attach", help=ATTACH_RECOVERY_HELP,
                            description=ATTACH_RECOVERY_HELP)
    attach.add_argument("request_id")
    attach.add_argument("--harness", choices=("claude", "codex"), required=True)
    attach.add_argument("--session", required=True)
    attach.add_argument("--surface", required=True)
    attach.add_argument("--timeout", type=float,
                        help="stop this recovery helper's wait; the reservation remains for later recovery")
    attach.add_argument("--detach", action="store_true",
                        help="run this recovery helper in a separate session and append output to its private log")
    batch = sub.add_parser("batch", help="atomically submit independent CPU-phase requests from a JSON array")
    batch.add_argument("--file", required=True)
    scheduler_parser = sub.add_parser("scheduler", help="run the single periodic sampler, fair grant and observation loop")
    scheduler_parser.add_argument("--once", action="store_true")
    scheduler_parser.add_argument("--send", action="store_true",
                                  help="send pending completion and engineer messages from this native terminal")
    sub.add_parser("status", help="passively show reservation and admission snapshots without attaching",
                   description="Passively show queue and host observations without attaching or replacing a helper. Admission diagnostics are a point-in-time snapshot, not a FIFO rank or ETA.")
    inspect = sub.add_parser("inspect", help="passively show one reservation and admission snapshot without attaching",
                             description="Passively show one reservation, its event history and current admission context without attaching or replacing a helper. Diagnostics are a point-in-time snapshot, not a FIFO rank or ETA.")
    inspect.add_argument("request_id")
    cancel = sub.add_parser("cancel", help="cancel a waiting or not-yet-started reservation")
    cancel.add_argument("request_id")
    delay = sub.add_parser("delay", help="set or clear eligibility time")
    delay.add_argument("request_id")
    group = delay.add_mutually_exclusive_group(required=True)
    group.add_argument("--seconds", type=float)
    group.add_argument("--until", type=float)
    group.add_argument("--hold", action="store_true")
    group.add_argument("--resume", action="store_true")
    transfer = sub.add_parser("transfer", help="transfer current owner without replaying work")
    transfer.add_argument("request_id")
    transfer.add_argument("--harness", choices=("claude", "codex"), required=True)
    transfer.add_argument("--session", required=True)
    transfer.add_argument("--surface", required=True)
    transfer.add_argument("--reason", required=True)
    transfer.add_argument("--from-session", required=True,
                          help="the exact current owner session authorizing this transfer")
    order = sub.add_parser("order", help="place a worktree in manual service order")
    order.add_argument("worktree")
    anchor = order.add_mutually_exclusive_group()
    anchor.add_argument("--before")
    anchor.add_argument("--after")
    order.add_argument("--actor", required=True)
    order.add_argument("--revision", type=int, required=True)
    boost = sub.add_parser("boost", help="set or remove one bounded worktree turn preference")
    boost.add_argument("request_id")
    boost.add_argument("--actor", required=True)
    boost.add_argument("--reason", required=True)
    boost.add_argument("--remove", action="store_true")
    config = sub.add_parser("config", help="configure target, authority, delegates and notifications")
    config_sub = config.add_subparsers(dest="config_action", required=True)
    target = config_sub.add_parser("target")
    target.add_argument("cpu_fraction", type=float)
    target.add_argument("--actor", required=True)
    operator = config_sub.add_parser("operator")
    operator.add_argument("identity")
    operator.add_argument("--actor", required=True)
    delegate = config_sub.add_parser("delegate")
    delegate.add_argument("action", choices=("add", "remove", "list"))
    delegate.add_argument("identity", nargs="?")
    delegate.add_argument("--actor", required=True)
    notifications = config_sub.add_parser("notifications")
    notifications.add_argument("mode", choices=("on", "off"))
    notifications.add_argument("--actor", required=True)
    config_sub.add_parser("show")
    sub.add_parser("doctor", help="inspect local scheduler, sample and Hailing readiness")
    incident = sub.add_parser("incident", help="inspect or record evidence for an exceptional condition")
    incident.add_argument("incident_id")
    incident.add_argument("--evidence")
    incident.add_argument("--action")
    incident.add_argument("--outcome")
    incident.add_argument("--resolve", action="store_true")
    reconcile = sub.add_parser("reconcile", help="record owner evidence for an uncertain command")
    reconcile.add_argument("request_id")
    reconcile.add_argument("--harness", choices=("claude", "codex"), required=True)
    reconcile.add_argument("--session", required=True)
    reconcile.add_argument("--surface", required=True)
    reconcile.add_argument("--outcome", choices=("unknown", "succeeded", "failed"), required=True)
    reconcile.add_argument("--evidence", required=True)
    reconcile.add_argument("--exit-code", type=int)
    return parser


def _add_request_args(parser):
    parser.add_argument("--key", required=True)
    parser.add_argument("--harness", choices=("claude", "codex"), required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--surface", required=True)
    parser.add_argument("--size", choices=("light", "heavy"), required=True,
                        help="bounded demand category for this CPU phase")
    parser.add_argument("--family", required=True,
                        help="workload family for later raw-data classification")
    parser.add_argument("--workers", type=int, required=True,
                        help="simultaneous CPU workers in this phase, not a sum across serial phases")
    parser.add_argument("--expected-runtime", type=float, required=True,
                        help="expected duration of this command phase in seconds")
    parser.add_argument("--repo")
    parser.add_argument("--worktree")
    parser.add_argument("command", nargs=argparse.REMAINDER)


def main(argv=None):
    product_umask = os.umask(0o077)
    try:
        return _main(argv, product_umask)
    finally:
        os.umask(product_umask)


def _main(argv, product_umask):
    args = build_parser().parse_args(argv)
    state_dir = Path(args.state_dir).expanduser().absolute()
    if args.command_name == "guide":
        guide_path = Path(__file__).with_name("guide.md")
        sys.stdout.write(guide_path.read_text(encoding="utf-8"))
        return 0
    if args.command_name == "init":
        with contextlib.closing(connect(state_dir)) as db:
            print(json.dumps({"initialized": str(state_dir), "operator": get_setting(db, "operator")}, indent=2))
        return 0
    if args.command_name == "submit":
        owner = owner_from_args(args)
        command = args.command[1:] if args.command and args.command[0] == "--" else args.command
        content = request_content(args, owner, list(command))
        with contextlib.closing(connect(state_dir)) as db:
            with immediate_transaction(db):
                request_id, created = submit_one(db, args.key, content, owner)
        print(json.dumps({"request_id": request_id, "request_key": args.key, "created": created}, indent=2))
        return 0
    if args.command_name == "run":
        owner = owner_from_args(args)
        result = cli_submit(args, state_dir, detach=args.detach, product_umask=product_umask)
        return 0 if args.detach else helper_exit_code(result)
    if args.command_name == "attach":
        request_id = args.request_id
        owner = owner_from_args(args)
        if args.detach:
            return detach_helper(args, state_dir, request_id, owner,
                                 product_umask=product_umask)
        return helper_exit_code(run_helper(state_dir, request_id, owner, timeout=args.timeout,
                                           product_umask=product_umask))
    if args.command_name == "batch":
        command_batch(args, state_dir)
        return 0
    if args.command_name == "scheduler":
        scheduler(state_dir, once=args.once, send=args.send)
        return 0
    with contextlib.closing(connect(state_dir)) as db:
        if args.command_name == "status":
            print(json.dumps(status_data(db), indent=2, default=str))
        elif args.command_name == "inspect":
            print(json.dumps(inspect_data(db, args.request_id), indent=2, default=str))
        elif args.command_name == "doctor":
            print(json.dumps(doctor_data(db), indent=2, default=str))
        elif args.command_name == "incident":
            with immediate_transaction(db):
                row = db.execute("SELECT * FROM incidents WHERE id=? OR condition_key=? ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END,opened_at DESC LIMIT 1",
                                 (args.incident_id, args.incident_id)).fetchone()
                require(row is not None, "Incident not found")
                if args.evidence is not None or args.action is not None or args.outcome is not None:
                    require(args.evidence and args.action and args.outcome,
                            "Recording incident work requires --evidence, --action, and --outcome")
                    evidence = json.loads(row["evidence_json"])
                    observations = evidence.setdefault("engineer_observations", [])
                    observation = {"at": now(), "evidence": args.evidence,
                                   "action": args.action, "outcome": args.outcome}
                    observations.append(observation)
                    db.execute("UPDATE incidents SET evidence_json=?,updated_at=? WHERE id=?",
                               (json_text(evidence), now(), row["id"]))
                    event(db, "incident_observation", None, incident_id=row["id"], **observation)
                if args.resolve and row["status"] == "open":
                    db.execute("UPDATE incidents SET status='resolved',resolved_at=?,updated_at=? WHERE id=?",
                               (now(), now(), row["id"]))
                    event(db, "incident_resolved", None, incident_id=row["id"], detail=args.outcome or "operator resolved")
                row = db.execute("SELECT * FROM incidents WHERE id=?", (row["id"],)).fetchone()
            value = dict(row)
            value["evidence"] = json.loads(value.pop("evidence_json"))
            print(json.dumps(value, indent=2, default=str))
        elif args.command_name == "reconcile":
            owner = owner_from_args(args)
            with immediate_transaction(db):
                row = current_request(db, args.request_id)
                require(owner_matches(row, owner), "Only the current owner may reconcile this reservation")
                require(row["state"] == "reserved" and row["launch_intent_at"] is not None,
                        "Only a launched reservation can be reconciled")
                if args.outcome == "unknown":
                    db.execute("UPDATE reservations SET outcome='unknown',lease_active=0 WHERE id=?",
                               (row["id"],))
                else:
                    db.execute("UPDATE reservations SET state='finished',outcome=?,exit_code=?,ended_at=?,lease_active=0,wake_status='pending' WHERE id=?",
                               (args.outcome, args.exit_code, now(), row["id"]))
                event(db, "owner_reconciled", row["id"], outcome=args.outcome,
                      exit_code=args.exit_code, evidence=args.evidence,
                      **child_accounting_unavailable(
                          "owner reconciliation has no helper-observed wait4 sample"))
                if args.outcome != "unknown":
                    resolve_incident(db, f"command-uncertain:{row['id']}")
                    resolve_incident(db, f"runtime-overdue:{row['id']}")
            print(json.dumps({"request_id": row["id"], "outcome": args.outcome,
                              "state": "finished" if args.outcome != "unknown" else "reserved",
                              "evidence_recorded": True,
                              "wake_status": "pending" if args.outcome != "unknown" else None}, indent=2))
        elif args.command_name == "cancel":
            with immediate_transaction(db):
                row = current_request(db, args.request_id)
                if row["state"] == "cancelled":
                    pass
                else:
                    require(row["state"] == "waiting" or (row["state"] == "reserved" and row["launch_intent_at"] is None),
                            "Started work is not stopped by warp-drive; the owner remains responsible")
                    cur = db.execute("UPDATE reservations SET state='cancelled',outcome='cancelled',ended_at=?,lease_active=0 WHERE id=? AND state=? AND launch_intent_at IS ?",
                                     (now(), row["id"], row["state"], row["launch_intent_at"]))
                    require(cur.rowcount == 1, "Reservation changed while cancelling; inspect current status")
                    event(db, "cancelled", row["id"], idempotent=False)
            print(json.dumps({"request_id": row["id"], "state": "cancelled"}, indent=2))
        elif args.command_name == "delay":
            with immediate_transaction(db):
                row = current_request(db, args.request_id)
                require(row["state"] == "waiting", "Only waiting reservations can be delayed or held")
                freeze_credit(db, row, now())
                if args.hold:
                    eligible_at = None
                    label = "held by owner"
                elif args.resume:
                    eligible_at = now()
                    label = "eligible after hold"
                elif args.seconds is not None:
                    require(args.seconds >= 0, "--seconds must be nonnegative")
                    eligible_at = now() + args.seconds
                    label = "delayed until requested time"
                else:
                    eligible_at = args.until
                    require(eligible_at >= now(), "--until must be a future Unix timestamp")
                    label = "delayed until requested time"
                db.execute("UPDATE reservations SET eligible_at=?,credit_started_at=?,wait_reason=? WHERE id=?",
                           (eligible_at, max(now(), eligible_at) if eligible_at is not None else None,
                            label, row["id"]))
                event(db, "eligibility_changed", row["id"], eligible_at=eligible_at,
                      held=eligible_at is None, preserved_credit_seconds=row["credit_seconds"])
            print(json.dumps({"request_id": row["id"], "eligible_at": eligible_at,
                              "credit_seconds": row["credit_seconds"]}, indent=2))
        elif args.command_name == "transfer":
            owner = owner_from_args(args)
            with immediate_transaction(db):
                row = current_request(db, args.request_id)
                old = {"harness": row["owner_harness"], "session": row["owner_session"], "surface": row["owner_surface"]}
                require(row["owner_session"] == canonical_uuid(args.from_session, "--from-session"),
                        "--from-session must be the current reservation owner")
                if row["state"] == "waiting":
                    freeze_credit(db, row, now())
                    db.execute("UPDATE reservations SET helper_id=NULL,heartbeat_at=NULL,credit_started_at=NULL WHERE id=?",
                               (row["id"],))
                    event(db, "waiting_helper_fenced_on_transfer", row["id"], old_session=old["session"])
                elif row["state"] == "reserved" and row["launch_intent_at"] is None:
                    freeze_credit(db, row, now())
                    db.execute("""UPDATE reservations SET state='waiting',lease_active=0,
                        reservation_claim=NULL,claim_helper_id=NULL,claim_generation=NULL,
                        granted_at=NULL,helper_id=NULL,heartbeat_at=NULL,credit_started_at=NULL,
                        wait_reason='owner transferred before launch; new owner must attach'
                        WHERE id=?""", (row["id"],))
                    event(db, "unstarted_reservation_requeued_on_transfer", row["id"], old_session=old["session"])
                cur = db.execute("UPDATE reservations SET owner_harness=?,owner_session=?,owner_surface=? WHERE id=? AND owner_harness=? AND owner_session=? AND owner_surface=?",
                                 (owner["harness"], owner["session"], owner["surface"], row["id"],
                                  old["harness"], old["session"], old["surface"]))
                require(cur.rowcount == 1, "Reservation owner changed during transfer; inspect current status")
                event(db, "owner_transferred", row["id"], old_owner=old, new_owner=owner, reason=args.reason)
            print(json.dumps({"request_id": row["id"], "from": old, "to": owner}, indent=2))
        elif args.command_name == "order":
            with immediate_transaction(db):
                resolve_actor(db, args.actor)
                revision = update_order(db, args.worktree, args.before, args.after, args.revision,
                                        actor=args.actor)
                event(db, "order_authorized", None, actor=args.actor, revision=revision)
            print(json.dumps({"order_revision": revision}, indent=2))
        elif args.command_name == "boost":
            with immediate_transaction(db):
                resolve_actor(db, args.actor)
                row = current_request(db, args.request_id)
                priority = 0 if args.remove else 1
                db.execute("UPDATE reservations SET priority=?,priority_actor=?,priority_reason=? WHERE id=?",
                           (priority, args.actor, args.reason, row["id"]))
                event(db, "boost_changed", row["id"], priority=priority, actor=args.actor, reason=args.reason)
            print(json.dumps({"request_id": row["id"], "priority": priority}, indent=2))
        elif args.command_name == "config":
            with immediate_transaction(db):
                action = args.config_action
                if action == "target":
                    resolve_actor(db, args.actor)
                    require(0.1 <= args.cpu_fraction <= 1.0, "CPU target must be in 0.10..1.00")
                    set_setting(db, "target_cpu_fraction", args.cpu_fraction)
                    event(db, "target_changed", None, cpu_fraction=args.cpu_fraction)
                elif action == "operator":
                    resolve_actor(db, args.actor)
                    set_setting(db, "operator", args.identity)
                    event(db, "operator_changed", None, identity=args.identity)
                elif action == "delegate":
                    if args.action != "list":
                        resolve_actor(db, args.actor)
                    delegates = list(get_setting(db, "delegates"))
                    if args.action == "list":
                        pass
                    else:
                        require(args.identity, "delegate add/remove requires an identity")
                        if args.action == "add" and args.identity not in delegates:
                            delegates.append(args.identity)
                        elif args.action == "remove":
                            delegates = [item for item in delegates if item != args.identity]
                        set_setting(db, "delegates", delegates)
                        event(db, "delegate_changed", None, action=args.action, identity=args.identity)
                elif action == "notifications":
                    resolve_actor(db, args.actor)
                    enabled = args.mode == "on"
                    set_setting(db, "notifications_enabled", enabled)
                    event(db, "notifications_changed", None, enabled=enabled)
                elif action == "show":
                    print(json.dumps({key: get_setting(db, key) for key in
                                      ("target_cpu_fraction", "operator", "delegates", "notifications_enabled",
                                       "policy_version", "current_round", "order_revision")}, indent=2))
                    return 0
            if args.config_action != "show":
                print(json.dumps({"action": args.config_action, "updated": True}, indent=2))
        else:
            raise WarpError(f"Unsupported command: {args.command_name}")
    return 0


if __name__ == "__main__":
    try:
        result = main()
        sys.exit(result if isinstance(result, int) else 0)
    except (WarpError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"warp-drive: {exc}", file=sys.stderr)
        sys.exit(2)
