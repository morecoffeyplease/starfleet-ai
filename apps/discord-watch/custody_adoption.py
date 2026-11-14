"""Explicit, resumable current-generation custody adoption for selected obligations."""

import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

try:
    import hailing_frequencies as hf
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hailing-frequencies"))
    import hailing_frequencies as hf
import temporal_beacon

MAX_ADOPTION_RESOURCES = 24


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_digest(value):
    return hashlib.sha256(value).hexdigest()


def _private_file(path, label):
    path = Path(path)
    require(not path.is_symlink(), f"Refusing symlink {label}")
    info = path.stat()
    require(path.is_file() and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
            f"{label} must be a regular owner-only file")
    return path


def _ro(path):
    path = Path(path)
    require(not path.is_symlink() and path.is_file(), "Required state database is unavailable")
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2)


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rows(db, table, where, parameters, order_by=None):
    columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
    require(columns, f"Required state table is missing: {table}")
    order = order_by or ",".join('"' + column.replace('"', '""') + '"' for column in columns)
    rows = db.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY {order}", parameters).fetchall()
    return [dict(zip(columns, row)) for row in rows]


def _target_tuple(target, worktree):
    return {"harness": target["harness"], "session": target["session"],
            "surface": target["surface"], "workspace": target["workspace"],
            "started_at": target["started_at"], "worktree": worktree}


def _owner_digest(target):
    return _digest(target)


def _hook_current(app, old_target):
    harness = app._launch_kind()
    require(harness == old_target["harness"], "Current harness differs from the registered owner")
    env_key = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID",
               "opencode": "OPENCODE_SESSION_ID"}[harness]
    session_raw = os.environ.get(env_key)
    require(session_raw, f"Missing exact current {env_key}")
    session = hf.session_identity(session_raw, harness)
    surface_raw = os.environ.get("CMUX_SURFACE_ID")
    workspace_raw = os.environ.get("CMUX_WORKSPACE_ID")
    require(surface_raw and workspace_raw, "Current surface and workspace identities are required")
    surface, workspace = hf.identity(surface_raw), hf.identity(workspace_raw)
    require((harness, session, surface, workspace) ==
            (old_target["harness"], old_target["session"], old_target["surface"],
             old_target["workspace"]),
            "Current caller is outside the exact registered harness/session/surface/workspace")
    cmux = app.cmux_factory()
    hook_path = cmux.hook_directory / f"{harness}-hook-sessions.json"
    _private_file(hook_path, "native hook store")
    try:
        store = json.loads(hook_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise ValueError("Current native hook store is invalid") from None
    records = store.get("sessions") if isinstance(store, dict) else None
    require(isinstance(records, dict), "Current native hook session records are unavailable")
    record = next((value for key, value in records.items()
                   if (key == session if harness == "opencode" else str(key).upper() == session)), None)
    require(isinstance(record, dict), "Exact current native session is absent from its hook store")
    hook_surface = record.get("surfaceId")
    hook_workspace = record.get("workspaceId")
    require(isinstance(hook_surface, str) and isinstance(hook_workspace, str) and
            hook_surface.upper() == surface and hook_workspace.upper() == workspace,
            "Current native hook surface or workspace changed")
    require(record.get("agentLifecycle") in hf.ACTIVE_LIFECYCLES,
            "Current native session is inactive or awaiting background work")
    started_at = record.get("startedAt")
    require(type(started_at) in (int, float) and math.isfinite(started_at) and started_at >= 0,
            "Current native generation is unavailable")
    require(started_at != old_target["started_at"],
            "Current owner still matches the enrolled generation; use the ordinary exact-owner path")
    cwd = record.get("cwd") or record.get("workingDirectory")
    require(isinstance(cwd, str) and cwd, "Current canonical worktree is unavailable")
    try:
        worktree = str(Path(cwd).expanduser().resolve(strict=True))
    except (OSError, RuntimeError):
        raise ValueError("Current canonical worktree is unavailable") from None
    target = dict(old_target)
    target["started_at"] = started_at
    target = app.binding_snapshot(target)
    if harness == "opencode":
        cmux.owner_identity_binding(target)
        require(str(Path(target["worktree_directory"]).expanduser().resolve(strict=True)) == worktree,
                "Current OpenCode worktree differs from its exact enrolled binding")
    else:
        cmux.binding(target)
    temporal_beacon.TemporalBeacon(hf.Monitor(app.hailing_state, cmux=cmux))._verify_expected_worktree(
        target, worktree)
    return target, worktree


def _find_target(app):
    config_path = app.hailing_state / "config.json"
    _private_file(config_path, "Hailing Frequencies config")
    try:
        config_bytes = config_path.read_bytes()
        config = json.loads(config_bytes)
    except (OSError, ValueError, UnicodeError):
        raise ValueError("Hailing Frequencies config is invalid") from None
    monitor = hf.Monitor(app.hailing_state, cmux=app.cmux_factory())
    monitor.config(config)
    harness = app._launch_kind()
    env_key = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID",
               "opencode": "OPENCODE_SESSION_ID"}[harness]
    raw_session = os.environ.get(env_key)
    require(raw_session, f"Missing exact current {env_key}")
    session = hf.session_identity(raw_session, harness)
    surface = hf.identity(os.environ.get("CMUX_SURFACE_ID", ""))
    workspace = hf.identity(os.environ.get("CMUX_WORKSPACE_ID", ""))
    matches = [target for target in config["targets"]
               if (target["harness"], target["session"], target["surface"], target["workspace"]) ==
               (harness, session, surface, workspace)]
    require(len(matches) == 1, "The exact current workspace has no unique Hailing Frequencies registration")
    hailing_target = dict(matches[0])
    old_target = app.binding_snapshot(hailing_target)
    current_target, worktree = _hook_current(app, old_target)
    require(current_target["started_at"] != old_target["started_at"],
            "A custody adoption requires a changed native generation")
    return config_path, config_bytes, config, hailing_target, old_target, current_target, worktree


