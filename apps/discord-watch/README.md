# Discord Watch

Discord Watch polls explicitly registered agent-notify forum threads and queues replies from the configured Discord user.
One supervised polling worker serves all registered threads independently of per-thread `agent-notify listen` processes.
Each open thread has one exact owner copied from that session's Hailing Frequencies enrollment, including an OpenCode API binding when applicable.

Read the [setup guide](guide.md) for installation, enrollment, a bounded pilot, and day-to-day commands.
Print the same guide from any working directory with `discord-watch guide --print`.

## Data and delivery model

The worker reads `DISCORD_BOT_TOKEN` and `DISCORD_USER_ID` from the existing `agent-notify` configuration.
When the user ID is omitted from that environment file, it uses the cached `user` value from `agent-notify doctor`.
Credentials stay in process memory and are never written to Discord Watch state or reports.
SQLite state lives under `~/.local/state/starfleet-ai/discord-watch/` by default with owner-only permissions.
Reply bodies and bounded attachment filenames and URLs are stored in SQLite so the worker can restart without losing a queued reply.
Each message page and its cursor or catch-up boundary commit in one SQLite transaction.
The cursor is a decimal snowflake string and is compared numerically without depending on SQLite integer range.
Discord returns channel messages newest to oldest, so a full `after` page is followed backward with bounded `before` pages until the previous cursor is reached.
The committed cursor advances only after that gap is complete, and the durable catch-up boundary lets a restarted worker resume safely.
Each page is sorted in ascending snowflake order before it is committed.
The implementation follows Discord's [Get Channel Messages](https://discord.com/developers/docs/resources/message#get-channel-messages) ordering and [rate-limit guidance](https://discord.com/developers/docs/topics/rate-limits).
Duplicate `(thread, message)` pairs are ignored.

For each thread, registration stores the exact Hailing Frequencies harness, surface, workspace, session, and generation.
The central worker checks that the same target remains enrolled and validates the current CMUX projection and input guard before each wake.
OpenCode wakes also recheck the exact authenticated API session, server PID, worktree, running state, pending permissions, and pending forms before using the OpenCode prompt route.
The worker never forces terminal input, reads or clears CMUX messages, launches another agent, or sends a reply body in a wake.
The prompt names one exact `discord-watch receive --thread THREAD --message MESSAGE` command, with `--session ses_ID` for OpenCode.
OpenCode registration, `managed`, `close`, and `receive` verify the exact enrolled owner without checking composer readiness.
Automatic wake readiness is checked separately against the persisted Hailing Frequencies delivery policy.
The default `native_empty_composer` policy requires an explicitly empty composer with no human-input obligation.
For OpenCode only, the explicit `api_sidechannel` policy also permits `draft` or `unknown` composer classifications when there is no blocking input or human-input obligation and every other native, API, lifecycle, receiving, and owner guard passes.
The current native CMUX OpenCode input state remains `unknown`, so default-policy automatic wakes hold until native state is explicitly empty.
Attachment filenames and HTTPS URLs are printed to the exact recipient during `receive` without downloading the files.

The worker recognizes a remote archived thread or the cached `Closed` forum tag and closes that local registration without sending more wakes.
Discord access errors, missing permissions, and rate limits remain visible per thread and do not look like remote closure.
Rate-limit `retry_after`, HTTP status, Discord error code, message, and global-limit scope are retained as structured status data.
A global rate limit defers all thread requests until its retry time, while a per-route limit defers that thread.
One thread's API error does not stop polling other registered threads.
Each request has a finite timeout, response and message sizes are bounded, and catch-up is limited to 50 pages per scan.

A confirmed guarded CMUX submit or exact OpenCode user-message admission moves one reply to `awaiting_receipt`.
Discord Watch waits two minutes before a reminder and caps confirmed submits at three per reply.
After the third wake remains unreceived for another interval, the reply becomes visibly `stalled`.
A transport failure after CMUX or the OpenCode API begins submission is `uncertain` and is never retried automatically.
The exact owner can resolve an uncertain, stalled, or held reply by explicitly receiving it.
No operator command resets an uncertain send to pending.

`receive` holds the local consumer lock while writing exact records and flushes stdout before saving receipts.
This flush is the CLI delivery boundary and is not proof that a person or model read or understood the reply.
If output fails or the current owner changes before the receipt commit, the reply remains available for another receive.
`managed` checks exact current ownership and open registration and includes the latest worker health report.
Its success is independent of worker freshness so the caller can retain ownership while diagnosing a stale worker.
`close` preserves all queued replies and closes only the caller's own exact registration.
`transfer` requires an explicit `--from-session` matching the stored owner and must be run from the new exact enrolled session.
Transfer preserves unreceived replies and resets confirmed-but-unreceived wakes so the new owner can safely receive them.
An uncertain send stays uncertain across transfer.

## Commands

Global path options go before the command:

```sh
discord-watch --state-dir PATH --notify-home PATH --hailing-state PATH --cmux PATH status
```

The main commands are `register`, `managed`, `receive`, `close`, `transfer`, `status`, `doctor`, `scan`, `serve`, and `supervise`.
`scan` is observe-only unless `--send` is supplied.
`serve` runs one worker, while `supervise` owns a separate supervisor lock and restarts a crashed worker with bounded backoff.
Both accept `--send` and `--only-thread THREAD`.
One state directory allows one worker and one supervisor at a time.
The installer starts neither process and configures no launch service.

## Installation and verification

From a Starfleet AI checkout, run:

```sh
python3 apps/discord-watch/verify.py
python3 apps/discord-watch/install.py
discord-watch guide --print
```

The installer copies Discord Watch, this README, the guide, and the Hailing Frequencies source dependency into an immutable hash-named runtime.
The installed guide can print before initialization, enrollment, Discord credentials, or a Git checkout.
Uninstalling the launcher preserves the runtime and local state.
