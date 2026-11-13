#!/usr/bin/env python3
"""Canonical isolated Hailing Frequencies verification; no live messages or service."""
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix="hailing-frequencies-compile-") as temporary:
    sources = [root / name for name in
               ("hailing_frequencies.py", "install.py", "service.py", "temporal_beacon.py",
                "discord_cleanup.py", "verify.py")]
    sources.append(root.parent / "opencode-adapter" / "opencode_adapter.py")
    for index, source in enumerate(sources):
        py_compile.compile(str(source), cfile=str(Path(temporary) / f"{index}-{source.name}c"), doraise=True)
for name in ("agent-notify", "tests/test_agent_notify.sh"):
    subprocess.run(["bash", "-n", str(root / name)], check=True)
subprocess.run(["bash", str(root / "tests/test_agent_notify.sh")], check=True)
sys.exit(subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", str(root / "tests"), "-v"]))
