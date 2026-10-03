#!/usr/bin/env python3
"""Warn about pull request title and description prose."""
import json
import os
import sys
from pathlib import Path

try:
    from scripts import check_commit_message
    from scripts.prose_policy import find_violations
except ImportError:
    import check_commit_message
    from prose_policy import find_violations

MAX_EVENT_BYTES = 1024 * 1024
MAX_TITLE_LENGTH = 4096
MAX_BODY_LENGTH = 1024 * 1024


def _load_event(path: Path) -> tuple[str, str, str]:
    """Load bounded pull request prose and author identity from event JSON."""
    if path.stat().st_size > MAX_EVENT_BYTES:
        raise ValueError("event payload exceeds the size limit")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("event payload must contain an object")
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ValueError("event payload lacks pull request data")
    title = pull_request.get("title")
    body = pull_request.get("body")
    author = pull_request.get("user")
    if not isinstance(title, str):
        raise ValueError("pull request title must contain text")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise ValueError("pull request body must contain text or null")
    if not isinstance(author, dict) or not isinstance(author.get("login"), str):
        raise ValueError("pull request author must contain a login")
    if len(title) > MAX_TITLE_LENGTH or len(body) > MAX_BODY_LENGTH:
        raise ValueError("pull request prose exceeds the size limit")
    return title, body, author["login"]


def check_event(path: str | Path) -> int:
    """Print sanitized advisory findings from one GitHub event file."""
    try:
        title, body, author = _load_event(Path(path))
        findings = []
        if author != "dependabot[bot]":
            findings.extend(
                check_commit_message.find_title_violations(title)
            )
        findings.extend(
            find_violations(
                check_commit_message.mask_type_prefix(title),
                "pull_request.title",
            )
        )
        findings.extend(find_violations(body, "pull_request.body"))
        for message in findings:
            print(message)
        if not findings:
            print("no pull-request prose findings found")
        return 0
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        print(f"error: pull request prose check failed: {error}", file=sys.stderr)
        return 1


def main() -> int:
    """Check the GitHub event path supplied by the runner environment."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        print("error: GITHUB_EVENT_PATH is unset", file=sys.stderr)
        return 1
    return check_event(event_path)


if __name__ == "__main__":
    sys.exit(main())
