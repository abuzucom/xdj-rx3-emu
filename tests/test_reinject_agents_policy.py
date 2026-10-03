#!/usr/bin/env python3
"""Tests for complete AGENTS.md lifecycle reinjection."""
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "reinject_agents_policy.py"
POLICY_PATH = REPO_ROOT / "AGENTS.md"
CLAUDE_SETTINGS = REPO_ROOT / ".claude" / "settings.json"
CODEX_HOOKS = REPO_ROOT / ".codex" / "hooks.json"
GEMINI_SETTINGS = REPO_ROOT / ".gemini" / "settings.json"
ANTIGRAVITY_HOOKS = REPO_ROOT / ".agents" / "hooks.json"


def load_hook_module():
    """Import the lifecycle hook from its repository path."""
    spec = importlib.util.spec_from_file_location("reinject_agents_policy", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_hook(client: str, payload: dict, *arguments: str) -> subprocess.CompletedProcess:
    """Run one client adapter with trusted project-root variables."""
    environment = dict(os.environ)
    environment["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT)
    environment["GEMINI_PROJECT_DIR"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH), "--client", client, *arguments],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def run_configured_command(command: str, payload: dict) -> subprocess.CompletedProcess:
    """Run one Antigravity command from its configuration directory."""
    arguments = shlex.split(command)
    if arguments and arguments[0] == "python":
        arguments[0] = sys.executable
    environment = dict(os.environ)
    environment["GITHUB_HEAD_REF"] = "feat/valid-branch"
    return subprocess.run(
        arguments,
        cwd=ANTIGRAVITY_HOOKS.parent,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


class PolicyContentTest(unittest.TestCase):
    """Every adapter receives the complete canonical policy."""

    @classmethod
    def setUpClass(cls):
        cls.hook = load_hook_module()
        cls.policy = POLICY_PATH.read_text(encoding="utf-8")
        cls.complete_policy, _digest = cls.hook.load_policy(REPO_ROOT)

    def test_claude_chunks_reconstruct_exact_policy(self):
        chunks = self.hook.split_policy(self.policy, self.hook.CLAUDE_CHUNK_COUNT)
        self.assertEqual("".join(chunks), self.policy)
        self.assertTrue(all(len(chunk) <= self.hook.MAX_CHUNK_CHARS for chunk in chunks))

    def test_codex_receives_complete_policy(self):
        payload = {"hook_event_name": "SessionStart", "cwd": str(REPO_ROOT)}
        result = run_hook("codex", payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context.endswith(self.complete_policy))

    def test_gemini_preserves_request_and_adds_policy(self):
        payload = {
            "hook_event_name": "BeforeModel",
            "cwd": str(REPO_ROOT),
            "llm_request": {
                "model": "gemini-test",
                "messages": [{"role": "user", "content": "hello"}],
                "config": {"temperature": 0},
            },
        }
        result = run_hook("gemini", payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        request = json.loads(result.stdout)["hookSpecificOutput"]["llm_request"]
        self.assertEqual(request["messages"][1]["content"], "hello")
        self.assertTrue(request["messages"][0]["content"].endswith(self.complete_policy))

    def test_antigravity_receives_ephemeral_policy(self):
        payload = {
            "conversationId": "test-conversation",
            "workspacePaths": [str(REPO_ROOT)],
            "invocationNum": 0,
        }
        result = run_hook("antigravity", payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        message = json.loads(result.stdout)["injectSteps"][0]["ephemeralMessage"]
        self.assertTrue(message.endswith(self.complete_policy))

    def test_antigravity_pre_tool_use_output_is_schema_safe(self):
        payload = {
            "hook_event_name": "PreToolUse",
            "workspacePaths": [str(REPO_ROOT)],
        }
        result = run_hook("antigravity", payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output, {})
        self.assertNotIn("injectSteps", output)

    def test_claude_emits_session_context_and_numbered_chunks(self):
        session = run_hook("claude", {"hook_event_name": "SessionStart"})
        self.assertEqual(session.returncode, 0, session.stderr)
        session_output = json.loads(session.stdout)["hookSpecificOutput"]
        self.assertEqual(session_output["hookEventName"], "SessionStart")
        self.assertIn("SHA-256", session_output["additionalContext"])

        subagent = run_hook(
            "claude", {"hook_event_name": "SubagentStart"},
            "--chunk-index", "0")
        self.assertEqual(subagent.returncode, 0, subagent.stderr)
        context = json.loads(subagent.stdout)["hookSpecificOutput"]
        self.assertIn("CHUNK 1/8", context["additionalContext"])

    def test_gemini_session_receives_complete_policy(self):
        payload = {"hook_event_name": "SessionStart", "cwd": str(REPO_ROOT)}
        result = run_hook("gemini", payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertTrue(context["additionalContext"].endswith(self.complete_policy))


class PolicyValidationTest(unittest.TestCase):
    """Malformed roots, files, payloads, and chunk requests fail closed."""

    @classmethod
    def setUpClass(cls):
        cls.hook = load_hook_module()

    def make_project(self, policy: bytes = b"policy\n") -> Path:
        """Create a temporary project with synchronized policy copies."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / ".git").mkdir()
        (root / "AGENTS.md").write_bytes(policy)
        (root / "CLAUDE.md").write_bytes(policy)
        for relative_name in self.hook.SUPPORTING_POLICY_FILES:
            path = root / relative_name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("detail\n", encoding="utf-8")
        return root

    def test_root_search_rejects_files_and_missing_projects(self):
        root = self.make_project()
        nested = root / "nested"
        nested.mkdir()
        self.assertEqual(self.hook.find_project_root(str(nested)), root.resolve())
        with self.assertRaises(ValueError):
            self.hook.find_project_root(str(root / "AGENTS.md"))

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with self.assertRaises(ValueError):
            self.hook.find_project_root(directory.name)

    def test_client_root_validation_rejects_missing_locations(self):
        with self.assertRaises(ValueError):
            self.hook.project_root("antigravity", {"workspacePaths": []})
        with self.assertRaises(ValueError):
            self.hook.project_root("codex", {"cwd": ""})

    def test_policy_validation_rejects_size_and_encoding(self):
        oversized = self.make_project(
            b"x" * (self.hook.MAX_POLICY_BYTES + 1))
        with self.assertRaises(ValueError):
            self.hook.load_policy(oversized)
        non_ascii = self.make_project(b"\xc3\xa9\n")
        with self.assertRaises(UnicodeEncodeError):
            self.hook.load_policy(non_ascii)

    def test_installed_root_rejects_an_incomplete_install(self):
        with patch.object(self.hook.Path, "is_file", return_value=False):
            with self.assertRaisesRegex(ValueError, "installed policy root"):
                self.hook.installed_root()

    def test_chunk_validation_rejects_invalid_policy_shapes(self):
        long_line = "x" * (self.hook.MAX_CHUNK_CHARS + 1)
        with self.assertRaises(ValueError):
            self.hook.split_policy(long_line, self.hook.CLAUDE_CHUNK_COUNT)
        two_chunks = ("x" * 5000 + "\n") * 2
        with self.assertRaises(ValueError):
            self.hook.split_policy(two_chunks, 1)

    def test_claude_rejects_an_unsynchronized_policy_copy(self):
        root = self.make_project()
        (root / "CLAUDE.md").write_text("different\n", encoding="utf-8")
        args = Namespace(client="claude", chunk_index=0)
        with patch.object(self.hook, "installed_root", return_value=root.resolve()):
            with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": ""}):
                with self.assertRaisesRegex(ValueError, "not synchronized"):
                    self.hook.run_hook(args, {"cwd": str(root)})

    def test_client_root_rejects_a_different_repository(self):
        root = self.make_project()
        other = self.make_project()
        with patch.object(self.hook, "installed_root", return_value=root):
            with self.assertRaisesRegex(ValueError, "installed policy root"):
                self.hook.project_root("codex", {"cwd": str(other)})

    def test_cli_rejects_malformed_json(self):
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--client", "codex"],
            input="{", capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Restore readable AGENTS.md", result.stderr)

    def test_cli_rejects_non_object_payload(self):
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--client", "codex"],
            input="[]", capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("payload is not an object", result.stderr)

    def test_claude_rejects_an_invalid_chunk_index(self):
        payload = {"hook_event_name": "SubagentStart"}
        result = run_hook("claude", payload, "--chunk-index", "8")
        self.assertEqual(result.returncode, 2)
        self.assertIn("chunk index is outside", result.stderr)

    def test_gemini_rejects_malformed_model_messages(self):
        payload = {
            "hook_event_name": "BeforeModel",
            "cwd": str(REPO_ROOT),
            "llm_request": {"messages": "invalid"},
        }
        result = run_hook("gemini", payload)
        self.assertEqual(result.returncode, 2)
        self.assertIn("malformed messages", result.stderr)


class LifecycleWiringTest(unittest.TestCase):
    """Committed client settings must activate every lifecycle adapter."""

    def test_claude_registers_compact_and_subagent_chunks(self):
        settings = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
        starts = settings["hooks"]["SessionStart"]
        self.assertIn("compact", starts[0]["matcher"])
        subagents = settings["hooks"]["SubagentStart"]
        commands = [hook for group in subagents for hook in group["hooks"]]
        self.assertEqual(len(commands), load_hook_module().CLAUDE_CHUNK_COUNT)

    def test_codex_disables_context_spilling(self):
        settings = json.loads(CODEX_HOOKS.read_text(encoding="utf-8"))
        starts = settings["hooks"]["SessionStart"][0]["hooks"]
        self.assertEqual(starts[0]["additionalContextLimit"], 0)
        self.assertIn("SubagentStart", settings["hooks"])

    def test_gemini_registers_before_every_model(self):
        settings = json.loads(GEMINI_SETTINGS.read_text(encoding="utf-8"))
        self.assertIn("BeforeModel", settings["hooks"])
        self.assertIn("BeforeTool", settings["hooks"])

    def test_antigravity_registers_every_invocation(self):
        settings = json.loads(ANTIGRAVITY_HOOKS.read_text(encoding="utf-8"))
        policy = settings["agents-policy"]
        self.assertIn("PreInvocation", policy)
        self.assertIn("PreToolUse", policy)

    def test_antigravity_commands_launch_from_config_directory(self):
        settings = json.loads(ANTIGRAVITY_HOOKS.read_text(encoding="utf-8"))
        policy = settings["agents-policy"]
        cases = (
            (
                policy["PreInvocation"][0]["command"],
                {
                    "conversationId": "test-conversation",
                    "workspacePaths": [str(REPO_ROOT)],
                    "invocationNum": 0,
                },
            ),
            (
                policy["PreToolUse"][0]["hooks"][0]["command"],
                {
                    "toolCall": {
                        "name": "view_file",
                        "args": {"AbsolutePath": "README.md"},
                    },
                    "workspacePaths": [str(REPO_ROOT)],
                },
            ),
        )
        for command, payload in cases:
            with self.subTest(command=command):
                result = run_configured_command(command, payload)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
