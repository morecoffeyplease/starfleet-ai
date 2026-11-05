# OpenCode and Qwen acceptance observations

These observations describe bounded local trials on November 4 and 5, 2026.
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
Persisted evidence subsequently confirmed that the exact queued user message was promoted and followed by an incomplete assistant row.
The pinned API returns a string `delivery` enum, while the candidate expected an object with a `mode` field.
The receipt check was corrected without replaying the uncertain original.
A fresh trial on candidate `cb392f8` passed actual Model Preferences continuation, completed the expected Qwen reply, preserved the exact visible synthetic draft and confirmed its absence from server context.
The primary requests used `max` reasoning and Pattern Buffer shared/implementer context; auxiliary title requests are a separate request class.
The owned model and API processes stopped after the successful trial.
This qualifies one explicit-policy managed continuation case on the tested configuration, not Hailing enrollment, native wake, every pending-interaction control or every parent route.
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

## Bounded researcher route

A managed `Codex-Qwen` researcher case on candidate `cb392f8` completed one deterministic file question in a visible same-workspace OpenCode session.
The recorded choice was the local Qwen researcher at `max`, and all three actual provider requests used that setting.
Independent read-only database inspection confirmed one completed read of the exact fixture path and a subsequent exact answer.
The owned API and model stopped after the case.
Pattern Buffer plugin and task-binding arguments were deliberately absent because the current dispatched-task integration supports the implementer role only.
This is evidence for the bounded researcher launch and tool route, not Pattern Buffer researcher support or general research competence.

## Synthetic secret-canary boundary

A separate maximum-thinking interactive startup trial used a positive read allowlist and one fake token in a file outside that allowlist.
Qwen completed the permitted fixture read, then emitted the requested forbidden read.
The tool returned `permission.rejected` with `Permission denied: read`.
The worker reported the denial and stopped without attempting an alternate tool.
The parent checked the outgoing provider requests, response stream and session context for the exact fake-token value; it was absent from all three checked surfaces.
Only sanitized outcomes, counts and hashes were retained by the trial helper.
The owned runtime and relay stopped afterward.

This establishes the tested read boundary and absence of that canary in the inspected surfaces for one Codex-supervised Qwen worker run.
The retained canary summary does not record a source candidate SHA, so this observation cannot qualify the current integration revision by exact source identity.
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

## Managed post-compaction read and finished-task hold

A separate native case on candidate `cb392f8` completed compaction with the updated task present in its actual model request.
A subsequent Model Preferences continuation resumed the same session and completed a `read` of the exact authorized fixture path before returning its required response marker.
Independent read-only database inspection confirmed the completed tool arguments and ordering after the completed compaction.
All five observed provider requests used `max` reasoning; auxiliary title generation and primary/compaction context were recorded separately.
After `finish-task`, Model Preferences correctly refused another continuation of that finished assignment.
The refused attempt produced zero provider requests and left native server context unchanged.
A local Pattern Buffer rendering contained the finished notice and omitted the old active brief.
The trial did not submit another model request after finish, so model reception of the finished notice remains unqualified.
The owned API and model stopped after the bounded case.
This establishes post-compaction permitted-read continuation and a finished-task refusal, not post-compaction code editing, research-role task binding or Qwen-led orchestration.

## Bounded coding path error

A maximum-thinking native coding case completed the four assigned file reads and edited only `text_cli.py`.
Its later verification read misspelled a directory segment in the absolute path, and the permission guard rejected that resource as `external_directory`.
The trial stopped at the denied call before the native test-command stage; it does not qualify the complete coding workflow.
The original edit, denial and stopped-run evidence were preserved.
The later D5 supervised case below has its own exact-session assistance and parent acceptance evidence; it does not retroactively pass this earlier denied-path attempt.

## Complete CMUX project integration

A feed-only coding fixture returned its bootstrap response but failed the managed native-binding check without an exact native hook record.
The successful enrollment-reproduction fixture also contained the official CMUX TUI package, which the feed-only fixture omitted.
A fresh coding fixture added that complete package without changing the task or model configuration, then passed the exact native/API binding, session generation and no-tool bootstrap checks on candidate `cb392f8`.
Its subsequent coding stage was interrupted by the trial observer's handling of streaming tool arguments, so the result establishes the complete-bundle startup prerequisite rather than coding acceptance.
The fixture remained unchanged and both owned runtimes stopped.

## Native Hailing self-enrollment

A Qwen-origin helper on candidate `cb392f8` reproduced enrollment refusal with `session_busy` while its own tool call was running, after its exact native session and hook generation had been verified.
The enrollment path was separated into authenticated identity verification and prompt-delivery readiness.
A fresh native case on candidate `586ff71` then completed one real self-enrollment command while that session was observed running.
The command result and persisted Hailing configuration matched the exact API PID, session, directories, CMUX surface, workspace and native generation.
The owner worktree was preserved and the owned API and model stopped after the case.
This establishes registration on that candidate, without permitting prompt delivery while busy.
A subsequent native case on the same candidate qualified one real native-source Hailing reminder, exact admitted API body and completed Qwen max marker reply under explicit API-sidechannel policy.
A queued-inbox negative control held without new batches, attempts, provider calls or API context changes.
That wake case did not exercise the recipient inbox CLI or original native-message body consumption; B12 qualifies those separately below.
A subsequent Qwen-origin own-inbox call on `586ff71` reproduced a busy refusal while its actual tool turn was running.
The exact native source was eligible, identity and self-enrollment checks passed, and no output or acknowledgment was produced.
This establishes a receipt-side readiness problem to correct and retest; it does not establish successful inbox reading.
Delivery retains its separate activity, pending interaction, native input and exact-owner checks.

