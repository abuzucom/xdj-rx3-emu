"""Exercise effective configuration and shell enforcement without executing payloads."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tests.test_enforce_branch_name import HOOK_PATH, REPO_ROOT, hook

TIMEOUT_SECONDS = 10


class BranchContextTest(unittest.TestCase):
    """Use isolated metadata and real hook subprocesses for branch decisions."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git_directory = self.root / ".git"
        self.git_directory.mkdir()
        (self.git_directory / "objects").mkdir()
        (self.git_directory / "refs").mkdir()
        (self.git_directory / "HEAD").write_text(
            "ref: refs/heads/fix/example\n", encoding="utf-8")
        (self.git_directory / "config").write_text(
            "[core]\nrepositoryformatversion = 0\nbare = false\n", encoding="utf-8")
        self.empty_config = self.root / "empty.config"
        self.empty_config.write_text("", encoding="utf-8")
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        self.environment.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.empty_config),
            "CLAUDE_PROJECT_DIR": str(self.root),
        })

    def run_command(self, command, tool="Bash"):
        """Send command text as JSON data to the actual hook."""
        payload = {
            "hook_event_name": "PreToolUse",
            "permission_mode": "default",
            "tool_name": tool,
            "tool_input": {"command": command},
        }
        return subprocess.run(
            [sys.executable, str(HOOK_PATH)], input=json.dumps(payload),
            env=self.environment, capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, check=False,
        )

    def test_direct_and_dynamic_targets(self):
        for command in (
            "git push origin HEAD:claude/x", "git switch -cclaude/x",
            "git checkout -bclaude/x", "git branch $target",
            "git push origin HEAD:$target", "git unknown-operation",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.run_command(command).returncode, 2)

    def test_opaque_commands_do_not_execute(self):
        for command in (
            'eval "git push origin claude/x"', 'bash -c "git branch claude/x"',
            'python -c "open(\'unexpected\', \'w\').write(\'x\')"',
            "node script.js", "./script.sh", "sh script.sh", "$program args",
        ):
            with self.subTest(command=command):
                result = self.run_command(command)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse((self.root / "unexpected").exists())

    def test_effective_aliases_and_precedence(self):
        self.environment.update({
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "alias.x",
            "GIT_CONFIG_VALUE_0": "push origin claude/x",
        })
        self.assertEqual(self.run_command("git x").returncode, 2)
        result = self.run_command('git -c alias.x="status --short" x')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_env_alias(self):
        self.environment["BRANCH_TEST_ALIAS"] = "status --short"
        result = self.run_command("git --config-env=alias.x=BRANCH_TEST_ALIAS x")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_global_alias_and_include(self):
        included = self.root / "included.config"
        included.write_text('[alias]\nx = status --short\n', encoding="utf-8")
        self.empty_config.write_text(
            '[include]\npath = included.config\n', encoding="utf-8")
        result = self.run_command("git x")
        self.assertEqual(result.returncode, 0, result.stderr)
        included.write_text('[alias]\nx = push origin claude/x\n', encoding="utf-8")
        self.assertEqual(self.run_command("git x").returncode, 2)

    def test_alias_cycles_shell_aliases_and_missing_aliases(self):
        for command in (
            "git -c alias.x=y -c alias.y=x x",
            "git -c alias.x='!echo hidden' x", "git absent-alias",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.run_command(command).returncode, 2)

    def test_local_head_overrides_forged_environment(self):
        (self.git_directory / "HEAD").write_text(
            "ref: refs/heads/claude/x\n", encoding="utf-8")
        self.environment["GITHUB_HEAD_REF"] = "fix/example"
        self.assertEqual(self.run_command("git status").returncode, 2)
        result = self.run_command("git branch -m fix/recovered")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_recovery_has_no_execution_suffix(self):
        (self.git_directory / "HEAD").write_text("ref: refs/heads/claude/x\n", encoding="utf-8")
        for command in (
            "git branch -m fix/recovered;", "(git branch -m fix/recovered)",
            "git branch -M fix/recovered", "git -C . branch -m fix/recovered",
            "git branch -m fix/recovered > output",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.run_command(command).returncode, 2)

    def test_metadata_read_and_document_write(self):
        result = self.run_command('printf "%s" ".git/HEAD claude/x"')
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in (
            "printf 'refs/heads/claude/x' > .git/HEAD",
            "touch .git/refs/heads/claude/x", "cp input .git/HEAD",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.run_command(command).returncode, 2)

    def test_parse_bounds_and_incomplete_reason(self):
        result = self.run_command('printf "claude/x')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Git", result.stderr)
        result = self.run_command("echo " + "x" * hook.bash_parser.MAX_COMMAND_CHARACTERS)
        self.assertEqual(result.returncode, 2)

    def test_environment_conflict_is_explicit_for_ci_reader(self):
        with patch.dict(os.environ, {"GITHUB_HEAD_REF": "feat/different"}):
            with self.assertRaises(ValueError):
                hook.current_branch(str(self.root), allow_environment=True)


def evaluate_condition(condition, event):
    """Evaluate the workflow's bounded boolean comparisons without eval."""
    if condition is None:
        return True
    if isinstance(condition, bool):
        return condition
    values = {**event, "true": True, "false": False}
    results = []
    for alternative in condition.split("||"):
        terms = []
        for term in alternative.split("&&"):
            operator = "!=" if "!=" in term else "=="
            left, separator, right = term.strip().partition(operator)
            if not separator:
                raise AssertionError("Unsupported workflow condition")
            actual = values[left.strip()]
            expected = values.get(right.strip(), right.strip().strip("'"))
            terms.append(actual != expected if operator == "!=" else actual == expected)
        results.append(all(terms))
    return any(results)


class DraftBranchValidationTest(unittest.TestCase):
    """Evaluate real workflow gates for draft and ready pull requests."""

    def test_branch_check_runs_for_draft_and_ready_prs(self):
        path = REPO_ROOT / ".github" / "workflows" / "agents-compliance.yml"
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for draft in (True, False):
            event = {
                "github.event_name": "pull_request",
                "github.event.pull_request.draft": draft,
                "github.event.pull_request.user.login": "contributor",
            }
            active = []
            for job in workflow["jobs"].values():
                for step in job.get("steps", []):
                    if "scripts/check_branch_name.py" in step.get("run", ""):
                        active.append(evaluate_condition(job.get("if"), event)
                                      and evaluate_condition(step.get("if"), event)
                                      and not job.get("needs"))
            with self.subTest(draft=draft):
                self.assertTrue(any(active))


if __name__ == "__main__":
    unittest.main()
