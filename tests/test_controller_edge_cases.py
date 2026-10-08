"""Cover controller input direction and error reporting edges."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from controllers.backends.base import ControllerBackend
from controllers.backends.midi import MidiBackend
from controllers.bridge_client import BridgeClient
from controllers.events import ButtonEvent, ButtonOp, ControlKey
from controllers.profiles import DEFAULT_PROFILE, load_profile


class ControllerEdgeCaseTest(unittest.TestCase):
    """Verify relative encoder direction and clear backend failures."""

    def test_encoder_counterclockwise_values_are_negative(self) -> None:
        one_tick = DEFAULT_PROFILE.map_cc(2, value=63, midi_channel=0)
        two_ticks = DEFAULT_PROFILE.map_cc(2, value=62, midi_channel=0)
        self.assertIsNotNone(one_tick)
        self.assertIsNotNone(two_ticks)
        assert one_tick is not None and two_ticks is not None
        self.assertEqual(one_tick.delta, -1)
        self.assertEqual(two_ticks.delta, -2)

    def test_encoder_profile_rejects_analog_scale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profile.json"
            data = {"encoders": {"2": {"key": "BROWSE", "analog_scale": ["bad"]}}}
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not support analog_scale"):
                load_profile(path)

    def test_missing_midi_port_has_recovery_message(self) -> None:
        midi = Mock()
        midi.open_input.side_effect = OSError("unknown port")
        with patch("controllers.backends.midi._MIDI", midi):
            with self.assertRaisesRegex(RuntimeError, "Verify its name and device connection"):
                MidiBackend(DEFAULT_PROFILE, port_name="Missing MIDI device")

    def test_send_without_connection_raises_instead_of_dropping(self) -> None:
        backend = Mock(spec=ControllerBackend)
        client = BridgeClient(backend, "127.0.0.1", 4480)
        event = ButtonEvent(key=ControlKey.PLAY, op=ButtonOp.PRESS)
        with self.assertRaisesRegex(RuntimeError, "Bridge socket is not connected"):
            client._send(event)


if __name__ == "__main__":
    unittest.main()
