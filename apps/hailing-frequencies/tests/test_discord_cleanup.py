import copy
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import discord_cleanup as cleanup
import install


UTC = dt.timezone.utc
DISCORD_EPOCH_MS = 1420070400000
FORUM = "900000000000000001"
OPEN_TAG = "100000000000000001"
CLOSED_TAG = "100000000000000002"
THREAD_A = "800000000000000001"
THREAD_B = "800000000000000002"
START = "2026-01-01T00:00:00+00:00"
END = "2026-02-01T00:00:00+00:00"
FIXTURE_TOKEN = "fixture-token-do-not-print"


def message_id(at):
    if isinstance(at, str):
        at = dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
    milliseconds = int(at.timestamp() * 1000)
    return str((milliseconds - DISCORD_EPOCH_MS) << 22)


def make_thread(thread_id, archive_at, last_message_at, tags=None, archived=True, parent=FORUM):
    last_message = message_id(last_message_at) if last_message_at is not None else None
    return {
        "id": thread_id,
        "type": 11,
        "parent_id": parent,
        "applied_tags": list(tags if tags is not None else (CLOSED_TAG,)),
        "thread_metadata": {"archived": archived, "archive_timestamp": archive_at},
        "last_message_id": last_message,
    }


def at(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


class DiscordFixture:
    def __init__(self, archive_rows, current_rows=None):
        self.archive_rows = copy.deepcopy(archive_rows)
        self.archive_pages = None
        self.current_rows = {row["id"]: copy.deepcopy(row) for row in
                             (current_rows if current_rows is not None else archive_rows)}
        self.requests = []
        self.capture_headers = []
        self.redirect_path = None
        self.uncertain_delete = set()
        self._server = None

        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def respond(self, status, value=None, headers=None):
                body = b"" if value is None else json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                for key, header_value in (headers or {}).items():
                    self.send_header(key, header_value)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def record(self, method):
                fixture.requests.append((method, self.path, self.headers.get("Authorization")))

            def do_GET(self):
                self.record("GET")
                parsed = urlsplit(self.path)
                path = parsed.path
                if path == "/capture":
                    fixture.capture_headers.append(self.headers.get("Authorization"))
                    self.respond(200, {})
                    return
                if fixture.redirect_path == path:
                    target = f"http://127.0.0.1:{self.server.server_port}/capture"
                    self.respond(302, {}, {"Location": target})
                    return
                if path == f"/api/v10/channels/{FORUM}":
                    self.respond(200, {
                        "id": FORUM,
                        "type": 15,
                        "available_tags": [
                            {"id": OPEN_TAG, "name": "Open"},
                            {"id": CLOSED_TAG, "name": "Closed"},
                        ],
                    })
                    return
                if path == f"/api/v10/channels/{FORUM}/threads/archived/public":
                    if fixture.archive_pages is None:
                        self.respond(200, {"threads": copy.deepcopy(fixture.archive_rows), "has_more": False})
                        return
                    before = parse_qs(parsed.query).get("before", [None])[0]
                    if before is None:
                        page_index = 0
                    else:
                        cursors = [cleanup.timestamp(page[-1]["thread_metadata"]["archive_timestamp"]).isoformat()
                                   for page in fixture.archive_pages]
                        previous = next((index for index, cursor in enumerate(cursors[:-1])
                                         if cursor == before), None)
                        page_index = previous + 1 if previous is not None else None
                    if page_index is None or page_index >= len(fixture.archive_pages):
                        self.respond(400, {"error": "fixture cursor not found"})
                        return
                    self.respond(200, {"threads": copy.deepcopy(fixture.archive_pages[page_index]),
                                       "has_more": page_index < len(fixture.archive_pages) - 1})
                    return
                prefix = "/api/v10/channels/"
                if path.startswith(prefix):
                    thread_id = path[len(prefix):]
                    row = fixture.current_rows.get(thread_id)
                    if row is not None:
                        self.respond(200, copy.deepcopy(row))
                    else:
                        self.respond(404, {"code": 10003, "message": "Unknown Channel"})
                    return
                self.respond(404, {"code": 10003, "message": "Unknown Channel"})

            def do_DELETE(self):
                self.record("DELETE")
                prefix = "/api/v10/channels/"
                thread_id = self.path[len(prefix):] if self.path.startswith(prefix) else ""
                fixture.current_rows.pop(thread_id, None)
                if thread_id in fixture.uncertain_delete:
                    fixture.uncertain_delete.remove(thread_id)
                    self.respond(500, {"error": "fixture outcome uncertain"})
                    return
                self.respond(204)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def api(self):
        return f"http://127.0.0.1:{self._server.server_port}/api/v10"

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


class DiscordCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="discord-cleanup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.config = self.root / "agent-notify"
        self.config.mkdir(mode=0o700)
        env_file = self.config / "env"
        env_file.write_text(f"DISCORD_BOT_TOKEN={FIXTURE_TOKEN}\n")
        env_file.chmod(0o600)
        cache = self.config / "cache.json"
        cache.write_text(json.dumps({"channel": FORUM}))
        cache.chmod(0o600)
        self.site = self.root / "sitecustomize"
        self.site.mkdir(mode=0o700)
        (self.site / "sitecustomize.py").write_text(
            "import os\nimport discord_cleanup\n"
            "discord_cleanup.API = os.environ['DISCORD_CLEANUP_TEST_API']\n")
        self.output_dir = self.root / "private"
        self.output_dir.mkdir(mode=0o700)

    def child_env(self, fake):
        return {
            "PATH": os.defpath,
            "HOME": str(self.home),
            "AGENT_NOTIFY_HOME": str(self.config),
            "PYTHONPATH": os.pathsep.join((str(self.site), str(APP))),
            "PYTHONDONTWRITEBYTECODE": "1",
            "DISCORD_CLEANUP_TEST_API": fake.api,
        }

    def run_cli(self, fake, *arguments):
        command = [sys.executable, str(APP / "hailing_frequencies.py"), *arguments]
        return subprocess.run(command, cwd=self.root, env=self.child_env(fake),
                              capture_output=True, text=True, timeout=20)

    def start_fake(self, rows=None, current=None):
        fake = DiscordFixture(rows or [], current)
        self.addCleanup(fake.close)
        return fake

    def make_archive_rows(self):
        start = at(START)
        end = at(END)
        before = start - dt.timedelta(seconds=1)
        last_before_end = end - dt.timedelta(days=1)
        last_after_end = end + dt.timedelta(seconds=1)
        return [
            make_thread(THREAD_A, START, last_before_end),
            make_thread(THREAD_B, "2026-01-20T12:30:00Z", last_before_end),
            make_thread("800000000000000003", "2026-01-10T00:00:00Z", end),
            make_thread("800000000000000004", "2026-01-10T00:00:00Z", None),
            make_thread("800000000000000005", "2026-01-10T00:00:00Z", last_before_end,
                        tags=(OPEN_TAG,)),
            make_thread("800000000000000006", "2026-01-10T00:00:00Z", last_before_end,
                        archived=False),
            make_thread("800000000000000007", before.isoformat(), last_before_end),
            make_thread("800000000000000008", "2026-01-10T00:00:00Z", last_before_end,
                        tags=(CLOSED_TAG, OPEN_TAG)),
            make_thread("800000000000000009", "2026-01-10T00:00:00Z", last_after_end),
            make_thread("800000000000000010", "2026-01-10T00:00:00Z", last_before_end,
                        parent="900000000000000099"),
        ]

    def test_time_range_is_explicit_utc_and_start_inclusive_end_exclusive(self):
        start, end = cleanup.timestamp(START), cleanup.timestamp(END)
        tags = {"open": OPEN_TAG, "closed": CLOSED_TAG}
        eligible = make_thread(THREAD_A, START, "2026-01-31T23:59:59Z")
        at_end = make_thread(THREAD_B, END, "2026-01-31T23:59:59Z")
        self.assertIsNotNone(cleanup.candidate(eligible, FORUM, tags, start, end))
        self.assertIsNone(cleanup.candidate(at_end, FORUM, tags, start, end))
        self.assertIsNone(cleanup.candidate(make_thread(THREAD_A, START, None), FORUM, tags, start, end))
        with self.assertRaisesRegex(ValueError, "explicit UTC offset"):
            cleanup.timestamp("2026-01-01T12:00:00")

    def test_plan_and_apply_use_fake_http_and_recheck_each_thread(self):
        rows = self.make_archive_rows()
        fake = self.start_fake(rows)
        plan_path = self.output_dir / "plan.json"
        planned = self.run_cli(fake, "discord-cleanup", "plan", "--start", START, "--end", END,
                               "--output", str(plan_path))
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual(planned.stderr, "")
        summary = json.loads(planned.stdout)
        self.assertEqual(summary["selected"], 2)
        self.assertEqual(summary["deleted"], 0)
        self.assertNotIn(FIXTURE_TOKEN, planned.stdout + planned.stderr)
        self.assertEqual(plan_path.stat().st_mode & 0o777, 0o600)
        plan = json.loads(plan_path.read_text())
        self.assertEqual([row["id"] for row in plan["candidates"]], [THREAD_A, THREAD_B])
        self.assertEqual(plan["sha256"], cleanup.digest(plan))

        changed = copy.deepcopy(fake.current_rows[THREAD_B])
        changed["last_message_id"] = message_id(at(END) + dt.timedelta(seconds=1))
        fake.current_rows[THREAD_B] = changed
        applied = self.run_cli(fake, "discord-cleanup", "apply", "--plan", str(plan_path),
                               "--confirm", summary["sha256"])
        self.assertEqual(applied.returncode, 0, applied.stderr)
        result = json.loads(applied.stdout)
        self.assertEqual((result["deleted"], result["changed"], result["already_absent"]), (1, 1, 0))
        self.assertNotIn(FIXTURE_TOKEN, applied.stdout + applied.stderr)
        thread_path_a = f"/api/v10/channels/{THREAD_A}"
        thread_path_b = f"/api/v10/channels/{THREAD_B}"
        a_events = [(method, path) for method, path, _auth in fake.requests if path == thread_path_a]
        b_events = [(method, path) for method, path, _auth in fake.requests if path == thread_path_b]
        self.assertEqual([method for method, _path in a_events], ["GET", "DELETE", "GET"])
        self.assertEqual([method for method, _path in b_events], ["GET"])
        self.assertTrue(all(auth == f"Bot {FIXTURE_TOKEN}" for _method, _path, auth in fake.requests))
        journal = Path(result["journal"])
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        journal_text = journal.read_text()
        self.assertIn("delete_intent", journal_text)
        self.assertIn("deleted_get404", journal_text)
        self.assertIn("changed_preserved", journal_text)
        self.assertNotIn(FIXTURE_TOKEN, journal_text)
        self.assertNotIn("message content", journal_text)

        previous_count = len(fake.requests)
        replay = self.run_cli(fake, "discord-cleanup", "apply", "--plan", str(plan_path),
                              "--confirm", summary["sha256"])
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("do not replay", replay.stderr)
        self.assertEqual(len(fake.requests), previous_count)

    def test_plan_paginates_and_rejects_nonadvancing_cursor_without_preview(self):
        page_one = [make_thread(THREAD_A, "2026-01-20T00:00:00Z", "2026-01-31T00:00:00Z")]
        page_two = [make_thread(THREAD_B, "2026-01-10T00:00:00Z", "2026-01-31T00:00:00Z")]
        fake = self.start_fake(page_one + page_two)
        fake.archive_pages = [page_one, page_two]
        plan_path = self.output_dir / "paged-plan.json"
        planned = self.run_cli(fake, "discord-cleanup", "plan", "--start", START, "--end", END,
                               "--output", str(plan_path))
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual(json.loads(planned.stdout)["selected"], 2)
        archived_requests = [path for method, path, _auth in fake.requests
                             if method == "GET" and "/threads/archived/public?" in path]
        self.assertEqual(len(archived_requests), 2)
        second_query = parse_qs(urlsplit(archived_requests[1]).query)
        self.assertEqual(second_query["before"], ["2026-01-20T00:00:00+00:00"])

        repeated_page = [make_thread("800000000000000011", "2026-01-20T00:00:00Z",
                                     "2026-01-31T00:00:00Z")]
        repeated_next = [make_thread("800000000000000012", "2026-01-20T00:00:00Z",
                                     "2026-01-31T00:00:00Z")]
        later_page = [make_thread("800000000000000013", "2026-01-10T00:00:00Z",
                                  "2026-01-31T00:00:00Z")]
        bad_fake = self.start_fake(repeated_page + repeated_next + later_page)
        bad_fake.archive_pages = [repeated_page, repeated_next, later_page]
        invalid_path = self.output_dir / "invalid-cursor-plan.json"
        invalid = self.run_cli(bad_fake, "discord-cleanup", "plan", "--start", START, "--end", END,
                               "--output", str(invalid_path))
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("cursor did not advance", invalid.stderr)
        self.assertFalse(invalid_path.exists())

    def test_modified_plan_fails_hash_check_before_discord_reads(self):
        fake = self.start_fake(self.make_archive_rows())
        plan_path = self.output_dir / "plan.json"
        planned = self.run_cli(fake, "discord-cleanup", "plan", "--start", START, "--end", END,
                               "--output", str(plan_path))
        self.assertEqual(planned.returncode, 0, planned.stderr)
        summary = json.loads(planned.stdout)
        plan = json.loads(plan_path.read_text())
        plan["candidates"][0]["id"] = "800000000000000099"
        plan_path.write_text(json.dumps(plan))
        plan_path.chmod(0o600)
        previous_count = len(fake.requests)
        result = self.run_cli(fake, "discord-cleanup", "apply", "--plan", str(plan_path),
                              "--confirm", summary["sha256"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Confirm the exact preview SHA-256", result.stderr)
        self.assertEqual(len(fake.requests), previous_count)
        self.assertFalse(plan_path.with_name("plan.json.journal.jsonl").exists())

    def test_uncertain_delete_is_journaled_and_same_plan_cannot_replay(self):
        row = make_thread(THREAD_A, START, "2026-01-31T00:00:00Z")
        fake = self.start_fake([row])
        fake.uncertain_delete.add(THREAD_A)
        plan_path = self.output_dir / "uncertain.json"
        planned = self.run_cli(fake, "discord-cleanup", "plan", "--start", START, "--end", END,
                               "--output", str(plan_path))
        self.assertEqual(planned.returncode, 0, planned.stderr)
        summary = json.loads(planned.stdout)
        applied = self.run_cli(fake, "discord-cleanup", "apply", "--plan", str(plan_path),
                               "--confirm", summary["sha256"])
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("stopped after DELETE intent", applied.stderr)
        journal = plan_path.with_name("uncertain.json.journal.jsonl")
        self.assertTrue(journal.exists())
        entries = [json.loads(line) for line in journal.read_text().splitlines()]
        self.assertEqual([entry["outcome"] for entry in entries], ["delete_intent", "uncertain_stop"])
        delete_count = sum(method == "DELETE" for method, _path, _auth in fake.requests)
        replay = self.run_cli(fake, "discord-cleanup", "apply", "--plan", str(plan_path),
                              "--confirm", summary["sha256"])
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("do not replay", replay.stderr)
        self.assertEqual(sum(method == "DELETE" for method, _path, _auth in fake.requests), delete_count)
        self.assertNotIn(FIXTURE_TOKEN, journal.read_text())

    def test_redirect_is_rejected_without_forwarding_authorization(self):
        fake = self.start_fake(self.make_archive_rows())
        fake.redirect_path = f"/api/v10/channels/{FORUM}"
        plan_path = self.output_dir / "redirect.json"
        result = self.run_cli(fake, "discord-cleanup", "plan", "--start", START, "--end", END,
                              "--output", str(plan_path))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTP 302", result.stderr)
        self.assertNotIn(FIXTURE_TOKEN, result.stderr)
        self.assertEqual(fake.capture_headers, [])
        self.assertFalse(plan_path.exists())


class InstalledGuideTests(unittest.TestCase):
    def test_installed_cleanup_guide_prints_without_source_config_or_writes(self):
        with tempfile.TemporaryDirectory(prefix="hailing-cleanup-install-") as temporary:
            root = Path(temporary)
            staging = root / "source-checkout"
            staging.mkdir()
            source_app = APP
            for name in install.SOURCE_NAMES:
                if name == "opencode_adapter.py":
                    adapter_dir = root / "opencode-adapter"
                    adapter_dir.mkdir()
                    shutil.copyfile(source_app.parent / "opencode-adapter" / name, adapter_dir / name)
                else:
                    shutil.copyfile(source_app / name, staging / name)
            for skill_name in install.SKILL_NAMES:
                skill = staging / skill_name
                skill.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_app / skill_name, skill)
            staged_installer = staging / "install.py"
            staged_installer.write_text("")
            expected = (staging / "discord-cleanup.md").read_text()
            with patch.object(install, "__file__", str(staged_installer)):
                result = install.install(root / "runtime", root / "bin")
            runtime = Path(result["runtime"])
            before = sorted(path.relative_to(runtime).as_posix() for path in runtime.rglob("*"))
            shutil.rmtree(staging)

            cwd = root / "outside-git"
            cwd.mkdir()
            home = root / "empty-home"
            missing_config = root / "missing-agent-notify"
            environment = {"PATH": os.defpath, "HOME": str(home),
                           "AGENT_NOTIFY_HOME": str(missing_config)}
            for arguments in (("discord-cleanup", "guide", "--print"), ("discord-cleanup", "guide")):
                printed = subprocess.run([result["command"], *arguments], cwd=cwd, env=environment,
                                         capture_output=True, text=True, timeout=20)
                self.assertEqual(printed.returncode, 0, printed.stderr)
                self.assertEqual(printed.stdout, expected)
                self.assertEqual(printed.stderr, "")
                self.assertFalse(home.exists())
                self.assertFalse(missing_config.exists())
                self.assertFalse((root / "runtime-state").exists())
                self.assertEqual(sorted(path.relative_to(runtime).as_posix() for path in runtime.rglob("*")),
                                 before)


if __name__ == "__main__":
    unittest.main()
