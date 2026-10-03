#!/usr/bin/env python3
"""Check tracked files for unresolved git merge conflict markers.

A portable, path-generic checker: copy this file into any repo and run
it in CI, pre-commit hooks, or Makefile targets. Scans files for unresolved
git merge conflict blocks and orphan markers across standard and configured
marker sizes. Inspects Git index modes to ignore symlinks and submodules,
supports UTF-16/32 encodings and BOMs, enforces read bounds, and allows
Markdown Setext headings only under valid heading context.

Modes:
  check_conflict_markers.py <file> ...   Check explicitly provided files
  check_conflict_markers.py --all        Check all git-tracked regular files
  check_conflict_markers.py --staged     Check staged index blobs (pre-commit)
  check_conflict_markers.py --repo PATH --tree OID
                                         Check one immutable commit or tree
  check_conflict_markers.py              Default: --all

Exits 1 on any violation or read/git error (fail-closed), 0 if clean.
"""
import errno
import functools
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_LINE_LENGTH = 65536  # 64 KB
MAX_VIOLATIONS = 100
# git check-attr reads paths from stdin; chunk them so one
# invocation stays a reasonable size on a large repository.
CHECK_ATTR_CHUNK = 500
MAX_DIAGNOSTIC_LENGTH = 200
REGULAR_MODES = {"100644", "100755"}
OBJECT_ID_PATTERN = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")


def _sanitize(value: object) -> str:
    """Render untrusted text as printable ASCII on one line.

    An allowlist, not a strip. Zero-width characters, bidi overrides, and
    Unicode tag characters are not control characters, and they render
    invisibly or reverse the text around them, so a denylist of known-bad
    codepoints misses the cases that matter. Paths and file content reach
    CI logs and terminals, where a newline forges a log line and an escape
    sequence rewrites the screen.
    """
    rendered = []
    for character in str(value):
        if " " <= character <= "~":
            rendered.append(character)
        elif ord(character) <= 0xFF:
            rendered.append(f"\\x{ord(character):02x}")
        else:
            rendered.append(f"\\u{ord(character):04x}")
    text = "".join(rendered)
    if len(text) > MAX_DIAGNOSTIC_LENGTH:
        return text[:MAX_DIAGNOSTIC_LENGTH] + "...[truncated]"
    return text

MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown", ".mkdn", ".mdx"}
CODE_FENCE_PATTERN = re.compile(r"^(`{3,}|~{3,})")

OPENER_PATTERN = re.compile(r"^(<+)(?: (.*))?$")
DIFF3_PATTERN = re.compile(r"^(\|+) *(?: (.*))?$")
SEPARATOR_PATTERN = re.compile(r"^(=+)$")
CLOSER_PATTERN = re.compile(r"^(>+)(?: (.*))?$")


def is_markdown_file(path: str) -> bool:
    """Return True if path has a Markdown extension."""
    return Path(path).suffix.lower() in MARKDOWN_EXTENSIONS


def is_valid_setext_heading(
    lines: list[str], index: int
) -> bool:
    """Return True if an = line underlines a preceding heading."""
    if index == 0:
        return False
    prev_line = lines[index - 1].strip()
    if not prev_line:
        return False
    if CODE_FENCE_PATTERN.match(prev_line):
        return False
    if prev_line.startswith(("#", "<", ">", "|", "=")):
        return False
    return True


