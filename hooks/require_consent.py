#!/usr/bin/env python3
"""Route writes to existing test files through a per-act human decision.

Not part of AGENTS.md, which stays tool-agnostic and is synced to non-Claude
tools verbatim. This is a Claude-Code-specific hook under hooks/, wired via
`.claude/settings.json` (live in this repo) or
hooks/claude-code-settings.example.json (for adopting repos). One file serves
two registrations, dispatched on `hook_event_name` and `tool_name`.

`PreToolUse` on Edit, Write, MultiEdit, and NotebookEdit gates writes to test
files. AGENTS.md Rule 3 says an agent that finds a test wrong stops, reports,
and waits for a human decision. An agent can talk itself out of that rule by
deciding the rule's purpose does not cover this case; it cannot talk itself
past a permission prompt. So the gate returns `ask` and the human answers.

Creating a test file that does not exist yet is the only exemption, and it is
the one the mandated test-first workflow needs. Every edit to a file that
already exists asks, in any language.

An earlier form cleared an end-of-file append as additive. Textual checks
cannot carry that claim: appending `ExistingTest.__unittest_skip__ = True`,
or rebinding the class to `None`, leaves every assertion above it present and
inert. Enumerating those spellings is a denylist over text an attacker
chooses, so the carve-out is gone rather than patched. The cost is real: an
iteration on an existing test now prompts.

`SessionStart` states which gates are live.

An `ask` needs a human to answer it. When `permission_mode` reports an
unattended session, or a mode this hook does not recognize, the ask becomes a
deny: absence of a human is absence of consent. The unrecognized value is
named in the reason, so a mode Claude Code adds later is visible rather than
silently denied.

Paths are resolved before anything is decided about them. A path is only as
trustworthy as the file it reaches, so classification runs on the name given,
its canonical target, and, when the file has more than one link, the inodes
of the project's test tree: `realpath` resolves a symlink but a hard link has
no target to resolve.

Unattended modes deny every gated act. Repository-controlled hooks are
best-effort prompts for compliant workflows, not an authorization boundary.
Repository writers can alter this hook or its settings before it runs.

A Bash call can also write a test file, through a redirect, a
here-document, `tee`, `sed -i`, `cp`, or `mv`, where no Edit or Write
matcher sees it. hooks/block_destructive_bash.py routes those to the same
decision, using the classifier both gates share in _gate_core.py. A write
through a program neither gate knows still passes.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

GATE = "require_consent.py"

try:
    import _gate_core as core
except ImportError as error:  # pragma: no cover (exercised by the adoption test)
    # Fail closed. Claude Code treats any non-zero exit other than 2 as a
    # non-blocking error, so an unhandled ImportError would wave the write
    # through in exactly the repos that installed this gate.
    _REASON = (f"hooks/_gate_core.py could not be imported ({error}), "
               "so the gate cannot clear this call")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": _REASON,
    }}))
    print(_REASON, file=sys.stderr)
    sys.exit(2)

GATED_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
PATH_KEYS = ("file_path", "notebook_path")
PROTECTED_PARTS = ("hooks", ".claude", ".git", ".agents", ".codex", ".gemini")
POLICY_REASON = "rewrites agent policy text that later sessions load as instructions"
SKIP_WALK_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__"})
MAX_INODE_WALK = 20000

SESSION_NOTICE = """Consent gates are live in this repository.

Every edit to a test file that already exists routes to the user for a
decision at the act (AGENTS.md Rule 3), in any language. Creating a new test
file is not gated, so the test-first workflow keeps its exemption where it is
verifiable. Destructive and history rewriting Bash commands route the same way
(Rule 2). Writes to hooks/, .claude/, .git/, and scripts/banned_models.txt route
the same way (Rule 22), because they decide whether these gates run at all.

These repository-controlled hooks are best-effort prompts for compliant
workflows, not an authorization boundary. Repository writers can alter them or
their settings. Tamper resistance requires controls outside this repository.

Approval of a plan is not authorization for the individual acts inside it.

GATE CHECKLIST, before you write any question for the user.

Does any option require editing an existing test, deleting files or
directories, rewriting pushed history, adding or upgrading a dependency, or
changing a public API contract?

If yes, that option must name the artifact and the rule in its own text, and
must say it requires the user's sign-off. Do not label it Recommended. State
the rule-governed cost, not only the engineering trade-off: the user cannot
make a call you did not put in front of them.

