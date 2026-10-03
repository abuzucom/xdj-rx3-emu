#!/usr/bin/env python3
"""Cover smoke test utility, mock bridge protocol, and firmware archive handling."""

from __future__ import annotations

import socket
import struct
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from scripts.smoke_test import (
    DEFAULT_FIRMWARE_URL,
    DEFAULT_PORT,
    EXPECTED_FIRMWARE_SHA256,
    FIRMWARE_ARCHIVE_NAME,
    MAX_FRAME_LENGTH,
    MIN_UPD_BYTES,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    UPD_FILENAME,
    MockBridgeServer,
    _handle_handshake,
    acquire_firmware,
    compute_file_sha256,
    connect_bridge_socket,
    download_firmware,
    find_firmware_zip,
    prepare_firmware,
    read_frame,
    test_bridge_client,
    write_frame,
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

    def test_frame_roundtrip(self) -> None:
        """Verify read_frame and write_frame roundtrip across a socket pair."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
            server_sock.bind(("127.0.0.1", 0))
            server_sock.listen(1)
            port = server_sock.getsockname()[1]

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_sock:
                client_sock.connect(("127.0.0.1", port))
                conn, _ = server_sock.accept()
                with conn:
                    payload = b"protocol payload data"
                    write_frame(client_sock, 0x05, payload)
                    result = read_frame(conn)
                    self.assertIsNotNone(result)
                    self.assertEqual(result, (0x05, payload))

    def test_read_frame_exceeds_max_length(self) -> None:
        """Verify read_frame rejects frames larger than max_length."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
            server_sock.bind(("127.0.0.1", 0))
            server_sock.listen(1)
            port = server_sock.getsockname()[1]

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_sock:
                client_sock.connect(("127.0.0.1", port))
                conn, _ = server_sock.accept()
                with conn:
                    oversized_len = MAX_FRAME_LENGTH + 1
                    client_sock.sendall(struct.pack("<BI", 0x01, oversized_len))
                    result = read_frame(conn)
                    self.assertIsNone(result)

    def test_connect_bridge_socket_failure_cleanup(self) -> None:
        """Verify connect_bridge_socket returns None cleanly without leaking sockets."""
        sock = connect_bridge_socket("127.0.0.1", port=1, timeout=0.1)
        self.assertIsNone(sock)

    def test_handshake_rejects_truncated_screen_info(self) -> None:
        """Verify _handle_handshake cleanly handles 0x10 frame shorter than 4 bytes."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
            server_sock.bind(("127.0.0.1", 0))
            server_sock.listen(1)
            port = server_sock.getsockname()[1]

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_sock:
                client_sock.connect(("127.0.0.1", port))
                conn, _ = server_sock.accept()
                with conn:
                    # Send truncated 0x10 frame (only 2 bytes instead of 4)
                    write_frame(conn, 0x10, b"\x00\x00")
                    result = _handle_handshake(client_sock, time.time(), timeout=1.0)
                    self.assertFalse(result)

    def test_prepare_firmware_rejects_path_traversal(self) -> None:
        """Verify prepare_firmware rejects symlink targets escaping output_dir."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            zip_path = tmp_path / "test.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(UPD_FILENAME, b"0" * MIN_UPD_BYTES)

            out_dir = tmp_path / "sub"
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / UPD_FILENAME
            try:
                target.symlink_to(tmp_path / "outside.txt")
            except (OSError, NotImplementedError):
                return

            with self.assertRaises(ValueError):
                prepare_firmware(zip_path, out_dir)

    def test_compute_file_sha256(self) -> None:
        """Verify compute_file_sha256 calculates expected SHA-256 digest."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sample_file = Path(tmp_dir) / "sample.bin"
            sample_data = b"XDJ-RX3-TEST-PAYLOAD"
            sample_file.write_bytes(sample_data)
            import hashlib
            expected = hashlib.sha256(sample_data).hexdigest()
            self.assertEqual(compute_file_sha256(sample_file), expected)

    def test_download_firmware_hash_validation(self) -> None:
        """Verify download_firmware validates SHA-256 and rejects hash mismatches."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest_file = Path(tmp_dir) / "test.zip"
            payload = b"MOCK_FIRMWARE_BINARY_DATA"
            import hashlib
            expected_hash = hashlib.sha256(payload).hexdigest()

            import io
            from unittest.mock import patch

            with patch("urllib.request.urlopen", side_effect=lambda req, timeout=60.0: io.BytesIO(payload)):
                # Valid hash matches
                download_firmware("http://example.com/fw.zip", dest_file, expected_sha256=expected_hash)
                self.assertTrue(dest_file.is_file())
                self.assertEqual(compute_file_sha256(dest_file), expected_hash)

                # Invalid hash raises ValueError
                with self.assertRaises(ValueError):
                    download_firmware("http://example.com/fw.zip", dest_file, expected_sha256="badhash")

    def test_acquire_firmware_existing_archive(self) -> None:
        """Verify acquire_firmware uses existing archive when SHA-256 matches."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            fw_dir = Path(tmp_dir)
            archive = fw_dir / FIRMWARE_ARCHIVE_NAME
            payload = b"MOCK_ARCHIVE_DATA"
            archive.write_bytes(payload)
            import hashlib
            valid_hash = hashlib.sha256(payload).hexdigest()

            result = acquire_firmware(fw_dir, url="http://example.com/fw.zip", expected_sha256=valid_hash)
            self.assertEqual(result, archive)

    def test_prepare_firmware_executable_payload(self) -> None:
        """Verify prepare_firmware extracts .exe when UPD_FILENAME is absent."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            zip_path = tmp_path / "driver.zip"
            exe_name = "XDJ-RX3_1.110.exe"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(exe_name, b"0" * 1_500_000)

            out_dir = tmp_path / "out"
            result = prepare_firmware(zip_path, out_dir)
            self.assertEqual(result.name, exe_name)
            self.assertTrue(result.is_file())

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
