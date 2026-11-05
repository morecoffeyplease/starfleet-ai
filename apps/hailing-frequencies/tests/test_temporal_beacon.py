import io
import json
import contextlib
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hailing_frequencies as hf
import temporal_beacon as tb


def uid():
    return str(uuid.uuid4()).upper()


class Clock:
    def __init__(self, value=1_800_000_000):
        self.value = value

    def __call__(self):
        return self.value


class Host:
    def __init__(self, root):
        self.executable = "cmux"
        self.hook_directory = Path(root)
        self.hold = None
        self.sent = []
        self.before_submit = None
        self.failure = None
        self.activity_value = None

    def binding(self, _target):
        if self.hold:
            raise ValueError(self.hold)

    def activity(self, target):
        if isinstance(self.activity_value, Exception):
            raise self.activity_value
        return self.activity_value

    def submit(self, target, text, on_transport_start=None):
        if self.before_submit:
            self.before_submit()
        if on_transport_start:
            on_transport_start()
        self.sent.append((target, text))
        if self.failure:
            raise self.failure
        return {"submitted": True}


class TemporalBeaconTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.target = {"surface": uid(), "workspace": uid(), "session": uid(), "harness": "claude",
                       "label": "pilot", "started_at": 100.0, "cutoff": 100.0}
        self.host = Host(self.root)
        self.app = hf.Monitor(self.root, self.host, clock=self.clock)
        hf.atomic_json(self.root / "config.json", {"version": 1, "targets": [self.target], "coordinator": None,
            "remind_after": 300, "escalate_after": 900, "cooldown": 600, "interval": 5})
        hf.atomic_json(self.root / "claude-hook-sessions.json", {"sessions": {self.target["session"]: {
            "surfaceId": self.target["surface"], "workspaceId": self.target["workspace"],
            "startedAt": self.target["started_at"], "cwd": str(self.root)}},
            "activeSessionsBySurface": {self.target["surface"]: {"sessionId": self.target["session"]}}})
        self.beacon = tb.TemporalBeacon(self.app, self.clock)
        hf.atomic_json(self.root / "timer-last-scan.json", {"at": self.clock.value, "supports_idle": True,
                       "worker_pid": 123, "worker_token": "unit-worker"})
        hf.atomic_json(self.root / "service.json", {"at": self.clock.value, "state": "running",
                       "worker_pid": 123, "worker_token": "unit-worker"})
        leases = patch.object(self.beacon, "worker_leases_held", return_value=True)
        leases.start()
        self.addCleanup(leases.stop)
        self.env = {"CMUX_AGENT_LAUNCH_KIND": "claude", "CLAUDE_CODE_SESSION_ID": self.target["session"],
                    "CMUX_SURFACE_ID": self.target["surface"]}

    def set_timer(self, **kwargs):
        with patch.dict("os.environ", self.env):
            return self.beacon.set(continuation="Continue the bounded task", **kwargs)

    def status(self, timer_id):
        with self.beacon.locked(), contextlib.closing(self.beacon.database()) as db:
            db.execute("BEGIN")
            timer = db.execute("SELECT status FROM timers WHERE id=?", (timer_id,)).fetchone()[0]
            occurrences = db.execute("SELECT status FROM timer_occurrences WHERE timer_id=? ORDER BY sequence", (timer_id,)).fetchall()
            db.commit()
            return timer, [row[0] for row in occurrences]

    def activity(self, at=None, state="idle"):
        self.host.activity_value = {"state": state, "last_activity_at":
            datetime.fromtimestamp(self.clock.value if at is None else at, timezone.utc).isoformat()}

    def test_idle_activity_resets_deadline_and_one_receipt_completes_episode(self):
        self.activity()
        timer = self.set_timer(after_idle="50m")["timer"]
        self.clock.value += 2400
        self.activity()
        self.clock.value += 601
        self.assertEqual(self.beacon.tick()["timers"][0]["status"], "waiting_for_idle")
        self.assertFalse(self.host.sent)
        self.clock.value += 2400
        self.beacon.tick()
        self.assertEqual(len(self.host.sent), 1)
        with patch.dict("os.environ", self.env):
            self.beacon.receive(self.beacon.owner()[1], io.StringIO(), timer["id"])
        self.assertEqual(self.status(timer["id"])[0], "completed")
        self.activity()  # The reminder's own response must not create another occurrence.
        self.clock.value += 7200
        self.beacon.tick()
        self.assertEqual(len(self.host.sent), 1)

    def test_activity_reader_matches_exact_session_and_rejects_incomplete_snapshot(self):
        current = {"truncated": False, "owner_availability": {"agent_sessions": "available"}, "items": [
            {"placement": {"kind": "local"}, "projections": [{"panel_id": self.target["surface"],
             "workspace_id": self.target["workspace"]}], "agents": [
                {"kind": "claude", "session_id": uid(), "state": "working", "last_activity_at": "other"},
                {"kind": "claude", "session_id": self.target["session"], "state": "idle",
                 "last_activity_at": "2027-01-01T00:00:00Z"}]}]}
        host = hf.Cmux()
        with patch.object(host, "command", return_value=current):
            self.assertEqual(host.activity(self.target)["last_activity_at"], "2027-01-01T00:00:00Z")
            current["truncated"] = True
            with self.assertRaisesRegex(ValueError, "incomplete"):
                host.activity(self.target)
            current["truncated"] = False
            current["items"][0]["agents"] = current["items"][0]["agents"][:1]
            with self.assertRaisesRegex(ValueError, "unavailable"):
                host.activity(self.target)

    def test_idle_timer_rejects_old_or_stopped_worker_without_breaking_fixed_timers(self):
        self.activity()
        hf.atomic_json(self.root / "timer-last-scan.json", {"at": self.clock.value})
        with self.assertRaisesRegex(ValueError, "Idle-aware worker"):
            self.set_timer(after_idle="1m")
        self.set_timer(after="1m")
        hf.atomic_json(self.root / "timer-last-scan.json", {"at": self.clock.value - 300, "supports_idle": True})
        with self.assertRaisesRegex(ValueError, "Idle-aware worker"):
            self.set_timer(after_idle="1m")

    def test_fresh_marker_rejects_replaced_generation_and_released_lifetime_locks(self):
        self.activity()
        hf.atomic_json(self.root / "service.json", {"at": self.clock.value, "state": "running",
                       "worker_pid": 456, "worker_token": "replacement"})
        with self.assertRaisesRegex(ValueError, "Idle-aware worker"):
            self.set_timer(after_idle="1m")
        hf.atomic_json(self.root / "service.json", {"at": self.clock.value, "state": "running",
                       "worker_pid": 123, "worker_token": "unit-worker"})
        self.beacon.worker_leases_held.return_value = False
        with self.assertRaisesRegex(ValueError, "Idle-aware worker"):
            self.set_timer(after_idle="1m")

    def test_working_session_never_gets_idle_warning_with_old_activity_stamp(self):
        self.activity(state="working")
        timer = self.set_timer(after_idle="1m")["timer"]
        self.clock.value += 70
        self.assertEqual(self.beacon.tick()["timers"][0]["status"], "waiting_for_idle")
        self.assertEqual(self.status(timer["id"]), ("active", []))
        self.assertFalse(self.host.sent)

    def test_idle_rechecks_activity_at_transport_boundary(self):
        self.activity()
        timer = self.set_timer(after_idle="1m")["timer"]
        self.clock.value += 61
        self.host.before_submit = lambda: self.activity()
        self.beacon.tick()
        self.assertEqual(self.status(timer["id"])[1], ["held"])
        self.assertFalse(self.host.sent)

    def test_activity_cancels_stale_held_occurrence_without_canceling_timer(self):
        self.activity()
        timer = self.set_timer(after_idle="1m")["timer"]
        self.clock.value += 61
        self.host.hold = "human draft"
        self.beacon.tick()
        self.host.hold = None
        self.activity()
        self.beacon.tick()
        self.assertEqual(self.status(timer["id"]), ("active", ["canceled"]))
        self.clock.value += 61
        self.beacon.tick()
        self.assertEqual(self.status(timer["id"])[1], ["canceled", "submitted"])

    def test_unknown_future_or_missing_idle_activity_fails_closed(self):
        for activity in [None, {"state": "idle", "last_activity_at": "invalid"},
                         {"state": "ended", "last_activity_at": "2027-01-01T00:00:00Z"}]:
            self.host.activity_value = activity
            with self.subTest(activity=activity), self.assertRaises(ValueError):
                self.set_timer(after_idle="1m")
        self.activity(at=self.clock.value + 300)
        with self.assertRaisesRegex(ValueError, "future"):
            self.set_timer(after_idle="1m")

    def test_idle_observe_scope_cancellation_and_uncertainty_preserve_no_replay(self):
        self.activity()
        timer = self.set_timer(after_idle="1m")["timer"]
        self.clock.value += 61
        self.beacon.tick(send=False)
        self.beacon.tick(only_sessions=[uid()])
        self.assertFalse(self.host.sent)
        self.host.failure = ValueError("transport outcome unknown")
        self.beacon.tick()
        self.beacon.tick()
        self.assertEqual(len(self.host.sent), 1)
        self.assertEqual(self.status(timer["id"])[1], ["uncertain"])

    def test_parsers_reject_bad_durations_and_missing_offsets(self):
        for value in ("", "0s", "-1m", "12x", "1e308d"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tb.duration(value)
        for value in ("2027-01-01T12:00:00", "tomorrow", "2027-01-01"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tb.offset_datetime(value)

    def test_offsets_normalize_to_the_same_instant(self):
        left, _ = tb.offset_datetime("2027-01-01T12:00:00-08:00")
        right, _ = tb.offset_datetime("2027-01-01T20:00:00Z")
        self.assertEqual(left, right)

    def test_request_id_is_idempotent_after_relative_and_absolute_deadline_passes(self):
        first = self.set_timer(after="10m", request_id="stable-1")["timer"]
        self.clock.value += 1200
        retry = self.set_timer(after="600s", request_id="stable-1")
        self.assertTrue(retry["idempotent"])
        self.assertEqual(retry["timer"]["id"], first["id"])
        absolute = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(self.clock.value + 60))
        second = self.set_timer(at=absolute, request_id="stable-2")["timer"]
        self.clock.value += 120
        retry_absolute = self.set_timer(at=absolute, request_id="stable-2")
        self.assertTrue(retry_absolute["idempotent"])
        self.assertEqual(retry_absolute["timer"]["id"], second["id"])

    def test_same_request_id_with_different_continuation_is_rejected(self):
        with patch.dict("os.environ", self.env):
            self.beacon.set(after="10m", continuation="first", request_id="stable")
            with self.assertRaisesRegex(ValueError, "different timer parameters"):
                self.beacon.set(after="10m", continuation="second", request_id="stable")

    def test_unrepresentable_finite_duration_fails_before_persisting_timer(self):
        with patch.dict("os.environ", self.env), self.assertRaisesRegex(ValueError, "supported range"):
            self.beacon.set(after="1e100d", continuation="never persisted")
        with contextlib.closing(self.beacon.database()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM timers").fetchone()[0], 0)

    def test_replacement_cancels_old_live_generation_for_same_owner(self):
        old = self.set_timer(after="10m", replacement_key="checkpoint")["timer"]
        new = self.set_timer(after="20m", replacement_key="checkpoint")["timer"]
        self.assertEqual(self.status(old["id"])[0], "canceled")
        self.assertEqual(self.status(new["id"])[0], "active")

    def test_worker_observe_mode_does_not_consume_timer(self):
        timer = self.set_timer(after="10s")["timer"]
        self.clock.value += 11
        report = self.beacon.tick(send=False)
        self.assertEqual(report["timers"], [{"timer_id": timer["id"], "status": "would_submit"}])
        self.assertEqual(self.status(timer["id"]), ("active", []))
        self.beacon.tick(send=True)
        self.assertEqual(self.status(timer["id"])[1], ["submitted"])

    def test_send_scope_does_not_consume_an_out_of_scope_timer(self):
        timer = self.set_timer(after="10s")["timer"]
        self.clock.value += 11
        self.beacon.tick(send=True, only_sessions=[uid()])
        self.assertEqual(self.status(timer["id"]), ("active", []))
        self.assertEqual(self.host.sent, [])

    def test_submission_intent_is_visible_before_cmux_submit(self):
        timer = self.set_timer(after="10s")["timer"]
        self.clock.value += 11
        def observe_intent():
            with contextlib.closing(sqlite3.connect(self.root / "receipts.sqlite3")) as other:
                state = other.execute("SELECT status,attempted_at FROM timer_occurrences WHERE timer_id=?", (timer["id"],)).fetchone()
            self.assertEqual(state, ("uncertain", self.clock.value))
        self.host.before_submit = observe_intent
        self.beacon.tick(send=True)
        self.assertEqual(self.status(timer["id"])[1], ["submitted"])

    def test_uncertain_submit_is_never_automatically_replayed(self):
        timer = self.set_timer(after="10s")["timer"]
        self.clock.value += 11
        self.host.failure = TimeoutError("outcome unknown")
        self.beacon.tick(send=True)
        self.host.failure = None
        self.clock.value += 50
        self.beacon.tick(send=True)
        self.assertEqual(len(self.host.sent), 1)
        self.assertEqual(self.status(timer["id"])[1], ["uncertain"])

    def test_held_occurrence_retries_once_safe_and_expires_at_until(self):
        timer = self.set_timer(every="10s", until="2027-01-15T10:00:00Z")["timer"]
        self.clock.value += 11
        self.host.hold = "Human draft"
        self.beacon.tick(send=True)
        self.assertEqual(self.status(timer["id"])[1], ["held"])
        self.host.hold = None
        self.beacon.tick(send=True)
        self.assertEqual(self.status(timer["id"])[1], ["submitted"])
        self.assertEqual(len(self.host.sent), 1)

        held = self.set_timer(every="10s", until="2027-01-15T10:00:00Z")["timer"]
        self.clock.value += 11
        self.host.hold = "permission prompt"
        self.beacon.tick(send=True)
        self.clock.value = tb.offset_datetime("2027-01-15T10:00:00Z")[0]
        self.beacon.tick(send=True)
        self.assertEqual(self.status(held["id"]), ("expired", ["expired"]))

    def test_restart_catches_up_one_missed_recurrence_after_receipt(self):
        timer = self.set_timer(every="10s")["timer"]
        self.clock.value += 101
        self.beacon.tick(send=True)
        self.beacon.tick(send=True)
        self.assertEqual(len(self.host.sent), 1)
        output = io.StringIO()
        with patch.dict("os.environ", self.env):
            self.beacon.receive(timer["owner_key"], output, timer["id"])
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["continuation"], "Continue the bounded task")
        self.beacon.tick(send=True)
        self.assertEqual(len(self.host.sent), 1)
        with self.beacon.locked(), contextlib.closing(self.beacon.database()) as db:
            next_due = db.execute("SELECT due_at FROM timers WHERE id=?", (timer["id"],)).fetchone()[0]
        self.assertGreater(next_due, self.clock.value)
        self.clock.value = next_due
        self.beacon.tick(send=True)
        self.assertEqual(len(self.host.sent), 2)
        self.assertEqual(self.status(timer["id"])[1], ["received", "submitted"])

    def test_first_recurrence_missed_past_until_expires_without_dispatch(self):
        until = self.clock.value + 20
        until_text = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(until))
        timer = self.set_timer(every="10s", until=until_text)["timer"]
        self.clock.value = until + 10
        self.beacon.tick(send=True)
        self.assertEqual(self.status(timer["id"]), ("expired", []))
        self.assertEqual(self.host.sent, [])

    def test_until_expiration_preserves_prior_received_occurrence(self):
        until = self.clock.value + 35
        until_text = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(until))
        timer = self.set_timer(every="10s", until=until_text)["timer"]
        self.clock.value += 11
        self.beacon.tick(send=True)
        with patch.dict("os.environ", self.env):
            self.beacon.receive(timer["owner_key"], io.StringIO(), timer["id"])
        self.clock.value = until + 1
        self.beacon.tick(send=True)
        self.assertEqual(self.status(timer["id"]), ("expired", ["received"]))
        self.assertEqual(len(self.host.sent), 1)

    def test_receipt_is_written_only_after_complete_output_and_binding_recheck(self):
        timer = self.set_timer(after="10s")["timer"]
        self.clock.value += 11
        self.beacon.tick(send=True)
        class BrokenOutput:
            def write(self, _value):
                return 0
            def flush(self):
                self.fail("flush must not run after a short write")
        with patch.dict("os.environ", self.env), self.assertRaisesRegex(ValueError, "incomplete"):
            self.beacon.receive(timer["owner_key"], BrokenOutput(), timer["id"])
        self.assertEqual(self.status(timer["id"])[1], ["submitted"])

        class ChangedBinding(Host):
            def __init__(self, root):
                super().__init__(root)
                self.checks = 0
            def binding(self, target):
                self.checks += 1
                if self.checks == 2:
                    raise ValueError("generation changed")
        changed = ChangedBinding(self.root)
        self.app.cmux = changed
        with patch.dict("os.environ", self.env), self.assertRaisesRegex(ValueError, "generation changed"):
            self.beacon.receive(timer["owner_key"], io.StringIO(), timer["id"])
        self.assertEqual(self.status(timer["id"])[1], ["submitted"])

    def test_cancel_blocks_old_receive_and_generation_change_holds_delivery(self):
        timer = self.set_timer(after="10s")["timer"]
        self.clock.value += 11
        self.beacon.tick(send=True)
        self.beacon.cancel(timer["owner_key"], timer["id"])
        with patch.dict("os.environ", self.env), self.assertRaisesRegex(ValueError, "canceled"):
            self.beacon.receive(timer["owner_key"], io.StringIO(), timer["id"])

        stale = self.set_timer(after="10s")["timer"]
        self.clock.value += 11
        self.host.hold = "Session generation changed"
        self.beacon.tick(send=True)
        self.assertEqual(self.status(stale["id"]), ("active", ["held"]))
        self.assertEqual(len(self.host.sent), 1)

    def test_native_worktree_change_holds_pre_dispatch(self):
        timer = self.set_timer(after="10s")["timer"]
        self.clock.value += 11
        hook_path = self.root / "claude-hook-sessions.json"
        store = json.loads(hook_path.read_text())
        store["sessions"][self.target["session"]]["cwd"] = str(self.root.parent)
        hf.atomic_json(hook_path, store)
        self.beacon.tick(send=True)
        self.assertEqual(self.status(timer["id"]), ("active", ["held"]))
        self.assertEqual(self.host.sent, [])

    def test_final_guard_race_is_held_before_paste_and_retries_safely(self):
        timer = self.set_timer(after="10s")["timer"]
        self.clock.value += 11
        cmux = hf.Cmux(hook_directory=self.root)
        checks = [0]
        pastes = []
        def guard(_target):
            checks[0] += 1
            if checks[0] == 2:
                raise ValueError("Human draft appeared before final submit")
        cmux.binding = guard
        cmux.command = lambda *args: pastes.append(args) or {"submitted": True}
        self.app.cmux = cmux
        self.beacon.tick(send=True)
        self.assertEqual(self.status(timer["id"]), ("active", ["held"]))
        self.assertEqual(pastes, [])
        cmux.binding = lambda _target: True
        self.beacon.tick(send=True)
        self.assertEqual(self.status(timer["id"])[1], ["submitted"])
        self.assertEqual(pastes[0][0], "paste")

    def test_exact_owner_inference_refuses_mismatched_harness_and_surface(self):
        with patch.dict("os.environ", {**self.env, "CMUX_SURFACE_ID": uid()}):
            with self.assertRaisesRegex(ValueError, "differs from its enrolled"):
                self.beacon.owner()
        with patch.dict("os.environ", {"CMUX_AGENT_LAUNCH_KIND": "codex", "CMUX_SURFACE_ID": self.target["surface"]}, clear=True):
            with self.assertRaisesRegex(ValueError, "Missing verified CODEX_THREAD_ID"):
                self.beacon.owner()

    def test_opencode_timer_owner_requires_exact_seeded_session_and_workspace(self):
        target = {**self.target, "session": "ses_TemporalFixture42", "harness": "opencode",
                  "api_url": "http://127.0.0.1:4096", "password_file": str(self.root / "password"),
                  "expected_server_pid": 12345, "session_directory": str(self.root),
                  "worktree_directory": str(self.root), "parent_task_id": "task:temporal-fixture"}
        hf.atomic_json(self.root / "config.json", {"version": 1, "targets": [target], "coordinator": None,
            "remind_after": 300, "escalate_after": 900, "cooldown": 600, "interval": 5})
        hf.atomic_json(self.root / "opencode-hook-sessions.json", {"sessions": {target["session"]: {
            "surfaceId": target["surface"], "workspaceId": target["workspace"],
            "startedAt": target["started_at"], "cwd": str(self.root)}}})
        env = {"CMUX_AGENT_LAUNCH_KIND": "opencode", "CMUX_SURFACE_ID": target["surface"],
               "CMUX_WORKSPACE_ID": target["workspace"]}
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaisesRegex(ValueError, "OPENCODE_SESSION_ID"):
                self.beacon.owner(verify=False)
        env["OPENCODE_SESSION_ID"] = target["session"]
        with patch.dict("os.environ", env, clear=True):
            owner, _key, _worktree = self.beacon.owner(verify=False)
        self.assertEqual(owner["session"], target["session"])

    def test_opencode_cli_set_allows_busy_identity_but_delivery_stays_strict(self):
        target = {**self.target, "session": "ses_TemporalBusyFixture42", "harness": "opencode",
                  "api_url": "http://127.0.0.1:4096", "password_file": str(self.root / "password"),
                  "expected_server_pid": 12345, "session_directory": str(self.root),
                  "worktree_directory": str(self.root), "parent_task_id": "task:temporal-busy-fixture",
                  "delivery_policy": "native_empty_composer"}
        hf.atomic_json(self.root / "config.json", {"version": 1, "targets": [target], "coordinator": None,
            "remind_after": 300, "escalate_after": 900, "cooldown": 600, "interval": 5})
        hf.atomic_json(self.root / "opencode-hook-sessions.json", {"sessions": {target["session"]: {
            "surfaceId": target["surface"], "workspaceId": target["workspace"],
            "startedAt": target["started_at"], "cwd": str(self.root), "agentLifecycle": "running"}}})
        current = {"truncated": False, "owner_availability": {"agent_sessions": "available"}, "items": [{
            "placement": {"kind": "local"},
            "projections": [{"panel_id": target["surface"], "workspace_id": target["workspace"]}],
            "agents": [{"kind": "opencode", "session_id": target["session"], "state": "working"}]}]}
        cmux = hf.Cmux(hook_directory=self.root)

        def command(*args):
            if args == ("current", "--limit", "200"):
                return current
            if len(args) == 3 and args[0] == "rpc" and args[1] == "agent.message.settings":
                return {"messages_enabled": True, "receiving": True, "workspace_receiving": True}
            raise AssertionError(f"Unexpected CMUX command: {args!r}")

        cmux.command = command
        env = {"CMUX_AGENT_LAUNCH_KIND": "opencode", "OPENCODE_SESSION_ID": target["session"],
               "CMUX_SURFACE_ID": target["surface"], "CMUX_WORKSPACE_ID": target["workspace"]}
        args = ["--state-dir", str(self.root), "temporal", "set", "--after", "2m",
                "--message", "Resume the bounded task"]

        with patch.object(hf, "Cmux", return_value=cmux), \
             patch.object(hf, "opencode_identity_binding", return_value=object()), \
             patch.object(hf, "opencode_binding", side_effect=ValueError("session_busy")), \
             patch.dict("os.environ", env, clear=True):
            result = hf.main(args)

            # The caller must still match the exact enrolled session, surface, and workspace.
            for key, value in (("OPENCODE_SESSION_ID", "ses_WrongOwnerFixture42"),
                               ("CMUX_SURFACE_ID", uid()), ("CMUX_WORKSPACE_ID", uid())):
                wrong_env = {**env, key: value}
                with patch.dict("os.environ", wrong_env, clear=True), self.assertRaises(ValueError):
                    hf.main(args)

            # Native generation and API identity remain mandatory for creation.
            hf.atomic_json(self.root / "opencode-hook-sessions.json", {"sessions": {target["session"]: {
                "surfaceId": target["surface"], "workspaceId": target["workspace"],
                "startedAt": target["started_at"] + 1, "cwd": str(self.root), "agentLifecycle": "running"}}})
            with self.assertRaisesRegex(ValueError, "generation changed"):
                hf.main(args)
            hf.atomic_json(self.root / "opencode-hook-sessions.json", {"sessions": {target["session"]: {
                "surfaceId": target["surface"], "workspaceId": target["workspace"],
                "startedAt": target["started_at"], "cwd": str(self.root), "agentLifecycle": "running"}}})
            with patch.object(hf, "opencode_identity_binding", side_effect=ValueError("session_id_mismatch")), \
                 self.assertRaisesRegex(ValueError, "session_id_mismatch"):
                hf.main(args)

            self.assertEqual(result["timer"]["session"], target["session"])
            self.assertEqual(result["timer"]["started_at"], target["started_at"])
            self.assertEqual(len(self.beacon.list(result["timer"]["owner_key"])["timers"]), 1)

            # A busy API session remains a hard hold when the due timer reaches delivery.
            self.beacon.app.cmux = cmux
            self.clock.value += 121
            with patch.object(cmux, "submit", wraps=cmux.submit) as submit:
                report = self.beacon.tick(send=True)
            self.assertEqual(report["timers"][0]["status"], "held")
            self.assertIn("session_busy", report["timers"][0]["detail"])
            submit.assert_not_called()
            self.assertEqual(self.status(result["timer"]["id"])[1], ["held"])

    def test_concurrent_timer_mutation_fails_with_actionable_retry(self):
        stream = (self.root / "timers.lock").open("a")
        hf.fcntl.flock(stream, hf.fcntl.LOCK_EX | hf.fcntl.LOCK_NB)
        try:
            with self.assertRaisesRegex(ValueError, "retry shortly"):
                self.beacon.list(":")
        finally:
            hf.fcntl.flock(stream, hf.fcntl.LOCK_UN)
            stream.close()

    def test_cli_set_list_inspect_cancel_and_receive_use_the_exact_owner(self):
        class CliHost(Host):
            pass
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(hf, "Cmux", return_value=CliHost(self.root)), patch.dict("os.environ", self.env):
            with redirect_stdout(output), redirect_stderr(errors):
                set_result = hf.main(["--state-dir", str(self.root), "temporal", "set", "--after", "10s",
                                      "--request-id", "cli-request", "--message", "Resume the issue"])
                timer_id = set_result["timer"]["id"]
                self.assertIn("due_at_local", set_result["timer"])
                self.assertEqual(set_result["delivery_capability"]["transport"], "guarded_cmux")
                self.assertEqual(len(hf.main(["--state-dir", str(self.root), "temporal", "list"])["timers"]), 1)
                self.assertEqual(hf.main(["--state-dir", str(self.root), "temporal", "inspect", timer_id])["timer"]["id"], timer_id)
                self.beacon.clock.value += 11
                self.beacon.tick(send=True)
                hf.main(["--state-dir", str(self.root), "temporal", "receive", "--timer-id", timer_id])
                self.assertEqual(json.loads(output.getvalue())["continuation"], "Resume the issue")
                self.assertIn('"received"', errors.getvalue())
                canceled = hf.main(["--state-dir", str(self.root), "temporal", "cancel", timer_id])
                self.assertEqual(canceled["status"], "completed")

    def test_installer_publishes_three_owned_commands_and_immutable_skills(self):
        import install
        app_dir = Path(__file__).resolve().parents[1]
        beacon_skill = app_dir / install.SKILL_NAME
        agent_notify_skill = app_dir / install.AGENT_NOTIFY_SKILL_NAME
        self.assertTrue(beacon_skill.is_file())
        self.assertTrue(agent_notify_skill.is_file())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = install.install(root / "runtime", root / "bin")
            hailing = Path(result["command"])
            temporal = Path(result["temporal_beacon_command"])
            agent_notify = Path(result["agent_notify_command"])
            self.assertIn("usage:", subprocess.check_output([str(temporal), "--help"], text=True))
            self.assertEqual(Path(result["skill"]).read_bytes(), beacon_skill.read_bytes())
            self.assertEqual(Path(result["skill"]).stat().st_mode & 0o222, 0)
            self.assertEqual(Path(result["agent_notify_skill"]).read_bytes(), agent_notify_skill.read_bytes())
            self.assertEqual(Path(result["agent_notify_skill"]).stat().st_mode & 0o222, 0)
            self.assertEqual(subprocess.check_output([str(agent_notify), "guide", "--print"], text=True),
                             (Path(result["runtime"]) / "agent-notify-guide.md").read_text())
            self.assertTrue(hailing.exists())
            state = root / "state"
            state.mkdir()
            hf.atomic_json(state / "config.json", {"version": 1, "targets": [], "coordinator": None,
                "remind_after": 300, "escalate_after": 900, "cooldown": 600, "interval": 30})
            doctor = subprocess.run([str(temporal), "--state-dir", str(root / "state"), "doctor"],
                                    capture_output=True, text=True, check=True)
            self.assertFalse(json.loads(doctor.stdout)["healthy"])
            install.install(root / "runtime", root / "bin", uninstall=True)
            self.assertFalse(hailing.exists())
            self.assertFalse(temporal.exists())
            self.assertFalse(agent_notify.exists())
            self.assertTrue(Path(result["runtime"]).is_dir())


if __name__ == "__main__":
    unittest.main()
