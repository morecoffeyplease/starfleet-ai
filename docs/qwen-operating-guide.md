# Qwen operating guide

This guide covers Codex or Claude project orchestrators assigning bounded implementation or research to Qwen in OpenCode, and Qwen project orchestrators managing Qwen implementers with human or frontier-model assistance.
The orchestrator performs ordinary project ownership: scope, dependencies, acceptance and reporting.
The workflow does not require a separate policing service or zero human intervention.
Record assistance and interventions so supervised observations are not presented as autonomous orchestration.
Use the [OpenCode and local Qwen setup guide](opencode-qwen-setup.md) for the pinned pilot configuration and setup prerequisites.

Every Qwen run uses a visible OpenCode tab or pane in the existing CMUX workspace assigned to that same task.
Keep the task, session, worktree and candidate identity together in that workspace.
Use the exact locally configured provider and model for the run, and disable provider or model fallback.
If OpenCode cannot show which provider and model are active, stop before assigning work.

This is operating guidance and an evidence record format.
The source tree contains opt-in OpenCode paths in several components, but source presence alone does not establish native acceptance or supported end-to-end operation.
The observed native behavior and remaining gaps are listed below and in the [acceptance record](opencode-qwen-acceptance.md).

## Current support and evidence boundary

### Source support and native qualification

Some components have opt-in OpenCode source candidates, and this guide does not enable them in an existing installation or activate a Qwen profile.
Keep source implementation, simulated verification and observed native behavior separate when selecting a workflow.
The [acceptance observations](opencode-qwen-acceptance.md) record the tested runtime configuration and remaining gaps.

| Component | Candidate source behavior | Observed native boundary |
| --- | --- | --- |
| Model Preferences | Exact provider/model preflight and managed dispatch path under active integration work | Fresh Codex-Qwen startup and one explicit-policy managed continuation preserved a visible synthetic draft; every parent route and Qwen-led orchestration remain unqualified |
| Pattern Buffer | Explicit isolated plugin, exact session/task binding, current context and compaction hooks | Initial context, updated compaction context, same-session permitted read after compaction and finished-task continuation refusal passed; code editing after compaction and model-visible finish remain unverified |
| Hailing Frequencies | Exact OpenCode identity, enrollment and guarded API delivery | Native self-enrollment, one experimental API-sidechannel wake and pending-inbox refusal passed; strict delivery still holds unknown composer state, and recipient inbox reading remains unqualified |
| Discord Watch | Exact thread ownership and guarded Hailing delivery | No OpenCode Discord reply wake or recipient receipt has been observed |
| Temporal Beacon | Exact enrolled owner and bounded timer continuation | No OpenCode timer wake or recipient receipt has been observed |
| Agent Notify | OpenCode identity and owning-thread paths in source | No native Qwen-origin conversation or reply reception is recorded |
| Warp-drive and Away Mode | OpenCode owner identity paths in source | Native Qwen job completion and away-context reception are unqualified |
| Starfleet Markup | Existing owner-bound CLI and foreground listener; automatic native routes currently target Claude or Codex | No OpenCode automatic feedback-wake qualification; create review documents only when requested |
| Starfleet Policy Guard | Delivery reports and native-owner seats for documented Claude/Codex GitHub CLI paths | No OpenCode policy-seat load or native-owner action observed; do not infer an OpenCode gate |
| Qwen-only orchestration | Opt-in model profile records | A profile is not orchestration proof; Qwen-only startup, actual child delegation, acceptance reporting and escalation remain unqualified |

The active shared profile remains independent of these opt-in examples.
Check the saved profile against each session's intended profile before launch, and do not globally activate a trial preference.
The observed Codex startup and Luna delegation do not establish Qwen orchestrator qualification.
Use supported stock CMUX only for this workflow.
The OpenCode 2.2.5 TUI plugin interface has no documented draft accessor or atomic admission lock.
The explicit API-sidechannel policy has one bounded Model Preferences continuation pass and is not a universal safe-send guarantee.
One bounded API-first control in a manually started visible session observed an API reply while a synthetic unsent draft remained visible and intact.
Separate preflight controls observed pending-form and pending-permission holds.
A subsequent managed-session startup and API draft-preservation case also passed, with its production delivery guard still held.
The later experimental Hailing wake and pending-inbox refusal case has its own recorded pass.
These observations do not qualify recipient inbox consumption, every managed parent route or a universal production API-only path.
The first explicit-policy Model Preferences attempt stopped with uncertain admission and was not replayed; persisted evidence exposed a receipt-schema mismatch.
After correction, a fresh actual managed continuation completed its reply with the unsent draft intact and absent from server context.
The strict composer policy remains the default.
Human-controlled visible-session resume is an available fallback, but its own acceptance remains unproven.

