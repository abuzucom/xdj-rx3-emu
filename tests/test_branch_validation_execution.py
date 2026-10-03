"""Exercise execution boundaries and bounded native configuration reads."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_enforce_branch_name import REPO_ROOT, hook

core = hook.core


class BranchExecutionTest(unittest.TestCase):
    """Reject execution paths that hide branch writes behind a Git read."""

    def test_git_read_redirection_cannot_write_a_prohibited_ref(self):
        reason = hook.command_execution_reason(
            "git status > .git/refs/heads/claude/x", str(REPO_ROOT), "Bash")
        self.assertIn("prohibited", reason)

    def test_repository_program_names_do_not_grant_execution(self):
        for command in ("./git status", "./echo hello", "echo.ps1 hello", "git.cmd status"):
            with self.subTest(command=command):
                self.assertTrue(hook.command_execution_reason(command, str(REPO_ROOT), "Bash"))

    def test_input_and_privilege_wrappers_remain_opaque(self):
        for command in ("xargs git branch", "sudo git status", "nohup git push origin HEAD"):
            with self.subTest(command=command):
                self.assertTrue(hook.command_execution_reason(command, str(REPO_ROOT), "Bash"))

    def test_implicit_push_targets_remain_unresolved(self):
        for arguments in ([], ["origin"], ["--repo=origin"], ["--repo", "origin"]):
            with self.subTest(arguments=arguments):
                context = {"subcommand": "push", "arguments": arguments}
                self.assertTrue(hook._git_context_reason(context, str(REPO_ROOT)))

    def test_explicit_push_targets_remain_available(self):
        for arguments in (["origin", "HEAD"], ["-u", "origin", "HEAD"], ["--repo=origin", "HEAD"]):
            with self.subTest(arguments=arguments):
                context = {"subcommand": "push", "arguments": arguments}
                self.assertEqual(hook._git_context_reason(context, str(REPO_ROOT)), "")

    def test_alias_failures_are_explicit(self):
        cases = (
            ("", {}, "unresolved"),
            ("$command", {}, "unresolved"),
            ("x", {}, "unresolved"),
            ("x", {"alias.x": "x"}, "cycle"),
            ("x", {"alias.x": "!echo text"}, "opaque"),
            ("x", {"alias.x": "'unterminated"}, "incomplete"),
            ("x", {"alias.x": "-c alias.y=status y"}, "settings"),
            ("x", {"alias.x": " "}, "settings"),
            ("x", {"alias.x": "x" * (core.MAX_CONFIG_BYTES + 1)}, "limit"),
        )
        for subcommand, entries, expected in cases:
            with self.subTest(subcommand=subcommand, expected=expected):
                _subcommand, _arguments, reason = core.resolve_branch_alias(subcommand, [], entries)
                self.assertIn(expected, reason)

    def test_alias_expansion_depth_is_bounded(self):
        entries = {f"alias.step{index}": f"step{index + 1}"
                   for index in range(core.MAX_GIT_ALIAS_DEPTH)}
        _subcommand, _arguments, reason = core.resolve_branch_alias("step0", [], entries)
        self.assertIn("limit", reason)

    def test_global_option_preconditions(self):
        for arguments in (
            ["--exec-path=/untrusted", "status"], ["--namespace=x", "status"],
            ["-C"], ["-c"], ["--config-env"], ["--config-env=alias.x=MISSING", "x"],
        ):
            with self.subTest(arguments=arguments):
                normalized, reason = core._branch_global_arguments(arguments, {})
                self.assertIsNone(normalized)
                self.assertTrue(reason)

    def test_global_option_normalization(self):
        normalized, reason = core._branch_global_arguments(
            ["-Cchild", "--config-env", "alias.x=ALIAS", "x"], {"ALIAS": "status"})
        self.assertEqual(normalized, ["-C", "child", "-c", "alias.x=status", "x"])
        self.assertEqual(reason, "")

    def test_git_context_rejects_uninspectable_settings(self):
        cases = (
            (["status"], [["PATH", "untrusted"]]),
            (["--namespace=x", "status"], []),
            (["-C", "$unresolved", "status"], []),
            (["status"], [["GIT_CONFIG_PARAMETERS", "unresolved"]]),
            (["status"], [["GIT_CONFIG_COUNT", "-1"]]),
        )
        for arguments, assignments in cases:
            with self.subTest(arguments=arguments, assignments=assignments):
                context = core.git_branch_context(arguments, str(REPO_ROOT), assignments)
                self.assertTrue(context["error"])

    def test_missing_config_resolver_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(core, "policy_root", return_value=directory):
                with self.assertRaises(OSError):
                    core._trusted_config_executable(directory)

    def test_real_config_reader_returns_bounded_bytes(self):
        command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'config')"]
        self.assertEqual(core._run_config_reader(command, dict(os.environ)), b"config")

    def test_real_config_reader_rejects_nonzero_exit(self):
        command = [sys.executable, "-c", "raise SystemExit(1)"]
        with self.assertRaises(OSError):
            core._run_config_reader(command, dict(os.environ))

    def test_real_config_reader_rejects_excess_output(self):
        command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 262145)"]
        with self.assertRaises(OSError):
            core._run_config_reader(command, dict(os.environ))

    def test_real_config_reader_reaps_timed_out_process(self):
        command = [sys.executable, "-c", "import time; time.sleep(10)"]
        with patch.object(core, "CONFIG_READ_TIMEOUT_SECONDS", 0.05):
            with self.assertRaises(subprocess.TimeoutExpired):
                core._run_config_reader(command, dict(os.environ))

    def test_metadata_common_directory_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            administration = root / ".git"
            administration.mkdir()
            common = root / "common"
            common.mkdir()
            pointer = administration / "commondir"
            pointer.write_text("../common\n", encoding="utf-8")
            self.assertEqual(hook._metadata_roots(directory), (str(administration), str(common)))
            pointer.write_text("\n", encoding="utf-8")
            self.assertEqual(hook._metadata_roots(directory), (str(administration),))

    def test_ci_reader_accepts_matching_branch_and_detached_head(self):
        with tempfile.TemporaryDirectory() as directory:
            administration = Path(directory) / ".git"
            administration.mkdir()
            head = administration / "HEAD"
            with patch.dict(os.environ, {"GITHUB_HEAD_REF": "fix/example"}):
                head.write_text("ref: refs/heads/fix/example\n", encoding="utf-8")
                self.assertEqual(hook.current_branch(directory), "fix/example")
                head.write_text("0" * 40 + "\n", encoding="utf-8")
                self.assertEqual(hook.current_branch(directory), "fix/example")


if __name__ == "__main__":
    unittest.main()
