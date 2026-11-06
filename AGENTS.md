# Starfleet AI contributor instructions

These instructions apply to changes in this repository.
Read the affected component README and guide before changing its behavior.

## Product boundaries

CMUX is required for Starfleet's coordinated session workflow.
Discord is the initial human-contact channel, and each conversation keeps its own thread.
Components are independently installed and enabled.
Do not infer permission to enroll sessions, start workers, alter hooks, widen permissions, or change owned state from a source edit.

Use generic examples and portable paths in maintained documentation.
Do not add personal workstation paths, private project names, private account data, private review artifacts, or credentials.
Do not require private repositories, personal files, personal model settings, or access to another maintainer's Discord server.
Never commit tokens, webhook URLs, private message bodies, complete agent transcripts, or command logs.

## Changes and verification

Reproduce a user-facing bug through its actual supported path before changing code when the required environment is available.
Use synthetic data, disposable state, and scoped environments for routine development and verification.
Never run tests against another person's active sessions, Discord threads, or machine-wide services.
If native behavior cannot be reproduced, state exactly what remains unverified.

Run the canonical aggregate verification on the final candidate:

```sh
python3 scripts/verify.py
```

Use `python3 scripts/verify.py --python-only` only when the Node.js browser suite is intentionally outside the current verification scope.
Run `git diff --check` and report the exact command, candidate revision, result, and any unverified native behavior.
Configuration and a successful simulated submission do not prove actual recipient delivery.

Before preparing OpenCode for Qwen or relaying a parent/child task brief, read the [Qwen inference and test observer preflight](docs/qwen-operating-guide.md#inference-and-test-observer-preflight).
Claude, Codex, and Qwen orchestrators must also read the [observed orchestrator gotchas](docs/qwen-operating-guide.md#orchestrator-gotchas-from-observed-runs) before dispatching a Qwen task.
Before costly inference, pin the runtime and reuse a maintained or generated client, then check configuration and API shapes without a model request.
Replay sanitized event examples and review the observer's terminal-state, raw/rendered-content and failure-classification behavior before launch.

CI must run on GitHub-hosted runners with read-only repository permissions.
Do not add self-hosted runners, live model credentials, real CMUX sessions, Discord credentials, or personal transcripts to CI.
Keep workflow action references pinned to full commit SHAs.
Avoid path filters on required checks unless unaffected paths still produce the exact required check result.

## Documentation and generated files

Keep maintained Markdown readable with one sentence per physical line.
Use plain ASCII hyphens and avoid typographic dash characters.
Update the source or run its supported generator instead of hand-editing generated files.
Keep component-specific setup and technical detail in the component documentation, and link to it from root documentation.
