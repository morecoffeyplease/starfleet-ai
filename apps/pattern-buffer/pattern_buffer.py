#!/usr/bin/env python3
"""Pattern Buffer restores approved worktree context for Claude, Codex, and OpenCode."""
import argparse
import datetime
from dataclasses import asdict, dataclass
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from urllib.parse import quote, unquote, urlsplit

ROLES = ("implementer", "orchestrator")
HARNESS_CHOICES = ("claude", "codex", "opencode")
_BINDING_UNSET = object()
LIMIT = 8192
FILE_LIMIT = 32768
HOOK_INPUT_LIMIT = 1024 * 1024
TASK_LIMIT = 16384
INJECTION_LIMIT = 24576
RECOVERY_OUTPUT_LIMIT = 9 * 1024
DISPATCH_ENV = "STARFLEET_CONTEXT_DISPATCH"
REPO_PROFILE_LIMIT = 32768
REFERENCE_FILE_LIMIT = 256 * 1024
REFERENCE_TOTAL_LIMIT = 1024 * 1024
REFERENCE_COUNT_LIMIT = 128
_OPENCODE_SESSION_ID = re.compile(r"^ses_[A-Za-z0-9]+$")
STRICT_RENDER_POLICY = "strict"
ORDINARY_WORKTREE_RENDER_POLICY = "ordinary_worktree"
_RENDER_FAILURES = {
    "combined_limit": "The composed output exceeds the existing 24 KiB injection limit.",
    "file_limit": "The context source exceeds the 32 KiB file limit.",
    "malformed": "The context source could not be parsed.",
    "missing_role_section": "The context source has no section for the assigned role.",
    "other": "The context could not be rendered safely.",
    "runtime_output_limit": "The complete output, including its warning and runtime or checkpoint addenda, exceeds the 9 KiB recovery limit.",
    "standing_limit": "The rendered role view exceeds the 8192-byte standing-context budget.",
    "unreadable": "The context source could not be read.",
}


class DispatchFailure(ValueError):
    """A known dispatched session must not silently lose its recovery context."""


class RequiredContextFailure(ValueError):
    """A session enrolled for isolated context must never inherit the default."""

    def __init__(self, message, path, enrollment_id=None):
        super().__init__(message)
        self.path = Path(path)
        self.enrollment_id = enrollment_id


@dataclass(frozen=True)
class RenderedContext:
    text: str
    source: Path
    runtime_policy: str
    over_budget_bytes: int = 0
    failure_kind: str | None = None


_AUXILIARY = {}
_RUNTIME_FINGERPRINT = None


def auxiliary_source(name, script=None):
    script = Path(script or __file__).resolve()
    return script.with_name(("pattern_buffer" if script.name == "pattern_buffer.py" else script.stem)
                            + "_" + name + ".py")


