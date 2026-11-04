#!/usr/bin/env bash
set -euo pipefail
umask 077

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TOOL="$APP_DIR/agent-notify"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/config/state"
chmod 700 "$TMP/config/state"
cat >"$TMP/config/env" <<'EOF'
DISCORD_WEBHOOK_URL=https://discord.test/webhook
DISCORD_BOT_TOKEN=fake-token
DISCORD_USER_ID=600000000000000001
EOF
cat >"$TMP/config/cache.json" <<'EOF'
{"channel":"800000000000000001","guild":"700000000000000001","user":"600000000000000001","tags":{"open":"1","closed":"2","urgent":"3","normal":"4","fyi":"5"}}
EOF
: >"$TMP/open.log"
: >"$TMP/thread.log"
: >"$TMP/api.log"
: >"$TMP/away.log"
: >"$TMP/gh.log"

cat >"$TMP/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
method=GET
url=""
payload=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -X) method="$2"; shift 2 ;;
    --data) payload="$2"; shift 2 ;;
    -H|-w) shift 2 ;;
    -sS) shift ;;
    *) url="$1"; shift ;;
  esac
done
printf '%s\t%s\n' "$method" "$url" >>"$CURL_LOG"
case "$method:$url" in
  POST:*thread_id=*) jq -c . <<<"$payload" >>"$THREAD_LOG"; printf '{}\n200' ;;
  POST:*wait=true*)
    jq -c . <<<"$payload" >>"$OPEN_LOG"
    count="$(wc -l <"$OPEN_LOG" | tr -d ' ')"
    printf '{"channel_id":"900000000000000%03d"}\n200' "$count"
    ;;
  GET:*'/messages?'*)
    if [ -n "${WAIT_REPLY:-}" ]; then
      jq -n --arg content "$WAIT_REPLY" '[{id:"800000000000000002",author:{bot:false,username:"Configured User"},webhook_id:null,content:$content}]'
    else
      printf '[]'
    fi
    printf '\n200'
    ;;
  GET:*'/users/@me'*) printf '{"username":"Agent Notify test"}\n200' ;;
  GET:*'/users/600000000000000001'*) printf '{"username":"Configured User"}\n200' ;;
  GET:https://discord.test/webhook) printf '{"name":"Agent Notify test","channel_id":"800000000000000001","guild_id":"700000000000000001"}\n200' ;;
  GET:*'/channels/'*) printf '{"available_tags":[{"name":"Open","id":"1"},{"name":"Closed","id":"2"},{"name":"Urgent","id":"3"},{"name":"Normal","id":"4"},{"name":"FYI","id":"5"}],"applied_tags":[]}\n200' ;;
  PATCH:*) printf '{}\n200' ;;
  *) printf '{}\n200' ;;
esac
EOF
cat >"$TMP/bin/away-mode" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$AWAY_LOG"
case "${AWAY_RESULT:-ok}" in
  fail) exit 1 ;;
  invalid) printf '{"mode":"home","workspace":"Work","window":"Agent","workspace_id":"%s","surface_id":"%s"}\n' "$CMUX_WORKSPACE_ID" "$CMUX_SURFACE_ID" ;;
  mismatch)
    workspace_id="$(printf '%s' "$CMUX_WORKSPACE_ID" | tr '[:upper:]' '[:lower:]')"
    surface_id="$(printf '%s' "$CMUX_SURFACE_ID" | tr '[:upper:]' '[:lower:]')"
    workspace_id="${workspace_id%?}f"
    printf '{"mode":"back","workspace":"Work","window":"Agent","workspace_id":"%s","surface_id":"%s"}\n' "$workspace_id" "$surface_id"
    ;;
  *)
    workspace_id="$CMUX_WORKSPACE_ID"
    surface_id="$CMUX_SURFACE_ID"
    if [ "${LOWERCASE_IDS:-0}" = 1 ]; then
      workspace_id="$(printf '%s' "$workspace_id" | tr '[:upper:]' '[:lower:]')"
      surface_id="$(printf '%s' "$surface_id" | tr '[:upper:]' '[:lower:]')"
    fi
    printf '{"mode":"%s","workspace":"Starfleet Dev","window":"Codex - agent-notify","workspace_id":"%s","surface_id":"%s"}\n' "${PRESENCE_MODE:-back}" "$workspace_id" "$surface_id"
    ;;
