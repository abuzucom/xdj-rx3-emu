#!/usr/bin/env python3
"""Cover the Windows bootstrap orchestrator phases and command construction."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from windows.rx3_windows import (
    ARMEL_WRAPPER_BODY,
    BUILD_MARKER,
    FETCH_MARKERS,
    HANDOFF_CLONE_DIR,
    HANDOFF_PINNED_COMMIT,
    HANDOFF_REPO_URL,
    HANDOFF_REQUIRED_FILES,
    SMOKE_TEST_SCRIPT,
    USB1_ALLOWED_ROOT_ENV,
    USB1_SOURCE_ENV,
    BootstrapError,
    Options,
    _resolve_usb1_source,
    _wsl_path_from_windows,
    acquire_firmware_archive,
    build_phase_plan,
    build_rootfs,
    check_environment,
    ensure_armel_wrapper,
    fetch_handoff,
    fetch_recovery_inputs,
    launch_emulator,
    main,
    seed_usb_stick,
    stage_wsl_files,
    sync_usb1_source,
)


def _completed(args: list[str], code: int = 0, stdout: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args, code, stdout, b"")


class FakeRunner:
    """Record commands, emulate WSL path state, and replay scripted results."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.launches: list[list[str]] = []
        self.scripted: list[tuple[str, subprocess.CompletedProcess[bytes]]] = []
        self.path_exists: set[str] = set()

    def run(self, args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(args), input_bytes))
        command = args[-1] if args else ""
        if command.startswith("test -e "):
            return self._path_probe(args, command)
        self._emulate_side_effects(command)
        joined = " ".join(str(part) for part in args)
        for needle, result in self.scripted:
            if needle in joined:
                return result
        return _completed(list(args))

    def launch(self, args: list[str]) -> None:
        self.launches.append(list(args))

    def _path_probe(self, args: list[str], command: str) -> subprocess.CompletedProcess[bytes]:
        path = command[len("test -e ") :]
        code = 0 if path in self.path_exists else 1
        return _completed(list(args), code)

    def _emulate_side_effects(self, command: str) -> None:
        if command.startswith("git clone"):
            self.path_exists.add(f"{HANDOFF_CLONE_DIR}/.git")
        if "cp -a" in command and "rx3-handoff" in command:
            for name in HANDOFF_REQUIRED_FILES:
                self.path_exists.add(f"~/rx3/rx3-handoff/{name}")


def _default_options() -> Options:
    return Options(check_only=False, no_run=False, no_usb=False, skip_download=False)


