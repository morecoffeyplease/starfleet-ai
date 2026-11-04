# OpenCode and Qwen acceptance observations

These observations describe bounded local trials on November 4, 2026.
They are not a benchmark, security certification or a claim that every Starfleet component supports OpenCode.
Private connection data, transcripts and native identifiers are excluded.
See the [setup guide](opencode-qwen-setup.md) for a portable summary of the observed runtime configuration and prerequisites.

## Configuration

The read-only adapter source candidate was `7de9198`.
The guide and adapter integration baseline was `ef44481`.
The runtime was OpenCode 2.2.5 and Ollama 0.32.14 with local `qwen3.6:35b`, GGUF Q4_K_M, on an Apple M5 Max with 128 GB unified memory.
The inference pilot configured a 131,072-token context and a 32,768-token primary output allowance.
Ollama reported thinking and tool capabilities but did not expose named thinking-level metadata through this version's model-show response.
Its tagged OpenAI-compatible implementation accepts `reasoning_effort: "max"`; the trial observed that exact outgoing value and nonempty reasoning output.
No claim is made that a larger named model-specific level exists.

## Read-only API contract

The real server returned the expected version and process PID, exact OpenCode session ID and directory, an inactive session and empty pending permission/form lists.
The adapter returned clear for those API-visible checks.
Seventeen synthetic adapter tests also passed independently on candidate `7de9198`.
The native check establishes compatibility of these read-only routes and response shapes for the tested configuration.
It does not independently authenticate the caller-supplied CMUX association or prove a safe message-send state.

## Model Preferences preflight

Candidate `2dda6cc` passed the component verifier with 90 tests, including synthetic OpenCode API and CMUX boundary tests.
A separate real read-only preflight against the owned OpenCode 2.2.5 server confirmed the authenticated API identity, exact local provider and model, the `max` variant and a 32,768-token output allowance.
That check did not create a session, launch a model or qualify managed dispatch.
Native CMUX returns uppercase UUID strings while the initial launcher normalized expected UUIDs to lowercase.
Candidate `d193b9e` normalizes those UUIDs semantically while preserving exact session IDs and refusing malformed UUIDs.
Its focused OpenCode test suite passed 11 tests; managed native launch qualification is still separate.

## Managed-dispatch attempts

An earlier managed attempt stopped at the parent guard before sending a session message or message part.
The Codex/Claude parent-routing regression was corrected, and its focused suite passed 25 tests while preserving the stricter OpenCode-parent contract.
The following native attempt created its API session and same-workspace tab but stopped before the child handshake and any model message.
A harmless startup-command control executed at 344 bytes but did not execute at 2,142 bytes, where the visible command was truncated.
The short-command and XDG isolation changes passed 32 focused tests.
A subsequent Codex-Qwen managed session on candidate `3c1e189` completed its exact startup task in a visible same-workspace tab with the selected `max` variant.
Model Preferences recorded the admitted user message and completed assistant turn, and the new private TUI state contained the expected session ID.
Its private descriptor forwarded only the four explicit XDG home paths and retained mode 0600.
A separate experimental API follow-up in that managed session produced the expected reply while the synthetic unsent draft stayed visible and intact, and the draft did not enter the server transcript.
The observed provider requests used maximum thinking, and shared and implementer Pattern Buffer context was present in the primary calls.
The owned API and model processes stopped after the bounded test.
Model Preferences still reported delivery held because native composer state was not proven empty; the experiment did not remove that production guard.
This qualifies the tested fresh managed startup and experimental draft-preservation cases, not Hailing wake reception, every parent route, negative delivery control or general Qwen-only orchestration.
No uncertain session or prompt was replayed.

## Explicit API-sidechannel candidate

Candidate `bde0ee5` adds an explicit, identity-bound API-sidechannel policy while keeping the strict composer policy as the default.
Its first actual Model Preferences continuation attempt followed successful managed startup with `max` reasoning and Pattern Buffer context.
An unsent synthetic draft was observed in the exact new tab before continuation.
The continuation stopped with `prompt_queue_admission_unconfirmed`; the attempt was preserved without replay and the owned runtimes stopped.
This is a failed acceptance result, not proof that the prompt was rejected or delivered.
Persisted admission and message evidence must be reconciled before another send.
A separate actual API control on candidate `792105b` observed a clear empty inbox followed by a `pending_inbox` hold after a synthetic queued message.
That control used no model request and does not establish native wake delivery.

## Visible-session API draft-preservation control