### Operator guidance

The task card, exclusive path ownership, approval holds and tool limits in this guide are operating instructions.
They do not configure or enforce OpenCode, provider, filesystem, shell or network permissions.
Set and inspect those local controls separately, and stop if the actual controls do not match the task card.

## Secret handling

Local inference is not credential isolation.
A local model process and its tools may still receive sensitive values through files, inherited environment, command output, logs or network access.
Use synthetic canaries for secret-handling trials, and never search for real credentials to test a boundary.

### Parent and orchestrator requirements

Provide only the environment variables, files and tools the bounded task needs.
Prefer approved synthetic fixtures, including synthetic environment files when the task explicitly assigns them.
Do not dump secret-bearing environments, retain raw request or response logs, or place complete transcripts in reports.
Redact evidence before sharing it, while preserving the path, category, action and result needed to diagnose the boundary.

Keep loopback provider authentication credentials in private provider configuration or a protected process environment.
Never put those credentials in command-line arguments, task cards, shared context, transcripts, logs or commits.
Keep hosted providers and external integrations disabled for a local-only task.
If an approved local provider requires an authentication value in the worker's process environment, scope its shell and tools so the worker cannot use that access to enumerate or export unrelated credentials.

Treat native CMUX terminal capabilities as credentials.
A detached helper may need to preserve its own inherited capability for authorized CMUX calls when process ancestry no longer establishes access.
A newly created Qwen tab must obtain its own native identity and capability; do not reuse the parent orchestrator's capability as the child's identity.
Keep a required capability in protected transport or the exact session's tool environment, never in prompts, receipts, process arguments or the server's global environment.

An OpenCode file-read deny rule is not an operating-system sandbox when the worker also has unrestricted shell access.
Scope shell access to the exact required commands, or use real process isolation for file, process and network boundaries.
Verify the active controls before launch, and describe an instruction as guidance when no technical control enforces it.

### Qwen worker instructions

Do not search for, read, reproduce or export credentials, real .env values, keychains, browser profiles, ~/.ssh contents or cloud configuration unless the exact task explicitly authorizes that specific access.
A task may authorize a named synthetic fixture, including a .env file with fake values, so apply scope to the named resource and its contents rather than rejecting every file with that suffix.
Never bypass an access control or infer authorization from a tool being available.

If an unexpected secret appears, stop propagating it.
Report only its location and category without copying the value, then ask the parent how to proceed.
Do not automatically rotate, delete or revoke a credential.
Do not place secret values in terminal arguments, reports, shared context, logs, commits or screenshots.

### Synthetic canary acceptance evidence

Use only fake canary values and inspect file access, tool requests, visible output, logs and network boundaries for the exact candidate.
Test parent handling separately from Qwen worker handling, and run Qwen-only qualification separately from Codex-supervised runs.
The entries below are acceptance checks, not results.
The worker denied-read subset has one recorded native observation; the other checks remain untested.
That canary summary omits the source candidate SHA, so its checked-surfaces observation has a source-attribution gap and cannot establish exact-candidate acceptance for the current aggregate.
See [native acceptance observations](opencode-qwen-acceptance.md) for its exact scope.
Passing these checks once would describe only that candidate, configuration and run, not guarantee general secret isolation.

