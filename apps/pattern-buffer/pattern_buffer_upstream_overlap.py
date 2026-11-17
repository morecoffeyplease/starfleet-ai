"""Read-only Git comparison for Pattern Buffer's prepare-time overlap advisory."""
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Literal

STATE_LIMIT = 512 * 1024
STATE_VERSION = 1
NOTICE_OVERLAP_LIMIT = 20


@dataclass(frozen=True)
class PathChange:
    status: str
    old_path: str | None
    new_path: str | None
    source: Literal["branch", "worktree", "upstream"]
    identity: str = ""

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(path for path in (self.old_path, self.new_path) if path is not None))

    @property
    def display(self) -> str:
        if self.old_path is not None and self.new_path is not None and self.old_path != self.new_path:
            return f"{self.status} {json.dumps(self.old_path, ensure_ascii=True)} -> {json.dumps(self.new_path, ensure_ascii=True)}"
        return f"{self.status} {json.dumps(self.new_path or self.old_path or '', ensure_ascii=True)}"


@dataclass(frozen=True)
class Overlap:
    upstream_change: PathChange
    task_changes: tuple[PathChange, ...]
    shared_path: str


@dataclass(frozen=True)
class Observation:
    status: Literal["overlap", "none", "unavailable"]
    task_worktree: str
    task_head: str | None
    upstream_ref: str | None
    upstream_revision: str | None
    fork_revision: str | None
    task_changes: tuple[PathChange, ...] = ()
    upstream_changes: tuple[PathChange, ...] = ()
    overlaps: tuple[Overlap, ...] = ()
    new_overlaps: tuple[Overlap, ...] = ()
    reason: str | None = None
    dedup_error: str | None = None


class GitUnavailable(Exception):
    pass


def _git(root: Path, *args: str, allow_missing: bool = False) -> bytes | None:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=3)
    except (OSError, subprocess.SubprocessError) as error:
        raise GitUnavailable(f"Git could not inspect {args[0] if args else 'the worktree'}") from error
    if result.returncode == 0:
        return result.stdout
    if allow_missing and result.returncode == 1:
        return None
    raise GitUnavailable(f"Git {args[0] if args else 'command'} exited with code {result.returncode}")


def _revision(root: Path, ref: str, allow_missing: bool = False) -> str | None:
    output = _git(root, "rev-parse", "--verify", "--quiet", ref, allow_missing=allow_missing)
    return output.decode("ascii").strip() if output is not None else None


def _known_main_ref(root: Path) -> tuple[str, str]:
    candidates: list[str] = []
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", allow_missing=True)
    if branch:
        branch_name = branch.decode("utf-8", errors="surrogateescape").strip()
        remote_output = _git(root, "config", "--get", f"branch.{branch_name}.remote", allow_missing=True)
        merge_output = _git(root, "config", "--get", f"branch.{branch_name}.merge", allow_missing=True)
        if remote_output:
            remote = remote_output.decode("utf-8", errors="surrogateescape").strip()
            merge = merge_output.decode("utf-8", errors="surrogateescape").strip() if merge_output else ""
            if remote == "." and merge == "refs/heads/main":
                candidates.append("refs/heads/main")
            elif remote and remote != ".":
                candidates.append(f"refs/remotes/{remote}/main")
    candidates.extend(("refs/remotes/origin/main", "refs/remotes/upstream/main"))
    for ref in dict.fromkeys(candidates):
        if _git(root, "check-ref-format", ref, allow_missing=True) is None:
            continue
        revision = _revision(root, ref, allow_missing=True)
        if revision is not None:
            return ref, revision
    raise GitUnavailable("no known main ref is available in the task checkout")


def _parse_changes(raw: bytes, source: str) -> tuple[PathChange, ...]:
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    changes: list[PathChange] = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii", errors="replace")
        index += 1
        if status.startswith(("R", "C")):
            if index + 1 >= len(fields):
                raise GitUnavailable("Git returned an incomplete rename record")
            old_path = os.fsdecode(fields[index])
            new_path = os.fsdecode(fields[index + 1])
            index += 2
        else:
            if index >= len(fields):
                raise GitUnavailable("Git returned an incomplete path record")
            path = os.fsdecode(fields[index])
            index += 1
            if status.startswith("D"):
                old_path, new_path = path, None
            elif status.startswith("A"):
                old_path, new_path = None, path
            else:
                old_path, new_path = path, path
        changes.append(PathChange(status, old_path, new_path, source))
    return tuple(changes)


def _unmerged_changes(raw: bytes) -> tuple[PathChange, ...]:
    changes: dict[str, PathChange] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        _metadata, separator, path = record.partition(b"\t")
        if not separator:
            raise GitUnavailable("Git returned an incomplete unmerged path record")
        decoded = os.fsdecode(path)
        changes[decoded] = PathChange("U", decoded, decoded, "worktree")
    return tuple(changes[path] for path in sorted(changes))


