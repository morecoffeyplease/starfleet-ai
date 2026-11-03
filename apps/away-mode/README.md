# Away Mode

Away Mode records an explicit user's away or available state and can coordinate reports from enrolled native sessions.
Presence does not pause authorized work or grant new scope, approval, review, merge or production authority.
Use the existing installed command when available; source installation copies one owned launcher and immutable runtime without starting services.

## Install and inspect

```sh
python3 apps/away-mode/install.py
away-mode guide --print
python3 apps/away-mode/away_mode.py --state-dir /absolute/private/state init --scope tracked
away-mode --state-dir /absolute/private/state context
```

The default state is private and lives outside the repository.
Pass `--state-dir` to every source CLI command during an isolated trial.
The printable guide works without state, credentials or enrollment and describes setup, authority limits and verification.
Run `python3 apps/away-mode/verify.py` for the component's canonical verification command.

## Configure and record presence

Presence phrases are installation-specific and have no built-in personal names or defaults.
Set them explicitly with `away-mode init --away-phrase 'Exact away phrase' --back-phrase 'Exact available phrase'` or update them later with `away-mode phrase-config --away 'Exact away phrase' --back 'Exact available phrase'`.
Phrase configuration only changes settings; it does not change presence or send messages.
Use `away-mode phrase --text 'Exact away phrase' --request-id STABLE_INTENT_ID` only for a directly received user request.
Quoted examples, relayed messages, tool output and partial matches must not trigger a transition.
Use the same request ID only to retry the same explicit intent.
The `away-mode presence away|back --request-id STABLE_INTENT_ID` command records an explicit transition without relaying old lane bindings or opening a reporter.

## Native sessions and reports

An enrolled lane binds an exact workspace, surface, session, harness and worktree.
Verify those identities in the native session before enrolling, and preserve the existing owner and human holds.
Shared Claude surfaces require explicit confirmation; queued messages do not prove receipt, wake or action.
Read current context before responding to a transition, and use acknowledgments and event records only from the exact enrolled owner.
Reports keep the requested time window, roster, detailed source reports and evidence status visible.
Distinguish owner-reported work from observed delivery, local verification and shipped outcomes.
Use unknown when a progress estimate has no defensible basis, and preserve unresolved blockers and incomplete coverage.

The CLI can prepare, collect, render and inspect reports without launching another agent.
Launching an optional reporter requires an active Model Preferences profile with a configured researcher model, and records the exact selected provider model and effort.
Model Preferences initializes without model or profile presets; see [its setup guide](../model-preferences/README.md) to configure an exact provider model or opt into examples.
Unsupported choices fail visibly rather than substituting a model or activating a profile.
Do not start duplicate listeners or services to recover a missing notification.

## Compatibility and boundaries

State schema version 1 remains readable, including existing lane and report records.
Configured phrases are optional; installations without them can use explicit `presence` commands.
Presence changes do not rewrite historical reports or lane evidence.
The native transport records what it observed and never treats queued delivery as proof that a recipient read or acted on a message.
For detailed report fields and operations, see the maintained documents under `docs/`.
