import json
import unittest
from unittest.mock import patch
import uuid

import test_preferences as fixtures
from test_preferences import mp


class OrchestratorRegistryTests(unittest.TestCase):
    setUp = fixtures.PreferencesTest.setUp
    args = fixtures.PreferencesTest.args
    call = fixtures.PreferencesTest.call
    activate = fixtures.PreferencesTest.activate
    inventory = fixtures.PreferencesTest.inventory

    def pending_receipt(self):
        other = dict(self.recipient, surface=str(uuid.uuid4()), session="other")
        self.app.save_state("orchestrators", {"lane": self.recipient, "other": other})
        successor = dict(self.recipient, surface=str(uuid.uuid4()), session="successor")
        result = self.app._commit_handoff_owner_receipt(uuid.uuid4().hex, "lane", self.recipient, successor)
        return result["receipt"]["handoff_id"], result["receipt"], successor, other

    def test_legacy_plain_map_is_read_and_upgraded_on_its_next_write(self):
        path = self.state_dir / "orchestrators.json"
        legacy = {"lane": self.recipient}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertEqual(self.app.state("orchestrators", {}), legacy)

        other = dict(self.recipient, surface=str(uuid.uuid4()), session="second")
        self.app.save_state("orchestrators", {**legacy, "other": other})
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(document), {"schema_version", "protocol", "recipients", "handoff_receipts"})
        self.assertEqual(document["schema_version"], mp.orchestrator_registry.REGISTRY_SCHEMA_VERSION)
        self.assertEqual(document["protocol"], mp.orchestrator_registry.REGISTRY_PROTOCOL)
        self.assertEqual(document["recipients"], {**legacy, "other": other})
        self.assertEqual(document["handoff_receipts"], {})

    def test_successor_resolver_returns_the_same_exact_seat_from_either_format(self):
        path = self.state_dir / "orchestrators.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        recipients = {"lane": self.recipient}
        path.write_text(json.dumps(recipients), encoding="utf-8")
        args = self.args("resolve-successor", "--seat", "lane")
        legacy = mp.resolve_successor_choice(self.app, self.app.config(), args)

        path.write_text(json.dumps(mp.orchestrator_registry.encode_registry(recipients)), encoding="utf-8")
        versioned = mp.resolve_successor_choice(self.app, self.app.config(), args)

        self.assertEqual(legacy, versioned)
        self.assertEqual(legacy["seat"], {
            "name": "lane",
            "harness": self.recipient["harness"],
            "session_id": self.recipient["session"],
            "workspace_id": self.recipient["workspace"],
            "panel_id": self.recipient["surface"],
        })

    def test_legacy_and_versioned_registry_give_the_same_passive_doctor_state(self):
        self.activate("Claude-Codex")
        recipients = {"lane": self.recipient}
        active = mp.selection(self.app.config())
        self.app.save_state("adoptions", {"lane": {
            "recipient": self.recipient, "selection": active["selection"], "profile": active["profile"],
        }})
        caller = {key: self.recipient[key] for key in ("harness", "session", "workspace", "surface")}
        path = self.state_dir / "orchestrators.json"
        path.write_text(json.dumps(recipients), encoding="utf-8")
        legacy_before = {item.name: item.read_bytes() for item in self.state_dir.iterdir() if item.is_file()}
        with patch.object(mp, "locked", side_effect=AssertionError("doctor must not lock")), \
                patch.object(mp, "run_json", side_effect=AssertionError("doctor must not query native state")):
            legacy_report = mp.read_only_diagnostic_report(self.app, caller)
        legacy_after = {item.name: item.read_bytes() for item in self.state_dir.iterdir() if item.is_file()}
        self.assertEqual(legacy_before, legacy_after)

        path.write_text(json.dumps(mp.orchestrator_registry.encode_registry(recipients)), encoding="utf-8")
        versioned_before = {item.name: item.read_bytes() for item in self.state_dir.iterdir() if item.is_file()}
        with patch.object(mp, "locked", side_effect=AssertionError("doctor must not lock")), \
                patch.object(mp, "run_json", side_effect=AssertionError("doctor must not query native state")):
            versioned_report = mp.read_only_diagnostic_report(self.app, caller)
        versioned_after = {item.name: item.read_bytes() for item in self.state_dir.iterdir() if item.is_file()}

        self.assertEqual(legacy_report, versioned_report)
        self.assertEqual(versioned_before, versioned_after)
        self.assertEqual(versioned_report["schema_version"], 1)
        self.assertEqual(versioned_report["checks"]["registration"], "ready")
        self.assertEqual(versioned_report["checks"]["configuration"], "ready")

    def test_owner_update_and_pending_receipt_share_one_versioned_atomic_file(self):
        handoff_id, receipt, successor, _other = self.pending_receipt()
        path = self.state_dir / "orchestrators.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["recipients"]["lane"], successor)
        self.assertEqual(document["handoff_receipts"][handoff_id], receipt)
        self.assertEqual(receipt["state"], "pending")
        before = path.read_bytes()

        reused = self.app._commit_handoff_owner_receipt(
            handoff_id, "lane", self.recipient, successor)
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["receipt"], receipt)
        self.assertEqual(path.read_bytes(), before)

        changed_successor = dict(successor, session="other-successor")
        with self.assertRaisesRegex(mp.orchestrator_registry.RegistryError, "different owner change"):
            self.app._commit_handoff_owner_receipt(
                handoff_id, "lane", self.recipient, changed_successor)
        self.assertEqual(path.read_bytes(), before)

        recipients = self.app.state("orchestrators", {})
        recipients["other"] = dict(self.recipient, surface=str(uuid.uuid4()), session="third")
        self.app.save_state("orchestrators", recipients)
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["handoff_receipts"][handoff_id], receipt)
        self.assertEqual(document["recipients"]["lane"], successor)

    def test_missing_receipt_is_uncertain_and_does_not_apply_an_owner_change(self):
        possible_successor = dict(self.recipient, surface=str(uuid.uuid4()), session="successor")
        self.app.save_state("orchestrators", {"lane": possible_successor})
        path = self.state_dir / "orchestrators.json"
        before = path.read_bytes()
        handoff_id = uuid.uuid4().hex
        self.assertIsNone(mp.orchestrator_registry.lookup_receipt(json.loads(before), handoff_id))
        with self.assertRaisesRegex(mp.orchestrator_registry.RegistryError, "missing; inspect the owner state"):
            self.app._acknowledge_handoff_receipt(handoff_id, "a" * 64)
        with self.assertRaisesRegex(mp.orchestrator_registry.RegistryError, "Registered owner changed"):
            self.app._commit_handoff_owner_receipt(
                handoff_id, "lane", self.recipient, possible_successor)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.app.state("orchestrators", {})["lane"], possible_successor)

    def test_enrollment_unenrollment_and_legacy_transfer_cannot_drop_pending_receipt(self):
        handoff_id, receipt, successor, other = self.pending_receipt()
        path = self.state_dir / "orchestrators.json"
        before = path.read_bytes()
        self.assertEqual(mp.enroll_recipient(self.app, "lane", successor)["enrolled"], "lane")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["handoff_receipts"][handoff_id], receipt)

        changed = dict(successor, session="replacement")
        with self.assertRaisesRegex(mp.orchestrator_registry.RegistryError, "unconsumed handoff receipt"):
            mp.enroll_recipient(self.app, "lane", changed)
        self.assertEqual(path.read_bytes(), before)

        with self.assertRaisesRegex(mp.orchestrator_registry.RegistryError, "unconsumed handoff receipt"):
            self.call("unenroll", "lane")
        self.assertEqual(self.app.state("orchestrators", {})["lane"], successor)
        self.assertEqual(self.app.state("orchestrators", {})["other"], other)

        legacy_id = uuid.uuid4().hex
        legacy = {
            "id": legacy_id,
            "name": "lane",
            "status": "ready",
            "outgoing": successor,
            "choice": {"harness": self.recipient["harness"]},
            "successor_surface": changed["surface"],
            "successor_session": changed["session"],
        }
        self.app.save_state(f"handoffs/{legacy_id}", legacy)
        args = self.args("handoff-transfer", legacy_id, "--outgoing-paused")
        with patch.object(mp.handoff_adapter.shutil, "which", return_value=None), \
                patch.object(mp.App, "current", return_value=self.inventory()), \
                patch.object(mp.App, "live", return_value="active"), \
                patch.object(mp, "run_json", return_value={"caller": {"surface_id": successor["surface"]}}):
            with self.assertRaisesRegex(mp.orchestrator_registry.RegistryError, "unconsumed handoff receipt"):
                mp.execute(args)
        self.assertEqual(self.app.state("orchestrators", {})["lane"], successor)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["handoff_receipts"][handoff_id], receipt)
        self.assertEqual(path.read_bytes(), before)

    def test_consumed_receipt_allows_a_later_owner_change_and_remains_recorded(self):
        handoff_id, receipt, successor, other = self.pending_receipt()
        consumed = self.app._acknowledge_handoff_receipt(handoff_id, receipt["request_digest"])
        self.assertFalse(consumed["reused"])
        self.assertEqual(consumed["receipt"]["state"], "consumed")
        acknowledged = self.app._acknowledge_handoff_receipt(handoff_id, receipt["request_digest"])
        self.assertTrue(acknowledged["reused"])
        next_owner = dict(successor, surface=str(uuid.uuid4()), session="later-owner")
        recipients = self.app.state("orchestrators", {})
        recipients["lane"] = next_owner
        self.app.save_state("orchestrators", recipients)
        document = json.loads((self.state_dir / "orchestrators.json").read_text(encoding="utf-8"))
        self.assertEqual(document["recipients"]["lane"], next_owner)
        self.assertEqual(document["handoff_receipts"][handoff_id]["state"], "consumed")
        self.assertEqual(document["recipients"]["other"], other)


if __name__ == "__main__":
    unittest.main()