def decode_content(
    raw_bytes: bytes, encoding_hint: str | None = None
) -> tuple[str | None, str | None]:
    """Decode raw bytes to text via BOM, attribute encoding, or utf-8.

    Returns (text, error_message). text is None if file is binary.
    """
    if len(raw_bytes) > MAX_FILE_SIZE:
        return None, (
            f"size ({len(raw_bytes)} bytes) exceeds "
            f"limit ({MAX_FILE_SIZE} bytes)"
        )

    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        decoded = raw_bytes.decode("utf-8-sig", errors="replace")
        return decoded.lstrip("\ufeff"), None
    if raw_bytes.startswith(
        (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")
    ):
        decoded = raw_bytes.decode("utf-32", errors="replace")
        return decoded.lstrip("\ufeff"), None
    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        decoded = raw_bytes.decode("utf-16", errors="replace")
        return decoded.lstrip("\ufeff"), None

    if encoding_hint and encoding_hint != "unspecified":
        try:
            decoded = raw_bytes.decode(
                encoding_hint, errors="replace"
            )
            return decoded.lstrip("\ufeff"), None
        except (LookupError, UnicodeDecodeError):
            # The declared working-tree-encoding did not apply. Fall
            # through to UTF-8 rather than reporting, since the attribute
            # can name an encoding this build of Python does not carry.
            pass

    try:
        return raw_bytes.decode("utf-8").lstrip("\ufeff"), None
    except UnicodeDecodeError:
        # Not UTF-8 either, so treat the file as binary below. A file the
        # decoder cannot read carries no conflict markers to find.
        pass

    if b"\x00" in raw_bytes[:4096]:
        return None, None

    return raw_bytes.decode("latin-1"), None


class _BlockScan:
    """The conflict block currently open, carried across lines."""

    def __init__(self, configured_marker_size: int | None):
        self.configured = configured_marker_size
        self.opener_line: int | None = None
        self.opener_len = 0
        self.has_separator = False

    def reportable(self, marker_len: int) -> bool:
        """Return True when a marker of this width is worth reporting.

        Width 3 and up is a real git marker. A narrower one counts only
        where .gitattributes asked for it, since a lone "=" or ">" is
        ordinary text in most files.
        """
        return marker_len >= 3 or (
            self.configured is not None and marker_len == self.configured
        )

    def close(self) -> None:
        """Forget the open block after it is resolved or reported."""
        self.opener_line = None
        self.opener_len = 0
        self.has_separator = False


def _scan_opener(scan: _BlockScan, marker_len: int, number: int,
                 safe_path: str) -> list:
    """Open a block, reporting any previous one this line abandons."""
    violations = []
    if scan.opener_line is not None and scan.reportable(scan.opener_len):
        violations.append(
            f"{safe_path}:{scan.opener_line}: "
            "unclosed conflict marker opener"
        )
    scan.opener_line = number
    scan.opener_len = marker_len
    scan.has_separator = False
    return violations


def _scan_closer(scan: _BlockScan, marker_len: int, number: int,
                 line: str, safe_path: str) -> list:
    """Close a balanced block, or report a mismatched or orphan closer."""
    if (scan.opener_line is not None
            and marker_len == scan.opener_len
            and scan.has_separator):
        violations = [
            f"{safe_path}:{scan.opener_line}-{number}: "
            "unresolved conflict block"
        ]
        scan.close()
        return violations

    if scan.opener_line is None:
        if scan.reportable(marker_len):
            return [
                f"{safe_path}:{number}: "
                f"orphan conflict marker closer '{_sanitize(line)}'"
            ]
        return []

    violations = []
    if scan.reportable(scan.opener_len):
        violations.append(
            f"{safe_path}:{scan.opener_line}: "
            "unclosed conflict marker opener"
        )
    if scan.reportable(marker_len):
        violations.append(
            f"{safe_path}:{number}: "
            f"mismatched conflict marker closer '{_sanitize(line)}'"
        )
    scan.close()
    return violations


def _scan_diff3(scan: _BlockScan, marker_len: int, number: int,
                 line: str, safe_path: str) -> list:
    """Record the diff3 base separator, or report it as an orphan."""
    if scan.opener_line is None:
        if scan.reportable(marker_len):
            return [
                f"{safe_path}:{number}: "
                f"orphan conflict marker separator '{_sanitize(line)}'"
            ]
        return []
    if marker_len == scan.opener_len:
        scan.has_separator = True
    elif scan.reportable(marker_len):
        return [
            f"{safe_path}:{number}: "
            f"orphan conflict marker separator '{_sanitize(line)}'"
        ]
    return []


def _scan_separator(scan: _BlockScan, marker_len: int, number: int,
                    line: str, safe_path: str, markdown: bool,
                    lines: list) -> list:
    """Record the block separator, allowing a Setext heading underline."""
    if scan.opener_line is not None:
        if marker_len == scan.opener_len:
            scan.has_separator = True
        elif (not markdown
              or not is_valid_setext_heading(lines, number - 1)):
            if scan.reportable(marker_len):
                return [
                    f"{safe_path}:{number}: "
                    "orphan conflict marker separator "
                    f"'{_sanitize(line)}'"
                ]
        return []
    if markdown and is_valid_setext_heading(lines, number - 1):
        return []
    if scan.reportable(marker_len):
        return [
            f"{safe_path}:{number}: "
            f"orphan conflict marker separator '{_sanitize(line)}'"
        ]
    return []


def _scan_line(scan: _BlockScan, lines: list, number: int, line: str,
               safe_path: str, markdown: bool) -> list:
    """Dispatch one line to the handler for the marker it carries."""
    opener = OPENER_PATTERN.match(line)
    if opener:
        return _scan_opener(scan, len(opener.group(1)), number, safe_path)
    closer = CLOSER_PATTERN.match(line)
    if closer:
        return _scan_closer(
            scan, len(closer.group(1)), number, line, safe_path)
    diff3 = DIFF3_PATTERN.match(line)
    if diff3:
        return _scan_diff3(
            scan, len(diff3.group(1)), number, line, safe_path)
    separator = SEPARATOR_PATTERN.match(line)
    if separator:
        return _scan_separator(
            scan, len(separator.group(1)), number, line, safe_path,
            markdown, lines)
    return []


def check_content(
    text: str,
    path: str,
    configured_marker_size: int | None = None,
) -> list[str]:
    """Scan text for conflict blocks and orphan markers.

    Parses balanced conflict blocks for any positive marker width (N >= 1).
    Flags unclosed openers, mismatched closers, and orphan markers for
    width >= 3 or matching configured_marker_size. Allows Setext headings
    in Markdown only when preceded by valid heading text.
    """
    safe_path = _sanitize(path)
    lines = text.splitlines()
    markdown = is_markdown_file(path)
    scan = _BlockScan(configured_marker_size)
    violations: list[str] = []

    for number, line in enumerate(lines, 1):
        if len(violations) >= MAX_VIOLATIONS:
            violations.append(
                f"{safe_path}: reached violation limit "
                f"({MAX_VIOLATIONS}), stopping"
            )
            break
        if len(line) > MAX_LINE_LENGTH:
            line = line[:MAX_LINE_LENGTH]
        violations.extend(
            _scan_line(scan, lines, number, line, safe_path, markdown))

    if scan.opener_line is not None and scan.reportable(scan.opener_len):
        violations.append(
            f"{safe_path}:{scan.opener_line}: "
            "unclosed conflict marker opener"
        )

    return violations


def _get_repo_root() -> str:
    """Resolve the git repository root directory."""
    result = subprocess.run(
        ["git", "--no-pager", "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        error_msg = result.stderr.decode(
            "utf-8", errors="replace"
        ).strip()
        raise RuntimeError(
            f"git rev-parse --show-toplevel failed: {error_msg}"
        )
    return result.stdout.decode("utf-8", errors="replace").strip()


def _probe_is_symlink(path: str):
    """Return True, False, or None when the check itself failed."""
    try:
        return os.path.islink(path)
    except OSError:
        return None


def _safe_read(
    path: str, max_size: int
) -> tuple[bytes | None, str | None]:
    """Open, verify the target is regular, and read a bounded amount.

    Where O_NOFOLLOW exists the open refuses a link itself, so the check
    and the open are one operation. Where it does not, a separate probe
    runs first and the window between probe and open stays open. Call
    that narrowed, not closed.

    Returns (data, error_message). data is None on skip or error.
    error_message is None when the file is silently skipped.
    """
    flags = os.O_RDONLY
    nofollow = hasattr(os, "O_NOFOLLOW")
    if nofollow:
        flags |= os.O_NOFOLLOW
    else:
        # Without O_NOFOLLOW the check and the open are separate calls, so
        # this narrows the window rather than closing it. A probe that
        # fails tells us nothing about the path, so refuse it.
        is_link = _probe_is_symlink(path)
        if is_link is None:
            return None, f"could not determine whether {_sanitize(path)} is a link"
        if is_link:
            return None, None

    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, f"file not found: {path}"
    except IsADirectoryError:
        return None, None
    except OSError as err:
        if nofollow and err.errno == errno.ELOOP:
            return None, None  # symlink with O_NOFOLLOW
        return None, f"could not open {path}: {err}"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, None
        if st.st_size > max_size:
            return None, (
                f"{path}: file size ({st.st_size} bytes) "
                f"exceeds limit ({max_size} bytes)"
            )
        data = bytearray()
        while True:
            chunk = os.read(fd, max_size + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_size:
                return None, (
                    f"{path}: read exceeded limit ({max_size} bytes)"
                )
        return bytes(data), None
    finally:
        os.close(fd)


def _parse_marker_size(
    size_str: str | None,
) -> int | None:
    """Parse a conflict-marker-size attribute value."""
    if not size_str or size_str in ("unspecified", "unset"):
        return None
    try:
        return int(size_str)
    except ValueError:
        return None


def _repo_relative_pairs(paths: list, repo_root: str | None) -> list:
    """Pair each given path with the spelling git addresses it by.

    git answers check-attr with forward slashes on every platform, so a
    Windows separator from relpath would lose the lookup.
    """
    if not repo_root:
        return [(p, p) for p in paths]
    norm_root = os.path.abspath(repo_root)
    pairs = []
    for given in paths:
        absolute = (given if os.path.isabs(given)
                    else os.path.join(norm_root, given))
        absolute = os.path.abspath(absolute)
        try:
            inside = os.path.commonpath([norm_root, absolute]) == norm_root
        except ValueError:
            # Different drives on Windows, so the path is not in the repo.
            continue
        if not inside:
            continue
        relative = os.path.relpath(absolute, norm_root).replace(os.sep, "/")
        if os.altsep:
            relative = relative.replace(os.altsep, "/")
        pairs.append((given, relative))
    return pairs


def _run_check_attr(rel_paths: list, repo_root: str | None,
                    cached: bool) -> bytes:
    """Return raw check-attr output for one chunk of repo-relative paths."""
    cmd = [
        "git", "--no-pager",
        "-c", "core.fsmonitor=",
        "check-attr",
        "conflict-marker-size",
        "working-tree-encoding",
        "-z", "--stdin",
    ]
    if cached:
        cmd.append("--cached")
    kwargs: dict = {
        "capture_output": True,
        "check": False,
        "input": b"\x00".join(p.encode("utf-8") for p in rel_paths) + b"\x00",
    }
    if repo_root:
        kwargs["cwd"] = repo_root
    proc = subprocess.run(cmd, **kwargs)
    if proc.returncode != 0:
        error_msg = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"git check-attr failed (exit {proc.returncode}): "
            f"{_sanitize(error_msg)}"
        )
    return proc.stdout


def _parse_check_attr(raw: bytes) -> dict:
    """Return {path: {attribute: value}} from -z check-attr output."""
    parsed: dict = {}
    parts = raw.split(b"\x00")
    for index in range(0, len(parts) - 2, 3):
        file_path = parts[index].decode("utf-8", errors="replace")
        attribute = parts[index + 1].decode("utf-8", errors="replace")
        value = parts[index + 2].decode("utf-8", errors="replace")
        parsed.setdefault(file_path, {})[attribute] = value
    return parsed


def get_git_attributes(
    paths: list[str],
    repo_root: str | None = None,
    cached: bool = False,
) -> dict[str, dict[str, str]]:
    """Query git attributes for marker size and encoding."""
    if not paths:
        return {}
    repo_paths = _repo_relative_pairs(paths, repo_root)
    if not repo_paths:
        return {}

    attributes: dict[str, dict[str, str]] = {}
    for start in range(0, len(repo_paths), CHECK_ATTR_CHUNK):
        chunk = repo_paths[start:start + CHECK_ATTR_CHUNK]
        raw_attrs = _parse_check_attr(
            _run_check_attr([rel for _, rel in chunk], repo_root, cached))
        for given, relative in chunk:
            found = raw_attrs.get(relative, raw_attrs.get(given, {}))
            attributes[given] = found
            attributes[relative] = found
    return attributes


def check_file(
    path: str,
    configured_size: int | None = None,
    encoding_hint: str | None = None,
) -> list[str]:
    """Check a single worktree file for conflict markers.

    Uses _safe_read for race-free, bounded, no-follow-symlink I/O.
    """
    raw_bytes, err = _safe_read(path, MAX_FILE_SIZE)
    if err:
        return [f"error: {_sanitize(err)}"]
    if raw_bytes is None:
        return []

    text, decode_err = decode_content(raw_bytes, encoding_hint)
    if decode_err:
        return [f"error: {path}: {decode_err}"]
    if text is None:
        return []

    return check_content(text, path, configured_size)


def get_tracked_regular_files(
    repo_root: str | None = None,
) -> list[str]:
    """Return all git-tracked regular files.

    Excludes symlinks (120000) and submodules (160000). Callers decide
    whether a skip-worktree entry comes from disk or its index blob.
    """
    if repo_root is None:
        repo_root = _get_repo_root()
    regular: list[str] = []
    for part in _run_ls_files(repo_root, ["-s"]).split(b"\x00"):
        if not part:
            continue
        try:
            file_path, _, _, mode = _parse_index_entry(
                part.decode("utf-8", errors="replace"))
        except (ValueError, IndexError):
            continue
        if mode in REGULAR_MODES:
            regular.append(file_path)
    return regular


def _skip_worktree_paths(repo_root: str) -> set:
    """Return index paths marked skip-worktree, which have no local file.

    A sparse checkout omits them deliberately, so reporting them as
    missing fails a valid working tree.
    """
    skipped = set()
    for part in _run_ls_files(repo_root, ["-v"]).split(b"\x00"):
        if not part:
            continue
        entry = part.decode("utf-8", errors="replace")
        # git ls-files -v tags skip-worktree entries "S", then one space,
        # then the path. A lowercase tag is assume-unchanged, which still
        # has a working-tree file and stays in scope.
        if len(entry) > 2 and entry[0] == "S" and entry[1] == " ":
            skipped.add(entry[2:])
    return skipped


def _run_ls_files(repo_root: str, extra: list) -> bytes:
    """Return raw ls-files output, raising with git's own message."""
    result = subprocess.run(
        ["git", "--no-pager", "-c", "core.fsmonitor=", "ls-files",
         *extra, "-z", "--full-name"],
        capture_output=True,
        check=False,
        cwd=repo_root,
    )
    if result.returncode != 0:
        error_msg = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"git ls-files failed (exit {result.returncode}): "
            f"{_sanitize(error_msg)}"
        )
    return result.stdout


def _parse_index_entry(entry: str):
    """Return (path, sha, stage, mode) for one ls-files -s record."""
    meta, file_path = entry.split("\t", 1)
    fields = meta.split()
    stage = fields[2] if len(fields) > 2 else "0"
    return (file_path, fields[1], stage, fields[0])


def _get_index_entries(
    repo_root: str,
) -> list[tuple[str, str, str, str]]:
    """Return (path, sha, stage, mode) for all index entries."""
    entries: list[tuple[str, str, str, str]] = []
    for part in _run_ls_files(repo_root, ["-s"]).split(b"\x00"):
        if not part:
            continue
        try:
            entries.append(_parse_index_entry(
                part.decode("utf-8", errors="replace")))
        except (ValueError, IndexError):
            # A record git wrote in a shape this parser does not know is
            # not an entry we can speak for; the tracked listing covers it.
            continue
    return entries


@functools.lru_cache(maxsize=1)
def _supports_no_lazy_fetch(repo_root: str) -> bool:
    """Return True when this git accepts the --no-lazy-fetch global option.

    The option arrived in Git 2.45. Ubuntu 24.04 LTS ships 2.43, where every
    object read failed with "unknown option" and the checker reported one
    error per tracked file rather than one message about git.

    Probe rather than parse `git --version`: a distribution backport carries
    the option without carrying the version number.
    """
    probe = subprocess.run(
        ["git", "--no-lazy-fetch", "rev-parse", "--git-dir"],
        capture_output=True, check=False, cwd=repo_root)
    stderr = probe.stderr.decode("utf-8", errors="replace")
    if "unknown option" not in stderr:
        return True
    print(
        "warning: this git predates cat-file --no-lazy-fetch (Git 2.45), so "
        "a partial clone may fetch a missing object while scanning. Object "
        "names still verify their content and replacement refs stay "
        "disabled, so no verdict changes.",
        file=sys.stderr)
    return False


def _object_command(repo_root: str, *args: str) -> list:
    """Return a git object command that neither replaces nor fetches objects.

    --no-replace-objects is the integrity-critical half and every supported
    git has it. --no-lazy-fetch is added only where it exists, because a git
    that rejects it fails the whole scan instead of reading one object.
    """
    command = ["git", "--no-pager", "--no-replace-objects"]
    if _supports_no_lazy_fetch(repo_root):
        command.append("--no-lazy-fetch")
    command += ["-c", "core.fsmonitor=", *args]
    return command


def _get_blob_size(sha: str, repo_root: str) -> int:
    """Query object size without buffering object data."""
    result = subprocess.run(
        _object_command(repo_root, "cat-file", "-s", "--", sha),
        capture_output=True,
        check=False,
        cwd=repo_root,
    )
    if result.returncode != 0:
        error_msg = result.stderr.decode(
            "utf-8", errors="replace"
        ).strip()
        raise RuntimeError(
            f"git cat-file -s {sha[:12]} failed: {error_msg}"
        )
    return int(result.stdout.decode("utf-8", errors="replace").strip())


def _read_blob(
    sha: str, repo_root: str, max_size: int = MAX_FILE_SIZE,
    expected_size: int | None = None,
) -> bytes:
    """Read a single blob from the git object store with size limit."""
    proc = subprocess.Popen(
        _object_command(repo_root, "cat-file", "blob", "--", sha),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=repo_root,
    )
    data, err = proc.communicate()
    if proc.returncode != 0:
        error_msg = err.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"git cat-file blob {sha[:12]} failed: {error_msg}"
        )
    if len(data) > max_size:
        raise RuntimeError(
            f"blob {sha[:12]} size ({len(data)} bytes) exceeds "
            f"limit ({max_size} bytes)"
        )
    if expected_size is not None and len(data) != expected_size:
        raise RuntimeError(
            f"blob {sha[:12]} read {len(data)} bytes; "
            f"expected {expected_size}"
        )
    return data


