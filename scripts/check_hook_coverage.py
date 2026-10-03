#!/usr/bin/env python3
"""Fail when a hook statement no test reaches is not a recorded one.

The gates run as subprocesses, so ordinary in-process coverage sees
almost none of their decision code and reports the opposite of the truth.
This runs the suite with tools/hook-trace on PYTHONPATH, which traces
every interpreter, and compares what went unrun against a committed baseline.

The baseline is per function rather than per line, so an edit above a
function does not churn it. A function gaining unreached statements fails
the check, and so does a baseline that has gone stale: a gate nobody can
fail is the failure mode this whole check exists to avoid.

Regenerate with --write-baseline and commit the result.
"""
import ast
import concurrent.futures
import io
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections import deque
from dataclasses import dataclass
from threading import Lock

BASELINE = "hook-coverage-baseline.json"
TARGET = "hooks"
# Not tools/coverage: a "coverage/" line is one of the commonest
# .gitignore entries, and it silently excluded this file from an
# adopting repository, which leaves the gate reporting every statement
# as unreached.
TRACER_DIR = os.path.join("tools", "hook-trace")
DEFAULT_WORKERS = 4
TEST_SHARD_TIMEOUT_SECONDS = 30
RESOURCE_SHARD_TIMEOUT_SECONDS = 180
PROGRESS_INTERVAL_SECONDS = 10
PROCESS_SHUTDOWN_TIMEOUT_SECONDS = 5
PRIORITY_TEST_SHARDS = (
    "test_enforce_git_identity.py::PreToolUseTest",
    "test_immutable_compliance.py::ImmutableComplianceScannerTest",
    "test_check_conflict_markers.py::CliExecutionTest",
    "test_check_conflict_markers.py::GitOptionSupportTest",
    "test_enforce_git_identity.py::CheckerContractTest",
    "test_enforce_git_identity.py::SessionStartTest",
    "test_block_destructive_bash.py::RepoExecutesOnReadTest",
    "test_enforce_branch_name.py::PreToolUseTest",
    "test_check_commit_message.py::MergeCommitTest",
    "test_enforce_git_identity.py::CommitRangeTest",
    "test_check_conflict_markers.py::SecurityHardeningTest",
    "test_check_conflict_markers.py::SparseCheckoutTest",
)
RESOURCE_HEAVY_TEST_SHARDS = frozenset(PRIORITY_TEST_SHARDS)
EXCLUSIVE_TEST_SHARDS = RESOURCE_HEAVY_TEST_SHARDS


@dataclass(frozen=True)
class TestShardResult:
    """Store one traced test class result."""

    name: str
    elapsed: float
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


class ActiveProcessRegistry:
    """Track active shard processes for fail-fast process-tree termination."""

    def __init__(self) -> None:
        self.processes: dict[str, subprocess.Popen] = {}
        self.lock = Lock()

    def register_process(self, label: str, process: subprocess.Popen) -> None:
        """Register one active test process by shard label."""
        with self.lock:
            self.processes[label] = process

    def unregister_process(self, label: str) -> None:
        """Remove one completed test process."""
        with self.lock:
            self.processes.pop(label, None)

    def terminate_all_processes(self) -> None:
        """Terminate every active process tree from a stable snapshot."""
        with self.lock:
            active_processes = tuple(self.processes.values())
        for active_process in active_processes:
            if active_process.poll() is None:
                terminate_process_tree(active_process)


def is_docstring(node, parent) -> bool:
    """Return True if `node` is `parent`'s docstring rather than code.

    A docstring compiles into __doc__ instead of executing, so it never
    appears in a trace count. Counting one as unreached measures the
    tracer, not the code.
    """
    return (bool(parent.body) and node is parent.body[0]
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def function_statements(path: str) -> dict:
    """Return {line: function name} for every real statement in a function."""
    with io.open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), path)
    owned = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.stmt) or child is node:
                continue
            if is_docstring(child, node):
                continue
            owned.setdefault(child.lineno, node.name)
    return owned


