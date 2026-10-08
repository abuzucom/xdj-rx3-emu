"""Cover the MIDI backend without requiring mido at import time."""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from controllers.backends.midi import MidiBackend
from controllers.events import ButtonOp, ControlKey
from controllers.profiles import DEFAULT_PROFILE


class _FakeMessage:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)
        self.type = kwargs.get("type", "")


class _FakePort:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = messages
        self.closed = False

    def iter_pending(self) -> list[_FakeMessage]:
        return list(self._messages)

    def close(self) -> None:
        self.closed = True


class MidiBackendTest(unittest.TestCase):
    """Cover MIDI message mapping to semantic events."""

    def _fake_mido(self, port_messages: list[_FakeMessage]) -> types.ModuleType:
        module = types.ModuleType("mido")
        module.Message = _FakeMessage

        def open_input(name: str | None = None) -> _FakePort:
            return _FakePort(port_messages)

        module.open_input = open_input
        return module

    def test_note_on_maps_to_button_press(self) -> None:
        fake = self._fake_mido([_FakeMessage(type="note_on", note=36, velocity=127, channel=0)])
        with patch("controllers.backends.midi._MIDI", fake):
            backend = MidiBackend(DEFAULT_PROFILE)
            event = backend.poll()
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.key, ControlKey.PLAY.value)
        self.assertEqual(event.op, ButtonOp.PRESS)

    def test_note_off_maps_to_button_release(self) -> None:
        fake = self._fake_mido([_FakeMessage(type="note_off", note=36, velocity=0, channel=0)])
        with patch("controllers.backends.midi._MIDI", fake):
            backend = MidiBackend(DEFAULT_PROFILE)
            event = backend.poll()
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.op, ButtonOp.RELEASE)

    def test_control_change_maps_to_fader(self) -> None:
        fake = self._fake_mido([_FakeMessage(type="control_change", control=1, value=64, channel=0)])
        with patch("controllers.backends.midi._MIDI", fake):
            backend = MidiBackend(DEFAULT_PROFILE)
            event = backend.poll()
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.key, ControlKey.TEMPO.value)
        self.assertEqual(event.value, 64)

    def test_pitchwheel_maps_to_jog(self) -> None:
        fake = self._fake_mido([_FakeMessage(type="pitchwheel", pitch=0x2010, channel=0)])
        with patch("controllers.backends.midi._MIDI", fake):
            backend = MidiBackend(DEFAULT_PROFILE)
            event = backend.poll()
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.key, ControlKey.JOG.value)
        self.assertEqual(event.delta, 16)

    def test_close_closes_midi_port(self) -> None:
        fake = self._fake_mido([])
        with patch("controllers.backends.midi._MIDI", fake):
            backend = MidiBackend(DEFAULT_PROFILE)
            port = backend._port
            backend.close()
        self.assertTrue(port.closed)


if __name__ == "__main__":
    unittest.main()
