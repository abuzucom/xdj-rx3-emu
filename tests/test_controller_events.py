"""Cover controller event serialization."""

from __future__ import annotations

import struct
import unittest

from controllers.events import (
    ButtonEvent,
    ButtonOp,
    ControlKey,
    EncoderEvent,
    FaderEvent,
    JogEvent,
    serialize_event,
)


class EventSerializationTest(unittest.TestCase):
    """Cover bridge payload generation for semantic events."""

    def test_button_event_serializes_press(self) -> None:
        event = ButtonEvent(key=ControlKey.PLAY, op=ButtonOp.PRESS, channel=1, value=0, analog=0.0)
        payload = serialize_event(event)
        self.assertEqual(payload, struct.pack("<iiiif", 0x4101, 0, 1, 0, 0.0))

    def test_button_event_serializes_release(self) -> None:
        event = ButtonEvent(key=ControlKey.SOURCE, op=ButtonOp.RELEASE, channel=0)
        payload = serialize_event(event)
        self.assertEqual(payload, struct.pack("<iiiif", 0x201, 2, 0, 0, 0.0))

    def test_jog_event_serializes_ticks(self) -> None:
        event = JogEvent(key=ControlKey.JOG, delta=-3, channel=0)
        payload = serialize_event(event)
        self.assertEqual(payload, struct.pack("<iiiif", 0x4305, 4, 0, -3, 0.0))

    def test_fader_event_serializes_value_and_analog(self) -> None:
        event = FaderEvent(key=ControlKey.TEMPO, value=64, channel=0, analog=0.0)
        payload = serialize_event(event)
        self.assertEqual(payload, struct.pack("<iiiif", ControlKey.TEMPO, 5, 0, 64, 0.0))

    def test_encoder_event_serializes_delta(self) -> None:
        event = EncoderEvent(key=ControlKey.BROWSE, delta=2, channel=0)
        payload = serialize_event(event)
        self.assertEqual(payload, struct.pack("<iiiif", 0x420C, 4, 0, 2, 0.0))

    def test_unsupported_event_type_raises(self) -> None:
        class UnknownEvent:
            pass

        with self.assertRaises(TypeError):
            serialize_event(UnknownEvent())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
