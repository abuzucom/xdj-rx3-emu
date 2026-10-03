#!/usr/bin/env python3
"""Regression coverage for trusted immutable compliance scanning."""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

try:
    from tests.persistent_main_worker import MainWorker
    from tests.retrying_temp_directory import RetryingTemporaryDirectory
except ImportError:
    from persistent_main_worker import MainWorker
    from retrying_temp_directory import RetryingTemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER_PATH = REPO_ROOT / "scripts" / "check_compliance_tree.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "immutable-conflict-check.yml"
WORKFLOW_ROOT = REPO_ROOT / ".github" / "workflows"
SYNTHETIC_SECRET = "ghp_" + ("A" * 36)
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
SCANNER_REQUEST_TIMEOUT_SECONDS = 60.0
_SCANNER_WORKER = None


def scanner_worker() -> MainWorker:
    """Return the process-local persistent immutable scanner worker."""
    global _SCANNER_WORKER
    if _SCANNER_WORKER is None:
        _SCANNER_WORKER = MainWorker(
            CHECKER_PATH,
            request_timeout=SCANNER_REQUEST_TIMEOUT_SECONDS,
        )
    return _SCANNER_WORKER


def _git_environment() -> dict[str, str]:
    """Return isolated test-only author and config state."""
    environment = dict(os.environ)
    environment.update({
        "GIT_AUTHOR_NAME": "compliance test",
        "GIT_AUTHOR_EMAIL": "1234567+compliance-test@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "compliance test",
        "GIT_COMMITTER_EMAIL": "1234567+compliance-test@users.noreply.github.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
    })
    return environment


def _run_git(repo: Path, *args: str, input_text: str | None = None) -> str:
    """Run Git with fixed argument boundaries and return stripped stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
        env=_git_environment(),
    )
    return result.stdout.strip()


def _initialize_repo(repo: Path) -> None:
    """Initialize an isolated repository with a test-only identity."""
    _run_git(repo, "init", "-q", "-b", "main")


def _write_file(repo: Path, relative_path: str, content: str) -> None:
    """Write one fixture file below `repo`."""
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _commit_all(repo: Path, message: str) -> str:
    """Commit the complete fixture state and return its object ID."""
    _run_git(repo, "add", "--all")
    _run_git(repo, "commit", "-qm", message)
    return _run_git(repo, "rev-parse", "HEAD")


def _scan_tree(
    repo: Path, tree: str, *metadata: str,
) -> subprocess.CompletedProcess[str]:
    """Run the trusted scanner entrypoint in the persistent worker."""
    arguments = ["--repo", str(repo), "--tree", tree, *metadata]
    return scanner_worker().invoke(
        arguments, None, REPO_ROOT, dict(os.environ))


def _scan_tree_process(
    repo: Path, tree: str, *metadata: str,
) -> subprocess.CompletedProcess[str]:
    """Run one fresh scanner process for CLI integration coverage."""
    return subprocess.run(
        [
            sys.executable, str(CHECKER_PATH), "--repo", str(repo),
            "--tree", tree, *metadata,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _scan_output(result: subprocess.CompletedProcess[str]) -> str:
    """Combine scanner diagnostics without assuming their output stream."""
    return result.stdout + result.stderr


def _workflow_events(content: str) -> set[str]:
    """Return keys in the workflow's top-level `on` mapping."""
    lines = content.splitlines()
    start = lines.index("on:") + 1
    events = set()
    for line in lines[start:]:
        if line and not line.startswith(" "):
            break
        match = re.match(r"^  ([a-z_]+):", line)
        if match:
            events.add(match.group(1))
    return events


def _indented_blocks(content: str, key: str) -> list[str]:
    """Return structural blocks introduced by an exact YAML key."""
    lines = content.splitlines()
    blocks = []
    pattern = re.compile(rf"^( *){re.escape(key)}:\s*$")
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        end = index + 1
        while end < len(lines):
            candidate = lines[end]
            candidate_indent = len(candidate) - len(candidate.lstrip(" "))
            if candidate.strip() and candidate_indent <= indent:
                break
            end += 1
        blocks.append("\n".join(lines[index:end]))
    return blocks


def _step_blocks(content: str) -> list[str]:
    """Return workflow sequence entries that represent steps."""
    lines = content.splitlines()
    starts = []
    for index, line in enumerate(lines):
        match = re.match(r"^( *)-\s+(?:name|uses):", line)
        if match:
            starts.append((index, len(match.group(1))))
    blocks = []
    for index, indent in starts:
        end = index + 1
        while end < len(lines):
            candidate = lines[end]
            candidate_indent = len(candidate) - len(candidate.lstrip(" "))
            if candidate.strip() and candidate_indent < indent:
                break
            if re.match(rf"^ {{{indent}}}-\s+", candidate):
                break
            end += 1
        blocks.append("\n".join(lines[index:end]))
    return blocks


