# OpenCode and Qwen acceptance observations

These observations describe bounded local trials on November 4, 2026.
They are not a benchmark, security certification or a claim that every Starfleet component supports OpenCode.
Private connection data, transcripts and native identifiers are excluded.

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
The current native-binding comparison needs semantic UUID normalization before a managed launch can be qualified; case differences must not weaken exact ownership checks.

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
The installed project hook discovery also reported a duplicate server-plugin ID for a compatibility file; one package was active and the duplicate was failed.
The trial does not establish that this duplicate caused either ownership or composer behavior.

## Remaining acceptance

Managed profile dispatch and exact continuation require their own native acceptance.
Pattern Buffer initial shared, role and exact-task delivery and an updated task in completed compaction are observed above.
A post-compaction implementation turn, same-session resume and model-visible task-finish behavior still require native acceptance.
Guarded messaging needs authoritative empty-versus-draft evidence, exact owner binding, actual wake receipt and held-permission/draft controls.
The remaining synthetic-secret checks are parent credential handling, allowed-token redaction and Qwen-only orchestration.
Qwen-only orchestration requires separate task-framing, dependency-relay, acceptance-reporting and escalation evidence.
The earlier Codex-supervised worker results cannot establish those capabilities.

## Sources

[Ollama v0.32.14 reasoning conversion](https://github.com/ollama/ollama/blob/v0.32.14/openai/openai.go) defines the accepted request values for the pinned runtime.
[OpenCode v2.2.5 protocol groups](https://github.com/anomalyco/opencode/tree/v2.2.5/packages/protocol/src/groups) define the inspected API shapes.
See the [operating guide](qwen-operating-guide.md) for the required distinction between provisional tactics and observed evidence.