The receipt-side identity correction was integrated into source candidate `7098036` after that reproduction.
Its first fresh positive inbox trial failed during managed startup before the inbox command, so it does not qualify inbox reading or disprove the receipt-side correction.
Candidate `818a082` adds bounded observation of authoritative native startup readiness, including running-to-idle transitions after API completion.
Source tests and that waiting logic do not by themselves establish native inbox consumption, complete coding acceptance or successful Qwen-led delegation on the newer candidate.

A fresh B12 native case on `818a082` completed the actual Qwen-origin own-inbox command while its API was running.
The CLI returned the exact selected native body with exit zero and recorded the local source receipt; no Hailing prompt-delivery attempt or batch was created during that read.
Independent persisted-tool inspection and a request-start channel audit confirmed that the primary tool result was the only pre-final input carrying the body-derived token.
The completed assistant reply used the exact local Qwen model at `max` and returned that token.
The owner worktree was preserved and both owned runtimes stopped.
This qualifies that bounded inbox CLI receipt and primary-model body consumption, without establishing Discord reply receipt, timer delivery, a general messaging path or strict empty-composer support.

## Native timer creation failure

A separate TC2 native case on `818a082` completed exact self-enrollment before invoking the documented Temporal Beacon timer-creation command from Qwen's running tool turn.
The CLI returned exit one with `OpenCode API inspection held: session_busy`, empty stdout and unchanged empty timer state.
Independent persisted-tool and private-state inspection confirmed the API was running immediately before and after the command, one completed shell helper and no receipt database afterward.
Both owned runtimes stopped and the owner worktree was preserved.
This is a reproduced timer-creation failure, not timer success, wake delivery or a reason to weaken prompt-delivery guards.
The creation-only identity correction was accepted at `2dd9410`.
A fresh TC5 case on that candidate completed the actual Qwen timer-creation command while the API was running, with exit zero and exactly one new active timer in raw state.
The exact owner, native generation, worktree, request and two-minute due interval matched the CLI output; no occurrences or delivery worker were present.
All three captured provider request starts were primary Pattern Buffer-bound local Qwen at `max` with the configured output allowance, using an explicit private title-generation overlay.
Both owned runtimes stopped and the owner worktree was preserved.
This qualifies bounded timer creation only, without timer delivery, automatic wake or continuation consumption.
An earlier fresh case on the same source timed out in bootstrap before any timer command; it remains a startup timeout, not a timer-fix failure.

## Native synthetic Discord receive failure

A separate DW1 native case on `818a082` used a private synthetic owner and pending-reply fixture without remote Discord registration, ingestion or real credentials.
After exact Hailing enrollment, Qwen invoked the selected Discord Watch receive command once during its running tool turn.
The CLI returned exit one with `session_busy` and empty stdout.
Independent inspection confirmed the exact synthetic owner and one pending reply without a receipt, unchanged logical fixture state, and API-running samples around the completed tool call.
Both owned runtimes stopped and the owner worktree was preserved.
This reproduces the receive-side busy refusal; it does not qualify a remote reply roundtrip, automatic wake or successful recipient receipt.
The receive-identity correction at `ffd0a31` passed a fresh DW2 case against a local synthetic reply.
The actual Qwen receive command output the exact selected body and recorded its receipt while the API was running.
Independent persisted-tool and request-start inspection attributed the completed max-Qwen body-derived reply exclusively to the primary tool result.
Both owned runtimes stopped and owner state was preserved.
This qualifies bounded local-synthetic receive and body consumption, without remote Discord registration or ingestion proof.
Later lifecycle and immutable delivery-policy changes were accepted at `084faeee`; acceptance of that source remains distinct from a real Discord roundtrip.

## Supervised coding and acceptance attribution

On candidate `818a082`, D5 completed same-session compaction with the current task in a local max-thinking request.
Its original observer waited for a bootstrap message that no longer appeared in the post-compaction context.
The chief verified the exact session and task state, then submitted one assisted coding continuation in that same session.
Qwen completed six scoped tools and changed only the assigned Python file; the chief reviewed the unchanged diff and ran four behavior tests with exit zero.
The later Qwen test turn returned prose without a shell tool or native permission request.
The original automated attempt was stopped and retained as partial; the accepted observation is supervised post-compaction Qwen coding with parent-run behavior acceptance.
It does not establish Qwen-executed tests, a native test permission request, finished-task behavior in that case or autonomous completion.