esac
EOF
cat >"$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$GH_LOG"
printf '%s\n' 'Restricted project information'
EOF
chmod +x "$TMP/bin/curl" "$TMP/bin/away-mode" "$TMP/bin/gh"

export PATH="$TMP/bin:$PATH"
export AGENT_NOTIFY_HOME="$TMP/config"
export AGENT_NOTIFY_DISCORD_WATCH_BIN="$TMP/bin/not-installed"
export CURL_LOG="$TMP/api.log"
export OPEN_LOG="$TMP/open.log"
export THREAD_LOG="$TMP/thread.log"
export AWAY_LOG="$TMP/away.log"
export GH_LOG="$TMP/gh.log"
export AWAY_MODE_BIN="$TMP/bin/away-mode"
export LOWERCASE_IDS=1
export CMUX_WORKSPACE_ID=AB12CD34-EF56-4789-ABCD-0123456789AB
export CMUX_SURFACE_ID=DEADBEEF-1234-4ABC-9DEF-000000000123

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
assert_eq() { [ "$1" = "$2" ] || fail "expected [$2], got [$1]"; }
assert_contains() { [[ "$1" == *"$2"* ]] || fail "expected output to contain [$2]"; }
assert_not_contains() { [[ "$1" != *"$2"* ]] || fail "unexpected sensitive text [$2]"; }
line_count() { wc -l <"$1" | tr -d ' '; }
mode_bits() { python3 -c 'import os, stat, sys; print(format(stat.S_IMODE(os.stat(sys.argv[1]).st_mode), "o"))' "$1"; }

guide_home="$TMP/uninitialized-home"
AGENT_NOTIFY_HOME="$TMP/missing-config" HOME="$guide_home" "$TOOL" guide --print >"$TMP/guide.out"
cmp "$APP_DIR/agent-notify-guide.md" "$TMP/guide.out" >/dev/null || fail 'guide output does not match the maintained guide'
[ ! -e "$guide_home" ] || fail 'guide created a home directory'
[ ! -e "$TMP/missing-config" ] || fail 'guide created a configuration directory'

doctor_output="$("$TOOL" doctor)"
assert_contains "$doctor_output" 'webhook: ok (Agent Notify test)'
assert_contains "$doctor_output" 'bot: ok (Agent Notify test)'
assert_contains "$doctor_output" 'tag open: ok'
assert_contains "$doctor_output" 'tag closed: ok'
assert_contains "$doctor_output" 'tag urgent: ok'
assert_contains "$doctor_output" 'tag normal: ok'
assert_contains "$doctor_output" 'tag fyi: ok'
assert_contains "$doctor_output" 'configured user: Configured User'
assert_not_contains "$doctor_output" 'fake-token'
assert_eq "$(mode_bits "$TMP/config/env")" 600
assert_eq "$(mode_bits "$TMP/config/cache.json")" 600

SECRET_TITLE='Confidential change approval?'
SECRET_ISSUE='Restricted project information'
SECRET_BODY='Review the confidential change and approve the requested update.'
LONG_BODY="$(printf '%2200s' '' | tr ' ' 'x')"

