#!/usr/bin/env python3
"""Print bounded sanitized Git state through the trusted Git runner."""
import json
import string
import sys
from pathlib import Path

try:
    from scripts.trusted_git import run_git
except ImportError:
    from trusted_git import run_git

MAX_FIELD_CHARS = 4096
MAX_RAW_OUTPUT_CHARS = 1024 * 1024
GIT_TIMEOUT_SECONDS = 15
PRINTABLE_MINIMUM = 32
PRINTABLE_MAXIMUM = 126
BYTE_MAXIMUM = 255
UNICODE_SHORT_MAXIMUM = 65535
SUCCESS = 0
DETACHED_HEAD = 1
REVISION_LENGTHS = frozenset((40, 64))
OPERATIONS = frozenset(("all", "branch", "remote", "revision", "status"))
TRUNCATION_MARKER = "..."
CONTROL_ESCAPES = {"\n": r"\n", "\r": r"\r", "\t": r"\t"}


def _render_character(character: str) -> str:
    """Return printable ASCII for one character."""
    if character in CONTROL_ESCAPES:
        return CONTROL_ESCAPES[character]
    codepoint = ord(character)
    if PRINTABLE_MINIMUM <= codepoint <= PRINTABLE_MAXIMUM:
        return character
    if codepoint <= BYTE_MAXIMUM:
        return f"\\x{codepoint:02x}"
    if codepoint <= UNICODE_SHORT_MAXIMUM:
        return f"\\u{codepoint:04x}"
    return f"\\U{codepoint:08x}"


def sanitize_text(value: str, *, limit: int = MAX_FIELD_CHARS) -> str:
    """Return bounded printable ASCII without terminal control characters."""
    if limit < len(TRUNCATION_MARKER):
        raise ValueError("text limit cannot hold the truncation marker")
    rendered = []
    length = 0
    content_limit = limit - len(TRUNCATION_MARKER)
    for character in value:
        replacement = _render_character(character)
        if length + len(replacement) > limit:
            return "".join(rendered)[:content_limit] + TRUNCATION_MARKER
        rendered.append(replacement)
        length += len(replacement)
    return "".join(rendered)


def redact_remote_url(value: str) -> str:
    """Remove URL user information before rendering a remote location."""
    scheme_separator = "://"
    scheme_index = value.find(scheme_separator)
    if scheme_index >= 0:
        authority_start = scheme_index + len(scheme_separator)
        path_start = value.find("/", authority_start)
        authority_end = len(value) if path_start < 0 else path_start
        account_end = value.rfind("@", authority_start, authority_end)
        if account_end >= 0:
            return value[:authority_start] + "<redacted>@" + value[account_end + 1:]
        return value
    account_end = value.find("@")
    path_start = value.find("/")
    if account_end >= 0 and (path_start < 0 or account_end < path_start):
        return "<redacted>@" + value[account_end + 1:]
    return value


def parse_branch(returncode: int, stdout: str) -> dict:
    """Return structured branch state from symbolic-ref output."""
    if returncode == DETACHED_HEAD:
        return {"branch": None, "detached": True}
    if returncode != SUCCESS:
        raise ValueError("Git could not read the current branch")
    branch = stdout.strip()
    if not branch:
        raise ValueError("Git returned an empty branch name")
    return {"branch": sanitize_text(branch), "detached": False}


def parse_revision(stdout: str) -> str:
    """Return one validated hexadecimal object identifier."""
    revision = stdout.strip()
    if len(revision) not in REVISION_LENGTHS:
        raise ValueError("Git returned an invalid revision length")
    if any(character not in string.hexdigits for character in revision):
        raise ValueError("Git returned a non-hexadecimal revision")
    return revision.lower()


def parse_status(stdout: str) -> dict:
    """Return dirty flags without returning repository paths."""
    tracked_changes = False
    untracked = False
    for line in stdout.splitlines():
        if line.startswith("? "):
            untracked = True
        elif line and not line.startswith("# "):
            tracked_changes = True
    return {
        "dirty": tracked_changes or untracked,
        "tracked_changes": tracked_changes,
        "untracked": untracked,
    }


def _run(repository: Path, arguments: list[str]):
    """Run one fixed Git query and reject oversized output."""
    result = run_git(repository, arguments, timeout=GIT_TIMEOUT_SECONDS)
    if len(result.stdout) > MAX_RAW_OUTPUT_CHARS:
        raise ValueError("Git output exceeded the safety bound")
    return result


def _read_branch(repository: Path) -> dict:
    """Read the current branch without returning raw diagnostics."""
    result = _run(repository, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    return parse_branch(result.returncode, result.stdout)


def _read_revision(repository: Path) -> dict:
    """Read the current revision."""
    result = _run(repository, ["rev-parse", "--verify", "HEAD"])
    if result.returncode != SUCCESS:
        raise ValueError("Git could not read the current revision")
    return {"revision": parse_revision(result.stdout)}


def _read_remote(repository: Path) -> dict:
    """Read the origin URL without exposing embedded credentials."""
    result = _run(repository, ["remote", "get-url", "origin"])
    if result.returncode != SUCCESS:
        return {"origin": None}
    remote = redact_remote_url(result.stdout.strip())
    return {"origin": sanitize_text(remote)}


def _read_status(repository: Path) -> dict:
    """Read dirty state without returning changed paths."""
    arguments = ["status", "--porcelain=v2", "--branch"]
    result = _run(repository, arguments)
    if result.returncode != SUCCESS:
        raise ValueError("Git could not read repository status")
    return parse_status(result.stdout)


def read_state(operation: str, repository: Path) -> dict:
    """Return structured state for one allowlisted operation."""
    readers = {
        "branch": _read_branch,
        "remote": _read_remote,
        "revision": _read_revision,
        "status": _read_status,
    }
    if operation == "all":
        state = {"operation": operation}
        for name, reader in readers.items():
            state[name] = reader(repository)
        return state
    if operation not in readers:
        raise ValueError("Git state operation is not allowed")
    return {"operation": operation, **readers[operation](repository)}


def main(argv: list[str]) -> int:
    """Print safe JSON for one fixed Git state operation."""
    if len(argv) != 1 or argv[0] not in OPERATIONS:
        print("error: select all, branch, remote, revision, or status", file=sys.stderr)
        return 2
    try:
        state = read_state(argv[0], Path.cwd())
    except (FileNotFoundError, OSError, ValueError):
        print("error: Git state read failed; verify the repository and retry", file=sys.stderr)
        return 1
    print(json.dumps(state, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
