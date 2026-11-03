import copy
import hashlib
import io
import json
from contextlib import closing, redirect_stdout
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hailing_frequencies as hf
import install


def uid():
    return str(uuid.uuid4()).upper()


def target(label="lane"):
    return dict(surface=uid(), workspace=uid(), session=uid(), harness="claude", label=label,
                started_at=1, cutoff=1)


def message(recipient, at=100, **changes):
    row = dict(id=uid(), recipient_surface_id=recipient["surface"], recipient_workspace_id=recipient["workspace"],
               sender_surface_id=uid(), sender_workspace_id=uid(), sender_name="peer", created_at=at,
               state="queued", read_at=None, in_reply_to=None, body="message body")
    row.update(changes)
    return row


def compact_message(row):
    return {key: row[key] for key in (
        "id", "sender_name", "sender_surface_id", "sender_workspace_id", "created_at", "in_reply_to", "body"
    )}


def reply(source, recipient, at=200, **changes):
    row = dict(id=uid(), recipient_surface_id=source["sender_surface_id"],
               recipient_workspace_id=source["sender_workspace_id"], sender_surface_id=recipient["surface"],
               sender_workspace_id=recipient["workspace"], sender_name="peer", created_at=at,
               state="delivered", read_at=None, in_reply_to=source["id"])
    row.update(changes)
    return row


