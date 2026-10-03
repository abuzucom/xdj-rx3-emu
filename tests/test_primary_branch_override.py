"""hooks/primary-branch.txt names one primary branch in place of main."""
import unittest

from tests.read_only_branch_support import BranchFixture, decision

INVALID_OVERRIDES = (
    b"", b"\n", b"develop\nmain\n", b"master\n", b"HEAD\n", b"bad name\n",
    b"-develop\n", "dévelop\n".encode("utf-8"), b"x" * 300 + b"\n",
)


class PrimaryBranchOverrideTest(unittest.TestCase):
    """The override moves the read-only state from main to the named branch."""

    def read(self, fixture: BranchFixture):
        return fixture.run("Read", {"file_path": str(fixture.root / "README.md")})

    def test_override_branch_is_read_only(self):
        for content in (b"develop\n", b"develop\r\n", b"develop"):
            with self.subTest(content=content):
                fixture = BranchFixture(self, "develop")
                fixture.write_override(content)
                self.assertEqual(decision(self.read(fixture)), "allow")
                self.assertEqual(decision(fixture.shell("git status")), "allow")
                denied = fixture.shell("touch notes.txt")
                self.assertEqual(decision(denied), "deny")
                self.assertIn("`develop` permits read-only inspection", denied.stderr)

    def test_main_is_strict_under_override(self):
        fixture = BranchFixture(self, "main")
        fixture.write_override(b"develop\n")
        result = self.read(fixture)
        self.assertEqual(decision(result), "deny")
        self.assertIn("git switch -c", result.stderr)
        self.assertEqual(decision(fixture.shell("git switch -c feat/new-work")), "ask")

    def test_develop_is_strict_without_override(self):
        fixture = BranchFixture(self, "develop")
        result = self.read(fixture)
        self.assertEqual(decision(result), "deny")
        self.assertIn("git branch -m", result.stderr)

    def test_invalid_override_denies_every_tool(self):
        for content in INVALID_OVERRIDES:
            for branch in ("develop", "main"):
                with self.subTest(content=content[:20], branch=branch):
                    fixture = BranchFixture(self, branch)
                    fixture.write_override(content)
                    result = self.read(fixture)
                    self.assertEqual(decision(result), "deny")
                    self.assertIn("hooks/primary-branch.txt", result.stderr)
                    start = fixture.lifecycle("SessionStart")
                    self.assertIn("hooks/primary-branch.txt", start.stdout)

    def test_directory_override_fails_closed(self):
        fixture = BranchFixture(self, "main")
        (fixture.root / "hooks" / "primary-branch.txt").mkdir(parents=True)
        result = self.read(fixture)
        self.assertEqual(decision(result), "deny")
        self.assertIn("hooks/primary-branch.txt", result.stderr)

    def test_invalid_override_blocks_lifecycle_completion(self):
        fixture = BranchFixture(self, "main")
        fixture.write_override(b"master\n")
        stop = fixture.lifecycle("Stop")
        self.assertIn("block", stop.stdout)
        self.assertIn("hooks/primary-branch.txt", stop.stdout)
        prompt = fixture.lifecycle("UserPromptSubmit")
        self.assertIn("hooks/primary-branch.txt", prompt.stdout)


if __name__ == "__main__":
    unittest.main()
