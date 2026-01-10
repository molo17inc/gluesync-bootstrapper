# This program is part of Gluesync.
#
# Automator is dual-licensed under the following licenses:
#
# 1. GNU General Public License (GPL) Version 3
#    You may use, modify, and distribute this software under the terms of the GPL v3.
#    See the LICENSE-GPL file or <http://www.gnu.org/licenses/gpl-3.0.html> for details.
#    This option is available at no cost, but any derivative works must also be licensed under GPL v3.
#
# 2. MOLO17 Commercial License
#    Alternatively, you may use this software under the MOLO17 Commercial License,
#    which includes a warranty and permits proprietary use. Contact MOLO17 at info@molo17.com
#    for licensing terms and conditions.
#
# You must choose one of these licenses to use this software. Using this software implies
# acceptance of one of these licenses. See the accompanying LICENSE files or contact
# MOLO17 for more information.
#
# Copyright (C) 2025 MOLO17. All rights reserved.

"""Command-line entrypoint for the Gluesync Automator."""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import webbrowser

import uvicorn

from .app import create_app

LOGGER = logging.getLogger(__name__)


def _show_error_dialog(title: str, message: str) -> None:
    """Show a native macOS error dialog."""
    if sys.platform == 'darwin':
        try:
            from AppKit import NSAlert, NSAlertStyleCritical, NSApp, NSApplication
            NSApplication.sharedApplication()
            alert = NSAlert.alloc().init()
            alert.setMessageText_(title)
            alert.setInformativeText_(message)
            alert.setAlertStyle_(NSAlertStyleCritical)
            alert.runModal()
        except ImportError:
            LOGGER.error("%s: %s", title, message)
    else:
        LOGGER.error("%s: %s", title, message)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Gluesync Automator web UI")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on (default: 8080)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Uvicorn log level (default: info)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload (development only)",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the Automator UI in the default browser after startup",
    )
    return parser.parse_args(argv)


def _is_port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _find_available_port(host: str, start_port: int, max_attempts: int = 100) -> int:
    """Find an available port starting from start_port."""
    import random
    
    # Try the requested port first
    if not _is_port_in_use(host, start_port):
        return start_port
    
    # Try random ports in the range 8000-9000
    for _ in range(max_attempts):
        port = random.randint(8000, 9000)
        if not _is_port_in_use(host, port):
            LOGGER.info("Port %d was in use, using port %d instead", start_port, port)
            return port
    
    # Fallback: let the OS assign a port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        port = sock.getsockname()[1]
        LOGGER.info("Using OS-assigned port %d", port)
        return port


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    try:
        # Find an available port instead of failing
        port = _find_available_port(args.host, args.port)
        if port != args.port:
            LOGGER.info("Requested port %d was in use, using port %d", args.port, port)
        args.port = port

        app = create_app()
        config = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            reload=args.reload,
            lifespan="on",
        )
        server = uvicorn.Server(config)

        url = f"http://{args.host}:{args.port}"

        # Run server in background thread so GUI can run in main thread
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()

        # Wait for server to start
        import time
        max_wait = 10
        for _ in range(max_wait * 10):
            if _is_port_in_use(args.host, args.port):
                break
            time.sleep(0.1)
        else:
            error_msg = "Server failed to start within timeout"
            LOGGER.error(error_msg)
            _show_error_dialog("Gluesync Automator - Startup Error", error_msg)
            raise SystemExit(1)

        # Open browser
        LOGGER.info("Opening browser at %s", url)
        import subprocess
        try:
            subprocess.run(['open', url], check=True)
        except Exception as e:
            LOGGER.warning("Failed to open with 'open' command: %s", e)
            webbrowser.open(url)
        
        # Keep server running
        LOGGER.info("Server running at %s", url)
        try:
            server_thread.join()
        except KeyboardInterrupt:
            LOGGER.info("Gluesync Automator interrupted by user")
            raise SystemExit(130) from None
                
    except Exception as e:
        error_msg = f"Failed to start Gluesync Automator: {str(e)}"
        LOGGER.exception(error_msg)
        _show_error_dialog("Gluesync Automator - Error", error_msg)
        raise SystemExit(1) from e


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
