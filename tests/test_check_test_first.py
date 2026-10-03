#!/usr/bin/env python3
"""Tests for test-first change enforcement."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_test_first


class TestFirstCheckTest(unittest.TestCase):
    """Executable changes require a changed behavioral test."""

    def test_executable_change_without_test_fails(self):
        self.assertTrue(check_test_first.violations(["scripts/trusted_gh.py"]))

    def test_executable_change_with_test_passes(self):
        self.assertEqual(
            check_test_first.violations(["scripts/trusted_gh.py", "tests/test_trusted_gh.py"]),
            [],
        )

    def test_documentation_only_change_passes(self):
        self.assertEqual(check_test_first.violations(["README.md"]), [])


if __name__ == "__main__":
    unittest.main()
