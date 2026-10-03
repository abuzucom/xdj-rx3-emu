#!/usr/bin/env python3
"""Fail when the gate set arrived incomplete (AGENTS.md Rule 18).

An incomplete candidate cannot activate. Missing registrations, shared modules,
or cited checkers leave the verified gate set unchanged. Every gate then
behaves as designed for an absent dependency: an unregistered gate never runs,
and a gate whose shared module is absent denies and exits 2.

This checker names the absent artifact instead. It verifies three properties:
required files exist, every sibling module a gate imports exists, and every
required hook stays registered under its required events and matchers.

The registration table repeats `HOOK_MATCHERS` in
`tests/test_enforce_branch_name.py` deliberately. That table lives inside a
test file, so trimming the table together with the settings entry passes the
suite, and editing it routes through a Rule 3 consent prompt rather than
failing a check. A copy outside `tests/` fails CI when a registration
disappears. The consent prompt covers a write made through a gated tool. This
checker covers an incomplete copy and a removal that reaches the branch by
another route.
"""
import argparse
import ast
import json
import sys
from pathlib import Path

SHARED_MANIFEST = "shared-files.json"
TRANSACTION_HOOK = "enforce_gate_adoption.py"
CLIENT_HOOKS = (
    "hooks/block_infrastructure_access.py",
    "hooks/enforce_branch_name.py",
    "hooks/enforce_git_identity.py",
    f"hooks/{TRANSACTION_HOOK}",
)
REQUIRED_CHECKERS = (
    "scripts/check_banned_agents.py",
    "scripts/check_branch_name.py",
    "scripts/check_commit_attribution.py",
    "scripts/check_commit_message.py",
    "scripts/complete_gate_adoption.py",
    "scripts/check_dockerfile_root.py",
    "scripts/check_external_pr_refs.py",
    "scripts/check_git_identity.py",
    "scripts/check_gate_pr_integrity.py",
    "scripts/check_hook_launchers.py",
    "scripts/check_persist_credentials.py",
    "scripts/check_secrets_heuristic.py",
    "scripts/check_weak_hashing.py",
    "scripts/read_git_state.py",
    "scripts/trusted_gh.py",
    "scripts/trusted_git.py",
)
REQUIRED_POLICY = ("AGENTS.md", SHARED_MANIFEST)
CONFIG_PATHS = (
    ".claude/settings.json",
    "hooks/claude-code-settings.example.json",
)
TRANSACTION_CONFIGS = {
    "claude": (".claude/settings.json", "PreToolUse", "*"),
    "codex": (".codex/hooks.json", "PreToolUse", "*"),
    "antigravity": (".agents/hooks.json", "PreToolUse", "*"),
    "gemini": (".gemini/settings.json", "BeforeTool", ".*"),
}
SESSION_START_MATCHER = "startup|resume|clear|compact|fork"
SUBAGENT_MATCHER = "Explore|Plan"
EDIT_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"
FILE_TOOL_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Read|Glob|Grep"
REQUIRED_REGISTRATIONS = {
    "enforce_branch_name.py": {
        "SessionStart": {SESSION_START_MATCHER},
        "UserPromptSubmit": {""},
        "Stop": {""},
        "SubagentStop": {SUBAGENT_MATCHER},
        "PreToolUse": {"*"},
    },
    "enforce_git_identity.py": {
        "SessionStart": {SESSION_START_MATCHER},
        "PreToolUse": {"Bash"},
    },
    "require_consent.py": {
        "SessionStart": {SESSION_START_MATCHER},
        "PreToolUse": {EDIT_MATCHER},
    },
    "reinject_agents_policy.py": {
        "SessionStart": {SESSION_START_MATCHER},
        "SubagentStart": {SUBAGENT_MATCHER},
    },
    "block_destructive_bash.py": {"PreToolUse": {"Bash"}},
    "block_destructive_powershell.py": {"PreToolUse": {"PowerShell"}},
    "block_destructive_cmd.py": {"PreToolUse": {"Cmd|CMD|CommandPrompt"}},
    "block_infrastructure_access.py": {"PreToolUse": {FILE_TOOL_MATCHER}},
}


def _shared_paths(root: Path) -> tuple[list[str], list[str]]:
    """Return manifest-listed paths and any manifest read finding."""
    manifest = root / SHARED_MANIFEST
    if not manifest.is_file():
        return [], [f"{SHARED_MANIFEST} is absent"]
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [], [f"{SHARED_MANIFEST} is unreadable: {error}"]
    if not isinstance(document, dict):
        return [], [
            f"{SHARED_MANIFEST} holds {type(document).__name__} at the top "
            f"level, so the manifest lists no shared gate file"
        ]
    shared = document.get("shared")
    if not isinstance(shared, dict) or not shared:
        return [], [f"{SHARED_MANIFEST} lists no shared gate files"]
    return sorted(shared), []


