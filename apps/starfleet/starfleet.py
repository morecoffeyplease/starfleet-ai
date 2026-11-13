#!/usr/bin/env python3
"""Read-only setup checks for the installed Starfleet tools."""

from dataclasses import dataclass
from enum import Enum
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time


SCHEMA_VERSION = 1
MAX_NATIVE_OUTPUT = 128 * 1024
MAX_COMPONENT_OUTPUT = 32 * 1024
MAX_COMPONENT_SECONDS = 3
UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
SESSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
OPENCODE_SESSION_PATTERN = re.compile(r"ses_[A-Za-z0-9]+\Z")
FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class CheckState(str, Enum):
    READY = "ready"
    MISSING = "missing"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"
    SKIPPED = "skipped"


class Evidence(str, Enum):
    INSTALLATION = "installation"
    CONFIGURATION = "configuration"
    NATIVE_IDENTITY = "native_identity"
    EXACT_OWNER = "exact_owner"
    SHARED_WORKER = "shared_worker"
    INTENDED_DELIVERY = "intended_delivery"
    OBSERVED_DELIVERY = "observed_delivery"
    DEPENDENCY = "dependency"


@dataclass(frozen=True)
class Check:
    name: str
    state: CheckState
    evidence: Evidence
    required: bool
    summary: str
    next_command: str

    def as_dict(self):
        return {
            "name": self.name,
            "status": self.state.value,
            "evidence": self.evidence.value,
            "required": self.required,
            "summary": self.summary,
            "next_command": self.next_command,
        }


@dataclass(frozen=True)
class Adapter:
    key: str
    label: str
    command: str | None
    diagnostic_args: tuple[str, ...]
    mode: str = "protocol"
    dependencies: tuple[tuple[str, bool], ...] = ()
    registration_required: bool = True
    worker_required: bool = False
    delivery_required: bool = True
    alias_of: str | None = None
    guide_command: str = "starfleet guide --print"


@dataclass(frozen=True)
class Caller:
    state: CheckState
    harness: str | None = None
    session: str | None = None
    workspace: str | None = None
    surface: str | None = None

    def private_payload(self):
        if self.state is not CheckState.READY:
            return None
        return {
            "schema_version": SCHEMA_VERSION,
            "caller": {
                "harness": self.harness,
                "session": self.session,
                "workspace": self.workspace,
                "surface": self.surface,
            },
        }

    def public(self):
        return {"status": self.state.value, "harness": self.harness}


ADAPTERS = (
    Adapter("starfleet-doctor", "Starfleet Doctor", None, (), mode="self",
            registration_required=False, delivery_required=False),
    Adapter("hailing-frequencies", "Hailing Frequencies", "hailing-frequencies",
            ("doctor", "--starfleet-json"), worker_required=True,
            guide_command="hailing-frequencies guide --print"),
    Adapter("temporal-beacon", "Temporal Beacon", "temporal-beacon",
            ("doctor", "--starfleet-json"), dependencies=(("hailing-frequencies", True),),
            worker_required=True, guide_command="temporal-beacon guide --print"),
    Adapter("agent-notify", "Agent Notify", "agent-notify",
            ("doctor", "--starfleet-json"), registration_required=False,
            dependencies=(("discord-watch", True),), guide_command="agent-notify guide --print"),
    Adapter("discord-watch", "Discord Watch", "discord-watch",
            ("doctor", "--starfleet-json"), worker_required=True,
            guide_command="discord-watch guide --print"),
    Adapter("pattern-buffer", "Pattern Buffer", "pattern-buffer",
            ("status", "--starfleet-json"), mode="pattern-buffer",
            guide_command="pattern-buffer guide --print"),
    Adapter("model-preferences", "Model Preferences", "model-preferences",
            ("doctor", "--starfleet-json"), guide_command="model-preferences --help"),
    Adapter("away-mode", "Away Mode", "away-mode",
            ("doctor", "--starfleet-json"), worker_required=False,
            delivery_required=False, dependencies=(("model-preferences", False),),
            guide_command="away-mode guide --print"),
    Adapter("warp-drive", "Warp-drive", "warp-drive",
            ("doctor", "--starfleet-json"), dependencies=(("hailing-frequencies", True),),
            registration_required=False, worker_required=True,
            guide_command="warp-drive guide --print"),
    Adapter("policy-guard", "Starfleet Policy Guard", None, (), mode="policy-guard",
            registration_required=False, guide_command="starfleet guide --print"),
    Adapter("starfleet-markup", "Starfleet Markup", "starfleet-markup", ("claude-config",), mode="static-command",
            registration_required=False, worker_required=False, guide_command="starfleet-markup guide --print"),
    Adapter("replicator", "Replicator", "replicator", (), mode="passive",
            registration_required=False, worker_required=False, delivery_required=False,
            guide_command="replicator guide --print"),
    Adapter("context-injector", "Context Injector compatibility", "pattern-buffer", (), mode="alias",
            registration_required=False, worker_required=False, delivery_required=False,
            alias_of="pattern-buffer", guide_command="pattern-buffer guide --print"),
)
ADAPTER_BY_KEY = {adapter.key: adapter for adapter in ADAPTERS}
STATUS_NAMES = ("configuration", "registration", "worker", "intended_delivery", "observed_delivery")
OPTIONAL_CHECKS = {"pattern-buffer": ("checkpoint",)}
STATE_BY_NAME = {state.value: state for state in CheckState}


