# Temporal Beacon: agent guide

Temporal Beacon schedules a future continuation for an enrolled Claude, Codex, or explicitly bound OpenCode session inside CMUX.
It shares Hailing Frequencies' worker, enrollment, exact-session input guards, and health reporting.
Timer records and occurrence receipts are separate from unread-message reminders.
Scheduling a timer does not create a new agent or grant additional task authority.

## How it works

The local CLI implements durable one-shot and recurring timers, time deadlines, replacement, cancellation, receipt, and diagnostics.
It uses Python 3.9 or newer with standard-library facilities on macOS or another POSIX host with `fcntl`.
It requires the same CMUX capabilities and exact-session enrollment as [Hailing Frequencies](README.md#install-without-activating).
The installer publishes an immutable runtime and the Hailing Frequencies, Temporal Beacon, and Agent Notify commands without starting a worker or registering harness skills.

OpenCode timers require an exact Hailing Frequencies self-enrollment manifest and use the enrolled loopback API binding when they become due.
Timer commands require the managed launch's exact `CMUX_AGENT_LAUNCH_KIND=opencode`, `OPENCODE_SESSION_ID`, `CMUX_SURFACE_ID`, and `CMUX_WORKSPACE_ID` environment values.
OpenCode enrollment and timer creation verify the exact API and native session identity and may proceed while the API session is busy.
Timer delivery still requires the full API readiness inspection and the enrolled composer policy; the current native pilot's `unknown` composer state holds delivery under the default `native_empty_composer` policy.
The OpenCode API identity inspector checks session identity, server PID, and directories without requiring send readiness, while the delivery inspector also checks running state, permissions, and forms; neither exposes composer drafts.

Register the packaged skill with each harness that needs to discover Temporal Beacon.
Delivery requires the authorized Hailing worker terminal to remain open.

## Install without activating

From the repository root, use the shared installer:

```sh
python3 apps/hailing-frequencies/install.py
temporal-beacon --help
```

The result prints `temporal_beacon_command`, `agent_notify_command`, `runtime`, `skill`, and `agent_notify_skill` paths.
The commands live in `~/.local/bin`; add that directory to your command search path if it is not already present.
The runtime lives under `~/.local/share/starfleet-ai/hailing-frequencies/`, so removing a development worktree does not remove the installed tool.
If Hailing Frequencies is already installed, this command updates the launchers in its immutable runtime as well.
Coordinate any active worker restart with its owner so it uses the intended immutable runtime.
Installation does not change enrollment, start a service, or register the skill with either harness.

For a new state directory, initialize and inspect it before enrolling sessions:

```sh
hailing-frequencies init
hailing-frequencies status
```

Reuse the existing state and worker when they already exist.
Follow [exact-session enrollment](README.md#enroll-exact-sessions) and [observe-then-send operation](README.md#observe-then-send) for activation.
Use `hailing-frequencies supervise --send` only within the authorized session scope.
Temporal Beacon needs no second daemon.

## Enable agent discovery

The installer prints a `skill` path ending in `skills/temporal-beacon/SKILL.md`.
Use its containing `temporal-beacon` directory as `beacon_skill_dir` below.
Replace the example path with the actual installed runtime path, not a temporary worktree path.

For Codex:

```sh
beacon_skill_dir='/absolute/installed/runtime/skills/temporal-beacon'
test -f "$beacon_skill_dir/SKILL.md" && mkdir -p "$HOME/.agents/skills"
test -f "$beacon_skill_dir/SKILL.md" && \
  test ! -e "$HOME/.agents/skills/temporal-beacon" && \
  test ! -L "$HOME/.agents/skills/temporal-beacon" && \
  ln -s "$beacon_skill_dir" "$HOME/.agents/skills/temporal-beacon"
```

For Claude, use the same installed directory:

```sh
beacon_skill_dir='/absolute/installed/runtime/skills/temporal-beacon'
test -f "$beacon_skill_dir/SKILL.md" && mkdir -p "$HOME/.claude/skills"
test -f "$beacon_skill_dir/SKILL.md" && \
  test ! -e "$HOME/.claude/skills/temporal-beacon" && \
  test ! -L "$HOME/.claude/skills/temporal-beacon" && \
  ln -s "$beacon_skill_dir" "$HOME/.claude/skills/temporal-beacon"
```

These commands refuse an existing destination, including a dangling link.
Inspect an existing destination before changing it; do not overwrite another skill.
An immutable runtime upgrade does not retarget an existing skill link automatically.
Start a new agent session after registration to load the skill.

## Agents start here

Run `temporal-beacon doctor` to check the worker and your current enrollment before promising an automatic wake.
Starfleet Doctor uses `temporal-beacon doctor --starfleet-json`, which reads local state without acquiring locks or creating the timer database.
A fresh Temporal Beacon heartbeat is recent worker evidence, not proof of current process liveness.
The command identifies a stopped worker, missing enrollment, changed binding, input hold, or uncertain delivery and provides recovery guidance.
Use the same `--state-dir` as the Hailing Frequencies worker when using an isolated pilot.
Global options precede the subcommand.

```sh
temporal-beacon set --after 15m --message 'Check PR CI and continue if green.'
temporal-beacon set --after-idle 50m --message 'Refresh the checkpoint before possible idle compaction.'
temporal-beacon set --at '2026-11-01T17:00:00-08:00' --message 'Review deployment status.'
temporal-beacon set --every 10m --until '2026-11-01T18:00:00-08:00' \
  --message 'Check CI; cancel this timer when it passes.'
temporal-beacon list
temporal-beacon inspect TIMER_ID
temporal-beacon cancel TIMER_ID
```

Use `temporal-beacon --help` and each subcommand's `--help` for the installed command contract.
Set commands bind the real caller to an enrolled native session and return the timer identity, resolved due time, recurrence, and delivery capability.
If identity cannot be established, follow the enrollment guidance rather than supplying a guessed identity or inheriting a parent's identity.
Confirm the resolved due time before telling the user what was scheduled.

Use `--request-id` for a stable creation intent: retry the same request rather than creating another timer after a client timeout.
Use `--replace-key` when a later timer should replace an earlier timer for the same purpose in the same session.
Cancellation or replacement cannot recall input that has already been submitted.

## Scheduling semantics

| Option | Meaning |
| --- | --- |
| `--after DURATION` | One wake after the requested duration |
| `--after-idle DURATION` | One wake after confirmed native inactivity; activity postpones delivery |
| `--at OFFSET_DATETIME` | One wake at an absolute date and time |
| `--every DURATION` | First wake after one interval, then repeat after receipt |
| `--until OFFSET_DATETIME` | Recurring timer cutoff; occurrences at or after the cutoff are excluded |

Absolute times require an explicit UTC offset or `Z`.
An idle timer waits at least its requested duration after creation, then checks the exact session's current native activity timestamp and idle state.
Creation requires a fresh idle-capable heartbeat matching the supervised worker's PID and generation token, with both existing supervisor and serving lifetime locks still held.
A recent capability marker alone cannot authorize a timer after worker stop or replacement.
Coordinate the existing worker upgrade with its owner before enabling idle reminders; ordinary timers remain available independently.
Working sessions postpone the deadline, and renewed activity cancels a stale held occurrence and resets its due time.
The worker checks activity again immediately before guarded submission, keeping human-input, enrollment, session-generation and no-replay protections.
Unknown or future activity evidence holds delivery; elapsed wall time by itself is insufficient.
After receipt, an idle timer completes and never repeats automatically.
Create or replace another timer only for a later genuine work episode, excluding the timer's own wake and checkpoint-only response.
This prevents a reminder from creating an endless keep-alive loop.
Idle timers do not promise a compaction time or change Claude settings, and delayed worker delivery can arrive later than its due time.
There is no daily calendar, cron, or daylight-saving recurrence interpretation.
There is no `--when` condition evaluator.
For "every ten minutes until CI passes," put the check in the continuation and have the agent cancel the timer when the condition is satisfied.
Temporal Beacon never executes the message as a shell command.

At most one occurrence is pending for each timer.
A recurring timer waits for receipt of its previous occurrence before advancing.
After sleep or worker downtime, missed intervals collapse into one catch-up notification rather than a burst.
An `--until` deadline also expires held recurring occurrences.
Actual submission can be later than the due time because the worker is stopped, CMUX is unavailable, or input is held.

## Wake and receipt

The wake prompt contains the timer identity and a command to retrieve the current continuation with `receive`.
Run that command in the originating agent session before acting.
The command checks current ownership and timer state, outputs the continuation, and records receipt only after successful output.
Receipt records an output boundary, not proof that the model understood or completed the task.
Recheck current external state before taking action: a CI result or deployment may have changed since the timer was set.

Submitted and received are distinct states.
Submission means CMUX confirmed the input operation; only receipt records retrieval of the continuation.
The service persists an attempt before writing input.
If submission times out or the process crashes during delivery, it preserves an uncertain attempt rather than automatically replaying a possibly delivered message.
Inspect uncertain delivery before deciding how to recover.

## Service operation and recovery

Use the existing Hailing Frequencies installation, initialization, enrollment, and `supervise --send` workflow in the [agent guide](README.md).
Installation alone starts no service and enrolls no sessions.
Run the worker in an authorized CMUX terminal; closing that terminal stops delivery until restarted.
Do not start a second worker against the same recipients or alter socket policy to work around authorization.
The independent Hailing health reader can report stopped coverage but cannot wake an agent itself.

| Finding | Recovery |
| --- | --- |
| Worker stopped or stale | Inspect its dedicated terminal, restart the existing supervisor with the same state directory, then check `doctor` |
| Observe-only or session outside send scope | Enable sending only within the operator-authorized canary or rollout scope |
| Session not enrolled | Observe actual CMUX session and surface identities and follow the printed enrollment command |
| Draft or human-input hold | Resolve the actual draft or human obligation; leave the timer pending until safe |
| Session replaced or binding changed | Inspect the new session; do not transfer old timers automatically |
| Submission uncertain | Inspect the recipient and saved occurrence before taking further action; do not blindly resubmit |
| Timer no longer needed | Cancel it; any already submitted notification must be checked against current timer state |

Native guards refuse unknown input state, drafts, human-input obligations, stale session generations, and disabled receiving settings.
The final CMUX input operation is not a transaction that freezes a human keyboard or guarantees exactly-once delivery.
Temporal Beacon is a continuation reminder, not an unattended high-impact command sender.

## Verification

The packaged [Temporal Beacon skill](skills/temporal-beacon/SKILL.md) gives agents scheduling, receipt, and diagnosis guidance.
From the repository root, run the canonical check using the [Warp-drive reservation workflow](../warp-drive/guide.md) for CPU-intensive execution:

```sh
python3 apps/hailing-frequencies/verify.py
git diff --check
```
