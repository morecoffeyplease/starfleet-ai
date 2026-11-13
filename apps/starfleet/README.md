# Starfleet Doctor

Starfleet Doctor checks the setup of the tools in the repository's orchestrator inventory.
It reads local component diagnostics and the current CMUX identity without installing, enrolling, starting a worker, sending a message, taking a lock, or changing settings.

## Install

From a source checkout, run `python3 apps/starfleet/install.py` to install into `~/.local`.
Use `python3 apps/starfleet/install.py --prefix PATH` to select another prefix.
Add the prefix's `bin` directory to `PATH`.
The installer stores a sealed content-addressed runtime and an owned launcher in that prefix.
Printing the guide works before configuration with `starfleet guide --print`.

## Diagnose

Run `starfleet doctor` for a readable report or `starfleet doctor --json` for structured output.
Run `starfleet doctor --skip TOOL` to exclude a tool explicitly, and repeat the option for more tools.
Run `starfleet doctor --cwd PATH` to inspect a selected worktree.
The optional `--policy-guard-cli PATH` identifies a trusted checkout-scoped Policy Guard CLI for its read-only status command.
Without an available safe diagnostic interface, a check remains unknown and reports its supported next command.

The doctor checks the inventory in the root README and records configuration, exact-owner registration, shared worker health, intended delivery, and observed delivery independently.
An explicitly skipped tool is reported as skipped by user instruction and is excluded from completeness failures.
Required dependency checks remain visible when a dependency is skipped, missing, held, or unknown.
The doctor never creates a resource to test an enrollment or delivery path.
An installed command or valid component response alone does not prove setup readiness.
Configured delivery remains separate from observed message or feedback delivery.

Statuses are `ready`, `missing`, `blocked`, `unknown`, `not_applicable`, and `skipped`.
Exit code `0` means every selected required check is ready or not applicable, or all tools were explicitly skipped.
Exit code `1` means required evidence is missing.
Exit code `2` is reserved for invalid command arguments.
Exit code `3` means a required check is blocked.
Exit code `4` means required evidence is unknown.
When every tool is skipped, the report says no tools were selected and does not claim setup is complete.

## Extend

Update the adapter table and typed report in `starfleet.py` when adding a root inventory tool.
Use a component-owned read-only diagnostic interface and keep default component behavior unchanged.
Add focused synthetic tests that compare file bytes, modes, timestamps, locks, SQLite sidecars, and schema state before and after inspection.
Add the new focused test command to `scripts/verify.py` and its maintained test matrix.
