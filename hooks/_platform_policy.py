#!/usr/bin/env python3
"""Classify macOS and Linux command behavior without native execution."""
import os
import sys


SENSITIVE_DISCOVERY_PROGRAMS = frozenset({
    "env",
    "hostname",
    "id",
    "ifconfig",
    "ip",
    "ps",
    "system_profiler",
    "uname",
    "whoami",
})
LINUX_STORAGE_DESTRUCTION = frozenset({
    "fdisk",
    "mkfs",
    "mkfs.btrfs",
    "mkfs.ext4",
    "mkfs.xfs",
    "parted",
    "sfdisk",
    "wipefs",
})
LINUX_PACKAGE_MANAGERS = frozenset({
    "apk",
    "apt",
    "apt-get",
    "dnf",
    "dpkg",
    "pacman",
    "rpm",
    "yum",
    "zypper",
})


def normalize_program_name(program: str) -> str:
    """Return one case-insensitive executable basename."""
    return os.path.basename(program).casefold()


def is_remote_endpoint(endpoint: str, platform_name: str = sys.platform) -> bool:
    """Return whether a transfer endpoint names a remote host."""
    candidate = endpoint.strip().strip('"').strip("'")
    if (platform_name.startswith("win")
            and len(candidate) >= 3
            and candidate[0].isalpha()
            and candidate[1] == ":"
            and candidate[2] in "\\/"):
        return False
    if "://" in candidate:
        return True
    if candidate.startswith("[") and "]:" in candidate:
        return True
    host_separator = candidate.find(":")
    path_separator_positions = [
        position
        for position in (candidate.find("/"), candidate.find("\\"))
        if position >= 0
    ]
    first_path_separator = min(path_separator_positions, default=len(candidate))
    return 0 < host_separator < first_path_separator


def classify_transfer_direction(
    arguments: list[str],
    platform_name: str = sys.platform,
) -> tuple[str, str]:
    """Classify a two-endpoint copy from local and remote path properties."""
    endpoints = [argument for argument in arguments if not argument.startswith("-")]
    if len(endpoints) < 2:
        return "ask", "a transfer endpoint is missing or cannot be resolved"
    source_endpoint = endpoints[-2]
    destination_endpoint = endpoints[-1]
    source_is_remote = is_remote_endpoint(source_endpoint, platform_name=platform_name)
    destination_is_remote = is_remote_endpoint(
        destination_endpoint,
        platform_name=platform_name,
    )
    if destination_is_remote:
        return "deny", "a transfer sends data to a remote endpoint"
    if source_is_remote:
        return "ask", "a transfer downloads data from a remote endpoint"
    return "", ""


def classify_macos_command(
    program_name: str,
    arguments: list[str],
) -> tuple[str, str]:
    """Classify one macOS-native command family."""
    lowered_arguments = [argument.casefold() for argument in arguments]
    if program_name == "launchctl":
        mutating_operations = {"bootstrap", "enable", "kickstart", "load", "submit"}
        if lowered_arguments and lowered_arguments[0] in mutating_operations:
            return "deny", "launchctl changes persistent or active service execution"
        return "ask", "launchctl reads service and persistence state"
    if program_name == "spctl" and "--master-disable" in lowered_arguments:
        return "deny", "spctl disables Gatekeeper policy"
    if program_name == "xattr" and "com.apple.quarantine" in lowered_arguments:
        return "deny", "xattr changes executable quarantine state"
    if program_name == "tccutil":
        return "deny", "tccutil changes privacy authorization state"
    if program_name == "diskutil":
        return "deny", "diskutil access is prohibited for agents"
    if program_name == "tmutil" and "delete" in lowered_arguments:
        return "deny", "tmutil deletes backup data"
    if program_name == "security":
        destructive_operations = {"delete-keychain", "export", "set-keychain-password"}
        if lowered_arguments and lowered_arguments[0] in destructive_operations:
            return "deny", "security changes or exports credential material"
        return "ask", "security accesses credential or trust state"
    return "", ""


def classify_linux_command(
    program_name: str,
    arguments: list[str],
) -> tuple[str, str]:
    """Classify one Linux-native command family."""
    lowered_arguments = [argument.casefold() for argument in arguments]
    if program_name in LINUX_STORAGE_DESTRUCTION:
        return "deny", f"{program_name} changes partition or filesystem storage"
    if program_name == "systemctl":
        mutating_operations = {"disable", "enable", "mask", "restart", "start", "stop"}
        if lowered_arguments and lowered_arguments[0] in mutating_operations:
            return "deny", "systemctl changes active or persistent service state"
        return "ask", "systemctl reads service state"
    if program_name == "crontab":
        if "-r" in lowered_arguments:
            return "deny", "crontab -r deletes scheduled persistence"
        return "", ""
    if program_name in LINUX_PACKAGE_MANAGERS:
        return "ask", f"{program_name} can change installed software"
    if program_name in ("iptables", "nft"):
        return "deny", f"{program_name} changes network security controls"
    if program_name in ("kubectl", "helm"):
        return "deny", f"{program_name} can change remote orchestration state"
    return "", ""


def classify_platform_command(
    platform_name: str,
    program: str,
    arguments: list[str],
) -> tuple[str, str]:
    """Return the host-specific behavior verdict for one named command."""
    program_name = normalize_program_name(program)
    if program_name in ("scp", "sftp", "rsync"):
        return classify_transfer_direction(arguments, platform_name=platform_name)
    if program_name in SENSITIVE_DISCOVERY_PROGRAMS:
        return "ask", f"{program_name} enumerates sensitive host state"
    if platform_name == "darwin":
        return classify_macos_command(program_name, arguments)
    if platform_name.startswith("linux"):
        return classify_linux_command(program_name, arguments)
    return "", ""
