#!/usr/bin/env python3
"""Gate CMD commands through behavior-based operation policies."""
import json
import ntpath
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import _cmd_parser as cmd_parser
    import _gate_core as core
except ImportError as error:  # pragma: no cover
    failure_reason = f"shared CMD parser or core import failed ({error})"
    print(failure_reason, file=sys.stderr)
    sys.exit(2)


GATE = "block_destructive_cmd.py"
CMD_TOOL_NAMES = frozenset({"Cmd", "CMD", "CommandPrompt"})
STORAGE_DESTRUCTION_PROGRAMS = frozenset({"diskpart", "format"})
SENSITIVE_DISCOVERY_PROGRAMS = frozenset({
    "arp",
    "driverquery",
    "getmac",
    "ipconfig",
    "netstat",
    "route",
    "systeminfo",
    "tasklist",
    "whoami",
})
REMOTE_EXECUTION_PROGRAMS = frozenset({"psexec", "winrs"})
SCRIPT_SUFFIXES = (".bat", ".cmd", ".ps1")
INTERPRETER_PROGRAMS = frozenset({
    "bash",
    "cmd",
    "csh",
    "dash",
    "fish",
    "ksh",
    "powershell",
    "pwsh",
    "sh",
    "tcsh",
    "zsh",
})
_CWD = [""]


def normalize_cmd_program(program_token: str) -> str:
    """Return a case-insensitive CMD executable name."""
    program_name = ntpath.basename(program_token)
    return core.normalize_windows_command_name(program_name)


def classify_interpreter(
    program_name: str,
    arguments: tuple[str, ...],
) -> tuple[str, str]:
    """Classify shell transitions, command strings, and fixed scripts."""
    if program_name in ("powershell", "pwsh"):
        powershell_verdict = core.powershell_policy_verdict(
            program_name,
            list(arguments),
        )
        if powershell_verdict[0]:
            return powershell_verdict
    if any(core.is_shell_payload_flag(argument) for argument in arguments):
        return "deny", f"{program_name} executes a command-string payload"
    return "ask", f"{program_name} changes the active command interpreter"


def classify_service_or_task(
    program_name: str,
    arguments: tuple[str, ...],
) -> tuple[str, str]:
    """Classify service and scheduled-task persistence operations."""
    lowered_arguments = tuple(argument.casefold() for argument in arguments)
    if program_name == "sc":
        mutating_operations = {"config", "create", "delete", "failure", "start"}
        if lowered_arguments and lowered_arguments[0] in mutating_operations:
            return "deny", "sc changes persistent service execution"
        return "ask", "sc reads service configuration"
    mutating_flags = {"/change", "/create", "/delete", "/end", "/run"}
    if any(argument in mutating_flags for argument in lowered_arguments):
        return "deny", "schtasks changes persistence or starts scheduled code"
    return "ask", "schtasks reads scheduled-task state"


def classify_named_program(
    program_token: str,
    program_name: str,
    arguments: tuple[str, ...],
) -> tuple[str, str]:
    """Return the direct policy verdict for one normalized CMD program."""
    prohibited = core.prohibited_command_verdict(program_name, list(arguments))
    if prohibited[0]:
        return prohibited
    if program_name in STORAGE_DESTRUCTION_PROGRAMS:
        return "deny", f"{program_name} partitions or formats storage"
    if program_name in core.DELETE_PROGRAMS:
        return core.any_delete_verdict(program_name, list(arguments))
    if program_name in ("sc", "schtasks"):
        return classify_service_or_task(program_name, arguments)
    if program_name in REMOTE_EXECUTION_PROGRAMS:
        return "deny", f"{program_name} enables remote command execution"
    if program_name in SENSITIVE_DISCOVERY_PROGRAMS:
        return "ask", f"{program_name} enumerates sensitive host state"
    if program_name == "curl":
        return core.curl_transfer_verdict(program_name, list(arguments))
    if program_name in INTERPRETER_PROGRAMS:
        return classify_interpreter(program_name, arguments)
    if program_name in ("call", "for"):
        return "deny", f"{program_name} can hide nested command execution"
    if program_token.casefold().endswith(SCRIPT_SUFFIXES):
        return "ask", "a fixed local batch script executes code"
    return "", ""


def classify_cmd_segment(command_tokens: tuple[str, ...]) -> tuple[str, str]:
    """Return the strongest policy verdict for one CMD command segment."""
    command_tokens, redirects, redirects_complete = (
        cmd_parser.split_output_redirects(command_tokens)
    )
    if not redirects_complete:
        return "deny", "CMD output redirect target is missing"
    if not command_tokens:
        return core.strongest(
            core.truncation_verdict("", [], redirects),
            core.test_write_verdict("", [], redirects),
        )
    program_name = normalize_cmd_program(command_tokens[0])
    arguments = command_tokens[1:]
    verdict = core.privilege_verdict(list(command_tokens))
    verdict = core.strongest(
        verdict,
        classify_named_program(command_tokens[0], program_name, arguments),
    )
    if program_name == "git":
        verdict = core.strongest(
            verdict,
            core.git_verdict(list(arguments), _CWD[0]),
        )
    policies = (
        core.cloudflare_pages_verdict(
            program_name, list(arguments), _CWD[0]),
        core.destruction_verdict(program_name, list(arguments)),
        core.alias_verdict(program_name, list(arguments)),
        core.mode_change_verdict(program_name, list(arguments)),
        core.truncation_verdict(program_name, list(arguments), redirects),
        core.process_verdict(program_name, list(arguments)),
        core.schedule_verdict(program_name, list(arguments)),
        core.forge_verdict(program_name, list(arguments), _CWD[0]),
        core.filesystem_repair_verdict(program_name, list(arguments)),
        core.infrastructure_path_verdict(
            program_name, list(arguments), redirects, _CWD[0]),
        core.github_routing_verdict(
            program_name, list(arguments), _CWD[0]),
        core.protected_write_verdict(
            program_name, list(arguments), redirects, _CWD[0]),
        core.test_write_verdict(program_name, list(arguments), redirects),
    )
    for policy in policies:
        verdict = core.strongest(verdict, policy)
    return verdict


def classify_cmd_command(command_text: str) -> tuple[str, str]:
    """Return the strongest verdict across one parsed CMD command."""
    parse_result = cmd_parser.parse_cmd_command(command_text)
    if parse_result.status == "empty":
        return "", ""
    if parse_result.status != "complete":
        return "deny", f"CMD command parsing stopped at {parse_result.status} input"
    strongest_verdict = ("", "")
    for command_segment in parse_result.segments:
        segment_verdict = classify_cmd_segment(command_segment)
        strongest_verdict = core.strongest(strongest_verdict, segment_verdict)
        if strongest_verdict[0] == "deny":
            return strongest_verdict
    return strongest_verdict


def main() -> int:
    """Read one hook payload and emit its CMD policy decision."""
    payload = core.read_payload()
    if payload is None:
        return core.emit(
            GATE,
            "deny",
            "the hook payload cannot be read, so the gate cannot clear it",
        )
    if payload.get("tool_name") not in CMD_TOOL_NAMES:
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return core.emit(GATE, "deny", "the CMD tool input is malformed")
    command_text = core.require_str(tool_input.get("command", ""))
    if command_text is None:
        return core.emit(GATE, "deny", "the CMD command field is not a string")
    _CWD[0] = core.project_dir(payload)
    decision, decision_reason = classify_cmd_command(command_text)
    if not decision:
        return 0
    return core.decide(GATE, payload, decision, decision_reason)


if __name__ == "__main__":
    sys.exit(main())
