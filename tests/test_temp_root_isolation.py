#!/usr/bin/env python3
"""Tests that no test module leaks its temporary root into other modules.

A module that assigns `tempfile.tempdir` at import changes where every
later test in the same process creates fixtures. Fixtures created under the
repository root then discover this checkout's `.git` and `AGENTS.md`.
"""
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
TRUSTED_GH_TESTS = TESTS_ROOT / "test_trusted_gh.py"


def load_module(name: str, path: Path):
    """Import one test module under a distinct registered name.

    unittest finds setUpModule and tearDownModule through sys.modules, so
    an unregistered module would skip its module fixtures.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class TempRootIsolationTest(unittest.TestCase):
    """Importing or running test_trusted_gh.py keeps the caller's temp root."""

    def setUp(self):
        self.original = tempfile.tempdir
        self.addCleanup(setattr, tempfile, "tempdir", self.original)

    def test_import_leaves_temp_root_unchanged(self):
        load_module("trusted_gh_tests_import_probe", TRUSTED_GH_TESTS)
        self.assertEqual(tempfile.tempdir, self.original)

    def test_suite_run_restores_temp_root(self):
        name = "trusted_gh_tests_run_probe"
        self.addCleanup(sys.modules.pop, name, None)
        module = load_module(name, TRUSTED_GH_TESTS)
        seen = []
        # unittest groups module fixtures by the test class's module, so the
        # probe class claims the loaded module to run inside its fixtures.
        probe_class = type("TempRootProbe", (unittest.TestCase,), {
            "__module__": name,
            "test_probe": lambda _case: seen.append(tempfile.tempdir),
        })
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        suite.addTest(probe_class("test_probe"))
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(seen, [str(TESTS_ROOT.parent)])
        self.assertEqual(tempfile.tempdir, self.original)


if __name__ == "__main__":
    unittest.main()
