#!/usr/bin/env python3
"""Parse Bash command boundaries and executable prefixes for hook classifiers."""
import os
import shlex

OPERATOR_CHARS = frozenset("&|;")
# A brace group and a backtick substitution both put a command where a
# program name goes. Reading "{" or a backtick as the program name sees
# no delete at all, so each ends a segment the way a parenthesis does.
GROUPING = frozenset({"(", ")", "{", "}", "`", "\n"})
# shlex's default punctuation set plus the backtick, so `cmd` splits
# into its own tokens instead of arriving glued to the words beside it.
PUNCTUATION_CHARS = "();<>|&`"
MAX_COMMAND_CHARACTERS = 65536
REDIRECTION_CHARS = frozenset("<>&0123456789")
PROCESS_SUBSTITUTION_CHARS = frozenset("<>")
WRAPPERS = frozenset({
    "sudo", "doas", "env", "time", "nohup", "nice", "command", "xargs",
    "timeout", "exec", "builtin",
})
WRAPPER_VALUE_OPTIONS = {
    "sudo": frozenset({
        "-u", "-g", "-p", "-C", "-h", "-U", "-r", "-t", "--user",
        "--group", "--prompt", "--close-from", "--host", "--role", "--type",
    }),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "xargs": frozenset({
        "-n", "-I", "-L", "-P", "-s", "-d", "-E", "-a", "--max-args",
        "--replace", "--max-procs", "--delimiter", "--arg-file",
    }),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-k", "-s", "--kill-after", "--signal"}),
    "exec": frozenset({"-a"}),
}


def is_env_assignment(token: str) -> bool:
    """Return True if `token` is a leading shell environment assignment."""
    if token.startswith("-") or "=" not in token:
        return False
    return token.split("=", 1)[0].isidentifier()


def is_redirection(token: str) -> bool:
    """Return True if `token` is a redirection operator."""
    return bool(token) and ("<" in token or ">" in token) and set(token) <= REDIRECTION_CHARS


def redirect_targets(tokens: list) -> list:
    """Return the files a segment redirects into."""
    targets = []
    for index, token in enumerate(tokens):
        if is_redirection(token) and index + 1 < len(tokens):
            targets.append(tokens[index + 1])
        elif ">" in token and not is_redirection(token):
            tail = token.partition(">")[2]
            if tail:
                targets.append(tail)
    return targets


def _env_split_value(token: str) -> tuple:
    """Return an env -S value and whether `token` carries one."""
    if token in ("-S", "--split-string"):
        return "", True
    for prefix in ("-S", "--split-string="):
        if token.startswith(prefix) and len(token) > len(prefix):
            return token[len(prefix):], True
    return "", False


def _expand_env_split(tokens: list, index: int):
    """Return tokens expanded through env -S, or None when not applicable."""
    value, applies = _env_split_value(tokens[index])
    if not applies:
        return None, True
    next_index = index + 1
    if not value:
        if next_index >= len(tokens):
            return [], False
        value = tokens[next_index]
        next_index += 1
    expanded, complete = _tokenize_line(value)
    if not complete or not expanded:
        return [], False
    return expanded + tokens[next_index:], True


def strip_prefixes(tokens: list) -> tuple:
    """Return executable tokens, assignments, and complete prefix parsing."""
    index = 0
    wrapper = ""
    wrapper_index = -1
    assignments = []
    while index < len(tokens):
        token = tokens[index]
        if is_redirection(token):
            index += 2
            continue
        if is_env_assignment(token):
            assignments.append(token.split("=", 1))
            index += 1
            continue
        name = os.path.basename(token).lower()
        if name in WRAPPERS:
            wrapper = name
            wrapper_index = index
            index += 1
            continue
        expanded, complete = _expand_env_split(tokens, index) if wrapper == "env" else (None, True)
        if expanded is not None:
            if not complete:
                return [], assignments, False
            tokens = expanded
            index = 0
            wrapper = ""
            wrapper_index = -1
            continue
        if wrapper and token.startswith("-"):
            takes_value = ("=" not in token
                           and token in WRAPPER_VALUE_OPTIONS.get(wrapper, frozenset()))
            index += 2 if takes_value else 1
            continue
        break
    if index >= len(tokens) and wrapper_index >= 0:
        # A wrapper with no program runs itself: a bare `env` prints the
        # whole environment.
        return [tokens[wrapper_index]], assignments, True
    return tokens[index:], assignments, True


