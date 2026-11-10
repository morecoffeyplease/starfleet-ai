# Policy Guard guide

Policy Guard keeps delivery reports and their local history for a native orchestrator seat.
It can refuse covered GitHub readiness and merge commands when an attached checkout or pull request has no valid report.
It does not run audits or verification, classify prompts, grant readiness authority, or enforce fix rounds.

## Review and fix cycles

Read this section when starting or resuming a review-and-fix cycle, then revisit the relevant rule before each repair decision.
Use the approved PR purpose to decide whether a finding warrants blocking delivery; review does not require eliminating every conceivable defect.
Repeated repairs can add risk and delay, so choose the smallest safe route that delivers the approved outcome.

### Triage findings

Use existing evidence to establish the concrete harm, likely path, and connection to the approved outcome.
Bot names, security labels, and agreement do not supply independent proof.

- **Fix here** for a material PR regression, an unmet approved outcome, or a high-impact defect on a common path, within existing authority.
- **Defer** a real but nonblocking, unrelated, or low-impact edge case; link a matching issue or file one actionable issue only when authorized.
- **Dismiss** a duplicate or an evidence-refuted or nonapplicable claim, and record the reason and evidence.

Missing evidence means a claim is unverified, not disproved.
Take the cheapest observation that could decide an unresolved claim, and ask the user before repairing if uncertainty remains.
Do not create an issue for every speculative warning.
Workers report new findings to the orchestrator and preserve their assigned batch rather than expanding it.

For a credible serious-but-rare security exposure, data-loss risk, or safety-contract failure, report it promptly and ask the user before implementing a fix.
Existing investigation is allowed, but do not silently defer the risk or mark the PR ready while its safety decision remains unresolved.
Keep sensitive details in the approved private channel.

### Handle repair-caused regressions and count attempts

If a repair creates a new regression in the same area, stop that affected repair chain before another product edit or dispatch.
Preserve the evidence and compare an isolatable revert with a simpler design.
Prefer removing an unsuccessful repair when safe, but do not mechanically revert mixed changes, discard unrelated approved work, or restore a demonstrated serious vulnerability.
If safe reversion is unclear or the approved outcome would remain broken, ask the user for a bounded next route.

Use the existing accepted product-fix-batch count.
One accepted batch may contain several findings, and a commit, worker launch, clean review, or unchanged verification rerun does not add a round.
Keep the existing counting exclusions for confirmed flakes, rebase-only work, and proof-harness-only changes without a qualifying product fix.
Product behavior changes cannot be relabelled proof repair to avoid the count.

A repair attempt is a coherent product change aimed at one underlying finding.
It is unsuccessful when deciding evidence shows that the finding remains unresolved or the repair creates a same-area regression.
Several commits or workers within one attempt do not multiply attempts.
A renamed symptom, handoff, rebase, split, or child symptom does not erase related history.
If the relationship between findings is uncertain, record the uncertainty rather than resetting the count.

After two unsuccessful attempts on the same finding, stop before a third, reconsider the premise, and ask the user for bounded continuation, safe reversion, or deferral.
A first repair-caused regression already requires an immediate stop, so the two-attempt limit does not grant another automatic patch.
From round three, prepare a fresh zoom-out for the actual upcoming repair batch before accepting or dispatching it.
Before round four, obtain user approval for a specific next batch or explicit allowance.
For an existing PR already beyond round three, apply this prospectively before the next unapproved batch; do not reset or retroactively approve its history.

The request for approval should state the approved outcome, remaining evidence-backed blocker, attempts and outcomes, added complexity, simplest safe route, and proposed bounded batch.
One request may cover overlapping stop conditions, but approval applies only to its stated batch or allowance.
Preserve stricter scope, mechanism, review, merge, safety, and human-approval rules.
Continue independent authorized work while a held batch waits, and do not stop already authorized jobs solely to introduce this policy.

### Record the zoom-out in existing records

Use the existing PR, tracker, and evidence links; do not create another form, panel, model call, or fresh run solely to populate it.
Keep one concise assessment row per finding where practical, with disposition, evidence, impact, attempt count, and next action.

The existing fixture assessment schema requires these fields:

