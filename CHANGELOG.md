# Changelog

This file documents every notable project change on `main`.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
One deviation applies. A version heading parenthesizes the release date.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] (2026-10-03)

### Added
- `scripts/smoke_test.py` validates firmware zip archives, simulates the bridge
  protocol via a mock TCP server, and verifies client handshakes.
- Firmware download and SHA-256 integrity verification against
  `firmware/firmware.sha256` in `scripts/smoke_test.py`.
- Support for official AlphaTheta firmware package `XDJ-RX3_v120.zip` and `XDJRX3.UPD` payload.
- CLI flags `--firmware-dir`, `--url`, `--expected-hash`, and `--download`.
- `.github/workflows/smoketest.yml` runs automated smoke tests in CI with firmware caching.
- `.github/workflows/seed-firmware-cache.yml` adds manual dispatch to seed workflow storage cache.
- `tests/test_smoke_test.py` covers archive discovery, size assertions, wire framing,
  hash calculation, and download validation.
- Tracking of `firmware/firmware.sha256` while ignoring firmware binaries in `.gitignore`.

### Fixed
- Removed auto-download from CI in `.github/workflows/smoketest.yml` and required workflow storage cache.
- Allowed `smoketest.yml` to proceed when the firmware cache is absent so pull-request checks do not require a pre-seeded cache.
- Switched firmware download from `urllib.request` to `http.client.HTTPSConnection` and replaced builtin `open` with `Path.open()` to satisfy Ruff checks.
- Replaced `socket.timeout` with `TimeoutError` in smoke test exception handlers.
- Hardened archive extraction by opening target files relative to the output directory file descriptor, guarding file descriptor lifecycles, and preserving the atomic `O_NOFOLLOW` behavior.
- Turned `connect_bridge_socket` into a context manager that closes the socket on exit and narrowed retry logic to `ConnectionRefusedError`.
- Cleaned up temporary firmware downloads only on failure so a successful download is never unlinked before renaming.
- Reset `server_socket` before closing the listening socket when the bridge thread fails to start.
- Enclosed test and client sockets in context managers to guarantee resource release.
- Added path traversal, symlink, and decompression size limit validation on archives.
- Bound client and server protocol frame sizes to 1 MiB in `scripts/smoke_test.py`.
- Added payload length bounds check before unpacking screen info frames in `_handle_handshake`.
- Added per-read timeout handling to `read_frame`.
- `wsl/test-e2e.sh` imports `os` and parameterizes bridge and screenshot paths.
- `wsl/measure-pitch.py` guards divisions against zero on silent audio buffers.
- Allocate ephemeral TCP ports dynamically (`MockBridgeServer(port=0)`) in tests.
- Manage client socket lifecycle via `connect_bridge_socket` and context blocks.
- Raise `SystemExit(130)` on `KeyboardInterrupt` in `mock-server` action.
- Atomically refuse symlinks during archive extraction with `os.O_NOFOLLOW`.
- Compare canonical file paths in `wsl/test-e2e.sh` to prevent self-truncation.

## [0.2.1] (2026-10-03)

### Added
- `docs/template-drift.md` records what this repository took from the agents,
  foucault, euler, and rough templates, what it declined, every local
  difference, and the license boundary.

## [0.2.0] (2026-10-03)

### Added
- PR quality review through `abuzucom/euler`, run after `ci` completes.
- `docs/pr-security-review.md` and `docs/pr-quality-review.md` record the
  reviewer wiring.

## [0.1.0] (2026-10-03)

### Added
- Ruff baseline from `abuzucom/rough`. CI runs the block tier, the warn tier,
  and the format check with Ruff 0.16.9 pinned by hash.
- Agent policy from `abuzucom/agents`: `AGENTS.md`, synchronized tool copies,
  portable checkers, client hooks, tests, and compliance workflows.
- PR security review through `abuzucom/foucault`.
- Dependabot updates for GitHub Actions and Python tools, targeting `main`.
- `docs/development.md`, `SECURITY.md`, and `CONTRIBUTING.md`.

### Fixed
- `wsl/*.py` passes the Ruff block tier. The USB worker logs the traceback of
  a failed event.