def _history_digest(db, session):
    names = ("attempts", "batches", "acknowledgements", "attention_intents")
    present = _tables(db)
    snapshot = {}
    for name in names:
        if name in present:
            snapshot[name] = _rows(db, name, "session=?", (session,))
        else:
            snapshot[name] = None
    return _digest(snapshot), snapshot


def _hailing_projection(target, history):
    value = dict(target)
    value.pop("started_at", None)
    return _digest({"target": value, "history": history})


def _counts(rows, key):
    result = {}
    for row in rows:
        value = str(row.get(key, "unknown"))
        result[value] = result.get(value, 0) + 1
    return result


def _dw_snapshot(db, thread_id):
    thread_rows = _rows(db, "threads", "thread_id=?", (thread_id,))
    require(len(thread_rows) == 1, f"Discord Watch thread {thread_id} is not registered")
    group_ids = [row["id"] for row in _rows(db, "delivery_groups", "thread_id=?", (thread_id,))]
    groups_sql = "group_id IN (" + ",".join("?" for _ in group_ids) + ")" if group_ids else "0"
    message_rows = _rows(db, "messages", "thread_id=?", (thread_id,),
                         "length(message_id),message_id")
    group_rows = _rows(db, "delivery_groups", "thread_id=?", (thread_id,), "id")
    member_rows = _rows(db, "delivery_group_members", "thread_id=?", (thread_id,), "group_id,ordinal")
    attempt_rows = (_rows(db, "delivery_group_attempts", groups_sql, tuple(group_ids), "id")
                    if group_ids else [])
    event_rows = _rows(db, "ownership_events", "thread_id=?", (thread_id,), "owner_generation,id")
    recovery_rows = _rows(db, "gateway_recovery_work", "thread_id=?", (thread_id,), "epoch,owner_generation")
    return {"thread": thread_rows[0], "messages": message_rows, "groups": group_rows,
            "members": member_rows, "attempts": attempt_rows, "ownership_events": event_rows,
            "gateway_recovery": recovery_rows}


def _dw_preserved_digest(snapshot, operation_id=None):
    value = json.loads(_canonical(snapshot))
    for key in ("owner_json", "owner_generation", "updated_at"):
        value["thread"].pop(key, None)
    for row in value["messages"]:
        row.pop("owner_generation", None)
    value["ownership_events"] = [row for row in value["ownership_events"]
                                 if not (operation_id and row.get("operation_id") == operation_id)]
    return _digest(value)


def _timer_snapshot(db, timer_id):
    rows = _rows(db, "timers", "id=?", (timer_id,))
    require(len(rows) == 1, f"Temporal Beacon timer {timer_id} is not registered")
    occurrences = _rows(db, "timer_occurrences", "timer_id=?", (timer_id,), "sequence")
    return {"timer": rows[0], "occurrences": occurrences}


def _timer_preserved_digest(snapshot):
    value = json.loads(_canonical(snapshot))
    for key in ("owner_key", "harness", "session", "surface", "workspace", "started_at", "updated_at"):
        value["timer"].pop(key, None)
    return _digest(value)


