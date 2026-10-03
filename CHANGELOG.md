# Changelog

This file documents every notable project change on `main`.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
One deviation applies. A version heading parenthesizes the release date.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] (2026-10-03)

### Added
- `scripts/smoke_test.py` validates firmware zip archives, simulates the bridge
  protocol via a mock TCP server, and verifies client handshakes.
- `.github/workflows/smoketest.yml` runs automated smoke tests in CI.
- `tests/test_smoke_test.py` covers archive discovery, size assertions, and
  wire framing.
- `firmware/` untracked directory in `.gitignore`.

### Fixed
- `wsl/test-e2e.sh` imports `os` and parameterizes bridge and screenshot paths.
- `wsl/measure-pitch.py` guards divisions against zero on silent audio buffers.

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
