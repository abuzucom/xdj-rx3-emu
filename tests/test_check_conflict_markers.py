#!/usr/bin/env python3
"""Tests for scripts/check_conflict_markers.py and its wiring."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

try:
    from tests.persistent_main_worker import MainWorker
    from tests.retrying_temp_directory import RetryingTemporaryDirectory
except ImportError:
    from persistent_main_worker import MainWorker
    from retrying_temp_directory import RetryingTemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER_PATH = REPO_ROOT / "scripts" / "check_conflict_markers.py"
WORKFLOW_PATH = (
    REPO_ROOT / ".github" / "workflows" / "sync-check.yml"
)
IMMUTABLE_WORKFLOW_PATH = (
    REPO_ROOT / ".github" / "workflows" / "immutable-conflict-check.yml"
)
AGENTS_MD_WORKFLOW_PATH = (
    REPO_ROOT / ".github" / "workflows" / "agents-md-compliance.yml"
)
MAKEFILE_PATH = REPO_ROOT / "Makefile"
PRE_COMMIT_CONFIG_PATH = REPO_ROOT / ".pre-commit-config.yaml"
AGENTS_PATH = REPO_ROOT / "AGENTS.md"
HANDOFF_PATH = REPO_ROOT / "plan" / "HANDOFF.md.example"
README_PATH = REPO_ROOT / "README.md"
_CHECKER_WORKER = None


def checker_worker() -> MainWorker:
    """Return the process-local persistent conflict checker worker."""
    global _CHECKER_WORKER
    if _CHECKER_WORKER is None:
        _CHECKER_WORKER = MainWorker(CHECKER_PATH)
    return _CHECKER_WORKER


def run_checker(arguments: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run the real conflict checker entrypoint in the persistent worker."""
    return checker_worker().invoke(arguments, None, cwd, dict(os.environ))


