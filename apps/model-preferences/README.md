# Model Preferences

Model Preferences stores exact provider model choices for future orchestrator, implementer, researcher and reviewer launches.
It has no background service and does not change existing sessions, jobs, permissions or native harness trust.

## Initialize without presets

The default init creates an empty model registry and profile list.
No provider model is activated or substituted automatically.

    model-preferences init
    model-preferences status

To inspect the bundled sample families and profiles, initialize with the explicit examples option.
Examples are inactive configuration, and live model availability is not checked.

    model-preferences init --examples
    model-preferences list
    model-preferences validate

The example set includes Codex Luna as an opt-in choice and exact example slugs for other supported harnesses.
Treat each slug as an example until its current availability has been confirmed in the intended provider CLI.
The application uses the saved slug exactly and never queries a catalog or substitutes another model during dispatch.

## Configure exact models and profiles

Use model-set to add a family or update its exact harness, provider slug, supported effort values and source statement.

    model-preferences model-set local-codex --harness codex --slug EXACT_PROVIDER_SLUG --efforts low medium high xhigh --source "confirmed with the provider CLI"

Create a profile JSON whose role choices reference configured model family keys and supported effort values.
Each profile must define orchestrator, implementer, researcher, standard_review and advanced_review.
Review groups contain one reviewer or two independently configured reviewers.
The `claude-codex-light` profile also defines `standard_review_policy` with the fixed values `{"initial_slots": [1, 2], "later_slots": [1]}`.
Use profile-put to save the reviewed profile.

    model-preferences profile-put team-default --file /absolute/path/to/profile.json
    model-preferences show team-default
    model-preferences activate team-default --orchestrators future-only

When an activation or model update changes active launch choices, select future-only, ask or handoff to record how existing orchestrators should be notified.
The command never changes running sessions or starts a handoff by itself.
Use startup --expect-profile NAME to compare a session's stated intent with the saved active selection without activating it.

## Human approval authority

No human approval authority is assigned by default.
Configure its label explicitly before recording advanced-review approval or starting a handoff.
The label is included in the active selection so changing it makes scope-bound review plans stale.

    model-preferences authority-set "project owner" --orchestrators future-only
    model-preferences approve-review PLAN_ID --authorization "approval reference or recorded decision"

Set the authority before activating a profile to avoid a notification choice; when it is changed after activation, include `--orchestrators future-only`, `ask` or `handoff`.
Advanced review still requires a separate explicit authorization record for the exact scope and profile.
The authorization text is a local attestation; the tool does not authenticate a person's identity or accept a peer relay as proof.
Standard review can be selected explicitly when the configured authority declines advanced review.
The configured authority label does not grant tool access or change native approval settings.

## Exact session and handoff ownership

Enroll orchestrators by stable native workspace, surface and session identity.
The exact current owner must acknowledge adoption from its own session before the app reports adoption.
Queued notifications are distinct from delivery receipts, owner acknowledgments and model comprehension.
Handoff preserves the outgoing session until ownership has transferred, the successor is confirmed active, dependent jobs are checked and the configured authority explicitly approves closure.
An uncertain close is observed before any retry, and the successor never closes its own tab.

## Dispatch and review

Resolve the saved active choice before each new launch.
Dispatch records the exact profile, role, model slug, effort and selection digest used for that launch.
Existing sessions retain their launch records even if a later profile changes.
Review plans bind to the current selection and repository scope, and any relevant change requires a new plan.
Advanced review needs an authorization record matching both the current review scope and selection.

### Track light-profile reviews by target and cycle

The opt-in `claude-codex-light` profile keeps the Claude-Codex launch choices and advanced-review pair.
Its first standard-review cycle requires Codex Sol high in slot 1 and Claude Opus high in slot 2.
Later cycles use Codex Sol high in slot 1 only after every required slot in the linked predecessor chain exits successfully.
The cycle policy never changes the active profile or advances from a changed scope.

For an existing installation, copy your saved `Claude-Codex` role choices before you add the light profile.
Keep the orchestrator, implementer, researcher and advanced-review entries from `model-preferences show Claude-Codex`.
Keep your configured family keys for Codex Sol high and Claude Opus high in the standard-review pair.
Add the policy field shown above, then save the file with `profile-put`.
Do not replace your saved model family keys with the example keys.

    model-preferences show Claude-Codex > /absolute/path/to/claude-codex-light.json
    model-preferences profile-put claude-codex-light --file /absolute/path/to/claude-codex-light.json
    model-preferences activate claude-codex-light --orchestrators future-only

Create one review target for the pull request and carry its UUID across worktrees and handoffs.
Use an HTTPS PR locator when you want to bind the target to a remote pull request.
The target accepts one PR locator, and the locator cannot be changed after binding.

    REVIEW_TARGET_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
    model-preferences review-target create --cwd /absolute/path/to/worktree --id "$REVIEW_TARGET_ID" \
      --pr-locator https://github.com/owner/repository/pull/27

