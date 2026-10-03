#!/usr/bin/env python3
"""Cover smoke test utility, mock bridge protocol, and firmware archive handling."""

from __future__ import annotations

import socket
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.smoke_test import (
    DEFAULT_PORT,
    MIN_UPD_BYTES,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    UPD_FILENAME,
    MockBridgeServer,
    find_firmware_zip,
    prepare_firmware,
    test_bridge_client,
)


class SmokeTestBridgeProtocolTest(unittest.TestCase):
    """Test bridge mock server and client protocol validation."""

    def test_mock_bridge_handshake(self) -> None:
        """Verify mock bridge server emits protocol frames and client asserts them."""
        # Use port 4485 to avoid conflicts
        test_port = 4485
        server = MockBridgeServer(port=test_port)
        server.start()
        try:
            success = test_bridge_client(host="127.0.0.1", port=test_port, timeout=5.0)
            self.assertTrue(success, "Bridge client failed to complete handshake")
        finally:
            server.stop()

    def test_find_firmware_zip(self) -> None:
        """Verify zip archive discovery in directory."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            self.assertIsNone(find_firmware_zip(tmp_path))
            dummy_zip = tmp_path / "firmware_sample.zip"
            dummy_zip.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
            found = find_firmware_zip(tmp_path)
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "firmware_sample.zip")

    def test_prepare_firmware_archive_validation(self) -> None:
        """Verify validation errors on malformed archives."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            bad_zip = tmp_path / "bad.zip"
            with zipfile.ZipFile(bad_zip, "w") as zf:
                zf.writestr("OTHER.TXT", "not firmware")

            out_dir = tmp_path / "extracted"
            with self.assertRaises(ValueError):
                prepare_firmware(bad_zip, out_dir)


class FirmwareArchiveIntegrationTest(unittest.TestCase):
    """Test actual firmware archive in firmware/ if present."""

    def test_actual_firmware_zip_structure(self) -> None:
        """Check user-provided firmware zip contains XDJRX3.UPD when available."""
        zip_path = find_firmware_zip(Path("firmware"))
        if not zip_path:
            self.skipTest("No firmware zip in firmware/ directory")

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            self.assertIn(UPD_FILENAME, names, f"Archive missing {UPD_FILENAME}")
            info = zf.getinfo(UPD_FILENAME)
            self.assertGreaterEqual(info.file_size, MIN_UPD_BYTES)


if __name__ == "__main__":
    unittest.main()
