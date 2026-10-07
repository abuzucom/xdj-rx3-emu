#!/usr/bin/env python3
"""One-click Windows bootstrap and launcher for the XDJ-RX3 emulator.

Stages the WSL harness files, fetches the pinned rx3-handoff recovery
tooling, verifies the firmware archive, builds the chroot, seeds the
virtual USB stick, and launches the player with the bridge. Every phase
is idempotent and safe to re-run.
"""

from __future__ import annotations

import argparse
import functools
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WSL_SOURCE_DIR = REPO_ROOT / "wsl"
SMOKE_TEST_SCRIPT = REPO_ROOT / "scripts" / "smoke_test.py"
FIRMWARE_DIR = REPO_ROOT / "firmware"
HANDOFF_CLONE_DIR = "~/rx3/Rx3-flx4"
HANDOFF_REPO_URL = "https://github.com/abuzucom/Rx3-flx4.git"
HANDOFF_PINNED_COMMIT = "c5659888aae4766d8e3fdce84f60ec09ceba0212"
HANDOFF_REQUIRED_FILES = (
    "recover-firmware.py",
    "extract_cramfs.py",
    "patch-player.py",
    "fbshim.c",
    "control-shim.c",
)
WSL_PACKAGE_COMMANDS = {
    "python3": "python3",
    "curl": "curl",
    "rsync": "rsync",
    "qemu-arm": "qemu-user",
    "mformat": "mtools",
    "git": "git",
    "tar": "tar",
    "unzip": "unzip",
    "7z": "p7zip-full",
}
APT_INSTALL_PREFIX = "sudo apt-get update && sudo apt-get install -y"
WSL_INSTALL_HINT = (
    "Install WSL first: open an elevated PowerShell, run 'wsl --install', "
    "reboot, install Ubuntu from the Microsoft Store, then run this again"
)
ARMEL_WRAPPER_BODY = '#!/bin/sh\nexec "$(ls "$HOME/rx3/tc-armel/bin/"*-gcc | head -n 1)" "$@"\n'
USB1_SOURCE_ENV = "RX3_USB1_SOURCE"
USB1_ALLOWED_ROOT_ENV = "RX3_USB1_ALLOWED_ROOT"
USB1_SYNC_TIMEOUT_SECONDS = 600
FETCH_MARKERS = ("~/rx3/proot", "~/rx3/tc-armel", "~/rx3/rx3-handoff/extracted/runtime-files")
BUILD_MARKER = "~/rx3/rootfs/root/pdj/rbp-pi"
BRIDGE_ADDRESS = "127.0.0.1:4480"
ERROR_TAIL_BYTES = 400

PhaseAction = Callable[["CommandRunner"], None]


class BootstrapError(Exception):
    """A bootstrap phase failed; the message names the recovery action."""


@dataclass
class Options:
    """Command-line switches that shape the phase list."""

    check_only: bool
    no_run: bool
    no_usb: bool
    skip_download: bool
    usb1_source: str | None = None


