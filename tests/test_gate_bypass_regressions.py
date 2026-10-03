#!/usr/bin/env python3
"""Regression tests for the gate bypasses listed in the audit report.

Each case drives the real hook entrypoint through a persistent worker or a
subprocess. No case mocks the unit under test.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

# discover -s tests puts this directory on the path; a direct
# `unittest tests.<module>` run does not, and CI uses both.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_corpus
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASH_HOOK = REPO_ROOT / "hooks" / "block_destructive_bash.py"
POWERSHELL_HOOK = REPO_ROOT / "hooks" / "block_destructive_powershell.py"
CONSENT_HOOK = REPO_ROOT / "hooks" / "require_consent.py"
INTEGRITY_CHECKER = REPO_ROOT / "scripts" / "check_gate_pr_integrity.py"
WRAPPER = "python scripts/trusted_gh.py run "
_WORKERS = {}


def shell_verdict(hook_path: Path, tool_name: str, command: str) -> str:
    """Return the permission decision one shell gate gives `command`."""
    worker = _WORKERS.get(hook_path)
    if worker is None:
        worker = gate_corpus.HookWorker(hook_path)
        _WORKERS[hook_path] = worker
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "permission_mode": "default",
        "cwd": str(REPO_ROOT),
        "tool_input": {"command": command},
    }
    _code, stdout, _stderr = worker.invoke(payload)
    if not stdout.strip():
        return ""
    return json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]


def bash_verdict(command: str) -> str:
    """Return the Bash gate decision for `command`."""
    return shell_verdict(BASH_HOOK, "Bash", command)


class ExpectationTable(unittest.TestCase):
    """Assert one decision per command in a table."""

    def assert_verdicts(self, cases: tuple) -> None:
        """Check every (command, expected decision) pair."""
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(bash_verdict(command), expected)


class ProcessSubstitutionTest(ExpectationTable):
    """H1: a command inside <( ) or >( ) gets classified."""

    def test_substituted_commands_are_classified(self):
        self.assert_verdicts((
            ("cat <(rm -rf ~)", "deny"),
            ("echo x >(rm -rf build)", "ask"),
        ))


class GitMetadataWriteTest(ExpectationTable):
    """H2: shell writes to .git and policy copies route to consent."""

    def test_git_and_policy_writes_ask(self):
        self.assert_verdicts((
            ("echo x > .git/hooks/pre-commit", "ask"),
            ("tee -a .git/config", "ask"),
            ("echo x > AGENTS.md", "ask"),
        ))


class SecretExposureTest(ExpectationTable):
    """H3: environment dumps and credential files stay out of reach."""

    def test_environment_and_credentials_are_gated(self):
        self.assert_verdicts((
            ("env", "ask"),
            ("cat /proc/self/environ", "deny"),
            ("cat ~/.config/gh/hosts.yml", "deny"),
            ("cat ~/.git-credentials", "deny"),
        ))


class GitHubCommandTest(ExpectationTable):
    """H4: token output, hosted deletion, labels, and gists are gated."""

    def test_hosted_commands_are_gated(self):
        self.assert_verdicts((
            (WRAPPER + "auth status --show-token", "deny"),
            (WRAPPER + "auth status -t", "deny"),
            (WRAPPER + "issue delete 5 --yes", "deny"),
            (WRAPPER + "label create triage", "deny"),
            (WRAPPER + "gist create notes.txt", "ask"),
            (WRAPPER + "auth status", ""),
        ))


class AbbreviatedOptionTest(ExpectationTable):
    """H5: unique long-option prefixes read as the full option."""

    def test_abbreviations_match_full_options(self):
        self.assert_verdicts((
            ("git push --forc origin main", "ask"),
            ("git push --del origin feat/x", "ask"),
            ("git push --mirr origin", "ask"),
            ("git reset --har HEAD~1", "deny"),
            ("git commit --amen", "ask"),
            ("rm --recur -f build", "ask"),
        ))


class WindowsGitNameTest(ExpectationTable):
    """H6: git.exe and mixed case reach the git verdict."""

    def test_bash_normalizes_git_name(self):
        self.assert_verdicts((
            ("git.exe reset --hard", "deny"),
            ("Git reset --hard", "deny"),
        ))

    def test_powershell_normalizes_git_name(self):
        self.assertEqual(
            shell_verdict(POWERSHELL_HOOK, "PowerShell", "git.exe push --force"),
            "ask")


class GitDeletionTest(ExpectationTable):
    """H9: clean and forced branch deletion ask in every spelling."""

    def test_deletions_ask(self):
        self.assert_verdicts((
            ("git clean -f", "ask"),
            ("git clean --force", "ask"),
            ("git branch -d -f feat/x", "ask"),
            ("git branch -df feat/x", "ask"),
        ))

    def test_dry_runs_and_safe_deletes_pass(self):
        self.assert_verdicts((
            ("git clean -n", ""),
            ("git clean -fn", ""),
            ("git branch -d feat/merged", ""),
        ))


class GitAliasDefinitionTest(ExpectationTable):
    """H10: alias definitions deny through --file and set."""

    def test_alias_definitions_deny(self):
        self.assert_verdicts((
            ("git config --file .git/config alias.x status", "deny"),
            ("git config set alias.x status", "deny"),
        ))


class IntegrityScopeTest(unittest.TestCase):
    """H7: the label gate covers the denylist, checkers, and policy."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "gate_pr_integrity_regression", INTEGRITY_CHECKER)
        cls.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.checker)

    def test_protected_paths_require_approval(self):
        for path in ("scripts/banned_models.txt",
                     "scripts/check_compliance_tree.py",
                     "scripts/trusted_git.py", "scripts/trusted_gh.py",
                     "requirements-checkers.txt", "AGENTS.md",
                     "docs/agent-policy/github.md"):
            with self.subTest(path=path):
                self.assertTrue(self.checker.requires_approval([path]))

    def test_ordinary_paths_pass(self):
        self.assertFalse(self.checker.requires_approval(["README.md"]))


