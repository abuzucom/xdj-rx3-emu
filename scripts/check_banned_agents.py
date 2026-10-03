#!/usr/bin/env python3
"""Flag banned-agent authorship on commits in a PR range.

Matches commit author, committer, and Co-authored-by trailer name/email,
plus the PR author's GitHub login, against a denylist. Never scans
free-form commit-message body or PR description text: "grok" is an
ordinary English verb and would false-positive constantly there.

Limitation: a banned agent committing under a human's own git identity,
with no Co-authored-by trailer, is invisible to this check. No mechanical
check can close that gap.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

try:
    from scripts.trusted_git import run_git
except ModuleNotFoundError:
    try:
        from trusted_git import run_git
    except ModuleNotFoundError:
        run_git = None

DEFAULT_BANNED_MODELS_PATH = Path(__file__).resolve().parent / "banned_models.txt"
MAX_DENYLIST_FILE_BYTES = 64 * 1024
DENYLIST_NAMES = ("grok", "xai")
DENYLIST_EMAIL_DOMAINS = ("x.ai",)
DENYLIST_MODELS = ("deepseekv4flash",)

TRAILER_LINE = re.compile(
    r"^(?P<key>[A-Za-z0-9]+(?:[- ][A-Za-z0-9]+)*):[ \t]*(?P<value>.*)$"
)
CO_AUTHOR = re.compile(
    r"^(?P<name>[^<>]+?)[ \t]*(?:<(?P<email>[^<>]+)>[ \t]*)?$"
)
OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40,64}$")
MAX_COMMITS = 200
MAX_COMMIT_BYTES = 256 * 1024
MAX_TOTAL_COMMIT_BYTES = 4 * 1024 * 1024


def _compile_wildcard(pattern: str) -> re.Pattern[str]:
    """Translate a simple glob pattern with * to an anchored regex."""
    escaped = re.escape(pattern)
    regex_str = "^" + escaped.replace(r"\*", ".*") + "$"
    return re.compile(regex_str, re.IGNORECASE)


def load_banned_models(
    file_path: Path | str | None = None,
) -> tuple[set[str], list[re.Pattern[str]]]:
    """Load exact normalized model bans and compiled wildcard patterns.

    Fails closed: raises ValueError or FileNotFoundError if the denylist file
    is missing, unreadable, or exceeds size limits.
    """
    path = Path(file_path) if file_path else DEFAULT_BANNED_MODELS_PATH
    exact_bans = set(DENYLIST_MODELS)
    wildcard_patterns: list[re.Pattern[str]] = []
    if file_path is not None and not path.is_file():
        raise FileNotFoundError(f"banned models file not found: {path}")
    if file_path is None and not path.is_file():
        raise FileNotFoundError(f"required banned models file is missing: {path}")
    size = path.stat().st_size
    if size > MAX_DENYLIST_FILE_BYTES:
        raise ValueError(
            f"{path}: {size} bytes exceeds the {MAX_DENYLIST_FILE_BYTES}-byte size limit"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"cannot read {path}: {error}") from error

    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        if "*" in cleaned:
            norm_pattern = re.sub(r"[^a-z0-9*]", "", cleaned.lower())
            if norm_pattern:
                wildcard_patterns.append(_compile_wildcard(norm_pattern))
        else:
            norm_term = re.sub(r"[^a-z0-9]", "", cleaned.lower())
            if norm_term:
                exact_bans.add(norm_term)
    return exact_bans, wildcard_patterns


def _matches_denylist(
    name: str,
    email: str,
    exact_bans: set[str] | None = None,
    wildcards: list[re.Pattern[str]] | None = None,
    *,
    is_disclosure: bool = False,
) -> bool:
    """Return True if an author, committer, or model disclosure names a banned agent."""
    name_lower = name.strip().lower()
    local_part = email.strip().lower().split("@", 1)[0]
    normalized_name = re.sub(r"[^a-z0-9]", "", name_lower)
    normalized_local = re.sub(r"[^a-z0-9]", "", local_part)
    for term in DENYLIST_NAMES:
        if term in normalized_name or term in normalized_local:
            return True
    domain = email.strip().lower().rsplit("@", 1)[-1] if "@" in email else ""
    if any(
        domain == denied or domain.endswith(f".{denied}")
        for denied in DENYLIST_EMAIL_DOMAINS
    ):
        return True
    active_exact = DENYLIST_MODELS if exact_bans is None else exact_bans
    if is_disclosure:
        for model in active_exact:
            if model in normalized_name or model in normalized_local:
                return True
        if wildcards:
            for pattern in wildcards:
                if pattern.match(normalized_name) or pattern.match(normalized_local):
                    return True
    else:
        # Human/bot author or committer field: match exact model identifier or bot login
        for model in active_exact:
            if (
                normalized_name == model
                or normalized_local == model
                or normalized_name == f"{model}bot"
                or normalized_local == f"{model}bot"
            ):
                return True
        if wildcards:
            for pattern in wildcards:
                if pattern.match(normalized_name) or pattern.match(normalized_local):
                    return True
    return False


def _extract_pr_disclosures(body: str) -> list[str]:
    """Extract model/agent names disclosed in PR description text."""
    if not body:
        return []
    disclosures = []
    for line in body.splitlines():
        clean = line.strip().lstrip("-* ").replace("**", "").strip()
        lower = clean.lower()
        if lower.startswith((
            "assisted by:", "assisted-by:", "co-authored by:", "co-authored-by:"
        )):
            _, _, model = clean.partition(":")
            val = model.strip()
            if val:
                disclosures.append(val)
    return disclosures


def _terminal_trailers(body: str) -> list[tuple[str, str]]:
    """Return key/value pairs from a structured terminal trailer paragraph."""
    lines = body.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return []
    trailer_lines = []
    for line in reversed(lines):
        if not line.strip():
            break
        trailer_lines.append(line)
    trailer_lines.reverse()
    trailers = []
    for line in trailer_lines:
        if line.startswith((" ", "\t")):
            if trailers:
                key, value = trailers[-1]
                trailers[-1] = (key, f"{value}\n{line.lstrip()}")
            continue
        match = TRAILER_LINE.fullmatch(line)
        if match:
            trailers.append((match.group("key"), match.group("value")))
    return trailers


def find_violations(
    commits: list[dict],
    pr_author: str = "",
    pr_body: str = "",
    banned_models_file: str | Path | None = None,
) -> list[str]:
    """Return one message per banned-agent authorship signal found.

    `commits` is a list of dicts with keys: sha, author_name, author_email,
    committer_name, committer_email, body (used only to parse trailers).
    """
    exact_bans, wildcards = load_banned_models(banned_models_file)
    violations = []
    for commit in commits:
        sha = commit["sha"][:12]
        for role in ("author", "committer"):
            name = commit[f"{role}_name"]
            email = commit[f"{role}_email"]
            if _matches_denylist(
                name, email, exact_bans, wildcards, is_disclosure=False
            ):
                violations.append(f"{sha}: banned-agent {role} '{name} <{email}>'")
        for key, value in _terminal_trailers(commit.get("body", "")):
            norm_key = key.lower().replace(" ", "-")
            if norm_key not in ("co-authored-by", "assisted-by"):
                continue
            match = CO_AUTHOR.fullmatch(value)
            if not match:
                continue
            name = match.group("name").strip()
            email = match.group("email") or ""
            if _matches_denylist(
                name, email, exact_bans, wildcards, is_disclosure=True
            ):
                if norm_key == "assisted-by":
                    violations.append(f"{sha}: banned-agent model '{name}'")
                else:
                    violations.append(f"{sha}: banned-agent co-author '{name} <{email}>'")
    if pr_author and _matches_denylist(
        pr_author, "", exact_bans, wildcards, is_disclosure=False
    ):
        violations.append(f"PR author: banned-agent login '{pr_author}'")
    if pr_body:
        for disclosure in _extract_pr_disclosures(pr_body):
            match = CO_AUTHOR.fullmatch(disclosure)
            name = match.group("name").strip() if match else disclosure.strip()
            email = match.group("email") or "" if match else ""
            if _matches_denylist(
                name, email, exact_bans, wildcards, is_disclosure=True
            ):
                violations.append(
                    f"PR description: banned-agent disclosure '{disclosure}'"
                )
    return violations


def _commit_size(repository, sha: str) -> int:
    """Return one bounded commit-object size."""
    result = run_git(
        repository, ["cat-file", "-s", "--", sha], check=True,
        runner=subprocess.run, timeout=30)
    raw_size = result.stdout.strip()
    if not raw_size.isdigit() or int(raw_size) > MAX_COMMIT_BYTES:
        raise ValueError(f"commit {sha} exceeds the metadata size limit")
    return int(raw_size)


def _load_commit(repository, sha: str) -> dict:
    """Load one commit without a message-controlled record delimiter."""
    fmt = "%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B"
    result = run_git(
        repository,
        ["show", "--no-ext-diff", "--no-patch", f"--format={fmt}", "--end-of-options", sha],
        check=True,
        runner=subprocess.run,
        timeout=30,
    )
    fields = result.stdout.split("\x00", 5)
    if len(fields) != 6 or fields[0] != sha:
        raise ValueError(f"malformed metadata for commit {sha}")
    return dict(
        zip(
            ("sha", "author_name", "author_email", "committer_name", "committer_email", "body"),
            fields,
        )
    )


def load_commits(base: str, head: str, repo=None) -> list[dict]:
    """Collect commit metadata for the base..head range via git log."""
    if run_git is None:
        raise FileNotFoundError("scripts/trusted_git.py is unavailable")
    repository = repo or os.getcwd()
    revision = f"{base}..{head}"
    count_result = run_git(
        repository,
        ["rev-list", "--count", "--end-of-options", revision],
        check=True,
        runner=subprocess.run,
        timeout=60,
    )
    raw_count = count_result.stdout.strip()
    if not raw_count.isdigit() or int(raw_count) > MAX_COMMITS:
        raise ValueError(f"commit range exceeds the {MAX_COMMITS}-commit limit")
    result = run_git(
        repository,
        ["rev-list", "--reverse", "--end-of-options", revision],
        check=True,
        runner=subprocess.run,
        timeout=60,
    )
    shas = result.stdout.splitlines()
    if len(shas) > MAX_COMMITS:
        raise ValueError(f"commit range exceeds the {MAX_COMMITS}-commit limit")
    if any(not OBJECT_ID.fullmatch(sha) for sha in shas):
        raise ValueError("git rev-list returned malformed object IDs")
    if len(shas) != int(raw_count):
        raise ValueError("git rev-list count changed during inspection")
    total_size = sum(_commit_size(repository, sha) for sha in shas)
    if total_size > MAX_TOTAL_COMMIT_BYTES:
        raise ValueError("commit range exceeds the metadata byte limit")
    return [_load_commit(repository, sha) for sha in shas]


def pr_author_from_event() -> str:
    """Read the PR author's GitHub login from the workflow event payload."""
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_path or not os.path.isfile(event_path):
        return ""
    try:
        with open(event_path, encoding="utf-8") as handle:
            event = json.load(handle)
        return event.get("pull_request", {}).get("user", {}).get("login", "")
    except (OSError, json.JSONDecodeError, UnicodeError) as error:
        print(f"warning: cannot read GITHUB_EVENT_PATH author: {error}", file=sys.stderr)
        return ""


