# Orchestrator checkpoints

A checkpoint is a short note to your future self about current implementation state.
It is separate from the approved mission and standing rules in the worktree default `.context-injector/context.md` or an explicitly enrolled session-named standing file.
For multiple orchestrators sharing a checkout and for replacement ownership, follow the [session enrollment guide](session-enrollments.md).
The standing context injection supplies stable mission, role, rules and authoritative references; its changes follow the lane's approval requirements.
The letter to yourself is the agent-maintained current implementation snapshot, updated within existing work authority.
When a checkpoint is enrolled, its recovery reminder directs the agent to read and reconcile that letter; the letter cannot change standing policy or grant permission.
Update it after a meaningful decision, worker result, verification outcome, blocker or authority change changes what you should do next.
Batch related changes into one update; do not rewrite it after every tool call or unchanged feedback message.
Refresh it as a current snapshot and replace outdated state rather than appending.
Remove completed work, superseded decisions and obsolete commands.
Keep the next action and active work, current decisions and relevant rejected approaches, exact commands still needed for the next action or recovery, active job IDs, evidence pointers, uncertainties and human holds.
Link useful history instead of repeating it.

## Shared recovery reminder

Every rendered context includes a short recovery reminder for every role, even when no checkpoint is enrolled.
After compaction or recovery, it directs agents to reread the installed `pstack-runtime` and `poteto-mode` entry points and relevant playbook and skill references, then use PStack `recall` as documented there.
The reminder asks agents to reconcile the active brief, latest user direction, role and optional checkpoint before continuing.
Do not rely on remembered or summarized instructions.
If a reference is unavailable, report that limit and continue within current authority without guessing its contents or installing it.
The reminder does not install PStack, load skills automatically, or change role or authority.
It is outside the 8192-byte standing-context budget and counts toward the 24 KiB combined injection limit.
Oversized context fails rather than being truncated.
The reminder is configured context, not proof of native loading or model receipt.

## Pilot and installation authority

Checkpoint support is opt-in per native orchestrator session and is off for existing lanes until enrolled.
Installing the code does not enroll a session.
Orchestrators may self-enroll without separate human approval.
Enrollment never replaces standing text, changes product authority or releases a human hold.
Tell the user in advance if activation will need their native hook-trust interaction, and wait for a safe normal restart boundary that preserves existing jobs and services.
Existing enrolled sessions keep their notes and settings unless deliberately changed through the existing controls.

## Standard installation and trust

Install the complete adapter once per harness:

```sh
python3 apps/pattern-buffer/pattern_buffer.py install --harness codex
python3 apps/pattern-buffer/pattern_buffer.py install --harness claude
```

The standard installation includes stable `SessionStart`, `PreCompact`, `PostToolUse` and `UserPromptSubmit` definitions at the user level.
Their interpreter, runtime path and command do not contain a session ID, worktree path or checkpoint path; the hook resolves identity from its event input.
Pressure hooks remain silent for sessions that have not enrolled, and installation does not create a checkpoint or assign a role.
Claude's status-line wrapper captures session-specific usage while preserving the existing output and display options; uninstall restores the prior status line when the owned wrapper is still present.
Repeated installation consolidates owned definitions without changing unrelated hooks.
The existing `--checkpoints` spelling remains compatible; `--no-checkpoints` explicitly installs only ordinary recovery and removes owned pressure hooks and Claude telemetry.
The same defaults and opt-out apply to `hook-config`.

