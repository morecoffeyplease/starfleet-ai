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