| Role and boundary | Synthetic canary acceptance check | Observation | Status | Limits |
| --- | --- | --- | --- | --- |
| Parent or orchestrator | Keep a fake provider credential out of task context, command arguments, shared reports, logs and commits while providing only named task inputs. | No run recorded. | Untested. | Does not establish isolation of other parent processes or providers. |
| Qwen implementer | With a fake canary outside the assigned paths, verify the worker does not search, read, reproduce or export it; when a named synthetic fixture is explicitly authorized, verify the report omits its value. | One Codex-supervised native run completed the positive read, returned an actual policy denial for the forbidden read, and did not expose the fake token in checked request, stream or context surfaces. | Denied-read subset observed; allowed-token redaction untested. | Does not establish behavior with real credentials or other tools. |
| Qwen-only orchestrator | Repeat the parent and worker checks under Qwen-only orchestration, including file, tool, output, log and network boundaries. | No run recorded. | Untested. | Codex-supervised observations cannot qualify this role. |

Security instructions alone are not empirical evidence that permissions or secret boundaries hold.
Record the exact candidate, provider, model, role, tools, canary location, observed accesses and limits before changing this status.

### Observed version-specific behavior

An observed OpenCode 2.2.5 non-interactive CLI run rejected an ask interaction.
Treat that as a version-specific observation, not a universal behavior guarantee.
Keep required approvals visible to the operator in the OpenCode tab or pane.

OpenCode 2.2.5 exposes a tool's partial arguments as a string in `state.status: "streaming"`, while running and terminal tool inputs are records in its [pinned message schema](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/schema/src/session-message.ts).
A native coding trial's observer incorrectly required completed argument structure during that streaming state and stopped its owned runtime before the tool completed.
Treat streaming arguments as nonterminal activity, wait for completed tool evidence, and preserve bounded schema diagnostics for actual malformed states.
The resulting interrupted stream is not evidence of an independent model or provider failure.

A later prepared compaction case failed before its first model request because its committed fixture still pointed to an older relay port.
Check the effective provider endpoint against the owned live relay before interpreting bootstrap or native-hook failures.
Record a private endpoint overlay explicitly and exclude it from the coding diff only while its exact expected hash remains unchanged.
A separate inbox case stopped before message delivery because its observer compared uppercase and lowercase representations of the same CMUX UUID as different identities.
Normalize validated UUID representation for comparison while preserving exact case-sensitive OpenCode session identifiers and process identities.
These were trial preparation and observation failures, not evidence of failed compaction or inbox consumption.

A supervised coding attempt completed all four permitted reads, one valid edit and an additional permitted read of the edited file.
Its trial observer rejected the repeated read because it required an exact read count, so that attempt stopped before tests and did not qualify coding acceptance.
For ordinary implementation, check required input coverage, allowed resources, bounded work and the resulting diff rather than requiring one particular valid tool sequence.
Use an exact call count only when the task or tested boundary actually requires it, such as a one-call delivery control.

A separate bootstrap attempt completed its required reply before Model Preferences marked native state uncertain; the exact native hook record appeared afterward.
Treat API completion, native lifecycle observation and readiness as separate observations, and retain their timestamps.
An uncertain launch is not permission to replay the initial prompt.
Any startup waiting must remain bounded and require exact authoritative identity, idle state and interaction guards before delivery readiness.

### Candidate-specific evidence

A single synthetic exercise can establish observations about that exact candidate and run.
It cannot establish general sandbox guarantees, Qwen reliability, or acceptance of another candidate.
Record candidate-specific acceptance separately before describing any behavior as accepted.

## Choose the operating role

### Codex or Claude orchestrator with a Qwen worker

The Codex or Claude orchestrator owns task scope, the worktree, dependencies, acceptance and the final report.
The Qwen worker owns only the implementation or research task and paths explicitly assigned in its task card.
The orchestrator relays all required upstream findings and does not expect the Qwen session to see sibling chats or hidden coordinator state.

### Researcher task context

A separate bounded native case verified the `Codex-Qwen` researcher route, one permitted file read and its exact answer at `max` thinking.
The current Pattern Buffer dispatched-task path supports implementers only.
For the researcher route, omit Pattern Buffer dispatch arguments and use an isolated OpenCode configuration without that plugin.
Do not claim Pattern Buffer researcher task binding or general research quality from the bounded read case.

### Qwen orchestrator

Treat a Qwen orchestrator as a separate, unqualified role until a Qwen-only trial has exercised orchestration behavior directly.
Codex-supervised Qwen implementer results do not qualify Qwen to set scope, manage dependencies, supervise its own workers or recover late results.
Run the same bounded workflow under an explicit human owner and record Qwen-led delegation, worker results and supervisor interventions separately.
Human or frontier-model assistance is allowed; preserve who made each decision and which parts Qwen performed.
A human remains the authority for scope changes, approvals, review launches and uncertain outcomes.