Codex still needs its hook feature enabled, for example `features.hooks = true` in its user configuration or `--enable hooks` at launch.
Trust is separate from configuration: inspect `/hooks` and review the stable definitions once in the normal native controls.
New sessions can load those unchanged trusted definitions without creating a new hook for each checkpoint enrollment.
A changed definition can require another review; the tool never writes native trust records or bypasses the trust gate.
Claude loads user settings across projects, with its separate workspace trust rules still applying.
See [Codex hooks](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks) and [Claude workspace trust](https://code.claude.com/docs/en/hooks#workspace-trust).

Use the exact installed `pattern_buffer_claude.py` or `pattern_buffer_codex.py` filename outside the source checkout.
Global `--cwd` goes before the subcommand.
Tell the user before any initial reload, restart or trust interaction; no process is automatically stopped or restarted.
Installation is not proof that the current session loaded or received a hook.

## Self-service enrollment

Run the briefing from the intended worktree before writing or adopting a note:

```sh
python3 /absolute/path/pattern_buffer_codex.py checkpoint-enroll --harness codex
```

Use `pattern_buffer_claude.py` and `--harness claude` for Claude.
Source-checkout usage is `python3 apps/pattern-buffer/pattern_buffer.py`.
The worktree must already be registered with ordinary Pattern Buffer and this session must resolve to an orchestrator; implementer, task-bound and disabled worktrees are rejected rather than reassigned.
Both harnesses require a matching standard installation with all four hook events before note adoption.
If the preflight reports missing, partial or stale hooks, use the current package's `install --harness HARNESS` first.
The command detects the worktree and uses `CODEX_THREAD_ID` for Codex when available.
For Claude, pass the actual native `--session SESSION_ID` unless the harness provides `CLAUDE_SESSION_ID`; an explicit ID conflicting with the inherited ID is rejected.
The first call prints the take-stock briefing, note template and detected threshold without changing files, hooks or registrations.

OpenCode sessions use the same private checkpoint state and atomic publication rules through the opt-in context plugin.
Assign the exact native `ses_` session as an orchestrator before enrollment because OpenCode sessions do not inherit a worktree default role.
The OpenCode plugin observes successful tool completion for draft publication and native compaction lifecycle events for bounded telemetry.
Its event stream has no replay guarantee, so observed completion counts are explicitly uncertain when the plugin was inactive or disconnected.
OpenCode percentage-based `--compact-at`, reminder thresholds and lead points remain disabled.
After exact same-session usage and the active provider, usable window and merged compaction settings are independently verified, a private descriptor can enable a conservative token-anchor reminder through the ordinary context callback.
The `session.compaction` context callback runs after the current v2.2.5 automatic compaction decision and is not used to warn on that same attempt.
An ordinary context callback can warn before a later automatic-compaction decision when the request proceeds below the configured ceiling, but a large unseen context tail can still compact without warning.
The reminder uses a last-measured lower-bound usage anchor and does not claim exact current occupancy or guaranteed headroom.
See the [OpenCode plugin guide](opencode.md) for exact enrollment commands, behavior and limits.

The briefing asks you to pause and re-read the mission, assigned roadmap, latest user direction and relevant guidance.
Reconcile current implementation state, worker results, dependencies, evidence and human holds.
Consider whether you have the full context and appropriate resources, including useful parallel work in temporary worktrees while respecting dependencies, ownership and shared limits.
Resolve gaps within existing authority and record remaining uncertainty, then write a concise, accurate note to your future self.
This is a judgment step, not a request to launch more agents or invent extra work.

Reuse a suitable existing private recovery entrypoint instead of creating a competing note.
Keep detailed evidence in its existing ledger and link it from the concise note.
Follow the six-section template below, then enroll with that reviewed file:

```sh
python3 /absolute/path/pattern_buffer_codex.py checkpoint-enroll --harness codex --file /absolute/private/working-checkpoint.md
```

Codex detects its effective compaction percentage from a read-only same-session, same-process runtime record when available and the record uses total-token scope with a known usable window.
Otherwise supply `--compact-at CONFIRMED_PERCENT`; the tool does not guess from the selected model or change native token limits.
Claude uses its active environment override or the documented 60% fallback, with `--compact-at` available for an explicit fallback.
`--lead-points` defaults to five.
The input must be a durable private file; temporary, tracked, unignored and other-session-owned paths fail explicitly.

Enrollment validates and registers the note, creates the ignored editable draft and publishes the reviewed note using the existing stable installed hooks.
It does not install or rewrite hook definitions, create session-specific runtime copies, change native trust records or enroll another session.
`--settings PATH` selects the already installed settings to inspect; `--runtime-dir PATH` checks the selected installed runtime location instead of creating a new one.
Repeated enrollment reports existing note and configuration without replacing them.
Use `checkpoint-save` and `checkpoint-config` for deliberate updates.

Prepared and already-enrolled results begin with `edit_this_file`, the ignored draft that the agent should maintain, followed by `published_copy` and `draft_body_limit_bytes`.
Edit the draft as a replacement current snapshot; the published copy and its generated header are maintained by the tool.
The published copy begins with YAML front matter containing `kind: working-checkpoint`, `harness`, the exact native `session` and a UTC `published_at` timestamp.
The header includes a tool ownership comment and quoted scalar values; it requires no YAML dependency or agent-maintained timestamps.
Publication time means the content was published then, not that its claims were independently verified or remain current.
The entire published note, including the generated header, must fit the 8192-byte UTF-8 limit.
`draft_body_limit_bytes` reserves the header space up front; invalid or oversized automatic edits keep the last valid published note and produce one advisory per edited version.
Existing headerless notes remain readable without a forced rewrite and gain the header at their next successful ordinary publication or explicit save.
Saving a generated published copy explicitly strips its old tool header and regenerates it for the current owner, without stacking headers.
Keep editing the body in the ignored draft; never hand-edit generated publication metadata.

The result distinguishes `prepared`, `hooks_loaded: unverified` and `recovery_observed: false`.
If the standard definitions are already loaded and trusted, enrollment adds no restart requirement or new trust identity.
The reported `restart_required` flags legacy native copies that would duplicate the stable source; it does not certify that initial installation or native trust completed.
Confirm actual model-visible reception and ordinary automatic publication during normal work.

### Migrate older session-specific hooks

Before enabling the standard source across old pilots, coordinate their existing process-local copies so the same event is not supplied twice.
An already enrolled Codex orchestrator can explicitly prepare removal of those owned native copies without changing its note, thresholds or enrollment:

```sh
python3 /absolute/path/pattern_buffer_codex.py checkpoint-enroll --harness codex --prepare-restart
```

For an identifiable same-session native resume, Python 3.11 or newer prepares a private launcher that retains unrelated cmux handlers, current model/effort and permissions while removing owned legacy copies.
The launcher refuses to run until the original native process exits, and it never stops a process itself.
At a safe boundary agreed with the user, run `/quit`, execute the returned launcher and review the stable definitions in `/hooks` if required.
The ordinary session identity and all job/service holds remain unchanged.
If process inspection is unavailable or the native launch form is unsupported, the tool preserves the original process and reports the gap instead of inventing a launcher.
Restricted process inspection also leaves the initial briefing usable, with native identity details or Codex thresholds marked unknown where necessary; supply an independently confirmed `--compact-at` instead of guessing.
Do not widen permissions merely to complete enrollment.
Regenerate restart preparation if native model, launcher arguments or hook configuration changes before the agreed restart.
Actual pressure reminders and natural compaction recovery remain separate observations to collect during normal work.

## Location, ownership and Git hygiene

The reviewed recovery note lives at `<absolute-worktree-git-dir>/context-injector/checkpoints/<session-key>/checkpoint.md`.
The editable draft lives at `<worktree>/.context-injector/checkpoints/<session-key>/draft.md`; editing this designated draft is the request to publish a reviewed update.
Use `git rev-parse --absolute-git-dir` to identify the worktree's private Git directory; a linked worktree's `.git` is usually a pointer file, not this directory.
The key includes the harness and exact native session identity, keeping sessions and worktrees separate.
`checkpoint-status` prints the actual absolute file path and freshness metadata.

The reviewed note and metadata are private Git administrative state, outside the tracked working tree.
They need no `.gitignore` entry.
The editable draft is inside the worktree so normal file-writing tools can update it without writing protected Git metadata.
Enrollment ensures the narrow draft subtree is ignored through local Git exclusion when the repository does not already ignore it, and rejects tracked drafts.
Neither the draft nor the reviewed note belongs in a commit, raw uploaded evidence or PR.
This is local working state, not a request to change the repository-wide ignore policy.
It survives compaction and process restarts while the Git worktree registration exists; it is not a backup against disk loss or deliberate worktree removal.
Before removing a worktree, preserve any needed handoff in durable private storage and explicitly register/adopt it in the successor's session.
A successor does not silently inherit another session's checkpoint.

An existing note can be registered with `checkpoint-init --path /absolute/private/checkpoint.md`.
The path must be durable, a regular non-symlink file if present, and unambiguously owned.
Tracked or nonignored paths inside any Git worktree and temporary-directory paths are rejected.
Ownership and previous-version companion files are private too; use an ignored directory rather than ignoring only one Markdown filename.
Do not create a second competing checkpoint if a lane already maintains a suitable private file.

## Start and save a checkpoint

First initialize Pattern Buffer in the disposable or approved owner worktree, then register the exact native orchestrator identity.
A session with an implementer role cannot use this orchestrator feature.

```sh
python3 apps/pattern-buffer/pattern_buffer.py --cwd /absolute/worktree init --default-role orchestrator
python3 apps/pattern-buffer/pattern_buffer.py --cwd /absolute/worktree checkpoint-init --harness claude --session ACTUAL_SESSION_ID --compact-at 60
python3 apps/pattern-buffer/pattern_buffer.py --cwd /absolute/worktree checkpoint-status --harness claude --session ACTUAL_SESSION_ID
```

Edit the exact `draft_path` printed by `checkpoint-init` or `checkpoint-status` using your normal file-writing tool.
Keep these nonempty sections, using `None.` for genuinely empty sections.
Keep roughly 400-700 words and at most 8 KiB; oversized or malformed saves fail explicitly without replacing the last valid note.
Leave room for later operational changes; be brief without sacrificing accurate next actions, decisions, uncertainties, evidence or human holds.
Remove stale repetition and link supporting detail rather than dropping facts needed for safe recovery.
Do not include secrets, copied logs or entire rules documents.

```markdown
# Working checkpoint

## Current state
Current implementation phase, worktree/revision and issue or PR.

## Next action
The next action, why it matters and any prerequisite.

## Decisions and uncertainty
Important decisions, rejected approaches with evidence, and unresolved hypotheses.

## Active work
Existing worker/process/CPU ticket identities and result locations, or None.

## Evidence
Verification command, exact candidate and inputs, result and evidence pointer, or None.

## Holds and guidance
Human holds, pending decisions and relevant authoritative rules/architecture links.
```

After an eligible tool writes the designated draft, the trusted `PostToolUse` hook validates and atomically publishes it to the reviewed recovery note.
It records the draft write time separately from publication time, preserves the last valid note on errors and gives one advisory for an unchanged invalid draft rather than repeating it after every tool.
No model summarizer or extra agent command is needed for this ordinary update path.
The existing publication receipt shows UTF-8 bytes used and remaining, without adding a recurring warning or required check.
`checkpoint-status`, `checkpoint-read` and explicit save results expose `note_bytes`, `note_limit_bytes` and `note_remaining_bytes` for the recovery note, not an unpublished draft.
If the recovery note is missing or unreadable, used and remaining bytes are unknown (`null`), not a claim that the full budget is available.
For an explicit save or a headless workflow without that hook, use:

```sh
python3 apps/pattern-buffer/pattern_buffer.py --cwd /absolute/worktree checkpoint-save --harness claude --session ACTUAL_SESSION_ID --file /absolute/private/draft.md
python3 apps/pattern-buffer/pattern_buffer.py --cwd /absolute/worktree checkpoint-read --harness claude --session ACTUAL_SESSION_ID
```

Both publishing paths validate ownership/structure/size, retain the previous valid version and atomically replace the current note.
Use the designated editable draft or another private input file rather than overwriting the reviewed recovery note before validation.
An explicit save synchronizes the designated draft so an older draft cannot overwrite the new note at a later hook.
Only publishing an explicitly edited/reviewed draft or an explicit save updates the review timestamp; untouched drafts and pre-compaction captures do not.
A saved timestamp or acknowledgment is not proof that the note is semantically current or was read by the model.
Working-state updates within already-authorized scope do not need new approval each time.
The note grants no mission, standing-policy, merge, deployment or job-control authority.

## Reminder limits and experiments

The reminder threshold is the configured auto-compaction percentage minus `lead-points`, which defaults to 5.
With the operator fallback at 60%, Claude warns at 55%; changing the active Claude override to 65% moves the reminder to 60%.
These are reminder-configuration percentages, not a guarantee of the exact native automatic-compaction trigger or available writing margin.
The configured token window can differ from actual boundary usage; idle, manual or early compaction can occur before a percentage reminder.
Runtime Claude environment/telemetry takes precedence over enrollment fallback, and status identifies its source.
A status-line display by itself does not prompt the model; an eligible conversational hook delivers the reminder.
Pressure and idle reminders ask: "Refresh this as a current snapshot. Replace outdated state rather than append. Remove completed work, superseded decisions and obsolete commands. Keep the next action and active work, current decisions and relevant rejected approaches, exact commands still needed for the next action or recovery, active job IDs, evidence pointers, uncertainties and human holds. Link useful history instead of repeating it."
Only an eligible verified pressure warning begins with "Take action immediately, compaction imminent." Enrollment, recovery, publication and ordinary checkpoint maintenance output stay neutral.
The urgency line signals configured context pressure, not proof of exact native compaction timing or coverage of idle compaction.
For an eligible pressure episode, the hook makes one bounded attention attempt through the installed Hailing Frequencies launcher when the canonical session UUID and current CMUX surface are available; Hailing Frequencies must already enroll the session and applies its own current binding and human-input guards.
The attention attempt has a three-second timeout and never replaces the hook's urgent additionalContext warning, including when delivery is held, unavailable, timed out or uncertain.
Checkpoint status shows the latest event key, outcome, attempted flag and bounded detail; "submitted" means a guarded submit was attempted, not that the recipient received, read or acted on the message.
There is no timer or automatic retry, and attention delivery does not cover idle compaction when no eligible conversational hook runs.

Save a new Claude auto-compaction setting through one command:

```sh
python3 apps/pattern-buffer/pattern_buffer.py compaction-settings --compact-at 60
```

Use `--settings /absolute/settings.json` for isolated tests or a deliberately selected settings file.
The command backs up the file and changes only `env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, preserving unrelated settings.
It does not restart or change a running session; verify its effective environment after the normal restart/resume boundary.
Do not use this command against another lane's settings without authorization.

Codex's effective compaction limit can differ from its reported usable context window and selected-model defaults.
Confirm the effective threshold on the same percentage scale before enrollment rather than guessing from the model name.
If compaction begins at 95% of that scale, use `--compact-at 95` for a 90% reminder; if it begins at 90%, use `--compact-at 90` for an 85% reminder.
Codex enrollment uses a confirmed same-session runtime percentage when available; otherwise supply an explicit confirmed percentage.
Enrollment does not alter Codex's compaction configuration.

```sh
python3 apps/pattern-buffer/pattern_buffer.py --cwd /absolute/worktree checkpoint-init --harness codex --session ACTUAL_SESSION_ID --compact-at 95
python3 apps/pattern-buffer/pattern_buffer.py --cwd /absolute/worktree checkpoint-config --harness codex --session ACTUAL_SESSION_ID --compact-at 90
python3 apps/pattern-buffer/pattern_buffer.py --cwd /absolute/worktree checkpoint-config --harness codex --session ACTUAL_SESSION_ID --lead-points 5 --reminders off
```

`--reminders off` supports a later comparison while retaining checkpoint recovery.
`--enabled off` disables the session's checkpoint feature while preserving its note; ordinary Pattern Buffer enable/disable remains independent.
These controls do not change the two-hour feedback schedule.

Within an observed compaction generation, a warning is emitted once per high-usage episode.
Fresh session-checked usage at least five percentage points below the active reminder threshold re-arms the warning, so a later rise to the threshold can prompt another checkpoint update even when the compaction count is uncertain.
An observed generation change also allows one warning at high usage.
Usage between the re-arm point and the warning threshold does not re-arm the warning, which avoids repeats from small percentage fluctuations.
Codex re-arming uses a fresh token-count record in the same verified session transcript; Claude re-arming uses its session-specific status-line cache while that cache is fresh.
Missing, stale, malformed or unrecognized telemetry and wrong-session transcripts stay unknown and neither invent a percentage nor re-arm the warning.
A large tool result can jump past a threshold before another safe opportunity; maintain the note at meaningful work transitions too.
No background agent, model summarizer, artificial token filling or forced compaction is required.

## Recovery, age and counters

Before compaction, the hook records the saved note's capture metadata without rewriting its body or pretending the agent reviewed it.
After recovery, Pattern Buffer provides its path, identity, review timestamp, observed generation information and relevant warnings.
When boundary provenance is incomplete or pending, model-facing output says "Compaction count not yet confirmed. This does not mean no compaction occurred."
Numeric observed counters remain available in structured status for diagnostics; uncertain counts are not presented as confirmed zero or a confirmed save-to-recovery gap.
Read the note before continuing, then reconcile its next action with the actual checkout, tracker, jobs and latest user direction.
Your native file reader can read the Markdown directly; `checkpoint-read` additionally validates the note's hash and reports metadata.
If the harness prevents access to private Git metadata or checkpoint writes, report that concrete permission failure rather than widening permissions or claiming the save succeeded.
Reattach to existing job/ticket identities before considering replacements.
Historical verification remains evidence for its recorded candidate; a changed revision requires checking whether that evidence still applies.

Compaction generations count actual observed transcript boundaries rather than hook calls.
Duplicate delivery of the same retained boundary must not increment the counter twice.
Some hosts run the recovery hook before persisting their boundary; recovery labels that boundary pending rather than pretending the count already includes it.
When a later tool hook observes the boundary, it supplies one updated freshness notice.
Without new transcript provenance, duplicate recovery delivery and a not-yet-persisted boundary remain ambiguous.
A bounded unread gap, replaced/truncated transcript or absent provenance is reported as uncertainty rather than a guaranteed lifetime total.
A compact-boundary JSONL record larger than the 256 KiB scan window cannot be parsed as a complete record and may leave the generation count uncertain; this uncertainty no longer suppresses pressure reminders.
Elapsed time, generations and revision differences are freshness signals, not automatic proof that a note is wrong.
An old unresolved hold remains binding until its owner changes it.

A malformed draft preserves the previous reviewed note and tells the agent what to correct.
A missing, corrupted or mismatched checkpoint produces an advisory recovery warning; it never blocks native compaction or suppresses standing context/task replay.
Reconstruct missing state from live evidence without inventing completed proof or replacing still-running jobs.
Do not automatically touch timestamps, silently truncate notes, or treat file existence as evidence of successful recovery.

## Capability and acceptance limits

Codex pressure sampling uses bounded, session-checked local token-usage records; that format is version-sensitive.
Claude pressure sampling uses its session-specific status-line usage cache.
Actual model-visible reminders and post-compaction reading must be measured separately from CLI fixtures and emitted hook output.
Manual `/compact` verifies recovery without filling a window; it does not prove automatic pressure timing.
Child implementers, Muse and broad rollout acceptance remain separate from orchestrator self-enrollment.

Official surfaces: [Codex hooks](https://learn.chatgpt.com/docs/hooks), [Codex limits](https://learn.chatgpt.com/docs/config-file/config-reference), [Claude hooks](https://code.claude.com/docs/en/hooks) and [Claude status line](https://code.claude.com/docs/en/statusline).
