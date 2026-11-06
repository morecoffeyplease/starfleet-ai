# Qwen runtime lifecycle

This runbook describes the isolated OpenCode 2.2.5 and Ollama 0.32.14 command path for a bounded local Qwen task.
It covers both a Codex-Qwen implementer and a Qwen-only orchestrator, including multiple worker sessions that share one owned runtime.

## Readiness gate

This is a source-checked operator recipe, not a clean-host or end-to-end qualification.
The candidate Pattern Buffer source now includes an opt-in OpenCode lower-anchor pressure reminder, descriptor validation, and checkpoint and compaction lifecycle accounting.
A native v4 case on source candidate `3c86383` exercised one exact Pattern Buffer descriptor, a low-threshold warning in each of two generations, checkpoint publication, manual compaction and lossless same-session recovery.
The component owner accepted it as scoped assisted evidence, without a production threshold or warning-timing guarantee.
A native v8 case on `3c86383` exercised registered Policy Guard plugin loading, exact ownership, report checks, native permission decisions and post-approval freshness with a scripted local provider and fake `gh`.
The OpenCode plugin and covered action/permission paths are unchanged in this integrated candidate; its later Policy Guard changes cover migration and runtime metadata recovery, which v8 did not exercise.
These cases do not establish Qwen model behavior, real GitHub action outcomes or adversarial security at the local JSON bridge.
The Prime Directive plugin passes event data as JSON to a local CLI bridge, and that bridge does not authenticate the event cryptographically.
A local caller able to invoke the bridge can forge its event shape and bypass the plugin's native permission assertion, subject to the remaining local owner, checkout, and report checks.
Treat this as a cooperative workflow guard, not an adversarial local-process or operating-system boundary.
Dispatch requires accepted Pattern Buffer pressure-warning evidence for the selected mode and an installed, enabled Policy Guard route for each covered action used by the task.

The current Model Preferences source reads the API password from the private manifest and places it in the OpenCode TUI process environment.
This source fact does not establish whether tool subprocesses can read the value; they may use a sanitized environment or execute in another process boundary.
The same source explicitly transfers the four private XDG paths to its TUI child, while its child setup inherits `HOME` from the launcher environment without assigning a new value.
The actual TUI and tool process environments have not been inspected, so credential non-exposure and full child-home isolation remain unqualified.
Do not make either isolation claim based on the manifest, profile, or source alone.
OpenCode permission rules are not an operating-system boundary.

The recorded API replay on `9092aef` exercised metadata-only parent-linked session creation and relocation to a distinct child worktree with zero model requests.
That proves only the exercised API sequence and controls, not native TUI startup, local inference, PB warning delivery, Prime Directive compliance, or Qwen-only orchestration.
The strict `native_empty_composer` delivery policy remains the default, and an unknown composer state is a hold.
Do not switch to `api_sidechannel` to get past a hold.

## Before opening a run

