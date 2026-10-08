"""Cover headless frame limits and the shared RXL1 wire layout."""

import struct
import unittest

from controllers.bridge_client import BridgeClient
from controllers.protocol import FrameStreamDecoder, MAX_FRAME_LENGTH


class LedReviewBoundsTest(unittest.TestCase):
    def test_headless_oversized_header_stops_connection_loop(self):
        client = BridgeClient(object(), "127.0.0.1", 4480)
        client._running.set()
        decoder = FrameStreamDecoder()
        header = struct.pack("<BI", 0x17, 0xFFFFFFFF)
        client._apply_inbound_data(decoder, header[:4])
        self.assertTrue(client._running.is_set())
        with self.assertLogs(level="WARNING"):
            client._apply_inbound_data(decoder, header[4:])
        self.assertFalse(client._running.is_set())
        self.assertEqual(len(decoder._buffer), 0)

    def test_limit_accepts_normal_screen_payloads(self):
        payload = bytes(4096)
        decoder = FrameStreamDecoder()
        self.assertEqual(decoder.feed(struct.pack("<BI", 0x11, len(payload)) + payload), [(0x11, payload)])
        with self.assertRaises(ValueError):
            decoder.feed(struct.pack("<BI", 0x11, MAX_FRAME_LENGTH + 1))

    def test_shared_layout_matches_decoder_and_relay(self):
        from controllers import leds
        from wsl import led_relay, rxl_layout

        self.assertEqual(rxl_layout.LED_SNAPSHOT_SIZE, 1048)
        self.assertEqual(leds.LED_SNAPSHOT_SIZE, rxl_layout.LED_SNAPSHOT_SIZE)
        self.assertEqual(led_relay.SNAPSHOT_SIZE, rxl_layout.LED_SNAPSHOT_SIZE)
        self.assertEqual(leds.LED_COUNT, led_relay.LED_COUNT)
