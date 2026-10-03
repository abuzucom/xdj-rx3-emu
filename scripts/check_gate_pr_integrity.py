#!/usr/bin/env python3
"""Require external approval for pull requests that alter gate enforcement."""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

APPROVAL_LABEL = "gate-change-approved"
MAX_LABEL_BYTES = 4096
MAX_TIMELINE_BYTES = 1024 * 1024
SHA_PATTERN = re.compile(r"[0-9a-f]{40,64}")
TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
HUMAN_ACTOR_TYPE = "User"
STALE_APPROVAL_MESSAGE = (
    f"the {APPROVAL_LABEL} label predates the latest push. Review the current "
    "head, then remove and re-add the label")
PROTECTED_PREFIXES = (
    "hooks/",
    ".claude/",
    ".codex/",
    ".agents/",
    ".gemini/",
    ".github/workflows/",
    "docs/agent-policy/",
)
PROTECTED_FILES = frozenset({
    ".github/CODEOWNERS",
    ".pre-commit-config.yaml",
    "AGENTS.md",
    "CODEOWNERS",
    "Makefile",
    "docs/CODEOWNERS",
    "requirements-checkers.txt",
    "shared-files.json",
    "scripts/banned_models.txt",
    "scripts/check_agent_prose_gate.py",
    "scripts/check_banned_agents.py",
    "scripts/check_branch_name.py",
    "scripts/check_commit_attribution.py",
    "scripts/check_compliance_tree.py",
    "scripts/check_gate_adoption.py",
    "scripts/check_gate_pr_integrity.py",
    "scripts/check_git_identity.py",
    "scripts/check_hook_coverage.py",
    "scripts/check_hook_launchers.py",
    "scripts/complete_gate_adoption.py",
    "scripts/read_git_state.py",
    "scripts/run_tests.py",
    "scripts/sync.py",
    "scripts/trusted_bot_identities.py",
    "scripts/trusted_gh.py",
    "scripts/trusted_git.py",
})
FETCH_TIMEOUT_SECONDS = 30
GIT_TIMEOUT_SECONDS = 10
# macOS and Windows checkouts load `Hooks/x.py` as `hooks/x.py`, so matching
# ignores case.
FOLDED_PREFIXES = tuple(prefix.casefold() for prefix in PROTECTED_PREFIXES)
FOLDED_FILES = frozenset(path.casefold() for path in PROTECTED_FILES)


def requires_approval(paths: list[str]) -> bool:
    """Return whether changed paths reach gate-enforcement surfaces."""
    return any(
        path.casefold() in FOLDED_FILES or path.casefold().startswith(FOLDED_PREFIXES)
        for path in paths
    )


def has_approval_label(labels: list[str]) -> bool:
    """Return whether trusted pull-request metadata contains the exact label."""
    return APPROVAL_LABEL in labels


def _timestamp(value: object) -> datetime:
    """Parse one GitHub UTC timestamp in its fixed API form."""
    if not isinstance(value, str) or not TIMESTAMP_PATTERN.fullmatch(value):
        raise ValueError("timeline timestamp is malformed")
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def _validate_event(event: object) -> None:
    """Reject one timeline event that lacks a required typed field."""
    if not isinstance(event, dict):
        raise ValueError("timeline event must be an object")
    if not isinstance(event.get("event"), str) or not isinstance(event.get("actor_type"), str):
        raise ValueError("timeline event lacks a type or actor type")
    if event.get("label") is not None and not isinstance(event["label"], str):
        raise ValueError("timeline event label must be a string or null")
    _timestamp(event.get("created_at"))