class Host:
    def __init__(self):
        self.executable = "cmux"
        self.rows = []
        self.sent = []
        self.hold = None
        self.failure = None

    def messages(self, _target):
        return copy.deepcopy(self.rows)

    def binding(self, _target):
        if self.hold:
            raise ValueError(self.hold)

    def submit(self, destination, text):
        self.sent.append((destination, text))
        if self.failure:
            raise self.failure
        return {"submitted": True}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.host = Host()
        self.now = 500
        self.app = hf.Monitor(self.directory, self.host, clock=lambda: self.now)
        self.lane = target()
        self.config = dict(version=1, targets=[self.lane], coordinator=None, remind_after=300,
                           escalate_after=900, cooldown=600, interval=30)
        self.save()

    def save(self):
        hf.atomic_json(self.directory / "config.json", self.config)

    def test_observe_does_not_submit_or_consume_intent(self):
        self.host.rows = [message(self.lane)]
        self.assertEqual(self.app.scan()["targets"][0]["attempts"][0]["outcome"], "would_submit")
        self.assertEqual(self.host.sent, [])
        self.app.scan(send=True)
        self.assertEqual(len(self.host.sent), 1)
        prompt = self.host.sent[0][1]
        self.assertIn("CMUX still records these messages as unread", prompt)
        self.assertIn("not task completion or authority", prompt)
        self.assertIn(self.app.inbox_command(self.lane, [self.host.rows[0]["id"]]), prompt)
        self.assertEqual(prompt.count(self.host.rows[0]["id"]), 1)
        self.assertEqual(len(prompt.splitlines()), 2)

    def test_prompt_batches_reader_commands_without_repeating_message_ids(self):
        self.host.rows = [message(self.lane) for _ in range(21)]
        self.app.scan(send=True)
        prompt = self.host.sent[0][1]
        lines = prompt.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1].count("--message"), 20)
        self.assertEqual(lines[2].count("--message"), 1)
        for source in self.host.rows:
            self.assertEqual(prompt.count(source["id"]), 1)

    def test_canary_scope_keeps_other_enrolled_sessions_observe_only(self):
        other = target("other")
        self.config["targets"].append(other)
        self.config["coordinator"] = other["session"]
        self.save()
        self.now = 1100
        self.host.rows = [message(self.lane), message(other)]
        report = self.app.scan(send=True, only_sessions=[self.lane["session"].lower()])
        self.assertEqual(len(self.host.sent), 1)
        self.assertEqual(self.host.sent[0][0]["session"], self.lane["session"])
        self.assertTrue(report["targets"][0]["sending_enabled"])
        self.assertFalse(report["targets"][1]["sending_enabled"])
        self.assertIn("outside", report["targets"][0]["escalation_hold"])
        self.app.scan(send=True)
        self.assertEqual(len(self.host.sent), 3)  # Previously observed messages still eligible.

    def test_unknown_canary_scope_refuses_all_sends(self):
        self.host.rows = [message(self.lane)]
        with self.assertRaisesRegex(ValueError, "not enrolled"):
            self.app.scan(send=True, only_sessions=[uid()])
        self.assertFalse(self.host.sent)

    def test_restart_never_replays_a_submitted_batch(self):
        self.host.rows = [message(self.lane)]
        self.app.scan(send=True)
        self.now += 1000
        restarted = hf.Monitor(self.directory, self.host, clock=lambda: self.now)
        restarted.scan(send=True)
        self.assertEqual(len(self.host.sent), 1)

    def test_uncertain_timeout_is_not_retried(self):
        self.host.rows = [message(self.lane)]
        self.host.failure = subprocess.TimeoutExpired("cmux", 15)
        report = self.app.scan(send=True)
        self.assertEqual(report["targets"][0]["attempts"][0]["outcome"], "uncertain")
        self.now += 1000
        self.app.scan(send=True)
        self.assertEqual(len(self.host.sent), 1)

    def test_intent_is_durable_before_transport(self):
        self.host.rows = [message(self.lane)]

        def crash(*_args):
            raise KeyboardInterrupt()

        self.host.submit = crash
        with self.assertRaises(KeyboardInterrupt):
            self.app.scan(send=True)
        self.host.submit = lambda *args: self.fail("Crash intent must suppress replay")
        self.now += 1000
        self.app.scan(send=True)

    def test_source_read_resolves_backlog_without_marking_anything(self):
        self.host.rows = [message(self.lane)]
        self.host.rows[0]["read_at"] = 450
        result = self.app.scan(send=True)["targets"][0]
        self.assertEqual(result["native_unread"], 0)
        self.assertEqual(result["overdue"], 0)
        self.assertFalse(self.host.sent)

    def test_delivered_unread_also_gets_a_reminder(self):
        self.host.rows = [message(self.lane)]
        self.host.rows[0]["state"] = "delivered"
        self.app.scan(send=True)
        self.assertEqual(len(self.host.sent), 1)

    def test_current_identity_and_human_holds_prevent_send(self):
        self.host.rows = [message(self.lane)]
        for reason in ("Human draft", "Session replaced", "Unknown input state", "Permission dialog"):
            self.host.hold = reason
            self.assertEqual(self.app.scan(send=True)["targets"][0]["hold"], reason)
        self.assertFalse(self.host.sent)
        self.host.hold = None
        self.app.scan(send=True)
        self.assertEqual(len(self.host.sent), 1)

    def test_new_messages_wait_out_cooldown(self):
        self.host.rows = [message(self.lane)]
        self.app.scan(send=True)
        self.host.rows.append(message(self.lane))
        self.app.scan(send=True)
        self.assertEqual(len(self.host.sent), 1)
        self.now += 601
        self.app.scan(send=True)
        self.assertEqual(len(self.host.sent), 2)

    def test_escalation_goes_to_exact_distinct_enrolled_coordinator_once(self):
        coordinator = target("coordinator")
        self.config["targets"].append(coordinator)
        self.config["coordinator"] = coordinator["session"]
        self.save()
        self.now = 1100
        self.host.rows = [message(self.lane)]
        self.app.scan(send=True)
        self.assertEqual([x[0]["session"] for x in self.host.sent], [self.lane["session"], coordinator["session"]])
        escalation = self.host.sent[1][1]
        self.assertIn("Investigate source metadata and current binding", escalation)
        self.assertIn("Do not read or consume messages as the recipient", escalation)
        self.assertEqual(escalation.count(self.host.rows[0]["id"]), 1)
        self.assertNotIn("inbox --session", escalation)
        self.assertNotIn("ack --session", escalation)
        self.now += 1000
        self.app.scan(send=True)
        self.assertEqual(len(self.host.sent), 2)

    def test_unrelated_outbound_old_and_monitor_messages_are_not_nudged(self):
        other = target()
        self.lane["cutoff"] = 90
        self.save()
        self.host.rows = [message(other), message(self.lane, at=80), message(self.lane, at=0), message(self.lane)]
        self.host.rows[-1]["sender_name"] = "hailing-frequencies"
        self.assertEqual(self.app.scan(send=True)["targets"][0]["overdue"], 0)
        self.assertFalse(self.host.sent)

    def test_saturation_fails_closed(self):
        self.host.rows = [message(self.lane)] * hf.SCAN_LIMIT
        with self.assertRaisesRegex(ValueError, "saturated"):
            self.app.scan(send=True)
        self.assertFalse(self.host.sent)

    def test_young_messages_are_not_overdue(self):
        self.host.rows = [message(self.lane, at=499)]
        self.assertEqual(self.app.scan(send=True)["targets"][0]["overdue"], 0)

    def test_read_race_between_planning_and_transport_sends_nothing(self):
        self.host.rows = [message(self.lane)]
        calls = 0
        original = self.host.messages

        def messages(target):
            nonlocal calls
            calls += 1
            return original(target) if calls == 1 else []

        self.host.messages = messages
        report = self.app.scan(send=True)
        self.assertEqual(report["targets"][0]["attempts"][0]["outcome"], "already_read")
        self.assertFalse(self.host.sent)

    def test_reply_race_before_submit_records_ack_and_sends_nothing(self):
        source = message(self.lane)
        answer = reply(source, self.lane)
        calls = 0

        def messages(_target):
            nonlocal calls
            calls += 1
            return copy.deepcopy([source] if calls == 1 else [source, answer])

        self.host.messages = messages
        result = self.app.scan(send=True)["targets"][0]
        self.assertEqual(result["attempts"][0]["outcome"], "already_acknowledged")
        self.assertEqual(result["native_unread"], 1)
        self.assertEqual(result["acknowledged"], 1)
        self.assertEqual(result["overdue"], 0)
        self.assertFalse(self.host.sent)

    def test_explicit_ack_is_recipient_owned_repeatable_and_persistent(self):
        source = message(self.lane)
        self.host.rows = [source]
        first = self.app.acknowledge(self.lane["session"], source["id"], self.lane["surface"])
        second = self.app.acknowledge(self.lane["session"].lower(), source["id"].lower(), self.lane["surface"].lower())
        self.assertEqual(first["evidence"], "explicit")
        self.assertEqual(second["message"], source["id"])
        report = self.app.scan(send=True)["targets"][0]
        self.assertEqual((report["native_unread"], report["acknowledged"], report["overdue"]), (1, 1, 0))
        restarted = hf.Monitor(self.directory, self.host, clock=lambda: self.now)
        report = restarted.scan(send=True)["targets"][0]
        self.assertEqual((report["native_unread"], report["acknowledged"], report["overdue"]), (1, 1, 0))
        self.assertIsNone(self.host.rows[0]["read_at"])
        self.assertEqual(self.host.rows[0]["state"], "queued")
        self.assertFalse(self.host.sent)

    def test_inbox_outputs_and_acknowledges_only_exact_selected_messages(self):
        selected = message(self.lane)
        unrelated = message(self.lane)
        self.host.rows = [selected, unrelated]
        output = io.StringIO()
        result = self.app.consume_inbox(
            self.lane["session"], [selected["id"]], self.lane["surface"], output
        )
        self.assertEqual(json.loads(output.getvalue()), {"messages": [compact_message(selected)]})
        self.assertEqual(result["evidence"], "inbox")
        report = self.app.scan()["targets"][0]
        self.assertEqual((report["native_unread"], report["acknowledged"], report["overdue"]), (2, 1, 1))
        self.assertIsNone(selected["read_at"])
        self.assertEqual(selected["state"], "queued")
        with closing(self.app.database()) as db:
            saved = db.execute("SELECT message,evidence FROM acknowledgements").fetchall()
            columns = {row[1] for row in db.execute("PRAGMA table_info(acknowledgements)")}
        self.assertEqual(saved, [(selected["id"], "inbox")])
        self.assertNotIn("body", columns)
        self.assertFalse(self.host.sent)

    def test_inbox_validates_all_recipients_and_generations_before_output(self):
        other = target("coordinator")
        self.config["targets"].append(other)
        self.config["coordinator"] = other["session"]
        self.save()
        selected = message(self.lane)
        foreign = message(other)
        old = message(self.lane, at=0)
        self.host.rows = [selected, foreign, old]
        for message_ids in ([selected["id"], foreign["id"]], [selected["id"], old["id"]]):
            output = io.StringIO()
            with self.assertRaisesRegex(ValueError, "missing, ambiguous, or outside"):
                self.app.consume_inbox(self.lane["session"], list(message_ids), self.lane["surface"], output)
            self.assertEqual(output.getvalue(), "")
        coordinator_output = io.StringIO()
        with self.assertRaisesRegex(ValueError, "missing, ambiguous, or outside"):
            self.app.consume_inbox(other["session"], [selected["id"]], other["surface"], coordinator_output)
        self.assertEqual(coordinator_output.getvalue(), "")
        with closing(self.app.database()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM acknowledgements").fetchone()[0], 0)

    def test_inbox_broken_stdout_flush_does_not_acknowledge(self):
        source = message(self.lane)
        self.host.rows = [source]

        class BrokenOutput:
            def write(self, payload):
                self.payload = payload
                return len(payload)

            def flush(self):
                raise BrokenPipeError("reader closed")

        output = BrokenOutput()
        with self.assertRaises(BrokenPipeError):
            self.app.consume_inbox(self.lane["session"], [source["id"]], self.lane["surface"], output)
        self.assertIn(source["body"], output.payload)
        with closing(self.app.database()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM acknowledgements").fetchone()[0], 0)
        self.assertEqual(self.app.scan()["targets"][0]["overdue"], 1)

    def test_inbox_rechecks_current_binding_after_output_before_ack(self):
        source = message(self.lane)
        self.host.rows = [source]
        original_messages = self.host.messages
        calls = 0

        def replace_binding(target):
            nonlocal calls
            calls += 1
            rows = original_messages(target)
            if calls == 2:
                self.host.hold = "Session replaced"
            return rows

        self.host.messages = replace_binding
        output = io.StringIO()
        with self.assertRaisesRegex(ValueError, "Session replaced"):
            self.app.consume_inbox(self.lane["session"], [source["id"]], self.lane["surface"], output)
        self.assertEqual(json.loads(output.getvalue()), {"messages": [compact_message(source)]})
        with closing(self.app.database()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM acknowledgements").fetchone()[0], 0)

    def test_inbox_refuses_changed_source_after_output(self):
        source = message(self.lane)
        self.host.rows = [source]
        calls = 0

        def changed_source(_target):
            nonlocal calls
            calls += 1
            current = copy.deepcopy(source)
            if calls > 1:
                current["body"] = "changed after selection"
            return [current]

        self.host.messages = changed_source
        output = io.StringIO()
        with self.assertRaisesRegex(ValueError, "Message changed during output"):
            self.app.consume_inbox(self.lane["session"], [source["id"]], self.lane["surface"], output)
        self.assertEqual(json.loads(output.getvalue()), {"messages": [compact_message(source)]})
        with closing(self.app.database()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM acknowledgements").fetchone()[0], 0)

    def test_inbox_bounds_ids_and_json_output_before_acknowledging(self):
        source = message(self.lane, body="x" * hf.INBOX_OUTPUT_LIMIT)
        self.host.rows = [source]
        output = io.StringIO()
        with self.assertRaisesRegex(ValueError, "exceeds 64 KiB"):
            self.app.consume_inbox(self.lane["session"], [source["id"]], self.lane["surface"], output)
        self.assertEqual(output.getvalue(), "")
        ids = [uid() for _ in range(hf.INBOX_MESSAGE_LIMIT + 1)]
        with self.assertRaisesRegex(ValueError, "between 1 and 20"):
            self.app.consume_inbox(self.lane["session"], ids, self.lane["surface"], output)
        with self.assertRaisesRegex(ValueError, "Duplicate message IDs"):
            self.app.consume_inbox(self.lane["session"], [source["id"], source["id"]],
                                   self.lane["surface"], output)
        with closing(self.app.database()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM acknowledgements").fetchone()[0], 0)

    def test_cli_inbox_outputs_one_json_document_and_uses_bounded_lock(self):
        source = message(self.lane)
        self.host.rows = [source]
        original_locked = hf.Monitor.locked
        selected_timeouts = []

        @hf.contextlib.contextmanager
        def capture_timeout(app, timeout=0):
            selected_timeouts.append(timeout)
            with original_locked(app, timeout=timeout):
                yield

        with patch.object(hf.Monitor, "locked", capture_timeout), \
                patch.object(hf, "Cmux", side_effect=lambda _executable: self.host), \
                patch.dict("os.environ", {"CMUX_SURFACE_ID": self.lane["surface"]}):
            output = io.StringIO()
            with redirect_stdout(output):
                result = hf.main(["--state-dir", str(self.directory), "inbox", "--session",
                                  self.lane["session"], "--message", source["id"]])
        self.assertIsNone(result)
        self.assertEqual(selected_timeouts, [30])
        self.assertEqual(json.loads(output.getvalue()), {"messages": [compact_message(source)]})
        self.assertEqual(output.getvalue().count("\"messages\""), 1)
        self.assertEqual(self.app.scan()["targets"][0]["overdue"], 0)

    def test_cli_inbox_details_option_preserves_full_records(self):
        source = message(self.lane)
        self.host.rows = [source]
        with patch.object(hf, "Cmux", side_effect=lambda _executable: self.host), \
                patch.dict("os.environ", {"CMUX_SURFACE_ID": self.lane["surface"]}):
            output = io.StringIO()
            with redirect_stdout(output):
                result = hf.main(["--state-dir", str(self.directory), "inbox", "--session",
                                  self.lane["session"], "--message", source["id"], "--details"])
        self.assertIsNone(result)
        self.assertEqual(json.loads(output.getvalue()), {"messages": [source]})
        self.assertEqual(self.app.scan()["targets"][0]["overdue"], 0)

    def test_prompt_reader_command_uses_active_runtime_and_custom_state_path(self):
        custom_state = self.directory / "private state with spaces"
        custom_state.mkdir()
        hf.atomic_json(custom_state / "config.json", copy.deepcopy(self.config))
        app = hf.Monitor(custom_state, self.host, clock=lambda: self.now)
        source = message(self.lane)
        self.host.rows = [source]
        app.scan(send=True)
        expected = shlex.join([
            sys.executable, str(Path(hf.__file__).resolve()), "--state-dir", str(custom_state),
            "--cmux", "cmux", "inbox", "--session", self.lane["session"],
            "--message", source["id"],
        ])
        prompt = self.host.sent[-1][1]
        self.assertIn(expected, prompt)
        self.assertEqual(shlex.split(expected)[2:6], ["--state-dir", str(custom_state), "--cmux", "cmux"])

    def test_prompt_uses_owned_installed_launcher_only_for_matching_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home with spaces"
            runtime_root = home / ".local/share/starfleet-ai/hailing-frequencies"
            staging = Path(temporary) / "staging"
            staging.mkdir()
            staged_script = staging / "hailing_frequencies.py"
            staged_service = staging / "service.py"
            staged_temporal = staging / "temporal_beacon.py"
            staged_guide = staging / "guide.md"
            staged_agent_notify = staging / "agent-notify"
            staged_agent_notify_guide = staging / "agent-notify-guide.md"
            staged_script.write_bytes(b"immutable runtime script")
            staged_service.write_bytes(b"immutable runtime service")
            source_app = Path(__file__).resolve().parents[1]
            staged_temporal.write_bytes((source_app / "temporal_beacon.py").read_bytes())
            staged_guide.write_bytes((source_app / "guide.md").read_bytes())
            staged_agent_notify.write_bytes((source_app / "agent-notify").read_bytes())
            staged_agent_notify_guide.write_bytes((source_app / "agent-notify-guide.md").read_bytes())
            skill_source = source_app / "skills/temporal-beacon/SKILL.md"
            agent_notify_skill_source = source_app / "skills/agent-notify/SKILL.md"
            sources = {"hailing_frequencies.py": staged_script.read_bytes(), "service.py": staged_service.read_bytes(),
                       "temporal_beacon.py": staged_temporal.read_bytes(), "guide.md": staged_guide.read_bytes(),
                       "agent-notify": staged_agent_notify.read_bytes(),
                       "agent-notify-guide.md": staged_agent_notify_guide.read_bytes(),
                       "skills/temporal-beacon/SKILL.md": skill_source.read_bytes(),
                       "skills/agent-notify/SKILL.md": agent_notify_skill_source.read_bytes()}
            digest = hashlib.sha256(b"".join(name.encode() + b"\0" + content
                                              for name, content in sources.items())).hexdigest()
            runtime = runtime_root / digest
            runtime.mkdir(parents=True)
            script = runtime / "hailing_frequencies.py"
            service_path = runtime / "service.py"
            for name, content in sources.items():
                target = runtime / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            hf.atomic_json(runtime / "installation.json", {"sha256": digest, "python": sys.executable})
            launcher = home / ".local/bin/hailing-frequencies"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("#!/bin/sh\n" + hf.INSTALL_MARKER + "\nexec " + shlex.quote(sys.executable) + " " +
                                shlex.quote(str(script.resolve())) + ' "$@"\n')
            launcher.chmod(0o755)
            default_state = home / ".local/state/starfleet-ai/hailing-frequencies"
            app = hf.Monitor(default_state, self.host)
            message_id = uid()
            with patch("pathlib.Path.home", return_value=home), patch.object(hf, "DEFAULT_STATE", default_state), \
                    patch.object(hf, "__file__", str(script)):
                command = app.inbox_command(self.lane, [message_id])
                expected = shlex.join([str(launcher), "--state-dir", str(default_state), "inbox",
                                       "--session", self.lane["session"], "--message", message_id])
                self.assertEqual(command, expected)
                launcher.write_text(launcher.read_text().replace(str(script.resolve()), str(runtime / "other.py")))
                fallback = app.inbox_command(self.lane, [uid()])
                self.assertIn(shlex.quote(str(script.resolve())), fallback)
                app.cmux.executable = "custom cmux"
                custom_cmux = app.inbox_command(self.lane, [uid()])
                self.assertIn("--cmux 'custom cmux'", custom_cmux)

    def test_ack_requires_exact_enrolled_recipient_metadata_and_generation(self):
        source = message(self.lane)
        cases = (
            {"recipient_surface_id": uid()},
            {"recipient_workspace_id": uid()},
            {"created_at": 0},
        )
        for changes in cases:
            row = dict(source)
            row.update(changes)
            self.host.rows = [row]
            with self.assertRaisesRegex(ValueError, "missing, ambiguous, or outside"):
                self.app.acknowledge(self.lane["session"], source["id"], self.lane["surface"])

    def test_coordinator_surface_cannot_ack_another_recipients_message(self):
        coordinator = target("coordinator")
        self.config["targets"].append(coordinator)
        self.config["coordinator"] = coordinator["session"]
        self.save()
        source = message(self.lane)
        self.host.rows = [source]
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.app.acknowledge(self.lane["session"], source["id"], coordinator["surface"])
        with self.assertRaisesRegex(ValueError, "missing, ambiguous, or outside"):
            self.app.acknowledge(coordinator["session"], source["id"], coordinator["surface"])

    def test_only_exact_post_source_reply_from_current_target_acknowledges(self):
        source = message(self.lane)
        answer = reply(source, self.lane)
        self.host.rows = [source, answer]
        result = self.app.scan()["targets"][0]
        self.assertEqual((result["native_unread"], result["acknowledged"], result["overdue"]), (1, 1, 0))

        wrong_replies = (
            {"sender_surface_id": uid()},
            {"sender_workspace_id": uid()},
            {"recipient_surface_id": uid()},
            {"recipient_workspace_id": uid()},
            {"created_at": source["created_at"]},
            {"created_at": 0},
            {"in_reply_to": uid()},
        )
        for changes in wrong_replies:
            source = message(self.lane)
            self.host.rows = [source, reply(source, self.lane, **changes)]
            result = self.app.scan()["targets"][0]
            self.assertEqual(result["acknowledged"], 0, changes)
            self.assertEqual(result["overdue"], 1, changes)

    def test_reply_ack_requires_the_current_enrolled_binding(self):
        source = message(self.lane)
        self.host.rows = [source, reply(source, self.lane)]
        self.host.hold = "Session replaced"
        result = self.app.scan()["targets"][0]
        self.assertEqual(result["acknowledged"], 0)
        self.assertEqual(result["overdue"], 1)
        self.assertEqual(result["acknowledgment_hold"], "Session replaced")
        self.host.hold = None
        result = self.app.scan()["targets"][0]
        self.assertEqual(result["acknowledged"], 1)
        self.assertEqual(result["overdue"], 0)

    def test_duplicate_enrollment_and_dangling_coordinator_fail_closed(self):
        self.config["targets"].append(copy.deepcopy(self.lane))
        self.save()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.app.config()
        self.config["targets"].pop()
        self.config["coordinator"] = uid()
        self.save()
        with self.assertRaisesRegex(ValueError, "not enrolled"):
            self.app.config()

    def test_invalid_timing_configuration_is_rejected(self):
        for key, value in (("interval", 0), ("remind_after", float("nan")), ("escalate_after", 2)):
            config = copy.deepcopy(self.config)
            config[key] = value
            hf.atomic_json(self.directory / "config.json", config)
            with self.assertRaises(ValueError):
                self.app.config()

    def test_conflicting_operation_cannot_send(self):
        with self.app.locked():
            with self.assertRaisesRegex(ValueError, "owns this state"):
                with hf.Monitor(self.directory).locked():
                    self.fail("Second lock acquired")

    def test_ack_waits_for_scan_lock_then_completes(self):
        source = message(self.lane)
        self.host.rows = [source]
        lock_stream = (self.directory / "monitor.lock").open("a")
        hf.fcntl.flock(lock_stream, hf.fcntl.LOCK_EX | hf.fcntl.LOCK_NB)
        releaser = threading.Thread(target=lambda: (time.sleep(0.2), hf.fcntl.flock(lock_stream, hf.fcntl.LOCK_UN), lock_stream.close()))
        releaser.start()
        try:
            with self.app.locked(timeout=1):
                result = self.app.acknowledge(self.lane["session"], source["id"], self.lane["surface"])
            self.assertEqual(result["evidence"], "explicit")
        finally:
            releaser.join()

    def test_bounded_lock_timeout_requests_next_checkpoint_retry(self):
        lock_stream = (self.directory / "monitor.lock").open("a")
        hf.fcntl.flock(lock_stream, hf.fcntl.LOCK_EX | hf.fcntl.LOCK_NB)
        try:
            with self.assertRaisesRegex(hf.LockBusyError, "retry at the next checkpoint"):
                with self.app.locked(timeout=0.05):
                    self.fail("Contended lock unexpectedly acquired")
        finally:
            hf.fcntl.flock(lock_stream, hf.fcntl.LOCK_UN)
            lock_stream.close()

    def test_cli_ack_selects_bounded_lock_wait(self):
        original_locked = hf.Monitor.locked
        selected = []

        @hf.contextlib.contextmanager
        def capture_timeout(app, timeout=0):
            selected.append(timeout)
            with original_locked(app, timeout=timeout):
                yield

        with patch.object(hf.Monitor, "locked", capture_timeout), \
             patch.object(hf.Monitor, "acknowledge", return_value={"acknowledged": True}), \
             patch.dict("os.environ", {"CMUX_SURFACE_ID": self.lane["surface"]}):
            result = hf.main(["--state-dir", str(self.directory), "ack", "--session", self.lane["session"],
                              "--message", uid()])
        self.assertEqual(selected, [30])
        self.assertTrue(result["acknowledgments"][0]["acknowledged"])


class AttentionHost(Host):
    def __init__(self):
        super().__init__()
        self.binding_calls = 0
        self.hold_on_binding_call = None

    def binding(self, _target):
        self.binding_calls += 1
        if self.hold_on_binding_call == self.binding_calls:
            self.hold = "Human draft appeared"
        if self.hold:
            raise ValueError(self.hold)

    def submit(self, destination, text, on_transport_start=None):
        try:
            self.binding(destination)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            raise hf.DeliveryHoldError(str(error)) from error
        if on_transport_start is not None:
            on_transport_start()
        self.sent.append((destination, text))
        if self.failure:
            raise self.failure
        return {"submitted": True}


class AttentionOnceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.host = AttentionHost()
        self.now = 1000
        self.app = hf.Monitor(self.directory, self.host, clock=lambda: self.now)
        self.lane = target()
        self.config = dict(version=1, targets=[self.lane], coordinator=None, remind_after=300,
                           escalate_after=900, cooldown=600, interval=30)
        hf.atomic_json(self.directory / "config.json", self.config)

    def cli(self, body=b"Review the current checkpoint now.", session=None, surface=None, event_key="checkpoint-1",
            caller_session=True, launch_kind=True):
        session = session or self.lane["session"]
        surface = self.lane["surface"] if surface is None else surface
        stdin = io.TextIOWrapper(io.BytesIO(body), encoding="utf-8")
        session_env = "CLAUDE_CODE_SESSION_ID" if self.lane["harness"] == "claude" else "CODEX_THREAD_ID"
        env = {"CMUX_SURFACE_ID": surface, "CMUX_AGENT_LAUNCH_KIND": "",
               "CLAUDE_CODE_SESSION_ID": "", "CODEX_THREAD_ID": ""}
        if caller_session is True:
            env[session_env] = session
        elif caller_session:
            env[session_env] = caller_session
        if launch_kind is True:
            env["CMUX_AGENT_LAUNCH_KIND"] = self.lane["harness"]
        elif launch_kind:
            env["CMUX_AGENT_LAUNCH_KIND"] = launch_kind
        with patch.object(hf, "Cmux", return_value=self.host), \
             patch.dict("os.environ", env, clear=False), \
             patch("sys.stdin", stdin):
            return hf.main(["--state-dir", str(self.directory), "attention-once",
                            "--session", session, "--event-key", event_key])

    def intent(self, event_key="checkpoint-1"):
        with hf.contextlib.closing(self.app.database()) as db:
            return db.execute("SELECT body_sha256,outcome,attempted,detail FROM attention_intents "
                              "WHERE session=? AND started_at=? AND event_key=?",
                              (self.lane["session"], self.lane["started_at"], event_key)).fetchone()

    def test_submits_only_to_callers_enrolled_session_and_deduplicates_after_restart(self):
        body = b"Review the current checkpoint now."
        result = self.cli(body=body)
        self.assertEqual(result["outcome"], "submitted")
        self.assertTrue(result["attempted"])
        self.assertEqual(set(result), {"outcome", "attempted", "detail", "event_key"})
        self.assertEqual(len(self.host.sent), 1)
        self.assertEqual(self.host.sent[0], (self.lane, body.decode()))
        row = self.intent()
        self.assertEqual(row[:3], (hashlib.sha256(body).hexdigest(), "submitted", 1))
        self.assertNotIn(body.decode(), row[3])

        duplicate = self.cli(body=body)
        self.assertEqual(duplicate, result)
        self.assertEqual(len(self.host.sent), 1)

        changed = self.cli(body=b"A different prompt", event_key="checkpoint-1")
        self.assertEqual(changed["outcome"], "rejected")
        self.assertFalse(changed["attempted"])
        self.assertEqual(len(self.host.sent), 1)

    def test_missing_enrollment_or_wrong_surface_is_unavailable_without_writes(self):
        missing = self.cli(session=uid())
        self.assertEqual((missing["outcome"], missing["attempted"]), ("unavailable", False))
        wrong_surface = self.cli(event_key="wrong-surface", surface=uid())
        self.assertEqual((wrong_surface["outcome"], wrong_surface["attempted"]), ("unavailable", False))
        missing_surface = self.cli(event_key="missing-surface", surface="")
        self.assertEqual((missing_surface["outcome"], missing_surface["attempted"]), ("unavailable", False))
        self.assertEqual(self.host.binding_calls, 0)
        self.assertFalse((self.directory / "receipts.sqlite3").exists())
        self.assertFalse(self.host.sent)

    def test_present_caller_session_and_launch_kind_must_match_but_are_optional(self):
        for result in (self.cli(event_key="wrong-session", caller_session=uid()),
                       self.cli(event_key="wrong-harness", launch_kind="codex")):
            self.assertEqual((result["outcome"], result["attempted"]), ("unavailable", False))
        self.assertFalse((self.directory / "receipts.sqlite3").exists())
        result = self.cli(event_key="no-optional-env", caller_session=False, launch_kind=False)
        self.assertEqual((result["outcome"], result["attempted"]), ("submitted", True))
        self.assertEqual(len(self.host.sent), 1)

    def test_native_claude_teams_launch_kind_matches_claude_target(self):
        result = self.cli(event_key="claude-teams", launch_kind="claudeTeams")
        self.assertEqual((result["outcome"], result["attempted"]), ("submitted", True))
        self.assertEqual(len(self.host.sent), 1)

    def test_preflight_human_hold_writes_no_intent_and_returns_held(self):
        self.host.hold = "Human draft or approval is active"
        result = self.cli()
        self.assertEqual((result["outcome"], result["attempted"]), ("held", False))
        self.assertIn("Human draft", result["detail"])
        self.assertFalse((self.directory / "receipts.sqlite3").exists())
        self.assertFalse(self.host.sent)

    def test_second_binding_hold_is_persisted_and_never_replayed(self):
        self.host.hold_on_binding_call = 2
        first = self.cli()
        self.assertEqual((first["outcome"], first["attempted"]), ("held", False))
        self.assertEqual(self.intent()[1:3], ("held", 0))
        self.host.hold = None
        self.host.hold_on_binding_call = None
        repeated = self.cli()
        self.assertEqual(repeated, first)
        self.assertFalse(self.host.sent)

    def test_uncertain_transport_is_not_replayed_and_does_not_store_body(self):
        self.host.failure = RuntimeError("failure must not echo the private message")
        result = self.cli(body=b"private checkpoint body")
        self.assertEqual((result["outcome"], result["attempted"]), ("uncertain", True))
        self.assertNotIn("private checkpoint body", result["detail"])
        row = self.intent()
        self.assertEqual(row[1:3], ("uncertain", 1))
        self.assertNotIn("private checkpoint body", row[3])
        self.host.failure = None
        self.assertEqual(self.cli(body=b"private checkpoint body"), result)
        self.assertEqual(len(self.host.sent), 1)

    def test_crash_after_intent_commit_suppresses_retry(self):
        def crash_before_submit(_target, _message, on_transport_start=None):
            raise KeyboardInterrupt()

        self.host.submit = crash_before_submit
        with self.assertRaises(KeyboardInterrupt):
            self.cli(body=b"crash before transport", event_key="crash-key")
        row = self.intent("crash-key")
        self.assertEqual(row[1:3], ("uncertain", 0))
        self.host.submit = AttentionHost.submit.__get__(self.host, AttentionHost)
        result = self.cli(body=b"crash before transport", event_key="crash-key")
        self.assertEqual((result["outcome"], result["attempted"]), ("uncertain", False))
        self.assertFalse(self.host.sent)

    def test_busy_lock_and_oversized_input_are_bounded_results_without_submit(self):
        with self.app.locked():
            busy = self.cli()
        self.assertEqual((busy["outcome"], busy["attempted"]), ("held", False))
        too_large = self.cli(body=b"x" * (hf.ATTENTION_MESSAGE_LIMIT + 1), event_key="oversized")
        self.assertEqual((too_large["outcome"], too_large["attempted"]), ("rejected", False))
        self.assertIn("4096", too_large["detail"])
        self.assertFalse((self.directory / "receipts.sqlite3").exists())
        self.assertFalse(self.host.sent)

    def test_accepts_exactly_4096_utf8_bytes(self):
        result = self.cli(body=b"x" * hf.ATTENTION_MESSAGE_LIMIT, event_key="exact-boundary")
        self.assertEqual((result["outcome"], result["attempted"]), ("submitted", True))
        self.assertEqual(len(self.host.sent[0][1].encode("utf-8")), hf.ATTENTION_MESSAGE_LIMIT)

    def test_rejection_cli_exit_matches_structured_result(self):
        result = subprocess.run(
            [sys.executable, str(Path(hf.__file__)), "--state-dir", str(self.directory),
             "attention-once", "--session", self.lane["session"], "--event-key", "oversized"],
            input=b"x" * (hf.ATTENTION_MESSAGE_LIMIT + 1), capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, b"")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["outcome"], "rejected")
        self.assertEqual(set(payload), {"outcome", "attempted", "detail", "event_key"})

    def test_event_key_is_scoped_to_session_generation(self):
        first = self.cli(body=b"First generation prompt")
        self.assertEqual(first["outcome"], "submitted")
        self.config["targets"][0]["started_at"] = 2
        self.lane["started_at"] = 2
        hf.atomic_json(self.directory / "config.json", self.config)
        second = self.cli(body=b"Replacement generation prompt")
        self.assertEqual(second["outcome"], "submitted")
        self.assertEqual(len(self.host.sent), 2)


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.t = target()
        self.cmux = hf.Cmux(hook_directory=self.temp.name)
        self.current = dict(truncated=False, owner_availability={"agent_sessions": "available"}, items=[{
            "placement": {"kind": "local"}, "projections": [{"panel_id": self.t["surface"], "workspace_id": self.t["workspace"]}],
            "agents": [{"session_id": self.t["session"], "kind": "claude", "state": "working"}]}])
        self.store = {"sessions": {self.t["session"]: dict(surfaceId=self.t["surface"], workspaceId=self.t["workspace"],
                      agentLifecycle="running", startedAt=1)},
                      "activeSessionsBySurface": {self.t["surface"]: {"sessionId": self.t["session"]}}}
        self.guard = dict(surface_id=self.t["surface"], workspace_id=self.t["workspace"], state="empty", agent=True,
                          terminal=True, blocks_typing=False, waiting_on_human=False, lifecycle="running")
        self.settings = dict(messages_enabled=True, receiving=True, workspace_receiving=True)
        self.calls = []

        def command(*args):
            self.calls.append(args)
            if args[0] == "current":
                return self.current
            if args[0] == "paste":
                return {"submitted": True}
            return {"surface.input_state": self.guard, "agent.message.settings": self.settings}[args[1]]

        self.cmux.command = command
        self.save()

    def save(self):
        Path(self.temp.name, self.t["harness"] + "-hook-sessions.json").write_text(json.dumps(self.store))

    def test_exact_current_binding_and_single_agent_aware_submit(self):
        self.cmux.submit(self.t, "Monitor\nRead queue")
        pastes = [args for args in self.calls if args[0] == "paste"]
        self.assertEqual(pastes, [("paste", "--surface", self.t["surface"], "--submit", "--", "Monitor\nRead queue")])
        self.assertFalse(any("--force" in args for args in self.calls))

    def test_submit_callback_runs_only_after_fresh_binding(self):
        started = []
        self.cmux.submit(self.t, "nudge", on_transport_start=lambda: started.append(True))
        self.assertEqual(started, [True])
        self.guard["state"] = "draft"
        started.clear()
        with self.assertRaises(hf.DeliveryHoldError):
            self.cmux.submit(self.t, "nudge", on_transport_start=lambda: started.append(True))
        self.assertFalse(started)
        self.assertEqual(len([args for args in self.calls if args[0] == "paste"]), 1)

    def test_message_scan_requests_all_native_states(self):
        self.cmux.command = lambda *args: self.calls.append(args) or {"messages": []}
        self.cmux.messages(self.t)
        self.assertEqual(self.calls, [(
            "rpc", "agent.message.list", json.dumps({"surface": self.t["surface"], "limit": hf.SCAN_LIMIT})
        )])

    def test_replaced_claude_binding_is_refused(self):
        self.store["activeSessionsBySurface"][self.t["surface"]]["sessionId"] = uid()
        self.save()
        with self.assertRaisesRegex(ValueError, "replaced"):
            self.cmux.submit(self.t, "nudge")
        self.assertFalse(any(args[0] == "paste" for args in self.calls))

    def test_human_draft_permission_and_unknown_guards_fail_closed(self):
        for change in ({"state": "draft"}, {"waiting_on_human": True}, {"blocks_typing": True},
                       {"state": "unknown"}, {"agent": False}, {"workspace_id": uid()}):
            original = dict(self.guard)
            self.guard.update(change)
            with self.assertRaises(ValueError):
                self.cmux.submit(self.t, "nudge")
            self.guard = original

    def test_missing_guard_fields_fail_closed(self):
        self.guard.pop("blocks_typing")
        with self.assertRaises(ValueError):
            self.cmux.binding(self.t)

    def test_disabled_native_messages_are_not_bypassed(self):
        self.settings["receiving"] = False
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.cmux.submit(self.t, "nudge")

    def test_incomplete_projection_is_refused(self):
        self.current["truncated"] = True
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.cmux.binding(self.t)

    def test_omitted_history_is_safe_when_exact_active_owner_is_present(self):
        self.current["items"][0]["omitted"] = {"agents": 20}
        self.cmux.binding(self.t)
        self.current["items"][0]["agents"] = []
        with self.assertRaisesRegex(ValueError, "absent"):
            self.cmux.binding(self.t)

    def test_background_work_does_not_mean_a_human_input_hold(self):
        self.store["sessions"][self.t["session"]]["agentLifecycle"] = "backgroundWorkPending"
        self.current["items"][0]["agents"][0]["state"] = "idle"
        self.guard["lifecycle"] = "backgroundWorkPending"
        self.save()
        self.cmux.binding(self.t)
        self.guard["waiting_on_human"] = True
        with self.assertRaisesRegex(ValueError, "Human draft"):
            self.cmux.binding(self.t)

    def test_non_submission_is_uncertain_not_success(self):
        original = self.cmux.command
        self.cmux.command = lambda *args: {"submitted": False} if args[0] == "paste" else original(*args)
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.cmux.submit(self.t, "nudge")

    def test_codex_binding_needs_its_own_current_identity(self):
        self.t["harness"] = "codex"
        self.current["items"][0]["agents"][0]["kind"] = "codex"
        self.save()
        self.cmux.binding(self.t)
        self.current["items"][0]["agents"][0]["state"] = "ended"
        with self.assertRaisesRegex(ValueError, "ended"):
            self.cmux.binding(self.t)

    def test_stop_prevents_starting_another_host_operation(self):
        cmux = hf.Cmux()
        cmux.stopping = lambda: True
        with patch.object(subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "stopping"):
                cmux.command("current")
            run.assert_not_called()


class InstallationTests(unittest.TestCase):
    def test_source_guide_print_is_state_free_outside_a_git_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "uninitialized-home"
            cwd = root / "not-a-git-checkout"
            cwd.mkdir()
            guide = Path(hf.__file__).resolve().with_name("guide.md").read_text()
            command = [sys.executable, str(Path(hf.__file__).resolve()), "--state-dir",
                       str(root / "missing-state"), "guide", "--print"]
            with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
                result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, guide)
            self.assertEqual(result.stderr, "")
            self.assertFalse(home.exists())
            self.assertFalse((root / "missing-state").exists())

    def test_installed_guide_print_survives_source_removal_without_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "staging-source"
            source.mkdir()
            source_app = Path(hf.__file__).resolve().parent
            for name in install.SOURCE_NAMES:
                shutil.copyfile(source_app / name, source / name)
            for skill_name in install.SKILL_NAMES:
                skill = source / skill_name
                skill.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_app / skill_name, skill)
            staged_installer = source / "install.py"
            staged_installer.write_text("")
            expected = (source / "guide.md").read_text()
            agent_notify_guide = (source / "agent-notify-guide.md").read_text()
            with patch.object(install, "__file__", str(staged_installer)):
                result = install.install(root / "runtime", root / "bin")
            shutil.rmtree(source)

            home = root / "uninitialized-home"
            cwd = root / "not-a-git-checkout"
            cwd.mkdir()
            launcher = result["command"]
            agent_notify_launcher = result["agent_notify_command"]
            for arguments in (("guide", "--print"), ("guide",)):
                with patch.dict("os.environ", {"HOME": str(home)}, clear=False):
                    printed = subprocess.run([launcher, *arguments], cwd=cwd,
                                             capture_output=True, text=True)
                self.assertEqual(printed.returncode, 0, printed.stderr)
                self.assertEqual(printed.stdout, expected)
                self.assertEqual(printed.stderr, "")
            self.assertFalse(home.exists())
            self.assertEqual(Path(result["runtime"], "guide.md").read_text(), expected)
            with patch.dict("os.environ", {"HOME": str(home), "AGENT_NOTIFY_HOME": str(home / "config")}, clear=False):
                printed = subprocess.run([agent_notify_launcher, "guide", "--print"], cwd=cwd,
                                         capture_output=True, text=True)
            self.assertEqual(printed.returncode, 0, printed.stderr)
            self.assertEqual(printed.stdout, agent_notify_guide)
            self.assertEqual(printed.stderr, "")
            self.assertFalse(home.exists())
            self.assertFalse((home / "config").exists())
            self.assertEqual(Path(result["agent_notify_skill"]).read_bytes(),
                             (source_app / install.AGENT_NOTIFY_SKILL_NAME).read_bytes())
            self.assertEqual(Path(result["agent_notify_skill"]).stat().st_mode & 0o222, 0)

    def test_install_survives_checkout_and_uninstall_preserves_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = install.install(root / "runtime", root / "bin")
            launcher = Path(result["command"])
            agent_notify = Path(result["agent_notify_command"])
            self.assertIn("usage:", subprocess.check_output([str(launcher), "--help"], text=True))
            self.assertEqual(subprocess.check_output([str(agent_notify), "guide", "--print"], text=True),
                             Path(result["runtime"], "agent-notify-guide.md").read_text())
            self.assertEqual(install.install(root / "runtime", root / "bin")["runtime"], result["runtime"])
            install.install(root / "runtime", root / "bin", uninstall=True)
            self.assertFalse(launcher.exists())
            self.assertFalse(agent_notify.exists())
            self.assertTrue(Path(result["runtime"]).is_dir())

    def test_foreign_launcher_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "hailing-frequencies").write_text("foreign")
            with self.assertRaisesRegex(ValueError, "not owned"):
                install.install(root / "runtime", root)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "agent-notify").write_text("foreign")
            with self.assertRaisesRegex(ValueError, "not owned"):
                install.install(root / "runtime", root)


class ServiceTests(unittest.TestCase):
    def test_foreground_service_status_singleton_and_graceful_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(hf.__file__)
            args = [sys.executable, str(app), "--state-dir", temporary]
            subprocess.run([*args, "init", "--interval", "5"], check=True, capture_output=True)
            # No recipients: this must never contact CMUX or submit a prompt.
            process = subprocess.Popen([*args, "serve"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not Path(temporary, "last-scan.json").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(Path(temporary, "last-scan.json").exists())
                duplicate = subprocess.run([*args, "serve"], capture_output=True, text=True, timeout=5)
                self.assertNotEqual(duplicate.returncode, 0)
                self.assertIn("already serves", duplicate.stderr)
                status = json.loads(subprocess.check_output([*args, "status"], text=True))
                self.assertEqual(status["last_scan"]["mode"], "observe")
                process.terminate()
                stdout, stderr = process.communicate(timeout=4)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertIn('"stopped": true', stdout)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
