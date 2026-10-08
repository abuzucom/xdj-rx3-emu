"""Relay stable firmware LED snapshots without importing desktop dependencies."""

from pathlib import Path

LED_FRAME_TYPE = 0x17
SNAPSHOT_SIZE = 1048
LED_COUNT = 64
POLL_SECONDS = 0.03


class LedRelay:
    """Publish changed RXL1 snapshots and initial state for each connection."""

    def __init__(self, path, client) -> None:
        self.path = Path(path)
        self.client = client
        self.previous = None

    def poll(self) -> None:
        """Reject incomplete or changing files before sending a full snapshot."""
        with self.client.lock:
            connection = self.client.sock
        if connection is None:
            return
        try:
            with self.path.open("rb") as stream:
                payload = stream.read(SNAPSHOT_SIZE + 1)
                stream.seek(0)
                confirmed = stream.read(SNAPSHOT_SIZE + 1)
        except OSError:
            # Firmware initialization can precede creation of the snapshot file.
            return
        if len(payload) != SNAPSHOT_SIZE or payload != confirmed or payload[:4] != b"RXL1":
            return
        if int.from_bytes(payload[8:12], "little") != LED_COUNT:
            return
        current = (connection, payload)
        if current != self.previous and self.client.send(LED_FRAME_TYPE, payload):
            self.previous = current

    def run(self, stopped) -> None:
        """Poll until the bridge shuts down."""
        while not stopped.wait(POLL_SECONDS):
            self.poll()
