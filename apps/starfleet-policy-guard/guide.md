# Policy Guard guide

Policy Guard keeps delivery reports and their local history for a native orchestrator seat.
It can refuse covered GitHub readiness and merge commands when an attached checkout or pull request has no valid report.
It does not run audits or verification, classify prompts, grant readiness authority, or enforce fix rounds.

## Requirements

Use Python 3.10 or later, Git, and the GitHub CLI `gh` on PATH.
Use a clean Starfleet source checkout for the first reviewed installation.
The owner harness and session ID must come from the actual Claude or Codex runtime, or from the OpenCode server plugin context described below.
The OpenCode prototype targets only version 2.2.5 and its server plugin API.

## Install one seat and attach a checkout

Choose a permanent owner checkout and the product checkout that contains the real branch.
Print this guide with `python3 /path/to/starfleet-policy-guard/lane.py guide --print` before setup.
The same read-only guide ships in the immutable runtime and prints outside Git without credentials, enrollment or state writes.
Review the read-only plan before applying it:

```sh
python3 /path/to/starfleet-policy-guard/lane.py --cwd /product/worktree --session-root /permanent/owner-seat \
  plan --lane --host github.com --owner-harness claude --owner-session ACTUAL_NATIVE_SESSION_ID
python3 /path/to/starfleet-policy-guard/lane.py --cwd /product/worktree --session-root /permanent/owner-seat \
  install --lane --host github.com --owner-harness claude --owner-session ACTUAL_NATIVE_SESSION_ID --plan-sha256 REVIEWED_PLAN_SHA256
```

Use `codex` for a Codex owner.
The first install result returns an immutable `seat_runtime` path.
Use that installed runtime CLI for later checkout attachments so the workflow keeps working after the source worktree is deleted:

```sh
python3 /INSTALLED/SEAT/RUNTIME/lane.py --cwd /another/product/worktree --session-root /permanent/owner-seat \
  plan --lane --host github.com --owner-harness claude --owner-session ACTUAL_NATIVE_SESSION_ID
python3 /INSTALLED/SEAT/RUNTIME/lane.py --cwd /another/product/worktree --session-root /permanent/owner-seat \
  install --lane --host github.com --owner-harness claude --owner-session ACTUAL_NATIVE_SESSION_ID --plan-sha256 REVIEWED_PLAN_SHA256
```

Use `codex` in those commands for a Codex owner.
Each later attachment reuses the seat hook and entrypoint and adds only checkout data.
Use `discover` from an attached product checkout after publishing its real PR; covered seat `gh` actions also discover the current branch's published PR before gating.
You may attach a native-owner lane before a PR exists.
For an active attachment whose recorded product root and branch match the current checkout, `status` reports `installed_here: true` even when `pr_url` is empty.
This reports local checkout configuration, not native hook activation or delivery eligibility.
An inactive attachment or a different checkout root or branch reports `installed_here: false`.
Use `status` and `coverage --summary` from the owning native session to inspect attachment, owner, activation observations, and PR binding separately:

```sh
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree status --session-id ACTUAL_NATIVE_SESSION_ID
python3 /path/to/installed/runtime/lane.py --cwd /product/worktree coverage --summary
```

Covered delivery remains PR-bound and requires a real PR binding and a recorded delivery report.
The plan and install commands do not create a PR, fetch, change branches, run checks, or update GitHub records.

## OpenCode 2.2.5 prototype server plugin

