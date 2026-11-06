# Starfleet Policy Guard

Policy Guard provides a local delivery report and an optional native-owner seat for supported GitHub CLI paths.
Reports organize audit, target-awareness, conflict and canonical verification evidence without running those checks or granting readiness, merge or deployment authority.
The seat can gate only its documented command paths; it does not intercept arbitrary scripts, other applications, web/API actions or commands outside its adapters.

## Install and inspect

Use Python 3.10 or newer, Git and the GitHub CLI.
Print the setup guide before changing a native installation:

```sh
python3 apps/starfleet-policy-guard/lane.py guide --print
```

The guide documents the read-only plan, explicit install, checkout attachment, owner handoff, recovery and cleanup commands.
Use the exact current native Claude or Codex session identity; never infer ownership from a title.
Installation preserves native trust requirements and existing settings outside the owned hook.
It does not start a service or create GitHub records.
`status` and `coverage` include one read-only setup recommendation based on the recorded attachment, owner, source pin, adapter and upgrade journal.
The recommendation is advisory, and an intact legacy installation does not need migration solely because it is legacy.
Standalone reporting and proof-only work do not require enrollment.
A recorded report is a covered-delivery requirement, not proof of setup or a prerequisite to attach a checkout.
`status --session-id` selects recorded activation events; it does not identify the actor or establish lane ownership.

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

The separate `guard.py` workflow uses the local policy in `config/default-policy.json` and is distinct from the delivery-only seat.
Its fixture workflow does not make filesystem access a security boundary and does not install into a lane through the seat installer.
For current limitations and exact supported paths, use the printable guide and each command's help.

## Migrate and recover an installed seat

`migrate-plan` and `migrate` record the target seat's Claude settings mode and immutable runtime pin in the migrated checkout state.
The migration plan also records the postimage hash for the target settings file, including when the seat already owns its hooks.
For a checkout migrated before those fields were recorded, run the reviewed `upgrade-plan` and `upgrade` flow from a clean newer source checkout.
The plan may restore missing runtime metadata only when the checkout's immutable source path and pinned file fingerprints match the owning seat runtime.
It may correct a stale settings mode only when that checkout records the same settings path and content hash, and the live mode matches the durable seat settings metadata.
Review every `migration_metadata_recovery` entry and apply only its exact plan hash.
For this metadata recovery path, changed settings content or mode, mismatched paths, invalid pins, and malformed runtime metadata continue to fail closed.
