#!/usr/bin/env python3
"""Shared decision logic for the shell gates.

Not part of AGENTS.md, which stays tool-agnostic and is synced to non-Claude
tools verbatim. This module holds everything the Bash, PowerShell, and CMD gates
decide identically, so the gates cannot drift: payload reading and field-type
validation, the deny and ask emission, the ask-to-deny downgrade for an
unattended session, root-target detection, and the whole git classification.

Each gate keeps only what its shell spells differently: how a command
tokenizes, where one statement ends, which wrappers and redirections lead a
    command, and how a delete's flags are written. All gates call this module
    for shared decisions.

A hook run as `python3 .../hooks/<gate>.py` gets `hooks/` as `sys.path[0]`,
so `import _gate_core` resolves with no packaging. A gate that cannot import
this module must fail closed rather than crash, because Claude Code treats a
non-zero exit other than 2 as a non-blocking error, which waves the command
through.
"""
import base64
import binascii
import fnmatch
import importlib.util
import json
import ntpath
import os
import posixpath
import shlex
import subprocess
import sys
import threading
import urllib.parse
from functools import lru_cache
from pathlib import Path

INTERACTIVE_MODES = frozenset({"default", "plan", "acceptEdits", "auto"})
AMBIGUOUS_MARKERS = ("$", "`")
FILESYSTEM_ROOTS = frozenset({"//", "/*"})
# A UNC share root is \\\\server\\share, so at most two components; a drive
# root includes a separator, such as C:\\. Both name a whole volume.
UNC_SHARE_ROOT_PARTS = 2
DRIVE_ROOT_LENGTH = 2
MAX_GIT_CONFIG_COUNT = 1000
MAX_GIT_ALIAS_DEPTH = 10
GITHUB_DOMAINS = frozenset({"github.com", "githubusercontent.com"})
CONFIG_READ_TIMEOUT_SECONDS = 5
CONFIG_READ_ENVIRONMENT = frozenset({
    "PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "XDG_CONFIG_HOME", "LANG", "LC_ALL", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_COUNT", "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
})
GIT_SHORT_OPTION_VALUE_INDEX = 2
# section.subsection.key, the only shape a driver name takes.
GIT_CONFIG_SUBSECTION_PARTS = 3
# Latin-1 renders as \\xNN; everything above it needs \\uNNNN.
MAX_LATIN1_CODEPOINT = 0xFF
# A forge delete reads as "<noun> delete <target>", so two words minimum.
FORGE_DELETE_MIN_WORDS = 2
# Directories whose removal takes the machine with them. These deny rather
# than ask: putting "delete /etc?" in front of a person is not consent, it
# is an invitation to a mistake nobody can undo.
SYSTEM_ROOTS = frozenset({
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32",
    "/lib64", "/libx32", "/media", "/mnt", "/opt", "/proc", "/root",
    "/run", "/sbin", "/srv", "/sys", "/temp", "/tmp", "/usr", "/var",
    "/system", "/library", "/applications", "/volumes", "/users",
    "/private", "/cores",
})

HOME_PREFIXES = ("~", "$HOME", "${HOME}", "$env:USERPROFILE",
                 "$Env:USERPROFILE", "%USERPROFILE%", "%HOMEPATH%")
GIT_VALUE_OPTIONS = frozenset({
    "-C", "-c", "--exec-path", "--git-dir", "--work-tree", "--namespace",
    "--config-env", "--super-prefix",
})
HISTORY_SUBCOMMANDS = {
    "rebase": "git rebase: this rewrites history",
    "filter-branch": "git filter-branch: this rewrites history",
    "filter-repo": "git filter-repo: this rewrites history",
}
STRENGTH = {"": 0, "ask": 1, "deny": 2}


def require_str(value):
    """Return `value` when it is a string, otherwise None.

    A gate that reads a field without checking its type crashes on a number
    or a list, and a crash is an exit code Claude Code ignores.
    """
    return value if isinstance(value, str) else None


def is_ambiguous(token: str) -> bool:
    """Return True if the token hides its value behind shell expansion."""
    return any(marker in token for marker in AMBIGUOUS_MARKERS)


def is_short_group(token: str) -> bool:
    """Return True if the token is a bundle of short flags such as -Rf."""
    return token.startswith("-") and not token.startswith("--") and len(token) > 1


def long_option_matches(token: str, option: str) -> bool:
    """Return True if `token` names `option` or a prefix git or getopt accepts.

    git parse-options and GNU getopt_long accept any unique prefix of a long
    option, so `--forc` is `--force`. An ambiguous prefix makes the tool
    exit with an error, so reading it as the gated option only over-asks.
    """
    name = token.split("=", 1)[0]
    return (name.startswith("--") and len(name) > len("--")
            and option.startswith(name))


def _is_drive_root(token: str) -> bool:
    """Return True for the root of any drive or share.

    C:\\, D:/, and \\\\server\\share name a whole volume. A bare C: names
    the current directory on that drive and is not a volume root.
    """
    candidate = token.strip().strip('"').strip("'")
    if candidate.startswith("\\\\") or candidate.startswith("//"):
        parts = [part for part in candidate.replace("\\", "/").split("/") if part]
        return len(parts) <= UNC_SHARE_ROOT_PARTS
    normalized = ntpath.normpath(candidate.replace("/", "\\"))
    return (len(normalized) == DRIVE_ROOT_LENGTH + 1
            and normalized[0].isalpha()
            and normalized[1:] == ":\\")


def _is_system_root(token: str) -> bool:
    """Return True if the token names a system directory itself.

    A path inside one is an ordinary recursive delete and still asks. The
    directory itself is not a target any workflow has.
    """
    candidate = token.strip().strip('"').strip("'").replace("\\", "/")
    if not candidate:
        return False
    # posixpath, not os.path: on Windows os.path.normpath("/etc") returns
    # "\\etc", so a check anchored on a leading slash rejects every POSIX
    # system root on the platform where a Windows agent runs.
    normalized = posixpath.normpath(candidate).lower()
    if not normalized.startswith("/"):
        return False
    return normalized in SYSTEM_ROOTS


def is_root_target(token: str) -> bool:
    """Return True if the token names the filesystem root or a home directory."""
    if _is_system_root(token) or _is_drive_root(token):
        return True
    return token in FILESYSTEM_ROOTS or token.startswith(HOME_PREFIXES)


def strongest(first: tuple, second: tuple) -> tuple:
    """Return whichever (decision, reason) pair carries the stronger decision."""
    return second if STRENGTH[second[0]] > STRENGTH[first[0]] else first


def delete_verdict(recursive: bool, operands: list, noun: str) -> tuple:
    """Return (decision, reason) for a recursive delete over `operands`.

    Each shell parses its own flags and hands the result here, so `rm -Rf`
    and `Remove-Item -Recurse -Force` reach the same answer. `noun` carries
    the caller's own wording, so each gate names the command its user typed.
    """
    if not recursive:
        return "", ""
    if any(is_root_target(operand) for operand in operands):
        return "deny", f"{noun} targeting / or the home directory"
    return "ask", f"{noun}: Rule 2 gates every target, including one this session created"


def git_subcommand(args: list) -> tuple:
    """Return (subcommand, remaining args) after skipping git's global options."""
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("-"):
            return token, args[index + 1:]
        if token.split("=", 1)[0] in GIT_VALUE_OPTIONS and "=" not in token:
            index += 2
        else:
            index += 1
    return "", []


def _push_flag_reason(token: str, name: str) -> str:
    """Return why a git push flag needs consent, or an empty string."""
    if name.startswith("--force") or long_option_matches(name, "--force-with-lease"):
        return "git push with a --force variant: a lease is not consent to rewrite pushed history"
    if long_option_matches(name, "--mirror"):
        return "git push --mirror: this overwrites and deletes published refs"
    if long_option_matches(name, "--delete") or (is_short_group(token) and "d" in token):
        return "git push --delete: this removes a published ref"
    if long_option_matches(name, "--prune"):
        return "git push --prune: this deletes every remote ref with no local counterpart"
    return ""


def _push_operand_reason(token: str) -> str:
    """Return why a git push refspec needs consent, or an empty string.

    A refspec deletes or rewrites without carrying any flag: `+HEAD:main`
    forces, and `:main` pushes an empty source, which deletes the remote ref.
    """
    if token.startswith("+"):
        return "git push with a forced refspec: this rewrites pushed history"
    if token.startswith(":"):
        return "git push with an empty-source refspec: this deletes the remote ref"
    return ""


def push_ask_reason(token: str, name: str) -> str:
    """Return why a single git push argument needs consent, or an empty string."""
    if token.startswith("-"):
        return _push_flag_reason(token, name)
    return _push_operand_reason(token)


def push_verdict(args: list) -> tuple:
    """Return (decision, reason) for a git push argument list."""
    ask = ""
    for token in args:
        name = token.split("=", 1)[0]
        if long_option_matches(name, "--force") or (is_short_group(token) and "f" in token):
            # Asked rather than refused, at the user's direction. A refusal
            # offers no way to consent, and the pushed-history rule is about
            # consent rather than impossibility.
            return "ask", ("git push --force rewrites published history, so "
                           "anyone who has fetched this branch keeps commits "
                           "that no longer exist upstream")
        ask = ask or push_ask_reason(token, name)
    return ("ask", ask) if ask else ("", "")


READ_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "blame",
                              "grep", "shortlog", "whatchanged"})


KNOWN_SUBCOMMANDS = frozenset({
    "push", "reset", "commit", "rebase", "filter-branch", "add", "checkout",
    "switch", "restore", "merge", "fetch", "pull", "clone", "branch", "tag",
    "remote", "stash", "config", "init", "rm", "mv", "cherry-pick", "revert",
    "bisect", "worktree", "submodule", "describe", "rev-parse", "ls-files",
    "clean",
    "cat-file", "hash-object", "check-attr", "replace", "update-index",
    "apply", "am", "format-patch", "archive", "gc", "reflog", "notes",
}) | READ_SUBCOMMANDS


def _git_alias_verdict(subcommand: str, rest: list, cwd: str) -> tuple:
    """Return (decision, reason) for a subcommand git does not define."""
    expansion = resolve_alias(cwd, subcommand)
    if not expansion:
        return "ask", (f"git {sanitize(subcommand)}: not a git subcommand "
                       "and not a declared alias, so the gate cannot tell "
                       "what this runs")
    if expansion.startswith("!"):
        # A shell alias. Classify the text; never expand or run it.
        return "ask", (f"git {sanitize(subcommand)} is a shell alias, "
                       "which the gate cannot read as a git command")
    return git_verdict(expansion.split() + rest, cwd)


def _git_resolution_verdict(subcommand: str, rest: list, args: list,
                            cwd: str, environment: list) -> tuple:
    """Return a verdict for a subcommand needing resolution, else None.

    None means git defines the subcommand and it is not a read, so the
    caller reads its flags.
    """
    if not subcommand or is_ambiguous(subcommand):
        return "ask", ("git with an unresolved subcommand: the gate cannot "
                       "tell what this runs")
    if subcommand in READ_SUBCOMMANDS:
        return git_read_verdict(args, cwd, environment)
    if subcommand in KNOWN_SUBCOMMANDS:
        return None
    return _git_alias_verdict(subcommand, rest, cwd)


def _git_branch_verdict(rest: list) -> tuple:
    """Return (decision, reason) for a git branch argument list."""
    # Case carries the meaning here: -d refuses an unmerged branch, -D
    # discards it. Lowercasing first would gate the safe one.
    short_flags = "".join(token[1:] for token in rest if is_short_group(token))
    deletes = "d" in short_flags or any(
        long_option_matches(token, "--delete") for token in rest)
    forces = "f" in short_flags or any(
        long_option_matches(token, "--force") for token in rest)
    if "D" in short_flags or (deletes and forces):
        return "ask", ("git branch -D discards a branch whose commits "
                       "may not be merged anywhere else")
    return "", ""


def _git_clean_verdict(rest: list) -> tuple:
    """Return (decision, reason) for a git clean argument list."""
    short_flags = "".join(token[1:] for token in rest if is_short_group(token))
    if "n" in short_flags or any(
            long_option_matches(token, "--dry-run") for token in rest):
        return "", ""
    return "ask", ("git clean removing untracked files, over a set "
                   "decided at run time")


def _git_flag_verdict(subcommand: str, rest: list) -> tuple:
    """Return (decision, reason) for the subcommands one flag decides."""
    if subcommand == "reset" and any(
            long_option_matches(token, "--hard") for token in rest):
        return "deny", "git reset --hard"
    if subcommand == "commit" and any(
            long_option_matches(token, "--amend") for token in rest):
        return "ask", "git commit --amend: this rewrites a commit"
    if subcommand in HISTORY_SUBCOMMANDS:
        return "ask", HISTORY_SUBCOMMANDS[subcommand]
    return "", ""


# Subcommands whose whole argument list decides the verdict, rather than
# one flag. push stays out: it runs before the alias check below.
GIT_SUBCOMMAND_READERS = {
    "branch": _git_branch_verdict,
    "clean": _git_clean_verdict,
}


def git_verdict(args: list, cwd: str = "", environment: list = None) -> tuple:
    """Return (decision, reason) for a git argument list."""
    subcommand, rest = git_subcommand(args)
    resolved = _git_resolution_verdict(
        subcommand, rest, args, cwd, environment or [])
    if resolved is not None:
        return resolved
    if subcommand == "push":
        return push_verdict(rest)
    alias_decision = alias_verdict("git", [subcommand] + rest)
    if alias_decision[0]:
        return alias_decision
    reader = GIT_SUBCOMMAND_READERS.get(subcommand)
    if reader:
        return reader(rest)
    return _git_flag_verdict(subcommand, rest)


def unparseable_verdict(command: str, keywords: tuple) -> tuple:
    """Fail closed when a command naming one of `keywords` will not tokenize.

    A command the gate cannot parse but which names a drive root or a
    system directory denies rather than asks. `rm -rf C:\\` ends in an
    unterminated escape, and no reading of it is one to put to a person.
    """
    if not any(keyword in command for keyword in keywords):
        return "", ""
    words = command.replace("\\", "/").split()
    for word in words:
        base = os.path.basename(word).lower().removesuffix(".exe")
        if base.split(".", 1)[0] in ALWAYS_DESTRUCTIVE:
            return "deny", (f"this command could not be parsed and names "
                            f"{sanitize(base)}, which only partitions, "
                            "formats, or wipes")
        if is_root_target(word) or is_root_target(word.rstrip("/") + "/"):
            return "deny", ("this command could not be parsed and names a "
                            "root or system directory")
    return "ask", "this command could not be parsed, so the gate cannot clear it"


def read_payload(empty_is_session_start: bool = False):
    """Return the stdin payload, or None when it cannot be parsed.

    `empty_is_session_start` yields an empty dict for empty stdin, which
    is how a SessionStart invocation arrives. Without it that hook would
    deny every session start. A gate that answers "fine" to input it
    could not read is worse than no gate, so anything else that will not
    parse is still None.
    """
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return None
    if empty_is_session_start and not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def emit(gate: str, decision: str, reason: str) -> int:
    """Print the gate's decision and return the exit code it needs."""
    message = f"blocked by hooks/{gate}: {reason}"
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": message,
        }
    }
    print(json.dumps(output))
    if decision == "deny":
        print(message, file=sys.stderr)
        return 2
    return 0


def decide(gate: str, payload: dict, decision: str, reason: str) -> int:
    """Emit the decision, turning an ask nobody can answer into a deny."""
    if decision == "deny":
        return emit(gate, "deny", reason)
    if require_str(payload.get("permission_mode")) in INTERACTIVE_MODES:
        return emit(gate, "ask", reason)
    return emit(gate, "deny", f"{reason}. No interactive session is available "
                              f"to consent (permission_mode {mode_label(payload)}).")


MAX_REASON_VALUE = 160


def mode_label(payload: dict) -> str:
    """Return the payload's permission_mode as text safe to put in a prompt.

    INTERACTIVE_MODES is a fixed list, so an interactive mode Claude Code
    adds later denies here. Naming the value is what separates that from a
    genuinely unattended session, which otherwise read identically.
    """
    mode = require_str(payload.get("permission_mode"))
    return sanitize(mode) if mode is not None else "absent"


def sanitize(value) -> str:
    """Render untrusted text as printable ASCII on a single line.

    An allowlist, not a strip. Zero-width characters, bidi overrides, and
    Unicode tag characters are not control characters, and they render
    invisibly or reverse the text around them, so a denylist of known-bad
    codepoints misses the cases that matter most. These values reach two
    readers who cannot afford to be misled: the human deciding on the
    permission prompt, and the model reading stderr as tool output.
    """
    rendered = []
    for character in str(value):
        if " " <= character <= "~":
            rendered.append(character)
        elif ord(character) <= MAX_LATIN1_CODEPOINT:
            rendered.append(f"\\x{ord(character):02x}")
        else:
            rendered.append(f"\\u{ord(character):04x}")
    text = "".join(rendered)
    if len(text) > MAX_REASON_VALUE:
        return text[:MAX_REASON_VALUE] + "...[truncated]"
    return text


