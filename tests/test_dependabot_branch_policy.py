#!/usr/bin/env python3
"""Tests for the narrow Dependabot branch-name exception."""
import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPLIANCE_PATH = REPO_ROOT / "scripts" / "check_compliance_tree.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "agents-compliance.yml"


def load_compliance_module():
    """Import immutable compliance orchestration by path."""
    spec = importlib.util.spec_from_file_location("check_compliance_tree", COMPLIANCE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EmptyBannedChecker:
    """Return no banned-agent findings for branch-policy isolation."""

    @staticmethod
    def find_violations(_commits, _author):
        """Return an empty finding list."""
        return []


class RecordingBranchChecker:
    """Record every branch sent to immutable branch validation."""

    def __init__(self):
        self.branches = []

    def find_violations(self, branch):
        """Record one branch and return a synthetic finding."""
        self.branches.append(branch)
        return ["invalid branch"]


class DependabotBranchPolicyTest(unittest.TestCase):
    """Dependabot skips branch shape without skipping other metadata checks."""

    def setUp(self):
        self.module = load_compliance_module()
        self.branch_checker = RecordingBranchChecker()
        self.checkers = {
            "check_banned_agents": EmptyBannedChecker(),
            "check_branch_name": self.branch_checker,
        }

    def test_dependabot_skips_immutable_branch_shape(self):
        values = {
            "--branch": "dependabot/pip/requests-3.0",
            "--pr-author": "dependabot[bot]",
        }
        violations = self.module._scan_metadata(values, REPO_ROOT, self.checkers)
        self.assertEqual(violations, [])
        self.assertEqual(self.branch_checker.branches, [])

    def test_other_authors_keep_branch_validation(self):
        values = {
            "--branch": "dependabot/pip/requests-3.0",
            "--pr-author": "octocat",
        }
        violations = self.module._scan_metadata(values, REPO_ROOT, self.checkers)
        self.assertEqual(violations, ["invalid branch"])
        self.assertEqual(self.branch_checker.branches, [values["--branch"]])

    def test_reusable_workflow_keeps_dependabot_exception(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        condition = "github.event.pull_request.user.login != 'dependabot[bot]'"
        self.assertIn(condition, workflow)


if __name__ == "__main__":
    unittest.main()
