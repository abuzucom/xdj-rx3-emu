#!/usr/bin/env python3
"""Gate destructive and history-rewriting Bash commands via a PreToolUse hook.

Not part of AGENTS.md, which stays tool-agnostic and is synced to non-Claude
tools verbatim. This is a Claude-Code-specific hook under hooks/, wired via
`.claude/settings.json` (live in this repo) or
hooks/claude-code-settings.example.json (for adopting repos). It reads the
PreToolUse JSON payload from stdin and checks only Bash tool calls.

The command is tokenized and normalized before any decision. Matching the raw
string matches spelling rather than meaning, and every destructive command has
many spellings: `rm -Rf` for `rm -rf`, `git -C dir push --force` for
`git push --force`, `--force-with-lease=main:<oid>` for `--force-with-lease`,
`git push origin +HEAD:main` for a forced push with no flag at all. A gate
that reads the string lets each of those through while appearing to work.

Two outcomes, because two different rules are in play:

`deny` covers commands with no legitimate form in this workflow: a recursive
`rm` aimed at `/`, `~`, or `$HOME`, a bare `git push --force`/`-f`, and
`git reset --hard`. It prints the reason on stderr and exits 2, so the block
holds wherever stdout JSON is ignored.

`ask` covers acts that are legitimate with the user's consent and forbidden
without it: a recursive `rm` against any other target, the
`--force-with-lease` family, `git push --mirror`, `git push --delete`, a
forced (`+`) refspec, `git commit --amend`, `git rebase`, and
`git filter-branch`. Routing these through the permission prompt puts the
decision where AGENTS.md puts it, with the human, at the act. Rule 2 carries
no scope qualifier, so a scratch directory the session created itself is
gated like any other target, and a lease does not make a history rewrite
consented to.

Ambiguity fails closed. A command that will not tokenize, or a `git` whose
subcommand is hidden behind a variable, is gated rather than waved through:
the gate cannot clear what it cannot read.

An `ask` needs a human to answer it. When `permission_mode` reports an
unattended session, or a mode this hook does not recognize, the ask becomes a
deny: absence of a human is absence of consent.

Still a heuristic, not a sandbox or authorization boundary. It does not
execute the shell, so a command hidden behind an alias, a wrapper script, or a
variable holding the program name is invisible. Repository writers can alter
this hook or its settings. Tamper resistance requires controls outside the
writable repository.
"""
import functools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import _gate_core as core
    import _bash_parser as bash_parser
    import _platform_policy as platform_policy
except ImportError as error:  # pragma: no cover
    # The adoption test exercises this import failure.
    # Fail closed. Claude Code treats any non-zero exit other than 2 as a
    # non-blocking error, so an unhandled ImportError would wave the command
    # through in exactly the repos that installed this gate.
    _REASON = (f"the shared hook parser or core could not be imported ({error}), "
               "so the gate cannot clear this command")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": _REASON,
    }}))
    print(_REASON, file=sys.stderr)
    sys.exit(2)

_CWD = [""]
GATE = "block_destructive_bash.py"
GATED_KEYWORDS = core.gated_keywords()
# A shell handed a command string is a wrapper whose payload is another
# command. Reading only the program name sees "bash" and stops there.
# Both shells are here, not only this one. A gate that knows only its own
# interpreters leaves `powershell -Command 'Remove-Item -Recurse -Force /etc'`
# as a hole, and on Windows that line runs.
INTERPRETERS = core.SHELL_INTERPRETERS
IMPLAUSIBLE_PROGRAM_CHARS = frozenset("<>&|;")


def _is_plausible_program(token: str) -> bool:
    """Return True if the token could name a command.

    A leading flag, a bare file descriptor, or a stray operator means the
    prefix strip did not reach the real program, so the caller fails closed
    rather than reporting that nothing gated was found.
    """
    if not token or token.startswith("-") or token.isdigit():
        return False
    return not (set(token) & IMPLAUSIBLE_PROGRAM_CHARS)


