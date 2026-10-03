#!/usr/bin/env python3
"""Require tests alongside executable changes in a pull request range."""
import argparse
import re
import subprocess
import sys


REVISION = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")
EXECUTABLE = re.compile(r"(?:^scripts/.*\.py$|^hooks/.*\.py$|^\.github/workflows/.*\.yml$)")


def _revision(value: str) -> str:
    """Validate one immutable Git revision argument."""
    if not REVISION.fullmatch(value):
        raise ValueError("revision must contain 7 to 40 hexadecimal characters")
    return value


def changed_files(base: str, head: str) -> list[str]:
    """Return changed paths from a validated revision range."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}", "--"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    if result.returncode:
        raise OSError("cannot inspect the pull request range")
    return [line for line in result.stdout.splitlines() if line]


def staged_files() -> list[str]:
    """Return staged paths for the local pre-commit check."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    if result.returncode:
        raise OSError("cannot inspect staged changes")
    return [line for line in result.stdout.splitlines() if line]


def violations(paths: list[str]) -> list[str]:
    """Return executable changes without a changed behavioral test."""
    executable = [path for path in paths if EXECUTABLE.fullmatch(path)]
    tests_changed = any(path.startswith("tests/") and path.endswith(".py") for path in paths)
    if executable and not tests_changed:
        return ["executable changes require a behavioral test change"]
    return []


def main(argv: list[str]) -> int:
    """Check a pull request range or the working tree."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--head")
    args = parser.parse_args(argv)
    try:
        if bool(args.base) != bool(args.head):
            raise ValueError("base and head must be supplied together")
        paths = (changed_files(_revision(args.base), _revision(args.head))
                 if args.base else staged_files())
        findings = violations(paths)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for finding in findings:
        print(f"error: {finding}", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
