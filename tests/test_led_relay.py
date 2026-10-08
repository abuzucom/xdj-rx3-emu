"""Exercise the standalone WSL LED relay with real snapshot files."""

import tempfile
import threading
import unittest
from pathlib import Path

from tests.test_controller_led_snapshot import make_snapshot
from wsl.led_relay import LedRelay


class RecordingClient:
    """Record protocol frames at the socket boundary."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sock = object()
        self.frames = []

    def send(self, kind, payload) -> bool:
        self.frames.append((kind, payload))
        return True


class LedRelayTest(unittest.TestCase):
    def test_initial_changed_and_reconnected_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "leds"
            client = RecordingClient()
            relay = LedRelay(path, client)
            relay.poll()
            self.assertEqual(client.frames, [])
            path.write_bytes(make_snapshot())
            relay.poll()
            relay.poll()
            self.assertEqual(client.frames, [(0x17, make_snapshot())])
            client.sock = object()
            relay.poll()
            self.assertEqual(len(client.frames), 2)
            changed = bytearray(make_snapshot())
            changed[4] = 8
            path.write_bytes(changed)
            relay.poll()
            self.assertEqual(client.frames[-1], (0x17, bytes(changed)))

    def test_invalid_file_never_emits_frame(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "leds"
            client = RecordingClient()
            relay = LedRelay(path, client)
            for data in (b"bad", make_snapshot()[:-1], make_snapshot() + b"x"):
                path.write_bytes(data)
                relay.poll()
            self.assertEqual(client.frames, [])


if __name__ == "__main__":
    unittest.main()
