"""Preview and delete closed Discord forum threads within an explicit UTC range."""
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://discord.com/api/v10"
UTC = dt.timezone.utc
MAX_PRIVATE_FILE = 8 * 1024 * 1024
DISCORD_EPOCH_MS = 1420070400000
MAX_SNOWFLAKE = (1 << 64) - 1


def require(value, message):
    if not value:
        raise ValueError(message)


def timestamp(value):
    require(isinstance(value, str), "Expected an ISO 8601 date or timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Use YYYY-MM-DD or an ISO 8601 timestamp with an explicit offset") from None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed = parsed.replace(tzinfo=UTC)
    require(parsed.tzinfo is not None, "Timestamps require an explicit UTC offset")
    return parsed.astimezone(UTC)


def snowflake(value):
    require(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value),
            "Discord IDs must be decimal snowflakes")
    require(int(value) <= MAX_SNOWFLAKE, "Discord ID is outside the snowflake range")
    return value


def snowflake_time(value):
    number = int(snowflake(value))
    try:
        milliseconds = (number >> 22) + DISCORD_EPOCH_MS
        return dt.datetime.fromtimestamp(milliseconds / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        raise ValueError("Discord message timestamp is outside the supported range") from None


def read_private(path, label, limit=MAX_PRIVATE_FILE):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError:
        raise ValueError(f"Could not read private {label}") from None
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_mode & 0o077 == 0, f"Expected an owner-only regular {label}")
        require(info.st_size <= limit, f"Private {label} is too large")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            require(total <= limit, f"Private {label} is too large")
        return b"".join(chunks)
    except OSError:
        raise ValueError(f"Could not read private {label}") from None
    finally:
        os.close(descriptor)


def private_directory(path):
    try:
        info = Path(path).lstat()
    except OSError:
        raise ValueError("Private output directory must already exist") from None
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and info.st_mode & 0o077 == 0, "Output directory must be owned by you and private")


def private_json(path, label):
    raw = read_private(path, label)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise ValueError(f"Private {label} is not valid JSON") from None


def credentials(home):
    home = Path(home).expanduser().absolute()
    env_file = home / "env"
    read_private(env_file, "Agent Notify environment", 65536)
    # The existing Agent Notify env file is shell-quoted by its setup flow.
    # Keep its value out of arguments, logs, and error text.
    shell_env = {"HOME": str(Path.home()), "PATH": os.defpath, "BASH_ENV": "/dev/null"}
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c",
         'set +x; source "$1" >/dev/null 2>&1 || exit; printf "%s" "${DISCORD_BOT_TOKEN-}"',
         "discord-cleanup-config", str(env_file)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=5, check=False, env=shell_env)
    require(result.returncode == 0 and 0 < len(result.stdout) < 16384,
            "Could not read DISCORD_BOT_TOKEN from Agent Notify configuration")
    try:
        token = result.stdout.decode("utf-8")
        cache = private_json(home / "cache.json", "Agent Notify cache")
        forum = snowflake(cache.get("channel"))
    except (UnicodeError, ValueError, AttributeError):
        raise ValueError("Invalid Agent Notify configuration; run agent-notify doctor") from None
    require(token and token.isascii() and all(33 <= ord(character) <= 126 for character in token),
            "Invalid Discord bot token in Agent Notify configuration")
    return token, forum


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Discord:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path):
        for attempt in range(4):
            req = urllib.request.Request(API + path, method=method, headers={
                "Authorization": "Bot " + self.token,
                "User-Agent": "DiscordBot (https://github.com/morecoffeyplease/starfleet-ai, 1.0)"})
            try:
                with self.opener.open(req, timeout=20) as response:
                    body = response.read(MAX_PRIVATE_FILE + 1)
                    require(len(body) <= MAX_PRIVATE_FILE, "Discord response is too large")
                    try:
                        return json.loads(body) if body else None
                    except (UnicodeError, ValueError):
                        raise ValueError("Discord returned invalid JSON; response omitted") from None
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    try:
                        body = exc.read(4096)
                        delay = float(json.loads(body)["retry_after"])
                    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
                        raise ValueError("Discord rate limit has no valid retry delay") from None
                    require(0 <= delay <= 30, "Discord rate limit exceeds the bounded retry window")
                    time.sleep(delay)
                    continue
                if exc.code == 404 and method == "GET":
                    try:
                        body = exc.read(4096)
                        error = json.loads(body)
                        if isinstance(error, dict) and error.get("code") == 10003:
                            return None
                    except (OSError, UnicodeError, ValueError, AttributeError):
                        pass
                raise ValueError(f"Discord {method} failed with HTTP {exc.code}; response omitted") from None
            except (http.client.HTTPException, urllib.error.URLError, TimeoutError, OSError):
                raise ValueError(f"Discord {method} transport failed; outcome may be uncertain") from None
        raise ValueError("Discord rate limit retry limit reached")