def _build_plan(app, operation_id, thread_ids, timer_ids, approval_reference):
    config_path, config_bytes, config, hailing_target, old_target, current_target, worktree = _find_target(app)
    hailing_db_path = app.hailing_state / "receipts.sqlite3"
    watch_db_path = app.directory / "discord-watch.sqlite3"
    require(hailing_db_path.is_file() and not hailing_db_path.is_symlink(),
            "Hailing Frequencies state database is unavailable")
    with contextlib.closing(_ro(hailing_db_path)) as hailing_db:
        require({"attempts", "batches", "acknowledgements", "attention_intents"}.issubset(_tables(hailing_db)),
                "Hailing Frequencies receipt history is incomplete")
        history_digest, history = _history_digest(hailing_db, old_target["session"])
    hailing_summary = {
        "attempt_outcomes": _counts(history["attempts"] or [], "outcome"),
        "batch_outcomes": _counts(history["batches"] or [], "outcome"),
        "acknowledgment_count": len(history["acknowledgements"] or []),
        "attention_outcomes": _counts(history["attention_intents"] or [], "outcome"),
        "uncertain_attention_attempts": sum(
            row.get("outcome") == "uncertain" for row in history["attention_intents"] or [])}
    old_config_digest = _file_digest(config_bytes)
    new_config = json.loads(json.dumps(config))
    new_hailing_target = None
    for index, target in enumerate(new_config["targets"]):
        if target.get("session") == old_target["session"] and target.get("harness") == old_target["harness"]:
            new_config["targets"][index]["started_at"] = current_target["started_at"]
            new_hailing_target = dict(new_config["targets"][index])
            break
    require(new_hailing_target is not None, "Hailing Frequencies target disappeared while planning")
    new_config_bytes = (json.dumps(new_config, indent=2) + "\n").encode("utf-8")
    new_config_digest = _file_digest(new_config_bytes)
    hailing = {"resource_id": old_target["session"], "before_digest": _digest({
        "config": old_config_digest, "history": history_digest}),
        "after_preserved_digest": _hailing_projection(current_target, history),
        "config_before_digest": old_config_digest, "config_after_digest": new_config_digest,
        "history_digest": history_digest, "cutoff": hailing_target["cutoff"],
        "old_target": hailing_target, "new_target": new_hailing_target,
        "summary": hailing_summary}
    threads = []
    if thread_ids:
        require(watch_db_path.is_file() and not watch_db_path.is_symlink(),
                "Discord Watch state database is unavailable")
        with contextlib.closing(_ro(watch_db_path)) as watch_db:
            required = {"threads", "messages", "delivery_groups", "delivery_group_members",
                        "delivery_group_attempts", "ownership_events", "gateway_recovery_work"}
            require(required.issubset(_tables(watch_db)), "Discord Watch state schema is incomplete")
            for thread_id in thread_ids:
                snapshot = _dw_snapshot(watch_db, thread_id)
                thread = snapshot["thread"]
                stored_owner = json.loads(thread["owner_json"])
                require(stored_owner == old_target,
                        f"Discord Watch thread {thread_id} is not owned by the exact old generation")
                threads.append({"thread_id": thread_id, "before_digest": _digest(snapshot),
                                "after_preserved_digest": _dw_preserved_digest(snapshot),
                                "old_generation": thread["owner_generation"],
                                "old_owner_digest": _owner_digest(stored_owner),
                                "new_owner_digest": _owner_digest(current_target),
                                "summary": {"thread_id": thread_id,
                                            "cursor": thread["cursor"],
                                            "catchup_before": thread["catchup_before"],
                                            "catchup_max": thread["catchup_max"],
                                            "message_states": _counts(snapshot["messages"], "state"),
                                            "delivery_group_states": _counts(snapshot["groups"], "status"),
                                            "attempt_states": _counts(snapshot["attempts"], "state"),
                                            "uncertain_messages": sum(row["state"] == "uncertain"
                                                                       for row in snapshot["messages"]),
                                            "uncertain_groups": sum(row["status"] == "uncertain"
                                                                    for row in snapshot["groups"])}})
    timers = []
    if timer_ids:
        with contextlib.closing(_ro(hailing_db_path)) as timer_db:
            require({"timers", "timer_occurrences"}.issubset(_tables(timer_db)),
                    "Temporal Beacon state schema is incomplete")
            for timer_id in timer_ids:
                snapshot = _timer_snapshot(timer_db, timer_id)
                timer = snapshot["timer"]
                require((timer["harness"], timer["session"], timer["surface"], timer["workspace"],
                         timer["started_at"]) ==
                        (old_target["harness"], old_target["session"], old_target["surface"],
                         old_target["workspace"], old_target["started_at"]),
                        f"Temporal Beacon timer {timer_id} is not owned by the exact old generation")
                require(timer["canonical_worktree"] == worktree,
                        f"Temporal Beacon timer {timer_id} belongs to a different canonical worktree")
                require(temporal_beacon.TemporalBeacon.owner_key(old_target, worktree) == timer["owner_key"],
                        f"Temporal Beacon timer {timer_id} owner key is inconsistent")
                timers.append({"timer_id": timer_id, "before_digest": _digest(snapshot),
                               "after_preserved_digest": _timer_preserved_digest(snapshot),
                               "old_owner_digest": _owner_digest(old_target),
                               "new_owner_digest": _owner_digest(current_target),
                               "old_owner_tuple": _target_tuple(old_target, timer["canonical_worktree"]),
                               "current_owner_tuple": _target_tuple(current_target, worktree),
                               "new_owner_key": temporal_beacon.TemporalBeacon.owner_key(
                                   current_target, worktree),
                               "summary": {"timer_id": timer_id,
                                           "status": timer["status"],
                                           "schedule": timer["schedule"],
                                           "due_at": timer["due_at"],
                                           "until_at": timer["until_at"],
                                           "request_id_present": timer["request_id"] is not None,
                                           "occurrence_states": _counts(snapshot["occurrences"], "status"),
                                           "uncertain_occurrences": sum(row["status"] == "uncertain"
                                                                        for row in snapshot["occurrences"]),
                                           "latest_occurrence": (snapshot["occurrences"][-1]["id"]
                                                                 if snapshot["occurrences"] else None)}})
    require(threads or timers, "Select at least one exact Discord Watch thread or Temporal Beacon timer")
    old_worktree = "not_recorded_by_hf_or_dw"
    if old_target["harness"] == "opencode":
        try:
            old_worktree = str(Path(old_target["worktree_directory"]).expanduser().resolve(strict=True))
        except (OSError, RuntimeError):
            raise ValueError("Stored OpenCode owner worktree is unavailable") from None
    return {"schema_version": 1, "operation_id": operation_id,
            "operation": "explicit_current_generation_custody_adoption",
            "continuity": "not_established",
            "approval_reference": approval_reference,
            "old_owner": old_target, "current_owner": current_target,
            "old_owner_tuple": _target_tuple(old_target, old_worktree),
            "current_owner_tuple": _target_tuple(current_target, worktree),
            "hailing": hailing, "discord_threads": threads, "temporal_timers": timers,
            "preserved": ["original Hailing Frequencies cutoff and receipt history",
                          "Discord messages, attachments, cursors, catch-up, groups and attempts",
                          "Temporal timer and occurrence identifiers, cadence, continuation, deadlines and status"],
            "limitations": ["old and current generations are distinct custody identities",
                            "historical process continuity is unavailable and is not claimed",
                            "approval reference is cooperative audit evidence, not authenticated proof"]}


