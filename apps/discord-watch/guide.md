# Discord Watch setup guide

Discord Watch polls registered agent-notify forum threads for replies from the configured Discord user and keeps those replies in a private local queue.
With `--send`, one supervised worker sends a guarded wake to the exact Claude or Codex session enrolled for each thread.
The wake contains the exact `discord-watch receive` command and never contains the reply body.

## Prerequisites

Use Python 3.9 or newer on macOS or another POSIX host with `fcntl`.
Install or reuse `agent-notify`, Hailing Frequencies, and a current CMUX command that supports `current`, `surface.input_state`, and JSON `paste --submit` results.
Run `agent-notify guide --print` first for complete Discord Community, Forum, webhook, bot, permission, and private credential setup.
The recipient must already be enrolled in Hailing Frequencies from the exact current Claude or Codex session.
The existing `agent-notify` environment file must contain `DISCORD_BOT_TOKEN` and should contain `DISCORD_USER_ID` for the account whose replies should be queued.
When `DISCORD_USER_ID` is omitted, Discord Watch uses the cached `user` written by `agent-notify doctor`.
Never copy tokens into command arguments, repository files, or logs.

## Install or reuse

From a Starfleet AI checkout, install or update Hailing Frequencies when the `agent-notify` command is not already available:

```sh
python3 apps/hailing-frequencies/install.py
agent-notify guide --print
python3 apps/discord-watch/verify.py
python3 apps/discord-watch/install.py
discord-watch guide --print
```

The Discord Watch installer creates the `discord-watch` launcher and an immutable runtime under `~/.local/share/starfleet-ai/discord-watch/`.
It packages the maintained guide, README, source, and a source copy of Hailing Frequencies' guarded CMUX binding.
Installation does not initialize state, register a thread, start a worker, or alter harness or CMUX settings.
If either launcher is already installed, run its guide and continue with the setup below.
To install into a different location, pass `--runtime-root PATH` and `--bin-dir PATH` to `install.py`.

## Prepare existing configuration

Run `agent-notify doctor` once if the cached configured-user ID or Closed forum tag is missing.
Use the existing Hailing Frequencies state and enroll each recipient from its own current session if needed.

```sh
hailing-frequencies status
hailing-frequencies enroll --harness claude --surface SURFACE_UUID --session CLAUDE_SESSION_UUID --label agent-notify-owner
discord-watch doctor
```

Use `--harness codex` and the exact `CODEX_THREAD_ID` for a Codex recipient.
The surface, session, workspace, harness hook generation, receiving settings, and empty composer are checked at registration and before every wake.
The command must run in the recipient's own CMUX session with its normal `CMUX_AGENT_LAUNCH_KIND`, session ID, and `CMUX_SURFACE_ID` environment.

## Register an exact thread

From the recipient session that will own the thread, run:

```sh
discord-watch register THREAD_ID
discord-watch managed THREAD_ID
```

Registration uses the current session's exact Hailing Frequencies enrollment.
It refuses a thread already owned by a different session.
For a new thread, the default starting cursor is the thread creation ID so older replies are replayed safely.
An existing agent-notify cursor records messages fetched by the old listener, not replies proven handled by its owner.
Pass `--after LAST_ACTUALLY_HANDLED_ID` only when the owner has confirmed that every earlier reply was handled.
An explicit cursor must be at or after the thread creation ID.
The initial intake cutoff is immutable and is set to the thread creation ID by default or to the explicit `--after` value.
Gateway replays at or below the cutoff are ignored, while a later unseen message can be ingested out of order below the advancing REST cursor.
Existing registrations migrate with their committed REST cursor as the conservative cutoff, without changing queued replies, cursors, or catch-up boundaries.
When a REST or Gateway message payload supplies `channel_id`, intake requires it to match the selected registered thread.
The legacy agent-notify listener should stop polling a thread after `managed` reports it as managed.
`managed` succeeds for a retained subscription owned by the exact current session, including a locally closed or dormant registration.
Reusing `register` from the same exact owner after local closure, a Closed tag, or archiving does not reopen the registration or reset its cursor, cutoff, or owner generation.
An idempotent repeat of `--after` may use either the immutable initial cutoff or the current committed cursor.
Its JSON includes worker health, and a stale worker warning does not change ownership or the central managed route.

