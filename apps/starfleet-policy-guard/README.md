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
