#!/usr/bin/env python3
"""Enforce a configured, allowed git identity on commits.

A portable, path-generic checker: copy this file into any repo and use it
as a pre-commit hook, a `pull_request` CI step, or a Claude Code hook
through hooks/enforce_git_identity.py. Three modes:

  no flags        the identity the next commit would use, read from config
  --unpushed      commits on HEAD absent from every remote-tracking ref
  --base/--head   commits in a range, for CI on a pull request

The default mode is the reliable one. With `user.name` or `user.email`
unset, git builds an identity from the account name and hostname, prints
its automatic-identity warning, and commits anyway. This mode fails first,
before the guess reaches a commit object.

Limitation: the commit modes cannot recover that signal, because a commit
object records no mark saying its author field was built rather than
configured. They apply the allowlist only.

The default allowlist accepts GitHub noreply addresses, which link a commit
to its account and publish no private address. The committer may also be
`noreply@github.com`, which is what GitHub itself sets on squash merges.
Override with --allow for a repo that commits under another convention.

Blocking: exits 1 on any violation, 2 on a usage error.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from scripts.trusted_git import resolve_git, run_git
except ModuleNotFoundError:
    try:
        from trusted_git import resolve_git, run_git
    except ModuleNotFoundError:
        resolve_git = None
        run_git = None

try:
    from scripts.trusted_gh import authenticated_account
except ModuleNotFoundError:
    try:
        from trusted_gh import authenticated_account
    except ModuleNotFoundError:
        authenticated_account = None

NOREPLY = re.compile(
    r"\A(?:[0-9]+\+)?[A-Za-z0-9-]+(?:\[bot\])?@users\.noreply\.github\.com\Z",
    re.IGNORECASE,
)
NOREPLY_LOGIN = re.compile(
    r"\A(?:[0-9]+\+)?(?P<login>[A-Za-z0-9-]+)@users\.noreply\.github\.com\Z",
    re.IGNORECASE,
)
GITHUB_COMMITTER = "noreply@github.com"

# Git treats each of these as an explicitly given identity, in this order.
# A checker that read only user.email would block harnesses and CI systems
# that supply one through the environment instead.
AUTHOR_NAME_SOURCES = (
    ("env", "GIT_AUTHOR_NAME"),
    ("config", "user.name"),
)
COMMITTER_NAME_SOURCES = (
    ("env", "GIT_COMMITTER_NAME"),
    ("config", "user.name"),
)
AUTHOR_EMAIL_SOURCES = (
    ("env", "GIT_AUTHOR_EMAIL"),
    ("config", "user.email"),
    ("env", "EMAIL"),
)
COMMITTER_EMAIL_SOURCES = (
    ("env", "GIT_COMMITTER_EMAIL"),
    ("config", "user.email"),
    ("env", "EMAIL"),
)

HISTORY_CANDIDATE_LIMIT = 5
HISTORY_COMMIT_LIMIT = 50
GIT_CONTEXT_VARIABLES = frozenset((
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
))
# Only identity settings travel as context. A forwarded core.fsmonitor or
# core.sshCommand would make Git run another program.
# trusted_git.GIT_CONTEXT_IDENTITY_KEYS holds the same set.
GIT_IDENTITY_CONFIG_KEYS = frozenset((
    "user.name", "user.email", "user.useconfigonly",
    "author.name", "author.email", "committer.name", "committer.email",
))
# A standalone copy cannot import trusted_git. Keep this set equal to
# trusted_git.CHILD_ENVIRONMENT.
STANDALONE_CHILD_ENVIRONMENT = frozenset((
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
CONFIG_PARAMETERS_ERROR = (
    "GIT_CONFIG_PARAMETERS is malformed. Rerun the command without -c identity overrides."
)
CONFIG_COUNT_ERROR = "GIT_CONFIG_COUNT names a missing or invalid config pair"
BARE_CONFIG_VALUE = "true"

FIX_MESSAGE = (
    "fix: derive or list identity candidates, then request explicit confirmation:\n"
    "  git config user.name  '<login>'\n"
    "  git config user.email '<id>+<login>@users.noreply.github.com'\n"
    "Set the confirmed identity only in this repository. Never auto-select a\n"
    "history candidate. An authenticated gh is not a git identity."
)
NUMBERED_NOREPLY = re.compile(
    r"\A[0-9]+\+[A-Za-z0-9-]+@users\.noreply\.github\.com\Z",
    re.IGNORECASE,
)


def _standalone_resolve_git(repo) -> str:
    """Resolve trusted Git when this portable checker was copied alone."""
    repository = Path(repo).resolve()
    if os.name == "nt":
        os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
    names = ("git.exe", "git.com") if os.name == "nt" else ("git",)
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(raw_directory.strip('"'))
        if not directory.is_absolute():
            continue
        for name in names:
            candidate = directory / name
            try:
                Path(os.path.abspath(candidate)).relative_to(repository)
                continue
            except ValueError:
                pass
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                executable = candidate.resolve(strict=True)
                executable.relative_to(repository)
                continue
            except ValueError:
                if os.name == "nt" or os.access(executable, os.X_OK):
                    return str(executable)
            except OSError:
                continue
    raise FileNotFoundError("trusted Git executable was not found on PATH")


def _standalone_safe_directory(repository: Path, executable: Path) -> str:
    """Return an external working directory for standalone execution."""
    for candidate in (Path(tempfile.gettempdir()), executable.parent):
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(repository)
        except ValueError:
            if resolved.is_dir():
                return str(resolved)
        except OSError:
            continue
    raise OSError("no safe external directory is available for Git execution")


def _standalone_safe_path(repository: Path) -> str:
    """Remove repository-controlled entries from standalone child PATH."""
    safe_entries = []
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(raw_directory.strip('"'))
        if not directory.is_absolute():
            continue
        try:
            resolved = directory.resolve(strict=False)
            resolved.relative_to(repository)
        except ValueError:
            safe_entries.append(str(resolved))
        except OSError:
            continue
    return os.pathsep.join(safe_entries)


def _standalone_child_environment() -> dict:
    """Return the allowlisted subset of this process environment."""
    if os.name == "nt":
        # Windows names are case-insensitive. Uppercase keys keep one entry per name.
        allowed = {name.upper() for name in STANDALONE_CHILD_ENVIRONMENT}
        return {name.upper(): value for name, value in os.environ.items()
                if name.upper() in allowed}
    return {name: value for name, value in os.environ.items()
            if name in STANDALONE_CHILD_ENVIRONMENT}


def _standalone_run_git(repo, arguments: list[str], *, check=False, runner=None,
                        git_context=None):
    """Run trusted Git for a standalone copy of this checker.

    The child receives allowlisted variables plus the location and identity
    values of `git_context`.
    """
    repository = Path(repo).resolve()
    executable = Path(_standalone_resolve_git(repository))
    environment = _standalone_child_environment()
    environment.update(_git_context(git_context or {}))
    environment.update({"GIT_PAGER": "", "PAGER": "", "GIT_TERMINAL_PROMPT": "0"})
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PROTOCOL_FROM_USER": "0",
            "PATH": _standalone_safe_path(repository),
        }
    )
    if os.name == "nt":
        environment["NoDefaultCurrentDirectoryInExePath"] = "1"
    command = [
        str(executable),
        "-C",
        str(repository),
        "--no-pager",
        "--no-replace-objects",
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
        *arguments,
    ]
    execute = runner or subprocess.run
    return execute(
        command,
        cwd=_standalone_safe_directory(repository, executable),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


if resolve_git is None:
    resolve_git = _standalone_resolve_git
    run_git = _standalone_run_git


def _read_quoted(text: str, index: int) -> tuple:
    """Read one shell-quoted word at `index`. Return it and the next index.

    Git quotes each word in single quotes and writes `'` and `!` as `'\\''`
    and `'\\!'`.
    """
    if not text.startswith("'", index):
        raise ValueError(CONFIG_PARAMETERS_ERROR)
    parts = []
    index += 1
    while True:
        end = text.find("'", index)
        if end < 0:
            raise ValueError(CONFIG_PARAMETERS_ERROR)
        parts.append(text[index:end])
        index = end + 1
        if not (text.startswith(("\\'", "\\!"), index) and text.startswith("'", index + 2)):
            return "".join(parts), index
        parts.append(text[index + 1])
        index += 3


def _parse_config_parameters(text: str) -> list:
    """Return the key and value pairs of a GIT_CONFIG_PARAMETERS value.

    Git writes `-c` settings as `'key'='value'` or as `'key=value'`. A bare
    `'key'` sets a boolean. A malformed value raises ValueError.
    """
    pairs = []
    index = 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        key, index = _read_quoted(text, index)
        if text.startswith("=", index):
            value, index = _read_quoted(text, index + 1)
        elif "=" in key:
            key, value = key.split("=", 1)
        else:
            value = BARE_CONFIG_VALUE
        if index < len(text) and not text[index].isspace():
            raise ValueError(CONFIG_PARAMETERS_ERROR)
        pairs.append((key, value))
    return pairs


def _count_pairs(source) -> list:
    """Return the GIT_CONFIG_KEY_n and GIT_CONFIG_VALUE_n pairs of `source`."""
    raw_count = source.get("GIT_CONFIG_COUNT")
    if raw_count is None:
        return []
    try:
        count = int(raw_count)
    except ValueError:
        raise ValueError(CONFIG_COUNT_ERROR) from None
    if count < 0:
        raise ValueError(CONFIG_COUNT_ERROR)
    pairs = []
    for index in range(count):
        key = source.get(f"GIT_CONFIG_KEY_{index}")
        value = source.get(f"GIT_CONFIG_VALUE_{index}")
        if key is None or value is None:
            raise ValueError(CONFIG_COUNT_ERROR)
        pairs.append((key, value))
    return pairs


def _git_context(source=None) -> dict:
    """Return the Git location and identity settings of the inspected command.

    The identity hook passes the inspected command's context through these
    variables. Git passes `-c` settings to its hooks in
    GIT_CONFIG_PARAMETERS. Trusted Git drops both unless they arrive as
    context. Identity settings are renumbered as GIT_CONFIG_COUNT pairs.
    Parameters follow count pairs because Git applies them last. Only the
    last value of each key is kept.
    """
    source = os.environ if source is None else source
    context = {name: value for name, value in source.items()
               if name in GIT_CONTEXT_VARIABLES}
    pairs = _count_pairs(source)
    pairs.extend(_parse_config_parameters(source.get("GIT_CONFIG_PARAMETERS", "")))
    latest = {}
    for key, value in pairs:
        folded = key.casefold()
        if folded in GIT_IDENTITY_CONFIG_KEYS:
            latest.pop(folded, None)
            latest[folded] = (key, value)
    if latest:
        context["GIT_CONFIG_COUNT"] = str(len(latest))
    for index, (key, value) in enumerate(latest.values()):
        context[f"GIT_CONFIG_KEY_{index}"] = key
        context[f"GIT_CONFIG_VALUE_{index}"] = value
    return context


def _git(repo, arguments: list[str], **options):
    """Run Git with the inspected command's context."""
    return run_git(repo, arguments, git_context=_git_context(), **options)


