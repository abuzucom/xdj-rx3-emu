#!/usr/bin/env python3
"""Exercise observable and bounded hook coverage shard execution."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_hook_coverage as gate


class TestShardRunnerTest(unittest.TestCase):
    """Coverage workers execute real test classes and report progress."""

    def make_root(self, test_files: dict[str, str]) -> Path:
        """Create a temporary test root and return its path."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        tests = root / "tests"
        tests.mkdir()
        for name, body in test_files.items():
            (tests / name).write_text(body, encoding="utf-8")
        return root

    def test_workers_split_classes_from_the_same_file(self):
        root = self.make_root({
            "test_alpha.py": (
                "import unittest\n\nclass Alpha(unittest.TestCase):\n"
                "    def test_ok(self):\n        self.assertTrue(True)\n\n"
                "class Beta(unittest.TestCase):\n"
                "    def test_ok(self):\n        self.assertEqual(1, 1)\n"),
        })
        diagnostics = io.StringIO()
        with contextlib.redirect_stderr(diagnostics):
            problems = gate.run_test_shards(
                str(root), dict(os.environ), workers=2, timeout=30)

        self.assertEqual(problems, [])
        output = diagnostics.getvalue()
        self.assertIn("starting 2 test shards with 2 workers", output)
        self.assertIn("passed test_alpha.py::Alpha", output)
        self.assertIn("passed test_alpha.py::Beta", output)

    def test_worker_timeout_names_the_test_file(self):
        root = self.make_root({
            "test_slow.py": (
                "import time\nimport unittest\n\nclass Slow(unittest.TestCase):\n"
                "    def test_slow(self):\n        time.sleep(2)\n"),
        })
        diagnostics = io.StringIO()
        with contextlib.redirect_stderr(diagnostics):
            problems = gate.run_test_shards(
                str(root), dict(os.environ), workers=1, timeout=0.1)

        self.assertEqual(len(problems), 1)
        self.assertIn("test_slow.py", problems[0])
        self.assertIn("timed out", problems[0])

    def test_first_failure_stops_future_shard_submission(self):
        root = self.make_root({
            "test_early.py": (
                "import unittest\n\nclass Early(unittest.TestCase):\n"
                "    def test_failure(self):\n        self.fail('stop')\n"
            ),
            "test_late.py": (
                "import pathlib\nimport unittest\n\n"
                "class Late(unittest.TestCase):\n"
                "    def test_marker(self):\n"
                "        pathlib.Path('late-ran').write_text('ran')\n"
            ),
        })
        diagnostics = io.StringIO()
        with contextlib.redirect_stderr(diagnostics):
            problems = gate.run_test_shards(
                str(root),
                dict(os.environ),
                workers=1,
                timeout=30,
            )

        self.assertEqual(len(problems), 1)
        self.assertFalse((root / "late-ran").exists())

    def test_invalid_worker_count_fails_before_executor_creation(self):
        root = self.make_root({
            "test_worker_count.py": (
                "import unittest\n\nclass WorkerCount(unittest.TestCase):\n"
                "    def test_ok(self):\n        self.assertTrue(True)\n"
            ),
        })
        with self.assertRaisesRegex(ValueError, "workers must be positive"):
            gate.run_test_shards(
                str(root),
                dict(os.environ),
                workers=0,
                timeout=30,
            )


if __name__ == "__main__":
    unittest.main()
