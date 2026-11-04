"""Read-only OpenCode dispatch preflight for Model Preferences."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import http.client
import hashlib
import ipaddress
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


SUPPORTED_OPENCODE_VERSION = "2.2.5"
MAX_REASONING_EFFORT = "max"
MAX_OUTPUT_TOKENS = 32768
HTTP_TIMEOUT_SECONDS = 2.0
MAX_RESPONSE_BYTES = 128 * 1024
MAX_MANIFEST_BYTES = 16 * 1024
MAX_PASSWORD_BYTES = 4096
_BASIC_USER = "opencode"
_MODEL_ID = re.compile(r"^[A-Za-z0-9_.:+/-]{1,200}$")
_MESSAGE_ID = re.compile(r"^msg_[A-Za-z0-9]+$")
_ACTIVE_LIFECYCLES = ("running", "idle", "backgroundWorkPending")


class OpenCodeDispatchError(ValueError):
    """A bounded, non-sensitive OpenCode preflight failure code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class Connection:
    api_origin: str
    expected_server_pid: int
    password: str
    metadata: dict
    parent_task_id: str | None = None

    def __repr__(self) -> str:
        return f"Connection(api_origin={self.api_origin!r}, expected_server_pid={self.expected_server_pid}, authenticated=True)"


@dataclass(frozen=True)
class Preflight:
    api_contract: str
    api_origin: str
    server_version: str
    server_pid: int
    provider_id: str
    model_id: str
    model_variant: str
    output_limit: int
    tools: bool
    local_provider_origin: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _private_bytes(path_value: object, maximum: int, code: str) -> bytes:
    if not isinstance(path_value, str) or not path_value:
        raise OpenCodeDispatchError(code)
    path = Path(path_value).expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise OpenCodeDispatchError(code) from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > maximum
        ):
            raise OpenCodeDispatchError(code)
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            raise OpenCodeDispatchError(code)
        return data
    finally:
        os.close(fd)


def _loopback_origin(value: object, *, allow_path: bool = False) -> str:
    if not isinstance(value, str):
        raise OpenCodeDispatchError("invalid_loopback_url")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise OpenCodeDispatchError("invalid_loopback_url") from None
    valid_paths = ("", "/", "/v1") if allow_path else ("", "/")
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or "%" in parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in valid_paths
        or parsed.query
        or parsed.fragment
        or port is None
    ):
        raise OpenCodeDispatchError("invalid_loopback_url")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        raise OpenCodeDispatchError("loopback_ip_literal_required") from None
    if not address.is_loopback:
        raise OpenCodeDispatchError("loopback_ip_literal_required")
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"http://{host}:{port}"


def load_connection(manifest_path: str | Path) -> Connection:
    raw = _private_bytes(str(manifest_path), MAX_MANIFEST_BYTES, "private_manifest_required")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise OpenCodeDispatchError("invalid_manifest_json") from None
    if not isinstance(manifest, dict):
        raise OpenCodeDispatchError("invalid_manifest_schema")
    allowed = {
        "api_url", "password_file", "expected_server_pid", "session_id", "session_directory",
        "worktree_directory", "cmux_workspace_uuid", "cmux_surface_uuid", "parent_task_id", "metadata",
    }
    if set(manifest) - allowed:
        raise OpenCodeDispatchError("unsupported_manifest_schema")
    api_origin = _loopback_origin(manifest.get("api_url"))
    pid = manifest.get("expected_server_pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise OpenCodeDispatchError("invalid_expected_server_pid")
    metadata = manifest.get("metadata", {})
    if not isinstance(metadata, dict):
        raise OpenCodeDispatchError("invalid_manifest_schema")
    try:
        metadata = json.loads(json.dumps(metadata))
    except (TypeError, ValueError):
        raise OpenCodeDispatchError("invalid_manifest_schema") from None
    secret = _private_bytes(manifest.get("password_file"), MAX_PASSWORD_BYTES, "private_password_file_required")
    try:
        password = secret.decode("utf-8")
    except UnicodeError:
        raise OpenCodeDispatchError("invalid_private_password_file") from None
    password = password.removesuffix("\n").removesuffix("\r")
    if not password or any(ord(char) < 32 for char in password):
        raise OpenCodeDispatchError("invalid_private_password_file")
    parent_task_id = manifest.get("parent_task_id", metadata.get("parent_task_id"))
    if (not isinstance(parent_task_id, str) or not parent_task_id.strip() or len(parent_task_id) > 256
            or any(ord(char) < 32 for char in parent_task_id)):
        raise OpenCodeDispatchError("invalid_parent_task_id")
    return Connection(api_origin, pid, password, metadata, parent_task_id)


class _Client:
    def __init__(self, connection: Connection):
        token = base64.b64encode(f"{_BASIC_USER}:{connection.password}".encode()).decode("ascii")
        self.origin = connection.api_origin
        self.authorization = "Basic " + token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self.last_headers = {}

    def request(self, method: str, path: str, payload: dict | None = None,
                *, authenticated: bool = True) -> tuple[int, bytes]:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = self.authorization
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.origin + path, data=body, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=HTTP_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            status = error.code
            self.last_headers = error.headers or {}
            error.close()
            return status, b""
        except (socket.timeout, TimeoutError):
            raise OpenCodeDispatchError("http_timeout") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (socket.timeout, TimeoutError)):
                raise OpenCodeDispatchError("http_timeout") from None
            raise OpenCodeDispatchError("http_unavailable") from None
        except (OSError, http.client.HTTPException):
            raise OpenCodeDispatchError("http_unavailable") from None
        try:
            with response:
                status = getattr(response, "status", None)
                self.last_headers = response.headers or {}
                if status != 200:
                    return int(status or 0), b""
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        declared_length = int(length)
                    except ValueError:
                        raise OpenCodeDispatchError("invalid_content_length") from None
                    if declared_length > MAX_RESPONSE_BYTES:
                        raise OpenCodeDispatchError("response_too_large")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise OpenCodeDispatchError("response_too_large")
                return 200, body
        except OpenCodeDispatchError:
            raise
        except (socket.timeout, TimeoutError):
            raise OpenCodeDispatchError("http_timeout") from None
        except (OSError, http.client.HTTPException):
            raise OpenCodeDispatchError("http_unavailable") from None

    def get(self, path: str, *, authenticated: bool = True) -> tuple[int, bytes]:
        return self.request("GET", path, authenticated=authenticated)

    def post(self, path: str, payload: dict) -> tuple[int, bytes]:
        return self.request("POST", path, payload)

    def put(self, path: str, payload: dict) -> tuple[int, bytes]:
        return self.request("PUT", path, payload)