def _load_checker_module():
    spec = importlib.util.spec_from_file_location(
        "check_conflict_markers", CHECKER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker_module()


def _workflow_events(content: str) -> set[str]:
    """Return event keys from a workflow's top-level on block."""
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


def _workflow_jobs(content: str) -> dict[str, str]:
    """Return job blocks keyed by job ID."""
    lines = content.splitlines()
    start = lines.index("jobs:") + 1
    starts = []
    for index in range(start, len(lines)):
        match = re.match(r"^  ([a-z0-9-]+):$", lines[index])
        if match:
            starts.append((index, match.group(1)))
    jobs = {}
    for position, (index, job_id) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        jobs[job_id] = "\n".join(lines[index:end])
    return jobs


def _job_needs(job_block: str) -> set[str]:
    """Return scalar or inline-list needs from one job block."""
    match = re.search(r"^    needs:\s*(.+)$", job_block, re.MULTILINE)
    if not match:
        return set()
    value = match.group(1).strip().strip("[]")
    return {item.strip() for item in value.split(",") if item.strip()}


def _depends_on(jobs: dict[str, str], job_id: str, target: str) -> bool:
    """Return True if a job reaches target through the needs graph."""
    pending = list(_job_needs(jobs[job_id]))
    visited = set()
    while pending:
        dependency = pending.pop()
        if dependency == target:
            return True
        if dependency in visited or dependency not in jobs:
            continue
        visited.add(dependency)
        pending.extend(_job_needs(jobs[dependency]))
    return False


class ConflictMarkerDetectionTest(unittest.TestCase):
    """Detection across marker formats, sizes, and edge cases."""

    def test_clean_file_has_no_violations(self):
        content = "def hello():\n    return 'world'\n"
        violations = checker.check_content(content, "clean.py")
        self.assertEqual(violations, [])

    def test_standard_7char_conflict_block(self):
        content = (
            "<<<<<<< HEAD\nleft\n=======\n"
            "right\n>>>>>>> branch\n"
        )
        violations = checker.check_content(content, "foo.py")
        self.assertEqual(len(violations), 1)
        self.assertIn(
            "foo.py:1-5: unresolved conflict block",
            violations[0],
        )

    def test_diff3_conflict_block(self):
        content = (
            "<<<<<<< HEAD\nleft\n||||||| base\n"
            "ancestor\n=======\nright\n>>>>>>> branch\n"
        )
        violations = checker.check_content(content, "foo.py")
        self.assertEqual(len(violations), 1)
        self.assertIn(
            "foo.py:1-7: unresolved conflict block",
            violations[0],
        )

    def test_size_1_conflict_block_detected(self):
        """Genuine size-1 balanced conflict block is detected."""
        content = "< HEAD\nleft\n=\nright\n> branch\n"
        violations = checker.check_content(content, "test.py")
        self.assertEqual(len(violations), 1)
        self.assertIn(
            "test.py:1-5: unresolved conflict block",
            violations[0],
        )

    def test_size_1_with_configured_size_1_detected(self):
        content = "< HEAD\nleft\n=\nright\n> branch\n"
        violations = checker.check_content(
            content, "test.py", configured_marker_size=1
        )
        self.assertEqual(len(violations), 1)
        self.assertIn(
            "test.py:1-5: unresolved conflict block",
            violations[0],
        )

    def test_size_2_conflict_block_detected(self):
        """Genuine size-2 balanced conflict block is detected."""
        content = "<< HEAD\nleft\n==\nright\n>> branch\n"
        violations = checker.check_content(content, "test.py")
        self.assertEqual(len(violations), 1)
        self.assertIn(
            "test.py:1-5: unresolved conflict block",
            violations[0],
        )

    def test_size_2_detected_with_different_configured_size(self):
        """Size-2 block detected even when configured size is 10."""
        content = "<< HEAD\nleft\n==\nright\n>> branch\n"
        violations = checker.check_content(
            content, "test.py", configured_marker_size=10
        )
        self.assertEqual(len(violations), 1)
        self.assertIn(
            "test.py:1-5: unresolved conflict block",
            violations[0],
        )

    def test_configurable_marker_size_detected_exact(self):
        content = "<<< HEAD\nleft\n===\nright\n>>> branch\n"
        violations = checker.check_content(
            content, "custom.py", configured_marker_size=3
        )
        self.assertEqual(len(violations), 1)
        self.assertIn(
            "custom.py:1-5: unresolved conflict block",
            violations[0],
        )

    def test_configurable_large_marker_size_detected(self):
        content = (
            "<<<<<<<<<< HEAD\nleft\n"
            "==========\nright\n>>>>>>>>>> branch\n"
        )
        violations = checker.check_content(
            content, "large.py", configured_marker_size=10
        )
        self.assertEqual(len(violations), 1)
        self.assertIn(
            "large.py:1-5: unresolved conflict block",
            violations[0],
        )

    def test_configured_size_10_still_catches_7char_conflict(self):
        """Generic patterns fire even when configured size is 10."""
        content = (
            "<<<<<<< HEAD\nleft\n=======\n"
            "right\n>>>>>>> branch\n"
        )
        violations = checker.check_content(
            content, "test.py", configured_marker_size=10
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("unresolved conflict block", violations[0])

    def test_configured_size_10_catches_7char_in_markdown(self):
        """Seven-char conflict in Markdown detected despite size=10."""
        content = (
            "<<<<<<< HEAD\nleft\n=======\n"
            "right\n>>>>>>> branch\n"
        )
        violations = checker.check_content(
            content, "test.md", configured_marker_size=10
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("unresolved conflict block", violations[0])

    def test_unclosed_opener_flagged(self):
        content = "prefix\n<<<<<<< HEAD\nleft\nsome code\n"
        violations = checker.check_content(
            content, "unclosed.py"
        )
        self.assertEqual(len(violations), 1)
        self.assertIn(
            "unclosed.py:2: unclosed conflict marker opener",
            violations[0],
        )

    def test_orphan_closer_flagged(self):
        content = "prefix\n>>>>>>> origin/main\nsuffix\n"
        violations = checker.check_content(
            content, "orphan.py"
        )
        self.assertEqual(len(violations), 1)
        self.assertIn(
            "orphan.py:2: orphan conflict marker closer",
            violations[0],
        )

    def test_orphan_diff3_separator_flagged(self):
        content = (
            "prefix\n||||||| merged common ancestors\nsuffix\n"
        )
        violations = checker.check_content(
            content, "orphan_diff3.py"
        )
        self.assertEqual(len(violations), 1)
        self.assertIn(
            "orphan_diff3.py:2: orphan conflict marker separator",
            violations[0],
        )

    def test_orphan_separator_in_python_is_flagged(self):
        content = "def foo():\n    x = 1\n=======\n    return x\n"
        violations = checker.check_content(
            content, "script.py"
        )
        self.assertEqual(len(violations), 1)
        self.assertIn(
            "script.py:3: orphan conflict marker separator",
            violations[0],
        )

    def test_orphan_separator_in_yaml_is_flagged(self):
        content = "key: value\n=======\nother: value\n"
        violations = checker.check_content(
            content, "config.yml"
        )
        self.assertEqual(len(violations), 1)
        self.assertIn(
            "config.yml:2: orphan conflict marker separator",
            violations[0],
        )

    def test_setext_markdown_heading_is_not_flagged(self):
        content = (
            "# Markdown Doc\n\nSection Title\n"
            "=============\n\nBody text.\n"
        )
        violations = checker.check_content(content, "doc.md")
        self.assertEqual(violations, [])

    def test_orphan_separator_in_md_without_heading_flagged(self):
        content = "\n=======\nSome text\n"
        violations = checker.check_content(content, "doc.md")
        self.assertEqual(len(violations), 1)
        self.assertIn(
            "doc.md:2: orphan conflict marker separator",
            violations[0],
        )

    def test_consecutive_openers_flag_previous(self):
        content = (
            "<<<<<<< HEAD\nleft\n<<<<<<< OTHER\n"
            "other\n=======\nright\n>>>>>>> branch\n"
        )
        violations = checker.check_content(
            content, "nested.py"
        )
        self.assertEqual(len(violations), 2)
        self.assertIn(
            "nested.py:1: unclosed conflict marker opener",
            violations[0],
        )
        self.assertIn(
            "nested.py:3-7: unresolved conflict block",
            violations[1],
        )

    def test_narrow_openers_do_not_hide_reportable_diff3_separators(self):
        for width in (1, 2):
            narrow = "<" * width
            content = (
                f"{narrow} HEAD\n||||||| base\n"
                f"{narrow.replace('<', '=')}\n"
                f"{narrow.replace('<', '>')} branch\n"
            )
            with self.subTest(width=width):
                violations = checker.check_content(content, "hidden.py")
                self.assertEqual(len(violations), 2)
                self.assertIn(
                    "hidden.py:2: orphan conflict marker separator",
                    violations[0],
                )
                self.assertIn(
                    "hidden.py:1-4: unresolved conflict block",
                    violations[1],
                )

    def test_narrow_openers_do_not_hide_reportable_separators(self):
        for width in (1, 2):
            narrow = "<" * width
            content = (
                f"{narrow} HEAD\n=======\n"
                f"{narrow.replace('<', '=')}\n"
                f"{narrow.replace('<', '>')} branch\n"
            )
            with self.subTest(width=width):
                violations = checker.check_content(content, "hidden.py")
                self.assertEqual(len(violations), 2)
                self.assertIn(
                    "hidden.py:2: orphan conflict marker separator",
                    violations[0],
                )
                self.assertIn(
                    "hidden.py:1-4: unresolved conflict block",
                    violations[1],
                )

    def test_setext_heading_stays_valid_under_a_narrow_opener(self):
        content = "< note\nHeading\n=======\n=\n> branch\n"
        violations = checker.check_content(content, "heading.md")
        self.assertEqual(
            violations,
            ["heading.md:1-5: unresolved conflict block"],
        )


class FileCheckingTest(unittest.TestCase):
    """Filesystem interactions, encodings, and fail-closed errors."""

    def test_nonexistent_file_fails_closed(self):
        violations = checker.check_file(
            "nonexistent_file_path_12345.txt"
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("error:", violations[0])
        self.assertIn("file not found", violations[0])

    def test_binary_file_is_skipped(self):
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"PNG\x00\x01\x02\x03<<<<<<< HEAD\x00")
            tmp_path = Path(tmp.name)
        try:
            violations = checker.check_file(str(tmp_path))
            self.assertEqual(violations, [])
        finally:
            tmp_path.unlink()

    def test_utf16le_file_with_bom_detected(self):
        text = (
            "<<<<<<< HEAD\nleft\n=======\n"
            "right\n>>>>>>> branch\n"
        )
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"\xff\xfe" + text.encode("utf-16-le"))
            tmp_path = Path(tmp.name)
        try:
            violations = checker.check_file(str(tmp_path))
            self.assertEqual(len(violations), 1)
            self.assertIn(
                "unresolved conflict block", violations[0]
            )
        finally:
            tmp_path.unlink()

    def test_utf16be_file_with_bom_detected(self):
        text = (
            "<<<<<<< HEAD\nleft\n=======\n"
            "right\n>>>>>>> branch\n"
        )
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"\xfe\xff" + text.encode("utf-16-be"))
            tmp_path = Path(tmp.name)
        try:
            violations = checker.check_file(str(tmp_path))
            self.assertEqual(len(violations), 1)
            self.assertIn(
                "unresolved conflict block", violations[0]
            )
        finally:
            tmp_path.unlink()

    def test_utf16_encoding_hint_without_bom_detected(self):
        text = (
            "<<<<<<< HEAD\nleft\n=======\n"
            "right\n>>>>>>> branch\n"
        )
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(text.encode("utf-16-le"))
            tmp_path = Path(tmp.name)
        try:
            violations = checker.check_file(
                str(tmp_path), encoding_hint="UTF-16LE"
            )
            self.assertEqual(len(violations), 1)
            self.assertIn(
                "unresolved conflict block", violations[0]
            )
        finally:
            tmp_path.unlink()

    def test_symlink_is_ignored(self):
        with RetryingTemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "target.txt"
            target.write_text(
                "<<<<<<< HEAD\na\n=======\n"
                "b\n>>>>>>> main\n",
                encoding="utf-8",
            )
            link = Path(tmp_dir) / "link.txt"
            try:
                os.symlink(target, link)
                violations = checker.check_file(str(link))
                self.assertEqual(violations, [])
            except (OSError, NotImplementedError):
                pass  # symlinks may need privileges on Windows

    def test_safe_read_regular_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"hello world")
            tmp_path = tmp.name
        try:
            data, err = checker._safe_read(tmp_path, 1024)
            self.assertIsNone(err)
            self.assertEqual(data, b"hello world")
        finally:
            os.unlink(tmp_path)

    def test_safe_read_nonexistent(self):
        data, err = checker._safe_read(
            "no_such_file_xyz.tmp", 1024
        )
        self.assertIsNone(data)
        self.assertIn("file not found", err)

    def test_safe_read_symlink_skipped(self):
        with RetryingTemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "target.txt"
            target.write_text("content", encoding="utf-8")
            link = Path(tmp_dir) / "link.txt"
            try:
                os.symlink(target, link)
                data, err = checker._safe_read(str(link), 1024)
                self.assertIsNone(data)
                self.assertIsNone(err)
            except (OSError, NotImplementedError):
                pass  # symlinks may need privileges on Windows

    def test_get_git_attributes_raises_on_git_failure(self):
        """get_git_attributes fails closed on non-zero git exit."""
        with patch("subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 128
            mock_proc.stderr = b"fatal: git check-attr error"
            mock_run.return_value = mock_proc
            with self.assertRaises(RuntimeError) as ctx:
                checker.get_git_attributes(["foo.py"])
            self.assertIn("git check-attr failed", str(ctx.exception))

    def test_staged_utf16_working_tree_encoding_not_applied(self):
        """Staged blob in index is decoded as UTF-8 without working-tree-encoding."""
        with RetryingTemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            # Simulate index entry with conflict text in UTF-8
            sha = "fake_sha_1234"
            with patch.object(checker, "_get_index_entries") as mock_entries, \
                 patch.object(checker, "get_git_attributes") as mock_attrs, \
                 patch.object(checker, "_get_blob_size") as mock_size, \
                 patch.object(checker, "_read_blob") as mock_blob:
                mock_entries.return_value = [
                    ("test.txt", sha, "0", "100644")
                ]
                mock_attrs.return_value = {
                    "test.txt": {"working-tree-encoding": "UTF-16LE"}
                }
                staged_content = (
                    b"<<<<<<< HEAD\nleft\n=======\n"
                    b"right\n>>>>>>> branch\n"
                )
                mock_size.return_value = len(staged_content)
                mock_blob.return_value = staged_content
                violations = checker._check_staged(str(repo_path))
                self.assertEqual(len(violations), 1)
                self.assertIn("unresolved conflict block", violations[0])

    def test_staged_oversized_blob_rejected_without_buffering(self):
        """Blobs exceeding MAX_FILE_SIZE are rejected via size check."""
        with patch.object(checker, "_get_index_entries") as mock_entries, \
             patch.object(checker, "get_git_attributes") as mock_attrs, \
             patch.object(checker, "_get_blob_size") as mock_size, \
             patch.object(checker, "_read_blob") as mock_blob:
            mock_entries.return_value = [
                ("huge.bin", "fake_sha_huge", "0", "100644")
            ]
            mock_attrs.return_value = {}
            mock_size.return_value = checker.MAX_FILE_SIZE + 100
            violations = checker._check_staged(".")
            self.assertEqual(len(violations), 1)
            self.assertIn("blob size", violations[0])
            self.assertIn("exceeds limit", violations[0])
            # _read_blob must never have been called
            mock_blob.assert_not_called()


class GitTrackedFilesTest(unittest.TestCase):
    """Git-tracked regular files enumeration."""

    def test_get_tracked_regular_files_includes_repo_files(self):
        files = checker.get_tracked_regular_files()
        self.assertIn("AGENTS.md", files)
        self.assertIn("README.md", files)
        self.assertIn(
            "scripts/check_conflict_markers.py", files
        )


class CliExecutionTest(unittest.TestCase):
    """CLI subprocess execution."""

    def test_cli_on_current_repo_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, str(CHECKER_PATH)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"stdout: {proc.stdout}, stderr: {proc.stderr}",
        )

    def test_cli_with_conflict_file_exits_one(self):
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(
                "<<<<<<< HEAD\na\n=======\n"
                "b\n>>>>>>> branch\n"
            )
            tmp_path = Path(tmp.name)
        try:
            proc = run_checker([str(tmp_path)], REPO_ROOT)
            self.assertEqual(proc.returncode, 1)
            self.assertIn(
                "unresolved conflict block", proc.stderr
            )
        finally:
            tmp_path.unlink()

    def test_cli_from_subdirectory_exits_zero(self):
        """Repo root is resolved; all tracked files are checked."""
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            nested = repo / "nested"
            nested.mkdir()
            subprocess.run(
                ["git", "init", "-q", "-b", "main"], cwd=repo,
                capture_output=True, check=True)
            (repo / "clean.txt").write_text("clean\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "clean.txt"], cwd=repo,
                capture_output=True, check=True)
            proc = run_checker([], nested)
        self.assertEqual(
            proc.returncode, 0,
            f"stdout: {proc.stdout}, stderr: {proc.stderr}",
        )

    def test_staged_flag_exits_zero_on_clean_repo(self):
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(
                ["git", "init", "-q", "-b", "main"], cwd=repo,
                capture_output=True, check=True)
            (repo / "staged.txt").write_text("clean\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "staged.txt"], cwd=repo,
                capture_output=True, check=True)
            proc = run_checker(["--staged"], repo)
        self.assertEqual(
            proc.returncode, 0,
            f"stdout: {proc.stdout}, stderr: {proc.stderr}",
        )


