#!/usr/bin/env python3
"""Enforce verified commit identities and approved co-author trailers."""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    from scripts.trusted_git import run_git
    from scripts.trusted_gh import run_gh
except ModuleNotFoundError:
    from trusted_git import run_git
    from trusted_gh import run_gh


APPROVED_HUMAN_COAUTHORS = frozenset()
NOREPLY_PATTERN = re.compile(
    r"\A(?P<account_id>[0-9]+)\+(?P<login>[A-Za-z0-9-]+)"
    r"@users\.noreply\.github\.com\Z",
    re.IGNORECASE,
)
TRAILER_PATTERN = re.compile(
    r"^(?P<key>[A-Za-z0-9]+(?:[- ][A-Za-z0-9]+)*):[ \t]*(?P<value>.*)$"
)
COAUTHOR_PATTERN = re.compile(r"\A(?P<name>[^<>]+?)\s*(?:<(?P<email>[^<>]+)>)?\Z")
OBJECT_ID_PATTERN = re.compile(r"\A[0-9a-fA-F]{40,64}\Z")
# A bare vendor or tool name ("Claude", "Codex") names no model. Every
# released model name carries a version digit.
VERSION_DIGIT_PATTERN = re.compile(r"[0-9]")
NON_PRINTABLE_PATTERN = re.compile(r"[^\x20-\x7e]")
MAX_REPORTED_MODEL = 80
MAX_COMMITS = 200
MAX_MESSAGE_BYTES = 256 * 1024


def terminal_trailers(body: str) -> list[tuple[str, str]]:
    """Return structured trailers from the terminal message paragraph."""
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
        match = TRAILER_PATTERN.fullmatch(line)
        if match:
            trailers.append((match.group("key"), match.group("value")))
    return trailers


def trailer_violations(commits: list[dict]) -> list[str]:
    """Return violations for unapproved co-author and assisted-by trailers."""
    violations = []
    for commit in commits:
        sha = commit["sha"][:12]
        body = commit.get("body", "")
        lines = body.splitlines()
        while lines and not lines[-1].strip():
            lines.pop()
        trailer_lines = []
        for line in reversed(lines):
            if not line.strip():
                break
            trailer_lines.append(line)
        trailer_lines.reverse()
        has_trailer = any(TRAILER_PATTERN.fullmatch(line) for line in trailer_lines)
        if has_trailer:
            for line in trailer_lines:
                if not line.startswith((" ", "\t")) and not TRAILER_PATTERN.fullmatch(line):
                    violations.append(
                        f"{sha}: non-trailer text in terminal trailer paragraph '{line}'"
                    )
        for key, value in terminal_trailers(body):
            norm_key = key.lower().replace(" ", "-")
            if norm_key == "co-authored-by":
                match = COAUTHOR_PATTERN.fullmatch(value.strip())
                if not match:
                    violations.append(f"{sha}: malformed co-author trailer")
                    continue
                name = match.group("name").strip()
                email = match.group("email")
                if not email and name:
                    continue
                if email and (name, email) in APPROVED_HUMAN_COAUTHORS:
                    continue
                violations.append(f"{sha}: unapproved co-author trailer for '{name}'")
            elif norm_key == "assisted-by":
                stripped = value.strip()
                if not stripped:
                    violations.append(f"{sha}: malformed assisted-by trailer")
                    continue
                match = COAUTHOR_PATTERN.fullmatch(stripped)
                if not match or not match.group("name").strip():
                    violations.append(f"{sha}: malformed assisted-by trailer")
                    continue
                if match.group("email"):
                    violations.append(
                        f"{sha}: assisted-by trailer must not include an email"
                    )
    return violations