Start the first standard-review cycle with a stable cycle key.
The plan output includes the saved plan ID and cycle ID.

    model-preferences review-plan --cwd /absolute/path/to/worktree --base main \
      --facts /absolute/path/to/review-facts.json --target "$REVIEW_TARGET_ID" \
      --cycle-key first-review

Resolve the contextual slot with the same target, cycle and plan IDs that you pass to dispatch.
Plain `resolve` and `status` output describe configured reviewer candidates without choosing a cycle slot.
Pass `--review-target` and `--review-cycle` to `status` to inspect a saved cycle.

    model-preferences resolve --role standard_review --slot 1 --review-target "$REVIEW_TARGET_ID" \
      --review-cycle CYCLE_ID --review-plan PLAN_ID
    model-preferences dispatch --role standard_review --slot 1 --cwd /absolute/path/to/worktree \
      --task-file /absolute/path/to/review-task.md --review-target "$REVIEW_TARGET_ID" \
      --review-cycle CYCLE_ID --review-plan PLAN_ID

Dispatch slot 2 with the same plan to complete the first cycle.
Create a later cycle with a new key and the exact predecessor cycle ID only after both first-cycle slots succeed.

    model-preferences review-plan --cwd /absolute/path/to/worktree --base main \
      --facts /absolute/path/to/review-facts.json --target "$REVIEW_TARGET_ID" \
      --cycle-key second-review --previous-cycle CYCLE_ID

Repeat `review-plan` with the same target and cycle key to refresh an unfinished cycle after a scope change.
The refresh keeps that cycle's frozen slot set and requires each slot to complete the new plan.
The command holds while a linked launch is active or uncertain.
Use a new cycle key and predecessor after a completed cycle for an intentional repeat review.

`review-plan` and `review-inspect` include a `review_guidance` object alongside the existing plan fields.
It shows whether the saved selection and scope are current, the saved approval authority when known, whether approval is required, matching approval decisions, and each configured reviewer slot's provider, model and effort.
For an advanced plan that needs approval, ask the configured authority to decide on that exact plan and scope before recording the decision with `approve-review`.
The output includes the scoped command prefix and the next supported action, including when the authority is unset or the plan is stale.
Inspection only reads the plan, current configuration, worktree and existing approval records; it does not create approvals or launch reviewers.
For `Codex-Qwen` and `Qwen-only`, the dispatch template also retains the required fresh `--review-authorization` value for each review launch.
Legacy plans can identify their saved authority and reviewer choices only while their selection digest still matches the active selection.
When a plan is stale, its current configured authority is never presented as the plan's saved authority.

Repeat a dispatch to inspect or reuse the saved slot launch.
Pass `--retry-failed` only after the exact slot's recorded launch exits with a nonzero code.
The tool does not retry a launch whose state is missing, active, failed without an observed exit code, or uncertain.

Standard-review cycles do not change advanced-review approval rules.
Other profiles keep their configured standard-review behavior and do not require target or cycle IDs.

### Reviewer judgment guidance

Model Preferences prepends its maintained reviewer judgment guidance when `dispatch` launches a configured `standard_review` or `advanced_review` role through the Codex, Claude, or Muse CLI.
This includes standard reviews selected through the `claude-codex-light` target and cycle workflow.
`resume` applies the same guidance to recorded Codex reviewer continuations after the existing scope, plan and approval checks pass.
OpenCode reviewer dispatch remains unsupported.

The complete task file stays unchanged at the end of the composed prompt, and launch task digests continue to identify the original file bytes.
The parent brief must supply the approved PR purpose and relevant repair history.
The guidance asks the reviewer to ask the parent when that context is materially missing.

Print the same maintained text used by those launch routes with `model-preferences review-guidance --print`.
The command works without reading or creating Model Preferences configuration, state or locks.
Supply the complete parent brief, including the approved purpose and relevant repair history, as the task file to automatic `dispatch` and supported Codex `resume`; those routes add the maintained guidance automatically.
Manual native launches and other routes that bypass Model Preferences dispatch or Codex resume do not receive the guidance automatically.
For those routes, include the printed guidance and the complete parent brief in the prompt.
The Pattern Buffer adapter remains limited to Claude and Codex implementers and does not add reviewer task replay.
Installed hooks and source configuration are not evidence that a reviewer received the text; confirm reception in an exact native pilot.

Pattern Buffer can provide exact task replay to temporary worktrees.
Use the --pattern-buffer script option and owner worktree only when the child should share that owner's approved context.
This adapter does not transfer task ownership or grant a broader sandbox.

## Installation and verification