def _json(status: int, body: bytes) -> dict:
    if status != 200:
        raise OpenCodeDispatchError("api_request_rejected")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise OpenCodeDispatchError("invalid_api_json") from None
    if not isinstance(value, dict):
        raise OpenCodeDispatchError("unsupported_api_schema")
    return value


def _api_instance(value: dict) -> tuple[str, int]:
    version, pid = value.get("version"), value.get("pid")
    urls, paths = value.get("urls"), value.get("paths")
    if (not isinstance(version, str) or isinstance(pid, bool) or not isinstance(pid, int)
            or not isinstance(urls, list) or any(not isinstance(url, str) for url in urls)
            or not isinstance(paths, dict) or not isinstance(paths.get("tmp"), str)):
        raise OpenCodeDispatchError("unsupported_api_schema")
    return version, pid


def _provider_origin(value: object) -> str:
    return _loopback_origin(value, allow_path=True)


def preflight(manifest_path: str | Path | Connection, choice: dict) -> Preflight:
    """Verify exact API/provider/model configuration with GET requests only."""
    if not isinstance(choice, dict) or choice.get("harness") != "opencode":
        raise OpenCodeDispatchError("opencode_choice_required")
    provider_id = choice.get("provider_id")
    model_id = choice.get("model")
    if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 128 or any(c.isspace() for c in provider_id):
        raise OpenCodeDispatchError("unsupported_local_provider_model")
    if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
        raise OpenCodeDispatchError("unsupported_local_provider_model")
    if choice.get("effort") != MAX_REASONING_EFFORT:
        raise OpenCodeDispatchError("unsupported_reasoning_effort")
    connection = manifest_path if isinstance(manifest_path, Connection) else load_connection(manifest_path)
    for key, expected in (("provider_id", provider_id), ("model_id", model_id)):
        actual = connection.metadata.get(key)
        if actual is not None and actual != expected:
            raise OpenCodeDispatchError("manifest_model_mismatch")
    client = _Client(connection)
    challenge_status, _ = client.get("/api/info", authenticated=False)
    challenge = client.last_headers.get("WWW-Authenticate", "")
    challenge_scheme = challenge.split(None, 1)[0].lower() if challenge else ""
    if challenge_status != 401 or challenge_scheme != "basic":
        raise OpenCodeDispatchError("authenticated_api_challenge_required")
    info = _json(*client.get("/api/info"))
    version, pid = _api_instance(info)
    if version != SUPPORTED_OPENCODE_VERSION:
        raise OpenCodeDispatchError("unsupported_opencode_version")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid != connection.expected_server_pid:
        raise OpenCodeDispatchError("opencode_server_pid_mismatch")
    provider_id_path = urllib.parse.quote(provider_id, safe="")
    provider_response = _json(*client.get(f"/api/provider/{provider_id_path}"))
    provider = provider_response.get("data")
    if not isinstance(provider, dict) or provider.get("id") != provider_id:
        raise OpenCodeDispatchError("unsupported_provider_schema")
    settings = provider.get("settings")
    if not isinstance(settings, dict):
        raise OpenCodeDispatchError("local_provider_base_url_unavailable")
    local_origin = _provider_origin(settings.get("baseURL"))
    models_response = _json(*client.get("/api/model"))
    models = models_response.get("data")
    if not isinstance(models, list):
        raise OpenCodeDispatchError("unsupported_model_schema")
    model = next((item for item in models if isinstance(item, dict) and
                  item.get("providerID") == provider_id and item.get("id") == model_id), None)
    if model is None or model.get("enabled") is not True:
        raise OpenCodeDispatchError("exact_model_unavailable")
    if model.get("package") != "@opencode/ai/providers/openai-compatible":
        raise OpenCodeDispatchError("unsupported_provider_package")
    capabilities = model.get("capabilities")
    if not isinstance(capabilities, dict) or capabilities.get("tools") is not True:
        raise OpenCodeDispatchError("model_tool_capability_unverified")
    limit = model.get("limit")
    output = limit.get("output") if isinstance(limit, dict) else None
    if isinstance(output, bool) or not isinstance(output, int) or output < MAX_OUTPUT_TOKENS:
        raise OpenCodeDispatchError("model_output_limit_below_maximum_effort")
    body = model.get("body")
    if not isinstance(body, dict) or body.get("reasoning_effort") != MAX_REASONING_EFFORT:
        raise OpenCodeDispatchError("max_reasoning_request_body_unverified")
    variants = model.get("variants")
    if not isinstance(variants, list):
        raise OpenCodeDispatchError("unsupported_model_variant_schema")
    max_variant = next((item for item in variants if isinstance(item, dict) and item.get("id") == "max"), None)
    variant_settings = max_variant.get("settings") if isinstance(max_variant, dict) else None
    if not isinstance(variant_settings, dict) or variant_settings.get("reasoningEffort") != MAX_REASONING_EFFORT:
        raise OpenCodeDispatchError("max_reasoning_variant_unverified")
    final_info = _json(*client.get("/api/info"))
    final_version, final_pid = _api_instance(final_info)
    if final_version != version or final_pid != pid:
        raise OpenCodeDispatchError("opencode_server_changed_during_preflight")
    return Preflight(
        api_contract="opencode-v2.2.5-model-preflight-1",
        api_origin=connection.api_origin,
        server_version=version,
        server_pid=pid,
        provider_id=provider_id,
        model_id=model_id,
        model_variant="max",
        output_limit=output,
        tools=True,
        local_provider_origin=local_origin,
    )


