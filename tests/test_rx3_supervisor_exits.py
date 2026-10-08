"""Cover supervisor child exits and simultaneous console shutdown."""

import subprocess
import sys
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from wsl.rx3_session import supervise


class SupervisorExitTest(unittest.TestCase):
    def _run_supervisor(self, command, stdin_eof=False):
        real_popen = subprocess.Popen
        child = None

        def start_child(args, **options):
            nonlocal child
            options.pop("start_new_session", None)
            child = real_popen(args, **options)
            child.wait(timeout=10)
            return child

        with ExitStack() as stack:
            stack.enter_context(patch("wsl.rx3_session.subprocess.Popen", side_effect=start_child))
            stack.enter_context(patch("wsl.rx3_session.os.getpgid", create=True, side_effect=lambda pid: pid))
            stack.enter_context(patch("wsl.rx3_session.os.killpg", create=True))
            stack.enter_context(patch("wsl.rx3_session.signal.SIGHUP", 1, create=True))
            stack.enter_context(patch("wsl.rx3_session.signal.SIGKILL", 9, create=True))
            stack.enter_context(patch("wsl.rx3_session.signal.signal"))
            if stdin_eof:
                stack.enter_context(patch("wsl.rx3_session.select.select", return_value=([sys.stdin], [], [])))
                stack.enter_context(patch("wsl.rx3_session.os.read", return_value=b""))
            result = supervise([command], stdin_eof, 1)
        self.assertIsNotNone(child)
        return result

    def test_clean_child_exit_returns_success(self):
        command = [sys.executable, "-c", "raise SystemExit(0)"]
        self.assertEqual(self._run_supervisor(command), 0)

    def test_failed_child_exit_preserves_status(self):
        command = [sys.executable, "-c", "raise SystemExit(7)"]
        self.assertEqual(self._run_supervisor(command), 7)

    def test_console_eof_wins_when_child_has_exited(self):
        command = [sys.executable, "-c", "raise SystemExit(7)"]
        self.assertEqual(self._run_supervisor(command, stdin_eof=True), 0)


if __name__ == "__main__":
    unittest.main()
