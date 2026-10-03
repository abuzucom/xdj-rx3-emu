#!/usr/bin/env python3
"""Enforce persist-credentials: false on checkout steps (Rule 11)."""
import sys
from pathlib import Path
from typing import Iterator

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.tokens import ScalarToken


class _MarkedMapping(dict):
    """Store source locations for a YAML mapping."""

    def __init__(self, line: int) -> None:
        super().__init__()
        self.line = line
        self.key_lines: dict[object, int] = {}


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: MappingNode
) -> Iterator[_MarkedMapping]:
    """Construct a marked mapping and reject duplicate keys."""
    mapping = _MarkedMapping(node.start_mark.line)
    yield mapping
    explicit_keys = set()
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in explicit_keys
        except TypeError as error:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        explicit_keys.add(key)
    loader.flatten_mapping(node)
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        mapping[key] = loader.construct_object(value_node, deep=True)
        mapping.key_lines[key] = key_node.start_mark.line


yaml.SafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)

EXCEPTION_COMMENT = (
    "# persist-credentials: true: this job ",
    " (Rule 11 exception).",
)
_MISSING = object()


def _is_exception_comment(line: str) -> bool:
    """Return whether a line has the exact Rule 11 exception form."""
    stripped = line.strip()
    prefix, suffix = EXCEPTION_COMMENT
    if not stripped.startswith(prefix) or not stripped.endswith(suffix):
        return False
    reason = stripped[len(prefix):-len(suffix)]
    return bool(reason.strip())


def _mapping_line_numbers(lines: list[str], mapping: dict) -> set[int]:
    """Return line numbers directly associated with a marked mapping."""
    start = getattr(mapping, "line", 0)
    indent = len(lines[start]) - len(lines[start].lstrip(" "))
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].strip() and len(lines[index]) - len(
            lines[index].lstrip(" ")
        ) <= indent:
            end = index
            break
    associated = set(range(start, end))
    index = start - 1
    while index >= 0 and lines[index].strip().startswith("#"):
        comment_indent = len(lines[index]) - len(lines[index].lstrip(" "))
        if comment_indent != indent:
            break
        associated.add(index)
        index -= 1
    return associated


def _exception_comment_lines(text: str) -> set[int]:
    """Return exact exception comments outside YAML block scalar content."""
    scalar_lines = set()
    for token in yaml.scan(text):
        if isinstance(token, ScalarToken) and (
            token.style in ("|", ">")
            or token.end_mark.line > token.start_mark.line
        ):
            scalar_lines.update(
                range(token.start_mark.line + 1, token.end_mark.line))
    return {
        number
        for number, line in enumerate(text.splitlines())
        if number not in scalar_lines and _is_exception_comment(line)
    }


def _walk_mappings(documents: list[object]) -> Iterator[dict]:
    """Yield every mapping without following YAML alias cycles twice."""
    stack = list(reversed(documents))
    visited: set[int] = set()
    while stack:
        value = stack.pop()
        if isinstance(value, (dict, list)):
            identity = id(value)
            if identity in visited:
                continue
            visited.add(identity)
        if isinstance(value, dict):
            yield value
            stack.extend(reversed(list(value.values())))
        elif isinstance(value, list):
            stack.extend(reversed(value))


def _checkout_violation(
    step: dict, lines: list[str], exception_lines: set[int], path: str,
) -> str | None:
    """Return a violation for one checkout step, if needed."""
    action = step.get("uses")
    if not isinstance(action, str) or not action.lower().startswith(
        "actions/checkout@"
    ):
        return None
    settings = step.get("with")
    value = _MISSING
    if isinstance(settings, dict):
        value = settings.get("persist-credentials", _MISSING)
    if value is False:
        return None
    has_exception = bool(
        exception_lines & _mapping_line_numbers(lines, step))
    if (value is _MISSING or value is True) and has_exception:
        return None
    key_lines = getattr(step, "key_lines", {})
    number = key_lines.get("uses", getattr(step, "line", 0)) + 1
    return (
        f"{path}:{number}: actions/checkout missing "
        "persist-credentials: false (Rule 11)"
    )


def find_violations(text: str, path: str) -> list[str]:
    """Return violations without propagating malformed YAML errors."""
    try:
        documents = list(yaml.safe_load_all(text))
        lines = text.splitlines()
        exception_lines = _exception_comment_lines(text)
        return [
            violation
            for step in _walk_mappings(documents)
            if (
                violation := _checkout_violation(
                    step, lines, exception_lines, path)
            ) is not None
        ]
    except Exception:
        return [f"{path}: malformed YAML cannot be checked (Rule 11)"]


def main() -> int:
    """Check each workflow file. Return 0 when all are clean."""
    paths = sys.argv[1:]
    if not paths:
        print("usage: check_persist_credentials.py FILE [FILE ...]", file=sys.stderr)
        return 1
    violations = []
    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        violations.extend(find_violations(text, path))
    if not violations:
        return 0
    for message in violations:
        print(message, file=sys.stderr)
    print(
        "fix: add `with: persist-credentials: false`, or the Rule 11 "
        "exception comment",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
