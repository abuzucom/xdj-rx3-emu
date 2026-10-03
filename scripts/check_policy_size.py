#!/usr/bin/env python3
"""Validate the canonical policy size and required rules."""
import argparse
import sys
from pathlib import Path


MAX_POLICY_BYTES = 32 * 1024
REQUIRED_TEXT = (
    "## Non-negotiable",
    "## Critical rules",
    "## Code quality",
    "## Style",
    "Change policy safely",
    "Version every change",
    "xAI",
    "Grok",
)


def find_violations(path: Path) -> list[str]:
    violations = []
    try:
        data = path.read_bytes()
    except OSError as error:
        return [f"{path}: cannot read policy: {error}"]
    if len(data) > MAX_POLICY_BYTES:
        violations.append(
            f"{path}: {len(data)} bytes exceeds {MAX_POLICY_BYTES} bytes"
        )
    if b"\r\n" in data:
        violations.append(f"{path}: policy must use LF line endings")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return violations + [f"{path}: policy is not ASCII"]
    violations.extend(
        f"{path}: missing required text: {required}"
        for required in REQUIRED_TEXT
        if required not in text
    )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", type=Path, default=Path("AGENTS.md"))
    args = parser.parse_args()
    violations = find_violations(args.path)
    for violation in violations:
        print(violation, file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
