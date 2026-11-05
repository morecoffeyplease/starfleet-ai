# Discord Watch setup guide

Discord Watch polls registered agent-notify forum threads for replies from the configured Discord user and keeps those replies in a private local queue.
With `--send`, one supervised worker sends a guarded wake to the exact Claude, Codex, or explicitly bound OpenCode session enrolled for each thread.
The wake contains the exact `discord-watch receive` command and never contains the reply body.

## Prerequisites

Use Python 3.9 or newer on macOS or another POSIX host with `fcntl`.
Install or reuse `agent-notify`, Hailing Frequencies, and a current CMUX command that supports `current` and `surface.input_state`.
Claude and Codex also require JSON `paste --submit` results, while OpenCode requires the supported Hailing Frequencies API binding and a native empty-input result.
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
OpenCode registration, `managed`, `close`, and `receive` verify the exact Hailing Frequencies owner identity without checking composer readiness.
Automatic wake checks the exact surface, session, workspace, hook generation, receiving settings, and native input state separately.
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
For a new or existing thread, the default starting cursor is the thread creation ID so older replies are replayed safely.
An existing agent-notify cursor records messages fetched by the old listener, not replies proven handled by its owner.
Pass `--after LAST_ACTUALLY_HANDLED_ID` only when the owner has confirmed that every earlier reply was handled.
An explicit cursor must be at or after the thread creation ID.
The legacy agent-notify listener should stop polling a thread after `managed` reports it as managed.
`managed` succeeds only for an open registration owned by the exact current session.
Its JSON includes worker health, and a stale worker warning does not change ownership.

Only the current registered owner can receive or locally close a thread.
For OpenCode, the wake command includes its exact `--session ses_ID` owner so reply receipt does not depend on a guessed terminal or a tab title.
To hand an open registration to a different current enrolled session, run `discord-watch transfer THREAD_ID --from-session OLD_SESSION_UUID` from the new owner's session.
Transfer requires the old session ID to match the stored owner and preserves every unreceived reply.
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
Status distinguishes a confirmed CMUX submit or OpenCode user-message admission from a recipient receipt and reports held, uncertain, and stalled states without printing reply bodies.
After the Discord thread is closed remotely, `close` closes its local registration and preserves unreceived replies for the current owner.

## Verification and delivery evidence

Run `python3 apps/discord-watch/verify.py` before using the worker.
`doctor` checks local configuration without printing credentials.
`status` reports the last observed API scan and each message's state.
A configured thread or a successful CMUX submit is not proof of receipt.
Only `receive` records the local output flush, and it is not proof of comprehension.
