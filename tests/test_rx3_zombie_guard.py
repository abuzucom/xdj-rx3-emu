"""Ensure zombie RX3 processes remain visible when proc cmdline is empty."""

import tempfile
import unittest
from pathlib import Path

from wsl.rx3_guard import find_processes


class ZombieGuardTest(unittest.TestCase):
    def test_process_comm_identifies_zombie_with_empty_cmdline(self):
        with tempfile.TemporaryDirectory() as folder:
            process = Path(folder) / "4242"
            process.mkdir()
            (process / "cmdline").write_bytes(b"")
            (process / "comm").write_text("rbp-pi\n", encoding="utf-8")
            self.assertEqual(find_processes(Path(folder)), [4242])


if __name__ == "__main__":
    unittest.main()
