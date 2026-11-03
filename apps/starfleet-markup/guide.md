# Starfleet Markup guide

Starfleet Markup provides local document review with immutable versions, anchored comments and an explicit feedback handoff to the owning agent.
Use it when the user asks for an annotated review, and keep discussion in the originating chat.

## Prerequisites and reuse

Use Node.js 22 or newer, npm, Git and a local browser.
Reuse an existing `starfleet-markup` installation and server when available.
Do not start two servers for the same state directory or remove a lock without checking its owner.
Print this guide with `starfleet-markup guide --print` from any directory, without configuration, credentials, server startup or writes.

If the command is not installed, run from the Starfleet source checkout:

```sh
npm ci --prefix apps/starfleet-markup --ignore-scripts --no-audit --no-fund
npm --prefix apps/starfleet-markup run verify
node apps/starfleet-markup/install.mjs
```

The installed runtime survives removal of the source checkout.
No registry package, machine service or harness hook is installed by these commands.

## Start or reuse a server

Start one foreground server in a dedicated terminal with an explicit lifecycle:

```sh
starfleet-markup serve
```

The server listens on a loopback address and prints its state location.
Default state is under `~/.local/share/starfleet-ai/markup`, or `$XDG_DATA_HOME/starfleet-ai/markup` when set.
Use `--state /absolute/private/path` consistently for an explicitly selected separate installation.
Keep state and review source copies outside Git repositories.
Review URLs are local capability links; do not publish them or include them in issue reports.

## Create an owned review

Use the actual native orchestrator session ID and canonical worktree path.
Do not borrow a worker or another session's identity.
For Codex automatic queued feedback:

```sh
starfleet-markup create /absolute/private/proposal.md \
  --harness codex --owner ACTUAL_SESSION_UUID --worktree /absolute/worktree
```

The native Codex CLI must support `codex queue`.
For Claude use `--harness claude`, inspect `starfleet-markup claude-config`, and follow the component README's supported hook setup and native trust flow.
Do not change trust or restart an active session without an appropriate safe boundary.
An implementer follows its parent dispatch and does not take over the parent's review.

## Feedback and revisions

Before sharing a review link, verify the latest version and draft round in Comment mode and confirm its editor is editable.
Cancel any empty test editor rather than saving synthetic feedback.
The user saves comments and chooses Send to Agent to submit and lock the round.
Read submitted feedback through the installed command:

```sh
starfleet-markup feedback REVIEW_ID
```

Acknowledge only after receiving the exact round:

```sh
starfleet-markup ack REVIEW_ID --version VERSION --round ROUND_ID \
  --owner ACTUAL_SESSION_UUID --worktree /absolute/worktree
```

A configured adapter or queued wake is not proof of actual receipt.
If automatic delivery is unavailable, recover with `pending --owner ACTUAL_SESSION_UUID --worktree /absolute/worktree`, or keep the documented event-driven `listen` command in a foreground tool call.
A detached listener is not proof that an idle agent will wake.
Feedback grants no implementation or deployment authority.
Discuss flagged comments in the originating chat and record agreed decisions in maintained project documentation.
Preserve submitted rounds; use Start another round for unchanged content or `version REVIEW_ID /absolute/private/revised.md` after submission for revised content.
Never unlock submitted feedback or commit review artifacts.

## Verification and removal

Use `npm --prefix apps/starfleet-markup run verify` and the separate `verify:browser` command from the source checkout.
Native wake reception must be observed separately from fixtures and queued transport responses.
Remove only the owned launcher with `node apps/starfleet-markup/install.mjs --uninstall`.
Review data and runtime versions are preserved; stop only servers and adapters you own when their reviews are complete.
See the adjacent README for state, assets, native delivery, recovery and lifecycle details.
