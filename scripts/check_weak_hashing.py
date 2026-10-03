#!/usr/bin/env python3
"""Flag unjustified MD5 and SHA-1 calls (Rule 7)."""
import ast
import io
import re
import sys
import tokenize
from pathlib import Path

HASH_CALL = re.compile(
    r"hashlib\.(?:md5|sha1)\s*\("
    r"|createHash\(\s*['\"](?:md5|sha1)['\"]\s*\)"
    r"|MessageDigest\.getInstance\(\s*['\"](?:md5|sha-?1)['\"]\s*\)"
    r"|\b(?:md5|sha1)\.New\s*\("
    r"|\b(?:MD5|SHA1)\.Create\s*\("
    r"|\b(?:md5|sha1)\s*\("
    r"|Digest::(?:MD5|SHA1)\b",
    re.IGNORECASE,
)
COMMENT_MARKERS = ("#", "//")
WEAK_NAMES = {"md5", "sha1"}
JUSTIFICATION_TERMS = ("non-security", "non-cryptographic")
SECURITY_TERMS = ("authentication", "integrity", "password", "session", "signature", "token")
MAX_VIOLATIONS = 1_000


class _WeakHashVisitor(ast.NodeVisitor):
    """Resolve common hashlib aliases and collect weak calls."""

    def __init__(self) -> None:
        self.module_names: set[str] = {"hashlib"}
        self.weak_names: set[str] = set()
        self.new_names: set[str] = set()
        self.calls: list[ast.Call] = []

    def _kind(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            if node.id in self.module_names:
                return "module"
            if node.id in self.weak_names:
                return "weak"
            if node.id in self.new_names:
                return "new"
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in self.module_names and node.attr in WEAK_NAMES:
                return "weak"
            if node.value.id in self.module_names and node.attr == "new":
                return "new"
        return None

    def _bind(self, target: ast.AST, kind: str | None) -> None:
        if not isinstance(target, ast.Name):
            return
        if kind is None:
            return
        self.module_names.discard(target.id)
        self.weak_names.discard(target.id)
        self.new_names.discard(target.id)
        destination = {
            "module": self.module_names,
            "weak": self.weak_names,
            "new": self.new_names,
        }.get(kind)
        if destination is not None:
            destination.add(target.id)

    def visit_Import(self, node: ast.Import) -> None:
        """Record hashlib module aliases."""
        for alias in node.names:
            if alias.name == "hashlib":
                self._bind(ast.Name(id=alias.asname or "hashlib"), "module")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Record imported weak constructors and hashlib.new."""
        if node.module != "hashlib":
            return
        for alias in node.names:
            if alias.name == "*":
                self.weak_names.update(WEAK_NAMES)
                self.new_names.add("new")
                continue
            name = alias.asname or alias.name
            if alias.name in WEAK_NAMES:
                self._bind(ast.Name(id=name), "weak")
            elif alias.name == "new":
                self._bind(ast.Name(id=name), "new")

    def visit_Assign(self, node: ast.Assign) -> None:
        """Track direct assignment aliases in source order."""
        self.visit(node.value)
        for target in node.targets:
            self._bind_assignment(target, node.value)

    def _bind_assignment(self, target: ast.AST, value: ast.AST) -> None:
        """Bind direct names and equal-length destructuring aliases."""
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(
            value, (ast.Tuple, ast.List)
        ) and len(target.elts) == len(value.elts):
            for child_target, child_value in zip(target.elts, value.elts):
                self._bind_assignment(child_target, child_value)
            return
        self._bind(target, self._kind(value))

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Track annotated assignment aliases."""
        kind = self._kind(node.value) if node.value is not None else None
        if node.value is not None:
            self.visit(node.value)
        self._bind(node.target, kind)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        """Track assignment-expression aliases."""
        kind = self._kind(node.value)
        self.visit(node.value)
        self._bind(node.target, kind)

    def visit_Call(self, node: ast.Call) -> None:
        """Collect direct weak calls and constant-name hashlib.new calls."""
        kind = self._kind(node.func)
        weak = kind == "weak"
        if kind == "new":
            name_arg = node.args[0] if node.args else None
            for keyword in node.keywords:
                if keyword.arg == "name":
                    name_arg = keyword.value
                    break
            weak = (
                isinstance(name_arg, ast.Constant)
                and isinstance(name_arg.value, str)
                and name_arg.value.lower() in WEAK_NAMES
            )
        if weak:
            self.calls.append(node)
        self.generic_visit(node)


def _python_comments(text: str) -> dict[int, list[tuple[int, str]]]:
    """Return actual, nonempty Python comments by line."""
    comments: dict[int, list[tuple[int, str]]] = {}
    tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    for token in tokens:
        if token.type == tokenize.COMMENT and token.string[1:].strip():
            comments.setdefault(token.start[0], []).append(
                (token.start[1], token.string[1:].strip()))
    return comments


def _is_justification(comment: str) -> bool:
    """Return whether a comment explicitly names a non-security use."""
    lowered = comment.lower()
    names_non_security_use = any(
        term in lowered for term in JUSTIFICATION_TERMS)
    names_security_use = any(term in lowered for term in SECURITY_TERMS)
    return names_non_security_use and not names_security_use


def _has_python_comment(
    call: ast.Call, comments: dict[int, list[tuple[int, str]]],
) -> bool:
    """Return whether a valid justification follows the call."""
    line = getattr(call, "end_lineno", call.lineno)
    column = getattr(call, "end_col_offset", call.col_offset)
    return any(
        comment_column >= column and _is_justification(comment)
        for comment_column, comment in comments.get(line, [])
    )


def _python_violations(text: str, path: str) -> list[str]:
    """Analyze Python syntax and fail closed when parsing is impossible."""
    try:
        tree = ast.parse(text, filename=path)
        comments = _python_comments(text)
        visitor = _WeakHashVisitor()
        visitor.visit(tree)
    except Exception:
        return [f"{path}: malformed Python cannot be checked (Rule 7)"]
    violations = [
        f"{path}:{call.lineno}: MD5/SHA-1 call without a "
        "justification comment (Rule 7)"
        for call in visitor.calls
        if not _has_python_comment(call, comments)
    ]
    return violations[:MAX_VIOLATIONS]


def _has_text_comment(line: str, after: int) -> bool:
    """Preserve the existing non-Python same-line comment behavior."""
    tail = line[after:]
    stripped = tail.lstrip(" ;")
    return any(
        stripped.startswith(marker)
        and _is_justification(stripped[len(marker):].strip())
        for marker in COMMENT_MARKERS)


def _text_violations(text: str, path: str) -> list[str]:
    """Apply the conservative legacy matcher to non-Python files."""
    violations = []
    for match in HASH_CALL.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]
        if _has_text_comment(line, match.end() - line_start):
            continue
        number = text.count("\n", 0, match.start()) + 1
        violations.append(
            f"{path}:{number}: MD5/SHA-1 call without a "
            "justification comment (Rule 7)"
        )
        if len(violations) >= MAX_VIOLATIONS:
            return violations
    return violations


def find_violations(text: str, path: str) -> list[str]:
    """Return one violation per unjustified weak-hash call."""
    if Path(path).suffix.lower() in {".py", ".pyw"}:
        return _python_violations(text, path)
    return _text_violations(text, path)


def main() -> int:
    """Check each source file. Return 0 when all are clean."""
    paths = sys.argv[1:]
    if not paths:
        print("usage: check_weak_hashing.py FILE [FILE ...]", file=sys.stderr)
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
        "fix: use SHA-256/bcrypt, or add a same-line comment naming "
        "the non-security use",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
