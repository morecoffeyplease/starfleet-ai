"""Check tracked Markdown links, portable examples and whitespace without network access."""

from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit


LINK = re.compile(r"!?\[[^\]\n]*\]\((<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")
PRIVATE = re.compile(r"/Users/[^/\s]+/|/home/[^/\s]+/|https://discord(?:app)?\.com/api/webhooks/[0-9]+/[^\s)]+|\b(?:github_pat_|ghp_)[A-Za-z0-9_]{20,}")


def verify(root, base=None):
    root = Path(root).resolve()
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")
    failures = []
    for raw in tracked:
        if not raw or not raw.endswith(b".md"):
            continue
        path = root / raw.decode()
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if PRIVATE.search(text):
            failures.append(f"{path.relative_to(root)}: nonportable path or credential-shaped content")
        text = re.sub(r"```[^\n]*\n.*?```", "", text, flags=re.S)
        text = re.sub(r"`[^`\n]+`", "", text)
        for match in LINK.finditer(text):
            target = match[1].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
                continue
            resolved = (path.parent / unquote(parsed.path)).resolve()
            if not resolved.is_relative_to(root) or not resolved.exists():
                failures.append(f"{path.relative_to(root)}: missing local link {target}")
    result = subprocess.run(["git", "diff", "--check", "HEAD"], cwd=root)
    if result.returncode:
        failures.append("source whitespace check failed")
    committed = subprocess.run(["git", "diff", "--check", base, "HEAD"] if base else
                               ["git", "show", "--format=", "--check", "HEAD"], cwd=root)
    if committed.returncode:
        failures.append("committed source whitespace check failed")
    for failure in failures:
        print(failure)
    print("Documentation, local links and portable-example privacy checks " + ("FAILED" if failures else "passed"))
    return 1 if failures else 0
