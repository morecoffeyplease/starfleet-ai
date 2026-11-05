import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { realpathSync } from "node:fs";
import path from "node:path";

const PLUGIN_MARKER = "starfleet.pattern-buffer";
const PLUGIN_VERSION = "v1";
const SESSION_ID = /^ses_[A-Za-z0-9]+$/;
const SCRIPT = fileURLToPath(new URL("../pattern_buffer.py", import.meta.url));
const MAX_OUTPUT = 128 * 1024;
const TIMEOUT_MS = 5000;

function isOwnedPart(part) {
  return part?.metadata?.[PLUGIN_MARKER] === PLUGIN_VERSION;
}

function contextPart(text) {
  return {
    type: "text",
    text,
    metadata: { [PLUGIN_MARKER]: PLUGIN_VERSION },
  };
}

function failContext(event, prior, code) {
  event.system = prior;
  throw new Error(`Pattern Buffer OpenCode context failed (${code}).`);
}

function invokeCommand(directory, arguments_) {
  const python = "python3";
  return spawnSync(python, [
    SCRIPT,
    "--cwd",
    directory,
    ...arguments_,
  ], {
    cwd: directory,
    env: {
      PATH: process.env.PATH || "/usr/bin:/bin",
      HOME: process.env.HOME || directory,
      LANG: process.env.LANG || "C.UTF-8",
      LC_ALL: process.env.LC_ALL || "C.UTF-8",
      ...(process.env.XDG_STATE_HOME ? { XDG_STATE_HOME: process.env.XDG_STATE_HOME } : {}),
      PYTHONDONTWRITEBYTECODE: "1",
      GIT_OPTIONAL_LOCKS: "0",
    },
    encoding: "utf8",
    maxBuffer: MAX_OUTPUT,
    shell: false,
    timeout: TIMEOUT_MS,
  });
}

function parsePayload(result, directory, sessionID) {
  if (result.error || result.status !== 0) {
    return { status: "blocked", code: result.error?.code || `exit_${result.status ?? "unknown"}` };
  }
  let payload;
  try {
    payload = JSON.parse(result.stdout);
  } catch {
    return { status: "blocked", code: "invalid_response" };
  }
  if (!payload || payload.harness !== "opencode" || payload.session_id !== sessionID ||
      payload.worktree !== directory) {
    return { status: "blocked", code: "identity_mismatch" };
  }
  return payload;
}

function invoke(directory, sessionID) {
  const result = invokeCommand(directory, ["context", "--harness", "opencode", "--session", sessionID]);
  const payload = parsePayload(result, directory, sessionID);
  if (payload.status === "blocked") return payload;
  if (["unregistered", "disabled"].includes(payload.status)) {
    return { status: payload.status };
  }
  if (payload.status !== "context" || typeof payload.text !== "string" || !payload.text) {
    return { status: "blocked", code: "invalid_context" };
  }
  return { status: "context", text: payload.text };
}

function invokeLifecycle(directory, sessionID, eventType, eventID, aggregateID, eventSequence) {
  const args = ["opencode-checkpoint-event", "--session", sessionID, "--event", eventType];
  if (eventID) args.push("--event-id", eventID);
  if (aggregateID) args.push("--aggregate-id", aggregateID);
  if (Number.isInteger(eventSequence)) args.push("--event-sequence", String(eventSequence));
  const result = invokeCommand(directory, args);
  return parsePayload(result, directory, sessionID);
}

export default {
  id: "starfleet-pattern-buffer",
  async setup(ctx) {
    const refresh = (event) => {
      const prior = Array.isArray(event.system) ? event.system.filter((part) => !isOwnedPart(part)) : [];
      const directory = ctx.location?.directory;
      const sessionID = event.sessionID;
      if (typeof directory !== "string" || !path.isAbsolute(directory) ||
          typeof sessionID !== "string" || !SESSION_ID.test(sessionID)) {
        failContext(event, prior, "missing_native_identity");
      }
      let currentDirectory;
      try {
        currentDirectory = realpathSync(directory);
      } catch {
        failContext(event, prior, "worktree_unavailable");
      }
      const result = invoke(currentDirectory, sessionID);
      if (result.status === "context") {
        event.system = [...prior, contextPart(result.text)];
      } else if (result.status === "blocked") {
        failContext(event, prior, result.code);
      } else {
        event.system = prior;
      }
    };
    const registrations = [];
    registrations.push(await ctx.session.hook("context", refresh));
    registrations.push(await ctx.session.hook("compaction", refresh));
    if (ctx.tool?.hook) {
      registrations.push(await ctx.tool.hook("execute.after", async (event) => {
        if (event?.status !== "completed" || typeof event.sessionID !== "string" ||
            !SESSION_ID.test(event.sessionID)) return;
        const directory = ctx.location?.directory;
        if (typeof directory !== "string" || !path.isAbsolute(directory)) return;
        let currentDirectory;
        try {
          currentDirectory = realpathSync(directory);
        } catch {
          return;
        }
        invokeLifecycle(currentDirectory, event.sessionID, "tool-completed");
      }));
    }
    const controller = new AbortController();
    let eventTask;
    if (ctx.event?.subscribe) {
      eventTask = (async () => {
        const types = ["session.compaction.started", "session.compaction.ended", "session.compaction.failed"];
        try {
          for await (const event of ctx.event.subscribe({ types, signal: controller.signal })) {
            const eventType = {
              "session.compaction.started": "compaction-started",
              "session.compaction.ended": "compaction-ended",
              "session.compaction.failed": "compaction-failed",
            }[event?.type];
            const data = event?.data;
            if (!eventType || !data || typeof data.sessionID !== "string" ||
                !SESSION_ID.test(data.sessionID)) continue;
            const directory = ctx.location?.directory;
            if (typeof directory !== "string" || !path.isAbsolute(directory)) continue;
            let currentDirectory;
            try {
              currentDirectory = realpathSync(directory);
            } catch {
              continue;
            }
            invokeLifecycle(currentDirectory, data.sessionID, eventType, event.id,
              event.durable?.aggregateID, event.durable?.seq);
          }
        } catch {
          // Event history is live-only, so a lost subscription leaves generation coverage uncertain.
        }
      })();
    }
    return async () => {
      controller.abort();
      for (const registration of registrations) {
        await registration?.dispose?.();
      }
      await eventTask;
    };
  },
};
