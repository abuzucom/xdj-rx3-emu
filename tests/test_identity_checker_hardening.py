#!/usr/bin/env python3
"""Identity checker Git context, standalone child environment, and checker source."""
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

import check_git_identity
import trusted_git

IDENTITY_HOOK = REPO_ROOT / "hooks" / "enforce_git_identity.py"
CHECKER = REPO_ROOT / "scripts" / "check_git_identity.py"
NOREPLY_EMAIL = "1234567+octocat@users.noreply.github.com"


def clean_environment(absent: Path) -> dict:
    """Return the process environment without Git or EMAIL variables."""
    base = {key: value for key, value in os.environ.items()
            if not key.startswith(("GIT_", "EMAIL"))}
    base.update({"GIT_CONFIG_GLOBAL": str(absent), "GIT_CONFIG_SYSTEM": str(absent)})
    return base


def make_repository(test_case) -> tuple:
    """Return a configured repository path and a clean environment for it."""
    directory = tempfile.TemporaryDirectory()
    test_case.addCleanup(directory.cleanup)
    root = Path(directory.name)
    repo = root / "repo"
    base = clean_environment(root / "absent-gitconfig")
    for args in (("init", "-q", str(repo)),
                 ("-C", str(repo), "config", "user.name", "octocat"),
                 ("-C", str(repo), "config", "user.email", NOREPLY_EMAIL)):
        subprocess.run(["git", *args], env=base, check=True, capture_output=True)
    return repo, base


def capture_config_parameters(repo: Path, environment: dict, settings: list) -> str:
    """Return the GIT_CONFIG_PARAMETERS value real Git builds for `-c` settings."""
    output = repo.parent / "parameters.txt"
    script = repo.parent / "capture.py"
    script.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(os.environ.get('GIT_CONFIG_PARAMETERS', ''),"
        " encoding='utf-8')\n",
        encoding="utf-8")
    alias = (f'alias.capture=!"{Path(sys.executable).as_posix()}" '
             f'"{script.as_posix()}" "{output.as_posix()}"')
    options = [part for setting in settings for part in ("-c", setting)]
    subprocess.run(["git", "-C", str(repo), *options, "-c", alias, "capture"],
                   env=environment, check=True, capture_output=True)
    return output.read_text(encoding="utf-8")


class PolicyRootCheckerTest(unittest.TestCase):
    """A missing policy-root checker skips even when the project opts in."""

    def test_missing_policy_root_checker_returns_none(self):
        spec = importlib.util.spec_from_file_location("identity_policy_root", IDENTITY_HOOK)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        project_directory = tempfile.TemporaryDirectory()
        self.addCleanup(project_directory.cleanup)
        empty_root = tempfile.TemporaryDirectory()
        self.addCleanup(empty_root.cleanup)
        project = Path(project_directory.name)
        marker = project / "executed.txt"
        (project / "scripts").mkdir()
        (project / "scripts" / "check_git_identity.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
            encoding="utf-8")
        with patch.object(hook.core, "policy_root", return_value=empty_root.name):
            self.assertIsNone(hook.run_checker(str(project), ["--advise"]))
        self.assertFalse(marker.exists())


class WindowsKeyCaseTest(unittest.TestCase):
    """Windows child environment keys are uppercase."""

    def test_keys_are_uppercased(self):
        with patch.object(trusted_git.os, "name", "nt"):
            environment = trusted_git._child_environment({"Temp": "t", "Path": "p"})
        self.assertEqual(environment, {"TEMP": "t"})


