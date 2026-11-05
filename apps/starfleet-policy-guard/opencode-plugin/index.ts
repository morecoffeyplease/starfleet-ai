import { spawn } from "node:child_process"
import { dirname, isAbsolute, join } from "node:path"
import { fileURLToPath } from "node:url"
import { Plugin } from "@opencode/plugin/effect"
import { Tool } from "@opencode/schema/tool"
import { Effect } from "effect"
import {
  directShellDenial,
  evaluateCoveredShellPermission,
  shellResource,
} from "./policy.js"

const PLUGIN_ID = "starfleet-policy-guard-opencode"
const NATIVE_SOURCE = "opencode-server-plugin-v2"
const DELIVERY_TOOL = "prime_directive_gh"
const MANAGE_TOOL = "prime_directive_manage"
const APP_ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const LANE_CLI = join(APP_ROOT, "lane.py")
const pendingAssertions = new Map<string, {
  sessionID: string
  agent: string
  messageID: string
  callID: string
  resources: string[]
}>()

const manageInput = {
  type: "object",
  properties: {
    operation: {
      type: "string",
      enum: ["plan", "install", "discover", "record", "rebind-owner", "status", "coverage", "registrations"],
    },
    checkout: { type: "string" },
    seatRoot: { type: "string" },
    planSha256: { type: "string" },
    host: { type: "string" },
    prURL: { type: "string" },
    baseRef: { type: "string" },
    headBranch: { type: "string" },
    evidence: { type: "string" },
    policy: { type: "string" },
    fromHarness: { type: "string", enum: ["claude", "codex", "opencode"] },
    fromSession: { type: "string" },
    summary: { type: "boolean" },
  },
  required: ["operation"],
  additionalProperties: false,
} as const

const deliveryInput = {
  type: "object",
  properties: {
    action: { type: "string", enum: ["ready", "merge"] },
    target: { type: "string" },
    mergeType: { type: "string", enum: ["merge", "rebase", "squash"] },
    auto: { type: "boolean" },
    deleteBranch: { type: "boolean" },
    checkout: { type: "string" },
  },
  required: ["action", "target"],
  additionalProperties: false,
} as const

function fail(message: string): never {
  throw new Tool.Error({ message })
}

function eventFor(ctx: Plugin.Context, tool: string, context: Tool.Context) {
  return Effect.gen(function* () {
    const session = yield* ctx.session.get({ sessionID: context.sessionID })
    if (session.id !== context.sessionID) return yield* Effect.fail(new Tool.Error({
      message: "OpenCode session API returned a different session than the active tool call.",
    }))
    return {
      native_source: NATIVE_SOURCE,
      tool,
      tool_context: {
        sessionID: context.sessionID,
        agent: context.agent,
        messageID: context.messageID,
        id: context.id,
      },
      session: {
        id: session.id,
        parentID: session.parentID ?? null,
        projectID: session.projectID,
        location: { directory: session.location.directory },
      },
    }
  })
}

function runGuard(argv: string[], nativeEvent: unknown, cwd: string) {
  return Effect.tryPromise({
    try: () => runGuardPromise(argv, nativeEvent, cwd),
    catch: (error) => error instanceof Tool.Error
      ? error
      : new Tool.Error({ message: error instanceof Error ? error.message : String(error) }),
  })
}

function runGuardPromise(argv: string[], nativeEvent: unknown, cwd: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn("python3", [LANE_CLI, "--cwd", cwd, "opencode"], {
      cwd,
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    })
    const stdout: Buffer[] = []
    const stderr: Buffer[] = []
    child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk))
    child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk))
    child.on("error", reject)
    child.on("close", (code) => {
      const output = Buffer.concat(stdout).toString("utf8")
      const error = Buffer.concat(stderr).toString("utf8").trim()
      if (code === 0) resolve(output)
      else reject(new Tool.Error({ message: error || output.trim() || `Policy Guard exited with code ${code}` }))
    })
    child.stdin.end(JSON.stringify({ native_event: nativeEvent, argv }))
  })
}

function absolutePath(value: unknown, label: string): string {
  if (typeof value !== "string" || !isAbsolute(value)) fail(`${label} must be an absolute path`)
  return value
}

