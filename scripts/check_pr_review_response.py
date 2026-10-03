#!/usr/bin/env python3
"""Validate the machine-readable PR review response contract."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

VERDICT_PATTERN = re.compile(
    r"^VERDICT:\s*(APPROVE|BLOCK|NEEDS-HUMAN)(?:\s+-\s+.*)?$"
)
CLASS_PATTERN = re.compile(r"^2\.\d+$")
VALID_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
FINDING_KEYS = {"severity", "class", "file", "line", "title"}
RESPONSE_LIMIT = 1_000_000


class ResponseError(ValueError):
    """Report a response-contract failure."""


def _last_verdict(lines: list[str]) -> tuple[str, str]:
    if not any(line.strip() for line in lines):
        raise ResponseError("response is empty")
    for line in reversed(lines):
        match = VERDICT_PATTERN.fullmatch(line.strip())
        if match:
            return match.group(1), line.strip()
    raise ResponseError("no valid final VERDICT line")


def _parse_json_line(lines: list[str]) -> dict[str, Any]:
    for line in reversed(lines):
        if not line.startswith("VERDICT_JSON:"):
            continue
        payload = line[len("VERDICT_JSON:"):].lstrip()
        try:
            result = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ResponseError("VERDICT_JSON is not valid JSON") from error
        if not isinstance(result, dict):
            raise ResponseError("VERDICT_JSON is not an object")
        return result
    raise ResponseError("no VERDICT_JSON line")


def _validate_finding(finding: Any) -> None:
    if not isinstance(finding, dict) or set(finding) != FINDING_KEYS:
        raise ResponseError("finding has an invalid shape")
    if finding["severity"] not in VALID_SEVERITIES:
        raise ResponseError("finding has an invalid severity")
    if not isinstance(finding["class"], str) or not CLASS_PATTERN.fullmatch(
        finding["class"]
    ):
        raise ResponseError("finding has an invalid class")
    if not isinstance(finding["file"], str) or not finding["file"]:
        raise ResponseError("finding has an invalid file")
    if (
        not isinstance(finding["line"], int)
        or isinstance(finding["line"], bool)
        or finding["line"] < 1
    ):
        raise ResponseError("finding has an invalid line")
    if not isinstance(finding["title"], str) or not finding["title"]:
        raise ResponseError("finding has an invalid title")


def validate_response(response: str) -> tuple[str, dict[str, Any]]:
    """Validate one bounded PR response and return its verdict and JSON."""
    if len(response.encode("utf-8")) > RESPONSE_LIMIT:
        raise ResponseError("response exceeds the configured limit")
    lines = response.splitlines()
    verdict, _line = _last_verdict(lines)
    payload = _parse_json_line(lines)
    if set(payload) != {"mode", "verdict", "findings"}:
        raise ResponseError("VERDICT_JSON has an invalid shape")
    if payload["mode"] != "PR" or payload["verdict"] != verdict:
        raise ResponseError("VERDICT_JSON does not match the PR verdict")
    findings = payload["findings"]
    if not isinstance(findings, list):
        raise ResponseError("findings is not a list")
    for finding in findings:
        _validate_finding(finding)
    return verdict, payload


def main() -> int:
    """Validate the response file and print a normalized verdict."""
    if len(sys.argv) != 2:
        print("usage: check_pr_review_response.py RESPONSE_FILE", file=sys.stderr)
        return 2
    try:
        response = Path(sys.argv[1]).read_text(encoding="utf-8")
        verdict, _payload = validate_response(response)
    except (OSError, UnicodeError, ResponseError) as error:
        print(f"invalid PR review response: {error}", file=sys.stderr)
        return 1
    print(f"VERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
