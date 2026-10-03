#!/usr/bin/env python3
"""Block autolinked pull request references to an external repository."""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    from scripts import check_commit_message, read_git_state
    from scripts.prose_policy import mask_markdown_code
except ImportError:
    import check_commit_message
    import read_git_state
    from prose_policy import mask_markdown_code

MAX_EVENT_BYTES = 1024 * 1024
MAX_TITLE_LENGTH = 4096
MAX_BODY_LENGTH = 1024 * 1024
DEPENDABOT_LOGIN = "dependabot[bot]"
SHORT_SHA_LENGTH = 12

# A remote URL splits into host and path. The host is validated in code
# rather than in the pattern, which keeps the pattern free of the nested
# quantifier a subdomain alternation would need.
REMOTE_PARTS = re.compile(
    r"^(?:[A-Za-z][\w+.-]*://)?(?:[^/@]*@)?([^/:]+)(?::\d+)?[:/](.+)$"
)

# GitHub posts a cross-reference event on the target of an autolinked
# reference. A code span suppresses the autolink, so masked text never
# reaches these patterns. Both patterns stay linear and avoid nested
# quantifiers.
SHORT_REFERENCE = re.compile(
    r"(?<![\w./-])([A-Za-z0-9][\w.-]*)/([\w.-]+?)"
    r"(?:#\d+(?![\w-])|@[0-9a-fA-F]{7,40}(?![0-9A-Za-z-]))"
)
RESOURCE_URL = re.compile(
    r"https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)"
    r"/(?:pull|issues|commit)/\w+",
    re.IGNORECASE,
)


def _load_event(path: Path) -> tuple[str, str, str, str]:
    """Load bounded pull request prose, author, and repository owner."""
    if path.stat().st_size > MAX_EVENT_BYTES:
        raise ValueError("event payload exceeds the size limit")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("event payload must contain an object")
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ValueError("event payload lacks pull request data")
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("event payload lacks repository data")
    owner = repository.get("owner")
    if not isinstance(owner, dict) or not isinstance(owner.get("login"), str):
        raise ValueError("event payload lacks a repository owner login")
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
    return title, body, author["login"], owner["login"]


def _sanitize(value: str) -> str:
    """Return untrusted text as one bounded single-line fragment."""
    collapsed = " ".join(value.split())
    return collapsed[:120]


def find_external_references(text: str, owner: str, field: str) -> list[str]:
    """Return findings for autolinked references outside the given owner."""
    masked = mask_markdown_code(text)
    findings = []
    for pattern in (SHORT_REFERENCE, RESOURCE_URL):
        for match in pattern.finditer(masked):
            if match.group(1).lower() == owner.lower():
                continue
            reference = _sanitize(match.group(0))
            findings.append(
                f"{field}: autolinked external reference '{reference}' "
                f"notifies another owner; wrap it in a code span"
            )
    return findings


def is_github_host(host: str) -> bool:
    """Return True when `host` is github.com itself or a subdomain of it.

    A suffix test alone would accept `attacker-github.com`, so the domain
    boundary is explicit.
    """
    name = host.lower().partition(":")[0]
    return name == "github.com" or name.endswith(".github.com")


def parse_remote_owner(url: str) -> str:
    """Return the GitHub owner named by a remote URL, or an empty string."""
    match = REMOTE_PARTS.match(url.strip())
    if not match:
        return ""
    if not is_github_host(match.group(1)):
        return ""
    segments = [segment for segment in match.group(2).split("/") if segment]
    return segments[0] if len(segments) == 2 else ""


def resolve_owner(explicit: str, environment: dict, repo) -> str:
    """Return the current repository owner from the first available source."""
    if explicit:
        return explicit
    from_environment = environment.get("GITHUB_REPOSITORY_OWNER", "")
    if from_environment:
        return from_environment
    remote = read_git_state.read_state("remote", Path(repo)).get("origin")
    owner = parse_remote_owner(remote) if remote else ""
    if not owner:
        raise ValueError(
            "no repository owner from --owner, GITHUB_REPOSITORY_OWNER, or "
            "the origin remote"
        )
    return owner


def check_messages(messages: list, owner: str) -> int:
    """Return 1 when a commit message cross-references an external owner."""
    findings = []
    for sha, subject, body in messages:
        label = sha[:SHORT_SHA_LENGTH]
        findings.extend(
            find_external_references(subject, owner, f"commit {label}.subject")
        )
        findings.extend(
            find_external_references(body, owner, f"commit {label}.body")
        )
    for message in findings:
        print(message)
    if findings:
        return 1
    print("no external-reference findings found")
    return 0


def check_range(base: str, head: str, owner: str, repo=None) -> int:
    """Return 1 when the base..head range carries an external reference."""
    repository = repo or os.getcwd()
    try:
        messages = check_commit_message.load_commit_messages(
            base, head, repository)
    except (OSError, subprocess.CalledProcessError, UnicodeError,
            ValueError) as error:
        print(f"error: external reference check failed: {error}",
              file=sys.stderr)
        return 1
    return check_messages(messages, owner)


def check_unpushed(owner: str, repo=None) -> int:
    """Return 1 when an unpushed commit carries an external reference."""
    repository = repo or os.getcwd()
    try:
        messages = check_commit_message.load_unpushed_messages(repository)
    except (OSError, subprocess.CalledProcessError, UnicodeError,
            ValueError) as error:
        print(f"error: external reference check failed: {error}",
              file=sys.stderr)
        return 1
    return check_messages(messages, owner)


def check_event(path: str | Path) -> int:
    """Return 1 when a pull request cross-references an external owner."""
    try:
        title, body, author, owner = _load_event(Path(path))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        print(f"error: external reference check failed: {error}",
              file=sys.stderr)
        return 1
    if author == DEPENDABOT_LOGIN:
        print("no external-reference findings found")
        return 0
    findings = find_external_references(title, owner, "pull_request.title")
    findings.extend(
        find_external_references(body, owner, "pull_request.body")
    )
    for message in findings:
        print(message)
    if findings:
        return 1
    print("no external-reference findings found")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the event and commit-range modes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="", help="base ref (exclusive)")
    parser.add_argument("--head", default="", help="head ref (inclusive)")
    parser.add_argument("--owner", default="",
                        help="current repository owner; overrides discovery")
    parser.add_argument("--unpushed", action="store_true",
                        help="check commits absent from every remote")
    return parser


def main(argv: list | None = None) -> int:
    """Check a commit range, or the event path supplied by the runner."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if bool(args.base) != bool(args.head):
        parser.error("--base and --head must be given together")
    if args.base and args.unpushed:
        parser.error("--unpushed does not combine with --base and --head")
    if args.base or args.unpushed:
        try:
            owner = resolve_owner(args.owner, os.environ, os.getcwd())
        except (OSError, ValueError) as error:
            print(f"error: external reference check failed: {error}",
                  file=sys.stderr)
            return 1
        if args.unpushed:
            return check_unpushed(owner)
        return check_range(args.base, args.head, owner)
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        print("error: GITHUB_EVENT_PATH is unset", file=sys.stderr)
        return 1
    return check_event(event_path)


if __name__ == "__main__":
    sys.exit(main())
