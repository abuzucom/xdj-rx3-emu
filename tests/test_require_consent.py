#!/usr/bin/env python3
"""Tests for hooks/require_consent.py and its wiring.

Runs the hook as a subprocess against synthetic Claude Code payloads, the
same path the harness uses, rather than asserting on mocks. Test files are
created on disk, because the hook distinguishes a new test file from an edit
to an existing one by looking at the filesystem.

The additive cases matter as much as the gated ones. AGENTS.md mandates a
test-first workflow, so a gate that prompted on every append to a test file
would be turned off within a day and enforce nothing.

The settings tests guard the wiring: a hook nobody registered enforces
nothing, and an edit to `.claude/settings.json` that drops an event would
otherwise pass every behavioral test in this file.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

# discover -s tests puts this directory on the path; a direct
# `unittest tests.<module>` run does not, and CI uses both.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_corpus
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "require_consent.py"
LIVE_SETTINGS = REPO_ROOT / ".claude" / "settings.json"
EXAMPLE_SETTINGS = REPO_ROOT / "hooks" / "claude-code-settings.example.json"
BLOCKING_EXIT_CODE = 2
EDIT_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"

EXISTING_TEST = """test('device switch failure leaves the visualizer silent', function () {
  assert.equal(deviceBCalls, 1, 'no retry attempted');
  // The old stream was already released before the failed switch, so the
  // visualizer is honestly silent rather than misreporting the old device.
  assert.equal(viz.diagnostics().trackState, 'none');
});
"""


def run_hook(payload: dict, env: dict = None) -> tuple:
    """Return the hook's (exit code, parsed stdout) for `payload`."""
    environment = dict(os.environ)
    environment.pop("CLAUDE_PROJECT_DIR", None)
    if env:
        environment.update(env)
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    parsed = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.returncode, parsed


def decision_of(parsed: dict) -> str:
    """Return the permission decision in `parsed`, or an empty string."""
    return parsed.get("hookSpecificOutput", {}).get("permissionDecision", "")


def load_hook_module(name: str):
    """Load a distinct consent-hook module for direct OS-boundary tests."""
    spec = importlib.util.spec_from_file_location(name, HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def edit_payload(file_path: str, old: str, new: str, mode: str = "default") -> dict:
    """Return a PreToolUse Edit payload for `file_path`."""
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "permission_mode": mode,
        "tool_input": {"file_path": file_path, "old_string": old, "new_string": new},
    }


def _symlinks_available(directory: str) -> bool:
    """Return True when this platform lets an unprivileged process link.

    Windows raises WinError 1314 without Developer Mode or elevation, so
    an unconditional symlink fixture fails the suite for an ordinary
    contributor on a supported platform.
    """
    probe = Path(directory) / "_symlink_probe"
    try:
        probe.symlink_to(directory)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