class WiringTest(unittest.TestCase):
    """Exact wiring across CI, Makefile, and pre-commit config."""

    def test_sync_check_workflow_wires_exact_command(self):
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        expected = (
            "- name: Check worktree conflict markers (ordinary test)\n"
            "        run: python scripts/"
            "check_conflict_markers.py"
        )
        self.assertIn(expected, content)

    def test_immutable_workflow_uses_only_the_base_checker(self):
        content = IMMUTABLE_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertEqual(_workflow_events(content), {"pull_request_target"})
        self.assertIn("edited", content)
        self.assertRegex(
            content,
            r"uses: actions/setup-python@[0-9a-f]{40}",
        )
        self.assertIn("ref: ${{ env.PR_BASE_SHA }}", content)
        self.assertIn("ref: ${{ env.PR_HEAD_SHA }}", content)
        self.assertIn("path: trusted-base", content)
        self.assertIn("path: pr-head", content)
        self.assertIn(
            "TRUSTED_CHECKER: trusted-base/scripts/check_compliance_tree.py",
            content,
        )
        self.assertIn('python "$TRUSTED_CHECKER"', content)
        self.assertIn('--repo "$PR_REPO" --tree "$PR_HEAD_SHA"', content)
        self.assertNotIn("python pr-head/", content)

    def test_privileged_workflow_has_one_immutable_job(self):
        content = IMMUTABLE_WORKFLOW_PATH.read_text(encoding="utf-8")
        jobs = _workflow_jobs(content)
        self.assertEqual(set(jobs), {"immutable-compliance"})
        block = jobs["immutable-compliance"]
        self.assertIn("    permissions:\n      contents: read", block)
        self.assertIn("ref: ${{ env.PR_BASE_SHA }}", block)
        self.assertIn("ref: ${{ env.PR_HEAD_SHA }}", block)
        self.assertNotIn("working-directory:", block)
        self.assertNotIn("pull-requests: write", content)
        self.assertNotIn("actions/github-script", content)

    def test_privileged_workflow_does_not_execute_pr_authored_tools(self):
        content = IMMUTABLE_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("python -m unittest", content)
        self.assertNotRegex(content, r"run:\s+python scripts/")
        self.assertNotIn("0xmariowu/AgentLint", content)

    def test_privileged_trigger_has_read_only_permissions_and_no_secrets(self):
        content = IMMUTABLE_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("secrets.", content.lower())
        self.assertNotIn("write-all", content.lower())
        self.assertIn("permissions:\n  contents: read", content)
        for job_id, block in _workflow_jobs(content).items():
            with self.subTest(job=job_id):
                match = re.search(
                    r"^    permissions:\n((?:      [^\n]+\n?)+)",
                    block,
                    re.MULTILINE,
                )
                self.assertIsNotNone(match)
                self.assertEqual(
                    [line.strip() for line in match.group(1).splitlines()],
                    ["contents: read"],
                )
        self.assertNotRegex(
            content,
            r"(?m)^\s+[a-z-]+:\s*(?:write|write-all)\s*$",
        )

    def test_security_jobs_use_trusted_base_checkers(self):
        content = IMMUTABLE_WORKFLOW_PATH.read_text(encoding="utf-8")
        block = _workflow_jobs(content)["immutable-compliance"]
        self.assertIn("path: trusted-base", block)
        self.assertIn("path: pr-head", block)
        self.assertIn('python "$TRUSTED_CHECKER"', block)
        self.assertIn('--repo "$PR_REPO" --tree "$PR_HEAD_SHA"', block)
        self.assertNotRegex(block, r"python (?:\.\./)?pr-head/")

    def test_untrusted_checks_use_the_standard_pull_request_event(self):
        sync_content = WORKFLOW_PATH.read_text(encoding="utf-8")
        compliance_content = AGENTS_MD_WORKFLOW_PATH.read_text(
            encoding="utf-8")
        self.assertEqual(
            _workflow_events(sync_content), {"push", "pull_request"})
        self.assertEqual(_workflow_events(compliance_content), {"push"})
        self.assertNotIn("pull_request_target", sync_content)
        self.assertNotIn("pull-requests: write", sync_content)
        self.assertNotIn("actions/github-script", sync_content)

    def test_pr_draft_semantics_remain_explicit(self):
        sync_jobs = _workflow_jobs(WORKFLOW_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("draft", sync_jobs["check-sync"])
        privileged = IMMUTABLE_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("draft == false", privileged)

    def test_handoff_requires_active_user_request(self):
        for path in (AGENTS_PATH, HANDOFF_PATH, README_PATH):
            content = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("acknowledged digest", content)
                self.assertIn("active-user request", content)
                self.assertNotIn("git --no-pager branch", content)
                self.assertIn("scripts/read_git_state.py", content)

    def test_handoff_prescribes_no_pre_consent_git_command(self):
        for path in (AGENTS_PATH, HANDOFF_PATH, README_PATH):
            content = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("git --no-pager --no-replace-objects", content)
                self.assertIn("Do not run Git commands before consent", content)
                self.assertIn("scripts/read_git_state.py", content)

    def test_makefile_wires_exact_lint_recipe(self):
        content = MAKEFILE_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "\t$(PYTHON) scripts/check_conflict_markers.py",
            content,
        )

    def test_pre_commit_config_wires_staged_hook(self):
        content = PRE_COMMIT_CONFIG_PATH.read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "- id: check-conflict-markers", content
        )
        self.assertIn(
            "entry: python scripts/"
            "check_conflict_markers.py --staged",
            content,
        )
        self.assertIn("always_run: true", content)
        self.assertIn("pass_filenames: false", content)


