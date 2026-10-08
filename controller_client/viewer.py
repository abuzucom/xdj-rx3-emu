"""Display the emulator framebuffer in a Tk window."""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from fractions import Fraction
from typing import TYPE_CHECKING

from controllers.bridge_client import BridgeClient, BridgeConnectionError
from controllers.screen import ScreenFrameBuffer

if TYPE_CHECKING:
    from controllers.backends.base import ControllerBackend

FRAME_INTERVAL_MILLISECONDS = 33
MAX_SCALE_DENOMINATOR = 64
MAX_DISPLAY_SCALE = 2.0
CLIENT_THREAD_JOIN_TIMEOUT_SECONDS = 3.0
INITIAL_WINDOW_WIDTH = 960
INITIAL_WINDOW_HEIGHT = 600
BACKGROUND_COLOR = "#000000"


def rgba_to_ppm(width: int, height: int, rgba: bytes) -> bytes:
    """Convert an RGBA tile to a binary PPM image for Tk PhotoImage."""
    if width <= 0 or height <= 0:
        raise ValueError("Tile dimensions must be positive")
    pixel_count = width * height
    if len(rgba) != pixel_count * 4:
        raise ValueError("RGBA tile length does not match its dimensions")
    rgb = bytearray(pixel_count * 3)
    rgb[0::3] = rgba[0::4]
    rgb[1::3] = rgba[1::4]
    rgb[2::3] = rgba[2::4]
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    return header + bytes(rgb)


def calculate_display_scale(
    canvas_width: int,
    canvas_height: int,
    screen_width: int,
    screen_height: int,
) -> Fraction:
    """Return a bounded rational scale that keeps the screen inside the canvas."""
    if min(canvas_width, canvas_height, screen_width, screen_height) <= 0:
        raise ValueError("Canvas and screen dimensions must be positive")
    scale = min(
        canvas_width / screen_width,
        canvas_height / screen_height,
        MAX_DISPLAY_SCALE,
    )
    ratio = Fraction(scale).limit_denominator(MAX_SCALE_DENOMINATOR)
    if ratio > scale:
        ratio = Fraction(int(scale * MAX_SCALE_DENOMINATOR), MAX_SCALE_DENOMINATOR)
    return ratio


