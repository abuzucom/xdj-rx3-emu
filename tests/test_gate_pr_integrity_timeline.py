#!/usr/bin/env python3
"""Bind gate-change approval to the current pull-request head."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import check_gate_pr_integrity as integrity

WORKFLOW = ROOT / ".github" / "workflows" / "gate-integrity.yml"
LABEL = integrity.APPROVAL_LABEL
PUSHED = "2026-09-30T10:00:00Z"
BEFORE_PUSH = "2026-09-30T09:00:00Z"
AFTER_PUSH = "2026-09-30T11:00:00Z"
LATER = "2026-09-30T12:00:00Z"


def labeled(created_at: str, actor_type: str = "User", label: str = LABEL) -> dict:
    """Return one reduced labeled timeline event."""
    return {"event": "labeled", "created_at": created_at, "label": label, "actor_type": actor_type}


def timeline(events: list, suite_times: tuple = (PUSHED,)) -> dict:
    """Return one reduced approval timeline."""
    return {"events": events, "head_suite_times": list(suite_times)}


class ApprovalTimelineTest(unittest.TestCase):
    """A present label counts only when a person applied it after the last push."""

    def test_label_after_push_is_current(self) -> None:
        self.assertTrue(integrity.approval_is_current(timeline([labeled(AFTER_PUSH)])))

    def test_label_before_push_is_stale(self) -> None:
        self.assertFalse(integrity.approval_is_current(timeline([labeled(BEFORE_PUSH)])))

    def test_force_push_after_label_is_stale(self) -> None:
        force_push = {"event": "head_ref_force_pushed", "created_at": LATER,
                      "label": None, "actor_type": "User"}
        self.assertFalse(integrity.approval_is_current(
            timeline([labeled(AFTER_PUSH), force_push])))

    def test_unlabeled_after_label_is_stale(self) -> None:
        removal = {"event": "unlabeled", "created_at": LATER, "label": LABEL, "actor_type": "User"}
        self.assertFalse(integrity.approval_is_current(timeline([labeled(AFTER_PUSH), removal])))

    def test_relabel_after_removal_is_current(self) -> None:
        removal = {"event": "unlabeled", "created_at": LATER, "label": LABEL, "actor_type": "User"}
        relabel = labeled("2026-09-30T13:00:00Z")
        self.assertTrue(integrity.approval_is_current(
            timeline([labeled(AFTER_PUSH), removal, relabel])))

    def test_bot_label_is_ignored(self) -> None:
        self.assertFalse(integrity.approval_is_current(timeline([labeled(AFTER_PUSH, "Bot")])))

    def test_other_label_is_ignored(self) -> None:
        self.assertFalse(integrity.approval_is_current(
            timeline([labeled(AFTER_PUSH, label="gate-change-approved-now")])))

    def test_missing_suite_times_fail_closed(self) -> None:
        self.assertFalse(integrity.approval_is_current(timeline([labeled(AFTER_PUSH)], ())))


class TimelineParsingTest(unittest.TestCase):
    """Malformed, oversized, or incomplete timeline data fails closed."""

    def write(self, content: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "timeline.json"
        path.write_text(content, encoding="utf-8")
        return path

    def test_valid_file_loads(self) -> None:
        path = self.write(json.dumps(timeline([labeled(AFTER_PUSH)])))
        self.assertTrue(integrity.approval_is_current(integrity.load_timeline(path)))

    def test_invalid_documents_are_rejected(self) -> None:
        for content in (
                "not json",
                "[]",
                json.dumps({"events": []}),
                json.dumps({"events": {}, "head_suite_times": []}),
                json.dumps(timeline([{"event": "labeled"}])),
                json.dumps(timeline([labeled("yesterday")])),
                json.dumps(timeline([labeled(AFTER_PUSH)], ("soon",))),
                json.dumps(timeline([labeled(AFTER_PUSH, actor_type=7)]))):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    integrity.load_timeline(self.write(content))

    def test_oversized_file_is_rejected(self) -> None:
        path = self.write(" " * (integrity.MAX_TIMELINE_BYTES + 1))
        with self.assertRaises(ValueError):
            integrity.load_timeline(path)


class TimelineCliTest(unittest.TestCase):
    """The optional flag binds approval without changing existing flags."""

    def run_main(self, paths: list, labels: list, extra: list) -> int:
        with patch.object(integrity, "_has_revision", return_value=True):
            with patch.object(integrity, "_changed_paths", return_value=paths):
                return integrity.main([
                    "--base", "a" * 40, "--head", "b" * 40, "--pr-number", "1",
                    "--labels-json", json.dumps(labels), *extra,
                ])

    def timeline_file(self, document: dict) -> str:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "timeline.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def test_without_flag_label_presence_still_approves(self) -> None:
        self.assertEqual(self.run_main(["hooks/x.py"], [LABEL], []), 0)

    def test_stale_label_fails_with_flag(self) -> None:
        path = self.timeline_file(timeline([labeled(BEFORE_PUSH)]))
        self.assertEqual(self.run_main(["hooks/x.py"], [LABEL], ["--timeline-json", path]), 1)

    def test_current_label_passes_with_flag(self) -> None:
        path = self.timeline_file(timeline([labeled(AFTER_PUSH)]))
        self.assertEqual(self.run_main(["hooks/x.py"], [LABEL], ["--timeline-json", path]), 0)

    def test_unprotected_change_passes_with_flag(self) -> None:
        path = self.timeline_file(timeline([]))
        self.assertEqual(self.run_main(["README.md"], [], ["--timeline-json", path]), 0)

    def test_missing_timeline_file_fails_closed(self) -> None:
        self.assertEqual(self.run_main(["README.md"], [], ["--timeline-json", "/nonexistent/t.json"]), 1)


class ProtectedPathCoverageTest(unittest.TestCase):
    """Case variants and enforcement-bearing build files require approval."""

    def test_case_variants_require_approval(self) -> None:
        for path in ("Hooks/x.py", ".Claude/settings.json", "agents.md", "SCRIPTS/TRUSTED_GH.PY",
                     ".GitHub/Workflows/ci.yml"):
            with self.subTest(path=path):
                self.assertTrue(integrity.requires_approval([path]))

    def test_build_and_ownership_files_require_approval(self) -> None:
        for path in ("Makefile", ".pre-commit-config.yaml", "scripts/run_tests.py", "CODEOWNERS",
                     ".github/CODEOWNERS", "docs/CODEOWNERS"):
            with self.subTest(path=path):
                self.assertTrue(integrity.requires_approval([path]))

    def test_unrelated_paths_stay_unprotected(self) -> None:
        for path in ("README.md", "docs/gate-threat-model.md", "tests/test_x.py"):
            with self.subTest(path=path):
                self.assertFalse(integrity.requires_approval([path]))


class WorkflowWiringTest(unittest.TestCase):
    """The workflow reads the timeline and never runs pull-request checker code."""

    def setUp(self) -> None:
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_bootstrap_never_runs_head_checker(self) -> None:
        self.assertNotIn("pr-head/scripts", self.text)

    def test_timeline_is_read_with_pinned_script_action(self) -> None:
        self.assertIn("actions/github-script@f28e40c7f34bde8b3046d885e986cb6290c5673b", self.text)
        self.assertIn("listEventsForTimeline", self.text)
        self.assertIn("listSuitesForRef", self.text)
        self.assertIn("--timeline-json", self.text)

    def test_permissions_stay_read_only(self) -> None:
        permissions = self.text.split("permissions:", 1)[1].split("jobs:", 1)[0]
        for line in permissions.strip().splitlines():
            with self.subTest(line=line):
                self.assertTrue(line.strip().endswith(": read"), line)
        self.assertIn("checks: read", permissions)
        self.assertIn("issues: read", permissions)


if __name__ == "__main__":
    unittest.main()
