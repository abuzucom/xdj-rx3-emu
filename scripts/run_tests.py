#!/usr/bin/env python3
"""Run every unittest class concurrently with bounded subprocesses."""
import collections
import os
import sys
import time
import unittest

try:
    from scripts import check_hook_coverage as shard_runner
except ImportError:
    import check_hook_coverage as shard_runner

DIAGNOSTIC_LIMIT = 4000


def _ensure_import_root(root: str) -> None:
    """Make test modules importable from the repository root."""
    resolved_root = os.path.abspath(root)
    if resolved_root not in sys.path:
        sys.path.insert(0, resolved_root)


def _class_shard(test) -> tuple[str, str]:
    """Return the import name and display label for one test class."""
    test_class = test.__class__
    import_name = f"{test_class.__module__}.{test_class.__qualname__}"
    module_path = test_class.__module__.replace(".", "/") + ".py"
    return import_name, f"{module_path}::{test_class.__qualname__}"


def _suite_ids(suite: unittest.TestSuite) -> collections.Counter:
    """Return every test ID in a nested suite."""
    identifiers = []
    stack = [suite]
    while stack:
        item = stack.pop()
        if isinstance(item, unittest.TestSuite):
            stack.extend(reversed(list(item)))
        else:
            identifiers.append(item.id())
    return collections.Counter(identifiers)


def _discovered_tests(
        root: str) -> tuple[collections.Counter, list[tuple[str, str]]]:
    """Return test IDs and class shards from one discovery pass."""
    _ensure_import_root(root)
    tests = os.path.join(root, "tests")
    suite = unittest.TestLoader().discover(tests, pattern="test_*.py")
    identifiers = collections.Counter()
    shards = {}
    stack = [suite]
    while stack:
        item = stack.pop()
        if isinstance(item, unittest.TestSuite):
            stack.extend(reversed(list(item)))
            continue
        identifiers[item.id()] += 1
        import_name, label = _class_shard(item)
        shards[import_name] = label
    ordered = sorted((label, name) for name, label in shards.items())
    return identifiers, ordered


def _sharded_ids(shards: list[tuple[str, str]]) -> collections.Counter:
    """Return test IDs loaded through every class shard."""
    loader = unittest.TestLoader()
    identifiers = collections.Counter()
    for _, import_name in shards:
        identifiers.update(_suite_ids(loader.loadTestsFromName(import_name)))
    return identifiers


def _test_module_names(root: str) -> set[str]:
    """Return import names that test discovery can load below root."""
    tests = os.path.join(root, "tests")
    names = set()
    for directory, _, files in os.walk(tests):
        relative = os.path.relpath(directory, tests)
        package = [] if relative == "." else relative.split(os.sep)
        for filename in files:
            if filename.startswith("test_") and filename.endswith(".py"):
                stem = filename[:-3]
                names.add(".".join(package + [stem]))
                names.add(".".join(["tests"] + package + [stem]))
    return names


def validated_test_shards(root: str) -> list[tuple[str, str]]:
    """Return shards only when sharding preserves exact discovery."""
    module_names = _test_module_names(root)
    saved_modules = {
        name: sys.modules.pop(name)
        for name in module_names
        if name in sys.modules
    }
    original_path = list(sys.path)
    try:
        expected, shards = _discovered_tests(root)
        actual = _sharded_ids(shards)
        if not expected or actual != expected:
            raise RuntimeError("class sharding changed the discovered test set")
        return shards
    finally:
        sys.path[:] = original_path
        for name in module_names:
            sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


def _safe_diagnostic(problem: str) -> str:
    """Return bounded escaped test output."""
    rendered = ascii(problem[-DIAGNOSTIC_LIMIT:])
    return rendered[:DIAGNOSTIC_LIMIT]


def run_suite(root: str, *, workers: int, timeout: float) -> int:
    """Run validated class shards and return a process exit code."""
    try:
        shards = validated_test_shards(root)
        problems = shard_runner.run_test_shards(
            root,
            dict(os.environ),
            workers=workers,
            timeout=timeout,
            test_shards=shards,
        )
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        diagnostic = _safe_diagnostic(f"{type(error).__name__}: {error}")
        print(diagnostic, file=sys.stderr)
        print(
            "error: test discovery failed; repair the suite and retry",
            file=sys.stderr,
        )
        return 1
    if not problems:
        print("all test shards passed")
        return 0
    for problem in problems:
        print(_safe_diagnostic(problem), file=sys.stderr)
    print("error: test shards failed; fix the reported tests and retry", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    """Run the repository suite with project defaults."""
    if argv:
        print("error: run_tests.py accepts no arguments", file=sys.stderr)
        return 2
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    started = time.monotonic()
    result = run_suite(
        root,
        workers=shard_runner.DEFAULT_WORKERS,
        timeout=shard_runner.TEST_SHARD_TIMEOUT_SECONDS,
    )
    elapsed = time.monotonic() - started
    print(f"test suite finished in {elapsed:.1f}s")
    return result


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
