#!/usr/bin/env python3
"""Install an immutable local runtime; never enable a service or edit harness hooks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

from hailing_frequencies import atomic_json, require

MARKER = "# starfleet-hailing-frequencies:v1"
BEACON_MARKER = "# starfleet-temporal-beacon:v1"
AGENT_NOTIFY_MARKER = "# starfleet-agent-notify:v1"
SOURCE_NAMES = ("hailing_frequencies.py", "service.py", "temporal_beacon.py", "guide.md",
                "agent-notify", "agent-notify-guide.md")
SKILL_NAME = "skills/temporal-beacon/SKILL.md"
AGENT_NOTIFY_SKILL_NAME = "skills/agent-notify/SKILL.md"
SKILL_NAMES = (SKILL_NAME, AGENT_NOTIFY_SKILL_NAME)


def install(runtime_root, bin_dir, uninstall=False):
    runtime_root, bin_dir = Path(runtime_root).expanduser(), Path(bin_dir).expanduser()
    launcher = bin_dir / "hailing-frequencies"
    beacon_launcher = bin_dir / "temporal-beacon"
    agent_notify_launcher = bin_dir / "agent-notify"
    require(not launcher.is_symlink(), "Refusing symlink launcher")
    if launcher.exists():
        require(MARKER in launcher.read_text(), "Existing command is not owned by this installer")
    require(not beacon_launcher.is_symlink(), "Refusing symlink Temporal Beacon launcher")
    if beacon_launcher.exists():
        require(BEACON_MARKER in beacon_launcher.read_text(), "Existing Temporal Beacon command is not owned by this installer")
    require(not agent_notify_launcher.is_symlink(), "Refusing symlink agent-notify launcher")
    if agent_notify_launcher.exists():
        require(AGENT_NOTIFY_MARKER in agent_notify_launcher.read_text(), "Existing agent-notify command is not owned by this installer")
    if uninstall:
        launcher.unlink(missing_ok=True)
        beacon_launcher.unlink(missing_ok=True)
        agent_notify_launcher.unlink(missing_ok=True)
        return {"removed": str(launcher), "temporal_beacon_removed": str(beacon_launcher),
                "agent_notify_removed": str(agent_notify_launcher), "runtime_and_state_preserved": True}
    sources = {name: Path(__file__).with_name(name).read_bytes() for name in SOURCE_NAMES}
    for skill_name in SKILL_NAMES:
        skill_source = Path(__file__).parent / skill_name
        if skill_source.is_file():
            sources[skill_name] = skill_source.read_bytes()
    digest = hashlib.sha256(b"".join(name.encode() + b"\0" + content for name, content in sources.items())).hexdigest()
    runtime = runtime_root / digest
    require(not runtime_root.is_symlink() and not runtime.is_symlink(), "Refusing symlink runtime")
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, source in sources.items():
        target = runtime / name
        require(not target.is_symlink(), "Refusing symlink source")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            require(target.read_bytes() == source, "Runtime content does not match its hash")
        else:
            with target.open("xb") as stream:
                stream.write(source)
            target.chmod(0o444)
    target = runtime / "hailing_frequencies.py"
    agent_notify_target = runtime / "agent-notify"
    bin_dir.mkdir(parents=True, exist_ok=True)
    content = ("#!/bin/sh\n" + MARKER + "\nexec " + shlex.quote(sys.executable) + " " +
               shlex.quote(str(target.absolute())) + ' "$@"\n')
    beacon_content = ("#!/bin/sh\n" + BEACON_MARKER + "\nexec " + shlex.quote(sys.executable) + " " +
                      shlex.quote(str(target.absolute())) + ' temporal "$@"\n')
    agent_notify_content = ("#!/bin/sh\n" + AGENT_NOTIFY_MARKER + "\nexec /bin/bash " +
                            shlex.quote(str(agent_notify_target.absolute())) + ' "$@"\n')
    temporary = launcher.with_name(".hailing-frequencies-" + str(os.getpid()))
    beacon_temporary = beacon_launcher.with_name(".temporal-beacon-" + str(os.getpid()))
    agent_notify_temporary = agent_notify_launcher.with_name(".agent-notify-" + str(os.getpid()))
    try:
        with temporary.open("x") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o755)
        os.replace(temporary, launcher)
        with beacon_temporary.open("x") as stream:
            stream.write(beacon_content)
            stream.flush()
            os.fsync(stream.fileno())
        beacon_temporary.chmod(0o755)
        os.replace(beacon_temporary, beacon_launcher)
        with agent_notify_temporary.open("x") as stream:
            stream.write(agent_notify_content)
            stream.flush()
            os.fsync(stream.fileno())
        agent_notify_temporary.chmod(0o755)
        os.replace(agent_notify_temporary, agent_notify_launcher)
    finally:
        temporary.unlink(missing_ok=True)
        beacon_temporary.unlink(missing_ok=True)
        agent_notify_temporary.unlink(missing_ok=True)
    atomic_json(runtime / "installation.json", {"sha256": digest, "python": sys.executable})
    return {"command": str(launcher), "temporal_beacon_command": str(beacon_launcher),
            "agent_notify_command": str(agent_notify_launcher),
            "skill": str(runtime / SKILL_NAME) if SKILL_NAME in sources else None,
            "agent_notify_skill": str(runtime / AGENT_NOTIFY_SKILL_NAME) if AGENT_NOTIFY_SKILL_NAME in sources else None,
            "runtime": str(runtime), "service_started": False, "fleet_enrolled": False}


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=Path.home() / ".local/share/starfleet-ai/hailing-frequencies")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.runtime_root, args.bin_dir, args.uninstall), indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