def project_dir(payload: dict) -> str:
    """Return the repository root, preferring Claude Code's own variable."""
    return (os.environ.get("CLAUDE_PROJECT_DIR")
            or payload.get("cwd")
            or os.getcwd())


def policy_root() -> str:
    """Return the repository root containing the installed policy hooks."""
    return os.path.realpath(os.path.join(os.path.dirname(__file__), os.pardir))


def resolved_under(root: str, *parts: str):
    """Return the joined path when it stays under `root`, else None.

    Joining a payload-supplied directory with a fixed relative path and
    running the result executes whatever sits at that path. Canonicalize
    first and require the result to stay inside the tree.
    """
    base = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(base, *parts))
    if candidate == base or candidate.startswith(base + os.sep):
        return candidate
    return None


CMD_DELETE_VERBS = frozenset({"del", "erase", "rd", "rmdir"})
CMD_TREE_DELETE_FLAGS = {
    "robocopy": frozenset({"/mir", "/purge"}),
    "xcopy": frozenset({"/e", "/s"}),
}
WINDOWS_COMMAND_SUFFIXES = (".exe", ".com", ".bat", ".cmd", ".ps1")
# The shells reach each other through interpreters, so each gate has to
# read a delete spelled the other way. The readings live here rather than in
# a gate, so neither can learn a spelling the other does not.
POSIX_DELETE_PROGRAMS = frozenset({"rm"})
POWERSHELL_DELETE_PROGRAMS = frozenset({"remove-item", "ri", "remove-itemproperty"})
DELETE_PROGRAMS = (POSIX_DELETE_PROGRAMS | POWERSHELL_DELETE_PROGRAMS
                   | CMD_DELETE_VERBS | set(CMD_TREE_DELETE_FLAGS))
# -Recurse abbreviates to any unambiguous prefix, and -Force to -fo.
RECURSE_PREFIXES = tuple(f"-{'recurse'[:n]}" for n in range(1, 8))
POWERSHELL_PATH_FLAGS = frozenset({"-path", "-literalpath"})


def posix_delete_verdict(args: list) -> tuple:
    """Return (decision, reason) for a POSIX rm argument list."""
    recursive = False
    operands = []
    parsing = True
    for token in args:
        if parsing and token == "--":
            parsing = False
        elif parsing and token.startswith("--"):
            recursive = recursive or long_option_matches(token, "--recursive")
        elif parsing and is_short_group(token):
            recursive = recursive or "r" in token or "R" in token
        else:
            operands.append(token)
    return delete_verdict(recursive, operands, "recursive rm")


def powershell_delete_verdict(args: list) -> tuple:
    """Return (decision, reason) for a Remove-Item argument list."""
    recursive = False
    operands = []
    for token in args:
        if token.startswith("-"):
            recursive = recursive or token.lower() in RECURSE_PREFIXES
        elif token.lower() not in POWERSHELL_PATH_FLAGS:
            operands.append(token)
    return delete_verdict(recursive, operands, "recursive Remove-Item")


def any_delete_verdict(program: str, args: list) -> tuple:
    """Return the strongest verdict any shell's reading of a delete gives.

    rd, rmdir, del, and erase name both a Remove-Item alias and a CMD verb,
    and the three shells spell recursion differently (-r against -Recurse
    against /s). Take whichever reading is strongest rather than guessing
    which shell the caller meant, because guessing wrong clears the act.
    """
    return strongest(posix_delete_verdict(args),
                     strongest(powershell_delete_verdict(args),
                               cmd_delete_verdict(program, args)))


# Interpreters reach one shell from the other. A gate that knows only its
# own leaves `powershell -Command` and `bash -c` as holes in the other.
SHELL_INTERPRETERS = frozenset({
    "bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "busybox",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
})
SHELL_PAYLOAD_FLAGS = frozenset({"-c", "--command", "-command", "/c", "/k"})
POWERSHELL_PROGRAMS = frozenset({"powershell", "powershell.exe", "pwsh", "pwsh.exe"})
POWERSHELL_ENCODED_FLAGS = frozenset(
    {"-e", "-ec"}
    | {f"-{'encodedcommand'[:length]}"
       for length in range(2, len("encodedcommand") + 1)}
)
MAX_COMMAND_DEPTH = 4
CMD_RECURSIVE_FLAGS = frozenset({"/s"})


def is_shell_payload_flag(token: str) -> bool:
    """Return whether one shell option introduces a command payload."""
    lowered = token.casefold()
    if lowered in SHELL_PAYLOAD_FLAGS:
        return True
    return lowered.startswith("-") and not lowered.startswith("--") and "c" in lowered


def normalize_windows_command_name(program: str) -> str:
    """Return a Windows command basename without a PATHEXT suffix."""
    name = ntpath.basename(program.strip().strip('"').strip("'")).casefold()
    for suffix in WINDOWS_COMMAND_SUFFIXES:
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


PROHIBITED_COMMANDS = frozenset({
    "arptables", "aws", "aws-vault", "az", "azcopy", "bcdedit", "bicep",
    "bootrec", "bootsect", "bq", "cfdisk", "clear-eventlog", "crictl",
    "cdk", "delgroup", "deluser", "dism", "diskpart", "diskutil", "dsrm", "eb",
    "ebtables", "eksctl", "fdisk", "firewall-cmd", "format", "format-volume",
    "ftp", "func", "gcloud", "gdisk", "gke-gcloud-auth-plugin", "gpasswd",
    "groupdel", "grub-install", "grub-mkconfig", "gsutil", "helm", "insmod",
    "ip6tables", "ip6tables-restore", "ip6tables-save", "iptables",
    "iptables-restore", "iptables-save", "k9s", "kind", "kubectl", "kustomize",
    "lftp", "lvconvert", "lvreduce", "lvremove", "makefs", "manage-bde",
    "mbr2gpt", "mke2fs", "minikube", "mkfs", "modprobe", "ncftp", "newfs",
    "nft", "oc", "packer", "parted", "passwd", "plink", "pscp", "psftp",
    "pulumi", "putty",
    "pvremove", "remove-adgroup", "remove-aduser", "remove-eventlog",
    "remove-localgroup", "remove-localuser", "rmmod", "rmuser", "sam", "scp",
    "sfc", "sfdisk", "sftp", "shred", "ssh", "ssh-add", "ssh-agent", "ssh-keygen",
    "ssh-keyscan", "sshd", "swapoff", "telnet", "terraform", "terragrunt",
    "tftp", "tofu", "ufw", "unlink", "update-grub", "userdel", "usermod",
    "winrm", "wipe", "wrangler",
})
PROHIBITED_COMMAND_PREFIXES = ("mkfs.", "newfs_")
INFRASTRUCTURE_PATH_MARKERS = (
    "/.aws/", "/.azure/", "/.boto", "/.config/gcloud/", "/.gsutil/",
    "/.kube/", "/.lftp/", "/.pulumi/", "/.ssh/", "/.terraform/",
    "/.terraform.d/",
    "/etc/ssh/",
    # Process environments and developer credential stores hold the same
    # tokens an agent must never read into its context.
    "/.config/gh/", "/github cli/",
    "/.git-credentials/", "/.docker/config.json/",
)
PROCESS_FILESYSTEM_ROOT = "/proc/"
PROCESS_ENVIRONMENT_NAME = "environ"
KUBERNETES_DIRECTORY_MARKERS = ("/charts/", "/helm/", "/k8s/", "/kubernetes/")
KUBERNETES_FILENAMES = frozenset({
    "chart.yaml", "chart.yml", "helmfile.yaml", "helmfile.yml",
    "kustomization.yaml", "kustomization.yml", "values.yaml", "values.yml",
})
INFRASTRUCTURE_SCAN_SKIP = frozenset({".git", ".venv", "node_modules", "__pycache__"})
MAX_INFRASTRUCTURE_FILE_BYTES = 65536
MAX_INFRASTRUCTURE_SCAN_FILES = 20000


def _account_delete_command(name: str, args: list) -> bool:
    """Return whether a multipurpose account tool deletes a user or group."""
    lowered = [token.casefold() for token in args]
    if name == "pw":
        return bool(lowered and lowered[0] in {"userdel", "groupdel"})
    if name == "net":
        return bool(lowered and lowered[0] in {"user", "localgroup"}
                    and "/delete" in lowered)
    if name == "sysadminctl":
        return "-deleteuser" in lowered
    if name == "dscl":
        text = " ".join(lowered).replace("\\", "/")
        return "-delete" in lowered and any(
            marker in text for marker in ("/users/", "/groups/"))
    if name == "wmic":
        return bool(lowered and lowered[0] in {"useraccount", "group"}
                    and "delete" in lowered)
    return False


def _safe_pages_value(value: str) -> bool:
    """Return whether a Pages option value is a literal argument."""
    return bool(value and not value.startswith("-") and not is_ambiguous(value))


_PAGES_PROTECTED_NAMES = {
    ".aws",
    ".git",
    ".netrc",
    ".npmrc",
    ".ssh",
    "credentials",
    "credentials.json",
    "secrets",
    "secrets.json",
}
_PAGES_OUTPUT_NAMES = {"build", "dist"}


def _pages_path_is_within(root: str, candidate: str) -> bool:
    """Return whether a resolved Pages path stays inside the output root."""
    root_drive = os.path.splitdrive(root)[0].casefold()
    candidate_drive = os.path.splitdrive(candidate)[0].casefold()
    return (root_drive == candidate_drive
            and os.path.commonpath((root, candidate)) == root)


def _pages_symlink_is_safe(output_root: str, entry_path: str) -> bool:
    """Return whether a deployment entry resolves to non-protected output."""
    target_path = os.path.realpath(entry_path)
    within_output = _pages_path_is_within(output_root, target_path)
    relative_path = os.path.relpath(target_path, output_root)
    path_parts = tuple(
        part for part in relative_path.replace("\\", "/").split("/")
        if part and part != "."
    )
    protected_names = tuple(name.casefold() for name in _PAGES_PROTECTED_NAMES)
    return within_output and not (
        any(part.casefold() in protected_names for part in path_parts)
        or any(part.casefold().startswith(".env") for part in path_parts)
    )


def _pages_deployment_path_verdict(root: str, resolved_path: str) -> tuple:
    """Reject repository roots, hidden paths, and protected output contents."""
    relative_path = os.path.relpath(resolved_path, root)
    path_parts = tuple(
        part for part in relative_path.replace("\\", "/").split("/")
        if part and part != "."
    )
    if not path_parts:
        return "deny", "Pages deployment must target a dedicated output directory"
    if any(part.startswith(".") for part in path_parts):
        return "deny", "Pages deployment cannot target hidden directories"
    if path_parts[-1].casefold() not in _PAGES_OUTPUT_NAMES:
        return "deny", "Pages deployment must target a dedicated output directory"
    protected_names = tuple(name.casefold() for name in _PAGES_PROTECTED_NAMES)
    if any(part.casefold() in protected_names for part in path_parts):
        return "deny", "Pages deployment cannot target protected content"
    for current_path, directory_names, file_names in os.walk(
        resolved_path, followlinks=False
    ):
        for name in (*directory_names, *file_names):
            lowered_name = name.casefold()
            if (lowered_name.startswith(".env")
                    or lowered_name in protected_names
                    or not _pages_symlink_is_safe(
                        resolved_path, os.path.join(current_path, name))):
                return "deny", "Pages deployment cannot include protected credentials"
    return "", ""


def cloudflare_pages_verdict(program: str, args: list, cwd: str = "") -> tuple:
    """Allow only a local, explicitly targeted Cloudflare Pages deployment."""
    name = normalize_windows_command_name(program)
    if name != "wrangler":
        return "", ""
    lowered = [token.casefold() for token in args]
    if len(args) < 3 or lowered[0:2] != ["pages", "deploy"]:
        return "deny", "only Wrangler Pages deployment is allowed"

    deploy_path = args[2]
    if deploy_path.startswith("-") or is_ambiguous(deploy_path):
        return "deny", "Pages deployment path must be a literal workspace path"
    root = os.path.realpath(os.path.abspath(cwd or os.getcwd()))
    resolved_path = os.path.realpath(os.path.abspath(os.path.join(root, deploy_path)))
    within_workspace = _pages_path_is_within(root, resolved_path)
    if not within_workspace or not os.path.isdir(resolved_path):
        return "deny", "Pages deployment path must be an existing workspace directory"
    path_verdict = _pages_deployment_path_verdict(root, resolved_path)
    if path_verdict[0]:
        return path_verdict

    project_name = ""
    seen_options = set()
    index = 3
    while index < len(args):
        option = args[index]
        lowered_option = option.casefold()
        option_name = lowered_option.split("=", 1)[0]
        if option_name in {"--project-name", "--branch"}:
            if option_name in seen_options:
                return "deny", "Wrangler Pages options cannot be repeated"
            seen_options.add(option_name)
        if lowered_option.startswith("--project-name="):
            project_name = option.split("=", 1)[1]
        elif lowered_option == "--project-name":
            index += 1
            if index >= len(args):
                return "deny", "Pages deployment requires a project name"
            project_name = args[index]
        elif lowered_option.startswith("--branch="):
            branch = option.split("=", 1)[1]
            if not _safe_pages_value(branch):
                return "deny", "Pages deployment branch must be literal"
        elif lowered_option == "--branch":
            index += 1
            if index >= len(args) or not _safe_pages_value(args[index]):
                return "deny", "Pages deployment branch must be literal"
        else:
            return "deny", "Wrangler Pages option is outside the deployment allowance"
        index += 1
    if not _safe_pages_value(project_name):
        return "deny", "Pages deployment requires a literal project name"
    return "", ""


def prohibited_command_verdict(program: str, args: list) -> tuple:
    """Deny commands prohibited on every host and through every shell."""
    name = normalize_windows_command_name(program)
    if name == "wrangler":
        return "", ""
    if name in PROHIBITED_COMMANDS or name.startswith(PROHIBITED_COMMAND_PREFIXES):
        return "deny", f"{sanitize(name)} is prohibited for agent execution"
    _verb, separator, noun = name.partition("-")
    if separator and (noun.startswith("az") or "netfirewall" in noun):
        return "deny", f"{sanitize(name)} is prohibited for agent execution"
    lowered = [token.casefold() for token in args]
    if name == "gpt" and lowered and lowered[0] == "destroy":
        return "deny", "gpt destroy removes partition metadata"
    if name == "log" and lowered and lowered[0] == "erase":
        return "deny", "log erase removes system log records"
    if _account_delete_command(name, args):
        return "deny", "account and group deletion is prohibited for agents"
    return "", ""


def _infrastructure_manifest_text(path: str) -> str:
    """Return a bounded manifest prefix without exposing it to the caller."""
    try:
        details = os.stat(path)
        if not os.path.isfile(path) or details.st_size > MAX_INFRASTRUCTURE_FILE_BYTES:
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read(MAX_INFRASTRUCTURE_FILE_BYTES)
    except (OSError, UnicodeError):
        return ""


def strip_windows_drive(normalized: str) -> str:
    """Return a normalized path without a leading `/<letter>:` segment.

    On Windows, os.path.abspath roots "/proc/1/environ" on the current
    drive, so POSIX-only checks such as /proc must ignore that segment.
    """
    has_drive = (len(normalized) > 3 and normalized[0] == "/"
                 and normalized[1].isalpha() and normalized[2] == ":"
                 and normalized[3] == "/")
    return normalized[3:] if has_drive else normalized


def is_protected_infrastructure_path(path: str, cwd: str = "", content: str = "") -> bool:
    """Return whether a path reaches protected infrastructure configuration."""
    candidate = path.strip().strip('"').strip("'")
    if not candidate or candidate.startswith("-"):
        return False
    absolute = candidate if os.path.isabs(candidate) else os.path.join(cwd or os.getcwd(), candidate)
    resolved = os.path.realpath(os.path.abspath(absolute))
    normalized = "/" + resolved.replace("\\", "/").casefold().strip("/")
    padded = normalized + ("/" if not normalized.endswith("/") else "")
    basename = normalized.rsplit("/", 1)[-1]
    if any(marker in padded for marker in INFRASTRUCTURE_PATH_MARKERS):
        return True
    if (strip_windows_drive(normalized).startswith(PROCESS_FILESYSTEM_ROOT)
            and basename == PROCESS_ENVIRONMENT_NAME):
        return True
    if any(marker in padded for marker in KUBERNETES_DIRECTORY_MARKERS):
        return True
    if basename in KUBERNETES_FILENAMES or basename.startswith(("values-", "values.")):
        return True
    terraform_suffixes = (".tf", ".tf.json", ".tfvars", ".tfvars.json")
    if basename.endswith(terraform_suffixes):
        return True
    if basename.startswith("terraform.tfstate") or basename == ".terraform.lock.hcl":
        return True
    if basename in {".netrc", "_netrc", ".terraformrc", "terraform.rc"}:
        return True
    if basename in {"cdk.json", "samconfig.toml"}:
        return True
    if basename.endswith((".bicep", ".bicepparam", ".pkr.hcl", ".pkr.json")):
        return True
    if basename.startswith("pulumi.") and basename.endswith((".yaml", ".yml", ".json")):
        return True
    if basename.endswith((".yaml", ".yml", ".json")):
        manifest = content or _infrastructure_manifest_text(resolved)
        lowered = manifest.casefold()
        return (("apiversion:" in lowered and "kind:" in lowered)
                or ('"apiversion"' in lowered and '"kind"' in lowered))
    return False


