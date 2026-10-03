#!/usr/bin/env python3
"""Define the optional handoff policy scope."""
HANDOFF_PATH = "plan/HANDOFF.md.example"


def is_handoff_path(path: str) -> bool:
    """Return whether a normalized path names the handoff template."""
    return path.replace("\\", "/") == HANDOFF_PATH
