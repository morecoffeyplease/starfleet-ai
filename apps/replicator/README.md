# Replicator

Replicator packages a maintained guide and read-only preflight for an explicitly requested manual handoff between orchestrator sessions.
The guide describes the handoff procedure, and preflight reports only the caller, worktree, and explicit input checks it can observe.
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