def _partition_index_entries(entries: list) -> tuple:
    """Split index entries into unmerged violations and readable blobs."""
    violations: list[str] = []
    readable: list = []
    for file_path, sha, stage, mode in entries:
        if stage != "0":
            violations.append(
                f"{_sanitize(file_path)}: unmerged index entry "
                f"(stage {stage})"
            )
        elif mode in REGULAR_MODES:
            readable.append((file_path, sha))
    return violations, readable


def _check_staged_blob(file_path: str, sha: str, repo_root: str,
                       marker_size: int | None) -> list:
    """Check one index blob, returning its violations or a read error."""
    try:
        blob_size = _get_blob_size(sha, repo_root)
    except (RuntimeError, ValueError) as err:
        return [f"error: {_sanitize(file_path)}: {_sanitize(err)}"]
    if blob_size > MAX_FILE_SIZE:
        return [
            f"error: {_sanitize(file_path)}: blob size "
            f"({blob_size} bytes) exceeds limit "
            f"({MAX_FILE_SIZE} bytes)"
        ]
    try:
        raw_bytes = _read_blob(
            sha, repo_root, MAX_FILE_SIZE, expected_size=blob_size)
    except RuntimeError as err:
        return [f"error: {_sanitize(err)}"]

    # Index blobs are stored canonically, so working-tree-encoding does
    # not apply when decoding them.
    text, decode_err = decode_content(raw_bytes, None)
    if decode_err:
        return [f"error: {_sanitize(file_path)}: {_sanitize(decode_err)}"]
    if text is None:
        return []
    return check_content(text, file_path, marker_size)


