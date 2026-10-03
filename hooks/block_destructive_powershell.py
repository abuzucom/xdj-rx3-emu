#!/usr/bin/env python3
"""Gate destructive PowerShell commands via a PreToolUse hook.

Not part of AGENTS.md, which stays tool-agnostic and is synced to non-Claude
tools verbatim. This is a Claude-Code-specific hook under hooks/, wired via
`.claude/settings.json` (live in this repo) or
hooks/claude-code-settings.example.json (for adopting repos). It reads the
PreToolUse JSON payload from stdin and checks only PowerShell tool calls.

Every decision comes from hooks/_gate_core.py, which
hooks/block_destructive_bash.py also imports. Only the parsing is
shell-specific: statement boundaries, the call operator and the cmdlets that
run another command, PowerShell's redirection forms, and `Remove-Item` with
its aliases and prefix-abbreviated parameters. A verdict reached here is the
verdict the Bash gate reaches for the equivalent command, by construction
rather than by promise, and tests/test_gate_parity.py asserts it.

Verified against synthetic payloads only. It has not been exercised against a
live PowerShell tool call.

PowerShell parameters may be abbreviated to any unambiguous prefix, so
`-Recurse`, `-Rec`, and `-r` are the same switch, and matching is
case-insensitive. Matching the full spelling alone would read
`Remove-Item -r` as non-recursive.

Still a heuristic, not a sandbox or authorization boundary. A command behind a
function, a module, or a variable holding a cmdlet name is invisible.
Repository writers can alter this hook or its settings. Tamper resistance
requires controls outside the writable repository.
"""
import functools
import json
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_CWD = [""]
GATE = "block_destructive_powershell.py"

try:
    import _gate_core as core
except ImportError as error:  # pragma: no cover
    # The adoption test exercises this import failure.
    # Fail closed. Claude Code treats any non-zero exit other than 2 as a
    # non-blocking error, so an unhandled ImportError would wave the command
    # through in exactly the repos that installed this gate.
    _REASON = (f"hooks/_gate_core.py could not be imported ({error}), so the "
               "gate cannot clear this command")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": _REASON,
    }}))
    print(_REASON, file=sys.stderr)
    sys.exit(2)

GATED_KEYWORDS = core.gated_keywords() + tuple(core.DELETE_PROGRAMS)
# A subexpression puts a command where an argument goes, the mirror of
# the Bash gate's brace group. Reading Write-Output as the program sees
# no delete at all, so the parentheses end a statement.
SEPARATORS = frozenset({";", "|", "&&", "||", "(", ")", "\n"})
OPERATOR_CHARACTERS = frozenset("&|;")
# The call operator and the cmdlets that run a command given to them.
WRAPPERS = frozenset({"&", "start-process", "invoke-expression", "iex",
                      "invoke-command", "sudo"})
# Both shells are here, not only this one. A gate that knows only its own
# interpreters leaves `bash -c 'rm -rf /etc'` as a hole, and on a Windows box
# carrying Git Bash that line runs.
INTERPRETERS = core.SHELL_INTERPRETERS
REDIRECTIONS = frozenset({">", ">>", "2>", "2>>", "*>", "*>>", "3>", "4>",
                          "5>", "6>"})
# Start-Process hands its target a plain argument list rather than a command
# string, so the payload has to be read as arguments to that program.
ARGUMENT_LIST_FLAGS = frozenset(
    [f"-{'argumentlist'[:n]}" for n in range(1, 13)] + ["-args"])
FILE_PATH_FLAGS = frozenset(f"-{'filepath'[:n]}" for n in range(2, 9))
SCRIPT_BLOCK_DELIMITERS = "{}"
MAX_WRAPPER_DEPTH = core.MAX_COMMAND_DEPTH


