#!/usr/bin/env python3
"""Test the complete-gate runtime enforcement hook."""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPOSITORY_ROOT / "hooks" / "enforce_gate_adoption.py"
PROCESS_RUNNER = (
    "import os\n"
    "import sys\n"
    "sys.path.insert(0, os.environ['TEST_HOOKS_DIR'])\n"
    "import enforce_gate_adoption as hook\n"
    "hook.core.policy_root = lambda: os.environ['TEST_POLICY_ROOT']\n"
    "if os.environ.get('TEST_CHECKER_ERROR'):\n"
    "    def fail_checker(*args, **kwargs):\n"
    "        raise OSError('test checker error')\n"
    "    hook.subprocess.run = fail_checker\n"
    "sys.exit(hook.main())\n"
)


def load_hook():
    """Load the hook module from its checked-in path."""
    spec = importlib.util.spec_from_file_location("enforce_gate_adoption", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GateAdoptionHookTest(unittest.TestCase):
    """The runtime gate permits only bounded recovery after a failure."""

    def setUp(self) -> None:
        self.hook = load_hook()

    def run_hook(self, payload: dict, failure: str) -> tuple[int, str]:
        """Run one Claude payload and return the result plus stderr."""
        stderr = io.StringIO()
        with patch.object(sys, "argv", [str(HOOK_PATH), "--client", "claude"]):
            with patch.object(self.hook.core, "read_payload", return_value=payload):
                with patch.object(self.hook, "_check_complete_set", return_value=failure):
                    with contextlib.redirect_stderr(stderr):
                        result = self.hook.main()
        return result, stderr.getvalue()

    def run_process(
        self,
        payload: object,
        root: Path,
        client: str,
        checker_error: bool = False,
    ) -> subprocess.CompletedProcess:
        """Run the checked-in hook in a traced child interpreter."""
        environment = dict(os.environ)
        environment["TEST_HOOKS_DIR"] = str(HOOK_PATH.parent)
        environment["TEST_POLICY_ROOT"] = str(root)
        if checker_error:
            environment["TEST_CHECKER_ERROR"] = "1"
        return subprocess.run(
            [sys.executable, "-c", PROCESS_RUNNER, "--client", client],
            cwd=REPOSITORY_ROOT,
            env=environment,
            input=json.dumps(payload),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

    def test_process_allows_complete_set(self) -> None:
        result = self.run_process(
            {"tool_name": "Bash", "tool_input": {"command": "git status"}},
            REPOSITORY_ROOT,
            "claude",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_process_denies_incomplete_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checker = root / "scripts" / "check_gate_adoption.py"
            checker.parent.mkdir()
            checker.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
            result = self.run_process(
                {"tool_name": "Bash", "tool_input": None}, root, "claude"
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("gate-adoption checker rejected", result.stderr)

    def test_process_denies_missing_checker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_process(
                {"tool_name": "Bash", "tool_input": {"CommandLine": "git status"}},
                Path(temporary),
                "claude",
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("checker is missing", result.stderr)

    def test_process_denies_empty_tool_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checker = root / "scripts" / "check_gate_adoption.py"
            checker.parent.mkdir()
            checker.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
            result = self.run_process(
                {"tool_name": "Bash", "tool_input": {}}, root, "claude"
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("checker rejected", result.stderr)

    def test_process_denies_checker_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checker = root / "scripts" / "check_gate_adoption.py"
            checker.parent.mkdir()
            checker.write_text("raise SystemExit(0)\n", encoding="utf-8")
            result = self.run_process(
                {"tool_name": "Bash", "tool_input": {"command": "git status"}},
                root,
                "claude",
                checker_error=True,
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("test checker error", result.stderr)

    def test_process_denies_antigravity_incomplete_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checker = root / "scripts" / "check_gate_adoption.py"
            checker.parent.mkdir()
            checker.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
            result = self.run_process(
                {"toolCall": {"name": "Bash", "args": {"command": "git status"}}},
                root,
                "antigravity",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"decision": "deny"', result.stdout)

    def test_process_allows_antigravity_staging_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checker = root / "scripts" / "check_gate_adoption.py"
            checker.parent.mkdir()
            checker.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
            result = self.run_process(
                {
                    "toolCall": {
                        "name": "write_to_file",
                        "args": {"TargetFile": ".gate-staging/release/hooks/example.py"},
                    },
                },
                root,
                "antigravity",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_process_denies_antigravity_write_outside_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checker = root / "scripts" / "check_gate_adoption.py"
            checker.parent.mkdir()
            checker.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
            result = self.run_process(
                {
                    "toolCall": {
                        "name": "replace_file_content",
                        "args": {"TargetFile": "hooks/example.py"},
                    },
                },
                root,
                "antigravity",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"decision": "deny"', result.stdout)

    def test_process_denies_antigravity_write_without_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checker = root / "scripts" / "check_gate_adoption.py"
            checker.parent.mkdir()
            checker.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
            result = self.run_process(
                {"toolCall": {"name": "write_to_file", "args": {}}},
                root,
                "antigravity",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"decision": "deny"', result.stdout)

    def test_process_denies_antigravity_write_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checker = root / "scripts" / "check_gate_adoption.py"
            checker.parent.mkdir()
            checker.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
            result = self.run_process(
                {
                    "toolCall": {
                        "name": "write_to_file",
                        "args": {"TargetFile": "../outside.py"},
                    },
                },
                root,
                "antigravity",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"decision": "deny"', result.stdout)

    def test_process_denies_malformed_antigravity_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checker = root / "scripts" / "check_gate_adoption.py"
            checker.parent.mkdir()
            checker.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
            result = self.run_process({"toolCall": None}, root, "antigravity")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"decision": "deny"', result.stdout)

    def test_process_denies_missing_payload(self) -> None:
        result = self.run_process(None, REPOSITORY_ROOT, "claude")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("payload cannot be inspected", result.stderr)

    def test_complete_set_allows_ordinary_tool(self) -> None:
        result, output = self.run_hook(
            {"tool_name": "Bash", "tool_input": {"command": "git status"}},
            "",
        )
        self.assertEqual(result, 0, output)

    def test_incomplete_set_allows_fixed_verifier(self) -> None:
        result, output = self.run_hook(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "python scripts/check_gate_adoption.py"},
            },
            "codex registration is absent",
        )
        self.assertEqual(result, 0, output)

    def test_incomplete_set_allows_staged_transaction(self) -> None:
        result, output = self.run_hook(
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python scripts/complete_gate_adoption.py --candidate "
                        ".gate-staging/release-2.3.0"
                    ),
                },
            },
            "codex registration is absent",
        )
        self.assertEqual(result, 0, output)

    def test_incomplete_set_allows_read_only_discovery(self) -> None:
        result, output = self.run_hook(
            {"tool_name": "Read", "tool_input": {"file_path": "hooks/example.py"}},
            "codex registration is absent",
        )
        self.assertEqual(result, 0, output)

    def test_incomplete_set_allows_only_staging_writes(self) -> None:
        allowed, output = self.run_hook(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": ".gate-staging/release/hooks/example.py",
                    "content": "content",
                },
            },
            "codex registration is absent",
        )
        blocked, blocked_output = self.run_hook(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "hooks/example.py", "content": "content"},
            },
            "codex registration is absent",
        )
        self.assertEqual(allowed, 0, output)
        self.assertEqual(blocked, 2)
        self.assertIn("codex registration is absent", blocked_output)

    def test_incomplete_set_rejects_staging_path_escape(self) -> None:
        result, output = self.run_hook(
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": ".gate-staging/release/../../hooks/example.py",
                    "content": "content",
                },
            },
            "codex registration is absent",
        )
        self.assertEqual(result, 2)
        self.assertIn("codex registration is absent", output)

    def test_antigravity_allows_native_recovery_tools(self) -> None:
        read_payload = {
            "toolCall": {"name": "view_file", "args": {"AbsolutePath": "README.md"}},
        }
        write_payload = {
            "toolCall": {
                "name": "write_to_file",
                "args": {"TargetFile": ".gate-staging/release/hooks/example.py"},
            },
        }
        for payload in (read_payload, write_payload):
            with self.subTest(payload=payload), \
                    patch.object(sys, "argv", [str(HOOK_PATH), "--client", "antigravity"]), \
                    patch.object(self.hook.core, "read_payload", return_value=payload), \
                    patch.object(self.hook, "_check_complete_set", return_value="gate is absent"):
                self.assertEqual(self.hook.main(), 0)

    def test_antigravity_rejects_native_write_outside_staging(self) -> None:
        payload = {
            "toolCall": {
                "name": "replace_file_content",
                "args": {"TargetFile": "hooks/example.py"},
            },
        }
        output = io.StringIO()
        with patch.object(sys, "argv", [str(HOOK_PATH), "--client", "antigravity"]), \
                patch.object(self.hook.core, "read_payload", return_value=payload), \
                patch.object(self.hook, "_check_complete_set", return_value="gate is absent"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(self.hook.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["decision"], "deny")

    def test_incomplete_set_denies_unbounded_transaction(self) -> None:
        result, output = self.run_hook(
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "python scripts/complete_gate_adoption.py --candidate C:/tmp",
                },
            },
            "codex registration is absent",
        )
        self.assertEqual(result, 2)
        self.assertIn("codex registration is absent", output)

    def test_incomplete_set_denies_ordinary_tool(self) -> None:
        result, output = self.run_hook(
            {"tool_name": "Bash", "tool_input": {"command": "git status"}},
            "codex registration is absent",
        )
        self.assertEqual(result, 2)
        self.assertIn("codex registration is absent", output)


if __name__ == "__main__":
    unittest.main()
