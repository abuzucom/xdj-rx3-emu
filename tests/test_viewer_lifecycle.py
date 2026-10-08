"""Exercise viewer behavior for bridge and shutdown failures."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from controller_client.viewer import ScreenViewer, run_viewer
from controllers.bridge_client import BridgeConnectionError


class _RaisingClient:
    def __init__(
        self,
        run_error: Exception | None = None,
        *,
        wait_for_close: bool = False,
    ) -> None:
        self.run_error = run_error
        self.wait_for_close = wait_for_close
        self.close_error: Exception | None = None
        self.run_started = threading.Event()
        self.stop_requested = threading.Event()

    def run(self) -> None:
        self.run_started.set()
        if self.wait_for_close:
            self.stop_requested.wait(timeout=5.0)
        if self.run_error is not None:
            raise self.run_error

    def close(self) -> None:
        self.stop_requested.set()
        if self.close_error is not None:
            raise self.close_error


class _DestroyTrackingRoot:
    def __init__(self) -> None:
        self.destroyed = False

    def title(self, _title: str) -> None:
        return None

    def geometry(self, _geometry: str) -> None:
        return None

    def minsize(self, _width: int, _height: int) -> None:
        return None

    def configure(self, **_options: object) -> None:
        return None

    def protocol(self, _name: str, _callback: object) -> None:
        return None

    def after(self, _delay: int, _callback: object) -> None:
        return None

    def destroy(self) -> None:
        self.destroyed = True


class _FakeWidget:
    def __init__(self, *_args: object, **_options: object) -> None:
        return None

    def pack(self, **_options: object) -> None:
        return None


class _FakeCanvas(_FakeWidget):
    def __init__(self, *_args: object, **_options: object) -> None:
        super().__init__()
        self.viewer: ScreenViewer | None = None
        self.image_before_configure: object | None = None
        self.configured_image: object | None = None

    def create_image(self, *_args: object, **_options: object) -> int:
        return 1

    def bind(self, *_args: object, **_options: object) -> None:
        return None

    def winfo_width(self) -> int:
        return 640

    def winfo_height(self) -> int:
        return 400

    def coords(self, *_args: object) -> None:
        return None

    def itemconfigure(self, _item: int, *, image: object) -> None:
        if self.viewer is not None:
            self.image_before_configure = self.viewer._display_image
        self.configured_image = image


class _FakeStringVar:
    def __init__(self, *, master: object, value: str) -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value


class _FakeImage:
    def __init__(self, width: int, height: int) -> None:
        self._width = width
        self._height = height

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height


class _FakeBackend:
    def __init__(self) -> None:
        self.closed = False
        self.close_error: Exception | None = None

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class ViewerLifecycleTest(unittest.TestCase):
    """Exercise viewer construction and shutdown through public entry points."""

    def create_viewer(
        self,
        client: _RaisingClient,
    ) -> tuple[ScreenViewer, _DestroyTrackingRoot, _FakeBackend]:
        root = _DestroyTrackingRoot()
        backend = _FakeBackend()

        def create_client(
            _backend: object,
            _host: str,
            _port: int,
            *,
            screen_buffer: object,
        ) -> _RaisingClient:
            return client

        with (
            patch("controller_client.viewer.tk.Label", _FakeWidget),
            patch("controller_client.viewer.tk.Canvas", _FakeCanvas),
            patch("controller_client.viewer.tk.StringVar", _FakeStringVar),
        ):
            viewer = ScreenViewer(
                backend,
                "bridge.test",
                4480,
                root=root,
                client_factory=create_client,
            )
        viewer._canvas.viewer = viewer
        return viewer, root, backend

    def test_expected_bridge_failure_reaches_status_queue(self) -> None:
        viewer, _root, _backend = self.create_viewer(_RaisingClient(BridgeConnectionError("connection refused")))
        viewer._client_thread.join(timeout=2.0)

        self.assertFalse(viewer._client_thread.is_alive())
        self.assertEqual(
            viewer._status_queue.get_nowait(),
            "BridgeConnectionError",
        )
        viewer._close()

    def test_unexpected_client_error_propagates(self) -> None:
        errors: list[BaseException] = []
        client = _RaisingClient(ValueError("invalid frame"))
        with patch.object(
            threading,
            "excepthook",
            side_effect=lambda arguments: errors.append(arguments.exc_value),
        ):
            viewer, _root, _backend = self.create_viewer(client)
            viewer._client_thread.join(timeout=2.0)

        self.assertFalse(viewer._client_thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        viewer._close()

    def test_close_error_propagates_after_root_destruction(self) -> None:
        client = _RaisingClient(wait_for_close=True)
        client.close_error = OSError("socket close failed")
        viewer, root, _backend = self.create_viewer(client)
        self.assertTrue(client.run_started.wait(timeout=2.0))

        with self.assertRaisesRegex(OSError, "socket close failed"):
            viewer._close()

        self.assertFalse(viewer._client_thread.is_alive())
        self.assertTrue(root.destroyed)

    def test_redraw_keeps_previous_display_reference_until_reconfigured(self) -> None:
        client = _RaisingClient(wait_for_close=True)
        viewer, _root, _backend = self.create_viewer(client)
        previous_display = object()
        source_image = _FakeImage(640, 400)
        viewer._display_image = previous_display
        viewer._source_image = source_image

        viewer._render_screen()

        self.assertIs(viewer._canvas.image_before_configure, previous_display)
        self.assertIs(viewer._canvas.configured_image, source_image)
        self.assertIs(viewer._display_image, source_image)
        viewer._close()

    def test_startup_failure_cleans_backend_and_tk_root(self) -> None:
        root = _DestroyTrackingRoot()
        backend = _FakeBackend()
        with (
            patch("controller_client.viewer.tk.Tk", return_value=root),
            patch(
                "controller_client.viewer.ScreenViewer",
                side_effect=ValueError("viewer construction failed"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "viewer construction failed"):
                run_viewer(backend, "bridge.test", 4480)

        self.assertTrue(backend.closed)
        self.assertTrue(root.destroyed)

    def test_startup_cleanup_failure_chains_original_error(self) -> None:
        root = _DestroyTrackingRoot()
        backend = _FakeBackend()
        backend.close_error = OSError("backend close failed")
        with (
            patch("controller_client.viewer.tk.Tk", return_value=root),
            patch(
                "controller_client.viewer.ScreenViewer",
                side_effect=ValueError("viewer construction failed"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "backend close failed") as caught:
                run_viewer(backend, "bridge.test", 4480)

        self.assertIsInstance(caught.exception.__context__, ValueError)
        self.assertTrue(root.destroyed)


if __name__ == "__main__":
    unittest.main()
