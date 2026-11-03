import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import discord_watch as dw
import hailing_frequencies as hf


THREAD = "100"
USER_ID = "500"
SESSION_A = "11111111-1111-4111-8111-111111111111".upper()
SESSION_B = "22222222-2222-4222-8222-222222222222".upper()
SURFACE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper()
SURFACE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb".upper()
WORKSPACE_A = "cccccccc-cccc-4ccc-8ccc-cccccccccccc".upper()
WORKSPACE_B = "dddddddd-dddd-4ddd-8ddd-dddddddddddd".upper()


def target(session=SESSION_A, surface=SURFACE_A, workspace=WORKSPACE_A, harness="codex", **extra):
    value = {"harness": harness, "session": session, "surface": surface,
             "workspace": workspace, "started_at": 1234.5,
             "label": "owner-label", "cutoff": 1234.5}
    value.update(extra)
    return value


def message(message_id, author_id=USER_ID, content="Configured user reply", author_name="Configured user", bot=False,
            webhook_id=None, attachments=None):
    return {"id": str(message_id), "author": {"id": str(author_id), "username": author_name,
                                                  "global_name": author_name, "bot": bot},
            "content": content, "timestamp": "2026-11-01T12:00:00.000000+00:00",
            "webhook_id": webhook_id, "attachments": attachments or []}


class FakeAPI:
    def __init__(self, rows=None, remote=None):
        self.rows = list(rows or [])
        self.remote = remote or {"id": THREAD, "type": 11,
                                 "thread_metadata": {"archived": False}, "applied_tags": []}
        self.fail = None
        self.calls = []

    def get(self, path, query=None):
        self.calls.append((path, query))
        if self.fail:
            raise self.fail
        if path.startswith("/channels/") and not path.endswith("/messages"):
            remote = dict(self.remote)
            remote["id"] = path.split("/")[2]
            return remote
        if path.endswith("/messages"):
            limit = int(query["limit"])
            if "after" in query:
                cursor = int(query["after"])
                selected = (row for row in self.rows if int(row["id"]) > cursor)
            else:
                cursor = int(query["before"])
                selected = (row for row in self.rows if int(row["id"]) < cursor)
            rows = sorted(selected, key=lambda row: int(row["id"]), reverse=True)
            return rows[:limit]
        raise AssertionError(f"Unexpected API path: {path}")


class FakeCmux:
    def __init__(self):
        self.fail_binding = None
        self.hold_submit = None
        self.uncertain = False
        self.prompts = []
        self.submit_count = 0
        self.lock = threading.Lock()
        self.on_submit = None
        self.entered = None
        self.release = None

    def binding(self, current):
        if self.fail_binding:
            raise ValueError(self.fail_binding)
        return True

    def submit(self, current, text, on_transport_start=None):
        self.binding(current)
        if self.hold_submit:
            raise hf.DeliveryHoldError(self.hold_submit)
        if on_transport_start:
            on_transport_start()
        with self.lock:
            self.submit_count += 1
            self.prompts.append(text)
        if self.entered is not None:
            self.entered.set()
            self.release.wait(timeout=3)
        if self.on_submit:
            self.on_submit()
        if self.uncertain:
            raise ValueError("CMUX did not confirm the submit")
        return {"submitted": True}


class FakeWatch(dw.Watch):
    def __init__(self, *args, api=None, cmux=None, now=None, **kwargs):
        self.api = api or FakeAPI()
        self.values = {"token": "private-test-token", "user_id": USER_ID, "closed_tag": "900"}
        self.fake_cmux = cmux or FakeCmux()
        self.now = now if now is not None else [1000.0]
        super().__init__(*args, clock=lambda: self.now[0], cmux_factory=lambda: self.fake_cmux, **kwargs)

    def read_credentials(self):
        return self.values, self.api


class DiscordWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.notify_home = self.root / "agent-notify"
        self.hailing = self.root / "hailing"
        self.notify_home.mkdir(mode=0o700)
        self.hailing.mkdir(mode=0o700)
        self.targets = [target()]
        self._write_hailing()
        self.api = FakeAPI()
        self.cmux = FakeCmux()
        self.clock = [1000.0]
        self.app = FakeWatch(self.state, self.hailing, self.notify_home, api=self.api,
                             cmux=self.cmux, now=self.clock)
        self.old_env = {key: os.environ.get(key) for key in (
            "CMUX_AGENT_LAUNCH_KIND", "CMUX_SURFACE_ID", "CMUX_WORKSPACE_ID",
            "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID")}
        os.environ["CMUX_AGENT_LAUNCH_KIND"] = "codex"
        os.environ["CMUX_SURFACE_ID"] = SURFACE_A
        os.environ["CMUX_WORKSPACE_ID"] = WORKSPACE_A
        os.environ["CODEX_THREAD_ID"] = SESSION_A
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def _write_hailing(self):
        path = self.hailing / "config.json"
        path.write_text(json.dumps({"version": 1, "targets": self.targets, "coordinator": None,
                                   "remind_after": 300, "escalate_after": 900,
                                   "cooldown": 600, "interval": 30}), encoding="utf-8")
        path.chmod(0o600)

    def register(self, after=None):
        return self.app.register(THREAD, after)

    def messages(self):
        with contextlib.closing(self.app.database(create=False)) as db:
            return db.execute("SELECT message_id,state,wake_attempts,body FROM messages WHERE thread_id=? "
                              "ORDER BY length(message_id),message_id", (THREAD,)).fetchall()

    def thread_cursor(self):
        with contextlib.closing(self.app.database(create=False)) as db:
            return db.execute("SELECT cursor FROM threads WHERE thread_id=?", (THREAD,)).fetchone()[0]

    def test_register_defaults_to_creation_and_ignores_legacy_cursor_without_proof(self):
        cursor_dir = self.notify_home / "state"
        cursor_dir.mkdir(mode=0o700)
        (cursor_dir / THREAD).write_text("150\n", encoding="ascii")
        (cursor_dir / THREAD).chmod(0o600)
        result = self.register()
        self.assertEqual(result["cursor"], THREAD)
        self.assertEqual(result["cursor_source"], "thread-created")
        self.targets[0]["label"] = "renamed harmlessly"
        self.targets[0]["cutoff"] = 9999
        self._write_hailing()
        self.assertTrue(self.app.managed(THREAD)["managed"])

    def test_register_accepts_only_an_explicit_valid_starting_cursor(self):
        result = self.register(after="150")
        self.assertEqual(result["cursor"], "150")
        self.assertEqual(result["cursor_source"], "explicit")
        self.api.remote["id"] = "200"
        with self.assertRaisesRegex(ValueError, "cannot precede"):
            self.app.register("200", after="199")

    def test_caller_without_session_env_uses_unique_surface_enrollment_and_binding(self):
        os.environ.pop("CODEX_THREAD_ID", None)
        self.assertEqual(self.register()["owner_session"], SESSION_A)

    def test_page_messages_and_cursor_roll_back_together_then_replay_after_restart(self):
        self.register()
        self.api.rows = [message("101"), message("102", content=7)]
        failed = self.app.scan()
        self.assertEqual(failed["threads"][0]["status"], "error")
        self.assertEqual(self.thread_cursor(), THREAD)
        self.assertEqual(self.messages(), [])

        self.api.rows = [message("101"), message("102", author_id="999", content="other")]
        self.clock[0] += dw.POLL_INTERVAL + 1
        self.app.scan()
        self.assertEqual(self.thread_cursor(), "102")
        self.assertEqual([row[0] for row in self.messages()], ["101"])
        restarted = FakeWatch(self.state, self.hailing, self.notify_home, api=self.api,
                              cmux=self.cmux, now=self.clock)
        restarted.scan()
        self.assertEqual([row[0] for row in self.messages()], ["101"])

        self.api.rows.append(message("103", content="second reply"))
        restarted.scan()
        self.assertEqual(self.thread_cursor(), "103")
        self.assertEqual([row[0] for row in self.messages()], ["101", "103"])

    def test_messages_are_filtered_to_configured_user_and_webhooks(self):
        self.register()
        self.api.rows = [message("101", author_id="999", content="other user"),
                         message("102", author_id=USER_ID, content="bot", bot=True),
                         message("103", author_id=USER_ID, content="webhook", webhook_id="77"),
                         message("104", author_id=USER_ID, content="Configured user")]
        self.app.scan()
        self.assertEqual([row[0] for row in self.messages()], ["104"])
        self.assertEqual(self.thread_cursor(), "104")

    def test_descending_api_pages_resume_durable_catchup_without_cursor_loss(self):
        self.register()
        self.api.rows = [message(str(message_id), content=f"reply {message_id}")
                         for message_id in range(101, 351)]
        with mock.patch.object(dw, "MAX_CATCHUP_PAGES", 2):
            partial = self.app.scan()
        self.assertTrue(partial["threads"][0]["catchup_pending"])
        self.assertEqual(self.thread_cursor(), THREAD)
        with contextlib.closing(self.app.database(create=False)) as db:
            boundary = db.execute("SELECT catchup_before,catchup_max FROM threads WHERE thread_id=?",
                                  (THREAD,)).fetchone()
        self.assertEqual(boundary, ("151", "350"))
        restarted = FakeWatch(self.state, self.hailing, self.notify_home, api=self.api,
                              cmux=self.cmux, now=self.clock)
        finished = restarted.scan()
        self.assertFalse(finished["threads"][0]["catchup_pending"])
        self.assertEqual(self.thread_cursor(), "350")
        self.assertEqual(len(self.messages()), 250)
        self.assertEqual(self.messages()[0][0], "101")
        self.assertEqual(self.messages()[-1][0], "350")

    def test_attachment_only_reply_preserves_filename_and_url_for_receive(self):
        self.register()
        url = "https://cdn.discordapp.com/attachments/100/101/screen.png"
        self.api.rows = [message("101", content="", attachments=[{"filename": "screen.png", "url": url}])]
        self.app.scan()
        output = io.StringIO()
        result = self.app.receive(THREAD, "101", output)
        self.assertEqual(result["received"], ["101"])
        self.assertIn("[discord:100:101] Configured user: ", output.getvalue())
        self.assertIn("[attachment] screen.png: " + url, output.getvalue())

    def test_held_input_does_not_mark_transport_or_consume_attempt(self):
        self.register()
        self.api.rows = [message("101")]
        self.app.scan()
        self.cmux.hold_submit = "Composer has a human draft"
        result = self.app.scan(send=True)
        self.assertEqual(result["threads"][0]["wake"]["state"], "pending")
        row = self.messages()[0]
        self.assertEqual(row[1:3], ("pending", 0))
        self.assertIn("last_error", self.app.status()["threads"][0]["recent"][0])
        self.assertEqual(self.cmux.submit_count, 0)

    def test_uncertain_transport_is_not_retried_and_explicit_receive_resolves_it(self):
        self.register()
        self.api.rows = [message("101", content="private reply body")]
        self.app.scan()
        self.cmux.uncertain = True
        self.app.scan(send=True)
        self.assertEqual(self.messages()[0][1], "uncertain")
        self.assertNotIn("private reply body", self.cmux.prompts[0])
        self.assertIn("--state-dir " + str(self.state), self.cmux.prompts[0])
        self.assertIn("receive --thread 100 --message 101", self.cmux.prompts[0])
        self.cmux.uncertain = False
        self.app.scan(send=True)
        self.assertEqual(self.cmux.submit_count, 1)
        output = io.StringIO()
        receipt = self.app.receive(THREAD, "101", output)
        self.assertEqual(receipt["received"], ["101"])
        self.assertIn("[discord:100:101] Configured user: private reply body", output.getvalue())
        self.assertEqual(self.messages()[0][1], "received")

    def test_concurrent_scans_submit_once_and_receive_does_not_get_overwritten(self):
        self.register()
        self.api.rows = [message("101", content="body")]
        self.app.scan()
        self.cmux.entered = threading.Event()
        self.cmux.release = threading.Event()
        results = []
        first = threading.Thread(target=lambda: results.append(self.app.scan(send=True)))
        first.start()
        self.assertTrue(self.cmux.entered.wait(timeout=2))
        second = self.app.scan(send=True)
        self.assertEqual(self.cmux.submit_count, 1)
        self.assertEqual(second["threads"][0]["wake"]["state"], "wake_busy")
        self.cmux.release.set()
        first.join(timeout=3)
        self.assertFalse(first.is_alive())
        self.assertEqual(self.messages()[0][1], "awaiting_receipt")

    def test_receive_during_transport_keeps_receipt_state(self):
        self.register()
        self.api.rows = [message("101", content="body")]
        self.app.scan()
        output = io.StringIO()
        self.cmux.on_submit = lambda: self.app.receive(THREAD, "101", output)
        result = self.app.scan(send=True)
        self.assertEqual(self.messages()[0][1], "received")
        self.assertEqual(result["threads"][0]["wake"]["state"], "received")

    def test_remote_archive_closes_registration_without_waking_or_dropping_reply(self):
        self.register()
        self.api.rows = [message("101")]
        self.app.scan()
        self.api.remote["thread_metadata"]["archived"] = True
        report = self.app.scan(send=True)
        self.assertEqual(report["threads"][0]["status"], "closed_remote")
        self.assertEqual(self.cmux.submit_count, 0)
        self.assertEqual(self.messages()[0][1], "pending")
        with self.assertRaisesRegex(ValueError, "closed"):
            self.app.managed(THREAD)

    def test_transfer_requires_exact_from_session_and_preserves_unreceived_replies(self):
        self.register()
        self.api.rows = [message("101")]
        self.app.scan()
        self.app.scan(send=True)
        self.targets.append(target(SESSION_B, SURFACE_B, WORKSPACE_B))
        self._write_hailing()
        os.environ["CMUX_SURFACE_ID"] = SURFACE_B
        os.environ["CMUX_WORKSPACE_ID"] = WORKSPACE_B
        os.environ["CODEX_THREAD_ID"] = SESSION_B
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.app.transfer(THREAD, SESSION_B)
        result = self.app.transfer(THREAD, SESSION_A)
        self.assertEqual(result["to_session"], SESSION_B)
        self.assertEqual(self.messages()[0][1:3], ("pending", 0))
        self.assertEqual(self.messages()[0][3], "Configured user reply")
        self.assertEqual(self.app.managed(THREAD)["owner_session"], SESSION_B)

    def test_api_rate_limit_details_are_retained_without_token_leak(self):
        import io as io_module
        headers = {"Retry-After": "4.25"}
        body = json.dumps({"message": "slow down", "code": 20028,
                           "retry_after": 4.25, "global": True}).encode("utf-8")
        error = urllib.error.HTTPError("https://discord.com/api/v10/channels/100", 429,
                                       "Too Many Requests", headers, io_module.BytesIO(body))
        api = dw.Discord("do-not-print-this-token", opener=lambda request, timeout: (_ for _ in ()).throw(error))
        with self.assertRaises(dw.DiscordAPIError) as caught:
            api.get("/channels/100")
        record = caught.exception.record()
        self.assertEqual(record["http_status"], 429)
        self.assertEqual(record["discord_code"], 20028)
        self.assertEqual(record["retry_after"], 4.25)
        self.assertTrue(record["global_rate_limit"])
        self.assertNotIn("do-not-print-this-token", str(caught.exception))

    def test_managed_health_is_specific_to_the_requested_thread(self):
        self.register()
        self.app.scan(send=True, origin="worker")
        with mock.patch.object(self.app, "_lock_is_held", return_value=True):
            self.assertTrue(self.app.managed(THREAD)["worker_health"]["ready_for_wakes"])
            with contextlib.closing(self.app.database(create=False)) as db:
                with db:
                    db.execute("UPDATE meta SET value=? WHERE key='last_send_scope'", (json.dumps(["999"]),))
            health = self.app.managed(THREAD)["worker_health"]
            self.assertFalse(health["ready_for_wakes"])
            self.assertFalse(health["thread_in_send_scope"])

    def test_failed_api_scan_does_not_refresh_success_health(self):
        self.register()
        self.api.rows = [message("101")]
        self.app.scan(origin="worker")
        self.clock[0] = 1070
        self.api.fail = dw.DiscordAPIError(403, "missing access", code=50001)
        self.app.scan(send=True, origin="worker")
        lock_path = self.state / "worker.lock"
        with lock_path.open("a") as stream:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
            with contextlib.closing(self.app.database(create=False)) as db:
                health = self.app._health(db, THREAD)
            fcntl.flock(stream, fcntl.LOCK_UN)
        self.assertTrue(health["worker_running"])
        self.assertFalse(health["api_success_fresh"])
        self.assertFalse(health["ready_for_wakes"])
        self.assertIsNotNone(health["thread_error"])

    def test_receive_empty_returns_three_without_stdout(self):
        self.register()
        output = io.StringIO()
        with mock.patch.object(dw.Watch, "current_owner", return_value=self.app.binding_snapshot(self.targets[0])):
            with contextlib.redirect_stdout(output):
                result = dw.main(["--state-dir", str(self.state), "--hailing-state", str(self.hailing),
                                  "--notify-home", str(self.notify_home), "receive", "--thread", THREAD])
        self.assertEqual(result, 3)
        self.assertEqual(output.getvalue(), "")

    def test_config_reader_captures_credentials_without_printing_them(self):
        config = self.notify_home / "env"
        config.write_text(f"DISCORD_BOT_TOKEN=hidden-token\nDISCORD_USER_ID={USER_ID}\n", encoding="utf-8")
        config.chmod(0o600)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            values = dw.read_notify_credentials(self.notify_home)
        self.assertEqual(values["token"], "hidden-token")
        self.assertEqual(values["user_id"], USER_ID)
        self.assertEqual(output.getvalue(), "")


