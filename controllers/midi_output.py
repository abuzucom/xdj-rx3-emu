"""Open the dedicated DJ2GO2 Touch LED output without changing input routing."""

import logging

from controllers.midi_feedback import LeftPlayFeedback

DEVICE_NAME = "DJ2GO2 Touch"


class MidiLedPort:
    """Convert LED byte messages at the optional mido dependency boundary."""

    def __init__(self, midi, port) -> None:
        self.midi = midi
        self.port = port

    def send(self, message) -> None:
        """Write one complete LED message."""
        self.port.send(self.midi.Message.from_bytes(message.bytes()))

    def close(self) -> None:
        """Release the physical MIDI output."""
        self.port.close()


def open_left_play_feedback(midi, name: str | None) -> LeftPlayFeedback | None:
    """Require one matching output unless an explicit port name is supplied."""
    try:
        if name is None:
            names = [entry for entry in midi.get_output_names() if DEVICE_NAME in entry]
            if len(names) != 1:
                logging.warning("LED output requires one DJ2GO2 Touch port. Select --midi-output explicitly.")
                return None
            name = names[0]
        return LeftPlayFeedback(MidiLedPort(midi, midi.open_output(name)))
    except (ImportError, OSError, RuntimeError) as exc:
        logging.warning("LED output unavailable (%s). Check --midi-output and device connection.", type(exc).__name__)
        return None
