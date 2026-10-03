#!/usr/bin/env python3
"""Block agent-attributed prose findings that stay advisory for humans.

scripts/check_us_spelling.py, scripts/check_english_only.py,
scripts/check_hedging.py, and scripts/check_pull_request_message.py always
exit 0: their findings stay advisory for human-authored prose, since the
pattern checks misfire on legitimate human writing and semantic review
covers what they miss. AGENTS.md's rules bind every AI system regardless,
so this check reuses the same analyzers and fails on their findings, but
only for commits and pull requests that disclose agent authorship.

Attribution reuses scripts/check_commit_attribution.py::has_agent_label,
the same name-only Assisted-by/Co-authored-by trailer signal already used
to apply stricter identity rules to agent-labeled commits, and
scripts/check_banned_agents.py::_extract_pr_disclosures for a disclosure
in the pull request description alone. A pull request with no disclosure
anywhere costs nothing extra here: no file is read and no analyzer runs.

Known limitation: an agent that never discloses itself is invisible to
this signal, the same documented gap in check_banned_agents.py ("a banned
agent committing under a human's own git identity... is invisible to this
check. No mechanical check can close that gap"). Removing or editing a
disclosure trailer specifically to stop this check from firing is not a
valid fix; see docs/agent-policy/enforcement.md. Fix the flagged prose.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from scripts.check_banned_agents import _extract_pr_disclosures, load_commits
    from scripts.check_commit_attribution import has_agent_label
    from scripts.check_commit_message import find_title_violations, mask_type_prefix
    from scripts.check_english_only import find_violations as find_english_violations
    from scripts.check_pull_request_message import _load_event
    from scripts.check_us_spelling import find_violations as find_spelling_violations
    from scripts.prose_policy import find_violations as find_prose_violations, load_denylist
except ModuleNotFoundError:
    from check_banned_agents import _extract_pr_disclosures, load_commits
    from check_commit_attribution import has_agent_label
    from check_commit_message import find_title_violations, mask_type_prefix
    from check_english_only import find_violations as find_english_violations
    from check_pull_request_message import _load_event
    from check_us_spelling import find_violations as find_spelling_violations
    from prose_policy import find_violations as find_prose_violations, load_denylist

# Must track the file list .github/workflows/sync-check.yml passes to
# check_us_spelling.py, check_english_only.py, and check_hedging.py.
TARGET_FILES = (
    "AGENTS.md",
    "README.md",
    "CHANGELOG.md",
    "docs/development.md",
    "docs/project-orientation.md",
    "docs/pr-security-review.md",
    "docs/pr-quality-review.md",
    "docs/template-drift.md",
    "plan/HANDOFF.md.example",
    "SECURITY.md",
    "CONTRIBUTING.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE.md",
)

DEPENDABOT_LOGIN = "dependabot[bot]"

ANTI_BYPASS_NOTICE = (
    "fix the prose above; do not remove or edit the Assisted-by/"
    "Co-authored-by trailer to evade this check"
)


def is_agent_attributed(commits: list[dict], pr_body: str) -> bool:
    """Return whether any commit or the pull request description discloses an agent."""
    return (
        any(has_agent_label(commit.get("body", "")) for commit in commits)
        or bool(_extract_pr_disclosures(pr_body))
    )


def find_doc_findings(repo: str, entries: list) -> list[str]:
    """Return advisory-analyzer findings for every tracked policy document."""
    root = Path(repo)
    findings = []
    for relative_path in TARGET_FILES:
        text = (root / relative_path).read_text(encoding="utf-8")
        findings.extend(find_spelling_violations(text, relative_path))
        findings.extend(find_english_violations(text, relative_path))
        findings.extend(find_prose_violations(text, relative_path, entries=entries))
    return findings


def find_pr_findings(title: str, body: str, author: str, entries: list) -> list[str]:
    """Return advisory-analyzer findings for the pull request title and body."""
    findings = []
    if author != DEPENDABOT_LOGIN:
        findings.extend(find_title_violations(title))
    findings.extend(
        find_prose_violations(mask_type_prefix(title), "pull_request.title", entries=entries)
    )
    findings.extend(find_prose_violations(body, "pull_request.body", entries=entries))
    return findings


def check(base: str, head: str, repo: str, event_path: str) -> int:
    """Block on advisory-analyzer findings, but only for agent-attributed work."""
    commits = load_commits(base, head, repo)
    title, body, author = _load_event(Path(event_path))

    if not is_agent_attributed(commits, body):
        print("no agent attribution found; advisory checks remain non-blocking")
        return 0

    entries = load_denylist()
    findings = find_doc_findings(repo, entries)
    findings.extend(find_pr_findings(title, body, author, entries))

    for message in findings:
        print(message)
    if findings:
        print(ANTI_BYPASS_NOTICE)
        return 1
    print("no agent-attributed prose findings found")
    return 0


def main() -> int:
    """Check the base..head commit range for agent-attributed prose findings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="base ref (exclusive)")
    parser.add_argument("--head", required=True, help="head ref (inclusive)")
    parser.add_argument("--repo", default=os.getcwd(), help="repository to inspect (default: cwd)")
    args = parser.parse_args()
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        print("error: GITHUB_EVENT_PATH is unset", file=sys.stderr)
        return 1
    try:
        return check(args.base, args.head, args.repo, event_path)
    except (
        OSError, subprocess.SubprocessError, UnicodeError, ValueError, json.JSONDecodeError,
    ) as error:
        print(f"error: agent prose gate failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
