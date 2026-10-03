#!/usr/bin/env python3
"""Test configured hook launcher validation."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_hook_launchers


class HookLauncherTest(unittest.TestCase):
    """The launcher probe fails closed across supported configurations."""

    def test_missing_configurations_are_filtered(self):
        root = Path("adopter")
        paths = [root / ".claude" / "settings.json"]
        with patch.object(check_hook_launchers.Path, "is_file", return_value=False):
            self.assertEqual(check_hook_launchers._config_paths(root), [])
        with patch.object(
                check_hook_launchers.Path,
                "is_file",
                side_effect=[True, False, False, False]):
            self.assertEqual(check_hook_launchers._config_paths(root), paths)

    def test_empty_configuration_reports_explicit_failure(self):
        stderr = io.StringIO()
        with patch.object(check_hook_launchers.Path, "cwd", return_value=Path("root")):
            with patch.object(check_hook_launchers, "_config_paths", return_value=[]):
                with contextlib.redirect_stderr(stderr):
                    result = check_hook_launchers.main()
        self.assertEqual(result, 1)
        self.assertIn("no configured hook launchers", stderr.getvalue())

    def test_missing_launcher_reports_executable(self):
        stderr = io.StringIO()
        config = Path("config.json")
        with patch.object(check_hook_launchers.Path, "cwd", return_value=Path("root")):
            with patch.object(check_hook_launchers, "_config_paths", return_value=[config]):
                with patch.object(Path, "read_text", return_value=json.dumps({
                    "command": "missing-python hooks/reinject_agents_policy.py",
                })):
                    with patch.object(check_hook_launchers.shutil, "which", return_value=None):
                        with contextlib.redirect_stderr(stderr):
                            result = check_hook_launchers.main()
        self.assertEqual(result, 1)
        self.assertIn("missing-python", stderr.getvalue())

    def test_probe_checks_every_launcher_and_gate(self):
        gates = [
            (Path("bash.py"), "Bash", 2, ()),
            (Path("codex.py"), "Bash", 0, ("--client", "claude")),
        ]
        destructive_result = type("Completed", (), {"returncode": 2})()
        transaction_result = type("Completed", (), {"returncode": 0})()
        with patch.object(check_hook_launchers.Path, "cwd", return_value=Path("root")):
            with patch.object(check_hook_launchers, "_config_paths", return_value=[Path("config")]):
                with patch.object(Path, "read_text", return_value=json.dumps({
                    "command": "python-a",
                    "commandWindows": "python-b",
                })):
                    with patch.object(check_hook_launchers, "_gate_paths", return_value=gates):
                        with patch.object(check_hook_launchers.shutil, "which", return_value="found"):
                            with patch.object(
                                check_hook_launchers.subprocess,
                                "run",
                                side_effect=[
                                    destructive_result,
                                    transaction_result,
                                    destructive_result,
                                    transaction_result,
                                ],
                            ) as run:
                                result = check_hook_launchers.main()
        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 4)


if __name__ == "__main__":
    unittest.main()
