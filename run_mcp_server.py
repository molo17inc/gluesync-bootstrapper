#!/usr/bin/env python3
"""Wrapper script to launch the MCP stdio server from any CWD.

Claude Desktop and other MCP clients may not set the working directory
to the project root, causing `python3 -m mcp_server.server` to fail with
`ModuleNotFoundError: No module named 'mcp_server'`.

This wrapper adds the project root to sys.path before importing, so it
works regardless of CWD.  Configure your MCP client to run:

    python3 /path/to/gluesync-bootstrapper/run_mcp_server.py

instead of:

    python3 -m mcp_server.server
"""
import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from mcp_server.server import run_stdio

if __name__ == "__main__":
    run_stdio()
