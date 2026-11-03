#!/usr/bin/env python3
"""Install a durable Model Preferences command and optional additive user hooks."""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid

OWNER = "starfleet-model-preferences:v1"
LAUNCHER_MARKER = "# " + OWNER


def require(condition, message):
    if not condition:
        raise ValueError(message)


def atomic(path, data, mode=0o600):
    require(not path.is_symlink(), f"Refusing symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".model-preferences-")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, "Installation is busy; retry.")
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def owned(handler, harness):
    return (handler.get("type") == "command" and isinstance(handler.get("command"), str) and
            handler["command"].endswith(f" # {OWNER}:{harness}"))


def settings(path):
    require(not path.is_symlink(), f"Refusing symlink: {path}")
    raw = path.read_bytes() if path.exists() else None
    data = json.loads(raw) if raw is not None else {}
    require(isinstance(data, dict) and isinstance(data.get("hooks", {}), dict), "Settings/hooks must be objects.")
    for groups in data.get("hooks", {}).values():
        require(isinstance(groups, list), "Hook events must contain arrays.")
        for group in groups:
            require(isinstance(group, dict) and isinstance(group.get("hooks"), list), "Hook group requires a hooks array.")
            require(all(isinstance(h, dict) for h in group["hooks"]), "Hook handlers must be objects.")
    return raw, data


def plan_hooks(source, runtime, args, harness, path):
    raw, data = settings(path)
    command = [sys.executable, str(source), "--config-dir", str(args.config_dir), "--state-dir", str(args.state_dir)]
    active = subprocess.run([*command, "status"], capture_output=True, text=True, timeout=10)
    require(active.returncode == 0 and json.loads(active.stdout).get("active"),
            "Initialize and activate a profile before installing runtime hooks.")
    proc = subprocess.run([*command, "hook-config", "--harness", harness], capture_output=True, text=True, timeout=10)
    require(proc.returncode == 0, proc.stderr)
    fragment = json.loads(proc.stdout)
    result = copy.deepcopy(data)
    hooks = result.setdefault("hooks", {})
    for event, groups in list(hooks.items()):
        kept = []
        for group in groups:
            remaining = [h for h in group["hooks"] if not owned(h, harness)]
            if len(remaining) == len(group["hooks"]):
                kept.append(group)
            elif remaining:
                kept.append({**group, "hooks": remaining})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    for event, groups in fragment["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                argv = shlex.split(handler["command"])
                require(argv[1] == str(source), "Unexpected hook command source.")
                argv[1] = str(runtime)
                handler["command"] = shlex.join(argv) + f" # {OWNER}:{harness}"
            hooks.setdefault(event, []).append(group)
    return {"path": path, "raw": raw, "data": result, "changed": result != data, "harness": harness}


def install(args):
    for name in ("runtime_dir", "bin_dir", "config_dir", "state_dir", "claude_settings", "codex_settings"):
        setattr(args, name, Path(getattr(args, name)).expanduser().absolute())
    source = Path(__file__).resolve().with_name("model_preferences.py")
    transport_source = source.with_name("cmux_transport.py")
    runtime = args.runtime_dir / "model_preferences.py"
    transport_runtime = args.runtime_dir / "cmux_transport.py"
    launcher = args.bin_dir / "model-preferences"
    manifest = args.runtime_dir / "install.json"
    for path in (args.runtime_dir, args.bin_dir, runtime, transport_runtime, launcher, manifest):
        require(not path.is_symlink(), f"Refusing symlink: {path}")
    if launcher.exists():
        require(LAUNCHER_MARKER in launcher.read_text().splitlines()[:3], "Existing model-preferences command is not installer-owned.")
    previous = json.loads(manifest.read_text()) if manifest.exists() else {}
    if runtime.exists():
        require(previous.get("owner") == OWNER and previous.get("runtime_sha256") == hashlib.sha256(runtime.read_bytes()).hexdigest(),
                "Existing runtime is unowned or modified; installation refused.")
    if transport_runtime.exists():
        require(previous.get("owner") == OWNER and previous.get("transport_sha256") == hashlib.sha256(transport_runtime.read_bytes()).hexdigest(),
                "Existing transport runtime is unowned or modified; installation refused.")
    plans = [plan_hooks(source, runtime, args, h, args.claude_settings if h == "claude" else args.codex_settings)
             for h in dict.fromkeys(args.harness)]
    payload = source.read_bytes()
    transport_payload = transport_source.read_bytes()
    snapshots = {p: p.read_bytes() if p.exists() else None for p in (runtime, transport_runtime, manifest, launcher)}
    launcher_text = "#!/bin/sh\n" + LAUNCHER_MARKER + "\nexec " + shlex.join([
        sys.executable, str(runtime), "--config-dir", str(args.config_dir), "--state-dir", str(args.state_dir)]) + ' "$@"\n'
    with lock(args.runtime_dir / ".install.lock"):
        for path, before in snapshots.items():
            require((path.read_bytes() if path.exists() else None) == before, "Installed runtime changed concurrently; retry installation.")
        # Recheck every shared file before publishing any hook mutation.
        for plan in plans:
            require(settings(plan["path"])[0] == plan["raw"], "Hook settings changed concurrently; retry installation.")
        if not transport_runtime.exists() or transport_runtime.read_bytes() != transport_payload:
            atomic(transport_runtime, transport_payload)
        if not runtime.exists() or runtime.read_bytes() != payload:
            atomic(runtime, payload)
        if not launcher.exists() or launcher.read_text() != launcher_text:
            atomic(launcher, launcher_text.encode(), 0o755)
        changed = []
        for plan in plans:
            if not plan["changed"]:
                continue
            path = plan["path"]
            with lock(path.with_name(path.name + ".model-preferences.lock")):
                require(settings(path)[0] == plan["raw"], "Hook settings changed concurrently; retry installation.")
                backup = None
                mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
                if plan["raw"] is not None:
                    backup = path.with_name(path.name + ".model-preferences-backup-" + uuid.uuid4().hex)
                    atomic(backup, plan["raw"])
                atomic(path, (json.dumps(plan["data"], indent=2) + "\n").encode(), mode)
                changed.append({"harness": plan["harness"], "settings": str(path), "backup": str(backup) if backup else None})
        metadata = {"owner": OWNER, "runtime": str(runtime), "runtime_sha256": hashlib.sha256(payload).hexdigest(),
                    "transport_sha256": hashlib.sha256(transport_payload).hexdigest(),
                    "command": str(launcher), "config_dir": str(args.config_dir), "state_dir": str(args.state_dir)}
        if not manifest.exists() or json.loads(manifest.read_text()) != metadata:
            atomic(manifest, (json.dumps(metadata, indent=2) + "\n").encode())
    return {**metadata, "changed_settings": changed, "hook_trust": "Review the exact Codex hook through its supported /hooks controls; installation does not bypass trust."}


def main():
    home = Path.home()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runtime-dir", default=str(Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")) / "starfleet-ai/model-preferences"))
    p.add_argument("--bin-dir", default=str(home / ".local/bin"))
    p.add_argument("--config-dir", default=str(Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "starfleet-ai/model-preferences"))
    p.add_argument("--state-dir", default=str(Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")) / "starfleet-ai/model-preferences"))
    p.add_argument("--harness", choices=("claude", "codex"), action="append", default=[])
    p.add_argument("--claude-settings", default=str(home / ".claude/settings.json"))
    p.add_argument("--codex-settings", default=str(Path(os.environ.get("CODEX_HOME", home / ".codex")) / "hooks.json"))
    try:
        print(json.dumps(install(p.parse_args()), indent=2))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"model-preferences install: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
