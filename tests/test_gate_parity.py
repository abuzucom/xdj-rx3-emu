#!/usr/bin/env python3
"""Assert the Bash, PowerShell, and CMD gates reach the same verdicts.

Parity is a promise unless something checks it. This session watched
scripts/check_commit_message.py diverge between two repositories until a
merge commit exposed it, so the shared corpus is a test rather than a
convention: a fix landing in one gate and not the other fails here.

Each row pairs a command with its equivalent in the other shell. Where a
form exists in only one shell, the pair repeats the same string, which
still asserts that both gates read it identically.
"""
import importlib.util
import json
import os
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_corpus
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import _gate_core

REPO_ROOT = Path(__file__).resolve().parent.parent
BASH_HOOK = REPO_ROOT / "hooks" / "block_destructive_bash.py"
POWERSHELL_HOOK = REPO_ROOT / "hooks" / "block_destructive_powershell.py"
CMD_HOOK = REPO_ROOT / "hooks" / "block_destructive_cmd.py"
CORE_PATH = REPO_ROOT / "hooks" / "_gate_core.py"
_HOOK_WORKERS = {}


def hook_worker(hook: Path):
    """Return one process-local persistent worker for `hook`."""
    worker = _HOOK_WORKERS.get(hook)
    if worker is None:
        worker = gate_corpus.HookWorker(hook)
        _HOOK_WORKERS[hook] = worker
    return worker


