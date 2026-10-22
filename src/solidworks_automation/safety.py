"""File-system policy for anything the server writes to disk.

Saves and exports go to an output folder (default ``~/Documents/SolidWorksMCP``).
Other folders must be listed in ``SW_MCP_ALLOWED_DIRS`` (``os.pathsep``-separated).
Existing files are never replaced unless the caller passes ``overwrite=True``.
"""

from __future__ import annotations

import os
from pathlib import Path


class PolicyError(Exception):
    """Raised when a request would break the write policy."""


def output_dir() -> Path:
    configured = os.environ.get("SW_MCP_OUTPUT_DIR")
    path = Path(configured) if configured else Path.home() / "Documents" / "SolidWorksMCP"
    return path.expanduser().resolve()


def allowed_dirs() -> list[Path]:
    dirs = [output_dir()]
    for entry in os.environ.get("SW_MCP_ALLOWED_DIRS", "").split(os.pathsep):
        if entry.strip():
            dirs.append(Path(entry.strip()).expanduser().resolve())
    return dirs


def resolve_write_path(requested: str, *, allowed_suffixes: set[str], overwrite: bool) -> Path:
    """Turn a user-supplied path into an absolute path we are allowed to write.

    A bare file name lands in the output folder. Absolute paths must sit inside
    an allowed folder.
    """
    if not requested or not requested.strip():
        raise PolicyError("A file name is required.")
    candidate = Path(requested.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = output_dir() / candidate
    candidate = candidate.resolve()

    if candidate.suffix.lower() not in allowed_suffixes:
        raise PolicyError(
            f"Unsupported file type '{candidate.suffix}'. Allowed: {', '.join(sorted(allowed_suffixes))}."
        )
    if not any(candidate.is_relative_to(d) for d in allowed_dirs()):
        raise PolicyError(
            f"Writing to '{candidate.parent}' is not allowed. Use the output folder "
            f"'{output_dir()}' or add the folder to SW_MCP_ALLOWED_DIRS."
        )
    if candidate.exists() and not overwrite:
        raise PolicyError(f"'{candidate}' already exists. Pass overwrite=true to replace it.")

    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate
