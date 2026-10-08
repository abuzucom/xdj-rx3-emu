"""Shared TCP frame helpers for emulator bridge clients."""

from __future__ import annotations

import logging
import socket
import struct
import time

MAX_FRAME_LENGTH = 1_048_576
FRAME_HEADER_SIZE = 5
DEFAULT_READ_TIMEOUT_SECONDS = 5.0
DEFAULT_BRIDGE_PORT = 4480
FRAME_HEADER_FORMAT = "<BI"


def write_frame(sock: socket.socket, msg_type: int, payload: bytes) -> None:
    """Send a frame using the bridge wire layout."""
    header = struct.pack(FRAME_HEADER_FORMAT, msg_type, len(payload))
    sock.sendall(header + payload)


class FrameStreamDecoder:
    """Decode bridge frames from arbitrarily segmented socket data."""

    def __init__(self, max_frame_length: int = MAX_FRAME_LENGTH) -> None:
        if max_frame_length < 0:
            raise ValueError("Maximum frame length cannot be negative")
        self._max_frame_length = max_frame_length
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Return complete frames and retain any incomplete trailing bytes."""
        self._buffer.extend(data)
        frames = []
        while len(self._buffer) >= FRAME_HEADER_SIZE:
            message_type, payload_length = struct.unpack_from(
                FRAME_HEADER_FORMAT,
                self._buffer,
            )
            if payload_length > self._max_frame_length:
                self._buffer.clear()
                raise ValueError("Bridge frame exceeds the configured length limit")
            frame_length = FRAME_HEADER_SIZE + payload_length
            if len(self._buffer) < frame_length:
                break
            payload = bytes(self._buffer[FRAME_HEADER_SIZE:frame_length])
            del self._buffer[:frame_length]
            frames.append((message_type, payload))
        return frames


def _apply_read_deadline(sock: socket.socket, deadline: float | None) -> None:
    """Constrain the next socket read to the remaining deadline budget."""
    if deadline is None:
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("read_frame deadline exceeded")
    sock.settimeout(remaining)


def read_frame(
    sock: socket.socket,
    max_length: int = MAX_FRAME_LENGTH,
    timeout: float | None = DEFAULT_READ_TIMEOUT_SECONDS,
) -> tuple[int, bytes] | None:
    """Read one frame, returning None for EOF or an oversized frame."""
    deadline = time.monotonic() + timeout if timeout is not None else None
    header = bytearray()
    while len(header) < FRAME_HEADER_SIZE:
        _apply_read_deadline(sock, deadline)
        chunk = sock.recv(FRAME_HEADER_SIZE - len(header))
        if not chunk:
            return None
        header.extend(chunk)

    message_type, length = struct.unpack("<BI", header)
    if length > max_length:
        logging.warning("Frame length %d exceeds maximum allowed %d", length, max_length)
        return None

    payload = bytearray()
    while len(payload) < length:
        _apply_read_deadline(sock, deadline)
        chunk = sock.recv(length - len(payload))
        if not chunk:
            return None
        payload.extend(chunk)
    return message_type, bytes(payload)
