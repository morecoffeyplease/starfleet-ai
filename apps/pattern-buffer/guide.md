# Pattern Buffer guide

Pattern Buffer restores approved worktree context to Claude Code and Codex at startup, resume and recovery, with an opt-in OpenCode context plugin pilot.
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
The installed launcher pins the Python executable used for installation, so a different `python3` on `PATH` cannot select an older runtime.
Reinstall after replacing or moving that interpreter.
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
For enabled events, an owned-only group is refreshed to canonical group settings in place, while a mixed group updates only the owned handler and preserves shared filters and metadata.
Duplicate owned handlers retain the existing cleanup behavior.
Preserving configured positions does not guarantee that a harness will trust or load the hook or deliver its output.

```sh
python3 apps/pattern-buffer/pattern_buffer.py install --harness codex
python3 apps/pattern-buffer/pattern_buffer.py install --harness claude
```

For the OpenCode v2.2.5 context hook pilot, use a dedicated OpenCode settings copy and pass it explicitly.
The adapter requires an exact session ID and does not infer roles from worktree defaults or OpenCode parent IDs.
Read the [OpenCode adapter guide](docs/opencode.md) for isolated setup, two-phase task binding, limits and native verification requirements.
If install refuses an unowned or damaged runtime entrypoint, preserve the existing runtime.
From a source checkout, use the same settings path and install into a new empty runtime:

```sh
python3 apps/pattern-buffer/pattern_buffer.py install \
  --harness codex \
  --settings /absolute/path/to/codex-settings.json \
  --runtime-dir /absolute/path/to/new-empty-runtime
```

