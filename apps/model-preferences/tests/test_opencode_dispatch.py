"""Synthetic OpenCode API and CMUX boundary tests for Model Preferences."""

import base64
import http.server
import importlib.util
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
import uuid

SCRIPT = Path(__file__).resolve().parents[1] / "opencode_dispatch.py"
SPEC = importlib.util.spec_from_file_location("opencode_dispatch", SCRIPT)
dispatch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dispatch
SPEC.loader.exec_module(dispatch)
from test_preferences import mp


CANARY = "synthetic-password-canary-never-output"
SESSION = "ses_synthetic123"
PROVIDER = "trial-local"
MODEL = "qwen3.6:35b"


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
        self.model_enabled = True
        self.model_tools = True
        self.model_output = 32768
        self.model_package = "@opencode/ai/providers/openai-compatible"
        self.model_body = {"reasoning_effort": "max"}
        self.variant_settings = {"reasoningEffort": "max"}
        self.session_directory = str(root)
        self.session_model = {"providerID": PROVIDER, "id": MODEL, "variant": "max"}
        self.session_metadata = {}
        self.environment = None
        self.active_sessions = {}
        self.pending_permissions = []
        self.pending_forms = []
        self.delay_info = False
        self.delay_create = False
        self.large_info = False
        self.wrong_create_id = False

    def session(self, session_id=SESSION):
        return {"id": session_id, "location": {"directory": self.session_directory},
                "model": self.session_model, "metadata": self.session_metadata,
                "time": {"created": 1, "updated": 1, "archived": None}}


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
            return self.respond(200, {"location": {"directory": str(self.state.root)}, "data": {
                "id": PROVIDER, "package": self.state.provider_package,
                "settings": {"baseURL": self.state.provider_url, "apiKey": "synthetic-provider-key"}}})
        if self.path == "/api/model":
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
            self.state.session_metadata = payload.get("metadata", {})
            if self.state.delay_create:
                time.sleep(2.2)
            try:
                return self.respond(200, {"data": self.state.session(session_id)})
            except OSError:
                return
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
                                         cmux_socket_path="/private/tmp/cmux.sock")
        self.assertEqual(self.state.calls[-1][0:2], ("PUT", f"/api/session/{SESSION}/environment"))
        self.assertIn("OPENCODE_SESSION_ID", self.state.environment["variables"])
        self.assertNotIn("/prompt", [path for _, path, _ in self.state.calls])
        self.assertNotIn(CANARY, repr(connection))

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

        with patch.object(dispatch.subprocess, "run", side_effect=fake_run):
            result = dispatch.tui_child(str(self.manifest), SESSION, str(self.root), str(prompt_file),
                                        PROVIDER, MODEL, launch_id, self.choice["selection"], task_digest,
                                        str(handshake_file), str(release_file), str(self.root / "abort.json"),
                                        "/fixture/bin/opencode")
        self.assertEqual(result, 0)
        self.assertEqual(observed["cwd"], str(self.root))
        argv = observed["argv"]
        self.assertEqual(argv[argv.index("--session") + 1], SESSION)
        self.assertEqual(argv[argv.index("--prompt") + 1], prompt)
        self.assertNotIn(CANARY, argv)
        self.assertEqual(observed["env"]["OPENCODE_SERVER_PASSWORD"], CANARY)
        self.assertNotIn("OPENCODE_PASSWORD", observed["env"])
        self.assertEqual(dispatch.read_private_json(handshake_file)["surface_uuid"], surface)

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
        item = {"lifecycle": "running", "freshness": {"state": "current"},
                "projections": [{"workspace_id": workspace, "panel_id": surface}],
                "agents": [{"kind": "opencode", "session_id": SESSION, "state": "idle"}]}
        current = {"truncated": False, "items": [item]}
        binding = dispatch.require_native_dispatch_binding(expected_workspace=workspace,
                                                           expected_surface=surface,
                                                           expected_session=SESSION, current=current)
        self.assertEqual(binding["surface_uuid"], surface)
        for altered, code in (({"truncated": True, "items": [item]}, "native_binding_observation_unknown"),
                              ({"truncated": False, "items": [{**item, "projections": [
                                  {"workspace_id": workspace, "panel_id": str(uuid.uuid4())}]}]},
                               "native_session_visible_on_other_surface"),
                              ({"truncated": False, "items": [{**item, "freshness": {"state": "stale"}}]},
                               "native_session_observation_stale"),
                              ({"truncated": False, "items": [{**item, "agents": [
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

    def test_initial_dispatch_creates_exact_session_scoped_env_and_accepts_idle_identity_with_composer_hold(self):
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
                def get(flag): return child_command[child_command.index("--" + flag) + 1]
                dispatch.write_private_json(get("handshake-file"), {
                    "launch_id": get("launch-id"), "session_id": get("session"),
                    "workspace_uuid": workspace, "surface_uuid": opened_surface,
                    "cmux_socket_path": None})
                return {"workspace_id": workspace, "surface_id": opened_surface}
            if "current" in argv:
                return {"truncated": False, "items": [{"lifecycle": "idle", "freshness": {"state": "current"},
                    "projections": [{"workspace_id": workspace, "panel_id": current_surface}],
                    "agents": [{"kind": "opencode", "session_id": SESSION, "state": "unknown"}]}]}
            raise AssertionError(f"Unexpected CMUX request: {argv}")

        args = SimpleNamespace(role="implementer", opencode_manifest=str(self.manifest), context_injector=None,
                               owner_worktree=None, receipt_file=None, task_file=str(task_file), slot=None)
        with patch.object(mp, "run_json", side_effect=cmux), \
                patch.object(mp.opencode_dispatch, "check_opencode_cli", return_value="/fixture/bin/opencode"):
            result = mp.dispatch_opencode(app, args, config, choice, repo, brief)
        launch = app.state(f"launches/{result['launch']}", None)
        self.assertEqual(result["session"], SESSION)
        self.assertEqual(result["cmux_surface_uuid"], opened_surface)
        self.assertEqual(result["execution_result"], "turn_idle")
        self.assertEqual(result["delivery_readiness"], "held")
        self.assertEqual(launch["native_session"], SESSION)
        self.assertEqual(launch["parent_task_id"], "task-parent-fixture")
        self.assertEqual(launch["expected_server_pid"], 4242)
        self.assertEqual(launch["cmux_workspace_uuid"], workspace)
        self.assertEqual(launch["cmux_surface_uuid"], opened_surface)
        self.assertEqual(self.state.environment["variables"]["CMUX_AGENT_LAUNCH_KIND"], "opencode")
        self.assertEqual(self.state.environment["variables"]["OPENCODE_SESSION_ID"], SESSION)
        self.assertNotIn(CANARY, json.dumps(launch))
        self.assertNotIn(CANARY, json.dumps(self.state.environment))
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
                def get(flag): return child[child.index("--" + flag) + 1]
                dispatch.write_private_json(get("handshake-file"), {
                    "launch_id": get("launch-id"), "session_id": get("session"),
                    "workspace_uuid": workspace, "surface_uuid": opened_surface, "cmux_socket_path": None})
                return {"workspace_id": workspace, "surface_id": opened_surface}
            if "current" in argv:
                return {"truncated": False, "items": [{"lifecycle": "idle", "freshness": {"state": "current"},
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
