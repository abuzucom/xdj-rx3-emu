"""Keep a pipe open only while the RX3 command window remains alive."""

import subprocess
import sys


def main() -> int:
    """Console termination closes the pipe and signals cleanup inside WSL."""
    command = sys.argv[1:]
    if not command or command[0].lower() != "wsl.exe":
        raise ValueError("Session requires a WSL command")
    command = [command[0], "env", "RX3_WATCH_STDIN=1", *command[1:]]
    child = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        result = child.wait()
    except KeyboardInterrupt:
        child.stdin.close()
        result = child.wait(timeout=10)
    finally:
        child.stdin.close()
    if result:
        print(f"RX3 stopped with exit code {result}. Check the output above and ~/rx3/player.log.")
        input("Press Enter to close.")
    return result


if __name__ == "__main__":
    sys.exit(main())
