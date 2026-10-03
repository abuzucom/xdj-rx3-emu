#!/usr/bin/env python3
"""Verify configured hook launchers and one fail-closed gate invocation."""
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

GATE_PROBES = (
    ("block_destructive_bash.py", "Bash", 2, ()),
    ("block_destructive_powershell.py", "PowerShell", 2, ()),
    ("block_destructive_cmd.py", "Cmd", 2, ()),
    ("enforce_gate_adoption.py", "Bash", 0, ("--client", "claude")),
)


def _deny_payload(tool_name: str) -> str:
    """Return a destructive probe payload for one gate client."""
    return json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "permission_mode": "default",
        "tool_input": {"command": "rm -rf /"},
    })


def _commands(value: object) -> list[str]:
    """Collect command strings from nested client configuration."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"command", "commandWindows"} and isinstance(child, str):
                found.append(child)
            found.extend(_commands(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_commands(child))
    return found


def _launcher(command: str) -> str:
    """Return the executable token from one configured command."""
    tokens = shlex.split(command, posix=sys.platform != "win32")
    if not tokens:
        raise ValueError("empty hook command")
    return tokens[0]


def _config_paths(root: Path) -> list[Path]:
    """Return client configuration files that define hooks."""
    candidates = [
        root / ".claude" / "settings.json",
        root / ".codex" / "hooks.json",
        root / ".gemini" / "settings.json",
        root / ".agents" / "hooks.json",
    ]
    return [path for path in candidates if path.is_file()]


def _gate_paths(root: Path) -> list[tuple[Path, str, int, tuple[str, ...]]]:
    """Return installed gate scripts and their matching client names."""
    return [
        (root / "hooks" / filename, tool_name, expected_returncode, arguments)
        for filename, tool_name, expected_returncode, arguments in GATE_PROBES
        if (root / "hooks" / filename).is_file()
    ]


def _diagnostic(value: str) -> str:
    """Return bounded printable launcher diagnostic text."""
    normalized = value.strip().replace("\r", " ").replace("\n", " ")
    return normalized.encode("ascii", "backslashreplace").decode("ascii")[:160]


def main() -> int:
    """Check launcher resolution and require a blocking gate response."""
    root = Path.cwd()
    commands = []
    for path in _config_paths(root):
        document = json.loads(path.read_text(encoding="utf-8"))
        commands.extend(_commands(document))
    launchers = sorted({_launcher(command) for command in commands})
    if not launchers:
        print("no configured hook launchers", file=sys.stderr)
        return 1
    missing = [name for name in launchers if shutil.which(name) is None]
    if missing:
        print(f"missing hook launchers: {', '.join(missing)}", file=sys.stderr)
        return 1
    gates = _gate_paths(root)
    if not gates:
        print("no installed destructive gate scripts", file=sys.stderr)
        return 1
    for launcher in launchers:
        for gate, tool_name, expected_returncode, arguments in gates:
            result = subprocess.run(
                [launcher, str(gate), *arguments], input=_deny_payload(tool_name), text=True,
                encoding="utf-8", errors="replace",
                capture_output=True, cwd=root, check=False,
            )
            if result.returncode != expected_returncode:
                detail = _diagnostic(result.stderr) or "no launcher diagnostic"
                print(
                    f"configured launcher {launcher} returned {result.returncode} "
                    f"instead of {expected_returncode} for {gate.name}: {detail}",
                    file=sys.stderr,
                )
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
