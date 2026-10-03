#!/usr/bin/env python3
"""Provide shared advisory prose-policy analysis."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

try:
    from scripts.handoff_policy import HANDOFF_PATH, is_handoff_path
except ModuleNotFoundError:
    from handoff_policy import HANDOFF_PATH, is_handoff_path

ROOT = Path(__file__).resolve().parent.parent
DENYLIST_PATH = Path(__file__).resolve().with_name("prose_bans.txt")
DENYLIST_RELATIVE_PATH = "scripts/prose_bans.txt"
HANDOFF_PATH = "plan/HANDOFF.md.example"
MAX_DENYLIST_BYTES = 64 * 1024

SCOPES = {"global", "handoff-exempt"}

PERSONAL_PRONOUNS = (
    "i", "me", "my", "mine", "myself",
    "we", "us", "our", "ours", "ourselves",
    "you", "your", "yours", "yourself", "yourselves",
    "he", "him", "his", "himself",
    "she", "her", "hers", "herself",
    "they", "them", "their", "theirs", "themselves",
    "i'm", "i've", "i'll", "i'd",
    "we're", "we've", "we'll", "we'd",
    "you're", "you've", "you'll", "you'd",
    "he's", "he'll", "he'd",
    "she's", "she'll", "she'd",
    "they're", "they've", "they'll", "they'd",
)

PASSIVE_PATTERN = re.compile(
    r"\b(?:am|is|are|was|were|be|been|being|get|gets|got|gotten)\s+"
    r"(?:[a-z]+ly\s+){0,2}"
    r"(?:[a-z]+(?:ed|en)|built|done|found|given|held|kept|known|made|read|"
    r"run|seen|sent|shown|taken|told|written)\b(?:\s+by\b)?",
    re.IGNORECASE,
)

COMMA_TAIL_PATTERN = re.compile(r",\s*(?:so|which)\b", re.IGNORECASE)
SEMICOLON_CLAUSE_PATTERN = re.compile(
    r"[A-Za-z][^\n;.!?]*;\s*[A-Za-z][^\n;.!?]*[.!?]"
)
COLON_CLAUSE_PATTERN = re.compile(
    r"[A-Za-z][^\n:.!?]*:[ \t]+"
    r"(?:the|a|an|this|that|these|those|[A-Z][a-z]+)\s+"
    r"(?:[A-Za-z-]+\s+){0,3}"
    r"(?:am|is|are|was|were|has|have|had|can|will|must|should|does|do|did|"
    r"[a-z]+(?:s|ed))\b",
    re.IGNORECASE,
)
INLINE_ENUMERATION_PATTERN = re.compile(
    r":[^\n.!?,]*(?:,[^\n.!?,]*){2,}\s+(?:and|or)\b[^\n.!?]*[.!?]",
    re.IGNORECASE,
)
RHETORICAL_PATTERNS = (
    re.compile(r"\bnot\b[^\n.!?]{1,80},?\s+but\b", re.IGNORECASE),
    re.compile(r"\bnot\s+(?:only|just|merely)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:is|are|was|were|means|provides|represents)\b"
        r"[^\n.!?]{1,80},\s*not\b",
        re.IGNORECASE,
    ),
)
LITERAL_ESCAPE_PATTERN = re.compile(r"\\[nrt]")

HEDGING_PHRASES = (
    "could potentially",
    "in some cases",
    "might potentially",
    "should probably",
    "worth checking",
)
JUSTIFICATION_PHRASES = (
    "follow industry standards",
    "since this is safer",
    "to make it more robust",
    "use a flexible design",
    "use a more efficient approach",
    "use a robust approach",
    "use a scalable solution",
    "use best practices",
    "this enhances security",
    "this follows best practices",
    "this follows the single-responsibility principle",
    "this handles edge cases",
    "this improves maintainability",
    "this is a reliable solution",
    "this keeps the implementation clean",
    "this makes the code more readable",
    "this makes the system more resilient",
    "this prevents potential issues",
    "this provides better performance",
)
PROVENANCE_PHRASES = (
    "as discussed",
    "as instructed",
    "as requested",
    "current conversation",
    "current workstream",
    "earlier chat",
    "from the prompt",
    "in the prompt",
    "per the plan",
    "per the prompt",
    "per the request",
    "per the task",
    "previous message",
)
HISTORICAL_PHRASES = (
    "changed from",
    "previously was",
    "removed the old",
    "replaced the old",
    "used to",
)

OWNERSHIP_PATTERN = re.compile(
    r"\b(?:belongs to another team|outside agent (?:ownership|responsibility)|"
    r"someone else's problem|pre-existing issue)\b",
    re.IGNORECASE,
)
INTENT_PATTERN = re.compile(
    r"\b(?:requester|maintainer|operator|customer|stakeholder|team)\s+"
    r"(?:wants?|wanted|prefers?|preferred|expects?|expected|needs?|needed|"
    r"asked|intends?|intended|requires?|required)\b",
    re.IGNORECASE,
)
GENERIC_CLAIM_PATTERN = re.compile(
    r"\b(?:robust|scalable|flexible)\s+(?:solution|approach|design)\b",
    re.IGNORECASE,
)
TUTORIAL_PATTERN = re.compile(
    r"^\s*(?:#|//)?\s*(?:First|Next|Finally),",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class DenyEntry:
    """Represent one exact denylist entry and policy scope."""

    text: str
    scope: str
    line: int


@dataclass(frozen=True)
class Finding:
    """Represent one categorized source finding."""

    start: int
    end: int
    category: str
    detail: str


def _normalize_path(path: str | Path) -> str:
    """Return a slash-normalized repository-relative path when possible."""
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(ROOT.resolve())
        except (OSError, ValueError):
            pass
    normalized = str(candidate).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def load_denylist(path: str | Path = DENYLIST_PATH) -> list[DenyEntry]:
    """Load and validate scoped exact entries from the policy file."""
    source = Path(path)
    if source.stat().st_size > MAX_DENYLIST_BYTES:
        raise ValueError("denylist exceeds the size limit")
    text = source.read_text(encoding="ascii")
    entries = []
    seen = set()
    scope = ""
    for number, raw in enumerate(text.splitlines(), start=1):
        value = raw.strip()
        if not value:
            continue
        if value.startswith("[") and value.endswith("]"):
            scope = value[1:-1]
            if scope not in SCOPES:
                raise ValueError(f"unknown denylist scope on line {number}")
            continue
        if not scope:
            raise ValueError(f"denylist entry lacks a scope on line {number}")
        key = value.casefold()
        if key in seen:
            raise ValueError(f"duplicate denylist entry on line {number}")
        seen.add(key)
        entries.append(DenyEntry(value, scope, number))
    if not entries:
        raise ValueError("denylist contains no entries")
    return entries


def _mask_characters(value: str) -> str:
    """Replace non-newline content with spaces while preserving offsets."""
    return "".join("\n" if character == "\n" else " " for character in value)


def mask_markdown_code(text: str) -> str:
    """Mask fenced and inline code while preserving source offsets."""
    output = []
    in_fence = False
    fence_character = ""
    fence_length = 0
    in_inline = False
    for line in text.splitlines(keepends=True):
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence = True
                fence_character = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_length:
                in_fence = False
                fence_character = ""
                fence_length = 0
            output.append(_mask_characters(line))
            in_inline = False
            continue
        if in_fence:
            output.append(_mask_characters(line))
            continue
        masked = []
        for character in line:
            if character == "`":
                in_inline = not in_inline
                masked.append(" ")
            elif in_inline and character != "\n":
                masked.append(" ")
            else:
                masked.append(character)
        output.append("".join(masked))
    return "".join(output)


def _entry_pattern(text: str) -> re.Pattern:
    """Compile one case-insensitive exact token or phrase pattern."""
    escaped = re.escape(text).replace(r"\ ", r"\s+")
    prefix = r"(?<![A-Za-z])" if text[0].isalnum() else ""
    suffix = r"(?![A-Za-z])" if text[-1].isalnum() else ""
    return re.compile(prefix + escaped + suffix, re.IGNORECASE)


def _phrase_pattern(phrases: tuple[str, ...]) -> re.Pattern:
    """Compile longest-first phrase alternatives with token boundaries."""
    alternatives = []
    for phrase in sorted(phrases, key=len, reverse=True):
        escaped = re.escape(phrase).replace(r"\ ", r"\s+")
        alternatives.append(escaped)
    return re.compile(
        r"(?<![A-Za-z])(?:" + "|".join(alternatives) + r")(?![A-Za-z])",
        re.IGNORECASE,
    )


def _overlaps(finding: Finding, occupied: list[tuple[int, int]]) -> bool:
    """Return whether a higher-priority finding owns any source span."""
    return any(finding.start < end and finding.end > start
               for start, end in occupied)


def _collect_pattern(
        text: str, pattern: re.Pattern, category: str, detail: str,
        findings: list[Finding], occupied: list[tuple[int, int]]) -> None:
    """Collect non-overlapping matches for one policy category."""
    for match in pattern.finditer(text):
        finding = Finding(match.start(), match.end(), category, detail)
        if _overlaps(finding, occupied):
            continue
        findings.append(finding)
        occupied.append((finding.start, finding.end))


def _format_finding(text: str, path: str, finding: Finding) -> str:
    """Return one bounded diagnostic without echoing source content."""
    line = text.count("\n", 0, finding.start) + 1
    previous_newline = text.rfind("\n", 0, finding.start)
    column = finding.start - previous_newline
    return (
        f"warning: {path}:{line}:{column}: {finding.category} "
        f"({finding.detail})"
    )


def find_violations(
        text: str, path: str, entries: list[DenyEntry] | None = None) -> list[str]:
    """Return advisory prose-policy findings for one configured source."""
    normalized_path = _normalize_path(path)
    if normalized_path == DENYLIST_RELATIVE_PATH:
        return []
    denylist = entries if entries is not None else load_denylist()
    findings = []
    occupied = []

    for entry in denylist:
        if entry.scope == "handoff-exempt" and is_handoff_path(normalized_path):
            continue
        _collect_pattern(
            text,
            _entry_pattern(entry.text),
            "controlled vocabulary",
            f"denylist line {entry.line}",
            findings,
            occupied,
        )

    prose = mask_markdown_code(text)
    pronoun_pattern = _phrase_pattern(PERSONAL_PRONOUNS)
    _collect_pattern(
        prose, pronoun_pattern, "personal pronoun", "impersonal voice",
        findings, occupied,
    )
    _collect_pattern(
        prose, PASSIVE_PATTERN, "passive voice", "active voice preferred",
        findings, occupied,
    )

    _collect_pattern(
        prose, COMMA_TAIL_PATTERN, "comma-led tail", "separate sentence",
        findings, occupied,
    )
    _collect_pattern(
        prose, SEMICOLON_CLAUSE_PATTERN, "clause join", "semicolon",
        findings, occupied,
    )
    _collect_pattern(
        prose, COLON_CLAUSE_PATTERN, "clause join", "colon",
        findings, occupied,
    )
    _collect_pattern(
        prose,
        INLINE_ENUMERATION_PATTERN,
        "punctuation chain",
        "inline enumeration",
        findings,
        occupied,
    )
    for pattern in RHETORICAL_PATTERNS:
        _collect_pattern(
            prose,
            pattern,
            "rhetorical contrast",
            "direct statement preferred",
            findings,
            occupied,
        )
    _collect_pattern(
        prose,
        LITERAL_ESCAPE_PATTERN,
        "literal escape sequence",
        "use real newlines and whitespace",
        findings,
        occupied,
    )

    discourse_patterns = (
        (_phrase_pattern(HEDGING_PHRASES), "hedging", "direct statement preferred"),
        (
            _phrase_pattern(JUSTIFICATION_PHRASES),
            "generic justification",
            "name the mechanism",
        ),
        (
            _phrase_pattern(HISTORICAL_PHRASES),
            "historical narration",
            "describe current behavior",
        ),
        (OWNERSHIP_PATTERN, "ownership deflection", "report the defect"),
        (INTENT_PATTERN, "intent attribution", "state the constraint"),
        (GENERIC_CLAIM_PATTERN, "generic justification", "name the mechanism"),
        (TUTORIAL_PATTERN, "tutorial narration", "direct instruction preferred"),
    )
    if not is_handoff_path(normalized_path):
        discourse_patterns += ((
            _phrase_pattern(PROVENANCE_PHRASES),
            "conversational provenance",
            "state a durable fact",
        ),)
    for pattern, category, detail in discourse_patterns:
        _collect_pattern(
            prose, pattern, category, detail, findings, occupied,
        )

    findings.sort(key=lambda item: (item.start, item.end, item.category))
    return [
        _format_finding(text, normalized_path, finding)
        for finding in findings
    ]