def tree_contains_protected_infrastructure(root: str, include: str = "") -> bool:
    """Return whether a bounded search root contains protected files."""
    seen = 0
    try:
        for current, directories, filenames in os.walk(root):
            directories[:] = [name for name in directories
                               if name not in INFRASTRUCTURE_SCAN_SKIP]
            for filename in filenames:
                seen += 1
                if seen > MAX_INFRASTRUCTURE_SCAN_FILES:
                    return True
                candidate = os.path.join(current, filename)
                relative = os.path.relpath(candidate, root).replace("\\", "/")
                matches = (not include or fnmatch.fnmatch(relative, include)
                           or fnmatch.fnmatch(filename, include))
                if matches and is_protected_infrastructure_path(candidate, root):
                    return True
    except OSError:
        return True
    return False


def infrastructure_path_verdict(program: str, args: list,
                                redirects: list, cwd: str) -> tuple:
    """Deny shell access to protected infrastructure files and trees."""
    candidates = [program, *args, *redirects]
    if any(is_protected_infrastructure_path(candidate, cwd)
           for candidate in candidates):
        return "deny", "infrastructure credentials and configuration are protected"
    name = normalize_windows_command_name(program)
    broad_readers = {"dir", "find", "get-childitem", "grep", "ls", "rg"}
    if name in broad_readers:
        roots = [token for token in args if not token.startswith("-")]
        if any(token in {".", "./", ".\\"} and
               tree_contains_protected_infrastructure(cwd) for token in roots):
            return "deny", "a broad search includes protected infrastructure files"
    return "", ""


def decode_powershell_command(value: str) -> tuple:
    """Return a strict UTF-16LE EncodedCommand payload or a deny reason."""
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None, "PowerShell EncodedCommand is not strict Base64"
    if len(raw) % 2:
        return None, "PowerShell EncodedCommand has an odd UTF-16LE byte length"
    try:
        return raw.decode("utf-16le", errors="strict"), ""
    except UnicodeDecodeError:
        return None, "PowerShell EncodedCommand is not valid UTF-16LE"


def powershell_payload(args: list) -> tuple:
    """Return (kind, payload or reason) for PowerShell command arguments."""
    for index, token in enumerate(args):
        lowered = token.lower()
        if lowered in POWERSHELL_ENCODED_FLAGS:
            if index + 1 >= len(args):
                return "deny", "PowerShell EncodedCommand has no Base64 payload"
            payload, reason = decode_powershell_command(args[index + 1])
            return ("deny", reason) if payload is None else ("command", payload)
        if lowered in ("-c", "-command", "--command"):
            if index + 1 < len(args):
                return "command", args[index + 1]
            return "", ""
    return "", ""


POWERSHELL_POLICY_ALIASES = {
    "ac": "add-content", "clc": "clear-content", "cp": "copy-item",
    "cpi": "copy-item", "gci": "get-childitem", "icm": "invoke-command",
    "iex": "invoke-expression",
    "ii": "invoke-item", "ipmo": "import-module",
    "irm": "invoke-restmethod", "iwr": "invoke-webrequest", "mi": "move-item",
    "mv": "move-item", "ri": "remove-item", "rm": "remove-item",
    "sajb": "start-job", "saps": "start-process", "start": "start-process",
}
POWERSHELL_POLICY_DENY = frozenset({
    "add-mppreference", "add-type", "clear-disk", "connect-pssession",
    "disable-bitlocker", "disable-netadapter", "enable-netadapter",
    "enable-psremoting", "enter-pssession", "export-pfxcertificate", "format-volume",
    "get-secret", "import-certificate", "import-pfxcertificate",
    "invoke-expression", "new-netfirewallrule", "new-pssession",
    "new-netroute", "new-selfsignedcertificate", "new-service",
    "new-vpnconnection", "register-objectevent",
    "register-scheduledtask", "remove-mppreference",
    "remove-netfirewallrule", "remove-netroute", "remove-service",
    "remove-scheduledtask", "remove-vpnconnection", "send-mailmessage",
    "set-authenticodesignature", "set-dnsclientserveraddress",
    "set-executionpolicy", "set-mppreference", "set-netadapter",
    "set-netfirewallprofile", "set-netfirewallrule", "set-netroute",
    "set-scheduledtask", "set-vpnconnection", "set-wsmanquickconfig", "start-job",
    "unregister-scheduledtask", "unlock-bitlocker",
})
POWERSHELL_POLICY_ASK = frozenset({
    "add-adgroupmember", "add-localgroupmember", "connect-msolservice",
    "connect-mggraph", "connect-azaccount", "disable-adaccount",
    "disable-localuser", "enable-adaccount", "enable-localuser",
    "expand-archive", "export-csv", "find-module", "get-adcomputer",
    "get-adgroup", "get-adgroupmember", "get-aduser", "get-credential",
    "export-clixml", "get-ciminstance", "get-dnsclientcache",
    "get-dnsclientserveraddress",
    "get-localgroup", "get-localgroupmember", "get-localuser",
    "get-netadapter", "get-netipconfiguration", "get-netroute",
    "get-netipaddress", "get-netipinterface", "get-netneighbor", "get-netnat",
    "get-nettcpconnection", "get-netudpendpoint", "get-psdrive",
    "get-smbconnection", "get-smbmapping", "get-smbshare",
    "get-vpnconnection", "get-wmiobject", "import-module", "install-module",
    "invoke-command",
    "invoke-item", "invoke-restmethod", "invoke-webrequest", "new-aduser",
    "new-localgroup", "new-localuser", "new-psdrive", "remove-aduser",
    "remove-localgroup", "remove-localgroupmember", "remove-localuser",
    "restart-service", "set-adaccountpassword", "set-aduser",
    "set-localuser", "set-service", "start-bitstransfer", "start-process",
    "resolve-dnsname", "stop-service", "test-connection", "test-netconnection",
    "update-module", "whoami", "nltest", "quser", "qwinsta",
})
POWERSHELL_FILE_WRITERS = frozenset({
    "add-content", "clear-content", "copy-item", "export-clixml",
    "export-csv", "move-item", "out-file", "rename-item", "set-content",
})
POWERSHELL_REGISTRY_WRITERS = frozenset({
    "clear-itemproperty", "new-itemproperty", "remove-itemproperty",
    "rename-itemproperty", "set-itemproperty",
})
POWERSHELL_MODULE_PROGRAMS = frozenset({
    "find-module", "import-module", "install-module", "update-module",
})
POWERSHELL_EXECUTABLE_SUFFIXES = (
    ".bat", ".cmd", ".com", ".dll", ".exe", ".msi", ".ps1", ".psm1",
    ".scr", ".vbs",
)
POWERSHELL_STATIC_API_MARKERS = (
    "diagnostics.process", "system.diagnostics.process", "system.net.http.httpclient",
    "system.net.mail.smtpclient",
    "system.net.sockets", "system.net.webclient", "system.reflection",
    "reflection.assembly", "]::load", "]::loadfrom", "]::loadfile",
    "credentialmanager", "lsaunprotect", "protecteddata", "windowsidentity",
)
POWERSHELL_SECURITY_BYPASS_MARKERS = (
    "amsiinitfailed", "amsiutils", "etwprovider", "scriptblocklogging",
    "transcriptionenabled",
)
POWERSHELL_NETWORK_API_MARKERS = (
    "system.net.dns", "system.net.networkinformation", "networkinterface]::",
    "windowsidentity",
)
POWERSHELL_WMI_PERSISTENCE_MARKERS = (
    "root\\subscription", "commandlineeventconsumer", "__eventfilter",
)
POWERSHELL_SENSITIVE_REGISTRY_MARKERS = (
    "hkey_local_machine", "hklm:", "hkey_classes_root", "hkcr:",
    "currentversion\\run", "currentversion/run", "\\sam", "/sam",
    "\\security", "/security", "\\system", "/system", "internet settings",
)
POWERSHELL_NETWORK_ENUMERATION_PROGRAMS = frozenset({
    "arp", "ipconfig", "net", "netstat", "nslookup", "ping", "query",
    "route", "ssh", "tracert",
})
POWERSHELL_CROSS_SHELL_ALIASES = frozenset({
    "ac", "clc", "cpi", "gci", "icm", "iex", "ii", "ipmo", "irm",
    "iwr", "mi", "ri", "sajb", "saps",
})
POWERSHELL_NATIVE_SECURITY_PROGRAMS = frozenset({
    "auditpol", "bcdedit", "manage-bde", "netsh", "reg", "schtasks",
    "secedit", "wevtutil", "winrs",
})


def normalize_program_name(program: str) -> str:
    """Return a case-insensitive Windows command name without `.exe`."""
    name = ntpath.basename(program.strip().strip('"').strip("'")).lower()
    name = name.removesuffix(".exe")
    return POWERSHELL_POLICY_ALIASES.get(name, name)


def powershell_policy_applies_in_bash(program: str) -> bool:
    """Return True when Bash launches a recognizable PowerShell policy form."""
    raw = ntpath.basename(program.strip().strip('"').strip("'")).lower()
    raw = raw.removesuffix(".exe")
    return (raw in {"powershell", "pwsh", "."}
            or raw in POWERSHELL_CROSS_SHELL_ALIASES
            or raw in POWERSHELL_NATIVE_SECURITY_PROGRAMS
            or "-" in raw
            or raw.startswith("[")
            or raw.endswith((".ps1", ".psm1")))


def _powershell_argument_text(program: str, args: list) -> str:
    """Return normalized command text for marker checks."""
    return " ".join([program, *args]).lower().replace("/", "\\")


def _powershell_parameter(token: str, name: str, minimum: int) -> bool:
    """Return True for an unambiguous prefix of a PowerShell parameter."""
    candidate = token.lower().split(":", 1)[0].lstrip("-")
    return len(candidate) >= minimum and name.startswith(candidate)


CURL_UPLOAD_LONG_OPTIONS = frozenset({
    "--data", "--data-ascii", "--data-binary", "--data-raw",
    "--data-urlencode", "--form", "--form-string", "--json", "--upload-file",
})
CURL_UPLOAD_SHORT_OPTIONS = ("-T", "-F", "-d")


def curl_transfer_verdict(program: str, args: list) -> tuple:
    """Return one shared verdict for curl network transfer direction."""
    if normalize_windows_command_name(program) != "curl":
        return "", ""
    for token in args:
        if any(token == option or token.startswith(option)
               for option in CURL_UPLOAD_SHORT_OPTIONS):
            return "deny", "curl uploads local data to a remote endpoint"
        lowered = token.casefold()
        if any(lowered == option or lowered.startswith(option + "=")
               for option in CURL_UPLOAD_LONG_OPTIONS):
            return "deny", "curl uploads local data to a remote endpoint"
    return "ask", "curl accesses an external network endpoint"


def _powershell_interpreter_policy(name: str, args: list) -> tuple:
    """Gate PowerShell launch options that hide or inject executable code."""
    if name not in {"powershell", "pwsh"}:
        return "", ""
    for index, token in enumerate(args):
        lowered = token.lower().split(":", 1)[0]
        if lowered in POWERSHELL_ENCODED_FLAGS or lowered in {
                "-c", "-command", "--command"}:
            return "deny", "PowerShell receives an arbitrary command payload"
        if lowered in {"-windowstyle", "-w"} and index + 1 < len(args):
            if args[index + 1].lower() == "hidden":
                return "deny", "PowerShell starts with a hidden window"
        if lowered in {"-executionpolicy", "-ex", "-ep"}:
            if index + 1 < len(args):
                policy = args[index + 1].lower()
                if policy in {"bypass", "unrestricted"}:
                    return "deny", "PowerShell disables execution policy controls"
            return "ask", "PowerShell overrides the process execution policy"
        if lowered in {"-file", "-f"}:
            if index + 1 < len(args) and is_ambiguous(args[index + 1]):
                return "deny", "PowerShell receives a dynamic script path"
            return "ask", "PowerShell executes a script file"
        if lowered in {"-noprofile", "-nop"}:
            return "ask", "PowerShell bypasses the configured profile"
    return "", ""


def _powershell_cli_policy(name: str, args: list) -> tuple:
    """Classify native administration tools by operation instead of operands."""
    lowered = [token.lower() for token in args]
    if name == "auditpol":
        return (("deny", "audit policy modification weakens security controls")
                if any(token.startswith(("/set", "/clear", "/remove"))
                       for token in lowered) else ("", ""))
    if name == "wevtutil":
        return (("deny", "event log modification removes security evidence")
                if lowered and lowered[0] in {"cl", "clear-log", "sl", "set-log"}
                else ("", ""))
    if name == "manage-bde":
        if lowered and all(token == "-status" or token.startswith("-") is False
                           for token in lowered):
            return "", ""
        return "deny", "BitLocker modification changes storage protection"
    if name == "bcdedit":
        markers = {"/set", "/delete", "/deletevalue", "/create", "/copy",
                   "/import", "/export", "/sysstore"}
        return (("deny", "boot policy modification changes host security")
                if markers.intersection(lowered) else ("", ""))
    if name == "secedit":
        if any(token in {"/configure", "/import", "/export"} for token in lowered):
            return "deny", "local security policy modification changes host controls"
        return "ask", "local security policy inspection accesses privileged state"
    if name == "netsh":
        return "deny", "netsh changes firewall, proxy, or network configuration"
    if name == "route":
        if lowered and lowered[0] in {"add", "change", "delete"}:
            return "deny", "route changes network egress configuration"
        return "ask", "route enumerates network topology"
    if name == "reg":
        text = " ".join(lowered).replace("/", "\\")
        sensitive = ("hklm\\sam", "hklm\\security", "hklm\\system")
        if any(marker in text for marker in sensitive):
            return "deny", "registry access reaches credential or security hives"
    if name in {"winrs", "schtasks"}:
        return "deny", f"{sanitize(name)} enables remote execution or persistence"
    return "", ""


def _powershell_path_policy(name: str, args: list, redirects: list) -> tuple:
    """Classify file operations from path properties rather than basenames."""
    if name not in POWERSHELL_FILE_WRITERS:
        return "", ""
    targets = _known_write_targets(name, args, redirects)
    for value in targets:
        cleaned = value.strip().strip('"').strip("'")
        normalized = cleaned.lower().replace("\\", "/")
        if normalized.startswith("//"):
            return "deny", "a file operation reaches a remote UNC path"
        if is_protected_windows_path(normalized):
            return "deny", "a file operation reaches a protected system path"
    if any(is_ambiguous(value) for value in targets):
        return "ask", "a file path contains an expansion the gate cannot resolve"
    broad_values = list(targets)
    if name in {"copy-item", "move-item", "rename-item"}:
        broad_values.extend(token for token in args if not token.startswith("-"))
    if any(is_ambiguous(value) for value in broad_values):
        return "ask", "a file path contains an expansion the gate cannot resolve"
    if any("*" in value or "?" in value for value in broad_values):
        return "ask", "a file operation uses a broad wildcard path"
    if any(token.lower() in RECURSE_PREFIXES for token in args):
        return "ask", "a file operation recursively changes a directory tree"
    if name == "clear-content":
        return "ask", "Clear-Content removes all data from a file"
    return "", ""


def is_protected_windows_path(normalized_path: str) -> bool:
    """Return whether a normalized path enters a protected Windows tree."""
    if len(normalized_path) < 4:
        return False
    if not normalized_path[0].isalpha() or normalized_path[1:3] != ":/":
        return False
    first_component = normalized_path[3:].split("/", 1)[0]
    return first_component in {"program files", "programdata", "windows"}


