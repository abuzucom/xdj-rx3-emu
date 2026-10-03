#!/usr/bin/env python3
"""Enforce AGENTS.md dash and ASCII rules through the portable checker."""
import sys
from pathlib import Path

try:
    from scripts.check_ascii import (
        DASH_SUBSTITUTE,
        EM_EN_DASH,
        LIST_MARKER,
        MAX_ASCII_CODEPOINT,
        TABLE_SEPARATOR,
        find_violations as find_path_violations,
        strip_code,
        strip_marker,
    )
except ModuleNotFoundError:
    from check_ascii import (
        DASH_SUBSTITUTE,
        EM_EN_DASH,
        LIST_MARKER,
        MAX_ASCII_CODEPOINT,
        TABLE_SEPARATOR,
        find_violations as find_path_violations,
        strip_code,
        strip_marker,
    )

SOURCE = "AGENTS.md"


def find_violations(text: str) -> list[str]:
    """Return portable checker findings under the canonical source path."""
    return find_path_violations(text, SOURCE)


def lint() -> int:
    """Lint the canonical source document."""
    root = Path(__file__).resolve().parent.parent
    source = root / SOURCE
    if not source.is_file():
        print(f"error: {SOURCE} not found at {root}", file=sys.stderr)
        return 1
    violations = find_violations(source.read_text(encoding="utf-8"))
    if violations:
        for message in violations:
            print(message, file=sys.stderr)
        print("fix: rewrite the prose as separate sentences", file=sys.stderr)
        return 1
    print("style clean")
    return 0


if __name__ == "__main__":
    sys.exit(lint())
