#!/usr/bin/env python3
"""Install Starfleet Doctor into a user-local prefix."""

import argparse
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile


FILES = ("starfleet.py", "guide.md", "install.py")
LAUNCHER_MARKER = "# starfleet-doctor:v1"


def runtime_digest(source):
    digest = hashlib.sha256()
    for name in FILES:
        digest.update(name.encode("utf-8") + b"\0")
        digest.update((source / name).read_bytes())
    return digest.hexdigest()


def launcher_text(runtime, interpreter=None):
    interpreter = interpreter or sys.executable
    return ("#!/bin/sh\n" + LAUNCHER_MARKER + "\nexec " +
            shlex.quote(interpreter) + " " +
            shlex.quote(str(Path(runtime) / "starfleet.py")) + ' "$@"\n')


def ensure_supported_python(version_info=None):
    version_info = version_info or sys.version_info
    if tuple(version_info[:2]) < (3, 10):
        raise RuntimeError("Starfleet Doctor requires Python 3.10 or newer")


def ensure_directory(path, mode):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"Refusing symlink directory: {path}")
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"Path is not a directory: {path}")
        return
    path.mkdir(mode=mode)


def verify_runtime(runtime, source):
    if runtime.is_symlink() or not runtime.is_dir():
        raise ValueError("Existing Starfleet Doctor runtime is not a real directory")
    if set(item.name for item in runtime.iterdir()) != set(FILES):
        raise ValueError("Existing Starfleet Doctor runtime has unexpected files")
    if (runtime.stat().st_mode & 0o777) != 0o555:
        raise ValueError("Existing Starfleet Doctor runtime is not sealed")
    for name in FILES:
        item = runtime / name
        if item.is_symlink() or not item.is_file() or (item.stat().st_mode & 0o777) != 0o444:
            raise ValueError("Existing Starfleet Doctor runtime contains an unsealed file")
        if item.read_bytes() != (source / name).read_bytes():
            raise ValueError("Existing Starfleet Doctor runtime does not match its content address")


def launcher_is_owned(launcher):
    if launcher.is_symlink():
        return False
    try:
        with launcher.open("rb") as stream:
            first_lines = stream.read(512).decode("utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    return len(first_lines) > 1 and first_lines[1] == LAUNCHER_MARKER


def install(prefix, source=None):
    ensure_supported_python()
    source = Path(source or __file__).resolve()
    source = source if source.is_dir() else source.parent
    prefix = Path(prefix).expanduser().absolute()
    if prefix.is_symlink():
        raise ValueError(f"Refusing symlink prefix: {prefix}")
    digest = runtime_digest(source)
    runtime_root = prefix / "share" / "starfleet-ai" / "starfleet"
    runtime = runtime_root / digest
    bin_dir = prefix / "bin"
    launcher = bin_dir / "starfleet"
    if (launcher.is_symlink() or launcher.exists()) and not launcher_is_owned(launcher):
        raise ValueError(f"Refusing to replace an unowned command: {launcher}")
    prefix.mkdir(mode=0o755, parents=True, exist_ok=True)
    for directory in (prefix / "share", prefix / "share" / "starfleet-ai", runtime_root, bin_dir):
        ensure_directory(directory, 0o755)
    if runtime.exists() or runtime.is_symlink():
        verify_runtime(runtime, source)
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".starfleet-", dir=runtime_root))
        try:
            for name in FILES:
                shutil.copyfile(source / name, temporary / name)
                os.chmod(temporary / name, 0o444)
            os.chmod(temporary, 0o555)
            os.replace(temporary, runtime)
        finally:
            if temporary.exists():
                os.chmod(temporary, 0o700)
                shutil.rmtree(temporary)
    verify_runtime(runtime, source)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".starfleet-", dir=bin_dir)
    temporary_launcher = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(launcher_text(runtime, sys.executable))
        os.chmod(temporary_launcher, 0o755)
        os.replace(temporary_launcher, launcher)
    finally:
        temporary_launcher.unlink(missing_ok=True)
    return launcher, runtime


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local",
                        help="installation prefix (default: ~/.local)")
    args = parser.parse_args(argv)
    launcher, runtime = install(args.prefix)
    print(f"Installed Starfleet Doctor runtime {runtime.name[:12]}.")
    print(f"Add {launcher.parent} to PATH, then run: starfleet guide --print")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