back_id="$("$TOOL" open --kind Agent --title "$SECRET_TITLE" --issue 842 --issue-title "$SECRET_ISSUE" --body "$LONG_BODY$SECRET_BODY" 2>"$TMP/back.err")"
assert_eq "$back_id" 900000000000000001
assert_eq "$(line_count "$OPEN_LOG")" 1
back_payload="$(sed -n '1p' "$OPEN_LOG")"
assert_not_contains "$back_payload" "$SECRET_TITLE"
assert_not_contains "$back_payload" "$SECRET_ISSUE"
assert_not_contains "$back_payload" "$SECRET_BODY"
assert_not_contains "$back_payload" "$(printf '%100s' '' | tr ' ' 'x')"
assert_not_contains "$back_payload" '#842'
assert_contains "$back_payload" 'Your attention is needed in CMUX.'
assert_contains "$back_payload" 'Please open that chat.'
assert_not_contains "$back_payload" 'full request'
assert_contains "$back_payload" 'Starfleet Dev'
assert_contains "$back_payload" 'Codex - agent-notify'
assert_contains "$(cat "$TMP/back.err")" "$SECRET_TITLE"
assert_contains "$(cat "$TMP/back.err")" "$SECRET_ISSUE"
assert_contains "$(cat "$TMP/back.err")" "$SECRET_BODY"
assert_contains "$(cat "$TMP/back.err")" 'paste the context below there'
assert_contains "$(cat "$TMP/back.err")" 'ab12cd34-ef56-4789-abcd-0123456789ab'
assert_contains "$(cat "$TMP/back.err")" 'deadbeef-1234-4abc-9def-000000000123'
assert_eq "$(jq -r '.body' "$TMP/config/state/$back_id.post.json")" "$LONG_BODY$SECRET_BODY"
assert_eq "$(line_count "$GH_LOG")" 0
assert_eq "$(mode_bits "$TMP/config/state")" 700
assert_eq "$(mode_bits "$TMP/config/state/$back_id.post.json")" 600
assert_eq "$(jq -r '.delivery' "$TMP/config/state/$back_id.post.json")" attention
assert_eq "$(jq -r '.location.workspace_id' "$TMP/config/state/$back_id.post.json")" 'ab12cd34-ef56-4789-abcd-0123456789ab'
assert_eq "$(jq -r '.location.surface_id' "$TMP/config/state/$back_id.post.json")" 'deadbeef-1234-4abc-9def-000000000123'

titleless_id="$("$TOOL" open --kind Agent --title "$SECRET_TITLE" --issue 842 --body "$SECRET_BODY" 2>"$TMP/titleless.err")"
assert_eq "$titleless_id" 900000000000000002
assert_eq "$(line_count "$GH_LOG")" 0
assert_not_contains "$(sed -n '2p' "$OPEN_LOG")" "$SECRET_ISSUE"

export WAIT_REPLY='Please continue in this CMUX conversation.'
wait_reply="$("$TOOL" wait "$back_id" --timeout 1)"
assert_contains "$wait_reply" '[discord:'
assert_contains "$wait_reply" "$WAIT_REPLY"
unset WAIT_REPLY

export PRESENCE_MODE=away
reply_err="$TMP/back-reply.err"
"$TOOL" reply "$back_id" --body 'The operator has returned.' 2>"$reply_err"
assert_eq "$(line_count "$THREAD_LOG")" 0
assert_contains "$(cat "$reply_err")" 'do not send this reply to Discord'
assert_contains "$(cat "$reply_err")" 'The operator has returned.'
"$TOOL" close "$back_id" --body 'Resolved in the owning CMUX chat.' 2>"$TMP/close.err"
assert_eq "$(line_count "$THREAD_LOG")" 0
[ ! -f "$TMP/config/state/$back_id.post.json" ] || fail 'closed attention metadata was not removed'

away_id="$("$TOOL" open --kind Agent --title "$SECRET_TITLE" --issue 842 --body "$SECRET_BODY" 2>"$TMP/away.err")"
assert_eq "$away_id" 900000000000000003
assert_eq "$(line_count "$OPEN_LOG")" 3
away_payload="$(sed -n '3p' "$OPEN_LOG")"
assert_contains "$away_payload" "$SECRET_TITLE"
assert_contains "$away_payload" "$SECRET_ISSUE"
assert_contains "$away_payload" "$SECRET_BODY"
assert_contains "$away_payload" '#842'
assert_eq "$(line_count "$GH_LOG")" 1

