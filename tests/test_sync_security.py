#!/usr/bin/env python3
"""Exercise filesystem safety boundaries for AGENTS.md synchronization."""
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import sync


class SyncSecurityTest(unittest.TestCase):
    """Run sync_copies against an isolated synthetic repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.root = self.workspace / "repo"
        (self.root / "scripts").mkdir(parents=True)
        supporting = self.root / "docs" / "agent-policy"
        supporting.mkdir(parents=True)
        for relative_name in sync.SUPPORTING_POLICY_FILES:
            (self.root / relative_name).write_text(
                "detail\n", encoding="utf-8")
        self.source = self.root / sync.SOURCE
        self.target = self.root / "COPY.md"

    def create_symlink(
            self, link: Path, target: Path,
            target_is_directory: bool = False) -> None:
        """Create a symlink, skipping only when the platform denies it."""
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except OSError as error:
            self.skipTest(f"platform denied symlink creation: {error}")

    def run_sync(self, *copies: str) -> int:
        """Run the public copy path with its repository root redirected."""
        script = self.root / "scripts" / "sync.py"
        with mock.patch.object(sync, "__file__", str(script)), \
                mock.patch.object(sync, "COPIES", list(copies)):
            return sync.sync_copies(check_only=False)

    def test_rejects_symlinked_agents_source(self):
        external_source = self.workspace / "external-agents.md"
        external_source.write_text("external source\n", encoding="utf-8")
        self.create_symlink(self.source, external_source)

        result = self.run_sync("COPY.md")

        self.assertNotEqual(result, 0)
        self.assertFalse(self.target.exists())

    def test_rejects_symlinked_copy_target(self):
        self.source.write_text("new source\n", encoding="utf-8")
        external_target = self.workspace / "external-copy.md"
        external_target.write_text("old destination\n", encoding="utf-8")
        self.create_symlink(self.target, external_target)

        result = self.run_sync("COPY.md")

        self.assertNotEqual(result, 0)
        self.assertEqual(
            external_target.read_text(encoding="utf-8"), "old destination\n")

    def test_rejects_symlinked_target_parent(self):
        self.source.write_text("new source\n", encoding="utf-8")
        external_parent = self.workspace / "external-parent"
        external_parent.mkdir()
        linked_parent = self.root / "linked-parent"
        self.create_symlink(
            linked_parent, external_parent, target_is_directory=True)

        result = self.run_sync("linked-parent/COPY.md")

        self.assertNotEqual(result, 0)
        self.assertFalse((external_parent / "COPY.md").exists())

    def test_rejects_target_resolving_outside_repository(self):
        self.source.write_text("new source\n", encoding="utf-8")
        escaped_target = self.workspace / "escaped.md"
        escaped_target.write_text("old destination\n", encoding="utf-8")

        result = self.run_sync("../escaped.md")

        self.assertNotEqual(result, 0)

    def test_oversized_supporting_file_fails_before_copy(self):
        self.source.write_text("new source\n", encoding="utf-8")
        supporting = self.root / sync.SUPPORTING_POLICY_FILES[0]
        supporting.write_bytes(b"x" * (sync.MAX_POLICY_BYTES + 1))
        self.assertNotEqual(self.run_sync("COPY.md"), 0)

    def test_adoptable_output_includes_supporting_policy(self):
        self.source.write_text(
            "prefix\n" + sync.REPOSITORY_ONLY_START
            + "\nlocal\n" + sync.REPOSITORY_ONLY_END + "\nsuffix\n",
            encoding="utf-8")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(sync.print_adoptable(self.root), 0)
        self.assertIn("prefix", output.getvalue())
        self.assertIn("detail", output.getvalue())

    def test_source_only_supporting_policy_is_not_adoptable(self):
        self.source.write_text(
            f"prefix\n{sync.REPOSITORY_ONLY_START}\n"
            f"{sync.REPOSITORY_ONLY_END}\nsuffix\n",
            encoding="utf-8")
        source_only = self.root / sync.SOURCE_ONLY_POLICY_FILES[0]
        source_only.parent.mkdir(parents=True, exist_ok=True)
        source_only.write_text(
            f"{sync.SOURCE_ONLY_START}\nsource facts\n"
            f"{sync.SOURCE_ONLY_END}\n",
            encoding="utf-8")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(sync.print_adoptable(self.root), 0)
        self.assertNotIn("source facts", output.getvalue())

    def test_failed_copy_does_not_replace_old_destination(self):
        self.source.write_text("new source\n", encoding="utf-8")
        self.target.write_text("old destination\n", encoding="utf-8")

        def fail_during_copy(_source, destination):
            Path(destination).write_text("partial write\n", encoding="utf-8")
            raise OSError("simulated write failure")

        result = None
        with mock.patch.object(sync.shutil, "copyfile", fail_during_copy):
            try:
                result = self.run_sync("COPY.md")
            except OSError:
                pass

        if result is not None:
            self.assertNotEqual(result, 0)
        self.assertEqual(
            self.target.read_text(encoding="utf-8"), "old destination\n")

    def test_missing_supporting_file_fails_closed(self):
        (self.root / sync.SUPPORTING_POLICY_FILES[0]).unlink()
        self.source.write_text("source\n", encoding="utf-8")
        self.assertNotEqual(self.run_sync("COPY.md"), 0)

    def test_non_ascii_supporting_file_fails_closed(self):
        self.source.write_text("source\n", encoding="utf-8")
        (self.root / sync.SUPPORTING_POLICY_FILES[0]).write_bytes(b"bad\xc3\xa9\n")
        self.assertNotEqual(self.run_sync("COPY.md"), 0)

    @unittest.skipIf(os.name == "nt", "Windows does not expose POSIX file modes")
    def test_copy_preserves_source_mode(self):
        self.source.write_text("new source\n", encoding="utf-8")
        self.source.chmod(0o644)

        result = self.run_sync("COPY.md")

        self.assertEqual(result, 0)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