def pr_body_from_event() -> str:
    """Read the PR description from the workflow event payload."""
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_path or not os.path.isfile(event_path):
        return ""
    try:
        with open(event_path, encoding="utf-8") as handle:
            event = json.load(handle)
        body = event.get("pull_request", {}).get("body")
        return body if isinstance(body, str) else ""
    except (OSError, json.JSONDecodeError, UnicodeError) as error:
        print(f"warning: cannot read GITHUB_EVENT_PATH body: {error}", file=sys.stderr)
        return ""


def check(
    base: str,
    head: str,
    repo=None,
    pr_body: str | None = None,
    banned_models_file: str | Path | None = None,
) -> int:
    """Check the base..head commit range. Return 0 when clean, 1 on a match."""
    commits = load_commits(base, head, repo)
    body = pr_body_from_event() if pr_body is None else pr_body
    violations = find_violations(
        commits,
        pr_author_from_event(),
        pr_body=body,
        banned_models_file=banned_models_file,
    )
    if violations:
        for message in violations:
            print(message, file=sys.stderr)
        print("banned agents must not read, edit, commit, or open PRs here", file=sys.stderr)
        return 1
    print("no banned-agent authorship found")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="base ref (exclusive)")
    parser.add_argument("--head", required=True, help="head ref (inclusive)")
    parser.add_argument("--repo", default=os.getcwd(), help="repository to inspect (default: cwd)")
    parser.add_argument("--pr-body", default=None, help="PR description text to inspect")
    parser.add_argument(
        "--banned-models-file", default=None, help="path to custom banned models file"
    )
    args = parser.parse_args()
    try:
        return check(
            args.base,
            args.head,
            args.repo,
            pr_body=args.pr_body,
            banned_models_file=args.banned_models_file,
        )
    except (
        OSError, subprocess.SubprocessError, UnicodeError, ValueError,
    ) as error:
        print(f"error: check_banned_agents failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