function manageArgv(input: any, sessionID: string, sessionDirectory: string): { argv: string[]; cwd: string } {
  const cwd = absolutePath(input.checkout ?? sessionDirectory, "Checkout")
  const argv = ["--cwd", cwd]
  const operation = input.operation
  if (!["plan", "install", "discover", "record", "rebind-owner", "status", "coverage", "registrations"].includes(operation)) {
    fail("Choose a supported Policy Guard operation")
  }
  if (operation === "plan" || operation === "install") {
    const seatRoot = absolutePath(input.seatRoot, "Permanent seat root")
    if (operation === "install" && !/^[0-9a-f]{64}$/u.test(input.planSha256 ?? "")) {
      fail("Install requires the exact reviewed plan hash")
    }
    argv.push("--session-root", seatRoot, operation, "--lane", "--host", input.host ?? "github.com",
      "--owner-harness", "opencode", "--owner-session", sessionID)
    if (input.prURL) argv.push("--pr-url", input.prURL)
    if (input.baseRef) argv.push("--base-ref", input.baseRef)
    if (input.headBranch) argv.push("--head-branch", input.headBranch)
    if (operation === "install") argv.push("--plan-sha256", input.planSha256)
    return { argv, cwd }
  }
  argv.push(operation)
  if (operation === "record") {
    if (typeof input.evidence !== "string" || !input.evidence) fail("Record requires an evidence packet path")
    argv.push("--evidence", input.evidence, "--format", "json")
    if (input.policy) argv.push("--policy", input.policy)
  } else if (operation === "rebind-owner") {
    if (!["claude", "codex", "opencode"].includes(input.fromHarness) || typeof input.fromSession !== "string" || !input.fromSession) {
      fail("Handoff requires the exact outgoing native harness and session")
    }
    argv.push("--from-harness", input.fromHarness, "--from-session", input.fromSession, "--confirm-handoff")
  } else if (operation === "coverage") {
    if (input.prURL) argv.push("--pr-url", input.prURL)
    if (input.summary) argv.push("--summary")
  }
  return { argv, cwd }
}

function manageTool(ctx: Plugin.Context, input: any, context: Tool.Context) {
  return Effect.gen(function* () {
    const nativeEvent = yield* eventFor(ctx, MANAGE_TOOL, context)
    const { argv, cwd } = manageArgv(input, context.sessionID, nativeEvent.session.location.directory)
    const output = yield* runGuard(argv, nativeEvent, cwd)
    return { content: output }
  })
}

function deliveryTool(ctx: Plugin.Context, input: any, context: Tool.Context) {
  return Effect.gen(function* () {
    const nativeEvent = yield* eventFor(ctx, DELIVERY_TOOL, context)
    const { argv: ghArgv, resource, save } = shellResource(input)
    const cwd = absolutePath(input.checkout ?? nativeEvent.session.location.directory, "Checkout")
    const key = `${context.sessionID}\u0000${context.id}`
    if (pendingAssertions.has(key)) return yield* Effect.fail(new Tool.Error({
      message: "A matching OpenCode delivery permission assertion is already pending for this tool call.",
    }))
    pendingAssertions.set(key, {
      sessionID: context.sessionID,
      agent: context.agent,
      messageID: context.messageID,
      callID: context.id,
      resources: [resource],
    })
    try {
      yield* ctx.permission.assert({
        sessionID: context.sessionID,
        agent: context.agent,
        source: { type: "tool", messageID: context.messageID, id: context.id },
        action: "shell",
        resources: [resource],
        save: [save],
      })
    } finally {
      pendingAssertions.delete(key)
    }
    const argv = ["--cwd", cwd, "gh", "--", ...ghArgv]
    const output = yield* runGuard(argv, nativeEvent, cwd)
    return { content: output }
  })
}

export default Plugin.define({
  id: PLUGIN_ID,
  effect: Effect.fn("StarfleetPolicyGuardOpenCode.setup")(function* (ctx) {
    if (ctx.app.version !== "2.2.5") {
      throw new Error(`This Policy Guard plugin supports OpenCode 2.2.5; found ${ctx.app.version}`)
    }
    yield* ctx.tool.hook("execute.before", (event) => {
      if (event.tool !== "shell") return Effect.void
      const reason = directShellDenial(event.input)
      return reason ? Effect.fail(new Tool.Error({ message: reason })) : Effect.void
    })
    yield* ctx.permission.hook("evaluate", (event) => Effect.sync(() => {
      const key = event.source?.type === "tool" ? `${event.sessionID}\u0000${event.source.id}` : ""
      evaluateCoveredShellPermission(event, pendingAssertions.get(key))
    }))
    yield* ctx.tool.transform((editor) => {
      editor.namespace({
        name: "prime_directive",
        description: "Policy Guard owner, report, and covered GitHub delivery tools",
      })
      editor.add({
        name: "manage",
        description: "Review or apply Policy Guard enrollment, inspect state, record an actual report, discover a PR, or perform an explicit owner handoff.",
        input: manageInput,
        options: { namespace: "prime_directive", codemode: false },
        execute: (input, context) => manageTool(ctx, input, context),
      })
      editor.add({
        name: "gh",
        description: "Perform one attached, report-checked GitHub readiness or merge action after OpenCode's native shell permission decision.",
        input: deliveryInput,
        options: { namespace: "prime_directive", codemode: false },
        execute: (input, context) => deliveryTool(ctx, input, context),
      })
    })
  }),
})