def _powershell_indirect_policy(name: str, text: str, args: list) -> tuple:
    """Classify indirect execution, security bypass, and persistence."""
    if any(marker in text for marker in POWERSHELL_STATIC_API_MARKERS):
        return "deny", "a static .NET API enables indirect code or network access"
    if any(marker in text for marker in POWERSHELL_SECURITY_BYPASS_MARKERS):
        return "deny", "the command targets a PowerShell security or logging control"
    if any(marker in text for marker in POWERSHELL_WMI_PERSISTENCE_MARKERS):
        return "deny", "the command targets WMI event persistence"
    if "$profile" in text and name in POWERSHELL_FILE_WRITERS:
        return "deny", "a file write targets a PowerShell profile"
    if name in POWERSHELL_POLICY_DENY:
        return "deny", f"{sanitize(name)} belongs to a denied PowerShell family"
    if name in {"scp", "sftp", "ftp", "azcopy", "rclone"}:
        return "deny", "a transfer utility can send local data to a remote system"
    if name == "curl":
        return curl_transfer_verdict(name, args)
    if name == "new-object":
        if "-comobject" in text or "wscript.shell" in text:
            return "deny", "a COM object enables indirect process or script execution"
        return "ask", "New-Object constructs a type outside static inspection"
    if name == "invoke-command":
        remote = (("computername", 3), ("session", 4),
                  ("connectionuri", 4), ("hostname", 2))
        if any(_powershell_parameter(token, parameter, minimum)
               for token in args for parameter, minimum in remote):
            return "deny", "Invoke-Command executes on a remote session"
    if name in POWERSHELL_MODULE_PROGRAMS and any(is_ambiguous(arg) for arg in args):
        return "deny", "a dynamic module reference hides executable code"
    if name in {"start-process", "invoke-item"} and any(
            is_ambiguous(arg) for arg in args):
        return "deny", "a dynamic process target bypasses static inspection"
    return "", ""


def _powershell_state_policy(name: str, text: str) -> tuple:
    """Classify persistent state, certificate, and mapped-drive operations."""
    if name in POWERSHELL_REGISTRY_WRITERS:
        if any(marker in text for marker in POWERSHELL_SENSITIVE_REGISTRY_MARKERS):
            return "deny", "a registry write reaches a security-sensitive hive or key"
        return "ask", "a registry write changes persistent machine state"
    if name == "remove-item" and "cert:\\" in text:
        return "deny", "certificate removal changes trust or identity material"
    if name == "get-childitem" and "cert:\\" in text:
        return "ask", "certificate enumeration accesses identity material"
    if name == "new-psdrive" and "\\\\" in text:
        return "deny", "a mapped drive reaches a remote UNC path"
    return "", ""


def _powershell_web_policy(name: str, args: list) -> tuple:
    """Classify executable downloads and request bodies without basename literals."""
    if name in {"invoke-webrequest", "invoke-restmethod", "start-bitstransfer"}:
        values = [value.lower().split("?", 1)[0] for value in args]
        if any(value.endswith(POWERSHELL_EXECUTABLE_SUFFIXES) for value in values):
            return "deny", "a network operation transfers executable content"
        for index, value in enumerate(args):
            flag, separator, attached = value.lower().partition(":")
            if (name == "start-bitstransfer"
                    and _powershell_parameter(flag, "transfertype", 9)):
                transfer_type = attached if separator else ""
                if not transfer_type and index + 1 < len(args):
                    transfer_type = args[index + 1].lower()
                if transfer_type == "upload":
                    return "deny", "BITS uploads local data to a remote endpoint"
            if flag in {"-body", "-form", "-infile"}:
                return "deny", "a web request can send local data to a remote endpoint"
            if flag == "-method":
                method = attached if separator else ""
                if not method and index + 1 < len(args):
                    method = args[index + 1].lower()
                if method in {"connect", "delete", "patch", "post", "put"}:
                    return "deny", "a web request uses a state-changing HTTP method"
    return "", ""


def _powershell_script_policy(program: str, name: str, args: list) -> tuple:
    """Classify direct script and local executable paths."""
    raw = program.strip().strip('"').strip("'").lower()
    if raw.startswith(("//", "\\\\")):
        return "deny", "a command path reaches a remote UNC location"
    if name.endswith((".ps1", ".psm1")) or name == ".":
        if any(is_ambiguous(arg) for arg in args):
            return "deny", "a dynamic script path bypasses static inspection"
        return "ask", "a script path executes code outside static inspection"
    if ("/" in raw or "\\" in raw) and raw.endswith(POWERSHELL_EXECUTABLE_SUFFIXES):
        return "ask", "a local executable path runs code outside static inspection"
    assignment = "=" in program and program.lower().startswith(("$env:", "${env:"))
    if is_ambiguous(program) and not assignment:
        return "deny", "a dynamic command name bypasses static inspection"
    return "", ""


def powershell_policy_verdict(program: str, args: list,
                              redirects: list = None) -> tuple:
    """Return the durable PowerShell policy verdict for one named command."""
    redirects = redirects or []
    name = normalize_program_name(program)
    text = _powershell_argument_text(program, args)
    for verdict in (_powershell_interpreter_policy(name, args),
                    _powershell_cli_policy(name, args),
                    _powershell_indirect_policy(name, text, args),
                    _powershell_state_policy(name, text),
                    _powershell_web_policy(name, args),
                    _powershell_path_policy(name, args, redirects),
                    _powershell_script_policy(program, name, args)):
        if verdict[0]:
            return verdict
    if name in {"compress-archive", "expand-archive"}:
        return "ask", "archive operations read or write a broad file collection"
    if name in POWERSHELL_POLICY_ASK:
        return "ask", f"{sanitize(name)} requires bounded administration approval"
    if name in POWERSHELL_NETWORK_ENUMERATION_PROGRAMS:
        return "ask", f"{sanitize(name)} accesses network or identity state"
    if any(marker in text for marker in POWERSHELL_NETWORK_API_MARKERS):
        return "ask", "a .NET API enumerates network state"
    return "", ""


def cmd_delete_verdict(program: str, args: list) -> tuple:
    """Return (decision, reason) for a CMD deletion verb.

    CMD flags are slash-prefixed and case-insensitive, and rd and rmdir
    remove a directory tree whenever /s is present. del and erase take
    /s to recurse into subdirectories.
    """
    program_name = normalize_windows_command_name(program)
    if program_name in CMD_DELETE_VERBS:
        recursive_flags = CMD_RECURSIVE_FLAGS
    elif program_name in CMD_TREE_DELETE_FLAGS:
        recursive_flags = CMD_TREE_DELETE_FLAGS[program_name]
    else:
        return "", ""
    recursive = False
    operands = []
    for token in args:
        if token.startswith("/"):
            recursive = recursive or token.casefold() in recursive_flags
        else:
            operands.append(token)
    return delete_verdict(recursive, operands, f"recursive {program_name}")


TEST_DIR_PARTS = frozenset({"tests", "test", "__tests__", "spec"})
TEST_PREFIX_SUFFIXES = (
    ".cjs", ".ipynb", ".js", ".jsx", ".mjs", ".py", ".ts", ".tsx",
)
TEST_NAME_SUFFIXES = (
    "_test.cjs", "_test.go", "_test.js", "_test.mjs", "_test.py",
    "_test.rb", "_test.ts", ".spec.cjs", ".spec.js", ".spec.jsx",
    ".spec.mjs", ".spec.ts", ".spec.tsx", ".test.cjs", ".test.js",
    ".test.jsx", ".test.mjs", ".test.ts", ".test.tsx",
)
# Commands whose named operand is a file they overwrite in place.
WRITE_PROGRAMS = {
    "tee": 0, "sed": -1, "cp": -1, "mv": -1,
    "set-content": 0, "sc": 0, "add-content": 0, "ac": 0,
    "out-file": 0, "copy-item": -1, "cpi": -1, "move-item": -1,
    "mi": -1, "clear-content": 0, "export-clixml": 0,
    "export-csv": 0, "rename-item": -1,
}
CMD_WRITE_PROGRAMS = {
    "copy": -1,
    "move": -1,
    "robocopy": 1,
    "type": None,
    "xcopy": 1,
}
# (canonical name, shortest valid unambiguous prefix, names an output target).
CONTENT_PARAMETERS = (
    ("path", 3, True), ("literalpath", 1, True), ("value", 2, False),
)
OUTPUT_PARAMETERS = (("filepath", 2, True), ("inputobject", 3, False))
PATH_PARAMETERS = (("path", 3, True), ("literalpath", 1, True))
COPY_PARAMETERS = (
    ("path", 3, False), ("literalpath", 1, False),
    ("destination", 3, True),
)
RENAME_PARAMETERS = (
    ("path", 3, False), ("literalpath", 1, False), ("newname", 2, True),
)
POWERSHELL_WRITE_PARAMETERS = {
    "set-content": CONTENT_PARAMETERS, "sc": CONTENT_PARAMETERS,
    "add-content": CONTENT_PARAMETERS, "ac": CONTENT_PARAMETERS,
    "out-file": OUTPUT_PARAMETERS,
    "copy-item": COPY_PARAMETERS, "cpi": COPY_PARAMETERS,
    "move-item": COPY_PARAMETERS, "mi": COPY_PARAMETERS,
    "clear-content": PATH_PARAMETERS, "export-clixml": CONTENT_PARAMETERS,
    "export-csv": CONTENT_PARAMETERS, "rename-item": RENAME_PARAMETERS,
}
PROTECTED_PATH_PARTS = frozenset({
    "hooks", ".claude", "scripts", ".agents", ".codex", ".gemini", ".git",
})
# The canonical policy, its synced copies, and supporting policy documents
# load as agent instructions.
POLICY_FILES = frozenset({
    "agents.md", "claude.md", "gemini.md", "conventions.md", ".cursorrules",
    ".clinerules", ".windsurfrules", ".copilot-instructions",
    ".github/copilot-instructions.md",
})
POLICY_PREFIXES = ("docs/agent-policy/",)


def is_policy_relative(relative: str) -> bool:
    """Return True if a lowercased root-relative path names agent policy text."""
    return relative in POLICY_FILES or relative.startswith(POLICY_PREFIXES)


def strip_windows_decorations(name: str) -> str:
    """Return the filename Windows opens for `name`.

    An NTFS alternate data stream (`test_x.py:evil`) writes into the same
    file, and Windows discards a trailing dot or space.
    """
    base = name.split(":", 1)[0] if ":" in name[2:] else name
    return base.rstrip(". ")


def is_test_path(path: str) -> bool:
    """Return True if `path` names a test file by directory or filename."""
    normalized = os.path.normpath(path).replace("\\", "/").replace(os.sep, "/")
    parts = [part.lower() for part in normalized.split("/")]
    if TEST_DIR_PARTS.intersection(parts[:-1]):
        return True
    file_name = strip_windows_decorations(parts[-1])
    if file_name.endswith(TEST_NAME_SUFFIXES):
        return True
    if not file_name.startswith("test_"):
        return False
    for suffix in TEST_PREFIX_SUFFIXES:
        if file_name.endswith(suffix):
            return len(file_name) > len("test_") + len(suffix)
    return False


def test_write_verdict(program: str, args: list, redirect_targets: list) -> tuple:
    """Return (decision, reason) for a command that writes to a test file.

    A redirect or an in-place edit reaches a test file where no Edit or
    Write matcher can see it, so Rule 3 would apply to the same act
    through one tool and not the other.
    """
    targets = _known_write_targets(program, args, redirect_targets)
    for target in targets:
        if is_test_path(target):
            return "ask", ("writes to an existing test file, which Rule 3 "
                           "puts in front of the user at the act")
    return "", ""


def _powershell_write_operands(name: str, args: list) -> tuple:
    """Return named targets and unused positional write operands."""
    targets = []
    operands = []
    index = 0
    while index < len(args):
        token = args[index]
        flag, separator, attached = token.lower().partition(":")
        parameter = _powershell_write_parameter(name, flag)
        if parameter:
            value = attached if separator else ""
            if not separator and index + 1 < len(args):
                index += 1
                value = args[index]
            if parameter[2] and value:
                targets.append(value)
        elif not token.startswith("-"):
            operands.append(token)
        index += 1
    return targets, operands


def _powershell_write_parameter(name: str, flag: str):
    """Return the parameter matched by a valid unambiguous prefix."""
    candidate = flag.lstrip("-")
    for parameter in POWERSHELL_WRITE_PARAMETERS.get(name, ()):
        canonical, minimum, _target = parameter
        if len(candidate) >= minimum and canonical.startswith(candidate):
            return parameter
    return None


def _known_write_targets(program: str, args: list, redirects: list) -> list:
    """Return known output operands for redirects and write programs."""
    targets = list(redirects)
    name = normalize_windows_command_name(program)
    if name in CMD_WRITE_PROGRAMS:
        position = CMD_WRITE_PROGRAMS[name]
        operands = [token for token in args if not token.startswith("/")]
        if (position is not None and operands
                and -len(operands) <= position < len(operands)):
            targets.append(operands[position])
        return targets
    position = WRITE_PROGRAMS.get(name)
    if position is None:
        return targets
    named_targets, operands = _powershell_write_operands(name, args)
    targets.extend(named_targets)
    if operands:
        targets.append(operands[position])
    return targets


def _protected_path(path: str, cwd: str) -> bool:
    """Return True when a literal path is under a protected gate directory."""
    cleaned = path.strip().strip('"').strip("'")
    if not cleaned or is_ambiguous(cleaned):
        return False
    root = os.path.realpath(cwd or ".")
    candidate = cleaned if os.path.isabs(cleaned) else os.path.join(root, cleaned)
    candidate = os.path.realpath(candidate)
    try:
        relative = os.path.relpath(candidate, root)
    except ValueError:
        return False
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return False
    normalized = relative.replace("\\", "/").lower()
    head = normalized.split("/", 1)[0]
    return head in PROTECTED_PATH_PARTS or is_policy_relative(normalized)


def protected_write_verdict(program: str, args: list,
                            redirects: list, cwd: str) -> tuple:
    """Gate known shell writes to repository-controlled hook files."""
    for target in _known_write_targets(program, args, redirects):
        if _protected_path(target, cwd):
            return "ask", ("a known shell write to gate files, .git/, or agent "
                           "policy. These prompts are best-effort workflow checks")
    return "", ""


MAX_CONFIG_BYTES = 256 * 1024
MAX_REPO_DISCOVERY_DEPTH = 100
# Keys whose value names a program git runs during an ordinary read.
EXEC_CAPABLE_KEYS = frozenset({
    "core.fsmonitor", "core.pager", "core.editor", "core.sshcommand",
    "core.hookspath", "core.alternaterefscommand", "core.askpass",
    "diff.external", "log.showsignature", "gpg.program",
    "uploadpack.packobjectshook", "sequence.editor", "pager.diff",
})
# Sections where any driver name carries an exec-capable key.
EXEC_CAPABLE_SUBSECTIONS = {
    "filter": ("clean", "smudge", "process"),
    "diff": ("textconv", "command"),
    "gpg": ("program",),
    "merge": ("driver",),
}
SAFE_PAGER_VALUES = frozenset({"cat"})


def _read_config_path(path: str):
    """Return config text, "" when absent, or None when unreadable."""
    try:
        if os.path.getsize(path) > MAX_CONFIG_BYTES:
            return None
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError):
        return None


def _section_name(header: str) -> str:
    """Return the normalized section name for a [section "sub"] header."""
    name, _, subsection = header.partition(" ")
    if not subsection:
        return name.lower()
    return f"{name.lower()}.{subsection.strip().strip(chr(34))}"


def _parse_git_config_path(path: str):
    """Return normalized config entries from `path`, or None on failure.

    The file is read, never queried through `git config`: running git to
    decide whether running git is safe is the bug this exists to close.
    Format is git's own, which configparser does not implement.
    """
    raw = _read_config_path(path)
    if raw is None:
        return None
    entries = {}
    section = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = _section_name(stripped[1:-1].strip())
            continue
        key, separator, value = stripped.partition("=")
        if not separator:
            parts = stripped.split(None, 1)
            key = parts[0]
            value = parts[1] if len(parts) > 1 else "true"
        key = key.strip().lower()
        if (not section or not key or not key[0].isalpha()
                or not key.replace("-", "").isalnum()):
            return None
        entries[f"{section}.{key}"] = value.strip()
    return entries


def parse_git_config(cwd: str):
    """Return normalized entries from the repository config under `cwd`."""
    path = os.path.join(cwd or ".", ".git", "config")
    return _parse_git_config_path(path)


def _exec_capable_key(entries: dict) -> str:
    """Return the first exec-capable key present, or an empty string."""
    for name, value in entries.items():
        if name in EXEC_CAPABLE_KEYS and value.lower() not in ("", "false", "0"):
            return name
        if name.startswith("pager.") and value.lower() not in ("", "false", "0"):
            return name
        if ((name.startswith("include.") or name.startswith("includeif."))
                and name.endswith(".path") and value):
            return name
        parts = name.split(".")
        if (len(parts) == GIT_CONFIG_SUBSECTION_PARTS
                and parts[2] in EXEC_CAPABLE_SUBSECTIONS.get(parts[0], ())
                and value.lower() not in ("", "false", "0")):
            return name
    return ""


def _assignment_map(assignments: list) -> dict:
    """Return leading environment assignments with last-value-wins order."""
    values = {}
    for name, value in assignments:
        values[name] = value
    return values


def _parse_config_setting(setting: str):
    """Return a normalized git config pair, or None when malformed."""
    key, separator, value = setting.partition("=")
    key = key.strip().lower()
    if not separator or not key:
        return None
    return key, value