def _check_staged(repo_root: str) -> list[str]:
    """Check staged index blobs for conflict markers.

    Reads from the object store, not the worktree, and flags unmerged
    entries (stage > 0) as violations.
    """
    violations, readable = _partition_index_entries(
        _get_index_entries(repo_root))

    paths = [os.path.join(repo_root, path) for path, _ in readable]
    try:
        attributes = get_git_attributes(
            paths, repo_root=repo_root, cached=True)
    except RuntimeError as err:
        return [f"error: {_sanitize(err)}"]

    for file_path, sha in readable:
        if len(violations) >= MAX_VIOLATIONS:
            violations.append(
                f"reached violation limit ({MAX_VIOLATIONS}), stopping")
            break
        marker_size = _parse_marker_size(
            attributes.get(file_path, {}).get("conflict-marker-size"))
        violations.extend(
            _check_staged_blob(file_path, sha, repo_root, marker_size))

    return violations


def _check_all(repo_root: str) -> list[str]:
    """Check present worktree files and absent skip-worktree index blobs."""
    violations, readable = _partition_index_entries(
        _get_index_entries(repo_root))
    skipped = _skip_worktree_paths(repo_root)

    def _path_exists(candidate_path: str) -> bool:
        return os.path.lexists(os.path.join(repo_root, candidate_path))

    present = [path for path, _ in readable if _path_exists(path)]
    absent_skipped = [
        (path, sha) for path, sha in readable
        if path in skipped and not _path_exists(path)
    ]
    worktree_attributes = get_git_attributes(
        present, repo_root=repo_root)
    cached_attributes = get_git_attributes(
        [path for path, _ in absent_skipped], repo_root=repo_root,
        cached=True)

    for file_path, sha in readable:
        if len(violations) >= MAX_VIOLATIONS:
            violations.append(
                f"reached violation limit ({MAX_VIOLATIONS}), stopping")
            break
        if file_path in skipped and not _path_exists(file_path):
            attrs = cached_attributes.get(file_path, {})
            violations.extend(_check_staged_blob(
                file_path, sha, repo_root,
                _parse_marker_size(attrs.get("conflict-marker-size"))))
            continue
        if not _path_exists(file_path):
            continue
        attrs = worktree_attributes.get(file_path, {})
        violations.extend(check_file(
            file_path,
            _parse_marker_size(attrs.get("conflict-marker-size")),
            attrs.get("working-tree-encoding"),
        ))
    return violations