def _tokenize(command: str):
    """Return the command's tokens, or None when it cannot be parsed."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # PowerShell escapes with a backtick. A backslash is a path separator,
    # and treating it as an escape turns C:\work\build into C:workbuild,
    # which matches no root and no test path.
    lexer.escape = "`"
    try:
        return list(lexer)
    except ValueError:
        return None


def _is_separator(token: str) -> bool:
    """Return True if `token` ends a PowerShell statement or pipeline stage."""
    return token in SEPARATORS or (bool(token) and set(token) <= OPERATOR_CHARACTERS)


def _segments(tokens: list) -> list:
    """Split tokens into the statements PowerShell would run separately."""
    segments = [[]]
    for token in tokens:
        if _is_separator(token):
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _redirect_targets(tokens: list) -> list:
    """Return the files this statement redirects into."""
    targets = []
    for index, token in enumerate(tokens):
        if token in REDIRECTIONS and index + 1 < len(tokens):
            targets.append(tokens[index + 1])
    return targets


def _argument_list_verdict(program: str, rest: list, depth: int) -> tuple:
    """Return the verdict for a payload handed to `program` as arguments.

    -ArgumentList carries arguments for `program`, not a command line of
    its own, so `/c rd /s /q C:\\` has to be read as cmd's arguments.
    Reading it as a statement finds a program named /c and clears it.
    """
    if not rest:
        return "", ""
    nested = _tokenize(rest[0])
    if nested is None:
        return core.unparseable_verdict(rest[0].lower(), GATED_KEYWORDS)
    if depth >= core.MAX_COMMAND_DEPTH:
        return "deny", "shell command nesting exceeds the inspection limit"
    return _interpreter_verdict(program, nested, depth + 1)


def _payload_verdict(flag: str, rest: list, depth: int) -> tuple:
    """Return the verdict for a command string handed to an interpreter."""
    if not rest:
        return "deny", "a shell command-string flag has no payload"
    return "deny", "a shell interpreter executes a command-string payload"


def _interpreter_verdict(program: str, args: list, depth: int = 0) -> tuple:
    """Return (decision, reason) for an interpreter handed a command.

    `depth` bounds the re-entry below, which follows one interpreter into
    the arguments another handed it. A hostile command can nest launches
    without limit; the gate reads a fixed number and then denies.
    """
    if depth >= MAX_WRAPPER_DEPTH:
        return "deny", "shell command nesting exceeds the inspection limit"
    if program.lower() in core.POWERSHELL_PROGRAMS:
        kind, value = core.powershell_payload(args)
        if kind == "deny":
            return "deny", value
        if kind == "command":
            return "deny", "PowerShell executes a command-string payload"
    for index, token in enumerate(args):
        lowered = token.lower()
        if lowered in ARGUMENT_LIST_FLAGS:
            return _argument_list_verdict(program, args[index + 1:], depth)
        if core.is_shell_payload_flag(token):
            return _payload_verdict(lowered, args[index + 1:], depth)
    return "", ""


def _strip_wrappers(tokens: list) -> list:
    """Drop the call operator, the cmdlets that run a command, and braces.

    `& { Remove-Item -Recurse -Force /etc }` puts a script block where a
    program name goes. Reading `{` as the program sees no delete at all,
    so the braces come off with the wrapper that introduced them.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index].strip(SCRIPT_BLOCK_DELIMITERS)
        name = os.path.basename(token).lower()
        if not token or name in WRAPPERS or name in FILE_PATH_FLAGS:
            index += 1
            continue
        break
    return [token.strip(SCRIPT_BLOCK_DELIMITERS) for token in tokens[index:]
            if token.strip(SCRIPT_BLOCK_DELIMITERS)]


def _statement_verdict(tokens: list, depth: int) -> tuple:
    """Return (decision, reason) for one PowerShell statement."""
    privileged = core.privilege_verdict(tokens)
    environment = _environment_assignment_verdict(tokens)
    redirects = _redirect_targets(tokens)
    return core.strongest(
        environment,
        core.strongest(privileged, _program_verdict(tokens, redirects, depth)))


def _environment_assignment_verdict(tokens: list) -> tuple:
    """Gate persistent assignments that can make a later Git read execute."""
    joined = "".join(tokens)
    name = ""
    if "=" in joined:
        name = _environment_reference_name(joined.split("=", 1)[0])
    program = os.path.basename(tokens[0]).lower() if tokens else ""
    if not name and program in ("set-item", "si", "set-variable", "sv"):
        for token in tokens[1:]:
            name = _environment_reference_name(token)
            if name:
                break
    if not core.is_relevant_git_environment(name):
        return "", ""
    return "ask", (f"assigning $env:{core.sanitize(name)} changes how a later "
                   "git read resolves or executes programs")


def _environment_reference_name(value: str) -> str:
    """Return an environment provider variable name from PowerShell syntax."""
    candidate = value.strip().strip('"').strip("'")
    lowered = candidate.lower()
    if lowered.startswith("${env:") and "}" in candidate:
        return candidate[6:candidate.index("}")].lstrip("\\/").upper()
    if lowered.startswith("$env:"):
        return candidate[5:].lstrip("\\/").upper()
    if lowered.startswith("env:"):
        return candidate[4:].lstrip("\\/").upper()
    return ""