def traced_lines(out_dir: str) -> set:
    """Return every (path, line) any traced interpreter executed."""
    hit = set()
    for name in os.listdir(out_dir):
        with io.open(os.path.join(out_dir, name), encoding="utf-8") as handle:
            records = json.load(handle)
        if not isinstance(records, list):
            raise ValueError("trace file must contain a list")
        for record in records:
            valid = (isinstance(record, list) and len(record) == 2
                     and isinstance(record[0], str)
                     and isinstance(record[1], int)
                     and not isinstance(record[1], bool))
            if not valid:
                raise ValueError("trace file contains an invalid line record")
            hit.add((record[0], record[1]))
    return hit


def unreached_counts(root: str, hit: set) -> dict:
    """Return {"file.py::function": unreached statement count}."""
    counts = {}
    target = os.path.join(root, TARGET)
    for name in sorted(os.listdir(target)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(target, name)
        owned = function_statements(path)
        for line, function in owned.items():
            if (os.path.abspath(path), line) in hit:
                continue
            key = "%s::%s" % (name, function)
            counts[key] = counts.get(key, 0) + 1
    return counts


def discover_test_shards(root: str) -> list[tuple[str, str]]:
    """Return display labels and import names for discovered test classes."""
    tests = os.path.join(root, "tests")
    resolved_root = os.path.abspath(root)
    if resolved_root not in sys.path:
        sys.path.insert(0, resolved_root)
    remove_stale_test_modules(tests)
    loader = unittest.TestLoader()
    suite = loader.discover(tests, pattern="test_*.py")
    stack = [suite]
    shards = {}
    while stack:
        item = stack.pop()
        if isinstance(item, unittest.TestSuite):
            stack.extend(reversed(list(item)))
            continue
        test_class = item.__class__
        import_name = "%s.%s" % (
            test_class.__module__, test_class.__qualname__)
        module_path = test_class.__module__.replace(".", "/") + ".py"
        shards[import_name] = "%s::%s" % (
            module_path, test_class.__qualname__)
    return sorted((label, name) for name, label in shards.items())


def remove_stale_test_modules(tests_directory: str) -> None:
    """Remove temporary test modules imported from a previous test root."""
    resolved_tests_directory = os.path.realpath(tests_directory)
    for module_name, module in tuple(sys.modules.items()):
        module_path = getattr(module, "__file__", "")
        if not module_name.startswith("test_") or not module_path:
            continue
        resolved_module_path = os.path.realpath(module_path)
        if not resolved_module_path.startswith(resolved_tests_directory + os.sep):
            sys.modules.pop(module_name, None)


def prioritize_test_shards(
        test_shards: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return measured slow shards first and all other shards lexically."""
    ordinary_priority = len(PRIORITY_TEST_SHARDS)
    decorated_shards = [
        (
            shard_priority(label, ordinary_priority),
            label,
            import_name,
        )
        for label, import_name in test_shards
    ]
    decorated_shards.sort()
    return [
        (label, import_name)
        for _priority, label, import_name in decorated_shards
    ]


def shard_priority(label: str, ordinary_priority: int) -> int:
    """Return the configured priority for one class or method label."""
    for index, priority_label in enumerate(PRIORITY_TEST_SHARDS):
        if label == priority_label or label.startswith(priority_label + "."):
            return index
    return ordinary_priority


def is_resource_heavy(label: str) -> bool:
    """Return whether a class or method label receives the resource timeout."""
    return any(
        label == resource_label or label.startswith(resource_label + ".")
        for resource_label in RESOURCE_HEAVY_TEST_SHARDS
    )


def terminate_process_tree(process: subprocess.Popen) -> None:
    """Stop a timed-out test process and all descendants."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, check=False, text=True,
            encoding="utf-8", errors="replace")
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=PROCESS_SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_test_shard(
    root: str,
    environment: dict,
    label: str,
    import_name: str,
    timeout: float,
    process_registry: ActiveProcessRegistry | None = None,
) -> TestShardResult:
    """Run one test class with a timeout and return its captured result."""
    print("hook coverage: started %s" % label, file=sys.stderr, flush=True)
    started = time.monotonic()
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        [sys.executable, "-m", "unittest", import_name],
        cwd=root, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        start_new_session=os.name != "nt", creationflags=creationflags)
    if process_registry is not None:
        process_registry.register_process(label, process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        terminate_process_tree(process)
        stdout, stderr = process.communicate()
        timed_out = True
    finally:
        if process_registry is not None:
            process_registry.unregister_process(label)
    elapsed = time.monotonic() - started
    return TestShardResult(
        label, elapsed, process.returncode, stdout, stderr, timed_out)


def result_problem(result: TestShardResult, timeout: float) -> str:
    """Return an actionable problem for a failed worker."""
    if result.timed_out:
        return "%s timed out after %.1f seconds" % (result.name, timeout)
    output = result.stderr or result.stdout
    return "%s failed with exit code %d:\n%s" % (
        result.name, result.returncode, output[-2000:])


def record_result(result: TestShardResult, timeout: float) -> str:
    """Report one completed shard and return its actionable problem."""
    status = "passed"
    problem = ""
    if result.timed_out:
        status = "timed out"
        problem = result_problem(result, timeout)
    elif result.returncode != 0:
        status = "failed"
        problem = result_problem(result, timeout)
    print("hook coverage: %s %s (%.1fs)"
          % (status, result.name, result.elapsed),
          file=sys.stderr, flush=True)
    return problem


def submit_available_shards(
    executor: concurrent.futures.ThreadPoolExecutor,
    active_futures: dict,
    ordinary_shards: deque,
    exclusive_shards: deque,
    worker_count: int,
    shard_arguments: tuple,
) -> None:
    """Fill workers while keeping exclusive work isolated."""
    exclusive_is_active = any(details[2] for details in active_futures.values())
    if exclusive_is_active:
        return
    while len(active_futures) < worker_count:
        is_exclusive = False
        if exclusive_shards and not active_futures:
            label, import_name = exclusive_shards.popleft()
            is_exclusive = True
        elif ordinary_shards:
            label, import_name = ordinary_shards.popleft()
        else:
            return
        shard_timeout = shard_arguments[2]
        if is_resource_heavy(label):
            shard_timeout = max(shard_timeout, RESOURCE_SHARD_TIMEOUT_SECONDS)
        future = executor.submit(
            run_test_shard,
            shard_arguments[0],
            shard_arguments[1],
            label,
            import_name,
            shard_timeout,
            shard_arguments[3],
        )
        active_futures[future] = (
            label, import_name, is_exclusive, shard_timeout)
        if is_exclusive:
            return


def collect_completed_results(
    completed_futures: set,
    active_futures: dict,
) -> str:
    """Record completed futures and return the first actionable failure."""
    for completed_future in completed_futures:
        details = active_futures.pop(completed_future)
        label, _import_name, _is_exclusive, shard_timeout = details
        try:
            shard_result = completed_future.result()
        except Exception as error:
            return f"{label} worker failed before reporting a result: {error}"
        problem = record_result(shard_result, shard_timeout)
        if problem:
            return problem
    return ""


def run_test_shards(root: str, environment: dict,
                    workers: int = DEFAULT_WORKERS,
                    timeout: float = TEST_SHARD_TIMEOUT_SECONDS,
                    test_shards: list[tuple[str, str]] | None = None) -> list[str]:
    """Run traced test classes concurrently and return worker problems."""
    if workers <= 0:
        raise ValueError("coverage workers must be positive")
    if timeout <= 0:
        raise ValueError("coverage timeout must be positive")
    if test_shards is None:
        test_shards = discover_test_shards(root)
    test_shards = prioritize_test_shards(test_shards)
    if not test_shards:
        raise ValueError("coverage test discovery returned no shards")
    exclusive_shards = deque(
        shard for shard in test_shards
        if shard[0] in EXCLUSIVE_TEST_SHARDS)
    ordinary_shards = deque(
        shard for shard in test_shards
        if shard[0] not in EXCLUSIVE_TEST_SHARDS)
    worker_count = min(workers, len(test_shards))
    print("hook coverage: starting %d test shards with %d workers"
          % (len(test_shards), worker_count), file=sys.stderr, flush=True)
    environment = dict(environment)
    test_path = os.path.join(root, "tests")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
        environment.get("PYTHONPATH", ""), test_path)))
    process_registry = ActiveProcessRegistry()
    active_futures = {}
    executor = concurrent.futures.ThreadPoolExecutor(worker_count)
    try:
        shard_arguments = (root, environment, timeout, process_registry)
        submit_available_shards(
            executor, active_futures, ordinary_shards,
            exclusive_shards, worker_count, shard_arguments)
        while active_futures:
            completed_futures, _pending_futures = concurrent.futures.wait(
                set(active_futures), timeout=PROGRESS_INTERVAL_SECONDS,
                return_when=concurrent.futures.FIRST_COMPLETED)
            if not completed_futures:
                remaining_count = (
                    len(active_futures)
                    + len(ordinary_shards)
                    + len(exclusive_shards)
                )
                print("hook coverage: %d test shards remain"
                      % remaining_count,
                      file=sys.stderr, flush=True)
                continue
            problem = collect_completed_results(
                completed_futures, active_futures)
            if problem:
                process_registry.terminate_all_processes()
                for active_future in active_futures:
                    active_future.cancel()
                return [problem]
            submit_available_shards(
                executor, active_futures, ordinary_shards,
                exclusive_shards, worker_count, shard_arguments)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return []