def _apply_git_environment_locations(state: dict, environment: dict) -> str:
    """Apply repository locations supplied through the environment."""
    if not state["explicit_git_dir"] and "GIT_DIR" in environment:
        value = environment["GIT_DIR"]
        if not value or is_ambiguous(value):
            return "GIT_DIR names an unresolved repository"
        state["git_dir"] = os.path.realpath(os.path.join(state["cwd"], value))
        state["explicit_git_dir"] = True
        state["repository_override"] = True
    if "GIT_COMMON_DIR" in environment:
        value = environment["GIT_COMMON_DIR"]
        if not value or is_ambiguous(value):
            return "GIT_COMMON_DIR names an unresolved repository"
        state["common_dir"] = os.path.realpath(os.path.join(state["cwd"], value))
        state["repository_override"] = True
    if not state["work_tree"] and "GIT_WORK_TREE" in environment:
        value = environment["GIT_WORK_TREE"]
        if not value or is_ambiguous(value):
            return "GIT_WORK_TREE names an unresolved work tree"
        state["work_tree"] = os.path.realpath(os.path.join(state["cwd"], value))
        state["repository_override"] = True
    return ""


def _finalize_git_state(state: dict, environment: dict) -> tuple:
    """Apply environment locations and return Git state or its error."""
    reason = _apply_git_environment_locations(state, environment)
    return (None, reason) if reason else (state, "")


def _apply_git_config_argument(args: list, index: int,
                               token: str, state: dict) -> tuple:
    """Apply one -c setting and return its last argument index."""
    setting = token[GIT_SHORT_OPTION_VALUE_INDEX:] if token != "-c" else ""
    if token == "-c":
        index += 1
        setting = args[index] if index < len(args) else ""
    parsed = _parse_config_setting(setting)
    if parsed is None:
        return index, "git -c has a missing or malformed setting"
    state["settings"].append(parsed)
    return index, ""


def _apply_git_path_argument(args: list, index: int,
                             token: str, state: dict) -> tuple:
    """Apply one repository path option and return its last argument index."""
    name, separator, attached = token.partition("=")
    value = attached if separator else ""
    if not separator:
        index += 1
        value = args[index] if index < len(args) else ""
    if not value or is_ambiguous(value):
        return index, f"git {name} has a missing or unresolved path"
    resolved = os.path.realpath(os.path.join(state["cwd"], value))
    if name == "-C":
        state["cwd"] = resolved
    elif name == "--git-dir":
        state["git_dir"] = resolved
        state["explicit_git_dir"] = True
    else:
        state["work_tree"] = resolved
    state["repository_override"] = True
    return index, ""


def _apply_git_global_argument(args: list, index: int, state: dict) -> tuple:
    """Apply one Git global argument and return its last index or an error."""
    token = args[index]
    name = token.partition("=")[0]
    if token == "-c" or (
            token.startswith("-c")
            and len(token) > GIT_SHORT_OPTION_VALUE_INDEX):
        return _apply_git_config_argument(args, index, token, state)
    if name in ("-C", "--git-dir", "--work-tree"):
        return _apply_git_path_argument(args, index, token, state)
    if name == "--config-env":
        return index, "git --config-env cannot be inspected from command text"
    return index, ""


def _git_global_state(args: list, cwd: str, environment: dict = None) -> tuple:
    """Return invocation settings and repository location, or an error."""
    environment = environment or {}
    state = {
        "settings": [],
        "cwd": os.path.realpath(cwd or "."),
        "git_dir": "",
        "common_dir": "",
        "work_tree": "",
        "explicit_git_dir": False,
        "repository_override": False,
    }
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index, reason = _apply_git_global_argument(args, index, state)
        if reason:
            return None, reason
        index += 1
    return _finalize_git_state(state, environment)