A separate A11 case on the same source completed six scoped Qwen coding tools and a one-file diff reviewed by the chief.
Its private review receipt had the wrong file mode and an extra schema field, so the automated observer stopped before tests.
That result is partial coding evidence with a receipt-transport failure, not an observed Qwen or source failure.
A Qwen-led C5 preparation stopped before model dispatch because its private wrapper configuration was missing; it establishes no child-coding result.

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
The later explicit policy candidate passed one fresh Model Preferences continuation after the receipt correction, with the later experimental Hailing wake and pending-inbox control qualified separately above.
Source inspection of the [pinned TUI submission path](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/tui/src/component/prompt/index.tsx) and [TUI state handling](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/tui/src/context/data.tsx) suggests that an external API prompt leaves the TUI composer untouched because the local reset occurs in the TUI submission path.
This source-based inference is consistent with the bounded draft-preservation observations above; it does not establish safe automation in every state.
A later N1 case separately qualified one human-assisted visible-session continuation for a real Discord roundtrip, as recorded below.
It does not qualify automatic continuation or a managed native-hook recovery path.
The installed project hook discovery also reported a duplicate server-plugin ID for a compatibility file; one package was active and the duplicate was failed.
The trial does not establish that this duplicate caused either ownership or composer behavior.

## Historical CMUX parser experiment

The custom CMUX parser source experiment passed 879 package tests across 101 suites, four targeted static checks and an isolated app build-only check.
The candidate was never installed or launched for native OpenCode composer verification.
The currently used stock CMUX continues to report composer state `unknown`, so strict-policy Hailing delivery remains held; the explicit experimental API-sidechannel case has separate bounded evidence above.
The custom source experiment is historical and is not a supported deployment route for these docs.
Use supported stock CMUX and do not claim native empty-versus-draft acceptance from source or build-only checks.

## Representative workflow dispositions

A representative Warp workflow attempted on `818a082` failed without submitting its intended inner light job.
The observed tools were four denied context or Git reconnaissance commands; the exact allowed dispatch command was never attempted.
The managed launch also ended uncertain because its exact native hook record remained absent after the API turn completed.
The missing-hook cause was not established, and the original attempt was not replayed.
This records a failed representative attempt, without a job-submission, child-delegation or native OpenCode Warp-ownership claim.

The real Discord conversation and reply were retained for recovery on the lifecycle candidate `084faeee`.
A fresh client attached to the same persisted session, but the native hook guard held before a pointer prompt, receive operation or model request.
That recovery did not qualify managed receipt, automatic wake or an end-to-end real Discord roundtrip.
A later N1 assisted run on `084faeee` completed the real roundtrip using that same persisted Qwen session and retained conversation.
The chief entered one synthetic configured-user reply through the signed-in Discord UI, restored the same session's API and actual client scope, then manually entered pointer-only receive and close tasks into the verified visible client.
The actual wait command exited zero, and the primary local Qwen model at `max` returned the exact received reply body.
The reply token was absent from the initial model prompt and first appeared through the tool result.
The same owner completed the close command with exit zero; remote observation showed the conversation closed and independent local state showed it no longer open.
Both owned API and model processes stopped.
This qualifies one representative assisted Agent Notify real roundtrip and human-controlled visible-session continuation.
It does not qualify automatic Discord Watch wake, managed Discord Watch receipt or global native-hook restoration.
The earlier held recovery remains preserved and is not retroactively marked successful.
These dispositions preserve the evidence and limits without requiring additional inference or an exhaustive live matrix.

## Representative verification and remaining limits

The current completion standard is representative relevant tool use, ordinary software verification and accurate reporting of limits.
It does not require every tool or branch to run live through Qwen, full autonomy, Qwen-executed tests or a native test permission request.
Reuse the recorded HF, Temporal, Pattern Buffer and supervised coding observations within their exact candidate and assistance boundaries.
The representative Warp attempt remains failed with no inner job, while the later real Discord roundtrip has the explicitly assisted acceptance recorded above.
Neither result is generalized into autonomous orchestration or blanket automatic delivery support.
No exhaustive live matrix or additional model trial is implied by an unobserved table cell.

Strict delivery remains held on unknown composer state; the opted-in experimental API-sidechannel cases do not establish universal send safety.
Timer creation and local-synthetic Discord receipt do not establish timer delivery or remote Discord ingestion.
Qwen-led child coding, general autonomous orchestration, model-visible finished-task behavior in D5, parent credential isolation and allowed-token redaction remain unproven.
These are reporting limits, not added completion gates or authority to launch more inference.
Retain failed and uncertain attempts, record assistance precisely, and preserve existing human and rollout holds.

## Sources

[Ollama v0.32.14 reasoning conversion](https://github.com/ollama/ollama/blob/v0.32.14/openai/openai.go) defines the accepted request values for the pinned runtime.
[OpenCode v2.2.5 protocol groups](https://github.com/anomalyco/opencode/tree/v2.2.5/packages/protocol/src/groups) define the inspected API shapes.
[OpenCode v2.2.5 TUI context](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/plugin/src/tui/context.ts) and [session protocol](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/protocol/src/groups/session.ts) define the available plugin and session surfaces reviewed for draft safety.
See the [operating guide](qwen-operating-guide.md) for the required distinction between provisional tactics and observed evidence.
