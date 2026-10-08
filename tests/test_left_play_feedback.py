"""Exercise left Play/Pause feedback through real frame decoding."""

import struct
import unittest

from controllers.bridge_client import BridgeClient
from controllers.midi_feedback import LeftPlayFeedback
from controllers.protocol import FrameStreamDecoder
from tests.test_controller_led_snapshot import make_snapshot


class RecordingOutput:
    """Record messages at the hardware boundary."""

    def __init__(self) -> None:
        self.messages = []
        self.closed = False

    def send(self, message) -> None:
        self.messages.append(message.bytes())

    def close(self) -> None:
        self.closed = True


class FeedbackBackend:
    """Deliver bridge feedback to the real LED translator."""

    def __init__(self, feedback) -> None:
        self.feedback = feedback

    def receive_feedback(self, payload: bytes) -> None:
        self.feedback.update(payload)


class LeftPlayFeedbackTest(unittest.TestCase):
    """Keep hardware output limited to channel zero and note zero."""

    def test_fragmented_headless_frame_reaches_left_led(self) -> None:
        output = RecordingOutput()
        feedback = LeftPlayFeedback(output)
        client = BridgeClient(FeedbackBackend(feedback), "127.0.0.1", 4480)
        decoder = FrameStreamDecoder()
        payload = make_snapshot()
        frame = struct.pack("<BI", 0x17, len(payload)) + payload
        client._apply_inbound_data(decoder, frame[:19])
        self.assertEqual(output.messages, [])
        client._apply_inbound_data(decoder, frame[19:])
        feedback.tick(0.1)
        feedback.tick(0.2)
        self.assertEqual(output.messages, [[0x90, 0, 127]])
        feedback.close()
        self.assertEqual(output.messages[-1], [0x90, 0, 0])
        self.assertTrue(output.closed)

    def test_paused_blink_uses_firmware_period(self) -> None:
        output = RecordingOutput()
        feedback = LeftPlayFeedback(output)
        payload = bytearray(make_snapshot())
        payload[33] = 2
        struct.pack_into("<H", payload, 38, 400)
        feedback.update(bytes(payload))
        for now in (0.1, 0.15, 0.3, 0.35, 0.5):
            feedback.tick(now)
        self.assertEqual(output.messages, [[0x90, 0, 127], [0x90, 0, 0], [0x90, 0, 127]])

    def test_invalid_snapshot_preserves_last_valid_state(self) -> None:
        output = RecordingOutput()
        feedback = LeftPlayFeedback(output)
        feedback.update(make_snapshot())
        with self.assertRaises(ValueError):
            feedback.update(b"invalid")
        feedback.tick(0)
        self.assertEqual(output.messages, [[0x90, 0, 127]])

    def test_output_failure_disables_feedback(self) -> None:
        class FailingOutput(RecordingOutput):
            def send(self, message) -> None:
                raise OSError("Device disconnected")

        output = FailingOutput()
        feedback = LeftPlayFeedback(output)
        feedback.update(make_snapshot())
        with self.assertLogs(level="WARNING"):
            feedback.tick(0)
        feedback.tick(1)
        feedback.close()
        self.assertTrue(output.closed)

    def test_close_is_idempotent_and_prevents_output(self) -> None:
        output = RecordingOutput()
        feedback = LeftPlayFeedback(output)
        feedback.update(make_snapshot())
        feedback.close()
        feedback.close()
        feedback.tick(0)
        self.assertEqual(output.messages, [[0x90, 0, 0]])


if __name__ == "__main__":
    unittest.main()
