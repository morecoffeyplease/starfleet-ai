import subprocess
import sys
from pathlib import Path
import importlib.util
import unittest


SCRIPT = Path(__file__).with_name("context_injector.py")
CANONICAL = Path(__file__).resolve().parents[1] / "pattern-buffer"
sys.path.insert(0, str(CANONICAL))

# Keep the historical discovery command meaningful by exposing the maintained suite here.
from test_pattern_buffer import PatternBufferTests
from test_checkpoints import CheckpointTests
from test_enrollment import EnrollmentTests
from test_restricted_enrollment import RestrictedEnrollmentTests
from test_idle_warnings import IdleWarningTests

_CANONICAL_COMPATIBILITY = CANONICAL / "test_compatibility.py"
_SPEC = importlib.util.spec_from_file_location("pattern_buffer_compatibility_tests", _CANONICAL_COMPATIBILITY)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
RenameCompatibilityTests = _MODULE.RenameCompatibilityTests


class LegacySourceCliCompatibilityTests(unittest.TestCase):
    def test_legacy_cli_prints_the_current_guide_without_git_or_state(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "guide", "--print"],
                                capture_output=True, text=True, check=True)
        self.assertIn("# Pattern Buffer guide", result.stdout)


if __name__ == "__main__":
    unittest.main()
