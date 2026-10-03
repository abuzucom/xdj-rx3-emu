#!/usr/bin/env python3
"""Resolve and run Git without repository-controlled executable lookup."""
import os
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

SAFE_CONFIG = (
    "-c",
    "core.pager=",
    "-c",
    "pager.log=false",
    "-c",
    "log.showSignature=false",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "diff.external=",
    "-c",
    "protocol.ext.allow=never",
)
GITHUB_HOSTS = frozenset(("github.com", "www.github.com"))
AMBIGUOUS_MARKERS = ("$", "`", "%")
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                   "http_proxy", "https_proxy", "all_proxy")
# The managed Codex sandbox points proxies at this closed loopback port.
MANAGED_PROXY_HOST = "127.0.0.1"
MANAGED_PROXY_PORT = 9
# Variables like GIT_SSH_COMMAND, GIT_ASKPASS, or LD_PRELOAD make Git run
# another program, so the child environment starts from this allowlist.
CHILD_ENVIRONMENT = frozenset((
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "SYSTEMROOT", "WINDIR",
    "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "XDG_CONFIG_HOME",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO",
    "SSH_AUTH_SOCK",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
))
# A caller such as the identity checker may forward the Git location and
# identity settings of the command it inspects. Config pairs are limited to
# identity keys, so a forwarded pair cannot override SAFE_CONFIG.
GIT_CONTEXT_NAMES = frozenset((
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
))
GIT_CONTEXT_IDENTITY_KEYS = frozenset((
    "user.name", "user.email", "user.useconfigonly",
    "author.name", "author.email", "committer.name", "committer.email",
))
MAX_GIT_CONTEXT_PAIRS = 64
REMOTE_NAME_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def is_managed_proxy_placeholder(value: str) -> bool:
    """Return whether a proxy value is the managed Codex placeholder."""
    candidate = value.strip()
    try:
        parsed = urllib.parse.urlsplit(candidate if "://" in candidate
                                       else "//" + candidate)
        port = parsed.port
    except ValueError:
        return False
    return parsed.hostname == MANAGED_PROXY_HOST and port == MANAGED_PROXY_PORT


def sanitize_managed_proxy(environment: dict) -> None:
    """Remove only managed proxy placeholders from an environment."""
    for variable in PROXY_VARIABLES:
        value = environment.get(variable)
        if value and is_managed_proxy_placeholder(value):
            environment.pop(variable, None)


def _is_inside(path: Path, directory: Path) -> bool:
    """Return whether `path` is within `directory`."""
    try:
        path_value = os.path.normcase(os.path.abspath(path))
        directory_value = os.path.normcase(os.path.abspath(directory))
        return os.path.commonpath((path_value, directory_value)) == directory_value
    except (ValueError, OSError):
        return False


def _candidate_names() -> tuple[str, ...]:
    """Return executable names accepted for this platform."""
    if os.name == "nt":
        os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
        return ("git.exe", "git.com")
    return ("git",)


def resolve_git(repo_root) -> str:
    """Return an absolute Git executable outside `repo_root`."""
    repository = Path(repo_root).resolve()
    names = _candidate_names()
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(raw_directory.strip('"'))
        if not directory.is_absolute():
            continue
        for name in names:
            candidate = directory / name
            if _is_inside(Path(os.path.abspath(candidate)), repository):
                continue
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if _is_inside(resolved, repository):
                continue
            if os.name != "nt" and not os.access(resolved, os.X_OK):
                continue
            return str(resolved)
    raise FileNotFoundError("trusted Git executable was not found on PATH")


def _safe_directory(repository: Path, executable: Path) -> Path:
    """Return an existing execution directory outside the repository."""
    for candidate in (Path(tempfile.gettempdir()), executable.parent):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() and not _is_inside(resolved, repository):
            return resolved
    raise OSError("no safe external directory is available for Git execution")


def _safe_search_path(repository: Path) -> str:
    """Return PATH without empty, relative, or repository-controlled entries."""
    safe_entries = []
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(raw_directory.strip('"'))
        if not directory.is_absolute():
            continue
        try:
            resolved = directory.resolve(strict=False)
        except OSError:
            continue
        if not _is_inside(resolved, repository):
            safe_entries.append(str(resolved))
    return os.pathsep.join(safe_entries)


def _workspace_root(start: Path) -> Path | None:
    """Find the nearest repository root without invoking Git."""
    current = start.resolve()
    while True:
        dot_git = current / ".git"
        if dot_git.is_dir() or dot_git.is_file():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _child_environment(source) -> dict:
    """Return the allowlisted subset of an environment for a Git child."""
    if os.name == "nt":
        # Windows names are case-insensitive. Uppercase keys keep one entry per name.
        allowed = {name.upper() for name in CHILD_ENVIRONMENT}
        return {name.upper(): value for name, value in source.items()
                if name.upper() in allowed}
    return {name: value for name, value in source.items() if name in CHILD_ENVIRONMENT}


