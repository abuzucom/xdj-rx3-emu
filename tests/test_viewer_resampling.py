"""Check pixel preservation when fitting the firmware screen to a window."""

from __future__ import annotations

import struct
import unittest

from controller_client.viewer import resize_rgba, rgba_to_ppm
from controllers.screen import ScreenFrameBuffer


def make_pattern(width: int, height: int) -> bytes:
    """Give every source location distinct horizontal and vertical channels."""
    return bytes(
        component for y in range(height) for x in range(width) for component in (x % 256, y % 256, (x + y) % 256, 255)
    )


class ViewerResamplingTest(unittest.TestCase):
    """Verify actual image bytes independently of Tk display availability."""

    def test_fractional_reduction_preserves_thin_strokes(self) -> None:
        for horizontal in (False, True):
            pixels = bytes(
                component
                for y in range(8)
                for x in range(8)
                for component in ((255 * ((y if horizontal else x) % 2),) * 3 + (255,))
            )
            resized = resize_rgba(8, 8, pixels, (6, 6))
            with self.subTest(horizontal=horizontal):
                self.assertTrue(all(0 < value < 255 for value in resized[0::4]))
                self.assertEqual(resized[0::4], resized[1::4])
                self.assertEqual(resized[0::4], resized[2::4])
                self.assertEqual(resized[3::4], bytes([255]) * 36)

    def test_fractional_enlargement_preserves_spatial_order(self) -> None:
        red, blue = bytes((255, 0, 0, 255)), bytes((0, 0, 255, 255))
        pixels = (red * 2 + blue * 2) * 2

        resized = resize_rgba(4, 2, pixels, (6, 3))

        self.assertEqual(resized[:4], red)
        self.assertEqual(resized[-4:], blue)
        self.assertGreater(resized[2 * 4], 0)
        self.assertGreater(resized[2 * 4 + 2], 0)
        self.assertEqual(resized[1::4], bytes(18))
        self.assertEqual(resized[3::4], bytes([255]) * 18)

    def test_native_size_preserves_every_channel(self) -> None:
        pixels = bytes((1, 2, 3, 4, 5, 6, 7, 8))
        self.assertEqual(resize_rgba(2, 1, pixels, (2, 1)), pixels)

    def test_single_pixel_destination_preserves_image_average(self) -> None:
        pixels = make_pattern(7, 5)
        self.assertEqual(resize_rgba(7, 5, pixels, (1, 1)), bytes((3, 2, 5, 255)))

    def test_rejects_invalid_dimensions_and_pixel_lengths(self) -> None:
        for width, height, pixels, target in (
            (0, 1, b"", (1, 1)),
            (1, -1, b"", (1, 1)),
            (1, 1, bytes(4), (0, 1)),
            (1, 1, bytes(4), (1, -1)),
            (2, 1, bytes(4), (1, 1)),
        ):
            with self.subTest(width=width, height=height, target=target):
                with self.assertRaises(ValueError):
                    resize_rgba(width, height, pixels, target)

    def test_full_screen_tiles_preserve_colors_and_positions_in_ppm(self) -> None:
        width, height = 1280, 800
        colors = ((16, 32, 48), (32, 96, 160), (192, 64, 32), (48, 160, 96))
        top = bytes((*colors[0], 255)) * 640 + bytes((*colors[1], 255)) * 640
        bottom = bytes((*colors[2], 255)) * 640 + bytes((*colors[3], 255)) * 640
        pixels = top * 400 + bottom * 400
        screen = ScreenFrameBuffer()
        screen.apply_frame(0x10, struct.pack("<HH", width, height))
        for y in range(0, height, 64):
            tile_height = min(64, height - y)
            start = y * width * 4
            payload = struct.pack("<HHHH", 0, y, width, tile_height)
            screen.apply_frame(0x11, payload + pixels[start : start + tile_height * width * 4])
        snapshot = screen.take_updates_with_snapshot()[4]
        self.assertEqual(snapshot, pixels)

        resized = resize_rgba(width, height, snapshot, (925, 578))
        ppm = rgba_to_ppm(925, 578, resized)
        header = b"P6\n925 578\n255\n"
        self.assertEqual(ppm[: len(header)], header)
        self.assertEqual(len(ppm), len(header) + 925 * 578 * 3)
        for x, y, color in ((23, 31, colors[0]), (900, 31, colors[1]), (23, 550, colors[2]), (924, 577, colors[3])):
            expected = bytes(color)
            start = len(header) + (y * 925 + x) * 3
            self.assertEqual(ppm[start : start + 3], expected)


if __name__ == "__main__":
    unittest.main()
