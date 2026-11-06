# Pattern Buffer

Pattern Buffer restores editable, worktree-specific context to Claude Code and Codex at startup, resume and compaction, with an opt-in OpenCode context plugin pilot.
It also supports exact-session context enrollment, explicit successor handoffs, bounded task replay, private dispatch receipts and local reliability observations.
The historical Context Injector command and source paths remain compatibility aliases.

## Authority and trust boundaries

Pattern Buffer does not assign work, expand task scope, approve context changes, or install hooks during worktree initialization.
An existing context file is preserved byte-for-byte by init.
An external repository profile is copied only when the context file does not exist, and its declared origin must match the checkout's single origin URL.
The profile is stored by path and content digest; later edits require an explicit profile selection, and existing context text still requires its normal review process.
Exact session identity, owner-linked context, native hook trust and current user direction remain separate requirements.
Hook output is configured context, not proof that a model received, understood or followed it.
Dispatch receipts, event logs and Hailing Frequencies receipts describe observed events, not task completion or model comprehension.

## Quick start

Read the [printable guide](guide.md) with the installed command pattern-buffer guide --print before configuring an installation.
Install only the harness adapters you use.
Installing one harness refreshes recognized aliases already present in that same runtime directory, while retaining their stable hook commands.
For enabled events, an owned-only group is refreshed to canonical group settings in place, while a mixed group updates only the owned handler and preserves shared filters and metadata.
Duplicate owned handlers retain the existing cleanup behavior.
Preserving configured positions does not guarantee that a harness will trust or load the hook or deliver its output.
It does not change another runtime directory, another harness's settings, or native trust.

    pattern-buffer install --harness codex
    pattern-buffer install --harness claude

If installation refuses an unowned or damaged runtime entrypoint, preserve that runtime.
From a source checkout, install the selected harness into a new empty runtime using the same settings file:

    python3 apps/pattern-buffer/pattern_buffer.py install \
        --harness codex \
        --settings /absolute/path/to/codex-settings.json \
        --runtime-dir /absolute/path/to/new-empty-runtime

Use the matching harness name and settings file for Claude.
The generated recovery command preserves whether checkpoint hooks were enabled for that settings file.
This updates the selected harness's owned hook command and leaves worktree enrollment and checkpoint files in place.
The existing PATH launcher remains unchanged; use the new runtime's `pattern-buffer` launcher by explicit path for commands against the recovered runtime.
Review the harness's normal reload and trust controls after the install; installation does not confirm delivery.

Initialize a directly managed Git worktree with an editable context file.

    pattern-buffer --cwd /absolute/path/to/worktree init --default-role orchestrator
    pattern-buffer --cwd /absolute/path/to/worktree validate --harness codex
    pattern-buffer --cwd /absolute/path/to/worktree preview --harness codex --session ACTUAL_SESSION_ID

Omit --default-role when sessions should remain unassigned until explicitly assigned.
Use an owner worktree's dispatch command for a temporary implementer worktree that should share the owner's approved context.

## External repository profiles

Repository-specific controls are supplied as a separate JSON file with schema_version, name, origin and fragment fields.
The [example profile](examples/repository-profile.json) uses a placeholder origin that must be replaced before selection.
Origin matching accepts one exact HTTPS or Git SSH URL and rejects credentials, alternate origins, nonstandard schemes, query strings and fragments.
Select the reviewed file with init --repo-profile-file /absolute/path/to/profile.json.
Profiles are never inferred from a folder name or downloaded from a repository.
When an existing context file is present, selection records the profile but does not merge or migrate its fragment.
An owner-linked child worktree always uses its owner's context and cannot select an independent profile.

## Session context and recovery

Use assign for a persistent worktree role or session-enroll for an exact native session that needs a private standing file.
Session enrollment replaces the worktree context for that exact identity and fails closed when required context is missing or invalid.
Use the [session enrollment guide](docs/session-enrollments.md) for replacement handoffs, identity checks, trust limits and recovery.
Use the [checkpoint guide](docs/checkpoints.md) for opt-in short-lived recovery notes, pressure reminders and Hailing Frequencies idle receipts.
A checkpoint is a current-state snapshot and does not replace standing context, task authorization or the authoritative tracker.
The read-only `handoff-check` distinguishes approved context and optional checkpoint state from installed-hook compatibility, native binding evidence and actual model delivery.
Unknown native or delivery evidence stays unknown and does not add a readiness gate.

## Dispatch and task replay

Dispatch enrolls an implementer worktree, binds a task brief to the owner and launches the selected Claude or Codex CLI.
The CLI does not infer a broader sandbox, role, model or repository permission from the task file.
The task owner can update or finish a task explicitly, and recovery never replays a changed or finished task implicitly.
The [implementer dispatch guide](docs/implementer-dispatch.md) documents the supported launch, update, recovery and ownership boundaries.
The [OpenCode adapter guide](docs/opencode.md) documents its versioned context hook, explicit two-phase task binding, opt-in setup and native delivery limits.
Use a unique --receipt-file path when an immutable local snapshot is useful; a receipt is not live task status.
OpenCode preparation and binding are separate from Claude and Codex launch dispatch, and never start an OpenCode process.
For an exact-session parent or a child already bound to another owner, use the [supported owner and fresh-child route](docs/implementer-dispatch.md#parents-with-exact-session-context).
Keep existing enrollments, tasks and human holds intact rather than substituting another mission or moving private registration metadata.

## Storage and event behavior

Worktree configuration and task records live under the worktree's private Git directory.
Session enrollments, checkpoints and reliability history use owner-only local state outside the repository.
There is no background daemon, transcript watcher or shared role registry.
Disabled and unregistered worktrees emit no context.
Malformed or oversized required context blocks continuation for an enrolled session instead of borrowing another mission.
Recovery errors and uncertain sends are reported for inspection and are not silently replayed.

## Verification and troubleshooting

Run the canonical isolated suite from the repository root.

    python3 -m unittest discover -s apps/pattern-buffer -p 'test_*.py' -v
    python3 -m unittest discover -s apps/context-injector -p 'test_*.py' -v

The compatibility suite checks the former Context Injector source entrypoint against the maintained Pattern Buffer behavior.
If context does not appear, inspect status, validate and preview, then check the harness's own trust and hook controls.
Validation and status report header overhead, usable content bytes and remaining UTF-8 budget for each role.
Pass the exact session ID when available so the calculation uses the enrolled file path and identity.
Without `--session`, status labels its placeholder header budget and does not establish exact native-session binding.
Session enrollment and handoff checks use the same calculation before reviewed text is adopted.
Oversized or malformed required sources still fail closed and are never truncated.
Doctor reads local registration and content only; it does not certify hook activation, model reception or compliance.
After installation changes, restart or resume the harness according to its own configuration reload behavior.
Native delivery checks require a separate bounded pilot in disposable state and are not established by these local tests.
The OpenCode adapter is covered by synthetic Python and Node callback fixtures only.

The canonical implementation is [pattern_buffer.py](pattern_buffer.py).
The [editable default template](templates/default.md) is the source starter used for new worktrees.
