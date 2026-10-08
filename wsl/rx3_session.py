"""Own RX3 child process groups until console EOF, a signal, or child exit."""

import argparse
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

# Poll child and console state ten times per second.
POLL_SECONDS = 0.1
# Allow graceful shutdown for two seconds before killing child groups.
STOP_SECONDS = 2.0
PROBE_TIMEOUT_SECONDS = 30.0


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
            child = subprocess.Popen(
                command,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT if log is not None else None,
            )
            try:
                group_id = os.getpgid(child.pid)
            except ProcessLookupError as error:
                status = child.poll()
                if status is not None:
                    raise RuntimeError(
                        f"RX3 child exited with status {status} before process-group verification."
                    ) from error
                _terminate_untracked_child(child)
                raise RuntimeError("Could not inspect the RX3 child's process group.") from error
            except OSError as error:
                _terminate_untracked_child(child)
                raise RuntimeError(
                    f"Could not inspect the RX3 child's process group (error {error.errno})."
                ) from error
            if group_id != child.pid:
                _terminate_untracked_child(child)
                raise RuntimeError("RX3 child did not start in its own process group")
            children.append(child)
        print("SESSION READY", flush=True)
        while not stopped and time.monotonic() < deadline:
            if watch_stdin:
                readable, _, _ = select.select([sys.stdin], [], [], POLL_SECONDS)
                if readable and not os.read(sys.stdin.fileno(), 1):
                    return 0
            for child in children:
                status = child.poll()
                if status is not None:
                    print(f"RX3 child {child.pid} exited with status {status}.", flush=True)
                    return status
            if not watch_stdin:
                time.sleep(POLL_SECONDS)
        return 0
    finally:
        # Keep the shutdown handler active until every child has been reaped.
        stop_children(children)
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        print("OWNED CHILDREN REAPED", flush=True)


def _terminate_untracked_child(child: subprocess.Popen) -> None:
    """Stop and reap one child before process-group ownership is verified."""
    try:
        child.kill()
    except ProcessLookupError:
        pass
    child.wait()


def main() -> int:
    """Run the shell session or a bounded child-cleanup diagnostic."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    if args.probe:
        return supervise(
            [[sys.executable, "-c", "import signal; signal.pause()"]],
            True,
            PROBE_TIMEOUT_SECONDS,
        )
    import fcntl
    from rx3_guard import DEFAULT_BRIDGE_PORT, check_idle, parse_port

    base = Path.home() / "rx3"
    if not base.is_dir():
        raise RuntimeError("RX3 runtime is missing. Run rx3.cmd to bootstrap it first.")
    with (base / "session.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("ERROR: An RX3 session already owns the runtime. Close that window before retrying.")
            return 1
        port = check_idle(parse_port(os.environ.get("RX3_PORT", str(DEFAULT_BRIDGE_PORT))))
        os.environ["RX3_PORT"] = str(port)
        command = ["bash", str(base / "run-rx3.sh"), "--owned-session"]
        return supervise([command], os.environ.get("RX3_WATCH_STDIN") == "1")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
