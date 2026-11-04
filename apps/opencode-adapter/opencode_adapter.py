"""Read-only, fail-closed identity and state inspection for OpenCode v2."""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
from pathlib import Path
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass


SUPPORTED_OPENCODE_VERSION = "2.2.5"
API_CONTRACT = "opencode-v2.2.5-session-inspection-2"
DEFAULT_DELIVERY_POLICY = "native_empty_composer"
API_SIDECHANNEL_DELIVERY_POLICY = "api_sidechannel"
HTTP_TIMEOUT_SECONDS = 2.0
MAX_HTTP_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 128 * 1024
_SESSION_ID = re.compile(r"^ses_[A-Za-z0-9]+$")
_BASIC_USER = "opencode"


class AdapterError(ValueError):
    """A safe, non-sensitive adapter error identified by a short code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def validate_delivery_policy(value: object) -> str:
    """Validate the explicit, bounded OpenCode delivery policy vocabulary."""
    if type(value) is not str or value not in (DEFAULT_DELIVERY_POLICY, API_SIDECHANNEL_DELIVERY_POLICY):
        raise AdapterError("invalid_delivery_policy")
    return value


@dataclass(frozen=True)
class SessionBinding:
    """Explicitly supplied identities that the adapter keeps distinct."""

    session_id: str
    expected_server_pid: int
    session_directory: str
    worktree_directory: str
    cmux_workspace_uuid: str
    cmux_surface_uuid: str
    parent_task_id: str


@dataclass(frozen=True)
class BoundIdentity:
    """Remote and caller-supplied identity fields, without credentials."""

    api_contract: str
    api_origin: str
    api_server_version: str
    api_server_pid: int
    opencode_session_id: str
    opencode_parent_session_id: str | None
    opencode_location_directory: str
    opencode_subpath: str | None
    session_directory: str
    worktree_directory: str
    cmux_workspace_uuid: str
    cmux_surface_uuid: str
    parent_task_id: str


@dataclass(frozen=True)
class Inspection:
    """One bounded API observation with no send or approval capability."""

    disposition: str
    hold_codes: tuple[str, ...]
    api_session_state: str
    identity: BoundIdentity | None
    limitations: tuple[str, ...] = (
        "native_draft_state_not_exposed_by_inspected_api",
        "cmux_surface_to_server_process_link_is_caller_supplied",
        "parent_task_ownership_is_caller_supplied",
        "server_pid_is_not_a_stable_startup_identifier",
    )


@dataclass(frozen=True)
class _HttpResponse:
    status: int
    headers: object
    body: bytes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _normalized_directory(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise AdapterError("invalid_binding_directory")
    try:
        path = Path(value)
        if not path.is_absolute():
            raise AdapterError("invalid_binding_directory")
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise AdapterError("binding_directory_unavailable") from None
    if not resolved.is_dir():
        raise AdapterError("binding_directory_unavailable")
    return str(resolved)


def _normalized_uuid(value: object, code: str) -> str:
    if not isinstance(value, str):
        raise AdapterError(code)
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise AdapterError(code) from None


def _validated_binding(binding: SessionBinding) -> SessionBinding:
    if not isinstance(binding, SessionBinding):
        raise AdapterError("invalid_binding")
    if not isinstance(binding.session_id, str) or not _SESSION_ID.fullmatch(binding.session_id):
        raise AdapterError("invalid_session_id")
    if (
        isinstance(binding.expected_server_pid, bool)
        or not isinstance(binding.expected_server_pid, int)
        or binding.expected_server_pid <= 0
    ):
        raise AdapterError("invalid_expected_server_pid")
    session_directory = _normalized_directory(binding.session_directory)
    worktree_directory = _normalized_directory(binding.worktree_directory)
    if not isinstance(binding.parent_task_id, str) or not binding.parent_task_id.strip():
        raise AdapterError("invalid_parent_task_id")
    if len(binding.parent_task_id) > 256 or any(ord(char) < 32 for char in binding.parent_task_id):
        raise AdapterError("invalid_parent_task_id")
    return SessionBinding(
        session_id=binding.session_id,
        expected_server_pid=binding.expected_server_pid,
        session_directory=session_directory,
        worktree_directory=worktree_directory,
        cmux_workspace_uuid=_normalized_uuid(binding.cmux_workspace_uuid, "invalid_cmux_workspace_uuid"),
        cmux_surface_uuid=_normalized_uuid(binding.cmux_surface_uuid, "invalid_cmux_surface_uuid"),
        parent_task_id=binding.parent_task_id,
    )


class OpenCodeSessionAdapter:
    """Inspect one OpenCode v2 session through an authenticated loopback API."""

    def __init__(
        self,
        api_url: str,
        password: str,
        *,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ):
        self._origin = self._validate_origin(api_url)
        if not isinstance(password, str) or not password:
            raise AdapterError("basic_auth_password_required")
        if any(ord(char) < 32 for char in password):
            raise AdapterError("invalid_basic_auth_password")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout < 0.05
            or timeout > MAX_HTTP_TIMEOUT_SECONDS
        ):
            raise AdapterError("invalid_timeout")
        token = base64.b64encode(f"{_BASIC_USER}:{password}".encode("utf-8")).decode("ascii")
        self._authorization = "Basic " + token
        self._timeout = float(timeout)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )

    def __repr__(self) -> str:
        return f"OpenCodeSessionAdapter(origin={self._origin!r}, authenticated=True)"

    @staticmethod
    def _validate_origin(api_url: object) -> str:
        if not isinstance(api_url, str):
            raise AdapterError("invalid_api_origin")
        try:
            parts = urllib.parse.urlsplit(api_url)
            port = parts.port
        except ValueError:
            raise AdapterError("invalid_api_origin") from None
        if (
            parts.scheme != "http"
            or not parts.hostname
            or "%" in parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or port is None
        ):
            raise AdapterError("invalid_api_origin")
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            raise AdapterError("loopback_ip_literal_required") from None
        if not address.is_loopback:
            raise AdapterError("loopback_ip_literal_required")
        host = f"[{address.compressed}]" if address.version == 6 else address.compressed
        return f"http://{host}:{port}"

    def _get(self, path: str, *, authenticated: bool = True) -> _HttpResponse:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = self._authorization
        request = urllib.request.Request(self._origin + path, headers=headers, method="GET")
        try:
            response = self._opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            result = _HttpResponse(error.code, error.headers or {}, b"")
            error.close()
            return result
        except (socket.timeout, TimeoutError):
            raise AdapterError("http_timeout") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (socket.timeout, TimeoutError)):
                raise AdapterError("http_timeout") from None
            raise AdapterError("http_unavailable") from None
        except (OSError, http.client.HTTPException):
            raise AdapterError("http_unavailable") from None
        try:
            with response:
                status = getattr(response, "status", None)
                if status != 200:
                    return _HttpResponse(int(status or 0), response.headers or {}, b"")
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        raise AdapterError("invalid_content_length") from None
                    if declared_length > MAX_RESPONSE_BYTES:
                        raise AdapterError("response_too_large")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise AdapterError("response_too_large")
                return _HttpResponse(200, response.headers or {}, body)
        except AdapterError:
            raise
        except (socket.timeout, TimeoutError):
            raise AdapterError("http_timeout") from None
        except (OSError, http.client.HTTPException):
            raise AdapterError("http_unavailable") from None

    @staticmethod
    def _json_object(response: _HttpResponse, code: str) -> dict:
        if response.status != 200:
            raise AdapterError(code)
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise AdapterError("invalid_json") from None
        if not isinstance(value, dict):
            raise AdapterError("unsupported_api_schema")
        return value

    @staticmethod
    def _api_info(value: dict) -> tuple[str, int]:
        version = value.get("version")
        pid = value.get("pid")
        urls = value.get("urls")
        paths = value.get("paths")
        if (
            not isinstance(version, str)
            or isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid < 0
            or not isinstance(urls, list)
            or any(not isinstance(url, str) for url in urls)
            or not isinstance(paths, dict)
            or not isinstance(paths.get("tmp"), str)
        ):
            raise AdapterError("unsupported_api_schema")
        if version != SUPPORTED_OPENCODE_VERSION:
            raise AdapterError("unsupported_opencode_version")
        return version, pid

    def _authenticated_api_info(self) -> tuple[str, int]:
        challenge = self._get("/api/info", authenticated=False)
        header = challenge.headers.get("WWW-Authenticate", "")
        scheme = header.split(None, 1)[0].lower() if header else ""
        if challenge.status != 401 or scheme != "basic":
            raise AdapterError("basic_auth_not_enforced")
        response = self._get("/api/info")
        info = self._json_object(response, "api_authentication_failed")
        version, pid = self._api_info(info)
        if pid == 0:
            raise AdapterError("api_server_pid_unavailable")
        return version, pid

    @staticmethod
    def _session_data(value: dict, requested_id: str) -> dict:
        session = value.get("data")
        if not isinstance(session, dict):
            raise AdapterError("unsupported_api_schema")
        if session.get("id") != requested_id:
            raise AdapterError("session_id_mismatch")
        location = session.get("location")
        if not isinstance(location, dict) or not isinstance(location.get("directory"), str):
            raise AdapterError("unsupported_api_schema")
        parent_id = session.get("parentID")
        if parent_id is not None and (
            not isinstance(parent_id, str) or not _SESSION_ID.fullmatch(parent_id)
        ):
            raise AdapterError("unsupported_api_schema")
        subpath = session.get("subpath")
        if subpath is not None and not isinstance(subpath, str):
            raise AdapterError("unsupported_api_schema")
        timestamp = session.get("time")
        if not isinstance(timestamp, dict):
            raise AdapterError("unsupported_api_schema")
        return session

    @staticmethod
    def _session_status(value: dict, session_id: str) -> str:
        active = value.get("data")
        if not isinstance(active, dict):
            raise AdapterError("unsupported_api_schema")
        if session_id not in active:
            return "inactive"
        entry = active[session_id]
        if not isinstance(entry, dict):
            raise AdapterError("unsupported_api_schema")
        state = entry.get("type")
        if state != "running":
            raise AdapterError("unknown_session_status")
        return state

    @staticmethod
    def _pending_list(value: dict) -> list:
        pending = value.get("data")
        if not isinstance(pending, list):
            raise AdapterError("unsupported_api_schema")
        return pending

    def inspect(self, binding: SessionBinding) -> Inspection:
        identity = None
        api_state = "unknown"
        try:
            bound = _validated_binding(binding)
            version, pid = self._authenticated_api_info()
            if pid != bound.expected_server_pid:
                raise AdapterError("api_server_pid_mismatch")

            quoted_id = urllib.parse.quote(bound.session_id, safe="")
            session_json = self._json_object(
                self._get(f"/api/session/{quoted_id}"),
                "session_unavailable",
            )
            session = self._session_data(session_json, bound.session_id)
            remote_directory = _normalized_directory(session["location"]["directory"])
            if remote_directory != bound.session_directory:
                raise AdapterError("session_directory_mismatch")
            if remote_directory != bound.worktree_directory:
                raise AdapterError("worktree_directory_mismatch")

            identity = BoundIdentity(
                api_contract=API_CONTRACT,
                api_origin=self._origin,
                api_server_version=version,
                api_server_pid=pid,
                opencode_session_id=session["id"],
                opencode_parent_session_id=session.get("parentID"),
                opencode_location_directory=remote_directory,
                opencode_subpath=session.get("subpath"),
                session_directory=bound.session_directory,
                worktree_directory=bound.worktree_directory,
                cmux_workspace_uuid=bound.cmux_workspace_uuid,
                cmux_surface_uuid=bound.cmux_surface_uuid,
                parent_task_id=bound.parent_task_id,
            )

            archived = session["time"].get("archived")
            if archived is not None:
                raise AdapterError("session_archived")

            active_json = self._json_object(
                self._get("/api/session/active"),
                "session_status_unavailable",
            )
            api_state = self._session_status(active_json, bound.session_id)
            if api_state == "running":
                raise AdapterError("session_busy")

            inbox_json = self._json_object(
                self._get(f"/api/session/{quoted_id}/inbox"),
                "inbox_state_unavailable",
            )
            if self._pending_list(inbox_json):
                raise AdapterError("pending_inbox")

            permission_json = self._json_object(
                self._get(f"/api/session/{quoted_id}/permission"),
                "permission_state_unavailable",
            )
            if self._pending_list(permission_json):
                raise AdapterError("pending_permissions")

            form_json = self._json_object(
                self._get(f"/api/session/{quoted_id}/form"),
                "form_state_unavailable",
            )
            if self._pending_list(form_json):
                raise AdapterError("pending_forms")

            final_version, final_pid = self._authenticated_api_info()
            if final_version != version or final_pid != pid:
                raise AdapterError("api_server_instance_changed")

            return Inspection("clear", (), api_state, identity)
        except AdapterError as error:
            return Inspection("hold", (error.code,), api_state, identity)

