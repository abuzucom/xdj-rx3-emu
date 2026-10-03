#!/usr/bin/env python3
"""Tests for bot branch exemptions that agents cannot impersonate."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
COVERAGE_LOGIN = "code-coverage-agent[bot]"
COVERAGE_ID = 295130552
COVERAGE_BRANCH = "code-coverage-agent/setup-code-coverage-reporting"
DEPENDABOT_EMAIL = "49699333+dependabot[bot]@users.noreply.github.com"
ACTIONS_LOGIN = "github-actions[bot]"
ACTIONS_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
HUMAN_EMAIL = "654939+itsjustatank@users.noreply.github.com"
INVALID_BRANCH = "invalid branch"


def load_script(name):
    """Import one script module by path."""
    spec = importlib.util.spec_from_file_location(f"_test_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event_payload(login, account_id, head_ref):
    """Return a minimal pull request event payload."""
    return {"pull_request": {"user": {"login": login, "id": account_id},
                             "head": {"ref": head_ref}}}


class RecordingBranchChecker:
    """Record branch checks and return one synthetic finding."""

    def __init__(self):
        self.branches = []

    def find_violations(self, branch):
        """Record the branch and return a finding."""
        self.branches.append(branch)
        return [INVALID_BRANCH]


class EmptyBannedChecker:
    """Return no banned-agent findings."""

    @staticmethod
    def find_violations(_commits, _author=""):
        """Return an empty finding list."""
        return []


class EventFileTestCase(unittest.TestCase):
    """Provide a temporary directory for event files."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def write_event(self, payload, name="event.json"):
        """Write a JSON event file and return its path."""
        path = Path(self.temp.name) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)


class EventLoaderTest(EventFileTestCase):
    """The event loader rejects every malformed payload with a reason."""

    def setUp(self):
        super().setUp()
        self.bots = load_script("trusted_bot_identities")

    def test_empty_path_means_no_event(self):
        self.assertIsNone(self.bots.load_event_author(""))

    def test_valid_event_returns_author(self):
        path = self.write_event(event_payload(COVERAGE_LOGIN, COVERAGE_ID, COVERAGE_BRANCH))
        author = self.bots.load_event_author(path)
        self.assertEqual(author, (COVERAGE_LOGIN, COVERAGE_ID, COVERAGE_BRANCH))

    def test_missing_file_raises(self):
        missing = str(Path(self.temp.name) / "absent.json")
        with self.assertRaisesRegex(self.bots.EventPayloadError, "unreadable"):
            self.bots.load_event_author(missing)

    def test_oversized_file_raises(self):
        path = Path(self.temp.name) / "big.json"
        path.write_bytes(b" " * (self.bots.MAX_EVENT_BYTES + 1))
        with self.assertRaisesRegex(self.bots.EventPayloadError, "exceeds"):
            self.bots.load_event_author(str(path))

    def test_bad_json_raises(self):
        path = Path(self.temp.name) / "bad.json"
        path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(self.bots.EventPayloadError, "unreadable"):
            self.bots.load_event_author(str(path))

    def test_push_event_raises(self):
        path = self.write_event({"ref": "refs/heads/main"})
        with self.assertRaisesRegex(self.bots.EventPayloadError, "pull_request"):
            self.bots.load_event_author(path)

    def test_missing_user_id_raises(self):
        payload = event_payload(COVERAGE_LOGIN, COVERAGE_ID, COVERAGE_BRANCH)
        del payload["pull_request"]["user"]["id"]
        with self.assertRaisesRegex(self.bots.EventPayloadError, "user.id"):
            self.bots.load_event_author(self.write_event(payload))

    def test_bool_user_id_raises(self):
        path = self.write_event(event_payload(COVERAGE_LOGIN, True, COVERAGE_BRANCH))
        with self.assertRaisesRegex(self.bots.EventPayloadError, "user.id"):
            self.bots.load_event_author(path)

    def test_error_message_omits_payload_values(self):
        payload = event_payload("secret-login", "secret-id", COVERAGE_BRANCH)
        with self.assertRaises(self.bots.EventPayloadError) as caught:
            self.bots.load_event_author(self.write_event(payload))
        self.assertNotIn("secret", str(caught.exception))

    def test_lazy_loader_reads_once_and_keeps_error(self):
        path = Path(self.temp.name) / "bad.json"
        path.write_text("{", encoding="utf-8")
        events = self.bots.LazyEventAuthor(str(path))
        self.assertIsNone(events.get())
        path.write_text(json.dumps(event_payload(COVERAGE_LOGIN, COVERAGE_ID, COVERAGE_BRANCH)),
                        encoding="utf-8")
        self.assertIsNone(events.get())
        self.assertIsInstance(events.error, self.bots.EventPayloadError)


