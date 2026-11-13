#!/usr/bin/env python3
"""Machine-local model profiles. Python standard library, no background service."""
import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid


_transport_path = Path(__file__).resolve().with_name("cmux_transport.py")
_transport_spec = importlib.util.spec_from_file_location("model_preferences_cmux_transport", _transport_path)
cmux_transport = importlib.util.module_from_spec(_transport_spec)
_transport_spec.loader.exec_module(cmux_transport)
PreferenceError = cmux_transport.PreferenceError
_opencode_path = Path(__file__).resolve().with_name("opencode_dispatch.py")
_opencode_spec = importlib.util.spec_from_file_location("model_preferences_opencode_dispatch", _opencode_path)
opencode_dispatch = importlib.util.module_from_spec(_opencode_spec)
sys.modules[_opencode_spec.name] = opencode_dispatch
_opencode_spec.loader.exec_module(opencode_dispatch)
_reviewer_guidance_path = Path(__file__).resolve().with_name("reviewer_guidance.py")
_reviewer_guidance_spec = importlib.util.spec_from_file_location(
    "model_preferences_reviewer_guidance", _reviewer_guidance_path)
reviewer_guidance = importlib.util.module_from_spec(_reviewer_guidance_spec)
_reviewer_guidance_spec.loader.exec_module(reviewer_guidance)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise PreferenceError(f"Cannot read {path}: {exc}") from exc


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def locked(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as stream:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PreferenceError("Preferences are busy; retry the command.")
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


ROLES = ("orchestrator", "implementer", "researcher", "standard_review", "advanced_review")
LIGHT_PROFILE = "claude-codex-light"
LIGHT_REVIEW_POLICY = {"initial_slots": [1, 2], "later_slots": [1]}
REVIEW_TARGET_SCHEMA = 1
QWEN_OBSERVER_GUIDE_URL = (
    "https://github.com/morecoffeyplease/starfleet-ai/blob/main/docs/qwen-operating-guide.md"
    "#inference-and-test-observer-preflight"
)


def qwen_observer_guide_path():
    source_path = Path(__file__).resolve()
    repository_root = source_path.parents[2]
    guide_path = repository_root / "docs/qwen-operating-guide.md"
    try:
        if (repository_root / ".git").exists() and guide_path.is_file():
            return str(guide_path.resolve(strict=True))
    except (OSError, RuntimeError):
        pass
    return None


DELEGATION = ("Ask your parent orchestrator for additional agents, including research and review, unless this task explicitly delegates that authority. "
              "Explicit delegation still follows current role choices, reviewer count and advanced-review permission.")

FLAGS = ("money", "time_zones", "repairing_previous_fix", "decision_reversed",
         "reviewer_disagreement", "shared_rule_ownership", "new_design", "rebuild")


def example_config():
    def choice(model, effort):
        return {"model": model, "effort": effort}

    def profile(orch, worker, standard, advanced):
        return {"orchestrator": choice(orch, "medium"),
                "implementer": choice(worker, "xhigh"), "researcher": choice(worker, "high"),
                "standard_review": [choice(m, "high") for m in standard],
                "advanced_review": [choice(m, "high") for m in advanced]}

    models = {}
    for name, harness, slug in (
        ("codex-luna", "codex", "gpt-6-luna"),
        ("codex-sol", "codex", "gpt-6.1-sol"),
        ("codex-astra", "codex", "gpt-6-astra"),
        ("claude-opus", "claude", "claude-opus-5-5"),
        ("claude-sonnet", "claude", "claude-sonnet-5-5"),
        ("claude-haiku", "claude", "claude-haiku-5-5"),
        ("claude-fable", "claude", "claude-fable-5-1"),
        ("muse-spark", "muse", "muse-spark-1.3-contributor"),
    ):
        models[name] = {"harness": harness, "slug": slug,
                        "efforts": ["low", "medium", "high", "xhigh", "max"],
                        "source": "opt-in example; current availability is not checked"}
    models["local-qwen3.6-35b"] = {
        "harness": "opencode", "provider_id": "trial-local", "slug": "qwen3.6:35b", "efforts": ["max"],
        "source": "opt-in example; native pilot verified the local OpenAI-compatible provider's max reasoning request and 32768 output limit",
    }
    qwen_max = choice("local-qwen3.6-35b", "max")
    sol_medium = choice("codex-sol", "medium")
    sol_high = choice("codex-sol", "high")
    light = profile("claude-opus", "codex-luna", ["codex-sol", "claude-opus"],
                    ["claude-fable", "codex-astra"])
    light["standard_review_policy"] = copy.deepcopy(LIGHT_REVIEW_POLICY)
    return {"schema_version": 1, "active": None, "activation": None, "models": models, "profiles": {
        "Claude-Codex": profile("claude-opus", "codex-luna", ["codex-sol", "claude-opus"],
                                ["claude-fable", "codex-astra"]),
        LIGHT_PROFILE: light,
        "Codex-only": profile("codex-sol", "codex-luna", ["codex-sol"], ["codex-astra"]),
        "Claude-only": profile("claude-opus", "claude-haiku", ["claude-opus"], ["claude-fable"]),
        "Claude-Muse": profile("claude-opus", "muse-spark", ["claude-opus"] * 2, ["claude-fable"] * 2),
        "Codex-Muse": profile("codex-sol", "muse-spark", ["codex-sol"], ["codex-astra"]),
        "Codex-Qwen": {"orchestrator": sol_medium, "implementer": qwen_max, "researcher": qwen_max,
                       "standard_review": [sol_high], "advanced_review": [sol_high]},
        "Qwen-only": {"orchestrator": qwen_max, "implementer": qwen_max, "researcher": qwen_max,
                      "standard_review": [sol_high], "advanced_review": [sol_high]},
    }, "approval_authority": {"label": None}, "review_rules": {"file_threshold": 10,
        "money_paths": ["**/money/**", "**/payments/**", "**/pricing/**"],
        "time_zone_paths": ["**/timezone/**", "**/timezones/**", "**/time-zone/**"],
        "excluded_paths": ["docs/**", "**/docs/**", "**/test/**", "**/tests/**",
                           "**/test_*", "**/*.test.*", "**/*.spec.*", "*.md",
                           "**/generated/**", "**/dist/**", "**/build/**", "**/node_modules/**",
                           "bun.lock", "bun.lockb", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                           "Cargo.lock", "poetry.lock", "uv.lock", "**/*.png", "**/*.jpg", "**/*.svg"]}}


def initial_config():
    config = example_config()
    config.update(models={}, profiles={})
    return config


def require(condition, message):
    if not condition:
        raise PreferenceError(message)


def validate(config):
    require(isinstance(config, dict) and config.get("schema_version") == 1, "Expected schema_version 1.")
    models, profiles = config.get("models"), config.get("profiles")
    require(isinstance(models, dict) and isinstance(profiles, dict), "Models and profiles must be objects.")
    for key, model in models.items():
        require(isinstance(model, dict), f"Invalid model {key}.")
        require(model.get("harness") in ("codex", "claude", "muse", "opencode"), f"Unknown harness for {key}.")
        slug = model.get("slug")
        require(isinstance(slug, str) and slug and not slug.startswith("-") and not any(c.isspace() for c in slug),
                f"Exact model slug required for {key}.")
        require(isinstance(model.get("efforts"), list) and model["efforts"] and
                all(e in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
                    for e in model["efforts"]), f"Invalid supported efforts for {key}.")
        if model["harness"] == "muse":
            require(slug.endswith("-contributor"), f"Muse model {key} must use a contributor slug.")
        if model["harness"] == "opencode":
            provider_id = model.get("provider_id")
            require(isinstance(provider_id, str) and provider_id and len(provider_id) <= 128
                    and not any(c.isspace() for c in provider_id),
                    f"OpenCode model {key} requires its exact provider ID.")
            require(model["efforts"] == ["max"], f"OpenCode model {key} requires the explicit max reasoning effort contract.")
        else:
            require("provider_id" not in model, f"Only OpenCode models may define provider_id ({key}).")
    for name, profile in profiles.items():
        expected_keys = set(ROLES) | ({"standard_review_policy"} if isinstance(profile, dict)
                                     and "standard_review_policy" in profile else set())
        require(isinstance(name, str) and name and isinstance(profile, dict) and set(profile) == expected_keys
                and set(ROLES).issubset(profile),
                f"Profile {name} must define exactly {', '.join(ROLES)} and supported policy metadata.")
        if "standard_review_policy" in profile:
            policy = profile["standard_review_policy"]
            require(name == LIGHT_PROFILE and type(policy) is dict and set(policy) == {"initial_slots", "later_slots"}
                    and type(policy.get("initial_slots")) is list and type(policy.get("later_slots")) is list
                    and all(type(slot) is int for slot in policy["initial_slots"] + policy["later_slots"])
                    and policy == LIGHT_REVIEW_POLICY,
                    f"{name}/standard_review_policy must select slots 1 and 2 first, then slot 1.")
            reviewers = profile["standard_review"]
            require(isinstance(reviewers, list) and len(reviewers) == 2
                    and all(isinstance(entry, dict) for entry in reviewers),
                    f"{name}/standard_review_policy requires two configured reviewers.")
            first, second = [models.get(entry.get("model"), {}) for entry in reviewers]
            require(first.get("harness") == "codex" and first.get("slug") == "gpt-6.1-sol"
                    and reviewers[0].get("effort") == "high"
                    and second.get("harness") == "claude" and second.get("slug") == "claude-opus-5-5"
                    and reviewers[1].get("effort") == "high",
                    f"{name}/standard_review_policy requires Codex Sol high followed by Claude Opus high.")
        require((name == LIGHT_PROFILE) == ("standard_review_policy" in profile),
                f"{LIGHT_PROFILE} must define its standard-review cycle policy.")
        for role in ROLES:
            entries = profile[role]
            group = role.endswith("_review")
            require(isinstance(entries, list) if group else isinstance(entries, dict), f"Invalid {name}/{role}.")
            if group:
                require(len(entries) in (1, 2), f"{name}/{role} requires one reviewer or two independent reviewers.")
            for entry in entries if group else [entries]:
                require(isinstance(entry, dict) and set(entry) == {"model", "effort"}, f"Invalid choice in {name}/{role}.")
                require(entry["model"] in models, f"Unknown model in {name}/{role}.")
                m = models[entry["model"]]
                require(entry["effort"] in m["efforts"], f"Unsupported effort in {name}/{role}.")
                allowed = {"codex"} if name == "Codex-only" else {"claude"} if name == "Claude-only" else None
                if allowed:
                    require(m["harness"] in allowed, f"{name} forbids {m['harness']}.")
                if name in ("Claude-Muse", "Codex-Muse"):
                    expected = "muse" if role in ("implementer", "researcher") else name.split("-")[0].lower()
                    require(m["harness"] == expected, f"{name}/{role} requires {expected}.")
                if name in ("Codex-Qwen", "Qwen-only"):
                    if role in ("standard_review", "advanced_review"):
                        require(m["harness"] == "codex" and entry["model"] == "codex-sol" and entry["effort"] == "high",
                                f"{name}/{role} requires the external Codex Sol high reviewer.")
                    elif name == "Codex-Qwen" and role == "orchestrator":
                        require(m["harness"] == "codex" and entry["model"] == "codex-sol" and entry["effort"] == "medium",
                                "Codex-Qwen/orchestrator requires Codex Sol medium.")
                    else:
                        require(m["harness"] == "opencode" and entry["model"] == "local-qwen3.6-35b"
                                and entry["effort"] == "max",
                                f"{name}/{role} requires the exact local Qwen OpenCode model at max effort.")
    authority = config.get("approval_authority")
    require(isinstance(authority, dict) and set(authority) == {"label"} and
            (authority.get("label") is None or
             (isinstance(authority["label"], str) and bool(authority["label"].strip()))),
            "Approval authority needs a nonempty label or an explicit unconfigured value.")
    require(config.get("active") is None or config["active"] in profiles, "Active profile is missing.")
    rules = config.get("review_rules", {})
    require(type(rules.get("file_threshold")) is int and rules["file_threshold"] > 0, "Positive file threshold required.")
    for key in ("money_paths", "time_zone_paths", "excluded_paths"):
        require(isinstance(rules.get(key), list) and all(isinstance(p, str) for p in rules[key]), f"Invalid {key}.")


def selection(config):
    name = config["active"]
    require(name is not None, "No active profile. Activate one first.")
    profile = config["profiles"][name]
    roles = {}
    for role in ROLES:
        entries = profile[role]
        group = role.endswith("_review")
        resolved = []
        for entry in entries if group else [entries]:
            model = config["models"][entry["model"]]
            resolved.append({"family": entry["model"], "harness": model["harness"],
                             "model": model["slug"], "effort": entry["effort"],
                             "contributor_mode": model["harness"] == "muse",
                             **({"provider_id": model["provider_id"]} if model["harness"] == "opencode" else {})})
        roles[role] = resolved if group else resolved[0]
    result = {"profile": name, "roles": roles, "review_rules": config["review_rules"],
              "approval_authority": config["approval_authority"]}
    if "standard_review_policy" in profile:
        result["standard_review_policy"] = profile["standard_review_policy"]
    return {**result, "selection": digest({**result, "activation": config.get("activation")})}


def configured_authority(config):
    label = config["approval_authority"]["label"]
    require(isinstance(label, str) and bool(label.strip()),
            "Configure the human approval authority with authority-set before recording approval or starting handoff.")
    return label.strip()


def describe_roles(roles):
    return {role: ([f"{c['model']} ({c['effort']})" for c in entries]
                   if isinstance(entries, list) else f"{entries['model']} ({entries['effort']})")
            for role, entries in roles.items()}


def run_command(argv, stdin=None):
    return cmux_transport.run_command(argv, stdin)


def run_json(argv, stdin=None):
    return cmux_transport.run_json(argv, stdin, runner=run_command)


def same_recipient(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return (all(left.get(k) == right.get(k) for k in ("session", "harness")) and
            all(isinstance(left.get(k), str) and isinstance(right.get(k), str) and
                left[k].lower() == right[k].lower() for k in ("workspace", "surface")))


class CmuxTransport(cmux_transport.CmuxTransport):
    """Profile notifications through native cmux messaging."""
    message_sender = "model-preferences"

    def run_command(self, argv, stdin=None):
        return run_command(argv, stdin)

    def run_json(self, argv, stdin=None):
        return run_json(argv, stdin)


class App(CmuxTransport):
    def __init__(self, config_dir, state_dir):
        self.config_dir, self.state_dir = Path(config_dir), Path(state_dir)
        self.config_path = self.config_dir / "profiles.json"

    def config(self):
        config = read(self.config_path)
        # Schema-v1 configurations predate explicit approval labels.
        # Supply the neutral legacy label in memory; a later edit persists it.
        config.setdefault("approval_authority", {"label": None})
        validate(config)
        return config

    def state(self, name, default):
        p = self.state_dir / f"{name}.json"
        return read(p) if p.exists() else copy.deepcopy(default)

    def save_state(self, name, value):
        write(self.state_dir / f"{name}.json", value)

    def caller(self):
        """Discover the sole current owner of this terminal, never from environment hints."""
        caller = self.run_json(["cmux", "--json", "--id-format", "uuids", "identify"]).get("caller") or {}
        require(isinstance(caller, dict) and caller.get("surface_type") == "terminal", "Run from the receiving agent's terminal.")
        recipient = {"workspace": caller.get("workspace_id"), "surface": caller.get("surface_id")}
        for value in recipient.values():
            try:
                uuid.UUID(value)
            except (ValueError, TypeError, AttributeError) as exc:
                raise PreferenceError("Cannot establish caller workspace/surface UUIDs.") from exc
        current = self.current()
        require(current.get("truncated") is False, "Caller inventory is incomplete; nothing changed.")
        items = [i for i in current["items"] if i.get("lifecycle") == "running" and
                 i.get("freshness", {}).get("state") == "current" and
                 any(p.get("workspace_id", "").lower() == recipient["workspace"].lower() and
                     p.get("panel_id", "").lower() == recipient["surface"].lower()
                     for p in i.get("projections", []))]
        require(len(items) == 1, "Cannot establish one fresh caller surface; nothing changed.")
        agents = [a for a in items[0].get("agents", []) if a.get("state") != "ended"]
        require(len(agents) == 1 and agents[0].get("state") in ("working", "idle", "needs_input") and
                agents[0].get("kind") in ("claude", "codex", "muse") and agents[0].get("session_id"),
                "Caller needs one unambiguous active native owner; nothing changed.")
        return {**recipient, "session": agents[0]["session_id"], "harness": agents[0]["kind"]}

    def notify(self, snapshot, policy, retry=False):
        with locked(self.config_dir):
            require(selection(self.config())["selection"] == snapshot["selection"], "Profile changed; notify the current selection instead.")
            recipients = self.state("orchestrators", {})
            notice = self.state("notification", {})
            if not retry or notice.get("selection") != snapshot["selection"]:
                notice = {"selection": snapshot["selection"], "profile": snapshot["profile"],
                          "orchestrators": policy, "recipients": {}, "created_at": now()}
                self.save_state("notification", notice)
        try:
            current = self.current()
        except PreferenceError as exc:
            current = None
            error = str(exc)
        results = {}
        for identity, recipient in recipients.items():
            if (retry and notice["recipients"].get(identity, {}).get("status") == "queued" and
                    same_recipient(notice["recipients"][identity].get("recipient"), recipient)):
                previous = notice["recipients"][identity]
                if policy != "future-only" and previous.get("wake", {}).get("status") not in ("submitted", "native", "unnecessary"):
                    results[identity] = {**previous, "wake": self.native_delivery(recipient, previous["receipt"])}
                continue
            state = self.live(recipient, current) if current else "unknown"
            result = {"status": state, "at": now(), "recipient": recipient}
            if state == "active":
                try:
                    with locked(self.config_dir):
                        require(selection(self.config())["selection"] == snapshot["selection"], "Superseded notification; use current preferences.")
                        require(same_recipient(self.state("orchestrators", {}).get(identity), recipient),
                                "Registration changed during notification; retry the current owner.")
                    query = shlex.join([sys.executable, str(Path(__file__).resolve()),
                                        "--config-dir", str(self.config_dir.resolve()),
                                        "--state-dir", str(self.state_dir.resolve())])
                    choices = describe_roles(snapshot["roles"])
                    text = (f"Model Preferences: active profile {snapshot['profile']} ({snapshot['selection'][:12]}). "
                            f"New tasks use these saved choices: {json.dumps(choices, sort_keys=True)}. "
                            f"Existing orchestrators: {policy}. "
                            f"Keep running jobs and permissions unchanged. Source of truth: {query} status; use its resolve command before new dispatches. "
                            "If asked to hand off, start your successor in a new tab of this workspace, critically compare the roadmap and briefing, "
                            "answer their questions, explicitly transfer ownership, and retain this tab until the configured authority approves closing it. "
                            "This message is queued; it is not evidence of adoption. "
                            f"After reading current choices, confirm future-dispatch adoption from your own tab with "
                            f"{query} adopt {shlex.quote(identity)} --selection {snapshot['selection']}.")
                    if policy == "future-only":
                        receipt = run_json(["cmux", "agent", "message", recipient["surface"], "--from", "model-preferences", "--json", "--", "-"], text)
                        result.update(status="queued", receipt=receipt, wake={"status": "not_requested"})
                    else:
                        result.update(self.send_action(recipient, text))
                except PreferenceError as exc:
                    result.update(status="failed", error=str(exc))
            elif state == "unknown":
                result["error"] = error if current is None else "Cannot confirm the enrolled session is active; not sent."
            results[identity] = result
        with locked(self.config_dir):
            saved = self.state("notification", {})
            if saved.get("selection") == snapshot["selection"]:
                saved["recipients"].update(results)
                saved["coverage"] = "Explicitly enrolled orchestrators only; terminal titles do not establish roles."
                saved["inventory_truncated"] = current.get("truncated", False) if current else None
                saved["inventory_error"] = error if current is None else None
                saved["enrolled_count"] = len(recipients)
                self.save_state("notification", saved)
                return saved
        return {"superseded": True, "selection": snapshot["selection"], "attempts": results}


def argv_for(choice, prompt, cwd, interactive=False):
    harness = choice["harness"]
    model, effort = choice["model"], choice["effort"]
    require(harness != "opencode", "OpenCode uses an authenticated API and native CMUX binding, not a CLI subprocess; use the guarded OpenCode dispatch path.")
    require(shutil.which(harness) is not None, f"Selected harness unavailable: {harness}. No fallback permitted.")
    if harness == "codex":
        argv = ["codex"] + ([] if interactive else ["exec"])
        return argv + ["-C", str(cwd), "-m", model, "-c", f'model_reasoning_effort="{effort}"', prompt]
    if harness == "claude":
        return ["claude"] + ([] if interactive else ["-p"]) + ["--model", model, "--effort", effort, prompt]
    require(choice["contributor_mode"] and model.endswith("-contributor"), "Muse contributor mode is mandatory.")
    return ["muse"] + ([] if interactive else ["exec"]) + ["--model", model, "--contributor-mode", "on",
            "--reasoning-effort", effort, "--workspace", str(cwd), prompt]


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, pat) or
               (pat.startswith("**/") and fnmatch.fnmatchcase(path, pat[3:])) for pat in patterns)


def git(cwd, *args):
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True)
    require(proc.returncode == 0, proc.stderr.decode(errors="replace").strip())
    return proc.stdout


