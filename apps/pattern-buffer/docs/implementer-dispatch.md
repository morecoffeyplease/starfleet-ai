# Implementer dispatch and task replay

Use this workflow when an orchestrator launches a separate Claude or Codex implementer session in a temporary worktree.
It restores shared context, implementer context, and the exact active task brief at startup, resume, and after compaction.
It does not infer a lane from branch names, inherit an orchestrator's role, or enroll native in-process subagents.

## Ownership

The lane's existing `.context-injector/context.md` remains the approved source for shared and role instructions.
Pattern Buffer reads that file in place and never copies or edits it during dispatch.
The context owner or an explicitly authorized delegate approves mission, roadmap identity and standing-policy changes.
An assigned-item pointer may be updated only when the context owner has granted that narrow maintenance permission; it does not expand task scope.
The orchestrator supplies and explicitly updates task briefs within the work already authorized by the user.
Task updates do not grant new scope or supersede later user instructions.

## Install the updated runtime

If a hook belongs to a different supported installation, dispatch remains refused until the CLI matches that installed bundle.
When a compatible installed entrypoint is verified, the refusal prints a complete command that preserves the original dispatch arguments.
Run the reported compatible command when one is available.
If installation refuses an unowned or damaged entrypoint, preserve that runtime and run the reported same-settings recovery command into a new empty runtime.
The recovery install updates only the selected harness's owned hook command and leaves worktree enrollment and checkpoint files in place.
The existing PATH launcher remains unchanged; use the new runtime's `pattern-buffer` launcher by explicit path for commands against the recovered runtime.
Review the harness's normal reload and trust controls after changing the hook command.
An orchestrator handoff to an already-installed harness usually needs the [read-only handoff check](session-enrollments.md#read-only-handoff-check), not another installation.
From this app's directory, install the updated adapter for each harness that needs it:

```sh
python3 pattern_buffer.py install --harness codex
python3 pattern_buffer.py install --harness claude
```

Use the harness's supported controls to review and trust the updated definition where required.
Codex's handler now disables output spilling with `additionalContextLimit: 0`, while the app strictly caps combined output at 24 KiB.
The dispatcher refuses a stale installed runtime or an unrecognized/missing/duplicated owned hook.
An install refreshes recognized aliases already present inside its selected runtime directory, including aliases for the other supported harness.
It does not search for, create or update aliases in a separate runtime directory.
This preflight checks the settings file and runtime, not the harness's effective feature flags, trust state, or model reception.
Keep hooks enabled and trusted; do not launch with bare/safe modes that omit hooks.
For custom test settings, pass `dispatch --settings PATH` and also configure the harness itself to load that same file.

## Launch an implementer

Use absolute paths for the lane owner, target worktree, and brief.
The owner must already be initialized and enabled.
The target must be in the same Git repository as its owner.

```sh
python3 /absolute/path/to/pattern_buffer.py --cwd /absolute/path/to/child dispatch \
  --owner-worktree /absolute/path/to/lane \
  --harness codex \
  --task-file /absolute/path/to/brief.md \
  --receipt-file /absolute/path/to/unique-launch.json \
  -- codex exec -C /absolute/path/to/child \
  -m MODEL -c model_reasoning_effort=high -s workspace-write \
  -o /absolute/path/to/result.md - \
  > /absolute/path/to/run.log 2>&1
```

Choose the model, sandbox, and remaining harness flags according to the task's existing authorization.
The command preserves the `codex` executable selected by PATH, including an existing CPU routing shim.
It adds `--no-daemon` to keep the launch ticket in the session's process environment.
It does not create a CPU wait loop or schedule heavy work itself.
The command's working directory must match `--cwd`; use a separate `-C DIR` or `--cd DIR` argument.
Dispatch rejects alternate worktree creation, resume/fork/review subcommands, and mismatched working directories.
For a Codex worker launched by the matching Model Preferences version, use its [supported continuation command](../../model-preferences/README.md#continue-an-exited-codex-worker) to preserve model, effort and task ownership.
Bare native Codex resume can select native defaults instead of the launch choice.
Pattern Buffer restores task context; it does not select or pin the resumed model.
For other authorized resume paths, apply the exact intended model and effort explicitly and retain the original task binding.

The task file supplies stdin, so omit the old `< brief.md` redirection.
Existing stdout/stderr redirection and Bash background-job tracking can remain unchanged.
Dispatch replaces its process with the harness, preserving exit status and normal signal behavior.

For Claude, use a plain-text print session:

```sh
python3 /absolute/path/to/pattern_buffer.py --cwd /absolute/path/to/child dispatch \
  --owner-worktree /absolute/path/to/lane \
  --harness claude \
  --task-file /absolute/path/to/brief.md \
  -- claude -p --output-format json
```

Dispatch supplies a plain-text brief on stdin, so streaming-JSON input mode is unsupported in this launch command.
Both examples start a fresh session; they do not attach a new assignment to a running agent.
This implementation supports the implementer role only for dispatch.
Do not automatically migrate review sessions to this workflow merely because their command also uses `codex exec`.

## Enrollment and session binding

Dispatch explicitly enrolls an unregistered child by storing its owner pointer in the child's private Git directory.
It creates no `.context-injector` directory in the child and makes no tracked worktree edits.
An existing local enrollment with a different context source is rejected rather than overwritten.
Dispatch in the owner worktree itself is also supported and leaves its orchestrator default unchanged.
Prefer a separate temporary worktree for concurrent code or proof work: separate task bindings do not isolate files, commits, generated artifacts or checkout resource locks.
An orchestrator commit in an owner-equals-child checkout can legitimately trigger the child's concurrent-change stop rule.
Keep shared Git metadata and CPU socket access within the task's existing sandbox authorization; the dispatcher does not grant missing access or choose a broader sandbox.

Each launch gets a unique task ID and immutable, content-addressed snapshot of the exact brief.
The ID is printed to stderr before launching and is visible in the run log.
An optional JSON receipt is written before the harness starts; it survives even if the child never writes a report.
The startup hook binds the launch ticket to the actual harness session ID as implementer.
A second session cannot claim that ticket, including a nested process that inherited its environment.
Tickets unclaimed for ten minutes are rejected; dispatch after an external admission wait has finished, or prepare a new ticket if that delay expires.
The environment variable is used only for the initial claim, not as a continuing role assignment.
Later compactions and resumes look up the persisted session binding without requiring that environment variable.

Concurrent tasks may share a worktree while retaining distinct session/task bindings.
The orchestrator section is excluded, and an unregistered sibling is not implicitly enrolled.
Disabling the child stops its future injections.
Disabling the owner also stops linked-child injections.
Changing or deleting an owner registration causes a visible error rather than guessing a replacement source.
`init` will not silently detach an enrolled child or create a stale copy of the lane's approved text.

The task snapshot is an exact repeat of the assignment, not an automatically generated summary or progress checkpoint.
Replaying it does not restart the task or discard progress.
References to changing task state should still be verified against their authoritative sources.

## Task receipts and discovery

Pass `--receipt-file PATH` before the dispatch command's `--` separator to save a launch receipt.
The parent directory is created if needed; the receipt is published atomically with owner-only permissions and never overwrites an existing file or symlink.
Choose a unique path per dispatch, separate from the brief, child-authored report and harness `-o` final-message file.
The receipt contains `version`, `receipt_type`, `note`, `task_status_command`, `task_id`, `owner_worktree`, `worktree`, `harness`, `session_id`, `brief_sha256`, `created_at`, and initial `status`.
It contains no brief contents.
`receipt_type: "immutable_dispatch_snapshot"` and `note` label it as a snapshot written before launch.
It records a pending launch with a null session; it is not updated when the hook binds the session, the brief changes or the task finishes.
`task_status_command` is a shell-quoted command for looking up the current task in its child worktree.
Dispatch also prints the receipt path, snapshot warning and live lookup command on stderr, preserving harness stdout and prepare-only JSON.
A receipt proves neither successful process launch nor model delivery.
Use `task-status` or `tasks` for current metadata.
If a later preflight or launch fails, a pending task can remain; inspect it and explicitly finish an abandoned assignment.

List saved assignments belonging to an owner:

```sh
python3 pattern_buffer.py --cwd /absolute/path/to/lane tasks
python3 pattern_buffer.py --cwd /absolute/path/to/child tasks --owner-worktree /absolute/path/to/lane --status active
```

`tasks` emits JSON with `owner_worktree`, `tasks`, and `errors`.
Each task includes its ID, child worktree, current brief hash, bound session and pending/active/finished lifecycle state; brief text is omitted.
Optional `--status pending|active|finished` filters the records.
Discovery reads existing task manifests in the repository's registered Git worktrees, including owner-equals-child tasks; it creates no new registry or daemon.
A missing owned worktree or unreadable/corrupt record appears in `errors`, retains any available task rows, and makes the command exit 1 rather than claim a complete listing.
Deleted or pruned worktree metadata is outside discovery, so this is not a permanent history ledger.
Nothing here identifies a latest task safely across parallel children; finish only the explicit task you inspected.
The child report and final harness message can overwrite each other if both target the same path, so keep their destinations distinct.

## Inspect, update, and finish

Use the target worktree and the task ID printed in its launch log:

Task lookup and mutation commands use the child's worktree, even when its orchestrator runs in the permanent owner worktree.
Find that path in the immutable receipt's `worktree`, its `task_status_command`, or the owner's `tasks` rows, then pass it as `--cwd`.
An absent task in the selected worktree produces this recovery guidance before creating a task lock; it does not search other worktrees or change an assignment implicitly.

```sh
python3 pattern_buffer.py --cwd /absolute/path/to/child task-status --task-id TASK_ID
python3 pattern_buffer.py --cwd /absolute/path/to/child preview --harness codex --session SESSION_ID
python3 pattern_buffer.py --cwd /absolute/path/to/child update-task --task-id TASK_ID --task-file revised-brief.md
python3 pattern_buffer.py --cwd /absolute/path/to/child finish-task --task-id TASK_ID
```

The normal session preview includes its active task.
A forced `preview --role` is a role-only preview and does not simulate a session/task binding.
Editing the original dispatch brief on disk does not change its saved snapshot.
`update-task` explicitly publishes a new snapshot for the same assignment, picked up at the next startup/resume/compaction hook.
Its JSON result includes `delivery_state: "pending_recovery"`, a `delivery_note` explaining that saving does not interrupt current work or confirm model reception, and an `urgent_change_guide` link to the procedure below.
These fields describe the update response, not a persisted acknowledgment or a promise that another hook will occur.
An active child without another hook can continue on its old brief indefinitely; an update is not a live message, acknowledgment or immediate scope change.
Owner standing-context edits are also read fresh at the next hook, including for already-dispatched children; they do not require a fresh dispatch to take effect after recovery.
Keep revisions within the existing task authorization; a separate new assignment should use a fresh dispatch/session.
`finish-task` suppresses task-brief replay and injects a finished notice asking the agent to await a new assignment.
Finished tasks cannot be revived with `update-task` or by reusing an inherited launch ticket.
Mark an abandoned pending dispatch finished as well.
The tool retains snapshots and metadata for inspection; it does not garbage-collect them automatically.

A pending task means a launch ticket exists, and active means the hook bound a session.
Neither status proves that the workload started successfully, passed verification, or completed.
The orchestrator must check actual output before marking the task finished.
Existing conversation text cannot be retracted, so a completed brief may remain in history even though the hook stops replaying it.

## Urgent scope changes

Use this procedure only within existing authority to stop and relaunch the assigned work.
If that authority is absent, ask the owning orchestrator or context owner before stopping it.

1. Preserve the task ID, worktree, brief hash, output/log paths, current progress and all admitted or queued CPU ticket IDs.
2. Stop only the identified child process using the approved orchestration path; do not kill unrelated jobs or assume that ending a client cancels a CPU ticket.
3. Inspect the retained output, because an interrupted child may never write its final report, and identify partial edits or commits before proceeding.
4. Mark the old assignment finished as abandoned, then dispatch a fresh session with a narrow revised brief and a new receipt.
5. Carry forward the existing authorized CPU tickets and their attach instructions; do not duplicate queued work merely to change the child's brief.
6. Verify the replacement receives the revised assignment and report incomplete proof honestly.

Do not use `finish-task` alone as a process stop: it changes future replay, not execution already in flight.
Preserve any human hold; restarting a worker does not authorize scope that was awaiting approval.

## Limits and failure behavior

Shared/role context remains limited to 8,192 UTF-8 bytes including its header.
Each exact task brief is limited to 16 KiB and combined injection to 24 KiB.
Oversized or malformed inputs are rejected explicitly rather than summarized or silently truncated.
Private task manifests, snapshots, and session bindings live in the child's Git metadata, separate from tracked source files.
The dispatch command logs the task ID rather than the brief itself.
The hook emits the selected context and active brief on stdout for the harness, and session preview prints that same context for inspection.
The chosen harness may also include prompt content in its logs, so treat previews, hook output, and harness logs as potentially sensitive.

Hook failures for a known dispatched session emit `continue: false` instead of merely advisory output, including recovery failures after the launch token is absent.
This depends on the harness honoring its hook result; disabled/untrusted hooks cannot enforce any policy.
Ordinary non-dispatch sessions retain advisory hook-error behavior.
Forked or relocated sessions do not automatically inherit task bindings.
Native SubagentStart coverage remains outside this implementation.
The supported local test path covers simulated command execution and task replay.
Live harness delivery remains a separate observation and is not established by these source tests.
Inspect authorized model-visible records for delivery evidence; ordinary run logs, printed IDs and successful dispatch/finish commands alone do not establish what the model received.
This public source tree does not include private pilot receipts or acceptance snapshots.

## Verification

Run the canonical local suite documented in the app README.
Dispatch tests cover linked-worktree enrollment, exact replay, concurrent tasks, role separation, owner disable, explicit update/finish, expired tickets, cross-repository rejection, inherited-token rejection, stale-runtime rejection, and actual command/stdin execution through a fake harness.
Any live harness observation requires a separate bounded run and should be retained outside the public source tree.
Do not force a live product workload or fill a context window just to test injection.
