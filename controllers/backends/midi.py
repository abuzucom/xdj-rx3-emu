"""MIDI controller backend using mido.

mido and a real-time backend such as python-rtmidi must be installed to use
this module. They are optional runtime dependencies; the controller client will
raise a clear error when they are missing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import time

from controllers.events import ControllerEvent

from .base import ControllerBackend

try:
    import mido

    _MIDI = mido
    _MIDI_ERROR: BaseException | None = None
except ImportError as exc:  # pragma: no cover; import failure path
    _MIDI = None
    _MIDI_ERROR = exc

if TYPE_CHECKING:
    from mido import Message
    from mido.ports import BaseInput

    from controllers.profiles import Profile


class MidiBackend(ControllerBackend):
    """Translate MIDI messages from a hardware or virtual input into semantic events."""

    def __init__(
        self,
        profile: Profile,
        port_name: str | None = None,
        output_name: str | None = None,
        led_feedback: bool = True,
    ) -> None:
        if _MIDI is None:
            raise RuntimeError(
                "MIDI support requires mido and a real-time backend such as python-rtmidi. "
                "Install them with: pip install -r requirements-controllers.txt"
            ) from _MIDI_ERROR
        self.profile = profile
        self._feedback = None
        self._port: BaseInput | None
        try:
            if port_name:
                self._port = _MIDI.open_input(port_name)
            else:
                self._port = _MIDI.open_input()
        except (ImportError, OSError, RuntimeError) as exc:
            if port_name:
                raise RuntimeError(
                    f"Could not open MIDI input port {port_name!r}. Verify its name and device connection."
                ) from exc
            raise RuntimeError("Could not open a MIDI input port. Connect a device or pass --midi-port.") from exc
        if led_feedback and profile.name == "DJ2GO2 Touch":
            from controllers.midi_output import open_left_play_feedback

            self._feedback = open_left_play_feedback(_MIDI, output_name)

    def poll(self) -> ControllerEvent | None:
        """Return the next MIDI event mapped to a semantic event."""
        if self._port is None:
            return None
        if self._feedback is not None:
            self._feedback.tick(time.monotonic())
        for message in self._port.iter_pending():
            event = self._map_message(message)
            if event is not None:
                return event
        return None

    def close(self) -> None:
        """Close the MIDI input port."""
        try:
            if self._feedback is not None:
                self._feedback.close()
        finally:
            if self._port is not None:
                self._port.close()
                self._port = None

    def receive_feedback(self, payload: bytes) -> None:
        """Accept a firmware snapshot from the bridge reader thread."""
        if self._feedback is not None:
            self._feedback.update(payload)

    def _map_message(self, message: Message) -> ControllerEvent | None:
        """Map a single mido Message to a semantic event."""
        kind = message.type
        if kind in ("note_on", "note_off"):
            velocity = message.velocity if kind == "note_on" else 0
            return self.profile.map_note(message.note, message.channel, velocity)
        if kind == "control_change":
            return self.profile.map_cc(message.control, message.value, message.channel)
        if kind == "pitchwheel":
            return self.profile.map_pitch_bend(message.pitch, message.channel)
        return None
