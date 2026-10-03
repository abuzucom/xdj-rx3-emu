#!/usr/bin/env python3
"""Tests for complete class-sharded test execution."""
import tempfile
import unittest
from pathlib import Path

from scripts import run_tests

REPO_ROOT = Path(__file__).resolve().parent.parent


class ParallelTestRunnerTest(unittest.TestCase):
    """Parallel execution retains every discovered test."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "tests").mkdir()

    def write_test(self, name: str, body: str) -> None:
        """Write one synthetic test module."""
        (self.root / "tests" / name).write_text(body, encoding="utf-8")

    def test_discovery_parity_covers_every_test_id(self):
        self.write_test(
            "test_alpha.py",
            "import unittest\n\n"
            "class Alpha(unittest.TestCase):\n"
            "    def test_one(self):\n        self.assertTrue(True)\n"
            "    def test_two(self):\n        self.assertTrue(True)\n\n"
            "class Beta(unittest.TestCase):\n"
            "    def test_three(self):\n        self.assertTrue(True)\n",
        )
        shards = run_tests.validated_test_shards(str(self.root))
        self.assertEqual(len(shards), 2)

    def test_parallel_runner_executes_every_class(self):
        first_marker = self.root / "first-ran"
        second_marker = self.root / "second-ran"
        self.write_test(
            "test_markers.py",
            "import pathlib\nimport unittest\n\n"
            "class First(unittest.TestCase):\n"
            f"    def test_marker(self):\n        pathlib.Path({str(first_marker)!r}).touch()\n\n"
            "class Second(unittest.TestCase):\n"
            f"    def test_marker(self):\n        pathlib.Path({str(second_marker)!r}).touch()\n",
        )
        result = run_tests.run_suite(str(self.root), workers=2, timeout=30)
        self.assertEqual(result, 0)
        self.assertTrue(first_marker.is_file())
        self.assertTrue(second_marker.is_file())

    def test_parallel_runner_propagates_failure(self):
        self.write_test(
            "test_failure.py",
            "import unittest\n\n"
            "class Failure(unittest.TestCase):\n"
            "    def test_failure(self):\n        self.fail('expected failure')\n",
        )
        result = run_tests.run_suite(str(self.root), workers=1, timeout=30)
        self.assertEqual(result, 1)

    def test_repository_commands_use_parallel_runner(self):
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        pre_commit = (REPO_ROOT / ".pre-commit-config.yaml").read_text(
            encoding="utf-8")
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "sync-check.yml"
        ).read_text(encoding="utf-8")
        command = "python scripts/run_tests.py"
        self.assertIn("$(PYTHON) scripts/run_tests.py", makefile)
        self.assertIn(f"entry: {command}", pre_commit)
        self.assertEqual(workflow.count(f"run: {command}"), 2)
        self.assertEqual(
            workflow.count("run: python scripts/check_hook_coverage.py"),
            1,
        )
        self.assertIn("runs-on: macos-latest", workflow)
        self.assertNotIn("unittest discover", workflow)


if __name__ == "__main__":
    unittest.main()
