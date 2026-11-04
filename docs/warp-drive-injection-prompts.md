# Warp-drive agent guidance

These excerpts describe useful role guidance for Warp-drive.
Adapt them to the repository and role context where they are used.
They do not grant authority beyond the operator's existing instructions.

## Shared guidance for participating agents

For CPU-intensive local work, read `warp-drive guide --print` and use its reservation workflow.
Eligible unserved worktrees are selected by authorized boost, least recently served round, then manual order, with waiting credit and stable request details breaking ties.
The existing protected aged-target path keeps its earned-credit selection and protected-turn behavior.
Submit one meaningful CPU phase per reservation and declare the phase's actual simultaneous CPU worker count, workload family, size, and expected runtime.
Do not sum worker counts across serial phases.
Within one checkout, keep only one CPU-intensive request eligible when commands share generated files, caches, or a database.
Enable each dependent phase after its predecessor finishes.

Prepare and own the environment.
Warp-drive determines order and does not manage setup, teardown, or follow-up work.
Use the documented helper and completion-wake path.
Keep the request ID and retry key.
Use status or inspect for passive observation, and attach only for documented recovery.
An unknown result is not permission to rerun the command.

Use your own observed harness, session, and surface as the completion recipient.
Use another owner's exact enrolled identity only when that owner explicitly delegates it for this request.
Change an existing reservation's owner only with an explicit `warp-drive transfer`.
Only the operator or a person explicitly authorized by the operator may boost work or change manual order.
Record the reason for an authorized priority change.

Use help, status, and doctor for ordinary questions.
Send unresolved incidents to the designated Warp-drive responder with the request ID and evidence.
Do not infer a cause from low CPU alone or claim a hard CPU or wait-time guarantee.

## Additional coordinator guidance

When delegating a parent recipient, provide the observed enrolled harness, session, and surface for that request.
That identity is recorded only as the reservation's current owner and completion recipient.
Do not infer or borrow another identity.

Let fitting worktrees take their ordinary turns.
A protected aged-heavy turn may delay fitting heavy backfill only under the admission conditions documented in the component README.
Keep light access and unrelated fitting work available while unmanaged load is the blocker.
An escape requires fresh observations and the documented eligibility credit, projected-load, startup, and memory-pressure checks.
Do not present the soft fairness target as a hard capacity or wait-time guarantee.

A batch does not grant extra priority.
Keep dependent phases under their owner's sequencing control.
Do not build private admission loops or add environment-management requirements to Warp-drive.

## Incident responder guidance

Inspect a surfaced exception using the responder's assigned model and effort limits.
Distinguish observed idleness, missing watcher signals, overdue work, and failed delivery from an established cause.
Check the owner and watcher, preserve useful evidence, and return ordinary follow-up to the responsible agent.

Record the incident, evidence references, action, and observed outcome in the existing incident record, including unknowns.
Revisit only when new evidence arrives or a person explicitly requests another review.
Do not kill or replay product commands, bypass ordering, change global policy, or expand scope.
Escalate an unresolved decision instead of repeating read-only polling.