This prototype targets the server plugin tools from an actual OpenCode 2.2.5 server session.
For the first enrollment, configure the clean committed source checkout's `opencode-plugin` directory in the project's `opencode.jsonc` or the intended OpenCode config:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["/CLEAN/SOURCE/apps/starfleet-policy-guard/opencode-plugin"]
}
```

OpenCode local plugin entries must point to a directory; the plugin loader finds `index.ts` inside that directory.
Use the `prime_directive_manage` tool with `operation: "plan"`, the product checkout, and permanent seat root.
Review the plan and its exact `plan_sha256`, then use `operation: "install"` with that same hash.
The install result returns an immutable `opencode_plugin_path` directory under the seat runtime.
Configure that returned directory for later sessions and restart or reload the OpenCode server as needed for its config loader.
Use the same actual OpenCode session and session API project/location for later seat operations.

The plugin reads `sessionID`, agent, message ID and call ID from the actual server tool context, then calls the OpenCode session API for the session's exact ID, parent ID, project ID and location directory.
The `OPENCODE_SESSION_ID` shell environment variable is not an owner identity and cannot enroll a seat.
The native server plugin passes its event and tool arguments as JSON on stdin to the local `lane.py opencode` bridge.
The bridge validates the event shape but does not authenticate it cryptographically.
A local caller that can invoke this bridge can manufacture event JSON and attempt covered operations without the plugin's native permission assertion, subject to the remaining local owner, checkout, and report checks.
This is a cooperative workflow guard, not an adversarial local-process or operating-system boundary.
Do not give untrusted local workers access to the bridge or seat state, and do not claim that plugin source or its JSON event proves native authorization.
The root session and every child session are distinct owners, and a child does not inherit a parent's seat.
Use `prime_directive_manage` with `operation: "rebind-owner"` from the incoming actual session for an explicit owner-authorized handoff.
The handoff preserves seat and PR history, archives the old owner's receipt, and requires a new report from the incoming owner.

Use `prime_directive_gh` only for supported `ready` and `merge` actions.
It asks OpenCode for the native `shell` permission using the equivalent plain `gh pr ready` or `gh pr merge` resource before Policy Guard checks the owner, checkout, branch, PR, source and fresh report immediately before forwarding.
An OpenCode native `ask` still waits for the operator, and a configured native `deny` remains final.
The plugin refuses plain `gh pr ready` and `gh pr merge` command segments at the start of a shell command or after a command-list operator before shell execution.
Wrapped commands, opaque shell syntax, alternate executables, scripts, API and web requests, MCP tools and external processes remain outside this bounded coverage.
This does not claim an arbitrary shell sandbox or complete interception of every GitHub delivery route.
The plugin source and local fixtures alone do not prove that a native OpenCode server loaded the plugin or preserved a real user's permission decision.
Do not claim OpenCode support qualification until the actual native pilot and Policy Guard owner acceptance are complete.

Codex currently uses the seat's explicit `gh` entrypoint for covered readiness and merge actions.
Claude's installed hook covers the supported direct `gh pr ready` and `gh pr merge` command forms in attached repositories.
On the seat entrypoint, a full PR URL determines its host and repository even when `--repo` is also present; unmatched explicit repository selectors pass through, while selectors ambiguous across attached hosts require a full URL or `HOST/OWNER/REPO` and unknown PRs within an attached repository remain gated.
Published-branch discovery runs only on the explicit seat `gh` delivery path; local Claude hook evaluation does not make GitHub calls.
Neither path intercepts arbitrary shell scripts, other applications, or commands outside its documented entrypoint.

## Upgrade an installed seat

Use a clean, committed newer Starfleet source checkout and run the reviewed upgrade from an active attached product checkout:

```sh
python3 /clean/starfleet-ai/apps/starfleet-policy-guard/lane.py --cwd /product/worktree upgrade-plan
python3 /clean/starfleet-ai/apps/starfleet-policy-guard/lane.py --cwd /product/worktree upgrade --plan-sha256 REVIEWED_PLAN_SHA256
```

Review the exact plan hash, old and new source fingerprints, and every active checkout listed in the seat-wide preimages before applying it.
The upgrade updates the active attachments together, archives each receipt in that checkout's PR history, and requires a fresh report from each checkout before covered actions can pass.
Migration records the target settings mode and immutable runtime pin in each migrated checkout, and preserves the target settings postimage hash even when the seat already owns its hooks.
For an earlier migrated checkout with missing runtime metadata and a stale settings mode, `upgrade-plan` can propose recovery only when its source path and pinned fingerprints match the owning seat runtime and its live settings path, content hash, and mode match the durable seat metadata.
Review each `migration_metadata_recovery` entry in the plan, then apply the exact plan hash with `upgrade`.
For this metadata recovery path, changed settings content or mode, mismatched paths, invalid pins, and malformed runtime metadata remain refused.
If an interrupted upgrade leaves a journal, use the reviewed source and same product checkout to restore the recorded preimages:

```sh
python3 /clean/starfleet-ai/apps/starfleet-policy-guard/lane.py --cwd /product/worktree upgrade-recover --rollback
```

## Inspect, hand off, and recover

Run `python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID status` to inspect a permanent seat even when product checkouts are unavailable.
Run `python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID registrations` to inspect attached PR history without a product checkout.
Run `coverage --summary` or `status` from an attached checkout to inspect local configuration and observations.
Configured hooks and adapters are not proof that the native runtime loaded or used them.
Use `python3 /path/to/installed/runtime/lane.py --seat-id SEAT_ID rebind-owner --from-harness claude --from-session OUTGOING_SESSION_ID --confirm-handoff` only after an authorized transfer to the incoming actual native session.
`--from-harness` and `--from-session` identify the exact outgoing owner; the incoming harness and session are detected from the current native runtime.
Handoff preserves history and requires a fresh report from the incoming owner.
For a cross-harness handoff, the transaction adds or removes only the seat-owned Claude hook pair as needed and preserves unrelated settings.
Coordinate the existing single-writer settings checkpoint before a Claude settings change, and preserve native trust requirements.
The settings change records configuration only and does not prove that the incoming native runtime loaded or trusted the hook.
If the seat `gh` entrypoint, Claude hook or a non-CLI runtime file is missing while the installed `lane.py` is intact, run `recover --inspect`, review `recover --plan`, then apply its hash with `recover --apply --plan-sha256 PLAN_SHA256` through the installed runtime.
If the installed runtime's `lane.py` or its entire directory is missing, use a clean current source checkout whose runtime fingerprint matches the seat's recorded version, then run those same recovery commands with that source `lane.py`.
Recovery verifies the exact recorded file hashes before restoring missing files, and changed runtime files fail closed for inspection rather than being overwritten.
If a legacy per-worktree source checkout or hook is missing, run `migrate-plan` and `migrate` from a clean current source checkout while the existing product Gitdir and state are available.
Migration accepts an active legacy lane with no PR when its recorded checkout root and branch match the current product checkout.
The migration plan preserves its existing lane and owner history without creating a PR binding or report.
It continues to refuse inactive legacy state and a product branch that differs from the recorded branch.
For a legacy installation without a native owner, include `--confirm-owner` on both commands.
For a legacy installation owned by another native session, pass `--confirm-handoff --from-harness claude --from-session OUTGOING_SESSION_ID` on both commands, using the exact recorded outgoing harness and session.
Migration preserves legacy state and requires exact owner confirmation or an authorized handoff when required.
Do not recreate receipts, manual counts or external evidence files.

The runtime is copied into a versioned user data directory and local history stays in the user state directory.
Product Git directories are not used as the durable history store.
No daemon is installed.

Covered adapter delivery calls refresh a read-only GitHub PR state observation, and a successful covered merge refreshes it once more after forwarding.
An observed closed PR becomes eligible for local retention after 30 days by default.
Run `retire --pr-url https://github.com/owner/repo/pull/123` from an attached checkout, or add the global `--seat-id SEAT_ID` before `retire` when no product checkout is available.
Use `pin --pr-url ...` and `unpin --pr-url ...` with the same seat-selection rule to protect or release one local PR record.
Preview cleanup and review its exact candidate hash before applying:

```sh
python3 /path/to/starfleet-policy-guard/lane.py --seat-id SEAT_ID cleanup --preview --retention-days 30
python3 /path/to/starfleet-policy-guard/lane.py --seat-id SEAT_ID cleanup --apply \
  --retention-days 30 --plan-sha256 REVIEWED_PLAN_SHA256
```

Run cleanup from the exact owning native session.
Use global `--seat-id SEAT_ID` before `cleanup` to restrict the preview and application to that seat when no product checkout is available.
Review the candidates and hash before applying the same plan.
Cleanup is bounded and opportunistic during installation and explicit maintenance for the exact current seat owner.
It prunes only known terminal local history past retention and unreferenced runtime versions.
It never removes active, uncertain, pinned, recovery-journaled or referenced records, or external packets, audits and manual counts.

## Scope and authority

`record` requires the report execution but permits documented checklist gaps.
The local verification command must exactly match the repository's documented canonical command, and its source and evidence hashes must be recorded.
Policy Guard does not infer a package manager or run the verification command while reporting.
Real audit findings, deviations approved by the configured human authority, local verification and repository delivery rules remain required.
The guard never bypasses native trust or automatic approval review.
Only the active exact native owner may use an attached seat; replacement sessions require the explicit handoff command.
Use `cleanup --preview` to review eligible old terminal local records before `cleanup --apply` removes them.

For detailed behavior and limitations, see the component README.
