"""Exercise branch inspection side effects, metadata copies, and workflow consent."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_enforce_branch_name import HOOK_PATH, REPO_ROOT


class BranchReviewTest(unittest.TestCase):
    """Send candidate commands to the real hook without executing candidates."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.admin = self.root / ".git"
        (self.admin / "objects").mkdir(parents=True)
        (self.admin / "refs" / "heads").mkdir(parents=True)
        (self.admin / "HEAD").write_text("ref: refs/heads/fix/example\n", encoding="utf-8")
        (self.admin / "config").write_text("[core]\nbare = false\n", encoding="utf-8")
        self.global_config = self.root / "global.config"
        self.global_config.write_text("", encoding="utf-8")
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.environment.update({"CLAUDE_PROJECT_DIR": str(self.root), "GIT_CONFIG_NOSYSTEM": "1",
                                 "GIT_CONFIG_GLOBAL": str(self.global_config)})
        (self.root / "scripts").mkdir()
        for name in ("run_tests.py", "read_git_state.py", "trusted_gh.py", "sync.py"):
            (self.root / "scripts" / name).write_bytes((REPO_ROOT / "scripts" / name).read_bytes())
        (self.root / "Makefile").write_bytes((REPO_ROOT / "Makefile").read_bytes())

    def run_candidate(self, command: str, mode: str = "default") -> subprocess.CompletedProcess:
        """Invoke the hook with isolated repository metadata and configuration."""
        payload = {"hook_event_name": "PreToolUse", "permission_mode": mode,
                   "tool_name": "Bash", "tool_input": {"command": command}}
        return subprocess.run(
            [sys.executable, str(HOOK_PATH)], input=json.dumps(payload),
            env=self.environment, capture_output=True, text=True, timeout=15, check=False,
        )

    def test_rejected_commands_cannot_write_trace_or_redirect_files(self) -> None:
        destination = self.root / "output.txt"
        for variable in ("GIT_TRACE", "GIT_TRACE_SETUP", "GIT_TRACE2", "GIT_TRACE2_EVENT",
                         "GIT_TRACE2_PERF", "GIT_REDIRECT_STDOUT", "GIT_REDIRECT_STDERR"):
            with self.subTest(variable=variable):
                destination.write_text("unchanged", encoding="utf-8")
                command = f"{variable}='{destination.as_posix()}' git branch claude/x"
                result = self.run_candidate(command)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(destination.read_text(encoding="utf-8"), "unchanged")

    def test_configuration_trace_targets_cannot_create_files(self) -> None:
        for setting in ("normalTarget", "eventTarget", "perfTarget"):
            with self.subTest(setting=setting):
                destination = self.root / setting
                self.global_config.write_text(
                    f'[trace2]\n{setting} = "{destination.as_posix()}"\n', encoding="utf-8")
                result = self.run_candidate("git branch claude/x")
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertFalse(destination.exists())

    def test_safe_aliases_still_resolve_without_trace_side_effects(self) -> None:
        destination = self.root / "trace.txt"
        self.environment["GIT_TRACE2_EVENT"] = str(destination)
        self.global_config.write_text("[alias]\ninspect = status --short\n", encoding="utf-8")
        result = self.run_candidate("git inspect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(destination.exists())

    def test_directory_copy_destinations_protect_metadata_roots(self) -> None:
        for command in ("cp -r claude/ .git/refs/heads/", "cp -r heads .git/refs",
                        "cp -r payload .git/worktrees", "cp payload .git"):
            with self.subTest(command=command):
                result = self.run_candidate(command)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("metadata", result.stderr)
        result = self.run_candidate("cp notes.txt docs")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_linked_metadata_root_copy_is_protected(self) -> None:
        administration = self.root / "administration"
        administration.mkdir()
        (administration / "HEAD").write_text("ref: refs/heads/fix/example\n", encoding="utf-8")
        linked = self.root / "linked"
        linked.mkdir()
        (linked / ".git").write_text("gitdir: ../administration\n", encoding="utf-8")
        self.environment["CLAUDE_PROJECT_DIR"] = str(linked)
        result = self.run_candidate("cp -r payload ../administration")
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_named_copy_destinations_protect_metadata(self) -> None:
        for command in ("cp -rt .git/refs/heads claude/", "cp -t.git/refs/heads claude/",
                        "cp --target-directory=.git/refs/heads claude/",
                        "mv --target-dir .git/refs/heads claude/"):
            with self.subTest(command=command):
                self.assertEqual(self.run_candidate(command).returncode, 2)
        result = self.run_candidate("cp -t docs .git/HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_directory_copies_cannot_reach_metadata_through_ancestors(self) -> None:
        for command in ("cp -r payload/. .", "cp -r -- payload .", "mv payload/ .",
                        "cp -rt .. payload", "Copy-Item payload . -Recurse"):
            with self.subTest(command=command):
                result = self.run_candidate(command)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("metadata", result.stderr)

    def test_required_workflows_request_consent(self) -> None:
        for command in ("python scripts/run_tests.py", "python scripts/read_git_state.py all",
                        "python scripts/sync.py --check", "make lint PYTHON=python",
                        "python -m unittest tests.test_enforce_branch_name -v",
                        "python scripts/trusted_gh.py run pr list --repo abuzucom/agents",
                        "rg -n branch README.md"):
            with self.subTest(command=command):
                result = self.run_candidate(command)
                self.assertEqual(result.returncode, 0, result.stderr)
                decision = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]
                self.assertEqual(decision, "ask")

    def test_workflow_consent_never_clears_opaque_or_chained_commands(self) -> None:
        for command in ("python -c 'print(1)'", "python other.py", "make lint SHELL=sh",
                        "rg --pre=sh pattern README.md", "python scripts/run_tests.py; git branch claude/x",
                        "python scripts/run_tests.py > .git/HEAD",
                        "python scripts/trusted_gh.py run repo delete example/repo",
                        "python scripts/sync.py --unknown", "rg pattern README%EXPANSION%.md",
                        "rg pattern README!EXPANSION!.md"):
            with self.subTest(command=command):
                self.assertEqual(self.run_candidate(command).returncode, 2)
        self.assertEqual(self.run_candidate("python scripts/run_tests.py", mode="dontAsk").returncode, 2)

    def test_workflow_consent_uses_each_client_response(self) -> None:
        command = "python scripts/run_tests.py"
        for client in ("gemini", "antigravity", "codex"):
            with self.subTest(client=client):
                payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                           "tool_input": {"command": command}, "workspacePaths": [str(self.root)],
                           "toolCall": {"name": "run_command", "args": {"CommandLine": command}}}
                result = subprocess.run(
                    [sys.executable, str(HOOK_PATH), "--client", client], input=json.dumps(payload),
                    env=self.environment, capture_output=True, text=True, timeout=15, check=False)
                if client == "codex":
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertIn("execution consent", result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)["decision"], "ask")

    def test_ordinary_redirect_preserves_nonmetadata_writes(self) -> None:
        result = self.run_candidate("echo notes > notes.txt")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
