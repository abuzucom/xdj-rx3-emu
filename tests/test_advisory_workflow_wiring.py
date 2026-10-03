#!/usr/bin/env python3
"""Cover safe advisory prose-check wiring in GitHub Actions."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNC_WORKFLOW = ROOT / ".github" / "workflows" / "sync-check.yml"
REUSABLE_WORKFLOW = ROOT / ".github" / "workflows" / "agents-compliance.yml"
INTEGRITY_WORKFLOW = ROOT / ".github" / "workflows" / "gate-integrity.yml"


class AdvisoryWorkflowTest(unittest.TestCase):
    """Workflow source keeps untrusted metadata outside shell text."""

    def test_sync_workflow_runs_pull_request_checker(self):
        text = SYNC_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("python scripts/check_pull_request_message.py", text)
        self.assertIn("if: github.event_name == 'pull_request'", text)
        self.assertNotIn("github.event.pull_request.title", text)
        self.assertNotIn("github.event.pull_request.body", text)

    def test_sync_workflow_runs_authorship_checkers(self):
        # The reusable workflow's pr-checks job is skipped on every event
        # this repository fires, so these checkers need a live home here.
        text = SYNC_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("python scripts/check_banned_agents.py", text)
        self.assertIn("python scripts/check_git_identity.py", text)
        self.assertIn("python scripts/check_commit_attribution.py", text)
        self.assertIn("GH_TOKEN", text)
        self.assertIn("fetch-depth: 0", text)
        self.assertIn("PR_BASE_SHA", text)
        self.assertIn("PR_HEAD_SHA", text)

    def test_reusable_workflow_avoids_ref_interpolation(self):
        text = REUSABLE_WORKFLOW.read_text(encoding="utf-8")
        run_blocks = re.findall(r"run:\s*[|>-]?\s*\n((?:\s{10,}.*\n)+)", text)
        combined = "\n".join(run_blocks)
        self.assertNotIn("github.event.pull_request.base.ref", combined)
        self.assertNotIn("github.event.pull_request.head.sha", combined)
        self.assertIn("PR_BASE_SHA", text)
        self.assertIn("PR_HEAD_SHA", text)

    def test_every_checkout_disables_persisted_credentials(self):
        for workflow in (SYNC_WORKFLOW, REUSABLE_WORKFLOW):
            text = workflow.read_text(encoding="utf-8")
            checkout_count = text.count("actions/checkout@")
            self.assertGreater(checkout_count, 0)
            self.assertEqual(
                checkout_count,
                text.count("persist-credentials: false"),
            )

    def test_changelog_range_jobs_fetch_full_history(self):
        workflow_jobs = (
            (SYNC_WORKFLOW, "check-sync"),
            (REUSABLE_WORKFLOW, "static-checks"),
        )
        for workflow, job in workflow_jobs:
            text = workflow.read_text(encoding="utf-8")
            section = text.split(f"  {job}:", 1)[1]
            self.assertIn("fetch-depth: 0", section)
            self.assertIn("python scripts/check_changelog.py --base", section)

    def test_workflows_enforce_complete_gate_adoption(self):
        for workflow in (SYNC_WORKFLOW, REUSABLE_WORKFLOW):
            text = workflow.read_text(encoding="utf-8")
            self.assertIn("python scripts/check_gate_adoption.py", text)
            self.assertIn("python scripts/check_hook_launchers.py", text)

    def test_integrity_workflow_uses_trusted_base_code(self):
        text = INTEGRITY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("pull_request", text)
        self.assertNotIn("pull_request_target", text)
        self.assertIn("github.event.pull_request.base.sha", text)
        self.assertIn("python scripts/check_gate_pr_integrity.py", text)
        self.assertIn("persist-credentials: false", text)


if __name__ == "__main__":
    unittest.main()
