#!/usr/bin/env python3
"""Run the maintained local verification commands for every Starfleet component."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MARKUP = ROOT / "apps" / "starfleet-markup"

PYTHON_CHECKS = (
    ("Away Mode", ("apps/away-mode/verify.py",)),
    ("Context Injector compatibility", ("-m", "unittest", "discover", "-s", "apps/context-injector", "-p", "test_*.py", "-v")),
    ("Discord Watch", ("apps/discord-watch/verify.py",)),
    ("Hailing Frequencies", ("apps/hailing-frequencies/verify.py",)),
    ("Pattern Buffer", ("-m", "unittest", "discover", "-s", "apps/pattern-buffer", "-p", "test_*.py", "-v")),
    ("Starfleet Policy Guard", ("apps/starfleet-policy-guard/verify.py",)),
    ("Warp-drive", ("apps/warp-drive/verify.py",)),
)


def run(label, command, cwd=ROOT, env=None):
    print(f"== {label} ==", flush=True)
    result = subprocess.run(command, cwd=cwd, env=env)
    if result.returncode:
        raise SystemExit(result.returncode)


def verify_python():
    for label, arguments in PYTHON_CHECKS:
        run(label, [sys.executable, *arguments])


def verify_markup():
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise SystemExit("Starfleet Markup verification requires Node.js 22 or newer and npm.")
    version = subprocess.run([node, "--version"], check=True, capture_output=True, text=True).stdout.strip()
    try:
        major = int(version.lstrip("v").split(".", 1)[0])
    except ValueError:
        raise SystemExit(f"Could not parse Node.js version: {version}") from None
    if major < 22:
        raise SystemExit(f"Starfleet Markup requires Node.js 22 or newer, found {version}.")

    run("Install locked Starfleet Markup dependencies", [
        npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"
    ], cwd=MARKUP)

    with tempfile.TemporaryDirectory(prefix="starfleet-markup-verify-") as temporary:
        temp = Path(temporary)
        environment = os.environ.copy()
        environment["PLAYWRIGHT_BROWSERS_PATH"] = str(temp / "browsers")
        environment["STARFLEET_MARKUP_EVIDENCE"] = str(temp / "evidence")
        run("Starfleet Markup source and unit verification", [
            npm, "run", "verify"
        ], cwd=MARKUP, env=environment)
        run("Install temporary Chromium browser", [
            str(MARKUP / "node_modules" / ".bin" / "playwright"), "install", "chromium"
        ], cwd=MARKUP, env=environment)
        run("Starfleet Markup browser verification", [
            npm, "run", "verify:browser"
        ], cwd=MARKUP, env=environment)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python-only",
        action="store_true",
        help="run all maintained Python checks and skip Node.js and browser verification",
    )
    args = parser.parse_args()

    verify_python()
    if not args.python_only:
        verify_markup()


if __name__ == "__main__":
    main()
