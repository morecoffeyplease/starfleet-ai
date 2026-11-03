import fs from "node:fs";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { demand, isSessionId } from "./store.mjs";

const execute = promisify(execFile);
const cmuxShimDirectory = "cmux-cli-shims";

function isCmuxShim(candidate, resolved) {
  return [candidate, resolved].some((value) =>
    path.resolve(value).split(path.sep).includes(cmuxShimDirectory),
  );
}

export function resolveCodexExecutable(pathValue = process.env.PATH) {
  if (typeof pathValue !== "string" || pathValue.length === 0)
    throw new Error("Codex CLI is not available on PATH");

  const seen = new Set();
  for (const directory of pathValue.split(path.delimiter)) {
    if (!directory) continue;
    const candidate = path.resolve(directory, "codex");
    let executable;
    try {
      executable = fs.realpathSync(candidate);
      if (isCmuxShim(candidate, executable) || seen.has(executable)) continue;
      const stat = fs.statSync(executable);
      if (!stat.isFile()) continue;
      fs.accessSync(executable, fs.constants.X_OK);
    } catch {
      continue;
    }
    seen.add(executable);
    return executable;
  }

  throw new Error(
    "No executable Codex CLI was found on PATH outside CMUX launcher shims",
  );
}

export async function queueCodex(
  { review, version, round, state },
  run = execute,
  resolveExecutable = resolveCodexExecutable,
) {
  demand(
    isSessionId(review.owner),
    "Codex wake requires the exact originating session UUID",
  );
  const message =
    "Starfleet Markup feedback is ready for this session. Retrieve the submitted round with the installed starfleet-markup CLI using the identity below. Acknowledge only after actually receiving it. Discuss flagged comments in this chat and record agreed decisions in maintained project documentation. Do not commit review artifacts. This is feedback, not additional authority to implement a proposal.\n" +
    JSON.stringify({
      review: review.id,
      version: version.number,
      round: round.id,
      owner: review.owner,
      worktree: review.worktree,
      state,
    });
  // The supported queue command targets an existing session; it does not resume a second owner.
  const result = await run(
    resolveExecutable(),
    ["queue", "--thread", review.owner, "--message", message],
    {
      cwd: review.worktree,
      timeout: 15000,
      maxBuffer: 65536,
      windowsHide: true,
      env: process.env,
    },
  );
  return {
    messageId:
      result.stdout.match(/Queued message ([a-f0-9-]{36})/i)?.[1] || null,
  };
}