class DirectExecutionOrderingTest(unittest.TestCase):
    """Direct execution must define every late test class before running."""

    def test_direct_execution_can_run_late_test_classes(self):
        tests = [
            "SecurityHardeningTest.test_blob_size_separates_the_object_name",
            "WindowsPathHandlingTest."
            "test_attribute_lookup_survives_a_backslash_relpath",
            "SparseCheckoutTest.test_skip_worktree_entry_does_not_fail_the_check",
        ]
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "-v", *tests],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"stdout: {result.stdout}, stderr: {result.stderr}",
        )
        for class_name in (
            "SecurityHardeningTest",
            "WindowsPathHandlingTest",
            "SparseCheckoutTest",
        ):
            self.assertIn(class_name, result.stderr)


def _init_repo(path: str) -> None:
    """Create a git repo with an identity, for object-store tests."""
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.name", "test"],
        ["config", "user.email", "test@example.invalid"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True,
                       capture_output=True)


class SecurityHardeningTest(unittest.TestCase):
    """Inputs an attacker controls must not steer git or the terminal."""

    def test_replacement_ref_does_not_hide_a_staged_conflict(self):
        """git honors refs/replace by default, so cat-file can return a lie.

        The indexed blob carries markers. A replacement points it at a clean
        blob, so without --no-replace-objects the checker reads the
        replacement and reports the tree as clean.
        """
        with RetryingTemporaryDirectory() as tmp:
            _init_repo(tmp)
            target = Path(tmp) / "merged.txt"
            target.write_text(
                "<<<<<<< HEAD\nleft\n=======\nright\n>>>>>>> branch\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "merged.txt"], cwd=tmp, check=True,
                           capture_output=True)
            dirty = subprocess.run(
                ["git", "rev-parse", ":merged.txt"], cwd=tmp, check=True,
                capture_output=True, text=True).stdout.strip()
            clean = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"], cwd=tmp, check=True,
                input="left\n", capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "replace", "--force", dirty, clean],
                           cwd=tmp, check=True, capture_output=True)

            violations = checker._check_staged(tmp)

        self.assertTrue(
            violations, "a replacement ref hid conflict markers in the index")

    def test_replacement_ref_does_not_hide_a_tree_conflict(self):
        with RetryingTemporaryDirectory() as tmp:
            _init_repo(tmp)
            target = Path(tmp) / "merged.txt"
            target.write_text(
                "<<<<<<< HEAD\nleft\n=======\nright\n>>>>>>> branch\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "merged.txt"], cwd=tmp, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-qm", "test: add conflict"],
                           cwd=tmp, check=True, capture_output=True)
            treeish = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=tmp, check=True,
                capture_output=True, text=True).stdout.strip()
            dirty = subprocess.run(
                ["git", "rev-parse", "HEAD:merged.txt"], cwd=tmp, check=True,
                capture_output=True, text=True).stdout.strip()
            clean = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"], cwd=tmp, check=True,
                input="left\n", capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "replace", "--force", dirty, clean],
                           cwd=tmp, check=True, capture_output=True)

            result = subprocess.run(
                [sys.executable, str(CHECKER_PATH), "--repo", tmp,
                 "--tree", treeish],
                cwd=tmp, capture_output=True, text=True, check=False)

        self.assertEqual(result.returncode, 1)
        self.assertIn("unresolved conflict block", result.stderr)

    def test_tree_mode_rejects_malformed_object_id(self):
        with RetryingTemporaryDirectory() as tmp:
            _init_repo(tmp)
            result = subprocess.run(
                [sys.executable, str(CHECKER_PATH), "--repo", tmp,
                 "--tree", "HEAD;echo unsafe"],
                cwd=tmp, capture_output=True, text=True, check=False)

        self.assertEqual(result.returncode, 1)
        self.assertIn("validated hexadecimal object id", result.stderr)

    def test_tree_parser_rejects_truncated_and_malformed_entries(self):
        malformed_outputs = (
            b"100644 blob " + (b"a" * 40) + b"\tfile.txt",
            b"100644 blob\x00",
            b"100644 tree " + (b"a" * 40) + b"\tfile.txt\x00",
        )
        for raw in malformed_outputs:
            with self.subTest(raw=raw):
                with self.assertRaises(RuntimeError):
                    checker._parse_tree_entries(raw)

    def test_tree_mode_reads_content_and_attributes_from_the_tree(self):
        with RetryingTemporaryDirectory() as tmp:
            _init_repo(tmp)
            target = Path(tmp) / "marker.txt"
            attributes = Path(tmp) / ".gitattributes"
            target.write_text("< HEAD\n", encoding="utf-8")
            attributes.write_text(
                "marker.txt conflict-marker-size=1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=tmp, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-qm", "test: add tree"],
                           cwd=tmp, check=True, capture_output=True)
            treeish = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=tmp, check=True,
                capture_output=True, text=True).stdout.strip()
            target.write_text("clean worktree content\n", encoding="utf-8")
            attributes.write_text("marker.txt -conflict-marker-size\n",
                                  encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(CHECKER_PATH), "--repo", tmp,
                 "--tree", treeish],
                cwd=tmp, capture_output=True, text=True, check=False)

        self.assertEqual(result.returncode, 1)
        self.assertIn("marker.txt:1: unclosed conflict marker opener",
                      result.stderr)

    def test_blob_size_separates_the_object_name(self):
        """A sha beginning with a dash is read as an option without --."""
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout=b"4")
            checker._get_blob_size("deadbeef", "/repo")
        self._assert_separated(run.call_args[0][0])

    def test_read_blob_separates_the_object_name(self):
        with patch("subprocess.Popen") as popen:
            popen.return_value = MagicMock(
                returncode=0,
                communicate=MagicMock(return_value=(b"data", b"")))
            checker._read_blob("deadbeef", "/repo")
        self._assert_separated(popen.call_args[0][0])

    def _assert_separated(self, argv: list) -> None:
        """Assert argv disables replacements and ends options before the sha."""
        self.assertIn("--no-replace-objects", argv)
        self.assertIn("--", argv)
        self.assertLess(argv.index("--"), argv.index("deadbeef"),
                        "the object name is not separated from options")

    def test_symlink_probe_reports_a_failed_check(self):
        """Swallowing the probe error opens the path without knowing."""
        with RetryingTemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain.txt"
            plain.write_text("clean\n", encoding="utf-8")
            link = Path(tmp) / "link.txt"
            with patch.object(checker.os.path, "islink",
                              side_effect=[False, True]):
                self.assertIs(checker._probe_is_symlink(str(plain)), False)
                self.assertIs(checker._probe_is_symlink(str(link)), True)
            with patch.object(checker.os.path, "islink",
                              side_effect=OSError("probe failed")):
                self.assertIsNone(checker._probe_is_symlink(str(plain)))

    def test_diagnostics_escape_control_characters(self):
        """A newline in a path would otherwise forge a diagnostic line."""
        hostile = "tests/evil\n[ok] nothing to see\u202e\x1b[31m.py"
        violations = checker.check_content(
            "<<<<<<< HEAD\nl\n=======\nr\n>>>>>>> b\n", hostile)
        self.assertTrue(violations)
        for line in violations:
            self.assertNotIn("\n", line, "diagnostic spans several lines")
            self.assertNotIn("\x1b", line, "an ANSI escape reached output")
            self.assertNotIn("\u202e", line, "a bidi override reached output")


