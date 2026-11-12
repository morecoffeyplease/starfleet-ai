# Starfleet AI

Starfleet AI helps you stay out of the weeds while coding agents work.
Agents can bring important checkpoints and decisions to you in Discord, with a separate thread for each conversation.
CMUX provides the workspace and session foundation; Starfleet adds tools for context, attention, coordination, and review.

Starfleet is source-only.
The repository does not promise publication to npm, PyPI, Homebrew, or another package registry.

## What is included

| Component | What it does |
| --- | --- |
| [Hailing Frequencies](apps/hailing-frequencies/README.md) | Watches explicitly enrolled CMUX sessions and submits guarded attention prompts. |
| [Agent Notify](apps/hailing-frequencies/agent-notify-guide.md) | Opens one Discord forum post per conversation and can register the thread for its exact enrolled owner. |
| [Replicator](apps/replicator/README.md) | Prints the maintained guide for an explicitly requested orchestrator handoff. |
| [Discord Watch](apps/discord-watch/README.md) | Uses one reply worker to queue responses from registered Discord threads for their exact enrolled owners. |
| [Away Mode](apps/away-mode/README.md) | Records explicit availability and routes human contact through the configured Discord workflow. |
| [Pattern Buffer](apps/pattern-buffer/README.md) | Restores worktree context for Claude Code and Codex sessions, with opt-in OpenCode context, checkpoint support and lower-anchor pressure reminders. |
| [Model Preferences](apps/model-preferences/README.md) | Saves local model preferences for supported agent roles. |
| [Warp-drive](apps/warp-drive/README.md) | Reserves local CPU capacity for agent-owned commands. |
| [Starfleet Policy Guard](apps/starfleet-policy-guard/README.md) | Records local delivery evidence and provides separately scoped workflow tools, including an opt-in OpenCode Prime Directive plugin candidate. |
| [Starfleet Markup](apps/starfleet-markup/README.md) | Reviews local HTML, Markdown, and images with versioned comments. |
| [Context Injector compatibility](apps/context-injector/README.md) | Preserves a compatibility path for the former Pattern Buffer name. |

Each component has its own setup, supported platforms, verification command, and limits.
Start with the component README and its printable guide when available.
For an explicitly requested handoff, read the [Replicator README](apps/replicator/README.md) and print its guide from a checkout with `python3 apps/replicator/replicator.py guide --print` or from an installed copy with `replicator guide --print`.
Installation is opt-in, and components do not start workers or change hooks merely because the source is present.
The [OpenCode and local Qwen setup guide](docs/opencode-qwen-setup.md) records prerequisites, isolated configuration and qualification limits, with the [runtime lifecycle runbook](docs/qwen-runtime-lifecycle.md) for bounded startup and shutdown.
The [Qwen operating guide](docs/qwen-operating-guide.md) covers task framing, supervision, evidence, secrets and escalation for orchestrators and implementers.
Qwen dispatch requires accepted Pattern Buffer pressure-warning evidence for the selected mode and an installed, enabled Policy Guard route for each covered action; see the runbook for scope and limits.

## Prerequisites

CMUX is required for Starfleet's coordinated session and attention workflow.
Read the selected component documentation for its required CMUX capabilities, supported harnesses, operating systems, and Python version.
Python 3.10 or newer is required by the repository-wide verification suite.
Starfleet Markup requires Node.js 22 or newer for its local source workflow.

## Set up an orchestrator with all Starfleet tools

When the user asks an orchestrator to install or set up Starfleet, cover every component listed above unless the user names a smaller tool set or explicitly leaves a tool disabled.
Do not stop after Pattern Buffer and Hailing Frequencies.

1. Read each component's maintained setup and usage instructions, including its installed `guide --print` command when available.
2. Install each tool that provides a runtime, or verify that its existing installation is current for the coordinated release.
   Use the component's supported installer and preserve existing configuration and owned state.
3. Complete every supported registration for your actual native session, worktree and orchestrator role.
   Reuse unchanged registrations, and use the documented owner handoff when replacing a session.
   Register packaged skills with the current harness where the component requires agent discovery.
4. Verify each installation and registration with the component's documented status, doctor or enrollment command.
   Check shared worker health without starting a second worker.
5. Report each component as ready, blocked or without a registration step for this role.
   Name missing credentials, unsupported capabilities, explicit holds and required native trust interactions instead of silently skipping a tool.
   Keep configured readiness separate from observed message or feedback receipt.

Use the following checklist with the linked component instructions.

| Tool | Orchestrator setup and registration |
| --- | --- |
| [Hailing Frequencies](apps/hailing-frequencies/guide.md) | Enroll the exact native session and surface, then verify the existing worker and intended send coverage. |
| [Temporal Beacon](apps/hailing-frequencies/temporal-beacon.md) | Verify the installed command, register its packaged skill and check its shared Hailing enrollment and worker; no second daemon or test timer is needed. |
| [Agent Notify](apps/hailing-frequencies/agent-notify-guide.md) | Verify the command and configured Discord route, and register its packaged skill; keep credentials private. |
| [Discord Watch](apps/discord-watch/guide.md) | Verify the existing reply worker and the exact ownership of each real conversation you open or inherit through a supported handoff. |
| [Pattern Buffer](apps/pattern-buffer/guide.md) | Set up the approved mission and standing context, then enroll the actual orchestrator's checkpoint and verify its recovery configuration. |
| [Model Preferences](apps/model-preferences/README.md) | Read the saved profile, check startup against the intended profile, and enroll and acknowledge adoption from the actual orchestrator session. |
| [Away Mode](apps/away-mode/guide.md) | Enroll the actual lane and read current presence; registration does not change the user's availability. |
| [Warp-drive](apps/warp-drive/guide.md) | Verify the command and existing scheduler, and use the exact enrolled owner for reservations; no separate per-session Warp enrollment or dummy job is required. |
| [Policy Guard](apps/starfleet-policy-guard/guide.md) | Complete the supported owner setup for the assigned checkout, including a pre-PR attachment when needed; preserve existing seats, pins and owner history. |
| [Starfleet Markup](apps/starfleet-markup/guide.md) | Verify the runtime and supported feedback adapter, and read the review workflow; create an owned review only when the user requests one. |
| [Replicator](apps/replicator/guide.md) | Install or verify the runtime and read the handoff guide; setup alone does not start a handoff or replace an agent. |

