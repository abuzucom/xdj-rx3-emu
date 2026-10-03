"""Check branch-gate boundaries through real payload and configuration paths."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_enforce_branch_name import HOOK_PATH, REPO_ROOT, hook

TIMEOUT_SECONDS = 10


class BranchBoundaryTest(unittest.TestCase):
    """Cover indirect metadata paths and opaque branch sources."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.admin = self.root / "administration"
        self.admin.mkdir()
        (self.admin / "HEAD").write_text("ref: refs/heads/fix/example\n", encoding="utf-8")
        (self.root / ".git").write_text("gitdir: administration\n", encoding="utf-8")

    def run_payload(self, tool, values):
        """Pass payload text to the hook without executing the requested action."""
        environment = dict(os.environ)
        environment["CLAUDE_PROJECT_DIR"] = str(self.root)
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": values}
        return subprocess.run(
            [sys.executable, str(HOOK_PATH)], input=json.dumps(payload),
            env=environment, capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, check=False,
        )

    def test_file_tool_protects_actual_worktree_head(self):
        result = self.run_payload("Write", {
            "file_path": str(self.admin / "HEAD"),
            "content": "ref: refs/heads/claude/x\n",
        })
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_shell_write_protects_actual_worktree_head(self):
        result = self.run_payload("Bash", {
            "command": "printf 'ref: refs/heads/claude/x' > administration/HEAD",
        })
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_file_tool_rejects_unresolved_metadata_content(self):
        result = self.run_payload("Write", {
            "file_path": ".git", "content": "gitdir: unknown-location\n",
        })
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_branch_stdin_is_not_a_literal_target(self):
        context = {"subcommand": "update-ref", "arguments": ["--stdin"]}
        self.assertTrue(hook._git_context_reason(context, str(self.root)))

    def test_branch_bootstrap_is_available_during_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory).resolve()
            (policy / ".git").mkdir()
            (policy / ".git" / "HEAD").write_text("ref: refs/heads/claude/x\n", encoding="utf-8")
            payload = {
                "hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "python scripts/read_git_state.py branch"},
            }
            # The installed root must match the command root before bootstrap approval.
            from unittest.mock import patch
            with patch.object(hook.core, "policy_root", return_value=str(policy)):
                result = hook._handle_invalid_branch(payload, str(policy), "claude", "invalid", "claude/x")
            self.assertEqual(result, 0)

    def test_long_literals_remain_bounded(self):
        for size in (1024, 8192, 32768):
            with self.subTest(size=size):
                result = self.run_payload("Bash", {"command": 'printf "' + "x" * size})
                self.assertEqual(result.returncode, 2)

    def test_checker_treats_option_like_branch_as_data(self):
        self.assertTrue(hook.check_branch("--help", project_dir=str(REPO_ROOT)))


if __name__ == "__main__":
    unittest.main()
