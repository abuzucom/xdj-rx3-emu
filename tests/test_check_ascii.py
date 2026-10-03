#!/usr/bin/env python3
"""Cover the dash and ASCII checker, including what it must not flag.

The checker reads prose. Markdown table separators, list markers, and
inline code spans are syntax and data, and flagging them pushes whoever
runs it into damaging a document to satisfy a linter. A preset name
carrying a spaced hyphen reached the dash check this way, and rewriting
it would have falsified the record of which preset was removed.
"""
import sys
import unittest
from pathlib import Path

# scripts/ is never on the path, however the suite is launched.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_ascii


def violations(text: str) -> list:
    """Return the checker's findings for `text`, under a fixed path."""
    return check_ascii.find_violations(text, "sample.md")


class TableSeparatorTest(unittest.TestCase):
    """A table delimiter row is syntax, not a dash substitute."""

    ROWS = (
        "| --- | --- |",
        "| --- | --- | --- |",
        "|---|---|",
        "| :--- | ---: | :---: |",
        "  | --- | --- |",
    )

    def test_separator_rows_pass(self):
        for row in self.ROWS:
            with self.subTest(row=row):
                self.assertEqual(violations(f"| a | b |\n{row}\n| 1 | 2 |\n"), [])


class ListMarkerTest(unittest.TestCase):
    """A list marker at the start of a line is not a dash substitute."""

    def test_a_nested_list_marker_passes(self):
        self.assertEqual(violations("   - Set Width to your canvas size.\n"), [])

    def test_a_top_level_list_marker_passes(self):
        self.assertEqual(violations("- Set Width to your canvas size.\n"), [])

    def test_a_dash_substitute_after_a_marker_still_fails(self):
        """Stripping the marker must not blind the check to the rest."""
        found = violations("   - The build failed - the cache was stale.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("spaced hyphen", found[0])


class MultiLineCodeSpanTest(unittest.TestCase):
    """An inline code span can open on one line and close on the next."""

    def test_a_hyphen_inside_a_wrapped_span_passes(self):
        text = ("Removed the presets named `Hampton GER (Adjustable Mix)`,\n"
                "`Optiks - Nerve`, and the rest.\n")
        self.assertEqual(violations(text), [])

    def test_a_span_opening_on_the_previous_line_passes(self):
        text = ("Removed `Eo.S. + Redi Jedi Phat Mexican Insanity Pepper\n"
                "edit colors2`, `Optiks - Nerve`), plus every other preset.\n")
        self.assertEqual(violations(text), [])

    def test_prose_after_a_closed_span_is_still_checked(self):
        found = violations("The `build` step failed - the cache was stale.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("spaced hyphen", found[0])


class StillFlaggedTest(unittest.TestCase):
    """What the rule covers keeps failing."""

    def test_a_spaced_hyphen_in_prose_fails(self):
        found = violations("The build failed - the cache was stale.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("spaced hyphen", found[0])

    def test_a_double_hyphen_in_prose_fails(self):
        found = violations("The build failed -- the cache was stale.\n")
        self.assertEqual(len(found), 1)

    def test_an_em_dash_character_fails(self):
        found = violations("The build failed — the cache was stale.\n")
        self.assertTrue(any("em/en dash" in line for line in found))

    def test_non_ascii_prose_fails(self):
        found = violations("Initialize the café palette.\n")
        self.assertEqual(len(found), 1)
        self.assertIn("non-ASCII", found[0])

    def test_non_ascii_inside_code_passes(self):
        self.assertEqual(violations("Print `café` to the log.\n"), [])

    def test_a_fenced_block_is_skipped(self):
        text = "Prose.\n```\nThe build failed - the cache was stale.\n```\nMore.\n"
        self.assertEqual(violations(text), [])


if __name__ == "__main__":
    unittest.main()