The Context Injector compatibility command belongs to the Pattern Buffer installation, not a second mission or worker.
Some tools register an owned resource when it exists, rather than registering the session globally.
Do not create fake PRs, reviews, conversations, jobs or timers to manufacture an enrollment receipt.
Reuse shared commands, hooks and workers, and preserve other agents, shared model profiles, human holds and machine-wide service policy.
A new shared service or an existing worker restart still requires coordination with its owner.
Implementers follow their parent's dispatch and task-replay instructions instead of enrolling an orchestrator checkpoint or taking over the parent's resources.

## Choose a component

1. Read the component README and guide.
2. Confirm the required CMUX and agent-harness capabilities are available.
3. Reuse an existing supported installation when possible.
4. Follow the component's exact self-service setup and removal instructions.
5. Verify configuration with its doctor or status command before enabling any sending worker.

The installer for one component does not enroll sessions or activate workers for another component.
Preserve existing permissions, human-input holds, owned state, and native trust prompts.
A configured route does not prove a message reached or was read by its intended agent.
Policy Guard can attach its owner's checkout before a PR exists, while covered delivery actions remain PR-bound and require a report.
Its `status` and `coverage` commands show one read-only next setup recommendation; standalone reporting and proof-only work do not require enrollment.
For legacy seat migrations and reviewed recovery of an earlier migration, follow the Policy Guard [upgrade and migration guide](apps/starfleet-policy-guard/guide.md).

## Discord and human contact

Discord is the initial human-contact channel.
Each user configures their own Discord server and credentials.
The project does not require a shared Starfleet support server.

The [Agent Notify setup guide](apps/hailing-frequencies/agent-notify-guide.md) gives the complete self-service path for a private Discord Community server, Forum channel, five tags, incoming webhook, bot, least-privilege channel permissions, private credentials, diagnostics, exact-session Hailing enrollment, one Discord Watch worker, and a bounded post, reply, receipt, and closure check.
Install or update the `agent-notify` command with `python3 apps/hailing-frequencies/install.py`, then print its installed instructions with `agent-notify guide --print`.
The [agent-notify skill](apps/hailing-frequencies/skills/agent-notify/SKILL.md) keeps each conversation in its own Discord thread, while the existing [Discord Watch worker](apps/discord-watch/README.md) owns managed reply polling and delivery.

Keep bot tokens, webhook URLs, private messages, and local conversation state outside this repository.
Test one owned conversation at a time and verify the exact enrolled session receives the reply before enabling additional threads.
Configuration, submission, and observed receipt are separate facts.

## Verify the source

Run the full aggregate verification from the repository root:

```sh
python3 scripts/verify.py
```

The Python-only portion is also available when the Node.js browser suite is outside the verification scope:

```sh
python3 scripts/verify.py --python-only
```

The aggregate runner checks its own selection and matrix behavior, then uses each component's maintained verification and test commands.
Starfleet Markup verification uses its checked-in npm lockfile and a local Chromium browser.
The test suite uses synthetic data and does not contact a live model, CMUX session, or Discord account.
Hosted GitHub Actions runs each maintained Python command in its own job across Ubuntu and macOS with an overall parallel cap of four.
The existing Python verification checks require the complete component matrix and its preparation to succeed.
Hosted GitHub Actions is the routine CI path; local verification is welcome.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.
Use [SUPPORT.md](SUPPORT.md) for setup questions, bugs, and feature requests.
Use [SECURITY.md](SECURITY.md) for the private vulnerability reporting route.
Community-concern issues are public; read the warning in the form before submitting one.

## License

Starfleet AI is licensed under the [MIT License](LICENSE).

## Check Claude Code weekly usage

If the local Claude Code status line writes `~/.claude/cache/rate-limits.json`, read it with `cat ~/.claude/cache/rate-limits.json`.
This optional cache is written by local status-line configuration, such as `~/.claude/statusline-command.sh`; installing Starfleet does not create it.
Before using the percentage, compare its `ts` epoch seconds with the current time from `date +%s`.
If the cache is more than 180 seconds old, report its age and treat the reading as stale.
If the file or `rate_limits.seven_day` is missing, report "unavailable" rather than guessing.
For a fresh reading, `rate_limits.seven_day.used_percentage` is the percentage used, and `100 - used_percentage` is the percentage remaining.
Convert `rate_limits.seven_day.resets_at` to local time with `date -r <epoch-seconds>` on macOS.
This reports Claude Code weekly usage only, not Codex limits; never write, rotate or delete the cache.