def verify_session(connection: Connection, session_id: str, *, expected_directory: str,
                   choice: dict, launch_id: str, task_digest: str,
                   parent_task_id: str | None = None,
                   parent_session_id: str | None = None) -> dict:
    if not isinstance(session_id, str) or not re.fullmatch(r"^ses_[A-Za-z0-9]+$", session_id):
        raise OpenCodeDispatchError("invalid_created_session_id")
    expected_directory = str(Path(expected_directory).resolve(strict=True))
    client = _Client(connection)
    quoted_id = urllib.parse.quote(session_id, safe="")
    value = _json(*client.get(f"/api/session/{quoted_id}"))
    session = value.get("data")
    if not isinstance(session, dict) or session.get("id") != session_id:
        raise OpenCodeDispatchError("created_session_identity_mismatch")
    location = session.get("location")
    if not isinstance(location, dict) or not isinstance(location.get("directory"), str):
        raise OpenCodeDispatchError("created_session_location_unverified")
    try:
        actual_directory = str(Path(location["directory"]).resolve(strict=True))
    except (OSError, RuntimeError):
        raise OpenCodeDispatchError("created_session_location_unavailable") from None
    if actual_directory != expected_directory:
        raise OpenCodeDispatchError("created_session_directory_mismatch")
    model = session.get("model")
    if not isinstance(model, dict) or (model.get("providerID"), model.get("id"), model.get("variant")) != (
        choice.get("provider_id"), choice.get("model"), "max"
    ):
        raise OpenCodeDispatchError("created_session_model_mismatch")
    if session.get("parentID") != parent_session_id:
        raise OpenCodeDispatchError("created_session_parent_session_mismatch")
    metadata = session.get("metadata")
    ticket = metadata.get("starfleet.model-preferences") if isinstance(metadata, dict) else None
    expected_ticket = {"launch_id": launch_id, "selection": choice.get("selection"), "task_sha256": task_digest}
    if parent_task_id is not None:
        expected_ticket["parent_task_id"] = parent_task_id
    if parent_session_id is not None:
        expected_ticket["parent_session_id"] = parent_session_id
    if not isinstance(ticket, dict) or any(ticket.get(key) != expected for key, expected in expected_ticket.items()):
        raise OpenCodeDispatchError("created_session_launch_ticket_mismatch")
    return {"session_id": session_id, "directory": actual_directory,
            "model": {"provider_id": model["providerID"], "id": model["id"], "variant": model["variant"]},
            "parent_session_id": session.get("parentID")}


def require_no_pending_interactions(connection: Connection, session_id: str) -> None:
    """Hold parent delegation when the exact session has human permission/form work."""
    if not isinstance(session_id, str) or not re.fullmatch(r"^ses_[A-Za-z0-9]+$", session_id):
        raise OpenCodeDispatchError("invalid_session_id")
    quoted_id = urllib.parse.quote(session_id, safe="")
    client = _Client(connection)
    def exact_instance() -> tuple[str, int]:
        version, pid = _api_instance(_json(*client.get("/api/info")))
        if version != SUPPORTED_OPENCODE_VERSION:
            raise OpenCodeDispatchError("unsupported_opencode_version")
        if pid != connection.expected_server_pid:
            raise OpenCodeDispatchError("opencode_server_pid_mismatch")
        return version, pid

    instance = exact_instance()
    for suffix, unavailable, pending in (
        ("permission", "permission_state_unavailable", "pending_permissions"),
        ("form", "form_state_unavailable", "pending_forms"),
    ):
        value = _json(*client.get(f"/api/session/{quoted_id}/{suffix}"))
        if not isinstance(value, dict) or not isinstance(value.get("data"), list):
            raise OpenCodeDispatchError(unavailable)
        if value["data"]:
            raise OpenCodeDispatchError(pending)
    if exact_instance() != instance:
        raise OpenCodeDispatchError("opencode_server_instance_changed")


