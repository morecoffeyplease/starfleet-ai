#!/usr/bin/env python3
"""Run Discord Watch's isolated standard-library verification suite."""

import pathlib
import sys
import unittest


APP_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))


def main():
    suite = unittest.defaultTestLoader.discover(str(APP_DIR / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