def _run_immutable_git(
    repo_root: str, args: list[str], input_bytes: bytes | None = None,
    env: dict | None = None,
) -> bytes:
    """Run a non-fetching Git object command without replacement refs."""
    command = _object_command(repo_root, *args)
    result = subprocess.run(
        command, cwd=repo_root, input=input_bytes, capture_output=True,
        check=False, env=env)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"git {args[0]} failed (exit {result.returncode}): "
            f"{_sanitize(message)}"
        )
    return result.stdout


def _validate_object_id(object_id: str) -> str:
    """Return a normalized validated hexadecimal object ID."""
    if not OBJECT_ID_PATTERN.fullmatch(object_id):
        raise RuntimeError(
            "--tree requires a validated hexadecimal object id "
            "with 40 or 64 characters"
        )
    return object_id.lower()


def _validate_tree_path(raw_path: bytes) -> str:
    """Decode and validate one repository-relative tree path."""
    try:
        file_path = raw_path.decode("utf-8")
    except UnicodeDecodeError as err:
        raise RuntimeError("tree entry path is not valid UTF-8") from err
    parts = file_path.split("/")
    if (not file_path or file_path.startswith("/")
            or any(part in ("", ".", "..") for part in parts)):
        raise RuntimeError(
            f"malformed tree entry path: {_sanitize(file_path)}")
    return file_path


