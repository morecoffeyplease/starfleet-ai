#!/usr/bin/env python3
"""Install Discord Watch as an immutable local runtime without starting it."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys


MARKER = "# starfleet-discord-watch:v1"
SOURCE_NAMES = ("discord_watch.py", "guide.md", "README.md")


def atomic_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"Refusing symlink: {path}")
    temporary = path.with_name(path.name + "." + str(os.getpid()))
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install(runtime_root=None, bin_dir=None, uninstall=False):
    runtime_root = Path(runtime_root or Path.home() / ".local/share/starfleet-ai/discord-watch").expanduser()
    bin_dir = Path(bin_dir or Path.home() / ".local/bin").expanduser()
    launcher = bin_dir / "discord-watch"
    if launcher.is_symlink():
        raise ValueError("Refusing symlink launcher")
    if launcher.exists() and MARKER not in launcher.read_text(encoding="utf-8"):
        raise ValueError("Existing discord-watch command is not owned by this installer")
    if uninstall:
        launcher.unlink(missing_ok=True)
        return {"removed": str(launcher), "runtime_and_state_preserved": True}

    source_dir = Path(__file__).resolve().parent
    hailing_source = source_dir.parent / "hailing-frequencies" / "hailing_frequencies.py"
    opencode_adapter_source = source_dir.parent / "opencode-adapter" / "opencode_adapter.py"
    if not hailing_source.is_file():
        raise ValueError("Hailing Frequencies source dependency is unavailable")
    if not opencode_adapter_source.is_file() or opencode_adapter_source.is_symlink():
        raise ValueError("OpenCode session adapter source dependency is unavailable")
    sources = {name: (source_dir / name).read_bytes() for name in SOURCE_NAMES}
    sources["hailing_frequencies.py"] = hailing_source.read_bytes()
    sources["opencode_adapter.py"] = opencode_adapter_source.read_bytes()
    digest = hashlib.sha256(b"".join(
        name.encode("utf-8") + b"\0" + content for name, content in sources.items()
    )).hexdigest()
    runtime_root = runtime_root.absolute()
    runtime = runtime_root / digest
    if runtime_root.is_symlink() or runtime.is_symlink():
        raise ValueError("Refusing symlink runtime")
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_root.chmod(0o700)
    for name, content in sources.items():
        target = runtime / name
        if target.is_symlink():
            raise ValueError("Refusing symlink runtime source")
        if target.exists():
            if target.read_bytes() != content:
                raise ValueError("Runtime content does not match its hash")
        else:
            with target.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(0o444)

    bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = ("#!/bin/sh\n" + MARKER + "\nexec " + shlex.quote(sys.executable) + " " +
               shlex.quote(str((runtime / "discord_watch.py").absolute())) + ' "$@"\n')
    temporary = launcher.with_name(".discord-watch-" + str(os.getpid()))
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o755)
        os.replace(temporary, launcher)
    finally:
        temporary.unlink(missing_ok=True)
    atomic_json(runtime / "installation.json", {"sha256": digest, "python": sys.executable})
    return {"command": str(launcher), "runtime": str(runtime), "sha256": digest,
            "worker_started": False, "threads_registered": False}


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path,
                        default=Path.home() / ".local/share/starfleet-ai/discord-watch")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.runtime_root, args.bin_dir, args.uninstall), indent=2))
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + "\n")
