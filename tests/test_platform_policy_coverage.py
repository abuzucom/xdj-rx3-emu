#!/usr/bin/env python3
"""Exercise platform policy paths that host integration tests reach."""
import sys
import unittest
from pathlib import Path


HOOKS_DIRECTORY = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIRECTORY))

import _platform_policy as platform_policy


class PlatformPolicyCoverageTest(unittest.TestCase):
    """Host-independent tests cover every platform dispatch path."""

    def assert_verdict(self, command: str, expected: str) -> None:
        """Assert one Linux command verdict."""
        program_name, *arguments = command.split()
        verdict, _reason = platform_policy.classify_platform_command(
            "linux",
            program_name,
            arguments,
        )
        self.assertEqual(verdict, expected)

    def test_remaining_linux_command_families(self):
        for command, expected in (
            ("systemctl status sample.service", "ask"),
            ("crontab -l", ""),
            ("iptables -L", "deny"),
            ("nft list ruleset", "deny"),
            ("kubectl get pods", "deny"),
            ("helm list", "deny"),
            ("printf value", ""),
        ):
            with self.subTest(command=command):
                self.assert_verdict(command, expected)

    def test_unsupported_platform_allows_unclassified_command(self):
        verdict, _reason = platform_policy.classify_platform_command(
            "win32",
            "printf",
            ["value"],
        )
        self.assertEqual(verdict, "")


if __name__ == "__main__":
    unittest.main()
