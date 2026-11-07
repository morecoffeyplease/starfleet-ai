# Warp-drive

Warp-drive is a compact local CPU reservation queue for agent-owned commands.
Agents prepare environments, choose commands and worker bounds, inspect results, and own cleanup.
Warp-drive orders waiting work, records host observations, and provides a thin agent-launched run helper.
The scheduler never executes, retries, kills, or repairs product work.

Start with the [agent guide](guide.md), available from any installed copy as `warp-drive guide --print`.

## Scope

The implementation uses Python's standard library and one private SQLite database.
CLI mutations use short transactions.
One scheduler process owns a filesystem lock and samples host CPU and pressure every two seconds.
There is no socket server, message broker, process supervisor, environment manager, benchmark runner, dashboard, retention job, report generator, or automatic tuning.

Reservations have waiting, reserved, finished, and cancelled states.
They retain a stable request key, repository and checkout identity, current owner, workload family, size, worker count, expected runtime, eligibility timestamp, earned waiting credit, priority, launch claim, outcome, and events.
An optional finite positive CPU estimate and nonempty basis may be recorded for a request, while rows without those fields retain the original fallback behavior.
Worktree identity includes the canonical path and filesystem device/inode, so a recreated checkout receives a new identity.
Command arguments and local measurements stay in the owner-only state directory.
Finished events record waited-child CPU seconds and direct-child peak RSS bytes where POSIX child accounting and platform units are supported.
Accounting covers the direct child and descendants it waited for, while excluding Docker, daemons, and orphaned processes.
Peak RSS is the maximum for that waited child set, not an aggregate across unrelated work.
Unsupported accounting, spawn failures, and owner reconciliation retain explicit missing-data reasons.

Each waiting request needs an attached helper with a fresh heartbeat.
The scheduler grants one request per eligible worktree per persisted round and advances after a capacity skip so an unfit request does not pin the round.
Among eligible unserved worktrees, explicit boosts come first, followed by the least recently served round and manual order, with waiting credit, request sequence and request ID breaking remaining ties.
New worktrees start one round behind the current round so they can take a turn immediately; existing never-served rows retain round zero as a finite first-service debt until they are served.
Manual ordering has a revision, and authorized boosts are one bounded level within the worktree's normal turn.
Light work bypasses heavy startup pacing and can use reserved headroom.
A fitting request gets its ordinary worktree turn even when an aged large request is waiting.
After other currently serviceable worktrees have received their turns, one deterministically selected heavy request that has waited at least 120 seconds can protect its worktree from fitting heavy backfill when releasing active managed work can restore its existing escape threshold.
Protected aged-target selection and retention continue to use earned waiting credit and the existing protected-turn rules.
The scheduler does not protect a worktree already receiving active service, and held, canceled, or stale requests stop being protection targets automatically.
During a protected turn, conflicting heavy backfill is withheld and at most one light backfill may be active at a time.
After managed work drains, the target can stay protected while a fresh low current sample waits for the existing recent CPU mean to settle; replacement light work pauses during that short observation gap.
Other fitting work remains eligible when no managed work can free enough headroom for the protected target.
At least two minutes of earned eligibility credit and a fresh sample with observed projected load at or below half the heavy budget allow up to two aged requests to take oversized turns alongside ordinary reflected heavy work.
The scheduler caps active aged escapes at two and preserves the ten-second unreflected-grant estimate, fresh-sample requirement, startup spacing and memory-pressure guard for each admission.
When an aged escape is active, another aged waiter does not start a competing protected drain turn.
A second aged request may pass through the same observed-headroom guard, while further oversized work waits for headroom or an available escape slot.
Fitting heavy and light work continue under their existing admission rules.
This soft target can cause temporary overload, and it gives no hard CPU or wait-time bound under continuous external saturation or indefinitely running commands.
Admission estimates are signals and do not throttle commands.
The light headroom reserve applies while light work is ready.
A heavy command admitted while no light work is queued can use that headroom until it finishes, so a later light request may wait for that non-preemptible command.

