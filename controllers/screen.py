"""Validate and assemble screen frames from the emulator bridge."""

from __future__ import annotations

import struct
import threading
from dataclasses import dataclass

SCREEN_INFO_FRAME_TYPE = 0x10
SCREEN_TILE_FRAME_TYPE = 0x11
SCREEN_INFO_PAYLOAD_SIZE = 4
SCREEN_TILE_HEADER_SIZE = 8
RGBA_BYTES_PER_PIXEL = 4
MAX_SCREEN_WIDTH = 1280
MAX_SCREEN_HEIGHT = 800


@dataclass(frozen=True)
class ScreenTile:
    """One validated rectangular RGBA screen update."""

    x: int
    y: int
    width: int
    height: int
    rgba: bytes


class ScreenFrameBuffer:
    """Store the latest screen dimensions and coalesced tile updates."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._width = 0
        self._height = 0
        self._pixels = bytearray()
        self._pending_tiles: dict[tuple[int, int], ScreenTile] = {}
        self._reset_pending = False

    def apply_frame(self, message_type: int, payload: bytes) -> bool:
        """Apply a screen frame and report whether the frame type was handled."""
        if message_type == SCREEN_INFO_FRAME_TYPE:
            self._apply_screen_info(payload)
            return True
        if message_type == SCREEN_TILE_FRAME_TYPE:
            self._apply_screen_tile(payload)
            return True
        return False

    def take_updates(self) -> tuple[int, int, bool, tuple[ScreenTile, ...]]:
        """Return pending screen changes and clear the pending update set."""
        with self._lock:
            updates = tuple(self._pending_tiles.values())
            result = self._width, self._height, self._reset_pending, updates
            self._pending_tiles.clear()
            self._reset_pending = False
            return result

    def snapshot(self) -> tuple[int, int, bytes]:
        """Return a consistent copy of the assembled RGBA framebuffer."""
        with self._lock:
            return self._width, self._height, bytes(self._pixels)

    def _apply_screen_info(self, payload: bytes) -> None:
        if len(payload) != SCREEN_INFO_PAYLOAD_SIZE:
            raise ValueError("Screen info frame has an invalid payload size")
        width, height = struct.unpack("<HH", payload)
        if not 0 < width <= MAX_SCREEN_WIDTH:
            raise ValueError("Screen width is outside the supported range")
        if not 0 < height <= MAX_SCREEN_HEIGHT:
            raise ValueError("Screen height is outside the supported range")
        pixels = bytearray(width * height * RGBA_BYTES_PER_PIXEL)
        with self._lock:
            self._width = width
            self._height = height
            self._pixels = pixels
            self._pending_tiles.clear()
            self._reset_pending = True

    def _apply_screen_tile(self, payload: bytes) -> None:
        if len(payload) < SCREEN_TILE_HEADER_SIZE:
            raise ValueError("Screen tile frame is missing its header")
        x, y, width, height = struct.unpack_from("<HHHH", payload)
        if width == 0 or height == 0:
            raise ValueError("Screen tile dimensions must be positive")
        expected_size = SCREEN_TILE_HEADER_SIZE + width * height * RGBA_BYTES_PER_PIXEL
        if len(payload) != expected_size:
            raise ValueError("Screen tile frame has an invalid payload size")
        rgba = payload[SCREEN_TILE_HEADER_SIZE:]
        tile = ScreenTile(x, y, width, height, rgba)
        with self._lock:
            self._validate_tile_bounds(x, y, width, height)
            self._copy_tile_pixels(tile)
            self._pending_tiles[x, y] = tile

    def _validate_tile_bounds(self, x: int, y: int, width: int, height: int) -> None:
        if self._width == 0 or self._height == 0:
            raise ValueError("Screen info must arrive before screen tiles")
        if x + width > self._width or y + height > self._height:
            raise ValueError("Screen tile exceeds the current screen dimensions")

    def _copy_tile_pixels(self, tile: ScreenTile) -> None:
        row_size = tile.width * RGBA_BYTES_PER_PIXEL
        for row in range(tile.height):
            source_start = row * row_size
            target_start = ((tile.y + row) * self._width + tile.x) * RGBA_BYTES_PER_PIXEL
            self._pixels[target_start : target_start + row_size] = tile.rgba[source_start : source_start + row_size]
