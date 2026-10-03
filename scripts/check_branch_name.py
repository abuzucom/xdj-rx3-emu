#!/usr/bin/env python3
"""Enforce the <type>/<kebab-description> branch naming convention.

Copy this portable checker into any repository. Use it as a pre-push hook or
CI step on `pull_request` events. Default checks exempt `main`, `master`, and a
detached HEAD. Strict agent preflight rejects those states. A mismatch exits 1.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    from scripts.trusted_git import run_git
except ModuleNotFoundError:
    from trusted_git import run_git

DEFAULT_PREFIXES = ("feat", "fix", "chore", "docs", "test")
EXEMPT_BRANCHES = ("main", "master", "HEAD")
PROHIBITED_AGENT_PREFIX = "claude/"
BRANCH_BANNED_TOKENS_PATH = Path(__file__).resolve().with_name(
    "branch_name_bans.txt"
)
FOREIGN_TOKENS = frozenset(
    "el la de que una para con las los por como pero esta este cuando "
    "le des une est dans avec pas pour sont vous nous cette mais der die "
    "das und ist nicht auch eine einen sich auf nao dos com sao isso "
    "il di che sono questo anche sul".split()
)
TECHNICAL_SUFFIXES = frozenset(
    ("base64", "es2022", "oauth2", "python310", "sha256")
)


def _pattern(prefixes: tuple[str, ...]) -> re.Pattern:
    """Build the <type>/<kebab-description> pattern for the given prefixes."""
    prefix_group = "|".join(re.escape(prefix) for prefix in prefixes)
    return re.compile(rf"^(?:{prefix_group})/[a-z0-9]+(?:-[a-z0-9]+)*$")


def _load_banned_tokens() -> frozenset[str]:
    """Load exact branch tokens that are unsuitable for repository names."""
    try:
        values = BRANCH_BANNED_TOKENS_PATH.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return frozenset()
    return frozenset(
        line.strip().casefold()
        for line in values.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _description_violations(branch: str) -> list[str]:
    """Return violations for opaque, vulgar, or clearly foreign tokens."""
    description = branch.split("/", 1)[-1]
    tokens = description.split("-")
    banned_tokens = _load_banned_tokens()
    violations = []
    for token in tokens:
        if token.casefold() in banned_tokens:
            violations.append(
                f"branch '{branch}' contains a prohibited vulgarity token"
            )
        if token.casefold() in FOREIGN_TOKENS:
            violations.append(
                f"branch '{branch}' contains a clearly non-English token"
            )
    final_token = tokens[-1]
    if (final_token.casefold() not in TECHNICAL_SUFFIXES
            and 6 <= len(final_token) <= 12
            and any(character.isalpha() for character in final_token)
            and any(character.isdigit() for character in final_token)):
        violations.append(
            f"branch '{branch}' ends with an opaque mixed alphanumeric token"
        )
    return violations


def find_violations(
    branch: str,
    prefixes: tuple[str, ...] = DEFAULT_PREFIXES,
    strict: bool = False,
) -> list[str]:
    """Return a violation message if `branch` breaks the naming convention."""
    if branch.casefold().startswith(PROHIBITED_AGENT_PREFIX):
        return [
            f"branch '{branch}' uses the prohibited claude/ agent prefix"
        ]
    if not strict and (not branch or branch in EXEMPT_BRANCHES):
        return []
    if _pattern(prefixes).match(branch):
        return _description_violations(branch)
    allowed = ", ".join(f"{prefix}/" for prefix in prefixes)
    return [f"branch '{branch}' does not match <type>/<kebab-description> ({allowed})"]


def _current_branch(repo=None) -> str:
    """Return the PR head branch in CI, or the local checked-out branch."""
    head_ref = os.environ.get("GITHUB_HEAD_REF", "")
    if head_ref:
        return head_ref
    repository = repo or os.getcwd()
    result = run_git(
        repository,
        ["rev-parse", "--abbrev-ref", "HEAD"],
        runner=subprocess.run,
    )
    branch = result.stdout.strip()
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            "git rev-parse",
            output=result.stdout,
            stderr=result.stderr,
        )
    if not branch:
        raise ValueError("git rev-parse returned an empty branch name")
    return branch


def _import_bot_registry():
    """Import the bot registry on demand.

    Hooks import this module for find_violations only, so a copy without the
    registry keeps the branch gate working.
    """
    try:
        from scripts import trusted_bot_identities
    except ModuleNotFoundError:
        import trusted_bot_identities
    return trusted_bot_identities


def _apply_bot_exemption(branch: str, violations: list[str]) -> list[str]:
    """Clear violations when the pull request event proves a trusted bot branch."""
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        return violations
    try:
        bots = _import_bot_registry()
    except ModuleNotFoundError as error:
        print(
            f"error: bot exemption registry missing ({error.name}); branch rules "
            "apply. Copy scripts/trusted_bot_identities.py from the policy source.",
            file=sys.stderr,
        )
        return violations
    if not branch.startswith(bots.EXEMPT_BRANCH_PREFIXES):
        return violations
    try:
        event = bots.load_event_author(os.environ.get("GITHUB_EVENT_PATH", ""))
    except bots.EventPayloadError as error:
        print(
            f"error: bot exemption check failed ({error}); branch rules apply. "
            "Re-run the workflow.",
            file=sys.stderr,
        )
        return violations
    if event is None:
        return violations
    bot = bots.match_exempt_bot(event.login, branch)
    if bot is not None and (bot.account_id is None or bots.event_verifies(bot, branch, event)):
        return []
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("branch", nargs="?", help="branch to check (default: current branch)")
    parser.add_argument("--repo", default=os.getcwd(), help="repository to inspect (default: cwd)")
    parser.add_argument(
        "--prefixes",
        default=",".join(DEFAULT_PREFIXES),
        help="comma-separated allowed prefixes",
    )
    parser.add_argument(
        "--strict-agent-preflight",
        action="store_true",
        help="reject primary branches and detached HEAD for agent preflight",
    )
    args = parser.parse_args()
    try:
        branch = args.branch or _current_branch(args.repo)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(
            f"Error. Branch lookup failed. Restore readable Git metadata and retry. {error}",
            file=sys.stderr,
        )
        return 1
    prefixes = tuple(prefix.strip() for prefix in args.prefixes.split(",") if prefix.strip())

    violations = find_violations(
        branch,
        prefixes,
        strict=args.strict_agent_preflight,
    )
    if violations and not args.branch:
        violations = _apply_bot_exemption(branch, violations)
    if violations:
        for message in violations:
            print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