Use the matching harness name and settings file for Claude.
The generated recovery command preserves whether checkpoint processing was enabled for that settings file.
This updates only the selected harness's owned hook command and leaves worktree enrollment and checkpoint files in place.
The existing PATH launcher remains unchanged; use the new runtime's `pattern-buffer` launcher by explicit path for commands against the recovered runtime.
Review the harness's normal reload and trust controls after the install; installation does not confirm delivery.
The installer keeps a `PostToolUse` hook for required-read completion when checkpoint processing is disabled.
In that mode, the hook skips checkpoint processing and handles only required-read completion.

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
Dispatch supports implementers only.
When the parent uses exact-session standing context, keep that enrollment and prepare a dedicated directly initialized owner worktree containing a reviewed copy of the selected standing source.
Use a fresh unregistered child if an enabled existing child points to another owner; preserve old tasks and do not bypass disabled sources or human holds.
See [the owner preparation and child reuse guidance](https://github.com/morecoffeyplease/starfleet-ai/blob/main/apps/pattern-buffer/docs/implementer-dispatch.md#parents-with-exact-session-context) for the supported route.
For repository-specific instructions, create and review an external JSON profile based on [the example](examples/repository-profile.json), then select it with `--repo-profile-file /absolute/path/to/profile.json`.
`init` preserves an existing context file and never installs hooks.

Check both role budgets and inspect the actual rendered context before relying on the hook:

```sh
pattern-buffer --cwd /absolute/worktree validate --harness codex --session ACTUAL_SESSION_ID
pattern-buffer --cwd /absolute/worktree status --harness codex
pattern-buffer --cwd /absolute/worktree preview --harness codex --session ACTUAL_SESSION_ID
```

Use `--harness claude` for Claude and a real session identity when known.
`validate` checks both role sections, supported local references, and the 8192-byte authoring budget.
It exits nonzero on malformed context, missing local targets or headings, and oversized context.
It reports header bytes, usable content bytes and remaining UTF-8 bytes from the exact rendered header.
Use the exact session ID when available so the report includes the selected session file path.
Without `--session`, status labels its placeholder header budget and does not establish exact native-session binding.
`status`, `session-enroll`, `enrollment-status` and `handoff-check` use the same calculation.
Status is read-only and continues to report local budget and delivery observations without claiming that the harness received context.
Exact-session enrollments and dispatched work keep their blocking integrity checks for missing, inactive, unreviewed, malformed or oversized required context.
An ordinary directly managed worktree may emit its complete role view above 8192 bytes with a warning when the total Pattern Buffer output, including runtime, task and checkpoint addenda, is at most 9216 bytes and remains under the 24 KiB combined limit.
If that total exceeds 9216 bytes, or the ordinary source cannot be read or parsed or the combined limit is exceeded, SessionStart emits a bounded notice with the actual source path and asks the agent to read the full file and notify its owner.
The notice preserves unresolved scope and holds, states that it is not full context, and does not expand authorization.
Preview shows the ordinary-worktree warning and fallback behavior but omits event-specific checkpoint addenda, so the actual SessionStart total can differ.
The hook uses the harness-reported working directory, not the directory from which installation was run.
Unregistered worktrees emit no context.

## Required reads

The worktree owner may register a reviewed manifest that names each required document, its absolute or home-relative path, and its applicable roles.
An entry applies to `orchestrator`, `implementer`, or both roles.
For example:

```json
{
  "schema_version": 1,
  "entries": [
    {"name": "engineering-guide", "path": "~/guides/engineering.md", "roles": ["orchestrator", "implementer"]}
  ]
}
```

The owner registers or clears the manifest from an interactive terminal outside every harness session.
The owner types the worktree directory name to confirm either change.

```sh
pattern-buffer --cwd /absolute/worktree required-reads set --file /absolute/reviewed/required-reads.json
pattern-buffer --cwd /absolute/worktree required-reads status --harness codex --session ACTUAL_SESSION_ID --json
pattern-buffer --cwd /absolute/worktree required-reads clear
```

`required-reads status` emits one JSON document by default, and `--json` is accepted for callers that make the format explicit.
The stable report has `registered`, `list_sha256`, `generation`, `harness`, `session_id`, `role`, and `entries` fields, with entry name, resolved path, roles, state, SHA-256, byte count and receipt parts.
Entries for other roles use `not_applicable`, and a worktree with no registered list returns `{"registered": false}` and exits successfully.
It can report an absent list in a plain Git worktree without initializing Pattern Buffer.
Policy Guard consumes this read-only report when it validates a published guidance attestation; see [Published guidance attestations](../starfleet-policy-guard/guide.md#published-guidance-attestations).

Agents may draft a manifest and run read-only status or doctor checks, but they cannot register or clear the list.
Agents run under the same operating-system account as the owner, so the terminal check is a cooperative safeguard and does not prove who typed the confirmation.
If the manifest or a required file is missing or changed, stop and notify the owner instead of editing or replacing the registered requirements.
The owner can restore the reviewed file or review and register a corrected manifest after confirming every entry.

Each session's `PreToolUse` gate checks the registered list, the resolved role, required-file integrity, and receipts for the current context generation.
A new startup, compaction, clear, or fork starts a fresh generation, while a resume keeps the current generation.
The SessionStart notice names pending reads and the next `read-required` command.
An already-running main session with no current generation starts generation 1 on its first gated tool call after the owner registers a list.
That call still follows the read gate and earns no receipt.
Pending and read-binding denials include `pattern-buffer guide --print` for recovery steps.
Use the command to print a whole file or its next numbered part:

```sh
pattern-buffer --cwd /absolute/worktree read-required --harness codex --session ACTUAL_SESSION_ID --next
pattern-buffer --cwd /absolute/worktree read-required --harness codex --session ACTUAL_SESSION_ID --name engineering-guide
```

`read-required` prints a canonical frame and performs no state, lock or telemetry writes.
The frame includes the entry name, part number, document byte count and file digest.
`PreToolUse` stores a bounded snapshot for the exact native session and tool call, including the current generation, list, entry, part and file identity.
`PostToolUse` records the existing receipt only when its complete stdout matches that frame and the generation, list and file still match the snapshot.
A standalone print, a missing or truncated response, a changed snapshot, or an unpaired tool call does not earn a receipt.
Receipt state contains metadata only and no document contents.
Codex supplies stdout as a string without an exit-status field, so a complete frame does not authenticate process exit.
Claude supplies stdout with stderr and interruption or output flags, which the hook checks before accepting a frame.
The Claude hook accepts removal of the final footer newline and compares every other frame byte, including the document bytes and footer digest.
The four recorded native default-budget outputs included complete frames, including an 8192-byte document part, without an explicit output allowance.
The observations came from Claude 2.1.322 and Codex 0.167.
The largest observed Codex tool result was 8490 bytes, which is a sample and not a guaranteed output ceiling.
These observations do not prove general model reception, comprehension or compliance.
On Codex, receipts reset at the next user turn after a compaction, not at the compaction itself, so a compaction in the middle of a continuing turn is not covered.

`required-reads status`, `status`, `doctor`, and `guide --print` remain available as read-only diagnostics when the manifest or a required file is invalid.
Read-only status, doctor and handoff checks do not create locks or change required-read state.
Doctor reports the registered digest, applicable files, pending session reads, the next command, and whether the selected settings file contains the Pattern Buffer `PreToolUse` hook.
Hook presence in settings does not prove that the harness loaded or trusted the hook, honored a denial, or delivered its output.
When no required-read manifest is registered, the gate stays inert and adds no hook output.

An owner can temporarily open the gate for one exact target session with a visible reason and an expiry between one and sixty minutes:

```sh
pattern-buffer --cwd /absolute/worktree required-reads override \
  --harness codex --session ACTUAL_SESSION_ID \
  --reason "Urgent recovery" --minutes 10
```

The owner runs the override from an interactive terminal outside every harness session and types the target session ID to confirm it.
The override belongs only to that session's current generation and ends when it expires or the generation changes.
An override does not repair a changed manifest or missing required file.

### Compose rules and mission

Use `compose` to combine reviewed project rules with a per-worktree mission.
It writes an ordinary context file and does not change runtime composition.

```sh
pattern-buffer --cwd /absolute/worktree compose \
  --rules /absolute/reviewed-rules.md \
  --mission /absolute/worktree/mission.md \
  --output /absolute/private/composed-context.md \
  --harness codex --session SESSION_UUID
```

The rules file must contain `# shared`, `# implementer`, and `# orchestrator` sections.
It may end with `# confirmation`.
The mission file must contain `# shared` and may include either role section.
Mission text comes before rules text in each section.
The confirmation section comes only from the rules file.
The command reports the input and output SHA-256 digests, paragraph byte counts, and exact rendered budgets for both roles.
It reuses the 8192-byte renderer and requires at least 1024 bytes of remaining headroom per role by default.
Pass `--harness` and `--session` to calculate a future session-enrollment budget against the existing destination path.
That calculation is a preview until you use the normal reviewed enrollment command.
Without `--session`, `validation` is a placeholder and the command reports no future-recipient budget.
Use `--check` to compare the composed bytes without writing.
Use `--replace` to replace an existing output whose bytes differ.
Compose to a private path, then use `session-enroll --reviewed-update --evidence` for registered exact-session context.

The offline reference checker resolves relative Markdown targets from the worktree root.
It checks simple inline Markdown targets, Markdown heading anchors, and standalone backticked absolute or home paths.
It reports HTTP and HTTPS references without making network requests.
Missing local targets and headings fail `compose` and `validate`.
`status`, `session-enroll`, `enrollment-status`, and `handoff-check` report the same findings as warnings.
Reference-style links, raw HTML links, and command-style inline code are not parsed as paths.
Review those warnings manually when a document uses unsupported or ambiguous syntax.

Use `handoff-check --harness HARNESS --session SESSION_UUID` for a read-only local successor check.
It reports standing context, optional checkpoint ownership and runtime compatibility separately from native binding and actual delivery.
Unavailable native or delivery evidence is marked unknown and does not add a readiness requirement.
For an exact Claude checkpoint owner it also reports the existing idle-warning minutes and timer state, if that checkpoint exists.
If an existing warning is disabled, its next action shows the supported `checkpoint-config` command without changing state.
New Claude checkpoints default to 50 minutes, while existing checkpoints remain unchanged until their owner enables a warning.
An armed timer is configuration evidence, not proof that its wake was received.

## Shared recovery reminder

Pattern Buffer appends the same short runtime PStack reminder to every rendered context, for every role and whether or not a checkpoint is enrolled.
It asks agents to use the installed PStack runtime and Poteto Mode, then after compaction read the sections relevant to current work in full, not the entire PStack library.
The default shared guidance also asks agents to restate the accepted outcome, next deliverable, holds, and what they will not do in four lines in their own chat.
The holds line names unavailable required reads and preserves holds.
The runtime reminder does not install PStack, change roles or authority, or rewrite custom context files.
It stays outside the 8192-byte standing-context budget and counts toward the 24 KiB combined injection limit.
For an ordinary worktree, the full role view can exceed 8192 bytes only when the complete output with its warning and any runtime, task or checkpoint addenda stays at or below 9216 bytes.
The 24 KiB limit remains a hard combined limit.
Oversized output produces a bounded full-file-read and owner-notification notice for an ordinary worktree, while exact-session and dispatched integrity failures still stop.
Hook output remains configuration evidence, not proof that the harness loaded or delivered the reminder.

## Confirmation and review guidance

The default context template ends with a `# confirmation` section.
Pattern Buffer appends that section after the selected role for implementers, orchestrators, and unassigned sessions.
The section counts toward the 8192-byte standing-context budget.
The footer asks agents to repeat the shared four-line restatement in their own chat.
Legacy custom contexts without `# confirmation` receive the same restatement after the runtime PStack reminder.
Custom context files keep their owner-managed confirmation text.
`validate` and context adoption continue to enforce the 8192-byte authoring budget for every role.
The ordinary-worktree SessionStart warning policy is a runtime recovery exception and does not change that authoring check.
Use preview to inspect the runtime warning or fallback notice; preview does not include event-specific checkpoint addenda.

The default context template includes shared review judgment and orchestrator repair-round guidance.
Before starting or resuming a review-and-fix cycle, read the [Prime Directive (Policy Guard) review and fix cycles guide](https://github.com/morecoffeyplease/starfleet-ai/blob/main/apps/starfleet-policy-guard/guide.md#review-and-fix-cycles) in full.
Revisit the relevant rule before each repair decision.
Prime Directive (Policy Guard) is the same delivery tool named Policy Guard in the CLI.
The orchestrator template also links its optional published guidance attestation flow.
That notice uses Pattern Buffer's existing SessionStart standing-context injection and does not detect issues, accepted batches or their status.
Read the [published guidance attestation guide](../starfleet-policy-guard/guide.md#published-guidance-attestations) for the owner-enabled packet, rendering and exact-comment checks.
The delivery seat does not infer repair-round decisions from those records or enforce unrelated work outside covered ready and merge paths.

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

After an observed compaction, an enrolled Claude or Codex session must receive its latest published checkpoint before ordinary tool calls resume.
Use the exact `checkpoint-read --part N` command supplied by the recovery hook, then check `checkpoint-status` for `recovery.required`, received parts and the next command.
Only complete matching output paired with the native `PreToolUse` and `PostToolUse` events for every part of one published version clears the prerequisite; a standalone print or a native file-reader call does not create this receipt.
A later checkpoint save in the same recovery generation does not reopen a completed read, while the next observed compaction requires a new read even when the note bytes are unchanged.
The static required-read gate remains independent, so its `read-required` operation can proceed while checkpoint recovery is pending, and framed checkpoint reads can proceed while a static required read is pending.
If the publication is missing or unreadable, keep recovery unresolved, inspect only that session's checkpoint, previous copy and owned draft, and edit only the owned draft until a valid publication can be read.
Unknown history stays explicit, and older decisions or human holds remain authoritative unless their owner changes them.
This enforcement uses the existing standard Claude and Codex `PreToolUse` and `PostToolUse` hooks; the OpenCode context plugin remains outside this gate.
Codex recovery still depends on an observed compaction hook signal, so a compaction during a continuing turn without that signal is not covered.

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