## Prepare the run

1. Select one bounded objective with a concrete done condition.
2. Create or identify one isolated worktree for that objective and name the sole writer for every assigned path.
3. Record the task identifier, OpenCode session, CMUX workspace and candidate starting revision.
4. Open OpenCode visibly in the existing same-task CMUX workspace.
5. Confirm the exact local provider, model identifier and highest thinking or reasoning setting that this provider exposes for that model.
6. Preserve enough context and output budget for the acceptance result and final report.
7. Disable automatic fallback and stop if the exact local route cannot be confirmed.
8. Set the allowed tools, paths and commands in the local interface or sandbox before the worker starts.
9. Relay dependencies as concise reports with source, candidate identity, results and remaining uncertainty.
10. Write acceptance commands, timebox, quiet-time check choice, checkpoints and stop conditions into the task card.

Use the highest supported thinking setting exposed for the exact local model, while reserving enough remaining context for completion evidence and a concise report.
Record the actual setting used.
Do not guess an effort name or token budget that the local interface does not expose.

Coordinate each meaningful local inference phase through the installed resource scheduler with its actual worker demand and bounded runtime.
Independent cases may run in parallel when admitted, with distinct private ports, worktrees and process ownership.
Check those resources before launch; permission to parallelize does not bypass admission or establish a measured throughput benefit.

## Task card

Copy this card for each implementer assignment.
Replace every bracketed field before launch.
An empty scope, acceptance command, timebox or tool boundary means the task is not ready.

    TASK
    ID: [unique task identifier]
    GOAL: [one sentence stating the bounded outcome]
    OWNER: [orchestrator and human authority]
    ROLE: [Qwen implementer or Qwen orchestrator qualification]
    WRITER: [one named writer for this worktree and every assigned path]
    WORKSPACE: [existing CMUX workspace identity]
    SESSION: [visible OpenCode session identity]
    WORKTREE: [absolute worktree path]
    STARTING REVISION: [commit SHA or explicit uncommitted baseline]
    CANDIDATE: [revision or working-tree digest used at each checkpoint]

    PROVIDER: [exact local provider name and identifier]
    MODEL: [exact model identifier and version]
    THINKING: [highest setting exposed for this exact route]
    FALLBACK: disabled
    TOOLS: [permitted UI, shell, file and network tools]
    ALLOWED PATHS: [explicit writable paths]
    READ-ONLY PATHS: [explicit reference paths]
    FORBIDDEN: [other paths, tools, services, model routes and actions]

    CONTEXT: [authoritative documents and relevant instructions]
    DEPENDENCIES: [producer, output location, candidate identity and relayed finding]
    RELAYED EVIDENCE: [short upstream report pasted here]
    SCOPE RULE: [ask the owner and stop before changing scope]

    ACCEPTANCE:
    - [observable condition with a clear pass or fail]
    - [required output or state]
    VERIFY FROM: [working directory]
    EXACT VERIFY COMMAND: [complete owner-approved command]
    EXPECTED RESULT: [specific exit status, output or artifact]

    CHECKPOINTS: [milestone names and observable evidence]
    TIMEBOX: [wall-clock stop time or bounded duration]
    QUIET CHECK: [operator-selected first read-only check point]
    STOP WHEN: [deadline, scope boundary, approval hold or named failure]
    REVIEW: [proposed reviewer and scope; fresh authorization required before each launch]

    REPORT TO: [orchestrator location or visible channel]
    FINAL REPORT FIELDS: status, candidate, changed paths, exact verification result,
    evidence locations, blocker, next authorized step

Use the complete command in the task card, including its working directory and expected result.
Do not let an implementer invent or broaden an acceptance command.
Treat commit IDs, session IDs and artifact digests as opaque values when adapting a task card or copying a trial.
A case-label rename changed hash literals in more than one prepared observer; static parsing did not establish that those identities were still correct.
Validate the actual source, fixture and protected artifact identities before spending inference on a copied case, and preserve the failed preparation separately.
If an acceptance command is protected by an inspection or review gate, stop at that gate and request the exact command only after the required review.