def auxiliary(name):
    if name not in _AUXILIARY:
        path = auxiliary_source(name)
        spec = importlib.util.spec_from_file_location("starfleet_pb_" + name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _AUXILIARY[name] = module
    return _AUXILIARY[name]


def session_binding(root, gitdir, harness, session, agent=""):
    if agent or not session:
        return None
    if opencode_task_assignment_precedes_enrollment(gitdir, harness, session):
        return None
    return auxiliary("sessions").lookup(root, gitdir, harness, session)


def session_binding_readonly(root, gitdir, harness, session, agent=""):
    """Read an enrollment without creating a lock, after honoring OpenCode task precedence."""
    if agent or not session or opencode_task_assignment_precedes_enrollment(gitdir, harness, session):
        return None
    return auxiliary("sessions").lookup_readonly(root, gitdir, harness, session)


def opencode_task_assignment_precedes_enrollment(gitdir, harness, session):
    if harness != "opencode" or not session:
        return False
    task_assignment = state_path(gitdir, harness, session)
    if task_assignment.is_symlink():
        return True
    if not task_assignment.exists():
        return False
    try:
        saved = json.loads(bounded(task_assignment, FILE_LIMIT))
    except (OSError, ValueError, UnicodeError, TypeError):
        return True
    return not isinstance(saved, dict) or bool(saved.get("task_id"))


def canonical_native_uuid(value, label):
    if not isinstance(value, str) or re.fullmatch(
            r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", value) is None:
        raise ValueError(f"{label} must be the exact CMUX UUID")
    return str(uuid.UUID(value)).upper()


def validate_parent_task_id(value):
    if not isinstance(value, str) or not value or len(value) > 256 or value.strip() != value:
        raise ValueError("OpenCode parent task ID must be 1-256 non-padded characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("OpenCode parent task ID is not valid UTF-8") from None
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("OpenCode parent task ID must not contain control characters")
    return value


def validate_opencode_parent_record(parent, owner):
    fields = {"harness", "session_id", "task_id", "role", "owner_worktree", "owner_gitdir",
              "surface_id", "workspace_id", "assignment_sha256"}
    if not isinstance(parent, dict) or set(parent) != fields:
        raise ValueError("OpenCode parent identity record is incomplete or ambiguous")
    if (not isinstance(owner, dict) or set(owner) != {"root", "gitdir"} or
            not isinstance(owner.get("root"), str) or not isinstance(owner.get("gitdir"), str)):
        raise ValueError("OpenCode task owner identity is invalid")
    if (parent.get("harness") != "opencode" or
            not isinstance(parent.get("session_id"), str) or
            not _OPENCODE_SESSION_ID.fullmatch(parent["session_id"]) or
            parent.get("role") != "orchestrator" or
            parent.get("owner_worktree") != owner.get("root") or
            parent.get("owner_gitdir") != owner.get("gitdir")):
        raise ValueError("OpenCode parent identity is foreign or not an orchestrator")
    validate_parent_task_id(parent.get("task_id"))
    for key in ("surface_id", "workspace_id"):
        if canonical_native_uuid(parent.get(key), "OpenCode parent " + key) != parent[key]:
            raise ValueError("OpenCode parent CMUX identity is not canonical")
    digest = parent.get("assignment_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("OpenCode parent assignment proof is invalid")
    return parent


def opencode_parent_assignment(owner_root, owner_gitdir, session, task_id, surface, workspace,
                               expected=None):
    owner = {"root": str(owner_root), "gitdir": str(owner_gitdir)}
    task_id = validate_parent_task_id(task_id)
    if not isinstance(session, str) or not _OPENCODE_SESSION_ID.fullmatch(session):
        raise ValueError("OpenCode parent must be the exact case-sensitive ses_ session ID")
    surface = canonical_native_uuid(surface, "OpenCode parent CMUX_SURFACE_ID")
    workspace = canonical_native_uuid(workspace, "OpenCode parent CMUX_WORKSPACE_ID")
    owner_config, source = load(owner_root, owner_gitdir)
    if owner_config.get("context_owner") or not enabled(owner_config):
        raise ValueError("OpenCode parent owner must be directly initialized and enabled")
    metadata_dir = owner_gitdir / "context-injector"
    assignment_dir = metadata_dir / "sessions"
    if metadata_dir.is_symlink() or assignment_dir.is_symlink():
        raise ValueError("OpenCode parent assignment path must not contain a symlink")
    if ((metadata_dir.exists() and not metadata_dir.is_dir()) or
            (assignment_dir.exists() and not assignment_dir.is_dir())):
        raise ValueError("OpenCode parent assignment path is ambiguous")
    assignment = state_path(owner_gitdir, "opencode", session)
    if assignment.is_symlink() or not assignment.is_file():
        raise ValueError("exact OpenCode parent has no session assignment in the selected owner worktree")
    raw = bounded(assignment, FILE_LIMIT)
    try:
        saved = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("OpenCode parent session assignment is malformed") from None
    if (not isinstance(saved, dict) or set(saved) != {"root", "role"} or
            saved.get("root") != str(owner_root) or saved.get("role") != "orchestrator"):
        raise ValueError("exact OpenCode parent assignment is foreign, task-bound or not an orchestrator")
    role, origin, resolved = resolve_role(owner_root, owner_gitdir, owner_config,
                                          "opencode", session)
    if role != "orchestrator" or origin != "session assignment" or resolved != saved:
        raise ValueError("OpenCode parent role is missing, ambiguous or changed")
    content = sections(source)
    if "orchestrator" not in content or "implementer" not in content:
        raise ValueError("OpenCode parent owner context is missing an orchestrator or implementer section")
    parent = {"harness": "opencode", "session_id": session, "task_id": task_id,
              "role": "orchestrator", "owner_worktree": str(owner_root),
              "owner_gitdir": str(owner_gitdir), "surface_id": surface,
              "workspace_id": workspace,
              "assignment_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}
    validate_opencode_parent_record(parent, owner)
    if expected is not None and parent != validate_opencode_parent_record(expected, owner):
        raise ValueError("OpenCode parent assignment or native identity changed after preparation")
    return parent


def opencode_parent_from_environment(owner_root, owner_gitdir, task_id):
    if os.environ.get("CMUX_AGENT_LAUNCH_KIND") != "opencode":
        raise ValueError("OpenCode task preparation requires CMUX_AGENT_LAUNCH_KIND=opencode")
    session = os.environ.get("OPENCODE_SESSION_ID")
    surface = os.environ.get("CMUX_SURFACE_ID")
    workspace = os.environ.get("CMUX_WORKSPACE_ID")
    if not session:
        raise ValueError("OpenCode task preparation requires the exact OPENCODE_SESSION_ID")
    if not surface or not workspace:
        raise ValueError("OpenCode task preparation requires exact CMUX_SURFACE_ID and CMUX_WORKSPACE_ID")
    return opencode_parent_assignment(owner_root, owner_gitdir, session, task_id, surface, workspace)


def record_event(root, event, harness=None, session=None, enrollment_id=None,
                 result=None, correlation_id=None, details=None):
    """Observability must not suppress an otherwise valid recovery."""
    try:
        global _RUNTIME_FINGERPRINT
        if _RUNTIME_FINGERPRINT is None:
            _RUNTIME_FINGERPRINT = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        details = {**(details or {}), "runtime_fingerprint": _RUNTIME_FINGERPRINT}
        report = auxiliary("reliability").emit(
            root, event, harness=harness, session=session, enrollment_id=enrollment_id,
            result=result, correlation_id=correlation_id, details=details)
        if isinstance(report, dict) and report.get("error"):
            print("pattern-buffer telemetry: " + str(report["error"]), file=sys.stderr)
    except Exception as error:
        print("pattern-buffer telemetry unavailable: " + type(error).__name__, file=sys.stderr)


def idle_receipt_report(root, since):
    """Read only this worktree's existing Beacon receipts without copying continuation bodies."""
    path = Path.home() / ".local/state/starfleet-ai/hailing-frequencies/receipts.sqlite3"
    if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
        return {"state": "unknown", "reason": "Hailing receipt history is unavailable", "receipts": []}
    try:
        start = datetime.datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
        with sqlite3.connect("file:" + quote(str(path)) + "?mode=ro", uri=True, timeout=1) as database:
            rows = database.execute(
                "SELECT o.id,o.timer_id,o.status,o.attempted_at,o.received_at,t.harness,t.session "
                "FROM timer_occurrences o JOIN timers t ON t.id=o.timer_id "
                "WHERE t.canonical_worktree=? AND t.replacement_key=? "
                "AND (o.attempted_at>=? OR o.received_at>=?) ORDER BY o.due_at DESC LIMIT 1001",
                (str(root), "pattern-buffer-checkpoint-idle", start, start)).fetchall()
        counts = {}
        receipts = []
        for row in rows[:1000]:
            counts[row[2]] = counts.get(row[2], 0) + 1
            receipts.append(dict(zip(("occurrence_id", "timer_id", "status", "attempted_at", "received_at",
                                      "harness", "session"), row)))
        return {"state": "observed", "counts": counts, "truncated": len(rows) > 1000,
                "receipts": receipts,
                "receipt_note": "An explicit exact-owner CLI receipt is not model comprehension or task completion."}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return {"state": "unknown", "reason": "Hailing receipt history could not be read", "receipts": []}


RESTATEMENT = (
    "After the required reads, give four lines in your own chat. State the accepted outcome, "
    "the next deliverable, the holds and any unavailable reads, and what you will not do."
)


TEMPLATE = """# shared
- Worktree: <WORKTREE>
- Mission: <MISSION>
- Next deliverable: <NEXT_DELIVERABLE>
- Holds: <HOLDS>
- Roadmap: <TRACKER>
- Assigned items: <AUTHORIZED_ITEM_REFERENCES>
- Read first: <3-5 exact paths or commands>

{RESTATEMENT}
System and developer instructions take precedence. Follow the latest human instruction, then the standing mission, tracker, checkpoint, and memory or summary. Reconcile conflicts and preserve ownership and holds.

**Scope**
- If the mission, authoritative tracker or authorized assignment is missing or unclear, ask the user to establish it.
- Never invent values, infer new scope or treat placeholders as approval.
- If no tracker or assignment applies, record the user's explicit agreement.
- Stay within the user's authorized scope.

**PStack guidance - do this now**
- Use the installed PStack runtime and Poteto Mode.
- After compaction, read the sections relevant to current work in full.
- Do not assume you know what this guidance says.

**Agents**
- Before launching any agent, read the saved active profile in Model Preferences and resolve its configured reviewers.
- A pilot preference or old startup summary doesn't prove the active selection or reviewer count.
- Launch only configured reviewer slots.
- Advanced-review permission doesn't authorize an extra reviewer, another provider or a shared profile switch.

**Agent communication** (concise but accurate)
- Lead with what changed and any action needed.
- Batch nonurgent updates unless that delays dependent work.
- Reply only with answers, needed questions, decisions or meaningful updates; otherwise use the supported receipt ack (means received, not completed).
- Link accessible evidence; don't repeat detailed logs.
- Send blockers, approval requests and key handoffs promptly.

**Test failures and claims about systems**
- Every explanation is a hypothesis until it survives an attempt to disprove it.
- Before you name a cause or a fix option, or state what CI, a config or a tool does, write down at least one competing explanation and the observation that would tell them apart.
- Check that observation on the failing run AND a passing control: the base branch, main, or an earlier green run of the same check.
- Use evidence already on disk first: result files, timestamps, receipts, logs, and the config and source themselves.
- Ask for a new run only when disk cannot answer.
- Look for the check that could prove you wrong, not the one that agrees.
- A mechanism that fits is not a cause.
- Agreement is not evidence: not from another agent, a reviewer, a coordinating tool or service, or your own earlier message.
- When config says one thing and behavior shows another, that contradiction is the finding.
- Report it; do not explain it away.
- Facts about tools and CI come from reading them now, not from memory.
- Never offer a fix option built on an unverified cause.
- Do not implement or dispatch a fix based on an unverified cause; diagnostic investigation is allowed.
- When reporting, separate what you observed from what you concluded, and mark every unchecked claim "unverified".
- When you find you were wrong, say so first and plainly, then correct it.

**Review judgment: protect delivery**
- Review exists to deliver the approved outcome safely, not to eliminate every imaginable defect; repeated repairs can create more risk and delay than the finding warrants.
- Before a repair, choose fix here, defer, or dismiss using evidence, user impact, likelihood and the PR's purpose; bot and security labels do not decide for you.
- Fix here only within existing authority for a material PR regression, an unmet approved outcome, or a high-impact defect on a common path.
- Ask the user before fixing a serious-but-rare security, data-loss, or safety-contract exception.
- Do not turn uncertainty into a blocker or call it disproved; investigate the cheapest decisive evidence, then defer unresolved claims explicitly.
- Workers report new findings rather than expanding the batch.
- If uncertainty remains after checking the deciding evidence, ask the user rather than guess or start a repair.
- Before starting or resuming a review-and-fix cycle, read the [Prime Directive (Policy Guard) review and fix cycles guide](https://github.com/morecoffeyplease/starfleet-ai/blob/main/apps/starfleet-policy-guard/guide.md#review-and-fix-cycles) in full; Prime Directive and Policy Guard are names for the same tool.
- Revisit the relevant rule before each repair decision.

# implementer
- Ask your parent orchestrator for more agents (incl. research and review) unless this task explicitly delegates that authority; delegation still follows current Model Preferences role choices, reviewer count and advanced-review permission.
- Reproduce reported bugs before editing code.
- Verify completed changes through the user's path.

# orchestrator
**Before resuming, do this now:**
1. Re-read the mission, roadmap/tracker and current PRs’ issues IN FULL; reconcile actual checkout, PR heads/states, active work, evidence and human holds, don’t rely on assumptions; then update only this lane's tracker sections where facts changed.
   Re-pull before editing; coordinate with other tracker owners.
2. Ask now: "Are we fanned out across as many temporary worktrees as would help finish ready, independent work sooner, respecting dependencies, clear ownership and shared resource limits?”

**PR openings and accepted repair batches:**
- At PR opening or accepted repair-batch start, read Published guidance attestations in `python3 /path/to/installed/lane.py guide --print`. Adopted ready and merge actions require current evidence.
- The owner rereads the applicable files, records references, digests and their effect, resolves prerequisites, and keeps `assumptions: []`. Prepare opening evidence before the PR and publish the exact comment on its draft. Run `attestation-start-check` after publication and before repair work. Use `--enable` for first adoption. Use Outcome, Playbook, Size and Facts in each implementer brief and the PR opening explanation.

**Repair rounds: don't spiral**
- Use the existing PR or tracker to record dispositions, evidence, and cumulative rounds.
- From round three, make a fresh zoom-out before accepting or dispatching the next repair batch.
- Stop a repair chain immediately when its fix creates another regression in that area; prefer reverting an isolatable unsuccessful repair instead of patching the patch.
- Do not blindly revert mixed changes or restore a serious vulnerability.
- After two unsuccessful attempts on the same underlying finding, attack the premise and ask the user before a third attempt; a renamed symptom, worker, or handoff does not reset the count.
- Before round four, or the next unapproved round if already beyond three, obtain approval for a bounded next batch or allowance.
- State the purpose, remaining blocker, attempted fixes, added complexity, and smallest safe route.
- A qualifying fix round is an accepted product-repair batch, not a commit, worker launch, or unchanged rerun.
- Preserve stronger holds, read the cycle guide for counting and exceptions, and continue independent authorized work while waiting.

**Takeover:** reconcile inherited PR identities, fix counts, round-specific zoom-out evidence and human holds before more product fixes; write a fresh assessment for the next repair round; confirm mission and roadmap still apply; verify successor tool bindings and actual context delivery before dispatching work.

**Always**
- Preserve unknowns and missing evidence as gaps; don't rewrite unchanged status or infer new authority.
- Track ownership, dependencies and completion evidence.
  Delegated ≠ completed.

**Pattern Buffer checkpoints:** if enrolled, maintain the registered private note after meaningful changes; read/reconcile it on recovery. Path, Git hygiene and controls: apps/pattern-buffer/docs/checkpoints.md

# confirmation
Restate the four lines above.
"""
TEMPLATE = TEMPLATE.replace("{RESTATEMENT}", RESTATEMENT)


def git(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                            text=True, timeout=3)
    if result.returncode:
        raise ValueError("working directory is not a Git worktree")
    return result.stdout.strip()


def checkout(cwd):
    root = Path(git(cwd, "rev-parse", "--show-toplevel")).resolve()
    gitdir = Path(git(root, "rev-parse", "--absolute-git-dir")).resolve()
    return root, gitdir


def bounded(path, limit):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular, non-symlink file: {path}")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"file exceeds {limit} bytes: {path}")
    return data.decode("utf-8")


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        if path.exists():
            os.fchmod(fd, path.stat().st_mode & 0o777)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load(root, gitdir):
    folder = root / ".context-injector"
    if folder.is_symlink():
        raise ValueError("context directory must not be a symlink")
    config = json.loads(bounded(gitdir / "context-injector" / "config.json", FILE_LIMIT))
    if not isinstance(config, dict) or config.get("version") != 1 or config.get("root") != str(root):
        raise ValueError("checkout registration changed; initialize this worktree again")
    if config.get("default_role") not in (None, *ROLES):
        raise ValueError("invalid default role")
    profile = config.get("repo_profile")
    if profile is not None and not isinstance(profile, (str, dict)):
        raise ValueError("invalid repository profile registration")
    if not isinstance(config.get("enabled", True), bool):
        raise ValueError("invalid enabled state")
    owner = config.get("context_owner")
    if owner is not None:
        owner_root, owner_gitdir = validate_owner(root, owner)
        owner_config = json.loads(bounded(owner_gitdir / "context-injector/config.json", FILE_LIMIT))
        if not isinstance(owner_config, dict) or owner_config.get("context_owner"):
            raise ValueError("context owners must be directly initialized worktrees")
        load(owner_root, owner_gitdir)
        return config, owner_root / ".context-injector/context.md"
    return config, folder / "context.md"


def validate_owner(root, owner):
    if not isinstance(owner, dict) or not isinstance(owner.get("root"), str):
        raise ValueError("invalid context owner")
    owner_root, owner_gitdir = checkout(owner["root"])
    if str(owner_gitdir) != owner.get("gitdir") or owner_root == root:
        raise ValueError("context owner identity changed")
    if git(root, "rev-parse", "--path-format=absolute", "--git-common-dir") != git(
            owner_root, "rev-parse", "--path-format=absolute", "--git-common-dir"):
        raise ValueError("context owner must belong to the same Git repository")
    return owner_root, owner_gitdir


def enabled(config):
    if not config.get("enabled", True):
        return False
    if config.get("context_owner"):
        owner_root, owner_gitdir = checkout(config["context_owner"]["root"])
        owner_config, _ = load(owner_root, owner_gitdir)
        return owner_config.get("enabled", True)
    return True


def resolved_repo_profile(root, config):
    owner = config.get("context_owner")
    if owner is None:
        return profile_name(config.get("repo_profile"))
    owner_root, owner_gitdir = validate_owner(root, owner)
    owner_config, _ = load(owner_root, owner_gitdir)
    return profile_name(owner_config.get("repo_profile"))


def profile_name(profile):
    if isinstance(profile, dict):
        return profile.get("name")
    return "legacy profile" if isinstance(profile, str) else None


def compose_template(fragment=None):
    if fragment is None:
        return TEMPLATE
    if not isinstance(fragment, str) or not fragment.strip():
        raise ValueError("repository profile fragment must be nonempty text")
    marker = "\n# implementer\n"
    if TEMPLATE.count(marker) != 1:
        raise ValueError("default template cannot accept a repository profile")
    return TEMPLATE.replace(marker, "\n" + fragment.rstrip() + marker, 1)


def origin_parts(url):
    if not isinstance(url, str) or not url or "\\n" in url or "\\r" in url:
        raise ValueError("repository profile origin must be one HTTPS or SSH URL")
    if url.casefold().startswith("git@"):
        address, separator, path = url.partition(":")
        username, at, host = address.partition("@")
        if not separator or not at or username.casefold() != "git" or not host:
            raise ValueError("repository profile origin must use HTTPS or git SSH")
        if "?" in path or "#" in path:
            raise ValueError("repository profile origin cannot include a query or fragment")
    else:
        try:
            parts = urlsplit(url)
            if (parts.scheme == "https" and parts.hostname and parts.username is None
                    and parts.password is None and parts.port in (None, 443)):
                pass
            elif (parts.scheme == "ssh" and parts.hostname and parts.username == "git"
                  and parts.password is None and parts.port in (None, 22)):
                pass
            else:
                raise ValueError("repository profile origin must use HTTPS or git SSH")
            if parts.query or parts.fragment:
                raise ValueError("repository profile origin cannot include a query or fragment")
            host, path = parts.hostname, parts.path
        except ValueError as error:
            raise ValueError("invalid repository profile origin URL") from error
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path or any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError("repository profile origin must identify a repository")
    return host.casefold(), path.casefold()


def _origin_matches(actual, expected):
    try:
        return origin_parts(actual) == origin_parts(expected)
    except ValueError:
        return False


def read_repo_profile(path):
    profile_path = Path(path).expanduser().absolute()
    if profile_path.is_symlink() or not profile_path.is_file():
        raise ValueError("repository profile must be a regular, non-symlink JSON file")
    raw = bounded(profile_path, REPO_PROFILE_LIMIT).encode("utf-8")
    try:
        profile = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("repository profile must contain valid UTF-8 JSON") from error
    if (not isinstance(profile, dict) or profile.get("schema_version") != 1
            or set(profile) != {"schema_version", "name", "origin", "fragment"}
            or not isinstance(profile.get("name"), str) or not profile["name"].strip()
            or not isinstance(profile.get("fragment"), str) or not profile["fragment"].strip()):
        raise ValueError("repository profile needs schema_version 1, name, origin and nonempty fragment fields")
    origin_parts(profile["origin"])
    return profile, {"name": profile["name"], "path": str(profile_path),
                     "sha256": hashlib.sha256(raw).hexdigest(), "origin": profile["origin"]}


def verify_repo_profile_origin(root, profile):
    try:
        result = subprocess.run(["git", "-C", str(root), "config", "--get-all", "remote.origin.url"],
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("could not verify the repository profile's origin remote") from error
    urls = result.stdout.splitlines()
    if result.returncode or len(urls) != 1 or not _origin_matches(urls[0].strip(), profile["origin"]):
        raise ValueError(f"repository profile {profile['name']!r} is bound to {profile['origin']!r}; origin must match exactly")


def load_registered_profile(root, descriptor):
    if isinstance(descriptor, str):
        return None
    if not isinstance(descriptor, dict):
        return None
    profile, current = read_repo_profile(descriptor.get("path", ""))
    if current != descriptor:
        raise ValueError("repository profile changed since registration; review and initialize it again")
    verify_repo_profile_origin(root, profile)
    return profile


@contextmanager
def locked(path):
    """Serialize only CLI mutations, including read/modify/write cycles."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as handle:
        deadline = time.monotonic() + 3
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError(f"another Pattern Buffer command holds {path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def locked_directory(path):
    """Serialize installs that share a runtime, even across harness settings files."""
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"runtime directory changed or is unsafe: {path}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise ValueError(f"runtime path must be a directory: {path}")

        def validate_path_identity():
            try:
                current = os.stat(path, follow_symlinks=False)
            except OSError as error:
                raise ValueError(f"runtime directory changed or is unsafe: {path}") from error
            if (not stat.S_ISDIR(current.st_mode)
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                raise ValueError(f"runtime directory changed or is unsafe: {path}")

        validate_path_identity()
        deadline = time.monotonic() + 3
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError(f"another Pattern Buffer install holds runtime directory {path}")
                time.sleep(0.05)
        try:
            validate_path_identity()
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def settings_path(harness, explicit=None):
    if explicit:
        return Path(explicit).expanduser().absolute()
    if harness == "opencode":
        raise ValueError("OpenCode settings must be passed explicitly with --settings")
    if harness == "codex":
        return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "hooks.json"
    return Path.home() / ".claude" / "settings.json"


RECOVERY_GUIDANCE_PREFIX = "Preserve the existing runtime and enrollment/checkpoint files. "
RECOVERY_COMMAND_MARKER = "Same-settings recovery command: "


def recovery_runtime_directory():
    data_root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")).expanduser().absolute()
    runtime = data_root / "starfleet-ai" / f"context-injector-recovery-{uuid.uuid4().hex[:10]}"
    while runtime.exists() or runtime.is_symlink():
        runtime = data_root / "starfleet-ai" / f"context-injector-recovery-{uuid.uuid4().hex[:10]}"
    return runtime


def recovery_install_command(harness, explicit_settings, checkpoints=True):
    settings = settings_path(harness, explicit_settings).expanduser().absolute()
    candidate = recovery_runtime_directory()
    command = [sys.executable, str(Path(__file__).resolve()), "install", "--harness", harness,
               "--settings", str(settings), "--runtime-dir", str(candidate)]
    if not checkpoints:
        command.append("--no-checkpoints")
    return candidate, shlex.join(command)


def recovery_guidance(harness, explicit_settings, checkpoints=True):
    runtime, command = recovery_install_command(harness, explicit_settings, checkpoints)
    launcher = shlex.quote(str(runtime / "pattern-buffer"))
    return (RECOVERY_GUIDANCE_PREFIX + "Install this harness into a new empty runtime with the same settings file. "
            "Any existing PATH launcher remains unchanged; use " + launcher +
            " for commands against the recovered runtime. Then review its normal reload and trust controls; "
            "delivery remains unverified. "
            + RECOVERY_COMMAND_MARKER + command)


def marker(harness):
    return f" # starfleet-context-injector:{harness}:v1"


def checkpoint_source(script=None):
    script = Path(script or __file__).resolve()
    return script.with_name("pattern_buffer_checkpoint.py" if script.name == "pattern_buffer.py"
                            else script.stem + "_checkpoint.py")


def guide_source(script=None):
    return Path(script or __file__).resolve().with_name("guide.md")


def checkpoints():
    path = checkpoint_source()
    spec = importlib.util.spec_from_file_location("starfleet_checkpoint", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def enrollment_source(script=None):
    script = Path(script or __file__).resolve()
    return script.with_name("pattern_buffer_enrollment.py" if script.name == "pattern_buffer.py"
                            else script.stem + "_enrollment.py")


def self_enroll(root, gitdir, args):
    spec = importlib.util.spec_from_file_location("starfleet_enrollment", enrollment_source())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.enroll(sys.modules[__name__], checkpoints(), root, gitdir, args)


def hook_config(harness, script=None, with_checkpoints=True, opencode_plugin=None):
    if harness == "opencode":
        return {"plugins": [str(Path(opencode_plugin or opencode_plugin_directory()).resolve())]}
    command_args = [sys.executable, str(script or Path(__file__).resolve()),
                    "hook", "--harness", harness]
    command = shlex.join(command_args) + marker(harness)
    handler = {"type": "command", "command": command, "timeout": 5}
    if harness == "codex":
        # Full output is strictly bounded in render, including the exact task brief.
        handler["additionalContextLimit"] = 0
    result = {"hooks": {"SessionStart": [{
        "matcher": "startup|resume|compact|fork|clear", "hooks": [handler]}]}}
    result["hooks"]["PreToolUse"] = [{"hooks": [dict(handler)]}]
    if with_checkpoints:
        for event in ("PreCompact", "PostToolUse", "UserPromptSubmit"):
            extra_handler = dict(handler)
            if event == "PreCompact":
                extra_handler.pop("additionalContextLimit", None)
            result["hooks"][event] = [{"hooks": [extra_handler]}]
    else:
        read_handler = dict(handler)
        read_handler["command"] = shlex.join([*command_args, "--required-reads-only"]) + marker(harness)
        result["hooks"]["PostToolUse"] = [{"hooks": [read_handler]}]
    return result


def opencode_runtime_directory(runtime_dir=None):
    base = (Path(runtime_dir).expanduser().absolute() if runtime_dir else
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
            / "starfleet-ai" / "context-injector")
    return base


def opencode_plugin_directory(runtime_dir=None):
    runtime = opencode_runtime_directory(runtime_dir) if runtime_dir else Path(__file__).resolve().parent
    return runtime / "opencode-plugin"

RUNTIME_HELPERS = ("checkpoint", "enrollment", "sessions", "reliability", "required_reads")


def runtime_bundle_paths(script):
    script = Path(script)
    return [script, *(auxiliary_source(name, script) for name in RUNTIME_HELPERS),
            enrollment_source(script), checkpoint_source(script)]


def existing_owned_aliases(base, harness):
    """Return only a verified supported alias bundle in this runtime."""
    aliases = [base / f"context_injector_{harness}.py", base / f"pattern_buffer_{harness}.py"]
    present = [path.exists() or path.is_symlink() for path in aliases]
    if not any(present):
        return []
    if not all(present):
        if present == [True, False]:
            return existing_owned_legacy_singleton(base, harness, aliases[0])
        raise ValueError(f"refusing to update an incomplete {harness} runtime alias bundle")
    first, second = (owned_runtime_core(path, harness) for path in aliases)
    if first != second:
        raise ValueError(f"refusing to update unverified {harness} runtime aliases")

    for name in RUNTIME_HELPERS:
        paired = [base / f"{prefix}_{harness}_{name}.py"
                  for prefix in ("context_injector", "pattern_buffer")]
        pair_present = [path.exists() or path.is_symlink() for path in paired]
        if any(path.is_symlink() for path in paired):
            raise ValueError(f"refusing to update a symlink in the {harness} runtime alias bundle")
        if any(pair_present) and not all(pair_present):
            raise ValueError(f"refusing to update an incomplete {harness} runtime helper bundle")
        if all(pair_present):
            if any(not path.is_file() for path in paired) or bounded(paired[0], 256 * 1024) != bounded(paired[1], 256 * 1024):
                raise ValueError(f"refusing to update unverified {harness} runtime helpers")
    return aliases


def existing_owned_legacy_singleton(base, harness, alias):
    """Recognize the historical Context Injector-only bundle without guessing partial layouts."""
    if alias.is_symlink() or not alias.is_file():
        raise ValueError(f"refusing to update an unsafe {harness} runtime alias bundle")
    owned_runtime_core(alias, harness)

    legacy_helpers = {
        "checkpoint": ("Checkpoint support for Pattern Buffer. Standard library only.",
                       "Checkpoint support for Context Injector. Standard library only.",
                       "def _state_path(", "def handle_cli("),
        "enrollment": ("Self-service checkpoint enrollment and conservative native restart preparation.",
                       "def enroll("),
    }
    for name, markers in legacy_helpers.items():
        path = auxiliary_source(name, alias)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"refusing to update an incomplete {harness} legacy runtime alias bundle")
        text = bounded(path, 256 * 1024)
        if name == "checkpoint":
            owned = (any(marker_text in text for marker_text in markers[:2]) and
                     all(marker_text in text for marker_text in markers[2:]))
        else:
            owned = all(marker_text in text for marker_text in markers)
        if not owned:
            raise ValueError(f"refusing to update unowned runtime helper: {path}")

    for prefix in ("context_injector", "pattern_buffer"):
        for name in RUNTIME_HELPERS:
            if name in legacy_helpers and prefix == "context_injector":
                continue
            path = base / f"{prefix}_{harness}_{name}.py"
            if path.exists() or path.is_symlink():
                raise ValueError(f"refusing to update an incomplete {harness} runtime alias bundle")
    return [alias, base / f"pattern_buffer_{harness}.py"]


def owned_runtime_core(path, harness):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"refusing to update an unsafe {harness} runtime alias bundle")
    text = bounded(path, 256 * 1024)
    owned_markers = ("def configure_hooks(args):", "starfleet-context-injector:",
                     "def hook_config(", "def main():")
    if not all(marker_text in text for marker_text in owned_markers):
        raise ValueError(f"refusing to update unowned runtime entrypoint: {path}")
    return text


def runtime_directory(args):
    return Path(args.runtime_dir).expanduser().absolute() if args.runtime_dir else (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        / "starfleet-ai" / "context-injector")


def prepare_install(args):
    base = runtime_directory(args)
    runtime = base / f"context_injector_{args.harness}.py"
    canonical_runtime = base / "pattern_buffer.py"
    harness_alias = base / f"pattern_buffer_{args.harness}.py"
    runtime_launcher = base / "pattern-buffer"
    bin_directory = (Path(args.bin_dir).expanduser().absolute() if args.bin_dir else
                     (Path.home() / ".local/bin" if not args.runtime_dir else base))
    launcher = bin_directory / "pattern-buffer"
    if base.is_symlink() or bin_directory.is_symlink():
        raise ValueError("runtime path must not be a symlink")
    if base.exists() and not base.is_dir():
        raise ValueError("runtime path must be a directory")
    if bin_directory.exists() and not bin_directory.is_dir():
        raise ValueError("launcher directory must be a directory")
    other_harness = "claude" if args.harness == "codex" else "codex"
    if base.exists():
        try:
            other_aliases = existing_owned_aliases(base, other_harness)
        except ValueError as error:
            guidance = recovery_guidance(args.harness, args.settings, args.checkpoints)
            raise ValueError(f"{error}. {guidance}") from error
    else:
        other_aliases = []
    install_scripts = [runtime, canonical_runtime, harness_alias, *other_aliases]
    runtime_paths = [path for script in install_scripts for path in runtime_bundle_paths(script)]
    runtime_paths.extend((base / "guide.md", base / "pattern-buffer.template"))
    if any(path.is_symlink() for path in
           (*runtime_paths, runtime_launcher, launcher, base, bin_directory)):
        raise ValueError("runtime path must not be a symlink")
    if any(path.exists() and not path.is_file() for path in runtime_paths):
        raise ValueError("runtime files must be regular files")
    for script in install_scripts:
        if script.exists():
            try:
                owned_runtime_core(script, other_harness if script in other_aliases else args.harness)
            except ValueError as error:
                if script in (runtime, canonical_runtime, harness_alias) and "unowned runtime entrypoint" in str(error):
                    guidance = recovery_guidance(args.harness, args.settings, args.checkpoints)
                    raise ValueError(f"{error}. {guidance}") from error
                raise
        elif any(path.exists() for path in runtime_bundle_paths(script)[1:]):
            raise ValueError(f"refusing to replace runtime helpers without an owned entrypoint: {script}")
    sources = {
        "core": bounded(Path(__file__).resolve(), 256 * 1024),
        "guide": bounded(guide_source(), FILE_LIMIT),
        "launcher_template": bounded(Path(__file__).resolve().with_name("pattern-buffer.template"), FILE_LIMIT),
        "checkpoint": bounded(checkpoint_source(), 256 * 1024),
        "enrollment": bounded(enrollment_source(), 256 * 1024),
        "sessions": bounded(auxiliary_source("sessions"), 256 * 1024),
        "reliability": bounded(auxiliary_source("reliability"), 256 * 1024),
        "required_reads": bounded(auxiliary_source("required_reads"), 256 * 1024),
    }
    launcher_template = sources["launcher_template"]
    runtime_binding = "RUNTIME_DIR = None"
    if launcher_template.count(runtime_binding) != 1:
        raise ValueError("Pattern Buffer launcher template is malformed")
    python_shebang = "#!/usr/bin/env python3"
    if launcher_template.count(python_shebang) != 1:
        raise ValueError("Pattern Buffer launcher template is malformed")
    if not Path(sys.executable).is_absolute():
        raise ValueError("Pattern Buffer launcher requires an absolute installation interpreter")
    launcher_source = launcher_template.replace(runtime_binding, f"RUNTIME_DIR = {str(base)!r}")
    launcher_source = launcher_source.replace(python_shebang, f"#!{sys.executable}", 1)
    for launcher_path in {runtime_launcher, launcher}:
        if not launcher_path.exists():
            continue
        existing_launcher = bounded(launcher_path, FILE_LIMIT)
        if "starfleet-pattern-buffer-launcher:v1" not in existing_launcher:
            raise ValueError(f"refusing to replace an unowned launcher: {launcher_path}")
    return {
        "base": base,
        "runtime": runtime,
        "runtime_launcher": runtime_launcher,
        "launcher": launcher,
        "install_scripts": install_scripts,
        "sources": sources,
        "launcher_source": launcher_source,
    }


def read_settings(path):
    # Preserve unrelated settings, but never rewrite malformed settings.
    if not path.exists() and not path.is_symlink():
        return {}
    data = json.loads(bounded(path, 1024 * 1024))
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError("settings and hooks must be JSON objects")
    for event in ("SessionStart", "PreCompact", "PostToolUse", "UserPromptSubmit", "PreToolUse"):
        groups = data.get("hooks", {}).get(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"{event} must be an array")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"each {event} group must contain a hooks array")
            if any(not isinstance(handler, dict) for handler in group["hooks"]):
                raise ValueError("hook handlers must be objects")
    return data


def owned(handler, harness):
    command = handler.get("command")
    return (handler.get("type") == "command" and isinstance(command, str)
            and command.endswith(marker(harness)))


def reconcile_hook_groups(groups, harness, additions):
    owned_slots = [(group_index, handler_index)
                   for group_index, group in enumerate(groups)
                   for handler_index, handler in enumerate(group["hooks"])
                   if owned(handler, harness)]
    if len(owned_slots) == 1 and len(additions) == 1:
        group_index, handler_index = owned_slots[0]
        updated = list(groups)
        group = groups[group_index]
        if len(group["hooks"]) == 1:
            updated[group_index] = additions[0]
        else:
            handlers = list(group["hooks"])
            handlers[handler_index] = additions[0]["hooks"][0]
            updated[group_index] = {**group, "hooks": handlers}
        return updated

    # Duplicate cleanup and uninstall retain the existing remove-then-add behavior.
    retained = []
    for group in groups:
        handlers = [handler for handler in group["hooks"] if not owned(handler, harness)]
        if len(handlers) == len(group["hooks"]):
            retained.append(group)
        elif handlers:
            retained.append({**group, "hooks": handlers})
    return [*retained, *additions]


def installed_checkpoints_enabled(data, harness):
    events = ("PreCompact", "UserPromptSubmit")
    if any(owned(handler, harness) for event in events
           for group in data.get("hooks", {}).get(event, []) for handler in group["hooks"]):
        return True
    for group in data.get("hooks", {}).get("PostToolUse", []):
        for handler in group["hooks"]:
            command = handler.get("command", "")
            if owned(handler, harness) and "--required-reads-only" not in command:
                return True
    status_line = data.get("statusLine")
    command = status_line.get("command") if isinstance(status_line, dict) else None
    return (harness == "claude" and isinstance(command, str)
            and command.endswith(" # starfleet-context-statusline:v1"))


def configure_hooks(args):
    if args.harness == "opencode":
        return configure_opencode(args)
    path = settings_path(args.harness, args.settings)
    with ExitStack() as locks:
        install = None
        if args.command == "install":
            base = runtime_directory(args)
            # Keep malformed settings and unsafe runtimes as no-write refusals.
            read_settings(path)
            try:
                install = prepare_install(args)
            except ValueError:
                if base.is_symlink() or not base.is_dir():
                    raise
                # Another cooperating install may have been between its atomic
                # bundle writes during the refusal-only preflight above.
                locks.enter_context(locked_directory(base))
                install = prepare_install(args)
            else:
                if not base.exists():
                    base.mkdir(parents=True, exist_ok=True, mode=0o700)
                locks.enter_context(locked_directory(base))
                # Rebuild the complete plan while holding the shared runtime lock.
                # Other-harness aliases may have changed since the first snapshot.
                install = prepare_install(args)
        locks.enter_context(locked(path.with_name(path.name + ".context-injector.lock")))
        data = read_settings(path)
        before = json.dumps(data, sort_keys=True)
        hooks = data.get("hooks", {})
        hooks = dict(hooks)
        runtime = install["runtime"] if install else None
        additions_by_event = (hook_config(args.harness, runtime, args.checkpoints)["hooks"]
                              if install else {})
        for event in ("SessionStart", "PreCompact", "PostToolUse", "UserPromptSubmit", "PreToolUse"):
            groups = reconcile_hook_groups(hooks.get(event, []), args.harness,
                                           additions_by_event.get(event, []))
            if groups:
                hooks[event] = groups
            else:
                hooks.pop(event, None)
        if install:
            base = install["base"]
            runtime_launcher = install["runtime_launcher"]
            launcher = install["launcher"]
            install_scripts = install["install_scripts"]
            install_sources = install["sources"]
            launcher_source = install["launcher_source"]
        if hooks:
            data["hooks"] = hooks
        else:
            data.pop("hooks", None)
        # Carry the exact prior status-line configuration inside the owned command.
        # This avoids a second global registry and preserves unrelated display options.
        if args.harness == "claude":
            prior = data.get("statusLine")
            command = prior.get("command", "") if isinstance(prior, dict) else ""
            status_marker = " # starfleet-context-statusline:v1"
            if command.endswith(status_marker):
                argv = shlex.split(command, comments=True)
                if len(argv) != 5 or argv[2:4] != ["statusline", "--delegate-json"]:
                    raise ValueError("owned status line is malformed; restore its backup before reinstalling")
                prior = json.loads(argv[4])
            if runtime and args.checkpoints:
                if prior is not None and (not isinstance(prior, dict) or prior.get("type") != "command"
                                          or not isinstance(prior.get("command"), str)):
                    raise ValueError("checkpoint status line requires an existing command status line or no status line")
                data["statusLine"] = {**(prior or {}), "type": "command", "command": shlex.join([
                    sys.executable, str(runtime), "statusline", "--delegate-json", json.dumps(prior)]) + status_marker}
            elif command.endswith(status_marker):
                if prior is None:
                    data.pop("statusLine", None)
                else:
                    data["statusLine"] = prior
        changed = json.dumps(data, sort_keys=True) != before
        if changed and path.exists():
            backup = path.with_name(path.name + f".context-injector-backup-{uuid.uuid4().hex}")
            atomic(backup, bounded(path, 1024 * 1024))
            print(f"Backup: {backup}")
        if runtime is not None:
            for installed_script in install_scripts:
                for name in RUNTIME_HELPERS:
                    atomic(auxiliary_source(name, installed_script), install_sources[name])
                atomic(enrollment_source(installed_script), install_sources["enrollment"])
                atomic(checkpoint_source(installed_script), install_sources["checkpoint"])
                atomic(installed_script, install_sources["core"])
            atomic(base / "guide.md", install_sources["guide"])
            atomic(base / "pattern-buffer.template", install_sources["launcher_template"])
            for launcher_path in {runtime_launcher, launcher}:
                if not launcher_path.exists() or bounded(launcher_path, FILE_LIMIT) != launcher_source:
                    atomic(launcher_path, launcher_source)
                os.chmod(launcher_path, 0o755)
        if changed:
            atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print(f"{args.command}: {path} ({'updated' if changed else 'unchanged'})")
        if runtime:
            print(f"Runtime: {runtime}")
            print("Restart or resume the harness; review hook trust where required. Delivery is not verified.")
        else:
            print("Worktree context, assignments, backups, and runtime copies were retained.")


def configure_opencode(args):
    if args.command == "install" and (not args.checkpoints or args.bin_dir):
        raise ValueError("OpenCode uses a plugin and does not accept Claude/Codex hook or launcher options")
    path = settings_path("opencode", args.settings)
    if path.is_symlink():
        raise ValueError("OpenCode settings path must not be a symlink")
    runtime = opencode_runtime_directory(args.runtime_dir)
    plugin = opencode_plugin_directory(runtime)
    plugin_entry = str(plugin.resolve())
    with locked(path.with_name(path.name + ".context-injector.lock")):
        data = read_settings(path)
        plugins = data.get("plugins", [])
        if not isinstance(plugins, list) or any(not isinstance(item, str) for item in plugins):
            raise ValueError("OpenCode plugins must be an array of strings")
        before = json.dumps(data, sort_keys=True)
        if args.command == "install":
            for candidate in (runtime, plugin):
                if candidate.is_symlink():
                    raise ValueError(f"OpenCode runtime path must not be a symlink: {candidate}")
            runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not runtime.is_dir():
                raise ValueError("OpenCode runtime path is not a directory")
            source_root = Path(__file__).resolve().parent
            copies = {
                runtime / "pattern_buffer.py": Path(__file__).resolve(),
                runtime / "pattern_buffer_sessions.py": auxiliary_source("sessions"),
                runtime / "pattern_buffer_reliability.py": auxiliary_source("reliability"),
                runtime / "pattern_buffer_checkpoint.py": checkpoint_source(),
                runtime / "pattern_buffer_enrollment.py": enrollment_source(),
                runtime / "guide.md": guide_source(),
                plugin / "package.json": source_root / "opencode-plugin/package.json",
                plugin / "server.js": source_root / "opencode-plugin/server.js",
            }
            plugin.mkdir(parents=True, exist_ok=True, mode=0o700)
            if plugin.is_symlink():
                raise ValueError("OpenCode plugin directory must not be a symlink")
            for destination, source in copies.items():
                if destination.is_symlink():
                    raise ValueError(f"OpenCode runtime file must not be a symlink: {destination}")
                atomic(destination, bounded(source, FILE_LIMIT if source.name == "guide.md" else 256 * 1024))
            if plugin_entry not in plugins:
                data["plugins"] = [*plugins, plugin_entry]
        else:
            remaining = [item for item in plugins if item != plugin_entry]
            if remaining != plugins:
                data["plugins"] = remaining
            elif "plugins" in data:
                data["plugins"] = plugins
        changed = json.dumps(data, sort_keys=True) != before
        if changed and path.exists():
            backup = path.with_name(path.name + f".context-injector-backup-{uuid.uuid4().hex}")
            atomic(backup, bounded(path, 1024 * 1024))
        if changed:
            atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print(f"{args.command}: {path} ({'updated' if changed else 'unchanged'})")
        if args.command == "install":
            print(f"Runtime: {runtime}")
            print("OpenCode plugin configured in the operator-supplied settings file; native delivery is not verified.")
        else:
            print("OpenCode settings entry removed; runtime files were retained.")


def sections(path):
    return sections_text(bounded(path, FILE_LIMIT))


def sections_text(text):
    result = {}
    current = None
    for line in text.splitlines():
        if line.startswith("```") or line.startswith("~~~"):
            raise ValueError("pilot format does not support fenced code blocks")
        if line.startswith("# "):
            if current == "confirmation":
                raise ValueError("# confirmation must be the final context section")
            current = line[2:].strip()
            if not result and current != "shared":
                raise ValueError("context must begin with # shared")
            if current not in ("shared", *ROLES, "confirmation") or current in result:
                raise ValueError("use each exact heading once: # shared, # implementer, # orchestrator, optional final # confirmation")
            result[current] = []
        elif current is not None:
            result[current].append(line)
        elif line.strip():
            raise ValueError("context must begin with # shared")
    if "shared" not in result:
        raise ValueError("missing # shared section")
    return {key: "\n".join(value).strip() for key, value in result.items()}


def compose_sections(rules_text, mission_text):
    rules = sections_text(rules_text)
    mission = sections_text(mission_text)
    required = ("shared", *ROLES)
    if any(section not in rules for section in required):
        raise ValueError("rules must include # shared, # implementer, and # orchestrator")
    if "confirmation" in mission:
        raise ValueError("mission may include # shared, # implementer, and # orchestrator only")
    result = {}
    for section in required:
        pieces = [text for text in (mission.get(section, ""), rules.get(section, "")) if text]
        result[section] = "\n\n".join(pieces)
    if "confirmation" in rules:
        result["confirmation"] = rules["confirmation"]
    return result


def serialize_sections(content):
    parts = []
    for section in ("shared", *ROLES, "confirmation"):
        if section not in content:
            continue
        parts.append(f"# {section}\n\n{content[section]}")
    return ("\n\n".join(parts).rstrip() + "\n").encode("utf-8")


def _paragraph_parts(text):
    paragraphs = []
    separator_bytes = 0
    for piece in re.split(r"(\n[ \t]*\n+)", text):
        if not piece:
            continue
        if piece.startswith("\n") and piece.strip(" \t\n") == "":
            separator_bytes += len(piece.encode("utf-8"))
        else:
            paragraphs.append(piece)
    return paragraphs, separator_bytes


def paragraph_accounting(mission, rules, content):
    accounting = {}
    for role in ROLES:
        selected = ["shared", role]
        if content.get("confirmation"):
            selected.append("confirmation")
        paragraphs = []
        separators = 0
        content_bytes = 0
        for section_index, section in enumerate(selected):
            if section_index:
                separators += 2
                content_bytes += 2
            source_sections = (("mission", mission), ("rules", rules))
            section_sources = [(label, source.get(section, ""))
                               for label, source in source_sections if source.get(section, "")]
            for source_index, (source_name, text) in enumerate(section_sources):
                if source_index:
                    separators += 2
                    content_bytes += 2
                blocks, internal_separators = _paragraph_parts(text)
                separators += internal_separators
                content_bytes += internal_separators
                for index, block in enumerate(blocks, 1):
                    size = len(block.encode("utf-8"))
                    content_bytes += size
                    paragraphs.append({"section": section, "source": source_name,
                                       "paragraph": index, "bytes": size})
        selected_text = content["shared"] + "\n\n" + content[role]
        if content.get("confirmation"):
            selected_text += "\n\n" + content["confirmation"]
        if content_bytes != len(selected_text.encode("utf-8")):
            raise ValueError("paragraph accounting did not match the rendered content")
        accounting[role] = {"paragraphs": paragraphs,
                            "paragraph_bytes": sum(item["bytes"] for item in paragraphs),
                            "separator_bytes": separators,
                            "content_bytes": content_bytes}
    return accounting


def _heading_slug(value):
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = value.replace("`", "").lower()
    value = re.sub(r"[^\w\- ]", "", value, flags=re.UNICODE)
    return re.sub(r"-+", "-", re.sub(r"\s+", "-", value).strip("-"))


def markdown_anchors(text):
    anchors = set()
    counts = {}
    previous = ""
    for line in text.splitlines():
        atx = re.match(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", line)
        if atx:
            heading = atx.group(1).strip()
        elif previous.strip() and re.match(r"^ {0,3}(?:=+|-+)[ \t]*$", line):
            heading = previous.strip()
        else:
            heading = None
        if heading is not None:
            explicit = re.search(r"\s*\{#([^}\s]+)\}\s*$", heading)
            if explicit:
                anchors.add(explicit.group(1))
                heading = heading[:explicit.start()].rstrip()
            slug = _heading_slug(heading)
            count = counts.get(slug, 0)
            counts[slug] = count + 1
            anchors.add(slug if count == 0 else f"{slug}-{count}")
        previous = line
    return anchors


def _reference_finding(kind, source, target, message, severity="warning"):
    return {"kind": kind, "source": str(source), "target": target,
            "message": message, "severity": severity}


def _markdown_target(target, source, root, own_text, totals, findings):
    target = target.strip()
    if not target:
        findings.append(_reference_finding("unsupported", source, target,
                                           "empty Markdown targets are not checked"))
        return
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    try:
        parts = urlsplit(target)
    except ValueError:
        findings.append(_reference_finding("unsupported", source, target,
                                           "ambiguous URL syntax was not checked"))
        return
    if parts.scheme.lower() in ("http", "https") or target.startswith("//"):
        findings.append(_reference_finding("remote", source, target,
                                           "remote reference was not requested from the network"))
        return
    if parts.scheme:
        findings.append(_reference_finding("unsupported", source, target,
                                           "non-HTTP URL schemes are not checked"))
        return
    path_text = unquote(parts.path)
    if path_text.startswith("~/"):
        target_path = Path(path_text).expanduser()
    elif path_text.startswith("/"):
        target_path = Path(path_text)
    elif path_text:
        target_path = root / path_text
    else:
        target_path = Path(source)
    same_document = target_path.resolve(strict=False) == Path(source).resolve(strict=False)
    if not same_document and not target_path.exists():
        findings.append(_reference_finding("missing_target", source, target,
                                           f"local Markdown target does not exist: {target_path}", "error"))
        return
    if not parts.fragment:
        return
    if target_path.suffix.lower() not in (".md", ".markdown"):
        findings.append(_reference_finding("unsupported_anchor", source, target,
                                           "anchors are checked only for Markdown targets"))
        return
    try:
        cache_key = target_path.resolve(strict=False)
        if cache_key in totals["anchors"]:
            anchors = totals["anchors"][cache_key]
        else:
            if same_document:
                target_text = own_text
            else:
                size = target_path.stat().st_size
                if size > REFERENCE_FILE_LIMIT or totals["bytes"] + size > REFERENCE_TOTAL_LIMIT:
                    findings.append(_reference_finding("reference_limit", source, target,
                                                       "Markdown heading scan exceeded its byte limit"))
                    return
                target_text = bounded(target_path, REFERENCE_FILE_LIMIT)
                totals["bytes"] += len(target_text.encode("utf-8"))
            anchors = markdown_anchors(target_text)
            totals["anchors"][cache_key] = anchors
    except (OSError, ValueError, UnicodeError) as error:
        findings.append(_reference_finding("unsupported_anchor", source, target,
                                           f"Markdown heading scan could not read the target: {error}"))
        return
    anchor = unquote(parts.fragment)
    if anchor not in anchors:
        findings.append(_reference_finding("missing_heading", source, target,
                                           f"Markdown heading anchor does not exist: #{anchor}", "error"))


def reference_check(text, source, root):
    source = Path(source)
    root = Path(root)
    findings = []
    totals = {"bytes": 0, "targets": 0, "anchors": {}}
    lines = text.splitlines(keepends=True)
    masked_lines = []
    in_fence = False
    fence_warning = False
    for line in lines:
        if re.match(r"^ {0,3}(?:```|~~~)", line):
            in_fence = not in_fence
            fence_warning = True
            masked_lines.append("\n" if line.endswith("\n") else "")
        elif in_fence:
            masked_lines.append("\n" if line.endswith("\n") else "")
        else:
            masked_lines.append(line)
    if in_fence:
        findings.append(_reference_finding("unsupported_fence", source, "fenced code block",
                                           "unclosed fenced code block was not scanned"))
    elif fence_warning:
        findings.append(_reference_finding("unsupported_fence", source, "fenced code block",
                                           "fenced code contents were not scanned"))
    visible = "".join(masked_lines)
    for match in re.finditer(r"(?<!`)``[^`\n]+``(?!`)", visible):
        findings.append(_reference_finding("unsupported_code_span", source, match.group(0),
                                           "multi-backtick code spans are not scanned"))
    masked = list(visible)
    code_spans = list(re.finditer(r"(?<!`)`([^`\n]+)`(?!`)", visible))
    for match in code_spans:
        masked[match.start():match.end()] = " " * (match.end() - match.start())
        value = match.group(1).strip()
        if (value.startswith("/") or value.startswith("~/")) and not re.search(r"\s", value):
            candidate = Path(value).expanduser()
            totals["targets"] += 1
            if not candidate.exists():
                findings.append(_reference_finding("missing_path", source, value,
                                                   f"backticked local path does not exist: {candidate}", "error"))
        elif value.startswith(("/", "~/")) or re.search(r"(?:^|\s)(?:/|~/)[^\s]+", value):
            findings.append(_reference_finding("unsupported_command_path", source, value,
                                               "ambiguous or command-style inline code is not parsed as a path"))
        if totals["targets"] > REFERENCE_COUNT_LIMIT:
            findings.append(_reference_finding("reference_limit", source, str(totals["targets"]),
                                               f"reference count exceeds {REFERENCE_COUNT_LIMIT}; remaining references were not checked"))
            break
    visible = "".join(masked)
    inline_pattern = re.compile(r"!?\[[^\]\n]*\]\(([^)\n]*)\)")
    inline_matches = list(inline_pattern.finditer(visible))
    for match in inline_matches:
        inside = match.group(1).strip()
        target = inside
        if inside.startswith("<"):
            end = inside.find(">")
            if end < 0:
                findings.append(_reference_finding("unsupported", source, inside,
                                                   "angle-bracket Markdown targets need a closing >"))
                continue
            target, suffix = inside[1:end], inside[end + 1:].strip()
            if suffix and not re.fullmatch(r"(?:\"[^\"]*\"|'[^']*')", suffix):
                findings.append(_reference_finding("unsupported", source, inside,
                                                   "Markdown target title syntax is not recognized"))
                continue
        else:
            tokens = inside.split(None, 1)
            target = tokens[0] if tokens else ""
            if ("(" in target or ")" in target or
                    (len(tokens) > 1 and not re.fullmatch(r"(?:\"[^\"]*\"|'[^']*')", tokens[1]))):
                findings.append(_reference_finding("unsupported", source, inside,
                                                   "ambiguous Markdown target syntax was not checked"))
                continue
        totals["targets"] += 1
        if totals["targets"] > REFERENCE_COUNT_LIMIT:
            findings.append(_reference_finding("reference_limit", source, str(totals["targets"]),
                                               f"reference count exceeds {REFERENCE_COUNT_LIMIT}; remaining references were not checked"))
            break
        _markdown_target(target, source, root, text, totals, findings)
    matched_starts = {match.start() for match in inline_matches}
    for match in re.finditer(r"!?\[[^\]\n]*\]\(", visible):
        if match.start() not in matched_starts:
            findings.append(_reference_finding("unsupported_inline_link", source, match.group(0),
                                               "nested or ambiguous inline Markdown targets are not checked"))
    for match in re.finditer(r"!?\[[^\]\n]*\]\s*\[[^\]\n]*\]", visible):
        findings.append(_reference_finding("unsupported_reference_link", source, match.group(0),
                                           "reference-style Markdown links are not checked"))
    for match in re.finditer(r"!?\[[^\]\n]+\]\s*\n\s*\[[^\]\n]*\]", visible):
        findings.append(_reference_finding("unsupported_reference_link", source, match.group(0),
                                           "multiline reference-style Markdown links are not checked"))
    for line in visible.splitlines():
        if re.match(r"^ {0,3}\[[^\]]+\]:", line):
            findings.append(_reference_finding("unsupported_reference_definition", source, line.strip(),
                                               "Markdown reference definitions are not checked"))
    if re.search(r"<\s*(?:a|img)\b", visible, re.IGNORECASE):
        findings.append(_reference_finding("unsupported_html_reference", source, "HTML link or image",
                                           "raw HTML references are not checked"))
    remote_targets = {item["target"] for item in findings if item["kind"] == "remote"}
    for match in re.finditer(r"https?://[^\s<>]+", visible, re.IGNORECASE):
        target = match.group(0).rstrip(".,;:!?)")
        if target and target not in remote_targets:
            totals["targets"] += 1
            if totals["targets"] > REFERENCE_COUNT_LIMIT:
                findings.append(_reference_finding("reference_limit", source, str(totals["targets"]),
                                                   f"reference count exceeds {REFERENCE_COUNT_LIMIT}; remaining references were not checked"))
                break
            findings.append(_reference_finding("remote", source, target,
                                               "remote reference was not requested from the network"))
            remote_targets.add(target)
    if totals["targets"] > REFERENCE_COUNT_LIMIT and not any(
            item["kind"] == "reference_limit" for item in findings):
        findings.append(_reference_finding("reference_limit", source, str(totals["targets"]),
                                           f"reference count exceeds {REFERENCE_COUNT_LIMIT}; remaining references were not checked"))
    unique = {}
    for item in findings:
        unique[(item["kind"], item["source"], item["target"], item["severity"])] = item
    findings = list(unique.values())
    return {"errors": [item for item in findings if item["severity"] == "error"],
            "warnings": [item for item in findings if item["severity"] != "error"]}


def reference_warnings_for_path(path, root):
    if path is None:
        return []
    try:
        text = bounded(Path(path), FILE_LIMIT)
        report = reference_check(text, path, root)
        findings = report["errors"] + report["warnings"]
        return [{**item, "validation_error": item["severity"] == "error", "severity": "warning"}
                for item in findings]
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        return [_reference_finding("unreadable_source", path, str(path),
                                   f"reference scan could not read the context file: {error}")]


def _read_compose_output(path):
    if path.is_symlink():
        raise ValueError(f"output must not be a symlink: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"output must be a regular file: {path}")
        data = bytearray()
        while len(data) <= FILE_LIMIT:
            chunk = os.read(descriptor, min(16 * 1024, FILE_LIMIT + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        return None if len(data) > FILE_LIMIT else bytes(data)
    finally:
        os.close(descriptor)


def compose_output_write(path, data, replace):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"output must not be a symlink: {path}")
    if path.exists():
        if not path.is_file():
            raise ValueError(f"output must be a regular file: {path}")
        old = _read_compose_output(path)
        if old == data:
            return "unchanged"
        if not replace:
            raise ValueError(f"output differs from the composed context; use --replace: {path}")
        atomic(path, data.decode("utf-8"))
        return "replaced"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".compose-pending-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"output must be a regular file: {path}")
            if _read_compose_output(path) == data:
                return "unchanged"
            if not replace:
                raise ValueError(f"output differs from the composed context; use --replace: {path}")
            atomic(path, data.decode("utf-8"))
            return "replaced"
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return "created"
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _path_is_session_source(path, binding=None):
    candidate = Path(path).resolve(strict=False)
    if binding is not None and candidate == Path(binding.path).resolve(strict=False):
        return True
    parts = candidate.parts
    return any(parts[index:index + 2] == (".context-injector", "sessions")
               for index in range(max(0, len(parts) - 1)))


def compose_report(root, gitdir, args):
    input_paths = {"rules": Path(args.rules).expanduser(), "mission": Path(args.mission).expanduser()}
    output = Path(args.output).expanduser()
    for label, path in (*input_paths.items(), ("output", output)):
        if not path.is_absolute():
            raise ValueError(f"--{label} must be an absolute path")
    if args.min_headroom < 0:
        raise ValueError("--min-headroom must be zero or greater")
    if args.session is not None and not args.session:
        raise ValueError("--session must not be empty")
    session = args.session or "validation"
    state_path(gitdir, args.harness, session, args.agent)
    for path in input_paths.values():
        if path.resolve(strict=False) == output.resolve(strict=False):
            raise ValueError("--output must be separate from both input files")
    if args.check:
        sys.dont_write_bytecode = True
    rules_text = bounded(input_paths["rules"], FILE_LIMIT)
    mission_text = bounded(input_paths["mission"], FILE_LIMIT)
    rules_sections = sections_text(rules_text)
    mission_sections = sections_text(mission_text)
    content = compose_sections(rules_text, mission_text)
    output_data = serialize_sections(content)
    if len(output_data) > FILE_LIMIT:
        raise ValueError(f"composed context exceeds {FILE_LIMIT} bytes")
    binding = None
    normalized_session = session
    if args.session and not args.agent:
        session_store = auxiliary("sessions")
        normalized_session = session_store._normalize_session(session, strict=True)
        binding = session_binding_readonly(root, gitdir, args.harness, session)
    if _path_is_session_source(output, binding):
        raise ValueError("compose to a private output path; update registered exact-session context with session-enroll --reviewed-update --evidence")
    reference_report = reference_check(output_data.decode("utf-8"), output, root)
    reference_errors = reference_report["errors"]
    reference_warnings = reference_report["warnings"]
    current_preview = standing_context_budgets(root, output, args.harness, session, args.agent,
                                               "composition preview", content)
    current_preview.update(exact_session=bool(args.session),
                           session_basis="requested exact session" if args.session else
                           "validation placeholder session",
                           meaning="Private output preview. It does not change or describe active runtime context.")
    future_recipient = None
    if args.session and not args.agent:
        target_path = Path(binding.path) if binding is not None else Path(
            auxiliary("sessions")._session_path(root, args.harness, normalized_session))
        future_recipient = standing_context_budgets(root, target_path, args.harness, session, "",
                                                    "session enrollment", content)
        future_recipient.update(exact_session=True,
                                budget_calculated=True,
                                binding_state=binding.state if binding is not None else "not_enrolled",
                                meaning="Prospective session-enrollment budget. Publication still requires the reviewed session-enroll route.")
    accounting = paragraph_accounting(mission_sections, rules_sections, content)
    for role in ROLES:
        if accounting[role]["content_bytes"] != current_preview["roles"][role].get("content_bytes"):
            raise ValueError(f"{role} paragraph accounting did not match the existing rendered budget")
        accounting[role].update(header_bytes=current_preview["roles"][role]["header_bytes"],
                                total_bytes=current_preview["roles"][role]["bytes_used"],
                                remaining_bytes=current_preview["roles"][role]["remaining_bytes"])
    reports = [("current preview", current_preview)]
    if future_recipient is not None:
        reports.append(("future recipient", future_recipient))
    headroom_failures = []
    for label, budget_set in reports:
        for role, budget in budget_set["roles"].items():
            if budget.get("remaining_bytes", -1) < args.min_headroom:
                headroom_failures.append(f"{label} {role}: {budget.get('remaining_bytes', -1)} bytes remain; "
                                         f"minimum is {args.min_headroom}")
    if future_recipient is None:
        future_recipient = {
            "validated": False,
            "meaning": ("No future session-enrollment budget was calculated because agent sessions cannot use "
                        "the primary-session enrollment route." if args.session and args.agent else
                        "No exact recipient was supplied. Pass --session to calculate a future session-enrollment budget."),
        }
    report = {
        "worktree": str(root),
        "harness": args.harness,
        "session": session,
        "agent": args.agent or None,
        "minimum_headroom_bytes": args.min_headroom,
        "inputs": {label: {"path": str(path),
                           "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
                   for label, path, text in (("rules", input_paths["rules"], rules_text),
                                             ("mission", input_paths["mission"], mission_text))},
        "output": {"path": str(output), "sha256": hashlib.sha256(output_data).hexdigest(),
                   "bytes": len(output_data)},
        "paragraph_accounting": accounting,
        "current_preview": current_preview,
        "future_recipient": future_recipient,
        "references": {"errors": reference_errors, "warnings": reference_warnings},
        "headroom_errors": headroom_failures,
        "write_state": "not_written",
    }
    if reference_errors or headroom_failures:
        report["write_state"] = "refused"
        return report, 1
    if args.check:
        if output.is_symlink():
            raise ValueError(f"output must not be a symlink: {output}")
        if not output.exists():
            report["write_state"] = "different"
            report["check"] = "output is missing"
            return report, 1
        if not output.is_file():
            raise ValueError(f"output must be a regular file: {output}")
        same = _read_compose_output(output) == output_data
        report["write_state"] = "not_written"
        report["check"] = "matches" if same else "differs"
        return report, 0 if same else 1
    report["write_state"] = compose_output_write(output, output_data, args.replace)
    return report, 0


def state_path(gitdir, harness, session, agent=""):
    if not isinstance(session, str) or not session or len(session) > 512:
        raise ValueError("a nonempty session ID is required (maximum 512 characters)")
    if harness == "opencode" and not _OPENCODE_SESSION_ID.fullmatch(session):
        raise ValueError("OpenCode requires the exact case-sensitive ses_ session ID")
    if not isinstance(agent, str) or len(agent) > 512:
        raise ValueError("invalid agent ID")
    key = hashlib.sha256(json.dumps([harness, session, agent]).encode()).hexdigest()
    return gitdir / "context-injector" / "sessions" / f"{key}.json"


def resolve_role(root, gitdir, config, harness, session, agent="", forced_role=None,
                 binding=_BINDING_UNSET):
    role = forced_role
    origin = "preview override" if role else "unassigned"
    saved = {}
    enrolled = (session_binding(root, gitdir, harness, session, agent)
                if binding is _BINDING_UNSET else binding)
    if enrolled is not None:
        if enrolled.state != "active":
            raise RequiredContextFailure("This enrollment binding is " + enrolled.state,
                                         enrolled.path, enrolled.enrollment_id)
        return forced_role or enrolled.role, "session enrollment", {"enrollment_id": enrolled.enrollment_id,
                                                     "context_file": str(enrolled.path)}
    path = state_path(gitdir, harness, session, agent)
    if not role and (path.exists() or path.is_symlink()):
        saved = json.loads(bounded(path, FILE_LIMIT))
        if not isinstance(saved, dict):
            raise ValueError("invalid session registration")
        role = saved.get("role")
        if role not in ROLES or saved.get("root") != str(root):
            raise ValueError("invalid session registration")
        origin = "session assignment"
    # Children never inherit the worktree's primary-session default.
    if not role and not agent and harness != "opencode":
        role = config.get("default_role")
        if role:
            origin = "worktree default"
    return role, origin, saved


def current_context_budgets(root, gitdir, harness, session, agent="", binding=_BINDING_UNSET):
    config, source = load(root, gitdir)
    enrolled = (session_binding(root, gitdir, harness, session, agent)
                if binding is _BINDING_UNSET else binding)
    if enrolled is not None:
        source = Path(enrolled.path)
    role, origin, _saved = resolve_role(root, gitdir, config, harness, session, agent,
                                        binding=enrolled)
    raw = bounded(source, FILE_LIMIT)
    if enrolled is not None and (enrolled.file_status != "valid" or
            hashlib.sha256(raw.encode("utf-8")).hexdigest() != enrolled.source_sha256):
        raise ValueError("registered context file is missing, unsafe, or changed outside reviewed publication")
    content = sections_text(raw)
    report = standing_context_budgets(root, source, harness, session, agent, origin, content,
                                      include_unassigned=role is None)
    return {
        "ok": all(budget.get("ok", False) for budget in report["roles"].values()),
        "worktree": str(root),
        "harness": harness,
        "session": session,
        "agent": agent or None,
        "source": str(source),
        "role": role,
        "role_source": origin,
        **report,
    }


def task_path(gitdir, task_id):
    if not isinstance(task_id, str) or len(task_id) != 32 or any(
            character not in "0123456789abcdef" for character in task_id):
        raise ValueError("invalid task ID")
    return gitdir / "context-injector/tasks" / task_id / "task.json"


def read_task(root, gitdir, task_id):
    path = task_path(gitdir, task_id)
    data = json.loads(bounded(path, FILE_LIMIT))
    if (not isinstance(data, dict) or data.get("root") != str(root)
            or data.get("gitdir") != str(gitdir)
            or data.get("harness") not in ("claude", "codex", "opencode")
            or data.get("status") not in ("pending", "active", "finished")):
        raise ValueError("invalid task registration")
    if data["harness"] == "opencode":
        if data.get("role") not in ROLES:
            raise ValueError("invalid OpenCode task role")
        if data.get("session_id") is not None and (
                not isinstance(data["session_id"], str)
                or not _OPENCODE_SESSION_ID.fullmatch(data["session_id"])):
            raise ValueError("invalid OpenCode task session ID")
        if "ticket_version" in data:
            ticket_version = data["ticket_version"]
            if ticket_version != 2 or data.get("parent_kind") not in (
                    "opencode", "claude", "codex", "worktree"):
                raise ValueError("invalid OpenCode task parent contract")
            if (data["parent_kind"] == "opencode") != ("parent" in data):
                raise ValueError("OpenCode parent contract is incomplete or ambiguous")
        if "parent" in data:
            validate_opencode_parent_record(data.get("parent"), data.get("owner"))
    digest = data.get("brief_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid task brief digest")
    brief = bounded(path.parent / f"{digest}.txt", TASK_LIMIT)
    if hashlib.sha256(brief.encode()).hexdigest() != digest:
        raise ValueError("task brief changed outside update-task")
    return data, brief


def snapshot_brief(path, brief):
    if not brief.strip():
        raise ValueError("task brief must not be empty")
    digest = hashlib.sha256(brief.encode()).hexdigest()
    snapshot = path.parent / f"{digest}.txt"
    if not snapshot.exists():
        atomic(snapshot, brief)
    return digest


def write_dispatch_receipt(path, root, gitdir, task_id):
    data, _ = read_task(root, gitdir, task_id)
    receipt = {"version": 1, "task_id": task_id, "owner_worktree": data["owner"]["root"],
               "worktree": data["root"], "harness": data["harness"],
               "session_id": data["session_id"], "brief_sha256": data["brief_sha256"],
               "created_at": data["created_at"], "status": data["status"],
               "receipt_type": "immutable_dispatch_snapshot",
               "note": "Immutable snapshot written before launch, not live task state; use task_status_command for current metadata.",
               "task_status_command": shlex.join([sys.executable, str(Path(__file__).resolve()),
                                                  "--cwd", str(root), "task-status", "--task-id", task_id])}
    if data["harness"] == "opencode":
        receipt.update(owner_gitdir=data["owner"]["gitdir"], role=data["role"])
        if "ticket_version" in data:
            receipt.update(ticket_version=data["ticket_version"], parent_kind=data["parent_kind"])
        if data.get("parent") is not None:
            receipt["parent"] = data["parent"]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".receipt-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2)
            handle.write("\n")
        # Publish a complete receipt without replacing any existing file or symlink.
        os.link(temporary, path)
    finally:
        os.unlink(temporary)
    return receipt


def opencode_receipt_path(value):
    path = Path(value).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("OpenCode receipt path must not be a symlink")
    return path.resolve()


def list_owner_tasks(owner_cwd, status=None):
    owner_root, owner_gitdir = checkout(owner_cwd)
    config, _ = load(owner_root, owner_gitdir)
    if config.get("context_owner"):
        raise ValueError("select the directly initialized owner worktree")
    owner = {"root": str(owner_root), "gitdir": str(owner_gitdir)}
    tasks, errors = [], []
    common = Path(git(owner_root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    linked = common / "worktrees"
    gitdirs = [common, *sorted(linked.iterdir())] if linked.is_dir() else [common]
    for gitdir in gitdirs:
        for path in sorted((gitdir / "context-injector/tasks").glob("*/task.json")):
            try:
                # Inspect identity before reading a different lane's brief snapshot.
                metadata = json.loads(bounded(path, FILE_LIMIT))
                if not isinstance(metadata, dict):
                    raise ValueError("invalid task registration")
                if metadata.get("owner") != owner:
                    continue
                root, actual_gitdir = checkout(metadata["root"])
                if actual_gitdir != gitdir.resolve():
                    raise ValueError("task worktree identity changed")
                data, _ = read_task(root, gitdir, path.parent.name)
                if not isinstance(data.get("created_at"), (int, float)):
                    raise ValueError("invalid task creation time")
                if status is None or data["status"] == status:
                    tasks.append({"task_id": path.parent.name, **data})
            except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
                errors.append({"gitdir": str(gitdir), "task_id": path.parent.name,
                               "error": str(error)})
    tasks.sort(key=lambda task: (task["created_at"], task["task_id"]))
    return {"owner_worktree": str(owner_root), "tasks": tasks, "errors": errors}


def preflight_dispatch_standing(root, source, harness, content, session=None):
    # OpenCode preflight reserves the largest accepted native session ID; actual binding rechecks the exact ID.
    session = session or ("ses_" + "A" * 508 if harness == "opencode"
                         else "00000000-0000-4000-8000-000000000000")
    return render_standing(root, source, harness, session, "",
                           "implementer", "session assignment", content)


def prepare_dispatch(root, gitdir, args):
    owner_root, owner_gitdir = checkout(args.owner_worktree)
    owner_config, source = load(owner_root, owner_gitdir)
    if owner_config.get("context_owner") or not enabled(owner_config):
        raise ValueError("owner must be a directly initialized, enabled worktree")
    parent_record = None
    parent_kind = None
    session_files = owner_root / ".context-injector" / "sessions"
    kind = os.environ.get("CMUX_AGENT_LAUNCH_KIND", "").strip().lower()
    if kind == "opencode":
        if args.harness != "opencode":
            raise ValueError("an OpenCode parent can prepare only an OpenCode child ticket")
        parent_record = opencode_parent_from_environment(
            owner_root, owner_gitdir, getattr(args, "parent_task_id", None))
        parent_kind = "opencode"
    else:
        if args.harness == "opencode" and os.environ.get("OPENCODE_SESSION_ID"):
            raise ValueError("OPENCODE_SESSION_ID requires CMUX_AGENT_LAUNCH_KIND=opencode")
        parent_harness = "claude" if kind in ("claude", "claudeteams") else "codex" if kind == "codex" else None
        parent_session = os.environ.get("CLAUDE_CODE_SESSION_ID" if parent_harness == "claude" else "CODEX_THREAD_ID")
        parent = session_binding(owner_root, owner_gitdir, parent_harness, parent_session) if parent_harness else None
        if parent is not None or session_files.exists() or session_files.is_symlink():
            if (parent is None or parent.state != "active" or parent.file_status != "valid" or
                    parent.source_sha256 != hashlib.sha256(bounded(source, FILE_LIMIT).encode()).hexdigest()):
                raise ValueError("dispatch cannot substitute a worktree default for a session-specific parent mission; "
                                 "use a dedicated --owner-worktree containing the parent's approved standing context")
        parent_kind = parent_harness if parent is not None else "worktree"
        if getattr(args, "parent_task_id", None) is not None:
            raise ValueError("--parent-task-id is only valid for a verified OpenCode parent session")
    owner = {"root": str(owner_root), "gitdir": str(owner_gitdir)}
    if owner_root != root:
        validate_owner(root, owner)
    content = sections(source)
    if "implementer" not in content:
        raise ValueError("owner is missing # implementer")
    brief = bounded(Path(args.task_file).expanduser().absolute(), TASK_LIMIT)
    if not brief.strip():
        raise ValueError("task brief must not be empty")
    selected_context = content["shared"] + "\n\n" + content["implementer"]
    if content.get("confirmation"):
        selected_context += "\n\n" + content["confirmation"]
    if len(selected_context.encode()) > LIMIT:
        raise ValueError("selected owner context is too large")
    preflight_text = (preflight_dispatch_standing(root, source, args.harness, content)
                      if args.harness == "opencode" else None)
    config_path = gitdir / "context-injector/config.json"
    with locked(config_path.with_suffix(".lock")):
        create_config = not config_path.exists()
        if config_path.exists():
            config, _ = load(root, gitdir)
            expected_owner = owner if owner_root != root else None
            if config.get("context_owner") != expected_owner or not enabled(config):
                raise ValueError("target is disabled or enrolled to another context source")
        else:
            config = {"version": 1, "root": str(root), "enabled": True,
                      "default_role": None, "context_owner": owner}
        if create_config and args.harness != "opencode":
            atomic(config_path, json.dumps(config, indent=2))
        task_id = uuid.uuid4().hex
        path = task_path(gitdir, task_id)
        digest = hashlib.sha256(brief.encode()).hexdigest()
        data = {"root": str(root), "gitdir": str(gitdir), "harness": args.harness,
                "owner": owner, "created_at": time.time(), "status": "pending",
                "session_id": None, "brief_sha256": digest}
        if args.harness == "opencode":
            if getattr(args, "role", None) != "implementer":
                raise ValueError("OpenCode task preparation requires an explicit implementer role")
            if not getattr(args, "receipt_file", None):
                raise ValueError("OpenCode task preparation requires --receipt-file")
            receipt_path = opencode_receipt_path(args.receipt_file)
            if receipt_path.exists() or receipt_path.is_symlink():
                raise ValueError("receipt file already exists; use a unique file for each preparation")
            data.update(role=args.role, receipt_file=str(receipt_path),
                        ticket_version=2, parent_kind=parent_kind or "worktree")
            if parent_record is not None:
                data["parent"] = parent_record
        if args.harness == "opencode":
            compose_task_context(preflight_text, task_id, data, brief,
                                 has_confirmation=bool(content.get("confirmation")))
        if create_config and args.harness == "opencode":
            atomic(config_path, json.dumps(config, indent=2))
        if snapshot_brief(path, brief) != digest:
            raise ValueError("task brief changed while preparing the dispatch")
        atomic(path, json.dumps(data, indent=2))
    return task_id


def preflight_dispatch_context(root, harness, task_id, task_data, brief, session=None):
    root, gitdir = checkout(root)
    _config, source = load(root, gitdir)
    content = sections(source)
    standing = preflight_dispatch_standing(root, source, harness, content, session=session)
    return compose_task_context(standing, task_id, task_data, brief,
                               has_confirmation=bool(content.get("confirmation")))


def bind_dispatch(root, gitdir, harness, session, task_id):
    path = task_path(gitdir, task_id)
    binding = state_path(gitdir, harness, session)
    with locked(binding.with_suffix(".lock")), locked(path.with_suffix(".lock")):
        data, _ = read_task(root, gitdir, task_id)
        if data["harness"] != harness or data["status"] == "finished":
            raise ValueError("dispatch is finished or belongs to another harness")
        if data["session_id"] not in (None, session):
            raise ValueError("dispatch already belongs to another session; inherited tokens cannot enroll children")
        if data["session_id"] is None and time.time() - data["created_at"] > 600:
            raise ValueError("unclaimed dispatch expired; prepare a fresh dispatch")
        if binding.exists():
            previous = json.loads(bounded(binding, FILE_LIMIT))
            if not isinstance(previous, dict) or previous.get("task_id") != task_id:
                raise ValueError("session already has another assignment")
        data.update(status="active", session_id=session)
        atomic(path, json.dumps(data, indent=2))
        atomic(binding, json.dumps({"root": str(root), "role": "implementer", "task_id": task_id}))


def bind_opencode_dispatch(root, gitdir, args):
    if args.harness != "opencode" or args.role != "implementer":
        raise ValueError("OpenCode binding requires the prepared implementer role")
    if not isinstance(args.session, str) or not _OPENCODE_SESSION_ID.fullmatch(args.session):
        raise ValueError("OpenCode binding requires the exact case-sensitive ses_ session ID")
    path = task_path(gitdir, args.task_id)
    binding = state_path(gitdir, "opencode", args.session)
    receipt_path = opencode_receipt_path(args.receipt_file)
    with locked(binding.with_suffix(".lock")), locked(path.with_suffix(".lock")):
        data, _ = read_task(root, gitdir, args.task_id)
        owner = data.get("owner")
        owner_root, owner_gitdir = validate_owner(root, owner)
        parent = data.get("parent")
        if "parent" in data:
            opencode_parent_assignment(owner_root, owner_gitdir, parent["session_id"], parent["task_id"],
                                       parent["surface_id"], parent["workspace_id"], expected=parent)
        config, _ = load(root, gitdir)
        if config.get("context_owner") != owner:
            raise ValueError("child worktree owner pointer no longer matches the prepared task")
        if data.get("harness") != "opencode" or data.get("role") != args.role:
            raise ValueError("task receipt harness or prepared role does not match")
        if data.get("receipt_file") != str(receipt_path):
            raise ValueError("receipt path does not match the prepared task ticket")
        if data.get("status") != "pending" or data.get("session_id") is not None:
            raise ValueError("only an unbound pending OpenCode ticket can be claimed")
        if time.time() - data["created_at"] > 600:
            raise ValueError("unclaimed OpenCode ticket expired; prepare a fresh ticket")
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise ValueError("prepared OpenCode receipt is missing or is not a regular file")
        receipt = json.loads(bounded(receipt_path, FILE_LIMIT))
        if not isinstance(receipt, dict):
            raise ValueError("receipt content does not match the pending task and owner pointers")
        expected = {
            "version": 1,
            "receipt_type": "immutable_dispatch_snapshot",
            "task_id": args.task_id,
            "owner_worktree": str(owner_root),
            "owner_gitdir": str(owner_gitdir),
            "worktree": str(root),
            "harness": "opencode",
            "role": "implementer",
            "session_id": None,
            "brief_sha256": data["brief_sha256"],
            "created_at": data["created_at"],
            "status": "pending",
        }
        if parent is not None:
            expected["parent"] = parent
        if "ticket_version" in data:
            expected.update(ticket_version=data["ticket_version"], parent_kind=data["parent_kind"])
        for key in ("parent", "ticket_version", "parent_kind"):
            if (key in receipt) != (key in expected) or receipt.get(key) != expected.get(key):
                raise ValueError("receipt parent contract does not match the prepared task")
        task_data, brief = read_task(root, gitdir, args.task_id)
        preflight_dispatch_context(root, "opencode", args.task_id, task_data, brief, session=args.session)
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise ValueError("receipt content does not match the pending task and owner pointers")
        if binding.exists() or binding.is_symlink():
            raise ValueError("OpenCode session already has an assignment")
        data.update(status="active", session_id=args.session)
        atomic(path, json.dumps(data, indent=2))
        atomic(binding, json.dumps({"root": str(root), "role": "implementer", "task_id": args.task_id}))


def dispatch_command(root, args):
    command = list(args.argv)
    if command[:1] == ["--"]:
        command.pop(0)
    if not command:
        raise ValueError("dispatch needs a harness command after --")
    if Path(command[0]).name != args.harness:
        raise ValueError("dispatch command must directly invoke the selected harness")
    if args.harness == "codex":
        if command[1:2] != ["exec"] or command[-1:] != ["-"]:
            raise ValueError("use codex exec FLAGS -; the task file supplies stdin")
        if any(value in command[2:] for value in ("resume", "fork", "review", "--worktree", "--ignore-user-config")):
            raise ValueError("dispatch starts a fresh session in the specified worktree")
        # Preserve the PATH shim while preventing client/server environment separation.
        command.insert(1, "--no-daemon")
    elif not any(flag in command for flag in ("-p", "--print")):
        raise ValueError("use claude -p FLAGS with its prompt supplied on stdin")
    elif any(value in command for value in ("--resume", "-r", "--continue", "-c", "--fork-session",
                                           "--worktree", "-w", "--bare", "--safe-mode")):
        raise ValueError("dispatch requires a fresh Claude session with hooks enabled")
    for i, value in enumerate(command):
        if value == "--input-format" and (i + 1 == len(command) or command[i + 1] != "text"):
            raise ValueError("dispatch supplies a plain-text task on stdin")
        if value.startswith("--input-format=") and value != "--input-format=text":
            raise ValueError("dispatch supplies a plain-text task on stdin")
        if value in ("-C", "--cd"):
            if i + 1 >= len(command) or checkout(command[i + 1])[0] != root:
                raise ValueError("command working directory does not match --cwd")
            command[i + 1] = str(root)
        elif value.startswith("--cd="):
            if checkout(value.split("=", 1)[1])[0] != root:
                raise ValueError("command working directory does not match --cwd")
            command[i] = "--cd=" + str(root)
        elif value.startswith("-C") and value != "-C":
            raise ValueError("use a separate -C DIR argument so its worktree can be validated")
    executable = shutil.which(command[0])
    if not executable:
        raise ValueError("harness executable not found on PATH")
    return executable, command


def check_dispatch_hook(harness, explicit=None):
    data = read_settings(settings_path(harness, explicit))
    handlers = [handler for group in data.get("hooks", {}).get("SessionStart", [])
                for handler in group["hooks"] if owned(handler, harness)]
    if len(handlers) != 1:
        raise ValueError("dispatch requires exactly one installed Pattern Buffer hook")
    if harness == "codex" and handlers[0].get("additionalContextLimit") != 0:
        raise ValueError("reinstall the Codex hook to enable bounded, untruncated task replay")
    argv = shlex.split(handlers[0]["command"], comments=True)
    if len(argv) != 5 or argv[2:] != ["hook", "--harness", harness]:
        raise ValueError("installed hook command is not recognized; reinstall")
    hook_script = Path(argv[1])
    if not runtime_bundle_matches(hook_script, Path(__file__).resolve()):
        command = compatible_installed_command(harness, handlers[0]["command"])
        if command:
            raise ValueError("installed hook runtime differs from this CLI; dispatch remains refused. "
                             "Run this complete command with the verified compatible installed entrypoint: " + command)
        guidance = recovery_guidance(harness, explicit, installed_checkpoints_enabled(data, harness))
        raise ValueError("installed hook runtime differs from this CLI; dispatch remains refused. "
                         "No compatible installed entrypoint was verified in its runtime directory; "
                         + guidance)


def runtime_bundle_matches(left_script, right_script):
    left_script, right_script = Path(left_script), Path(right_script)
    try:
        left_paths = runtime_bundle_paths(left_script)
        right_paths = runtime_bundle_paths(right_script)
        for left, right in zip(left_paths, right_paths):
            if any(path.is_symlink() or not path.is_file() for path in (left, right)):
                return False
            if bounded(left, 256 * 1024) != bounded(right, 256 * 1024):
                return False
        return True
    except (OSError, ValueError, UnicodeError):
        return False


def compatible_installed_command(harness, hook_command):
    """Find a known installed alias matching the hook bundle without executing it."""
    try:
        hook_argv = shlex.split(hook_command, comments=True)
        if len(hook_argv) != 5 or hook_argv[2:] != ["hook", "--harness", harness]:
            return None
        hook_script = Path(hook_argv[1])
        if hook_script.name not in (f"context_injector_{harness}.py",
                                    f"pattern_buffer_{harness}.py", "pattern_buffer.py"):
            return None
        if any(parent.is_symlink() for parent in (hook_script, *hook_script.parents)):
            return None
        interpreter = shutil.which(hook_argv[0])
        if not interpreter or not os.access(interpreter, os.X_OK):
            return None
        runtime_dir = hook_script.parent
        for name in (f"pattern_buffer_{harness}.py", f"context_injector_{harness}.py"):
            candidate = runtime_dir / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            try:
                owned_runtime_core(candidate, harness)
            except (OSError, ValueError, UnicodeError):
                continue
            if runtime_bundle_matches(hook_script, candidate):
                return shlex.join([interpreter, str(candidate), *sys.argv[1:]])
    except (OSError, ValueError, TypeError):
        return None
    return None


def check_handoff_interpreter(harness, explicit=None):
    data = read_settings(settings_path(harness, explicit))
    handlers = [handler for group in data.get("hooks", {}).get("SessionStart", [])
                for handler in group["hooks"] if owned(handler, harness)]
    if len(handlers) != 1:
        raise ValueError("expected exactly one installed Pattern Buffer hook")
    argv = shlex.split(handlers[0]["command"], comments=True)
    interpreter = argv[0] if argv else ""
    executable = shutil.which(interpreter)
    if not executable or not os.access(executable, os.X_OK):
        raise ValueError(f"hook interpreter is not available and executable on the current PATH: {interpreter or '(empty)'}")
    return executable


def checkpoint_handoff_report(root, gitdir, harness, session, role, role_source=None):
    owner = {"worktree": str(root), "gitdir": str(gitdir), "harness": harness, "session": session}
    if role is None and role_source is None:
        command = shlex.join(["pattern-buffer", "--cwd", str(root), "checkpoint-status",
                              "--harness", harness, "--session", session])
        return {
            "state": "unknown", "required": False, "owner": owner,
            "ownership_verified": False,
            "detail": "The exact-session role could not be resolved, so checkpoint ownership cannot be determined.",
            "next_action": "Use this supported command from the exact owner session: " + command,
        }
    if role != "orchestrator":
        return {
            "state": "not_applicable", "required": False,
            "owner": {"worktree": str(root), "harness": harness, "session": session},
            "detail": "This session does not resolve to the orchestrator role, so no checkpoint is expected here.",
            "next_action": "No checkpoint action is needed for this role.",
        }
    helper = checkpoints()
    state_path = helper._state_path(gitdir, harness, session)
    if state_path.is_symlink():
        return {"state": "invalid", "required": False, "owner": owner,
                "detail": "The exact-session checkpoint metadata path is a symlink.",
                "next_action": "Inspect checkpoint-status from the exact owner and repair only through its supported commands."}
    if not state_path.exists():
        return {"state": "not_enrolled", "required": False, "owner": owner,
                "detail": "No checkpoint is enrolled for this exact session; checkpoint enrollment remains optional.",
                "next_action": "If this session already owns a checkpoint, inspect checkpoint-status from that session; otherwise no action is required."}
    try:
        _path, state = helper._load_state(sys.modules[__name__], root, gitdir, harness, session)
        report = helper._report(sys.modules[__name__], root, gitdir, harness, session, state)
        note, warning = helper._read_note(state, sys.modules[__name__])
        valid = note is not None and warning is None
        minutes = report["idle_warning_minutes"]
        timer = report.get("idle_timer")
        if harness != "claude":
            idle_warning = {"state": "not_applicable", "minutes": 0, "timer": timer,
                            "next_action": "No automatic idle warning is configured for Codex."}
        elif minutes:
            timer_state = timer.get("status") if isinstance(timer, dict) else None
            idle_warning = {
                "state": timer_state or "configured",
                "minutes": minutes,
                "timer": timer,
                "detail": "The existing Claude idle-warning configuration is shown for this exact owner; an armed timer is not proof of wake receipt.",
                "next_action": ("No idle-warning change is needed." if timer_state == "armed" else
                                "Inspect checkpoint-status for this session and temporal-beacon doctor for timer coverage."),
            }
        else:
            idle_warning = {
                "state": "disabled", "minutes": 0, "timer": timer,
                "detail": "This existing Claude checkpoint has no enabled idle warning.",
                "next_action": "If already authorized for this exact owner, enable 50 minutes with: " + shlex.join([
                    "pattern-buffer", "--cwd", str(root), "checkpoint-config", "--harness", "claude",
                    "--session", session, "--idle-warning-minutes", "50"]),
            }
        return {
            "state": "valid" if valid else "invalid",
            "required": False,
            "owner": owner,
            "ownership_verified": True,
            "checkpoint_path": report["checkpoint_path"],
            "enabled": report["enabled"],
            "reminders": report["reminders"],
            "reviewed_at": report["reviewed_at"],
            "saved_revision": report["saved_revision"],
            "revision_mismatch": report["revision_mismatch"],
            "idle_warning": idle_warning,
            "note_state": "valid" if valid else "invalid_or_unreviewed",
            "draft_pending": report["draft_pending"],
            "warnings": report["warnings"],
            "detail": ("Checkpoint metadata and the reviewed note match this exact session." if valid else
                       "Checkpoint metadata belongs to this exact session, but its reviewed note needs inspection."),
            "next_action": ("Use checkpoint-status from this exact session, review its warnings, then use checkpoint-save only for an explicitly reviewed update." if not valid else
                            idle_warning["next_action"] if harness == "claude" and idle_warning["state"] != "armed" else
                            "No checkpoint action is needed; keep its existing owner and note."),
        }
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        return {"state": "invalid", "required": False, "owner": owner,
                "ownership_verified": False, "detail": f"Checkpoint state could not be verified: {error}",
                "next_action": "Use checkpoint-status from the exact owner session and follow its supported recovery guidance."}


def handoff_report(root, gitdir, config_path, harness, session, explicit_settings=None):
    registered = config_path.exists()
    config = None
    source = root / ".context-injector/context.md"
    binding = None
    registration_error = None
    binding_error = None
    if registered:
        try:
            config, source = load(root, gitdir)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            registration_error = str(error)
        if config is not None:
            try:
                binding = session_binding_readonly(root, gitdir, harness, session)
                if binding is not None:
                    source = Path(binding.path)
            except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
                binding_error = str(error)
    else:
        registration_error = "No Pattern Buffer registration exists for this worktree."

    checks = {
        "registration": {
            "ok": config is not None,
            "detail": ("Registration is valid." if config is not None else
                       f"Registration is unavailable: {registration_error}"),
            "next_action": ("No registration change is needed." if config is not None else
                            "Inspect the target worktree and initialize it only if its approved context is ready."),
        },
    }
    if config is None:
        checks["enabled"] = {
            "ok": False,
            "detail": "Cannot resolve enabled state without a valid registration.",
            "next_action": "Resolve the registration finding before changing enabled state.",
        }
        resolved_role = {"role": None, "source": None}
        checks["resolved_role"] = {
            "ok": False,
            **resolved_role,
            "detail": f"Cannot resolve role: {registration_error}",
            "next_action": "Resolve the registration finding before changing a role.",
        }
        context_budgets = {
            role: {"ok": False, "detail": f"Cannot validate context: {registration_error}"}
            for role in ROLES
        }
        checks["context"] = {
            "ok": False,
            "detail": f"Context cannot be checked: {registration_error}",
            "budgets": context_budgets,
            "next_action": "Resolve the registration finding, then run validate and preview for this exact session.",
        }
    else:
        try:
            is_enabled = enabled(config)
            checks["enabled"] = {
                "ok": is_enabled,
                "detail": ("Worktree and context owner are enabled." if is_enabled else
                           "Worktree or its context owner is disabled."),
                "next_action": ("No enabled-state change is needed." if is_enabled else
                                "Enable this worktree only if its approved context is authorized for use."),
            }
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            checks["enabled"] = {"ok": False, "detail": f"Enabled state is invalid: {error}",
                                  "next_action": "Inspect the reported registration state before changing it."}

        try:
            if binding_error:
                raise ValueError("exact-session binding could not be read: " + binding_error)
            role, origin, _ = resolve_role(root, gitdir, config, harness, session, binding=binding)
            resolved_role = {"role": role, "source": origin}
            checks["resolved_role"] = {
                "ok": role == "orchestrator",
                **resolved_role,
                "detail": (f"Resolved role is orchestrator from {origin}." if role == "orchestrator" else
                           f"Resolved role is {role or 'unassigned'} from {origin}; expected orchestrator."),
                "next_action": ("No role change is needed." if role == "orchestrator" else
                                "Use the session's already-approved role through the supported assign or enrollment command."),
            }
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            resolved_role = {"role": None, "source": None}
            checks["resolved_role"] = {
                "ok": False,
                **resolved_role,
                "detail": f"Cannot resolve role: {error}",
                "next_action": "Inspect enrollment-status and the exact session assignment before changing roles.",
            }

        if binding_error:
            budget_report = {
                "ok": False, "source": None, "limit_bytes": LIMIT,
                "roles": {role: {"ok": False, "detail":
                                  "Exact-session binding could not be read: " + binding_error}
                          for role in ROLES},
            }
        else:
            budget_report = safe_context_budget_report(root, gitdir, harness, session, binding=binding)
        context_budgets = budget_report.get("roles", {})
        context_ok = all(result["ok"] for result in context_budgets.values())
        context_errors = [f"{role}: {result['detail']}" for role, result in context_budgets.items()
                          if not result["ok"]]
        checks["context"] = {
            "ok": context_ok,
            "detail": ("Both role sections are valid and fit their byte budgets." if context_ok else
                       "Context validation failed: " + "; ".join(context_errors)),
            "budgets": context_budgets,
            "next_action": ("No standing-context change is needed." if context_ok else
                            "Use validate and preview for this exact worktree and session; keep an invalid required source fail-closed."),
        }

    settings = settings_path(harness, explicit_settings)
    hook_report = {"state": "unknown", "evidence_available": False,
                   "detail": "No installed hook result is available from this local check.",
                   "next_action": "Inspect the selected harness settings and use its normal hook controls when convenient."}
    try:
        check_dispatch_hook(harness, explicit_settings)
        interpreter = check_handoff_interpreter(harness, explicit_settings)
        checks["hook_runtime"] = {
            "ok": True,
            "detail": "One owned hook matches this CLI, and its interpreter is executable on the current PATH.",
            "next_action": "No local runtime change is needed; inspect native loading and trust through the harness when convenient.",
        }
        hook_report = {"state": "compatible", "evidence_available": True,
                       "interpreter": interpreter,
                       "detail": "One owned hook runtime and its installed support files match this CLI; native loading and trust remain unknown.",
                       "next_action": "No local runtime change is needed; inspect native loading and trust through the harness when convenient."}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        message = str(error)
        suggestion_marker = "Run this complete command with the verified compatible installed entrypoint: "
        if suggestion_marker in message:
            next_action = message.split(suggestion_marker, 1)[1]
            hook_state = "compatible_alternate_cli"
        elif RECOVERY_COMMAND_MARKER in message:
            guidance_index = message.find(RECOVERY_GUIDANCE_PREFIX)
            next_action = message[guidance_index:] if guidance_index >= 0 else message
            hook_state = "problem"
        else:
            next_action = ("Review the selected hook/runtime details and repair only through the supported installer. "
                           "If it refuses an unowned or damaged entrypoint, preserve that runtime and install into a "
                           "new empty runtime using the same settings file. This check does not change settings or "
                           "trust; delivery remains unverified.")
            hook_state = "problem"
        hook_report = {"state": hook_state, "evidence_available": True,
                       "detail": message, "next_action": next_action}
        checks["hook_runtime"] = {"ok": False, "detail": f"Local hook/runtime preflight failed: {error}",
                                  "next_action": next_action}

    required_reads_report = None
    try:
        required_reads_helper = auxiliary("required_reads")
        required_reads_report = required_reads_helper.status(
            sys.modules[__name__], root, gitdir, harness, session, resolved_role.get("role"))
        if required_reads_report is not None:
            required_reads_ok = required_reads_report.get("ok", False)
            checks["required_reads"] = {
                "ok": required_reads_ok,
                "detail": ("Required-read list and applicable files are valid." if required_reads_ok else
                           "Required-read list or an applicable file failed its integrity check."),
                "next_action": ("No required-read integrity repair is needed." if required_reads_ok else
                                "Ask the worktree owner to repair and re-register the reviewed list or restore its required files."),
            }
    except (OSError, ValueError, TypeError, UnicodeError, KeyError, ImportError) as error:
        checks["required_reads"] = {
            "ok": False,
            "detail": f"Required-read status is unavailable: {error}",
            "next_action": "Ask the worktree owner to inspect the required-read registration and files.",
        }
    configuration_ready = all(check["ok"] for check in checks.values())
    checkpoint = checkpoint_handoff_report(root, gitdir, harness, session, resolved_role.get("role"),
                                           resolved_role.get("source"))
    native_binding = {
        "state": "unknown", "evidence_available": False,
        "detail": "A command-line session ID and settings file do not prove the live native session, worktree, hook loading or trust.",
        "next_action": "In the intended session, confirm its exact identity and hook loading through the harness's normal controls.",
    }
    delivery = {
        "state": "unknown", "evidence_available": False,
        "detail": "This read-only check has no model-visible receipt or in-session confirmation to inspect.",
        "next_action": "Use the existing reliability report for retained local receipt events, then confirm actual reception through ordinary in-session observation.",
    }
    delivery_note = "Model reception is not recorded by this check; configuration and emitted hook output do not prove it."
    result = {
        "target_worktree": str(root),
        "harness": harness,
        "session": session,
        "registered": registered,
        "resolved_role": resolved_role,
        "settings_file": str(settings),
        "checks": checks,
        "configuration_ready": configuration_ready,
        "standing_context": {
            "state": "valid" if checks["context"]["ok"] else "invalid_or_unknown",
            "source": str(source),
            "role": resolved_role,
            "enrollment_binding": ({"state": binding.state, "role": binding.role,
                                    "source": binding.path, "ownership": "exact-session"}
                                   if binding is not None else
                                   {"state": "unknown", "detail": binding_error} if binding_error else
                                   {"state": "not_enrolled", "detail":
                                    "No exact-session standing-context enrollment was found."}),
            "detail": checks["context"]["detail"],
            "next_action": ("No standing-context change is needed." if checks["context"]["ok"] else
                            "Use validate and preview for this exact worktree and session; keep an invalid required source fail-closed."),
            "budgets": context_budgets,
        },
        "reference_warnings": reference_warnings_for_path(source, root),
        "checkpoint": checkpoint,
        "installed_hooks": hook_report,
        "native_binding": native_binding,
        "delivery_state": "not_tracked",
        "delivery_note": delivery_note,
        "delivery_evidence": delivery,
        "notes": [
            "The actual harness cwd must match target_worktree.",
            "Actual model reception is still a manual observation; this command cannot probe it.",
            "Hook trust and runtime delivery remain unverified.",
            "This check is read-only and does not enroll, assign, install, repair, or dispatch.",
        ],
    }
    if required_reads_report is not None:
        result["required_reads"] = {
            "state": "valid" if required_reads_report.get("ok") else "integrity_error",
            "generation": required_reads_report.get("generation"),
            "pending_read_count": required_reads_report.get("pending_read_count"),
            "next_command": required_reads_report.get("next_command"),
            "integrity_errors": required_reads_report.get("integrity_errors", []),
            "receipt_limit": "A local receipt records emitted bytes for this generation; it does not prove model reception or comprehension.",
        }
    return result


def render_failure_kind(error):
    if isinstance(error, OSError):
        return "unreadable"
    message = str(error).lower()
    if "expected a regular, non-symlink file" in message:
        return "unreadable"
    if "file exceeds" in message or "source exceeds" in message:
        return "file_limit"
    if "missing section for assigned role" in message:
        return "missing_role_section"
    if "24 kib" in message or "injection limit" in message:
        return "combined_limit"
    if "context uses " in message and "8192 bytes" in message:
        return "standing_limit"
    if isinstance(error, (ValueError, UnicodeError)):
        return "malformed"
    return "other"


def failure_notice(source, failure_kind):
    reason = _RENDER_FAILURES.get(failure_kind, _RENDER_FAILURES["other"])
    return (f"Pattern Buffer could not deliver the complete standing context. {reason}\n"
            f"Source file: {source}\n"
            "Read the complete source file now with your normal file tool and notify its owner before continuing.\n"
            "Preserve unresolved scope and holds. This notice is not the full context and does not expand authorization.")


def over_budget_warning(over_budget_bytes):
    byte_word = "byte" if over_budget_bytes == 1 else "bytes"
    return ("Pattern Buffer delivered the complete standing context above. "
            f"The rendered role view exceeded its 8192-byte budget by {over_budget_bytes} {byte_word}. "
            "Notify the context owner. This warning does not expand authorization.")


def finalize_rendered_context(rendered, addendum=""):
    if rendered.failure_kind:
        return rendered
    text = rendered.text
    if addendum:
        text += "\n\n" + addendum
    if rendered.over_budget_bytes:
        text += "\n\n" + over_budget_warning(rendered.over_budget_bytes)
    output_bytes = len(text.encode("utf-8"))
    if output_bytes > INJECTION_LIMIT:
        if rendered.runtime_policy == ORDINARY_WORKTREE_RENDER_POLICY:
            kind = "combined_limit"
            return RenderedContext(failure_notice(rendered.source, kind), rendered.source,
                                   rendered.runtime_policy, failure_kind=kind)
        raise ValueError("combined reminder, checkpoint pointer and task exceed the 24 KiB injection limit")
    if (rendered.over_budget_bytes and output_bytes > RECOVERY_OUTPUT_LIMIT and
            rendered.runtime_policy == ORDINARY_WORKTREE_RENDER_POLICY):
        kind = "runtime_output_limit"
        return RenderedContext(failure_notice(rendered.source, kind), rendered.source,
                               rendered.runtime_policy, failure_kind=kind)
    return RenderedContext(text, rendered.source, rendered.runtime_policy,
                           rendered.over_budget_bytes)


def render(cwd, harness, session, agent="", forced_role=None, *,
           runtime_policy=STRICT_RENDER_POLICY):
    if runtime_policy not in (STRICT_RENDER_POLICY, ORDINARY_WORKTREE_RENDER_POLICY):
        raise ValueError("invalid Pattern Buffer render policy")
    root, gitdir = checkout(cwd)
    config, source = load(root, gitdir)
    enrolled = session_binding(root, gitdir, harness, session, agent)
    if enrolled is not None:
        if enrolled.state != "active":
            raise RequiredContextFailure("This enrollment binding is " + enrolled.state,
                                         enrolled.path, enrolled.enrollment_id)
        source = Path(enrolled.path)
    role, origin, saved = resolve_role(root, gitdir, config, harness, session, agent, forced_role)
    if (runtime_policy == ORDINARY_WORKTREE_RENDER_POLICY and
            (enrolled is not None or config.get("context_owner") or saved.get("task_id") or
             os.environ.get(DISPATCH_ENV))):
        runtime_policy = STRICT_RENDER_POLICY
    try:
        raw = bounded(source, FILE_LIMIT)
        if enrolled is not None and (enrolled.file_status != "valid" or
                hashlib.sha256(raw.encode()).hexdigest() != enrolled.source_sha256):
            raise ValueError("registered context file is missing, unsafe, or changed outside reviewed publication")
        content = sections_text(raw)
        text, budget = render_standing_details(root, source, harness, session, agent, role, origin, content)
        over_budget_bytes = max(0, budget["bytes_used"] - LIMIT)
        if over_budget_bytes and runtime_policy == STRICT_RENDER_POLICY:
            raise ValueError(standing_budget_message(role, budget))
    except (OSError, ValueError, UnicodeError) as error:
        if enrolled is not None:
            raise RequiredContextFailure(str(error), source, enrolled.enrollment_id) from error
        if runtime_policy == ORDINARY_WORKTREE_RENDER_POLICY:
            failure_kind = render_failure_kind(error)
            return RenderedContext(failure_notice(source, failure_kind), source, runtime_policy,
                                   failure_kind=failure_kind)
        raise
    try:
        if saved.get("task_id"):
            if role != "implementer" or agent:
                raise ValueError("task bindings require a primary implementer session")
            task_id = saved["task_id"]
            data, brief = read_task(root, gitdir, task_id)
            expected_owner = config.get("context_owner") or {"root": str(root), "gitdir": str(gitdir)}
            if data["owner"] != expected_owner or data["session_id"] != session or data["harness"] != harness:
                raise ValueError("task/session/context-owner binding changed")
            text = compose_task_context(text, task_id, data, brief,
                                        has_confirmation=bool(content.get("confirmation")))
        else:
            text = compose_task_context(text, has_confirmation=bool(content.get("confirmation")))
    except ValueError as error:
        failure_kind = render_failure_kind(error)
        if (runtime_policy == ORDINARY_WORKTREE_RENDER_POLICY and
                failure_kind == "combined_limit"):
            return RenderedContext(failure_notice(source, failure_kind), source, runtime_policy,
                                   failure_kind=failure_kind)
        raise
    return RenderedContext(text, source, runtime_policy, over_budget_bytes)


def compose_task_context(text, task_id=None, task_data=None, brief=None, has_confirmation=False):
    guidance = ("Use the installed PStack runtime and Poteto Mode. "
                "After compaction, read the sections relevant to current work in full.")
    text += "\n\n" + guidance
    if not has_confirmation:
        text += "\n\n" + RESTATEMENT
    if task_id is not None:
        if task_data["status"] == "finished":
            text += f"\n\nTask {task_id} is finished. No task is restored; await a new assignment."
        else:
            text += (f"\n\n# Active task {task_id}\nTask brief SHA-256: {task_data['brief_sha256']}\n"
                     "Repeat of the dispatched assignment, subject to later user direction; this does not reset progress.\n\n" + brief)
    if len(text.encode("utf-8")) > INJECTION_LIMIT:
        raise ValueError("combined reminder and task exceed the 24 KiB injection limit")
    return text


def opencode_context(root, gitdir, session):
    if not isinstance(session, str) or not _OPENCODE_SESSION_ID.fullmatch(session):
        raise ValueError("OpenCode context requires the exact case-sensitive ses_ session ID")
    config_path = gitdir / "context-injector" / "config.json"
    if not config_path.exists() and not config_path.is_symlink():
        return {"status": "unregistered", "harness": "opencode", "session_id": session,
                "worktree": str(root)}
    config, _ = load(root, gitdir)
    if not enabled(config):
        return {"status": "disabled", "harness": "opencode", "session_id": session,
                "worktree": str(root)}
    if config.get("context_owner"):
        binding = state_path(gitdir, "opencode", session)
        if binding.is_symlink() or not binding.is_file():
            raise ValueError("linked OpenCode worktree requires an explicit active task/session binding")
        saved = json.loads(bounded(binding, FILE_LIMIT))
        if (not isinstance(saved, dict) or saved.get("root") != str(root)
                or saved.get("role") != "implementer" or not saved.get("task_id")):
            raise ValueError("linked OpenCode worktree session is not bound to its prepared implementer task")
    text = render(root, "opencode", session).text
    checkpoint = checkpoints().opencode_recovery_context(sys.modules[__name__], root, gitdir, session)
    if checkpoint:
        text += "\n\n" + checkpoint
        if len(text.encode()) > INJECTION_LIMIT:
            raise ValueError("standing context and checkpoint recovery guidance exceed the 24 KiB injection limit")
    assignment = state_path(gitdir, "opencode", session)
    assignment_sha256 = (hashlib.sha256(bounded(assignment, FILE_LIMIT).encode("utf-8")).hexdigest()
                         if assignment.exists() or assignment.is_symlink() else None)
    pressure = checkpoints().opencode_pressure_status(sys.modules[__name__], root, gitdir, session)
    return {"status": "context", "harness": "opencode", "session_id": session,
            "worktree": str(root), "text": text, "assignment_sha256": assignment_sha256,
            "pressure_status": pressure["status"], "pressure_reason": pressure["reason"],
            "pressure_observation_required": pressure["observation_required"]}


def standing_parts(root, source, harness, session, agent, role, origin, content):
    if role and role not in content:
        raise ValueError(f"missing section for assigned role: {role}")
    selected = content["shared"]
    if role:
        selected += "\n\n" + content[role]
    if content.get("confirmation"):
        selected += "\n\n" + content["confirmation"]
    header = (f"pattern-buffer | role: {role or 'unassigned'} | source: {origin}\n"
              f"Session: {harness}/{session}" + (f" | agent: {agent}" if agent else "") + "\n"
              f"Worktree: {root}\nContext file: {source}\n"
              "These reminders do not expand authorization. Follow current user direction.\n")
    if not role:
        header += ("Role is unassigned; no role-specific section is included.\n"
                   if content.get("confirmation") else
                   "Role is unassigned; only shared context is included.\n")
    return header + "\n", selected


def standing_budget(header, content):
    header_bytes = len(header.encode("utf-8"))
    content_bytes = len(content.encode("utf-8"))
    used = header_bytes + content_bytes
    remaining = LIMIT - used
    return {
        "header_bytes": header_bytes,
        "content_bytes": content_bytes,
        "usable_content_bytes": max(0, LIMIT - header_bytes),
        "content_remaining_bytes": remaining,
        "bytes_used": used,
        "limit_bytes": LIMIT,
        "remaining_bytes": remaining,
        "ok": remaining >= 0,
    }


def render_standing_details(root, source, harness, session, agent, role, origin, content):
    header, selected = standing_parts(root, source, harness, session, agent, role, origin, content)
    return header + selected, standing_budget(header, selected)


def standing_context_budgets(root, source, harness, session, agent, origin, content,
                             include_unassigned=False):
    roles = {}
    selected_roles = ROLES + ((None,) if include_unassigned else ())
    for role in selected_roles:
        label = role or "unassigned"
        try:
            _text, budget = render_standing_details(root, source, harness, session, agent, role, origin, content)
            budget["detail"] = (
                f"Header {budget['header_bytes']} bytes; content {budget['content_bytes']} / "
                f"{budget['usable_content_bytes']} usable bytes; {budget['remaining_bytes']} bytes remaining."
            )
            roles[label] = budget
        except (OSError, ValueError, TypeError, UnicodeError) as error:
            roles[label] = {"ok": False, "detail": str(error)}
    return {"limit_bytes": LIMIT, "source": str(source), "roles": roles}


def render_standing(root, source, harness, session, agent, role, origin, content):
    text, budget = render_standing_details(root, source, harness, session, agent, role, origin, content)
    if not budget["ok"]:
        raise ValueError(standing_budget_message(role, budget))
    return text


def standing_budget_message(role, budget):
    return (f"{role or 'unassigned'} context uses {budget['bytes_used']} / {LIMIT} bytes "
            f"(header {budget['header_bytes']}; content {budget['content_bytes']} / "
            f"{budget['usable_content_bytes']} usable bytes; "
            f"{abs(budget['remaining_bytes'])} over); shorten the file")


def context_validator(root, harness, session):
    def validate(text, destination, role):
        if len(text.encode()) > FILE_LIMIT:
            raise ValueError(f"standing context source exceeds {FILE_LIMIT} bytes")
        content = sections_text(text)
        report = standing_context_budgets(root, destination, harness, session, "",
                                          "session enrollment", content)
        validate.last_report = report
        errors = [f"{selected}: {budget['detail']}" for selected, budget in report["roles"].items()
                  if not budget["ok"]]
        if errors:
            raise ValueError("standing context validation failed: " + "; ".join(errors))
        return {"valid": True, "budgets": report}
    validate.last_report = None
    return validate


def ensure_session_ignore(root, gitdir):
    relative = ".context-injector/sessions"
    if git(root, "ls-files", "--", relative):
        raise ValueError("session context directory is tracked; move tracked files before enrolling")
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    exclude = common / "info" / "exclude"
    lock = exclude.with_name("exclude.context-injector.lock")
    helper = checkpoints()
    helper._ensure_directory_chain(exclude.parent)
    helper._check_path(exclude, missing_ok=True)
    helper._check_path(lock, missing_ok=True)
    with locked(lock):
        existing = bounded(exclude, 1024 * 1024) if exclude.exists() else ""
        rule = "/.context-injector/sessions/"
        if rule not in existing.splitlines():
            atomic(exclude, existing + ("" if not existing or existing.endswith("\n") else "\n") + rule + "\n")


def binding_report(binding):
    return asdict(binding) if binding is not None else None


def safe_context_budget_report(root, gitdir, harness, session, agent="", binding=_BINDING_UNSET):
    try:
        return current_context_budgets(root, gitdir, harness, session, agent, binding)
    except (OSError, ValueError, TypeError, UnicodeError, subprocess.SubprocessError) as error:
        return {
            "ok": False, "worktree": str(root), "harness": harness, "session": session,
            "agent": agent or None, "source": str(binding.path) if binding is not _BINDING_UNSET and binding else None,
            "limit_bytes": LIMIT, "detail": str(error),
            "roles": {role: {"ok": False, "detail": str(error)} for role in ROLES},
        }


def require_own_session(harness, session):
    variable = "CLAUDE_CODE_SESSION_ID" if harness == "claude" else "CODEX_THREAD_ID"
    if os.environ.get(variable, "").lower() != session.lower():
        raise ValueError("Run this ownership operation from the exact outgoing or incoming native session")
    kind = os.environ.get("CMUX_AGENT_LAUNCH_KIND", "").lower()
    if kind not in ({"claude", "claudeteams"} if harness == "claude" else {"codex"}):
        raise ValueError("Native caller harness is unavailable or does not match")


def clear_owned_idle_timers(root, helper):
    """Reconcile only this native owner's Pattern Buffer idle timers, including orphaned receipts."""
    launcher = Path.home() / ".local/bin/hailing-frequencies"
    command = [str(launcher), "temporal", "list"]
    code, output, timeout, oversized = helper._bounded_attention_process(command, b"", output_limit=256 * 1024)
    if code or timeout or oversized:
        raise ValueError("cannot confirm outgoing idle timers; inspect temporal-beacon list from this exact owner")
    report = json.loads(output)
    if not isinstance(report, dict) or not isinstance(report.get("timers"), list):
        raise ValueError("outgoing timer listing was not confirmed")
    for timer in report["timers"]:
        if (timer.get("replacement_key") != "pattern-buffer-checkpoint-idle" or
                timer.get("canonical_worktree") != str(root) or timer.get("status") != "active"):
            continue
        code, output, timeout, oversized = helper._bounded_attention_process(
            [str(launcher), "temporal", "cancel", timer["id"]], b"")
        if code or timeout or oversized or json.loads(output).get("status") not in ("canceled", "completed", "expired"):
            raise ValueError("outgoing idle-timer cancellation remains unresolved; no ownership transfer was made")


def session_command(root, gitdir, args):
    store = auxiliary("sessions")
    _config, context_source = load(root, gitdir)
    if args.command == "enrollment-status":
        binding = store.status(root, gitdir, args.harness, args.session)
        report = binding_report(binding) or {"enrollment": None}
        report["standing_context_budgets"] = safe_context_budget_report(
            root, gitdir, args.harness, args.session, binding=binding)
        report["reference_warnings"] = reference_warnings_for_path(
            binding.path if binding is not None else context_source, root)
        return report
    if args.command in ("session-enroll", "assign"):
        if getattr(args, "agent", ""):
            raise ValueError("session context enrollment requires a primary session")
        old_path = state_path(gitdir, args.harness, args.session)
        if old_path.exists() and json.loads(bounded(old_path, FILE_LIMIT)).get("task_id"):
            raise ValueError("a dispatched task cannot be replaced by a session enrollment")
        ensure_session_ignore(root, gitdir)
        validator = context_validator(root, args.harness, args.session)
        binding = store.enroll(root, gitdir, args.harness, args.session,
                               args.context_file, args.role, validator,
                               reviewed_update=args.reviewed_update, evidence=args.evidence)
    elif args.command == "enrollment-release":
        require_own_session(args.harness, args.session)
        previous = store.lookup(root, gitdir, args.harness, args.session)
        if previous is None or previous.enrollment_id != args.enrollment_id:
            raise ValueError("outgoing enrollment does not match this exact session")
        helper = checkpoints()
        state_path_ = helper._state_path(gitdir, args.harness, args.session)
        if state_path_.exists() and previous.state == "active":
            from types import SimpleNamespace
            _, prior = helper._load_state(sys.modules[__name__], root, gitdir, args.harness, args.session)
            result = helper.handle_cli(sys.modules[__name__], root, gitdir, SimpleNamespace(
                command="checkpoint-config", harness=args.harness, session=args.session,
                reminders="off", enabled="off", idle_warning_minutes=0 if args.harness == "claude" else None,
                compact_at=None, lead_points=None))
            timer = result.get("idle_timer") or {}
            if timer.get("status") in ("armed", "uncertain", "cancel_unconfirmed"):
                raise ValueError("outgoing checkpoint disabled, but idle-timer cancellation is unresolved; preserve its identity")
            if args.harness == "claude" and (prior.get("idle_warning_minutes") or prior.get("idle_timer")):
                clear_owned_idle_timers(root, helper)
        binding = store.release(root, gitdir, args.enrollment_id, args.harness, args.session,
                                args.next_harness, args.next_session, args.evidence, timers_safe=True)
    elif args.command == "enrollment-adopt":
        require_own_session(args.harness, args.session)
        helper = checkpoints()
        # Review is explicit; do not silently adopt an old owner's checkpoint state or authority.
        if args.role == "orchestrator":
            if not args.checkpoint_file:
                raise ValueError("orchestrator adoption requires a fresh reviewed --checkpoint-file")
            text = helper._note_body(bounded(Path(args.checkpoint_file).expanduser().absolute(), helper.NOTE_LIMIT))
            helper._validate_note(text)
        ensure_session_ignore(root, gitdir)
        validator = context_validator(root, args.harness, args.session)
        binding = store.adopt(root, gitdir, args.enrollment_id, args.harness, args.session,
                              args.context_file, args.role, args.evidence,
                              validator)
        # Successor checkpoints are initialized through their normal exact-owner enrollment,
        # not by copying the predecessor's private metadata or timers.
        record_event(root, "enrollment." + args.command, args.harness, args.session,
                     binding.enrollment_id, binding.state)
        return {"enrollment": binding_report(binding),
                "standing_context_budgets": validator.last_report,
                "reviewed_checkpoint_file": args.checkpoint_file,
                "checkpoint_setup_required": args.role == "orchestrator",
                "next_command": (shlex.join(["pattern-buffer", "--cwd", str(root), "checkpoint-enroll",
                    "--harness", args.harness, "--session", args.session, "--file", args.checkpoint_file])
                    if args.role == "orchestrator" else shlex.join(["pattern-buffer", "--cwd", str(root),
                        "preview", "--harness", args.harness, "--session", args.session])),
                "authority_note": "Adoption preserves existing authority only; reception is unverified."}
    record_event(root, "enrollment." + args.command, args.harness, args.session,
                 binding.enrollment_id, binding.state)
    report = binding_report(binding)
    if args.command in ("session-enroll", "assign"):
        report["standing_context_budgets"] = validator.last_report
    if args.command == "session-enroll":
        report["reference_warnings"] = reference_warnings_for_path(args.context_file, root)
    return report


def required_context_alarm(root, gitdir, harness, session, error):
    message = ("STOP: This exact session has no active, valid standing-context binding. "
               "Pause product work and all other substantive work. Preserve running jobs, human holds, and evidence. "
               "Do not use memory, another session's file, the worktree default, or inferred authority.\n"
               "Use enrollment-status for this exact harness and session to diagnose the binding. "
               "You may perform only already-authorized supported enrollment recovery needed to restore this exact binding. "
               "If status confirms this session is the named pending successor of an approved release, use enrollment-adopt "
               "with that existing enrollment ID, the already-reviewed successor context, and the release evidence. "
               "Then follow any required fresh checkpoint setup command and validate the binding. "
               "For an active binding with a changed or invalid file, use session-enroll --reviewed-update only with an "
               "already user-approved source and evidence. These steps restore the binding only. Do no product work "
               "until validation succeeds, and do not create a new mission or authority.\n"
               "For any unexpected missing or invalid state, missing approval or reviewed input, or failed recovery, "
               "notify the context owner through the configured contact workflow.\n"
               f"Session: {harness}/{session}\nExpected file: {error.path}\nFailure: {str(error)[:512]}")
    key = hashlib.sha256(json.dumps([harness, session]).encode()).hexdigest()
    path = gitdir / "context-injector" / "context-alarms" / (key + ".json")
    event_key = None
    try:
        checkpoints()._ensure_directory_chain(path.parent)
        checkpoints()._check_path(path, missing_ok=True)
        checkpoints()._check_path(path.with_suffix(".lock"), missing_ok=True)
        with locked(path.with_suffix(".lock")):
            saved = json.loads(bounded(path, FILE_LIMIT)) if path.exists() else {}
            if not saved.get("active"):
                event_key = str(uuid.uuid4())
                # Durable intent precedes external submission; an interrupted attempt is never replayed.
                atomic(path, json.dumps({"active": True, "event_key": event_key,
                                         "outcome": "pending", "expected_file": str(error.path)}))
                record_event(root, "context.missing_alarm", harness, session, error.enrollment_id,
                             "emitted", event_key, {"error_code": "required_context_unavailable"})
                result = checkpoints()._attention_once(harness, session, event_key, message)
                atomic(path, json.dumps({"active": True, "event_key": event_key,
                                         "outcome": result, "expected_file": str(error.path)}))
                record_event(root, "delivery." + {
                    "submitted": "submitted", "held": "held", "uncertain": "uncertain"
                }.get(result.get("outcome"), "refused"), harness, session, error.enrollment_id,
                             result.get("outcome"), event_key)
    except Exception as alarm_error:
        # No durable intent means no stronger attempt. The hook alarm still reaches stdout.
        print("pattern-buffer context alarm attention unavailable: " + type(alarm_error).__name__, file=sys.stderr)
    return message


def check_required_context(root, gitdir, harness, session, agent=""):
    if agent or not session:
        return None
    try:
        binding = session_binding(root, gitdir, harness, session)
        if binding is None:
            return None
        render(root, harness, session)
    except (OSError, ValueError, UnicodeError) as error:
        if not isinstance(error, RequiredContextFailure):
            error = RequiredContextFailure(str(error), root / ".context-injector" / "sessions")
        return required_context_alarm(root, gitdir, harness, session, error)
    key = hashlib.sha256(json.dumps([harness, session]).encode()).hexdigest()
    path = gitdir / "context-injector" / "context-alarms" / (key + ".json")
    if path.exists():
        try:
            with locked(path.with_suffix(".lock")):
                previous = json.loads(bounded(path, FILE_LIMIT))
                if previous.get("active"):
                    atomic(path, json.dumps({**previous, "active": False}))
                    record_event(root, "context.recovered", harness, session, binding.enrollment_id,
                                 "validated", previous.get("event_key"))
        except (OSError, ValueError):
            print("pattern-buffer could not record context-alarm recovery", file=sys.stderr)
    return None


def hook(harness, checkpoints_enabled=True):
    raw = sys.stdin.buffer.read(HOOK_INPUT_LIMIT + 1)
    if len(raw) > HOOK_INPUT_LIMIT:
        raise ValueError("hook input too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("hook input must be an object")
    event = payload.get("hook_event_name")
    if event not in ("SessionStart", "PreCompact", "PostToolUse", "UserPromptSubmit", "PreToolUse"):
        return
    if event == "SessionStart" and payload.get("source") not in ("startup", "resume", "compact", "fork", "clear"):
        return
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise ValueError("hook cwd must be an absolute path")
    try:
        root, gitdir = checkout(cwd)
    except ValueError:
        if event != "SessionStart":
            return  # Standard pressure hooks are inert outside Git worktrees too.
        raise
    config_path = gitdir / "context-injector" / "config.json"
    required_reads = auxiliary("required_reads")
    if event == "PreToolUse":
        static_decision = required_reads.pretool_gate(sys.modules[__name__], root, gitdir, harness, payload)
        static_output = (static_decision.get("hookSpecificOutput", {})
                         if isinstance(static_decision, dict) else {})
        static_denied = static_output.get("permissionDecision") == "deny"
        recovery_decision = (checkpoints().recovery_pretool_gate(
            sys.modules[__name__], root, gitdir, harness, payload)
            if checkpoints_enabled and not static_denied else None)
        decision = recovery_decision or static_decision
        if recovery_decision and static_decision:
            reason = recovery_decision.get("hookSpecificOutput", {}).get("permissionDecisionReason")
            advisory = static_output.get("additionalContext") or static_decision.get("systemMessage")
            if isinstance(reason, str) and isinstance(advisory, str) and advisory:
                recovery_decision["hookSpecificOutput"]["permissionDecisionReason"] = reason + " " + advisory
        if decision:
            print(json.dumps(decision, ensure_ascii=False), flush=True)
        return
    if event == "PostToolUse":
        required_reads.posttool_finalize(sys.modules[__name__], root, gitdir, harness, payload)
        if not checkpoints_enabled:
            return
        checkpoints().recovery_posttool_finalize(sys.modules[__name__], root, gitdir, harness, payload)
    if event == "SessionStart" and not payload.get("agent_id"):
        required_reads.session_start(sys.modules[__name__], root, gitdir, harness,
                                     payload.get("session_id"), payload.get("source"))
    if not config_path.exists():
        return  # This checkout has not opted in.
    config, _ = load(root, gitdir)
    if enabled(config):
        alarm = check_required_context(root, gitdir, harness, payload.get("session_id"),
                                       payload.get("agent_id") or "")
        if alarm:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                                       "additionalContext": alarm}}, ensure_ascii=False), flush=True)
            return
    if event != "SessionStart":
        extra = checkpoint_event(root, gitdir, harness, payload)
        if extra:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                                      "additionalContext": extra}}, ensure_ascii=False))
        return
    token = os.environ.get(DISPATCH_ENV)
    binding = state_path(gitdir, harness, payload.get("session_id"), payload.get("agent_id") or "")
    saved = json.loads(bounded(binding, FILE_LIMIT)) if binding.exists() else {}
    dispatched = bool(token or isinstance(saved, dict) and saved.get("task_id"))
    try:
        config, _ = load(root, gitdir)
        if not enabled(config):
            notice = required_reads.session_start_notice(
                sys.modules[__name__], root, gitdir, harness,
                payload.get("session_id"), payload.get("agent_id") or "")
            if notice:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                           "additionalContext": notice}},
                                 ensure_ascii=False), flush=True)
            return
        if token and payload.get("source") == "startup":
            if payload.get("agent_id"):
                raise ValueError("dispatch cannot enroll a nested agent")
            bind_dispatch(root, gitdir, harness, payload.get("session_id"), token)
        runtime_policy = (STRICT_RENDER_POLICY if dispatched else
                          ORDINARY_WORKTREE_RENDER_POLICY)
        rendered = render(root, harness, payload.get("session_id"), payload.get("agent_id") or "",
                          runtime_policy=runtime_policy)
        if rendered.failure_kind:
            output = rendered
        else:
            extra = checkpoint_event(root, gitdir, harness, payload)
            notice = required_reads.session_start_notice(
                sys.modules[__name__], root, gitdir, harness,
                payload.get("session_id"), payload.get("agent_id") or "")
            if notice:
                extra = "\n\n".join(part for part in (extra, notice) if part)
            output = finalize_rendered_context(rendered, extra)
        text = output.text
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        record_event(root, "standing.injection_failed", harness, payload.get("session_id"),
                     result="failed", details={"error_code": type(error).__name__,
                                               "failure_kind": render_failure_kind(error)})
        if dispatched:
            raise DispatchFailure(str(error)) from error
        raise
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                              "additionalContext": text}}, ensure_ascii=False), flush=True)
    binding = session_binding(root, gitdir, harness, payload.get("session_id"), payload.get("agent_id") or "")
    if output.failure_kind:
        record_event(root, "standing.injection_failed", harness, payload.get("session_id"),
                     binding.enrollment_id if binding else None, "failed",
                     details={"failure_kind": output.failure_kind})
        return
    details = {
        "trigger": payload.get("source"), "bytes": len(text.encode()),
        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source": "session" if binding else "default",
    }
    if output.over_budget_bytes:
        details["over_budget_bytes"] = output.over_budget_bytes
    record_event(root, "standing.injection_emitted", harness, payload.get("session_id"),
                 binding.enrollment_id if binding else None, "emitted", details=details)


