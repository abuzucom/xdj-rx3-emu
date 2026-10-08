"""Cover audio frame validation, buffering, and bridge routing."""

from __future__ import annotations

import ctypes
import struct
import unittest

from controller_client.audio import AudioPcmBuffer, _WaveFormatEx, _WaveHeader
from controllers.backends.base import ControllerBackend
from controllers.bridge_client import BridgeClient
from controllers.events import ControllerEvent
from controllers.protocol import FrameStreamDecoder


class _IdleBackend(ControllerBackend):
    def poll(self) -> ControllerEvent | None:
        return None

    def close(self) -> None:
        return None


class AudioPcmBufferTest(unittest.TestCase):
    def make_payload(
        self,
        samples: bytes,
        *,
        sample_rate: int = 44100,
        channels: int = 2,
    ) -> bytes:
        return struct.pack("<IB", sample_rate, channels) + samples

    def test_reads_pcm_from_a_valid_bridge_audio_frame(self) -> None:
        pcm = bytes(range(16))
        audio = AudioPcmBuffer(maximum_buffer_bytes=32)

        dropped = audio.submit_frame(self.make_payload(pcm))

        self.assertEqual(dropped, 0)
        self.assertEqual(audio.read_chunk(len(pcm), timeout=0), pcm)

    def test_drops_oldest_samples_when_the_buffer_reaches_its_limit(self) -> None:
        audio = AudioPcmBuffer(maximum_buffer_bytes=8)
        audio.submit_frame(self.make_payload(bytes(range(8))))

        dropped = audio.submit_frame(self.make_payload(bytes(range(8, 16))))

        self.assertEqual(dropped, 8)
        self.assertEqual(audio.read_chunk(8, timeout=0), bytes(range(8, 16)))

    def test_rejects_audio_with_unsupported_format_or_partial_sample_frames(self) -> None:
        audio = AudioPcmBuffer(maximum_buffer_bytes=32)

        with self.assertRaisesRegex(ValueError, "sample rate"):
            audio.submit_frame(self.make_payload(bytes(4), sample_rate=48000))
        with self.assertRaisesRegex(ValueError, "channel count"):
            audio.submit_frame(self.make_payload(bytes(4), channels=1))
        with self.assertRaisesRegex(ValueError, "frame-aligned"):
            audio.submit_frame(self.make_payload(bytes(6)))

    def test_wave_output_structures_match_the_windows_abi_sizes(self) -> None:
        pointer_size = ctypes.sizeof(ctypes.c_void_p)

        self.assertEqual(ctypes.sizeof(_WaveFormatEx), 18)
        self.assertEqual(ctypes.sizeof(_WaveHeader), 32 if pointer_size == 4 else 48)

    def test_bridge_routes_a_split_audio_frame_to_the_playback_buffer(self) -> None:
        pcm = bytes(range(16))
        audio = AudioPcmBuffer(maximum_buffer_bytes=32)
        client = BridgeClient(
            _IdleBackend(),
            "127.0.0.1",
            4480,
            audio_sink=audio,
        )
        payload = self.make_payload(pcm)
        frame = struct.pack("<BI", 0x14, len(payload)) + payload
        decoder = FrameStreamDecoder()

        client._apply_inbound_data(decoder, frame[:7])
        self.assertEqual(audio.read_chunk(len(pcm), timeout=0), b"")
        client._apply_inbound_data(decoder, frame[7:])

        self.assertEqual(audio.read_chunk(len(pcm), timeout=0), pcm)


if __name__ == "__main__":
    unittest.main()
