#!/usr/bin/env python3
"""Cover advisory prose checks for commits and pull request metadata."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import check_commit_message
import check_pull_request_message


class CommitProseTest(unittest.TestCase):
    """Commit subjects and bodies share the prose policy."""

    def test_required_type_prefix_has_no_prose_finding(self):
        messages = [
            (
                "a" * 40,
                "fix: reject malformed records",
                "Reject malformed records before persistence.\n",
            )
        ]
        self.assertEqual(check_commit_message.find_message_violations(messages), [])

    def test_subject_and_body_findings_remain_advisory(self):
        messages = [
            (
                "a" * 40,
                "fix: improve validation",
                "The requester prefers serious validation.\n",
            )
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = check_commit_message.check_messages(messages)
        self.assertEqual(result, 0)
        self.assertIn("controlled vocabulary", output.getvalue())
        self.assertNotIn("serious", output.getvalue().lower())

    def test_commit_body_with_literal_escapes_warns(self):
        messages = [
            (
                "a" * 40,
                "fix: improve validation",
                "Summary\\n- Allow rebase recovery.\\n",
            )
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = check_commit_message.check_messages(messages)
        self.assertEqual(result, 0)
        self.assertIn("literal escape sequence", output.getvalue())

    def test_existing_subject_contract_remains_available(self):
        found = check_commit_message.find_violations(
            [("a" * 40, "missing prefix")]
        )
        self.assertEqual(len(found), 1)
        self.assertIn("must start with", found[0])


class PullRequestProseTest(unittest.TestCase):
    """Event JSON supplies pull request prose without shell interpolation."""

    def _write_event(self, payload: object) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "event.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_findings_return_zero_and_hide_raw_text(self):
        marker = "PRIVATE-MARKER"
        event = {
            "pull_request": {
                "title": "fix: improve validation",
                "body": f"{marker} This session used serious checks.",
                "draft": True,
                "user": {"login": "octocat"},
            }
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = check_pull_request_message.check_event(
                self._write_event(event)
            )
        self.assertEqual(result, 0)
        self.assertIn("warning", output.getvalue())
        self.assertNotIn(marker, output.getvalue())

    def test_required_title_prefix_has_no_prose_finding(self):
        event = {
            "pull_request": {
                "title": "fix: reject malformed records",
                "body": "Reject malformed records before persistence.",
                "draft": True,
                "user": {"login": "octocat"},
            }
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = check_pull_request_message.check_event(
                self._write_event(event)
            )
        self.assertEqual(result, 0)
        self.assertEqual(
            output.getvalue().strip(),
            "no pull-request prose findings found",
        )

    def test_null_body_passes(self):
        event = {
            "pull_request": {
                "title": "fix: improve validation",
                "body": None,
                "draft": False,
                "user": {"login": "octocat"},
            }
        }
        self.assertEqual(
            check_pull_request_message.check_event(self._write_event(event)),
            0,
        )

    def test_dependabot_skips_title_shape_only(self):
        event = {
            "pull_request": {
                "title": "Bump package from 1 to 2",
                "body": "A serious update.",
                "draft": False,
                "user": {"login": "dependabot[bot]"},
            }
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = check_pull_request_message.check_event(
                self._write_event(event)
            )
        self.assertEqual(result, 0)
        self.assertIn("controlled vocabulary", output.getvalue())
        self.assertNotIn("title format", output.getvalue())

    def test_pull_request_body_with_literal_escapes_warns(self):
        event = {
            "pull_request": {
                "title": "fix: reject malformed records",
                "body": "Summary\\n- Allow rebase recovery.\\n",
                "draft": True,
                "user": {"login": "octocat"},
            }
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = check_pull_request_message.check_event(
                self._write_event(event)
            )
        self.assertEqual(result, 0)
        self.assertIn("literal escape sequence", output.getvalue())

    def test_malformed_event_returns_one(self):
        event_path = self._write_event({"pull_request": {"title": 7}})
        self.assertEqual(check_pull_request_message.check_event(event_path), 1)


if __name__ == "__main__":
    unittest.main()