class InstallerTests(unittest.TestCase):
    def test_installed_guide_prints_outside_checkout_without_creating_state(self):
        sys.path.insert(0, str(APP_DIR))
        import install
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            runtime_root = root / "runtime"
            result = install.install(runtime_root, bin_dir)
            self.assertFalse(result["worker_started"])
            cwd = root / "outside"
            cwd.mkdir()
            runtime = Path(result["runtime"])
            before = {path.relative_to(runtime): (path.read_bytes(), path.stat().st_mode & 0o777)
                      for path in runtime.rglob("*") if path.is_file()}
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root)}
            completed = __import__("subprocess").run(
                [str(bin_dir / "discord-watch"), "guide", "--print"], cwd=cwd,
                env=env, stdout=__import__("subprocess").PIPE,
                stderr=__import__("subprocess").PIPE, text=True, timeout=10, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, (APP_DIR / "guide.md").read_text(encoding="utf-8"))
            self.assertFalse((root / ".local/state/starfleet-ai/discord-watch").exists())
            self.assertTrue((runtime / "hailing_frequencies.py").is_file())
            after = {path.relative_to(runtime): (path.read_bytes(), path.stat().st_mode & 0o777)
                     for path in runtime.rglob("*") if path.is_file()}
            self.assertEqual(after, before)
            self.assertFalse(any("__pycache__" in path.parts for path in runtime.rglob("*")))


class ServiceCliTests(unittest.TestCase):
    def test_cli_validation_error_is_json_not_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            completed = __import__("subprocess").run(
                [sys.executable, str(APP_DIR / "discord_watch.py"), "--state-dir", temporary, "managed", "not-a-thread"],
                capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("error", json.loads(completed.stderr))
        self.assertNotIn("Traceback", completed.stderr)

    def test_graceful_service_stop_returns_zero(self):
        for command in ("serve", "supervise"):
            with self.subTest(command=command), mock.patch.object(dw.Watch, command, return_value={"stopped": True}):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(dw.main([command]), 0)
                self.assertEqual(json.loads(output.getvalue()), {"stopped": True})


if __name__ == "__main__":
    unittest.main()
