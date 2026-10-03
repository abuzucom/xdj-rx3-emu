import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = ROOT / "hooks" / "reinject_agents_policy.py"


def load_hook():
    spec = importlib.util.spec_from_file_location("reinject_supporting", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SupportingPolicyTests(unittest.TestCase):
    def test_load_policy_assembles_supporting_file(self):
        hook = load_hook()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("canonical\n", encoding="utf-8")
            supporting = root / "docs" / "agent-policy"
            supporting.mkdir(parents=True)
            (supporting / "adoption.md").write_text(
                "supporting\n", encoding="utf-8")
            for relative_name in hook.SUPPORTING_POLICY_FILES[1:]:
                (root / relative_name).write_text("detail\n", encoding="utf-8")
            policy, _digest = hook.load_policy(root)
        self.assertTrue(policy.startswith("canonical\n"))
        self.assertIn("supporting\n", policy)

    def test_load_policy_assembles_source_only_file_when_present(self):
        hook = load_hook()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("canonical\n", encoding="utf-8")
            for relative_name in hook.SUPPORTING_POLICY_FILES:
                path = root / relative_name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("detail\n", encoding="utf-8")
            source_only = root / hook.SOURCE_ONLY_POLICY_FILES[0]
            source_only.write_text("source detail\n", encoding="utf-8")
            policy, _digest = hook.load_policy(root)
        self.assertIn("source detail\n", policy)

    def test_load_policy_rejects_non_regular_supporting_file(self):
        hook = load_hook()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("canonical\n", encoding="utf-8")
            supporting = root / "docs" / "agent-policy"
            supporting.mkdir(parents=True)
            (supporting / "adoption.md").mkdir()
            with self.assertRaisesRegex(ValueError, "regular file"):
                hook.load_policy(root)

    def test_load_policy_rejects_missing_supporting_file(self):
        hook = load_hook()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("canonical\n", encoding="utf-8")
            (root / "docs" / "agent-policy").mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "missing"):
                hook.load_policy(root)

    def test_load_policy_rejects_missing_supporting_directory(self):
        hook = load_hook()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("canonical\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing"):
                hook.load_policy(root)

    def test_load_policy_rejects_non_ascii_supporting_file(self):
        hook = load_hook()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("canonical\n", encoding="utf-8")
            supporting = root / "docs" / "agent-policy"
            supporting.mkdir(parents=True)
            for relative_name in hook.SUPPORTING_POLICY_FILES:
                path = root / relative_name
                path.write_bytes(b"detail\n")
            (supporting / "security.md").write_bytes(b"bad\xc3\xa9\n")
            with self.assertRaisesRegex(ValueError, "non-ASCII"):
                hook.load_policy(root)

    def test_load_policy_rejects_oversized_supporting_file(self):
        hook = load_hook()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("canonical\n", encoding="utf-8")
            for relative_name in hook.SUPPORTING_POLICY_FILES:
                path = root / relative_name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("detail\n", encoding="utf-8")
            oversized = root / hook.SUPPORTING_POLICY_FILES[0]
            oversized.write_bytes(b"x" * (hook.MAX_POLICY_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "exceeds the policy size limit"):
                hook.load_policy(root)

    def test_load_policy_rejects_oversized_canonical_file(self):
        hook = load_hook()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_bytes(
                b"x" * (hook.MAX_POLICY_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "bounded regular file"):
                hook.load_policy(root)

    def test_load_policy_rejects_non_regular_canonical_file(self):
        hook = load_hook()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").mkdir()
            with self.assertRaisesRegex(ValueError, "bounded regular file"):
                hook.load_policy(root)

    def test_subprocess_rejects_oversized_supporting_file(self):
        hook = load_hook()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("canonical\n", encoding="utf-8")
            for relative_name in hook.SUPPORTING_POLICY_FILES:
                path = root / relative_name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("detail\n", encoding="utf-8")
            (root / hook.SUPPORTING_POLICY_FILES[0]).write_bytes(
                b"x" * (hook.MAX_POLICY_BYTES + 1))
            result = subprocess.run(
                [sys.executable, "-c", (
                    "import importlib.util, pathlib, sys; "
                    "spec = importlib.util.spec_from_file_location('hook', sys.argv[1]); "
                    "module = importlib.util.module_from_spec(spec); "
                    "spec.loader.exec_module(module); "
                    "module.load_policy(pathlib.Path(sys.argv[2]))"),
                 str(HOOK_PATH), str(root)],
                capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)

    def test_subprocess_rejects_assembled_policy_overflow(self):
        hook = load_hook()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_bytes(b"x" * 20000)
            for relative_name in hook.SUPPORTING_POLICY_FILES:
                path = root / relative_name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * 10000)
            result = subprocess.run(
                [sys.executable, "-c", (
                    "import importlib.util, pathlib, sys; "
                    "spec = importlib.util.spec_from_file_location('hook', sys.argv[1]); "
                    "module = importlib.util.module_from_spec(spec); "
                    "spec.loader.exec_module(module); "
                    "module.load_policy(pathlib.Path(sys.argv[2]))"),
                 str(HOOK_PATH), str(root)],
                capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)

    def test_subprocess_rejects_escaping_supporting_file(self):
        hook = load_hook()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("canonical\n", encoding="utf-8")
            for relative_name in hook.SUPPORTING_POLICY_FILES:
                path = root / relative_name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("detail\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-c", (
                    "import importlib.util, pathlib, sys; "
                    "original = pathlib.Path.resolve; "
                    "pathlib.Path.resolve = lambda self: pathlib.Path('outside') "
                    "if str(self).endswith('adoption.md') else original(self); "
                    "spec = importlib.util.spec_from_file_location('hook', sys.argv[1]); "
                    "module = importlib.util.module_from_spec(spec); "
                    "spec.loader.exec_module(module); "
                    "module.load_policy(pathlib.Path(sys.argv[2]))"),
                 str(HOOK_PATH), str(root)],
                capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