def _parse_tree_entries(raw: bytes) -> list[tuple[str, str]]:
    """Parse complete ls-tree output into regular blob paths and IDs."""
    if raw and not raw.endswith(b"\x00"):
        raise RuntimeError("truncated git ls-tree output")
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for record in raw[:-1].split(b"\x00") if raw else []:
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, raw_id = metadata.split(b" ")
            object_id = raw_id.decode("ascii")
        except (ValueError, UnicodeDecodeError) as err:
            raise RuntimeError("malformed git ls-tree entry") from err
        file_path = _validate_tree_path(raw_path)
        if file_path in seen or not OBJECT_ID_PATTERN.fullmatch(object_id):
            raise RuntimeError(
                f"malformed git ls-tree entry: {_sanitize(file_path)}")
        seen.add(file_path)
        if (mode, object_type) in ((b"100644", b"blob"),
                                   (b"100755", b"blob")):
            entries.append((file_path, object_id.lower()))
        elif (mode, object_type) not in ((b"120000", b"blob"),
                                        (b"160000", b"commit")):
            raise RuntimeError(
                f"unsupported git ls-tree entry: {_sanitize(file_path)}")
    return entries


def _parse_tree_attributes(
    raw: bytes, requested: list[str],
) -> dict[str, dict[str, str]]:
    """Parse complete check-attr output and require every requested value."""
    if raw and not raw.endswith(b"\x00"):
        raise RuntimeError("truncated git check-attr output")
    parts = raw[:-1].split(b"\x00") if raw else []
    if len(parts) % 3 != 0:
        raise RuntimeError("malformed git check-attr output")
    expected = set(requested)
    parsed: dict[str, dict[str, str]] = {}
    for index in range(0, len(parts), 3):
        try:
            path = parts[index].decode("utf-8")
            attribute = parts[index + 1].decode("ascii")
            value = parts[index + 2].decode("utf-8")
        except UnicodeDecodeError as err:
            raise RuntimeError("malformed git check-attr output") from err
        values = parsed.setdefault(path, {})
        if path not in expected or attribute in values:
            raise RuntimeError("unexpected git check-attr output")
        values[attribute] = value
    required = {"conflict-marker-size", "working-tree-encoding"}
    if set(parsed) != expected or any(set(value) != required
                                     for value in parsed.values()):
        raise RuntimeError("incomplete git check-attr output")
    return parsed


