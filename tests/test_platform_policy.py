#!/usr/bin/env python3
"""Verify macOS and Linux behavior policy without host-specific execution."""
import sys
import unittest
from pathlib import Path


HOOKS_DIRECTORY = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIRECTORY))

import _platform_policy as platform_policy


class MacOSPolicyTest(unittest.TestCase):
    """macOS persistence, security, storage, and privacy operations gate."""

    def assert_verdict(self, command: str, expected: str) -> None:
        """Assert one macOS command verdict."""
        program_name, *arguments = command.split()
        verdict, _reason = platform_policy.classify_platform_command(
            "darwin",
            program_name,
            arguments,
        )
        self.assertEqual(verdict, expected)

    def test_persistence_and_security_changes_deny(self):
        for command in (
            "launchctl bootstrap gui/501 agent.plist",
            "spctl --master-disable",
            "xattr -d com.apple.quarantine app",
            "tccutil reset All",
        ):
            with self.subTest(command=command):
                self.assert_verdict(command, "deny")

    def test_storage_destruction_denies(self):
        for command in (
            "diskutil eraseDisk APFS disk4",
            "diskutil apfs deleteVolume disk5s1",
            "diskutil list",
            "tmutil delete /Volumes/Backup",
        ):
            with self.subTest(command=command):
                self.assert_verdict(command, "deny")

    def test_sensitive_discovery_asks(self):
        for command in (
            "system_profiler SPHardwareDataType",
            "security find-generic-password -a account",
        ):
            with self.subTest(command=command):
                self.assert_verdict(command, "ask")


class LinuxPolicyTest(unittest.TestCase):
    """Linux storage, persistence, package, and remote operations gate."""

    def assert_verdict(self, command: str, expected: str) -> None:
        """Assert one Linux command verdict."""
        program_name, *arguments = command.split()
        verdict, _reason = platform_policy.classify_platform_command(
            "linux",
            program_name,
            arguments,
        )
        self.assertEqual(verdict, expected)

    def test_storage_destruction_denies(self):
        for command in (
            "mkfs.ext4 /dev/vdb1",
            "wipefs -a /dev/vdb",
            "parted /dev/vdb rm 1",
        ):
            with self.subTest(command=command):
                self.assert_verdict(command, "deny")

    def test_service_persistence_denies(self):
        for command in (
            "systemctl enable sample.service",
            "systemctl start sample.service",
            "crontab -r",
        ):
            with self.subTest(command=command):
                self.assert_verdict(command, "deny")

    def test_package_changes_and_discovery_ask(self):
        for command in (
            "apt install ripgrep",
            "dnf remove package",
            "ps aux",
            "env",
            "whoami",
        ):
            with self.subTest(command=command):
                self.assert_verdict(command, "ask")


class TransferPolicyTest(unittest.TestCase):
    """Transfer direction survives changes to endpoint literals."""

    def test_local_to_remote_denies(self):
        for endpoint in ("host:/srv/file", "other:/tmp/file"):
            with self.subTest(endpoint=endpoint):
                verdict, _reason = platform_policy.classify_platform_command(
                    "linux",
                    "scp",
                    ["report.txt", endpoint],
                )
                self.assertEqual(verdict, "deny")

    def test_remote_to_local_asks(self):
        verdict, _reason = platform_policy.classify_platform_command(
            "darwin",
            "scp",
            ["host:/srv/file", "report.txt"],
        )
        self.assertEqual(verdict, "ask")

    def test_windows_drive_is_not_remote_endpoint(self):
        verdict, _reason = platform_policy.classify_platform_command(
            "win32",
            "scp",
            ["C:\\work\\report.txt", "D:\\archive\\report.txt"],
        )
        self.assertEqual(verdict, "")

    def test_single_letter_remote_host_denies(self):
        for platform_name, destination in (
            ("linux", "x:dest"),
            ("linux", "D:archive\\report.txt"),
            ("win32", "x:dest"),
        ):
            with self.subTest(platform=platform_name, destination=destination):
                verdict, _reason = platform_policy.classify_platform_command(
                    platform_name,
                    "scp",
                    ["report.txt", destination],
                )
                self.assertEqual(verdict, "deny")


if __name__ == "__main__":
    unittest.main()
