"""Focused tests for bounded Pattern Buffer reliability history."""

from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import pattern_buffer_reliability as reliability


UTC = datetime.timezone.utc


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pattern-buffer-reliability-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.state_home = self.base / "state"
        self.root = self.base / "worktree"
        self.root.mkdir()
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_home)})
        self.env.start()
        self.addCleanup(self.env.stop)

    @staticmethod
    def at(day, hour=12, second=0):
        return datetime.datetime(2026, 11, day, hour, 0, second, tzinfo=UTC)

    def emit(self, event, **kwargs):
        return reliability.emit(self.root, event, **kwargs)

    def test_private_daily_jsonl_filters_unapproved_text_and_reports_identity(self):
        with mock.patch.object(reliability, "_utc_now", return_value=self.at(2, hour=12)):
            result = self.emit(
                "standing.injection_emitted",
                harness="codex",
                session="session-123",
                enrollment_id="enroll-456",
                result="emitted",
                correlation_id="alarm-789",
                details={
                    "role": "orchestrator",
                    "source": "session",
                    "source_sha256": "a" * 64,
                    "bytes": 512,
                    "usage_percent": 94.5,
                    "generation_uncertain": False,
                    "reviewed_at": "2026-11-02T12:00:00Z",
                    "note_body": "PRIVATE NOTE BODY MUST NOT BE STORED",
                    "prompt": "TOKEN_DO_NOT_LEAK",
                },
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["dropped_detail_count"], 2)
        state_dir, worktree_id = reliability._state_path(self.root)
        files = list(state_dir.glob("*.jsonl"))
        self.assertEqual([path.name for path in files], [result["timestamp"][:10] + ".jsonl"])
        raw = files[0].read_text()
        self.assertNotIn("PRIVATE NOTE BODY MUST NOT BE STORED", raw)
        self.assertNotIn("TOKEN_DO_NOT_LEAK", raw)
        self.assertNotIn(str(self.root), raw)
        record = json.loads(raw)
        self.assertEqual(record["worktree_id"], worktree_id)
        self.assertEqual(record["session"], "session-123")
        self.assertEqual(record["enrollment_id"], "enroll-456")
        self.assertEqual(record["details"]["role"], "orchestrator")
        self.assertEqual(record["details"]["source_sha256"], "a" * 64)
        self.assertEqual(record["details"]["bytes"], 512)
        self.assertEqual(len(record["runtime_fingerprint"]), 64)
        with mock.patch.object(reliability, "_utc_now", return_value=self.at(2, hour=13)):
            report = reliability.report(self.root, since="1d")
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["emitted"], 1)
        self.assertEqual(report["events"][0]["event_id"], result["event_id"])
        self.assertEqual(report["events"][0]["details"]["usage_percent"], 94.5)

    def test_report_keeps_delivery_states_distinct_and_durations_correlated(self):
        correlation = "pressure-episode-1"
        with mock.patch.object(reliability, "_utc_now", side_effect=[
                self.at(2, second=0), self.at(2, second=2), self.at(2, second=3),
                self.at(2, second=4), self.at(2, second=8)]):
            self.assertTrue(self.emit("delivery.emitted", correlation_id=correlation)["ok"])
            self.assertTrue(self.emit("delivery.submitted", correlation_id=correlation,
                                      result="submitted")["ok"])
            self.assertTrue(self.emit("delivery.submitted", correlation_id=correlation,
                                      result="submitted")["ok"])
            self.assertTrue(self.emit("receipt.received", correlation_id=correlation,
                                      result="received")["ok"])
            self.assertTrue(self.emit("note.published", correlation_id=correlation,
                                      result="published")["ok"])
        with mock.patch.object(reliability, "_utc_now", return_value=self.at(2, hour=13)):
            report = reliability.report(self.root, since="1d")
        summary = report["summary"]
        self.assertEqual(summary["emitted"], 1)
        self.assertEqual(summary["submitted"], 2)
        self.assertEqual(summary["received"], 1)
        self.assertEqual(summary["published"], 1)
        self.assertEqual(summary["unique_incidents"], 1)
        self.assertEqual(summary["duplicate_events"], 1)
        self.assertEqual(summary["durations_seconds"]["emission_to_receipt"], [4.0])
        self.assertNotIn("receipt_not_observed", {item["code"] for item in report["unknowns"]})

    def test_unobserved_receipt_is_unknown_and_unrelated_events_have_no_duration(self):
        with mock.patch.object(reliability, "_utc_now", side_effect=[self.at(2), self.at(2, second=5)]):
            self.emit("delivery.emitted", correlation_id="episode-2")
            self.emit("receipt.received")
        with mock.patch.object(reliability, "_utc_now", return_value=self.at(2, hour=13)):
            report = reliability.report(self.root, since="1d")
        codes = {item["code"] for item in report["unknowns"]}
        self.assertIn("receipt_not_observed", codes)
        self.assertIn("events_without_correlation", codes)
        self.assertNotIn("emission_to_receipt", report["summary"]["durations_seconds"])
        self.assertEqual(report["summary"]["received"], 1)

    def test_retention_and_size_cap_prune_only_this_roots_daily_files(self):
        other_root = self.base / "other-worktree"
        other_root.mkdir()
        with mock.patch.object(reliability, "_utc_now", side_effect=[
                self.at(1), self.at(2), self.at(3), self.at(3)]):
            self.emit("note.draft_changed", correlation_id="note-1")
            self.emit("note.published", correlation_id="note-2")
            reliability.emit(other_root, "note.published", correlation_id="other-note")
            state_dir, _ = reliability._state_path(self.root)
            other_dir, _ = reliability._state_path(other_root)
            self.assertTrue((state_dir / "2026-11-01.jsonl").exists())
            configured = reliability.configure(self.root, retention_days=2, max_bytes=100000)
            self.assertTrue(configured["ok"])
        self.assertFalse((state_dir / "2026-11-01.jsonl").exists())
        self.assertTrue((state_dir / "2026-11-02.jsonl").exists())
        self.assertTrue((other_dir / "2026-11-03.jsonl").exists())
        with mock.patch.object(reliability, "_utc_now", return_value=self.at(3)):
            capped = reliability.configure(self.root, retention_days=2, max_bytes=1)
            self.assertTrue(capped["ok"])
            self.assertEqual(capped["used_bytes"], 0)
            self.assertEqual(list(state_dir.glob("*.jsonl")), [])
            rejected = self.emit("note.published", correlation_id="too-large-for-cap")
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"]["code"], "capacity_exceeded")
        self.assertTrue((other_dir / "2026-11-03.jsonl").exists())

    def test_configure_preserves_unspecified_limit(self):
        fresh = reliability.configure(self.root)
        self.assertEqual(fresh["retention_days"], 30)
        self.assertEqual(fresh["max_bytes"], 20 * 1024 * 1024)
        initial = reliability.configure(self.root, retention_days=8, max_bytes=100000)
        self.assertTrue(initial["ok"])
        changed_cap = reliability.configure(self.root, max_bytes=50000)
        self.assertEqual(changed_cap["retention_days"], 8)
        self.assertEqual(changed_cap["max_bytes"], 50000)
        changed_retention = reliability.configure(self.root, retention_days=4)
        self.assertEqual(changed_retention["retention_days"], 4)
        self.assertEqual(changed_retention["max_bytes"], 50000)

    def test_report_uses_armed_stage_and_pairs_cross_correlation_by_exact_binding(self):
        harness, session, enrollment_id = "codex", "primary-session", "durable-enrollment"
        shared = {"harness": harness, "session": session, "enrollment_id": enrollment_id}
        with mock.patch.object(reliability, "_utc_now", side_effect=[
                self.at(2, second=0), self.at(2, second=0), self.at(2, second=1),
                self.at(2, second=2), self.at(2, second=5), self.at(2, second=6),
                self.at(2, second=20), self.at(2, second=30), self.at(2, second=31)]):
            self.emit("reminder.armed", **shared, result="armed", correlation_id="idle-timer")
            self.emit("reminder.armed", **shared, result="uncertain", correlation_id="uncertain-idle-timer")
            self.emit("reminder.eligible", **shared, result="eligible", correlation_id="pressure-eligible")
            self.emit("delivery.emitted", **shared, result="emitted", correlation_id="pressure-event")
            self.emit("note.published", **shared, result="published", correlation_id="note-hash")
            self.emit("note.published", harness=harness, session="other-session",
                      enrollment_id=enrollment_id, result="published", correlation_id="other-note")
            self.emit("compaction.observed", **shared, result="observed", correlation_id="compact-event")
            self.emit("recovery.observed", **shared, result="observed", correlation_id="recovery-event",
                      details={"reviewed_at": "2026-11-02T12:00:05Z", "generation_uncertain": True})
            self.emit("recovery.observed", **shared, result="observed", correlation_id="recovery-unknown")
        with mock.patch.object(reliability, "_utc_now", return_value=self.at(2, hour=13)):
            report = reliability.report(self.root, since="1d")
        summary = report["summary"]
        self.assertEqual(summary["armed"], 1)
        self.assertEqual(summary["timer_arm_attempts"], 2)
        self.assertEqual(summary["arm_outcomes"], {"armed": 1, "uncertain": 1})
        self.assertEqual(summary["eligible"], 1)
        self.assertEqual(summary["emitted"], 1)
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["received"], 0)
        self.assertEqual(summary["published"], 2)
        self.assertEqual(summary["durations_seconds"]["warning_emission_to_publication"], [3.0])
        self.assertEqual(summary["durations_seconds"]["publication_to_compaction"], [15.0])
        self.assertEqual(summary["note_age_at_recovery_seconds"], [25.0])
        self.assertIn({"code": "recovery_note_age_unknown", "count": 1}, report["unknowns"])
        self.assertNotIn("unrecognized_event_types", {item["code"] for item in report["unknowns"]})

    def test_enrollment_lifecycle_events_are_recognized(self):
        names = ("enrollment.session-enroll", "enrollment.assign",
                 "enrollment.enrollment-release", "enrollment.enrollment-adopt")
        with mock.patch.object(reliability, "_utc_now", side_effect=[
                self.at(2, second=1), self.at(2, second=2),
                self.at(2, second=3), self.at(2, second=4)]):
            for index, name in enumerate(names):
                self.emit(name, harness="claude", session="owner-session",
                          enrollment_id="enrollment-id", result="active",
                          correlation_id=f"enrollment-action-{index}")
        with mock.patch.object(reliability, "_utc_now", return_value=self.at(2, hour=13)):
            report = reliability.report(self.root, since="1d")
        self.assertEqual(set(report["summary"]["by_event"]), set(names))
        self.assertNotIn("unrecognized_event_types", {item["code"] for item in report["unknowns"]})

    def test_concurrent_appends_are_complete_unique_jsonl_records(self):
        def append(index):
            return self.emit("delivery.held", harness="claude", session="shared-session",
                             correlation_id="shared-incident", result="held",
                             details={"attempt": index + 1})

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(append, range(60)))
        self.assertTrue(all(item["ok"] for item in results))
        state_dir, _ = reliability._state_path(self.root)
        path = next(state_dir.glob("*.jsonl"))
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(records), 60)
        self.assertEqual(len({record["event_id"] for record in records}), 60)
        self.assertEqual(len({record["details"]["attempt"] for record in records}), 60)
        self.assertEqual(reliability.report(self.root, since="1d")["summary"]["event_count"], 60)

    def test_symlink_history_file_is_refused_without_following_it(self):
        configured = reliability.configure(self.root)
        self.assertTrue(configured["ok"])
        state_dir, _ = reliability._state_path(self.root)
        outside = self.base / "outside.jsonl"
        outside.write_text("do not modify\n")
        today = reliability._utc_now().date().isoformat()
        (state_dir / (today + ".jsonl")).symlink_to(outside)
        result = self.emit("context.missing_alarm", correlation_id="missing-1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "symlink_refused")
        self.assertEqual(outside.read_text(), "do not modify\n")
        report = reliability.report(self.root)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"]["code"], "symlink_refused")

    def test_malformed_lines_are_counted_without_echoing_contents(self):
        self.assertTrue(self.emit("context.recovered", correlation_id="recover-1")["ok"])
        state_dir, _ = reliability._state_path(self.root)
        path = next(state_dir.glob("*.jsonl"))
        with path.open("a") as handle:
            handle.write("{broken SECRET_BODY\n")
            handle.write(json.dumps({"event": "bad", "note": "DO_NOT_REPORT_THIS"}) + "\n")
        report = reliability.report(self.root, since="1d")
        self.assertTrue(report["ok"])
        self.assertEqual(report["coverage"]["malformed_lines"], 2)
        self.assertIn({"code": "malformed_events", "count": 2}, report["unknowns"])
        self.assertNotIn("SECRET_BODY", json.dumps(report))
        self.assertNotIn("DO_NOT_REPORT_THIS", json.dumps(report))

    def test_invalid_arguments_return_structured_errors_without_state_writes(self):
        result = self.emit("invalid event name", session="SESSION BODY")
        self.assertFalse(result["ok"])
        self.assertIn("error", result)
        self.assertFalse(self.state_home.exists())
        invalid = reliability.configure(self.root, retention_days=0)
        self.assertFalse(invalid["ok"])
        self.assertFalse(self.state_home.exists())


if __name__ == "__main__":
    unittest.main()
