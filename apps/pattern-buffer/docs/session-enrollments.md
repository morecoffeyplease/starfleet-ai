# Session context and replacement handoffs

Standing context defines an agent's approved mission, tracker, assignment and authority boundaries.
A checkpoint is its separate, pruned current-state letter to its future self.
Neither document grants authority beyond the user's instructions.

## Multiple orchestrators in one worktree

Use session enrollment when different agents share a checkout but have different missions.
First initialize the worktree if it is not already registered, preserving any existing context and installation.
Do not change a worktree's default role or standing file to configure a different session.

```sh
pattern-buffer --cwd /absolute/worktree status --harness codex
pattern-buffer --cwd /absolute/worktree init
pattern-buffer --cwd /absolute/worktree session-enroll --harness codex --session SESSION_UUID --context-file /absolute/private/reviewed-standing.md
pattern-buffer --cwd /absolute/worktree status --harness codex --session SESSION_UUID
pattern-buffer --cwd /absolute/worktree validate --harness codex --session SESSION_UUID
```

Use the actual native session ID and `--harness claude` for Claude.
The reviewed standing file uses the existing `# shared`, `# implementer` and `# orchestrator` sections.
Enrollment copies it to `.context-injector/sessions/codex-SESSION_UUID.md` or `claude-SESSION_UUID.md` and prints the durable enrollment ID and expected path.
The session file completely replaces the worktree standing file for that exact primary session.
Unenrolled sessions retain the existing worktree behavior.
Existing implementer replay continues to use its explicitly selected owner worktree.
When a session-specific parent mission differs from that worktree's default, dispatch refuses to substitute the default; select a dedicated owner worktree containing that parent's approved standing context.
This release does not add automatic session-to-child context inheritance or transfer existing child-task ownership.
The installer and existing hooks are reused; enrollment does not restart agents, alter trust or create a worker.

Session context files are private working files, excluded through the repository's local Git exclude file.
Do not commit them or remove another session's file.
For reviewed updates, edit a private input file and repeat `session-enroll` with `--reviewed-update` and an evidence reference.
The supported command validates the final rendered budgets before atomically publishing the update.
Direct changes to the registered file are treated as unreviewed until adopted through the supported command.

```sh
pattern-buffer --cwd /absolute/worktree session-enroll --harness codex --session SESSION_UUID --context-file /absolute/private/reviewed-standing.md --reviewed-update --evidence USER_APPROVAL_REFERENCE
```

Then enroll the separate checkpoint from that session using its reviewed current-state note.
Keep all existing native compaction thresholds and approvals; do not guess a Codex threshold.

```sh
pattern-buffer --cwd /absolute/worktree checkpoint-enroll --harness codex --session SESSION_UUID --file /absolute/private/reviewed-checkpoint.md
```

## Missing or invalid required context

An explicitly enrolled session must not silently inherit the worktree mission if its own file is missing, unreadable, modified outside publication, malformed or over budget.
The hook emits a blocking `STOP` instruction identifying the exact session, expected path and failure.
It also attempts the existing guarded Hailing Frequencies attention path once per unresolved alarm episode.
Held, refused and uncertain delivery remain distinct from successful submission or actual model receipt.
The hook alarm remains available when stronger delivery is unavailable.

On an alarm, pause product work and preserve running jobs, human holds and evidence.
Use `enrollment-status` for the exact harness and session to diagnose the binding.
An exact session reported as the named pending successor of an approved release is in the expected handoff state.
The successor may complete only the already-authorized recovery: adopt the existing enrollment with its ID, reviewed successor context and release evidence, then follow any required fresh checkpoint setup command and validate the binding.
For an active binding with a changed or invalid file, use `session-enroll --reviewed-update` only with an already user-approved source and evidence.
These steps restore the binding only, and product work stays paused until validation succeeds.
For any unexpected missing or invalid state, missing approval or reviewed input, or failed recovery, notify the context owner through the configured contact workflow.
Do not kill jobs, borrow another mission, create new authority or bypass native input guards.
A model receiving a stop instruction is still different from software mechanically enforcing a pause.

## Replacement ownership

The durable enrollment identifies the seat's continuity; the active binding identifies its current exact harness and session.
A replacement does not automatically inherit authority because it has the same workspace name, model or working directory.
Prepare the replacement's reviewed standing file and a fresh, accurate checkpoint before transferring ownership.
The outgoing owner runs release from its own native session and names the exact intended successor.
Release disables its checkpoint reminders and requests supported cancellation of its known idle timer before retiring the binding.
Unresolved cancellation blocks release and must be reconciled without resubmission or impersonation.

```sh
pattern-buffer --cwd /absolute/worktree enrollment-release --harness codex --session OLD_SESSION --enrollment-id ENROLLMENT_ID --next-harness claude --next-session NEW_SESSION --evidence APPROVED_HANDOFF_REFERENCE
```

The named replacement then adopts from its own native session.
It receives a new session-named context file and retains the same durable enrollment ID and historical predecessor identity.
The old binding remains retired and cannot receive the successor's mission or rearm its reminders.

```sh
pattern-buffer --cwd /absolute/worktree enrollment-adopt --harness claude --session NEW_SESSION --enrollment-id ENROLLMENT_ID --context-file /absolute/private/reviewed-standing.md --checkpoint-file /absolute/private/fresh-checkpoint.md --evidence APPROVED_HANDOFF_REFERENCE
pattern-buffer --cwd /absolute/worktree checkpoint-enroll --harness claude --session NEW_SESSION --file /absolute/private/fresh-checkpoint.md
pattern-buffer --cwd /absolute/worktree enrollment-status --harness claude --session NEW_SESSION
```

Adoption does not copy the predecessor's private checkpoint metadata, timers, native approvals or permissions.
Follow the printed checkpoint setup command and verify configuration and eventual model reception separately.
Do not close the predecessor's tab or restart another agent merely because release or adoption succeeded.

## Reliability history

```sh
pattern-buffer --cwd /absolute/worktree reliability --since 7d
pattern-buffer --cwd /absolute/worktree reliability --since 24h --json
pattern-buffer --cwd /absolute/worktree reliability-config --retention-days 30 --max-bytes 20971520
```

Private daily event files live under the user's Starfleet state directory, keyed by canonical worktree identity.
They contain timestamps, session and enrollment identities, lifecycle results, hashes and byte counts, without full standing text, checkpoint bodies or conversations.
The report separates reminder eligibility, hook emission, transport submission, explicitly evidenced receipt and checkpoint publication.
It reports missing history, uncertain compaction provenance and unsupported intervals as unknowns rather than invented success rates.
Publication proves that a note was validated and saved, not that its contents are accurate or sufficiently pruned.

Default retention is 30 days with a 20 MiB per-worktree cap.
Cleanup runs opportunistically during normal tool activity, removing only owned telemetry files.
An inactive worktree is cleaned at its next invocation, not by a new background service or an exact wall-clock deadline.
Keep separately retained investigation evidence under its own retention plan; telemetry cleanup does not own it.
The report can also read existing Hailing Frequencies idle-timer receipts for this worktree without copying continuation bodies or changing that database.
Do not downgrade to a runtime that ignores required session bindings after adopting them; reconcile affected owners and preserve their reviewed files before any rollback.