This arrives now rather than when a question is asked, because by the time a
question reaches a hook its options are already written."""


def is_test_path(path: str) -> bool:
    """Return True if `path` names a test file, per the shared classifier."""
    return core.is_test_path(path)


def _relative_name(target: str, project_dir: str):
    """Return the lowercased root-relative name of `target`, or None outside it."""
    root = os.path.realpath(project_dir)
    target_real = os.path.realpath(target)
    if not (target_real == root or target_real.startswith(root + os.sep)):
        return None
    relative = os.path.relpath(target_real, root).replace(os.sep, "/")
    return core.strip_windows_decorations(relative).lower()


def is_protected_path(target: str, project_dir: str) -> bool:
    """Return True if `target` is a file that decides whether gates run."""
    stripped = _relative_name(target, project_dir)
    if stripped is None:
        return False
    head = stripped.split("/", 1)[0]
    if head in PROTECTED_PARTS:
        return True
    return stripped == "scripts/banned_models.txt"


def is_policy_path(target: str, project_dir: str) -> bool:
    """Return True if `target` is agent policy text loaded as instructions."""
    stripped = _relative_name(target, project_dir)
    return stripped is not None and core.is_policy_relative(stripped)


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    """Return True if two stat results name one file on disk."""
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def reaches_a_test_inode(target: str, project_dir: str) -> bool:
    """Return True if `target` is hard-linked to a file in the test tree.

    `realpath` resolves a symlink because a symlink has a target. A hard
    link has none: two names are the same file with equal standing, so a
    test can be edited through a name that looks like anything. Compare
    inodes instead.

    Only files with more than one link are walked, so the ordinary case
    costs one stat. A test file outside the scanned tree is not matched,
    and a tree larger than MAX_INODE_WALK entries stops early. An incomplete
    scan returns None so the caller gates the write conservatively.
    """
    try:
        target_stat = os.stat(target)
    except OSError:
        return None
    if target_stat.st_nlink <= 1:
        return False

    seen = 0
    traversal_failed = [False]

    def record_walk_error(_error) -> None:
        traversal_failed[0] = True

    for current, dirnames, filenames in os.walk(
            os.path.realpath(project_dir), onerror=record_walk_error):
        dirnames[:] = [d for d in dirnames if d not in SKIP_WALK_DIRS]
        for name in filenames:
            seen += 1
            if seen > MAX_INODE_WALK:
                return None
            candidate = os.path.join(current, name)
            if not is_test_path(os.path.relpath(candidate, project_dir)):
                continue
            try:
                if _same_file(target_stat, os.stat(candidate)):
                    return True
            except OSError:
                return None
    return None if traversal_failed[0] else False


def given_path(tool_input: dict):
    """Return the path the tool call names, or None when it is not a string."""
    for key in PATH_KEYS:
        raw = tool_input.get(key)
        if raw is None or raw == "":
            continue
        return core.require_str(raw)
    return ""


def resolve_target(raw: str, project_dir: str) -> str:
    """Return the absolute, symlink-resolved path `raw` reaches."""
    candidate = raw if os.path.isabs(raw) else os.path.join(project_dir, raw)
    return os.path.realpath(candidate)


def names_a_test(raw: str, target: str, project_dir: str) -> bool:
    """Return True if the named path, its target, or its inode is a test.

    Each is checked because any one alone can be made to lie. A symlink
    called notes.txt reaches a test file, a test-named symlink reaches
    whatever its author chose, and a hard link resolves to itself.
    """
    inode_result = reaches_a_test_inode(target, project_dir)
    return (is_test_path(raw)
            or is_test_path(target)
            or inode_result is not False)


def escapes_root(target: str, project_dir: str) -> bool:
    """Return True if `target` sits outside the project root.

    commonpath rather than a string prefix: Windows short names and \\\\?\\
    prefixes make two spellings of one directory compare unequal.
    """
    root = os.path.realpath(project_dir)
    try:
        return os.path.commonpath([root, target]) != root
    except ValueError:
        # Different drives on Windows, so the target is not in the tree.
        return True


def is_redirected(raw: str, project_dir: str, target: str) -> bool:
    """Return True if resolving `raw` followed a link to somewhere else."""
    candidate = raw if os.path.isabs(raw) else os.path.join(project_dir, raw)
    return os.path.abspath(candidate) != target


def escape_reason(raw: str, target: str, project_dir: str) -> str:
    """Return why an out-of-tree path cannot be cleared, or an empty string."""
    if not escapes_root(target, project_dir):
        return ""
    if is_redirected(raw, project_dir, target):
        return ("follows a link to a test file outside the project root, "
                "which the gate cannot vouch for")
    return ("names a test file outside the project root, which the gate "
            "cannot vouch for")


def find_gate_reason(tool_name: str, target: str) -> str:
    """Return why this write needs consent, or an empty string.

    The only unprompted write is to a path that does not exist. Opening
    once and branching on the result avoids a check-then-act window in
    which a file appears between the test and the write.
    """
    try:
        handle = os.open(target, os.O_RDONLY)
    except FileNotFoundError:
        return ""
    except OSError as error:
        return (f"cannot be opened ({core.sanitize(error.strerror)}), so the "
                "gate cannot confirm what this edit changes")
    os.close(handle)
    if tool_name == "NotebookEdit":
        return "rewrites a cell in an existing test notebook"
    return ("rewrites a test file that already exists, where the gate cannot "
            "confirm the existing tests still run")


def build_reason(target: str, reason: str) -> str:
    """Return the text the user reads on the permission prompt."""
    if reason == POLICY_REASON:
        return (
            f"{core.sanitize(os.path.basename(target))}: this edit {reason}. "
            "AGENTS.md requires active-human approval for every policy change. "
            "Approving a plan is not authorization for this edit; consent is per act."
        )
    if "Rule 22" in reason or "decides whether these gates run" in reason:
        return (
            f"{core.sanitize(os.path.basename(target))}: this edit {reason}. "
            "AGENTS.md Non-negotiable Rule 22 says stop work, enter plan mode, and "
            "obtain fresh active-human approval immediately before execution. "
            "Approving a plan is not authorization for this edit; consent is per act. "
            "Never work around absent consent."
        )
    return (
        f"{core.sanitize(os.path.basename(target))}: this edit {reason}. "
        "AGENTS.md Rule 3 says stop, report it, and wait for a human decision. "
        "Approving a plan is not authorization for this edit; consent is per act. "
        "Allow it only if you decided this test should change."
    )


def emit(decision: str, reason: str) -> int:
    """Print the hook's decision and return the exit code it needs."""
    message = f"gated by hooks/{GATE}: {reason}"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": message,
        }
    }))
    if decision == "deny":
        print(message, file=sys.stderr)
        return 2
    return 0


