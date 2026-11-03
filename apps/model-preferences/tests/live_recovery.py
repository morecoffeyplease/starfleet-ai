#!/usr/bin/env python3
"""Opt-in tool-free Claude proof of startup, changed-profile compaction and resume."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from claude_session import ClaudeSession

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--run", action="store_true", required=True)
p.add_argument("--out", required=True)
args = p.parse_args()
script = Path(__file__).resolve().parents[1] / "model_preferences.py"
spec = importlib.util.spec_from_file_location("mp", script)
mp = importlib.util.module_from_spec(spec); spec.loader.exec_module(mp)
root = Path(tempfile.mkdtemp(prefix="model-preferences-recovery-", dir="/private/tmp"))
repo = root / "repo"; repo.mkdir()
subprocess.run(["git", "init", "-q", str(repo)], check=True)
base = ["--config-dir", str(root / "config"), "--state-dir", str(root / "state")]


def app(*argv):
    return mp.execute(mp.parser().parse_args([*base, *argv]))


app("init", "--examples"); app("activate", "Claude-only", "--orchestrators", "future-only")
choice = app("resolve", "--role", "implementer")
ticket = uuid.uuid4().hex
store = mp.App(root / "config", root / "state")
store.save_state(f"launches/{ticket}", {"id": ticket, "choice": choice, "cwd": str(repo), "status": "live-recovery-test"})
settings = root / "settings.json"
settings.write_text(json.dumps(app("hook-config", "--harness", "claude")))
session = str(uuid.uuid4())
prompt = ("Reply only JSON with active_profile and running_model from the latest Model Preferences runtime context. "
          "Use null if absent. Do not use tools or read files. Do not infer the active profile from the running model.")
checks = []
events = []


def Client(resume=False):
    env = dict(os.environ)
    env.pop("STARFLEET_MODEL_LAUNCH", None)
    if not resume:
        env["STARFLEET_MODEL_LAUNCH"] = ticket
    return ClaudeSession(cwd=repo, settings=settings, model=choice["model"], effort=choice["effort"],
                         session=session, log=root / "stderr.log", events=events, resume=resume, env=env)


client = Client()
try:
    reply, _ = client.turn(prompt)
    assert "Claude-only" in reply and choice["model"] in reply, reply
    checks.append({"check": "startup_actual_choice", "reply": reply, "passed": True})
    app("activate", "Codex-only", "--orchestrators", "future-only")
    _, compact_events = client.turn("/compact")
    assert any(e.get("subtype") == "compact_boundary" for e in compact_events), "No compact_boundary"
    reply, _ = client.turn(prompt)
    assert "Codex-only" in reply and choice["model"] in reply, reply
    checks.append({"check": "manual_compaction_fresh_active_original_running_choice", "reply": reply, "passed": True})
    client.close(); client = Client(resume=True)
    reply, _ = client.turn(prompt)
    assert "Codex-only" in reply and choice["model"] in reply, reply
    checks.append({"check": "resume_without_launch_environment", "reply": reply, "passed": True})
finally:
    client.close()
    result = {"fixture": str(root), "session": session, "launch": ticket,
              "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(), "checks": checks,
              "limits": ["Claude only; manual compaction, not automatic mid-turn compaction.",
                         "Separate temporary hook settings; no machine hook installation."]}
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    (root / "events.json").write_text(json.dumps(events, indent=2) + "\n")
    print(json.dumps(result, indent=2))