class BranchExemptionTest(EventFileTestCase):
    """Only a verified trusted bot skips branch naming on its own prefix."""

    def setUp(self):
        super().setUp()
        self.module = load_script("check_compliance_tree")
        self.branch_checker = RecordingBranchChecker()
        self.checkers = {
            "check_banned_agents": EmptyBannedChecker(),
            "check_branch_name": self.branch_checker,
        }

    def scan(self, author, branch, event_path):
        """Run branch metadata checks with an explicit event path."""
        values = {"--branch": branch, "--pr-author": author}
        with patch.dict(os.environ, {"GITHUB_EVENT_PATH": event_path}):
            return self.module._scan_metadata(values, REPO_ROOT, self.checkers)

    def test_verified_coverage_bot_is_exempt(self):
        path = self.write_event(event_payload(COVERAGE_LOGIN, COVERAGE_ID, COVERAGE_BRANCH))
        self.assertEqual(self.scan(COVERAGE_LOGIN, COVERAGE_BRANCH, path), [])
        self.assertEqual(self.branch_checker.branches, [])

    def test_reregistered_slug_with_other_id_is_rejected(self):
        path = self.write_event(event_payload(COVERAGE_LOGIN, 1, COVERAGE_BRANCH))
        violations = self.scan(COVERAGE_LOGIN, COVERAGE_BRANCH, path)
        self.assertIn(INVALID_BRANCH, violations)
        self.assertTrue(any("does not match the pull request event" in v for v in violations))

    def test_head_ref_mismatch_is_rejected(self):
        path = self.write_event(event_payload(COVERAGE_LOGIN, COVERAGE_ID, "code-coverage-agent/other"))
        violations = self.scan(COVERAGE_LOGIN, COVERAGE_BRANCH, path)
        self.assertIn(INVALID_BRANCH, violations)

    def test_coverage_bot_without_event_is_rejected(self):
        violations = self.scan(COVERAGE_LOGIN, COVERAGE_BRANCH, "")
        self.assertIn(INVALID_BRANCH, violations)
        self.assertTrue(any("needs the GitHub pull request event" in v for v in violations))

    def test_unreadable_event_reports_cause(self):
        path = Path(self.temp.name) / "bad.json"
        path.write_text("{", encoding="utf-8")
        violations = self.scan(COVERAGE_LOGIN, COVERAGE_BRANCH, str(path))
        self.assertIn(INVALID_BRANCH, violations)
        self.assertTrue(any("pull request event unreadable" in v for v in violations))
        self.assertFalse(any("needs the GitHub pull request event" in v for v in violations))

    def test_wrong_prefix_skips_event_read(self):
        path = Path(self.temp.name) / "bad.json"
        path.write_text("{", encoding="utf-8")
        violations = self.scan(COVERAGE_LOGIN, "coverage/setup", str(path))
        self.assertEqual(violations, [INVALID_BRANCH])

    def test_human_author_on_bot_branch_is_rejected(self):
        path = self.write_event(event_payload("octocat", 583231, COVERAGE_BRANCH))
        self.assertEqual(self.scan("octocat", COVERAGE_BRANCH, path), [INVALID_BRANCH])

    def test_dependabot_ignores_push_event(self):
        path = self.write_event({"ref": "refs/heads/main"})
        self.assertEqual(self.scan("dependabot[bot]", "dependabot/pip/requests-3.0", path), [])

    def test_dependabot_needs_its_prefix(self):
        self.assertEqual(self.scan("dependabot[bot]", "update-requests", ""), [INVALID_BRANCH])


def git(repo, *arguments, env=None):
    """Run one git command in a fixture repository."""
    subprocess.run(["git", "-C", str(repo), *arguments], check=True,
                   capture_output=True, env=env)


