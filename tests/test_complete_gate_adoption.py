#!/usr/bin/env python3
"""Test staged complete-gate transaction path controls."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "complete_gate_adoption.py"


def load_script():
    """Load the transaction installer from its checked-in path."""
    spec = importlib.util.spec_from_file_location("complete_gate_adoption", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CompleteGateAdoptionTest(unittest.TestCase):
    """The installer accepts only a complete candidate below fixed staging."""

    def setUp(self) -> None:
        self.script = load_script()

    def test_candidate_requires_fixed_staging_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged = root / ".gate-staging" / "release"
            staged.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            self.assertEqual(self.script._candidate(root, str(staged)), staged.resolve())
            with self.assertRaises(ValueError):
                self.script._candidate(root, str(outside))

    def test_candidate_paths_include_all_transaction_configs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary)
            (candidate / "shared-files.json").write_text(
                json.dumps({"shared": {"hooks/example.py": "digest"}}),
                encoding="utf-8",
            )
            paths = self.script._candidate_paths(candidate)
        self.assertIn("hooks/example.py", paths)
        self.assertIn(".claude/settings.json", paths)
        self.assertIn(".codex/config.toml", paths)

    def test_target_path_normalizes_root_before_containment_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested"
            nested.mkdir()
            unresolved_root = nested / ".."
            target = self.script._target_path(unresolved_root, "hooks/example.py")
        self.assertEqual(target, (root / "hooks" / "example.py").resolve())

    def test_transaction_configs_copy_last(self) -> None:
        paths = self.script._ordered_paths([
            ".claude/settings.json",
            "hooks/example.py",
            ".codex/hooks.json",
        ])
        self.assertEqual(paths[0], "hooks/example.py")
        self.assertEqual(paths[-2:], [".claude/settings.json", ".codex/hooks.json"])

    def test_install_failure_restores_prior_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / ".gate-staging" / "release"
            first = "hooks/first.py"
            second = "hooks/second.py"
            for relative, content in ((first, "new first"), (second, "new second")):
                path = candidate / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            (root / first).parent.mkdir(parents=True)
            (root / first).write_text("old first", encoding="utf-8")
            (root / second).write_text("old second", encoding="utf-8")
            original_copy = self.script.shutil.copyfile

            def fail_second(source, destination):
                if Path(source) == candidate / second:
                    raise OSError("simulated failure")
                return original_copy(source, destination)

            with patch.object(self.script.shutil, "copyfile", side_effect=fail_second):
                with self.assertRaises(OSError):
                    self.script._install_paths(root, candidate, [first, second])

            self.assertEqual((root / first).read_text(encoding="utf-8"), "old first")
            self.assertEqual((root / second).read_text(encoding="utf-8"), "old second")

    def test_install_failure_removes_transaction_owned_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / ".gate-staging" / "release"
            new_path = "new/directory/first.py"
            failing_path = "hooks/second.py"
            for relative in (new_path, failing_path):
                path = candidate / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("new content", encoding="utf-8")
            original_copy = self.script.shutil.copyfile

            def fail_second(source, destination):
                if Path(source) == candidate / failing_path:
                    raise OSError("simulated failure")
                return original_copy(source, destination)

            with patch.object(self.script.shutil, "copyfile", side_effect=fail_second):
                with self.assertRaises(OSError):
                    self.script._install_paths(root, candidate, [new_path, failing_path])

            self.assertFalse((root / new_path).exists())
            self.assertFalse((root / "new").exists())

    def test_validation_failure_restores_replaced_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / ".gate-staging" / "release"
            relative = "hooks/example.py"
            source = candidate / relative
            source.parent.mkdir(parents=True)
            source.write_text("new content", encoding="utf-8")
            target = root / relative
            target.parent.mkdir(parents=True)
            target.write_text("old content", encoding="utf-8")

            def fail_validation() -> None:
                raise OSError("simulated validation failure")

            with self.assertRaises(OSError):
                self.script._install_paths(root, candidate, [relative], fail_validation)

            self.assertEqual(target.read_text(encoding="utf-8"), "old content")


if __name__ == "__main__":
    unittest.main()
