#!/usr/bin/env python3
"""Tests for the identity advisory when no candidate exists."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
CHECKER_PATH = REPOSITORY_ROOT / "scripts" / "check_git_identity.py"


def load_checker():
    """Load the identity checker from its file path."""
    spec = importlib.util.spec_from_file_location("check_git_identity_advisory", CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MissingCandidateAdvisoryTest(unittest.TestCase):
    """The advisory directs self-resolution instead of asking the user."""

    def setUp(self):
        checker = load_checker()
        with patch.object(checker, "authenticated_account", None):
            with patch.object(checker, "history_identity_candidates", return_value=[]):
                self.advisory = checker.bootstrap_identity_advisory(str(REPOSITORY_ROOT))

    def test_advisory_directs_authenticated_derivation(self):
        self.assertIn("derive name and email from an authenticated account source",
                      self.advisory)
        self.assertIn("request approval", self.advisory)

    def test_advisory_does_not_ask_for_values(self):
        self.assertNotIn("ask for a repository-local name and email", self.advisory)


if __name__ == "__main__":
    unittest.main()