class TestFileFixture(unittest.TestCase):
    """Base class providing a real test file on disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp.name)
        self.test_file = Path(self.tmp.name) / "tests" / "test_bcviz_api.js"
        self.test_file.parent.mkdir(parents=True)
        self.test_file.write_text(EXISTING_TEST, encoding="utf-8")
        self.source_file = Path(self.tmp.name) / "src" / "bcviz.js"
        self.source_file.parent.mkdir(parents=True)
        self.source_file.write_text("export function create() {}\n", encoding="utf-8")


class AllowTest(TestFileFixture):
    """Work the rules permit passes without a prompt."""

    def test_source_file_edit_passes(self):
        payload = edit_payload(str(self.source_file), "create() {}", "create() { return 1; }")
        code, parsed = run_hook(payload)
        self.assertEqual(code, 0)
        self.assertEqual(decision_of(parsed), "")

    def test_new_test_file_passes(self):
        target = str(Path(self.tmp.name) / "tests" / "test_new_feature.js")
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "permission_mode": "default",
            "tool_input": {"file_path": target, "content": "test('new', function () {});\n"},
        }
        code, parsed = run_hook(payload)
        self.assertEqual(code, 0)
        self.assertEqual(decision_of(parsed), "")

    def test_appending_a_test_asks(self):
        """Appending to a file that exists is still an edit to it.

        The carve-out this replaces cleared an append textually, which
        cannot tell a new test from a statement that neutralizes every
        test above it. Creating a new file stays unprompted, so the
        test-first workflow keeps its exemption where it is verifiable.
        """
        old = "});\n"
        new = "});\n\ntest('a new case', function () {\n  assert.equal(1, 1);\n});\n"
        code, parsed = run_hook(edit_payload(str(self.test_file), old, new))
        self.assertEqual(code, 0)
        self.assertEqual(decision_of(parsed), "ask")


class GateTest(TestFileFixture):
    """Edits that remove or weaken existing test content need the user."""

    def test_removing_an_assertion_asks(self):
        old = "  assert.equal(viz.diagnostics().trackState, 'none');\n"
        new = "  assert.equal(viz.diagnostics().trackState, 'device-b');\n"
        code, parsed = run_hook(edit_payload(str(self.test_file), old, new))
        self.assertEqual(code, 0)
        self.assertEqual(decision_of(parsed), "ask")

    def test_deleting_an_assertion_asks(self):
        old = "  assert.equal(viz.diagnostics().trackState, 'none');\n"
        code, parsed = run_hook(edit_payload(str(self.test_file), old, ""))
        self.assertEqual(decision_of(parsed), "ask")

    def test_introduced_skip_marker_asks_even_when_additive(self):
        old = "});\n"
        new = "});\n\ntest.skip('a case for later', function () {});\n"
        _, parsed = run_hook(edit_payload(str(self.test_file), old, new))
        self.assertEqual(decision_of(parsed), "ask")

    def test_overwriting_an_existing_test_file_asks(self):
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "permission_mode": "default",
            "tool_input": {"file_path": str(self.test_file), "content": "test('x', function () {});\n"},
        }
        _, parsed = run_hook(payload)
        self.assertEqual(decision_of(parsed), "ask")

    def test_direct_git_metadata_edit_asks(self):
        git_dir = Path(self.tmp.name) / ".git"
        git_dir.mkdir()
        head = git_dir / "HEAD"
        head.write_text("ref: refs/heads/feat/example\n", encoding="utf-8")
        payload = edit_payload(
            str(head),
            "ref: refs/heads/feat/example",
            "ref: refs/heads/feat/renamed",
        )
        _, parsed = run_hook(payload)
        self.assertEqual(decision_of(parsed), "ask")

    def test_multiedit_asks_when_any_edit_is_not_additive(self):
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "MultiEdit",
            "permission_mode": "default",
            "tool_input": {
                "file_path": str(self.test_file),
                "edits": [
                    {"old_string": "});\n", "new_string": "});\n\ntest('ok', function () {});\n"},
                    {"old_string": "assert.equal(deviceBCalls, 1, 'no retry attempted');", "new_string": ""},
                ],
            },
        }
        _, parsed = run_hook(payload)
        self.assertEqual(decision_of(parsed), "ask")

    def test_notebook_edit_uses_its_own_path_key(self):
        notebook = Path(self.tmp.name) / "tests" / "test_analysis.ipynb"
        notebook.write_text("{}", encoding="utf-8")
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "NotebookEdit",
            "permission_mode": "default",
            "tool_input": {"notebook_path": str(notebook), "new_source": "assert False"},
        }
        _, parsed = run_hook(payload)
        self.assertEqual(decision_of(parsed), "ask")

    def test_reason_names_the_file_and_the_rule(self):
        old = "  assert.equal(viz.diagnostics().trackState, 'none');\n"
        _, parsed = run_hook(edit_payload(str(self.test_file), old, ""))
        reason = parsed["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("test_bcviz_api.js", reason)
        self.assertIn("Rule 3", reason)

    def test_reason_states_that_plan_approval_is_not_consent(self):
        """Violation 3 of the incident: approval of a document read as per-act consent."""
        old = "  assert.equal(viz.diagnostics().trackState, 'none');\n"
        _, parsed = run_hook(edit_payload(str(self.test_file), old, ""))
        reason = parsed["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Approving a plan is not authorization for this edit", reason)

    def test_banned_models_edit_asks_and_cites_rule_22(self):
        models_file = Path(self.tmp.name) / "scripts" / "banned_models.txt"
        models_file.parent.mkdir(parents=True, exist_ok=True)
        models_file.write_text("grok*\n", encoding="utf-8")
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "permission_mode": "default",
            "tool_input": {
                "file_path": str(models_file),
                "old_string": "grok*\n",
                "new_string": "grok*\nxai\n",
            },
        }
        _, parsed = run_hook(payload)
        self.assertEqual(decision_of(parsed), "ask")
        reason = parsed["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Rule 22", reason)

    def test_banned_models_edit_denies_in_unattended_mode(self):
        models_file = Path(self.tmp.name) / "scripts" / "banned_models.txt"
        models_file.parent.mkdir(parents=True, exist_ok=True)
        models_file.write_text("grok*\n", encoding="utf-8")
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "permission_mode": "headless",
            "tool_input": {
                "file_path": str(models_file),
                "old_string": "grok*\n",
                "new_string": "grok*\nxai\n",
            },
        }
        code, parsed = run_hook(payload)
        self.assertEqual(code, BLOCKING_EXIT_CODE)
        self.assertEqual(decision_of(parsed), "deny")

    def test_banned_models_windows_decorations_ask(self):
        module = load_hook_module("require_consent_windows_decorations")
        for decorated in (
            "scripts/banned_models.txt:stream",
            "scripts/banned_models.txt.",
            "scripts/banned_models.txt ",
        ):
            with self.subTest(decorated=decorated):
                target = str(Path(self.tmp.name) / decorated)
                self.assertTrue(module.is_protected_path(target, self.tmp.name))
        self.assertFalse(module.is_protected_path("/outside/path.txt", self.tmp.name))


class PreservedTextEvasionTest(TestFileFixture):
    """Keeping the old text somewhere in the new text is not keeping the test.

    Every case here passed the original substring check, which asked only
    whether the old text still appeared anywhere in the new text. An
    assertion that is commented out, wrapped in a string, or moved into a
    branch that never runs is still present as text and no longer runs.
    """

    ASSERTION = "  assert.equal(viz.diagnostics().trackState, 'none');"

    def _decision_for(self, old: str, new: str) -> str:
        _, parsed = run_hook(edit_payload(str(self.test_file), old, new))
        return decision_of(parsed)

    def test_commenting_out_an_assertion_asks(self):
        for old, new in (
            ("assert authorized", "# assert authorized"),
            ("assert.ok(authorized)", "// assert.ok(authorized)"),
            (self.ASSERTION, "  // " + self.ASSERTION.strip()),
        ):
            with self.subTest(new=new):
                self.assertEqual(self._decision_for(old, new), "ask")

    def test_moving_an_assertion_into_dead_code_asks(self):
        new = "  if (false) {\n" + self.ASSERTION + "\n  }"
        self.assertEqual(self._decision_for(self.ASSERTION, new), "ask")

    def test_guarding_an_assertion_behind_a_condition_asks(self):
        new = "  if (process.env.CI) { assert.ok(x) }"
        self.assertEqual(self._decision_for("assert.ok(x)", new), "ask")

    def test_wrapping_an_assertion_in_a_string_asks(self):
        new = "  const dead = `" + self.ASSERTION + "`;"
        self.assertEqual(self._decision_for(self.ASSERTION, new), "ask")

    def test_appending_to_the_same_line_asks(self):
        """An addition that continues the last line can change its meaning."""
        self.assertEqual(self._decision_for("assert.ok(x)", "assert.ok(x) || true"), "ask")

    def test_insertion_into_the_middle_of_the_file_asks(self):
        """Only an end-of-file append can be verified as leaving the rest intact."""
        old = "  assert.equal(deviceBCalls, 1, 'no retry attempted');"
        new = old + "\n  assert.equal(2, 2);"
        self.assertEqual(self._decision_for(old, new), "ask")

    def test_end_of_file_append_asks(self):
        """An end-of-file append can still disable the tests above it."""
        old = "});\n"
        new = "});\n\ntest('a new case', function () {\n  assert.equal(1, 1);\n});\n"
        self.assertEqual(self._decision_for(old, new), "ask")

    def test_whole_file_append_via_write_asks(self):
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "permission_mode": "default",
            "tool_input": {
                "file_path": str(self.test_file),
                "content": EXISTING_TEST + "\ntest('added', function () {\n  assert.ok(1);\n});\n",
            },
        }
        _, parsed = run_hook(payload)
        self.assertEqual(decision_of(parsed), "ask")


class FailClosedTest(TestFileFixture):
    """Absence of a human is absence of consent."""

    def test_unattended_modes_deny(self):
        old = "  assert.equal(viz.diagnostics().trackState, 'none');\n"
        for mode in ("bypassPermissions", "dontAsk", "", "something-new"):
            with self.subTest(mode=mode):
                code, parsed = run_hook(edit_payload(str(self.test_file), old, "", mode))
                self.assertEqual(code, BLOCKING_EXIT_CODE)
                self.assertEqual(decision_of(parsed), "deny")

class PathAliasTest(unittest.TestCase):
    """A path is only as trustworthy as what it resolves to.

    Classifying the string the caller supplied, rather than the file it
    reaches, lets a symlink with an innocuous name carry an edit straight
    into a test file.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.root, "tests"))
        self.test_file = Path(self.root) / "tests" / "test_auth.js"
        self.test_file.write_text(EXISTING_TEST, encoding="utf-8")

    def _edit(self, target: str, env: dict = None, mode: str = "default") -> str:
        payload = edit_payload(target, "  assert.equal(deviceBCalls, 1, 'no retry attempted');", "", mode)
        payload["cwd"] = self.root
        _, parsed = run_hook(payload, env)
        return decision_of(parsed)

    def test_symlink_with_an_innocuous_name_is_still_a_test(self):
        if not _symlinks_available(self.tmp.name):
            self.skipTest("this platform needs elevation to create symlinks; "
                          "HardLinkTest covers alias resolution without it")
        alias = Path(self.root) / "notes.txt"
        alias.symlink_to(self.test_file)
        self.assertEqual(self._edit(str(alias)), "ask")

    def test_test_named_symlink_pointing_outside_the_root_is_gated(self):
        if not _symlinks_available(self.tmp.name):
            self.skipTest("this platform needs elevation to create symlinks; "
                          "HardLinkTest covers alias resolution without it")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(os.path.realpath(outside.name)) / "payload.js"
        target.write_text(EXISTING_TEST, encoding="utf-8")
        alias = Path(self.root) / "tests" / "test_escape.js"
        alias.symlink_to(target)
        self.assertEqual(self._edit(str(alias)), "ask")

    def test_test_file_outside_the_root_is_gated_without_any_link(self):
        """The gate speaks for one tree and is asked about anything outside it."""
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(os.path.realpath(outside.name)) / "tests"
        target.mkdir()
        external = target / "test_external.js"
        external.write_text(EXISTING_TEST, encoding="utf-8")
        self.assertEqual(self._edit(str(external)), "ask")

    def test_ordinary_non_test_file_still_passes(self):
        source = Path(self.root) / "src.js"
        source.write_text("export function create() {}\n", encoding="utf-8")
        payload = edit_payload(str(source), "create() {}", "create() { return 1; }")
        payload["cwd"] = self.root
        _, parsed = run_hook(payload)
        self.assertEqual(decision_of(parsed), "")


