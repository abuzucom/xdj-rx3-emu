"""Relay firmware snapshots with consumer validation and best-effort race detection.

The snapshot file assumes one trusted firmware writer. Matching reads do not
guarantee an atomic snapshot or authenticity. Replacement can leave an open
descriptor on an older snapshot until the next poll. The desktop decoder
validates every received entry before applying LED state.
"""

from pathlib import Path

if __package__:
    from .rxl_layout import LED_COUNT, LED_SNAPSHOT_SIZE
else:
    from rxl_layout import LED_COUNT, LED_SNAPSHOT_SIZE

LED_FRAME_TYPE = 0x17
SNAPSHOT_SIZE = LED_SNAPSHOT_SIZE
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
