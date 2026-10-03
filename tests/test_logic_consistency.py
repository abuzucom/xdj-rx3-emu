#!/usr/bin/env python3
"""Unit tests for logic consistency, precondition checks, and execution paths."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hooks import _gate_core as core
from scripts import check_commit_attribution
from scripts import check_compliance_tree
from scripts import check_conflict_markers


class TestLogicConsistency(unittest.TestCase):
    """Test resolution of contradictory logic and precondition checks."""

    def test_filesystem_roots_excludes_slash(self) -> None:
        """Verify FILESYSTEM_ROOTS does not redundantly contain slash."""
        self.assertNotIn("/", core.FILESYSTEM_ROOTS)
        self.assertTrue(core.is_root_target("/"))
        self.assertTrue(core.is_root_target("//"))
        self.assertTrue(core.is_root_target("/*"))

    def test_forge_verdict_does_not_repeat_github_cli_verdict(self) -> None:
        """Verify forge_verdict evaluates github_cli_verdict at most once."""
        args = ["scripts/trusted_gh.py", "run", "repo", "view"]
        with mock.patch.object(core, "github_cli_verdict", return_value=("", "")) as mock_verdict:
            decision, reason = core.forge_verdict("python", args, cwd=os.getcwd())
            self.assertEqual(decision, "")
            self.assertEqual(reason, "")
            self.assertEqual(mock_verdict.call_count, 1)

    def test_external_target_verdict_ignores_lowercase_r(self) -> None:
        """Verify gh pr create -r reviewer does not match repository target."""
        args = ["pr", "create", "-r", "external_org/team"]
        command = ["pr", "create", "-r", "external_org/team"]
        decision, reason = core._external_target_verdict(
            args, command, "pr", "create", repo_owner="local_org"
        )
        self.assertEqual(decision, "")
        self.assertEqual(reason, "")

    def test_external_target_verdict_matches_uppercase_r(self) -> None:
        """Verify gh pr create -R owner/repo identifies external repository."""
        args = ["pr", "create", "-R", "external_org/repo"]
        command = ["pr", "create"]
        decision, reason = core._external_target_verdict(
            args, command, "pr", "create", repo_owner="local_org"
        )
        self.assertEqual(decision, "ask")
        self.assertIn("external_org", reason)

    def test_compliance_tree_pull_target_handles_non_dict_jobs(self) -> None:
        """Verify _pull_target_violations handles null or invalid jobs mapping."""
        doc_null = {"on": "pull_request_target", "jobs": None}
        violations_null = check_compliance_tree._pull_target_violations(
            doc_null, "on: pull_request_target\njobs:\n", ".github/workflows/test.yml"
        )
        self.assertTrue(violations_null)

        doc_scalar = {"on": "pull_request_target", "jobs": "not-a-dict"}
        violations_scalar = check_compliance_tree._pull_target_violations(
            doc_scalar, "on: pull_request_target\njobs: invalid\n", ".github/workflows/test.yml"
        )
        self.assertTrue(violations_scalar)

    def test_identity_violation_guard_ordering(self) -> None:
        """Verify _identity_violation returns expected status with and without match."""
        role = "author"
        identity = {"login": "correctuser", "id": 12345}
        sha = "0123456789abcdef0123456789abcdef01234567"

        commit_mismatch = {
            "sha": sha,
            "commit": {"author": {"email": "12345+wronguser@users.noreply.github.com"}},
        }
        violation = check_commit_attribution._identity_violation(
            role, commit_mismatch, identity
        )
        self.assertIn("does not match GitHub identity", violation)

        commit_match = {
            "sha": sha,
            "commit": {"author": {"email": "12345+correctuser@users.noreply.github.com"}},
        }
        no_violation = check_commit_attribution._identity_violation(
            role, commit_match, identity
        )
        self.assertIsNone(no_violation)

        commit_public = {
            "sha": sha,
            "commit": {"author": {"email": "user@example.com"}},
        }
        skipped = check_commit_attribution._identity_violation(
            role, commit_public, identity
        )
        self.assertIsNone(skipped)

    def test_conflict_markers_check_all_skips_absent_tracked_files(self) -> None:
        """Verify _check_all skips worktree files absent from disk without error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            readable = [("present.txt", "sha1"), ("absent.txt", "sha2")]
            present_path = os.path.join(tmpdir, "present.txt")
            with open(present_path, "w", encoding="utf-8") as handle:
                handle.write("clean content\n")

            with mock.patch("scripts.check_conflict_markers._get_index_entries", return_value=[]), \
                 mock.patch("scripts.check_conflict_markers._partition_index_entries", return_value=([], readable)), \
                 mock.patch("scripts.check_conflict_markers._skip_worktree_paths", return_value=set()), \
                 mock.patch("scripts.check_conflict_markers.get_git_attributes", return_value={}), \
                 mock.patch("scripts.check_conflict_markers.check_file", return_value=[]):
                violations = check_conflict_markers._check_all(tmpdir)
                self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
