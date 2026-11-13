#!/usr/bin/env python3
"""Monitor native CMUX inboxes and nudge explicitly enrolled Claude, Codex, and OpenCode sessions."""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import http.client
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_STATE = Path.home() / ".local/state/starfleet-ai/hailing-frequencies"
SCAN_LIMIT = 1000
INBOX_MESSAGE_LIMIT = 20
INBOX_OUTPUT_LIMIT = 64 * 1024
ATTENTION_MESSAGE_LIMIT = 4 * 1024
ACTIVE_LIFECYCLES = ("running", "idle", "backgroundWorkPending")
NATIVE_PROMPT_READINESS = "native_prompt"
DISCORD_REPLY_READINESS = "discord_reply"
CMUX_READINESS_POLICIES = frozenset((NATIVE_PROMPT_READINESS, DISCORD_REPLY_READINESS))
INSTALL_MARKER = "# starfleet-hailing-frequencies:v1"
RUNTIME_SOURCE_NAMES = ("hailing_frequencies.py", "service.py", "temporal_beacon.py", "guide.md",
                        "agent-notify", "agent-notify-guide.md", "discord_cleanup.py",
                        "discord-cleanup.md", "opencode_adapter.py")
RUNTIME_SKILL_NAMES = ("skills/temporal-beacon/SKILL.md", "skills/agent-notify/SKILL.md")
OPENCODE_SESSION = re.compile(r"^ses_[A-Za-z0-9]+$")
OPENCODE_MESSAGE = re.compile(r"^msg_[A-Za-z0-9]+$")
OPENCODE_MANIFEST_FIELDS = frozenset((
    "api_url", "password_file", "session_id", "expected_server_pid", "session_directory",
    "worktree_directory", "cmux_workspace_uuid", "cmux_surface_uuid", "parent_task_id",
))
OPENCODE_MANIFEST_OPTIONAL_FIELDS = frozenset(("delivery_policy",))
OPENCODE_DEFAULT_DELIVERY_POLICY = "native_empty_composer"
OPENCODE_API_SIDECHANNEL_DELIVERY_POLICY = "api_sidechannel"
OPENCODE_HTTP_TIMEOUT = 5
OPENCODE_RESPONSE_LIMIT = 128 * 1024
STARFLEET_DIAGNOSTIC_LIMIT = 256 * 1024


class LockBusyError(ValueError):
    """A state operation already owns the monitor lock."""


class InboxScanSaturatedError(ValueError):
    """A native inbox reached the scan limit and cannot be evaluated safely."""


class TargetInboxError(ValueError):
    """A native inbox lookup failed for one enrolled target."""


class DeliveryHoldError(ValueError):
    """The final guarded binding check held delivery before paste began."""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def identity(value):
    return str(uuid.UUID(value)).upper()


def session_identity(value, harness=None):
    if harness == "opencode":
        require(isinstance(value, str) and OPENCODE_SESSION.fullmatch(value) is not None,
                "OpenCode session ID must be the exact ses_ identity")
        return value
    if harness in ("claude", "codex"):
        return identity(value)
    require(isinstance(value, str), "Session identity is invalid")
    if OPENCODE_SESSION.fullmatch(value):
        return value
    return identity(value)


