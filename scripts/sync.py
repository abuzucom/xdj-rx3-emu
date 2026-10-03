#!/usr/bin/env python3
"""Sync AGENTS.md to tool-specific copies, and watch the shared gate files.

--check verifies the AGENTS.md copies without writing.
--check-shared verifies the files that must stay byte-identical with the
other repositories adopting these gates, against SHARED_MANIFEST.
--write-shared rewrites that manifest from what is on disk.

sync.py covers the AGENTS.md family by copying. It cannot copy the gate
files, which live in repositories it cannot see, so it compares hashes
against a manifest committed in each of them instead. A file that differs
is either a fix one repository has and the other does not, or drift that
belongs in the drift record. Both need somebody to look.
"""
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

SOURCE = "AGENTS.md"
SHARED_MANIFEST = "shared-files.json"
REPOSITORY_ONLY_START = "<!-- repository-only:start -->"
REPOSITORY_ONLY_END = "<!-- repository-only:end -->"
SOURCE_ONLY_START = "<!-- source-only:start -->"
SOURCE_ONLY_END = "<!-- source-only:end -->"
MAX_POLICY_BYTES = 64 * 1024
# Files that must be byte-identical wherever these gates are installed. A
# decision reached by one gate and not the other is the failure the whole
# design exists to prevent, so the files carrying decisions are listed here
# and their hashes are committed in every repository holding them.
SHARED_FILES = [
    "hooks/github-command-denylist.txt",
    "hooks/_bash_parser.py",
    "hooks/_cmd_parser.py",
    "hooks/_gate_core.py",
    "hooks/_platform_policy.py",
    "hooks/block_destructive_bash.py",
    "hooks/block_destructive_cmd.py",
    "hooks/block_destructive_powershell.py",
    "hooks/enforce_gate_adoption.py",
    "hooks/reinject_agents_policy.py",
    "hooks/require_consent.py",
    "scripts/check_gate_adoption.py",
    "scripts/complete_gate_adoption.py",
    "scripts/check_gate_pr_integrity.py",
    "scripts/check_hook_launchers.py",
    "tests/gate_corpus.py",
    "tests/json_line_worker.py",
    "tests/json_line_worker_child.py",
    "tests/test_block_destructive_bash.py",
    "tests/test_block_destructive_cmd.py",
    "tests/test_block_destructive_powershell.py",
    "tests/test_check_gate_adoption.py",
    "tests/test_complete_gate_adoption.py",
    "tests/test_check_hook_launchers.py",
    "tests/test_check_gate_pr_integrity.py",
    "tests/test_enforce_gate_adoption.py",
    "tests/test_gate_adoption_transaction.py",
    "tests/test_platform_policy.py",
]
COPIES = [
    "CLAUDE.md",
    "GEMINI.md",
    "CONVENTIONS.md",
    ".cursorrules",
    ".clinerules",
    ".windsurfrules",
    ".copilot-instructions",
    ".github/copilot-instructions.md",
]
ADOPTER_POLICY_FILES = (
    "docs/agent-policy/adoption.md",
    "docs/agent-policy/enforcement.md",
    "docs/agent-policy/clients.md",
    "docs/agent-policy/github.md",
    "docs/agent-policy/security.md",
    "docs/project-orientation.md",
)
SOURCE_ONLY_POLICY_FILES = ("docs/agent-policy/source-orientation.md",)
SUPPORTING_POLICY_FILES = ADOPTER_POLICY_FILES


def policy_bytes(root: Path) -> bytes:
    """Return canonical policy plus available supporting documents."""
    source, _ = _inspect_source(root)
    if source.stat().st_size > MAX_POLICY_BYTES:
        raise ValueError("AGENTS.md exceeds the policy size limit")
    raw = source.read_bytes()
    parts = []
    root_resolved = root.resolve()
    for relative_name in ADOPTER_POLICY_FILES + SOURCE_ONLY_POLICY_FILES:
        path = _lexical_path(root, relative_name)
        if not path.exists():
            if relative_name in SOURCE_ONLY_POLICY_FILES:
                continue
            raise ValueError(f"{relative_name} is missing")
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("supporting policy is not a regular file")
        if details.st_size > MAX_POLICY_BYTES:
            raise ValueError("supporting policy exceeds the policy size limit")
        if not path.resolve().is_relative_to(root_resolved):
            raise ValueError("supporting policy escapes the repository")
        content = path.read_bytes()
        content.decode("ascii")
        parts.append(content + (b"\n" if not content.endswith(b"\n") else b""))
    assembled = raw + (b"\n" if not raw.endswith(b"\n") else b"") + b"".join(parts)
    if len(assembled) > MAX_POLICY_BYTES:
        raise ValueError("AGENTS.md exceeds the policy size limit")
    return assembled