def _redirect_targets(tokens: list) -> list:
    """Return the files this segment redirects into.

    Both spellings matter: `> path` arrives as two tokens under
    punctuation_chars, and `>path` as one.
    """
    return bash_parser.redirect_targets(tokens)


def _nested_command_verdict(command: str, depth: int, shell: str) -> tuple:
    """Classify one nested command while enforcing the inspection limit."""
    if depth >= core.MAX_COMMAND_DEPTH:
        return "deny", f"{shell} command nesting exceeds the inspection limit"
    return classify(command, depth + 1)


def _powershell_interpreter_verdict(program: str, args: list,
                                    depth: int):
    """Return a PowerShell payload verdict, or None when none is present."""
    if program.lower() not in core.POWERSHELL_PROGRAMS:
        return None
    kind, value = core.powershell_payload(args)
    if kind == "deny":
        return "deny", value
    if kind == "command":
        return "deny", "PowerShell executes a command-string payload"
    return None


def _interpreter_argument_verdict(token: str, has_payload: bool):
    """Return a command-payload verdict for one interpreter argument."""
    if core.is_shell_payload_flag(token):
        if not has_payload:
            return "deny", "a shell command-string flag has no payload"
        return "deny", "a shell interpreter executes a command-string payload"
    return None


def _interpreter_verdict(program: str, args: list, depth: int) -> tuple:
    """Return (decision, reason) for a shell handed a command string.

    Only the payload after -c, /c, or /k is another command. A shell
    invoked without one runs a script or a REPL, which this gate cannot
    read either way, so it is not gated on the interpreter's own name.
    A shell name among the arguments (busybox sh -c) hands the rest to that
    shell. The scan follows it in place rather than recursing, so a long
    run of names cannot exhaust the stack and crash the gate open.
    """
    verdict = _powershell_interpreter_verdict(program, args, depth)
    if verdict is not None:
        return verdict
    start = 0
    for index, token in enumerate(args):
        verdict = _interpreter_argument_verdict(token, index + 1 < len(args))
        if verdict is not None:
            return verdict
        lowered = token.lower()
        if token.startswith(("-", "/")) or lowered not in INTERPRETERS:
            continue
        start = index + 1
        if lowered in core.POWERSHELL_PROGRAMS:
            verdict = _powershell_interpreter_verdict(lowered, args[start:], depth)
            if verdict is not None:
                return verdict
    args = args[start:]
    if any(argument.casefold() == "--version" for argument in args):
        return "", ""
    fixed_script = next(
        (argument for argument in args if not argument.startswith(("-", "/"))),
        "",
    )
    if fixed_script:
        return "ask", "a fixed local script executes code outside inspection"
    return "ask", "a shell invocation changes the active interpreter"


def _eval_verdict(args: list, depth: int) -> tuple:
    """Classify the command string a shell eval executes."""
    if not args:
        return "", ""
    if any(core.is_ambiguous(token) for token in args):
        return "ask", "eval command text contains an expansion the gate cannot inspect"
    return _nested_command_verdict(" ".join(args), depth, "eval")


def _segment_verdict(tokens: list, depth: int) -> tuple:
    """Return (decision, reason) for one command segment.

    The privilege verdict is folded into whatever the wrapped command
    yields, so `sudo ls` asks and `sudo rm -rf /` still denies.
    """
    privileged = core.privilege_verdict(tokens)
    redirects = _redirect_targets(tokens)
    tokens, environment, complete = bash_parser.strip_prefixes(tokens)
    if not complete:
        return "ask", "env -S command text could not be inspected"
    privileged = core.strongest(privileged, core.privilege_verdict(tokens))
    return core.strongest(
        privileged, _program_verdict(tokens, redirects, environment, depth))