def _run_blocks(content: str) -> list[str]:
    """Return normalized inline and multiline workflow commands."""
    lines = content.splitlines()
    commands = []
    for index, line in enumerate(lines):
        match = re.match(r"^( *)run:\s*(.*)$", line)
        if not match:
            continue
        indent = len(match.group(1))
        parts = [] if match.group(2) in ("|", "|-", ">", ">-") else [match.group(2)]
        end = index + 1
        while end < len(lines):
            candidate = lines[end]
            candidate_indent = len(candidate) - len(candidate.lstrip(" "))
            if candidate.strip() and candidate_indent <= indent:
                break
            if candidate.strip():
                parts.append(candidate.strip())
            end += 1
        commands.append(" ".join(parts))
    return commands


def _parse_scalar(value: str):
    """Parse the narrow YAML scalar subset used by these assertions."""
    value = value.strip()
    if value == "false":
        return False
    if value == "true":
        return True
    return value.strip("'\"")


def _block_scalar(block: str, key: str):
    """Return one scalar from a structural block, or None when absent."""
    match = re.search(rf"^\s*{re.escape(key)}:\s*([^#\n]+)", block, re.MULTILINE)
    return _parse_scalar(match.group(1)) if match else None


class ImmutableComplianceScannerTest(unittest.TestCase):
    """The trusted CLI reads exact Git objects, never mutable PR files."""

    def _assert_detected(self, repo: Path, tree: str, path: str, term: str) -> None:
        result = _scan_tree(repo, tree)
        output = _scan_output(result)
        self.assertEqual(result.returncode, 1, output)
        self.assertIn(path, output)
        self.assertIn(term, output.lower())

    def test_docs_secret_survives_a_pr_checker_that_exits_zero(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            _write_file(repo, "docs/security.md", f"token: {SYNTHETIC_SECRET}\n")
            _write_file(
                repo,
                "scripts/check_compliance_tree.py",
                "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n",
            )
            violating_tree = _commit_all(repo, "test: add PR checker")
            _write_file(repo, "docs/security.md", "No credentials here.\n")
            _commit_all(repo, "test: clean current tree")

            self._assert_detected(repo, violating_tree, "docs/security.md", "secret")

    def test_workflow_secret_is_read_from_the_requested_tree(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            path = ".github/workflows/test.yaml"
            _write_file(repo, path, f"env:\n  TOKEN: {SYNTHETIC_SECRET}\n")
            violating_tree = _commit_all(repo, "test: add workflow secret")
            _write_file(repo, path, "name: clean\n")
            _commit_all(repo, "test: clean current tree")

            self._assert_detected(repo, violating_tree, path, "secret")

    def test_production_env_file_is_read_from_the_requested_tree(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            _write_file(repo, ".env.production", "APP_MODE=production\n")
            violating_tree = _commit_all(repo, "test: add production env")
            (repo / ".env.production").rename(repo / ".env.example")
            _commit_all(repo, "test: clean current tree")

            self._assert_detected(repo, violating_tree, ".env.production", "env")

    def test_symlink_blob_is_read_from_the_requested_tree(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            link_blob = _run_git(
                repo, "hash-object", "-w", "--stdin",
                input_text=SYNTHETIC_SECRET,
            )
            _run_git(repo, "update-index", "--add", "--cacheinfo", f"120000,{link_blob},docs-link")
            _run_git(repo, "commit", "-qm", "test: add symlink blob")
            violating_tree = _run_git(repo, "rev-parse", "HEAD")
            clean_blob = _run_git(repo, "hash-object", "-w", "--stdin", input_text="clean\n")
            _run_git(repo, "update-index", "--cacheinfo", f"100644,{clean_blob},docs-link")
            _run_git(repo, "commit", "-qm", "test: clean current tree")

            self._assert_detected(repo, violating_tree, "docs-link", "secret")

    def test_javascript_weak_hash_is_read_from_the_requested_tree(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            _write_file(
                repo, "src/hash.js",
                "const digest = createHash('sha1').update(payload);\n",
            )
            violating_tree = _commit_all(repo, "test: add weak hash")

            self._assert_detected(repo, violating_tree, "src/hash.js", "sha-1")

    def test_bomless_utf16_secret_is_detected(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            path = repo / "docs" / "encoded.txt"
            path.parent.mkdir(parents=True)
            path.write_bytes(f"token: {SYNTHETIC_SECRET}\n".encode("utf-16-le"))
            violating_tree = _commit_all(repo, "test: add encoded secret")

            self._assert_detected(repo, violating_tree, "encoded.txt", "secret")

    def test_symlink_named_dockerfile_is_not_parsed_as_dockerfile(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            link_blob = _run_git(
                repo, "hash-object", "-w", "--stdin", input_text="Containerfile\n"
            )
            _run_git(
                repo, "update-index", "--add", "--cacheinfo",
                f"120000,{link_blob},Dockerfile",
            )
            _run_git(repo, "commit", "-qm", "test: add Dockerfile symlink")
            tree = _run_git(repo, "rev-parse", "HEAD")

            result = _scan_tree_process(repo, tree)
            self.assertEqual(result.returncode, 0, _scan_output(result))

    def test_unsafe_pull_request_target_command_is_rejected(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            path = ".github/workflows/unsafe.yml"
            _write_file(
                repo,
                path,
                "on:\n  pull_request_target:\njobs:\n  unsafe:\n"
                "    permissions:\n      contents: read\n    steps:\n"
                "      - run: bash pr-head/payload.sh\n",
            )
            tree = _commit_all(repo, "test: add unsafe workflow")

            self._assert_detected(repo, tree, path, "pull_request_target")

    def test_unpinned_action_in_yaml_workflow_is_rejected(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            path = ".github/workflows/unsafe.yaml"
            _write_file(
                repo,
                path,
                "on:\n  push:\njobs:\n  test:\n    steps:\n"
                "      - uses: actions/checkout@v4\n        with:\n"
                "          persist-credentials: false\n",
            )
            tree = _commit_all(repo, "test: add mutable action")

            self._assert_detected(repo, tree, path, "commit sha")

    def test_branch_and_pr_author_are_checked_without_base(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            _write_file(repo, "README.md", "clean\n")
            tree = _commit_all(repo, "test: add clean tree")

            branch_result = _scan_tree(
                repo, tree, "--branch", "unsafe-branch")
            author_result = _scan_tree(
                repo, tree, "--pr-author", "grok-code")

            self.assertEqual(branch_result.returncode, 1)
            self.assertIn("branch", _scan_output(branch_result))
            self.assertEqual(author_result.returncode, 1)
            self.assertIn("banned-agent", _scan_output(author_result))

    def test_commit_metadata_is_checked_from_the_requested_range(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            _write_file(repo, "README.md", "base\n")
            base = _commit_all(repo, "test: add base")
            _write_file(repo, "README.md", "head\n")
            _run_git(repo, "add", "README.md")
            _run_git(
                repo, "commit", "-q", "-F", "-", input_text=(
                    "test: add head\n\n"
                    "Co-authored-by: Grok <agent@x.ai>\n"
                ),
            )
            head = _run_git(repo, "rev-parse", "HEAD")

            result = _scan_tree(
                repo, head, "--base", base, "--branch", "fix/clean",
                "--pr-author", "human",
            )

            self.assertEqual(result.returncode, 1, _scan_output(result))
            self.assertIn("banned-agent co-author", _scan_output(result))

    def test_current_privileged_workflow_policy_passes(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            _write_file(
                repo, ".github/workflows/immutable.yml",
                WORKFLOW_PATH.read_text(encoding="utf-8"),
            )
            tree = _commit_all(repo, "test: add trusted workflow")

            result = _scan_tree(repo, tree)
            self.assertEqual(result.returncode, 0, _scan_output(result))

    def test_privileged_checkout_cannot_redirect_trusted_base(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            content = WORKFLOW_PATH.read_text(encoding="utf-8").replace(
                "repository: ${{ env.PR_BASE_REPOSITORY }}",
                "repository: ${{ env.PR_HEAD_REPOSITORY }}",
                1,
            )
            path = ".github/workflows/immutable.yml"
            _write_file(repo, path, content)
            tree = _commit_all(repo, "test: redirect trusted checkout")

            self._assert_detected(repo, tree, path, "job schema")

    def test_privileged_scan_cannot_continue_on_error(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            content = WORKFLOW_PATH.read_text(encoding="utf-8").replace(
                "      - name: Scan immutable pull request objects",
                "      - continue-on-error: true\n"
                "        name: Scan immutable pull request objects",
            )
            path = ".github/workflows/immutable.yml"
            _write_file(repo, path, content)
            tree = _commit_all(repo, "test: weaken trusted scan")

            self._assert_detected(repo, tree, path, "job schema")

    def test_critical_checker_path_cannot_be_a_symlink(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            link_blob = _run_git(
                repo, "hash-object", "-w", "--stdin",
                input_text="../../pr-head/payload.py\n",
            )
            _run_git(
                repo, "update-index", "--add", "--cacheinfo",
                f"120000,{link_blob},scripts/trusted_git.py",
            )
            _run_git(repo, "commit", "-qm", "test: add checker symlink")
            tree = _run_git(repo, "rev-parse", "HEAD")

            self._assert_detected(
                repo, tree, "scripts/trusted_git.py", "symlink")

    def test_composite_action_requires_pinned_checkout(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            _initialize_repo(repo)
            path = ".github/actions/test/action.yml"
            _write_file(
                repo, path,
                "runs:\n  using: composite\n  steps:\n"
                "    - uses: actions/checkout@v4\n",
            )
            tree = _commit_all(repo, "test: add unsafe composite action")

            self._assert_detected(repo, tree, path, "commit sha")


class ImmutableWorkflowTest(unittest.TestCase):
    """The privileged workflow executes only pinned trusted immutable checks."""

    @classmethod
    def setUpClass(cls):
        cls.content = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_uses_only_pull_request_target_with_read_only_permissions(self):
        self.assertEqual(_workflow_events(self.content), {"pull_request_target"})
        permission_blocks = _indented_blocks(self.content, "permissions")
        self.assertTrue(permission_blocks)
        for block in permission_blocks:
            with self.subTest(block=block):
                body = "\n".join(block.splitlines()[1:])
                entries = re.findall(
                    r"^\s+([a-z-]+):\s*([^#\n]+)", body, re.MULTILINE)
                parsed = {key: _parse_scalar(value) for key, value in entries}
                self.assertEqual(parsed, {"contents": "read"})

    def test_has_no_pull_request_working_directory(self):
        self.assertNotRegex(self.content, r"(?m)^\s*working-directory:")

    def test_security_commands_use_trusted_immutable_scanning(self):
        self.assertIn(
            "TRUSTED_CHECKER: trusted-base/scripts/check_compliance_tree.py",
            self.content,
        )
        commands = [
            command
            for command in _run_blocks(self.content)
            if "check_" in command or "TRUSTED_CHECKER" in command
        ]
        self.assertTrue(commands)
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(
                    "trusted-base/scripts/" in command or '"$TRUSTED_CHECKER"' in command,
                    command,
                )
                self.assertRegex(command, r"--repo\s+[\"']?\$PR_REPO[\"']?")
                self.assertRegex(command, r"--tree\s+[\"']?\$PR_HEAD_SHA[\"']?")

    def test_no_pr_authored_script_is_executed(self):
        self.assertNotIn("pr-head/scripts/", self.content)
        self.assertNotRegex(self.content, r"(?m)python\s+(?:\.\./)?scripts/")
        for command in _run_blocks(self.content):
            if "python " not in command:
                continue
            with self.subTest(command=command):
                self.assertTrue(
                    "trusted-base/scripts/" in command
                    or '"$TRUSTED_CHECKER"' in command
                    or command == (
                        "python -m pip install --requirement "
                        "trusted-base/requirements-checkers.txt"
                    ),
                    command,
                )

    def test_external_actions_are_pinned_to_full_commit_shas(self):
        paths = [
            *WORKFLOW_ROOT.glob("*.yml"),
            *WORKFLOW_ROOT.glob("*.yaml"),
        ]
        for path in paths:
            content = path.read_text(encoding="utf-8")
            references = re.findall(r"(?m)^\s*uses:\s*([^\s#]+)", content)
            external = [
                reference for reference in references
                if not reference.startswith("./")
            ]
            for reference in external:
                with self.subTest(path=path.name, reference=reference):
                    self.assertIn("@", reference)
                    revision = reference.rsplit("@", 1)[1]
                    self.assertRegex(revision, rf"^{FULL_SHA.pattern}$")

    def test_every_checkout_parses_persist_credentials_as_false(self):
        checkout_steps = [
            block for block in _step_blocks(self.content) if "uses: actions/checkout@" in block
        ]
        self.assertTrue(checkout_steps)
        for block in checkout_steps:
            with self.subTest(block=block):
                self.assertIs(_block_scalar(block, "persist-credentials"), False)


if __name__ == "__main__":
    unittest.main()
