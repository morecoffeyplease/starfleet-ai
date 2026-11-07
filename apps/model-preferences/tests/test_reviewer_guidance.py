import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


APP = Path(__file__).resolve().parents[1]
SCRIPT = APP / "reviewer_guidance.py"
spec = importlib.util.spec_from_file_location("reviewer_guidance", SCRIPT)
reviewer_guidance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reviewer_guidance)


class ReviewerGuidanceTest(unittest.TestCase):
    def test_review_roles_receive_guidance_and_the_complete_original_task_last(self):
        task = "Approved purpose: preserve checkout flow.\n\nRepair history: retry added a regression.\n"
        task_digest = hashlib.sha256(task.encode("utf-8")).hexdigest()

        for role in ("standard_review", "advanced_review"):
            with self.subTest(role=role):
                prompt = reviewer_guidance.compose_reviewer_prompt(role, task)
                self.assertTrue(prompt.startswith("Reviewer judgment guidance\n"))
                self.assertEqual(prompt[-len(task):], task)
                self.assertEqual(hashlib.sha256(prompt[-len(task):].encode("utf-8")).hexdigest(), task_digest)
                self.assertIn("approved PR purpose", prompt)
                self.assertIn("private path", prompt)
                self.assertIn("cheapest deciding observation", prompt)

    def test_nonreview_roles_keep_the_exact_task_unchanged(self):
        task = "Task with significant whitespace.\n\n"
        for role in ("orchestrator", "implementer", "researcher"):
            with self.subTest(role=role):
                self.assertIs(reviewer_guidance.compose_reviewer_prompt(role, task), task)

    def test_print_command_matches_composition_guidance_without_creating_state(self):
        with tempfile.TemporaryDirectory(prefix="review-guidance-print-") as temp:
            result = subprocess.run([sys.executable, str(SCRIPT), "--print"], cwd=temp,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, reviewer_guidance.REVIEWER_GUIDANCE)
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
