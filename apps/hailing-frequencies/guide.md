# Hailing Frequencies: safe setup

This guide helps you confirm your exact CMUX identity and use the machine's existing Hailing Frequencies installation safely.
Use the machine's existing `~/.local/bin/hailing-frequencies` launcher and default state directory.
Do not create a second state directory or start a duplicate `supervise` worker.
Use `hailing-frequencies status` to inspect saved enrollment and `hailing-frequencies health` for recent successful scans.
Neither establishes permission to start another worker; if worker availability is unclear, ask the existing owner or operator instead.

The Hailing Frequencies installer also installs the `agent-notify` Discord conversation command and its skill.
Use `agent-notify guide --print` for private Discord setup, credentials, forum permissions, and a bounded reply-receipt check.
Agent Notify uses the existing Discord Watch installation and worker when present; it does not start another worker.

## Idle continuation timers

Temporal Beacon shares this existing worker and exact-session enrollment.
Use `temporal-beacon set --after-idle 50m --message 'Refresh the checkpoint before possible idle compaction.'` for one continuation after native inactivity.
Activity postpones delivery, and receipt completes the timer; do not rearm it from its own wake.
Unknown activity, human-input holds and replaced sessions prevent submission.
Run `temporal-beacon doctor` before relying on delivery and read [the timer guide](https://github.com/morecoffeyplease/starfleet-ai/blob/main/apps/hailing-frequencies/temporal-beacon.md) for details.

## Identify the exact session

Run `cmux current --json --limit 200` and `cmux --id-format both --json tree --all`.
Match your actual harness session identity to its CMUX panel or surface UUID and workspace UUID.
Use the harness's reported identity, such as Codex's `CODEX_THREAD_ID` or OpenCode's exact `ses_` session ID, rather than choosing another visible agent.
In `cmux current`, match your own `agents[].session_id` and harness `kind`, then use that item's `projections[].panel_id` and `workspace_id` for enrollment.
The panel ID is the current terminal surface; do not substitute `stable_surface_id` or `durable_surface_id`.
Use the IDs CMUX reports; do not infer them from a workspace title, project folder, parent session, or helper terminal.
If the view is incomplete, the session is missing, or the identities disagree, stop and report the observed mismatch.

## Enroll only your own session

Enroll the exact session you are using, with its actual harness and surface:

```sh
hailing-frequencies enroll --harness claude --surface YOUR_SURFACE_UUID --session YOUR_SESSION_UUID --label YOUR_LABEL
hailing-frequencies enroll --harness codex --surface YOUR_SURFACE_UUID --session YOUR_SESSION_UUID --label YOUR_LABEL
```

Run the command matching your harness and replace each placeholder with the observed value.
Do not use `--include-existing`; enrollment should cover messages created after enrollment.
Prefer self-enrollment for your own exact session.
A coordinator may help enroll another session only with explicit operator authorization and the exact observed session, surface, and workspace identities.
If the session is already enrolled unchanged, keep its existing cutoff and receipts.
After enrollment, run `hailing-frequencies status` to confirm that the exact session is listed.
Enrollment confirms saved configuration only; it does not prove a reminder reached or was read by the agent.

### OpenCode self-enrollment

OpenCode uses `hailing-frequencies opencode-enroll` with a private binding manifest supplied by the Model Preferences worker.
The manifest fields are `session_id`, `expected_server_pid`, `session_directory`, `worktree_directory`, `cmux_workspace_uuid`, `cmux_surface_uuid`, and `parent_task_id`, plus `api_url` and `password_file`.
Optional `delivery_policy` accepts `native_empty_composer` or `api_sidechannel`; omission selects `native_empty_composer`.
The policy is stored with the enrolled target, and changing it requires explicit unenrollment and reenrollment.
The manifest and password file must be absolute regular files owned by you with mode `0600` or stricter and no symlink.
The password file contains the OpenCode loopback API password, and the password itself never belongs in the manifest, command arguments, or logs.
Before the initial task, the managed OpenCode launcher must seed `CMUX_AGENT_LAUNCH_KIND=opencode`, `OPENCODE_SESSION_ID`, `CMUX_SURFACE_ID`, and `CMUX_WORKSPACE_ID` through the supported per-session API environment after CMUX placement.
The OpenCode server does not inherit the TUI's CMUX environment, so enrollment refuses to infer caller identity from server process variables, working directory, or manifest fields alone.
Run self-enrollment as a command launched by the exact OpenCode TUI on its owning CMUX surface with those three values present:

```sh
hailing-frequencies opencode-enroll --manifest /absolute/private/opencode-binding.json --label YOUR_LABEL
```

Enrollment verifies the exact current OpenCode hook record, matching CMUX surface and workspace, matching API session and server PID, matching worktree, session generation, and enabled native messaging.
It may register the exact session while that API session is busy because registration records identity rather than send readiness.
Each delivery still requires an inactive API session with no pending inbox, permission, or form state, and it rechecks all native composer and human-input guards.
The default policy requires an explicitly empty composer.
The experimental `api_sidechannel` policy permits only `empty`, `draft`, or `unknown` composer classifications and sends an explicit queue request with resume enabled.
Both policies still require exact agent and terminal identity, an active native lifecycle, exact current and hook owner, no blocking input, no human-input obligation, active native receiving, an inactive API session, and empty API inbox, permission, and form lists.
Passive API sessions without a current visible-TUI hook record, a session observed on another surface, or any mismatch remain held.
The default policy holds when CMUX reports OpenCode `surface.input_state` as `unknown`.
The API does not expose composer drafts, and `api_sidechannel` is an explicit experimental choice to queue work without claiming that the composer is empty.
The opt-in never bypasses missing or mismatched identities, dialogs, disabled receiving, active API work, or pending inbox, permission, or form state.

## Checkpoint attention

The stronger Pattern Buffer compaction warning requires that session's Pattern Buffer checkpoint registration.
See the [Pattern Buffer checkpoint guide](https://github.com/morecoffeyplease/starfleet-ai/blob/main/apps/pattern-buffer/docs/checkpoints.md) for that separate setup.
Optional Hailing Frequencies attention delivery also requires this exact Hailing Frequencies session enrollment.
During an eligible pressure warning, Pattern Buffer makes one Hailing Frequencies attempt with a three-second timeout.
The urgent Pattern Buffer warning remains the fallback if the delivery is busy, held, unavailable, timed out, or uncertain.
The hook does not retry the event; do not erase receipts or use a new key to force an uncertain submit again.
`submitted` means CMUX confirmed a guarded submit or OpenCode's API admitted an exact user message, not that the agent received, read, or acted on it.

## Preserve holds and permissions

Current binding, human-input, and native messaging guards must pass for delivery.
Approvals, missing identities, disabled messaging, active API work, pending inbox, pending permission or form state, and unknown guard obligations are reasons to stop and report under either delivery policy.
The default policy also holds draft and unknown composer classifications.
Do not clear a hold, force terminal input, change CMUX socket policy, widen permissions, or install hooks to bypass a refusal.
If the sandbox or another permission boundary denies an operation, preserve state and report the exact error to the operator or an explicitly authorized coordinator.
Do not widen permissions or impersonate another session to get past a denial.

## Read more

See the Hailing Frequencies README sections on [installation](https://github.com/morecoffeyplease/starfleet-ai/blob/main/apps/hailing-frequencies/README.md#install-without-activating), [session enrollment](https://github.com/morecoffeyplease/starfleet-ai/blob/main/apps/hailing-frequencies/README.md#enroll-exact-sessions), and [delivery safeguards](https://github.com/morecoffeyplease/starfleet-ai/blob/main/apps/hailing-frequencies/README.md#delivery-and-safeguards).