def load_timeline(path: Path) -> dict:
    """Read one bounded approval timeline and reject incomplete data."""
    with open(path, "rb") as handle:
        content = handle.read(MAX_TIMELINE_BYTES + 1)
    if len(content) > MAX_TIMELINE_BYTES:
        raise ValueError("timeline data exceeds the size limit")
    document = json.loads(content.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("timeline data must be a JSON object")
    events = document.get("events")
    suite_times = document.get("head_suite_times")
    if not isinstance(events, list) or not isinstance(suite_times, list):
        raise ValueError("timeline data lacks events or head suite times")
    for event in events:
        _validate_event(event)
    for value in suite_times:
        _timestamp(value)
    return document


def approval_is_current(document: dict) -> bool:
    """Return whether a person applied the label after the head last changed.

    The earliest check suite for the head SHA stands in for its push time,
    because the timeline omits ordinary pushes and commit dates are
    author-controlled.
    """
    suite_times = [_timestamp(value) for value in document["head_suite_times"]]
    if not suite_times:
        return False
    events = document["events"]
    barriers = [min(suite_times)] + [
        _timestamp(event["created_at"]) for event in events
        if event["event"] == "head_ref_force_pushed"]
    approvals = [
        _timestamp(event["created_at"]) for event in events
        if event["event"] == "labeled" and event["label"] == APPROVAL_LABEL
        and event["actor_type"] == HUMAN_ACTOR_TYPE]
    if not approvals:
        return False
    approved_at = max(approvals)
    removed = any(
        event["event"] == "unlabeled" and event["label"] == APPROVAL_LABEL
        and _timestamp(event["created_at"]) >= approved_at
        for event in events)
    return not removed and approved_at > max(barriers)


def _approval_failure(paths: list[str], labels: list[str], timeline) -> str:
    """Return why protected changes lack current approval, or an empty string."""
    if not requires_approval(paths):
        return ""
    if not has_approval_label(labels):
        return f"protected gate changes require the {APPROVAL_LABEL} label"
    if timeline is not None and not approval_is_current(timeline):
        return STALE_APPROVAL_MESSAGE
    return ""


def _revision(value: str) -> str:
    """Validate one immutable Git object identifier."""
    if not SHA_PATTERN.fullmatch(value):
        raise ValueError("revision must be a 40 to 64 character lowercase SHA")
    return value


def _labels(value: str) -> list[str]:
    """Parse bounded pull-request labels as untrusted JSON data."""
    if len(value.encode("utf-8")) > MAX_LABEL_BYTES:
        raise ValueError("label data exceeds the size limit")
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("label data must be a JSON string list")
    return parsed


def _run(command: list[str], root: Path, timeout: int) -> subprocess.CompletedProcess:
    """Run one fixed argument-array command with bounded text output."""
    return subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _fetch_head(root: Path, number: int) -> None:
    """Fetch one validated pull-request head through the trusted Git wrapper."""
    wrapper = root / "scripts" / "trusted_git.py"
    refspec = f"refs/pull/{number}/head"
    result = _run(
        [sys.executable, "-E", "-s", str(wrapper), "fetch", ".", "origin", refspec],
        root,
        FETCH_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError("trusted fetch of the pull-request head failed")


def _has_revision(root: Path, revision: str) -> bool:
    """Return whether one validated commit already exists locally."""
    result = _run(
        ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
        root,
        GIT_TIMEOUT_SECONDS,
    )
    return result.returncode == 0


def _changed_paths(root: Path, base: str, head: str) -> list[str]:
    """Return validated repository-relative paths changed by one pull request."""
    result = _run(
        ["git", "diff", "--name-only", "--no-renames", base, head],
        root,
        GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError("Git could not list pull-request changes")
    paths = [line for line in result.stdout.splitlines() if line]
    if any("\\" in path or path.startswith("/") or ".." in Path(path).parts for path in paths):
        raise ValueError("changed path is not a safe repository-relative path")
    return paths


def main(argv: list[str]) -> int:
    """Fail unapproved protected changes using trusted base-branch code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--labels-json", required=True)
    parser.add_argument("--timeline-json")
    options = parser.parse_args(argv)
    try:
        base = _revision(options.base)
        head = _revision(options.head)
        if options.pr_number < 1:
            raise ValueError("pull-request number must be positive")
        labels = _labels(options.labels_json)
        timeline = (load_timeline(Path(options.timeline_json))
                    if options.timeline_json is not None else None)
        root = Path.cwd().resolve(strict=True)
        if not _has_revision(root, head):
            _fetch_head(root, options.pr_number)
        paths = _changed_paths(root, base, head)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"gate integrity check failed: {error}", file=sys.stderr)
        return 1
    failure = _approval_failure(paths, labels, timeline)
    if failure:
        print(failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
