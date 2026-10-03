#!/usr/bin/env python3
"""Cover the vendored PR review response contract validator."""
import unittest

from scripts.check_pr_review_response import ResponseError, validate_response


VALID_JSON = (
    '{"mode": "PR", "verdict": "APPROVE", "findings": []}'
)


class ValidateResponseTest(unittest.TestCase):
    """Accept a well-formed response and reject each documented defect."""

    def test_accepts_a_well_formed_approve_response(self):
        response = f"VERDICT: APPROVE - no findings\nVERDICT_JSON: {VALID_JSON}\n"
        verdict, payload = validate_response(response)
        self.assertEqual(verdict, "APPROVE")
        self.assertEqual(payload["findings"], [])

    def test_rejects_a_missing_verdict_line(self):
        response = f"No verdict here.\nVERDICT_JSON: {VALID_JSON}\n"
        with self.assertRaises(ResponseError):
            validate_response(response)

    def test_rejects_a_verdict_json_that_disagrees_with_the_verdict_line(self):
        mismatched = '{"mode": "PR", "verdict": "BLOCK", "findings": []}'
        response = f"VERDICT: APPROVE - no findings\nVERDICT_JSON: {mismatched}\n"
        with self.assertRaises(ResponseError):
            validate_response(response)

    def test_rejects_a_finding_with_an_invalid_severity(self):
        findings = '{"mode": "PR", "verdict": "BLOCK", "findings": [{"severity": "EXTREME", "class": "2.1", "file": "a.py", "line": 1, "title": "x"}]}'
        response = f"VERDICT: BLOCK - one finding\nVERDICT_JSON: {findings}\n"
        with self.assertRaises(ResponseError):
            validate_response(response)

    def test_rejects_an_empty_response(self):
        with self.assertRaises(ResponseError):
            validate_response("")


if __name__ == "__main__":
    unittest.main()