def _git_log(repository: str, base: str, head: str) -> list[dict]:
    """Load bounded commit metadata from one validated range."""
    revision = f"{base}..{head}"
    result = run_git(
        repository,
        ["rev-list", "--reverse", "--end-of-options", revision],
        check=True,
        runner=subprocess.run,
        timeout=60,
    )
    shas = result.stdout.splitlines()
    if len(shas) > MAX_COMMITS or any(not OBJECT_ID_PATTERN.fullmatch(sha) for sha in shas):
        raise ValueError("commit range exceeds the supported limit")
    commits = []
    for sha in shas:
        metadata = run_git(
            repository,
            ["show", "--no-ext-diff", "--no-patch", f"--format=%H%x00%B",
             "--end-of-options", sha],
            check=True,
            runner=subprocess.run,
            timeout=30,
        ).stdout
        fields = metadata.split("\x00", 1)
        if len(fields) != 2 or fields[0] != sha:
            raise ValueError("git returned malformed commit metadata")
        if len(fields[1].encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError("commit message exceeds the supported limit")
        commits.append({"sha": sha, "body": fields[1]})
    return commits


def _repository_name(explicit: str) -> str:
    """Return the validated GitHub repository name for API requests."""
    value = explicit or os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("GitHub repository is missing or malformed")
    return value


def _commit_api(repository: str, sha: str, repo_root: str) -> dict:
    """Load one commit from GitHub through the trusted CLI wrapper."""
    endpoint = f"repos/{repository}/commits/{sha}"
    result = run_gh(repo_root, ["api", endpoint], timeout=30)
    if result.returncode != 0:
        raise RuntimeError("GitHub commit identity lookup failed")
    document = json.loads(result.stdout)
    if document.get("sha") != sha:
        raise ValueError("GitHub returned an unexpected commit")
    return document


def _identity_violation(role: str, commit: dict, identity: dict | None,
                        require_noreply: bool = False) -> str | None:
    """Return a bounded identity violation for one commit field."""
    sha = commit["sha"][:12]
    if not isinstance(identity, dict) or not identity.get("login"):
        return f"{sha}: GitHub could not resolve the {role} identity"
    raw = commit.get("commit", {}).get(role, {}).get("email", "")
    login = identity["login"]
    match = NOREPLY_PATTERN.fullmatch(raw)
    if require_noreply and not match:
        return f"{sha}: {role} email must use a numbered GitHub noreply address"
    if not match:
        return None
    if match.group("login").lower() != login.lower():
        return f"{sha}: {role} noreply login does not match GitHub identity"
    account_id = identity.get("id")
    if not isinstance(account_id, int) or match.group("account_id") != str(account_id):
        return f"{sha}: {role} noreply account ID does not match GitHub identity"
    return None


def has_agent_label(body: str) -> bool:
    """Return whether a message contains a name-only agent label."""
    for key, value in terminal_trailers(body):
        norm_key = key.lower().replace(" ", "-")
        if norm_key not in ("co-authored-by", "assisted-by"):
            continue
        match = COAUTHOR_PATTERN.fullmatch(value.strip())
        if match and not match.group("email") and match.group("name").strip():
            return True
    return False


def _reported_value(value: str) -> str:
    """Return a bounded printable copy of untrusted trailer text."""
    return NON_PRINTABLE_PATTERN.sub("", value)[:MAX_REPORTED_MODEL]


def _disclosure_labels(body: str) -> tuple[list[str], bool]:
    """Return name-only Assisted-by models and whether an agent co-author exists."""
    models = []
    agent_label = False
    for key, value in terminal_trailers(body):
        norm_key = key.lower().replace(" ", "-")
        match = COAUTHOR_PATTERN.fullmatch(value.strip())
        if not match or match.group("email") or not match.group("name").strip():
            continue
        if norm_key == "assisted-by":
            models.append(match.group("name").strip())
        elif norm_key == "co-authored-by":
            agent_label = True
    return models, agent_label


def model_disclosure_violations(commits: list[dict]) -> list[str]:
    """Require agent-labeled commits to disclose a versioned model.

    A commit with no name-only agent label is outside Rule 14 disclosure,
    so human commits without trailers pass.
    """
    violations = []
    for commit in commits:
        sha = commit["sha"][:12]
        models, agent_label = _disclosure_labels(commit.get("body", ""))
        for model in models:
            if not VERSION_DIGIT_PATTERN.search(model):
                violations.append(
                    f"{sha}: assisted-by trailer must name a versioned model, "
                    f"not '{_reported_value(model)}'"
                )
        if agent_label and not models:
            violations.append(
                f"{sha}: agent co-author label requires an Assisted-by model disclosure"
            )
    return violations


def github_identity_violations(commits: list[dict], repository: str,
                               repo_root: str) -> list[str]:
    """Validate each commit author and committer through GitHub metadata."""
    violations = []
    for commit in commits:
        document = _commit_api(repository, commit["sha"], repo_root)
        require_noreply = has_agent_label(commit.get("body", ""))
        for role in ("author", "committer"):
            violation = _identity_violation(
                role, document, document.get(role), require_noreply
            )
            if violation:
                violations.append(violation)
    return violations


def main() -> int:
    """Run strict trailer and GitHub identity checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--repo", default=os.getcwd())
    parser.add_argument("--repository", default="")
    parser.add_argument("--message-file", action="store_true")
    parser.add_argument("message_path", nargs="?")
    args = parser.parse_args()
    try:
        if args.message_file:
            if args.base or args.head or not args.message_path:
                parser.error("--message-file requires one message path")
            message_path = Path(args.message_path).resolve(strict=True)
            body = message_path.read_text(encoding="utf-8")
            if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
                raise ValueError("commit message exceeds the supported limit")
            commits = [{"sha": "message", "body": body}]
        else:
            if not args.base or not args.head or args.message_path:
                parser.error("--base and --head require no positional path")
            commits = _git_log(args.repo, args.base, args.head)
        violations = trailer_violations(commits)
        violations.extend(model_disclosure_violations(commits))
        if not args.message_file:
            violations.extend(
                github_identity_violations(
                    commits, _repository_name(args.repository), args.repo
                )
            )
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError,
            json.JSONDecodeError) as error:
        print(f"error: strict commit attribution check failed: {error}", file=sys.stderr)
        return 1
    for violation in violations:
        print(violation, file=sys.stderr)
    if violations:
        return 1
    print(f"verified commit attribution for {len(commits)} commit(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
