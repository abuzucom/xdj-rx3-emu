#!/usr/bin/env python3
"""Flag containers with no effective non-root user (Rule 12)."""
import re
import sys
from pathlib import Path
from typing import Iterator

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.tokens import ScalarToken

FROM_STAGE = re.compile(r"^\s*FROM\s+\S+", re.IGNORECASE)
USER_INSTRUCTION = re.compile(r"^\s*USER\b(.*)$", re.IGNORECASE)
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z0-9_.-]+)\1")
CONTAINER_KEYS = ("containers", "initContainers", "ephemeralContainers")
EXCEPTION_PREFIX = "# runtime-root: this container "
EXCEPTION_SUFFIX = " (Rule 12 exception)."


class _MarkedMapping(dict):
    """Store the source line for a YAML mapping."""

    def __init__(self, line: int) -> None:
        super().__init__()
        self.line = line


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


yaml.SafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _is_exception_comment(line: str) -> bool:
    """Return whether a line has the exact Rule 12 exception form."""
    stripped = line.strip()
    if not stripped.startswith(EXCEPTION_PREFIX) or not stripped.endswith(
        EXCEPTION_SUFFIX
    ):
        return False
    reason = stripped[len(EXCEPTION_PREFIX):-len(EXCEPTION_SUFFIX)]
    return bool(reason.strip())


def _is_root_user(value: object) -> bool:
    """Return whether a runtime identity is root or invalid."""
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return value <= 0
    if not isinstance(value, str) or not value.strip():
        return True
    if "$" in value:
        return True
    principal = value.strip().split()[0].split(":", 1)[0].strip().lower()
    if principal == "root":
        return True
    if principal.lstrip("+-").isdigit():
        return int(principal) <= 0
    return False


def _mapping_line_numbers(lines: list[str], mapping: dict) -> set[int]:
    """Return line numbers directly associated with a marked mapping."""
    start = getattr(mapping, "line", 0)
    indent = len(lines[start]) - len(lines[start].lstrip(" "))
    end = len(lines)
    for index in range(start + 1, len(lines)):
        next_indent = len(lines[index]) - len(lines[index].lstrip(" "))
        if lines[index].strip() and next_indent <= indent:
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


def _has_mapping_exception(
    lines: list[str], exception_lines: set[int], mapping: dict,
) -> bool:
    """Return whether a mapping carries an exact exception comment."""
    return bool(
        exception_lines & _mapping_line_numbers(lines, mapping))


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


def _docker_escape(lines: list[str]) -> str:
    """Return the Dockerfile escape character from its parser directive."""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        match = re.fullmatch(
            r"#\s*escape\s*=\s*([\\`])", stripped, re.IGNORECASE)
        return match.group(1) if match else "\\"
    return "\\"


def _skip_heredocs(lines: list[str], index: int, logical: str) -> int:
    """Return the first line after every heredoc body in one instruction."""
    instruction = logical.lstrip().split(None, 1)[0].upper() if logical.strip() else ""
    matches = list(HEREDOC.finditer(logical)) if instruction in (
        "RUN", "COPY", "ADD") else []
    for delimiter in matches:
        marker = delimiter.group(2)
        strip_tabs = "<<-" in delimiter.group(0)
        while index < len(lines):
            candidate = lines[index].lstrip("\t") if strip_tabs else lines[index]
            index += 1
            if candidate == marker:
                break
        else:
            raise ValueError("unterminated Dockerfile heredoc")
    return index


def _docker_instructions(text: str) -> list[tuple[int, str]]:
    """Return logical Dockerfile instructions outside heredoc bodies."""
    lines = text.splitlines()
    escape = _docker_escape(lines)
    instructions = []
    index = 0
    while index < len(lines):
        start = index
        logical = lines[index]
        while logical.rstrip().endswith(escape) and index + 1 < len(lines):
            logical = logical.rstrip()[:-1] + " " + lines[index + 1].lstrip()
            index += 1
        instructions.append((start, logical))
        index = _skip_heredocs(lines, index + 1, logical)
    return instructions


def _dockerfile_violations(text: str, path: str) -> list[str]:
    """Check the last USER instruction in the final build stage."""
    instructions = _docker_instructions(text)
    starts = [
        index for index, (_, line) in enumerate(instructions)
        if FROM_STAGE.match(line)
    ]
    stage = instructions[starts[-1]:] if starts else instructions
    users = [
        match.group(1).split(" #", 1)[0].strip()
        for _, line in stage
        if (match := USER_INSTRUCTION.match(line))
    ]
    if users and not _is_root_user(users[-1]):
        return []
    if any(_is_exception_comment(line) for _, line in stage):
        return []
    return [f"{path}: final build stage has no non-root USER (Rule 12)"]


