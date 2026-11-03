#!/usr/bin/env python3
"""Canonical isolated verification for Away Mode and its shared adapter."""
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix="away-mode-compile-") as tmp:
    py_compile.compile(str(root / "away_mode.py"), cfile=str(Path(tmp) / "app.pyc"), doraise=True)
for command in ([sys.executable, "-m", "unittest", "discover", "-s", str(root / "tests"), "-v"],
                [sys.executable, str(root.parent / "model-preferences/verify.py")]):
    result = subprocess.call(command)
    if result:
        sys.exit(result)
