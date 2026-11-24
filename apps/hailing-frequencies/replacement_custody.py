"""Preserve exact-session timer and Discord Watch custody during Hailing replacement."""
import contextlib
import fcntl
import os
from pathlib import Path
import sqlite3
import stat

from temporal_beacon import TemporalBeacon


def _owned_path_exists(path, acceptable_type, name):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not acceptable_type(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f"Cannot verify {name} ownership or file type")
    return True


@contextlib.contextmanager
def _read_database(path, name):
    if not _owned_path_exists(path, stat.S_ISREG, name):
        yield None
        return
    try:
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
            yield db
    except sqlite3.Error as exc:
        raise ValueError(f"Cannot verify {name} custody: {exc}") from None


@contextlib.contextmanager
def _discord_registration_lock(directory):
    path = directory / "state.lock"
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags, 0o600), "w") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Cannot verify Discord Watch state lock ownership or file type")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A Discord Watch registration operation is in progress; retry shortly") from None
        yield


def _check_timers(directory, session):
    with _read_database(directory / "receipts.sqlite3", "Temporal Beacon") as db:
        if db is None:
            return
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='timers'").fetchone()
        if exists is None:
            return
        blocked = db.execute(
            "SELECT id FROM timers WHERE upper(session) = ? AND status IS NOT 'completed' "
            "AND status IS NOT 'canceled' AND status IS NOT 'expired' ORDER BY id LIMIT 1",
            (session,),
        ).fetchone()
        if blocked:
            raise ValueError(f"Predecessor has an active or unresolved Temporal Beacon timer {blocked[0]}")


def _check_threads(db, session):
    blocked = db.execute(
        """SELECT thread_id FROM threads AS t
           WHERE json_type(t.owner_json, '$.session') IS NOT 'text'
              OR (upper(json_extract(t.owner_json, '$.session')) = ?
                  AND (t.is_open IS NOT 0 OR t.remote_closed IS NOT 1
                       OR EXISTS (SELECT 1 FROM messages AS m WHERE m.thread_id = t.thread_id
                                  AND m.state IS NOT 'received')
                       OR EXISTS (SELECT 1 FROM delivery_groups AS g WHERE g.thread_id = t.thread_id
                                  AND g.status IS NOT 'received')))
           ORDER BY thread_id LIMIT 1""",
        (session,),
    ).fetchone()
    if blocked:
        raise ValueError(f"Discord Watch thread {blocked[0]} has open, unresolved, or unverified custody")


@contextlib.contextmanager
def clear_predecessor(app, predecessor):
    """Hold companion checks through the caller's Hailing config commit."""
    discord_directory = Path.home() / ".local/state/starfleet-ai/discord-watch"
    with TemporalBeacon(app).locked():
        _check_timers(app.directory, predecessor["session"])
        if not _owned_path_exists(discord_directory, stat.S_ISDIR, "Discord Watch state"):
            yield
            return
        with _discord_registration_lock(discord_directory):
            with _read_database(discord_directory / "discord-watch.sqlite3", "Discord Watch") as db:
                if db is not None:
                    # Rollback journal readers prevent a worker commit until the config swap.
                    journal = db.execute("PRAGMA journal_mode").fetchone()[0]
                    if journal not in ("delete", "truncate", "persist"):
                        raise ValueError("Cannot verify Discord Watch custody under its current journal mode")
                    db.execute("BEGIN")
                    _check_threads(db, predecessor["session"])
                yield