def _isolated_git_env(repo_root: str, config_root: str) -> dict:
    """Build an environment whose attributes come only from source objects."""
    raw_objects = _run_immutable_git(
        repo_root, ["rev-parse", "--git-path", "objects"])
    try:
        object_path = raw_objects.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError as err:
        raise RuntimeError("git object directory is not valid UTF-8") from err
    if "\n" in object_path or "\r" in object_path:
        raise RuntimeError("git returned a malformed object directory")
    if not os.path.isabs(object_path):
        object_path = os.path.join(repo_root, object_path)
    object_path = os.path.abspath(object_path)
    if not os.path.isdir(object_path):
        raise RuntimeError("git object directory does not exist")
    env = os.environ.copy()
    env.update({
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": object_path,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "HOME": config_root,
        "XDG_CONFIG_HOME": config_root,
    })
    return env


def _get_tree_attributes(
    paths: list[str], repo_root: str, object_id: str,
) -> dict[str, dict[str, str]]:
    """Read attributes from an isolated index populated by the exact tree."""
    if not paths:
        return {}
    attributes: dict[str, dict[str, str]] = {}
    with tempfile.TemporaryDirectory() as temp_dir:
        env = _isolated_git_env(repo_root, temp_dir)
        init = subprocess.run(
            ["git", "--no-pager", "-c", "init.templateDir=", "init",
             "--bare", "--quiet", temp_dir],
            capture_output=True, check=False, env=env)
        if init.returncode != 0:
            message = init.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"git init failed: {_sanitize(message)}")
        git_dir = f"--git-dir={temp_dir}"
        _run_immutable_git(
            repo_root, [git_dir, "read-tree", object_id], env=env)
        for start in range(0, len(paths), CHECK_ATTR_CHUNK):
            chunk = paths[start:start + CHECK_ATTR_CHUNK]
            stdin = b"\x00".join(path.encode("utf-8") for path in chunk)
            raw = _run_immutable_git(
                repo_root,
                [git_dir, "check-attr", "conflict-marker-size",
                 "working-tree-encoding", "-z", "--stdin", "--cached"],
                input_bytes=stdin + b"\x00", env=env)
            attributes.update(_parse_tree_attributes(raw, chunk))
    return attributes


