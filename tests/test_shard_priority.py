#!/usr/bin/env python3
"""Tests for deterministic longest-first test shard scheduling."""
import threading
import time
import unittest
from unittest.mock import patch

from scripts import check_hook_coverage as shard_runner


class ShardPriorityTest(unittest.TestCase):
    """Measured slow shards start before ordinary lexical shards."""

    def test_slow_shards_run_first_and_unknown_shards_stay_sorted(self):
        shards = [
            ("test_zeta.py::Zeta", "test_zeta.Zeta"),
            ("test_immutable_compliance.py::ImmutableComplianceScannerTest",
             "test_immutable_compliance.ImmutableComplianceScannerTest"),
            ("test_alpha.py::Alpha", "test_alpha.Alpha"),
            ("test_enforce_git_identity.py::PreToolUseTest",
             "test_enforce_git_identity.PreToolUseTest"),
        ]

        ordered = shard_runner.prioritize_test_shards(shards)

        self.assertEqual(ordered, [
            ("test_enforce_git_identity.py::PreToolUseTest",
             "test_enforce_git_identity.PreToolUseTest"),
            ("test_immutable_compliance.py::ImmutableComplianceScannerTest",
             "test_immutable_compliance.ImmutableComplianceScannerTest"),
            ("test_alpha.py::Alpha", "test_alpha.Alpha"),
            ("test_zeta.py::Zeta", "test_zeta.Zeta"),
        ])

    def test_exclusive_shards_never_overlap(self):
        shards = [
            ("test_enforce_git_identity.py::PreToolUseTest", "identity"),
            ("test_enforce_git_identity.py::CheckerContractTest", "checker"),
            ("test_alpha.py::Alpha", "alpha"),
        ]
        lock = threading.Lock()
        active_heavy = 0
        maximum_heavy = 0
        observed_timeouts = {}

        def run_shard(_root, _environment, label, _name, timeout, _registry):
            nonlocal active_heavy, maximum_heavy
            heavy = label in shard_runner.RESOURCE_HEAVY_TEST_SHARDS
            with lock:
                observed_timeouts[label] = timeout
                if heavy:
                    active_heavy += 1
                    maximum_heavy = max(maximum_heavy, active_heavy)
            time.sleep(0.02)
            if heavy:
                with lock:
                    active_heavy -= 1
            return shard_runner.TestShardResult(label, 0.02, 0, "", "", False)

        exclusive_shards = frozenset(label for label, _name in shards[:2])
        with patch.object(
            shard_runner,
            "EXCLUSIVE_TEST_SHARDS",
            exclusive_shards,
        ), patch.object(shard_runner, "run_test_shard", side_effect=run_shard):
            problems = shard_runner.run_test_shards(
                ".", {}, workers=3, timeout=1, test_shards=shards)

        self.assertEqual(problems, [])
        self.assertEqual(maximum_heavy, 1)
        self.assertEqual(observed_timeouts["test_alpha.py::Alpha"], 1)
        self.assertEqual(
            observed_timeouts["test_enforce_git_identity.py::PreToolUseTest"],
            shard_runner.RESOURCE_SHARD_TIMEOUT_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
