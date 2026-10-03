#!/usr/bin/env python3
"""Test gate adoption completeness checking (AGENTS.md Rule 18)."""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_gate_adoption

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
LIVE_SETTINGS = ".claude/settings.json"


def _run(root: Path) -> tuple:
    """Return the exit code and stderr text for one checked root."""
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = check_gate_adoption.main(["--root", str(root)])
    return code, stderr.getvalue()


class LiveRepositoryTest(unittest.TestCase):
    """The source repository carries the complete gate set."""

    def test_live_repository_passes(self):
        code, output = _run(REPOSITORY_ROOT)
        self.assertEqual(code, 0, output)

    def test_every_required_registration_names_a_present_hook(self):
        """A table entry naming an absent hook would never fail."""
        for hook in check_gate_adoption.REQUIRED_REGISTRATIONS:
            with self.subTest(hook=hook):
                self.assertTrue((REPOSITORY_ROOT / "hooks" / hook).is_file())


class PartialAdoptionTest(unittest.TestCase):
    """An incomplete copy reports the absent artifact by name."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "adopter"
        self.root.mkdir()
        self.addCleanup(self.temporary.cleanup)

    def _copy_full_tree(self) -> Path:
        """Copy the live gate set into the temporary adopter root."""
        for relative in (
            "hooks",
            "scripts",
            "tests",
            ".claude",
            ".codex",
            ".agents",
            ".gemini",
        ):
            shutil.copytree(REPOSITORY_ROOT / relative, self.root / relative)
        for relative in check_gate_adoption.REQUIRED_POLICY:
            shutil.copy(REPOSITORY_ROOT / relative, self.root / relative)
        return self.root

    def _settings_document(self) -> dict:
        """Return the copied live settings document."""
        path = self.root / LIVE_SETTINGS
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_settings(self, document: dict) -> None:
        """Write one settings document into the adopter root."""
        path = self.root / LIVE_SETTINGS
        path.write_text(json.dumps(document), encoding="utf-8")

    def test_full_copy_passes(self):
        code, output = _run(self._copy_full_tree())
        self.assertEqual(code, 0, output)

    def test_hooks_only_copy_reports_registrations_and_checkers(self):
        shutil.copytree(REPOSITORY_ROOT / "hooks", self.root / "hooks")
        code, output = _run(self.root)
        self.assertEqual(code, 1)
        self.assertIn("shared-files.json is absent", output)
        self.assertIn("scripts/check_branch_name.py is absent", output)
        self.assertIn(f"{LIVE_SETTINGS} is absent or unreadable", output)
        self.assertIn("forbids partial hook or gate adoption", output)

    def test_absent_shared_module_reports_importing_gate(self):
        self._copy_full_tree()
        (self.root / "hooks" / "_gate_core.py").unlink()
        code, output = _run(self.root)
        self.assertEqual(code, 1)
        self.assertIn("hooks/_gate_core.py is absent", output)
        self.assertIn("imports hooks/_gate_core.py", output)
        self.assertIn("exits 2 at run time", output)

    def test_removed_registration_reports_hook_and_event(self):
        self._copy_full_tree()
        document = self._settings_document()
        groups = document["hooks"]["PreToolUse"]
        document["hooks"]["PreToolUse"] = [
            group for group in groups
            if "require_consent.py" not in json.dumps(group)
        ]
        self._write_settings(document)
        code, output = _run(self.root)
        self.assertEqual(code, 1)
        self.assertIn("does not register require_consent.py under "
                      "PreToolUse", output)

    def test_narrowed_matcher_reports_the_expected_matcher(self):
        self._copy_full_tree()
        document = self._settings_document()
        for group in document["hooks"]["PreToolUse"]:
            if group.get("matcher") == check_gate_adoption.EDIT_MATCHER:
                group["matcher"] = "Write"
        self._write_settings(document)
        code, output = _run(self.root)
        self.assertEqual(code, 1)
        self.assertIn(check_gate_adoption.EDIT_MATCHER, output)

    def test_unreadable_configuration_fails(self):
        self._copy_full_tree()
        (self.root / LIVE_SETTINGS).write_text("{", encoding="utf-8")
        code, output = _run(self.root)
        self.assertEqual(code, 1)
        self.assertIn(f"{LIVE_SETTINGS} is absent or unreadable", output)

    def test_non_mapping_configuration_reports_the_type(self):
        """A top-level array registers no hook and must not raise."""
        self._copy_full_tree()
        path = self.root / LIVE_SETTINGS
        path.write_text(json.dumps([{"hooks": {}}]), encoding="utf-8")
        code, output = _run(self.root)
        self.assertEqual(code, 1)
        self.assertIn("holds list at the top level", output)

    def test_null_hooks_property_reports_every_registration(self):
        """A null hooks branch reports findings rather than raising."""
        self._copy_full_tree()
        path = self.root / LIVE_SETTINGS
        path.write_text(json.dumps({"hooks": None}), encoding="utf-8")
        code, output = _run(self.root)
        self.assertEqual(code, 1)
        self.assertIn("does not register require_consent.py", output)

    def test_malformed_event_and_group_shapes_report_findings(self):
        """A mistyped event branch or group entry must not raise."""
        self._copy_full_tree()
        document = {"hooks": {"PreToolUse": {"matcher": "Bash"},
                              "SessionStart": ["text", {"hooks": None}]}}
        self._write_settings(document)
        code, output = _run(self.root)
        self.assertEqual(code, 1)
        self.assertIn("does not register enforce_branch_name.py", output)

    def test_absent_shared_manifest_fails(self):
        self._copy_full_tree()
        (self.root / check_gate_adoption.SHARED_MANIFEST).unlink()
        code, output = _run(self.root)
        self.assertEqual(code, 1)
        self.assertIn("shared-files.json is absent", output)

    def test_non_mapping_shared_manifest_reports_the_type(self):
        """A top-level array lists no shared file and must not raise."""
        self._copy_full_tree()
        path = self.root / check_gate_adoption.SHARED_MANIFEST
        path.write_text(json.dumps(["hooks/_gate_core.py"]), encoding="utf-8")
        code, output = _run(self.root)
        self.assertEqual(code, 1)
        self.assertIn("holds list at the top level", output)

    def test_empty_shared_manifest_fails(self):
        self._copy_full_tree()
        path = self.root / check_gate_adoption.SHARED_MANIFEST
        path.write_text(json.dumps({"shared": {}}), encoding="utf-8")
        code, output = _run(self.root)
        self.assertEqual(code, 1)
        self.assertIn("lists no shared gate files", output)


class SiblingImportTest(unittest.TestCase):
    """Underscore-prefixed imports identify sibling gate modules."""

    def test_underscore_imports_are_collected(self):
        source = "import os\nfrom _gate_core import verdict\nimport _bash_parser\n"
        self.assertEqual(
            check_gate_adoption._sibling_imports(source),
            {"_gate_core", "_bash_parser"},
        )

    def test_standard_library_imports_are_ignored(self):
        source = "import json\nfrom pathlib import Path\n"
        self.assertEqual(check_gate_adoption._sibling_imports(source), set())


if __name__ == "__main__":
    unittest.main()