def inspect_session(connection: Connection, *, session_id: str, session_directory: str,
                    worktree_directory: str, workspace_uuid: str, surface_uuid: str,
                    parent_task_id: str) -> dict:
    adjacent = Path(__file__).resolve().with_name("opencode_adapter.py")
    source = adjacent if adjacent.is_file() else Path(__file__).resolve().parents[1] / "opencode-adapter" / "opencode_adapter.py"
    if not source.is_file():
        raise OpenCodeDispatchError("shared_session_adapter_unavailable")
    spec = importlib.util.spec_from_file_location("model_preferences_shared_opencode_adapter", source)
    if spec is None or spec.loader is None:
        raise OpenCodeDispatchError("shared_session_adapter_unavailable")
    adapter_module = importlib.util.module_from_spec(spec)
    try:
        sys.modules[spec.name] = adapter_module
        spec.loader.exec_module(adapter_module)
    except (ImportError, OSError, ValueError):
        raise OpenCodeDispatchError("shared_session_adapter_unavailable") from None
    adapter = adapter_module.OpenCodeSessionAdapter(connection.api_origin, connection.password)
    binding = adapter_module.SessionBinding(
        session_id=session_id,
        expected_server_pid=connection.expected_server_pid,
        session_directory=session_directory,
        worktree_directory=worktree_directory,
        cmux_workspace_uuid=workspace_uuid,
        cmux_surface_uuid=surface_uuid,
        parent_task_id=parent_task_id,
    )
    result = adapter.inspect(binding)
    if result.disposition != "clear" or result.identity is None:
        code = result.hold_codes[0] if result.hold_codes else "session_state_unknown"
        raise OpenCodeDispatchError("session_guard_" + code)
    return {
        "api_contract": result.identity.api_contract,
        "api_origin": result.identity.api_origin,
        "api_server_version": result.identity.api_server_version,
        "api_server_pid": result.identity.api_server_pid,
        "opencode_session_id": result.identity.opencode_session_id,
        "opencode_parent_session_id": result.identity.opencode_parent_session_id,
        "opencode_location_directory": result.identity.opencode_location_directory,
        "opencode_subpath": result.identity.opencode_subpath,
        "session_directory": result.identity.session_directory,
        "worktree_directory": result.identity.worktree_directory,
        "cmux_workspace_uuid": result.identity.cmux_workspace_uuid,
        "cmux_surface_uuid": result.identity.cmux_surface_uuid,
        "parent_task_id": result.identity.parent_task_id,
        "api_session_state": result.api_session_state,
    }


def startup_turn_observation(connection: Connection, *, session_id: str, prompt_sha256: str,
                            provider_id: str, model_id: str) -> dict:
    """Prove the exact startup prompt was admitted and a matching assistant turn completed."""
    if not re.fullmatch(r"^ses_[A-Za-z0-9]+$", session_id or ""):
        raise OpenCodeDispatchError("invalid_session_id")
    if not re.fullmatch(r"[0-9a-f]{64}", prompt_sha256 or ""):
        raise OpenCodeDispatchError("invalid_prompt_digest")
    quoted_id = urllib.parse.quote(session_id, safe="")
    client = _Client(connection)
    rows, cursors = [], set()
    cursor_value = None
    for page_number in range(10):
        query = {"limit": "100"}
        if cursor_value is None:
            query["order"] = "asc"
        else:
            query["cursor"] = cursor_value
        path = f"/api/session/{quoted_id}/message?{urllib.parse.urlencode(query)}"
        value = _json(*client.get(path))
        if not isinstance(value, dict) or set(value) != {"data", "cursor"}:
            raise OpenCodeDispatchError("startup_message_schema_unknown")
        page, page_cursor = value["data"], value["cursor"]
        if (not isinstance(page, list) or not isinstance(page_cursor, dict)
                or set(page_cursor) - {"previous", "next"}
                or any(item is not None and not isinstance(item, str) for item in page_cursor.values())):
            raise OpenCodeDispatchError("startup_message_schema_unknown")
        if not page:
            if any(item is not None for item in page_cursor.values()):
                raise OpenCodeDispatchError("startup_message_schema_unknown")
            break
        rows.extend(page)
        if len(rows) > 900:
            raise OpenCodeDispatchError("startup_message_history_truncated")
        if page_number == 9:
            raise OpenCodeDispatchError("startup_message_history_truncated")
        next_cursor = page_cursor.get("next")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise OpenCodeDispatchError("startup_message_schema_unknown")
        if next_cursor in cursors:
            raise OpenCodeDispatchError("startup_message_pagination_ambiguous")
        cursors.add(next_cursor)
        cursor_value = next_cursor
    users, assistants = [], []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not _MESSAGE_ID.fullmatch(row["id"]):
            raise OpenCodeDispatchError("startup_message_schema_unknown")
        kind = row.get("type")
        if kind == "user":
            text = row.get("text")
            if not isinstance(text, str) or len(text.encode("utf-8")) > 32768:
                raise OpenCodeDispatchError("startup_user_message_schema_unknown")
            users.append(row)
        elif kind == "assistant":
            assistants.append(row)
        elif kind not in ("agent-switched", "model-switched", "location-switched", "synthetic",
                          "system", "skill", "shell", "compaction", "idle"):
            raise OpenCodeDispatchError("startup_message_schema_unknown")
    if not users:
        return {"state": "not_admitted"}
    if len(users) != 1 or hashlib.sha256(users[0]["text"].encode("utf-8")).hexdigest() != prompt_sha256:
        raise OpenCodeDispatchError("startup_prompt_identity_mismatch")
    user_time = users[0].get("time")
    user_created = user_time.get("created") if isinstance(user_time, dict) else None
    if type(user_created) not in (int, float) or not math.isfinite(user_created):
        raise OpenCodeDispatchError("startup_user_message_schema_unknown")
    later = []
    for row in assistants:
        time_data = row.get("time")
        created = time_data.get("created") if isinstance(time_data, dict) else None
        if type(created) not in (int, float) or not math.isfinite(created):
            raise OpenCodeDispatchError("startup_assistant_message_schema_unknown")
        if created < user_created:
            continue
        model = row.get("model")
        if (not isinstance(model, dict) or model.get("providerID") != provider_id
                or model.get("id") != model_id
                or model.get("variant") != "max"):
            raise OpenCodeDispatchError("startup_assistant_model_mismatch")
        later.append(row)
    if not later:
        return {"state": "running", "user_message_id": users[0]["id"]}
    assistant = later[-1]
    finish = assistant.get("finish")
    time_data = assistant.get("time", {})
    completed = time_data.get("completed") if isinstance(time_data, dict) else None
    if finish == "error":
        return {"state": "failed", "user_message_id": users[0]["id"],
                "assistant_message_id": assistant["id"]}
    if finish not in ("stop", "length") or type(completed) not in (int, float) or not math.isfinite(completed):
        return {"state": "running", "user_message_id": users[0]["id"]}
    return {"state": "complete", "user_message_id": users[0]["id"],
            "assistant_message_id": assistant["id"], "finish": finish}