def commit_as(repo, email, message):
    """Create one empty commit with the given author and committer email."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": email}
    git(repo, "commit", "--allow-empty", "-q", "-m", message, env=env)


def head_sha(repo):
    """Return the fixture repository HEAD commit."""
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                            check=True, capture_output=True, text=True)
    return result.stdout.strip()


class BotCommitIdentityTest(unittest.TestCase):
    """A bot noreply identity is valid only in that bot's own pull request."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls.temp.name)
        git(cls.repo, "init", "-q")
        commit_as(cls.repo, HUMAN_EMAIL, "chore: base")
        cls.base = head_sha(cls.repo)
        cls.ranges = {}
        for key, email in (
            ("dependabot", DEPENDABOT_EMAIL),
            ("unnumbered", "dependabot[bot]@users.noreply.github.com"),
            ("uppercase", "49699333+DEPENDABOT[BOT]@USERS.NOREPLY.GITHUB.COM"),
            ("actions", ACTIONS_EMAIL),
            ("coverage", f"{COVERAGE_ID}+{COVERAGE_LOGIN}@users.noreply.github.com"),
            ("coverage_wrong_id", f"1+{COVERAGE_LOGIN}@users.noreply.github.com"),
        ):
            git(cls.repo, "checkout", "-q", "-b", key, cls.base)
            commit_as(cls.repo, email, f"chore: {key}")
            cls.ranges[key] = head_sha(cls.repo)
        cls.bots = load_script("trusted_bot_identities")
        cls.identity = load_script("check_git_identity")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def violations(self, key, author, event_path=""):
        """Return bot commit violations for one fixture range."""
        identities = self.identity.log_identities(
            ["--end-of-options", f"{self.base}..{self.ranges[key]}"], self.repo)
        events = self.bots.LazyEventAuthor(event_path)
        return self.bots.bot_commit_violations(identities, author, events)

    def test_human_pull_request_with_dependabot_commit_fails(self):
        found = self.violations("dependabot", "itsjustatank")
        self.assertEqual(len(found), 2)
        self.assertIn("pull request author is itsjustatank", found[0])

    def test_uppercase_bot_email_cannot_bypass(self):
        self.assertEqual(len(self.violations("uppercase", "itsjustatank")), 2)

    def test_unnumbered_bot_email_fails(self):
        found = self.violations("unnumbered", "dependabot[bot]")
        self.assertTrue(found)
        self.assertIn("numbered", found[0])

    def test_dependabot_own_commits_pass(self):
        self.assertEqual(self.violations("dependabot", "dependabot[bot]"), [])

    def test_github_actions_own_commits_pass(self):
        self.assertEqual(self.violations("actions", ACTIONS_LOGIN), [])

    def test_registered_bot_id_mismatch_fails_offline(self):
        found = self.violations("coverage_wrong_id", COVERAGE_LOGIN)
        self.assertTrue(found)
        self.assertIn("account ID", found[0])

    def test_registered_bot_matching_id_passes(self):
        self.assertEqual(self.violations("coverage", COVERAGE_LOGIN), [])

    def test_event_id_overrides_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps(event_payload(ACTIONS_LOGIN, 7, "chore/x")),
                            encoding="utf-8")
            found = self.violations("actions", ACTIONS_LOGIN, str(path))
        self.assertTrue(found)
        self.assertIn("account ID", found[0])


class BranchNameMainTest(EventFileTestCase):
    """The portable branch checker applies the same verified exemption."""

    def setUp(self):
        super().setUp()
        self.module = load_script("check_branch_name")

    def run_main(self, event_path, branch=COVERAGE_BRANCH):
        """Run main() as a pull request CI step and return exit code and stderr."""
        environment = {"GITHUB_EVENT_NAME": "pull_request",
                       "GITHUB_EVENT_PATH": event_path, "GITHUB_HEAD_REF": branch}
        stderr = io.StringIO()
        with patch.dict(os.environ, environment), \
                patch.object(sys, "argv", ["check_branch_name.py"]), \
                redirect_stderr(stderr):
            code = self.module.main()
        return code, stderr.getvalue()

    def test_verified_coverage_bot_passes(self):
        path = self.write_event(event_payload(COVERAGE_LOGIN, COVERAGE_ID, COVERAGE_BRANCH))
        self.assertEqual(self.run_main(path)[0], 0)

    def test_human_on_bot_branch_fails(self):
        path = self.write_event(event_payload("octocat", 583231, COVERAGE_BRANCH))
        self.assertEqual(self.run_main(path)[0], 1)

    def test_wrong_id_fails(self):
        path = self.write_event(event_payload(COVERAGE_LOGIN, 1, COVERAGE_BRANCH))
        self.assertEqual(self.run_main(path)[0], 1)

    def test_bad_event_reports_and_fails(self):
        path = Path(self.temp.name) / "bad.json"
        path.write_text("{", encoding="utf-8")
        code, stderr = self.run_main(str(path))
        self.assertEqual(code, 1)
        self.assertIn("bot exemption check failed", stderr)

    def test_conforming_branch_skips_event(self):
        path = Path(self.temp.name) / "bad.json"
        path.write_text("{", encoding="utf-8")
        code, stderr = self.run_main(str(path), branch="fix/exempt-coverage-bot-branch-name")
        self.assertEqual(code, 0)
        self.assertNotIn("bot exemption", stderr)


if __name__ == "__main__":
    unittest.main()
