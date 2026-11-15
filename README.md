# Starfleet AI

Starfleet AI helps you stay out of the weeds while coding agents work.
Agents can bring important checkpoints and decisions to you in Discord, with a separate thread for each conversation.
CMUX provides the workspace and session foundation; Starfleet adds tools for context, attention, coordination, and review.

Starfleet is source-only.
The repository does not promise publication to npm, PyPI, Homebrew, or another package registry.

## What is included

| Component | What it does |
| --- | --- |
| [Starfleet Doctor](apps/starfleet/README.md) | Checks the maintained tool inventory without changing configuration, ownership, or worker state. |
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

## Set up tools for the task and role

Set up the tools needed for the assigned task and native role.
Reuse working installations and unchanged registrations before installing or configuring anything.
A source checkout does not require every component to be enabled.
When the user explicitly requests all Starfleet tools, cover the full checklist below.
Preserve an explicitly disabled tool and explain any dependent capability that remains unavailable.

1. Select the tools needed by the actual task, role and resources.
   For human contact, follow Agent Notify's companion Hailing Frequencies and Discord Watch requirements.
   For checkpoint recovery, follow Pattern Buffer's companion enrollment requirements.
   Use Warp-drive for scheduled local commands, Policy Guard for covered delivery, and Replicator for an authorized handoff.
   Use Temporal Beacon when a timed continuation is needed and Markup when a review document is requested.
2. Read the selected components' maintained instructions, including installed `guide --print` commands when available.
3. Verify existing installations and registrations through the documented read-only diagnostics.
   Install or update only what the selected work needs, using supported installers and preserving owned state.
4. Complete only registrations applicable to the actual native session, role, checkout or existing resource.
   Use the supported owner handoff when replacing a session.
5. Check required shared worker health without starting a second worker.
   Report relevant missing credentials, unsupported capabilities, human holds and native trust interactions.
   Keep configured readiness separate from observed delivery.

Treat the checklist as an inventory, not a requirement to create resources or repeat setup at every task.
For a selected tool set, use Starfleet Doctor's repeatable `--skip TOOL` option to exclude tools outside that set.
Required dependencies remain visible even when a tool is skipped.
Record the selected scope and any material gaps in the existing task handoff rather than creating a separate setup report.

Use the following checklist with the linked component instructions.

| Tool | Orchestrator setup and registration |
| --- | --- |
| [Starfleet Doctor](apps/starfleet/README.md) | Install the read-only aggregate checker and verify that `starfleet guide --print` works outside the checkout. |
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
The Starfleet Doctor inventory and read-only adapter tests are maintained in [apps/starfleet](apps/starfleet/README.md).
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

Run the canonical verification from the repository root:

```sh
python3 scripts/verify.py
```

The Python-only portion is also available when the Node.js browser suite is outside the verification scope:

```sh
python3 scripts/verify.py --python-only
```

The runner compares the candidate with the merge base of `origin/main`, including staged, unstaged and untracked inputs.
Use `--base COMMIT` to select the exact comparison base and `--plan` to inspect the selection without running checks.
Missing change information and unclassified paths select the full suite.
Use `--all` to request full verification explicitly.

Documentation-only changes run the runner's own tests, local Markdown links, portable-example privacy checks and whitespace checks.
They report runtime checks as unaffected, not as tests passed.
Component changes select that component and its transitive dependents from `scripts/affected.py`.
Shared runtime, hook and custody implementations, verification infrastructure and unknown inputs retain broad verification.
Update the dependency map when adding a cross-component dependency.

The selected runtime checks use each component's maintained commands.
Selected Markup verification uses its checked-in npm lockfile and a local Chromium browser.
The suite uses synthetic data without contacting live models, CMUX sessions or Discord accounts.
Hosted GitHub Actions uses the same exact-base selection and preserves the required Python and Markup check names.
An unaffected required check reports its scope and succeeds only after successful preparation.
Source scanning still runs for every candidate, including documentation-only changes.
Local portable-example checks do not replace the hosted credential scanner.
Native pilots and observed delivery remain separate evidence.

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