def _decision(hook: Path, tool: str, command, mode: str = "default",
              cwd: str = "") -> str:
    """Return the permission decision one gate reaches for `command`."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "permission_mode": mode,
        "tool_input": {"command": command},
    }
    if cwd:
        payload["cwd"] = cwd
    _code, stdout, _stderr = hook_worker(hook).invoke(payload)
    try:
        return json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]
    except (ValueError, KeyError):
        return ""


def bash(command, mode: str = "default") -> str:
    return _decision(BASH_HOOK, "Bash", command, mode)


def powershell(command, mode: str = "default") -> str:
    return _decision(POWERSHELL_HOOK, "PowerShell", command, mode)


def cmd(command, mode: str = "default", cwd: str = "") -> str:
    return _decision(CMD_HOOK, "Cmd", command, mode, cwd)


class GitParityTest(unittest.TestCase):
    """git decisions come from the shared core, so they cannot differ."""

    COMMANDS = (
        "git push --force",
        "git push -f origin main",
        "git reset --hard",
        "git push --force-with-lease",
        "git push --force-with-lease=main:abc123",
        "git push --mirror",
        "git push --delete origin feat/x",
        "git push origin +HEAD:main",
        "git push origin :main",
        "git push --prune origin",
        "git commit --amend",
        "git rebase -i HEAD~3",
        "git filter-branch --tree-filter true HEAD",
        "git -C /repo push --force",
        "git status",
        "git log --oneline -5",
        "git push origin feat/x",
        "git push -u origin feat/x",
    )

    def test_all_gates_agree_on_git(self):
        for command in self.COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(bash(command), powershell(command))
                self.assertEqual(bash(command), cmd(command))

    def test_inline_config_and_repo_location_forms_agree(self):
        with tempfile.TemporaryDirectory() as root:
            git_dir = Path(root) / ".git"
            git_dir.mkdir()
            (git_dir / "config").write_text(
                '[diff "x"]\n\ttextconv = /tmp/evil\n', encoding="utf-8")
            commands = (
                "git -c core.pager=/tmp/evil status",
                "git -ccore.pager=/tmp/evil log",
                "git -c core.pager=/tmp/evil -c core.pager= status",
                "git --git-dir .git --work-tree . show HEAD",
                "git -C . diff",
            )
            for command in commands:
                with self.subTest(command=command):
                    self.assertEqual(
                        _decision(BASH_HOOK, "Bash", command, cwd=root),
                        _decision(POWERSHELL_HOOK, "PowerShell", command, cwd=root),
                    )
                    self.assertEqual(
                        _decision(BASH_HOOK, "Bash", command, cwd=root),
                        cmd(command, cwd=root),
                    )


class MalformedParityTest(unittest.TestCase):
    """All gates must fail closed on the same malformed inputs."""

    def test_non_string_commands_agree(self):
        for value in (None, 5, ["rm"], {"a": 1}):
            with self.subTest(value=value):
                self.assertEqual(bash(value), powershell(value))
                self.assertEqual(bash(value), cmd(value))

    def test_unattended_modes_agree(self):
        for mode in ("bypassPermissions", "dontAsk", "", "unknown-mode"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    bash("git push --force-with-lease", mode),
                    powershell("git push --force-with-lease", mode))
                self.assertEqual(
                    bash("git push --force-with-lease", mode),
                    cmd("git push --force-with-lease", mode))


class CmdParityTest(unittest.TestCase):
    """Shared destructive behavior reaches one verdict in all three gates."""

    CASES = (
        ("rm -rf build", "Remove-Item -Recurse build", "rd /s /q build", "ask"),
        ("rm -rf C:\\", "Remove-Item -Recurse C:\\", "rd /s /q C:\\", "deny"),
        (
            "echo x > tests/test_auth.py",
            "Write-Output x > tests/test_auth.py",
            "echo x > tests\\test_auth.py",
            "ask",
        ),
    )

    def test_shared_behavior_agrees(self):
        for bash_command, powershell_command, cmd_command, expected in self.CASES:
            with self.subTest(command=cmd_command):
                self.assertEqual(bash(bash_command), expected)
                self.assertEqual(powershell(powershell_command), expected)
                self.assertEqual(cmd(cmd_command), expected)


class BoundaryParityTest(unittest.TestCase):
    """Each boundary class, in both spellings, reaches the same verdict."""

    PAIRS = (
        ("true\nrm -rf /tmp/x", "Write-Output x\nRemove-Item -Recurse /tmp/x"),
        ("true; rm -rf /tmp/x", "Write-Output x; Remove-Item -Recurse /tmp/x"),
        ("true && rm -rf /tmp/x", "Test-Path x && Remove-Item -Recurse /tmp/x"),
        ("true || rm -rf /tmp/x", "Test-Path x || Remove-Item -Recurse /tmp/x"),
        ("echo x | xargs rm -rf /tmp/x", "Get-Process | Remove-Item -Recurse /tmp/x"),
        ("rm -rf /", "Remove-Item -Recurse /"),
        ("rm -rf $HOME", "Remove-Item -Recurse $HOME"),
        ("rm -rf ~", "Remove-Item -Recurse ~"),
        ("rm -rf /tmp/scratch", "Remove-Item -Recurse /tmp/scratch"),
        ("rm build.log", "Remove-Item build.log"),
        ("ls -la", "Get-ChildItem"),
        ("echo x > tests/test_auth.py", "Write-Output x > tests/test_auth.py"),
        ("echo x > src/app.js", "Write-Output x > src/app.js"),
        ("cmd /c rd /s /q C:/work", "cmd /c rd /s /q C:/work"),
        # Grouping that puts a command where a program name goes. The
        # PowerShell gate read its own & { ... } and the Bash gate did not
        # read { ... ; }; the Bash gate read $( ... ) and the PowerShell
        # gate did not. Each gap was invisible until both spellings sat
        # in one row.
        ("{ rm -rf /tmp/x; }", "& { Remove-Item -Recurse -Force /tmp/x }"),
        ("{ git push --force; }", "& { git push --force }"),
        ("echo `rm -rf /tmp/x`",
         "Write-Output $(Remove-Item -Recurse -Force /tmp/x)"),
        ("rm -rf build", "Remove-Item -Recurse -Force (Join-Path $a build)"),
        ("echo {a,b}.txt", "Write-Output {a,b}.txt"),
    )

    def test_equivalent_commands_agree(self):
        for bash_command, powershell_command in self.PAIRS:
            with self.subTest(bash=bash_command):
                self.assertEqual(bash(bash_command), powershell(powershell_command))


class PowerShellPolicyParityTest(unittest.TestCase):
    """Shared high-risk program decisions apply through either shell gate."""

    CASES = (
        ("Invoke-Expression 'Get-Date'", "deny"),
        ("pwsh -Command 'Get-ChildItem'", "deny"),
        ("Add-Type -TypeDefinition 'public class X {}'", "deny"),
        ("Set-MpPreference -DisableRealtimeMonitoring true", "deny"),
        ("Register-ScheduledTask -TaskName x -Action y", "deny"),
        ("Copy-Item source.database '\\\\server\\share'", "deny"),
        ("Start-Process notepad.exe", "ask"),
        ("Import-Module Pester", "ask"),
        ("Get-Credential", "ask"),
        ("Invoke-WebRequest https://example.test/data.json", "ask"),
        ("Get-ChildItem .", ""),
    )

    def test_shared_policy_commands_match_expected_verdicts(self):
        for command, expected in self.CASES:
            with self.subTest(command=command):
                self.assertEqual(bash(command), expected)
                self.assertEqual(powershell(command), expected)


class SharedCommandPolicyParityTest(unittest.TestCase):
    """Platform-independent command bans reach the same gate verdict."""

    DENIED = (
        "bash -lc 'git push --force'",
        "aws --version",
        "az account show",
        "azcopy --help",
        "cdk list",
        "packer version",
        "pulumi stack ls",
        "terraform validate",
        "tofu validate",
        "terragrunt --version",
        "gcloud auth list",
        "bq version",
        "gsutil version",
        "kubectl get pods",
        "helm list",
        "kustomize version",
        "oc whoami",
        "minikube status",
        "kind get clusters",
        "k9s version",
        "eksctl version",
        "crictl version",
        "ssh host.example.test",
        "scp source host.example.test:/tmp",
        "sftp host.example.test",
        "ssh-add -l",
        "ssh-agent -s",
        "ssh-keygen -l -f key.pub",
        "ssh-keyscan host.example.test",
        "sshd -T",
        "telnet host.example.test",
        "ftp host.example.test",
        "lftp host.example.test",
        "iptables -L",
        "ip6tables -L",
        "nft list ruleset",
        "ufw status",
        "Get-AzVM",
        "Get-NetFirewallRule",
        "mkfs --help",
        "mkfs.ext4 /dev/example",
        "mke2fs /dev/example",
        "newfs /dev/example",
        "userdel example-user",
        "groupdel example-group",
        "deluser example-user",
        "delgroup example-group",
        "Remove-LocalUser example-user",
        "Remove-LocalGroup example-group",
        "diskpart /s plan.txt",
        "format D:",
        "winrm get winrm/config",
        "Clear-EventLog Application",
        "Remove-EventLog Application",
        "sfc /verifyonly",
        "DISM /Online /Get-Packages",
        "bcdedit /enum",
        "bootrec /scanos",
        "bootsect /help",
        "mbr2gpt /validate",
        "manage-bde -status",
        "diskutil list",
        "unlink build.log",
        "shred --help",
        "wipe --help",
        "fdisk -l",
        "gdisk -l /dev/example",
        "parted -l",
        "sfdisk -l",
        "cfdisk --help",
        "pvremove --help",
        "lvremove --help",
        "lvreduce --help",
        "lvconvert --help",
        "swapoff --help",
        "grub-install --version",
        "update-grub --help",
        "grub-mkconfig --help",
        "usermod --help",
        "passwd --status example-user",
        "gpasswd --help",
        "insmod --help",
        "modprobe --show-depends example",
        "rmmod --help",
        "gpt destroy disk0",
        "log erase --all",
        "cat main.tf",
        "Get-Content .kube/config",
        "type charts/app/Chart.yaml",
    )

    def test_unconditional_command_bans_match(self):
        for command in self.DENIED:
            with self.subTest(command=command):
                self.assertEqual(bash(command), "deny")
                self.assertEqual(powershell(command), "deny")
                self.assertEqual(cmd(command), "deny")

    def test_subcommand_only_bans_keep_read_forms(self):
        for command in ("gpt show disk0", "log show --last 1m"):
            with self.subTest(command=command):
                self.assertEqual(bash(command), powershell(command))
                self.assertEqual(bash(command), cmd(command))


class CurlPolicyParityTest(unittest.TestCase):
    """Every shell uses one curl transfer classifier."""

    UPLOADS = (
        "curl -T report.data https://example.test/upload",
        "curl -F file=@report.data https://example.test/upload",
        "curl -d value https://example.test/upload",
        "curl --upload-file=report.data https://example.test/upload",
        "curl --data-ascii=value https://example.test/upload",
        "curl --data-binary=value https://example.test/upload",
        "curl --data-raw=value https://example.test/upload",
        "curl --data-urlencode=value https://example.test/upload",
        "curl --form-string=value https://example.test/upload",
        "curl --json={} https://example.test/upload",
    )

    def test_uploads_deny_in_every_gate(self):
        for command in self.UPLOADS:
            with self.subTest(command=command):
                self.assertEqual(bash(command), "deny")
                self.assertEqual(powershell(command), "deny")
                self.assertEqual(cmd(command), "deny")

    def test_downloads_ask_in_every_gate(self):
        command = "curl https://example.test/data"
        self.assertEqual(bash(command), "ask")
        self.assertEqual(powershell(command), "ask")
        self.assertEqual(cmd(command), "ask")


class GitHubCliSafetyParityTest(unittest.TestCase):
    """High-risk GitHub CLI operations share one cross-shell verdict."""

    WRAPPER = "python scripts/trusted_gh.py run "
    CASES = (
        (WRAPPER + "agent-task create", "deny"),
        (WRAPPER + "pr -R OWNER/REPO merge 12", "deny"),
        (WRAPPER + "pr -ROWNER/REPO merge 12", "deny"),
        (WRAPPER + "pr --repo=OWNER/REPO merge 12", "deny"),
        (WRAPPER + "pr -- merge 12", "deny"),
        (WRAPPER + "auth --hostname github.example token", "deny"),
        (WRAPPER + "auth -h github.example token", "deny"),
        (WRAPPER + "auth -u octocat token", "deny"),
        (WRAPPER + "repo --repo OWNER/REPO clone OWNER/REPO", "deny"),
        (WRAPPER + "repo --repo OWNER/REPO edit --visibility public", "deny"),
        (WRAPPER + "agent create", "deny"),
        (WRAPPER + "agents list", "deny"),
        (WRAPPER + "agent-tasks view 1", "deny"),
        (WRAPPER + "cs list", "deny"),
        (WRAPPER + "ext list", "deny"),
        (WRAPPER + "extensions list", "deny"),
        (WRAPPER + "skills list", "deny"),
        (WRAPPER + "alias set x status", "deny"),
        (WRAPPER + "config set prompt disabled", "deny"),
        (WRAPPER + "codespace list", "deny"),
        (WRAPPER + "completion bash", "deny"),
        (WRAPPER + "copilot", "deny"),
        (WRAPPER + "extension list", "deny"),
        (WRAPPER + "gpg-key list", "deny"),
        (WRAPPER + "preview", "deny"),
        (WRAPPER + "release list", "deny"),
        (WRAPPER + "secret list", "deny"),
        (WRAPPER + "skill list", "deny"),
        (WRAPPER + "ssh-key list", "deny"),
        (WRAPPER + "variable list", "deny"),
        (WRAPPER + "pr lock 1", "deny"),
        (WRAPPER + "repo create OWNER/REPO", "deny"),
        (WRAPPER + "run cancel 1", "deny"),
        (WRAPPER + "auth login", "deny"),
        (WRAPPER + "repo delete OWNER/REPO", "deny"),
        (WRAPPER + "-R OWNER/REPO release delete TAG --cleanup-tag", "deny"),
        (WRAPPER + "run delete 123", "deny"),
        (WRAPPER + "secret delete NAME --repo OWNER/REPO", "deny"),
        (WRAPPER + "variable delete NAME --repo OWNER/REPO", "deny"),
        (WRAPPER + "api --method DELETE repos/OWNER/REPO", "deny"),
        (WRAPPER + "api -X PATCH repos/OWNER/REPO", "deny"),
        (WRAPPER + "api --method=POST repos/OWNER/REPO", "deny"),
        (WRAPPER + "api repos/OWNER/REPO -f name=value", "deny"),
        (WRAPPER + "api graphql -f query=mutation", "deny"),
        (WRAPPER + "pr merge 12 --admin", "deny"),
        (WRAPPER + "--repo OWNER/REPO repo edit --visibility public", "deny"),
        (WRAPPER + "auth token", "deny"),
        (WRAPPER + "auth refresh --scopes delete_repo", "deny"),
        (WRAPPER + "auth refresh --scopes repo", "deny"),
        (WRAPPER + "auth login", "deny"),
        (WRAPPER + "auth logout", "deny"),
        (WRAPPER + "auth refresh", "deny"),
        (WRAPPER + "auth setup-git", "deny"),
        (WRAPPER + "auth switch", "deny"),
        (WRAPPER + "pr merge 12", "deny"),
        (WRAPPER + "pr merge 12 --delete-branch", "deny"),
        (WRAPPER + "repo archive OWNER/REPO", "deny"),
        (WRAPPER + "repo edit OWNER/REPO --visibility private", "ask"),
        (WRAPPER + "repo view OWNER/REPO", ""),
        (WRAPPER + "release view TAG", "deny"),
        (WRAPPER + "run view 123", ""),
        (WRAPPER + "secret list --repo OWNER/REPO", "deny"),
        (WRAPPER + "variable list --repo OWNER/REPO", "deny"),
        (WRAPPER + "api --method GET repos/OWNER/REPO", ""),
        (WRAPPER + "pr checks 12", ""),
        (WRAPPER + "auth status", ""),
        ("gh repo view OWNER/REPO", "deny"),
        # Rule 17. These owners never match a real origin owner, so the
        # verdict holds whether or not the gate reads one, which keeps the
        # row independent of the checkout running it.
        (WRAPPER + "pr comment 16 --repo outside-owner-x9/repo", "ask"),
        (WRAPPER + "pr create --repo outside-owner-x9/repo", "ask"),
        (WRAPPER + "pr review 16 --repo outside-owner-x9/repo", "ask"),
        (WRAPPER + "issue create -R outside-owner-x9/repo", "ask"),
        (WRAPPER + "issue comment 3 --repo outside-owner-x9/repo", "ask"),
        (WRAPPER + "repo fork outside-owner-x9/repo", "deny"),
        (WRAPPER + "release create v1 --repo outside-owner-x9/repo", "deny"),
        (WRAPPER + "pr view 16 --repo outside-owner-x9/repo", ""),
        (WRAPPER + "pr diff 16 --repo outside-owner-x9/repo", ""),
        (WRAPPER + "issue list --repo outside-owner-x9/repo", ""),
        (WRAPPER + "repo clone outside-owner-x9/repo", "deny"),
        # gh accepts HOST/OWNER/REPO and full URLs, so those forms reach the
        # same verdict as the bare owner/name form.
        (WRAPPER + "pr create --repo github.com/outside-owner-x9/repo", "ask"),
        (WRAPPER + "pr comment 1 --repo https://github.com/outside-owner-x9/repo",
         "ask"),
        (WRAPPER + "issue create -R https://github.com/outside-owner-x9/repo",
         "ask"),
        (WRAPPER + "repo fork https://github.com/outside-owner-x9/repo", "deny"),
        (WRAPPER + "pr view 1 --repo https://github.com/outside-owner-x9/repo",
         ""),
        # A lookalike host is unreadable, and an unreadable explicit target is
        # not clearance.
        (WRAPPER + "pr create --repo https://attacker-github.com/owner/repo",
         "ask"),
        (WRAPPER + "pr create --repo not-a-repository", "ask"),
    )

    def test_github_cli_safety_matches(self):
        for command, expected in self.CASES:
            with self.subTest(command=command):
                self.assertEqual(bash(command), expected)
                self.assertEqual(powershell(command), expected)
                self.assertEqual(cmd(command), expected)

    def test_git_and_http_substitutes_deny(self):
        commands = (
            "git clone https://github.com/OWNER/REPO.git",
            "git clone https://raw.githubusercontent.com/OWNER/REPO/main/file",
            "git clone git@github.com:OWNER/REPO.git",
            "git clone github.com:OWNER/REPO.git",
            "curl github.com:443/OWNER/REPO",
            "git remote -v",
            "git fetch origin refs/pull/12/head",
            "curl https://api.github.com/repos/OWNER/REPO",
            "wget https://raw.githubusercontent.com/OWNER/REPO/main/file",
            "hub pr list",
            "git-credential-manager erase",
            "git-credential-manager-core configure",
            "start https://github.com/login",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(bash(command), "deny")
                self.assertEqual(powershell(command), "deny")
                self.assertEqual(cmd(command), "deny")

    def test_shared_browser_and_credential_denials(self):
        self.assertEqual(
            _gate_core.github_routing_verdict(
                "start-process", ["https://github.com/login"], "")[0],
            "deny")
        self.assertEqual(
            _gate_core.github_routing_verdict(
                "git-credential-manager", ["erase"], "")[0],
            "deny")

    def test_github_target_matching_rejects_lookalikes(self):
        commands = (
            "git clone https://not-github.com/OWNER/REPO.git",
            "git clone https://example.com/github.com/OWNER/REPO.git",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(bash(command), "")
                self.assertEqual(powershell(command), "")
                self.assertEqual(cmd(command), "")

    def test_marked_git_fallback_asks(self):
        command = (
            "git -c agents.githubFallback=confirmed clone "
            "https://github.com/OWNER/REPO.git"
        )
        self.assertEqual(bash(command), "ask")
        self.assertEqual(powershell(command), "ask")
        self.assertEqual(cmd(command), "ask")

    def test_normal_local_git_transport_stays_available(self):
        for command in ("git fetch origin main", "git pull", "git push origin HEAD"):
            with self.subTest(command=command):
                self.assertEqual(bash(command), "")
                self.assertEqual(powershell(command), "")
                self.assertEqual(cmd(command), "")


class CoreOwnershipTest(unittest.TestCase):
    """Every decision function lives in the core, not in one gate."""

    def test_git_environment_classifier_is_public(self):
        spec = importlib.util.spec_from_file_location("gate_core_public", CORE_PATH)
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        self.assertTrue(core.is_relevant_git_environment("GIT_DIR"))
        self.assertFalse(core.is_relevant_git_environment("PATH"))

    def test_no_gate_defines_its_own_verdict_helpers(self):
        shared = ("delete_verdict", "git_verdict", "push_verdict",
                  "is_root_target", "strongest", "is_test_path",
                  "cmd_delete_verdict", "curl_transfer_verdict",
                  "is_shell_payload_flag", "prohibited_command_verdict", "sanitize",
                  "is_relevant_git_environment")
        for hook in (BASH_HOOK, POWERSHELL_HOOK, CMD_HOOK):
            source = hook.read_text(encoding="utf-8")
            for name in shared:
                with self.subTest(hook=hook.name, function=name):
                    self.assertNotIn(
                        f"def {name}(", source,
                        f"{hook.name} redefines {name}; it belongs to the core "
                        "so the gates cannot drift")


if __name__ == "__main__":
    unittest.main()
