#!/usr/bin/env python3
"""Label options, repeated API methods, and wrapper recognition in the gh gate."""
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))

try:
    from tests.retrying_temp_directory import RetryingTemporaryDirectory
except ImportError:
    from retrying_temp_directory import RetryingTemporaryDirectory

import _gate_core

OWNER = "abuzucom"


def verdict(args: list, owner: str = OWNER) -> str:
    """Return the decision for one GitHub CLI argument list."""
    return _gate_core.github_cli_verdict(args, repo_owner=owner)[0]


class LabelOptionDenialTest(unittest.TestCase):
    """Rule 23: agents never add, remove, or change labels through gh."""

    def test_label_options_on_create_and_edit_deny(self):
        for args in (
                ["pr", "create", "--label", "gate-change-approved"],
                ["pr", "create", "--label=gate-change-approved"],
                ["pr", "create", "-l", "bug"],
                ["pr", "create", "-lbug"],
                ["pr", "edit", "79", "--add-label", "gate-change-approved"],
                ["pr", "edit", "79", "--add-label=gate-change-approved"],
                ["pr", "edit", "79", "--remove-label", "bug"],
                ["pr", "edit", "79", "--remove-label=bug"],
                ["issue", "create", "--label", "bug"],
                ["issue", "create", "-l", "bug"],
                ["issue", "edit", "3", "--add-label", "bug"],
                ["issue", "edit", "3", "--remove-label", "bug"],
                ["pr", "--repo", "abuzucom/agents", "edit", "79", "--add-label", "x"]):
            with self.subTest(args=args):
                decision, reason = _gate_core.github_cli_verdict(args, repo_owner=OWNER)
                self.assertEqual(decision, "deny")
                self.assertIn("Rule 23", reason)

    def test_label_filters_on_read_commands_stay_available(self):
        for args in (
                ["pr", "list", "-l", "bug"],
                ["pr", "list", "--label", "bug"],
                ["issue", "list", "--label", "bug"],
                ["pr", "view", "79"]):
            with self.subTest(args=args):
                self.assertEqual(verdict(args), "")

    def test_edits_without_label_options_keep_prior_verdict(self):
        self.assertEqual(verdict(["pr", "edit", "79", "--title", "x"]), "")
        self.assertEqual(verdict(["pr", "create", "--draft", "--title", "x"]), "")


class ApiMethodDenialTest(unittest.TestCase):
    """gh keeps the last method option, so every method value counts."""

    def test_any_non_get_method_denies(self):
        for args in (
                ["api", "-X", "GET", "-X", "POST", "repos/o/r/issues/1/labels"],
                ["api", "--method", "GET", "--method", "DELETE", "repos/o/r"],
                ["api", "-XGET", "-XPATCH", "repos/o/r"],
                ["api", "--method=GET", "-X", "PUT", "repos/o/r"],
                ["api", "-X", "POST", "repos/o/r"]):
            with self.subTest(args=args):
                self.assertEqual(verdict(args), "deny")

    def test_repeated_get_passes(self):
        self.assertEqual(verdict(["api", "-X", "GET", "-X", "GET", "repos/o/r"]), "")

    def test_fields_without_method_deny_and_fields_with_get_pass(self):
        self.assertEqual(verdict(["api", "repos/o/r/issues", "-f", "title=x"]), "deny")
        self.assertEqual(verdict(["api", "-X", "GET", "search/issues", "-f", "q=x"]), "")


class UnreadableOriginTest(unittest.TestCase):
    """An outward-facing command asks when the origin owner cannot be read."""

    def test_implicit_target_asks_without_origin_owner(self):
        for args in (["pr", "create", "--draft"], ["pr", "comment", "5", "--body", "x"],
                     ["issue", "create", "--title", "x"]):
            with self.subTest(args=args):
                self.assertEqual(verdict(args, ""), "ask")

    def test_implicit_target_passes_with_origin_owner(self):
        self.assertEqual(verdict(["pr", "create", "--draft"]), "")

    def test_read_only_command_passes_without_origin_owner(self):
        self.assertEqual(verdict(["pr", "view", "5"], ""), "")


class WrapperRecognitionTest(unittest.TestCase):
    """Interpreter flags and script location do not hide the wrapper."""

    def test_wrapper_forms_are_recognized(self):
        with RetryingTemporaryDirectory() as tmp_dir:
            root = os.path.realpath(tmp_dir)
            subdirectory = os.path.join(root, "docs")
            script = os.path.join(root, "scripts", "trusted_gh.py")
            for program, args, cwd in (
                    ("python", ["-E", "-s", "scripts/trusted_gh.py", "run", "pr", "view"], root),
                    ("python3", ["-I", "-B", "-u", "scripts/trusted_gh.py", "run", "pr", "view"], root),
                    ("python", ["../scripts/trusted_gh.py", "run", "pr", "view"], subdirectory),
                    ("python", [script, "run", "pr", "view"], subdirectory),
                    ("py", ["-E", script, "run", "pr", "view"], root)):
                with self.subTest(program=program, args=args):
                    self.assertEqual(
                        _gate_core.trusted_gh_arguments(program, args, cwd), ["pr", "view"])

    def test_non_wrapper_forms_are_not_recognized(self):
        with RetryingTemporaryDirectory() as tmp_dir:
            root = os.path.realpath(tmp_dir)
            for program, args in (
                    ("python", ["-c", "print(1)", "run", "pr"]),
                    ("python", ["scripts/other.py", "run", "pr", "view"]),
                    ("python", ["scripts/trusted_gh.py", "view"]),
                    ("node", ["scripts/trusted_gh.py", "run", "pr", "view"])):
                with self.subTest(program=program, args=args):
                    self.assertEqual(_gate_core.trusted_gh_arguments(program, args, root), [])

    def test_recognized_wrapper_label_edit_denies(self):
        with RetryingTemporaryDirectory() as tmp_dir:
            args = ["-E", "-s", "scripts/trusted_gh.py", "run", "pr", "edit", "7",
                    "--add-label", "gate-change-approved"]
            self.assertEqual(_gate_core.forge_verdict("python", args, tmp_dir)[0], "deny")


class PullRefDetectionTest(unittest.TestCase):
    """Pull request refs still count as GitHub targets."""

    def test_pull_request_refs_are_github_targets(self):
        for tokens in (["refs/pull/12/head"], ["origin", "pull/12/head"]):
            with self.subTest(tokens=tokens):
                self.assertTrue(_gate_core._is_github_target(tokens))


if __name__ == "__main__":
    unittest.main()
