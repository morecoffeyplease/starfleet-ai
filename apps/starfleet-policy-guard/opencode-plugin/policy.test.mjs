import assert from "node:assert/strict"
import test from "node:test"
import {
  directShellDenial,
  directShellResource,
  evaluateCoveredShellPermission,
  isCoveredShellResource,
  pendingAssertionMatches,
  shellResource,
} from "./policy.js"

test("dedicated delivery resources match the shell permission shape", () => {
  const ready = shellResource({ action: "ready", target: "https://github.com/acme/app/pull/42" })
  assert.deepEqual(ready.argv, ["pr", "ready", "https://github.com/acme/app/pull/42"])
  assert.equal(ready.resource, "gh pr ready https://github.com/acme/app/pull/42")
  assert.equal(ready.save, "gh pr ready *")

  const merge = shellResource({
    action: "merge",
    target: "42",
    mergeType: "squash",
    auto: true,
    deleteBranch: true,
  })
  assert.deepEqual(merge.argv, ["pr", "merge", "42", "--squash", "--auto", "--delete-branch"])
  assert.equal(merge.resource, "gh pr merge 42 --squash --auto --delete-branch")
  assert.equal(merge.save, "gh pr merge *")
})

test("only the exact in-memory native tool assertion preserves shell permission", () => {
  const pending = {
    sessionID: "ses_parent123",
    agent: "build",
    messageID: "msg_parent123",
    callID: "call_parent123",
    resources: ["gh pr merge 42 --squash"],
  }
  const event = {
    action: "shell",
    sessionID: pending.sessionID,
    agent: pending.agent,
    source: { type: "tool", messageID: pending.messageID, id: pending.callID },
    resources: [...pending.resources],
    effect: "ask",
  }
  assert.equal(pendingAssertionMatches(event, pending), true)
  evaluateCoveredShellPermission(event, pending)
  assert.equal(event.effect, "ask")

  for (const changed of [
    { sessionID: "ses_child123" },
    { agent: "other-agent" },
    { source: { type: "tool", messageID: pending.messageID, id: "call_other" } },
    { resources: ["gh pr merge 42 --squash --auto"] },
  ]) {
    const mismatch = { ...event, ...changed, effect: "allow" }
    evaluateCoveredShellPermission(mismatch, pending)
    assert.equal(mismatch.effect, "deny")
  }
})

test("covered direct shell is denied while unrelated native effects stay untouched", () => {
  assert.equal(isCoveredShellResource("shell", "gh pr ready 42"), true)
  assert.equal(isCoveredShellResource("shell", "gh pr merge 42 --squash"), true)
  assert.equal(isCoveredShellResource("prime_directive_gh", "gh pr ready 42"), false)
  assert.equal(isCoveredShellResource("shell", "gh pr view 42"), false)

  const direct = { action: "shell", resources: ["gh pr ready 42"], effect: "allow" }
  evaluateCoveredShellPermission(direct, undefined)
  assert.equal(direct.effect, "deny")

  const unrelatedShell = { action: "shell", resources: ["git status"], effect: "ask" }
  evaluateCoveredShellPermission(unrelatedShell, undefined)
  assert.equal(unrelatedShell.effect, "ask")

  const otherAction = { action: "read", resources: ["gh pr ready 42"], effect: "allow" }
  evaluateCoveredShellPermission(otherAction, undefined)
  assert.equal(otherAction.effect, "allow")
})

test("direct covered shell segments yield a native permission resource before execution", () => {
  assert.deepEqual(directShellResource({ command: "printf before && gh pr ready 42" }), {
    resource: "gh pr ready",
    save: "gh pr ready *",
  })
  assert.deepEqual(directShellResource({ command: "echo started; gh pr merge 42 --squash" }), {
    resource: "gh pr merge",
    save: "gh pr merge *",
  })
  assert.match(directShellDenial({ command: "gh pr ready 42" }), /Prime Directive/u)
  assert.match(directShellDenial({ command: "gh pr merge 42 --squash; echo done" }), /Prime Directive/u)
  assert.match(directShellDenial({ command: "echo started; gh pr ready 42" }), /Prime Directive/u)
  assert.equal(directShellDenial({ command: "git status" }), undefined)
  assert.equal(directShellDenial({ command: "echo 'gh pr ready 42'" }), undefined)
  assert.equal(directShellDenial({ command: "env GH_HOST=github.com gh pr ready 42" }), undefined)
})