class WindowsPathHandlingTest(unittest.TestCase):
    """Git speaks forward slashes whatever the platform separator is."""

    def test_attribute_lookup_survives_a_backslash_relpath(self):
        """os.path.relpath returns backslashes on Windows, check-attr does not.

        Keying the result on the backslash spelling loses every attribute
        for a nested path, so conflict-marker-size is silently dropped.
        """
        with RetryingTemporaryDirectory() as tmp:
            nested = Path(tmp) / "nested"
            nested.mkdir()
            target = nested / "file.txt"
            target.write_text("x\n", encoding="utf-8")

            with patch.object(checker.os, "sep", "\\"), \
                 patch.object(checker.os, "altsep", None), \
                 patch.object(checker.os.path, "relpath",
                              return_value="nested\\file.txt"), \
                 patch("subprocess.run") as run:
                run.return_value = MagicMock(
                    returncode=0,
                    stdout=b"nested/file.txt\x00conflict-marker-size\x001\x00",
                    stderr=b"")
                attributes = checker.get_git_attributes([str(target)], tmp)

        found = [value for value in attributes.values() if value]
        self.assertTrue(
            found, "the attribute was dropped by the separator spelling")
        self.assertEqual(found[0].get("conflict-marker-size"), "1")


class SparseCheckoutTest(unittest.TestCase):
    """Scan skip-worktree content from its authoritative source."""

    def test_skip_worktree_entry_does_not_fail_the_check(self):
        with RetryingTemporaryDirectory() as tmp:
            _init_repo(tmp)
            target = Path(tmp) / "sparse.txt"
            target.write_text("clean content\n", encoding="utf-8")
            subprocess.run(["git", "add", "sparse.txt"], cwd=tmp, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-qm", "chore: add"], cwd=tmp,
                           check=True, capture_output=True)
            subprocess.run(["git", "update-index", "--skip-worktree",
                            "sparse.txt"], cwd=tmp, check=True,
                           capture_output=True)
            target.unlink()

            result = subprocess.run(
                [sys.executable, str(CHECKER_PATH), "--all"],
                cwd=tmp, capture_output=True, text=True, check=False)

        self.assertEqual(
            result.returncode, 0,
            f"a valid sparse checkout failed: {result.stderr}")

    def test_present_skip_worktree_entry_uses_unstaged_content(self):
        with RetryingTemporaryDirectory() as tmp:
            _init_repo(tmp)
            target = Path(tmp) / "present.txt"
            target.write_text("clean content\n", encoding="utf-8")
            subprocess.run(["git", "add", "present.txt"], cwd=tmp,
                           check=True, capture_output=True)
            subprocess.run(["git", "commit", "-qm", "test: add file"],
                           cwd=tmp, check=True, capture_output=True)
            subprocess.run(["git", "update-index", "--skip-worktree",
                            "present.txt"], cwd=tmp, check=True,
                           capture_output=True)
            target.write_text(
                "<<<<<<< HEAD\nleft\n=======\nright\n>>>>>>> branch\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, str(CHECKER_PATH), "--all"],
                cwd=tmp, capture_output=True, text=True, check=False)

        self.assertEqual(result.returncode, 1)
        self.assertIn("unresolved conflict block", result.stderr)

    def test_sparse_checkout_scans_absent_index_blob_with_cached_attributes(self):
        with RetryingTemporaryDirectory() as tmp:
            _init_repo(tmp)
            target = Path(tmp) / "sparse.txt"
            present = Path(tmp) / "present.txt"
            attributes = Path(tmp) / ".gitattributes"
            target.write_text("< HEAD\n", encoding="utf-8")
            present.write_text("clean content\n", encoding="utf-8")
            attributes.write_text(
                "sparse.txt conflict-marker-size=1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=tmp, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-qm", "test: add sparse"],
                           cwd=tmp, check=True, capture_output=True)
            subprocess.run(["git", "sparse-checkout", "init", "--no-cone"],
                           cwd=tmp, check=True, capture_output=True)
            subprocess.run(
                ["git", "sparse-checkout", "set", "--no-cone",
                 "/present.txt", "/.gitattributes"],
                cwd=tmp, check=True, capture_output=True)
            self.assertFalse(target.exists())
            index_state = subprocess.run(
                ["git", "ls-files", "-v", "sparse.txt"], cwd=tmp,
                check=True, capture_output=True, text=True).stdout
            self.assertTrue(index_state.startswith("S "), index_state)

            result = subprocess.run(
                [sys.executable, str(CHECKER_PATH), "--all"],
                cwd=tmp, capture_output=True, text=True, check=False)

        self.assertEqual(result.returncode, 1)
        self.assertIn("sparse.txt:1: unclosed conflict marker opener",
                      result.stderr)