def _config(key: str, repo=None) -> str:
    """Return a git config value, or an empty string when it is unset."""
    result = _git(
        repo or os.getcwd(),
        ["config", "--get", key],
        runner=subprocess.run,
    )
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(result.returncode, "git config")
    return result.stdout.strip() if result.returncode == 0 else ""


def _first_explicit(sources: tuple, repo=None) -> str:
    """Return the first value git would treat as an explicitly given field."""
    for kind, key in sources:
        value = os.environ.get(key, "").strip() if kind == "env" else _config(key, repo)
        if value:
            return value
    return ""


def worktree_identity(repo=None) -> dict:
    """Return the identity the next commit would use, and what git would guess."""
    repository = repo or os.getcwd()
    result = _git(repository, ["version"], runner=subprocess.run)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, "git version")
    author_name = _first_explicit(AUTHOR_NAME_SOURCES, repository)
    committer_name = _first_explicit(COMMITTER_NAME_SOURCES, repository)
    author_email = _first_explicit(AUTHOR_EMAIL_SOURCES, repository)
    committer_email = _first_explicit(COMMITTER_EMAIL_SOURCES, repository)
    committer_name = committer_name or author_name
    committer_email = committer_email or author_email
    return {
        "label": "worktree",
        "author_email": author_email,
        "committer_email": committer_email,
        "unset_name": not author_name or not committer_name,
        "unset_email": not author_email or not committer_email,
    }