def _check_tree(repo_root: str, raw_object_id: str) -> list[str]:
    """Check regular blobs and attributes from one immutable commit or tree."""
    object_id = _validate_object_id(raw_object_id)
    object_type = _run_immutable_git(
        repo_root, ["cat-file", "-t", "--", object_id])
    if object_type not in (b"commit\n", b"tree\n"):
        raise RuntimeError("--tree object must be a commit or tree")
    raw_entries = _run_immutable_git(
        repo_root, ["ls-tree", "-r", "-z", "--full-tree", object_id])
    entries = _parse_tree_entries(raw_entries)
    attributes = _get_tree_attributes(
        [path for path, _ in entries], repo_root, object_id)
    violations: list[str] = []
    for file_path, blob_id in entries:
        if len(violations) >= MAX_VIOLATIONS:
            violations.append(
                f"reached violation limit ({MAX_VIOLATIONS}), stopping")
            break
        marker_size = _parse_marker_size(
            attributes[file_path].get("conflict-marker-size"))
        violations.extend(_check_staged_blob(
            file_path, blob_id, repo_root, marker_size))
    return violations


def _collect_files(resolved: list, repo_root: str) -> list:
    """Return the files to check, expanding --all against the index."""
    if not resolved or resolved == ["--all"]:
        return get_tracked_regular_files(repo_root)
    files: list[str] = []
    for arg in resolved:
        if arg == "--all":
            files.extend(get_tracked_regular_files(repo_root))
        else:
            files.append(arg)
    return files


def _report(violations: list) -> int:
    """Print violations and return the exit code they imply."""
    for violation in violations:
        print(violation, file=sys.stderr)
    return 1 if violations else 0


def _check_worktree(args: list, repo_root: str) -> list:
    """Check working-tree files, resolving paths before the chdir."""
    resolved = [os.path.abspath(arg) for arg in args if arg != "--all"]
    os.chdir(repo_root)
    violations = _check_all(repo_root) if not args or "--all" in args else []
    attributes = get_git_attributes(resolved, repo_root=repo_root)

    for path in resolved:
        file_attrs = attributes.get(path, {})
        violations.extend(check_file(
            path,
            _parse_marker_size(file_attrs.get("conflict-marker-size")),
            file_attrs.get("working-tree-encoding"),
        ))
    return violations


def _parse_cli(raw_args: list[str]) -> tuple[bool, list[str], str | None,
                                             str | None]:
    """Parse the staged, tree, and worktree CLI modes."""
    staged = False
    tree_id: str | None = None
    repo_path: str | None = None
    files: list[str] = []
    index = 0
    while index < len(raw_args):
        argument = raw_args[index]
        if argument == "--staged":
            staged = True
        elif argument in ("--tree", "--repo"):
            if index + 1 >= len(raw_args):
                raise RuntimeError(f"{argument} requires a value")
            value = raw_args[index + 1]
            index += 1
            if argument == "--tree":
                if tree_id is not None:
                    raise RuntimeError("--tree may be supplied only once")
                tree_id = value
            else:
                if repo_path is not None:
                    raise RuntimeError("--repo may be supplied only once")
                repo_path = value
        else:
            files.append(argument)
        index += 1
    if tree_id is not None or repo_path is not None:
        if tree_id is None or repo_path is None:
            raise RuntimeError("--tree and --repo must be supplied together")
        if staged or files:
            raise RuntimeError("--tree mode does not accept other modes or files")
    return staged, files, tree_id, repo_path


def _resolve_repo_path(repo_path: str) -> str:
    """Resolve an existing repository directory without changing cwd."""
    try:
        resolved = Path(repo_path).resolve(strict=True)
    except OSError as err:
        raise RuntimeError(
            f"could not resolve --repo path: {_sanitize(err)}") from err
    if not resolved.is_dir():
        raise RuntimeError("--repo path is not a directory")
    return str(resolved)


def main() -> int:
    """Entry point for CLI execution."""
    try:
        staged, args, tree_id, repo_path = _parse_cli(sys.argv[1:])
        if tree_id is not None and repo_path is not None:
            repo_root = _resolve_repo_path(repo_path)
            return _report(_check_tree(repo_root, tree_id))
        repo_root = _get_repo_root()
        if staged:
            return _report(_check_staged(repo_root))
        return _report(_check_worktree(args, repo_root))
    except RuntimeError as err:
        print(f"error: {_sanitize(err)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
