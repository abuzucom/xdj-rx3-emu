#!/usr/bin/env python3
"""Command-line controller client for the XDJ-RX3 emulator bridge."""

from __future__ import annotations

import argparse

from controllers.backends.midi import MidiBackend
from controllers.bridge_client import BridgeClient
from controllers.profiles import load_profile


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the selected controller backend."""
    parser = argparse.ArgumentParser(description="Forward MIDI/HID controller input to the XDJ-RX3 bridge.")
    parser.add_argument("--backend", choices=["midi"], default="midi", help="controller backend to use")
    parser.add_argument("--bridge-host", default="127.0.0.1", help="emulator bridge host")
    parser.add_argument("--bridge-port", type=int, default=4480, help="emulator bridge port")
    parser.add_argument(
        "--profile",
        default=None,
        help="JSON profile path inside controllers/profiles",
    )
    parser.add_argument("--midi-port", default=None, help="MIDI input port name (default: first available)")
    args = parser.parse_args(argv)

    profile = load_profile(args.profile)
    if args.backend == "midi":
        backend: MidiBackend = MidiBackend(profile, port_name=args.midi_port)
    else:
        raise ValueError(f"Unsupported backend: {args.backend}")

    client = BridgeClient(backend, args.bridge_host, args.bridge_port)
    try:
        client.run()
    except KeyboardInterrupt:
        return 0
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