def _normalize_inputs(operation_id, thread_ids, timer_ids, approval_reference):
    try:
        normalized_id = str(uuid.UUID(operation_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Operation ID must be a canonical UUID") from None
    require(normalized_id == operation_id.lower(), "Operation ID must use canonical UUID spelling")
    require(all(isinstance(item, str) and re.fullmatch(r"[1-9][0-9]{0,24}", item)
                for item in thread_ids), "Discord thread IDs must be valid snowflakes")
    threads = sorted(set(thread_ids), key=lambda item: (len(item), item))
    try:
        timers = sorted({str(uuid.UUID(item)) for item in timer_ids})
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Temporal timer IDs must be UUIDs") from None
    require(len(threads) == len(thread_ids) and len(timers) == len(timer_ids),
            "Selected resource IDs must not be duplicated")
    require(len(threads) + len(timers) <= MAX_ADOPTION_RESOURCES,
            f"Select at most {MAX_ADOPTION_RESOURCES} Discord threads and timers per operation")
    require(isinstance(approval_reference, str) and 1 <= len(approval_reference.strip()) <= 512,
            "Approval reference is required and must be at most 512 characters")
    approval_reference = approval_reference.strip()
    return normalized_id, threads, timers, approval_reference


def _read_operation(app, operation_id):
    path = app.hailing_state / "receipts.sqlite3"
    with contextlib.closing(_ro(path)) as db:
        require("custody_adoption_operations" in _tables(db), "Custody adoption operation is not prepared")
        row = db.execute("SELECT plan_json,plan_digest,approval_reference,state,accepted_owner_json "
                         "FROM custody_adoption_operations WHERE operation_id=?", (operation_id,)).fetchone()
    require(row is not None, "Custody adoption operation is not prepared")
    try:
        plan = json.loads(row[0])
    except (ValueError, TypeError):
        raise ValueError("Custody adoption plan record is malformed") from None
    require(_digest(plan) == row[1] and plan["operation_id"] == operation_id,
            "Custody adoption plan digest is invalid")
    return plan, row[1], row[2], row[3], row[4]


def _check_digest(expected, supplied):
    require(isinstance(supplied, str) and supplied == expected,
            "Exact custody adoption plan digest is required")


def _owner_acceptance(app, plan):
    current_target, worktree = _hook_current(app, plan["old_owner"])
    require(current_target == plan["current_owner"] and worktree == plan["current_owner_tuple"]["worktree"],
            "Current receiving owner differs from the immutable adoption plan")
    return {"owner": current_target, "worktree": worktree,
            "guard": "existing exact native CMUX and hook binding",
            "continuity": "not_established"}


def _assert_accepted_owner(app, plan, accepted_owner):
    require(_owner_acceptance(app, plan) == accepted_owner,
            "Current owner changed after accepting the adoption plan")


def _record_operation(app, plan, plan_digest, approval_reference):
    monitor = hf.Monitor(app.hailing_state)
    with contextlib.closing(monitor.database()) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT plan_json,plan_digest,approval_reference,state "
                              "FROM custody_adoption_operations WHERE operation_id=?",
                              (plan["operation_id"],)).fetchone()
        if existing:
            require(existing[0] == _canonical(plan) and existing[1] == plan_digest and
                    existing[2] == approval_reference,
                    "Operation ID already names a different immutable plan")
        else:
            now = time.time()
            db.execute("INSERT INTO custody_adoption_operations(operation_id,plan_json,plan_digest,"
                       "approval_reference,state,created_at,updated_at) VALUES(?,?,?,?,'prepared',?,?)",
                       (plan["operation_id"], _canonical(plan), plan_digest,
                        approval_reference, now, now))
        db.commit()


def _accept_operation(app, operation_id, plan_digest):
    plan, digest, approval_reference, state, accepted = _read_operation(app, operation_id)
    _check_digest(digest, plan_digest)
    require(state in ("prepared", "accepted"),
            "Only a prepared operation can receive current-owner acceptance")
    current_acceptance = _owner_acceptance(app, plan)
    if state == "accepted":
        require(json.loads(accepted) == current_acceptance,
                "Operation was already accepted by a different current owner")
        return {"operation_id": operation_id, "plan_digest": digest,
                "state": "accepted", "idempotent": True,
                "continuity": "not_established"}
    now = time.time()
    with contextlib.closing(hf.Monitor(app.hailing_state).database()) as db:
        db.execute("BEGIN IMMEDIATE")
        updated = db.execute("UPDATE custody_adoption_operations SET state='accepted',"
                             "accepted_owner_json=?,accepted_at=?,updated_at=? WHERE operation_id=? "
                             "AND plan_digest=? AND state='prepared'",
                             (_canonical(current_acceptance), now, now, operation_id, digest))
        require(updated.rowcount == 1, "Operation changed before current-owner acceptance")
        db.commit()
    return {"operation_id": operation_id, "plan_digest": digest,
            "state": "accepted", "continuity": "not_established"}


@contextlib.contextmanager
def _operation_locks(app, plan):
    names = [app.hailing_state / "monitor.lock", app.hailing_state / "timers.lock"]
    if plan["discord_threads"]:
        names.extend((app.directory / "state.lock", app.directory / "receive.lock"))
        for item in plan["discord_threads"]:
            names.extend((app.directory / f"poll-{item['thread_id']}.lock",
                          app.directory / f"wake-{item['thread_id']}.lock"))
    streams = []
    try:
        for path in sorted(set(names), key=lambda item: str(item)):
            require(path.parent.is_dir() and not path.parent.is_symlink(),
                    "Adoption state directory is unavailable")
            require(not path.is_symlink(), "Refusing symlink operation lock")
            stream = path.open("a")
            os.chmod(path, 0o600)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stream.close()
                raise ValueError("A selected component operation is active; retry the same adoption") from None
            streams.append(stream)
        yield
    finally:
        for stream in reversed(streams):
            fcntl.flock(stream, fcntl.LOCK_UN)
            stream.close()


def _operation_db(app):
    return hf.Monitor(app.hailing_state).database()


def _central_hold_conflicts(journal, kind, resource_id, operation_id, digest):
    rows = journal.execute(
        "SELECT h.operation_id,h.plan_digest,o.plan_digest,o.state "
        "FROM custody_adoption_holds h LEFT JOIN custody_adoption_operations o "
        "USING(operation_id) WHERE h.resource_kind=? AND h.resource_id=?",
        (kind, resource_id)).fetchall()
    return any(not (row[0] == operation_id and row[1] == digest) and
               not (row[2] == row[1] and row[3] == "complete") for row in rows)


def _ensure_operation_holds(app, plan, digest):
    with contextlib.ExitStack() as stack:
        journal = stack.enter_context(contextlib.closing(_operation_db(app)))
        journal.execute("BEGIN IMMEDIATE")
        row = journal.execute("SELECT plan_digest,state FROM custody_adoption_operations "
                              "WHERE operation_id=?", (plan["operation_id"],)).fetchone()
        require(row == (digest, "accepted") or row == (digest, "applying"),
                "Operation is not accepted for apply")
        resources = [("hailing_session", plan["old_owner"]["session"])]
        resources.extend(("discord_thread", item["thread_id"]) for item in plan["discord_threads"])
        resources.extend(("temporal_timer", item["timer_id"]) for item in plan["temporal_timers"])
        for kind, resource_id in resources:
            require(not _central_hold_conflicts(journal, kind, resource_id,
                                                plan["operation_id"], digest),
                    f"A {kind} resource has a competing custody hold")
        dw = None
        stale_local_holds = []
        if plan["discord_threads"]:
            dw = stack.enter_context(contextlib.closing(app.database(create=False)))
            dw.execute("BEGIN IMMEDIATE")
            for thread in plan["discord_threads"]:
                existing = dw.execute("SELECT operation_id,plan_digest FROM custody_adoption_holds "
                                      "WHERE thread_id=?", (thread["thread_id"],)).fetchall()
                for hold_operation, hold_digest in existing:
                    hold = journal.execute("SELECT plan_digest,state FROM custody_adoption_operations "
                                           "WHERE operation_id=?", (hold_operation,)).fetchone()
                    if hold == (hold_digest, "complete"):
                        stale_local_holds.append((hold_operation, thread["thread_id"]))
                        continue
                    require((hold_operation, hold_digest) == (plan["operation_id"], digest),
                            "A Discord Watch thread has a competing custody hold")
        now = time.time()
        journal.execute("UPDATE custody_adoption_operations SET state='applying',"
                        "apply_started_at=COALESCE(apply_started_at,?),updated_at=?,last_error=NULL "
                        "WHERE operation_id=? AND plan_digest=?",
                        (now, now, plan["operation_id"], digest))
        for kind, resource_id in resources:
            journal.execute("INSERT OR IGNORE INTO custody_adoption_holds "
                            "(operation_id,resource_kind,resource_id,plan_digest) VALUES(?,?,?,?)",
                            (plan["operation_id"], kind, resource_id, digest))
        journal.commit()
        if dw is not None:
            for stale_operation, thread_id in stale_local_holds:
                dw.execute("DELETE FROM custody_adoption_holds WHERE operation_id=? AND thread_id=?",
                           (stale_operation, thread_id))
            for thread in plan["discord_threads"]:
                dw.execute("INSERT OR IGNORE INTO custody_adoption_holds(operation_id,thread_id,plan_digest) "
                           "VALUES(?,?,?)", (plan["operation_id"], thread["thread_id"], digest))
            dw.commit()


def _event_matches(row, plan, resource, kind):
    return (row is not None and row[0] == plan["operation_id"] and row[1] == _digest(plan) and
            row[2] == resource["old_owner_digest"] and row[3] == resource["new_owner_digest"] and
            row[4] == resource["before_digest"] and row[5] == resource["after_preserved_digest"])


def _record_journal_event(app, plan, digest, kind, resource_id, resource):
    with contextlib.closing(_operation_db(app)) as journal:
        journal.execute("BEGIN IMMEDIATE")
        existing = journal.execute("SELECT operation_id,plan_digest,old_owner_digest,new_owner_digest,"
                                    "before_digest,after_preserved_digest FROM custody_adoption_events "
                                    "WHERE operation_id=? AND resource_kind=? AND resource_id=?",
                                    (plan["operation_id"], kind, resource_id)).fetchone()
        if existing:
            require(_event_matches(existing, plan, resource, kind),
                    f"{kind} journal event conflicts with the accepted adoption plan")
        else:
            journal.execute("INSERT INTO custody_adoption_events(operation_id,resource_kind,resource_id,"
                            "plan_digest,old_owner_digest,new_owner_digest,before_digest,"
                            "after_preserved_digest,committed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            (plan["operation_id"], kind, resource_id, digest,
                             resource["old_owner_digest"], resource["new_owner_digest"],
                             resource["before_digest"], resource["after_preserved_digest"], time.time()))
        journal.commit()


