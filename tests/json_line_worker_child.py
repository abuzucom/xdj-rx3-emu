#!/usr/bin/env python3
"""Run one fixed persistent worker protocol for hook and CLI tests."""
import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path


WORKER_MODES = frozenset({"hook", "main"})


def load_target_module(target_path: Path):
    """Import one explicit Python target for persistent test invocation."""
    target_directory = str(target_path.parent)
    if target_directory not in sys.path:
        sys.path.insert(0, target_directory)
    module_spec = importlib.util.spec_from_file_location(
        "persistent_worker_target",
        target_path,
    )
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("persistent worker target cannot be imported")
    target_module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(target_module)
    return target_module


def invoke_hook(target_module, request: dict) -> dict:
    """Invoke one hook request with isolated standard streams."""
    captured_output = io.StringIO()
    captured_error = io.StringIO()
    original_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO(json.dumps(request))
        with contextlib.redirect_stdout(captured_output):
            with contextlib.redirect_stderr(captured_error):
                return_code = target_module.main()
    finally:
        sys.stdin = original_stdin
    return {
        "code": return_code,
        "stdout": captured_output.getvalue(),
        "stderr": captured_error.getvalue(),
    }


def invoke_main(target_path: Path, target_module, request: dict) -> dict:
    """Invoke one CLI main function with isolated process-global state."""
    captured_output = io.StringIO()
    captured_error = io.StringIO()
    original_arguments = sys.argv
    original_directory = os.getcwd()
    original_environment = os.environ.copy()
    original_stdin = sys.stdin
    try:
        if request.get("change_directory", True):
            os.chdir(request["cwd"])
        os.environ.clear()
        os.environ.update(request["environment"])
        sys.argv = [str(target_path), *request["arguments"]]
        sys.stdin = io.StringIO(request["stdin"])
        with contextlib.redirect_stdout(captured_output):
            with contextlib.redirect_stderr(captured_error):
                return_code = target_module.main()
    finally:
        sys.argv = original_arguments
        os.chdir(original_directory)
        os.environ.clear()
        os.environ.update(original_environment)
        sys.stdin = original_stdin
    return {
        "code": return_code,
        "stdout": captured_output.getvalue(),
        "stderr": captured_error.getvalue(),
    }


def process_requests(worker_mode: str, target_path: Path) -> None:
    """Process JSON lines until the parent closes the request stream."""
    target_module = load_target_module(target_path)
    for request_line in sys.stdin:
        try:
            request = json.loads(request_line)
            if not isinstance(request, dict):
                raise ValueError("worker request is not an object")
            if worker_mode == "hook":
                response = invoke_hook(target_module, request)
            else:
                response = invoke_main(target_path, target_module, request)
        except Exception as error:
            response = {
                "worker_error": f"{type(error).__name__}: {error}",
            }
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


def main() -> int:
    """Validate fixed worker arguments and run the request protocol."""
    if len(sys.argv) != 3 or sys.argv[1] not in WORKER_MODES:
        print("usage: json_line_worker_child.py <hook|main> <target>", file=sys.stderr)
        return 2
    target_path = Path(sys.argv[2]).resolve()
    if not target_path.is_file() or target_path.suffix.casefold() != ".py":
        print("persistent worker target must be a Python file", file=sys.stderr)
        return 2
    process_requests(sys.argv[1], target_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