Only the current registered owner can receive or locally close a thread.
To hand a retained registration to a different current enrolled session, run `discord-watch transfer THREAD_ID --from-session OLD_SESSION_UUID` from the new owner's session.
Transfer requires the old session ID to match the stored owner and preserves every unreceived reply.
Transfer can preserve a locally closed registration without reopening it.
An explicit transfer resets confirmed-but-unreceived wakes for safe delivery to the new owner.
Uncertain submissions are never retried automatically.

## Observe, pilot, and supervise

The scanner is observe-only unless `--send` is supplied.
Use the existing default state directory and one central worker for all registered threads.
Check `discord-watch status` before starting anything; reuse a healthy worker and never start a second worker for the same state directory.

```sh
discord-watch scan --only-thread THREAD_ID
discord-watch scan --send --only-thread THREAD_ID
discord-watch supervise --send
```

`serve --send` runs the single polling worker for a 12 second interval.
`supervise --send` owns one supervisor lock and restarts a crashed worker with bounded backoff.
Both commands support `--only-thread THREAD_ID` for a bounded pilot.
Stop the supervising terminal with SIGINT or SIGTERM to stop future polling and wakes.
There is no automatic service startup.

## Receive a reply and inspect status

The recipient can retrieve a reply even when the wake was held, uncertain, or the worker is stopped.

```sh
discord-watch receive --thread THREAD_ID
discord-watch receive --thread THREAD_ID --message MESSAGE_ID
discord-watch status
discord-watch close THREAD_ID
```

The receive command prints exact `[discord:THREAD:MESSAGE] author: content` records and flushes stdout before recording a local receipt.
That flush is a delivery boundary for the CLI and does not prove that the reply was read or understood.
The private database stores message bodies, attachment filenames, and attachment URLs with owner-only filesystem permissions.
Receive prints bounded attachment filenames and HTTPS URLs to the exact recipient and does not download files.
Status distinguishes a confirmed CMUX submit from a recipient receipt and reports held, uncertain, and stalled states without printing reply bodies.
`close` closes the local registration and preserves the exact owner and unreceived replies.
It does not unsubscribe the thread, so active retained subscriptions continue polling and queued replies remain eligible for guarded delivery.
Remote Closed tags and archived state are recorded separately and do not close the local registration or stop reply intake.

## Retained subscriptions and dormancy

Meaningful activity is a newly stored message from the configured Discord user, measured by the message's Discord timestamp.
If no configured-user reply has been recorded, the seven-day window starts when the thread is registered.
Exactly seven days without meaningful activity remains eligible for routine REST polling.
After more than seven days, the subscription becomes dormant and routine REST polling stops for that thread.
Polling, retries, reminders, and worker heartbeats never extend the activity window.
A newly inserted configured-user reply at least as recent as the latest activity reactivates the subscription and starts a fresh seven-day window from that timestamp.
Duplicate messages and older replays do not extend the window.

Dormancy preserves the thread, exact owner generation, REST cursor, catch-up boundary, queued message bodies, attachment references, and delivery state.
The worker can still send a guarded wake for queued replies on dormant subscriptions.
Missing or stale exact owners remain a hold, and replies stay available to the owner through `receive`.
Use `discord-watch scan --only-thread THREAD_ID` for an explicit REST reconciliation of a dormant known thread.
The narrow Gateway intake method stores messages through the same validation and deduplication path as REST without advancing the REST cursor.
When Gateway intake is connected, reconnect recovery must reconcile every known retained thread, including dormant threads, before reporting the Gateway recovered.
Until that integration exists, run the bounded explicit scan for a dormant thread when reconciling after a missed Gateway interval.

## Verification and delivery evidence

Run `python3 apps/discord-watch/verify.py` before using the worker.
`doctor` checks local configuration without printing credentials.
`status` reports the last observed API scan and each message's state.
A configured thread or a successful CMUX submit is not proof of receipt.
Only `receive` records the local output flush, and it is not proof of comprehension.