- `batch` and `round` must identify the active batch and round.
- `purpose_quote` must quote the exact current PR purpose.
- `observed`, `concluded`, `simplest_design`, `design_reason`, and `decision` must be nonempty.
- `inventory` must be a list, including an explicit empty list when no mechanisms were added.
- Each inventory entry must include nonempty `name`, `needed_because`, and `deletion_effect`, a nonnegative integer `lines` count, and an originating `round` number or `unknown` when history is unavailable.
- `runtime_failure` must be a boolean.
- When `runtime_failure` is true, include nonempty `solo_evidence` and `main_evidence`; otherwise include nonempty `finding_quote`, `source_path`, and `unproven`.
- `decision` must be `keep`, `delete`, `revert`, `split out`, or `simplify`.
- `adds_scope` and `adds_mechanism` must be booleans.

The guard continues to require its existing accepted and dismissed finding-ID coverage and report obligations.
If the selected decision is `split out`, or `adds_scope` or `adds_mechanism` is true, the fixture guard stops and requires human approval.
Policy Guard does not import or authenticate that approval.
This guidance does not change the fixture schema, round counting, or delivery-seat behavior.
The current delivery seat does not enforce fix rounds, and this guide does not claim automated enforcement.

## Published guidance attestations

Prime Directive (Policy Guard) is the same delivery tool described by this guide and the `lane.py` CLI.
This optional protocol binds one opening or accepted repair-batch attestation to the existing evidence packet and current PR binding.
The human owner selects and rereads the applicable guidance, playbooks, principles and repository fix-guidance before editing the packet.
The evidence packet may be authored manually before a PR exists, while the current renderer and publication check require the published owner-bound PR binding.
For each reread file, record its kind, name, actual source reference, local path and SHA-256 of its current bytes.
Use navigable source references, such as a public file URL pinned to a commit, and record installed-only files by their actual path and digest.
The validator checks the declared local bytes, packet fields, gaps and stage ordering; it cannot prove comprehension, sound judgment or that every applicable source was declared.

Add one `guidance_attestation` object to the existing evidence packet.
Use `stage: "opening"` for the opening comment and `stage: "repair_batch"` for an accepted repair batch.
Both stages require an exact `issue_ref`, explicit prerequisite completion and an empty `gaps` list, current `reread` file references, `guidance_application`, concrete `approach` and `approach_reason`, `evidence_refs`, `assumptions: []`, an explicit `uncertainty` list, and links to the approved scope and human holds.
Missing, null, non-list or nonempty `assumptions` values are rejected instead of discarded, and the rendered assumptions section states `None`.
Repair batches also require `accepted_batch_ref` to identify the accepted batch and `repair_history_ref` to link existing repair history.
Resolve an unknown required fact by investigating it or asking the configured human on Discord, and do not attest while a required fact or prerequisite remains unresolved.
Disclosing an unknown as an assumption or uncertainty does not complete a prerequisite or waive missing evidence, verification, scope or authority.
The `uncertainty` list may still record honest tool-capability limits or future outcomes, such as a later authenticated comment read being unavailable.
Do not claim future repair verification is complete.

```json
{
  "guidance_attestation": {
    "version": 1,
    "stage": "opening",
    "issue_ref": "https://github.com/OWNER/REPO/issues/123",
    "prerequisites_complete": true,
    "gaps": [],
    "reread": [
      {
        "kind": "playbook",
        "name": "Applicable playbook",
        "source_ref": "https://github.com/OWNER/REPO/blob/FULL_COMMIT/path/to/playbook.md",
        "path": "/absolute/path/to/playbook.md",
        "sha256": "CURRENT_FILE_SHA256"
      }
    ],
    "guidance_application": "How the reread guidance shaped this approach and evidence.",
    "approach": "The chosen concrete approach.",
    "approach_reason": "Why this approach meets the outcome and why a smaller change would not suffice, where relevant.",
    "evidence_refs": ["https://github.com/OWNER/REPO/pull/123/files"],
    "assumptions": [],
    "uncertainty": ["A later authenticated GitHub comment read may be unavailable."],
    "approved_scope_ref": "https://github.com/OWNER/REPO/issues/123#approved-scope",
    "human_holds_ref": "https://github.com/OWNER/REPO/issues/123#human-holds"
  }
}
```

An opening render requires a current clean report with successful canonical verification evidence, including a valid nothing-affected outcome when applicable.
Unrelated report gaps remain independent from the mandatory opening-attestation prerequisite.
Its body presents the approach, completed evidence, reread references, scope, holds and declared uncertainty, renders assumptions as explicit `None`, then ends with the attestation.
A repair-batch render does not require the final candidate report, begins with its completed prerequisite attestation, and never claims future fix verification passed.
Both bodies include this statement:

> I have chosen the simplest viable approach that meets the approved outcome and constraints, and kept the solution proportionate to the problem it solves.
> I have made no claims based on assumptions, and I checked the required reads, investigation, evidence, verification, approved scope, and authority for this work.
> Any required fact that was initially unknown was investigated or raised to the configured human on Discord, then resolved before I attested.
> Disclosing uncertainty does not waive a missing prerequisite.

Support that statement with the concrete design choice and why a smaller change would not suffice where relevant; do not invent an alternative to fill a template.
The validator checks declared fields and bytes, but cannot prove comprehension or the absence of hidden assumptions in reasoning.

Render the exact body without state writes, then post it yourself with the ordinary explicit GitHub CLI command.
The start check only accepts the exact comment already published to the currently bound PR; it never creates, comments on or edits GitHub records.

```sh
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree record \
  --evidence /path/to/delivery-evidence.json
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree attestation-render \
  --evidence /path/to/delivery-evidence.json --stage opening > /tmp/opening-attestation.md
gh pr comment https://github.com/OWNER/REPO/pull/123 --body-file /tmp/opening-attestation.md
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree attestation-start-check \
  --evidence /path/to/delivery-evidence.json --stage opening \
  --comment-url 'https://github.com/OWNER/REPO/pull/123#issuecomment-ID' --enable
```

Use `--stage repair-batch` for an accepted batch and include its `accepted_batch_ref` in the packet before rendering.
Run the start check before starting the repair work.
The first owner-coordinated adoption uses `--enable`; it is accepted only after owner, current remote PR mapping, exact comment membership, body, packet and reread-source validation succeed.
That sets checkout-level `attestation_required: true` and records the exact publication reference under the current PR binding.
Legacy attachments with no flag remain unchanged until this explicit adoption.
Existing immutable seats must first receive the reviewed source through the normal `upgrade-plan` and `upgrade` flow; pinned runtime source is never hand-edited.
For each later PR, discover its current binding and validate its own published comment with the same command without `--enable`; publication references do not transfer between PRs or owners.
There is no disable or fallback flag.

An adopted target refuses covered ready or merge unless its current packet attestation, exact local reread bytes and per-PR publication reference match.
Each covered action reads the exact authenticated GitHub comment using the captured `gh` executable with a bounded timeout; unavailable, edited, deleted or mismatched comments fail closed.
When evaluating a recorded report, ordinary `status` checks the attestation packet and reread bytes locally without querying GitHub for comment freshness.
The explicit `attestation-start-check` and each covered ready or merge action still make a fresh authenticated read.
The ordinary report still checks candidate freshness and preserves its existing checklist-gap behavior.
No atomicity is claimed between the remote read and later GitHub forwarding.
The attestation text and hash prove only that the declared files and comment bytes matched these observations.

This addition is qualified only for Codex's explicit seat `gh` entrypoint and Claude's supported direct `gh pr ready` and `gh pr merge` hook paths.
New enabled Claude checks perform the bounded authenticated comment read; local evaluation of a legacy target remains local.
OpenCode adoption is refused in this release.
Pattern Buffer supplies a notice through its existing SessionStart standing-context injection and does not detect an issue or batch.
Raw push, PR creation, comment editing, scripts, web/API requests, GitHub Actions and alternate start paths remain outside interception.
Existing report checks, review and CI, human readiness holds, approval authority and merge authority remain unchanged.

## Requirements

Use Python 3.10 or later, Git, and the GitHub CLI `gh` on PATH.
Use a clean Starfleet source checkout for the first reviewed installation.
The owner harness and session ID must come from the actual Claude or Codex runtime, or from the OpenCode server plugin context described below.
The OpenCode prototype targets only version 2.2.5 and its server plugin API.

## Install one seat and attach a checkout

Choose a permanent owner checkout and the product checkout that contains the real branch.
Print this guide with `python3 /path/to/starfleet-policy-guard/lane.py guide --print` before setup.
The same read-only guide ships in the immutable runtime and prints outside Git without credentials, enrollment or state writes.
Review the read-only plan before applying it:

```sh
python3 /path/to/starfleet-policy-guard/lane.py --cwd /product/worktree --session-root /permanent/owner-seat \
  plan --lane --host github.com --owner-harness claude --owner-session ACTUAL_NATIVE_SESSION_ID
python3 /path/to/starfleet-policy-guard/lane.py --cwd /product/worktree --session-root /permanent/owner-seat \
  install --lane --host github.com --owner-harness claude --owner-session ACTUAL_NATIVE_SESSION_ID --plan-sha256 REVIEWED_PLAN_SHA256
```