def no_evidence_states(adapter):
    return {
        "configuration": CheckState.UNKNOWN,
        "registration": (CheckState.UNKNOWN if adapter.registration_required else CheckState.NOT_APPLICABLE),
        "worker": (CheckState.UNKNOWN if adapter.worker_required else CheckState.NOT_APPLICABLE),
        "intended_delivery": (CheckState.UNKNOWN if adapter.delivery_required else CheckState.NOT_APPLICABLE),
        "observed_delivery": CheckState.UNKNOWN,
    }


def expected_components():
    return tuple(adapter.key for adapter in ADAPTERS)


def validate_caller_value(value):
    if not isinstance(value, dict) or set(value) != {"workspace_id", "surface_id", "items"}:
        raise ValueError("CMUX identity observation has an unsupported shape")
    workspace, surface = value["workspace_id"], value["surface_id"]
    if not isinstance(workspace, str) or not UUID_PATTERN.fullmatch(workspace):
        raise ValueError("CMUX workspace identity is unavailable")
    if not isinstance(surface, str) or not UUID_PATTERN.fullmatch(surface):
        raise ValueError("CMUX surface identity is unavailable")
    items = value["items"]
    if not isinstance(items, list):
        raise ValueError("CMUX current inventory is unavailable")
    matches = []
    for item in items:
        if not isinstance(item, dict) or item.get("lifecycle") != "running":
            continue
        freshness = item.get("freshness")
        if not isinstance(freshness, dict) or freshness.get("state") != "current":
            continue
        projections = item.get("projections")
        if not isinstance(projections, list):
            continue
        exact_surface = any(
            isinstance(projection, dict)
            and str(projection.get("workspace_id", "")).casefold() == workspace.casefold()
            and str(projection.get("panel_id", "")).casefold() == surface.casefold()
            for projection in projections
        )
        if exact_surface:
            matches.append(item)
    if len(matches) != 1:
        raise ValueError("CMUX did not identify one fresh current caller")
    agents = matches[0].get("agents")
    if not isinstance(agents, list):
        raise ValueError("CMUX caller agent identity is unavailable")
    active = [agent for agent in agents if isinstance(agent, dict) and agent.get("state") != "ended"]
    if len(active) != 1:
        raise ValueError("CMUX caller is ambiguous")
    agent = active[0]
    harness = agent.get("kind")
    session = agent.get("session_id")
    if harness not in ("claude", "codex", "muse", "opencode"):
        raise ValueError("CMUX caller harness is unsupported")
    if agent.get("state") not in ("working", "idle", "needs_input", "backgroundWorkPending"):
        raise ValueError("CMUX caller is not in a supported active state")
    if not isinstance(session, str) or len(session) > 512:
        raise ValueError("CMUX caller session identity is unavailable")
    if harness == "opencode":
        valid_session = OPENCODE_SESSION_PATTERN.fullmatch(session)
    elif harness in ("claude", "codex"):
        valid_session = UUID_PATTERN.fullmatch(session)
    else:
        valid_session = SESSION_PATTERN.fullmatch(session)
    if not valid_session:
        raise ValueError("CMUX caller session identity is invalid")
    return Caller(CheckState.READY, harness, session, workspace, surface)


