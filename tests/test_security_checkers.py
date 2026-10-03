#!/usr/bin/env python3
"""Adversarial coverage for the repository's security checkers."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_ROOT = REPO_ROOT / "scripts"


def _load_checker(name: str):
    """Load a checker directly from its script path."""
    path = SCRIPTS_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load checker: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


persist_credentials = _load_checker("check_persist_credentials")
dockerfile_root = _load_checker("check_dockerfile_root")
weak_hashing = _load_checker("check_weak_hashing")
banned_agents = _load_checker("check_banned_agents")
secrets_heuristic = _load_checker("check_secrets_heuristic")
branch_name = _load_checker("check_branch_name")


def _commit(body: str) -> dict:
    """Return clean commit metadata with the supplied message body."""
    return {
        "sha": "a" * 40,
        "author_name": "Human Author",
        "author_email": "author@example.invalid",
        "committer_name": "Human Committer",
        "committer_email": "committer@example.invalid",
        "body": body,
    }


class PersistCredentialsTest(unittest.TestCase):
    """Only a valid YAML boolean false satisfies Rule 11."""

    def test_comment_only_false_does_not_bypass_the_check(self):
        text = (
            "jobs:\n"
            "  test:\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "        # persist-credentials: false\n"
        )

        self.assertTrue(persist_credentials.find_violations(text, "ci.yml"))

    def test_boolean_false_passes(self):
        text = (
            "jobs:\n"
            "  test:\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "        with:\n"
            "          persist-credentials: false\n"
        )

        self.assertEqual(persist_credentials.find_violations(text, "ci.yml"), [])

    def test_quoted_false_fails(self):
        for value in ('"false"', "'false'"):
            with self.subTest(value=value):
                text = (
                    "jobs:\n"
                    "  test:\n"
                    "    steps:\n"
                    "      - uses: actions/checkout@v4\n"
                    "        with:\n"
                    f"          persist-credentials: {value}\n"
                )
                self.assertTrue(
                    persist_credentials.find_violations(text, "ci.yml")
                )

    def test_checkout_action_matching_is_case_insensitive(self):
        text = "steps:\n  - uses: Actions/Checkout@v4\n"

        self.assertTrue(persist_credentials.find_violations(text, "ci.yml"))

    def test_exception_text_inside_block_scalar_does_not_bypass(self):
        text = (
            "steps:\n"
            "  - uses: actions/checkout@v4\n"
            "    env:\n"
            "      NOTE: |\n"
            "        # persist-credentials: true: this job reads history "
            "(Rule 11 exception).\n"
        )

        self.assertTrue(persist_credentials.find_violations(text, "ci.yml"))

    def test_exception_text_inside_quoted_scalar_does_not_bypass(self):
        text = (
            "steps:\n  - uses: actions/checkout@v4\n    env:\n"
            "      NOTE: '\n"
            "        # persist-credentials: true: this job reads history "
            "(Rule 11 exception).\n        '\n"
        )

        self.assertTrue(persist_credentials.find_violations(text, "ci.yml"))

    def test_malformed_yaml_fails_closed(self):
        text = (
            "jobs:\n"
            "  test:\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "        with:\n"
            "\t  persist-credentials: false\n"
        )

        self.assertTrue(persist_credentials.find_violations(text, "ci.yml"))


class ContainerRootTest(unittest.TestCase):
    """Final runtime identities govern every container independently."""

    def test_final_dockerfile_user_root_overrides_earlier_user(self):
        text = (
            "FROM python:3.12-slim\n"
            "RUN useradd -m appuser\n"
            "USER appuser\n"
            "USER root\n"
            'CMD ["python", "app.py"]\n'
        )

        self.assertTrue(dockerfile_root.find_violations(text, "Dockerfile"))

    def test_compose_root_and_zero_users_fail(self):
        for user in ("root", "0", "+0"):
            with self.subTest(user=user):
                text = (
                    "services:\n"
                    "  api:\n"
                    "    image: example.invalid/app:1\n"
                    f"    user: {user}\n"
                )
                self.assertTrue(
                    dockerfile_root.find_violations(text, "compose.yml")
                )

    def test_container_setting_can_override_pod_wide_non_root(self):
        text = (
            "apiVersion: v1\n"
            "kind: Pod\n"
            "spec:\n"
            "  securityContext:\n"
            "    runAsNonRoot: true\n"
            "  containers:\n"
            "    - name: inherited\n"
            "      image: example.invalid/safe:1\n"
            "    - name: overridden\n"
            "      image: example.invalid/unsafe:1\n"
            "      securityContext:\n"
            "        runAsNonRoot: false\n"
        )

        violations = dockerfile_root.find_violations(text, "pod.yml")

        self.assertEqual(len(violations), 1)
        self.assertIn("overridden", violations[0])

    def test_each_kubernetes_container_is_checked_independently(self):
        text = (
            "apiVersion: v1\n"
            "kind: Pod\n"
            "spec:\n"
            "  containers:\n"
            "    - name: safe\n"
            "      image: example.invalid/safe:1\n"
            "      securityContext:\n"
            "        runAsNonRoot: true\n"
            "    - name: missing\n"
            "      image: example.invalid/missing:1\n"
            "    - name: disabled\n"
            "      image: example.invalid/disabled:1\n"
            "      securityContext:\n"
            "        runAsNonRoot: false\n"
        )

        violations = dockerfile_root.find_violations(text, "pod.yml")

        self.assertEqual(len(violations), 2)
        messages = "\n".join(violations)
        self.assertIn("missing", messages)
        self.assertIn("disabled", messages)
        self.assertNotIn("safe", messages)

    def test_dockerfile_continuation_cannot_hide_root(self):
        text = "FROM example.invalid/base:1\nUSER \\\nroot\n"

        self.assertTrue(dockerfile_root.find_violations(text, "Dockerfile"))

    def test_dockerfile_heredoc_user_is_not_an_instruction(self):
        text = (
            "FROM example.invalid/base:1\n"
            "USER root\n"
            "RUN <<EOF\n"
            "USER appuser\n"
            "EOF\n"
        )

        self.assertTrue(dockerfile_root.find_violations(text, "Dockerfile"))

    def test_dockerfile_escape_directive_is_honored(self):
        text = "# escape=`\nFROM example.invalid/base:1\nUSER `\nroot\n"

        self.assertTrue(dockerfile_root.find_violations(text, "Dockerfile"))

    def test_multiple_heredocs_cannot_forge_user_instruction(self):
        text = (
            "FROM example.invalid/base:1\nUSER root\n"
            "RUN <<FIRST <<SECOND\nfirst\nFIRST\n"
            "USER appuser\nSECOND\n"
        )

        self.assertTrue(dockerfile_root.find_violations(text, "Dockerfile"))

    def test_variable_runtime_users_fail_closed(self):
        cases = (
            ("FROM example.invalid/base:1\nUSER ${APP_USER}\n", "Dockerfile"),
            (
                "services:\n  api:\n    image: example.invalid/app:1\n"
                "    user: \"${UID:-0}\"\n",
                "compose.yml",
            ),
        )
        for text, path in cases:
            with self.subTest(path=path):
                self.assertTrue(dockerfile_root.find_violations(text, path))

    def test_exception_text_inside_container_scalar_does_not_bypass(self):
        text = (
            "apiVersion: v1\nkind: Pod\nspec:\n  containers:\n"
            "    - name: unsafe\n      image: example.invalid/app:1\n"
            "      command: |\n"
            "        # runtime-root: this container needs root "
            "(Rule 12 exception).\n"
        )

        self.assertTrue(dockerfile_root.find_violations(text, "pod.yml"))

    def test_exception_text_inside_quoted_container_scalar_does_not_bypass(self):
        text = (
            "apiVersion: v1\nkind: Pod\nspec:\n  containers:\n"
            "    - name: unsafe\n      image: example.invalid/app:1\n"
            "      command: '\n"
            "        # runtime-root: this container needs root "
            "(Rule 12 exception).\n        '\n"
        )

        self.assertTrue(dockerfile_root.find_violations(text, "pod.yml"))

    def test_compose_merge_can_override_defaults(self):
        text = (
            "x-service: &service\n  image: example.invalid/app:1\n"
            "  user: appuser\nservices:\n  api:\n    <<: *service\n"
            "    user: otheruser\n"
        )

        self.assertEqual(dockerfile_root.find_violations(text, "compose.yml"), [])


class WeakHashingTest(unittest.TestCase):
    """Python aliases and strings cannot conceal weak hash calls."""

    def test_hashlib_new_md5_fails(self):
        text = "import hashlib\ndigest = hashlib.new('md5', payload)\n"

        self.assertEqual(len(weak_hashing.find_violations(text, "hashes.py")), 1)

    def test_imported_and_renamed_hash_functions_fail(self):
        text = (
            "from hashlib import md5\n"
            "from hashlib import sha1 as legacy_digest\n"
            "first = md5(payload)\n"
            "second = legacy_digest(payload)\n"
        )

        self.assertEqual(len(weak_hashing.find_violations(text, "hashes.py")), 2)

    def test_assignment_alias_fails(self):
        text = (
            "import hashlib\n"
            "legacy_digest = hashlib.md5\n"
            "digest = legacy_digest(payload)\n"
        )

        self.assertEqual(len(weak_hashing.find_violations(text, "hashes.py")), 1)

    def test_double_slash_inside_string_is_not_a_comment(self):
        text = (
            "import hashlib\n"
            'digest = hashlib.md5(payload); source = "https://example.invalid"\n'
        )

        self.assertEqual(len(weak_hashing.find_violations(text, "hashes.py")), 1)

    def test_nested_assignment_does_not_hide_module_call(self):
        text = (
            "import hashlib\n"
            "def helper():\n"
            "    hashlib = object()\n"
            "digest = hashlib.md5(payload)\n"
        )

        self.assertEqual(len(weak_hashing.find_violations(text, "hashes.py")), 1)

    def test_security_comment_does_not_justify_weak_hash(self):
        text = "import hashlib\ndigest = hashlib.md5(password)  # password hash\n"

        self.assertEqual(len(weak_hashing.find_violations(text, "hashes.py")), 1)

    def test_non_security_comment_justifies_weak_hash(self):
        text = (
            "import hashlib\n"
            "digest = hashlib.md5(payload)  "
            "# MD5: non-cryptographic cache key only\n"
        )

        self.assertEqual(weak_hashing.find_violations(text, "hashes.py"), [])

    def test_security_use_cannot_claim_non_security_justification(self):
        text = (
            "import hashlib\n"
            "digest = hashlib.md5(password)  "
            "# non-cryptographic password hashing\n"
        )

        self.assertEqual(len(weak_hashing.find_violations(text, "hashes.py")), 1)

    def test_star_import_and_destructuring_aliases_fail(self):
        cases = (
            "from hashlib import *\ndigest = md5(payload)\n",
            (
                "import hashlib\nlegacy, = (hashlib.md5,)\n"
                "digest = legacy(payload)\n"
            ),
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    len(weak_hashing.find_violations(text, "hashes.py")), 1)

    def test_non_python_string_comment_does_not_justify_call(self):
        text = (
            "const digest = createHash('md5'); "
            "const note = '// non-security cache key';\n"
        )

        self.assertEqual(len(weak_hashing.find_violations(text, "hashes.js")), 1)

    def test_multiline_and_language_specific_calls_fail(self):
        cases = (
            ("createHash(\n  'sha1'\n)\n", "hashes.mjs"),
            ('MessageDigest.getInstance("MD5")\n', "Hashes.java"),
            ("digest = md5.New()\n", "hashes.go"),
            ("var digest = MD5.Create();\n", "Hashes.cs"),
        )
        for text, path in cases:
            with self.subTest(path=path):
                self.assertEqual(len(weak_hashing.find_violations(text, path)), 1)


class BannedAgentsTest(unittest.TestCase):
    """Only terminal trailers contribute structured co-author identities."""

    def test_co_authored_by_trailer_key_is_case_insensitive(self):
        body = (
            "Implement the change.\n\n"
            "cO-aUtHoReD-bY: Grok <agent@x.ai>\n"
        )

        violations = banned_agents.find_violations([_commit(body)])

        self.assertEqual(len(violations), 1)
        self.assertIn("co-author", violations[0])

    def test_body_lookalike_outside_terminal_trailers_is_ignored(self):
        body = (
            "Document this literal example:\n\n"
            "Co-authored-by: Grok <agent@x.ai>\n\n"
            "Keep the example in the message body.\n"
        )

        self.assertEqual(banned_agents.find_violations([_commit(body)]), [])

    def test_name_only_banned_agent_is_rejected(self):
        body = "Implement the change.\n\nCo-authored-by: Grok\n"
        violations = banned_agents.find_violations([_commit(body)])
        self.assertEqual(len(violations), 1)
        self.assertIn("co-author", violations[0])

    def test_assisted_by_banned_model_is_rejected(self):
        for trailer in (
            "Assisted-by: DeepSeek V4 Flash\n",
            "Assisted by: DeepSeek V4 Flash\n",
            "Assisted-by: grok-4.6\n",
            "Assisted by: grok-4.20-0309-reasoning\n",
            "Assisted-by: grok-build-0.1\n",
            "Assisted-by: grok-5-preview\n",
            "Assisted-by: deepseek-v4-flash-preview\n",
        ):
            with self.subTest(trailer=trailer):
                body = f"Implement feature.\n\n{trailer}"
                violations = banned_agents.find_violations([_commit(body)])
                self.assertEqual(len(violations), 1)
                self.assertIn("banned-agent model", violations[0])

    def test_assisted_by_permitted_model_passes(self):
        for trailer in (
            "Assisted-by: Claude Sonnet 5\n",
            "Assisted by: Claude Sonnet 5\n",
            "Assisted-by: DeepSeek V3\n",
            "Assisted-by: DeepSeek R1\n",
        ):
            with self.subTest(trailer=trailer):
                body = f"Implement feature.\n\n{trailer}"
                violations = banned_agents.find_violations([_commit(body)])
                self.assertEqual(violations, [])

    def test_pr_description_banned_model_is_rejected(self):
        for line in (
            "Assisted by: DeepSeek V4 Flash",
            "Assisted-by: DeepSeek V4 Flash",
            "- Assisted by: grok-4.20-0309-reasoning",
            "**Assisted by:** grok-build-0.1",
            "* Assisted by: grok-imagine-image-2.0",
            "Assisted by: grok-5-future",
        ):
            with self.subTest(line=line):
                body = f"PR summary.\n\n{line}\n\nMore details."
                violations = banned_agents.find_violations([], pr_body=body)
                self.assertEqual(len(violations), 1)
                self.assertIn("PR description: banned-agent disclosure", violations[0])

    def test_pr_description_permitted_model_passes(self):
        body = (
            "PR summary.\n\n"
            "- Assisted by: Claude Sonnet 5\n"
            "- Co-authored by: Claude Code\n"
            "Assisted by: DeepSeek V3\n"
        )
        violations = banned_agents.find_violations([], pr_body=body)
        self.assertEqual(violations, [])

    def test_missing_banned_models_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            banned_agents.load_banned_models("nonexistent_models_file.txt")

    def test_oversized_banned_models_fails_closed(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as temp:
            temp.write(b"a" * (64 * 1024 + 1))
            temp_path = temp.name
        try:
            with self.assertRaises(ValueError):
                banned_agents.load_banned_models(temp_path)
        finally:
            os.unlink(temp_path)

    def test_exact_model_ban_does_not_false_positive_on_human_author(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as temp:
            temp.write("phi\nnova\nr1\n")
            temp_path = temp.name
        try:
            # Human author Philip should not trigger exact model ban 'phi'
            human_commit = {
                "sha": "a" * 40,
                "author_name": "Philip Morris",
                "author_email": "philip@example.com",
                "committer_name": "Philip Morris",
                "committer_email": "philip@example.com",
                "body": "feat: normal change\n",
            }
            violations = banned_agents.find_violations(
                [human_commit], banned_models_file=temp_path
            )
            self.assertEqual(violations, [])

            # Bot claiming exact model should be blocked
            bot_commit = {
                "sha": "b" * 40,
                "author_name": "phi[bot]",
                "author_email": "phi@users.noreply.github.com",
                "committer_name": "phi[bot]",
                "committer_email": "phi@users.noreply.github.com",
                "body": "feat: bot change\n",
            }
            bot_violations = banned_agents.find_violations(
                [bot_commit], banned_models_file=temp_path
            )
            self.assertEqual(len(bot_violations), 2)  # author and committer

            # Model disclosure of 'phi' should be blocked
            disclosure_commit = {
                "sha": "c" * 40,
                "author_name": "Human Author",
                "author_email": "human@example.com",
                "committer_name": "Human Author",
                "committer_email": "human@example.com",
                "body": "feat: change\n\nAssisted-by: phi\n",
            }
            disc_violations = banned_agents.find_violations(
                [disclosure_commit], banned_models_file=temp_path
            )
            self.assertEqual(len(disc_violations), 1)
            self.assertIn("banned-agent model", disc_violations[0])
        finally:
            os.unlink(temp_path)

    def test_trailer_with_trailing_non_trailer_text_still_flags_banned_model(self):
        body = (
            "feat: change\n\n"
            "Assisted-by: grok-4.6\n"
            "Some plain text evasion attempt\n"
        )
        violations = banned_agents.find_violations([_commit(body)])
        self.assertEqual(len(violations), 1)
        self.assertIn("banned-agent model 'grok-4.6'", violations[0])


class SecretsHeuristicTest(unittest.TestCase):
    """Environment variants and private-key formats remain blocked."""

    def test_env_production_is_blocked(self):
        violations = secrets_heuristic.find_violations(
            "APP_MODE=production\n", "config/.env.production"
        )

        self.assertEqual(len(violations), 1)

    def test_env_example_is_allowed(self):
        violations = secrets_heuristic.find_violations(
            "APP_MODE=development\n", "config/.env.example"
        )

        self.assertEqual(violations, [])

    def test_encrypted_and_pgp_private_key_headers_fail(self):
        headers = (
            "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----",
            "-----BEGIN " + "PGP PRIVATE KEY BLOCK-----",
        )
        for header in headers:
            with self.subTest(header=header):
                violations = secrets_heuristic.find_violations(
                    f"{header}\n", "private-key.txt"
                )
                self.assertEqual(len(violations), 1)

    def test_github_user_and_refresh_tokens_fail(self):
        for prefix in ("ghu_", "ghr_"):
            with self.subTest(prefix=prefix):
                token = prefix + ("A" * 36)
                violations = secrets_heuristic.find_violations(
                    f"TOKEN={token}\n", "config.txt"
                )
                self.assertEqual(len(violations), 1)


class BranchNameTest(unittest.TestCase):
    """An unavailable branch name is an error, not an exemption."""

    def _run_main(self, returncode: int, stdout: str, stderr: str) -> int:
        """Run branch discovery with a controlled git result."""
        result = SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        with patch.dict(branch_name.os.environ, {"GITHUB_HEAD_REF": ""}), \
                patch.object(branch_name.subprocess, "run", return_value=result), \
                patch.object(sys, "argv", ["check_branch_name.py"]):
            return branch_name.main()

    def test_git_lookup_failure_fails(self):
        result = self._run_main(128, "", "fatal: not a git repository")

        self.assertEqual(result, 1)

    def test_empty_git_output_fails(self):
        result = self._run_main(0, "\n", "")

        self.assertEqual(result, 1)

    def test_task_specific_branch_names_are_accepted(self):
        for branch in (
            "fix/branch-name-validation",
            "chore/synchronize-policy-copies",
            "docs/clarify-agent-branch-rules",
        ):
            with self.subTest(branch=branch):
                self.assertEqual(branch_name.find_violations(branch), [])

    def test_random_and_opaque_branch_names_are_rejected(self):
        for branch in (
            "chore/kind-thompson-6vv1rjc",
        ):
            with self.subTest(branch=branch):
                self.assertTrue(branch_name.find_violations(branch))

    def test_vulgar_and_non_english_tokens_are_rejected(self):
        for branch in ("fix/fuck-parser", "fix/la-validacion"):
            with self.subTest(branch=branch):
                self.assertTrue(branch_name.find_violations(branch))

    def test_established_technical_suffixes_are_accepted(self):
        for suffix in ("sha256", "base64", "oauth2", "python310", "es2022"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    branch_name.find_violations(f"fix/support-{suffix}"), []
                )

    def test_mit_license_identifier_is_accepted(self):
        self.assertEqual(
            branch_name.find_violations("docs/update-mit-license"), []
        )


class TrustedGitTest(unittest.TestCase):
    """Repository-local executables cannot replace trusted Git."""

    def test_repository_git_is_excluded_from_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            executable = repo / ("git.exe" if os.name == "nt" else "git")
            executable.write_text("malicious executable\n", encoding="utf-8")
            executable.chmod(0o755)
            old_path = os.environ.get("PATH", "")
            environment = {"PATH": os.pathsep.join((str(repo), old_path))}

            with patch.dict(os.environ, environment, clear=False):
                trusted_git = _load_checker("trusted_git")
                resolved = Path(trusted_git.resolve_git(repo))

        self.assertNotEqual(resolved.resolve(), executable.resolve())


if __name__ == "__main__":
    unittest.main()
