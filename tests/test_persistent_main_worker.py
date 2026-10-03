#!/usr/bin/env python3
"""Tests for isolated repeated CLI entrypoint execution."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.persistent_main_worker import MainWorker


MODULE_SOURCE = """\
import json
import os
import sys
import time

invocations = 0

def main():
    global invocations
    invocations += 1
    payload = json.load(sys.stdin)
    time.sleep(payload.get("sleep", 0))
    print(json.dumps({
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "invocations": invocations,
        "payload": payload,
        "value": os.environ.get("WORKER_TEST_VALUE", ""),
    }))
    print("worker diagnostic", file=sys.stderr)
    return payload["code"]
"""


class MainWorkerTest(unittest.TestCase):
    """Each request gets isolated process inputs while module state persists."""

    def test_repeated_invocations_restore_request_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module_path = root / "sample_cli.py"
            first_cwd = root / "first"
            second_cwd = root / "second"
            first_cwd.mkdir()
            second_cwd.mkdir()
            module_path.write_text(MODULE_SOURCE, encoding="utf-8")
            worker = MainWorker(module_path)
            self.addCleanup(worker.close)

            first = worker.invoke(
                ["alpha"], {"code": 3}, first_cwd,
                {**os.environ, "WORKER_TEST_VALUE": "first"},
            )
            second = worker.invoke(
                ["beta"], {"code": 0}, second_cwd,
                {**os.environ, "WORKER_TEST_VALUE": "second"},
            )

        first_output = json.loads(first.stdout)
        second_output = json.loads(second.stdout)
        self.assertEqual(first.returncode, 3)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(first_output["argv"], ["alpha"])
        self.assertEqual(second_output["argv"], ["beta"])
        self.assertEqual(Path(first_output["cwd"]).resolve(), first_cwd.resolve())
        self.assertEqual(Path(second_output["cwd"]).resolve(), second_cwd.resolve())
        self.assertEqual(first_output["invocations"], 1)
        self.assertEqual(second_output["invocations"], 2)
        self.assertEqual(first_output["payload"], {"code": 3})
        self.assertEqual(second_output["value"], "second")
        self.assertEqual(first.stderr, "worker diagnostic\n")

    def test_request_timeout_terminates_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository_root = Path(temporary)
            module_path = repository_root / "slow_cli.py"
            module_path.write_text(MODULE_SOURCE, encoding="utf-8")
            worker = MainWorker(module_path, request_timeout=0.05)
            self.addCleanup(worker.close)
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                worker.invoke(
                    [],
                    {"code": 0, "sleep": 1},
                    repository_root,
                    dict(os.environ),
                )


if __name__ == "__main__":
    unittest.main()
