# Starfleet orchestrator setup guide

Use this guide to review the Starfleet tools selected for an orchestrator role.
Starfleet components are independent installations and do not enroll sessions or start workers merely because their source is present.

## Install Starfleet Doctor

From a Starfleet source checkout, run `python3 apps/starfleet/install.py`.
The installer writes a sealed content-addressed runtime and the `starfleet` launcher under `~/.local` by default.
Use `python3 apps/starfleet/install.py --prefix PATH` to choose a different prefix.
Add the prefix's `bin` directory to `PATH`.
Run `starfleet guide --print` to print this guide without Git, configuration, credentials, state creation, or service access.

## Review the tool inventory

Read the root README setup checklist and each selected component's README and printable guide.
The maintained inventory includes Hailing Frequencies, Temporal Beacon, Agent Notify, Discord Watch, Pattern Buffer, Model Preferences, Away Mode, Warp-drive, Policy Guard, Starfleet Markup, Replicator, and Context Injector compatibility.
Context Injector compatibility is part of Pattern Buffer and does not require another worker or registration.
Temporal Beacon uses Hailing Frequencies state and its shared worker rather than a second daemon.
Warp-drive uses its shared scheduler and Hailing configuration rather than per-session enrollment.
Starfleet Markup review ownership applies only when a real review exists.
Replicator prints a requested handoff guide and does not start a handoff during setup.

Use `starfleet doctor` for a read-only inventory report.
Use `starfleet doctor --json` for a structured report.
Use `starfleet doctor --skip TOOL` for each tool the user explicitly leaves disabled.
The report distinguishes `ready`, `missing`, `blocked`, `unknown`, `not_applicable`, and `skipped`.
It separates configuration and intended delivery from observed native delivery.
It does not create enrollments, reviews, messages, timers, jobs, workers, or services to make a check pass.
No selected tools produces a `not_applicable` report with exit code zero and no setup-complete claim.

Caller-aware component diagnostics receive at most 16 KiB of UTF-8 JSON on piped stdin with this schema:

```json
{"schema_version":1,"caller":{"harness":"codex","session":"SESSION_ID","workspace":"WORKSPACE_UUID","surface":"SURFACE_UUID"}}
```

The caller object is configuration-matching evidence only; the aggregate doctor separately establishes the current CMUX identity.
Component diagnostics do not read caller data from an interactive terminal.

Exit code `0` means selected required checks are ready or not applicable.
Exit code `1` means required evidence is missing.
Exit code `2` means command arguments are invalid.
Exit code `3` means a required check is blocked.
Exit code `4` means required evidence is unknown.
An unknown check needs manual review through the next command printed by the doctor.

## Complete required component setup

For Hailing Frequencies, install the component, configure credentials only when the user has authorized the Discord workflow, enroll the exact native session, and inspect its existing worker and intended send coverage.
For Temporal Beacon, verify the installed command and packaged skill, then inspect its shared Hailing enrollment and existing worker without creating a timer.
For Agent Notify, verify its configured private Discord route and packaged skill without printing credentials or creating a conversation.
For Discord Watch, verify the existing reply worker and exact ownership of each real conversation opened or inherited through a supported handoff.
For Pattern Buffer, follow its mission and standing-context setup, then enroll the actual orchestrator's checkpoint only when enrollment is required for that role.
For Model Preferences, read startup readiness and complete adoption acknowledgment from the actual orchestrator session when it applies.
For Away Mode, read current presence and enroll the actual lane when the role requires one.
For Warp-drive, verify the command and existing scheduler, then use the exact enrolled owner for reservations without creating a dummy job.
For Policy Guard, complete the supported owner setup for the assigned checkout and preserve existing seats, pins, and owner history.
If its checkout-scoped CLI is not discoverable, pass a trusted executable path with `starfleet doctor --policy-guard-cli PATH` or follow the doctor next step.
For Starfleet Markup, verify the runtime and static feedback adapter, and create an owned review only when the user requests one.
For Replicator, verify the runtime and read its handoff guide; setup alone does not start a handoff or replace an agent.

Preserve existing permissions, human-input holds, owned state, and native trust prompts.
Do not use credentials or send a test message as part of a read-only diagnostic.
Configuration and submission do not prove that the intended recipient observed delivery.
