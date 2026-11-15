"""Select maintained checks from a conservative source dependency map."""

from pathlib import PurePosixPath
import subprocess


COMPONENT_CHECKS = {
    "opencode-adapter": ("OpenCode adapter",),
    "away-mode": ("Away Mode",),
    "context-injector": ("Context Injector compatibility",),
    "discord-watch": ("Discord Watch",),
    "hailing-frequencies": ("Hailing Frequencies",),
    "model-preferences": ("Model Preferences",),
    "pattern-buffer": ("Pattern Buffer",),
    "replicator": ("Replicator",),
    "starfleet-policy-guard": ("Starfleet Policy Guard",),
    "starfleet": ("Starfleet Doctor",),
    "warp-drive": ("Warp-drive", "Warp-drive Starfleet diagnostics"),
    "starfleet-markup": (),
}

DEPENDENTS = {
    "opencode-adapter": {"model-preferences", "hailing-frequencies", "discord-watch", "pattern-buffer", "starfleet-policy-guard"},
    "model-preferences": {"away-mode", "replicator"},
    "hailing-frequencies": {"discord-watch", "warp-drive", "pattern-buffer", "away-mode"},
    "pattern-buffer": {"context-injector", "replicator", "starfleet-policy-guard"},
    "discord-watch": {"away-mode", "replicator"},
}

SHARED_COMPONENTS = {
    "away-mode", "context-injector", "hailing-frequencies", "discord-watch", "model-preferences",
    "pattern-buffer", "replicator", "starfleet-policy-guard",
}


def select_components(paths):
    selected = set()
    for name in paths:
        path = PurePosixPath(name)
        if path.suffix.lower() == ".md" or name == "LICENSE":
            continue
        parts = path.parts
        if len(parts) < 3 or parts[0] != "apps" or parts[1] not in COMPONENT_CHECKS:
            return set(COMPONENT_CHECKS), "shared or unclassified verification input"
        component = parts[1]
        if parts[-1] in {"opencode_adapter.py", "warp_drive.py"}:
            return set(COMPONENT_CHECKS), "shared runtime, hook or custody implementation"
        if component in SHARED_COMPONENTS and not (
            parts[-1].startswith("test_") or "tests" in parts or parts[-1] == "verify.py"
        ):
            return set(COMPONENT_CHECKS), "shared runtime, hook or custody implementation"
        selected.add(component)
    while True:
        expanded = selected | {"starfleet"} if selected else set()
        for component in selected:
            expanded.update(DEPENDENTS.get(component, ()))
        if expanded == selected:
            return selected, "affected components and dependents" if selected else "runtime checks unaffected"
        selected = expanded


def changed_paths(root, base=None):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root)

    try:
        if base is None:
            base = git("merge-base", "HEAD", "origin/main").decode().strip()
        git("rev-parse", "--verify", base + "^{commit}")
        committed = git("diff", "--name-only", "-z", "--no-renames", base, "HEAD")
        staged = git("diff", "--name-only", "-z", "--no-renames", "HEAD")
        untracked = git("ls-files", "--others", "--exclude-standard", "-z")
        return sorted({p.decode() for p in (committed + staged + untracked).split(b"\0") if p}), base
    except (subprocess.CalledProcessError, UnicodeError):
        return None, base
