#!/usr/bin/env python3
"""Run a Python CLI entrypoint repeatedly with isolated request state."""
import json
import subprocess
import sys
from pathlib import Path

try:
    from tests.json_line_worker import (
        DEFAULT_REQUEST_TIMEOUT_SECONDS,
        JsonLineWorkerProcess,
    )
except ImportError:
    from json_line_worker import DEFAULT_REQUEST_TIMEOUT_SECONDS, JsonLineWorkerProcess


class MainWorker:
    """Keep one imported CLI module alive across isolated invocations."""

    def __init__(self, module_path: Path,
                 request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
                 change_directory: bool = True):
        self.module_path = Path(module_path)
        self.change_directory = change_directory
        self.transport = JsonLineWorkerProcess(
            "main",
            self.module_path,
            request_timeout=request_timeout,
        )
        self.process = self.transport.process

    def invoke(self, arguments: list[str], payload, cwd: Path,
               environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        """Run one isolated request and return a completed-process result."""
        request = {
            "arguments": list(arguments),
            "change_directory": self.change_directory,
            "cwd": str(cwd),
            "environment": dict(environment),
            "stdin": "" if payload is None else json.dumps(payload),
        }
        response = self.transport.request(request)
        if "worker_error" in response:
            raise RuntimeError(response["worker_error"])
        command = [sys.executable, str(self.module_path), *arguments]
        return subprocess.CompletedProcess(
            command, response["code"], response["stdout"], response["stderr"])

    def close(self) -> None:
        """Close the request stream and join the worker process."""
        self.transport.close()
