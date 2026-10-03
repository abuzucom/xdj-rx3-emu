#!/usr/bin/env python3
"""Tests for scripts/check_external_pr_refs.py and its wiring."""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

try:
    from tests.retrying_temp_directory import RetryingTemporaryDirectory
except ImportError:
    from retrying_temp_directory import RetryingTemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import check_external_pr_refs

WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "agents-compliance.yml"
SYNC_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "sync-check.yml"
PRE_COMMIT_PATH = REPO_ROOT / ".pre-commit-config.yaml"
OWNER = "abuzucom"
EXTERNAL_REFERENCE = "evanpurkhiser/prolink-go#16"
EXTERNAL_URL = "https://github.com/evanpurkhiser/prolink-go/pull/16"


def build_event(title: str, body: str, owner: str = OWNER,
                login: str = "itsjustatank") -> dict:
    """Return a minimal pull request event payload for one check."""
    return {
        "pull_request": {
            "title": title,
            "body": body,
            "user": {"login": login},
        },
        "repository": {"owner": {"login": owner}},
    }


class CheckerBehaviorTest(unittest.TestCase):
    """The checker blocks autolinked references to another owner."""

    def run_check(self, payload) -> tuple:
        """Return the exit status and captured output for one payload."""
        with RetryingTemporaryDirectory() as tmp_dir:
            event_path = Path(tmp_dir) / "event.json"
            if isinstance(payload, str):
                event_path.write_text(payload, encoding="utf-8")
            else:
                event_path.write_text(json.dumps(payload), encoding="utf-8")
            out = io.StringIO()
            err = io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                status = check_external_pr_refs.check_event(event_path)
        return status, out.getvalue() + err.getvalue()

    def assert_clean(self, payload) -> None:
        """Assert one payload produces no finding."""
        status, output = self.run_check(payload)
        self.assertEqual(status, 0, output)

    def assert_blocked(self, payload) -> str:
        """Return the output after asserting one payload is blocked."""
        status, output = self.run_check(payload)
        self.assertEqual(status, 1, output)
        return output

    def test_same_owner_reference_passes(self):
        self.assert_clean(
            build_event("feat: add checker", "See abuzucom/agents#6.")
        )

    def test_bare_number_reference_passes(self):
        self.assert_clean(build_event("feat: add checker", "Closes #6."))

    def test_external_short_reference_blocks(self):
        output = self.assert_blocked(
            build_event("feat: add checker", f"Ports {EXTERNAL_REFERENCE}.")
        )
        self.assertIn("pull_request.body", output)
        self.assertIn("evanpurkhiser/prolink-go", output)

    def test_external_url_blocks(self):
        self.assert_blocked(
            build_event("feat: add checker", f"Ports {EXTERNAL_URL}.")
        )

    def test_external_commit_reference_blocks(self):
        self.assert_blocked(
            build_event("feat: add checker",
                        "Ports evanpurkhiser/prolink-go@a1b2c3d.")
        )

    def test_external_issue_url_blocks(self):
        self.assert_blocked(
            build_event(
                "feat: add checker",
                "See https://github.com/evanpurkhiser/prolink-go/issues/3.",
            )
        )

    def test_title_violation_blocks(self):
        output = self.assert_blocked(
            build_event(f"feat: port {EXTERNAL_REFERENCE}", "Body text.")
        )
        self.assertIn("pull_request.title", output)

    def test_code_span_reference_passes(self):
        self.assert_clean(
            build_event(
                "feat: add checker",
                f"Ports `{EXTERNAL_REFERENCE}` at `{EXTERNAL_URL}`.",
            )
        )

    def test_fenced_block_reference_passes(self):
        body = f"Upstream:\n\n```\n{EXTERNAL_REFERENCE}\n{EXTERNAL_URL}\n```\n"
        self.assert_clean(build_event("feat: add checker", body))

    def test_owner_comparison_ignores_case(self):
        self.assert_clean(
            build_event("feat: add checker", "See AbuzuCom/agents#6.")
        )

    def test_dependabot_author_passes(self):
        self.assert_clean(
            build_event("chore: bump dep", f"Ports {EXTERNAL_REFERENCE}.",
                        login="dependabot[bot]")
        )

    def test_null_body_passes(self):
        payload = build_event("feat: add checker", "")
        payload["pull_request"]["body"] = None
        self.assert_clean(payload)

    def test_markdown_link_to_external_pull_request_blocks(self):
        self.assert_blocked(
            build_event("feat: add checker",
                        f"Ports [upstream 16]({EXTERNAL_URL}).")
        )

    def test_malformed_payload_reports_error(self):
        status, output = self.run_check("{not json")
        self.assertEqual(status, 1)
        self.assertIn("error:", output)
        self.assertNotIn("Traceback", output)

    def test_missing_repository_owner_reports_error(self):
        payload = build_event("feat: add checker", "Body text.")
        del payload["repository"]
        status, output = self.run_check(payload)
        self.assertEqual(status, 1)
        self.assertIn("error:", output)

    def test_finding_output_holds_one_line_per_finding(self):
        output = self.assert_blocked(
            build_event("feat: add checker",
                        f"Line one\n{EXTERNAL_REFERENCE}\nline two")
        )
        findings = [line for line in output.splitlines()
                    if "pull_request.body" in line]
        self.assertEqual(len(findings), 1)


