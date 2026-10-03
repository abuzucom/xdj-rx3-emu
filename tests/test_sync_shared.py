#!/usr/bin/env python3
"""Cover the shared-file manifest that watches the gates across repos.

sync.py copies the AGENTS.md family. It cannot copy the gate files, which
live in repositories it cannot see, so --check-shared compares hashes
against a manifest committed in each. These tests build a synthetic tree
rather than touching the real one, so a failure here is the checker's and
not the repository's.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# scripts/ is never on the path, however the suite is launched, so SHARED_FILES
# has to be read from the module that defines it rather than restated here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import sync

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNC = REPO_ROOT / "scripts" / "sync.py"
MANIFEST = REPO_ROOT / "shared-files.json"


def run_sync(cwd: Path, *args) -> subprocess.CompletedProcess:
    """Run sync.py inside `cwd` with the given flags."""
    return subprocess.run(
        [sys.executable, str(cwd / "scripts" / "sync.py"), *args],
        capture_output=True, text=True, check=False)


class ManifestFixture(unittest.TestCase):
    """A synthetic repository holding every shared file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "sync.py").write_text(
            SYNC.read_text(encoding="utf-8"), encoding="utf-8")
        for name in sync.SHARED_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {name}\nbody = 1\n", encoding="utf-8")
        run_sync(self.root, "--write-shared")


class CheckSharedTest(ManifestFixture):
    """A shared file that changes without the manifest fails the check."""

    def test_a_matching_tree_passes(self):
        result = run_sync(self.root, "--check-shared")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_changed_shared_file_fails(self):
        target = self.root / sync.SHARED_FILES[0]
        target.write_text("# changed\nbody = 2\n", encoding="utf-8")
        result = run_sync(self.root, "--check-shared")
        self.assertEqual(result.returncode, 1)
        self.assertIn(sync.SHARED_FILES[0], result.stderr)
        self.assertIn("differs", result.stderr)

    def test_a_missing_shared_file_fails(self):
        os.remove(self.root / sync.SHARED_FILES[0])
        result = run_sync(self.root, "--check-shared")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not present here", result.stderr)

    def test_a_missing_manifest_fails(self):
        os.remove(self.root / "shared-files.json")
        result = run_sync(self.root, "--check-shared")
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing or unreadable", result.stderr)

    def test_an_unrecorded_shared_file_fails(self):
        manifest = self.root / "shared-files.json"
        body = json.loads(manifest.read_text(encoding="utf-8"))
        body["shared"].pop(sync.SHARED_FILES[0])
        manifest.write_text(json.dumps(body), encoding="utf-8")
        result = run_sync(self.root, "--check-shared")
        self.assertEqual(result.returncode, 1)
        self.assertIn("absent from", result.stderr)

    def test_line_endings_do_not_count_as_drift(self):
        """A Windows checkout must not report every shared file as drift."""
        target = self.root / sync.SHARED_FILES[0]
        body = target.read_text(encoding="utf-8")
        target.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
        result = run_sync(self.root, "--check-shared")
        self.assertEqual(result.returncode, 0, result.stderr)


class LiveManifestTest(unittest.TestCase):
    """This repository's own manifest stays current with its files."""

    def test_the_committed_manifest_matches_this_tree(self):
        result = run_sync(REPO_ROOT, "--check-shared")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_shared_file_exists(self):
        for name in sync.SHARED_FILES:
            with self.subTest(name=name):
                self.assertTrue((REPO_ROOT / name).is_file())

    def test_the_manifest_records_exactly_the_shared_files(self):
        body = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(sorted(body["shared"]), sorted(sync.SHARED_FILES))


if __name__ == "__main__":
    unittest.main()
