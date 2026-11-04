"""Synthetic OpenCode API and CMUX boundary tests for Model Preferences."""

import base64
import contextlib
import hashlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import uuid

SCRIPT = Path(__file__).resolve().parents[1] / "opencode_dispatch.py"
SPEC = importlib.util.spec_from_file_location("opencode_dispatch", SCRIPT)
dispatch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dispatch
SPEC.loader.exec_module(dispatch)
from test_preferences import mp


CANARY = "synthetic-password-canary-never-output"
CMUX_CANARY = "synthetic-cmux-capability-canary-never-output"
SESSION = "ses_synthetic123"
PROVIDER = "trial-local"
MODEL = "qwen3.6:35b"


def child_value(argv, flag):
    if "--launch-file" not in argv:
        return argv[argv.index("--" + flag) + 1]
    descriptor = dispatch.read_tui_child_descriptor(argv[argv.index("--launch-file") + 1])
    keys = {
        "manifest": "manifest_path", "session": "session_id", "directory": "directory",
        "prompt-file": "prompt_file", "provider-id": "provider_id", "model-id": "model_id",
        "launch-id": "launch_id", "selection": "selection", "task-digest": "task_digest",
        "handshake-file": "handshake_file", "release-file": "release_file",
        "abort-file": "abort_file", "binary": "binary", "parent-session-id": "parent_session_id",
    }
    return descriptor[keys[flag]]


class FakeAPIState:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.password = CANARY
        self.version = "2.2.5"
        self.pid = 4242
        self.challenge = True
        self.provider_url = "http://127.0.0.1:11459/v1"
        self.provider_package = "@opencode/ai/providers/openai-compatible"
        self.lazy_provider_registry = False
        self.provider_catalog_loaded = False
        self.provider_route_missing = False
        self.model_enabled = True
        self.model_tools = True
        self.model_output = 32768
        self.model_package = "@opencode/ai/providers/openai-compatible"
        self.model_body = {"reasoning_effort": "max"}
        self.variant_settings = {"reasoningEffort": "max"}
        self.session_directory = str(root)
        self.session_model = {"providerID": PROVIDER, "id": MODEL, "variant": "max"}
        self.session_parent_id = None
        self.session_metadata = {}
        self.environment = None
        self.messages = []
        self.message_reads = 0
        self.delay_startup_messages = False
        self.pending_startup_prompt = None
        self.sent_prompts = []
        self.active_sessions = {}
        self.pending_permissions = []
        self.pending_forms = []
        self.delay_info = False
        self.delay_create = False
        self.large_info = False
        self.wrong_create_id = False

    def session(self, session_id=SESSION):
        return {"id": session_id, "parentID": self.session_parent_id,
                "location": {"directory": self.session_directory},
                "model": self.session_model, "metadata": self.session_metadata,
                "time": {"created": 1, "updated": 1, "archived": None}}

    def startup_messages(self, prompt):
        return [{"id": "msg_startupUser123", "type": "user", "text": prompt,
                 "time": {"created": 10}},
                {"id": "msg_startupAssistant123", "type": "assistant", "agent": "build",
                 "model": {"providerID": PROVIDER, "id": MODEL, "variant": "max"},
                 "time": {"created": 11, "completed": 12}, "finish": "stop", "content": []}]


class FakeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    @property
    def state(self):
        return self.server.state

    def respond(self, status, value=None, headers=None):
        body = b"" if value is None else json.dumps(value).encode()
        self.send_response(status)
        for key, item in (headers or {}).items():
            self.send_header(key, item)
        if value is not None:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def authenticated(self):
        expected = "Basic " + base64.b64encode(f"opencode:{self.state.password}".encode()).decode()
        if self.headers.get("Authorization") == expected:
            return True
        if self.state.challenge:
            self.respond(401, headers={"WWW-Authenticate": "Basic realm=\"opencode\""})
        else:
            self.respond(401)
        return False

    def do_GET(self):
        self.state.calls.append(("GET", self.path, self.headers.get("Authorization")))
        if not self.authenticated():
            return
        if self.path == "/api/info":
            if self.state.delay_info:
                time.sleep(2.2)
            if self.state.large_info:
                self.send_response(200)
                self.send_header("Content-Length", str(dispatch.MAX_RESPONSE_BYTES + 1))
                self.end_headers()
                return
            return self.respond(200, {"version": self.state.version, "pid": self.state.pid,
                                      "urls": [], "paths": {"tmp": str(self.state.root)}})
        if self.path == f"/api/provider/{PROVIDER}":
            if (self.state.provider_route_missing or
                    (self.state.lazy_provider_registry and not self.state.provider_catalog_loaded)):
                return self.respond(404, {"error": "provider configuration unavailable"})
            return self.respond(200, {"location": {"directory": str(self.state.root)}, "data": {
                "id": PROVIDER, "package": self.state.provider_package,
                "settings": {"baseURL": self.state.provider_url, "apiKey": "synthetic-provider-key"}}})
        if self.path == "/api/model":
            if self.state.lazy_provider_registry:
                self.state.provider_catalog_loaded = True
            return self.respond(200, {"location": {"directory": str(self.state.root)}, "data": [{
                "id": MODEL, "modelID": MODEL, "providerID": PROVIDER, "package": self.state.model_package,
                "enabled": self.state.model_enabled, "body": self.state.model_body,
                "capabilities": {"tools": self.state.model_tools, "input": ["text"], "output": ["text"]},
                "variants": [{"id": "max", "settings": self.state.variant_settings}],
                "limit": {"context": 131072, "output": self.state.model_output}}]})
        if self.path == "/api/session/active":
            return self.respond(200, {"data": self.state.active_sessions})
        if self.path == f"/api/session/{SESSION}":
            return self.respond(200, {"data": self.state.session()})
        if self.path == f"/api/session/{SESSION}/permission":
            return self.respond(200, {"data": self.state.pending_permissions})
        if self.path == f"/api/session/{SESSION}/form":
            return self.respond(200, {"data": self.state.pending_forms})
        if self.path.split("?", 1)[0] == f"/api/session/{SESSION}/message":
            self.state.message_reads += 1
            if self.state.delay_startup_messages and self.state.message_reads >= 2 and not self.state.messages:
                self.state.messages = self.state.startup_messages(self.state.pending_startup_prompt)
            query = parse_qs(urlsplit(self.path).query)
            if "cursor" in query:
                messages = []
            else:
                messages = self.state.messages
                if query.get("type") == ["user"]:
                    messages = [row for row in messages if row.get("type") == "user"]
                if query.get("order") == ["desc"]:
                    messages = list(reversed(messages))
                if "limit" in query:
                    messages = messages[:int(query["limit"][0])]
            cursor = ({"previous": "fixture-previous", "next": "fixture-next"} if messages
                      else {"previous": None, "next": None})
            return self.respond(200, {"data": messages, "cursor": cursor})
        return self.respond(404)

    def do_POST(self):
        self.state.calls.append(("POST", self.path, self.headers.get("Authorization")))
        if not self.authenticated():
            return
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size).decode())
        if self.path == "/api/session":
            session_id = "ses_wrongcreate" if self.state.wrong_create_id else SESSION
            location = payload.get("location", {})
            self.state.session_directory = location.get("directory", "")
            self.state.session_model = payload.get("model")
            self.state.session_parent_id = payload.get("parentID")
            self.state.session_metadata = payload.get("metadata", {})
            if self.state.delay_create:
                time.sleep(2.2)
            try:
                return self.respond(200, {"data": self.state.session(session_id)})
            except OSError:
                return
        if self.path == f"/api/session/{SESSION}/prompt":
            self.state.sent_prompts.append(payload)
            return self.respond(200, {"data": {"id": f"msg_user{len(self.state.sent_prompts)}",
                                                "sessionID": SESSION, "type": "user", "time": {},
                                                "payload": {}, "delivery": {}}})
        return self.respond(404)

    def do_PUT(self):
        self.state.calls.append(("PUT", self.path, self.headers.get("Authorization")))
        if not self.authenticated():
            return
        size = int(self.headers.get("Content-Length", "0"))
        self.state.environment = json.loads(self.rfile.read(size).decode())
        self.send_response(204)
        self.end_headers()


class OpenCodeDispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="model-preferences-opencode-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = FakeAPIState(self.root)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        self.server.state = self.state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.password_file = self.root / "password"
        self.password_file.write_text(CANARY)
        self.password_file.chmod(0o600)
        self.manifest = self.root / "connection.json"
        self.manifest.write_text(json.dumps({
            "api_url": f"http://127.0.0.1:{self.server.server_port}",
            "expected_server_pid": 4242,
            "password_file": str(self.password_file),
            "parent_task_id": "task-parent-fixture",
            "metadata": {"provider_id": PROVIDER, "model_id": MODEL},
        }))
        self.manifest.chmod(0o600)
        self.choice = {"harness": "opencode", "provider_id": PROVIDER, "model": MODEL,
                       "effort": "max", "selection": "a" * 64}

    def tui_descriptor(self):
        return {
            "schema_version": dispatch.TUI_CHILD_DESCRIPTOR_SCHEMA,
            "manifest_path": str(self.manifest), "session_id": SESSION,
            "directory": str(self.root), "prompt_file": str(self.root / "prompt"),
            "provider_id": PROVIDER, "model_id": MODEL, "launch_id": "d" * 32,
            "selection": "a" * 64, "task_digest": "e" * 64,
            "xdg_environment": {
                "XDG_CONFIG_HOME": str(self.root / "private-config"),
                "XDG_DATA_HOME": str(self.root / "private-data"),
                "XDG_STATE_HOME": str(self.root / "private-state"),
                "XDG_CACHE_HOME": str(self.root / "private-cache"),
            },
            "handshake_file": str(self.root / "handshake.json"),
            "release_file": str(self.root / "release.json"),
            "abort_file": str(self.root / "abort.json"),
            "binary": "/fixture/bin/opencode",
        }

    def test_private_tui_launch_descriptor_reaches_child_cli(self):
        descriptor_path = self.root / "launch.child.json"
        descriptor = self.tui_descriptor()
        dispatch.write_tui_child_descriptor(descriptor_path, descriptor)
        seen = []
        with patch.object(dispatch, "tui_child", side_effect=lambda *args: seen.append(args) or 7):
            result = dispatch.main(["tui-child", "--launch-file", str(descriptor_path)])
        self.assertEqual(result, 7)
        self.assertEqual(seen, [(
            descriptor["manifest_path"], descriptor["session_id"], descriptor["directory"],
            descriptor["prompt_file"], descriptor["provider_id"], descriptor["model_id"],
            descriptor["launch_id"], descriptor["selection"], descriptor["task_digest"],
            descriptor["handshake_file"], descriptor["release_file"], descriptor["abort_file"],
            descriptor["binary"], None, descriptor["xdg_environment"],
        )])
        raw = descriptor_path.read_text()
        self.assertNotIn(CANARY, raw)
        self.assertNotIn(CMUX_CANARY, raw)
        self.assertEqual(descriptor_path.stat().st_mode & 0o777, 0o600)

    def test_tui_launch_descriptor_rejects_malformed_public_symlink_and_unknown_schema(self):
        cases = []
        malformed = self.root / "malformed.child.json"
        malformed.write_text("{")
        malformed.chmod(0o600)
        cases.append(("malformed", malformed))
        public = self.root / "public.child.json"
        dispatch.write_tui_child_descriptor(public, self.tui_descriptor())
        public.chmod(0o644)
        cases.append(("public", public))
        target = self.root / "target.child.json"
        dispatch.write_tui_child_descriptor(target, self.tui_descriptor())
        symlink = self.root / "symlink.child.json"
        symlink.symlink_to(target)
        cases.append(("symlink", symlink))
        unknown = self.root / "unknown.child.json"
        unsupported = self.tui_descriptor(); unsupported["extra"] = "ignored?"
        dispatch.write_private_json(unknown, unsupported)
        cases.append(("unknown", unknown))
        secret_env = self.root / "secret-env.child.json"
        invalid_env_key = self.tui_descriptor()
        invalid_env_key["xdg_environment"] = {"CMUX_SOCKET_CAPABILITY": CMUX_CANARY}
        dispatch.write_private_json(secret_env, invalid_env_key)
        cases.append(("secret_env_key", secret_env))
        bad_path = self.root / "bad-xdg-path.child.json"
        invalid_path = self.tui_descriptor()
        invalid_path["xdg_environment"] = {"XDG_STATE_HOME": "relative/state"}
        dispatch.write_private_json(bad_path, invalid_path)
        cases.append(("bad_xdg_path", bad_path))
        wrong_session = self.root / "wrong-session.child.json"
        invalid_session = self.tui_descriptor(); invalid_session["session_id"] = "session-from-title"
        dispatch.write_private_json(wrong_session, invalid_session)
        cases.append(("wrong_session", wrong_session))
        wrong_launch = self.root / "wrong-launch.child.json"
        invalid_launch = self.tui_descriptor(); invalid_launch["launch_id"] = "not-a-launch-id"
        dispatch.write_private_json(wrong_launch, invalid_launch)
        cases.append(("wrong_launch", wrong_launch))
        for name, path in cases:
            with self.subTest(name=name), contextlib.redirect_stderr(io.StringIO()) as stderr:
                with patch.object(dispatch, "tui_child") as child:
                    self.assertEqual(dispatch.main(["tui-child", "--launch-file", str(path)]), 1)
                    child.assert_not_called()
                self.assertNotIn(CANARY, stderr.getvalue())
                self.assertNotIn(CMUX_CANARY, stderr.getvalue())

    def test_tui_startup_command_is_short_quoted_and_bounded(self):
        helper = self.root / "opencode_dispatch.py"
        descriptor = self.root / "launch.child.json"
        command = dispatch.tui_child_command(helper, descriptor, sys.executable)
        self.assertLessEqual(len(command.encode("utf-8")), dispatch.MAX_TUI_CHILD_COMMAND_BYTES)
        self.assertEqual(shlex.split(command), [sys.executable, str(helper), "tui-child",
                                                "--launch-file", str(descriptor)])
        with self.assertRaisesRegex(dispatch.OpenCodeDispatchError, "cmux_startup_command_too_long"):
            dispatch.tui_child_command(helper, self.root / ("x" * 600), sys.executable)

    def test_caller_private_xdg_environment_copies_only_the_four_explicit_paths(self):
        paths = {key: str(self.root / key.lower()) for key in (
            "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")}
        source = {**paths, "HOME": str(self.root / "home"), "PATH": "/private/bin",
                  "CMUX_SOCKET_CAPABILITY": CMUX_CANARY, "OPENCODE_SERVER_PASSWORD": CANARY}
        self.assertEqual(dispatch.caller_private_xdg_environment(source), paths)
        self.assertEqual(dispatch.caller_private_xdg_environment({}), {})

    def test_legacy_direct_tui_child_cli_remains_supported(self):
        descriptor = self.tui_descriptor()
        args = ["tui-child"]
        for name, key in (
                ("manifest", "manifest_path"), ("session", "session_id"),
                ("directory", "directory"), ("prompt-file", "prompt_file"),
                ("provider-id", "provider_id"), ("model-id", "model_id"),
                ("launch-id", "launch_id"), ("selection", "selection"),
                ("task-digest", "task_digest"), ("handshake-file", "handshake_file"),
                ("release-file", "release_file"), ("abort-file", "abort_file"),
                ("binary", "binary")):
            args.extend(["--" + name, descriptor[key]])
        seen = []
        with patch.object(dispatch, "tui_child", side_effect=lambda *values: seen.append(values) or 8):
            self.assertEqual(dispatch.main(args), 8)
        self.assertEqual(len(seen[0]), 14)
        self.assertIsNone(seen[0][-1])

    def test_overlong_child_descriptor_path_refuses_before_session_creation(self):
        config_dir = self.root / "long-config"
        config = mp.example_config(); config["active"] = "Codex-Qwen"
        mp.write(config_dir / "profiles.json", config)
        long_state_dir = self.root / ("s" * 180) / ("t" * 180)
        app = mp.App(config_dir, long_state_dir)
        worktree = self.root / "long-worktree"; worktree.mkdir()
        subprocess.run(["git", "-C", str(worktree), "init", "-q"], check=True)
        task_file = self.root / "long-task.txt"; task_file.write_text("A bounded synthetic task.")
        choice = mp.resolve(mp.selection(config), "implementer", None)
        args = SimpleNamespace(role="implementer", opencode_manifest=str(self.manifest), context_injector=None,
                               owner_worktree=None, receipt_file=None, task_file=str(task_file), slot=None)
        workspace, surface, pane = str(uuid.uuid4()), str(uuid.uuid4()), "pane-fixture"

        def cmux(argv, stdin=None):
            if "identify" in argv:
                return {"caller": {"workspace_id": workspace, "surface_id": surface}}
            if "tree" in argv:
                return {"windows": [{"workspaces": [{"id": workspace, "panes": [
                    {"id": pane, "surfaces": [{"id": surface}]}]}]}]}
            raise AssertionError(argv)

        with patch.object(mp, "run_json", side_effect=cmux), \
                patch.object(mp.opencode_dispatch, "check_opencode_cli", return_value="/fixture/bin/opencode"), \
                patch.object(mp.opencode_dispatch, "create_session") as create_session, \
                self.assertRaisesRegex(mp.PreferenceError, "cmux_startup_command_too_long"):
            mp.dispatch_opencode(app, args, config, choice, worktree, task_file.read_bytes())
        create_session.assert_not_called()
        self.assertFalse(long_state_dir.exists())

    def test_invalid_caller_xdg_path_refuses_before_session_creation(self):
        config_dir = self.root / "xdg-config"
        config = mp.example_config(); config["active"] = "Codex-Qwen"
        mp.write(config_dir / "profiles.json", config)
        app = mp.App(config_dir, self.root / "xdg-state")
        worktree = self.root / "xdg-worktree"; worktree.mkdir()
        subprocess.run(["git", "-C", str(worktree), "init", "-q"], check=True)
        task_file = self.root / "xdg-task.txt"; task_file.write_text("A bounded synthetic task.")
        choice = mp.resolve(mp.selection(config), "implementer", None)
        args = SimpleNamespace(role="implementer", opencode_manifest=str(self.manifest), context_injector=None,
                               owner_worktree=None, receipt_file=None, task_file=str(task_file), slot=None)
        workspace, surface, pane = str(uuid.uuid4()), str(uuid.uuid4()), "pane-fixture"

        def cmux(argv, stdin=None):
            if "identify" in argv:
                return {"caller": {"workspace_id": workspace, "surface_id": surface}}
            if "tree" in argv:
                return {"windows": [{"workspaces": [{"id": workspace, "panes": [
                    {"id": pane, "surfaces": [{"id": surface}]}]}]}]}
            raise AssertionError(argv)

        with patch.dict(os.environ, {"XDG_STATE_HOME": "relative/state"}), \
                patch.object(mp, "run_json", side_effect=cmux), \
                patch.object(mp.opencode_dispatch, "check_opencode_cli", return_value="/fixture/bin/opencode"), \
                patch.object(mp.opencode_dispatch, "create_session") as create_session, \
                self.assertRaisesRegex(mp.PreferenceError, "invalid_child_xdg_environment"):
            mp.dispatch_opencode(app, args, config, choice, worktree, task_file.read_bytes())
        create_session.assert_not_called()

    def continuation_fixture(self, *, native_state="idle", guard_state="empty", wrong_surface=False):
        self.state.active_sessions = {}
        self.state.pending_permissions = []
        self.state.pending_forms = []
        self.state.sent_prompts = []
        fixture_id = uuid.uuid4().hex
        config_dir, state_dir = self.root / ("resume-config-" + fixture_id), self.root / ("resume-state-" + fixture_id)
        config = mp.example_config()
        config["active"] = "Qwen-only"
        mp.write(config_dir / "profiles.json", config)
        app = mp.App(config_dir, state_dir)
        worktree = self.root / ("resume-worktree-" + uuid.uuid4().hex)
        worktree.mkdir()
        subprocess.run(["git", "-C", str(worktree), "init", "-q"], check=True)
        connection = dispatch.load_connection(self.manifest)
        choice = mp.resolve(mp.selection(config), "implementer", None)
        launch_id = uuid.uuid4().hex
        original = b"Original bounded task."
        task_digest = hashlib.sha256(original).hexdigest()
        created = dispatch.create_session(connection, directory=str(worktree), choice=choice,
                                          launch_id=launch_id, task_digest=task_digest,
                                          parent_task_id=connection.parent_task_id)
        workspace, surface = str(uuid.uuid4()), str(uuid.uuid4())
        launch = {"id": launch_id, "choice": choice, "cwd": str(worktree), "created_at": "fixture",
                  "status": "running", "native_session": SESSION, "parent_task_id": connection.parent_task_id,
                  "task_id": connection.parent_task_id, "task_digest": task_digest,
                  "api_origin": connection.api_origin, "api_contract": "opencode-v2.2.5-model-preflight-1",
                  "expected_server_pid": connection.expected_server_pid,
                  "session_directory": created["directory"], "worktree_directory": str(worktree),
                  "cmux_workspace_uuid": workspace, "cmux_surface_uuid": surface,
                  "native_started_at": 123.0, "execution_result": "turn_idle", "slot": None,
                  "context_injector": None, "opencode_parent_session_id": None}
        app.save_state(f"launches/{launch_id}", launch)
        hook_path = self.root / ("resume-hook-" + uuid.uuid4().hex + ".json")
        dispatch.write_private_json(hook_path, {"sessions": {SESSION: {
            "workspaceId": workspace, "surfaceId": surface, "startedAt": 123.0,
            "agentLifecycle": "idle", "cwd": str(worktree)}}})
        visible_surface = str(uuid.uuid4()) if wrong_surface else surface
        current = {"truncated": False, "owner_availability": {"agent_sessions": "available"},
                   "items": [{"lifecycle": "running", "placement": {"kind": "local"},
                              "freshness": {"state": "current"},
                              "projections": [{"workspace_id": workspace, "panel_id": visible_surface}],
                              "agents": [{"kind": "opencode", "session_id": SESSION,
                                          "state": native_state}]}]}

        def cmux(argv, stdin=None):
            method = argv[argv.index("rpc") + 1]
            params = json.loads(argv[-1])
            if method == "surface.input_state":
                return {"surface_id": params["surface_id"], "workspace_id": workspace,
                        "agent": True, "terminal": True, "state": guard_state,
                        "blocks_typing": guard_state != "empty", "waiting_on_human": False,
                        "lifecycle": "idle"}
            if method == "agent.message.settings":
                return {"messages_enabled": True, "receiving": True, "workspace_receiving": True}
            raise AssertionError(method)

        args = SimpleNamespace(launch=launch_id, task_file=str(self.root / "followup.txt"),
                               opencode_manifest=str(self.manifest), review_plan=None, approval=None,
                               review_authorization=None)
        Path(args.task_file).write_bytes(b"Continue with one distinct synthetic task.")
        return app, launch, args, current, hook_path, cmux

    def test_preflight_and_create_use_exact_model_without_prompt_or_permission_calls(self):
        proof = dispatch.preflight(self.manifest, self.choice)
        self.assertEqual(proof.server_pid, 4242)
        self.assertEqual(proof.local_provider_origin, "http://127.0.0.1:11459")
        self.assertEqual(proof.output_limit, 32768)
        self.assertTrue(proof.tools)
        self.assertTrue(all(method == "GET" for method, _, _ in self.state.calls))

        connection = dispatch.load_connection(self.manifest)
        launch_id, task_digest = "b" * 32, "c" * 64
        created = dispatch.create_session(connection, directory=str(self.root), choice=self.choice,
                                          launch_id=launch_id, task_digest=task_digest,
                                          parent_task_id=connection.parent_task_id)
        self.assertEqual(created["session_id"], SESSION)
        self.assertEqual(created["model"], {"provider_id": PROVIDER, "id": MODEL, "variant": "max"})
        payload = self.state.session_metadata["starfleet.model-preferences"]
        self.assertEqual(payload["launch_id"], launch_id)
        self.assertEqual(payload["task_sha256"], task_digest)
        self.assertEqual(payload["parent_task_id"], "task-parent-fixture")
        dispatch.set_session_environment(connection, SESSION, workspace_uuid=str(uuid.uuid4()),
                                         surface_uuid=str(uuid.uuid4()), launch_id=launch_id,
                                         cmux_socket_path="/private/tmp/cmux.sock",
                                         cmux_socket_capability=CMUX_CANARY,
                                         model_preferences_manifest=str(self.manifest),
                                         task_id="task-child-fixture")
        self.assertEqual(self.state.calls[-1][0:2], ("PUT", f"/api/session/{SESSION}/environment"))
        self.assertIn("OPENCODE_SESSION_ID", self.state.environment["variables"])
        self.assertEqual(self.state.environment["variables"]["CMUX_SOCKET_CAPABILITY"], CMUX_CANARY)
        self.assertEqual(self.state.environment["variables"]["MODEL_PREFERENCES_TASK_ID"], "task-child-fixture")
        self.assertNotIn("/prompt", [path for _, path, _ in self.state.calls])
        self.assertNotIn(CANARY, repr(connection))

    def test_cold_provider_registry_is_initialized_by_model_catalog_read(self):
        self.state.lazy_provider_registry = True
        proof = dispatch.preflight(self.manifest, self.choice)
        self.assertEqual(proof.provider_id, PROVIDER)
        paths = [path.split("?", 1)[0] for method, path, _ in self.state.calls if method == "GET"]
        self.assertLess(paths.index("/api/model"), paths.index(f"/api/provider/{PROVIDER}"))

    def test_provider_http_diagnostic_exposes_only_operation_and_status(self):
        self.state.provider_route_missing = True
        with self.assertRaises(dispatch.OpenCodeDispatchError) as raised:
            dispatch.preflight(self.manifest, self.choice)
        self.assertEqual(raised.exception.code, "provider_config_http_status_404")
        self.assertNotIn("provider configuration unavailable", str(raised.exception))
        self.assertNotIn(CANARY, str(raised.exception))

    def test_opencode_cli_version_accepts_exact_banner_and_bare_version_only(self):
        binary = "/fixture/bin/opencode"
        accepted = ("opencode v2.2.5\n", "2.2.5", "2.2.5\n")
        rejected = ("opencode v2.2.6\n", "prefix opencode v2.2.5\n",
                    "opencode v2.2.5\nextra\n", "2.2.5\nextra\n")
        for output in accepted:
            with self.subTest(output=repr(output)), \
                    patch.object(dispatch.subprocess, "run", return_value=SimpleNamespace(
                        returncode=0, stdout=output, stderr="")) as run:
                self.assertEqual(dispatch.check_opencode_cli(binary), str(Path(binary).resolve()))
                run.assert_called_once_with([binary, "--version"], capture_output=True,
                                            text=True, timeout=5)
        for output in rejected:
            with self.subTest(output=repr(output)), \
                    patch.object(dispatch.subprocess, "run", return_value=SimpleNamespace(
                        returncode=0, stdout=output, stderr="")), \
                    self.assertRaisesRegex(dispatch.OpenCodeDispatchError, "unsupported_opencode_cli_version"):
                dispatch.check_opencode_cli(binary)
        with patch.object(dispatch.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout="opencode v2.2.5\n", stderr="failure")), \
                self.assertRaisesRegex(dispatch.OpenCodeDispatchError, "unsupported_opencode_cli_version"):
            dispatch.check_opencode_cli(binary)

    def test_child_session_keeps_native_parent_session_separate_from_parent_task(self):
        connection = dispatch.load_connection(self.manifest)
        parent_session = "ses_parentFixture123"
        created = dispatch.create_session(connection, directory=str(self.root), choice=self.choice,
                                          launch_id="d" * 32, task_digest="e" * 64,
                                          parent_task_id=connection.parent_task_id,
                                          parent_session_id=parent_session)
        self.assertEqual(created["parent_session_id"], parent_session)
        self.assertEqual(self.state.session_metadata["starfleet.model-preferences"]["parent_task_id"],
                         "task-parent-fixture")
        self.assertEqual(self.state.session_metadata["starfleet.model-preferences"]["parent_session_id"],
                         parent_session)
        self.assertEqual(self.state.session_parent_id, parent_session)
        self.assertNotEqual(created["parent_session_id"], connection.parent_task_id)
        self.state.session_parent_id = "ses_differentParent123"
        with self.assertRaisesRegex(dispatch.OpenCodeDispatchError, "created_session_parent_session_mismatch"):
            dispatch.verify_session(connection, SESSION, expected_directory=str(self.root), choice=self.choice,
                                    launch_id="d" * 32, task_digest="e" * 64,
                                    parent_task_id=connection.parent_task_id,
                                    parent_session_id=parent_session)

    def test_startup_message_proof_requires_exact_prompt_and_completed_configured_model(self):
        connection = dispatch.load_connection(self.manifest)
        prompt = "Exact synthetic startup prompt"
        prompt_digest = hashlib.sha256(prompt.encode()).hexdigest()
        self.state.messages = self.state.startup_messages(prompt)
        observation = dispatch.startup_turn_observation(
            connection, session_id=SESSION, prompt_sha256=prompt_digest,
            provider_id=PROVIDER, model_id=MODEL)
        self.assertEqual(observation["state"], "complete")
        self.assertEqual(observation["user_message_id"], "msg_startupUser123")
        self.assertNotIn(prompt, json.dumps(observation))
        pages = [urlsplit(path).query for method, path, _ in self.state.calls
                 if method == "GET" and urlsplit(path).path == f"/api/session/{SESSION}/message"]
        self.assertEqual(len(pages), 2)
        first_query, terminal_query = (parse_qs(page) for page in pages)
        self.assertEqual(first_query, {"limit": ["100"], "order": ["asc"]})
        self.assertEqual(set(terminal_query), {"limit", "cursor"})
        self.assertEqual(terminal_query["limit"], ["100"])
        self.assertTrue(dispatch.latest_user_message_matches(
            connection, session_id=SESSION, message_id="msg_startupUser123", text_sha256=prompt_digest))
        assistant = next(row for row in self.state.messages if row.get("type") == "assistant")
        assistant["model"]["variant"] = "default"
        with self.assertRaises(dispatch.OpenCodeDispatchError) as raised:
            dispatch.startup_turn_observation(
                connection, session_id=SESSION, prompt_sha256=prompt_digest,
                provider_id=PROVIDER, model_id=MODEL)
        self.assertEqual(raised.exception.code, "startup_assistant_model_mismatch")
        assistant["model"].pop("variant")
        with self.assertRaises(dispatch.OpenCodeDispatchError) as raised:
            dispatch.startup_turn_observation(
                connection, session_id=SESSION, prompt_sha256=prompt_digest,
                provider_id=PROVIDER, model_id=MODEL)
        self.assertEqual(raised.exception.code, "startup_assistant_model_mismatch")
        assistant["model"]["variant"] = "max"
        self.state.messages[0]["text"] = "different prompt"
        with self.assertRaises(dispatch.OpenCodeDispatchError) as raised:
            dispatch.startup_turn_observation(
                connection, session_id=SESSION, prompt_sha256=prompt_digest,
                provider_id=PROVIDER, model_id=MODEL)
        self.assertEqual(raised.exception.code, "startup_prompt_identity_mismatch")

    def test_capability_handoff_rejects_missing_or_unbounded_values(self):
        connection = dispatch.load_connection(self.manifest)
        for value in (None, "", "  padded", "bad\nvalue", "x" * 4097):
            with self.subTest(value=repr(value)[:16]), self.assertRaises(dispatch.OpenCodeDispatchError) as raised:
                dispatch.set_session_environment(connection, SESSION, workspace_uuid=str(uuid.uuid4()),
                                                surface_uuid=str(uuid.uuid4()), launch_id="a" * 32,
                                                cmux_socket_path=None, cmux_socket_capability=value)
            self.assertEqual(raised.exception.code, "cmux_socket_capability_unavailable")
        self.assertFalse(any(method == "PUT" for method, _, _ in self.state.calls))

    def test_opencode_continuation_requires_native_empty_composer_and_persists_intent_before_send(self):
        app, launch, args, current, hook_path, cmux = self.continuation_fixture()
        original_send = dispatch.send_prompt

        def send_after_intent(connection, *, session_id, text):
            intent = app.state(f"continuations/{SESSION}", None)
            self.assertEqual(intent["status"], "send_intent")
            self.assertEqual(intent["session"], SESSION)
            self.assertNotIn(text, json.dumps(intent))
            return original_send(connection, session_id=session_id, text=text)

        with patch.object(app, "current", return_value=current), \
                patch.object(mp.opencode_dispatch, "native_hook_store_path", return_value=hook_path), \
                patch.object(mp, "run_json", side_effect=cmux), \
                patch.object(mp.opencode_dispatch, "send_prompt", side_effect=send_after_intent):
            result = mp.resume_opencode_launch(app, args, Path(args.task_file).read_bytes())
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["session"], SESSION)
        self.assertEqual(len(self.state.sent_prompts), 1)
        saved = app.state(f"continuations/{SESSION}", None)
        self.assertEqual(saved["status"], "submitted")
        self.assertEqual(saved["message_id"], result["message_id"])
        self.assertNotIn("Continue with one distinct synthetic task.", json.dumps(saved))

    def test_opencode_continuation_holds_busy_draft_unknown_and_wrong_surface_without_sending(self):
        cases = (("busy", {"active": {SESSION: {"type": "running"}}}, "empty", False,
                  "session_guard_session_busy"),
                 ("permission", {"permission": [{"id": "permission-fixture"}]}, "empty", False,
                  "session_guard_pending_permissions"),
                 ("form", {"form": [{"id": "form-fixture"}]}, "empty", False,
                  "session_guard_pending_forms"),
                 ("draft", {}, "draft", False, "native_composer_not_empty"),
                 ("unknown", {}, "unknown", False, "native_composer_not_empty"),
                 ("wrong_surface", {}, "empty", True, "native_session_visible_on_other_surface"))
        for name, api_state, guard_state, wrong_surface, expected in cases:
            with self.subTest(name=name):
                app, launch, args, current, hook_path, _ = self.continuation_fixture(
                    guard_state=guard_state, wrong_surface=wrong_surface)
                self.state.active_sessions = api_state.get("active", {})
                self.state.pending_permissions = api_state.get("permission", [])
                self.state.pending_forms = api_state.get("form", [])

                def cmux(argv, stdin=None):
                    method = argv[argv.index("rpc") + 1]
                    params = json.loads(argv[-1])
                    if method == "surface.input_state":
                        return {"surface_id": params["surface_id"],
                                "workspace_id": launch["cmux_workspace_uuid"],
                                "agent": True, "terminal": True, "state": guard_state,
                                "blocks_typing": guard_state != "empty", "waiting_on_human": False,
                                "lifecycle": "idle"}
                    return {"messages_enabled": True, "receiving": True, "workspace_receiving": True}

                with patch.object(app, "current", return_value=current), \
                        patch.object(mp.opencode_dispatch, "native_hook_store_path", return_value=hook_path), \
                        patch.object(mp, "run_json", side_effect=cmux), \
                        self.assertRaisesRegex(mp.PreferenceError, expected):
                    mp.resume_opencode_launch(app, args, Path(args.task_file).read_bytes())
                self.assertEqual(self.state.sent_prompts, [])

    def test_uncertain_opencode_prompt_send_blocks_retry(self):
        app, launch, args, current, hook_path, cmux = self.continuation_fixture()
        calls = []

        def uncertain_send(*args, **kwargs):
            calls.append(1)
            raise mp.opencode_dispatch.OpenCodeDispatchError("http_timeout")

        with patch.object(app, "current", return_value=current), \
                patch.object(mp.opencode_dispatch, "native_hook_store_path", return_value=hook_path), \
                patch.object(mp, "run_json", side_effect=cmux), \
                patch.object(mp.opencode_dispatch, "send_prompt", side_effect=uncertain_send), \
                self.assertRaisesRegex(mp.PreferenceError, "admission is uncertain"):
            mp.resume_opencode_launch(app, args, Path(args.task_file).read_bytes())
        self.assertEqual(app.state(f"continuations/{SESSION}", None)["status"], "uncertain")
        with patch.object(app, "current", return_value=current), \
                patch.object(mp.opencode_dispatch, "native_hook_store_path", return_value=hook_path), \
                patch.object(mp, "run_json", side_effect=cmux), \
                patch.object(mp.opencode_dispatch, "send_prompt", side_effect=uncertain_send), \
                self.assertRaisesRegex(mp.PreferenceError, "uncertain admission"):
            mp.resume_opencode_launch(app, args, Path(args.task_file).read_bytes())
        self.assertEqual(len(calls), 1)

    def test_fresh_qwen_only_orchestrator_gets_exact_pattern_buffer_assignment(self):
        config_dir, state_dir = self.root / "orch-config", self.root / "orch-state"
        config = mp.example_config(); config["active"] = "Qwen-only"
        mp.validate(config); mp.write(config_dir / "profiles.json", config)
        app = mp.App(config_dir, state_dir)
        worktree = self.root / "orch-worktree"; worktree.mkdir()
        subprocess.run(["git", "-C", str(worktree), "init", "-q"], check=True)
        task_file = self.root / "orch-task.txt"; task_file.write_text("Coordinate this synthetic task.")
        pb_script = self.root / "pattern_buffer.py"; pb_script.write_text("# synthetic PB boundary\n")
        choice = mp.resolve(mp.selection(config), "orchestrator", None)
        assignment_sha = "a" * 64
        context = {"status": "context", "harness": "opencode", "session_id": SESSION,
                   "worktree": str(worktree), "text": "pattern-buffer | role: orchestrator | fixture",
                   "assignment_sha256": assignment_sha}
        app.run_command = lambda argv, stdin=None: "assigned"
        app.run_json = lambda argv, stdin=None: context if "context" in argv else {
            "truncated": False, "owner_availability": {"agent_sessions": "available"},
            "items": [{"lifecycle": "idle", "placement": {"kind": "local"},
                       "freshness": {"state": "current"},
                       "projections": [{"workspace_id": workspace, "panel_id": opened_surface}],
                       "agents": [{"kind": "opencode", "session_id": SESSION, "state": "idle"}]}]}

        workspace, caller_surface, pane, opened_surface = (
            str(uuid.uuid4()), str(uuid.uuid4()), "pane-fixture", str(uuid.uuid4()))
        hook_path = self.root / "orch-hook.json"
        prompt_seen = {}

        def cmux(argv, stdin=None):
            if "identify" in argv:
                return {"caller": {"workspace_id": workspace, "surface_id": caller_surface}}
            if "tree" in argv:
                return {"windows": [{"workspaces": [{"id": workspace, "panes": [
                    {"id": pane, "surfaces": [{"id": caller_surface}]}]}]}]}
            if "new-surface" in argv:
                child = shlex.split(argv[argv.index("--command") + 1])
                def get(flag): return child_value(child, flag)
                prompt_text = Path(get("prompt-file")).read_text()
                prompt_seen["text"] = prompt_text
                self.state.messages = self.state.startup_messages(prompt_text)
                dispatch.write_private_json(get("handshake-file"), {
                    "launch_id": get("launch-id"), "session_id": get("session"),
                    "workspace_uuid": workspace, "surface_uuid": opened_surface,
                    "cmux_socket_path": None, "cmux_socket_capability": CMUX_CANARY})
                dispatch.write_private_json(hook_path, {"sessions": {SESSION: {
                    "workspaceId": workspace, "surfaceId": opened_surface, "startedAt": 456.0,
                    "agentLifecycle": "idle", "cwd": str(worktree)}}})
                return {"workspace_id": workspace, "surface_id": opened_surface}
            if "current" in argv:
                return {"truncated": False, "owner_availability": {"agent_sessions": "available"},
                        "items": [{"lifecycle": "idle", "placement": {"kind": "local"},
                                   "freshness": {"state": "current"},
                                   "projections": [{"workspace_id": workspace, "panel_id": opened_surface}],
                                   "agents": [{"kind": "opencode", "session_id": SESSION, "state": "idle"}]}]}
            if "rpc" in argv:
                method = argv[argv.index("rpc") + 1]
                params = json.loads(argv[-1])
                if method == "surface.input_state":
                    return {"surface_id": params["surface_id"], "workspace_id": workspace,
                            "agent": True, "terminal": True, "state": "empty", "blocks_typing": False,
                            "waiting_on_human": False, "lifecycle": "idle"}
                return {"messages_enabled": True, "receiving": True, "workspace_receiving": True}
            raise AssertionError(argv)

        args = SimpleNamespace(role="orchestrator", opencode_manifest=str(self.manifest),
                               context_injector=str(pb_script), owner_worktree=None, receipt_file=None,
                               task_file=str(task_file), slot=None)
        with patch.object(mp, "run_json", side_effect=cmux), \
                patch.object(mp.opencode_dispatch, "native_hook_store_path", return_value=hook_path), \
                patch.object(mp.opencode_dispatch, "check_opencode_cli", return_value="/fixture/bin/opencode"):
            result = mp.dispatch_opencode(app, args, config, choice, worktree, task_file.read_bytes())
        saved = app.state(f"launches/{result['launch']}", None)
        self.assertEqual(result["session"], SESSION)
        self.assertEqual(saved["choice"]["role"], "orchestrator")
        self.assertEqual(saved["pattern_buffer_assignment_sha256"], assignment_sha)
        self.assertEqual(saved["task_id"], "task-parent-fixture")
        self.assertIsNone(saved["opencode_parent_session_id"])
        self.assertIn("fresh Qwen-only orchestrator", prompt_seen["text"])
        self.assertEqual(self.state.environment["variables"]["MODEL_PREFERENCES_TASK_ID"],
                         "task-parent-fixture")
        self.assertEqual(self.state.environment["variables"]["MODEL_PREFERENCES_PATTERN_BUFFER"],
                         str(pb_script))
        self.assertNotIn(CMUX_CANARY, json.dumps(saved))

    def test_active_parent_proof_keeps_task_session_and_pb_assignment_distinct(self):
        config_dir, state_dir = self.root / "parent-config", self.root / "parent-state"
        config = mp.example_config(); config["active"] = "Qwen-only"
        mp.write(config_dir / "profiles.json", config)
        app = mp.App(config_dir, state_dir)
        worktree = self.root / "parent-worktree"; worktree.mkdir()
        subprocess.run(["git", "-C", str(worktree), "init", "-q"], check=True)
        pb_script = self.root / "parent-pattern-buffer.py"; pb_script.write_text("# fixture\n")
        choice = mp.resolve(mp.selection(config), "orchestrator", None)
        connection = dispatch.load_connection(self.manifest)
        launch_id, task_digest = "a" * 32, "b" * 64
        dispatch.create_session(connection, directory=str(worktree), choice=choice,
                                 launch_id=launch_id, task_digest=task_digest,
                                 parent_task_id=connection.parent_task_id)
        workspace, surface = str(uuid.uuid4()), str(uuid.uuid4())
        prompt = "Active exact parent task."
        prompt_digest = hashlib.sha256(prompt.encode()).hexdigest()
        self.state.messages = [{"id": "msg_parentUser123", "type": "user", "text": prompt,
                                "time": {"created": 10}}]
        assignment_sha = "c" * 64
        launch = {"id": launch_id, "choice": choice, "cwd": str(worktree),
                  "status": "running", "native_session": SESSION,
                  "parent_task_id": connection.parent_task_id, "task_id": connection.parent_task_id,
                  "task_digest": task_digest, "startup_prompt_sha256": prompt_digest,
                  "startup_turn": {"state": "running", "user_message_id": "msg_parentUser123"},
                  "api_origin": connection.api_origin, "expected_server_pid": connection.expected_server_pid,
                  "session_directory": str(worktree), "worktree_directory": str(worktree),
                  "cmux_workspace_uuid": workspace, "cmux_surface_uuid": surface,
                  "context_injector": {"script": str(pb_script), "role": "orchestrator", "session_id": SESSION},
                  "pattern_buffer_assignment_sha256": assignment_sha, "opencode_parent_session_id": None,
                  "created_at": "fixture"}
        app.save_state(f"launches/{launch_id}", launch)
        current = {"truncated": False, "owner_availability": {"agent_sessions": "available"},
                   "items": [{"lifecycle": "running", "placement": {"kind": "local"},
                              "freshness": {"state": "current"},
                              "projections": [{"workspace_id": workspace, "panel_id": surface}],
                              "agents": [{"kind": "opencode", "session_id": SESSION, "state": "working"}]}]}
        context = {"status": "context", "harness": "opencode", "session_id": SESSION,
                   "worktree": str(worktree), "text": "pattern-buffer | role: orchestrator | fixture",
                   "assignment_sha256": assignment_sha}
        app.current = lambda: current
        app.run_json = lambda argv, stdin=None: context
        environ = {"CMUX_AGENT_LAUNCH_KIND": "opencode", "OPENCODE_SESSION_ID": SESSION,
                   "CMUX_SURFACE_ID": surface, "CMUX_WORKSPACE_ID": workspace}
        with patch.dict(os.environ, environ):
            parent = mp.opencode_parent_caller(
                app, connection, worktree,
                {"surface_uuid": surface, "workspace_uuid": workspace})
        self.assertEqual(parent["session_id"], SESSION)
        self.assertEqual(parent["task_id"], "task-parent-fixture")
        self.assertNotEqual(parent["task_id"], parent["session_id"])
        self.assertEqual(parent["assignment_sha256"], assignment_sha)
        for interaction, attribute in (("permission", "pending_permissions"), ("form", "pending_forms")):
            with self.subTest(interaction=interaction):
                setattr(self.state, attribute, [{"id": f"{interaction}-fixture"}])
                with patch.dict(os.environ, environ), self.assertRaisesRegex(mp.PreferenceError, "pending_"):
                    mp.opencode_parent_caller(
                        app, connection, worktree,
                        {"surface_uuid": surface, "workspace_uuid": workspace})
                setattr(self.state, attribute, [])
        self.state.pending_permissions = {"unsupported": True}
        with patch.dict(os.environ, environ), self.assertRaisesRegex(mp.PreferenceError, "permission_state_unavailable"):
            mp.opencode_parent_caller(
                app, connection, worktree,
                {"surface_uuid": surface, "workspace_uuid": workspace})
        self.state.pending_permissions = []
        self.state.pid = 5252
        with patch.dict(os.environ, environ), self.assertRaisesRegex(mp.PreferenceError, "opencode_server_pid_mismatch"):
            mp.opencode_parent_caller(
                app, connection, worktree,
                {"surface_uuid": surface, "workspace_uuid": workspace})
        self.state.pid = 4242
        current["items"][0]["agents"][0]["state"] = "idle"
        with patch.dict(os.environ, environ), self.assertRaisesRegex(mp.PreferenceError, "currently executing"):
            mp.opencode_parent_caller(
                app, connection, worktree,
                {"surface_uuid": surface, "workspace_uuid": workspace})

    def test_dispatch_qwen_parent_to_distinct_child_worktree(self):
        config_dir, state_dir = self.root / "child-dispatch-config", self.root / "child-dispatch-state"
        config = mp.example_config(); config["active"] = "Qwen-only"
        mp.write(config_dir / "profiles.json", config)
        app = mp.App(config_dir, state_dir)
        owner = self.root / "owner-worktree"; owner.mkdir()
        child = self.root / "child-worktree"; child.mkdir()
        for worktree in (owner, child):
            subprocess.run(["git", "-C", str(worktree), "init", "-q"], check=True)
        pb_script = self.root / "child-pattern-buffer.py"; pb_script.write_text("# fixture\n")
        task_file = self.root / "child-task.txt"; task_file.write_text("Implement one bounded child task.")
        receipt_file = self.root / "child-receipt.json"
        parent_choice = mp.resolve(mp.selection(config), "orchestrator", None)
        child_choice = mp.resolve(mp.selection(config), "implementer", None)
        connection = dispatch.load_connection(self.manifest)
        parent_id, parent_digest = "a" * 32, "b" * 64
        dispatch.create_session(connection, directory=str(owner), choice=parent_choice,
                                 launch_id=parent_id, task_digest=parent_digest,
                                 parent_task_id=connection.parent_task_id)
        workspace, caller_surface = str(uuid.uuid4()), str(uuid.uuid4())
        child_surface, child_session = str(uuid.uuid4()), "ses_childFixture456"
        parent_prompt = "Dispatch one bounded child during this exact first task."
        parent_prompt_digest = hashlib.sha256(parent_prompt.encode()).hexdigest()
        self.state.messages = [{"id": "msg_parentChildTask123", "type": "user", "text": parent_prompt,
                                "time": {"created": 10}}]
        assignment_sha = "c" * 64
        app.save_state(f"launches/{parent_id}", {
            "id": parent_id, "choice": parent_choice, "cwd": str(owner),
            "status": "running", "native_session": SESSION,
            "parent_task_id": connection.parent_task_id, "task_id": connection.parent_task_id,
            "task_digest": parent_digest, "startup_prompt_sha256": parent_prompt_digest,
            "startup_turn": {"state": "running", "user_message_id": "msg_parentChildTask123"},
            "api_origin": connection.api_origin, "expected_server_pid": connection.expected_server_pid,
            "session_directory": str(owner), "worktree_directory": str(owner),
            "cmux_workspace_uuid": workspace, "cmux_surface_uuid": caller_surface,
            "context_injector": {"script": str(pb_script), "role": "orchestrator", "session_id": SESSION},
            "pattern_buffer_assignment_sha256": assignment_sha, "opencode_parent_session_id": None,
            "created_at": "fixture"})
        parent_context = {"status": "context", "harness": "opencode", "session_id": SESSION,
                          "worktree": str(owner), "text": "pattern-buffer | role: orchestrator | fixture",
                          "assignment_sha256": assignment_sha}
        child_assignment_sha = "d" * 64
        child_context = {"status": "context", "harness": "opencode", "session_id": child_session,
                         "worktree": str(child), "text": "pattern-buffer | role: implementer | fixture",
                         "assignment_sha256": child_assignment_sha}
        child_ticket = "e" * 32
        expected_parent = {"harness": "opencode", "session_id": SESSION,
                           "task_id": connection.parent_task_id, "role": "orchestrator",
                           "owner_worktree": str(owner),
                           "owner_gitdir": mp.git(owner, "rev-parse", "--absolute-git-dir").decode().strip(),
                           "surface_id": caller_surface.upper(), "workspace_id": workspace.upper(),
                           "assignment_sha256": assignment_sha}
        current_child = False
        hook_path = self.root / "child-hook-store.json"
        pb_actions = []
        create_requests = []

        def current_for(session, surface, state):
            return {"truncated": False, "owner_availability": {"agent_sessions": "available"},
                    "items": [{"lifecycle": "running" if state == "working" else "idle",
                               "placement": {"kind": "local"}, "freshness": {"state": "current"},
                               "projections": [{"workspace_id": workspace, "panel_id": surface}],
                               "agents": [{"kind": "opencode", "session_id": session, "state": state}]}]}

        def fake_create(conn, *, directory, choice, launch_id, task_digest, parent_task_id,
                        parent_session_id=None):
            create_requests.append({"directory": directory, "parent_session_id": parent_session_id,
                                    "parent_task_id": parent_task_id, "choice": choice})
            return {"session_id": child_session, "directory": str(child),
                    "parent_session_id": parent_session_id,
                    "model": {"provider_id": choice["provider_id"], "id": choice["model"], "variant": "max"}}

        def fake_cmux(argv, stdin=None):
            nonlocal current_child
            if argv[0] == sys.executable and argv[1] == str(pb_script):
                action = next(item for item in ("prepare-dispatch", "bind-dispatch", "context") if item in argv)
                pb_actions.append(action)
                if action == "context":
                    return parent_context if SESSION in argv else child_context
                if action == "prepare-dispatch":
                    receipt_path = Path(argv[argv.index("--receipt-file") + 1])
                    receipt_path.write_text(json.dumps({"task_id": child_ticket, "harness": "opencode",
                        "role": "implementer", "status": "pending", "session_id": None,
                        "worktree": str(child), "owner_worktree": str(owner),
                        "owner_gitdir": expected_parent["owner_gitdir"],
                        "brief_sha256": hashlib.sha256(task_file.read_bytes()).hexdigest(),
                        "ticket_version": 2, "parent_kind": "opencode", "parent": expected_parent}))
                    self.assertEqual(argv[argv.index("--cwd") + 1], str(child))
                    self.assertEqual(argv[argv.index("--owner-worktree") + 1], str(owner))
                    return {"status": "pending", "harness": "opencode", "role": "implementer",
                            "task_id": child_ticket, "ticket_version": 2,
                            "parent_kind": "opencode", "parent": expected_parent}
                if action == "bind-dispatch":
                    return {"status": "active", "task_id": child_ticket, "session_id": child_session,
                            "harness": "opencode", "role": "implementer"}
            if "identify" in argv:
                return {"caller": {"workspace_id": workspace, "surface_id": caller_surface}}
            if "tree" in argv:
                return {"windows": [{"workspaces": [{"id": workspace, "panes": [
                    {"id": "pane-child-dispatch", "surfaces": [{"id": caller_surface}]}]}]}]}
            if "new-surface" in argv:
                child_command = shlex.split(argv[argv.index("--command") + 1])
                def get(flag): return child_value(child_command, flag)
                prompt_text = Path(get("prompt-file")).read_text()
                self.assertEqual(get("session"), child_session)
                self.assertEqual(get("directory"), str(child))
                dispatch.write_private_json(get("handshake-file"), {
                    "launch_id": get("launch-id"), "session_id": child_session,
                    "workspace_uuid": workspace, "surface_uuid": child_surface,
                    "cmux_socket_path": None, "cmux_socket_capability": CMUX_CANARY})
                dispatch.write_private_json(hook_path, {"sessions": {child_session: {
                    "workspaceId": workspace, "surfaceId": child_surface, "startedAt": 987.0,
                    "agentLifecycle": "idle", "cwd": str(child)}}})
                current_child = True
                return {"workspace_id": workspace, "surface_id": child_surface}
            if "current" in argv:
                return current_for(child_session, child_surface, "idle") if current_child else current_for(
                    SESSION, caller_surface, "working")
            if "rpc" in argv:
                method = argv[argv.index("rpc") + 1]
                params = json.loads(argv[-1])
                if method == "surface.input_state":
                    return {"surface_id": params["surface_id"], "workspace_id": workspace,
                            "agent": True, "terminal": True, "state": "empty", "blocks_typing": False,
                            "waiting_on_human": False, "lifecycle": "idle"}
                if method == "agent.message.settings":
                    return {"messages_enabled": True, "receiving": True, "workspace_receiving": True}
            raise AssertionError(f"Unexpected fake dispatch call: {argv}")

        def startup_observation(conn, *, session_id, prompt_sha256, provider_id, model_id):
            self.assertEqual(session_id, child_session)
            return {"state": "complete", "user_message_id": "msg_childUser123",
                    "assistant_message_id": "msg_childAssistant123", "finish": "stop"}

        def child_identity(conn, *, session_id, session_directory, worktree_directory,
                           workspace_uuid, surface_uuid, parent_task_id):
            self.assertEqual(session_id, child_session)
            self.assertEqual(session_directory, str(child))
            self.assertEqual(worktree_directory, str(child))
            return {"opencode_session_id": child_session, "opencode_parent_session_id": SESSION,
                    "worktree_directory": str(child), "cmux_workspace_uuid": workspace,
                    "cmux_surface_uuid": child_surface, "parent_task_id": parent_task_id}

        args = SimpleNamespace(role="implementer", opencode_manifest=str(self.manifest),
                               context_injector=str(pb_script), owner_worktree=str(owner),
                               receipt_file=str(receipt_file), task_file=str(task_file), slot=None)
        with patch.dict(os.environ, {"CMUX_AGENT_LAUNCH_KIND": "opencode", "OPENCODE_SESSION_ID": SESSION,
                                     "CMUX_SURFACE_ID": caller_surface, "CMUX_WORKSPACE_ID": workspace}), \
                patch.object(mp, "run_json", side_effect=fake_cmux), \
                patch.object(mp.opencode_dispatch, "native_hook_store_path", return_value=hook_path), \
                patch.object(mp.opencode_dispatch, "check_opencode_cli", return_value="/fixture/bin/opencode"), \
                patch.object(mp.opencode_dispatch, "create_session", side_effect=fake_create), \
                patch.object(mp.opencode_dispatch, "startup_turn_observation", side_effect=startup_observation), \
                patch.object(mp.opencode_dispatch, "inspect_session", side_effect=child_identity), \
                patch.object(mp.opencode_dispatch, "set_session_environment", wraps=mp.opencode_dispatch.set_session_environment):
            result = mp.dispatch_opencode(app, args, config, child_choice, child, task_file.read_bytes())
        launch = app.state(f"launches/{result['launch']}", None)
        self.assertEqual(result["session"], child_session)
        self.assertEqual(create_requests[0]["directory"], str(child))
        self.assertEqual(create_requests[0]["parent_session_id"], SESSION)
        self.assertEqual(launch["opencode_parent_session_id"], SESSION)
        self.assertEqual(launch["pattern_buffer_task_id"], child_ticket)
        self.assertEqual(launch["pattern_buffer_owner_worktree"], str(owner))
        self.assertEqual(launch["worktree_directory"], str(child))
        self.assertEqual(pb_actions, ["context", "prepare-dispatch", "context", "bind-dispatch", "context"])
        self.assertNotIn(CMUX_CANARY, json.dumps(launch))

    def test_codex_and_claude_parent_cli_dispatch_uses_legacy_pattern_buffer_contract(self):
        cases = (("codex", "codex-thread-fixture", "CODEX_THREAD_ID", "codex"),
                 ("codex", "codex-worktree-fixture", "CODEX_THREAD_ID", "worktree"),
                 ("claude", "claude-session-fixture", "CLAUDE_CODE_SESSION_ID", "claude"),
                 ("claude", "claude-worktree-fixture", "CLAUDE_CODE_SESSION_ID", "worktree"))
        for parent_harness, parent_session, session_variable, receipt_parent_kind in cases:
            case_name = f"{parent_harness}-{receipt_parent_kind}"
            with self.subTest(parent_harness=parent_harness, receipt_parent_kind=receipt_parent_kind):
                config_dir = self.root / f"{case_name}-cli-config"
                state_dir = self.root / f"{case_name}-cli-state"
                config = mp.example_config(); config["active"] = "Codex-Qwen"
                mp.write(config_dir / "profiles.json", config)
                owner = self.root / f"{case_name}-owner"; owner.mkdir()
                child = self.root / f"{case_name}-child"; child.mkdir()
                for worktree in (owner, child):
                    subprocess.run(["git", "-C", str(worktree), "init", "-q"], check=True)
                task_file = self.root / f"{case_name}-task.txt"
                task_file.write_text("Dispatch this bounded local Qwen implementation task.")
                pb_script = self.root / f"{case_name}-pattern-buffer.py"
                pb_script.write_text("# fake PB CLI boundary\n")
                receipt_file = self.root / f"{case_name}-receipt.json"
                workspace, surface = str(uuid.uuid4()), str(uuid.uuid4())
                child_ticket = "f" * 32
                owner_gitdir = mp.git(owner, "rev-parse", "--absolute-git-dir").decode().strip()
                pb_commands = []
                current = {"truncated": False, "owner_availability": {"agent_sessions": "available"},
                           "items": [{"lifecycle": "running", "placement": {"kind": "local"},
                                      "freshness": {"state": "current"},
                                      "projections": [{"workspace_id": workspace, "panel_id": surface}],
                                      "agents": [{"kind": parent_harness, "session_id": parent_session,
                                                  "state": "working"}]}]}

                def fake_boundary(argv, stdin=None):
                    if argv[0] == sys.executable and argv[1] == str(pb_script):
                        pb_commands.append(list(argv))
                        self.assertIn("prepare-dispatch", argv)
                        self.assertNotIn("--parent-task-id", argv)
                        receipt = {"task_id": child_ticket, "harness": "opencode", "role": "implementer",
                                   "status": "pending", "session_id": None,
                                   "worktree": str(child), "owner_worktree": str(owner),
                                   "owner_gitdir": owner_gitdir,
                                   "brief_sha256": hashlib.sha256(task_file.read_bytes()).hexdigest(),
                                   "ticket_version": 2, "parent_kind": receipt_parent_kind}
                        Path(argv[argv.index("--receipt-file") + 1]).write_text(json.dumps(receipt))
                        return {"status": "pending", "harness": "opencode", "role": "implementer",
                                "task_id": child_ticket, "ticket_version": 2,
                                "parent_kind": receipt_parent_kind}
                    if "identify" in argv:
                        return {"caller": {"surface_type": "terminal", "workspace_id": workspace,
                                            "surface_id": surface}}
                    if "tree" in argv:
                        return {"windows": [{"workspaces": [{"id": workspace, "panes": [
                            {"id": "pane-parent-cli", "surfaces": [{"id": surface}]}]}]}]}
                    if "current" in argv:
                        return current
                    raise AssertionError(f"Unexpected parent-dispatch request: {argv}")

                args = mp.parser().parse_args([
                    "--config-dir", str(config_dir), "--state-dir", str(state_dir),
                    "dispatch", "--role", "implementer", "--cwd", str(child),
                    "--task-file", str(task_file), "--opencode-manifest", str(self.manifest),
                    "--pattern-buffer", str(pb_script), "--owner-worktree", str(owner),
                    "--receipt-file", str(receipt_file),
                ])
                env = {"CMUX_AGENT_LAUNCH_KIND": parent_harness, "OPENCODE_SESSION_ID": "",
                       "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "",
                       session_variable: parent_session}
                with patch.dict(os.environ, env), \
                        patch.object(mp, "run_json", side_effect=fake_boundary), \
                        patch.object(mp.opencode_dispatch, "check_opencode_cli", return_value="/fixture/bin/opencode"), \
                        patch.object(mp.opencode_dispatch, "create_session",
                                     side_effect=mp.opencode_dispatch.OpenCodeDispatchError(
                                         "synthetic_stop_before_api_mutation")) as create:
                    with self.assertRaisesRegex(mp.PreferenceError, "uncertain: synthetic_stop_before_api_mutation"):
                        mp.execute(args)
                self.assertEqual(len(pb_commands), 1)
                self.assertEqual(create.call_args.kwargs["directory"], str(child))
                self.assertIsNone(create.call_args.kwargs["parent_session_id"])
                self.assertFalse(any(method == "POST" and path == "/api/session"
                                     for method, path, _ in self.state.calls))
                launches = list((state_dir / "launches").glob("*.json"))
                self.assertEqual(len(launches), 1)
                saved = json.loads(launches[0].read_text())
                self.assertIsNone(saved["opencode_parent_session_id"])
                self.assertEqual(saved["context_injector"]["parent_kind"], receipt_parent_kind)

    def test_pattern_buffer_child_receipt_binds_parent_session_and_task_separately(self):
        owner = self.root / "pb-owner"; owner.mkdir()
        child = self.root / "pb-child"; child.mkdir()
        for worktree in (owner, child):
            subprocess.run(["git", "-C", str(worktree), "init", "-q"], check=True)
        script = self.root / "pb-contract.py"; script.write_text("# synthetic PB boundary\n")
        task_file = self.root / "pb-child-task.txt"; task_file.write_text("Bounded child task.")
        receipt_path = self.root / "pb-receipt.json"
        workspace, surface = str(uuid.uuid4()), str(uuid.uuid4())
        parent = {"harness": "opencode", "session_id": "ses_parentFixture123", "task_id": "parent-task-ref",
                  "owner_worktree": str(owner), "owner_gitdir": str(owner / ".git"),
                  "workspace_uuid": workspace, "surface_uuid": surface,
                  "assignment_sha256": "d" * 64, "launch_id": "a" * 32}
        args = SimpleNamespace(context_injector=str(script), owner_worktree=str(owner),
                               receipt_file=str(receipt_path), role="implementer",
                               task_file=str(task_file))
        task_digest = hashlib.sha256(task_file.read_bytes()).hexdigest()
        child_ticket = "e" * 32
        expected_parent = {"harness": "opencode", "session_id": parent["session_id"],
                           "task_id": parent["task_id"], "role": "orchestrator",
                           "owner_worktree": parent["owner_worktree"],
                           "owner_gitdir": parent["owner_gitdir"],
                           "surface_id": surface.upper(), "workspace_id": workspace.upper(),
                           "assignment_sha256": parent["assignment_sha256"]}

        class FakeApp:
            def run_json(self, argv, stdin=None):
                self.argv = argv
                receipt = {"task_id": child_ticket, "harness": "opencode", "role": "implementer",
                           "status": "pending", "session_id": None, "worktree": str(child),
                           "owner_worktree": str(owner), "owner_gitdir": parent["owner_gitdir"],
                           "brief_sha256": task_digest,
                           "ticket_version": 2, "parent_kind": "opencode", "parent": expected_parent}
                Path(argv[argv.index("--receipt-file") + 1]).write_text(json.dumps(receipt))
                return {"status": "pending", "harness": "opencode", "role": "implementer",
                        "task_id": child_ticket, "ticket_version": 2,
                        "parent_kind": "opencode", "parent": expected_parent}

        fake = FakeApp()
        adapter = mp.prepare_opencode_pattern_buffer(fake, args, child, task_digest, parent)
        self.assertEqual(adapter["task_id"], child_ticket)
        self.assertNotEqual(adapter["task_id"], parent["task_id"])
        self.assertIn("--parent-task-id", fake.argv)
        self.assertEqual(fake.argv[fake.argv.index("--parent-task-id") + 1], parent["task_id"])
        self.assertEqual(adapter["parent"]["session_id"], parent["session_id"])
        self.assertEqual(adapter["parent"]["task_id"], parent["task_id"])

        downgraded_receipt_path = self.root / "pb-downgraded-receipt.json"
        downgraded_args = SimpleNamespace(context_injector=str(script), owner_worktree=str(owner),
                                          receipt_file=str(downgraded_receipt_path), role="implementer",
                                          task_file=str(task_file))

        class DowngradedFakeApp:
            def run_json(self, argv, stdin=None):
                self.argv = argv
                receipt = {"task_id": child_ticket, "harness": "opencode", "role": "implementer",
                           "status": "pending", "session_id": None, "worktree": str(child),
                           "owner_worktree": str(owner), "owner_gitdir": parent["owner_gitdir"],
                           "brief_sha256": task_digest, "ticket_version": 2, "parent_kind": "worktree"}
                Path(argv[argv.index("--receipt-file") + 1]).write_text(json.dumps(receipt))
                return {"status": "pending", "harness": "opencode", "role": "implementer",
                        "task_id": child_ticket, "ticket_version": 2, "parent_kind": "worktree"}

        downgraded = DowngradedFakeApp()
        with self.assertRaisesRegex(mp.PreferenceError, "did not return the prepared OpenCode ticket"):
            mp.prepare_opencode_pattern_buffer(downgraded, downgraded_args, child, task_digest, parent)
        self.assertIn("--parent-task-id", downgraded.argv)
        self.assertEqual(downgraded.argv[downgraded.argv.index("--parent-task-id") + 1], parent["task_id"])

    def test_tui_child_targets_only_the_verified_session_and_keeps_password_out_of_argv(self):
        connection = dispatch.load_connection(self.manifest)
        launch_id, task_digest = "d" * 32, "e" * 64
        dispatch.create_session(connection, directory=str(self.root), choice=self.choice,
                                launch_id=launch_id, task_digest=task_digest,
                                parent_task_id=connection.parent_task_id)
        workspace, surface = str(uuid.uuid4()), str(uuid.uuid4())
        prompt = "One synthetic startup task."
        prompt_file = self.root / "prompt"
        dispatch.write_private_snapshot(prompt_file, prompt.encode())
        release_file = self.root / "release.json"
        dispatch.write_private_json(release_file, {"launch_id": launch_id, "session_id": SESSION,
                                                   "workspace_uuid": workspace, "surface_uuid": surface})
        handshake_file = self.root / "handshake.json"
        observed = {}

        def fake_run(argv, **kwargs):
            if argv[0] == "cmux":
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"caller": {
                    "workspace_id": workspace, "surface_id": surface}}), stderr="")
            if argv == ["/fixture/bin/opencode", "--version"]:
                return subprocess.CompletedProcess(argv, 0, stdout="2.2.5\n", stderr="")
            if argv[0] == "/fixture/bin/opencode":
                observed["argv"] = argv
                observed["env"] = kwargs["env"]
                observed["cwd"] = kwargs["cwd"]
                return subprocess.CompletedProcess(argv, 0)
            raise AssertionError(f"Unexpected child command: {argv}")

        xdg_paths = self.tui_descriptor()["xdg_environment"]
        with patch.dict(os.environ, {"CMUX_SOCKET_CAPABILITY": CMUX_CANARY}), \
                patch.object(dispatch.subprocess, "run", side_effect=fake_run):
            result = dispatch.tui_child(str(self.manifest), SESSION, str(self.root), str(prompt_file),
                                        PROVIDER, MODEL, launch_id, self.choice["selection"], task_digest,
                                        str(handshake_file), str(release_file), str(self.root / "abort.json"),
                                        "/fixture/bin/opencode", None, xdg_paths)
        self.assertEqual(result, 0)
        self.assertEqual(observed["cwd"], str(self.root))
        argv = observed["argv"]
        self.assertEqual(argv[argv.index("--session") + 1], SESSION)
        self.assertEqual(argv[argv.index("--prompt") + 1], prompt)
        self.assertNotIn(CANARY, argv)
        self.assertEqual(observed["env"]["OPENCODE_SERVER_PASSWORD"], CANARY)
        self.assertEqual(observed["env"]["CMUX_SOCKET_CAPABILITY"], CMUX_CANARY)
        self.assertNotIn("OPENCODE_PASSWORD", observed["env"])
        for key, value in xdg_paths.items():
            self.assertEqual(observed["env"][key], value)
        handshake = dispatch.read_private_json(handshake_file)
        self.assertEqual(handshake["surface_uuid"], surface)
        self.assertEqual(handshake["cmux_socket_capability"], CMUX_CANARY)

    def test_ambiguous_session_create_timeout_is_uncertain_and_blocks_retry(self):
        config_dir, state_dir = self.root / "timeout-config", self.root / "timeout-state"
        config = mp.example_config()
        config["active"] = "Qwen-only"
        mp.write(config_dir / "profiles.json", config)
        app = mp.App(config_dir, state_dir)
        repo = self.root / "timeout-worktree"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        brief = b"A synthetic task whose API create response is delayed."
        task_file = self.root / "timeout-brief.txt"
        task_file.write_bytes(brief)
        choice = mp.resolve(mp.selection(config), "implementer", None)
        workspace, caller_surface, pane = str(uuid.uuid4()), str(uuid.uuid4()), "pane-fixture"

        def cmux(argv, stdin=None):
            if "identify" in argv:
                return {"caller": {"workspace_id": workspace, "surface_id": caller_surface}}
            if "tree" in argv:
                return {"windows": [{"workspaces": [{"id": workspace, "panes": [
                    {"id": pane, "surfaces": [{"id": caller_surface}]}]}]}]}
            raise AssertionError(f"Unexpected CMUX request: {argv}")

        args = SimpleNamespace(role="implementer", opencode_manifest=str(self.manifest), context_injector=None,
                               owner_worktree=None, receipt_file=None, task_file=str(task_file), slot=None)
        self.state.delay_create = True
        with patch.object(mp, "run_json", side_effect=cmux), \
                patch.object(mp.opencode_dispatch, "check_opencode_cli", return_value="/fixture/bin/opencode"), \
                self.assertRaisesRegex(mp.PreferenceError, "uncertain: http_timeout"):
            mp.dispatch_opencode(app, args, config, choice, repo, brief)
        launches = list((state_dir / "launches").glob("*.json"))
        self.assertEqual(len(launches), 1)
        saved = json.loads(launches[0].read_text())
        self.assertEqual(saved["status"], "uncertain")
        self.assertEqual(saved["error_code"], "http_timeout")
        self.assertEqual(sum(method == "POST" and path == "/api/session"
                             for method, path, _ in self.state.calls), 1)

        self.state.delay_create = False
        with patch.object(mp, "run_json", side_effect=cmux), \
                patch.object(mp.opencode_dispatch, "check_opencode_cli", return_value="/fixture/bin/opencode"), \
                self.assertRaisesRegex(mp.PreferenceError, "unresolved or live"):
            mp.dispatch_opencode(app, args, config, choice, repo, brief)
        self.assertEqual(sum(method == "POST" and path == "/api/session"
                             for method, path, _ in self.state.calls), 1)

    def test_preflight_fails_closed_for_wrong_server_provider_model_effort_and_variant(self):
        cases = (("version", "9.0.0", "unsupported_opencode_version"),
                 ("pid", 17, "opencode_server_pid_mismatch"),
                 ("provider_url", "https://example.invalid/v1", "invalid_loopback_url"),
                 ("model_enabled", False, "exact_model_unavailable"),
                 ("model_tools", False, "model_tool_capability_unverified"),
                 ("model_output", 4096, "model_output_limit_below_maximum_effort"),
                 ("model_package", "unexpected-package", "unsupported_provider_package"),
                 ("model_body", {"reasoning_effort": "low"}, "max_reasoning_request_body_unverified"),
                 ("variant_settings", {"reasoningEffort": "high"}, "max_reasoning_variant_unverified"))
        for field, value, code in cases:
            with self.subTest(field=field):
                setattr(self.state, field, value)
                with self.assertRaises(dispatch.OpenCodeDispatchError) as raised:
                    dispatch.preflight(self.manifest, self.choice)
                self.assertEqual(raised.exception.code, code)
                setattr(self.state, field, getattr(FakeAPIState(self.root), field))

    def test_authentication_manifest_permissions_and_nonloopback_are_rejected(self):
        self.state.challenge = False
        with self.assertRaisesRegex(dispatch.OpenCodeDispatchError, "authenticated_api_challenge_required"):
            dispatch.preflight(self.manifest, self.choice)
        self.state.challenge = True
        self.password_file.chmod(0o644)
        with self.assertRaisesRegex(dispatch.OpenCodeDispatchError, "private_password_file_required"):
            dispatch.load_connection(self.manifest)
        self.password_file.chmod(0o600)
        link = self.root / "password-link"
        link.symlink_to(self.password_file)
        manifest = json.loads(self.manifest.read_text()); manifest["password_file"] = str(link)
        self.manifest.write_text(json.dumps(manifest)); self.manifest.chmod(0o600)
        with self.assertRaisesRegex(dispatch.OpenCodeDispatchError, "private_password_file_required"):
            dispatch.load_connection(self.manifest)
        manifest["password_file"] = str(self.password_file)
        manifest["parent_task_id"] = "x" * 257
        self.manifest.write_text(json.dumps(manifest)); self.manifest.chmod(0o600)
        with self.assertRaisesRegex(dispatch.OpenCodeDispatchError, "invalid_parent_task_id"):
            dispatch.load_connection(self.manifest)
        with self.assertRaisesRegex(dispatch.OpenCodeDispatchError, "loopback_ip_literal_required"):
            dispatch._loopback_origin("http://192.0.2.10:4096")
        with self.assertRaisesRegex(dispatch.OpenCodeDispatchError, "loopback_ip_literal_required"):
            dispatch._loopback_origin("http://localhost:4096")

    def test_wrong_created_directory_or_model_is_not_accepted(self):
        connection = dispatch.load_connection(self.manifest)
        self.state.session_directory = str(self.root / "wrong")
        (self.root / "wrong").mkdir()
        with self.assertRaisesRegex(dispatch.OpenCodeDispatchError, "created_session_directory_mismatch"):
            dispatch.verify_session(connection, SESSION, expected_directory=str(self.root), choice=self.choice,
                                    launch_id="b" * 32, task_digest="c" * 64)
        self.state.session_directory = str(self.root)
        self.state.session_model = {"providerID": PROVIDER, "id": "other:model", "variant": "max"}
        with self.assertRaisesRegex(dispatch.OpenCodeDispatchError, "created_session_model_mismatch"):
            dispatch.verify_session(connection, SESSION, expected_directory=str(self.root), choice=self.choice,
                                    launch_id="b" * 32, task_digest="c" * 64)

    def test_busy_session_pending_permission_and_pending_form_all_hold_without_approval(self):
        connection = dispatch.load_connection(self.manifest)
        launch_id, task_digest = "f" * 32, "1" * 64
        dispatch.create_session(connection, directory=str(self.root), choice=self.choice,
                                launch_id=launch_id, task_digest=task_digest,
                                parent_task_id=connection.parent_task_id)
        workspace, surface = str(uuid.uuid4()), str(uuid.uuid4())
        cases = (("active_sessions", {SESSION: {"type": "running"}}, "session_guard_session_busy"),
                 ("pending_permissions", [{"id": "permission-fixture"}], "session_guard_pending_permissions"),
                 ("pending_forms", [{"id": "form-fixture"}], "session_guard_pending_forms"))
        for attribute, value, expected in cases:
            with self.subTest(expected=expected):
                setattr(self.state, attribute, value)
                with self.assertRaises(dispatch.OpenCodeDispatchError) as raised:
                    dispatch.inspect_session(connection, session_id=SESSION,
                                             session_directory=str(self.root), worktree_directory=str(self.root),
                                             workspace_uuid=workspace, surface_uuid=surface,
                                             parent_task_id=connection.parent_task_id)
                self.assertEqual(raised.exception.code, expected)
                setattr(self.state, attribute, {} if attribute == "active_sessions" else [])
        self.assertFalse(any(method == "POST" and path != "/api/session" for method, path, _ in self.state.calls))

    def test_native_binding_requires_one_exact_surface_known_state_and_fresh_projection(self):
        workspace, surface = str(uuid.uuid4()), str(uuid.uuid4())
        item = {"lifecycle": "running", "placement": {"kind": "local"},
                "freshness": {"state": "current"},
                "projections": [{"workspace_id": workspace, "panel_id": surface}],
                "agents": [{"kind": "opencode", "session_id": SESSION, "state": "idle"}]}
        current = {"truncated": False, "owner_availability": {"agent_sessions": "available"},
                   "items": [item]}
        binding = dispatch.require_native_dispatch_binding(expected_workspace=workspace,
                                                           expected_surface=surface,
                                                           expected_session=SESSION, current=current)
        self.assertEqual(binding["surface_uuid"], surface)
        uppercase = {"truncated": False, "owner_availability": {"agent_sessions": "available"},
                     "items": [{**item, "projections": [
            {"workspace_id": workspace.upper(), "panel_id": surface.upper()}]}]}
        uppercase_binding = dispatch.require_native_dispatch_binding(expected_workspace=workspace,
                                                                     expected_surface=surface,
                                                                     expected_session=SESSION,
                                                                     current=uppercase)
        self.assertEqual(uppercase_binding["workspace_uuid"], workspace)
        self.assertEqual(uppercase_binding["surface_uuid"], surface)
        for altered, code in (({"truncated": True, "owner_availability": {"agent_sessions": "available"},
                                "items": [item]}, "native_binding_observation_unknown"),
                              ({"truncated": False, "owner_availability": {"agent_sessions": "available"},
                                "items": [{**item, "projections": [
                                  {"workspace_id": "not-a-uuid", "panel_id": surface}]}]},
                               "native_binding_observation_unknown"),
                              ({"truncated": False, "owner_availability": {"agent_sessions": "available"},
                                "items": [{**item, "projections": [
                                  {"workspace_id": workspace, "panel_id": str(uuid.uuid4())}]}]},
                               "native_session_visible_on_other_surface"),
                              ({"truncated": False, "owner_availability": {"agent_sessions": "available"},
                                "items": [{**item, "freshness": {"state": "stale"}}]},
                               "native_session_observation_stale"),
                              ({"truncated": False, "owner_availability": {"agent_sessions": "available"},
                                "items": [{**item, "agents": [
                                  {"kind": "opencode", "session_id": SESSION, "state": "unknown"}]}]},
                               "native_session_state_unknown")):
            with self.subTest(code=code), self.assertRaises(dispatch.OpenCodeDispatchError) as raised:
                dispatch.require_native_dispatch_binding(expected_workspace=workspace,
                                                          expected_surface=surface,
                                                          expected_session=SESSION, current=altered)
            self.assertEqual(raised.exception.code, code)

    def test_timeout_and_large_response_are_bounded_without_secret_diagnostics(self):
        self.state.delay_info = True
        with self.assertRaises(dispatch.OpenCodeDispatchError) as raised:
            dispatch.preflight(self.manifest, self.choice)
        self.assertEqual(raised.exception.code, "http_timeout")
        self.state.delay_info = False
        self.state.large_info = True
        with self.assertRaises(dispatch.OpenCodeDispatchError) as raised:
            dispatch.preflight(self.manifest, self.choice)
        self.assertEqual(raised.exception.code, "response_too_large")
        self.assertNotIn(CANARY, str(raised.exception))

    def test_initial_dispatch_waits_for_delayed_prompt_admission_before_accepting_idle_state(self):
        config_dir, state_dir = self.root / "config", self.root / "state"
        config = mp.example_config()
        config["active"] = "Qwen-only"
        mp.validate(config)
        mp.write(config_dir / "profiles.json", config)
        app = mp.App(config_dir, state_dir)
        repo = self.root / "worktree"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        brief = b"Complete one synthetic OpenCode task.\n"
        task_file = self.root / "brief.txt"
        task_file.write_bytes(brief)
        choice = mp.resolve(mp.selection(config), "implementer", None)
        workspace, caller_surface, pane = str(uuid.uuid4()), str(uuid.uuid4()), "pane-fixture"
        opened_surface = str(uuid.uuid4())
        current_surface = opened_surface
        hook_path = self.root / "hook-sessions.json"
        self.state.delay_startup_messages = True

        def cmux(argv, stdin=None):
            nonlocal current_surface
            if "identify" in argv:
                return {"caller": {"workspace_id": workspace, "surface_id": caller_surface}}
            if "tree" in argv:
                return {"windows": [{"workspaces": [{"id": workspace, "panes": [
                    {"id": pane, "surfaces": [{"id": caller_surface}]}]}]}]}
            if "new-surface" in argv:
                child_command = shlex.split(argv[argv.index("--command") + 1])
                self.assertNotIn(CANARY, argv[-1])
                def get(flag): return child_value(child_command, flag)
                prompt_text = Path(get("prompt-file")).read_text()
                self.state.pending_startup_prompt = prompt_text
                if not self.state.delay_startup_messages:
                    self.state.messages = self.state.startup_messages(prompt_text)
                dispatch.write_private_json(get("handshake-file"), {
                    "launch_id": get("launch-id"), "session_id": get("session"),
                    "workspace_uuid": workspace, "surface_uuid": opened_surface,
                    "cmux_socket_path": None, "cmux_socket_capability": CMUX_CANARY})
                dispatch.write_private_json(hook_path, {"sessions": {SESSION: {
                    "workspaceId": workspace, "surfaceId": opened_surface, "startedAt": 123.0,
                    "agentLifecycle": "idle", "cwd": str(repo)}}})
                return {"workspace_id": workspace, "surface_id": opened_surface}
            if "current" in argv:
                return {"truncated": False, "owner_availability": {"agent_sessions": "available"},
                    "items": [{"lifecycle": "idle", "placement": {"kind": "local"},
                    "freshness": {"state": "current"},
                    "projections": [{"workspace_id": workspace, "panel_id": current_surface}],
                    "agents": [{"kind": "opencode", "session_id": SESSION, "state": "idle"}]}]}
            if "rpc" in argv:
                method = argv[argv.index("rpc") + 1]
                params = json.loads(argv[-1])
                if method == "surface.input_state":
                    return {"surface_id": params["surface_id"], "workspace_id": workspace,
                            "agent": True, "terminal": True, "state": "empty", "blocks_typing": False,
                            "waiting_on_human": False, "lifecycle": "idle"}
                if method == "agent.message.settings":
                    return {"messages_enabled": True, "receiving": True, "workspace_receiving": True}
            raise AssertionError(f"Unexpected CMUX request: {argv}")

        args = SimpleNamespace(role="implementer", opencode_manifest=str(self.manifest), context_injector=None,
                               owner_worktree=None, receipt_file=None, task_file=str(task_file), slot=None)
        with patch.object(mp, "run_json", side_effect=cmux), \
                patch.object(mp.opencode_dispatch, "native_hook_store_path", return_value=hook_path), \
                patch.object(mp.opencode_dispatch, "check_opencode_cli", return_value="/fixture/bin/opencode"):
            result = mp.dispatch_opencode(app, args, config, choice, repo, brief)
        launch = app.state(f"launches/{result['launch']}", None)
        self.assertEqual(result["session"], SESSION)
        self.assertEqual(result["cmux_surface_uuid"], opened_surface)
        self.assertEqual(result["execution_result"], "turn_idle")
        self.assertEqual(result["delivery_readiness"], "ready")
        self.assertEqual(launch["native_started_at"], 123.0)
        self.assertEqual(self.state.message_reads, 3)
        self.assertEqual(launch["native_session"], SESSION)
        self.assertEqual(launch["parent_task_id"], "task-parent-fixture")
        self.assertEqual(launch["expected_server_pid"], 4242)
        self.assertEqual(launch["cmux_workspace_uuid"], workspace)
        self.assertEqual(launch["cmux_surface_uuid"], opened_surface)
        self.assertEqual(self.state.environment["variables"]["CMUX_AGENT_LAUNCH_KIND"], "opencode")
        self.assertEqual(self.state.environment["variables"]["OPENCODE_SESSION_ID"], SESSION)
        self.assertEqual(self.state.environment["variables"]["CMUX_SOCKET_CAPABILITY"], CMUX_CANARY)
        self.assertFalse(any(path.name.endswith(".handshake.json") for path in
                             (state_dir / "opencode-launches").glob("*")))
        self.assertNotIn(CANARY, json.dumps(launch))
        self.assertNotIn(CMUX_CANARY, json.dumps(launch))
        self.assertNotIn(CANARY, json.dumps(self.state.environment))
        self.assertNotIn(CMUX_CANARY, json.dumps(launch))
        self.assertNotIn("/prompt", [path for _, path, _ in self.state.calls])

    def test_initial_dispatch_wrong_surface_is_uncertain_and_never_falls_back(self):
        config_dir, state_dir = self.root / "wrong-config", self.root / "wrong-state"
        config = mp.example_config(); config["active"] = "Codex-Qwen"; mp.write(config_dir / "profiles.json", config)
        app = mp.App(config_dir, state_dir)
        repo = self.root / "wrong-worktree"; repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        brief = b"One synthetic task."
        task_file = self.root / "wrong-brief.txt"; task_file.write_bytes(brief)
        choice = mp.resolve(mp.selection(config), "implementer", None)
        workspace, caller_surface, pane = str(uuid.uuid4()), str(uuid.uuid4()), "pane-fixture"
        opened_surface, wrong_surface = str(uuid.uuid4()), str(uuid.uuid4())
        args = SimpleNamespace(role="implementer", opencode_manifest=str(self.manifest), context_injector=None,
                               owner_worktree=None, receipt_file=None, task_file=str(task_file), slot=None)

        def cmux(argv, stdin=None):
            if "identify" in argv:
                return {"caller": {"workspace_id": workspace, "surface_id": caller_surface}}
            if "tree" in argv:
                return {"windows": [{"workspaces": [{"id": workspace, "panes": [
                    {"id": pane, "surfaces": [{"id": caller_surface}]}]}]}]}
            if "new-surface" in argv:
                child = shlex.split(argv[argv.index("--command") + 1])
                def get(flag): return child_value(child, flag)
                dispatch.write_private_json(get("handshake-file"), {
                    "launch_id": get("launch-id"), "session_id": get("session"),
                    "workspace_uuid": workspace, "surface_uuid": opened_surface, "cmux_socket_path": None,
                    "cmux_socket_capability": CMUX_CANARY})
                return {"workspace_id": workspace, "surface_id": opened_surface}
            if "current" in argv:
                return {"truncated": False, "owner_availability": {"agent_sessions": "available"},
                    "items": [{"lifecycle": "idle", "placement": {"kind": "local"},
                    "freshness": {"state": "current"},
                    "projections": [{"workspace_id": workspace, "panel_id": wrong_surface}],
                    "agents": [{"kind": "opencode", "session_id": SESSION, "state": "unknown"}]}]}
            raise AssertionError(f"Unexpected CMUX request: {argv}")

        with patch.object(mp, "run_json", side_effect=cmux), \
                patch.object(mp.opencode_dispatch, "check_opencode_cli", return_value="/fixture/bin/opencode"), \
                self.assertRaisesRegex(mp.PreferenceError, "uncertain: native_session_visible_on_other_surface"):
            mp.dispatch_opencode(app, args, config, choice, repo, brief)
        launches = list((state_dir / "launches").glob("*.json"))
        self.assertEqual(len(launches), 1)
        saved = json.loads(launches[0].read_text())
        self.assertEqual(saved["status"], "uncertain")
        self.assertEqual(saved["native_session"], SESSION)
        self.assertEqual(saved["cmux_surface_uuid"], opened_surface)
        self.assertEqual(saved["error_code"], "native_session_visible_on_other_surface")


if __name__ == "__main__":
    unittest.main()
