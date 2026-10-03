#!/usr/bin/env python3
"""Test that the gate-change label covers the Rule 14 attribution checker.

check_commit_attribution.py decides whether agent commits disclose a
versioned model. A pull request that weakens it must need the same
gate-change-approved label as the other trusted checkers.
"""
import importlib.util
import unittest
from pathlib import Path


CHECKER_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "check_gate_pr_integrity.py"
)


def load_checker():
    """Load the integrity checker by path."""
    spec = importlib.util.spec_from_file_location("gate_pr_integrity_attribution", CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AttributionCheckerScopeTest(unittest.TestCase):
    """Changes to the attribution checker require approval."""

    def test_attribution_checker_requires_approval(self):
        checker = load_checker()
        self.assertTrue(checker.requires_approval(["scripts/check_commit_attribution.py"]))

    def test_identity_and_prose_gate_checkers_require_approval(self):
        checker = load_checker()
        for path in ("scripts/check_git_identity.py", "scripts/check_agent_prose_gate.py"):
            with self.subTest(path=path):
                self.assertTrue(checker.requires_approval([path]))

    def test_attribution_tests_stay_outside_scope(self):
        checker = load_checker()
        self.assertFalse(checker.requires_approval(["tests/test_check_commit_attribution.py"]))


if __name__ == "__main__":
    unittest.main()
