#!/usr/bin/env python3
"""Parse CMD command boundaries without executing command text."""
from dataclasses import dataclass


MAX_COMMAND_CHARACTERS = 65536
COMMAND_SEPARATORS = frozenset({"&", "|", "(", ")", "\n", "\r"})
OUTPUT_REDIRECT = ">"


@dataclass(frozen=True)
class CmdParseResult:
    """Store one exclusive parser state and its complete command segments."""

    status: str
    segments: tuple[tuple[str, ...], ...] = ()


def contains_dynamic_expansion(command_text: str) -> bool:
    """Return whether CMD expansion can change command structure."""
    escaped = False
    inside_quotes = False
    percent_open = False
    exclamation_open = False
    for current_character in command_text:
        if escaped:
            escaped = False
            continue
        if current_character == '"':
            inside_quotes = not inside_quotes
            continue
        if current_character == "^" and not inside_quotes:
            escaped = True
            continue
        if current_character == "%":
            if percent_open:
                return True
            percent_open = True
            continue
        if current_character == "!":
            if exclamation_open:
                return True
            exclamation_open = True
    return percent_open or exclamation_open


def append_token(
    token_characters: list[str],
    command_tokens: list[str],
) -> None:
    """Append one completed token and clear its character buffer."""
    if not token_characters:
        return
    command_tokens.append("".join(token_characters))
    token_characters.clear()


def append_segment(
    command_tokens: list[str],
    command_segments: list[tuple[str, ...]],
) -> None:
    """Append one nonempty command segment and clear its token buffer."""
    if not command_tokens:
        return
    command_segments.append(tuple(command_tokens))
    command_tokens.clear()


def split_output_redirects(
    command_tokens: tuple[str, ...],
) -> tuple[tuple[str, ...], list[str], bool]:
    """Return executable tokens, redirect targets, and syntax completeness."""
    executable_tokens: list[str] = []
    redirect_targets: list[str] = []
    token_index = 0
    while token_index < len(command_tokens):
        token = command_tokens[token_index]
        if token != OUTPUT_REDIRECT:
            executable_tokens.append(token)
            token_index += 1
            continue
        if executable_tokens and executable_tokens[-1].isdigit():
            executable_tokens.pop()
        token_index += 1
        while (token_index < len(command_tokens)
               and command_tokens[token_index] == OUTPUT_REDIRECT):
            token_index += 1
        if token_index >= len(command_tokens):
            return tuple(executable_tokens), redirect_targets, False
        target = command_tokens[token_index]
        if not target.startswith("&"):
            redirect_targets.append(target)
        token_index += 1
    return tuple(executable_tokens), redirect_targets, True


def parse_cmd_command(command_text: str) -> CmdParseResult:
    """Return bounded CMD segments or one fail-closed parser state."""
    if not isinstance(command_text, str):
        return CmdParseResult("malformed")
    if len(command_text) > MAX_COMMAND_CHARACTERS:
        return CmdParseResult("input_too_large")
    if not command_text.strip():
        return CmdParseResult("empty")
    if contains_dynamic_expansion(command_text):
        return CmdParseResult("dynamic")
    return scan_cmd_characters(command_text)


def scan_cmd_characters(command_text: str) -> CmdParseResult:
    """Scan CMD quoting, escaping, tokens, and command separators once."""
    command_segments: list[tuple[str, ...]] = []
    command_tokens: list[str] = []
    token_characters: list[str] = []
    inside_quotes = False
    escaped = False
    previous_character = ""
    for current_character in command_text:
        if escaped:
            if current_character == OUTPUT_REDIRECT:
                token_characters.append("^")
            token_characters.append(current_character)
            escaped = False
            previous_character = current_character
            continue
        if current_character == '"':
            inside_quotes = not inside_quotes
            previous_character = current_character
            continue
        if current_character == "^" and not inside_quotes:
            escaped = True
            previous_character = current_character
            continue
        if not inside_quotes and current_character == OUTPUT_REDIRECT:
            append_token(token_characters, command_tokens)
            command_tokens.append(OUTPUT_REDIRECT)
            previous_character = current_character
            continue
        if (not inside_quotes and current_character == "&"
                and previous_character == OUTPUT_REDIRECT):
            token_characters.append(current_character)
            previous_character = current_character
            continue
        if not inside_quotes and current_character in COMMAND_SEPARATORS:
            append_token(token_characters, command_tokens)
            append_segment(command_tokens, command_segments)
            previous_character = current_character
            continue
        if not inside_quotes and current_character.isspace():
            append_token(token_characters, command_tokens)
            previous_character = current_character
            continue
        token_characters.append(current_character)
        previous_character = current_character
    if escaped or inside_quotes:
        return CmdParseResult("malformed")
    append_token(token_characters, command_tokens)
    append_segment(command_tokens, command_segments)
    if not command_segments:
        return CmdParseResult("empty")
    return CmdParseResult("complete", tuple(command_segments))