class PolicyEditConsentTest(unittest.TestCase):
    """H8: file-tool edits to agent policy text route to consent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "docs" / "agent-policy").mkdir(parents=True)
        (self.root / "src").mkdir()
        for relative in ("AGENTS.md", "CLAUDE.md",
                         "docs/agent-policy/github.md", "src/app.py"):
            (self.root / relative).write_text("text\n", encoding="utf-8")

    def decision(self, relative: str) -> str:
        """Return the consent hook decision for an Edit of `relative`."""
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "permission_mode": "default",
            "cwd": str(self.root),
            "tool_input": {"file_path": str(self.root / relative),
                           "old_string": "text", "new_string": "other"},
        }
        environment = dict(os.environ)
        environment.pop("CLAUDE_PROJECT_DIR", None)
        result = subprocess.run(
            [sys.executable, str(CONSENT_HOOK)], input=json.dumps(payload),
            capture_output=True, text=True, check=False, env=environment)
        if not result.stdout.strip():
            return ""
        parsed = json.loads(result.stdout)
        return parsed["hookSpecificOutput"]["permissionDecision"]

    def test_policy_edits_ask(self):
        for relative in ("AGENTS.md", "CLAUDE.md", "docs/agent-policy/github.md"):
            with self.subTest(path=relative):
                self.assertEqual(self.decision(relative), "ask")

    def test_source_edit_passes(self):
        self.assertEqual(self.decision("src/app.py"), "")


class ReviewFollowUpTest(ExpectationTable):
    """Review follow-up: nested policy paths, environ scope, Windows gh storage."""

    def test_nested_policy_writes_ask(self):
        self.assert_verdicts((
            ("echo x > docs/agent-policy/github.md", "ask"),
            ("tee .github/copilot-instructions.md", "ask"),
        ))

    def test_environ_marker_targets_proc_only(self):
        self.assert_verdicts((
            ("cat src/environ/app.py", ""),
            ("cat /proc/1/environ", "deny"),
            ("cat /proc/self/environ", "deny"),
        ))

    def test_windows_gh_credentials_deny(self):
        self.assert_verdicts((
            ('cat "C:/Users/u/AppData/Roaming/GitHub CLI/hosts.yml"', "deny"),
        ))


if __name__ == "__main__":
    unittest.main()
