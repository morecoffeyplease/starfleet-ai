import contextlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from replacement_custody import clear_predecessor
from temporal_beacon import TemporalBeacon


class ReplacementCustodyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        home = patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.hailing = self.home / "hailing"
        self.hailing.mkdir()
        self.discord = self.home / ".local/state/starfleet-ai/discord-watch"
        self.owner = str(uuid.uuid4()).upper()
        self.foreign = str(uuid.uuid4()).upper()
        self.app = SimpleNamespace(directory=self.hailing)
        self.predecessor = {"session": self.owner}

    def thread_store(self):
        self.discord.mkdir(parents=True)
        path = self.discord / "discord-watch.sqlite3"
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE threads(thread_id TEXT PRIMARY KEY, owner_json TEXT, is_open INTEGER, remote_closed INTEGER);
                CREATE TABLE messages(thread_id TEXT, state TEXT);
                CREATE TABLE delivery_groups(thread_id TEXT, status TEXT);
            """)
        return path

    def add_thread(self, db, session, thread_id, closed, remote_closed):
        db.execute("INSERT INTO threads VALUES (?,?,?,?)",
                   (thread_id, json.dumps({"session": session}), int(not closed), int(remote_closed)))

    def test_optional_uninitialized_stores_create_no_database_or_discord_directory(self):
        with clear_predecessor(self.app, self.predecessor):
            pass
        self.assertFalse((self.hailing / "receipts.sqlite3").exists())
        self.assertFalse(self.discord.exists())

    def test_timer_guard_blocks_active_exact_owner_and_releases_timer_lock(self):
        beacon = TemporalBeacon(self.app)
        with contextlib.closing(beacon.database()) as db:
            row = ("test-timer", "owner-key", "claude", self.owner.lower(), "surface", "workspace",
                   100, None, "private content", "after", None, 100, None, "active", None, None, 0, 0, "{}")
            db.execute("INSERT INTO timers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        with self.assertRaisesRegex(ValueError, "Temporal Beacon timer test-timer"):
            with clear_predecessor(self.app, self.predecessor):
                self.fail("Active timer allowed replacement")
        with beacon.locked(), contextlib.closing(beacon.database()) as db:
            db.execute("UPDATE timers SET status = 'completed'")
            db.execute("INSERT INTO timers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("foreign-timer", *row[1:3], self.foreign, *row[4:]))
        with clear_predecessor(self.app, self.predecessor):
            with self.assertRaisesRegex(ValueError, "operation owns timer state"):
                with beacon.locked():
                    self.fail("Timer lock was not retained")

    def test_discord_requires_local_and_remote_close_and_settled_replies(self):
        path = self.thread_store()
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            self.add_thread(db, self.owner.lower(), "exact-thread", closed=False, remote_closed=True)
            self.add_thread(db, self.foreign, "foreign-thread", closed=False, remote_closed=False)
        for local_open, remote_closed, message, group in (
            (1, 1, "received", "received"),
            (0, 0, "received", "received"),
            (0, 1, "pending", "received"),
            (0, 1, "received", "uncertain"),
        ):
            with self.subTest(local_open=local_open, remote_closed=remote_closed, message=message, group=group):
                with contextlib.closing(sqlite3.connect(path)) as db, db:
                    db.execute("UPDATE threads SET is_open=?,remote_closed=? WHERE thread_id='exact-thread'",
                               (local_open, remote_closed))
                    db.execute("DELETE FROM messages")
                    db.execute("DELETE FROM delivery_groups")
                    db.execute("INSERT INTO messages VALUES ('exact-thread',?)", (message,))
                    db.execute("INSERT INTO delivery_groups VALUES ('exact-thread',?)", (group,))
                baseline = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "Discord Watch thread exact-thread"):
                    with clear_predecessor(self.app, self.predecessor):
                        self.fail("Unresolved Discord custody allowed replacement")
                self.assertEqual(path.read_bytes(), baseline)
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE delivery_groups SET status='received'")
        baseline = path.read_bytes()
        with clear_predecessor(self.app, self.predecessor):
            pass
        self.assertEqual(path.read_bytes(), baseline)

    def test_discord_worker_cannot_commit_custody_change_during_swap(self):
        path = self.thread_store()
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            self.add_thread(db, self.owner, "exact-thread", closed=True, remote_closed=True)
        with contextlib.closing(sqlite3.connect(path, timeout=0)) as worker:
            with clear_predecessor(self.app, self.predecessor):
                worker.execute("INSERT INTO messages VALUES ('exact-thread','pending')")
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    worker.commit()
                worker.rollback()
            with worker:
                worker.execute("INSERT INTO messages VALUES ('exact-thread','pending')")

    def test_invalid_companion_database_holds(self):
        path = self.thread_store()
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("DROP TABLE delivery_groups")
        with self.assertRaisesRegex(ValueError, "Cannot verify Discord Watch custody"):
            with clear_predecessor(self.app, self.predecessor):
                self.fail("Incomplete Discord schema allowed replacement")