def _named_program_verdict(program: str, args: list,
                           environment: list, depth: int) -> tuple:
    """Return the verdict for a program read by name, or None.

    None means the program is not one of the three the gate reads whole,
    so the caller runs it past every other check instead.
    """
    lowered = program.lower()
    prohibited = core.prohibited_command_verdict(program, args)
    if prohibited[0]:
        return prohibited
    curl_verdict = core.curl_transfer_verdict(program, args)
    if curl_verdict[0]:
        return curl_verdict
    if lowered == "eval":
        return _eval_verdict(args, depth)
    if lowered in INTERPRETERS:
        return _interpreter_verdict(program, args, depth)
    if lowered in core.DELETE_PROGRAMS:
        return core.any_delete_verdict(lowered, args)
    if core.normalize_windows_command_name(program) == "git":
        return core.git_verdict(args, _CWD[0], environment)
    return None


def _program_verdict(tokens: list, redirects: list,
                     environment: list, depth: int) -> tuple:
    """Return (decision, reason) for the program this segment runs."""
    if not tokens:
        return core.strongest(
            core.truncation_verdict("", [], redirects),
            core.test_write_verdict("", [], redirects))
    program = os.path.basename(tokens[0])
    args = tokens[1:]
    policy = (core.powershell_policy_verdict(program, args, redirects)
              if core.powershell_policy_applies_in_bash(program) else ("", ""))
    policy = core.strongest(
        core.strongest(
            policy, core.github_routing_verdict(program, args, _CWD[0])),
        core.forge_verdict(program, args, _CWD[0]),
    )
    policy = core.strongest(
        policy, core.cloudflare_pages_verdict(program, args, _CWD[0]))
    named = _named_program_verdict(program, args, environment, depth)
    if named is not None:
        return core.strongest(policy, named)
    # GitHub routing and forge verdicts merge into policy above.
    # That placement covers named programs without evaluating twice below.
    verdict = policy
    for candidate in (platform_policy.classify_platform_command(
                          sys.platform, program, args),
                      core.destruction_verdict(program, args),
                      core.alias_verdict(program, args),
                      core.environment_assignment_verdict(program, args),
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
    if verdict[0]:
        return verdict
    if core.is_ambiguous(program):
        return "ask", ("the command name contains an expansion the gate "
                       "cannot inspect")
    if not _is_plausible_program(program) and _mentions_gated_command(tokens):
        return "ask", ("the command boundaries could not be interpreted, so "
                       "the gate cannot clear it")
    return "", ""


def _mentions_gated_command(tokens: list) -> bool:
    """Return True if any token in the segment names a command this gate covers."""
    return any(os.path.basename(token) in GATED_KEYWORDS for token in tokens)


def classify(command: str, depth: int = 0) -> tuple:
    """Return the strongest (decision, reason) across the command's segments."""
    if not isinstance(command, str):
        return "ask", "the command is not a string, so the gate cannot read it"
    segments, tokens, complete = bash_parser.command_segments_and_tokens(command)
    if not complete:
        return core.unparseable_verdict(command, GATED_KEYWORDS)
    verdict = core.pipeline_execution_verdict(
        segments, tokens, bash_parser._is_separator, core.BASH_LIST_OPERATORS)
    for segment in segments:
        verdict = core.strongest(verdict, _segment_verdict(segment, depth))
    return verdict


def emit(decision: str, reason: str) -> int:
    """Print the gate's decision and return the exit code it needs."""
    return core.emit(GATE, decision, reason)


def main() -> int:
    """Classify one Bash tool call, denying on any unexpected error."""
    return core.run_fail_closed(_run, functools.partial(emit, "deny"))


def _run() -> int:
    payload = core.read_payload()
    if payload is None:
        return emit("deny", "the hook payload could not be parsed, so the gate cannot clear this command")
    if payload.get("tool_name") != "Bash":
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return emit("deny", "the tool input is malformed, so the gate cannot read this command")
    _CWD[0] = core.project_dir(payload)
    command = core.require_str(tool_input.get("command", ""))
    if command is None:
        return emit("deny", "the command field is not a string, so the gate cannot read it")
    decision, reason = classify(command)
    if not decision:
        return 0
    return core.decide(GATE, payload, decision, reason)


if __name__ == "__main__":
    sys.exit(main())
