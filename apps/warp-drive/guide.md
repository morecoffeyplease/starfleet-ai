# Warp-drive agent guide

Warp-drive keeps local CPU work in a small, fair reservation queue.
You prepare the environment, choose the command and worker bounds, and own follow-up and cleanup.
The scheduler only grants reservations and records observations.
The run helper launched by your agent executes the command and reports its exit status.

## Before using it

Use the existing installed `warp-drive` command when it is already available.
Otherwise, from the Starfleet AI checkout run `python3 apps/warp-drive/install.py`, then run `warp-drive init`.
Installation copies an immutable runtime and printable guide to your user account and writes one owned launcher.
It does not start a scheduler or change Claude, Codex, CMUX, Hailing Frequencies, or launch services.
Use Python 3.9 or newer on macOS or another POSIX system with `fcntl`.

For guarded completion prompts, reuse your exact Hailing Frequencies enrollment for this session and surface.
Print its guide with `hailing-frequencies guide --print` and follow that component's setup if enrollment is missing.
An engineer notification uses the existing enrolled `warp-drive-engineer` target and native CMUX inbox.
Run the dedicated scheduler with `--send` to dispatch completion and engineer messages from its own native terminal.
The scheduler's `CMUX_SURFACE_ID` must identify that terminal and differ from enrolled recipient surfaces.
This does not start or configure Hailing Frequencies.
The Hailing worker's own send setting, exact bindings, and human-input guard still determine whether an inbox reminder is submitted.

## Start the scheduler

Run one sending scheduler in a dedicated native CMUX terminal:

```sh
warp-drive scheduler --send
```

The scheduler samples host signals every two seconds, grants at most one request per eligible worktree per round, and never starts or kills product commands.
It exits if another scheduler already owns the same state directory.
It does not start automatically when the tool is installed.
Running `warp-drive scheduler` without `--send` samples and grants work but leaves all inbox messages pending.

Use `warp-drive status` to inspect current requests and the latest sample.
Use `warp-drive doctor` to inspect sample freshness, stale helpers, the optional Hailing launcher, and engineer enrollment.
Doctor counts pending and unavailable completion wakes separately from uncertain sends.
Use `warp-drive inspect REQUEST_ID` to see one completion's wake status and event history.
Status and inspect are passive observations and do not attach to or replace a helper.
Use `status` or `inspect` for the current point-in-time admission snapshot; initial `run` output cannot predict a later scheduler decision.
The snapshot shows host sample freshness, observed and projected CPU, memory pressure, estimated active managed contributors, and current candidate constraints.
Active managed commands are estimated contributors, not proof that each command caused host load or must finish before a request can start.
When policy identifies an active aged escape or a selected protected backfill target, the snapshot includes that request ID as the specific blocker, and a selected target remains waiting until a later admission pass.
Host capacity, stale sample, memory pressure, and startup observation restrictions do not assign a causal reservation ID.
The turn context shows current round, each ready worktree's manual order and served round, and whether it is eligible in the selection round if the scheduler runs now.
The snapshot is not a FIFO rank, reservation promise or ETA, and a later sample or scheduler pass may change it.
Status and doctor list completed request IDs whose owner lacks an exact Hailing route and give the supported enrollment or owner-authorized transfer action.
The result remains durable, and the existing sending scheduler retries it after the owner route is available.
Fresh configuration alone does not prove that a scheduler, prompt, native receipt, or model is active.

## Submit and run one request

Use the exact session and surface identities observed for your current Claude or Codex owner.
Do not use a workspace title or an identity copied from another session.
Use a parent's stable, exact enrolled completion recipient only when that parent explicitly delegates it for this request.
That delegated identity is recorded as the request's current owner and recipient; handoff later still requires an explicit `warp-drive transfer`.

```sh
warp-drive run --key UNIQUE_REQUEST_KEY \
  --harness codex --session SESSION_UUID --surface SURFACE_UUID \
  --size light --family lint --workers 1 --expected-runtime 600 \
  --repo /absolute/path/to/repository --worktree /absolute/path/to/worktree \
  -- bun run lint
```

