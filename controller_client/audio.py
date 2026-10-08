"""Play bridge PCM through the Windows default audio device."""

from __future__ import annotations

import ctypes
import logging
import struct
import sys
import threading
import time
from typing import Protocol

AUDIO_FORMAT = struct.Struct("<IB")
AUDIO_SAMPLE_RATE = 44100
AUDIO_CHANNELS = 2
AUDIO_BYTES_PER_SAMPLE = 2
AUDIO_BLOCK_ALIGNMENT = AUDIO_CHANNELS * AUDIO_BYTES_PER_SAMPLE
AUDIO_BYTES_PER_SECOND = AUDIO_SAMPLE_RATE * AUDIO_BLOCK_ALIGNMENT
AUDIO_FRAME_LIMIT_MILLISECONDS = 100
AUDIO_BUFFER_LIMIT_MILLISECONDS = 60
AUDIO_BUFFER_LIMIT_BYTES = AUDIO_BYTES_PER_SECOND * AUDIO_BUFFER_LIMIT_MILLISECONDS // 1000
PLAYBACK_CHUNK_MILLISECONDS = 10
PLAYBACK_CHUNK_BYTES = AUDIO_BYTES_PER_SECOND * PLAYBACK_CHUNK_MILLISECONDS // 1000
PLAYBACK_BUFFER_COUNT = 2
AUDIO_THREAD_JOIN_TIMEOUT_SECONDS = 2.0
WAVE_FORMAT_PCM = 1
WAVE_MAPPER = 0xFFFFFFFF
CALLBACK_NULL = 0
WHDR_DONE = 0x00000001


class AudioSink(Protocol):
    """Accept bridge audio frames and release playback resources."""

    def submit_frame(self, payload: bytes) -> int:
        """Queue one PCM frame and return the number of dropped bytes."""
        ...

    def close(self) -> None:
        """Stop playback and release the audio device."""
        ...


class AudioPcmBuffer:
    """Hold a bounded sequence of validated stereo PCM samples."""

    def __init__(self, maximum_buffer_bytes: int = AUDIO_BUFFER_LIMIT_BYTES) -> None:
        if maximum_buffer_bytes < AUDIO_BLOCK_ALIGNMENT:
            raise ValueError("Audio buffer capacity must hold one sample frame")
        if maximum_buffer_bytes % AUDIO_BLOCK_ALIGNMENT:
            raise ValueError("Audio buffer capacity must align to sample frames")
        self._maximum_buffer_bytes = maximum_buffer_bytes
        self._samples = bytearray()
        self._lock = threading.Lock()

    def submit_frame(self, payload: bytes) -> int:
        """Validate a bridge frame and keep its newest samples in the buffer."""
        if len(payload) < AUDIO_FORMAT.size:
            raise ValueError("Audio frame is missing its format header")
        if len(payload) > self._maximum_frame_bytes():
            raise ValueError("Audio frame exceeds the supported duration")
        sample_rate, channels = AUDIO_FORMAT.unpack_from(payload)
        if sample_rate != AUDIO_SAMPLE_RATE:
            raise ValueError("Audio frame has an unsupported sample rate")
        if channels != AUDIO_CHANNELS:
            raise ValueError("Audio frame has an unsupported channel count")
        samples = payload[AUDIO_FORMAT.size :]
        if not samples or len(samples) % AUDIO_BLOCK_ALIGNMENT:
            raise ValueError("Audio sample data must be nonempty and frame-aligned")
        return self._append_samples(samples)

    def read_chunk(
        self,
        maximum_bytes: int,
        timeout: float = 0.0,
        minimum_bytes: int = AUDIO_BLOCK_ALIGNMENT,
    ) -> bytes:
        """Remove an aligned PCM chunk after the requested minimum arrives."""
        self._validate_read_size(maximum_bytes, minimum_bytes)
        aligned_limit = maximum_bytes - maximum_bytes % AUDIO_BLOCK_ALIGNMENT
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            with self._lock:
                if len(self._samples) >= minimum_bytes:
                    count = min(aligned_limit, len(self._samples))
                    result = bytes(self._samples[:count])
                    del self._samples[:count]
                    return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b""
            time.sleep(min(remaining, 0.001))

    def _append_samples(self, samples: bytes) -> int:
        dropped = 0
        if len(samples) > self._maximum_buffer_bytes:
            excess = len(samples) - self._maximum_buffer_bytes
            samples = samples[excess:]
            dropped += excess
        with self._lock:
            overflow = max(
                0,
                len(self._samples) + len(samples) - self._maximum_buffer_bytes,
            )
            overflow -= overflow % AUDIO_BLOCK_ALIGNMENT
            if overflow:
                del self._samples[:overflow]
                dropped += overflow
            self._samples.extend(samples)
        return dropped

    @staticmethod
    def _maximum_frame_bytes() -> int:
        frame_limit = AUDIO_BYTES_PER_SECOND * AUDIO_FRAME_LIMIT_MILLISECONDS // 1000
        return AUDIO_FORMAT.size + frame_limit

    @staticmethod
    def _validate_read_size(maximum_bytes: int, minimum_bytes: int) -> None:
        if maximum_bytes < AUDIO_BLOCK_ALIGNMENT:
            raise ValueError("Audio read size must hold one sample frame")
        if minimum_bytes < AUDIO_BLOCK_ALIGNMENT:
            raise ValueError("Audio read threshold must hold one sample frame")
        if minimum_bytes > maximum_bytes:
            raise ValueError("Audio read threshold cannot exceed the read size")
        if minimum_bytes % AUDIO_BLOCK_ALIGNMENT:
            raise ValueError("Audio read threshold must align to sample frames")