Admission uses fresh observed CPU plus conservative estimates for grants whose startup has not yet had time to affect a sample.
The recent CPU mean and latest sample, memory severity, heavy startup spacing, light headroom, and the configurable target inform decisions.
New grants remain included in projected load until a sample arrives at least ten seconds after the helper's recorded start or grant time.
Warp-drive records memory severity as `normal`, `warning`, `critical`, or `unknown`.
On macOS, sysctl flags 1, 2, and 4 map to normal, warning, and critical.
Unsupported platforms, failed reads, and unrecognized flags produce unknown.
Legacy generic `pressure` samples remain unknown and block new work until a fresh severity-aware sample arrives.
During warning, Warp-drive pauses heavy starts and allows at most one managed light reservation at a time.
A light grant counts before its helper starts, and the light must still pass the fresh-sample and CPU headroom checks.
Critical and unknown pressure block every new start.
Existing commands continue without preemption when pressure changes.
If several light reservations are active when warning begins, Warp-drive lets them finish and grants no new light until none remains active.
A fresh normal sample restores the ordinary admission rules.
Do not restart a pre-v8 runtime as a pressure-policy rollback because it may treat unknown pressure as normal.
If the v8 pilot needs a rollback, stop the owned scheduler to pause new grants while active commands finish.
Resume admission only with a compatible runtime that blocks unknown pressure.
The default target is 95 percent.
CPU estimates are not hard quotas, and below-target utilization does not establish a scheduler fault.
Worker counts describe independent workload execution slots that can run at the same time, while backend thread counts and internal tool parallelism are separate details.
A browser task with three independent browser workers has three workload slots, a build or test command should use its actual parallel task count, and one serial GPU inference job has one slot even if its backend uses many CPU threads.
`--cpu-cores` and `--cpu-basis` may be supplied together to declare an optional CPU demand estimate and its evidence or bound basis.
The scheduler caps the requested estimate at the configured target and preserves the existing heavy-work light-headroom reserve.
The requested estimate and basis appear in reservation and admission diagnostics, while `estimated_cores` records the effective estimate used by admission and grant telemetry.
An estimate is an admission signal, not a throttle or a peak-use guarantee, and a measured historical average does not establish a peak bound.
Without both fields, the light and heavy fallback estimates remain one core per light worker and four cores per heavy worker, capped to the target.
The compatible scheduler reports policy version v8 after a scheduler pass, and new explicit estimates are rejected while the state reports another or unknown version.
The client does not change the policy setting or start a scheduler to force compatibility.
Before rolling back to a scheduler that predates v7, allow active explicit-estimate commands to finish and let waiting requests run under v8 or v7, or cancel them.
Restore the older scheduler only after no waiting or active request has an explicit estimate because that scheduler will use the generic fallback for those rows.
Unavailable thermal data remains explicitly unavailable.

## Storage and commands

Default state lives under `~/.local/state/starfleet-ai/warp-drive/` with mode 0700 for the directory and 0600 for the database.
The default install copies immutable source and guide files under `~/.local/share/starfleet-ai/warp-drive/` and writes the owned launcher `~/.local/bin/warp-drive`.
The installer starts no scheduler or notification worker.

The core CLI includes:

- `init`, `submit`, `batch`, `run`, `attach`, and `scheduler`.
- `status`, `inspect`, `cancel`, `delay`, and `transfer`.
- `order`, `boost`, `config`, `doctor`, `incident`, `reconcile`, and `guide --print`.

Submit one meaningful CPU phase per reservation and report how many CPU workers can run simultaneously during that phase.
Do not sum worker counts across serial phases; within one checkout, make only one CPU-intensive request eligible at a time when commands share generated files, caches or a database, and enable each dependent phase after its predecessor finishes.
Independent requests and batches may run concurrently across checkouts when their shared inputs are safe.
Use your own observed harness, session and surface by default; use a parent's stable, exact enrolled recipient only when that parent explicitly delegates it for this request.
Naming a delegated parent identity records that request's owner and completion recipient, and any later ownership handoff still requires an explicit `warp-drive transfer`.

Run the sending scheduler explicitly in a dedicated native CMUX terminal with `warp-drive scheduler --send`.
Its `CMUX_SURFACE_ID` must identify that terminal and differ from enrolled recipient surfaces.
`warp-drive scheduler` performs admission and sampling without sending inbox messages.
An agent may use `run --detach` to launch its helper in a separate POSIX session and save output in a private log file.
That helper does not start the scheduler and does not become a general process supervisor.
Product commands inherit the invoking CLI's umask for newly created files and directories.
Warp-drive state and detached helper logs remain private.
The mechanism requires an observed native Claude and Codex survival pilot before claiming resilience to owner-session exit.

The source CLI also accepts an exact OpenCode owner session in the native `ses_` namespace and case-preserved form.
This source support does not update an installed runtime, start a scheduler, grant admission authority or prove that Hailing Frequencies can route completion to an OpenCode owner.
See the [agent guide](guide.md) for the current boundary and setup details.

