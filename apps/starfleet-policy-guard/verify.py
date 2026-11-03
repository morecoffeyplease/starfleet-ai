#!/usr/bin/env python3
"""Canonical local checks; real LLM runtime smoke tests are explicitly opt-in."""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
for source in root.rglob("*.py"):
    ast.parse(source.read_text(), filename=str(source))
policy = json.loads((root / "config/default-policy.json").read_text())
assert policy["zoom_out"]["first_required_round"] == 3
assert policy["zoom_out"]["repeat"] == "every-subsequent-fix-round"
assert "{N}" in policy["zoom_out"]["instruction_template"]
assert "{pr}" in policy["zoom_out"]["instruction_template"]
result = subprocess.run(
    [sys.executable, "-m", "unittest", "discover", "-s", str(root / "tests"), "-v"],
    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
)
raise SystemExit(result.returncode)
