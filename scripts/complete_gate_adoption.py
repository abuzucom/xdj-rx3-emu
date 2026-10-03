#!/usr/bin/env python3
"""Install one verified staged gate set without exposing ordinary work."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import check_gate_adoption as adoption
except ImportError:
    from scripts import check_gate_adoption as adoption

STAGING_DIRECTORY = ".gate-staging"
EXTRA_CLIENT_CONFIGS = (".codex/config.toml",)
CHECK_TIMEOUT_SECONDS = 30


def _is_under(path: Path, parent: Path) -> bool:
    """Return whether a resolved path remains below its expected parent."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _candidate(root: Path, value: str) -> Path:
    """Return a verified candidate directory below the fixed staging root."""
    staging = (root / STAGING_DIRECTORY).resolve(strict=True)
    candidate = Path(value).resolve(strict=True)
    if not candidate.is_dir() or not _is_under(candidate, staging):
        raise ValueError("candidate must be a directory below .gate-staging")
    return candidate


def _candidate_paths(candidate: Path) -> list[str]:
    """Return the complete static file set required for one transaction."""
    manifest_path = candidate / adoption.SHARED_MANIFEST
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    shared = document.get("shared")
    if not isinstance(shared, dict):
        raise ValueError("candidate shared-files.json is invalid")
    paths = set(shared)
    paths.update(adoption.CLIENT_HOOKS)
    paths.update(adoption.REQUIRED_CHECKERS)
    paths.update(adoption.REQUIRED_POLICY)
    paths.update(adoption.CONFIG_PATHS)
    paths.update(relative for relative, _event, _matcher in adoption.TRANSACTION_CONFIGS.values())
    paths.update(EXTRA_CLIENT_CONFIGS)
    return sorted(paths)


def _validate_candidate(root: Path, candidate: Path) -> None:
    """Reject an incomplete candidate before any target file changes."""
    checker = root / "scripts" / "check_gate_adoption.py"
    result = subprocess.run(
        [sys.executable, "-E", "-s", str(checker), "--root", str(candidate)],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CHECK_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode:
        raise ValueError("candidate fails check_gate_adoption.py")


def _target_path(root: Path, relative: str) -> Path:
    """Return a non-symlink target path below the repository root."""
    root = root.resolve(strict=True)
    path = root / relative
    if not _is_under(path.parent.resolve(strict=False), root):
        raise ValueError(f"target escapes repository: {relative}")
    if path.is_symlink():
        raise ValueError(f"target is a symlink: {relative}")
    return path


def _replace_file(source: Path, target: Path) -> None:
    """Replace one target through a same-directory temporary file."""
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"candidate artifact is not a regular file: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".gate-adoption-", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        shutil.copystat(source, temporary)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _ordered_paths(paths: list[str]) -> list[str]:
    """Copy hook registrations last so one invocation completes the transaction."""
    transaction_paths = {relative for relative, _event, _matcher in adoption.TRANSACTION_CONFIGS.values()}
    ordinary = sorted(path for path in paths if path not in transaction_paths)
    registrations = sorted(path for path in paths if path in transaction_paths)
    return ordinary + registrations


def _backup_path(backup: Path, relative: str) -> Path:
    """Return one backup path below the transaction-owned directory."""
    path = backup / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _restore_paths(
    root: Path,
    backup: Path,
    replaced: list[tuple[str, bool]],
    created_directories: list[Path],
) -> None:
    """Restore all replaced targets in reverse order after transaction failure."""
    for relative, existed in reversed(replaced):
        target = _target_path(root, relative)
        if existed:
            _replace_file(_backup_path(backup, relative), target)
        elif target.exists():
            target.unlink()
    for directory in created_directories:
        directory.rmdir()


def _create_target_parents(root: Path, target: Path) -> list[Path]:
    """Create missing target parents and return only transaction-owned paths."""
    created = []
    current = target.parent
    while current != root and not current.exists():
        created.append(current)
        current = current.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    return created


def _install_paths(
    root: Path, candidate: Path, paths: list[str], validate=None,
) -> None:
    """Install paths and restore every changed target if any replacement fails."""
    root = root.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix=".gate-adoption-", dir=root) as temporary:
        backup = Path(temporary)
        replaced = []
        created_directories = []
        try:
            for relative in paths:
                target = _target_path(root, relative)
                created_directories.extend(_create_target_parents(root, target))
                existed = target.exists()
                if existed:
                    _replace_file(target, _backup_path(backup, relative))
                _replace_file(candidate / relative, target)
                replaced.append((relative, existed))
            if validate is not None:
                validate()
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            try:
                _restore_paths(root, backup, replaced, created_directories)
            except (OSError, ValueError, subprocess.TimeoutExpired) as restore_error:
                raise OSError(f"{error}; rollback failed: {restore_error}") from restore_error
            raise


def main(argv: list[str]) -> int:
    """Install a complete staged candidate and verify the resulting target."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--candidate", required=True)
    options = parser.parse_args(argv)
    root = Path.cwd().resolve(strict=True)
    try:
        candidate = _candidate(root, options.candidate)
        _validate_candidate(root, candidate)
        paths = _ordered_paths(_candidate_paths(candidate))
        _install_paths(root, candidate, paths, lambda: _validate_candidate(root, root))
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        print(f"gate adoption transaction failed: {error}", file=sys.stderr)
        return 1
    print("gate adoption transaction completed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
