"""Trace hook code in every interpreter started by the test suite.

Python imports `sitecustomize` at process startup. The import reaches every
subprocess launched by the suite. Gates run as subprocesses. Ordinary
in-process coverage therefore misses most gate decision code.

Both environment variables activate the tracer. Other uses of this PYTHONPATH
entry incur no instrumentation cost.
"""
import atexit
import json
import os
import sys
import threading
import uuid

TARGET_DIR = os.environ.get("HOOK_COVERAGE_TARGET", "")
OUT_DIR = os.environ.get("HOOK_COVERAGE_OUT", "")

if TARGET_DIR and OUT_DIR:
    TARGET_DIR = os.path.normcase(os.path.abspath(TARGET_DIR))
    _hits = set()
    _path_cache = {}
    _target_codes = set()
    _monitoring = getattr(sys, "monitoring", None)
    _monitor_tool = None

    def _target_path(filename: str) -> str:
        """Return an absolute target path or an empty string."""
        cached = _path_cache.get(filename)
        if cached is not None:
            return cached
        absolute = os.path.abspath(filename)
        normalized = os.path.normcase(absolute)
        try:
            inside_target = os.path.commonpath((TARGET_DIR, normalized)) == TARGET_DIR
        except ValueError:
            inside_target = False
        result = absolute if inside_target else ""
        _path_cache[filename] = result
        return result

    def _trace_target(frame, event, _argument):
        """Trace lines only after a frame enters the hook target."""
        path = _target_path(frame.f_code.co_filename)
        if not path:
            return None
        if event == "line":
            _hits.add((path, frame.f_lineno))
        return _trace_target

    def _monitor_start(code, _instruction_offset):
        """Enable local line events when monitored code enters the target."""
        path = _target_path(code.co_filename)
        if path:
            _target_codes.add(code)
            _monitoring.set_local_events(
                _monitor_tool, code, _monitoring.events.LINE)
        return _monitoring.DISABLE

    def _monitor_line(code, line):
        """Record one target line from the low-overhead monitoring API."""
        path = _target_path(code.co_filename)
        if path:
            _hits.add((path, line))

    def _install_monitoring() -> bool:
        """Install local monitoring when this Python provides the API."""
        global _monitor_tool
        if _monitoring is None:
            return False
        tool_id = _monitoring.COVERAGE_ID
        try:
            _monitoring.use_tool_id(tool_id, "hook-coverage")
        except ValueError:
            return False
        _monitor_tool = tool_id
        _monitoring.register_callback(
            tool_id, _monitoring.events.PY_START, _monitor_start)
        _monitoring.register_callback(
            tool_id, _monitoring.events.LINE, _monitor_line)
        _monitoring.set_events(tool_id, _monitoring.events.PY_START)
        return True

    def _dump() -> None:
        """Write this process's counts for the target directory."""
        # Stop tracing before copying shared line records.
        if _monitor_tool is not None:
            _monitoring.set_events(_monitor_tool, 0)
            for code in _target_codes:
                _monitoring.set_local_events(_monitor_tool, code, 0)
            _monitoring.free_tool_id(_monitor_tool)
        else:
            sys.settrace(None)
            threading.settrace(None)
        counts = set(_hits)
        if not counts:
            return
        path = os.path.join(OUT_DIR, "%s.json" % uuid.uuid4().hex)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(sorted(counts), handle)

    atexit.register(_dump)
    if not _install_monitoring():
        sys.settrace(_trace_target)
        threading.settrace(_trace_target)
