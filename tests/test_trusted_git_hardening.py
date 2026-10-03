#!/usr/bin/env python3
"""Fetch remotes, child environment, symlinked paths, and the identity checker source."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import trusted_git

IDENTITY_HOOK = REPO_ROOT / "hooks" / "enforce_git_identity.py"


def fetch_workspace(test_case) -> Path:
    """Return a workspace holding one repository directory named repo."""
    directory = tempfile.TemporaryDirectory()
    test_case.addCleanup(directory.cleanup)
    workspace = Path(directory.name)
    (workspace / "repo" / ".git").mkdir(parents=True)
    return workspace


class FetchRemoteTest(unittest.TestCase):
    """The fetch remote is a GitHub URL or a bare remote name."""

    def test_rejected_remotes(self):
        workspace = fetch_workspace(self)
        for remote in ("https://example.com/o/r.git", "file:///tmp/r", "ext::sh",
                       "/tmp/other", "../other", "git@example.com:o/r.git",
                       "http://github.com/o/r.git", "origin/main", ".hidden"):
            with self.subTest(remote=remote):
                self.assertIsNone(trusted_git._transport_arguments(
                    workspace, ["fetch", "repo", remote]))

    def test_accepted_remotes(self):
        workspace = fetch_workspace(self)
        repository = os.path.abspath(workspace / "repo")
        for remote in ("origin", "upstream", "fork-1.mirror_2",
                       "https://github.com/o/r.git", "git@github.com:o/r.git"):
            with self.subTest(remote=remote):
                self.assertEqual(
                    trusted_git._transport_arguments(
                        workspace, ["fetch", "repo", remote, "refs/pull/1/head"]),
                    ["-C", repository, "fetch", remote, "refs/pull/1/head"])


class ChildEnvironmentTest(unittest.TestCase):
    """Only allowlisted variables reach the Git child process."""

    def run_with_environment(self, environment: dict) -> dict:
        """Run trusted Git through a capturing runner and return its env."""
        captured = {}

        def runner(arguments, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch.dict(os.environ, environment, clear=False):
            with patch.object(trusted_git, "resolve_git", return_value=sys.executable):
                trusted_git.run_git(Path(tempfile.gettempdir()), ["status"], runner=runner)
        return captured["env"]

    def test_execution_hooks_are_dropped(self):
        names = ("GIT_SSH_COMMAND", "GIT_SSH", "GIT_ASKPASS", "GIT_EXEC_PATH",
                 "GIT_TEMPLATE_DIR", "GIT_PROXY_COMMAND", "LD_PRELOAD", "PYTHONPATH")
        environment = self.run_with_environment({name: "injected" for name in names})
        for name in names:
            with self.subTest(name=name):
                self.assertFalse(name in environment)

    def test_allowlisted_variables_are_kept(self):
        values = {"HTTPS_PROXY": "http://proxy.example.test:8080",
                  "SSL_CERT_FILE": "/etc/ssl/ca.pem", "LANG": "C.UTF-8"}
        environment = self.run_with_environment(values)
        for name, value in values.items():
            with self.subTest(name=name):
                self.assertTrue(environment.get(name) == value)

    def test_fixed_values_are_set(self):
        environment = self.run_with_environment({})
        self.assertEqual(environment.get("GIT_TERMINAL_PROMPT"), "0")
        self.assertEqual(environment.get("GIT_PAGER"), "")


class GitContextTest(unittest.TestCase):
    """A caller-supplied Git context passes only location and identity values."""

    def run_with_context(self, context: dict) -> dict:
        """Run trusted Git with one context and return the child env."""
        captured = {}

        def runner(arguments, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch.object(trusted_git, "resolve_git", return_value=sys.executable):
            trusted_git.run_git(Path(tempfile.gettempdir()), ["config", "--get", "user.email"],
                                runner=runner, git_context=context)
        return captured["env"]

    def test_location_and_identity_values_pass(self):
        environment = self.run_with_context({
            "GIT_DIR": "/work/other/.git", "GIT_WORK_TREE": "/work/other",
            "GIT_CONFIG_GLOBAL": "/work/absent", "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "evil",
            "GIT_CONFIG_KEY_1": "User.Email", "GIT_CONFIG_VALUE_1": "bad@corp.example",
            "GIT_CONFIG_KEY_2": "core.pager", "GIT_CONFIG_VALUE_2": "evil",
            "LD_PRELOAD": "evil", "GIT_SSH_COMMAND": "evil",
        })
        self.assertEqual(environment.get("GIT_DIR"), "/work/other/.git")
        self.assertEqual(environment.get("GIT_WORK_TREE"), "/work/other")
        self.assertEqual(environment.get("GIT_CONFIG_GLOBAL"), "/work/absent")
        self.assertEqual(environment.get("GIT_CONFIG_COUNT"), "1")
        self.assertEqual(environment.get("GIT_CONFIG_KEY_0"), "User.Email")
        self.assertEqual(environment.get("GIT_CONFIG_VALUE_0"), "bad@corp.example")
        for name in ("GIT_CONFIG_KEY_1", "GIT_CONFIG_KEY_2", "LD_PRELOAD", "GIT_SSH_COMMAND"):
            with self.subTest(name=name):
                self.assertFalse(name in environment)

    def test_malformed_pairs_and_nul_values_are_dropped(self):
        environment = self.run_with_context({
            "GIT_CONFIG_COUNT": "x", "GIT_CONFIG_KEY_0": "user.email",
            "GIT_CONFIG_VALUE_0": "a@b", "GIT_DIR": "bad\0dir",
        })
        self.assertFalse("GIT_CONFIG_COUNT" in environment)
        self.assertFalse("GIT_CONFIG_KEY_0" in environment)
        self.assertFalse("GIT_DIR" in environment)

    def test_no_context_passes_nothing(self):
        captured = {}

        def runner(arguments, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch.dict(os.environ, {"GIT_DIR": "/elsewhere", "GIT_CONFIG_GLOBAL": "/x"}):
            with patch.object(trusted_git, "resolve_git", return_value=sys.executable):
                trusted_git.run_git(Path(tempfile.gettempdir()), ["status"], runner=runner)
        self.assertFalse("GIT_DIR" in captured["env"])
        self.assertFalse("GIT_CONFIG_GLOBAL" in captured["env"])


class CheckerContextTest(unittest.TestCase):
    """The policy-root checker sees an inline identity through trusted Git."""

    def test_inline_user_email_reaches_the_checker(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        repo = root / "repo"
        absent = root / "absent-gitconfig"
        base = {key: value for key, value in os.environ.items()
                if not key.startswith(("GIT_", "EMAIL"))}
        base.update({"GIT_CONFIG_GLOBAL": str(absent), "GIT_CONFIG_SYSTEM": str(absent)})
        for args in (("init", "-q", str(repo)),
                     ("-C", str(repo), "config", "user.name", "octocat"),
                     ("-C", str(repo), "config", "user.email",
                      "1234567+octocat@users.noreply.github.com")):
            subprocess.run(["git", *args], env=base, check=True, capture_output=True)
        environment = dict(base, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="user.email",
                           GIT_CONFIG_VALUE_0="ada@example.com")
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check_git_identity.py")],
            cwd=repo, env=environment, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("ada@example.com", result.stderr)


class SymlinkPathTest(unittest.TestCase):
    """A path that traverses a symlink is not used."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        (self.workspace / "real" / ".git").mkdir(parents=True)
        try:
            os.symlink(self.workspace / "real", self.workspace / "link",
                       target_is_directory=True)
        except OSError:
            # Windows without symlink privilege cannot build this fixture.
            self.skipTest("symlink creation is unavailable on this host")

    def test_symlinked_fetch_repository_is_rejected(self):
        self.assertIsNone(trusted_git._transport_arguments(
            self.workspace, ["fetch", "link", "origin"]))

    def test_symlinked_clone_destination_is_rejected(self):
        self.assertIsNone(trusted_git._transport_arguments(
            self.workspace, ["clone", "https://github.com/o/r.git", "link/copy"]))

    def test_real_paths_still_pass(self):
        self.assertIsNotNone(trusted_git._transport_arguments(
            self.workspace, ["fetch", "real", "origin"]))


class IdentityCheckerSourceTest(unittest.TestCase):
    """The identity hook runs the policy-root checker, never the project copy."""

    def test_project_copy_is_not_executed(self):
        spec = importlib.util.spec_from_file_location("identity_checker_source", IDENTITY_HOOK)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        project = Path(directory.name)
        marker = project / "executed.txt"
        (project / "scripts").mkdir()
        (project / "scripts" / "check_git_identity.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
            encoding="utf-8")
        result = hook.run_checker(str(project), ["--advise"])
        self.assertIsNotNone(result)
        self.assertFalse(marker.exists())

    def test_absent_project_copy_still_skips(self):
        spec = importlib.util.spec_from_file_location("identity_checker_absent", IDENTITY_HOOK)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.assertIsNone(hook.run_checker(directory.name, ["--advise"]))


if __name__ == "__main__":
    unittest.main()
