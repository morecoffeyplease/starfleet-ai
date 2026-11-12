import contextlib
import io
import unittest
from unittest import mock

from scripts import verify


EXPECTED_PYTHON_CHECKS = (
    ("OpenCode adapter", ("-m", "unittest", "discover", "-s", "apps/opencode-adapter/tests", "-p", "test_*.py", "-v")),
    ("Away Mode", ("apps/away-mode/verify.py",)),
    ("Context Injector compatibility", ("-m", "unittest", "discover", "-s", "apps/context-injector", "-p", "test_*.py", "-v")),
    ("Discord Watch", ("apps/discord-watch/verify.py",)),
    ("Hailing Frequencies", ("apps/hailing-frequencies/verify.py",)),
    ("Pattern Buffer", ("-m", "unittest", "discover", "-s", "apps/pattern-buffer", "-p", "test_*.py", "-v")),
    ("Replicator", ("apps/replicator/verify.py",)),
    ("Starfleet Policy Guard", ("apps/starfleet-policy-guard/verify.py",)),
    ("Warp-drive", ("apps/warp-drive/verify.py",)),
)


class VerificationRunnerTests(unittest.TestCase):
    def test_python_commands_match_the_maintained_aggregate(self):
        self.assertEqual(verify.PYTHON_CHECKS, EXPECTED_PYTHON_CHECKS)

    def test_selector_returns_only_the_existing_command(self):
        expected = (("Warp-drive", ("apps/warp-drive/verify.py",)),)
        self.assertEqual(verify.select_python_checks("Warp-drive"), expected)
        with mock.patch.object(verify, "run", return_value=0) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(verify.verify_python("Warp-drive"), [])
        run.assert_called_once_with(
            "Warp-drive", [verify.sys.executable, "apps/warp-drive/verify.py"]
        )

    def test_selector_rejects_empty_and_unknown_names(self):
        for name in ("", "unknown"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    verify.select_python_checks(name)

    def test_cli_rejects_missing_empty_and_unknown_selections(self):
        parser = verify.build_parser()
        for arguments in (
            ["--python-check"],
            ["--python-check", ""],
            ["--python-check", "unknown"],
        ):
            with self.subTest(arguments=arguments):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        parser.parse_args(arguments)
                self.assertEqual(raised.exception.code, 2)

    def test_component_matrix_covers_each_command_on_all_four_platforms(self):
        matrix = verify.python_component_matrix()
        platforms = {(item["os"], item["python"]) for item in matrix}
        self.assertEqual(
            platforms,
            {
                ("macos-15-intel", "3.10"),
                ("macos-15-intel", "3.14"),
                ("ubuntu-latest", "3.10"),
                ("ubuntu-latest", "3.14"),
            },
        )
        self.assertEqual(len(matrix), 4 * len(EXPECTED_PYTHON_CHECKS))
        for operating_system, version in verify.PYTHON_PLATFORMS:
            labels = [
                item["check"]
                for item in matrix
                if item["os"] == operating_system and item["python"] == version
            ]
            self.assertCountEqual(labels, [label for label, _ in EXPECTED_PYTHON_CHECKS])
            self.assertEqual(len(labels), len(set(labels)))

    def test_aggregate_requires_successful_components_and_preparation(self):
        self.assertTrue(verify.python_verification_succeeded("success", "success"))
        for result in ("failure", "skipped", "cancelled", "timed_out", "neutral", ""):
            with self.subTest(component_result=result):
                self.assertFalse(verify.python_verification_succeeded(result, "success"))
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        verify.main(["--require-python-results", result, "success"]), 1
                    )
            with self.subTest(preparation_result=result):
                self.assertFalse(verify.python_verification_succeeded("success", result))
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        verify.main(["--require-python-results", "success", result]), 1
                    )
        self.assertEqual(verify.main(["--require-python-results", "success", "success"]), 0)

    def test_single_component_mode_skips_markup(self):
        with (
            mock.patch.object(verify, "verify_python", return_value=[]) as verify_python,
            mock.patch.object(verify, "verify_runner_tests") as verify_runner_tests,
            mock.patch.object(verify, "verify_markup", side_effect=AssertionError),
        ):
            self.assertEqual(verify.main(["--python-check", "Warp-drive"]), 0)
        verify_python.assert_called_once_with("Warp-drive")
        verify_runner_tests.assert_not_called()

    def test_local_aggregate_modes_run_runner_tests_and_preserve_markup_scope(self):
        with (
            mock.patch.object(verify, "verify_runner_tests", return_value=[]) as runner_tests,
            mock.patch.object(verify, "verify_python", return_value=[]) as verify_python,
            mock.patch.object(verify, "verify_markup", return_value=[]) as verify_markup,
        ):
            self.assertEqual(verify.main([]), 0)
            self.assertEqual(verify.main(["--python-only"]), 0)
        self.assertEqual(runner_tests.call_count, 2)
        verify_python.assert_has_calls([mock.call(None), mock.call(None)])
        verify_markup.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