def review_scope(config, cwd, base, facts):
    require(isinstance(facts, dict) and set(facts) == set(FLAGS) and all(type(v) is bool for v in facts.values()),
            f"Review facts must explicitly declare true/false for {', '.join(FLAGS)}.")
    base_sha = git(cwd, "rev-parse", "--verify", f"{base}^{{commit}}").decode().strip()
    root = git(cwd, "rev-parse", "--show-toplevel").decode().strip()
    tracked = set(p.decode() for p in git(root, "diff", "--name-only", "--no-renames", "-z", base_sha, "--").split(b"\0") if p)
    untracked = set(p.decode() for p in git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if p)
    paths = tracked | untracked
    rules = config["review_rules"]
    counted = sorted(p for p in paths if not matches(p, rules["excluded_paths"]))
    reasons = [key for key, value in facts.items() if value]
    if any(matches(p, rules["money_paths"]) for p in paths):
        reasons.append("money_path")
    if any(matches(p, rules["time_zone_paths"]) for p in paths):
        reasons.append("time_zone_path")
    if len(counted) >= rules["file_threshold"]:
        reasons.append("file_threshold")
    content = {}
    for p in sorted(paths):
        f = Path(root) / p
        content[p] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None
    return {"root": root, "base": base_sha, "facts": facts, "paths": sorted(paths),
            "tracked_paths": sorted(tracked), "untracked_paths": sorted(untracked),
            "counted_files": counted, "reasons": reasons,
            "role": "advanced_review" if reasons else "standard_review",
            "scope": digest({"root": root, "base": base_sha, "content": content, "facts": facts,
                             "rules": rules})}


def review_summary(plan):
    """Bound terminal output only; full scope and content hashing remain unchanged."""
    result = {key: value for key, value in plan.items() if key != "selection_snapshot"}
    for key in ("paths", "counted_files", "tracked_paths", "untracked_paths"):
        if key not in plan:
            result[key] = None
            result[key + "_count"] = None
            result[key + "_truncated"] = False
            continue
        paths = plan[key]
        result[key] = paths[:20]
        result[key + "_count"] = len(paths)
        result[key + "_truncated"] = len(paths) > 20
    directories = {}
    for path in plan.get("untracked_paths", []):
        parent = str(Path(path).parent)
        directories[parent] = directories.get(parent, 0) + 1
    result["untracked_directories"] = sorted(directories.items(), key=lambda pair: (-pair[1], pair[0]))[:20]
    result["untracked_directory_count"] = len(directories)
    result["untracked_directories_truncated"] = len(directories) > 20
    result["path_classification"] = "recorded" if "untracked_paths" in plan else "unavailable_in_legacy_plan"
    result["scope_note"] = "All tracked and untracked paths remain in scope; previews do not exclude inputs or weaken approval invalidation."
    result["details"] = f"model-preferences review-inspect {plan['id']} --full"
    return result


def review_selection_snapshot(config, plan):
    saved = plan.get("selection_snapshot")
    if isinstance(saved, dict) and saved.get("selection") == plan.get("selection"):
        return saved, "saved_snapshot"
    try:
        active = selection(config)
    except PreferenceError:
        return None, "unknown"
    if active["selection"] == plan.get("selection"):
        return active, "current_selection"
    return None, "unknown"


def review_approval_decisions(app, plan):
    directory = app.state_dir / "approvals"
    if not directory.is_dir():
        return [], "available"
    decisions = []
    for path in sorted(directory.glob("*.json")):
        try:
            approval = read(path)
        except (OSError, ValueError, TypeError, PreferenceError):
            return decisions, "unknown"
        required = {"id", "scope", "selection", "authority", "authorization", "at", "role"}
        if not isinstance(approval, dict) or not required.issubset(approval):
            return decisions, "unknown"
        if (approval.get("scope") == plan.get("scope")
                and approval.get("selection") == plan.get("selection")
                and approval.get("role") in ("advanced_review", "standard_review")):
            decisions.append({"id": approval.get("id", path.stem),
                              "review_role": approval["role"],
                              "authority": approval.get("authority"),
                              "recorded_at": approval.get("at")})
    return decisions, "available"


def review_cycle_slots(app, plan):
    if plan.get("role") != "standard_review" or not plan.get("review_target") or not plan.get("review_cycle"):
        return None, "not_applicable"
    try:
        target = load_target(app, plan["review_target"])
        cycle = cycle_for_target(target, plan["review_cycle"])
        require(any(item.get("id") == plan.get("id") for item in cycle_plan_refs(cycle)),
                "Review plan is not a member of the saved review cycle.")
        require(cycle.get("selection") == plan.get("selection"),
                "Review plan selection differs from the saved review cycle.")
    except (OSError, PreferenceError, ValueError, KeyError, TypeError):
        return None, "unknown"
    if (cycle.get("current_plan", {}).get("id") != plan.get("id")
            or cycle.get("current_plan", {}).get("scope") != plan.get("scope")):
        return cycle, "stale"
    return cycle, "saved_cycle"


def review_freshness(config, plan):
    try:
        active = selection(config)
    except PreferenceError:
        selection_status = "unknown"
    else:
        selection_status = "current" if active["selection"] == plan.get("selection") else "stale"
    try:
        current = review_scope(config, plan["root"], plan["base"], plan["facts"])
    except (OSError, PreferenceError, ValueError, KeyError, TypeError):
        scope_status = "unknown"
    else:
        scope_status = "current" if current["scope"] == plan.get("scope") else "stale"
    if "stale" in (selection_status, scope_status):
        status = "stale"
    elif "unknown" in (selection_status, scope_status):
        status = "unknown"
    else:
        status = "current"
    return {"status": status, "selection": selection_status, "scope": scope_status}


def review_guidance(app, config, plan):
    freshness = review_freshness(config, plan)
    saved_selection, selection_source = review_selection_snapshot(config, plan)
    authority = None
    authority_status = "unknown"
    fresh_review_authorization_required = False
    if saved_selection is not None:
        authority = saved_selection.get("approval_authority", {}).get("label")
        authority_status = "configured" if isinstance(authority, str) and authority.strip() else "unset"
        if authority_status == "unset":
            authority = None
        fresh_review_authorization_required = saved_selection.get("profile") in ("Codex-Qwen", "Qwen-only")

    cycle, cycle_status = review_cycle_slots(app, plan)
    if cycle_status == "stale":
        freshness["status"] = "stale"

    classification_role = plan.get("role")
    requires_approval = classification_role == "advanced_review"
    decisions, decisions_status = review_approval_decisions(app, plan)
    advanced_approval = next((item for item in decisions if item["review_role"] == "advanced_review"), None)
    standard_instead = next((item for item in decisions if item["review_role"] == "standard_review"), None)
    if freshness["status"] == "stale":
        approval_status = "not_required" if not requires_approval else "stale"
    elif decisions_status == "unknown":
        approval_status = "unknown" if requires_approval else "not_required"
    elif freshness["status"] == "unknown":
        approval_status = "unknown" if requires_approval else "not_required"
    elif not requires_approval:
        approval_status = "not_required"
    elif standard_instead and advanced_approval:
        approval_status = "multiple_decisions"
    elif standard_instead:
        approval_status = "standard_instead"
    elif advanced_approval:
        approval_status = "approved"
    else:
        approval_status = "missing"

    dispatch_role = (None if (freshness["status"] == "current" and standard_instead and advanced_approval)
                     else "standard_review" if (freshness["status"] == "current" and standard_instead)
                     else classification_role)
    reviewer_choices = []

    def describe_choice(slot, choice):
        return {"slot": slot, "harness": choice.get("harness"), "provider": choice.get("harness"),
                "provider_id": choice.get("provider_id"), "family": choice.get("family"),
                "model": choice.get("model"), "effort": choice.get("effort")}

    if cycle is not None:
        for item in cycle["slots"]:
            reviewer_choices.append(describe_choice(item["number"], item["choice"]))
    elif saved_selection is not None and dispatch_role in ROLES and dispatch_role.endswith("_review"):
        for slot, choice in enumerate(saved_selection.get("roles", {}).get(dispatch_role, []), 1):
            reviewer_choices.append(describe_choice(slot, choice))

    if classification_role == "advanced_review" and freshness["status"] == "current":
        if approval_status == "multiple_decisions":
            option_roles = ["advanced_review", "standard_review"]
        elif dispatch_role:
            option_roles = [dispatch_role]
        else:
            option_roles = ["advanced_review"]
    else:
        option_roles = [dispatch_role] if dispatch_role else []
    review_options = []
    if saved_selection is not None:
        for role in option_roles:
            if role == dispatch_role and cycle is not None:
                choices = reviewer_choices
            else:
                choices = [describe_choice(slot, choice) for slot, choice in
                           enumerate(saved_selection.get("roles", {}).get(role, []), 1)]
            review_options.append({"role": role, "slots": choices})

    prefix = (f"model-preferences --config-dir {shlex.quote(str(app.config_dir))} "
              f"--state-dir {shlex.quote(str(app.state_dir))}")
    plan_id = plan.get("id", "<plan-id>")
    if freshness["status"] != "current":
        next_action = "Create a fresh review plan for the current worktree scope and active selection before dispatch; do not reuse a decision from this plan."
        command = None
        next_role = None
    elif cycle_status == "unknown":
        next_action = "The saved review cycle could not be verified. Inspect its target and cycle records before dispatch."
        command = None
        next_role = None
    elif requires_approval and authority_status == "unset":
        next_action = ("No human approval authority is configured in this plan's saved selection. Configure the actual authority, "
                       "then create a fresh plan because changing authority changes the selection.")
        command = f'{prefix} authority-set "<human approval authority label>" --orchestrators future-only'
        next_role = None
    elif requires_approval and approval_status == "missing":
        next_action = (f'Ask the configured authority "{authority}" to decide on this exact plan and scope. '
                       "If approved, record that explicit decision; the configured label does not authenticate a person.")
        command = f'{prefix} approve-review {plan_id} --authorization "<recorded explicit decision>"'
        next_role = None
    elif requires_approval and approval_status == "approved":
        next_action = f"Use saved approval {advanced_approval['id']} to dispatch the advanced reviewers in the configured slots."
        next_role = "advanced_review"
        command = (f"{prefix} dispatch --role advanced_review --slot <slot> --cwd "
                   f"{shlex.quote(str(plan.get('root', '<worktree>')))} --task-file <review-brief> "
                   f"--review-plan {plan_id} --approval {advanced_approval['id']}")
    elif requires_approval and approval_status == "standard_instead":
        next_action = (f"The saved authority decision selects standard review. Use approval {standard_instead['id']} "
                       "to dispatch the standard reviewers in the configured slots.")
        next_role = "standard_review"
        command = (f"{prefix} dispatch --role standard_review --slot <slot> --cwd "
                   f"{shlex.quote(str(plan.get('root', '<worktree>')))} --task-file <review-brief> "
                   f"--review-plan {plan_id} --approval {standard_instead['id']}")
    elif requires_approval and approval_status == "multiple_decisions":
        next_action = (f"Both approval decisions are recorded: advanced review ({advanced_approval['id']}) and standard instead "
                       f"({standard_instead['id']}). Choose either supported review role and pass its matching approval.")
        next_role = None
        command = None
    elif requires_approval and approval_status == "unknown":
        next_action = "Approval records could not be fully read. Resolve the approval state before dispatch."
        next_role = None
        command = None
    elif not requires_approval:
        next_action = "Approval is not required for this standard review plan. Dispatch the configured standard reviewer slots."
        next_role = "standard_review"
        command = (f"{prefix} dispatch --role standard_review --slot <slot> --cwd "
                   f"{shlex.quote(str(plan.get('root', '<worktree>')))} --task-file <review-brief> "
                   f"--review-plan {plan_id}")
        if plan.get("review_target") and plan.get("review_cycle"):
            command += (f" --review-target {shlex.quote(plan['review_target'])}"
                        f" --review-cycle {shlex.quote(plan['review_cycle'])}")
    else:
        next_action = "Approval status could not be established. Inspect the saved plan and approval state before dispatch."
        next_role = None
        command = None

    if fresh_review_authorization_required:
        next_action += " Include a fresh --review-authorization value on each review launch."
        if command and " dispatch " in command:
            command += ' --review-authorization "<fresh operator authorization>"'

    return {"plan_status": freshness["status"], "selection_status": freshness["selection"],
            "scope_status": freshness["scope"], "selection_source": selection_source,
            "classification_role": classification_role, "review_role": next_role or dispatch_role,
            "review_slots": reviewer_choices, "review_options": review_options,
            "approval_required": requires_approval, "approval_status": approval_status,
            "approval_authority": {"status": authority_status, "label": authority},
            "fresh_review_authorization_required": fresh_review_authorization_required,
            "approval_records_status": decisions_status, "review_cycle_status": cycle_status,
            "approval_decisions": decisions, "next_action": next_action,
            "next_action_command": command}


def review_output(app, config, plan, full=False, saved_snapshot=False):
    result = dict(plan) if full else review_summary(plan)
    result.pop("selection_snapshot", None)
    result["review_guidance"] = review_guidance(app, config, plan)
    if saved_snapshot:
        result["saved_snapshot"] = True
    return result


def review_target_id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise PreferenceError("Review target IDs must be UUIDs.") from exc


def review_cycle_key(value):
    require(isinstance(value, str) and value == value.strip() and 1 <= len(value) <= 128
            and all(char.isprintable() for char in value),
            "Review cycle keys must be 1 to 128 printable characters without surrounding whitespace.")
    return value


def review_pr_locator(value):
    try:
        parsed = urllib.parse.urlsplit(value) if isinstance(value, str) else None
    except ValueError as exc:
        raise PreferenceError("PR locators must be portable HTTPS URLs without credentials, query strings, or fragments.") from exc
    require(parsed and parsed.scheme == "https" and parsed.hostname and parsed.path not in ("", "/")
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and not any(char.isspace() for char in value),
            "PR locators must be portable HTTPS URLs without credentials, query strings, or fragments.")
    return value


