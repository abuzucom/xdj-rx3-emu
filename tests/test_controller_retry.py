"""Cover bridge connection retry behavior."""

from __future__ import annotations

import socket
import unittest
from unittest.mock import MagicMock, patch

from controllers.backends.base import ControllerBackend
from controllers.bridge_client import BridgeClient
from controllers.events import ControllerEvent


class _IdleBackend(ControllerBackend):
    def poll(self) -> ControllerEvent | None:
        return None

    def close(self) -> None:
        pass


class BridgeRetryTest(unittest.TestCase):
    """Ensure transient socket errors do not end the retry window early."""

    def test_retries_socket_error_before_connecting(self) -> None:
        client = BridgeClient(_IdleBackend(), "127.0.0.1", 4480)
        connected_socket = MagicMock(spec=socket.socket)
        with (
            patch(
                "controllers.bridge_client.socket.create_connection",
                side_effect=[OSError("network unavailable"), connected_socket],
            ) as create_connection,
            patch("controllers.bridge_client.time.sleep") as sleep,
        ):
            result = client._connect()

        self.assertIs(result, connected_socket)
        self.assertEqual(create_connection.call_count, 2)
        sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
