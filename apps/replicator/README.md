# Replicator

Replicator packages a maintained guide, read-only preflight, one-shot successor intent configuration, a versioned handoff freeze, and a scoped Codex predecessor settings observation for an explicitly requested manual handoff between orchestrator sessions.
The guide describes the handoff procedure, preflight reports only the caller, worktree, and explicit input checks it can observe, and successor intent records a frozen choice for a configured seat and worktree.
Replicator does not start agents, workers, timers, or an automatic handoff trigger.

## Print the guide

Use Python 3.10 or newer.
From a source checkout, print help and the guide with:

```sh
python3 apps/replicator/replicator.py --help
python3 apps/replicator/replicator.py guide --print
```

After installing the local launcher, use:

```sh
replicator --help
replicator guide --print
```

The help and guide commands work outside a checkout without configuration, enrollment, credentials, native tools, or state writes.
The installed launcher reads the same packaged `guide.md` as the source command.

## Run the read-only preflight

Run preflight from the exact caller and target worktree.
Pass the context and guidance files whose bytes the successor must read:

```sh
replicator preflight \
  --context-file /path/to/standing-context.md \
  --guidance-file /path/to/AGENTS.md
```

Add `--json` for a structured report.
Add `--skip-tool NAME` for each tool the user explicitly excludes.
The report records each exclusion as `skipped` and does not enroll or inspect that tool.

Preflight uses the read-only `cmux current --json --limit 200` observation and bounded read-only Git commands.
It reads the supplied context and guidance files without printing their paths or contents.
It does not query tool doctors, prove native delivery, accept custody, or complete predecessor cleanup.
An unknown status means the required evidence is missing or ambiguous.
Keep the predecessor while a required handoff check remains blocked or unknown.
The current CMUX snapshot provides caller identity and state, but no model, effort, or effective permission fields.
Preflight reports those settings as `unknown` until an exact-session native control provides evidence.

## Configure a one-shot successor intent

Configure the exact registered Model Preferences seat and Git worktree before a separately authorized handoff.
Omit `--family` and `--effort` to record a request to preserve the predecessor's observed model and effort.
Pass both options to snapshot one existing Model Preferences family and supported effort without activating or changing a shared profile.

```sh
replicator successor configure --seat SEAT --worktree /absolute/path/to/worktree \
  --family codex-luna --effort xhigh
replicator successor show --seat SEAT --worktree /absolute/path/to/worktree
replicator successor show --seat SEAT --worktree /absolute/path/to/worktree --json
replicator successor cancel INTENT_ID
```

Show prints a concise human-readable summary by default and accepts `--json` for automation.
An explicit candidate summary separates its configured family, model, effort, active profile, active selection digest, and candidate digest from unknown native generation, predecessor settings, permissions, and readiness.
The configure and show commands accept `--mp-config-dir` and `--mp-state-dir` when the existing Model Preferences installation uses explicit paths.
The command resolves the seat and candidate through Model Preferences, which accepts only a configured family and supported effort for the registered harness.
Replicator stores the candidate and its separate provenance digest in a private state directory, and repeating the same configured request for the same seat and worktree is idempotent.
A different configured request for that scope requires cancelling the current intent first.
This interface supports one-shot intents only, and persistent defaults are unsupported.
The default preserve-observed request does not infer a predecessor model or effort from the saved profile, argv, or environment.
The stored native generation, predecessor model and effort, effective permissions, and readiness remain `unknown` until you record a supported exact-session observation.
The current CMUX item provides session and placement identity but no model, effort, or effective-permission fields.
Configuration does not bind a generation or establish runtime parity or readiness.
Configuration does not launch a successor, transfer custody, close a predecessor, or consume an intent.

## Freeze and inspect a versioned handoff

Freeze a configured intent once its exact registered seat and canonical worktree still match.
For an explicit choice, Replicator re-resolves the same configured family and effort through Model Preferences without activating a profile.
The candidate must remain unchanged and supported for the exact registered harness.
For preserve-observed, Replicator revalidates the seat only and does not infer a predecessor model or effort.

