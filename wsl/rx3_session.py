"""Own RX3 child process groups until console EOF, a signal, or child exit."""

import argparse
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

POLL_SECONDS = 0.1
STOP_SECONDS = 2.0


def stop_children(children: list[subprocess.Popen]) -> None:
    """Terminate only groups created by this supervisor and reap direct children."""
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.monotonic() + STOP_SECONDS
    while time.monotonic() < deadline and any(child.poll() is None for child in children):
        time.sleep(POLL_SECONDS)
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
    for child in children:
        child.wait()


def supervise(commands: list[list[str]], watch_stdin: bool, timeout: float = 0, log=None) -> int:
    """Keep children in owned groups and treat console pipe EOF as shutdown."""
    children = []
    stopped = []
    handlers = {}
    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        handlers[signum] = signal.signal(signum, lambda *_args: stopped.append(True))
    deadline = time.monotonic() + timeout if timeout else float("inf")
    try:
        for command in commands:
            children.append(
                subprocess.Popen(
                    command,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT if log is not None else None,
                )
            )
        print("SESSION READY", flush=True)
        while not stopped and time.monotonic() < deadline:
            for child in children:
                status = child.poll()
                if status is not None:
                    print(f"RX3 child {child.pid} exited with status {status}.", flush=True)
                    return status or 1
            if watch_stdin:
                readable, _, _ = select.select([sys.stdin], [], [], POLL_SECONDS)
                if readable and not os.read(sys.stdin.fileno(), 1):
                    return 0
            else:
                time.sleep(POLL_SECONDS)
        return 0
    finally:
        stop_children(children)
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        print("OWNED CHILDREN REAPED", flush=True)


def main() -> int:
    """Run the shell session or a bounded child-cleanup diagnostic."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    if args.probe:
        return supervise([[sys.executable, "-c", "import time; time.sleep(30)"]], True, 30)
    import fcntl
    from rx3_guard import check_idle

    base = Path.home() / "rx3"
    with (base / "session.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("ERROR: An RX3 session already owns the runtime. Close that window before retrying.")
            return 1
        check_idle(int(os.environ.get("RX3_PORT", "4480")))
        command = ["bash", str(base / "run-rx3.sh"), "--owned-session"]
        return supervise([command], os.environ.get("RX3_WATCH_STDIN") == "1")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
