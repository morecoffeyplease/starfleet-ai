# Replicator

Replicator packages a maintained guide for an explicitly requested manual handoff between orchestrator sessions.
The component prints instructions and does not inspect or change sessions, work, resources, permissions, or owner registrations.
It does not start agents, workers, timers, or an automatic handoff trigger.

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
The guide reuses existing CMUX, Pattern Buffer, Hailing Frequencies, Temporal Beacon, Warp-drive, Discord Watch, Agent Notify, and Policy Guard controls.
It does not qualify a Claude or Codex native pilot or prove that a successor received a handoff.
Each same-harness route needs its own native evidence.
An automatic activity-budget trigger remains a later increment that must use the same procedure.

## Verify

Run the focused component checks with:

```sh
python3 apps/replicator/verify.py
```

The tests install only into temporary directories and do not contact CMUX, Discord, or native agents.
