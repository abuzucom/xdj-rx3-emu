"""Test Cloudflare Pages deployment path safeguards."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hooks import _gate_core


class CloudflarePagesPathSecurityTest(unittest.TestCase):
    """Reject repository and credential paths from Pages deployment."""

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.temp_root = tempfile.TemporaryDirectory(dir=self.root)
        self.output_path = Path(self.temp_root.name) / "dist"
        self.output_path.mkdir()
        self.output_argument = self.output_path.relative_to(self.root).as_posix()

    def tearDown(self) -> None:
        self.temp_root.cleanup()

    def verdict(self, path: str) -> tuple:
        return _gate_core.cloudflare_pages_verdict(
            "wrangler",
            ["pages", "deploy", path, "--project-name", "site"],
            str(self.root),
        )

    def test_rejects_workspace_root_and_hidden_directories(self) -> None:
        for path in (".", ".git", ".agents"):
            with self.subTest(path=path):
                self.assertEqual(self.verdict(path)[0], "deny")

    def test_bash_gate_reaches_path_safeguards(self) -> None:
        """Exercise every path safeguard through the real Bash entrypoint."""
        hook_path = self.root / "hooks" / "block_destructive_bash.py"
        environment = os.environ.copy()
        (self.output_path / "bundle.js").write_text("console.log('ok');\n")
        allowed_commands = (
            f"wrangler pages deploy {self.output_argument} --project-name site",
            f"wrangler pages deploy {self.output_argument} "
            "--project-name=site --branch=preview",
        )
        for allowed_command in allowed_commands:
            allowed_payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "permission_mode": "default",
                "cwd": str(self.root),
                "tool_input": {"command": allowed_command},
            }
            allowed_result = subprocess.run(
                [sys.executable, str(hook_path)],
                input=json.dumps(allowed_payload) + "\n",
                capture_output=True,
                cwd=self.root,
                env=environment,
                text=True,
                check=False,
            )
            with self.subTest(command=allowed_command):
                self.assertEqual(
                    allowed_result.returncode,
                    0,
                    allowed_result.stdout + allowed_result.stderr,
                )
        protected_file = self.output_path / ".env.production"
        protected_file.write_text("TOKEN=secret\n")
        protected_target = Path(self.temp_root.name) / "credentials"
        protected_target.mkdir()
        protected_argument = protected_target.relative_to(self.root).as_posix()
        commands = (
            f"wrangler pages deploy {self.output_argument} --project-name",
            f"wrangler pages deploy {self.output_argument} --project-name site --branch",
            f"wrangler pages deploy {self.output_argument} --project-name site --branch=$BRANCH",
            f"wrangler pages deploy {self.output_argument} --project-name site --unsupported",
            f"wrangler pages deploy {self.output_argument} --branch preview",
            f"wrangler pages deploy {self.output_argument} --project-name site",
            f"wrangler pages deploy {self.output_argument} --project-name=$NAME",
            f"wrangler pages deploy {protected_argument} --project-name site",
            "wrangler pages deploy . --project-name site",
            "wrangler pages deploy .git --project-name site",
            "wrangler pages deploy .agents --project-name site",
            "wrangler pages deploy hooks --project-name site",
        )
        for command in commands:
            payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "permission_mode": "default",
                "cwd": str(self.root),
                "tool_input": {"command": command},
            }
            result = subprocess.run(
                [sys.executable, str(hook_path)],
                input=json.dumps(payload) + "\n",
                capture_output=True,
                cwd=self.root,
                env=environment,
                text=True,
                check=False,
            )
            with self.subTest(command=command):
                self.assertEqual(result.returncode, 2)
        protected_file.unlink()

    def test_rejects_protected_target_and_contents(self) -> None:
        self.assertEqual(self.verdict("credentials")[0], "deny")
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            output_path = Path(directory)
            (output_path / ".env.production").write_text("TOKEN=secret\n")
            self.assertEqual(self.verdict(output_path.name)[0], "deny")

    def test_rejects_symlinks_to_protected_content(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            output_path = Path(directory)
            protected_target = self.root / ".git" / "config"
            link = output_path / "bundle.js"
            try:
                link.symlink_to(protected_target)
            except OSError as error:
                self.skipTest(f"platform denied symlink creation: {error}")
            self.assertEqual(self.verdict(output_path.name)[0], "deny")


if __name__ == "__main__":
    unittest.main()