A separate short OpenCode session was started directly in a visible tab and was not created through Model Preferences managed dispatch.
The startup prompt returned its expected baseline response.
While an unsent synthetic draft remained visible, one API prompt reached the same session and produced a reply.
Direct before-and-after screen inspection showed the synthetic draft remained visible and intact.
The two provider requests carried `reasoning_effort: "max"` and Pattern Buffer shared and implementer context without the orchestrator marker.
No tools were invoked, and the provider transcript did not contain the synthetic draft text.
The isolated API and model processes were stopped after the bounded trial.
This is limited evidence that an API prompt preserved one visible synthetic draft on the tested candidate.
This direct control by itself does not qualify managed launch, exact parent routing, Hailing delivery, wake, pending-permission handling or an API-only safe-send path.
Current product guards remain unchanged.

Separate API safety controls observed a Hailing preflight hold for a pending form and a clear preflight result in a queued-inbox red-control case.
A task using an explicitly configured agent and an allowed fixture produced a pending permission, and the Hailing preflight held on `pending_permissions`.
An earlier denied-action control was caused by a missing configured agent under the pinned source behavior, not a wrong API route.
These controls do not prove that OpenCode presented an approval to a person or qualify all permission and message states.

## Detached-helper authentication control

An initial managed-launch pilot on candidate `ad736a6` stopped during isolated profile notification inventory before Qwen session creation or model loading.
The helper had removed CMUX's inherited terminal capability from its environment, and the detached process no longer qualified through native process ancestry.
The owned API process stopped afterward.

A separate read-only comparison in one detached helper held the command and environment constant except for preservation of its own native-issued `CMUX_SOCKET_CAPABILITY`.
Without that capability, `cmux identify` returned access denied; with it, the command returned an actual caller identity.
No capability value was printed or placed in arguments or receipts.
This proves the tested authentication requirement for the detached helper, not managed Qwen delivery.
A managed child must obtain its own new tab's native capability and scope any necessary server-side tool access to that exact session.
Copying the parent capability into the shared server environment would not establish child ownership.

## Maximum-thinking startup and instruction reception

An interactive OpenCode startup prompt requested one allowed fixture read and two known markers.
The fixture remained unchanged, the real read tool completed, both expected markers appeared in the final answer, and the model reached idle.
The primary requests carried `reasoning_effort: "max"` and `max_tokens: 32768`.
The observed provider stream included reasoning output; only counts and sanitized acceptance results were retained in trial evidence.
The owned model runtime and capture relay stopped after the run.

OpenCode also issued an auxiliary title request using the same local model and thinking setting.
That request omitted an explicit output-token field and accounted for 1,987 completion tokens in this one run.
The entire bounded run took about 42 seconds, including model loading and the auxiliary request.
These timings are one observation, not a throughput estimate or a basis for a universal heartbeat interval.
Task budgets should account for auxiliary model calls as well as the worker's visible tool loop.

## Five-marker task-output criterion

A separate maximum-thinking task requested a real allowed fixture read and a final report containing five known markers.
The request was admitted, the expected fixture was read, and Pattern Buffer context was present in the request.
The final report named only the fixture marker and omitted the other four requested markers, so the exact reporting criterion failed.
A separate one-output control has its own scope and does not change this failed result.
This observation separates mechanical context delivery from final-report completeness for one task.
It does not establish a general Qwen limitation, a universal marker-count limit, a context budget or a task-size rule.

## Synthetic secret-canary boundary

A separate maximum-thinking interactive startup trial used a positive read allowlist and one fake token in a file outside that allowlist.
Qwen completed the permitted fixture read, then emitted the requested forbidden read.
The tool returned `permission.rejected` with `Permission denied: read`.
The worker reported the denial and stopped without attempting an alternate tool.
The parent checked the outgoing provider requests, response stream and session context for the exact fake-token value; it was absent from all three checked surfaces.
Only sanitized outcomes, counts and hashes were retained by the trial helper.
The owned runtime and relay stopped afterward.

This establishes the tested read boundary and absence of that canary in the inspected surfaces for one Codex-supervised Qwen worker run.
It does not prove isolation from unrestricted shell commands, arbitrary filesystem access, other processes or real credentials.
The fixture used no real secrets and no real-secret discovery.
Redaction after an explicitly allowed synthetic-token read, parent credential handling and Qwen-only orchestrator secret handling still require separate acceptance.

## Pattern Buffer native context and compaction

The first native Pattern Buffer source candidate was `569e48b`.
The plugin was installed only into an explicit isolated OpenCode settings file and private trial runtime.
A two-phase prepare/bind operation attached one immutable task receipt to an actual API-created OpenCode session before its interactive startup prompt.

The outgoing primary requests contained the approved shared marker, implementer marker and exact first-task marker.
They did not contain the orchestrator marker.
Qwen completed the allowed read and reported the expected markers.
After an explicit task update, a real manual compaction request contained the new task marker and omitted the old task marker from its current system context.
The event stream recorded `session.compaction.started` and `session.compaction.ended` for that exact session.
This proves initial context delivery and current-task delivery into the tested compaction call, not a subsequent resumed implementation turn.
A local render after finishing the task omitted the active brief; model-visible finish behavior still needs a separate continuation check.
The owned model runtime and relay stopped after the bounded run.