Use `--size heavy` for a command with larger bounded CPU demand, and report the actual worker count.
Submit one meaningful CPU phase per reservation, with the actual number of CPU workers that can run simultaneously during that phase.
Do not sum worker counts across serial phases.
Within one checkout, make only one CPU-intensive request eligible at a time when commands share generated files, caches or a database, and enable the next dependent phase after its predecessor finishes.
Independent requests and batches may run concurrently across checkouts when their shared inputs are safe.
The initial fallback estimates are one core per light worker and four cores per heavy worker, capped to the configured target.
These estimates are admission signals and do not throttle the command or prove actual utilization.
The target defaults to 95 percent and can be changed by the configured operator with `warp-drive config target 0.90 --actor OPERATOR_ID`.
New grants stay in projected load until a host sample arrives at least ten seconds after their helper start or grant time.
Fitting work receives its ordinary worktree turn before an aged request protects its worktree from heavy backfill.
After other currently serviceable worktrees have received their turns, one eligible aged heavy request may hold conflicting heavy backfill when releasing active managed work can restore the existing escape threshold.
During that protected turn, at most one light backfill may be active at a time so light work continues while repeated grants do not refill every slot.
After managed work drains, protection can persist while a fresh low current sample waits for the existing recent CPU mean to settle, and replacement light work pauses during that short observation gap.
A held, canceled, stale or actively served target worktree cannot hold this protection.
With at least two minutes of earned eligibility credit, a fresh sample and observed projected load at or below half the heavy budget, up to two aged requests may take oversized turns alongside ordinary reflected heavy work.
The scheduler caps active aged escapes at two and preserves the ten-second unreflected-grant estimate, startup spacing and memory-pressure guard for each admission.
When an aged escape is active, another aged waiter does not start a competing protected drain turn.
A second aged request may pass through the same observed-headroom guard, while further oversized work waits for headroom or an available escape slot.
Fitting heavy and light work continue under their existing admission rules.
The scheduler does not preempt commands, and this soft target can temporarily exceed the CPU target without a hard wait-time guarantee under continuous external saturation or indefinitely running commands.
The light reserve applies when light work is ready.
A heavy command admitted while no light request is ready can consume that headroom until it exits, so a later light request may wait for that non-preemptible command.

Use a stable request key for a client retry.
An identical retry attaches to the same request, while changed command details under that key are rejected.
Use a new key only when you deliberately intend a new product-command run.

To let the helper continue if your command-wait surface times out, add `--detach`.
This starts a separate POSIX session and writes output to the private state directory's `logs` folder.
Product commands inherit the invoking CLI's umask for newly created files and directories.
Warp-drive state and detached helper logs remain private.
The installed scheduler must still be running for a waiting helper to receive a grant.
The separate-session behavior is the supported software launch path, but survival through each native Claude and Codex owner-session exit still needs an observed pilot.

Observe or recover without resubmitting the command:

Use `status` or `inspect` for passive observation.
`attach` is a recovery action, not a status query.
A waiting attach can replace the current helper, become the helper that launches the product command, and send command output to this process.
If this process's stdout is redirected away, for example to `/dev/null`, product output can be lost.
Use `attach --detach` to run recovery separately and append output to the private request log.

```sh
warp-drive status
warp-drive inspect REQUEST_ID
warp-drive attach REQUEST_ID --harness codex --session SESSION_UUID --surface SURFACE_UUID
```

Attaching after a recorded launch intent never starts the product command again.
If the command's outcome is uncertain, inspect it and reconcile with the responsible owner.
Unknown outcome is not evidence of failure and is not permission to replay.

## Batch and queue controls

`warp-drive batch --file requests.json` accepts an array of independent request objects.
Every item needs `key`, `harness`, `session`, `surface`, `size`, `family`, `workers`, `expected_runtime`, `worktree`, and `command` fields.
The batch is atomic, and each item must attach its own helper before it becomes eligible.
Batch size grants no extra turns.

Use `warp-drive delay REQUEST_ID --seconds 300` to delay work, `--hold` to hold it, and `--resume` to make it eligible now.
These preserve earned waiting credit but do not accrue credit while held, delayed, or disconnected.
Use `warp-drive cancel REQUEST_ID` only before its command starts.
Warp-drive does not stop a command that has already started.
Foreground `run` and `attach` return the product command's exit status.
Waiting timeouts, cancelled requests, and uncertain launches return nonzero with a JSON status.

Transfer ownership explicitly during handoff:

```sh
warp-drive transfer REQUEST_ID --harness claude --session NEW_SESSION_UUID \
  --surface NEW_SURFACE_UUID --from-session OLD_SESSION_UUID \
  --reason "handoff to current orchestrator"
```

The request ID, waiting credit, priority, and current execution remain attached to the same reservation.
The new owner must attach before a waiting request is eligible.

## Manual priority

Only the configured operator or an explicitly listed delegate may change worktree order or set a bounded boost.
An authorized actor must supply a reason for every boost.
For example:

```sh
warp-drive order /absolute/path/to/worktree --before /absolute/path/to/other-worktree \
  --actor OPERATOR_LABEL --revision CURRENT_ORDER_REVISION
warp-drive boost REQUEST_ID --actor OPERATOR_LABEL --reason "release blocker"
```

Use `warp-drive config show` or `warp-drive status` to inspect the current operator, delegates, target and order revision.
The authority is cooperative same-user configuration, not an operating-system security boundary.
Repeated boosts do not add turns.

## Completion and exceptional outcomes

The helper records completion once, releases its accounting lease, and leaves a durable pending completion event.
Finished events record waited-child CPU seconds and direct-child peak RSS bytes where POSIX child accounting and platform units are supported.
Accounting covers the direct child and descendants it waited for, while excluding Docker, daemons, and orphaned processes.
Peak RSS is the maximum for that waited child set, not an aggregate across unrelated work.
Unsupported accounting, spawn failures, and owner reconciliation retain explicit missing-data reasons.
The dedicated scheduler resolves the current owner and queues one native CMUX inbox message to that owner's exact Hailing-enrolled surface.
Each scheduler sampling pass attempts at most one completion and one engineer incident message, so recovered backlogs drain at the scheduler cadence.
The sender stays the scheduler terminal, allowing Hailing's self-message filter to accept same-owner completion.
CMUX delivers native agent messages through recipient hooks without typing into the terminal.
The existing Hailing worker may also prompt from its monitored inbox, applying its exact session, surface, composer, lifecycle, and human-input checks.
Warp-drive records that CMUX queued the message, not that Hailing prompted the owner or that a model received or read it.
The scheduler reads the current owner at dispatch, so a transfer during execution routes the message to the new owner without another attach.
Unavailable delivery stays pending for a later sending scheduler pass after enrollment or CMUX capability recovers.
If a queue attempt is uncertain, inspect the recorded event and do not send a duplicate manually.

For a launched reservation whose helper stopped before recording completion, use `warp-drive reconcile REQUEST_ID --harness claude --session SESSION_UUID --surface SURFACE_UUID --outcome unknown --evidence "what was observed"` to preserve the uncertainty.
Use `--outcome succeeded` or `--outcome failed` only after obtaining owner evidence for that result.
Record an engineer investigation with `warp-drive incident INCIDENT_ID --evidence "source" --action "what was done" --outcome "observed result"`.
Resolve it only after the observed condition has cleared, by adding `--resolve` to that command.

The scheduler records uncertain launches, overdue runtimes, and prolonged ready work without available headroom as incidents.
The sending scheduler queues one native CMUX message to the existing enrolled `warp-drive-engineer` recipient for each open incident.
It attempts at most one engineer incident message per sampling pass.
The queued message is not proof of a Hailing prompt or engineer receipt.
Missing engineer enrollment or CMUX capability stays eligible for a later send pass.
An uncertain external send is retained and not replayed automatically.
The engineer records evidence, action, and observed outcome through the incident event record, and does not kill or replay product commands.
Engineer review is on-demand: inspect a surfaced exception once, then return ordinary diagnosis and follow-up to its owner.
Revisit only when new evidence arrives or someone explicitly requests another review.

Commands are stored in owner-only local state for this machine.
The database and detached helper logs are under `~/.local/state/starfleet-ai/warp-drive/` by default.
Host rows contain raw CPU, memory pressure, swap, thermal availability, and sample timestamps.
There is no automatic retention policy.

## Verification

Run `python3 apps/warp-drive/verify.py` from the source checkout.
Run `warp-drive guide --print` from any directory to read the same guide shipped with the installed runtime.
Use the native pilot to observe helper survival and actual Hailing prompt reception in the supported Claude and Codex sessions.
Local verification and configured notification state do not establish those native delivery observations.
