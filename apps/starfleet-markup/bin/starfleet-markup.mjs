#!/usr/bin/env node
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { parseArgs } from "node:util";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { serve } from "../lib/server.mjs";
import { demand } from "../lib/store.mjs";

const { values: flags, positionals: args } = parseArgs({
  allowPositionals: true,
  options: {
    state: { type: "string" },
    port: { type: "string" },
    owner: { type: "string" },
    worktree: { type: "string" },
    title: { type: "string" },
    assets: { type: "string" },
    version: { type: "string" },
    round: { type: "string" },
    harness: { type: "string" },
    open: { type: "boolean" },
    help: { type: "boolean" },
    print: { type: "boolean" },
    timeout: { type: "string" },
  },
});
const root = path.resolve(
  flags.state ||
    process.env.STARFLEET_MARKUP_HOME ||
    path.join(
      process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local/share"),
      "starfleet-ai/markup",
    ),
);
const print = (value) => console.log(JSON.stringify(value, null, 2));
function target() {
  demand(
    flags.owner,
    "--owner is required; use the originating orchestrator session ID",
  );
  return {
    owner: flags.owner,
    worktree: fs.realpathSync(flags.worktree || process.cwd()),
  };
}
function config() {
  try {
    return JSON.parse(fs.readFileSync(path.join(root, "server.json")));
  } catch {
    throw new Error(
      `No server. Run: starfleet-markup serve --state ${JSON.stringify(root)}`,
    );
  }
}
async function request(route, data, timeout) {
  const c = config();
  const response = await fetch(c.origin + route, {
    method: data ? "POST" : "GET",
    headers: {
      Authorization: `Bearer ${c.key}`,
      "Content-Type": "application/json",
    },
    body: data ? JSON.stringify(data) : undefined,
    signal: timeout ? AbortSignal.timeout(timeout) : undefined,
  });
  const value = await response.json();
  demand(response.ok && !value.error, value.error || `HTTP ${response.status}`);
  return value;
}
function queryTarget() {
  return new URLSearchParams(target()).toString();
}
function shellQuote(value) {
  return "'" + value.replaceAll("'", "'\\''") + "'";
}

