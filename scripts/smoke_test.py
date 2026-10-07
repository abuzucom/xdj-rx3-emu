#!/usr/bin/env python3
"""Smoke test utility for XDJ-RX3 firmware preparation and bridge communication.

Supports firmware archive verification, mock bridge protocol simulation,
and client handshake validation for local and CI environments.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import logging
import os
import socket
import stat
import struct
import sys
import threading
import time
import urllib.parse
import zipfile
from collections.abc import Iterator
from pathlib import Path

DEFAULT_PORT = 4480
SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
UPD_FILENAME = "XDJRX3.UPD"
MIN_UPD_BYTES = 50_000_000
MAX_FRAME_LENGTH = 1_048_576
MAX_DOWNLOAD_BYTES = 1_073_741_824
MAX_EXTRACT_BYTES = 2_147_483_648
READ_CHUNK_SIZE = 64 * 1024
OUTPUT_FILE_MODE = 0o644
TILE_SIZE = 64
BLACK_OPAQUE_PIXEL = b"\x00\x00\x00\xff"
DEFAULT_FIRMWARE_URL = (
    "https://downloads.support.alphatheta.com/firmwares/all-in-one-dj-systems/XDJ-RX3/XDJ-RX3_v120.zip"
)
EXPECTED_FIRMWARE_SHA256 = "e81f34ef300c5faa7faf4b4c436eaaf1476d407447b2dbb845c7fbddb4f51389"
FIRMWARE_ARCHIVE_NAME = "XDJ-RX3_v120.zip"
DEFAULT_HASH_FILE = Path("firmware/firmware.sha256")


def load_expected_hash(hash_path: Path = DEFAULT_HASH_FILE) -> str:
    """Load expected SHA-256 hash from repository checksum file."""
    if hash_path.is_file():
        content = hash_path.read_text(encoding="utf-8").strip()
        tokens = content.split()
        token = tokens[0] if tokens else ""
        if len(token) == 64:
            return token.lower()
    return EXPECTED_FIRMWARE_SHA256


def write_frame(sock: socket.socket, msg_type: int, payload: bytes) -> None:
    """Send wire frame [type u8][len u32 LE][payload]."""
    hdr = struct.pack("<BI", msg_type, len(payload))
    sock.sendall(hdr + payload)


def _apply_read_deadline(sock: socket.socket, deadline: float | None) -> None:
    """Constrain the next socket read to the remaining deadline budget."""
    if deadline is None:
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("read_frame deadline exceeded")
    sock.settimeout(remaining)


def read_frame(
    sock: socket.socket,
    max_length: int = MAX_FRAME_LENGTH,
    timeout: float | None = 5.0,
) -> tuple[int, bytes] | None:
    """Read a wire frame [type u8][len u32 LE][payload].

    Returns (msg_type, payload) tuple, or None if socket closed or length invalid.
    """
    deadline = time.monotonic() + timeout if timeout is not None else None

    hdr = b""
    while len(hdr) < 5:
        _apply_read_deadline(sock, deadline)
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
        _apply_read_deadline(sock, deadline)
        chunk = sock.recv(length - len(payload))
        if not chunk:
            return None
        payload.extend(chunk)

    return msg_type, bytes(payload)


def compute_file_sha256(path: Path) -> str:
    """Calculate the SHA-256 digest of a file in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(READ_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _open_https_url(url: str, timeout: float = 60.0) -> tuple[http.client.HTTPSConnection, http.client.HTTPResponse]:
    """Open an HTTPS GET request and return the connection plus response."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Only HTTPS URLs are supported, got {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError(f"URL has no hostname: {url}")
    request_path = parsed.path or "/"
    if parsed.query:
        request_path += "?" + parsed.query
    conn = http.client.HTTPSConnection(parsed.hostname, port=parsed.port, timeout=timeout)
    conn.request("GET", request_path, headers={"User-Agent": "Mozilla/5.0"})
    response = conn.getresponse()
    if response.status != http.client.OK:
        conn.close()
        raise http.client.HTTPException(f"Unexpected HTTP status {response.status} for {url}")
    return conn, response


def download_firmware(url: str, dest_path: Path, expected_sha256: str | None = None) -> Path:
    """Download firmware archive from url to dest_path and verify SHA-256."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest_path.with_suffix(".tmp")
    digest = hashlib.sha256()
    total_bytes = 0
    success = False
    conn, resp = _open_https_url(url, timeout=60.0)
    try:
        with conn, tmp_dest.open("wb") as dst:
            while True:
                chunk = resp.read(READ_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"Download size exceeded maximum allowed limit of {MAX_DOWNLOAD_BYTES} bytes")
                digest.update(chunk)
                dst.write(chunk)

        computed_hash = digest.hexdigest()
        if expected_sha256 and computed_hash.lower() != expected_sha256.lower():
            raise ValueError(f"Hash mismatch for {url}: expected {expected_sha256}, got {computed_hash}")
        tmp_dest.replace(dest_path)
        success = True
        print(f"Downloaded and verified firmware archive: {dest_path} (SHA-256: {computed_hash})")
        return dest_path
    finally:
        if not success and tmp_dest.exists():
            tmp_dest.unlink()


def acquire_firmware(
    firmware_dir: Path,
    url: str = DEFAULT_FIRMWARE_URL,
    expected_sha256: str | None = None,
    allow_download: bool = True,
) -> Path:
    """Ensure firmware archive is present in firmware_dir with valid SHA-256 hash."""
    if expected_sha256 is None:
        expected_sha256 = load_expected_hash()

    archive_path = firmware_dir / FIRMWARE_ARCHIVE_NAME
    if not archive_path.is_file():
        existing_zip = find_firmware_zip(firmware_dir)
        if existing_zip:
            archive_path = existing_zip

    if archive_path.is_file():
        current_hash = compute_file_sha256(archive_path)
        print(f"Found firmware archive: {archive_path} (SHA-256: {current_hash})")
        if current_hash.lower() != expected_sha256.lower():
            raise ValueError(
                f"Firmware hash mismatch for {archive_path}: expected {expected_sha256}, got {current_hash}. "
                "Archive is corrupted or modified. Aborting."
            )
        return archive_path

    if not allow_download:
        raise FileNotFoundError(f"Firmware archive not found in {firmware_dir} and download is disabled.")

    print(f"Firmware not found in {firmware_dir}, downloading from {url}...")
    return download_firmware(url, firmware_dir / FIRMWARE_ARCHIVE_NAME, expected_sha256)


def find_firmware_zip(search_dir: Path) -> Path | None:
    """Locate the first firmware zip archive in search_dir."""
    if not search_dir.is_dir():
        return None
    candidates = sorted(search_dir.glob("*.zip"))
    return candidates[0] if candidates else None


def is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    """Check if a ZipInfo entry is a symbolic link."""
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode) if mode else False


