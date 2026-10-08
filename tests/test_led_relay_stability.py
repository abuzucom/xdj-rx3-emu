"""Reject a snapshot changed between the relay's two file reads."""

import tempfile
import unittest
from pathlib import Path

from tests.test_controller_led_snapshot import make_snapshot
from tests.test_led_relay import RecordingClient
from wsl.led_relay import LedRelay


class ChangingFile:
    """Change real file bytes when the reader rewinds."""

    def __init__(self, path, changed):
        self.path = path
        self.changed = changed

    def open(self, mode, **options):
        self.stream = self.path.open(mode, **options)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()

    def read(self, size):
        return self.stream.read(size)

    def seek(self, offset):
        self.path.write_bytes(self.changed)
        return self.stream.seek(offset)


class LedRelayStabilityTest(unittest.TestCase):
    def test_changed_file_does_not_send_cached_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "leds"
            path.write_bytes(make_snapshot())
            changed = bytearray(make_snapshot())
            changed[4] += 1
            client = RecordingClient()
            relay = LedRelay(path, client)
            relay.path = ChangingFile(path, bytes(changed))
            relay.poll()
            self.assertEqual(client.frames, [])
