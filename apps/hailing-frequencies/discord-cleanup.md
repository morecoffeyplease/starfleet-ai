# Discord cleanup guide

`hailing-frequencies discord-cleanup` previews and deletes archived, Closed threads from the forum already configured for Agent Notify.
It is a separate opt-in command and does not run with the Hailing monitor.
It requires no worker restart or enrollment change.

The command reads the bot token from the existing private Agent Notify configuration and the forum ID from its cache.
It accepts no credential arguments and never prints the token or Discord response bodies.
The guide prints before Agent Notify is configured and makes no files or network requests.

## Preview one UTC date range

Use `YYYY-MM-DD` for midnight UTC or an ISO 8601 timestamp with an explicit UTC offset.
The start is inclusive and the end is exclusive.
Do not use local times without an offset.

Create an owner-only directory for the preview and journal, then set the range variables:

```sh
PRIVATE_DIR="$HOME/.local/state/starfleet-ai/discord-cleanup"
umask 077
mkdir -p "$PRIVATE_DIR"
chmod 700 "$PRIVATE_DIR"
START_DATE=2026-01-01T00:00:00Z
END_DATE=2026-02-01T00:00:00Z
hailing-frequencies discord-cleanup plan \
  --start "$START_DATE" \
  --end "$END_DATE" \
  --output "$PRIVATE_DIR/plan.json"
```

The preview lists only thread IDs, archive timestamps, last-message IDs, the date range, the selected count, and a SHA-256 confirmation value.
Its file is created with mode `0600` in an owner-only directory.
The selection requires an archived thread in the configured forum with the `Closed` tag and without the `Open` tag.
The archive timestamp must fall in the requested range, and the last message must predate the end of the range.
Threads with unknown or newer last-message activity are preserved.

Inspect the preview and copy its exact SHA-256 value before applying it.
The plan hash binds the forum, date range, tags, and full selected ID set.

## Apply the reviewed preview

```sh
hailing-frequencies discord-cleanup apply \
  --plan "$PRIVATE_DIR/plan.json" \
  --confirm SHA256_FROM_THE_PREVIEW
```

Apply rechecks the forum tags and each thread's eligibility immediately before acting.
It preserves threads that are missing, changed, newly Open, unarchived, or updated after the preview.
Eligible threads are deleted one at a time, and each deletion must be followed by a GET that confirms Discord returns `404` for the unknown channel.

The command permanently deletes entire Discord threads and their messages.
It leaves local Agent Notify routing history and Discord Watch history and subscriptions untouched.
Discord Watch may report `remote_missing` for a thread that cleanup deleted, which is separate from a credential failure or local retirement.
This command does not retire local records and has no retirement option.
The adjacent `.journal.jsonl` records only the plan hash, thread IDs, and outcomes.
If an outcome becomes uncertain after delete intent is recorded, the command stops and never retries that plan.
Keep the journal, inspect Discord, and create a fresh preview after resolving the uncertain result.

Use `--notify-home /absolute/private/agent-notify` on `plan` and `apply` only when Agent Notify uses a nondefault configuration directory.
No bot token, webhook URL, or message content belongs in command arguments or the preview.
