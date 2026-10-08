"""Cover thread-safe controller bridge shutdown."""

from __future__ import annotations

import socket
import threading
import unittest

from controllers.backends.base import ControllerBackend
from controllers.bridge_client import BridgeClient
from controllers.events import ControllerEvent


class _IdleBackend(ControllerBackend):
    def __init__(self) -> None:
        self.closed = False

    def poll(self) -> ControllerEvent | None:
        return None

    def close(self) -> None:
        self.closed = True


class BridgeShutdownTest(unittest.TestCase):
    """Ensure peer closure stops the client loop and closes the backend."""

    def test_peer_eof_stops_forwarding_thread(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        peer_connected = threading.Event()

        def close_peer() -> None:
            try:
                connection, _ = listener.accept()
                peer_connected.set()
                connection.close()
            finally:
                listener.close()

        peer_thread = threading.Thread(target=close_peer)
        peer_thread.start()
        backend = _IdleBackend()
        client = BridgeClient(backend, "127.0.0.1", port)
        client_thread = threading.Thread(target=client.run)
        client_thread.start()
        self.assertTrue(peer_connected.wait(timeout=2.0))
        client_thread.join(timeout=3.0)
        peer_thread.join(timeout=2.0)

        self.assertFalse(client_thread.is_alive())
        self.assertFalse(peer_thread.is_alive())
        self.assertTrue(backend.closed)


if __name__ == "__main__":
    unittest.main()