TWO_TESTS = """test('a', function () {
  assert.ok(1);
});

test('b', function () {
  assert.ok(2);
});
"""


class OccurrenceCountTest(unittest.TestCase):
    """An append is verifiable only when the old text occurs once, at the end.

    The hook ignored `replace_all`, so it validated one replacement at the
    end of the file while the tool rewrote every occurrence. The same hole
    exists without the flag: when the old text appears twice, a plain Edit
    replaces the first, which is mid-file, while `content.endswith(old)`
    still reported an append.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.root, "tests"))
        self.target = Path(self.root) / "tests" / "test_two.js"
        self.target.write_text(TWO_TESTS, encoding="utf-8")

    def _edit(self, old, new, replace_all=None):
        payload = edit_payload(str(self.target), old, new)
        payload["cwd"] = self.root
        if replace_all is not None:
            payload["tool_input"]["replace_all"] = replace_all
        _, parsed = run_hook(payload)
        return decision_of(parsed)

    APPEND = "});\n\ntest('c', function () {\n  assert.ok(3);\n});\n"

    def test_repeated_old_text_gates_with_replace_all(self):
        self.assertEqual(self._edit("});\n", self.APPEND, replace_all=True), "ask")

    def test_repeated_old_text_gates_without_replace_all(self):
        """A plain Edit replaces the first occurrence, which is not the end."""
        self.assertEqual(self._edit("});\n", self.APPEND), "ask")

    def test_single_occurrence_at_the_end_now_gates(self):
        """The append carve-out is gone: an existing test file always asks."""
        tail = "  assert.ok(2);\n});\n"
        self.assertEqual(
            self._edit(tail, tail + "\ntest('c', function () {});\n"), "ask")

    def test_single_occurrence_gates_under_replace_all(self):
        tail = "  assert.ok(2);\n});\n"
        new = tail + "\ntest('c', function () {});\n"
        self.assertEqual(self._edit(tail, new, replace_all=True), "ask")

    def test_multiedit_on_an_existing_test_gates(self):
        """Every edit to an existing test file asks, MultiEdit included."""
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "MultiEdit",
            "permission_mode": "default",
            "cwd": self.root,
            "tool_input": {
                "file_path": str(self.target),
                "edits": [
                    {"old_string": "assert.ok(1)", "new_string": "assert.ok(11)", "replace_all": True},
                    {"old_string": "});\n", "new_string": self.APPEND},
                ],
            },
        }
        _, parsed = run_hook(payload)
        self.assertEqual(decision_of(parsed), "ask")


class HardLinkTest(unittest.TestCase):
    """realpath resolves a symlink. A hard link has no target to resolve."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.root, "tests"))
        self.test_file = Path(self.root) / "tests" / "test_auth.js"
        self.test_file.write_text(EXISTING_TEST, encoding="utf-8")

    def _edit(self, target):
        payload = edit_payload(str(target), "  assert.equal(deviceBCalls, 1, 'no retry attempted');", "")
        payload["cwd"] = self.root
        _, parsed = run_hook(payload)
        return decision_of(parsed)

    def test_hard_link_under_an_innocuous_name_is_gated(self):
        alias = Path(self.root) / "notes.txt"
        os.link(self.test_file, alias)
        self.assertEqual(self._edit(alias), "ask")

    def test_unrelated_multiply_linked_file_is_not_gated(self):
        source = Path(self.root) / "src.js"
        source.write_text("export function create() {}\n", encoding="utf-8")
        os.link(source, Path(self.root) / "src-alias.js")
        payload = edit_payload(str(source), "create() {}", "create() { return 1; }")
        payload["cwd"] = self.root
        _, parsed = run_hook(payload)
        self.assertEqual(decision_of(parsed), "")

    def test_inode_walk_budget_exhaustion_gates_the_alias(self):
        alias = Path(self.root) / "notes.txt"
        os.link(self.test_file, alias)
        spec = importlib.util.spec_from_file_location("require_consent_budget", HOOK_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.MAX_INODE_WALK = 0
        target = module.resolve_target(str(alias), self.root)
        self.assertTrue(module.names_a_test(str(alias), target, self.root))

    def test_inode_walk_traversal_error_gates_the_alias(self):
        alias = Path(self.root) / "notes.txt"
        os.link(self.test_file, alias)
        module = self._load_module("require_consent_walk_error")
        target = module.resolve_target(str(alias), self.root)

        def failed_walk(_root, onerror=None):
            onerror(OSError("walk denied"))
            return []

        with unittest.mock.patch.object(module.os, "walk", side_effect=failed_walk):
            self.assertIsNone(module.reaches_a_test_inode(target, self.root))

    def test_candidate_stat_error_gates_the_alias(self):
        alias = Path(self.root) / "notes.txt"
        os.link(self.test_file, alias)
        module = self._load_module("require_consent_stat_error")
        target = module.resolve_target(str(alias), self.root)
        real_stat = module.os.stat

        def failed_stat(path):
            if os.path.realpath(path) == os.path.realpath(self.test_file):
                raise OSError("stat denied")
            return real_stat(path)

        with unittest.mock.patch.object(module.os, "stat", side_effect=failed_stat):
            self.assertIsNone(module.reaches_a_test_inode(target, self.root))

    @staticmethod
    def _load_module(name: str):
        spec = importlib.util.spec_from_file_location(name, HOOK_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class PathFieldTypeTest(unittest.TestCase):
    """A path that is not a string must deny rather than crash."""

    def test_non_string_path_denies(self):
        for value in (5, ["tests/test_x.js"], {"path": "x"}):
            with self.subTest(value=value):
                code, parsed = run_hook({
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Edit",
                    "permission_mode": "default",
                    "tool_input": {"file_path": value, "old_string": "a", "new_string": "b"},
                })
                self.assertEqual(code, BLOCKING_EXIT_CODE)
                self.assertEqual(decision_of(parsed), "deny")


class PathReasonTest(unittest.TestCase):
    """Out-of-tree and unreadable paths explain why consent is required."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.module = load_hook_module("require_consent_path_reasons")

    def test_redirected_out_of_root_path_names_the_link_boundary(self):
        raw = self.root / "linked" / "test_external.py"
        target = Path(self.tmp.name) / "outside" / "test_external.py"
        reason = self.module.escape_reason(
            str(raw), str(target), str(self.root))
        self.assertEqual(
            reason,
            "follows a link to a test file outside the project root, "
            "which the gate cannot vouch for",
        )

    def test_open_os_error_reports_that_the_target_cannot_be_confirmed(self):
        target = self.root / "tests" / "test_unreadable.py"
        error = PermissionError(13, "permission denied")
        with unittest.mock.patch.object(self.module.os, "open", side_effect=error):
            reason = self.module.find_gate_reason("Edit", str(target))
        self.assertEqual(
            reason,
            "cannot be opened (permission denied), so the gate cannot confirm "
            "what this edit changes",
        )

    def test_installed_policy_root_is_protected_from_another_working_directory(self):
        target = str(Path(self.module.__file__).resolve().parent / "_gate_core.py")
        payload = edit_payload(target, "old", "new")
        reason = self.module._write_reason(
            payload,
            target,
            target,
            str(self.root),
            self.module.core.policy_root(),
        )
        self.assertIn("decides whether these gates run", reason)


class FailClosedInputTest(unittest.TestCase):
    """A gate that cannot read its input must not answer 'fine'."""

    def test_unparseable_payload_denies(self):
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="this is not json",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, BLOCKING_EXIT_CODE)
        parsed = json.loads(result.stdout)
        self.assertEqual(decision_of(parsed), "deny")

    def test_empty_stdin_is_still_a_session_start(self):
        """No payload at all is the SessionStart invocation, not a failure."""
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Rule 3", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_non_dict_tool_input_denies(self):
        code, parsed = run_hook({
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "permission_mode": "default",
            "tool_input": "not a dict",
        })
        self.assertEqual(code, BLOCKING_EXIT_CODE)
        self.assertEqual(decision_of(parsed), "deny")


class UnreadableFileTest(unittest.TestCase):
    """A test file the gate cannot decode is not an empty file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.root, "tests"))

    def _write_over(self, target: str) -> str:
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "permission_mode": "default",
            "cwd": self.root,
            "tool_input": {"file_path": target, "content": "\ntest('replaced', function () {});\n"},
        }
        _, parsed = run_hook(payload)
        return decision_of(parsed)

    def test_non_utf8_test_file_is_gated(self):
        target = os.path.join(self.root, "tests", "test_binary.js")
        with open(target, "wb") as handle:
            handle.write(b"test('x', function () {\n  assert.ok(\xff\xfe);\n});\n")
        self.assertEqual(self._write_over(target), "ask")

    def test_unreadable_test_file_is_gated(self):
        target = os.path.join(self.root, "tests", "test_noread.js")
        Path(target).write_text(EXISTING_TEST, encoding="utf-8")
        os.chmod(target, 0o000)
        self.addCleanup(os.chmod, target, 0o644)
        self.assertEqual(self._write_over(target), "ask")

    def test_non_utf8_file_cannot_be_edited_through_an_empty_old_string(self):
        target = os.path.join(self.root, "tests", "test_binary2.js")
        with open(target, "wb") as handle:
            handle.write(b"assert.ok(\xff\xfe);\n")
        payload = edit_payload(target, "", "\nwhatever")
        payload["cwd"] = self.root
        _, parsed = run_hook(payload)
        self.assertEqual(decision_of(parsed), "ask")