def measure(root: str) -> dict:
    """Run the suite under the tracer and return the unreached counts."""
    out_dir = tempfile.mkdtemp(prefix="hook-coverage-")
    try:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.path.join(root, TRACER_DIR)
        environment["HOOK_COVERAGE_TARGET"] = os.path.join(root, TARGET)
        environment["HOOK_COVERAGE_OUT"] = out_dir
        problems = run_test_shards(root, environment)
        if problems:
            print("the suite failed under tracing, so coverage says nothing:",
                  file=sys.stderr)
            for problem in problems:
                print(problem, file=sys.stderr)
            raise SystemExit(1)
        return unreached_counts(root, traced_lines(out_dir))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def compare(actual: dict, recorded: dict) -> list:
    """Return one line per disagreement between the run and the baseline."""
    problems = []
    for key in sorted(set(actual) | set(recorded)):
        now = actual.get(key, 0)
        was = recorded.get(key, 0)
        if now > was:
            problems.append(
                "%s: %d unreached statements, %d recorded. New code no test "
                "reaches." % (key, now, was))
        elif now < was:
            problems.append(
                "%s: %d unreached statements, %d recorded. The baseline is "
                "stale." % (key, now, was))
    return problems


def read_baseline(root: str) -> dict:
    """Return the committed baseline, or an empty mapping when absent."""
    path = os.path.join(root, BASELINE)
    if not os.path.isfile(path):
        return {}
    with io.open(path, encoding="utf-8") as handle:
        return json.load(handle).get("unreached", {})


