"""Edge paths of the branch gate: malformed calls, broken metadata, and path checks.

Each class stays small because the hook coverage runner gives every test class
one shard with a fixed time limit.
"""
import os
import tempfile
import unittest
from pathlib import Path

from tests.read_only_branch_support import BranchFixture, decision
from tests.test_enforce_branch_name import hook


class MultilineAndIncompleteCommandTest(unittest.TestCase):
    """Multiline and unterminated commands fail on the primary branch."""

    def test_multiline_and_incomplete_commands_are_denied(self):
        fixture = BranchFixture(self, "main")
        for command in ("git status\ngit status", 'git log "unterminated'):
            with self.subTest(command=command):
                result = fixture.shell(command)
                self.assertEqual(decision(result), "deny", result.stdout)
                self.assertIn("permits read-only inspection", result.stderr)


class MasterMalformedCallTest(unittest.TestCase):
    """A tool call without a name fails on master."""

    def test_missing_tool_name_is_denied(self):
        fixture = BranchFixture(self, "master")
        result = fixture.run_payload({"hook_event_name": "PreToolUse",
                                      "tool_input": {"command": "git status"},
                                      "cwd": str(fixture.root)})
        self.assertEqual(decision(result), "deny")
        self.assertIn("Tool name is missing or malformed", result.stderr)


class BrokenGitPointerTest(unittest.TestCase):
    """A .git pointer to a missing directory fails closed."""

    def test_read_is_denied_when_gitdir_target_is_missing(self):
        fixture = BranchFixture(self, "main")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        project = Path(directory.name).resolve()
        (project / ".git").write_text("gitdir: missing\n", encoding="utf-8")
        (project / "README.md").write_text("fixture\n", encoding="utf-8")
        fixture.environment["CLAUDE_PROJECT_DIR"] = str(project)
        result = fixture.run_payload({"hook_event_name": "PreToolUse", "tool_name": "Read",
                                      "tool_input": {"file_path": str(project / "README.md")},
                                      "cwd": str(project)})
        self.assertEqual(decision(result), "deny", result.stdout)
        self.assertIn("rebase lookup failed", result.stderr)


class ContextEventValidBranchTest(unittest.TestCase):
    """A prompt on a compliant feature branch receives no context."""

    def test_user_prompt_on_valid_branch_is_silent(self):
        fixture = BranchFixture(self, "feat/valid-work")
        result = fixture.lifecycle("UserPromptSubmit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


class WithinPathTest(unittest.TestCase):
    """Containment checks reject paths that cannot share a root."""

    def test_relative_path_is_not_within_absolute_directory(self):
        directory = os.path.realpath(tempfile.gettempdir())
        self.assertTrue(hook._is_within(os.path.join(directory, "child"), directory))
        self.assertFalse(hook._is_within("relative", directory))


if __name__ == "__main__":
    unittest.main()