def _read_gitdir_pointer(path: str, marker: str):
    """Return a path stored in a small git administrative pointer file."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read(MAX_REASON_VALUE).strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError):
        return None
    if marker:
        name, separator, raw = raw.partition(":")
        if name.strip().lower() != marker or not separator:
            return None
    return raw.strip() or None


def _parent_repository_dir(current: str, device: int) -> tuple:
    """Return the next parent on the same device, or an inspection error."""
    parent = os.path.dirname(current)
    if parent == current:
        return "", ""
    try:
        parent_device = os.stat(parent).st_dev
    except OSError:
        return None, "a parent repository directory could not be inspected"
    if parent_device != device:
        return "", ""
    return parent, ""


def _discover_dot_git(cwd: str) -> tuple:
    """Return the .git entry Git discovers upward, or an inspection error."""
    current = os.path.realpath(cwd or ".")
    try:
        device = os.stat(current).st_dev
    except OSError:
        return None, "the repository search root could not be inspected"
    for _depth in range(MAX_REPO_DISCOVERY_DEPTH):
        dot_git = os.path.join(current, ".git")
        try:
            os.stat(dot_git)
        except FileNotFoundError:
            pass
        except OSError:
            return None, "a parent repository entry could not be inspected"
        else:
            return dot_git, ""
        parent, reason = _parent_repository_dir(current, device)
        if parent is None:
            return None, reason
        if not parent:
            return "", ""
        current = parent
    return None, "the parent repository search exceeded its depth limit"


def _redirected_repo_config_paths(dot_git: str) -> tuple:
    """Return config paths reached through a .git pointer file."""
    value = _read_gitdir_pointer(dot_git, "gitdir")
    if value is None:
        return None, "the redirected repository config could not be resolved"
    git_dir = os.path.realpath(os.path.join(os.path.dirname(dot_git), value))
    common = _read_gitdir_pointer(os.path.join(git_dir, "commondir"), "")
    if common is None:
        return None, "the redirected repository common config could not be resolved"
    if not common:
        return [(os.path.join(git_dir, "config"), True)], ""
    common_dir = os.path.realpath(os.path.join(git_dir, common))
    return [
        (os.path.join(common_dir, "config"), True),
        (os.path.join(git_dir, "config"), False),
        (os.path.join(git_dir, "config.worktree"), False),
    ], ""


def _repo_config_paths(state: dict) -> tuple:
    """Return repository config paths with required-file markers."""
    if state["common_dir"]:
        paths = [(os.path.join(state["common_dir"], "config"), True)]
        if state["git_dir"] and state["git_dir"] != state["common_dir"]:
            paths.append((os.path.join(state["git_dir"], "config.worktree"), False))
        return paths, ""
    if state["explicit_git_dir"]:
        return [(os.path.join(state["git_dir"], "config"), True)], ""
    dot_git, reason = _discover_dot_git(state["cwd"])
    if dot_git is None:
        return None, reason
    if not dot_git:
        return [(os.path.join(state["cwd"], ".git", "config"), False)], ""
    if not os.path.isfile(dot_git):
        return [(os.path.join(dot_git, "config"), False)], ""
    return _redirected_repo_config_paths(dot_git)


def _environment_config(environment: dict) -> tuple:
    """Return config settings supplied through GIT_CONFIG_COUNT."""
    raw_count = environment.get("GIT_CONFIG_COUNT")
    if raw_count is None:
        return [], ""
    try:
        count = int(raw_count)
    except ValueError:
        return None, "GIT_CONFIG_COUNT is not a non-negative integer"
    if count < 0 or count > MAX_GIT_CONFIG_COUNT:
        return None, "GIT_CONFIG_COUNT is outside the inspectable range"
    settings = []
    for index in range(count):
        key = environment.get(f"GIT_CONFIG_KEY_{index}")
        value = environment.get(f"GIT_CONFIG_VALUE_{index}")
        if key is None or value is None:
            return None, "GIT_CONFIG_COUNT names an incomplete key/value vector"
        parsed = _parse_config_setting(f"{key}={value}")
        if parsed is None:
            return None, "GIT_CONFIG_COUNT names a malformed setting"
        settings.append(parsed)
    return settings, ""


CONFIG_PATH_ENV = (
    "GIT_CONFIG", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_LOCAL", "GIT_CONFIG_WORKTREE",
)


def _read_invocation_configs(state: dict, environment: dict,
                             assigned_names: set = None) -> tuple:
    """Return merged file config entries or a fail-closed reason."""
    assigned_names = assigned_names or set()
    entries = {}
    for variable in CONFIG_PATH_ENV:
        path = environment.get(variable)
        if path is None:
            continue
        if not path or is_ambiguous(path):
            return None, f"{variable} names an uninspectable config path"
        resolved = os.path.realpath(os.path.join(state["cwd"], path))
        parsed = _parse_git_config_path(resolved)
        if parsed is None or (variable in assigned_names and not os.path.isfile(resolved)):
            return None, f"{variable} config could not be read"
        entries.update(parsed)
    repo_paths, reason = _repo_config_paths(state)
    if repo_paths is None:
        return None, reason
    for repo_path, required in repo_paths:
        parsed = _parse_git_config_path(repo_path)
        if parsed is None or (required and not os.path.isfile(repo_path)):
            return None, "the redirected repository config could not be read"
        entries.update(parsed)
    return entries, ""


def _environment_exec_key(environment: dict) -> str:
    """Return the first environment variable that makes a git read execute."""
    pager = environment.get("GIT_PAGER")
    if pager and pager.strip().lower() not in SAFE_PAGER_VALUES:
        return "GIT_PAGER"
    pager = environment.get("PAGER")
    if pager and pager.strip().lower() not in SAFE_PAGER_VALUES:
        return "PAGER"
    if environment.get("GIT_EXTERNAL_DIFF"):
        return "GIT_EXTERNAL_DIFF"
    return ""


def is_relevant_git_environment(name: str) -> bool:
    """Return True when a variable can alter inspected Git behavior."""
    return (name in {
        "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_PAGER", "PAGER",
        "GIT_EXTERNAL_DIFF", "GIT_CONFIG", "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_LOCAL", "GIT_CONFIG_WORKTREE",
        "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL",
    } or name.startswith("GIT_CONFIG_KEY_")
        or name.startswith("GIT_CONFIG_VALUE_"))


def _git_environment(assignments: list) -> dict:
    """Return effective relevant environment values for one invocation."""
    environment = {
        name: value for name, value in os.environ.items()
        if is_relevant_git_environment(name)
    }
    environment.update(_assignment_map(assignments))
    return environment


def _effective_git_entries(state: dict, environment: dict,
                           assigned_names: set = None) -> tuple:
    """Return merged config entries and ordered invocation settings."""
    entries, reason = _read_invocation_configs(
        state, environment, assigned_names)
    if entries is None:
        return None, None, reason
    env_settings, reason = _environment_config(environment)
    if env_settings is None:
        return None, None, reason
    settings = env_settings + state["settings"]
    for key, value in settings:
        entries[key] = value
    return entries, settings, ""


def git_read_verdict(args: list, cwd: str, assignments: list) -> tuple:
    """Classify executable settings and config paths for one git read."""
    environment = _git_environment(assignments)
    if "GIT_CONFIG_PARAMETERS" in environment:
        return "ask", "GIT_CONFIG_PARAMETERS cannot be inspected safely"
    state, reason = _git_global_state(args, cwd, environment)
    if state is None:
        return "ask", reason
    assigned_names = {name for name, _value in assignments}
    entries, _, reason = _effective_git_entries(
        state, environment, assigned_names)
    if entries is None:
        return "ask", reason
    found = _environment_exec_key(environment) or _exec_capable_key(entries)
    if not found:
        return "", ""
    return "ask", (f"a git read sets {found}, which names a program git runs")


def _shell_alias_write_label(expansion: str, entries: dict,
                             depth: int, visited: frozenset) -> tuple:
    """Return a write label found inside a Git shell alias."""
    try:
        shell_tokens = shlex.split(expansion[1:])
    except ValueError:
        return "", "git shell alias could not be inspected"
    for index, token in enumerate(shell_tokens):
        if os.path.basename(token).lower().removesuffix(".exe") != "git":
            continue
        nested, nested_rest = git_subcommand(shell_tokens[index + 1:])
        label, reason = _alias_write_label(
            nested, nested_rest, entries, depth + 1, visited)
        if label or reason:
            return label, reason
        return "", "git shell alias may hide a Git write"
    return "", "git shell alias may execute an uninspectable command"


def _alias_write_label(subcommand: str, rest: list,
                       entries: dict, depth: int = 0,
                       visited: frozenset = frozenset()) -> tuple:
    """Return a resolved write label and an alias-resolution error."""
    if depth >= MAX_GIT_ALIAS_DEPTH:
        return "", "git alias expansion exceeds the inspection limit"
    if subcommand in ("commit", "push"):
        return f"git {subcommand}", ""
    normalized_subcommand = subcommand.lower()
    if normalized_subcommand in visited:
        return "", "git alias expansion contains a cycle"
    expansion = entries.get(f"alias.{normalized_subcommand}", "")
    if not expansion:
        return "", ""
    next_visited = visited | {normalized_subcommand}
    if expansion.startswith("!"):
        return _shell_alias_write_label(
            expansion, entries, depth, next_visited)
    try:
        expanded = shlex.split(expansion)
    except ValueError:
        return "", "git alias could not be inspected"
    nested, nested_rest = git_subcommand(expanded + rest)
    return _alias_write_label(
        nested, nested_rest, entries, depth + 1, next_visited)


def _git_write_context(state: dict, environment: dict, assignments: list,
                       settings: list, resolution: tuple) -> dict:
    """Return structured context for an effective Git write."""
    label, error = resolution
    return {
        "label": label,
        "error": error,
        "cwd": state["cwd"],
        "git_dir": state["git_dir"],
        "common_dir": state["common_dir"],
        "work_tree": state["work_tree"],
        "repository_override": state["repository_override"],
        "assignments": assignments,
        "settings": settings,
        "environment": environment,
    }


def _could_be_alias(subcommand: str) -> bool:
    """Return True when an unknown subcommand may resolve through config."""
    return bool(subcommand) and (
        is_ambiguous(subcommand) or subcommand not in KNOWN_SUBCOMMANDS)


def _ambiguous_git_context(state: dict, environment: dict,
                           assignments: list, subcommand: str,
                           reason: str) -> dict:
    """Return a fail-closed context for an alias source we cannot inspect."""
    label = f"git {sanitize(subcommand)}" if subcommand else "ambiguous git write"
    return _git_write_context(
        state, environment, assignments, [], (label, reason))


def _fallback_git_state(cwd: str) -> dict:
    """Return minimal state for a Git write whose globals were malformed."""
    return {
        "cwd": os.path.realpath(cwd or "."), "git_dir": "",
        "common_dir": "", "work_tree": "", "repository_override": True,
    }


def _resolve_git_write_context(state: dict, environment: dict,
                               assignments: list, subcommand: str,
                               rest: list) -> dict:
    """Resolve aliases and return context for a Git write."""
    direct_label = f"git {subcommand}" if subcommand in ("commit", "push") else ""
    assigned_names = {name for name, _value in assignments}
    entries, settings, reason = _effective_git_entries(
        state, environment, assigned_names)
    if entries is None:
        if direct_label or _could_be_alias(subcommand):
            return _git_write_context(
                state, environment, assignments, [],
                (direct_label or f"git {sanitize(subcommand)}", reason))
        return {}
    if is_ambiguous(subcommand):
        return _ambiguous_git_context(
            state, environment, assignments, subcommand,
            "git subcommand is nonliteral and cannot be resolved")
    label, alias_error = _alias_write_label(subcommand, rest, entries)
    if not label and not alias_error:
        return {}
    return _git_write_context(
        state, environment, assignments, settings,
        (label or f"git {sanitize(subcommand)}", alias_error))


def git_write_context(args: list, cwd: str, assignments: list) -> dict:
    """Return effective context for a Git commit or push, including aliases."""
    subcommand, rest = git_subcommand(args)
    direct_label = f"git {subcommand}" if subcommand in ("commit", "push") else ""
    environment = _git_environment(assignments)
    state, reason = _git_global_state(args, cwd, environment)
    if state is None:
        if direct_label or _could_be_alias(subcommand):
            return _ambiguous_git_context(
                _fallback_git_state(cwd), environment, assignments,
                subcommand, reason)
        return {}
    if "GIT_CONFIG_PARAMETERS" in environment:
        if direct_label or _could_be_alias(subcommand):
            return _ambiguous_git_context(
                state, environment, assignments, subcommand,
                "GIT_CONFIG_PARAMETERS cannot be inspected safely")
        return {}
    return _resolve_git_write_context(
        state, environment, assignments, subcommand, rest)


def git_checker_environment(context: dict) -> dict:
    """Return a constrained child environment matching a Git invocation."""
    environment = dict(os.environ)
    for name, value in context.get("assignments", []):
        if is_relevant_git_environment(name):
            environment[name] = value
    locations = {
        "GIT_DIR": context.get("git_dir", ""),
        "GIT_COMMON_DIR": context.get("common_dir", ""),
        "GIT_WORK_TREE": context.get("work_tree", ""),
    }
    for name, value in locations.items():
        if value:
            environment[name] = value
    settings = context.get("settings", [])
    if settings:
        for name in list(environment):
            if (name == "GIT_CONFIG_COUNT" or name.startswith("GIT_CONFIG_KEY_")
                    or name.startswith("GIT_CONFIG_VALUE_")):
                environment.pop(name)
        environment["GIT_CONFIG_COUNT"] = str(len(settings))
        for index, (key, value) in enumerate(settings):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


def environment_assignment_verdict(program: str, args: list) -> tuple:
    """Gate persistent shell assignment of variables that alter Git reads."""
    name = os.path.basename(program).lower()
    exports = name == "export"
    if name in ("declare", "typeset"):
        exports = any(
            token == "--export" or (is_short_group(token) and "x" in token)
            for token in args)
    if not exports:
        return "", ""
    for token in args:
        if token.startswith("-"):
            continue
        name = token.split("=", 1)[0]
        if is_relevant_git_environment(name):
            return "ask", (f"exporting {sanitize(name)} changes how a later "
                           "git read resolves or executes programs")
    return "", ""


def resolve_alias(cwd: str, subcommand: str) -> str:
    """Return what `subcommand` expands to, or an empty string."""
    entries = parse_git_config(cwd)
    if not entries:
        return ""
    return entries.get(f"alias.{subcommand.lower()}", "")


def resolve_branch_alias(subcommand: str, arguments: list, entries: dict) -> tuple:
    """Return literal Git arguments or an explicit alias inspection error."""
    visited = set()
    for _depth in range(MAX_GIT_ALIAS_DEPTH):
        if not subcommand or is_ambiguous(subcommand):
            return subcommand, arguments, "Git subcommand is unresolved"
        if subcommand in KNOWN_SUBCOMMANDS or subcommand in ("symbolic-ref", "update-ref"):
            return subcommand, arguments, ""
        if subcommand in visited:
            return subcommand, arguments, "Git alias expansion contains a cycle"
        visited.add(subcommand)
        expansion = entries.get(f"alias.{subcommand}")
        if not expansion:
            return subcommand, arguments, "Git alias is unresolved"
        if expansion.startswith("!"):
            return subcommand, arguments, "Git shell alias has opaque execution"
        if len(expansion) > MAX_CONFIG_BYTES:
            return subcommand, arguments, "Git alias exceeds the inspection limit"
        try:
            expanded = shlex.split(expansion)
        except ValueError:
            return subcommand, arguments, "Git alias syntax is incomplete"
        if not expanded or expanded[0].startswith("-"):
            return subcommand, arguments, "Git alias changes unresolved invocation settings"
        subcommand = expanded[0]
        arguments = expanded[1:] + arguments
    return subcommand, arguments, "Git alias expansion exceeds the inspection limit"


def _branch_global_arguments(args: list, environment: dict) -> tuple:
    """Normalize inspectable global options without invoking a shell."""
    normalized = []
    index = 0
    while index < len(args) and args[index].startswith("-"):
        token = args[index]
        name, separator, value = token.partition("=")
        if name in ("--exec-path", "--namespace", "--super-prefix", "--bare"):
            return None, "Git global option changes opaque execution or repository settings"
        if name == "--config-env":
            if not separator:
                index += 1
                value = args[index] if index < len(args) else ""
            key, separator, variable = value.partition("=")
            if not separator or not key or variable not in environment:
                return None, "Git config environment value is missing"
            normalized.extend(["-c", key + "=" + environment[variable]])
        elif token.startswith("-C") and token != "-C":
            normalized.extend(["-C", token[2:]])
        else:
            normalized.append(token)
            if name in GIT_VALUE_OPTIONS and not separator:
                index += 1
                if index >= len(args):
                    return None, "Git global option value is missing"
                normalized.append(args[index])
        index += 1
    normalized.extend(args[index:])
    return normalized, ""


def _trusted_config_executable(cwd: str) -> str:
    """Select the installed Git resolver and reject checkout-local programs."""
    resolver_path = resolved_under(policy_root(), "scripts", "trusted_git.py")
    if resolver_path is None or not os.path.isfile(resolver_path):
        raise OSError("trusted Git resolver is missing")
    spec = importlib.util.spec_from_file_location("_branch_trusted_git", resolver_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    executable = module.resolve_git(policy_root())
    target_root = os.path.realpath(cwd or ".")
    dot_git, reason = _discover_dot_git(target_root)
    if dot_git is None:
        raise OSError(reason)
    if dot_git:
        target_root = os.path.dirname(dot_git)
    candidate = os.path.realpath(executable)
    if candidate == target_root or candidate.startswith(target_root + os.sep):
        raise OSError("Git executable belongs to the target checkout")
    return executable


def _collect_config_output(process, result: list) -> None:
    """Bound pipe consumption before retaining configuration output."""
    try:
        output = process.stdout.read(MAX_CONFIG_BYTES + 1)
        if len(output) > MAX_CONFIG_BYTES:
            process.kill()
            result.append(None)
        else:
            result.append(output)
    except OSError:
        result.append(None)


def _run_config_reader(command: list, environment: dict) -> bytes:
    """Read bounded config output and supervise the pipe reader to completion."""
    with subprocess.Popen(
        command, cwd=policy_root(), env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as process:
        output = []
        reader = threading.Thread(target=_collect_config_output, args=(process, output))
        reader.start()
        try:
            process.wait(timeout=CONFIG_READ_TIMEOUT_SECONDS)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            reader.join()
        if process.returncode or not output or output[0] is None:
            raise OSError("Git configuration read failed or exceeded the output limit")
        return output[0]


def _config_read_environment(environment: dict) -> dict:
    """Preserve config selection without inheriting tracing, redirection, or loader controls."""
    selected = {
        name: value for name, value in environment.items()
        if name.upper() in CONFIG_READ_ENVIRONMENT
        or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }
    selected.update({"GIT_PAGER": "", "PAGER": "", "GIT_TERMINAL_PROMPT": "0",
                     "GIT_OPTIONAL_LOCKS": "0", "GIT_TRACE": "0", "GIT_TRACE2": "0",
                     "GIT_TRACE2_EVENT": "0", "GIT_TRACE2_PERF": "0"})
    # Explicit environment values also override Trace2 destinations in Git configuration.
    return selected


def _branch_config_entries(state: dict, environment: dict) -> dict:
    """Read effective aliases through the fixed native config operation."""
    environment = _config_read_environment(environment)
    for variable, key in (("GIT_DIR", "git_dir"), ("GIT_WORK_TREE", "work_tree"),
                          ("GIT_COMMON_DIR", "common_dir")):
        if state[key]:
            environment[variable] = state[key]
    command = [_trusted_config_executable(state["cwd"]), "-C", state["cwd"], "--no-pager"]
    for key, value in state["settings"]:
        command.extend(["-c", key + "=" + value])
    command.extend(["config", "--null", "--list", "--includes"])
    raw = _run_config_reader(command, environment).decode("utf-8")
    entries = {}
    for record in raw.split("\0"):
        if not record:
            continue
        key, _separator, value = record.partition("\n")
        entries[key] = value
    return entries


def git_branch_context(args: list, cwd: str, assignments: list) -> dict:
    """Resolve branch targets using one effective configuration snapshot."""
    environment = dict(os.environ)
    environment.update(_assignment_map(assignments))
    if any(name in ("PATH", "GIT_EXEC_PATH", "LD_PRELOAD", "LD_LIBRARY_PATH")
           or name.startswith("DYLD_") for name, _value in assignments):
        return _ambiguous_git_context(_fallback_git_state(cwd), {}, [], "",
                                      "Git invocation changes executable loading")
    args, reason = _branch_global_arguments(args, environment)
    if args is None:
        return _ambiguous_git_context(_fallback_git_state(cwd), environment, assignments, "", reason)
    subcommand, arguments = git_subcommand(args)
    state, reason = _git_global_state(args, cwd, environment)
    if state is None:
        return _ambiguous_git_context(
            _fallback_git_state(cwd), environment, assignments, subcommand, reason)
    if "GIT_CONFIG_PARAMETERS" in environment:
        return _ambiguous_git_context(state, environment, assignments, subcommand,
                                      "GIT_CONFIG_PARAMETERS cannot be inspected safely")
    settings, reason = _environment_config(environment)
    if settings is None:
        return _ambiguous_git_context(state, environment, assignments, subcommand, reason)
    try:
        entries = _branch_config_entries(state, environment)
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        return _ambiguous_git_context(state, environment, assignments, subcommand,
                                      "Git configuration inspection failed; restore readable bounded config")
    subcommand, arguments, reason = resolve_branch_alias(subcommand, arguments, entries)
    context = _git_write_context(
        state, environment, assignments, settings, (f"git {sanitize(subcommand)}", reason))
    context["subcommand"] = subcommand
    context["arguments"] = arguments
    return context


# Removing recovery data is the step that makes destruction irreversible,
# and it is the highest-signal indicator in the data-destruction family.
# No agent workflow deletes a shadow copy or a backup catalog.
RECOVERY_DESTRUCTION = {
    "vssadmin": ("delete",),
    "wmic": ("shadowcopy",),
    "wbadmin": ("delete",),
    "bcdedit": ("recoveryenabled", "bootstatuspolicy", "safeboot"),
}
# Programs that write over a device or lay down a filesystem.
DEVICE_WRITERS = frozenset({"mkfs", "diskpart", "format", "cipher", "fdisk",
                            "parted", "sgdisk", "wipefs", "blkdiscard",
                            "shred", "sdelete", "wipe", "dd", "hdparm",
                            "badblocks", "nvme"})
# Programs with no purpose but to partition, format, or wipe. Unlike dd or
# shred, no invocation of these operates on a single ordinary file, so an
# unparseable one has no benign reading to fall back on.
ALWAYS_DESTRUCTIVE = frozenset({"diskpart", "fdisk", "sgdisk", "parted",
                                "wipefs", "blkdiscard", "mkfs", "format",
                                "cipher", "dd", "hdparm"})
# Programs that fetch a remote resource to standard output.
DOWNLOADERS = frozenset({"curl", "wget", "fetch", "invoke-webrequest", "iwr",
                         "invoke-restmethod", "irm", "httpie", "http", "aria2c"})
# Programs that execute whatever they are handed on standard input.
INPUT_EXECUTORS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "busybox",
                             "fish", "csh", "tcsh", "python", "python3",
                             "perl", "ruby", "node", "php", "iex",
                             "invoke-expression", "pwsh", "powershell"})
DEVICE_PATH_MARKERS = ("/dev/", "\\\\.\\physicaldrive", "\\\\.\\",
                       "/dev/disk")
# Commands whose target set is decided at run time rather than written out.
LOGGING_DISABLERS = {
    "aws": ("stop-logging", "delete-trail", "delete-log-group"),
    "az": ("diagnostic-settings",),
    "gcloud": ("sinks",),
    "vim-cmd": ("destroy", "unregister"),
    "esxcli": ("destroy",),
    "auditctl": ("-D",),
    "systemctl": ("auditd", "rsyslog"),
}
# Mount points and shared temp directories DET0146 calls out by name: a
# volume mount is somebody else's data reached through this filesystem.
VOLUME_PREFIXES = ("/mnt/", "/media/", "/volumes/", "/private/tmp",
                   "/var/tmp", "/srv/", "/net/", "/mounts/")

UNBOUNDED_OPERATIONS = {
    "find": ("-delete", "-exec", "-execdir", "-ok"),
    "git": ("clean",),
    "truncate": ("-s",),
    "sed": ("-i",),
    "perl": ("-i",),
    "xargs": ("rm", "shred", "truncate"),
}
GLOB_CHARACTERS = "*?["


def _mentions_device(args: list) -> bool:
    """Return True if any operand names a raw device rather than a file."""
    for token in args:
        lowered = token.lower()
        value = lowered.partition("=")[2] if "=" in lowered else lowered
        if any(marker in value for marker in DEVICE_PATH_MARKERS):
            # /dev/zero and /dev/urandom are sources, not targets.
            if value.rstrip("0123456789").endswith(("/zero", "/urandom",
                                                    "/random", "/null")):
                continue
            return True
        if len(value) == DRIVE_ROOT_LENGTH and value[1] == ":":
            return True
        if lowered.startswith("/w:"):
            return True
    return False


def recovery_verdict(program: str, args: list) -> tuple:
    """Return (decision, reason) for a command that destroys recovery data."""
    name = os.path.basename(program).lower().removesuffix(".exe")
    markers = RECOVERY_DESTRUCTION.get(name)
    if not markers:
        return "", ""
    lowered = [token.lower() for token in args]
    if not any(marker in token for marker in markers for token in lowered):
        return "", ""
    return "deny", (f"{name} removing recovery data: this is what makes a "
                    "destructive act irreversible")


def device_write_verdict(program: str, args: list) -> tuple:
    """Return (decision, reason) for a device wipe or a file overwrite."""
    name = os.path.basename(program).lower().removesuffix(".exe")
    # mkfs ships as mkfs.ext4, mkfs.xfs, and so on.
    base = name.split(".", 1)[0] if name.startswith("mkfs") else name
    if base not in DEVICE_WRITERS:
        return "", ""
    name = base
    # These exist only to partition or wipe, so any invocation counts.
    if name in ALWAYS_DESTRUCTIVE and name not in ("cipher",):
        return "deny", (f"{name} partitions or lays down a filesystem, which "
                        "destroys every file on the target at once")
    if _mentions_device(args):
        return "deny", (f"{name} writing over a device or filesystem, which "
                        "destroys every file on it at once")
    if name in ("shred", "sdelete", "wipe"):
        return "ask", f"{name} overwriting file contents in place"
    return "", ""


def _is_shallow_glob(token: str) -> bool:
    """Return True for a glob whose parent is a root, such as /* or ~/*."""
    if not any(character in token for character in GLOB_CHARACTERS):
        return False
    parent = token.rstrip("*?[]/")
    return is_root_target(parent) or is_root_target(parent + "/")


def mass_operation_verdict(program: str, args: list) -> tuple:
    """Return (decision, reason) for a command with an unbounded target set.

    A glob rooted at a system directory denies for the same reason the
    directory itself does. Everything else asks: the gate cannot count
    what a pattern will match, and a command that decides its own targets
    at run time is one a person should see.
    """
    for token in args:
        if _is_shallow_glob(token):
            return "deny", ("an operation over every entry of a root or "
                            "system directory")
    name = os.path.basename(program).lower()
    markers = UNBOUNDED_OPERATIONS.get(name)
    if not markers:
        return "", ""
    lowered = [token.lower() for token in args]
    if any(flag in lowered for flag in ("-n", "--dry-run", "--no-act")):
        return "", ""
    if not any(marker in lowered for marker in markers):
        return "", ""
    if any(any(character in token for character in GLOB_CHARACTERS)
           for token in args) or name in ("find", "git", "xargs"):
        return "ask", (f"{name} over a target set decided at run time, which "
                       "the gate cannot count before it runs")
    return "", ""


def destruction_verdict(program: str, args: list) -> tuple:
    """Return the strongest data-destruction verdict for one command."""
    verdict = ("", "")
    for check in (recovery_verdict, device_write_verdict,
                  mass_operation_verdict, logging_verdict,
                  disguised_destruction_verdict):
        verdict = strongest(verdict, check(program, args))
    return verdict


def logging_verdict(program: str, args: list) -> tuple:
    """Return (decision, reason) for disabling logging or purging a disk.

    Destroying the record of an act belongs to the same campaign as the
    act. These are read as commands rather than counted as events, which
    is all a per-call gate can do.
    """
    name = os.path.basename(program).lower().removesuffix(".exe")
    markers = LOGGING_DISABLERS.get(name)
    if not markers:
        return "", ""
    lowered = " ".join(args).lower()
    if not any(marker in lowered for marker in markers):
        return "", ""
    if name in ("vim-cmd", "esxcli"):
        return "deny", f"{name} destroying a virtual disk or registration"
    return "ask", (f"{name} disabling an audit or logging service, which "
                   "removes the record of what follows")


def volume_verdict(recursive: bool, operands: list) -> tuple:
    """Return (decision, reason) for a recursive act on a mounted volume."""
    if not recursive:
        return "", ""
    for token in operands:
        candidate = token.strip().strip('"').strip("'").replace("\\", "/")
        lowered = posixpath.normpath(candidate).lower() + "/"
        if any(lowered.startswith(prefix) for prefix in VOLUME_PREFIXES):
            return "ask", ("a recursive act under a mount point or shared "
                           "temp directory, which is not this project's data")
    return "", ""


def gated_keywords() -> tuple:
    """Return every program name this core classifies.

    Derived rather than written out. A hardcoded list is a second place to
    remember, and the one that decides whether an unparseable command
    fails closed: cipher /w:C:\\ does not tokenize, and a list that had
    not learned "cipher" waved it through.
    """
    names = {"rm", "git"}
    names.update(DEVICE_WRITERS)
    names.update(RECOVERY_DESTRUCTION)
    names.update(LOGGING_DISABLERS)
    names.update(UNBOUNDED_OPERATIONS)
    names.update(CMD_DELETE_VERBS)
    names.update(FORGE_PROGRAMS)
    names.update({"mv", "move", "chmod", "chown", "chgrp", "cat", "tee",
                  "alias", "kill", "killall", "pkill", "hdparm", "install"})
    names.update({"mkfs.ext4", "mkfs.xfs", "mkfs.btrfs", "mkfs.vfat"})
    return tuple(sorted(names))


def _segment_program(segment: list) -> str:
    """Return the real program a segment runs, past any wrapper."""
    for token in segment:
        name = os.path.basename(token).lower().removesuffix(".exe")
        if name not in ("sudo", "doas", "env", "command", "nohup", "time"):
            return name
    return ""


def remote_execution_verdict(segments: list) -> tuple:
    """Return (decision, reason) for anything piped into an interpreter.

    Whatever arrives on standard input is executed without being read, so
    the producer of that stream decides what runs. `curl | bash` hands the
    choice to a remote host; `history | sh` hands it to whatever the shell
    happens to remember. Neither is a decision anyone can give consent to
    in advance, so both are prohibited rather than asked about.
    """
    previous = ""
    for segment in segments:
        if not segment:
            continue
        program = _segment_program(segment)
        if previous and program in INPUT_EXECUTORS:
            source = ("the remote host" if previous in DOWNLOADERS
                      else f"whatever {sanitize(previous)} produces")
            return "deny", (f"a pipe into {sanitize(program)}: {source} "
                            "chooses what runs, and nothing reads it first")
        previous = program
    return "", ""


# Operators that start the next command after the previous one ends rather
# than feeding it that command's output. PowerShell's & is its call
# operator, so each shell supplies its own set.
BASH_LIST_OPERATORS = frozenset({"&&", "||", ";", "&"})
POWERSHELL_LIST_OPERATORS = frozenset({"&&", "||", ";"})
PIPE_OPERATORS = frozenset({"|", "|&"})
GROUP_OPENERS = frozenset({"(", "{"})
GROUP_CLOSERS = frozenset({")", "}"})
# Deeper nesting exceeds the inspection limit and denies.
MAX_GROUP_DEPTH = 32
FUSED_OPERATOR_CHARACTERS = frozenset("();|&")
TWO_CHARACTER_OPERATORS = frozenset({"&&", "||", "|&"})


def operator_parts(token: str) -> list:
    """Split a fused run of shell punctuation such as `|(` into its operators.

    shlex joins adjacent punctuation, so `curl x |(bash)` arrives as `|(`.
    The walk splits it so the pipe stays visible.
    """
    if len(token) < 2 or not set(token) <= FUSED_OPERATOR_CHARACTERS:
        return [token]
    parts = []
    index = 0
    while index < len(token):
        step = 2 if token[index:index + 2] in TWO_CHARACTER_OPERATORS else 1
        parts.append(token[index:index + step])
        index += step
    return parts


def split_pipelines(tokens: list, is_separator, list_operators: frozenset):
    """Return the command's pipelines, or None past MAX_GROUP_DEPTH.

    A pipeline is the run of segments joined by pipes. A list operator ends
    it, except inside a group opened on the receiving side of a pipe:
    `curl x | (cd /; bash)` still feeds curl's output to bash. A newline
    right after a pipe continues the pipeline, as Bash does.
    """
    pipelines = [[]]
    segment = []
    depth = 0
    piped_depth = None
    previous = ""
    for token in [part for token in tokens for part in operator_parts(token)]:
        if not is_separator(token):
            segment.append(token)
            previous = token
            continue
        if token == "\n" and previous in PIPE_OPERATORS:
            continue
        if segment:
            pipelines[-1].append(segment)
            segment = []
        if token in GROUP_OPENERS:
            depth += 1
            if depth > MAX_GROUP_DEPTH:
                return None
            if piped_depth is None and previous in PIPE_OPERATORS:
                piped_depth = depth
        elif token in GROUP_CLOSERS:
            if piped_depth == depth:
                piped_depth = None
            depth = max(depth - 1, 0)
        elif piped_depth is None and (token in list_operators or token == "\n"):
            pipelines.append([])
        previous = token
    if segment:
        pipelines[-1].append(segment)
    return [pipeline for pipeline in pipelines if pipeline]


def pipeline_execution_verdict(segments: list, tokens: list, is_separator,
                               list_operators: frozenset) -> tuple:
    """Return the strongest pipe-into-interpreter verdict across pipelines.

    A backtick substitution appends its segments out of order, so the
    whole command is read as one pipeline. Nesting past the bound denies,
    as the gate cannot read the pipelines inside it.
    """
    if "`" in tokens:
        return remote_execution_verdict(segments)
    pipelines = split_pipelines(tokens, is_separator, list_operators)
    if pipelines is None:
        return "deny", (f"group nesting over {MAX_GROUP_DEPTH} levels exceeds "
                        "the inspection limit")
    verdict = ("", "")
    for pipeline in pipelines:
        verdict = strongest(verdict, remote_execution_verdict(pipeline))
    return verdict


def run_fail_closed(entrypoint, deny) -> int:
    """Run a hook entrypoint and deny when it raises.

    Claude Code treats any exit code other than 2 as a non-blocking error,
    so an exception escaping a gate would let the command run. Only a broad
    catch fails closed on an error nobody anticipated. SystemExit and
    KeyboardInterrupt are not Exception subclasses and still propagate.
    """
    try:
        return entrypoint()
    except Exception as error:
        return deny(f"gate internal error {type(error).__name__}: "
                    f"{sanitize(error)}. The command was denied. Report it "
                    "to the repository maintainers.")


# Moving a file to a device discards it. The command reads as a move, and
# the file is gone with no delete anywhere in the line.
DISCARD_DESTINATIONS = ("/dev/null", "/dev/random", "/dev/urandom",
                        "/dev/zero", "nul", "nul:")
# chmod 000 and its symbolic equivalents leave a file nobody can open.
NO_ACCESS_MODES = frozenset({"000", "0000", "00000", "a-rwx", "ugo-rwx",
                             "ug-rwx", "a=", "ugo=", "u-rwx,g-rwx,o-rwx"})


def disguised_destruction_verdict(program: str, args: list) -> tuple:
    """Return (decision, reason) for destruction wearing another name."""
    name = os.path.basename(program).lower().removesuffix(".exe")
    operands = [token for token in args if not token.startswith("-")]
    if name in ("mv", "move") and operands:
        destination = operands[-1].strip().strip('"').strip("'").lower()
        if destination.rstrip("/") in DISCARD_DESTINATIONS:
            return "deny", (f"mv to {sanitize(destination)} discards the "
                            "file: a delete that reads as a move")
    if name == "chmod":
        for token in operands:
            if token.strip().lower() in NO_ACCESS_MODES:
                return "deny", ("chmod to a mode nobody can read, write, or "
                                "execute, which makes the file unusable "
                                "without deleting it")
    return "", ""


# Running as another user is a decision about authority, not about the
# command. It is asked for on its own terms, then the wrapped command is
# judged separately and the stronger verdict wins.
PRIVILEGE_PROGRAMS = frozenset({"sudo", "su", "doas", "pkexec", "runas",
                                "gsudo", "please"})
SU_COMMAND_OPTIONS = frozenset({"-c", "--command"})
SU_VALUE_OPTIONS = frozenset({
    "-g",
    "-G",
    "-s",
    "-w",
    "--group",
    "--shell",
    "--supp-group",
    "--whitelist-environment",
})


def su_target_verdict(arguments: list) -> tuple:
    """Classify the effective target and command payload of one su call."""
    for argument in arguments:
        lowered_argument = argument.casefold()
        option_name = lowered_argument.split("=", 1)[0]
        if option_name in SU_COMMAND_OPTIONS:
            return "deny", "su executes a command-string payload"
        if argument.startswith("-") and not argument.startswith("--"):
            if "c" in argument[1:]:
                return "deny", "su executes a command-string payload"
    argument_index = 0
    target_user = "root"
    target_found = False
    while argument_index < len(arguments):
        argument = arguments[argument_index]
        if argument in SU_VALUE_OPTIONS:
            argument_index += 2
            continue
        if argument in ("-", "-l", "--login", "-m", "-p", "--preserve-environment"):
            argument_index += 1
            continue
        if argument == "--":
            argument_index += 1
            if not target_found and argument_index < len(arguments):
                target_user = arguments[argument_index]
                target_found = True
            break
        if not target_found and not argument.startswith("-"):
            target_user = argument
            target_found = True
        argument_index += 1
    if is_ambiguous(target_user):
        return "deny", "su uses a dynamic target account"
    if target_user.casefold() == "root":
        return "deny", "su targets root explicitly or through its default"
    return "ask", f"su changes identity to {sanitize(target_user)}"


def privilege_verdict(tokens: list) -> tuple:
    """Return (decision, reason) when a statement escalates privilege."""
    for token_index, token in enumerate(tokens):
        name = os.path.basename(token).lower().removesuffix(".exe")
        if name == "su":
            return su_target_verdict(tokens[token_index + 1:])
        if name in PRIVILEGE_PROGRAMS:
            return "ask", (f"{name}: running as another user is the user's "
                           "call, whatever the command does")
        if not token.startswith("-"):
            break
    return "", ""


# A shell rc file runs on every future session, so a line added to one
# outlives the change that added it.
SHELL_PROFILES = ("/.bashrc", "/.bash_profile", "/.bash_login",
                  "/.bash_aliases", "/.bash_logout", "/.profile",
                  "/.zshrc", "/.zshenv", "/.zprofile", "/.zlogin",
                  "/.zlogout", "/.cshrc", "/.kshrc", "/.inputrc",
                  "/.config/fish/config.fish")
SCHEDULE_PROGRAMS = frozenset({"crontab", "schtasks", "at"})
FILESYSTEM_REPAIR = frozenset({"fsck", "e2fsck", "xfs_repair", "ntfsfix",
                               "chkdsk", "resize2fs", "tune2fs"})
PROCESS_PROGRAMS = frozenset({"kill", "killall", "pkill", "skill",
                              "taskkill", "stop-process"})
ALIAS_PROGRAMS = frozenset({"alias", "set-alias", "new-alias", "doskey"})
MODE_PROGRAMS = frozenset({"chmod", "chown", "chgrp", "icacls", "takeown"})
# Emptying a file destroys it without naming a delete.
TRUNCATION_SOURCES = ("/dev/null", "nul")


def _names_shell_profile(tokens: list) -> str:
    """Return the shell profile a token names, or an empty string."""
    for token in tokens:
        candidate = token.strip().strip('"').strip("'").replace("\\", "/")
        lowered = candidate.lower()
        if any(lowered.endswith(profile) or lowered == profile.lstrip("/")
               for profile in SHELL_PROFILES):
            return candidate
    return ""


def profile_verdict(program: str, args: list, redirects: list) -> tuple:
    """Return (decision, reason) for a write to a shell startup file."""
    name = os.path.basename(program).lower()
    writes = name in ("tee", "sed", "cp", "mv", "install", "truncate", "dd")
    named = _names_shell_profile(list(redirects) + (args if writes else []))
    if not named:
        return "", ""
    return "ask", (f"a write to {sanitize(named)}, which runs at the start of "
                   "every future shell session")


def process_verdict(program: str, args: list) -> tuple:
    """Return (decision, reason) for terminating processes."""
    name = os.path.basename(program).lower().removesuffix(".exe")
    if name not in PROCESS_PROGRAMS:
        return "", ""
    return "ask", (f"{name} terminating a process: what it stops and what "
                   "that loses is the user's call")


GIT_CONFIG_VALUE_OPTIONS = frozenset({
    "-f", "--file", "--blob", "--type", "--default", "--comment", "--value",
})
GIT_CONFIG_WRITE_SUBCOMMANDS = frozenset({"set"})


def _git_config_positionals(args: list) -> list:
    """Return git config operands after option values and the set subcommand."""
    positionals = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in GIT_CONFIG_VALUE_OPTIONS:
            index += 2
            continue
        if not token.startswith("-"):
            positionals.append(token)
        index += 1
    if positionals and positionals[0] in GIT_CONFIG_WRITE_SUBCOMMANDS:
        return positionals[1:]
    return positionals


def alias_verdict(program: str, args: list) -> tuple:
    """Return (decision, reason) for defining a command alias.

    An alias makes one name run another command, which defeats reading a
    command to know what it does. Listing aliases is not defining one.
    """
    name = os.path.basename(program).lower().removesuffix(".exe")
    if name in ALIAS_PROGRAMS:
        if not args or not any("=" in token or not token.startswith("-")
                               for token in args):
            return "", ""
        return "deny", (f"{name} defining a command alias, which makes a name "
                        "run something the name does not say")
    if name == "git" and args and args[0] == "config":
        settings = _git_config_positionals(args[1:])
        if settings and settings[0].lower().startswith("alias.") and len(settings) > 1:
            return "deny", ("git config defining an alias, which makes a "
                            "subcommand run something it does not name")
    return "", ""


def truncation_verdict(program: str, args: list, redirects: list) -> tuple:
    """Return (decision, reason) for emptying a file or writing a device."""
    for target in redirects:
        cleaned = target.strip().strip('"').strip("'")
        if _mentions_device([cleaned]):
            return "deny", (f"a redirect onto {sanitize(cleaned)}, which "
                            "writes over a device rather than a file")
    if not redirects:
        return "", ""
    name = os.path.basename(program).lower() if program else ""
    if not name or name == ":":
        return "deny", ("a bare redirect, which empties the target without "
                        "naming a delete")
    if name == "cat" and any(
            token.strip().lower().rstrip("/") in TRUNCATION_SOURCES
            for token in args):
        return "deny", ("a redirect from an empty device, which empties the "
                        "target without naming a delete")
    return "", ""


def mode_change_verdict(program: str, args: list) -> tuple:
    """Return (decision, reason) for a recursive mode change on a root."""
    name = os.path.basename(program).lower().removesuffix(".exe")
    if name not in MODE_PROGRAMS:
        return "", ""
    # Recursion is not what makes this dangerous. chown nobody /etc breaks
    # the machine with no -R anywhere in the line.
    for token in args:
        if token.startswith("-"):
            continue
        if is_root_target(token) or is_root_target(token.rstrip("/") + "/"):
            return "deny", (f"recursive {name} across a root or system "
                            "directory, which breaks every program that "
                            "depends on those permissions")
    return "", ""


def schedule_verdict(program: str, args: list) -> tuple:
    """Return (decision, reason) for removing scheduled work."""
    name = os.path.basename(program).lower().removesuffix(".exe")
    if name not in SCHEDULE_PROGRAMS:
        return "", ""
    lowered = [token.lower() for token in args]
    if name == "crontab" and ("-r" in lowered or "--remove" in lowered):
        return "deny", ("crontab -r removes every scheduled job at once, "
                        "with no copy kept and no prompt from crontab itself")
    if name == "schtasks" and "/delete" in lowered:
        return "deny", "schtasks deleting a scheduled task"
    return "", ""


def filesystem_repair_verdict(program: str, args: list) -> tuple:
    """Return (decision, reason) for a filesystem repair tool."""
    name = os.path.basename(program).lower().removesuffix(".exe")
    if name not in FILESYSTEM_REPAIR:
        return "", ""
    return "deny", (f"{name} rewrites filesystem metadata in place and can "
                    "discard data it cannot reconcile")


# Deleting a repository, a release, or a tag on the forge destroys work
# that is not in any local clone.
FORGE_PROGRAMS = frozenset({"gh", "glab", "hub", "tea"})
FORGE_DELETE_NOUNS = frozenset({"repo", "repository", "release", "project",
                                 "org", "organization", "gist", "secret",
                                 "environment", "cache", "run", "variable",
                                 "issue", "label"})
GH_GLOBAL_VALUE_OPTIONS = frozenset({"-R", "--repo", "--hostname"})
GH_SUBCOMMAND_VALUE_OPTIONS = frozenset({
    "-a", "-b", "-B", "-F", "-H", "-l", "-m", "-p", "-r", "-t", "-T",
    "--assignee", "--body", "--base", "--body-file", "--head", "--label",
    "--milestone", "--project", "--reviewer", "--title", "--template",
})
GH_COMMAND_VALUE_OPTIONS = (GH_GLOBAL_VALUE_OPTIONS
                            | GH_SUBCOMMAND_VALUE_OPTIONS
                            | frozenset({"-h", "-s", "-u", "--scopes"}))
GH_BROAD_AUTH_SCOPES = frozenset({"admin:org", "admin:public_key",
                                  "admin:repo_hook", "delete_repo", "gist",
                                  "project", "repo", "user", "workflow",
                                  "write:discussion", "write:org",
                                  "write:packages"})
# Labels record human decisions such as gate-change-approved (Rule 23), so
# no gh subcommand that writes them runs for an agent.
GH_LABEL_WRITE_COMMANDS = frozenset({
    ("pr", "create"), ("pr", "edit"), ("issue", "create"), ("issue", "edit"),
})
GH_LABEL_OPTIONS = ("--label", "--add-label", "--remove-label")
GH_LABEL_SHORT_OPTION = "-l"
GH_API_METHOD_OPTIONS = frozenset({"--method", "-X", "-x"})
GH_FALLBACK_CONFIG = "agents.githubfallback=confirmed"
GH_COMMAND_DENYLIST = Path(__file__).with_name("github-command-denylist.txt")


@lru_cache(maxsize=1)
def _load_github_command_denylist() -> tuple[frozenset, frozenset]:
    """Load GitHub CLI family and exact command-path bans."""
    try:
        lines = GH_COMMAND_DENYLIST.read_text(encoding="ascii").splitlines()
    except OSError as error:
        raise ValueError("GitHub CLI denylist is unavailable") from error
    families = set()
    paths = set()
    for line_number, line in enumerate(lines, 1):
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if fields[0] == "family" and len(fields) == 2:
            families.add((fields[1],))
        elif fields[0] == "path" and len(fields) > 1:
            paths.add(tuple(fields[1:]))
        else:
            raise ValueError(f"invalid GitHub CLI denylist line {line_number}")
    return frozenset(families), frozenset(paths)


def _github_command_denylist_verdict(command: list) -> tuple:
    """Return a denial for a configured GitHub CLI command path."""
    try:
        families, paths = _load_github_command_denylist()
    except ValueError as error:
        return "deny", str(error)
    command_path = _github_command_path(command)
    if command_path and command_path[:1] in families:
        if command_path[0] == "secret":
            return "deny", "gh secret operations expose or change hosted secrets"
        if command_path[0] == "variable":
            return "deny", "gh variable operations expose or change hosted variables"
        return "deny", f"gh {command_path[0]} is denied by policy"
    for path in paths:
        if command_path[:len(path)] == path:
            if path == ("repo", "delete"):
                return "deny", "gh repo delete removes work and is denied by policy"
            return "deny", f"gh {' '.join(path)} is denied by policy"
    return "", ""


def _github_command_path(command: list) -> tuple:
    """Return the noun and action after consuming option values."""
    command_path = []
    index = 0
    while index < len(command) and len(command_path) < 2:
        token = command[index]
        if token == "--":
            index += 1
            continue
        if token in GH_COMMAND_VALUE_OPTIONS:
            index += 2
            continue
        lowered = token.casefold()
        if any(lowered.startswith(option.casefold() + "=")
               for option in GH_COMMAND_VALUE_OPTIONS if option.startswith("--")):
            index += 1
            continue
        if any(token.casefold().startswith(option.casefold())
               and len(token) > len(option)
               for option in GH_COMMAND_VALUE_OPTIONS
               if len(option) == 2):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        command_path.append(lowered)
        index += 1
    return tuple(command_path)


def _github_command_args(args: list) -> list:
    """Return gh arguments after supported global options."""
    index = 0
    while index < len(args):
        token = args[index]
        lowered = token.lower()
        if token in GH_GLOBAL_VALUE_OPTIONS:
            index += 2
            continue
        if any(lowered.startswith(option.lower() + "=")
               for option in GH_GLOBAL_VALUE_OPTIONS):
            index += 1
            continue
        if lowered.startswith("-r") and token != "-R":
            index += 1
            continue
        if not token.startswith("-"):
            break
        index += 1
    return args[index:]


def _option_value(args: list, names: frozenset) -> str:
    """Return a command option value in separate or joined form."""
    for index, token in enumerate(args):
        lowered = token.lower()
        for name in names:
            is_short = len(name) == 2 and name.startswith("-") and not name.startswith("--")
            target = token if is_short else lowered
            option_name = name if is_short else name.lower()
            if target == option_name:
                return args[index + 1] if index + 1 < len(args) else ""
            prefix = option_name + "="
            if target.startswith(prefix):
                return token[len(prefix):]
            if is_short and target.startswith(option_name) and len(token) > 2:
                return token[2:]
    return ""


def _option_values(args: list, names: frozenset) -> list:
    """Return every value of a command option in separate or joined form."""
    values = []
    for index, token in enumerate(args):
        for name in names:
            is_short = len(name) == 2 and name.startswith("-") and not name.startswith("--")
            target = token if is_short else token.lower()
            option_name = name if is_short else name.lower()
            if target == option_name:
                values.append(args[index + 1] if index + 1 < len(args) else "")
            elif target.startswith(option_name + "="):
                values.append(token[len(option_name) + 1:])
            elif is_short and target.startswith(option_name) and len(token) > 2:
                values.append(token[2:])
    return values


def _github_api_verdict(args: list) -> tuple:
    """Deny state-changing REST and GraphQL API requests."""
    lowered = [token.lower() for token in args]
    # gh keeps the last method option, so every value must be GET.
    methods = [value.upper() for value in _option_values(args, GH_API_METHOD_OPTIONS)]
    if any(method != "GET" for method in methods):
        return "deny", "gh api can mutate hosted GitHub resources"
    if any(token in {"-f", "-F", "--field", "--raw-field", "--input"}
           or token.startswith(("-f=", "-F=", "--field=", "--raw-field=",
                                "--input=")) for token in args):
        # Fields without an explicit GET turn the request into a POST.
        if not methods:
            return "deny", "gh api fields default to a state-changing request"
    if "graphql" in lowered and any("mutation" in token for token in lowered):
        return "deny", "GraphQL mutations can change hosted GitHub resources"
    return "", ""


def _github_auth_verdict(args: list) -> tuple:
    """Protect GitHub credentials and broad authorization scopes."""
    if any(token in ("-t", "--show-token") or token.startswith("--show-token=")
           for token in args):
        return "deny", "gh auth prints the account token into agent context"
    scopes = _option_value(args, frozenset({"--scopes", "-s"}))
    scope_set = {scope.strip().lower() for scope in scopes.split(",") if scope}
    if scope_set & GH_BROAD_AUTH_SCOPES:
        return "deny", "gh auth requests a broad write or deletion scope"
    return "", ""


def _is_github_host(host: str) -> bool:
    """Return True when `host` is github.com itself or a subdomain of it.

    A suffix test alone would accept `attacker-github.com`, so the domain
    boundary is explicit.
    """
    name = host.lower().partition(":")[0]
    return name == "github.com" or name.endswith(".github.com")


def _repository_target_owner(token: str) -> str:
    """Return the owner of a GitHub CLI repository argument.

    gh accepts `OWNER/REPO`, `HOST/OWNER/REPO`, and a full URL, so a scheme,
    any user information, and a leading GitHub host are stripped before the
    owner is read. An unrecognized shape returns an empty string, which the
    caller treats as unreadable rather than as clearance.
    """
    if not token or token.startswith("-") or is_ambiguous(token):
        return ""
    value = token.partition("://")[2] or token
    value = value.rpartition("@")[2]
    segments = [segment for segment in value.split("/") if segment]
    if len(segments) == 3 and _is_github_host(segments[0]):
        segments = segments[1:]
    if len(segments) != 2:
        return ""
    # Splitting dropped empty segments, so the owner is never empty here.
    # A separator left inside it means the token was never owner/name.
    owner = segments[0]
    if any(character in owner for character in "@:\\"):
        return ""
    return owner


def _origin_owner(cwd: str) -> str:
    """Return the GitHub owner of the origin remote, or an empty string.

    The URL is parsed with string operations because this module carries no
    regular expressions: a gate must not depend on backtracking behavior.
    """
    entries = parse_git_config(cwd)
    if not entries:
        return ""
    url = entries.get("remote.origin.url", "").strip()
    if not url:
        return ""
    url = url.partition("://")[2] or url
    url = url.rpartition("@")[2]
    # The SCP-like form is host:owner/name, so the colon can precede the
    # first slash. Split on whichever separator comes first.
    colon = url.find(":")
    slash = url.find("/")
    if slash == -1 and colon == -1:
        return ""
    if colon != -1 and (slash == -1 or colon < slash):
        host, path = url[:colon], url[colon + 1:]
        if path.split("/")[0].isdigit():
            # A port, not an owner: re-split on the slash after it.
            host, _, path = url.partition("/")
    else:
        host, path = url[:slash], url[slash + 1:]
    if not _is_github_host(host):
        return ""
    return _repository_target_owner(path.removesuffix(".git"))


# Posting to a repository outside the current owner notifies people who did
# not ask for it, so these subcommands reach an active human first.
OUTWARD_FACING_COMMANDS = frozenset({
    ("pr", "create"), ("pr", "comment"), ("pr", "review"), ("pr", "edit"),
    ("pr", "close"), ("pr", "reopen"), ("pr", "ready"),
    ("issue", "create"), ("issue", "comment"), ("issue", "edit"),
    ("issue", "close"), ("issue", "reopen"),
    ("repo", "fork"), ("release", "create"),
})


def _external_target_verdict(args: list, command: list, noun: str,
                             action: str, repo_owner: str) -> tuple:
    """Route an outward-facing command at another owner to active consent."""
    if (noun, action) not in OUTWARD_FACING_COMMANDS:
        return "", ""
    # -R and --repo are global options, so _github_command_args already
    # removed them: the target has to come from the original arguments.
    target = _option_value(args, frozenset({"--repo", "-R"}))
    if target:
        owner = _repository_target_owner(target)
        if not owner:
            # An explicit target this gate cannot read is not clearance.
            return "ask", (f"{sanitize(noun)} {sanitize(action)} names a "
                           "repository target this gate cannot read")
    else:
        owner = ""
        index = 0
        while index < len(command):
            token = command[index]
            if token in GH_SUBCOMMAND_VALUE_OPTIONS:
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            owner = _repository_target_owner(token)
            if owner:
                break
            index += 1
    if not repo_owner:
        # An unreadable origin cannot clear any target, implicit ones
        # included, so the gate asks rather than waving the command through.
        target = sanitize(owner) if owner else "an implicit repository"
        return "ask", (f"{sanitize(noun)} {sanitize(action)} targets "
                       f"{target}, and this repository names no "
                       "origin owner to compare")
    if not owner or owner.lower() == repo_owner.lower():
        return "", ""
    return "ask", (f"{sanitize(noun)} {sanitize(action)} targets "
                   f"{sanitize(owner)}, an owner outside this repository")


def _has_label_option(command: list) -> bool:
    """Return whether gh arguments carry a label option in any form."""
    for token in command:
        lowered = token.lower()
        if lowered in GH_LABEL_OPTIONS or lowered.startswith(
                tuple(option + "=" for option in GH_LABEL_OPTIONS)):
            return True
        if token.startswith(GH_LABEL_SHORT_OPTION):
            return True
    return False


def github_cli_verdict(args: list, *, repo_owner: str = "") -> tuple:
    """Return the safety verdict for one GitHub CLI invocation."""
    command = _github_command_args(args)
    if not command:
        return "", ""
    decision, reason = _github_command_denylist_verdict(command)
    if decision:
        return decision, reason
    words = _github_command_path(command)
    noun = words[0] if words else ""
    action = words[1] if len(words) > 1 else ""
    if (noun, action) in GH_LABEL_WRITE_COMMANDS and _has_label_option(command):
        return "deny", "agents never add, remove, or change labels (Rule 23)"
    if noun == "api":
        return _github_api_verdict(command[1:])
    if noun == "auth":
        return _github_auth_verdict(command)
    if noun == "repo" and action == "edit":
        visibility = _option_value(command, frozenset({"--visibility"})).lower()
        if visibility == "public" or is_ambiguous(visibility):
            return "deny", "public repository visibility can expose private content"
        return "ask", "repository edits change hosted settings"
    if noun == "gist" and action in ("create", "edit"):
        return "ask", "a gist uploads local content to GitHub"
    return _external_target_verdict(args, command, noun, action, repo_owner)


def trusted_gh_arguments(program: str, args: list, cwd: str) -> list:
    """Return GitHub CLI arguments carried by the trusted wrapper."""
    name = normalize_windows_command_name(program)
    if name not in {"py", "python", "python3"}:
        return []
    # Any run of interpreter flags may precede the script. Recognizing more
    # wrapper forms only adds verdicts, so skipping every option is safe.
    script_index = 0
    while script_index < len(args) and args[script_index].startswith("-"):
        script_index += 1
    if len(args) <= script_index + 1 or args[script_index + 1] != "run":
        return []
    script = args[script_index]
    candidate = script if os.path.isabs(script) else os.path.join(cwd, script)
    parts = os.path.abspath(candidate).replace("\\", "/").split("/")
    if [part.casefold() for part in parts[-2:]] != ["scripts", "trusted_gh.py"]:
        return []
    return args[script_index + 2:]


def _git_subcommand(args: list) -> tuple:
    """Return a Git subcommand and its remaining arguments."""
    index = 0
    while index < len(args):
        token = args[index]
        lowered = token.lower()
        if lowered in {"-c", "-C", "--git-dir", "--work-tree"}:
            index += 2
            continue
        if lowered.startswith(("-c", "--git-dir=", "--work-tree=")):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return lowered, args[index + 1:]
    return "", []


def _is_github_hostname(hostname: str) -> bool:
    """Return whether a hostname belongs to GitHub."""
    normalized = hostname.rstrip(".").casefold()
    return any(normalized == domain or normalized.endswith("." + domain)
               for domain in GITHUB_DOMAINS)


def _token_hostname(token: str) -> str:
    """Return a GitHub-relevant hostname parsed from one command token."""
    candidate = token.strip()
    if "://" in candidate or candidate.startswith("//"):
        return urllib.parse.urlsplit(candidate).hostname or ""
    colon_index = candidate.find(":")
    slash_index = candidate.find("/")
    if colon_index >= 0 and (slash_index < 0 or colon_index < slash_index):
        hostname = candidate[:colon_index].rsplit("@", 1)[-1]
        return hostname
    if "/" in candidate and not candidate.startswith("/"):
        return candidate.split("/", 1)[0]
    return ""


def _is_github_target(tokens: list) -> bool:
    """Return whether arguments name GitHub or a pull request ref."""
    for token in tokens:
        if _is_github_hostname(_token_hostname(token)):
            return True
    text = " ".join(tokens).casefold()
    return "pull/" in text


def _github_git_substitute(args: list) -> bool:
    """Return whether Git substitutes for a selected hosted GitHub operation."""
    subcommand, remaining = _git_subcommand(args)
    if subcommand == "remote":
        return True
    if subcommand == "config":
        text = " ".join(remaining).casefold()
        return "remote." in text and ".url" in text
    if subcommand in {"clone", "ls-remote"}:
        return _is_github_target(remaining)
    if subcommand in {"checkout", "diff", "fetch", "log", "show", "switch"}:
        return _is_github_target(remaining)
    return False


def github_routing_verdict(program: str, args: list, cwd: str) -> tuple:
    """Require the trusted authenticated wrapper for hosted GitHub operations."""
    wrapped = trusted_gh_arguments(program, args, cwd)
    if wrapped:
        return "", ""
    name = normalize_windows_command_name(program)
    if name in {"git-credential-manager", "git-credential-manager-core",
                "credential-manager"}:
        return "deny", "agents cannot modify Git Credential Manager"
    if (name in {"start", "start-process", "explorer", "open", "xdg-open"}
            and _is_github_target(args)):
        return "deny", "agents cannot open GitHub authentication in a browser"
    if name == "gh":
        return "deny", "direct gh lookup is untrusted; use scripts/trusted_gh.py run"
    if name == "hub":
        return "deny", "hub bypasses the trusted authenticated GitHub CLI path"
    if name == "git" and _github_git_substitute(args):
        lowered = [token.casefold() for token in args]
        if GH_FALLBACK_CONFIG in lowered:
            return "ask", "a marked one-time Git fallback follows a failed gh operation"
        return "deny", "this hosted GitHub operation must use trusted authenticated gh"
    if name in {"curl", "wget"} and _is_github_target(args):
        return "deny", "GitHub HTTP operations must use trusted authenticated gh"
    return "", ""


def forge_verdict(program: str, args: list, cwd: str = "") -> tuple:
    """Return (decision, reason) for a destructive forge command."""
    name = os.path.basename(program).lower().removesuffix(".exe")
    repository = cwd or os.getcwd()
    owner = _origin_owner(repository)
    wrapped = trusted_gh_arguments(program, args, repository)
    if wrapped:
        decision, reason = github_cli_verdict(wrapped, repo_owner=owner)
        if decision:
            return decision, reason
        name = "gh"
        args = wrapped
    if name not in FORGE_PROGRAMS:
        return "", ""
    if name == "gh" and not wrapped:
        decision, reason = github_cli_verdict(args, repo_owner=owner)
        if decision:
            return decision, reason
    command = _github_command_args(args) if name == "gh" else args
    words = [token.lower() for token in command if not token.startswith("-")]
    if len(words) < FORGE_DELETE_MIN_WORDS or "delete" not in words[:3]:
        return "", ""
    noun = words[0]
    if noun not in FORGE_DELETE_NOUNS:
        return "", ""
    return "deny", (f"{name} {sanitize(noun)} delete removes work that no "
                    "local clone holds, and no local action undoes it")
