#!/usr/bin/env python3
"""Deny ordinary work when the complete cross-client gate set is absent."""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import _gate_core as core
except ImportError as error:  # pragma: no cover
    print(f"shared gate core import failed ({error})", file=sys.stderr)
    sys.exit(2)

GATE = "enforce_gate_adoption.py"
CHECKER_PATH = os.path.join("scripts", "check_gate_adoption.py")
CHECKER_TIMEOUT_SECONDS = 10
QUESTION_TOOLS = frozenset({"AskUserQuestion", "ask_question"})
READ_ONLY_TOOLS = frozenset({
    "Read", "Glob", "Grep", "view_file", "list_dir", "find_by_name", "grep_search",
})
STAGING_WRITE_TOOLS = frozenset({
    "Edit", "MultiEdit", "NotebookEdit", "Write", "write_to_file", "replace_file_content",
})
STAGING_DIRECTORY = ".gate-staging"
RECOVERY_COMMANDS = frozenset({
    "python scripts/check_gate_adoption.py",
    "python3 scripts/check_gate_adoption.py",
    "python scripts/check_hook_launchers.py",
    "python3 scripts/check_hook_launchers.py",
    "python scripts/check_hook_coverage.py",
    "python3 scripts/check_hook_coverage.py",
    "python scripts/sync.py --check",
    "python3 scripts/sync.py --check",
})
RECOVERY_TRANSACTION = re.compile(
    r"(?:python|python3) scripts/complete_gate_adoption\.py --candidate "
    r"\.gate-staging/[A-Za-z0-9][A-Za-z0-9._-]*"
)


def _tool_call(payload: dict, client: str) -> tuple[object, object]:
    """Return one client-normalized tool name and argument mapping."""
    if client == "antigravity":
        call = payload.get("toolCall")
        if not isinstance(call, dict):
            return None, None
        return call.get("name"), call.get("args")
    return payload.get("tool_name"), payload.get("tool_input")


def _command(tool_input: object) -> str:
    """Return one exact shell command from supported tool input shapes."""
    if not isinstance(tool_input, dict):
        return ""
    for name in ("command", "CommandLine"):
        value = tool_input.get(name)
        if isinstance(value, str):
            return value
    return ""


def _deny(client: str, reason: str) -> int:
    """Emit the native deny response for one supported client."""
    message = f"blocked by hooks/{GATE}: {reason}"
    if client in ("gemini", "antigravity"):
        print(json.dumps({"decision": "deny", "reason": message}))
        return 0
    return core.emit(GATE, "deny", reason)


def _is_recovery_command(command: str) -> bool:
    """Return whether a command is fixed verification or staged recovery."""
    return command in RECOVERY_COMMANDS or bool(RECOVERY_TRANSACTION.fullmatch(command))


def _staging_write(tool_name: object, tool_input: object) -> bool:
    """Return whether one file write stays below the fixed staging directory."""
    if tool_name not in STAGING_WRITE_TOOLS or not isinstance(tool_input, dict):
        return False
    value = tool_input.get(
        "file_path", tool_input.get("notebook_path", tool_input.get("TargetFile")),
    )
    if not isinstance(value, str) or not value:
        return False
    root = core.policy_root()
    staging = core.resolved_under(root, STAGING_DIRECTORY)
    candidate = core.resolved_under(root, value)
    if staging is None or candidate is None:
        return False
    return candidate != staging and candidate.startswith(staging + os.sep)


def _check_complete_set() -> str:
    """Return a bounded failure reason or an empty string for a valid set."""
    root = core.policy_root()
    checker = core.resolved_under(root, CHECKER_PATH)
    if checker is None or not os.path.isfile(checker):
        return "the gate-adoption checker is missing"
    try:
        result = subprocess.run(
            [sys.executable, "-E", "-s", checker, "--root", root],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHECKER_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"the gate-adoption checker failed: {core.sanitize(error)}"
    if result.returncode == 0:
        return ""
    detail = result.stderr.strip() or "the gate-adoption checker rejected the set"
    return core.sanitize(detail)


def main() -> int:
    """Allow only fixed verification while the adoption transaction is invalid."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--client",
        choices=("claude", "codex", "gemini", "antigravity"),
        required=True,
    )
    options = parser.parse_args()
    payload = core.read_payload()
    if payload is None:
        return _deny(options.client, "the hook payload cannot be inspected")
    failure = _check_complete_set()
    if not failure:
        return 0
    tool_name, tool_input = _tool_call(payload, options.client)
    if (tool_name in QUESTION_TOOLS or tool_name in READ_ONLY_TOOLS
            or _staging_write(tool_name, tool_input)
            or _is_recovery_command(_command(tool_input))):
        return 0
    return _deny(options.client, failure)


if __name__ == "__main__":
    sys.exit(main())