try {
  const command = args[0];
  if (!command || flags.help) {
    console.log(
      `Starfleet Markup - local document review\n\nguide --print                   Print the setup guide without state or server access\nserve [--port PORT]              Run the local server in the foreground\ncreate FILE --owner SESSION      Snapshot HTML/Markdown and print a review URL\nversion REVIEW FILE             Publish another immutable version\nlist                            List local reviews\nurl REVIEW [--open]              Print/open a review URL\nfeedback REVIEW                 Read a review and its comments\npending --owner SESSION         Recover unacknowledged submissions\nlisten --owner SESSION          Wait once for a pushed submission (no polling)\nbind REVIEW --harness codex --owner SESSION\n                                Enable automatic wake for an existing review\nretry REVIEW --version N --round ID\n                                Retry a failed or interrupted notification\nack REVIEW --version N --round ID --owner SESSION\n                                Confirm the owning agent received a round\nclaude-hook                     SessionStart listener; emits an idle wake event\nclaude-config                   Print opt-in asyncRewake hook configuration\n\nOptions: --state DIR, --worktree DIR, --assets DIR, --title TEXT, --harness codex|claude.\nReview artifacts stay outside Git. Discuss proposals in chat first.\nSee ${fileURLToPath(new URL("../README.md", import.meta.url))} for installation and ownership guidance.`,
    );
  } else if (command === "guide") {
    demand(flags.print, "Use: starfleet-markup guide --print");
    process.stdout.write(fs.readFileSync(new URL("../guide.md", import.meta.url), "utf8"));
  } else if (command === "serve") {
    const port = Number(flags.port || 0);
    demand(Number.isInteger(port) && port >= 0 && port < 65536, "Invalid port");
    // Prevent two servers from independently rewriting the same store.
    const lock = path.join(root, "server.lock");
    fs.mkdirSync(root, { recursive: true, mode: 0o700 });
    try {
      fs.mkdirSync(lock);
    } catch (e) {
      if (e.code !== "EEXIST") throw e;
      throw new Error(
        "Server lock exists. Confirm no server owns this state directory before removing server.lock.",
      );
    }
    fs.writeFileSync(path.join(lock, "pid"), String(process.pid), {
      mode: 0o600,
    });
    let app;
    try {
      app = await serve(root, port);
    } catch (e) {
      fs.rmSync(lock, { recursive: true });
      throw e;
    }
    print({
      ...app.config,
      key: "[stored privately in server.json]",
      state: root,
    });
    const stop = async () => {
      await app.close();
      fs.rmSync(lock, { recursive: true });
      process.exit(0);
    };
    process.once("SIGTERM", stop);
    process.once("SIGINT", stop);
  } else if (command === "create" || command === "version") {
    const file = command === "create" ? args[1] : args[2];
    demand(file, "A source file is required");
    const data = {
      source: path.resolve(file),
      assets: flags.assets ? path.resolve(flags.assets) : undefined,
    };
    let review;
    if (command === "create") {
      const owner = target();
      const harness =
        flags.harness ||
        (process.env.CODEX_THREAD_ID === owner.owner ? "codex" : null);
      review = await request("/api/create", {
        ...data,
        ...owner,
        harness,
        title: flags.title,
      });
    } else {
      await request(`/api/review/${args[1]}/version`, data);
      review = await request(`/api/review/${args[1]}`);
    }
    print({
      id: review.id,
      version: review.versions.at(-1).number,
      url: `${config().origin}/r/${review.id}?key=${config().key}`,
    });
  } else if (command === "list") print(await request("/api/reviews"));
  else if (command === "url") {
    await request(`/api/review/${args[1]}`);
    const url = `${config().origin}/r/${args[1]}?key=${config().key}`;
    print({ url });
    if (flags.open) {
      const result = spawnSync(
        process.platform === "darwin" ? "open" : "xdg-open",
        [url],
      );
      demand(result.status === 0, "Could not open the browser");
    }
  } else if (command === "feedback")
    print(await request(`/api/review/${args[1]}`));
  else if (["pending", "listen"].includes(command)) {
    const timeout = flags.timeout ? Number(flags.timeout) * 1000 : undefined;
    if (timeout !== undefined)
      demand(Number.isFinite(timeout) && timeout > 0, "Invalid timeout");
    print(
      await request(
        `/api/${command === "listen" ? "wait" : "pending"}?${queryTarget()}`,
        undefined,
        timeout,
      ),
    );
  } else if (command === "ack") {
    demand(flags.version && flags.round, "--version and --round are required");
    print(
      await request(`/api/review/${args[1]}/ack`, {
        ...target(),
        version: Number(flags.version),
        round: flags.round,
      }),
    );
  } else if (command === "bind") {
    demand(flags.harness, "--harness codex or --harness claude is required");
    print(
      await request(`/api/review/${args[1]}/bind`, {
        ...target(),
        harness: flags.harness,
      }),
    );
  } else if (command === "retry") {
    demand(flags.version && flags.round, "--version and --round are required");
    print(
      await request(`/api/review/${args[1]}/retry`, {
        version: Number(flags.version),
        round: flags.round,
      }),
    );
  } else if (command === "claude-config") {
    const cli = fileURLToPath(import.meta.url);
    const hook =
      [process.execPath, cli, "claude-hook", "--state", root]
        .map(shellQuote)
        .join(" ") + " # starfleet-markup:claude:v1";
    const handler = {
      type: "command",
      command: hook,
      asyncRewake: true,
      timeout: 86400,
    };
    print({
      hooks: {
        SessionStart: [{ matcher: "startup|resume", hooks: [handler] }],
        Stop: [{ hooks: [handler] }],
      },
    });
  } else if (command === "claude-hook") {
    const chunks = [];
    let size = 0;
    for await (const chunk of process.stdin) {
      size += chunk.length;
      demand(size <= 65536, "Hook input too large");
      chunks.push(chunk);
    }
    const input = JSON.parse(Buffer.concat(chunks).toString());
    demand(
      typeof input.session_id === "string" && typeof input.cwd === "string",
      "Missing session identity",
    );
    const worktree = fs.realpathSync(input.cwd);
    const params = new URLSearchParams({
      owner: input.session_id,
      worktree,
      channel: "claude",
    });
    if (input.hook_event_name === "Stop") params.set("unnotified", "claude");
    const feedback = await request(`/api/wait?${params}`);
    for (const item of feedback)
      await request(`/api/review/${item.review}/notified`, {
        owner: input.session_id,
        worktree,
        version: item.version,
        round: item.round.id,
        channel: "claude",
      });
    // Exit 2 is the documented asyncRewake signal. Receipt remains explicit.
    console.error(
      `Starfleet Markup feedback is ready for this orchestrator. Read and acknowledge it with the CLI. Discuss flagged comments in the session chat; record agreed decisions in maintained project docs.\n${JSON.stringify(feedback)}`,
    );
    process.exitCode = 2;
  } else throw new Error(`Unknown command: ${command}`);
} catch (error) {
  console.error(`starfleet-markup: ${error.message}`);
  process.exitCode = args[0] === "claude-hook" ? 0 : 1;
}