def checkpoint_event(root, gitdir, harness, payload):
    if not (gitdir / "context-injector/checkpoints").exists():
        return ""
    try:
        return checkpoints().event(sys.modules[__name__], root, gitdir, harness, payload)
    except (OSError, ValueError, TypeError, KeyError, ImportError, subprocess.SubprocessError) as error:
        # Checkpoint failure must not suppress the standing reminder or task replay.
        print(f"pattern-buffer checkpoint: {error}", file=sys.stderr)
        if payload.get("hook_event_name") == "SessionStart":
            return "Checkpoint unavailable. Reconstruct current state from live evidence; preserve existing jobs and human holds."
        return ""


def required_reads_command(root, gitdir, args):
    helper = auxiliary("required_reads")
    if args.required_reads_action in ("set", "clear", "override"):
        load(root, gitdir)
    if args.required_reads_action == "set":
        result = helper.set_list(sys.modules[__name__], root, gitdir, args.file)
    elif args.required_reads_action == "clear":
        result = helper.clear_list(sys.modules[__name__], root, gitdir)
    elif args.required_reads_action == "override":
        session = helper.resolve_session(args.harness, args.session)
        result = helper.override(sys.modules[__name__], root, gitdir, args.harness, session,
                                 args.reason, args.minutes)
    else:
        session = helper.resolve_session(args.harness, args.session)
        try:
            required = helper.load_list(gitdir)
        except helper.IntegrityError as error:
            result = helper.status(sys.modules[__name__], root, gitdir, args.harness, session, None,
                                   registration_error=error)
        else:
            if required is None:
                result = {"registered": False}
            else:
                config, _source = load(root, gitdir)
                role, _origin, _saved = resolve_role(root, gitdir, config, args.harness, session)
                result = helper.status(sys.modules[__name__], root, gitdir, args.harness, session, role,
                                       required_list=required)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def read_required_command(root, gitdir, args):
    helper = auxiliary("required_reads")
    config, _source = load(root, gitdir)
    session = helper.resolve_session(args.harness, args.session)
    role, _origin, _saved = resolve_role(root, gitdir, config, args.harness, session)
    if args.next_entry and args.part is not None:
        raise ValueError("--part applies only with --name")
    helper.read_required(sys.modules[__name__], root, gitdir, args.harness, session,
                         name=args.name, part=args.part, next_entry=args.next_entry, role=role)


def statusline(delegate_json):
    raw = sys.stdin.buffer.read(1024 * 1024 + 1)
    payload = {}
    try:
        payload = json.loads(raw) if len(raw) <= 1024 * 1024 else {}
        if isinstance(payload, dict):
            checkpoints().statusline(sys.modules[__name__], "claude", payload)
    except (OSError, ValueError, TypeError, KeyError, ImportError, subprocess.SubprocessError):
        pass  # The original display must keep working when telemetry is unavailable.
    delegate = json.loads(delegate_json)
    if delegate:
        return subprocess.run(delegate["command"], shell=True, input=raw).returncode
    used = payload.get("context_window", {}).get("used_percentage") if isinstance(payload, dict) else None
    print(f"Context: {used}%" if used is not None else "Context: unavailable")
    return 0


def compaction_settings(args):
    if not 5 < args.compact_at <= 100:
        raise ValueError("Claude auto-compaction percentage must be greater than 5 and at most 100")
    path = settings_path("claude", args.settings)
    with locked(path.with_name(path.name + ".context-injector.lock")):
        data = read_settings(path)
        env = data.get("env", {})
        if not isinstance(env, dict):
            raise ValueError("Claude settings env must be an object")
        if path.exists():
            atomic(path.with_name(path.name + f".context-injector-backup-{uuid.uuid4().hex}"), bounded(path, 1024 * 1024))
        data["env"] = {**env, "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(args.compact_at)}
        atomic(path, json.dumps(data, indent=2) + "\n")
    print(json.dumps({"settings_file": str(path), "saved_auto_compact_percent": args.compact_at,
                      "default_reminder_percent": args.compact_at - 5,
                      "note": "Saved configuration only. Existing sessions may retain earlier environment values; restart/resume and verify the actual session before relying on it. No session was changed or restarted."}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=os.getcwd(), help="worktree or a directory inside it")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="opt this worktree in; never installs hooks")
    init.add_argument("--default-role", choices=ROLES)
    init.add_argument("--repo-profile-file",
                      help="select an external JSON profile bound to this checkout's exact origin")
    cmd = sub.add_parser("dispatch", help="enroll an implementer worktree and launch its task on stdin")
    cmd.add_argument("--owner-worktree", required=True)
    cmd.add_argument("--harness", choices=("claude", "codex"), required=True)
    cmd.add_argument("--task-file", required=True)
    cmd.add_argument("--settings", help="settings JSON used for hook-runtime preflight")
    cmd.add_argument("--prepare-only", action="store_true", help="create a dispatch ticket without launching a process")
    cmd.add_argument("--receipt-file", help="write an exclusive immutable JSON dispatch snapshot with a live task lookup command")
    cmd.add_argument("argv", nargs=argparse.REMAINDER)
    cmd = sub.add_parser("prepare-dispatch", help="create an immutable pending OpenCode ticket without launching a session")
    cmd.add_argument("--owner-worktree", required=True)
    cmd.add_argument("--harness", choices=("opencode",), required=True)
    cmd.add_argument("--task-file", required=True)
    cmd.add_argument("--receipt-file", required=True)
    cmd.add_argument("--role", choices=("implementer",), required=True)
    cmd.add_argument("--parent-task-id",
                     help="exact active parent task reference when the caller is an OpenCode orchestrator")
    cmd = sub.add_parser("bind-dispatch", help="bind one prepared ticket to its exact native OpenCode session")
    cmd.add_argument("--harness", choices=("opencode",), required=True)
    cmd.add_argument("--session", required=True)
    cmd.add_argument("--task-id", required=True)
    cmd.add_argument("--receipt-file", required=True)
    cmd.add_argument("--role", choices=("implementer",), required=True)
    cmd = sub.add_parser("context", help="render current context for a native OpenCode context hook")
    cmd.add_argument("--harness", choices=("opencode",), required=True)
    cmd.add_argument("--session", required=True)
    cmd = sub.add_parser("tasks", help="list this owner's tasks across registered Git worktrees; not proof of success")
    cmd.add_argument("--owner-worktree", help="directly initialized owner; defaults to --cwd")
    cmd.add_argument("--status", choices=("pending", "active", "finished"))
    for name in ("update-task", "finish-task", "task-status"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--task-id", required=True)
        if name == "update-task":
            cmd.add_argument("--task-file", required=True)
    for name in ("enable", "disable"):
        sub.add_parser(name, help=f"{name} future injections for this worktree")
    status = sub.add_parser("status", help="report registration, context budgets, enabled state, and optional hook presence")
    status.add_argument("--harness", choices=HARNESS_CHOICES)
    status.add_argument("--session", help="report the exact session's selected standing context")
    status.add_argument("--settings", help="inspect an explicit settings JSON file")
    status.add_argument("--runtime-dir", help="inspect the OpenCode plugin in an explicit runtime directory")
    required_reads = sub.add_parser(
        "required-reads", help="manage and inspect owner-reviewed required reads",
        description="Register the owner-reviewed documents that gate tool use for each session role.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  pattern-buffer --cwd /worktree required-reads set --file ~/required-reads.json\n  pattern-buffer --cwd /worktree required-reads status --harness codex --session SESSION_ID")
    required_actions = required_reads.add_subparsers(dest="required_reads_action", required=True)
    set_reads = required_actions.add_parser(
        "set", help="register a reviewed list after owner confirmation",
        description="Register the reviewed JSON list for this worktree. Run it from an interactive terminal outside a harness session.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example: pattern-buffer --cwd /worktree required-reads set --file ~/required-reads.json")
    set_reads.add_argument("--file", required=True,
                           help="reviewed UTF-8 JSON list file with schema_version 1 and entries")
    clear_reads = required_actions.add_parser(
        "clear", help="remove the registered list after owner confirmation",
        description="Remove required-read registration for this worktree. Run it from an interactive terminal outside a harness session.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example: pattern-buffer --cwd /worktree required-reads clear")
    override_reads = required_actions.add_parser(
        "override", help="open one session's gate for 1 to 60 minutes",
        description="Open the required-reads gate for one session's current context generation. The owner confirms the target session in an interactive terminal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example: pattern-buffer --cwd /worktree required-reads override --harness codex --session SESSION_ID --reason 'Urgent recovery' --minutes 10")
    override_reads.add_argument("--harness", choices=("claude", "codex"), required=True,
                                help="native harness that owns the target session")
    override_reads.add_argument("--session", required=True, help="exact target session ID")
    override_reads.add_argument("--reason", required=True, help="visible reason for opening the gate")
    override_reads.add_argument("--minutes", required=True, type=int,
                                help="whole minutes from 1 through 60")
    status_reads = required_actions.add_parser(
        "status", help="print the required-read report for one current context window",
        description="Print one JSON report for each required entry that applies to the session's role and generation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example: pattern-buffer --cwd /worktree required-reads status --harness codex --session SESSION_ID --json")
    status_reads.add_argument("--harness", choices=("claude", "codex"), required=True,
                              help="native harness that owns the session")
    status_reads.add_argument("--session", required=True,
                              help="exact session ID; must match the native caller when one is present")
    status_reads.add_argument("--json", action="store_true", dest="json_output",
                              help="accepted for explicit machine-readable use; JSON is already the default")
    read_required = sub.add_parser(
        "read-required", help="print a required document or its next unread part",
        description="Print one canonical frame to stdout without changing receipt state. A matching native PostToolUse event records receipt metadata only when the complete output and current inputs pass validation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  pattern-buffer read-required --harness codex --name engineering-guide\n  pattern-buffer read-required --harness codex --next")
    read_required.add_argument("--harness", choices=("claude", "codex"), required=True,
                               help="native harness that owns the session")
    read_required.add_argument("--session", help="exact session ID; defaults to the native harness session")
    read_choice = read_required.add_mutually_exclusive_group(required=True)
    read_choice.add_argument("--name", help="registered entry name to print")
    read_choice.add_argument("--next", action="store_true", dest="next_entry",
                             help="print the next unread entry or part for this role")
    read_required.add_argument("--part", type=int,
                               help="1-based part number for a file larger than the harness piece size")
    handoff = sub.add_parser("handoff-check", help="read-only context, checkpoint, runtime, and evidence check for a successor")
    handoff.add_argument("--harness", choices=("claude", "codex"), required=True)
    handoff.add_argument("--session", required=True)
    handoff.add_argument("--settings", help="inspect an explicit settings JSON file")
    for name in ("install", "uninstall"):
        cmd = sub.add_parser(name, help=f"{name} only this app's owned harness integration")
        cmd.add_argument("--harness", choices=HARNESS_CHOICES, required=True)
        cmd.add_argument("--settings", help="override the user-level settings JSON path")
        cmd.add_argument("--runtime-dir", help="override the stable installed runtime directory")
        if name == "install":
            cmd.add_argument("--bin-dir", help="install the pattern-buffer launcher here; defaults to ~/.local/bin for standard installs and the runtime directory for custom --runtime-dir installs")
            mode = cmd.add_mutually_exclusive_group()
            mode.add_argument("--checkpoints", action="store_true", default=True,
                              help="include checkpoint processing hooks (default; does not enroll sessions)")
            mode.add_argument("--no-checkpoints", action="store_false", dest="checkpoints",
                              help="disable checkpoint processing while retaining required-read receipt handling")
    for name in ("assign", "preview"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--harness", choices=HARNESS_CHOICES, required=True)
        cmd.add_argument("--session", required=True)
        cmd.add_argument("--agent", default="")
        cmd.add_argument("--role", choices=ROLES, required=name == "assign")
        if name == "assign":
            cmd.add_argument("--context-file", help="reviewed input copied to this exact session's private named file")
            cmd.add_argument("--reviewed-update", action="store_true")
            cmd.add_argument("--evidence", help="brief reference for the explicit reviewed update")
    enrollment_help = {
        "session-enroll": "validate exact rendered budgets and publish reviewed context for one session",
        "enrollment-status": "inspect the exact binding, required file and context budget; not model receipt",
        "enrollment-release": "release from the outgoing native owner to an explicitly named successor",
        "enrollment-adopt": "adopt from the named native successor using reviewed handoff inputs",
    }
    for name, description in enrollment_help.items():
        cmd = sub.add_parser(name, help=description, description=description)
        cmd.add_argument("--harness", choices=("claude", "codex"), required=True)
        cmd.add_argument("--session", required=True)
        if name in ("session-enroll", "enrollment-adopt"):
            cmd.add_argument("--context-file", "--file", required=True,
                             help="reviewed standing source; separate from the current-state checkpoint")
            cmd.add_argument("--role", choices=ROLES, default="orchestrator")
        if name == "session-enroll":
            cmd.add_argument("--reviewed-update", action="store_true",
                             help="replace or restore this owner's approved source; requires --evidence")
        if name in ("enrollment-release", "enrollment-adopt"):
            cmd.add_argument("--enrollment-id", required=True)
            cmd.add_argument("--evidence", required=True, help="existing authority/handoff evidence reference")
        elif name == "session-enroll":
            cmd.add_argument("--evidence")
        if name == "enrollment-release":
            cmd.add_argument("--next-harness", choices=("claude", "codex"), required=True)
            cmd.add_argument("--next-session", required=True)
        if name == "enrollment-adopt":
            cmd.add_argument("--checkpoint-file", help="fresh reviewed incoming note; required for orchestrators")
    cmd = sub.add_parser("reliability", help="summarize bounded local lifecycle events, not model comprehension")
    cmd.add_argument("--since", default="7d", help="report window, for example 7d or 24h (default: 7d)")
    cmd.add_argument("--json", action="store_true", dest="json_output",
                     help="print structured bounded evidence, with receipt and coverage limits")
    cmd = sub.add_parser("reliability-config", help="configure owned telemetry retention and size limits")
    cmd.add_argument("--retention-days", type=int, help="owned telemetry age limit (default: 30 days)")
    cmd.add_argument("--max-bytes", type=int, help="per-worktree owned telemetry cap (default: 20971520 bytes)")
    for name in ("validate", "doctor"):
        cmd = sub.add_parser(name, help="lint both roles and report exact header/content budgets; not delivery or hook trust")
        cmd.add_argument("--harness", choices=("claude", "codex"), default="codex")
        cmd.add_argument("--session", default="validation", help="session identity to include in the budget")
        cmd.add_argument("--agent", default="")
        if name == "doctor":
            cmd.add_argument("--settings", help="inspect this harness settings file without changing it")
    cmd = sub.add_parser("compose", help="compose reviewed rules and a worktree mission into a private context file")
    cmd.add_argument("--rules", required=True, help="absolute shared and role rules file")
    cmd.add_argument("--mission", required=True, help="absolute per-worktree mission file")
    cmd.add_argument("--output", required=True, help="absolute ordinary context output file")
    cmd.add_argument("--min-headroom", type=int, default=1024,
                     help="minimum remaining rendered bytes per role (default: 1024)")
    cmd.add_argument("--replace", action="store_true", help="replace a differing existing output file")
    cmd.add_argument("--check", action="store_true", help="compare output bytes without writing")
    cmd.add_argument("--harness", choices=("claude", "codex"), default="codex")
    cmd.add_argument("--session", help="exact recipient session for prospective enrollment budgets")
    cmd.add_argument("--agent", default="", help="existing agent ID used in the rendered preview header")
    cmd = sub.add_parser("hook")
    cmd.add_argument("--harness", choices=("claude", "codex"), required=True)
    cmd.add_argument("--required-reads-only", action="store_true", help=argparse.SUPPRESS)
    cmd = sub.add_parser("hook-config", help="print configuration to merge manually")
    cmd.add_argument("--harness", choices=HARNESS_CHOICES, required=True)
    cmd.add_argument("--runtime-dir", help="select the OpenCode plugin runtime directory")
    mode = cmd.add_mutually_exclusive_group()
    mode.add_argument("--checkpoints", action="store_true", default=True)
    mode.add_argument("--no-checkpoints", action="store_false", dest="checkpoints")
    guide = sub.add_parser("guide", help="print the operational guide without reading worktree state")
    guide.add_argument("--print", action="store_true", dest="print_guide",
                       help="print the guide (the default when using this command)")
    cmd = sub.add_parser("checkpoint-enroll", help="self-service briefing, note adoption and supported integration preparation; no separate human approval")
    cmd.add_argument("--harness", choices=("claude", "codex", "opencode"), required=True)
    cmd.add_argument("--session", help="defaults to this harness's inherited native session ID")
    cmd.add_argument("--compact-at", type=float, help="confirmed percentage when native runtime detection is unavailable")
    cmd.add_argument("--lead-points", type=float)
    cmd.add_argument("--file", help="reviewed private note; omit to show the take-stock briefing without changes")
    cmd.add_argument("--settings", help="explicit harness settings JSON; defaults to user-level settings")
    cmd.add_argument("--runtime-dir", help="explicit installed runtime directory")
    cmd.add_argument("--prepare-restart", action="store_true", help="prepare removal of legacy native hook copies without changing an existing checkpoint")
    for name in ("checkpoint-init", "checkpoint-config", "checkpoint-save", "checkpoint-status", "checkpoint-read"):
        cmd = sub.add_parser(name, help="manage a private orchestrator checkpoint; see docs/checkpoints.md")
        cmd.add_argument("--harness", choices=HARNESS_CHOICES, required=True)
        cmd.add_argument("--session", required=True, help="exact native orchestrator session ID")
        if name in ("checkpoint-init", "checkpoint-config"):
            cmd.add_argument("--compact-at", type=float, help="operator-confirmed compaction percentage on this harness's usage scale")
            cmd.add_argument("--lead-points", type=float)
        if name == "checkpoint-init":
            cmd.add_argument("--path", help="reuse an explicitly owned, durable private checkpoint file")
        if name == "checkpoint-config":
            cmd.add_argument("--reminders", choices=("on", "off"))
            cmd.add_argument("--enabled", choices=("on", "off"))
            cmd.add_argument("--idle-warning-minutes", type=float,
                             help="Claude idle warning delay, default 50; 0 disables future arming")
        if name == "checkpoint-save":
            cmd.add_argument("--file", required=True, help="reviewed Markdown input; no automatic summarization")
        if name == "checkpoint-read":
            cmd.add_argument("--part", type=int, help="print one bounded, hook-verifiable checkpoint part")
    cmd = sub.add_parser("checkpoint-pressure-config", help=argparse.SUPPRESS)
    cmd.add_argument("--harness", choices=("opencode",), required=True)
    cmd.add_argument("--session", required=True)
    cmd.add_argument("--descriptor-file", required=True,
                     help="private native runtime descriptor outside the worktree")
    cmd = sub.add_parser("opencode-checkpoint-event", help=argparse.SUPPRESS)
    cmd.add_argument("--session", required=True)
    cmd.add_argument("--event", choices=("tool-completed", "compaction-started", "compaction-ended", "compaction-failed"), required=True)
    cmd.add_argument("--event-id")
    cmd.add_argument("--aggregate-id")
    cmd.add_argument("--event-sequence", type=int)
    cmd = sub.add_parser("opencode-checkpoint-pressure", help=argparse.SUPPRESS)
    cmd.add_argument("--session", required=True)
    cmd = sub.add_parser("opencode-checkpoint-pressure-emitted", help=argparse.SUPPRESS)
    cmd.add_argument("--session", required=True)
    cmd.add_argument("--correlation-id", required=True)
    cmd = sub.add_parser("statusline", help=argparse.SUPPRESS)
    cmd.add_argument("--delegate-json", default="null")
    cmd = sub.add_parser("compaction-settings", help="save Claude's auto-compaction percentage without restarting sessions")
    cmd.add_argument("--compact-at", required=True, type=float)
    cmd.add_argument("--settings", help="Claude settings JSON; defaults to user settings")
    args = parser.parse_args()
    try:
        if args.command == "guide":
            sys.stdout.write(bounded(guide_source(), FILE_LIMIT))
            return 0
        if args.command == "hook":
            hook(args.harness, checkpoints_enabled=not args.required_reads_only)
            return 0
        if args.command == "hook-config":
            if args.harness == "opencode" and not args.checkpoints:
                raise ValueError("OpenCode context plugins do not use Pattern Buffer checkpoint hooks")
            runtime_plugin = opencode_plugin_directory(args.runtime_dir) if args.harness == "opencode" else None
            print(json.dumps(hook_config(args.harness, with_checkpoints=args.checkpoints,
                                         opencode_plugin=runtime_plugin), indent=2))
            return 0
        if args.command == "statusline":
            return statusline(args.delegate_json)
        if args.command == "compaction-settings":
            compaction_settings(args)
            return 0
        if args.command in ("install", "uninstall"):
            configure_hooks(args)
            return 0
        root, gitdir = checkout(args.cwd)
        config_path = gitdir / "context-injector" / "config.json"
        if args.command == "required-reads":
            required_reads_command(root, gitdir, args)
            return 0
        elif args.command == "read-required":
            read_required_command(root, gitdir, args)
            return 0
        elif args.command == "reliability":
            report = auxiliary("reliability").report(root, since=args.since)
            if report.get("ok"):
                report["idle_timer_receipts"] = idle_receipt_report(root, report["since"])
            if args.json_output or not report.get("ok"):
                print(json.dumps(report, indent=2, default=str))
            else:
                summary = report["summary"]
                print(f"Pattern Buffer reliability: {root}; since {report['since']}")
                print("Observed lifecycle events: " + str(summary.get("event_count", 0)))
                for name, count in sorted(summary.get("by_event", {}).items()):
                    print(f"  {name}: {count}")
                print("History coverage: " + json.dumps(report.get("coverage", {}), sort_keys=True))
                gaps = sorted({item["code"] for item in report.get("unknowns", [])})
                print("Unknowns: " + (", ".join(gaps) if gaps else "none reported"))
                idle = report["idle_timer_receipts"]
                print("Hailing idle receipts: " + json.dumps({key: value for key, value in idle.items()
                                                             if key != "receipts"}, sort_keys=True))
                print("Emission and submission do not prove model receipt, note accuracy, or task completion.")
            return 0 if report.get("ok") else 1
        elif args.command == "reliability-config":
            values = {key: value for key, value in {
                "retention_days": args.retention_days, "max_bytes": args.max_bytes}.items() if value is not None}
            result = auxiliary("reliability").configure(root, **values)
            print(json.dumps(result, indent=2))
            return 0 if result.get("ok") else 1
        elif args.command.startswith("enrollment-") or args.command == "session-enroll" or (
                args.command == "assign" and args.context_file):
            print(json.dumps(session_command(root, gitdir, args), indent=2, default=str))
        elif args.command == "checkpoint-enroll":
            print(json.dumps(self_enroll(root, gitdir, args), indent=2))
        elif args.command == "checkpoint-pressure-config":
            result = checkpoints().configure_opencode_pressure(
                sys.modules[__name__], root, gitdir, args.session, args.descriptor_file)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "opencode-checkpoint-event":
            result = checkpoints().opencode_event(sys.modules[__name__], root, gitdir, args.session,
                                                  args.event, args.event_id, args.aggregate_id,
                                                  args.event_sequence)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "opencode-checkpoint-pressure":
            raw = sys.stdin.buffer.read(checkpoints().OPENCODE_PRESSURE_OBSERVATION_LIMIT + 1)
            if len(raw) > checkpoints().OPENCODE_PRESSURE_OBSERVATION_LIMIT:
                raise ValueError("pressure observation exceeds its bounded size")
            try:
                observation = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                raise ValueError("pressure observation is not valid UTF-8 JSON") from None
            result = checkpoints().opencode_pressure_observe(
                sys.modules[__name__], root, gitdir, args.session, observation)
            result.update(harness="opencode", session_id=args.session, worktree=str(root))
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "opencode-checkpoint-pressure-emitted":
            result = checkpoints().opencode_pressure_emitted(
                sys.modules[__name__], root, gitdir, args.session, args.correlation_id)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "checkpoint-read" and args.part is not None:
            sys.stdout.write(checkpoints().handle_cli(sys.modules[__name__], root, gitdir, args))
            sys.stdout.flush()
        elif args.command.startswith("checkpoint-"):
            print(json.dumps(checkpoints().handle_cli(sys.modules[__name__], root, gitdir, args), indent=2))
        elif args.command == "dispatch":
            receipt = Path(args.receipt_file).expanduser().absolute() if args.receipt_file else None
            if receipt and (receipt.exists() or receipt.is_symlink()):
                raise ValueError("receipt file already exists; use a unique path for each dispatch")
            if not args.prepare_only:
                executable, command = dispatch_command(root, args)
                check_dispatch_hook(args.harness, args.settings)
            task_id = prepare_dispatch(root, gitdir, args)
            if receipt:
                saved_receipt = write_dispatch_receipt(receipt, root, gitdir, task_id)
                print(f"Pattern Buffer receipt {receipt}: immutable dispatch snapshot, not live task state.\n"
                      f"Current task state: {saved_receipt['task_status_command']}", file=sys.stderr, flush=True)
            if args.prepare_only:
                print(json.dumps({"task_id": task_id, "environment": {DISPATCH_ENV: task_id}}))
            else:
                task_data, brief = read_task(root, gitdir, task_id)
                preflight_dispatch_context(root, args.harness, task_id, task_data, brief)
                print(f"Pattern Buffer dispatch {task_id}; task retained for explicit update/finish.", file=sys.stderr, flush=True)
                snapshot = task_path(gitdir, task_id).parent / f"{hashlib.sha256(brief.encode()).hexdigest()}.txt"
                with snapshot.open("rb") as handle:
                    os.dup2(handle.fileno(), 0)
                os.chdir(root)
                os.execvpe(executable, command, {**os.environ, DISPATCH_ENV: task_id})
        elif args.command == "prepare-dispatch":
            receipt = opencode_receipt_path(args.receipt_file)
            if receipt.exists() or receipt.is_symlink():
                raise ValueError("receipt file already exists; use a unique file for each preparation")
            if root == checkout(args.owner_worktree)[0]:
                raise ValueError("OpenCode task preparation requires a dedicated child worktree")
            task_id = prepare_dispatch(root, gitdir, args)
            saved_receipt = write_dispatch_receipt(receipt, root, gitdir, task_id)
            result = {"task_id": task_id, "harness": "opencode", "role": args.role,
                      "status": "pending", "receipt_file": str(receipt),
                      "receipt_type": saved_receipt["receipt_type"]}
            if "ticket_version" in saved_receipt:
                result.update(ticket_version=saved_receipt["ticket_version"],
                              parent_kind=saved_receipt["parent_kind"])
            if saved_receipt.get("parent") is not None:
                result["parent"] = saved_receipt["parent"]
            print(json.dumps(result))
        elif args.command == "bind-dispatch":
            bind_opencode_dispatch(root, gitdir, args)
            print(json.dumps({"task_id": args.task_id, "harness": "opencode", "role": args.role,
                              "session_id": args.session, "status": "active"}))
        elif args.command == "context":
            print(json.dumps(opencode_context(root, gitdir, args.session), ensure_ascii=False))
        elif args.command == "compose":
            result, status = compose_report(root, gitdir, args)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return status
        elif args.command == "tasks":
            result = list_owner_tasks(args.owner_worktree or root, args.status)
            print(json.dumps(result, indent=2))
            return 1 if result["errors"] else 0
        elif args.command in ("update-task", "finish-task", "task-status"):
            path = task_path(gitdir, args.task_id)
            if not path.exists() and not path.is_symlink():
                raise ValueError(
                    f"task {args.task_id} was not found in this worktree: {root}. "
                    "Use the receipt's worktree or run tasks from the registered owner to find the child worktree, "
                    f"then retry your existing command with --cwd CHILD_WORKTREE before {args.command}, keeping its other arguments")
            with locked(path.with_suffix(".lock")):
                data, _ = read_task(root, gitdir, args.task_id)
                if args.command == "update-task":
                    if data["status"] == "finished":
                        raise ValueError("finished tasks cannot be revived; dispatch a new assignment")
                    if data["harness"] == "opencode" and data["status"] == "pending":
                        raise ValueError("pending OpenCode receipts are immutable; prepare a fresh dispatch ticket")
                    brief = bounded(Path(args.task_file).expanduser().absolute(), TASK_LIMIT)
                    data["brief_sha256"] = snapshot_brief(path, brief)
                    data["updated_at"] = time.time()
                elif args.command == "finish-task":
                    data["status"] = "finished"
                    data["finished_at"] = time.time()
                if args.command != "task-status":
                    atomic(path, json.dumps(data, indent=2))
            result = {"task_id": args.task_id, **data}
            if args.command == "update-task":
                result.update(
                    delivery_state="pending_recovery",
                    delivery_note="Saved for the next eligible recovery hook; this does not interrupt current work or confirm model reception.",
                    urgent_change_guide="apps/pattern-buffer/docs/implementer-dispatch.md#urgent-scope-changes")
            print(json.dumps(result, indent=2))
        elif args.command == "status":
            if args.settings and not args.harness:
                raise ValueError("--settings requires --harness")
            registered = config_path.exists()
            config, source = load(root, gitdir) if registered else ({}, root / ".context-injector/context.md")
            result = {"worktree": str(root), "registered": registered,
                      "enabled": registered and enabled(config),
                      "default_role": config.get("default_role"), "context_file": str(source),
                      "context_owner": config.get("context_owner"),
                      "repo_profile": resolved_repo_profile(root, config) if registered else None,
                      "delivery_state": "not_tracked",
                      "delivery_note": (
                          "Model reception is not recorded by Pattern Buffer; "
                          "legacy delivery_verified=false does not mean reception failed."),
                      "delivery_verified": False}
            reference_source = source
            if args.harness:
                settings = settings_path(args.harness, args.settings)
                data = read_settings(settings)
                result["settings_file"] = str(settings)
                if args.harness == "opencode":
                    plugins = data.get("plugins", [])
                    if not isinstance(plugins, list) or any(not isinstance(item, str) for item in plugins):
                        raise ValueError("OpenCode plugins must be an array of strings")
                    plugin = str(opencode_plugin_directory(
                        opencode_runtime_directory(args.runtime_dir)).resolve())
                    result["owned_plugin_entries"] = plugins.count(plugin)
                else:
                    result["owned_hook_handlers"] = sum(
                        owned(handler, args.harness)
                        for group in data.get("hooks", {}).get("SessionStart", [])
                        for handler in group["hooks"])
                    result["owned_required_reads_pretool_handlers"] = sum(
                        owned(handler, args.harness)
                        for group in data.get("hooks", {}).get("PreToolUse", [])
                        for handler in group["hooks"])
                budget_session = args.session or (
                    "ses_" + "A" * 508 if args.harness == "opencode" else "validation")
                try:
                    budgets = current_context_budgets(root, gitdir, args.harness, budget_session)
                    budgets["exact_session"] = bool(args.session)
                    budgets["session_basis"] = ("requested exact session" if args.session else
                                                 "conservative maximum-length header placeholder")
                    result["standing_context_budgets"] = budgets
                except (OSError, ValueError, TypeError, UnicodeError, subprocess.SubprocessError) as error:
                    result["standing_context_budgets"] = {
                        "ok": False, "worktree": str(root), "harness": args.harness,
                        "session": budget_session, "limit_bytes": LIMIT,
                        "detail": str(error),
                        "roles": {role: {"ok": False, "detail": str(error)} for role in ROLES},
                    }
            if args.session:
                if not args.harness:
                    raise ValueError("--session requires --harness")
                binding = session_binding(root, gitdir, args.harness, args.session)
                result.update(session=args.session, enrollment=binding_report(binding),
                              context_file=str(binding.path) if binding else str(source),
                              context_required=binding is not None)
                reference_source = Path(binding.path) if binding else source
            if args.harness in ("claude", "codex"):
                helper = auxiliary("required_reads")
                required_registration = helper.registration_path(gitdir)
                if required_registration.exists() or required_registration.is_symlink():
                    exact_session = bool(args.session)
                    if args.session:
                        required_session = helper.resolve_session(args.harness, args.session)
                    else:
                        try:
                            required_session = helper.resolve_session(args.harness)
                        except ValueError:
                            required_session = "validation"
                    try:
                        role, _origin, _saved = resolve_role(
                            root, gitdir, config, args.harness, required_session)
                    except (OSError, ValueError, TypeError, UnicodeError, subprocess.SubprocessError):
                        role = None
                    required_report = helper.status(
                        sys.modules[__name__], root, gitdir, args.harness, required_session, role)
                    if required_report is not None:
                        required_report["session_basis"] = (
                            "requested session" if exact_session else
                            "native harness session" if required_session != "validation" else
                            "validation placeholder; pass --session for an exact session")
                        result["required_reads"] = required_report
            result["reference_warnings"] = reference_warnings_for_path(reference_source, root)
            print(json.dumps(result, indent=2))
        elif args.command == "handoff-check":
            result = handoff_report(root, gitdir, config_path, args.harness, args.session, args.settings)
            print(json.dumps(result, indent=2))
            return 0 if result["configuration_ready"] else 1
        elif args.command in ("enable", "disable"):
            with locked(config_path.with_suffix(".lock")):
                config, source = load(root, gitdir)
                if args.command == "enable":
                    # Reject invalid reminders before opting back in.
                    sections(source)
                    render(root, "codex", "enable-validation")
                config["enabled"] = args.command == "enable"
                atomic(config_path, json.dumps(config, indent=2))
            print(f"{args.command}d: {root}. Applies to future hook events only.")
        elif args.command == "init":
            folder = root / ".context-injector"
            if folder.is_symlink():
                raise ValueError("context directory must not be a symlink")
            source = folder / "context.md"
            previous = load(root, gitdir)[0] if config_path.exists() else {}
            if previous.get("context_owner"):
                raise ValueError("dispatch-enrolled worktree uses its owner's context; do not initialize a local copy")
            requested_profile = None
            if args.repo_profile_file is not None:
                profile, requested_profile = read_repo_profile(args.repo_profile_file)
                verify_repo_profile_origin(root, profile)
            with locked(config_path.with_suffix(".lock")):
                previous = load(root, gitdir)[0] if config_path.exists() else {}
                if previous.get("context_owner"):
                    raise ValueError("dispatch-enrolled worktree uses its owner's context; do not initialize a local copy")
                saved_profile = previous.get("repo_profile")
                if requested_profile is not None and isinstance(saved_profile, dict) and requested_profile != saved_profile:
                    raise ValueError("a different repository profile is already registered; existing contexts are not migrated")
                selected_profile = requested_profile or saved_profile
                source_existed = source.exists() or source.is_symlink()
                profile = profile if requested_profile is not None else (
                    load_registered_profile(root, selected_profile)
                    if selected_profile is not None and not source_existed else None)
                if source.is_symlink():
                    raise ValueError(f"expected a regular, non-symlink file: {source}")
                if not source.exists():
                    folder.mkdir(exist_ok=True)
                    try:
                        with source.open("x", encoding="utf-8") as handle:
                            handle.write(compose_template(profile.get("fragment") if profile else None))
                    except FileExistsError:
                        source_existed = True
                sections(source)
                updated = {**previous, "version": 1, "root": str(root),
                           "default_role": args.default_role if args.default_role is not None
                           else previous.get("default_role"),
                           "enabled": previous.get("enabled", True)}
                if selected_profile is not None:
                    updated["repo_profile"] = selected_profile
                else:
                    updated.pop("repo_profile", None)
                atomic(config_path, json.dumps(updated, indent=2))
            if selected_profile and args.repo_profile_file and source_existed and not saved_profile:
                print(f"Initialized {root}; existing context preserved. Profile {selected_profile} recorded, but its controls were not applied to existing text. Review and adopt {folder / 'context.md'} explicitly. No hooks installed.")
            elif isinstance(selected_profile, dict):
                print(f"Initialized {root}; repository profile: {selected_profile['name']}. Edit {source}. No hooks installed.")
            elif isinstance(selected_profile, str):
                print(f"Initialized {root}; legacy repository profile registration retained. Edit {source}. No hooks installed.")
            else:
                print(f"Initialized {root}; edit {source}. No hooks installed.")
        elif args.command == "assign":
            config, _ = load(root, gitdir)
            if args.harness == "opencode" and config.get("context_owner"):
                raise ValueError("linked OpenCode implementers must bind a prepared task ticket instead of assigning a role")
            if session_binding(root, gitdir, args.harness, args.session, args.agent) is not None:
                raise ValueError("session has isolated standing context; use session-enroll --reviewed-update")
            path = state_path(gitdir, args.harness, args.session, args.agent)
            if path.exists():
                previous = json.loads(bounded(path, FILE_LIMIT))
                if not isinstance(previous, dict):
                    raise ValueError("invalid session registration")
                if previous.get("task_id"):
                    raise ValueError("session has a dispatched task; use update-task or finish-task instead")
            # Validate the selected content before replacing an assignment.
            render(root, args.harness, args.session, args.agent, args.role)
            path = state_path(gitdir, args.harness, args.session, args.agent)
            atomic(path, json.dumps({"root": str(root), "role": args.role}))
            print(f"Assigned {args.harness} session {args.session} to {args.role}.")
        elif args.command == "preview":
            rendered = render(root, args.harness, args.session, args.agent, args.role,
                              runtime_policy=ORDINARY_WORKTREE_RENDER_POLICY)
            print(finalize_rendered_context(rendered).text)
        else:
            config, _source = load(root, gitdir)
            report = current_context_budgets(root, gitdir, args.harness, args.session, args.agent)
            reference_report = reference_check(bounded(Path(report["source"]), FILE_LIMIT),
                                               report["source"], root)
            for finding in reference_report["warnings"]:
                print(f"WARNING: {finding['message']} ({finding['source']}: {finding['target']})",
                      file=sys.stderr)
            if args.command == "validate" and reference_report["errors"]:
                for finding in reference_report["errors"]:
                    print(f"ERROR: {finding['message']} ({finding['source']}: {finding['target']})",
                          file=sys.stderr)
                raise ValueError("local context references are invalid")
            if args.command == "doctor":
                for finding in reference_report["errors"]:
                    print(f"WARNING: {finding['message']} ({finding['source']}: {finding['target']})",
                          file=sys.stderr)
            failures = []
            for role, budget in report["roles"].items():
                if "bytes_used" in budget:
                    print(f"{role}: {budget['bytes_used']} / {LIMIT} bytes; "
                          f"header {budget['header_bytes']}; content {budget['content_bytes']} / "
                          f"{budget['usable_content_bytes']} usable bytes; "
                          f"{budget['remaining_bytes']} remaining")
                    if budget["remaining_bytes"] <= LIMIT * 0.2:
                        print(f"WARNING: {role} context has {budget['remaining_bytes']} bytes remaining "
                              "in its exact rendered budget", file=sys.stderr)
                if not budget.get("ok", False):
                    failures.append(f"{role}: {budget.get('detail', 'context does not fit')}")
            if failures and args.command != "doctor":
                raise ValueError("; ".join(failures))
            if failures:
                print(f"Context issues reported: {report['source']}")
            else:
                print(f"Context valid: {report['source']}; default role: {config.get('default_role') or 'unassigned'}")
            if args.command == "doctor":
                helper = auxiliary("required_reads")
                try:
                    required_report = helper.status(
                        sys.modules[__name__], root, gitdir, args.harness, args.session,
                        resolve_role(root, gitdir, config, args.harness, args.session, args.agent)[0])
                except (OSError, ValueError, TypeError, UnicodeError, KeyError, ImportError) as error:
                    required_report = {"registered": True, "ok": False,
                                       "integrity_errors": [str(error)]}
                if required_report is not None:
                    digest_state = ("matches registration" if required_report.get("list_digest_matches")
                                    else "invalid or unavailable")
                    print(f"Required reads list: {required_report.get('list_path') or required_report.get('registration_path')} ({digest_state}).")
                    for item in required_report.get("entries", []):
                        print(f"  {item['name']}: {item['state']} ({item['path']})")
                    for detail in required_report.get("integrity_errors", []):
                        print(f"  Integrity error: {detail}")
                    print(f"  Session {required_report.get('session_id')}, generation {required_report.get('generation')}, "
                          f"pending reads {required_report.get('pending_read_count')}.")
                    if required_report.get("next_command"):
                        print(f"  Next command: {required_report['next_command']}")
                    if not required_report.get("ok", False):
                        failures.append("required-read list or file integrity failed")
                    settings = settings_path(args.harness, args.settings)
                    try:
                        settings_data = read_settings(settings)
                        pretool_groups = settings_data.get("hooks", {}).get("PreToolUse", [])
                        pretool_count = sum(owned(handler, args.harness)
                                            for group in pretool_groups for handler in group["hooks"])
                        print(f"PreToolUse hook configured for {args.harness}: {pretool_count}; "
                              "native activation and delivery remain unverified.")
                    except (OSError, ValueError, TypeError, UnicodeError) as error:
                        print(f"PreToolUse hook presence unavailable: {error}; native activation remains unverified.")
                if failures:
                    raise ValueError("; ".join(failures))
                print("Local registration and file state checked only. Hook presence is configuration evidence, not native activation or model receipt.")
        return 0
    except (OSError, ValueError, TypeError, KeyError, ImportError, subprocess.SubprocessError) as error:
        print(f"pattern-buffer: {error}", file=sys.stderr)
        if args.command == "hook" and (os.environ.get(DISPATCH_ENV) or isinstance(error, DispatchFailure)):
            print(json.dumps({"continue": False, "stopReason": "Pattern Buffer dispatch failed: " + str(error)}))
        # Hook failures are advisory. Never block the user's session.
        return 0 if args.command == "hook" else 1


if __name__ == "__main__":
    sys.exit(main())
