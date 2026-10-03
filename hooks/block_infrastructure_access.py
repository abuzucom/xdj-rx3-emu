#!/usr/bin/env python3
"""Deny agent file access to infrastructure credentials and configuration."""
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import _gate_core as core
except ImportError as error:  # pragma: no cover
    print(f"shared gate core import failed ({error})", file=sys.stderr)
    sys.exit(2)


GATE = "block_infrastructure_access.py"
FILE_TOOLS = frozenset({"Edit", "MultiEdit", "NotebookEdit", "Read", "Write"})
SEARCH_TOOLS = frozenset({"Glob", "Grep"})
PATH_KEYS = ("file_path", "notebook_path", "path")
BROAD_PATTERNS = frozenset({"", "*", "**", "**/*"})


def _path(tool_input: dict) -> str:
    """Return the first supported path field."""
    for key in PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _content(tool_input: dict) -> str:
    """Return bounded text introduced by one write request."""
    values = []
    for key in ("content", "new_string", "new_source"):
        value = tool_input.get(key)
        if isinstance(value, str):
            values.append(value[:core.MAX_INFRASTRUCTURE_FILE_BYTES])
    edits = tool_input.get("edits", [])
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict) and isinstance(edit.get("new_string"), str):
                values.append(edit["new_string"][:core.MAX_INFRASTRUCTURE_FILE_BYTES])
    return " ".join(values)


def _file_access_denied(tool_input: dict, project_dir: str) -> bool:
    """Return whether one direct file operation reaches protected content."""
    path = _path(tool_input)
    return bool(path and core.is_protected_infrastructure_path(
        path, project_dir, _content(tool_input)))


def _search_denied(tool_name: str, tool_input: dict, project_dir: str) -> bool:
    """Return whether one search can traverse protected infrastructure files."""
    root = _path(tool_input) or project_dir
    include = tool_input.get("include", tool_input.get("glob", ""))
    pattern = tool_input.get("pattern", "")
    for candidate in (root, include, pattern):
        if isinstance(candidate, str) and core.is_protected_infrastructure_path(
                candidate, project_dir):
            return True
    if isinstance(include, str) and include not in BROAD_PATTERNS:
        resolved_root = root if os.path.isabs(root) else os.path.join(project_dir, root)
        return core.tree_contains_protected_infrastructure(resolved_root, include)
    if isinstance(pattern, str) and pattern not in BROAD_PATTERNS:
        if tool_name == "Glob":
            resolved_root = root if os.path.isabs(root) else os.path.join(project_dir, root)
            return core.tree_contains_protected_infrastructure(resolved_root, pattern)
    resolved_root = root if os.path.isabs(root) else os.path.join(project_dir, root)
    return core.tree_contains_protected_infrastructure(resolved_root)


def main() -> int:
    """Read one file-tool payload and deny protected infrastructure access."""
    payload = core.read_payload()
    if payload is None:
        return core.emit(GATE, "deny", "the hook payload cannot be inspected")
    tool_name = payload.get("tool_name")
    if tool_name not in FILE_TOOLS | SEARCH_TOOLS:
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return core.emit(GATE, "deny", "the file tool input cannot be inspected")
    project_dir = core.project_dir(payload)
    denied = (_file_access_denied(tool_input, project_dir)
              if tool_name in FILE_TOOLS else _search_denied(
                  tool_name, tool_input, project_dir))
    if not denied:
        return 0
    return core.emit(
        GATE,
        "deny",
        "infrastructure credentials and configuration are protected from agent access",
    )


if __name__ == "__main__":
    sys.exit(main())