def repository_identity(cwd):
    root = Path(git(cwd, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    remotes = git(root, "remote").decode().splitlines()
    if "origin" in remotes:
        origin = git(root, "remote", "get-url", "origin").decode().strip()
        require(origin, "The repository origin URL is empty.")
        value = None
        try:
            parsed = urllib.parse.urlsplit(origin) if "://" in origin else None
            if parsed and parsed.hostname:
                host = parsed.hostname.casefold()
                if parsed.port:
                    host += f":{parsed.port}"
                path = parsed.path.strip("/")
                if path.endswith(".git"):
                    path = path[:-4]
                if path:
                    value = f"{host}/{path}"
        except ValueError:
            value = None
        if value is None and "@" in origin and ":" in origin:
            host_path = origin.rsplit("@", 1)[1]
            if ":" in host_path:
                host, path = host_path.split(":", 1)
                path = path.strip("/")
                if path.endswith(".git"):
                    path = path[:-4]
                if host and path:
                    value = f"{host.casefold()}/{path}"
        if value:
            return {"kind": "origin", "value": value}
    common = Path(git(root, "rev-parse", "--git-common-dir").decode().strip())
    if not common.is_absolute():
        common = root / common
    return {"kind": "git-common-dir", "value": str(common.resolve())}


def target_file(app, target_id):
    return app.state_dir / "review-targets" / f"{target_id}.json"


def save_target(app, target):
    validate_target(app, target, target["id"])
    app.save_state(f"review-targets/{target['id']}", target)


def load_target(app, target_id):
    target_id = review_target_id(target_id)
    target = app.state(f"review-targets/{target_id}", None)
    require(target, "Unknown review target.")
    validate_target(app, target, target_id)
    return target


def cycle_plan_refs(cycle):
    return [*cycle["prior_plans"], cycle["current_plan"]]


def launch_for_link(app, target, cycle, slot, link):
    launch = app.state(f"launches/{link['launch_id']}", None)
    membership = launch.get("review_cycle") if isinstance(launch, dict) else None
    require(isinstance(launch, dict) and launch.get("id") == link["launch_id"] and
            membership == {"target_id": target["id"], "cycle_id": cycle["id"],
                           "plan_id": link["plan_id"], "slot": slot["number"]} and
            launch.get("choice") == slot["choice"] and launch.get("slot") == slot["number"] and
            isinstance(launch.get("task_digest"), str) and len(launch["task_digest"]) == 64,
            "Review cycle launch membership is missing or conflicting; inspect the target before continuing.")
    return launch


def cycle_completion(app, target, cycle):
    plan_ids = {plan["id"] for plan in cycle_plan_refs(cycle)}
    require(cycle["current_plan"]["id"] in plan_ids, "Review cycle current plan is missing from its history.")
    result = []
    for slot in cycle["slots"]:
        grouped = {plan_id: [] for plan_id in plan_ids}
        for link in slot["launches"]:
            grouped[link["plan_id"]].append(launch_for_link(app, target, cycle, slot, link))
        for plan_id, attempts in grouped.items():
            if plan_id != cycle["current_plan"]["id"]:
                for launch in attempts:
                    require(launch.get("status") == "exited" and type(launch.get("exit_code")) is int,
                            "A previous-plan reviewer launch is unresolved; review plan refresh is held.")
        current = grouped[cycle["current_plan"]["id"]]
        if not current:
            return None
        for launch in current[:-1]:
            require(launch.get("status") == "exited" and type(launch.get("exit_code")) is int
                    and launch["exit_code"] != 0,
                    "Review cycle has duplicate or unresolved launch attempts for one slot.")
        latest = current[-1]
        require(latest.get("status") == "exited" and type(latest.get("exit_code")) is int,
                "A reviewer launch is active or uncertain; the review cycle cannot advance.")
        if latest["exit_code"] != 0:
            return None
        result.append({"number": slot["number"], "launch_id": latest["id"]})
    return result


def predecessor_snapshot(app, target, cycle):
    completed = cycle_completion(app, target, cycle)
    require(completed is not None, "The predecessor review cycle is incomplete; all required slots must exit successfully.")
    return {"cycle_id": cycle["id"], "plan_id": cycle["current_plan"]["id"],
            "selection": cycle["selection"], "slots": completed}


def cycle_slot_status(app, target, cycle, slot):
    all_launches = [launch_for_link(app, target, cycle, slot, link) for link in slot["launches"]]
    if any(launch.get("status") != "exited" or type(launch.get("exit_code")) is not int
           for launch in all_launches):
        return "unresolved", all_launches[-1]["id"] if all_launches else None
    current = [launch for link, launch in zip(slot["launches"], all_launches)
               if link["plan_id"] == cycle["current_plan"]["id"]]
    if not current:
        return "not_started", None
    if any(launch["exit_code"] == 0 for launch in current[:-1]):
        return "conflict", current[-1]["id"]
    latest = current[-1]
    return ("complete" if latest["exit_code"] == 0 else "failed"), latest["id"]


def cycle_summary(app, target, cycle):
    slots = []
    for slot in cycle["slots"]:
        status, launch_id = cycle_slot_status(app, target, cycle, slot)
        slots.append({"number": slot["number"], "choice": slot["choice"],
                      "status": status, "launch": launch_id})
    return {"id": cycle["id"], "key": cycle["key"],
            "previous_cycle_id": cycle["previous_cycle_id"],
            "plan_id": cycle["current_plan"]["id"], "scope": cycle["current_plan"]["scope"],
            "slots": slots, "complete": all(slot["status"] == "complete" for slot in slots)}


def bind_target_pr(app, target, locator):
    locator = review_pr_locator(locator)
    for path in sorted((app.state_dir / "review-targets").glob("*.json")):
        other = read(path)
        require(isinstance(other, dict), "A review target record is malformed; PR binding is held.")
        if other.get("id") != target["id"] and other.get("pr_locator") == locator:
            raise PreferenceError("This PR locator is already bound to another review target.")
    require(target["pr_locator"] in (None, locator),
            "Review target PR locator is already bound and cannot be changed.")
    target["pr_locator"] = locator
    save_target(app, target)
    return target


def create_review_target(app, args):
    target_id = review_target_id(args.id)
    repository = repository_identity(args.cwd)
    path = target_file(app, target_id)
    if path.exists():
        target = load_target(app, target_id)
        require(target["repository"] == repository,
                "Review target belongs to a different repository.")
        if args.pr_locator:
            bind_target_pr(app, target, args.pr_locator)
        return {**target, "created": False}
    target = {"schema_version": REVIEW_TARGET_SCHEMA, "id": target_id, "repository": repository,
              "pr_locator": None, "cycles": []}
    save_target(app, target)
    if args.pr_locator:
        target = bind_target_pr(app, target, args.pr_locator)
    return {**target, "created": True}


def review_target_command(app, args):
    if args.review_target_action == "create":
        return create_review_target(app, args)
    target = load_target(app, args.target)
    if args.review_target_action == "bind-pr":
        return bind_target_pr(app, target, args.pr_locator)
    return {**target, "cycles": [cycle_summary(app, target, cycle) for cycle in target["cycles"]]}


def cycle_for_target(target, cycle_id):
    cycle_id = safe_id(cycle_id)
    cycle = next((item for item in target["cycles"] if item["id"] == cycle_id), None)
    require(cycle, "Review cycle does not belong to this target.")
    return cycle


def saved_review_plan(app, plan_id):
    plan = app.state(f"reviews/{safe_id(plan_id)}", None)
    require(isinstance(plan, dict), "Unknown or malformed review plan.")
    return plan


def cycle_review_context(app, config, args, cwd=None):
    target_id = review_target_id(getattr(args, "review_target", None))
    cycle_id = safe_id(getattr(args, "review_cycle", None))
    plan_id = safe_id(getattr(args, "review_plan", None))
    target = load_target(app, target_id)
    cycle = cycle_for_target(target, cycle_id)
    plan = saved_review_plan(app, plan_id)
    active = selection(config)
    require(active["profile"] == LIGHT_PROFILE and cycle["profile"] == LIGHT_PROFILE
            and active["selection"] == cycle["selection"] == plan.get("selection"),
            "Current profile selection does not match this review cycle.")
    require(plan.get("role") == "standard_review" and plan.get("review_target") == target_id
            and plan.get("review_cycle") == cycle_id
            and plan.get("review_cycle_key") == cycle["key"]
            and plan.get("review_repository") == target["repository"]
            and cycle["current_plan"]["id"] == plan_id
            and cycle["current_plan"]["scope"] == plan.get("scope"),
            "Review plan does not match the saved target and current cycle.")
    if cwd is not None:
        require(repository_identity(cwd) == target["repository"],
                "Review worktree belongs to a different repository than the target.")
    requested_slot = getattr(args, "slot", None)
    if requested_slot is None and len(cycle["slots"]) == 1:
        requested_slot = cycle["slots"][0]["number"]
    slot = next((item for item in cycle["slots"] if item["number"] == requested_slot), None)
    require(slot, "Reviewer slot is not required by this review cycle.")
    choice = resolve(active, "standard_review", requested_slot)
    require(choice == slot["choice"],
            "Configured reviewer choice conflicts with the frozen review cycle slot.")
    return target, cycle, plan, slot, choice


def find_cycle_plans(app, target_id, cycle_key):
    directory = app.state_dir / "reviews"
    if not directory.is_dir():
        return []
    plans = []
    for path in sorted(directory.glob("*.json")):
        plan = read(path)
        require(isinstance(plan, dict), "A review plan record is malformed; cycle recovery is held.")
        if plan.get("review_target") == target_id and plan.get("review_cycle_key") == cycle_key:
            plans.append(plan)
    return plans


def make_light_review_plan(app, config, args, plan):
    target_id = review_target_id(args.target)
    cycle_key = review_cycle_key(args.cycle_key)
    target = load_target(app, target_id)
    require(repository_identity(args.cwd) == target["repository"],
            "Review worktree belongs to a different repository than the target.")
    active = selection(config)
    require(active["profile"] == LIGHT_PROFILE, "Review targets with cycles are supported only by claude-codex-light.")
    cycle = next((item for item in target["cycles"] if item["key"] == cycle_key), None)
    if cycle:
        require(args.previous_cycle in (None, cycle["previous_cycle_id"]),
                "Review cycle key is already bound to a different predecessor.")
        require(cycle["selection"] == active["selection"],
                "The active selection changed during this cycle; start a new cycle after resolving it.")
        if cycle["current_plan"]["scope"] == plan["scope"]:
            saved = saved_review_plan(app, cycle["current_plan"]["id"])
            return saved
        require(cycle_completion(app, target, cycle) is None,
                "This review cycle is complete; use a new cycle key for another review pass.")
        for slot in cycle["slots"]:
            for link in slot["launches"]:
                launch = launch_for_link(app, target, cycle, slot, link)
                require(launch.get("status") == "exited" and type(launch.get("exit_code")) is int,
                        "A reviewer launch is active or uncertain; review plan refresh is held.")
        cycle_id = cycle["id"]
        previous_cycle = cycle["previous_cycle_id"]
    else:
        if target["cycles"]:
            previous = target["cycles"][-1]
            require(args.previous_cycle == previous["id"],
                    "A new review cycle requires the exact latest --previous-cycle ID.")
            completed = predecessor_snapshot(app, target, previous)
            slots = LIGHT_REVIEW_POLICY["later_slots"]
            predecessor = completed
            previous_cycle = previous["id"]
        else:
            require(args.previous_cycle is None, "The first review cycle cannot name a predecessor.")
            slots = LIGHT_REVIEW_POLICY["initial_slots"]
            predecessor = None
            previous_cycle = None
        orphaned = find_cycle_plans(app, target_id, cycle_key)
        if orphaned:
            require(len(orphaned) == 1 and orphaned[0].get("scope") == plan["scope"]
                    and orphaned[0].get("selection") == active["selection"]
                    and orphaned[0].get("review_previous_cycle") == previous_cycle,
                    "An incomplete review-cycle plan write conflicts with the requested scope or predecessor.")
            plan = orphaned[0]
            cycle_id = safe_id(plan.get("review_cycle"))
        else:
            cycle_id = uuid.uuid4().hex
        cycle = {"id": cycle_id, "key": cycle_key, "previous_cycle_id": previous_cycle,
                 "predecessor": predecessor, "profile": LIGHT_PROFILE,
                 "selection": active["selection"], "policy": copy.deepcopy(LIGHT_REVIEW_POLICY),
                 "slots": [{"number": slot, "choice": resolve(active, "standard_review", slot), "launches": []}
                           for slot in slots],
                 "prior_plans": [], "current_plan": {"id": plan["id"], "scope": plan["scope"]}}
    if cycle and cycle["current_plan"]["scope"] != plan["scope"]:
        orphaned = find_cycle_plans(app, target_id, cycle_key)
        current_ids = {item["id"] for item in cycle_plan_refs(cycle)}
        pending = [item for item in orphaned if item.get("id") not in current_ids]
        if pending:
            require(len(pending) == 1 and pending[0].get("scope") == plan["scope"]
                    and pending[0].get("selection") == active["selection"],
                    "An incomplete review plan refresh conflicts with the requested scope.")
            plan = pending[0]
        else:
            cycle["prior_plans"].append(cycle["current_plan"])
            cycle["current_plan"] = {"id": plan["id"], "scope": plan["scope"]}
    if plan.get("review_cycle") != cycle_id:
        plan.update(review_target=target_id, review_cycle=cycle_id, review_cycle_key=cycle_key,
                    review_previous_cycle=previous_cycle, review_repository=target["repository"])
        app.save_state(f"reviews/{plan['id']}", plan)
    if not any(item["id"] == cycle_id for item in target["cycles"]):
        target["cycles"].append(cycle)
    elif cycle["current_plan"]["id"] != plan["id"]:
        cycle["prior_plans"].append(cycle["current_plan"])
        cycle["current_plan"] = {"id": plan["id"], "scope": plan["scope"]}
    save_target(app, target)
    return plan


def reconcile_review_launch(app, target, cycle, slot, plan_id, task_digest):
    links = [link for link in slot["launches"] if link["plan_id"] == plan_id]
    linked_ids = {link["launch_id"] for link in links}
    matches = []
    launches_dir = app.state_dir / "launches"
    if launches_dir.is_dir():
        for path in sorted(launches_dir.glob("*.json")):
            launch = read(path)
            require(isinstance(launch, dict), "A launch record is malformed; review dispatch is held.")
            if launch.get("review_cycle") == {"target_id": target["id"], "cycle_id": cycle["id"],
                                               "plan_id": plan_id, "slot": slot["number"]} and \
                    launch.get("id") not in linked_ids:
                matches.append(launch)
    require(len(matches) <= 1,
            "Multiple unlinked launches claim this target, cycle, plan, and slot; inspect before retrying.")
    if matches:
        launch = matches[0]
        require(launch.get("choice") == slot["choice"] and launch.get("task_digest") == task_digest,
                "Unlinked review launch conflicts with the requested slot or task.")
        slot["launches"].append({"plan_id": plan_id, "launch_id": launch["id"]})
        save_target(app, target)
        links.append({"plan_id": plan_id, "launch_id": launch["id"]})
    if not links:
        return None
    latest = launch_for_link(app, target, cycle, slot, links[-1])
    require(latest.get("task_digest") == task_digest,
            "This review slot already has a different task brief under the saved plan.")
    if latest.get("status") != "exited" or type(latest.get("exit_code")) is not int:
        return latest
    if latest["exit_code"] == 0:
        return latest
    return latest


def record_review_launch(app, target, cycle, slot, plan_id, launch):
    app.save_state(f"launches/{launch['id']}", launch)
    slot["launches"].append({"plan_id": plan_id, "launch_id": launch["id"]})
    save_target(app, target)


def validate_target(app, target, target_id):
    require(isinstance(target, dict) and set(target) == {"schema_version", "id", "repository", "pr_locator", "cycles"}
            and target.get("schema_version") == REVIEW_TARGET_SCHEMA and target.get("id") == target_id
            and isinstance(target.get("repository"), dict)
            and set(target["repository"]) == {"kind", "value"}
            and target["repository"].get("kind") in ("origin", "git-common-dir")
            and isinstance(target["repository"].get("value"), str) and target["repository"]["value"]
            and isinstance(target.get("cycles"), list),
            "Review target record is malformed; progression is held.")
    if target["pr_locator"] is not None:
        review_pr_locator(target["pr_locator"])
    cycle_ids, cycle_keys, launch_ids = set(), set(), set()
    for index, cycle in enumerate(target["cycles"]):
        expected_fields = {"id", "key", "previous_cycle_id", "predecessor", "profile", "selection",
                           "policy", "slots", "prior_plans", "current_plan"}
        require(isinstance(cycle, dict) and set(cycle) == expected_fields and
                isinstance(cycle.get("slots"), list) and isinstance(cycle.get("prior_plans"), list) and
                isinstance(cycle.get("current_plan"), dict) and
                isinstance(cycle.get("id"), str) and len(cycle["id"]) == 32 and
                all(char in "0123456789abcdef" for char in cycle["id"]) and
                cycle["id"] not in cycle_ids and cycle.get("profile") == LIGHT_PROFILE and
                cycle.get("policy") == LIGHT_REVIEW_POLICY and isinstance(cycle.get("selection"), str)
                and len(cycle["selection"]) == 64 and
                all(char in "0123456789abcdef" for char in cycle["selection"]),
                "Review target cycle record is malformed or conflicting; progression is held.")
        review_cycle_key(cycle["key"])
        require(cycle["key"] not in cycle_keys, "Review target has duplicate cycle keys; progression is held.")
        cycle_ids.add(cycle["id"])
        cycle_keys.add(cycle["key"])
        expected_slots = LIGHT_REVIEW_POLICY["initial_slots"] if index == 0 else LIGHT_REVIEW_POLICY["later_slots"]
        require([slot.get("number") if isinstance(slot, dict) else None for slot in cycle["slots"]] == expected_slots,
                "Review cycle required slots conflict with the light policy; progression is held.")
        if index == 0:
            require(cycle["previous_cycle_id"] is None and cycle["predecessor"] is None,
                    "The first review cycle cannot have a predecessor.")
        else:
            require(cycle["previous_cycle_id"] == target["cycles"][index - 1].get("id")
                    and isinstance(cycle["predecessor"], dict),
                    "Review target predecessor chain is missing or conflicting; progression is held.")
        require(isinstance(cycle["prior_plans"], list) and isinstance(cycle["current_plan"], dict),
                "Review cycle plan history is malformed or conflicting; progression is held.")
        plan_refs = cycle_plan_refs(cycle)
        require(plan_refs and all(isinstance(plan, dict) and set(plan) == {"id", "scope"} and
                                  isinstance(plan.get("id"), str) and len(plan["id"]) == 32 and
                                  all(char in "0123456789abcdef" for char in plan["id"]) and
                                  isinstance(plan.get("scope"), str) and len(plan["scope"]) == 64 and
                                  all(char in "0123456789abcdef" for char in plan["scope"])
                                  for plan in plan_refs) and
                len({plan["id"] for plan in plan_refs}) == len(plan_refs) and
                len(cycle["prior_plans"]) == len(plan_refs) - 1,
                "Review cycle plan history is malformed or conflicting; progression is held.")
        for plan in plan_refs:
            saved = app.state(f"reviews/{plan['id']}", None)
            require(isinstance(saved, dict) and saved.get("id") == plan["id"]
                    and saved.get("role") == "standard_review"
                    and saved.get("selection") == cycle["selection"] and saved.get("scope") == plan["scope"]
                    and saved.get("review_target") == target_id and saved.get("review_cycle") == cycle["id"]
                    and saved.get("review_cycle_key") == cycle["key"]
                    and saved.get("review_previous_cycle") == cycle["previous_cycle_id"]
                    and saved.get("review_repository") == target["repository"],
                    "Review cycle plan record is missing or conflicts with its target; progression is held.")
        plan_ids = {plan["id"] for plan in plan_refs}
        for slot in cycle["slots"]:
            number = slot["number"]
            expected_model = ("gpt-6.1-sol", "codex", "high") if number == 1 else \
                ("claude-opus-5-5", "claude", "high")
            choice = slot.get("choice")
            require(set(slot) == {"number", "choice", "launches"} and isinstance(choice, dict)
                    and choice.get("profile") == cycle["profile"]
                    and choice.get("selection") == cycle["selection"]
                    and choice.get("role") == "standard_review"
                    and (choice.get("model"), choice.get("harness"), choice.get("effort")) == expected_model
                    and isinstance(slot.get("launches"), list),
                    "Review cycle slot choice is malformed or conflicts with the light policy.")
            references = set()
            for link in slot["launches"]:
                require(isinstance(link, dict) and set(link) == {"plan_id", "launch_id"}
                        and link.get("plan_id") in plan_ids and isinstance(link.get("launch_id"), str)
                        and len(link["launch_id"]) == 32 and
                        all(char in "0123456789abcdef" for char in link["launch_id"]) and
                        link["launch_id"] not in launch_ids and link["launch_id"] not in references,
                        "Review target has duplicate or conflicting launch membership; progression is held.")
                references.add(link["launch_id"])
                launch_ids.add(link["launch_id"])
                launch_for_link(app, target, cycle, slot, link)
            for plan_id in plan_ids:
                attempts = [link for link in slot["launches"] if link["plan_id"] == plan_id]
                for link in attempts[:-1]:
                    launch = launch_for_link(app, target, cycle, slot, link)
                    require(launch.get("status") == "exited" and type(launch.get("exit_code")) is int
                            and launch["exit_code"] != 0,
                            "Review cycle has an unapproved retry or unresolved earlier attempt.")
    for index in range(1, len(target["cycles"])):
        previous = target["cycles"][index - 1]
        cycle = target["cycles"][index]
        require(cycle["predecessor"] == predecessor_snapshot(app, target, previous),
                "Review cycle predecessor completion snapshot conflicts with launch history; progression is held.")


def startup_report(config, expected=None):
    require(expected is None or expected in config["profiles"], "Unknown intended profile.")
    active = selection(config) if config["active"] else None
    mismatch = expected is not None and (active is None or active["profile"] != expected)
    if mismatch:
        instruction = "Saved selection differs from startup intent; coordinate an authorized shared activation before dispatch."
    elif not active:
        instruction = "No active profile; coordinate activation before dispatch."
    else:
        instruction = "Use these saved choices for new dispatches; existing sessions and jobs keep their original models."
    return {"active_profile": active["profile"] if active else None, "intended_profile": expected,
            "profile_mismatch": mismatch, "selection": active["selection"] if active else None,
            "roles": active["roles"] if active else None, "ready_for_dispatch": bool(active) and not mismatch,
            "instruction": instruction,
            "authority": "Read-only check; startup intent does not activate a profile or replace an orchestrator."}


def read_only_diagnostic_report(app, caller=None):
    """Return bounded setup states without entering the configuration lock."""
    statuses = {"configuration": "unknown", "registration": "unknown", "worker": "not_applicable",
                "intended_delivery": "not_applicable", "observed_delivery": "unknown"}
    try:
        config = read_only_diagnostic_json(app.config_path)
        if config is None:
            statuses["configuration"] = "missing"
            active = None
        else:
            config.setdefault("approval_authority", {"label": None})
            validate(config)
            active = selection(config) if config.get("active") else None
            statuses["configuration"] = "ready" if active is not None else "missing"
    except (PreferenceError, OSError, ValueError, KeyError, TypeError):
        statuses["configuration"] = "blocked"
        active = None
    try:
        recipients = read_only_diagnostic_json(app.state_dir / "orchestrators.json") or {}
        adoptions = read_only_diagnostic_json(app.state_dir / "adoptions.json") or {}
    except (PreferenceError, OSError, ValueError, KeyError, TypeError):
        return {"schema_version": 1, "checks": statuses}
    if not isinstance(recipients, dict) or not isinstance(adoptions, dict):
        statuses["registration"] = "unknown"
        return {"schema_version": 1, "checks": statuses}
    if not isinstance(caller, dict):
        return {"schema_version": 1, "checks": statuses}
    caller_fields = {key: caller.get(key) for key in ("workspace", "surface", "session", "harness")}
    if (not all(isinstance(caller_fields[key], str) and caller_fields[key]
                for key in caller_fields) or
            caller_fields["harness"] not in ("claude", "codex", "muse", "opencode")):
        return {"schema_version": 1, "checks": statuses}
    matches = [name for name, recipient in recipients.items()
               if same_recipient(recipient, caller_fields)]
    statuses["registration"] = "ready" if len(matches) == 1 else "missing" if not matches else "blocked"
    if len(matches) == 1 and active is not None:
        record = adoptions.get(matches[0])
        if (isinstance(record, dict) and same_recipient(record.get("recipient"), caller_fields)
                and record.get("selection") == active["selection"]):
            statuses["configuration"] = "ready"
        else:
            statuses["configuration"] = "blocked"
    return {"schema_version": 1, "checks": statuses}


def read_only_diagnostic_json(path, maximum=256 * 1024):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("diagnostic input is unavailable") from None
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode), "diagnostic input is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        require(len(raw) <= maximum, "diagnostic input exceeds its size limit")
        value = json.loads(raw.decode("utf-8"))
        require(isinstance(value, dict), "diagnostic input is not a JSON object")
        return value
    except (OSError, UnicodeError, ValueError, TypeError):
        raise ValueError("diagnostic input is invalid") from None
    finally:
        os.close(descriptor)


def read_only_diagnostic_caller(stream):
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
    return value["caller"]


def adoption_status(app, active, recipients):
    records = app.state("adoptions", {})
    result = {}
    for name, recipient in recipients.items():
        record = records.get(name)
        if record is None:
            status = "not_acknowledged"
        elif not same_recipient(record["recipient"], recipient):
            status = "stale_owner"
        elif active is None or record["selection"] != active["selection"]:
            status = "stale_selection"
        else:
            status = "acknowledged"
        result[name] = {"status": status, "acknowledgment": record}
    return result


def enroll_recipient(app, name, recipient, replace=True):
    recipients = app.state("orchestrators", {})
    previous = recipients.get(name)
    require(previous is None or same_recipient(previous, recipient) or replace,
            "This name belongs to another owner. Inspect status, then use enroll-self NAME --replace for an intended successor.")
    require(not any(r["surface"].lower() == recipient["surface"].lower() and key != name
                    for key, r in recipients.items()), "This tab is already enrolled under another name.")
    recipients[name] = recipient
    app.save_state("orchestrators", recipients)
    return {"enrolled": name, **recipient, "replaced": previous if previous and not same_recipient(previous, recipient) else None,
            "instruction": "Notification binding only; no session, worker, task ownership or permission was changed."}


def parser():
    home = Path.home()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config-dir", default=str(Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "starfleet-ai/model-preferences"))
    p.add_argument("--state-dir", default=str(Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")) / "starfleet-ai/model-preferences"))
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--examples", action="store_true",
                      help="include the opt-in sample model families and profiles; no profile is activated")
    sub.add_parser("examples", help="print the opt-in sample model families and profiles as JSON")
    print_guidance = sub.add_parser("review-guidance", help="print reviewer judgment guidance without reading configuration or state")
    print_guidance.add_argument("--print", action="store_true", required=True,
                                help="print the maintained guidance")
    sub.add_parser("list")
    doctor = sub.add_parser("doctor", help="Read local setup without acquiring the configuration lock")
    doctor.add_argument("--starfleet-json", action="store_true",
                        help="read a bounded caller match from stdin and print the Starfleet diagnostic protocol")
    status = sub.add_parser("status")
    status.add_argument("--review-target")
    status.add_argument("--review-cycle")
    startup = sub.add_parser("startup"); startup.add_argument("--expect-profile")
    preflight = sub.add_parser(
        "preflight",
        help="Check a saved OpenCode role with read-only API metadata requests.",
        description="Check the active profile's exact role, worktree and OpenCode API configuration without creating a session, surface, prompt or model inference request.",
        epilog=("This is metadata/config evidence, not proof of native TUI behavior or task success. "
                "Optional --require-plugin checks the API server's exact project plugin row for state.status=active.\n"
                "Use --config-dir and --state-dir for an existing isolated pilot profile; this command never activates or overrides a profile.\n"
                "Required reading before OpenCode harness preparation or parent/child task-brief relay:\n"
                f"{QWEN_OBSERVER_GUIDE_URL}\n"
                "Relay this guide and applicable checks before a child brief is snapshotted; do not rewrite an existing brief."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    preflight.add_argument("--role", choices=ROLES, required=True)
    preflight.add_argument("--slot", type=int, choices=(1, 2))
    preflight.add_argument("--cwd", required=True, help="Exact Git worktree root whose OpenCode config should be checked.")
    preflight.add_argument("--opencode-manifest", required=True, help="Private loopback OpenCode connection manifest.")
    preflight.add_argument("--require-plugin", action="append", default=[], metavar="ID",
                           help="Require this exact API-reported project plugin ID to have state.status=active; repeatable.")
    sub.add_parser("validate")
    show = sub.add_parser("show"); show.add_argument("profile")
    for name in ("activate", "notify"):
        s = sub.add_parser(name)
        if name == "activate":
            s.add_argument("profile")
            s.add_argument("--orchestrators", choices=("future-only", "ask", "handoff"), required=True,
                           help="Collect the user's intent; never infer whether existing conversations should move.")
        else:
            s.add_argument("--retry", action="store_true", required=True)
            s.add_argument("--orchestrators", choices=("future-only", "ask", "handoff"))
    put = sub.add_parser("profile-put"); put.add_argument("name"); put.add_argument("--file", required=True)
    put.add_argument("--orchestrators", choices=("future-only", "ask", "handoff"))
    model = sub.add_parser("model-set"); model.add_argument("family"); model.add_argument("--harness", choices=("claude", "codex", "muse", "opencode"), required=True)
    model.add_argument("--provider-id")
    model.add_argument("--slug", required=True)
    model.add_argument("--efforts", nargs="+", required=True); model.add_argument("--source", required=True)
    model.add_argument("--orchestrators", choices=("future-only", "ask", "handoff"))
    authority = sub.add_parser("authority-set"); authority.add_argument("label")
    authority.add_argument("--orchestrators", choices=("future-only", "ask", "handoff"))
    enroll = sub.add_parser("enroll"); enroll.add_argument("name")
    for key in ("workspace", "surface", "session", "harness"):
        enroll.add_argument(f"--{key}", required=True)
    self_enroll = sub.add_parser("enroll-self"); self_enroll.add_argument("name")
    self_enroll.add_argument("--replace", action="store_true", help="Replace this lane's registration with the verified caller, without stopping its outgoing owner.")
    adopt = sub.add_parser("adopt"); adopt.add_argument("name"); adopt.add_argument("--selection", required=True)
    remove = sub.add_parser("unenroll"); remove.add_argument("name")
    review_target = sub.add_parser("review-target")
    target_actions = review_target.add_subparsers(dest="review_target_action", required=True)
    target_create = target_actions.add_parser("create")
    target_create.add_argument("--cwd", required=True)
    target_create.add_argument("--id", required=True)
    target_create.add_argument("--pr-locator")
    target_show = target_actions.add_parser("show")
    target_show.add_argument("target")
    target_bind = target_actions.add_parser("bind-pr")
    target_bind.add_argument("target")
    target_bind.add_argument("--pr-locator", required=True)
    for name in ("resolve", "dispatch"):
        s = sub.add_parser(name); s.add_argument("--role", choices=ROLES, required=True)
        s.add_argument("--slot", type=int, choices=(1, 2))
        s.add_argument("--review-target")
        s.add_argument("--review-cycle")
        s.add_argument("--review-plan")
        if name == "dispatch":
            s.add_argument("--cwd", required=True); s.add_argument("--task-file", required=True)
            s.add_argument("--retry-failed", action="store_true",
                           help="Retry a reviewer slot only after its prior launch exited nonzero.")
            s.add_argument("--approval")
            s.add_argument("--review-authorization", help="Fresh explicit operator authorization for each Qwen profile review launch.")
            s.add_argument("--opencode-manifest", help="Private loopback OpenCode connection manifest for guarded preflight.")
            s.add_argument("--pattern-buffer", "--context-injector", dest="context_injector", help="Pattern Buffer script; --context-injector is a compatibility alias."); s.add_argument("--owner-worktree")
            s.add_argument("--receipt-file", help="Forward a unique Pattern Buffer dispatch snapshot path; requires its script and owner worktree.")
    resume = sub.add_parser("resume", help="Continue a guarded OpenCode session or an exited, verified Codex launch.")
    resume.add_argument("launch"); resume.add_argument("--task-file", required=True)
    resume.add_argument("--opencode-manifest", help="Private loopback OpenCode connection manifest for a guarded continuation.")
    resume.add_argument("--review-plan"); resume.add_argument("--approval")
    resume.add_argument("--review-target"); resume.add_argument("--review-cycle")
    resume.add_argument("--review-authorization", help="Fresh explicit operator authorization for each Qwen profile review continuation.")
    review = sub.add_parser("review-plan"); review.add_argument("--cwd", required=True)
    review.add_argument("--base", required=True); review.add_argument("--facts", required=True)
    review.add_argument("--target"); review.add_argument("--cycle-key"); review.add_argument("--previous-cycle")
    review.add_argument("--full", action="store_true", help="Print every path; default output is a bounded preview of the full saved scope.")
    inspect = sub.add_parser("review-inspect"); inspect.add_argument("plan"); inspect.add_argument("--full", action="store_true")
    approve = sub.add_parser("approve-review"); approve.add_argument("plan")
    approve.add_argument("--authorization", required=True)
    approve.add_argument("--standard-instead", action="store_true",
                         help="Record the configured authority's decision to decline advanced review and use standard reviewers.")
    context = sub.add_parser("context"); context.add_argument("--launch")
    hook = sub.add_parser("hook"); hook.add_argument("--harness", choices=("claude", "codex"), required=True)
    hook_config = sub.add_parser("hook-config"); hook_config.add_argument("--harness", choices=("claude", "codex"), required=True)
    hs = sub.add_parser("handoff-start"); hs.add_argument("name"); hs.add_argument("--brief", required=True)
    hs.add_argument("--roadmap", required=True); hs.add_argument("--cwd", required=True)
    for name in ("handoff-ready", "handoff-transfer", "handoff-close", "handoff-approve-close"):
        s = sub.add_parser(name); s.add_argument("handoff")
        if name == "handoff-ready":
            s.add_argument("--remaining-questions", required=True)
        if name == "handoff-transfer":
            s.add_argument("--outgoing-paused", action="store_true", required=True)
        if name in ("handoff-close", "handoff-approve-close"):
            s.add_argument("--authorization", required=True)
        if name == "handoff-close":
            s.add_argument("--dependent-jobs-cleared", action="store_true", required=True)
    return p


def resolve(snapshot, role, slot):
    choice = snapshot["roles"][role]
    if role.endswith("_review"):
        if slot is None:
            require(len(choice) == 1, "Select reviewer --slot 1 or 2; each is a separate launch.")
            slot = 1
        require(1 <= slot <= len(choice), f"Reviewer --slot {slot} is unavailable; {snapshot['profile']}/{role} has {len(choice)} configured reviewers.")
        choice = choice[slot - 1]
    else:
        require(slot is None, "--slot applies only to reviewer groups.")
    return {"profile": snapshot["profile"], "selection": snapshot["selection"], "role": role, **choice}


def preflight_remediation(code, details=None):
    messages = {
        "profile_config_missing": "Pass the intended existing pilot configuration with --config-dir and its matching isolated path with --state-dir; this command does not create or activate profiles.",
        "profile_config_invalid": "Repair the intended isolated Model Preferences configuration and confirm it with `model-preferences --config-dir PATH validate`.",
        "active_profile_required": "Pass an existing config with the intended active profile using --config-dir and its matching isolated --state-dir; this command never activates or overrides profiles.",
        "selected_role_not_opencode": "Use --config-dir and --state-dir for the intended isolated pilot choices, then inspect it with `model-preferences --config-dir CONFIG --state-dir STATE resolve --role ROLE`; this command cannot override or activate a profile.",
        "invalid_role_slot": "Use --slot only for a configured reviewer group and select an existing slot.",
        "preflight_worktree_unavailable": "Pass an existing Git worktree root for the intended task.",
        "preflight_worktree_root_required": "Pass the exact Git worktree root, not a subdirectory.",
        "invalid_loopback_url": "Use the exact authenticated OpenCode API origin in the private manifest, with an HTTP loopback IP address and no embedded credentials.",
        "loopback_ip_literal_required": "Bind the private manifest to the intended loopback IP address; do not use a hostname or remote provider.",
        "private_manifest_required": "Check the private loopback connection manifest and its mode-0600 owner-only password file; do not change global credentials.",
        "invalid_private_password_file": "Check that the private owner-only password file matches the intended API server; do not change global credentials.",
        "private_password_file_required": "Check the private loopback connection manifest and its mode-0600 owner-only password file; do not change global credentials.",
        "invalid_manifest_json": "Repair the private connection manifest JSON and keep its API URL, expected PID and password-file path explicit.",
        "authenticated_api_challenge_required": "Check that the private manifest origin and password file match the intended authenticated OpenCode server; do not change global credentials.",
        "invalid_expected_server_pid": "Set the expected server PID in the private manifest to the owned OpenCode API process.",
        "unsupported_opencode_version": "Use the supported OpenCode 2.2.5 API server for this preflight.",
        "opencode_server_pid_mismatch": "Confirm the manifest points to the intended owned API process, then update only its private manifest if needed.",
        "manifest_model_mismatch": "Align optional provider/model pins in the private manifest with the exact saved role choice.",
        "unsupported_local_provider_model": "Inspect the exact OpenCode provider id and model slug in the saved role and existing target-worktree settings; do not substitute a catalog model.",
        "exact_model_unavailable": "Inspect the exact existing provider/model configuration and API catalog in the target worktree with read-only commands; do not update a catalog or substitute a model.",
        "model_output_limit_below_maximum_effort": "Check the exact model's reported output allowance; no fallback model is selected.",
        "unsupported_provider_package": "Check that the existing exact provider uses the supported local OpenAI-compatible package; do not install or update packages.",
        "unsupported_model_schema": "Check the pinned OpenCode 2.2.5 model catalog response shape in the exact worktree; unknown schemas are not treated as ready.",
        "unsupported_model_variant_schema": "Inspect the existing model's `max` variant in the target worktree configuration; the preflight does not change variants.",
        "max_reasoning_variant_unverified": "Inspect the existing `max` variant settings in the target worktree; do not replace the saved role choice.",
        "max_reasoning_request_body_unverified": "Inspect the existing model and variant request-body overlays for `reasoning_effort: max`; do not alter them automatically.",
        "model_tool_capability_unverified": "Check the exact provider/model's reported tool capability in the existing target-worktree catalog.",
        "unsupported_provider_schema": "Check the pinned OpenCode 2.2.5 provider response shape and exact provider id in the target worktree.",
        "local_provider_base_url_unavailable": "Confirm the exact saved provider reports its local loopback base URL in the target worktree.",
        "unsupported_plugin_schema": "Inspect the pinned OpenCode 2.2.5 GET /api/plugin response shape for the exact worktree; unknown rows are not treated as active.",
        "required_plugin_not_active": "Resolve the plugin load failure in the exact project and rerun with its API-reported ID; a configured path alone is not proof.",
        "required_plugin_status_ambiguous": "Resolve duplicate API plugin rows for this ID and retry only after the exact project reports one active row.",
        "invalid_required_plugin_id": "Pass the exact nonempty plugin id reported by the OpenCode API.",
        "plugin_status_location_mismatch": "Check the exact worktree and OpenCode project configuration; the API response must report that same directory.",
    }
    if code == "required_plugin_not_active" and isinstance(details, dict) and details.get("unidentified_failed_local_plugins"):
        return ("Inspect the failed local plugin row's source basename, error summary and ref; the API omitted its id, "
                "so it cannot satisfy --require-plugin. Resolve the load failure and rerun after the exact API row "
                "reports active; do not fabricate an id or trust a configured path.")
    if code in messages:
        return messages[code]
    if code.startswith("plugin_status_"):
        return "Check the owned loopback OpenCode API and its exact project plugin endpoint; preserve the typed error and do not infer plugin readiness."
    if code.endswith("_http_timeout") or code in ("http_timeout", "http_unavailable"):
        return "Check that the intended owned loopback OpenCode API is responsive, then rerun this read-only preflight after resolving the cause."
    return "Inspect the typed preflight code against the Model Preferences guide and exact worktree configuration; no session or prompt was created."


def _preflight_output(args, *, status, code=None, details=None, profile=None,
                      choice=None, selection_digest=None, cwd=None, metadata=None, plugins=None):
    required_reading = {
        "purpose": ["OpenCode harness preparation", "parent/child task-brief relay"],
        "guide_url": QWEN_OBSERVER_GUIDE_URL,
        "relay_to_children_before_brief_snapshot": True,
        "comprehension_verified": False,
    }
    guide_path = qwen_observer_guide_path()
    if guide_path:
        required_reading["guide_path"] = guide_path
    result = {
        "kind": "opencode_preflight",
        "status": status,
        "evidence_scope": "read_only_api_metadata",
        "profile": profile,
        "role": args.role,
        "slot": args.slot,
        "selection": selection_digest,
        "worktree": str(cwd) if cwd else None,
        "choice": ({"harness": choice.get("harness"), "provider_id": choice.get("provider_id"),
                    "model": choice.get("model"), "effort": choice.get("effort")} if choice else None),
        "metadata": metadata,
        "required_plugins": plugins or [],
        "required_reading": required_reading,
        "operations": {
            "api_request_methods_allowed": ["GET"],
            "preferences_written": False,
            "session_created": False,
            "surface_created": False,
            "prompt_submitted": False,
            "model_inference_requested": False,
        },
        "native_proof": False,
        "exit_code": 0 if status == "passed" else 2,
    }
    if code:
        result["error"] = {"code": code, "remediation": preflight_remediation(code, details),
                           **({"details": details} if details else {})}
    else:
        result["limits"] = [
            "API metadata and any server-reported plugin status do not prove native TUI hook delivery, permissions, model reception or task success.",
            "No session, CMUX surface, prompt or model inference was started by this command.",
        ]
    return result


def opencode_preflight(app, args):
    profile = None
    active_selection = None
    choice = None
    cwd = None
    try:
        config = app.config()
    except FileNotFoundError:
        return _preflight_output(args, status="held", code="profile_config_missing")
    except (OSError, PreferenceError, ValueError, KeyError, TypeError):
        return _preflight_output(args, status="held", code="profile_config_invalid")
    if config.get("active") is None:
        return _preflight_output(args, status="held", code="active_profile_required")
    try:
        active_selection = selection(config)
        profile = active_selection["profile"]
        choice = resolve(active_selection, args.role, args.slot)
    except PreferenceError:
        return _preflight_output(args, status="held", code="invalid_role_slot", profile=profile)
    if choice.get("harness") != "opencode":
        return _preflight_output(args, status="held", code="selected_role_not_opencode",
                                 profile=profile, choice=choice,
                                 selection_digest=active_selection["selection"])
    try:
        cwd = Path(args.cwd).expanduser().resolve(strict=True)
        if not cwd.is_dir():
            raise OSError("not a directory")
    except (OSError, RuntimeError, TypeError, ValueError):
        return _preflight_output(args, status="held", code="preflight_worktree_unavailable",
                                 profile=profile, choice=choice,
                                 selection_digest=active_selection["selection"])
    try:
        git_root = Path(git(cwd, "rev-parse", "--show-toplevel").decode().strip()).resolve(strict=True)
    except (PreferenceError, OSError, RuntimeError, ValueError):
        return _preflight_output(args, status="held", code="preflight_worktree_unavailable",
                                 profile=profile, choice=choice,
                                 selection_digest=active_selection["selection"], cwd=cwd)
    if git_root != cwd:
        return _preflight_output(args, status="held", code="preflight_worktree_root_required",
                                 profile=profile, choice=choice,
                                 selection_digest=active_selection["selection"], cwd=cwd)
    metadata = None
    try:
        connection = opencode_dispatch.load_connection(args.opencode_manifest)
        evidence = opencode_dispatch.preflight(connection, choice, directory=cwd)
        metadata = {
            "api_contract": evidence.api_contract,
            "api_origin": evidence.api_origin,
            "server_version": evidence.server_version,
            "server_pid": evidence.server_pid,
            "provider_id": evidence.provider_id,
            "model_id": evidence.model_id,
            "variant": evidence.model_variant,
            "output_limit": evidence.output_limit,
            "tools": evidence.tools,
            "local_provider_origin": evidence.local_provider_origin,
        }
        plugins = opencode_dispatch.preflight_plugins(
            connection, directory=cwd, required_plugin_ids=args.require_plugin)
    except opencode_dispatch.OpenCodeDispatchError as exc:
        return _preflight_output(args, status="held", code=exc.code, details=exc.details,
                                 profile=profile, choice=choice,
                                 selection_digest=active_selection["selection"], cwd=cwd,
                                 metadata=metadata)
    return _preflight_output(args, status="passed", profile=profile, choice=choice,
                             selection_digest=active_selection["selection"], cwd=cwd,
                             metadata=metadata, plugins=plugins)


def cmux_uuid(value, field):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise PreferenceError(f"CMUX {field} UUID is unavailable; no OpenCode session created.") from exc


def opencode_destination():
    caller = run_json(["cmux", "--json", "--id-format", "uuids", "identify"]).get("caller") or {}
    workspace = cmux_uuid(caller.get("workspace_id"), "workspace")
    surface = cmux_uuid(caller.get("surface_id"), "surface")
    tree = run_json(["cmux", "--json", "--id-format", "uuids", "tree", "--all"])
    panes = [pane for window in tree.get("windows", []) for ws in window.get("workspaces", [])
             if str(ws.get("id", "")).lower() == workspace.lower()
             for pane in ws.get("panes", [])
             if any(str(item.get("id", "")).lower() == surface.lower() for item in pane.get("surfaces", []))]
    require(len(panes) == 1, "Cannot identify one exact caller pane; no OpenCode session created.")
    return {"workspace_uuid": workspace, "surface_uuid": surface, "pane_id": panes[0]["id"]}


def opencode_parent_caller(app, connection, owner_worktree, destination):
    launch_kind = os.environ.get("CMUX_AGENT_LAUNCH_KIND", "").strip().casefold()
    session = os.environ.get("OPENCODE_SESSION_ID")
    owner_worktree = str(Path(owner_worktree).expanduser().resolve(strict=True))
    if launch_kind != "opencode":
        require(not session, "OPENCODE_SESSION_ID is present without the OpenCode launch kind; refusing inherited identity.")
        parent_harness = ("claude" if launch_kind in ("claude", "claudeteams") else
                          "codex" if launch_kind == "codex" else None)
        if parent_harness is None:
            require(not os.environ.get("CODEX_THREAD_ID") and not os.environ.get("CLAUDE_CODE_SESSION_ID"),
                    "A native parent session ID is present without its recognized launch kind.")
            return None
        session_name = "CLAUDE_CODE_SESSION_ID" if parent_harness == "claude" else "CODEX_THREAD_ID"
        expected_session = os.environ.get(session_name)
        caller = app.caller()
        require(isinstance(expected_session, str) and expected_session
                and caller.get("harness") == parent_harness and caller.get("session") == expected_session,
                "OpenCode dispatch parent does not match the exact current Codex or Claude session.")
        try:
            session_surface = cmux_uuid(caller.get("surface"), "parent surface")
            session_workspace = cmux_uuid(caller.get("workspace"), "parent workspace")
        except PreferenceError as exc:
            raise PreferenceError(f"Native parent environment is incomplete: {exc}") from None
        require(session_surface == destination["surface_uuid"] and session_workspace == destination["workspace_uuid"],
                "Native parent does not match the exact CMUX caller surface and workspace.")
        return {"harness": parent_harness, "session_id": expected_session,
                "workspace_uuid": session_workspace, "surface_uuid": session_surface,
                "owner_worktree": owner_worktree}
    require(isinstance(session, str) and session.startswith("ses_"),
            "OpenCode child dispatch needs the exact current OPENCODE_SESSION_ID.")
    try:
        session_surface = cmux_uuid(os.environ.get("CMUX_SURFACE_ID"), "surface")
        session_workspace = cmux_uuid(os.environ.get("CMUX_WORKSPACE_ID"), "workspace")
    except PreferenceError as exc:
        raise PreferenceError(f"OpenCode parent environment is incomplete: {exc}") from None
    require(session_surface == destination["surface_uuid"] and session_workspace == destination["workspace_uuid"],
            "OpenCode parent environment does not match the exact CMUX caller surface and workspace.")
    current = app.current()
    native = opencode_dispatch.require_native_dispatch_binding(
        expected_workspace=session_workspace, expected_surface=session_surface,
        expected_session=session, current=current)
    require(native["state"] == "working",
            "Only the currently executing exact OpenCode orchestrator may start a child session.")
    launches = app.state_dir / "launches"
    matches = []
    if launches.is_dir():
        for path in launches.glob("*.json"):
            record = read(path)
            if (record.get("choice", {}).get("harness") == "opencode"
                    and record.get("native_session") == session
                    and record.get("parent_task_id") == connection.parent_task_id
                    and record.get("worktree_directory") == owner_worktree
                    and record.get("cmux_workspace_uuid") == session_workspace
                    and record.get("cmux_surface_uuid") == session_surface
                    and record.get("task_id") == connection.parent_task_id
                    and record.get("choice", {}).get("profile") == "Qwen-only"
                    and record.get("choice", {}).get("role") == "orchestrator"
                    and record.get("status") == "running"):
                matches.append(record)
    require(len(matches) == 1, "OpenCode parent session has no unique active Model Preferences launch binding.")
    parent = matches[0]
    active_continuation = app.state(f"continuations/{session}", None)
    startup = parent.get("startup_turn")
    active_task = (isinstance(startup, dict) and startup.get("state") == "running"
                   and isinstance(startup.get("user_message_id"), str))
    task_message_id = startup.get("user_message_id") if active_task else None
    task_prompt_sha256 = parent.get("startup_prompt_sha256") if active_task else None
    if not active_task and isinstance(active_continuation, dict) and active_continuation.get("status") == "submitted":
        task_message_id = active_continuation.get("message_id")
        task_prompt_sha256 = active_continuation.get("task_digest")
        active_task = isinstance(task_message_id, str)
    require(active_task,
            "OpenCode orchestrator has no admitted active Model Preferences task; no child created.")
    try:
        latest_user = opencode_dispatch.latest_user_message_matches(
            connection, session_id=session, message_id=task_message_id, text_sha256=task_prompt_sha256)
    except opencode_dispatch.OpenCodeDispatchError as exc:
        raise PreferenceError(f"OpenCode parent task admission held: {exc.code}.") from None
    require(latest_user,
            "The current OpenCode parent prompt does not match the exact admitted task; no child created.")
    if parent.get("native_started_at") is not None:
        opencode_dispatch.require_native_hook_binding(
            expected_workspace=session_workspace, expected_surface=session_surface,
            expected_session=session, worktree_directory=owner_worktree,
            expected_started_at=parent["native_started_at"])
    try:
        opencode_dispatch.verify_session(
            connection, session, expected_directory=owner_worktree, choice=parent["choice"],
            launch_id=parent["id"], task_digest=parent["task_digest"],
            parent_task_id=parent["parent_task_id"],
            parent_session_id=parent.get("opencode_parent_session_id"))
        opencode_dispatch.require_no_pending_interactions(connection, session)
    except opencode_dispatch.OpenCodeDispatchError as exc:
        raise PreferenceError(f"OpenCode parent API identity held: {exc.code}.") from None
    context_adapter = parent.get("context_injector")
    require(isinstance(context_adapter, dict) and context_adapter.get("role") == "orchestrator"
            and context_adapter.get("session_id") == session,
            "OpenCode parent lacks its exact Pattern Buffer orchestrator assignment.")
    context = app.run_json([sys.executable, context_adapter["script"], "--cwd", owner_worktree, "context",
                            "--harness", "opencode", "--session", session])
    assignment_sha256 = parent.get("pattern_buffer_assignment_sha256")
    require(context.get("status") == "context" and context.get("harness") == "opencode"
            and context.get("session_id") == session and context.get("worktree") == owner_worktree
            and isinstance(assignment_sha256, str) and len(assignment_sha256) == 64
            and context.get("assignment_sha256") == assignment_sha256,
            "OpenCode parent Pattern Buffer assignment changed or is unavailable.")
    return {"harness": "opencode", "session_id": session, "workspace_uuid": session_workspace,
            "surface_uuid": session_surface, "started_at": parent.get("native_started_at"),
            "task_id": parent["task_id"], "launch_id": parent["id"],
            "owner_worktree": owner_worktree,
            "owner_gitdir": git(owner_worktree, "rev-parse", "--absolute-git-dir").decode().strip(),
            "assignment_sha256": assignment_sha256}


def prepare_opencode_pattern_buffer(app, args, cwd, task_digest, parent):
    require(args.context_injector and args.owner_worktree and args.receipt_file,
            "OpenCode Pattern Buffer dispatch requires --pattern-buffer, --owner-worktree and a unique --receipt-file.")
    require(args.role == "implementer", "OpenCode Pattern Buffer dispatch is available for implementer only.")
    require(parent is None or (isinstance(parent, dict)
            and parent.get("harness") in ("opencode", "codex", "claude")),
            "Pattern Buffer OpenCode dispatch requires a verified OpenCode, Codex or Claude parent.")
    script = Path(args.context_injector).expanduser().resolve(strict=True)
    require(script.is_file(), "Pattern Buffer script is unavailable.")
    owner = Path(args.owner_worktree).expanduser().resolve(strict=True)
    if parent is not None:
        require(str(owner) == parent["owner_worktree"],
                "The child owner must match the verified parent worktree.")
    require(str(cwd) != str(owner), "Pattern Buffer OpenCode child must use a separate Git worktree.")
    owner_gitdir = git(owner, "rev-parse", "--absolute-git-dir").decode().strip()
    receipt_path = Path(args.receipt_file).expanduser().absolute()
    require(not receipt_path.exists() and not receipt_path.is_symlink(), "Pattern Buffer receipt path must be new and unique.")
    command = [sys.executable, str(script), "--cwd", str(cwd), "prepare-dispatch",
               "--harness", "opencode", "--owner-worktree", str(owner),
               "--task-file", str(Path(args.task_file).resolve()),
               "--receipt-file", str(receipt_path), "--role", "implementer"]
    open_parent = parent if parent is not None and parent["harness"] == "opencode" else None
    if open_parent:
        command.extend(["--parent-task-id", open_parent["task_id"]])
    prepared = app.run_json(command)
    receipt = read(receipt_path)
    expected_parent = ({"harness": "opencode", "session_id": open_parent["session_id"],
                       "task_id": open_parent["task_id"], "role": "orchestrator",
                       "owner_worktree": open_parent["owner_worktree"],
                       "owner_gitdir": open_parent["owner_gitdir"],
                       "surface_id": open_parent["surface_uuid"].upper(),
                       "workspace_id": open_parent["workspace_uuid"].upper(),
                       "assignment_sha256": open_parent["assignment_sha256"]}
                       if open_parent else None)
    expected_parent_kinds = ({"opencode"} if open_parent else
                             ({parent["harness"], "worktree"} if parent else {"worktree"}))
    require(prepared.get("status") == "pending" and prepared.get("harness") == "opencode"
            and prepared.get("role") == "implementer" and prepared.get("task_id") == receipt.get("task_id")
            and prepared.get("ticket_version") == 2
            and prepared.get("parent_kind") in expected_parent_kinds
            and ((prepared.get("parent") == expected_parent) if expected_parent is not None
                 else "parent" not in prepared),
            "Pattern Buffer did not return the prepared OpenCode ticket; no session created.")
    require(receipt.get("harness") == "opencode" and receipt.get("role") == "implementer"
            and receipt.get("status") == "pending" and receipt.get("session_id") is None
            and receipt.get("worktree") == str(cwd) and receipt.get("owner_worktree") == str(owner)
            and receipt.get("owner_gitdir") == owner_gitdir
            and receipt.get("brief_sha256") == task_digest and receipt.get("ticket_version") == 2
            and receipt.get("parent_kind") == prepared.get("parent_kind")
            and ((receipt.get("parent") == expected_parent) if expected_parent is not None
                 else "parent" not in receipt),
            "Pattern Buffer ticket does not match the exact task, worktree and owner; no session created.")
    task_id = safe_id(receipt.get("task_id"))
    return {"script": str(script), "owner_worktree": str(owner), "receipt": str(receipt_path),
            "owner_gitdir": owner_gitdir, "task_id": task_id, "parent": expected_parent,
            "parent_kind": receipt["parent_kind"]}


def assign_opencode_orchestrator_context(app, args, cwd, session):
    require(args.context_injector and not args.owner_worktree and not args.receipt_file,
            "A fresh OpenCode orchestrator requires --pattern-buffer and an independent orchestrator assignment, without child-task flags.")
    script = Path(args.context_injector).expanduser().resolve(strict=True)
    require(script.is_file(), "Pattern Buffer script is unavailable.")
    cli = [sys.executable, str(script), "--cwd", str(cwd)]
    try:
        app.run_command([*cli, "assign", "--harness", "opencode", "--session", session,
                         "--role", "orchestrator"])
        context = app.run_json([*cli, "context", "--harness", "opencode", "--session", session])
    except PreferenceError as exc:
        raise PreferenceError(f"Pattern Buffer orchestrator context failed: {exc}") from None
    require(context.get("status") == "context" and context.get("harness") == "opencode"
            and context.get("session_id") == session and context.get("worktree") == str(cwd)
            and isinstance(context.get("text"), str)
            and context["text"].startswith("pattern-buffer | role: orchestrator |"),
            "Pattern Buffer did not confirm the exact OpenCode orchestrator assignment; no prompt sent.")
    assignment_sha256 = context.get("assignment_sha256")
    require(isinstance(assignment_sha256, str) and len(assignment_sha256) == 64
            and all(char in "0123456789abcdef" for char in assignment_sha256),
            "Pattern Buffer did not return exact orchestrator assignment proof; no prompt sent.")
    return {"script": str(script), "role": "orchestrator", "session_id": session,
            "assignment_sha256": assignment_sha256}


def bind_opencode_pattern_buffer(app, adapter, session):
    receipt = read(adapter["receipt"])
    cli = [sys.executable, adapter["script"], "--cwd", receipt["worktree"]]
    bound = app.run_json([*cli, "bind-dispatch", "--harness", "opencode", "--session", session,
                          "--task-id", adapter["task_id"], "--receipt-file", adapter["receipt"],
                          "--role", "implementer"])
    require(bound.get("status") == "active" and bound.get("task_id") == adapter["task_id"]
            and bound.get("session_id") == session and bound.get("harness") == "opencode"
            and bound.get("role") == "implementer",
            "Pattern Buffer did not confirm the exact OpenCode session binding; no prompt sent.")
    context = app.run_json([*cli, "context", "--harness", "opencode", "--session", session])
    assignment_sha256 = context.get("assignment_sha256")
    require(context.get("status") == "context" and context.get("harness") == "opencode"
            and context.get("session_id") == session and context.get("worktree") == receipt["worktree"]
            and isinstance(context.get("text"), str)
            and context["text"].startswith("pattern-buffer | role: implementer |")
            and isinstance(assignment_sha256, str) and len(assignment_sha256) == 64
            and all(char in "0123456789abcdef" for char in assignment_sha256),
            "Pattern Buffer did not confirm the exact active implementer context; no prompt sent.")
    return assignment_sha256


def update_opencode_launch(app, launch):
    with locked(app.config_dir):
        app.save_state(f"launches/{launch['id']}", launch)


def signal_private(path, value):
    try:
        opencode_dispatch.write_private_json(path, value)
    except opencode_dispatch.OpenCodeDispatchError as exc:
        raise PreferenceError(f"Private OpenCode launch signal failed: {exc.code}.") from None


def run_cmux_rpc(method, **params):
    require(isinstance(method, str) and method in ("surface.input_state", "agent.message.settings"),
            "Unsupported CMUX guard operation.")
    value = run_json(["cmux", "--json", "rpc", method, json.dumps(params, separators=(",", ":"))])
    require(isinstance(value, dict), "CMUX native guard response is unavailable.")
    return value


NATIVE_STARTUP_BINDING_TIMEOUT_SECONDS = 180


def _native_startup_clock():
    return time.monotonic()


def _native_startup_pause():
    time.sleep(1)


def _native_hook_observation_pending(session):
    """Return true only while the exact session's native hook record is absent."""
    path = Path(opencode_dispatch.native_hook_store_path())
    try:
        value = opencode_dispatch.read_private_json(path, maximum=2 * 1024 * 1024)
    except opencode_dispatch.OpenCodeDispatchError as exc:
        if exc.code != "private_json_file_invalid":
            raise
        try:
            path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            raise opencode_dispatch.OpenCodeDispatchError("native_hook_store_unavailable") from None
        raise opencode_dispatch.OpenCodeDispatchError("native_hook_store_malformed") from None
    sessions = value.get("sessions")
    if not isinstance(sessions, dict):
        raise opencode_dispatch.OpenCodeDispatchError("native_hook_store_malformed")
    if session not in sessions:
        return True
    if not isinstance(sessions[session], dict):
        raise opencode_dispatch.OpenCodeDispatchError("native_hook_identity_malformed")
    return False


def _bounded_native_value(value, allowed):
    if value is None:
        return None
    return value if isinstance(value, str) and value in allowed else "unrecognized"


def _bounded_startup_reason(value, fallback="startup_observation_unclassified"):
    if (isinstance(value, str) and value and len(value) <= 96 and value.isascii()
            and all(char.islower() or char.isdigit() or char == "_" for char in value)):
        return value
    return fallback


def _native_startup_diagnostic(current, expected_session, expected_workspace, expected_surface):
    states = ("working", "idle", "needs_input", "unknown")
    lifecycles = ("running", "idle", "backgroundWorkPending")
    freshness_states = ("current", "stale")
    input_states = ("unknown", "empty", "draft", "needs_input")
    diagnostic = {"schema": "cmux-current-opencode-v1", "status": "unknown",
                  "observation_truncated": None, "agent_sessions_available": False,
                  "items_observed": 0, "items_total_capped": 0, "items_truncated": False,
                  "agent_lists_truncated": False, "candidate_count_capped": 0,
                  "candidates_truncated": False, "candidates": []}
    if not isinstance(current, dict):
        diagnostic["status"] = "observation_unavailable"
        return diagnostic
    diagnostic["observation_truncated"] = (current.get("truncated")
                                           if isinstance(current.get("truncated"), bool) else None)
    availability = current.get("owner_availability")
    diagnostic["agent_sessions_available"] = (
        isinstance(availability, dict) and availability.get("agent_sessions") == "available")
    items = current.get("items")
    if not isinstance(items, list):
        diagnostic["status"] = "items_schema_unknown"
        return diagnostic
    scan_limit = 256
    diagnostic["status"] = "observed"
    diagnostic["items_observed"] = min(len(items), scan_limit)
    diagnostic["items_total_capped"] = min(len(items), 9999)
    diagnostic["items_truncated"] = len(items) > scan_limit
    for item in items[:scan_limit]:
        if not isinstance(item, dict):
            continue
        agents = item.get("agents")
        if not isinstance(agents, list):
            continue
        if len(agents) > 64:
            diagnostic["agent_lists_truncated"] = True
        projections = item.get("projections")
        projections = projections if isinstance(projections, list) else []
        expected_workspace_match = any(
            isinstance(projection, dict) and isinstance(projection.get("workspace_id"), str)
            and projection["workspace_id"].casefold() == expected_workspace.casefold()
            for projection in projections)
        expected_surface_match = any(
            isinstance(projection, dict) and isinstance(projection.get("panel_id"), str)
            and projection["panel_id"].casefold() == expected_surface.casefold()
            for projection in projections)
        for agent in agents[:64]:
            if (not isinstance(agent, dict) or agent.get("kind") != "opencode"
                    or agent.get("session_id") != expected_session):
                continue
            diagnostic["candidate_count_capped"] = min(diagnostic["candidate_count_capped"] + 1, 9999)
            if len(diagnostic["candidates"]) < 8:
                diagnostic["candidates"].append({
                    "native_state": _bounded_native_value(agent.get("state"), states),
                    "native_lifecycle": _bounded_native_value(item.get("lifecycle"), lifecycles),
                    "native_freshness": _bounded_native_value(
                        item.get("freshness", {}).get("state")
                        if isinstance(item.get("freshness"), dict) else None, freshness_states),
                    "native_input_state": _bounded_native_value(agent.get("input_state"), input_states),
                    "expected_workspace_match": expected_workspace_match,
                    "expected_surface_match": expected_surface_match})
    diagnostic["candidates_truncated"] = diagnostic["candidate_count_capped"] > len(diagnostic["candidates"])
    if diagnostic["candidate_count_capped"] == 1:
        diagnostic.update(diagnostic["candidates"][0])
    return diagnostic


def dispatch_opencode(app, args, config, choice, cwd, task_bytes):
    role = choice["role"]
    require(role in ("orchestrator", "implementer", "researcher"),
            "OpenCode dispatch supports fresh orchestration, implementation and research roles only.")
    if role == "orchestrator":
        require(choice["profile"] == "Qwen-only" and args.context_injector
                and not args.owner_worktree and not args.receipt_file,
                "A fresh Qwen-only orchestrator requires --pattern-buffer and cannot replace an existing owner.")
    require(args.opencode_manifest, "OpenCode dispatch requires --opencode-manifest with a mode-0600 loopback API connection.")
    try:
        connection = opencode_dispatch.load_connection(args.opencode_manifest)
        require(connection.parent_task_id, "OpenCode manifest must name the explicit parent_task_id.")
        require(Path(git(cwd, "rev-parse", "--show-toplevel").decode().strip()).resolve() == cwd,
                "OpenCode --cwd must be the exact Git worktree root so session and worktree identity remain distinct and verifiable.")
        # Confirm the exact provider/model/output/variant contract before creating a server-side session.
        evidence = opencode_dispatch.preflight(connection, choice, directory=str(cwd))
        binary = opencode_dispatch.check_opencode_cli()
        destination = opencode_destination()
    except opencode_dispatch.OpenCodeDispatchError as exc:
        raise PreferenceError(f"OpenCode preflight held: {exc.code}") from None
    wants_child_ticket = bool(args.owner_worktree or args.receipt_file)
    if role == "orchestrator":
        require(not wants_child_ticket, "A fresh orchestrator receives its own role assignment, not a child receipt.")
        adapter = None
        parent = None
    elif args.context_injector or wants_child_ticket:
        require(args.context_injector and args.owner_worktree and args.receipt_file,
                "OpenCode child dispatch requires --pattern-buffer, --owner-worktree and --receipt-file together.")
        try:
            owner_worktree = Path(args.owner_worktree).expanduser().resolve(strict=True)
            parent = opencode_parent_caller(app, connection, owner_worktree, destination)
        except (PreferenceError, opencode_dispatch.OpenCodeDispatchError) as exc:
            raise PreferenceError(f"OpenCode child parent proof held: {getattr(exc, 'code', str(exc))}.") from None
        adapter = prepare_opencode_pattern_buffer(app, args, cwd, hashlib.sha256(task_bytes).hexdigest(), parent)
    else:
        require(not args.owner_worktree and not args.receipt_file,
                "--owner-worktree and --receipt-file require --pattern-buffer.")
        adapter = None
        parent = None
    opencode_parent = parent if parent is not None and parent.get("harness") == "opencode" else None

    brief = task_bytes.decode("utf-8")
    if role == "orchestrator":
        cli = shlex.join([sys.executable, str(Path(__file__).resolve()),
                          "--config-dir", str(app.config_dir.resolve()),
                          "--state-dir", str(app.state_dir.resolve())])
        prompt = ("You are a fresh Qwen-only orchestrator for the exact task below. Your native OpenCode session, "
                  "Pattern Buffer orchestrator assignment, worktree, task reference and CMUX surface are bound separately. "
                  "Keep the task within this owner worktree. For bounded implementation work, create a separate Git worktree "
                  "and dispatch an implementer using the current private OpenCode manifest and Pattern Buffer path from "
                  "MODEL_PREFERENCES_OPENCODE_MANIFEST and MODEL_PREFERENCES_PATTERN_BUFFER. Use the exact current owner "
                  "worktree and a unique receipt file, then verify the returned child session and receipt before delegating. "
                  "Do not launch a successor owner or change shared profile activation. Every review launch requires a fresh "
                  "operator authorization; do not infer or reuse one. Use the configured external Codex Sol high reviewer "
                  "only after that authorization.\n\n"
                  f"Dispatch command prefix: {cli}\n"
                  f"For a child, run `{cli} dispatch --role implementer --cwd CHILD_WORKTREE --task-file BRIEF "
                  f"--pattern-buffer \"$MODEL_PREFERENCES_PATTERN_BUFFER\" --owner-worktree {shlex.quote(str(cwd))} "
                  "--receipt-file UNIQUE_RECEIPT "
                  "--opencode-manifest \"$MODEL_PREFERENCES_OPENCODE_MANIFEST\"`.\n\n"
                  f"{brief}")
    else:
        prompt = DELEGATION + (" Use the current researcher role for authorized research. Verify its findings.\n\n"
                               if role == "researcher" else " Follow the exact bounded implementation task.\n\n") + brief
    prompt_bytes = prompt.encode("utf-8")
    require(0 < len(prompt_bytes) <= 32768, "OpenCode startup prompt must be at most 32768 UTF-8 bytes.")
    launch_id = uuid.uuid4().hex
    task_digest = hashlib.sha256(task_bytes).hexdigest()
    prompt_file = app.state_dir / "opencode-launches" / f"{launch_id}.prompt"
    handshake_file = app.state_dir / "opencode-launches" / f"{launch_id}.handshake.json"
    release_file = app.state_dir / "opencode-launches" / f"{launch_id}.release.json"
    abort_file = app.state_dir / "opencode-launches" / f"{launch_id}.abort.json"
    child_descriptor_file = app.state_dir / "opencode-launches" / f"{launch_id}.child.json"
    helper = Path(__file__).resolve().with_name("opencode_dispatch.py")
    try:
        child_xdg_environment = opencode_dispatch.caller_private_xdg_environment()
        child_command = opencode_dispatch.tui_child_command(
            helper, child_descriptor_file.resolve(), sys.executable)
    except opencode_dispatch.OpenCodeDispatchError as exc:
        raise PreferenceError(f"OpenCode launch preparation held: {exc.code}.") from None
    launch = {"id": launch_id, "choice": choice, "cwd": str(cwd), "created_at": now(),
              "context_injector": adapter, "slot": args.slot, "task_digest": task_digest,
              "status": "session_create_pending", "api_contract": evidence.api_contract,
              "api_origin": evidence.api_origin, "expected_server_pid": evidence.server_pid,
              "delivery_policy": connection.delivery_policy,
              "parent_task_id": connection.parent_task_id, "caller_workspace_uuid": destination["workspace_uuid"],
              "caller_surface_uuid": destination["surface_uuid"], "session_directory": str(cwd),
              "worktree_directory": str(cwd), "task_id": adapter["task_id"] if adapter else connection.parent_task_id,
              "opencode_parent_session_id": opencode_parent["session_id"] if opencode_parent else None,
              "parent_launch_id": opencode_parent["launch_id"] if opencode_parent else None,
              "prompt_snapshot": str(prompt_file),
              "startup_prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest()}

    with locked(app.config_dir):
        active_launches = app.state_dir / "launches"
        if active_launches.is_dir():
            for path in active_launches.glob("*.json"):
                previous = read(path)
                if (previous.get("choice", {}).get("harness") == "opencode"
                        and previous.get("cwd") == str(cwd) and previous.get("task_digest") == task_digest
                        and previous.get("choice", {}).get("selection") == choice["selection"]
                        and previous.get("status") in ("session_create_pending", "session_created",
                                                        "session_move_pending", "task_bound",
                                                        "surface_opening", "native_binding_pending", "uncertain", "running")):
                    raise PreferenceError(f"Matching OpenCode launch {previous.get('id')} is unresolved or live; inspect it before retrying.")

        try:
            opencode_dispatch.write_private_snapshot(prompt_file, prompt_bytes)
        except opencode_dispatch.OpenCodeDispatchError as exc:
            raise PreferenceError(f"OpenCode launch preparation failed: {exc.code}.") from None
        app.save_state(f"launches/{launch['id']}", launch)
    creation_attempted = False
    surface_opened = False
    last_startup_readiness = None
    startup_observation_history = []
    startup_poll_count = 0
    startup_wait_count = 0
    startup_classification_count = 0
    startup_first_wait_at = None
    startup_last_wait_at = None
    startup_started_at = None
    startup_started_monotonic = None

    def classify_startup(classification, reason, current=None, turn=None, *, persist=True):
        nonlocal last_startup_readiness, startup_wait_count, startup_classification_count
        nonlocal startup_first_wait_at, startup_last_wait_at
        reason = _bounded_startup_reason(reason)
        observed_at = now()
        startup_classification_count = min(
            startup_classification_count + 1, NATIVE_STARTUP_BINDING_TIMEOUT_SECONDS + 2)
        if classification == "waiting":
            startup_wait_count = min(startup_wait_count + 1, NATIVE_STARTUP_BINDING_TIMEOUT_SECONDS + 1)
            startup_first_wait_at = startup_first_wait_at or observed_at
            startup_last_wait_at = observed_at
        turn_state = _bounded_native_value(
            turn.get("state") if isinstance(turn, dict) else None,
            ("not_admitted", "running", "complete", "failed"))
        sample = {"classification": classification, "reason": reason,
                  "observed_at": observed_at,
                  "elapsed_seconds": min(float(NATIVE_STARTUP_BINDING_TIMEOUT_SECONDS),
                                         round(max(0.0, _native_startup_clock() - startup_started_monotonic), 3))
                  if startup_started_monotonic is not None else 0.0,
                  "poll_number": min(startup_poll_count, NATIVE_STARTUP_BINDING_TIMEOUT_SECONDS + 1),
                  "wait_number": startup_wait_count,
                  "startup_turn_state": turn_state,
                  "native_observation": _native_startup_diagnostic(current, session, workspace, surface)}
        startup_observation_history.append(sample)
        history_limit = NATIVE_STARTUP_BINDING_TIMEOUT_SECONDS + 2
        if len(startup_observation_history) > history_limit:
            del startup_observation_history[0]
        last_startup_readiness = {
            "schema_version": 2,
            "state": classification,
            "reason": reason,
            "observed_at": observed_at,
            "started_at": startup_started_at,
            "elapsed_seconds": sample["elapsed_seconds"],
            "poll_count": sample["poll_number"],
            "wait_count": startup_wait_count,
            "classification_count": startup_classification_count,
            "first_wait_at": startup_first_wait_at,
            "last_wait_at": startup_last_wait_at,
            "startup_turn_state": turn_state,
            "native_observation": sample["native_observation"],
            "classifications": list(startup_observation_history)}
        if persist:
            launch["startup_readiness_observation"] = last_startup_readiness
            update_opencode_launch(app, launch)
        return last_startup_readiness

    try:
        # Recheck immediately before creation in case provider configuration or server identity changed.
        evidence = opencode_dispatch.preflight(connection, choice, directory=str(cwd))
        if parent:
            parent_recheck = opencode_parent_caller(app, connection, owner_worktree=Path(parent["owner_worktree"]),
                                                    destination=destination)
            require(parent_recheck == parent,
                    "OpenCode parent session, assignment or native generation changed after Pattern Buffer preparation.")
        creation_attempted = True
        created = opencode_dispatch.create_session(connection, directory=str(cwd), choice=choice,
                                                   launch_id=launch_id, task_digest=task_digest,
                                                   parent_task_id=connection.parent_task_id,
                                                   parent_session_id=opencode_parent["session_id"] if opencode_parent else None)
        session = created["session_id"]
        launch.update(status="session_created", native_session=session,
                      opencode_parent_session_id=created.get("parent_session_id"),
                      model=created["model"], session_directory=created["directory"])
        update_opencode_launch(app, launch)
        if created["directory"] != str(cwd):
            launch.update(status="session_move_pending",
                          session_move_source_directory=created["directory"],
                          session_move_target_directory=str(cwd))
            update_opencode_launch(app, launch)
            moved = opencode_dispatch.move_session(
                connection, session, source_directory=created["directory"], directory=str(cwd),
                choice=choice, launch_id=launch_id, task_digest=task_digest,
                parent_task_id=connection.parent_task_id,
                parent_session_id=opencode_parent["session_id"] if opencode_parent else None)
            launch.update(status="session_created", session_directory=moved["directory"],
                          session_move_confirmed_at=now())
            update_opencode_launch(app, launch)
        if role == "orchestrator":
            assignment = assign_opencode_orchestrator_context(app, args, cwd, session)
            launch["context_injector"] = assignment
            launch["pattern_buffer_assignment_sha256"] = assignment["assignment_sha256"]
            update_opencode_launch(app, launch)
        if adapter:
            launch["status"] = "task_binding_pending"
            update_opencode_launch(app, launch)
            assignment_sha256 = bind_opencode_pattern_buffer(app, adapter, session)
            launch["status"] = "task_bound"
            launch["pattern_buffer_task_id"] = adapter["task_id"]
            launch["pattern_buffer_owner_worktree"] = adapter["owner_worktree"]
            launch["pattern_buffer_assignment_sha256"] = assignment_sha256
            update_opencode_launch(app, launch)

        child_descriptor = {
            "schema_version": opencode_dispatch.TUI_CHILD_DESCRIPTOR_SCHEMA,
            "manifest_path": str(Path(args.opencode_manifest).expanduser().resolve()),
            "session_id": session,
            "directory": str(cwd),
            "prompt_file": str(prompt_file.resolve()),
            "provider_id": choice["provider_id"],
            "model_id": choice["model"],
            "launch_id": launch_id,
            "selection": choice["selection"],
            "task_digest": task_digest,
            "xdg_environment": child_xdg_environment,
            "handshake_file": str(handshake_file.resolve()),
            "release_file": str(release_file.resolve()),
            "abort_file": str(abort_file.resolve()),
            "binary": binary,
        }
        if opencode_parent:
            child_descriptor["parent_session_id"] = opencode_parent["session_id"]
        try:
            opencode_dispatch.write_tui_child_descriptor(child_descriptor_file, child_descriptor)
        except opencode_dispatch.OpenCodeDispatchError as exc:
            raise PreferenceError(f"OpenCode launch preparation held: {exc.code}.") from None
        launch["status"] = "surface_opening"
        update_opencode_launch(app, launch)
        surface_opened = True
        opened = run_json(["cmux", "--json", "--id-format", "uuids", "new-surface", "--type", "terminal",
                           "--workspace", destination["workspace_uuid"], "--pane", destination["pane_id"],
                           "--working-directory", str(cwd), "--focus", "false", "--command", child_command])
        surface = cmux_uuid(opened.get("surface_id"), "new surface")
        workspace = cmux_uuid(opened.get("workspace_id", destination["workspace_uuid"]), "new workspace")
        require(workspace.lower() == destination["workspace_uuid"].lower(),
                "OpenCode surface opened in a different CMUX workspace; prompt remains held.")
        launch.update(status="native_binding_pending", cmux_workspace_uuid=workspace, cmux_surface_uuid=surface,
                      surface_opened_at=now())
        update_opencode_launch(app, launch)

        try:
            deadline = time.monotonic() + 15
            handshake = None
            while time.monotonic() < deadline:
                if handshake_file.exists():
                    handshake = opencode_dispatch.read_private_json(handshake_file)
                    break
                time.sleep(0.1)
            expected_handshake = {"launch_id": launch_id, "session_id": session,
                                  "workspace_uuid": workspace, "surface_uuid": surface}
            require(isinstance(handshake, dict)
                    and all(handshake.get(k) == v for k, v in expected_handshake.items()),
                    "OpenCode child CMUX handshake did not match the exact new surface; initial prompt was not released.")
            socket_path = handshake.get("cmux_socket_path")
            capability = handshake.get("cmux_socket_capability")
            opencode_dispatch.set_session_environment(
                connection, session, workspace_uuid=workspace, surface_uuid=surface, launch_id=launch_id,
                cmux_socket_path=socket_path, cmux_socket_capability=capability,
                model_preferences_manifest=str(Path(args.opencode_manifest).expanduser().resolve()),
                pattern_buffer_script=adapter["script"] if adapter else
                (launch.get("context_injector", {}).get("script") if role == "orchestrator" else None),
                task_id=launch["task_id"], parent_task_id=opencode_parent["task_id"] if opencode_parent else None)
            signal_private(release_file, expected_handshake)
        finally:
            try:
                handshake_file.unlink()
            except OSError:
                pass

        launch["status"] = "native_binding_pending"
        launch["initial_prompt_released_at"] = now()
        update_opencode_launch(app, launch)
        startup_started_at = now()
        startup_started_monotonic = _native_startup_clock()
        deadline = startup_started_monotonic + NATIVE_STARTUP_BINDING_TIMEOUT_SECONDS
        last_startup_current = None
        last_startup_turn = None
        while _native_startup_clock() < deadline:
            startup_poll_count = min(startup_poll_count + 1, NATIVE_STARTUP_BINDING_TIMEOUT_SECONDS + 1)
            try:
                current = app.current()
            except Exception as exc:
                reason = (exc.code if isinstance(exc, opencode_dispatch.OpenCodeDispatchError)
                          else "native_current_observation_unavailable")
                classify_startup("held", reason)
                raise
            last_startup_current = current
            try:
                native = opencode_dispatch.require_native_dispatch_binding(
                    expected_workspace=workspace, expected_surface=surface,
                    expected_session=session, current=current, allow_startup_unknown=True)
            except opencode_dispatch.OpenCodeDispatchError as exc:
                if exc.code == "native_session_surface_binding_pending":
                    classify_startup("waiting", exc.code, current)
                    _native_startup_pause()
                    continue
                classify_startup("held", exc.code, current)
                raise
            if (native["state"] == "needs_input"
                    or native.get("input_state") not in (None, "unknown", "empty")):
                classify_startup("held", "native_session_state_unknown", current)
                raise opencode_dispatch.OpenCodeDispatchError("native_session_state_unknown")
            try:
                turn = opencode_dispatch.startup_turn_observation(
                    connection, session_id=session, prompt_sha256=launch["startup_prompt_sha256"],
                    provider_id=choice["provider_id"], model_id=choice["model"])
            except opencode_dispatch.OpenCodeDispatchError as exc:
                classify_startup("held", exc.code, current)
                raise
            last_startup_turn = turn
            if not isinstance(turn, dict) or turn.get("state") not in ("not_admitted", "running", "complete", "failed"):
                classify_startup("held", "startup_turn_observation_schema_unknown", current, turn)
                raise opencode_dispatch.OpenCodeDispatchError("startup_turn_observation_schema_unknown")
            if turn["state"] == "not_admitted":
                classify_startup("waiting", "startup_prompt_not_admitted", current, turn)
                _native_startup_pause()
                continue
            if turn["state"] == "running":
                if role == "orchestrator" and native["state"] == "working":
                    native_generation = None
                    try:
                        hook = opencode_dispatch.require_native_hook_binding(
                            expected_workspace=workspace, expected_surface=surface,
                            expected_session=session, worktree_directory=str(cwd))
                        native_generation = hook["started_at"]
                    except opencode_dispatch.OpenCodeDispatchError:
                        pass
                    launch.update(status="running", native_binding=native, startup_turn=turn,
                                  initial_turn_state="working", execution_result="turn_in_progress",
                                  delivery_readiness="held",
                                  delivery_hold_codes=["startup_turn_in_progress"])
                    if native_generation is not None:
                        launch["native_started_at"] = native_generation
                    update_opencode_launch(app, launch)
                classify_startup("waiting", "startup_turn_running", current, turn)
                _native_startup_pause()
                continue
            if turn["state"] == "failed":
                classify_startup("held", "startup_turn_failed", current, turn)
                raise opencode_dispatch.OpenCodeDispatchError("startup_turn_failed")
            if native["state"] == "unknown":
                classify_startup("waiting", "native_session_state_unknown", current, turn)
                _native_startup_pause()
                continue
            if native["state"] == "working":
                if (turn["state"] == "complete" and native["lifecycle"] == "running"
                        and native.get("input_state") in (None, "unknown", "empty")):
                    classify_startup("waiting", "startup_turn_completed_native_working", current, turn)
                    _native_startup_pause()
                    continue
                classify_startup("held", "native_session_state_unknown", current, turn)
                raise opencode_dispatch.OpenCodeDispatchError("native_session_state_unknown")
            if native["state"] != "idle":
                classify_startup("held", "native_session_state_unknown", current, turn)
                raise opencode_dispatch.OpenCodeDispatchError("native_session_state_unknown")
            try:
                identity = opencode_dispatch.inspect_session(
                    connection, session_id=session, session_directory=str(cwd), worktree_directory=str(cwd),
                    workspace_uuid=workspace, surface_uuid=surface, parent_task_id=connection.parent_task_id)
            except opencode_dispatch.OpenCodeDispatchError as exc:
                if exc.code == "session_guard_session_busy":
                    classify_startup("waiting", exc.code, current, turn)
                    _native_startup_pause()
                    continue
                classify_startup("held", exc.code, current, turn)
                raise
            hold_codes = []
            try:
                hook = opencode_dispatch.require_native_hook_binding(
                    expected_workspace=workspace, expected_surface=surface, expected_session=session,
                    worktree_directory=str(cwd))
                native_generation = hook["started_at"]
            except opencode_dispatch.OpenCodeDispatchError as exc:
                if exc.code in ("native_hook_store_unavailable", "native_hook_session_unavailable"):
                    try:
                        hook_pending = _native_hook_observation_pending(session)
                    except opencode_dispatch.OpenCodeDispatchError as hook_error:
                        classify_startup("held", hook_error.code, current, turn)
                        raise
                    classify_startup("waiting",
                                     "native_hook_record_missing" if hook_pending else "native_hook_record_arrived",
                                     current, turn)
                    _native_startup_pause()
                    continue
                classify_startup("held", exc.code, current, turn)
                raise
            delivery_guard_reason = None
            try:
                opencode_dispatch.require_native_delivery_binding(
                    run_cmux_rpc, expected_workspace=workspace, expected_surface=surface,
                    allow_composer_state=connection.delivery_policy == "api_sidechannel")
            except opencode_dispatch.OpenCodeDispatchError as exc:
                hold_codes.append("native_composer_state_not_proven_empty")
                delivery_guard_reason = _bounded_startup_reason(
                    exc.code, "native_delivery_binding_unavailable")
            except PreferenceError:
                hold_codes.append("native_composer_state_not_proven_empty")
                delivery_guard_reason = "native_delivery_binding_unavailable"
            execution_result = "turn_idle"
            readiness = classify_startup(
                "held" if hold_codes else "ready",
                hold_codes[0] if hold_codes else "startup_native_binding_ready",
                current, turn, persist=False)
            readiness["delivery_guard_reason"] = delivery_guard_reason
            launch.update(status="running", native_binding=native, identity=identity,
                          initial_turn_state="idle", execution_result=execution_result,
                          startup_turn=turn, delivery_readiness="ready" if not hold_codes else "held",
                          delivery_hold_codes=hold_codes, native_bound_at=now(),
                          startup_readiness_observation=readiness)
            if native_generation is not None:
                launch["native_started_at"] = native_generation
            update_opencode_launch(app, launch)
            try:
                prompt_file.unlink()
            except OSError:
                pass
            return {"launch": launch_id, "choice": choice, "status": "running", "session": session,
                    "cmux_workspace_uuid": workspace, "cmux_surface_uuid": surface,
                    "initial_turn_state": "idle", "execution_result": execution_result,
                    "delivery_readiness": launch["delivery_readiness"],
                    "delivery_hold_codes": hold_codes, "identity": identity}
        classify_startup("timed_out", "native_binding_timeout", last_startup_current, last_startup_turn)
        raise opencode_dispatch.OpenCodeDispatchError("native_binding_timeout")
    except BaseException as exc:
        if surface_opened:
            try:
                signal_private(abort_file, {"launch_id": launch_id, "reason": "native_binding_unconfirmed"})
            except PreferenceError:
                pass
        if (launch.get("initial_prompt_released_at") and
                (last_startup_readiness is None
                 or last_startup_readiness.get("state") not in ("held", "timed_out"))):
            failure_reason = (exc.code if isinstance(exc, opencode_dispatch.OpenCodeDispatchError)
                              else "startup_observation_failed")
            try:
                classify_startup("held", failure_reason,
                                 locals().get("last_startup_current"), locals().get("last_startup_turn"))
            except Exception:
                pass
        code = exc.code if isinstance(exc, opencode_dispatch.OpenCodeDispatchError) else "dispatch_uncertain"
        launch.update(status="uncertain" if creation_attempted else "failed", error_code=code,
                      uncertain_since=now() if creation_attempted else None)
        if last_startup_readiness is not None:
            launch["startup_readiness_observation"] = last_startup_readiness
        update_opencode_launch(app, launch)
        if not surface_opened:
            try:
                prompt_file.unlink()
            except OSError:
                pass
        raise PreferenceError(f"OpenCode launch {launch_id} {launch['status']}: {code}. Inspect its exact session and CMUX surface before retrying.") from None


def execute(args):
    if args.command == "review-guidance":
        return {"guidance": reviewer_guidance.REVIEWER_GUIDANCE}
    app = App(args.config_dir, args.state_dir)
    command = args.command
    if command == "doctor":
        caller = (read_only_diagnostic_caller(sys.stdin.buffer)
                  if args.starfleet_json and not sys.stdin.isatty() else None)
        return read_only_diagnostic_report(app, caller)
    if command == "preflight":
        return opencode_preflight(app, args)
    if command == "resume":
        return resume_launch(app, args)
    if command == "examples":
        return example_config()
    with locked(app.config_dir):
        if command == "init":
            require(not app.config_path.exists(), "Configuration exists; init will not overwrite it.")
            config = example_config() if args.examples else initial_config()
            validate(config); write(app.config_path, config)
            return {"config": str(app.config_path), "profiles": list(config["profiles"]), "active": None,
                    "examples_included": args.examples}
        config = app.config()
        if command == "list":
            return {"profiles": list(config["profiles"]), "active": config["active"]}
        if command == "validate":
            return {"valid": True}
        if command == "show":
            require(args.profile in config["profiles"], "Unknown profile.")
            return config["profiles"][args.profile]
        if command == "startup":
            report = startup_report(config, args.expect_profile)
            return {**report, "exit_code": 0 if report["ready_for_dispatch"] else 2}
        if command == "review-target":
            return review_target_command(app, args)
        if command == "status":
            active = selection(config) if config["active"] else None
            notice = app.state("notification", {})
            handoffs = [read(p) for p in sorted((app.state_dir / "handoffs").glob("*.json"))]
            recipients = app.state("orchestrators", {})
            result = {"active": active,
                      "notification": notice,
                      "notification_pending": bool(active and notice.get("selection") != active["selection"]),
                      "handoffs": handoffs,
                      "orchestrators": recipients, "adoption": adoption_status(app, active, recipients),
                      "adoption_note": "Explicit owner acknowledgment of saved choices for future dispatches, not proof of subsequent behavior or a live session.",
                      "coverage": "Only explicitly enrolled roles; all active orchestrators must enroll."}
            if active and "standard_review_policy" in active:
                result["review_selection"] = {"kind": "configured_candidates",
                                               "configured_standard_review": active["roles"]["standard_review"],
                                               "contextual_cycle": None}
            if args.review_target or args.review_cycle:
                require(args.review_target and args.review_cycle and active
                        and "standard_review_policy" in active,
                        "Contextual review status requires a light profile, --review-target, and --review-cycle.")
                target = load_target(app, args.review_target)
                cycle = cycle_for_target(target, args.review_cycle)
                result["review_selection"]["contextual_cycle"] = cycle_summary(app, target, cycle)
                result["review_selection"]["selection_matches_active"] = cycle["selection"] == active["selection"]
            return result
        if command in ("activate", "model-set", "profile-put", "authority-set"):
            original = copy.deepcopy(config)
            before = selection(config) if config["active"] else None
            if command == "activate":
                require(args.profile in config["profiles"], "Unknown profile; create it with profile-put first.")
                config["active"] = args.profile
            elif command == "model-set":
                require(args.family and not any(c.isspace() for c in args.family), "Model family must be a nonempty token.")
                require((args.harness == "opencode") == bool(args.provider_id),
                        "Pass --provider-id for OpenCode models only.")
                config["models"][args.family] = {"harness": args.harness, "slug": args.slug,
                                                  "efforts": args.efforts, "source": args.source,
                                                  **({"provider_id": args.provider_id} if args.harness == "opencode" else {})}
            elif command == "profile-put":
                config["profiles"][args.name] = read(args.file)
            else:
                require(args.label.strip(), "Approval authority label cannot be empty.")
                config["approval_authority"] = {"label": args.label.strip()}
            validate(config)
            after = selection(config) if config["active"] else None
            changed = after != before
            if changed:
                require(args.orchestrators, "Active choices change. Collect --orchestrators future-only, ask, or handoff first.")
                config["activation"] = uuid.uuid4().hex
                after = selection(config)
            if config != original:
                write(app.config_path, config)
            if not changed:
                return {"changed": False, "active": after, "saved": True}
        elif command == "notify":
            after = selection(config)
            notice = app.state("notification", {})
            args.orchestrators = (notice["orchestrators"] if notice.get("selection") == after["selection"]
                                  else args.orchestrators)
            require(args.orchestrators, "No saved notification intent. Collect --orchestrators future-only, ask, or handoff.")
        elif command == "enroll":
            for key in ("workspace", "surface"):
                try:
                    uuid.UUID(getattr(args, key))
                except ValueError as exc:
                    raise PreferenceError(f"Use a stable UUID for --{key}, not a title or short ref.") from exc
            require(args.harness in ("claude", "codex", "muse"), "Unknown harness.")
            recipient = {k: getattr(args, k) for k in ("workspace", "surface", "session", "harness")}
            require(app.live(recipient, app.current()) == "active", "Cannot confirm this exact live session. Nothing enrolled.")
            return enroll_recipient(app, args.name, recipient)
        elif command == "enroll-self":
            return enroll_recipient(app, args.name, app.caller(), replace=args.replace)
        elif command == "adopt":
            active = selection(config)
            require(args.selection == active["selection"], "Selection changed; read startup/status and acknowledge the current selection.")
            caller = app.caller()
            recipients = app.state("orchestrators", {})
            require(same_recipient(recipients.get(args.name), caller), "Only this name's exact enrolled native owner can acknowledge adoption.")
            records = app.state("adoptions", {})
            previous = records.get(args.name)
            if previous is None or not same_recipient(previous["recipient"], caller) or previous["selection"] != args.selection:
                records[args.name] = {"recipient": caller, "profile": active["profile"],
                                      "selection": args.selection, "at": now()}
                app.save_state("adoptions", records)
            return {"name": args.name, "adoption": "acknowledged", **records[args.name],
                    "instruction": "Future-dispatch choices acknowledged only; no jobs, sessions, profiles or permissions changed."}
        elif command == "unenroll":
            recipients = app.state("orchestrators", {}); require(args.name in recipients, "Unknown orchestrator.")
            del recipients[args.name]; app.save_state("orchestrators", recipients)
            return {"removed": args.name}
        elif command == "review-plan":
            plan = review_scope(config, args.cwd, args.base, read(args.facts))
            active = selection(config)
            plan.update(selection=active["selection"], selection_snapshot=active, id=uuid.uuid4().hex)
            has_cycle_context = bool(args.target or args.cycle_key or args.previous_cycle)
            light_standard = ("standard_review_policy" in active and plan["role"] == "standard_review")
            if light_standard:
                require(args.target and args.cycle_key,
                        "claude-codex-light standard reviews require --target and --cycle-key.")
                plan = make_light_review_plan(app, config, args, plan)
            else:
                require(not has_cycle_context,
                        "Target cycle options apply only to standard reviews under claude-codex-light.")
                app.save_state(f"reviews/{plan['id']}", plan)
            return review_output(app, config, plan, full=args.full)
        elif command == "review-inspect":
            plan = app.state(f"reviews/{safe_id(args.plan)}", None)
            require(plan, "Unknown review plan.")
            return review_output(app, config, plan, full=args.full, saved_snapshot=True)
        elif command == "approve-review":
            plan = app.state(f"reviews/{safe_id(args.plan)}", None)
            require(plan and plan["role"] == "advanced_review", "Advanced review plan required.")
            authority = configured_authority(config)
            require(args.authorization.strip(), "Record the configured authority's explicit authorization; do not infer permission.")
            approval = {"id": uuid.uuid4().hex, "scope": plan["scope"], "selection": plan["selection"],
                        "authority": authority, "authorization": args.authorization, "at": now(),
                        "role": "standard_review" if args.standard_instead else "advanced_review"}
            app.save_state(f"approvals/{approval['id']}", approval)
            return approval
        elif command == "resolve":
            active = selection(config)
            has_context = bool(args.review_target or args.review_cycle or args.review_plan)
            if args.role == "standard_review" and "standard_review_policy" in active:
                if has_context:
                    require(args.review_target and args.review_cycle and args.review_plan,
                            "Contextual light review resolution requires --review-target, --review-cycle, and --review-plan.")
                    target, cycle, plan, slot, choice = cycle_review_context(app, config, args)
                    return {**choice, "selection_kind": "cycle", "review_target": target["id"],
                            "review_cycle": cycle["id"], "review_plan": plan["id"],
                            "slot": slot["number"]}
                if args.slot is not None:
                    return {**resolve(active, args.role, args.slot),
                            "selection_kind": "configured_candidate", "slot": args.slot}
                return {"profile": active["profile"], "selection": active["selection"],
                        "role": args.role, "selection_kind": "configured_candidates",
                        "configured_candidates": active["roles"]["standard_review"],
                        "contextual_cycle": None}
            require(not has_context, "Review target context applies only to standard reviews under claude-codex-light.")
            return resolve(active, args.role, args.slot)
        elif command == "context":
            active = selection(config)
            result = {"active": active, "instruction": "Read saved preferences before new dispatches. Running tasks retain their recorded choice."}
            if args.launch:
                result["running"] = app.state(f"launches/{safe_id(args.launch)}", None)
                require(result["running"], "Unknown launch.")
                result["different_selection"] = result["running"]["choice"]["selection"] != active["selection"]
            return result
        elif command == "hook-config":
            cli = [sys.executable, str(Path(__file__).resolve()), "--config-dir", str(app.config_dir.resolve()),
                   "--state-dir", str(app.state_dir.resolve()), "hook", "--harness", args.harness]
            handler = {"hooks": [{"type": "command", "command": shlex.join(cli), "timeout": 5}]}
            event = "UserPromptSubmit" if args.harness == "claude" else "SessionStart"
            if event == "SessionStart":
                handler["matcher"] = "startup|resume|compact|fork|clear"
            return {"hooks": {event: [handler]}}
        elif command == "hook":
            payload = json.loads(sys.stdin.read(65537))
            require(isinstance(payload, dict), "Hook input must be an object.")
            session = payload.get("session_id")
            require(isinstance(session, str) and session, "Session ID missing from hook.")
            event = payload.get("hook_event_name", "SessionStart")
            require(event in ("SessionStart", "UserPromptSubmit"), "Unknown hook event.")
            require(event != "UserPromptSubmit" or args.harness == "claude", "Prompt hook is verified for Claude only.")
            source = "prompt" if event == "UserPromptSubmit" else payload.get("source")
            require(source in ("startup", "resume", "compact", "fork", "clear", "prompt"), "Unknown hook source.")
            key = digest({"harness": args.harness, "session": session})
            binding = app.state(f"bindings/{key}", None)
            ticket = os.environ.get("STARFLEET_MODEL_LAUNCH")
            if binding is None and ticket and source in ("startup", "prompt"):
                launch = app.state(f"launches/{safe_id(ticket)}", None)
                require(launch and launch["choice"]["harness"] == args.harness, "Invalid launch ticket.")
                claims = app.state("claims", {})
                require(ticket not in claims or claims[ticket] == key, "Launch ticket already belongs to another session.")
                require(str(Path(payload.get("cwd", "")).resolve()) == launch["cwd"], "Launch worktree mismatch.")
                claims[ticket] = key; app.save_state("claims", claims)
                binding = {"launch": ticket}; app.save_state(f"bindings/{key}", binding)
                launch["native_session"] = session
                app.save_state(f"launches/{ticket}", launch)
            active = selection(config)
            text = (f"Model Preferences: active profile {active['profile']}. "
                    f"Saved role choices: {json.dumps(describe_roles(active['roles']), sort_keys=True)}. "
                    "Read the saved profile before each new launch; use its exact configured provider model and do not query a catalog or substitute another model. "
                    "Check model-preferences startup --expect-profile NAME against session-specific intent; a pilot preference or your own harness does not activate it. "
                    "Advanced reviewers require explicit permission from the configured human authority; delegated research uses the current researcher role. "
                    "Preferences do not expand permissions or change existing running jobs.")
            if "standard_review_policy" in active:
                text += (" The standard-review choices above are configured candidates. A contextual review uses only the slot "
                         "saved for its target, cycle, and exact plan.")
            if binding:
                launch = app.state(f"launches/{safe_id(binding['launch'])}", None)
                require(launch, "Running task choice is missing.")
                choice = launch["choice"]
                text += (f" This running task actually launched with profile {choice['profile']}, role {choice['role']}, "
                         f"model {choice['model']}, effort {choice['effort']}, selection {choice['selection'][:12]}.")
                membership = launch.get("review_cycle")
                if membership:
                    text += (f" Its contextual review slot is target {membership['target_id']}, cycle "
                             f"{membership['cycle_id']}, plan {membership['plan_id']}, slot {membership['slot']}.")
                text += " " + DELEGATION
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
        elif command == "dispatch":
            require(not args.receipt_file or (args.context_injector and args.owner_worktree),
                    "--receipt-file requires --context-injector and --owner-worktree.")
            active = selection(config)
            cwd = Path(args.cwd).resolve(); require(cwd.is_dir(), "Task directory is missing.")
            has_cycle_context = bool(args.review_target or args.review_cycle)
            review_cycle = None
            target = cycle = review_slot = None
            saved_plan = (app.state(f"reviews/{safe_id(args.review_plan)}", None)
                          if args.role == "standard_review" and "standard_review_policy" in active
                          and args.review_plan else None)
            standard_instead_plan = (isinstance(saved_plan, dict)
                                     and saved_plan.get("role") == "advanced_review")
            if (args.role == "standard_review" and "standard_review_policy" in active
                    and not standard_instead_plan):
                require(args.review_target and args.review_cycle and args.review_plan,
                        "claude-codex-light standard review dispatch requires --review-target, --review-cycle, and --review-plan.")
                target, cycle, plan, review_slot, choice = cycle_review_context(app, config, args, cwd)
                args.slot = review_slot["number"]
                review_cycle = {"target_id": target["id"], "cycle_id": cycle["id"],
                                "plan_id": plan["id"], "slot": review_slot["number"]}
            else:
                require(not has_cycle_context and not args.retry_failed,
                        "Review target context and --retry-failed apply only to standard reviews under claude-codex-light.")
                choice = resolve(active, args.role, args.slot)
            require(args.role != "orchestrator" or choice["harness"] == "opencode",
                    "Fresh successor orchestration remains outside Model Preferences handoff authority.")
            task_bytes = Path(args.task_file).read_bytes()
            try:
                prompt = task_bytes.decode("utf-8")
            except UnicodeError as exc:
                raise PreferenceError("Task brief must be UTF-8.") from exc
            require(0 < len(task_bytes) <= 16384 and prompt.strip(), "Task brief must be 1 to 16384 bytes of nonempty UTF-8.")
            validate_review(app, config, args, choice, cwd)
            task_digest = hashlib.sha256(task_bytes).hexdigest()
            if review_cycle:
                existing = reconcile_review_launch(app, target, cycle, review_slot,
                                                   review_cycle["plan_id"], task_digest)
                if args.retry_failed:
                    require(existing and existing.get("status") == "exited"
                            and type(existing.get("exit_code")) is int and existing["exit_code"] != 0,
                            "--retry-failed requires the exact slot's latest launch to have a known nonzero exit.")
                elif existing:
                    return {"launch": existing["id"], "status": existing.get("status"),
                            "exit_code": existing.get("exit_code"), "reused": True,
                            "review_cycle": review_cycle}
                else:
                    require(not args.retry_failed,
                            "--retry-failed requires an existing known nonzero launch for this exact slot.")
            if choice["harness"] == "opencode":
                opencode_task = task_bytes
            else:
                require(bool(args.context_injector) == bool(args.owner_worktree), "Pattern Buffer needs both its script and --owner-worktree.")
                if args.context_injector:
                    require(args.role == "implementer" and choice["harness"] in ("claude", "codex"),
                            f"Pattern Buffer automatic task replay is unavailable for role {args.role} on harness {choice['harness']}. "
                            "It supports Claude/Codex implementers only. Use plain Model Preferences dispatch without "
                            "--context-injector, --owner-worktree or --receipt-file, putting required context in --task-file "
                            "and retaining applicable review/permission flags. "
                            "Guide: apps/model-preferences/README.md#pattern-buffer-support")
                prompt = reviewer_guidance.compose_reviewer_prompt(args.role, prompt)
                prompt = DELEGATION + " Use the current researcher role for authorized research. Verify its findings.\n\n" + prompt
                argv = argv_for(choice, prompt, cwd)
                launch_id = uuid.uuid4().hex
                adapter = None
                if args.context_injector:
                    receipt = (Path(args.receipt_file).expanduser().absolute() if args.receipt_file else
                               app.state_dir.resolve() / "ci-receipts" / f"{launch_id}.json")
                    adapter = {"script": str(Path(args.context_injector).resolve()),
                               "owner_worktree": str(Path(args.owner_worktree).resolve()), "receipt": str(receipt)}
                    argv = argv_for(choice, "-", cwd)
                    if choice["harness"] == "claude":
                        argv = argv[:-1]
                    argv = [sys.executable, str(Path(args.context_injector).resolve()), "--cwd", str(cwd), "dispatch",
                            "--owner-worktree", str(Path(args.owner_worktree).resolve()), "--harness", choice["harness"],
                            "--task-file", str(Path(args.task_file).resolve()),
                            "--receipt-file", str(receipt), "--", *argv]
                launch = {"id": launch_id, "choice": choice, "cwd": str(cwd), "created_at": now(),
                          "context_injector": adapter, "slot": args.slot,
                          "task_digest": task_digest, "status": "starting"}
                if review_cycle:
                    launch["review_cycle"] = review_cycle
                if choice["profile"] in ("Codex-Qwen", "Qwen-only") and choice["role"].endswith("_review"):
                    launch["review_authorization_sha256"] = hashlib.sha256(args.review_authorization.encode()).hexdigest()
                if review_cycle:
                    record_review_launch(app, target, cycle, review_slot, review_cycle["plan_id"], launch)
                else:
                    app.save_state(f"launches/{launch['id']}", launch)
        elif command.startswith("handoff-"):
            return handoff(app, config, args)
        else:
            raise PreferenceError("Unsupported command.")
    if command == "dispatch" and choice["harness"] == "opencode":
        return dispatch_opencode(app, args, config, choice, cwd, opencode_task)
    if command in ("activate", "model-set", "profile-put", "authority-set", "notify"):
        notice = app.notify(after, args.orchestrators, retry=command == "notify")
        return {"changed": command != "notify", "active": after, "notification": notice}
    print(json.dumps({"launch": launch["id"], "choice": choice}), file=sys.stderr, flush=True)
    try:
        env = dict(os.environ, STARFLEET_MODEL_LAUNCH=launch["id"])
        env.pop("STARFLEET_CONTEXT_DISPATCH", None)
        if choice["harness"] == "codex":
            argv.insert(argv.index("exec") + 1, "--json")
            completed = stream_codex(app, launch, argv, env, cwd)
        else:
            completed = subprocess.run(argv, cwd=cwd, env=env)
        launch = app.state(f"launches/{launch['id']}", launch)
        launch.update(status="exited", exit_code=completed.returncode, finished_at=now())
    except OSError as exc:
        launch.update(status="failed", error=str(exc)); app.save_state(f"launches/{launch['id']}", launch)
        raise PreferenceError(str(exc)) from exc
    with locked(app.config_dir):
        app.save_state(f"launches/{launch['id']}", launch)
    return {"launch": launch["id"], "exit_code": completed.returncode}


def validate_review(app, config, args, choice, cwd):
    if not choice["role"].endswith("_review"):
        return
    if choice["profile"] in ("Codex-Qwen", "Qwen-only"):
        authorization = getattr(args, "review_authorization", None)
        require(isinstance(authorization, str) and authorization.strip(),
                "Every review launch in a Qwen profile requires a fresh --review-authorization on this command, including standard review.")
    require(args.review_plan, "Review dispatch requires a review-plan, including standard review.")
    plan = saved_review_plan(app, args.review_plan)
    current = review_scope(config, cwd, plan["base"], plan["facts"])
    require(current["scope"] == plan["scope"] and plan["selection"] == choice["selection"],
            "Review scope/profile changed; create a fresh plan.")
    if plan["role"] == "advanced_review":
        authority = configured_authority(config)
        require(args.approval, f"Advanced review was requested. Record explicit permission from the configured authority ({authority}) or choose standard reviewers first.")
        approval = app.state(f"approvals/{safe_id(args.approval)}", None)
        require(approval and approval["scope"] == plan["scope"] and approval["selection"] == choice["selection"]
                and approval["role"] == choice["role"], "Review decision does not cover this scope, profile and reviewer group.")
    else:
        require(choice["role"] == "standard_review", "Create an advanced review plan before asking for advanced reviewers.")
        if "standard_review_policy" in config["profiles"][choice["profile"]]:
            target, cycle, bound_plan, slot, contextual_choice = cycle_review_context(app, config, args, cwd)
            require(bound_plan["id"] == plan["id"] and contextual_choice == choice
                    and getattr(args, "slot", None) == slot["number"],
                    "Resolved reviewer does not match the saved target, cycle, plan, and slot.")
            launch_id = getattr(args, "review_launch_id", None)
            if launch_id:
                link = next((item for item in slot["launches"]
                             if item["plan_id"] == plan["id"] and item["launch_id"] == launch_id), None)
                require(link, "Codex resume launch is not a member of this exact review cycle slot.")
                launch_for_link(app, target, cycle, slot, link)
        else:
            require(not (getattr(args, "review_target", None) or getattr(args, "review_cycle", None)),
                    "Review target context applies only to claude-codex-light.")


def bind_native(app, launch_id, session):
    try:
        require(str(uuid.UUID(session)) == session, "Native session ID must be a canonical UUID.")
    except (ValueError, TypeError, AttributeError) as exc:
        raise PreferenceError("Invalid native session ID.") from exc
    with locked(app.config_dir):
        launch = app.state(f"launches/{launch_id}", None)
        require(launch and launch["choice"]["harness"] == "codex", "Unknown Codex launch.")
        require(launch.get("native_session") in (None, session), "Native session identity changed.")
        key = digest({"harness": "codex", "session": session})
        binding = app.state(f"bindings/{key}", None)
        require(binding is None or binding["launch"] == launch_id, "Native session belongs to another launch.")
        claims = app.state("claims", {})
        require(claims.get(launch_id) in (None, key), "Launch already belongs to another native session.")
        claims[launch_id] = key
        app.save_state("claims", claims)
        app.save_state(f"bindings/{key}", {"launch": launch_id})
        launch["native_session"] = session
        app.save_state(f"launches/{launch_id}", launch)


def stream_codex(app, launch, argv, env, cwd, prompt=None, expected=None):
    """Forward JSONL output and bind only the explicit native thread.started event."""
    seen = None
    with subprocess.Popen(argv, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                          stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL) as process:
        if prompt is not None:
            process.stdin.write(prompt)
            process.stdin.close()
        for line in process.stdout:
            print(line, end="", flush=True)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "thread.started":
                session = event.get("thread_id")
                require(seen in (None, session) and (expected is None or session == expected),
                        "Codex started an unexpected native session; continuation is uncertain.")
                bind_native(app, launch["id"], session)
                seen = session
        result = subprocess.CompletedProcess(argv, process.wait())
    # A failing harness may emit no thread event. Never invent an identity from its exit status.
    require(expected is None or seen == expected, "Native continuation identity was not confirmed; inspect the attempt before retrying.")
    return result


def require_stopped(app, session):
    current = app.current()
    require(current.get("truncated") is False and isinstance(current.get("items"), list),
            "Native session inventory is incomplete; nothing resumed.")
    for item in current["items"]:
        agents = [a for a in item.get("agents", []) if a.get("kind") == "codex" and a.get("session_id") == session]
        if agents:
            require(item.get("freshness", {}).get("state") == "current", "Native session observation is stale; nothing resumed.")
            require(all(a.get("state") == "ended" for a in agents), "Native session is still active; nothing resumed.")


def injector_task(app, launch, session):
    adapter = launch.get("context_injector")
    if adapter is None:
        return None
    receipt = read(adapter["receipt"])
    require(receipt.get("worktree") == launch["cwd"] and receipt.get("harness") == "codex"
            and receipt.get("owner_worktree") == adapter["owner_worktree"], "Pattern Buffer receipt ownership changed.")
    task_id = safe_id(receipt.get("task_id"))
    cli = [sys.executable, adapter["script"], "--cwd", launch["cwd"]]
    task = app.run_json([*cli, "task-status", "--task-id", task_id])
    require(task.get("status") == "active" and task.get("session_id") == session and task.get("harness") == "codex"
            and task.get("root") == launch["cwd"] and task.get("owner", {}).get("root") == adapter["owner_worktree"],
            "Pattern Buffer task is finished, unbound or belongs to another owner/session; nothing resumed.")
    status = app.run_json([*cli, "status", "--harness", "codex"])
    require(status.get("registered") and status.get("enabled") and status.get("owned_hook_handlers") == 1,
            "Pattern Buffer recovery is unavailable; nothing resumed.")
    expected_owner = None if adapter["owner_worktree"] == launch["cwd"] else task["owner"]
    require(status.get("context_owner") == expected_owner, "Pattern Buffer current owner changed; nothing resumed.")
    return cli, task_id, task


def resume_opencode_launch(app, args, brief):
    require(args.opencode_manifest,
            "OpenCode resume requires the private --opencode-manifest used by the original launch.")
    launch_id = safe_id(args.launch)
    try:
        connection = opencode_dispatch.load_connection(args.opencode_manifest)
    except opencode_dispatch.OpenCodeDispatchError as exc:
        raise PreferenceError(f"OpenCode resume preflight held: {exc.code}.") from None
    initial = app.state(f"launches/{launch_id}", None)
    require(initial and initial.get("choice", {}).get("harness") == "opencode",
            "OpenCode resume requires a recorded OpenCode launch.")
    session = initial.get("native_session")
    require(isinstance(session, str) and session.startswith("ses_"),
            "Recorded OpenCode launch has no exact native session identity.")
    lock_path = app.state_dir / "resume-locks" / f"{session}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PreferenceError("This exact OpenCode session already has a continuation in flight.") from exc
        with locked(app.config_dir):
            config = app.config()
            launch = app.state(f"launches/{launch_id}", None)
            require(launch and launch.get("status") == "running",
                    "OpenCode launch is not an exact verified running session; nothing sent.")
            choice = launch["choice"]
            active = selection(config)
            require(choice.get("selection") == active["selection"] and
                    resolve(active, choice["role"], launch.get("slot")) == choice,
                    "Current profile choice conflicts with this OpenCode session; nothing sent.")
            require(connection.parent_task_id == launch.get("parent_task_id")
                    and connection.api_origin == launch.get("api_origin")
                    and connection.expected_server_pid == launch.get("expected_server_pid")
                    and connection.delivery_policy == launch.get("delivery_policy", "native_empty_composer"),
                    "OpenCode manifest no longer matches the exact server and task binding.")
            text = brief.decode("utf-8")
            require(text.strip(), "Continuation task must be nonempty UTF-8.")
            task_digest = hashlib.sha256(brief).hexdigest()
            require(task_digest != launch.get("task_digest"),
                    "The original task digest cannot be submitted again to this session.")
            previous = app.state(f"continuations/{session}", None)
            require(previous is None or previous.get("status") not in ("send_intent", "uncertain"),
                    "A prior OpenCode send has uncertain admission; inspect the exact session before retrying.")
            require(previous is None or previous.get("task_digest") != task_digest,
                    "This continuation task digest was already submitted to the exact session.")
            validate_review(app, config, args, choice, Path(launch["worktree_directory"]))
            worktree = Path(launch["worktree_directory"]).resolve(strict=True)
            require(str(worktree) == launch["worktree_directory"] and worktree.is_dir(),
                    "Recorded OpenCode worktree is missing or moved.")
            require(launch.get("native_started_at") is not None,
                    "Native OpenCode hook generation is unavailable; managed continuation remains held.")
            require(launch.get("execution_result") in ("turn_idle", "turn_failed"),
                    "The initial OpenCode task has not completed; managed continuation remains held.")
        try:
            evidence = opencode_dispatch.preflight(
                connection, choice, directory=launch["session_directory"])
            require(evidence.api_origin == launch["api_origin"] and evidence.server_pid == launch["expected_server_pid"],
                    "OpenCode server binding changed; nothing sent.")
            opencode_dispatch.verify_session(
                connection, session, expected_directory=launch["session_directory"], choice=choice,
                launch_id=launch_id, task_digest=launch["task_digest"],
                parent_task_id=launch["parent_task_id"],
                parent_session_id=launch.get("opencode_parent_session_id"))
            identity = opencode_dispatch.inspect_session(
                connection, session_id=session, session_directory=launch["session_directory"],
                worktree_directory=launch["worktree_directory"],
                workspace_uuid=launch["cmux_workspace_uuid"], surface_uuid=launch["cmux_surface_uuid"],
                parent_task_id=launch["parent_task_id"])
            current = app.current()
            native = opencode_dispatch.require_native_dispatch_binding(
                expected_workspace=launch["cmux_workspace_uuid"],
                expected_surface=launch["cmux_surface_uuid"], expected_session=session, current=current)
            require(native["state"] == "idle",
                    "Exact OpenCode native session is busy, awaiting input or unknown; nothing sent.")
            hook = opencode_dispatch.require_native_hook_binding(
                expected_workspace=launch["cmux_workspace_uuid"],
                expected_surface=launch["cmux_surface_uuid"], expected_session=session,
                worktree_directory=launch["worktree_directory"],
                expected_started_at=launch["native_started_at"])
            require(hook["agent_lifecycle"] == "idle",
                    "Exact OpenCode hook lifecycle is not idle; nothing sent.")
            opencode_dispatch.require_native_delivery_binding(
                run_cmux_rpc, expected_workspace=launch["cmux_workspace_uuid"],
                expected_surface=launch["cmux_surface_uuid"],
                allow_composer_state=connection.delivery_policy == "api_sidechannel")
            adapter = launch.get("context_injector")
            if choice["role"] == "orchestrator":
                require(isinstance(adapter, dict) and adapter.get("role") == "orchestrator",
                        "OpenCode orchestrator assignment is missing; nothing sent.")
                context = app.run_json([sys.executable, adapter["script"], "--cwd", str(worktree),
                                        "context", "--harness", "opencode", "--session", session])
                require(context.get("status") == "context" and isinstance(context.get("text"), str)
                        and context["text"].startswith("pattern-buffer | role: orchestrator |")
                        and context.get("assignment_sha256") == launch.get("pattern_buffer_assignment_sha256"),
                        "OpenCode orchestrator assignment changed; nothing sent.")
            elif isinstance(adapter, dict) and adapter.get("task_id"):
                cli = [sys.executable, adapter["script"], "--cwd", str(worktree)]
                task = app.run_json([*cli, "task-status", "--task-id", adapter["task_id"]])
                require(task.get("status") == "active" and task.get("harness") == "opencode"
                        and task.get("session_id") == session and task.get("root") == str(worktree)
                        and task.get("role") == "implementer"
                        and task.get("owner", {}).get("root") == adapter.get("owner_worktree"),
                        "OpenCode Pattern Buffer task is no longer active on this exact session.")
                context = app.run_json([*cli, "context", "--harness", "opencode", "--session", session])
                require(context.get("status") == "context" and context.get("worktree") == str(worktree)
                        and context.get("assignment_sha256") == launch.get("pattern_buffer_assignment_sha256"),
                        "OpenCode implementer assignment changed; nothing sent.")
        except (opencode_dispatch.OpenCodeDispatchError, PreferenceError) as exc:
            detail = exc.code if isinstance(exc, opencode_dispatch.OpenCodeDispatchError) else str(exc)
            raise PreferenceError(f"OpenCode continuation held: {detail}.") from None

        attempt = {"id": uuid.uuid4().hex, "launch": launch_id, "session": session,
                   "choice": choice, "task_digest": task_digest, "created_at": now(),
                   "status": "send_intent"}
        with locked(app.config_dir):
            latest = app.state(f"launches/{launch_id}", None)
            require(latest and latest.get("status") == "running"
                    and latest.get("native_started_at") == launch.get("native_started_at"),
                    "OpenCode launch changed before send intent; nothing sent.")
            current_previous = app.state(f"continuations/{session}", None)
            require(current_previous == previous,
                    "OpenCode continuation state changed before send intent; nothing sent.")
            app.save_state(f"continuations/{session}", attempt)
            app.save_state(f"attempts/{attempt['id']}", attempt)
        try:
            sent = opencode_dispatch.send_prompt(connection, session_id=session, text=text)
        except opencode_dispatch.OpenCodeDispatchError as exc:
            attempt.update(status="uncertain", error_code=exc.code, uncertain_at=now())
            with locked(app.config_dir):
                app.save_state(f"continuations/{session}", attempt)
                app.save_state(f"attempts/{attempt['id']}", attempt)
            raise PreferenceError(f"OpenCode prompt admission is uncertain: {exc.code}; inspect before retrying.") from None
        attempt.update(status="submitted", message_id=sent["message_id"], submitted_at=now(),
                       admission=sent["admission"])
        with locked(app.config_dir):
            app.save_state(f"continuations/{session}", attempt)
            app.save_state(f"attempts/{attempt['id']}", attempt)
        return {"launch": launch_id, "continuation": attempt["id"], "session": session,
                "status": "submitted", "message_id": sent["message_id"],
                "execution_result": "not_yet_observed", "delivery_readiness": "held"}


def resume_launch(app, args):
    launch_id = safe_id(args.launch)
    # Snapshot before side effects, then freeze the exact bytes for both CI update and native stdin.
    brief = Path(args.task_file).read_bytes()
    require(0 < len(brief) <= 16384, "Task brief must be 1 to 16384 bytes of nonempty UTF-8.")
    try:
        require(brief.decode("utf-8").strip(), "Task brief must be 1 to 16384 bytes of nonempty UTF-8.")
    except UnicodeError as exc:
        raise PreferenceError("Task brief must be UTF-8.") from exc
    existing = app.state(f"launches/{launch_id}", None)
    if existing and existing.get("choice", {}).get("harness") == "opencode":
        return resume_opencode_launch(app, args, brief)
    with locked(app.config_dir):
        launch = app.state(f"launches/{launch_id}", None)
        require(launch and launch["choice"]["harness"] == "codex", "Resume supports recorded Codex launches only.")
        session = launch.get("native_session")
        require(session, "Missing native session identity; nothing resumed.")
        try:
            require(str(uuid.UUID(session)) == session, "Native session ID must be a canonical UUID.")
        except (ValueError, TypeError, AttributeError) as exc:
            raise PreferenceError("Invalid native session ID.") from exc
        binding = app.state(f"bindings/{digest({'harness': 'codex', 'session': session})}", None)
        require(binding and binding.get("launch") == launch_id, "Native session has no matching launch binding; nothing resumed.")
    lock_path = app.state_dir / "resume-locks" / f"{session}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PreferenceError("This native session already has a continuation in flight.") from exc
        with locked(app.config_dir):
            config = app.config()
            launch = app.state(f"launches/{launch_id}", None)
            require(launch["status"] == "exited", "Original launch has not exited; nothing resumed.")
            choice = launch["choice"]
            membership = launch.get("review_cycle")
            if membership:
                require(args.review_target == membership["target_id"]
                        and args.review_cycle == membership["cycle_id"]
                        and args.review_plan == membership["plan_id"],
                        "Codex resume must name the launch's original review target, cycle, and plan.")
                args.slot = membership["slot"]
                args.review_launch_id = launch_id
            else:
                require(not args.review_target and not args.review_cycle,
                        "This launch has no review-cycle membership.")
            active = selection(config)
            require(choice["selection"] == active["selection"], "Current profile instructions conflict with this launch; use a fresh dispatch or coordinate the profile first.")
            require(resolve(active, choice["role"], launch.get("slot")) == choice, "Recorded role choice changed; nothing resumed.")
            cwd = Path(launch["cwd"])
            require(cwd.is_dir() and str(cwd.resolve()) == launch["cwd"], "Original worktree is missing or moved.")
            validate_review(app, config, args, choice, cwd)
            previous = app.state(f"continuations/{session}", None)
            require(previous is None or previous.get("status") == "exited", "Previous continuation is unresolved; inspect it before retrying.")
            require_stopped(app, session)
            task = injector_task(app, launch, session)
            # Legacy records cannot prove whether Pattern Buffer owned their task.
            require("context_injector" in launch, "Legacy launch lacks task-ownership metadata; use a fresh dispatch.")
            argv = argv_for(choice, "-", cwd)
            argv = argv[:-1] + ["resume", session, "--json", "-"]
            attempt = {"id": uuid.uuid4().hex, "launch": launch_id, "session": session, "choice": choice,
                       "task_digest": hashlib.sha256(brief).hexdigest(), "created_at": now(), "status": "starting",
                       "previous": previous.get("id") if previous else None}
            if membership:
                attempt["review_cycle"] = membership
            if choice["profile"] in ("Codex-Qwen", "Qwen-only") and choice["role"].endswith("_review"):
                attempt["review_authorization_sha256"] = hashlib.sha256(args.review_authorization.encode()).hexdigest()
            app.save_state(f"continuations/{session}", attempt)
            app.save_state(f"attempts/{attempt['id']}", attempt)
        try:
            if task:
                cli, task_id, original_task = task
                with tempfile.NamedTemporaryFile(prefix="mp-resume-brief-", mode="wb") as snapshot:
                    snapshot.write(brief); snapshot.flush()
                    updated = app.run_json([*cli, "update-task", "--task-id", task_id, "--task-file", snapshot.name])
                require(updated.get("session_id") == session and updated.get("owner") == original_task["owner"]
                        and updated.get("brief_sha256") == attempt["task_digest"], "Pattern Buffer update could not be verified; inspect the attempt.")
                attempt["context_injector_task"] = task_id
            prompt = reviewer_guidance.compose_reviewer_prompt(choice["role"], brief.decode())
            prompt = DELEGATION + " Use the current researcher role for authorized research.\n\n" + prompt
            env = dict(os.environ, STARFLEET_MODEL_LAUNCH=launch_id)
            env.pop("STARFLEET_CONTEXT_DISPATCH", None)
            print(json.dumps({"launch": launch_id, "continuation": attempt["id"], "session": session, "choice": choice}), file=sys.stderr, flush=True)
            completed = stream_codex(app, launch, argv, env, cwd, prompt=prompt, expected=session)
            attempt.update(status="exited", exit_code=completed.returncode, finished_at=now())
        except BaseException as exc:
            attempt.update(status="uncertain", error=str(exc), finished_at=now())
            raise
        finally:
            with locked(app.config_dir):
                app.save_state(f"continuations/{session}", attempt)
                app.save_state(f"attempts/{attempt['id']}", attempt)
        return {"launch": launch_id, "continuation": attempt["id"], "session": session, "exit_code": completed.returncode}


def safe_id(value):
    require(isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value), "Invalid record ID.")
    return value


def handoff(app, config, args):
    if args.command == "handoff-start":
        authority = configured_authority(config)
        recipients = app.state("orchestrators", {})
        require(args.name in recipients, "Outgoing orchestrator must enroll first.")
        outgoing = recipients[args.name]
        require(app.live(outgoing, app.current()) == "active", "Cannot confirm the outgoing session is active.")
        snapshot = selection(config)
        choice = resolve(snapshot, "orchestrator", None)
        cwd = Path(args.cwd).resolve()
        require(cwd.is_dir(), "Successor working directory is missing.")
        brief = Path(args.brief).read_text()
        roadmap = Path(args.roadmap).resolve()
        roadmap_text = roadmap.read_text()
        require(brief.strip() and roadmap_text.strip(), "Provide both a substantial briefing and roadmap.")
        require(len(brief.encode()) + len(roadmap_text.encode()) <= 48000, "Brief and roadmap exceed 48 KiB.")
        tree = run_json(["cmux", "--json", "--id-format", "uuids", "tree", "--all"])
        panes = [p for w in tree["windows"] for ws in w["workspaces"]
                 if ws["id"].lower() == outgoing["workspace"].lower() for p in ws["panes"]
                 if any(s["id"].lower() == outgoing["surface"].lower() for s in p["surfaces"])]
        require(len(panes) == 1, "Cannot identify the outgoing tab's pane; no successor opened.")
        record = {"id": uuid.uuid4().hex, "name": args.name, "outgoing": outgoing, "choice": choice,
                  "cwd": str(cwd), "brief": brief, "roadmap_path": str(roadmap), "roadmap_snapshot": roadmap_text,
                  "status": "opening", "created_at": now()}
        cli = shlex.join([sys.executable, str(Path(__file__).resolve()), "--config-dir", str(app.config_dir.resolve()),
                          "--state-dir", str(app.state_dir.resolve())])
        prompt = (f"You are the successor orchestrator for {args.name}. Handoff ID: {record['id']}.\n"
                  f"Outgoing tab: {outgoing['surface']}; workspace: {outgoing['workspace']}.\n"
                  "During briefing, investigate read-only and do not dispatch changes or direct existing agents. "
                  "Compare the roadmap, current user direction, actual repository state and this briefing skeptically. "
                  "Ask the outgoing orchestrator necessary questions using cmux agent message to its exact tab. "
                  "Do not treat inherited decisions or claimed completion as established facts. "
                  f"When fully briefed, run {cli} handoff-ready {record['id']} --remaining-questions none. "
                  f"Wait for explicit ownership transfer before resuming work. After transfer, tell {authority} you are fully briefed, "
                  "state remaining uncertainties honestly, and ask permission to close the outgoing tab. "
                  "Keep the outgoing tab until that permission arrives, and check for dependent jobs before closing. "
                  f"Inspect handoff state using {cli} status.\n"
                  f"Roadmap source: {roadmap}\nRoadmap snapshot:\n{roadmap_text}\n\nOutgoing briefing:\n{brief}")
        argv = argv_for(choice, prompt, cwd, interactive=True)
        app.save_state(f"handoffs/{record['id']}", record)
        launch = {"id": record["id"], "choice": choice, "cwd": str(cwd), "created_at": now(),
                  "status": "handoff-opening", "task_digest": hashlib.sha256(prompt.encode()).hexdigest()}
        app.save_state(f"launches/{record['id']}", launch)
        try:
            opened = run_json(["cmux", "--json", "--id-format", "uuids", "new-surface", "--type", "terminal",
                               "--workspace", outgoing["workspace"], "--pane", panes[0]["id"],
                               "--working-directory", str(cwd), "--focus", "false", "--command",
                               shlex.join(["env", f"STARFLEET_MODEL_LAUNCH={record['id']}", *argv])])
            surface = opened.get("surface_id")
            require(surface and opened.get("workspace_id", outgoing["workspace"]).lower() == outgoing["workspace"].lower(),
                    "Successor creation returned an unexpected identity. Inspect cmux before retrying.")
            uuid.UUID(surface)
            record.update(status="briefing", successor_surface=surface, opened=opened)
            launch.update(status="handoff-briefing", surface=surface)
        except (PreferenceError, ValueError) as exc:
            record.update(status="opening-unconfirmed", error=str(exc))
            app.save_state(f"handoffs/{record['id']}", record)
            raise PreferenceError(f"Handoff {record['id']}: {exc}; inspect it before starting another successor.") from exc
        app.save_state(f"handoffs/{record['id']}", record)
        app.save_state(f"launches/{record['id']}", launch)
        return record
    record = app.state(f"handoffs/{safe_id(args.handoff)}", None)
    require(record, "Unknown handoff.")
    current = app.current()
    caller = run_json(["cmux", "--json", "--id-format", "uuids", "identify"]).get("caller", {})
    caller_surface = caller.get("surface_id")
    require(caller_surface, "cmux cannot identify the calling tab; no transition performed.")
    if args.command == "handoff-ready":
        require(caller_surface.lower() == record["successor_surface"].lower(), "Only the successor tab can report readiness.")
        require(record["status"] == "briefing", "Handoff is not awaiting briefing.")
        require(args.remaining_questions.strip().lower() == "none", "Resolve questions with the outgoing orchestrator before declaring readiness.")
        agents = [a for i in current["items"]
                  if any(p.get("panel_id", "").lower() == caller_surface.lower() for p in i.get("projections", []))
                  for a in i.get("agents", []) if a.get("kind") == record["choice"]["harness"] and
                  a.get("state") in ("working", "idle", "needs_input")]
        require(len(agents) == 1, "Cannot identify one live successor session; no ownership transferred.")
        record.update(status="ready", successor_session=agents[0]["session_id"], ready_at=now())
        record["next"] = "Outgoing orchestrator must stop initiating work and explicitly transfer ownership."
    elif args.command == "handoff-transfer":
        require(caller_surface.lower() == record["outgoing"]["surface"].lower(), "Only the outgoing tab can transfer ownership.")
        require(record["status"] == "ready", "Successor must declare readiness first.")
        require(app.live(record["outgoing"], current) == "active", "Outgoing session identity changed; reconcile before transferring.")
        successor = {"workspace": record["outgoing"]["workspace"], "surface": record["successor_surface"],
                     "session": record["successor_session"], "harness": record["choice"]["harness"]}
        require(app.live(successor, current) == "active", "Successor is not confirmed active.")
        recipients = app.state("orchestrators", {})
        require(recipients.get(record["name"]) == record["outgoing"], "Registered ownership changed; reconcile before transferring.")
        recipients[record["name"]] = successor
        app.save_state("orchestrators", recipients)
        record.update(status="transferred", transferred_at=now())
        record["next"] = "Successor may resume. Confirm you are fully briefed and ask the configured authority before closing the outgoing tab."
    elif args.command == "handoff-approve-close":
        require(record["status"] == "transferred", "Ownership must transfer before authorizing predecessor closure.")
        require(args.authorization.strip(), "Record the configured authority's explicit approval; do not infer it.")
        successor = {"workspace": record["outgoing"]["workspace"], "surface": record["successor_surface"],
                     "session": record["successor_session"], "harness": record["choice"]["harness"]}
        require(app.live(successor, current) == "active", "Cannot confirm the exact successor session.")
        authority = configured_authority(config)
        record.update(close_approval={"authority": authority, "authorization": args.authorization, "at": now()})
        record["next"] = (f"{authority} approved closing ONLY outgoing tab {record['outgoing']['surface']}. "
                          "Recheck its exact session and dependent jobs, then run handoff-close from your successor tab. "
                          f"Record this authorization: {args.authorization}. Keep your own tab open.")
    else:
        require(caller_surface.lower() == record["successor_surface"].lower(), "Only the successor tab can close its predecessor.")
        require(record["status"] == "transferred", "Ownership must transfer before closing.")
        require(args.authorization.strip(), "Record the configured authority's explicit permission to close this tab.")
        authority = configured_authority(config)
        require(app.live(record["outgoing"], current) == "active", "Outgoing identity changed or ended; inspect it manually.")
        # Closing a surface may terminate its processes. This is a separate, explicit authorization.
        record.update(status="close-requested", close_authorization=args.authorization,
                      close_authority=authority)
        app.save_state(f"handoffs/{record['id']}", record)
        try:
            run_json(["cmux", "--json", "close-surface", "--workspace", record["outgoing"]["workspace"],
                      "--surface", record["outgoing"]["surface"], "--force"])
            record.update(status="closed", closed_at=now())
        except PreferenceError as exc:
            # A cmux error can arrive after the surface was removed. Observe before retrying.
            record.update(status="close-unconfirmed", close_error=str(exc))
            try:
                tree = run_json(["cmux", "--json", "--id-format", "uuids", "tree", "--all"])
                present = any(s["id"].lower() == record["outgoing"]["surface"].lower()
                              for w in tree["windows"] for ws in w["workspaces"]
                              for p in ws["panes"] for s in p["surfaces"])
                if not present:
                    record.update(status="closed", closed_at=now(), close_observation="Tab absent after command error; error retained.")
            except PreferenceError:
                pass
    app.save_state(f"handoffs/{record['id']}", record)
    if args.command in ("handoff-ready", "handoff-transfer", "handoff-approve-close"):
        recipient = record["outgoing"] if args.command == "handoff-ready" else {
            "workspace": record["outgoing"]["workspace"], "surface": record["successor_surface"],
            "session": record["successor_session"], "harness": record["choice"]["harness"]}
        cli = shlex.join([sys.executable, str(Path(__file__).resolve()), "--config-dir", str(app.config_dir.resolve()),
                          "--state-dir", str(app.state_dir.resolve())])
        text = (f"Model Preferences handoff {record['id']}: {record['status']}. {record['next']} "
                f"Inspect {cli} status. " +
                (f"Once you have stopped initiating work, run {cli} handoff-transfer {record['id']} --outgoing-paused."
                 if args.command == "handoff-ready" else
                 "Use the recorded close_approval after checking dependent jobs." if args.command == "handoff-approve-close" else
                 "Ask the configured authority before closing the outgoing tab; do not infer permission."))
        try:
            record["notification"] = app.send_action(recipient, text)
        except PreferenceError as exc:
            record["notification"] = {"status": "failed", "error": str(exc)}
        app.save_state(f"handoffs/{record['id']}", record)
    return record


def main():
    try:
        args = parser().parse_args()
        if args.command == "review-guidance":
            sys.stdout.write(reviewer_guidance.REVIEWER_GUIDANCE)
            return 0
        result = execute(args)
        print(json.dumps(result, indent=2))
        if result.get("exit_code"):
            return result["exit_code"] if result["exit_code"] > 0 else 1
        notice = result.get("notification", {})
        if notice.get("inventory_error") or notice.get("inventory_truncated") or any(
                r["status"] in ("unknown", "failed") for r in notice.get("recipients", {}).values()):
            return 2
        return 0
    except (PreferenceError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"model-preferences: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
