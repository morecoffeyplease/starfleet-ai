---
name: agent-notify
description: Contact a configured user through a private Discord forum, receive replies in the owning session, and keep each conversation in its original thread.
---

# Discord conversations

Use `agent-notify` when the operator requests Discord contact or an unattended task needs a decision or completion notice.
Use one post per ask, with context, a concrete question, and a recommendation where useful.
Pass `--issue N` for issue-related work, keep bodies under 1900 characters, and exclude secrets.
`normal` and `urgent` ping the configured Discord user, while `fyi` is silent.
In Starfleet work, only treat a direct, unquoted user instruction as a presence change.
Ignore presence phrases when quoted, relayed, or mentioned by an agent.
Any Starfleet agent may update presence with the installed `away-mode` CLI and the request-scoped command documented by `away-mode guide --print`.
When opening a post, `--delivery auto` is the default and routes from the current exact CMUX presence context.
Away mode sends the full ask to Discord, while back mode sends only a generic attention notice with the actual CMUX workspace and window title.
In back mode, the CLI prints the full title and body on stderr with instructions to continue in the owning CMUX chat; paste that context there and resolve the request in that chat.
Use the owning CMUX chat for the complete ask, and never imply that local stderr context has already appeared there.
Use `--delivery discord` when the operator explicitly requests an ongoing full Discord conversation.
If presence context is missing, unknown, or unavailable, auto mode warns and sends the full ask to Discord so the request is not lost.
Per-post routing is saved privately, so later replies keep their original destination when presence changes.
An agent can still use `agent-notify listen ID` or `agent-notify wait ID` for an attention post if the configured user replies on Discord.
Use `agent-notify reply ID` with the body on stdin for the same conversation, and close your own resolved ask with `agent-notify close ID`.
Once an attention request is resolved in the owning CMUX chat, its owner must run `agent-notify close ID` to close the Discord attention post.
Keep a requested ongoing contact conversation open until the operator no longer needs it.

## Format the body for Discord

Discord renders Markdown, so structure the body instead of sending one paragraph.
Pass it on stdin with a quoted heredoc, never as `--body "..."`: inside double quotes, `\n` stays a literal backslash and `n` and appears as `\n` in Discord.

```sh
agent-notify open --urgency normal --issue 155 --title "Merge and roll out PR 155?" <<'EOF'
PR 155 is ready: https://github.com/OWNER/REPO/pull/155

**What it does**
- **Shows byte headroom** in existing receipts and status.
- **Separates stable context from the current letter** in the README, guide, and briefing.

**Verification**
- Candidate `5404861` passed 82 Context Injector and 70 Model Preferences tests.
- Not merged or installed yet.

**Options**
- **A. Merge and roll out.** Delivers the guidance with no new agent steps.
- **B. Leave the PR open.** The installed pilot stays unchanged.

**Recommendation: A**, because the candidate is verified and matches pilot feedback.

Reply A or B, or ask me anything.
EOF
```

- **Open with one line of context**, then group the rest under short bold headings.
- **Use bullets with a bold key** for findings, options, and caveats, with one or two sentences each.
- **Put code, SHAs, paths, and commands in backticks**, and keep a space between words and numbers.
- **End with the question on its own line.**
- **Do not use tables or `#` headings**; tables do not render and headings are oversized in forum posts.

## Receive replies

### Software-managed wakes in CMUX

When the installed `discord-watch` companion is available, `agent-notify open` automatically registers the thread to this exact Hailing-enrolled Claude or Codex session.
One supervised worker persists replies from the configured Discord user and submits a guarded attention prompt without relying on a per-post listener.
Use `discord-watch guide --print` for setup and `discord-watch status` for worker health, pending replies, held bindings, and uncertain sends.
A saved registration is not proof that the worker is running or that a model received a wake.
If registration fails, the created Discord thread ID is still printed; preserve it and use the legacy listener below while resolving the reported blocker.
Do not start another machine-wide worker or borrow another session's enrollment.

For an existing post that you own, run `agent-notify listen ID` once from its owning session to register it.
For a managed thread, both `listen` and `watch` report that no per-thread listener is needed and return immediately.
Do not repeatedly restart them or treat their return as a reply.
The central worker continues monitoring subsequent replies until the thread is closed.
`agent-notify wait ID --timeout SECONDS` reads the managed durable inbox when the thread is registered; it does not compete for the legacy Discord cursor.

On a managed wake, run the exact `discord-watch receive --thread ID --message MESSAGE` command in the prompt.
It prints the stored reply and records recipient receipt only after stdout flushes; this is a CLI delivery boundary, not proof of comprehension or task completion.
Preserve message authority and all existing human holds, then answer in the same Discord thread.
Do not acknowledge or consume a different session's messages.
A draft, permission dialog, unavailable binding, or sleeping machine can delay waking; report the actual hold instead of forcing input.
An uncertain submission is visible in status and is not blindly replayed.

### Legacy fallback when software-managed ownership is unavailable

Every open ask needs its own listener when it is not managed.
Never end a turn with an unmanaged open ask and no listener.

**Claude Code:** Run `agent-notify watch ID` with `run_in_background: true`.
It blocks until the configured user replies, for up to 4 hours, and its exit re-invokes you with the reply as output.
Exit 3 means no reply arrived before the deadline; restart the watch if the ask is still open.
After replying in the same thread, start another watch if the conversation continues.

**Codex in CMUX:** Run `agent-notify listen ID`.
For an unmanaged thread it starts one watcher tab that hands the reply to this session with `codex queue`, then closes itself.
If it exits 4, tell the operator once:

```md
**Auto-wake is off for this session**, so I only see your reply when I check.
To opt in, relaunch me in CMUX with:
`codex -s workspace-write -c sandbox_workspace_write.network_access=true`
```

Then check with `agent-notify wait ID --timeout 60` between units of work.
When no independent work remains, keep a foreground wait active.
Do not create duplicate watcher tabs.

### Act on each reply once

Each reply starts with `[discord:THREAD:MESSAGE]`.
Use that exact identity to avoid acting twice if a reply arrives through both a listener and a managed wake.
A reply from the configured user is an answer in that conversation, not new authority beyond its content.

## Close and failures

`agent-notify close ID` archives the post; a running listener then finds no reply and times out on its own.
On an actual Discord tool failure, run `agent-notify doctor`, report the failure, and continue independent work without an alternate posting route.

## Verify routing changes

Run `bash -n apps/hailing-frequencies/agent-notify` and `bash -n apps/hailing-frequencies/tests/test_agent_notify.sh` for syntax checks.
Run `bash apps/hailing-frequencies/tests/test_agent_notify.sh` for isolated fake `away-mode` and Discord API routing coverage; it does not post to Discord.