def forum_tags(api, forum):
    channel = api.request("GET", "/channels/" + snowflake(forum))
    require(isinstance(channel, dict) and channel.get("id") == forum and channel.get("type") == 15,
            "Configured channel must be an accessible Discord forum")
    tags = channel.get("available_tags")
    require(isinstance(tags, list), "Discord forum tags are unavailable")
    result = {}
    for name in ("open", "closed"):
        matches = [tag.get("id") for tag in tags if isinstance(tag, dict)
                   and isinstance(tag.get("name"), str) and tag["name"].casefold() == name]
        require(len(matches) == 1, "Forum needs exactly one Open tag and one Closed tag")
        result[name] = snowflake(matches[0])
    return result


def candidate(thread, forum, tags, start, end):
    if not isinstance(thread, dict):
        return None
    metadata = thread.get("thread_metadata")
    applied = thread.get("applied_tags")
    if (not isinstance(metadata, dict) or not isinstance(applied, list)
            or any(not isinstance(tag, str) for tag in applied)
            or thread.get("parent_id") != forum or thread.get("type") != 11
            or metadata.get("archived") is not True
            or tags["closed"] not in applied or tags["open"] in applied):
        return None
    try:
        archived = timestamp(metadata.get("archive_timestamp"))
        thread_id = snowflake(thread.get("id"))
        last_message_id = snowflake(thread.get("last_message_id"))
        last_at = snowflake_time(last_message_id)
    except ValueError:
        # Unknown archive or last-message activity must never become a delete candidate.
        return None
    if not start <= archived < end or last_at >= end:
        return None
    return {"id": thread_id, "archive_timestamp": archived.isoformat(),
            "last_message_id": last_message_id}