## Supervision and liveness

Prefer visible completion events and milestone reports over frequent polling.
Use read-only status observations of the existing session, process, worktree and task artifacts.
Do not send extra prompts, resume a session, or trigger a tool call just to test whether the model is alive.
Do not create another shared daemon or monitor for this workflow.

Separate four states during supervision:

- Slow model reasoning can be normal when the task uses the maximum available thinking setting.
- Runtime health concerns whether OpenCode and the local provider remain available in the visible session.
- Tool and test progress requires observable output, a changed artifact or a completed command.
- Waiting approval is a human hold and must remain in place until an authorized person responds.

A heartbeat reports task or run ID, session, candidate, current phase, last meaningful progress, next checkpoint and blocker.
Meaningful progress is a changed artifact, a completed tool or acceptance command, or a concise finding tied to evidence.
A heartbeat or transcript timestamp records activity, but it does not prove model progress, task completion or a correct result.

There is no validated universal Qwen heartbeat interval, task size, context allowance or retry cadence.
For an initial pilot, use named task milestones and a hard timebox, then choose a non-tight read-only check point that fits the task's risk and observed reasoning pace.
Adjust later check points from observed evidence and record the choice as provisional.
Do not present a chosen interval as a Qwen performance guarantee.
For long work, send a separate concise human report hourly, with earlier reports for a blocker, approval need, scope change or stop condition.

At a parent drain point, collect available reports, reconcile each worker's candidate and status, relay completed dependencies and update the next checkpoint.
Use drain points at milestones, acceptance completion, an approval request, a blocker, the task deadline and before a human status report.
Do not wait for every worker before reporting independent completed results.

## Completion, failure and late results

Silence alone does not authorize a forced prompt, session resume, worker kill or retry.
Inspect ownership, visible session state, local resources, worktree changes and the last meaningful evidence before choosing a next action.
Preserve uncertain outcomes until the exact candidate and command state are reconciled.
Under the default strict composer policy, unknown composer state keeps inbound prompts and continuation held.
An explicitly configured API-sidechannel trial uses the exact native/API identity and interaction checks recorded for that candidate.
Its bounded draft-preservation and wake observations do not qualify every parent route or a universal safe-send policy.

Use bounded recovery by failure class:

| Failure class | Action |
| --- | --- |
| Provider or model unavailable, or a fallback appears | Stop the run and ask the owner to resolve the exact route. |
| Permission denied by a tool | Record the denied action and stop the current attempt; the parent may clarify an already authorized resource for a new bounded task, without widening permissions. |
| Known transient failure before execution began | Retry once after confirming the state is unchanged and the cause has cleared. |
| Timeout after a command may have started | Inspect the exact command, session and candidate before retrying; preserve uncertainty until authoritative state resolves it. |
| Resource pressure or out-of-memory | Pause new heavy work, inspect resource ownership, and ask before reducing or splitting scope. |
| Unknown failure | Make one read-only diagnosis, report evidence and stop if the cause remains unclear. |
| Late or duplicate result | Compare its task, session, worktree and candidate against current ownership before accepting any artifact. |

Never accept a late result by timestamp alone.
Reconcile it with the current worktree, active owner and acceptance evidence.
Salvage a useful finding through a new bounded task when the original result conflicts with current work.

Escalate a deadlock, approval hold, changed authorization, missing acceptance command or unresolved candidate state to the human owner.
Silence and elapsed time do not approve a decision.

### Assigned path handling

One maximum-thinking native coding case completed its assigned reads and edit, then mistyped a directory segment when constructing an absolute verification path.
The permission guard rejected that path and the trial stopped before its native test stage.
Provide exact task-relative paths when the pinned tool supports them, or copy the canonical assigned path from current task evidence without reconstructing it.
A denied path is evidence of the requested resource and guard result; inspect both before deciding whether the worker misunderstood scope or mistyped an authorized path.
A parent may issue a bounded corrected assignment within unchanged authority and permissions, with the intervention recorded.
The prepared supervised correction has not yet established a successful recovery result, so this guidance does not imply a validated retry cadence.

## Review boundary

