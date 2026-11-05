# OpenCode and local Qwen setup

This guide records the observed pilot configuration for OpenCode 2.2.5, Ollama 0.32.14 and `qwen3.6:35b`.
It is a setup recipe for controlled local trials, not a claim that every Starfleet component or workflow is supported.
The model was tested as GGUF Q4_K_M with a 131,072-token context, 32,768-token output allowance and the maximum reasoning setting exposed by this provider.
The observations came from one Apple M5 Max host with 128 GB of unified memory and are not a performance recommendation.
Clean-host installation, clean-host model download and the full end-to-end Starfleet orchestration path have not been qualified.
Use the [Qwen runtime lifecycle runbook](qwen-runtime-lifecycle.md) for the source-checked private runtime, profile, managed dispatch, worker scaling, and owned shutdown command sequence.
The current candidate includes source implementations for the opt-in Pattern Buffer pressure reminder and Policy Guard OpenCode Prime Directive plugin.
Their native hook loading, warning delivery, permissions and covered action outcomes remain unqualified, so the runbook retains its native acceptance prerequisite.

## Prerequisites

Use a visible OpenCode tab or pane inside the existing CMUX workspace assigned to this task.
Use a supported stock CMUX release for native session and composer state.
The earlier custom CMUX parser experiment is historical and is not a required or supported deployment path.

From the assigned worktree, install the supported project integration with `cmux hooks opencode install --project`.
The tested integration used the complete generated bundle under `.opencode/plugins`: `cmux-feed.js` and `cmux/{.cmux-owned,index.js,package.json,tui.js}`.
The feed bridge alone does not provide the TUI session lifecycle hooks; the package exports its TUI adapter separately.
Keep generated plugin files intact and recheck the installed CMUX release's supported layout when upgrading.
Before treating a launch as ready, verify current native hook evidence for its exact OpenCode session, CMUX surface, workspace, worktree and generation.
Bundle presence alone does not prove a live binding.

Keep the task brief, worktree, native OpenCode session, CMUX surface and CMUX workspace identity aligned.
Use an isolated Git worktree for each bounded task and assign one writer to each changed path.
Use the exact provider ID and model slug in the task card, then verify OpenCode shows that same route before work starts.
Do not configure an alternate provider or model as an automatic fallback for a local-only run.

The observed runtime versions were OpenCode 2.2.5 and Ollama 0.32.14.
The model identifier was `qwen3.6:35b` with quantization GGUF Q4_K_M.
The trial selected `reasoning_effort: "max"` and observed nonempty reasoning output on primary model requests.
Ollama did not expose named thinking-level metadata through the tested model-show response.
Do not infer that this model has a higher named reasoning setting than the one that was observed.

Check an existing installation with read-only commands before preparing a task.

```sh
opencode --version
ollama --version
ollama list
ollama show qwen3.6:35b
```

