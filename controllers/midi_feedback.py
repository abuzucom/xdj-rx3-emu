"""Translate firmware left Play/Pause state into serialized LED output."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from controllers.leds import LedSnapshot

LEFT_PLAY_LED = 1
LEFT_DECK = 1
MIDI_NOTE_ON = 0x90
MIDI_LED_ON = 127
FEEDBACK_WARNING_SECONDS = 5.0


@dataclass(frozen=True)
class LedMessage:
    """One left Play/Pause note message at the MIDI output boundary."""

    velocity: int

    def bytes(self) -> list[int]:
        """Return the hardware's channel-zero, note-zero LED message."""
        return [MIDI_NOTE_ON, 0, self.velocity]


class LeftPlayFeedback:
    """Serialize snapshot updates, LED writes, and shutdown with one lock."""

    def __init__(self, output) -> None:
        self._output = output
        self._snapshot: LedSnapshot | None = None
        self._sent: bool | None = None
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._warned = False

    def update(self, payload: bytes) -> None:
        """Accept only complete validated firmware snapshots."""
        snapshot = LedSnapshot.decode(payload)
        with self._lock:
            self._snapshot = snapshot

    def tick(self, now: float) -> None:
        """Refresh blink phase without repeating unchanged MIDI messages."""
        with self._lock:
            if self._output is None:
                return
            if self._snapshot is None:
                if not self._warned and now - self._started >= FEEDBACK_WARNING_SECONDS:
                    logging.warning("Firmware LED data unavailable. Check the bridge and rebuild the QEMU shim.")
                    self._warned = True
                return
            lit = self._snapshot.lit(LEFT_PLAY_LED, LEFT_DECK, now)
            if lit == self._sent:
                return
            try:
                self._output.send(LedMessage(MIDI_LED_ON if lit else 0))
                self._sent = lit
            except (OSError, RuntimeError) as exc:
                logging.warning(
                    "MIDI LED output stopped (%s). Reconnect the controller and restart.", type(exc).__name__
                )
                self._close_output()

    def _close_output(self) -> None:
        """Close an output under the caller's lock."""
        output = self._output
        self._output = None
        if output is not None:
            try:
                output.close()
            except (OSError, RuntimeError) as exc:
                logging.warning("MIDI LED port close failed (%s). Reconnect the device.", type(exc).__name__)

    def close(self) -> None:
        """Clear only the controlled LED before closing its output port."""
        with self._lock:
            if self._output is None:
                return
            try:
                self._output.send(LedMessage(0))
            except (OSError, RuntimeError) as exc:
                logging.warning("MIDI LED clear failed (%s). Reconnect the controller.", type(exc).__name__)
            finally:
                self._close_output()
