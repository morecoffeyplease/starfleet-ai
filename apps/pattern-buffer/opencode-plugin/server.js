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

function invoke(directory, sessionID) {
  const python = "python3";
  const result = spawnSync(python, [
    SCRIPT,
    "--cwd",
    directory,
    "context",
    "--harness",
    "opencode",
    "--session",
    sessionID,
  ], {
    cwd: directory,
    env: {
      PATH: process.env.PATH || "/usr/bin:/bin",
      HOME: process.env.HOME || directory,
      LANG: process.env.LANG || "C.UTF-8",
      LC_ALL: process.env.LC_ALL || "C.UTF-8",
      PYTHONDONTWRITEBYTECODE: "1",
      GIT_OPTIONAL_LOCKS: "0",
    },
    encoding: "utf8",
    maxBuffer: MAX_OUTPUT,
    shell: false,
    timeout: TIMEOUT_MS,
  });
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
  if (["unregistered", "disabled"].includes(payload.status)) {
    return { status: payload.status };
  }
  if (payload.status !== "context" || typeof payload.text !== "string" || !payload.text) {
    return { status: "blocked", code: "invalid_context" };
  }
  return { status: "context", text: payload.text };
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
    await ctx.session.hook("context", refresh);
    await ctx.session.hook("compaction", refresh);
  },
};
