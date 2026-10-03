#!/usr/bin/env python3
"""Verify infrastructure configuration stays outside agent file access."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPOSITORY_ROOT / "hooks" / "block_infrastructure_access.py"
SETTINGS_PATHS = (
    REPOSITORY_ROOT / ".claude" / "settings.json",
    REPOSITORY_ROOT / "hooks" / "claude-code-settings.example.json",
)


def run_hook(payload: dict, project_dir: Path) -> subprocess.CompletedProcess:
    """Run one synthetic file-tool request through the infrastructure gate."""
    environment = dict(os.environ)
    environment["CLAUDE_PROJECT_DIR"] = str(project_dir)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def payload(tool_name: str, **tool_input) -> dict:
    """Return one Claude PreToolUse payload."""
    return {
        "hook_event_name": "PreToolUse",
        "permission_mode": "default",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


class InfrastructureAccessTest(unittest.TestCase):
    """Protected credentials and project manifests deny every file operation."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".aws").mkdir()
        (self.root / ".aws" / "credentials").write_text("synthetic", encoding="utf-8")
        (self.root / "main.tf").write_text("terraform {}\n", encoding="utf-8")
        (self.root / "deployment.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\n", encoding="utf-8")
        (self.root / "source.py").write_text("print('safe')\n", encoding="utf-8")

    def assert_denied(self, request: dict) -> None:
        """Assert one request receives a native denial."""
        result = run_hook(request, self.root)
        self.assertEqual(result.returncode, 2, result.stderr)
        output = json.loads(result.stdout)
        decision = output["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "deny")

    def test_explicit_protected_paths_deny(self):
        paths = (
            ".aws/credentials",
            "main.tf",
            "deployment.yaml",
            "image.pkr.hcl",
            "main.bicep",
            "Pulumi.yaml",
            "cdk.json",
            "samconfig.toml",
        )
        for file_path in paths:
            with self.subTest(file_path=file_path):
                self.assert_denied(payload("Read", file_path=file_path))

    def test_writes_to_new_terraform_files_deny(self):
        self.assert_denied(payload("Write", file_path="network.tf", content=""))

    def test_broad_search_over_protected_tree_denies(self):
        self.assert_denied(payload("Grep", path=str(self.root), pattern="resource"))
        self.assert_denied(payload("Glob", path=str(self.root), pattern="**/*"))
        self.assert_denied(payload(
            "Grep",
            path=str(self.root),
            pattern="kind",
            include="*.yaml",
        ))

    def test_narrow_safe_access_passes(self):
        result = run_hook(payload("Read", file_path="source.py"), self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run_hook(
            payload("Grep", path=str(self.root), pattern="safe", include="*.py"),
            self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class InfrastructureWiringTest(unittest.TestCase):
    """Claude settings register the infrastructure gate for file tools."""

    def test_live_and_example_settings_register_gate(self):
        for settings_path in SETTINGS_PATHS:
            with self.subTest(settings=settings_path.name):
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                registrations = settings["hooks"]["PreToolUse"]
                commands = [
                    argument
                    for registration in registrations
                    for hook in registration["hooks"]
                    for argument in hook.get("args", [])
                ]
                self.assertTrue(any(
                    "block_infrastructure_access.py" in command
                    for command in commands
                ))


if __name__ == "__main__":
    unittest.main()