def _read_private_file(path, label, limit):
    require(isinstance(path, str) and Path(path).is_absolute(), f"{label} path must be absolute")
    candidate = Path(path)
    require(not candidate.is_symlink(), f"Refusing symlink {label}")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        try:
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode), f"{label} is not a regular file")
            require(info.st_uid == os.getuid(), f"{label} is not owned by this user")
            require(info.st_mode & 0o177 == 0, f"{label} must be private to this user")
            chunks = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(4096, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                require(total <= limit, f"{label} exceeds the bounded file size")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ValueError(f"Could not read private {label} ({type(exc).__name__})") from None


def load_opencode_manifest(path):
    raw = _read_private_file(str(Path(path).expanduser().absolute()), "OpenCode manifest", 16 * 1024)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise ValueError("OpenCode manifest is not valid JSON") from None
    require(isinstance(value, dict), "OpenCode manifest must be a JSON object")
    require(OPENCODE_MANIFEST_FIELDS.issubset(value) and
            set(value).issubset(OPENCODE_MANIFEST_FIELDS | OPENCODE_MANIFEST_OPTIONAL_FIELDS),
            "OpenCode manifest fields do not match the supported session binding contract")
    value["delivery_policy"] = opencode_delivery_policy(value)
    require(isinstance(value.get("api_url"), str), "OpenCode API URL is missing")
    require(isinstance(value.get("password_file"), str), "OpenCode password file path is missing")
    for key in ("password_file", "session_directory", "worktree_directory"):
        require(isinstance(value.get(key), str) and Path(value[key]).is_absolute(),
                f"OpenCode manifest {key} must be an absolute path")
    return value


def opencode_delivery_policy(target):
    require(isinstance(target, dict), "OpenCode delivery policy target is malformed")
    if "delivery_policy" not in target:
        return OPENCODE_DEFAULT_DELIVERY_POLICY
    adapter = opencode_adapter_module()
    validator = getattr(adapter, "validate_delivery_policy", None)
    require(callable(validator), "OpenCode delivery policy validator is unavailable")
    try:
        return validator(target["delivery_policy"])
    except ValueError:
        raise ValueError("OpenCode delivery policy is invalid") from None


def opencode_adapter_module():
    candidates = (
        Path(__file__).resolve().with_name("opencode_adapter.py"),
        Path(__file__).resolve().parent.parent / "opencode-adapter" / "opencode_adapter.py",
    )
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            name = "starfleet_hailing_opencode_adapter"
            loaded = sys.modules.get(name)
            if loaded is not None and Path(getattr(loaded, "__file__", "")).resolve() == candidate.resolve():
                return loaded
            spec = importlib.util.spec_from_file_location(name, candidate)
            require(spec is not None and spec.loader is not None,
                    "OpenCode session adapter could not be loaded")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(name, None)
                raise
            return module
    raise ValueError("OpenCode session adapter is unavailable; reinstall Hailing Frequencies")


def _opencode_api_binding(target, *, identity_only):
    adapter = opencode_adapter_module()
    password_path = target.get("password_file")
    raw_password = _read_private_file(password_path, "OpenCode password file", 4096)
    if raw_password.endswith(b"\n"):
        raw_password = raw_password[:-1]
        if raw_password.endswith(b"\r"):
            raw_password = raw_password[:-1]
    try:
        password = raw_password.decode("utf-8")
    except UnicodeError:
        raise ValueError("OpenCode password file is not valid UTF-8") from None
    require(bool(password), "OpenCode password file is empty")
    binding = adapter.SessionBinding(
        session_id=target["session"],
        expected_server_pid=target["expected_server_pid"],
        session_directory=target["session_directory"],
        worktree_directory=target["worktree_directory"],
        cmux_workspace_uuid=target["workspace"],
        cmux_surface_uuid=target["surface"],
        parent_task_id=target["parent_task_id"],
    )
    client = adapter.OpenCodeSessionAdapter(target["api_url"], password)
    inspection = client.inspect_identity(binding) if identity_only else client.inspect(binding)
    expected_disposition = "identity_verified" if identity_only else "clear"
    require(inspection.disposition == expected_disposition,
            ("OpenCode API identity held: " if identity_only else "OpenCode API inspection held: ")
            + ", ".join(inspection.hold_codes or ("unknown_state",)))
    return password, inspection


def opencode_binding(target):
    return _opencode_api_binding(target, identity_only=False)


def opencode_identity_binding(target):
    return _opencode_api_binding(target, identity_only=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def opencode_prompt(target, password, prompt):
    adapter = opencode_adapter_module()
    policy = opencode_delivery_policy(target)
    origin = adapter.OpenCodeSessionAdapter._validate_origin(target["api_url"])
    quoted_session = urllib.parse.quote(target["session"], safe="")
    payload = {"text": prompt}
    if policy == OPENCODE_API_SIDECHANNEL_DELIVERY_POLICY:
        payload.update({"delivery": "queue", "resume": True})
    request = urllib.request.Request(
        origin + f"/api/session/{quoted_session}/prompt",
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": "Basic " + base64.b64encode(
                f"opencode:{password}".encode("utf-8")).decode("ascii"),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=OPENCODE_HTTP_TIMEOUT)
    except urllib.error.HTTPError as error:
        error.close()
        raise ValueError("OpenCode prompt response was not confirmed") from None
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
        raise ValueError("OpenCode prompt response was not confirmed") from None
    try:
        with response:
            if getattr(response, "status", None) != 200:
                raise ValueError("OpenCode prompt response was not confirmed")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    raise ValueError("OpenCode prompt response was not confirmed") from None
                require(declared_length <= OPENCODE_RESPONSE_LIMIT,
                        "OpenCode prompt response exceeded the bounded response size")
            body = response.read(OPENCODE_RESPONSE_LIMIT + 1)
            require(len(body) <= OPENCODE_RESPONSE_LIMIT,
                    "OpenCode prompt response exceeded the bounded response size")
            value = json.loads(body.decode("utf-8"))
            require(isinstance(value, dict), "OpenCode prompt response was not confirmed")
            data = value.get("data")
            require(isinstance(data, dict), "OpenCode prompt response was not confirmed")
            message_id = data.get("id")
            require(isinstance(message_id, str) and OPENCODE_MESSAGE.fullmatch(message_id) is not None,
                    "OpenCode prompt response was not confirmed")
            require(data.get("sessionID") == target["session"] and data.get("type") == "user",
                    "OpenCode prompt response was not associated with the exact session")
            require(all(key in data for key in ("time", "payload", "delivery")),
                    "OpenCode prompt response was not confirmed")
            if policy == OPENCODE_API_SIDECHANNEL_DELIVERY_POLICY:
                require(data.get("delivery") == "queue",
                        "OpenCode queue admission was not confirmed")
    except (UnicodeError, ValueError) as error:
        raise ValueError(str(error) or "OpenCode prompt response was not confirmed") from None
    except (OSError, TimeoutError, http.client.HTTPException):
        raise ValueError("OpenCode prompt response was not confirmed") from None
    return {"submitted": True, "transport": "opencode_api", "admission": "confirmed",
            "message_id": message_id, "session_id": target["session"]}


def atomic_json(path, value):
    require(not path.is_symlink(), f"Refusing symlink: {path}")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with open(temporary, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Cmux:
    def __init__(self, executable="cmux", hook_directory=None):
        self.executable = executable
        self.hook_directory = Path(hook_directory or Path.home() / ".cmuxterm")
        self.stopping = lambda: False

    def command(self, *args):
        require(not self.stopping(), "Monitor stopping; no more CMUX operations")
        result = subprocess.run([self.executable, "--json", *args], capture_output=True,
                                text=True, timeout=15)
        require(result.returncode == 0, result.stderr.strip() or "CMUX command failed")
        return json.loads(result.stdout)

    def rpc(self, method, **params):
        return self.command("rpc", method, json.dumps(params))

    def messages(self, target):
        # Omitting state requests the full inbox, including native read records
        # needed to correlate exact replies. Never change native read state.
        rows = self.rpc("agent.message.list", surface=target["surface"], limit=SCAN_LIMIT)["messages"]
        if len(rows) >= SCAN_LIMIT:
            raise InboxScanSaturatedError(
                "Inbox scan saturated; no nudges sent. Reconcile native inboxes first.")
        return rows

    def activity(self, target):
        """Read activity for one exact native session without changing its helper."""
        current = self.command("current", "--limit", "200")
        require(not current.get("truncated", True), "CMUX activity snapshot is incomplete")
        require(current.get("owner_availability", {}).get("agent_sessions") == "available",
                "CMUX activity identities unavailable")
        matches = [agent for item in current["items"]
                   if item.get("placement", {}).get("kind") == "local"
                   and any(p.get("panel_id", "").upper() == target["surface"]
                           and p.get("workspace_id", "").upper() == target["workspace"]
                           for p in item.get("projections", []))
                   for agent in item.get("agents", [])
                   if ((agent.get("session_id") == target["session"] if target["harness"] == "opencode"
                        else agent.get("session_id", "").upper() == target["session"]))
                   and agent.get("kind") == target["harness"]]
        require(len(matches) == 1, "Exact session activity is unavailable or ambiguous")
        return {"state": matches[0].get("state"), "last_activity_at": matches[0].get("last_activity_at")}

    def binding(self, target, *, readiness_policy=NATIVE_PROMPT_READINESS):
        """Validate the live owner and selected native input-readiness policy."""
        require(readiness_policy in CMUX_READINESS_POLICIES, "Unknown CMUX readiness policy")
        if target.get("harness") == "opencode":
            require(readiness_policy == NATIVE_PROMPT_READINESS,
                    "Discord reply readiness is only available for Claude and Codex")
            opencode_binding(target)
            self._opencode_native_binding(target)
            return True
        if readiness_policy == DISCORD_REPLY_READINESS:
            require(target.get("harness") in ("claude", "codex"),
                    "Discord reply readiness is only available for Claude and Codex")
        current = self.command("current", "--limit", "200")
        require(not current.get("truncated", True), "CMUX current snapshot is incomplete")
        require(current.get("owner_availability", {}).get("agent_sessions") == "available",
                "CMUX agent identities unavailable")
        matches = []
        for item in current["items"]:
            if item.get("placement", {}).get("kind") != "local":
                continue
            for projection in item.get("projections", []):
                if projection.get("panel_id", "").upper() == target["surface"]:
                    matches.append((item, projection))
        require(len(matches) == 1, "Surface is missing or ambiguous")
        item, projection = matches[0]
        require(identity(projection["workspace_id"]) == target["workspace"], "Workspace changed")
        require(any(a.get("session_id", "").upper() == target["session"] and
                    a.get("kind") == target["harness"] and a.get("state") in ("working", "idle")
                    for a in item.get("agents", [])), "Session is absent, ended or waiting for input")
        store = json.loads((self.hook_directory / (target["harness"] + "-hook-sessions.json")).read_text())
        records = {k.upper(): v for k, v in store["sessions"].items()}
        record = records.get(target["session"], {})
        require(record.get("surfaceId", "").upper() == target["surface"] and
                record.get("workspaceId", "").upper() == target["workspace"], "Hook binding changed")
        if target["harness"] == "claude":
            active = store.get("activeSessionsBySurface", {}).get(target["surface"], {})
            require(active.get("sessionId", "").upper() == target["session"], "Claude session replaced")
        require(record.get("agentLifecycle") in ACTIVE_LIFECYCLES, "Harness is not running, idle or awaiting background work")
        started = record.get("startedAt")
        require(isinstance(started, (int, float)) and math.isfinite(started), "Missing session start evidence")
        require(started == target["started_at"], "Session generation changed")
        if readiness_policy == NATIVE_PROMPT_READINESS:
            guard = self.rpc("surface.input_state", surface_id=target["surface"])
            require(guard.get("surface_id", "").upper() == target["surface"] and
                    guard.get("workspace_id", "").upper() == target["workspace"], "Input owner changed")
            require(guard.get("agent") is True and guard.get("terminal") is True and
                    guard.get("state") == "empty" and guard.get("blocks_typing") is False and
                    guard.get("waiting_on_human") is False and guard.get("lifecycle") in ACTIVE_LIFECYCLES,
                    "Human draft, approval, unknown input state or inactive session")
        settings = self.rpc("agent.message.settings", target=target["surface"])
        require(all(settings.get(k) is True for k in ("messages_enabled", "receiving", "workspace_receiving")),
                "Native messaging is disabled")
        return True

    def enrollment_binding(self, target):
        """Verify exact OpenCode identity without requiring the session to be idle."""
        require(target.get("harness") == "opencode", "Enrollment identity check requires OpenCode")
        opencode_identity_binding(target)
        return self._opencode_native_binding(target, require_delivery_readiness=False)

    def owner_identity_binding(self, target):
        """Verify an exact OpenCode owner without requiring prompt receiving to be enabled."""
        require(target.get("harness") == "opencode", "Owner identity check requires OpenCode")
        opencode_identity_binding(target)
        return self._opencode_native_binding(target, require_delivery_readiness=False,
                                             require_message_receiving=False)

    def _opencode_native_binding(self, target, *, require_delivery_readiness=True,
                                 require_message_receiving=True):
        current = self.command("current", "--limit", "200")
        require(isinstance(current, dict), "CMUX current snapshot is unknown")
        require(not current.get("truncated", True), "CMUX current snapshot is incomplete")
        availability = current.get("owner_availability")
        require(isinstance(availability, dict) and availability.get("agent_sessions") == "available",
                "CMUX agent identities unavailable")
        items = current.get("items")
        require(isinstance(items, list), "CMUX current item projection is unavailable")
        matches = []
        for item in items:
            if not isinstance(item, dict):
                continue
            placement = item.get("placement")
            if not isinstance(placement, dict) or placement.get("kind") != "local":
                continue
            projections = item.get("projections")
            require(isinstance(projections, list), "CMUX surface projection is unknown")
            for projection in projections:
                if isinstance(projection, dict) and isinstance(projection.get("panel_id"), str) and \
                        projection["panel_id"].upper() == target["surface"]:
                    matches.append((item, projection))
        require(len(matches) == 1, "OpenCode surface is missing or ambiguous")
        item, projection = matches[0]
        workspace_id = projection.get("workspace_id")
        require(isinstance(workspace_id, str) and identity(workspace_id) == target["workspace"],
                "OpenCode CMUX workspace changed")

        hook_path = self.hook_directory / "opencode-hook-sessions.json"
        try:
            hook_raw = _read_private_file(str(hook_path), "OpenCode hook store", 2 * 1024 * 1024)
            store = json.loads(hook_raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise ValueError("OpenCode visible-TUI hook identity is unavailable") from None
        sessions = store.get("sessions") if isinstance(store, dict) else None
        require(isinstance(sessions, dict), "OpenCode hook identity store is malformed")
        record = sessions.get(target["session"])
        require(isinstance(record, dict), "Exact OpenCode ses_ identity is absent from the native hook store")
        surface_id = record.get("surfaceId")
        workspace_id = record.get("workspaceId")
        require(isinstance(surface_id, str) and surface_id.upper() == target["surface"] and
                isinstance(workspace_id, str) and workspace_id.upper() == target["workspace"],
                "OpenCode native hook surface or workspace changed")
        require(record.get("agentLifecycle") in ACTIVE_LIFECYCLES,
                "OpenCode session is not running or idle")
        started = record.get("startedAt")
        require(type(started) in (int, float) and math.isfinite(started),
                "OpenCode native session generation is unavailable")
        require(started == target["started_at"], "OpenCode native session generation changed")
        cwd = record.get("cwd") or record.get("workingDirectory")
        require(isinstance(cwd, str) and cwd, "OpenCode native worktree is unavailable")
        try:
            native_worktree = str(Path(cwd).expanduser().resolve(strict=True))
            expected_worktree = str(Path(target["worktree_directory"]).expanduser().resolve(strict=True))
        except (OSError, RuntimeError):
            raise ValueError("OpenCode native worktree is unavailable") from None
        require(native_worktree == expected_worktree, "OpenCode native worktree changed")

        agents = item.get("agents")
        require(isinstance(agents, list), "CMUX OpenCode agent projection is unknown")
        require(all(isinstance(agent, dict) for agent in agents), "CMUX OpenCode agent projection is malformed")
        visible_agents = [agent for agent in agents
                          if isinstance(agent, dict) and agent.get("kind") == "opencode" and
                          agent.get("session_id") == target["session"]]
        require(len(visible_agents) == 1 and visible_agents[0].get("state") in ("working", "idle"),
                "Exact OpenCode CMUX session projection is missing, ambiguous or inactive")

        if not require_delivery_readiness:
            if require_message_receiving:
                return self._require_opencode_message_receiving(target)
            return True

        guard = self.rpc("surface.input_state", surface_id=target["surface"])
        require(isinstance(guard, dict), "OpenCode input state is unknown")
        input_surface = guard.get("surface_id")
        input_workspace = guard.get("workspace_id")
        require(isinstance(input_surface, str) and input_surface.upper() == target["surface"] and
                isinstance(input_workspace, str) and input_workspace.upper() == target["workspace"],
                "OpenCode input owner changed")
        policy = opencode_delivery_policy(target)
        state = guard.get("state")
        guard_lifecycle = guard.get("lifecycle")
        if policy == OPENCODE_API_SIDECHANNEL_DELIVERY_POLICY:
            eligible_composer_state = state in ("empty", "draft", "unknown")
        else:
            eligible_composer_state = state == "empty"
        require(guard.get("agent") is True and guard.get("terminal") is True and
                eligible_composer_state and guard.get("blocks_typing") is False and
                guard.get("waiting_on_human") is False and guard_lifecycle in ACTIVE_LIFECYCLES,
                "OpenCode human draft, approval, unknown input state or inactive session")
        return self._require_opencode_message_receiving(target)

    def _require_opencode_message_receiving(self, target):
        settings = self.rpc("agent.message.settings", target=target["surface"])
        require(isinstance(settings, dict), "Native messaging settings are unknown")
        require(all(settings.get(key) is True for key in
                    ("messages_enabled", "receiving", "workspace_receiving")),
                "Native messaging is disabled")
        return True

    def submit(self, target, text, on_transport_start=None, *,
               readiness_policy=NATIVE_PROMPT_READINESS):
        if target.get("harness") == "opencode":
            require(readiness_policy == NATIVE_PROMPT_READINESS,
                    "Discord reply readiness is only available for Claude and Codex")
            try:
                password, _inspection = opencode_binding(target)
                self._opencode_native_binding(target)
            except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError, AttributeError) as exc:
                raise DeliveryHoldError(str(exc)) from exc
            if on_transport_start is not None:
                on_transport_start()
            return opencode_prompt(target, password, text)
        # One host paste/submit operation, never a force flag or separate Escape/Ctrl-C.
        # A multiline Claude paste selects the host's agent-aware submit key.
        try:
            if readiness_policy == NATIVE_PROMPT_READINESS:
                self.binding(target)
            else:
                self.binding(target, readiness_policy=readiness_policy)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise DeliveryHoldError(str(exc)) from exc
        if on_transport_start is not None:
            on_transport_start()
        result = self.command("paste", "--surface", target["surface"], "--submit", "--", text)
        require(result.get("submitted") is True, "Paste outcome uncertain: host did not confirm submit")
        return result


class Monitor:
    def __init__(self, directory, cmux=None, clock=time.time):
        self.directory = Path(directory).expanduser().absolute()
        self.cmux = cmux or Cmux()
        self.clock = clock

    @contextlib.contextmanager
    def locked(self, timeout=0):
        require(self.directory.is_dir() and not self.directory.is_symlink(), "Initialize a real state directory first")
        require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout >= 0,
                "Lock timeout must be a finite nonnegative number")
        path = self.directory / "monitor.lock"
        require(not path.is_symlink(), "Refusing symlink lock")
        with path.open("a") as stream:
            deadline = time.monotonic() + timeout
            try:
                while True:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            if timeout:
                                raise LockBusyError("Another Hailing Frequencies operation owns this state; retry at the next checkpoint") from None
                            raise LockBusyError("Another Hailing Frequencies operation owns this state") from None
                        time.sleep(min(0.1, remaining))
            except BaseException:
                stream.close()
                raise
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def config(self, value=None):
        if value is None:
            value = json.loads((self.directory / "config.json").read_text())
        require(value.get("version") == 1, "Unsupported configuration version")
        for key in ("remind_after", "escalate_after", "cooldown", "interval"):
            require(type(value.get(key)) in (int, float) and math.isfinite(value[key]) and value[key] > 0,
                    f"Invalid {key}")
        require(value["escalate_after"] >= value["remind_after"], "Escalation precedes reminder")
        require(value["interval"] >= 5, "Interval must be at least 5 seconds")
        require(isinstance(value.get("targets"), list), "Targets must be an array")
        surfaces, sessions = set(), set()
        for target in value["targets"]:
            harness = target.get("harness")
            require(harness in ("claude", "codex", "opencode"), "Unsupported harness")
            for key in ("surface", "workspace"):
                require(identity(target[key]) == target[key], "Use canonical UUIDs")
            require(session_identity(target["session"], harness) == target["session"],
                    "Session identity does not match its exact harness")
            for key in ("cutoff", "started_at"):
                require(type(target[key]) in (int, float) and math.isfinite(target[key]) and target[key] >= 0,
                        f"Invalid {key}")
            if harness == "opencode":
                require(all(isinstance(target.get(key), str) and target[key] for key in
                            ("api_url", "password_file", "session_directory", "worktree_directory",
                             "parent_task_id")), "OpenCode target binding is incomplete")
                require(all(Path(target[key]).is_absolute() for key in
                            ("password_file", "session_directory", "worktree_directory")),
                        "OpenCode target paths must be absolute")
                require(type(target.get("expected_server_pid")) is int and target["expected_server_pid"] > 0,
                        "Invalid OpenCode API server PID")
                if "delivery_policy" in target:
                    require(opencode_delivery_policy(target) == target["delivery_policy"],
                            "Invalid OpenCode delivery policy")
            else:
                require("delivery_policy" not in target,
                        "Delivery policy is supported only for OpenCode targets")
            require(target["surface"] not in surfaces and target["session"] not in sessions,
                    "Duplicate surface or session enrollment")
            surfaces.add(target["surface"])
            sessions.add(target["session"])
        require(value.get("coordinator") is None or value["coordinator"] in sessions, "Coordinator is not enrolled")
        return value

    def database(self):
        path = self.directory / "receipts.sqlite3"
        require(not path.is_symlink(), "Refusing symlink database")
        db = sqlite3.connect(path)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS attempts (session TEXT, message TEXT, kind TEXT, at REAL, outcome TEXT, PRIMARY KEY(session,message,kind))")
        db.execute("CREATE TABLE IF NOT EXISTS batches (token TEXT PRIMARY KEY, session TEXT, at REAL, outcome TEXT, detail TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS acknowledgements (session TEXT, message TEXT, at REAL, evidence TEXT, PRIMARY KEY(session,message))")
        db.execute("CREATE TABLE IF NOT EXISTS attention_intents (session TEXT, started_at REAL, event_key TEXT, body_sha256 TEXT, outcome TEXT, attempted INTEGER, at REAL, detail TEXT, PRIMARY KEY(session,started_at,event_key))")
        db.commit()
        return db

    def prior_attention_intent(self, target, event_key):
        path = self.directory / "receipts.sqlite3"
        require(not path.is_symlink(), "Refusing symlink database")
        if not path.is_file():
            return None
        uri = path.resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attention_intents'").fetchone()
            if table is None:
                return None
            return db.execute(
                "SELECT body_sha256,outcome,attempted,detail FROM attention_intents "
                "WHERE session=? AND started_at=? AND event_key=?",
                (target["session"], target["started_at"], event_key)).fetchone()

    def attention_result(self, outcome, attempted, detail, event_key):
        return {"outcome": outcome, "attempted": bool(attempted),
                "detail": str(detail)[:500], "event_key": str(event_key)[:128]}

    def enroll_opencode(self, manifest_path, label, include_existing=False):
        require(isinstance(label, str) and label.strip(), "Enrollment label is required")
        manifest = load_opencode_manifest(manifest_path)
        target = {
            "harness": "opencode",
            "session": session_identity(manifest["session_id"], "opencode"),
            "surface": identity(manifest["cmux_surface_uuid"]),
            "workspace": identity(manifest["cmux_workspace_uuid"]),
            "api_url": manifest["api_url"],
            "password_file": manifest["password_file"],
            "expected_server_pid": manifest["expected_server_pid"],
            "session_directory": manifest["session_directory"],
            "worktree_directory": manifest["worktree_directory"],
            "parent_task_id": manifest["parent_task_id"],
            "delivery_policy": manifest["delivery_policy"],
            "label": label.strip(),
        }
        require(os.environ.get("CMUX_AGENT_LAUNCH_KIND", "").strip().casefold() == "opencode",
                "OpenCode enrollment must run from an OpenCode TUI-launched command")
        require(os.environ.get("CMUX_SURFACE_ID", "").upper() == target["surface"],
                "CMUX_SURFACE_ID does not match the OpenCode manifest")
        require(os.environ.get("CMUX_WORKSPACE_ID", "").upper() == target["workspace"],
                "CMUX_WORKSPACE_ID does not match the OpenCode manifest")
        caller_session = os.environ.get("OPENCODE_SESSION_ID")
        require(isinstance(caller_session, str) and caller_session,
                "OpenCode enrollment requires the exact OPENCODE_SESSION_ID")
        require(session_identity(caller_session, "opencode") == target["session"],
                "OPENCODE_SESSION_ID does not match the OpenCode manifest")
        hook_path = self.cmux.hook_directory / "opencode-hook-sessions.json"
        try:
            hook_raw = _read_private_file(str(hook_path), "OpenCode hook store", 2 * 1024 * 1024)
            store = json.loads(hook_raw.decode("utf-8"))
            record = store["sessions"][target["session"]]
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            raise ValueError("Exact OpenCode session needs a current native TUI hook record") from None
        require(isinstance(record, dict), "OpenCode native hook record is malformed")
        record_surface = record.get("surfaceId")
        record_workspace = record.get("workspaceId")
        require(isinstance(record_surface, str) and record_surface.upper() == target["surface"] and
                isinstance(record_workspace, str) and record_workspace.upper() == target["workspace"],
                "OpenCode native hook surface or workspace differs from the manifest")
        started = record.get("startedAt")
        require(type(started) in (int, float) and math.isfinite(started) and started >= 0,
                "OpenCode native session generation is unavailable")
        target["started_at"] = started
        target["cutoff"] = started if include_existing else self.clock()
        self.cmux.enrollment_binding(target)
        config = self.config()
        existing = next((item for item in config["targets"] if item["session"] == target["session"]), None)
        expected = {**target, "cutoff": existing["cutoff"]} if existing else target
        existing_binding = ({**existing, "delivery_policy": opencode_delivery_policy(existing)}
                            if existing else None)
        require(existing is None or existing_binding == expected,
                "OpenCode enrollment differs; unenroll explicitly before changing its binding")
        require(not any(item["surface"] == target["surface"] and
                        item["session"] != target["session"] for item in config["targets"]),
                "CMUX surface has another enrolled session")
        if existing is None:
            config["targets"].append(target)
        atomic_json(self.directory / "config.json", config)
        return {"enrolled": existing or target, "self_enrollment": True,
                "identity_source": "private_manifest_and_native_opencode_hook"}

    def attention_once(self, session_id, event_key, body, surface_id):
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", event_key) is None:
            return self.attention_result("rejected", False,
                                         "Event key must be 1-128 ASCII letters, digits, dots, underscores, colons or hyphens",
                                         event_key)
        config = self.config()
        session = session_identity(session_id)
        target = next((item for item in config["targets"] if item["session"] == session), None)
        if target is None:
            return self.attention_result("unavailable", False, "Session is not enrolled", event_key)
        if not surface_id:
            return self.attention_result("unavailable", False, "CMUX_SURFACE_ID is required", event_key)
        try:
            surface = identity(surface_id)
        except ValueError:
            return self.attention_result("unavailable", False, "CMUX_SURFACE_ID is invalid", event_key)
        if surface != target["surface"]:
            return self.attention_result("unavailable", False,
                                         "CMUX_SURFACE_ID does not match the enrolled surface", event_key)
        if target["harness"] == "opencode":
            workspace_value = os.environ.get("CMUX_WORKSPACE_ID")
            try:
                workspace = identity(workspace_value) if workspace_value else None
            except ValueError:
                workspace = None
            if workspace != target["workspace"]:
                return self.attention_result("unavailable", False,
                                             "CMUX_WORKSPACE_ID does not match the enrolled OpenCode workspace",
                                             event_key)
        launch_kind = os.environ.get("CMUX_AGENT_LAUNCH_KIND", "").strip().casefold()
        # Native Claude team surfaces identify their launcher as "claudeTeams".
        launch_kind = {"claudeteams": "claude"}.get(launch_kind, launch_kind)
        if target["harness"] == "opencode" and launch_kind != "opencode":
            return self.attention_result("unavailable", False,
                                         "OpenCode attention must run from the exact OpenCode TUI", event_key)
        if launch_kind and launch_kind != target["harness"]:
            return self.attention_result("unavailable", False,
                                         "Caller launch kind does not match the enrolled session", event_key)
        session_env = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID",
                       "opencode": "OPENCODE_SESSION_ID"}[target["harness"]]
        caller_session = os.environ.get(session_env)
        if target["harness"] == "opencode" and not caller_session:
            return self.attention_result("unavailable", False,
                                         "OpenCode attention requires the exact OPENCODE_SESSION_ID", event_key)
        if caller_session:
            try:
                caller_session = session_identity(caller_session, target["harness"])
            except ValueError:
                return self.attention_result("unavailable", False,
                                             f"Caller {session_env} is invalid", event_key)
            if caller_session != session:
                return self.attention_result("unavailable", False,
                                             "Caller session does not match the enrolled session", event_key)

        body_sha256 = hashlib.sha256(body).hexdigest()
        previous = self.prior_attention_intent(target, event_key)
        if previous is not None:
            saved_hash, outcome, attempted, detail = previous
            if saved_hash != body_sha256:
                return self.attention_result("rejected", False,
                                             "Event key was already used with a different message", event_key)
            if target["harness"] != "opencode" or outcome != "held" or attempted:
                return self.attention_result(outcome, attempted, detail, event_key)

        try:
            self.cmux.binding(target)
        except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError, AttributeError) as error:
            if target["harness"] == "opencode":
                now = self.clock()
                detail = f"{error}; no OpenCode prompt sent and no attempt consumed"
                with contextlib.closing(self.database()) as db, db:
                    db.execute("INSERT INTO attention_intents VALUES (?,?,?,?,?,?,?,?) "
                               "ON CONFLICT(session,started_at,event_key) DO UPDATE SET "
                               "outcome='held',attempted=0,at=excluded.at,detail=excluded.detail",
                               (session, target["started_at"], event_key, body_sha256,
                                "held", 0, now, detail[:500]))
                return self.attention_result("held", False, detail, event_key)
            return self.attention_result("held", False, f"{error}; no intent recorded", event_key)

        message = body.decode("utf-8")
        now = self.clock()
        with contextlib.closing(self.database()) as db:
            with db:
                if target["harness"] == "opencode":
                    db.execute("INSERT INTO attention_intents VALUES (?,?,?,?,?,?,?,?) "
                               "ON CONFLICT(session,started_at,event_key) DO UPDATE SET "
                               "outcome='uncertain',attempted=0,at=excluded.at,detail=excluded.detail",
                               (session, target["started_at"], event_key, body_sha256,
                                "uncertain", 0, now, "Intent committed before OpenCode API request"))
                else:
                    db.execute("INSERT INTO attention_intents VALUES (?,?,?,?,?,?,?,?)",
                               (session, target["started_at"], event_key, body_sha256,
                                "uncertain", 0, now, "Intent committed before submit"))

            attempted = False

            def mark_transport_start():
                nonlocal attempted
                with db:
                    db.execute("UPDATE attention_intents SET attempted=1 WHERE session=? AND started_at=? AND event_key=?",
                               (session, target["started_at"], event_key))
                attempted = True

            try:
                self.cmux.submit(target, message, on_transport_start=mark_transport_start)
            except DeliveryHoldError as error:
                if target["harness"] == "opencode":
                    outcome, detail, attempted = "held", f"{error}; no OpenCode prompt sent and no attempt consumed", False
                else:
                    outcome, detail, attempted = "held", f"{error}; event key is reserved and will not be retried", False
            except Exception:
                outcome = "uncertain"
                detail = ("OpenCode API response was not confirmed; delivery is uncertain"
                          if target["harness"] == "opencode" else
                          "CMUX submit was not confirmed; delivery is uncertain")
            else:
                if target["harness"] == "opencode":
                    outcome, detail, attempted = "submitted", (
                        "OpenCode API confirmed exact user-message admission; model receipt is unconfirmed"), True
                else:
                    outcome, detail, attempted = "submitted", "CMUX confirmed submit", True
            with db:
                db.execute("UPDATE attention_intents SET outcome=?,attempted=?,detail=? "
                           "WHERE session=? AND started_at=? AND event_key=?",
                           (outcome, int(attempted), detail[:500], session, target["started_at"], event_key))
        return self.attention_result(outcome, attempted, detail, event_key)

    def source_messages(self, target, messages):
        sources = []
        for message in messages:
            if str(message.get("recipient_surface_id") or "").upper() != target["surface"] or \
                    str(message.get("recipient_workspace_id") or "").upper() != target["workspace"]:
                continue
            # Never treat our own reminders or outbound messages as an inbound request.
            if message.get("sender_name") == "hailing-frequencies" or \
                    str(message.get("sender_surface_id") or "").upper() == target["surface"]:
                continue
            identity(message["id"])
            created = message.get("created_at")
            require(type(created) in (int, float) and math.isfinite(created), "Malformed message timestamp")
            if created < max(target["started_at"], target["cutoff"]):
                continue
            sources.append(message)
        return sources

    def enrolled_target(self, session_id, surface_id):
        config = self.config()
        session = session_identity(session_id)
        require(surface_id, "CMUX_SURFACE_ID is required")
        surface = identity(surface_id)
        target = next((item for item in config["targets"] if item["session"] == session), None)
        require(target is not None, "Session is not enrolled")
        require(surface == target["surface"], "CMUX_SURFACE_ID does not match the enrolled surface")
        return target

    def select_inbox_messages(self, target, message_ids, rows):
        by_id = {}
        for message in self.source_messages(target, rows):
            by_id.setdefault(identity(message["id"]), []).append(message)
        selected = []
        for message_id in message_ids:
            matches = by_id.get(message_id, [])
            require(len(matches) == 1, "Message is missing, ambiguous, or outside this session's enrollment")
            message = matches[0]
            require(isinstance(message.get("body"), str), "Message body is unavailable")
            require(isinstance(message.get("sender_name"), str), "Message sender name is unavailable")
            require(isinstance(message.get("sender_surface_id"), str), "Message sender surface is unavailable")
            require(isinstance(message.get("sender_workspace_id"), str), "Message sender workspace is unavailable")
            identity(message.get("sender_surface_id"))
            identity(message.get("sender_workspace_id"))
            require(message.get("in_reply_to") is None or isinstance(message.get("in_reply_to"), str),
                    "Message reply reference is malformed")
            if message.get("in_reply_to") is not None:
                identity(message["in_reply_to"])
            selected.append(message)
        return selected

    def inbox_command(self, target, message_ids):
        return self._inbox_command(target, message_ids, self.installed_launcher())

    def _inbox_command(self, target, message_ids, launcher):
        if launcher is not None:
            command = [str(launcher), "--state-dir", str(self.directory), "inbox", "--session", target["session"]]
        else:
            command = [sys.executable, str(Path(__file__).resolve()),
                       "--state-dir", str(self.directory), "--cmux", self.cmux.executable,
                       "inbox", "--session", target["session"]]
        for message_id in message_ids:
            command.extend(("--message", identity(message_id)))
        return shlex.join(command)

    def installed_launcher(self):
        if self.directory != DEFAULT_STATE.expanduser().absolute() or self.cmux.executable != "cmux":
            return None
        home = Path.home()
        runtime_root = home / ".local/share/starfleet-ai/hailing-frequencies"
        script = Path(__file__).resolve()
        try:
            if script.parent.parent != runtime_root.resolve():
                return None
            digest_input = b"".join(name.encode() + b"\0" + (script.parent / name).read_bytes()
                                    for name in RUNTIME_SOURCE_NAMES)
            for skill_name in RUNTIME_SKILL_NAMES:
                skill = script.parent / skill_name
                if skill.is_file():
                    digest_input += skill_name.encode() + b"\0" + skill.read_bytes()
            digest = hashlib.sha256(digest_input).hexdigest()
            metadata = json.loads(script.with_name("installation.json").read_text())
            if script.parent.name != digest or metadata.get("sha256") != digest:
                return None
            python = metadata.get("python")
            if not isinstance(python, str) or not python:
                return None
            launcher = home / ".local/bin/hailing-frequencies"
            if launcher.is_symlink() or not launcher.is_file() or not os.access(launcher, os.X_OK):
                return None
            expected = ("#!/bin/sh\n" + INSTALL_MARKER + "\nexec " + shlex.quote(python) + " " +
                        shlex.quote(str(script)) + ' "$@"\n')
            return launcher if launcher.read_text() == expected else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def compact_inbox_messages(self, selected):
        return [{
            "id": identity(message["id"]),
            "sender_name": message["sender_name"],
            "sender_surface_id": identity(message["sender_surface_id"]),
            "sender_workspace_id": identity(message["sender_workspace_id"]),
            "created_at": message["created_at"],
            "in_reply_to": identity(message["in_reply_to"]) if message.get("in_reply_to") else None,
            "body": message["body"],
        } for message in selected]

    def inbox_commands(self, target, message_ids):
        launcher = self.installed_launcher()
        batches = (message_ids[offset:offset + INBOX_MESSAGE_LIMIT]
                   for offset in range(0, len(message_ids), INBOX_MESSAGE_LIMIT))
        return "\n".join(self._inbox_command(target, batch, launcher) for batch in batches)

    def _inbox_binding(self, target):
        if target.get("harness") == "opencode":
            # A recipient tool is already running while reading its own inbox.
            return self.cmux.enrollment_binding(target)
        return self.cmux.binding(target)

    def consume_inbox(self, session_id, message_ids, surface_id, output, details=False):
        require(isinstance(message_ids, list) and 0 < len(message_ids) <= INBOX_MESSAGE_LIMIT,
                f"Select between 1 and {INBOX_MESSAGE_LIMIT} message IDs")
        normalized_ids = [identity(message_id) for message_id in message_ids]
        require(len(set(normalized_ids)) == len(normalized_ids), "Duplicate message IDs are not allowed")
        target = self.enrolled_target(session_id, surface_id)
        self._inbox_binding(target)
        rows = self.cmux.messages(target)
        require(len(rows) < SCAN_LIMIT, "Inbox scan saturated; no messages output or acknowledged")
        selected = self.select_inbox_messages(target, normalized_ids, rows)
        rendered = selected if details else self.compact_inbox_messages(selected)
        payload = json.dumps({"messages": rendered}, indent=2, allow_nan=False) + "\n"
        require(len(payload.encode("utf-8")) <= INBOX_OUTPUT_LIMIT,
                "Inbox output exceeds 64 KiB; select a smaller batch. No messages acknowledged")
        written = output.write(payload)
        require(written == len(payload), "Inbox output was incomplete; no messages acknowledged")
        output.flush()

        current_target = self.enrolled_target(session_id, surface_id)
        require(current_target == target, "Enrollment changed; no messages acknowledged")
        self._inbox_binding(current_target)
        current_rows = self.cmux.messages(current_target)
        require(len(current_rows) < SCAN_LIMIT, "Inbox scan saturated; no messages acknowledged")
        current = self.select_inbox_messages(current_target, normalized_ids, current_rows)
        for previous, latest in zip(selected, current):
            stable_fields = ("id", "body", "sender_surface_id", "sender_workspace_id", "sender_name",
                             "recipient_surface_id", "recipient_workspace_id", "created_at", "in_reply_to")
            require(all(previous.get(field) == latest.get(field) for field in stable_fields),
                    "Message changed during output; no messages acknowledged")
        self._inbox_binding(current_target)

        acknowledged_at = self.clock()
        with contextlib.closing(self.database()) as db:
            with db:
                for message_id in normalized_ids:
                    db.execute("INSERT INTO acknowledgements VALUES (?,?,?,?) "
                               "ON CONFLICT(session,message) DO UPDATE SET at=excluded.at,evidence='inbox'",
                               (target["session"], message_id, acknowledged_at, "inbox"))
        return {"session": target["session"], "messages": normalized_ids, "acknowledged": True,
                "evidence": "inbox", "output_bytes": len(payload.encode("utf-8"))}

    def reply_acknowledgements(self, target, sources, messages):
        by_reference = {}
        for message in messages:
            try:
                reference = identity(message.get("in_reply_to"))
            except (ValueError, TypeError, AttributeError):
                continue
            by_reference.setdefault(reference, []).append(message)

        acknowledged = set()
        for source in sources:
            source_id = identity(source["id"])
            sender_surface = str(source.get("sender_surface_id") or "").upper()
            sender_workspace = str(source.get("sender_workspace_id") or "").upper()
            if not sender_surface or not sender_workspace:
                continue
            for reply in by_reference.get(source_id, []):
                created = reply.get("created_at")
                if type(created) not in (int, float) or not math.isfinite(created) or \
                        created <= source["created_at"] or created < target["started_at"]:
                    continue
                if str(reply.get("sender_surface_id") or "").upper() != target["surface"] or \
                        str(reply.get("sender_workspace_id") or "").upper() != target["workspace"] or \
                        str(reply.get("recipient_surface_id") or "").upper() != sender_surface or \
                        str(reply.get("recipient_workspace_id") or "").upper() != sender_workspace:
                    continue
                acknowledged.add(source_id)
                break
        return acknowledged

    def record_acknowledgements(self, db, target, message_ids, evidence, now):
        with db:
            for message_id in message_ids:
                db.execute("INSERT OR IGNORE INTO acknowledgements VALUES (?,?,?,?)",
                           (target["session"], message_id, now, evidence))

    def record_explicit_acknowledgement(self, db, target, message_id, now):
        with db:
            db.execute("INSERT INTO acknowledgements VALUES (?,?,?,?) "
                       "ON CONFLICT(session,message) DO UPDATE SET at=excluded.at,evidence='explicit'",
                       (target["session"], message_id, now, "explicit"))

    def target_snapshot(self, sources, acknowledged, now, remind_after):
        native_unread = [message for message in sources if
                         message.get("state") in ("queued", "delivered") and message.get("read_at") is None]
        acknowledged_ids = {identity(message["id"]) for message in sources
                            if identity(message["id"]) in acknowledged}
        overdue = [message for message in native_unread if identity(message["id"]) not in acknowledged and
                   now - message["created_at"] >= remind_after]
        return {
            "native_unread": len(native_unread),
            "acknowledged": len(acknowledged_ids),
            "overdue": len(overdue),
            "oldest_seconds": round(max((now - message["created_at"] for message in overdue), default=0)),
            "overdue_messages": overdue,
            "acknowledged_ids": acknowledged_ids,
        }

    def target_messages(self, target):
        try:
            rows = self.cmux.messages(target)
        except InboxScanSaturatedError:
            raise
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise TargetInboxError(str(exc)[:500]) from None
        if len(rows) >= SCAN_LIMIT:
            raise InboxScanSaturatedError("Inbox scan saturated")
        return rows

    def acknowledgement_command(self, target, message_id):
        return shlex.join([sys.executable, str(Path(__file__).resolve()),
                           "--state-dir", str(self.directory), "--cmux", self.cmux.executable,
                           "ack", "--session", target["session"], "--message", identity(message_id)])

    def acknowledge(self, session_id, message_id, surface_id):
        config = self.config()
        session = session_identity(session_id)
        source_id = identity(message_id)
        require(surface_id, "CMUX_SURFACE_ID is required")
        surface = identity(surface_id)
        target = next((item for item in config["targets"] if item["session"] == session), None)
        require(target is not None, "Session is not enrolled")
        require(surface == target["surface"], "CMUX_SURFACE_ID does not match the enrolled surface")
        self.cmux.binding(target)
        rows = self.cmux.messages(target)
        require(len(rows) < SCAN_LIMIT, "Inbox scan saturated; no acknowledgment recorded")
        sources = self.source_messages(target, rows)
        matches = [message for message in sources if identity(message["id"]) == source_id]
        require(len(matches) == 1, "Message is missing, ambiguous, or outside this session's enrollment")
        self.cmux.binding(target)
        with contextlib.closing(self.database()) as db:
            self.record_explicit_acknowledgement(db, target, source_id, self.clock())
        return {"session": session, "message": source_id, "acknowledged": True,
                "task_completed": False, "evidence": "explicit"}

    def reserve(self, db, target, messages, kind, now):
        token = uuid.uuid4().hex
        with db:
            for message in messages:
                db.execute("INSERT INTO attempts VALUES (?,?,?,?,?)",
                           (target["session"], message["id"], kind, now, "uncertain"))
            db.execute("INSERT INTO batches VALUES (?,?,?,?,?)", (token, target["session"], now, "uncertain", kind))
        return token

    def deliver(self, db, target, messages, kind, destination, now, remind_after):
        # Recheck source and reply evidence after planning; the recipient may have
        # acknowledged, read, or answered the message while the scan was running.
        current_rows = self.target_messages(target)
        current_sources = self.source_messages(target, current_rows)
        reply_ids = self.reply_acknowledgements(target, current_sources, current_rows)
        if reply_ids:
            try:
                self.cmux.binding(target)
            except (ValueError, OSError, subprocess.SubprocessError):
                reply_ids = set()
        self.record_acknowledgements(db, target, reply_ids, "reply", now)
        acknowledged = {row[0] for row in db.execute(
            "SELECT message FROM acknowledgements WHERE session=?", (target["session"],))}
        snapshot = self.target_snapshot(current_sources, acknowledged, now, remind_after)
        current = {identity(m["id"]) for m in current_sources if
                   m.get("state") in ("queued", "delivered") and m.get("read_at") is None and
                   identity(m["id"]) not in acknowledged}
        planned_ids = {identity(m["id"]) for m in messages}
        messages = [m for m in messages if identity(m["id"]) in current]
        if not messages:
            outcome = "already_acknowledged" if planned_ids & acknowledged else "already_read"
            return {"kind": kind, "outcome": outcome, "ids": [], "_snapshot": snapshot}
        token = self.reserve(db, target, messages, kind, now)
        ids = [m["id"] for m in messages]
        if kind == "escalation":
            text = (f"Hailing Frequencies escalation [{token}]: recipient {target['session']}, overdue IDs "
                    f"{', '.join(ids)}. Investigate source metadata and current binding, then contact the recipient. "
                    "Do not read or consume messages as the recipient.")
        else:
            text = ("CMUX still records these messages as unread. Review them at your next safe checkpoint; "
                    "acknowledgment resolves only this queue alert, not task completion or authority.\n"
                    f"{self.inbox_commands(target, ids)}")
        try:
            self.cmux.submit(destination, text)
            outcome, detail = "submitted", kind
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            # Even a timeout may have delivered. Retain the intent; never automatically replay.
            outcome, detail = "uncertain", str(exc)[:500]
        with db:
            db.execute("UPDATE batches SET outcome=?,detail=? WHERE token=?", (outcome, detail, token))
            for mid in ids:
                db.execute("UPDATE attempts SET outcome=? WHERE session=? AND message=? AND kind=?",
                           (outcome, target["session"], mid, kind))
        return {"token": token, "kind": kind, "outcome": outcome, "detail": detail,
                "ids": ids, "_snapshot": snapshot}

    def scan(self, send=False, only_sessions=None):
        now = self.clock()
        config = self.config()
        if only_sessions is not None:
            only_sessions = {session_identity(session) for session in only_sessions}
            require(only_sessions <= {t["session"] for t in config["targets"]},
                    "Send scope contains a session that is not enrolled")
        messages_by_id = {}
        rows_by_session = {}
        initial_errors_by_session = {}
        target_errors = []
        for target in config["targets"]:
            try:
                rows = self.target_messages(target)
            except TargetInboxError as exc:
                error = {"label": target["label"], "session": target["session"],
                         "stage": "initial_scan", "error": str(exc)[:500]}
                initial_errors_by_session[target["session"]] = error
                target_errors.append(error)
                continue
            rows_by_session[target["session"]] = rows
            messages_by_id.update((row["id"], row) for row in rows)
        messages = list(messages_by_id.values())
        results = []
        with contextlib.closing(self.database()) as db:
            for target in config["targets"]:
                sending = send and (only_sessions is None or target["session"] in only_sessions)
                initial_error = initial_errors_by_session.get(target["session"])
                if initial_error:
                    results.append({"label": target["label"], "session": target["session"],
                                    "inbox_status": "incomplete", "error": initial_error["error"],
                                    "sending_enabled": sending, "attempts": []})
                    continue
                rows = rows_by_session[target["session"]]
                sources = self.source_messages(target, rows)
                reply_ids = self.reply_acknowledgements(target, sources, messages)
                binding_checked = False
                reply_hold = None
                if reply_ids:
                    binding_checked = True
                    try:
                        self.cmux.binding(target)
                    except (ValueError, OSError, subprocess.SubprocessError) as exc:
                        reply_hold = str(exc)
                        reply_ids = set()
                self.record_acknowledgements(db, target, reply_ids, "reply", now)
                acknowledged = {row[0] for row in db.execute(
                    "SELECT message FROM acknowledgements WHERE session=?", (target["session"],))}
                snapshot = self.target_snapshot(sources, acknowledged, now, config["remind_after"])
                overdue = snapshot["overdue_messages"]
                result = {"label": target["label"], "session": target["session"],
                          "inbox_status": "complete",
                          "native_unread": snapshot["native_unread"],
                          "acknowledged": snapshot["acknowledged"],
                          "overdue": snapshot["overdue"],
                          "sending_enabled": sending,
                          "oldest_seconds": snapshot["oldest_seconds"], "attempts": []}
                if reply_hold:
                    result["acknowledgment_hold"] = reply_hold
                results.append(result)
                if not overdue:
                    continue
                if reply_hold:
                    result["hold"] = reply_hold
                    continue
                if not binding_checked:
                    try:
                        self.cmux.binding(target)
                    except (ValueError, OSError, subprocess.SubprocessError) as exc:
                        result["hold"] = str(exc)
                        continue
                for kind, age in (("reminder", config["remind_after"]), ("escalation", config["escalate_after"])):
                    eligible = [m for m in overdue if now - m["created_at"] >= age and not db.execute(
                        "SELECT 1 FROM attempts WHERE session=? AND message=? AND kind=?",
                        (target["session"], m["id"], kind)).fetchone()]
                    if not eligible:
                        continue
                    last = db.execute("SELECT MAX(at) FROM attempts WHERE session=? AND kind=?",
                                      (target["session"], kind)).fetchone()[0]
                    if last is not None and now - last < config["cooldown"]:
                        result[kind + "_hold"] = "cooldown"
                        continue
                    destination = target
                    if kind == "escalation":
                        destination = next((t for t in config["targets"] if t["session"] == config.get("coordinator")), None)
                        if destination is None or destination["session"] == target["session"]:
                            result["escalation_hold"] = "No distinct enrolled coordinator; see this local report"
                            continue
                        if only_sessions is not None and destination["session"] not in only_sessions:
                            result["escalation_hold"] = "Coordinator is outside the active send scope"
                            continue
                        try:
                            self.cmux.binding(destination)
                        except (ValueError, OSError, subprocess.SubprocessError) as exc:
                            result["escalation_hold"] = str(exc)
                            continue
                    if sending:
                        try:
                            attempt = self.deliver(db, target, eligible, kind, destination, now,
                                                   config["remind_after"])
                        except TargetInboxError as exc:
                            detail = str(exc)[:500]
                            target_errors.append({"label": target["label"], "session": target["session"],
                                                  "stage": "delivery_recheck", "error": detail})
                            result.update(inbox_status="incomplete", inbox_error=detail)
                            result["attempts"].append({"kind": kind, "outcome": "held", "detail": detail})
                            break
                        fresh = attempt.pop("_snapshot")
                        result.update({key: fresh[key] for key in
                                       ("native_unread", "acknowledged", "overdue", "oldest_seconds")})
                        overdue = fresh["overdue_messages"]
                        result["attempts"].append(attempt)
                    else:
                        result["attempts"].append({"kind": kind, "outcome": "would_submit", "ids": [m["id"] for m in eligible]})
        report = {"at": now, "completed_at": self.clock(), "mode": "send" if send else "observe",
                  "sending_sessions": sorted(only_sessions) if only_sessions is not None else None,
                  "coverage_complete": not target_errors, "target_errors": target_errors, "targets": results}
        atomic_json(self.directory / "last-scan.json", report)
        return report


