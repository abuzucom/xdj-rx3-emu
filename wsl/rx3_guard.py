"""Reject concurrent RX3 sessions before startup changes runtime files."""

import os
from pathlib import Path
import socket
import sys

PROCESS_NAMES = {"rx3_bridge.py", "rbp-pi"}
MIN_USER_PORT = 1024
MAX_USER_PORT = 65535
DEFAULT_BRIDGE_PORT = 4480


def find_processes(proc: Path = Path("/proc")) -> list[int]:
    """Find RX3 player or bridge processes without terminating any process."""
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            args = entry.joinpath("cmdline").read_bytes().split(b"\0")
            process_name = entry.joinpath("comm").read_bytes().decode(errors="replace").strip()
        except (FileNotFoundError, ProcessLookupError):
            continue
        names = {Path(os.fsdecode(arg)).name for arg in args if arg}
        names.add(process_name)
        if names & PROCESS_NAMES:
            found.append(int(entry.name))
    return sorted(found)


def validate_port(port: int) -> int:
    """Require an integer in the user port range."""
    if isinstance(port, bool) or not isinstance(port, int) or not MIN_USER_PORT <= port <= MAX_USER_PORT:
        raise ValueError(f"RX3_PORT must be an integer from {MIN_USER_PORT} to {MAX_USER_PORT}.")
    return port


def parse_port(value: str) -> int:
    """Parse an ASCII decimal port and enforce the user port range."""
    if not value.isascii() or not value.isdecimal():
        raise ValueError(f"RX3_PORT must be an integer from {MIN_USER_PORT} to {MAX_USER_PORT}.")
    return validate_port(int(value))


def check_idle(port: int = DEFAULT_BRIDGE_PORT, proc: Path = Path("/proc")) -> int:
    """Fail closed on stale processes, unreadable state, or an occupied port."""
    port = validate_port(port)
    processes = find_processes(proc)
    if processes:
        identifiers = ", ".join(map(str, processes))
        raise RuntimeError(
            f"RX3 processes already exist (PIDs {identifiers}). Close the existing session before retrying."
        )
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("", port))
        except OSError as error:
            raise RuntimeError(
                f"RX3 bridge port {port} is occupied. Close the existing bridge before retrying."
            ) from error
    return port


if __name__ == "__main__":
    try:
        check_idle(parse_port(os.environ.get("RX3_PORT", str(DEFAULT_BRIDGE_PORT))))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
