# Hand off an orchestrator session

Use this checklist for an authorized handoff.
Keep the handoff in the existing private checkpoint or packet.
Use the approved mission, next action, and holds throughout the handoff.

## Notify the outgoing owner

If you are initiating the authorized handoff, send this alert to the exact outgoing session through native CMUX agent messaging.

```text
Start the authorized handoff for <approved scope> at your next safe boundary.
Run replicator guide --print and follow the outgoing checklist.
Start diagnostics with starfleet doctor --json.
Use the current component guides linked in Replicator for exact owner checks
and transfer steps.
```

## Outgoing owner

1. Prepare the checkpoint below and find a safe boundary for the work you are handing over.
   Check the exact checkout and the owner records for live children, jobs, timers and occurrences, pending replies, and tool registrations.
   Use the authorized successor already open, or open one through the existing CMUX terminal route described below.
   Send the private checkpoint path and a short orientation on the approved scope and holds to the exact incoming owner through native CMUX agent messaging.
2. Before releasing work, stop starting new writes on that scope and recheck the checkout, owners, and obligations that could have changed.
   Use each tool's supported owner workflow and record its outcome in the checkpoint.
   Record your release to the exact successor once the work is ready for that owner to accept.
   The incoming owner's timestamped acceptance of your release is the handoff point and makes the successor the sole writer for that scope.
3. Settle any obligations you still own, then deregister your own exact binding from each service that still lists you.
   Keep any binding needed for a retained job, pending reply, or timer occurrence until that obligation is resolved.
   Confirm the remaining dispositions in the checkpoint and exit normally after verified acceptance and cleanup.

### Open a successor when needed

For an authorized terminal launch, reuse the installed native Claude or Codex entry point and the approved options.
Read `cmux new-surface --help` and use the exact workspace and worktree.

```sh
cmux new-surface --type terminal --workspace WORKSPACE_ID --working-directory /worktree --command 'AUTHORIZED_INSTALLED_LAUNCH_COMMAND'
```

Let the installed integration assign a fresh native session ID.
Preserve the approved model, effort, permissions, and hook trust settings.
If launch or binding is uncertain, inspect the exact candidate before attempting another launch.

## Incoming owner

1. Read the checkpoint, approved mission and tracker, repository guidance, and applicable component guides yourself.
   Verify your exact native session, CMUX surface and workspace, worktree, model and effort, effective permissions, and hook trust.
   Check the tool's recorded generation when it binds an owner by generation.
   Use the exact session's runtime report, user confirmation, or saved model and effort settings with launch arguments that show no override for model and effort evidence.
   Record any unavailable live reading and resolve actual conflicts before the dependent transition.
2. Register your exact session with all applicable Starfleet owner tools.
   Adopt existing resources through their documented routes.
   Where adoption is unavailable, create and verify an equivalent own binding only when the tool supports it and it preserves obligations without duplicate work or notices.
   Keep unsupported or uncertain resources with their verified owner and record what they block.
   Verify actual receipt where delivery is required to protect an obligation.
   Record timestamped acceptance of the outgoing owner's release when required custody for the work is verified.
   Continue the approved next action as its sole writer.
3. After the outgoing owner settles its resources and exits normally, verify the exact native process has ended.
   Close only the verified outgoing CMUX tab if it remains open.
   An unresolved resource or uncertain exit holds dependent cleanup.

## Copy into the existing private checkpoint

For a Pattern Buffer checkpoint, retain these headings and fill in the current values.

```markdown
# Working checkpoint

## Current state

- Approved outcome and scope: <what the user authorized>
- Checkout: <repository, worktree, branch, head, and uncommitted changes>
- Outgoing and incoming: <exact harness, session, CMUX surface, and workspace>

## Next action

- Approved next action: <one concrete action and its completion evidence>

## Decisions and uncertainty

- Existing grants, decisions, and unresolved questions: <references>

## Active work

- <tool and exact resource ID> | <verified owner and scope> | <job, reply, timer occurrence, or binding state> | <adopted, recreated, retained, resolved, or blocked> | <evidence>

## Evidence

- Current proof and resource queries: <references and results>
- Outgoing release: <exact outgoing and incoming identities, scope, timestamp>
- Incoming acceptance: <exact incoming identity, scope, timestamp, verified custody>
- Outgoing cleanup: <remaining obligations, deregistration and verified exit>

## Holds and guidance

- Human holds, intentional exclusions, and blocked actions: <references>
- Discovery: run `replicator guide --print` and `starfleet doctor --json`.
- Follow the current component guides linked in Replicator for exact owner checks and transfer steps.
```