class GitOptionSupportTest(unittest.TestCase):
    """A git without --no-lazy-fetch must still read objects."""

    def setUp(self):
        checker._supports_no_lazy_fetch.cache_clear()

    def tearDown(self):
        checker._supports_no_lazy_fetch.cache_clear()

    @staticmethod
    def _probe(stderr: bytes):
        """Return a subprocess.run stub answering the support probe."""
        return MagicMock(returncode=129 if stderr else 0, stderr=stderr,
                         stdout=b"")

    def test_unknown_option_is_read_as_no_support(self):
        """Git 2.43 rejects the option; Ubuntu 24.04 LTS ships 2.43."""
        with patch("subprocess.run",
                   return_value=self._probe(b"unknown option: --no-lazy-fetch\n")):
            self.assertFalse(checker._supports_no_lazy_fetch("."))

    def test_accepted_option_is_read_as_support(self):
        with patch("subprocess.run", return_value=self._probe(b"")):
            self.assertTrue(checker._supports_no_lazy_fetch("."))

    def test_command_omits_the_option_where_git_rejects_it(self):
        with patch("subprocess.run",
                   return_value=self._probe(b"unknown option: --no-lazy-fetch\n")):
            command = checker._object_command(".", "cat-file", "-s", "--", "abc")
        self.assertNotIn("--no-lazy-fetch", command)
        self.assertIn("--no-replace-objects", command)
        self.assertLess(command.index("--"), command.index("abc"))

    def test_command_keeps_the_option_where_git_accepts_it(self):
        with patch("subprocess.run", return_value=self._probe(b"")):
            command = checker._object_command(".", "cat-file", "-s", "--", "abc")
        self.assertIn("--no-lazy-fetch", command)
        self.assertIn("--no-replace-objects", command)

    def test_missing_support_warns_once_rather_than_per_file(self):
        """The old failure printed one error line per tracked file."""
        stub = self._probe(b"unknown option: --no-lazy-fetch\n")
        with patch("subprocess.run", return_value=stub), \
             patch("sys.stderr", new_callable=io.StringIO) as captured:
            for _ in range(5):
                checker._object_command(".", "cat-file", "-s", "--", "abc")
            warnings = captured.getvalue()
        self.assertEqual(warnings.count("--no-lazy-fetch"), 1)
        self.assertIn("no verdict changes", warnings)

    def test_the_checker_reads_objects_on_this_git(self):
        """Whatever git runs here, --staged must reach a verdict."""
        with RetryingTemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(
                ["git", "init", "-q", "-b", "main"], cwd=repo,
                capture_output=True, check=True)
            (repo / "object.txt").write_text("clean\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "object.txt"], cwd=repo,
                capture_output=True, check=True)
            result = run_checker(["--staged"], repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("unknown option", result.stderr)


if __name__ == "__main__":
    unittest.main()
