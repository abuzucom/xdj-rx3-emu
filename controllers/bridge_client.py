"""Connect a controller backend to the XDJ-RX3 emulator bridge."""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import TYPE_CHECKING

from controllers.events import ControllerEvent, serialize_event
from scripts.smoke_test import write_frame

if TYPE_CHECKING:
    from controllers.backends.base import ControllerBackend


class BridgeClient:
    """Forward controller events to the emulator bridge over TCP."""

    FRAME_TYPE_KEY_COMMAND = 0x30
    CONNECT_TIMEOUT = 10.0

    def __init__(self, backend: ControllerBackend, host: str, port: int) -> None:
        self.backend = backend
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._running = False
        self._reader: threading.Thread | None = None

    def run(self) -> None:
        """Run the forwarding loop until the backend is exhausted or an error occurs."""
        self._sock = self._connect()
        self._running = True
        self._reader = threading.Thread(target=self._drain_inbound, daemon=True)
        self._reader.start()
        try:
            while self._running:
                event = self.backend.poll()
                if event is not None:
                    self._send(event)
        finally:
            self._running = False
            self.close()

    def close(self) -> None:
        """Close the backend and the bridge socket."""
        self._running = False
        self.backend.close()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _connect(self) -> socket.socket:
        """Connect to the bridge with a retry loop."""
        deadline = time.monotonic() + self.CONNECT_TIMEOUT
        last_error: Exception | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return socket.create_connection((self.host, self.port), timeout=min(2.0, remaining))
            except ConnectionRefusedError as exc:
                last_error = exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.5, remaining))
            except OSError as exc:
                last_error = exc
                break
        raise RuntimeError(f"Could not connect to bridge at {self.host}:{self.port}") from last_error

    def _send(self, event: ControllerEvent) -> None:
        if self._sock is None:
            return
        payload = serialize_event(event)
        try:
            write_frame(self._sock, self.FRAME_TYPE_KEY_COMMAND, payload)
        except struct.error as exc:
            raise RuntimeError(f"Failed to serialize event {event}") from exc

    def _drain_inbound(self) -> None:
        """Discard inbound bridge frames so the TCP receive buffer does not stall."""
        if self._sock is None:
            return
        try:
            while self._running:
                try:
                    data = self._sock.recv(4096)
                    if not data:
                        break
                except TimeoutError:
                    continue
                except OSError:
                    break
        finally:
            self._running = False
