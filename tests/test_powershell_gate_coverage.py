"""Exercise PowerShell payload paths used by gate coverage."""

from __future__ import annotations

import base64
import unittest

from hooks import _gate_core
from hooks import block_destructive_powershell


def encode_powershell_command(command: str) -> str:
    """Return a Base64 UTF-16LE representation of a PowerShell command."""
    payload = command.encode("utf-16le")
    return base64.b64encode(payload).decode("ascii")


class PowerShellPayloadCoverageTest(unittest.TestCase):
    """Reach strict decode and shell interpreter classification branches."""

    def test_non_unc_path_returns_false_without_filesystem_access(self) -> None:
        self.assertFalse(_gate_core._is_windows_unc_path("ordinary.txt"))

    def test_extended_unc_path_uses_unc_prefix(self) -> None:
        self.assertTrue(
            _gate_core._is_windows_unc_path(
                r"\\?\UNC\server\share\screen.bin"
            )
        )

    def test_extended_drive_path_is_not_a_unc_path(self) -> None:
        self.assertFalse(
            _gate_core._is_windows_unc_path(r"\\?\C:\screen.bin")
        )

    def test_decode_accepts_utf16le_and_rejects_malformed_payloads(self) -> None:
        valid_payload = encode_powershell_command("Get-Date")
        odd_length_payload = base64.b64encode(b"x").decode("ascii")
        invalid_utf16_payload = base64.b64encode(b"\x00\xd8").decode("ascii")

        self.assertEqual(
            _gate_core.decode_powershell_command(valid_payload),
            ("Get-Date", ""),
        )
        self.assertEqual(
            _gate_core.decode_powershell_command("not-base64!"),
            (None, "PowerShell EncodedCommand is not strict Base64"),
        )
        self.assertEqual(
            _gate_core.decode_powershell_command(odd_length_payload),
            (None, "PowerShell EncodedCommand has an odd UTF-16LE byte length"),
        )
        self.assertEqual(
            _gate_core.decode_powershell_command(invalid_utf16_payload),
            (None, "PowerShell EncodedCommand is not valid UTF-16LE"),
        )

    def test_payload_parser_handles_encoded_and_command_flags(self) -> None:
        valid_payload = encode_powershell_command("Get-Date")

        self.assertEqual(
            _gate_core.powershell_payload(["-EncodedCommand", valid_payload]),
            ("command", "Get-Date"),
        )
        self.assertEqual(
            _gate_core.powershell_payload(["-EncodedCommand"]),
            ("deny", "PowerShell EncodedCommand has no Base64 payload"),
        )
        self.assertEqual(
            _gate_core.powershell_payload(["-Command", "Get-Date"]),
            ("command", "Get-Date"),
        )
        self.assertEqual(
            _gate_core.powershell_payload(["-Command"]),
            ("", ""),
        )

    def test_interpreter_verdict_reaches_powershell_and_shell_payloads(self) -> None:
        encoded = encode_powershell_command("Get-Date")

        self.assertEqual(
            block_destructive_powershell._interpreter_verdict(
                "powershell",
                ["-EncodedCommand", encoded],
            )[0],
            "deny",
        )
        self.assertEqual(
            block_destructive_powershell._interpreter_verdict(
                "bash",
                ["-c", "echo ready"],
            )[0],
            "deny",
        )
        self.assertEqual(
            block_destructive_powershell._interpreter_verdict("bash", ["-c"])[0],
            "deny",
        )
        self.assertEqual(
            block_destructive_powershell._interpreter_verdict("bash", ["--help"]),
            ("", ""),
        )

    def test_nested_interpreter_depth_fails_closed(self) -> None:
        verdict = block_destructive_powershell._interpreter_verdict(
            "bash",
            ["--help"],
            depth=block_destructive_powershell.MAX_WRAPPER_DEPTH,
        )

        self.assertEqual(verdict[0], "deny")


if __name__ == "__main__":
    unittest.main()
