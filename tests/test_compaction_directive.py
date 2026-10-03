#!/usr/bin/env python3
"""Tests for the compaction-event directive in lifecycle reinjection."""
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "reinject_agents_policy.py"


def load_hook_module():
    """Import the lifecycle hook from its repository path."""
    spec = importlib.util.spec_from_file_location("reinject_agents_policy", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_hook(client: str, payload: dict) -> subprocess.CompletedProcess:
    """Run one client adapter with trusted project-root variables."""
    environment = dict(os.environ)
    environment["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT)
    environment["GEMINI_PROJECT_DIR"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH), "--client", client],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


class CompactionDetectionTest(unittest.TestCase):
    """The payload source field alone drives compaction detection."""

    def test_compact_source_is_detected(self):
        hook = load_hook_module()
        self.assertTrue(hook.is_compaction_event({"source": "compact"}))

    def test_other_sources_are_not_detected(self):
        hook = load_hook_module()
        for source in ("startup", "resume", "clear", "fork", "COMPACT", ""):
            with self.subTest(source=source):
                self.assertFalse(hook.is_compaction_event({"source": source}))

    def test_missing_source_is_not_detected(self):
        hook = load_hook_module()
        self.assertFalse(hook.is_compaction_event({}))


class ClaudeCompactionDirectiveTest(unittest.TestCase):
    """Claude session compaction prepends the directive to the notice."""

    def run_claude(self, source: str) -> str:
        """Run the Claude adapter for one session source and return context."""
        payload = {"hook_event_name": "SessionStart", "source": source}
        result = run_hook("claude", payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_compact_session_emits_directive(self):
        context = self.run_claude("compact")
        self.assertTrue(context.startswith("COMPACTION EVENT DETECTED"))
        self.assertIn("MANDATORY AGENTS.md RE-ADOPTION", context)
        self.assertIn("Disclose the complete compaction text", context)
        self.assertIn("Execution stays stopped", context)
        self.assertIn("Re-read the canonical AGENTS.md.", context)
        self.assertNotIn("reproduced below", context)

    def test_non_compact_sessions_emit_no_directive(self):
        for source in ("startup", "resume", "clear", "fork"):
            with self.subTest(source=source):
                context = self.run_claude(source)
                self.assertNotIn("COMPACTION EVENT DETECTED", context)


class CodexCompactionDirectiveTest(unittest.TestCase):
    """Codex session compaction prepends the directive to the policy."""

    @classmethod
    def setUpClass(cls):
        cls.complete_policy = load_hook_module().load_policy(REPO_ROOT)[0]

    def run_codex(self, source: str) -> str:
        """Run the Codex adapter for one session source and return context."""
        payload = {
            "hook_event_name": "SessionStart",
            "source": source,
            "cwd": str(REPO_ROOT),
        }
        result = run_hook("codex", payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_compact_session_emits_directive_with_complete_policy(self):
        context = self.run_codex("compact")
        self.assertTrue(context.startswith("COMPACTION EVENT DETECTED"))
        self.assertTrue(context.endswith(self.complete_policy))
        self.assertIn("SHA-256", context)
        self.assertIn("Re-read the canonical AGENTS.md.", context)
        self.assertNotIn("reproduced below", context)

    def test_non_compact_sessions_emit_no_directive(self):
        for source in ("startup", "resume", "clear"):
            with self.subTest(source=source):
                context = self.run_codex(source)
                self.assertNotIn("COMPACTION EVENT DETECTED", context)


if __name__ == "__main__":
    unittest.main()
