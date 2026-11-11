# Hand off an orchestrator session

Use this guide when an authorized user explicitly requests a manual handoff for an exact orchestrator session.
The `replicator` command only prints this guide and does not perform a handoff.
Treat the checklist as the existing handoff procedure, not as a new approval step.

## Prerequisites and existing installations

Printing requires Python 3.10 or newer and the packaged guide, without native tools or configuration.
Performing a handoff requires CMUX, the applicable installed Claude or Codex integration, and the tools actually used by the outgoing seat.
Read the [Replicator README](README.md) for source and local installation commands.
If `replicator` is already installed, reuse it with `replicator guide --print`.
Reuse existing tool installations, exact-owner registrations, hooks and workers.
The successor's required receiving setup is part of the procedure; installation alone does not complete it.

## Confirm the request and mission

1. Confirm the explicit request, the outgoing session, the target worktree, the permitted successor route, and the current approval boundary.
2. Read the approved goal, current issue or roadmap, latest human decisions, active work, evidence, repair counts, and holds.
3. Ask the outgoing owner for the next concrete deliverable, current decisions, and any unresolved assumptions before repeating an investigation.
4. Preserve the approved goal and scope unless the authorized user changes them.
5. Reach a safe boundary before preparing the successor, and stop starting new product work at that boundary.
6. Let active commands finish or record their exact continuing owner and recovery path.
7. Update approved standing context and the roadmap through their existing owner workflows.
8. Record the current branch, head, uncommitted changes, active jobs, evidence, holds, and next action in the existing checkpoint or handoff packet.
9. Keep one product writer at a time, and do not stop a worker or cancel work merely to make handoff easier.

## Inspect the actual checkout and tools

Use the current native session to discover its CMUX worktree and installed tool entry points.
Reuse existing integrations and owner workflows instead of creating duplicate services or workers.
Read each applicable installed tool's help and maintained guide before choosing a state-changing command.
Use the installed Temporal Beacon skill, `temporal-beacon --help`, and `apps/hailing-frequencies/temporal-beacon.md` because that CLI does not provide `guide --print`.

Use supported inspection commands to inspect exact identities and records:

```sh
cmux identify --json
cmux current --json --limit 200
cmux tree --all --json
git worktree list --porcelain
git -C /worktree status --short --branch
git -C /worktree rev-parse HEAD
pattern-buffer tasks --status active
pattern-buffer task-status --task-id TASK_ID
pattern-buffer --cwd /worktree handoff-check --harness codex --session SESSION_ID
pattern-buffer --cwd /worktree enrollment-status --harness codex --session SESSION_ID
pattern-buffer --cwd /worktree checkpoint-status --harness codex --session SESSION_ID
hailing-frequencies status
warp-drive status
warp-drive inspect REQUEST_ID
temporal-beacon list
temporal-beacon inspect TIMER_ID
discord-watch status
discord-watch managed THREAD_ID
python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID status
python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID registrations
python3 /path/to/installed/runtime/lane.py --cwd /worktree coverage --summary
```

Replace every placeholder with a value observed for this handoff.
Use `--harness claude` for Claude sessions and `--harness codex` for Codex sessions.
Inspect the Policy Guard seat status for every attached checkout, then inspect coverage from each applicable checkout.
Do not infer a native identity from a title, a parent session, a saved profile, or a worktree default.
An empty result covers only the exact owner and scope queried.

## Record applicable resources

Query authoritative owner records instead of relying only on the outgoing agent's memory.
Check Pattern Buffer owner tasks and native child sessions, exact Hailing Frequencies bindings, standing context and checkpoint ownership, Warp requests and helpers, Temporal Beacon timers and occurrences, owned Agent Notify conversations, Discord Watch generations and queued replies, Policy Guard seat attachments, Git worktrees, and maintained plans.
Inspect only records owned by the outgoing session or resources explicitly in scope for this handoff.
Enroll only the exact successor when a documented tool requires enrollment for this handoff.
Do not blanket-enroll sessions or tools.
Use `discord-watch status` and `managed THREAD_ID` to inspect managed threads without receiving another session's replies.
Use `agent-notify wait POST_ID --timeout SECONDS` only for the exact conversation you own, and record any receipt it creates.
If the supported tools cannot enumerate a required owned resource completely, record the scope as blocked instead of claiming it is empty.

Keep resource rows in the existing checkpoint or handoff packet, not in a new ledger.
For each row, record the exact resource ID, owner identity, repository or worktree scope, authoritative query, required disposition, and verification evidence.
End each row with one of these outcomes:

- Transferred and verified.
- Completed or drained.
- Continuing custody verified for a named owner.
- Blocked with the unresolved reason.

Unknown ownership, an omitted applicable resource, unsupported required transfer, or unresolved human hold blocks the dependent transition and predecessor retirement.
Continue independent authorized work when it does not depend on the unresolved item.

## Review work and drift

After its own mission and rule reads, the successor records the accepted user outcome and the next concrete deliverable.
It states whether the inherited work advances that deliverable and whether the approach remains the simplest viable one under the actual constraints.
It checks relevance to the approved outcome and identifies repeated repairs, duplicate proof, disproportionate machinery, and material unresolved assumptions without presuming that the outgoing owner is wrong.
It checks the outgoing owner's answers and existing decisions before asking the user a question that has already been resolved.
It concludes with one disposition: continue, simplify within authority, investigate one bounded material uncertainty, or ask for a genuine scope or authority decision.
It records the judgment and material answers in the existing checkpoint or roadmap.
Only work or retirement that depends on a material unresolved issue stays on hold.
Do not rewrite the goal, weaken required proof, expand repair authority, or add a model, panel, or mandatory questionnaire.

## Start a successor through CMUX

Observe the outgoing session's actual provider, model, effort, and effective permissions before launch.
Use CMUX's existing agent-session integration for the same native harness.
Read `cmux new-surface --help` to confirm the installed command and its current options.

For example, use the observed workspace and exact worktree:

```sh
cmux new-surface --type agent-session --provider claude --workspace WORKSPACE_ID --working-directory /worktree
cmux new-surface --type agent-session --provider codex --workspace WORKSPACE_ID --working-directory /worktree
```

Do not launch a provider binary directly or invent CMUX lifecycle flags.
These examples create a candidate and do not carry its model, effort or permission settings.
Use the installed native controls that can preserve those settings, and verify the result before any transfer.
If using terminal scripts, invoke the installed CMUX wrapper or verified shell integration rather than an absolute provider-binary path.
Confirm that CMUX actually tracks the resulting native session before attempting owner enrollment.
Keep the successor's initial prompt short.
Identify its role, the approved mission and safety boundaries, and the installed `replicator guide --print` command.
From a source checkout, use `python3 apps/replicator/replicator.py guide --print`.
Let the successor discover the maintained procedure and independently inspect the handoff records.
Keep private launch recipes and transcripts out of the procedure.
After startup, inspect the actual native session and verify its provider, model, effort, and effective permissions.
Inspect flags added by the installed wrapper as well as the flags supplied by the caller.
Resolve any difference from the user's authorized permission and trust policy before transferring ownership.
Use the integration's normal permission dialogs when authorized.
A saved Model Preferences choice, command flag, or registration is configuration evidence and does not prove runtime parity or delivery.
Do not change shared profiles, settings, permissions, or another session to make parity appear to match.
If the successor's actual configuration differs or remains unknown, hold the dependent transfer and retain the outgoing session.
Claude evidence does not qualify Codex, and Codex evidence does not qualify Claude.

## Read and bind the successor independently

The successor reads the repository's `AGENTS.md`, `CLAUDE.md` when present, `README.md`, `CONTRIBUTING.md`, relevant component guides, and the installed PStack sections required by its task.
It reads the approved mission and current tracker from their authoritative sources.
It confirms completed reads and any missing guidance in its own native session.
The outgoing summary does not replace the successor's reads.

For the native attention and timer routes, the successor requires its own exact Hailing Frequencies enrollment.
Read `hailing-frequencies guide --print` and `pattern-buffer guide --print` before setup.
Match its actual native session to the current surface, workspace and generation in `cmux current`.
Use the live panel UUID that matches the session's `CMUX_SURFACE_ID` for Hailing Frequencies enrollment.
CMUX can also report a distinct durable `stable_surface_id`.
Confirm which identity the installed command accepts.
Reuse an unchanged existing enrollment, or self-enroll that exact successor:

```sh
hailing-frequencies enroll --harness codex --surface SUCCESSOR_SURFACE_ID --session SUCCESSOR_SESSION_ID --label SUCCESSOR_LABEL
hailing-frequencies status
```

Use `--harness claude` for Claude.
Do not use `--include-existing`, enroll a guessed identity, reset another owner's cutoff, or start another worker.
Verify the exact saved binding separately from actual native attention receipt.

The outgoing owner uses the existing Pattern Buffer release command only for the exact current enrollment:

```sh
pattern-buffer --cwd /worktree enrollment-release --harness codex --session OUTGOING_SESSION_ID --enrollment-id ENROLLMENT_ID --next-harness codex --next-session SUCCESSOR_SESSION_ID --evidence HANDOFF_REFERENCE
```

Before release, record the successor's actual identity and reconcile any owned timer or job that still needs the outgoing owner.
Confirm the supported predecessor cleanup path before releasing its binding.
Complete any required predecessor-owned operation that the retired binding would prevent, or retain verified continuing custody.
Do not restore a retired binding or bypass a STOP to finish cleanup.
An unenrolled successor can report an unassigned role and fail a checkpoint ownership check during preparation.
Distinguish those status results from a literal native standing-context STOP alarm.
If an alarm occurs, follow its supported binding-recovery instructions before dependent work.

The successor adopts that exact enrollment from its own session with reviewed standing context and a fresh checkpoint:

```sh
pattern-buffer --cwd /worktree enrollment-adopt --harness codex --session SUCCESSOR_SESSION_ID --enrollment-id ENROLLMENT_ID --context-file /path/to/reviewed-standing.md --checkpoint-file /path/to/reviewed-checkpoint.md --evidence HANDOFF_REFERENCE
pattern-buffer --cwd /worktree checkpoint-enroll --harness codex --session SUCCESSOR_SESSION_ID --file /path/to/reviewed-checkpoint.md
pattern-buffer --cwd /worktree handoff-check --harness codex --session SUCCESSOR_SESSION_ID
```

