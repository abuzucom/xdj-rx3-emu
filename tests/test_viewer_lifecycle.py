"""Exercise viewer behavior for bridge and shutdown failures."""

from __future__ import annotations

import queue
import threading
import unittest

from controller_client.viewer import ScreenViewer
from controllers.bridge_client import BridgeConnectionError


class _RaisingClient:
    def __init__(self, run_error: Exception | None = None) -> None:
        self.run_error = run_error
        self.close_error: Exception | None = None

    def run(self) -> None:
        if self.run_error is not None:
            raise self.run_error

    def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error


class _DestroyTrackingRoot:
    def __init__(self) -> None:
        self.destroyed = False

    def destroy(self) -> None:
        self.destroyed = True


class ViewerLifecycleTest(unittest.TestCase):
    """Keep expected connection failures separate from programming errors."""

    def create_viewer(self, client: _RaisingClient) -> ScreenViewer:
        viewer = ScreenViewer.__new__(ScreenViewer)
        viewer._client = client
        viewer._closing = False
        viewer._status_queue = queue.SimpleQueue()
        viewer._client_thread = threading.current_thread()
        viewer._root = _DestroyTrackingRoot()
        return viewer

    def test_expected_bridge_failure_reaches_status_queue(self) -> None:
        viewer = self.create_viewer(
            _RaisingClient(BridgeConnectionError("connection refused"))
        )

        viewer._run_client()

        self.assertEqual(
            viewer._status_queue.get_nowait(),
            "BridgeConnectionError",
        )

    def test_unexpected_client_error_propagates(self) -> None:
        viewer = self.create_viewer(_RaisingClient(ValueError("invalid frame")))

        with self.assertRaisesRegex(ValueError, "invalid frame"):
            viewer._run_client()

    def test_close_error_propagates_after_root_destruction(self) -> None:
        client = _RaisingClient()
        client.close_error = OSError("socket close failed")
        viewer = self.create_viewer(client)

        with self.assertRaisesRegex(OSError, "socket close failed"):
            viewer._close()

        self.assertTrue(viewer._root.destroyed)


if __name__ == "__main__":
    unittest.main()