def serve_iteration(app, send=False, only_sessions=None):
    try:
        with app.locked():
            report = app.scan(send, only_sessions)
    except LockBusyError:
        report = {"at": app.clock(), "skipped": "lock_busy"}
    except (ValueError, OSError, subprocess.SubprocessError, sqlite3.Error) as exc:
        error = {"at": app.clock(), "error": str(exc)[:500], "sending_paused": True}
        atomic_json(app.directory / "last-error.json", error)
        report = error
    try:
        from temporal_beacon import TemporalBeacon
        report["temporal_beacon"] = TemporalBeacon(app, clock=app.clock).tick(send, only_sessions)
        (app.directory / "timer-last-error.json").unlink(missing_ok=True)
    except (ValueError, OSError, subprocess.SubprocessError, sqlite3.Error) as exc:
        error = {"at": app.clock(), "error": str(exc)[:500], "sending_paused": True}
        atomic_json(app.directory / "timer-last-error.json", error)
        report["temporal_beacon"] = error
    report["at"] = app.clock()
    return report


def _read_diagnostic_bytes(path, maximum=STARFLEET_DIAGNOSTIC_LIMIT):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("diagnostic file is unavailable") from None
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode), "diagnostic file is not regular")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            value = stream.read(maximum + 1)
        require(len(value) <= maximum, "diagnostic file exceeds its size limit")
        return value
    except OSError:
        raise ValueError("diagnostic file is unreadable") from None
    finally:
        os.close(descriptor)


