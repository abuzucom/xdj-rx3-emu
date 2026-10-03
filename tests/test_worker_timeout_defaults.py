#!/usr/bin/env python3
"""Verify persistent workers use a CI-safe bounded request deadline."""
import tempfile
import unittest
from pathlib import Path

from tests.json_line_worker import DEFAULT_REQUEST_TIMEOUT_SECONDS
from tests.persistent_main_worker import MainWorker


MINIMUM_CI_REQUEST_TIMEOUT_SECONDS = 15.0
MODULE_SOURCE = """\
def main():
    return 0
"""


class WorkerTimeoutDefaultTest(unittest.TestCase):
    """The default deadline covers observed CI process startup time."""

    def test_main_worker_uses_ci_safe_bounded_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            module_path = Path(temporary) / "sample_cli.py"
            module_path.write_text(MODULE_SOURCE, encoding="utf-8")
            worker = MainWorker(module_path)
            self.addCleanup(worker.close)

        self.assertEqual(
            worker.transport.request_timeout,
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )
        self.assertGreaterEqual(
            worker.transport.request_timeout,
            MINIMUM_CI_REQUEST_TIMEOUT_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
