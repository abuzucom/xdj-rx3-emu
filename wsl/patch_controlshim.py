"""Adapt the imported native ARM LED scanner for QEMU user-mode emulation."""

import argparse
from pathlib import Path

HOST_MAP_LITERAL = '"/hostproc/self/maps"'
GUEST_MAP_LITERAL = '"/proc/self/maps"'


def adapt_control_source(source: str) -> str:
    """Require the known source shape before selecting guest address ranges."""
    if source.count(HOST_MAP_LITERAL) != 1:
        raise ValueError("Expected one host-map path in the control shim. Review the imported source.")
    # QEMU translates this exact proc path into guest address ranges.
    return source.replace(HOST_MAP_LITERAL, GUEST_MAP_LITERAL)


def main() -> None:
    """Write the adapted build artifact without changing the imported source."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        raise ValueError("Control shim output must differ from the imported source")
    source = args.source.read_text(encoding="utf-8")
    args.output.write_text(adapt_control_source(source), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
