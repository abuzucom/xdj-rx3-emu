#!/usr/bin/env python3
"""Cover the euler pull request quality review wiring."""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "quality-review-pr.yml"
PINNED_COMMIT = "b23947068a328c19215f8ade3db7bd2b4bcb05cc"
FULL_SHA = re.compile(r"[0-9a-f]{40}")


class QualityReviewWiringTest(unittest.TestCase):
    """Keep the euler caller workflow pinned, scoped, and fail-closed."""

    def setUp(self):
        self.text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.text)

    def test_workflow_parses_and_exists(self):
        self.assertTrue(WORKFLOW_PATH.is_file())
        self.assertIsInstance(self.workflow, dict)

    def test_triggers_after_ci(self):
        # PyYAML 1.1 parses a bare `on:` key as the boolean True, so match the
        # trigger block as raw text instead of indexing the parsed mapping.
        self.assertIn("workflow_run:", self.text)
        self.assertIn("workflows: [ci]", self.text)
        self.assertIn("types: [completed]", self.text)
        self.assertNotIn("pull_request_target", self.text)

    def test_review_job_pins_workflow_and_policy_to_the_same_commit(self):
        review_job = self.workflow["jobs"]["review"]
        self.assertEqual(
            review_job["uses"],
            f"abuzucom/euler/.github/workflows/quality-review.yml@{PINNED_COMMIT}",
        )
        self.assertEqual(review_job["with"]["quality_ref"], PINNED_COMMIT)
        self.assertTrue(FULL_SHA.fullmatch(PINNED_COMMIT))

    def test_review_job_maps_only_the_provider_secret(self):
        review_job = self.workflow["jobs"]["review"]
        self.assertEqual(
            review_job["secrets"],
            {"MODEL_API_KEY": "${{ secrets.OLLAMA_API_KEY }}"},
        )
        self.assertNotIn("secrets: inherit", self.text)

    def test_review_job_fails_closed_on_block_or_needs_human(self):
        review_job = self.workflow["jobs"]["review"]
        self.assertTrue(review_job["with"]["fail_on_block"])

    def test_review_runs_only_for_same_repository_pull_requests(self):
        review_job = self.workflow["jobs"]["review"]
        self.assertIn("same_repo == 'true'", review_job["if"])

    def test_fork_pull_requests_receive_no_provider_secret(self):
        fork_job = self.workflow["jobs"]["fork-skip"]
        self.assertNotIn("secrets", fork_job)
        self.assertIn("same_repo != 'true'", fork_job["if"])

    def test_pull_request_text_reaches_scripts_through_environment_only(self):
        script = self.workflow["jobs"]["resolve"]["steps"][0]["with"]["script"]
        self.assertNotIn("${{", script)

    def test_github_script_actions_are_pinned_to_full_commit_shas(self):
        for job_name in ("resolve", "fork-skip"):
            step = self.workflow["jobs"][job_name]["steps"][0]
            action, _, revision = step["uses"].partition("@")
            self.assertEqual(action, "actions/github-script")
            self.assertTrue(FULL_SHA.fullmatch(revision.split()[0]))

    def test_workflow_default_permissions_are_read_only(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})


if __name__ == "__main__":
    unittest.main()