def _read_diagnostic_json(path, maximum=STARFLEET_DIAGNOSTIC_LIMIT):
    raw = _read_diagnostic_bytes(path, maximum)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise ValueError("diagnostic JSON is malformed") from None
    require(isinstance(value, dict), "diagnostic JSON must be an object")
    return value


def read_starfleet_caller(stream):
    raw = stream.read(16 * 1024 + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes) or len(raw) > 16 * 1024:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    if (not isinstance(value, dict) or set(value) != {"schema_version", "caller"}
            or value.get("schema_version") != 1 or not isinstance(value.get("caller"), dict)
            or set(value["caller"]) != {"harness", "session", "workspace", "surface"}):
        return None
    caller = value["caller"]
    if (caller.get("harness") not in ("claude", "codex", "muse", "opencode")
            or any(not isinstance(caller.get(key), str) or not caller[key]
                   for key in ("session", "workspace", "surface"))):
        return None
    return caller


def _worker_heartbeat(directory, config, name="last-scan.json", error_name="last-error.json"):
    try:
        scan = _read_diagnostic_json(directory / name)
        if scan is None:
            return "missing", None
        heartbeat = scan.get("completed_at", scan.get("at"))
        require(type(heartbeat) in (int, float) and math.isfinite(heartbeat),
                "invalid heartbeat timestamp")
        age = time.time() - heartbeat
        threshold = max(120, config["interval"] * 4)
        require(math.isfinite(threshold), "invalid heartbeat threshold")
        require(0 <= age <= threshold, "worker heartbeat is stale")
        target_errors = scan.get("target_errors", [])
        require(isinstance(target_errors, list) and not target_errors, "worker target coverage is incomplete")
        require(scan.get("coverage_complete", True) is True, "worker coverage is incomplete")
        error = _read_diagnostic_json(directory / error_name)
        if error is not None:
            error_at = error.get("at")
            require(type(error_at) in (int, float) and math.isfinite(error_at), "invalid worker error time")
            require(error_at < heartbeat, "latest worker scan reports an error")
        return "ready", scan
    except (OSError, ValueError, KeyError, TypeError):
        return "blocked", None