def _named_program_verdict(program: str, args: list, depth: int) -> tuple:
    """Return the verdict for a cmdlet read by name, or None.

    None means the name is not one of the three the gate reads whole, so
    the caller runs it past every other check instead.
    """
    prohibited = core.prohibited_command_verdict(program, args)
    if prohibited[0]:
        return prohibited
    curl_verdict = core.curl_transfer_verdict(program, args)
    if curl_verdict[0]:
        return curl_verdict
    if program in INTERPRETERS:
        return _interpreter_verdict(program, args, depth)
    if program in core.DELETE_PROGRAMS:
        return core.any_delete_verdict(program, args)
    if core.normalize_windows_command_name(program) == "git":
        return core.git_verdict(args, _CWD[0])
    return None


def _program_verdict(tokens: list, redirects: list, depth: int) -> tuple:
    """Return (decision, reason) for the cmdlet this statement runs."""
    policy = (core.powershell_policy_verdict(tokens[0], tokens[1:], redirects)
              if tokens else ("", ""))
    stripped = _strip_wrappers(tokens)
    if len(stripped) < len(tokens) and len(stripped) == 1 and " " in stripped[0]:
        # Invoke-Expression and the call operator take a command as one
        # string. Reading it as a program name sees the whole statement.
        if depth >= core.MAX_COMMAND_DEPTH:
            return "deny", "PowerShell command nesting exceeds the inspection limit"
        return core.strongest(policy, classify(stripped[0], depth + 1))
    tokens = [token for token in stripped if token not in REDIRECTIONS]
    if not tokens:
        return core.strongest(
            policy, core.test_write_verdict("", [], redirects))
    program = os.path.basename(tokens[0]).lower()
    args = tokens[1:]
    policy = core.strongest(
        core.strongest(
            policy, core.github_routing_verdict(program, args, _CWD[0])),
        core.forge_verdict(program, args, _CWD[0]),
    )
    policy = core.strongest(
        policy, core.cloudflare_pages_verdict(program, args, _CWD[0]))
    named = _named_program_verdict(program, args, depth)
    if named is not None:
        return core.strongest(policy, named)
    verdict = policy
    for candidate in (core.destruction_verdict(program, args),
                      core.alias_verdict(program, args),
                      core.mode_change_verdict(program, args),
                      core.truncation_verdict(program, args, redirects),
                      core.process_verdict(program, args),
                      core.schedule_verdict(program, args),
                      core.filesystem_repair_verdict(program, args),
                      core.infrastructure_path_verdict(
                          program, args, redirects, _CWD[0]),
                      core.profile_verdict(program, args, redirects),
                      core.protected_write_verdict(
                          program, args, redirects, _CWD[0]),
                      core.cmd_delete_verdict(program, args),
                      core.test_write_verdict(program, args, redirects)):
        verdict = core.strongest(verdict, candidate)
    return verdict


def classify(command, depth: int = 0) -> tuple:
    """Return the strongest (decision, reason) across the command's statements."""
    if not isinstance(command, str):
        return "ask", "the command is not a string, so the gate cannot read it"
    verdict = ("", "")
    for line in command.splitlines():
        tokens = _tokenize(line)
        if tokens is None:
            return core.unparseable_verdict(command.lower(), GATED_KEYWORDS)
        segments = _segments(tokens)
        verdict = core.strongest(verdict, core.pipeline_execution_verdict(
            segments, tokens, _is_separator, core.POWERSHELL_LIST_OPERATORS))
        for segment in segments:
            verdict = core.strongest(
                verdict, _statement_verdict(segment, depth))
    return verdict


def main() -> int:
    """Classify one PowerShell tool call, denying on any unexpected error."""
    return core.run_fail_closed(_run, functools.partial(core.emit, GATE, "deny"))


def _run() -> int:
    payload = core.read_payload()
    if payload is None:
        return core.emit(GATE, "deny", "the hook payload could not be parsed, "
                                       "so the gate cannot clear this command")
    if payload.get("tool_name") != "PowerShell":
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return core.emit(GATE, "deny", "the tool input is malformed, so the "
                                       "gate cannot read this command")
    _CWD[0] = core.project_dir(payload)
    command = core.require_str(tool_input.get("command", ""))
    if command is None:
        return core.emit(GATE, "deny", "the command field is not a string, so "
                                       "the gate cannot read it")
    decision, reason = classify(command)
    if not decision:
        return 0
    return core.decide(GATE, payload, decision, reason)


if __name__ == "__main__":
    sys.exit(main())
