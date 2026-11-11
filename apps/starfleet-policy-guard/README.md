# Starfleet Policy Guard

Policy Guard provides a local delivery report and optional native-owner seats for supported GitHub CLI paths, including an opt-in OpenCode plugin source candidate for bounded Prime Directive actions.
Reports organize audit, target-awareness, conflict and canonical verification evidence without running those checks or granting readiness, merge or deployment authority.
The seat can gate only its documented command paths; it does not intercept arbitrary scripts, other applications, web/API actions or commands outside its adapters.

## Install and inspect

Use Python 3.10 or newer, Git and the GitHub CLI.
Print the setup guide before changing a native installation:

```sh
python3 apps/starfleet-policy-guard/lane.py guide --print
```

The guide documents the read-only plan, explicit install, checkout attachment, owner handoff, recovery and cleanup commands.
Use the exact current native Claude or Codex session identity in those CLI flows; never infer ownership from a title.
Installation preserves native trust requirements and existing settings outside the owned hook.
It does not start a service or create GitHub records.
`status` and `coverage` include one read-only setup recommendation based on the recorded attachment, owner, source pin, adapter and upgrade journal.
The recommendation is advisory, and an intact legacy installation does not need migration solely because it is legacy.
Standalone reporting and proof-only work do not require enrollment.
A recorded report is a covered-delivery requirement, not proof of setup or a prerequisite to attach a checkout.
`status --session-id` selects recorded activation events; it does not identify the actor or establish lane ownership.

## Optional published guidance attestations