def _match_diagnostic_target(config, caller):
    if not isinstance(caller, dict):
        return None
    try:
        session = session_identity(caller["session"], caller["harness"])
        workspace = identity(caller["workspace"])
        surface = identity(caller["surface"])
    except (KeyError, ValueError, TypeError):
        return None
    matches = [target for target in config.get("targets", [])
               if target.get("harness") == caller["harness"] and target.get("session") == session
               and target.get("workspace") == workspace and target.get("surface") == surface]
    return matches[0] if len(matches) == 1 else None


def _common_diagnostic_report(config_state, registration_state, worker_state,
                              delivery_state, observed_state="unknown"):
    return {"schema_version": 1, "checks": {
        "configuration": config_state, "registration": registration_state,
        "worker": worker_state, "intended_delivery": delivery_state,
        "observed_delivery": observed_state,
    }}


def hailing_diagnostic_report(app, caller):
    config_path = app.directory / "config.json"
    if app.directory.is_symlink():
        return _common_diagnostic_report("blocked", "unknown", "unknown", "unknown")
    try:
        raw = _read_diagnostic_json(config_path)
        if raw is None:
            return _common_diagnostic_report("missing", "missing" if caller else "unknown",
                                             "missing", "missing" if caller else "unknown")
        config = app.config(raw)
    except (OSError, ValueError, KeyError, TypeError):
        return _common_diagnostic_report("blocked", "unknown", "unknown", "unknown")
    target = _match_diagnostic_target(config, caller)
    registration = "unknown" if caller is None else "ready" if target else "missing"
    worker, scan = _worker_heartbeat(app.directory, config)
    delivery = "unknown"
    if caller is not None:
        if target is None:
            delivery = "missing"
        elif scan is not None and worker == "ready":
            scope = scan.get("sending_sessions")
            target_rows = scan.get("targets")
            row = next((item for item in target_rows if isinstance(item, dict)
                        and item.get("session") == session_identity(caller["session"], caller["harness"])),
                       None) if isinstance(target_rows, list) else None
            if (scan.get("mode") == "send" and (scope is None or target["session"] in scope)
                    and isinstance(row, dict) and row.get("sending_enabled") is True):
                delivery = "ready"
            else:
                delivery = "blocked"
    return _common_diagnostic_report("ready", registration, worker, delivery)