class BootstrapUsbSourceTest(unittest.TestCase):
    """Cover the USB1 source sync stage."""

    def test_resolve_usb1_source_prefers_argument_over_env(self) -> None:
        with patch.dict("os.environ", {USB1_SOURCE_ENV: r"C:\env\music"}):
            options = Options(check_only=False, no_run=False, no_usb=False, skip_download=False, usb1_source=r"C:\arg\music")
            self.assertEqual(_resolve_usb1_source(options), r"C:\arg\music")

    def test_resolve_usb1_source_falls_back_to_env(self) -> None:
        with patch.dict("os.environ", {USB1_SOURCE_ENV: r"C:\env\music"}):
            options = Options(check_only=False, no_run=False, no_usb=False, skip_download=False, usb1_source=None)
            self.assertEqual(_resolve_usb1_source(options), r"C:\env\music")

    def test_resolve_usb1_source_returns_none_when_unset(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(_resolve_usb1_source(_default_options()))

    def test_wsl_path_translates_windows_drive_path(self) -> None:
        self.assertEqual(_wsl_path_from_windows(r"C:\Music\140"), "/mnt/c/Music/140")

    def test_wsl_path_translates_path_with_spaces(self) -> None:
        self.assertEqual(_wsl_path_from_windows(r"C:\My Music\140"), "/mnt/c/My Music/140")

    def test_wsl_path_rejects_relative_path(self) -> None:
        with self.assertRaises(BootstrapError) as ctx:
            _wsl_path_from_windows("relative\\folder")
        message = str(ctx.exception)
        self.assertIn("must be absolute", message)
        self.assertIn("relative", message)

    def test_wsl_path_rejects_unc_and_drive_relative_paths(self) -> None:
        with self.assertRaises(BootstrapError) as ctx:
            _wsl_path_from_windows(r"\\server\share")
        self.assertIn("drive letter", str(ctx.exception))
        with self.assertRaises(BootstrapError) as ctx:
            _wsl_path_from_windows(r"\Windows")
        self.assertIn("must be absolute", str(ctx.exception))

    def test_sync_usb1_source_rsyncs_existing_folder(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "music"
            source.mkdir()
            expected_wsl = _wsl_path_from_windows(str(source)) + "/"
            sync_usb1_source(runner, str(source))
        commands = [call[0][-1] for call in runner.calls]
        self.assertTrue(any("test -d" in command for command in commands))
        rsync_command = next(command for command in commands if "rsync" in command)
        self.assertIn("rsync -a --delete", rsync_command)
        self.assertIn("--max-size=1G", rsync_command)
        self.assertIn("timeout 600", rsync_command)
        self.assertIn(expected_wsl, rsync_command)
        self.assertIn("~/rx3/usb1/Music/", rsync_command)

    def test_sync_usb1_source_quotes_shell_metacharacters(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "music'; echo pwned"
            source.mkdir()
            sync_usb1_source(runner, str(source))
        commands = [call[0][-1] for call in runner.calls]
        rsync_command = next(command for command in commands if "rsync" in command)
        tokens = shlex.split(rsync_command)
        # The malicious words must not appear as separate shell tokens.
        self.assertNotIn("echo", tokens)
        self.assertNotIn("pwned", tokens)
        # The rsync source must be a single quoted token equal to the WSL path.
        expected_src = _wsl_path_from_windows(str(source)) + "/"
        source_tokens = [t for t in tokens if t.startswith("/mnt/")]
        self.assertEqual(source_tokens, [expected_src])

    def test_sync_usb1_source_fails_when_folder_missing_in_wsl(self) -> None:
        runner = FakeRunner()
        runner.scripted.append(("test -d", _completed(["wsl.exe"], 1)))
        with self.assertRaises(BootstrapError) as ctx:
            sync_usb1_source(runner, r"C:\nonexistent\folder")
        message = str(ctx.exception)
        self.assertIn("USB1 source folder does not exist in WSL", message)
        self.assertIn("C:", message)

    def test_sync_usb1_source_rejects_parent_traversal(self) -> None:
        runner = FakeRunner()
        with self.assertRaises(BootstrapError) as ctx:
            sync_usb1_source(runner, r"C:\Music\..\Windows")
        self.assertIn("must not traverse parents", str(ctx.exception))

    def test_sync_usb1_source_honors_allowed_root_env(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            allowed.mkdir()
            allowed_sub = allowed / "sub"
            allowed_sub.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            with patch.dict("os.environ", {USB1_ALLOWED_ROOT_ENV: str(allowed)}):
                sync_usb1_source(runner, str(allowed_sub))
            with self.assertRaises(BootstrapError) as ctx:
                with patch.dict("os.environ", {USB1_ALLOWED_ROOT_ENV: str(allowed)}):
                    sync_usb1_source(runner, str(outside))
        self.assertIn("outside allowed root", str(ctx.exception))

    def test_plan_appends_sync_stage_when_source_configured(self) -> None:
        options = Options(check_only=False, no_run=False, no_usb=False, skip_download=False, usb1_source=r"C:\Music")
        titles = [title for title, _ in build_phase_plan(options)]
        self.assertIn("Sync USB1 music", titles)

    def test_plan_omits_sync_stage_when_source_unset(self) -> None:
        titles = [title for title, _ in build_phase_plan(_default_options())]
        self.assertNotIn("Sync USB1 music", titles)

    def test_no_usb_drops_sync_stage(self) -> None:
        options = Options(check_only=False, no_run=False, no_usb=True, skip_download=False, usb1_source=r"C:\Music")
        titles = [title for title, _ in build_phase_plan(options)]
        self.assertNotIn("Sync USB1 music", titles)


class BootstrapEnvironmentTest(unittest.TestCase):
    """Cover the environment check phase."""

    def test_check_passes_with_distro_and_packages(self) -> None:
        runner = FakeRunner()
        runner.scripted.append(("wsl.exe -l -q", _completed(["wsl.exe"], 0, b"Ubuntu\n")))
        with patch("shutil.which", return_value="C:\\tools\\bin"):
            check_environment(runner)
        self.assertEqual(runner.calls[0][0], ["wsl.exe", "-l", "-q"])

    def test_check_fails_without_wsl(self) -> None:
        runner = FakeRunner()
        with patch("shutil.which", return_value=None):
            with self.assertRaises(BootstrapError) as ctx:
                check_environment(runner)
        self.assertIn("wsl --install", str(ctx.exception))

    def test_check_fails_without_distro(self) -> None:
        runner = FakeRunner()
        runner.scripted.append(("wsl.exe -l -q", _completed(["wsl.exe"], 0, b"")))
        with patch("shutil.which", return_value="C:\\tools\\bin"):
            with self.assertRaises(BootstrapError) as ctx:
                check_environment(runner)
        self.assertIn("No WSL distribution", str(ctx.exception))

    def test_check_reports_missing_packages_with_apt_hint(self) -> None:
        runner = FakeRunner()
        runner.scripted.append(("wsl.exe -l -q", _completed(["wsl.exe"], 0, b"Ubuntu\n")))
        runner.scripted.append(("command -v", _completed(["wsl.exe"], 0, b"MISSING:qemu-arm\nMISSING:mformat\n")))
        with patch("shutil.which", return_value="C:\\tools\\bin"):
            with self.assertRaises(BootstrapError) as ctx:
                check_environment(runner)
        message = str(ctx.exception)
        self.assertIn("qemu-user", message)
        self.assertIn("mtools", message)
        self.assertIn("apt-get install", message)

    def test_check_decodes_utf16_distro_listing(self) -> None:
        runner = FakeRunner()
        runner.scripted.append(("wsl.exe -l -q", _completed(["wsl.exe"], 0, "Ubuntu\r\n".encode("utf-16-le"))))
        with patch("shutil.which", return_value="C:\\tools\\bin"):
            check_environment(runner)


class BootstrapStagingTest(unittest.TestCase):
    """Cover staging, handoff, and build phases."""

    def test_stage_pipes_tarball_into_wsl(self) -> None:
        runner = FakeRunner()
        runner.scripted.append(("tar -C", _completed(["tar"], 0, b"TARBALL")))
        runner.path_exists.add("~/rx3/run-rx3.sh")
        stage_wsl_files(runner)
        tar_call = runner.calls[1]
        self.assertEqual(tar_call[0][0], "tar")
        unpack_call = runner.calls[2]
        self.assertIn("tar -C ~/rx3 -xf -", unpack_call[0][-1])
        self.assertEqual(unpack_call[1], b"TARBALL")

    def test_stage_fails_when_verification_misses(self) -> None:
        runner = FakeRunner()
        runner.scripted.append(("tar -C", _completed(["tar"], 0, b"TARBALL")))
        with self.assertRaises(BootstrapError) as ctx:
            stage_wsl_files(runner)
        self.assertIn("run-rx3.sh", str(ctx.exception))

    def test_handoff_skips_when_files_present(self) -> None:
        runner = FakeRunner()
        for name in HANDOFF_REQUIRED_FILES:
            runner.path_exists.add(f"~/rx3/rx3-handoff/{name}")
        fetch_handoff(runner)
        commands = [call[0][-1] for call in runner.calls]
        self.assertFalse(any("git clone" in command for command in commands))

    def test_handoff_clones_pinned_commit(self) -> None:
        runner = FakeRunner()
        runner.scripted.append(("rev-parse HEAD", _completed(["git"], 0, HANDOFF_PINNED_COMMIT.encode() + b"\n")))
        fetch_handoff(runner)
        commands = [call[0][-1] for call in runner.calls]
        self.assertTrue(any(f"git clone {HANDOFF_REPO_URL}" in command for command in commands))
        self.assertTrue(any(HANDOFF_PINNED_COMMIT in command for command in commands))

    def test_handoff_rejects_commit_mismatch(self) -> None:
        runner = FakeRunner()
        runner.scripted.append(("rev-parse HEAD", _completed(["git"], 0, b"0" * 40 + b"\n")))
        with self.assertRaises(BootstrapError) as ctx:
            fetch_handoff(runner)
        self.assertIn("commit mismatch", str(ctx.exception))

    def test_firmware_phase_defaults_to_verified_download(self) -> None:
        runner = FakeRunner()
        acquire_firmware_archive(runner, _default_options())
        args = runner.calls[0][0]
        self.assertEqual(args[1], str(SMOKE_TEST_SCRIPT))
        self.assertIn("--download", args)

    def test_firmware_phase_honors_skip_download(self) -> None:
        runner = FakeRunner()
        options = Options(check_only=False, no_run=False, no_usb=False, skip_download=True, usb1_source=None)
        acquire_firmware_archive(runner, options)
        self.assertNotIn("--download", runner.calls[0][0])

    def test_fetch_skips_when_markers_present(self) -> None:
        runner = FakeRunner()
        runner.path_exists.update(FETCH_MARKERS)
        fetch_recovery_inputs(runner)
        commands = [call[0][-1] for call in runner.calls]
        self.assertFalse(any("rx3_fetch.sh" in command for command in commands))

    def test_wrapper_writes_constant_body(self) -> None:
        runner = FakeRunner()
        ensure_armel_wrapper(runner)
        self.assertEqual(runner.calls[1][1], ARMEL_WRAPPER_BODY.encode())
        self.assertIn("chmod +x ~/rx3/armel-gcc", runner.calls[1][0][-1])

    def test_wrapper_skips_when_present(self) -> None:
        runner = FakeRunner()
        runner.path_exists.add("~/rx3/armel-gcc")
        ensure_armel_wrapper(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_build_skips_when_chroot_exists(self) -> None:
        runner = FakeRunner()
        runner.path_exists.add(BUILD_MARKER)
        build_rootfs(runner)
        commands = [call[0][-1] for call in runner.calls]
        self.assertFalse(any("build-rootfs-wsl.sh" in command for command in commands))

    def test_seed_usb_invokes_make_usb(self) -> None:
        runner = FakeRunner()
        seed_usb_stick(runner)
        self.assertIn("make-usb.sh", runner.calls[0][0][-1])

    def test_launch_starts_player_in_wsl(self) -> None:
        runner = FakeRunner()
        launch_emulator(runner)
        self.assertEqual(runner.launches[0][:2], ["wsl.exe", "bash"])
        self.assertIn("run-rx3.sh", runner.launches[0][-1])


class BootstrapPlanTest(unittest.TestCase):
    """Cover phase list assembly and the main entry point."""

    def test_check_only_plan_has_single_phase(self) -> None:
        options = Options(check_only=True, no_run=False, no_usb=False, skip_download=False, usb1_source=None)
        plan = build_phase_plan(options)
        self.assertEqual(len(plan), 1)

    def test_default_plan_bootstraps_then_launches(self) -> None:
        plan = build_phase_plan(_default_options())
        self.assertEqual(plan[-1][0], "Launch emulator")
        self.assertEqual(plan[0][0], "Check environment")

    def test_no_run_and_no_usb_drop_phases(self) -> None:
        options = Options(check_only=False, no_run=True, no_usb=True, skip_download=False, usb1_source=None)
        titles = [title for title, _ in build_phase_plan(options)]
        self.assertNotIn("Launch emulator", titles)
        self.assertNotIn("Seed virtual USB stick", titles)

    def test_main_returns_error_on_failed_phase(self) -> None:
        with patch("shutil.which", return_value=None):
            result = main(["--check-only"])
        self.assertEqual(result, 1)

    def test_main_runs_check_only_cleanly(self) -> None:
        with (
            patch("shutil.which", return_value="C:\\tools\\bin"),
            patch("windows.rx3_windows.CommandRunner") as runner_cls,
        ):
            runner_cls.return_value.run.return_value = _completed(["wsl.exe"], 0, b"Ubuntu\n")
            result = main(["--check-only"])
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
