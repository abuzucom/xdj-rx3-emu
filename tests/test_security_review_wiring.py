#!/usr/bin/env python3
"""Cover the foucault pull request security review wiring."""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "security-review-pr.yml"
PINNED_COMMIT = "62851df1ef177593adbb9e06b223f5a6dce66fc0"
FULL_SHA = re.compile(r"[0-9a-f]{40}")
CI_ADAPTER_FILES = (
    "ci/build_pr_case.py",
    "ci/run_model_command.py",
    "ci/call_model.py",
    "ci/model_providers.json",
    # Run by the reusable workflow from the caller's own checkout (not the
    # separate .foucault checkout that holds AUDIT.md), so it must live in
    # this repository's scripts/ directory, not ci/.
    "scripts/check_pr_review_response.py",
)


class SecurityReviewWiringTest(unittest.TestCase):
    """Keep the foucault caller workflow pinned, scoped, and complete."""

    def setUp(self):
        self.text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.text)

    def test_workflow_parses_and_exists(self):
        self.assertTrue(WORKFLOW_PATH.is_file())
        self.assertIsInstance(self.workflow, dict)

    def test_triggers_after_immutable_compliance(self):
        # PyYAML 1.1 parses a bare `on:` key as the boolean True, so match the
        # trigger block as raw text instead of indexing the parsed mapping.
        self.assertIn("workflow_run:", self.text)
        self.assertIn("workflows: [Immutable Compliance]", self.text)
        self.assertIn("types: [completed]", self.text)

    def test_review_job_pins_workflow_and_audit_to_the_same_commit(self):
        review_job = self.workflow["jobs"]["review"]
        uses = review_job["uses"]
        audit_ref = review_job["with"]["audit_ref"]
        self.assertEqual(
            uses,
            f"abuzucom/foucault/.github/workflows/security-review.yml@{PINNED_COMMIT}",
        )
        self.assertEqual(audit_ref, PINNED_COMMIT)
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

    def test_fork_pull_requests_receive_no_provider_secret(self):
        fork_job = self.workflow["jobs"]["fork-review-skipped"]
        self.assertNotIn("secrets", fork_job)
        self.assertIn("same_repository == 'false'", fork_job["if"])

    def test_github_script_action_is_pinned_to_a_full_commit_sha(self):
        resolve_job = self.workflow["jobs"]["resolve-pr"]
        step = resolve_job["steps"][0]
        action, _, revision = step["uses"].partition("@")
        self.assertEqual(action, "actions/github-script")
        self.assertTrue(FULL_SHA.fullmatch(revision.split()[0]))

    def test_ci_adapter_files_referenced_by_the_workflow_exist(self):
        self.assertIn("python3 ci/call_model.py", self.text)
        for relative_path in CI_ADAPTER_FILES:
            self.assertTrue(
                (ROOT / relative_path).is_file(),
                f"missing vendored adapter file: {relative_path}",
            )


if __name__ == "__main__":
    unittest.main()