class QuestionChecklistTest(unittest.TestCase):
    """The checklist arrives at SessionStart, before any question is written.

    It used to be injected on PreToolUse for AskUserQuestion, which is too
    late to help: by the time a tool call reaches a hook, the model has
    already written the question and its option labels, and additionalContext
    cannot rewrite them. Guidance that arrives after the decision it is meant
    to shape is decoration.
    """

    def test_checklist_is_in_the_session_notice(self):
        code, parsed = run_hook({"hook_event_name": "SessionStart"})
        self.assertEqual(code, 0)
        context = parsed["hookSpecificOutput"]["additionalContext"]
        self.assertIn("existing test", context)
        self.assertIn("Recommended", context)

    def test_ask_user_question_gets_no_decision(self):
        """The hook has nothing useful to say at this point and says nothing."""
        code, parsed = run_hook({
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "permission_mode": "default",
            "tool_input": {"questions": []},
        })
        self.assertEqual(code, 0)
        self.assertEqual(parsed, {})


class SessionStartTest(unittest.TestCase):
    """The session is told which gates are live."""

    def test_session_start_states_the_gates(self):
        code, parsed = run_hook({"hook_event_name": "SessionStart"})
        self.assertEqual(code, 0)
        self.assertIn("Rule 3", parsed["hookSpecificOutput"]["additionalContext"])