def _apply_hailing(app, plan, digest, accepted_owner):
    config_path = app.hailing_state / "config.json"
    hailing_db_path = app.hailing_state / "receipts.sqlite3"
    require(not config_path.is_symlink(), "Refusing symlink Hailing Frequencies config")
    with contextlib.closing(_ro(hailing_db_path)) as db:
        history_digest, history = _history_digest(db, plan["old_owner"]["session"])
    require(history_digest == plan["hailing"]["history_digest"],
            "Hailing Frequencies receipts changed after the adoption plan")
    raw = config_path.read_bytes()
    current_digest = _file_digest(raw)
    if current_digest == plan["hailing"]["config_before_digest"]:
        config = json.loads(raw)
        matches = [row for row in config["targets"] if row.get("session") == plan["old_owner"]["session"]
                   and row.get("harness") == plan["old_owner"]["harness"]]
        require(len(matches) == 1 and matches[0] == plan["hailing"]["old_target"],
                "Hailing Frequencies owner changed after the adoption plan")
        for target in config["targets"]:
            if target.get("session") == plan["old_owner"]["session"] and target.get("harness") == plan["old_owner"]["harness"]:
                target["started_at"] = plan["current_owner"]["started_at"]
                break
        rendered = (json.dumps(config, indent=2) + "\n").encode("utf-8")
        require(_file_digest(rendered) == plan["hailing"]["config_after_digest"],
                "Planned Hailing Frequencies config does not match the immutable plan")
        _assert_accepted_owner(app, plan, accepted_owner)
        hf.atomic_json(config_path, config)
    else:
        require(current_digest == plan["hailing"]["config_after_digest"],
                "Hailing Frequencies config changed during adoption; reconcile manually")
    current_config = json.loads(config_path.read_text(encoding="utf-8"))
    current_target = next((row for row in current_config["targets"]
                           if row.get("session") == plan["current_owner"]["session"]
                           and row.get("harness") == plan["current_owner"]["harness"]), None)
    require(current_target == plan["hailing"]["new_target"],
            "Hailing Frequencies after-state differs from the accepted owner")
    after_digest = _hailing_projection(app.binding_snapshot(current_target), history)
    require(after_digest == plan["hailing"]["after_preserved_digest"],
            "Hailing Frequencies preserved-state digest differs from the plan")
    with contextlib.closing(_operation_db(app)) as journal:
        journal.execute("BEGIN IMMEDIATE")
        existing = journal.execute("SELECT operation_id,plan_digest,old_owner_digest,new_owner_digest,"
                                    "before_digest,after_preserved_digest FROM custody_adoption_events "
                                    "WHERE operation_id=? AND resource_kind='hailing_session' AND resource_id=?",
                                    (plan["operation_id"], plan["old_owner"]["session"])).fetchone()
        resource = {"old_owner_digest": _owner_digest(plan["old_owner"]),
                    "new_owner_digest": _owner_digest(plan["current_owner"]),
                    "before_digest": plan["hailing"]["before_digest"],
                    "after_preserved_digest": after_digest}
        if existing:
            require(_event_matches(existing, plan, resource, "hailing_session"),
                    "Hailing Frequencies adoption event conflicts with the accepted plan")
        else:
            journal.execute("INSERT INTO custody_adoption_events(operation_id,resource_kind,resource_id,"
                            "plan_digest,old_owner_digest,new_owner_digest,before_digest,"
                            "after_preserved_digest,committed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            (plan["operation_id"], "hailing_session", plan["old_owner"]["session"],
                             digest, resource["old_owner_digest"], resource["new_owner_digest"],
                             resource["before_digest"], after_digest, time.time()))
        journal.commit()


