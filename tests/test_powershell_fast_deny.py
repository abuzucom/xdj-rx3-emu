"""Cover early return for an established PowerShell deny verdict."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from hooks import _gate_core
from hooks import block_destructive_powershell


class PowerShellFastDenyTest(unittest.TestCase):
    """Ensure an existing deny skips redundant infrastructure path lookup."""

    def test_unc_file_write_keeps_deny_without_filesystem_resolution(self) -> None:
        with patch.object(
            _gate_core,
            "infrastructure_path_verdict",
            side_effect=AssertionError("denied commands must short-circuit"),
        ):
            verdict = block_destructive_powershell.classify("Copy-Item source.database '\\\\server\\share'")

        self.assertEqual(
            verdict,
            ("deny", "a file operation reaches a remote UNC path"),
        )


class UncInfrastructurePathTest(unittest.TestCase):
    """Use lexical path checks for remote shares."""

    def test_unc_write_target_skips_repository_path_resolution(self) -> None:
        with patch.object(
            _gate_core.os.path,
            "realpath",
            side_effect=AssertionError("UNC writes must not resolve network shares"),
        ):
            protected = _gate_core._protected_path(
                r"\\server\share\ordinary.txt", r"C:\repo"
            )

        self.assertFalse(protected)

    def test_unc_path_avoids_filesystem_resolution(self) -> None:
        with patch.object(
            _gate_core.os.path,
            "realpath",
            side_effect=AssertionError("UNC checks must not contact the share"),
        ):
            protected = _gate_core.is_protected_infrastructure_path(
                r"\\server\share\ordinary.txt"
            )

        self.assertFalse(protected)

    def test_unc_credential_path_stays_protected(self) -> None:
        with patch.object(
            _gate_core.os.path,
            "realpath",
            side_effect=AssertionError("UNC checks must not contact the share"),
        ):
            protected = _gate_core.is_protected_infrastructure_path(
                r"\\server\share\.aws\credentials"
            )

        self.assertTrue(protected)

    def test_unknown_unc_manifest_fails_closed_without_reading_share(self) -> None:
        with patch.object(
            _gate_core,
            "_infrastructure_manifest_text",
            side_effect=AssertionError("UNC manifest must not be read"),
        ):
            protected = _gate_core.is_protected_infrastructure_path(
                r"\\server\share\application.yaml"
            )

        self.assertTrue(protected)


if __name__ == "__main__":
    unittest.main()
