"""Exercise branch enforcement with local metadata and hostile command data."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_enforce_branch_name import hook


class BranchValidationBypassTest(unittest.TestCase):
    """Keep branch evidence separate from unrelated command content."""

    def test_local_preflight_rejects_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            git_directory = Path(directory) / ".git"
            git_directory.mkdir()
            (git_directory / "HEAD").write_text(
                "ref: refs/heads/claude/blocked\n", encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_HEAD_REF": "feat/allowed"}):
                branch, violation = hook.read_branch_preflight(directory)
            self.assertTrue(violation)
            self.assertNotEqual(branch, "feat/allowed")

    def test_invocation_alias_rejects_prohibited_target(self):
        command = 'git -c alias.x="push origin claude/x" x'
        self.assertTrue(hook.command_names_prohibited_branch(command))

    def test_unrelated_incomplete_text_is_not_a_literal_branch_match(self):
        command = 'printf "claude/x'
        self.assertFalse(hook.command_names_prohibited_branch(command))

    def test_unrelated_incomplete_text_is_not_a_git_write(self):
        self.assertEqual(hook.blocked_command('printf "unfinished'), [])

    def test_incomplete_git_still_produces_a_blocking_context(self):
        contexts = hook.blocked_command('git push origin "unfinished')
        self.assertTrue(contexts)
        self.assertTrue(any(context.get("error") for context in contexts))

    def test_metadata_read_is_not_a_metadata_write(self):
        command = 'printf "%s" ".git/HEAD claude/x"'
        self.assertFalse(hook.command_names_prohibited_metadata(command))

    def test_document_content_is_not_a_metadata_destination(self):
        payload = {
            "file_path": "notes.txt",
            "content": "Example .git/HEAD with ref: refs/heads/claude/x",
        }
        self.assertFalse(hook.file_write_names_prohibited_metadata(payload, "."))

    def test_target_checkout_cannot_supply_checker_code(self):
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory) / "scripts"
            scripts.mkdir()
            (scripts / "check_branch_name.py").write_text(
                "raise SystemExit(0)\n", encoding="utf-8")
            violation = hook.check_branch("claude/x", project_dir=directory)
            self.assertTrue(violation)

    def test_recovery_rejects_chained_execution(self):
        self.assertFalse(hook._valid_recovery(
            "git branch -m fix/recovered; git push origin claude/x",
            "claude/x", os.getcwd()))


if __name__ == "__main__":
    unittest.main()