```sh
replicator handoff freeze INTENT_ID --worktree /absolute/path/to/worktree \
  --mp-config-dir /path/to/model-preferences/config \
  --mp-state-dir /path/to/model-preferences/state
replicator handoff show HANDOFF_ID
replicator handoff show HANDOFF_ID --json
replicator handoff reconcile HANDOFF_ID
replicator handoff reconcile HANDOFF_ID --json
```

Freeze stores one immutable `starfleet.replicator.handoff/v1` record and links it to the one-shot intent in the same atomic state-file replacement.
Repeating freeze for that intent returns the same handoff ID and handoff generation.
The application-owned handoff generation is distinct from the native process generation and CMUX hook generation.
Both native generations remain `unknown` until you record a supported exact-session observation.
Show separates configured Model Preferences choice and provenance from native observations and readiness.
Reconcile reads only the private local state, performs no native queries or writes, and reports the current native-observation blockers.
The frozen record leaves native model, effort, effective permissions, native bindings, readiness, transfer, and predecessor cleanup unknown, blocked, or not applicable as appropriate.
Cancel refuses a frozen intent, and another candidate cannot replace it.

## Record the Codex predecessor settings snapshot

Run the observation command from the exact registered Codex predecessor in the frozen worktree.

```sh
replicator handoff observe-predecessor HANDOFF_ID
replicator handoff show HANDOFF_ID --json
replicator handoff reconcile HANDOFF_ID --json
```

Replicator matches the caller's session, panel, workspace, and canonical worktree against the frozen scope and current CMUX item.
It matches the CMUX Codex hook record to a stable live process and the one open rollout for that session.
It rechecks process and file identity after it reads bounded session metadata and the latest native turn settings.
The native turn must follow the OS process start time.
Replicator records the process generation separately from the hook generation.
It records the turn's model, effort, approval policy, sandbox policy, source identity, and timestamp.
It does not accept a rollout path or observation JSON from the caller.
It does not expose conversation bodies.
An identical observation is idempotent, and a different observation for the frozen predecessor is refused.

The observer supports Codex CLI versions `0.166.0` and `0.169.0`.
It does not support other Codex versions or other harnesses.
The settings describe one observed turn and do not prove historical continuity, future settings, effective file or Git access, network access, or native trust.
Effective permissions remain `unknown`, so readiness stays `blocked`.
This command records settings only and does not launch a successor, transfer custody, close a predecessor, or consume the intent.

Run the passive configuration diagnostic outside a Git checkout:

```sh
replicator doctor --starfleet-json
```

The diagnostic performs no writes, locks, native queries, or credential reads.
The `checks` object maps each check name directly to its string status.
With no intent file, configuration is `ready`, registration, worker, and intended delivery are `not_applicable`, and observed delivery is `unknown`.
Valid diagnostic output exits successfully even when an individual check is blocked or unknown.

## Install the local launcher

Run the source installer only when you want the `replicator` command in your local command directory:

```sh
python3 apps/replicator/install.py \
  --runtime-root "$HOME/.local/share/starfleet-ai/replicator" \
  --bin-dir "$HOME/.local/bin"
```

The installer copies the CLI and guide into a content-addressed runtime and updates only its owned launcher.
It refuses a symlink or unowned launcher and preserves immutable runtime files.
It does not install a package globally, start a service, enroll a session, or alter CMUX configuration.
Run `python3 apps/replicator/install.py --help` for the supported local path options.

## Scope

Read the [manual handoff guide](guide.md) before using its checklist.
The guide keeps tool-specific custody checks in existing CMUX, Pattern Buffer, Hailing Frequencies, Temporal Beacon, Warp-drive, Discord Watch, Agent Notify, and Policy Guard controls.
Preflight does not aggregate those tool checks or register tools.
It does not qualify a Claude or Codex native pilot or prove that a successor received a handoff.
Each same-harness route needs its own native evidence.
An automatic activity-budget trigger remains a later increment that must use the same procedure.

## Verify

Run the focused component checks with:

```sh
python3 apps/replicator/verify.py
```

The tests install only into temporary directories and do not contact CMUX, Discord, or native agents.
