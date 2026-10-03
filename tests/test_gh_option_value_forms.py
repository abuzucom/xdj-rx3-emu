#!/usr/bin/env python3
"""Every option form the gh gate reads through `_option_value`."""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))

import _gate_core

REPO_OPTIONS = frozenset({"--repo", "-R"})


class OptionValueFormTest(unittest.TestCase):
    """Separate, joined, equals, dangling, and absent options all resolve."""

    def test_separate_value(self):
        self.assertEqual(_gate_core._option_value(["--repo", "o/r"], REPO_OPTIONS), "o/r")
        self.assertEqual(_gate_core._option_value(["-R", "o/r"], REPO_OPTIONS), "o/r")

    def test_equals_value(self):
        self.assertEqual(_gate_core._option_value(["--REPO=o/r"], REPO_OPTIONS), "o/r")

    def test_joined_short_value(self):
        self.assertEqual(_gate_core._option_value(["-Ro/r"], REPO_OPTIONS), "o/r")

    def test_dangling_option_is_empty(self):
        self.assertEqual(_gate_core._option_value(["pr", "--repo"], REPO_OPTIONS), "")

    def test_absent_option_is_empty(self):
        self.assertEqual(_gate_core._option_value(["pr", "view"], REPO_OPTIONS), "")

    def test_lowercase_short_option_does_not_match(self):
        self.assertEqual(_gate_core._option_value(["-r", "team"], REPO_OPTIONS), "")


if __name__ == "__main__":
    unittest.main()
