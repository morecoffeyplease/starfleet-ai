#!/usr/bin/env python3
"""Opt-in tiny inference checks through the real PATH-selected harnesses."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--run", action="store_true", required=True)
p.add_argument("--harness", choices=("codex", "claude", "muse"), required=True)
p.add_argument("--out", required=True)
args = p.parse_args()
script = Path(__file__).resolve().parents[1] / "model_preferences.py"
root = Path(tempfile.mkdtemp(prefix="model-preferences-live-", dir="/private/tmp"))
repo = root / "repo"; repo.mkdir()
subprocess.run(["git", "init", "-q", str(repo)], check=True)
marker = "MODEL_PREFERENCES_LIVE_" + args.harness.upper() + "_OK"
brief = root / "brief.txt"
brief.write_text("This is a disposable harness acceptance check, not a delegated engineering task. "
                 "Do not use tools, read files, call other agents or change anything. Reply with exactly " + marker)
base = [sys.executable, str(script), "--config-dir", str(root / "config"), "--state-dir", str(root / "state")]
env = dict(os.environ, MUSE_NO_AUTO_UPDATE="1")
checks = []
for command in (["init", "--examples"], ["activate", {"codex": "Codex-only", "claude": "Claude-only", "muse": "Codex-Muse"}[args.harness],
                          "--orchestrators", "future-only"]):
    r = subprocess.run(base + command, env=env, capture_output=True, text=True, timeout=30)
    checks.append({"command": command, "exit_code": r.returncode})
    # No recipients are enrolled. A cmux inventory failure does not invalidate the committed local selection.
    if r.returncode not in (0, 2):
        raise SystemExit(r.stderr)
r = subprocess.run(base + ["dispatch", "--role", "implementer", "--cwd", str(repo), "--task-file", str(brief)],
                   env=env, capture_output=True, text=True, timeout=180)
(root / "stdout.txt").write_text(r.stdout)
(root / "stderr.txt").write_text(r.stderr)
launches = [json.loads(f.read_text()) for f in (root / "state/launches").glob("*.json")]
result = {"harness": args.harness, "fixture": str(root), "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
          "setup": checks, "exit_code": r.returncode, "marker_seen": marker in r.stdout,
          "launches": launches, "limits": ["One inference turn; no live hook/compaction or conversational handoff proof."]}
Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
sys.exit(0 if r.returncode == 0 and marker in r.stdout else 1)
