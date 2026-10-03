#!/usr/bin/env python3
"""Require immutable commit pins for external workflow actions."""
import sys
from pathlib import Path

import yaml

FULL_SHA_LENGTH = 40
CONTAINER_DIGEST_LENGTH = 64


def _workflow_paths(root: Path) -> list[Path]:
    """Return workflow files beneath the configured directory."""
    workflow_root = root / ".github" / "workflows"
    return sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")))


def _references(value: object, path: str = "") -> list[tuple[str, str]]:
    """Collect uses values from nested YAML data."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key == "uses" and isinstance(child, str):
                found.append((child_path, child))
            found.extend(_references(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_references(child, f"{path}[{index}]"))
    return found


def find_violations(text: str, path: str) -> list[str]:
    """Return violations for external actions without full SHA pins."""
    try:
        documents = list(yaml.safe_load_all(text))
    except yaml.YAMLError as error:
        return [f"{path}: invalid YAML ({error})"]

    violations = []
    for document in documents:
        for location, reference in _references(document):
            if reference.startswith("./"):
                continue
            revision = reference.rsplit("@", 1)[-1] if "@" in reference else ""
            if reference.startswith("docker://"):
                digest = revision.removeprefix("sha256:")
                if len(digest) != CONTAINER_DIGEST_LENGTH or any(
                        character not in "0123456789abcdefABCDEF"
                        for character in digest):
                    violations.append(
                        f"{path}: {location} must use a full image digest")
                continue
            if len(revision) != FULL_SHA_LENGTH or any(
                    character not in "0123456789abcdefABCDEF"
                    for character in revision):
                violations.append(
                    f"{path}: {location} must use a full commit SHA")
    return violations


def main() -> int:
    """Check all workflow files and return a blocking status."""
    root = Path.cwd()
    violations = []
    for path in _workflow_paths(root):
        violations.extend(find_violations(path.read_text(encoding="utf-8"), str(path)))
    for violation in violations:
        print(violation, file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
