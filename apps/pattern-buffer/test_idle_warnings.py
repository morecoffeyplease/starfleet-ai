"""Exercise idle arming through the supported CLI and real hook subprocess."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from _test_support import isolate_state_home


SCRIPT = Path(__file__).with_name("pattern_buffer.py")


class IdleWarningTests(unittest.TestCase):
    def setUp(self):
        isolate_state_home(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / "repo"
        self.root.mkdir()
        for args in [("init", "-q"), ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                      "commit", "--allow-empty", "-qm", "Fixture")]:
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        self.session = str(uuid.uuid4())
        self.env = {**os.environ, "HOME": str(self.home), "CMUX_AGENT_LAUNCH_KIND": "claude",
                    "CLAUDE_CODE_SESSION_ID": self.session, "CMUX_SURFACE_ID": str(uuid.uuid4()),
                    "PB_TEST_WORKTREE": str(self.root.resolve())}
        launcher = self.home / ".local/bin/hailing-frequencies"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!" + sys.executable + "\n" + '''import json, os, sys
from pathlib import Path
root = Path(os.environ['HOME'])
args = sys.argv[1:]
calls_path = root / 'calls.jsonl'
calls = [json.loads(line) for line in calls_path.read_text().splitlines()] if calls_path.exists() else []
calls.append(args)
with calls_path.open('a') as f: f.write(json.dumps(args) + '\\n')
state_path = root / 'timers.json'
timers = json.loads(state_path.read_text()) if state_path.exists() else []
def finish(code, stdout='', stderr=''):
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    raise SystemExit(code)
if args[1] == 'cancel':
    timer = next((item for item in timers if item['id'] == args[2]), None)
    if timer and timer['status'] == 'active':
        timer['status'] = 'canceled'
    state_path.write_text(json.dumps(timers))
    finish(0, json.dumps({'status': timer['status'] if timer else 'expired'}))
if args[1] == 'list':
    if os.environ.get('PB_FAKE_LIST_MODE') == 'malformed':
        finish(0, '{')
    finish(0, json.dumps({'timers': timers}))
if args[1] != 'set':
    finish(2, '', 'unsupported synthetic timer command')
set_calls = [call for call in calls if call[1] == 'set']
mode = os.environ.get('PB_FAKE_SECOND_SET', '')
if len(set_calls) == 2 and mode == 'refused':
    finish(1, json.dumps({'error': 'synthetic refusal'}))
request_id = args[args.index('--request-id') + 1]
replace_key = args[args.index('--replace-key') + 1]
if len(set_calls) == 2 and mode == 'accepted-malformed':
    for timer in timers:
        if timer['replacement_key'] == replace_key and timer['status'] == 'active':
            timer['status'] = 'canceled'
    timers.append({'id': '22222222-2222-4222-8222-222222222222', 'request_id': request_id,
                   'replacement_key': replace_key, 'canonical_worktree': os.environ['PB_TEST_WORKTREE'],
                   'status': 'active'})
    state_path.write_text(json.dumps(timers))
    finish(0, '{')
timer_id = ('11111111-1111-4111-8111-111111111111' if len(set_calls) == 1
            else '33333333-3333-4333-8333-333333333333')
for timer in timers:
    if timer['replacement_key'] == replace_key and timer['status'] == 'active':
        timer['status'] = 'canceled'
timers.append({'id': timer_id, 'request_id': request_id, 'replacement_key': replace_key,
               'canonical_worktree': os.environ['PB_TEST_WORKTREE'], 'status': 'active'})
state_path.write_text(json.dumps(timers))
finish(0, json.dumps({'timer': {'id': timer_id, 'due_at': 1800003000}}))
''')
        launcher.chmod(0o700)
        self.cli("init", "--default-role", "orchestrator")
        self.cli("checkpoint-init", "--harness", "claude", "--session", self.session, "--compact-at", "60")

    def cli(self, *args, payload=None):
        result = subprocess.run([sys.executable, str(SCRIPT), "--cwd", str(self.root), *args],
                                env=self.env, capture_output=True, text=True,
                                input=json.dumps(payload) if payload is not None else None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def calls(self):
        return [json.loads(line) for line in (self.home / "calls.jsonl").read_text().splitlines()]

    def active_timers(self):
        state_path = self.home / "timers.json"
        if not state_path.exists():
            return []
        return [timer for timer in json.loads(state_path.read_text()) if timer["status"] == "active"]

    def prompt(self, text):
        return self.cli("hook", "--harness", "claude", payload={"hook_event_name": "UserPromptSubmit",
                        "session_id": self.session, "cwd": str(self.root), "prompt": text})

    def test_enrollment_and_real_prompt_arm_but_timer_wake_does_not(self):
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("--after-idle", self.calls()[0])
        self.assertIn("50m", self.calls()[0])
        message = self.calls()[0][self.calls()[0].index("--message") + 1]
        self.assertEqual(message.count("Refresh this as a current snapshot."), 1)
        self.assertIn("Keep the next action and active work, current decisions and relevant rejected approaches", message)
        self.assertIn("Link useful history instead of repeating it.", message)
        self.prompt("Continue the authorized work")
        self.assertEqual(len(self.calls()), 2)
        self.prompt("Temporal Beacon timer 11111111-1111-4111-8111-111111111111 is due. Retrieve it.")
        self.assertEqual(len(self.calls()), 2)
        # Actual native Haiku input wraps the pasted wake, including an id on the closing tag.
        self.prompt('\n\n<pasted_content id="bba4">\nTemporal Beacon timer '
                    '11111111-1111-4111-8111-111111111111 is due. Retrieve it.\n'
                    '</pasted_content id="bba4">\n')
        self.assertEqual(len(self.calls()), 2)
        self.prompt('<pasted_content>Temporal Beacon timer '
                    '11111111-1111-4111-8111-111111111111 is due. Retrieve it.</pasted_content>')
        self.assertEqual(len(self.calls()), 2)
        self.prompt('<pasted_content id="new">Continue the authorized work.</pasted_content id="new">')
        self.assertEqual(len(self.calls()), 3)
        report = json.loads(self.cli("checkpoint-status", "--harness", "claude", "--session", self.session).stdout)
        self.assertEqual(report["idle_timer"]["status"], "armed")

    def test_disable_cancels_owned_timer_and_later_prompt_does_not_rearm(self):
        self.cli("checkpoint-config", "--harness", "claude", "--session", self.session,
                 "--idle-warning-minutes", "0")
        self.assertEqual(self.calls()[-1][1], "cancel")
        self.assertEqual(self.active_timers(), [])
        count = len(self.calls())
        self.prompt("Read the current state")
        self.assertEqual(len(self.calls()), count)

    def test_refused_rearm_preserves_known_timer_for_disable(self):
        self.env["PB_FAKE_SECOND_SET"] = "refused"
        result = self.cli("checkpoint-config", "--harness", "claude", "--session", self.session,
                          "--idle-warning-minutes", "60")
        self.assertEqual(json.loads(result.stdout)["idle_timer"]["status"], "unavailable")
        report = json.loads(result.stdout)
        self.assertEqual(report["idle_timer"]["timer_id"], "11111111-1111-4111-8111-111111111111")
        self.assertTrue(report["idle_timer"]["pending_request_id"])
        self.assertEqual([timer["id"] for timer in self.active_timers()],
                         ["11111111-1111-4111-8111-111111111111"])
        self.cli("checkpoint-config", "--harness", "claude", "--session", self.session,
                 "--idle-warning-minutes", "0")
        self.assertEqual(self.active_timers(), [])

    def test_legacy_failed_arm_record_reconciles_without_timer_id(self):
        state_path = next((self.root / ".git" / "context-injector" / "checkpoints").glob("*/state.json"))
        state = json.loads(state_path.read_text())
        state["idle_timer"] = {"status": "unavailable", "request_id": "legacy-failed-arm-request",
                               "detail": "Timer creation was refused"}
        state_path.write_text(json.dumps(state))
        result = self.cli("checkpoint-config", "--harness", "claude", "--session", self.session,
                          "--idle-warning-minutes", "0")
        report = json.loads(result.stdout)
        self.assertEqual(report["idle_timer"]["status"], "canceled")
        self.assertEqual(report["idle_timer"]["timer_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(report["idle_timer"]["reconciled_request_id"], "legacy-failed-arm-request")
        self.assertEqual(self.active_timers(), [])

    def test_malformed_rearm_response_reconciles_committed_replacement_on_disable(self):
        self.env["PB_FAKE_SECOND_SET"] = "accepted-malformed"
        result = self.cli("checkpoint-config", "--harness", "claude", "--session", self.session,
                          "--idle-warning-minutes", "60")
        self.assertEqual(json.loads(result.stdout)["idle_timer"]["status"], "unavailable")
        report = json.loads(result.stdout)
        self.assertEqual(report["idle_timer"]["timer_id"], "11111111-1111-4111-8111-111111111111")
        self.assertTrue(report["idle_timer"]["pending_request_id"])
        self.assertEqual([timer["id"] for timer in self.active_timers()],
                         ["22222222-2222-4222-8222-222222222222"])
        self.cli("checkpoint-config", "--harness", "claude", "--session", self.session,
                 "--idle-warning-minutes", "0")
        self.assertEqual(self.active_timers(), [])

    def test_unconfirmed_list_keeps_timer_evidence_and_does_not_claim_cleanup(self):
        self.env["PB_FAKE_SECOND_SET"] = "refused"
        self.cli("checkpoint-config", "--harness", "claude", "--session", self.session,
                 "--idle-warning-minutes", "60")
        self.env["PB_FAKE_LIST_MODE"] = "malformed"
        result = self.cli("checkpoint-config", "--harness", "claude", "--session", self.session,
                          "--idle-warning-minutes", "0")
        report = json.loads(result.stdout)
        self.assertEqual(report["idle_timer"]["status"], "cancel_unconfirmed")
        self.assertEqual(report["idle_timer"]["timer_id"], "11111111-1111-4111-8111-111111111111")
        self.assertTrue(report["idle_timer"]["pending_request_id"])
        self.assertTrue(any("cancellation is unconfirmed" in warning for warning in report["warnings"]))
        self.assertEqual([timer["id"] for timer in self.active_timers()],
                         ["11111111-1111-4111-8111-111111111111"])

    def test_reconciliation_cancels_only_this_worktree_idle_timer(self):
        self.env["PB_FAKE_SECOND_SET"] = "refused"
        self.cli("checkpoint-config", "--harness", "claude", "--session", self.session,
                 "--idle-warning-minutes", "60")
        state_path = self.home / "timers.json"
        timers = json.loads(state_path.read_text())
        timers.extend([
            {"id": "44444444-4444-4444-8444-444444444444", "request_id": "foreign-worktree",
             "replacement_key": "pattern-buffer-checkpoint-idle", "canonical_worktree": "/other/repo",
             "status": "active"},
            {"id": "55555555-5555-4555-8555-555555555555", "request_id": "other-purpose",
             "replacement_key": "another-purpose", "canonical_worktree": str(self.root),
             "status": "active"},
        ])
        state_path.write_text(json.dumps(timers))
        self.cli("checkpoint-config", "--harness", "claude", "--session", self.session,
                 "--idle-warning-minutes", "0")
        self.assertEqual(sorted(timer["id"] for timer in self.active_timers()), [
            "44444444-4444-4444-8444-444444444444",
            "55555555-5555-4555-8555-555555555555",
        ])

    def test_ordinary_pressure_reminder_disable_cancels_idle_timer_too(self):
        self.cli("checkpoint-config", "--harness", "claude", "--session", self.session, "--reminders", "off")
        self.assertEqual(self.calls()[-1][1], "cancel")

    def test_other_inherited_session_cannot_arm_a_timer(self):
        count = len(self.calls())
        self.env["CLAUDE_CODE_SESSION_ID"] = str(uuid.uuid4())
        self.prompt("A foreign session must not arm this timer")
        self.assertEqual(len(self.calls()), count)


if __name__ == "__main__":
    unittest.main()