def write_baseline(root: str, counts: dict) -> int:
    """Record `counts` as the baseline and return an exit code.

    Refuses to run as root. Permission-dependent branches do not fire for
    root, which reads a mode 000 file happily, so a baseline written here
    records fewer unreached statements than CI finds and fails the check
    it was written to satisfy. Two OSError branches in require_consent.py
    behave exactly this way.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("refusing to write a baseline as root: a mode 000 file is "
              "readable for root, so the permission-dependent branches this "
              "records would not fire, and the result would not match CI. "
              "Run this as an ordinary user.", file=sys.stderr)
        return 1
    body = {
        "note": ("Statements in hooks/ that the suite, subprocesses included, "
                 "does not run, counted per function. Regenerate with "
                 "scripts/check_hook_coverage.py --write-baseline. Every entry "
                 "needs a reason in docs/gate-threat-model.md; an entry nobody "
                 "can explain is untested code, not a recorded limit."),
        "unreached": dict(sorted(counts.items())),
    }
    path = os.path.join(root, BASELINE)
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(body, indent=2) + "\n")
    print("recorded %d functions with unreached statements in %s"
          % (len(counts), BASELINE))
    return 0


def main(argv: list) -> int:
    """Measure, then either record the result or check it. Return an exit code."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    actual = measure(root)
    if "--write-baseline" in argv:
        return write_baseline(root, actual)
    problems = compare(actual, read_baseline(root))
    if not problems:
        print("hook coverage matches %s (%d functions recorded)"
              % (BASELINE, len(actual)))
        return 0
    for line in problems:
        print(line, file=sys.stderr)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("Running as root. A mode 000 file is readable for root, so the "
              "permission-dependent branches read as unreached here and do "
              "not in CI, which is where this baseline was measured. Compare "
              "as an ordinary user before believing this.", file=sys.stderr)
    print("Add a test, or record the limit: run "
          "scripts/check_hook_coverage.py --write-baseline and say in "
          "docs/gate-threat-model.md why the statement is unreachable.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