def digest(plan):
    body = {key: value for key, value in plan.items() if key != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plan_cleanup(api, forum, start, end):
    require(start < end <= dt.datetime.now(UTC), "Require start < end, with end in the past")
    tags = forum_tags(api, forum)
    before, selected, seen = None, [], set()
    for _ in range(10000):
        query = {"limit": "100"}
        if before is not None:
            query["before"] = before
        page = api.request("GET", f"/channels/{forum}/threads/archived/public?" +
                           urllib.parse.urlencode(query))
        require(isinstance(page, dict) and isinstance(page.get("threads"), list)
                and type(page.get("has_more")) is bool, "Malformed archived-thread page")
        threads = page["threads"]
        for thread in threads:
            row = candidate(thread, forum, tags, start, end)
            if row and row["id"] not in seen:
                selected.append(row)
                seen.add(row["id"])
        if not page["has_more"]:
            break
        require(threads, "Pagination has_more without threads")
        last = threads[-1]
        require(isinstance(last, dict) and isinstance(last.get("thread_metadata"), dict),
                "Malformed archived-thread cursor")
        cursor = timestamp(last["thread_metadata"].get("archive_timestamp"))
        require(before is None or cursor < timestamp(before), "Archived-thread cursor did not advance")
        before = cursor.isoformat()
    else:
        raise ValueError("Archived-thread page limit exceeded; no plan written")
    result = {"version": 1, "forum": forum, "start": start.isoformat(), "end": end.isoformat(),
              "tags": tags, "candidates": sorted(selected, key=lambda row: row["id"])}
    result["sha256"] = digest(result)
    return result


def write_private(path, value):
    path = Path(path)
    private_directory(path.parent)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    require(len(payload) <= MAX_PRIVATE_FILE, "Preview is too large for a private plan file")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def validate_plan(plan, forum, confirmation):
    expected = {"version", "forum", "start", "end", "tags", "candidates", "sha256"}
    require(isinstance(plan, dict) and set(plan) == expected and type(plan["version"]) is int
            and plan["version"] == 1 and plan["forum"] == forum,
            "Plan does not match the configured forum or schema")
    require(isinstance(plan["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", plan["sha256"])
            and digest(plan) == plan["sha256"] == confirmation,
            "Confirm the exact preview SHA-256")
    start, end = timestamp(plan["start"]), timestamp(plan["end"])
    require(start.isoformat() == plan["start"] and end.isoformat() == plan["end"]
            and start < end <= dt.datetime.now(UTC), "Invalid plan date range")
    tags = plan["tags"]
    require(isinstance(tags, dict) and set(tags) == {"open", "closed"}
            and all(snowflake(value) for value in tags.values())
            and tags["open"] != tags["closed"], "Invalid plan forum tags")
    rows = plan["candidates"]
    require(isinstance(rows, list), "Invalid candidate list")
    normalized = []
    for row in rows:
        require(isinstance(row, dict) and set(row) == {"id", "archive_timestamp", "last_message_id"},
                "Invalid candidate row")
        thread_id = snowflake(row["id"])
        archive_at = timestamp(row["archive_timestamp"])
        last_message_id = snowflake(row["last_message_id"])
        require(archive_at.isoformat() == row["archive_timestamp"] and start <= archive_at < end
                and snowflake_time(last_message_id) < end, "Candidate is outside the reviewed time range")
        normalized.append({"id": thread_id, "archive_timestamp": row["archive_timestamp"],
                           "last_message_id": last_message_id})
    require([row["id"] for row in normalized] == sorted({row["id"] for row in normalized}),
            "Candidate IDs must be unique and sorted")
    return start, end, tags, normalized


def apply_cleanup(api, forum, plan, confirmation, journal):
    start, end, planned_tags, rows = validate_plan(plan, forum, confirmation)
    tags = forum_tags(api, forum)
    require(tags == planned_tags, "Forum tags changed; create a new plan")
    private_directory(Path(journal).parent)
    counts = {"deleted": 0, "already_absent": 0, "changed": 0}
    # Exclusive creation blocks concurrent applications and replay of a partial run.
    with Path(journal).open("x", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)

        def record(row, outcome):
            stream.write(json.dumps({"plan_sha256": confirmation, "id": row["id"], "outcome": outcome},
                                    sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

        for row in rows:
            current = api.request("GET", "/channels/" + row["id"])
            if current is None:
                counts["already_absent"] += 1
                record(row, "already_absent")
                continue
            if candidate(current, forum, tags, start, end) != row:
                counts["changed"] += 1
                record(row, "changed_preserved")
                continue
            record(row, "delete_intent")
            try:
                api.request("DELETE", "/channels/" + row["id"])
                require(api.request("GET", "/channels/" + row["id"]) is None,
                        "Thread still accessible after DELETE")
            except (ValueError, OSError):
                record(row, "uncertain_stop")
                raise ValueError("Cleanup stopped after DELETE intent; preserve the journal and inspect before creating a new plan") from None
            record(row, "deleted_get404")
            counts["deleted"] += 1
    return counts


def main(args):
    if args.cleanup_action == "plan":
        start, end = timestamp(args.start), timestamp(args.end)
        require(start < end <= dt.datetime.now(UTC), "Require start < end, with end in the past")
        private_directory(args.output.parent)
        require(not args.output.exists() and not args.output.is_symlink(),
                "Preview path already exists; choose a new path")
    else:
        private_directory(args.plan.parent)
        plan = private_json(args.plan, "Discord cleanup plan")
        require(isinstance(plan, dict), "Invalid plan")
        journal = args.plan.with_name(args.plan.name + ".journal.jsonl")
        require(not journal.exists() and not journal.is_symlink(),
                "Journal already exists; inspect it and create a fresh plan, do not replay")
    token, forum = credentials(args.notify_home)
    api = Discord(token)
    if args.cleanup_action == "plan":
        plan = plan_cleanup(api, forum, start, end)
        write_private(args.output, plan)
        return {"plan": str(args.output), "sha256": plan["sha256"], "selected": len(plan["candidates"]),
                "start": plan["start"], "end": plan["end"], "deleted": 0}
    return {**apply_cleanup(api, forum, plan, args.confirm, journal), "journal": str(journal)}
