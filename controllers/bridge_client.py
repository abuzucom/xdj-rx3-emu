"""Connect a controller backend to the XDJ-RX3 emulator bridge."""

from __future__ import annotations

import socket
import struct
import logging
import threading
import time
from typing import TYPE_CHECKING

from controllers.events import ControllerEvent, serialize_event
from controllers.protocol import FrameStreamDecoder, write_frame

if TYPE_CHECKING:
    from controllers.backends.base import ControllerBackend
    from controllers.screen import ScreenFrameBuffer


class BridgeClient:
    """Forward controller events to the emulator bridge over TCP."""

    FRAME_TYPE_KEY_COMMAND = 0x30
    CONNECT_TIMEOUT = 10.0
    CONNECT_ATTEMPT_TIMEOUT_SECONDS = 2.0
    CONNECT_RETRY_INTERVAL_SECONDS = 0.5
    POLL_INTERVAL_SECONDS = 0.02
    INBOUND_BUFFER_SIZE_BYTES = 4096
    READER_JOIN_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        backend: ControllerBackend,
        host: str,
        port: int,
        screen_buffer: ScreenFrameBuffer | None = None,
    ) -> None:
        self.backend = backend
        self.host = host
        self.port = port
        self.screen_buffer = screen_buffer
        self._sock: socket.socket | None = None
        self._running = threading.Event()
        self._closed = threading.Event()
        self._reader: threading.Thread | None = None
        self._close_lock = threading.Lock()

    def run(self) -> None:
        """Block while forwarding events until close() or an error occurs."""
        try:
            if self._closed.is_set():
                raise RuntimeError("Cannot run a closed bridge client")
            sock = self._connect()
            with self._close_lock:
                if self._closed.is_set():
                    self._close_socket(sock)
                    raise RuntimeError("Cannot run a closed bridge client")
                self._sock = sock
                self._running.set()
                self._reader = threading.Thread(target=self._drain_inbound, daemon=True)
                self._reader.start()
            while self._running.is_set():
                event = self.backend.poll()
                if event is not None:
                    self._send(event)
                else:
                    time.sleep(self.POLL_INTERVAL_SECONDS)
        finally:
            self.close()

    def close(self) -> None:
        """Close the backend and the bridge socket."""
        with self._close_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            self._running.clear()
            sock = self._sock
            self._sock = None
        try:
            self.backend.close()
        finally:
            if sock is not None:
                self._close_socket(sock)
            reader = self._reader
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=self.READER_JOIN_TIMEOUT_SECONDS)
                if reader.is_alive():
                    logging.warning("Bridge reader thread did not stop after socket shutdown")

    @staticmethod
    def _close_socket(sock: socket.socket) -> None:
        """Shutdown and close a socket to wake the reader thread."""
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError as exc:
            # The peer may have closed the socket before shutdown.
            logging.debug("Bridge socket shutdown failed during close: %s", exc)
        try:
            sock.close()
        except OSError as exc:
            # The peer may have closed the socket before close.
            logging.debug("Bridge socket close failed during cleanup: %s", exc)

    def _connect(self) -> socket.socket:
        """Connect to the bridge with a retry loop."""
        deadline = time.monotonic() + self.CONNECT_TIMEOUT
        last_error: Exception | None = None
        while True:
            if self._closed.is_set():
                raise RuntimeError("Bridge connection was cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return socket.create_connection(
                    (self.host, self.port),
                    timeout=min(self.CONNECT_ATTEMPT_TIMEOUT_SECONDS, remaining),
                )
            except OSError as exc:
                last_error = exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(self.CONNECT_RETRY_INTERVAL_SECONDS, remaining))
        raise RuntimeError(f"Could not connect to bridge at {self.host}:{self.port}") from last_error

    def _send(self, event: ControllerEvent) -> None:
        sock = self._sock
        if sock is None:
            raise RuntimeError("Bridge socket is not connected. Call run() before sending controller events.")
        payload = serialize_event(event)
        try:
            write_frame(sock, self.FRAME_TYPE_KEY_COMMAND, payload)
        except struct.error as exc:
            raise RuntimeError(f"Failed to serialize event {event}") from exc

    def _drain_inbound(self) -> None:
        """Read screen frames while keeping the bridge receive buffer drained."""
        sock = self._sock
        if sock is None:
            return
        decoder = FrameStreamDecoder() if self.screen_buffer is not None else None
        try:
            self._receive_inbound_frames(sock, decoder)
        finally:
            self._running.clear()

    def _receive_inbound_frames(
        self,
        sock: socket.socket,
        decoder: FrameStreamDecoder | None,
    ) -> None:
        while self._running.is_set():
            try:
                data = sock.recv(self.INBOUND_BUFFER_SIZE_BYTES)
            except TimeoutError:
                logging.debug("Bridge inbound frame read timed out")
                continue
            except OSError as exc:
                logging.debug("Bridge inbound socket read stopped: %s", type(exc).__name__)
                return
            if not data:
                return
            if decoder is not None:
                self._apply_inbound_data(decoder, data)

    def _apply_inbound_data(self, decoder: FrameStreamDecoder, data: bytes) -> None:
        try:
            frames = decoder.feed(data)
        except ValueError as exc:
            logging.warning("Bridge frame decoding stopped: %s", exc)
            self._running.clear()
            return
        if self.screen_buffer is None:
            return
        for message_type, payload in frames:
            try:
                self.screen_buffer.apply_frame(message_type, payload)
            except ValueError as exc:
                logging.warning("Bridge screen frame rejected: %s", exc)
