#!/usr/bin/env python3
"""Tests for the private temporary directory around each GitHub CLI call.

gh caches API and extension data under `$TMPDIR/gh-cli-cache`. A shared
temp root lets one account or sandbox own that folder and deny every other
caller. Each wrapper call therefore gets its own directory.
"""
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import trusted_gh

TEMP_VARIABLES = ("TMPDIR", "TMP", "TEMP")


class CapturingRunner:
    """Record the child environment and directory state during one call."""

    def __init__(self):
        self.calls = []

    def __call__(self, arguments, **kwargs):
        directory = Path(kwargs["cwd"])
        self.calls.append({
            "cwd": directory,
            "env": kwargs["env"],
            "existed": directory.is_dir(),
            "mode": stat.S_IMODE(directory.stat().st_mode),
        })
        return subprocess.CompletedProcess(arguments, 0, "", "")


class PrivateTempDirectoryTest(unittest.TestCase):
    """Each gh call runs inside its own private temporary directory."""

    def setUp(self):
        self.parent = tempfile.TemporaryDirectory()
        self.addCleanup(self.parent.cleanup)
        self.repository = tempfile.TemporaryDirectory()
        self.addCleanup(self.repository.cleanup)
        self.parent_path = Path(self.parent.name).resolve()

    def run_calls(self, count: int) -> CapturingRunner:
        """Run `count` wrapper calls against a fixed safe parent."""
        runner = CapturingRunner()
        with patch.object(trusted_gh, "resolve_gh", return_value=sys.executable):
            with patch.object(trusted_gh, "_safe_directory",
                              return_value=self.parent_path):
                for _ in range(count):
                    trusted_gh.run_gh(Path(self.repository.name), ["api", "user"],
                                      runner=runner)
        return runner

    def test_child_temp_variables_name_the_private_directory(self):
        call = self.run_calls(1).calls[0]
        for name in TEMP_VARIABLES:
            with self.subTest(variable=name):
                self.assertEqual(Path(call["env"][name]), call["cwd"])

    def test_private_directory_sits_under_the_safe_parent(self):
        call = self.run_calls(1).calls[0]
        self.assertNotEqual(call["cwd"], self.parent_path)
        self.assertEqual(call["cwd"].parent, self.parent_path)

    def test_each_call_gets_a_distinct_directory(self):
        first, second = self.run_calls(2).calls
        self.assertNotEqual(first["cwd"], second["cwd"])

    def test_directory_exists_during_call_and_leaves_after(self):
        call = self.run_calls(1).calls[0]
        self.assertTrue(call["existed"])
        self.assertFalse(call["cwd"].exists())

    @unittest.skipIf(os.name == "nt", "Windows does not expose POSIX file modes")
    def test_private_directory_is_owner_only(self):
        call = self.run_calls(1).calls[0]
        self.assertEqual(call["mode"], 0o700)


class TempRootSelectionTest(unittest.TestCase):
    """The private directory follows the caller's configured temp root."""

    def test_private_directory_lands_under_configured_temp_root(self):
        with tempfile.TemporaryDirectory() as configured, \
                tempfile.TemporaryDirectory() as repository:
            configured_root = Path(configured).resolve()
            runner = CapturingRunner()
            overrides = {name: str(configured_root) for name in TEMP_VARIABLES}
            with patch.dict(os.environ, overrides, clear=False), \
                    patch.object(tempfile, "tempdir", None), \
                    patch.object(trusted_gh, "resolve_gh", return_value=sys.executable):
                trusted_gh.run_gh(Path(repository), ["api", "user"], runner=runner)
        self.assertEqual(runner.calls[0]["cwd"].parent, configured_root)


class PermissionMessageTest(unittest.TestCase):
    """A temp permission failure names its path and the recovery step."""

    DENIED = PermissionError(13, "Permission denied", "/tmp/gh-cli-cache")

    def test_requested_command_reports_the_denied_path(self):
        output = io.StringIO()
        with patch.object(trusted_gh, "with_repository_context",
                          side_effect=lambda _root, arguments: list(arguments)), \
                patch.object(trusted_gh, "authenticated_account",
                             return_value={"id": 1, "login": "octocat"}), \
                patch.object(trusted_gh, "run_gh", side_effect=self.DENIED), \
                redirect_stderr(output):
            result = trusted_gh._run_requested_command(
                str(REPOSITORY_ROOT), ["api", "user"])
        self.assertEqual(result, 1)
        self.assertIn("not writable", output.getvalue())
        self.assertIn("gh-cli-cache", output.getvalue())
        self.assertIn("TMPDIR", output.getvalue())

    def test_account_check_reports_the_denied_path(self):
        output = io.StringIO()
        with patch.object(trusted_gh, "authenticated_account",
                          side_effect=self.DENIED), \
                patch.object(trusted_gh.sys, "argv", ["trusted_gh.py"]), \
                redirect_stderr(output):
            result = trusted_gh.main()
        self.assertEqual(result, 1)
        self.assertIn("not writable", output.getvalue())
        self.assertIn("gh-cli-cache", output.getvalue())


if __name__ == "__main__":
    unittest.main()
