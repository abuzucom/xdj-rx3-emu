#!/usr/bin/env python3
"""Smoke test utility for XDJ-RX3 firmware preparation and bridge communication.

Supports firmware archive verification, mock bridge protocol simulation,
and client handshake validation for local and CI environments.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import socket
import struct
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_PORT = 4480
SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
UPD_FILENAME = "XDJRX3.UPD"
MIN_UPD_BYTES = 50_000_000
MAX_FRAME_LENGTH = 1_048_576
DEFAULT_FIRMWARE_URL = (
    "https://downloads.support.alphatheta.com/drivers/all-in-one-dj-systems/XDJ-RX3/XDJRX31110exe.zip"
)
EXPECTED_FIRMWARE_SHA256 = (
    "3db66f95199b22aa3115decf0ed03549761ca6f29a4cf113fa583c6da891c4e0"
)
FIRMWARE_ARCHIVE_NAME = "XDJRX31110exe.zip"


def write_frame(sock: socket.socket, msg_type: int, payload: bytes) -> None:
    """Send wire frame [type u8][len u32 LE][payload]."""
    hdr = struct.pack("<BI", msg_type, len(payload))
    sock.sendall(hdr + payload)


def read_frame(sock: socket.socket, max_length: int = MAX_FRAME_LENGTH) -> tuple[int, bytes] | None:
    """Read a wire frame [type u8][len u32 LE][payload].

    Returns (msg_type, payload) tuple, or None if socket closed or length invalid.
    """
    hdr = b""
    while len(hdr) < 5:
        chunk = sock.recv(5 - len(hdr))
        if not chunk:
            return None
        hdr += chunk

    msg_type, length = struct.unpack("<BI", hdr)
    if length > max_length:
        logging.warning("Frame length %d exceeds maximum allowed %d", length, max_length)
        return None

    payload = bytearray()
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            return None
        payload.extend(chunk)

    return msg_type, bytes(payload)


def compute_file_sha256(path: Path) -> str:
    """Calculate the SHA-256 digest of a file in 64 KiB chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def download_firmware(url: str, dest_path: Path, expected_sha256: str | None = None) -> Path:
    """Download firmware archive from url to dest_path and verify SHA-256."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest_path.with_suffix(".tmp")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=60.0) as resp, open(tmp_dest, "wb") as dst:
            shutil.copyfileobj(resp, dst)
        computed_hash = compute_file_sha256(tmp_dest)
        if expected_sha256 and computed_hash.lower() != expected_sha256.lower():
            raise ValueError(
                f"Hash mismatch for {url}: expected {expected_sha256}, got {computed_hash}"
            )
        tmp_dest.replace(dest_path)
        print(f"Downloaded and verified firmware archive: {dest_path} (SHA-256: {computed_hash})")
        return dest_path
    finally:
        if tmp_dest.exists():
            tmp_dest.unlink()


def acquire_firmware(
    firmware_dir: Path,
    url: str = DEFAULT_FIRMWARE_URL,
    expected_sha256: str = EXPECTED_FIRMWARE_SHA256,
) -> Path:
    """Ensure firmware archive is present in firmware_dir with valid SHA-256 hash."""
    archive_path = firmware_dir / FIRMWARE_ARCHIVE_NAME
    if not archive_path.is_file():
        existing_zip = find_firmware_zip(firmware_dir)
        if existing_zip:
            archive_path = existing_zip
    if archive_path.is_file():
        current_hash = compute_file_sha256(archive_path)
        print(f"Found firmware archive: {archive_path} (SHA-256: {current_hash})")
        if expected_sha256 and current_hash.lower() == expected_sha256.lower():
            return archive_path
        if not expected_sha256:
            return archive_path
        print(f"Archive hash mismatch, re-downloading to {firmware_dir / FIRMWARE_ARCHIVE_NAME}...")

    return download_firmware(url, firmware_dir / FIRMWARE_ARCHIVE_NAME, expected_sha256)


def find_firmware_zip(search_dir: Path) -> Path | None:
    """Locate the first firmware zip archive in search_dir."""
    if not search_dir.is_dir():
        return None
    candidates = sorted(search_dir.glob("*.zip"))
    return candidates[0] if candidates else None


def prepare_firmware(zip_path: Path, output_dir: Path) -> Path:
    """Extract and validate firmware payload from the firmware zip archive."""
    if not zip_path.is_file():
        raise FileNotFoundError(f"Firmware zip not found: {zip_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    resolved_out = output_dir.resolve()

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if UPD_FILENAME in names:
            payload_name = UPD_FILENAME
            min_bytes = MIN_UPD_BYTES
        else:
            exe_candidates = [n for n in names if n.endswith(".exe") and not n.startswith("__MACOSX")]
            if not exe_candidates:
                raise ValueError(
                    f"Archive {zip_path.name} contains neither {UPD_FILENAME} nor an executable payload"
                )
            payload_name = exe_candidates[0]
            min_bytes = 1_000_000

        target_file = output_dir / payload_name
        resolved_target = target_file.resolve()
        if target_file.is_symlink() or not resolved_target.is_relative_to(resolved_out):
            raise ValueError("Archive member resolves outside output directory")

        info = zf.getinfo(payload_name)
        if info.file_size < min_bytes:
            raise ValueError(f"Extracted payload size {info.file_size} is below expected threshold")

        print(f"Extracting {payload_name} ({info.file_size} bytes) to {target_file}...")
        with zf.open(payload_name) as src, open(target_file, "wb") as dst:
            shutil.copyfileobj(src, dst)

    if not target_file.is_file() or target_file.stat().st_size < min_bytes:
        raise RuntimeError(f"Extracted payload verification failed at {target_file}")

    print(f"Firmware extracted and verified: {target_file} ({target_file.stat().st_size} bytes)")
    return target_file


class MockBridgeServer:
    """Mock TCP bridge server implementing the XDJ-RX3 wire protocol."""

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self.running = False
        self.server_socket: socket.socket | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        """Start listening on the configured port."""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("127.0.0.1", self.port))
        self.server_socket.listen(1)
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while self.running and self.server_socket:
            try:
                self.server_socket.settimeout(1.0)
                conn, _ = self.server_socket.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                # Send 0x01 status text
                write_frame(conn, 0x01, b"bridge connected")
                # Send 0x10 screen info (1280x800)
                screen_payload = struct.pack("<HH", SCREEN_WIDTH, SCREEN_HEIGHT)
                write_frame(conn, 0x10, screen_payload)
                # Echo loop for client commands
                conn.settimeout(2.0)
                while self.running:
                    try:
                        frame = read_frame(conn)
                        if frame is None:
                            break
                        msg_type, _ = frame
                        if msg_type == 0x21:  # full frame request
                            tile_hdr = struct.pack("<HHHH", 0, 0, 64, 64)
                            tile_pixels = b"\x00\x00\x00\xff" * (64 * 64)
                            write_frame(conn, 0x11, tile_hdr + tile_pixels)
                    except (socket.timeout, OSError):
                        break

    def stop(self) -> None:
        """Shut down the mock server."""
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except OSError as exc:
                logging.warning("Failed to close mock bridge socket: %s", exc)
            self.server_socket = None


def connect_bridge_socket(host: str, port: int, timeout: float) -> socket.socket | None:
    """Connect to bridge with retry loop until timeout expires."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            return socket.create_connection((host, port), timeout=2.0)
        except OSError:
            time.sleep(0.5)
    return None


