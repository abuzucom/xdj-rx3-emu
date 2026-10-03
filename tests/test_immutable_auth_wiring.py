"""Require scoped authentication for immutable commit attribution."""
import unittest
from pathlib import Path

import yaml

from scripts import check_compliance_tree as checker


class ImmutableAuthenticationTest(unittest.TestCase):
    """Preserve the exact trusted execution surface and read-only access."""

    def test_scanner_token_matches_trusted_schema(self) -> None:
        root = Path(__file__).resolve().parents[1]
        path = root / ".github" / "workflows" / "immutable-conflict-check.yml"
        text = path.read_text(encoding="utf-8")
        workflow = yaml.safe_load(text)
        job = workflow["jobs"]["immutable-compliance"]
        scan = job["steps"][-1]
        self.assertEqual(scan.get("env"), {"GH_TOKEN": "${{ github.token }}"})
        self.assertEqual(job["permissions"], {"contents": "read"})
        self.assertEqual(checker._pull_target_violations(workflow, text, str(path)), [])
        scan["env"]["GH_TOKEN"] = "untrusted"
        self.assertTrue(checker._pull_target_violations(workflow, text, str(path)))


if __name__ == "__main__":
    unittest.main()
