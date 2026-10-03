#!/usr/bin/env python3
"""Tests for repository-only and adoptable policy content."""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = REPO_ROOT / "AGENTS.md"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import sync


class AdoptablePolicyTest(unittest.TestCase):
    """Adoption excludes source-repository orientation."""

    def test_filter_removes_repository_only_block(self):
        source = (
            "before\n"
            f"{sync.REPOSITORY_ONLY_START}\n"
            "local facts\n"
            f"{sync.REPOSITORY_ONLY_END}\n"
            "after\n"
        )
        self.assertEqual(sync.adoptable_content(source), "before\nafter\n")

    def test_filter_rejects_unbalanced_markers(self):
        source = f"before\n{sync.REPOSITORY_ONLY_START}\nlocal facts\n"
        with self.assertRaises(ValueError):
            sync.adoptable_content(source)

    def test_live_policy_marks_one_repository_only_block(self):
        policy = POLICY_PATH.read_text(encoding="utf-8")
        self.assertEqual(policy.count(sync.REPOSITORY_ONLY_START), 1)
        self.assertEqual(policy.count(sync.REPOSITORY_ONLY_END), 1)

    def test_adoptable_policy_keeps_orientation_template(self):
        policy = POLICY_PATH.read_text(encoding="utf-8")
        adoptable = sync.adoptable_content(policy)
        self.assertIn("<!-- Per-repo orientation.", adoptable)
        self.assertNotIn("## Repository-only orientation", adoptable)

    def test_adoptable_policy_removes_source_only_supporting_content(self):
        source_only = (
            f"{sync.SOURCE_ONLY_START}\n"
            "source facts\n"
            f"{sync.SOURCE_ONLY_END}\n"
        )
        policy = (
            f"before\n{sync.REPOSITORY_ONLY_START}\n"
            f"{sync.REPOSITORY_ONLY_END}\nafter\n{source_only}"
        )
        adoptable = sync.adoptable_content(policy)
        self.assertIn("after", adoptable)
        self.assertNotIn("source facts", adoptable)


if __name__ == "__main__":
    unittest.main()
