# Pattern Buffer guide

Pattern Buffer restores approved worktree context to Claude Code and Codex at startup, resume and recovery.
It can also remind enrolled orchestrators to refresh a private checkpoint as context fills.
It is a local Python CLI with no daemon, database or model-based role detection.
It requires Python 3.10 or newer, Git, and macOS or Linux.

Print this guide with the installed launcher or a harness CLI:

```sh
pattern-buffer guide --print
$HOME/.local/bin/pattern-buffer guide --print
python3 "$HOME/.local/share/starfleet-ai/context-injector/pattern_buffer_codex.py" guide --print
python3 "$HOME/.local/share/starfleet-ai/context-injector/pattern_buffer_claude.py" guide --print
```

The standard installer puts `pattern-buffer` in `$HOME/.local/bin` and canonical per-harness CLIs beside the existing runtimes.
The per-harness commands above use the default runtime location; replace it with `$XDG_DATA_HOME/starfleet-ai/context-injector` when `XDG_DATA_HOME` is set.
If the launcher is not on `PATH`, run `$HOME/.local/bin/pattern-buffer guide --print`.
With a custom `--runtime-dir`, the installer places the generic launcher in that runtime directory unless `--bin-dir` is supplied.
The old `context_injector_{harness}.py` files remain the registered hook entrypoints, preserving existing trust and hook configuration.
The canonical `pattern_buffer_{harness}.py` aliases provide the renamed CLI.
Printing the guide reads the adjacent asset and writes only to stdout, so it works outside Git without enrollment or settings.

## Keep the two context documents distinct

**Standing context** lives in `.context-injector/context.md`.
It contains the approved mission, authoritative tracker, assignment, durable rules and authority boundaries.
Change it only within its approval requirements.

**The checkpoint** is the current implementation snapshot for your future self.
Keep the next action and active work, current decisions and relevant rejected approaches, exact commands still needed for the next action or recovery, active job IDs, evidence pointers, uncertainties and human holds.
Refresh it as a current snapshot and replace outdated state rather than appending.
Remove completed work, superseded decisions and obsolete commands.
Link useful history instead of repeating it.
A checkpoint does not change standing context or grant authority.

## Install or reuse the existing hooks

Install the user-level adapter once per harness from a Starfleet-AI checkout.
Reinstalling updates the existing `context_injector_{harness}.py` runtime path and preserves its hook marker and command.
It also installs the Pattern Buffer CLI aliases without adding another hook or trust identity.

```sh
python3 apps/pattern-buffer/pattern_buffer.py install --harness codex
python3 apps/pattern-buffer/pattern_buffer.py install --harness claude
```

If install refuses an unowned or damaged runtime entrypoint, preserve the existing runtime.
From a source checkout, use the same settings path and install into a new empty runtime:

```sh
python3 apps/pattern-buffer/pattern_buffer.py install \
  --harness codex \
  --settings /absolute/path/to/codex-settings.json \
  --runtime-dir /absolute/path/to/new-empty-runtime
```

Use the matching harness name and settings file for Claude.
The generated recovery command preserves whether checkpoint hooks were enabled for that settings file.
This updates only the selected harness's owned hook command and leaves worktree enrollment and checkpoint files in place.
Review the harness's normal reload and trust controls after the install; installation does not confirm delivery.

The generic `pattern-buffer` launcher goes to `$HOME/.local/bin` for a standard install.
Add that directory to `PATH` using your usual shell setup if you want the short command.
No worktree is initialized or enabled by installation.
Installing one harness refreshes recognized aliases already present in that same runtime directory, while retaining their stable hook commands.
It does not change another runtime directory, another harness's settings, or native trust.
Restart or resume the harness so it loads the existing configuration, then review and trust the Codex hook through its supported hook-management flow.

## Prepare and check a worktree

Inspect the intended worktree before changing setup:

```sh
pattern-buffer --cwd /absolute/worktree status --harness codex
```

