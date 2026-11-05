#!/usr/bin/env python3
"""Render the pinned Discord Watch dependency lock from its source manifest."""

import argparse
import json
from pathlib import Path
import re


HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "dependency-manifest.json"
LOCK = HERE / "requirements.lock"


def render(manifest_path=MANIFEST):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    version_floor = manifest.get("python_requires")
    if version_floor != ">=3.10":
        raise ValueError("Discord Watch dependency manifest must require Python >=3.10")
    dependencies = manifest.get("dependencies")
    if not isinstance(dependencies, list) or len(dependencies) != 1:
        raise ValueError("Discord Watch requires exactly one locked runtime dependency")
    dependency = dependencies[0]
    name = dependency.get("name")
    version = dependency.get("version")
    wheel = dependency.get("wheel")
    digest = dependency.get("sha256")
    if name != "websocket-client" or version != "1.9.2":
        raise ValueError("Discord Watch websocket-client version is not the supported pin")
    if wheel != "websocket_client-1.9.2-py3-none-any.whl":
        raise ValueError("Discord Watch websocket-client wheel is not the supported artifact")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("Discord Watch dependency wheel hash is invalid")
    return f"{name}=={version} --hash=sha256:{digest}\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render()
    if args.check:
        if not LOCK.is_file() or LOCK.read_text(encoding="utf-8") != expected:
            parser.error("requirements.lock is stale; run generate_dependency_lock.py")
        return 0
    LOCK.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