def check_required_files(root: Path) -> list[str]:
    """Report every required gate artifact absent from the tree."""
    shared, findings = _shared_paths(root)
    required = list(shared) + list(CLIENT_HOOKS)
    required += list(REQUIRED_CHECKERS) + list(REQUIRED_POLICY)
    for relative in required:
        if not (root / relative).is_file():
            findings.append(f"{relative} is absent from the adopted tree")
    return findings


def _sibling_imports(source: str) -> set:
    """Return imported module names that name a sibling hook module.

    Every shared gate module starts with an underscore, so an underscore
    prefix identifies a sibling import without resolving the package.
    """
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
    return {name for name in names if name.startswith("_")}


def check_hook_imports(root: Path) -> list[str]:
    """Report every sibling module a present gate imports without it."""
    findings = []
    for path in sorted((root / "hooks").glob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            imports = _sibling_imports(source)
        except (OSError, SyntaxError, ValueError) as error:
            findings.append(f"hooks/{path.name} is unreadable: {error}")
            continue
        for name in sorted(imports):
            if not (path.parent / f"{name}.py").is_file():
                findings.append(
                    f"hooks/{path.name} imports hooks/{name}.py, which is "
                    f"absent, so the gate denies and exits 2 at run time"
                )
    return findings


def _invocations(entry: object) -> str:
    """Return one registered hook invocation as a single string."""
    if not isinstance(entry, dict):
        return ""
    arguments = entry.get("args")
    parts = [str(entry.get("command", ""))]
    if isinstance(arguments, list):
        parts += [str(value) for value in arguments]
    return " ".join(parts)


def _event_groups(document: dict, event: str) -> list:
    """Return the matcher groups registered under one event.

    Hand-edited configuration reaches this checker, so a null or
    mistyped branch reports a finding rather than raising.
    """
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return []
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return []
    return [group for group in groups if isinstance(group, dict)]


def _registered_matchers(document: dict, event: str, hook: str) -> set:
    """Return every matcher registering one hook under one event."""
    matchers = set()
    for group in _event_groups(document, event):
        entries = group.get("hooks")
        entries = entries if isinstance(entries, list) else []
        if any(hook in _invocations(entry) for entry in entries):
            matchers.add(str(group.get("matcher", "")))
    return matchers


def _event_groups_recursive(document: object, event: str) -> list[dict]:
    """Return every hook group named by one event in nested client settings."""
    groups = []
    if isinstance(document, dict):
        for key, value in document.items():
            if key == event and isinstance(value, list):
                groups.extend(item for item in value if isinstance(item, dict))
            groups.extend(_event_groups_recursive(value, event))
    elif isinstance(document, list):
        for value in document:
            groups.extend(_event_groups_recursive(value, event))
    return groups


def _group_commands(group: dict) -> list[str]:
    """Return command-plus-argument text from one bounded hook group."""
    commands = []
    for entry in group.get("hooks", []):
        if isinstance(entry, dict):
            commands.append(_invocations(entry))
    return commands


def check_transaction_registrations(root: Path) -> list[str]:
    """Require the transaction gate under every adopted client pre-tool event."""
    findings = []
    for client, (relative, event, matcher) in TRANSACTION_CONFIGS.items():
        path = root / relative
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            findings.append(f"{client} configuration {relative} is unreadable: {error}")
            continue
        groups = _event_groups_recursive(document, event)
        expected = f"{TRANSACTION_HOOK} --client {client}"
        registered = any(
            str(group.get("matcher", "")) == matcher
            and any(expected in command for command in _group_commands(group))
            for group in groups
        )
        if not registered:
            findings.append(
                f"{client} does not register {TRANSACTION_HOOK} under "
                f"{event} with matcher {matcher!r}"
            )
    return findings


def _check_document(name: str, document: dict) -> list[str]:
    """Report absent and narrowed registrations in one configuration."""
    findings = []
    for hook, events in sorted(REQUIRED_REGISTRATIONS.items()):
        for event, expected in sorted(events.items()):
            found = _registered_matchers(document, event, hook)
            missing = sorted(expected - found)
            if missing:
                findings.append(
                    f"{name} does not register {hook} under {event} with "
                    f"matcher {', '.join(repr(item) for item in missing)}"
                )
    return findings


def check_registrations(root: Path) -> list[str]:
    """Report registration findings across every client configuration."""
    findings = []
    for relative in CONFIG_PATHS:
        path = root / relative
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            findings.append(f"{relative} is absent or unreadable: {error}")
            continue
        if not isinstance(document, dict):
            findings.append(
                f"{relative} holds {type(document).__name__} at the top "
                f"level, so the file registers no hook"
            )
            continue
        findings.extend(_check_document(relative, document))
    return findings


def main(argv: list) -> int:
    """Check gate adoption completeness under one repository root."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repository root path")
    options = parser.parse_args(argv)
    root = Path(options.root)
    findings = check_required_files(root)
    findings += check_hook_imports(root)
    findings += check_registrations(root)
    findings += check_transaction_registrations(root)
    if not findings:
        return 0
    for finding in findings:
        print(finding, file=sys.stderr)
    print(
        "Rule 18 forbids partial hook or gate adoption. Keep the verified set "
        "unchanged and stage the complete candidate.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
