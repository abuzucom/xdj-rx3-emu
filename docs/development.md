# Development

This repository runs the Pioneer XDJ-RX3 player firmware under WSL with proot
and qemu-arm. It holds the harness, the shims, and the bridge. It holds no
Pioneer software.

## Commands

Obtain consent before tests, scripts, or Makefile targets.

- Fetch inputs: `wsl/rx3_fetch.sh`. It downloads proot, an armv7 toolchain,
  the public firmware update, and the GPL source drop into `~/rx3`.
- Build the chroot and shims: `wsl/build-rootfs-wsl.sh`.
- Seed the virtual USB stick: `wsl/make-usb.sh`.
- Run the player and bridge: `wsl ~/rx3/run-rx3.sh`. Set `RX3_NOBRIDGE=1` for
  the player only. Set `RX3_TIMEOUT=N` to stop after N seconds.
- End-to-end check: `wsl/test-e2e.sh`. It needs WSL and the recovered
  firmware. CI cannot run it.
- Smoke test utility: `python scripts/smoke_test.py --action e2e --download`.
  It downloads and verifies the firmware archive, extracts the payload, and
  validates the bridge protocol against a mock server.
- Windows bootstrap: `rx3.cmd` or `python windows/rx3_windows.py`. It stages
  the WSL harness, fetches the pinned rx3-handoff tooling, builds the chroot,
  and launches the emulator. See `docs/running.md` for the operator guide.
- Ruff install: `python -m pip install --require-hashes -r
  requirements-ruff.txt`.
- Ruff block tier: `ruff check --config ruff.toml --ignore-noqa
  --no-respect-gitignore`.
- Ruff format: `ruff format --config ruff.toml --check`.
- Ruff warn tier: `ruff check --config ruff.warn.toml --ignore-noqa
  --exit-zero`.
- Policy copies: `python scripts/sync.py --check`.
- Policy tests: `python scripts/run_tests.py`.
- Prose lint: `make lint PYTHON=python3`.

## Do not touch

- Never commit firmware, keys, images, or extracted trees. `.gitignore` lists
  `*.zip`, `*.bin`, `*.img`, `*.raw`, `*.key`, `rootfs/`, `rx3-handoff/`,
  `extracted/`, `deb/`, `tc-armel/`, and `proot`.
- Never commit to `master`. It mirrors upstream only. Merge `master` into
  `main` through a pull request.
- `docs/screenshots/` holds captured images. Change a screenshot only on request.
- Template copies from `abuzucom/agents` under `hooks/`, `tests/`, `tools/`,
  and `scripts/` stay byte-identical. `shared-files.json` records the shared
  digests. Record any local difference in `docs/template-drift.md`.

## Architecture

- `wsl/rx3_fetch.sh` and `wsl/build-rootfs-wsl.sh` assemble the chroot in
  `~/rx3/rootfs`.
- `wsl/wsl-shim.c` provides real-time scheduling and paced audio capture.
- `wsl/g2d-shim.c` implements the G2D blitter calls in software.
- `wsl/patch_fbshim.py` derives the framebuffer shim.
  `wsl/asound-wsl.conf` routes ALSA to a null device.
- `wsl/rx3_bridge.py` is the entry point for clients. It listens on TCP port
  4480. Frames are `[type u8][len u32 LE][payload]`. The README lists the
  frame types. The frame layout is a public contract.
- `wsl/usb_bridge_patch.py` adds virtual USB stick handling to the bridge.

## Gotchas

- The harness targets WSL on Windows. It runs without root.
- The firmware outputs S24_LE, 6 channels, 44.1 kHz. The capture converts it
  to S16 stereo master and cue files.
- `wsl/test-e2e.sh` reads the bridge from a fixed Windows path. Adjust it
  locally before running.