A separate native negative control bound a fresh session and then temporarily made its synthetic owner context unreadable as UTF-8.
The interactive startup recorded the Pattern Buffer context failure, reached idle and made zero requests to the capture-provider endpoint.
No model runtime was started for that control.
The original synthetic context bytes were restored exactly afterward.
This establishes that the tested hook failure prevented provider invocation for this candidate, rather than relying on an instruction telling the model to stop.

## Native ownership and composer gaps

Passively opening an API-created session did not initially register a native CMUX agent identity.
A subsequent actual startup task produced OpenCode lifecycle events and a native session record.
With two interactive clients attached to the same API/project, the native projection associated the session with the earlier passive tab rather than the newly opened startup tab.
A managed launcher must verify the exact intended surface and refuse a mismatched projection.
A matching session somewhere in the workspace is insufficient.

After the task completed, the native input-state API reported an OpenCode agent and idle lifecycle but composer state `unknown`.
An inactive model and empty permission/form lists do not establish that a user has no draft.
Guarded automatic delivery therefore remains held on this observed configuration.
Do not reinterpret unknown as empty, type a probe, clear a draft or synthesize hook records to make enrollment succeed.
The pinned OpenCode 2.2.5 TUI plugin context and session API references have no documented draft accessor or atomic admission lock.
The inspected server routes expose active-turn, inbox, permission and form state but do not expose unsent composer text.
The initial API-first experiment checked exact native ownership and preserved one synthetic draft, as recorded above.
The later explicit policy candidate remains under qualification after its uncertain-admission result.
Source inspection of the [pinned TUI submission path](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/tui/src/component/prompt/index.tsx) and [TUI state handling](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/tui/src/context/data.tsx) suggests that an external API prompt leaves the TUI composer untouched because the local reset occurs in the TUI submission path.
This source-based inference is consistent with the bounded draft-preservation observations above; it does not establish safe automation in every state.
A human-controlled visible-session resume remains an unproven alternative and is not the selected experiment.
The installed project hook discovery also reported a duplicate server-plugin ID for a compatibility file; one package was active and the duplicate was failed.
The trial does not establish that this duplicate caused either ownership or composer behavior.

## Historical CMUX parser experiment

The custom CMUX parser source experiment passed 879 package tests across 101 suites, four targeted static checks and an isolated app build-only check.
The candidate was never installed or launched for native OpenCode composer verification.
The currently used stock CMUX continues to report composer state `unknown`, so Hailing Frequencies must keep OpenCode delivery held.
The custom source experiment is historical and is not a supported deployment route for these docs.
Use supported stock CMUX and do not claim native empty-versus-draft acceptance from source or build-only checks.

## Remaining acceptance

Managed profile dispatch and exact continuation require their own native acceptance.
Pattern Buffer initial shared, role and exact-task delivery and an updated task in completed compaction are observed above.
A post-compaction implementation turn, same-session resume and model-visible task-finish behavior still require native acceptance.
Automatic OpenCode inbound delivery and continuation remain held while stock CMUX reports composer state `unknown`.
No API-only send or continuation bypass is qualified.
OpenCode API and plugin integration remain under investigation for safe ownership and delivery behavior.
API-sidechannel qualification remains a bounded investigation; the default strict policy is unchanged.
Guarded messaging still needs authoritative empty-versus-draft evidence, exact owner binding, actual wake receipt and held-permission/draft controls.
A human-controlled visible-session resume is an unproven alternative, not the selected experiment.
The remaining synthetic-secret checks are parent credential handling, allowed-token redaction and Qwen-only orchestration.
Qwen-led orchestration with human or frontier-model assistance requires separate task-framing, actual worker delegation, dependency-relay, acceptance-reporting and escalation evidence.
Record interventions without treating their presence as failure of the supervised workflow or claiming autonomous behavior.
The earlier Codex-supervised worker results cannot establish those capabilities.
The active Model Preferences integration work still needs fresh managed-session, explicit-maximum-variant, post-compaction continuation and failure-control evidence before its source candidate can be described as accepted.

## Sources

[Ollama v0.32.14 reasoning conversion](https://github.com/ollama/ollama/blob/v0.32.14/openai/openai.go) defines the accepted request values for the pinned runtime.
[OpenCode v2.2.5 protocol groups](https://github.com/anomalyco/opencode/tree/v2.2.5/packages/protocol/src/groups) define the inspected API shapes.
[OpenCode v2.2.5 TUI context](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/plugin/src/tui/context.ts) and [session protocol](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/protocol/src/groups/session.ts) define the available plugin and session surfaces reviewed for draft safety.
See the [operating guide](qwen-operating-guide.md) for the required distinction between provisional tactics and observed evidence.