def run_native_observation(argv, timeout=MAX_COMPONENT_SECONDS):
    status, output, _ = run_bounded(argv, None, None, MAX_NATIVE_OUTPUT, timeout,
                                    native_connection=True)
    if status != "ready":
        return None
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError):
        return None
    return result if isinstance(result, dict) else None


def discover_caller():
    executable = shutil.which("cmux")
    if not executable:
        return Caller(CheckState.UNKNOWN)
    identity = run_native_observation([executable, "--json", "--id-format", "uuids", "identify"])
    current = run_native_observation([executable, "current", "--json", "--limit", "200"])
    if not isinstance(identity, dict) or not isinstance(current, dict):
        return Caller(CheckState.UNKNOWN)
    native_caller = identity.get("caller")
    if not isinstance(native_caller, dict) or native_caller.get("surface_type") != "terminal":
        return Caller(CheckState.UNKNOWN)
    workspace = native_caller.get("workspace_id")
    surface = native_caller.get("surface_id")
    if current.get("truncated") is not False:
        return Caller(CheckState.UNKNOWN)
    try:
        return validate_caller_value({"workspace_id": workspace, "surface_id": surface,
                                      "items": current.get("items")})
    except ValueError:
        return Caller(CheckState.UNKNOWN)


def _read_process_output(stream, remaining):
    try:
        return os.read(stream.fileno(), min(8192, remaining))
    except OSError:
        return b""


def run_bounded(argv, cwd, stdin_bytes, maximum, timeout, native_connection=False):
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=inspection_environment(native_connection=native_connection),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=True,
        )
    except OSError:
        return "unavailable", b"", 127
    selector = selectors.DefaultSelector()
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        try:
            process.stdin.write(stdin_bytes or b"")
            process.stdin.close()
        except (BrokenPipeError, OSError):
            try:
                process.stdin.close()
            except OSError:
                pass
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + timeout
        total = 0
        while selector.get_map():
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                process.kill()
                process.wait()
                return "timeout", b"", process.returncode
            events = selector.select(remaining_time)
            if not events:
                process.kill()
                process.wait()
                return "timeout", b"", process.returncode
            for key, _mask in events:
                chunk = _read_process_output(key.fileobj, maximum - total + 1)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > maximum:
                    process.kill()
                    process.wait()
                    return "oversized", b"", process.returncode
                captured[key.data].extend(chunk)
        remaining_time = max(0.01, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining_time)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return "timeout", b"", process.returncode
        if process.returncode != 0:
            return "failed", b"", process.returncode
        try:
            output = bytes(captured["stdout"]).decode("utf-8")
        except UnicodeDecodeError:
            return "malformed", b"", process.returncode
        return "ready", output, process.returncode
    finally:
        selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        if process.poll() is None:
            process.kill()
            process.wait()


def inspection_environment(native_connection=False):
    allowed = (
        "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE",
        "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "CODEX_HOME", "AGENT_NOTIFY_HOME",
        "WARP_DRIVE_HAILING_STATE",
    )
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.setdefault("PATH", os.defpath)
    environment.setdefault("HOME", str(Path.home()))
    if native_connection and "CMUX_SOCKET" in os.environ:
        environment["CMUX_SOCKET"] = os.environ["CMUX_SOCKET"]
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def executable_version(executable):
    try:
        path = Path(executable).resolve(strict=True)
        if not path.is_file() or path.is_symlink():
            return None
        for parent in path.parents:
            if FINGERPRINT_PATTERN.fullmatch(parent.name):
                return parent.name[:12]
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > 8 * 1024 * 1024:
                    return None
                digest.update(chunk)
        return digest.hexdigest()[:12]
    except OSError:
        return None


def self_version():
    app_dir = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ("starfleet.py", "guide.md", "install.py"):
        path = app_dir / name
        try:
            content = path.read_bytes()
        except OSError:
            return None
        digest.update(name.encode("utf-8") + b"\0" + content)
    return digest.hexdigest()[:12]


