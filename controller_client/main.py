#!/usr/bin/env python3
"""Command-line controller client for the XDJ-RX3 emulator bridge."""

from __future__ import annotations

import argparse

from controllers.backends.midi import MidiBackend
from controllers.bridge_client import BridgeClient
from controllers.profiles import load_profile
from controllers.protocol import DEFAULT_BRIDGE_PORT

DEFAULT_BRIDGE_HOST = "127.0.0.1"
# Local loopback keeps controller events on the local emulator by default.


def parse_controller_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse controller options while keeping the direct CLI headless by default."""
    parser = argparse.ArgumentParser(description="Forward MIDI/HID controller input to the XDJ-RX3 bridge.")
    parser.add_argument("--backend", choices=["midi"], default="midi", help="controller backend to use")
    parser.add_argument("--bridge-host", default=DEFAULT_BRIDGE_HOST, help="emulator bridge host")
    parser.add_argument("--bridge-port", type=int, default=DEFAULT_BRIDGE_PORT, help="emulator bridge port")
    parser.add_argument("--view", action="store_true", help="show the live RX3 firmware screen")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run without the visual screen window",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="JSON profile path inside controllers/profiles",
    )
    parser.add_argument("--midi-port", default=None, help="MIDI input port name (default: first available)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the selected controller backend."""
    args = parse_controller_arguments(argv)
    show_view = args.view and not args.headless
    viewer_runner = None
    if show_view:
        try:
            from controller_client.viewer import run_viewer
        except ModuleNotFoundError as exc:
            if exc.name == "PIL":
                raise RuntimeError(
                    "Visual mode requires Pillow. Install it with: "
                    "python -m pip install --require-hashes -r requirements-viewer.txt"
                ) from exc
            if exc.name not in {"tkinter", "_tkinter"}:
                raise
            raise RuntimeError(
                "Visual mode requires Python with Tcl/Tk support. Install a Tk-enabled Python build."
            ) from exc
        viewer_runner = run_viewer

    profile = load_profile(args.profile)
    if args.backend == "midi":
        backend: MidiBackend = MidiBackend(profile, port_name=args.midi_port)
    else:
        raise ValueError(f"Unsupported backend: {args.backend}")

    from controller_client.audio import create_default_audio_sink

    audio_sink = create_default_audio_sink()
    if viewer_runner is not None:
        # The viewer owns the BridgeClient through window shutdown.
        if audio_sink is not None:
            return viewer_runner(
                backend,
                args.bridge_host,
                args.bridge_port,
                audio_sink=audio_sink,
            )
        return viewer_runner(backend, args.bridge_host, args.bridge_port)

    if audio_sink is None:
        client = BridgeClient(backend, args.bridge_host, args.bridge_port)
    else:
        client = BridgeClient(
            backend,
            args.bridge_host,
            args.bridge_port,
            audio_sink=audio_sink,
        )
    try:
        client.run()
    except KeyboardInterrupt:
        return 0
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
