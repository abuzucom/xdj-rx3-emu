"""Verify complete metadata destinations and effective repository discovery."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_enforce_branch_name import HOOK_PATH


class BranchWriteDestinationTest(unittest.TestCase):
    """Exercise real hook decisions with isolated repository metadata."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.admin = self.root / ".git"
        (self.admin / "objects").mkdir(parents=True)
        (self.admin / "refs").mkdir()
        (self.admin / "HEAD").write_text("ref: refs/heads/fix/example\n", encoding="utf-8")
        (self.admin / "config").write_text("[core]\nbare = false\n", encoding="utf-8")
        (self.root / "empty.config").write_text("", encoding="utf-8")
        (self.root / "src" / "nested").mkdir(parents=True)
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.environment.update({"CLAUDE_PROJECT_DIR": str(self.root), "GIT_CONFIG_NOSYSTEM": "1",
                                 "GIT_CONFIG_GLOBAL": str(self.root / "empty.config")})

    def run_candidate(self, command: str, tool: str = "Bash") -> subprocess.CompletedProcess:
        """Pass candidate text to the hook without executing the candidate."""
        payload = {"hook_event_name": "PreToolUse", "permission_mode": "default",
                   "tool_name": tool, "tool_input": {"command": command}}
        return subprocess.run(
            [sys.executable, str(HOOK_PATH)], input=json.dumps(payload), env=self.environment,
            capture_output=True, text=True, timeout=15, check=False,
        )

    def test_tee_checks_every_destination(self) -> None:
        for targets in ("notes.txt .git/HEAD", ".git/HEAD notes.txt", "-a notes.txt .git/HEAD",
                        "-- notes.txt .git/HEAD", "notes.txt .git/refs/heads/claude/x"):
            with self.subTest(targets=targets):
                result = self.run_candidate("printf 'ref: refs/heads/claude/x' | tee " + targets)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("metadata", result.stderr)

    def test_powershell_checks_each_path_array_element(self) -> None:
        for arguments in ("-Path .git/HEAD,notes.txt", "-Path notes.txt, .git/HEAD",
                          "notes.txt,.git/HEAD", "-LiteralPath notes.txt,'.git/HEAD'",
                          "-Path:notes.txt,.git/HEAD", r"-Path notes.txt,.git\HEAD"):
            with self.subTest(arguments=arguments):
                command = "Set-Content " + arguments + " -Value 'ref: refs/heads/claude/x'"
                result = self.run_candidate(command, "PowerShell")
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("metadata", result.stderr)

    def test_powershell_arrays_do_not_confuse_values_and_destinations(self) -> None:
        for command in (
            "Write-Output 'notes.txt,other.txt'",
            "Set-Content -Path notes.txt,other.txt -Value 'ref: refs/heads/claude/x'",
            "Set-Content -Path 'notes.txt,.git/HEAD' -Value 'ref: refs/heads/claude/x'",
            "Set-Content -Value .git/HEAD,notes.txt -Path report.txt",
            "Copy-Item -Path .git/HEAD,notes.txt -Destination backup",
        ):
            with self.subTest(command=command):
                result = self.run_candidate(command, "PowerShell")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_tee_preserves_ordinary_multiple_outputs(self) -> None:
        result = self.run_candidate("printf text | tee -a -- notes.txt other.txt")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_incomplete_powershell_destination_arrays_fail_closed(self) -> None:
        for command in ("Set-Content -Path , notes.txt", "Set-Content -Path notes.txt,",
                        "Set-Content -Path notes.txt, ,other.txt"):
            with self.subTest(command=command):
                result = self.run_candidate(command, "PowerShell")
                self.assertEqual(result.returncode, 2)

    def test_git_reads_discover_repository_from_subdirectories(self) -> None:
        for command in ("git -C src status", "git -C src/nested status", "git -C src -C nested status"):
            with self.subTest(command=command):
                result = self.run_candidate(command)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_subdirectory_discovery_preserves_invalid_branch_denial(self) -> None:
        nested_admin = self.root / "src" / ".git"
        (nested_admin / "objects").mkdir(parents=True)
        (nested_admin / "refs").mkdir()
        (nested_admin / "HEAD").write_text("ref: refs/heads/claude/x\n", encoding="utf-8")
        (nested_admin / "config").write_text("[core]\nbare = false\n", encoding="utf-8")
        result = self.run_candidate("git -C src/nested status")
        self.assertEqual(result.returncode, 2)

    def test_subdirectory_discovery_resolves_linked_worktree(self) -> None:
        linked = self.root / "linked"
        (linked / "src").mkdir(parents=True)
        (linked / ".git").write_text("gitdir: ../.git\n", encoding="utf-8")
        result = self.run_candidate("git -C linked/src status")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_repository_discovery_denies_missing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = "git -C '" + Path(directory).as_posix() + "' status"
            result = self.run_candidate(command)
        self.assertEqual(result.returncode, 2)
        self.assertIn("repository metadata was not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
