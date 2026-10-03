#!/usr/bin/env python3
"""Test the agent-attributed prose gate: advisory for humans, blocking for disclosed agents."""
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
CHECKER_PATH = ROOT / "scripts" / "check_agent_prose_gate.py"
SYNC_CHECK_PATH = ROOT / ".github" / "workflows" / "sync-check.yml"


def load_checker():
    """Load the checker by path for isolated pure-function tests."""
    spec = importlib.util.spec_from_file_location(
        "check_agent_prose_gate", CHECKER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def commit(body: str) -> dict:
    """Return the bounded commit shape consumed by attribution detection."""
    return {"sha": "a" * 40, "body": body}


def write_event(
    path: Path, title: str = "chore: test", body: str = "", author: str = "octocat",
) -> None:
    """Write a minimal pull_request event payload for _load_event."""
    payload = {"pull_request": {"title": title, "body": body, "user": {"login": author}}}
    path.write_text(json.dumps(payload), encoding="utf-8")


class AttributionTest(unittest.TestCase):
    """A PR counts as agent-attributed through either a commit trailer or the PR body."""

    def test_no_disclosure_is_not_attributed(self):
        checker = load_checker()
        self.assertFalse(checker.is_agent_attributed([commit("plain change")], ""))

    def test_commit_trailer_is_attributed(self):
        checker = load_checker()
        body = "change\n\nAssisted-by: Claude Sonnet 5\n"
        self.assertTrue(checker.is_agent_attributed([commit(body)], ""))

    def test_pr_body_disclosure_alone_is_attributed(self):
        checker = load_checker()
        pr_body = "Summary\n\nAssisted-by: Claude Sonnet 5\n"
        self.assertTrue(checker.is_agent_attributed([commit("plain change")], pr_body))

    def test_empty_commit_range_and_no_pr_disclosure_is_not_attributed(self):
        checker = load_checker()
        self.assertFalse(checker.is_agent_attributed([], ""))


class DocFindingsTest(unittest.TestCase):
    """Doc-level findings reuse the same three analyzers the advisory checks call."""

    def test_clean_file_has_no_findings(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "AGENTS.md").write_text("Use active voice.\n", encoding="utf-8")
            with mock.patch.object(checker, "TARGET_FILES", ("AGENTS.md",)):
                findings = checker.find_doc_findings(tmp, checker.load_denylist())
        self.assertEqual(findings, [])

    def test_british_spelling_is_flagged(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "AGENTS.md").write_text(
                "Use British colour spelling.\n", encoding="utf-8"
            )
            with mock.patch.object(checker, "TARGET_FILES", ("AGENTS.md",)):
                findings = checker.find_doc_findings(tmp, checker.load_denylist())
        self.assertTrue(any("British spelling" in finding for finding in findings))


class PrFindingsTest(unittest.TestCase):
    """PR title/body findings reuse check_pull_request_message's own analyzers."""

    def test_clean_title_and_body_have_no_findings(self):
        checker = load_checker()
        findings = checker.find_pr_findings(
            "chore: add a feature", "Adds a feature.", "octocat", checker.load_denylist(),
        )
        self.assertEqual(findings, [])

    def test_malformed_title_is_flagged(self):
        checker = load_checker()
        findings = checker.find_pr_findings(
            "not a conventional title", "", "octocat", checker.load_denylist(),
        )
        self.assertTrue(any("title format" in finding for finding in findings))

    def test_dependabot_title_format_is_exempt(self):
        checker = load_checker()
        findings = checker.find_pr_findings(
            "Bump foo from 1 to 2", "", "dependabot[bot]", checker.load_denylist(),
        )
        self.assertFalse(any("title format" in finding for finding in findings))


class CheckOrchestrationTest(unittest.TestCase):
    """check() gates on attribution first and only blocks disclosed agent work."""

    def test_human_commit_with_violation_stays_advisory(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            write_event(event_path, body="")
            with mock.patch.object(
                checker, "load_commits", return_value=[commit("plain change")],
            ), mock.patch.object(
                checker, "find_doc_findings",
                side_effect=AssertionError("must not run for human-authored work"),
            ), mock.patch.object(
                checker, "find_pr_findings",
                side_effect=AssertionError("must not run for human-authored work"),
            ):
                result = checker.check("base", "head", tmp, str(event_path))
        self.assertEqual(result, 0)

    def test_agent_labeled_commit_with_violation_fails_the_gate(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            write_event(event_path, body="")
            agent_commit = commit("change\n\nAssisted-by: Claude Sonnet 5\n")
            with mock.patch.object(
                checker, "load_commits", return_value=[agent_commit],
            ), mock.patch.object(
                checker, "find_doc_findings",
                return_value=["warning: AGENTS.md:1: British spelling 'colour' (use 'color')"],
            ), mock.patch.object(
                checker, "find_pr_findings", return_value=[],
            ), mock.patch("builtins.print") as printed:
                result = checker.check("base", "head", tmp, str(event_path))
        self.assertEqual(result, 1)
        printed_lines = [call.args[0] for call in printed.call_args_list]
        self.assertIn(checker.ANTI_BYPASS_NOTICE, printed_lines)

    def test_agent_labeled_clean_commit_passes(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            write_event(event_path, body="")
            agent_commit = commit("change\n\nAssisted-by: Claude Sonnet 5\n")
            with mock.patch.object(
                checker, "load_commits", return_value=[agent_commit],
            ), mock.patch.object(
                checker, "find_doc_findings", return_value=[],
            ), mock.patch.object(
                checker, "find_pr_findings", return_value=[],
            ):
                result = checker.check("base", "head", tmp, str(event_path))
        self.assertEqual(result, 0)

    def test_pr_body_only_disclosure_triggers_the_gate(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            write_event(event_path, body="Summary\n\nAssisted-by: Claude Sonnet 5\n")
            with mock.patch.object(
                checker, "load_commits", return_value=[commit("plain change")],
            ), mock.patch.object(
                checker, "find_doc_findings",
                return_value=["warning: AGENTS.md:1: British spelling 'colour' (use 'color')"],
            ), mock.patch.object(
                checker, "find_pr_findings", return_value=[],
            ):
                result = checker.check("base", "head", tmp, str(event_path))
        self.assertEqual(result, 1)

    def test_empty_range_with_no_disclosure_exits_clean_without_reading_files(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            write_event(event_path, body="")
            with mock.patch.object(
                checker, "load_commits", return_value=[],
            ), mock.patch.object(
                checker, "find_doc_findings", side_effect=AssertionError("must not run"),
            ), mock.patch.object(
                checker, "find_pr_findings", side_effect=AssertionError("must not run"),
            ):
                result = checker.check("base", "head", tmp, str(event_path))
        self.assertEqual(result, 0)


class SyncCheckFileListTest(unittest.TestCase):
    """The gate's target-file list must track sync-check.yml's, not drift from it."""

    def test_target_files_match_sync_check_workflow(self):
        checker = load_checker()
        text = SYNC_CHECK_PATH.read_text(encoding="utf-8")
        match = re.search(r"run: python scripts/check_us_spelling\.py (.+)", text)
        self.assertIsNotNone(match, "sync-check.yml check_us_spelling.py invocation not found")
        workflow_files = tuple(match.group(1).split())
        self.assertEqual(checker.TARGET_FILES, workflow_files)


if __name__ == "__main__":
    unittest.main()
