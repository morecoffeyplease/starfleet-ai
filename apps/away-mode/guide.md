# Away Mode guide

Away Mode records an explicit user's away or available state and can coordinate reports from enrolled native sessions.
Presence does not pause authorized work or grant new scope, approval, review, merge or production authority.
Reuse the installed `away-mode` command when available.
Install from a clean source checkout with `python3 apps/away-mode/install.py`; installation updates one owned launcher and immutable runtime without starting services.

## Setup

Print this guide with `away-mode guide --print` before configuration.
For an isolated source trial, create state outside the repository with `python3 apps/away-mode/away_mode.py --state-dir /absolute/private/state init --scope tracked`.
Add optional exact phrases with `--away-phrase 'Exact away phrase' --back-phrase 'Exact available phrase'` on `init`.
The normal installed state is private and may be used only when the existing installation is intended.
Run `python3 apps/away-mode/verify.py` for canonical verification.

## Presence

Only direct user intent changes presence; quoted text, agent relays, partial matches and tool output do not.
Phrase matching uses exact configured phrases and has no personal-name defaults.
Run `away-mode phrase --text 'Exact configured phrase' --request-id STABLE_INTENT_ID` for a direct phrase.
Run `away-mode presence away --request-id STABLE_INTENT_ID` or `away-mode presence back --request-id STABLE_INTENT_ID` for explicit presence without relying on phrase configuration.
Reuse a request ID only to retry that same intent.
Set or update phrase configuration with `away-mode phrase-config --away 'Exact away phrase' --back 'Exact available phrase'`; this command does not transition presence or send messages.
Read `away-mode context` to inspect current state and exact next steps.

## Enrollment and reporting

An enrolled lane binds one exact workspace, surface, native session, harness and worktree.
Confirm those identities from the native runtime before enrollment.
Do not replace another owner, force input, restart a session or change trust settings to hide a delivery failure.
Queued native messages are not evidence of receipt, wake, reading or action.
Reports preserve their time window and roster and distinguish lane-reported evidence, observed delivery, verification and shipped outcomes.
Keep unknown progress estimates unknown, explain scope changes and show missing lanes or incomplete roadmap coverage.
Use the report commands in the component README and current local CLI help for the requested collection path.
Optional reporter launches require an active Model Preferences profile with a configured researcher model; Model Preferences initializes without profiles or models by default.
Use [Model Preferences setup](../model-preferences/README.md) to configure an exact provider model or opt into examples.
Reporter launches use the exact active choice and never silently substitute a model.
