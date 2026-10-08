"""Cover bridge frame decoding and firmware screen assembly."""

from __future__ import annotations

import socket
import struct
import threading
import time
import unittest
from fractions import Fraction

from controller_client.main import parse_controller_arguments
from controller_client.viewer import (
    ScreenViewer,
    calculate_display_scale,
    rgba_to_ppm,
)
from controllers.backends.base import ControllerBackend
from controllers.bridge_client import BridgeClient
from controllers.events import ButtonEvent, ButtonOp, ControlKey
from controllers.protocol import FrameStreamDecoder, read_frame, write_frame
from controllers.screen import ScreenFrameBuffer


def encode_frame(message_type: int, payload: bytes) -> bytes:
    """Encode one bridge frame for decoder tests."""
    return struct.pack("<BI", message_type, len(payload)) + payload


class _OneShotScreenBackend(ControllerBackend):
    def __init__(self) -> None:
        self._sent = False
        self.closed = False

    def poll(self) -> ButtonEvent | None:
        if self._sent:
            return None
        self._sent = True
        return ButtonEvent(key=ControlKey.PLAY, op=ButtonOp.PRESS, channel=0)

    def close(self) -> None:
        self.closed = True


class FrameStreamDecoderTest(unittest.TestCase):
    """Exercise stream decoding across arbitrary TCP segment boundaries."""

    def test_retains_partial_frame_and_decodes_concatenated_frames(self) -> None:
        decoder = FrameStreamDecoder()
        first_frame = encode_frame(0x01, b"bridge connected")
        second_frame = encode_frame(0x10, struct.pack("<HH", 4, 2))

        self.assertEqual(decoder.feed(first_frame[:3]), [])
        decoded = decoder.feed(first_frame[3:] + second_frame)

        self.assertEqual(decoded, [(0x01, b"bridge connected"), (0x10, struct.pack("<HH", 4, 2))])

    def test_rejects_oversized_frame_length(self) -> None:
        decoder = FrameStreamDecoder(max_frame_length=2)
        oversized_header = struct.pack("<BI", 0x10, 3)

        with self.assertRaisesRegex(ValueError, "length limit"):
            decoder.feed(oversized_header)