Run the component verifier from the repository root.

    python3 apps/model-preferences/verify.py

Use the installer with isolated runtime, binary, config, state and harness settings paths when checking installation behavior.
The installer preserves unrelated settings and refuses unowned or modified runtime files.
For installed hooks, configuration and hook registration do not prove that a native session loaded, trusted or received the context.
Review the harness's own trust controls and confirm behavior through the exact session that will use the saved profile.

The application stores profile configuration under the XDG config directory and local launch, review and handoff records under the XDG state directory.
Use the CLI's --config-dir and --state-dir options to select isolated paths for a separate installation.
Existing schema-v1 configurations without an approval label remain readable with authority unconfigured and are not rewritten on read.
Approval and handoff-close actions fail closed until the operator uses authority-set.

## OpenCode local Qwen dispatch

The opt-in examples include `local-qwen3.6-35b`, `Codex-Qwen` and `Qwen-only`, and initialization never activates them.
`Codex-Qwen` selects Codex Sol medium as orchestrator, local Qwen at max effort for implementation and research, and Codex Sol high for reviews.
`Qwen-only` selects local Qwen at max effort for orchestration, implementation and research, and Codex Sol high for reviews.
Every review launch in either reserved profile requires a fresh `--review-authorization` value on that command, including standard review, and the state record keeps only its SHA-256 digest.
Advanced reviews also require the existing exact-scope plan and explicit configured-authority approval.

The OpenCode family binds an exact provider ID and model slug instead of asking OpenCode to resolve a model name.
For a new configuration, record the observed local provider and model explicitly.

    model-preferences model-set local-qwen3.6-35b --harness opencode --provider-id trial-local --slug qwen3.6:35b --efforts max --source "verified OpenCode 2.2.5 local provider"

OpenCode dispatch requires a pre-existing OpenCode 2.2.5 API server with Basic authentication on a loopback IP address and the exact local provider/model enabled.
The separate JSON connection manifest and password file must be regular files owned by the current user with mode `0600`, and neither may be a symlink.
The manifest contains `api_url`, `expected_server_pid`, `password_file` and an explicit `parent_task_id`; optional `metadata` can pin `provider_id` and `model_id`.
Optional top-level `delivery_policy` accepts `native_empty_composer` or `api_sidechannel`; omission selects `native_empty_composer`.
Model Preferences records the selected policy with the exact launch and refuses a resume if the private manifest changes it.
The explicit `api_sidechannel` policy permits only `empty`, `draft`, or `unknown` native composer classifications while retaining exact current, hook, API, lifecycle, receiving, and human-input guards.
Both policies require an inactive API session and empty inbox, permission, and form lists.
An opted-in continuation sends `text`, `delivery: queue`, and `resume: true`, and remains uncertain unless the exact API message is admitted to the queue.
The policy does not change startup prompts or role choices, and changing it on a recorded launch holds before a continuation send.
The password stays in its private file and is not copied into the manifest, process arguments, launch records or receipts.
The API PID and loopback origin bind the connection for this preflight, but OpenCode 2.2.5 exposes no stable server-startup identity, so PID reuse remains a limitation.

