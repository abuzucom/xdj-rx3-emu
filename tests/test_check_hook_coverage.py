#!/usr/bin/env python3
"""Cover the hook-coverage gate's comparison and statement accounting.

Deliberately does not run the gate end to end: it launches the whole
suite under a tracer, and a suite that runs itself does not terminate.
CI runs it directly, as its own step.

The comparison is what the gate is. A gate that cannot fail is the thing
this check exists to prevent, so the failing directions are pinned first.
"""
import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# scripts/ is never on the path, however the suite is launched.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_hook_coverage as gate

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = REPO_ROOT / "hook-coverage-baseline.json"


class CompareTest(unittest.TestCase):
    """Both directions of disagreement have to fail."""

    def test_a_matching_run_reports_nothing(self):
        counts = {"a.py::f": 2, "b.py::g": 1}
        self.assertEqual(gate.compare(counts, dict(counts)), [])

    def test_an_unrecorded_function_fails(self):
        problems = gate.compare({"a.py::f": 1}, {})
        self.assertEqual(len(problems), 1)
        self.assertIn("New code no test reaches", problems[0])
        self.assertIn("a.py::f", problems[0])

    def test_more_unreached_than_recorded_fails(self):
        problems = gate.compare({"a.py::f": 3}, {"a.py::f": 1})
        self.assertEqual(len(problems), 1)
        self.assertIn("New code no test reaches", problems[0])

    def test_fewer_unreached_than_recorded_fails_as_stale(self):
        """A baseline nobody updates stops meaning anything."""
        problems = gate.compare({"a.py::f": 1}, {"a.py::f": 3})
        self.assertEqual(len(problems), 1)
        self.assertIn("stale", problems[0])

    def test_a_function_that_became_covered_fails_as_stale(self):
        problems = gate.compare({}, {"a.py::f": 2})
        self.assertEqual(len(problems), 1)
        self.assertIn("stale", problems[0])

    def test_every_disagreement_is_reported_not_just_the_first(self):
        problems = gate.compare({"a.py::f": 2}, {"b.py::g": 1})
        self.assertEqual(len(problems), 2)


class StatementAccountingTest(unittest.TestCase):
    """Only real statements inside functions count."""

    def write(self, body: str) -> str:
        """Write `body` to a temp module and return its path."""
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8")
        handle.write(body)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return handle.name

    def test_a_docstring_is_not_a_statement(self):
        path = self.write('def f():\n    """Doc."""\n    return 1\n')
        owned = gate.function_statements(path)
        self.assertEqual(sorted(owned), [3])

    def test_module_level_code_is_not_counted(self):
        path = self.write("CONSTANT = 1\n\n\ndef f():\n    return CONSTANT\n")
        owned = gate.function_statements(path)
        self.assertEqual(sorted(owned), [5])

    def test_nested_statements_belong_to_their_function(self):
        path = self.write("def f():\n    for x in (1,):\n        print(x)\n")
        owned = gate.function_statements(path)
        self.assertEqual(set(owned.values()), {"f"})
        self.assertEqual(sorted(owned), [2, 3])

    def test_a_string_that_is_not_first_is_a_statement(self):
        path = self.write('def f():\n    x = 1\n    "not a docstring"\n')
        owned = gate.function_statements(path)
        self.assertEqual(sorted(owned), [2, 3])


class TracedLineFileTest(unittest.TestCase):
    """Trace files use data-only serialization."""

    def test_json_trace_file_is_read(self):
        with tempfile.TemporaryDirectory() as out_dir:
            path = str(REPO_ROOT / "hooks" / "example.py")
            trace_file = Path(out_dir) / "trace.json"
            trace_file.write_text(
                json.dumps([[path, 7], [path, 11]]), encoding="utf-8")

            self.assertEqual(
                gate.traced_lines(out_dir), {(path, 7), (path, 11)})


class TracerScopeTest(unittest.TestCase):
    """The subprocess tracer limits instrumentation to hook source."""

    def test_target_lines_do_not_require_global_line_tracing(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            target = root_path / "hooks"
            output = root_path / "trace-output"
            target.mkdir()
            output.mkdir()
            module = target / "sample_hook.py"
            module.write_text(
                "import sys\n\ndef traced():\n"
                "    return sys._getframe().f_trace is None\n",
                encoding="utf-8",
            )
            probe = root_path / "probe.py"
            probe.write_text(
                "import sys\nimport sample_hook\n"
                "print(sys._getframe().f_trace is None)\n"
                "print(sample_hook.traced())\n",
                encoding="utf-8",
            )
            environment = dict(os.environ)
            tracer = REPO_ROOT / "tools" / "hook-trace"
            environment["PYTHONPATH"] = os.pathsep.join((str(tracer), str(target)))
            environment["HOOK_COVERAGE_TARGET"] = str(target)
            environment["HOOK_COVERAGE_OUT"] = str(output)
            result = subprocess.run(
                [sys.executable, str(probe)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            target_has_no_trace = str(hasattr(sys, "monitoring"))
            self.assertEqual(result.stdout.splitlines(), ["True", target_has_no_trace])
            self.assertTrue(any(path == str(module) for path, _line in gate.traced_lines(output)))


class BaselineFileTest(unittest.TestCase):
    """The committed baseline stays readable and matches this tree's shape.

    Skipped when there is no baseline yet. Generating the first one runs
    this suite, so a suite that requires the file makes it impossible to
    produce, which is what a second installation of this gate found. CI
    still fails on a missing baseline, through the gate rather than here.
    """

    def setUp(self):
        if not BASELINE.is_file():
            self.skipTest("no baseline yet; run --write-baseline to make one")

    def test_the_baseline_is_present_and_parses(self):
        body = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertIn("unreached", body)
        self.assertIn("note", body)

    def test_every_recorded_function_still_exists(self):
        """A renamed function must not leave a silent entry behind."""
        body = json.loads(BASELINE.read_text(encoding="utf-8"))
        for key in body["unreached"]:
            name, _, function = key.partition("::")
            path = REPO_ROOT / "hooks" / name
            with self.subTest(key=key):
                self.assertTrue(path.is_file(), f"{name} is gone")
                source = io.open(path, encoding="utf-8").read()
                tree = ast.parse(source, str(path))
                defined = {
                    node.name for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                self.assertIn(function, defined)

    def test_a_missing_baseline_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(gate.read_baseline(root), {})


if __name__ == "__main__":
    unittest.main()
