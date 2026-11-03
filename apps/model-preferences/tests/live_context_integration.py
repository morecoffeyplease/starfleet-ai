#!/usr/bin/env python3
"""Opt-in real Model Preferences -> Pattern Buffer -> Claude acceptance."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from claude_session import ClaudeSession

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--run", action="store_true", required=True)
p.add_argument("--injector", required=True, help="Reviewed source whose installed default Claude runtime matches.")
p.add_argument("--out", required=True)
args = p.parse_args()
root = Path(tempfile.mkdtemp(prefix="model-preferences-ci-", dir="/private/tmp"))
source = Path(args.injector).resolve()
injector = root / "injector" / "pattern_buffer.py"
injector.parent.mkdir()
shutil.copyfile(source, injector)
if (source.parent / "checkpoint_support.py").is_file():
    shutil.copyfile(source.parent / "checkpoint_support.py", injector.parent / "checkpoint_support.py")
for asset in ("enrollment_support.py", "pattern_buffer_checkpoint.py", "pattern_buffer_enrollment.py", "guide.md", "pattern-buffer", "pattern-buffer.template"):
    if (source.parent / asset).is_file():
        shutil.copyfile(source.parent / asset, injector.parent / asset)
if (source.parent / "templates").is_dir():
    shutil.copytree(source.parent / "templates", injector.parent / "templates")
app = Path(__file__).resolve().parents[1] / "model_preferences.py"
spec = importlib.util.spec_from_file_location("mp", app)
mp = importlib.util.module_from_spec(spec); spec.loader.exec_module(mp)
base = [sys.executable, str(app), "--config-dir", str(root / "config"), "--state-dir", str(root / "state")]
owner = root / "owner"; child = root / "child"
settings = root / "settings.json"
checks = []
events = []
client = None


def run(argv, env=None):
    result = subprocess.run([str(a) for a in argv], capture_output=True, text=True, env=env, timeout=180)
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:] or result.stdout[-4000:])
    return result.stdout


def cli(*argv):
    return json.loads(run([*base, *argv]))


def ci(*argv, cwd=owner):
    return run([sys.executable, injector, "--cwd", cwd, *argv])


def verify(reply, active, model, markers):
    assert active in reply and model in reply, reply
    assert all(markers[k] in reply for k in ("shared", "implementer", "task")), reply
    assert markers["orchestrator"] not in reply, reply


try:
    run(["git", "init", "-q", owner])
    run(["git", "-C", owner, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "fixture"])
    run(["git", "-C", owner, "worktree", "add", "-qb", "worker", child])
    ci("init", "--default-role", "orchestrator")
    markers = {k: k + "_" + uuid.uuid4().hex[:12] for k in ("shared", "implementer", "orchestrator", "task")}
    approved = (owner / ".context-injector/context.md")
    approved.write_text(f"# shared\nCI_SHARED={markers['shared']}\n# implementer\nCI_IMPLEMENTER={markers['implementer']}\n# orchestrator\nCI_ORCHESTRATOR={markers['orchestrator']}\n")
    approved_hash = hashlib.sha256(approved.read_bytes()).hexdigest()
    ci("install", "--harness", "claude", "--settings", settings, "--runtime-dir", root / "runtime")
    cli("init", "--examples"); cli("activate", "Claude-only", "--orchestrators", "future-only")
    choice = cli("resolve", "--role", "implementer")
    combined = json.loads(settings.read_text())
    for event, groups in cli("hook-config", "--harness", "claude")["hooks"].items():
        combined["hooks"].setdefault(event, []).extend(groups)
    # Generate a separate combined test document; never edit installed user settings.
    combined_path = root / "combined-settings.json"
    combined_path.write_text(json.dumps(combined, indent=2) + "\n")
    real_claude = shutil.which("claude")
    assert real_claude
    bindir = root / "bin"; bindir.mkdir()
    wrapper = bindir / "claude"
    testing = ["--setting-sources", "", "--settings", str(combined_path), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--tools", "", "--output-format", "json", "--system-prompt", "You are a tool-free context-delivery acceptance subject. Answer briefly."]
    wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(real_claude) + ' "$@" ' + shlex.join(testing) + "\n")
    wrapper.chmod(0o755)
    prompt = ("Reply only JSON containing active_profile and running_model from the latest Model Preferences runtime context, "
              "plus the latest CI_SHARED, CI_IMPLEMENTER, CI_ORCHESTRATOR and TASK_MARKER values. "
              "Use null for absent values. Never use tools or read files.")
    task = root / "brief.txt"; task.write_text(prompt + f"\nTASK_MARKER={markers['task']}\n")
    task_hash = hashlib.sha256(task.read_bytes()).hexdigest()
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"])
    env.pop("CLAUDECODE", None)
    launched = subprocess.run([*base, "dispatch", "--role", "implementer", "--cwd", str(child), "--task-file", str(task),
        "--pattern-buffer", str(injector), "--owner-worktree", str(owner)], env=env, capture_output=True, text=True, timeout=180)
    (root / "dispatch.stdout").write_text(launched.stdout)
    (root / "dispatch.stderr").write_text(launched.stderr)
    assert launched.returncode == 0, launched.stderr[-4000:]
    decoder = json.JSONDecoder(); remaining = launched.stdout.strip(); objects = []
    while remaining:
        value, end = decoder.raw_decode(remaining); objects.append(value); remaining = remaining[end:].lstrip()
    harness = next(x for x in objects if x.get("type") == "result")
    reply, session = harness["result"], harness["session_id"]
    verify(reply, "Claude-only", choice["model"], markers)
    task_id = re.search(r"Pattern Buffer dispatch ([a-f0-9]{32})", launched.stderr).group(1)
    binding = json.loads(ci("task-status", "--task-id", task_id, cwd=child))
    assert binding["session_id"] == session and binding["status"] == "active", binding
    launch = next(x for x in objects if "launch" in x)["launch"]
    checks.append({"check": "profile_selected_real_dispatch_with_exact_owner_task_binding", "passed": True,
                   "reply": reply, "model": choice["model"], "session": session, "launch": launch, "injector_task": task_id})
    cli("activate", "Codex-only", "--orchestrators", "future-only")
    clean_env = dict(os.environ)
    clean_env.pop("STARFLEET_MODEL_LAUNCH", None)
    clean_env.pop("CONTEXT_INJECTOR_TASK", None)
    client = ClaudeSession(cwd=child, settings=combined_path, model=choice["model"], effort=choice["effort"], session=session,
                           log=root / "resume.stderr", events=events, resume=True, env=clean_env)
    reply, _ = client.turn(prompt)
    verify(reply, "Codex-only", choice["model"], markers)
    checks.append({"check": "resume_restores_both_bindings_without_launch_environment", "passed": True, "reply": reply})
    _, compact = client.turn("/compact")
    assert any(e.get("subtype") == "compact_boundary" for e in compact)
    reply, _ = client.turn(prompt)
    verify(reply, "Codex-only", choice["model"], markers)
    checks.append({"check": "manual_compaction_keeps_actual_model_and_exact_task", "passed": True, "reply": reply})
    assert hashlib.sha256(approved.read_bytes()).hexdigest() == approved_hash
    assert hashlib.sha256(task.read_bytes()).hexdigest() == task_hash
    assert not (child / ".context-injector").exists()
    checks.append({"check": "switch_preserves_owner_text_task_and_child_link", "passed": True})
finally:
    if client:
        client.close()
    result = {"fixture": str(root), "source_sha256": hashlib.sha256(app.read_bytes()).hexdigest(),
              "injector_source_sha256": hashlib.sha256(injector.read_bytes()).hexdigest(), "injector_source": str(source),
              "checks": checks, "limits": ["Claude only; default runtime preflight must match the reviewed injector source.",
                  "Test-local PATH wrapper adds isolated combined hook settings and disables tools; shared shims are unchanged.",
                  "Manual compaction only; no automatic mid-turn compaction or operational workload."]}
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    (root / "events.json").write_text(json.dumps(events, indent=2) + "\n")
    print(json.dumps(result, indent=2))