def temporal_diagnostic_report(app, caller):
    report = hailing_diagnostic_report(app, caller)
    checks = report["checks"]
    if checks["configuration"] != "ready":
        return report
    try:
        config = app.config(_read_diagnostic_json(app.directory / "config.json"))
        worker, scan = _worker_heartbeat(app.directory, config, "timer-last-scan.json",
                                         "timer-last-error.json")
        checks["worker"] = worker
        if caller is not None and checks["registration"] == "ready":
            target = _match_diagnostic_target(config, caller)
            hailing_scan_state, hailing_scan = _worker_heartbeat(app.directory, config)
            if hailing_scan_state == "ready" and isinstance(hailing_scan, dict):
                scope = hailing_scan.get("sending_sessions")
                checks["intended_delivery"] = (
                    "ready" if hailing_scan.get("mode") == "send" and
                    target is not None and (scope is None or target["session"] in scope) and
                    isinstance(_match_scan_target(hailing_scan, target["session"]), dict) else "blocked")
            else:
                checks["intended_delivery"] = "unknown"
    except (OSError, ValueError, KeyError, TypeError):
        checks["worker"] = "blocked"
        checks["intended_delivery"] = "unknown"
    return report


def _match_scan_target(scan, session):
    targets = scan.get("targets")
    return next((item for item in targets if isinstance(item, dict)
                 and item.get("session") == session and item.get("sending_enabled") is True), None) \
        if isinstance(targets, list) else None