The pilot did not validate installation or download on a clean host.
Use the [OpenCode v2 configuration guide](https://dev.opencode.ai/v2/docs/config/), [OpenCode v2 provider guide](https://dev.opencode.ai/v2/docs/providers/) and [Ollama 0.32.14 OpenAI compatibility guide](https://github.com/ollama/ollama/blob/v0.32.14/docs/api/openai-compatibility.mdx) for setup details, then compare the selected settings with the observed configuration below.
The Ollama 0.32.14 guide documents `http://localhost:11434/v1/` as its local OpenAI-compatible endpoint.
Confirm the actual local listener and keep it bound to loopback for this workflow.
The pilot's private capture endpoint is excluded, and a fresh direct connection to this endpoint has not been qualified.

The provider context limit describes the harness budget; it does not configure Ollama's runtime context by itself.
The observed owned runtime also set `OLLAMA_CONTEXT_LENGTH=131072`, `OLLAMA_KEEP_ALIVE=0`, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NO_CLOUD=1` and `OLLAMA_NOPRUNE=1`.
Match those settings in a dedicated temporary loopback runtime with an available port, or verify an already owned runtime's effective settings before use.

## Isolated OpenCode profile

Create a dedicated OpenCode configuration for the pilot rather than changing the active profile used by other work.
The tested v2 configuration uses top-level `providers`, `agents` and a `default_agent` entry.
Its permission rules are objects with `action`, `resource` and `effect` fields in an array under the selected agent's `permissions`.
These are v2 configuration names observed in OpenCode 2.2.5 and differ from older singular v1 fields.
Treat this excerpt as a field guide, then validate the final profile with the pinned runtime before using it.

The following excerpt shows the tested field shape and values that mattered for this model.
Replace the local display labels and assigned file names with the exact values for the task.
The permission list is an example boundary and does not turn unrestricted shell access into a filesystem sandbox.
The `edit` allowance illustrates where a task-scoped rule belongs, but that exact edit permission was not covered by the recorded native canary.

```json
{
  "update": "disable",
  "model": "trial-local/qwen3.6:35b",
  "default_agent": "qwen-implementer",
  "compaction": { "auto": false },
  "warming": false,
  "providers": {
    "trial-local": {
      "name": "Local Qwen",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": { "baseURL": "http://127.0.0.1:11434/v1" },
      "models": {
        "qwen3.6:35b": {
          "name": "Qwen3.6 35B",
          "limit": { "context": 131072, "output": 32768 },
          "capabilities": {
            "tools": true,
            "reasoning": { "supported": true, "effort": ["max"] }
          },
          "compatibility": { "maxTokensField": "max_tokens" },
          "body": { "reasoning_effort": "max" },
          "variants": [
            {
              "id": "max",
              "settings": { "reasoningEffort": "max" },
              "body": { "reasoning_effort": "max" }
            }
          ]
        }
      }
    }
  },
  "agents": {
    "qwen-implementer": {
      "description": "Implement one bounded task in the assigned worktree",
      "permissions": [
        { "action": "*", "resource": "*", "effect": "deny" },
        { "action": "read", "resource": "AGENTS.md", "effect": "allow" },
        { "action": "read", "resource": "synthetic-input.txt", "effect": "allow" },
        { "action": "edit", "resource": "src/assigned-file.py", "effect": "allow" }
      ]
    }
  }
}
```

The runtime trial used the local provider ID `trial-local` and model slug `qwen3.6:35b`.
The context and output limits are separate values, and the maximum reasoning value was included in the outgoing request.
Use only the protected credentials for the fresh pilot API server, and obtain a new tab's own native CMUX capability rather than reusing the parent's.
OpenCode 2.2.5 Basic authentication applies to the owned API server, so clients sharing that server are in the same API trust domain; it is not a separate credential boundary for each child session.
Keep any local API password in a user-owned mode-0600 file and pass it only through the supported private connection mechanism.
Never place credentials in provider URLs, command arguments, task files, shared context, receipts, logs, transcripts or commits.

Permission entries control matching OpenCode actions but do not impose operating-system isolation.
Use exact task-scoped file access and narrowly scoped shell commands, or run the worker in real process isolation.
Do not provide a worker with unrestricted shell access while describing a denied file-read rule as a security boundary.
Keep hosted providers, cloud integrations and fallback routes disabled in the isolated local-only profile.
Verify the active model and inspect the actual request destination before accepting the run as local-only.
These checks are operator requirements unless a separate technical control enforces them.

## Instructions and task context

OpenCode v2 discovers actual `AGENTS.md` files based on the workspace and files explored during the session.
It does not treat a task card's mention of an instruction file as proof that the model received that file.
Use the actual repository `AGENTS.md` at the worktree root for project-wide rules and place narrower `AGENTS.md` files under the directory they govern.
For additional persistent instruction sources, configure the documented `instructions` array explicitly and verify the source is active.
OpenCode v2 recognizes `AGENTS.md` and does not use `CLAUDE.md` as its fallback instruction file.
It combines instruction sources rather than resolving conflicts between them, so keep the sources consistent and escalate an unresolved conflict to the parent.
See the official [OpenCode v2 instruction guide](https://dev.opencode.ai/v2/docs/instructions/) for discovery scope and ordering.

Pass a one-task brief with one bounded objective, exclusive file ownership, dependencies, exact acceptance conditions, commands, timebox, checkpoints and stop conditions.
The orchestrator must relay required upstream findings into the brief because the child does not inherit sibling conversations or hidden coordinator state.
Keep the final response and evidence budget available when selecting the context and output limits.
Default every Qwen run to the highest thinking setting actually supported by its selected model and runtime, and reserve output space for acceptance evidence and the report.
A different thinking budget requires an explicit operator decision.

## Manual local trial flow

1. Confirm the exact OpenCode and Ollama versions and the presence of the chosen local model.
2. Confirm the local provider URL is loopback-only and that no hosted provider or fallback route is active.
3. Create an isolated worktree and a task card naming the exact writable paths and acceptance command.
4. Open OpenCode visibly in the existing CMUX workspace for the same task.
5. Confirm the active agent, provider ID, model slug, reasoning setting and output allowance in the visible session.
6. Start with one bounded synthetic task and record the candidate revision before the first edit.
7. Observe actual tool permissions, provider requests, result artifacts and final report before accepting the task.
8. Preserve uncertain results until the session, worktree and candidate are reconciled.

The OpenCode non-interactive CLI rejected an ask interaction in one tested OpenCode 2.2.5 run.
Treat this as an observed version-specific behavior rather than a guarantee about every release or tool.
Keep approvals visible to the operator in the OpenCode tab or pane and stop at an approval hold.
Do not force a prompt, resume, kill or retry solely because the model is quiet.

## Starfleet component setup and current limits

Read the maintained [Model Preferences guide](../apps/model-preferences/README.md) for exact provider selection and its API preflight contract.
Its OpenCode-managed dispatch path requires a pilot-scoped OpenCode configuration, a pre-existing API server authenticated on loopback and an owner-only private connection manifest.
Use a fresh per-task connection manifest for the exact loopback API process.
The OpenCode management API is separate from the Ollama inference endpoint, and its Basic authentication value must remain in a protected private file.
The current managed TUI source sets that value in the TUI process environment, but whether tool subprocesses can read it remains untested; do not infer either exposure or isolation without process-boundary evidence.
Source code and a successful read-only preflight do not establish managed session creation, startup placement, continuation or safe sending.
The parent-routing and startup-command failures were corrected, and a fresh Codex-Qwen managed session completed its exact baseline task using the short protected descriptor and explicit private XDG paths.
This observed startup case does not qualify every parent route, Hailing delivery or a production API-only send path.
The after-fix API replay on `9092aef` exercised parent-linked session creation and distinct-worktree relocation with zero model requests; it is metadata-only API evidence, not native startup or inference qualification.

The researcher route has one bounded native read-and-answer case without Pattern Buffer plugin or task-binding arguments.
Current Pattern Buffer dispatched-task integration supports implementers only; keep researcher configuration separate and do not infer researcher task context from implementer evidence.

Read the maintained [Pattern Buffer OpenCode guide](../apps/pattern-buffer/docs/opencode.md) for explicit plugin installation, two-phase task binding and exact session identity.
The same guide documents opt-in orchestrator checkpoint enrollment, successful tool-completion publication, recovery instructions and uncertain compaction observations.
OpenCode pressure warnings are opt-in and require an imported descriptor for the exact session, provider, model, variant, agent, usable context window and active compaction settings.
The warning uses a lower anchor from last-measured assistant token usage, not exact current occupancy or guaranteed headroom.
Native descriptor evidence and warning delivery remain unqualified for this candidate.
The context callback runs after the v2.2.5 automatic compaction decision and is not a supported advance-warning point.
The native pilot observed shared context, the implementer role and initial task text in a primary request, an updated task in completed compaction, and a broken-bound-context failure that resulted in zero provider requests.
A separate managed case resumed a permitted fixture read after compaction and correctly held continuation after task finish without another provider request.
A later supervised case completed a same-session post-compaction edit; the chief reviewed its unchanged diff and ran four passing behavior tests.
Qwen itself did not run those tests or issue a native test permission request, and model reception of the finished notice remains unqualified.
The Pattern Buffer source now includes opt-in lower-anchor pressure reminders and checkpoint and compaction lifecycle accounting.
Its native plugin loading, same-session usage descriptor, recovery behavior and warning delivery remain unqualified, so context and task reinjection observations do not satisfy that readiness requirement.

Hailing Frequencies currently holds OpenCode delivery while the installed CMUX projection reports composer state `unknown`.
The historical CMUX parser experiment passed its package and build-only checks but was never installed or launched for a native empty-composer check.
Do not build or install a custom CMUX fork for this workflow.
Keep automatic OpenCode inbound prompts and continuation held while composer state is unknown.
The OpenCode 2.2.5 TUI plugin API has no documented draft accessor or atomic admission lock.
Pinned source shows the local TUI submission path resets its composer state, which suggests an external API prompt may leave a visible draft intact; this inference is not runtime evidence.
A bounded API-first trial in a manually started visible session observed one API prompt and reply while a synthetic unsent draft remained visible and intact.
It used no tools and was not Model Preferences managed dispatch or Hailing automatic delivery.
A subsequent managed-session experiment also preserved the synthetic draft, while Model Preferences retained its production delivery hold.
Separate preflight controls observed pending-form and pending-permission holds, with the exact scope and limits recorded in the [acceptance record](opencode-qwen-acceptance.md).
These are bounded draft-preservation observations, including one actual opted-in Model Preferences continuation; they do not qualify every API-sidechannel route.
The first explicit-policy continuation exposed a receipt-schema mismatch after admission and was not replayed.
A fresh trial after correction passed actual Model Preferences continuation with the exact visible draft intact and absent from server context.
A fresh candidate passed actual Qwen-origin Hailing self-enrollment with an exact persisted identity binding while its tool call was running.
On that enrollment candidate, one experimental API-sidechannel Hailing wake completed its exact Qwen reply, and a pending-inbox control refused further delivery without new provider activity.
A fresh case on `818a082` subsequently completed the Qwen-origin own-inbox CLI and exact local receipt while its API was running.
Independent tool and request-channel inspection attributed the completed max-Qwen body-derived reply to the primary tool result.
This qualifies bounded inbox consumption; the later assisted Discord roundtrip below is separate, and other pending-interaction controls and timer delivery retain their limits.
The default strict composer policy is unchanged; do not opt in based only on source availability.
One N1 case on `084faeee` later qualified a human-controlled visible-session continuation for a real Agent Notify reply and owner close in the same persisted Qwen session.
The chief supplied the signed-in user reply and manual pointer tasks after restoring the actual client/API scope; reply content entered the model only through the tool result.
This is assisted legacy CLI coverage, without automatic DW wake, managed DW receipt or global native-hook restoration.
A later timer case on `2dd9410` qualified actual Qwen-origin timer creation while busy, without delivery or automatic wake.
A local-synthetic Discord Watch case on `ffd0a31` qualified receipt and primary-model body consumption, without remote registration or ingestion.
Lifecycle identity and delivery-policy changes were accepted separately at `084faeee`.
Other unobserved component behavior remains a reporting limit; representative tool use and ordinary software tests are the completion standard, without requiring an exhaustive live Qwen matrix.
Do not infer notification, wake, task completion or review feedback support from source identity handling alone.

## Stop conditions

Stop before task launch if the exact local provider or model cannot be confirmed.
Stop if an unexpected credential appears, a hosted request occurs, a permission boundary is broader than the task, or the task needs an unapproved path.
Report the credential's location and category without copying its value, and wait for the parent to decide the next step.
Stop any managed or automatic send if OpenCode shows a permission request or human-input obligation.
Under the default strict policy, a draft or ambiguous composer state also holds delivery.
Do not treat an idle model state or empty permission list as evidence that the composer is empty.
Use the experimental API-sidechannel policy only when explicitly selected for a bounded authorized trial with its separate exact-identity and pending-interaction checks.
The synthetic draft-preservation control is evidence for one bounded visible-session trial, not authorization to bypass a production guard.

## Evidence and status

Use the [acceptance observations](opencode-qwen-acceptance.md) as the evidence record for the tested configuration and its limits.
Use the [Qwen operating guide](qwen-operating-guide.md) for task cards, supervision, liveness, secret handling, review authorization and report formats.
Record exact runtime versions, candidate, role, task, provider, model, reasoning setting, actual request limits, tool permissions, observations, consequences and remaining limits.
Keep Codex-supervised Qwen implementer observations separate from Qwen-only orchestrator qualification.
One successful run does not establish a benchmark, universal cadence, retry policy, task size, security guarantee or support for another component.

## References

[OpenCode v2 configuration](https://dev.opencode.ai/v2/docs/config/).
[OpenCode v2 providers](https://dev.opencode.ai/v2/docs/providers/).
[OpenCode v2 instructions](https://dev.opencode.ai/v2/docs/instructions/).
[Ollama OpenAI compatibility](https://github.com/ollama/ollama/blob/main/docs/api/openai-compatibility.mdx).
[Pinned OpenCode 2.2.5 plugin context source](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/plugin/src/README.md).
[Pinned OpenCode 2.2.5 TUI submission path](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/tui/src/component/prompt/index.tsx) and [TUI context state](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/tui/src/context/data.tsx).
[Pinned OpenCode 2.2.5 Pattern Buffer acceptance notes](opencode-qwen-acceptance.md#pattern-buffer-native-context-and-compaction).