Use `--harness claude` for a Claude-to-Claude handoff.
Read the installed command help and [Pattern Buffer's session handoff guide](../pattern-buffer/docs/session-enrollments.md) before changing an enrollment.
The successor verifies its own task binding, standing context, checkpoint ownership, and actual receipt.
Do not copy the outgoing session's task binding, checkpoint receipts, permissions, or identity.
Follow any runtime-matched checkpoint setup command returned by adoption instead of assuming it copied the outgoing checkpoint metadata.
Create the fresh checkpoint file before adoption and use canonical absolute paths for both input files.
Use these required checkpoint headings once each, in this order:

```markdown
# Working checkpoint

## Current state

## Next action

## Decisions and uncertainty

## Active work

## Evidence

## Holds and guidance
```

Fill each section with current facts and the successor's verified responsibilities.
Inspect the checkpoint's actual reminder and timer state after enrollment.
Claude checkpoint setup can create an idle-warning timer.
Retain or cancel it according to the authorized reminder policy and verify its occurrences.
Codex has no automatic idle warning, so inspect its recorded state instead of applying Claude-only idle-warning options.

The successor checks its own Policy Guard seat and every applicable checkout attachment.
Use the documented receiving-owner workflow only from the exact authorized successor session.
After checking the exact seat, all attachments and handoff authority, its supported owner change is:

```sh
python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID rebind-owner --from-harness codex --from-session OUTGOING_SESSION_ID --confirm-handoff
```

Use the actual outgoing harness for `--from-harness`.
This rebind affects every attachment in that seat and requires fresh receiving evidence; it is not a single-worktree operation.
Record each attachment and receiving result in the same resource section.
Do not inherit the outgoing owner's receipt or claim that a configured hook proves actual receiving behavior.

## Preserve owned work and conversations

For each Warp request, inspect its exact request ID, owner, worktree, helper, launch intent, current phase, and completion route.
Transfer a live request only through the documented exact-owner command after verifying the target session and surface:

```sh
warp-drive transfer REQUEST_ID --harness codex --session SUCCESSOR_SESSION_ID --surface SUCCESSOR_SURFACE_ID --reason 'approved handoff' --from-session OUTGOING_SESSION_ID
warp-drive inspect REQUEST_ID
```

Read the installed Warp-drive guide before deciding whether a transferred request needs an attach.
Inspect the current helper and execution state first.
A running command can keep its helper while completion routes to the new owner.
Do not resubmit a request to imitate a transfer or attach a replacement helper during discovery.
For a managed Discord thread, inspect its exact owner generation and pending groups before transferring it from the successor's own session:

```sh
discord-watch transfer THREAD_ID --from-session OUTGOING_SESSION_ID
discord-watch managed THREAD_ID
discord-watch status
```

Preserve unread replies, uncertain submissions, and their exact owner generation.
Never read or acknowledge another session's messages.
Keep one existing Discord Watch worker for its state directory.

## Preserve Temporal Beacon timers

Within an otherwise authorized handoff, the exact incoming owner may recreate an owned timer and the exact outgoing owner may stop the old timer when direct transfer is unsupported.
This replacement policy is already authorized within an otherwise authorized handoff, so do not ask the user to repeat it or apply it to unrelated timers.
Inspect the outgoing owner's timer and occurrence history before scheduling or cancelling anything:

```sh
temporal-beacon list
temporal-beacon inspect TIMER_ID
temporal-beacon set --at '2030-01-01T12:00:00Z' --message 'Continue the approved task.' --request-id NEW_REQUEST_ID
temporal-beacon set --every 10m --until '2030-01-01T13:00:00Z' --message 'Continue the approved task.' --request-id NEW_REQUEST_ID
temporal-beacon cancel TIMER_ID
```

Read the installed `temporal-beacon set --help` before choosing a schedule.
Use `--at` for a one-time wake and `--every` for a recurring timer.
The current command accepts one timing mode per call, so it cannot directly combine an arbitrary next due time with a recurring interval.
Preserve the continuation, cadence, next due time, deadline, and request intent through supported commands.
Use a stable creation request ID for the incoming owner's intent and record it with the old and new timer IDs.
Verify the incoming timer's exact owner, schedule, and pending state.
Verify the outgoing timer's cancellation state from its exact owner.
Choose the operation order from the current pending occurrence and the installed API behavior for that timer.
Do not assume atomic transfer, reset cadence, or create a second continuation for an old wake that is queued, uncertain, or received.
Cancellation cannot retract input that the native tool already submitted.
If no supported sequence preserves the exact schedule and pending occurrence without a duplicate or gap, retain the outgoing owner and hold dependent retirement.

## Reconcile cutover and acceptance

Immediately before custody changes, repeat the authoritative queries for resources that could have arrived during preparation.
Reconcile new child tasks, reservation events, timer occurrences, replies, messages, worktree changes, and human decisions against the existing packet.
The successor independently verifies its receiving bindings and events after each supported transfer.

The outgoing owner gives a separate acceptance that the checkpoint is current, the next action is clear, and its remaining resources have verified dispositions.
The successor gives a separate acceptance that it read the mission and rules, matches the actual native configuration, owns the required bindings, understands the next action, and has verified its receiving responsibilities.
Do not treat a prepared command, registration, hook, guide print, or submitted wake as proof of acceptance or receipt.

Retire only the exact predecessor through its supported normal CMUX control after both acceptances and all required resource rows are resolved.
Do not force input, kill a process, clear a human hold, consume a foreign receipt, or remove shared services.
If ownership, permission parity, receipt, or required custody remains unknown, keep the predecessor available and hold only the dependent transition.

## Recover an interrupted handoff

After an interruption, inspect the exact task binding, native session, worktree, checkpoint, and affected resource records before acting.
If a launch, receipt, or transfer may have succeeded, reconcile its exact record instead of replaying it.
Do not launch a second successor while the first launch or its binding remains uncertain.
Keep the last verified owner and its safe resources in place while resolving the interruption.
If a required status or recovery command is unavailable, report the exact missing evidence to the current authorized operator.

Use the owning CMUX conversation or the configured Agent Notify thread for operator help.
Ask one concrete question with the exact affected scope, current safe boundary, observed status, and decision needed.
Do not include credentials, private transcripts, or another owner's message body.

## Record the agreed validation

Agree on validation for the authorized change or real handoff before creating test sessions or traffic.
Use the same maintained procedure for each supported harness, and record each route's actual results and remaining gaps.
Keep observations, exact commands, actual parity, separate acceptances, and custody outcomes in the existing checkpoint or handoff packet.
Configuration checks and partial custody observations leave the full handoff unproven.

When the user authorizes a disposable pilot, bound its identities, worktrees, traffic, cleanup, and evidence location.
A finite timer, reservation, second worktree, and omitted or stale packet item can exercise relevant custody paths when those resources are in scope.
Reconcile an omitted required resource before predecessor retirement.
Do not manufacture a workload, repeat an unchanged test, or add a rehearsal merely to complete a checklist.
An authorized real handoff can supply operational evidence when the user selects that validation plan.

Preserve the source and evidence when cleaning up owned test resources.
Use each tool's supported removal controls and verify terminal timer and job states before removing an attention route.
If a tool retains an active enrollment record after its isolated worktree is disabled, report both facts and preserve the history.
Inspect the exact native input and process state before ordinary exit, then verify the process ended before closing its tab.

Record which behavior remains untested, including native delivery, interception, or recovery paths that the agreed run did not exercise.
Installation only makes the guide available.
A later opt-in configurable trigger with an active-work budget remains a future increment and must invoke this same procedure.