def is_process_substitution(token: str) -> bool:
    """Return True for a fused `<(` or `>(` token that opens a command.

    shlex fuses the redirection character with the parenthesis. Read as a
    word, the token hides the substituted command inside its neighbor's
    arguments.
    """
    head = token[:-1]
    return token.endswith("(") and bool(head) and set(head) <= PROCESS_SUBSTITUTION_CHARS


def _is_separator(token: str) -> bool:
    """Return True if `token` separates commands."""
    return (token in GROUPING
            or (bool(token) and set(token) <= OPERATOR_CHARS)
            or is_process_substitution(token))


def _split_plain_segments(tokens: list) -> list:
    """Split a token stream at command separators."""
    segments = [[]]
    for token in tokens:
        if _is_separator(token):
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _collapse_backticks(tokens: list) -> list:
    """Return the enclosing command with substitutions kept as markers."""
    outer = []
    nested = []
    inside = False
    for token in tokens:
        if token != "`":
            (nested if inside else outer).append(token)
            continue
        if inside:
            outer.append("`" + " ".join(nested) + "`")
            nested = []
        inside = not inside
    if inside:
        outer.append("`" + " ".join(nested))
    return outer


def _split_segments(tokens: list) -> list:
    """Split commands while preserving the context around backticks."""
    segments = _split_plain_segments(tokens)
    if "`" not in tokens:
        return segments
    seen = {tuple(segment) for segment in segments}
    for segment in _split_plain_segments(_collapse_backticks(tokens)):
        key = tuple(segment)
        if key not in seen:
            segments.append(segment)
            seen.add(key)
    return segments


def _tokenize_line(line: str) -> tuple:
    """Return tokens and whether the whole line parsed."""
    lexer = shlex.shlex(line, posix=True,
                        punctuation_chars=PUNCTUATION_CHARS)
    lexer.whitespace_split = True
    tokens = []
    try:
        for token in lexer:
            tokens.append(token)
    except ValueError:
        return tokens, False
    return tokens, True


def command_segments_and_tokens(command: str) -> tuple:
    """Return segments, the token stream, and whether every line parsed.

    The stream joins lines with a newline token, so a caller can walk the
    whole command without tokenizing it a second time.
    """
    if not isinstance(command, str) or len(command) > MAX_COMMAND_CHARACTERS:
        return [], [], False
    segments = []
    stream = []
    complete = True
    for line in command.splitlines():
        tokens, parsed = _tokenize_line(line)
        segments.extend(_split_segments(tokens))
        stream.extend(tokens)
        stream.append("\n")
        complete = complete and parsed
    return segments, stream, complete


def command_segments(command: str) -> tuple:
    """Return parsed segments and whether every line parsed completely."""
    segments, _stream, complete = command_segments_and_tokens(command)
    return segments, complete


def has_quoted_redirects(command: str) -> bool:
    """Detect operator data that tokenization cannot distinguish from redirects."""
    quote = ""
    escaped = False
    for character in command:
        if escaped:
            if character in "<>":
                return True
            escaped = False
        elif character == "\\" and quote != "'":
            escaped = True
        elif quote:
            if character == quote:
                quote = ""
            elif character in "<>":
                return True
        elif character in "\"'":
            quote = character
    return False


def _ambiguous_context(label: str, error: str, cwd: str) -> dict:
    """Return the context a caller blocks on when a command cannot be read."""
    return {
        "label": label,
        "error": error,
        "cwd": os.path.realpath(cwd or "."),
        "settings": [],
    }


def git_write_operation(command: str, resolve_git, cwd: str = "") -> list:
    """Return every effective or ambiguous Git write in `command`."""
    if not isinstance(command, str):
        return []
    contexts = []
    recovered_git = False
    segments, parsed = command_segments(command)
    for segment in segments:
        executable, assignments, complete = strip_prefixes(segment)
        if not complete:
            contexts.append(_ambiguous_context(
                "unresolved env -S command",
                "env -S command text could not be inspected", cwd))
            continue
        if not executable:
            continue
        program = os.path.basename(executable[0]).lower().removesuffix(".exe")
        if program != "git":
            continue
        recovered_git = True
        context = resolve_git(executable[1:], cwd, assignments)
        if context:
            contexts.append(context)
    if not parsed and recovered_git:
        # Only recovered Git syntax supplies evidence of an ambiguous Git write.
        contexts.append(_ambiguous_context(
            "unparseable command",
            "the command could not be parsed, so a git write it names "
            "could not be read", cwd))
    return contexts