Use the read-only preflight before costly inference to check the active saved role against the exact worktree and OpenCode API metadata.
It reads the active profile only from the provided `--config-dir` and requires the requested role to select OpenCode and the exact Git worktree root.
Use the runbook's isolated `--config-dir` and matching `--state-dir` for pilot choices; preflight never activates a profile or overrides shared launch choices.
It reads the private connection manifest, checks the OpenCode version, provider, model, local URL, `max` reasoning variant, request body and output allowance, and returns bounded JSON evidence with a typed error code and remediation when held.
It sends `GET` requests only and does not write profile state, create a session or CMUX surface, submit a prompt, or request model inference.
Optional repeated `--require-plugin ID` checks use the `GET /api/plugin` response for that exact directory and require the matching row's `state.status` to be `active`.
A configured plugin path is not evidence that the API server loaded it, and a failed local plugin row may have no `id`, so that failure is reported without assigning it a made-up ID.
API plugin status is server-reported configuration evidence only; it does not prove native TUI hook delivery, permissions, model reception or task success.
For model-free config and API-shape smoke checks plus observer preparation, see the [Qwen inference and test observer preflight](../../docs/qwen-operating-guide.md#inference-and-test-observer-preflight).

```sh
model-preferences --config-dir /private/config --state-dir /private/state preflight \
  --role implementer --cwd /absolute/path/to/worktree \
  --opencode-manifest /private/opencode-connection.json \
  --require-plugin EXACT_API_PLUGIN_ID
```

Launch an implementer or researcher task from the exact Git worktree root while running inside the intended CMUX pane.

    model-preferences --config-dir /private/config --state-dir /private/state dispatch --role implementer --cwd /absolute/path/to/worktree --task-file /absolute/path/to/task.md --opencode-manifest /private/opencode-connection.json

The helper scopes both catalog requests to the exact target or session directory and requires both API responses to report that directory.
It resolves provider, model, and selected `max` variant settings and request bodies using OpenCode 2.2.5's overlay rules before checking the effective loopback endpoint and `reasoning_effort` body value.
The helper checks the authenticated API version, server PID, exact provider and model, local provider URL, tools, the named `max` reasoning variant and a distinct output budget of at least 32768 tokens before creating a session.
An unlinked session is created directly at the target directory and its native `ses_` ID, directory, and model are verified before TUI startup.
OpenCode 2.2.5 creates a parent-linked child at its parent's directory, so the helper first persists the returned native `ses_` ID and parent identity, then uses the supported session move API to relocate the idle child to its target directory.
The helper confirms the exact session, parent, model, launch ticket, and final directory before it proceeds; an uncertain move is not repeated.
When Pattern Buffer dispatch is requested, its immutable task snapshot is prepared before session creation and bound to that exact native session only after the final child directory is verified.
The child TUI obtains its CMUX workspace and surface UUIDs from its own `cmux identify` result, and a private launch barrier requires those UUIDs to match the exact newly opened surface before the session-scoped CMUX environment is set and the initial prompt is released.
The child transfers its own native `CMUX_SOCKET_CAPABILITY` through that mode-0600 handshake, and Model Preferences writes it only into the exact OpenCode session environment.
The capability is never written to launch records, receipts, command arguments or diagnostics.
The initial task is recorded only after the exact startup prompt appears in the exact session message history, a matching assistant turn is complete, the native session is idle, and API inspection reports no active turn, pending permissions or pending forms.
The bounded pinned-2.2.5 history reader follows opaque cursors to an empty end page, because nonempty response cursors are navigation anchors rather than evidence of additional pages.
This records an observed completed turn, not semantic task success.
If CMUX current, hook-generation or empty-composer proof is unavailable, the completed turn remains recorded while managed continuation stays held.
Wrong or ambiguous surfaces, timeouts, unsupported schemas and uncertain sends remain unresolved and block retry until inspected.
Titles and working directories alone never establish a native session identity.

Start a fresh Qwen-only orchestrator with its own Pattern Buffer orchestrator assignment.

    model-preferences --config-dir /private/config --state-dir /private/state dispatch --role orchestrator --cwd /absolute/path/to/owner-worktree --task-file /absolute/path/to/task.md --pattern-buffer /absolute/path/to/pattern_buffer.py --opencode-manifest /private/opencode-connection.json

This opens a new visible TUI in the current CMUX workspace after creating and verifying the exact API session.
It does not replace an existing orchestrator or mutate the active profile.
The orchestrator may prepare a bounded implementer child in a separate Git worktree with `--pattern-buffer`, the exact owner worktree and a unique immutable receipt.
Child dispatch requires the exact currently executing OpenCode orchestrator session, its current CMUX surface and workspace, its active admitted task, and the matching Pattern Buffer `{root, role}` assignment digest.
The API child `parentID` stores that verified native parent `ses_` ID separately from the explicit parent task reference and the new child Pattern Buffer ticket.
Codex-Qwen keeps Codex Sol medium orchestration and uses local Qwen only for configured implementation and research roles.
Qwen-only uses local Qwen for orchestration, implementation and research, with external Codex Sol high reviews.
Every review launch still requires fresh explicit operator authorization.

Continue a recorded OpenCode session with a new bounded task only when the exact server, session model, worktree, native session, hook generation and CMUX placement still match.

    model-preferences --config-dir /private/config --state-dir /private/state resume LAUNCH_ID --task-file /absolute/path/to/follow-up.md --opencode-manifest /private/opencode-connection.json

Before one Model Preferences continuation prompt is sent, the default policy requires an empty native composer, while `api_sidechannel` permits a `draft` or `unknown` composer classification with no blocking input or human-input obligation.
Both policies require native receiving, exact CMUX current and hook records for the recorded session generation, an idle OpenCode API session, and no pending inbox work, permissions, or forms.
The helper persists a send intent before the API request.
If the response is uncertain, the exact session is held against automatic retry.
An accepted prompt records submission only; later model output is not inferred.
Human drafts, busy or unknown states, changed worktrees, stale or ambiguous surfaces, unsupported versions and uncertain API responses all hold without sending.

The OpenCode API server must already be running on the exact loopback origin with its private connection manifest and password file.
Dispatch itself opens the interactive TUI in a new CMUX surface and never starts or configures a global OpenCode service.
OpenCode 2.2.5 exposes no stable server-startup identifier, so a reused PID at the same loopback origin remains a binding limitation.
