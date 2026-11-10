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
    ("OpenCode adapter", ("-m", "unittest", "discover", "-s", "apps/opencode-adapter/tests", "-p", "test_*.py", "-v")),
    ("Away Mode", ("apps/away-mode/verify.py",)),
    ("Context Injector compatibility", ("-m", "unittest", "discover", "-s", "apps/context-injector", "-p", "test_*.py", "-v")),
    ("Discord Watch", ("apps/discord-watch/verify.py",)),
    ("Hailing Frequencies", ("apps/hailing-frequencies/verify.py",)),
    ("Pattern Buffer", ("-m", "unittest", "discover", "-s", "apps/pattern-buffer", "-p", "test_*.py", "-v")),
    ("Replicator", ("apps/replicator/verify.py",)),
    ("Starfleet Policy Guard", ("apps/starfleet-policy-guard/verify.py",)),
    ("Warp-drive", ("apps/warp-drive/verify.py",)),
)


def run(label, command, cwd=ROOT, env=None):
    print(f"== {label} ==", flush=True)
    result = subprocess.run(command, cwd=cwd, env=env)
    return result.returncode


def verify_python():
    failures = []
    for label, arguments in PYTHON_CHECKS:
        code = run(label, [sys.executable, *arguments])
        if code:
            failures.append((label, code))
    return failures


def verify_markup():
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        print("Starfleet Markup verification requires Node.js 22 or newer and npm.", file=sys.stderr)
        return [("Starfleet Markup prerequisites", 1)]
    version_result = subprocess.run([node, "--version"], capture_output=True, text=True)
    if version_result.returncode:
        if version_result.stderr:
            print(version_result.stderr, file=sys.stderr, end="")
        return [("Check Node.js version", version_result.returncode)]
    version = version_result.stdout.strip()
    try:
        major = int(version.lstrip("v").split(".", 1)[0])
    except ValueError:
        print(f"Could not parse Node.js version: {version}", file=sys.stderr)
        return [("Check Node.js version", 1)]
    if major < 22:
        print(f"Starfleet Markup requires Node.js 22 or newer, found {version}.", file=sys.stderr)
        return [("Check Node.js version", 1)]

    label = "Install locked Starfleet Markup dependencies"
    code = run(label, [
        npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"
    ], cwd=MARKUP)
    if code:
        return [(label, code)]

    with tempfile.TemporaryDirectory(prefix="starfleet-markup-verify-") as temporary:
        temp = Path(temporary)
        environment = os.environ.copy()
        environment["PLAYWRIGHT_BROWSERS_PATH"] = str(temp / "browsers")
        environment["STARFLEET_MARKUP_EVIDENCE"] = str(temp / "evidence")
        label = "Starfleet Markup source and unit verification"
        code = run(label, [
            npm, "run", "verify"
        ], cwd=MARKUP, env=environment)
        if code:
            return [(label, code)]
        label = "Install temporary Chromium browser"
        code = run(label, [
            str(MARKUP / "node_modules" / ".bin" / "playwright"), "install", "chromium"
        ], cwd=MARKUP, env=environment)
        if code:
            return [(label, code)]
        label = "Starfleet Markup browser verification"
        code = run(label, [
            npm, "run", "verify:browser"
        ], cwd=MARKUP, env=environment)
        if code:
            return [(label, code)]
    return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python-only",
        action="store_true",
        help="run all maintained Python checks and skip Node.js and browser verification",
    )
    args = parser.parse_args()

    failures = verify_python()
    if not args.python_only:
        failures.extend(verify_markup())
    if failures:
        print("\nVerification failures:", file=sys.stderr)
        for label, code in failures:
            print(f"- {label}: exit {code}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
