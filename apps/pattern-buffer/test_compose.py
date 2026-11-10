import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "apps/pattern-buffer/pattern_buffer.py"
FIXTURES = REPO / ".context-injector/.h/wp3-compose-20261110/fixtures"

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("pattern_buffer_compose_test", SCRIPT)
pattern_buffer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pattern_buffer
SPEC.loader.exec_module(pattern_buffer)


class ComposeCliTests(unittest.TestCase):
    def setUp(self):
        self.root = FIXTURES / self._testMethodName / "worktree"
        self.base = self.root.parent
        if self.base.exists():
            shutil.rmtree(self.base)
        self.root.mkdir(parents=True)
        self.home = self.base / "home"
        for name in ("config", "state", "data", "cache"):
            (self.home / ".local" / name).mkdir(parents=True, exist_ok=True)
        self.env = {
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".local/config"),
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "XDG_DATA_HOME": str(self.home / ".local/data"),
            "XDG_CACHE_HOME": str(self.home / ".local/cache"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.home / ".gitconfig"),
            "LC_ALL": "C",
        }
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, env=self.env)
        self.rules = self.base / "rules.md"
        self.mission = self.base / "mission.md"
        self.write_inputs()

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def write_inputs(self, mission_extra=""):
        self.rules.write_text(
            "# shared\n\nRULE SHARED\n\n"
            "# implementer\n\nRULE IMPLEMENTER\n\n"
            "# orchestrator\n\nRULE ORCHESTRATOR\n\n"
            "# confirmation\n\nRULE CONFIRMATION\n",
            encoding="utf-8",
        )
        self.mission.write_text(
            "# shared\n\nMISSION SHARED\n\n"
            "# implementer\n\nMISSION IMPLEMENTER\n" + mission_extra,
            encoding="utf-8",
        )

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--cwd", str(self.root), *map(str, args)],
            capture_output=True,
            text=True,
            env=self.env,
        )

    def compose(self, output, *extra):
        return self.cli("compose", "--rules", self.rules, "--mission", self.mission,
                        "--output", output, *extra)

    def snapshot(self, path):
        return {str(item.relative_to(path)): item.read_bytes() if item.is_file() else None
                for item in sorted(path.rglob("*"))}

    def register(self, context_text, default_role="orchestrator"):
        folder = self.root / ".context-injector"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "context.md").write_text(context_text, encoding="utf-8")
        config_path = self.root / ".git/context-injector/config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"version": 1, "root": str(self.root),
                                           "default_role": default_role, "enabled": True}),
                               encoding="utf-8")

    @staticmethod
    def json_result(result):
        return json.loads(result.stdout)

    def test_section_order_hashes_and_exact_validator_budgets(self):
        output = self.base / "composed.md"
        result = self.compose(output, "--session", "fixture-session")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.json_result(result)
        self.assertEqual(report["minimum_headroom_bytes"], 1024)
        composed = output.read_bytes()
        self.assertEqual(report["output"]["sha256"], hashlib.sha256(composed).hexdigest())
        self.assertEqual(report["inputs"]["rules"]["sha256"], hashlib.sha256(self.rules.read_bytes()).hexdigest())
        self.assertEqual(report["inputs"]["mission"]["sha256"], hashlib.sha256(self.mission.read_bytes()).hexdigest())
        text = composed.decode("utf-8")
        self.assertLess(text.index("MISSION SHARED"), text.index("RULE SHARED"))
        self.assertLess(text.index("MISSION IMPLEMENTER"), text.index("RULE IMPLEMENTER"))
        self.assertLess(text.index("# orchestrator"), text.index("RULE ORCHESTRATOR"))
        self.assertTrue(text.rstrip().endswith("RULE CONFIRMATION"))
        self.assertEqual(report["write_state"], "created")
        for role in ("implementer", "orchestrator"):
            self.assertEqual(report["paragraph_accounting"][role]["content_bytes"],
                             report["current_preview"]["roles"][role]["content_bytes"])
        future = report["future_recipient"]
        target = pattern_buffer.auxiliary("sessions")._session_path(
            self.root, "codex", "fixture-session")
        validator = pattern_buffer.context_validator(self.root, "codex", "fixture-session")
        validator(text, target, "orchestrator")
        self.assertEqual(future["roles"], validator.last_report["roles"])
        self.assertEqual(future["source"], str(target))

    def test_headroom_is_exact_and_prevents_output(self):
        output = self.base / "first.md"
        result = self.compose(output, "--session", "fixture-session", "--min-headroom", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.json_result(result)
        minimum = min(budget["remaining_bytes"]
                      for group in (report["current_preview"], report["future_recipient"])
                      for budget in group["roles"].values())
        same_path = self.compose(output, "--session", "fixture-session", "--min-headroom", str(minimum))
        self.assertEqual(same_path.returncode, 0, same_path.stderr)
        rejected_output = self.base / "rejected.md"
        rejected = self.compose(rejected_output, "--session", "fixture-session",
                                "--min-headroom", str(minimum + 1))
        self.assertEqual(rejected.returncode, 1)
        self.assertTrue(self.json_result(rejected)["headroom_errors"])
        self.assertFalse(rejected_output.exists())

    def test_check_compares_bytes_without_writing(self):
        output = self.root / ".context-injector/.h/draft.md"
        before = self.snapshot(self.root)
        auxiliary_dir = SCRIPT.parent / "__pycache__"
        aux_before = self.snapshot(auxiliary_dir) if auxiliary_dir.exists() else {}
        missing = self.compose(output, "--session", "fixture-session", "--check", "--min-headroom", "0")
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(self.snapshot(self.root), before)
        self.assertEqual(self.snapshot(auxiliary_dir) if auxiliary_dir.exists() else {}, aux_before)
        self.assertFalse(output.parent.exists())
        created = self.compose(output, "--session", "fixture-session", "--min-headroom", "0")
        self.assertEqual(created.returncode, 0, created.stderr)
        before_same = self.snapshot(self.root)
        same = self.compose(output, "--session", "fixture-session", "--check", "--min-headroom", "0")
        self.assertEqual(same.returncode, 0, same.stderr)
        self.assertEqual(self.json_result(same)["check"], "matches")
        self.assertEqual(self.snapshot(self.root), before_same)
        output.write_text("different bytes\n", encoding="utf-8")
        before_diff = self.snapshot(self.root)
        diff = self.compose(output, "--session", "fixture-session", "--check", "--min-headroom", "0")
        self.assertEqual(diff.returncode, 1)
        self.assertEqual(self.json_result(diff)["check"], "differs")
        self.assertEqual(self.snapshot(self.root), before_diff)

    def test_overwrite_requires_replace(self):
        output = self.base / "composed.md"
        first = self.compose(output, "--min-headroom", "0")
        self.assertEqual(first.returncode, 0, first.stderr)
        original = output.read_bytes()
        self.mission.write_text(self.mission.read_text() + "\nUPDATED MISSION\n", encoding="utf-8")
        refused = self.compose(output, "--min-headroom", "0")
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(output.read_bytes(), original)
        replaced = self.compose(output, "--replace", "--min-headroom", "0")
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertIn(b"UPDATED MISSION", output.read_bytes())
        session_source = self.root / ".context-injector/sessions/codex-fixture-session.md"
        refused_session_source = self.compose(session_source, "--session", "fixture-session",
                                              "--min-headroom", "0")
        self.assertEqual(refused_session_source.returncode, 1)
        self.assertIn("session-enroll --reviewed-update --evidence", refused_session_source.stderr)
        self.assertFalse(session_source.exists())

    def test_references_check_local_targets_anchors_and_warn_offline(self):
        docs = self.root / "docs"
        docs.mkdir()
        reference = docs / "guide.md"
        reference.write_text("# Setup\n\n## Repeat\n\n## Repeat\n", encoding="utf-8")
        self.write_inputs(
            f"\nSee [guide](docs/guide.md#repeat-1) and `{reference}`. "
            "Remote [site](https://example.invalid/guide), https://bare.invalid/page, "
            "and command `cd /missing/path`.\n"
        )
        output = self.base / "references.md"
        valid = self.compose(output, "--min-headroom", "0")
        self.assertEqual(valid.returncode, 0, valid.stderr)
        warnings = self.json_result(valid)["references"]["warnings"]
        self.assertTrue(any(item["kind"] == "remote" for item in warnings))
        self.assertTrue(any(item["target"] == "https://bare.invalid/page" for item in warnings))
        self.assertTrue(any(item["kind"] == "unsupported_command_path" for item in warnings))
        self.assertFalse(self.json_result(valid)["references"]["errors"])
        self.write_inputs("\nSee [missing](docs/missing.md#absent).\n")
        missing = self.compose(self.base / "missing.md", "--min-headroom", "0")
        self.assertEqual(missing.returncode, 1)
        kinds = {item["kind"] for item in self.json_result(missing)["references"]["errors"]}
        self.assertEqual(kinds, {"missing_target"})
        self.write_inputs("\nSee [guide](docs/guide.md#absent).\n")
        missing_heading = self.compose(self.base / "missing-heading.md", "--min-headroom", "0")
        self.assertEqual(missing_heading.returncode, 1)
        self.assertTrue(any(item["kind"] == "missing_heading"
                            for item in self.json_result(missing_heading)["references"]["errors"]))

    def test_composed_anchor_can_target_a_heading_from_rules(self):
        self.mission.write_text("# shared\n\nSee [implementer](#implementer).\n", encoding="utf-8")
        output = self.base / "cross-section-anchor.md"
        result = self.compose(output, "--min-headroom", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.json_result(result)["references"]["errors"])

    def test_mission_cannot_supply_confirmation(self):
        self.mission.write_text("# shared\n\nMISSION SHARED\n\n# confirmation\n\nUNREVIEWED FOOTER\n",
                                encoding="utf-8")
        output = self.base / "invalid.md"
        result = self.compose(output, "--min-headroom", "0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("mission may include", result.stderr)
        self.assertFalse(output.exists())

    def test_validation_errors_and_operational_commands_only_warn(self):
        context = (
            "# shared\n\nSee [missing](docs/missing.md).\n\n"
            "# implementer\n\nImplement.\n\n"
            "# orchestrator\n\nCoordinate.\n"
        )
        self.register(context)
        validated = self.cli("validate", "--harness", "codex", "--session", "fixture-session")
        self.assertEqual(validated.returncode, 1)
        self.assertIn("local context references are invalid", validated.stderr)
        status = self.cli("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        status_warnings = self.json_result(status)["reference_warnings"]
        self.assertTrue(status_warnings)
        self.assertTrue(all(item["severity"] == "warning" for item in status_warnings))

        source = self.base / "reviewed-context.md"
        source.write_text(context, encoding="utf-8")
        enrolled = self.cli("session-enroll", "--harness", "codex", "--session", "fixture-session",
                            "--context-file", source, "--role", "orchestrator")
        self.assertEqual(enrolled.returncode, 0, enrolled.stderr)
        enrollment_warnings = self.json_result(enrolled)["reference_warnings"]
        self.assertTrue(enrollment_warnings)
        self.assertTrue(all(item["severity"] == "warning" for item in enrollment_warnings))
        enrollment_status = self.cli("enrollment-status", "--harness", "codex",
                                     "--session", "fixture-session")
        self.assertEqual(enrollment_status.returncode, 0, enrollment_status.stderr)
        self.assertTrue(self.json_result(enrollment_status)["reference_warnings"])

        settings = self.base / "codex-hooks.json"
        settings.write_text(json.dumps(pattern_buffer.hook_config("codex", SCRIPT, with_checkpoints=False)),
                            encoding="utf-8")
        handoff = self.cli("handoff-check", "--harness", "codex", "--session", "other-fixture-session",
                           "--settings", settings)
        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        handoff_report = self.json_result(handoff)
        self.assertTrue(handoff_report["configuration_ready"])
        self.assertTrue(handoff_report["reference_warnings"])


if __name__ == "__main__":
    unittest.main()