def _apply_discord(app, plan, digest, resource, accepted_owner):
    _assert_accepted_owner(app, plan, accepted_owner)
    with contextlib.closing(app.database(create=False)) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT operation_id,plan_digest,old_owner_digest,new_owner_digest,"
                              "before_digest,after_preserved_digest FROM ownership_events "
                              "WHERE operation_id=? AND thread_id=?",
                              (plan["operation_id"], resource["thread_id"])).fetchone()
        current = _dw_snapshot(db, resource["thread_id"])
        thread = current["thread"]
        expected_new_generation = resource["old_generation"] + 1
        if existing:
            require(_event_matches(existing, plan, resource, "discord_thread"),
                    "Discord Watch adoption event conflicts with the accepted plan")
            require(json.loads(thread["owner_json"]) == plan["current_owner"] and
                    thread["owner_generation"] == expected_new_generation and
                    _dw_preserved_digest(current, plan["operation_id"]) == resource["after_preserved_digest"],
                    "Discord Watch committed state differs from the recorded adoption event")
            db.commit()
        else:
            require(_digest(current) == resource["before_digest"] and
                    json.loads(thread["owner_json"]) == plan["old_owner"] and
                    thread["owner_generation"] == resource["old_generation"],
                    "Discord Watch thread changed after the adoption plan")
            _assert_accepted_owner(app, plan, accepted_owner)
            new_generation = expected_new_generation
            new_owner_json = _canonical(plan["current_owner"])
            before_json = thread["owner_json"]
            updated = db.execute("UPDATE threads SET owner_json=?,owner_generation=?,updated_at=? "
                                 "WHERE thread_id=? AND owner_json=? AND owner_generation=?",
                                 (new_owner_json, new_generation, time.time(), resource["thread_id"],
                                  before_json, resource["old_generation"]))
            require(updated.rowcount == 1, "Discord Watch owner changed before adoption commit")
            db.execute("UPDATE messages SET owner_generation=? WHERE thread_id=? AND group_id IS NULL "
                       "AND state!='received'", (new_generation, resource["thread_id"]))
            old_json, new_json = _canonical(plan["old_owner"]), _canonical(plan["current_owner"])
            db.execute("INSERT INTO ownership_events(thread_id,from_session,to_session,at,owner_generation,"
                       "event_type,operation_id,plan_digest,from_owner_json,to_owner_json,old_owner_digest,"
                       "new_owner_digest,before_digest,after_preserved_digest,from_worktree,to_worktree,continuity) "
                       "VALUES(?,?,?,?,?,'custody_adoption',?,?,?,?,?,?,?,?,?,?,?)",
                       (resource["thread_id"], plan["old_owner"]["session"],
                        plan["current_owner"]["session"], time.time(), new_generation,
                        plan["operation_id"], digest, old_json, new_json,
                        resource["old_owner_digest"], resource["new_owner_digest"],
                        resource["before_digest"], resource["after_preserved_digest"],
                        plan["old_owner_tuple"]["worktree"], plan["current_owner_tuple"]["worktree"],
                        "not_established"))
            after = _dw_snapshot(db, resource["thread_id"])
            require(_dw_preserved_digest(after, plan["operation_id"]) == resource["after_preserved_digest"],
                    "Discord Watch adoption changed preserved messages, cursors, groups or attempts")
            db.commit()
    _record_journal_event(app, plan, digest, "discord_thread", resource["thread_id"], resource)


