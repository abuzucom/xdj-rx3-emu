"""Decode the firmware adapter's RXL1 panel LED snapshots."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from wsl.rxl_layout import (
    LED_HEADER_SIZE,
    LED_COUNT,
    LED_DECKS,
    LED_ENTRY_SIZE,
    LED_LEVELS_SIZE,
    LED_SNAPSHOT_SIZE,
)

DEFAULT_BLINK_PERIOD_MS = 500
MILLISECONDS_PER_SECOND = 1000


@dataclass(frozen=True)
class LedSnapshot:
    """One complete immutable snapshot from the firmware panel adapter."""

    sequence: int
    data: bytes

    @classmethod
    def decode(cls, payload: bytes) -> LedSnapshot:
        """Validate the fixed RXL1 layout before exposing any LED state."""
        if len(payload) != LED_SNAPSHOT_SIZE or payload[:4] != b"RXL1":
            raise ValueError("Invalid RXL1 snapshot header or length")
        sequence, count = struct.unpack_from("<II", payload, 4)
        if count != LED_COUNT:
            raise ValueError("Unsupported RXL1 LED count")
        table_end = LED_SNAPSHOT_SIZE - LED_LEVELS_SIZE
        for offset in range(LED_HEADER_SIZE, table_end, LED_ENTRY_SIZE):
            present, state, brightness = payload[offset : offset + 3]
            if present not in (0, 1) or state not in (0, 1, 2) or brightness not in (0, 1):
                raise ValueError("Invalid RXL1 LED entry")
        return cls(sequence, bytes(payload))

    def _entry(self, led_id: int, deck: int) -> bytes:
        """Read one validated panel index and one-based deck."""
        if not 0 <= led_id < LED_COUNT or not 1 <= deck <= LED_DECKS:
            raise ValueError("LED index or deck is outside the RXL1 table")
        offset = LED_HEADER_SIZE + (led_id * LED_DECKS + deck - 1) * LED_ENTRY_SIZE
        return self.data[offset : offset + LED_ENTRY_SIZE]

    def rgb(self, led_id: int, deck: int) -> tuple[int, int, int]:
        """Return the firmware color for a panel LED."""
        entry = self._entry(led_id, deck)
        return entry[3], entry[4], entry[5]

    def lit(self, led_id: int, deck: int, now: float) -> bool:
        """Evaluate brightness and blink phase using a monotonic timestamp."""
        entry = self._entry(led_id, deck)
        if not entry[0] or entry[2] or entry[1] == 0:
            return False
        if entry[1] == 1:
            return True
        period = int.from_bytes(entry[6:8], "little") or DEFAULT_BLINK_PERIOD_MS
        return (now * MILLISECONDS_PER_SECOND) % period < period / 2
