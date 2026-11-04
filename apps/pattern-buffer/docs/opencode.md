# OpenCode context adapter

The OpenCode adapter is an opt-in pilot for the v2.2.5 plugin context and compaction hooks.
It renders Pattern Buffer shared context, the explicit session role, and an active task into the model system context immediately before primary and compaction requests.
The plugin reads the exact native `event.sessionID` and OpenCode `ctx.location.directory` on every callback.
It does not inherit a role or task from an OpenCode parent session ID.
The v2.2.5 source documents `ctx.session.hook("context", ...)` as a mutable pre-provider hook in the [plugin API README](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/plugin/src/README.md).
The same tag defines the exact session identity and mutable system parts in [session context types](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/plugin/src/promise/session.ts), and the project directory in [location types](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/schema/src/location.ts).
The [model request implementation](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/core/src/session/model-request.ts) calls separate `context` and `compaction` hooks while preparing requests.
The [hook dispatcher](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/core/src/plugin/hooks.ts) awaits each callback without swallowing its failure.
Recheck the exact OpenCode version source before using a later release.

## Current limits

The [native acceptance observations](../../../docs/opencode-qwen-acceptance.md) record initial context/task delivery, an updated task in completed compaction and a zero-provider-request failure control on the tested candidate.
They do not establish a post-compaction implementation turn, same-session resume or model-visible finished-task behavior.
An operator must run the separate native pilot in a disposable child worktree before relying on the integration.
Use an already existing same-task CMUX workspace and keep the native OpenCode session in a visible tab or pane.
Select the exact approved local provider and model in OpenCode itself, and verify that hosted providers and fallback paths are disabled for a local-only run.
Pattern Buffer does not select, pin, or verify the model or provider.
The context plugin is prompt delivery, not an operating-system sandbox or a guarantee that the model follows the context.
It launches a bounded local Python process without passing the OpenCode process environment wholesale, but this does not isolate other OpenCode tools or credentials.
OpenCode context recomputation covers both primary agent calls and the separate compaction model request.
The plugin throws when a bound child lacks its exact task/session state or when rendering fails.
Pinned v2.2.5 source awaits these callbacks before returning the prepared provider request, but the native pilot must still verify that a failed callback produces zero provider requests.
Checkpoint pressure detection, compaction telemetry, native session recovery evidence, and native delivery confirmation are not implemented here.
The existing synthetic trial evidence proves only its own candidate fixture, denied actions, API call, tests, and rollback.
Do not make a portable acceptance claim until the actual OpenCode candidate passes its acceptance task and its owner records the evidence.

## Explicit opt-in plugin setup

`hook-config` is read-only and prints a plugin path to merge into an OpenCode configuration file.
Prefer an isolated settings file during the pilot and pass it explicitly to every install, status, and uninstall command.
The adapter refuses to guess OpenCode's default settings path.
The installer updates only the exact plugin path it owns, preserves other settings, and retains runtime files on uninstall.

```sh
python3 apps/pattern-buffer/pattern_buffer.py hook-config --harness opencode --runtime-dir /absolute/path/to/pattern-buffer-runtime
python3 apps/pattern-buffer/pattern_buffer.py install --harness opencode --settings /absolute/path/to/isolated-opencode.json --runtime-dir /absolute/path/to/pattern-buffer-runtime
python3 apps/pattern-buffer/pattern_buffer.py status --harness opencode --settings /absolute/path/to/isolated-opencode.json --runtime-dir /absolute/path/to/pattern-buffer-runtime
python3 apps/pattern-buffer/pattern_buffer.py uninstall --harness opencode --settings /absolute/path/to/isolated-opencode.json --runtime-dir /absolute/path/to/pattern-buffer-runtime
```

Restart or reload OpenCode according to its own configuration behavior after editing settings.
Review and trust the plugin through the controls of the exact OpenCode version in use.
Installation and a configured plugin entry do not prove that the hook runs or that the provider receives its output.

## Two-phase task binding

Use a directly initialized owner worktree and a separate child worktree in the same Git repository.
Create one bounded task brief and a unique receipt path before opening the implementer session.
The first command creates a pending task and immutable receipt only, and never starts the OpenCode CLI or UI.

```sh
python3 apps/pattern-buffer/pattern_buffer.py --cwd /absolute/path/to/child prepare-dispatch \
  --harness opencode \
  --owner-worktree /absolute/path/to/owner \
  --task-file /absolute/path/to/brief.md \
  --receipt-file /absolute/path/to/unique-receipt.json \
  --role implementer
```

After opening the visible native OpenCode session in that exact child worktree, obtain its exact case-sensitive `ses_...` ID from the native session identity display.
Bind only that session and the receipt returned by preparation.

```sh
python3 apps/pattern-buffer/pattern_buffer.py --cwd /absolute/path/to/child bind-dispatch \
  --harness opencode \
  --session ses_EXACT_NATIVE_ID \
  --task-id TASK_ID_FROM_PREPARATION \
  --receipt-file /absolute/path/to/unique-receipt.json \
  --role implementer
```

Binding checks the exact task, child worktree, owner root and Git directory, receipt path and contents, prepared role, and native session identifier.
The prepared role is fixed by the ticket and cannot be changed during binding.
The receipt remains an immutable pre-bind snapshot and is not updated as the task changes.
If writing or sending the receipt fails after task creation, the pending task remains in the child Git metadata for inspection.
An unclaimed ticket expires after ten minutes; prepare a fresh ticket rather than reviving an expired receipt.
The native context and compaction callbacks use only their exact `sessionID` and current project directory, and never infer assignment through `parentID`.

## Session roles and task updates

OpenCode sessions do not inherit `default_role`; assign each non-dispatched session explicitly.

```sh
python3 apps/pattern-buffer/pattern_buffer.py --cwd /absolute/path/to/worktree assign \
  --harness opencode --session ses_EXACT_NATIVE_ID --role orchestrator
python3 apps/pattern-buffer/pattern_buffer.py --cwd /absolute/path/to/child task-status --task-id TASK_ID
python3 apps/pattern-buffer/pattern_buffer.py --cwd /absolute/path/to/child update-task --task-id TASK_ID --task-file /absolute/path/to/revised-brief.md
python3 apps/pattern-buffer/pattern_buffer.py --cwd /absolute/path/to/child finish-task --task-id TASK_ID
```

Active task updates and finishes are reflected on the next context callback.
They do not interrupt a running response or establish that the model received an update.
Pending OpenCode receipts are immutable, so a task that needs revision before binding requires a fresh prepare command and receipt.
Finished tasks stop replaying the brief and receive a finished notice.
Ordinary task lifecycle commands do not erase prior prompt text from native history.
Use `context --harness opencode --session ses_EXACT_NATIVE_ID` for a local rendering check.
The local rendering check is not proof of native hook activation or provider receipt.
