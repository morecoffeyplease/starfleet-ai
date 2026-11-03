# Status reports

Away Mode reports collect a bounded view of work from the exact lanes enrolled when a report is created.
The report retains its original time window and lane roster, including missing responses.
Use an owner-reported update as reported evidence, not as proof of delivery, independent verification, review, merge or deployment.

## Request and collection

Use `away-mode report --request-id STABLE_INTENT_ID` for the two hours before the request, or the return workflow for the recorded away interval.
Use `--prepare-only` when a report should be recorded without opening a briefing workspace.
Inspect with `away-mode report-show REPORT_ID`, then collect and render with the CLI commands shown by `away-mode --help`.
The detailed render retains reported item evidence and the complete ordered roadmap.
Do not silently change the requested window, infer ownership from a terminal title or treat an empty response as no work or no blockers.
Reconcile uncertain sends by inspecting the exact enrolled inbox before retrying.

## Lane update

Submit JSON through `away-mode report-submit REPORT_ID --lane LANE --file UPDATE.json` from that lane's exact enrolled owner.
The legacy `attention` and `items` fields remain supported.
New reports can add a concise `headline`, assessed `brief_blockers`, and a `roadmap` with its authoritative source, completeness flag and ordered entries.
Each detailed item records its roadmap reference, progress, current state, next authorized step and blockers.
Optional effort estimates use before, current and within-window accomplished percentage ranges, with a scope and estimation basis.
Use `null` for an unknown range and explain scope changes in the revision field.
Shipped outcomes are actual merges or deployments; useful local implementation and verification remain separate evidence states.
Keep unresolved decisions with impact and recommendation, identify blockers and possible parallel work, and name evidence gaps.
When roadmap coverage is incomplete, set `complete` to false and list the known gaps.

The sample response template is in `../templates/status-response.txt`.
The request, ETA and follow-up templates are starting points; adapt the requested fields and timing to the actual report and owner.
Templates do not grant new work, test, agent-launch or interruption authority.
