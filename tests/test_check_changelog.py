"""Test the blocking SemVer changelog checker."""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

CHECKER_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_changelog.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_changelog", CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


class ChangelogCheckerTest(unittest.TestCase):
    """Exercise changelog rules through the checker API."""

    def test_valid_versioned_changelog_passes(self):
        text = """# Changelog\n\n## [2.0.0] (2026-09-13)\n\n### Added\n- Add linked policy loading.\n\n## [1.14.0] (2026-09-12)\n\n### Fixed\n- Fix a gate.\n"""
        self.assertEqual(checker.find_violations(text), [])

    def test_unreleased_heading_fails(self):
        text = "# Changelog\n\n## [Unreleased]\n\n- Pending change.\n"
        findings = checker.find_violations(text)
        self.assertTrue(any("Unreleased" in finding for finding in findings))
        self.assertEqual(sum("line 3:" in finding for finding in findings), 1)

    def test_invalid_version_order_fails(self):
        text = """# Changelog\n\n## [1.0.0] (2026-09-13)\n\n- New.\n\n## [2.0.0] (2026-09-12)\n\n- Old.\n"""
        findings = checker.find_violations(text)
        self.assertTrue(any("descending" in finding for finding in findings))

    def test_missing_versioned_entry_fails(self):
        text = "# Changelog\n\n## [2.0.0] (2026-09-13)\n"
        findings = checker.find_violations(text)
        self.assertTrue(any("entry" in finding for finding in findings))

    def test_malformed_changelog_fails(self):
        findings = checker.find_violations("not a changelog")
        self.assertTrue(any("no versioned" in finding for finding in findings))

    def test_range_requires_version_advance_and_entry(self):
        base = "## [2.0.0] (2026-09-13)\n\n### Added\n- Old.\n"
        findings = checker.find_range_violations(base, base, ["README.md"])
        self.assertTrue(findings)

    def test_range_accepts_new_versioned_entry(self):
        base = "## [2.0.0] (2026-09-13)\n\n### Added\n- Old.\n"
        head = "## [2.0.1] (2026-09-14)\n\n### Fixed\n- New.\n\n" + base
        findings = checker.find_range_violations(
            base, head, ["README.md", "CHANGELOG.md"])
        self.assertEqual(findings, [])

    def test_range_missing_base_changelog_passes_with_versioned_head(self):
        head = "## [1.0.0] (2026-09-13)\n\n### Added\n- First release.\n"
        findings = checker.find_range_violations("", head, ["CHANGELOG.md"])
        self.assertEqual(findings, [])

    def test_range_unversioned_boilerplate_base_passes_with_versioned_head(self):
        base = (
            "# Changelog\n\n"
            "All notable changes to this project will be documented here.\n\n"
            "## [Unreleased]\n"
        )
        head = "## [1.0.0] (2026-09-13)\n\n### Added\n- First release.\n"
        findings = checker.find_range_violations(base, head, ["CHANGELOG.md"])
        self.assertEqual(findings, [])

    def test_range_missing_base_changelog_still_requires_head_version(self):
        findings = checker.find_range_violations("", "", ["CHANGELOG.md"])
        self.assertTrue(findings)

    def test_revision_arguments_reject_options(self):
        self.assertFalse(checker.valid_revision("-s:CHANGELOG.md"))
        self.assertTrue(checker.valid_revision("HEAD~1"))
        self.assertTrue(checker.valid_revision("main^"))

    def test_pre_release_versions_order_and_uniqueness(self):
        text = ("## [1.0.0] (2026-09-14)\n\n- Final.\n"
                "## [1.0.0-rc.1] (2026-09-13)\n\n- RC.\n")
        self.assertEqual(checker.find_violations(text), [])

    def test_undated_release_heading_fails(self):
        findings = checker.find_violations("## [1.0.0]\n\n- Change.\n")
        self.assertTrue(any("invalid version heading" in item for item in findings))

    def test_staged_version_regression_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            (repository / "CHANGELOG.md").write_text(
                "## [1.0.0]\n\n- New.\n", encoding="utf-8")
            self.assertNotEqual(checker.check_staged(repository), 0)

    def test_range_check_passes_for_a_repositorys_first_changelog(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            _run_git(repository, "init", "-q", "-b", "main")
            _run_git(repository, "config", "user.name", "Test User")
            _run_git(repository, "config", "user.email", "test@example.com")
            (repository / "README.md").write_text("# Project\n", encoding="utf-8")
            _run_git(repository, "add", "README.md")
            _run_git(repository, "commit", "-q", "-m", "feat: initial commit")
            base = _run_git(repository, "rev-parse", "HEAD").stdout.strip()
            (repository / "CHANGELOG.md").write_text(
                "## [1.0.0] (2026-09-13)\n\n### Added\n- First release.\n",
                encoding="utf-8")
            _run_git(repository, "add", "CHANGELOG.md")
            _run_git(repository, "commit", "-q", "-m", "docs: add changelog")
            head = _run_git(repository, "rev-parse", "HEAD").stdout.strip()
            self.assertEqual(checker.check_range(repository, base, head), 0)


def _run_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess:
    """Run a real git command in a throwaway test repository."""
    return subprocess.run(
        ["git", *arguments], cwd=repository, check=True,
        capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
