"""Cover forwarding controller events to the emulator bridge."""

from __future__ import annotations

import socket
import struct
import threading
import time
import unittest

from controllers.backends.base import ControllerBackend
from controllers.bridge_client import BridgeClient
from controllers.events import ButtonEvent, ButtonOp, ControlKey
from scripts.smoke_test import read_frame


class _OneShotBackend(ControllerBackend):
    def __init__(self) -> None:
        self._sent = False
        self.closed = False

    def poll(self) -> ButtonEvent | None:
        if not self._sent:
            self._sent = True
            return ButtonEvent(key=ControlKey.PLAY, op=ButtonOp.PRESS, channel=0)
        return None

    def close(self) -> None:
        self.closed = True


class BridgeClientTest(unittest.TestCase):
    """Cover TCP bridge forwarding for controller events."""

    def test_client_forwards_button_event_to_bridge(self) -> None:
        received: list[tuple[int, bytes]] = []
        port_holder: list[int] = []

        def serve() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.bind(("127.0.0.1", 0))
                server.listen(1)
                port_holder.append(server.getsockname()[1])
                server.settimeout(5.0)
                conn, _ = server.accept()
                with conn:
                    conn.settimeout(2.0)
                    while True:
                        frame = read_frame(conn)
                        if frame is None:
                            break
                        received.append(frame)

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()
        deadline = time.monotonic() + 2.0
        while not port_holder and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(port_holder, "server did not start")

        backend = _OneShotBackend()
        client = BridgeClient(backend, "127.0.0.1", port_holder[0])
        client_thread = threading.Thread(target=client.run)
        client_thread.start()

        deadline = time.monotonic() + 3.0
        while len(received) < 1 and time.monotonic() < deadline:
            time.sleep(0.05)

        client.close()
        client_thread.join(timeout=2.0)

        self.assertEqual(len(received), 1)
        self.assertTrue(backend.closed)
        typ, payload = received[0]
        self.assertEqual(typ, 0x30)
        key, op, channel, value, analog = struct.unpack("<iiiif", payload)
        self.assertEqual(key, ControlKey.PLAY.value)
        self.assertEqual(op, ButtonOp.PRESS.value)
        self.assertEqual(channel, 0)


if __name__ == "__main__":
    unittest.main()
