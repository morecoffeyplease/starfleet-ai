export function isCoveredShellResource(action, resource) {
  if (action !== "shell" || typeof resource !== "string") return false
  return /^\s*gh[\t ]+pr[\t ]+(?:ready|merge)(?=$|[\t ])/u.test(resource)
}

export function isSameResources(left, right) {
  return Array.isArray(left) && Array.isArray(right) &&
    left.length === right.length && left.every((value, index) => value === right[index])
}

export function shellResource(input) {
  if (!input || !["ready", "merge"].includes(input.action)) {
    throw new Error("Choose a covered GitHub readiness or merge action")
  }
  if (typeof input.target !== "string" || !/^[A-Za-z0-9_./:-]{1,512}$/u.test(input.target)) {
    throw new Error("The pull request target must use a plain URL, number, or branch token")
  }
  if (input.action === "ready" && (input.mergeType || input.auto || input.deleteBranch)) {
    throw new Error("Merge options cannot be used with readiness")
  }
  const argv = ["pr", input.action, input.target]
  if (input.mergeType) {
    if (!["merge", "rebase", "squash"].includes(input.mergeType)) {
      throw new Error("Choose one supported merge method")
    }
    argv.push(`--${input.mergeType}`)
  }
  if (input.auto) argv.push("--auto")
  if (input.deleteBranch) argv.push("--delete-branch")
  const resource = ["gh", ...argv].join(" ")
  return { argv, resource, save: `gh pr ${input.action} *` }
}

export function pendingAssertionMatches(event, pending) {
  return Boolean(
    pending &&
    event.action === "shell" &&
    event.sessionID === pending.sessionID &&
    event.agent === pending.agent &&
    event.source?.type === "tool" &&
    event.source?.messageID === pending.messageID &&
    event.source?.id === pending.callID &&
    isSameResources(event.resources, pending.resources),
  )
}

export function evaluateCoveredShellPermission(event, pending) {
  if (!event || event.action !== "shell") return
  if (pendingAssertionMatches(event, pending)) return
  if (!Array.isArray(event.resources) ||
      !event.resources.some((resource) => isCoveredShellResource(event.action, resource))) return
  event.effect = "deny"
  event.message = "Use the Prime Directive GitHub tool for covered readiness and merge actions."
}

export function directShellResource(input) {
  if (!input || typeof input !== "object" || typeof input.command !== "string") return undefined
  const directCommand = /(?:^|[;&|]+\s*|\n\s*|\(\s*)(gh[\t ]+pr[\t ]+(?:ready|merge))(?=$|[\t ;&|)\n])/u
  const match = directCommand.exec(input.command)
  if (!match) return undefined
  const resource = match[1].replace(/[\t ]+/gu, " ").trim()
  return { resource, save: `${resource} *` }
}

export function directShellDenial(input) {
  if (!directShellResource(input)) return undefined
  return "Use the Prime Directive GitHub tool for covered readiness and merge actions."
}
