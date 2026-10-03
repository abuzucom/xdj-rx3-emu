#!/usr/bin/env python3
"""Run trusted compliance checks against one immutable Git tree."""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

MAX_BLOB_SIZE = 50 * 1024 * 1024
MAX_TOTAL_BLOB_BYTES = 256 * 1024 * 1024
MAX_TREE_ENTRIES = 100_000
MAX_VIOLATIONS = 1_000
MAX_TREE_OUTPUT = 64 * 1024 * 1024
MAX_GIT_DIAGNOSTIC = 64 * 1024
MAX_DIAGNOSTIC_LENGTH = 500
OBJECT_ID_PATTERN = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
ACTION_PATTERN = re.compile(r"[^@\s]+@[0-9a-fA-F]{40}\Z")
PYTHON_VERSION_PATTERN = re.compile(r"^3\.\d+(?:\.\d+)?$")
CODE_SUFFIXES = {
    ".c", ".cc", ".cjs", ".cpp", ".cs", ".go", ".java", ".js",
    ".jsx", ".mjs", ".php", ".py", ".pyw", ".rb", ".rs", ".ts",
    ".tsx",
}
TRUSTED_REQUIREMENTS_COMMAND = (
    "python -m pip install --requirement "
    "trusted-base/requirements-checkers.txt"
)
TRUSTED_SCAN_COMMAND = (
    'python "$TRUSTED_CHECKER" --repo "$PR_REPO" --tree "$PR_HEAD_SHA" '
    '--base "$PR_BASE_SHA" --branch "$PR_HEAD_BRANCH" '
    '--pr-author "$PR_AUTHOR"'
)
CHECKOUT_ACTION = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
SETUP_PYTHON_ACTION = (
    "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
)
TRUSTED_ENVIRONMENT = {
    "PR_BASE_REPOSITORY": "${{ github.event.pull_request.base.repo.full_name }}",
    "PR_BASE_SHA": "${{ github.event.pull_request.base.sha }}",
    "PR_HEAD_BRANCH": "${{ github.event.pull_request.head.ref }}",
    "PR_HEAD_REPOSITORY": "${{ github.event.pull_request.head.repo.full_name }}",
    "PR_HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
    "PR_AUTHOR": "${{ github.event.pull_request.user.login }}",
    "PR_REPO": "pr-head",
    "TRUSTED_CHECKER": "trusted-base/scripts/check_compliance_tree.py",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES": (
        "${{ github.workspace }}/trusted-base/.git/objects"
    ),
}
SUPPORTED_ENTRIES = {
    (b"100644", b"blob"),
    (b"100755", b"blob"),
    (b"120000", b"blob"),
}
CHECKER_NAMES = (
    "check_secrets_heuristic",
    "check_persist_credentials",
    "check_weak_hashing",
    "check_dockerfile_root",
    "check_conflict_markers",
    "check_banned_agents",
    "check_commit_attribution",
    "check_commit_message",
    "check_git_identity",
    "check_branch_name",
)
BOT_REGISTRY_NAME = "trusted_bot_identities"


def _sanitize(value: object) -> str:
    """Render untrusted data as bounded printable ASCII."""
    rendered = []
    for character in str(value):
        if " " <= character <= "~":
            rendered.append(character)
        elif ord(character) <= 0xFF:
            rendered.append(f"\\x{ord(character):02x}")
        else:
            rendered.append(f"\\u{ord(character):04x}")
    text = "".join(rendered)
    if len(text) > MAX_DIAGNOSTIC_LENGTH:
        return text[:MAX_DIAGNOSTIC_LENGTH] + "...[truncated]"
    return text


