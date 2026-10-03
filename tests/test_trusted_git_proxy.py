#!/usr/bin/env python3
"""Tests for managed proxy placeholder handling in trusted Git."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import trusted_git


class TrustedGitProxyTest(unittest.TestCase):
    """Trusted Git drops only the managed loopback placeholder."""

    def run_with_environment(self, environment: dict) -> dict:
        """Run trusted Git through a capturing runner and return its env."""
        captured = {}

        def runner(arguments, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch.dict(os.environ, environment, clear=False):
            with patch.object(trusted_git, "resolve_git", return_value=sys.executable):
                trusted_git.run_git(Path(tempfile.gettempdir()), ["status"],
                                    runner=runner)
        return captured["env"]

    def test_placeholder_proxy_variables_are_removed(self):
        environment = self.run_with_environment({
            "HTTP_PROXY": "http://127.0.0.1:9",
            "https_proxy": "127.0.0.1:9",
        })
        # Compare booleans so a failure never prints environment values.
        self.assertFalse("HTTP_PROXY" in environment)
        self.assertFalse("https_proxy" in environment)

    def test_valid_proxy_is_preserved(self):
        environment = self.run_with_environment({
            "ALL_PROXY": "http://proxy.example.test:8080",
        })
        self.assertTrue(environment.get("ALL_PROXY") == "http://proxy.example.test:8080")

    def test_placeholder_detection_rejects_other_ports(self):
        self.assertTrue(trusted_git.is_managed_proxy_placeholder("http://127.0.0.1:9"))
        self.assertFalse(trusted_git.is_managed_proxy_placeholder("http://127.0.0.1:1"))
        self.assertFalse(trusted_git.is_managed_proxy_placeholder("not a url:port"))


if __name__ == "__main__":
    unittest.main()
