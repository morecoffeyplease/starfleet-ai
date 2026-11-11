#!/usr/bin/env python3
"""Run the focused Replicator behavior checks."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


if __name__ == "__main__":
    raise SystemExit(subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "apps/replicator", "-p", "test_*.py", "-v"],
        cwd=ROOT,
    ).returncode)