def latest_user_message_matches(connection: Connection, *, session_id: str,
                                message_id: str, text_sha256: str) -> bool:
    if not re.fullmatch(r"^ses_[A-Za-z0-9]+$", session_id or ""):
        raise OpenCodeDispatchError("invalid_session_id")
    if not _MESSAGE_ID.fullmatch(message_id or "") or not re.fullmatch(r"[0-9a-f]{64}", text_sha256 or ""):
        raise OpenCodeDispatchError("invalid_message_binding")
    quoted_id = urllib.parse.quote(session_id, safe="")
    value = _json(*_Client(connection).get(
        f"/api/session/{quoted_id}/message?limit=1&order=desc&type=user"))
    if not isinstance(value, dict) or set(value) != {"data", "cursor"}:
        raise OpenCodeDispatchError("session_message_schema_unknown")
    rows, cursor = value["data"], value["cursor"]
    if (not isinstance(rows, list) or not isinstance(cursor, dict)
            or set(cursor) - {"previous", "next"}
            or len(rows) != 1 or not isinstance(rows[0], dict)):
        raise OpenCodeDispatchError("session_message_schema_unknown")
    row = rows[0]
    text = row.get("text")
    return (row.get("id") == message_id and row.get("type") == "user"
            and isinstance(text, str) and hashlib.sha256(text.encode("utf-8")).hexdigest() == text_sha256)


def create_session(connection: Connection, *, directory: str, choice: dict, launch_id: str,
                   task_digest: str, parent_task_id: str | None,
                   parent_session_id: str | None = None) -> dict:
    if parent_task_id != connection.parent_task_id:
        raise OpenCodeDispatchError("parent_task_id_mismatch")
    if parent_session_id is not None and not re.fullmatch(r"^ses_[A-Za-z0-9]+$", parent_session_id):
        raise OpenCodeDispatchError("invalid_parent_session_id")
    directory = str(Path(directory).resolve(strict=True))
    instance = _json(*_Client(connection).get("/api/info"))
    version, pid = _api_instance(instance)
    if version != SUPPORTED_OPENCODE_VERSION:
        raise OpenCodeDispatchError("unsupported_opencode_version")
    if pid != connection.expected_server_pid:
        raise OpenCodeDispatchError("opencode_server_pid_mismatch")
    metadata = {"starfleet.model-preferences": {
        "launch_id": launch_id,
        "selection": choice["selection"],
        "task_sha256": task_digest,
        "parent_task_id": parent_task_id,
        "parent_session_id": parent_session_id,
    }}
    payload = {"location": {"directory": directory},
               "model": {"providerID": choice["provider_id"], "id": choice["model"], "variant": "max"},
               "metadata": metadata}
    if parent_session_id is not None:
        payload["parentID"] = parent_session_id
    client = _Client(connection)
    result = _json(*client.post("/api/session", payload))
    data = result.get("data")
    session_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(session_id, str) or not re.fullmatch(r"^ses_[A-Za-z0-9]+$", session_id):
        raise OpenCodeDispatchError("created_session_id_unverified")
    verified = verify_session(connection, session_id, expected_directory=directory, choice=choice,
                              launch_id=launch_id, task_digest=task_digest,
                              parent_task_id=parent_task_id, parent_session_id=parent_session_id)
    return verified


