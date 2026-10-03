#!/usr/bin/env python3
"""Exercise representative denials through fresh gate processes."""
import json
import subprocess
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
CASES = (
    ("block_destructive_bash.py", "Bash", "aws --version"),
    ("block_destructive_powershell.py", "PowerShell", "terraform validate"),
    ("block_destructive_cmd.py", "Cmd", "kubectl get pods"),
)
GITHUB_CASES = (
    ("python scripts/trusted_gh.py run secret list", "deny", 2),
    ("python scripts/trusted_gh.py run variable list", "deny", 2),
    ("python scripts/trusted_gh.py run repo delete OWNER/REPO", "deny", 2),
    ("python scripts/trusted_gh.py run auth status", "", 0),
    ("python scripts/trusted_gh.py run auth token", "deny", 2),
    ("python scripts/trusted_gh.py run auth login", "deny", 2),
    ("python scripts/trusted_gh.py run auth status --scopes repo", "deny", 2),
    ("python scripts/trusted_gh.py run auth -h github.example token", "deny", 2),
    ("python scripts/trusted_gh.py run auth -u octocat token", "deny", 2),
    ("python scripts/trusted_gh.py run api --method DELETE repos/OWNER/REPO",
     "deny", 2),
    ("python scripts/trusted_gh.py run api --method GET repos/OWNER/REPO", "", 0),
    ("python scripts/trusted_gh.py run --help", "", 0),
    ("python scripts/trusted_gh.py run pr create", "", 0),
    ("python scripts/trusted_gh.py run pr view 1", "", 0),
    ("python scripts/trusted_gh.py run pr merge 1", "deny", 2),
    ("python scripts/trusted_gh.py run repo archive OWNER/REPO", "deny", 2),
    ("python scripts/trusted_gh.py run repo edit OWNER/REPO --visibility public",
     "deny", 2),
    ("python scripts/trusted_gh.py run repo edit OWNER/REPO --visibility private",
     "ask", 0),
    ("python scripts/trusted_gh.py run pr comment 1 --repo outside-owner-x9/repo",
     "ask", 0),
    ("python scripts/trusted_gh.py run pr create --repo not-a-repository",
     "ask", 0),
    ("python scripts/trusted_gh.py run pr create --title Title outside-owner-x9/repo",
     "ask", 0),
    ("python scripts/trusted_gh.py run pr create --draft outside-owner-x9/repo",
     "ask", 0),
)


class FreshGateEntrypointTest(unittest.TestCase):
    """Fresh processes preserve denial output and exit status."""

    def test_representative_denials_exit_two(self):
        for hook_name, tool_name, command in CASES:
            with self.subTest(hook=hook_name):
                payload = {
                    "hook_event_name": "PreToolUse",
                    "permission_mode": "default",
                    "tool_name": tool_name,
                    "tool_input": {"command": command},
                }
                result = subprocess.run(
                    [sys.executable, str(REPOSITORY_ROOT / "hooks" / hook_name)],
                    cwd=REPOSITORY_ROOT,
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                output = json.loads(result.stdout)
                decision = output["hookSpecificOutput"]["permissionDecision"]
                self.assertEqual(decision, "deny")

    def test_github_routing_through_fresh_bash_processes(self):
        for command, expected_decision, expected_exit in GITHUB_CASES:
            with self.subTest(command=command):
                payload = {
                    "hook_event_name": "PreToolUse",
                    "permission_mode": "default",
                    "tool_name": "Bash",
                    "cwd": str(REPOSITORY_ROOT),
                    "tool_input": {"command": command},
                }
                result = subprocess.run(
                    [sys.executable,
                     str(REPOSITORY_ROOT / "hooks" / "block_destructive_bash.py")],
                    cwd=REPOSITORY_ROOT,
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, expected_exit, result.stderr)
                if not expected_decision:
                    self.assertEqual(result.stdout, "")
                    continue
                output = json.loads(result.stdout)
                decision = output["hookSpecificOutput"]["permissionDecision"]
                self.assertEqual(decision, expected_decision)


if __name__ == "__main__":
    unittest.main()