def component_summary(name, state, evidence):
    if state is CheckState.READY:
        return {
            "configuration": "Configuration passed the component's local read-only check.",
            "registration": "The exact CMUX caller matches this component's owner record.",
            "worker": "The component's existing worker heartbeat evidence is current; process liveness was not separately checked.",
            "intended_delivery": "The configured delivery scope covers this component's intended route.",
            "observed_delivery": "No native delivery was observed by this read-only check.",
            "dependency": "The required component dependency is selected and ready.",
        }.get(name, "The read-only inspection found the requested evidence." )
    if state is CheckState.MISSING:
        return {
            "configuration": "Required local configuration is missing.",
            "registration": "No exact owner registration was found for the CMUX caller.",
            "worker": "No current worker heartbeat evidence is available.",
            "intended_delivery": "No configured delivery route covers this component.",
            "dependency": "A required component dependency is missing.",
        }.get(name, "Required local evidence is missing.")
    if state is CheckState.BLOCKED:
        return {
            "configuration": "The component reports a local setup hold.",
            "registration": "The existing owner record does not match the exact CMUX caller.",
            "worker": "The existing worker heartbeat is stale or reports a hold.",
            "intended_delivery": "A configured route is held or outside its allowed scope.",
            "dependency": "A required capability is unavailable because its dependency is skipped or held.",
        }.get(name, "The component reports a blocking condition.")
    if state is CheckState.NOT_APPLICABLE:
        return "No registration or worker setup applies to this tool in the current role."
    if state is CheckState.SKIPPED:
        return "Skipped by user instruction. The tool remains disabled and was not inspected."
    if evidence is Evidence.OBSERVED_DELIVERY:
        return "The doctor did not send a message or inspect private conversation content. Delivery remains unverified."
    return "The read-only inspection could not establish this condition."


def _check(name, state, evidence, required, adapter, summary=None, next_command=None):
    summary = summary or component_summary(name, state, evidence)
    next_command = next_command or ("none" if state in (CheckState.READY, CheckState.NOT_APPLICABLE,
                                                        CheckState.SKIPPED) else adapter.guide_command)
    return Check(name, state, evidence, required, summary, next_command)


def skipped_tool(adapter):
    checks = [
        _check("runtime", CheckState.SKIPPED, Evidence.INSTALLATION, False, adapter),
        _check("configuration", CheckState.SKIPPED, Evidence.CONFIGURATION, False, adapter),
        _check("registration", CheckState.SKIPPED, Evidence.EXACT_OWNER, False, adapter),
        _check("worker", CheckState.SKIPPED, Evidence.SHARED_WORKER, False, adapter),
        _check("intended_delivery", CheckState.SKIPPED, Evidence.INTENDED_DELIVERY, False, adapter),
        _check("observed_delivery", CheckState.SKIPPED, Evidence.OBSERVED_DELIVERY, False, adapter),
    ]
    checks.extend(_check(name, CheckState.SKIPPED, Evidence.CONFIGURATION, False, adapter)
                  for name in OPTIONAL_CHECKS.get(adapter.key, ()))
    return {"key": adapter.key, "name": adapter.label, "status": CheckState.SKIPPED.value,
            "selected": False, "runtime_version": None, "checks": [item.as_dict() for item in checks]}


def skipped_statuses(adapter):
    return {key: CheckState.SKIPPED for key in (*STATUS_NAMES, *OPTIONAL_CHECKS.get(adapter.key, ()))}


def normalize_component_protocol(adapter, stdout):
    defaults = no_evidence_states(adapter)
    defaults.update({key: CheckState.UNKNOWN for key in OPTIONAL_CHECKS.get(adapter.key, ())})
    try:
        value = json.loads(stdout)
    except (ValueError, UnicodeError):
        return defaults
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return defaults
    raw_checks = value.get("checks")
    if not isinstance(raw_checks, dict):
        return defaults
    for key in STATUS_NAMES:
        raw = raw_checks.get(key)
        if isinstance(raw, str) and raw in STATE_BY_NAME:
            defaults[key] = STATE_BY_NAME[raw]
    for key in OPTIONAL_CHECKS.get(adapter.key, ()):
        raw = raw_checks.get(key)
        if isinstance(raw, str) and raw in STATE_BY_NAME:
            defaults[key] = STATE_BY_NAME[raw]
    if not adapter.registration_required and defaults["registration"] is CheckState.UNKNOWN:
        defaults["registration"] = CheckState.NOT_APPLICABLE
    if not adapter.worker_required and defaults["worker"] is CheckState.UNKNOWN:
        defaults["worker"] = CheckState.NOT_APPLICABLE
    if not adapter.delivery_required and defaults["intended_delivery"] is CheckState.UNKNOWN:
        defaults["intended_delivery"] = CheckState.NOT_APPLICABLE
    if defaults["observed_delivery"] is CheckState.READY:
        defaults["observed_delivery"] = CheckState.UNKNOWN
    return defaults


