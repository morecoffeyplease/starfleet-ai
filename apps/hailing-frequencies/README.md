# Hailing Frequencies

Hailing Frequencies watches native CMUX message queues and prompts explicitly enrolled Claude Code and Codex agents to check overdue messages.
Its bounded OpenCode path accepts an exact `ses_` binding manifest and sends only through the authenticated loopback API after the native session, API, and receiving guards pass.
The default `native_empty_composer` policy requires CMUX to report an explicitly empty composer.
An explicit experimental `api_sidechannel` policy queues OpenCode attention while the composer state is `draft` or `unknown`, while retaining the exact session, surface, worktree, generation, receiving, API-idle, inbox, permission, form, and human-input guards.
OpenCode's supported TUI plugin API does not expose composer draft contents, so this opt-in does not infer or claim that the composer is empty.
It is a local Python 3 standard-library CLI, with an observe-only default, opt-in supervised monitor, and independent health alerts.
It does not own messages, mark another agent's messages read, poll Discord, install harness hooks, change models, or grant task authority.

Temporal Beacon adds durable agent wake timers to this same worker.
Read the [Temporal Beacon guide](temporal-beacon.md) for `--after`, `--at`, `--every`, time-based `--until`, receipt, installation, and diagnosis, or load its [agent skill](skills/temporal-beacon/SKILL.md).
Timer delivery uses the existing enrolled-session guards and opt-in send scope; unread-message reminder timing remains separate.

The Hailing installer also installs the `agent-notify` Discord conversation command, its [setup guide](agent-notify-guide.md), and its [agent skill](skills/agent-notify/SKILL.md).
Print its setup instructions with `agent-notify guide --print` before creating Discord configuration or a worker.
Agent Notify keeps its existing private configuration and thread routing state, and registers posts with the existing Discord Watch worker when available.

## Start here

Print the concise [safe setup guide](guide.md) from any directory, before initialization, with `hailing-frequencies guide --print`.

An ordinary peer message can remain queued while its recipient works.
Hailing Frequencies sends a short attention prompt through the recipient's terminal input path instead of putting another reminder into that same message queue.
The prompt uses one safety line and an exact inbox reader command that carries the selected message IDs.
The monitor never copies message bodies into its database or logs.
Recipients preserve each sender's request scope and existing holds, and address actionable requests at a safe checkpoint without canceling running work solely because a reminder arrived.

The default timing is a reminder after 300 seconds, escalation after 900 seconds, a 600-second per-recipient cooldown for each kind of prompt, and a scan every 30 seconds.
These are configurable initial defaults, not a claim that the operator has approved a fleet-wide timing policy.
Changing a timing setting requires stopping the monitor and editing its private `config.json` before restarting.
`init` also accepts `--remind-after`, `--escalate-after`, `--cooldown`, and `--interval`, all in seconds.

## Install without activating

