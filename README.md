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
| [Discord Watch](apps/discord-watch/README.md) | Uses one reply worker to queue responses from registered Discord threads for their exact enrolled owners. |
| [Away Mode](apps/away-mode/README.md) | Records explicit availability and routes human contact through the configured Discord workflow. |
| [Pattern Buffer](apps/pattern-buffer/README.md) | Restores worktree context for Claude Code and Codex sessions. |
| [Model Preferences](apps/model-preferences/README.md) | Saves local model preferences for supported agent roles. |
| [Warp-drive](apps/warp-drive/README.md) | Reserves local CPU capacity for agent-owned commands. |
| [Starfleet Policy Guard](apps/starfleet-policy-guard/README.md) | Records local delivery evidence and provides separately scoped workflow tools. |
| [Starfleet Markup](apps/starfleet-markup/README.md) | Reviews local HTML, Markdown, and images with versioned comments. |
| [Context Injector compatibility](apps/context-injector/README.md) | Preserves a compatibility path for the former Pattern Buffer name. |

Each component has its own setup, supported platforms, verification command, and limits.
Start with the component README and its printable guide when available.
Installation is opt-in, and components do not start workers or change hooks merely because the source is present.

## Prerequisites

CMUX is required for Starfleet's coordinated session and attention workflow.
Read the selected component documentation for its required CMUX capabilities, supported harnesses, operating systems, and Python version.
Python 3.10 or newer is required by the repository-wide verification suite.
Starfleet Markup requires Node.js 22 or newer for its local source workflow.

## Choose a component

1. Read the component README and guide.
2. Confirm the required CMUX and agent-harness capabilities are available.
3. Reuse an existing supported installation when possible.
4. Follow the component's exact self-service setup and removal instructions.
5. Verify configuration with its doctor or status command before enabling any sending worker.

The installer for one component does not enroll sessions or activate workers for another component.
Preserve existing permissions, human-input holds, owned state, and native trust prompts.
A configured route does not prove a message reached or was read by its intended agent.

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

The Python-only portion is also available for CI matrices:

```sh
python3 scripts/verify.py --python-only
```

The aggregate runner uses each component's maintained verification and test commands.
Starfleet Markup verification uses its checked-in npm lockfile and a local Chromium browser.
The test suite uses synthetic data and does not contact a live model, CMUX session, or Discord account.
Hosted GitHub Actions is the routine CI path; local verification is welcome.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.
Use [SUPPORT.md](SUPPORT.md) for setup questions, bugs, and feature requests.
Use [SECURITY.md](SECURITY.md) for the private vulnerability reporting route.
Community-concern issues are public; read the warning in the form before submitting one.

## License

Starfleet AI is licensed under the [MIT License](LICENSE).
