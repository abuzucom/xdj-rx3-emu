#!/usr/bin/env python3
"""Warn about likely non-English prose in the given files.

A stopword-ratio heuristic, not true language detection (no dependency
added; see AGENTS.md Rule 9). Targets Latin-script languages only
(Spanish, French, German, Portuguese, Italian): Chinese, Japanese, and
Korean script is already non-ASCII and caught by check_ascii.py instead.

Portable and path-generic: copy this single file into any repo and point
it at that repo's own source globs and CI. Always exits 0, even when it
finds violations; this check is advisory, not blocking.
"""
import re
import sys
from pathlib import Path

INLINE_CODE = re.compile(r"`[^`]*`")
MIN_WORDS = 4
MIN_FOREIGN_HITS = 2

ENGLISH_STOPWORDS = {
    "the", "is", "and", "of", "to", "in", "that", "it", "for", "on",
    "with", "as", "this", "be", "are", "was", "were", "not", "you", "an",
    "a", "or", "but", "if", "then", "than", "from", "by", "at", "we",
    "they", "he", "she", "have", "has", "had", "do", "does", "did",
    "will", "would", "should", "can", "could", "must", "so", "no",
}
FOREIGN_STOPWORDS = {
    # Spanish
    "el", "la", "de", "que", "una", "para", "con", "las", "los", "por",
    "como", "pero", "esta", "este", "cuando",
    # French
    "le", "des", "une", "est", "dans", "avec", "pas", "pour", "sont",
    "vous", "nous", "cette", "mais",
    # German
    "der", "die", "das", "und", "ist", "nicht", "mit", "auch", "eine",
    "einen", "sich", "auf",
    # Portuguese (also uses "para", already listed under Spanish)
    "nao", "uma", "dos", "com", "sao", "isso",
    # Italian
    "il", "di", "che", "sono", "questo", "anche", "sul",
}
ENGLISH_PATTERN = re.compile(
    r"\b(" + "|".join(sorted(ENGLISH_STOPWORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
FOREIGN_PATTERN = re.compile(
    r"\b(" + "|".join(sorted(FOREIGN_STOPWORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def strip_code(line: str) -> str:
    """Remove inline code spans so illustrative Bad/Good examples are ignored."""
    return INLINE_CODE.sub("", line)


def find_violations(text: str, path: str) -> list[str]:
    """Return one warning per line that looks like non-English prose."""
    violations = []
    in_fence = False
    for number, raw in enumerate(text.splitlines(), start=1):
        if raw.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        prose = strip_code(raw)
        if len(prose.split()) < MIN_WORDS:
            continue
        english_hits = len(ENGLISH_PATTERN.findall(prose))
        foreign_hits = len(FOREIGN_PATTERN.findall(prose))
        if foreign_hits >= MIN_FOREIGN_HITS and english_hits == 0:
            violations.append(
                f"warning: {path}:{number}: looks like non-English text "
                f"({foreign_hits} foreign stopwords, 0 English stopwords)"
            )
    return violations


def main() -> int:
    """Warn about likely non-English prose in each given file. Always exits 0."""
    paths = sys.argv[1:]
    if not paths:
        print("usage: check_english_only.py FILE [FILE ...]", file=sys.stderr)
        return 0

    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        for message in find_violations(text, path):
            print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