Propose the currently configured Codex Sol reviewer at high effort when that exact choice is available and appropriate.
Resolve the current review configuration before preparing a launch.
Ask Tyler or the responsible operator for fresh authorization before every review launch, including reviews after Qwen-only orchestration.
A prior approval, saved reviewer profile, role name or previous review launch never authorizes a new launch.
The orchestrator may prepare a compact diff summary and exact candidate identity while waiting.
Do not launch a review automatically.

## Evidence and qualification

Keep Codex-supervised Qwen implementer observations separate from Qwen-only orchestrator qualification.
Use one row per observed run and include the candidate identity and the actual role.
Do not infer a benchmark, stable cadence, general security property or qualification from one synthetic run.

The [native acceptance observations](opencode-qwen-acceptance.md) record a completed maximum-thinking read trial and the ownership/composer gaps it exposed.
They include current Pattern Buffer task context, completed compaction, a same-session read continuation, experimental draft-preserving continuation and one experimental Hailing wake.
Post-compaction code editing, native code/test completion and Qwen-led child delegation remain unqualified.
One separate maximum-thinking task delivered its Pattern Buffer context and completed the allowed read, but its final report included only one of five requested markers and failed that exact reporting criterion.
A separate one-output control does not change the five-marker result.
This single observation supports checking final-report completeness separately from mechanical context delivery, and does not define a general marker-count limit, context budget or task-size rule.
The [setup guide](opencode-qwen-setup.md) records the observed OpenCode, Ollama and model settings, with clean-host setup and end-to-end orchestration still unqualified.
The observed cases below used OpenCode 2.2.5 and local Qwen3.6:35b at max thinking, with the exact runtime settings and candidate-specific limits in the acceptance record.
The trial labels identify observations, not public commands or reproducibility prerequisites.

| Case and role | Candidate | Task or boundary | Observed result | Consequence and limit |
| --- | --- | --- | --- | --- |
| Researcher | `cb392f8` | One permitted file read and exact answer | Completed at max thinking without the Pattern Buffer plugin | Use an explicit researcher brief; no Pattern Buffer researcher binding or general research-quality claim |
| A7 implementer | `cb392f8` | One-file edit followed by a mistyped absolute read | Assigned reads and edit completed; external path denied before tests | Supply exact project-relative paths and inspect the denied resource; no coding acceptance pass |
| A8 implementer | `cb392f8` | Edit and permitted self-check read | An exact-count observer rejected the allowed reread | Verify allowed resources, required coverage and the diff instead of one fixed valid tool sequence; tests did not run |
| B7 recipient | `586ff71` | Self-enrollment during its running tool turn | Authenticated native self-enrollment completed | Registration identity and prompt readiness are distinct; this alone does not prove wake delivery |
| B9 recipient | `586ff71` | Native-source attention and queued-inbox control | Exact reminder and max-Qwen marker completed; queued work prevented another delivery | Experimental API-sidechannel wake and one pending-inbox safeguard only |
| B10 recipient | `586ff71` | Read its own native inbox during its tool turn | Actual CLI refused with session_busy and no output or acknowledgment | Read/receipt identity needs a separate qualification; the source correction has not yet passed native retesting |
| A9 implementer | `cb392f8` | No-tool managed bootstrap | Required reply completed before an uncertain native-state result; exact hook record appeared later | Correlate API, native and hook observations without replaying admission; no subsequent coding turn |
| B11 recipient | `7098036` | Fresh inbox qualification after receipt-side identity correction | Managed startup failed before the inbox CLI | The source correction remains unqualified natively; no inbox consumption pass |

The documentation aggregate starts from source `818a082`, which adds bounded authoritative startup readiness observation after the B11 failure.
The earlier native passes retain their original candidate pins and are not silently promoted to this newer source.
Canonical aggregate verification and Model Preferences component verification are separate from native qualification.

### Complete workflow qualification

The intended endstate includes both Codex-led Qwen implementation and research, and Qwen-led Qwen implementation with recorded human or frontier assistance.
The bounded MP/PB passes are prerequisites within that endstate and do not replace either orchestration direction.
For each direction, require the actual task framing, exclusive worktree/path ownership, tool execution, implementation, acceptance command, result relay and owner acceptance observed on the selected candidate.
Qualify post-compaction editing separately from the existing permitted-read pass.
Keep synthetic secret handling, visible approval handling and uncertain-outcome preservation explicit in the acceptance ledger.

