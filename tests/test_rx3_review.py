"""Cover review findings for port bounds and process group ownership."""

import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from wsl.rx3_guard import validate_port
from wsl.rx3_session import PROBE_TIMEOUT_SECONDS, supervise


class Rx3ReviewTest(unittest.TestCase):
    def test_port_accepts_user_range(self):
        self.assertEqual(validate_port(1024), 1024)
        self.assertEqual(validate_port(65535), 65535)

    def test_port_rejects_out_of_range_and_non_integer_values(self):
        for port in (1023, 65536, True, "4480"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                validate_port(port)

    def test_probe_timeout_is_named_and_bounded(self):
        self.assertEqual(PROBE_TIMEOUT_SECONDS, 30.0)

    def test_supervisor_rejects_child_without_owned_group(self):
        real_popen = subprocess.Popen
        child = None

        def start_without_new_group(command, **kwargs):
            nonlocal child
            kwargs["start_new_session"] = False
            child = real_popen(command, **kwargs)
            return child

        with patch("wsl.rx3_session.subprocess.Popen", side_effect=start_without_new_group):
            with patch("wsl.rx3_session.os.getpgid", create=True, return_value=-1):
                with patch.object(signal, "SIGHUP", 1, create=True):
                    with patch("wsl.rx3_session.signal.signal"):
                        with self.assertRaisesRegex(RuntimeError, "process group"):
                            supervise([[sys.executable, "-c", "import time; time.sleep(30)"]], False, 1)
        self.assertIsNotNone(child)
        self.assertIsNotNone(child.poll())


if __name__ == "__main__":
    unittest.main()
