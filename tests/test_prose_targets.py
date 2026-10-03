#!/usr/bin/env python3
"""Check that every prose file the agent prose gate targets exists here."""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE_PATH = ROOT / "scripts" / "check_agent_prose_gate.py"


def load_gate():
    """Load scripts/check_agent_prose_gate.py as a module."""
    spec = importlib.util.spec_from_file_location("check_agent_prose_gate", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProseTargetsExistTest(unittest.TestCase):
    """A missing target would make the prose checks fail on every pull request."""

    def test_every_target_file_exists(self):
        gate = load_gate()
        missing = [name for name in gate.TARGET_FILES if not (ROOT / name).is_file()]
        self.assertEqual(missing, [])

    def test_template_drift_record_is_a_target(self):
        gate = load_gate()
        self.assertIn("docs/template-drift.md", gate.TARGET_FILES)


if __name__ == "__main__":
    unittest.main()