def pattern_buffer_status(adapter, executable, cwd, caller):
    statuses = no_evidence_states(adapter)
    argv = [executable, "--cwd", str(cwd), *adapter.diagnostic_args]
    if caller.state is CheckState.READY and caller.harness in ("claude", "codex", "opencode"):
        argv.extend(("--harness", caller.harness, "--session", caller.session))
    result, output, _ = run_bounded(argv, cwd, None, MAX_COMPONENT_OUTPUT, MAX_COMPONENT_SECONDS)
    if result != "ready":
        return statuses
    try:
        value = json.loads(output)
    except (ValueError, UnicodeError):
        return statuses
    if not isinstance(value, dict):
        return statuses
    if value.get("schema_version") == SCHEMA_VERSION:
        return normalize_component_protocol(adapter, output)
    if value.get("registered") is False:
        statuses["configuration"] = CheckState.MISSING
    elif value.get("registered") is True and value.get("enabled") is True:
        statuses["configuration"] = CheckState.READY
    elif value.get("registered") is True:
        statuses["configuration"] = CheckState.BLOCKED
    if caller.state is not CheckState.READY:
        statuses["registration"] = CheckState.UNKNOWN
    else:
        enrollment = value.get("enrollment")
        if enrollment is None:
            statuses["registration"] = CheckState.MISSING
        elif isinstance(enrollment, dict) and enrollment.get("state") == "active":
            statuses["registration"] = CheckState.READY
        else:
            statuses["registration"] = CheckState.BLOCKED
    settings_ready = value.get("owned_hook_handlers", 0) > 0 or value.get("owned_plugin_entries", 0) > 0
    statuses["intended_delivery"] = CheckState.READY if settings_ready else CheckState.MISSING
    statuses["observed_delivery"] = CheckState.UNKNOWN
    return statuses


def policy_guard_status(adapter, argv_prefix, cwd, caller):
    statuses = no_evidence_states(adapter)
    if not argv_prefix:
        statuses["configuration"] = CheckState.UNKNOWN
        return statuses
    argv = [*argv_prefix, "--cwd", str(cwd), "status"]
    if caller.state is CheckState.READY:
        argv.extend(("--session-id", caller.session))
    result, output, _ = run_bounded(argv, cwd, None, MAX_COMPONENT_OUTPUT, MAX_COMPONENT_SECONDS)
    if result != "ready":
        return statuses
    try:
        value = json.loads(output)
    except (ValueError, UnicodeError):
        return statuses
    if not isinstance(value, dict):
        return statuses
    installed = value.get("installed_here")
    if installed is True:
        statuses["configuration"] = CheckState.READY
    elif value.get("attachment_state") == "not-installed":
        statuses["configuration"] = CheckState.MISSING
    elif installed is False:
        statuses["configuration"] = CheckState.BLOCKED
    owner = value.get("current_owner")
    if caller.state is CheckState.READY and isinstance(owner, dict):
        statuses["registration"] = (CheckState.READY if owner.get("state") == "matches"
                                     else CheckState.BLOCKED)
    else:
        statuses["registration"] = CheckState.UNKNOWN
    statuses["intended_delivery"] = CheckState.READY if value.get("coverage", {}).get("configured") else CheckState.UNKNOWN
    statuses["observed_delivery"] = CheckState.UNKNOWN
    return statuses


