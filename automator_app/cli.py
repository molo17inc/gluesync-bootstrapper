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
import os
import socket
import sys
import threading
from typing import Optional
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


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Gluesync Automator web UI")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on (default: 8080, auto-selects if in use)",
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
        default=True,
        help="Open the Automator UI in the default browser after startup (default: True)",
    )
    parser.add_argument(
        "--no-open-browser",
        dest="open_browser",
        action="store_false",
        help="Don't open browser on startup",
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


def _run_server_only(args):
    """Run only the uvicorn server in a separate process."""
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
    server.run()


def _run_server(args):
    """Run the uvicorn server in a separate process."""
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
    server.run()

def _wait_for_server_ready(host: str, port: int, timeout: int = 10) -> bool:
    """Wait until host:port is reachable or timeout expires."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_port_in_use(host, port):
            LOGGER.info("Server reachable at %s:%d", host, port)
            return True
        time.sleep(0.1)
    LOGGER.error("Timed out waiting for server at %s:%d", host, port)
    return False


def _open_browser(url: str) -> None:
    LOGGER.info("Opening browser/webview at %s", url)
    import subprocess

    if sys.platform == "darwin":
        try:
            subprocess.run(['open', url], check=True)
            return
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("Failed to open with 'open' command: %s", err)
    webbrowser.open(url)


def _resolve_icon_path() -> Optional[str]:
    """Return absolute path to the tray icon, handling PyInstaller bundles."""
    icon_rel = os.path.join('automator_app', 'static', 'favicon.ico')
    base_path = getattr(sys, '_MEIPASS', os.path.abspath(os.path.dirname(__file__)))
    candidate = os.path.join(base_path, icon_rel)
    if os.path.exists(candidate):
        return candidate

    repo_candidate = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), 'static', 'favicon.ico')
    if os.path.exists(repo_candidate):
        return repo_candidate

    LOGGER.warning("Tray icon not found at %s or %s", candidate, repo_candidate)
    return None


def _start_uvicorn_thread(args: argparse.Namespace) -> threading.Thread:
    """Start uvicorn server in a background thread and wait until it's reachable."""
    server_thread = threading.Thread(target=_run_server, args=(args,), daemon=True)
    server_thread.start()
    LOGGER.info("Server thread started, waiting for port to be reachable...")
    if not _wait_for_server_ready(args.host, args.port):
        error_msg = "Server failed to start within timeout"
        LOGGER.error(error_msg)
        _show_error_dialog("Gluesync Automator - Startup Error", error_msg)
        raise SystemExit(1)
    return server_thread


class GluesyncTrayApp:
    """PyQt6 system tray icon that opens the web UI in the default browser."""

    def __init__(self, server_args: argparse.Namespace, server_url: str):
        LOGGER.info("Initializing PyQt6 tray app")

        LOGGER.info("Importing PyQt6 modules...")
        from PyQt6 import QtCore, QtGui, QtWidgets

        LOGGER.info("Creating QApplication instance...")
        self.qt_app = QtWidgets.QApplication(sys.argv or [])
        self.qt_app.setQuitOnLastWindowClosed(False)
        self.server_args = server_args
        self.server_url = server_url

        LOGGER.info("Resolving icon path...")
        icon_path = _resolve_icon_path()
        icon = QtGui.QIcon(icon_path) if icon_path else QtGui.QIcon()
        LOGGER.info("Icon loaded: %s (null=%s)", icon_path, icon.isNull())

        # Tray setup
        LOGGER.info("Creating system tray icon...")
        if not icon.isNull():
            self.tray_icon = QtWidgets.QSystemTrayIcon(icon, self.qt_app)
        else:
            self.tray_icon = QtWidgets.QSystemTrayIcon(self.qt_app)
        self.tray_icon.setToolTip("Gluesync Automator")
        menu = QtWidgets.QMenu()

        open_action = menu.addAction("Open Gluesync Automator")
        open_action.triggered.connect(self.show_window)

        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self.quit_app)

        self.tray_icon.setContextMenu(menu)
        LOGGER.info("Showing tray icon...")
        self.tray_icon.show()
        self.tray_icon.activated.connect(self._handle_activation)
        LOGGER.info("Tray icon initialized and visible")

        # Auto-open window on start if requested
        if self.server_args.open_browser:
            LOGGER.info("Auto-opening window (--open-browser flag set)")
            self.show_window()

    def show_window(self):
        LOGGER.info("Opening browser window")
        _open_browser(self.server_url)

    def quit_app(self):
        LOGGER.info("Quit requested from tray menu")
        self.tray_icon.hide()
        self.qt_app.quit()

    def run(self) -> int:
        LOGGER.info("Starting PyQt6 event loop")
        return self.qt_app.exec()

    def _handle_activation(self, reason):
        """Open the window on single click."""
        from PyQt6 import QtWidgets

        if reason == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger:
            LOGGER.info("Tray icon activated via click")
            self.show_window()


def main(argv: Optional[list[str]] = None) -> None:
    # Filter out multiprocessing fork arguments that shouldn't be parsed
    if argv is None:
        argv = sys.argv[1:]
    
    # Remove multiprocessing-specific arguments - be more aggressive
    filtered_argv = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        
        # Skip multiprocessing-related arguments
        if (arg.startswith('--multiprocessing-fork') or 
            arg in ['-B', '-S', '-I', '-c'] or
            arg.startswith('tracker_fd=') or 
            arg.startswith('pipe_handle=') or
            'multiprocessing.resource_tracker' in arg or
            'from multiprocessing' in arg):
            # Skip this argument and possibly the next one if it's a parameter
            i += 1
            if i < len(argv) and not argv[i].startswith('-'):
                i += 1
            continue
            
        filtered_argv.append(arg)
        i += 1
    
    args = _parse_args(filtered_argv)

    try:
        # If the user explicitly passed --port, respect it exactly.
        # Only auto-discover a free port when the default (8080) is used.
        if args.port is None:
            args.port = _find_available_port(args.host, 8080)
        # else: use the explicit port as-is

        url = f"http://{args.host}:{args.port}"

        # Start uvicorn server in background thread
        server_thread = _start_uvicorn_thread(args)

        # Attempt to start PyQt tray app
        try:
            LOGGER.info("Launching PyQt6 tray/webview shell")
            tray_app = GluesyncTrayApp(args, url)
            tray_app.run()
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("PyQt6 tray failed (%s). Falling back to headless mode.", exc)
            if args.open_browser:
                _open_browser(url)
            LOGGER.info("Server running headless at %s. Press Ctrl+C to exit.", url)
            try:
                server_thread.join()
            except KeyboardInterrupt:
                LOGGER.info("Gluesync Automator interrupted by user")
                raise SystemExit(130) from None
            return

    except Exception as e:
        LOGGER.error("Failed to start Gluesync Automator: %s", e, exc_info=True)
        _show_error_dialog("Gluesync Automator - Startup Error", f"Failed to start: {e}")
        raise SystemExit(1) from e