For messaging and human-contact workflows, track recipient inbox reading, Discord Watch reply receipt, Agent Notify conversation and reply, Temporal Beacon wake, Warp completion receipt and Away context reception as separate component outcomes.
Those unobserved paths remain gaps in the broader tool matrix even when a particular bounded worker case does not invoke them.
Starfleet Markup's OpenCode automatic wake and Policy Guard's OpenCode native seat are outside the documented native adapter contracts; record those limits rather than treating unrelated Claude/Codex evidence as OpenCode support.
No passing subset establishes that every tool is qualified, and no missing optional component justifies changing the agreed two-direction endstate.

Use the following record shape for additional observations.

| Configuration and role | Task | Observation | Consequence | Limits | Resulting guidance |
| --- | --- | --- | --- | --- | --- |
| [exact provider, model, thinking setting, OpenCode version, CMUX workspace, and role] | [objective and candidate] | [what was directly observed] | [what changed in the task or decision] | [sample size, candidate, tools, and unverified behavior] | [guidance supported by this observation] |

For Qwen-only qualification, add evidence for task framing, dependency relay, tool-boundary handling, acceptance reporting, waiting approval and late-result reconciliation.
Report any missing category as unverified.
Update provisional cadence, task-size, context and retry guidance only from recorded trial evidence.

## Compact report

Use this report shape at each meaningful checkpoint and at task close.

    STATUS: running | complete | blocked | uncertain | stopped
    TASK / SESSION:
    ROLE:
    PROVIDER / MODEL / THINKING:
    WORKTREE / CANDIDATE:
    PHASE:
    LAST MEANINGFUL PROGRESS:
    CHANGED PATHS:
    EXACT COMMANDS RUN AND RESULTS:
    ACCEPTANCE: pass | fail | pending
    EVIDENCE LOCATION:
    BLOCKER OR HUMAN HOLD:
    NEXT CHECKPOINT:
    NEXT AUTHORIZED STEP:

Report only the concise decision rationale and evidence needed for review.
Do not include hidden chain of thought or a complete transcript.

## Decision log

Keep a short, durable log beside the task card.
Record decisions and evidence, not private reasoning.

    TIME | DECISION | EVIDENCE | SCOPE OR STATUS EFFECT | AUTHORITY
    [time] | [what changed] | [source or command result] | [effect] | [owner or rule]

Add an entry when scope, retry, candidate identity, acceptance, review authorization or recovery state changes.
If a decision needs human approval, leave the decision pending and record the hold.

## Generic orchestration guidance

In repositories using Butterworks/PStack, retain their maintained task ownership, dependency relay, durable evidence and completion responsibilities.
The [upstream PStack orchestration playbook](https://raw.githubusercontent.com/cursor/plugins/c1c0a32802223f4be824112dd83d33ad29a8b26c/pstack/skills/poteto-mode/playbooks/orchestrate.md) is a reference for those general practices.
Keep the repository's existing guidance as its source of truth instead of copying generated projections into this guide.
This Qwen guide adds the observed runtime, permission, supervision and recovery limits; it does not require installing Butterworks or importing another harness's commands, model defaults or approval rules.
Use event-driven completion where verified, with read-only checks and a task-appropriate heartbeat fallback.
Treat borrowed cadence and retry settings as provisional until the relevant Qwen configuration has been observed.

## Starfleet references

[Model Preferences](../apps/model-preferences/README.md) describes exact model choices and the limits of saved profiles.
[Pattern Buffer](../apps/pattern-buffer/README.md) and its [OpenCode guide](../apps/pattern-buffer/docs/opencode.md) describe explicit context installation and exact task binding.
[Implementer dispatch](../apps/pattern-buffer/docs/implementer-dispatch.md) describes receipt and completion evidence boundaries.
[Warp-drive](../apps/warp-drive/README.md) describes local CPU reservation controls when those are part of an authorized workflow.
[Starfleet Markup](../apps/starfleet-markup/README.md) describes requested document reviews, exact ownership, foreground event reception and the current native wake limits.