def _validate_cmux_capability(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096
            or value.strip() != value or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise OpenCodeDispatchError("cmux_socket_capability_unavailable")
    return value


def set_session_environment(connection: Connection, session_id: str, *, workspace_uuid: str,
                            surface_uuid: str, launch_id: str, cmux_socket_path: str | None,
                            cmux_socket_capability: str, model_preferences_manifest: str | None = None,
                            pattern_buffer_script: str | None = None, task_id: str | None = None,
                            parent_task_id: str | None = None) -> None:
    capability = _validate_cmux_capability(cmux_socket_capability)
    quoted_id = urllib.parse.quote(session_id, safe="")
    payload = {"variables": {"OPENCODE_SESSION_ID": session_id,
                              "CMUX_WORKSPACE_ID": workspace_uuid,
                              "CMUX_SURFACE_ID": surface_uuid,
                              "CMUX_AGENT_LAUNCH_KIND": "opencode",
                              "CMUX_SOCKET_CAPABILITY": capability,
                              "MODEL_PREFERENCES_LAUNCH_ID": launch_id}}
    if cmux_socket_path is not None:
        if not isinstance(cmux_socket_path, str) or not Path(cmux_socket_path).is_absolute() or any(ord(char) < 32 for char in cmux_socket_path):
            raise OpenCodeDispatchError("cmux_socket_path_unverified")
        payload["variables"]["CMUX_SOCKET_PATH"] = cmux_socket_path
    optional = {
        "MODEL_PREFERENCES_OPENCODE_MANIFEST": model_preferences_manifest,
        "MODEL_PREFERENCES_PATTERN_BUFFER": pattern_buffer_script,
        "MODEL_PREFERENCES_TASK_ID": task_id,
        "MODEL_PREFERENCES_PARENT_TASK_ID": parent_task_id,
    }
    for key, value in optional.items():
        if value is not None:
            if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
                raise OpenCodeDispatchError("session_environment_value_unverified")
            payload["variables"][key] = value
    status, _ = _Client(connection).put(f"/api/session/{quoted_id}/environment", payload)
    if status != 204:
        raise OpenCodeDispatchError("session_environment_set_unconfirmed")


def write_private_snapshot(path: str | Path, content: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        raise OpenCodeDispatchError("prompt_snapshot_create_failed") from None
    try:
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(content):
            offset += os.write(fd, content[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)


def read_private_json(path: str | Path, maximum: int = 16 * 1024) -> dict:
    raw = _private_bytes(str(path), maximum, "private_json_file_invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise OpenCodeDispatchError("private_json_file_invalid") from None
    if not isinstance(value, dict):
        raise OpenCodeDispatchError("private_json_file_invalid")
    return value


def write_private_json(path: str | Path, value: dict) -> None:
    write_private_snapshot(path, json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def tui_child(manifest_path: str, session_id: str, directory: str, prompt_file: str,
              provider_id: str, model_id: str, launch_id: str, selection: str, task_digest: str,
              handshake_file: str, release_file: str, abort_file: str, binary: str,
              parent_session_id: str | None = None) -> int:
    """Open a fresh, exact API-created session in an interactive TUI surface."""
    connection = load_connection(manifest_path)
    choice = {"harness": "opencode", "provider_id": provider_id, "model": model_id, "effort": "max",
              "selection": selection}
    preflight(connection, choice)
    verify_session(connection, session_id, expected_directory=directory, choice=choice,
                   launch_id=launch_id, task_digest=task_digest,
                   parent_task_id=connection.parent_task_id,
                   parent_session_id=parent_session_id)
    prompt_path = Path(prompt_file)
    prompt = _private_bytes(str(prompt_path), 32768, "private_prompt_snapshot_invalid").decode("utf-8")
    try:
        identify = subprocess.run(["cmux", "--json", "--id-format", "uuids", "identify"],
                                  capture_output=True, text=True, timeout=5, check=True)
        caller = json.loads(identify.stdout).get("caller") or {}
        workspace_uuid = str(uuid.UUID(caller.get("workspace_id")))
        surface_uuid = str(uuid.UUID(caller.get("surface_id")))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        raise OpenCodeDispatchError("cmux_surface_handshake_unavailable") from None
    capability = _validate_cmux_capability(os.environ.get("CMUX_SOCKET_CAPABILITY"))
    socket_path = os.environ.get("CMUX_SOCKET_PATH")
    if socket_path is not None and (not Path(socket_path).is_absolute()
                                    or any(ord(char) < 32 for char in socket_path)):
        raise OpenCodeDispatchError("cmux_socket_path_unverified")
    write_private_json(handshake_file, {"launch_id": launch_id, "session_id": session_id,
                                        "workspace_uuid": workspace_uuid, "surface_uuid": surface_uuid,
                                        "cmux_socket_path": socket_path,
                                        "cmux_socket_capability": capability})
    deadline = time.monotonic() + 45
    release = None
    while time.monotonic() < deadline:
        if Path(abort_file).exists():
            return 2
        if Path(release_file).exists():
            release = read_private_json(release_file)
            break
        time.sleep(0.1)
    expected_release = {"launch_id": launch_id, "session_id": session_id,
                        "workspace_uuid": workspace_uuid, "surface_uuid": surface_uuid}
    if release != expected_release:
        raise OpenCodeDispatchError("launch_barrier_release_unverified")
    preflight(connection, choice)
    verify_session(connection, session_id, expected_directory=directory, choice=choice,
                   launch_id=launch_id, task_digest=task_digest,
                   parent_task_id=connection.parent_task_id,
                   parent_session_id=parent_session_id)
    binary = check_opencode_cli(binary)
    env = dict(os.environ)
    env.pop("OPENCODE_PASSWORD", None)
    env["OPENCODE_SERVER_PASSWORD"] = connection.password
    argv = [binary, directory, "--server", connection.api_origin, "--session", session_id, "--prompt", prompt]
    return subprocess.run(argv, cwd=directory, env=env).returncode


def shutil_which_opencode() -> str:
    import shutil
    binary = shutil.which("opencode")
    if not binary:
        raise OpenCodeDispatchError("opencode_cli_unavailable")
    return binary


def check_opencode_cli(binary: str | None = None) -> str:
    binary = binary or shutil_which_opencode()
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise OpenCodeDispatchError("opencode_cli_version_unverified") from None
    if result.returncode != 0 or result.stdout.strip() != SUPPORTED_OPENCODE_VERSION:
        raise OpenCodeDispatchError("unsupported_opencode_cli_version")
    return str(Path(binary).resolve())


def require_native_dispatch_binding(*, expected_workspace: str, expected_surface: str,
                                    expected_session: str, current: dict) -> dict:
    """Accept only one exact native OpenCode session binding in its created surface."""
    if not all(isinstance(value, str) and value for value in (expected_workspace, expected_surface, expected_session)):
        raise OpenCodeDispatchError("native_binding_expectations_required")
    try:
        expected_workspace = str(uuid.UUID(expected_workspace))
        expected_surface = str(uuid.UUID(expected_surface))
    except (ValueError, AttributeError):
        raise OpenCodeDispatchError("native_binding_expectations_required") from None
    availability = current.get("owner_availability") if isinstance(current, dict) else None
    if (not isinstance(current, dict) or current.get("truncated") is not False
            or not isinstance(current.get("items"), list)
            or not isinstance(availability, dict) or availability.get("agent_sessions") != "available"):
        raise OpenCodeDispatchError("native_binding_observation_unknown")
    matches, conflicts = [], []
    for item in current["items"]:
        if not isinstance(item, dict):
            raise OpenCodeDispatchError("native_binding_observation_unknown")
        lifecycle = item.get("lifecycle")
        if lifecycle not in _ACTIVE_LIFECYCLES:
            continue
        placement = item.get("placement")
        if not isinstance(placement, dict):
            raise OpenCodeDispatchError("native_binding_observation_unknown")
        if placement.get("kind") != "local":
            continue
        agents = item.get("agents", [])
        if not isinstance(agents, list):
            raise OpenCodeDispatchError("native_binding_observation_unknown")
        freshness = item.get("freshness")
        if not isinstance(freshness, dict):
            raise OpenCodeDispatchError("native_binding_observation_unknown")
        for agent in agents:
            if not isinstance(agent, dict) or agent.get("kind") != "opencode" or agent.get("session_id") != expected_session:
                continue
            projections = item.get("projections")
            if not isinstance(projections, list):
                raise OpenCodeDispatchError("native_binding_observation_unknown")
            for projection in projections:
                if not isinstance(projection, dict):
                    raise OpenCodeDispatchError("native_binding_observation_unknown")
                try:
                    workspace_uuid = str(uuid.UUID(projection.get("workspace_id")))
                    surface_uuid = str(uuid.UUID(projection.get("panel_id")))
                except (ValueError, TypeError, AttributeError):
                    raise OpenCodeDispatchError("native_binding_observation_unknown") from None
                match = {"workspace_uuid": workspace_uuid,
                         "surface_uuid": surface_uuid, "session_id": expected_session,
                         "state": agent.get("state"), "lifecycle": lifecycle,
                         "input_state": agent.get("input_state"), "freshness": freshness.get("state")}
                conflicts.append(match)
                if match["workspace_uuid"] == expected_workspace and match["surface_uuid"] == expected_surface:
                    matches.append(match)
    if len(matches) == 0 and len(conflicts) == 0:
        raise OpenCodeDispatchError("native_session_surface_binding_pending")
    if len(matches) == 0 and len(conflicts) > 0:
        raise OpenCodeDispatchError("native_session_visible_on_other_surface")
    if len(matches) != 1 or len(conflicts) != 1:
        raise OpenCodeDispatchError("native_session_surface_ambiguous")
    exact = matches[0]
    if exact.get("state") not in ("working", "idle", "needs_input", "unknown"):
        raise OpenCodeDispatchError("native_session_state_unknown")
    if exact.get("state") == "unknown" and exact.get("lifecycle") != "idle":
        raise OpenCodeDispatchError("native_session_state_unknown")
    if exact.get("freshness") != "current":
        raise OpenCodeDispatchError("native_session_observation_stale")
    return exact


def native_hook_store_path() -> Path:
    return Path.home() / ".cmuxterm" / "opencode-hook-sessions.json"


def require_native_hook_binding(*, expected_workspace: str, expected_surface: str,
                                expected_session: str, worktree_directory: str,
                                expected_started_at: float | int | None = None,
                                hook_path: str | Path | None = None) -> dict:
    """Bind one exact current OpenCode session to the CMUX native hook generation and worktree."""
    try:
        workspace = str(uuid.UUID(expected_workspace))
        surface = str(uuid.UUID(expected_surface))
    except (ValueError, TypeError, AttributeError):
        raise OpenCodeDispatchError("native_binding_expectations_required") from None
    if not isinstance(expected_session, str) or not re.fullmatch(r"^ses_[A-Za-z0-9]+$", expected_session):
        raise OpenCodeDispatchError("native_binding_expectations_required")
    try:
        expected_worktree = str(Path(worktree_directory).expanduser().resolve(strict=True))
    except (OSError, RuntimeError, TypeError):
        raise OpenCodeDispatchError("native_worktree_unavailable") from None
    if not Path(expected_worktree).is_dir():
        raise OpenCodeDispatchError("native_worktree_unavailable")
    path = Path(hook_path) if hook_path is not None else native_hook_store_path()
    raw = _private_bytes(str(path), 2 * 1024 * 1024, "native_hook_store_unavailable")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise OpenCodeDispatchError("native_hook_store_malformed") from None
    sessions = value.get("sessions") if isinstance(value, dict) else None
    record = sessions.get(expected_session) if isinstance(sessions, dict) else None
    if not isinstance(record, dict):
        raise OpenCodeDispatchError("native_hook_session_unavailable")
    try:
        record_workspace = str(uuid.UUID(record.get("workspaceId")))
        record_surface = str(uuid.UUID(record.get("surfaceId")))
    except (ValueError, TypeError, AttributeError):
        raise OpenCodeDispatchError("native_hook_identity_malformed") from None
    if record_workspace != workspace or record_surface != surface:
        raise OpenCodeDispatchError("native_hook_surface_mismatch")
    started_at = record.get("startedAt")
    if (type(started_at) not in (int, float) or not math.isfinite(started_at) or started_at < 0
            or (expected_started_at is not None and started_at != expected_started_at)):
        raise OpenCodeDispatchError("native_hook_generation_mismatch")
    lifecycle = record.get("agentLifecycle")
    if lifecycle not in _ACTIVE_LIFECYCLES:
        raise OpenCodeDispatchError("native_hook_session_inactive")
    native_cwd = record.get("cwd") or record.get("workingDirectory")
    try:
        actual_worktree = str(Path(native_cwd).expanduser().resolve(strict=True))
    except (OSError, RuntimeError, TypeError):
        raise OpenCodeDispatchError("native_hook_worktree_unavailable") from None
    if actual_worktree != expected_worktree:
        raise OpenCodeDispatchError("native_hook_worktree_mismatch")
    return {"session_id": expected_session, "workspace_uuid": workspace,
            "surface_uuid": surface, "started_at": started_at,
            "agent_lifecycle": lifecycle, "worktree_directory": actual_worktree}


def require_native_empty_composer(run_rpc, *, expected_workspace: str, expected_surface: str) -> dict:
    """Require CMUX's exact empty-composer and native receiving guards before a prompt send."""
    try:
        workspace = str(uuid.UUID(expected_workspace))
        surface = str(uuid.UUID(expected_surface))
    except (ValueError, TypeError, AttributeError):
        raise OpenCodeDispatchError("native_binding_expectations_required") from None
    guard = run_rpc("surface.input_state", surface_id=surface)
    if not isinstance(guard, dict):
        raise OpenCodeDispatchError("native_input_state_unknown")
    try:
        input_surface = str(uuid.UUID(guard.get("surface_id")))
        input_workspace = str(uuid.UUID(guard.get("workspace_id")))
    except (ValueError, TypeError, AttributeError):
        raise OpenCodeDispatchError("native_input_state_identity_mismatch") from None
    if input_surface != surface or input_workspace != workspace:
        raise OpenCodeDispatchError("native_input_state_identity_mismatch")
    if (guard.get("agent") is not True or guard.get("terminal") is not True
            or guard.get("state") != "empty" or guard.get("blocks_typing") is not False
            or guard.get("waiting_on_human") is not False
            or guard.get("lifecycle") not in _ACTIVE_LIFECYCLES):
        raise OpenCodeDispatchError("native_composer_not_empty")
    settings = run_rpc("agent.message.settings", target=surface)
    if not isinstance(settings, dict):
        raise OpenCodeDispatchError("native_message_settings_unknown")
    if not all(settings.get(key) is True for key in ("messages_enabled", "receiving", "workspace_receiving")):
        raise OpenCodeDispatchError("native_message_receiving_disabled")
    return {"workspace_uuid": workspace, "surface_uuid": surface,
            "state": guard.get("state"), "lifecycle": guard.get("lifecycle"),
            "messages_enabled": True, "receiving": True, "workspace_receiving": True}


def send_prompt(connection: Connection, *, session_id: str, text: str) -> dict:
    """Submit one API prompt after the caller has completed the exact native empty-composer guard."""
    if not isinstance(session_id, str) or not re.fullmatch(r"^ses_[A-Za-z0-9]+$", session_id):
        raise OpenCodeDispatchError("invalid_session_id")
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 16 * 1024:
        raise OpenCodeDispatchError("invalid_prompt")
    quoted_id = urllib.parse.quote(session_id, safe="")
    status, body = _Client(connection).post(f"/api/session/{quoted_id}/prompt", {"text": text})
    value = _json(status, body)
    data = value.get("data")
    if not isinstance(data, dict):
        raise OpenCodeDispatchError("prompt_response_unconfirmed")
    message_id = data.get("id")
    if (not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id)
            or data.get("sessionID") != session_id or data.get("type") != "user"
            or not all(key in data for key in ("time", "payload", "delivery"))):
        raise OpenCodeDispatchError("prompt_response_unconfirmed")
    return {"message_id": message_id, "session_id": session_id,
            "admission": "confirmed", "transport": "opencode_api"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args.pop(0) != "tui-child":
        return 2
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    for name in ("manifest", "session", "directory", "prompt-file", "provider-id", "model-id",
                 "launch-id", "selection", "task-digest", "handshake-file", "release-file", "abort-file", "binary"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--parent-session-id")
    try:
        values = parser.parse_args(args)
        return tui_child(values.manifest, values.session, values.directory, values.prompt_file,
                         values.provider_id, values.model_id, values.launch_id,
                         values.selection, values.task_digest, values.handshake_file,
                         values.release_file, values.abort_file, values.binary,
                         values.parent_session_id)
    except (OpenCodeDispatchError, OSError, ValueError, TypeError) as error:
        code = error.code if isinstance(error, OpenCodeDispatchError) else "child_launch_failed"
        print(f"model-preferences OpenCode child: {code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

