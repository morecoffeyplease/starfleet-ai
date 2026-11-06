#!/usr/bin/env python3
"""Canonical local verification for the Model Preferences pilot."""
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix="model-preferences-compile-") as tmp:
    for name in ("model_preferences.py", "install.py", "opencode_dispatch.py"):
        py_compile.compile(str(root / name), cfile=str(Path(tmp) / (name + "c")), doraise=True)
    transport = root / "cmux_transport.py"
    py_compile.compile(str(transport), cfile=str(Path(tmp) / "cmux_transport.pyc"), doraise=True)
    adapter = root.parent / "opencode-adapter" / "opencode_adapter.py"
    py_compile.compile(str(adapter), cfile=str(Path(tmp) / "opencode_adapter.pyc"), doraise=True)
sys.exit(subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", str(root / "tests"), "-v"]))
