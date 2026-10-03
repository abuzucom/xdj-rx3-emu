#!/usr/bin/env python3
"""Test trusted workflow schema normalization."""
import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_compliance_tree


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "immutable-conflict-check.yml"


class ComplianceTreeTest(unittest.TestCase):
    """Numeric YAML versions remain valid trusted workflow candidates."""

    def test_unquoted_numeric_python_version_matches_candidate_job(self):
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        text = text.replace("python-version: '3.x'", "python-version: 3.12")
        document = yaml.safe_load(text)
        violations = check_compliance_tree._pull_target_violations(
            document, text, str(WORKFLOW_PATH))
        self.assertNotIn("job schema is not trusted", " ".join(violations))


if __name__ == "__main__":
    unittest.main()
