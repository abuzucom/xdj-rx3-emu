import unittest
import tempfile
from pathlib import Path

from scripts.check_policy_size import find_violations


class PolicySizeTests(unittest.TestCase):
    def test_canonical_policy_has_required_size_and_rules(self):
        policy = Path("AGENTS.md").read_bytes()

        self.assertEqual(find_violations(Path("AGENTS.md")), [])
        text = policy.decode("ascii")
        for required in (
            "## Non-negotiable",
            "## Critical rules",
            "## Code quality",
            "## Style",
            "Change policy safely",
            "Version every change",
            "xAI",
            "Grok",
        ):
            self.assertIn(required, text)

    def test_missing_policy_reports_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "AGENTS.md"
            self.assertTrue(find_violations(missing))

    def test_oversized_policy_reports_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "AGENTS.md"
            path.write_bytes(b"x" * (32 * 1024 + 1))
            self.assertTrue(any("exceeds" in item for item in find_violations(path)))

    def test_non_ascii_policy_reports_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "AGENTS.md"
            path.write_bytes(b"bad\xc3\xa9")
            self.assertTrue(any("not ASCII" in item for item in find_violations(path)))

    def test_crlf_policy_reports_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "AGENTS.md"
            path.write_bytes(b"policy\r\n")
            self.assertTrue(any("LF line endings" in item
                                for item in find_violations(path)))


if __name__ == "__main__":
    unittest.main()
