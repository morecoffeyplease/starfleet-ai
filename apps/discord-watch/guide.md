# Discord Watch setup guide

Discord Watch receives configured-user replies through one Discord Gateway connection and reconciles retained agent-notify forum threads through REST.
With `--send`, one supervised worker sends a guarded wake to the exact Claude, Codex, or explicitly bound OpenCode session enrolled for each thread.
The wake contains the exact `discord-watch receive` command and never contains the reply body.
New replies are grouped by thread and exact owner generation after three seconds of quiet, with a maximum ten-second delay from the first queued reply.
Quiet time uses local intake time for unique configured-user messages, while the maximum delay starts at the first message in that pending group.
Each group contains at most 20 messages and 256 KiB of rendered UTF-8 output, and overflow remains in later ordered groups.
The output bound fits every accepted single message with its supported attachment references.
Messages are ordered by numeric Discord message ID and retain their full bodies and supported attachment filenames and HTTPS URLs.

## Prerequisites

Use Python 3.10 or newer on macOS or another POSIX host with `fcntl`.
Install or reuse `agent-notify`, Hailing Frequencies, and a current CMUX command that supports `current` and `surface.input_state`.
Claude and Codex also require JSON `paste --submit` results.
OpenCode also requires its supported Hailing Frequencies API binding; the selected delivery policy determines whether a native empty-composer result is required.
Run `agent-notify guide --print` first for complete Discord Community, Forum, webhook, bot, permission, and private credential setup.
The recipient must already be enrolled in Hailing Frequencies from the exact current Claude or Codex session, or from the explicit OpenCode manifest path in the Hailing Frequencies guide.
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
It packages the maintained guide, README, source, pinned Gateway dependency, and a source copy of Hailing Frequencies' guarded CMUX binding.
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
OpenCode registration, `managed`, `close`, and `receive` verify the exact Hailing Frequencies owner identity without checking composer readiness.
Claude and Codex owner commands and Discord reply wakes check the exact live surface, session, workspace, hook generation, and all three native receiving settings.
They do not read `surface.input_state`, so draft and unknown composer or prompt lifecycle states do not hold a Discord reply wake or the exact owner's `receive` command.
Ordinary Hailing Frequencies prompts keep the strict empty-composer and no-human-input guard.
The default `native_empty_composer` policy requires an explicitly empty composer with no human-input obligation.
For OpenCode only, an explicitly selected `api_sidechannel` policy may also accept `draft` or `unknown` classifications when there is no blocking input or human-input obligation and all other native, API, lifecycle, receiving, and owner guards pass.
The command must run in the recipient's own CMUX session with its normal `CMUX_AGENT_LAUNCH_KIND`, session ID, and `CMUX_SURFACE_ID` environment.
For OpenCode, keep the session's exact `ses_` identity and API binding in Hailing Frequencies, and pass `--session ses_ID` to owner commands when needed.
OpenCode owner commands also require `CMUX_AGENT_LAUNCH_KIND=opencode`, `OPENCODE_SESSION_ID`, and the exact `CMUX_SURFACE_ID` and `CMUX_WORKSPACE_ID` values seeded for that session.
The current native CMUX OpenCode input state is `unknown`, so identity-only registration and owner commands can succeed while default-policy automatic wakes remain held until CMUX proves the composer empty.
An explicitly configured `api_sidechannel` wake may accept that `unknown` state only with the full guard set above.
This policy boundary does not qualify automatic wake delivery or prove a recipient received the reply.

## Register an exact thread

From the recipient session that will own the thread, run:

```sh
discord-watch register THREAD_ID
discord-watch managed THREAD_ID
```

For OpenCode, supply the exact session ID explicitly:

```sh
discord-watch register THREAD_ID --session ses_EXACT_SESSION_ID
discord-watch managed THREAD_ID --session ses_EXACT_SESSION_ID
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
Worker health reports configured sender coverage separately from `last_send_scope`, which contains the latest dispatch worklist.
An unrestricted sending worker covers quiet and newly registered threads before they have queued replies.
Previously queued per-message records migrate into singleton frozen groups without resetting message state or attempt evidence.
New groups are not sealed while the thread has an incomplete REST catch-up boundary or incomplete current-epoch Gateway recovery coverage.
Worker submission and default foreground pulls wait for both per-thread ordering barriers, while explicit frozen-group and exact-message receipts remain available to the exact owner.

Only the current registered owner can receive or locally close a thread.
For OpenCode, the group wake command includes its exact `--session ses_ID` owner so reply receipt does not depend on a guessed terminal or a tab title.
To hand a retained registration to a different current enrolled session, run `discord-watch transfer THREAD_ID --from-session OLD_SESSION_UUID` from the new owner's session.
Transfer requires the old session ID to match the stored owner and preserves every unreceived reply.
Transfer can preserve a locally closed registration without reopening it.
An explicit transfer preserves frozen group membership and lets the authorized new owner receive unread members through the ownership-event chain.
Unsealed messages can be assigned to the new generation, while a group never combines messages from different generations.
An unexplained generation change without a matching ownership-event chain holds the affected groups.
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

`serve --send` runs the single Gateway receiver, fair REST recovery lane, and native sender.
Routine REST reconciliation runs every 30 seconds with a bounded request budget.
Rotation follows the stable retained-thread order before filtering due threads, so the last-serviced thread remains a rotation anchor while it waits for its next poll.
An explicit scan that completes one thread's full REST catch-up also satisfies that thread's current recovery coverage while unselected threads remain queued.
The worker also reconciles every retained thread after Gateway disconnects, reconnects, and fresh Identify operations, including dormant subscriptions.
Recovery can take longer under rate limits, slow API responses, incomplete catch-up, or owner holds, so 30 seconds is a scheduling interval rather than a delivery guarantee.
`supervise --send` owns one supervisor lock and restarts a crashed worker with bounded backoff.
Both commands support `--only-thread THREAD_ID` to bound the native send scope and `--rest-only` for an explicit Gateway-disabled mode.
Reconnect recovery still covers every retained subscription, including when a thread-specific send scope is selected.
REST-only operation reports Gateway degradation in status and continues reconciliation without claiming Gateway intake is healthy.
Stop the supervising terminal with SIGINT or SIGTERM to stop future polling and wakes.
There is no automatic service startup.

## Receive a reply and inspect status

The recipient can retrieve a reply even when the wake was held, uncertain, or the worker is stopped.

```sh
discord-watch receive --thread THREAD_ID
discord-watch receive --thread THREAD_ID --group GROUP_ID
discord-watch receive --thread THREAD_ID --message MESSAGE_ID
discord-watch status
discord-watch close THREAD_ID
```

The group receive command prints every unread member of the selected frozen group in message ID order and flushes stdout before recording exact member receipts.
The default receive command selects the oldest frozen group, or freezes the next bounded pending group immediately when none exists.
This explicit foreground pull does not wait for the native wake quiet window or require a sending worker, and it does not submit a native wake.
REST catch-up, Gateway recovery, exact-owner, output, and receipt guards still apply to the manual pull, while `--message` retains the exact single-message path.
The receive command prints exact `[discord:THREAD:MESSAGE] author: content` records and flushes stdout before recording a local receipt.
That flush is a delivery boundary for the CLI and does not prove that the reply was read or understood.
The private database stores message bodies, attachment filenames, and attachment URLs with owner-only filesystem permissions.
Receive prints bounded attachment filenames and HTTPS URLs to the exact recipient and does not download files.
Status distinguishes forming and eligible groups, holds, durable submission intent, uncertain submissions, confirmed groups awaiting receipt, exact member receipts, and later queued messages without printing reply bodies.
It also reports each attempt's exact target owner binding and generation when available, while historical attempts with unknown targets remain unattributed.
A confirmed group is not submitted again while awaiting receipt, and uncertain submissions remain blocked indefinitely.
The wake command invokes the active Discord Watch runtime with its qualified interpreter, so a stale launcher earlier on `PATH` is not used to parse `--group`.
The new receiver shares `receive.lock` with supported older CLI versions and reconciles older per-message receipts without reprinting received members.
Older CLI versions do not understand `--group`, while their exact `--message` path remains available during upgrades.
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
The narrow `Watch.ingest_gateway_message` intake method stores one Gateway message through the shared REST validation and deduplication path without advancing the REST cursor.
The production Gateway adapter commits each dispatch sequence atomically with eligible message intake and stores session resume state in the private database.
Status reports Gateway condition, resumable-session availability, recovery coverage, and each thread's REST recovery and access condition separately.
If an exact owner is missing or stale, restore that same Hailing Frequencies binding or arrange an explicit authorized transfer before delivery can continue.
Discord Watch never reroutes replies to another owner automatically or starts a separate escalation service.
OpenCode identity-only owner commands and API admission remain separate: registration, `managed`, `close`, and `receive` verify exact enrollment, while automatic wake readiness follows the selected Hailing Frequencies delivery policy.

## Verification and delivery evidence

Run `python3 apps/discord-watch/verify.py` before using the worker.
`doctor` checks local configuration without printing credentials.
`status` reports the last observed API scan and each message's state.
A configured thread or a successful CMUX submit is not proof of receipt.
Only `receive` records the local output flush, and it is not proof of comprehension.