Use `codex` for a Codex owner.
The first install result returns an immutable `seat_runtime` path.
Use that installed runtime CLI for later checkout attachments so the workflow keeps working after the source worktree is deleted:

```sh
python3 /INSTALLED/SEAT/RUNTIME/lane.py --cwd /another/product/worktree --session-root /permanent/owner-seat \
  plan --lane --host github.com --owner-harness claude --owner-session ACTUAL_NATIVE_SESSION_ID
python3 /INSTALLED/SEAT/RUNTIME/lane.py --cwd /another/product/worktree --session-root /permanent/owner-seat \
  install --lane --host github.com --owner-harness claude --owner-session ACTUAL_NATIVE_SESSION_ID --plan-sha256 REVIEWED_PLAN_SHA256
```

Use `codex` in those commands for a Codex owner.
Each later attachment reuses the seat hook and entrypoint and adds only checkout data.
Use `discover` from an attached product checkout after publishing its real PR; covered seat `gh` actions also discover the current branch's published PR before gating.
You may attach a native-owner lane before a PR exists.
For an active attachment whose recorded product root and branch match the current checkout, `status` reports `installed_here: true` even when `pr_url` is empty.
This reports local checkout configuration, not native hook activation or delivery eligibility.
An inactive attachment or a different checkout root or branch reports `installed_here: false`.
Use `status` and `coverage --summary` from the owning native session to inspect attachment, owner, activation observations, and PR binding separately:

```sh
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree status --session-id ACTUAL_NATIVE_SESSION_ID
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree coverage --summary
```

## Read-only setup recommendation

`status` and `coverage` include the same `setup_guidance` object with one `action`, a concrete `reason`, and a `command` only when its required inputs are known.
`coverage --summary` prints the same recommendation as one `Next setup:` line and an optional `Run:` command.
Actions include `attach`, `upgrade`, `migrate`, `handoff`, `recover`, `inspect`, and `none`.
The recommendation uses the recorded checkout, installed source fingerprints, managed adapter state, upgrade journal, and actual native identity.
The `status --session-id` argument selects activation observations only and does not identify the acting owner.
Missing identity, unrecognized owner metadata, unavailable source, or a different root or branch produces a bounded inspection recommendation without guessed IDs, paths, or flags.
A clean committed source checkout is required before the recommendation prints an `upgrade-plan` or `migrate-plan` command.
An installed immutable runtime can report status, but it is not presented as an executable source-upgrade plan when no clean Git revision is available.
An owner mismatch does not authorize transfer; a handoff command is conditional on explicit authorization from the recorded owner.
Interrupted upgrade recovery is shown only when the actual native identity and checkout root and branch match the recorded context.

If no lane is attached, the recommendation keeps attachment optional because standalone reporting and proof-only work do not need enrollment.
If an existing legacy binding is healthy, the recommendation does not require migration solely because the installation is legacy.
If a legacy source checkout is missing, `migrate-plan` may assess migration from a clean source without recreating the deleted source.
A recorded delivery report remains separate from setup and is needed only before ordinary covered delivery.
The report can record checklist gaps and does not replace repository checks, human approval, or delivery authority.

Covered delivery remains PR-bound and requires a real PR binding and a recorded delivery report.
The plan and install commands do not create a PR, fetch, change branches, run checks, or update GitHub records.

## OpenCode 2.2.5 prototype server plugin