class ConfigParametersParserTest(unittest.TestCase):
    """GIT_CONFIG_PARAMETERS parses in both Git quoting forms."""

    def test_split_form(self):
        self.assertEqual(
            check_git_identity._parse_config_parameters(
                "'user.email'='ada@example.com' 'core.pager'='less'"),
            [("user.email", "ada@example.com"), ("core.pager", "less")])

    def test_joined_form(self):
        self.assertEqual(
            check_git_identity._parse_config_parameters("'user.email=ada@example.com'"),
            [("user.email", "ada@example.com")])

    def test_escapes(self):
        self.assertEqual(
            check_git_identity._parse_config_parameters(
                "'user.name'='O'\\''Hara'\\!'' 'user.useConfigOnly'"),
            [("user.name", "O'Hara!"), ("user.useConfigOnly", "true")])

    def test_empty_value(self):
        self.assertEqual(check_git_identity._parse_config_parameters(""), [])

    def test_malformed_values_raise(self):
        for value in ("user.email=a", "'user.email", "'user.email'=", "'a''b'",
                      "'user.email'=x", "'user.email'\\x"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    check_git_identity._parse_config_parameters(value)


class CheckerGitContextTest(unittest.TestCase):
    """The checker forwards location values and identity pairs only."""

    def test_context_keeps_identity_pairs_only(self):
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_DIR": "/work/other/.git", "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "evil",
            "GIT_CONFIG_KEY_1": "user.name", "GIT_CONFIG_VALUE_1": "Ada",
            "GIT_CONFIG_PARAMETERS":
                "'core.sshCommand'='evil' 'User.Email'='ada@example.com'",
        }
        with patch.dict(os.environ, environment, clear=True):
            context = check_git_identity._git_context()
        self.assertEqual(context, {
            "GIT_DIR": "/work/other/.git", "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "Ada",
            "GIT_CONFIG_KEY_1": "User.Email", "GIT_CONFIG_VALUE_1": "ada@example.com",
        })

    def test_malformed_parameters_raise(self):
        with patch.dict(os.environ, {"GIT_CONFIG_PARAMETERS": "'user.email"}, clear=True):
            with self.assertRaises(ValueError):
                check_git_identity._git_context()


class StandaloneEnvironmentTest(unittest.TestCase):
    """The standalone runner passes an allowlisted environment plus the context."""

    def test_standalone_child_environment(self):
        captured = {}

        def runner(arguments, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        injected = {"GIT_SSH_COMMAND": "evil", "LD_PRELOAD": "evil",
                    "GIT_ASKPASS": "evil", "LANG": "C.UTF-8"}
        context = {"GIT_DIR": "/work/other/.git", "GIT_CONFIG_COUNT": "2",
                   "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "evil",
                   "GIT_CONFIG_KEY_1": "user.email", "GIT_CONFIG_VALUE_1": "ada@example.com"}
        with patch.dict(os.environ, injected):
            check_git_identity._standalone_run_git(
                tempfile.gettempdir(), ["status"], runner=runner, git_context=context)
        environment = captured["env"]
        for name in ("GIT_SSH_COMMAND", "LD_PRELOAD", "GIT_ASKPASS"):
            with self.subTest(name=name):
                self.assertFalse(name in environment)
        self.assertEqual(environment.get("LANG"), "C.UTF-8")
        self.assertEqual(environment.get("GIT_DIR"), "/work/other/.git")
        self.assertEqual(environment.get("GIT_CONFIG_COUNT"), "1")
        self.assertEqual(environment.get("GIT_CONFIG_KEY_0"), "user.email")
        self.assertEqual(environment.get("GIT_CONFIG_VALUE_0"), "ada@example.com")
        self.assertFalse("GIT_CONFIG_KEY_1" in environment)


class ConfigParametersRoundTripTest(unittest.TestCase):
    """Real `git -c` settings reach the checker as identity pairs only."""

    def test_real_parameters_reach_the_checker(self):
        repo, base = make_repository(self)
        parameters = capture_config_parameters(
            repo, base, ["user.email=ada@example.com", "core.fsmonitor=evil"])
        with patch.dict(os.environ, dict(base, GIT_CONFIG_PARAMETERS=parameters),
                        clear=True):
            context = check_git_identity._git_context()
        self.assertEqual(context.get("GIT_CONFIG_COUNT"), "1")
        self.assertEqual(context.get("GIT_CONFIG_KEY_0").casefold(), "user.email")
        self.assertEqual(context.get("GIT_CONFIG_VALUE_0"), "ada@example.com")
        result = subprocess.run(
            [sys.executable, str(CHECKER)], cwd=repo,
            env=dict(base, GIT_CONFIG_PARAMETERS=parameters),
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("ada@example.com", result.stderr)

    def test_precedence_matches_git(self):
        repo, base = make_repository(self)
        environment = dict(
            base, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="user.email",
            GIT_CONFIG_VALUE_0="count@example.com",
            GIT_CONFIG_PARAMETERS="'user.email'='parameters@example.com'")
        expected = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "user.email"],
            env=environment, check=True, capture_output=True, text=True).stdout.strip()
        with patch.dict(os.environ, environment, clear=True):
            actual = trusted_git.run_git(
                repo, ["config", "--get", "user.email"],
                git_context=check_git_identity._git_context()).stdout.strip()
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