class ScreenViewer:
    """Render screen updates while the bridge client forwards MIDI events."""

    def __init__(
        self,
        backend: ControllerBackend,
        host: str,
        port: int,
    ) -> None:
        self._root = tk.Tk()
        self._root.title("XDJ-RX3 Emulator")
        self._root.geometry(f"{INITIAL_WINDOW_WIDTH}x{INITIAL_WINDOW_HEIGHT}")
        self._root.minsize(320, 200)
        self._root.configure(background=BACKGROUND_COLOR)
        self._screen = ScreenFrameBuffer()
        self._client = BridgeClient(backend, host, port, screen_buffer=self._screen)
        self._client_thread = threading.Thread(
            target=self._run_client,
            name="rx3-controller-client",
            daemon=False,
        )
        self._status_queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._status = tk.StringVar(master=self._root, value="Connecting to emulator bridge")
        self._source_image: tk.PhotoImage | None = None
        self._display_image: tk.PhotoImage | None = None
        self._closing = False
        self._resize_pending = True
        self._create_widgets()
        self._root.protocol("WM_DELETE_WINDOW", self._close)
        self._canvas.bind("<Configure>", self._handle_resize)
        self._client_thread.start()
        self._root.after(0, self._refresh_screen)

    def run(self) -> None:
        """Run the Tk event loop and close the client on exit."""
        try:
            self._root.mainloop()
        finally:
            self._close()

    def _create_widgets(self) -> None:
        status_label = tk.Label(
            self._root,
            anchor="w",
            background="#202020",
            foreground="#f0f0f0",
            padx=8,
            textvariable=self._status,
        )
        status_label.pack(fill="x")
        self._canvas = tk.Canvas(
            self._root,
            background=BACKGROUND_COLOR,
            highlightthickness=0,
        )
        self._canvas.pack(fill="both", expand=True)
        self._image_item = self._canvas.create_image(0, 0, anchor="center")

    def _run_client(self) -> None:
        try:
            self._client.run()
            if not self._closing:
                self._status_queue.put("Bridge disconnected")
        except (BridgeConnectionError, OSError) as exc:
            logging.warning(
                "RX3 screen client stopped: %s",
                type(exc).__name__,
            )
            if not self._closing:
                self._status_queue.put(type(exc).__name__)

    def _refresh_screen(self) -> None:
        if self._closing:
            return
        self._refresh_status()
        width, height, reset, tiles = self._screen.take_updates()
        if reset:
            self._source_image = tk.PhotoImage(width=width, height=height)
            self._status.set(f"Live RX3 screen: {width}x{height}")
            self._resize_pending = True
        if tiles:
            width, height, pixels = self._screen.snapshot()
            ppm_data = rgba_to_ppm(width, height, pixels)
            self._source_image = tk.PhotoImage(data=ppm_data, format="PPM")
            self._resize_pending = True
        if self._source_image is not None and (reset or tiles or self._resize_pending):
            self._render_screen()
            self._resize_pending = False
        self._root.after(FRAME_INTERVAL_MILLISECONDS, self._refresh_screen)

    def _refresh_status(self) -> None:
        try:
            error_name = self._status_queue.get_nowait()
        except queue.Empty:
            return
        self._status.set(f"Controller stopped: {error_name}")

    def _render_screen(self) -> None:
        if self._source_image is None:
            return
        canvas_width = self._canvas.winfo_width()
        canvas_height = self._canvas.winfo_height()
        if canvas_width <= 1 or canvas_height <= 1:
            return
        ratio = calculate_display_scale(
            canvas_width,
            canvas_height,
            self._source_image.width(),
            self._source_image.height(),
        )
        if ratio.numerator == ratio.denominator:
            display_image = self._source_image
        else:
            scaled_width = self._scaled_dimension(self._source_image.width(), ratio)
            scaled_height = self._scaled_dimension(self._source_image.height(), ratio)
            display_image = tk.PhotoImage(width=scaled_width, height=scaled_height)
            display_image.tk.call(
                str(display_image),
                "copy",
                str(self._source_image),
                "-zoom",
                ratio.numerator,
                ratio.numerator,
                "-subsample",
                ratio.denominator,
                ratio.denominator,
            )
        self._display_image = display_image
        center_x = canvas_width // 2
        center_y = canvas_height // 2
        self._canvas.coords(self._image_item, center_x, center_y)
        self._canvas.itemconfigure(self._image_item, image=self._display_image)

    @staticmethod
    def _scaled_dimension(source_size: int, ratio: Fraction) -> int:
        numerator = source_size * ratio.numerator
        return max(1, (numerator + ratio.denominator - 1) // ratio.denominator)

    def _handle_resize(self, _event: tk.Event) -> None:
        self._resize_pending = True

    def _close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self._client.close()
        finally:
            try:
                self._join_client_thread()
            finally:
                self._root.destroy()

    def _join_client_thread(self) -> None:
        if self._client_thread is threading.current_thread():
            return
        self._client_thread.join(timeout=CLIENT_THREAD_JOIN_TIMEOUT_SECONDS)
        if self._client_thread.is_alive():
            logging.warning("RX3 screen client thread did not stop after window close")


def run_viewer(backend: ControllerBackend, host: str, port: int) -> int:
    """Open the live RX3 screen and return after the window closes."""
    try:
        viewer = ScreenViewer(backend, host, port)
    except Exception as exc:
        # Release the backend if construction fails before the viewer owns it.
        try:
            backend.close()
        except Exception:
            # Preserve the startup exception if backend cleanup also fails.
            logging.exception("Controller backend cleanup failed after viewer startup")
        if isinstance(exc, tk.TclError):
            raise RuntimeError("Could not open the RX3 screen window. Check Python Tcl/Tk support.") from exc
        raise
    viewer.run()
    return 0