def _apply_timer(app, plan, digest, resource, accepted_owner):
    _assert_accepted_owner(app, plan, accepted_owner)
    with contextlib.closing(_operation_db(app)) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT operation_id,plan_digest,old_owner_digest,new_owner_digest,"
                              "before_digest,after_preserved_digest FROM custody_adoption_events "
                              "WHERE operation_id=? AND resource_kind='temporal_timer' AND resource_id=?",
                              (plan["operation_id"], resource["timer_id"])).fetchone()
        current = _timer_snapshot(db, resource["timer_id"])
        timer = current["timer"]
        if existing:
            require(_event_matches(existing, plan, resource, "temporal_timer"),
                    "Temporal Beacon adoption event conflicts with the accepted plan")
            require((timer["owner_key"], timer["harness"], timer["session"], timer["surface"],
                     timer["workspace"], timer["started_at"]) ==
                    (resource["new_owner_key"], plan["current_owner"]["harness"],
                     plan["current_owner"]["session"], plan["current_owner"]["surface"],
                     plan["current_owner"]["workspace"], plan["current_owner"]["started_at"]) and
                    _timer_preserved_digest(current) == resource["after_preserved_digest"],
                    "Temporal Beacon committed state differs from its adoption event")
            db.commit()
            return
        require(_digest(current) == resource["before_digest"],
                "Temporal Beacon timer or occurrences changed after the adoption plan")
        _assert_accepted_owner(app, plan, accepted_owner)
        updated = db.execute("UPDATE timers SET owner_key=?,harness=?,session=?,surface=?,workspace=?,"
                             "started_at=?,updated_at=? WHERE id=? AND owner_key=? AND started_at=?",
                             (resource["new_owner_key"], plan["current_owner"]["harness"],
                              plan["current_owner"]["session"], plan["current_owner"]["surface"],
                              plan["current_owner"]["workspace"], plan["current_owner"]["started_at"],
                              time.time(), resource["timer_id"], timer["owner_key"], timer["started_at"]))
        require(updated.rowcount == 1, "Temporal Beacon timer owner changed before adoption commit")
        after = _timer_snapshot(db, resource["timer_id"])
        require(_timer_preserved_digest(after) == resource["after_preserved_digest"],
                "Temporal Beacon adoption changed the cadence or occurrence state")
        db.execute("INSERT INTO custody_adoption_events(operation_id,resource_kind,resource_id,"
                   "plan_digest,old_owner_digest,new_owner_digest,before_digest,after_preserved_digest,"
                   "committed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (plan["operation_id"], "temporal_timer", resource["timer_id"], digest,
                    resource["old_owner_digest"], resource["new_owner_digest"],
                    resource["before_digest"], resource["after_preserved_digest"], time.time()))
        db.commit()


