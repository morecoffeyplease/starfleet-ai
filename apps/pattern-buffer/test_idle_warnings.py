"""Exercise idle arming through the supported CLI and real hook subprocess."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid


SCRIPT = Path(__file__).with_name("pattern_buffer.py")


class IdleWarningTests(unittest.TestCase):
    def setUp(self):
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
                    "CLAUDE_CODE_SESSION_ID": self.session, "CMUX_SURFACE_ID": str(uuid.uuid4())}
        launcher = self.home / ".local/bin/hailing-frequencies"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!" + sys.executable + "\n" + '''import json, os, sys
from pathlib import Path
p = Path(os.environ['HOME']) / 'calls.jsonl'
with p.open('a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[2] == 'cancel':
    print(json.dumps({'status': 'canceled'}))
else:
    print(json.dumps({'timer': {'id': '11111111-1111-4111-8111-111111111111', 'due_at': 1800003000}}))
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

    def prompt(self, text):
        return self.cli("hook", "--harness", "claude", payload={"hook_event_name": "UserPromptSubmit",
                        "session_id": self.session, "cwd": str(self.root), "prompt": text})

    def test_enrollment_and_real_prompt_arm_but_timer_wake_does_not(self):
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("--after-idle", self.calls()[0])
        self.assertIn("50m", self.calls()[0])
        message = self.calls()[0][self.calls()[0].index("--message") + 1]
        self.assertEqual(message.count("Refresh this as a current snapshot."), 1)
        self.assertIn("Keep current decisions and relevant rejected approaches", message)
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
        count = len(self.calls())
        self.prompt("Read the current state")
        self.assertEqual(len(self.calls()), count)

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