Repeated stable keys attach to the same request only when work contents and checkout identity match.
Batch input is validated before one transaction commits.
Delayed, held, and disconnected work retain earned credit and stop accruing new credit.
Transfer changes the current notification owner without creating another request.
Recorded launch intent prevents automatic product-command replay, and late completion is accepted for the same fenced claim.
Started commands remain the agent's responsibility.

## Hailing Frequencies

New `submit`, `run`, and batch reservations require an exact harness, session, and surface match in the existing local Hailing enrollment.
The check reads one local snapshot and accepts enrolled idle or busy recipients, including an explicitly delegated parent, without checking prompt readiness or starting a service.
An identical stable-key retry remains available after enrollment is lost, while changed content or a different owner under the same key remains rejected.
Correct a rejected tuple to the observed enrolled recipient, or follow `hailing-frequencies guide --print` to enroll the actual intended owner; do not enroll a guessed identity.
Any later ownership handoff still requires an explicit `warp-drive transfer`.

The helper records completion and a durable pending event.
The dedicated scheduler resolves the current owner and queues one native CMUX inbox message to that owner's exact Hailing-enrolled surface.
Its sender surface remains the scheduler terminal, so Hailing's self-message filter can accept same-owner completion.
CMUX delivers native agent messages through recipient hooks without typing into the terminal.
The existing Hailing worker may also prompt from its monitored inbox after validating session binding, composer, lifecycle, and human-input state.
The scheduler resolves the current owner at dispatch, so a mid-run transfer needs no manual reattachment.
Unavailable delivery stays pending for a later sending scheduler pass after enrollment or CMUX capability recovers.
Warp-drive records only that CMUX queued the message, not that Hailing prompted the owner or that the prompt was received or read.
An uncertain CMUX send is recorded and never blindly replayed.
Use `status` and `inspect` for passive observation; they do not attach or replace a helper.
`attach` is a recovery action that can replace a waiting helper and run the command through this CLI, with output sent here and lost if stdout is redirected away.
Use `attach --detach` when recovery should run separately and append output to the private request log.
Use `status` or `inspect` for a point-in-time admission snapshot because initial `run` output cannot predict a later scheduler decision.
The snapshot shows host sample freshness, observed and projected CPU, memory pressure, estimated active managed contributors, and current candidate constraints.
Active managed commands are estimated contributors, not proof that each command caused host load or must finish before a request can start.
When policy identifies an active aged escape or a selected protected backfill target, the snapshot includes that request ID as the specific blocker, and a selected target remains waiting until a later admission pass.
Host capacity, stale sample, memory pressure, and startup observation restrictions do not assign a causal reservation ID.
The turn context shows current round, each ready worktree's manual order and served round, and whether it is eligible in the selection round if the scheduler runs now.
The snapshot is not a FIFO rank, reservation promise or ETA, and a later sample or scheduler pass may change it.
Status and doctor list completed request IDs whose owners lack an exact Hailing route and show the supported enrollment or owner-authorized transfer action.
Pending completion events remain durable for the existing sending scheduler to retry after route recovery.

For exceptional incidents, the same `scheduler --send` process may queue one native CMUX inbox message to the exact enrolled `warp-drive-engineer` Hailing target.
Each scheduler pass attempts at most one completion and one engineer incident message, so recovered backlogs drain at the sampling cadence.
Hailing's existing worker remains responsible for its guarded prompt path.
Doctor distinguishes an installed launcher and enrolled target from observed prompt delivery.
Outbound completion and incident messages are off unless that scheduler command includes `--send`.
An absent engineer enrollment or CMUX executable remains eligible for a later send pass.
A nonzero or uncertain external send result is retained and never blindly replayed.

## Verification and limits

Run `python3 apps/warp-drive/verify.py` for temporary-state CLI acceptance.
The verifier uses only temporary state, no persistent scheduler, no native CMUX message or Hailing send, and no live model session.
`git diff --check` is also required for source review.

Native helper survival across owner-session exit and actual completion wake receipt are platform-pilot observations, not claims made by source verification.
CPU-demand estimates are provisional and not learned automatically.
Child CPU attribution is recorded as unavailable when not measured.
Raw host samples have no retention policy.
Open incidents need explicit owner or engineer review; incident records do not authorize process termination, retry, order bypass, or global policy change.