This prototype targets the server plugin tools from an actual OpenCode 2.2.5 server session.
For the first enrollment, configure the clean committed source checkout's `opencode-plugin` directory in the project's `opencode.jsonc` or the intended OpenCode config:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["/CLEAN/SOURCE/apps/starfleet-policy-guard/opencode-plugin"]
}
```

OpenCode local plugin entries must point to a directory; the plugin loader finds `index.ts` inside that directory.
Use the `prime_directive_manage` tool with `operation: "plan"`, the product checkout, and permanent seat root.
Review the plan and its exact `plan_sha256`, then use `operation: "install"` with that same hash.
The install result returns an immutable `opencode_plugin_path` directory under the seat runtime.
Configure that returned directory for later sessions and restart or reload the OpenCode server as needed for its config loader.
Use the same actual OpenCode session and session API project/location for later seat operations.

The plugin reads `sessionID`, agent, message ID and call ID from the actual server tool context, then calls the OpenCode session API for the session's exact ID, parent ID, project ID and location directory.
The `OPENCODE_SESSION_ID` shell environment variable is not an owner identity and cannot enroll a seat.
The native server plugin passes its event and tool arguments as JSON on stdin to the local `lane.py opencode` bridge.
The bridge validates the event shape but does not authenticate it cryptographically.
A local caller that can invoke this bridge can manufacture event JSON and attempt covered operations without the plugin's native permission assertion, subject to the remaining local owner, checkout, and report checks.
This is a cooperative workflow guard, not an adversarial local-process or operating-system boundary.
Do not give untrusted local workers access to the bridge or seat state, and do not claim that plugin source or its JSON event proves native authorization.
The root session and every child session are distinct owners, and a child does not inherit a parent's seat.
Use `prime_directive_manage` with `operation: "rebind-owner"` from the incoming actual session for an explicit owner-authorized handoff.
The handoff preserves seat and PR history, archives the old owner's receipt, and requires a new report from the incoming owner.

Use `prime_directive_gh` only for supported `ready` and `merge` actions.
It asks OpenCode for the native `shell` permission using the equivalent plain `gh pr ready` or `gh pr merge` resource before Policy Guard checks the owner, checkout, branch, PR, source and fresh report immediately before forwarding.
An OpenCode native `ask` still waits for the operator, and a configured native `deny` remains final.
The plugin refuses plain `gh pr ready` and `gh pr merge` command segments at the start of a shell command or after a command-list operator before shell execution.
Wrapped commands, opaque shell syntax, alternate executables, scripts, API and web requests, MCP tools and external processes remain outside this bounded coverage.
This does not claim an arbitrary shell sandbox or complete interception of every GitHub delivery route.
The plugin source and local fixtures alone do not prove that a native OpenCode server loaded the plugin or preserved a real user's permission decision.
Do not claim OpenCode support qualification until the actual native pilot and Policy Guard owner acceptance are complete.

Codex currently uses the seat's explicit `gh` entrypoint for covered readiness and merge actions.
Claude's installed hook covers the supported direct `gh pr ready` and `gh pr merge` command forms in attached repositories.
On the seat entrypoint, a full PR URL determines its host and repository even when `--repo` is also present; unmatched explicit repository selectors pass through, while selectors ambiguous across attached hosts require a full URL or `HOST/OWNER/REPO` and unknown PRs within an attached repository remain gated.
Published-branch discovery runs only on the explicit seat `gh` delivery path.
Claude's local hook evaluation makes no GitHub calls for legacy targets; an adopted attestation target adds the bounded authenticated comment read described above.
Neither path intercepts arbitrary shell scripts, other applications, or commands outside its documented entrypoint.

## Upgrade an installed seat

Use a clean, committed newer Starfleet source checkout and run the reviewed upgrade from an active attached product checkout:

```sh
python3 /clean/starfleet-ai/apps/starfleet-policy-guard/lane.py --cwd /product/worktree upgrade-plan
python3 /clean/starfleet-ai/apps/starfleet-policy-guard/lane.py --cwd /product/worktree upgrade --plan-sha256 REVIEWED_PLAN_SHA256
```

Review the exact plan hash, old and new source fingerprints, and every active checkout listed in the seat-wide preimages before applying it.
The upgrade updates the active attachments together, archives each receipt in that checkout's PR history, and requires a fresh report from each checkout before covered actions can pass.
Migration records the target settings mode and immutable runtime pin in each migrated checkout, and preserves the target settings postimage hash even when the seat already owns its hooks.
For an earlier migrated checkout with missing runtime metadata and a stale settings mode, `upgrade-plan` can propose recovery only when its source path and pinned fingerprints match the owning seat runtime and its live settings path, content hash, and mode match the durable seat metadata.
Review each `migration_metadata_recovery` entry in the plan, then apply the exact plan hash with `upgrade`.
For this metadata recovery path, changed settings content or mode, mismatched paths, invalid pins, and malformed runtime metadata remain refused.
If an interrupted upgrade leaves a journal, use the reviewed source and same product checkout to restore the recorded preimages:

```sh
python3 /clean/starfleet-ai/apps/starfleet-policy-guard/lane.py --cwd /product/worktree upgrade-recover --rollback
```

## Inspect, hand off, and recover

Run `python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID status` to inspect a permanent seat even when product checkouts are unavailable.
Run `python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID registrations` to inspect attached PR history without a product checkout.
Run `coverage --summary` or `status` from an attached checkout to inspect local configuration and observations.
Configured hooks and adapters are not proof that the native runtime loaded or used them.
Use `python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID rebind-owner --from-harness claude --from-session OUTGOING_SESSION_ID --confirm-handoff` only after an authorized transfer to the incoming actual native session.
`--from-harness` and `--from-session` identify the exact outgoing owner; the incoming harness and session are detected from the current native runtime.
Handoff preserves history and requires a fresh report from the incoming owner.
For a cross-harness handoff, the transaction adds or removes only the seat-owned Claude hook pair as needed and preserves unrelated settings.
Coordinate the existing single-writer settings checkpoint before a Claude settings change, and preserve native trust requirements.
The settings change records configuration only and does not prove that the incoming native runtime loaded or trusted the hook.
If the seat `gh` entrypoint, Claude hook or a non-CLI runtime file is missing while the installed `lane.py` is intact, run `recover --inspect`, review `recover --plan`, then apply its hash with `recover --apply --plan-sha256 PLAN_SHA256` through the installed runtime.
If the installed runtime's `lane.py` or its entire directory is missing, use a clean current source checkout whose runtime fingerprint matches the seat's recorded version, then run those same recovery commands with that source `lane.py`.
Recovery verifies the exact recorded file hashes before restoring missing files, and changed runtime files fail closed for inspection rather than being overwritten.
If a legacy per-worktree source checkout or hook is missing, run `migrate-plan` and `migrate` from a clean current source checkout while the existing product Gitdir and state are available.
Migration accepts an active legacy lane with no PR when its recorded checkout root and branch match the current product checkout.
The migration plan preserves its existing lane and owner history without creating a PR binding or report.
It continues to refuse inactive legacy state and a product branch that differs from the recorded branch.
For a legacy installation without a native owner, include `--confirm-owner` on both commands.
For a legacy installation owned by another native session, pass `--confirm-handoff --from-harness claude --from-session OUTGOING_SESSION_ID` on both commands, using the exact recorded outgoing harness and session.
Migration preserves legacy state and requires exact owner confirmation or an authorized handoff when required.
Do not recreate receipts, manual counts or external evidence files.

The runtime is copied into a versioned user data directory and local history stays in the user state directory.
Product Git directories are not used as the durable history store.
No daemon is installed.

Covered adapter delivery calls refresh a read-only GitHub PR state observation, and a successful covered merge refreshes it once more after forwarding.
An observed closed PR becomes eligible for local retention after 30 days by default.
Run `retire --pr-url https://github.com/owner/repo/pull/123` from an attached checkout, or add the global `--seat-id SEAT_ID` before `retire` when no product checkout is available.
Use `pin --pr-url ...` and `unpin --pr-url ...` with the same seat-selection rule to protect or release one local PR record.
Preview cleanup and review its exact candidate hash before applying:

