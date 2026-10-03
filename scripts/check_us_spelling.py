#!/usr/bin/env python3
"""Warn about British spellings in the given files.

Portable and path-generic (unlike lint_style.py, which is hardcoded to
AGENTS.md in this repo): copy this single file into any repo and point it
at that repo's own source globs and CI. Always exits 0, even when it finds
violations; this check is advisory, not blocking.
"""
import re
import sys
from pathlib import Path

INLINE_CODE = re.compile(r"`[^`]*`")

# -our -> -or
# -ise/-isation -> -ize/-ization
# -re -> -er
# -ce -> -se
# -ogue -> -og
# miscellaneous, plus contested pairs the user chose to enforce
# (theatre/theater, catalogue/catalog, dialogue/dialog, analogue/analog)
#
# Deliberately excluded as ambiguous or dual-valid in American English:
# glamour, burnt, learnt, disc, grey/gray, judgement/judgment,
# acknowledgement/acknowledgment, moustache/mustache, and the whole class
# of unstressed-final-syllable "-el"/"-ol" verb forms (cancelled/canceled,
# travelled/traveled, traveller/traveler, modelled/modeled, and similar),
# which American style guides accept in both forms.
BRITISH_TO_AMERICAN = {
    "colour": "color", "behaviour": "behavior", "favour": "favor",
    "honour": "honor", "labour": "labor", "neighbour": "neighbor",
    "humour": "humor", "rumour": "rumor", "armour": "armor",
    "flavour": "flavor", "rigour": "rigor", "vigour": "vigor",
    "saviour": "savior", "endeavour": "endeavor", "harbour": "harbor",
    "organise": "organize", "optimise": "optimize", "realise": "realize",
    "recognise": "recognize", "analyse": "analyze", "authorise": "authorize",
    "categorise": "categorize", "customise": "customize",
    "emphasise": "emphasize", "finalise": "finalize",
    "initialise": "initialize", "localise": "localize",
    "maximise": "maximize", "minimise": "minimize",
    "prioritise": "prioritize", "serialise": "serialize",
    "standardise": "standardize", "summarise": "summarize",
    "synchronise": "synchronize", "utilise": "utilize",
    "visualise": "visualize", "capitalise": "capitalize",
    "criticise": "criticize", "organisation": "organization",
    "centre": "center", "metre": "meter", "litre": "liter",
    "theatre": "theater", "fibre": "fiber", "calibre": "caliber",
    "sombre": "somber", "manoeuvre": "maneuver", "spectre": "specter",
    "licence": "license", "defence": "defense", "offence": "offense",
    "pretence": "pretense",
    "catalogue": "catalog", "dialogue": "dialog", "analogue": "analog",
    "programme": "program", "aeroplane": "airplane", "tyre": "tire",
    "kerb": "curb", "cheque": "check", "mould": "mold",
    "artefact": "artifact", "aluminium": "aluminum", "storey": "story",
    "sceptic": "skeptic", "practise": "practice", "wilful": "willful",
    "skilful": "skillful", "fulfil": "fulfill",
    "instalment": "installment", "enrolment": "enrollment",
    "jewellery": "jewelry",
}
BRITISH_PATTERN = re.compile(
    r"\b(" + "|".join(sorted(BRITISH_TO_AMERICAN, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def strip_code(line: str) -> str:
    """Remove inline code spans so illustrative Bad/Good examples are ignored."""
    return INLINE_CODE.sub("", line)


def find_violations(text: str, path: str) -> list[str]:
    """Return one warning per British spelling found in the prose of `text`."""
    violations = []
    in_fence = False
    for number, raw in enumerate(text.splitlines(), start=1):
        if raw.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        prose = strip_code(raw)
        for match in BRITISH_PATTERN.finditer(prose):
            word = match.group(1).lower()
            violations.append(
                f"warning: {path}:{number}: British spelling "
                f"'{match.group(1)}' (use '{BRITISH_TO_AMERICAN[word]}')"
            )
    return violations


def main() -> int:
    """Warn about British spellings in each given file. Always exits 0."""
    paths = sys.argv[1:]
    if not paths:
        print("usage: check_us_spelling.py FILE [FILE ...]", file=sys.stderr)
        return 0

    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        for message in find_violations(text, path):
            print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
