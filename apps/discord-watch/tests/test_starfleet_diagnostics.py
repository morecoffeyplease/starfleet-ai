import contextlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import unittest

sys.dont_write_bytecode = True
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import discord_watch


def snapshot_tree(path):
    path = Path(path)
    if not path.exists():
        return None
    result = {}
    for item in sorted(path.rglob("*")):
        info = item.lstat()
        result[str(item.relative_to(path))] = (
            stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_size,
            info.st_mtime_ns,
            os.readlink(item) if stat.S_ISLNK(info.st_mode) else
            item.read_bytes() if stat.S_ISREG(info.st_mode) else None,
        )
    return result


def make_fixture(root):
    state = root / "state"
    hailing = root / "hailing"
    notify = root / "agent-notify"
    for directory in (state, hailing, notify):
        directory.mkdir(mode=0o700)
    session, surface, workspace = [str(uuid.uuid4()).upper() for _ in range(3)]
    caller = {"harness": "codex", "session": session,
              "surface": surface, "workspace": workspace}
    target = {**caller, "label": "fixture", "started_at": time.time(),
              "cutoff": time.time() + 3600}
    config = {"version": 1, "remind_after": 60, "escalate_after": 120,
              "cooldown": 30, "interval": 10, "targets": [target]}
    config_path = hailing / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    os.chmod(config_path, 0o600)
    env_path = notify / "env"
    env_path.write_text("DISCORD_BOT_TOKEN=synthetic_token_123456789012\n"
                        "DISCORD_USER_ID=123456789012345678\n", encoding="utf-8")
    os.chmod(env_path, 0o600)
    app = discord_watch.Watch(state, hailing, notify, cmux="unavailable-fixture-cmux")
    previous_umask = os.umask(0o077)
    try:
        with contextlib.closing(app.database()) as db:
            owner = {key: target[key] for key in
                     ("harness", "session", "surface", "workspace", "started_at")}
            db.execute("INSERT INTO threads(thread_id,owner_json,cursor,registered_at,updated_at) "
                       "VALUES(?,?,?,?,?)", ("123456789012345678", json.dumps(owner),
                                               "123456789012345678", time.time(), time.time()))
            generation = "a" * 32
            meta = {
                "last_worker_tick_at": json.dumps({"at": time.time(), "generation": generation}),
                "worker_sender_config": json.dumps({"generation": generation, "send": True,
                                                     "only_thread": None}),
                "last_api_success_at": str(time.time()),
            }
            db.executemany("INSERT INTO meta(key,value) VALUES(?,?)", meta.items())
            db.commit()
    finally:
        os.umask(previous_umask)
    return state, hailing, notify, caller


class DiscordWatchDiagnosticTests(unittest.TestCase):
    def test_healthy_diagnostic_is_read_only_and_uses_exact_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, hailing, notify, caller = make_fixture(root)
            before = snapshot_tree(root)
            report = discord_watch.discord_watch_diagnostic_report(state, hailing, notify, caller)
            self.assertEqual(report["checks"], {
                "configuration": "ready", "registration": "ready", "worker": "ready",
                "intended_delivery": "ready", "observed_delivery": "unknown",
            })
            self.assertEqual(snapshot_tree(root), before)
            for name in ("state.lock", "worker.lock", "supervise.lock", "receive.lock"):
                self.assertFalse((state / name).exists())

    def test_partial_wrong_owner_and_stale_worker_remain_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, hailing, notify, caller = make_fixture(root)
            wrong_owner = {**caller, "session": str(uuid.uuid4()).upper()}
            wrong = discord_watch.discord_watch_diagnostic_report(state, hailing, notify, wrong_owner)
            self.assertEqual(wrong["checks"]["registration"], "missing")
            self.assertEqual(wrong["checks"]["intended_delivery"], "missing")

            previous_umask = os.umask(0o077)
            try:
                app = discord_watch.Watch(state, hailing, notify)
                with contextlib.closing(app.database(create=False)) as db:
                    db.execute("UPDATE meta SET value=? WHERE key='worker_sender_config'",
                               (json.dumps({"generation": "b" * 32, "send": False,
                                            "only_thread": None}),))
                    db.execute("UPDATE meta SET value=? WHERE key='last_worker_tick_at'",
                               (json.dumps({"at": time.time() - 1000, "generation": "b" * 32}),))
                    db.commit()
            finally:
                os.umask(previous_umask)
            partial = discord_watch.discord_watch_diagnostic_report(state, hailing, notify, caller)
            self.assertEqual(partial["checks"]["registration"], "ready")
            self.assertEqual(partial["checks"]["worker"], "blocked")
            self.assertEqual(partial["checks"]["intended_delivery"], "blocked")

    def test_missing_malformed_and_sqlite_sidecars_do_not_change_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing"
            report = discord_watch.discord_watch_diagnostic_report(
                missing, root / "missing-hailing", root / "missing-notify")
            self.assertEqual(report["checks"]["configuration"], "missing")
            self.assertFalse(missing.exists())

            state = root / "malformed-state"
            hailing = root / "hailing"
            notify = root / "notify"
            for directory in (state, hailing, notify):
                directory.mkdir(mode=0o700)
            database = state / "discord-watch.sqlite3"
            database.write_text("not a sqlite database", encoding="utf-8")
            os.chmod(database, 0o600)
            before = snapshot_tree(root)
            malformed = discord_watch.discord_watch_diagnostic_report(state, hailing, notify)
            self.assertEqual(malformed["checks"]["configuration"], "blocked")
            self.assertEqual(snapshot_tree(root), before)

            sidecar = Path(str(database) + "-wal")
            sidecar.write_bytes(b"synthetic WAL")
            before = snapshot_tree(root)
            unknown = discord_watch.discord_watch_diagnostic_report(state, hailing, notify)
            self.assertEqual(unknown["checks"]["configuration"], "unknown")
            self.assertEqual(snapshot_tree(root), before)

    def test_cli_internal_mode_does_not_create_state_or_read_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "not-created"
            command = [sys.executable, str(APP / "discord_watch.py"),
                       "--state-dir", str(state), "--hailing-state", str(root / "no-hailing"),
                       "--notify-home", str(root / "no-notify"), "doctor", "--starfleet-json"]
            result = subprocess.run(command, input="{}", text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["checks"]["configuration"], "missing")
            self.assertFalse(state.exists())

    def test_symlink_database_is_blocked_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir(mode=0o700)
            target = root / "private.sqlite3"
            target.write_text("private synthetic marker", encoding="utf-8")
            os.chmod(target, 0o600)
            os.symlink(target, state / "discord-watch.sqlite3")
            before = snapshot_tree(root)
            report = discord_watch.discord_watch_diagnostic_report(
                state, root / "no-hailing", root / "no-notify")
            self.assertEqual(report["checks"]["configuration"], "blocked")
            self.assertEqual(snapshot_tree(root), before)


if __name__ == "__main__":
    unittest.main()
