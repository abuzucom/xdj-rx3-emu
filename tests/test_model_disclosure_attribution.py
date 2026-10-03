#!/usr/bin/env python3
"""Test that agent-labeled commits disclose a versioned model.

Commits with no agent label stay outside this check, so human commits
without trailers pass unchanged.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CHECKER_PATH = ROOT / "scripts" / "check_commit_attribution.py"


def load_checker():
    """Load the checker by path for isolated pure-function tests."""
    spec = importlib.util.spec_from_file_location(
        "check_commit_attribution_disclosure", CHECKER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def commit(body: str) -> dict:
    """Return the bounded commit shape consumed by trailer validation."""
    return {"sha": "b" * 40, "body": body}


class UnversionedModelTest(unittest.TestCase):
    """An Assisted-by value must name a versioned model."""

    def test_vendor_or_tool_name_is_rejected(self):
        checker = load_checker()
        for value in ("Claude", "Claude Code", "Codex", "Gemini", "Antigravity"):
            with self.subTest(value=value):
                body = f"fix: change\n\nAssisted-by: {value}\n"
                violations = checker.model_disclosure_violations([commit(body)])
                self.assertEqual(len(violations), 1)
                self.assertIn("must name a versioned model", violations[0])

    def test_versioned_model_passes(self):
        checker = load_checker()
        for value in ("Claude Opus 5.5", "claude-opus-5-5", "GPT-5.3-Codex", "Gemini 3 Pro"):
            with self.subTest(value=value):
                body = (
                    f"fix: change\n\nAssisted-by: {value}\n"
                    "Co-authored-by: Claude Code\n"
                )
                self.assertEqual(checker.model_disclosure_violations([commit(body)]), [])

    def test_spaced_key_spelling_is_checked(self):
        checker = load_checker()
        body = "fix: change\n\nAssisted by: Claude\n"
        self.assertEqual(len(checker.model_disclosure_violations([commit(body)])), 1)

    def test_violation_text_strips_control_characters(self):
        checker = load_checker()
        body = "fix: change\n\nAssisted-by: Claude\x1b\x07\n"
        violations = checker.model_disclosure_violations([commit(body)])
        self.assertEqual(len(violations), 1)
        self.assertNotIn("\x1b", violations[0])


class MissingDisclosureTest(unittest.TestCase):
    """A name-only agent label requires an Assisted-by disclosure."""

    def test_agent_label_without_assisted_by_is_rejected(self):
        checker = load_checker()
        body = "fix: change\n\nCo-authored-by: Claude Code\n"
        violations = checker.model_disclosure_violations([commit(body)])
        self.assertEqual(len(violations), 1)
        self.assertIn("requires an Assisted-by model disclosure", violations[0])


class HumanCommitTest(unittest.TestCase):
    """Commits with no agent label never fail this check."""

    def test_commit_without_trailers_passes(self):
        checker = load_checker()
        body = "fix: change\n\nExplain the change in the body.\n"
        self.assertEqual(checker.model_disclosure_violations([commit(body)]), [])

    def test_subject_only_commit_passes(self):
        checker = load_checker()
        self.assertEqual(checker.model_disclosure_violations([commit("fix: change\n")]), [])

    def test_non_agent_trailers_pass(self):
        checker = load_checker()
        body = "fix: change\n\nSigned-off-by: Ada Lovelace <ada@example.com>\n"
        self.assertEqual(checker.model_disclosure_violations([commit(body)]), [])

    def test_approved_human_coauthor_passes(self):
        checker = load_checker()
        checker.APPROVED_HUMAN_COAUTHORS = frozenset({("Ada Lovelace", "ada@example.com")})
        body = "fix: change\n\nCo-authored-by: Ada Lovelace <ada@example.com>\n"
        self.assertEqual(checker.model_disclosure_violations([commit(body)]), [])


class CommandLineTest(unittest.TestCase):
    """The CLI fails on a bare vendor name and passes a human message."""

    def run_checker(self, body: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as directory:
            message = Path(directory) / "COMMIT_EDITMSG"
            message.write_text(body, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(CHECKER_PATH), "--message-file", str(message)],
                capture_output=True, text=True, timeout=30,
                cwd=str(ROOT), env=dict(os.environ),
            )

    def test_bare_vendor_name_fails(self):
        result = self.run_checker(
            "fix: change\n\nAssisted-by: Claude\nCo-authored-by: Claude Code\n"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("must name a versioned model", result.stderr)

    def test_human_message_passes(self):
        result = self.run_checker("fix: change\n\nExplain the change.\n")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
