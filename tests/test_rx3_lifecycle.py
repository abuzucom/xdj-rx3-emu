"""Cover stale-session detection and supervised child cleanup."""

import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class LifecycleTest(unittest.TestCase):
    def test_guard_reports_matching_bridge_pid(self):
        from wsl.rx3_guard import find_processes

        with tempfile.TemporaryDirectory() as folder:
            process = Path(folder) / "123"
            process.mkdir()
            (process / "cmdline").write_bytes(b"python3\0/tmp/rx3_bridge.py\0--port\04480\0")
            (process / "comm").write_text("python3\n", encoding="utf-8")
            self.assertEqual(find_processes(Path(folder)), [123])

    def test_guard_reports_busy_port(self):
        from wsl.rx3_guard import check_idle

        with tempfile.TemporaryDirectory() as folder, socket.socket() as listener:
            listener.bind(("", 0))
            listener.listen()
            with self.assertRaisesRegex(RuntimeError, "port"):
                check_idle(listener.getsockname()[1], Path(folder))

    def test_pipe_close_cleans_owned_child(self):
        script = Path(__file__).resolve().parents[1] / "wsl" / "rx3_session.py"
        if os.name == "nt":
            translated = subprocess.run(
                ["wsl", "wslpath", "-a", script.as_posix()],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            command = ["wsl", "python3", translated, "--probe"]
        else:
            command = [sys.executable, str(script), "--probe"]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(process.stdout.readline().strip(), b"SESSION READY")
            process.stdin.close()
            process.wait(timeout=15)
            self.assertEqual(process.returncode, 0, process.stderr.read().decode())
            self.assertIn(b"OWNED CHILDREN REAPED", process.stdout.read())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()
