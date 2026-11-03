---
name: temporal-beacon
description: Schedule a wake-up for this Claude or Codex agent using Temporal Beacon inside CMUX, including one-shot reminders and recurring checks with a time deadline. Inspect timers and diagnose unavailable or held delivery.
---

# Temporal Beacon

Use the installed `temporal-beacon` command to schedule a future continuation in the current enrolled session.
Use `temporal-beacon --help` and `temporal-beacon set --help` for the installed command contract.
Run `temporal-beacon doctor` before promising automatic wake delivery.
The existing Hailing Frequencies worker must be running with sending enabled for this session inside an authorized CMUX terminal.
Timer persistence does not mean the worker is currently available.

```sh
temporal-beacon set --after 15m --message 'Check PR CI and continue within the existing task scope.'
temporal-beacon set --after-idle 50m --message 'Refresh the checkpoint before possible idle compaction.'
temporal-beacon set --at '2026-11-01T17:00:00-08:00' --message 'Review deployment status.'
temporal-beacon set --every 10m --until '2026-11-01T18:00:00-08:00' --message 'Check CI; cancel this timer when it passes.'
temporal-beacon list
temporal-beacon inspect TIMER_ID
temporal-beacon cancel TIMER_ID
```

Use an explicit UTC offset or `Z` for absolute times.
Confirm the resolved due time and delivery capability from the result before reporting what was scheduled.
Use `--request-id` to retry the same creation request without duplication, and `--replace-key` when a newer timer should replace an earlier timer for the same purpose.
Recurring timers first fire after one interval and exclude occurrences at or after `--until`.
An `--after-idle` timer is one-shot: it waits at least the duration after creation and postpones delivery while the exact native session is working or has more recent activity.
Receipt completes it; the reminder's own response must not automatically create another timer.
Unknown activity and human-input holds prevent submission; a timer cannot guarantee when Claude will compact.
There is no condition-evaluating `--when`; check the condition when woken and cancel when satisfied.

On wake, follow the supplied `receive` command to retrieve the current continuation and record receipt.
Receipt means the continuation was output, not that its task completed.
Act only on the current timer generation and within existing task authority.
At most one occurrence is pending per timer; the next recurring occurrence becomes eligible after receipt.
Missed intervals collapse into one catch-up rather than a burst.

If wake delivery is unavailable, use `doctor` and `inspect` for the concrete hold and recovery instructions.
Enroll only observed native identities; do not guess session IDs, borrow another session's identity, force terminal input, or transfer a timer to a replacement agent.
Drafts and human-input obligations hold delivery until safe, subject to the timer deadline.
An uncertain submission may already have arrived: inspect the recipient and occurrence before retrying; creating another timer is not a safe retry.
Canceling prevents future dispatch but cannot retract already submitted input.
Closing the worker terminal stops coverage until it is restarted.
