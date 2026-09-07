# This program is part of Gluesync.
#
# Bootstrapper is dual-licensed under the following licenses:
#
# 1. GNU General Public License (GPL) Version 3
# 2. MOLO17 Commercial License
#
# Copyright (C) 2025 MOLO17. All rights reserved.

"""Private on-disk location for the Automator → MCP shared token file.

Never write this under the repository root. The file holds a CoreHub JWT and
must stay in a user-private directory with restrictive permissions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

TOKEN_FILENAME = ".gluesync_mcp_token"


def mcp_token_dir() -> Path:
    """Return a user-private directory for Automator/MCP runtime credentials."""
    override = os.environ.get("GLUESYNC_MCP_TOKEN_DIR")
    if override:
        return Path(override).expanduser()

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE")
        if not base:
            base = str(Path.home())
        return Path(base) / "GluesyncAutomator"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "GluesyncAutomator"

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "gluesync-automator"
    return Path.home() / ".local" / "state" / "gluesync-automator"


def mcp_token_file_path() -> Path:
    """Absolute path of the MCP token file.

    Override with ``GLUESYNC_MCP_TOKEN_FILE`` when clients cannot share the
    default private directory (e.g. some stdio MCP hosts).
    """
    override = os.environ.get("GLUESYNC_MCP_TOKEN_FILE")
    if override:
        return Path(override).expanduser()
    return mcp_token_dir() / TOKEN_FILENAME


def ensure_mcp_token_parent(path: Path) -> None:
    """Create the parent directory with restrictive permissions when possible."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass


def write_mcp_token_file(contents: str) -> Path:
    """Write ``contents`` to the private token path with mode 0600. Returns path."""
    path = mcp_token_file_path()
    ensure_mcp_token_parent(path)
    # Prefer atomic-ish write with explicit mode on POSIX.
    flags = os.O_WRONLY
    flags |= os.O_CREAT
    flags |= os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path


def remove_mcp_token_file() -> None:
    """Delete the private token file if it exists."""
    path = mcp_token_file_path()
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
