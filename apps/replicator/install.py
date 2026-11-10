#!/usr/bin/env python3
"""Install the local Replicator guide launcher from this source checkout."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

MARKER = "# starfleet-replicator:v1"
SOURCE_NAMES = ("replicator.py", "guide.md", "README.md")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def install(runtime_root, bin_dir):
    runtime_root = Path(runtime_root).expanduser()
    bin_dir = Path(bin_dir).expanduser()
    launcher = bin_dir / "replicator"

    require(not launcher.is_symlink(), "Refusing symlink launcher")
    if launcher.exists():
        require(MARKER.encode() in launcher.read_bytes(), "Existing command is not owned by this installer")

    sources = {}
    for name in SOURCE_NAMES:
        source = Path(__file__).with_name(name)
        require(source.is_file() and not source.is_symlink(), f"Source file is unavailable: {name}")
        sources[name] = source.read_bytes()

    content = b"".join(name.encode() + b"\0" + data for name, data in sources.items())
    digest = hashlib.sha256(content).hexdigest()
    runtime = runtime_root / digest
    require(not runtime_root.is_symlink(), "Refusing symlink runtime root")
    require(not runtime.is_symlink(), "Refusing symlink runtime")
    if runtime_root.exists():
        require(runtime_root.is_dir(), "Runtime root is not a directory")
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime.mkdir(mode=0o700, exist_ok=True)

    for name, data in sources.items():
        target = runtime / name
        require(not target.is_symlink(), f"Refusing symlink runtime file: {name}")
        if target.exists():
            require(target.is_file() and target.read_bytes() == data,
                    f"Runtime content does not match its hash: {name}")
        else:
            try:
                with target.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                require(target.is_file() and target.read_bytes() == data,
                        f"Runtime content does not match its hash: {name}")
        target.chmod(0o444)

    bin_dir.mkdir(parents=True, exist_ok=True)
    script = runtime / "replicator.py"
    launcher_text = (
        "#!/bin/sh\n"
        + MARKER
        + "\nexec "
        + shlex.quote(sys.executable)
        + " "
        + shlex.quote(str(script.absolute()))
        + ' "$@"\n'
    )
    temporary = launcher.with_name(f".replicator-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(launcher_text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o755)
        os.replace(temporary, launcher)
    finally:
        temporary.unlink(missing_ok=True)

    return {"command": str(launcher), "runtime": str(runtime), "guide": str(runtime / "guide.md")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path.home() / ".local/share/starfleet-ai/replicator",
    )
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(install(args.runtime_root, args.bin_dir), indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
