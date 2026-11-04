# Agent Notify setup guide

Agent Notify opens and manages one Discord forum post per conversation for a configured Discord user.
It can route a short attention notice to the current CMUX chat when the optional Away Mode tool is available.
When Discord Watch is installed, new posts register to the exact current Hailing-enrolled session for guarded reply delivery.
An OpenCode session is labeled `OpenCode` when its exact `ses_` ID and CMUX surface and workspace are present, even if inherited Codex variables also exist.
Managed OpenCode registration additionally requires `CMUX_AGENT_LAUNCH_KIND=opencode` and passes that exact `OPENCODE_SESSION_ID` to Discord Watch.

## Requirements

Use Bash, `curl`, and `jq` on macOS or another POSIX host.
Install or reuse Hailing Frequencies from this Starfleet AI checkout to install the `agent-notify` command and its maintained skill.
For managed Discord reply delivery, also install or reuse Discord Watch, a current CMUX CLI, and an exact Hailing Frequencies enrollment for the session that will own each thread.
`gh` adds issue titles when available, and `away-mode` enables optional presence-based routing.
Neither optional command is required for ordinary Discord conversations.

## Prepare a private Discord inbox

Create or choose a Discord server and enable Community in Server Settings if it is not enabled.
Discord requires Community to be enabled before a server can create Forum channels.
Follow Discord's [Community server setup](https://support.discord.com/hc/en-us/articles/360047132851-Enabling-Your-Community-Server) if the server has not enabled it yet.
Community servers can remain invite-only; enabling Community does not by itself make a server discoverable.
Create a Forum channel for the inbox and restrict its channel permissions to the operator, the bot role, and other trusted members.
Discord describes [Forum Channels](https://support.discord.com/hc/en-us/articles/6208479917079-Forum-Channels-FAQ) and [channel permissions](https://support.discord.com/hc/en-us/articles/10543994968087-Channel-Permissions-Settings-101) in its Help Center.

Add these five tags to the Forum channel: `Open`, `Closed`, `Urgent`, `Normal`, and `FYI`.
The command resolves their IDs with `agent-notify doctor`, so their visible spelling is case-insensitive.

Create an incoming webhook in the Forum channel through Channel Settings > Integrations > Webhooks.
The person creating it needs `Manage Webhooks` in that channel; the bot role does not need that permission.
Copy its URL for the local setup below and treat it like a password.
The webhook posts the new forum post and later replies from the same configured identity.
Discord's [Execute Webhook API](https://discord.com/developers/docs/resources/webhook#execute-webhook) supports creating forum posts with a thread name and applying forum tags.

Create a bot application in the [Discord Developer Portal](https://discord.com/developers/applications), create its bot user, and copy its bot token.
Treat that token like a password and never paste it into a repository or command argument.
Enable the Message Content privileged intent in the Bot settings because Discord gates message content and attachment fields from the API behind that intent.
Discord's current [privileged intent guidance](https://support-dev.discord.com/hc/en-us/articles/6207308062871-What-are-Privileged-Intents) explains when Discord requires an access review.

In Developer Portal > OAuth2 > URL Generator, select the `bot` scope and generate an invite URL.
Use no server-wide permission grants in that URL.
Invite the bot to the server, then open the Forum channel's Settings > Permissions and add the bot role with only these channel permissions:

- `View Channel` so the bot can read the channel and its thread metadata.
- `Read Message History` so it can read forum replies.
- `Manage Threads` so it can apply the `Closed` tag and archive resolved posts.

Keep the Forum channel hidden from `@everyone` and other roles that should not read the inbox.
Do not grant `Administrator`, `Manage Channels`, or message-send permissions to the bot role for this tool.
Agent Notify publishes through the incoming webhook, and the Discord Watch worker reads and closes threads through the bot API.
The API operations are documented under [Get Channel Messages](https://discord.com/developers/docs/resources/message#get-channel-messages) and [Modify Channel](https://discord.com/developers/docs/resources/channel#modify-channel).

Enable Discord Developer Mode and copy the ID of the Discord account that should receive pings and whose replies should be queued.
Set it as `DISCORD_USER_ID` so posts ping only that configured account and Discord Watch queues replies from that account.
Leaving it empty preserves the legacy behavior of defaulting to the server owner.

## Install or reuse the local command

Run `command -v agent-notify` to inspect the current command path.
If an independently installed `agent-notify` already works with the existing config and open threads, keep using it and skip reinstalling the companion.
The Hailing installer preserves any launcher it does not own and refuses to replace an unowned file or symlink.
Do not delete or rename that launcher to get around the refusal.

To install the Starfleet launcher beside an independent one, use a separate bin directory and call its path explicitly:

```sh
python3 apps/hailing-frequencies/install.py --bin-dir "$HOME/.local/bin/starfleet-ai"
export PATH="$HOME/.local/bin/starfleet-ai:$PATH"
"$HOME/.local/bin/starfleet-ai/agent-notify" guide --print
```

This reuses the existing `AGENT_NOTIFY_HOME` when set, or the same `~/.config/agent-notify/` config and routing state otherwise.
The installer does not migrate or reset Discord cursors, open-thread metadata, or the Discord Watch worker.

From the Starfleet AI checkout, install Hailing Frequencies when the command is not already installed or needs an update:

```sh
python3 apps/hailing-frequencies/install.py
agent-notify guide --print
```

The Hailing installer installs `agent-notify` beside `hailing-frequencies` and `temporal-beacon` under the selected `--bin-dir`.
It copies the command, this guide, and the maintained skill into the same immutable runtime.
Installation does not initialize Discord configuration, alter existing thread cursors or routing records, start Discord Watch, or change CMUX settings.
With the default installer options, the command is `~/.local/bin/agent-notify`; add `~/.local/bin` to `PATH` if it is not already there.
The default private configuration directory is `~/.config/agent-notify/`.
It contains `env`, the cached Discord channel, user, and tag IDs, and private conversation routing state.
The routing files retain the original title and body so replies can remain attached to their original conversation.

If this config already exists, reuse it and run `agent-notify doctor` instead of replacing it during an install.
Use the prompts below only for first-time setup or when intentionally replacing credentials.
The webhook URL and bot token are entered without terminal echo and the values are shell-quoted into a mode-`600` file inside a mode-`700` directory.
The shell command contains no credential values, so it is safe to keep in shell history.

```sh
bash <<'EOF'
set -eu
umask 077
config_dir="$HOME/.config/agent-notify"
mkdir -p "$config_dir"
chmod 700 "$config_dir"
read -r -s -p 'Discord webhook URL: ' DISCORD_WEBHOOK_URL </dev/tty
printf '\n' >/dev/tty
read -r -s -p 'Discord bot token: ' DISCORD_BOT_TOKEN </dev/tty
printf '\n' >/dev/tty
read -r -p 'Discord user ID to notify: ' DISCORD_USER_ID </dev/tty
tmp="$(mktemp "$config_dir/.env.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
{
  printf 'DISCORD_WEBHOOK_URL=%q\n' "$DISCORD_WEBHOOK_URL"
  printf 'DISCORD_BOT_TOKEN=%q\n' "$DISCORD_BOT_TOKEN"
  printf 'DISCORD_USER_ID=%q\n' "$DISCORD_USER_ID"
} >"$tmp"
chmod 600 "$tmp"
mv -f "$tmp" "$config_dir/env"
trap - EXIT
unset DISCORD_WEBHOOK_URL DISCORD_BOT_TOKEN DISCORD_USER_ID
printf 'Saved private config at %s/env\n' "$config_dir"
EOF
```

To update one value in an existing config, edit the file and preserve the other entries:

```sh
"${EDITOR:-vi}" "$HOME/.config/agent-notify/env"
chmod 600 "$HOME/.config/agent-notify/env"
agent-notify doctor
```

Run `agent-notify doctor` to check the webhook, bot token, configured account, and required forum tags.
The command caches the resolved IDs in `cache.json` with private permissions and does not create a post.
Its success confirms the Discord configuration only, not a received reply or a running worker.
If the configured account or forum tags change, run `agent-notify doctor` again to refresh the cache.

The installer prints an `agent_notify_skill` path ending in `skills/agent-notify/SKILL.md`.
Use its containing `agent-notify` directory as `agent_notify_skill_dir` below and replace the example runtime path with the actual installed path.
These commands refuse an existing destination, including a dangling link.
Inspect an existing destination before changing it; do not overwrite another skill.

For Codex:

```sh
agent_notify_skill_dir='/absolute/installed/runtime/skills/agent-notify'
test -f "$agent_notify_skill_dir/SKILL.md" && mkdir -p "$HOME/.agents/skills"
test -f "$agent_notify_skill_dir/SKILL.md" && \
  test ! -e "$HOME/.agents/skills/agent-notify" && \
  test ! -L "$HOME/.agents/skills/agent-notify" && \
  ln -s "$agent_notify_skill_dir" "$HOME/.agents/skills/agent-notify"
```

For Claude Code, use the same installed directory:

```sh
agent_notify_skill_dir='/absolute/installed/runtime/skills/agent-notify'
test -f "$agent_notify_skill_dir/SKILL.md" && mkdir -p "$HOME/.claude/skills"
test -f "$agent_notify_skill_dir/SKILL.md" && \
  test ! -e "$HOME/.claude/skills/agent-notify" && \
  test ! -L "$HOME/.claude/skills/agent-notify" && \
  ln -s "$agent_notify_skill_dir" "$HOME/.claude/skills/agent-notify"
```

An immutable runtime upgrade does not retarget an existing skill link automatically.

## Reuse Hailing Frequencies and Discord Watch

Reuse the installed Hailing Frequencies state and enrollment when they already exist.
If this is a new Hailing Frequencies state directory, initialize it once with `hailing-frequencies init` and confirm it with `hailing-frequencies status`.
From the exact Claude Code or Codex session that will own the conversation, observe the current CMUX identities and enroll only that session:

```sh
cmux current --json --limit 200
cmux --id-format both --json tree --all
hailing-frequencies status
hailing-frequencies enroll --harness codex --surface YOUR_SURFACE_UUID --session YOUR_CODEX_THREAD_ID --label discord-inbox
```

For Claude Code, use `--harness claude` and the exact current Claude session ID instead.
Replace the placeholders with the identities CMUX reports for this session.
Do not use `--include-existing` or re-enroll an unchanged session to reset its cutoff or receipts.
Read the [Hailing Frequencies guide](guide.md) before enrolling if the session identities or existing enrollment need inspection.
For OpenCode, the managed launcher must seed `OPENCODE_SESSION_ID`, `CMUX_SURFACE_ID`, `CMUX_WORKSPACE_ID`, and `CMUX_AGENT_LAUNCH_KIND=opencode` through the supported per-session API environment after CMUX placement and before the initial task.
OpenCode API servers do not inherit the TUI's CMUX environment, so Agent Notify does not infer ownership from a repository path or Codex thread variable.

Install Discord Watch only when it is not already installed or needs an update:

```sh
python3 apps/discord-watch/install.py
discord-watch guide --print
discord-watch doctor
discord-watch status
```

Agent Notify automatically registers a newly created post to the exact current enrolled session when it can find `discord-watch` on `PATH`.
OpenCode registration passes its exact `--session ses_ID`, and it requires the seeded OpenCode launch and CMUX scope values.
The legacy `listen` and `watch --queue` queue handoff is Codex-only and refuses OpenCode sessions even when an inherited `CODEX_THREAD_ID` exists.
Use one central Discord Watch worker for every registered thread.
If `discord-watch status` shows a healthy worker, reuse it and do not start another.
If there is no worker, follow the Discord Watch guide to start its one supervised worker in the authorized CMUX terminal.
Do not create per-thread listeners when `discord-watch managed THREAD_ID` confirms managed ownership.

## Run a bounded delivery check

From the enrolled owner session, open one low-urgency test post and record the printed thread ID:

```sh
agent-notify open --delivery discord --urgency fyi --title 'Agent Notify setup check' <<'EOF'
Please reply in this post with: setup check reply
EOF
```

Reply to the post from the configured Discord account.
Wait for the existing Discord Watch worker to poll it, then receive the queued reply from the same enrolled session:

```sh
discord-watch status
agent-notify wait THREAD_ID --timeout 60
discord-watch status
```

For a managed thread, `wait` reads the Discord Watch queue through `receive`, prints the selected reply, and records its local receipt only after stdout flushes.
If a managed wake provides an exact `discord-watch receive --thread THREAD_ID --message MESSAGE_ID` command, run that command instead.
That receipt proves the CLI delivered the output, not that a model read or acted on it.
For a legacy thread without managed registration, the same `wait` command reads directly from Discord and prints replies after its saved cursor.
Close the test post after receipt:

```sh
agent-notify close THREAD_ID --body 'Agent Notify setup check complete.'
```

`doctor` proves the webhook, token, account, and tags are configured.
`discord-watch register` and `managed` prove saved ownership, while `status` reports the worker's observed polling and delivery state.
Only a reply printed by `receive` or `wait` establishes local receipt.

## More detail

Load the [Agent Notify skill](skills/agent-notify/SKILL.md) for conversation format, routing, managed wakes, legacy listeners, and reply handling.
Read the [Hailing Frequencies README](https://github.com/morecoffeyplease/starfleet-ai/blob/main/apps/hailing-frequencies/README.md) for core installation, exact-session enrollment, and safety detail.
Read the [Discord Watch guide](https://github.com/morecoffeyplease/starfleet-ai/blob/main/apps/discord-watch/guide.md) for worker ownership, exact thread registration, delivery states, and diagnosis.
Run `agent-notify guide --print` from any directory to print this same guide before configuration or enrollment.