For idle-aware continuations, Temporal Beacon supports `set --after-idle 50m --message TEXT` using the same worker.
It is one-shot, postpones delivery on native activity and checks inactivity again immediately before guarded submission.
See [idle scheduling semantics](temporal-beacon.md#scheduling-semantics); no additional service or enrollment scope is created.

From the repository root:

```sh
python3 apps/hailing-frequencies/verify.py
python3 apps/hailing-frequencies/install.py
hailing-frequencies init
hailing-frequencies status
```

The installer copies the source, the read-only OpenCode session adapter, printable guides, and skills to an immutable SHA-256 directory under `~/.local/share/starfleet-ai/hailing-frequencies/` and writes the owned launchers `~/.local/bin/hailing-frequencies`, `~/.local/bin/temporal-beacon`, and `~/.local/bin/agent-notify`.
It prints immutable paths for the Temporal Beacon and Agent Notify skills.
Follow [Temporal Beacon skill registration](temporal-beacon.md#enable-agent-discovery) or the Agent Notify guide to make the selected skill discoverable in a harness.
Removing a development worktree therefore does not remove the installed runtime.
Installation starts no process and changes no Claude, Codex, CMUX, Discord, or launchd settings.
Use Python 3.9 or newer on macOS or another POSIX host with `fcntl`.
Use a current CMUX build providing `current`, `surface.input_state`, native message listing/settings, and JSON `paste --submit` results.
Claude Code must support the host's immediate-send key; the initial pilot used v2.1.312.

## Enroll exact sessions

Run `cmux current --json --limit 200` and `cmux --id-format both --json tree --all` to observe the actual harness session, surface, and workspace.
Do not guess identities from a workspace title, inherit a parent's identity for a child, or enroll helper terminals.
Claude and Codex enrollment validates CMUX's current projection, the harness hook record, the workspace, the session generation, native receiving settings, and a free agent composer.
Claude additionally requires the active session binding for that surface.
The normal `backgroundWorkPending` lifecycle is eligible when the exact owner is present and the composer is empty with no human-input obligation.
Omitted historical child-session records do not block a positively identified current owner; a missing exact owner or a truncated whole-surface snapshot still refuses enrollment and sending.
If enrollment refuses a draft, permission prompt, missing session, disabled messaging, or incomplete projection, resolve the actual condition instead of forcing input.

```sh
hailing-frequencies enroll --harness claude --surface SURFACE_UUID --session CLAUDE_SESSION_UUID --label claude-agent-1
hailing-frequencies enroll --harness codex --surface SURFACE_UUID --session CODEX_SESSION_UUID --label cpu-officer
```

OpenCode uses a separate explicit self-enrollment command because its session IDs are `ses_` strings and it has no Claude/Codex hook file.
The Model Preferences worker supplies a private manifest containing the exact API session, loopback server, PID, worktree, and CMUX surface binding.
The manifest uses the OpenCode adapter field names `session_id`, `expected_server_pid`, `session_directory`, `worktree_directory`, `cmux_workspace_uuid`, `cmux_surface_uuid`, and `parent_task_id`, plus `api_url` and `password_file`.
It may include `delivery_policy` with the exact value `native_empty_composer` or `api_sidechannel`; an omitted value defaults to `native_empty_composer`.
The selected policy is stored on the enrolled target, and changing it requires explicit unenrollment and reenrollment.
The manifest and password file must be regular owner-only files with no symlink, and the password itself must exist only in the password file.
The managed OpenCode launcher must seed `CMUX_AGENT_LAUNCH_KIND=opencode`, `OPENCODE_SESSION_ID`, `CMUX_SURFACE_ID`, and `CMUX_WORKSPACE_ID` through the supported per-session API environment after CMUX placement and before the initial task.
The OpenCode server does not inherit the TUI's CMUX environment, so enrollment refuses to infer those caller identities from the server process, working directory, or manifest alone.
Run self-enrollment from a command launched by the exact OpenCode TUI on its CMUX surface with those three values present:

```sh
hailing-frequencies opencode-enroll --manifest /absolute/private/opencode-binding.json --label opencode-owner
```

Enrollment requires the current OpenCode hook record, exact CMUX surface and workspace, matching API session and server PID, matching native worktree, and enabled native messaging.
The default policy requires an explicitly empty native composer before sending, while `api_sidechannel` permits only exact `empty`, `draft`, or `unknown` composer classifications.
Both policies require agent and terminal identity, `blocks_typing: false`, `waiting_on_human: false`, an active native lifecycle, exact current and hook state, and an inactive API session with empty inbox, permission, and form lists before sending.
OpenCode enrollment verifies authenticated API, exact session, PID, directories, native surface, workspace, worktree, and session generation, and may register while the exact API session is busy.
Registration does not establish send readiness or bypass any later busy, inbox, permission, form, composer, human-input, or receiving guard.
Passive server-opened sessions, missing hook records, a different CMUX surface, disabled receiving, an input dialog, or an unknown guard obligation remain held.

UUIDs above are placeholders for observed identities.
Enrollment normally covers only messages created after enrollment.
Use `--include-existing` to include an enrolled session's existing backlog; messages older than that session's recorded start are always excluded.
Re-enrollment of an unchanged identity preserves its original cutoff and receipts.
Session replacement never transfers enrollment or pending prompts automatically.
Unenroll the old session explicitly, inspect the replacement, then enroll it separately.

To receive escalation prompts, designate one exact enrolled coordinator using `enroll ... --coordinator`.
Only one coordinator is selected in a state directory.
With no coordinator, overdue escalation remains visible in local scan output and `status`.
The coordinator gets the overdue source IDs and recipient identity; escalation itself grants no authority to act for that recipient.

## Observe, then send

```sh
hailing-frequencies scan
hailing-frequencies serve
```

Both commands are observe-only unless `--send` is present.
Review `would_submit`, holds, source IDs, and recipient identities before enabling sends:

```sh
hailing-frequencies scan --send
hailing-frequencies serve --send
```

For a canary rollout, keep every enrollment and limit sending to selected session UUIDs:

```sh
hailing-frequencies serve --send --only-session CLAUDE_SESSION_UUID --only-session CODEX_SESSION_UUID
```

All other enrolled sessions remain observe-only, without consuming reminder receipts.
Escalation cannot submit to a coordinator outside that active send scope.
Unknown session UUIDs refuse the scan rather than broadening coverage.
Stop the server and omit `--only-session` to activate all enrolled recipients after the canaries pass.

Run `supervise` in one dedicated CMUX terminal so its child CLI calls retain CMUX socket authorization.
The supervisor restarts a crashed `serve` worker with bounded backoff while preserving enrollment and durable receipts.
It stops its worker on SIGTERM, SIGINT, or SIGHUP and does not restart after an intentional stop.
A second supervisor or worker for the same state refuses ownership.
CMUX currently rejects launchd clients under `cmuxOnly`, so terminal closure still stops monitoring.
This product never changes socket access policy or copies socket credentials into launchd.
Install the independent health watcher to receive a macOS notification if successful scans stop or the latest scan fails:

```sh
hailing-frequencies supervise --send
# In another terminal, install the health reader (it does not start the monitor):
hailing-frequencies health-service install
hailing-frequencies health
hailing-frequencies health-service status
```

The launchd health reader runs every 30 seconds, persists its findings in `health.json`, and requests a local notification after the larger of 120 seconds or four scan intervals without success.
Notification presentation depends on macOS notification settings; a requested notification is not proof that the operator saw it.
A failed latest scan is unhealthy immediately.
Notifications repeat at most hourly per unhealthy episode; recovery permits the next failure to notify again.
The watcher cannot access CMUX, send prompts, restart a closed terminal, or establish coverage after reboot.
After login, it warns until the operator starts `supervise` in CMUX again.
Use `health-service uninstall` to remove only the owned watcher.
The independent `health` command reads atomic reports without taking the scan lock.
Only one supervisor and its child monitor can own a state directory, and individual enrollment, status, and scan operations use a separate short lock.
SIGINT, SIGTERM, or SIGHUP stops the supervisor and its worker without restarting it.
Shutdown waits for the current CMUX call, which is bounded to 15 seconds, or at most one second between scan intervals.
Transient CMUX failures pause sends for that scan and are printed as structured errors.
If `last_scan.at` is old, the monitor is not evidence of healthy ongoing coverage: inspect the owning terminal and its errors.
A busy short state lock skips that scan without writing `last-error.json` or advancing the successful-scan heartbeat.
The health reader continues to evaluate the last successful scan and preserves any newer real scan failure until a later successful scan recovers it.

For isolated pilots, put global options before the subcommand:

```sh
hailing-frequencies --state-dir /absolute/private/pilot init --remind-after 10 --escalate-after 60 --interval 5
hailing-frequencies --state-dir /absolute/private/pilot status
```

Use a single operator-owned state directory for the fleet.
Separate state directories deliberately do not share enrollment or deduplication, so do not run overlapping live monitors against the same recipients.

## Delivery and safeguards

Each submit rechecks the current session, workspace, start generation, native message switches, and `surface.input_state`.
Only an explicitly empty agent terminal with no human-input obligation is eligible.
Unknown state, incomplete projections, closed sessions, waiting-for-input sessions, drafts, and permission dialogs hold delivery.
The actual transport is one guarded `cmux --json paste --surface UUID --submit -- TEXT` operation with a multiline attention prompt.
CMUX chooses the agent-aware submit key; the app never uses `--force`, Escape, Ctrl-C, a separate Enter, or model/session launch commands.
Native CMUX input guards remain enabled.
The host still owns concurrent keyboard/input handling; this is not a transaction that freezes a human's keyboard or atomically binds a prompt to a harness session across a restart.
Do not use it as an unattended high-impact command sender.

OpenCode sends use `POST /api/session/{ses_id}/prompt` after the read-only adapter confirms the expected 2.2.5 server PID, exact session directory, inactive session, and empty inbox, permission, and form lists.
The default policy requires CMUX `surface.input_state` to report the exact empty composer.
The explicit `api_sidechannel` policy sends `text`, `delivery: queue`, and `resume: true` while the composer classification is `draft` or `unknown`, after all other native identity, lifecycle, human-input, and receiving guards pass.
Missing or malformed API/native guard data, busy sessions, queued inbox work, permissions, forms, disabled receiving, drafts under the default policy, and human-input obligations record a durable hold with zero API sends and zero attempt consumption.
The same held event can be checked again after the state becomes clear, while an API response without an exact session message ID and expected queue admission remains uncertain and is never retried.
The admitted user-message ID is API admission evidence only and does not prove that a model received or acted on the prompt.

The monitor checks incoming queued and delivered-but-unread records for enrolled session generations, and exact reply metadata from a complete inbox snapshot.
Outbound messages returned by CMUX inbox listing are excluded by exact recipient identity.
An incomplete 1,000-message scan for any enrolled surface refuses all sends instead of silently missing older records.
Unenrolled and retired surfaces do not fill an enrolled surface's scan budget.
Read/native-delivery states are not proof that a request was acted on.
The monitor never clears, claims, or edits native queue records.
An exact reply from the enrolled recipient to the original sender acknowledges the source locally, including when CMUX still records it as unread.
For queued or unread messages without a delivered hook notification, the enrolled recipient can output exact records and acknowledge their queue alerts in one command:

```sh
hailing-frequencies inbox --session OWN_ENROLLED_SESSION_UUID --message SOURCE_MESSAGE_UUID [--details]
```

Repeat `--message` for additional IDs, up to 20 per command.
The command requires the caller's `CMUX_SURFACE_ID` to match the enrolled surface and verifies the current session binding, workspace, and generation.
OpenCode inbox reads verify the authenticated API identity, exact native owner, worktree, generation, and enabled receiving while allowing the recipient's tool turn to be running.
Prompt delivery continues to require the full API-idle, pending-interaction, composer, and human-input guards.
It validates every ID before output, writes the selected message records as one JSON document, and flushes stdout before saving local acknowledgments.
Output is limited to 64 KiB; select a smaller batch when the limit is reached.
The command waits up to 30 seconds for an active scan or another short state operation to release its lock.
By default, output includes the message ID, sender name and identities, creation time, reply reference, and body.
Add `--details` to include each complete native CMUX record.
Both output modes validate the full native source records and use the same size limit and acknowledgment checks.
Invalid IDs, changed bindings or messages, saturation, incomplete output, and stdout failures do not acknowledge any selected message.
The command does not change CMUX read or delivery state, and it does not store message bodies in receipts or logs.
A successful stdout flush is a delivery boundary for this CLI, not proof that a person or model read or understood the output; bytes can still be lost after the flush.
Only the enrolled recipient can use `inbox` for that recipient's records, and a coordinator cannot consume on another recipient's behalf.
When the owned installed launcher points to the active immutable runtime with the default state and CMUX command, reminder prompts use that stable launcher.
For custom runtimes, state directories, CMUX executables, or a mismatched launcher, prompts print a fully qualified, shell-quoted command with the active paths.

For messages already received through a hook notification, an agent can also explicitly acknowledge its own source IDs:

```sh
hailing-frequencies ack --session OWN_ENROLLED_SESSION_UUID --message SOURCE_MESSAGE_UUID
```

Repeat `--message` for multiple source IDs.
The `ack` command waits up to 30 seconds for an active scan or other short state operation to release its lock.
If the lock remains busy, it exits with instructions to retry at the next checkpoint.
Other state commands retain immediate lock behavior.
The command requires the caller's `CMUX_SURFACE_ID`, the enrolled current binding, and exact inbound workspace and generation metadata.
The coordinator cannot acknowledge for another recipient.
Acknowledgment resolves only the queue alert and never means the underlying task is complete.
Scan output separates native unread records, acknowledged records, and remaining overdue records.
No body text is used to infer acknowledgment or stored in receipts.
Escalation tells the coordinator to inspect metadata and contact the recipient rather than acting as that recipient.

An agent can explicitly submit one checkpoint prompt to its own enrolled session with `attention-once`:

```sh
printf '%s' 'Review the current checkpoint now.' | hailing-frequencies --state-dir "$HOME/.local/state/starfleet-ai/hailing-frequencies" attention-once --session OWN_ENROLLED_SESSION_UUID --event-key CHECKPOINT_EVENT_KEY
```

The command requires `CMUX_SURFACE_ID` to match the enrolled surface and uses the same current binding, human-input, and native messaging guards as monitor prompts.
For a Claude native team wrapper, CMUX reports `CMUX_AGENT_LAUNCH_KIND=claudeTeams`, which the CLI recognizes as Claude; other present launch or session identities must match the enrolled target.
The session must already be enrolled; the command cannot select another recipient or bypass a hold.
The plain UTF-8 message on stdin is limited to 4 KiB.
The event key is limited to 128 ASCII letters, digits, dots, underscores, colons or hyphens and is scoped to that session generation.
The CLI prints one bounded JSON result with `outcome`, `attempted`, `detail`, and `event_key`; inspect `outcome` rather than treating a successful CLI exit as proof of submission.
It exits 2 for `rejected` and 0 for other structured outcomes.
An exact repeated key and message returns its saved result, while reusing the key with a different message is rejected.
The receipt stores only the message SHA-256, never the message body.
Preflight holds and busy-state results record no intent, so the same key may be tried later after the hold clears.
A final binding hold after intent reservation is recorded and that key is never retried.
After the transport starts, any exception or unconfirmed submit remains `uncertain` and the key is never replayed.
There is no reset or resend option; inspect the recipient's composer before issuing a new event key after uncertainty.

SQLite intent receipts are committed before transport.
Restarting the monitor cannot automatically replay an attempted message/kind pair, including a timeout, crash, or unconfirmed host submit.
If the paste succeeded but submission was not confirmed, the result remains `uncertain`; inspect the recipient's composer before doing anything else.
There is intentionally no automatic retry or force-reset command for an uncertain submit.
Have the original sender issue a new message only after the operator reconciles what happened.
New messages can trigger another reminder after the cooldown; an already-attempted message cannot repeatedly interrupt its recipient.
Escalation is separately deduplicated and sent only to a distinct enrolled coordinator.

## Inspect and remove

```sh
hailing-frequencies status
hailing-frequencies unenroll --session SESSION_UUID
python3 apps/hailing-frequencies/install.py --uninstall
```

Remove the health watcher before uninstalling the launcher or replacing a runtime used by that watcher.
Reinstall `health-service install` after a runtime update so its immutable runtime pointer follows the verified version.

State defaults to `~/.local/state/starfleet-ai/hailing-frequencies/` with private permissions.
It contains configuration, SQLite receipts, and the latest scan report.
`status` shows the latest 50 attempt batches, including uncertainty; logs and receipts stay outside repositories.
Stop the monitor before uninstalling its launcher.
Uninstalling preserves the immutable runtime and private state for audit or rollback.
Rollback means stopping the server and running an earlier immutable runtime explicitly with Python, or reinstalling an earlier verified source; do not delete receipt state to make an old version resend.

## Verification and rollout

```sh
python3 apps/hailing-frequencies/verify.py
git diff --check
```

Verification uses disposable state, fake CMUX boundaries, a real installed-launcher subprocess, and Agent Notify's fake Discord and Away Mode shell fixtures.
Agent Notify's routing test does not post to Discord.
It exercises stale identities, human-input holds, disabled messaging, read races, cooldown, escalation, saturation, crash-before-send intent, uncertain delivery, restart suppression, and installer ownership.
There is no browser UI.
Real harness attention and host submit-key behavior require a separate disposable live pilot; an accepted transport response is not evidence of model acknowledgment.

The initial interactive pilot used one Haiku session and one Luna session, each executing a series of short foreground commands.
Luna acknowledged a submitted follow-up before step 6 of 16 and resumed its task.
Haiku's plain Enter submission waited, while its immediate-send shortcut acknowledged in about 5 seconds and resumed the remaining steps.
Those observations establish the harness interaction, not arbitrary maximum acknowledgment latency or production monitor coverage.
The PR's evidence records the separate end-to-end monitor pilot and exact final verification candidate.

Roll out in stages: install without activation, observe one Claude and one Codex session, enable sends only for those canaries, inspect actual acknowledgments and holds, then enroll the rest of the fleet explicitly.
Agree the reminder and escalation thresholds before the broad rollout.
Pause or remove a recipient with `unenroll`; stop the supervisor to pause all sends.
The health watcher will warn about that stop until monitoring resumes or the watcher is uninstalled.
Native CMUX message monitoring and Discord Watch reply delivery remain separate paths with separate state and worker ownership.
