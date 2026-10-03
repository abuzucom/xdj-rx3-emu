#!/usr/bin/env python3
"""Test trusted base-ref checks for protected gate changes."""
import sys
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_gate_pr_integrity as integrity


class ProtectedPathTest(unittest.TestCase):
    """Protected paths require out-of-band human approval."""

    def test_hook_change_requires_approval(self) -> None:
        self.assertTrue(integrity.requires_approval(["hooks/_gate_core.py"]))

    def test_client_configuration_requires_approval(self) -> None:
        self.assertTrue(integrity.requires_approval([".codex/hooks.json"]))

    def test_documentation_only_change_does_not_require_approval(self) -> None:
        self.assertFalse(integrity.requires_approval(["README.md"]))

    def test_approval_label_matches_exact_value(self) -> None:
        self.assertTrue(integrity.has_approval_label([integrity.APPROVAL_LABEL]))
        self.assertFalse(integrity.has_approval_label(["gate-change-approved-now"]))

    def test_existing_head_skips_trusted_fetch(self) -> None:
        with patch.object(integrity, "_has_revision", return_value=True):
            with patch.object(integrity, "_fetch_head") as fetch_head:
                with patch.object(integrity, "_changed_paths", return_value=[]):
                    result = integrity.main([
                        "--base", "a" * 40,
                        "--head", "b" * 40,
                        "--pr-number", "1",
                        "--labels-json", "[]",
                    ])
        self.assertEqual(result, 0)
        fetch_head.assert_not_called()

    def test_fetch_head_uses_workspace_root_with_real_git(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.git"
            seed = root / "seed"
            workspace = root / "workspace"
            subprocess.run(["git", "init", "--bare", str(source)], check=True)
            subprocess.run(["git", "init", str(seed)], check=True)
            subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test User"], check=True)
            subprocess.run(
                ["git", "-C", str(seed), "config", "user.email", "test@example.test"],
                check=True,
            )
            (seed / "fixture.txt").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(seed), "add", "fixture.txt"], check=True)
            subprocess.run(["git", "-C", str(seed), "commit", "-m", "test: add fixture"], check=True)
            revision = subprocess.run(
                ["git", "-C", str(seed), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", str(seed), "push", str(source), "HEAD:refs/pull/1/head"],
                check=True,
            )
            subprocess.run(["git", "init", str(workspace)], check=True)
            subprocess.run(
                ["git", "-C", str(workspace), "remote", "add", "origin", str(source)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(workspace), "config", "protocol.file.allow", "always"],
                check=True,
            )
            (workspace / "scripts").mkdir()
            shutil.copyfile(
                Path(integrity.__file__).parent / "trusted_git.py",
                workspace / "scripts" / "trusted_git.py",
            )

            integrity._fetch_head(workspace, 1)

            self.assertTrue(integrity._has_revision(workspace, revision))


if __name__ == "__main__":
    unittest.main()
