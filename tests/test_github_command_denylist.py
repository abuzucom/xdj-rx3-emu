#!/usr/bin/env python3
"""Test fail-closed loading of the GitHub CLI denylist."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "hooks"))

import _gate_core


class GithubCommandDenylistTest(unittest.TestCase):
    """Malformed or unavailable denylist data never clears a command."""

    def setUp(self):
        _gate_core._load_github_command_denylist.cache_clear()

    def tearDown(self):
        _gate_core._load_github_command_denylist.cache_clear()

    def test_missing_denylist_denies(self):
        missing = Path("missing-github-command-denylist.txt")
        with patch.object(_gate_core, "GH_COMMAND_DENYLIST", missing):
            decision, reason = _gate_core._github_command_denylist_verdict(
                ["agent-task", "list"])
        self.assertEqual(decision, "deny")
        self.assertIn("unavailable", reason)

    def test_malformed_denylist_denies(self):
        denylist = REPOSITORY_ROOT / "tests" / "fixtures" / (
            "malformed-github-command-denylist.txt")
        with patch.object(_gate_core, "GH_COMMAND_DENYLIST", denylist):
            decision, reason = _gate_core._github_command_denylist_verdict(
                ["agent-task", "list"])
        self.assertEqual(decision, "deny")
        self.assertIn("invalid", reason)

    def test_option_values_do_not_shift_denied_paths(self):
        cases = (
            ["pr", "-R", "OWNER/REPO", "merge", "12"],
            ["pr", "-ROWNER/REPO", "merge", "12"],
            ["pr", "--repo=OWNER/REPO", "merge", "12"],
            ["pr", "--", "merge", "12"],
            ["auth", "--hostname", "github.example", "token"],
            ["auth", "-h", "github.example", "token"],
            ["auth", "-u", "octocat", "token"],
            ["auth", "-s", "read:org", "token"],
            ["auth", "--scopes", "read:org", "login"],
            ["repo", "--repo", "OWNER/REPO", "clone", "OWNER/REPO"],
        )
        for command in cases:
            with self.subTest(command=command):
                decision, _reason = _gate_core.github_cli_verdict(command)
                self.assertEqual(decision, "deny")

    def test_option_values_do_not_shift_legacy_guards(self):
        decision, _reason = _gate_core.github_cli_verdict(
            ["repo", "--repo", "OWNER/REPO", "edit", "--visibility", "public"])
        self.assertEqual(decision, "deny")


if __name__ == "__main__":
    unittest.main()
