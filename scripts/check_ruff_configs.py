"""Fail when any Ruff config other than the root ruff.toml exists.

Ruff picks the nearest config file for each file it checks, and `.ruff.toml` outranks `ruff.toml`.
A config added anywhere in the tree can therefore replace the baseline for the files beside it.
CI passes `--config ruff.toml` so Ruff ignores such files; this check makes their presence a failure
instead of a silent no-op. `ruff.warn.toml` is not a name Ruff discovers, so it is allowed.
Folders Ruff excludes by default are skipped, since Ruff never reads configs inside them.

Usage (from the repository root, wherever the script is kept):
  python scripts/check_ruff_configs.py
"""

import argparse
import os
import pathlib
import sys
import tomllib

BASELINE_FILE = "ruff.toml"
RUFF_CONFIG_NAMES = ("ruff.toml", ".ruff.toml")
PYPROJECT_FILE = "pyproject.toml"
# Ruff's default `exclude` list (0.16.9, from `ruff check --isolated --show-settings`).
SKIPPED_DIRS = frozenset({
    ".bzr",
    ".direnv",
    ".eggs",
    ".git",
    ".git-rewrite",
    ".hg",
    ".ipynb_checkpoints",
    ".mypy_cache",
    ".nox",
    ".pants.d",
    ".pyenv",
    ".pytest_cache",
    ".pytype",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    ".vscode",
    "__pypackages__",
    "_build",
    "buck-out",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
})


def configures_ruff(pyproject: pathlib.Path) -> bool:
    """Return whether a pyproject.toml holds Ruff settings.

    A file that cannot be parsed counts as a Ruff config, since the checker cannot prove otherwise.
    """
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return True
    tool = data.get("tool", {})
    return not isinstance(tool, dict) or "ruff" in tool


def is_stray_config(root: pathlib.Path, path: pathlib.Path) -> bool:
    """Return whether the file at path is a Ruff config other than root/ruff.toml."""
    if path.name in RUFF_CONFIG_NAMES:
        return path != root / BASELINE_FILE
    return path.name == PYPROJECT_FILE and configures_ruff(path)


def find_stray_configs(root: pathlib.Path) -> list[pathlib.Path]:
    """Return every Ruff config under root except root/ruff.toml, sorted by path.

    Returns:
        The stray config paths.
    """
    stray = []
    for directory, subdirs, files in os.walk(root):
        # Prune in place so os.walk never descends into excluded folders.
        subdirs[:] = [name for name in subdirs if name not in SKIPPED_DIRS]
        stray.extend(p for p in (pathlib.Path(directory, name) for name in files) if is_stray_config(root, p))
    return sorted(stray)


def main(argv: list[str] | None = None) -> int:
    """Report stray Ruff configs.

    Returns:
        0 when only the baseline config exists, else 1.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path(), help="repository root (default: .)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    stray = find_stray_configs(args.root)
    for path in stray:
        label = path.relative_to(args.root).as_posix()
        print(f"{label}: Ruff config outside {BASELINE_FILE}. Remove it; change the register instead.", file=sys.stderr)
    if stray:
        return 1
    print(f"No Ruff config besides {BASELINE_FILE}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
