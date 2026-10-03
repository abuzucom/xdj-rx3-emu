"""Read-only branches accept plan and scratch writes only under client roots."""
import json
import ntpath
import os
import posixpath
import unittest

from tests.read_only_branch_support import BranchFixture, decision
from tests.test_enforce_branch_name import hook

POSIX_SPECS = (
    ("tree", "/home/u/.claude/plans"),
    ("scratchpad", "/tmp"),
    ("gemini_plans", "/home/u/.gemini/tmp"),
    ("child", "/home/u/.gemini/antigravity/brain"),
)
WINDOWS_SPECS = (
    ("tree", r"C:\Users\u\.claude\plans"),
    ("scratchpad", r"C:\Users\u\AppData\Local\Temp"),
)


class WriteRootMatchTest(unittest.TestCase):
    """Run the pure root matcher with explicit path modules on every OS."""

    def assert_paths(self, specs, pathmod, allowed, denied):
        for path in allowed:
            with self.subTest(path=path):
                self.assertTrue(hook.is_allowed_write_path(path, specs, pathmod))
        for path in denied:
            with self.subTest(path=path):
                self.assertFalse(hook.is_allowed_write_path(path, specs, pathmod))

    def test_posix_roots(self):
        self.assert_paths(
            POSIX_SPECS, posixpath,
            allowed=(
                "/home/u/.claude/plans/plan.md",
                "/home/u/.claude/plans/nested/plan.md",
                "/tmp/claude-0/-home-user-agents/session/scratchpad/notes.md",
                "/tmp/claude-1000/project/scratchpad/deep/notes.md",
                "/home/u/.gemini/tmp/project/plans/plan.md",
                "/home/u/.gemini/antigravity/brain/abc/plan.md",
            ),
            denied=(
                "/home/u/.claude/plans", "/home/u/.claude/plansx/plan.md",
                "/home/u/.claude/plans/../../.bashrc", "plans/plan.md", "",
                "/tmp/other/scratchpad/notes.md", "/tmp/claude-0/scratchpad",
                "/tmp/claude-0/project/notes.md", "/home/u/.gemini/tmp/project/notes.md",
                "/home/u/.gemini/tmp/project/plans", "/home/u/.gemini/antigravity/brain/abc",
                "/home/u/.claude/plans/a\0b",
            ),
        )

    def test_windows_roots_through_ntpath(self):
        self.assert_paths(
            WINDOWS_SPECS, ntpath,
            allowed=(
                r"C:\Users\u\.claude\plans\plan.md",
                r"c:\users\U\.Claude\PLANS\plan.md",
                "C:/Users/u/.claude/plans/plan.md",
                r"C:\Users\u\AppData\Local\Temp\claude-0\p\scratchpad\n.md",
            ),
            denied=(
                r"D:\Users\u\.claude\plans\plan.md",
                r"\\server\share\Users\u\.claude\plans\plan.md",
                r"\\?\C:\Users\u\.claude\plans\plan.md",
                r"\\.\C:\Users\u\.claude\plans\plan.md",
                r"C:\Users\u\.claude\plans\plan.md:stream",
                r"C:\Users\u\.claude\plans\CON",
                r"C:\Users\u\.claude\plans\nul.txt",
                r"C:\Users\u\.claude\plans\com1.md",
                r"Users\u\.claude\plans\plan.md",
            ),
        )

    def test_unknown_spec_kind_matches_nothing(self):
        self.assertFalse(hook.is_allowed_write_path(
            "/home/u/.claude/plans/plan.md", (("other", "/home/u/.claude/plans"),), posixpath))


class HookWriteRootTest(unittest.TestCase):
    """Send real file-tool payloads on main with isolated home and temp roots."""

    def setUp(self):
        self.fixture = BranchFixture(self, "main")
        self.home = self.fixture.home

    def write(self, path, client="claude", tool=None):
        if client == "antigravity":
            return self.fixture.run(tool or "write_to_file",
                                    {"TargetFile": str(path), "CodeContent": "x"}, client)
        name = tool or ("write_file" if client == "gemini" else "Write")
        return self.fixture.run(name, {"file_path": str(path), "content": "x"}, client)

    def test_claude_plan_and_scratchpad_roots(self):
        plan = self.home / ".claude" / "plans" / "plan.md"
        scratch = self.fixture.temp / "claude-0" / "project" / "session" / "scratchpad" / "n.md"
        for path in (plan, scratch):
            with self.subTest(path=path):
                self.assertEqual(decision(self.write(path)), "allow")
        edit = self.fixture.run("Edit", {"file_path": str(plan), "old_string": "a",
                                         "new_string": "b"})
        self.assertEqual(decision(edit), "allow")
        self.assertEqual(decision(self.write(self.home / ".bashrc")), "deny")

    def test_codex_has_no_write_roots(self):
        plan = self.home / ".claude" / "plans" / "plan.md"
        self.assertEqual(decision(self.write(plan, client="codex")), "deny")

    def test_gemini_default_and_user_setting_roots(self):
        default = self.home / ".gemini" / "tmp" / "project" / "plans" / "plan.md"
        self.assertEqual(decision(self.write(default, "gemini"), "gemini"), "allow")
        custom = self.home / "custom-plans"
        settings = self.home / ".gemini" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"general": {"plan": {"directory": str(custom)}}}),
                            encoding="utf-8")
        result = self.write(custom / "plan.md", "gemini", tool="replace")
        self.assertEqual(decision(result, "gemini"), "allow", result.stdout)

    def test_gemini_project_and_unsafe_settings_are_ignored(self):
        project_plans = self.home / "project-plans"
        project_settings = self.fixture.root / ".gemini" / "settings.json"
        project_settings.parent.mkdir()
        project_settings.write_text(
            json.dumps({"general": {"plan": {"directory": str(project_plans)}}}), encoding="utf-8")
        self.assertEqual(decision(self.write(project_plans / "p.md", "gemini"), "gemini"), "deny")
        settings = self.home / ".gemini" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        for value in (str(self.home), os.path.abspath(os.sep), "relative-plans", 7, "{"):
            with self.subTest(value=value):
                document = "{" if value == "{" else json.dumps(
                    {"general": {"plan": {"directory": value}}})
                settings.write_text(document, encoding="utf-8")
                result = self.write(self.home / ".bashrc", "gemini")
                self.assertEqual(decision(result, "gemini"), "deny")

    def test_antigravity_brain_root(self):
        allowed = self.home / ".gemini" / "antigravity" / "brain" / "abc" / "plan.md"
        self.assertEqual(decision(self.write(allowed, "antigravity"), "antigravity"), "allow")
        replace = self.write(allowed, "antigravity", tool="replace_file_content")
        self.assertEqual(decision(replace, "antigravity"), "allow")
        outside = self.home / ".gemini" / "antigravity" / "plan.md"
        self.assertEqual(decision(self.write(outside, "antigravity"), "antigravity"), "deny")

    def test_repository_and_symlink_escapes_are_denied(self):
        plans = self.home / ".claude" / "plans"
        plans.mkdir(parents=True)
        link = plans / "link"
        try:
            os.symlink(self.fixture.root, link, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlink creation needs privileges here: {error}")
        self.assertEqual(decision(self.write(link / "README.md")), "deny")
        self.assertEqual(decision(self.write(self.fixture.root / "README.md")), "deny")


if __name__ == "__main__":
    unittest.main()
