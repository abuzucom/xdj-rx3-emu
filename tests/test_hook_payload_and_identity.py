#!/usr/bin/env python3
"""Identity recovery wording in the commit block, and CI installation tokens."""
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
IDENTITY_HOOK = REPO_ROOT / "hooks" / "enforce_git_identity.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import trusted_gh

INTEGRATION_STDERR = ("gh: Resource not accessible by integration (HTTP 403)\n"
                      "{\"message\":\"Resource not accessible by integration\"}")


class IdentityBlockWordingTest(unittest.TestCase):
    """The commit block derives identity and never asks for values."""

    def test_block_message_forbids_asking_for_values(self):
        spec = importlib.util.spec_from_file_location("identity_block_wording", IDENTITY_HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        stderr = io.StringIO()
        with patch.object(module, "find_violation", return_value="user.email is unset"):
            with redirect_stderr(stderr):
                blocked = module._blocks_invocation("", {"label": "git commit"})
        self.assertTrue(blocked)
        message = stderr.getvalue()
        self.assertNotIn("Ask the user which name", message)
        self.assertIn("authenticated account source", message)
        self.assertIn("Never ask the active human to supply", message)


class InstallationTokenTest(unittest.TestCase):
    """A CI installation token cannot read /user, and only CI may skip that check."""

    def test_integration_403_is_installation_token(self):
        self.assertEqual(trusted_gh.classify_gh_failure(1, INTEGRATION_STDERR),
                         "installation_token")

    def test_other_403_stays_unclassified(self):
        self.assertEqual(trusted_gh.classify_gh_failure(1, "HTTP 403: Must have admin rights"),
                         "unclassified")

    def run_command(self, environment: dict) -> tuple:
        """Run the wrapper with an installation-token account failure."""
        calls = []
        error = trusted_gh.GitHubAccessError("installation_token", 1)

        def run_gh(repository, arguments):
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref: refs/heads/feature/test\n", encoding="utf-8")
            (root / ".git" / "config").write_text(
                '[remote "origin"]\n\turl = https://github.com/owner/repo.git\n', encoding="utf-8")
            with patch.dict(os.environ, environment, clear=False):
                with patch.object(trusted_gh, "authenticated_account", side_effect=error):
                    with patch.object(trusted_gh, "run_gh", side_effect=run_gh):
                        with redirect_stderr(io.StringIO()):
                            code = trusted_gh._run_requested_command(root, ["pr", "view", "1"])
        return code, calls

    def test_actions_runner_skips_account_check(self):
        code, calls = self.run_command({"GITHUB_ACTIONS": "true"})
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)

    def test_outside_actions_the_failure_stands(self):
        for value in ("", "false", "1"):
            with self.subTest(value=value):
                code, calls = self.run_command({"GITHUB_ACTIONS": value})
                self.assertEqual(code, 1)
                self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
