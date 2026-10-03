#!/usr/bin/env python3
"""Enforce focused structure limits on new command-policy runtime modules."""
import ast
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_PATHS = (
    "hooks/_cmd_parser.py",
    "hooks/_platform_policy.py",
    "hooks/block_destructive_cmd.py",
    "tests/json_line_worker.py",
    "tests/json_line_worker_child.py",
)
MAX_FUNCTION_LINES = 60
MAX_LINE_CHARACTERS = 120
MAX_LOCAL_NAMES = 9
MAX_CONTROL_DEPTH = 3
CONTROL_NODES = (ast.For, ast.If, ast.Match, ast.Try, ast.While, ast.With)
LOOP_NODES = (ast.For, ast.While)


def parse_runtime_module(relative_path: str) -> tuple[str, ast.Module]:
    """Return source and syntax tree for one runtime module."""
    source = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
    return source, ast.parse(source, relative_path)


def function_local_names(function_node: ast.FunctionDef) -> set[str]:
    """Return assigned local names without entering nested function scopes."""
    local_names = set()
    pending_nodes = list(ast.iter_child_nodes(function_node))
    while pending_nodes:
        current_node = pending_nodes.pop()
        if isinstance(current_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(current_node, ast.Name) and isinstance(current_node.ctx, ast.Store):
            local_names.add(current_node.id)
        pending_nodes.extend(ast.iter_child_nodes(current_node))
    return local_names


def function_structure_problems(function_node: ast.FunctionDef) -> list[str]:
    """Return nesting and nested-loop break problems for one function."""
    problems = []
    pending_nodes = [(function_node, 0, 0)]
    while pending_nodes:
        current_node, control_depth, loop_depth = pending_nodes.pop()
        if current_node is not function_node and isinstance(
            current_node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
        ):
            continue
        next_depth = control_depth + int(isinstance(current_node, CONTROL_NODES))
        next_loop_depth = loop_depth + int(isinstance(current_node, LOOP_NODES))
        if next_depth > MAX_CONTROL_DEPTH:
            problems.append(f"{function_node.name} exceeds control depth")
            return problems
        if isinstance(current_node, ast.Break) and loop_depth > 1:
            problems.append(f"{function_node.name} breaks from a nested loop")
            return problems
        for child_node in ast.iter_child_nodes(current_node):
            pending_nodes.append((child_node, next_depth, next_loop_depth))
    return problems


class RuntimeStructureTest(unittest.TestCase):
    """New runtime modules retain bounded and readable implementation shape."""

    def test_runtime_modules_meet_static_limits(self):
        problems = []
        for relative_path in RUNTIME_PATHS:
            source, syntax_tree = parse_runtime_module(relative_path)
            for line_number, source_line in enumerate(source.splitlines(), 1):
                if len(source_line) > MAX_LINE_CHARACTERS:
                    problems.append(f"{relative_path}:{line_number} exceeds line limit")
            for syntax_node in ast.walk(syntax_tree):
                if isinstance(syntax_node, ast.Lambda):
                    problems.append(f"{relative_path}:{syntax_node.lineno} uses lambda")
                if not isinstance(syntax_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                function_lines = syntax_node.end_lineno - syntax_node.lineno + 1
                if function_lines > MAX_FUNCTION_LINES:
                    problems.append(f"{relative_path}:{syntax_node.name} is too long")
                if len(function_local_names(syntax_node)) > MAX_LOCAL_NAMES:
                    problems.append(f"{relative_path}:{syntax_node.name} has too many locals")
                problems.extend(
                    f"{relative_path}:{problem}"
                    for problem in function_structure_problems(syntax_node)
                )
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
