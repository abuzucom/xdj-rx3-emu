#!/usr/bin/env python3
"""Resolve GitHub CLI outside the repository and return bounded account data."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

# The proxy constants stay importable from this module for existing callers.
try:
    from scripts.trusted_git import (
        MANAGED_PROXY_HOST, MANAGED_PROXY_PORT, PROXY_VARIABLES,
        is_managed_proxy_placeholder, sanitize_managed_proxy,
    )
except ModuleNotFoundError:
    from trusted_git import (
        MANAGED_PROXY_HOST, MANAGED_PROXY_PORT, PROXY_VARIABLES,
        is_managed_proxy_placeholder, sanitize_managed_proxy,
    )


ACCOUNT_OUTPUT_LIMIT = 256
COMMAND_OUTPUT_LIMIT = 1024 * 1024
METADATA_OUTPUT_LIMIT = 65536
GH_TIMEOUT_SECONDS = 5
# GitHub CLI exits with 4 when a command requires authentication.
GH_AUTH_REQUIRED_EXIT = 4
AUTHENTICATION_FAILURE = re.compile(r"HTTP 401\b")
INSTALLATION_TOKEN_FAILURE = "Resource not accessible by integration"
INSTALLATION_TOKEN_STATUS = "HTTP 403"
NETWORK_FAILURE = re.compile(
    r"proxyconnect|connection refused|dial tcp|no such host|i/o timeout"
    r"|TLS handshake",
    re.IGNORECASE,
)
LOGIN = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
TEXT_OPTIONS = frozenset(("--body", "--title"))
REPOSITORY_COMMANDS = frozenset(("pr", "issue", "run"))
REPOSITORY_NAME = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?\Z")
BRANCH_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")
GLOBAL_VALUE_OPTIONS = frozenset(("-R", "--repo", "--hostname"))
# gh caches under $TMPDIR/gh-cli-cache. A shared temp root lets the first
# account or sandbox own that folder and deny every later caller, so each
# call gets a private directory. Go reads TMPDIR on POSIX and TMP, then
# TEMP, on Windows.
PRIVATE_TEMP_PREFIX = "trusted-gh-"
TEMP_VARIABLES = ("TMPDIR", "TMP", "TEMP")
MAX_REPORTED_PATH = 200


def find_literal_escape_sequences(arguments: list[str]) -> list[str]:
    """Return prose options containing escape text instead of real newlines."""
    findings = []
    for index, argument in enumerate(arguments[:-1]):
        if argument in TEXT_OPTIONS and "\\n" in arguments[index + 1]:
            findings.append(argument)
    return findings


def _find_git_entry(start: Path) -> Path:
    """Return the nearest .git entry or raise a bounded error."""
    current = start.resolve()
    for _ in range(100):
        candidate = current / ".git"
        if candidate.is_dir() or candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    raise ValueError("repository context is missing; run from a Git checkout")


def _git_config_path(git_entry: Path) -> Path:
    """Return a local Git config path for a directory or worktree pointer."""
    if git_entry.is_dir():
        return git_entry / "config"
    if git_entry.stat().st_size > METADATA_OUTPUT_LIMIT:
        raise ValueError("repository context Git pointer exceeds the safety limit")
    content = git_entry.read_text(encoding="utf-8", errors="strict")
    marker, separator, value = content.strip().partition(":")
    if marker.strip().lower() != "gitdir" or not separator or not value.strip():
        raise ValueError("repository context has an invalid Git worktree pointer")
    git_dir = (git_entry.parent / value.strip()).resolve()
    if not git_dir.is_dir():
        raise ValueError("repository context has a missing Git worktree directory")
    common_file = git_dir / "commondir"
    if common_file.is_file():
        if common_file.stat().st_size > METADATA_OUTPUT_LIMIT:
            raise ValueError("repository context common pointer exceeds the safety limit")
        common_value = common_file.read_text(encoding="utf-8", errors="strict").strip()
        if not common_value or "\n" in common_value or "\r" in common_value:
            raise ValueError("repository context has an invalid common Git directory")
        common_dir = (git_dir / common_value).resolve()
        if not common_dir.is_dir():
            raise ValueError("repository context has a missing common Git directory")
        return common_dir / "config"
    return git_dir / "config"


def _git_dir(git_entry: Path) -> Path:
    """Return the resolved administrative Git directory."""
    if git_entry.is_dir():
        return git_entry
    if git_entry.stat().st_size > METADATA_OUTPUT_LIMIT:
        raise ValueError("repository context Git pointer exceeds the safety limit")
    content = git_entry.read_text(encoding="utf-8", errors="strict")
    marker, separator, value = content.strip().partition(":")
    if marker.strip().lower() != "gitdir" or not separator or not value.strip():
        raise ValueError("repository context has an invalid Git worktree pointer")
    git_dir = (git_entry.parent / value.strip()).resolve()
    if not git_dir.is_dir():
        raise ValueError("repository context has a missing Git worktree directory")
    return git_dir


def _origin_url(config_path: Path) -> str:
    """Read the origin URL from a bounded local Git config."""
    if config_path.stat().st_size > METADATA_OUTPUT_LIMIT:
        raise ValueError("repository context Git config exceeds the safety limit")
    section = ""
    origin = ""
    for line in config_path.read_text(encoding="utf-8", errors="strict").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.lower()
            continue
        key, separator, value = stripped.partition("=")
        if separator and section == '[remote "origin"]' and key.strip().lower() == "url":
            origin = value.strip()
    if not origin:
        raise ValueError("repository context has no origin remote")
    return origin


def _repository_from_origin(origin: str) -> str:
    """Return a validated GitHub OWNER/REPOSITORY target."""
    if any(character in origin for character in "\r\n\x00"):
        raise ValueError("repository context contains unsafe origin metadata")
    value = origin.strip()
    if value.startswith("https://") or value.startswith("ssh://"):
        parsed = urllib.parse.urlsplit(value)
        if (parsed.hostname != "github.com" or parsed.password
                or parsed.username not in (None, "git")):
            raise ValueError("repository origin is not a safe GitHub remote")
        path = parsed.path.lstrip("/")
    elif value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
    else:
        raise ValueError("repository origin is not a supported GitHub remote")
    path = path.removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2 or not all(REPOSITORY_NAME.fullmatch(part) for part in parts):
        raise ValueError("repository origin has an invalid owner or repository")
    return "/".join(parts)


def repository_target(start: Path) -> str:
    """Return the validated GitHub target for the checkout containing start."""
    return _repository_from_origin(_origin_url(_git_config_path(_find_git_entry(start))))


def repository_branch(start: Path) -> str:
    """Return the validated current branch from local Git metadata."""
    head_path = _git_dir(_find_git_entry(start)) / "HEAD"
    if head_path.stat().st_size > METADATA_OUTPUT_LIMIT:
        raise ValueError("repository HEAD exceeds the safety limit")
    content = head_path.read_text(encoding="utf-8", errors="strict").strip()
    marker = "ref: refs/heads/"
    if not content.startswith(marker):
        raise ValueError("repository has no named branch; check out a branch first")
    branch = content.removeprefix(marker)
    components = branch.split("/")
    if (not BRANCH_NAME.fullmatch(branch) or any(component in ("", ".", "..")
                                                for component in components)):
        raise ValueError("repository has an invalid current branch")
    return branch


def _has_repository_option(arguments: list[str]) -> bool:
    """Return whether arguments contain a structural repository option."""
    for index, argument in enumerate(arguments):
        if argument in ("-R", "--repo") and index + 1 < len(arguments):
            return True
        if argument.startswith("--repo=") or argument.startswith("-R") and len(argument) > 2:
            return True
    return False


def _command_position(arguments: list[str]) -> int:
    """Return the first GitHub command token after global options."""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return index + 1
        if argument in GLOBAL_VALUE_OPTIONS:
            index += 2
            continue
        if argument.startswith("--") and "=" in argument:
            index += 1
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return index
    return len(arguments)


def with_repository_context(repository: Path, arguments: list[str]) -> list[str]:
    """Add validated repository context to a repository-bound command."""
    position = _command_position(arguments)
    if (position >= len(arguments) or arguments[position] not in REPOSITORY_COMMANDS
            or _has_repository_option(arguments)):
        return list(arguments)
    target = repository_target(repository)
    delimiter = arguments.index("--") if "--" in arguments else len(arguments)
    context = [*arguments[:delimiter], "--repo", target, *arguments[delimiter:]]
    command = arguments[position:position + 2]
    has_head = any(argument == "--head" or argument.startswith("--head=")
                   for argument in arguments)
    if command == ["pr", "create"] and not has_head:
        context[delimiter:delimiter] = (
            "--head", f"{target.split('/')[0]}:{repository_branch(repository)}"
        )
    return context


def _is_inside(path: Path, directory: Path) -> bool:
    """Return whether one path resides under a directory."""
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _candidate_names() -> tuple[str, ...]:
    """Return accepted GitHub CLI executable names."""
    if os.name == "nt":
        os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
        return ("gh.exe", "gh.com")
    return ("gh",)


def resolve_gh(repo_root) -> str:
    """Return an absolute GitHub CLI executable outside the repository."""
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
    raise FileNotFoundError("trusted GitHub CLI executable was not found on PATH")


def _safe_directory(repository: Path, executable: Path) -> Path:
    """Return an external execution directory."""
    for candidate in (Path(tempfile.gettempdir()), executable.parent):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() and not _is_inside(resolved, repository):
            return resolved
    raise OSError("no safe external directory is available for GitHub CLI")


def _safe_search_path(repository: Path) -> str:
    """Return PATH without relative or repository-controlled entries."""
    entries = []
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(raw_directory.strip('"')) if raw_directory else Path()
        if not raw_directory or not directory.is_absolute():
            continue
        try:
            resolved = directory.resolve(strict=False)
        except OSError:
            continue
        if not _is_inside(resolved, repository):
            entries.append(str(resolved))
    return os.pathsep.join(entries)


def _is_managed_proxy_placeholder(value: str) -> bool:
    """Return whether a proxy value is the managed Codex placeholder."""
    return is_managed_proxy_placeholder(value)


def _sanitize_proxy_environment(environment: dict) -> None:
    """Remove only managed proxy placeholders from an environment."""
    sanitize_managed_proxy(environment)


def proxy_variable_names(environment: dict) -> list[str]:
    """Return proxy variable names that survive placeholder removal."""
    remaining = dict(environment)
    sanitize_managed_proxy(remaining)
    return [name for name in PROXY_VARIABLES if remaining.get(name)]


class GitHubAccessError(OSError):
    """A GitHub CLI account check failed with a diagnosed category."""

    MESSAGES = {
        "authentication": "GitHub CLI has no authenticated account; "
                          "an active human must sign in",
        "network": "GitHub CLI could not reach GitHub; this is not an "
                   "authentication result",
        "installation_token": "GitHub CLI holds an app installation token, which "
                              "cannot read /user; run this under a user account",
        "unclassified": "GitHub CLI account check failed with exit {returncode}; "
                        "this does not prove missing authentication",
    }

    def __init__(self, category: str, returncode: int):
        super().__init__(self.MESSAGES[category].format(returncode=returncode))
        self.category = category
        self.returncode = returncode


def classify_gh_failure(returncode: int, stderr: str) -> str:
    """Return the failure category for a nonzero GitHub CLI result."""
    if returncode == GH_AUTH_REQUIRED_EXIT or AUTHENTICATION_FAILURE.search(stderr):
        return "authentication"
    if INSTALLATION_TOKEN_STATUS in stderr and INSTALLATION_TOKEN_FAILURE in stderr:
        return "installation_token"
    if NETWORK_FAILURE.search(stderr):
        return "network"
    return "unclassified"


def describe_access_error(error: GitHubAccessError) -> str:
    """Return a user-facing message that names proxy variables, not values."""
    message = str(error)
    if error.category != "network":
        return message
    names = proxy_variable_names(dict(os.environ))
    listed = ", ".join(names) if names else "none set"
    return f"{message}; inspect proxy variables: {listed}"


def run_gh(repo_root, arguments: list[str], *, runner=None, timeout=None):
    """Run trusted GitHub CLI from outside the repository."""
    repository = Path(repo_root).resolve()
    executable = Path(resolve_gh(repository))
    environment = dict(os.environ)
    environment.pop("GH_CONFIG_DIR", None)
    environment.pop("GH_REPO", None)
    _sanitize_proxy_environment(environment)
    environment.update({"GH_PAGER": "", "GH_PROMPT_DISABLED": "1"})
    environment["PATH"] = _safe_search_path(repository)
    if os.name == "nt":
        environment["NoDefaultCurrentDirectoryInExePath"] = "1"
    execute = runner or subprocess.run
    private = tempfile.mkdtemp(
        prefix=PRIVATE_TEMP_PREFIX, dir=_safe_directory(repository, executable))
    try:
        for name in TEMP_VARIABLES:
            environment[name] = private
        return execute(
            [str(executable), *arguments],
            cwd=private,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    finally:
        _remove_private_directory(private)


def _remove_private_directory(path: str) -> None:
    """Remove one private gh temp directory, warning when removal fails."""
    try:
        shutil.rmtree(path)
    except OSError as error:
        print(f"warning: could not remove GitHub CLI temp directory "
              f"{_bounded_path(path)} ({type(error).__name__}); remove it manually",
              file=sys.stderr)


def _bounded_path(path: object) -> str:
    """Return a path as bounded printable ASCII for diagnostics."""
    return ascii(str(path))[1:-1][:MAX_REPORTED_PATH]


def describe_permission_error(error: PermissionError) -> str:
    """Return a message naming the unwritable path and the recovery step."""
    location = _bounded_path(error.filename) if error.filename else "the temp directory"
    return (f"error: GitHub CLI temporary storage is not writable ({location}); "
            "set TMPDIR to a writable directory and retry")


def parse_account(output: str) -> dict:
    """Parse a bounded numeric ID and GitHub login."""
    if len(output) > ACCOUNT_OUTPUT_LIMIT:
        raise ValueError("GitHub account output exceeds the bound")
    fields = output.strip().split("\t")
    if len(fields) != 2 or not fields[0].isdigit() or int(fields[0]) < 1:
        raise ValueError("GitHub account output has an invalid account ID")
    if not LOGIN.fullmatch(fields[1]):
        raise ValueError("GitHub account output has an invalid login")
    return {"id": int(fields[0]), "login": fields[1]}


def authenticated_account(repo_root) -> dict:
    """Return the authenticated GitHub account through a fixed API request."""
    result = run_gh(
        repo_root,
        ["api", "user", "--jq", "[.id,.login]|@tsv"],
        timeout=GH_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        category = classify_gh_failure(result.returncode, result.stderr or "")
        raise GitHubAccessError(category, result.returncode)
    return parse_account(result.stdout)


def verify_account(repo_root) -> None:
    """Verify the authenticated account, allowing only a CI installation token.

    A GitHub Actions `GITHUB_TOKEN` is an app installation token and cannot
    read /user. Only that failure, only on an Actions runner, skips the check.
    """
    try:
        authenticated_account(repo_root)
    except GitHubAccessError as error:
        if error.category == "installation_token" and os.environ.get("GITHUB_ACTIONS") == "true":
            return
        raise


def _run_requested_command(repo_root, arguments: list[str]) -> int:
    """Run one authenticated GitHub CLI command with bounded output."""
    if not arguments:
        print("error: run requires GitHub CLI arguments", file=sys.stderr)
        return 2
    escape_options = find_literal_escape_sequences(arguments)
    if escape_options:
        print(
            "error: an argument contains literal escape text; use real newlines "
            "or --body-file",
            file=sys.stderr,
        )
        return 2
    hooks_directory = Path(__file__).resolve().parent.parent / "hooks"
    sys.path.insert(0, str(hooks_directory))
    try:
        import _gate_core as gate_core
    except ImportError as error:
        print("error: GitHub safety policy is unavailable; repair adoption", file=sys.stderr)
        return 2
    decision, reason = gate_core.forge_verdict("gh", arguments)
    if decision == "deny":
        print("error: GitHub command denied by policy; review the command", file=sys.stderr)
        return 2
    try:
        effective_arguments = with_repository_context(Path(repo_root), arguments)
        decision, reason = gate_core.forge_verdict("gh", effective_arguments)
        if decision == "deny":
            print("error: GitHub command denied by policy; review the command", file=sys.stderr)
            return 2
        verify_account(repo_root)
        result = run_gh(repo_root, effective_arguments)
    except subprocess.TimeoutExpired:
        print("error: GitHub CLI timed out; verify connectivity and retry", file=sys.stderr)
        return 1
    except ValueError:
        print("error: GitHub CLI input or repository metadata is invalid; inspect and retry",
              file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("error: GitHub CLI or repository metadata is unavailable; inspect installation",
              file=sys.stderr)
        return 1
    except GitHubAccessError as error:
        print(f"error: {describe_access_error(error)}", file=sys.stderr)
        return 1
    except PermissionError as error:
        print(describe_permission_error(error), file=sys.stderr)
        return 1
    except OSError:
        print("error: GitHub CLI execution failed; inspect connectivity and repository context",
              file=sys.stderr)
        return 1
    sys.stdout.write(result.stdout[:COMMAND_OUTPUT_LIMIT])
    sys.stderr.write(result.stderr[:COMMAND_OUTPUT_LIMIT])
    return result.returncode


def main() -> int:
    """Print bounded authenticated account metadata as JSON."""
    if len(sys.argv) > 1:
        if sys.argv[1] != "run":
            print("error: expected 'run' or no arguments", file=sys.stderr)
            return 2
        return _run_requested_command(os.getcwd(), sys.argv[2:])
    try:
        account = authenticated_account(os.getcwd())
    except subprocess.TimeoutExpired:
        print("error: GitHub CLI timed out; verify connectivity and retry", file=sys.stderr)
        return 1
    except ValueError:
        print("error: GitHub account metadata is invalid; inspect authentication", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("error: GitHub CLI is unavailable; inspect installation", file=sys.stderr)
        return 1
    except GitHubAccessError as error:
        print(f"error: {describe_access_error(error)}", file=sys.stderr)
        return 1
    except PermissionError as error:
        print(describe_permission_error(error), file=sys.stderr)
        return 1
    except OSError:
        print("error: GitHub authentication failed; inspect connectivity and account state",
              file=sys.stderr)
        return 1
    print(json.dumps(account, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
