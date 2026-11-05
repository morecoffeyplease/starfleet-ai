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

function invokeCommand(directory, arguments_, input) {
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
    ...(input === undefined ? {} : { input }),
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
  return { status: "context", text: payload.text,
    pressureStatus: payload.pressure_status || "unknown",
    pressureReason: payload.pressure_reason || "descriptor_missing",
    pressureObservationRequired: payload.pressure_observation_required === true };
}

function identityFromHook(event, directory, sessionID) {
  const model = event?.model;
  if (!model || typeof model !== "object") return null;
  return {
    session_id: sessionID,
    worktree: directory,
    provider_id: model.providerID,
    model_id: model.id,
    variant: model.variant,
    agent: event.agent,
  };
}

function assistantRecord(context, sessionID, worktree) {
  const messages = Array.isArray(context) ? context : context?.messages;
  if (!Array.isArray(messages)) return null;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const row = messages[index];
    const info = row?.info && typeof row.info === "object" ? row.info : row;
    const role = info?.role || row?.type || row?.role;
    if (role !== "assistant") continue;
    const model = info.model && typeof info.model === "object" ? info.model : info;
    const tokens = info.tokens;
    const cache = tokens?.cache;
    return {
      identity: {
        session_id: sessionID,
        worktree,
        provider_id: model.providerID || info.providerID,
        model_id: model.id || info.modelID || info.modelId,
        variant: model.variant ?? info.variant,
        agent: info.agent,
      },
      completed_at_ms: info.time?.completed,
      tokens: {
        input: tokens?.input,
        output: tokens?.output,
        reasoning: tokens?.reasoning,
        cache_read: cache?.read,
        cache_write: cache?.write,
      },
    };
  }
  return null;
}

function pressureNotice(status, reason) {
  if (status === "below_threshold") {
    return "Pattern Buffer pressure status: the last measured usage anchor is below the configured reminder threshold; no warning was emitted.";
  }
  if (status === "warning_deduplicated") {
    return "Pattern Buffer pressure status: this compaction generation already received its one early warning.";
  }
  if (status === "warning_emitted") {
    return "Pattern Buffer pressure status: the early warning was emitted for this compaction generation.";
  }
  const detail = typeof reason === "string" ? ` (${reason.replaceAll("_", " ")})` : "";
  return `Pattern Buffer pressure status: unknown${detail}; no pressure warning was emitted.`;
}

function invokePressure(directory, sessionID, payload) {
  const result = invokeCommand(directory,
    ["opencode-checkpoint-pressure", "--session", sessionID], JSON.stringify(payload));
  const parsed = parsePayload(result, directory, sessionID);
  if (parsed.status === "blocked") {
    return { status: "unknown", reason: "observation_failed", warning: null };
  }
  return parsed;
}

function markPressureEmitted(directory, sessionID, correlationID) {
  if (typeof correlationID !== "string") return;
  invokeCommand(directory, ["opencode-checkpoint-pressure-emitted", "--session", sessionID,
    "--correlation-id", correlationID]);
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
    const refresh = async (event, hookType) => {
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
        let text = result.text;
        let warningCorrelationID;
        if (hookType === "context" && result.pressureObservationRequired) {
          let observation;
          try {
            if (typeof ctx.session.context !== "function") throw new Error("context_api_unavailable");
            const history = await ctx.session.context({ sessionID }, {
              signal: AbortSignal.timeout(3000),
            });
            const currentIdentity = identityFromHook(event, currentDirectory, sessionID);
            const assistant = assistantRecord(history, sessionID, currentDirectory);
            if (!assistant) {
              observation = { read_error: "assistant_missing" };
            } else {
              observation = { request: currentIdentity, assistant };
            }
          } catch {
            observation = { read_error: "context_read_failed" };
          }
          const pressure = invokePressure(currentDirectory, sessionID, observation);
          if (typeof pressure.warning === "string" && pressure.warning) {
            text += `\n\n${pressure.warning}`;
            warningCorrelationID = pressure.correlation_id;
          } else {
            text += `\n\n${pressureNotice(pressure.status, pressure.reason)}`;
          }
        } else if (hookType === "context") {
          text += `\n\n${pressureNotice(result.pressureStatus, result.pressureReason)}`;
        }
        event.system = [...prior, contextPart(text)];
        if (warningCorrelationID) {
          markPressureEmitted(currentDirectory, sessionID, warningCorrelationID);
        }
      } else if (result.status === "blocked") {
        failContext(event, prior, result.code);
      } else {
        event.system = prior;
      }
    };
    const registrations = [];
    registrations.push(await ctx.session.hook("context", (event) => refresh(event, "context")));
    registrations.push(await ctx.session.hook("compaction", (event) => refresh(event, "compaction")));
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
