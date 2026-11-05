# Discord Watch

Discord Watch polls explicitly registered agent-notify forum threads and queues replies from the configured Discord user.
One supervised polling worker serves all registered threads independently of per-thread `agent-notify listen` processes.
Each registered thread retains one exact owner copied from that session's Hailing Frequencies enrollment, including after local closure or remote archiving.

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
Each registration also keeps an immutable initial intake cutoff, set to the thread creation ID by default or to the explicit `--after` ID.
Gateway replays at or below that cutoff are ignored, while later unseen messages can still be ingested out of order even below the advancing REST cursor.
The upgrade migration uses each existing registration's committed cursor as its conservative initial cutoff without changing queued rows or cursor state.

Local registration closure and Discord conversation closure are recorded separately from reply-subscription eligibility.
Closing a registration or observing a Closed tag or archived thread does not unsubscribe it, reset its cursor, or discard its catch-up boundary.
The worker continues polling closed and archived threads while their subscriptions are active, and status reports local `open`, remote `remote_closed`, and `subscription_state` independently.
Meaningful activity means a newly stored message from the configured Discord user, using that message's Discord timestamp.
When no such reply has been recorded, the seven-day window starts at registration time.
An exact seven days of inactivity remains eligible, while more than seven days makes the subscription dormant and stops routine REST polling.
Polls, retries, reminders, and worker heartbeats do not change the activity timestamp.
An inserted reply at least as recent as the latest activity reactivates the subscription and starts its seven-day window from that timestamp.
Duplicate IDs and older replays remain ineligible to extend the activity window.
The narrow `Watch.ingest_gateway_message` method uses the same validation, filtering, attachment storage, and deduplication path as REST intake, and does not advance the REST cursor.
When a payload supplies `channel_id`, intake requires it to match the selected registered thread.
Queued replies remain available for guarded delivery while a subscription is dormant, and missing or stale exact owners continue to hold delivery.
Use `discord-watch scan --only-thread THREAD_ID` for explicit REST reconciliation of a dormant thread.
When Gateway intake is in use, reconnect reconciliation must include every retained known thread, including dormant threads, before the integration reports recovery.

New replies for one exact owner generation are grouped after three seconds of quiet, with a maximum ten-second delay from the first queued reply.
Quiet time uses local intake time for unique configured-user messages, while the maximum delay starts at the first message in that pending group.
Each group holds at most 20 messages and 256 KiB of the exact rendered UTF-8 receive output.
The same renderer checks the byte limit and prints full message bodies plus supported attachment filenames and HTTPS URLs.
Messages are ordered by numeric Discord message ID, and count or byte overflow is sealed into later ordered groups without dropping or summarizing replies.
The accepted single-message body and attachment bounds fit inside the receive output bound.
New groups remain unsealed while REST catch-up is incomplete, and newer frozen groups remain held until catch-up completes.
Group membership and a durable attempt intent are stored before guarded native submission.
The wake contains a command that invokes the active Discord Watch runtime with its qualified Python interpreter and the immutable group ID, without including reply bodies.
An uncertain submission remains blocked indefinitely, while a confirmed group waits for an explicit receipt without redundant wake submissions.
Later replies remain queued and are grouped after the earlier group is received.
Status reports forming groups, held groups, intent or uncertain submissions, confirmed groups awaiting receipt, each member's receipt, and later queued replies.

For each thread, registration stores the exact Hailing Frequencies harness, surface, workspace, session, and generation.
The central worker checks that the same target remains enrolled and validates the current CMUX projection and input guard before each wake.
It never forces input, reads or clears CMUX messages, launches another agent, or sends a reply body in a wake.
The prompt names one exact `discord-watch receive --thread THREAD --group GROUP` command for the frozen group.
An empty composer with no human-input obligation is required by Hailing Frequencies' existing `Cmux.submit` guard.
Attachment filenames and HTTPS URLs are printed to the exact recipient during `receive` without downloading the files.

The worker records a remote archived thread or the cached `Closed` forum tag without closing the local registration or stopping reply intake.
Discord access errors, missing permissions, and rate limits remain visible per thread and do not look like remote closure.
Rate-limit `retry_after`, HTTP status, Discord error code, message, and global-limit scope are retained as structured status data.
A global rate limit defers all thread requests until its retry time, while a per-route limit defers that thread.
One thread's API error does not stop polling other registered threads.
Each request has a finite timeout, response and message sizes are bounded, and catch-up is limited to 50 pages per scan.

A confirmed CMUX submit moves each unread group member to `awaiting_receipt`.
A confirmed group is not submitted again while awaiting receipt.
A transport failure after CMUX begins submission is `uncertain` and is never retried automatically.
Existing stalled, confirmed, uncertain, and per-message attempt evidence remains visible after migration.
A current exact owner can explicitly receive the remaining members of an uncertain or stalled group, or a held group when the owner guard is available.
No operator command resets an uncertain send to pending.

`receive` holds the shared local consumer lock while writing exact records and flushes stdout before saving receipts.
It then revalidates the exact live owner and atomically receipts only the message IDs that were written from the selected frozen group.
An older supported CLI can still receive an exact message using `--message`; the new CLI reconciles those per-message receipts and prints only the remaining unread members of a group.
Group wake commands invoke the active immutable runtime directly so an older launcher elsewhere on `PATH` cannot receive an unsupported `--group` option.
This flush is the CLI delivery boundary and is not proof that a person or model read or understood the reply.
If output fails or the current owner changes before the receipt commit, the reply remains available for another receive.
`managed` checks exact current ownership of the retained subscription, including locally closed and dormant registrations, and includes the latest worker health report.
Its success is independent of worker freshness so the caller can retain ownership while diagnosing a stale worker.
`close` preserves all queued replies, is idempotent for the caller's exact registration, and closes only its local management state while retaining the reply subscription.
`transfer` requires an explicit `--from-session` matching the stored owner and must be run from the new exact enrolled session.
An explicit transfer can change the owner of a locally closed retained subscription without reopening it.
Transfer preserves frozen group membership and lets the explicitly authorized new owner receive its unread members through the recorded ownership-event chain.
Unsealed messages can be assigned to the new generation, and a group never combines messages from different generations.
An unexplained generation change without the matching ownership-event chain holds its groups.
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
