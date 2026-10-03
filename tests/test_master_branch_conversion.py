"""master is not allowed; only a switch to an existing main is offered."""
import unittest

from tests.read_only_branch_support import BranchFixture, decision

MASTER_MESSAGE = "`master` is not allowed. Convert this repository to use `main`."
DENIED_COMMANDS = (
    "git switch -c feat/x", "git checkout -b feat/x", "git branch -m main",
    "git branch -m feat/x", "git branch -M main", "git branch -d master",
    "git branch -D master", "git status", "git switch main extra", "git checkout main",
)


class MasterBranchTest(unittest.TestCase):
    """Relay the conversion requirement and never perform the conversion."""

    def test_ordinary_tools_are_denied_with_conversion_message(self):
        fixture = BranchFixture(self, "master")
        for tool, tool_input in (("Read", {"file_path": str(fixture.root / "README.md")}),
                                 ("Write", {"file_path": str(fixture.root / "x"), "content": ""}),
                                 ("ExitPlanMode", {})):
            with self.subTest(tool=tool):
                result = fixture.run(tool, tool_input)
                self.assertEqual(decision(result), "deny")
                self.assertIn(MASTER_MESSAGE, result.stderr)
                self.assertIn("active-human decisions", result.stderr)

    def test_questions_stay_available(self):
        fixture = BranchFixture(self, "master")
        self.assertEqual(decision(fixture.run("AskUserQuestion", {"questions": []})), "allow")

    def test_switch_to_existing_main_requests_consent(self):
        fixture = BranchFixture(self, "master")
        self.assertEqual(decision(fixture.shell("git switch main")), "deny")
        fixture.add_local_branch("main")
        result = fixture.shell("git switch main")
        self.assertEqual(decision(result), "ask")
        self.assertIn(MASTER_MESSAGE, result.stdout)
        command = f"git -C {fixture.root.as_posix()} switch main"
        self.assertEqual(decision(fixture.shell(command)), "ask")

    def test_conversion_commands_are_denied(self):
        fixture = BranchFixture(self, "master")
        fixture.add_local_branch("main")
        for command in DENIED_COMMANDS:
            with self.subTest(command=command):
                result = fixture.shell(command)
                self.assertEqual(decision(result), "deny")
                self.assertIn(MASTER_MESSAGE, result.stderr)

    def test_lifecycle_messages_carry_conversion_message(self):
        fixture = BranchFixture(self, "master")
        for event in ("SessionStart", "UserPromptSubmit", "Stop"):
            with self.subTest(event=event):
                result = fixture.lifecycle(event)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(MASTER_MESSAGE, result.stdout)
        self.assertIn("block", fixture.lifecycle("Stop").stdout)

    def test_gemini_receives_native_denial(self):
        fixture = BranchFixture(self, "master")
        result = fixture.run("read_file", {"path": "README.md"}, client="gemini")
        self.assertEqual(decision(result, "gemini"), "deny")
        self.assertIn(MASTER_MESSAGE, result.stdout)


if __name__ == "__main__":
    unittest.main()
