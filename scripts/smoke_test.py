#!/usr/bin/env python3
"""Smoke test utility for XDJ-RX3 firmware preparation and bridge communication.

Supports firmware archive verification, mock bridge protocol simulation,
and client handshake validation for local and CI environments.
"""

from __future__ import annotations

import argparse
import os
import socket
import struct
import sys
import threading
import time
import zipfile
from pathlib import Path

DEFAULT_PORT = 4480
SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
UPD_FILENAME = "XDJRX3.UPD"
MIN_UPD_BYTES = 50_000_000


def find_firmware_zip(search_dir: Path) -> Path | None:
    """Locate the first firmware zip archive in search_dir."""
    if not search_dir.is_dir():
        return None
    candidates = sorted(search_dir.glob("*.zip"))
    return candidates[0] if candidates else None


def prepare_firmware(zip_path: Path, output_dir: Path) -> Path:
    """Extract and validate XDJRX3.UPD from the firmware zip archive."""
    if not zip_path.is_file():
        raise FileNotFoundError(f"Firmware zip not found: {zip_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    target_upd = output_dir / UPD_FILENAME

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if UPD_FILENAME not in names:
            raise ValueError(f"Archive {zip_path.name} does not contain {UPD_FILENAME}")

        info = zf.getinfo(UPD_FILENAME)
        if info.file_size < MIN_UPD_BYTES:
            raise ValueError(f"Extracted UPD file size {info.file_size} is below expected threshold")

        print(f"Extracting {UPD_FILENAME} ({info.file_size} bytes) to {target_upd}...")
        zf.extract(UPD_FILENAME, path=output_dir)

    if not target_upd.is_file() or target_upd.stat().st_size < MIN_UPD_BYTES:
        raise RuntimeError(f"Extracted payload verification failed at {target_upd}")

    print(f"Firmware extracted and verified: {target_upd} ({target_upd.stat().st_size} bytes)")
    return target_upd


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
                status_payload = b"bridge connected"
                conn.sendall(struct.pack("<BI", 0x01, len(status_payload)) + status_payload)
                # Send 0x10 screen info (1280x800)
                screen_payload = struct.pack("<HH", SCREEN_WIDTH, SCREEN_HEIGHT)
                conn.sendall(struct.pack("<BI", 0x10, len(screen_payload)) + screen_payload)
                # Echo loop for client commands
                conn.settimeout(2.0)
                while self.running:
                    try:
                        hdr = conn.recv(5)
                        if not hdr or len(hdr) < 5:
                            break
                        msg_type, length = struct.unpack("<BI", hdr)
                        payload = conn.recv(length) if length > 0 else b""
                        if msg_type == 0x21:  # full frame request
                            tile_hdr = struct.pack("<HHHH", 0, 0, 64, 64)
                            tile_pixels = b"\x00\x00\x00\xff" * (64 * 64)
                            conn.sendall(struct.pack("<BI", 0x11, len(tile_hdr) + len(tile_pixels)) + tile_hdr + tile_pixels)
                    except (socket.timeout, OSError):
                        break

    def stop(self) -> None:
        """Shut down the mock server."""
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except OSError:
                pass
            self.server_socket = None


def test_bridge_client(host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 10.0) -> bool:
    """Connect to bridge and assert protocol handshake frames."""
    start_time = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)

    connected = False
    while time.time() - start_time < timeout:
        try:
            s.connect((host, port))
            connected = True
            break
        except OSError:
            time.sleep(0.5)

    if not connected:
        print(f"Failed to connect to bridge at {host}:{port} within {timeout}s", file=sys.stderr)
        return False

    with s:
        received_status = False
        received_screen = False

        while time.time() - start_time < timeout:
            try:
                hdr = s.recv(5)
                if not hdr or len(hdr) < 5:
                    break
                msg_type, length = struct.unpack("<BI", hdr)
                payload = b""
                while len(payload) < length:
                    chunk = s.recv(length - len(payload))
                    if not chunk:
                        break
                    payload += chunk

                if msg_type == 0x01:
                    print(f"Received status frame (0x01): {payload.decode(errors='replace')}")
                    received_status = True
                elif msg_type == 0x10:
                    width, height = struct.unpack("<HH", payload[:4])
                    print(f"Received screen info (0x10): {width}x{height}")
                    if width == SCREEN_WIDTH and height == SCREEN_HEIGHT:
                        received_screen = True
                    # Request full frame to verify bidirectional communication
                    s.sendall(struct.pack("<BI", 0x21, 0))

                if received_status and received_screen:
                    print("Smoke test protocol verification successful.")
                    return True
            except socket.timeout:
                continue
            except OSError as err:
                print(f"Socket error during smoke test: {err}", file=sys.stderr)
                break

    return False


def main() -> int:
    """Parse CLI arguments and execute the requested smoke test action."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=["prepare", "client", "mock-server", "e2e"], default="e2e")
    parser.add_argument("--zip", type=Path, default=None, help="Path to firmware zip archive")
    parser.add_argument("--out-dir", type=Path, default=Path("firmware/extracted"), help="Extraction directory")
    parser.add_argument("--host", default="127.0.0.1", help="Bridge host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bridge port")
    parser.add_argument("--timeout", type=float, default=15.0, help="Operation timeout in seconds")

    args = parser.parse_args()

    if args.action == "prepare":
        zip_file = args.zip or find_firmware_zip(Path("firmware"))
        if not zip_file:
            print("No firmware zip found in firmware/ directory.", file=sys.stderr)
            return 1
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
        # Check firmware archive if present
        zip_file = args.zip or find_firmware_zip(Path("firmware"))
        if zip_file:
            print(f"Found firmware archive: {zip_file}")
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