export PRESENCE_MODE=back
"$TOOL" reply "$away_id" --body 'Keep this in its original Discord thread.'
assert_eq "$(line_count "$THREAD_LOG")" 1
assert_contains "$(sed -n '1p' "$THREAD_LOG")" 'Keep this in its original Discord thread.'

away_calls_before="$(line_count "$AWAY_LOG")"
explicit_id="$("$TOOL" open --delivery discord --kind Agent --title "$SECRET_TITLE" --body "$SECRET_BODY" 2>"$TMP/explicit.err")"
assert_eq "$explicit_id" 900000000000000004
assert_eq "$(line_count "$AWAY_LOG")" "$away_calls_before"
assert_contains "$(sed -n '4p' "$OPEN_LOG")" "$SECRET_BODY"

export AWAY_RESULT=fail
fallback_id="$("$TOOL" open --kind Agent --title "$SECRET_TITLE" --body "$SECRET_BODY" 2>"$TMP/fallback.err")"
assert_eq "$fallback_id" 900000000000000005
assert_contains "$(cat "$TMP/fallback.err")" 'presence context unavailable'
assert_contains "$(sed -n '5p' "$OPEN_LOG")" "$SECRET_BODY"
assert_eq "$(jq -r '.delivery' "$TMP/config/state/$fallback_id.post.json")" discord

export AWAY_RESULT=mismatch
mismatch_id="$("$TOOL" open --kind Agent --title "$SECRET_TITLE" --body "$SECRET_BODY" 2>"$TMP/mismatch.err")"
assert_eq "$mismatch_id" 900000000000000006
assert_contains "$(cat "$TMP/mismatch.err")" 'incomplete or mismatched CMUX context'
assert_contains "$(sed -n '6p' "$OPEN_LOG")" "$SECRET_BODY"

export AWAY_RESULT=ok
unset CMUX_SURFACE_ID
missing_identity_id="$("$TOOL" open --kind Agent --title "$SECRET_TITLE" --body "$SECRET_BODY" 2>"$TMP/missing-identity.err")"
assert_eq "$missing_identity_id" 900000000000000007
assert_contains "$(cat "$TMP/missing-identity.err")" 'CMUX workspace or surface identity is missing'
assert_contains "$(sed -n '7p' "$OPEN_LOG")" "$SECRET_BODY"

"$TOOL" reply 900000000000000999 --body 'Legacy threads remain full Discord conversations.'
assert_eq "$(line_count "$THREAD_LOG")" 2
assert_contains "$(sed -n '2p' "$THREAD_LOG")" 'Legacy threads remain full Discord conversations.'

# A managed thread remains deliverable without a legacy per-thread listener.
cat >"$TMP/bin/discord-watch" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$MANAGED_LOG"
case "$1" in
  register) [ "${REGISTER_FAIL:-0}" = 0 ] ;;
  managed) [ "${MANAGED_ENABLED:-1}" = 1 ] ;;
  receive)
    if [ "${MANAGED_NO_REPLY:-0}" = 1 ]; then exit 3; fi
    printf '[discord:%s:800000000000000003] Configured User: managed reply\n' "$3"
    ;;
  close) exit 0 ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$TMP/bin/discord-watch"