def log_identities(revisions: list, repo=None) -> list:
    """Return one identity record per commit reachable by `revisions`."""
    repository = repo or os.getcwd()
    result = _git(
        repository,
        ["log", "--no-ext-diff", "--format=%H%x00%ae%x00%ce", *revisions],
        check=True,
        runner=subprocess.run,
    )
    identities = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        fields = line.split("\x00")
        if len(fields) != 3 or not re.fullmatch(r"[0-9a-fA-F]{40,64}", fields[0]):
            raise ValueError("git log returned malformed identity metadata")
        sha, author_email, committer_email = fields
        identities.append(
            {
                "label": sha[:12],
                "author_email": author_email,
                "committer_email": committer_email,
                "unset_name": False,
                "unset_email": False,
            }
        )
    return identities


def unpushed_identities(repo=None) -> list:
    """Return identity records for commits on HEAD absent from every remote."""
    repository = repo or os.getcwd()
    result = _git(repository, ["remote"], runner=subprocess.run)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, "git remote")
    if not result.stdout.strip():
        return []
    revisions = ["--not", "--remotes", "--not", "--end-of-options", "HEAD"]
    return log_identities(revisions, repository)


def account_identity_candidate(account: dict, configured_name: str) -> tuple:
    """Derive a repository-local identity from authenticated GitHub metadata."""
    account_id = account.get("id")
    login = account.get("login", "")
    if not isinstance(account_id, int) or account_id < 1:
        raise ValueError("GitHub account ID is invalid")
    if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
        raise ValueError("GitHub account login is invalid")
    name = configured_name.strip() or login
    return name, f"{account_id}+{login}@users.noreply.github.com"


