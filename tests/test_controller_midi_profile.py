"""Cover controller profile loading and default mapping."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from controllers.events import ButtonOp, ControlKey
from controllers.profiles import DEFAULT_PROFILE, load_profile


class ProfileTest(unittest.TestCase):
    """Cover mapping from MIDI messages to semantic events."""

    def test_default_profile_maps_play_note(self) -> None:
        event = DEFAULT_PROFILE.map_note(36, midi_channel=0, velocity=127)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.key, ControlKey.PLAY.value)
        self.assertEqual(event.op, ButtonOp.PRESS)

    def test_default_profile_releases_button_on_zero_velocity(self) -> None:
        event = DEFAULT_PROFILE.map_note(36, midi_channel=0, velocity=0)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.op, ButtonOp.RELEASE)

    def test_default_profile_maps_tempo_cc(self) -> None:
        event = DEFAULT_PROFILE.map_cc(1, value=0, midi_channel=0)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.key, ControlKey.TEMPO.value)
        self.assertAlmostEqual(event.analog, -1.0)

    def test_default_profile_maps_browse_encoder(self) -> None:
        event = DEFAULT_PROFILE.map_cc(2, value=70, midi_channel=0)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.key, ControlKey.BROWSE.value)
        self.assertEqual(event.delta, 6)

    def test_default_profile_maps_pitch_bend_to_jog(self) -> None:
        event = DEFAULT_PROFILE.map_pitch_bend(0x2005, midi_channel=0)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.key, ControlKey.JOG.value)
        self.assertEqual(event.delta, 5)

    def test_load_default_json_profile(self) -> None:
        profile = load_profile(Path("controllers/profiles/default.json"))
        self.assertEqual(profile.name, "default")
        event = profile.map_note(37, midi_channel=0, velocity=127)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.key, ControlKey.LOAD.value)

    def test_load_custom_profile_rejects_unknown_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text('{"buttons": {"1": {"key": "BOGUS"}}}')
            with self.assertRaises(ValueError):
                load_profile(path)


if __name__ == "__main__":
    unittest.main()
