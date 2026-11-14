#!/usr/bin/env python3
"""Install Discord Watch as an immutable local runtime without starting it."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile


MARKER = "# starfleet-discord-watch:v1"
SOURCE_NAMES = ("discord_watch.py", "discord_gateway.py", "custody_adoption.py", "guide.md", "README.md",
                "dependency-manifest.json", "requirements.lock")
INTEGRITY_NAME = "runtime-integrity.json"
INSTALLATION_NAME = "installation.json"


def atomic_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"Refusing symlink: {path}")
    temporary = path.with_name(path.name + "." + str(os.getpid()))
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def provision_dependency(vendor_dir, lock_file):
    vendor_dir = Path(vendor_dir)
    vendor_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "--no-deps", "--no-compile", "--no-warn-script-location", "--target",
             str(vendor_dir), "--require-hashes", "-r", str(lock_file)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Pinned Gateway dependency provisioning failed") from None
    if result.returncode != 0:
        raise ValueError("Pinned Gateway dependency provisioning failed")


def verify_dependency(vendor_dir):
    vendor_dir = Path(vendor_dir).resolve()
    script = (
        "import pathlib, websocket; "
        "root=pathlib.Path(__import__('sys').argv[1]).resolve(); "
        "module=pathlib.Path(websocket.__file__).resolve(); "
        "assert websocket.__version__ == '1.9.2'; "
        "assert module.is_relative_to(root)"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(vendor_dir)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run([sys.executable, "-c", script, str(vendor_dir)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=20,
                                check=False, env=environment)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Pinned Gateway dependency integrity check failed") from None
    if result.returncode != 0:
        raise ValueError("Pinned Gateway dependency integrity check failed")


def _file_map(root):
    root = Path(root)
    values = {}
    for path in sorted(root.rglob("*")):
        if path.name in (INTEGRITY_NAME, INSTALLATION_NAME):
            continue
        if path.is_symlink():
            raise ValueError("Refusing symlink in immutable runtime")
        if path.is_file():
            values[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return values


def _content_digest(root, file_hashes):
    digest = hashlib.sha256()
    for relative, file_hash in sorted(file_hashes.items()):
        content = (Path(root) / relative).read_bytes()
        digest.update(relative.encode("utf-8") + b"\0" + content)
    return digest.hexdigest()


def _make_read_only(root):
    root = Path(root)
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            raise ValueError("Refusing symlink in immutable runtime")
        if path.is_dir():
            path.chmod(0o555)
        elif path.is_file():
            path.chmod(0o444)
    root.chmod(0o555)


def _remove_staging(root):
    root = Path(root)
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o700)
        elif path.exists() and not path.is_symlink():
            path.chmod(0o600)
    root.chmod(0o700)
    shutil.rmtree(root, ignore_errors=True)


def _remove_published_runtime(runtime, identity):
    runtime = Path(runtime)
    try:
        root_stat = runtime.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(root_stat.st_mode) or (root_stat.st_dev, root_stat.st_ino) != identity:
        raise RuntimeError("Refusing to remove a runtime not owned by this installation")

    runtime.chmod(0o700)
    for path in runtime.rglob("*"):
        path_stat = path.lstat()
        if stat.S_ISDIR(path_stat.st_mode):
            path.chmod(0o700)
        elif not stat.S_ISLNK(path_stat.st_mode):
            path.chmod(0o600)

    root_stat = runtime.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or (root_stat.st_dev, root_stat.st_ino) != identity:
        raise RuntimeError("Refusing to remove a runtime no longer owned by this installation")
    shutil.rmtree(runtime)
    if os.path.lexists(runtime):
        raise OSError(f"Published runtime still exists after cleanup: {runtime}")


def _require_sealed_runtime(runtime):
    runtime = Path(runtime)
    for path in (runtime, *runtime.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            expected = 0o555
        elif stat.S_ISREG(mode):
            expected = 0o444
        else:
            raise ValueError("Existing runtime contains an unsupported entry")
        if stat.S_IMODE(mode) != expected:
            raise ValueError(f"Existing runtime is not sealed: {runtime}")


def _verify_existing_runtime(runtime, expected_hashes, expected_digest):
    runtime = Path(runtime)
    if runtime.is_symlink() or not runtime.is_dir():
        raise ValueError("Refusing invalid immutable runtime")
    _require_sealed_runtime(runtime)
    if _file_map(runtime) != expected_hashes:
        raise ValueError("Existing runtime content does not match its content identity")
    try:
        integrity = json.loads((runtime / INTEGRITY_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Existing runtime integrity record is invalid") from None
    if integrity.get("files") != expected_hashes or integrity.get("sha256") != expected_digest:
        raise ValueError("Existing runtime integrity record does not match its content")


def install(runtime_root=None, bin_dir=None, uninstall=False, provisioner=None):
    if sys.version_info < (3, 10):
        raise ValueError("Discord Watch Gateway requires Python 3.10 or newer")
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
    temporal_source = source_dir.parent / "hailing-frequencies" / "temporal_beacon.py"
    opencode_adapter_source = source_dir.parent / "opencode-adapter" / "opencode_adapter.py"
    if not hailing_source.is_file():
        raise ValueError("Hailing Frequencies source dependency is unavailable")
    if not temporal_source.is_file() or temporal_source.is_symlink():
        raise ValueError("Temporal Beacon source dependency is unavailable")
    if not opencode_adapter_source.is_file() or opencode_adapter_source.is_symlink():
        raise ValueError("OpenCode session adapter source dependency is unavailable")
    sources = {name: (source_dir / name).read_bytes() for name in SOURCE_NAMES}
    sources["hailing_frequencies.py"] = hailing_source.read_bytes()
    sources["temporal_beacon.py"] = temporal_source.read_bytes()
    sources["opencode_adapter.py"] = opencode_adapter_source.read_bytes()
    from generate_dependency_lock import render
    if sources["requirements.lock"].decode("utf-8") != render(source_dir / "dependency-manifest.json"):
        raise ValueError("Discord Watch dependency lock is stale")
    runtime_root = runtime_root.absolute()
    if runtime_root.is_symlink():
        raise ValueError("Refusing symlink runtime root")
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_root.chmod(0o700)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=runtime_root))
    try:
        for name, content in sources.items():
            target = staging / name
            with target.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(0o600)
        vendor_dir = staging / "vendor"
        vendor_dir.mkdir(mode=0o700)
        provision = provisioner or provision_dependency
        provision(vendor_dir, staging / "requirements.lock")
        verify_dependency(vendor_dir)

        file_hashes = _file_map(staging)
        digest = _content_digest(staging, file_hashes)
        runtime = runtime_root / digest
        atomic_json(staging / INTEGRITY_NAME, {"sha256": digest, "files": file_hashes})
        atomic_json(staging / INSTALLATION_NAME, {"sha256": digest, "python": sys.executable})
        if runtime.exists():
            _verify_existing_runtime(runtime, file_hashes, digest)
            _remove_staging(staging)
        else:
            staging_stat = staging.lstat()
            identity = (staging_stat.st_dev, staging_stat.st_ino)
            os.replace(staging, runtime)
            try:
                _make_read_only(runtime)
            except BaseException as seal_error:
                try:
                    _remove_published_runtime(runtime, identity)
                except Exception as cleanup_error:
                    raise RuntimeError(
                        f"Could not seal new runtime {runtime}: {seal_error}; "
                        f"cleanup also failed: {cleanup_error}"
                    ) from seal_error
                raise

        bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        content = ("#!/bin/sh\n" + MARKER + "\nexec " + shlex.quote(sys.executable) + " " +
                   shlex.quote(str((runtime / "discord_watch.py").absolute())) + ' "$@"\n')
        temporary = bin_dir / (".discord-watch-" + str(os.getpid()))
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o755)
            os.replace(temporary, launcher)
        finally:
            temporary.unlink(missing_ok=True)
        return {"command": str(launcher), "runtime": str(runtime), "sha256": digest,
                "worker_started": False, "threads_registered": False,
                "gateway_dependency": "websocket-client 1.9.2"}
    except BaseException:
        _remove_staging(staging)
        raise


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
