"""Replay the isolated DJ2GO2 Touch left Play/Pause capture."""

import struct
import unittest

from controllers.backends.midi import MidiBackend
from controllers.events import ControlKey, serialize_event
from controllers.profiles import ButtonMapping, DEFAULT_PROFILE, load_profile


class CapturedMessage:
    """Expose the captured MIDI message fields without opening hardware."""

    def __init__(self, kind: str, channel: int, note: int, velocity: int) -> None:
        self.type = kind
        self.channel = channel
        self.note = note
        self.velocity = velocity


class LeftPlayTest(unittest.TestCase):
    """Exercise real backend mapping, profile loading, and serialization."""

    def setUp(self) -> None:
        self.backend = object.__new__(MidiBackend)
        self.backend.profile = load_profile("dj2go2-touch.json")

    def test_captured_press_and_release_reach_deck_one(self) -> None:
        for kind, velocity, operation in (("note_on", 127, 0), ("note_off", 0, 2), ("note_on", 0, 2)):
            event = self.backend._map_message(CapturedMessage(kind, 0, 0, velocity))
            self.assertIsNotNone(event)
            self.assertEqual(serialize_event(event), struct.pack("<iiiif", 0x4101, operation, 1, 0, 0.0))

    def test_other_channels_and_buttons_do_not_trigger_play(self) -> None:
        for channel, note in ((1, 0), (4, 0), (15, 0), (0, 1), (0, 2)):
            self.assertIsNone(self.backend._map_message(CapturedMessage("note_on", channel, note, 127)))

    def test_legacy_mapping_still_accepts_any_input_channel(self) -> None:
        for channel in (0, 1, 15):
            event = DEFAULT_PROFILE.map_note(36, channel, 127)
            self.assertEqual(event.key, ControlKey.PLAY)
            self.assertEqual(event.channel, 0)

    def test_input_channel_rejects_invalid_values(self) -> None:
        for value in (-1, 16, True, "0", 0.5):
            with self.assertRaises(ValueError):
                ButtonMapping(ControlKey.PLAY, midi_channel=value)


if __name__ == "__main__":
    unittest.main()