class _WaveFormatEx(ctypes.Structure):
    _pack_ = 2
    _fields_ = [
        ("format_tag", ctypes.c_uint16),
        ("channels", ctypes.c_uint16),
        ("samples_per_second", ctypes.c_uint32),
        ("average_bytes_per_second", ctypes.c_uint32),
        ("block_alignment", ctypes.c_uint16),
        ("bits_per_sample", ctypes.c_uint16),
        ("extra_size", ctypes.c_uint16),
    ]


class _WaveHeader(ctypes.Structure):
    _fields_ = [
        ("data", ctypes.c_void_p),
        ("buffer_length", ctypes.c_uint32),
        ("bytes_recorded", ctypes.c_uint32),
        ("user", ctypes.c_size_t),
        ("flags", ctypes.c_uint32),
        ("loops", ctypes.c_uint32),
        ("next", ctypes.c_void_p),
        ("reserved", ctypes.c_size_t),
    ]


class _WaveBuffer:
    """Keep one prepared PCM buffer alive until the driver finishes it."""

    def __init__(self) -> None:
        self.data = ctypes.create_string_buffer(PLAYBACK_CHUNK_BYTES)
        self.header = _WaveHeader(
            ctypes.cast(self.data, ctypes.c_void_p),
            PLAYBACK_CHUNK_BYTES,
            0,
            0,
            0,
            0,
            None,
            0,
        )
        self.prepared = False
        self.queued = False