def prepare_firmware(zip_path: Path, output_dir: Path) -> Path:
    """Extract and validate firmware payload from the firmware zip archive."""
    if not zip_path.is_file():
        raise FileNotFoundError(f"Firmware zip not found: {zip_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    resolved_out = output_dir.resolve()

    with zipfile.ZipFile(zip_path, "r") as zf:
        for entry in zf.infolist():
            entry_path = Path(entry.filename)
            if entry_path.is_absolute() or ".." in entry_path.parts:
                raise ValueError(f"Archive entry {entry.filename} contains invalid path traversal")
            if is_zip_symlink(entry):
                raise ValueError(f"Archive entry {entry.filename} is a symlink")

        names = zf.namelist()
        if UPD_FILENAME in names:
            payload_name = UPD_FILENAME
            min_bytes = MIN_UPD_BYTES
        else:
            exe_candidates = [n for n in names if n.endswith(".exe") and not n.startswith("__MACOSX")]
            if not exe_candidates:
                raise ValueError(f"Archive {zip_path.name} contains neither {UPD_FILENAME} nor an executable payload")
            payload_name = exe_candidates[0]
            min_bytes = 1_000_000

        clean_name = Path(payload_name).name
        info = zf.getinfo(payload_name)
        if info.file_size > MAX_EXTRACT_BYTES:
            raise ValueError(
                f"Extracted payload size {info.file_size} exceeds maximum limit of {MAX_EXTRACT_BYTES} bytes"
            )
        if info.file_size < min_bytes:
            raise ValueError(f"Extracted payload size {info.file_size} is below expected threshold")

        target_file = output_dir / clean_name
        resolved_target = target_file.resolve()
        if target_file.is_symlink() or not resolved_target.is_relative_to(resolved_out):
            raise ValueError("Archive member resolves outside output directory")

        print(f"Extracting {payload_name} ({info.file_size} bytes) to {target_file}...")
        written = 0
        extract_complete = False
        target_opened = False
        open_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            open_flags |= os.O_NOFOLLOW

        dir_fd = -1
        fd = -1
        file_owned_by_fdopen = False
        try:
            if hasattr(os, "O_DIRECTORY"):
                dir_flags = os.O_RDONLY | os.O_DIRECTORY
                if hasattr(os, "O_NOFOLLOW"):
                    dir_flags |= os.O_NOFOLLOW
                dir_fd = os.open(output_dir, dir_flags)
                fd = os.open(clean_name, open_flags, OUTPUT_FILE_MODE, dir_fd=dir_fd)
            else:
                fd = os.open(target_file, open_flags, OUTPUT_FILE_MODE)
            target_opened = True
            file_obj = os.fdopen(fd, "wb", closefd=True)
            file_owned_by_fdopen = True
            with zf.open(payload_name) as src, file_obj as dst:
                while True:
                    chunk = src.read(READ_CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_EXTRACT_BYTES:
                        raise ValueError(
                            f"Extracted payload exceeds maximum allowed limit of {MAX_EXTRACT_BYTES} bytes"
                        )
                    dst.write(chunk)
            extract_complete = True
        except Exception:
            if target_opened and not extract_complete and target_file.is_file() and not target_file.is_symlink():
                with contextlib.suppress(OSError):
                    target_file.unlink()
            raise
        finally:
            if fd != -1 and not file_owned_by_fdopen:
                os.close(fd)
            if dir_fd != -1:
                os.close(dir_fd)

    if target_file.is_symlink() or not target_file.resolve().is_relative_to(resolved_out):
        raise ValueError("Extracted target resolves outside output directory")
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

    def __enter__(self) -> MockBridgeServer:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    def start(self) -> None:
        """Start listening on the configured port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.port))
            sock.listen(1)
            self.port = sock.getsockname()[1]
            self.server_socket = sock
            self.running = True
            self.thread = threading.Thread(target=self._serve, daemon=True)
            self.thread.start()
        except Exception:
            if self.server_socket is sock:
                self.stop()
            else:
                sock.close()
            self.thread = None
            raise

    def _serve(self) -> None:
        while self.running and self.server_socket:
            try:
                self.server_socket.settimeout(1.0)
                conn, _ = self.server_socket.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                try:
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
                                tile_hdr = struct.pack("<HHHH", 0, 0, TILE_SIZE, TILE_SIZE)
                                tile_pixels = BLACK_OPAQUE_PIXEL * (TILE_SIZE * TILE_SIZE)
                                write_frame(conn, 0x11, tile_hdr + tile_pixels)
                        except (TimeoutError, OSError):
                            break
                except (OSError, ValueError) as exc:
                    logging.warning("Mock bridge client handler error: %s", exc)

    def stop(self) -> None:
        """Shut down the mock server."""
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except OSError as exc:
                logging.warning("Failed to close mock bridge socket: %s", exc)
            finally:
                self.server_socket = None
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)


@contextlib.contextmanager
def connect_bridge_socket(
    host: str,
    port: int,
    timeout: float,
) -> Iterator[socket.socket | None]:
    """Connect to bridge with retry loop until timeout expires.

    Yields the connected socket, or None if the timeout expires. The
    socket is closed when the with-block exits.
    """
    sock: socket.socket | None = None
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            sock = socket.create_connection((host, port), timeout=min(2.0, remaining))
            break
        except ConnectionRefusedError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))
        except OSError as exc:
            logging.warning("Bridge connection attempt to %s:%d failed: %s", host, port, exc)
            break
    try:
        yield sock
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError as exc:
                logging.warning("Failed to close bridge socket: %s", exc)


def _handle_handshake(sock: socket.socket, start_time: float, timeout: float) -> bool:
    """Read handshake frames from socket until status and screen info are verified."""
    received_status = False
    received_screen = False

    while time.time() - start_time < timeout:
        remaining = timeout - (time.time() - start_time)
        if remaining <= 0:
            return False
        try:
            frame = read_frame(sock, timeout=remaining)
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
        except TimeoutError:
            continue
        except OSError as err:
            print(f"Socket error during smoke test: {err}", file=sys.stderr)
            break

    return False


def test_bridge_client(host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 10.0) -> bool:
    """Connect to bridge and assert protocol handshake frames."""
    deadline = time.time() + timeout
    with connect_bridge_socket(host, port, timeout) as sock:
        if sock is None:
            print(f"Failed to connect to bridge at {host}:{port} within {timeout}s", file=sys.stderr)
            return False
        return _handle_handshake(sock, time.time(), deadline - time.time())


def main() -> int:
    """Parse CLI arguments and execute the requested smoke test action."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=["prepare", "client", "mock-server", "e2e"], default="e2e")
    parser.add_argument("--zip", type=Path, default=None, help="Path to firmware zip archive")
    parser.add_argument("--firmware-dir", type=Path, default=Path("firmware"), help="Directory for firmware archives")
    parser.add_argument("--url", default=DEFAULT_FIRMWARE_URL, help="Firmware download URL")
    parser.add_argument(
        "--expected-hash",
        default=None,
        help="Expected SHA-256 hash (loads from firmware/firmware.sha256 if omitted)",
    )
    parser.add_argument("--download", action="store_true", help="Download firmware archive if missing")
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
                allow_download=args.download,
            )
        prepare_firmware(zip_file, args.out_dir)
        return 0

    if args.action == "mock-server":
        with MockBridgeServer(port=args.port) as server:
            print(f"Mock bridge server listening on {server.port}. Press Ctrl+C to stop.")
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                raise SystemExit(130)
        return 0

    if args.action == "client":
        success = test_bridge_client(host=args.host, port=args.port, timeout=args.timeout)
        return 0 if success else 1

    if args.action == "e2e":
        zip_file = args.zip
        if not zip_file and args.download:
            zip_file = acquire_firmware(
                args.firmware_dir,
                url=args.url,
                expected_sha256=args.expected_hash,
                allow_download=True,
            )
        elif not zip_file:
            zip_file = find_firmware_zip(args.firmware_dir)
            if zip_file:
                expected = args.expected_hash or load_expected_hash()
                cur_hash = compute_file_sha256(zip_file)
                if cur_hash.lower() != expected.lower():
                    raise ValueError(
                        f"Firmware hash mismatch for {zip_file}: expected {expected}, got {cur_hash}. "
                        "Archive is corrupted or modified. Aborting."
                    )

        if zip_file:
            print(f"Using firmware archive: {zip_file}")
            prepare_firmware(zip_file, args.out_dir)

        # Run mock server and client handshake validation
        with MockBridgeServer(port=args.port):
            success = test_bridge_client(host=args.host, port=args.port, timeout=args.timeout)
            return 0 if success else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