class SettingsWiringTest(unittest.TestCase):
    """The hook enforces nothing unless the settings files register it."""

    @staticmethod
    def _registers(entry: dict, hook_name: str) -> bool:
        """Return True if this hook entry runs `hook_name`, in either form."""
        parts = [entry.get("command", "")] + list(entry.get("args", []))
        return any(hook_name in part for part in parts)

    @classmethod
    def _matchers_for(cls, settings: dict, hook_name: str, event: str) -> list:
        """Return every matcher registering `hook_name` for `event`."""
        return [
            matcher.get("matcher", "")
            for matcher in settings.get("hooks", {}).get(event, [])
            for entry in matcher.get("hooks", [])
            if cls._registers(entry, hook_name)
        ]

    def _assert_registered(self, path: Path):
        settings = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(
            self._matchers_for(settings, "require_consent.py", "SessionStart"),
            f"{path.name} does not register require_consent.py for SessionStart",
        )
        pre_tool_use = self._matchers_for(settings, "require_consent.py", "PreToolUse")
        self.assertEqual(
            pre_tool_use, [EDIT_MATCHER],
            f"{path.name} must register require_consent.py for exactly {EDIT_MATCHER}",
        )

    def test_live_settings_register_the_hook(self):
        self._assert_registered(LIVE_SETTINGS)

    def test_example_settings_register_the_hook(self):
        self._assert_registered(EXAMPLE_SETTINGS)

    def test_entries_use_the_exec_form(self):
        """Shell form splits a project path containing spaces and the hook never runs."""
        for path in (LIVE_SETTINGS, EXAMPLE_SETTINGS):
            settings = json.loads(path.read_text(encoding="utf-8"))
            for event in ("SessionStart", "PreToolUse"):
                for matcher in settings["hooks"][event]:
                    for entry in matcher.get("hooks", []):
                        command = entry.get("command", "")
                        with self.subTest(path=path.name, command=command):
                            self.assertTrue(entry.get("args"), "exec form requires args")
                            self.assertTrue(command, "no launcher configured")
                            # A launcher carrying a space or a shell
                            # metacharacter is shell form wearing exec
                            # form's shape. Which program it names is
                            # asserted by the launcher-resolution test,
                            # which requires it to exist on PATH.
                            self.assertNotIn(" ", command)
                            self.assertFalse(
                                set(command) & set("$\"'|&;<>()"),
                                "launcher carries shell syntax")

    def test_destructive_bash_hook_is_registered(self):
        """The incident's rm -rf and force-push ran because nothing wired this."""
        for path in (LIVE_SETTINGS, EXAMPLE_SETTINGS):
            with self.subTest(path=path.name):
                settings = json.loads(path.read_text(encoding="utf-8"))
                matchers = self._matchers_for(settings, "block_destructive_bash.py", "PreToolUse")
                self.assertIn("Bash", matchers, f"{path.name} does not register the Bash gate")


class CorpusTest(unittest.TestCase):
    """Every known consent-gate path reaches the verdict the corpus records."""

    @staticmethod
    def decision_for(relative: str, exists: bool) -> str:
        """Return the gate's decision for `relative` inside a fresh project."""
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if exists:
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write(EXISTING_TEST)
            payload = edit_payload(target, "assert True", "assert False")
            payload["cwd"] = root
            _, parsed = run_hook(payload)
            return decision_of(parsed) or gate_corpus.ALLOW

    def test_every_corpus_row_reaches_its_verdict(self):
        for relative, exists, expected, why in gate_corpus.CONSENT_CASES:
            with self.subTest(path=relative, why=why):
                self.assertEqual(self.decision_for(relative, exists), expected)


if __name__ == "__main__":
    unittest.main()