@dataclass
class CommandRunner:
    """Executes local processes; tests inject a fake through the same interface."""

    def run(self, args: Sequence[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        """Run a command to completion and capture its output."""
        return subprocess.run(list(args), input=input_bytes, capture_output=True, check=False)

    def launch(self, args: Sequence[str]) -> None:
        """Start a detached command in its own console window."""
        creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        subprocess.Popen(list(args), creationflags=creation_flags)


def require_success(result: subprocess.CompletedProcess[bytes], action: str, recovery: str) -> None:
    """Raise BootstrapError with output context when a command fails."""
    if result.returncode == 0:
        return
    detail = result.stderr.decode(errors="replace").strip()[-ERROR_TAIL_BYTES:]
    raise BootstrapError(f"{action} failed with exit code {result.returncode}. {recovery}. Output: {detail}")


def wsl_script(command: str) -> list[str]:
    """Build a wsl.exe invocation for a constant bash command.

    Only module constants reach this helper. No operator input is ever
    interpolated into the command text.
    """
    return ["wsl.exe", "bash", "-lc", command]


def wsl_path_exists(runner: CommandRunner, wsl_path: str) -> bool:
    """Check whether a path inside WSL exists. Callers pass constants only."""
    result = runner.run(wsl_script(f"test -e {wsl_path}"))
    return result.returncode == 0


def _decode_wsl_listing(raw: bytes) -> str:
    """Decode wsl.exe list output, which arrives as UTF-16LE on Windows."""
    if b"\x00" in raw:
        return raw.decode("utf-16-le", errors="ignore")
    return raw.decode(errors="replace")


def check_environment(runner: CommandRunner) -> None:
    """Verify WSL, a distro, Windows tar, and the required WSL packages."""
    if shutil.which("wsl.exe") is None:
        raise BootstrapError(f"WSL is not available. {WSL_INSTALL_HINT}")
    listing = runner.run(["wsl.exe", "-l", "-q"])
    require_success(listing, "WSL distro listing", WSL_INSTALL_HINT)
    if not _decode_wsl_listing(listing.stdout).strip():
        raise BootstrapError(f"No WSL distribution is installed. {WSL_INSTALL_HINT}")
    if shutil.which("tar") is None:
        raise BootstrapError("Windows tar.exe is missing. Install Windows 10 17063 or newer")
    probe = "; ".join(f"command -v {name} >/dev/null || echo MISSING:{name}" for name in WSL_PACKAGE_COMMANDS)
    result = runner.run(wsl_script(probe))
    require_success(result, "WSL package probe", "Check the WSL distribution")
    missing = [line.split(":", 1)[1] for line in result.stdout.decode().splitlines() if line.startswith("MISSING:")]
    if missing:
        packages = " ".join(WSL_PACKAGE_COMMANDS[name] for name in missing)
        raise BootstrapError(
            f"Missing WSL packages: {', '.join(missing)}. Install them inside WSL: {APT_INSTALL_PREFIX} {packages}"
        )


def stage_wsl_files(runner: CommandRunner) -> None:
    """Copy the repository wsl directory into the WSL home as ~/rx3."""
    require_success(runner.run(wsl_script("mkdir -p ~/rx3")), "WSL home staging", "Check the WSL distribution")
    archive = runner.run(["tar", "-C", str(WSL_SOURCE_DIR), "-cf", "-", "."])
    require_success(archive, "WSL file archive", "Check that tar.exe works on Windows")
    unpack = runner.run(wsl_script("tar -C ~/rx3 -xf -"), input_bytes=archive.stdout)
    require_success(unpack, "WSL file staging", "Check free space in the WSL home")
    if not wsl_path_exists(runner, "~/rx3/run-rx3.sh"):
        raise BootstrapError("Staging verification failed: ~/rx3/run-rx3.sh is missing")


def _handoff_files_present(runner: CommandRunner) -> bool:
    """Check that every required recovery file exists in ~/rx3/rx3-handoff."""
    for name in HANDOFF_REQUIRED_FILES:
        if not wsl_path_exists(runner, f"~/rx3/rx3-handoff/{name}"):
            return False
    return True


def _verify_handoff_commit(runner: CommandRunner) -> None:
    """Clone or reuse the pinned fork and verify the checked-out commit."""
    if not wsl_path_exists(runner, f"{HANDOFF_CLONE_DIR}/.git"):
        clone = runner.run(wsl_script(f"git clone {HANDOFF_REPO_URL} {HANDOFF_CLONE_DIR}"))
        require_success(clone, "rx3-handoff clone", "Check network access to GitHub")
    checkout = runner.run(wsl_script(f"git -C {HANDOFF_CLONE_DIR} checkout --quiet {HANDOFF_PINNED_COMMIT}"))
    require_success(checkout, "rx3-handoff checkout", "Check the pinned commit exists in the fork")
    current = runner.run(wsl_script(f"git -C {HANDOFF_CLONE_DIR} rev-parse HEAD"))
    require_success(current, "rx3-handoff commit probe", "Check the clone")
    checked_out = current.stdout.decode().strip()
    if checked_out != HANDOFF_PINNED_COMMIT:
        raise BootstrapError(f"rx3-handoff commit mismatch: expected {HANDOFF_PINNED_COMMIT}, got {checked_out}")


def fetch_handoff(runner: CommandRunner) -> None:
    """Stage the pinned rx3-handoff recovery tooling into ~/rx3."""
    if _handoff_files_present(runner):
        print("rx3-handoff already present; skipping clone.")
        return
    _verify_handoff_commit(runner)
    copy_command = f"mkdir -p ~/rx3/rx3-handoff && cp -a {HANDOFF_CLONE_DIR}/rx3-handoff/. ~/rx3/rx3-handoff/"
    require_success(runner.run(wsl_script(copy_command)), "rx3-handoff copy", "Check free space in the WSL home")
    if not _handoff_files_present(runner):
        raise BootstrapError("rx3-handoff copy incomplete: required recovery files are missing")


def acquire_firmware_archive(runner: CommandRunner, options: Options) -> None:
    """Download and verify the smoke test firmware archive on the Windows side."""
    args = [
        sys.executable,
        str(SMOKE_TEST_SCRIPT),
        "--action",
        "prepare",
        "--firmware-dir",
        str(FIRMWARE_DIR),
        "--out-dir",
        str(FIRMWARE_DIR / "extracted"),
    ]
    if not options.skip_download:
        args.append("--download")
    result = runner.run(args)
    require_success(result, "Firmware acquisition", "Check network access or place XDJ-RX3_v120.zip in firmware")


def fetch_recovery_inputs(runner: CommandRunner) -> None:
    """Run rx3_fetch.sh inside WSL unless its outputs already exist."""
    if all(wsl_path_exists(runner, marker) for marker in FETCH_MARKERS):
        print("Recovery inputs already fetched; skipping rx3_fetch.sh.")
        return
    result = runner.run(wsl_script("cd ~/rx3 && bash rx3_fetch.sh"))
    require_success(result, "rx3_fetch.sh", "Check the rx3-handoff staging and network access")


def ensure_armel_wrapper(runner: CommandRunner) -> None:
    """Create the armel-gcc cross-compiler wrapper expected by the build."""
    if wsl_path_exists(runner, "~/rx3/armel-gcc"):
        return
    result = runner.run(
        wsl_script("cat > ~/rx3/armel-gcc && chmod +x ~/rx3/armel-gcc"),
        input_bytes=ARMEL_WRAPPER_BODY.encode(),
    )
    require_success(result, "armel-gcc wrapper creation", "Check the WSL home permissions")


def build_rootfs(runner: CommandRunner) -> None:
    """Assemble the chroot unless a previous build already exists."""
    if wsl_path_exists(runner, BUILD_MARKER):
        print("Chroot already built; skipping build-rootfs-wsl.sh.")
        return
    result = runner.run(wsl_script("cd ~/rx3 && bash build-rootfs-wsl.sh"))
    require_success(result, "build-rootfs-wsl.sh", "Run the fetch phase again with its inputs intact")


def seed_usb_stick(runner: CommandRunner) -> None:
    """Seed the virtual USB1 stick with test tones and a fake block device."""
    result = runner.run(wsl_script("cd ~/rx3 && bash make-usb.sh"))
    require_success(result, "make-usb.sh", "Check the chroot build output")


def _resolve_usb1_source(options: Options) -> str | None:
    """Return the configured Windows source path for USB1, or None if unset."""
    return options.usb1_source or os.environ.get(USB1_SOURCE_ENV)


def _wsl_path_from_windows(windows_path: str) -> str:
    """Translate a Windows drive-letter path to a WSL /mnt path."""
    p = Path(windows_path)
    if not p.is_absolute():
        raise BootstrapError(f"USB source path must be absolute: {windows_path!r}")
    drive = p.drive
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
        raise BootstrapError(f"USB source path must use a drive letter (e.g., C:\\): {windows_path!r}")
    return f"/mnt/{drive[0].lower()}/" + "/".join(p.parts[1:])


def _validate_usb1_source(windows_source: str) -> None:
    """Validate format and allowed root before rsync sees the path.

    Requires an absolute drive-letter path with no parent-traversal
    components. If RX3_USB1_ALLOWED_ROOT is set, the source must resolve
    under that root. Existence is checked in WSL, where rsync runs.
    """
    input_path = Path(windows_source)
    if not input_path.is_absolute():
        raise BootstrapError(f"USB1 source path must be absolute: {windows_source!r}")
    if any(part == ".." for part in input_path.parts):
        raise BootstrapError(f"USB1 source path must not traverse parents: {windows_source!r}")
    drive = input_path.drive
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
        raise BootstrapError(f"USB1 source path must use a drive letter (e.g., C:\\): {windows_source!r}")
    allowed_root = os.environ.get(USB1_ALLOWED_ROOT_ENV)
    if allowed_root:
        try:
            src = input_path.resolve(strict=True)
        except OSError as exc:
            raise BootstrapError(f"USB1 source folder does not exist: {windows_source!r}") from exc
        try:
            root = Path(allowed_root).resolve(strict=True)
        except OSError as exc:
            raise BootstrapError(f"USB1 allowed root does not exist: {allowed_root!r}") from exc
        if root not in (src, *src.parents):
            raise BootstrapError(f"USB1 source outside allowed root {allowed_root!r}: {windows_source!r}")


def sync_usb1_source(runner: CommandRunner, windows_source: str) -> None:
    """Sync the validated Windows music folder into the WSL virtual USB1 stick."""
    _validate_usb1_source(windows_source)
    wsl_src = _wsl_path_from_windows(windows_source)
    quoted_src = shlex.quote(wsl_src + "/")
    check = runner.run(wsl_script(f"test -d {quoted_src}"))
    if check.returncode != 0:
        raise BootstrapError(
            f"USB1 source folder does not exist in WSL: {windows_source!r}. "
            "Create it and add music, or unset RX3_USB1_SOURCE / omit --usb1-source to skip this phase."
        )
    command = (
        "mkdir -p ~/rx3/usb1/Music && "
        f"timeout {USB1_SYNC_TIMEOUT_SECONDS} rsync -a --delete --max-size=1G --exclude='.*' "
        f"{quoted_src} ~/rx3/usb1/Music/"
    )
    result = runner.run(wsl_script(command))
    require_success(result, "USB1 music sync", "Check the source path and that rsync is installed in WSL")


def launch_emulator(runner: CommandRunner) -> None:
    """Start the player and bridge in a new console window."""
    runner.launch(["wsl.exe", "bash", "-lc", "cd ~/rx3 && exec bash run-rx3.sh"])
    print(f"Emulator starting in a new console window. Bridge: {BRIDGE_ADDRESS} after about a minute.")


def build_phase_plan(options: Options) -> list[tuple[str, PhaseAction]]:
    """Assemble the ordered phase list for the requested mode."""
    if options.check_only:
        return [("Check environment", check_environment)]
    plan: list[tuple[str, PhaseAction]] = [
        ("Check environment", check_environment),
        ("Stage WSL files", stage_wsl_files),
        ("Fetch rx3-handoff tooling", fetch_handoff),
        ("Acquire firmware archive", lambda runner: acquire_firmware_archive(runner, options)),
        ("Fetch recovery inputs", fetch_recovery_inputs),
        ("Create armel-gcc wrapper", ensure_armel_wrapper),
        ("Build chroot", build_rootfs),
    ]
    if not options.no_usb:
        plan.append(("Seed virtual USB stick", seed_usb_stick))
        usb_source = _resolve_usb1_source(options)
        if usb_source:
            plan.append(("Sync USB1 music", functools.partial(sync_usb1_source, windows_source=usb_source)))
    if not options.no_run:
        plan.append(("Launch emulator", launch_emulator))
    return plan


def parse_args(argv: Sequence[str] | None = None) -> Options:
    """Parse command-line switches."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="run environment checks and stop")
    parser.add_argument("--no-run", action="store_true", help="bootstrap without launching the emulator")
    parser.add_argument("--no-usb", action="store_true", help="skip seeding the virtual USB stick")
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="require a local firmware zip instead of downloading it",
    )
    parser.add_argument(
        "--usb1-source",
        metavar="PATH",
        default=None,
        help="Windows folder to sync into the virtual USB1 stick (also via RX3_USB1_SOURCE env var)",
    )
    args = parser.parse_args(argv)
    return Options(
        check_only=args.check_only,
        no_run=args.no_run,
        no_usb=args.no_usb,
        skip_download=args.skip_download,
        usb1_source=args.usb1_source,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run every phase in order and stop at the first failure."""
    options = parse_args(argv)
    plan = build_phase_plan(options)
    runner = CommandRunner()
    for index, (title, action) in enumerate(plan, start=1):
        print(f"[{index}/{len(plan)}] {title}...")
        try:
            action(runner)
        except BootstrapError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    print("Bootstrap complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