def _context_pairs(context: dict) -> list:
    """Return the identity config pairs from a GIT_CONFIG_* triple set."""
    try:
        count = int(context.get("GIT_CONFIG_COUNT", "0"))
    except (TypeError, ValueError):
        return []
    pairs = []
    for index in range(min(max(count, 0), MAX_GIT_CONTEXT_PAIRS)):
        key = context.get(f"GIT_CONFIG_KEY_{index}")
        value = context.get(f"GIT_CONFIG_VALUE_{index}")
        if (isinstance(key, str) and isinstance(value, str) and "\0" not in value
                and key.casefold() in GIT_CONTEXT_IDENTITY_KEYS):
            pairs.append((key, value))
    return pairs


def _context_environment(context: dict) -> dict:
    """Return the accepted subset of a caller-supplied Git context."""
    environment = {
        name: value for name, value in context.items()
        if name in GIT_CONTEXT_NAMES and isinstance(value, str) and value
        and "\0" not in value
    }
    pairs = _context_pairs(context)
    if pairs:
        environment["GIT_CONFIG_COUNT"] = str(len(pairs))
        for index, (key, value) in enumerate(pairs):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


def run_git(
    repo_root, arguments: list[str], *, input_text=None, check=False,
    runner=None, timeout=None, git_context=None,
):
    """Run trusted Git against `repo_root` from an external directory."""
    repository = Path(repo_root).resolve()
    executable = Path(resolve_git(repository))
    environment = _child_environment(os.environ)
    if git_context:
        environment.update(_context_environment(git_context))
    environment["GIT_PAGER"] = ""
    environment["PAGER"] = ""
    environment["GIT_ATTR_NOSYSTEM"] = "1"
    environment["GIT_NO_LAZY_FETCH"] = "1"
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_PROTOCOL_FROM_USER"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["PATH"] = _safe_search_path(repository)
    sanitize_managed_proxy(environment)
    if os.name == "nt":
        environment["NoDefaultCurrentDirectoryInExePath"] = "1"
    command = [
        str(executable),
        "-C",
        str(repository),
        "--no-pager",
        "--no-replace-objects",
        *SAFE_CONFIG,
        *arguments,
    ]
    execute = runner or subprocess.run
    return execute(
        command,
        cwd=_safe_directory(repository, executable),
        env=environment,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
        timeout=timeout,
    )


def _literal(value: str) -> bool:
    """Return whether a CLI value has no shell expansion markers."""
    return bool(value and not value.startswith("-")
                and not any(marker in value for marker in AMBIGUOUS_MARKERS))


def _github_source(value: str) -> bool:
    """Return whether a clone or fetch source names GitHub safely."""
    if not _literal(value):
        return False
    if value.startswith("git@"):
        ssh_path = value.removeprefix("git@github.com:")
        return (value.startswith("git@github.com:") and bool(ssh_path)
                and "?" not in ssh_path and "#" not in ssh_path)
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    return (parsed.scheme == "https" and hostname in GITHUB_HOSTS
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and bool(parsed.path))


def _workspace_path(
    workspace: Path, value: str, *, must_exist: bool, allow_root: bool = False,
) -> Path | None:
    """Resolve a literal path inside the current workspace."""
    if not _literal(value):
        return None
    root = workspace.resolve()
    candidate = (root / value).resolve()
    if not _is_inside(candidate, root) or (candidate == root and not allow_root):
        return None
    if must_exist and not candidate.is_dir():
        return None
    if not must_exist and candidate.exists():
        return None
    # The returned path must name the checked location. A symlink inside the
    # workspace makes the literal path and the resolved path diverge.
    literal = Path(os.path.abspath(workspace / value))
    if literal != Path(os.path.abspath(workspace)) / candidate.relative_to(root):
        return None
    return literal


def _fetch_remote(value: str) -> bool:
    """Return whether a fetch remote is a GitHub URL or a bare remote name."""
    if _github_source(value):
        return True
    return (bool(value) and value[0].isascii() and value[0].isalnum()
            and all(character in REMOTE_NAME_CHARACTERS for character in value))


def _transport_arguments(workspace: Path, arguments: list[str]) -> list[str] | None:
    """Validate the fixed clone and fetch CLI surface."""
    if not arguments or arguments[0] not in {"clone", "fetch"}:
        return None
    operation = arguments[0]
    if operation == "clone":
        if len(arguments) != 3 or not _github_source(arguments[1]):
            return None
        destination = _workspace_path(workspace, arguments[2], must_exist=False)
        if destination is None:
            return None
        return ["clone", "--", arguments[1], str(destination)]
    if len(arguments) < 2:
        return None
    repository = _workspace_path(
        workspace, arguments[1], must_exist=True, allow_root=True,
    )
    if repository is None or not (repository / ".git").exists():
        return None
    remaining = arguments[2:]
    if any(not _literal(value) for value in remaining):
        return None
    if remaining and not _fetch_remote(remaining[0]):
        return None
    return ["-C", str(repository), "fetch", *remaining]


def main(argv: list[str] | None = None) -> int:
    """Run one validated GitHub clone or fetch operation."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    workspace = _workspace_root(Path.cwd())
    if workspace is None:
        print("trusted Git must run inside a repository", file=sys.stderr)
        return 2
    command = _transport_arguments(workspace, arguments)
    if command is None:
        print("usage: trusted_git.py clone <github-url> <new-directory>",
              file=sys.stderr)
        print("       trusted_git.py fetch <repository-directory> [refspec... ]",
              file=sys.stderr)
        return 2
    result = run_git(workspace, command)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
