"""Focused lifecycle and safe-publication tests for session enrollments."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from _test_support import isolate_state_home

SCRIPT = Path(__file__).with_name("pattern_buffer_sessions.py")
sys.path.insert(0, str(SCRIPT.parent))
import pattern_buffer_sessions as sessions


class SessionEnrollmentTests(unittest.TestCase):
    def setUp(self):
        isolate_state_home(self)
        self.temp = tempfile.TemporaryDirectory(dir=Path.home(), prefix=".session-enrollment-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.root = self.folder / "repo"
        self.root.mkdir()
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        self.gitdir = self.root / ".git"
        self.source = self.folder / "standing.md"
        self.source.write_text("# Mission\n\nKeep this session scoped.\n", encoding="utf-8")

    def validator(self, expected_text=None, expected_role="orchestrator"):
        def validate(text, final_path, role):
            self.assertIsInstance(final_path, Path)
            self.assertEqual(final_path.parent, self.root / ".context-injector" / "sessions")
            if expected_text is not None:
                self.assertEqual(text, expected_text)
            self.assertEqual(role, expected_role)
            return {"valid": True}
        return validate

    def enroll(self, session_id="session-a", *, source_file=None, validator=None, **kwargs):
        return sessions.enroll(
            self.root, self.gitdir, "codex", session_id, source_file or self.source,
            "orchestrator", validator or self.validator(), **kwargs)

    def test_fresh_enrollment_uses_canonical_named_file_and_marker(self):
        binding = self.enroll()
        self.assertEqual(binding.state, "active")
        self.assertEqual(binding.path, str(self.root / ".context-injector" / "sessions" / "codex-session-a.md"))
        self.assertEqual(Path(binding.path).read_text(encoding="utf-8"), self.source.read_text(encoding="utf-8"))
        self.assertEqual(binding.file_status, "valid")
        self.assertEqual(binding.source_sha256, sessions._digest(self.source.read_bytes()))
        self.assertEqual(binding.history[0]["action"], "enrolled")
        self.assertEqual(sessions.lookup(self.root, self.gitdir, "codex", "session-a"), binding)
        self.assertFalse((self.root / ".gitignore").exists())

    def test_validation_rejection_happens_before_any_mutation(self):
        def reject(text, final_path, role):
            self.assertFalse((self.root / ".context-injector").exists())
            self.assertFalse((self.gitdir / "context-injector").exists())
            return {"valid": False}

        with self.assertRaisesRegex(ValueError, "validation was rejected"):
            self.enroll(validator=reject)
        self.assertFalse((self.root / ".context-injector").exists())
        self.assertFalse((self.gitdir / "context-injector").exists())

    def test_unopted_unsafe_identity_keeps_legacy_lookup_read_only(self):
        self.assertIsNone(sessions.lookup(self.root, self.gitdir, "codex", "s/session-a"))
        self.assertFalse((self.gitdir / "context-injector").exists())
        with self.assertRaisesRegex(ValueError, "safe opaque token"):
            self.enroll("s/session-a")
        self.assertFalse((self.gitdir / "context-injector").exists())

    def test_reviewed_update_requires_explicit_flag_and_preserves_old_file_on_rejection(self):
        original = self.enroll()
        original_file = Path(original.path).read_bytes()
        registry_path = self.gitdir / "context-injector" / "session-enrollments" / "registry.json"
        original_registry = registry_path.read_bytes()
        self.source.write_text("# Updated mission\n\nNew reviewed text.\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "active same owner"):
            self.enroll(validator=self.validator())
        self.assertEqual(Path(original.path).read_bytes(), original_file)
        self.assertEqual(registry_path.read_bytes(), original_registry)

        def reject(text, final_path, role):
            self.assertEqual(text, self.source.read_text(encoding="utf-8"))
            return False

        with self.assertRaisesRegex(ValueError, "validation was rejected"):
            self.enroll(reviewed_update=True, evidence="Reviewed source", validator=reject)
        self.assertEqual(Path(original.path).read_bytes(), original_file)
        self.assertEqual(registry_path.read_bytes(), original_registry)

        updated = self.enroll(reviewed_update=True, evidence="Reviewed current mission")
        self.assertEqual(Path(updated.path).read_text(encoding="utf-8"), self.source.read_text(encoding="utf-8"))
        self.assertEqual(updated.source_sha256, sessions._digest(self.source.read_bytes()))
        self.assertEqual(updated.history[-1]["action"], "reviewed_update")

    def test_release_pending_adoption_and_duplicate_handoff(self):
        old_id = "00000000-0000-0000-0000-000000000001"
        new_id = "00000000-0000-0000-0000-000000000002"
        old = self.enroll(old_id)
        old_bytes = Path(old.path).read_bytes()
        args = (self.root, self.gitdir, old.enrollment_id, "codex", old_id,
                "claude", new_id, "User directed replacement")
        with self.assertRaisesRegex(ValueError, "timer cleanup"):
            sessions.release(*args, timers_safe=False)
        self.assertEqual(sessions.lookup(self.root, self.gitdir, "codex", old_id).state, "active")

        released = sessions.release(*args, timers_safe=True)
        self.assertEqual(released.state, "released")
        self.assertEqual(sessions.lookup(self.root, self.gitdir, "codex", old_id).state, "released")
        self.assertEqual(sessions.lookup(self.root, self.gitdir, "claude", new_id).state, "pending")
        self.assertEqual(sessions.release(*args, timers_safe=True).state, "released")

        adopted = sessions.adopt(self.root, self.gitdir, old.enrollment_id, "claude", new_id,
                                 self.source, "orchestrator", "Successor reviewed the mission",
                                 self.validator())
        self.assertEqual(adopted.state, "active")
        self.assertEqual(sessions.lookup(self.root, self.gitdir, "codex", old_id).state, "retired")
        self.assertEqual(sessions.lookup(self.root, self.gitdir, "claude", new_id).state, "active")
        self.assertEqual(Path(old.path).read_bytes(), old_bytes)
        self.assertEqual(Path(adopted.path).read_bytes(), self.source.read_bytes())
        self.assertEqual(sessions.adopt(self.root, self.gitdir, old.enrollment_id, "claude", new_id,
                                        self.source, "orchestrator", "Successor reviewed the mission",
                                        self.validator()).state, "active")
        self.assertEqual(sessions.release(*args, timers_safe=True).state, "retired")
        with self.assertRaisesRegex(ValueError, "conflicting duplicate release"):
            sessions.release(self.root, self.gitdir, old.enrollment_id, "codex", old_id,
                             "claude", new_id, "different evidence", timers_safe=True)
        with self.assertRaisesRegex(ValueError, "pending successor"):
            sessions.adopt(self.root, self.gitdir, old.enrollment_id, "codex", old_id,
                           self.source, "orchestrator", "Stale owner", self.validator())

    def test_lookup_fails_closed_for_missing_registry_and_reports_bad_file(self):
        binding = self.enroll()
        context_path = Path(binding.path)
        context_path.unlink()
        missing = sessions.lookup(self.root, self.gitdir, "codex", "session-a")
        self.assertEqual(missing.state, "active")
        self.assertEqual(missing.file_status, "missing")
        context_path.write_text("unreviewed replacement\n", encoding="utf-8")
        changed = sessions.lookup(self.root, self.gitdir, "codex", "session-a")
        self.assertEqual(changed.file_status, "changed")

        registry_path = self.gitdir / "context-injector" / "session-enrollments" / "registry.json"
        registry_path.unlink()
        with self.assertRaisesRegex(ValueError, "required-binding marker"):
            sessions.lookup(self.root, self.gitdir, "codex", "session-a")

    def test_lookup_rechecks_session_directory_symlinks(self):
        binding = self.enroll()
        context_dir = self.root / ".context-injector"
        saved_dir = self.root / "saved-context"
        context_dir.rename(saved_dir)
        context_dir.symlink_to(saved_dir, target_is_directory=True)
        found = sessions.lookup(self.root, self.gitdir, "codex", "session-a")
        self.assertEqual(found.state, "active")
        self.assertEqual(found.file_status, "unsafe")

    def test_source_symlink_components_and_root_mismatch_are_rejected(self):
        source_link = self.folder / "source-link.md"
        source_link.symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.enroll("symlink-session", source_file=source_link)
        linked_dir = self.folder / "linked-directory"
        linked_dir.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.enroll("linked-directory-session", source_file=linked_dir / self.source.name)
        self.assertFalse((self.gitdir / "context-injector").exists())

        other_root = self.folder / "other"
        other_root.mkdir()
        subprocess.run(["git", "-C", str(other_root), "init", "-q"], check=True)
        with self.assertRaisesRegex(ValueError, "exact worktree root"):
            sessions.lookup(self.root, other_root / ".git", "codex", "session-a")

    def test_source_size_is_bounded_before_enrollment(self):
        self.source.write_bytes(b"x" * (sessions.SESSION_FILE_LIMIT + 1))
        with self.assertRaisesRegex(ValueError, "64 KiB|65536"):
            self.enroll()
        self.assertFalse((self.gitdir / "context-injector").exists())

    def test_uuid_case_is_canonical_in_binding_and_filename(self):
        upper = "ABCDEF12-3456-7890-ABCD-EF1234567890"
        binding = self.enroll(upper)
        self.assertEqual(binding.session_id, upper.lower())
        self.assertTrue(binding.path.endswith(f"codex-{upper.lower()}.md"))
        self.assertEqual(sessions.lookup(self.root, self.gitdir, "codex", upper.lower()).state, "active")

    def test_corrupt_registry_never_falls_back_to_another_context(self):
        self.enroll()
        registry = self.gitdir / "context-injector" / "session-enrollments" / "registry.json"
        registry.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "registry is corrupt"):
            sessions.lookup(self.root, self.gitdir, "codex", "session-a")


if __name__ == "__main__":
    unittest.main()
