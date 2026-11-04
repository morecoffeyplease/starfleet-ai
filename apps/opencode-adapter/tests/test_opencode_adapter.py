import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import opencode_adapter as oa


SESSION_ID = "ses_fixture0123456789"
PID = 2468
PASSWORD = "synthetic-password"
WORKSPACE_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SURFACE_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class FakeOpenCodeServer:
    def __init__(self, directory):
        self.directory = str(directory)
        self.server_info = {
            "version": oa.SUPPORTED_OPENCODE_VERSION,
            "pid": PID,
            "hostname": "127.0.0.1",
            "urls": ["http://127.0.0.1"],
            "paths": {"tmp": "/tmp/opencode-fixture"},
        }
        self.session = {
            "id": SESSION_ID,
            "projectID": "prj_fixture",
            "time": {"created": "2026-11-04T00:00:00Z", "updated": "2026-11-04T00:00:00Z"},
            "location": {"directory": self.directory},
            "subpath": "",
        }
        self.active = {}
        self.inbox = []
        self.permissions = []
        self.forms = []
        self.overrides = {}
        self.delay_path = None
        self.delay_seconds = 0
        self.auth_required = True
        self.requests = []
        self.expected_auth = "Basic " + base64.b64encode(
            f"opencode:{PASSWORD}".encode("utf-8")
        ).decode("ascii")

        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                return

            def _send(self, status, body=b"", headers=None):
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                authorization = self.headers.get("Authorization")
                owner.requests.append((self.command, self.path, authorization))
                if self.path == owner.delay_path and owner.delay_seconds:
                    time.sleep(owner.delay_seconds)
                if owner.auth_required:
                    if authorization != owner.expected_auth:
                        self._send(401, headers={"WWW-Authenticate": 'Basic realm="Fixture"'})
                        return
                elif authorization not in (None, owner.expected_auth):
                    self._send(401, headers={"WWW-Authenticate": 'Basic realm="Fixture"'})
                    return
                if self.path in owner.overrides:
                    status, body = owner.overrides[self.path]
                    if isinstance(body, dict):
                        body = json.dumps(body).encode("utf-8")
                    self._send(status, body)
                    return
                if self.path == "/api/info":
                    value = owner.server_info
                elif self.path == f"/api/session/{SESSION_ID}":
                    value = {"data": owner.session}
                elif self.path == "/api/session/active":
                    value = {"data": owner.active}
                elif self.path == f"/api/session/{SESSION_ID}/inbox":
                    value = {"data": owner.inbox}
                elif self.path == f"/api/session/{SESSION_ID}/permission":
                    value = {"data": owner.permissions}
                elif self.path == f"/api/session/{SESSION_ID}/form":
                    value = {"data": owner.forms}
                else:
                    self._send(404)
                    return
                self._send(200, json.dumps(value).encode("utf-8"), {"Content-Type": "application/json"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


class OpenCodeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "worktree"
        self.directory.mkdir()
        self.server = FakeOpenCodeServer(self.directory)
        self.client = oa.OpenCodeSessionAdapter(self.server.url, PASSWORD, timeout=0.5)
        self.binding = oa.SessionBinding(
            session_id=SESSION_ID,
            expected_server_pid=PID,
            session_directory=str(self.directory),
            worktree_directory=str(self.directory),
            cmux_workspace_uuid=WORKSPACE_UUID,
            cmux_surface_uuid=SURFACE_UUID,
            parent_task_id="task:parent-9",
        )

    def tearDown(self):
        self.server.close()
        self.temp.cleanup()

    def test_clear_inspection_keeps_all_identity_domains_distinct(self):
        result = self.client.inspect(self.binding)

        self.assertEqual(result.disposition, "clear")
        self.assertEqual(result.hold_codes, ())
        self.assertEqual(result.api_session_state, "inactive")
        identity = result.identity
        self.assertEqual(identity.api_contract, oa.API_CONTRACT)
        self.assertEqual(identity.api_server_pid, PID)
        self.assertEqual(identity.opencode_session_id, SESSION_ID)
        self.assertEqual(identity.session_directory, str(self.directory.resolve()))
        self.assertEqual(identity.worktree_directory, str(self.directory.resolve()))
        self.assertEqual(identity.cmux_workspace_uuid, WORKSPACE_UUID)
        self.assertEqual(identity.cmux_surface_uuid, SURFACE_UUID)
        self.assertEqual(identity.parent_task_id, "task:parent-9")
        self.assertFalse(hasattr(identity, "opencode_workspace_id"))
        self.assertEqual({method for method, _, _ in self.server.requests}, {"GET"})
        self.assertEqual(
            [path for _, path, _ in self.server.requests],
            [
                "/api/info",
                "/api/info",
                f"/api/session/{SESSION_ID}",
                "/api/session/active",
                f"/api/session/{SESSION_ID}/inbox",
                f"/api/session/{SESSION_ID}/permission",
                f"/api/session/{SESSION_ID}/form",
                "/api/info",
                "/api/info",
            ],
        )

    def test_delivery_policy_validator_accepts_only_the_two_explicit_values(self):
        self.assertEqual(oa.validate_delivery_policy("native_empty_composer"), "native_empty_composer")
        self.assertEqual(oa.validate_delivery_policy("api_sidechannel"), "api_sidechannel")
        for value in (None, "", "API_SIDECHANNEL", "future", [], True):
            with self.subTest(value=repr(value)), self.assertRaises(oa.AdapterError) as raised:
                oa.validate_delivery_policy(value)
            self.assertEqual(raised.exception.code, "invalid_delivery_policy")

    def test_wrong_session_id_holds(self):
        self.server.session = {**self.server.session, "id": "ses_another"}

        result = self.client.inspect(self.binding)

        self.assertEqual(result.disposition, "hold")
        self.assertEqual(result.hold_codes, ("session_id_mismatch",))
        self.assertIsNone(result.identity)

    def test_wrong_session_directory_holds(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        self.server.session = {
            **self.server.session,
            "location": {"directory": str(other)},
        }

        result = self.client.inspect(self.binding)

        self.assertEqual(result.disposition, "hold")
        self.assertEqual(result.hold_codes, ("session_directory_mismatch",))

    def test_wrong_worktree_directory_holds(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        binding = oa.SessionBinding(
            **{**self.binding.__dict__, "worktree_directory": str(other)}
        )

        result = self.client.inspect(binding)

        self.assertEqual(result.disposition, "hold")
        self.assertEqual(result.hold_codes, ("worktree_directory_mismatch",))

    def test_wrong_expected_server_pid_holds(self):
        binding = oa.SessionBinding(**{**self.binding.__dict__, "expected_server_pid": PID + 1})

        result = self.client.inspect(binding)

        self.assertEqual(result.disposition, "hold")
        self.assertEqual(result.hold_codes, ("api_server_pid_mismatch",))

    def test_running_sessions_hold_and_unknown_active_schema_holds(self):
        self.server.active = {SESSION_ID: {"type": "running"}}

        result = self.client.inspect(self.binding)

        self.assertEqual(result.disposition, "hold")
        self.assertEqual(result.api_session_state, "running")
        self.assertEqual(result.hold_codes, ("session_busy",))

        self.server.active = {SESSION_ID: {"type": "future-state"}}
        result = self.client.inspect(self.binding)
        self.assertEqual(result.hold_codes, ("unknown_session_status",))

    def test_valid_server_info_without_os_pid_holds(self):
        self.server.server_info = {**self.server.server_info, "pid": 0}

        result = self.client.inspect(self.binding)

        self.assertEqual(result.hold_codes, ("api_server_pid_unavailable",))

    def test_pending_permissions_and_forms_hold(self):
        self.server.permissions = [{"id": "perm_fixture"}]
        result = self.client.inspect(self.binding)
        self.assertEqual(result.hold_codes, ("pending_permissions",))

        self.server.permissions = []
        self.server.forms = [{"id": "form_fixture"}]
        result = self.client.inspect(self.binding)
        self.assertEqual(result.hold_codes, ("pending_forms",))

    def test_pending_inbox_holds_and_malformed_or_unavailable_inbox_fails_closed(self):
        self.server.inbox = [{"id": "inbox_fixture", "type": "user"}]
        result = self.client.inspect(self.binding)
        self.assertEqual(result.hold_codes, ("pending_inbox",))

        inbox_path = f"/api/session/{SESSION_ID}/inbox"
        self.server.inbox = []
        self.server.overrides[inbox_path] = (200, {"data": {"unexpected": "shape"}})
        result = self.client.inspect(self.binding)
        self.assertEqual(result.hold_codes, ("unsupported_api_schema",))

        self.server.overrides[inbox_path] = (503, {})
        result = self.client.inspect(self.binding)
        self.assertEqual(result.hold_codes, ("inbox_state_unavailable",))

        self.server.overrides[inbox_path] = (200, b"not-json")
        result = self.client.inspect(self.binding)
        self.assertEqual(result.hold_codes, ("invalid_json",))

    def test_archived_session_holds(self):
        self.server.session = {
            **self.server.session,
            "time": {**self.server.session["time"], "archived": "2026-11-04T00:00:00Z"},
        }

        result = self.client.inspect(self.binding)

        self.assertEqual(result.hold_codes, ("session_archived",))

    def test_unsupported_version_and_schema_hold(self):
        self.server.server_info = {**self.server.server_info, "version": "2.2.6"}
        result = self.client.inspect(self.binding)
        self.assertEqual(result.hold_codes, ("unsupported_opencode_version",))

        self.server.server_info = {key: value for key, value in self.server.server_info.items() if key != "pid"}
        result = self.client.inspect(self.binding)
        self.assertEqual(result.hold_codes, ("unsupported_api_schema",))

        self.server.server_info = {
            "version": oa.SUPPORTED_OPENCODE_VERSION,
            "pid": PID,
            "hostname": "127.0.0.1",
            "urls": ["http://127.0.0.1"],
            "paths": {"tmp": "/tmp/opencode-fixture"},
        }
        self.server.overrides["/api/session/active"] = (200, {"data": []})
        result = self.client.inspect(self.binding)
        self.assertEqual(result.hold_codes, ("unsupported_api_schema",))

    def test_unauthenticated_server_is_rejected(self):
        self.server.auth_required = False

        result = self.client.inspect(self.binding)

        self.assertEqual(result.hold_codes, ("basic_auth_not_enforced",))
        self.assertNotIn(PASSWORD, repr(self.client))

    def test_wrong_password_is_not_returned(self):
        client = oa.OpenCodeSessionAdapter(self.server.url, "wrong-secret", timeout=0.5)

        result = client.inspect(self.binding)

        self.assertEqual(result.hold_codes, ("api_authentication_failed",))
        self.assertNotIn("wrong-secret", repr(client))
        self.assertNotIn("wrong-secret", repr(result))

    def test_timeout_holds_without_exposing_credentials(self):
        self.server.delay_path = "/api/info"
        self.server.delay_seconds = 0.2
        client = oa.OpenCodeSessionAdapter(self.server.url, PASSWORD, timeout=0.05)

        result = client.inspect(self.binding)

        self.assertEqual(result.hold_codes, ("http_timeout",))
        self.assertNotIn(PASSWORD, repr(result))

    def test_oversized_response_holds(self):
        self.server.overrides["/api/session/active"] = (200, b"x" * (oa.MAX_RESPONSE_BYTES + 1))

        result = self.client.inspect(self.binding)

        self.assertEqual(result.hold_codes, ("response_too_large",))

    def test_non_loopback_and_hostname_origins_are_rejected_without_request(self):
        for url in ("http://192.0.2.10:4096", "http://localhost:4096", "https://127.0.0.1:4096"):
            with self.subTest(url=url):
                with self.assertRaises(oa.AdapterError):
                    oa.OpenCodeSessionAdapter(url, PASSWORD)
        self.assertEqual(self.server.requests, [])

    def test_environment_proxy_is_not_used_for_loopback_requests(self):
        proxy_environment = {
            "http_proxy": "http://127.0.0.1:1",
            "HTTP_PROXY": "http://127.0.0.1:1",
            "no_proxy": "",
            "NO_PROXY": "",
        }
        with patch.dict(os.environ, proxy_environment):
            client = oa.OpenCodeSessionAdapter(self.server.url, PASSWORD, timeout=0.5)
            result = client.inspect(self.binding)

        self.assertEqual(result.disposition, "clear")
        self.assertEqual(self.server.requests[0][1], "/api/info")

    def test_invalid_identity_binding_holds(self):
        binding = oa.SessionBinding(
            session_id="a-session-title",
            expected_server_pid=PID,
            session_directory=str(self.directory),
            worktree_directory=str(self.directory),
            cmux_workspace_uuid=WORKSPACE_UUID,
            cmux_surface_uuid=SURFACE_UUID,
            parent_task_id="task:parent-9",
        )

        result = self.client.inspect(binding)

        self.assertEqual(result.hold_codes, ("invalid_session_id",))
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