Prime Directive (Policy Guard) is the same delivery tool described by this README and its `lane.py` CLI.
An owner can explicitly adopt its stage-specific opening and accepted repair-batch attestations for a target; older attachments remain unchanged until adoption.
The [published guidance attestation guide](guide.md#published-guidance-attestations) documents packet fields, exact-comment validation, the `--enable` adoption command and source/runtime upgrade requirements.
When Pattern Buffer is installed with a required-reads list, the owner must include current matching read receipts in the attestation; `attestation-rereads` prints the required packet entries and leaves `source_ref` for the owner to supply.
An attestation requires `assumptions: []`; unknown required facts must be investigated or raised to the configured human on Discord, then resolved before attesting.
The packet can record honest uncertainty about tool limits and future outcomes, but it cannot prove that the author's reasoning contains no hidden assumptions.
Existing immutable seats must use the reviewed source-upgrade flow before they can run the new pinned attestation commands.
Adopted targets require a current per-PR comment reference before covered ready or merge actions, with source and comment freshness checked on each supported action.
Codex coverage is through the explicit seat `gh` entrypoint and Claude coverage is through its supported direct ready/merge hook path; raw push, PR creation, comments and alternate tools are not intercepted.
The notice comes from Pattern Buffer's existing SessionStart context injection, which does not detect issues or batches.

## OpenCode 2.2.5 prototype

The local prototype targets only the OpenCode 2.2.5 server plugin API and the dedicated `prime_directive_manage` and `prime_directive_gh` tools.
The plugin binds identity from the actual server tool context and OpenCode session API result, including the exact session, parent, project and location.
An `OPENCODE_SESSION_ID` environment value does not enroll a native owner.
Configure the immutable plugin directory returned by `plan` or `install` as a local directory in the OpenCode `plugins` setting.
The dedicated GitHub tool first requests OpenCode's native `shell` permission for the equivalent command resource, then checks the owner, checkout, PR, source and fresh report immediately before forwarding.
Configured native asks and denies remain effective.
Direct plain-shell `gh pr ready` and `gh pr merge` commands are refused.
Wrapped commands, opaque shell syntax, alternate executables, scripts, APIs, web and other tools remain outside this bounded coverage.
For setup and handoff details, use the printable guide.
Source and fixture checks do not qualify a native OpenCode runtime or establish a public support claim.

## Inspect a lane before a PR exists

A native-owner lane may be attached before its product branch has a PR.
In that state, `status` reports `lane_enrolled: true`, `installed_here: true`, and no `pr_url` when the active attachment's product root and branch match the current checkout.
An inactive state or a different root or branch reports `installed_here: false`, and `coverage` labels the binding `inactive-local-binding`.
`installed_here` reports checkout configuration only; inspect `current_owner` and `activation` separately because a configured adapter does not prove that the native runtime loaded it.
Covered delivery remains PR-bound and requires a published PR binding and a recorded delivery report.

Use the owning native session to inspect the attachment and the separate activation observations:

```sh
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree status --session-id ACTUAL_NATIVE_SESSION_ID
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree coverage --summary
```

## Report and delivery boundaries

A delivery report records evidence, gaps and its exact candidate revision.
The configured human approval authority label is explicit in `config/delivery-checklist.json`.
Approval claims require an accepted channel and a decision record; the program records the claim but does not authenticate its author.
Peer relays alone cannot authorize a deviation or ownership change.
Local verification is evidence for the recorded source and verification inputs, not a substitute for required CI, review, readiness approval or merge authority.
The reported verification command must exactly match the canonical command documented by the repository.
Policy Guard neither guesses a package manager nor runs the command while reporting.
Configured installation and adapter state are not proof that a native runtime loaded the hook or that a message was delivered.
The OpenCode plugin uses a local JSON CLI bridge that does not authenticate its event payload cryptographically, so this remains a cooperative workflow guard rather than an adversarial local-process boundary.
Native plugin loading and covered action outcomes remain unqualified; see the [setup guide](guide.md) for the bridge limitation and the exact acceptance boundary.

The separate `guard.py` workflow uses the local policy in `config/default-policy.json` and is distinct from the delivery-only seat.
Its fixture workflow does not make filesystem access a security boundary and does not install into a lane through the seat installer.
The maintained [review and fix cycles guide](guide.md#review-and-fix-cycles) documents shared judgment, attempt counting, zoom-out timing, the existing fixture assessment schema, and approval limits.
It complements the fixture's existing assessment obligations without changing its schema or counting rules.
The delivery seat does not automatically enforce fix-round decisions, and neither the guide nor fixture workflow authenticates a user's approval.
For current limitations and exact supported paths, use the printable guide and each command's help.

## Migrate and recover an installed seat

`migrate-plan` and `migrate` record the target seat's Claude settings mode and immutable runtime pin in the migrated checkout state.
The migration plan also records the postimage hash for the target settings file, including when the seat already owns its hooks.
For a checkout migrated before those fields were recorded, run the reviewed `upgrade-plan` and `upgrade` flow from a clean newer source checkout.
The plan may restore missing runtime metadata only when the checkout's immutable source path and pinned file fingerprints match the owning seat runtime.
It may correct a stale settings mode only when that checkout records the same settings path and content hash, and the live mode matches the durable seat settings metadata.
Review every `migration_metadata_recovery` entry and apply only its exact plan hash.
For this metadata recovery path, changed settings content or mode, mismatched paths, invalid pins, and malformed runtime metadata continue to fail closed.

## Retire a missing attachment

An exact seat owner can retire a recorded attachment when its checkout path is gone or now belongs to a different Git repository or worktree.
The command refuses a live matching checkout and directs the owner to run `uninstall` from that checkout.
It also refuses a recorded worktree that exists on another branch, and refuses while an install, migration, handoff, or upgrade recovery is pending.
It changes only the Policy Guard attachment record, preserves pull request records and their counts, and records the owner, time, and missing-checkout evidence.

For example, review the plan for a deleted temporary checkout, then apply only its exact hash:

```sh
python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID retire-attachment \
  --checkout /worktrees/temporary-feature
python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID retire-attachment \
  --checkout /worktrees/temporary-feature --apply --plan-sha256 REVIEWED_PLAN_SHA256
python3 /path/to/clean/starfleet-ai/apps/starfleet-policy-guard/lane.py \
  --cwd /worktrees/remaining-feature upgrade-plan
```

If the recorded checkout still exists on another branch, restore its recorded branch and run `uninstall` from it instead of retiring it.
Use the [Policy Guard guide](guide.md#retire-a-missing-attachment) for the full plan, apply, upgrade, and recovery workflow.
