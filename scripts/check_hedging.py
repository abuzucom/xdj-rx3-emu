#!/usr/bin/env python3
"""Warn about configured prose-policy findings in supplied files.

The command remains advisory. Findings always return exit code 0.
Unreadable policy data or source files return exit code 1.
"""
import sys
from pathlib import Path

try:
    from scripts.prose_policy import find_violations, mask_markdown_code
except ModuleNotFoundError:
    from prose_policy import find_violations, mask_markdown_code


def strip_code(line: str) -> str:
    """Preserve the legacy helper through shared Markdown masking."""
    return mask_markdown_code(line)


def main() -> int:
    """Warn about prose policy findings in every supplied file."""
    paths = sys.argv[1:]
    if not paths:
        print("usage: check_hedging.py FILE [FILE ...]", file=sys.stderr)
        return 0
    try:
        for path in paths:
            text = Path(path).read_text(encoding="utf-8")
            for message in find_violations(text, path):
                print(message)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"error: prose check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
