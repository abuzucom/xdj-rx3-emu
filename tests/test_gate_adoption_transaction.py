#!/usr/bin/env python3
"""Test atomic cross-client gate-adoption verification."""
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_gate_adoption

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def run_check(root: Path) -> tuple[int, str]:
    """Return the transaction-check exit code and diagnostics."""
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = check_gate_adoption.main(["--root", str(root)])
    return code, stderr.getvalue()


class TransactionRegistrationTest(unittest.TestCase):
    """Every adopted client must register the transaction gate."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repository"
        self.root.mkdir()
        self.addCleanup(self.temporary.cleanup)

    def copy_tree(self) -> Path:
        """Copy every static gate-adoption artifact into a fixture root."""
        for relative in (
            "hooks",
            "scripts",
            "tests",
            ".claude",
            ".codex",
            ".agents",
            ".gemini",
        ):
            shutil.copytree(REPOSITORY_ROOT / relative, self.root / relative)
        for relative in ("AGENTS.md", "shared-files.json"):
            shutil.copy(REPOSITORY_ROOT / relative, self.root / relative)
        return self.root

    def test_live_repository_registers_every_client(self) -> None:
        code, output = run_check(REPOSITORY_ROOT)
        self.assertEqual(code, 0, output)

    def test_omitted_initiating_client_fails(self) -> None:
        root = self.copy_tree()
        path = root / ".codex" / "hooks.json"
        content = path.read_text(encoding="utf-8")
        path.write_text(
            content.replace("enforce_gate_adoption.py", "omitted_gate.py"),
            encoding="utf-8",
        )
        code, output = run_check(root)
        self.assertEqual(code, 1)
        self.assertIn("codex", output)
        self.assertIn("enforce_gate_adoption.py", output)

    def test_transaction_hook_is_required(self) -> None:
        self.assertEqual(
            check_gate_adoption.TRANSACTION_HOOK,
            "enforce_gate_adoption.py",
        )


if __name__ == "__main__":
    unittest.main()
