"""Read the local Claude status-line cache and persist weekly quota alerts."""

from decimal import Decimal
import json
import math
import os
import stat
import subprocess
import uuid


CACHE_LIMIT = 64 * 1024
MAX_AGE = 180
WATCH_COLUMNS = (
    "owner_key", "session", "canonical_worktree", "continuation", "threshold",
    "active", "observed_at", "resets_at", "remaining_percent", "last_alert_reset", "timer_id",
)


def finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def read_cache(path, now):
    def unavailable(reason):
        return {"available": False, "reason": reason}

    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return unavailable("Claude quota cache is not an owner-held regular file")
            if info.st_mode & 0o022:
                return unavailable("Claude quota cache is writable by other users")
            if info.st_size >= CACHE_LIMIT:
                return unavailable("Claude quota cache exceeds its size limit")
            raw = stream.read(CACHE_LIMIT)
    except OSError:
        return unavailable("Claude quota cache is missing or unreadable")
    if len(raw) >= CACHE_LIMIT:
        return unavailable("Claude quota cache exceeds its size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        return unavailable("Claude quota cache is not valid JSON")
    if not isinstance(payload, dict):
        return unavailable("Claude quota cache must contain an object")
    observed_at = payload.get("ts")
    if not finite_number(observed_at) or not 0 <= now - observed_at <= MAX_AGE:
        return unavailable("Claude quota cache observation is stale or invalid")
    limits = payload.get("rate_limits")
    weekly = limits.get("seven_day") if isinstance(limits, dict) else None
    if not isinstance(weekly, dict):
        return unavailable("Claude weekly quota fields are unavailable")
    used, resets_at = weekly.get("used_percentage"), weekly.get("resets_at")
    if not finite_number(used) or not 0 <= used <= 100:
        return unavailable("Claude weekly usage percentage is invalid")
    if not finite_number(resets_at) or resets_at <= now:
        return unavailable("Claude weekly reset is expired or invalid")
    return {"available": True, "observed_at": float(observed_at), "resets_at": float(resets_at),
            "remaining_percent": float(100 - Decimal(str(used)))}


def initialize(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS quota_watches (
            owner_key TEXT PRIMARY KEY, session TEXT NOT NULL, canonical_worktree TEXT,
            continuation TEXT NOT NULL, threshold REAL, active INTEGER NOT NULL,
            observed_at REAL, resets_at REAL, remaining_percent REAL,
            last_alert_reset REAL, timer_id TEXT REFERENCES timers(id)
        )
    """)


def watch_dict(row):
    if row is None:
        return None
    watch = dict(zip(WATCH_COLUMNS, row))
    watch["active"] = bool(watch["active"])
    return watch


def get_watch(db, owner_key):
    return watch_dict(db.execute("SELECT * FROM quota_watches WHERE owner_key=?", (owner_key,)).fetchone())


def timer_active(db, timer_id):
    return timer_id is not None and db.execute(
        "SELECT 1 FROM timers WHERE id=? AND status='active'", (timer_id,),
    ).fetchone() is not None


def should_alert(watch, sample):
    threshold = watch["threshold"]
    if threshold is not None:
        last_reset = watch["last_alert_reset"]
        return (sample["remaining_percent"] <= threshold
                and (last_reset is None or sample["resets_at"] > last_reset))
    return watch["observed_at"] is not None and (
        sample["remaining_percent"], sample["resets_at"]
    ) != (watch["remaining_percent"], watch["resets_at"])


def confirmed_ended(beacon, target):
    try:
        activity = beacon.app.cmux.activity(target)
        return isinstance(activity, dict) and activity.get("state") == "ended"
    except (AttributeError, KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        return False


def enqueue(beacon, db, now, allowed):
    watches = [watch_dict(row) for row in db.execute("SELECT * FROM quota_watches WHERE active=1")]
    if not watches:
        return
    sample = read_cache(beacon.quota_cache, now)
    if not sample["available"]:
        return
    targets = {target["session"]: target for target in beacon.app.config()["targets"]}
    for watch in watches:
        session = watch["session"]
        target = targets.get(session)
        if target is None or (allowed is not None and session not in allowed):
            continue
        worktree = watch["canonical_worktree"]
        if beacon.owner_key(target, worktree) != watch["owner_key"]:
            continue
        if timer_active(db, watch["timer_id"]):
            continue
        last_observed = watch["observed_at"]
        if last_observed is not None and sample["observed_at"] <= last_observed:
            continue
        timer_id = watch["timer_id"]
        alert_reset = watch["last_alert_reset"]
        if should_alert(watch, sample):
            if confirmed_ended(beacon, target):
                continue
            timer_id = str(uuid.uuid4())
            db.execute("""
                INSERT INTO timers (
                    id, owner_key, harness, session, surface, workspace, started_at,
                    canonical_worktree, continuation, schedule, due_at, status, created_at, updated_at,
                    schedule_spec
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'at', ?, 'active', ?, ?, ?)
            """, (timer_id, watch["owner_key"], target["harness"], session, target["surface"],
                  target["workspace"], target["started_at"], worktree, watch["continuation"],
                  now, now, now, json.dumps({"quota_observation": sample})))
            if watch["threshold"] is not None:
                alert_reset = sample["resets_at"]
        db.execute("""
            UPDATE quota_watches SET observed_at=?, resets_at=?, remaining_percent=?,
                last_alert_reset=?, timer_id=? WHERE owner_key=?
        """, (sample["observed_at"], sample["resets_at"], sample["remaining_percent"],
              alert_reset, timer_id, watch["owner_key"]))
