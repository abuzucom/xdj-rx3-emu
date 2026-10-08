# Changelog

This file documents every notable project change on `main`.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
One deviation applies. A version heading parenthesizes the release date.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] (2026-10-08)

### Added
- New `controllers/` package for sending controller input to the emulator bridge:
  - `controllers/events.py` defines semantic events (`ButtonEvent`, `JogEvent`, `FaderEvent`, `EncoderEvent`) and serializes them to bridge `0x30` key-command frames.
  - `controllers/profiles.py` loads JSON device profiles mapping MIDI notes/CC/pitch-bend to firmware keys; a default profile ships at `controllers/profiles/default.json`.
  - `controllers/backends/midi.py` implements a MIDI backend via optional `mido`/`python-rtmidi` dependencies.
  - `controllers/bridge_client.py` connects a backend to `127.0.0.1:4480` and forwards events, reusing `scripts/smoke_test.py` framing.
- New `controller_client/main.py` CLI with `--backend`, `--bridge-host`, `--bridge-port`, `--profile`, and `--midi-port` options.
- `requirements-controllers.txt` pins optional runtime dependencies with SHA-256 hashes.
- `docs/running.md` documents MIDI controller setup.

## [0.5.4] (2026-10-08)

### Fixed
- `windows/rx3_windows.py` passes the USB1 source and allowed-root paths to WSL as environment variables (`RX3_SRC`, `RX3_ROOT`) instead of embedding quoted strings, removing the shell-quoting surface from operator input.
- Replaced the string-prefix allowed-root check with a true path-prefix `case` check so sibling directories such as `C:\allowed-root-evil` no longer bypass `C:\allowed-root`.
- Added a destination free-space check before `rsync` and a `--no-usb1-sync` switch to skip music sync while still seeding the virtual USB stick.
- `_resolve_usb1_source()` now treats an empty `RX3_USB1_SOURCE` value as unset, matching `.env.example` placeholder behavior.

## [0.5.3] (2026-10-08)

