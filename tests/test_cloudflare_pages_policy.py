"""Test the shared Cloudflare Pages deployment allowance."""
import unittest
import json
import tempfile
from pathlib import Path

from hooks import _gate_core
from tests import gate_corpus


class CloudflarePagesPolicyTest(unittest.TestCase):
    """Verify the allowlist and its denial boundaries."""

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.temp_root = tempfile.TemporaryDirectory(dir=self.root)
        self.output_path = Path(self.temp_root.name) / "dist"
        self.output_path.mkdir()
        self.output_argument = self.output_path.relative_to(self.root).as_posix()
        self.workers = {}

    def tearDown(self) -> None:
        for worker in self.workers.values():
            worker.close()
        self.temp_root.cleanup()

    def verdict(self, *arguments: str) -> tuple:
        return _gate_core.cloudflare_pages_verdict(
            "wrangler", list(arguments), str(self.root))

    def hook_verdict(self, tool_name: str, command: str,
                     cwd: str = "") -> tuple:
        """Run one real shell gate and return its exit code and decision."""
        hook_name = {
            "Bash": "block_destructive_bash.py",
            "PowerShell": "block_destructive_powershell.py",
            "Cmd": "block_destructive_cmd.py",
        }[tool_name]
        worker = self.workers.get(tool_name)
        if worker is None:
            worker = gate_corpus.HookWorker(self.root / "hooks" / hook_name)
            self.workers[tool_name] = worker
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "permission_mode": "default",
            "cwd": cwd or str(self.root),
            "tool_input": {"command": command},
        }
        code, stdout, _stderr = worker.invoke(payload)
        decision = ""
        if stdout.strip():
            decision = json.loads(stdout)["hookSpecificOutput"][
                "permissionDecision"
            ]
        return code, decision

    def test_real_gates_use_payload_cwd(self) -> None:
        """Resolve Pages paths against the hook payload working directory."""
        workspace = Path(self.temp_root.name) / "isolated-workspace"
        output_path = workspace / "dist"
        output_path.mkdir(parents=True)
        command = "wrangler pages deploy dist --project-name site"
        for tool_name in ("Bash", "PowerShell", "Cmd"):
            with self.subTest(tool_name=tool_name):
                result = self.hook_verdict(tool_name, command, str(workspace))
                self.assertEqual(result, (0, ""))

    def test_real_gates_reach_pages_policy(self) -> None:
        """Exercise the shared Pages policy through every shell gate."""
        (self.output_path / "bundle.js").write_text("console.log('ok');\n")
        for tool_name in ("Bash", "PowerShell", "Cmd"):
            with self.subTest(tool_name=tool_name):
                allowed = self.hook_verdict(
                    tool_name,
                    f"wrangler pages deploy {self.output_argument} --project-name site",
                )
                denied = self.hook_verdict(tool_name, "wrangler deploy hooks")
                self.assertEqual(allowed, (0, ""))
                self.assertEqual(denied, (2, "deny"))

                protected_file = self.output_path / ".env.production"
                protected_file.write_text("TOKEN=secret\n")
                cases = (
                    "wrangler pages deploy . --project-name site",
                    "wrangler pages deploy .git --project-name site",
                    "wrangler pages deploy .agents --project-name site",
                    "wrangler pages deploy hooks --project-name site",
                    f"wrangler pages deploy {self.output_argument} --project-name",
                    f"wrangler pages deploy {self.output_argument} --project-name site --branch",
                    f"wrangler pages deploy {self.output_argument} --project-name site --branch=$BRANCH",
                    f"wrangler pages deploy {self.output_argument} --project-name site --project-name other",
                    f"wrangler pages deploy {self.output_argument} --project-name site --unsupported",
                    f"wrangler pages deploy {self.output_argument} --branch preview",
                    f"wrangler pages deploy {self.output_argument} --project-name site",
                    "wrangler pages deploy $PATH --project-name site",
                    "wrangler pages deploy .. --project-name site",
                )
                for command in cases:
                    with self.subTest(tool_name=tool_name, command=command):
                        self.assertEqual(self.hook_verdict(tool_name, command)[0], 2)
                protected_file.unlink()

    def test_allows_named_pages_deployment(self) -> None:
        self.assertEqual(
            self.verdict("pages", "deploy", self.output_argument,
                         "--project-name", "time-chime"),
            ("", ""),
        )

    def test_allows_preview_branch(self) -> None:
        self.assertEqual(
            self.verdict("pages", "deploy", self.output_argument,
                         "--project-name=site",
                         "--branch", "preview"),
            ("", ""),
        )

    def test_scans_nonempty_output_entries(self) -> None:
        (self.output_path / "bundle.js").write_text("console.log('ok');\n")
        self.assertEqual(
            self.verdict("pages", "deploy", self.output_argument,
                         "--project-name", "site"),
            ("", ""),
        )

    def test_prohibited_classifier_does_not_classify_pages(self) -> None:
        """Leave Wrangler policy decisions to the CWD-aware shell gates."""
        self.assertEqual(
            _gate_core.prohibited_command_verdict(
                "wrangler", ["pages", "deploy", ".", "--project-name", "site"]),
            ("", ""),
        )

    def test_rejects_other_wrangler_commands(self) -> None:
        self.assertEqual(self.verdict("deploy", "dist" )[0], "deny")
        self.assertEqual(self.verdict("pages", "project", "list")[0], "deny")
        self.assertEqual(self.verdict("pages", "secret", "put", "KEY")[0], "deny")

    def test_rejects_missing_or_extra_options(self) -> None:
        self.assertEqual(
            self.verdict("pages", "deploy", self.output_argument)[0], "deny"
        )
        self.assertEqual(
            self.verdict("pages", "deploy", self.output_argument,
                         "--project-name", "site",
                         "--commit-dirty")[0], "deny")

    def test_rejects_paths_outside_workspace(self) -> None:
        self.assertEqual(
            self.verdict("pages", "deploy", "..", "--project-name", "site")[0],
            "deny",
        )

    def test_rejects_ambiguous_values(self) -> None:
        self.assertEqual(
            self.verdict("pages", "deploy", self.output_argument,
                         "--project-name", "$NAME")[0],
            "deny",
        )


if __name__ == "__main__":
    unittest.main()
