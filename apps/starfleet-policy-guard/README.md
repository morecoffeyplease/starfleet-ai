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
For current limitations and exact supported paths, use the printable guide and each command's help.