export AGENT_NOTIFY_DISCORD_WATCH_BIN="$TMP/bin/discord-watch"
export MANAGED_LOG="$TMP/managed.log"
: >"$MANAGED_LOG"
export CMUX_SURFACE_ID=DEADBEEF-1234-4ABC-9DEF-000000000123
managed_id="$("$TOOL" open --delivery discord --kind Agent --title 'Managed delivery' --body 'Canary' 2>"$TMP/managed-open.err")"
assert_contains "$(cat "$MANAGED_LOG")" "register $managed_id"
"$TOOL" listen "$managed_id" 2>"$TMP/managed-listen.err"
assert_contains "$(cat "$TMP/managed-listen.err")" 'no listener tab is needed'
"$TOOL" watch "$managed_id" 2>"$TMP/managed-watch.err"
assert_contains "$(cat "$TMP/managed-watch.err")" 'per-thread watch is unnecessary'
api_before="$(line_count "$CURL_LOG")"
assert_contains "$("$TOOL" wait "$managed_id" --timeout 0)" 'managed reply'
assert_eq "$(line_count "$CURL_LOG")" "$api_before"
export MANAGED_NO_REPLY=1
if "$TOOL" wait "$managed_id" --timeout 0; then fail 'empty managed inbox returned success'; else assert_eq "$?" 3; fi
unset MANAGED_NO_REPLY
"$TOOL" close "$managed_id" <<'EOF'
Pilot resolved through stdin.
EOF
assert_contains "$(cat "$MANAGED_LOG")" "close $managed_id"
assert_contains "$(tail -1 "$THREAD_LOG")" 'Pilot resolved through stdin.'
export MANAGED_ENABLED=0
"$TOOL" close 900000000000000999 <<'EOF'
Resolved legacy conversation.
EOF
if rg -q '^close 900000000000000999$' "$MANAGED_LOG"; then fail 'unmanaged thread attempted local ownership cleanup'; fi
unset MANAGED_ENABLED
export REGISTER_FAIL=1
failed_registration_id="$("$TOOL" open --delivery discord --kind Agent --title 'Registration failed' --body 'Keep ask visible' 2>"$TMP/managed-registration.err")"
assert_contains "$(cat "$TMP/managed-registration.err")" 'registration failed'
[ -n "$failed_registration_id" ] || fail 'registration failure hid the created thread id'

unset REGISTER_FAIL
export CMUX_AGENT_LAUNCH_KIND=opencode
export OPENCODE_SESSION_ID=ses_AgentNotifyFixture42
export CODEX_THREAD_ID=stale-codex-thread-id
export AGENT_NOTIFY_KIND=Codex
opencode_id="$("$TOOL" open --delivery discord --title 'OpenCode managed owner' --body 'Fixture only' 2>"$TMP/opencode-open.err")"
assert_eq "$opencode_id" 900000000000000010
assert_contains "$(sed -n '10p' "$OPEN_LOG")" '[OpenCode]'
assert_contains "$(cat "$MANAGED_LOG")" "register $opencode_id --session $OPENCODE_SESSION_ID"
"$TOOL" watch "$opencode_id" 2>"$TMP/opencode-watch.err"
assert_contains "$(cat "$MANAGED_LOG")" "managed $opencode_id --session $OPENCODE_SESSION_ID"
assert_not_contains "$(cat "$TMP/opencode-watch.err")" 'Codex'
export MANAGED_ENABLED=0
if "$TOOL" listen "$opencode_id" 2>"$TMP/opencode-listen.err"; then fail 'OpenCode listen used a Codex queue'; else assert_eq "$?" 4; fi
assert_contains "$(cat "$TMP/opencode-listen.err")" 'only available to an exact Codex session'
if "$TOOL" watch "$opencode_id" --queue "$CODEX_THREAD_ID" 2>"$TMP/opencode-queue.err"; then
  fail 'OpenCode watch accepted an inherited Codex queue'
else
  assert_eq "$?" 1
fi
assert_contains "$(cat "$TMP/opencode-queue.err")" 'cannot use an OpenCode session'
export MANAGED_ENABLED=1
unset AGENT_NOTIFY_KIND

unset CMUX_SURFACE_ID
unscoped_opencode_id="$("$TOOL" open --delivery discord --title 'OpenCode unscoped' --body 'Fixture only' 2>"$TMP/opencode-unscoped.err")"
assert_eq "$unscoped_opencode_id" 900000000000000011
assert_contains "$(cat "$TMP/opencode-unscoped.err")" 'OpenCode managed-thread registration requires an exact ses_ identity and CMUX workspace/surface scope'
if rg -q "register $unscoped_opencode_id" "$MANAGED_LOG"; then fail 'unscoped OpenCode owner attempted managed registration'; fi

printf '%s\n' 'agent-notify routing tests passed'