def _handle_handshake(sock: socket.socket, start_time: float, timeout: float) -> bool:
    """Read handshake frames from socket until status and screen info are verified."""
    received_status = False
    received_screen = False

    while time.time() - start_time < timeout:
        try:
            frame = read_frame(sock)
            if frame is None:
                break
            msg_type, payload = frame

            if msg_type == 0x01:
                print(f"Received status frame (0x01): {payload.decode(errors='replace')}")
                received_status = True
            elif msg_type == 0x10:
                if len(payload) < 4:
                    logging.warning("Screen info frame too short: %d bytes", len(payload))
                    break
                dims = struct.unpack("<HH", payload[:4])
                print(f"Received screen info (0x10): {dims[0]}x{dims[1]}")
                if dims == (SCREEN_WIDTH, SCREEN_HEIGHT):
                    received_screen = True
                write_frame(sock, 0x21, b"")

            if received_status and received_screen:
                print("Smoke test protocol verification successful.")
                return True
        except socket.timeout:
            continue
        except OSError as err:
            print(f"Socket error during smoke test: {err}", file=sys.stderr)
            break

    return False


def test_bridge_client(host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 10.0) -> bool:
    """Connect to bridge and assert protocol handshake frames."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(2.0)
                sock.connect((host, port))
                return _handle_handshake(sock, start_time, timeout)
        except OSError:
            time.sleep(0.5)

    print(f"Failed to connect to bridge at {host}:{port} within {timeout}s", file=sys.stderr)
    return False


def main() -> int:
    """Parse CLI arguments and execute the requested smoke test action."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=["prepare", "client", "mock-server", "e2e"], default="e2e")
    parser.add_argument("--zip", type=Path, default=None, help="Path to firmware zip archive")
    parser.add_argument("--firmware-dir", type=Path, default=Path("firmware"), help="Directory for firmware archives")
    parser.add_argument("--url", default=DEFAULT_FIRMWARE_URL, help="Firmware download URL")
    parser.add_argument("--expected-hash", default=EXPECTED_FIRMWARE_SHA256, help="Expected SHA-256 hash")
    parser.add_argument("--out-dir", type=Path, default=Path("firmware/extracted"), help="Extraction directory")
    parser.add_argument("--host", default="127.0.0.1", help="Bridge host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bridge port")
    parser.add_argument("--timeout", type=float, default=15.0, help="Operation timeout in seconds")

    args = parser.parse_args()

    if args.action == "prepare":
        if args.zip:
            zip_file = args.zip
        else:
            zip_file = acquire_firmware(
                args.firmware_dir,
                url=args.url,
                expected_sha256=args.expected_hash,
            )
        prepare_firmware(zip_file, args.out_dir)
        return 0

    if args.action == "mock-server":
        server = MockBridgeServer(port=args.port)
        server.start()
        print(f"Mock bridge server listening on {args.port}. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            server.stop()
        return 0

    if args.action == "client":
        success = test_bridge_client(host=args.host, port=args.port, timeout=args.timeout)
        return 0 if success else 1

    if args.action == "e2e":
        if args.zip:
            zip_file = args.zip
        else:
            zip_file = acquire_firmware(
                args.firmware_dir,
                url=args.url,
                expected_sha256=args.expected_hash,
            )
        print(f"Using firmware archive: {zip_file}")
        prepare_firmware(zip_file, args.out_dir)

        # Run mock server and client handshake validation
        server = MockBridgeServer(port=args.port)
        server.start()
        try:
            success = test_bridge_client(host=args.host, port=args.port, timeout=args.timeout)
            return 0 if success else 1
        finally:
            server.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