def _parse_static_shell_literal(raw):
    require(isinstance(raw, str) and not any(ord(character) < 32 or ord(character) == 127
                                             for character in raw),
            "Agent Notify setting is not a supported literal")
    value = []
    quote = None
    index = 0
    while index < len(raw):
        character = raw[index]
        if quote == "'":
            if character == "'":
                quote = None
            else:
                value.append(character)
        elif quote == '"':
            if character == '"':
                quote = None
            elif character == "\\":
                index += 1
                require(index < len(raw), "Agent Notify setting has an incomplete escape")
                escaped = raw[index]
                if escaped in ("$", "`", '"', "\\"):
                    value.append(escaped)
                else:
                    value.extend(("\\", escaped))
            elif character in ("$", "`"):
                raise ValueError("Agent Notify setting contains unsupported shell expansion")
            else:
                value.append(character)
        elif character in ("'", '"'):
            quote = character
        elif character == "\\":
            index += 1
            require(index < len(raw), "Agent Notify setting has an incomplete escape")
            value.append(raw[index])
        elif character in ("$", "`", ";", "|", "&", "<", ">", "(", ")"):
            raise ValueError("Agent Notify setting contains unsupported shell syntax")
        elif character.isspace():
            raise ValueError("Agent Notify setting is not one static shell word")
        else:
            value.append(character)
        index += 1
    require(quote is None, "Agent Notify setting is not a supported literal")
    return "".join(value)


def agent_notify_diagnostic_report(notify_home=None, *, require_webhook=True,
                                   require_cached_user=False):
    directory = Path(notify_home or os.environ.get(
        "AGENT_NOTIFY_HOME", Path.home() / ".config/agent-notify")).expanduser()
    path = directory / "env"
    config = "unknown"
    try:
        if directory.is_symlink():
            config = "blocked"
        elif not directory.exists():
            config = "missing"
        elif not directory.is_dir() or stat.S_IMODE(directory.stat().st_mode) & 0o077:
            config = "blocked"
        else:
            raw = _read_diagnostic_bytes(path, 16 * 1024)
            if raw is None:
                config = "missing"
            else:
                info = path.stat(follow_symlinks=False)
                require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                        and stat.S_IMODE(info.st_mode) & 0o077 == 0,
                        "Agent Notify credentials have unsafe ownership or permissions")
                text = raw.decode("utf-8")
                values = {}
                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    match = re.fullmatch(r"(DISCORD_WEBHOOK_URL|DISCORD_BOT_TOKEN|DISCORD_USER_ID)=(.*)", line)
                    require(match is not None, "Agent Notify configuration contains an unsupported line")
                    key, value = match.groups()
                    value = _parse_static_shell_literal(value)
                    require(key not in values, "Agent Notify configuration has a duplicate setting")
                    if key != "DISCORD_USER_ID":
                        require(bool(value), "Agent Notify required configuration is empty")
                    values[key] = value
                webhook = values.get("DISCORD_WEBHOOK_URL", "")
                parsed = urllib.parse.urlsplit(webhook)
                valid_webhook = (not require_webhook or (
                    parsed.scheme == "https" and parsed.netloc == "discord.com"
                    and parsed.path.startswith("/api/webhooks/") and not parsed.query
                    and not parsed.fragment))
                token = values.get("DISCORD_BOT_TOKEN", "")
                valid_token = bool(re.fullmatch(r"[A-Za-z0-9._-]{20,}", token))
                user = values.get("DISCORD_USER_ID", "")
                if not user and require_cached_user:
                    cache_path = directory / "cache.json"
                    cache = _read_diagnostic_json(cache_path, 16 * 1024)
                    if cache is not None:
                        cache_info = cache_path.stat(follow_symlinks=False)
                        require(stat.S_ISREG(cache_info.st_mode) and cache_info.st_uid == os.getuid()
                                and stat.S_IMODE(cache_info.st_mode) & 0o077 == 0,
                                "Agent Notify cache has unsafe ownership or permissions")
                        user = str(cache.get("user", ""))
                valid_user = (bool(re.fullmatch(r"[0-9]{16,22}", user))
                              if require_cached_user else
                              ("DISCORD_USER_ID" not in values or not user or
                               bool(re.fullmatch(r"[0-9]{16,22}", user))))
                config = "ready" if valid_webhook and valid_token and valid_user else "missing"
    except (OSError, ValueError, UnicodeError, TypeError):
        config = "blocked"
    delivery = "unknown" if config == "ready" else config
    return _common_diagnostic_report(config, "not_applicable", "not_applicable", delivery)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    p.add_argument("--cmux", default="cmux")
    sub = p.add_subparsers(dest="command", required=True)
    guide = sub.add_parser("guide", help="Print the maintained safe-setup guide")
    guide.add_argument("--print", action="store_true", help="Print the guide (also the default behavior)")
    init = sub.add_parser("init", help="Create private observe-only configuration")
    init.add_argument("--remind-after", type=float, default=300, help="Seconds (default 300)")
    init.add_argument("--escalate-after", type=float, default=900)
    init.add_argument("--cooldown", type=float, default=600)
    init.add_argument("--interval", type=float, default=30)
    enroll = sub.add_parser("enroll", help="Explicitly enroll one exact currently bound session")
    enroll.add_argument("--surface", required=True)
    enroll.add_argument("--session", required=True)
    enroll.add_argument("--harness", choices=("claude", "codex"), required=True)
    enroll.add_argument("--label", required=True)
    enroll.add_argument("--include-existing", action="store_true", help="Include this session's existing messages")
    enroll.add_argument("--coordinator", action="store_true", help="Receive escalation prompts")
    opencode_enroll = sub.add_parser("opencode-enroll",
                                     help="Self-enroll one exact OpenCode session from a private binding manifest")
    opencode_enroll.add_argument("--manifest", type=Path, required=True)
    opencode_enroll.add_argument("--label", required=True)
    opencode_enroll.add_argument("--include-existing", action="store_true")
    remove = sub.add_parser("unenroll")
    remove.add_argument("--session", required=True)
    for name in ("scan", "serve", "supervise"):
        command = sub.add_parser(name)
        command.add_argument("--send", action="store_true", help="Enable guarded prompt submission; default observe only")
        command.add_argument("--only-session", action="append", help="Limit sends to this enrolled UUID; repeat for canaries")
    sub.add_parser("status")
    doctor = sub.add_parser("doctor", help="Read local setup and existing worker heartbeat without a lock")
    doctor.add_argument("--starfleet-json", action="store_true",
                        help="emit normalized read-only setup states for Starfleet Doctor")
    notify_diagnostic = sub.add_parser("agent-notify-diagnostic", help=argparse.SUPPRESS)
    notify_diagnostic.add_argument("--starfleet-json", action="store_true", required=True)
    sub.add_parser("health")
    watcher = sub.add_parser("health-watch")
    watcher.add_argument("--no-notify", action="store_true")
    service = sub.add_parser("health-service")
    service.add_argument("action", choices=("install", "uninstall", "status"))
    inbox = sub.add_parser("inbox", help="Output exact enrolled messages and acknowledge queue alerts")
    inbox.add_argument("--session", required=True)
    inbox.add_argument("--message", required=True, action="append")
    inbox.add_argument("--details", action="store_true", help="Include the full native message records")
    ack = sub.add_parser("ack", help="Acknowledge your own queue alerts, without completing the task")
    ack.add_argument("--session", required=True)
    ack.add_argument("--message", required=True, action="append")
    attention = sub.add_parser("attention-once", help="Submit one deduplicated prompt to your own enrolled session")
    attention.add_argument("--session", required=True)
    attention.add_argument("--event-key", required=True)
    cleanup = sub.add_parser("discord-cleanup", help="Preview and delete closed Discord forum threads")
    cleanup_commands = cleanup.add_subparsers(dest="cleanup_action", required=True)
    cleanup_guide = cleanup_commands.add_parser("guide", help="Print the cleanup guide without configuration or writes")
    cleanup_guide.add_argument("--print", action="store_true")
    cleanup_plan = cleanup_commands.add_parser("plan", help="Read Discord and save a private deletion preview")
    cleanup_plan.add_argument("--start", required=True, help="Inclusive UTC date or ISO 8601 timestamp")
    cleanup_plan.add_argument("--end", required=True, help="Exclusive UTC date or ISO 8601 timestamp")
    cleanup_plan.add_argument("--output", type=Path, required=True)
    cleanup_apply = cleanup_commands.add_parser("apply", help="Permanently delete the exact reviewed selection")
    cleanup_apply.add_argument("--plan", type=Path, required=True)
    cleanup_apply.add_argument("--confirm", required=True, help="SHA-256 printed by plan")
    for command in (cleanup_plan, cleanup_apply):
        command.add_argument("--notify-home", type=Path,
                             default=Path(os.environ.get("AGENT_NOTIFY_HOME", "~/.config/agent-notify")).expanduser())
    temporal = sub.add_parser("temporal", help="Manage durable exact-session wake timers")
    temporal.add_argument("--state-dir", type=Path, default=argparse.SUPPRESS)
    temporal.add_argument("--cmux", default=argparse.SUPPRESS)
    temporal_sub = temporal.add_subparsers(dest="temporal_command", required=True)
    set_timer = temporal_sub.add_parser("set", help="Schedule one continuation timer")
    schedule = set_timer.add_mutually_exclusive_group(required=True)
    schedule.add_argument("--after")
    schedule.add_argument("--after-idle", help="one continuation after confirmed native inactivity; activity resets the deadline")
    schedule.add_argument("--at")
    schedule.add_argument("--every")
    set_timer.add_argument("--until")
    set_timer.add_argument("--message", required=True, help="Continuation to retrieve when woken")
    set_timer.add_argument("--request-id")
    set_timer.add_argument("--replace-key")
    temporal_sub.add_parser("list", help="List this exact owner's timers")
    inspect = temporal_sub.add_parser("inspect", help="Inspect a timer and its occurrence history")
    inspect.add_argument("timer_id")
    cancel = temporal_sub.add_parser("cancel", help="Cancel a timer and any held occurrence")
    cancel.add_argument("timer_id")
    temporal_sub.add_parser("receive", help="Output the current continuation and record its receipt")
    temporal_sub.choices["receive"].add_argument("--timer-id")
    temporal_doctor = temporal_sub.add_parser("doctor", help="Diagnose timer worker and delivery state")
    temporal_doctor.add_argument("--starfleet-json", action="store_true",
                                 help="emit normalized read-only setup states for Starfleet Doctor")
    return p