def adoptable_content(content: str) -> str:
    """Return policy content without source-repository orientation."""
    if content.count(REPOSITORY_ONLY_START) != 1:
        raise ValueError("repository-only start marker must occur once")
    if content.count(REPOSITORY_ONLY_END) != 1:
        raise ValueError("repository-only end marker must occur once")
    prefix, remainder = content.split(REPOSITORY_ONLY_START, 1)
    _, suffix = remainder.split(REPOSITORY_ONLY_END, 1)
    if prefix.endswith("\n") and suffix.startswith("\n"):
        suffix = suffix[1:]
    content = prefix + suffix
    if content.count(SOURCE_ONLY_START) != content.count(SOURCE_ONLY_END):
        raise ValueError("source-only markers must be balanced")
    while SOURCE_ONLY_START in content:
        source_prefix, remainder = content.split(SOURCE_ONLY_START, 1)
        _, source_suffix = remainder.split(SOURCE_ONLY_END, 1)
        content = source_prefix + source_suffix
    return content


def print_adoptable(root: Path) -> int:
    """Print adoptable policy content without changing files."""
    try:
        source, _ = _inspect_source(root)
        content = policy_bytes(source.parent).decode("ascii")
        print(adoptable_content(content), end="")
    except (OSError, UnicodeDecodeError, ValueError):
        print("error: adoptable policy generation failed", file=sys.stderr)
        return 1
    return 0


