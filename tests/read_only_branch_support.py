"""Shared fixture for read-only primary branch and master hook tests.

Each fixture owns an isolated repository, home directory, and temporary
directory. The hook runs as a subprocess against those roots. The real
home directory and the real checkout stay untouched.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.test_enforce_branch_name import HOOK_PATH, REPO_ROOT

DETACHED_HEAD = "HEAD"
BLOCKING_EXIT_CODE = 2
HOOK_TIMEOUT_SECONDS = 30
COPIED_SCRIPTS = (
    "run_tests.py", "read_git_state.py", "sync.py", "trusted_gh.py",
    "check_action_pins.py", "check_gate_adoption.py",
)


class BranchFixture:
    """Build one repository fixture and send payloads to the installed hook."""

    def __init__(self, test_case, branch: str):
        directories = [tempfile.TemporaryDirectory() for _ in range(3)]
        for directory in directories:
            test_case.addCleanup(directory.cleanup)
        self.root, self.home, self.temp = (
            Path(directory.name).resolve() for directory in directories)
        self.admin = self.root / ".git"
        (self.admin / "objects").mkdir(parents=True)
        (self.admin / "refs" / "heads").mkdir(parents=True)
        (self.admin / "config").write_text("[core]\nbare = false\n", encoding="utf-8")
        self.set_branch(branch)
        (self.root / "README.md").write_text("fixture\n", encoding="utf-8")
        (self.root / "scripts").mkdir()
        for name in COPIED_SCRIPTS:
            (self.root / "scripts" / name).write_bytes(
                (REPO_ROOT / "scripts" / name).read_bytes())
        self.global_config = self.temp / "global.config"
        self.global_config.write_text("", encoding="utf-8")
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("GIT_") and key != "GITHUB_HEAD_REF"
        }
        self.environment.update({
            "CLAUDE_PROJECT_DIR": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.global_config),
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "TMPDIR": str(self.temp),
            "TMP": str(self.temp),
            "TEMP": str(self.temp),
        })

    def set_branch(self, branch: str) -> None:
        """Point HEAD at a named branch or detach it."""
        head = "0" * 40 if branch == DETACHED_HEAD else "ref: refs/heads/" + branch
        (self.admin / "HEAD").write_text(head + "\n", encoding="utf-8")

    def add_local_branch(self, branch: str) -> None:
        """Create one loose local branch reference."""
        reference = self.admin / "refs" / "heads" / Path(*branch.split("/"))
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_text("0" * 40 + "\n", encoding="utf-8")

    def write_override(self, content: bytes) -> None:
        """Write hooks/primary-branch.txt with exact bytes."""
        hooks = self.root / "hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "primary-branch.txt").write_bytes(content)

    def run(self, tool_name: str, tool_input: dict, client: str = "claude",
            event: str = "PreToolUse", mode: str = "default") -> subprocess.CompletedProcess:
        """Send one tool call through the hook for the selected client."""
        payload = {"hook_event_name": event, "permission_mode": mode,
                   "tool_name": tool_name, "tool_input": tool_input,
                   "cwd": str(self.root)}
        if client == "antigravity":
            payload = {"workspacePaths": [str(self.root)],
                       "toolCall": {"name": tool_name, "args": tool_input}}
        return self.run_payload(payload, client)

    def run_payload(self, payload: dict, client: str = "claude") -> subprocess.CompletedProcess:
        """Send one raw payload through the hook."""
        return subprocess.run(
            [sys.executable, str(HOOK_PATH), "--client", client],
            input=json.dumps(payload), env=self.environment, capture_output=True,
            text=True, timeout=HOOK_TIMEOUT_SECONDS, check=False,
        )

    def shell(self, command: str, tool_name: str = "Bash", client: str = "claude"):
        """Send one shell command through the hook."""
        key = "CommandLine" if client == "antigravity" else "command"
        return self.run(tool_name, {key: command}, client)

    def lifecycle(self, event: str) -> subprocess.CompletedProcess:
        """Send one lifecycle event through the hook."""
        return self.run_payload({"hook_event_name": event, "cwd": str(self.root)})


def decision(result: subprocess.CompletedProcess, client: str = "claude") -> str:
    """Return allow, ask, or deny from one hook result."""
    if client in ("gemini", "antigravity"):
        if not result.stdout.strip():
            return "allow"
        return json.loads(result.stdout)["decision"]
    if result.returncode == BLOCKING_EXIT_CODE:
        return "deny"
    if not result.stdout.strip():
        return "allow"
    return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]