def main(argv=None):
    os.umask(0o077)
    if argv is None:
        argv = sys.argv[1:]
        if Path(sys.argv[0]).name == "temporal-beacon":
            argv = ["temporal", *argv]
    args = parser().parse_args(argv)
    if args.command == "guide":
        sys.stdout.write(Path(__file__).with_name("guide.md").read_text(encoding="utf-8"))
        return None
    if args.command == "discord-cleanup":
        if args.cleanup_action == "guide":
            sys.stdout.write(Path(__file__).with_name("discord-cleanup.md").read_text(encoding="utf-8"))
            return None
        import discord_cleanup
        return discord_cleanup.main(args)
    app = Monitor(args.state_dir, Cmux(args.cmux))
    if args.command == "agent-notify-diagnostic":
        return agent_notify_diagnostic_report()
    if args.command == "doctor":
        caller = (read_starfleet_caller(getattr(sys.stdin, "buffer", sys.stdin))
                  if args.starfleet_json and not sys.stdin.isatty() else None)
        return hailing_diagnostic_report(app, caller)
    if args.command == "attention-once":
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        try:
            body = stream.read(ATTENTION_MESSAGE_LIMIT + 1)
        except OSError:
            return app.attention_result("rejected", False, "Could not read the message from stdin", args.event_key)
        if isinstance(body, str):
            body = body.encode("utf-8")
        if not isinstance(body, bytes) or len(body) > ATTENTION_MESSAGE_LIMIT:
            return app.attention_result("rejected", False, "Message must be at most 4096 UTF-8 bytes", args.event_key)
        try:
            message = body.decode("utf-8")
        except UnicodeDecodeError:
            return app.attention_result("rejected", False, "Message must be valid UTF-8", args.event_key)
        if not message.strip() or "\x00" in message:
            return app.attention_result("rejected", False, "Message must be nonempty UTF-8 text", args.event_key)
        config_path = app.directory / "config.json"
        if app.directory.is_symlink() or not app.directory.is_dir() or not config_path.is_file():
            return app.attention_result("unavailable", False, "Hailing Frequencies is not initialized", args.event_key)
        try:
            with app.locked():
                return app.attention_once(args.session, args.event_key, body,
                                          os.environ.get("CMUX_SURFACE_ID"))
        except LockBusyError:
            return app.attention_result("held", False, "Hailing Frequencies state is busy", args.event_key)
    if args.command == "init":
        require(not app.directory.is_symlink(), "Refusing symlink state directory")
        app.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with app.locked():
            require(not (app.directory / "config.json").exists(), "Configuration already exists")
            config = {"version": 1, "targets": [], "coordinator": None,
                      **{k: getattr(args, k) for k in ("remind_after", "escalate_after", "cooldown", "interval")}}
            # Validate without leaving an invalid initialized configuration.
            atomic_json(app.directory / "config.json", config)
            try:
                app.config()
            except ValueError:
                (app.directory / "config.json").unlink()
                raise
        return {"initialized": str(app.directory), "sending_enabled": False}
    if args.command in ("health", "health-watch", "health-service", "supervise"):
        import service
        if args.command == "health":
            return service.health(app.directory)
        if args.command == "health-watch":
            return service.watchdog(app.directory, not args.no_notify)
        if args.command == "health-service":
            return service.health_service(app.directory, args.action)
        app.config()
        return service.supervise(app.directory, args.cmux, args.send, args.only_session)
    if args.command == "status":
        import service
        health_report = service.health(app.directory)
        with app.locked():
            config = app.config()
            last = app.directory / "last-scan.json"
            with contextlib.closing(app.database()) as db:
                attempts = [dict(zip(("token", "session", "at", "outcome", "detail"), row)) for row in
                            db.execute("SELECT * FROM batches ORDER BY at DESC LIMIT 50")]
            return {"config": config, "last_scan": json.loads(last.read_text()) if last.exists() else None,
                    "health": health_report, "attempts": attempts}
    if args.command == "temporal":
        from temporal_beacon import TemporalBeacon
        beacon = TemporalBeacon(app)
        if args.temporal_command == "doctor":
            if args.starfleet_json:
                caller = (None if sys.stdin.isatty() else
                          read_starfleet_caller(getattr(sys.stdin, "buffer", sys.stdin)))
                return temporal_diagnostic_report(app, caller)
            return beacon.doctor()
        if args.temporal_command == "set":
            return beacon.set(args.after, args.at, args.every, args.until, args.message,
                              args.request_id, args.replace_key, after_idle=args.after_idle)
        target, owner_key, _worktree = beacon.owner()
        if args.temporal_command == "list":
            return beacon.list(owner_key)
        if args.temporal_command == "inspect":
            return beacon.inspect(owner_key, args.timer_id)
        if args.temporal_command == "cancel":
            return beacon.cancel(owner_key, args.timer_id)
        if args.temporal_command == "receive":
            receipt = beacon.receive(owner_key, sys.stdout, args.timer_id)
            print(json.dumps(receipt), file=sys.stderr)
            return None
    with app.locked(timeout=30 if args.command in ("ack", "inbox") else 0):
        config = app.config()
        if args.command == "inbox":
            try:
                app.consume_inbox(args.session, args.message, os.environ.get("CMUX_SURFACE_ID"), sys.stdout,
                                  details=args.details)
            except BrokenPipeError as exc:
                try:
                    sys.stdout.close()
                except OSError:
                    pass
                sys.stdout = open(os.devnull, "w")
                raise ValueError("Inbox output failed; no messages were acknowledged") from exc
            return None
        if args.command == "ack":
            return {"acknowledgments": [app.acknowledge(args.session, mid, os.environ.get("CMUX_SURFACE_ID"))
                                         for mid in args.message]}
        if args.command == "enroll":
            surface, session = identity(args.surface), identity(args.session)
            store = json.loads((app.cmux.hook_directory / (args.harness + "-hook-sessions.json")).read_text())
            record = next((v for k, v in store["sessions"].items() if k.upper() == session), {})
            target = {"surface": surface, "workspace": identity(record["workspaceId"]), "session": session,
                      "harness": args.harness, "label": args.label, "started_at": record["startedAt"],
                      "cutoff": record["startedAt"] if args.include_existing else app.clock()}
            app.cmux.binding(target)
            existing = next((t for t in config["targets"] if t["session"] == session), None)
            require(existing is None or existing == {**target, "cutoff": existing["cutoff"]},
                    "Session enrollment differs; unenroll explicitly before changing it")
            require(not any(t["surface"] == surface and t["session"] != session for t in config["targets"]),
                    "Surface has another enrolled session; reconcile ownership first")
            if existing is None:
                config["targets"].append(target)
            if args.coordinator:
                config["coordinator"] = session
            atomic_json(app.directory / "config.json", config)
            return {"enrolled": existing or target}
        if args.command == "opencode-enroll":
            return app.enroll_opencode(args.manifest, args.label, args.include_existing)
        if args.command == "unenroll":
            session = session_identity(args.session)
            config["targets"] = [t for t in config["targets"] if t["session"] != session]
            if config.get("coordinator") == session:
                config["coordinator"] = None
            atomic_json(app.directory / "config.json", config)
            return {"unenrolled": session, "receipts_preserved": True}
        if args.command == "scan":
            return app.scan(args.send, args.only_session)
    # Serve owns a different lifetime lock; short configuration/scan transactions remain available.
    lock = app.directory / "serve.lock"
    require(not lock.is_symlink(), "Refusing symlink service lock")
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A monitor already serves this state") from None
        stopping = False

        def stop(_signal, _frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        app.cmux.stopping = lambda: stopping
        while not stopping:
            report = serve_iteration(app, args.send, args.only_session)
            print(json.dumps(report), flush=True)
            deadline = time.monotonic() + app.config()["interval"]
            while not stopping and time.monotonic() < deadline:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
    return {"stopped": True}


if __name__ == "__main__":
    try:
        result = main()
        if result is not None:
            print(json.dumps(result, indent=2))
            if result.get("outcome") == "rejected" and "event_key" in result:
                sys.exit(2)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        sys.exit(1)