def _compose_violations(
    documents: list[object], lines: list[str], exception_lines: set[int], path: str
) -> list[str]:
    """Check every Compose service user value."""
    violations = []
    for document in documents:
        if not isinstance(document, dict) or "services" not in document:
            continue
        services = document.get("services")
        if not isinstance(services, dict):
            violations.append(f"{path}: services must be a mapping (Rule 12)")
            continue
        for name, service in services.items():
            exception = isinstance(service, dict) and _has_mapping_exception(
                lines, exception_lines, service
            )
            user = service.get("user") if isinstance(service, dict) else None
            if exception or (user is not None and not _is_root_user(user)):
                continue
            line = getattr(service, "line", getattr(services, "line", 0)) + 1
            violations.append(
                f"{path}:{line}: service '{name}' has no non-root user (Rule 12)"
            )
    return violations


def _effective_security_value(container: dict, pod: dict, key: str) -> object:
    """Return a container security value with pod-level inheritance."""
    container_context = container.get("securityContext")
    pod_context = pod.get("securityContext")
    if isinstance(container_context, dict) and key in container_context:
        return container_context[key]
    if isinstance(pod_context, dict):
        return pod_context.get(key)
    return None


def _container_violation(
    container: object,
    pod: dict,
    kind: str,
    index: int,
    lines: list[str],
    exception_lines: set[int],
    path: str,
) -> str | None:
    """Return a violation for one Kubernetes container."""
    if not isinstance(container, dict):
        return f"{path}: {kind}[{index}] is not a container mapping (Rule 12)"
    if _has_mapping_exception(lines, exception_lines, container):
        return None
    non_root = _effective_security_value(container, pod, "runAsNonRoot")
    run_as_user = _effective_security_value(container, pod, "runAsUser")
    valid_user = run_as_user is None or (
        type(run_as_user) is int and run_as_user > 0
    )
    if non_root is True and valid_user:
        return None
    name = container.get("name", f"{kind}[{index}]")
    line = getattr(container, "line", 0) + 1
    return f"{path}:{line}: container '{name}' is not non-root (Rule 12)"


def _k8s_violations(
    documents: list[object], lines: list[str], exception_lines: set[int], path: str
) -> list[str]:
    """Check every standard Kubernetes container list independently."""
    violations = []
    kubernetes_documents = [
        document for document in documents
        if isinstance(document, dict)
        and isinstance(document.get("apiVersion"), str)
        and isinstance(document.get("kind"), str)
    ]
    for pod in _walk_mappings(kubernetes_documents):
        for kind in CONTAINER_KEYS:
            if kind not in pod:
                continue
            containers = pod[kind]
            if not isinstance(containers, list):
                violations.append(f"{path}: {kind} must be a list (Rule 12)")
                continue
            for index, container in enumerate(containers):
                violation = _container_violation(
                    container, pod, kind, index, lines, exception_lines, path
                )
                if violation is not None:
                    violations.append(violation)
    return violations


def find_violations(text: str, path: str) -> list[str]:
    """Dispatch by filename and contain all YAML parser failures."""
    name = Path(path).name.lower()
    if name in ("dockerfile", "containerfile") or name.startswith(
        ("dockerfile.", "dockerfile-", "containerfile.", "containerfile-")
    ):
        try:
            return _dockerfile_violations(text, path)
        except ValueError:
            return [f"{path}: malformed Dockerfile cannot be checked (Rule 12)"]
    if not name.endswith((".yml", ".yaml")):
        return []
    try:
        documents = list(yaml.safe_load_all(text))
        lines = text.splitlines()
        exception_lines = _exception_comment_lines(text)
        if name.startswith(("docker-compose", "compose")):
            return _compose_violations(
                documents, lines, exception_lines, path)
        return _k8s_violations(documents, lines, exception_lines, path)
    except Exception:
        return [f"{path}: malformed YAML cannot be checked (Rule 12)"]


def main() -> int:
    """Check each file. Return 0 when all are clean."""
    paths = sys.argv[1:]
    if not paths:
        print("usage: check_dockerfile_root.py FILE [FILE ...]", file=sys.stderr)
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
        "fix: set a non-root user, or add the Rule 12 exception comment",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
