#!/usr/bin/env python3
"""Install an immutable away-mode runtime and one owned launcher."""

import argparse
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import sys


MARKER = "# starfleet-away-mode:v1"
FILES = ("apps/away-mode/away_mode.py", "apps/away-mode/guide.md", "apps/away-mode/README.md",
         "apps/model-preferences/model_preferences.py", "apps/model-preferences/cmux_transport.py")


def digest_source(source):
    digest = hashlib.sha256()
    for name in FILES:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update((source / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def install(source, share_dir, bin_dir):
    source = Path(source).resolve()
    share_dir = Path(share_dir).expanduser().absolute()
    bin_dir = Path(bin_dir).expanduser().absolute()
    digest = digest_source(source)
    runtime = share_dir / "runtimes" / digest
    runtime.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime.parent, 0o700)
    if not runtime.exists():
        staging = Path(tempfile.mkdtemp(prefix=".away-mode-", dir=runtime.parent))
        os.chmod(staging, 0o700)
        try:
            for name in FILES:
                (staging / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, staging / name)
                os.chmod(staging / name, 0o444)
            os.chmod(staging, 0o555)
            os.replace(staging, runtime)
        finally:
            if staging.exists():
                os.chmod(staging, 0o700)
                shutil.rmtree(staging)
    else:
        for name in FILES:
            if (runtime / name).read_bytes() != (source / name).read_bytes():
                raise RuntimeError(f"Immutable runtime digest collision: {runtime}")
    bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(bin_dir, bin_dir.stat().st_mode & 0o777)
    launcher = bin_dir / "away-mode"
    if launcher.exists() or launcher.is_symlink():
        if launcher.is_symlink() or not any(marker in launcher.read_text(encoding="utf-8") for marker in (MARKER, "# starfleet-away-mode-pilot")):
            raise RuntimeError(f"Refusing to replace unowned launcher: {launcher}")
    body = ("#!/bin/sh\n" + MARKER + "\n" +
            "exec " + shlex.quote(sys.executable) + " " + shlex.quote(str(runtime / "apps/away-mode/away_mode.py")) + " \"$@\"\n")
    temporary = launcher.with_name(launcher.name + ".tmp")
    temporary.write_text(body, encoding="utf-8")
    os.chmod(temporary, 0o755)
    os.replace(temporary, launcher)
    return {"runtime": str(runtime), "launcher": str(launcher), "sha256": digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--share-dir", type=Path,
                        default=Path.home() / ".local/share/starfleet-ai/away-mode")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    args = parser.parse_args()
    result = install(Path(__file__).resolve().parents[2], args.share_dir, args.bin_dir)
    print(f"Installed {result['launcher']}\nRuntime: {result['runtime']}\nSHA-256: {result['sha256']}")
    print("Existing state is preserved. No reporter, hooks or notification service was started.")


if __name__ == "__main__":
    main()
