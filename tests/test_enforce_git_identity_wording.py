#!/usr/bin/env python3
"""Tests for the identity-recovery wording in the git identity hook."""
import importlib.util
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPOSITORY_ROOT / "hooks" / "enforce_git_identity.py"


def load_hook():
    """Load the hook module from its file path."""
    spec = importlib.util.spec_from_file_location("enforce_git_identity_wording", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IdentityRecoveryWordingTest(unittest.TestCase):
    """The warning directs self-resolution instead of asking for values."""

    def setUp(self):
        self.warning = load_hook().build_warning("identity unset", "")

    def test_warning_directs_derivation_from_authenticated_source(self):
        self.assertIn("Derive the name and email from an authenticated account source",
                      self.warning)
        self.assertIn("Confirm the derived values with the user", self.warning)

    def test_warning_prohibits_asking_the_user_for_values(self):
        self.assertIn("Never ask the user", self.warning)
        self.assertNotIn("Confirm the exact name and email with the user", self.warning)

    def test_warning_keeps_history_candidate_rule(self):
        self.assertIn("Never auto-select a history candidate", self.warning)


if __name__ == "__main__":
    unittest.main()