class BridgeScreenIntegrationTest(unittest.TestCase):
    """Exercise inbound screen rendering with outbound MIDI on one connection."""

    def test_receives_screen_tiles_and_forwards_midi_event(self) -> None:
        port_holder: list[int] = []
        received_events: list[tuple[int, bytes]] = []
        server_errors: list[Exception] = []
        server_ready = threading.Event()
        pixels = bytes((11, 22, 33, 255))

        def serve_bridge() -> None:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                    server.bind(("127.0.0.1", 0))
                    server.listen(1)
                    server.settimeout(5.0)
                    port_holder.append(server.getsockname()[1])
                    server_ready.set()
                    connection, _ = server.accept()
                    with connection:
                        write_frame(connection, 0x10, struct.pack("<HH", 1, 1))
                        write_frame(connection, 0x11, struct.pack("<HHHH", 0, 0, 1, 1) + pixels)
                        frame = read_frame(connection, timeout=5.0)
                        if frame is not None:
                            received_events.append(frame)
            except OSError as exc:
                server_errors.append(exc)
                server_ready.set()

        server_thread = threading.Thread(target=serve_bridge)
        server_thread.start()
        self.assertTrue(server_ready.wait(timeout=2.0), "bridge server did not start")
        self.assertTrue(port_holder, "bridge server did not bind a port")
        backend = _OneShotScreenBackend()
        screen = ScreenFrameBuffer()
        client = BridgeClient(backend, "127.0.0.1", port_holder[0], screen_buffer=screen)
        client_thread = threading.Thread(target=client.run)
        client_thread.start()
        client_thread.join(timeout=5.0)
        client.close()
        server_thread.join(timeout=5.0)

        self.assertFalse(client_thread.is_alive(), "bridge client did not stop")
        self.assertFalse(server_thread.is_alive(), "bridge server did not stop")
        self.assertFalse(server_errors)
        self.assertTrue(backend.closed)
        self.assertEqual(received_events[0][0], BridgeClient.FRAME_TYPE_KEY_COMMAND)
        self.assertEqual(screen.snapshot(), (1, 1, pixels))

    def test_close_cancels_bridge_connection_retry(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            port = server.getsockname()[1]
        backend = _OneShotScreenBackend()
        client = BridgeClient(backend, "127.0.0.1", port, screen_buffer=ScreenFrameBuffer())
        client_errors: list[RuntimeError] = []

        def run_client() -> None:
            try:
                client.run()
            except RuntimeError as exc:
                client_errors.append(exc)

        client_thread = threading.Thread(target=run_client)
        client_thread.start()
        time.sleep(0.05)
        client.close()
        client_thread.join(timeout=3.0)

        self.assertFalse(client_thread.is_alive(), "connection retry did not stop")
        self.assertTrue(backend.closed)
        self.assertTrue(client_errors)
        self.assertEqual(str(client_errors[0]), "Bridge connection was cancelled")


class ScreenFrameBufferTest(unittest.TestCase):
    """Exercise screen dimensions, tile assembly, and malformed input checks."""

    def test_assembles_rgba_tiles_and_coalesces_replacements(self) -> None:
        screen = ScreenFrameBuffer()
        screen.apply_frame(0x10, struct.pack("<HH", 4, 2))
        first_pixels = bytes((1, 2, 3, 255, 4, 5, 6, 255))
        latest_pixels = bytes((7, 8, 9, 255, 10, 11, 12, 255))
        first_tile = struct.pack("<HHHH", 1, 0, 2, 1) + first_pixels
        latest_tile = struct.pack("<HHHH", 1, 0, 2, 1) + latest_pixels

        screen.apply_frame(0x11, first_tile)
        screen.apply_frame(0x11, latest_tile)
        width, height, reset, tiles = screen.take_updates()

        self.assertEqual((width, height, reset), (4, 2, True))
        self.assertEqual(len(tiles), 1)
        self.assertEqual(tiles[0].rgba, latest_pixels)
        self.assertEqual(screen.snapshot()[2][4:12], latest_pixels)
        self.assertEqual(screen.take_updates()[2:], (False, ()))

    def test_rejects_tiles_before_dimensions_and_out_of_bounds_tiles(self) -> None:
        screen = ScreenFrameBuffer()
        tile = struct.pack("<HHHH", 0, 0, 1, 1) + bytes(4)
        with self.assertRaisesRegex(ValueError, "must arrive before"):
            screen.apply_frame(0x11, tile)

        screen.apply_frame(0x10, struct.pack("<HH", 2, 2))
        outside_tile = struct.pack("<HHHH", 2, 0, 1, 1) + bytes(4)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            screen.apply_frame(0x11, outside_tile)

    def test_rejects_tile_payload_with_wrong_pixel_count(self) -> None:
        screen = ScreenFrameBuffer()
        screen.apply_frame(0x10, struct.pack("<HH", 2, 2))
        malformed_tile = struct.pack("<HHHH", 0, 0, 1, 1) + bytes(3)

        with self.assertRaisesRegex(ValueError, "payload size"):
            screen.apply_frame(0x11, malformed_tile)


class ControllerArgumentsTest(unittest.TestCase):
    """Keep direct Python CLI launches headless unless requested."""

    def test_visual_mode_is_opt_in_for_direct_cli(self) -> None:
        default_options = parse_controller_arguments([])
        visual_options = parse_controller_arguments(["--view"])
        headless_options = parse_controller_arguments(["--view", "--headless"])

        self.assertFalse(default_options.view)
        self.assertTrue(visual_options.view)
        self.assertTrue(headless_options.headless)


class ViewerImageDataTest(unittest.TestCase):
    """Check conversion of RGBA tiles to Tk-compatible PPM data."""

    def test_converts_rgba_pixels_without_alpha(self) -> None:
        rgba = bytes((1, 2, 3, 255, 4, 5, 6, 127))

        ppm = rgba_to_ppm(2, 1, rgba)

        self.assertEqual(ppm, b"P6\n2 1\n255\n" + bytes((1, 2, 3, 4, 5, 6)))

    def test_scaled_dimensions_round_up_to_keep_screen_visible(self) -> None:
        width = ScreenViewer._scaled_dimension(1280, Fraction(3, 4))
        height = ScreenViewer._scaled_dimension(800, Fraction(3, 4))

        self.assertEqual((width, height), (960, 600))

    def test_display_scale_keeps_screen_inside_small_canvas(self) -> None:
        scale = calculate_display_scale(318, 178, 1280, 800)
        width = ScreenViewer._scaled_dimension(1280, scale)
        height = ScreenViewer._scaled_dimension(800, scale)

        self.assertLessEqual(width, 318)
        self.assertLessEqual(height, 178)


if __name__ == "__main__":
    unittest.main()