def inspect_adapter(adapter, caller, cwd, prior_results, policy_guard_cli=None):
    if adapter.mode == "self":
        version = self_version()
        statuses = {
            "configuration": CheckState.NOT_APPLICABLE,
            "registration": CheckState.NOT_APPLICABLE,
            "worker": CheckState.NOT_APPLICABLE,
            "intended_delivery": CheckState.NOT_APPLICABLE,
            "observed_delivery": CheckState.NOT_APPLICABLE,
        }
        return version, statuses
    if adapter.mode == "alias":
        source = prior_results.get(adapter.alias_of)
        if source is None:
            return None, no_evidence_states(adapter)
        return source[0], {
            "configuration": CheckState.NOT_APPLICABLE,
            "registration": CheckState.NOT_APPLICABLE,
            "worker": CheckState.NOT_APPLICABLE,
            "intended_delivery": CheckState.NOT_APPLICABLE,
            "observed_delivery": CheckState.UNKNOWN,
        }
    if adapter.mode == "unsupported":
        executable = shutil.which(adapter.command) if adapter.command else None
        version = executable_version(executable) if executable else None
        statuses = no_evidence_states(adapter)
        statuses["configuration"] = CheckState.UNKNOWN
        statuses["intended_delivery"] = CheckState.UNKNOWN
        statuses["observed_delivery"] = CheckState.UNKNOWN
        return version, statuses
    if adapter.mode == "static-command":
        executable = shutil.which(adapter.command) if adapter.command else None
        version = executable_version(executable) if executable else None
        statuses = no_evidence_states(adapter)
        result = "unavailable"
        if executable:
            result, _output, _ = run_bounded(
                [executable, *adapter.diagnostic_args], str(cwd), None,
                MAX_COMPONENT_OUTPUT, MAX_COMPONENT_SECONDS)
            statuses["configuration"] = (CheckState.READY if result == "ready"
                                           else CheckState.UNKNOWN)
        statuses["registration"] = CheckState.NOT_APPLICABLE
        statuses["worker"] = CheckState.NOT_APPLICABLE
        statuses["intended_delivery"] = (CheckState.READY if result == "ready"
                                          else CheckState.UNKNOWN)
        statuses["observed_delivery"] = CheckState.UNKNOWN
        return version, statuses
    if adapter.mode == "passive":
        executable = shutil.which(adapter.command) if adapter.command else None
        version = executable_version(executable) if executable else None
        statuses = {
            "configuration": CheckState.NOT_APPLICABLE,
            "registration": CheckState.NOT_APPLICABLE,
            "worker": CheckState.NOT_APPLICABLE,
            "intended_delivery": CheckState.NOT_APPLICABLE,
            "observed_delivery": CheckState.NOT_APPLICABLE,
        }
        return version, statuses
    if adapter.mode == "pattern-buffer":
        executable = shutil.which(adapter.command)
        if not executable:
            return None, no_evidence_states(adapter)
        return executable_version(executable), pattern_buffer_status(adapter, executable, cwd, caller)
    if adapter.mode == "policy-guard":
        executable = policy_guard_cli
        if executable:
            path = Path(executable)
            argv_prefix = [sys.executable, str(path)] if path.suffix == ".py" else [str(path)]
            version = executable_version(str(path))
        else:
            discovered = shutil.which("starfleet-policy-guard")
            argv_prefix = [discovered] if discovered else []
            version = executable_version(discovered) if discovered else None
        return version, policy_guard_status(adapter, argv_prefix, cwd, caller)

    executable = shutil.which(adapter.command) if adapter.command else None
    if not executable:
        return None, no_evidence_states(adapter)
    version = executable_version(executable)
    caller_payload = caller.private_payload()
    payload = json.dumps(caller_payload, separators=(",", ":")).encode("utf-8") if caller_payload else b"{}"
    result, output, _returncode = run_bounded(
        [executable, *adapter.diagnostic_args], str(cwd), payload,
        MAX_COMPONENT_OUTPUT, MAX_COMPONENT_SECONDS,
    )
    if result != "ready":
        return version, no_evidence_states(adapter)
    return version, normalize_component_protocol(adapter, output)