def _load_module(path: Path, name: str) -> ModuleType:
    """Load a module from an exact trusted sibling path."""
    spec = importlib.util.spec_from_file_location(f"_compliance_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load trusted checker {_sanitize(name)}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_trusted(name: str) -> ModuleType:
    """Load one module from the orchestrator's own directory."""
    path = Path(__file__).resolve().parent / f"{name}.py"
    if not path.is_file():
        raise RuntimeError(f"trusted checker is missing: {name}.py")
    return _load_module(path, name)


def _load_checkers() -> dict[str, ModuleType]:
    """Load every checker from the orchestrator's own directory."""
    return {name: _load_trusted(name) for name in CHECKER_NAMES}


def _is_within(path: Path, parent: Path) -> bool:
    """Return True when path resolves to parent or one of its descendants."""
    try:
        return os.path.commonpath((str(path), str(parent))) == str(parent)
    except ValueError:
        return False


def _safe_search_path(repo: Path) -> str:
    """Return PATH without directories controlled by the candidate repo."""
    safe_entries = []
    for raw_entry in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_entry:
            continue
        try:
            entry = Path(raw_entry).resolve(strict=False)
        except OSError:
            continue
        if not _is_within(entry, repo):
            safe_entries.append(str(entry))
    return os.pathsep.join(safe_entries)


def _safe_execution_directory(repo: Path, executable: str) -> Path:
    """Return an existing process directory outside the candidate repo."""
    for candidate in (Path(os.getenv("TEMP", "")), Path(executable).parent):
        if not str(candidate):
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() and not _is_within(resolved, repo):
            return resolved
    raise RuntimeError("no safe external Git execution directory is available")


def _validate_executable(raw_executable: object, repo: Path) -> str:
    """Return an absolute executable path outside the candidate repo."""
    try:
        executable = Path(str(raw_executable)).resolve(strict=True)
    except OSError as error:
        raise RuntimeError(f"could not resolve trusted Git: {_sanitize(error)}") from error
    if not executable.is_file() or _is_within(executable, repo):
        raise RuntimeError("trusted Git is not a file outside the candidate repo")
    if os.name != "nt" and not os.access(executable, os.X_OK):
        raise RuntimeError("trusted Git is not executable")
    return str(executable)


def _resolve_git(repo: Path) -> tuple[str, str]:
    """Resolve Git through trusted_git.py or a candidate-free PATH."""
    scripts_dir = Path(__file__).resolve().parent
    resolver_path = scripts_dir / "trusted_git.py"
    safe_path = _safe_search_path(repo)
    if resolver_path.is_file():
        resolver = _load_module(resolver_path, "trusted_git")
        resolve_git = getattr(resolver, "resolve_git", None)
        if not callable(resolve_git):
            raise RuntimeError("trusted_git.py does not define resolve_git")
        return _validate_executable(resolve_git(repo), repo), safe_path
    executable = shutil.which("git", path=safe_path)
    if executable is None:
        raise RuntimeError("could not find Git outside the candidate repo")
    return _validate_executable(executable, repo), safe_path


def _git_environment(safe_path: str) -> dict[str, str]:
    """Return a noninteractive Git environment with unsafe features disabled."""
    environment = os.environ.copy()
    for name in (
        "GIT_COMMON_DIR", "GIT_CONFIG", "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS", "GIT_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_WORK_TREE",
    ):
        environment.pop(name, None)
    for name in list(environment):
        if name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(name)
    environment.update({
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "PATH": safe_path,
    })
    return environment


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    """Stop and join a Git process that exceeded an output bound."""
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _capture_bounded(
    command: list[str], repo: Path, environment: dict[str, str], limit: int,
) -> tuple[int, bytes]:
    """Capture at most limit bytes from one Git process."""
    process = subprocess.Popen(
        command,
        cwd=_safe_execution_directory(repo, command[0]),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if process.stdout is None:
        _stop_process(process)
        raise RuntimeError("could not capture Git output")
    try:
        output = process.stdout.read(limit + 1)
        if len(output) > limit:
            _stop_process(process)
            raise RuntimeError(f"Git output exceeds limit ({limit} bytes)")
        return_code = process.wait()
    except BaseException:
        if process.poll() is None:
            _stop_process(process)
        raise
    finally:
        process.stdout.close()
    return return_code, output


def _supports_option(
    executable: str, option: str, repo: Path, environment: dict[str, str],
) -> bool:
    """Return True when trusted Git accepts one global safety option."""
    command = [executable, option, "--version"]
    return_code, _ = _capture_bounded(
        command, repo, environment, MAX_GIT_DIAGNOSTIC)
    return return_code == 0


def _git_prefix(
    executable: str, repo: Path, environment: dict[str, str],
) -> list[str]:
    """Build the common trusted Git object-command prefix."""
    prefix = [executable, "-C", str(repo), "--no-pager"]
    if _supports_option(executable, "--no-replace-objects", repo, environment):
        prefix.append("--no-replace-objects")
    if _supports_option(executable, "--no-lazy-fetch", repo, environment):
        prefix.append("--no-lazy-fetch")
    prefix.extend(("-c", "core.fsmonitor="))
    return prefix


def _run_git(
    prefix: list[str], arguments: list[str], repo: Path,
    environment: dict[str, str], limit: int,
) -> bytes:
    """Run one bounded Git command and raise a sanitized-safe error."""
    return_code, output = _capture_bounded(
        prefix + arguments, repo, environment, limit)
    if return_code != 0:
        message = output.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"git {arguments[0]} failed (exit {return_code}): "
            f"{_sanitize(message)}"
        )
    return output


def _validate_object_id(object_id: str) -> str:
    """Return a normalized 40- or 64-character hexadecimal object ID."""
    if not OBJECT_ID_PATTERN.fullmatch(object_id):
        raise RuntimeError("--tree requires 40 or 64 hexadecimal characters")
    return object_id.lower()


def _validate_tree_path(raw_path: bytes) -> str:
    """Decode and validate one repository-relative tree path."""
    try:
        path = raw_path.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("tree entry path is not valid UTF-8") from error
    parts = path.split("/")
    if not path or path.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise RuntimeError(f"malformed tree entry path: {_sanitize(path)}")
    return path


def _parse_tree_entries(raw_output: bytes) -> list[tuple[str, str, str]]:
    """Parse complete ls-tree records into supported tracked blobs."""
    if raw_output and not raw_output.endswith(b"\x00"):
        raise RuntimeError("truncated git ls-tree output")
    records = raw_output[:-1].split(b"\x00") if raw_output else []
    entries = []
    seen = set()
    for record in records:
        if len(entries) >= MAX_TREE_ENTRIES:
            raise RuntimeError(
                f"tree has more than {MAX_TREE_ENTRIES} entries")
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, raw_id = metadata.split(b" ")
            object_id = raw_id.decode("ascii")
        except (ValueError, UnicodeDecodeError) as error:
            raise RuntimeError("malformed git ls-tree entry") from error
        path = _validate_tree_path(raw_path)
        if path in seen or not OBJECT_ID_PATTERN.fullmatch(object_id):
            raise RuntimeError(f"malformed git ls-tree entry: {_sanitize(path)}")
        if (mode, object_type) not in SUPPORTED_ENTRIES:
            raise RuntimeError(f"unsupported git ls-tree entry: {_sanitize(path)}")
        seen.add(path)
        entries.append((path, object_id.lower(), mode.decode("ascii")))
    return entries


def _parse_blob_size(raw_size: bytes, path: str) -> int:
    """Parse and bound one exact cat-file size response."""
    if not re.fullmatch(rb"(?:0|[1-9][0-9]*)\n", raw_size):
        raise RuntimeError(f"malformed blob size for {_sanitize(path)}")
    size = int(raw_size[:-1])
    if size > MAX_BLOB_SIZE:
        raise RuntimeError(
            f"{_sanitize(path)}: blob size ({size} bytes) exceeds "
            f"limit ({MAX_BLOB_SIZE} bytes)"
        )
    return size


def _read_blob(
    prefix: list[str], repo: Path, environment: dict[str, str],
    path: str, object_id: str,
) -> bytes:
    """Read one blob after checking and enforcing its exact size."""
    raw_size = _run_git(
        prefix, ["cat-file", "-s", "--", object_id], repo,
        environment, MAX_GIT_DIAGNOSTIC)
    size = _parse_blob_size(raw_size, path)
    return_code, content = _capture_bounded(
        prefix + ["cat-file", "blob", "--", object_id],
        repo, environment, size)
    if return_code != 0:
        raise RuntimeError(f"git cat-file failed while reading {_sanitize(path)}")
    if len(content) != size:
        raise RuntimeError(
            f"{_sanitize(path)}: blob read {len(content)} bytes; expected {size}"
        )
    return content


def _decode_blob(content: bytes) -> str:
    """Decode BOM-aware Unicode, using Latin-1 as a one-to-one fallback."""
    encodings = (
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    )
    for marker, encoding in encodings:
        if not content.startswith(marker):
            continue
        try:
            return content.decode(encoding).lstrip("\ufeff")
        except UnicodeDecodeError:
            return content.decode("latin-1")
    inferred = _decode_bomless_unicode(content)
    if inferred is not None:
        return inferred
    try:
        return content.decode("utf-8").lstrip("\ufeff")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def _zero_ratio(content: bytes, offset: int, width: int) -> float:
    """Return the NUL ratio at one byte position in fixed-width units."""
    sample = content[offset::width]
    return sample.count(0) / len(sample) if sample else 0.0


def _decode_bomless_unicode(content: bytes) -> str | None:
    """Decode UTF-16/32 text whose byte-position NUL pattern identifies it."""
    candidates = []
    if len(content) % 4 == 0 and len(content) >= 4:
        ratios = [_zero_ratio(content, offset, 4) for offset in range(4)]
        if min(ratios[1:]) > 0.6 and ratios[0] < 0.2:
            candidates.append("utf-32-le")
        if min(ratios[:3]) > 0.6 and ratios[3] < 0.2:
            candidates.append("utf-32-be")
    if len(content) % 2 == 0 and len(content) >= 2:
        even = _zero_ratio(content, 0, 2)
        odd = _zero_ratio(content, 1, 2)
        if odd > 0.4 and even < 0.2:
            candidates.append("utf-16-le")
        if even > 0.4 and odd < 0.2:
            candidates.append("utf-16-be")
    for encoding in candidates:
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _walk_values(root: object):
    """Yield nested YAML values once, including alias graphs."""
    stack = [root]
    visited = set()
    while stack:
        value = stack.pop()
        if isinstance(value, (dict, list)):
            identity = id(value)
            if identity in visited:
                continue
            visited.add(identity)
        yield value
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def _action_violations(document: object, path: str) -> list[str]:
    """Require immutable revisions on every external workflow action."""
    violations = []
    for value in _walk_values(document):
        if not isinstance(value, dict) or "uses" not in value:
            continue
        reference = value["uses"]
        if not isinstance(reference, str):
            violations.append(f"{path}: action reference must be text")
        elif not reference.startswith("./") and not ACTION_PATTERN.fullmatch(reference):
            violations.append(
                f"{path}: action '{reference}' must use a full commit SHA")
    return violations


def _pull_target_trigger(document: dict) -> bool:
    """Return whether a workflow declares pull_request_target."""
    trigger = document.get("on", document.get(True))
    if isinstance(trigger, str):
        return trigger == "pull_request_target"
    if isinstance(trigger, list):
        return "pull_request_target" in trigger
    return isinstance(trigger, dict) and "pull_request_target" in trigger


def _checkout_step(base: bool) -> dict:
    """Return one exact trusted checkout step schema."""
    role = "trusted base" if base else "pull request head"
    prefix = "BASE" if base else "HEAD"
    return {
        "name": f"Check out the exact {role}",
        "uses": CHECKOUT_ACTION,
        "with": {
            "repository": f"${{{{ env.PR_{prefix}_REPOSITORY }}}}",
            "ref": f"${{{{ env.PR_{prefix}_SHA }}}}",
            "path": "trusted-base" if base else "pr-head",
            "persist-credentials": False,
            "fetch-depth": 201,
        },
    }


def _trusted_steps() -> list[dict]:
    """Return the exact privileged step sequence allowed for promotion."""
    return [
        _checkout_step(True),
        _checkout_step(False),
        {
            "name": "Set up Python",
            "uses": SETUP_PYTHON_ACTION,
            "with": {"python-version": "3.x"},
        },
        {
            "name": "Install trusted checker dependencies",
            "run": TRUSTED_REQUIREMENTS_COMMAND,
        },
        {
            "name": "Scan immutable pull request objects with the trusted checker",
            "env": {"GH_TOKEN": "${{ github.token }}"},
            "run": TRUSTED_SCAN_COMMAND,
        },
    ]


def _pull_target_violations(document: dict, text: str, path: str) -> list[str]:
    """Require the exact closed privileged workflow execution surface."""
    if not _pull_target_trigger(document):
        return []
    expected_job = {
        "runs-on": "ubuntu-latest",
        "permissions": {"contents": "read"},
        "steps": _trusted_steps(),
    }
    trusted_job = dict(expected_job)
    raw_jobs = document.get("jobs")
    candidate_job = raw_jobs.get("immutable-compliance") if isinstance(raw_jobs, dict) else None
    if isinstance(candidate_job, dict):
        candidate_job = dict(candidate_job)
        candidate_steps = candidate_job.get("steps")
        if isinstance(candidate_steps, list) and len(candidate_steps) == 5:
            python_step = candidate_steps[2]
            version = (python_step.get("with", {}).get("python-version")
                       if isinstance(python_step, dict)
                       else None)
            version_text = str(version) if isinstance(version, (str, float, int)) else ""
            if PYTHON_VERSION_PATTERN.fullmatch(version_text):
                candidate_steps = list(candidate_steps)
                python_step = dict(python_step)
                python_step["with"] = {"python-version": version_text}
                candidate_steps[2] = python_step
                candidate_job["steps"] = candidate_steps
                trusted_steps = _trusted_steps()
                trusted_steps[2] = dict(trusted_steps[2])
                trusted_steps[2]["with"] = {"python-version": version_text}
                trusted_job["steps"] = trusted_steps
    candidate_jobs = dict(raw_jobs) if isinstance(raw_jobs, dict) else {}
    if isinstance(candidate_job, dict):
        candidate_jobs["immutable-compliance"] = candidate_job
    job_schema_ok = document.get("jobs") in (
        {"immutable-compliance": expected_job},
        {"immutable-compliance": trusted_job},
    ) or candidate_jobs in (
        {"immutable-compliance": expected_job},
        {"immutable-compliance": trusted_job},
    )
    allowed_top_keys = {"name", True, "on", "concurrency", "permissions", "env", "jobs"}
    checks = (
        (set(document).issubset(allowed_top_keys), "has unsupported top-level keys"),
        (document.get("permissions") == {"contents": "read"}, "permissions are not read-only"),
        (document.get("env") == TRUSTED_ENVIRONMENT, "does not bind exact trusted inputs"),
        (job_schema_ok, "job schema is not trusted"),
        (re.search(r"secrets\s*(?:\.|\[)", text, re.IGNORECASE) is None, "references secrets"),
    )
    return [
        f"{path}: pull_request_target {message}"
        for passed, message in checks if not passed
    ]


def _workflow_violations(
    text: str, path: str, persist_checker: ModuleType,
) -> list[str]:
    """Check immutable actions and privileged workflow execution policy."""
    try:
        documents = list(persist_checker.yaml.safe_load_all(text))
    except Exception:
        return [f"{path}: malformed workflow YAML cannot be checked"]
    violations = []
    for document in documents:
        violations.extend(_action_violations(document, path))
        if isinstance(document, dict):
            violations.extend(_pull_target_violations(document, text, path))
    return violations


def _scan_blob(
    path: str, text: str, mode: str, checkers: dict[str, ModuleType],
) -> list[str]:
    """Dispatch one decoded tracked blob to every applicable checker."""
    violations = list(
        checkers["check_secrets_heuristic"].find_violations(text, path))
    if mode == "120000":
        critical = (
            path == "requirements-checkers.txt"
            or path == "scripts/trusted_git.py"
            or path.startswith("scripts/check_")
            or path.startswith(".github/workflows/")
        )
        if critical:
            violations.append(
                f"{path}: trusted compliance files must not be symlinks")
        return violations
    lower_path = path.lower()
    name = lower_path.rsplit("/", 1)[-1]
    is_yaml = lower_path.endswith((".yml", ".yaml"))
    is_action = lower_path.startswith(".github/actions/") and name in (
        "action.yml", "action.yaml")
    if (lower_path.startswith(".github/workflows/") and is_yaml) or is_action:
        violations.extend(
            checkers["check_persist_credentials"].find_violations(text, path))
        violations.extend(_workflow_violations(
            text, path, checkers["check_persist_credentials"]))
    if Path(lower_path).suffix in CODE_SUFFIXES:
        violations.extend(
            checkers["check_weak_hashing"].find_violations(text, path))
    is_dockerfile = name in ("dockerfile", "containerfile") or name.startswith(
        ("dockerfile.", "dockerfile-", "containerfile.", "containerfile-"))
    if is_dockerfile or is_yaml:
        violations.extend(
            checkers["check_dockerfile_root"].find_violations(text, path))
    return violations


@contextmanager
def _trusted_path(executable: str, safe_path: str):
    """Make bare `git` calls from the conflict checker resolve safely."""
    old_path = os.environ.get("PATH")
    os.environ["PATH"] = os.pathsep.join((str(Path(executable).parent), safe_path))
    try:
        yield
    finally:
        if old_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = old_path


def _scan_conflicts(
    checker: ModuleType, repo: Path, object_id: str,
    executable: str, safe_path: str,
) -> list[str]:
    """Invoke the trusted conflict checker's immutable tree mode."""
    immutable_scan = getattr(checker, "_check_tree", None)
    if not callable(immutable_scan):
        raise RuntimeError("conflict checker has no immutable tree scan")
    with _trusted_path(executable, safe_path):
        return list(immutable_scan(str(repo), object_id))


def _scan_tree(
    repo: Path, object_id: str, checkers: dict[str, ModuleType],
) -> list[str]:
    """Scan all supported blobs and conflict markers in one exact object."""
    executable, safe_path = _resolve_git(repo)
    environment = _git_environment(safe_path)
    prefix = _git_prefix(executable, repo, environment)
    object_type = _run_git(
        prefix, ["cat-file", "-t", "--", object_id], repo,
        environment, MAX_GIT_DIAGNOSTIC)
    if object_type not in (b"commit\n", b"tree\n"):
        raise RuntimeError("--tree object must be a commit or tree")
    raw_entries = _run_git(
        prefix, ["ls-tree", "-r", "-z", "--full-tree", object_id],
        repo, environment, MAX_TREE_OUTPUT)
    entries = _parse_tree_entries(raw_entries)
    violations = []
    total_bytes = 0
    blobs = {}
    for path, blob_id, mode in entries:
        content = blobs.get(blob_id)
        if content is None:
            content = _read_blob(prefix, repo, environment, path, blob_id)
            blobs[blob_id] = content
        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_BLOB_BYTES:
            raise RuntimeError(
                f"tree content exceeds limit ({MAX_TOTAL_BLOB_BYTES} bytes)")
        violations.extend(
            _scan_blob(path, _decode_blob(content), mode, checkers))
        if len(violations) >= MAX_VIOLATIONS:
            violations = violations[:MAX_VIOLATIONS]
            violations.append(
                f"reached violation limit ({MAX_VIOLATIONS}), stopping")
            return violations
    violations.extend(_scan_conflicts(
        checkers["check_conflict_markers"], repo, object_id,
        executable, safe_path))
    return violations


def _scan_metadata(
    values: dict[str, str], repo: Path, checkers: dict[str, ModuleType],
) -> list[str]:
    """Run trusted commit and branch checks for workflow metadata."""
    author = values.get("--pr-author", "")
    violations = checkers["check_banned_agents"].find_violations([], author)
    bots = _load_trusted(BOT_REGISTRY_NAME)
    events = bots.LazyEventAuthor(os.environ.get("GITHUB_EVENT_PATH", ""))
    violations.extend(_branch_violations(
        values.get("--branch", ""), author, bots, events, checkers))
    if values.get("--base", ""):
        violations.extend(_scan_commit_range(values, repo, checkers, bots, events))
    if events.error is not None:
        violations.append(
            f"pull request event unreadable ({_sanitize(events.error)}); bot "
            "exemptions denied. Re-run the workflow; if it persists, report "
            "the runner event file.")
    return violations


def _branch_violations(
    branch: str, author: str, bots: ModuleType, events: object,
    checkers: dict[str, ModuleType],
) -> list[str]:
    """Apply branch naming unless a verified trusted bot owns the branch."""
    if not branch:
        return []
    branch_checker = checkers["check_branch_name"]
    bot = bots.match_exempt_bot(author, branch)
    if bot is None:
        return branch_checker.find_violations(branch)
    if bot.account_id is None:
        return []
    event = events.get()
    if event is not None and bots.event_verifies(bot, branch, event):
        return []
    violations = branch_checker.find_violations(branch)
    if event is not None:
        violations.append(
            f"{bot.login} account ID or head ref does not match the pull "
            "request event")
    elif events.error is None:
        violations.append(
            f"{bot.login} branch exemption needs the GitHub pull request "
            "event (ID verification); local runs cannot grant it")
    return violations


def _scan_commit_range(
    values: dict[str, str], repo: Path, checkers: dict[str, ModuleType],
    bots: ModuleType, events: object,
) -> list[str]:
    """Run trusted identity, attribution, and message checks on the range."""
    banned = checkers["check_banned_agents"]
    violations = []
    base = _validate_object_id(values["--base"])
    head = values["--tree"]
    commits = banned.load_commits(base, head, repo)
    violations.extend(banned.find_violations(commits))
    attribution = checkers.get("check_commit_attribution")
    if attribution is not None:
        violations.extend(attribution.trailer_violations(commits))
        repository_name = os.environ.get("PR_BASE_REPOSITORY", "")
        if repository_name:
            try:
                violations.extend(
                    attribution.github_identity_violations(
                        commits, repository_name, str(repo)
                    )
                )
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
                violations.append(
                    "commit attribution lookup failed: " + _sanitize(error)
                )
    identity = checkers["check_git_identity"]
    identities = identity.log_identities(
        ["--end-of-options", f"{base}..{head}"], repo)
    violations.extend(identity.find_violations(identities))
    violations.extend(bots.bot_commit_violations(
        identities, values.get("--pr-author", ""), events))
    messages = checkers["check_commit_message"]
    commit_messages = messages.load_commit_messages(base, head, repo)
    warnings = messages.find_message_violations(commit_messages)
    for warning in warnings:
        print(_sanitize(warning))
    return violations


def _parse_cli(arguments: list[str]) -> tuple[dict[str, str], bool]:
    """Parse required tree inputs and reserved workflow metadata."""
    option_names = {
        "--repo", "--tree", "--base", "--branch", "--pr-author",
    }
    values = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option in ("-h", "--help"):
            return {}, True
        if option not in option_names:
            raise RuntimeError(f"unknown argument: {_sanitize(option)}")
        if option in values:
            raise RuntimeError(f"{option} may be supplied only once")
        if index + 1 >= len(arguments):
            raise RuntimeError(f"{option} requires a value")
        values[option] = arguments[index + 1]
        index += 2
    missing = [option for option in ("--repo", "--tree") if option not in values]
    if missing:
        raise RuntimeError(f"required argument missing: {', '.join(missing)}")
    return values, False


def _resolve_repo(raw_path: str) -> Path:
    """Resolve the candidate repository without reading candidate code."""
    try:
        repo = Path(raw_path).resolve(strict=True)
    except OSError as error:
        raise RuntimeError(f"could not resolve --repo: {_sanitize(error)}") from error
    if not repo.is_dir():
        raise RuntimeError("--repo must name a directory")
    return repo


def _usage() -> str:
    """Return the stable CLI usage text."""
    return (
        "usage: check_compliance_tree.py --repo PATH --tree OID "
        "[--base OID] [--branch VALUE] [--pr-author VALUE]"
    )


def _report(violations: list[str]) -> int:
    """Print sanitized violations and return the implied status."""
    for violation in violations:
        print(_sanitize(violation), file=sys.stderr)
    return 1 if violations else 0


def main() -> int:
    """Run the immutable compliance CLI and fail closed on every error."""
    try:
        values, help_requested = _parse_cli(sys.argv[1:])
        if help_requested:
            print(_usage())
            return 0
        repo = _resolve_repo(values["--repo"])
        object_id = _validate_object_id(values["--tree"])
        values["--tree"] = object_id
        checkers = _load_checkers()
        violations = _scan_tree(repo, object_id, checkers)
        violations.extend(_scan_metadata(values, repo, checkers))
        return _report(violations)
    except (Exception, KeyboardInterrupt) as error:
        print(f"error: {_sanitize(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
