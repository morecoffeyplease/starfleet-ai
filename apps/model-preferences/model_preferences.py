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
import subprocess
import sys
import tempfile
import time
import uuid


_transport_path = Path(__file__).resolve().with_name("cmux_transport.py")
_transport_spec = importlib.util.spec_from_file_location("model_preferences_cmux_transport", _transport_path)
cmux_transport = importlib.util.module_from_spec(_transport_spec)
_transport_spec.loader.exec_module(cmux_transport)
PreferenceError = cmux_transport.PreferenceError


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
    return {"schema_version": 1, "active": None, "activation": None, "models": models, "profiles": {
        "Claude-Codex": profile("claude-opus", "codex-luna", ["codex-sol", "claude-opus"],
                                ["claude-fable", "codex-astra"]),
        "Codex-only": profile("codex-sol", "codex-luna", ["codex-sol"], ["codex-astra"]),
        "Claude-only": profile("claude-opus", "claude-haiku", ["claude-opus"], ["claude-fable"]),
        "Claude-Muse": profile("claude-opus", "muse-spark", ["claude-opus"] * 2, ["claude-fable"] * 2),
        "Codex-Muse": profile("codex-sol", "muse-spark", ["codex-sol"], ["codex-astra"]),
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
        require(model.get("harness") in ("codex", "claude", "muse"), f"Unknown harness for {key}.")
        slug = model.get("slug")
        require(isinstance(slug, str) and slug and not slug.startswith("-") and not any(c.isspace() for c in slug),
                f"Exact model slug required for {key}.")
        require(isinstance(model.get("efforts"), list) and model["efforts"] and
                all(e in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
                    for e in model["efforts"]), f"Invalid supported efforts for {key}.")
        if model["harness"] == "muse":
            require(slug.endswith("-contributor"), f"Muse model {key} must use a contributor slug.")
    for name, profile in profiles.items():
        require(isinstance(name, str) and name and isinstance(profile, dict) and set(profile) == set(ROLES),
                f"Profile {name} must define exactly {', '.join(ROLES)}.")
        for role, entries in profile.items():
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
    roles = {}
    for role, entries in config["profiles"][name].items():
        group = role.endswith("_review")
        resolved = []
        for entry in entries if group else [entries]:
            model = config["models"][entry["model"]]
            resolved.append({"family": entry["model"], "harness": model["harness"],
                             "model": model["slug"], "effort": entry["effort"],
                             "contributor_mode": model["harness"] == "muse"})
        roles[role] = resolved if group else resolved[0]
    result = {"profile": name, "roles": roles, "review_rules": config["review_rules"],
              "approval_authority": config["approval_authority"]}
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
    result = dict(plan)
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
    sub.add_parser("list")
    sub.add_parser("status")
    startup = sub.add_parser("startup"); startup.add_argument("--expect-profile")
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
    model = sub.add_parser("model-set"); model.add_argument("family"); model.add_argument("--harness", choices=("claude", "codex", "muse"), required=True)
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
    for name in ("resolve", "dispatch"):
        s = sub.add_parser(name); s.add_argument("--role", choices=ROLES, required=True)
        s.add_argument("--slot", type=int, choices=(1, 2))
        if name == "dispatch":
            s.add_argument("--cwd", required=True); s.add_argument("--task-file", required=True)
            s.add_argument("--review-plan"); s.add_argument("--approval")
            s.add_argument("--pattern-buffer", "--context-injector", dest="context_injector", help="Pattern Buffer script; --context-injector is a compatibility alias."); s.add_argument("--owner-worktree")
            s.add_argument("--receipt-file", help="Forward a unique Pattern Buffer dispatch snapshot path; requires its script and owner worktree.")
    resume = sub.add_parser("resume", help="Continue an exited, identity-verified Codex launch with its recorded choice.")
    resume.add_argument("launch"); resume.add_argument("--task-file", required=True)
    resume.add_argument("--review-plan"); resume.add_argument("--approval")
    review = sub.add_parser("review-plan"); review.add_argument("--cwd", required=True)
    review.add_argument("--base", required=True); review.add_argument("--facts", required=True)
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


def execute(args):
    app = App(args.config_dir, args.state_dir)
    command = args.command
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
        if command == "status":
            active = selection(config) if config["active"] else None
            notice = app.state("notification", {})
            handoffs = [read(p) for p in sorted((app.state_dir / "handoffs").glob("*.json"))]
            recipients = app.state("orchestrators", {})
            return {"active": active,
                    "notification": notice,
                    "notification_pending": bool(active and notice.get("selection") != active["selection"]),
                    "handoffs": handoffs,
                    "orchestrators": recipients, "adoption": adoption_status(app, active, recipients),
                    "adoption_note": "Explicit owner acknowledgment of saved choices for future dispatches, not proof of subsequent behavior or a live session.",
                    "coverage": "Only explicitly enrolled roles; all active orchestrators must enroll."}
        if command in ("activate", "model-set", "profile-put", "authority-set"):
            original = copy.deepcopy(config)
            before = selection(config) if config["active"] else None
            if command == "activate":
                require(args.profile in config["profiles"], "Unknown profile; create it with profile-put first.")
                config["active"] = args.profile
            elif command == "model-set":
                require(args.family and not any(c.isspace() for c in args.family), "Model family must be a nonempty token.")
                config["models"][args.family] = {"harness": args.harness, "slug": args.slug,
                                                  "efforts": args.efforts, "source": args.source}
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
            plan.update(selection=selection(config)["selection"], id=uuid.uuid4().hex)
            app.save_state(f"reviews/{plan['id']}", plan)
            return plan if args.full else review_summary(plan)
        elif command == "review-inspect":
            plan = app.state(f"reviews/{safe_id(args.plan)}", None)
            require(plan, "Unknown review plan.")
            return {**(plan if args.full else review_summary(plan)), "saved_snapshot": True}
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
            return resolve(selection(config), args.role, args.slot)
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
            if binding:
                launch = app.state(f"launches/{safe_id(binding['launch'])}", None)
                require(launch, "Running task choice is missing.")
                choice = launch["choice"]
                text += (f" This running task actually launched with profile {choice['profile']}, role {choice['role']}, "
                         f"model {choice['model']}, effort {choice['effort']}, selection {choice['selection'][:12]}.")
                text += " " + DELEGATION
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
        elif command == "dispatch":
            require(not args.receipt_file or (args.context_injector and args.owner_worktree),
                    "--receipt-file requires --context-injector and --owner-worktree.")
            choice = resolve(selection(config), args.role, args.slot)
            require(args.role != "orchestrator", "Use handoff-start for successor orchestrators.")
            cwd = Path(args.cwd).resolve(); require(cwd.is_dir(), "Task directory is missing.")
            prompt = Path(args.task_file).read_text()
            require(0 < len(prompt.encode()) <= 16384, "Task brief must be 1 to 16384 bytes.")
            validate_review(app, config, args, choice, cwd)
            require(bool(args.context_injector) == bool(args.owner_worktree), "Pattern Buffer needs both its script and --owner-worktree.")
            if args.context_injector:
                require(args.role == "implementer" and choice["harness"] in ("claude", "codex"),
                        f"Pattern Buffer automatic task replay is unavailable for role {args.role} on harness {choice['harness']}. "
                        "It supports Claude/Codex implementers only. Use plain Model Preferences dispatch without "
                        "--context-injector, --owner-worktree or --receipt-file, putting required context in --task-file "
                        "and retaining applicable review/permission flags. "
                        "Guide: apps/model-preferences/README.md#pattern-buffer-support")
            prompt = DELEGATION + " Use the current researcher role for authorized research. Verify its findings.\n\n" + prompt
            argv = argv_for(choice, prompt, cwd)
            launch_id = uuid.uuid4().hex
            adapter = None
            if args.context_injector:
                receipt = (Path(args.receipt_file).expanduser().absolute() if args.receipt_file else
                           app.state_dir.resolve() / "ci-receipts" / f"{launch_id}.json")
                adapter = {"script": str(Path(args.context_injector).resolve()),
                           "owner_worktree": str(Path(args.owner_worktree).resolve()), "receipt": str(receipt)}
                # The owning app supplies stdin. Do not change its approved brief.
                argv = argv_for(choice, "-", cwd)
                if choice["harness"] == "claude":
                    argv = argv[:-1]
                argv = [sys.executable, str(Path(args.context_injector).resolve()), "--cwd", str(cwd), "dispatch",
                        "--owner-worktree", str(Path(args.owner_worktree).resolve()), "--harness", choice["harness"],
                        "--task-file", str(Path(args.task_file).resolve()),
                        "--receipt-file", str(receipt),
                        "--", *argv]
            launch = {"id": launch_id, "choice": choice, "cwd": str(cwd), "created_at": now(),
                      "context_injector": adapter, "slot": args.slot,
                      "task_digest": hashlib.sha256(Path(args.task_file).read_bytes()).hexdigest(), "status": "starting"}
            app.save_state(f"launches/{launch['id']}", launch)
        elif command.startswith("handoff-"):
            return handoff(app, config, args)
        else:
            raise PreferenceError("Unsupported command.")
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
    require(args.review_plan, "Review dispatch requires a review-plan, including standard review.")
    plan = app.state(f"reviews/{safe_id(args.review_plan)}", None)
    require(plan, "Unknown review plan.")
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


def resume_launch(app, args):
    launch_id = safe_id(args.launch)
    # Snapshot before side effects, then freeze the exact bytes for both CI update and native stdin.
    brief = Path(args.task_file).read_bytes()
    require(0 < len(brief) <= 16384 and brief.decode().strip(), "Task brief must be 1 to 16384 bytes of nonempty UTF-8.")
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
            prompt = DELEGATION + " Use the current researcher role for authorized research.\n\n" + brief.decode()
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
        result = execute(parser().parse_args())
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
