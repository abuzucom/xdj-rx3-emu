"""Reject concurrent RX3 sessions before startup changes runtime files."""

import os
from pathlib import Path
import socket
import sys

PROCESS_NAMES = {"rx3_bridge.py", "rbp-pi"}


def find_processes(proc: Path = Path("/proc")) -> list[int]:
    """Find RX3 player or bridge processes without terminating any process."""
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            args = entry.joinpath("cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError):
            continue
        names = {Path(os.fsdecode(arg)).name for arg in args if arg}
        if names & PROCESS_NAMES:
            found.append(int(entry.name))
    return sorted(found)


def check_idle(port: int = 4480, proc: Path = Path("/proc")) -> None:
    """Fail closed on stale processes, unreadable state, or an occupied port."""
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


if __name__ == "__main__":
    try:
        check_idle(int(os.environ.get("RX3_PORT", "4480")))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