def _is_within(root: Path, path: Path) -> bool:
    """Return whether path is root or one of its descendants."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _lexical_path(root: Path, name: str) -> Path:
    """Return a normalized path that remains lexically beneath root."""
    normalized_root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(root / name))
    if not _is_within(normalized_root, path):
        raise ValueError("path escapes root")
    return path


def _require_resolved_within(root: Path, path: Path) -> None:
    """Reject paths whose existing components resolve outside root."""
    resolved_root = root.resolve(strict=True)
    resolved_path = path.resolve(strict=False)
    if not _is_within(resolved_root, resolved_path):
        raise ValueError("resolved path escapes root")


def _directory_state(path: Path) -> tuple:
    """Return a stable identity for a real directory found with lstat."""
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError("directory component is not a real directory")
    return details.st_dev, details.st_ino


def _regular_state(path: Path) -> tuple:
    """Return state for a regular, non-symlink file found with lstat."""
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("path is not a regular file")
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
    )


def _target_state(path: Path) -> tuple | None:
    """Return target state without following a link, or None if absent."""
    try:
        return _regular_state(path)
    except FileNotFoundError:
        return None


def _validate_parent_tree(root: Path, parent: Path, create: bool) -> None:
    """Validate each parent with lstat, creating absent directories safely."""
    current = root
    _directory_state(current)
    for part in parent.relative_to(root).parts:
        current = current / part
        try:
            _directory_state(current)
        except FileNotFoundError:
            if not create:
                return
            os.mkdir(current)
            _directory_state(current)
        _require_resolved_within(root, current)


def _inspect_source(root: Path) -> tuple:
    """Validate the source and return its path and current state."""
    source = _lexical_path(root, SOURCE)
    _validate_parent_tree(root, source.parent, create=False)
    source_state = _regular_state(source)
    _require_resolved_within(root, source)
    return source, source_state


def _inspect_target(root: Path, name: str, create: bool) -> tuple:
    """Validate one destination and return its path and current state."""
    target = _lexical_path(root, name)
    _validate_parent_tree(root, target.parent, create=create)
    target_state = _target_state(target)
    _require_resolved_within(root, target)
    return target, target_state


def files_match(source: Path, target: Path) -> bool:
    """Compare regular file contents, normalizing line endings."""
    try:
        _regular_state(source)
        if _target_state(target) is None:
            return False
        expected = policy_bytes(source.parent)
        actual = target.read_bytes().replace(b"\r\n", b"\n")
        return expected.replace(b"\r\n", b"\n") == actual
    except (OSError, RuntimeError, UnicodeDecodeError, ValueError):
        return False


def _copy_to_temp(source: Path, parent: Path) -> Path:
    """Copy source into a flushed temporary file beside the destination."""
    descriptor, temp_name = tempfile.mkstemp(prefix=".sync-", dir=parent)
    temp_path = Path(temp_name)
    complete = False
    try:
        os.close(descriptor)
        shutil.copyfile(source, temp_path)
        assembled = policy_bytes(source.parent)
        if assembled != source.read_bytes():
            temp_path.write_bytes(assembled)
        with temp_path.open("ab") as temp_file:
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.chmod(stat.S_IMODE(source.lstat().st_mode))
        complete = True
    finally:
        if not complete:
            temp_path.unlink(missing_ok=True)
    return temp_path


def _revalidate_destination(
        root: Path, target: Path, target_state: tuple | None,
        parent_state: tuple) -> None:
    """Reject destination changes made while the temporary copy was written."""
    if _target_state(target) != target_state:
        raise OSError("target changed during copy")
    _require_resolved_within(root, target)
    _validate_parent_tree(root, target.parent, create=False)
    if _directory_state(target.parent) != parent_state:
        raise OSError("target parent changed during copy")


def _atomic_copy(root: Path, source: Path, target: Path) -> None:
    """Copy source to target without exposing a partial destination."""
    source_state = _inspect_source(root)[1]
    target_state = _inspect_target(
        root, str(target.relative_to(root)), create=True)[1]
    parent = target.parent
    parent_state = _directory_state(parent)
    temp_path = _copy_to_temp(source, parent)
    try:
        if _regular_state(source) != source_state:
            raise OSError("source changed during copy")
        _revalidate_destination(root, target, target_state, parent_state)
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)


def sync_copies(check_only: bool) -> int:
    """Copy SOURCE over each target, or with --check report stale targets.

    Returns a process exit code: 0 on success, 1 if a check fails.
    """
    targets = []
    stale = []
    try:
        root = Path(__file__).resolve().parent.parent
        source, _ = _inspect_source(root)
        for name in COPIES:
            target, _ = _inspect_target(root, name, create=False)
            targets.append((name, target))
            if not files_match(source, target):
                stale.append(name)
    except (OSError, RuntimeError, ValueError):
        print("error: unsafe or inaccessible sync path", file=sys.stderr)
        return 1

    if check_only:
        if stale:
            print(f"out of sync with {SOURCE}: {', '.join(stale)}", file=sys.stderr)
            print("run: make sync (or python scripts/sync.py)", file=sys.stderr)
            return 1
        print("all copies in sync")
        return 0

    try:
        for name, target in targets:
            if name not in stale:
                continue
            _atomic_copy(root, source, target)
            print(f"synced {name}")
    except (OSError, RuntimeError, ValueError):
        print("error: sync copy failed", file=sys.stderr)
        return 1
    if not stale:
        print("all copies already in sync")
    return 0


def content_digest(path: Path) -> str:
    """Return the file's SHA-256 with line endings normalized.

    Normalizing is what keeps a Windows checkout from reporting every
    shared file as drift. SHA-256 rather than a faster hash because this
    is an integrity comparison, not a cache key (Rule 7).
    """
    body = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def read_manifest(root: Path) -> dict:
    """Return the shared-file manifest, or an empty one when absent."""
    manifest = root / SHARED_MANIFEST
    if not manifest.is_file():
        return {}
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError as error:
        print(f"error: {SHARED_MANIFEST} is not valid JSON ({error})",
              file=sys.stderr)
        return {}


def shared_problems(root: Path, recorded: dict) -> list:
    """Return one line per shared file that does not match the manifest."""
    problems = []
    for name in SHARED_FILES:
        path = root / name
        if not path.is_file():
            problems.append(f"{name}: listed as shared but not present here")
            continue
        if name not in recorded:
            problems.append(f"{name}: shared but absent from {SHARED_MANIFEST}")
            continue
        try:
            actual = content_digest(path)
        except (OSError, UnicodeDecodeError) as error:
            problems.append(f"{name}: cannot be read ({error})")
            continue
        if actual != recorded[name]:
            problems.append(f"{name}: differs from the recorded copy")
    return problems


def check_shared(root: Path) -> int:
    """Report shared files that no longer match the committed manifest."""
    manifest = read_manifest(root)
    if not manifest:
        print(f"error: {SHARED_MANIFEST} is missing or unreadable",
              file=sys.stderr)
        return 1
    problems = shared_problems(root, manifest.get("shared", {}))
    if not problems:
        print(f"all {len(SHARED_FILES)} shared files match {SHARED_MANIFEST}")
        return 0
    for line in problems:
        print(line, file=sys.stderr)
    print("Mirror the change into every repository holding these files, then "
          "run: python scripts/sync.py --write-shared in each. If the "
          "difference is deliberate, record it in the drift file and move the "
          "file out of SHARED_FILES.", file=sys.stderr)
    return 1


def write_shared(root: Path) -> int:
    """Rewrite the manifest from the shared files on disk."""
    recorded = {}
    for name in SHARED_FILES:
        path = root / name
        if not path.is_file():
            print(f"error: {name} is listed as shared but not present here",
                  file=sys.stderr)
            return 1
        recorded[name] = content_digest(path)
    body = {
        "note": ("SHA-256 of every file that must stay byte-identical across "
                 "the repositories adopting these gates. Line endings are "
                 "normalized before hashing. Regenerate with "
                 "scripts/sync.py --write-shared and commit the result in "
                 "every one of them."),
        "shared": recorded,
    }
    (root / SHARED_MANIFEST).write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"recorded {len(recorded)} shared files in {SHARED_MANIFEST}")
    return 0


def main(argv: list) -> int:
    """Dispatch on the mode flag and return a process exit code."""
    root = Path(__file__).resolve().parent.parent
    if "--print-adoptable" in argv:
        return print_adoptable(root)
    if "--check-shared" in argv:
        return check_shared(root)
    if "--write-shared" in argv:
        return write_shared(root)
    return sync_copies(check_only="--check" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
