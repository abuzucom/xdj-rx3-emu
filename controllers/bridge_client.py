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
    from controller_client.audio import AudioSink


class BridgeConnectionError(RuntimeError):
    """Report a bridge connection failure or a cancelled connection attempt."""


class BridgeClient:
    """Forward controller events to the emulator bridge over TCP."""

    FRAME_TYPE_KEY_COMMAND = 0x30
    FRAME_TYPE_AUDIO = 0x14
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
        *,
        audio_sink: AudioSink | None = None,
    ) -> None:
        self.backend = backend
        self.host = host
        self.port = port
        self.screen_buffer = screen_buffer
        self.audio_sink = audio_sink
        self._sock: socket.socket | None = None
        self._connecting_socket: socket.socket | None = None
        self._running = threading.Event()
        self._closed = threading.Event()
        self._reader: threading.Thread | None = None
        self._close_lock = threading.Lock()

    def run(self) -> None:
        """Block while forwarding events until close() or an error occurs."""
        try:
            if self._closed.is_set():
                raise BridgeConnectionError("Bridge connection was cancelled")
            sock = self._connect()
            with self._close_lock:
                if self._closed.is_set():
                    self._close_socket(sock)
                    raise BridgeConnectionError("Bridge connection was cancelled")
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
            connecting_socket = self._connecting_socket
            self._connecting_socket = None
        try:
            if connecting_socket is not None:
                self._close_socket(connecting_socket)
            self.backend.close()
        finally:
            try:
                if sock is not None:
                    self._close_socket(sock)
            finally:
                try:
                    self._join_reader()
                finally:
                    self._close_audio_sink()

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
                raise BridgeConnectionError("Bridge connection was cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return self._connect_once(deadline)
            except BridgeConnectionError:
                raise
            except OSError as exc:
                last_error = exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                cancelled = self._closed.wait(min(self.CONNECT_RETRY_INTERVAL_SECONDS, remaining))
                if cancelled:
                    raise BridgeConnectionError("Bridge connection was cancelled") from exc
        raise BridgeConnectionError(f"Could not connect to bridge at {self.host}:{self.port}") from last_error

    def _connect_once(self, deadline: float) -> socket.socket:
        """Try each resolved address while exposing sockets to close()."""
        addresses = socket.getaddrinfo(
            self.host,
            self.port,
            type=socket.SOCK_STREAM,
        )
        last_error: OSError | None = None
        for family, socket_type, protocol, _canonical_name, address in addresses:
            if self._closed.is_set():
                raise BridgeConnectionError("Bridge connection was cancelled")
            connection = socket.socket(family, socket_type, protocol)
            with self._close_lock:
                if self._closed.is_set():
                    cancelled = True
                else:
                    self._connecting_socket = connection
                    cancelled = False
            if cancelled:
                self._close_socket(connection)
                raise BridgeConnectionError("Bridge connection was cancelled")
            connected = False
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Bridge connection deadline exceeded")
                connection.settimeout(min(self.CONNECT_ATTEMPT_TIMEOUT_SECONDS, remaining))
                connection.connect(address)
                with self._close_lock:
                    if self._closed.is_set():
                        raise BridgeConnectionError("Bridge connection was cancelled")
                    self._connecting_socket = None
                connected = True
                return connection
            except OSError as exc:
                last_error = exc
            finally:
                with self._close_lock:
                    if self._connecting_socket is connection:
                        self._connecting_socket = None
                if not connected:
                    self._close_socket(connection)
            if self._closed.is_set():
                raise BridgeConnectionError("Bridge connection was cancelled") from last_error
        if last_error is not None:
            raise last_error
        raise TimeoutError("Bridge connection has no available address")

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
        decoder = FrameStreamDecoder()
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
            # The length-prefixed protocol has no sync marker for safe recovery.
            self._running.clear()
            return
        receiver = getattr(self.backend, "receive_feedback", None)
        for message_type, payload in frames:
            if message_type == self.FRAME_TYPE_AUDIO and self.audio_sink is not None:
                try:
                    dropped_bytes = self.audio_sink.submit_frame(payload)
                except ValueError as exc:
                    logging.warning("Bridge audio frame rejected: %s", exc)
                    continue
                except OSError as exc:
                    logging.warning("Windows audio output is unavailable: %s", exc)
                    continue
                if dropped_bytes:
                    logging.warning(
                        "Windows audio buffer dropped %d stale bytes",
                        dropped_bytes,
                    )
                continue
            if message_type == 0x17 and receiver is not None:
                try:
                    receiver(payload)
                except ValueError as exc:
                    logging.warning("Bridge feedback frame rejected: %s", exc)
                continue
            if self.screen_buffer is None:
                continue
            try:
                self.screen_buffer.apply_frame(message_type, payload)
            except ValueError as exc:
                logging.warning("Bridge screen frame rejected: %s", exc)

    def _join_reader(self) -> None:
        reader = self._reader
        if reader is None or reader is threading.current_thread():
            return
        reader.join(timeout=self.READER_JOIN_TIMEOUT_SECONDS)
        if reader.is_alive():
            logging.warning("Bridge reader thread did not stop after socket shutdown")

    def _close_audio_sink(self) -> None:
        if self.audio_sink is None:
            return
        try:
            self.audio_sink.close()
        except OSError as exc:
            logging.warning("Windows audio sink cleanup failed: %s", exc)
