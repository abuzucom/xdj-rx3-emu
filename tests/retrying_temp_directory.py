#!/usr/bin/env python3
"""Retry temporary-directory cleanup after transient Windows Git locks."""
import tempfile
import time

MAX_CLEANUP_ATTEMPTS = 20
CLEANUP_RETRY_SECONDS = 0.05


class RetryingTemporaryDirectory(tempfile.TemporaryDirectory):
    """Remove a temporary directory after bounded transient-lock retries."""

    def cleanup(self) -> None:
        for attempt in range(MAX_CLEANUP_ATTEMPTS):
            try:
                super().cleanup()
                return
            except PermissionError:
                if attempt + 1 == MAX_CLEANUP_ATTEMPTS:
                    raise
                time.sleep(CLEANUP_RETRY_SECONDS)
