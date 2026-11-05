import contextlib
import copy
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hailing_frequencies as hf


SESSION = "ses_HailingFixture42"
PASSWORD = "synthetic-opencode-password"
SURFACE = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
WORKSPACE = "BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB"
STARTED_AT = 1234.5


class FakeOpenCodeServer:
    def __init__(self, directory):
        self.directory = str(directory)
        self.active = {}
        self.inbox = []
        self.permissions = []
        self.forms = []
        self.overrides = {}
        self.post_status = 200
        self.post_delay = 0
        self.post_response = {"data": {"id": "msg_HailingFixture1", "sessionID": SESSION,
                                       "time": {"created": 1}, "type": "user",
                                       "payload": {"parts": []}, "delivery": "steer"}}
        self.posts = []
        self.gets = []
        self.password = PASSWORD
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def respond(self, status, value, headers=None):
                body = json.dumps(value, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                for name, content in (headers or {}).items():
                    self.send_header(name, content)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def authorized(self):
                expected = "Basic " + __import__("base64").b64encode(
                    f"opencode:{owner.password}".encode()).decode()
                return self.headers.get("Authorization") == expected

            def do_GET(self):
                owner.gets.append(self.path)
                if self.path == "/api/info" and not self.headers.get("Authorization"):
                    self.respond(401, {}, {"WWW-Authenticate": 'Basic realm="opencode"'})
                    return
                if not self.authorized():
                    self.respond(401, {})
                    return
                if self.path in owner.overrides:
                    status, value = owner.overrides[self.path]
                    self.respond(status, value)
                    return
                if self.path == "/api/info":
                    self.respond(200, {"version": "2.2.5", "pid": os.getpid(), "urls": [],
                                       "paths": {"tmp": "/tmp/opencode-fixture"}})
                elif self.path == "/api/session/active":
                    self.respond(200, {"data": owner.active})
                elif self.path == f"/api/session/{SESSION}/inbox":
                    self.respond(200, {"data": owner.inbox})
                elif self.path == f"/api/session/{SESSION}":
                    self.respond(200, {"data": {"id": SESSION,
                        "location": {"directory": owner.directory}, "time": {"archived": None}}})
                elif self.path == f"/api/session/{SESSION}/permission":
                    self.respond(200, {"data": owner.permissions})
                elif self.path == f"/api/session/{SESSION}/form":
                    self.respond(200, {"data": owner.forms})
                else:
                    self.respond(404, {})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.posts.append((self.path, self.headers.get("Authorization"), body))
                try:
                    request = json.loads(body.decode("utf-8"))
                except (UnicodeError, ValueError):
                    self.respond(400, {})
                    return
                valid_text = isinstance(request, dict) and isinstance(request.get("text"), str)
                strict_shape = isinstance(request, dict) and set(request) == {"text"}
                queue_shape = (isinstance(request, dict) and set(request) == {"text", "delivery", "resume"}
                               and request.get("delivery") == "queue" and request.get("resume") is True)
                if not valid_text or not (strict_shape or queue_shape):
                    self.respond(400, {})
                    return
                if owner.post_delay:
                    time.sleep(owner.post_delay)
                self.respond(owner.post_status, owner.post_response)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


class OpenCodeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.hooks = self.root / "hooks"
        self.hooks.mkdir(mode=0o700)
        self.password_file = self.root / "opencode-password"
        self.password_file.write_text(PASSWORD, encoding="utf-8")
        self.password_file.chmod(0o600)
        self.server = FakeOpenCodeServer(self.worktree)
        self.addCleanup(self.server.close)
        self.target = {
            "harness": "opencode", "session": SESSION, "surface": SURFACE,
            "workspace": WORKSPACE, "api_url": self.server.url,
            "password_file": str(self.password_file), "expected_server_pid": os.getpid(),
            "session_directory": str(self.worktree), "worktree_directory": str(self.worktree),
            "parent_task_id": "task:fixture", "label": "opencode-fixture",
            "started_at": STARTED_AT, "cutoff": STARTED_AT,
        }
        self.guard = {"surface_id": SURFACE, "workspace_id": WORKSPACE, "state": "empty",
                      "agent": True, "terminal": True, "blocks_typing": False,
                      "waiting_on_human": False, "lifecycle": "idle"}
        self.settings = {"messages_enabled": True, "receiving": True, "workspace_receiving": True}
        self.messages = []
        self.current = {"truncated": False, "owner_availability": {"agent_sessions": "available"},
                        "items": [{"placement": {"kind": "local"},
                                   "projections": [{"panel_id": SURFACE, "workspace_id": WORKSPACE}],
                                   "agents": [{"session_id": SESSION, "kind": "opencode", "state": "idle"}]}]}
        self.hook_record = {"surfaceId": SURFACE, "workspaceId": WORKSPACE,
                            "agentLifecycle": "idle", "startedAt": STARTED_AT,
                            "cwd": str(self.worktree)}
        self._write_hook()
        self.cmux = hf.Cmux(hook_directory=self.hooks)
        self.cmux.command = self.native_command
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        hf.atomic_json(self.state / "config.json", {"version": 1, "targets": [self.target],
            "coordinator": None, "remind_after": 300, "escalate_after": 900,
            "cooldown": 600, "interval": 5})
        self.app = hf.Monitor(self.state, self.cmux, clock=lambda: 5000.0)
        self.env = {"CMUX_AGENT_LAUNCH_KIND": "opencode", "CMUX_SURFACE_ID": SURFACE,
                    "CMUX_WORKSPACE_ID": WORKSPACE, "OPENCODE_SESSION_ID": SESSION}

    def _write_hook(self):
        path = self.hooks / "opencode-hook-sessions.json"
        hf.atomic_json(path, {"sessions": {SESSION: self.hook_record}})
        path.chmod(0o600)

    def native_command(self, *args):
        if args[0] == "current":
            return copy.deepcopy(self.current)
        if args[0] == "rpc":
            method = args[1]
            if method == "surface.input_state":
                return copy.deepcopy(self.guard)
            if method == "agent.message.settings":
                return copy.deepcopy(self.settings)
            if method == "agent.message.list":
                return {"messages": copy.deepcopy(self.messages)}
        raise AssertionError(f"Unexpected native command: {args}")

    def submit(self, target=None):
        return self.cmux.submit(target or self.target, "Synthetic attention prompt")

    def inbox_source(self):
        source = {
            "id": "CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC", "body": "Synthetic peer request",
            "recipient_surface_id": SURFACE, "recipient_workspace_id": WORKSPACE,
            "sender_surface_id": "DDDDDDDD-DDDD-4DDD-8DDD-DDDDDDDDDDDD",
            "sender_workspace_id": WORKSPACE, "sender_name": "peer", "created_at": STARTED_AT + 1,
            "state": "queued", "read_at": None, "in_reply_to": None,
        }
        self.messages = [source]
        self.server.active[SESSION] = {"type": "running"}
        self.current["items"][0]["agents"][0]["state"] = "working"
        self.hook_record["agentLifecycle"] = "running"
        self._write_hook()
        return source

    def test_running_recipient_reads_and_receipts_own_inbox_without_prompting(self):
        source = self.inbox_source()
        self.guard.update(state="unknown", lifecycle="running")
        self.server.inbox = [{"id": "queued-api-work"}]
        self.server.permissions = [{"id": "permission-fixture"}]
        self.server.forms = [{"id": "form-fixture"}]
        output = io.StringIO()

        result = self.app.consume_inbox(SESSION, [source["id"]], SURFACE, output)

        self.assertEqual(json.loads(output.getvalue())["messages"][0]["body"], source["body"])
        self.assertEqual((result["acknowledged"], result["evidence"]), (True, "inbox"))
        with contextlib.closing(self.app.database()) as db:
            self.assertEqual(db.execute("SELECT session,message,evidence FROM acknowledgements").fetchall(),
                             [(SESSION, source["id"], "inbox")])
        self.assertIsNone(self.messages[0]["read_at"])
        self.assertEqual(self.messages[0]["state"], "queued")
        with self.assertRaises(hf.DeliveryHoldError):
            self.submit()
        self.assertEqual(self.server.posts, [])

    def test_running_inbox_still_requires_authenticated_exact_native_identity(self):
        source = self.inbox_source()
        cases = ("api-auth", "api-pid", "api-directory", "native-generation", "native-worktree",
                 "receiving", "caller-surface")
        for case in cases:
            with self.subTest(case=case):
                self.server.overrides.clear()
                self.server.password = PASSWORD
                self.hook_record["startedAt"] = STARTED_AT
                self.hook_record["cwd"] = str(self.worktree)
                self._write_hook()
                self.settings["receiving"] = True
                surface = SURFACE
                if case == "api-auth":
                    self.server.password = "different-synthetic-password"
                elif case == "api-pid":
                    self.server.overrides["/api/info"] = (200, {"version": "2.2.5", "pid": os.getpid() + 1})
                elif case == "api-directory":
                    self.server.overrides[f"/api/session/{SESSION}"] = (200, {"data": {
                        "id": SESSION, "location": {"directory": str(self.root)}, "time": {"archived": None}}})
                elif case == "native-generation":
                    self.hook_record["startedAt"] += 1
                    self._write_hook()
                elif case == "native-worktree":
                    self.hook_record["cwd"] = str(self.root)
                    self._write_hook()
                elif case == "receiving":
                    self.settings["receiving"] = False
                else:
                    surface = WORKSPACE
                output = io.StringIO()
                with self.assertRaises(ValueError):
                    self.app.consume_inbox(SESSION, [source["id"]], surface, output)
                self.assertEqual(output.getvalue(), "")
                self.assertFalse((self.state / "receipts.sqlite3").exists())
                self.assertEqual(self.server.posts, [])

    def test_running_inbox_rechecks_identity_after_output_before_receipt(self):
        source = self.inbox_source()
        fixture = self

        class ReplacedOnFlush(io.StringIO):
            def flush(self):
                fixture.hook_record["startedAt"] += 1
                fixture._write_hook()

        output = ReplacedOnFlush()
        with self.assertRaisesRegex(ValueError, "generation changed"):
            self.app.consume_inbox(SESSION, [source["id"]], SURFACE, output)
        self.assertEqual(json.loads(output.getvalue())["messages"][0]["body"], source["body"])
        self.assertFalse((self.state / "receipts.sqlite3").exists())
        self.assertEqual(self.server.posts, [])

    def attention_once(self, body=b"Synthetic attention prompt", key="fixture-event"):
        with patch.dict("os.environ", self.env, clear=False):
            return self.app.attention_once(SESSION, key, body, SURFACE)

    def opencode_manifest(self, delivery_policy=None):
        manifest = {
            "api_url": self.target["api_url"], "password_file": self.target["password_file"],
            "session_id": self.target["session"], "expected_server_pid": self.target["expected_server_pid"],
            "session_directory": self.target["session_directory"],
            "worktree_directory": self.target["worktree_directory"],
            "cmux_workspace_uuid": self.target["workspace"], "cmux_surface_uuid": self.target["surface"],
            "parent_task_id": self.target["parent_task_id"],
        }
        if delivery_policy is not None:
            manifest["delivery_policy"] = delivery_policy
        path = self.root / "opencode-binding.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        path.chmod(0o600)
        return path

    def enroll_api_sidechannel(self):
        config = self.app.config()
        config["targets"] = []
        hf.atomic_json(self.state / "config.json", config)
        with patch.dict("os.environ", self.env, clear=False):
            result = self.app.enroll_opencode(self.opencode_manifest("api_sidechannel"), "api-sidechannel")
        self.target = result["enrolled"]
        return self.target

    def test_manifest_self_enrollment_binds_session_server_directories_and_surface(self):
        manifest = {
            "api_url": self.target["api_url"], "password_file": self.target["password_file"],
            "session_id": self.target["session"], "expected_server_pid": self.target["expected_server_pid"],
            "session_directory": self.target["session_directory"],
            "worktree_directory": self.target["worktree_directory"],
            "cmux_workspace_uuid": self.target["workspace"], "cmux_surface_uuid": self.target["surface"],
            "parent_task_id": self.target["parent_task_id"],
        }
        path = self.root / "binding.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        path.chmod(0o600)
        config = self.app.config()
        config["targets"] = []
        hf.atomic_json(self.state / "config.json", config)
        with patch.dict("os.environ", self.env, clear=False):
            result = self.app.enroll_opencode(path, "pilot")
        enrolled = result["enrolled"]
        self.assertEqual(enrolled["session"], SESSION)
        self.assertEqual(enrolled["expected_server_pid"], os.getpid())
        self.assertEqual(enrolled["session_directory"], str(self.worktree))
        self.assertEqual(enrolled["worktree_directory"], str(self.worktree))
        self.assertEqual(enrolled["surface"], SURFACE)
        self.assertEqual(enrolled["delivery_policy"], "native_empty_composer")
        self.assertNotIn(PASSWORD, json.dumps(result))
        self.assertEqual(self.app.config()["targets"][0]["session"], SESSION)

    def test_busy_session_can_enroll_by_identity_but_delivery_stays_held(self):
        config = self.app.config()
        config["targets"] = []
        hf.atomic_json(self.state / "config.json", config)
        self.server.active[SESSION] = {"type": "running"}
        self.server.inbox = [{"id": "queued-fixture"}]
        self.server.permissions = [{"id": "permission-fixture"}]
        self.server.forms = [{"id": "form-fixture"}]
        self.current["items"][0]["agents"][0]["state"] = "working"
        self.hook_record["agentLifecycle"] = "running"
        self._write_hook()
        self.guard.update(state="draft", waiting_on_human=True)
        self.server.gets.clear()

        with patch.dict("os.environ", self.env, clear=False):
            enrolled = self.app.enroll_opencode(self.opencode_manifest("api_sidechannel"), "busy-owner")

        self.target = enrolled["enrolled"]
        self.assertEqual(self.target["session"], SESSION)
        self.assertEqual(self.target["started_at"], STARTED_AT)
        self.assertNotIn("/api/session/active", self.server.gets)
        self.assertFalse(any(path.endswith(("/inbox", "/permission", "/form"))
                            for path in self.server.gets))

        result = self.attention_once(key="busy-enrolled-session")

        self.assertEqual((result["outcome"], result["attempted"]), ("held", False))
        self.assertIn("/api/session/active", self.server.gets)
        self.assertEqual(self.server.posts, [])
        row = self.app.prior_attention_intent(self.target, "busy-enrolled-session")
        self.assertEqual(row[1:3], ("held", 0))

    def test_manifest_policy_is_explicit_validated_and_existing_policy_cannot_change(self):
        invalid = self.opencode_manifest("future-policy")
        with self.assertRaisesRegex(ValueError, "delivery policy"):
            hf.load_opencode_manifest(invalid)

        config = self.app.config()
        config["targets"] = []
        hf.atomic_json(self.state / "config.json", config)
        with patch.dict("os.environ", self.env, clear=False):
            strict = self.app.enroll_opencode(self.opencode_manifest(), "strict")
        before = self.app.config()["targets"]
        self.assertEqual(strict["enrolled"]["delivery_policy"], "native_empty_composer")

        with patch.dict("os.environ", self.env, clear=False):
            with self.assertRaisesRegex(ValueError, "unenroll explicitly"):
                self.app.enroll_opencode(self.opencode_manifest("api_sidechannel"), "strict")
        self.assertEqual(self.app.config()["targets"], before)

    def test_manifest_self_enrollment_requires_exact_caller_session(self):
        manifest = {
            "api_url": self.target["api_url"], "password_file": self.target["password_file"],
            "session_id": self.target["session"], "expected_server_pid": self.target["expected_server_pid"],
            "session_directory": self.target["session_directory"],
            "worktree_directory": self.target["worktree_directory"],
            "cmux_workspace_uuid": self.target["workspace"], "cmux_surface_uuid": self.target["surface"],
            "parent_task_id": self.target["parent_task_id"],
        }
        path = self.root / "binding-missing-caller.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        path.chmod(0o600)
        config = self.app.config()
        config["targets"] = []
        hf.atomic_json(self.state / "config.json", config)
        env = {key: value for key, value in self.env.items() if key != "OPENCODE_SESSION_ID"}
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaisesRegex(ValueError, "OPENCODE_SESSION_ID"):
                self.app.enroll_opencode(path, "pilot")
        self.assertEqual(self.app.config()["targets"], [])

    def test_identity_enrollment_mismatch_does_not_save_target(self):
        config = self.app.config()
        config["targets"] = []
        hf.atomic_json(self.state / "config.json", config)
        manifest = self.opencode_manifest("api_sidechannel")
        value = json.loads(manifest.read_text())
        value["expected_server_pid"] += 1
        manifest.write_text(json.dumps(value), encoding="utf-8")
        manifest.chmod(0o600)

        with patch.dict("os.environ", self.env, clear=False):
            with self.assertRaisesRegex(ValueError, "API identity held: api_server_pid_mismatch"):
                self.app.enroll_opencode(manifest, "mismatch")
        self.assertEqual(self.app.config()["targets"], [])

        self.current["items"][0]["projections"][0]["workspace_id"] = SURFACE
        manifest = self.opencode_manifest("api_sidechannel")
        with patch.dict("os.environ", self.env, clear=False):
            with self.assertRaisesRegex(ValueError, "CMUX workspace changed"):
                self.app.enroll_opencode(manifest, "mismatch")
        self.assertEqual(self.app.config()["targets"], [])

    def test_wrong_server_pid_or_directory_holds_before_post(self):
        for changes in ({"expected_server_pid": os.getpid() + 10},
                        {"session_directory": str(self.root)}):
            candidate = {**self.target, **changes}
            with self.assertRaises(hf.DeliveryHoldError):
                self.submit(candidate)
        self.assertEqual(self.server.posts, [])

    def test_unknown_or_staged_native_input_holds_without_post_or_attempt(self):
        for changes in ({"state": "unknown"}, {"state": "draft"}, {"waiting_on_human": True},
                        {"blocks_typing": True}, {"agent": False}):
            self.guard.update(changes)
            result = self.attention_once(key="native-hold")
            self.assertEqual((result["outcome"], result["attempted"]), ("held", False))
            self.assertEqual(self.server.posts, [])
            row = self.app.prior_attention_intent(self.target, "native-hold")
            self.assertEqual(row[1:3], ("held", 0))
            self.guard = {"surface_id": SURFACE, "workspace_id": WORKSPACE, "state": "empty",
                          "agent": True, "terminal": True, "blocks_typing": False,
                          "waiting_on_human": False, "lifecycle": "idle"}

    def test_attention_without_exact_session_is_held_before_intent_or_post(self):
        env = {key: value for key, value in self.env.items() if key != "OPENCODE_SESSION_ID"}
        with patch.dict("os.environ", env, clear=True):
            result = self.app.attention_once(SESSION, "missing-caller", b"Private prompt", SURFACE)
        self.assertEqual((result["outcome"], result["attempted"]), ("unavailable", False))
        self.assertEqual(self.server.posts, [])
        self.assertIsNone(self.app.prior_attention_intent(self.target, "missing-caller"))

    def test_api_busy_permission_and_form_holds_do_not_post(self):
        self.server.active[SESSION] = {"type": "running"}
        with self.assertRaises(hf.DeliveryHoldError):
            self.submit()
        self.server.active.clear()
        self.server.permissions = [{"id": "permission-fixture"}]
        with self.assertRaises(hf.DeliveryHoldError):
            self.submit()
        self.server.permissions = []
        self.server.forms = [{"id": "form-fixture"}]
        with self.assertRaises(hf.DeliveryHoldError):
            self.submit()
        self.assertEqual(self.server.posts, [])

    def test_api_sidechannel_queues_while_composer_state_is_unknown_or_draft(self):
        self.enroll_api_sidechannel()
        self.server.post_response["data"]["delivery"] = "queue"

        cases = (("unknown", "idle"), ("draft", "idle"))
        for index, (state, lifecycle) in enumerate(cases):
            with self.subTest(state=state, lifecycle=lifecycle):
                self.guard["state"] = state
                self.guard["lifecycle"] = lifecycle
                result = self.attention_once(key=f"api-policy-{index}")
                self.assertEqual((result["outcome"], result["attempted"]), ("submitted", True))

        self.assertEqual(len(self.server.posts), 2)
        for path, _authorization, body in self.server.posts:
            self.assertEqual(path, f"/api/session/{SESSION}/prompt")
            self.assertEqual(json.loads(body.decode("utf-8")), {
                "text": "Synthetic attention prompt", "delivery": "queue", "resume": True})

    def test_api_sidechannel_queue_mismatch_and_timeout_are_uncertain_without_replay(self):
        self.enroll_api_sidechannel()
        self.guard["state"] = "unknown"
        for index, wrong_delivery in enumerate(("steer", {"mode": "queue"}, None)):
            with self.subTest(delivery=wrong_delivery):
                if wrong_delivery is None:
                    self.server.post_response["data"].pop("delivery", None)
                else:
                    self.server.post_response["data"]["delivery"] = wrong_delivery
                key = f"api-queue-mismatch-{index}"
                mismatch = self.attention_once(key=key)
                self.assertEqual((mismatch["outcome"], mismatch["attempted"]), ("uncertain", True))
                mismatch_row = self.app.prior_attention_intent(self.target, key)
                self.assertEqual(mismatch_row[1:3], ("uncertain", 1))
                self.assertEqual(self.attention_once(key=key), mismatch)
                self.assertEqual(len(self.server.posts), index + 1)
                self.server.post_response["data"]["delivery"] = "queue"

        self.server.post_delay = 0.2
        with patch.object(hf, "OPENCODE_HTTP_TIMEOUT", 0.05):
            timeout = self.attention_once(key="api-queue-timeout")
            self.assertEqual((timeout["outcome"], timeout["attempted"]), ("uncertain", True))
            self.assertEqual(self.attention_once(key="api-queue-timeout"), timeout)
        self.assertEqual(len(self.server.posts), 4)

    def test_api_sidechannel_keeps_api_receiving_and_human_input_holds(self):
        self.enroll_api_sidechannel()
        cases = (
            ("active", {SESSION: {"type": "running"}}),
            ("inbox", [{"id": "queued-fixture"}]),
            ("permissions", [{"id": "permission-fixture"}]),
            ("forms", [{"id": "form-fixture"}]),
            ("inbox-malformed", {"data": {"unexpected": "shape"}}),
            ("inbox-unavailable", (503, {})),
        )
        for field, value in cases:
            with self.subTest(field=field):
                if field == "active":
                    self.server.active = value
                elif field.startswith("inbox-"):
                    status, body = (200, value) if field == "inbox-malformed" else value
                    self.server.overrides[f"/api/session/{SESSION}/inbox"] = (status, body)
                else:
                    setattr(self.server, field, value)
                key = f"api-policy-{field}"
                result = self.attention_once(key=key)
                self.assertEqual((result["outcome"], result["attempted"]), ("held", False))
                row = self.app.prior_attention_intent(self.target, key)
                self.assertEqual(row[1:3], ("held", 0))
                self.server.active = {}
                self.server.inbox = []
                self.server.permissions = []
                self.server.forms = []
                self.server.overrides.clear()

        guard_cases = (
            {"waiting_on_human": True}, {"blocks_typing": True}, {"agent": False},
            {"terminal": False}, {"lifecycle": "needsInput"}, {"lifecycle": "unknown"},
            {"state": "future-state"},
        )
        for index, changes in enumerate(guard_cases):
            with self.subTest(guard=changes):
                self.guard.update(changes)
                key = f"api-policy-guard-{index}"
                result = self.attention_once(key=key)
                self.assertEqual((result["outcome"], result["attempted"]), ("held", False))
                self.guard = {"surface_id": SURFACE, "workspace_id": WORKSPACE, "state": "unknown",
                              "agent": True, "terminal": True, "blocks_typing": False,
                              "waiting_on_human": False, "lifecycle": "idle"}
        for index, field in enumerate(("surface_id", "workspace_id", "state", "blocks_typing",
                                       "waiting_on_human", "lifecycle", "agent", "terminal")):
            with self.subTest(missing=field):
                self.guard.pop(field)
                key = f"api-policy-missing-guard-{index}"
                result = self.attention_once(key=key)
                self.assertEqual((result["outcome"], result["attempted"]), ("held", False))
                self.guard = {"surface_id": SURFACE, "workspace_id": WORKSPACE, "state": "unknown",
                              "agent": True, "terminal": True, "blocks_typing": False,
                              "waiting_on_human": False, "lifecycle": "idle"}
        for key, value in (("messages_enabled", False), ("receiving", False),
                           ("workspace_receiving", False)):
            self.settings[key] = value
            result = self.attention_once(key=f"api-policy-{key}-off")
            self.assertEqual((result["outcome"], result["attempted"]), ("held", False))
            self.settings[key] = True
        self.assertEqual(self.server.posts, [])

    def test_api_sidechannel_still_requires_exact_native_binding_and_generation(self):
        self.enroll_api_sidechannel()
        original_current = copy.deepcopy(self.current)
        original_hook = copy.deepcopy(self.hook_record)
        cases = (
            ("projection", lambda: self.current["items"][0]["projections"][0].update(
                panel_id="CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC")),
            ("generation", lambda: self.hook_record.update(startedAt=STARTED_AT + 1)),
            ("worktree", lambda: self.hook_record.update(cwd=str(self.root))),
        )
        for name, change in cases:
            with self.subTest(binding=name):
                self.current = copy.deepcopy(original_current)
                self.hook_record = copy.deepcopy(original_hook)
                change()
                self._write_hook()
                result = self.attention_once(key=f"api-policy-binding-{name}")
                self.assertEqual((result["outcome"], result["attempted"]), ("held", False))
                self.assertEqual(self.server.posts, [])

    def test_pending_api_inbox_is_durable_hold_without_send_or_attempt(self):
        self.server.inbox = [{"id": "inbox-fixture", "type": "user"}]

        result = self.attention_once(key="pending-opencode-inbox")

        self.assertEqual((result["outcome"], result["attempted"]), ("held", False))
        self.assertEqual(self.server.posts, [])
        row = self.app.prior_attention_intent(self.target, "pending-opencode-inbox")
        self.assertEqual(row[1:3], ("held", 0))

    def test_wrong_visible_surface_or_worktree_holds(self):
        self.hook_record["surfaceId"] = "CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC"
        self._write_hook()
        with self.assertRaises(hf.DeliveryHoldError):
            self.submit()
        self.hook_record["surfaceId"] = SURFACE
        self.hook_record["cwd"] = str(self.root)
        self._write_hook()
        with self.assertRaises(hf.DeliveryHoldError):
            self.submit()
        self.assertEqual(self.server.posts, [])

    def test_wrong_or_ambiguous_cmux_projection_holds(self):
        original = copy.deepcopy(self.current)
        self.current["items"][0]["projections"][0]["panel_id"] = "CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC"
        with self.assertRaises(hf.DeliveryHoldError):
            self.submit()
        self.current = original
        self.current["items"].append(copy.deepcopy(self.current["items"][0]))
        with self.assertRaises(hf.DeliveryHoldError):
            self.submit()
        self.assertEqual(self.server.posts, [])

    def test_missing_or_conflicting_cmux_agent_projection_holds_even_with_hook(self):
        agents = self.current["items"][0]["agents"]
        self.current["items"][0]["agents"] = []
        with self.assertRaisesRegex(ValueError, "Exact OpenCode CMUX session projection"):
            self.cmux.binding(self.target)
        self.current["items"][0]["agents"] = [
            {"session_id": "ses_OtherSession", "kind": "opencode", "state": "idle"}]
        with self.assertRaisesRegex(ValueError, "Exact OpenCode CMUX session projection"):
            self.cmux.binding(self.target)
        self.current["items"][0]["agents"] = agents
        self.assertTrue(self.cmux.binding(self.target))
        self.assertEqual(self.server.posts, [])

    def test_unknown_native_hold_is_durable_retryable_without_consuming_attempt(self):
        self.guard["state"] = "unknown"
        held = self.attention_once(key="retry-after-hold")
        self.assertEqual((held["outcome"], held["attempted"]), ("held", False))
        self.assertEqual(len(self.server.posts), 0)
        self.guard["state"] = "empty"
        submitted = self.attention_once(key="retry-after-hold")
        self.assertEqual((submitted["outcome"], submitted["attempted"]), ("submitted", True))
        self.assertEqual(len(self.server.posts), 1)
        path, authorization, _body = self.server.posts[0]
        self.assertEqual(path, f"/api/session/{SESSION}/prompt")
        self.assertEqual(json.loads(_body.decode("utf-8")), {"text": "Synthetic attention prompt"})
        self.assertEqual(authorization, "Basic " + __import__("base64").b64encode(
            f"opencode:{PASSWORD}".encode()).decode())
        duplicate = self.attention_once(key="retry-after-hold")
        self.assertEqual(duplicate, submitted)
        self.assertEqual(len(self.server.posts), 1)

    def test_ambiguous_post_response_is_uncertain_and_never_retried(self):
        self.server.post_status = 500
        result = self.attention_once(body=b"Private synthetic prompt", key="uncertain-event")
        self.assertEqual((result["outcome"], result["attempted"]), ("uncertain", True))
        self.assertEqual(len(self.server.posts), 1)
        duplicate = self.attention_once(body=b"Private synthetic prompt", key="uncertain-event")
        self.assertEqual(duplicate, result)
        self.assertEqual(len(self.server.posts), 1)
        with contextlib.closing(self.app.database()) as db:
            row = db.execute("SELECT body_sha256,outcome,attempted,detail FROM attention_intents "
                             "WHERE event_key='uncertain-event'").fetchone()
        self.assertEqual(row[1:3], ("uncertain", 1))
        self.assertNotIn("Private synthetic prompt", row[3])
        self.assertNotIn(PASSWORD, json.dumps(row))

    def test_post_timeout_is_uncertain_and_never_retried(self):
        self.server.post_delay = 0.2
        with patch.object(hf, "OPENCODE_HTTP_TIMEOUT", 0.05):
            result = self.attention_once(key="timeout-event")
            self.assertEqual((result["outcome"], result["attempted"]), ("uncertain", True))
            duplicate = self.attention_once(key="timeout-event")
        self.assertEqual(duplicate, result)
        self.assertEqual(len(self.server.posts), 1)

    def test_200_requires_actual_exact_user_message_admission_or_stays_uncertain(self):
        valid = copy.deepcopy(self.server.post_response["data"])
        invalid_responses = (
            {"data": {**valid, "sessionID": "ses_OtherSession"}},
            {"data": {**valid, "type": "assistant"}},
            {"data": {**valid, "id": "not-a-message-id"}},
            {"data": {key: value for key, value in valid.items() if key != "delivery"}},
        )
        for index, response in enumerate(invalid_responses):
            self.server.post_response = response
            key = f"bad-admission-{index}"
            result = self.attention_once(key=key)
            self.assertEqual((result["outcome"], result["attempted"]), ("uncertain", True))
            self.assertEqual(self.attention_once(key=key), result)
            self.assertEqual(len(self.server.posts), index + 1)

    def test_manifest_must_be_private_and_exact(self):
        path = self.root / "loose.json"
        manifest = {
            "api_url": self.target["api_url"], "password_file": self.target["password_file"],
            "session_id": self.target["session"], "expected_server_pid": self.target["expected_server_pid"],
            "session_directory": self.target["session_directory"],
            "worktree_directory": self.target["worktree_directory"],
            "cmux_workspace_uuid": self.target["workspace"], "cmux_surface_uuid": self.target["surface"],
            "parent_task_id": self.target["parent_task_id"],
        }
        path.write_text(json.dumps(manifest), encoding="utf-8")
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            hf.load_opencode_manifest(path)
        path.chmod(0o600)
        manifest["unexpected"] = True
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "fields"):
            hf.load_opencode_manifest(path)


if __name__ == "__main__":
    unittest.main()