class OwnerResolutionTest(unittest.TestCase):
    """The range mode resolves the current owner or fails closed."""

    def test_explicit_owner_wins(self):
        resolved = check_external_pr_refs.resolve_owner(
            "Explicit", {"GITHUB_REPOSITORY_OWNER": "fromenv"}, REPO_ROOT)
        self.assertEqual(resolved, "Explicit")

    def test_environment_owner_used_without_explicit(self):
        resolved = check_external_pr_refs.resolve_owner(
            "", {"GITHUB_REPOSITORY_OWNER": "fromenv"}, REPO_ROOT)
        self.assertEqual(resolved, "fromenv")

    def test_origin_remote_used_as_last_source(self):
        resolved = check_external_pr_refs.resolve_owner("", {}, REPO_ROOT)
        self.assertEqual(resolved, OWNER)

    def test_unresolvable_owner_raises(self):
        with RetryingTemporaryDirectory() as tmp_dir:
            with self.assertRaises(ValueError):
                check_external_pr_refs.resolve_owner("", {}, Path(tmp_dir))

    def test_owner_parses_from_ssh_remote(self):
        parsed = check_external_pr_refs.parse_remote_owner(
            "git@github.com:abuzucom/agents.git")
        self.assertEqual(parsed, OWNER)

    def test_owner_parses_from_https_remote(self):
        parsed = check_external_pr_refs.parse_remote_owner(
            "https://github.com/abuzucom/agents")
        self.assertEqual(parsed, OWNER)

    def test_non_github_remote_yields_no_owner(self):
        parsed = check_external_pr_refs.parse_remote_owner(
            "https://gitlab.com/abuzucom/agents")
        self.assertEqual(parsed, "")

    def test_lookalike_host_yields_no_owner(self):
        # A suffix test alone would accept this domain as GitHub.
        parsed = check_external_pr_refs.parse_remote_owner(
            "https://attacker-github.com/victim/repo")
        self.assertEqual(parsed, "")

    def test_lookalike_scp_host_yields_no_owner(self):
        parsed = check_external_pr_refs.parse_remote_owner(
            "git@attacker-github.com:victim/repo.git")
        self.assertEqual(parsed, "")

    def test_subdomain_remote_yields_owner(self):
        parsed = check_external_pr_refs.parse_remote_owner(
            "https://www.github.com/abuzucom/agents")
        self.assertEqual(parsed, OWNER)

    def test_remote_with_port_yields_owner(self):
        parsed = check_external_pr_refs.parse_remote_owner(
            "https://github.com:443/abuzucom/agents")
        self.assertEqual(parsed, OWNER)

    def test_remote_with_extra_path_yields_no_owner(self):
        parsed = check_external_pr_refs.parse_remote_owner(
            "https://github.com/abuzucom/agents/extra")
        self.assertEqual(parsed, "")

    def test_github_host_boundary(self):
        for host, expected in (
                ("github.com", True),
                ("www.github.com", True),
                ("GitHub.Com", True),
                ("github.com:443", True),
                ("attacker-github.com", False),
                ("gitlab.com", False),
        ):
            with self.subTest(host=host):
                self.assertEqual(
                    check_external_pr_refs.is_github_host(host), expected)


class CommitRangeTest(unittest.TestCase):
    """The range mode scans commit subjects and bodies."""

    def check(self, messages) -> tuple:
        """Return the status and output for one prepared message list."""
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = check_external_pr_refs.check_messages(messages, OWNER)
        return status, out.getvalue() + err.getvalue()

    def test_external_reference_in_subject_blocks(self):
        status, output = self.check(
            [("a" * 40, f"fix: port {EXTERNAL_REFERENCE}", "")])
        self.assertEqual(status, 1)
        self.assertIn("subject", output)
        self.assertIn("aaaaaaaaaaaa", output)

    def test_external_reference_in_body_blocks(self):
        status, output = self.check(
            [("b" * 40, "fix: port upstream patch", f"Ports {EXTERNAL_URL}.")])
        self.assertEqual(status, 1)
        self.assertIn("body", output)

    def test_same_owner_reference_passes(self):
        status, output = self.check(
            [("c" * 40, "fix: follow up", "Closes abuzucom/agents#6.")])
        self.assertEqual(status, 0, output)

    def test_backticked_reference_passes(self):
        status, output = self.check(
            [("d" * 40, "fix: port upstream patch",
              f"Ports `{EXTERNAL_REFERENCE}`.")])
        self.assertEqual(status, 0, output)

    def test_clean_range_passes(self):
        status, output = self.check([("e" * 40, "docs: tidy", "No refs here.")])
        self.assertEqual(status, 0, output)

    def test_empty_range_passes(self):
        status, output = self.check([])
        self.assertEqual(status, 0, output)


class WiringTest(unittest.TestCase):
    """CI and pre-commit configuration run the new checker."""

    def test_workflow_runs_checker_in_blocking_job(self):
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("python scripts/check_external_pr_refs.py", text)
        blocking = text.split("  pr-checks:")[1].split("\n  static-checks:")[0]
        self.assertIn("check_external_pr_refs.py", blocking)

    def test_pull_request_workflow_runs_the_checker(self):
        text = SYNC_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("pull_request:", text)
        self.assertIn("python scripts/check_external_pr_refs.py", text)

    def test_pre_commit_runs_tests_for_the_checker(self):
        text = PRE_COMMIT_PATH.read_text(encoding="utf-8")
        self.assertIn("external_pr_refs", text)


if __name__ == "__main__":
    unittest.main()