Before costly inference, run Model Preferences `preflight` for the exact active role and worktree, then follow the [Qwen inference and test observer preflight](qwen-operating-guide.md#inference-and-test-observer-preflight).
The read-only API result checks saved model configuration and optional server-reported plugin status; it is not native TUI or task acceptance and does not clear the readiness gate.

1. Use an existing task-assigned CMUX workspace and work in a visible tab or pane for that task.
2. Run `cmux --json --id-format uuids identify` in the current pane, record its exact workspace and surface UUIDs, and confirm it is the intended workspace; never create a new left-rail workspace for another session in the same task.
3. Confirm `opencode --version` prints `2.2.5` and `ollama --version` prints `0.32.14`.
4. Confirm `ollama list` contains the exact existing model `qwen3.6:35b`, and inspect it with `ollama show qwen3.6:35b`.
5. Use only a model store already owned by the operator; do not pull, copy, prune, or remove model files as part of this runbook.
6. Select an exact parent task ID, task brief, owner worktree, child worktree when needed, and acceptance command before dispatch.
7. Stop if any readiness-gate item is unresolved, if either runtime version differs, if the exact model is absent, or if the required CMUX identity is unknown.

The run commands below are source checked but have not been exercised as a complete operator lifecycle on a clean host.
Do not run them until the readiness gate is cleared and the task owner authorizes the specific task.

## Create private run state

Run this in the shell opened in the existing task CMUX workspace.
Keep the shell open for the run so it retains ownership of the processes it starts.

```sh
set -eu
umask 077

REPO="$(git rev-parse --show-toplevel)"
QWEN_MODEL_STORE="${OLLAMA_MODELS:-$HOME/.ollama/models}"
BASE_STATE="${XDG_STATE_HOME:-$HOME/.local/state}"
RUN_PARENT="$BASE_STATE/starfleet-qwen/runs"
mkdir -p "$RUN_PARENT"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_ROOT="$RUN_PARENT/$RUN_ID"
mkdir "$RUN_ROOT"
chmod 700 "$RUN_ROOT"
mkdir -p "$RUN_ROOT/home" "$RUN_ROOT/xdg-config" "$RUN_ROOT/xdg-data" \
  "$RUN_ROOT/xdg-state" "$RUN_ROOT/xdg-cache" "$RUN_ROOT/logs" \
  "$RUN_ROOT/pids" "$RUN_ROOT/tasks" "$RUN_ROOT/receipts"

export HOME="$RUN_ROOT/home"
export XDG_CONFIG_HOME="$RUN_ROOT/xdg-config"
export XDG_DATA_HOME="$RUN_ROOT/xdg-data"
export XDG_STATE_HOME="$RUN_ROOT/xdg-state"
export XDG_CACHE_HOME="$RUN_ROOT/xdg-cache"
export OPENCODE_CONFIG_DIR="$XDG_CONFIG_HOME/opencode"
export OPENCODE_DISABLE_AUTOUPDATE=1
unset OPENCODE_DISABLE_PROJECT_CONFIG
mkdir -p "$OPENCODE_CONFIG_DIR"

OLLAMA_PORT=11459
OPENCODE_PORT=4096
export OLLAMA_MODELS="$QWEN_MODEL_STORE"
export OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT"
export OLLAMA_CONTEXT_LENGTH=131072
export OLLAMA_KEEP_ALIVE=0
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_NO_CLOUD=1
export OLLAMA_NOPRUNE=1
OPENCODE_API_URL="http://127.0.0.1:$OPENCODE_PORT"

test -d "$QWEN_MODEL_STORE"
opencode --version
ollama --version
```

The example uses alternate loopback ports to avoid touching a default shared listener.
If either port is occupied, choose an unused loopback port and update the environment and provider URL consistently.
Do not stop or reuse an unknown listener.
`OLLAMA_NOPRUNE=1` and the absence of pull or remove commands preserve the existing model store.

## Write the isolated OpenCode profile

Create one private OpenCode profile with one local provider and one model.
This profile declares a 131072-token context window, a 32768-token output limit, and an explicit `max` variant whose effective request body uses `reasoning_effort: max`.
Model Preferences still checks the server's effective provider, model, selected variant, request body, and output limit for the exact target directory before it creates a session.

Replace the example read and edit resources with only the task's approved files.
This deny-all example leaves shell execution disabled; adjust it only for the exact task controls that have been separately reviewed.

```sh
cat > "$OPENCODE_CONFIG_DIR/opencode.json" <<JSON
{
  "\$schema": "https://opencode.ai/config.json",
  "update": "disable",
  "model": "trial-local/qwen3.6:35b",
  "default_agent": "qwen-implementer",
  "warming": false,
  "providers": {
    "trial-local": {
      "name": "Local Qwen",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:$OLLAMA_PORT/v1"
      },
      "models": {
        "qwen3.6:35b": {
          "name": "Qwen3.6 35B",
          "limit": {
            "context": 131072,
            "output": 32768
          },
          "capabilities": {
            "tools": true,
            "reasoning": {
              "supported": true,
              "effort": ["max"]
            }
          },
          "compatibility": {
            "maxTokensField": "max_tokens"
          },
          "body": {
            "reasoning_effort": "max"
          },
          "variants": [
            {
              "id": "max",
              "settings": {
                "reasoningEffort": "max"
              },
              "body": {
                "reasoning_effort": "max"
              }
            }
          ]
        }
      }
    }
  },
  "agents": {
    "qwen-implementer": {
      "description": "Complete one bounded task in its assigned worktree",
      "permissions": [
        { "action": "*", "resource": "*", "effect": "deny" },
        { "action": "read", "resource": "AGENTS.md", "effect": "allow" },
        { "action": "read", "resource": "src/assigned-file.py", "effect": "allow" },
        { "action": "edit", "resource": "src/assigned-file.py", "effect": "allow" }
      ]
    }
  }
}
JSON
chmod 600 "$OPENCODE_CONFIG_DIR/opencode.json"
```

Do not set `OPENCODE_DISABLE_PROJECT_CONFIG` for this run because the project `AGENTS.md` instructions must remain discoverable.
Project configuration can still contribute settings, so stop if effective configuration or the Model Preferences preflight does not resolve to this exact local provider and model.
The permissions are an application-level task boundary and do not isolate shell, plugins, other processes, or credentials.

## Create a private model profile and prepare Pattern Buffer

Use a private Model Preferences configuration and state directory for this run.
Initialization writes opt-in examples and does not activate one until the explicit private `activate` command.

```sh
MP="$REPO/apps/model-preferences/model_preferences.py"
MP_CONFIG="$RUN_ROOT/model-preferences/config"
MP_STATE="$RUN_ROOT/model-preferences/state"
mkdir -p "$MP_CONFIG" "$MP_STATE"
python3 "$MP" --config-dir "$MP_CONFIG" --state-dir "$MP_STATE" init --examples
python3 "$MP" --config-dir "$MP_CONFIG" --state-dir "$MP_STATE" \
  model-set local-qwen3.6-35b --harness opencode --provider-id trial-local \
  --slug qwen3.6:35b --efforts max \
  --source "operator-pinned local provider; native qualification is pending"
```

Set `PROFILE` to exactly `Codex-Qwen` or `Qwen-only` after confirming that the private profile's other model choices match the operator's current approved configuration.
The example Codex-Qwen profile uses Codex Sol medium orchestration and Qwen max implementation and research, with Codex Sol high review slots.
The Qwen-only profile uses Qwen max for orchestration, implementation, and research, with Codex Sol high review slots.
Every review launch still requires fresh explicit authorization.
The existing Claude hold remains in effect, and the Claude-Qwen follow-up starts only after its supporting PR is merged.

```sh
# Choose exactly one value after checking the private profile's Codex choices.
PROFILE="Qwen-only"
python3 "$MP" --config-dir "$MP_CONFIG" --state-dir "$MP_STATE" \
  activate "$PROFILE" --orchestrators future-only
python3 "$MP" --config-dir "$MP_CONFIG" --state-dir "$MP_STATE" status
python3 "$MP" --config-dir "$MP_CONFIG" --state-dir "$MP_STATE" validate
python3 "$MP" --config-dir "$MP_CONFIG" --state-dir "$MP_STATE" show "$PROFILE"
python3 "$MP" --config-dir "$MP_CONFIG" --state-dir "$MP_STATE" \
  resolve --role orchestrator
python3 "$MP" --config-dir "$MP_CONFIG" --state-dir "$MP_STATE" \
  resolve --role implementer
```

For the owner worktree, opt Pattern Buffer into that exact worktree and install its plugin only into the private OpenCode settings file.
Review the owner context produced or preserved by `init`, including its `# shared`, `# orchestrator`, and `# implementer` sections, then run the component's documented validation before dispatch.
The managed Qwen-only launch assigns the orchestrator role to its exact native session.
Model Preferences performs child prepare and bind itself; do not also run the separate `prepare-dispatch` or `bind-dispatch` commands for a managed child.

```sh
PB="$REPO/apps/pattern-buffer/pattern_buffer.py"
OWNER_WORKTREE="/absolute/path/to/the/task-owner-worktree"
PB_RUNTIME="$RUN_ROOT/pattern-buffer-runtime"
python3 "$PB" --cwd "$OWNER_WORKTREE" init
# Review and update .context-injector/context.md before continuing.
python3 "$PB" --cwd "$OWNER_WORKTREE" validate
python3 "$PB" hook-config --harness opencode --runtime-dir "$PB_RUNTIME"
python3 "$PB" install --harness opencode \
  --settings "$OPENCODE_CONFIG_DIR/opencode.json" --runtime-dir "$PB_RUNTIME"
python3 "$PB" status --harness opencode \
  --settings "$OPENCODE_CONFIG_DIR/opencode.json" --runtime-dir "$PB_RUNTIME"
```

If the approved CMUX installation requires its project OpenCode hook, run `cmux hooks opencode install --project` from each exact owner or child worktree.
That is a project-scoped install command, not a read-only status check or proof of live hook delivery.
Managed dispatch must still confirm the exact session, worktree, CMUX workspace, surface, and hook generation before it releases the prompt.
The OpenCode Pattern Buffer source includes the opt-in lower-anchor pressure warning described in its guide.
It remains disabled until an exact-session descriptor is reviewed and imported.
Native v4 observed a 131072-token backend window, a 111412-token automatic-compaction ceiling and one warning before checkpoint publication.
After manual compaction advanced the generation, it observed one new warning and lossless same-session recovery.
That case used a deliberately low test threshold and does not qualify a production threshold, lead time or any untested descriptor.

## Start one owned runtime pair

Start exactly one Ollama process and one authenticated OpenCode API process for the run.
All worker sessions in this task share these two processes; never start one pair per worker.
With `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_MAX_LOADED_MODELS=1`, plan for one active Qwen inference at a time.

Check both loopback ports before starting either process.
If a port is occupied, choose a different port and update all matching variables and the provider URL.

```sh
python3 - "$OLLAMA_PORT" "$OPENCODE_PORT" <<'PY'
import socket
import sys

for value in sys.argv[1:]:
    port = int(value)
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as error:
            raise SystemExit(f"loopback port {port} is unavailable: {error}")
PY

ollama serve >"$RUN_ROOT/logs/ollama.log" 2>&1 &
OLLAMA_PID=$!
printf '%s\n' "$OLLAMA_PID" > "$RUN_ROOT/pids/ollama.pid"
ps -p "$OLLAMA_PID" -o pid=,lstart=,command= > "$RUN_ROOT/pids/ollama.identity"
OLLAMA_READY=0
WAIT=0
while test "$WAIT" -lt 30; do
  if ollama list >/dev/null 2>&1; then
    OLLAMA_READY=1
    break
  fi
  kill -0 "$OLLAMA_PID"
  sleep 1
  WAIT=$((WAIT + 1))
done
test "$OLLAMA_READY" -eq 1
ollama list
ollama show qwen3.6:35b

python3 - "$RUN_ROOT/opencode.password" <<'PY'
import os
import secrets
import sys

path = sys.argv[1]
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
fd = os.open(path, flags, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write(secrets.token_urlsafe(32) + "\n")
PY

IFS= read -r OPENCODE_SERVER_PASSWORD < "$RUN_ROOT/opencode.password"
export OPENCODE_SERVER_PASSWORD
opencode serve --hostname 127.0.0.1 --port "$OPENCODE_PORT" \
  >"$RUN_ROOT/logs/opencode-api.log" 2>&1 &
OPENCODE_PID=$!
printf '%s\n' "$OPENCODE_PID" > "$RUN_ROOT/pids/opencode.pid"
ps -p "$OPENCODE_PID" -o pid=,lstart=,command= > "$RUN_ROOT/pids/opencode.identity"
OPENCODE_READY=0
WAIT=0
while test "$WAIT" -lt 30; do
  if opencode --server "$OPENCODE_API_URL" api GET /api/info \
      > "$RUN_ROOT/api-info.json" 2>/dev/null; then
    OPENCODE_READY=1
    break
  fi
  kill -0 "$OPENCODE_PID"
  sleep 1
  WAIT=$((WAIT + 1))
done
unset OPENCODE_SERVER_PASSWORD
test "$OPENCODE_READY" -eq 1
```

The OpenCode server password is generated into a mode-0600 file and is never placed in an argument, URL, task, receipt, or log.
The TUI source receives the password in its process environment, but tool-level visibility has not been established and must not be inferred from that observation alone.
Do not use `opencode service start`, `opencode service stop`, `ollama stop`, `pkill`, or `killall` for this lifecycle.

Create the connection manifest using the exact task ID supplied by the task owner.
For a Qwen-only top-level task, use the same exact ID in the owner brief and manifest; for a child, use the active parent's exact task ID.
Do not invent or reuse an unrelated parent task ID.

```sh
PARENT_TASK_ID="EXACT_OWNER_TASK_ID"
export OPENCODE_API_URL PARENT_TASK_ID OPENCODE_PID RUN_ROOT
python3 - <<'PY'
import json
import os

root = os.environ["RUN_ROOT"]
value = {
    "api_url": os.environ["OPENCODE_API_URL"],
    "expected_server_pid": int(os.environ["OPENCODE_PID"]),
    "password_file": os.path.join(root, "opencode.password"),
    "parent_task_id": os.environ["PARENT_TASK_ID"],
    "metadata": {
        "provider_id": "trial-local",
        "model_id": "qwen3.6:35b",
    },
    "delivery_policy": "native_empty_composer",
}
path = os.path.join(root, "opencode-connection.json")
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
fd = os.open(path, flags, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(value, handle, indent=2)
    handle.write("\n")
PY
chmod 600 "$RUN_ROOT/opencode-connection.json"
```

Verify that the read-only API response names the exact server process before creating the manifest.
The API response contains no password.

```sh
python3 - "$RUN_ROOT/api-info.json" "$OPENCODE_PID" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    info = json.load(handle)
assert info.get("version") == "2.2.5", "unexpected OpenCode API version"
assert info.get("pid") == int(sys.argv[2]), "OpenCode API PID does not match the owned process"
print("OpenCode API version and PID match the owned process")
PY
```

## Start the Qwen session

Save a one-task brief with objective, role, exclusive paths, dependencies, acceptance command, timebox, checkpoints, stop conditions, and recovery owner under `$RUN_ROOT/tasks`.
The task brief and receipts must contain no credentials.
The owner worktree passed to `--cwd` must be the exact Git worktree root.
Check each worktree root before dispatch with `git -C "$OWNER_WORKTREE" rev-parse --show-toplevel` and compare it with `cd "$OWNER_WORKTREE" && pwd -P`.

Use the task owner's exact assigned ID and fill the brief before dispatch.

```sh
OWNER_TASK="$RUN_ROOT/tasks/qwen-orchestrator.md"
cat > "$OWNER_TASK" <<'TASK'
Task ID: REPLACE_WITH_EXACT_OWNER_TASK_ID
Objective: REPLACE_WITH_ONE_BOUNDED_OBJECTIVE
Role: Qwen orchestrator
Owner worktree: REPLACE_WITH_EXACT_WORKTREE_ROOT
Authorized child tasks and exclusive paths: REPLACE_WITH_EXACT_SCOPE
Acceptance command and expected evidence: REPLACE_WITH_EXACT_ACCEPTANCE
Timebox, checkpoints, stop conditions, and recovery owner: REPLACE_WITH_EXACT_PLAN
TASK
```

For Qwen-only orchestration, run the following command from the current owner pane in the existing task CMUX workspace.
Model Preferences creates the exact session, assigns its Pattern Buffer orchestrator context, opens a visible TUI in the same workspace, and waits for exact native startup evidence before it releases the initial task.

```sh
python3 "$MP" --config-dir "$MP_CONFIG" --state-dir "$MP_STATE" \
  dispatch --role orchestrator --cwd "$OWNER_WORKTREE" \
  --task-file "$OWNER_TASK" --pattern-buffer "$PB" \
  --opencode-manifest "$RUN_ROOT/opencode-connection.json"
```

For Codex-Qwen, select the private `Codex-Qwen` profile, run from the assigned Codex pane, and use the exact isolated child worktree and unique receipt path.
The current Model Preferences child dispatch prepares and binds Pattern Buffer to the exact returned native session before it releases the task.
Write the child task file before dispatch and replace its sample paths with the exact authorized scope.

```sh
PROFILE="Codex-Qwen"
python3 "$MP" --config-dir "$MP_CONFIG" --state-dir "$MP_STATE" \
  activate "$PROFILE" --orchestrators future-only
python3 "$MP" --config-dir "$MP_CONFIG" --state-dir "$MP_STATE" \
  resolve --role implementer
CHILD_WORKTREE="/absolute/path/to/the/assigned-qwen-child-worktree"
CHILD_TASK="$RUN_ROOT/tasks/qwen-implementer.md"
CHILD_RECEIPT="$RUN_ROOT/receipts/qwen-implementer.json"
python3 "$MP" --config-dir "$MP_CONFIG" --state-dir "$MP_STATE" \
  dispatch --role implementer --cwd "$CHILD_WORKTREE" \
  --task-file "$CHILD_TASK" --pattern-buffer "$PB" \
  --owner-worktree "$OWNER_WORKTREE" --receipt-file "$CHILD_RECEIPT" \
  --opencode-manifest "$RUN_ROOT/opencode-connection.json"
```

The dispatcher validates the effective provider, model, `max` variant settings and request body, output allowance, API identity, and exact target directory before session creation.
For a parent-linked OpenCode child in another worktree, it creates the child under the parent directory, persists the exact returned session identity, and sends one supported move request before bounded read-only location polling.
An uncertain create or move is inspected using the recorded exact session and launch state; never replay the POST or weaken the parent, worktree, or directory checks.

## Add workers without duplicating the runtime

Begin with one worker and one active Qwen inference.
Each added worker needs a distinct task brief, child worktree, immutable receipt, and exclusive path set, and each dispatch remains a single-worker command.
For a Qwen-only parent, the child command must run from that exact active Qwen orchestrator's current CMUX surface and workspace.
For a Codex-Qwen parent, the owner worktree is the exact assigned Codex worktree.
The Qwen-only parent prompt includes the exact Model Preferences script, private config and state paths, and dispatch prefix.
Use that prompt's values instead of assuming the parent's shell variables such as `$MP`, `$MP_CONFIG`, `$MP_STATE`, or `$RUN_ROOT` exist in the child TUI.
Replace the child worktree, task, receipt, and owner paths with the exact values authorized for that worker.
Copy the script, private config, and private state arguments from the command prefix included in the Model Preferences task prompt.

```sh
CHILD_WORKTREE="/absolute/path/to/worker-two-worktree"
CHILD_TASK="/absolute/path/to/private-run/tasks/worker-two.md"
CHILD_RECEIPT="/absolute/path/to/private-run/receipts/worker-two.json"
OWNER_WORKTREE="/absolute/path/to/the/exact-owner-worktree"
python3 /absolute/path/to/model_preferences.py \
  --config-dir /absolute/private/path/model-preferences/config \
  --state-dir /absolute/private/path/model-preferences/state \
  dispatch --role implementer --cwd "$CHILD_WORKTREE" \
  --task-file "$CHILD_TASK" --pattern-buffer "$MODEL_PREFERENCES_PATTERN_BUFFER" \
  --owner-worktree "$OWNER_WORKTREE" --receipt-file "$CHILD_RECEIPT" \
  --opencode-manifest "$MODEL_PREFERENCES_OPENCODE_MANIFEST"
```

Do not run another `ollama serve` or `opencode serve` for a worker.
The configured Ollama parallelism is one, so extra sessions queue work rather than prove parallel inference throughput.
Increase parallelism only after the task owner reviews measured memory and latency evidence for this exact model and host.
No worker-count recommendation is qualified by the current evidence.
Reconcile each session ID, parent ID, worktree directory, receipt, completed turn, changed paths, and acceptance result independently.

## Shut down only the owned runtime

Stop assigning work and wait until every owned session is idle and has no pending prompt, inbox item, permission, form, or human-input obligation.
Have each worker report its exact session and receipt, then close only the owned TUI surfaces using their normal UI controls.
If any session state or process identity is uncertain, preserve the run and do not signal it.

Before sending a signal, load the PID from this run's private file and compare the live process identity with the saved PID, start time, and command snapshot.
For OpenCode, also repeat the authenticated `/api/info` check and require its PID to equal the saved process PID.
Signal only a process that still matches every recorded identity check.
OpenCode 2.2.5 has no stable server-startup identity, so a PID and start-time snapshot is a bounded local check rather than a permanent identity guarantee.

```sh
read -r OPENCODE_PID < "$RUN_ROOT/pids/opencode.pid"
CURRENT="$(ps -p "$OPENCODE_PID" -o pid=,lstart=,command= 2>/dev/null || true)"
RECORDED="$(cat "$RUN_ROOT/pids/opencode.identity")"
test -n "$CURRENT" && test "$CURRENT" = "$RECORDED"

IFS= read -r OPENCODE_SERVER_PASSWORD < "$RUN_ROOT/opencode.password"
export OPENCODE_SERVER_PASSWORD
opencode --server "$OPENCODE_API_URL" api GET /api/info > "$RUN_ROOT/api-info-before-stop.json"
unset OPENCODE_SERVER_PASSWORD
python3 - "$RUN_ROOT/api-info-before-stop.json" "$OPENCODE_PID" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    info = json.load(handle)
assert info.get("pid") == int(sys.argv[2]), "OpenCode API PID changed; do not signal"
PY

CURRENT="$(ps -p "$OPENCODE_PID" -o pid=,lstart=,command= 2>/dev/null || true)"
test -n "$CURRENT" && test "$CURRENT" = "$RECORDED"
kill -INT "$OPENCODE_PID"
WAIT=0
while kill -0 "$OPENCODE_PID" 2>/dev/null && test "$WAIT" -lt 15; do
  sleep 1
  WAIT=$((WAIT + 1))
done
STATE="$(ps -p "$OPENCODE_PID" -o stat= 2>/dev/null || true)"
case "$STATE" in
  "") ;;
  Z*) wait "$OPENCODE_PID" || true ;;
  *)
  echo "OpenCode process is still present; inspect it and stop here." >&2
  exit 1
  ;;
esac
```

After OpenCode has stopped and the task owner has confirmed all Qwen activity is complete, repeat the saved identity comparison for Ollama and confirm the private listener reports no loaded model with `ollama ps`.
Then signal only the exact recorded PID with `kill -INT` and use the same bounded wait and `kill -0` check to verify that PID has exited before closing the run shell.

```sh
read -r OLLAMA_PID < "$RUN_ROOT/pids/ollama.pid"
CURRENT="$(ps -p "$OLLAMA_PID" -o pid=,lstart=,command= 2>/dev/null || true)"
RECORDED="$(cat "$RUN_ROOT/pids/ollama.identity")"
test -n "$CURRENT" && test "$CURRENT" = "$RECORDED"
OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT" ollama ps
CURRENT="$(ps -p "$OLLAMA_PID" -o pid=,lstart=,command= 2>/dev/null || true)"
test -n "$CURRENT" && test "$CURRENT" = "$RECORDED"
kill -INT "$OLLAMA_PID"
WAIT=0
while kill -0 "$OLLAMA_PID" 2>/dev/null && test "$WAIT" -lt 15; do
  sleep 1
  WAIT=$((WAIT + 1))
done
STATE="$(ps -p "$OLLAMA_PID" -o stat= 2>/dev/null || true)"
case "$STATE" in
  "") ;;
  Z*) wait "$OLLAMA_PID" || true ;;
  *)
  echo "Ollama process is still present; inspect it and stop here." >&2
  exit 1
  ;;
esac
```
If a process ignores `SIGINT` or its identity does not match, leave it running and escalate to the owner rather than sending a stronger signal.

Keep the run directory, PID snapshots, task briefs, receipts, and logs under their private owner-only permissions until the owner completes evidence reconciliation.
This runbook does not delete runtime data, model data, worktrees, receipts, or logs.

## Evidence boundary

Record the exact candidate, runtime versions, profile, provider ID, model slug, selected `max` variant, context and output limits, API PID, model store, task IDs, session IDs, worktree directories, CMUX workspace and surface IDs, receipts, acceptance command, assistance, and remaining uncertainty.
The local loopback URL and a successful read-only preflight are configuration evidence, not proof that an inference request stayed local.
Only a separately authorized native run that correlates the actual request with the owned Ollama process and exact model can report local inference acceptance.
Keep Qwen-only orchestration claims separate from Codex-supervised implementer observations.
The current source replay made no model request and is not inference evidence.

The [Pattern Buffer OpenCode guide](../apps/pattern-buffer/docs/opencode.md) records the current context, compaction, and lifecycle limits.
The [Model Preferences guide](../apps/model-preferences/README.md) records the managed dispatch contract, strict delivery behavior, and exact identity requirements.
The [OpenCode Qwen setup guide](opencode-qwen-setup.md) and [Qwen operating guide](qwen-operating-guide.md) provide the configuration and task-supervision context for this runbook.

## Pinned implementation references

[OpenCode 2.2.5 CLI command definitions](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/cli/src/commands/commands.ts) define the `serve`, `api`, and `debug` command forms used here.
[OpenCode 2.2.5 server process source](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/cli/src/server-process.ts) defines its environment-based Basic authentication behavior.
[Model Preferences OpenCode dispatch guide](../apps/model-preferences/README.md#opencode-local-qwen-dispatch) and [Pattern Buffer OpenCode guide](../apps/pattern-buffer/docs/opencode.md) define the managed session, task binding, and plugin behavior.
[Ollama 0.32.14 OpenAI compatibility guide](https://github.com/ollama/ollama/blob/v0.32.14/docs/api/openai-compatibility.mdx) documents the local OpenAI-compatible provider endpoint.