class _WaveOutDevice:
    """Stream PCM through the current Windows multimedia output device."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows audio output requires Windows")
        self._winmm = ctypes.WinDLL("winmm")
        self._handle = ctypes.c_void_p()
        self._buffers: list[_WaveBuffer] = []
        self._configure_functions()
        self._open_default_device()
        try:
            self._prepare_buffers()
        except BaseException:
            try:
                self.close()
            except OSError as exc:
                logging.warning("Windows audio startup cleanup failed: %s", exc)
            raise

    def play_available(self, audio: AudioPcmBuffer) -> bool:
        """Submit ready 10 ms chunks to free device buffers."""
        wrote_samples = False
        for buffer in self._buffers:
            self._release_completed_buffer(buffer)
            if buffer.queued:
                continue
            samples = audio.read_chunk(
                PLAYBACK_CHUNK_BYTES,
                minimum_bytes=PLAYBACK_CHUNK_BYTES,
            )
            if not samples:
                continue
            ctypes.memmove(buffer.data, samples, len(samples))
            result = self._winmm.waveOutWrite(
                self._handle,
                ctypes.byref(buffer.header),
                ctypes.sizeof(buffer.header),
            )
            self._check_result(result, "waveOutWrite")
            buffer.queued = True
            wrote_samples = True
        return wrote_samples

    def close(self) -> None:
        """Stop queued audio and release each prepared buffer and device."""
        if not self._handle:
            return
        reset_result = self._winmm.waveOutReset(self._handle)
        cleanup_error = self._result_error(reset_result, "waveOutReset")
        for buffer in self._buffers:
            if not buffer.prepared:
                continue
            result = self._winmm.waveOutUnprepareHeader(
                self._handle,
                ctypes.byref(buffer.header),
                ctypes.sizeof(buffer.header),
            )
            error = self._result_error(result, "waveOutUnprepareHeader")
            cleanup_error = cleanup_error or error
            buffer.prepared = False
        close_result = self._winmm.waveOutClose(self._handle)
        error = self._result_error(close_result, "waveOutClose")
        cleanup_error = cleanup_error or error
        self._handle = ctypes.c_void_p()
        if cleanup_error is not None:
            raise cleanup_error

    def _configure_functions(self) -> None:
        self._winmm.waveOutOpen.argtypes = (
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint32,
            ctypes.POINTER(_WaveFormatEx),
            ctypes.c_size_t,
            ctypes.c_size_t,
            ctypes.c_uint32,
        )
        self._winmm.waveOutOpen.restype = ctypes.c_uint32
        header_arguments = (
            ctypes.c_void_p,
            ctypes.POINTER(_WaveHeader),
            ctypes.c_uint32,
        )
        for name in ("waveOutPrepareHeader", "waveOutWrite", "waveOutUnprepareHeader"):
            function = getattr(self._winmm, name)
            function.argtypes = header_arguments
            function.restype = ctypes.c_uint32
        self._winmm.waveOutReset.argtypes = (ctypes.c_void_p,)
        self._winmm.waveOutReset.restype = ctypes.c_uint32
        self._winmm.waveOutClose.argtypes = (ctypes.c_void_p,)
        self._winmm.waveOutClose.restype = ctypes.c_uint32

    def _open_default_device(self) -> None:
        audio_format = _WaveFormatEx(
            WAVE_FORMAT_PCM,
            AUDIO_CHANNELS,
            AUDIO_SAMPLE_RATE,
            AUDIO_BYTES_PER_SECOND,
            AUDIO_BLOCK_ALIGNMENT,
            AUDIO_BYTES_PER_SAMPLE * 8,
            0,
        )
        result = self._winmm.waveOutOpen(
            ctypes.byref(self._handle),
            WAVE_MAPPER,
            ctypes.byref(audio_format),
            CALLBACK_NULL,
            0,
            CALLBACK_NULL,
        )
        self._check_result(result, "waveOutOpen")

    def _prepare_buffers(self) -> None:
        for _ in range(PLAYBACK_BUFFER_COUNT):
            buffer = _WaveBuffer()
            self._buffers.append(buffer)
            result = self._winmm.waveOutPrepareHeader(
                self._handle,
                ctypes.byref(buffer.header),
                ctypes.sizeof(buffer.header),
            )
            self._check_result(result, "waveOutPrepareHeader")
            buffer.prepared = True

    def _release_completed_buffer(self, buffer: _WaveBuffer) -> None:
        if buffer.queued and buffer.header.flags & WHDR_DONE:
            buffer.queued = False

    @classmethod
    def _check_result(cls, result: int, operation: str) -> None:
        error = cls._result_error(result, operation)
        if error is not None:
            raise error

    @staticmethod
    def _result_error(result: int, operation: str) -> OSError | None:
        if result == 0:
            return None
        return OSError(f"Windows audio {operation} failed with status {result}")


class WindowsAudioSink(AudioSink):
    """Play bridge PCM through the Windows default multimedia output."""

    def __init__(self) -> None:
        self._audio = AudioPcmBuffer()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run_playback,
            name="rx3-windows-audio",
            daemon=False,
        )
        self._thread.start()

    def submit_frame(self, payload: bytes) -> int:
        """Queue bridge PCM while dropping stale data at the buffer limit."""
        if self._stop.is_set():
            raise RuntimeError("Windows audio playback is closed")
        if not self._thread.is_alive():
            raise OSError("Windows audio output is unavailable")
        return self._audio.submit_frame(payload)

    def close(self) -> None:
        """Stop the playback worker and wait for device cleanup."""
        self._stop.set()
        self._thread.join(timeout=AUDIO_THREAD_JOIN_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            logging.warning("Windows audio playback thread did not stop")

    def _run_playback(self) -> None:
        while not self._stop.is_set():
            device = None
            try:
                device = _WaveOutDevice()
                while not self._stop.is_set():
                    if not device.play_available(self._audio):
                        self._stop.wait(0.002)
            except OSError as exc:
                if not self._stop.is_set():
                    logging.warning("Windows audio output stopped: %s", exc)
                    self._stop.wait(1.0)
            finally:
                if device is not None:
                    try:
                        device.close()
                    except OSError as exc:
                        logging.warning(
                            "Windows audio device cleanup failed: %s",
                            exc,
                        )


def create_default_audio_sink() -> AudioSink | None:
    """Create Windows playback when the client runs on a Windows host."""
    if sys.platform != "win32":
        return None
    return WindowsAudioSink()