For a directly managed owner, agree the complete context text, place it at `/absolute/worktree/.context-injector/context.md`, then initialize the worktree:

```sh
pattern-buffer --cwd /absolute/worktree init --default-role orchestrator
```

Use `--default-role implementer` or omit the option when appropriate.
Implementer worktrees should use their parent's dispatch and task replay instead of initializing a separate context copy.
For repository-specific instructions, create and review an external JSON profile based on [the example](examples/repository-profile.json), then select it with `--repo-profile-file /absolute/path/to/profile.json`.
`init` preserves an existing context file and never installs hooks.

Check both role budgets and inspect the actual rendered context before relying on the hook:

```sh
pattern-buffer --cwd /absolute/worktree validate --harness codex --session ACTUAL_SESSION_ID
pattern-buffer --cwd /absolute/worktree status --harness codex
pattern-buffer --cwd /absolute/worktree preview --harness codex --session ACTUAL_SESSION_ID
```

Use `--harness claude` for Claude and a real session identity when known.
`validate` checks both role sections and exits nonzero on malformed or oversized context.
It reports header bytes, usable content bytes and remaining UTF-8 bytes from the exact rendered header.
Use the exact session ID when available so the report includes the selected session file path.
`status`, `session-enroll`, `enrollment-status` and `handoff-check` use the same calculation.
Oversized or malformed required sources fail closed and are never truncated.
The hook uses the harness-reported working directory, not the directory from which installation was run.
Unregistered worktrees emit no context.

Use `handoff-check --harness HARNESS --session SESSION_UUID` for a read-only local successor check.
It reports standing context, optional checkpoint ownership and runtime compatibility separately from native binding and actual delivery.
Unavailable native or delivery evidence is marked unknown and does not add a readiness requirement.
For an exact Claude checkpoint owner it also reports the existing idle-warning minutes and timer state, if that checkpoint exists.
If an existing warning is disabled, its next action shows the supported `checkpoint-config` command without changing state.
New Claude checkpoints default to 50 minutes, while existing checkpoints remain unchanged until their owner enables a warning.
An armed timer is configuration evidence, not proof that its wake was received.

## Checkpoints for orchestrators

If several agents share this worktree with different missions, first use `session-enroll --harness HARNESS --session SESSION_UUID --context-file /absolute/private/reviewed-standing.md`.
It creates an ignored session-named file that completely replaces the worktree context for that session.
Keep the separate checkpoint for current implementation state.
Use `status --harness HARNESS --session SESSION_UUID` to inspect the selected file; missing or invalid required files emit a blocking stop-and-notify alarm instead of borrowing another mission.
Use `enrollment-release` and `enrollment-adopt` for an explicitly authorized successor, preserving exact identity and outgoing timer cancellation.
If a required binding is missing, invalid or inactive, pause product work and inspect `enrollment-status --harness HARNESS --session SESSION_UUID`.
Only already-authorized supported recovery is allowed: the named pending successor may adopt the reviewed handoff, finish its fresh checkpoint setup and validate; an active owner may restore an already-approved source with `session-enroll --reviewed-update --evidence REFERENCE`.
Do not resume product work until validation succeeds; notify the context owner through the configured contact workflow for unexpected state, missing authority or reviewed inputs, or failed recovery.
Read the [session enrollment and reliability guide](docs/session-enrollments.md) for commands, authority boundaries and failure recovery.

Print per-worktree delivery/publication evidence with `pattern-buffer --cwd /absolute/worktree reliability --since 7d`.
Logs are private, bounded to 30 days and 20 MiB by default, and cleaned during normal tool activity without another service.
Use `reliability --json` for structured evidence and `reliability-config --retention-days 30 --max-bytes 20971520` to configure only this tool's owned telemetry.
Emission, submission, receipt and publication are different facts; note accuracy and model obedience are not inferred.

Checkpoint enrollment is opt-in and does not assign a role.
Only an enrolled orchestrator can publish a checkpoint.
The initial call is a read-only take-stock briefing:

