"""Exercise branch preflight independently of the runner checkout state."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tests.test_enforce_branch_name import REPO_ROOT, hook


class CiBranchFixtureTest(unittest.TestCase):
    """Use real compliant metadata for every valid-branch handler path."""

    def test_valid_branch_dispatch_uses_isolated_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref: refs/heads/fix/fixture\n", encoding="utf-8")
            (root / "scripts").mkdir()
            for name in ("check_branch_name.py", "trusted_git.py"):
                (root / "scripts" / name).write_bytes((REPO_ROOT / "scripts" / name).read_bytes())
            cases = (
                ({"tool_name": [], "tool_input": {}}, 2),
                ({"tool_name": "Write", "tool_input": {"file_path": "notes.txt", "content": "text"}}, 0),
                ({"tool_name": "Bash", "tool_input": {"command": "echo safe"}}, 0),
                ({"tool_name": "Bash", "tool_input": {"command": "python scripts/read_git_state.py branch"}}, 0),
            )
            with patch.object(hook.core, "policy_root", return_value=str(root)):
                for payload, expected in cases:
                    with self.subTest(payload=payload):
                        self.assertEqual(hook._handle_pre_tool_use(payload, str(root), "claude"), expected)


class ImmutableAuthenticationTest(unittest.TestCase):
    """Require a scoped token for the trusted commit attribution lookup."""

    def test_scanner_receives_read_only_job_token(self) -> None:
        path = REPO_ROOT / ".github" / "workflows" / "immutable-conflict-check.yml"
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        job = workflow["jobs"]["immutable-compliance"]
        scan = next(step for step in job["steps"] if "$TRUSTED_CHECKER" in step.get("run", ""))
        self.assertEqual(scan.get("env", {}).get("GH_TOKEN"), "${{ github.token }}")
        self.assertEqual(job["permissions"], {"contents": "read"})


if __name__ == "__main__":
    unittest.main()