def emit_context(event: str, context: str) -> int:
    """Print `context` for Claude to read before it acts."""
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": context}
    }))
    return 0


def _decide(payload: dict, target: str, reason: str) -> int:
    """Emit the decision, turning an ask nobody can answer into a deny."""
    message = build_reason(target, reason)
    mode = core.require_str(payload.get("permission_mode"))
    if mode in core.INTERACTIVE_MODES:
        return emit("ask", message)
    return emit("deny", f"{message} No interactive session is available to "
                        f"consent (permission_mode {core.mode_label(payload)}).")


def _write_reason(payload: dict, raw: str, target: str,
                  project_dir: str, policy_root: str) -> str:
    """Return why this write needs consent, or an empty string."""
    if (is_protected_path(target, project_dir)
            or is_protected_path(target, policy_root)):
        return "writes to a file that decides whether these gates run at all"
    if (is_policy_path(target, project_dir)
            or is_policy_path(target, policy_root)):
        return POLICY_REASON
    if names_a_test(raw, target, project_dir):
        return (escape_reason(raw, target, project_dir)
                or find_gate_reason(payload.get("tool_name", ""), target))
    return ""


def _handle_write(payload: dict, project_dir: str, policy_root: str) -> int:
    """Gate a write to an existing test file or to the gates' own files."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return emit("deny", "the tool input is malformed, so the gate cannot "
                            "read what this call writes")
    raw = given_path(tool_input)
    if raw is None:
        return emit("deny", "the file path is not a string, so the gate "
                            "cannot read it")
    if not raw:
        return 0
    target = resolve_target(raw, project_dir)
    reason = _write_reason(payload, raw, target, project_dir, policy_root)
    if not reason:
        return 0
    return _decide(payload, target, reason)


def _handle_pre_tool_use(payload: dict, project_dir: str, policy_root: str) -> int:
    """Dispatch on the tool the session is about to call."""
    if payload.get("tool_name") in GATED_TOOLS:
        return _handle_write(payload, project_dir, policy_root)
    return 0


def _run() -> int:
    """Read one payload and answer it."""
    payload = core.read_payload(empty_is_session_start=True)
    if payload is None:
        return emit("deny", "the hook payload could not be parsed, so the "
                            "gate cannot clear this call")
    if payload.get("hook_event_name") == "PreToolUse":
        return _handle_pre_tool_use(
            payload, core.project_dir(payload), core.policy_root())
    return emit_context("SessionStart", SESSION_NOTICE)


def main() -> int:
    try:
        return _run()
    except Exception:  # noqa: BLE001 (the boundary is the point)
        # Claude Code treats any non-zero exit other than 2 as a
        # non-blocking error, so an unhandled exception waves the write
        # through. Emit a fixed reason: a traceback here would carry
        # internal paths into the prompt the user reads.
        return emit("deny", "the gate raised an unexpected error, so it "
                            "cannot clear this call")


if __name__ == "__main__":
    sys.exit(main())