def tool_status(adapter, states, version, selected=True):
    checks = []
    runtime_state = CheckState.READY if version else CheckState.UNKNOWN
    if adapter.mode != "self" and adapter.command and not shutil.which(adapter.command):
        runtime_state = CheckState.MISSING
    checks.append(_check("runtime", runtime_state, Evidence.INSTALLATION, True, adapter,
                         summary=(f"Installed command is available; runtime fingerprint {version}."
                                  if runtime_state is CheckState.READY and version else None)))
    evidence = {
        "configuration": Evidence.CONFIGURATION,
        "registration": Evidence.EXACT_OWNER,
        "worker": Evidence.SHARED_WORKER,
        "intended_delivery": Evidence.INTENDED_DELIVERY,
        "observed_delivery": Evidence.OBSERVED_DELIVERY,
    }
    required = {
        "configuration": True,
        "registration": adapter.registration_required,
        "worker": adapter.worker_required,
        "intended_delivery": adapter.delivery_required,
        "observed_delivery": False,
    }
    for name in STATUS_NAMES:
        state = states.get(name, CheckState.UNKNOWN)
        if name == "registration" and not adapter.registration_required and state is CheckState.UNKNOWN:
            state = CheckState.NOT_APPLICABLE
        if name == "worker" and not adapter.worker_required and state is CheckState.UNKNOWN:
            state = CheckState.NOT_APPLICABLE
        if name == "intended_delivery" and not adapter.delivery_required and state is CheckState.UNKNOWN:
            state = CheckState.NOT_APPLICABLE
        checks.append(_check(name, state, evidence[name], required[name], adapter))
    for name in OPTIONAL_CHECKS.get(adapter.key, ()):
        state = states.get(name, CheckState.UNKNOWN)
        checks.append(_check(name, state, Evidence.CONFIGURATION, False, adapter))
    status = aggregate_check_states(checks)
    return {"key": adapter.key, "name": adapter.label, "status": status.value,
            "selected": selected, "runtime_version": version,
            "checks": [check.as_dict() for check in checks]}


def aggregate_check_states(checks):
    required = [check.state for check in checks if check.required]
    if not required:
        return CheckState.NOT_APPLICABLE
    if CheckState.MISSING in required:
        return CheckState.MISSING
    if CheckState.BLOCKED in required:
        return CheckState.BLOCKED
    if CheckState.UNKNOWN in required:
        return CheckState.UNKNOWN
    return CheckState.READY


def add_dependency_checks(tools, skipped):
    by_key = {tool["key"]: tool for tool in tools}
    for tool in tools:
        adapter = ADAPTER_BY_KEY[tool["key"]]
        if not tool["selected"]:
            continue
        for dependency, required in adapter.dependencies:
            if dependency in skipped:
                state = CheckState.BLOCKED
                summary = f"{ADAPTER_BY_KEY[dependency].label} was skipped by user instruction; the dependent capability remains unavailable."
            else:
                dependency_tool = by_key.get(dependency)
                if dependency_tool is None:
                    state, summary = CheckState.UNKNOWN, "The required component dependency was not inspected."
                elif dependency_tool["status"] == CheckState.READY.value:
                    state, summary = CheckState.READY, "The required component dependency is selected and ready."
                elif dependency_tool["status"] == CheckState.MISSING.value:
                    state, summary = CheckState.BLOCKED, f"{ADAPTER_BY_KEY[dependency].label} is missing; the dependent capability remains unavailable."
                elif dependency_tool["status"] == CheckState.SKIPPED.value:
                    state, summary = CheckState.BLOCKED, f"{ADAPTER_BY_KEY[dependency].label} was skipped by user instruction; the dependent capability remains unavailable."
                elif dependency_tool["status"] == CheckState.BLOCKED.value:
                    state, summary = CheckState.BLOCKED, f"{ADAPTER_BY_KEY[dependency].label} reports a hold; the dependent capability remains unavailable."
                else:
                    state, summary = CheckState.UNKNOWN, f"{ADAPTER_BY_KEY[dependency].label} readiness is unknown; dependent capability remains unverified."
            next_command = ADAPTER_BY_KEY[dependency].guide_command
            tool["checks"].append(_check(f"dependency:{dependency}", state, Evidence.DEPENDENCY,
                                         required, adapter, summary=summary, next_command=next_command).as_dict())
        tool["status"] = aggregate_dict_checks(tool["checks"]).value


def aggregate_dict_checks(checks):
    required = [STATE_BY_NAME[item["status"]] for item in checks if item.get("required") is True]
    if not required:
        return CheckState.NOT_APPLICABLE
    if CheckState.MISSING in required:
        return CheckState.MISSING
    if CheckState.BLOCKED in required:
        return CheckState.BLOCKED
    if CheckState.UNKNOWN in required:
        return CheckState.UNKNOWN
    return CheckState.READY


EXIT_CODES = {
    CheckState.READY: 0,
    CheckState.NOT_APPLICABLE: 0,
    CheckState.MISSING: 1,
    CheckState.BLOCKED: 3,
    CheckState.UNKNOWN: 4,
}