### Fixed
- `windows/rx3_windows.py` now performs the USB1 allowed-root confinement check and `rsync` in a single WSL script, passing the resolved canonical path to `rsync` so symlink components cannot be swapped after verification.
- `_validate_windows_drive_path()` rejects drive-letter roots such as `C:\` to prevent accidental full-drive sync.
- `sync_usb1_source()` reports a plain missing-source message when no allowed root is configured.
- `tests/test_rx3_windows.py` no longer gates the shell-escaping test on an external `bash` executable.

## [0.5.2] (2026-10-08)

### Fixed
- `windows/rx3_windows.py` uses `PureWindowsPath` for drive-letter validation and WSL path translation so the USB1 source checks behave identically on Windows, macOS, and Linux runners.
- Removed the duplicated "path" word in the USB source path error message.
- `tests/test_rx3_windows.py` uses a `_windows_path_under_tmp()` helper to feed realistic `C:\...` paths to the Windows-specific code, so the suite passes on Linux and macOS CI runners where `tempfile` produces POSIX paths.
- `tests/test_rx3_windows.py` no longer invokes an external `bash -n` syntax check, which fails on CI images that ship a non-standard `bash.exe`; token-level quoting assertions remain.

## [0.5.1] (2026-10-07)

### Fixed
- `windows/rx3_windows.py` now reports distinct errors for a missing allowed root, a missing USB1 source folder, and a source folder outside the allowed root, instead of one conflated message.
- `tests/test_rx3_windows.py` `FakeRunner` supports ordered `scripted` responses so multi-step WSL checks can be exercised deterministically.

## [0.5.0] (2026-10-07)

### Added
- `windows/rx3_windows.py` supports syncing a Windows music folder into the virtual USB1 stick via `RX3_USB1_SOURCE` or `--usb1-source`.
- `.env.example` documents the `RX3_USB1_SOURCE` environment variable.
- `docs/running.md` describes the virtual USB music folder, environment variable, and command-line switch.

### Fixed
- Hardened `sync_usb1_source()` against shell injection by passing the WSL source path through `shlex.quote()`.
- Moved USB1 source existence and allowed-root confinement into WSL via `readlink -f` and a case-insensitive prefix check, closing Windows/WSL view mismatches and TOCTOU races.
- Validated `RX3_USB1_SOURCE` and `RX3_USB1_ALLOWED_ROOT` as drive-letter absolute paths and rejected parent-traversal components.
- Made `_resolve_usb1_source()` distinguish `None` from an explicit empty string.
- Replaced the phase-plan lambda with `functools.partial`.
- Bounded the USB1 sync with `rsync --max-size=1G` and a WSL `timeout 600` to limit resource exhaustion.
- `tests/test_rx3_windows.py` covers USB1 source resolution, drive-letter validation, WSL path translation, shell escaping, bash syntax validation, validation failures, allowed-root enforcement (including case-insensitivity and normalized traversal), rsync bounds, and plan assembly.

## [0.4.0] (2026-10-07)

### Added
- `windows/rx3_windows.py` one-click Windows bootstrap and launcher with idempotent phases for environment checks, WSL staging, pinned rx3-handoff fetch, verified firmware acquisition, chroot build, USB seeding, and emulator launch.
- `rx3.cmd` double-click launcher at the repository root.
- `tests/test_rx3_windows.py` covers phase logic, command construction, and idempotency.
- `docs/running.md` operator guide with requirements, quick start, manual setup, environment variables, and troubleshooting.
- `.gitattributes` CRLF exception for `*.cmd` batch files.

### Fixed
- `wsl/rx3_fetch.sh` pins SHA-256 hashes for the proot binary and the cross toolchain, verifies downloads, and re-downloads on hash mismatch.
- `wsl/rx3_fetch.sh` switches the cross toolchain to Bootlin `armv7-eabihf--glibc--stable-2024.05-1` because Bootlin removed the soft-float `armv7-eabi` release and the old URL returns 404.
- Removed the unused `clang` WSL package requirement from the bootstrap probe and `docs/running.md`. No build step invokes clang.
- Corrected the `actions/cache` pin comments in `smoketest.yml` and `seed-firmware-cache.yml` to `v6.1.0`, matching the pinned commit.
- `wsl/test-e2e.sh` calls `measure-pitch.py` instead of the missing `pitch.py`.
- README setup and run sections point to `docs/running.md` and document the smoke test utility.
- `docs/development.md` lists the smoke test and bootstrap commands.

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
- Wrapped mock bridge client handling in `try/except` so an unexpected socket error does not kill the server thread.
- Passed the remaining handshake timeout budget to `_handle_handshake` after connection retries.
- Named mock-bridge tile constants (`TILE_SIZE`, `BLACK_OPAQUE_PIXEL`) and increased the `smoketest.yml` e2e timeout to 30 seconds.
- Logged `OSError` from `connect_bridge_socket` cleanup instead of swallowing it.
- Enforced the remaining per-read timeout inside `_handle_handshake` so handshake waits do not exceed the requested budget.
- Parameterized the WSL test working directory with `RX3_RUN_DIR` in `wsl/test-e2e.sh`.
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
- Scan every zip entry for path traversal and symlink attributes before payload selection.
- Open the extraction output directory with `os.O_NOFOLLOW` and remove partial payloads on extraction failure.
- Bound `read_frame` header and payload reads to a single monotonic deadline.
- Use a monotonic retry budget with per-attempt timeouts and log non-refused connection errors in `connect_bridge_socket`.
- Reset mock bridge state when startup fails so bind and thread errors release the listening socket.
- Close the mock bridge listening socket directly on startup failure so failed starts cannot leak it.
- Move mock bridge listen-socket creation into a factory helper that transfers ownership on success or closes on failure.
- Create the mock bridge listening socket with `socket.create_server` to remove the raw `socket.socket` call from user code.
- Use `time.monotonic()` for the handshake timeout budget in `test_bridge_client` and `_handle_handshake`.
- Pre-unlink the extraction target and create it with `os.O_EXCL` so the Windows fallback cannot follow a swapped symlink.
- Run `wsl/test-e2e.sh` with `set -euo pipefail` and tolerate absent diagnostic logs.

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