## Inspect status and find current tool instructions

Start with `starfleet doctor --json` for the aggregate inventory.
Use `replicator preflight --context-file /path/to/standing-context.md --guidance-file /path/to/AGENTS.md --json` for the exact caller, worktree, and access checks.
Run preflight from the relevant native session and worktree.
Use `cmux current --json`, `git status --short --branch`, and `git rev-parse HEAD` to reconcile your identity and checkout with the packet.
Read [Replicator's README](https://github.com/morecoffeyplease/starfleet-ai/tree/main/apps/replicator#readme) for installation, prerequisites, and preflight limits.
Status and preflight are inspection only.
A configured registration or submitted wake alone cannot establish receipt, custody acceptance, or cleanup.

Use the current component guide for enrollment, transfer, verification, and cleanup.
Check installed CLI help before applying commands to an older runtime.
Query only the exact owned records and authorized transfer scope.

| For | Inspect | Current instructions |
| --- | --- | --- |
| Standing context, checkpoint, and child tasks | `pattern-buffer enrollment-status`, `checkpoint-status`, and `task-status --task-id TASK_ID` with the exact owner inputs from the guide | `pattern-buffer guide --print` and [Pattern Buffer session enrollments](https://github.com/morecoffeyplease/starfleet-ai/blob/main/apps/pattern-buffer/docs/session-enrollments.md) |
| Native attention and timers | `hailing-frequencies status`, `temporal-beacon list`, `temporal-beacon inspect TIMER_ID` | [Hailing Frequencies handoff](https://github.com/morecoffeyplease/starfleet-ai/blob/main/apps/hailing-frequencies/guide.md#receive-an-authorized-handoff) and the [Temporal Beacon guide](https://github.com/morecoffeyplease/starfleet-ai/blob/main/apps/hailing-frequencies/temporal-beacon.md) |
| Managed conversations and pending replies | `discord-watch status`, `discord-watch managed THREAD_ID` | `discord-watch guide --print` and the [Agent Notify skill](https://github.com/morecoffeyplease/dotclaude/blob/main/skills/agent-notify/SKILL.md) for the owning conversation |
| Requests, helpers, and reservations | `warp-drive status`, `warp-drive inspect REQUEST_ID` | `warp-drive guide --print` |
| Seats, attachments, and legacy routes | Exact installed Policy Guard seat status, registrations, and checkout coverage | [Policy Guard handoff and recovery](https://github.com/morecoffeyplease/starfleet-ai/blob/main/apps/starfleet-policy-guard/guide.md#inspect-hand-off-and-recover) |
| Model selection and owner registration | `model-preferences status` using the applicable configuration and state | [Model Preferences README](https://github.com/morecoffeyplease/starfleet-ai/tree/main/apps/model-preferences#readme) |
| Presence and lane owner | `away-mode context` | `away-mode guide --print` |

Keep worktree-default Pattern Buffer standing context where it applies and register the successor's own checkpoint.
A Policy Guard seat transfer may affect multiple attachments.
Check every applicable seat and legacy route before moving custody.
Use established installations and preserve concurrent work, history, and shared services.

### Policy Guard seat handoff

Inspect the exact permanent seat and its attachments with the installed pinned CLI before changing custody:

```sh
python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID status
```

Record the seat ID, birth root, operational anchor, exact owner, every active or retired attachment, covered PR, and any missing or changed worktree in the existing checkpoint.
Record the authorized release of the approved scope to the exact incoming owner in the existing checkpoint, with the outgoing owner recording it when available.
If that owner is unavailable, use the applicable existing authority process to record the disposition and hold the scope until it is resolved.
The incoming owner verifies their exact native identity, runtime and hook trust, and accepts the same scope in the checkpoint before rebind.
When status supplies `handoff.rebind_owner_command`, the incoming actual native session should use that exact command.
If the installed CLI is unavailable or outdated, the documented same-harness maintenance fallback remains supported when its full runtime-pin checks pass.
The incoming owner becomes the only writer after acceptance and the authorized rebind.
The command updates active attachment ownership and source pins together, while preserving retired rows and their histories.
For cross-harness transfers, review the receipt's exact Claude settings path, before and after hashes, and owned hook groups.
Review changed and retained checkout dispositions by branch and PR, then run each available command in `verification.coverage_commands` to inspect local coverage and report state.
Use actual native tool output when a relevant action has occurred, but do not create a production action or restart solely to validate a handoff.
If no native event occurred, record that interception remains unobserved; `verification.observed.state` is a static note about the rebind and does not update later.
The supported explicit adapter probe is non-forwarding and does not prove native hook interception.
Prior reports are archived where present, so a fresh report is required for each rebound active checkout with a covered PR.
Do not declare sole custody while an attachment, owner, permission, or trust state is unresolved.

## Review checkout acceptance and outgoing retirement

Add a private owner evidence file when a checkout can be accepted before the outgoing session retires.
Run the same preflight with the file and keep its report in the private handoff record.

```sh
replicator preflight --context-file /path/to/standing-context.md \
  --guidance-file /path/to/AGENTS.md --handoff-file /path/to/handoff-evidence.json
```

The file is a JSON object containing exact scope and evidence references.
For example, record an accepted checkout, a pending Discord reply that still depends on the outgoing session, and an old Warp record with an unknown exit and documented absence of dependent work or delivery.

```json
{
  "checkout": {
    "worktree": "/path/to/worktree",
    "head": "1234567890abcdef1234567890abcdef12345678",
    "acceptance": "accepted",
    "evidence": "Incoming owner checkpoint, acceptance receipt for this head"
  },
  "resources_complete": true,
  "resources": [
    {
      "tool": "discord-watch",
      "id": "1234567890123456789",
      "state": "pending",
      "affects": ["outgoing_retirement"],
      "evidence": "Exact thread status reports a reply awaiting delivery"
    },
    {
      "tool": "warp-drive",
      "id": "11111111-2222-4333-8444-555555555555",
      "state": "unknown",
      "affects": [],
      "evidence": "Verified-stop receipt and successor confirmation that no delivery or work depends on this record"
    }
  ]
}
```

Use `accepted`, `pending`, or `unknown` for checkout acceptance.
An accepted record needs the exact absolute worktree path, full commit ID, and an acceptance evidence reference.
Set `resources_complete` only after verifying all applicable resources for the scoped handoff.
For each resource, use `active`, `pending`, `settled`, or `unknown` and list the actions that still depend on it in `affects`.
The supported actions are `checkout_acceptance` and `outgoing_retirement`.
An empty list requires evidence that there is no remaining dependency.
Use short references to receipts, logs, or checkpoint entries and leave credentials and message contents out.
Unknown entries tied to a specific action can omit their evidence until it is available.

The report represents acceptance as recorded by the owner.
It does not fetch evidence references, verify retirement, or change the preflight exit code.
A pending or active dependency holds the named action.
An unknown dependency marks only the named action unknown.
An incomplete inventory leaves retirement unknown unless a listed resource already shows a blocker.
When retirement shows `review`, repeat the exact owner checks in this guide and confirm the outgoing process exits normally.
Use generated inspect commands as pointers and follow the component guides for any owner action.

## Handle exceptions

If a launch, receipt, or transfer is uncertain, inspect the exact record before any retry.
Keep the last verified owner and hold the dependent action when a required owner, permission, trust setting, or resource is unknown.
Treat an intentionally excluded tool separately from an incomplete inventory.
Preserve pending replies and submitted wakes with their verified owner until they are settled through supported commands.
Use supported native controls and owner commands throughout.

Resolve routine tool issues with their owner.
Ask the user in the existing conversation when a matter remains unresolved and needs their help, or when the next action needs a new scope, permission, or approval decision.
Give the exact affected scope, what you observed, where work is safely held, and what decision is needed.
Record the decision in the same checkpoint and continue independent authorized work.