def overall_status(tools):
    selected = [tool for tool in tools if tool["selected"]]
    if not selected:
        return CheckState.NOT_APPLICABLE
    states = [STATE_BY_NAME[tool["status"]] for tool in selected]
    if CheckState.MISSING in states:
        return CheckState.MISSING
    if CheckState.BLOCKED in states:
        return CheckState.BLOCKED
    if CheckState.UNKNOWN in states:
        return CheckState.UNKNOWN
    return CheckState.READY


def build_report(skipped, cwd, caller=None, policy_guard_cli=None):
    skipped = set(skipped)
    selected = [adapter for adapter in ADAPTERS if adapter.key not in skipped]
    if caller is None:
        caller = discover_caller() if selected else Caller(CheckState.UNKNOWN)
    results = {}
    tools = []
    for adapter in ADAPTERS:
        if adapter.key in skipped:
            tool = skipped_tool(adapter)
            tools.append(tool)
            results[adapter.key] = (None, skipped_statuses(adapter))
            continue
        version, states = inspect_adapter(adapter, caller, cwd, results, policy_guard_cli)
        tool = tool_status(adapter, states, version)
        tools.append(tool)
        results[adapter.key] = (version, states)
    add_dependency_checks(tools, skipped)
    status = overall_status(tools)
    return {
        "schema_version": SCHEMA_VERSION,
        "overall_status": status.value,
        "exit_code": EXIT_CODES[status],
        "caller": caller.public(),
        "working_directory": "current directory inspected; paths omitted",
        "selected_tools": [tool["key"] for tool in tools if tool["selected"]],
        "skipped_tools": [tool["key"] for tool in tools if not tool["selected"]],
        "tools": tools,
    }


def print_human(report):
    print(f"Starfleet Doctor: {report['overall_status']}")
    caller = report["caller"]
    if caller["status"] == CheckState.READY.value:
        print(f"Current CMUX caller: {caller['harness']} identity confirmed.")
    else:
        print("Current CMUX caller: unknown.")
    if not report["selected_tools"]:
        print("No tools selected. Every tool was skipped by user instruction.")
        print("No setup-complete claim was made.")
    for tool in report["tools"]:
        print(f"\n{tool['name']}: {tool['status']}")
        for check in tool["checks"]:
            suffix = f" (version {tool['runtime_version']})" if check["name"] == "runtime" and tool["runtime_version"] else ""
            print(f"  {check['name'].replace('_', ' ').replace(':', ' ')}: {check['status']}{suffix}")
            print(f"    {check['summary']}")
            if check["next_command"] != "none":
                print(f"    Next: {check['next_command']}")
    print("\nConfiguration readiness is separate from observed native delivery.")


def build_parser():
    parser = argparse.ArgumentParser(prog="starfleet", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="inspect selected Starfleet setup without changing state")
    doctor.add_argument("--skip", action="append", choices=expected_components(), default=[],
                        metavar="TOOL", help="exclude a tool explicitly; repeat for multiple tools")
    doctor.add_argument("--cwd", type=Path, default=Path.cwd(), help="worktree to inspect; defaults to the current directory")
    doctor.add_argument("--json", action="store_true", help="print structured JSON")
    doctor.add_argument("--policy-guard-cli", type=Path,
                        help="trusted Policy Guard CLI path for this checkout's read-only status")
    guide = commands.add_parser("guide", help="print the setup guide")
    guide.add_argument("--print", action="store_true", required=True, dest="print_guide")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "guide":
        try:
            sys.stdout.write((Path(__file__).resolve().with_name("guide.md")).read_text(encoding="utf-8"))
        except OSError:
            print("starfleet: the installed guide is unavailable", file=sys.stderr)
            return 1
        return 0
    try:
        cwd = args.cwd.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        parser.error("--cwd must name an existing directory")
    if not cwd.is_dir():
        parser.error("--cwd must name an existing directory")
    policy_guard_cli = None
    if args.policy_guard_cli is not None:
        try:
            policy_guard_cli = args.policy_guard_cli.expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            parser.error("--policy-guard-cli must name an existing trusted CLI file")
        if not policy_guard_cli.is_file():
            parser.error("--policy-guard-cli must name an existing trusted CLI file")
        policy_guard_cli = str(policy_guard_cli)
    report = build_report(args.skip, cwd, policy_guard_cli=policy_guard_cli)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_human(report)
    return report["exit_code"]


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