def _apply_operation(app, operation_id, plan_digest):
    plan, digest, _reference, state, accepted_raw = _read_operation(app, operation_id)
    _check_digest(digest, plan_digest)
    require(state in ("accepted", "applying", "complete"),
            "Current-owner acceptance is required before apply")
    if state == "complete":
        return {"operation_id": operation_id, "plan_digest": digest,
                "state": "complete", "idempotent": True,
                "continuity": "not_established"}
    require(accepted_raw, "Current-owner acceptance record is missing")
    accepted_owner = json.loads(accepted_raw)
    with _operation_locks(app, plan):
        current_acceptance = _owner_acceptance(app, plan)
        require(current_acceptance == accepted_owner,
                "Current owner changed after accepting the adoption plan")
        if state == "accepted":
            candidate = _build_plan(app, operation_id,
                                    [row["thread_id"] for row in plan["discord_threads"]],
                                    [row["timer_id"] for row in plan["temporal_timers"]],
                                    plan["approval_reference"])
            require(_digest(candidate) == digest,
                    "Selected state changed after acceptance; adoption was not started")
        _ensure_operation_holds(app, plan, digest)
        try:
            _assert_accepted_owner(app, plan, accepted_owner)
            if state == "accepted":
                candidate = _build_plan(app, operation_id,
                                        [row["thread_id"] for row in plan["discord_threads"]],
                                        [row["timer_id"] for row in plan["temporal_timers"]],
                                        plan["approval_reference"])
                require(_digest(candidate) == digest,
                        "Selected state changed before the first adoption commit")
            _apply_hailing(app, plan, digest, accepted_owner)
            for resource in plan["discord_threads"]:
                _apply_discord(app, plan, digest, resource, accepted_owner)
            for resource in plan["temporal_timers"]:
                _apply_timer(app, plan, digest, resource, accepted_owner)
            with contextlib.closing(_operation_db(app)) as db:
                db.execute("BEGIN IMMEDIATE")
                now = time.time()
                events = db.execute("SELECT resource_kind,resource_id,plan_digest FROM custody_adoption_events "
                                    "WHERE operation_id=?", (operation_id,)).fetchall()
                expected = {("hailing_session", plan["old_owner"]["session"])}
                expected.update(("discord_thread", row["thread_id"]) for row in plan["discord_threads"])
                expected.update(("temporal_timer", row["timer_id"]) for row in plan["temporal_timers"])
                require({(kind, resource_id) for kind, resource_id, _ in events} == expected and
                        all(row[2] == digest for row in events),
                        "Adoption component event set is incomplete or inconsistent")
                db.execute("UPDATE custody_adoption_operations SET state='complete',updated_at=?,last_error=NULL "
                           "WHERE operation_id=? AND plan_digest=? AND state='applying'",
                           (now, operation_id, digest))
                db.commit()
            if plan["discord_threads"]:
                with contextlib.closing(app.database(create=False)) as dw:
                    dw.execute("DELETE FROM custody_adoption_holds WHERE operation_id=? AND plan_digest=?",
                               (operation_id, digest))
                    dw.commit()
        except BaseException as error:
            with contextlib.closing(_operation_db(app)) as db:
                db.execute("UPDATE custody_adoption_operations SET last_error=?,updated_at=? "
                           "WHERE operation_id=? AND plan_digest=? AND state='applying'",
                           (str(error)[:500], time.time(), operation_id, digest))
                db.commit()
            raise
    return {"operation_id": operation_id, "plan_digest": digest,
            "state": "complete", "continuity": "not_established",
            "discord_threads": [row["thread_id"] for row in plan["discord_threads"]],
            "temporal_timers": [row["timer_id"] for row in plan["temporal_timers"]],
            "hailing_session": plan["old_owner"]["session"]}


def run(app, args):
    if args.adoption_action == "preview":
        operation_id, threads, timers, reference = _normalize_inputs(
            args.operation_id, args.thread, args.timer_id, args.approval_reference)
        plan = _build_plan(app, operation_id, threads, timers, reference)
        digest = _digest(plan)
        return {"operation_id": operation_id, "plan_digest": digest,
                "state": "preview_only", "continuity": "not_established",
                "scope": {"hailing_session": plan["old_owner"]["session"],
                          "discord_threads": threads, "temporal_timers": timers},
                "old_owner_tuple": plan["old_owner_tuple"],
                "current_owner_tuple": plan["current_owner_tuple"],
                "resource_summaries": {"hailing": plan["hailing"]["summary"],
                                       "discord_threads": [row["summary"] for row in plan["discord_threads"]],
                                       "temporal_timers": [row["summary"] for row in plan["temporal_timers"]]},
                "before_digests": {"hailing": plan["hailing"]["before_digest"],
                                   "discord_threads": [row["before_digest"] for row in plan["discord_threads"]],
                                   "temporal_timers": [row["before_digest"] for row in plan["temporal_timers"]]},
                "preservation_digests": {"hailing": plan["hailing"]["after_preserved_digest"],
                                         "discord_threads": [row["after_preserved_digest"] for row in plan["discord_threads"]],
                                         "temporal_timers": [row["after_preserved_digest"] for row in plan["temporal_timers"]]},
                "approval_reference_sha256": _file_digest(reference.encode("utf-8")),
                "dry_run": True, "writes": False, "sends": False,
                "limitations": plan["limitations"]}
    if args.adoption_action == "prepare":
        operation_id, threads, timers, reference = _normalize_inputs(
            args.operation_id, args.thread, args.timer_id, args.approval_reference)
        require(args.plan_digest, "Exact preview plan digest is required")
        plan = _build_plan(app, operation_id, threads, timers, reference)
        digest = _digest(plan)
        _check_digest(digest, args.plan_digest)
        _record_operation(app, plan, digest, reference)
        return {"operation_id": operation_id, "plan_digest": digest,
                "state": "prepared", "approval_reference_sha256": _file_digest(reference.encode("utf-8")),
                "continuity": "not_established"}
    if args.adoption_action == "accept":
        return _accept_operation(app, args.operation_id, args.plan_digest)
    if args.adoption_action == "apply":
        return _apply_operation(app, args.operation_id, args.plan_digest)
    if args.adoption_action == "status":
        plan, digest, reference, state, accepted = _read_operation(app, args.operation_id)
        _check_digest(digest, args.plan_digest)
        return {"operation_id": args.operation_id, "plan_digest": digest,
                "state": state, "accepted": bool(accepted),
                "approval_reference_sha256": _file_digest(reference.encode("utf-8")),
                "components": {"hailing_session": plan["old_owner"]["session"],
                               "discord_threads": [row["thread_id"] for row in plan["discord_threads"]],
                               "temporal_timers": [row["timer_id"] for row in plan["temporal_timers"]]},
                "continuity": "not_established"}
    raise ValueError("Unknown custody adoption action")