def strict_identity_violations(identities: list, repo=None) -> list[str]:
    """Require every selected identity to match the authenticated operator."""
    repository = repo or os.getcwd()
    if authenticated_account is None:
        return ["trusted GitHub account lookup is unavailable"]
    try:
        account = authenticated_account(repository)
        expected_email = account_identity_candidate(
            account, _config("user.name", repository)
        )[1]
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        return [f"trusted GitHub account lookup failed: {error}"]
    violations = []
    for identity in identities:
        for role in ("author", "committer"):
            email = identity[f"{role}_email"].strip()
            if email.lower() != expected_email.lower():
                violations.append(
                    f"{identity['label']}: {role} email does not match the "
                    "authenticated operator"
                )
    return violations


def history_identity_candidates(repo=None) -> list:
    """Return bounded noreply identity candidates from local commit metadata."""
    repository = repo or os.getcwd()
    result = _git(
        repository,
        ["log", "--no-ext-diff", f"-n{HISTORY_COMMIT_LIMIT}",
         "--format=%an%x00%ae%x00%cn%x00%ce", "--all", "--"],
        check=True,
        runner=subprocess.run,
    )
    candidates = []
    seen = set()
    for line in result.stdout.splitlines():
        fields = line.split("\x00")
        if len(fields) != 4:
            raise ValueError("git log returned malformed identity candidates")
        for name, email in ((fields[0], fields[1]), (fields[2], fields[3])):
            candidate = (name.strip(), email.strip())
            if not candidate[0] or not NUMBERED_NOREPLY.fullmatch(candidate[1]):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)
            if len(candidates) >= HISTORY_CANDIDATE_LIMIT:
                return candidates
    return candidates


def _allowed(email: str, pattern: re.Pattern) -> bool:
    """Return True when `email` matches the allowlist pattern."""
    return bool(pattern.match(email.strip()))


def find_violations(identities: list, pattern: re.Pattern = NOREPLY) -> list:
    """Return one message per unset field or disallowed address."""
    violations = []
    for identity in identities:
        label = identity["label"]
        if identity["unset_email"]:
            violations.append(
                f"{label}: user.email is unset, so git builds one from this "
                "machine's account name and hostname and commits on a warning"
            )
        if identity["unset_name"]:
            violations.append(
                f"{label}: user.name is unset, so git builds one from this "
                "machine's account and commits on a warning"
            )
        if identity["unset_email"] or identity["unset_name"]:
            continue
        author = identity["author_email"]
        if not _allowed(author, pattern):
            violations.append(f"{label}: author email '{author}' is not an allowed address")
        committer = identity["committer_email"]
        if committer.strip().lower() == GITHUB_COMMITTER:
            continue
        if not _allowed(committer, pattern):
            violations.append(
                f"{label}: committer email '{committer}' is not an allowed address"
            )
    return violations


