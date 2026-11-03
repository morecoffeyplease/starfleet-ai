#!/usr/bin/env python3
"""Opt-in cmux API check: message this caller and create/close a marker-only tab."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--run", action="store_true", required=True)
p.add_argument("--out", required=True)
args = p.parse_args()


def cmux(*argv, stdin=None):
    proc = subprocess.run(["cmux", "--json", "--id-format", "uuids", *argv], input=stdin,
                          text=True, capture_output=True, timeout=20)
    if proc.returncode:
        raise RuntimeError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout)


caller = cmux("identify")["caller"]
workspace, surface, pane = (caller[k] for k in ("workspace_id", "surface_id", "pane_id"))
root = Path(tempfile.mkdtemp(prefix="model-preferences-cmux-", dir="/private/tmp"))
marker = root / "marker.txt"
probe = root / "probe.py"
probe.write_text(f"from pathlib import Path\nimport time\nPath({str(marker)!r}).write_text('CMUX_TAB_OK')\ntime.sleep(60)\n")
opened = cmux("new-surface", "--type", "terminal", "--workspace", workspace, "--pane", pane,
              "--working-directory", str(root), "--focus", "false", "--command", shlex.join([sys.executable, str(probe)]))
result = {"caller": caller, "opened": opened}
Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
created = opened.get("surface_id")
if not created:
    raise RuntimeError("Unexpected creation result; inspect before retrying or closing anything.")
try:
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    result["marker_seen"] = marker.exists() and marker.read_text() == "CMUX_TAB_OK"
    tree = cmux("tree", "--all")
    placements = [(ws["id"], pn["id"]) for window in tree["windows"] for ws in window["workspaces"]
                  for pn in ws["panes"] for s in pn["surfaces"] if s["id"] == created]
    result["same_workspace_and_pane"] = placements == [(workspace, pane)]
    result["receipt"] = cmux("agent", "message", surface, "--from", "model-preferences-test", "--",
                             "Acceptance probe only: MODEL_PREFERENCES_CMUX_OK. Do not switch any profiles or alter your task. No reply needed.")
finally:
    # This tab runs only the marker process above. It is not an outgoing orchestrator.
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    try:
        result["closed_marker_tab"] = cmux("close-surface", "--workspace", workspace, "--surface", created, "--force")
    except RuntimeError as exc:
        result["close_error"] = str(exc)
    tree = cmux("tree", "--all")
    result["marker_tab_absent"] = not any(s["id"] == created for window in tree["windows"]
        for ws in window["workspaces"] for pn in ws["panes"] for s in pn["surfaces"])
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
sys.exit(0 if result["marker_seen"] and result["same_workspace_and_pane"] and result["marker_tab_absent"] else 1)
