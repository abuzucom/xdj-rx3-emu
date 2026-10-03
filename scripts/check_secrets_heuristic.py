#!/usr/bin/env python3
"""Heuristically flag likely-committed secrets (Rule 8).

A portable, path-generic checker: copy this file into any repo and point
it at that repo's own source globs and CI. Matches well-anchored,
structured secret-token prefixes (AWS, GitHub, Slack, Google, private keys)
and blocks .env files except the exact basename .env.example. This is a
heuristic, not entropy-based scanning: it misses
secrets with no recognizable prefix. Propose gitleaks or detect-secrets
(Rule 9) for that. Blocking: exits 1 on any match.
"""
import re
import sys
from pathlib import Path

TOKEN_PATTERNS = [
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[opsur]_[0-9A-Za-z]{36,}\b"),
    re.compile(r"\bgithub_pat_[0-9A-Za-z_]{22,}\b"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
]
MAX_VIOLATIONS = 1_000


def find_violations(text: str, path: str) -> list[str]:
    """Return one message per likely secret token or blocked filename."""
    violations = []
    basename = Path(path).name
    blocked_variant = basename.startswith(".env.") and basename != ".env.example"
    if basename == ".env" or blocked_variant:
        violations.append(f"{path}: file must not be committed (Rule 8)")
    for number, line in enumerate(text.splitlines(), start=1):
        for pattern in TOKEN_PATTERNS:
            if pattern.search(line):
                violations.append(
                    f"{path}:{number}: likely secret token committed (Rule 8)"
                )
                if len(violations) >= MAX_VIOLATIONS:
                    return violations
                break
    return violations


def main() -> int:
    """Check each given file. Return 0 when all are clean, 1 otherwise."""
    paths = sys.argv[1:]
    if not paths:
        print("usage: check_secrets_heuristic.py FILE [FILE ...]", file=sys.stderr)
        return 1

    all_violations = []
    for path in paths:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        all_violations.extend(find_violations(text, path))

    if all_violations:
        for message in all_violations:
            print(message, file=sys.stderr)
        print(
            "fix: remove and rotate the secret; use env vars or a secret manager",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
