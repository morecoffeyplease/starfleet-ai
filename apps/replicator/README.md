# Replicator

Replicator packages a maintained guide and read-only preflight for an explicitly requested manual handoff between orchestrator sessions.
The [handoff guide](guide.md) has a copyable start-handoff alert, one checkpoint template, and a short checklist for each owner.
The person or agent initiating the authorized handoff sends the alert to the exact outgoing owner through native CMUX agent messaging.
Both owners can use `starfleet doctor --json` for the aggregate inventory and the guide's component references for exact owner checks.
Replicator does not send alerts, start agents or workers, schedule timers, or trigger handoffs.

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
The current CMUX snapshot provides caller identity and state.
Preflight reports model, effort, and effective permissions as `unknown`.
Use the [handoff guide](guide.md#verify-model-and-reasoning-effort) to verify the actual launch and native settings.
Run the read-only `pattern-buffer session-effort --harness HARNESS --session SESSION_UUID` when the exact native session exposes a model and reasoning effort observation.
If the result is unknown, use the other evidence choices in the handoff guide.

## Review an outgoing handoff

Add `--handoff-file /path/to/handoff-evidence.json` to preflight to report checkout acceptance and session retirement separately.
Use the [handoff evidence example](guide.md#review-checkout-acceptance-and-outgoing-retirement) to supply the exact checkout, acceptance receipt, and remaining resource dependencies.
The report lists each resource's affected actions and prints supported inspection commands where it can scope them to an exact ID.
An unknown Warp exit can appear as historical when the owner records evidence that neither action depends on it.
A live job can hold retirement while the checkout remains accepted.

The input file contains owner-supplied assertions and evidence references.
Copy references from verified records and keep them private.
Replicator reads the explicit file alongside its existing live CMUX and Git checks.
It does not fetch the referenced records, run component commands, or establish receipt, custody, or a safe exit.
The optional advisory report does not change the existing preflight exit codes.

## Preview missing Hailing registrations

Use `replicator retirement-preview --snapshot-dir PATH` during a handoff or a review of older registrations.
The [retirement preview guide](guide.md#preview-missing-hailing-registrations) shows how to collect the four private snapshots and supply exact retirement evidence.
Add `--json` for bindings, reasons, and exact pending Discord and Warp records.
The preview reads only the supplied files, works outside a checkout, and leaves services and snapshots untouched.
It lists bindings missing from a fresh Hailing scan and distinguishes a live session to recover, an unresolved record to hold, and a record ready for retirement review.
Open Discord posts with no pending replies do not hold the review.
The preview cannot verify a process exit, establish the absence of another owner's timer, or remove an enrollment.

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

## Verify

Run the focused component checks with:

```sh
python3 apps/replicator/verify.py
```

The tests install only into temporary directories and do not contact CMUX, Discord, or native agents.
