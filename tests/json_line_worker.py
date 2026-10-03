#!/usr/bin/env python3
"""Provide bounded JSON-line transport for persistent test workers."""
import atexit
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path


DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0
WORKER_ENTRYPOINT = Path(__file__).with_name("json_line_worker_child.py")


class JsonLineWorkerProcess:
    """Own one child process and its bounded JSON-line request protocol."""

    def __init__(
        self,
        worker_mode: str,
        target_path: Path,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if request_timeout <= 0:
            raise ValueError("worker request timeout must be positive")
        self.request_timeout = request_timeout
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(WORKER_ENTRYPOINT),
                worker_mode,
                str(target_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.response_lines: queue.Queue[str] = queue.Queue()
        self.reader_thread = threading.Thread(
            target=self._read_response_lines,
            name=f"json-worker-{self.process.pid}",
            daemon=True,
        )
        self.reader_thread.start()
        atexit.register(self._close_at_exit)

    def _read_response_lines(self) -> None:
        """Move child response lines into the bounded request queue."""
        if self.process.stdout is None:
            self.response_lines.put("")
            return
        for response_line in self.process.stdout:
            self.response_lines.put(response_line)
        self.response_lines.put("")

    def request(self, request_payload: dict) -> dict:
        """Send one JSON request and return its bounded decoded response."""
        if self.process.stdin is None:
            raise RuntimeError("JSON worker request stream is unavailable")
        try:
            request_line = json.dumps(request_payload, separators=(",", ":"))
            self.process.stdin.write(request_line + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as error:
            self.terminate()
            raise RuntimeError("JSON worker request could not be sent") from error
        try:
            response_line = self.response_lines.get(timeout=self.request_timeout)
        except queue.Empty as error:
            self.terminate()
            raise RuntimeError("JSON worker request timed out") from error
        if not response_line:
            return_code = self.process.poll()
            raise RuntimeError(f"JSON worker exited without a response: {return_code}")
        try:
            response = json.loads(response_line)
        except json.JSONDecodeError as error:
            self.terminate()
            raise RuntimeError("JSON worker returned malformed protocol data") from error
        if not isinstance(response, dict):
            self.terminate()
            raise RuntimeError("JSON worker response is not an object")
        return response

    def terminate(self) -> None:
        """Terminate the child process through a bounded shutdown sequence."""
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)

    def close(self) -> None:
        """Close the request stream and join the child process once."""
        if self.process.poll() is not None:
            return
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.terminate()

    def _close_at_exit(self) -> None:
        """Report worker cleanup failures that cannot reach a caller."""
        try:
            self.close()
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            print(f"JSON worker cleanup failed: {error}", file=sys.stderr)