```sh
pattern-buffer --cwd /absolute/worktree checkpoint-enroll --harness codex --session SESSION_UUID
```

Re-read the mission, authoritative tracker and latest user direction, then reconcile active work, results, evidence, uncertainty and human holds.
After reviewing a durable private note, repeat with `--file /absolute/private/reviewed-checkpoint.md`.
Codex also requires a confirmed `--compact-at PERCENT` when the active threshold cannot be detected.
Enrollment reuses the standard hooks and does not assign a role or change native trust.

Maintain the returned `edit_this_file` path, which is the ignored draft validated for publication.
The hook publishes valid changes and maintains the recovery copy.
Check local enrollment with:

```sh
pattern-buffer --cwd /absolute/worktree checkpoint-status --harness codex --session SESSION_UUID
```

Read the [checkpoint lifecycle guide](docs/checkpoints.md) for file locations, Git-ignore behavior, ownership, save/read commands and recovery limits.

## Claude idle warnings

New Claude checkpoint enrollments default to a one-shot Temporal Beacon timer after 50 minutes of confirmed inactivity.
Existing enrollments remain unchanged until their owner enables the idle warning with `checkpoint-config` below, allowing a staged rollout.
Install the matching Hailing Frequencies runtime with idle-timer support before upgrading Pattern Buffer, coordinating an existing worker upgrade with its owner.
Enrollment and later ordinary prompts arm or replace the timer through the existing worker; no new daemon, hook identity or enrollment is created.
The timer's own wake is excluded, so its checkpoint-refresh response does not create another idle warning.
Other activity postpones the deadline; exact-session and human-input guards still apply.
The idle reminder repeats the current-snapshot guidance above and gives an estimated five-minute compaction window.
Waking the session may itself reset Claude's idle clock.
Percentage-based warnings remain independent, and Codex receives no automatic idle timer.

```sh
pattern-buffer --cwd /absolute/worktree checkpoint-config --harness claude --session SESSION_UUID --idle-warning-minutes 50
pattern-buffer --cwd /absolute/worktree checkpoint-config --harness claude --session SESSION_UUID --idle-warning-minutes 0
```

Run configuration from the exact owning Claude session.
Zero disables future arming and requests cancellation of the owned idle timer; already submitted input cannot be recalled.
Disabling checkpoint reminders or the checkpoint also cancels its known idle timer.
Inspect `checkpoint-status` for the timer ID and armed, unavailable, uncertain or cancellation state; use `temporal-beacon doctor` for coverage.
An armed timer is configuration evidence, not proof that its wake was received.

## Enroll Hailing Frequencies for the stronger reminder

The extra one-shot attention prompt requires Hailing Frequencies enrollment for this exact harness session and CMUX surface.
Complete both enrollments for standard orchestrator onboarding unless the operator excludes the extra attention path.
Read `hailing-frequencies guide --print`, then observe the exact live identities with `cmux current --json --limit 200` and `cmux --id-format both --json tree --all`.
If this session is not already enrolled, use the observed UUIDs in this command:

```sh
hailing-frequencies enroll --harness HARNESS --surface OBSERVED_SURFACE_UUID --session OBSERVED_SESSION_UUID --label pattern-buffer
```

If it is already enrolled, preserve that installation, state, enrollment and worker rather than creating duplicates or starting another worker for this feature.
Hailing Frequencies uses its current session, surface, composer and human-input guards.
A hold or unavailable result leaves the Pattern Buffer hook warning as fallback.

## Delivery limits

Local status, enabled flags, reminders and enrollment describe configuration or an attempt, not actual model reception.
Confirm reception through normal in-session observation before claiming the agent received, read or acted on a reminder.
Do not force compaction, change the active model profile, alter human holds or expand task authority to make a reminder appear.
Hook trust and runtime delivery remain subject to the harness's own controls.

For implementation details and limitations, see the [Pattern Buffer README](README.md), [default standing context template](templates/default.md), and [checkpoint lifecycle guide](docs/checkpoints.md).