def _tree_entry(root: Path, revision: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), "--literal-pathspecs", "ls-tree", "-z", revision, "--", path],
        capture_output=True,
        timeout=3,
    )
    if result.returncode:
        raise GitUnavailable(f"Git ls-tree exited with code {result.returncode}")
    return result.stdout


def _identity(root: Path, fork: str, upstream: str, change: PathChange) -> str:
    entries = []
    for path in change.paths:
        before = _tree_entry(root, fork, path)
        after = _tree_entry(root, upstream, path)
        entries.append({"path": path, "before": hashlib.sha256(before).hexdigest(),
                        "after": hashlib.sha256(after).hexdigest()})
    value = {"status": change.status, "old_path": change.old_path, "new_path": change.new_path,
             "entries": entries}
    payload = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _unavailable(task_worktree: str, task_head: str | None, upstream_ref: str | None, upstream_revision: str | None,
                 fork_revision: str | None, task_changes: tuple[PathChange, ...],
                 upstream_changes: tuple[PathChange, ...], reason: str) -> Observation:
    return Observation("unavailable", task_worktree, task_head, upstream_ref, upstream_revision, fork_revision,
                       task_changes, upstream_changes, reason=reason)


def _inspect(root: Path) -> Observation:
    task_head = upstream_ref = upstream_revision = fork_revision = None
    task_changes: tuple[PathChange, ...] = ()
    upstream_changes: tuple[PathChange, ...] = ()
    try:
        task_head = _revision(root, "HEAD")
        if task_head is None:
            raise GitUnavailable("task HEAD is unavailable")
        working_tree_raw = _git(root, "diff", "--name-status", "-z", "-M", task_head, "--") or b""
        task_changes = _parse_changes(working_tree_raw, "worktree")
        unmerged = _unmerged_changes(_git(root, "ls-files", "--unmerged", "-z") or b"")
        task_changes += unmerged
        if unmerged:
            raise GitUnavailable("task checkout has unmerged tracked paths")
        upstream_ref, upstream_revision = _known_main_ref(root)
        if upstream_revision is None:
            raise GitUnavailable("known main ref has no revision")
        merge = _git(root, "merge-base", task_head, upstream_revision)
        fork_revision = merge.decode("ascii").strip() if merge is not None else None
        if not fork_revision:
            raise GitUnavailable("task HEAD and the known main ref have no merge base")
        committed = _parse_changes(
            _git(root, "diff", "--name-status", "-z", "-M", fork_revision, task_head, "--") or b"",
            "branch",
        )
        task_changes = committed + task_changes
        upstream_changes = _parse_changes(
            _git(root, "diff", "--name-status", "-z", "-M", fork_revision, upstream_revision, "--") or b"",
            "upstream",
        )
        paths_to_task_changes: dict[str, list[PathChange]] = {}
        for change in task_changes:
            for path in change.paths:
                paths_to_task_changes.setdefault(path, []).append(change)
        overlaps = []
        for upstream_change in upstream_changes:
            shared_paths = [path for path in upstream_change.paths if paths_to_task_changes.get(path)]
            if not shared_paths:
                continue
            identified = replace(
                upstream_change,
                identity=_identity(root, fork_revision, upstream_revision, upstream_change),
            )
            for shared_path in shared_paths:
                overlaps.append(Overlap(identified, tuple(paths_to_task_changes[shared_path]), shared_path))
        current_head = _revision(root, "HEAD")
        current_upstream = _revision(root, upstream_ref)
        current_worktree = _git(root, "diff", "--name-status", "-z", "-M", task_head, "--") or b""
        if current_head != task_head:
            raise GitUnavailable(f"task HEAD moved from {task_head} to {current_head} during inspection")
        if current_upstream != upstream_revision:
            raise GitUnavailable(f"{upstream_ref} moved from {upstream_revision} to {current_upstream} during inspection")
        if current_worktree != working_tree_raw:
            raise GitUnavailable("tracked task worktree changes moved during inspection")
        if not overlaps:
            return Observation("none", str(root), task_head, upstream_ref, upstream_revision, fork_revision,
                               task_changes, upstream_changes)
        return Observation("overlap", str(root), task_head, upstream_ref, upstream_revision, fork_revision,
                           task_changes, upstream_changes, tuple(overlaps))
    except Exception as error:
        return _unavailable(str(root), task_head, upstream_ref, upstream_revision, fork_revision,
                            task_changes, upstream_changes, str(error))


def _dedup_key(overlap: Overlap) -> str:
    change = overlap.upstream_change
    return json.dumps([change.status, change.old_path, change.new_path, overlap.shared_path],
                      ensure_ascii=True, separators=(",", ":"))


