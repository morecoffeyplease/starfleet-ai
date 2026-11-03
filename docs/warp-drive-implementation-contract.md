# Warp-drive implementation contract

This document records the stable design boundaries for Warp-drive.
For installation, commands, configuration, and current limitations, use the [component README](../apps/warp-drive/README.md) and [agent guide](../apps/warp-drive/guide.md).

## Scope

Warp-drive is a local CPU reservation queue for agent-owned commands.
Agents prepare environments, choose commands and worker bounds, inspect results, and own cleanup.
The scheduler orders eligible work and records host observations.
The scheduler does not execute, retry, kill, or repair product work.

The implementation uses Python's standard library and a local SQLite database.
A dedicated scheduler process owns an operating-system lock and samples host conditions.
There is no network service, broker, process supervisor, benchmark runner, or automatic tuning service.

## Reservation identity and ownership

Each request has a stable key, repository and worktree identity, current owner, workload family, declared size, worker count, expected runtime, eligibility time, priority, and event history.
Repeated keys attach to the same request only when work contents and checkout identity match.
A changed command or checkout is a new request.

Owner identity comes from the observed harness, native session, and surface.
A reservation can be explicitly transferred to another owner without creating another request.
A transfer does not replay a started command or claim that the new owner received an earlier wake.

Worktree identity includes its canonical path and filesystem identity.
Recreating a checkout creates a new worktree identity.

## Admission and fairness

A waiting request needs an attached helper with a fresh heartbeat.
The scheduler uses fresh host samples, recent CPU observations, memory pressure, and conservative estimates for newly granted work.
These values guide admission and do not throttle commands or impose a hard CPU quota.

The scheduler serves at most one eligible request per worktree in each persisted round.
A request that cannot fit does not prevent unrelated ready work from taking its turn.
Light work has separate headroom and bypasses heavy-work startup pacing.

An aged heavy request may protect its worktree from fitting heavy backfill after other serviceable worktrees take their turns.
Protection applies only when releasing active managed work could make that request eligible.
The protected turn remains subject to the documented sample, memory-pressure, startup, and light-work limits.

The queue has no hard CPU or wait-time bound under continuous unmanaged saturation or indefinitely running commands.
Admission estimates are signals and do not prove which process caused observed host load.

## Launch, outcome, and evidence

A request has one launch claim.
A deliberate rerun requires a new request.
An uncertain result remains visible until the responsible owner supplies evidence.

The helper records completion and releases the reservation once.
Repeated completion reports are harmless.
Child CPU time and peak memory are recorded only when the host can measure them reliably.
Unsupported or incomplete measurements remain explicitly unavailable.

Command arguments and local measurements remain in owner-only state.
Warp-drive does not collect environment secrets or complete agent transcripts.

## Notifications and verification limits

Completion events use the existing Hailing Frequencies path to address the exact enrolled owner.
A configured route, queued CMUX message, prompted agent, and actual recipient receipt are distinct observations.
The CLI does not add a second notification worker or duplicate delivery state.

The component verifier uses temporary state, deterministic samples, and harmless commands.
It does not send native CMUX messages or establish that a live agent received a completion wake.
Native helper survival and actual receipt require separate bounded platform pilots.
