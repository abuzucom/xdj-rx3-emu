#!/usr/bin/env python3
"""Tests for GitHub CLI account-check failure classification."""
import io
import os
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

PROXY_SECRET = "http://user:proxy-secret@proxy.example.test:8080"
STDERR_SECRET = "stderr-secret-value"


def completed(returncode: int, stderr: str) -> subprocess.CompletedProcess:
    """Return a GitHub CLI result with the given exit code and stderr."""
    return subprocess.CompletedProcess(["gh"], returncode, "", stderr)


class FailureClassificationTest(unittest.TestCase):
    """Account-check failures report their real category."""

    def classify(self, result: subprocess.CompletedProcess) -> trusted_gh.GitHubAccessError:
        """Run the real account check against a fixed CLI result."""
        def runner(arguments, **kwargs):
            return result

        real_run_gh = trusted_gh.run_gh

        def run_gh(repo_root, arguments, **kwargs):
            kwargs["runner"] = runner
            return real_run_gh(repo_root, arguments, **kwargs)

        with patch.object(trusted_gh, "resolve_gh", return_value=sys.executable):
            with patch.object(trusted_gh, "run_gh", side_effect=run_gh):
                with self.assertRaises(trusted_gh.GitHubAccessError) as context:
                    trusted_gh.authenticated_account(Path(tempfile.gettempdir()))
        return context.exception

    def test_auth_required_exit_code_is_authentication(self):
        error = self.classify(completed(4, "To get started with GitHub CLI"))
        self.assertEqual(error.category, "authentication")

    def test_http_401_is_authentication(self):
        error = self.classify(completed(1, "HTTP 401: Bad credentials"))
        self.assertEqual(error.category, "authentication")

    def test_proxy_connect_failure_is_network(self):
        stderr = ('Get "https://api.github.com/user": proxyconnect tcp: '
                  "dial tcp 127.0.0.1:9: connect: connection refused")
        error = self.classify(completed(1, stderr))
        self.assertEqual(error.category, "network")

    def test_unknown_failure_is_unclassified(self):
        error = self.classify(completed(1, STDERR_SECRET))
        self.assertEqual(error.category, "unclassified")
        self.assertEqual(error.returncode, 1)
        self.assertNotIn(STDERR_SECRET, str(error))

    def test_access_error_remains_an_os_error(self):
        self.assertTrue(issubclass(trusted_gh.GitHubAccessError, OSError))


class FailureMessageTest(unittest.TestCase):
    """Failure messages name the category without leaking values."""

    def message_for(self, error: OSError) -> str:
        """Return stderr from main() when the account check raises error."""
        output = io.StringIO()
        with patch.object(trusted_gh, "authenticated_account", side_effect=error):
            with patch.object(trusted_gh.sys, "argv", ["trusted_gh.py"]):
                with patch.dict(os.environ, {"HTTPS_PROXY": PROXY_SECRET}, clear=False):
                    with redirect_stderr(output):
                        result = trusted_gh.main()
        self.assertEqual(result, 1)
        return output.getvalue()

    def test_network_message_names_variables_not_values(self):
        message = self.message_for(trusted_gh.GitHubAccessError("network", 1))
        self.assertIn("could not reach GitHub", message)
        self.assertIn("HTTPS_PROXY", message)
        self.assertNotIn("proxy-secret", message)
        self.assertNotIn("no authenticated account", message)

    def test_authentication_message_is_specific(self):
        message = self.message_for(trusted_gh.GitHubAccessError("authentication", 4))
        self.assertIn("no authenticated account", message)

    def test_unclassified_message_disclaims_authentication(self):
        message = self.message_for(trusted_gh.GitHubAccessError("unclassified", 7))
        self.assertIn("exit 7", message)
        self.assertIn("does not prove missing authentication", message)

    def test_run_command_uses_category_message(self):
        output = io.StringIO()
        error = trusted_gh.GitHubAccessError("network", 1)
        with patch.object(trusted_gh, "authenticated_account", side_effect=error):
            with patch.object(trusted_gh.sys, "argv",
                              ["trusted_gh.py", "run", "api", "user"]):
                with redirect_stderr(output):
                    result = trusted_gh.main()
        self.assertEqual(result, 1)
        self.assertIn("could not reach GitHub", output.getvalue())


class ProxyVariableNamesTest(unittest.TestCase):
    """Proxy diagnostics report names after placeholder removal."""

    def test_names_exclude_placeholder_and_values(self):
        names = trusted_gh.proxy_variable_names({
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": PROXY_SECRET,
            "PATH": "/usr/bin",
        })
        self.assertEqual(names, ["HTTPS_PROXY"])


if __name__ == "__main__":
    unittest.main()