def _deduplicate(observation: Observation, gitdir: Path, locked, atomic) -> Observation:
    state_path = gitdir / "context-injector" / "upstream-overlap.json"
    lock_path = state_path.with_suffix(".lock")
    try:
        state_dir = state_path.parent
        if state_dir.is_symlink() or state_path.is_symlink() or lock_path.is_symlink():
            raise ValueError("private overlap state contains a symlink")
        with locked(lock_path):
            if state_dir.is_symlink() or state_path.is_symlink():
                raise ValueError("private overlap state changed to a symlink")
            if state_path.exists():
                if not state_path.is_file() or state_path.stat().st_size > STATE_LIMIT:
                    raise ValueError("private overlap state is not a bounded regular file")
                raw = state_path.read_text(encoding="utf-8")
                state = json.loads(raw)
                if (not isinstance(state, dict) or state.get("version") != STATE_VERSION
                        or not isinstance(state.get("seen"), dict)
                        or any(not isinstance(key, str) or not isinstance(value, str)
                               for key, value in state["seen"].items())):
                    raise ValueError("private overlap state has an unsupported shape")
                seen = dict(state["seen"])
            else:
                seen = {}
            new_overlaps = []
            for overlap in observation.overlaps:
                key = _dedup_key(overlap)
                identity = overlap.upstream_change.identity
                if seen.get(key) != identity:
                    new_overlaps.append(overlap)
                    seen[key] = identity
            if new_overlaps:
                encoded = json.dumps({"version": STATE_VERSION, "seen": seen}, sort_keys=True,
                                     ensure_ascii=True, separators=(",", ":")) + "\n"
                if len(encoded.encode("utf-8")) > STATE_LIMIT:
                    raise ValueError("private overlap state reached its size limit")
                atomic(state_path, encoded)
            return replace(observation, new_overlaps=tuple(new_overlaps))
    except Exception as error:
        return replace(observation, new_overlaps=observation.overlaps,
                       dedup_error=str(error))


def observe(root: Path, gitdir: Path, locked, atomic) -> Observation:
    observation = _inspect(Path(root).resolve())
    if observation.status != "overlap":
        return observation
    return _deduplicate(observation, Path(gitdir).resolve(), locked, atomic)


def _change_list(changes: tuple[PathChange, ...]) -> str:
    if not changes:
        return "none"
    selected = changes[:NOTICE_OVERLAP_LIMIT]
    rendered = "; ".join(change.display for change in selected)
    remainder = len(changes) - len(selected)
    return rendered + (f"; and {remainder} more" if remainder else "")


def format_notice(observation: Observation) -> str | None:
    if observation.status == "none":
        return None
    if observation.status == "unavailable":
        fields = [f"task worktree {observation.task_worktree}",
                  f"task HEAD {observation.task_head or 'unavailable'}"]
        if observation.upstream_ref:
            fields.append(f"upstream ref {observation.upstream_ref} at {observation.upstream_revision or 'unavailable'}")
        if observation.fork_revision:
            fields.append(f"fork {observation.fork_revision}")
        paths = []
        if observation.task_changes:
            paths.append("observed task paths: " + _change_list(observation.task_changes))
        if observation.upstream_changes:
            paths.append("upstream paths: " + _change_list(observation.upstream_changes))
        details = "; ".join([*fields, *paths])
        return (f"Pattern Buffer upstream overlap check unavailable: {observation.reason or 'Git metadata could not be read'}. "
                f"Examined {details}. No overlap conclusion is available. Task preparation continues.")
    if observation.status != "overlap" or not observation.new_overlaps:
        return None
    overlaps = observation.new_overlaps[:NOTICE_OVERLAP_LIMIT]
    lines = [f"Pattern Buffer upstream overlap advisory: inspected task worktree {observation.task_worktree}, "
             f"{observation.upstream_ref} at "
             f"{observation.upstream_revision}, fork {observation.fork_revision}, task HEAD {observation.task_head}."]
    for overlap in overlaps:
        task = "; ".join(change.display for change in overlap.task_changes)
        upstream = overlap.upstream_change.display
        lines.append(f"  Shared path {json.dumps(overlap.shared_path, ensure_ascii=True)}: task {task}; upstream {upstream}.")
    extra = len(observation.new_overlaps) - len(overlaps)
    if extra:
        lines.append(f"  {extra} additional new overlapping path changes are omitted from this notice.")
    if observation.dedup_error:
        lines.append(f"  Deduplication state was unavailable: {observation.dedup_error}. A later preparation may repeat this notice.")
    if observation.new_overlaps:
        displayed = observation.new_overlaps[:NOTICE_OVERLAP_LIMIT]
        paths = sorted({path for overlap in displayed for path in overlap.upstream_change.paths})
        quoted_paths = " ".join(shlex.quote(path) for path in paths)
        lines.append(f"  Compare with: git -C {shlex.quote(observation.task_worktree)} diff --find-renames {observation.fork_revision} "
                     f"{observation.upstream_revision} -- {quoted_paths}.")
    lines.append("  This reports file-path overlap only and does not establish semantic impact or current product behavior.")
    return "\n".join(lines)
