# Policy Guard guide

Policy Guard keeps delivery reports and their local history for a native orchestrator seat.
It can refuse covered GitHub readiness and merge commands when an attached checkout or pull request has no valid report.
It does not run audits or verification, classify prompts, grant readiness authority, automatically detect new repair batches, count rounds, or enforce repair decisions.

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

After the third unsuccessful repair batch, if deciding evidence still shows a material blocker, stop and ask the configured human authority whether they want one optional skeptical review of the exact next-step proposal.
State the approved outcome, remaining blocker, attempts and outcomes, added complexity, and smallest safe next route.
Count from the existing accepted repair history; never infer repair-round count from a Model Preferences review-cycle ordinal.
The skeptical review is optional, uses the single reviewer saved in the active profile's adopted Model Preferences policy, and is scoped to the exact target, proposal, selection and worktree.
See the [Model Preferences skeptical proposal review path](../model-preferences/README.md#request-an-optional-skeptical-proposal-review) for its explicit setup, approval and dispatch commands.
If the human authority declines, no model launches, and the user may still make a separate bounded continuation, revert or defer decision under the existing rules.
If the review runs, return its recommendation and unresolved disagreement to the human authority before asking for a separate bounded continuation decision.
Approval to run the reviewer authorizes only that review and never authorizes the next repair batch.
This optional review is not a readiness gate, and neither Model Preferences nor Policy Guard detects undeclared repair edits.

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
The delivery seat does not automatically detect new batches, count rounds, or enforce repair decisions.
Its bounded repair-batch check validates declared assessment fields, required approval references, and whether the packet has an accepted repair disposition.

## Published guidance attestations

Prime Directive (Policy Guard) is the same delivery tool described by this guide and the `lane.py` CLI.
This optional protocol binds an opening, accepted repair-batch, or successor-validation attestation to the existing evidence packet and current PR binding.
The human owner selects and rereads the applicable guidance, playbooks, principles and repository fix-guidance before editing the packet.
The evidence packet may be authored manually before a PR exists, while the current renderer and publication check require the published owner-bound PR binding.
For each reread file, record its kind, name, actual source reference, local path and SHA-256 of its current bytes.
Use navigable source references, such as a public file URL pinned to a commit, and record installed-only files by their actual path and digest.
The validator checks the declared local bytes, packet fields, gaps and stage ordering; it cannot prove comprehension, sound judgment or that every applicable source was declared.
Before attesting, inspect the evidence supporting each fact the approach relies on.
A worker report or partial check does not establish more than its demonstrated coverage.
Follow the [briefing and evidence acceptance guidance](../pattern-buffer/docs/implementer-dispatch.md#briefing-and-evidence-acceptance).

Add one `guidance_attestation` object to the existing evidence packet.
Use `stage: "opening"` for the opening comment and `stage: "repair_batch"` for an accepted repair batch.
Both stages require an exact `issue_ref`, explicit prerequisite completion and an empty `gaps` list, current `reread` file references, `guidance_application`, concrete `approach` and `approach_reason`, `evidence_refs`, `assumptions: []`, an explicit `uncertainty` list, and links to the approved scope and human holds.
Missing, null, non-list or nonempty `assumptions` values are rejected instead of discarded, and the rendered assumptions section states `None`.
Repair batches also require `accepted_batch_ref` to identify the accepted batch and `repair_history_ref` to link existing repair history.
Resolve an unknown required fact by investigating it or asking the configured human on Discord, and do not attest while a required fact or prerequisite remains unresolved.
Disclosing an unknown as an assumption or uncertainty does not complete a prerequisite or waive missing evidence, verification, scope or authority.
The `uncertainty` list may still record honest tool-capability limits or future outcomes, such as a later authenticated comment read being unavailable.
Do not claim future repair verification is complete.
Keep actual repair rounds in the linked existing history, separately from Model Preferences review-cycle ordinals.
Use existing `evidence_refs` for the exact skeptical proposal and review result when applicable.
Keep `approval_evidence_ref` bound to the distinct authorization for the accepted repair batch; approval for a skeptical review is not that authorization.
Do not add a second repair-round counter or claim that attestation records intercept undeclared work.

Before recommending another repair, requesting approval, or dispatching implementation, complete the existing assessment of prior outcomes, premise, deciding observation, disposition, and applicable rules.
After any required authorization, reuse this same assessment in the accepted repair-batch attestation without creating a second form or store.
New repair-batch attestations also require `repair_assessment` in the same `guidance_attestation` object.
It contains a nonempty `findings` list and an explicit `approval_required` boolean.
Each finding requires `finding_ref`, an explicit `prior_attempts` list, `premise`, `deciding_observation`, `deciding_evidence_ref`, `disposition`, and a nonempty `applicable_rules` list.
Each prior attempt requires an `attempt_ref` and an `outcome`.
Each applicable rule requires a `reference` and `why_applicable` explanation.
Use an empty `prior_attempts` list only when existing history confirms this is a first attempt.
The validator checks required fields and their types, but it cannot prove that history is complete, the observation is true, the assessment preceded a recommendation, or the referenced approval was authored by the required person.
If a required fact remains unknown, keep it in the existing `gaps` list and do not attest.

Allowed finding dispositions are `fix here`, `revert`, `simplify`, `split out`, `defer`, `dismiss`, and `stop`.
An accepted repair-batch attestation needs at least one `fix here`, `revert`, `simplify`, or `split out` disposition.
Additional findings in the same batch may be deferred or dismissed.
A `stop` disposition refuses rendering and start-checking until the existing stop is resolved and reassessed.
An all-deferred or all-dismissed assessment describes no accepted repair batch and stays in the existing PR or tracker.
If `approval_required` is true, `approval_evidence_ref` must link the existing approval evidence; if false, set `approval_evidence_ref` to `null`.
A `split out` disposition requires the assessment to declare approval and link its evidence.
These checks do not infer approval thresholds, decision quality, timing, or authorship.

The renderer prints each declared finding assessment once under `Repair assessment`.
The accepted repair-batch body records completed prerequisites and the approved batch before implementation; it does not claim final candidate verification is complete.
Run the repository's canonical verification and record the resulting candidate through the existing report flow after the repair.

### Repair assessment example

Add this object inside the repair-batch `guidance_attestation` object.
Keep `accepted_batch_ref` and `repair_history_ref` as the existing batch and history links.

```json
{
  "repair_assessment": {
    "findings": [
      {
        "finding_ref": "https://github.com/OWNER/REPO/issues/123#finding-1",
        "prior_attempts": [
          {
            "attempt_ref": "https://github.com/OWNER/REPO/pull/123#issuecomment-98",
            "outcome": "The same finding remained after the first attempt."
          }
        ],
        "premise": "The remaining defect comes from the existing branch binding.",
        "deciding_observation": "The supported path reproduces the defect when the branch binding is stale.",
        "deciding_evidence_ref": "https://github.com/OWNER/REPO/pull/123#issuecomment-99",
        "disposition": "simplify",
        "applicable_rules": [
          {
            "reference": "https://github.com/OWNER/REPO/blob/FULL_COMMIT/apps/starfleet-policy-guard/guide.md#handle-repair-caused-regressions-and-count-attempts",
            "why_applicable": "The prior attempt targeted this same underlying finding."
          }
        ]
      },
      {
        "finding_ref": "https://github.com/OWNER/REPO/issues/123#finding-2",
        "prior_attempts": [],
        "premise": "The separate edge case does not block the approved outcome.",
        "deciding_observation": "The common path succeeds and the edge case is not reproduced.",
        "deciding_evidence_ref": "https://github.com/OWNER/REPO/pull/123#issuecomment-100",
        "disposition": "defer",
        "applicable_rules": [
          {
            "reference": "https://github.com/OWNER/REPO/blob/FULL_COMMIT/apps/starfleet-policy-guard/guide.md#triage-findings",
            "why_applicable": "The issue is nonblocking and unrelated to the accepted repair."
          }
        ]
      }
    ],
    "approval_required": false,
    "approval_evidence_ref": null
  }
}
```

For a repair requiring approval under existing policy, set `approval_required` to `true` and provide its existing evidence reference.
The validator requires the reference but does not authenticate its author or infer whether approval was required.
Keep stop and no-work outcomes in the existing brief or tracker instead of publishing them as an accepted repair batch.

### Starfleet handoff acceptance

For `github.com/morecoffeyplease/starfleet-ai`, record concise successor acceptance in the existing evidence packet after the supported owner rebind.
The default checklist's `packet_successor_acceptance_repositories` selects this policy only for Starfleet.
Other repositories retain their existing publication requirements.
Existing publication references and owner history remain preserved.

```json
{
  "handoff_acceptance": {
    "version": 1,
    "owner": {"harness": "codex", "session_id": "ACTUAL_CURRENT_SESSION"},
    "from_owner": {"harness": "codex", "session_id": "ACTUAL_PREDECESSOR_SESSION"},
    "candidate": "FULL_CURRENT_CANDIDATE_SHA",
    "summary": "Accepted the reconciled work and current evidence in this handoff packet.",
    "repair_history_ref": "Existing repair history with cumulative counts and remaining gaps.",
    "scope_and_holds_ref": "Existing approved scope and unchanged human holds."
  }
}
```

Run the existing `record --evidence PATH` command from the exact incoming owner session.
Use the current candidate's successful canonical verification, including an explicit successful unaffected verdict when applicable.
The report preserves its normal candidate, target, packet, policy and evidence freshness checks.
The recorded predecessor must connect to the current native owner through the existing supported owner history.
When Pattern Buffer has required reads, complete them through its native owner flow before recording.
The report uses those current receipts directly, without copying read hashes into another attestation.
Changed required-read bytes invalidate the recorded acceptance.

No successor-render command, new public comment, copied predecessor publication hash or comment reread is needed for this selected policy.
Keep the existing handoff packet as the acceptance record.
This does not approve a new repair batch, reset repair counts, waive a human hold, grant delivery authority or prove comprehension.
Immutable installed seats still require their supported source upgrade before this behavior is available.

### Published successor validation for other policies

After a supported `rebind-owner` handoff on an already adopted target, the current native owner can use `stage: "successor_validation"` to reconcile work that is already complete.
The `prior_publication_ref` must be an exact copy of the current `guidance_attestation_ref` object shown in the owner session's `status` JSON, including any nested predecessor reference.
The packet must include `repair_history_ref` and a `reconciliation` object with nonempty `inherited_work`, `repair_history`, `scope_and_holds`, and `current_owner_work` entries.
Do not include `accepted_batch_ref` on this stage.
The renderer links the exact inherited comment and its recorded body hash, and includes the current candidate SHA from the successful report.
This stage records current responsibility for covered delivery and creates no repair batch, round count, authorship claim, or approval transfer.
The current owner must reread applicable guidance and reconcile completed work, current scope, holds, and candidate evidence before rendering.
The current review explains why the inherited approach remains the simplest viable route and proportionate to the approved outcome, makes no claims based on assumptions, checks current evidence, verification, scope, and authority, and resolves any unknown required fact before attesting.

Use the same evidence packet and current owner session for these commands.
Copy the full `guidance_attestation_ref` value from the first command into `prior_publication_ref` before recording.
Use the current `attestation-rereads` output to populate any registered required-read entries in `reread`.

```sh
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree status > /tmp/policy-guard-status.json
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree attestation-rereads
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree record \
  --evidence /path/to/delivery-evidence.json
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree attestation-render \
  --evidence /path/to/delivery-evidence.json --stage successor-validation > /tmp/successor-validation.md
gh pr comment https://github.com/OWNER/REPO/pull/123 --body-file /tmp/successor-validation.md
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree attestation-start-check \
  --evidence /path/to/delivery-evidence.json --stage successor-validation \
  --comment-url 'https://github.com/OWNER/REPO/pull/123#issuecomment-ID'
```

Successor rendering and start-check require a current clean report with successful canonical verification, including an explicit successful nothing-affected result when applicable.
The start check also verifies the exact inherited publication, the ordered supported owner-history path, the current local and published branch mapping, the inherited comment's current authenticated body hash, and the new exact comment.
It cannot run with `--enable`, and an inherited publication by itself still fails covered ready or merge delivery.
The incoming owner must record a fresh report before start-check, and current required-read receipts must match that owner's current session and file bytes.
The next handoff links the then-current publication, retaining the immediate predecessor reference and the existing owner-history path.

When the packet, candidate, and required-read context remain unchanged, a repeated start-check for the same exact successor comment is idempotent and keeps the same inherited predecessor.
When a successor report, packet, source read, or remote successor comment becomes stale, update the current packet and report, render a new body, post it as a new comment, and run start-check with that new comment URL.
Do not edit an earlier comment to replace its published body.
A missing or changed inherited comment fails closed because the exact prior publication can no longer be verified.
The tool does not infer comment authorship, renew human approval, create a repair batch, or reset repair history.

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
Opening and repair-batch bodies include this statement:

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
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree attestation-rereads
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree attestation-render \
  --evidence /path/to/delivery-evidence.json --stage opening > /tmp/opening-attestation.md
gh pr comment https://github.com/OWNER/REPO/pull/123 --body-file /tmp/opening-attestation.md
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree attestation-start-check \
  --evidence /path/to/delivery-evidence.json --stage opening \
  --comment-url 'https://github.com/OWNER/REPO/pull/123#issuecomment-ID' --enable
```

When Pattern Buffer is absent from `PATH` or reports `{"registered": false}`, Policy Guard keeps the existing attestation body and publication metadata unchanged.
With a registered list, `record`, `attestation-render`, and `attestation-start-check` query the installed `pattern-buffer` command once for the actual lane owner's harness, session and attached worktree.
Every applicable required entry must have a current receipt in the owner's current generation and a `reread` entry with the same resolved path and SHA-256.
The check also validates any extra reread entries using the existing packet rules, and it preserves those extras without marking them as required reads.
Missing, stale, omitted, path-mismatched or digest-mismatched entries refuse with their names and the `pattern-buffer ... read-required` commands to run in the owner session.
A Pattern Buffer integrity error retains its reported reason, while a failed command, timeout or malformed report refuses with a status command and guide recovery path.
Use `attestation-rereads` to print ready-to-paste `kind`, `name`, `path` and `sha256` values for the applicable required files, then add a `source_ref` for each entry yourself.
That helper only formats packet entries and does not publish an attestation or grant permission.
Receipt-backed lines in the rendered `Guidance reread` section include the current context generation.
The existing report receipt and per-target publication reference retain the list digest, generation, observation time and required entry names for later `status` output.
Covered ready and merge delivery reconstructs the published body from that stored context and does not start another Pattern Buffer subprocess.

Use `--stage repair-batch` for an accepted batch and include its `accepted_batch_ref` in the packet before rendering.
Run the start check before starting the repair work.
The first owner-coordinated adoption uses `--enable`; it is accepted only after owner, current remote PR mapping, exact comment membership, body, packet and reread-source validation succeed.
That sets checkout-level `attestation_required: true` and records the exact publication reference under the current PR binding.
Legacy attachments with no flag remain unchanged until this explicit adoption.
Existing immutable seats must first receive the reviewed source through the normal `upgrade-plan` and `upgrade` flow; pinned runtime source is never hand-edited.
For each later PR, discover its current binding and validate its own published comment with the same command without `--enable`.
Opening and repair-batch publications remain bound to their original owner and PR; after a supported owner handoff, successor validation creates a separate current-owner publication that links the exact predecessor.
There is no disable or fallback flag.

### Preserve an active legacy repair publication during upgrade

The reviewed source upgrade preserves an already accepted repair publication as the current publication while its original packet and exact rendered comment still match the stored attestation and body hashes.
This compatibility path exists only inside covered delivery validation for that exact current publication.
It does not let `attestation-render` or `attestation-start-check` accept a new repair-batch packet without `repair_assessment`.
The software cannot detect an undeclared new repair batch or prove that the assessment preceded a recommendation.
After the owner accepts another repair batch, add its assessment to the existing packet, render a new body, publish it as a new PR comment, then run `attestation-start-check` without `--enable`.
Do not edit or replace an earlier comment or rewrite prior attempt counts, repair history, or owner history.
An all-deferred, all-dismissed, or stopped decision remains in the existing PR or tracker and cannot authorize implementation dispatch.

For an active pinned seat, use the normal reviewed source upgrade before the next accepted batch.
The upgrade updates active source pins and requires a fresh report for covered actions, while preserving the existing current publication for in-progress accepted work.
Before the next batch starts, update the packet and publish a new exact comment through the same flow:

```sh
python3 /clean/starfleet-ai/apps/starfleet-policy-guard/lane.py --cwd /product/worktree upgrade-plan
python3 /clean/starfleet-ai/apps/starfleet-policy-guard/lane.py --cwd /product/worktree upgrade \
  --plan-sha256 REVIEWED_PLAN_SHA256
python3 /clean/starfleet-ai/apps/starfleet-policy-guard/lane.py --cwd /product/worktree attestation-rereads
python3 /clean/starfleet-ai/apps/starfleet-policy-guard/lane.py --cwd /product/worktree attestation-render \
  --evidence /private/delivery-evidence.json --stage repair-batch > /tmp/repair-batch-attestation.md
gh pr comment https://github.com/OWNER/REPO/pull/123 --body-file /tmp/repair-batch-attestation.md
python3 /clean/starfleet-ai/apps/starfleet-policy-guard/lane.py --cwd /product/worktree attestation-start-check \
  --evidence /private/delivery-evidence.json --stage repair-batch \
  --comment-url 'https://github.com/OWNER/REPO/pull/123#issuecomment-NEW'
```

The reviewed source must be clean and committed as required by `upgrade-plan`.
Preserve the existing batch/history references and required-read workflow, add the new assessment, and leave the prior comment intact.
The repair-batch attestation is prerequisite evidence before work, not final candidate proof.
After implementation, run canonical verification and record a fresh report for the resulting candidate before covered delivery.

Under the published policy, an adopted target refuses covered ready or merge unless its current packet attestation, exact local reread bytes and per-PR publication reference match.
Under that policy, each covered action reads the exact authenticated GitHub comment using the captured `gh` executable with a bounded timeout; unavailable, edited, deleted or mismatched comments fail closed.
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

## Retire a missing attachment

Use `retire-attachment` when a recorded checkout path no longer exists or no longer identifies the recorded repository worktree.
Any local operator can run this command without the recorded owner's session.
Select one seat with global `--seat-id SEAT_ID` or use a surviving attachment.
Pass the absolute checkout path shown in the seat record.
An older installed runtime may still require the former owner.
In that case, use `lane.py` from a current Starfleet source checkout for the same preview and apply commands.
The default command prints a plan with the seat, attachment path, recorded repository and branch, pull request records, missing-checkout evidence, and preimage hashes.
Review that output, then apply only with `--apply --plan-sha256 REVIEWED_PLAN_SHA256`.
For a live worktree with the recorded Git identity, use the [live registration workflow](#retire-a-live-attachment-registration) or run the existing owner-bound `uninstall` workflow from that checkout.
Changing the Git remote leaves the worktree identity intact.
It refuses a checkout that still has the recorded worktree identity but is on a different branch.
For that case, restore the recorded branch and run `uninstall` from the checkout.
It refuses while a seat install, migration, handoff, or upgrade is incomplete and names the applicable recovery command.
It never creates, deletes, or modifies a product checkout, branch, or repository metadata.
A successful apply reports `retired: true` and `history_preserved: true`.
The receipt records the historical seat owner, retirement time, and checkout evidence.
The report history, PR bindings, and counts remain available.
A transfer of live work or seat ownership still uses its existing handoff workflow.
`status` and `coverage --summary` from a surviving attachment show retired checkout paths and the preserved pull request records.

For example, a temporary checkout and its branch can both be deleted while another attachment remains live:

```sh
python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID retire-attachment \
  --checkout /worktrees/temporary-feature
python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID retire-attachment \
  --checkout /worktrees/temporary-feature --apply --plan-sha256 REVIEWED_PLAN_SHA256
python3 /path/to/clean/starfleet-ai/apps/starfleet-policy-guard/lane.py \
  --cwd /worktrees/remaining-feature upgrade-plan
python3 /path/to/clean/starfleet-ai/apps/starfleet-policy-guard/lane.py \
  --cwd /worktrees/remaining-feature upgrade --plan-sha256 REVIEWED_UPGRADE_PLAN_SHA256
```

If `/worktrees/temporary-feature` still exists as its recorded worktree but is on another branch, use the manual restore route instead:

```sh
git -C /worktrees/temporary-feature switch RECORDED_BRANCH
python3 /path/to/installed/runtime/lane.py --cwd /worktrees/temporary-feature uninstall
```

## Upgrade an installed seat

Use a clean, committed newer Starfleet source checkout and run the reviewed upgrade from an active attached product checkout:

`upgrade-plan` reads the executing installer source's Git revision, while the product checkout supplies the installed seat state.
An immutable runtime has no Git-backed source checkout and cannot produce an upgrade plan.
If `setup_guidance.reason` says the executing CLI has no clean committed source and `setup_guidance.command` is null, no exact compatible source entrypoint is recorded.
Choose a verified matching clean committed newer Starfleet source checkout before planning.
Policy Guard does not search local directories or fetch sources to guess compatibility.

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

### Replace an absent permanent seat root

`recover --inspect` reads the seat, while `recover --plan` can repair only its recorded runtime, adapter, or exact hook files.
It does not move a Git worktree identity.
Use `upgrade-plan --replacement-seat-root` only when the immutable birth root and recorded birth Gitdir are both absent.
The transition preserves the source seat ID, birth root, birth Gitdir, owner history, PR history, and removed attachment states, then records a separate active operational anchor for live Git and settings checks.
Same-repository proof comes from a retained attachment row for the birth root whose recorded Gitdir matches the immutable birth Gitdir and whose recorded common Git directory matches the replacement worktree's actual common directory.
An owner match, remote URL, host label, or repository name cannot substitute for that recorded proof.
Every active source attachment must exist and match its recorded worktree; review and apply the existing `retire-attachment` plan for each missing active attachment first.
If another seat already owns the replacement root, its exact ID must be supplied with `--release-anchor-seat`.
The destination seat must have no pending transition, an intact runtime and adapter pin, intact owned hooks, and only attachment rows already marked removed.
An absent active destination attachment still blocks release until an operator retires it through `retire-attachment`.
The transaction removes the destination seat's exact owned hook groups, adds the source seat's current groups when that harness manages hooks, upgrades the source seat, and leaves the released seat's runtime pin and history intact.
The two seat IDs are never merged, and removed attachment state bytes are not rewritten.
The normal upgrade receipt policy applies: prior receipts are archived and each active attachment needs a fresh report before covered actions can pass.

Review and apply the operation from the clean source checkout and an active source-seat product attachment:

```sh
python3 /clean/starfleet-ai/apps/starfleet-policy-guard/lane.py \
  --cwd /product/worktree --seat-id SOURCE_SEAT_ID upgrade-plan \
  --replacement-seat-root /worktrees/permanent-anchor \
  --release-anchor-seat DESTINATION_SEAT_ID
python3 /clean/starfleet-ai/apps/starfleet-policy-guard/lane.py \
  --cwd /product/worktree --seat-id SOURCE_SEAT_ID upgrade \
  --replacement-seat-root /worktrees/permanent-anchor \
  --release-anchor-seat DESTINATION_SEAT_ID --plan-sha256 REVIEWED_PLAN_SHA256
```

When no seat owns the replacement root, omit `--release-anchor-seat`.
Future `plan` and `install` commands at the moved operational anchor must put global `--seat-id SOURCE_SEAT_ID` before the action so Policy Guard does not derive a new ID from the replacement path.
The released destination remains available for read-only status and history inspection.
An older pinned executable cannot understand a later release marker, so use a current supported source checkout for later state changes and do not treat the old binary as enforcing the marker.
If an upgrade is interrupted, use `upgrade-recover --rollback` from the recorded product checkout while the replacement worktree still has the Git identity in the journal.
Rollback restores the exact state, settings, adapter, settings backup, and both seat manifests without recreating the missing birth root.

### Retire a drained durable seat

Use `retire-seat` only after every recorded attachment is already removed and an existing explicit authorization names this local seat cleanup.
The command requires the exact historical seat owner and separately checks the actual native operator running it.
The operator may differ from the historical owner, and neither identity is rewritten by this operation.
The authorization reference is recorded for review but is not authenticated by Policy Guard.
`--seat-id SEAT_ID status` reports a drained seat whose adapter or managed hook groups remain installed and gives the conditional next action.
The read-only plan binds the active Git anchor, complete runtime pin, captured `gh` identity, adapter bytes and mode, current settings bytes and mode, exact owned hook groups, every removed attachment receipt, and the expected operator.
It reports settings paths and hashes without printing unrelated settings content.
Review the plan, then apply only the exact digest:

```sh
python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID retire-seat \
  --expected-owner-harness claude --expected-owner-session HISTORICAL_SESSION_ID \
  --expected-operator-harness codex --expected-operator-session OPERATOR_SESSION_ID \
  --authorization-ref EXISTING_AUTHORIZATION_REFERENCE --confirm-retirement
python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID retire-seat \
  --expected-owner-harness claude --expected-owner-session HISTORICAL_SESSION_ID \
  --expected-operator-harness codex --expected-operator-session OPERATOR_SESSION_ID \
  --authorization-ref EXISTING_AUTHORIZATION_REFERENCE --confirm-retirement \
  --apply --plan-sha256 REVIEWED_PLAN_SHA256
```

The apply removes only the selected seat's exact adapter file and owned hook groups that appear once in its recorded active settings path.
It releases that seat's operational anchor and preserves its owner, immutable runtime pin, attachment rows, PR records, and report history.
It does not change product Git checkouts, branches, or upstream PRs.
If the operation is interrupted, `recover --inspect` shows the private retirement journal and `recover --rollback` restores the exact manifest, settings, and adapter preimages for the native operator that began the operation.

```sh
python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID recover --inspect
python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID recover --rollback
```

The receipt proves only the local file changes.
It does not prove that a running native host refreshed its hook configuration or stopped invoking a cached hook.
Use real receiving evidence or a supported owner reload at a safe boundary to confirm native refresh, and do not force a restart.

## Retire a live attachment registration

Use `retire-attachment --live-registration` when the recorded permanent-seat checkout still exists.
Any local operator can review and apply the plan.
Coordinate an idle point for that checkout with any active writer before you apply.
The plan checks the exact Git worktree, repository, branch, and current head.
If any bound record or checkout evidence changes, review a fresh plan.

```sh
python3 /path/to/current/lane.py --seat-id SEAT_ID retire-attachment \
  --checkout /worktrees/finished-feature --live-registration
python3 /path/to/current/lane.py --seat-id SEAT_ID retire-attachment \
  --checkout /worktrees/finished-feature --live-registration --apply --plan-sha256 REVIEWED_PLAN_SHA256
python3 /path/to/current/lane.py --seat-id SEAT_ID status
```

Check that the exact attachment reports `retired` and shows its preserved PR records.
The operation writes only the local registration and receipt.
Shared hooks and the seat adapter remain installed for other attachments.
If the seat is now drained, use [the drained-seat retirement workflow](#retire-a-drained-durable-seat) to detach its exact hooks and adapter.
Use the existing owner-bound `uninstall` workflow for legacy per-worktree installations, whose registrations and hook ownership do not use the permanent-seat transaction.

## Inspect, hand off, and recover

Run `python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID status` to inspect a permanent seat even when product checkouts are unavailable.
Status reports the immutable birth root separately from the active operational anchor, the pinned runtime CLI, recorded or prospective Claude settings path, and each checkout's active, missing, changed, or retired worktree state.
When run from an unbound permanent root, `permanent_seat_hint` identifies a unique registered seat only when its active root and recorded Gitdir match exactly.
The accompanying `setup_guidance` still describes the current product checkout attachment, so do not create another seat when the hint says the permanent seat is already registered.
Run `python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID registrations` to inspect attached PR history without a product checkout.
Run `coverage --summary` or `status` from an attached checkout to inspect local configuration and observations.
Configured hooks and adapters are not proof that the native runtime loaded or used them.
For a seat handoff, the outgoing owner prepares the exact seat ID, every active and retired attachment, covered PRs, pending work, and any missing worktree in the existing checkpoint.
Retire missing attachments through the reviewed `retire-attachment` plan before handoff if they block it.
Record the authorized release to the exact incoming owner in the existing checkpoint, with the outgoing owner recording it when available.
If the outgoing owner is unavailable, resolve the scope through the applicable existing authority process and hold the dependent handoff until that disposition is recorded.
The incoming owner records acceptance of that same scope before the rebind.
When `handoff.rebind_owner_command` is present, the current incoming native session should use that exact command.
If the installed CLI is unavailable or outdated, the documented same-harness maintenance fallback below remains supported when its full runtime-pin checks pass.
Run a rebind only after the authorized scope transfer is recorded and the incoming owner has accepted it.
The status command is read-only and does not supply transfer authority or establish a new owner.
For a drained seat, `seat_retirement` reports whether the old hook and adapter remain on disk and gives a conditional plan command only when the exact owner, actual operator, runtime pin, and anchor are available.
Retirement removes local files only; a cached native hook can remain active until a supported host reload or real receiving evidence confirms refresh.
The rebind receipt lists every changed and retained checkout by branch and PR, with retired history preserved and active missing worktrees identified without guessing at their owner.
For cross-harness handoffs, review the exact Claude settings path, before and after hashes, and managed hook groups in `settings_receipt`.
The rebind receipt archives prior reports where present and identifies active rebound checkouts with covered PRs that need a fresh report.
After rebind, run each available command in `verification.coverage_commands` to inspect local coverage and report state.
Use actual native tool output when a relevant action has occurred, but do not create a production action or restart solely to validate a handoff.
If no native event occurred, record that interception remains unobserved; `verification.observed.state` is a static note about the rebind and does not update later.
The supported explicit adapter probe is non-forwarding and does not prove native hook interception.
If any custody, permission, trust, or checkout state is uncertain, keep the scope held and resolve it through the existing checkpoint process before rebind.
If the installed CLI entrypoint is unavailable or outdated, a current source checkout can run this command for an existing permanent seat after an authorized same-harness transfer, provided the seat-recorded runtime remains present and intact:

```sh
python3 /path/to/current/starfleet-ai/apps/starfleet-policy-guard/lane.py \
  --seat-id SEAT_ID rebind-owner --from-harness claude \
  --from-session OUTGOING_SESSION_ID --confirm-handoff
```

That path verifies the seat-recorded immutable runtime path, version, complete file manifest and hashes before checking active attachment pins.
It preserves the installed runtime pin, adapter and hook targets, and leaves removed attachment records unchanged.
It does not upgrade or repair the seat runtime; changed or missing runtime files fail closed for recovery.
Cross-harness handoffs still require the executing source to match the complete seat-recorded runtime.
`--from-harness` and `--from-session` identify the exact outgoing owner; the incoming harness and session are detected from the current native runtime.
Handoff preserves history and requires a fresh report from the incoming owner.
For a cross-harness handoff, the transaction adds or removes only the seat-owned Claude hook pair as needed and preserves unrelated settings.
Coordinate the existing single-writer settings checkpoint before a Claude settings change, and preserve native trust requirements.
The settings change records configuration only and does not prove that the incoming native runtime loaded or trusted the hook.
If the seat `gh` entrypoint, Claude hook or a non-CLI runtime file is missing while the installed `lane.py` is intact, run `recover --inspect`, review `recover --plan`, then apply its hash with `recover --apply --plan-sha256 PLAN_SHA256` through the installed runtime.
Released history-only seats cannot use `recover --plan` or `recover --apply` to restore live hooks or adapters.
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
