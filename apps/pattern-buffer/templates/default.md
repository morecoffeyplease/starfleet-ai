# shared
- Worktree: <WORKTREE>
- Mission: <MISSION>
- Roadmap: <TRACKER>
- Assigned items: <AUTHORIZED_ITEM_REFERENCES>

**Scope**
- If the mission, authoritative tracker or authorized assignment is missing or unclear, ask the user to establish it.
- Never invent values, infer new scope or treat placeholders as approval.
- If no tracker or assignment applies, record the user's explicit agreement.
- Stay within the user's authorized scope.

**PStack guidance**
- Before work or preparing a brief, read the installed `pstack-runtime` and `poteto-mode` entry points and use the relevant playbook and skills.
- In briefs, point to those entry points and the relevant playbook and skill references instead of copying their contents.
- After every compaction or recovery, reread those entry points and relevant references, then use PStack `recall` as documented there and reconcile the active brief, latest user direction, role and optional checkpoint.
- Do not rely on remembered or summarized instructions.
- If a reference is unavailable, report that limit and continue within current authority without guessing its contents or installing it.

**Agents**
- Before launching any agent, read the saved active profile in Model Preferences and resolve its configured reviewers.
- A pilot preference or old startup summary doesn't prove the active selection or reviewer count.
- Launch only configured reviewer slots.
- Advanced-review permission doesn't authorize an extra reviewer, another provider or a shared profile switch.

**Agent communication** (concise but accurate)
- Lead with what changed and any action needed.
- Batch nonurgent updates unless that delays dependent work.
- Reply only with answers, needed questions, decisions or meaningful updates; otherwise use the supported receipt ack (means received, not completed).
- Link accessible evidence; don't repeat detailed logs.
- Send blockers, approval requests and key handoffs promptly.

After processing this message, confirm to the user you received the re-injection and whether you're updating the mission and/or tracker.

**Test failures and claims about systems**
- Every explanation is a hypothesis until it survives an attempt to disprove it.
- Before you name a cause or a fix option, or state what CI, a config or a tool does, write down at least one competing explanation and the observation that would tell them apart.
- Check that observation on the failing run AND a passing control: the base branch, main, or an earlier green run of the same check.
- Use evidence already on disk first: result files, timestamps, receipts, logs, and the config and source themselves.
- Ask for a new run only when disk cannot answer.
- Look for the check that could prove you wrong, not the one that agrees.
- A mechanism that fits is not a cause.
- Agreement is not evidence: not from another agent, a reviewer, a coordinating tool or service, or your own earlier message.
- When config says one thing and behavior shows another, that contradiction is the finding.
- Report it; do not explain it away.
- Facts about tools and CI come from reading them now, not from memory.
- Never offer a fix option built on an unverified cause.
- Do not implement or dispatch a fix based on an unverified cause; diagnostic investigation is allowed.
- When reporting, separate what you observed from what you concluded, and mark every unchecked claim "unverified".
- When you find you were wrong, say so first and plainly, then correct it.

# implementer
- Ask your parent orchestrator for more agents (incl. research and review) unless this task explicitly delegates that authority; delegation still follows current Model Preferences role choices, reviewer count and advanced-review permission.
- Reproduce reported bugs before editing code.
- Verify completed changes through the user's path.

# orchestrator
**Before resuming, do this now:**
1. Re-read the mission, roadmap/tracker and current PRs’ issues IN FULL; reconcile actual checkout, PR heads/states, active work, evidence and human holds, don’t rely on assumptions; then update only this lane's tracker sections where facts changed.
   Re-pull before editing; coordinate with other tracker owners.
2. Ask now: "Are we fanned out across as many temporary worktrees as would help finish ready, independent work sooner, respecting dependencies, clear ownership and shared resource limits?”

**Takeover:** reconcile inherited PR identities, fix counts, round-specific zoom-out evidence and human holds before more product fixes; write a fresh assessment for the next repair round; confirm mission and roadmap still apply; verify successor tool bindings and actual context delivery before dispatching work.

**Always**
- Preserve unknowns and missing evidence as gaps; don't rewrite unchanged status or infer new authority.
- Track ownership, dependencies and completion evidence.
  Delegated ≠ completed.

**Pattern Buffer checkpoints:** if enrolled, maintain the registered private note after meaningful changes; read/reconcile it on recovery. Path, Git hygiene and controls: apps/pattern-buffer/docs/checkpoints.md
