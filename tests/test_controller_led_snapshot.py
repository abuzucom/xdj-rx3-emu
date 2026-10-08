"""Exercise firmware LED snapshot validation and timing."""

import struct
import unittest

from controllers.leds import LedSnapshot


def make_snapshot() -> bytes:
    """Build the adapter's complete RXL1 frame with distinct deck states."""
    payload = bytearray(16 + 64 * 16 + 8)
    struct.pack_into("<4sII", payload, 0, b"RXL1", 7, 64)
    struct.pack_into("<6BH", payload, 32, 1, 1, 0, 255, 0, 0, 0)
    struct.pack_into("<6BH", payload, 40, 1, 2, 0, 0, 255, 0, 400)
    struct.pack_into("<ii", payload, len(payload) - 8, -12, -8)
    return bytes(payload)


class LedSnapshotTest(unittest.TestCase):
    """Verify bounds before exposing firmware LED entries."""

    def test_decode_preserves_decks_and_blink_period(self) -> None:
        snapshot = LedSnapshot.decode(make_snapshot())
        self.assertEqual(snapshot.sequence, 7)
        self.assertTrue(snapshot.lit(1, 1, 0.3))
        self.assertTrue(snapshot.lit(1, 2, 0.1))
        self.assertFalse(snapshot.lit(1, 2, 0.3))
        self.assertEqual(snapshot.rgb(1, 2), (0, 255, 0))
        self.assertFalse(snapshot.lit(2, 1, 0.1))

    def test_reject_invalid_layout(self) -> None:
        valid = make_snapshot()
        invalid_count = bytearray(valid)
        struct.pack_into("<I", invalid_count, 8, 65)
        for payload in (b"", valid[:-1], valid + b"x", b"BAD!" + valid[4:], bytes(invalid_count)):
            with self.subTest(size=len(payload)):
                with self.assertRaises(ValueError):
                    LedSnapshot.decode(payload)

    def test_reject_invalid_entry(self) -> None:
        for offset, value in ((32, 2), (33, 3), (34, 2)):
            payload = bytearray(make_snapshot())
            payload[offset] = value
            with self.assertRaises(ValueError):
                LedSnapshot.decode(bytes(payload))

    def test_dim_entry_is_off(self) -> None:
        payload = bytearray(make_snapshot())
        payload[34] = 1
        self.assertFalse(LedSnapshot.decode(bytes(payload)).lit(1, 1, 0))

    def test_zero_period_uses_half_second(self) -> None:
        payload = bytearray(make_snapshot())
        struct.pack_into("<H", payload, 46, 0)
        snapshot = LedSnapshot.decode(bytes(payload))
        self.assertTrue(snapshot.lit(1, 2, 0.1))
        self.assertFalse(snapshot.lit(1, 2, 0.4))

    def test_invalid_lookup_rejected(self) -> None:
        snapshot = LedSnapshot.decode(make_snapshot())
        for led_id, deck in ((-1, 1), (64, 1), (1, 0), (1, 3)):
            with self.assertRaises(ValueError):
                snapshot.lit(led_id, deck, 0)


if __name__ == "__main__":
    unittest.main()