def gh_advisory(email: str, repo=None) -> str:
    """Return a note about the gh-authenticated account. Never blocks."""
    if authenticated_account is None:
        return "note: trusted gh support is unavailable, so the configured account is unverified"
    try:
        account = authenticated_account(repo or os.getcwd())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return "note: gh is unavailable, so the account behind this identity is unverified"
    login = account["login"]
    match = NOREPLY_LOGIN.match(email.strip())
    if not match:
        return (
            f"note: gh is authenticated as '{login}', but the configured address is "
            "not a noreply address for that account"
        )
    if match.group("login").lower() != login.lower():
        return f"note: gh is authenticated as '{login}' but commits would be authored as '{email}'"
    return ""


def bootstrap_identity_advisory(repo=None) -> str:
    """Return one confirmation-first identity proposal or history fallback."""
    repository = repo or os.getcwd()
    if authenticated_account is not None:
        try:
            account = authenticated_account(repository)
            name = _config("user.name", repository)
            candidate = account_identity_candidate(account, name)
            return (
                "identity candidate from authenticated gh for explicit confirmation: "
                f"repository-local name '{candidate[0]}', email '{candidate[1]}'"
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    candidates = history_identity_candidates(repository)
    if not candidates:
        return ("identity candidate unavailable; derive name and email from an "
                "authenticated account source, then request approval")
    rendered = "; ".join(f"'{name}' <{email}>" for name, email in candidates)
    return (
        "local history candidates for explicit confirmation only; never auto-select: "
        f"{rendered}"
    )


def config_only_advisory(repo=None) -> str:
    """Return a note when git may still auto-detect an identity on this machine."""
    if _config("user.useConfigOnly", repo).lower() == "true":
        return ""
    return (
        "note: user.useConfigOnly is not true, so git auto-detects an identity "
        "when user.email is unset. Set it once per machine: "
        "git config --global user.useConfigOnly true"
    )


def select_identities(args: argparse.Namespace) -> list:
    """Return the identity records the requested mode covers."""
    if args.base:
        return log_identities(["--end-of-options", f"{args.base}..{args.head}"], args.repo)
    if args.unpushed:
        return unpushed_identities(args.repo)
    return [worktree_identity(args.repo)]


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="", help="base ref (exclusive); pairs with --head")
    parser.add_argument("--head", default="", help="head ref (inclusive); pairs with --base")
    parser.add_argument("--repo", default=os.getcwd(), help="repository to inspect (default: cwd)")
    parser.add_argument(
        "--unpushed", action="store_true", help="check commits absent from every remote"
    )
    parser.add_argument("--allow", default="", help="regex of allowed emails; overrides the default")
    parser.add_argument(
        "--advise",
        action="store_true",
        help="report the gh account and user.useConfigOnly; never changes the exit code",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="require author and committer emails to match authenticated gh",
    )
    return parser


def _print_advisories(identities: list, repo=None) -> None:
    """Print the non-blocking machine and account notes."""
    email = identities[0]["author_email"] if identities else ""
    notes = [config_only_advisory(repo), gh_advisory(email, repo)]
    if any(identity["unset_name"] or identity["unset_email"]
           for identity in identities):
        notes.append(bootstrap_identity_advisory(repo))
    for note in notes:
        if note:
            print(note)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if bool(args.base) != bool(args.head):
        parser.error("--base and --head must be given together")
    if args.strict and args.base:
        parser.error("--strict cannot validate a commit range")
    if args.strict and args.allow:
        parser.error("--strict cannot be combined with --allow")
    pattern = re.compile(args.allow) if args.allow else NOREPLY

    try:
        identities = select_identities(args)
        if args.advise:
            _print_advisories(identities, args.repo)
    except (subprocess.CalledProcessError, UnicodeError, ValueError) as error:
        print(f"error: git log failed: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"error: git is unavailable: {error}", file=sys.stderr)
        return 1

    violations = find_violations(identities, pattern)
    if args.strict:
        violations.extend(strict_identity_violations(identities, args.repo))
    if violations:
        for message in violations:
            print(message, file=sys.stderr)
        print(FIX_MESSAGE, file=sys.stderr)
        return 1
    if args.base or args.unpushed:
        print(f"no identity violations found in {len(identities)} commit(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
