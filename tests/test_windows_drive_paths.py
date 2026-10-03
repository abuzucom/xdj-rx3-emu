#!/usr/bin/env python3
"""Test drive-prefix handling for POSIX-only protected paths.

On Windows, os.path.abspath roots "/proc/1/environ" on the current drive.
The normalized form "/d:/proc/1/environ" must still match the /proc check.
These cases run the real helper on every platform.
"""
import importlib.util
import unittest
from pathlib import Path


CORE_PATH = Path(__file__).resolve().parent.parent / "hooks" / "_gate_core.py"


def load_core():
    """Load the gate core by path."""
    spec = importlib.util.spec_from_file_location("gate_core_drive_paths", CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StripWindowsDriveTest(unittest.TestCase):
    """A leading drive segment drops. Other paths stay unchanged."""

    def setUp(self):
        self.core = load_core()

    def test_drive_prefix_drops(self):
        self.assertEqual(self.core.strip_windows_drive("/d:/proc/1/environ"), "/proc/1/environ")
        self.assertEqual(self.core.strip_windows_drive("/c:/proc/self/environ"), "/proc/self/environ")

    def test_posix_path_stays(self):
        self.assertEqual(self.core.strip_windows_drive("/proc/1/environ"), "/proc/1/environ")
        self.assertEqual(self.core.strip_windows_drive("/src/environ/app.py"), "/src/environ/app.py")

    def test_non_drive_colon_stays(self):
        self.assertEqual(self.core.strip_windows_drive("/ab:/proc/1/environ"), "/ab:/proc/1/environ")
        self.assertEqual(self.core.strip_windows_drive("/1:/proc/1/environ"), "/1:/proc/1/environ")

    def test_bare_drive_stays(self):
        self.assertEqual(self.core.strip_windows_drive("/d:"), "/d:")


class ProcessEnvironmentTest(unittest.TestCase):
    """The /proc environ check still holds and still spares other paths."""

    def setUp(self):
        self.core = load_core()

    def test_proc_environ_is_protected(self):
        self.assertTrue(self.core.is_protected_infrastructure_path("/proc/1/environ"))

    def test_project_environ_directory_is_not_protected(self):
        self.assertFalse(
            self.core.is_protected_infrastructure_path("src/environ/app.py", cwd=str(CORE_PATH.parent))
        )


if __name__ == "__main__":
    unittest.main()
