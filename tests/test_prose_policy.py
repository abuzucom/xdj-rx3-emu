#!/usr/bin/env python3
"""Cover consolidated prose policy checks and category ownership."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REDOS_TIMEOUT_SECONDS = 5
REDOS_TOKEN_COUNT = 28

sys.path.insert(0, str(ROOT / "scripts"))
import prose_policy


def findings(text: str, path: str = "sample.md") -> list:
    """Return policy findings for fixed sample input."""
    return prose_policy.find_violations(text, path)


class VocabularyTest(unittest.TestCase):
    """Exact vocabulary checks scan raw configured content."""

    def test_configured_entries_load_in_two_scopes(self):
        entries = prose_policy.load_denylist()
        self.assertEqual(
            [entry.text for entry in entries],
            [
                "worth noting",
                "worst",
                "serious",
                "testament",
                "serves as",
                "stands as",
                "functions as",
                "utilize",
                "this session",
                "a round",
                "could not carry",
                "denied outright",
                "Denied:",
                "the user",
            ],
        )

    def test_matching_ignores_case(self):
        found = findings("A SERIOUS failure occurred.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("controlled vocabulary", found[0])

    def test_matching_uses_exact_forms(self):
        self.assertEqual(findings("The failure was seriously disruptive.\n"), [])
        self.assertEqual(findings("The helper utilized an existing API.\n"), [])

    def test_matching_includes_inline_and_fenced_code(self):
        inline = findings("Call `utilize(resource)` here.\n")
        fenced = findings("```text\nworst\n```\n")
        self.assertEqual(len(inline), 1)
        self.assertEqual(len(fenced), 1)

    def test_denylist_source_skips_self_scanning(self):
        text = (Path(prose_policy.DENYLIST_PATH)
                .read_text(encoding="utf-8"))
        self.assertEqual(findings(text, "scripts/prose_bans.txt"), [])

    def test_handoff_skips_only_scoped_vocabulary(self):
        path = "plan/HANDOFF.md.example"
        self.assertEqual(findings("This session records state.\n", path), [])
        found = findings("A serious failure occurred.\n", path)
        self.assertEqual(len(found), 1)


class VoiceTest(unittest.TestCase):
    """Voice checks own personal pronouns and common passive forms."""

    def test_personal_pronouns_share_one_category(self):
        found = findings("I sent your report after they approved it.\n")
        self.assertEqual(len(found), 3)
        self.assertTrue(all("personal pronoun" in item for item in found))

    def test_neutral_object_pronouns_pass(self):
        text = "It stores its state by itself. It's stable. It'll persist. It'd recover.\n"
        self.assertEqual(findings(text), [])

    def test_pronouns_inside_examples_pass(self):
        text = "Bad: `I sent your report after they approved it.`\n"
        self.assertEqual(findings(text), [])

    def test_common_passive_form_warns(self):
        found = findings("The request was rejected by the validator.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("passive voice", found[0])

    def test_active_form_passes(self):
        self.assertEqual(findings("The validator rejected the request.\n"), [])


class SentenceFormTest(unittest.TestCase):
    """Sentence checks cover high-confidence clause and punctuation patterns."""

    def test_comma_so_warns(self):
        found = findings("The cache was stale, so the build failed.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("comma-led tail", found[0])

    def test_comma_which_warns(self):
        found = findings("The parser rejects input, which prevents bad records.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("comma-led tail", found[0])

    def test_clause_semicolon_warns(self):
        found = findings("The gate rejected input; the parser logged the result.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("clause join", found[0])

    def test_clause_colon_warns(self):
        found = findings("Newlines remain: the parser preserves each line.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("clause join", found[0])

    def test_rhetorical_contrast_warns(self):
        found = findings("The change is not broad, but focused.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("rhetorical contrast", found[0])

    def test_technical_contrast_passes(self):
        self.assertEqual(findings("Use argument arrays, not shell strings.\n"), [])

    def test_shared_subject_predicate_passes(self):
        self.assertEqual(findings("The checker reads files and reports warnings.\n"), [])

    def test_inline_enumeration_warns(self):
        found = findings("Values: alpha, beta, and gamma.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("punctuation chain", found[0])

    def test_inline_enumeration_rejects_pathological_input_quickly(self):
        script = (
            "from scripts.prose_policy import find_violations\n"
            f"text = 'Values: ' + ', '.join(['token'] * {REDOS_TOKEN_COUNT}) + '.\\n'\n"
            "find_violations(text, 'sample.md')\n"
        )
        subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=REDOS_TIMEOUT_SECONDS,
        )


class DiscourseTest(unittest.TestCase):
    """Discourse checks own pronoun-free framing patterns."""

    def test_conversational_provenance_warns(self):
        found = findings("The earlier chat established the approach.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("conversational provenance", found[0])

    def test_handoff_skips_provenance_only(self):
        path = "plan\\HANDOFF.md.example"
        self.assertEqual(
            findings("The earlier chat established the approach.\n", path),
            [],
        )
        found = findings("A scalable solution handles edge cases.\n", path)
        self.assertGreaterEqual(len(found), 1)

    def test_ownership_deflection_warns(self):
        found = findings("The defect belongs to another team.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("ownership deflection", found[0])

    def test_pronoun_ownership_emits_only_pronoun_warning(self):
        found = findings("The defect is not mine to fix.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("personal pronoun", found[0])

    def test_pronoun_intent_emits_only_pronoun_warnings(self):
        found = findings("This matches what you want.\n")
        self.assertEqual(len(found), 1)
        self.assertTrue(all("personal pronoun" in item for item in found))

    def test_pronoun_free_intent_warns(self):
        found = findings("The requester prefers strict validation.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("intent attribution", found[0])


class LiteralEscapeTest(unittest.TestCase):
    """Literal escape sequences in prose trigger advisory warnings."""

    def test_literal_newline_escape_warns(self):
        found = findings("Summary\\n- Allow rebase recovery.\\n")
        self.assertEqual(len(found), 2)
        self.assertTrue(all("literal escape sequence" in item for item in found))

    def test_literal_carriage_return_and_tab_warn(self):
        found = findings("Header\\r\\tDetails.\n")
        self.assertEqual(len(found), 2)
        self.assertTrue(all("literal escape sequence" in item for item in found))

    def test_code_span_skips_escape_sequences(self):
        self.assertEqual(findings("Use `\\n` to represent newlines.\n"), [])

    def test_fenced_code_block_skips_escape_sequences(self):
        text = "```text\nSummary\\n- item\n```\n"
        self.assertEqual(findings(text), [])


if __name__ == "__main__":
    unittest.main()
