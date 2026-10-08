"""Cover bridge connection retry behavior."""

from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from controllers.backends.base import ControllerBackend
from controllers.bridge_client import BridgeClient
from controllers.events import ControllerEvent


class _ScriptedSocket:
    def __init__(self, connect_error: OSError | None = None) -> None:
        self.connect_error = connect_error
        self.closed = False

    def settimeout(self, _timeout: float) -> None:
        return None

    def connect(self, _address: tuple[str, int]) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    def shutdown(self, _how: int) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


class _IdleBackend(ControllerBackend):
    def __init__(self) -> None:
        self.closed = False

    def poll(self) -> ControllerEvent | None:
        return None

    def close(self) -> None:
        self.closed = True


class BridgeRetryTest(unittest.TestCase):
    """Ensure transient socket errors do not end the retry window early."""

    def test_close_closes_backend(self) -> None:
        backend = _IdleBackend()
        client = BridgeClient(backend, "127.0.0.1", 4480)

        client.close()

        self.assertTrue(backend.closed)

    def test_retries_socket_error_before_connecting(self) -> None:
        client = BridgeClient(_IdleBackend(), "127.0.0.1", 4480)
        first_socket = _ScriptedSocket(OSError("network unavailable"))
        connected_socket = _ScriptedSocket()
        address_info = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 4480),
            )
        ]
        with (
            patch(
                "controllers.bridge_client.socket.getaddrinfo",
                return_value=address_info,
            ) as getaddrinfo,
            patch(
                "controllers.bridge_client.socket.socket",
                side_effect=[first_socket, connected_socket],
            ) as socket_factory,
            patch.object(client, "CONNECT_RETRY_INTERVAL_SECONDS", 0),
        ):
            result = client._connect()

        self.assertIs(result, connected_socket)
        self.assertEqual(getaddrinfo.call_count, 2)
        self.assertEqual(socket_factory.call_count, 2)
        self.assertTrue(first_socket.closed)


if __name__ == "__main__":
    unittest.main()