```sh
python3 /path/to/starfleet-policy-guard/lane.py --seat-id SEAT_ID cleanup --preview --retention-days 30
python3 /path/to/starfleet-policy-guard/lane.py --seat-id SEAT_ID cleanup --apply \
  --retention-days 30 --plan-sha256 REVIEWED_PLAN_SHA256
```

Run cleanup from the exact owning native session.
Use global `--seat-id SEAT_ID` before `cleanup` to restrict the preview and application to that seat when no product checkout is available.
Review the candidates and hash before applying the same plan.
Cleanup is bounded and opportunistic during installation and explicit maintenance for the exact current seat owner.
It prunes only known terminal local history past retention and unreferenced runtime versions.
It never removes active, uncertain, pinned, recovery-journaled or referenced records, or external packets, audits and manual counts.

## Scope and authority

`record` requires the report execution but permits documented checklist gaps.
The local verification command must exactly match the repository's documented canonical command, and its source and evidence hashes must be recorded.
Policy Guard does not infer a package manager or run the verification command while reporting.
Real audit findings, deviations approved by the configured human authority, local verification and repository delivery rules remain required.
The guard never bypasses native trust or automatic approval review.
Only the active exact native owner may use an attached seat; replacement sessions require the explicit handoff command.
Use `cleanup --preview` to review eligible old terminal local records before `cleanup --apply` removes them.

For detailed behavior and limitations, see the component README.
